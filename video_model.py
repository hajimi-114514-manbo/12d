# -*- coding: utf-8 -*-
"""视频工程数据模型（v3.0.0 第一阶段）。

纯 Python、无 Qt/cv2 依赖，可直接单测；所有编辑操作**原地修改**
（与工作台约定一致：先改数据、后 push history）。

结构（全部 JSON 可序列化）：
    proj = {
      "type": "video_edit", "version": 1,
      "width": 1920, "height": 1080, "fps": 30,
      "tracks": [
        {"id": "v1", "kind": "video"|"audio", "name": "视频轨 1",
         "hidden": False, "locked": False,
         "clips": [clip, ...]},
      ]
    }
    clip = {
      "id": "c1",
      "source": {"kind": "image"|"video"|"puppet"|"audio",
                 "path": "...", "w": int, "h": int,
                 "fps": float, "src_dur": float},
      "start": 0.0,     # 时间轴起点（秒）
      "src_in": 0.0,    # 素材内部偏移（秒）；image 恒 0
      "dur":  3.2,      # 时间轴时长（秒）
      "speed": 1.0,     # 第一阶段恒 1.0（字段预留）
      # v3.4.0 A1：舞台布局（WebGAL/DSH 适配）。所有消费方一律经 layout_at()
      #   取"合并默认后"的完整 dict，禁止直接读 clip["layout"] 原始值——
      #   C 方案的关键帧动画将来只改 layout_at 内部，不动数据结构。
      "layout": {"x":0.5, "y":0.5, "scale":1.0, "rot":0.0},
      #   x/y：素材中心点在舞台上的归一化坐标（0..1，允许越界±，做画面外进出）
      #   scale：对 fit 基准（等比缩放铺满画布）的倍率，0.1..10
      #   rot：绕素材中心旋转角度（度，逆时针），rot==0 走纯缩放平移快路径
      #   未识别的键（opacity/anchor/crop/blend/keyframes…）sanitize 时**原样透传**
      #   保留，供后续方案使用，不校验不改动。
      #   C2：motion / expression（可选，仅 puppet/立绘片段有意义）
      #   "motion": "idle",        # 预置动作 id，来自 motion_table（C1）
      #   "motion": "taki/idle01", # 或 Cubism 键名形态（G5：素材库 meta.motions）
      #   "expression": "smile",   # 预置表情 id，来自 motion_table（C1）
      #   sanitize 时：str 且在 motion_table 的 id 集合内，**或**形如 Cubism
      #   键名（含 "/"、长度≤64、无空白/分号/引号/冒号，见 _motion_value_ok）
      #   才保留；空/缺失/脏值一律**丢键不丢 clip**（表不可用时宽松透传
      #   非空字符串）。
    }

    dialogue = [{"t": float, "dur": float, "who": str, "text": str,
                 # H2a：台词级动作/表情（可选；motion_table id 或 Cubism 键名，
                 # 脏值丢字段不丢台词。导出在该台词行前 emit changeFigure 切换行）
                 "motion": str, "expression": str}, ...]

    comments = [{"t": float, "text": str}, ...]
                 # I3（task-67）：脚本注释行载体（导入端 `//xxx` 与 `;xxx;`
                 # 都收进来；导出端经 _comment() 转引擎合法 `;xxx;`）。缺省 []
       # A4：台词表。WebGAL 导出与舞台字幕共用；t 为出现时刻（秒），dur 时长。

规则（第一阶段拍板）：
  - 同一轨内 clip **不允许重叠**：移动/trim 撞邻接直接拒绝（返回错误），
    不做 ripple——可预测比花哨重要。
  - image 是无限长素材：src_in 恒 0，trim 两端只动 start/dur。
  - video/puppet/audio 受 src_dur 上限约束：src_in + dur <= src_dur + EPS。
  - 时间吸附精度 0.01s；重叠判定容差 EPS=0.005s。
"""
import copy
import re

# C2：预置动作/表情表（C1 纯数据模块）。表缺失时整体宽松，不硬依赖。
try:
    import motion_table as _MT
except Exception:                              # pragma: no cover
    _MT = None

EPS = 0.005            # 重叠/边界判定容差（秒）
MIN_DUR = 0.05         # clip 最短时长（秒）
SNAP_STEP = 0.01       # 吸附/对齐精度（秒）

PROJECT_TYPE = "video_edit"

