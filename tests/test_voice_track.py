# -*- coding: utf-8 -*-
"""时间轴语音轨的回归测试。

覆盖（需求「语音轨默认跟台词，可解锁为独立」= C 方案）：
  · 语音 clip 是 dialogue 的**投影**，不写进 proj["tracks"]
  · 默认跟台词：改台词 t -> 语音 clip 跟着走
  · 脱离后：改台词 t 不再影响语音
  · 拖动分流：未脱离时拖语音=改台词；脱离后=只改语音
  · sanitize：detach 脏值/无 t/负 t 都安全退化
  · 时间轴面板：语音轨出现/可隐藏、选中判定、脱离接口
  · 波形缓存
"""
import os
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


import video_model as VM


def mkproj(dialogue=None):
    p = VM.new_project()
    p["dialogue"] = dialogue or []
    return p


def voice(path="vocal/a.wav", dur=1.9, **kw):
    v = {"path": path, "dur": dur}
    v.update(kw)
    return v


print("== 投影：语音 clip 不进 tracks")
proj = mkproj([
    {"t": 0.0, "dur": 2.0, "who": "千早爱音", "text": "你好",
     "voice": voice()},
    {"t": 3.0, "dur": 2.0, "who": "高松灯", "text": "第二句",
     "voice": voice(path="vocal/b.wav", dur=2.2)},
])
chk(len(proj["tracks"]) == 2, "tracks 里仍只有 video+audio（没有被塞进语音）")
chk(not any(t.get("kind") == "voice" for t in proj["tracks"]),
    "tracks 里没有 kind=voice 的轨道")
clips = VM.voice_clips(proj)
chk(len(clips) == 2, "投影出 2 段：%d" % len(clips))
chk([c["id"] for c in clips] == ["vc:0", "vc:1"], "id 是 vc:<下标>")
chk(clips[0]["t"] == 0.0 and clips[1]["t"] == 3.0, "起点取自台词 t")
chk([c["src_index"] for c in clips] == [0, 1], "按起点排序后 src_index 仍可追")

print()
print("== 默认跟台词")
# 注意：voice_clips 按起点**排序**，改完 t 之后下标会变 —— 所以按
# src_index 找那条，别按下标找（测试一开始就踩了这个坑）
proj["dialogue"][0]["t"] = 5.0
_by_idx = {c["src_index"]: c for c in VM.voice_clips(proj)}
chk(_by_idx[0]["t"] == 5.0, "改台词 t -> 语音跟着走")
chk(_by_idx[0]["detach"] is False, "标记为未脱离")

print()
print("== 脱离后独立")
ok, msg, d = VM.voice_set_detach(proj, 0, True, t=1.0)
chk(ok, "脱离成功：%s" % msg)
chk(d["detach"] is True and d["t"] == 1.0, "返回 detach=True t=1.0")
_by_idx = {c["src_index"]: c for c in VM.voice_clips(proj)}
chk(_by_idx[0]["t"] == 1.0, "投影起点变成 1.0")
proj["dialogue"][0]["t"] = 9.0
_by_idx = {c["src_index"]: c for c in VM.voice_clips(proj)}
chk(_by_idx[0]["t"] == 1.0, "**改台词不再影响已脱离的语音**")
ok2, msg2, _ = VM.voice_set_detach(proj, 0, False)
chk(ok2, "绑回成功：%s" % msg2)
chk("detach" not in proj["dialogue"][0]["voice"], "绑回清掉了 detach 字段")
chk("t" not in proj["dialogue"][0]["voice"], "绑回清掉了 t 字段")
_by_idx = {c["src_index"]: c for c in VM.voice_clips(proj)}
chk(_by_idx[0]["t"] == 9.0, "绑回后跟随台词（9.0）")

print()
print("== 拖动分流")
proj2 = mkproj([{"t": 1.0, "dur": 2.0, "who": "千早爱音", "text": "x",
                 "voice": voice()}])
ok, msg, d = VM.voice_move(proj2, 0, 4.0)
chk(ok and proj2["dialogue"][0]["t"] == 4.0,
    "未脱离：拖语音=改台词 t（%s）" % msg)
VM.voice_set_detach(proj2, 0, True, t=4.0)
ok, msg, d = VM.voice_move(proj2, 0, 7.0)
chk(ok, "脱离后移动成功")
chk(proj2["dialogue"][0]["t"] == 4.0, "**台词没被改**")
chk(proj2["dialogue"][0]["voice"]["t"] == 7.0, "语音移到 7.0")

