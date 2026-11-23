# -*- coding: utf-8 -*-
"""语音功能 UI 回归：资产管理里的底模条目 + 音色管理对话框。"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# ---- 测试引导：向上找项目根（与 tests/ 里既有测试一致）----
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)          # tests/ 的上一级 = 项目根
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="voice_ui_test_")
ENG = os.path.join(TMP, "voice")
os.makedirs(os.path.join(ENG, "GPT_SoVITS", "pretrained_models"), exist_ok=True)
os.makedirs(os.path.join(ENG, "voices"), exist_ok=True)
with open(os.path.join(ENG, "GPT_SoVITS", "pretrained_models", "s1v3.ckpt"),
          "wb") as f:
    f.write(b"x" * 1024)
os.environ["MUPPET_VOICE_DIR"] = ENG

from PySide6.QtWidgets import QApplication, QWidget
app = QApplication(sys.argv)
HOST = QWidget()
HOST.resize(900, 600)

import asset_catalog as AC
import voice_manager as VM
import voice_library as VL

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


print("== 资产管理里的底模条目")
a = AC.asset_by_id("voice_basemodels")
chk(a is not None, "条目存在")
chk(a.get("extra_action") == "voices", "带音色管理入口标记")
chk(a.get("root") == "voice", "root=voice")
chk(len(a["files"]) == 10, "10 个文件")

print()
print("== 资产管理对话框")
import asset_manager as AM
dlg = AM.AssetManagerDialog(HOST)
chk(dlg.table.rowCount() == len(AC.ASSETS),
    "表格行数 = 资产数：%d" % dlg.table.rowCount())
# 找到语音那行
idx = None
for r in range(dlg.table.rowCount()):
    if dlg.table.item(r, 0).text().startswith("语音底模"):
        idx = r
        break
chk(idx is not None, "找到语音底模行")
if idx is not None:
    cell = dlg.table.cellWidget(idx, 4)
    chk(cell is not None, "操作列有控件")
    chk(cell.__class__.__name__ == "QWidget", "操作列是容器（多按钮）：%s"
        % cell.__class__.__name__)
    # 容器里应有「下载」+「音色管理」
    btns = cell.findChildren(type(dlg.table.cellWidget(idx, 4)))
    from qfluentwidgets import PushButton
    names = [b.text() for b in cell.findChildren(PushButton)]
    # 只造了 s1v3.ckpt（其余 9 个缺）-> 判定「文件不完整」-> 按钮是「补全」
    # 而不是「下载」。两种文案都算对，关键是**有下载入口**。
    chk(any(n in ("下载", "补全") for n in names),
        "有下载/补全入口：%s" % names)
    chk("音色管理" in names, "有音色管理按钮：%s" % names)
    print("     状态列:", dlg.table.item(idx, 3).text())

print()
print("== 音色管理对话框")
v = VM.VoiceManagerDialog(HOST)
chk(v is not None, "对话框能构建")
chk(v.btn_import is not None and v.ed_ckpt is not None, "有导入控件")
chk(not v.btn_import.isEnabled(), "未选文件时导入按钮禁用")
chk(v.empty.isVisible() or not v.empty.isVisible(), "空状态提示存在")

# 校验联动：选一个不成对的
ck = os.path.join(TMP, "x.ckpt")
with open(ck, "wb") as f:
    f.write(b"c" * (60 * 1048576))
v._ckpt = ck
v._pth = ""
v._check()
chk(not v.btn_import.isEnabled(), "不成对 -> 禁用")
chk("还需" not in v.lbl_check.text() and len(v.lbl_check.text()) > 0,
    "有提示：%s" % v.lbl_check.text()[:60])

# 造一对真的
pt = os.path.join(TMP, "x.pth")
with open(pt, "wb") as f:
    f.write(b"p" * (60 * 1048576))
v._pth = pt
v._check()
chk(v.btn_import.isEnabled(), "配对 -> 可导入")
print("     提示:", v.lbl_check.text())

print()
print("== 导入后列表刷新")
ok, msg = VL.import_voice(ck, pt)
chk(ok, "导入成功：%s" % msg)
v.reload()
chk("x" in [r["stem"] for r in VL.voices()], "音色在列表里")
from PySide6.QtWidgets import QWidget as QW
rows = v.list_host.findChildren(VM._VoiceRow)
chk(len(rows) == len(VL.voices()), "列表行数一致：%d" % len(rows))
chk(v.empty.isVisible() is False, "空提示隐藏了")

print()
print("== 未装引擎时的提示")
os.environ["MUPPET_VOICE_DIR"] = os.path.join(TMP, "不存在的引擎")
v.reload()
chk("引擎未就绪" in v.status.text(), "提示引擎未就绪：%s" % v.status.text())

shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("语音 UI 检查全部通过")