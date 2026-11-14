# -*- coding: utf-8 -*-
"""C1 预置动作表 + 表情表（纯数据 + 纯函数，无 Qt / 无 cv2 / 无 IO 副作用）。

定位：木偶工作室的「预置动作表 + 表情表」，供 app 播放、StageView 下拉、
remote_control catalog.get、WebGAL 导出脚本名 四方共用。

两层数据（K4 / task-70 扩容）
------------------------------
1. **程序化动作**（kind 缺省）：曲线表，motion_table 自己 eval 出各部件增量，
   puppet 立绘/PNG 都能用。7 动作 + 5 表情，出厂种子不变。
2. **cubism 模型动作**（kind="cubism"/"cubism_exp"）：id **原样等于**整合包
   model.json 键名（如 "anon/angry01"），导出直接写 -motion=anon/angry01，
   零映射。共 664 动作 + 232 表情（11 角色命名空间，动作集跨角色通用）。
   这类条目**由 WebGAL 引擎播放**，本模块不求值：
   - eval_motion / eval_expression 遇 kind=cubism* 返回 {}（中性，消费方
     _preset_tracks 自然跳过，不抛）
   - get_motion / get_expression 正常返回条目副本（带 kind/ns/common）
   - motion_ids()/expression_ids() 含 cubism 键 -> 白名单主路直接放行
   顶层可选元数据：namespaces（ns->角色/计数/说明）、recommended（约 20 条
   常用键，情绪面覆盖）；catalog() 仅在元数据非空时追加这两个键（旧消费方
   断言 keys=={motions,expressions} 对种子表继续成立）。

数据落盘（C2 变更）
------------------
表优先读数据文件：``templates/webgal/motion_table.json``（相对本模块目录），
路径可被环境变量 ``MUPPET_TEMPLATES_DIR`` 覆盖（指向目录，文件名固定
``motion_table.json``）。

- 文件存在且结构合法 -> **整体接管**（内置表仅作出厂种子），id 冲突以文件为准。
- 文件不存在 -> 自动把内置表 **self-seed** 写出（用户打开文件夹即可编辑）。
- 文件坏 JSON / 结构非法 -> 回退内置表，并在模块变量 ``warnings`` 记录。
- ``reload()`` 显式重读；``catalog()`` / ``eval_motion`` / ``eval_expression``
  等读表入口会按文件 mtime 惰性检测，文件变了即刻生效，无需重启。
- 900+ 条目加载：解析后建 id->条目 dict 索引，get/eval O(1)。

数据契约
--------
MOTIONS = [
    # 程序化（同 C1/C2）：
    {"id": str, "name": str, "dur": float, "loop": bool,
     "channels": {role: "rot"|"dy"},     # 可选；缺省按 ROLE_CHANNEL
     "curves": {role: [keyframe, ...]}}  # keyframe 用 anim.new_keyframe
    # cubism（K4）：
    {"id": "anon/angry01", "name": str, "kind": "cubism", "ns": "anon",
     "common": bool, "dur": 0.0, "loop": False}
]
    role ∈ {"head", "arm", "body", "hair"}（语义部件，节点名关键词解析
    留给消费方）。keyframe 的 v 是该部件的变换增量：rot 通道=度，dy 通道=
    y 偏移比例。曲线词汇对齐 anim.py（ease: linear/in/out/inout/hold）。

EXPRESSIONS = [
    {"id": str, "name": str, "fade": float,
     "pose": {role: {"rot": float, "dy": float, "dscale": float}}}
    # cubism_exp：{"id": "anon/angry01", "name": str, "kind": "cubism_exp",
    #              "ns": "anon", "fade": 0.0}
]
    pose 是终态快照；eval_expression(progress) 在 0（中性，全 0）到
    1（完整 pose）之间线性插值。

JSON 序列化：keyframe 用 ``[[t, v, ease], ...]`` 数组形式（镜像 Python 结构）。
数值护栏：rot ≤ 30 度，|dy| ≤ 0.05，dscale ≤ 0.05（克制、可用）——只约束
程序化条目；cubism 条目不评数值（引擎侧资产）。

API
---
motion_ids() / expression_ids() / get_motion(id) / get_expression(id)
catalog() -> {"motions": [...], "expressions": [...]}（表带元数据时另含
             "namespaces"、"recommended"）
eval_motion(id, t) -> {role: {"rot": float, "dy": float}}   # loop 取模；
                     cubism 条目 -> {}
eval_expression(id, progress) -> {role: {...}}              # cubism -> {}
reload() -> None（显式重读数据文件）

坏 id：get_motion / get_expression / eval_motion / eval_expression 抛 KeyError。
"""
import copy
import json
import os

