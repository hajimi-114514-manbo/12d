# -*- coding: utf-8 -*-
"""语音引擎分档的回归测试。

覆盖：
  · 四档资产条目存在、体积/落点正确
  · 各档装到**各自**的子目录（不互相覆盖）
  · voice_paths 能按档位找到已装引擎
  · 下载器的 voice_engine 模式接线正确
  · 分档的关键约束：引擎包里**不含 torch、不含音色**（用户自备）
"""
import io
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import asset_catalog as AC
import voice_paths as VP

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


TIERS = ["zh-basic", "zh-full", "ja", "zh-ja"]

print("== 四档资产条目")
engines = [a for a in AC.ASSETS if a.get("mode") == "voice_engine"]
chk(len(engines) == 4, "4 个引擎条目：%d" % len(engines))
for t in TIERS:
    a = AC.asset_by_id("voice_engine_" + t)
    chk(a is not None, "%s 条目存在" % t)
    if a:
        chk(a.get("mode") == "voice_engine", "%s mode 正确" % t)
        chk(a.get("target") == "voice", "%s 落点语义正确" % t)
        chk(a.get("needs_torch") is True, "%s 标记需另装 torch" % t)
        chk(2.5e9 < a["size"] < 4.5e9,
            "%s 体积在 2.5~4.5GB：%.2f GB" % (t, a["size"] / 1073741824))

print()
print("== 各档装到各自子目录（不覆盖）")
TMP = tempfile.mkdtemp(prefix="tier_test_")
os.environ["MUPPET_VOICE_DIR"] = TMP
targets = []
for t in TIERS:
    a = AC.asset_by_id("voice_engine_" + t)
    p = AC.parts_target_dir(a, ROOT)
    targets.append(p)
    chk(p.endswith(os.sep + t) or p.endswith("/" + t),
        "%s 落在 voice/%s/ 下：%s" % (t, t, p))
chk(len(set(targets)) == 4, "四档落点互不相同")

print()
print("== voice_paths 按档位找已装引擎")
for t in TIERS:
    d = os.path.join(TMP, t, "GPT_SoVITS", "pretrained_models")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "s1v3.ckpt"), "wb") as f:
        f.write(b"x" * 100)
chk(VP.installed_tier() == "zh-ja" or VP.installed_tier() in TIERS,
    "installed_tier() 能报出档位：%s" % VP.installed_tier())
eng = VP.ensure_voice(scan=False)
chk(eng and os.path.isdir(eng), "ensure_voice 找到引擎：%s" % eng)

print()
print("== 未装时能报「没有引擎」")
TMP2 = tempfile.mkdtemp(prefix="tier_empty_")
os.environ["MUPPET_VOICE_DIR"] = TMP2
chk(VP.installed_tier() == "", "空目录 -> 无档位")
chk(VP.ensure_voice(scan=False) is None, "空目录 -> ensure_voice 返回 None")

print()
print("== 分档的关键约束：引擎包不含 torch / 不含音色")
# 构建脚本里就是这么裁的；这里检查脚本本身有没有把这两样排除
B = os.path.join(ROOT, "tools", "build_voice_engine.py")
chk(os.path.isfile(B), "构建脚本存在")
if os.path.isfile(B):
    s = io.open(B, encoding="utf-8").read()
    chk('"torch"' in s and "nvidia" in s, "脚本排除 torch 系")
    chk("voices" in s and "gpt_weights" in s, "脚本排除音色")
    chk("users.pth" in s, "脚本重写 users.pth（关键）")
    # 引擎补丁以**文件**形式存在仓库里（引擎源码在仓库外，被 .gitignore
    # 排除，只能靠补丁复现）。脚本只负责 apply，不负责「知道要改什么」。
    pdir = os.path.join(ROOT, "tools", "voice_engine_patches")
    chk(os.path.isdir(pdir), "补丁目录存在")
    for tag in ("onnxruntime-lazy", "language-filter", "is-g2pw-env",
                "bigvgan-lazy"):
        f = os.path.join(pdir, tag + ".py")
        chk(os.path.isfile(f), "补丁文件存在：%s.py" % tag)
        if os.path.isfile(f):
            chk(os.path.getsize(f) > 500, "%s.py 非空（%d 字节）"
                % (tag, os.path.getsize(f)))
    ap = os.path.join(ROOT, "tools", "apply_voice_patches.py")
    chk(os.path.isfile(ap), "补丁应用脚本存在")
    if os.path.isfile(ap):
        a = io.open(ap, encoding="utf-8").read()
        for kw in ("_get_onnxruntime", "_available_language_modules",
                   "_import_bigvgan", "MUPPET_VOICE_PATCH"):
            chk(kw in a, "apply 脚本登记了补丁：%s" % kw)
    chk("apply_voice_patches.py" in s, "构建脚本调用补丁应用脚本")
    for tier in TIERS:
        chk('"%s"' % tier in s, "脚本支持档位：%s" % tier)

print()
print("== 下载器接线")
import asset_downloader as AD
chk(hasattr(AD.AssetDownloadThread, "_run_voice_engine"),
    "下载器有 voice_engine 模式")
s = io.open(os.path.join(ROOT, "asset_downloader.py"), encoding="utf-8").read()
chk('"voice_engine"' in s, "run() 里分发到 voice_engine")

print()
print("== 既有资产没被影响")
for aid in ("runtime", "model_layerdiff", "model_marigold", "model_sam",
            "cuda_pack", "webengine_pack", "webgal_runtime", "voice_basemodels"):
    a = AC.asset_by_id(aid)
    chk(a is not None, "既有资产 %s 仍在" % aid)
    if aid == "cuda_pack":
        chk(a.get("target") == "site-packages", "CUDA 包落点语义没变")

shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(TMP2, ignore_errors=True)
print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("语音引擎分档全部检查通过")