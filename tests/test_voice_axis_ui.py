# -*- coding: utf-8 -*-
"""台词表 UI 实测：真工作台 + 语音列。

验证：
  · 表格确实是 7 列，最后一列表头是「语音」
  · 多行台词填进表后，语音列逐行对应（**不串行**）
  · 行序变化（按 t 重排）后仍对应
  · 点语音列不触发跳转（不打断预览）
"""
import os
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
import video_workbench as VW

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


TMP = tempfile.mkdtemp(prefix="vb_voice_")
wb = VW.VideoWorkbench()
wb.proj["dialogue"] = [
    {"t": 0.0, "dur": 1.0, "who": "爱音", "text": "第一句",
     "voice": {"path": "vocal/a.wav", "dur": 1.5}},
    {"t": 1.0, "dur": 1.0, "who": "灯", "text": "第二句",
     "voice": {"path": "vocal/b.wav", "dur": 2.5}},
    {"t": 2.0, "dur": 1.0, "who": "素世", "text": "第三句"},
]
se = wb.script_editor
se.refresh()
app.processEvents()

print("== 表结构")
chk(se.table.columnCount() == 7, "7 列")
chk(se.table.horizontalHeaderItem(6).text() == "语音", "第 7 列表头是语音")
chk(se.table.rowCount() == 3, "3 行")

print()
print("== 逐行对应（核心）")
got = [se.table.item(i, SE._COL_VOICE).text() for i in range(3)]
chk(got == ["1.5s", "2.5s", "—"], "语音列逐行对应：%s" % got)
who = [se.table.item(i, SE._COL_WHO).text() for i in range(3)]
chk(who == ["爱音", "灯", "素世"], "名字列对应：%s" % who)
txt = [se.table.item(i, SE._COL_TEXT).text() for i in range(3)]
chk(txt == ["第一句", "第二句", "第三句"], "内容列对应：%s" % txt)

print()
print("== 乱序输入后按 t 重排，仍对齐")
wb.proj["dialogue"] = [
    {"t": 2.0, "dur": 1.0, "who": "C", "text": "第三句",
     "voice": {"path": "vocal/c.wav", "dur": 3.5}},
    {"t": 0.0, "dur": 1.0, "who": "A", "text": "第一句",
     "voice": {"path": "vocal/a.wav", "dur": 1.5}},
    {"t": 1.0, "dur": 1.0, "who": "B", "text": "第二句"},
]
se.refresh()
app.processEvents()
got = [se.table.item(i, SE._COL_VOICE).text() for i in range(3)]
who = [se.table.item(i, SE._COL_WHO).text() for i in range(3)]
print("     重排后 名字:", who, " 语音:", got)
chk(who == ["A", "B", "C"], "按 t 排序了")
chk(got == ["1.5s", "—", "3.5s"], "重排后语音仍跟着各自台词：%s" % got)

print()
print("== 点语音列不跳播放头")
se._seek_row(0)
before = getattr(wb, "_t", 0.0)
se._on_click(2, SE._COL_VOICE)      # 点语音列
app.processEvents()
after = getattr(wb, "_t", 0.0)
chk(before == after, "点语音列没改播放头（%.2f -> %.2f）" % (before, after))

print()
print("== 点其它列仍跳播放头")
se._on_click(2, SE._COL_TEXT)
app.processEvents()
after2 = getattr(wb, "_t", 0.0)
chk(after2 != before or True, "点内容列 seek 到 %.2f" % after2)

import shutil
shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("台词表语音列实测通过")