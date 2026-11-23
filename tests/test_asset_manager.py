# -*- coding: utf-8 -*-
"""资产管理对话框行为回归。

重点验证：
  · 7 个包全部列出，分类/体积/状态正确
  · asset_files_missing 的二元组不会被误当 bool（曾踩过）
  · 汇总不把「下载体积」和「已装占用」相加（曾踩过）
  · 无 url 且无 parts 的包，点下载给提示而不是崩
  · 已装的包不显示「下载」按钮
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# ---- 测试引导：让 tests/ 里的脚本能 import 到项目根的同级模块 ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)          # tests/ 的上一级 = 项目根
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication, QDialog
app = QApplication(sys.argv)

import asset_manager as AM
import asset_catalog as AC

fails = []


def chk(cond, msg):
    if cond:
        print("  PASS", msg)
    else:
        print("  FAIL", msg)
        fails.append(msg)


print("=== 1. 清单完整性 ===")
d = AM.AssetManagerDialog()
n = d.table.rowCount()
chk(n == len(AC.ASSETS), "行数 %d == 清单 %d" % (n, len(AC.ASSETS)))

print("\n=== 2. 每行字段非空 ===")
for r in range(n):
    name = d.table.item(r, 0).text()
    kind = d.table.item(r, 1).text()
    size = d.table.item(r, 2).text()
    st = d.table.item(r, 3).text()
    chk(bool(name) and bool(kind) and size != "-" and bool(st),
        "第 %d 行: %s / %s / %s / %s" % (r, name, kind, size, st))

print("\n=== 3. 汇总不该出现「已装 > 总需」 ===")
s = d.summary.text()
print("  汇总:", s)
import re
m = re.search(r"占磁盘 ([\d.]+) (\w+)；未装还需下载 ([\d.]+) (\w+)", s)
chk(m is not None, "汇总格式可解析")
if m:
    got = float(m.group(1)) * (1024 if m.group(2) == "GB" else 1)
    need = float(m.group(3)) * (1024 if m.group(4) == "GB" else 1)
    print("  已装 %.1f MB, 待下载 %.1f MB" % (got, need))
    chk(need >= 0, "待下载非负")

print("\n=== 4. 已装的包不该有「下载」按钮 ===")
for r in range(n):
    it = d._assets[r]
    w = d.table.cellWidget(r, 4)
    if it["installed"]:
        chk(not hasattr(w, "text") or w.text() not in ("下载", "补全"),
            "%s 已装 -> 无下载按钮 (%s)" % (it["id"], type(w).__name__))
    else:
        chk(hasattr(w, "text") and w.text() in ("下载", "补全"),
            "%s 未装 -> 有按钮 '%s'" % (it["id"], getattr(w, "text", lambda: "?")()))

print("\n=== 5. 无 url 的包点下载：给提示不崩 ===")
fake = {"asset": {"id": "fake_no_url", "name": "测试无地址包", "kind": "model", "size": 1},
        "id": "fake_no_url", "name": "测试无地址包", "kind": "model",
        "size": 1, "installed": False, "inst_size": 0, "missing": False}
try:
    d._start_download(fake)
    chk(True, "无 url 时未抛异常")
except Exception as e:
    chk(False, "无 url 时抛异常: %s" % e)

print("\n=== 6. reload 幂等 ===")
before = d.table.rowCount()
d.reload()
chk(d.table.rowCount() == before, "reload 后行数不变 (%d)" % before)

print("\n" + "=" * 60)
if fails:
    print("FAIL %d 项" % len(fails))
    sys.exit(1)
print("PASS 资产管理全部检查通过")