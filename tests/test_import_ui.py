# -*- coding: utf-8 -*-
"""导入模型 UI 回归：按钮可见性、对话框字段、校验联动、端到端导入。

重点验证几件容易做错的事：
  · 「导入模型」按钮只在角色页出现，别的页签必须隐藏
  · 对话框未填齐时确定按钮禁用（而不是点了才报错）
  · 缩略图渠道切换时「选图」按钮跟着启用/禁用
  · 走完整流程后：角色页能看到新模型、作者分组正确、缩略图生效
"""
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

TMP = tempfile.mkdtemp(prefix="uitest_")
LIB = os.path.join(TMP, "asset_library")
os.makedirs(os.path.join(LIB, "figure"), exist_ok=True)
INDEX = os.path.join(LIB, "asset_index.json")
INIT = [{"id": "bg_0001_t", "kind": "bg", "file": "background/a.webp",
         "copied": True, "name": "a", "tags": ["misc"], "meta": {}}]
with open(INDEX, "wb") as f:
    f.write(json.dumps(INIT, ensure_ascii=False, indent=2)
            .replace("\n", "\r\n").encode("utf-8"))
os.environ["MUPPET_ASSET_INDEX"] = LIB
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


def make_model(name):
    d = os.path.join(TMP, name)
    os.makedirs(os.path.join(d, "live2d"), exist_ok=True)
    with open(os.path.join(d, "model.json"), "w", encoding="utf-8") as f:
        f.write('{"FileReferences":{"Motions":{"idle":[{"file":"i.mtn"}]},'
                '"Expressions":{}}}')
    return d


SRC = make_model("soyo_jk")
THUMB = os.path.join(TMP, "pick.png")
from PIL import Image
Image.new("RGB", (300, 400), (10, 120, 200)).save(THUMB)

print("== 对话框字段与校验联动")
dlg = VW._ImportModelDialog(HOST)
chk(hasattr(dlg, "ed_src") and hasattr(dlg, "ed_name"), "有源目录/命名字段")
chk(hasattr(dlg, "cmb_role"), "有一级分类下拉")
chk(hasattr(dlg, "cmb_author"), "有二级改模人下拉")
chk(hasattr(dlg, "cmb_thumb"), "有缩略图渠道下拉")
chk(dlg.cmb_thumb.count() == 3,
    "缩略图三选项（选图/渲染/不要）：%d" % dlg.cmb_thumb.count())
chk(not dlg.yesButton.isEnabled(), "初始未填 -> 确定禁用")
chk("还需" in dlg.lbl_hint.text(), "有缺项提示：%s" % dlg.lbl_hint.text())

dlg._src_dir = SRC
dlg.ed_src.setText(SRC)
dlg.ed_name.setText("月之森JK")
dlg.cmb_thumb.setCurrentIndex(VW._ImportModelDialog.THUMB_NONE)
dlg._refresh_hint()
chk(dlg.yesButton.isEnabled(), "填齐（缩略图=不要）后确定可用")

print("== 缩略图渠道切换")
dlg.cmb_thumb.setCurrentIndex(VW._ImportModelDialog.THUMB_PICK)
chk(dlg.btn_thumb.isEnabled(), "选「自己选图」-> 选图按钮可用")
chk(not dlg.yesButton.isEnabled(), "还没选图 -> 确定禁用")
dlg._thumb_path = THUMB
dlg._refresh_hint()
chk(dlg.yesButton.isEnabled(), "选了图 -> 确定可用")
dlg.cmb_thumb.setCurrentIndex(VW._ImportModelDialog.THUMB_RENDER)
chk(not dlg.btn_thumb.isEnabled(), "切到「渲染」-> 选图按钮禁用")
chk(dlg.yesButton.isEnabled(), "渲染渠道不需要预先选图 -> 确定可用")
dlg.cmb_thumb.setCurrentIndex(VW._ImportModelDialog.THUMB_NONE)
chk(not dlg.btn_thumb.isEnabled(), "切到「不要」-> 选图按钮禁用")

print("== values() 返回形状")
dlg._thumb_path = THUMB
dlg.cmb_thumb.setCurrentIndex(VW._ImportModelDialog.THUMB_PICK)
vals = dlg.values()
chk(len(vals) == 6, "返回 6 元组")
src_v, slug_v, au_v, cos_v, mode_v, tsrc_v = vals
chk(src_v == SRC, "源目录正确")
chk(cos_v == "月之森JK", "命名正确：%s" % cos_v)
chk(mode_v == "pick" and tsrc_v == THUMB, "渠道=选图且带图片路径")
chk(slug_v != "", "分类 slug 非空：%s" % slug_v)

print("== 工作台：按钮只在角色页出现")
wb = VW.VideoWorkbench()
chk(hasattr(wb, "btn_import_model"), "工作台有导入按钮")
pages = ["media", "audio", "bg", "template", "filter", "transition", "text"]
for p in pages:
    wb._asset_tab = p
    wb._rebuild_asset_cards_inner()
    app.processEvents()
    # 窗口本身没 show，isVisible() 永远是 False；要问「相对父控件是否可见」
    vis = wb.btn_import_model.isVisibleTo(wb.btn_import_model.parentWidget())
    if p == "media":
        chk(vis, "角色页(media) 按钮可见")
    else:
        chk(not vis, "%s 页按钮隐藏" % p)

print("== 端到端：走 CL.add_model（对话框最终调的就是它）")
ok, msg, entry = CL.add_model(SRC, slug_v, "紫铜Kris", "月之森JK",
                              thumb_mode="pick", thumb_src=THUMB)
chk(ok, "导入成功：%s" % msg)
d = CL.model_path(entry)
chk(os.path.isfile(os.path.join(d, "_thumb.png")), "缩略图已落盘")
last = [x for x in CL.models_of(slug_v) if x["file"] == entry["file_rel"]]
chk(bool(last) and CL.display_name(last[0]).startswith("月之森JK"),
    "显示名以服装名开头")
grp = [g for g in CL.authors_of(slug_v) if g["author"] == "紫铜Kris"]
chk(bool(grp), "作者分组里出现紫铜Kris")
chk(bool(grp) and any(m["file"] == entry["file_rel"] for m in grp[0]["items"]),
    "新模型在该作者分组项里")

print("== 角色页重建能看到它")
wb._asset_tab = "media"
wb._role_open = slug_v
wb._author_open = {"紫铜Kris"}
wb._author_open_role = slug_v
try:
    wb._rebuild_asset_cards_inner()
    app.processEvents()
    chk(True, "角色页重建未抛异常")
except Exception as e:
    chk(False, "角色页重建抛异常：%s" % e)

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1)
print("导入模型 UI 检查全部通过")
shutil.rmtree(TMP, ignore_errors=True)