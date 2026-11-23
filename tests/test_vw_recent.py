# -*- coding: utf-8 -*-
"""video_workbench._record_recent：写入前剔除失效条目。"""
import json
import os
import shutil
import sys
import tempfile

# ---- 测试引导：让 tests/ 里的脚本能 import 到项目根的同级模块 ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)          # tests/ 的上一级 = 项目根
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

fails = []


def chk(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c:
        fails.append(m)


work = tempfile.mkdtemp(prefix="vw_recent_")
recent = os.path.join(work, "recent_projects.json")
old_dead = os.path.join(work, "old_dead")
old_alive = os.path.join(work, "old_alive")
new_proj = os.path.join(work, "new_proj")
for d in (old_dead, old_alive, new_proj):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "video_project.json"), "w", encoding="utf8") as f:
        f.write("{}")
shutil.rmtree(old_dead)

with open(recent, "w", encoding="utf8") as f:
    json.dump([
        {"dir": old_dead, "name": "old_dead", "type": "video"},
        {"dir": old_alive, "name": "old_alive", "type": "video"},
    ], f, ensure_ascii=False, indent=2)

import video_workbench as VW
VW.RECENT_FILE = recent

# 造一个最小宿主对象（只测这一个方法，不需要真窗口）
class Fake:
    proj_dir = new_proj

Fake._record_recent = VW.VideoWorkbench._record_recent
f = Fake()
f._record_recent()

data = json.load(open(recent, encoding="utf8"))
names = [x["name"] for x in data]
print("  写入后:", names)

chk(data[0]["name"] == "new_proj", "新工程置顶")
chk("old_dead" not in names, "失效条目被剔除")
chk("old_alive" in names, "活条目保留")
chk(len(data) == 2, "共 2 条（实际 %d）" % len(data))

# 重复记录同一个工程不应产生重复
f._record_recent()
data2 = json.load(open(recent, encoding="utf8"))
chk(len(data2) == 2, "重复记录不产生重复项（实际 %d）" % len(data2))

shutil.rmtree(work, ignore_errors=True)
print("\n" + "=" * 50)
if fails:
    print("FAIL %d" % len(fails))
    sys.exit(1)
print("PASS video_workbench 最近工程清理通过")