import anim

# 语义部件
ROLES = ("head", "arm", "body", "hair")

# 缺省通道：head/arm/hair 走 rot，body 走 dy（motion 可用 channels 覆盖）
ROLE_CHANNEL = {"head": "rot", "arm": "rot", "body": "dy", "hair": "rot"}

# 数值护栏
ROT_MAX = 30.0
DY_MAX = 0.05
DSCALE_MAX = 0.05

# 模块级警告记录（加载回退/结构非法时写入）
warnings = []


def _kf(t, v, ease="linear"):
    """造关键帧（复用 anim.new_keyframe，词汇一致）。"""
    return anim.new_keyframe(t, v, ease)


# ---------------- 出厂种子（文件缺失/损坏时兜底，用户可编辑） ----------------
_SEED_MOTIONS = [
    {
        "id": "idle",
        "name": "待机呼吸",
        "dur": 4.0,
        "loop": True,
        "channels": {"body": "dy", "head": "rot"},
        "curves": {
            # body y 正弦近似：0 → 0.02 → 0 → -0.02 → 0
            "body": [_kf(0.0, 0.0, "linear"), _kf(1.0, 0.02, "inout"),
                     _kf(2.0, 0.0, "inout"), _kf(3.0, -0.02, "inout"),
                     _kf(4.0, 0.0, "linear")],
            # head 微 rot 轻摆
            "head": [_kf(0.0, 0.0, "linear"), _kf(1.0, 1.0, "inout"),
                     _kf(2.0, 0.0, "inout"), _kf(3.0, -1.0, "inout"),
                     _kf(4.0, 0.0, "linear")],
        },
    },
    {
        "id": "blink",
        "name": "眨眼",
        "dur": 2.0,
        "loop": True,
        "channels": {"head": "dy"},
        "curves": {
            # head 通道瞬降（eye/head 瞬时下压的近似表达）
            "head": [_kf(0.0, 0.0, "linear"), _kf(0.10, -0.008, "hold"),
                     _kf(0.25, 0.0, "linear"), _kf(2.0, 0.0, "linear")],
        },
    },
    {
        "id": "nod",
        "name": "点头",
        "dur": 2.0,
        "loop": True,
        "channels": {"head": "rot"},
        "curves": {
            # head rot 0 → 8 → 0，做两次
            "head": [_kf(0.0, 0.0, "inout"), _kf(0.5, 8.0, "inout"),
                     _kf(1.0, 0.0, "inout"), _kf(1.5, 8.0, "inout"),
                     _kf(2.0, 0.0, "inout")],
        },
    },
    {
        "id": "shake_head",
        "name": "摇头",
        "dur": 2.0,
        "loop": True,
        "channels": {"head": "rot"},
        "curves": {
            # head rot 左右摆动
            "head": [_kf(0.0, 0.0, "linear"), _kf(0.5, 6.0, "linear"),
                     _kf(1.0, -6.0, "linear"), _kf(1.5, 6.0, "linear"),
                     _kf(2.0, 0.0, "linear")],
        },
    },
    {
        "id": "wave",
        "name": "挥手",
        "dur": 2.0,
        "loop": True,
        "channels": {"arm": "rot"},
        "curves": {
            # arm rot 摆动
            "arm": [_kf(0.0, 0.0, "inout"), _kf(0.5, 22.0, "inout"),
                    _kf(1.0, 0.0, "inout"), _kf(1.5, -22.0, "inout"),
                    _kf(2.0, 0.0, "inout")],
        },
    },
    {
        "id": "shrug",
        "name": "耸肩",
        "dur": 2.0,
        "loop": True,
        "channels": {"body": "dy", "arm": "dy"},
        "curves": {
            # 耸肩：body + arm 一起微升
            "body": [_kf(0.0, 0.0, "inout"), _kf(1.0, 0.02, "inout"),
                     _kf(2.0, 0.0, "inout")],
            "arm": [_kf(0.0, 0.0, "inout"), _kf(1.0, 0.015, "inout"),
                    _kf(2.0, 0.0, "inout")],
        },
    },
    {
        "id": "bow",
        "name": "鞠躬",
        "dur": 3.0,
        "loop": True,
        "channels": {"head": "rot", "body": "rot"},
        "curves": {
            # 鞠躬：head + body 大角度（rot 通道）
            "head": [_kf(0.0, 0.0, "inout"), _kf(1.5, 18.0, "inout"),
                     _kf(3.0, 0.0, "inout")],
            "body": [_kf(0.0, 0.0, "inout"), _kf(1.5, 25.0, "inout"),
                     _kf(3.0, 0.0, "inout")],
        },
    },
]

