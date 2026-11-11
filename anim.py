# -*- coding: utf-8 -*-
"""关键帧动画数据层（纯函数、无 Qt、无 IO）。

设计要点
--------
1. **向后兼容**：没有关键帧的节点仍走原来的 sin 循环（见 l2d_rig_core.animation_for）。
   本模块只负责"用户显式打了关键帧的通道"。

2. **数据结构**（可直接 json 序列化）
       tracks = {
           "head": {
               "rot": [{"t": 0.0, "v": 0.0, "ease": "linear"},
                       {"t": 2.0, "v": 8.0, "ease": "inout"}],
               "ty":  [...],
           },
           "body": {"ty": [...]},
       }
   - 顶层 key 是节点名（rig["nodes"][*]["node"]）
   - 第二层是参数名（PARAMS）
   - 值是按 t 升序的关键帧列表

3. **求值**
   - 首个关键帧之前 -> 取首帧值（不外推）
   - 末个关键帧之后 -> 取末帧值
   - 相邻两帧之间 -> 按前一帧的 ease 插值

4. **缓动**（ease 记在"起始帧"上，即从这一帧出发的那一段用什么曲线）
   - linear / in / out / inout / hold

5. **容错**：所有公开函数都不抛异常（脏数据返回默认值），
   因为工程文件可能被手改、旧版本读写。
"""
import math

# 节点可动画参数（与 l2d_rig_core.animation_for 的返回键一致）
PARAMS = ("rot", "tx", "ty", "sc", "sc_y")

# 参数默认值（None 表示"该参数不参与"，sc_y 特殊）
DEFAULTS = {
    "rot": 0.0,
    "tx": 0.0,
    "ty": 0.0,
    "sc": 1.0,
    "sc_y": None,
}

# 界面显示名
PARAM_LABELS = {
    "rot": "旋转",
    "tx": "X 位移",
    "ty": "Y 位移",
    "sc": "缩放",
    "sc_y": "Y 缩放",
}

EASES = ("linear", "in", "out", "inout", "hold")
EASE_LABELS = {
    "linear": "匀速",
    "in": "缓入",
    "out": "缓出",
    "inout": "缓入缓出",
    "hold": "保持（阶跃）",
}

# 数值护栏：防止手改 JSON 后出现 inf/nan 把渲染搞崩
_ABS_MAX = 1.0e6


def _safe_float(x, default=0.0):
    """转 float，非法/超范围一律回落默认值。"""
    try:
        v = float(x)
    except Exception:
        return default
    if v != v or v in (float("inf"), float("-inf")):   # nan / inf
        return default
    if abs(v) > _ABS_MAX:
        return default
    return v


# ---------------- 缓动 ----------------

def _ease_linear(u):
    return u


def _ease_in(u):
    return u * u


def _ease_out(u):
    return 1.0 - (1.0 - u) * (1.0 - u)


def _ease_inout(u):
    # smoothstep：两端导数为 0，手感最接近常见动画软件
    return u * u * (3.0 - 2.0 * u)


def _ease_hold(u):
    return 0.0


_EASE_FN = {
    "linear": _ease_linear,
    "in": _ease_in,
    "out": _ease_out,
    "inout": _ease_inout,
    "hold": _ease_hold,
}


def ease_fn(name):
    """取缓动函数；未知名字回落 linear（不抛异常）。"""
    return _EASE_FN.get(name, _ease_linear)


def normalize_ease(name):
    """把 ease 名规范化到合法集合内。"""
    try:
        s = str(name or "").strip().lower()
    except Exception:
        return "linear"
    return s if s in _EASE_FN else "linear"


# ---------------- 关键帧构造 / 校验 ----------------

def new_keyframe(t, v, ease="linear"):
    """造一个关键帧 dict。"""
    return {"t": _safe_float(t, 0.0),
            "v": _safe_float(v, 0.0),
            "ease": normalize_ease(ease)}


def _norm_kf(kf):
    """把任意来源的 kf 规整成合法 dict；不合法返回 None。"""
    if not isinstance(kf, dict):
        return None
    if "t" not in kf or "v" not in kf:
        return None
    return new_keyframe(kf.get("t"), kf.get("v"), kf.get("ease", "linear"))


def sort_track(track):
    """按 t 升序排；同 t 只保留最后一个（后来的覆盖先来的）。"""
    if not isinstance(track, list):
        return []
    out = []
    for kf in track:
        n = _norm_kf(kf)
        if n is not None:
            out.append(n)
    out.sort(key=lambda k: k["t"])
    # 同 t 去重：保留后出现的（sort 是稳定排序，后出现的位置更靠后）
    dedup = []
    for kf in out:
        if dedup and abs(dedup[-1]["t"] - kf["t"]) < 1e-9:
            dedup[-1] = kf
        else:
            dedup.append(kf)
    return dedup


# ---------------- 求值 ----------------

def eval_track(track, t):
    """单条轨道在 t 时刻的值。空轨道返回 None（表示"无动画"）。"""
    ks = sort_track(track)
    if not ks:
        return None
    t = _safe_float(t, 0.0)
    if t <= ks[0]["t"]:
        return ks[0]["v"]
    if t >= ks[-1]["t"]:
        return ks[-1]["v"]
    # 找区间 [a, b]
    lo, hi = 0, len(ks) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if ks[mid]["t"] <= t:
            lo = mid
        else:
            hi = mid
    a, b = ks[lo], ks[hi]
    span = b["t"] - a["t"]
    if span <= 1e-9:
        return b["v"]
    u = (t - a["t"]) / span
    if u < 0.0:
        u = 0.0
    elif u > 1.0:
        u = 1.0
    e = ease_fn(a.get("ease"))(u)
    return a["v"] + (b["v"] - a["v"]) * e


