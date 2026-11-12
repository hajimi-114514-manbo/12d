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


def compose_frame(proj, t, src_get, canvas=None):
    """t 时刻一帧 RGB uint8。src_get(source_dict)->FrameSource（外层做缓存）。
    视频素材缺失/解码失败 -> 黑帧（不中断导出）。"""
    H, W = int(proj["height"]), int(proj["width"])
    if canvas is None:
        canvas = np.zeros((H, W, 3), np.uint8)
    else:
        canvas[...] = 0
    clip = V.top_video_clip_at(proj, t)
    if clip is None:
        return canvas
    try:
        src = src_get(clip["source"])
        img = src.frame(max(0.0, t - clip["start"] + clip["src_in"]))
    except Exception:
        return canvas
    if img is None or img.size == 0:
        return canvas
    if img.ndim == 2:
        img = np.dstack([img] * 3)
    if img.shape[2] == 4:
        img = img[:, :, :3]
    if img.shape[0] != H or img.shape[1] != W:
        img = fit_paste(canvas, img)
        return img
    canvas[...] = img
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
