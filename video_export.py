# -*- coding: utf-8 -*-
"""视频工程导出与逐帧合成（v3.0.0 第一阶段）。

- compose_frame(proj, t, src_get)：按轨道时间线合成一帧 RGB（预览与导出共用，
  保证"所见即所得"是同一条代码路径）。
- VideoExportWorker(QThread)：cv2.VideoWriter 写无声 mp4 -> 有音轨时
  ffmpeg 合流（候选路径与 l2d_rig_qt 一致，但本模块不 import 它，常量复制）。
- 混音：音频轨 clip + 视频 clip 自带音（能探到就混）按 start 摆放相加，
  溢出截断、int16 饱和。

约定：第一阶段预览**无声**（QTimer 逐帧合成画面），声音在导出里体现。
"""
import math
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np

import video_model as V

# ffmpeg 候选（与 l2d_rig_qt.FFMPEG_CANDIDATES 同步；勿 import 那边）
_HERE = os.path.dirname(os.path.abspath(__file__))
FFMPEG_CANDIDATES = [os.path.join(_HERE, "ffmpeg.exe"),
                     r"D:\BaiduNetdiskDownload\Vocal2Midi\ffmpeg.exe"]


def ffmpeg_path():
    return next((p for p in FFMPEG_CANDIDATES if os.path.exists(p)), None)


# ---------------- 单帧合成 ----------------
def source_key(src):
    return (src.get("kind"), os.path.normpath(str(src.get("path") or "")))


def fit_paste(canvas, img):
    """等比缩放居中贴到画布（fit，黑边）——第一阶段唯一适配模式。"""
    H, W = canvas.shape[:2]
    h, w = img.shape[:2]
    if h == 0 or w == 0:
        return canvas
    s = min(W / w, H / h)
    nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    import cv2
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    x, y = (W - nw) // 2, (H - nh) // 2
    canvas[y:y + nh, x:x + nw] = r
    return canvas


def _track_clip_at(tr, t):
    """该轨在 t 时刻生效的 clip（同轨不重叠 => 至多一个）。"""
    for c in tr["clips"]:
        if c["start"] - V.EPS <= t < c["start"] + c["dur"] - V.EPS:
            return c
    return None


def _over_rgba(dst3, rgb, a2f):
    """把 rgb(同 dst3 区域大小) 按逐像素 alpha a2f(float32 HxWx1, 0..1)
    混合进 dst3（就地 uint8）。out = fg*a + dst*(1-a)，四舍五入。"""
    fg = rgb.astype(np.float32)
    out = fg * a2f + dst3.astype(np.float32) * (1.0 - a2f)
    np.add(out, 0.5, out=out)
    dst3[...] = out.astype(np.uint8)


def layer_rect(w, h, W, H, lay):
    """按 layout 计算素材在画布上的外接矩形（StageView 命中/画框与
    paste_layer 共用同一数学，取整一致）。

    - fit 基准 s0=min(W/w,H/h)，实际缩放 = s0*lay["scale"]
    - 中心放 (x*W, y*H)
    - rot==0：精确矩形；rot!=0：旋转后的外接矩形（与 paste_layer 同式）
    返回 (px, py, pw, ph) 画布像素坐标。
    """
    try:
        x, y = lay["x"], lay["y"]
        sc, rot = lay["scale"], lay["rot"]
    except Exception:
        return (0, 0, 0, 0)
    if w <= 0 or h <= 0 or W <= 0 or H <= 0:
        return (0, 0, 0, 0)
    s0 = min(W / w, H / h)
    s = s0 * sc
    nw = max(1, int(round(w * s)))
    nh = max(1, int(round(h * s)))
    cx, cy = x * W, y * H
    if rot == 0.0:
        return (int(round(cx - nw / 2.0)), int(round(cy - nh / 2.0)), nw, nh)
    rad = math.radians(rot)
    ca, sa = abs(math.cos(rad)), abs(math.sin(rad))
    rw = int(math.ceil(nw * ca + nh * sa))
    rh = int(math.ceil(nh * ca + nw * sa))
    return (int(round(cx - rw / 2.0)), int(round(cy - rh / 2.0)), rw, rh)


