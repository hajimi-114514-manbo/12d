# -*- coding: utf-8 -*-
"""L2D 逐帧动画主线程渲染泵（方案 A）。

**为什么必须是"主线程泵 + 导出线程等"**：QWebEngine 的 JS 往返与渲染只能
跑在 GUI 主线程的事件循环里（在导出 QThread 里直接渲染实测死锁：
`QObject::startTimer: current thread's event dispatcher has already been
destroyed`）。v3.5.11 的方案 B 因此改成"导出前主线程预渲染每个状态一张
定格图"，代价是同一 clip 内画面不动（没有呼吸/眨眼）。

本模块把 B 升级成 A：导出线程按帧向泵**取图**，泵在主线程事件循环里
被 QTimer 驱动，逐步推进宿主页的手动时钟（stage_preview.html 的
`clockAPI.seek`）并抓一帧回来。用户拍板"动画优先"，接受导出变慢。

三条设计要点（都有实测依据）：

1. **相位网格固定，第 k 帧永远由"从 0 起走 k 步"得到**
   宿主页的 seek 只向前推进；回拖（乱序线程撞到更小的 k）才重启动作
   从 0 重走。这样一帧的像素只取决于 k，与线程到达顺序无关 => 同一次
   导出内可复现，跨次导出也复现（前提是页面步进本身确定，见
   l2d_animation_test 的确定性断言）。

2. **每 tick 有时间预算（默认 12ms）**，用完就把事件循环还给 GUI。
   一次服务多个请求比"一 tick 一请求"吞吐高得多，但独占主线程会让
   进度条/窗口失去响应，所以按预算切片。

3. **任何异常/超时都返回 None**，调用方（PuppetSource）回退到预渲染
   定格图或占位卡。导出**永不**因为渲染器卡死——宁可掉回定格。
"""
import os
import queue
import sys
import threading
import time

import numpy as np
import contextlib

_V_DIR = os.path.dirname(os.path.abspath(__file__))
if _V_DIR not in sys.path:
    sys.path.insert(0, _V_DIR)

# 每 tick 最多占用主线程多少毫秒（超出留到下个 tick）
#
# v3.7.5 实测：孤立泵测试显示 12 -> 24ms 能让吞吐 +39%（46.5 -> 64.7 帧/秒），
# 但**真实导出里没有收益**（20s 切片交替 A/B 4 轮：中位 11.61s vs 11.79s，
# 在噪声内）。原因：孤立测试没有导出线程抢 GIL，真实导出会。
# 结论：保持原值 12ms —— 它的本意是「别让 GUI 长时间失去响应」，
# 而实测加大预算换不来速度，只会让导出期间界面更卡。
TICK_BUDGET_MS = 12.0
# 单次请求的等待上限（秒）：超时 -> None -> 调用方回落定格
REQUEST_TIMEOUT = 45.0
# 饥饿检查节拍：等这么久都没看到一次 timer tick，就判定主线程被调用方
# 占住（泵永远不会服务），本轮导出后续帧直接回落定格，见 request 注释
STARVE_CHECK_S = 3.0
# 每道（lane）保留的已渲染帧数：并行导出线程的时间窗 ≈2×workers 帧，
# 缓存住这一段就能纯命中，不必回退重启重走。
RING_KEEP = 12


def _qt_app():
    try:
        from PySide6.QtWidgets import QApplication
        return QApplication.instance()
    except Exception:
        return None


class _Job(object):
    """一次取图请求（导出线程 -> 主线程）。"""
    __slots__ = ("lane", "k", "dt", "done", "img", "err")

    def __init__(self, lane, k):
        self.lane = lane
        self.k = k
        self.dt = 1000.0 / 30.0
        self.done = threading.Event()
        self.img = None
        self.err = ""