# v3.4.0 A1 舞台布局默认值（契约见模块 docstring）。scale 合法区间钳位用。
LAYOUT_DEFAULT = {"x": 0.5, "y": 0.5, "scale": 1.0, "rot": 0.0}
_SCALE_MIN, _SCALE_MAX = 0.1, 10.0


def default_layout():
    """新 clip 用的默认 layout（每次新 dict，防共享可变）。"""
    return dict(LAYOUT_DEFAULT)


def _sanitize_layout(raw):
    """逐字段校验补默认；未识别键**原样保留透传**（C 方案余量）。
    - x/y：有限浮点保留（可越界±），脏值回默认 0.5
    - scale：有限且 >0，钳到 [0.1,10]；脏值回 1.0
    - rot：有限浮点（度），脏值回 0.0
    非 dict / None -> 纯默认。绝不抛。"""
    out = dict(LAYOUT_DEFAULT)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k == "x" or k == "y":
                n = _num(v, LAYOUT_DEFAULT[k])
                out[k] = n
            elif k == "scale":
                n = _num(v, 1.0)
                out[k] = min(_SCALE_MAX, max(_SCALE_MIN, n))
            elif k == "rot":
                out[k] = _num(v, 0.0)
            else:                       # opacity/anchor/crop/blend/keyframes/...
                out[k] = v              # 不校验不改写，原样带走
    return out


def layout_at(clip, t=None):
    """clip 在 t 时刻的完整布局 dict（合并默认后）。**消费方唯一入口**。

    A1：纯静态，忽略 t。C 方案的 layout 关键帧只改本函数内部
    （从 clip["layout"]["keyframes"] 采样插值覆盖对应通道），签名与
    返回结构不变，调用方零改动。"""
    if not isinstance(clip, dict):
        return dict(LAYOUT_DEFAULT)
    return _sanitize_layout(clip.get("layout"))


# ---------------- 构造 ----------------
def new_project(width=1920, height=1080, fps=30):
    """默认一条视频轨 + 一条音频轨。"""
    return {
        "type": PROJECT_TYPE, "version": 1,
        "width": int(width), "height": int(height), "fps": float(fps),
        "dialogue": [],
        "tracks": [
            {"id": "v1", "kind": "video", "name": "视频轨 1",
             "hidden": False, "locked": False, "clips": []},
            {"id": "a1", "kind": "audio", "name": "音频轨 1",
             "hidden": False, "locked": False, "clips": []},
        ],
    }


def new_source(kind, path, w=0, h=0, fps=0.0, src_dur=0.0):
    return {"kind": kind, "path": str(path), "w": int(w), "h": int(h),
            "fps": float(fps), "src_dur": float(src_dur)}


def new_clip(source, start, dur, src_in=0.0, clip_id=None):
    return {"id": clip_id or "c1", "source": dict(source),
            "start": round(float(start), 3), "src_in": round(float(src_in), 3),
            "dur": round(float(dur), 3), "speed": 1.0,
            "layout": default_layout()}


def next_id(prefix, existing):
    """'c' + 现有最大编号+1，保证不与 existing 冲突。"""
    n = 1
    used = set(str(x) for x in existing)
    while ("%s%d" % (prefix, n)) in used:
        n += 1
    return "%s%d" % (prefix, n)


def next_clip_id(proj):
    return next_id("c", [c["id"] for t in proj["tracks"] for c in t["clips"]])


def next_track_id(proj, kind):
    p = "v" if kind == "video" else "a"
    return next_id(p, [t["id"] for t in proj["tracks"] if t["kind"] == kind])


def add_track(proj, kind, name=None):
    """追加轨道；kind 只允许 video/audio。返回 (ok, msg, track_id)。"""
    if kind not in ("video", "audio"):
        return False, "轨道类型必须是 video/audio", None
    tid = next_track_id(proj, kind)
    cnt = sum(1 for t in proj["tracks"] if t["kind"] == kind) + 1
    label = name or ("%s轨 %d" % ("视频" if kind == "video" else "音频", cnt))
    proj["tracks"].append({"id": tid, "kind": kind, "name": label,
                           "hidden": False, "locked": False, "clips": []})
    return True, "", tid


# ---------------- 查询 ----------------
def clip_end(clip):
    return clip["start"] + clip["dur"]


def find_clip(proj, clip_id):
    """-> (track, clip) 或 (None, None)"""
    for t in proj["tracks"]:
        for c in t["clips"]:
            if c["id"] == clip_id:
                return t, c
    return None, None


