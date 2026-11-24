# -*- coding: utf-8 -*-
"""口型匹配（lip sync）的回归测试。

覆盖：
  · 包络分析：静音闭嘴 / 有声张开 / 有起伏不是常数 / 缓存命中
  · 参数映射：不同语种基准、gain 钳位、offset 语义
  · 数据模型：lip 字段 sanitize、绑定表、按 id 取语音、脏值防线
  · 烘焙：时长/帧数/越界钳位
"""
import os
import sys
import tempfile
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


import lip_sync as LS
import video_model as VM

TMP = tempfile.mkdtemp(prefix="muppet_lip_")


def make_wav(path, dur=2.0, sr=32000, talk_ratio=0.66, amp_peak=0.55):
    """造一段「前段说话、后段静音」的测试语音。"""
    n = int(sr * dur)
    t = np.arange(n) / float(sr)
    amp = np.zeros(n)
    talk = int(n * talk_ratio)
    amp[:talk] = np.abs(np.sin(2 * np.pi * 3.0 * t[:talk])) * amp_peak
    sig = (np.sin(2 * np.pi * 230.0 * t) * amp * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(sig.tobytes())
    return path


WAV = make_wav(os.path.join(TMP, "line.wav"))

# ============ 1. 包络 ============
print("=== 1. 包络分析")
env = LS.envelope(WAV, "zh")
chk(env is not None, "算得出包络")
if env:
    print("     %d 点 / %.2fs" % (env.vals.size, env.dur))
    chk(abs(env.dur - 2.0) < 0.05, "时长 ≈2s（%.2f）" % env.dur)

vals = env.bake(30.0, 2.0)
chk(len(vals) == 60, "2s@30fps = 60 帧（%d）" % len(vals))
chk(max(vals) > 0.3, "有声段张开（max=%.2f）" % max(vals))
chk(min(vals[-15:]) < 0.05, "尾部静音闭嘴（min=%.3f）" % min(vals[-15:]))
chk(all(0.0 <= v <= 1.0 for v in vals), "全部落在 0..1")
nuniq = len(set(round(v, 2) for v in vals))
chk(nuniq > 5, "有起伏不是常数（%d 种取值）" % nuniq)

print()
print("=== 2. 静音 -> 闭嘴（门限生效）")
silent = os.path.join(TMP, "silent.wav")
with wave.open(silent, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(32000)
    w.writeframes(np.zeros(32000 * 2, np.int16).tobytes())
env_s = LS.envelope(silent, "zh")
chk(env_s is not None, "静音文件也能算")
if env_s:
    chk(all(v == 0.0 for v in env_s.vals), "全静音 -> 开口度全 0")

print()
print("=== 3. 缓存")
LS.clear_cache()
e1 = LS.envelope(WAV, "zh")
e2 = LS.envelope(WAV, "zh")
chk(e1 is e2, "同一文件同一语种命中缓存（同一对象）")
chk(LS.stats()["cached"] == 1, "缓存只有 1 条")
e3 = LS.envelope(WAV, "ja")
chk(e3 is not e1, "换语种 != 同一对象（基准不同）")

print()
print("=== 4. 语种基准（日文基准更小 -> 同样音量张得更开）")
LS.clear_cache()
vzh = LS.envelope(WAV, "zh").bake(30.0, 2.0)
vja = LS.envelope(WAV, "ja").bake(30.0, 2.0)
mzh = sum(vzh) / len(vzh)
mja = sum(vja) / len(vja)
print("     zh 均值=%.3f  ja 均值=%.3f" % (mzh, mja))
chk(mja > mzh, "同样音频下 ja 开口度更大（基准小）")

print()
print("=== 5. offset 语义")
b1 = env.bake(30.0, 1.0, offset=-1.5)
chk(all(v == 0.0 for v in b1), "offset=-1.5（语音延后）-> 前段全静音")
b2 = env.bake(30.0, 1.0, offset=0.0)
chk(max(b2) > 0.3, "offset=0 -> 有声")
b3 = env.bake(30.0, 1.0, offset=1.9)
chk(all(v == 0.0 for v in b3), "offset 越过音频尾 -> 静音（越界钳位）")

print()
print("=== 6. 异常输入不崩")
chk(LS.envelope("") is None, "空路径 -> None")
chk(LS.envelope(os.path.join(TMP, "nope.wav")) is None, "不存在 -> None")
notwav = os.path.join(TMP, "x.mp3")
open(notwav, "wb").write(b"not a wav")
chk(LS.envelope(notwav) is None, "非 wav -> None 不抛")
chk(LS.open_at(WAV, 0.5, "zh") >= 0.0, "open_at 正常返回")
chk(LS.open_at("", 0.5) == 0.0, "open_at 空路径 -> 0.0")

print()
print("=== 7. 数据模型：lip 字段")
proj = VM.new_project()
proj["dialogue"] = [{"t": 0.0, "who": "高松灯", "text": "测试",
                     "voice": {"path": WAV, "dur": 2.0}}]
src = VM.new_source("puppet", "C:/fake/model.json")
tr = proj["tracks"][0]
c = VM.new_clip(src, 0.0, 3.0)
c["lip"] = {"voice": "vc:0", "gain": 1.0}
tr["clips"].append(c)

chk(VM.lip_bindings(proj) == {c["id"]: {"voice": "vc:0", "gain": 1.0}},
    "lip_bindings 取到绑定")
vc = VM.voice_clip_by_id(proj, "vc:0")
chk(vc is not None and vc.get("path") == WAV, "voice_clip_by_id 找到语音")
chk(VM.voice_clip_by_id(proj, "vc:99") is None, "越界 id -> None")
chk(VM.voice_clip_by_id(proj, None) is None, "None id -> None")

print()
print("=== 8. sanitize 脏值防线")
chk(VM._sanitize_lip(None) is None, "None -> None")
chk(VM._sanitize_lip([]) is None, "list -> None")
chk(VM._sanitize_lip("vc:0") is None, "裸字符串 -> None")
chk(VM._sanitize_lip({"voice": ""}) is None, "空 voice -> None")
chk(VM._sanitize_lip({"voice": "  "}) is None, "空白 voice -> None")
chk(VM._sanitize_lip({"voice": "../../etc/passwd"}) is None,
    "路径注入 -> None（只认 vc:<数字>）")
chk(VM._sanitize_lip({"voice": "vc:abc"}) is None, "vc:非数字 -> None")
chk(VM._sanitize_lip({"voice": "vc:0", "gain": 999})["gain"] == 2.5,
    "gain 上限钳到 2.5")
chk(VM._sanitize_lip({"voice": "vc:0", "gain": -5})["gain"] == 0.3,
    "gain 下限钳到 0.3")
chk(VM._sanitize_lip({"voice": "vc:0", "gain": "abc"})["gain"] == 1.0,
    "脏 gain 回默认 1.0")

print()
print("=== 9. 工程 round-trip")
p2 = VM.sanitize_project(proj)
got = (p2["tracks"][0]["clips"][0] or {}).get("lip")
chk(got == {"voice": "vc:0", "gain": 1.0}, "sanitize 后保留 lip：%s" % got)

# 没有 lip 的 clip 不该凭空多出字段
c2 = VM.new_clip(src, 0.0, 1.0)
tr["clips"].append(c2)
p3 = VM.sanitize_project(proj)
chk("lip" not in (p3["tracks"][0]["clips"][1] or {}),
    "没绑口型的 clip 不带 lip 键")

print()
print("=== 10. 空/无绑定工程")
empty = VM.new_project()
chk(VM.lip_bindings(empty) == {}, "空工程 -> 空绑定表")
chk(VM.lip_bindings(None) == {}, "None 工程 -> 空表不崩")

import shutil
shutil.rmtree(TMP, ignore_errors=True)

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("口型匹配：全部通过")