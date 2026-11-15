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
# F2-L1（task-39 / v3.5）：工程版本 2 = 多幕（acts）。
# 读入时 version<=1 自动迁移为单幕 v2 形态；内存里永远是 v2。
PROJECT_VERSION = 2
# 分支规模上限（防导出文件爆炸，设计 §2.2）
MAX_CHOICES_PER_ACT = 8
MAX_OPTIONS_PER_CHOICE = 6

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
    """默认一条视频轨 + 一条音频轨。

    F2-L1 兼容策略（**懒升级**）：新建工程仍是**单幕 v1 形状**
    （tracks/dialogue 在顶层、无 acts），这样 564+ 项既有回归与全部消费
    方零改动；只有真正用到多幕（add_act 等）时才升级为 v2。读入 v2 文件
    也保持 v2（见 sanitize_project）。两形状都由 act_view 统一投影。"""
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


def new_act(aid="a1", name=None):
    """一幕（F2-L1 §2.2）：自带完整单时间轴 + 分支节点。

    默认一条视频轨 + 一条音频轨（与单幕工程默认双轨一致）。"""
    return {
        "aid": str(aid), "name": str(name or aid),
        "tracks": [
            {"id": "v1", "kind": "video", "name": "视频轨 1",
             "hidden": False, "locked": False, "clips": []},
            {"id": "a1", "kind": "audio", "name": "音频轨 1",
             "hidden": False, "locked": False, "clips": []},
        ],
        "dialogue": [],
        "choices": [],       # [{cid, t|null, prompt, options:[{text,target,set,when}]}]
        "jumps": [],         # [{jid, t, when, target}]
        "next": None,        # 无分支续幕（导出 changeScene 衔接）
    }


def act_view(proj, aid=None):
    """F2-L1 §2.3：把某幕投影成**单时间轴形状**（消费方唯一入口）。

    返回 {type,width,height,fps,tracks,dialogue,comments}。
    `video_export.compose_frame`、快照、时长计算、StageView、时间轴面板
    **全部只吃本函数**，签名零改动（多幕改造不炸既有代码的关键机制）。

    **两种形状都支持**：
    - v1 单幕（无 acts）：直接投影顶层 tracks/dialogue —— 与旧行为全等
    - v2 多幕：按 aid 取幕（None=第一幕），取不到回第一幕

    返回的 tracks/dialogue 是**原对象引用**（面板编辑直接生效）；
    需要隔离时调用方 deepcopy。绝不抛。"""
    src = proj if isinstance(proj, dict) else {}
    view = {"type": PROJECT_TYPE, "width": src.get("width", 1920),
            "height": src.get("height", 1080), "fps": src.get("fps", 30.0),
            "tracks": [], "dialogue": [], "comments": src.get("comments", [])}
    acts = src.get("acts")
    if not isinstance(acts, list) or not acts:
        # v1 单幕：顶层即幕
        # 注意：不能用 `x or []` —— 空 list 是 falsy，会换成新对象破坏
        # 「返回原引用」契约（面板直接改 view 必须落到工程上）。
        t = src.get("tracks")
        view["tracks"] = t if isinstance(t, list) else []
        d = src.get("dialogue")
        view["dialogue"] = d if isinstance(d, list) else []
        return view
    act = None
    if aid is not None:
        for a in acts:
            if isinstance(a, dict) and a.get("aid") == aid:
                act = a
                break
    if act is None and acts and isinstance(acts[0], dict):
        act = acts[0]
    if act:
        t = act.get("tracks")
        view["tracks"] = t if isinstance(t, list) else []
        d = act.get("dialogue")
        view["dialogue"] = d if isinstance(d, list) else []
        if "comments" in act:
            c = act.get("comments")
            view["comments"] = c if isinstance(c, list) else []
    return view


def is_multi_act(proj):
    """是否已是 v2 多幕形状。"""
    return isinstance(proj, dict) and isinstance(proj.get("acts"), list) \
        and bool(proj.get("acts"))


def _upgrade_to_v2(proj):
    """懒升级：v1 单幕 -> v2 多幕（原地改 proj 并返回它）。

    只在**真正需要多幕**时调用（add_act / 读入 v2 文件）。迁移语义见
    §2.3：顶层 tracks/dialogue 收进 acts[0]，顶层不再保留镜像。"""
    if is_multi_act(proj):
        return proj
    act = new_act("a1", "幕一")
    act["tracks"] = proj.get("tracks") or act["tracks"]
    act["dialogue"] = proj.get("dialogue") or []
    if "comments" in proj:
        act["comments"] = proj.get("comments") or []
    proj.pop("tracks", None)
    proj.pop("dialogue", None)
    proj.pop("comments", None)
    proj["version"] = PROJECT_VERSION
    proj["vars"] = proj.get("vars") if isinstance(proj.get("vars"), list) else []
    proj["acts"] = [act]
    return proj


