# -*- coding: utf-8 -*-
"""多段路径运镜（camera path）——纯函数，无 Qt 无 IO。

## 为什么要单独一层

引擎的 `setTempAnimation` 指令本身就能表达多段运动（关键帧数组），木偶
早就能导入/导出/预览它。缺的是**「怎么写出这条指令」**：用户面对
`[{"duration":0,"scale":1.2},{"duration":800,"scale":1.5}]` 这种 JSON
无从下手，也没有地方看到自己录的路径长什么样。

本模块负责三件事，全部是纯数据变换，可独立测试：
  1. **关键帧表 <-> 指令**：build_cmd() / parse_cmd()
  2. **独立构图**：背景与立绘各录一套（引擎用 -target 分槽位表达）
  3. **采样**：sample_at() 给出任意时刻的插值结果，供预览与校验

## 引擎 v1 关键帧语义（照 webgal_preview._apply_camera 的既有实现）

    [{"duration":0,"scale":{"x":1,"y":1}}, {"duration":800,"scale":...}]

  · kf[0] 是**起始状态**，在指令时刻 t0 生效，它的 duration 不计入总时长
  · kf[i] 的 duration 是「从 kf[i-1] 过渡到 kf[i] 的耗时（毫秒）」
  · 所以 times[0]=0，times[i]=times[i-1]+kf[i].duration
  · 结束后保持末帧（不外推回弹）

**这套语义必须与预览端完全一致**，否则编辑器里看到的和导出后引擎跑的
是两回事——这正是要抽成独立模块+测试的原因。
"""

# 合法的运镜通道（引擎实际会读的键）
CHANNELS = ("scale", "position", "rotation", "alpha", "blur")

# 通道的默认值（缺省时按"不变"处理）
DEFAULTS = {
    "scale": {"x": 1.0, "y": 1.0},
    "position": {"x": 0.0, "y": 0.0},
    "rotation": 0.0,
    "alpha": 1.0,
    "blur": 0.0,
}

# 引擎 -target 槽位：舞台整体（bg-main）与立绘（fig-*）
TARGET_STAGE = "bg-main"
TARGET_FIG = "fig-center"
VALID_TARGETS = (TARGET_STAGE, TARGET_FIG,
                 "fig-left", "fig-right")