def find_track(proj, track_id):
    for t in proj["tracks"]:
        if t["id"] == track_id:
            return t
    return None


def project_duration(proj):
    end = 0.0
    for t in proj["tracks"]:
        for c in t["clips"]:
            end = max(end, clip_end(c))
    return round(end, 3)


def clips_at(proj, t, kind=None):
    out = []
    for tr in proj["tracks"]:
        if kind and tr["kind"] != kind:
            continue
        for c in tr["clips"]:
            if c["start"] - EPS <= t < clip_end(c) - EPS:
                out.append(c)
    return out


def top_video_clip_at(proj, t):
    """最上层可见视频轨在 t 时刻的 clip（后加的轨在上）。"""
    for tr in reversed(proj["tracks"]):
        if tr["kind"] != "video" or tr.get("hidden"):
            continue
        for c in tr["clips"]:
            if c["start"] - EPS <= t < clip_end(c) - EPS:
                return c
    return None


def snap_points(proj, exclude_id=None):
    """所有 clip 边界 + 0，供吸附。"""
    pts = {0.0}
    for t in proj["tracks"]:
        for c in t["clips"]:
            if c["id"] == exclude_id:
                continue
            pts.add(round(c["start"], 3))
            pts.add(round(clip_end(c), 3))
    return sorted(pts)


def snap_time(points, t, eps=SNAP_STEP * 3):
    """把 t 吸到最近的边界点（eps 留浮点余量：3.02-3.0=0.02000000000000046）。"""
    best, bd = t, eps
    for p in points:
        d = abs(p - t)
        if d <= bd:
            best, bd = p, d
    return round(best, 3)


def _overlap(a0, a1, b0, b1):
    return a0 + EPS < b1 - EPS and b0 + EPS < a1 - EPS


def _clash(proj, track, start, dur, exclude_id=None):
    """同轨重叠检查 -> 冲突 clip 或 None。"""
    for c in track["clips"]:
        if c["id"] == exclude_id:
            continue
        if _overlap(start, start + dur, c["start"], clip_end(c)):
            return c
    return None


def src_dur_of(clip):
    return float(clip["source"].get("src_dur") or 0.0)


def _src_limit_ok(clip, src_in, dur):
    """素材时长约束：image 无限长恒 OK；其余 src_in+dur <= src_dur+EPS。"""
    if clip["source"]["kind"] == "image":
        return True
    sd = src_dur_of(clip)
    if sd <= 0:          # 未知长度素材（探测失败）：不拦
        return True
    return src_in + dur <= sd + EPS


# ---------------- 编辑（原地改，返回 (ok, msg)） ----------------
def add_clip(proj, track_id, clip):
    tr = find_track(proj, track_id)
    if tr is None:
        return False, "轨道不存在: %s" % track_id
    if tr.get("locked"):
        return False, "轨道已锁定"
    if clip["dur"] < MIN_DUR - EPS:
        return False, "clip 太短（<%.2fs）" % MIN_DUR
    if _clash(proj, tr, clip["start"], clip["dur"]):
        return False, "与同轨 clip 重叠"
    clip = dict(clip)
    clip["id"] = next_clip_id(proj)
    tr["clips"].append(clip)
    return True, clip["id"]


def remove_clip(proj, clip_id):
    tr, c = find_clip(proj, clip_id)
    if c is None:
        return False, "clip 不存在"
    if tr.get("locked"):
        return False, "轨道已锁定"
    tr["clips"].remove(c)
    return True, ""


def move_clip(proj, clip_id, new_start, new_track_id=None):
    """同轨/跨轨移动。跨轨换轨时校验目标轨类型不限、重叠不撞。"""
    tr, c = find_clip(proj, clip_id)
    if c is None:
        return False, "clip 不存在"
    if tr.get("locked"):
        return False, "轨道已锁定"
    dst = find_track(proj, new_track_id) if new_track_id else tr
    if dst is None:
        return False, "目标轨道不存在"
    if dst.get("locked"):
        return False, "目标轨道已锁定"
    new_start = round(max(0.0, float(new_start)), 3)
    if _clash(proj, dst, new_start, c["dur"], exclude_id=clip_id):
        return False, "与目标轨 clip 重叠"
    if dst is not tr:
        tr["clips"].remove(c)
        dst["clips"].append(c)
    c["start"] = new_start
    return True, ""