def find_act(proj, aid):
    """-> 幕 dict 或 None（v1 单幕形状返回 None：它没有 acts）。"""
    if not isinstance(proj, dict):
        return None
    for a in proj.get("acts") or []:
        if isinstance(a, dict) and a.get("aid") == aid:
            return a
    return None


def act_ids(proj):
    """已声明的幕 id 列表（v1 单幕 -> ['a1'] 虚拟幕，便于 UI 统一展示）。"""
    if not isinstance(proj, dict):
        return []
    if not is_multi_act(proj):
        return ["a1"]
    return [a.get("aid") for a in proj["acts"]
            if isinstance(a, dict) and a.get("aid")]


def act_list(proj):
    """UI 用幕摘要列表：[{aid,name,clips,duration,choices}]。"""
    out = []
    if not isinstance(proj, dict):
        return out
    if not is_multi_act(proj):
        view = act_view(proj)
        clips = sum(len(t.get("clips") or []) for t in view["tracks"]
                    if isinstance(t, dict))
        return [{"aid": "a1", "name": "幕一", "clips": clips,
                 "duration": act_duration(view), "choices": 0}]
    for a in proj["acts"]:
        if isinstance(a, dict) and a.get("aid"):
            out.append({
                "aid": a["aid"], "name": a.get("name") or a["aid"],
                "clips": sum(len(t.get("clips") or [])
                             for t in (a.get("tracks") or [])
                             if isinstance(t, dict)),
                "duration": act_duration(a),
                "choices": len(a.get("choices") or [])})
    return out


def next_act_id(proj):
    return next_id("a", act_ids(proj))


def act_duration(act):
    """幕内时长（该幕 tracks 的最大 clip 结束时刻）。"""
    if not isinstance(act, dict):
        return 0.0
    end = 0.0
    for t in (act.get("tracks") or []):
        if isinstance(t, dict):
            for c in (t.get("clips") or []):
                try:
                    end = max(end, float(c["start"]) + float(c["dur"]))
                except Exception:
                    pass
    return round(end, 3)


def add_act(proj, name=None, after_aid=None):
    """新增一幕后返回 (ok, msg, aid)。首次调用会把 v1 懒升级成 v2。

    新幕复制画布参数（在 proj 顶层，天然共享）+ 空双轨；不复制 tracks/
    dialogue（那是 duplicate_act 的语义）。"""
    if not isinstance(proj, dict):
        return False, "工程不是 dict", None
    _upgrade_to_v2(proj)
    aid = next_act_id(proj)
    act = new_act(aid, name or ("幕%d" % (len(proj["acts"]) + 1)))
    if after_aid:
        idx = next((i for i, a in enumerate(proj["acts"])
                    if a.get("aid") == after_aid), None)
        if idx is not None:
            proj["acts"].insert(idx + 1, act)
        else:
            proj["acts"].append(act)
    else:
        proj["acts"].append(act)
    return True, "", aid


def rename_act(proj, aid, name):
    """改幕显示名（aid 不变，引用不受影响）。返回 (ok, msg)。"""
    a = find_act(proj, aid)
    if a is None:
        return False, "幕不存在: %s" % aid
    a["name"] = str(name or aid)
    return True, ""


def duplicate_act(proj, aid):
    """复制一幕（含 tracks/dialogue/choices/jumps，引用按原样带走）。"""
    src = find_act(proj, aid)
    if src is None:
        return False, "幕不存在: %s" % aid, None
    new_aid = next_act_id(proj)
    dup = copy.deepcopy(src)
    dup["aid"] = new_aid
    dup["name"] = "%s（副本）" % (src.get("name") or aid)
    idx = proj["acts"].index(src)
    proj["acts"].insert(idx + 1, dup)
    return True, "", new_aid


