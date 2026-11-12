# -*- coding: utf-8 -*-
"""视频素材统一来源层（解码 / 帧缓存 / 波形）—— v3.0 视频工作台。

纯 Python（numpy + cv2 + PIL + 标准库），**不 import 任何 Qt**；
**绝不 import l2d_rig_qt**（__main__ 双实例坑），PuppetSource 只懒加载
无 UI 的 l2d_rig_core。

统一契约（外层预览/导出只依赖这个）：
    class FrameSource:                    # 基类
        width: int   height: int          # 素材原生尺寸
        fps: float                        # 视频源帧率；image=0；puppet=工程 fps
        src_dur: float                    # 素材总时长秒；image=0（表示无限长）
        frame(t: float) -> np.ndarray     # RGB uint8 (H,W,3)；t 越界夹到 [0, src_dur]
        close()
    open_source(source: dict) -> FrameSource   # source = clip["source"]
    probe_source(path) -> source dict          # kind 自动判定

音频工具（预览时间轴/波形图用）：
    AudioLoader.load_samples(path) -> (np.int16 单声道, sr)  失败 (None, 0)
    waveform(path, buckets=200) -> list[float] 0..1 包络     失败 []

已知坑（都已处理）：
  - Windows 下 cv2.imread 读中文路径返回 None -> np.fromfile + cv2.imdecode 兜底
  - ffmpeg.exe 找不到 -> 子进程不抛异常，返回 (None, 0) / src_dur=0
  - subprocess 一律 DEVNULL/临时文件，不用管道（GUI 无黑框、沙箱兼容）
"""
import json
import os
import subprocess
import sys
import tempfile
import wave
from collections import OrderedDict

import numpy as np
import cv2
from PIL import Image

# 应用目录（本文件所在处）。FFMPEG_CANDIDATES 值复制自 l2d_rig_qt.py:119
# ——规格要求"复制常量不要 import"，两处若挪动需同步。
V_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG_CANDIDATES = [os.path.join(V_DIR, "ffmpeg.exe"),
                     r"D:\BaiduNetdiskDownload\Vocal2Midi\ffmpeg.exe"]

VIDEO_EXTS = (".mp4", ".avi", ".mov", ".mkv", ".webm")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac")

CREATE_NO_WINDOW = 0x08000000      # Windows：子进程不弹控制台黑框


class SourceClosed(RuntimeError):
    """close() 之后再取帧——明确抛这个，不静默返回黑帧（方便外层定位生命周期 bug）。"""


# ---------------- 基类 ----------------
class FrameSource(object):
    """统一帧来源基类。子类负责填 width/height/fps/src_dur 并实现 frame/close。"""
    width = 0
    height = 0
    fps = 0.0
    src_dur = 0.0

    def frame(self, t):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------- 静态图片 ----------------
def _imread_rgb(path):
    """BGR imread + 中文路径 imdecode 兜底 -> RGB uint8 (H,W,3)。失败抛 ValueError。"""
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        try:
            raw = np.fromfile(path, dtype=np.uint8)
            img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
        except Exception:
            img = None
    if img is None:
        raise ValueError("无法解码图片: %s" % path)
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


class ImageSource(FrameSource):
    """静态图片：解码一次常驻缓存，frame(t) 恒返回同一帧（src_dur=0 表示无限长）。"""

    def __init__(self, path):
        self.path = str(path)
        self._img = _imread_rgb(self.path)
        self.height, self.width = self._img.shape[:2]
        self.fps = 0.0
        self.src_dur = 0.0
        self._closed = False

    def frame(self, t=0.0):
        if self._closed:
            raise SourceClosed("ImageSource 已关闭: %s" % self.path)
        return self._img

    def close(self):
        self._closed = True
        self._img = None