def trim_clip(proj, clip_id, side, new_value):
    """side='start'：改起点（联动 src_in）；side='end'：改终点。
    new_value 是**时间轴坐标**（秒）。两端都保持素材偏移合法。"""
    tr, c = find_clip(proj, clip_id)
    if c is None:
        return False, "clip 不存在"
    if tr.get("locked"):
        return False, "轨道已锁定"
    v = round(max(0.0, float(new_value)), 3)
    if side == "start":
        d = v - c["start"]                      # >0 左端右移，<0 左扩
        new_in = c["src_in"] + d                # image 恒回 0
        if c["source"]["kind"] == "image":
            new_in = 0.0
        if new_in < -EPS:
            return False, "不能拖到 0 之前"
        new_dur = c["dur"] - d
        if new_dur < MIN_DUR - EPS:
            return False, "trim 后太短"
        if not _src_limit_ok(c, max(0.0, new_in), new_dur):
            return False, "超出素材长度"
        if _clash(proj, tr, v, new_dur, exclude_id=clip_id):
            return False, "与邻接 clip 重叠"
        c["start"], c["src_in"], c["dur"] = v, round(max(0.0, new_in), 3), new_dur
        return True, ""
    elif side == "end":
        new_dur = v - c["start"]
        if new_dur < MIN_DUR - EPS:
            return False, "trim 后太短"
        if not _src_limit_ok(c, c["src_in"], new_dur):
            return False, "超出素材长度"
        if _clash(proj, tr, c["start"], new_dur, exclude_id=clip_id):
            return False, "与邻接 clip 重叠"
        c["dur"] = round(new_dur, 3)
        return True, ""
    return False, "side 必须是 start/end"


def split_clip(proj, clip_id, t):
    """在时间轴 t 处切成两段。t 必须严格在 clip 内部。返回 (ok, msg, 右段id)。"""
    tr, c = find_clip(proj, clip_id)
    if c is None:
        return False, "clip 不存在", None
    if tr.get("locked"):
        return False, "轨道已锁定", None
    left = t - c["start"]
    right = clip_end(c) - t
    if left < MIN_DUR - EPS or right < MIN_DUR - EPS:
        return False, "切分后片段太短", None
    if c["source"]["kind"] != "image" and not _src_limit_ok(c, c["src_in"] + left, right):
        return False, "超出素材长度", None
    right_clip = new_clip(c["source"], t, right,
                          src_in=(0.0 if c["source"]["kind"] == "image"
                                  else c["src_in"] + left),
                          clip_id=next_clip_id(proj))
    right_clip["layout"] = copy.deepcopy(c["layout"]) if "layout" in c \
        else default_layout()          # 切分两半是同一画面区域：布局继承
    c["dur"] = round(left, 3)
    i = tr["clips"].index(c)
    tr["clips"].insert(i + 1, right_clip)
    return True, "", right_clip["id"]


def delete_range(proj, t0, t1):
    """剪映「删除选中」简化版：把 [t0,t1) 覆盖部分从 clip 上抠掉
    （两侧留头尾则一分为二）。第一阶段供工具栏删除按钮用。"""
    if t1 - t0 < MIN_DUR - EPS:
        return 0
    n = 0
    for tr in proj["tracks"]:
        if tr.get("locked"):
            continue
        out = []
        for c in list(tr["clips"]):
            if not _overlap(c["start"], clip_end(c), t0, t1):
                out.append(c)
                continue
            n += 1
            pre = t0 - c["start"]
            post = clip_end(c) - t1
            if pre >= MIN_DUR - EPS:
                c["dur"] = round(pre, 3)
                out.append(c)
            if post >= MIN_DUR - EPS:
                # 右段：起点 t1，素材偏移 = 原 src_in + 被抠掉的左段时长
                rc = new_clip(c["source"], t1, post,
                              src_in=max(0.0, c["src_in"] + (t1 - c["start"])),
                              clip_id=next_clip_id(proj))
                if "layout" in c:               # 布局同 split：继承
                    rc["layout"] = copy.deepcopy(c["layout"])
                out.append(rc)
        tr["clips"] = out
    return n


def clone_project(proj):
    return copy.deepcopy(proj)


# ---------------- 序列化 / 校验 ----------------
_KINDS = ("image", "video", "puppet", "audio")


