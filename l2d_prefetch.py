# -*- coding: utf-8 -*-
"""L2D 立绘预渲染缓存（v3.5.11，方案 B）。

**为什么需要它**：L2D 渲染必须在 GUI 主线程（QWebEngine + QEventLoop），
而导出在 QThread 里跑，两者互斥（实测报
`QObject::startTimer: current thread's event dispatcher has already been
destroyed`，导出线程内渲染必然死锁）。

**方案**：导出开始前，在主线程把全时间线用到的**唯一立绘状态**渲染一遍
（实测真实工程只有 8 个状态、耗时 0.2s、内存 6.5MB），导出线程逐帧只做
「按 clip 查缓存 + 贴图」，零渲染、零线程问题。

代价与取舍：
- 画面是「状态首帧的定格」——不含呼吸/眨眼逐帧变化（同一 clip 内画面
  恒定）。换来的是导出可重现 + 速度。若要逐帧动画，需改方案 A（主线程
  队列式渲染），那是更大的架构改动。
- 状态键 = (模型路径, motion, expression)：clip 内这些字段不变，所以
  同 clip 各帧可复用同一张图。
"""
import os
import sys
import threading

import numpy as np

_V_DIR = os.path.dirname(os.path.abspath(__file__))
if _V_DIR not in sys.path:
    sys.path.insert(0, _V_DIR)


class L2DPrefetch(object):
    """立绘状态 -> RGBA 图的预渲染缓存（线程安全只读）。"""

    def __init__(self):
        self._cache = {}            # key -> ndarray(H,W,4)
        self._lock = threading.RLock()
        self._renderer = None
        self._built = False
        self.stats = {"states": 0, "hit": 0, "miss": 0, "seconds": 0.0}

    # ---------------- 键 ----------------
    @staticmethod
    def state_key(model_path, motion, expression):
        """状态键。模型路径归一化（避免分隔符差异造成重复渲染）。

        motion/expression 统一成字符串：工程里字段可能是 None（缺省）或
        ""（空串），两者语义都是「无」，必须归一成同一个键，否则
        预渲染写 ""、查询用 None 就会 miss（实测踩过）。
        """
        p = os.path.normpath(str(model_path or "")).lower()
        return (p, str(motion or ""), str(expression or ""))

    @staticmethod
    def clip_key(clip):
        """从 clip dict 直接取状态键。"""
        src = clip.get("source") or {}
        return L2DPrefetch.state_key(src.get("path"),
                                     clip.get("motion"),
                                     clip.get("expression"))

    # ---------------- 预渲染 ----------------
    def build(self, proj, force=False):
        """主线程调用：收集全工程立绘状态并逐一渲染。

        返回 成功渲染的状态数；渲染器不可用时返回 0（导出侧自动回退
        占位卡/原有路径，不阻断导出）。
        """
        if self._built and not force:
            return self.stats["states"]
        import time
        t0 = time.time()
        keys = self.collect_states(proj)
        if not keys:
            self._built = True
            self.stats = {"states": 0, "hit": 0, "miss": 0,
                          "seconds": time.time() - t0}
            return 0
        try:
            import l2d_renderer as L2R
        except Exception:
            self._built = True
            return 0
        rd = L2R.shared_renderer()
        if rd is None or not rd.ready():
            self._built = True
            return 0
        self._renderer = rd
        out = {}
        for model_path, motion, expression in keys:
            try:
                ref = rd.register(model_path)
                if not ref:
                    continue
                img = rd.render({"ref": ref, "motion": motion,
                                 "expression": expression})
                if img is not None and getattr(img, "size", 0):
                    out[(os.path.normpath(str(model_path)).lower(),
                         motion, expression)] = img
            except Exception:
                continue
        with self._lock:
            self._cache.update(out)
            self._built = True
            self.stats = {"states": len(out), "hit": 0, "miss": 0,
                          "seconds": time.time() - t0}
        return len(out)

    @staticmethod
    def collect_states(proj):
        """全工程立绘 clip 的唯一状态集合（去重、保序）。"""
        seen = []
        seen_set = set()
        try:
            tracks = proj.get("tracks") or []
            for tr in tracks:
                if not isinstance(tr, dict):
                    continue
                if not str(tr.get("name") or "").startswith("FIG"):
                    continue
                for c in (tr.get("clips") or []):
                    if not isinstance(c, dict):
                        continue
                    k = L2DPrefetch.clip_key(c)
                    if not k[0]:            # 无路径，跳过
                        continue
                    if k in seen_set:
                        continue
                    seen_set.add(k)
                    seen.append(k)
            # 多幕工程：每幕的 tracks
            for act in (proj.get("acts") or []):
                if not isinstance(act, dict):
                    continue
                for tr in (act.get("tracks") or []):
                    if not isinstance(tr, dict):
                        continue
                    if not str(tr.get("name") or "").startswith("FIG"):
                        continue
                    for c in (tr.get("clips") or []):
                        if not isinstance(c, dict):
                            continue
                        k = L2DPrefetch.clip_key(c)
                        if not k[0] or k in seen_set:
                            continue
                        seen_set.add(k)
                        seen.append(k)
        except Exception:
            pass
        return seen

    # ---------------- 查询（导出线程用） ----------------
    def get(self, model_path, motion, expression):
        """取预渲染图；没有则 None（调用方回退占位卡）。"""
        k = self.state_key(model_path, motion, expression)
        with self._lock:
            hit = self._cache.get(k)
            if hit is not None:
                self.stats["hit"] += 1
                return hit
            self.stats["miss"] += 1
            return None

    def get_for_clip(self, clip):
        return self.get((clip.get("source") or {}).get("path"),
                        clip.get("motion"), clip.get("expression"))

    def ready(self):
        return bool(self._cache)

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._built = False
            self.stats = {"states": 0, "hit": 0, "miss": 0, "seconds": 0.0}


_PREFETCH = None
_PREFETCH_LOCK = threading.RLock()


def prefetch():
    """进程级预渲染缓存单例。"""
    global _PREFETCH
    with _PREFETCH_LOCK:
        if _PREFETCH is None:
            _PREFETCH = L2DPrefetch()
        return _PREFETCH


def build_for_project(proj, force=False):
    """便捷入口：主线程调用，预渲染整工程立绘状态。"""
    return prefetch().build(proj, force=force)