print()
print("== sanitize 边界")
chk(VM._sanitize_voice(voice(detach=False)).get("detach") is None,
    "detach=False 不落字段（规范化成默认）")
chk(VM._sanitize_voice(voice(detach=True, t=3.0)).get("detach") is True,
    "detach=True + t 保留")
chk("detach" not in VM._sanitize_voice(voice(detach=True)),
    "detach=True 但无 t -> 丢弃 detach（否则语音无处安放）")
chk("detach" not in VM._sanitize_voice(voice(detach="yes", t=3.0)),
    "detach 脏值 -> 当 False")
chk("detach" not in VM._sanitize_voice(voice(detach=True, t=-5)),
    "detach + 负 t -> 丢弃")

print()
print("== 时间轴面板")
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
import clip_timeline as CT

proj3 = mkproj([{"t": 0.0, "dur": 2.0, "who": "千早爱音", "text": "你好",
                 "voice": voice()}])
panel = CT.ClipTimelinePanel(None, proj3)
trs = panel._tracks()
chk(len(trs) == 3, "默认多出语音轨（3 轨：video/audio/voice）")
chk(trs[-1].get("kind") == "voice", "语音轨在最下面")
chk(trs[-1].get("id") == VM.VOICE_TRACK_ID, "轨道 id = %s" % VM.VOICE_TRACK_ID)
chk(len(trs[-1]["clips"]) == 1, "语音轨有 1 段 clip")
chk(panel.has_voice() is True, "has_voice=True")

panel.set_voice_track_visible(False)
chk(len(panel._tracks()) == 2, "可以隐藏语音轨")
chk(panel.voice_track_visible() is False, "状态记下了")
panel.set_voice_track_visible(True)
chk(len(panel._tracks()) == 3, "可以再打开")

# 没有语音时不出现空轨道
proj4 = mkproj([{"t": 0.0, "dur": 2.0, "who": "x", "text": "无语音"}])
panel4 = CT.ClipTimelinePanel(None, proj4)
chk(len(panel4._tracks()) == 2, "没有语音 -> 不出现空语音轨")
chk(panel4.has_voice() is False, "has_voice=False")

print()
print("== 选中与脱离接口")
panel.set_selection(["vc:0"])
chk(panel.selected_voice_index() == 0, "选中语音 -> 下标 0")
panel.set_selection(["v1:1"])
chk(panel.selected_voice_index() == -1, "选中普通 clip -> -1")
panel.set_selection(["vc:0"])
ok, msg = panel.voice_detach_selected(True)
chk(ok, "面板脱离成功：%s" % msg)
chk(proj3["dialogue"][0]["voice"].get("detach") is True, "数据里 detach=True")
ok, msg = panel.voice_detach_selected(False)
chk(ok, "面板绑回成功")

print()
print("== 渲染适配 + 波形")
rd = CT.voice_clips_for_render(proj3, proj_dir="")
chk(len(rd) == 1, "适配出 1 条")
chk(rd[0]["source"]["kind"] == "voice", "source.kind=voice（有专属颜色）")
chk(rd[0]["id"] == "vc:0", "id 保持一致")
chk(CT.CLIP_COLORS.get("voice") is not None, "有 voice 配色")

# 造一个真 wav 验波形读取
import tempfile
TMP = tempfile.mkdtemp(prefix="vt_")


def make_wav(path, dur=1.0, sr=32000):
    n = int(dur * sr)
    data = struct.pack("<%dh" % n, *([3000, -3000] * (n // 2)))
    def chunk(tag, body):
        return tag + struct.pack("<I", len(body)) + body
    hdr = b"RIFF" + struct.pack("<I", 4 + 36 + len(data)) + b"WAVE"
    fmt = struct.pack("<HHIIHH", 1, 1, sr, sr * 2, 2, 16)
    open(path, "wb").write(hdr + chunk(b"fmt ", fmt) + chunk(b"data", data))


wp = os.path.join(TMP, "w.wav")
make_wav(wp)
w = CT._voice_wave(wp)
chk(w is not None and len(w) >= 2, "能读出波形（%s 点）"
    % (len(w) if w else 0))
chk(all(0.0 <= v <= 1.0 for v in (w or [])), "波形归一化到 0..1")
w2 = CT._voice_wave(wp)
chk(w2 is w, "第二次命中缓存（同一对象）")
chk(CT._voice_wave(os.path.join(TMP, "nope.wav")) is None,
    "文件不存在 -> None（不抛）")

import shutil
shutil.rmtree(TMP, ignore_errors=True)

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("语音轨全部检查通过")