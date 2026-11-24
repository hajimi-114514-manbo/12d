# -*- coding: utf-8 -*-
"""口型匹配（lip sync）：从语音包络驱动 Live2D 的嘴部参数。

## 为什么自己做，不用运行时那套

WebGAL 运行时里确实有一套口型（`setModelMouthY` / `tA`），但它有
两处不能直接用（**已查证** `templates/webgal_runtime/assets/
index-Dcp3ZA1M.js`）：

1. **靠 `AnalyserNode` 实时抓 `<audio>` 的频谱**
   （`Rt.analyser.fftSize = 256` + `getByteFrequencyData`），
   依赖「真的在播音」。导出时是**离线逐帧合成**、根本没有播放，
   拿不到频谱。
2. **阈值是「本次播放的峰值」相对量**
   （`OPEN_THRESHOLD = maxAudioLevel * .75`），要先播一遍才知道
   峰在哪 —— 结果不确定（同一份音频跑两次可能不一样）。

导出要的是**确定性**：同一个 t 必须永远画出同一张嘴。所以这里
在 Python 侧离线算包络，把 0~1 的开口度直接下发，两边都不碰音频。

## 三个关键决定

### 1. 不直接用「线性幅度」

`abs` 均值当开口度，嘴会抖得厉害、而且看着不像在说话。真实口型
要过一遍**非线性**：

    open = (rms / ref) ** 0.6

指数 0.6 是把它「抬起来」：小音量也能张开一点，大音量不至于一直
顶格。这个值来自实测对比（1.0 太僵、0.4 长期半张）。

### 2. 先做「音节级平滑」再采样

原始包络每 10ms 一个值，嘴会碎抖。做法是滑动平均（约 50ms 窗），
再按帧率重采样。**只平滑不削峰**：削峰会把「啊」和「一」抹平，
嘴就成了匀速开合。

### 3. 中文 / 日文给不同增益

中文口型幅度天生比日文大（元音更开），同一条曲线套两个语种，
日文会显得「嘴不动」、中文会「张太大」。所以按语种给基准：

    zh: ref = 0.30    ja: ref = 0.22

`ref` 是「正常说话音量的 rms」——超过它开始顶格。

## 数据流

    voice_clip.path ──> envelope()  ──> 0..1 序列（每 10ms）
                             │
                             ├─> sample(env, t)   单点查（预览每帧）
                             └─> bake(env, fps, n) 整段烘焙（导出）

预览走 `sample()`（跟手，随时能改），导出走 `bake()`（一次算完，
避免每帧重算）。**两条路必须同一套参数**，否则预览和成片不一致。
"""
import os
import struct
import threading
import wave

import numpy as np

# ---------------- 可调参数（改这里就能调手感） ----------------
# 数值来自**实测校准**，不是拍脑袋：拿语料库 40 段真实语音（8457 个
# 10ms 窗口）算 rms 分布 -> p50=0.143 / p85=0.205 / p95=0.236 / p99=0.263。
# 以 ref=0.30 代入，开口度落在 p50≈0.67 / p85≈0.81 / p99≈0.93，
# **顶格 0%** —— 既不长期大张，也不纹丝不动（见 test_lip_sync 的分布断言）。
HOP_MS = 10             # 包络采样间隔（毫秒）
SMOOTH_MS = 50          # 音节级平滑窗（毫秒）；太小会抖，太大会糊
EXP = 0.6               # 非线性指数：把包络抬起来（1.0 太僵、0.4 长期半张）
GAIN = {"zh": 1.00, "ja": 0.96, "en": 0.98, "": 1.00}
# ref = 「正常说话音量的 rms」。中文元音更开、rms 更高，所以基准略大；
# 日文反过来。调大 -> 嘴张得小，调小 -> 容易顶格。
REF = {"zh": 0.30, "ja": 0.27, "en": 0.29, "": 0.30}
FLOOR = 0.02            # 静音门限：低于它就闭嘴（不想让呼吸声动嘴）
CEIL = 1.0

_CACHE_LOCK = threading.Lock()
_CACHE = {}             # path+mtime+size -> _Env


class _Env(object):
    """一段语音的包络（不可变，可缓存共享）。"""

    __slots__ = ("hop_s", "vals", "dur")

    def __init__(self, hop_s, vals, dur):
        self.hop_s = float(hop_s)
        self.vals = vals                    # numpy float32, 0..1
        self.dur = float(dur)

    def at(self, t):
        """t 秒处的开口度（0..1）。越界钳位，绝不抛。"""
        try:
            tf = float(t)
        except (TypeError, ValueError):
            return 0.0
        if tf < 0 or not self.vals.size:
            return 0.0
        if tf > self.dur:
            return 0.0
        i = int(round(tf / self.hop_s))
        if i < 0:
            return 0.0
        if i >= self.vals.size:
            i = self.vals.size - 1
        return float(self.vals[i])

    def bake(self, fps, dur, offset=0.0):
        """按帧率烘焙整段：[open0, open1, ...]（长度 = dur*fps 向上取整）。

        `offset` = **片段第 0 帧对应音频里的哪一刻**（秒）。
          · 立绘 0s 入场、语音 2s 才开始  -> offset = -2（前 2 秒静音）
          · 立绘 2s 入场、语音 0s 就开始  -> offset = +2（跳过音频前 2 秒）
        越界由 _Env.at() 钳成 0/末点，调用方不用自己判边界。
        """
        try:
            f = float(fps)
        except (TypeError, ValueError):
            f = 30.0
        if f <= 0:
            f = 30.0
        n = int(np.ceil(max(0.0, float(dur)) * f))
        out = []
        for k in range(n):
            out.append(self.at(offset + k / f))
        return out


