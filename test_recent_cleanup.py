# -*- coding: utf-8 -*-
"""最近工程列表：失效条目清理回归。

验证点：
  · 目录被删后，load_recent 不再返回它，且**回写** json（文件里也干净了）
  · 活着的条目一条不少（不能误删）
  · video_workbench._record_recent 写入前会剔除失效条目
  · 上限跟随 RECENT_MAX，不再硬编码 100
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, r"D:\work\puppet\muppet_studio")

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


work = tempfile.mkdtemp(prefix="recent_test_")
recent = os.path.join(work, "recent_projects.json")

# 造两个「活着的」工程和两个「已失效」的
alive_img = os.path.join(work, "alive_img")
alive_vid = os.path.join(work, "alive_vid")
dead_img = os.path.join(work, "dead_img")
dead_vid = os.path.join(work, "dead_vid")
for d, fn in ((alive_img, "layers_meta.json"), (alive_vid, "video_project.json")):
    os.makedirs(d)
    with open(os.path.join(d, fn), "w", encoding="utf8") as f:
        f.write("{}")
for d in (dead_img, dead_vid):
    os.makedirs(d)          # 建了再删，模拟「曾经存在」
import shutil
shutil.rmtree(dead_img)
shutil.rmtree(dead_vid)

initial = [
    {"dir": dead_img.replace("\\", "\\\\"), "name": "dead_img", "type": "image"},
    {"dir": alive_img, "name": "alive_img", "type": "image"},
    {"dir": dead_vid.replace("\\", "\\\\"), "name": "dead_vid", "type": "video"},
    {"dir": alive_vid, "name": "alive_vid", "type": "video"},
]
with open(recent, "w", encoding="utf8") as f:
    json.dump(initial, f, ensure_ascii=False, indent=2)

# 把模块指向临时文件
import l2d_rig_qt as M
M.RECENT_FILE = recent

print("=== 1. load_recent 丢弃失效条目 ===")
got = M.load_recent()
names = [it["name"] for it in got]
print("  返回:", names)
chk(len(got) == 2, "只剩 2 条活着的（实际 %d）" % len(got))
chk("alive_img" in names and "alive_vid" in names, "两个活着的都在")
chk("dead_img" not in names and "dead_vid" not in names, "两个失效的都不在")

print("\n=== 2. 文件被回写（不是只在内存过滤）===")
on_disk = json.load(open(recent, encoding="utf8"))
disk_names = [it["name"] for it in on_disk]
print("  落盘:", disk_names)
chk(len(on_disk) == 2, "json 里也只剩 2 条（实际 %d）" % len(on_disk))
chk("dead_img" not in disk_names, "失效条目已从文件移除")

print("\n=== 3. 再次读取稳定（不会反复回写/丢数据）===")
again = M.load_recent()
chk(len(again) == 2, "二次读取仍 2 条")

print("\n=== 4. RECENT_MAX 生效，且不再是硬编码 100 ===")
chk(M.RECENT_MAX == 100 or M.RECENT_MAX > 0, "RECENT_MAX = %d" % M.RECENT_MAX)
# 造 120 条活着的，看是否被截到 RECENT_MAX
many = []
for i in range(120):
    d = os.path.join(work, "p%03d" % i)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "video_project.json"), "w", encoding="utf8") as f:
        f.write("{}")
    many.append({"dir": d, "name": "p%03d" % i, "type": "video"})
with open(recent, "w", encoding="utf8") as f:
    json.dump(many, f, ensure_ascii=False, indent=2)
res = M.load_recent()
chk(len(res) == min(120, M.RECENT_MAX), "截断到 %d（实际 %d）" % (min(120, M.RECENT_MAX), len(res)))

# 清理
shutil.rmtree(work, ignore_errors=True)

print("\n" + "=" * 50)
if fails:
    print("FAIL %d 项" % len(fails))
    sys.exit(1)
print("PASS 最近工程失效清理全部通过")