_SEED_EXPRESSIONS = [
    {
        "id": "smile",
        "name": "微笑",
        "fade": 0.4,
        "pose": {
            "head": {"rot": 3.0, "dy": -0.01, "dscale": 0.02},
            "body": {"rot": 0.0, "dy": 0.0, "dscale": 0.02},
        },
    },
    {
        "id": "sad",
        "name": "难过",
        "fade": 0.5,
        "pose": {
            "head": {"rot": -4.0, "dy": 0.02, "dscale": -0.02},
            "body": {"rot": 0.0, "dy": 0.01, "dscale": -0.01},
        },
    },
    {
        "id": "angry",
        "name": "生气",
        "fade": 0.3,
        "pose": {
            "head": {"rot": 6.0, "dy": -0.02, "dscale": 0.0},
            "body": {"rot": 3.0, "dy": 0.0, "dscale": 0.0},
        },
    },
    {
        "id": "surprised",
        "name": "惊讶",
        "fade": 0.25,
        "pose": {
            "head": {"rot": -8.0, "dy": -0.03, "dscale": 0.05},
            "body": {"rot": 0.0, "dy": 0.0, "dscale": 0.03},
        },
    },
    {
        "id": "shy",
        "name": "害羞",
        "fade": 0.4,
        "pose": {
            "head": {"rot": -5.0, "dy": 0.01, "dscale": -0.02},
            "body": {"rot": 0.0, "dy": 0.0, "dscale": -0.01},
        },
    },
]


# ---------------- 路径 / 序列化 ----------------
def _table_dir():
    """数据目录：env MUPPET_TEMPLATES_DIR 优先，否则模块目录/templates/webgal。"""
    env = os.environ.get("MUPPET_TEMPLATES_DIR")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "templates", "webgal")


def table_path():
    """motion_table.json 的绝对路径。"""
    return os.path.join(_table_dir(), "motion_table.json")


def _is_cubism(m):
    return isinstance(m, dict) and str(m.get("kind") or "").startswith("cubism")


def _to_json(motions, expressions, namespaces=None, recommended=None):
    """Python 表 -> 可 JSON 序列化结构（keyframe 转 [t,v,ease] 数组）。
    cubism 条目按 kind 分流（无 curves/pose 要求）；元数据非空才写键
    （种子往返保持 {motions,expressions} 两键旧契约）。"""
    m_out = []
    for m in motions:
        if _is_cubism(m):
            mm = {"id": m["id"], "name": m.get("name", m["id"]),
                  "kind": m["kind"], "dur": float(m.get("dur") or 0.0),
                  "loop": bool(m.get("loop", False))}
            if m.get("ns"):
                mm["ns"] = m["ns"]
            if m.get("common"):
                mm["common"] = True
            m_out.append(mm)
            continue
        mm = {"id": m["id"], "name": m["name"], "dur": m["dur"],
              "loop": bool(m.get("loop", True))}
        if m.get("channels"):
            mm["channels"] = dict(m["channels"])
        cur = {}
        for role, track in (m.get("curves") or {}).items():
            cur[role] = [[kf["t"], kf["v"], kf.get("ease", "linear")]
                         for kf in track]
        if cur:
            mm["curves"] = cur
        m_out.append(mm)
    e_out = []
    for e in expressions:
        if _is_cubism(e):
            ee = {"id": e["id"], "name": e.get("name", e["id"]),
                  "kind": e["kind"], "fade": float(e.get("fade") or 0.0)}
            if e.get("ns"):
                ee["ns"] = e["ns"]
            if e.get("common"):
                ee["common"] = True
            e_out.append(ee)
            continue
        ee = {"id": e["id"], "name": e["name"], "fade": e["fade"],
              "pose": {role: dict(snap) for role, snap in (e.get("pose") or {}).items()}}
        e_out.append(ee)
    out = {"motions": m_out, "expressions": e_out}
    if namespaces:
        out["namespaces"] = dict(namespaces)
    if recommended:
        out["recommended"] = list(recommended)
    return out


