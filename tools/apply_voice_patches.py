# -*- coding: utf-8 -*-
"""把语音引擎的补丁应用到引擎源码上。

用法：
    python tools/apply_voice_patches.py [--engine D:\\gsv_slim\\work]

幂等：已打过补丁的文件会跳过（靠 marker 判定）。
"""
import argparse
import io
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATCH_DIR = os.path.join(HERE, "voice_engine_patches")
DEFAULT_ENGINE = os.environ.get("MUPPET_VOICE_SRC", r"D:\gsv_slim\work")

# (补丁文件名, 引擎内相对路径, 已打补丁的标记)
PATCHES = [
    ("onnxruntime-lazy.py", "GPT_SoVITS/text/g2pw/onnx_api.py",
     "_get_onnxruntime"),
    ("language-filter.py", "GPT_SoVITS/text/cleaner.py",
     "_available_language_modules"),
    ("is-g2pw-env.py", "GPT_SoVITS/text/chinese2.py",
     "MUPPET_VOICE_PATCH"),
    ("bigvgan-lazy.py", "GPT_SoVITS/TTS_infer_pack/TTS.py",
     "_import_bigvgan"),
]


def apply_one(patch_name, rel, marker, engine, dry=False):
    src = os.path.join(PATCH_DIR, patch_name)
    dst = os.path.join(engine, rel.replace("/", os.sep))
    if not os.path.isfile(src):
        print("   !! 补丁文件缺失：%s" % src)
        return False
    if not os.path.isfile(dst):
        print("   !! 引擎文件缺失：%s" % dst)
        return False
    new = io.open(src, encoding="utf-8", errors="replace").read()
    cur = io.open(dst, encoding="utf-8", errors="replace").read()
    if marker in cur:
        print("   已打过：%s" % rel)
        return True
    if dry:
        print("   [dry] 会改：%s" % rel)
        return True
    # 备份原文件（首次）
    bak = dst + ".orig"
    if not os.path.isfile(bak):
        shutil.copy2(dst, bak)
    io.open(dst, "w", encoding="utf-8", newline="\n").write(new)
    print("   已打补丁：%s（原文件备份为 %s）"
          % (rel, os.path.basename(bak)))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=DEFAULT_ENGINE)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    print("=== 引擎：%s" % a.engine)
    ok = True
    for name, rel, marker in PATCHES:
        if not apply_one(name, rel, marker, a.engine, a.dry):
            ok = False
    print()
    print("结果：%s" % ("全部已应用" if ok else "有缺失，见上面的 !! 行"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