# ---------------- 视频文件 ----------------
class VideoSource(FrameSource):
    """cv2.VideoCapture 封装：顺序读优先 + 近邻 seek 兜底 + LRU 帧缓存。

    取帧策略（_decode）：
      - 目标帧在当前位置**之后 0..SEQ_MAX(16) 帧内** -> 连续 read() 走过去
        （避免关键帧 seek 的解码器重置，顺序小跳远比 seek 快）；
      - 否则（更远的未来帧、或任何回退帧）-> CAP_PROP_POS_FRAMES seek。
    缓存：OrderedDict 键=帧号，上限 64 帧；重复取同帧 0 解码（cache_hits 计数）。
    解码失败（坏帧/越界）返回上一好帧，没有则纯黑。
    """
    SEQ_MAX = 16
    CACHE_MAX = 64

    def __init__(self, path):
        self.path = str(path)
        self._cap = cv2.VideoCapture(self.path)
        if not self._cap.isOpened():
            raise ValueError("无法打开视频: %s" % self.path)
        fps = self._cap.get(cv2.CAP_PROP_FPS)
        if not np.isfinite(fps) or fps <= 0:
            fps = 30.0                       # 属性异常/0 -> 兜底 30
        self.fps = float(fps)
        n = self._cap.get(cv2.CAP_PROP_FRAME_COUNT)
        self.frames = max(0, int(round(n))) if np.isfinite(n) else 0
        self.width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        self.height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if self.width <= 0 or self.height <= 0:
            # 属性缺失：读第一帧拿真实尺寸（该帧顺手进缓存，不多解码一次）
            ok, im = self._cap.read()
            if ok:
                self.height, self.width = im.shape[:2]
                self._pos = 1
                rgb = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
                self._last_good = rgb
            else:
                self.width = self.height = 0
        else:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)   # 归零；_pos=0 表示下次 read 得帧 0
            self._pos = 0
        self.src_dur = (self.frames / self.fps) if self.fps > 0 and self.frames > 0 else 0.0
        self._cache = OrderedDict()          # 帧号 -> RGB uint8
        self._last_good = None
        self._closed = False
        self.stats = {"cache_hits": 0, "decodes": 0, "seeks": 0, "seq_reads": 0}

    def _decode(self, idx):
        """解码帧 idx -> RGB uint8 或 None（失败）。"""
        if idx < self._pos or idx - self._pos > self.SEQ_MAX:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, float(idx))
            self._pos = idx
            self.stats["seeks"] += 1
        else:
            while self._pos < idx:
                if not self._cap.grab():   # cv2 5: grab() 返回 bool；只推进不解码
                    return None
                self._pos += 1
                self.stats["seq_reads"] += 1
        ok, im = self._cap.read()
        self.stats["decodes"] += 1
        if not ok or im is None:
            self._pos = self.frames          # 已到末尾，后续都算越界
            return None
        self._pos += 1
        rgb = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
        return rgb

    def frame(self, t=0.0):
        if self._closed:
            raise SourceClosed("VideoSource 已关闭: %s" % self.path)
        t = float(t)
        if self.src_dur > 0:
            t = min(max(t, 0.0), self.src_dur)
        else:
            t = max(t, 0.0)
        idx = int(round(t * self.fps))
        idx = max(0, min(idx, max(0, self.frames - 1)))
        hit = self._cache.get(idx)
        if hit is not None:
            self._cache.move_to_end(idx)
            self.stats["cache_hits"] += 1
            return hit
        img = self._decode(idx)
        if img is None:
            # 坏帧：回退上一好帧，没有则纯黑；不进缓存（允许下次重试）
            if self._last_good is not None:
                return self._last_good
            h = self.height or 1
            w = self.width or 1
            return np.zeros((h, w, 3), np.uint8)
        self._last_good = img
        self._cache[idx] = img
        if len(self._cache) > self.CACHE_MAX:
            self._cache.popitem(last=False)
        return img

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._cap.release()
            except Exception:
                pass
        self._cache = OrderedDict()
        self._last_good = None


# ---------------- 木偶工程（伪 L2D 实时渲染） ----------------
class PuppetSource(FrameSource):
    """木偶工程目录（含 layers_meta.json）：每帧用 l2d_rig_core 纯 Python 合成。

    加载：C.load_layers(meta, dir) -> (layers, W, H)；C.build_rig(layers, W, H)。
    再读同目录 rig_project.json 的 keyframes（dict，可无）与 anim.duration（默认 4.0）、
    anim.fps（默认 30）。keyframes 塞进 rig["keyframes"]，core 的 _cum_transforms
    会自动走「sin 基线 + 关键帧覆盖」。

    合成方式：**直接复用 core 的现成纯合成函数** C.render_frame(rig, t)
    （规格允许“若 core 已有可复用纯合成函数就直接用”）——它内部即
    _cum_transforms（BFS 累积 3x3）+ 按 rig["draw_order"] 从底到顶 warpAffine
    子矩形 warp + 预乘 src-over 混合 + 黑底展平为 RGB uint8。
    确定性：同 t 两次调用逐像素相等（无随机/时间源/缓存副作用）。
    """

    def __init__(self, project_dir):
        self.path = str(project_dir)
        if V_DIR not in sys.path:
            sys.path.insert(0, V_DIR)
        import l2d_rig_core as C       # 无 UI 核心层，纯 numpy/cv2/PIL/scipy
        self._C = C
        meta = os.path.join(self.path, "layers_meta.json")
        if not os.path.isfile(meta):
            raise ValueError("不是木偶工程目录（缺 layers_meta.json）: %s" % self.path)
        layers, W, H = C.load_layers(meta, self.path)
        self._rig = C.build_rig(layers, W, H)
        self.width, self.height = int(W), int(H)
        self.fps = 30.0
        self.src_dur = 4.0
        self._keyframes = {}
        proj = os.path.join(self.path, "rig_project.json")
        if os.path.isfile(proj):
            try:
                p = json.load(open(proj, encoding="utf8"))
            except Exception:
                p = {}
            if isinstance(p.get("keyframes"), dict):
                self._keyframes = p["keyframes"]
            an = p.get("anim")
            if isinstance(an, dict):
                d = an.get("duration")
                if isinstance(d, (int, float)) and 0.2 <= float(d) <= 600:
                    self.src_dur = float(d)
                f = an.get("fps")
                if isinstance(f, int) and 1 <= f <= 120:
                    self.fps = float(f)
        self._rig["keyframes"] = self._keyframes
        self._closed = False

    def frame(self, t=0.0):
        if self._closed:
            raise SourceClosed("PuppetSource 已关闭: %s" % self.path)
        t = min(max(float(t), 0.0), self.src_dur)
        rgb = self._C.render_frame(self._rig, t)   # 黑底 RGB uint8 (H,W,3)
        return rgb

    def close(self):
        self._closed = True
        self._rig = None
        self._keyframes = None


