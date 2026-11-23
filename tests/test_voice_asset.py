# -*- coding: utf-8 -*-
"""语音底模资产条目的静态验证（不真下 1.4GB）。"""
import io
import os
import sys

# ---- 测试引导：向上找项目根（与 tests/ 里既有测试一致）----
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)          # tests/ 的上一级 = 项目根
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT)

# 隔离：引擎根指向临时目录，别污染真实用户数据目录
TMP = os.path.join(os.environ.get("TEMP", "."), "muppet_voice_test")
os.environ["MUPPET_VOICE_DIR"] = TMP

import asset_catalog as AC
import voice_paths as VP

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


print("=== voice_paths 基础")
chk(VP.voice_dir() == TMP, "env 硬限定生效：%s" % VP.voice_dir())
chk(not VP.available(), "没有引擎时 available()=False")
d = VP.describe()
chk(d["env_locked"] is True, "describe 报 env_locked")
chk(isinstance(d["candidates"], list) and d["candidates"], "候选列表非空")

print()
print("=== 资产条目")
a = AC.asset_by_id("voice_basemodels")
chk(a is not None, "条目存在")
if a:
    print("     name:", a["name"])
    print("     size: %.0f MB" % (a["size"] / 1048576.0))
    print("     dest:", a["dest"])
    print("     root:", a.get("root"))
    chk(a["repo"] == "lj1995/GPT-SoVITS", "repo=官方仓库")
    chk(len(a["files"]) == 10, "10 个文件：%d" % len(a["files"]))
    chk(set(a["sha256"].keys()) == set(a["files"]),
        "sha256 覆盖全部文件")
    chk(all(len(v) == 64 for v in a["sha256"].values()), "sha256 长度合法")

    print()
    print("=== 落点解析（root=voice 语义）")
    p = AC.installed_path(a, ROOT)
    print("     installed_path =", p)
    chk(p == os.path.join(TMP, "GPT_SoVITS", "pretrained_models"),
        "落在引擎的 pretrained_models 下")
    chk(os.path.join(TMP, "GPT_SoVITS", "pretrained_models")
        in p.replace("/", os.sep), "路径层级正确")

    print()
    print("=== URL 生成（逐文件）")
    plan = AC.asset_urls(a)
    chk(len(plan) == 10, "10 条下载计划：%d" % len(plan))
    for rel, urls in plan[:3]:
        print("     %-52s %s" % (rel, urls[0]))
    chk(all(u.startswith("https://hf-mirror.com/lj1995/GPT-SoVITS/resolve/main/")
            for _r, us in plan for u in us[:1]), "URL 指向 hf-mirror 官方仓库")
    chk(all(len(us) == 2 for _r, us in plan), "每条有镜像+官方两个候选")

    print()
    print("=== 已装判定")
    chk(AC.is_installed(a, ROOT) is False, "未装 -> False")
    # 造一个假目录冒充装好
    fake = os.path.join(TMP, "GPT_SoVITS", "pretrained_models")
    os.makedirs(fake, exist_ok=True)
    with open(os.path.join(fake, "s1v3.ckpt"), "wb") as f:
        f.write(b"x" * 150000000)
    chk(AC.is_installed(a, ROOT) is True, "marker 到位 -> True")
    present, missing = AC.asset_files_missing(a, ROOT)
    chk(len(present) == 1 and len(missing) == 9,
        "files_missing 正确：present=%d missing=%d" % (len(present), len(missing)))
    chk(AC.installed_size(a, ROOT) > 100000000, "installed_size 正确")

    print()
    print("=== 不影响既有资产")
    for aid in ("runtime", "model_layerdiff", "model_marigold", "model_sam",
                "cuda_pack", "webengine_pack", "webgal_runtime"):
        x = AC.asset_by_id(aid)
        chk(x is not None, "既有资产 %s 仍在" % aid)
        if aid != "voice_basemodels":
            chk("root" not in x, "%s 未被误加 root" % aid)
            chk(AC.installed_path(x, ROOT).startswith(ROOT),
                "%s 仍落在程序目录" % aid)

print()
print("=== 清理")
import shutil
shutil.rmtree(TMP, ignore_errors=True)

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("语音底模资产条目验证通过")