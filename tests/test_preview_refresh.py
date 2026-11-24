# -*- coding: utf-8 -*-
"""实时预览刷新的回归测试（用户报「要按空格才刷新」）。

根因（v3.6.3 修）：apply_state 原来是**纯防抖** —— 每次调用都
`self._deb.start()` 重置 80ms 单次定时器。而播放时 `_advance` 每帧
（1000/fps ≈ 33ms）调一次，33ms < 80ms -> 定时器被无限重启、永远
不到期 -> 播放期间实时预览一帧都不动，只有停播放（按空格）之后
80ms 才补推一次。

覆盖：
  · 播放中：每帧都落地（不节流）—— 旧代码这里是 0 次
  · 拖动 scrub：被节流，定时器**只挂一次不重置**（旧代码每帧重置）
  · 尾沿一定会补一帧最新状态（真 QTimer + 事件循环）
  · force=True 立刻落地
  · 降级后零流量
"""
import os
import sys
import time as _real_time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


import webgal_preview as W

if not getattr(W, "HAS_WEBENGINE", False):
    print("  SKIP 没有 WebEngine，跳过实时预览测试")
    raise SystemExit(0)

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

APP = QApplication.instance() or QApplication([])

View = W.WebPreviewView
PROJ = {"fps": 30}


class Clock:
    """可手动推进的假时钟（替掉模块里的 time.perf_counter）。

    起点取 1000s 而不是 0：真实 perf_counter 是开机以来的秒数，
    绝不会是 0。起点给 0 会让「首次调用」看起来才刚落地过。
    """

    def __init__(self):
        self.t = 1000.0

    def perf_counter(self):
        return self.t

    def advance(self, ms):
        self.t += ms / 1000.0


class FakeTimer:
    """记录 start() 次数的假定时器 —— 「有没有被反复重置」靠它精确判定。

    真 QTimer 的 remainingTime() 走的是真实时间，在假时钟下不可比。
    """

    def __init__(self):
        self.starts = 0
        self.active = False
        self.single = False
        self._rem = -1

    def setSingleShot(self, b):
        self.single = bool(b)

    def setInterval(self, ms):
        self._rem = int(ms)

    def start(self, ms=None):
        self.starts += 1
        self.active = True
        if ms is not None:
            self._rem = int(ms)

    def stop(self):
        self.active = False
        self._rem = -1

    def isActive(self):
        return self.active

    def remainingTime(self):
        return self._rem if self.active else -1


def make_view(fake_timer=True, interval=None):
    """不走 __init__（那要真的建 QWebEngineView），只装配节流要用的字段。

    QTimer 不挂父对象：`View.__new__` 出来的对象 Qt 基类没初始化，
    给 QTimer 传它会报 libshiboken 错。
    """
    v = View.__new__(View)
    v._degraded = None
    v._pending_apply = None
    v._last_apply_ms = 0.0
    v.DEBOUNCE_MS = View.DEBOUNCE_MS if interval is None else interval
    if fake_timer:
        v._deb = FakeTimer()
        v._deb.setSingleShot(True)
        v._deb.setInterval(v.DEBOUNCE_MS)
    else:
        v._deb = QTimer()
        v._deb.setSingleShot(True)
        v._deb.setInterval(v.DEBOUNCE_MS)
        v._deb.timeout.connect(v._flush_apply)
    v.calls = []

    def fake_do_apply(proj, t):
        v.calls.append(t)

    v._do_apply = fake_do_apply
    return v