def remove_act(proj, aid):
    """删一幕。级联：把 choices 选项/jumps/next 里指向它的引用清空。

    至少保留一幕（删最后一个 -> 拒绝）。返回 (ok, msg, cleared_refs)。
    cleared_refs 供 UI 提示"已清理 N 处引用"。"""
    a = find_act(proj, aid)
    if a is None:
        return False, "幕不存在: %s" % aid, 0
    if len(proj.get("acts") or []) <= 1:
        return False, "至少要保留一幕", 0
    proj["acts"].remove(a)
    cleared = 0
    for other in proj["acts"]:
        if not isinstance(other, dict):
            continue
        if other.get("next") == aid:
            other["next"] = None
            cleared += 1
        for ch in (other.get("choices") or []):
            for opt in (ch.get("options") or []):
                if opt.get("target") == aid:
                    opt["target"] = None
                    cleared += 1
        for j in (other.get("jumps") or []):
            if j.get("target") == aid:
                j["target"] = None
                cleared += 1
    return True, "", cleared


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
    """下一 clip id。**全工程唯一**（跨幕也不重名）：导出/remote 用
    clip id 全局定位，跨幕撞 id 会串幕，故扫描全部幕。"""
    ids = []
    if is_multi_act(proj):
        for a in proj.get("acts") or []:
            for t in (a.get("tracks") or []):
                for c in (t.get("clips") or []):
                    ids.append(c.get("id"))
    else:
        for t in _tracks_of(proj):
            for c in t["clips"]:
                ids.append(c["id"])
    return next_id("c", ids)


def next_track_id(proj, kind, aid=None):
    """下一轨道 id。**幕内唯一**（每幕自带 v1/a1 起点，与轨道是幕内
    局部容器一致）；跨幕可同名，所有按轨定位都必须带 aid（见 _tracks_of）。"""
    tracks = _tracks_of(proj, aid) if aid is not None else _tracks_of(proj)
    p = "v" if kind == "video" else "a"
    return next_id(p, [t["id"] for t in tracks if t["kind"] == kind])


def add_track(proj, kind, name=None, aid=None):
    """追加轨道；kind 只允许 video/audio。返回 (ok, msg, track_id)。

    aid（F2-L1 可选）：指定加到哪一幕（缺省当前幕/第一幕）。
    轨道 id 幕内唯一——多幕同名合法，调用方需带 aid 定位。"""
    if kind not in ("video", "audio"):
        return False, "轨道类型必须是 video/audio", None
    tid = next_track_id(proj, kind, aid)
    tracks = _tracks_of(proj, aid)
    cnt = sum(1 for t in tracks if t["kind"] == kind) + 1
    label = name or ("%s轨 %d" % ("视频" if kind == "video" else "音频", cnt))
    tracks.append({"id": tid, "kind": kind, "name": label,
                   "hidden": False, "locked": False, "clips": []})
    return True, "", tid


# ---------------- 查询 ----------------
def clip_end(clip):
    return clip["start"] + clip["dur"]


def find_clip(proj, clip_id):
    """-> (track, clip) 或 (None, None)"""
    for t in _tracks_of(proj):
        for c in t["clips"]:
            if c["id"] == clip_id:
                return t, c
    return None, None


def find_track(proj, track_id):
    for t in _tracks_of(proj):
        if t["id"] == track_id:
            return t
    return None


def project_duration(proj):
    end = 0.0
    for t in _tracks_of(proj):
        for c in t["clips"]:
            end = max(end, clip_end(c))
    return round(end, 3)


def _tracks_of(proj, aid=None):
    """F2-L1：取"当前幕"的 tracks（消费方零改动接入点）。

    v1 单幕 -> 顶层 tracks 原引用（行为与旧版全等）；
    v2 多幕 -> act_view(proj, aid)["tracks"]，aid=None 取第一幕。
    修改型函数（add_track/add_clip…）拿到的是该幕真实 list，写入即生效。"""
    if not isinstance(proj, dict):
        return []
    if is_multi_act(proj):
        return act_view(proj, aid)["tracks"]
    t = proj.get("tracks")
    return t if isinstance(t, list) else []


def _dialogue_of(proj, aid=None):
    """F2-L1：取"当前幕"的 dialogue list（同 _tracks_of 口径）。"""
    if not isinstance(proj, dict):
        return []
    if is_multi_act(proj):
        return act_view(proj, aid)["dialogue"]
    d = proj.get("dialogue")
    return d if isinstance(d, list) else []


def clips_at(proj, t, kind=None):
    out = []
    for tr in _tracks_of(proj):
        if kind and tr["kind"] != kind:
            continue
        for c in tr["clips"]:
            if c["start"] - EPS <= t < clip_end(c) - EPS:
                out.append(c)
    return out


def top_video_clip_at(proj, t):
    """最上层可见视频轨在 t 时刻的 clip（后加的轨在上）。"""
    for tr in reversed(_tracks_of(proj)):
        if tr["kind"] != "video" or tr.get("hidden"):
            continue
        for c in tr["clips"]:
            if c["start"] - EPS <= t < clip_end(c) - EPS:
                return c
    return None