def _num(v, d=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return d
    if f != f or f in (float("inf"), float("-inf")):   # nan / inf
        return d
    return f


def _clean_kf(kf):
    """把单个关键帧归一成 {duration, <channel>: <值>}；脏通道丢弃。"""
    if not isinstance(kf, dict): return None
    out = {"duration": max(0.0, _num(kf.get("duration"), 0.0))}
    for ch in CHANNELS:
        if ch not in kf: continue
        raw = kf.get(ch)
        if ch == "scale":
            if isinstance(raw, dict):
                out["scale"] = {"x": _num(raw.get("x"), 1.0),
                                "y": _num(raw.get("y"), 1.0)}
            elif isinstance(raw, (int, float)):
                k = _num(raw, 1.0)          # 单值 = 等比
                out["scale"] = {"x": k, "y": k}
        elif ch == "position":
            if isinstance(raw, dict):
                out["position"] = {"x": _num(raw.get("x"), 0.0),
                                   "y": _num(raw.get("y"), 0.0)}
        else: out[ch] = _num(raw, DEFAULTS[ch])
    return out


def normalize_keys(keyframes):
    """清洗关键帧数组：至少保留 2 帧（单帧无法表达"运动"）。

    返回新列表；不足 2 帧时补一个与首帧相同的终止帧（duration=0），
    这样调用方永远拿到可用的数组，不必到处判空。
    """
    out = []
    for kf in (keyframes or []):
        c = _clean_kf(kf)
        if c is not None: out.append(c)
    if not out: out = [{"duration": 0.0}]
    if len(out) == 1: out.append(dict(out[0], duration=0.0))
    return out


def keyframe_times(keyframes):
    """按引擎语义算每个关键帧的**绝对毫秒时刻**。

    times[0] = 0（kf0 生效点）；times[i] = times[i-1] + kf[i].duration。
    注意 kf[0].duration 不参与——它是起始状态的驻留时间，引擎忽略了它
    （照 webgal_preview 的实现，那里也明确写了这条）。
    """
    kfs = normalize_keys(keyframes)
    times = [0.0]
    for i in range(1, len(kfs)):
        times.append(times[-1] + kfs[i]["duration"])
    return times


def total_ms(keyframes):
    """整段运镜的总时长（毫秒）。"""
    t = keyframe_times(keyframes)
    return t[-1] if t else 0.0


def _val_at(kfs, idx, ch):
    """取第 idx 段的某通道值；该帧没有则向前回溯（与预览端 _val 一致）。"""
    for i in range(min(idx, len(kfs) - 1), -1, -1):
        v = kfs[i].get(ch)
        if v is not None: return v
    return DEFAULTS.get(ch)


def sample_at(keyframes, ms):
    """在相对起点 ms 毫秒处采样，返回 {channel: value}（已插值）。

    超出总时长时**保持末帧**（引擎行为：结束后停在终点，不回弹）。
    线性插值——引擎 setTempAnimation 的 ease 由引擎负责，这里的采样只
    用于编辑器预览与校验，不需要实现缓动曲线。
    """
    kfs = normalize_keys(keyframes)
    times = keyframe_times(keyframes)
    total = times[-1]
    m = max(0.0, min(_num(ms, 0.0), total)) if total > 0 else 0.0

    # 段定位：落在 [times[i], times[i+1]) 的段 i（段 i 从 kf[i] 到 kf[i+1]）
    idx = 0
    for i in range(len(times) - 1):
        if m >= times[i] - 1e-6: idx = i
    seg_start = times[idx]
    seg_end = times[idx + 1] if idx + 1 < len(times) else total
    seg_dur = seg_end - seg_start
    p = 1.0 if seg_dur <= 1e-9 else max(0.0, min(1.0, (m - seg_start) / seg_dur))

    nxt = min(idx + 1, len(kfs) - 1)
    out = {}
    for ch in CHANNELS:
        a = _val_at(kfs, idx, ch)
        b = _val_at(kfs, nxt, ch)
        if isinstance(a, dict) or isinstance(b, dict):
            a = a if isinstance(a, dict) else DEFAULTS[ch]
            b = b if isinstance(b, dict) else DEFAULTS[ch]
            out[ch] = {
                "x": a.get("x", DEFAULTS[ch]["x"]) * (1 - p)
                     + b.get("x", DEFAULTS[ch]["x"]) * p,
                "y": a.get("y", DEFAULTS[ch]["y"]) * (1 - p)
                     + b.get("y", DEFAULTS[ch]["y"]) * p,
            }
        else:
            av = _num(a, DEFAULTS[ch])
            bv = _num(b, DEFAULTS[ch])
            out[ch] = av * (1 - p) + bv * p
    out["_p"] = p
    out["_seg"] = idx
    return out


def build_cmd(keyframes, target=TARGET_STAGE, t0=0.0, duration=None):
    """把关键帧数组拼成引擎指令：setTempAnimation:[...] -target=...;

    duration 参数：指令末尾的 -duration 是**整条指令的过渡时长**，
    与关键帧自身的 duration 不是一回事。多段运镜通常写 0（时长完全由
    关键帧决定）。这里默认省略，显式传入才写。
    """
    import json as _json
    kfs = normalize_keys(keyframes)
    body = _json.dumps(kfs, ensure_ascii=False, separators=(",", ":"))
    tgt = target if target in VALID_TARGETS else TARGET_STAGE
    cmd = "setTempAnimation:%s -target=%s" % (body, tgt)
    if duration is not None: cmd += " -duration=%d" % int(_num(duration, 0))
    return cmd + ";"


def parse_cmd(cmd):
    """从指令里取出 (keyframes, target)。失败返回 (None, None)。

    只认 setTempAnimation；参数体必须是 JSON 数组。容忍 -target 缺失
    （引擎默认 fig-center，但舞台级指令通常显式写 bg-main）。
    """
    import json as _json
    import re as _re
    s = str(cmd or "").strip()
    if not s.startswith("setTempAnimation"): return None, None
    m = _re.search(r"\[.*\]", s, _re.S)
    if not m: return None, None
    try:
        kfs = _json.loads(m.group(0))
    except Exception:
        return None, None
    if not isinstance(kfs, list) or not kfs: return None, None
    tm = _re.search(r"-target=(\S+?)(?:\s|;|$)", s)
    target = tm.group(1) if tm else TARGET_FIG
    return normalize_keys(kfs), target


def make_effect(keyframes, t0, target=TARGET_STAGE):
    """产出可直接塞进 proj["effects"] 的 {t, cmd} 条目。"""
    from_ = max(0.0, _num(t0, 0.0))
    return {"t": round(from_, 3),
            "cmd": build_cmd(keyframes, target=target)}


# ---------------- 常用路径模板（对标对方工具的「便捷运镜」预设） ----------------
# 每个模板给「舞台级」和「立绘级」两套，用户点一下就得到可编辑的关键帧。
TEMPLATES = {
    "缓慢推进": {
        "stage": [{"duration": 0, "scale": {"x": 1.0, "y": 1.0}},
                  {"duration": 1500, "scale": {"x": 1.15, "y": 1.15}}],
        "fig": [{"duration": 0, "scale": {"x": 1.0, "y": 1.0}},
                {"duration": 1500, "scale": {"x": 1.2, "y": 1.2}}],
    },
    "缓慢拉远": {
        "stage": [{"duration": 0, "scale": {"x": 1.15, "y": 1.15}},
                  {"duration": 1500, "scale": {"x": 1.0, "y": 1.0}}],
        "fig": [{"duration": 0, "scale": {"x": 1.2, "y": 1.2}},
                {"duration": 1500, "scale": {"x": 1.0, "y": 1.0}}],
    },
    "向左取景": {
        "stage": [{"duration": 0, "position": {"x": 0, "y": 0}},
                  {"duration": 1200, "position": {"x": 180, "y": 0}}],
        "fig": [{"duration": 0, "position": {"x": 0, "y": 0}},
                {"duration": 1200, "position": {"x": 120, "y": 0}}],
    },
    "向右取景": {
        "stage": [{"duration": 0, "position": {"x": 0, "y": 0}},
                  {"duration": 1200, "position": {"x": -180, "y": 0}}],
        "fig": [{"duration": 0, "position": {"x": 0, "y": 0}},
                {"duration": 1200, "position": {"x": -120, "y": 0}}],
    },
    "向上取景": {
        "stage": [{"duration": 0, "position": {"x": 0, "y": 0}},
                  {"duration": 1200, "position": {"x": 0, "y": 120}}],
        "fig": [{"duration": 0, "position": {"x": 0, "y": 0}},
                {"duration": 1200, "position": {"x": 0, "y": 80}}],
    },
    "向下取景": {
        "stage": [{"duration": 0, "position": {"x": 0, "y": 0}},
                  {"duration": 1200, "position": {"x": 0, "y": -120}}],
        "fig": [{"duration": 0, "position": {"x": 0, "y": 0}},
                {"duration": 1200, "position": {"x": 0, "y": -80}}],
    },
    "向左移动": {
        "stage": [{"duration": 0, "position": {"x": 200, "y": 0}},
                  {"duration": 2000, "position": {"x": -200, "y": 0}}],
        "fig": [{"duration": 0, "position": {"x": 150, "y": 0}},
                {"duration": 2000, "position": {"x": -150, "y": 0}}],
    },
    "人物特写": {
        "stage": [{"duration": 0, "scale": {"x": 1.0, "y": 1.0}},
                  {"duration": 1000, "scale": {"x": 1.35, "y": 1.35}}],
        "fig": [{"duration": 0, "scale": {"x": 1.3, "y": 1.3},
                 "position": {"x": 0, "y": -60}},
                {"duration": 1000, "scale": {"x": 1.6, "y": 1.6},
                 "position": {"x": 0, "y": -90}}],
    },
    "恢复全部运镜": {
        "stage": [{"duration": 0}, {"duration": 0}],
        "fig": [{"duration": 0}, {"duration": 0}],
    },
}

TEMPLATE_NAMES = tuple(TEMPLATES.keys())


def template_keys(name, level="stage"):
    """取模板关键帧（深拷贝，防止调用方改坏常量表）。"""
    import copy
    tpl = TEMPLATES.get(name)
    if not tpl: return None
    kfs = tpl.get(level if level in ("stage", "fig") else "stage")
    return copy.deepcopy(kfs) if kfs is not None else None