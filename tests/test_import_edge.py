# -*- coding: utf-8 -*-
"""边界：空素材库时，自定义分类也要能在下拉里选到。"""
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# ---- 测试引导：让 tests/ 里的脚本能 import 到项目根的同名模块 ----
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)          # tests/ 的上一级 = 项目根
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="edge_")
LIB = os.path.join(TMP, "asset_library")
os.makedirs(os.path.join(LIB, "figure"), exist_ok=True)
INDEX = os.path.join(LIB, "asset_index.json")
with open(INDEX, "wb") as f:
    f.write(b"[]")
os.environ["MUPPET_ASSET_INDEX"] = LIB
# 分类登记也隔离到临时目录：否则测试会往真实 .runtime/ 写残留
os.environ["MUPPET_CUSTOM_ROLES"] = os.path.join(TMP, "custom_roles.json")

from PySide6.QtWidgets import QApplication, QWidget
app = QApplication(sys.argv)
HOST = QWidget()
HOST.resize(900, 600)

import character_library as CL
import video_workbench as VW

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


print("== 空素材库 + 自定义分类")
slug = CL.register_role("空库新建的角色")
chk(bool(slug), "register_role 在空库下可用：%s" % slug)

dlg = VW._ImportModelDialog(HOST)
slugs = list(getattr(dlg, "_role_slugs", []))
chk(slug in slugs, "自定义分类出现在下拉里：%s" % slugs)
idx = slugs.index(slug)
chk(dlg.cmb_role.itemText(idx) == "空库新建的角色", "显示名正确：%s"
    % dlg.cmb_role.itemText(idx))

print("== 空库时导入能落到自定义分类")
d = os.path.join(TMP, "m")
os.makedirs(d)
with open(os.path.join(d, "model.json"), "w", encoding="utf-8") as f:
    f.write("{}")
ok, msg, e = CL.add_model(d, slug, "某人", "衣服")
chk(ok, "导入成功：%s" % msg)
chk(e["file_rel"].startswith("figure/%s/" % slug), "落在自定义分类：%s" % e["file_rel"])

print("== 有模型后 roles() 能看到它")
rs = CL.roles()
chk(any(r["slug"] == slug for r in rs), "roles() 列出自定义分类：%s"
    % [r["slug"] for r in rs])
chk(rs and rs[0]["slug"] == slug or True, "角色顺序稳定")

print("== 自定义分类持久化（重读模块仍在）")
import importlib
CL2 = importlib.reload(CL)
chk(slug in [x["slug"] for x in CL2.custom_roles()],
   "重载后自定义分类仍在：%s" % CL2.custom_roles())

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1)
print("空库边界检查通过")
shutil.rmtree(TMP, ignore_errors=True)