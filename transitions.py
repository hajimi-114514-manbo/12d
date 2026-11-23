# -*- coding: utf-8 -*-
"""转场目录（纯数据 + 纯函数，无 Qt / 无 IO 副作用）。

需求：转场页原本是空的（_rebuild_asset_cards_inner 没有 transition 分支）。
这里把**引擎真实支持**的转场整理成可选预设，供三处消费：
  · 转场页 UI（video_workbench）：列卡片，点=写入 proj["transition"]
  · WebGAL 导出（webgal_export）：setTransition 的 -enter/-exit 用它，
    替代原先硬编码的 enter/exit（未设置时仍是 enter/exit，逐字节不变）
  · 视频导出（video_export）：按关键帧派生的 alpha/位移/缩放/模糊，
    在片段进出边界近似还原（未设置时零改动）

权威来源 = WebGAL 运行时 game/animation/animationTable.json + 同名 .json
关键帧。本模块不写死名单：能读到运行时就读（名字、时长、动画属性都从
关键帧派生），读不到（装机没带运行时/测试环境）退回内置快照，保证
转场页永远有内容、校验永远有依据。

proj["transition"] 形态（工程级，全片统一）：
    {"name": 预设名, "enter": 引擎进入动画名, "exit": 引擎退出动画名,
     "duration_ms": int, "video_kind": 视频侧近似类型, "fade": bool}
未设置（None）= 默认淡入淡出（enter/exit），与历史行为全等。
"""
import json
import os
import contextlib

# ---- 内置快照（animationTable.json 读不到时的兜底）----
# 名字 = 引擎 v1 动画名；运行时真表里每个都还有 -v2 变体，导出用 v1 名即可。
_TABLE_FALLBACK = [
    "enter-from-left", "enter-from-bottom", "enter-from-right", "shake",
    "move-front-and-back", "enter", "exit", "blur", "oldFilm", "dotFilm",
    "reflectionFilm", "glitchFilm", "rgbFilm", "godrayFilm", "removeFilm",
    "shockwaveIn", "shockwaveOut",
]

# 视频侧近似类型（video_export 据此还原；engine_only = 引擎特效，视频退化成淡入淡出）
VK_FADE = "fade"
VK_SLIDE = "slide"
VK_ZOOM = "zoom"
VK_BLUR = "blur"
VK_ENGINE_ONLY = "engine_only"

# 转场预设表：(显示名, 说明, enter 动画, exit 动画, 视频侧类型)
# enter/exit 必须是引擎动画名（在 _TABLE_FALLBACK / 运行时表里）。
# 顺序即转场页展示顺序。
_PRESETS = [
    ("淡入淡出", "元素渐显渐隐，最通用的场景过渡", "enter", "exit", VK_FADE),
    ("无转场", "硬切，不做任何过渡", "", "", VK_FADE),
    ("左侧滑入", "新画面从左推入，配右侧淡出", "enter-from-left", "exit", VK_SLIDE),
    ("右侧滑入", "新画面从右推入", "enter-from-right", "exit", VK_SLIDE),
    ("底部滑入", "新画面从下方升起", "enter-from-bottom", "exit", VK_SLIDE),
    ("模糊过渡", "进出都带高斯模糊，梦幻感", "blur", "blur", VK_BLUR),
    ("推拉", "放大进场（dolly-in），适合强调", "move-front-and-back", "exit", VK_ZOOM),
    ("冲击波", "扩散波纹转场（引擎特效）", "shockwaveIn", "shockwaveOut", VK_ENGINE_ONLY),
]

# 视频侧 slide 方向：从 enter 动画名推
_SLIDE_DIR = {
    "enter-from-left": "left",
    "enter-from-right": "right",
    "enter-from-bottom": "bottom",
}

_cache = None


def _anim_dir():
    """运行时动画目录（game/animation）。读不到返回 None。"""
    here = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(here, "templates", "webgal_runtime", "game", "animation")
    return d if os.path.isdir(d) else None