def _num(x, dft=0.0):
    try:
        v = float(x)
        return v if v == v and abs(v) != float("inf") else dft   # nan/inf 回退
    except (TypeError, ValueError):
        return dft


def _sanitize_comments(raw):
    """工程级注释载体 comments=[{t,text}]（I3/task-67）：导入端 `//` 与
    `;xxx;` 注释行内容保留，导出端转合法 `;xxx;`。脏项丢弃，绝不抛。"""
    out = []
    if not isinstance(raw, list):
        return out
    for it in raw:
        if not isinstance(it, dict):
            continue
        t = _num(it.get("t"), -1.0)
        text = it.get("text")
        if t < 0 or not isinstance(text, str) or not text.strip():
            continue
        out.append({"t": round(t, 3), "text": text.strip()})
    return out


def _sanitize_dialogue(raw):
    """台词表 dialogue：逐项校验 t/dur 浮点 >=0、who/text 字符串；脏项丢弃。

    A4：WebGAL 导出与舞台字幕共用。缺省/非 list 一律 []，绝不抛。
    H2a（task-47）：条目可选 motion/expression（台词级动作表情），复用
    _motion_value_ok 校验（motion_table id 或 Cubism 键名形态）；**脏值
    只丢字段不丢台词**。
    """
    out = []
    if not isinstance(raw, list):
        return out
    for it in raw:
        if not isinstance(it, dict):
            continue
        t = _num(it.get("t"), -1.0)
        dur = _num(it.get("dur"), -1.0)
        if t < 0 or dur < 0:
            continue
        who, text = it.get("who"), it.get("text")
        if not isinstance(who, str) or not isinstance(text, str):
            continue
        ent = {"t": round(t, 3), "dur": round(dur, 3),
               "who": who, "text": text}
        mv = _sanitize_preset(it.get("motion"),
                              lambda: _MT.motion_ids())
        ex = _sanitize_preset(it.get("expression"),
                              lambda: _MT.expression_ids())
        if mv:
            ent["motion"] = mv
        if ex:
            ent["expression"] = ex
        out.append(ent)
    return out


def sanitize_project(obj):
    """脏文件容错：逐字段校验 + 补默认。不合法返回 None。"""
    if not isinstance(obj, dict) or obj.get("type") != PROJECT_TYPE:
        return None
    # 画布参数：脏值/越界一律回默认（宽容，别因一个坏字段拒整个工程）
    w = int(_num(obj.get("width"), 1920))
    h = int(_num(obj.get("height"), 1080))
    fps = _num(obj.get("fps"), 30.0)
    if not (16 <= w <= 7680):
        w = 1920
    if not (16 <= h <= 7680):
        h = 1080
    if not (1 <= fps <= 240):
        fps = 30.0
    proj = {"type": PROJECT_TYPE, "version": 1, "width": w, "height": h,
            "fps": fps, "tracks": []}
    tracks = obj.get("tracks")
    if not isinstance(tracks, list):
        return None
    seen_ids = set()
    for tr in tracks:
        if not isinstance(tr, dict) or tr.get("kind") not in ("video", "audio"):
            continue
        tid = str(tr.get("id") or "")
        if not tid or tid in seen_ids:
            tid = next_track_id(proj, tr["kind"])   # 冲突/缺失：重排而非丢轨
        seen_ids.add(tid)
        clips = []
        for c in (tr.get("clips") or []):
            cc = _sanitize_clip(c, seen_clip_ids=seen_ids)
            if cc:
                clips.append(cc)
        proj["tracks"].append({
            "id": tid, "kind": tr["kind"],
            "name": str(tr.get("name") or tid),
            "hidden": bool(tr.get("hidden", False)),
            "locked": bool(tr.get("locked", False)),
            "clips": clips})
    if not proj["tracks"]:
        proj["tracks"] = new_project()["tracks"]     # 至少保证有默认双轨
    # A4：台词表（WebGAL 导出与舞台字幕共用）
    proj["dialogue"] = _sanitize_dialogue(obj.get("dialogue"))
    # I3（task-67）：工程级注释载体（导入保留 → 导出转合法语法）
    proj["comments"] = _sanitize_comments(obj.get("comments"))
    return proj


# G5（task-46）：Cubism 动作/表情键名形态（`角色/动作` 命名空间形态）。
# 自检规则，不依赖 motion_table：含 "/"、长度≤64、无空白/分号/引号/冒号。
_CUBISM_KEY_BAD_RE = re.compile(r"[\s;:\"']")


