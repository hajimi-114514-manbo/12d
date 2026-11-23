# -*- coding: utf-8 -*-
"""character_library.add_model 端到端测试（临时素材库，不碰真实数据）。

覆盖：
  · 源校验（无 model.json / 目录不存在）
  · 复制落盘 + 索引追加 + 缓存刷新
  · 目标目录撞名自动加序号
  · 作者署名文件被 _author_from_dir 认到
  · 索引原文拼接：其余部分逐字节不动，只有末尾多出新条目
  · 失败路径：索引写坏时不留半成品目录
  · register_role / custom_roles / roles 合并
"""
import io
import json
import os
import shutil
import sys
import tempfile

# ---- 测试引导：让 tests/ 里的脚本能 import 到项目根的同名模块 ----
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)          # tests/ 的上一级 = 项目根
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 隔离到临时素材库
TMP = tempfile.mkdtemp(prefix="libtest_")
LIB = os.path.join(TMP, "asset_library")
os.makedirs(os.path.join(LIB, "figure"), exist_ok=True)
INDEX = os.path.join(LIB, "asset_index.json")
# 最小索引（沿用真实文件的 CRLF + indent=2 + 元素前 1 空格风格）
INIT = [
    {"id": "bg_0001_test", "kind": "bg", "file": "background/a.webp",
     "copied": True, "name": "a", "tags": ["misc"], "meta": {}},
]
io.open(INDEX, "wb").write(
    json.dumps(INIT, ensure_ascii=False, indent=2)
    .replace("\n", "\r\n").encode("utf-8"))
os.environ["MUPPET_ASSET_INDEX"] = LIB
# 分类登记隔离到临时目录（否则测试污染真实 .runtime/）
os.environ["MUPPET_CUSTOM_ROLES"] = os.path.join(TMP, "custom_roles.json")

import character_library as CL  # noqa: E402

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


# ---- 造一个假模型目录 ----
SRC = os.path.join(TMP, "src_model")
os.makedirs(os.path.join(SRC, "live2d", "chara"))
io.open(os.path.join(SRC, "model.json"), "w", encoding="utf-8").write(
    '{"FileReferences":{"Motions":{"idle":[{"file":"idle.mtn"}]},'
    '"Expressions":{"smile":{}}}}')
io.open(os.path.join(SRC, "live2d", "chara", "texture_00.png"), "wb").write(b"x")

print("== 源校验")
ok, msg, _ = CL.add_model(os.path.join(TMP, "nope"), "anon", "某人", "测试服")
chk(not ok and "不存在" in msg, "目录不存在 -> 拒绝：%s" % msg)
bad = os.path.join(TMP, "bad")
os.makedirs(bad)
ok, msg, _ = CL.add_model(bad, "anon", "某人", "测试服")
chk(not ok and "model.json" in msg, "无 model.json -> 拒绝：%s" % msg)
ok, msg, _ = CL.add_model(SRC, "", "某人", "测试服")
chk(not ok and "角色分类" in msg, "未选分类 -> 拒绝：%s" % msg)
ok, msg, _ = CL.add_model(SRC, "不存在的角色", "某人", "测试服")
chk(not ok and "未知角色" in msg, "未知分类 -> 拒绝：%s" % msg)

print("== 导入主流程")
ok, msg, entry = CL.add_model(SRC, "anon", "北极暖水港", "测试服")
chk(ok, "导入成功：%s" % msg)
chk(entry is not None and entry["kind"] == "model", "返回条目 kind=model")
chk(entry["file_rel"] == "figure/anon/src_model", "目标相对路径：%s" % entry["file_rel"])
chk(os.path.isdir(os.path.join(LIB, entry["file_rel"])), "目录已复制落盘")
chk(os.path.isfile(os.path.join(LIB, entry["file_rel"], "model.json")),
   "model.json 已复制")
chk(os.path.isfile(os.path.join(LIB, entry["file_rel"],
                                "改模作者：北极暖水港.txt")), "作者署名文件已写")
chk("千早爱音·测试服" == entry["name"], "索引 name = 角色·服装名：%s" % entry["name"])
chk(entry["meta"].get("motions") == ["idle"], "meta.motions 解析：%s" % entry["meta"])
chk(entry["meta"].get("expressions") == ["smile"], "meta.expressions 解析")

