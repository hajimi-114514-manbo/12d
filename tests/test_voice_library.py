# -*- coding: utf-8 -*-
"""voice_library 端到端测试（临时引擎目录，不碰真实音色）。

覆盖：
  · 扫描：同名 ckpt+pth 配对、缺一不成对
  · validate_pair：扩展名 / 存在性 / 体积 / 同名四类校验
  · import_voice：正常导入、重名不覆盖、失败不留半成品
  · remove_voice / stats
"""
import io
import os
import shutil
import sys
import tempfile

# ---- 测试引导：向上找项目根（与 tests/ 里既有测试一致）----
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)          # tests/ 的上一级 = 项目根
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="voice_lib_test_")
ENG = os.path.join(TMP, "voice")
os.makedirs(os.path.join(ENG, "GPT_SoVITS", "pretrained_models"), exist_ok=True)
os.makedirs(os.path.join(ENG, "voices"), exist_ok=True)
# 让 voice_paths 认为引擎就绪
with open(os.path.join(ENG, "GPT_SoVITS", "pretrained_models", "s1v3.ckpt"),
          "wb") as f:
    f.write(b"x" * 1024)
os.environ["MUPPET_VOICE_DIR"] = ENG

import voice_paths as VP
import voice_library as VL

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def make_pair(stem, size_mb=60):
    """造一对假音色（体积够过下限校验）。"""
    d = tempfile.mkdtemp(dir=TMP)
    ck = os.path.join(d, stem + ".ckpt")
    pt = os.path.join(d, stem + ".pth")
    with open(ck, "wb") as f:
        f.write(b"c" * (size_mb * 1048576))
    with open(pt, "wb") as f:
        f.write(b"p" * (size_mb * 1048576))
    return ck, pt


print("== 路径解析")
chk(VP.voice_dir() == ENG, "引擎目录：%s" % VP.voice_dir())
chk(VP.voices_dir() == os.path.join(ENG, "voices"), "音色目录")
chk(VP.pretrained_dir().endswith("pretrained_models"), "底模目录")

print()
print("== 扫描（空目录）")
chk(VL.voices() == [], "空目录无音色")
chk(VL.voice_names() == [], "voice_names 空")
st = VL.stats()
chk(st["total"] == 0 and st["ok"] == 0, "stats：%s" % st)

print()
print("== 导入正常一对")
ck, pt = make_pair("MyGO_测试角色_v2pp")
ok, msg = VL.import_voice(ck, pt)
chk(ok, "导入成功：%s" % msg)
chk(VL.find_voice("MyGO_测试角色_v2pp") is not None, "能查到")
chk(VL.voice_names() == ["MyGO_测试角色_v2pp"], "voice_names 列出：%s"
   % VL.voice_names())
r = VL.find_voice("MyGO_测试角色_v2pp")
chk(r["ok"], "配对完整")
chk(r["size"] > 100 * 1048576, "体积统计正确")
chk(os.path.isfile(r["ckpt"]) and os.path.isfile(r["pth"]), "两个文件都在")

print()
print("== 重名不覆盖")
ck2, pt2 = make_pair("MyGO_测试角色_v2pp")
ok, msg = VL.import_voice(ck2, pt2)
chk(ok, "重名导入成功（改名）：%s" % msg)
chk(msg != "已导入音色：MyGO_测试角色_v2pp", "改了名：%s" % msg)
chk(VL.find_voice("MyGO_测试角色_v2pp_2") is not None, "新名存在")
chk(os.path.getsize(VL.find_voice("MyGO_测试角色_v2pp")["ckpt"])
    == 60 * 1048576, "原音色没被覆盖")

print()
print("== 校验四类错误")
bad = os.path.join(TMP, "x.pth")
with open(bad, "wb") as f:
    f.write(b"p" * (60 * 1048576))
ok, msg = VL.validate_pair(bad, bad)
chk(not ok and "后缀" in msg, "扩展名错：%s" % msg)

ck3, pt3 = make_pair("A")
ok, msg = VL.validate_pair(ck3, os.path.join(TMP, "不存在.pth"))
chk(not ok and "不存在" in msg, "文件不存在：%s" % msg)

ck4, pt4 = make_pair("B")
small = os.path.join(TMP, "C.pth")
with open(small, "wb") as f:
    f.write(b"p" * 1024)
ok, msg = VL.validate_pair(ck4, small)
chk(not ok and "体积" in msg, "体积异常：%s" % msg)

ck5, pt5 = make_pair("名字一")
ck6, pt6 = make_pair("名字二")
ok, msg = VL.validate_pair(ck5, pt6)
chk(not ok and "同名" in msg, "名字不同：%s" % msg)

print()
print("== 缺一半不成对")
half = os.path.join(TMP, "只有一半.ckpt")
with open(half, "wb") as f:
    f.write(b"c" * (60 * 1048576))
shutil.copy2(half, os.path.join(ENG, "voices", "只有一半.ckpt"))
rows = VL.voices()
bad_row = [r for r in rows if r["stem"] == "只有一半"]
chk(bad_row and not bad_row[0]["ok"], "缺 pth -> 标记为不成对")
chk("只有一半" not in VL.voice_names(), "不成对的不进 voice_names")

print()
print("== 失败不留半成品")
ck7, pt7 = make_pair("D")
# 让 voices/ 变成一个**文件**（不是目录）-> os.makedirs 必失败
bad_dir = os.path.join(ENG, "voices")
real_dir = bad_dir + "_real"
os.rename(bad_dir, real_dir)
with open(bad_dir, "wb") as f:      # 用文件占住 voices 这个路径
    f.write(b"x")
ok, msg = VL.import_voice(ck7, pt7)
chk(not ok, "voices 路径被文件占住时导入失败：%s" % msg)
os.remove(bad_dir)
os.rename(real_dir, bad_dir)
leftovers = [f for f in os.listdir(os.path.join(ENG, "voices"))
             if f.startswith("D")]
chk(not leftovers, "失败没留半成品：%s" % leftovers)

print()
print("== 删除")
ok, msg = VL.remove_voice("MyGO_测试角色_v2pp")
chk(ok, "删除成功：%s" % msg)
chk(VL.find_voice("MyGO_测试角色_v2pp") is None, "删干净了")
ok, msg = VL.remove_voice("不存在")
chk(not ok, "删不存在的返回 False")

print()
print("== stats")
st = VL.stats()
chk(st["total"] >= 2, "统计总数：%d" % st["total"])
chk(st["incomplete"] >= 1, "不成对数：%d" % st["incomplete"])

shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("voice_library 全部检查通过")