def _cubism_entry(raw, need_kind):
    """cubism 条目校验：id 非空 str；kind 必须 cubism/cubism_exp；
    ns 从 id 前缀推断。非法返回 None（调用方判结构非法）。"""
    mid = raw.get("id")
    if not isinstance(mid, str) or not mid:
        return None
    kind = str(raw.get("kind") or "")
    if kind not in ("cubism", "cubism_exp"):
        return None
    out = {"id": mid, "name": str(raw.get("name") or mid), "kind": kind,
           "dur": float(raw.get("dur") or 0.0),
           "loop": bool(raw.get("loop", False))}
    if kind == "cubism_exp":
        out["fade"] = float(raw.get("fade") or 0.0)
        out["loop"] = False
    ns = str(raw.get("ns") or "")
    if not ns and "/" in mid:
        ns = mid.split("/")[0]
    if ns:
        out["ns"] = ns
    if raw.get("common"):
        out["common"] = True
    return out


def _from_json(raw):
    """JSON 结构 -> (motions, expressions, meta)；结构非法返回 None。
    meta = {"namespaces": dict, "recommended": list}（缺省空）。
    cubism 条目免 curves/pose；程序化条目沿用 C1 严格校验。"""
    if not isinstance(raw, dict):
        return None
    mraw, eraw = raw.get("motions"), raw.get("expressions")
    if not isinstance(mraw, list) or not isinstance(eraw, list):
        return None
    motions, expressions = [], []
    meta = {"namespaces": {}, "recommended": []}
    ns = raw.get("namespaces")
    if ns is not None and not isinstance(ns, dict):
        return None
    if isinstance(ns, dict):
        meta["namespaces"] = {str(k): v for k, v in ns.items()
                              if isinstance(v, dict)}
    rec = raw.get("recommended")
    if rec is not None and not isinstance(rec, list):
        return None
    if isinstance(rec, list):
        meta["recommended"] = [str(x) for x in rec if isinstance(x, str)]
    try:
        for m in mraw:
            if not isinstance(m, dict):
                return None
            mid = m.get("id")
            if not isinstance(mid, str) or not mid:
                return None
            if _is_cubism(m):
                ce = _cubism_entry(m, True)
                if ce is None:
                    return None
                motions.append(ce)
                continue
            if str(m.get("kind") or ""):
                return None          # 未知 kind：结构非法，整表回退
            curves = m.get("curves")
            if not isinstance(curves, dict):
                return None
            cur = {}
            for role, track in curves.items():
                if role not in ROLES or not isinstance(track, list):
                    return None
                ks = []
                for kf in track:
                    if not isinstance(kf, (list, tuple)) or len(kf) < 3:
                        return None
                    ks.append(_kf(kf[0], kf[1], kf[2]))
                ks.sort(key=lambda x: x["t"])
                cur[role] = ks
            mm = {"id": mid, "name": str(m.get("name") or mid),
                  "dur": float(m.get("dur") or 0.0),
                  "loop": bool(m.get("loop", True))}
            ch = m.get("channels")
            if isinstance(ch, dict):
                mm["channels"] = {r: c for r, c in ch.items()
                                  if c in ("rot", "dy")}
            mm["curves"] = cur
            motions.append(mm)
        for e in eraw:
            if not isinstance(e, dict):
                return None
            eid = e.get("id")
            if not isinstance(eid, str) or not eid:
                return None
            if _is_cubism(e):
                ce = _cubism_entry(e, True)
                if ce is None:
                    return None
                expressions.append(ce)
                continue
            if str(e.get("kind") or ""):
                return None
            pose = e.get("pose")
            if not isinstance(pose, dict):
                return None
            pp = {}
            for role, snap in pose.items():
                if role not in ROLES or not isinstance(snap, dict):
                    return None
                pp[role] = {"rot": float(snap.get("rot", 0.0)),
                            "dy": float(snap.get("dy", 0.0)),
                            "dscale": float(snap.get("dscale", 0.0))}
            expressions.append({"id": eid, "name": str(e.get("name") or eid),
                                "fade": float(e.get("fade") or 0.0),
                                "pose": pp})
    except (TypeError, ValueError):
        return None
    return motions, expressions, meta


