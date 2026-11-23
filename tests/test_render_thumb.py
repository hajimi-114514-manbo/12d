# -*- coding: utf-8 -*-
"""渠道①（导入后渲染缩略图）验证 —— 需要**真实素材库**。

为什么单独放：渲染依赖素材库里的 Cubism SDK（lib/live2d.min.js）和
WebGAL 运行时，临时素材库跑不起来（实测报 boot_failed:
"Could not find Cubism 2 runtime"）。所以这个脚本在**真库**上跑，
并且**跑完必须完整回滚**（删导入目录 + 索引还原到原样）。

用法：
    python tests/test_render_thumb.py

不在常规回归里跑（会动用户素材库）。CI/普通开发跑 tests/ 下其余脚本。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# offscreen 出真画面需要 swiftshader 软渲染（见 webgal_preview
# .recommended_chromium_flags 的实测矩阵）
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--disable-gpu --use-gl=angle --use-angle=swiftshader "
                      "--enable-unsafe-swiftshader")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)          # tests/ 的上一级 = 项目根
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

import character_library as CL
import video_workbench as VW
import l2d_renderer as LR

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


import asset_paths as AP
LIB = AP.ensure_library(scan=False)
if not LIB:
    print("素材库不可用，跳过（需要真实素材库）")
    sys.exit(0)
INDEX = os.path.join(LIB, "asset_index.json")
if not os.path.isfile(INDEX):
    print("素材库索引不存在，跳过")
    sys.exit(0)

before_items = json.loads(io.open(INDEX, encoding="utf-8-sig").read())
print("素材库:", LIB)
print("导入前索引 %d 条" % len(before_items))

# 分类登记隔离（别写进用户环境）
os.environ["MUPPET_CUSTOM_ROLES"] = os.path.join(
    tempfile.gettempdir(), "muppet_render_verify_roles.json")

# 找一个源模型：任意含 model.json 的
src = None
for m in CL.all_models():
    d = CL.model_path(m)
    if os.path.isfile(os.path.join(d, "model.json")):
        src = d
        break
chk(src is not None, "找到源模型")
if src is None:
    sys.exit(1)
print("     源:", src)

# 导入到「其他」分类（lib），别动常用角色根
role = "lib" if "lib" in CL._ROLE_NAME else list(CL._ROLE_NAME)[0]
# 角色根的共享池在导入前是否存在？导入可能顺带复制一个过来，
# 回滚时要知道是「原来就有」还是「这次带进来的」——不然每次跑测试
# 都往素材库扔 1000+ 个文件。
_role_dir = os.path.join(LIB, "figure", role)
_pool_before = os.path.isdir(os.path.join(_role_dir, ".mtn_exp"))
ok, msg, entry = CL.add_model(src, role, "渲染验证作者", "渲染验证服",
                              thumb_mode="none")
chk(ok, "导入成功：%s" % msg)
if not ok:
    sys.exit(1)
mdir = CL.model_path(entry)
print("     目标:", mdir)

try:
    print("== 渲染（渠道①）")
    wb = VW.VideoWorkbench()
    ok_render = wb._grab_model_thumb(mdir)
    chk(bool(ok_render), "渲染返回 True")

    thumb = os.path.join(mdir, "_thumb.png")
    chk(os.path.isfile(thumb), "_thumb.png 已生成")
    if os.path.isfile(thumb):
        from PIL import Image
        im = Image.open(thumb)
        px = list(im.convert("RGBA").getdata())
        uniq = len(set(px))
        print("     尺寸 %s  唯一色 %d  字节 %d"
              % (im.size, uniq, os.path.getsize(thumb)))
        chk(uniq > 100, "不是空白（唯一色 %d > 100）" % uniq)
        chk(im.width * 4 == im.height * 3, "3:4 竖幅：%s" % (im.size,))
        chk(os.path.getsize(thumb) > 5000,
            "字节数 %d > 5000" % os.path.getsize(thumb))
finally:
    print("== 回滚")
    shutil.rmtree(mdir, ignore_errors=True)
    # 共享资源池：导入时可能顺带复制到角色根，回滚要一起清掉
    # （否则每次跑测试都往素材库扔 1000+ 个文件）
    pool = os.path.join(os.path.dirname(mdir), ".mtn_exp")
    if not _pool_before and os.path.isdir(pool):
        shutil.rmtree(pool, ignore_errors=True)
    items = [e for e in json.loads(io.open(INDEX, encoding="utf-8-sig").read())
             if e.get("id") != entry["id"]]
    body = json.dumps(items, ensure_ascii=False, indent=2)
    text = "\r\n".join(" " + ln for ln in body.split("\n"))
    io.open(INDEX, "wb").write(text.encode("utf-8"))
    rolled = json.loads(io.open(INDEX, encoding="utf-8-sig").read())
    chk(len(rolled) == len(before_items), "索引条数还原：%d" % len(rolled))
    chk(rolled == before_items, "索引内容与导入前完全一致")
    chk(not os.path.isdir(mdir), "导入目录已删")
    LR.shutdown()

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("渠道① 渲染验证通过（已回滚）")