def paste_layer(canvas, img, lay):
    """按 layout（video_model.layout_at 产物）把素材帧 img 叠进 RGB canvas。

    - 素材 3ch=恒不透明，4ch=带 alpha（out=fg*a+dst*(1-a)）
    - fit 基准 s0=min(W/w,H/h)，实际缩放 = s0*lay["scale"]
    - 中心放 (x*W, y*H)，允许越界（裁剪）
    - rot==0 快路径：resize + 平移贴；rot!=0 才上 warpAffine
    - 默认布局(0.5,0.5,1.0,0.0)+无 alpha 走**旧代码原路径**
      （整幅直贴 / fit_paste），保证旧工程逐像素一致。"""
    import cv2
    H, W = canvas.shape[:2]
    x, y, sc, rot = lay["x"], lay["y"], lay["scale"], lay["rot"]
    # ---- 旧行为精确保持 ----
    is_default = (x == 0.5 and y == 0.5 and sc == 1.0 and rot == 0.0)
    if is_default and img.ndim == 2:
        img = np.dstack([img] * 3)
    if is_default and img.ndim == 3 and img.shape[2] == 3:
        if img.shape[0] == H and img.shape[1] == W:
            canvas[...] = img
        else:
            fit_paste(canvas, img)
        return canvas
    # ---- 通用路径 ----
    if img.ndim == 2:
        img = np.dstack([img] * 3)
    if img.shape[2] >= 4:
        rgb, alpha = img[:, :, :3], img[:, :, 3]
    else:
        rgb, alpha = img[:, :, :3], None
    h, w = rgb.shape[:2]
    if h == 0 or w == 0:
        return canvas
    s0 = min(W / w, H / h)
    s = s0 * sc
    nw = max(1, int(round(w * s)))
    nh = max(1, int(round(h * s)))
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
    rgb = cv2.resize(rgb, (nw, nh), interpolation=interp)
    alpha = cv2.resize(alpha, (nw, nh), interpolation=interp) \
        if alpha is not None else None
    ox, oy, rw, rh = layer_rect(w, h, W, H, lay)
    if rot == 0.0:
        dx0, dy0 = max(0, ox), max(0, oy)
        dx1, dy1 = min(W, ox + rw), min(H, oy + rh)
        if dx1 <= dx0 or dy1 <= dy0:
            return canvas
        sx0, sy0 = dx0 - ox, dy0 - oy
        sub_rgb = rgb[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)]
        dst = canvas[dy0:dy1, dx0:dx1]
        if alpha is None:
            dst[...] = sub_rgb                  # 全不透明：直贴
        else:
            a2 = alpha[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)] \
                    .astype(np.float32)[:, :, None] * (1.0 / 255.0)
            _over_rgba(dst, sub_rgb, a2)
        return canvas
    # rot!=0：绕素材中心旋转到扩充画幅（透明边界），再平移贴
    rad = math.radians(rot)
    ca, sa = abs(math.cos(rad)), abs(math.sin(rad))
    M = cv2.getRotationMatrix2D((nw / 2.0, nh / 2.0), rot, 1.0)
    M[0, 2] += rw / 2.0 - nw / 2.0
    M[1, 2] += rh / 2.0 - nh / 2.0
    rgb = cv2.warpAffine(rgb, M, (rw, rh), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    if alpha is not None:
        # 旋转会把透明边界的 rgb 混进不透明边缘：alpha 同 warp 后必须再
        # 乘回（warp 后的 rgb 边缘已被 0 污染），混合才正确
        a2 = cv2.warpAffine(alpha, M, (rw, rh), flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT,
                            borderValue=0).astype(np.float32)
    else:
        a2 = cv2.warpAffine(np.full((nh, nw), 255, np.uint8), M, (rw, rh),
                            flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT,
                            borderValue=0).astype(np.float32)
    dx0, dy0 = max(0, ox), max(0, oy)
    dx1, dy1 = min(W, ox + rw), min(H, oy + rh)
    if dx1 <= dx0 or dy1 <= dy0:
        return canvas
    sx0, sy0 = dx0 - ox, dy0 - oy
    sl = (slice(sy0, sy0 + (dy1 - dy0)), slice(sx0, sx0 + (dx1 - dx0)))
    a2f = a2[sl][:, :, None] * (1.0 / 255.0)
    _over_rgba(canvas[dy0:dy1, dx0:dx1], rgb[sl], a2f)
    return canvas


def compose_frame(proj, t, src_get, canvas=None):
    """t 时刻一帧 RGB uint8（v3.4.0 起：**自底向上逐可见视频轨 RGBA 叠加**，
    不再只取最上层 clip；上层无 alpha 素材自然覆盖=旧语义，
    带 alpha 素材按 out=fg*a+dst*(1-a) 混合）。
    src_get(source_dict)->FrameSource（外层做缓存）。
    某轨素材缺失/解码失败 -> 跳过该轨继续合成（不中断导出/预览）。"""
    H, W = int(proj["height"]), int(proj["width"])
    if canvas is None:
        canvas = np.zeros((H, W, 3), np.uint8)
    else:
        canvas[...] = 0
    for tr in proj["tracks"]:                   # 列表序 = 底→顶（后加的在上）
        if tr.get("kind") != "video" or tr.get("hidden"):
            continue
        clip = _track_clip_at(tr, t)
        if clip is None:
            continue
        try:
            src = src_get(clip["source"])
            img = src.frame(max(0.0, t - clip["start"] + clip["src_in"]))
        except Exception:
            continue
        if img is None or img.size == 0:
            continue
        paste_layer(canvas, img, V.layout_at(clip))
    return canvas


class SourceManager:
    """source dict -> FrameSource 缓存池；close_all 释放。"""

    def __init__(self):
        self._cache = {}

    def get(self, source):
        k = source_key(source)
        if k not in self._cache:
            import video_source as VS
            self._cache[k] = VS.open_source(source)
        return self._cache[k]

    def close_all(self):
        for s in self._cache.values():
            try:
                s.close()
            except Exception:
                pass
        self._cache.clear()


# ---------------- 混音 ----------------
def _place(buf, samples, at_sample):
    """把 samples 放到 buf[at_sample:]，饱和截断。"""
    n = len(samples)
    i0 = max(0, at_sample)
    if i0 >= len(buf):
        return
    take = min(n, len(buf) - i0)
    buf[i0:i0 + take] = np.clip(buf[i0:i0 + take].astype(np.int64)
                                + samples[:take].astype(np.int64),
                                -32768, 32767).astype(np.int16)


def extract_audio(path):
    """任意媒体 -> (int16 mono, sr)。wav 直读；其余 ffmpeg 转；失败 (None, 0)。"""
    if not path or not os.path.isfile(path):
        return None, 0
    if path.lower().endswith(".wav"):
        try:
            with wave.open(path, "rb") as wf:
                sr = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
            a = np.frombuffer(raw, dtype=np.int16)
            if a.size % max(1, wf.getnchannels()):
                a = a[:-(a.size % wf.getnchannels())]
            ch = max(1, wf.getnchannels())
            mono = a.reshape(-1, ch).mean(axis=1).astype(np.int16) if ch > 1 else a
            return mono, sr
        except Exception:
            return None, 0
    ff = ffmpeg_path()
    if not ff:
        return None, 0
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        subprocess.run(
            [ff, "-y", "-i", path, "-vn", "-ac", "1", "-ar", "44100",
             "-sample_fmt", "s16", tmp],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=0x08000000, timeout=120)
        return extract_audio(tmp)
    except Exception:
        return None, 0
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def mix_audio(proj, sr=44100):
    """整工程混音 -> int16 ndarray（时长=project_duration）。无音频返回 None。"""
    dur = V.project_duration(proj)
    if dur <= 0:
        return None
    buf = np.zeros(int(dur * sr) + sr // 2, np.int16)
    any_a = False
    for tr in proj["tracks"]:
        for c in tr["clips"]:
            kind = c["source"]["kind"]
            if kind in ("puppet", "image"):
                continue
            samples, s2 = extract_audio(c["source"]["path"])
            if samples is None:
                continue          # 素材没声音轨，正常
            if s2 != sr:
                k = sr / s2
                idx = (np.arange(int(len(samples) * k)) / k).astype(np.int32)
                idx = np.clip(idx, 0, len(samples) - 1)
                samples = samples[idx]
            at = int(round((c["start"] - c["src_in"]) * sr))
            if at >= 0:
                _place(buf, samples, at)
            else:                        # 素材头已被 trim 掉
                _place(buf, samples[-at:], 0)
            any_a = True
    return buf if any_a and np.any(buf) else None


def write_wav(path, samples, sr=44100):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())


# ---------------- 导出 Worker ----------------
try:
    from PySide6.QtCore import QThread, Signal

    class VideoExportWorker(QThread):
        """按轨道时间线导出 mp4。progress(pct)，done(ok, msg)。"""
        progress = Signal(int)
        done = Signal(bool, str)

        def __init__(self, proj, out_path, preview_scale=1.0, parent=None):
            super().__init__(parent)
            self.proj = proj
            self.path = out_path
            self.scale = float(preview_scale)
            self._cancel = False

        def cancel(self):
            self._cancel = True

        def run(self):
            import cv2
            sm = SourceManager()
            try:
                W = max(2, int(round(self.proj["width"] * self.scale)) & ~1)
                H = max(2, int(round(self.proj["height"] * self.scale)) & ~1)
                fps = float(self.proj["fps"])
                n = max(1, int(round(V.project_duration(self.proj) * fps)))
                vw = cv2.VideoWriter(self.path, cv2.VideoWriter_fourcc(*"mp4v"),
                                    fps, (W, H))
                if not vw.isOpened():
                    self.done.emit(False, "无法创建视频编码器（cv2 mp4v）")
                    return
                canvas = np.zeros((H, W, 3), np.uint8)
                for i in range(n):
                    if self._cancel:
                        vw.release()
                        self.done.emit(False, "已取消")
                        return
                    t = i / fps
                    frame = compose_frame(self.proj, t, sm.get)
                    if frame.shape[0] != H or frame.shape[1] != W:
                        frame = cv2.resize(frame, (W, H), interpolation=cv2.INTER_AREA)
                    vw.write(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
                    if i % max(1, n // 50) == 0:
                        self.progress.emit(int(i / n * 80))
                vw.release()
                self.progress.emit(80)
                # 混音合流
                audio = mix_audio(self.proj) if self.scale >= 1.0 else None
                if audio is None or not np.any(audio):
                    self.done.emit(True, self.path)
                    return
                ff = ffmpeg_path()
                if not ff:
                    self.done.emit(True, self.path + "（未找到 ffmpeg，导出为无声）")
                    return
                wavp = tempfile.mktemp(suffix=".wav")
                write_wav(wavp, audio)
                finalp = tempfile.mktemp(suffix=".mp4")
                r = subprocess.run(
                    [ff, "-y", "-i", self.path, "-i", wavp,
                     "-c:v", "copy", "-c:a", "aac", "-shortest", finalp],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, creationflags=0x08000000, timeout=600)
                self.progress.emit(98)
                if r.returncode == 0 and os.path.isfile(finalp):
                    os.replace(finalp, self.path)
                    msg = "已导出（含音轨）"
                else:
                    msg = "音轨合流失败，输出无声视频"
                for p in (wavp, finalp):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                self.done.emit(True, "%s %s" % (self.path, msg))
            except Exception as e:
                self.done.emit(False, "导出异常: %r" % e)
            finally:
                sm.close_all()
except ImportError:      # 无 Qt 环境（纯单测）也能 import 本模块
    VideoExportWorker = None
