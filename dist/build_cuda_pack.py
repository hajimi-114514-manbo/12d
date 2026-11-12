# -*- coding: utf-8 -*-
"""CUDA 加速包打包：本机 torch(cu130)+bitsandbytes -> zip -> 3 分卷 + sha256。

zip 内部结构（对齐 asset_catalog cuda_pack 的 target=site-packages）：
    torch/...            （CUDA 版整目录）
    bitsandbytes/...
解压即覆盖 site-packages 对应目录（pre_delete 已先删旧 CPU 版）。
排除 __pycache__/*.pyc（省 ~300MB 且无运行时价值）。
"""
import hashlib
import os
import zipfile

SP = r"D:\Python312\Lib\site-packages"
OUT_DIR = r"D:\build\cuda_pack"
PKGS = ["torch", "bitsandbytes"]
ZIP = os.path.join(OUT_DIR, "cuda_pack.zip")
N_PARTS = 3

os.makedirs(OUT_DIR, exist_ok=True)

print("[1/4] zip（deflate level 1，排除 pycache/ptx/tests）…", flush=True)
n = skipped = 0
with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
    for pkg in PKGS:
        root = os.path.join(SP, pkg)
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith((".pyc", ".ptx")):
                    skipped += 1
                    continue
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, SP).replace(os.sep, "/")
                try:
                    z.write(full, rel)
                    n += 1
                except Exception as e:
                    print("  跳过:", rel, repr(e))
print("  写入 %d 文件，跳过 %d" % (n, skipped), flush=True)
size = os.path.getsize(ZIP)
print("[2/4] zip %d 字节 (%.2f GB)" % (size, size / 2 ** 30), flush=True)

h = hashlib.sha256()
with open(ZIP, "rb") as f:
    while True:
        b = f.read(8 * 1024 * 1024)
        if not b:
            break
        h.update(b)
sha = h.hexdigest()
print("  sha256:", sha, flush=True)

print("[3/4] 切 %d 分卷…" % N_PARTS, flush=True)
step = (size + N_PARTS - 1) // N_PARTS
parts = []
with open(ZIP, "rb") as f:
    for i in range(N_PARTS):
        p = os.path.join(OUT_DIR, "cuda_pack.zip.part%02d" % (i + 1))
        remaining = step
        with open(p, "wb") as g:
            while remaining > 0:
                b = f.read(min(8 * 1024 * 1024, remaining))
                if not b:
                    break
                g.write(b)
                remaining -= len(b)
        if os.path.getsize(p):
            parts.append((p, os.path.getsize(p)))
for p, s in parts:
    print("  %s  %.2f GB" % (os.path.basename(p), s / 2 ** 30), flush=True)

print("[4/4] 回填数据（贴进 asset_catalog）:", flush=True)
print("SIZE=%d" % size)
print("SHA256=%s" % sha)
for i, (p, s) in enumerate(parts):
    print("PART%02d=%d" % (i + 1, s))
