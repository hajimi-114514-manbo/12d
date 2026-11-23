# -*- coding: utf-8 -*-
"""clip.add 的 layout 解析测试（两条路都要生效）。

背景：原来 scale 只在「散着给」时生效，走 layout dict 时漏了 scale 会被
忽略 —— 两条路语义不一致，调用方会以为「传了就管用」。
"""
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT)

import remote_control as RC
import video_model as V

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


TMP = tempfile.mkdtemp(prefix="clip_layout_")
IMG = os.path.join(TMP, "x.png")
# clip.add 会校验素材真实存在 -> 造个真文件（1x1 png）
import struct
import zlib
def _png(path):
    w = h = 1
    raw = b"\x00" + b"\xff\x00\x00" * w
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    open(path, "wb").write(png)
_png(IMG)


class FakeWB:
    """最小工作台替身：clip.add 只用到 proj 和 push_history。"""

    def __init__(self):
        self.proj = {"type": "video_edit", "version": 1, "width": 1920,
                     "height": 1080, "fps": 30,
                     "tracks": [{"id": "v1", "kind": "video",
                                 "name": "视频轨 1", "clips": []}],
                     "dialogue": [], "acts": [], "comments": []}

    def push_history(self):
        pass

    def _after_edit(self):
        """clip.add 末尾会调它（工作台里是刷新 UI）。替身里空实现即可。"""

    def render_frame(self, t):
        pass


def add_clip(args):
    wb = FakeWB()
    ok, msg, data = RC.run_command(wb, "clip.add", args)
    clips = wb.proj["tracks"][0]["clips"]
    return ok, msg, (clips[0] if clips else None)


print("== ① 整个 layout dict")
ok, msg, c = add_clip({"kind": "image", "path": IMG,
                        "layout": {"x": 0.3, "y": 0.7, "scale": 1.5}})
chk(ok, "添加成功：%s" % msg)
if c:
    lay = c["layout"]
    chk(lay["x"] == 0.3, "x=%s" % lay.get("x"))
    chk(lay["y"] == 0.7, "y=%s" % lay.get("y"))
    chk(lay["scale"] == 1.5, "scale=%s（dict 里的 scale 要生效）" % lay.get("scale"))

print()
print("== ② 散着给 x/y/scale")
ok, msg, c = add_clip({"kind": "image", "path": IMG,
                        "x": 0.2, "y": 0.4, "scale": 2.0})
chk(ok, "添加成功")
if c:
    lay = c["layout"]
    chk(lay["x"] == 0.2 and lay["y"] == 0.4, "x/y 生效：%s" % lay)
    chk(lay["scale"] == 2.0, "scale=%s（散着给的 scale 要生效）" % lay.get("scale"))

print()
print("== ③ 两条路混用：dict 优先，散着补缺")
ok, msg, c = add_clip({"kind": "image", "path": IMG,
                        "layout": {"x": 0.1}, "y": 0.9, "scale": 3.0})
chk(ok, "添加成功")
if c:
    lay = c["layout"]
    chk(lay["x"] == 0.1, "dict 的 x 生效：%s" % lay.get("x"))
    chk(lay["y"] == 0.9, "散着的 y 补上：%s" % lay.get("y"))
    chk(lay["scale"] == 3.0, "散着的 scale 补上：%s" % lay.get("scale"))

print()
print("== ④ 都不给 -> 用默认值")
ok, msg, c = add_clip({"kind": "image", "path": IMG})
chk(ok, "添加成功")
if c:
    d = V.default_layout()
    chk(c["layout"] == d, "默认 layout：%s" % c["layout"])

print()
print("== ⑤ rot 也能散着给")
ok, msg, c = add_clip({"kind": "image", "path": IMG, "rot": 15.0})
chk(ok, "添加成功")
if c:
    chk(c["layout"]["rot"] == 15.0, "rot=%s" % c["layout"].get("rot"))

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
shutil.rmtree(TMP, ignore_errors=True)
print("clip.add 的 layout 解析测试通过")