class Lane(object):
    """一路立绘（模型 + motion + expression 的组合）。

    head = 页面里该 slot 当前已推进到第几步；ring = k -> 图（近若干帧）。
    """

    def __init__(self, key, path, ref, motion, expression, mid,
                 lip=None, lip_gain=1.0, lip_tag=""):
        self.key = key                    # (normpath(model).lower(), motion, expression)
        self.path = path                  # **原始**路径（注册要用大小写原样，别用键里的小写）
        self.ref = ref
        self.motion = motion
        self.expression = expression
        self.mid = mid                    # 宿主页 slot id
        self.head = 0                     # 页面侧已推进到的步号
        self.grid = None                  # 生效的 dt_ms（网格变了必须从头走）
        self.roi = None                   # 固定裁剪框 (x,y,w,h) 画布坐标；
                                          # 首帧算出后整段沿用（省 5ms/帧 +
                                          # 消除 bbox 逐帧抖动）
        # v3.6.4 口型：**按帧变化的序列**，不能进 key（进了会让同一条 lane
        # 每帧都算"换了状态"、每帧重载模型）。所以单独挂在这里，渲染时按
        # k 取第 k 个值传下去。
        self.lip = lip                    # list[float] | None
        self.lip_gain = float(lip_gain or 1.0)
        self.lip_sent = None              # 已下发过的 lip 表指纹（省重复下传）
        # v3.7.3：表标签 = 该表所属 clip 的标识。同一条 lane 会被**同一立绘
        # 的多个 clip** 复用，而相邻台词的表可能等长同值（纯静音段就是全 0），
        # 只比序列判断不出"换台词了"。tag 变即视为换表，由渲染层强制重发。
        self.lip_tag = str(lip_tag or "")
        self.lip_ver = 0                  # 表版本：每换一次 +1（ring 据此作废）
        self.ring = {}                    # k -> ndarray
        self.order = []                   # ring 的插入序（做 FIFO 淘汰）
        self.last_err = ""
        self.renders = 0
        self.restarts = 0
        # **动作连续性**：request 检测到 motion 变化时置 True，
        # render_animated 读到后用 FORCE 切换（不重置相位、不重新加载模型）。
        self.motion_changed = False