def _cubism_key_ok(v):
    """值是否形如 Cubism 键名（anon/idle01、taki/smile_02 …）。"""
    return ("/" in v and len(v) <= 64
            and not _CUBISM_KEY_BAD_RE.search(v))


def _motion_value_ok(v, ids=None):
    """motion/expression 保留判定的纯函数（G5，供 _sanitize_preset 与测试共用）。

    规则=并集：值命中 motion_table id 集（ids，C2 现行为）**或**值为 Cubism
    键名形态（G5 放行，AI 直接引用素材库 meta.motions 的键名）。两者都不
    满足 -> False（脏值防线不降）。ids 传 None/空 = 只按键名形态判。
    表不可用时的"非空串透传"是 _sanitize_preset 的既有兜底，不在本函数。"""
    if not isinstance(v, str) or not v.strip():
        return False
    if ids and v in ids:
        return True
    return _cubism_key_ok(v)


def _sanitize_preset(raw, ids_fn):
    """motion/expression 透传校验：C2 表内 id 或 G5 Cubism 键名才保留，否则 None。

    motion_table 不可用（import 失败）时宽松透传非空字符串，绝不因表缺失
    拒绝整个 clip（G5 的键名形态在此兜底下同样放行）。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    if _MT is None:
        return raw
    try:
        ids = ids_fn()
    except Exception:
        return raw
    return raw if _motion_value_ok(raw, ids) else None


def _sanitize_clip(c, seen_clip_ids):
    if not isinstance(c, dict):
        return None
    src = c.get("source")
    if not isinstance(src, dict) or src.get("kind") not in _KINDS:
        return None
    path = str(src.get("path") or "")
    if not path:
        return None
    cid = str(c.get("id") or "")
    if cid in seen_clip_ids:
        cid = ""            # 让调用方后面重排；先保留 clip
    start = max(0.0, _num(c.get("start"), 0.0))
    dur = _num(c.get("dur"), 0.0)
    if dur < MIN_DUR - EPS:
        return None
    src_in = max(0.0, _num(c.get("src_in"), 0.0))
    out = {"id": cid,
           "source": {"kind": src["kind"], "path": path,
                      "w": int(_num(src.get("w"), 0)),
                      "h": int(_num(src.get("h"), 0)),
                      "fps": _num(src.get("fps"), 0.0),
                      "src_dur": max(0.0, _num(src.get("src_dur"), 0.0))},
           "start": round(start, 3), "src_in": round(src_in, 3),
           "dur": round(dur, 3), "speed": 1.0,
           "layout": _sanitize_layout(c.get("layout"))}
    # C2：motion / expression 透传（脏值丢键不丢 clip）
    mv = _sanitize_preset(c.get("motion"), _MT.motion_ids)
    ex = _sanitize_preset(c.get("expression"), _MT.expression_ids)
    if mv:
        out["motion"] = mv
    if ex:
        out["expression"] = ex
    return out


# ---------------- 同屏立绘统计助手（I1 建立，K1/task-69 转纯信息展示） ----------------
# K1（task-69，用户 2026-11-14 拍板）：**同屏人数无上限**——原"最多 3 人"
# 产品规约全面撤销。本组助手函数**保留**，用途从"拒绝/警告的判定依据"转为
# UI/回执的"当前同屏 N 人"信息展示。判定口径不变：
#   - 立绘 clip = 非音频轨上 source.kind∈{"image","puppet"}，且满足
#     「所在轨名以 FIG 开头（import 的 FIG-<角色> / remote 的 FIG-<who>）」
#     或「source.kind=="puppet"」（puppet 永不是背景）。
#   - 底轨/BG 轨的满幅 image 视为背景，不计。
#   - 时间区间 [start, start+dur) **右开**：A 在 t=5 结束、B 在 t=5 开始
#     不算同时在场（与导出 changeFigure/none 时序一致）。
FIGURE_LIMIT = None            # K1：无上限；助手函数仅供信息展示
_FIGURE_SRC_KINDS = ("image", "puppet")


def is_figure_clip(clip, track=None):
    """单 clip 是否计入同屏人数（判定口径见上方注释）。"""
    if not isinstance(clip, dict):
        return False
    src = clip.get("source") or {}
    sk = src.get("kind")
    if sk not in _FIGURE_SRC_KINDS:
        return False
    if sk == "puppet":
        return True
    tr_name = str((track or {}).get("name") or "").upper()
    return tr_name.startswith("FIG")


def figure_clips_at(proj, t):
    """时刻 t（含）生效的立绘 clip 列表（dict 引用，按轨道序）。"""
    out = []
    if not isinstance(proj, dict):
        return out
    try:
        tf = float(t)
    except (TypeError, ValueError):
        return out
    for tr in proj.get("tracks") or []:
        if not isinstance(tr, dict) or tr.get("kind") == "audio":
            continue
        for c in tr.get("clips") or []:
            if not is_figure_clip(c, tr):
                continue
            s = _num(c.get("start"), 0.0)
            if s <= tf < s + _num(c.get("dur"), 0.0):
                out.append(c)
    return out


def figure_intervals(proj, exclude_cid=None):
    """全部立绘 clip 的 (start, end, clip) 区间表（end 开区间右端）。"""
    iv = []
    if not isinstance(proj, dict):
        return iv
    for tr in proj.get("tracks") or []:
        if not isinstance(tr, dict) or tr.get("kind") == "audio":
            continue
        for c in tr.get("clips") or []:
            if exclude_cid is not None and c.get("id") == exclude_cid:
                continue
            if is_figure_clip(c, tr):
                s = _num(c.get("start"), 0.0)
                iv.append((s, s + _num(c.get("dur"), 0.0), c))
    return iv


def figure_overlap_max(proj, t0, t1, exclude_cid=None):
    """区间 [t0,t1) 内任意时刻的最大同屏人数（事件点扫描；端点开闭约定
    同 figure_clips_at）。exclude_cid：排除某 clip 自身（figure.move 用）。"""
    try:
        a0, a1 = float(t0), float(t1)
    except (TypeError, ValueError):
        return 0
    if a1 <= a0:
        return 0
    ivs = figure_intervals(proj, exclude_cid=exclude_cid)
    pts = {a0, a1}
    for s, e, _c in ivs:
        if a0 <= s < a1:
            pts.add(s)
        if a0 < e <= a1:
            pts.add(e)
    sp = sorted(pts)
    best = 0
    for a, b in zip(sp, sp[1:]):
        if b <= a:
            continue
        mid = (a + b) / 2.0
        n = sum(1 for s, e, _c in ivs if s <= mid < e)
        if n > best:
            best = n
    return best


def figure_occupancy_note(proj, t0, t1, exclude_cid=None):
    """K1（task-69）后为**纯信息展示**文案（原 I1 拒绝报错用的占位清单）：
    取 [t0,t1) 内首次达到峰值人数的时刻，列出生效立绘的 轨道名@区间。"""
    ivs = [(s, e, c) for s, e, c in figure_intervals(proj, exclude_cid)
           if e > t0 and s < t1]
    if not ivs:
        return ""
    pts = sorted({t0} | {s for s, _e, _c in ivs if t0 <= s < t1}
                 | {e for _s, e, _c in ivs if t0 < e <= t1} | {t1})
    peak, peak_t = 0, t0
    for a, b in zip(pts, pts[1:]):
        if b <= a:
            continue
        mid = (a + b) / 2.0
        n = sum(1 for s, e, _c in ivs if s <= mid < e)
        if n > peak:
            peak, peak_t = n, mid
    if peak <= 0:
        return ""
    names = []
    for tr in (proj.get("tracks") or []):
        for c in tr.get("clips") or []:
            if exclude_cid is not None and c.get("id") == exclude_cid:
                continue
            if not is_figure_clip(c, tr):
                continue
            s = _num(c.get("start"), 0.0)
            if s <= peak_t < s + _num(c.get("dur"), 0.0):
                nm = str(tr.get("name") or tr.get("id") or "?")
                names.append("%s@[%.1f-%.1fs]"
                             % (nm, s, s + _num(c.get("dur"), 0.0)))
    return "同屏峰值 %d 人（t≈%.1fs）：%s" % (peak, peak_t, "、".join(names[:6]))


def normalize_ids(proj):
    """补齐 sanitize 留下的空 id / 去重。load 后调用一次。"""
    used = set()
    for tr in proj["tracks"]:
        if tr["id"] in used:
            tr["id"] = next_track_id(proj, tr["kind"])
        used.add(tr["id"])
        for c in tr["clips"]:
            if not c["id"] or c["id"] in used:
                c["id"] = next_clip_id(proj)
            used.add(c["id"])
    return proj