# ---------------- 统一入口 ----------------
_KIND_FACTORY = {"image": ImageSource, "video": VideoSource, "puppet": PuppetSource}


def open_source(source):
    """source = video_model 的 clip["source"] dict：{"kind","path","w","h","fps","src_dur"}。
    尺寸/帧率一律以实际解码为准（dict 里的只是缓存值）。audio 无画面帧 -> ValueError。"""
    if not isinstance(source, dict):
        raise ValueError("source 必须是 dict")
    kind = source.get("kind")
    if kind == "audio":
        raise ValueError("audio 素材没有视频帧（请用 AudioLoader 取波形/采样）")
    fac = _KIND_FACTORY.get(kind)
    if fac is None:
        raise ValueError("未知素材 kind: %r" % (kind,))
    return fac(source.get("path"))


# ---------------- 音频探测辅助 ----------------
def _find_ffmpeg():
    return next((c for c in FFMPEG_CANDIDATES if os.path.exists(c)), None)


def _ffprobe_candidates():
    """ffprobe.exe 与 ffmpeg.exe 同目录（候选路径同源）。"""
    out = []
    for ff in FFMPEG_CANDIDATES:
        p = os.path.join(os.path.dirname(ff), "ffprobe.exe")
        if os.path.exists(p):
            out.append(p)
    return out


def _run_no_pipe(argv, stdout_file=None, timeout=60):
    """子进程一律不建管道（沙箱/无黑框都稳）：stdout 可重定向到临时文件。
    返回 (returncode 或 None)。"""
    try:
        fh = open(stdout_file, "wb") if stdout_file else subprocess.DEVNULL
    except OSError:
        return None
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL,
                           stdout=fh, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW, timeout=timeout)
        return r.returncode
    except Exception:
        return None
    finally:
        if stdout_file:
            try:
                fh.close()
            except Exception:
                pass


def _ffprobe_duration(path):
    """非 wav 音频时长；所有 ffprobe 候选都失败 -> 0.0。"""
    for probe in _ffprobe_candidates():
        fd, tmp = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            rc = _run_no_pipe([probe, "-v", "error", "-show_entries", "format=duration",
                               "-of", "default=noprint_wrappers=1:nokey=1",
                               str(path)], stdout_file=tmp, timeout=30)
            if rc != 0:
                continue
            txt = open(tmp, encoding="utf8", errors="ignore").read().strip()
            try:
                d = float(txt)
                if np.isfinite(d) and d >= 0:
                    return d
            except ValueError:
                pass
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return 0.0


def _wav_duration(path):
    try:
        with wave.open(path, "rb") as w:
            sr = w.getframerate()
            return (w.getnframes() / float(sr)) if sr > 0 else 0.0
    except Exception:
        return None