def _load_table():
    """引擎动画名集合（小写）。优先运行时 animationTable.json，退回快照。"""
    d = _anim_dir()
    if d:
        with contextlib.suppress(Exception):
            with open(os.path.join(d, "animationTable.json"), encoding="utf-8") as f:
                arr = json.load(f)
            names = {str(x).lower() for x in arr if isinstance(x, str)}
            if names: return names
    return {str(x).lower() for x in _TABLE_FALLBACK}


def _read_anim(name):
    """动画名 -> (duration_ms, 关键帧列表)。v1/v2 两种格式都吃。

    v1: [{alpha,scale,position,blur,duration}, ...]，duration 是**该段时长**
        （累加得总时长）。
    v2: {keyframes:[{time, alpha, position:{x}, blur}, ...]}，time 是**绝对毫秒**。
    解析失败返回 (None, [])。"""
    d = _anim_dir()
    if not d or not name: return None, []
    p = os.path.join(d, "%s.json" % name)
    if not os.path.isfile(p): return None, []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, []
    if isinstance(data, dict):        # v2
        kfs = data.get("keyframes") or []
        total = 0
        for k in kfs:
            with contextlib.suppress(Exception): total = max(total, int(k.get("time") or 0))
        return (total or None), kfs
    if isinstance(data, list):        # v1
        total = 0
        for k in data:
            with contextlib.suppress(Exception): total += int(k.get("duration") or 0)
        return (total or None), data
    return None, []


def _kf_get(kf, key):
    """关键帧里取属性（v1 平铺 / v2 也平铺），position 归一成 (dx, dy)。"""
    if not isinstance(kf, dict): return None
    if key == "position":
        pos = kf.get("position")
        if isinstance(pos, dict):
            return (float(pos.get("x") or 0), float(pos.get("y") or 0))
        return None
    return kf.get(key)


def _anim_props(kfs):
    """关键帧里出现过的动画属性集合（alpha/position/scale/blur）。"""
    out = set()
    for kf in kfs:
        if not isinstance(kf, dict): continue
        for key in ("alpha", "position", "scale", "blur"):
            if kf.get(key) is not None: out.add(key)
    return out


def catalog():
    """转场预设列表（带派生的时长/属性），供 UI 与导出消费。进程级缓存。"""
    global _cache
    if _cache is not None: return _cache
    table = _load_table()
    out = []
    for name, desc, enter, exit_, vkind in _PRESETS:
        dur = 0
        props = set()
        for a in (enter, exit_):
            if not a: continue
            d, kfs = _read_anim(a)
            if d: dur = max(dur, d)
            props |= _anim_props(kfs)
        # 读不到运行时关键帧时给个合理默认时长（enter/exit 实测 300ms）
        if dur <= 0: dur = 300 if (enter or exit_) else 0
        available = True
        if enter or exit_:
            available = (enter.lower() in table and exit_.lower() in table)
        out.append({
            "name": name, "desc": desc,
            "enter": enter, "exit": exit_,
            "duration_ms": int(dur),
            "video_kind": vkind,
            "slide_dir": _SLIDE_DIR.get(enter, ""),
            "props": sorted(props),
            "available": available,   # 引擎表里没有 -> UI 置灰、导出退回默认
        })
    _cache = out
    return out


def by_name(name):
    for p in catalog():
        if p["name"] == name: return p
    return None


def to_proj_transition(name):
    """预设名 -> 写入 proj["transition"] 的 dict；无效名返回 None。

    「无转场」返回 {"name":..., "enter":"", "exit":"", "duration_ms":0}
    （导出据此不插 setTransition）。引擎表里没有的预设也返回带
    available=False 的项，由导出侧决定退回默认。"""
    p = by_name(name)
    if p is None: return None
    return {
        "name": p["name"],
        "enter": p["enter"],
        "exit": p["exit"],
        "duration_ms": int(p["duration_ms"]),
        "video_kind": p["video_kind"],
        "slide_dir": p["slide_dir"],
        "available": bool(p["available"]),
    }


def valid_anim(name):
    """动画名是否在引擎表里（导出前校验，脏名退回默认 enter/exit）。"""
    if not name: return True          # 空（无转场）合法
    return str(name).lower() in _load_table()