# ---------------- 加载 / 重载 ----------------
# MOTIONS / EXPRESSIONS 是模块级表视图，_load 每次重载后整体替换，
# 保证 reload()/mtime 惰性检测后所有读表入口看到最新表。
# NAMESPACES / RECOMMENDED：cubism 层元数据（K4），种子表恒空。
# _MIDX / _EIDX：id->条目索引（900+ 条目 get O(1)），随 _load 重建。
MOTIONS = []
EXPRESSIONS = []
NAMESPACES = {}
RECOMMENDED = []
_MIDX = {}
_EIDX = {}
_MTIME = 0.0
_FSIZE = 0


def _stat_pair():
    """(mtime, size)。NTFS 的 mtime 有 ~15.6ms tick 粒度：同一 tick 内
    "种子写出->用户编辑"两次落盘 mtime 可能相同，只比 mtime 会漏 reload
    （motion_table_test 10f 实测抖动根因）；size 入比较后确定性。"""
    try:
        st = os.stat(table_path())
        return st.st_mtime, st.st_size
    except Exception:
        return 0.0, 0


def _rebuild_index():
    global _MIDX, _EIDX
    _MIDX = {m["id"]: m for m in MOTIONS}
    _EIDX = {e["id"]: e for e in EXPRESSIONS}


