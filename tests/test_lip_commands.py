# -*- coding: utf-8 -*-
"""口型联动命令的回归测试（lip.status / list / bind / unbind）。

覆盖：
  · status：立绘/语音/已绑定计数，Live2D 与非 Live2D 的可用性提示
  · list：已绑定/未绑定分离、语音候选、语音被删后的 broken 标记
  · bind：正常绑定、voice_index、auto_pick、越界、非立绘、语音文件缺失
  · unbind：单个解除、整幕清空、本来就没绑
  · 脏值：voice_id 形态、gain 钳位
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


import video_model as VM
import lip_commands as LC
import remote_control as RC

TMP = tempfile.mkdtemp(prefix="muppet_lipcmd_")


def make_wav(path, dur=1.0, sr=16000):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    n = int(sr * dur)
    t = np.arange(n) / float(sr)
    sig = (np.sin(2 * np.pi * 200 * t) * 0.3 * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(sig.tobytes())


def wav(name, dur=1.0):
    p = os.path.join(TMP, "vocal", name)
    make_wav(p, dur)
    return p


class FakeWB(object):
    """最小 wb 桩：只要 proj / _t / _cur_aid / render_frame。"""

    def __init__(self, proj, t=0.0, aid=None):
        self.proj = proj
        self._t = t
        self._cur_aid = aid
        self.rendered = 0

    def render_frame(self, t):
        self.rendered += 1


def mkproj(voices=None, figs=None, live2d=True):
    """造工程：voices=[(t, who, text, wavpath, dur)]，figs=[(start,dur,track)]。"""
    p = VM.new_project()
    p["dialogue"] = [
        {"t": t, "who": who, "text": txt,
         "voice": {"path": vp, "dur": dur}}
        for (t, who, txt, vp, dur) in (voices or [])
    ]
    if figs is None:
        figs = [(0.0, 5.0, "FIG-灯")]
    for (start, dur, tr_name) in figs:
        tr = None
        for t in p["tracks"]:
            if t.get("name") == tr_name:
                tr = t
                break
        if tr is None:
            tr = {"id": "t_%s" % tr_name, "kind": "video", "name": tr_name,
                  "hidden": False, "locked": False, "clips": []}
            p["tracks"].append(tr)
        src = VM.new_source(
            "puppet",
            os.path.join(TMP, "live2d" if live2d else "png",
                         "model.json" if live2d else "a.png"))
        c = VM.new_clip(src, start, dur)
        tr["clips"].append(c)
    return p


# ============ 1. status ============
print("=== 1. lip.status")
w1 = wav("a.wav", 1.0)
w2 = wav("b.wav", 1.2)
proj = mkproj(voices=[(0.0, "灯", "第一句", w1, 1.0),
                      (2.0, "灯", "第二句", w2, 1.2)])
wb = FakeWB(proj)
ok, msg, data = RC.run_command(wb, "lip.status", {})
chk(ok, "status ok：%s" % msg)
chk(data["figures"] == 1, "1 个立绘（%d）" % data["figures"])
chk(data["voices"] == 2, "2 句语音（%d）" % data["voices"])
chk(data["figures_live2d"] == 1, "识别为 Live2D")
chk(data["bound"] == 0, "初始 0 个绑定")
chk(data["usable"] is True, "可用")

# 非 Live2D 工程
proj_png = mkproj(voices=[(0.0, "灯", "x", w1, 1.0)], live2d=False)
ok, msg, d2 = RC.run_command(FakeWB(proj_png), "lip.status", {})
chk(ok and d2["figures_live2d"] == 0, "PNG 立绘不算 Live2D")
chk("Live2D" in d2.get("note", ""), "提示只对 Live2D 有效：%s"
    % d2.get("note", "")[:40])

# 没语音
proj_nv = mkproj(voices=[])
ok, msg, d3 = RC.run_command(FakeWB(proj_nv), "lip.status", {})
chk(d3["voices"] == 0 and d3["usable"] is False, "没语音 -> 不可用")
chk("语音" in d3.get("note", ""), "提示缺语音")

# ============ 2. list ============
print()
print("=== 2. lip.list")
ok, msg, data = RC.run_command(wb, "lip.list", {})
chk(ok, "list ok：%s" % msg)
chk(len(data["unbound"]) == 1, "1 个未绑定")
chk(len(data["bound"]) == 0, "0 个已绑定")
chk(len(data["voices"]) == 2, "2 句语音候选")
vid0 = data["voices"][0]["voice_id"]
chk(str(vid0).startswith("vc:"), "语音 id 形态 vc:N（%s）" % vid0)
chk("text" in data["voices"][0], "语音摘要带 text")

# ============ 3. bind 正常 ============
print()
print("=== 3. lip.bind")
cid = data["unbound"][0]["clip_id"]
ok, msg, data = RC.run_command(wb, "lip.bind",
                               {"clip_id": cid, "voice_id": vid0})
chk(ok, "绑定成功：%s" % msg)
chk(proj["tracks"][-1]["clips"][0].get("lip") == {"voice": vid0, "gain": 1.0},
    "写进了 clip[\"lip\"]")
chk(wb.rendered > 0, "绑定后触发了预览刷新（%d 次）" % wb.rendered)

# 再 list 应看到已绑定
ok, msg, data2 = RC.run_command(wb, "lip.list", {})
chk(len(data2["bound"]) == 1, "list 里 1 个已绑定")
chk(data2["bound"][0]["voice"]["voice_id"] == vid0, "绑定指向正确语音")
chk(data2["bound"][0].get("gain") == 1.0, "gain 报出来")

# ============ 4. bind 带 gain ============
print()
print("=== 4. bind 带 gain")
ok, msg, data = RC.run_command(wb, "lip.bind",
                               {"clip_id": cid, "voice_id": vid0, "gain": 2.0})
chk(ok, "带 gain 绑定成功")
chk(proj["tracks"][-1]["clips"][0]["lip"]["gain"] == 2.0, "gain 写入 2.0")
ok, msg, _ = RC.run_command(wb, "lip.bind",
                            {"clip_id": cid, "voice_id": vid0, "gain": 99})
chk(proj["tracks"][-1]["clips"][0]["lip"]["gain"] == 2.5, "gain 超限钳到 2.5")
ok, msg, _ = RC.run_command(wb, "lip.bind",
                            {"clip_id": cid, "voice_id": vid0, "gain": -3})
chk(proj["tracks"][-1]["clips"][0]["lip"]["gain"] == 0.3, "gain 下限钳到 0.3")

# ============ 5. bind 用 voice_index ============
print()
print("=== 5. voice_index 绑定")
ok, msg, data = RC.run_command(wb, "lip.bind", {"clip_id": cid, "voice_index": 1})
chk(ok, "用 voice_index 绑定成功：%s" % msg)
chk(proj["tracks"][-1]["clips"][0]["lip"]["voice"] == "vc:1",
    "绑到了第二句（vc:1）")
ok, msg, _ = RC.run_command(wb, "lip.bind", {"clip_id": cid, "voice_index": 99})
chk(not ok, "voice_index 越界 -> 拒绝")

# ============ 6. bind 各种错误 ============
print()
print("=== 6. bind 错误分支")
ok, msg, _ = RC.run_command(wb, "lip.bind", {})
chk(not ok and "clip_id" in msg, "缺 clip_id -> 拒绝")
ok, msg, _ = RC.run_command(wb, "lip.bind", {"clip_id": "不存在的id"})
chk(not ok and "找不到" in msg, "clip 不存在 -> 拒绝")
ok, msg, _ = RC.run_command(wb, "lip.bind",
                            {"clip_id": cid, "voice_id": "vc:99"})
chk(not ok and "找不到语音" in msg, "语音不存在 -> 拒绝")
ok, msg, _ = RC.run_command(wb, "lip.bind",
                            {"clip_id": cid, "voice_id": "../../etc"})
chk(not ok, "非法 voice_id -> 拒绝")
# 背景 clip 不能绑：造一个带背景的工程（默认工程是空轨，没有 clip）
proj_bg = mkproj(voices=[(0.0, "灯", "x", w1, 1.0)])
from PIL import Image as _Img
_bg = os.path.join(TMP, "bg.png")
_Img.new("RGB", (32, 18), (5, 5, 5)).save(_bg)
_bgc = VM.new_clip(VM.new_source("image", _bg), 0.0, 5.0)
proj_bg["tracks"][0]["clips"].append(_bgc)      # tracks[0] = 视频轨 1（BG）
ok, msg, _ = RC.run_command(FakeWB(proj_bg), "lip.bind",
                            {"clip_id": _bgc["id"], "voice_id": vid0})
chk(not ok, "背景 clip 不能绑口型（%s）" % msg[:26])

# 语音文件被删 -> 拒绝绑定
w3 = wav("gone.wav", 1.0)
proj2 = mkproj(voices=[(0.0, "灯", "会消失的", w3, 1.0)])
wb2 = FakeWB(proj2)
os.remove(w3)
cid2 = wb2.proj["tracks"][-1]["clips"][0]["id"]
ok, msg, _ = RC.run_command(wb2, "lip.bind",
                            {"clip_id": cid2, "voice_id": "vc:0"})
chk(not ok and "不存在" in msg, "语音文件缺失 -> 拒绝绑定")

# ============ 7. auto_pick ============
print()
print("=== 7. auto_pick（显式开启才生效）")
w4 = wav("p1.wav", 1.0)
w5 = wav("p2.wav", 1.0)
proj3 = mkproj(voices=[(0.0, "灯", "重叠的", w4, 1.0),
                       (10.0, "灯", "不重叠", w5, 1.0)],
               figs=[(0.0, 3.0, "FIG-灯")])
wb3 = FakeWB(proj3)
cid3 = proj3["tracks"][-1]["clips"][0]["id"]
ok, msg, data = RC.run_command(wb3, "lip.bind",
                               {"clip_id": cid3, "auto_pick": True})
chk(ok, "auto_pick 成功：%s" % msg)
chk(proj3["tracks"][-1]["clips"][0]["lip"]["voice"] == "vc:0",
    "挑中了时间重叠的第一句（vc:0）")

# ============ 8. unbind ============
print()
print("=== 8. lip.unbind")
ok, msg, data = RC.run_command(wb, "lip.unbind", {"clip_id": cid})
chk(ok, "单个解除成功：%s" % msg)
chk(proj["tracks"][-1]["clips"][0].get("lip") is None, "lip 字段已删")
ok, msg, data = RC.run_command(wb, "lip.unbind", {"clip_id": cid})
chk(ok and data["cleared"] == 0, "本来就没绑 -> ok 但 cleared=0")
ok, msg, _ = RC.run_command(wb, "lip.unbind", {"clip_id": "不存在"})
chk(not ok and "找不到" in msg, "clip 不存在 -> 拒绝")

# 整幕清空
ok, msg, data = RC.run_command(wb, "lip.bind",
                               {"clip_id": cid, "voice_id": "vc:0"})
chk(ok, "先绑一个")
ok, msg, data = RC.run_command(wb, "lip.unbind", {})
chk(ok and data["cleared"] >= 1, "整幕清空（%d 个）" % data.get("cleared", 0))
chk(proj["tracks"][-1]["clips"][0].get("lip") is None, "清空后无 lip")

# ============ 9. 未知命令 / 异常 ============
print()
print("=== 9. 未知命令")
ok, msg, _ = RC.run_command(wb, "lip.nope", {})
chk(not ok and "未知" in msg, "未知命令不崩：%s" % msg)

# ============ 10. broken 绑定（语音被删） ============
print()
print("=== 10. 绑定的语音后来被删")
proj4 = mkproj(voices=[(0.0, "灯", "会没", wav("tmp.wav"), 1.0)])
wb4 = FakeWB(proj4)
cid4 = proj4["tracks"][-1]["clips"][0]["id"]
RC.run_command(wb4, "lip.bind", {"clip_id": cid4, "voice_id": "vc:0"})
proj4["dialogue"] = []       # 模拟台词被删
ok, msg, data = RC.run_command(wb4, "lip.list", {})
chk(ok, "list 不崩")
if data["bound"]:
    chk(data["bound"][0].get("broken") is True,
        "绑定指向已删除的语音 -> 标 broken")

import shutil
shutil.rmtree(TMP, ignore_errors=True)

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("口型联动命令：全部通过")