class L2DAnimPump(object):
    """导出逐帧取图的主线程泵（进程级单例，见 `pump()`）。

    **所有权约定（关键）**：`start/stop/configure` 只能在 **GUI 主线程**
    调（QTimer 必须活在有事件循环的主线程；VideoExportWorker.run() 跑在
    QThread 里，那里既没有事件循环、也不能建 timer——v3.5.11 的
    `QObject::startTimer: 事件分发器已销毁` 死锁就是这么来的）。
    导出线程只调 `request()`，它只做入队 + 等 Event，不碰 Qt 对象。
    """

    def __init__(self):
        self._q = queue.Queue()
        self._lock = threading.RLock()
        self._lanes = {}
        self._timer = None
        self._busy = False
        self._renderer = None
        self._avail = None                # None=未探测, True/False
        self._unavailable_reason = ""
        self._active = False              # 有导出在跑才启 timer
        self._beats = 0                   # timer 心跳计数（饥饿检测用）
        self._starved = False             # 本轮泵饿死 -> 后续直接回落定格
        self.enabled = False              # 本轮导出是否走逐帧动画
        self.fps = 30.0                   # 相位网格 = 导出帧率
        self.ring_keep = RING_KEEP        # 每道缓存帧数（configure 按线程数放大）
        self.stats = {"requests": 0, "served": 0, "ring_hit": 0,
                      "restarted": 0, "failed": 0, "timeouts": 0,
                      "skipped": 0, "frames": 0}

    # ---------------- 配置（主线程） ----------------
    def configure(self, enabled, fps=30.0, workers=1):
        """本轮导出的模式、网格与线程数。enabled=False 时 request 直接返回
        None（调用方回落定格图），连入队都不做。

        ring 容量按线程数放大：并行导出是 FIFO 取任务、乱序完成，同一道
        的请求 k 大致递增但会小幅来回跳（窗口 ≈2×workers 帧）。ring 盖不住
        就要回拖重载（~0.7s/次，且画面只能靠重载保持可复现），所以宁可
        多留几帧。"""
        self.enabled = bool(enabled)
        try:
            f = float(fps)
        except (TypeError, ValueError):
            f = 30.0
        self.fps = min(120.0, max(1.0, f))
        try:
            nw = max(1, int(workers))
        except (TypeError, ValueError):
            nw = 1
        self.ring_keep = max(RING_KEEP, 4 * nw)
        return self.enabled

    # ---------------- 可用性 ----------------
    def available(self):
        """泵能不能用：要有 QApplication + runtime 就绪。不建窗口。"""
        if self._avail is not None:
            return self._avail
        ok = False
        why = ""
        if _qt_app() is None:
            why = "无 QApplication（非 GUI 进程）"
        else:
            try:
                import l2d_renderer as LR
                rd = LR.shared_renderer()
                if rd is None:
                    why = "渲染器创建失败"
                elif not rd.ready():
                    why = "runtime/live2d SDK 未就绪"
                else:
                    self._renderer = rd
                    ok = True
            except Exception as e:
                why = repr(e)
        self._avail = ok
        self._unavailable_reason = why
        return ok

    def reason(self):
        self.available()
        return self._unavailable_reason

    # ---------------- 生命周期 ----------------
    def start(self):
        """导出开始时调用（主线程）。装/启 QTimer。"""
        if not self.available():
            return False
        try:
            from PySide6.QtCore import QTimer
        except Exception:
            return False
        with self._lock:
            self._active = True
            self._starved = False      # 每轮导出重新判定，别带上轮的结论
            self._beats = 0
            self.stats = {"requests": 0, "served": 0, "ring_hit": 0,
                          "restarted": 0, "failed": 0, "timeouts": 0,
                          "skipped": 0, "starved": 0, "frames": 0}
            if self._renderer is not None and hasattr(self._renderer, "_reloads"):
                self._renderer._reloads = 0
            if self._timer is None:
                t = QTimer()
                t.setInterval(1)              # 1ms：预算内连续服务，超预算让位
                t.timeout.connect(self._tick)
                self._timer = t
            self._timer.start()
            try:
                self._renderer.set_manual(True)
                # 重载计数按"本轮导出"统计：它是累计值，不清零的话第二轮
                # 导出会带着上一轮的次数，测试里"零重载"的断言就失真了
                if hasattr(self._renderer, "_reloads"):
                    self._renderer._reloads = 0
            except Exception as e:
                self._unavailable_reason = "set_manual: %r" % (e,)
        return True

    def stop(self):
        """导出结束时调用（主线程）：停 timer，把页面交回墙上时钟。"""
        with self._lock:
            self._active = False
            if self._timer is not None:
                with contextlib.suppress(Exception): self._timer.stop()
            rd = self._renderer
        # 退出手动模式（预览/下次导出都从干净状态开始）
        if rd is not None:
            with contextlib.suppress(Exception): rd.set_manual(False)
        self._drop_all("导出结束")

    def abort(self):
        """取消导出：唤醒所有等待中的线程（不等 timer 慢慢清队列）。

        导出线程是 `job.done.wait(timeout)` 阻塞的，不叫醒它们就要空等
        45 秒才能退出——取消按钮会像卡死一样。"""
        while True:
            try:
                job = self._q.get_nowait()
            except queue.Empty:
                break
            job.err = "已取消"
            if not job.done.is_set():
                job.done.set()
        with self._lock:
            for ln in self._lanes.values():
                ln.ring.clear()
                ln.order.clear()
                ln.head = 0
                ln.grid = None

    # ---------------- 请求方（导出线程） ----------------
    @staticmethod
    def state_key(model_path, motion, expression):
        """与 l2d_prefetch 同口径的状态键（归一化小写路径，防重复渲染）。

        **动作连续性修复**：key 只按 model_path，不再包含 motion/expression。
        原来 motion 变了就换 lane、从 k=0 重走，导致导出时动作切换永远从头
        播放（预览已修，导出漏了）。现在同一模型只保留一条 lane，motion
        变化在 lane 内通过 FORCE 切换（见 request / render_animated）。
        """
        p = os.path.normpath(str(model_path or "")).lower()
        return (p,)

    def _bump(self, name, n=1):
        """计数器（多线程自增，走同一把锁；纯诊断用，丢了几个不影响正确性）。"""
        with self._lock:
            self.stats[name] = self.stats.get(name, 0) + n

    def k_for(self, t_local):
        """素材内时间 -> 相位步号（网格 = 1/self.fps 秒）。"""
        try:
            return max(0, int(round(float(t_local) * self.fps)))
        except (TypeError, ValueError):
            return 0

    def request(self, model_path, motion, expression, t_local, timeout=None,
                lip=None, lip_gain=1.0, lip_tag=""):
        """取该立绘在素材内时间 t_local 处的动画帧。

        返回 (H,W,4) RGBA uint8；不可用/关闭/超时 -> **None**（调用方回落
        定格图或占位卡）。这里绝不抛：导出线程里一个异常就整片失败，
        而「这一帧没动画」只是画质降级。

        v3.6.4：lip 是**按帧变化的开口度序列**（list[float]，帧序与
        t_local 同一网格），不进 lane key —— 它逐帧变，进 key 会让同一条
        lane 每帧都算换了状态、每帧重载模型。挂到 lane 上按 k 取即可。
        """
        if not self.enabled or not self._active or self._starved or not self.available():
            # `_active` 这条守卫很关键：泵没被主线程 start() 时（headless
            # 脚本/测试直接 VideoExportWorker.run()，或 GUI 还没回到事件
            # 循环），入队的 job 永远不会有人服务 —— 少了这个判断，每帧都
            # 要白等满 REQUEST_TIMEOUT 才回落定格，导出直接僵死。
            self._bump("skipped")
            return None
        timeout = REQUEST_TIMEOUT if timeout is None else float(timeout)
        k = self.k_for(t_local)
        dt_ms = 1000.0 / max(1.0, float(self.fps))
        key = self.state_key(model_path, motion, expression)
        with self._lock:
            ln = self._lanes.get(key)
            if ln is None:
                # slot id 用进程内自增序号：不用 hash(key)——那既随
                # PYTHONHASHSEED 变、又可能 20 bit 碰撞（两个不同立绘共用
                # 一个页面 slot 就会互相抢相位）。
                self._next_mid = getattr(self, "_next_mid", 0) + 1
                ln = self._lanes[key] = Lane(
                    key, str(model_path or ""), None,
                    str(motion or ""), str(expression or ""),
                    "anim%d" % self._next_mid)
            else:
                # **动作连续性**：同一模型只保留一条 lane。motion/expression
                # 变化时更新 lane 字段，**不创建新 lane**（原来会换 lane、
                # 从 k=0 重走，导致导出时动作切换永远从头播放）。
                # render_animated 里会检测变化并用 FORCE 切换。
                if str(motion or "") != ln.motion:
                    ln.motion = str(motion or "")
                    ln.motion_changed = True
                if str(expression or "") != ln.expression:
                    ln.expression = str(expression or "")
            # 口型表随请求带进来（同一 lane 不同台词的 lip 不同，要更新）
            tag = str(lip_tag or "")
            new_tbl = False
            if lip is not None:
                # ===== v3.7.3 换表检测 =====
                # 判据用「tag 变 / 长度变 / 首末值变」，任一成立即视为换台词：
                # 同一条 lane 上 c21->c39->c41 的表长分别是 912/420/1680，
                # tag 也各不相同，双保险。**换表必须让 ring 作废**，否则命中
                # 的旧帧是旧表渲出来的（嘴型对不上，还会掩盖真正的下发失败）。
                if (tag != ln.lip_tag) or (ln.lip is None) \
                        or (len(lip) != len(ln.lip)) \
                        or (lip and ln.lip and (lip[0] != ln.lip[0]
                                                or lip[-1] != ln.lip[-1])):
                    new_tbl = True
                    ln.lip_ver += 1
                    self._bump("lip_tbl")
                ln.lip = lip
                ln.lip_gain = float(lip_gain or 1.0)
                ln.lip_tag = tag
                if new_tbl:
                    ln.ring.clear()
                    ln.order = []
            elif tag != ln.lip_tag or ln.lip is not None:
                # ===== v3.7.3 换到「无台词」clip =====
                # lip=None 也要参与换表判定：c21->c22（无对白）时若不处理，
                # ln.lip 会一直挂着 c21 的表、ln.lip_tag 也不更新，页面继续
                # 按新 clip 内偏移去抽旧表 —— 越界后嘴定死，且 ring 还会
                # 一直命中旧帧（grid 未变、new_tbl=False）。
                # 这里把表显式置空并让 ring 作废，renderer 侧据 tag 变化重发
                # 一条空表（stage_preview.setLip 收到空 vals = 解除绑定）。
                new_tbl = True
                ln.lip_ver += 1
                self._bump("lip_none")
                ln.lip = None
                ln.lip_gain = 1.0
                ln.lip_tag = tag
                ln.ring.clear()
                ln.order = []
            # ring 命中：同 k 同网格就是同一张（下游只读，可安全共享）
            if ln.grid == dt_ms and not new_tbl:
                img = ln.ring.get(k)
                if img is not None:
                    self._bump("ring_hit")
                    return img
        job = _Job(ln, k)
        job.dt = dt_ms
        self._bump("requests")
        self._q.put(job)
        # **饥饿检测**（真死锁场景）：导出 worker 若在 GUI 主线程里被同步
        # 调用（bench 脚本/单测常这么写 `w.run()`），占住主线程的就是发起
        # 请求的这一串合成线程所在的调用栈，泵的 QTimer 永远不会 tick ——
        # 每个请求干等 45s 会把整次导出拖死。这里等 3s 看 `_beats` 有没有
        # 前进过：没前进就判定泵饿死，置 _starved，本轮导出后续帧直接回落
        # 定格（不再排队）。
        deadline = time.monotonic() + timeout
        beat0 = self._beats
        while not job.done.wait(STARVE_CHECK_S):
            if self._beats != beat0:
                beat0 = self._beats          # 泵活着，继续等（正常慢渲染）
                continue
            if time.monotonic() > deadline:
                break
            self._bump("starved")
            self._starved = True
            self._unavailable_reason = "主线程未回到事件循环（泵饿死）"
            return None
        if not job.done.is_set():
            self._bump("timeouts")
            job.err = "timeout"
            return None
        return job.img

    # ---------------- 服务方（主线程） ----------------
    def _tick(self):
        """QTimer 回调：在预算内尽可能多服务几帧。"""
        if not self._active:
            return
        # 心跳：证明"主线程确实回到过事件循环"。饥饿检测靠它区分
        # 「泵活着但这一帧渲染慢」与「主线程被调用方占死、永远不会服务」。
        # （漏了心跳 = _beats 恒 0，任何一次 wait 超时都会被误判饿死，
        #  整轮导出静默降级成定格，比卡死更难发现。）
        self._beats += 1
        if self._busy:
            return                      # 嵌套事件循环重入保护（见下方注释）
        self._busy = True
        t0 = time.perf_counter()
        try:
            while True:
                try:
                    job = self._q.get_nowait()
                except queue.Empty:
                    return
                if job.done.is_set():           # 已取消/已超时的请求直接丢
                    continue
                try:
                    self._serve(job)
                except Exception as e:
                    job.img = None
                    job.err = repr(e)[:200]
                    self._bump("failed")
                finally:
                    job.done.set()
                if (time.perf_counter() - t0) * 1000.0 >= TICK_BUDGET_MS:
                    return                      # 预算用完，把事件循环还给 GUI
        finally:
            self._busy = False

    def _serve(self, job):
        """主线程真正干活：确保 slot 已按该状态加载 -> seek -> 抓图。"""
        ln = job.lane
        k = int(job.k)
        dt = float(job.dt)
        rd = self._renderer
        ref = ln.ref
        if ref is None:
            ref = rd.register(ln.path)          # 原始大小写路径，不是键里的小写
            ln.ref = ref
            if ref is None:
                ln.last_err = "register 失败: %s" % ln.path
                self._bump("failed")
                return
        if ln.grid != dt:
            ln.grid = dt
            ln.head = 0                         # 网格变了：页面从头走
        img, new_roi = rd.render_animated({"ref": ref, "motion": ln.motion,
                                           "expression": ln.expression,
                                           "lip": ln.lip,
                                           "lip_gain": ln.lip_gain,
                                           # v3.7.3：把「这张表属于哪个 clip」
                                           # 一起带下去，渲染层按 tag 变化
                                           # 强制重发 setLip（同 ref 连续
                                           # 多 clip 全靠这个字段换表）
                                           "lip_tag": ln.lip_tag,
                                           "motion_switch": ln.motion_changed},
                                          model_id=ln.mid, k=k, dt_ms=dt,
                                          roi=ln.roi)
        ln.motion_changed = False      # 消费掉，下一帧不再重复发 FORCE
        if new_roi:
            ln.roi = tuple(int(v) for v in new_roi)
        ln.renders += 1
        if img is None or not getattr(img, "size", 0):
            ln.last_err = (rd.stats().get("err", "") or "渲染返回空")[:200]
            self._bump("failed")
            return
        self._bump("served")
        self._bump("frames")
        with self._lock:
            if k not in ln.ring:
                ln.order.append(k)
            ln.ring[k] = img
            while len(ln.order) > self.ring_keep:
                ln.ring.pop(ln.order.pop(0), None)
        ln.head = k
        job.img = img

    # ---------------- 内部 ----------------
    def _drop_all(self, why):
        while True:
            try:
                job = self._q.get_nowait()
            except queue.Empty:
                break
            if not job.done.is_set():
                job.err = why
                job.done.set()

    def lanes_info(self):
        with self._lock:
            return {ln.key: {"mid": ln.mid, "head": ln.head, "ring": len(ln.ring),
                             "renders": ln.renders, "err": ln.last_err[:120]}
                    for ln in self._lanes.values()}

    def clear_lanes(self):
        with self._lock:
            self._lanes.clear()


def _lane_path(key):
    """键里的路径是归一化小写形（只为去重）；真注册一律走 Lane.path 原样。

    留着这个函数只为调试/测试里从键反查路径，别拿它去 register。"""
    return key[0]


_PUMP = None
_PUMP_LOCK = threading.RLock()


def pump():
    """进程级泵单例。"""
    global _PUMP
    with _PUMP_LOCK:
        if _PUMP is None:
            _PUMP = L2DAnimPump()
        return _PUMP


def shutdown():
    global _PUMP
    with _PUMP_LOCK:
        if _PUMP is not None:
            with contextlib.suppress(Exception): _PUMP.stop()
            _PUMP = None