def eval_node(tracks, node, t, only_params=None):
    """某节点在 t 时刻要覆盖的全部参数值。

    返回 dict（只含"确实有轨道"的参数），可直接 a.update(...) 覆盖到
    animation_for 的默认结果上。
    """
    out = {}
    if not isinstance(tracks, dict):
        return out
    node_tracks = tracks.get(node)
    if not isinstance(node_tracks, dict):
        return out
    for p, track in node_tracks.items():
        if p not in PARAMS:
            continue
        if only_params is not None and p not in only_params:
            continue
        v = eval_track(track, t)
        if v is not None:
            out[p] = v
    return out


def has_anim(tracks):
    """tracks 里是否存在任何非空轨道。"""
    if not isinstance(tracks, dict):
        return False
    for node, ntr in tracks.items():
        if not isinstance(ntr, dict):
            continue
        for p, track in ntr.items():
            if p in PARAMS and isinstance(track, list) and len(track) > 0:
                return True
    return False


# ---------------- 编辑操作（返回新对象，便于 history 值记录）----------------

def clone_tracks(tracks):
    """深拷贝（只到关键帧 dict 层，足够做 undo 快照）。"""
    out = {}
    if not isinstance(tracks, dict):
        return out
    for node, ntr in tracks.items():
        if not isinstance(ntr, dict):
            continue
        d = {}
        for p, track in ntr.items():
            if p not in PARAMS:
                continue
            ks = sort_track(track)
            if ks:
                d[p] = [dict(k) for k in ks]
        if d:
            out[node] = d
    return out


def set_keyframe(tracks, node, param, t, v, ease="linear"):
    """在某节点某参数上打/改一个关键帧（同 t 覆盖）。返回新的 tracks 副本。"""
    if param not in PARAMS:
        return clone_tracks(tracks)
    out = clone_tracks(tracks)
    nt = out.setdefault(node, {})
    ks = nt.get(param, [])
    newk = new_keyframe(t, v, ease)
    # 同 t 覆盖
    replaced = False
    for i, kf in enumerate(ks):
        if abs(kf["t"] - newk["t"]) < 1e-9:
            ks[i] = newk
            replaced = True
            break
    if not replaced:
        ks.append(newk)
    nt[param] = sort_track(ks)
    return out


def remove_keyframe(tracks, node, param, t, eps=0.05):
    """删掉 t 附近（±eps 秒）的关键帧。返回新的 tracks 副本。"""
    out = clone_tracks(tracks)
    nt = out.get(node)
    if not isinstance(nt, dict):
        return out
    ks = nt.get(param)
    if not isinstance(ks, list):
        return out
    keep = [k for k in ks if abs(k["t"] - _safe_float(t, -1e9)) > eps]
    if keep:
        nt[param] = keep
    else:
        nt.pop(param, None)
    if not nt:
        out.pop(node, None)
    return out


def remove_param(tracks, node, param):
    """整条轨道删除。"""
    out = clone_tracks(tracks)
    nt = out.get(node)
    if isinstance(nt, dict):
        nt.pop(param, None)
        if not nt:
            out.pop(node, None)
    return out


def find_keyframe_at(tracks, node, param, t, eps=0.05):
    """找 t 附近的关键帧，返回下标或 None（时间轴 UI 点选/拖动用）。"""
    nt = (tracks or {}).get(node) if isinstance(tracks, dict) else None
    if not isinstance(nt, dict):
        return None
    ks = nt.get(param)
    if not isinstance(ks, list) or not ks:
        return None
    t = _safe_float(t, -1e9)
    best, bestd = None, None
    for i, kf in enumerate(ks):
        d = abs(_safe_float(kf.get("t"), 0.0) - t)
        if d <= eps and (bestd is None or d < bestd):
            best, bestd = i, d
    return best


def keyframe_times(tracks):
    """所有轨道出现过的关键帧时间点（升序去重），时间轴画竖线用。"""
    ts = set()
    if isinstance(tracks, dict):
        for ntr in tracks.values():
            if not isinstance(ntr, dict):
                continue
            for p, track in ntr.items():
                if p not in PARAMS or not isinstance(track, list):
                    continue
                for kf in track:
                    if isinstance(kf, dict) and "t" in kf:
                        ts.add(round(_safe_float(kf["t"], 0.0), 6))
    return sorted(ts)


def all_tracks(tracks):
    """展平成 [(node, param, track), ...]，按节点名/参数名稳定排序（时间轴 UI 用）。"""
    rows = []
    if not isinstance(tracks, dict):
        return rows
    for node in sorted(tracks.keys()):
        ntr = tracks[node]
        if not isinstance(ntr, dict):
            continue
        for p in PARAMS:
            track = ntr.get(p)
            if isinstance(track, list) and track:
                rows.append((node, p, sort_track(track)))
    return rows


# ---------------- 与渲染对接 ----------------

def apply_to(node, t, base, tracks):
    """把关键帧覆盖到 base（animation_for 的默认结果）上，返回 base。

    base 里没有的键不动；tracks 里没有的通道不动 —— 这样"没做动画的工程"
    完全保持原有的 sin 行为。
    """
    ov = eval_node(tracks, node, t)
    if ov:
        for k, v in ov.items():
            base[k] = v
    return base