def snap_points(proj, exclude_id=None):
    """所有 clip 边界 + 0，供吸附。"""
    pts = {0.0}
    for t in _tracks_of(proj):
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
def add_clip(proj, track_id, clip, aid=None):
    """加 clip 到指定轨。aid（F2-L1 可选）=目标幕，缺省当前幕/第一幕。"""
    tr = find_track(proj, track_id)
    if tr is None and aid is not None:
        tr = None
        for t in _tracks_of(proj, aid):
            if t.get("id") == track_id:
                tr = t
                break
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
    for tr in _tracks_of(proj):
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


def _sanitize_tracks(tracks, proj_for_id):
    """校验一组轨道（sanitize 内部共用：v1 顶层与 v2 每幕）。"""
    out = []
    if not isinstance(tracks, list):
        return out
    seen_ids = set()
    for tr in tracks:
        if not isinstance(tr, dict) or tr.get("kind") not in ("video", "audio"):
            continue
        tid = str(tr.get("id") or "")
        if not tid or tid in seen_ids:
            # 冲突/缺失：按同类已用编号重排（别丢轨）
            p = "v" if tr["kind"] == "video" else "a"
            n = 1
            used = {t["id"] for t in out}
            while ("%s%d" % (p, n)) in used:
                n += 1
            tid = "%s%d" % (p, n)
        seen_ids.add(tid)
        clips = []
        for c in (tr.get("clips") or []):
            cc = _sanitize_clip(c, seen_clip_ids=seen_ids)
            if cc:
                clips.append(cc)
        out.append({
            "id": tid, "kind": tr["kind"],
            "name": str(tr.get("name") or tid),
            "hidden": bool(tr.get("hidden", False)),
            "locked": bool(tr.get("locked", False)),
            "clips": clips})
    return out


def _sanitize_str_list(raw, limit=0):
    """字符串数组清洗（option.set 用：形如 "met=true"）。"""
    out = []
    for x in (raw if isinstance(raw, list) else []):
        if isinstance(x, str) and x.strip():
            s = x.strip()[:200]
            if limit and len(out) >= limit:
                break
            out.append(s)
    return out


def _sanitize_choices(raw, act_aids, warn=None):
    """F2-L1 §2.2：choices 清洗（上限/悬空引用容错，绝不抛）。"""
    out = []
    for ch in (raw if isinstance(raw, list) else []):
        if not isinstance(ch, dict):
            continue
        if len(out) >= MAX_CHOICES_PER_ACT:
            break
        cid = str(ch.get("cid") or "")
        if not cid:
            cid = next_id("c", [c["cid"] for c in out])
        t = ch.get("t")
        try:
            t = None if t is None else round(max(0.0, float(t)), 3)
        except (TypeError, ValueError):
            t = None
        opts = []
        for o in (ch.get("options") or []):
            if not isinstance(o, dict):
                continue
            if len(opts) >= MAX_OPTIONS_PER_CHOICE:
                break
            text = str(o.get("text") or "").strip()[:100]
            if not text:
                continue
            tgt = o.get("target")
            tgt = str(tgt) if tgt else None
            if tgt and act_aids and tgt not in act_aids:
                # 悬空引用：保留原名（往返保真），但标记
                if warn is not None:
                    warn.append("选项「%s」目标幕不存在: %s" % (text, tgt))
            opt = {"text": text, "target": tgt,
                   "set": _sanitize_str_list(o.get("set"), 8),
                   "when": (str(o.get("when")).strip()[:120]
                            if o.get("when") else None)}
            opts.append(opt)
        out.append({"cid": cid, "t": t,
                    "prompt": (str(ch.get("prompt")).strip()[:200]
                               if ch.get("prompt") else None),
                    "options": opts})
    return out


def _sanitize_jumps(raw, act_aids, warn=None):
    """F2-L1 §2.2：jumps 清洗。"""
    out = []
    for j in (raw if isinstance(raw, list) else []):
        if not isinstance(j, dict):
            continue
        try:
            t = round(max(0.0, float(j.get("t"))), 3)
        except (TypeError, ValueError):
            continue
        when = str(j.get("when") or "").strip()[:200]
        if not when:
            continue
        tgt = j.get("target")
        tgt = str(tgt) if tgt else None
        if tgt and act_aids and tgt not in act_aids and warn is not None:
            warn.append("跳转目标幕不存在: %s" % tgt)
        out.append({"jid": str(j.get("jid") or next_id("j", [x["jid"]
                                                            for x in out])),
                    "t": t, "when": when, "target": tgt})
    return out