def _read_mono(path):
    """读 wav -> (float32 单声道 -1..1, sr)。读不了返回 (None, 0)。

    只认 wav。mp3/ogg 等要靠 ffmpeg，但语音轨本来就是 wav
    （GPT-SoVITS 输出 32kHz 单声道 wav），不为它引入转码依赖。
    """
    try:
        with wave.open(str(path), "rb") as w:
            sr = int(w.getframerate())
            ch = max(1, int(w.getnchannels()))
            sw = int(w.getsampwidth())
            raw = w.readframes(w.getnframes())
    except Exception:
        return None, 0
    if sr <= 0 or not raw:
        return None, 0
    try:
        if sw == 2:
            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        elif sw == 1:
            a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0)
            a *= 256.0
        elif sw == 4:
            a = np.frombuffer(raw, dtype=np.int32).astype(np.float32)
            a /= 65536.0
        elif sw == 3:                       # 24 位：手动解
            b = np.frombuffer(raw, dtype=np.uint8)
            n = (b.size // 3) * 3
            b = b[:n].reshape(-1, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            v = np.where(v & 0x800000, v - 0x1000000, v)
            a = v.astype(np.float32) / 8388608.0
            raw = None
        else:
            return None, 0
        if raw is not None:
            a = a / 32768.0
    except Exception:
        return None, 0
    if ch > 1:
        n = a.size // ch
        a = a[:n * ch].reshape(n, ch).mean(axis=1)
    return np.ascontiguousarray(a, dtype=np.float32), sr


def _lang_key(lang):
    s = str(lang or "").strip().lower()
    if s.startswith("ja") or s in ("jp", "japanese", "日文"):
        return "ja"
    if s.startswith("zh") or s in ("cn", "chinese", "中文"):
        return "zh"
    if s.startswith("en"):
        return "en"
    return ""


def envelope(path, lang=""):
    """给一段 wav 算口型包络（带缓存）。失败返回 None，绝不抛。

    返回 _Env：`.at(t)` 单点查、`.bake(fps, dur)` 整段烘焙。
    """
    p = str(path or "")
    if not p or not os.path.isfile(p):
        return None
    try:
        st = os.stat(p)
        key = (os.path.abspath(p), int(st.st_mtime), int(st.st_size),
               _lang_key(lang))
    except OSError:
        return None
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit

    a, sr = _read_mono(p)
    if a is None or sr <= 0 or a.size == 0:
        return None
    env = _build(a, sr, lang)

    with _CACHE_LOCK:
        # 顺手清掉同一文件的旧版本（改完语音重算）
        for k in [k for k in _CACHE if k[0] == key[0] and k != key]:
            _CACHE.pop(k, None)
        _CACHE[key] = env
    return env


def _build(a, sr, lang):
    """幅度 -> rms -> 平滑 -> 归一到 0..1。"""
    lk = _lang_key(lang)
    ref = float(REF.get(lk, REF[""]))
    gain = float(GAIN.get(lk, GAIN[""]))
    hop = max(1, int(round(sr * HOP_MS / 1000.0)))

    n = a.size
    nh = max(1, n // hop)
    # 逐跳 rms（分块求平方均值，比循环快得多）
    core = a[:nh * hop].reshape(nh, hop)
    rms = np.sqrt(np.mean(core * core, axis=1) + 1e-12).astype(np.float32)

    # 音节级平滑：滑动平均（窗按 hop 数换算）
    w = max(1, int(round(SMOOTH_MS / float(HOP_MS))))
    if w > 1 and rms.size > w:
        # 用 cumulative sum 做 O(n)
        c = np.cumsum(np.concatenate(([0.0], rms))) 
        half = w // 2
        idx = np.arange(rms.size)
        lo = np.clip(idx - half, 0, rms.size)
        hi = np.clip(idx + half + 1, 0, rms.size)
        rms = ((c[hi] - c[lo]) / np.maximum(1, hi - lo)).astype(np.float32)

    # 非线性抬升 + 门限
    v = np.power(np.clip(rms / max(1e-6, ref), 0.0, 4.0), EXP) * gain
    v = np.where(rms < FLOOR, 0.0, np.clip(v, 0.0, CEIL)).astype(np.float32)

    dur = n / float(sr)
    return _Env(HOP_MS / 1000.0, v, dur)


def open_at(path, t, lang="", clip_start=0.0, src_in=0.0):
    """时间轴 t 时刻的开口度（自动换算到音频内时刻）。

    clip_start 是语音 clip 在工程里的起点，src_in 是音频内的裁剪偏移。
    语音被拖到别处、被裁过头，都要对上正确的音频位置。
    """
    env = envelope(path, lang)
    if env is None:
        return 0.0
    return env.at(float(t) - float(clip_start) + float(src_in))


def bake_clip(voice_clip, lang="", fps=30):
    """给一个语音 clip 烘焙整段开口度（导出用）。

    voice_clip 就是 video_model.voice_clips() 里那种 dict：
      {t, dur, path, role, ...}
    返回 list[float]，长度 = dur*fps 向上取整；素材不可用返回 []。
    """
    if not isinstance(voice_clip, dict):
        return []
    env = envelope(voice_clip.get("path"), lang)
    if env is None:
        return []
    return env.bake(fps, voice_clip.get("dur") or 0.0, offset=0.0)


def stats():
    """缓存概况（测试/诊断用）。"""
    with _CACHE_LOCK:
        items = list(_CACHE.values())
    return {"cached": len(items),
            "slots": int(sum(e.vals.size for e in items)),
            "dur": round(sum(e.dur for e in items), 2)}


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()