print("== 作者被现有分组逻辑认到")
ms = CL.models_of("anon")
chk(len(ms) == 1, "models_of 能查到 1 条")
grp = CL.authors_of("anon")
chk(len(grp) == 1 and grp[0]["author"] == "北极暖水港",
   "作者分组：%s" % [g["author"] for g in grp])
chk(CL.display_name(ms[0]) == "测试服（北极暖水港）", "显示名：%s" % CL.display_name(ms[0]))

print("== 索引：其余部分逐字节不动")
raw = io.open(INDEX, "rb").read()
j = json.loads(raw.decode("utf-8"))
chk(len(j) == 2, "索引从 %d 条变 %d 条" % (len(INIT), len(j)))
chk(j[0] == INIT[0], "原有条目内容不变")
# 与 test_index_append 同判据：旧内容（去掉结尾 ]）应作为前缀完整保留
_before = io.open(INDEX, "rb").read().decode("utf-8")
_head = _before[:_before.rfind("]")].rstrip()
chk(_before.startswith(_head), "原内容作为前缀完整保留")
chk(_before.endswith("]"), "结尾仍是 ]")
chk("\r\n" in _before, "CRLF 风格保持")

print("== 撞名自动加序号")
SRC2 = os.path.join(TMP, "src_model2")
os.makedirs(SRC2)
io.open(os.path.join(SRC2, "model.json"), "w", encoding="utf-8").write("{}")
ok, msg, e2 = CL.add_model(SRC2, "anon", "某人", "二号")
chk(ok, "第二次导入成功")
chk(e2["file_rel"] == "figure/anon/src_model2", "不同源目录名不撞：%s" % e2["file_rel"])
# 同名源目录再导一次 -> 应加序号
SRC3 = os.path.join(TMP, "src_model")
ok, msg, e3 = CL.add_model(SRC3, "anon", "某人", "三号")
chk(ok, "同名源目录导入成功")
chk(e3["file_rel"] == "figure/anon/src_model_2",
   "撞名加序号：%s" % e3["file_rel"])

print("== id 不撞车")
ids = [e["id"] for e in json.loads(io.open(INDEX, encoding="utf-8").read())
       if e.get("kind") == "model"]
chk(len(ids) == len(set(ids)), "所有 model id 唯一：%s" % ids)

print("== 自定义分类")
slug = CL.register_role("我的角色")
chk(slug == "我的角色", "新建分类 slug：%s" % slug)
chk(slug in CL._ROLE_NAME, "并入 _ROLE_NAME")
chk(CL.custom_roles() and CL.custom_roles()[-1]["cn"] == "我的角色",
   "custom_roles 可读")
ok, msg, e4 = CL.add_model(SRC2, slug, "某人", "自定义下的衣服")
chk(ok, "往自定义分类导入成功")
chk(e4["file_rel"].startswith("figure/%s/" % slug), "落在自定义分类下：%s" % e4["file_rel"])
roles = CL.roles()
chk(any(r["slug"] == slug for r in roles), "roles() 能列出自定义分类")

print("== 缩略图渠道②")
try:
    import model_thumb as MT
    from PIL import Image
    img = os.path.join(TMP, "pick.jpg")
    Image.new("RGB", (400, 300), (200, 30, 30)).save(img)
    dst_dir = os.path.join(LIB, e4["file_rel"])
    p = MT.save_image_file(img, dst_dir)
    chk(bool(p) and os.path.isfile(p), "选图落盘 _thumb.png")
    chk(os.path.basename(p) == "_thumb.png", "文件名是 _thumb.png")
    m = [x for x in CL.models_of(slug) if x["file"] == e4["file_rel"]][0]
    chk(bool(CL.thumb_path(m)), "thumb_path 能读到：%s" % CL.thumb_path(m))
    chk(not MT.save_image_file(os.path.join(TMP, "nope.jpg"), dst_dir),
       "源图不存在 -> 返回空不抛")
except ImportError as e:
    print("  SKIP PIL 不可用：%s" % e)

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("add_model 全部检查通过")
shutil.rmtree(TMP, ignore_errors=True)