def _sanitize_vars(raw):
    """F2-L1：变量声明清洗 [{name, default}]。"""
    out = []
    for v in (raw if isinstance(raw, list) else []):
        if not isinstance(v, dict):
            continue
        name = str(v.get("name") or "").strip()
        if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            continue
        out.append({"name": name, "default": v.get("default", 0)})
    return out


def sanitize_project(obj, warn=None):
    """脏文件容错：逐字段校验 + 补默认。不合法返回 None。

    F2-L1：支持两种形状读入——
    - v1（无 acts）：顶层 tracks/dialogue 校验后**保持 v1 形状输出**
      （零破坏：既有全部测试与消费方行为全等）
    - v2（有 acts）：逐幕校验，输出 v2

    warn（可选 list）：收集容错警告（悬空分支引用等）。"""
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

    acts_raw = obj.get("acts")
    if isinstance(acts_raw, list) and acts_raw:
        # ---------- v2 多幕 ----------
        proj = {"type": PROJECT_TYPE, "version": PROJECT_VERSION,
                "width": w, "height": h, "fps": fps,
                "vars": _sanitize_vars(obj.get("vars")), "acts": []}
        # 先定 aid 集合（choices/jumps 悬空校验需要）
        aids, seen = [], set()
        for i, a in enumerate(acts_raw):
            if not isinstance(a, dict):
                continue
            aid = str(a.get("aid") or "")
            if not aid or aid in seen:
                aid = "a%d" % (i + 1)
                while aid in seen:
                    aid += "_"
            seen.add(aid)
            aids.append((aid, a))
        act_aids = [x[0] for x in aids]
        for aid, a in aids:
            act = {"aid": aid, "name": str(a.get("name") or aid),
                   "tracks": _sanitize_tracks(a.get("tracks"), proj),
                   "dialogue": _sanitize_dialogue(a.get("dialogue")),
                   "choices": _sanitize_choices(a.get("choices"), act_aids, warn),
                   "jumps": _sanitize_jumps(a.get("jumps"), act_aids, warn),
                   "next": (str(a["next"]) if a.get("next") else None)}
            if not act["tracks"]:
                act["tracks"] = new_act()["tracks"]
            if "comments" in a:
                act["comments"] = _sanitize_comments(a.get("comments"))
            if act["next"] and act["next"] not in act_aids and warn is not None:
                warn.append("续幕目标不存在: %s" % act["next"])
            proj["acts"].append(act)
        if not proj["acts"]:
            return None
        return proj

    # ---------- v1 单幕（原行为逐字节保留）----------
    tracks = obj.get("tracks")
    if not isinstance(tracks, list):
        return None
    proj = {"type": PROJECT_TYPE, "version": 1, "width": w, "height": h,
            "fps": fps, "tracks": _sanitize_tracks(tracks, None)}
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
    for tr in _tracks_of(proj):
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
    for tr in _tracks_of(proj):
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
    for tr in _tracks_of(proj):
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
    """补齐 sanitize 留下的空 id / 去重。load 后调用一次。

    F2-L1：轨道 id 幕内唯一、clip id 全工程唯一（分幕分别处理）。"""
    if is_multi_act(proj):
        clips_used = set()
        for a in proj.get("acts") or []:
            _norm_act_ids(proj, a, clips_used)
        return proj
    used = set()
    for tr in _tracks_of(proj):
        if tr["id"] in used:
            tr["id"] = next_track_id(proj, tr["kind"])
        used.add(tr["id"])
        for c in tr["clips"]:
            if not c["id"] or c["id"] in used:
                c["id"] = next_clip_id(proj)
            used.add(c["id"])
    return proj


def _norm_act_ids(proj, act, clips_used):
    """单幕 id 归一（轨道幕内唯一，clip 跨幕唯一由 clips_used 保证）。"""
    used = set()
    for tr in (act.get("tracks") or []):
        if tr.get("id") in used:
            p = "v" if tr.get("kind") == "video" else "a"
            n = 1
            while ("%s%d" % (p, n)) in used:
                n += 1
            tr["id"] = "%s%d" % (p, n)
        used.add(tr.get("id"))
        for c in (tr.get("clips") or []):
            if not c.get("id") or c["id"] in clips_used:
                p = "c"
                n = 1
                while ("%s%d" % (p, n)) in clips_used:
                    n += 1
                c["id"] = "%s%d" % (p, n)
            clips_used.add(c["id"])