def _write_seed():
    """把出厂种子写到数据文件（self-seed），用户即可编辑。"""
    try:
        d = _table_dir()
        os.makedirs(d, exist_ok=True)
        data = _to_json(_SEED_MOTIONS, _SEED_EXPRESSIONS)
        with open(table_path(), "w", encoding="utf8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        warnings.append("motion_table 自种子写出失败: %r" % (e,))


def _apply(parsed, fallback):
    """_from_json 结果落全局；None 走回退。"""
    global MOTIONS, EXPRESSIONS, NAMESPACES, RECOMMENDED
    if parsed is None:
        MOTIONS, EXPRESSIONS = fallback
        NAMESPACES, RECOMMENDED = {}, []
    else:
        MOTIONS, EXPRESSIONS, meta = parsed
        NAMESPACES = meta.get("namespaces") or {}
        RECOMMENDED = meta.get("recommended") or []
    _rebuild_index()


def _load(force=False):
    """读数据文件；缺失 self-seed，坏 JSON 回退内置并记 warning。"""
    global _MTIME, _FSIZE
    path = table_path()
    mt, fs = _stat_pair()
    if not force and (mt, fs) == (_MTIME, _FSIZE):
        return
    fallback = (copy.deepcopy(_SEED_MOTIONS),
                copy.deepcopy(_SEED_EXPRESSIONS))
    try:
        with open(path, encoding="utf8") as f:
            raw = json.load(f)
        parsed = _from_json(raw)
        if parsed is None:
            warnings.append("motion_table 结构非法，回退内置表: %s" % path)
        _apply(parsed, fallback)
    except Exception as e:
        if not os.path.isfile(path):
            # 文件不存在 -> self-seed 后重读
            warnings.append("motion_table 首次运行，已写出出厂表: %s" % path)
            _write_seed()
            try:
                with open(path, encoding="utf8") as f:
                    raw = json.load(f)
                _apply(_from_json(raw), fallback)
            except Exception as e2:
                warnings.append("motion_table 自种子后仍不可用: %r" % (e2,))
                _apply(None, fallback)
        else:
            warnings.append("motion_table 读取失败，回退内置表: %r" % (e,))
            _apply(None, fallback)
    _MTIME, _FSIZE = mt, fs


def reload():
    """显式重读数据文件（mtime 变化也走这里）。"""
    _load(force=True)


def _ensure_fresh():
    """读表入口调用：文件 mtime 或 size 变了就自动 reload。"""
    mt, fs = _stat_pair()
    if (mt, fs) != (_MTIME, _FSIZE):
        _load(force=True)


# 模块加载即加载一次（不抛异常，任何失败都回退内置）
try:
    _load(force=True)
except Exception as _e:                       # pragma: no cover
    warnings.append("motion_table 初始化异常: %r" % (_e,))
    _apply((copy.deepcopy(_SEED_MOTIONS), copy.deepcopy(_SEED_EXPRESSIONS),
            None), (copy.deepcopy(_SEED_MOTIONS),
                    copy.deepcopy(_SEED_EXPRESSIONS)))


# ---------------- 查询 ----------------
def motion_ids():
    """动作 id 列表（保持表顺序，含 cubism 键）。"""
    _ensure_fresh()
    return [m["id"] for m in MOTIONS]


def expression_ids():
    """表情 id 列表（保持表顺序，含 cubism 键）。"""
    _ensure_fresh()
    return [e["id"] for e in EXPRESSIONS]


def get_motion(mid):
    """按 id 取动作（返回副本，防调用方改表）；坏 id 抛 KeyError。
    cubism 条目同样可取（带 kind/ns/common，无 curves）。"""
    _ensure_fresh()
    m = _MIDX.get(mid)
    if m is None:
        raise KeyError(mid)
    return copy.deepcopy(m)


def get_expression(eid):
    """按 id 取表情（返回副本）；坏 id 抛 KeyError。"""
    _ensure_fresh()
    e = _EIDX.get(eid)
    if e is None:
        raise KeyError(eid)
    return copy.deepcopy(e)


def catalog():
    """DSH 直读的精简清单（不含 curves/pose，只留展示字段）。
    表带 cubism 元数据时追加 namespaces/recommended 两键；种子表维持
    旧契约 keys=={motions,expressions}（motion_table_test 断言依赖）。"""
    _ensure_fresh()

    def _row(x, extra):
        r = {"id": x["id"], "name": x["name"]}
        r.update(extra)
        if x.get("kind"):
            r["kind"] = x["kind"]
            if x.get("ns"):
                r["ns"] = x["ns"]
            if x.get("common"):
                r["common"] = True
        return r

    out = {
        "motions": [_row(m, {"dur": m.get("dur", 0.0),
                             "loop": bool(m.get("loop", False))})
                    for m in MOTIONS],
        "expressions": [_row(e, {"fade": e.get("fade", 0.0)})
                        for e in EXPRESSIONS],
    }
    if NAMESPACES:
        out["namespaces"] = copy.deepcopy(NAMESPACES)
    if RECOMMENDED:
        out["recommended"] = list(RECOMMENDED)
    return out


# ---------------- 求值 ----------------
def _channel(m, role):
    ch = (m.get("channels") or {}).get(role)
    return ch if ch in ("rot", "dy") else ROLE_CHANNEL.get(role, "rot")


def eval_motion(mid, t):
    """动作在 t 时刻的各部件增量：{role: {"rot": float, "dy": float}}。

    loop 动作 t 取模（t=dur 与 t=0 连续）；非 loop 钳到 [0, dur]。
    cubism 条目返回 {}（引擎侧播放，本模块不求值；消费方自然跳过）。
    坏 id 抛 KeyError。
    """
    m = get_motion(mid)
    if _is_cubism(m):
        return {}
    dur = float(m.get("dur") or 0.0)
    tt = float(t)
    if m.get("loop"):
        tt = tt % dur if dur > 0 else 0.0
    else:
        tt = max(0.0, min(tt, dur))
    out = {}
    for role, track in (m.get("curves") or {}).items():
        v = anim.eval_track(track, tt)
        if v is None:
            continue
        d = {"rot": 0.0, "dy": 0.0}
        d[_channel(m, role)] = float(v)
        out[role] = d
    return out


def eval_expression(eid, progress):
    """表情在 progress（0=中性，1=完整 pose）时的快照：线性插值。

    cubism_exp 条目返回 {}（引擎侧播放）。坏 id 抛 KeyError。
    """
    e = get_expression(eid)
    if _is_cubism(e):
        return {}
    p = float(progress)
    if p < 0.0:
        p = 0.0
    elif p > 1.0:
        p = 1.0
    out = {}
    for role, snap in (e.get("pose") or {}).items():
        out[role] = {
            "rot": float(snap.get("rot", 0.0)) * p,
            "dy": float(snap.get("dy", 0.0)) * p,
            "dscale": float(snap.get("dscale", 0.0)) * p,
        }
    return out


__all__ = [
    "ROLES", "ROLE_CHANNEL", "ROT_MAX", "DY_MAX", "DSCALE_MAX",
    "MOTIONS", "EXPRESSIONS", "NAMESPACES", "RECOMMENDED", "warnings",
    "table_path", "reload",
    "motion_ids", "expression_ids", "get_motion", "get_expression",
    "catalog", "eval_motion", "eval_expression",
]