# ============ 1. 播放中：每帧都要落地 ============
print("=== 1. 播放中（playing=True）不节流")
clock = Clock()
_saved = W.time
W.time = clock
try:
    v = make_view()
    fps = 30.0
    step = 1000.0 / fps
    for i in range(30):                     # 30 帧 = 1 秒
        v.apply_state(PROJ, i / fps, playing=True)
        clock.advance(step)
    chk(len(v.calls) == 30,
        "30 帧全部落地（%d 次；旧代码是 0 次 = 画面冻住）" % len(v.calls))
    chk(abs(v.calls[-1] - 29 / fps) < 1e-9, "最后一帧时间是 29/30s")
    chk(v._deb.starts == 0, "播放中从不挂尾沿定时器")

    # ============ 2. 拖动 scrub：节流且不重置定时器 ============
    print()
    print("=== 2. 拖动时间轴（playing=False）要节流")
    v2 = make_view()
    clock2 = Clock()
    W.time = clock2
    v2.apply_state(PROJ, 0.0)               # 首次：没有历史 -> 立刻落地
    chk(len(v2.calls) == 1, "首次调用立刻落地（前沿，%d 次）" % len(v2.calls))
    chk(not v2._deb.isActive(), "已经落地就不必再挂定时器")
    clock2.advance(10)
    v2.apply_state(PROJ, 0.01)              # 间隔不足 -> 挂尾沿
    chk(v2._deb.isActive(), "间隔不足时才挂尾沿定时器")
    for i in range(2, 21):                  # 连拖 20 次，每次 10ms
        clock2.advance(10)
        v2.apply_state(PROJ, i * 0.01)
    # 200ms / 80ms = 每 8 次调用才落地一次，共 3 次（首 + 第 8 + 第 16）
    chk(len(v2.calls) == 3,
        "20 次拖动只落地 %d 次（80ms 一次）" % len(v2.calls))
    chk(v2._deb.starts == len(v2.calls),
        "定时器只为「每次真正落地之后」重挂一次（start %d 次 / 20 次调用）"
        % v2._deb.starts)
    chk(v2._deb.starts < 20, "没有每次调用都重置（旧代码 = 20 次）")
    chk(v2._pending_apply is not None and
        abs(v2._pending_apply[1] - 20 * 0.01) < 1e-9,
        "待推的是最新那份（t=%.2f，不丢帧）" % v2._pending_apply[1])

    # ============ 3. 尾沿真的会补一帧（真时钟 + 事件循环）============
    print()
    print("=== 3. 尾沿补帧（真 QTimer）")
    W.time = _real_time
    v3 = make_view(fake_timer=False, interval=40)
    v3._last_apply_ms = _real_time.perf_counter()   # 让它先走尾沿
    v3.apply_state(PROJ, 1.0)
    chk(not v3.calls, "间隔不足 -> 先不落地")
    t0 = _real_time.perf_counter()
    while _real_time.perf_counter() - t0 < 0.50 and not v3.calls:
        APP.processEvents()
        _real_time.sleep(0.005)
    chk(len(v3.calls) == 1, "尾沿 40ms 后补上了（%d 次）" % len(v3.calls))
    chk(v3.calls and abs(v3.calls[0] - 1.0) < 1e-9, "补的是最新那份 t")

    # ============ 4. force=True 立刻落地 ============
    print()
    print("=== 4. force=True 跳过节流")
    v4 = make_view()
    clock4 = Clock()
    W.time = clock4
    v4.apply_state(PROJ, 0.0, force=True)
    v4.apply_state(PROJ, 0.1, force=True)
    v4.apply_state(PROJ, 0.2, force=True)
    chk(len(v4.calls) == 3, "连续 force 全部立刻落地（%d 次）" % len(v4.calls))
    chk(not v4._deb.isActive(), "force 不留定时器")

    # ============ 5. 降级后零流量 ============
    print()
    print("=== 5. 降级后不发")
    v5 = make_view()
    v5._degraded = "no_runtime"
    v5.apply_state(PROJ, 0.0, playing=True)
    chk(not v5.calls, "降级后一次都不发")

    # ============ 6. 播放/暂停来回切：播放帧一帧不丢 ============
    print()
    print("=== 6. 播放中按空格来回切（用户的复现路径）")
    v6 = make_view()
    clock6 = Clock()
    W.time = clock6
    n = 0
    for _ in range(3):                      # 播 10 帧 -> 停 -> 播 10 帧
        for _ in range(10):
            v6.apply_state(PROJ, n / 30.0, playing=True)
            clock6.advance(1000.0 / 30.0)
            n += 1
        v6.apply_state(PROJ, n / 30.0, playing=False)   # 空格暂停
    # 播放帧每帧必落地（30 次）；暂停那次距上帧才 33ms，走尾沿，
    # 真实环境里 80ms 内补上（见测试 3）——假定时器不会自己到期。
    chk(len(v6.calls) == n,
        "播放的 %d 帧全部落地（实际 %d）" % (n, len(v6.calls)))
    chk(v6._pending_apply is not None and
        abs(v6._pending_apply[1] - n / 30.0) < 1e-9,
        "暂停帧挂在待推里（t=%.3f），尾沿会补" % (n / 30.0))
finally:
    W.time = _saved

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("实时预览刷新：全部通过")