def probe_source(path):
    """探测素材 -> source dict（kind 自动判定），字段与 video_model.new_source 对齐。
    目录且含 layers_meta.json -> puppet；视频/图片/音频按扩展名；未知 -> ValueError。"""
    path = str(path)
    if os.path.isdir(path):
        if os.path.isfile(os.path.join(path, "layers_meta.json")):
            s = PuppetSource(path)
            try:
                src = {"kind": "puppet", "path": path, "w": s.width, "h": s.height,
                       "fps": float(s.fps), "src_dur": float(s.src_dur)}
            finally:
                s.close()
            return src
        raise ValueError("目录不是木偶工程（缺 layers_meta.json）: %s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        s = VideoSource(path)
        try:
            return {"kind": "video", "path": path, "w": int(s.width), "h": int(s.height),
                    "fps": float(s.fps), "src_dur": float(s.src_dur)}
        finally:
            s.close()
    if ext in IMAGE_EXTS:
        w = h = 0
        try:
            with Image.open(path) as im:      # PIL 原生支持中文路径
                w, h = im.size
        except Exception:
            rgb = _imread_rgb(path)
            h, w = rgb.shape[:2]
        return {"kind": "image", "path": path, "w": int(w), "h": int(h),
                "fps": 0.0, "src_dur": 0.0}
    if ext in AUDIO_EXTS:
        dur = _wav_duration(path) if ext == ".wav" else None
        if dur is None:
            dur = _ffprobe_duration(path) if ext != ".wav" else 0.0
        return {"kind": "audio", "path": path, "w": 0, "h": 0,
                "fps": 0.0, "src_dur": float(dur)}
    raise ValueError("未知素材类型: %s" % path)


# ---------------- 音频采样 / 波形 ----------------
class AudioLoader(object):
    """音频 -> int16 单声道采样。wav 走标准库 wave（零依赖零进程）；
    其余容器/编码走 ffmpeg.exe 子进程转 16bit mono wav 临时文件再读。
    ffmpeg 不存在或解码失败 -> (None, 0)，**绝不抛**（外层波形显示按空处理）。"""

    @staticmethod
    def _read_wav(path):
        with wave.open(path, "rb") as w:
            sr = int(w.getframerate())
            ch = max(1, int(w.getnchannels()))
            sw = int(w.getsampwidth())
            data = w.readframes(w.getnframes())
        if sr <= 0 or not data:
            return None, 0
        if sw == 2:
            a = np.frombuffer(data, dtype=np.int16)
        elif sw == 1:
            a = (np.frombuffer(data, dtype=np.uint8).astype(np.int32) - 128) * 256
            a = np.clip(a, -32768, 32767).astype(np.int16)
        elif sw == 4:
            a = (np.frombuffer(data, dtype=np.int32) >> 16).astype(np.int16)
        else:
            raise ValueError("不支持的 wav 位宽: %d" % sw)
        if ch > 1:
            n = a.size // ch
            a = a[:n * ch].reshape(n, ch).mean(axis=1)
            a = np.clip(np.rint(a), -32768, 32767).astype(np.int16)
        return np.ascontiguousarray(a, np.int16), sr

    @staticmethod
    def _ffmpeg_wav(path, out_wav):
        ff = _find_ffmpeg()
        if ff is None:
            return False
        rc = _run_no_pipe([ff, "-y", "-i", str(path), "-ac", "1",
                           "-acodec", "pcm_s16le", out_wav], timeout=300)
        return rc == 0 and os.path.isfile(out_wav) and os.path.getsize(out_wav) > 44

    @staticmethod
    def load_samples(path):
        """-> (np.int16 单声道混, sr)；失败 (None, 0) 不崩。"""
        path = str(path)
        if not os.path.isfile(path):
            return None, 0
        if os.path.splitext(path)[1].lower() == ".wav":
            try:
                return AudioLoader._read_wav(path)
            except Exception:
                pass                        # 怪 wav（浮点/非常规头）-> 走 ffmpeg 兜底
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            os.remove(tmp)                  # 让 ffmpeg -y 自己创建，避免句柄占用
            if AudioLoader._ffmpeg_wav(path, tmp):
                try:
                    return AudioLoader._read_wav(tmp)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        return None, 0


def load_samples(path):
    """AudioLoader.load_samples 的模块级别名。"""
    return AudioLoader.load_samples(path)


def waveform(path, buckets=200):
    """0..1 幅度包络（每桶 abs 均值 / 32768）。任何失败 -> []。"""
    try:
        buckets = int(buckets)
    except (TypeError, ValueError):
        return []
    if buckets <= 0:
        return []
    samples, sr = AudioLoader.load_samples(path)
    if samples is None or sr <= 0 or samples.size == 0:
        return []
    a = np.abs(samples).astype(np.float32)
    n = a.size
    edges = np.linspace(0, n, buckets + 1).astype(np.int64)
    out = []
    for i in range(buckets):
        s, e = int(edges[i]), int(edges[i + 1])
        if e <= s:
            e = s + 1
        e = min(e, n)
        out.append(float(min(1.0, a[s:e].mean() / 32768.0)) if e > s else 0.0)
    return out
