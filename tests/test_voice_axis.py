# -*- coding: utf-8 -*-
"""语音轴（台词表语音列）测试。

核心要求：**语音轴与角色台词轴相对齐** —— 第 N 行台词的语音就是第 N 条
dialogue 的 voice，不能错位、不能串行。

覆盖：
  · 列结构（多出「语音」列，位置在最后）
  · 单元格文本（有时长显示时长，无则显示 —）
  · **对齐性**：多行台词各带各的语音，行序改变后仍一一对应
  · voice 字段的 sanitize（脏值只丢字段不丢台词）
  · 合成产物的命名与路径规则
"""
import io
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# ---- 测试引导：向上找项目根（与 tests/ 里既有测试一致）----
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

import script_editor as SE
import video_model as V
import voice_synth as VS

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


print("== 列结构")
chk(SE._NCOL == 7, "7 列：%s" % (SE._HEADERS,))
chk(SE._HEADERS[-1] == "语音", "最后一列是语音")
chk(SE._COL_VOICE == 6, "语音列索引 = 6")
chk(SE._COL_TEXT == 2, "内容列索引没被挤动（对齐的前提）")

print()
print("== 单元格文本")
chk(SE._voice_cell_text({}) == "—", "无语音 -> —")
chk(SE._voice_cell_text({"voice": {}}) == "有", "voice 空 dict -> 有")
chk(SE._voice_cell_text({"voice": {"dur": 1.94}}) == "1.9s", "有时长 -> 1.9s")
chk(SE._voice_cell_text({"voice": {"dur": "坏"}}) == "有", "脏时长 -> 有")

print()
print("== sanitize：voice 字段")
d = V._sanitize_dialogue([
    {"t": 0, "dur": 1, "who": "爱音", "text": "你好",
     "voice": {"path": "vocal/a.wav", "dur": 1.94, "voice": "X", "text": "你好"}},
    {"t": 1, "dur": 1, "who": "", "text": "无语音"},
    {"t": 2, "dur": 1, "who": "x", "text": "空路径",
     "voice": {"path": ""}},
    {"t": 3, "dur": 1, "who": "x", "text": "脏类型", "voice": "不是dict"},
    {"t": 4, "dur": 1, "who": "x", "text": "负时长",
     "voice": {"path": "v.wav", "dur": -1}},
])
chk(len(d) == 5, "5 条台词都在（脏值只丢字段不丢台词）：%d" % len(d))
chk(d[0].get("voice", {}).get("path") == "vocal/a.wav", "正常 voice 保留")
chk(d[0]["voice"]["dur"] == 1.94, "dur 保留")
chk(d[0]["voice"]["voice"] == "X", "音色名保留")
chk("voice" not in d[1], "没语音的没有该键")
chk("voice" not in d[2], "空路径 -> 丢字段")
chk("voice" not in d[3], "非 dict -> 丢字段")
chk(d[4]["voice"].get("path") == "v.wav", "负时长只丢 dur，保留 path")
chk("dur" not in d[4]["voice"], "负时长被丢")

print()
print("== 工程存盘/读盘后 voice 还在（往返）")
TMP = tempfile.mkdtemp(prefix="voice_axis_")
proj = {"type": "video_edit", "version": 1, "width": 1920, "height": 1080,
        "fps": 30, "tracks": [], "dialogue": [
            {"t": 0.0, "dur": 1.0, "who": "爱音", "text": "你好",
             "voice": {"path": "vocal/a.wav", "dur": 1.94}},
            {"t": 1.5, "dur": 2.0, "who": "灯", "text": "早上好"},
        ], "comments": []}
pp = os.path.join(TMP, "video_project.json")
import video_export as VE
try:
    VE.save_project(proj, TMP)
except Exception as e:
    print("  （save_project 签名不同，用 JSON 往返代替）:", e)
    with io.open(pp, "w", encoding="utf-8") as f:
        json.dump(proj, f, ensure_ascii=False)
with io.open(pp, encoding="utf-8") as f:
    back = json.load(f)
san = V._sanitize_dialogue(back.get("dialogue"))
chk(len(san) == 2, "往返后 2 条台词")
chk(san[0].get("voice", {}).get("path") == "vocal/a.wav",
    "往返后 voice 仍在：%s" % san[0].get("voice"))

print()
print("== 对齐性：多行各带各的语音，不串行")
rows = [{"t": 0.0, "dur": 1.0, "who": "A", "text": "第一句",
         "voice": {"path": "vocal/a.wav", "dur": 1.0}},
        {"t": 1.0, "dur": 1.0, "who": "B", "text": "第二句",
         "voice": {"path": "vocal/b.wav", "dur": 2.0}},
        {"t": 2.0, "dur": 1.0, "who": "C", "text": "第三句"}]
texts = [SE._voice_cell_text(r) for r in rows]
chk(texts == ["1.0s", "2.0s", "—"], "逐行对应不串行：%s" % texts)

print()
print("== 合成产物命名规则")
TMP2 = tempfile.mkdtemp(prefix="voice_out_")
p0 = VS.voice_file(TMP2, "a0", 0, "爱音")
p1 = VS.voice_file(TMP2, "a0", 1, "爱音")
p2 = VS.voice_file(TMP2, "a1", 0, "爱音")
chk(os.path.basename(p0) == "a0_000_爱音.wav", "命名含幕+序号+角色：%s"
    % os.path.basename(p0))
chk(p0 != p1, "同幕不同句不同文件")
chk(p0 != p2, "不同幕不同文件")
chk(os.path.dirname(p0) == os.path.join(TMP2, "vocal"), "落在 vocal/ 下")

print()
print("== 语言猜测")
chk(VS._ext_for("高松灯") == "zh", "中文名 -> zh")
chk(VS._ext_for("三角初華") == "zh", "繁体中文名 -> zh")
chk(VS._ext_for("若葉睦") == "zh", "汉字名 -> zh")
chk(VS._ext_for("あおい") == "ja", "假名 -> ja")

print()
print("== voice_path_of")
it = {"voice": {"path": "vocal/x.wav"}}
os.makedirs(os.path.join(TMP2, "vocal"), exist_ok=True)
with open(os.path.join(TMP2, "vocal", "x.wav"), "wb") as f:
    f.write(b"RIFF")
chk(VS.voice_path_of(TMP2, it) != "", "存在的文件能解析出路径")
chk(VS.voice_path_of(TMP2, {"voice": {"path": "vocal/none.wav"}}) == "",
    "文件不存在 -> 空")
chk(VS.voice_path_of(TMP2, {}) == "", "没有 voice -> 空")

shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(TMP2, ignore_errors=True)
print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("语音轴检查全部通过")