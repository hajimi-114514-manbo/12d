# -*- coding: utf-8 -*-
"""运行时文件迁移到 .runtime/ 后的回归验证。

重点：
  · 新写入落到 .runtime/
  · 旧位置留文件时仍能读（向后兼容，不能丢用户数据）
  · 两个模块（主模块 / video_workbench）用的是同一个文件
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = r"D:\work\puppet\muppet_studio"
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

fails = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        fails.append(m)


print("=== 1. 模块能 import（路径函数没写坏）===")
import l2d_rig_qt as M
chk(hasattr(M, "_runtime_file"), "_runtime_file 存在")
chk(hasattr(M, "_runtime_read_path"), "_runtime_read_path 存在")

print("\n=== 2. 写入路径指向 .runtime/ ===")
chk(".runtime" in M.RECENT_FILE.replace("\\", "/"),
    "RECENT_FILE 在 .runtime/ 下: %s" % M.RECENT_FILE)
chk(".runtime" in M.IMPORT_GDIR_FILE.replace("\\", "/"),
    "IMPORT_GDIR_FILE 在 .runtime/ 下: %s" % M.IMPORT_GDIR_FILE)

print("\n=== 3. 读取能找回已迁移的数据（不能丢）===")
disk = os.path.join(ROOT, ".runtime", "recent_projects.json")
chk(os.path.isfile(disk), ".runtime/recent_projects.json 存在")
readp = M._runtime_read_path("recent_projects.json")
chk(readp is not None and ".runtime" in readp.replace("\\", "/"),
    "读路径也指向 .runtime（新位置优先）")

print("\n=== 4. load_recent 能读到内容 ===")
items = M.load_recent()
print("  读到 %d 条" % len(items))
chk(isinstance(items, list), "load_recent 返回 list")

print("\n=== 5. 保存后能读回（闭环）===")
orig = M.save_recent
test_items = [{"dir": ROOT, "name": "projects", "type": "video"}]
backup = None
if os.path.isfile(disk):
    backup = open(disk, encoding="utf8").read()
try:
    M.save_recent(test_items)
    got = json.load(open(disk, encoding="utf8"))
    chk(len(got) == 1 and got[0]["name"] == "projects",
        "save_recent 写入 .runtime/ 且能读回")
finally:
    if backup is not None:
        open(disk, "w", encoding="utf8").write(backup)
    M.save_recent = orig

print("\n=== 6. 向后兼容：旧位置有文件、新位置没有时能读 ===")
tmp = tempfile.mkdtemp(prefix="runtime_compat_")
try:
    old = os.path.join(tmp, "recent_projects.json")
    with open(old, "w", encoding="utf8") as f:
        json.dump([{"dir": tmp, "name": "OLD", "type": "video"}], f)
    # 临时把 V2 指到 tmp 验证回退逻辑
    orig_v2 = M.V2
    M.V2 = tmp
    try:
        p = M._runtime_read_path("recent_projects.json")
        chk(p == old, "新位置没有时回退读旧位置（%s）" % os.path.basename(p or ""))
        w = M._runtime_file("recent_projects.json")
        chk(".runtime" in w.replace("\\", "/") or w == old,
            "写入优先新位置（%s）" % w)
    finally:
        M.V2 = orig_v2
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n=== 7. video_workbench 与主模块指向同一文件 ===")
import video_workbench as VW
a = os.path.normpath(M.RECENT_FILE).lower()
b = os.path.normpath(VW.RECENT_FILE).lower()
chk(a == b, "两处 RECENT_FILE 一致:\n        主模块 %s\n        vw    %s" % (a, b))

print("\n=== 8. 根目录已不再有这三个文件 ===")
for n in ("recent_projects.json", "import_game_dir.json", "launcher.log"):
    chk(not os.path.isfile(os.path.join(ROOT, n)),
        "根目录无 %s" % n)

print("\n" + "=" * 58)
if fails:
    print("FAIL %d 项" % len(fails))
    sys.exit(1)
print("PASS 运行时文件迁移验证通过")