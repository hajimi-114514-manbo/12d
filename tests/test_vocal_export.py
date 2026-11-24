# -*- coding: utf-8 -*-
"""语音导出到 WebGAL 的回归测试（需求池【语音V4】）。

覆盖：
  · 有语音的台词前 emit `vocal:文件名;`
  · wav 真的拷进 game/vocal/
  · 没语音的台词照常输出，不加 vocal
  · 语音文件缺失 -> 跳过 + 记警告，不阻断导出
  · 脱离台词（detach）的语音按自己的时刻发，不贴在原台词前
  · 重名去重（两个不同目录的同名 wav）
  · 预览路径（_render_scene_lines）与落盘路径同规格
"""
import io
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


import video_model as VM
import webgal_export as WE

TMP = tempfile.mkdtemp(prefix="muppet_vocal_test_")


def make_wav(path, dur=1.0, sr=32000, tone=220.0):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    n = int(sr * dur)
    t = np.arange(n) / float(sr)
    sig = (np.sin(2 * np.pi * tone * t) * 0.3 * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(sig.tobytes())


def build_proj(proj_dir, dialogue):
    proj = VM.new_project()
    proj["dialogue"] = dialogue
    # 一个满幅背景，避免空工程走异常分支
    from PIL import Image
    if not os.path.isdir(proj_dir):
        os.makedirs(proj_dir, exist_ok=True)
    bg = os.path.join(proj_dir, "_bg.png")
    Image.new("RGB", (64, 36), (20, 20, 20)).save(bg)
    proj["tracks"].insert(0, {
        "id": "bg1", "kind": "video", "name": "BG", "hidden": False,
        "locked": False,
        "clips": [dict(VM.new_clip(VM.new_source("image", bg), 0.0, 20.0),
                       layout=dict(VM.LAYOUT_DEFAULT))]})
    return proj


def run_export(proj, proj_dir, tag):
    out = os.path.join(TMP, "out_" + tag)
    res = WE.export_game(proj, out, proj_dir=proj_dir)
    start = None
    vdir = None
    for root, dirs, files in os.walk(out):
        if "start.txt" in files:
            start = os.path.join(root, "start.txt")
        if os.path.basename(root) == "vocal":
            vdir = root
    body = io.open(start, encoding="utf-8").read() if start else ""
    stats = {}
    if isinstance(res, tuple):
        ok = res[0]
        for x in res:
            if isinstance(x, dict):
                stats = x
    else:
        ok = bool(res)
    return body, vdir, stats, ok


# ============ 1. 基本：有语音 / 没语音 / 缺失 ============
print("=== 1. 基本三态")
P1 = os.path.join(TMP, "p1")
make_wav(os.path.join(P1, "vocal", "有.wav"), 1.0, tone=220)
make_wav(os.path.join(P1, "vocal", "有2.wav"), 1.0, tone=330)
proj = build_proj(P1, [
    {"t": 0.0, "who": "灯", "text": "第一句",
     "voice": {"path": "vocal/有.wav", "dur": 1.0}},
    {"t": 2.0, "who": "灯", "text": "没有语音"},
    {"t": 4.0, "who": "灯", "text": "语音丢了",
     "voice": {"path": "vocal/缺.wav", "dur": 1.0}},
    {"t": 6.0, "who": "灯", "text": "第二句有语音",
     "voice": {"path": "vocal/有2.wav", "dur": 1.0}},
])
body, vdir, stats, ok = run_export(proj, P1, "basic")
chk(ok, "导出成功")
chk(body.count("vocal:") == 2, "两条 vocal（%d）" % body.count("vocal:"))
chk("vocal:有.wav;" in body, "第一条文件名对")
chk("vocal:有2.wav;" in body, "第二条文件名对")
chk("没有语音" in body, "没语音的台词照常输出")
chk("语音丢了" in body, "语音缺失的台词仍然输出")
chk("vocal:缺.wav" not in body, "缺失的语音没写进脚本")
chk(bool(vdir), "vocal/ 目录存在")
if vdir:
    wavs = sorted(f for f in os.listdir(vdir) if f.lower().endswith(".wav"))
    chk(len(wavs) == 2, "拷进 2 个 wav（%s）" % wavs)
warns = stats.get("warnings") or []
chk(any("语音缺失" in str(w) for w in warns),
    "记了「语音缺失」警告")

# ============ 2. vocal 在台词之前 ============
print()
print("=== 2. vocal 排在台词行之前")
lines = [l for l in body.split("\n") if l.strip()]
pairs = 0
for i, l in enumerate(lines):
    if l.startswith("vocal:") and i + 1 < len(lines):
        nxt = lines[i + 1]
        if ":" in nxt and not nxt.startswith(("vocal:", "bgm:", "playEffect:")):
            pairs += 1
chk(pairs == 2, "两条 vocal 都紧跟着台词行（%d）" % pairs)

# ============ 3. 脱离台词：按自己的时刻发 ============
print()
print("=== 3. detach：语音按自己的 t 发，不贴原台词")
P2 = os.path.join(TMP, "p2")
make_wav(os.path.join(P2, "vocal", "游离.wav"), 1.0)
proj2 = build_proj(P2, [
    {"t": 0.0, "who": "灯", "text": "台词在0秒",
     "voice": {"path": "vocal/游离.wav", "dur": 1.0,
               "detach": True, "t": 5.0}},
    {"t": 8.0, "who": "灯", "text": "台词在8秒"},
])
body2, vdir2, stats2, ok2 = run_export(proj2, P2, "detach")
chk(ok2, "导出成功")
chk("vocal:游离.wav;" in body2, "脱离的语音仍然被导出")
# 校验位置：vocal 行应出现在「台词在0秒」之后、「台词在8秒」之前
lines2 = [l for l in body2.split("\n") if l.strip()]
iv = [i for i, l in enumerate(lines2) if "vocal:游离" in l]
i0 = [i for i, l in enumerate(lines2) if "台词在0秒" in l]
i8 = [i for i, l in enumerate(lines2) if "台词在8秒" in l]
print("     索引 vocal=%s  t0=%s  t8=%s" % (iv, i0, i8))
if iv and i0 and i8:
    chk(iv[0] > i0[0], "vocal 不在 0 秒台词前（脱离后跟着自己的 t=5）")
    chk(iv[0] < i8[0], "vocal 在 8 秒台词之前（5 < 8）")

# ============ 4. 重名去重 ============
print()
print("=== 4. 不同目录同名 wav 去重")
P3 = os.path.join(TMP, "p3")
make_wav(os.path.join(P3, "vocal", "a", "同名.wav"), 1.0, tone=200)
make_wav(os.path.join(P3, "vocal", "b", "同名.wav"), 1.0, tone=400)
proj3 = build_proj(P3, [
    {"t": 0.0, "who": "灯", "text": "甲",
     "voice": {"path": "vocal/a/同名.wav", "dur": 1.0}},
    {"t": 2.0, "who": "灯", "text": "乙",
     "voice": {"path": "vocal/b/同名.wav", "dur": 1.0}},
])
body3, vdir3, stats3, ok3 = run_export(proj3, P3, "dup")
chk(ok3, "导出成功")
chk(body3.count("vocal:") == 2, "两条 vocal（%d）" % body3.count("vocal:"))
if vdir3:
    wavs3 = sorted(f for f in os.listdir(vdir3) if f.lower().endswith(".wav"))
    print("     vocal/ 里:", wavs3)
    chk(len(wavs3) == 2, "两个都拷进来了，没互相覆盖（%d）" % len(wavs3))
    chk(len(set(wavs3)) == 2, "文件名不同（去重生效）")

# ============ 5. 预览路径同规格 ============
print()
print("=== 5. 预览路径（_render_scene_lines）也带 vocal")
try:
    lines5, pseudo5, warns5 = WE._render_scene_lines(
        {"dialogue": [
            {"t": 0.0, "who": "灯", "text": "预览句",
             "voice": {"path": "vocal/有.wav", "dur": 1.0}},
            {"t": 2.0, "who": "灯", "text": "没语音"},
        ]}, P1, "start.txt", {}, None)
    joined = "\n".join(lines5)
    print("     生成的台词段:", [l for l in lines5 if l.strip()][:4])
    chk("vocal:有.wav;" in joined, "预览路径也 emit vocal")
    chk("预览句" in joined, "预览路径保留台词")
    chk("没语音" in joined, "没语音的台词照常")
except Exception as e:
    print("     调用失败:", type(e).__name__, e)
    chk(False, "预览路径调用成功")

# ============ 6. 全部没语音 -> 一行 vocal 都不该有 ============
print()
print("=== 6. 全工程没语音")
P4 = os.path.join(TMP, "p4")
proj4 = build_proj(P4, [
    {"t": 0.0, "who": "灯", "text": "甲"},
    {"t": 2.0, "who": "灯", "text": "乙"},
])
body4, vdir4, stats4, ok4 = run_export(proj4, P4, "novoice")
chk(ok4, "导出成功")
chk("vocal:" not in body4, "没有 vocal 指令")
chk("甲" in body4 and "乙" in body4, "台词都在")
chk(not (stats4.get("warnings") and
         any("语音" in str(w) for w in stats4["warnings"])),
    "没有多余的语音警告")

import shutil
shutil.rmtree(TMP, ignore_errors=True)

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("语音导出：全部通过")