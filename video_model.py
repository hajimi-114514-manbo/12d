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
import os
import re
import contextlib

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
            elif k == "rot": out[k] = _num(v, 0.0)
            else:                       # opacity/anchor/crop/blend/keyframes/...
                out[k] = v              # 不校验不改写，原样带走
    return out


def layout_at(clip, t=None):
    """clip 在 t 时刻的完整布局 dict（合并默认后）。**消费方唯一入口**。

    A1：纯静态，忽略 t。C 方案的 layout 关键帧只改本函数内部
    （从 clip["layout"]["keyframes"] 采样插值覆盖对应通道），签名与
    返回结构不变，调用方零改动。"""
    if not isinstance(clip, dict): return dict(LAYOUT_DEFAULT)
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
        "effects": [],       # v3.5：[{t,cmd}] setTransform 等画面特效（按时刻）
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
            "tracks": [], "dialogue": [], "comments": src.get("comments", []),
            "effects": src.get("effects", [])}
    acts = src.get("acts")
    if not isinstance(acts, list) or not acts:
        # v1 单幕：顶层即幕
        # 注意：不能用 `x or []` —— 空 list 是 falsy，会换成新对象破坏
        # 「返回原引用」契约（面板直接改 view 必须落到工程上）。
        t = src.get("tracks")
        view["tracks"] = t if isinstance(t, list) else []
        d = src.get("dialogue")
        view["dialogue"] = d if isinstance(d, list) else []
        e = src.get("effects")
        view["effects"] = e if isinstance(e, list) else []
        return view
    act = None
    if aid is not None:
        for a in acts:
            if isinstance(a, dict) and a.get("aid") == aid:
                act = a
                break
    if act is None and acts and isinstance(acts[0], dict): act = acts[0]
    if act:
        t = act.get("tracks")
        view["tracks"] = t if isinstance(t, list) else []
        d = act.get("dialogue")
        view["dialogue"] = d if isinstance(d, list) else []
        if "comments" in act:
            c = act.get("comments")
            view["comments"] = c if isinstance(c, list) else []
        if "effects" in act:
            e = act.get("effects")
            view["effects"] = e if isinstance(e, list) else []
    return view


def is_multi_act(proj):
    """是否已是 v2 多幕形状。"""
    return isinstance(proj, dict) and isinstance(proj.get("acts"), list) \
        and bool(proj.get("acts"))


def _upgrade_to_v2(proj):
    """懒升级：v1 单幕 -> v2 多幕（原地改 proj 并返回它）。

    只在**真正需要多幕**时调用（add_act / 读入 v2 文件）。迁移语义见
    §2.3：顶层 tracks/dialogue 收进 acts[0]，顶层不再保留镜像。"""
    if is_multi_act(proj): return proj
    act = new_act("a1", "幕一")
    act["tracks"] = proj.get("tracks") or act["tracks"]
    act["dialogue"] = proj.get("dialogue") or []
    if "comments" in proj: act["comments"] = proj.get("comments") or []
    if "effects" in proj: act["effects"] = proj.get("effects") or []
    proj.pop("tracks", None)
    proj.pop("dialogue", None)
    proj.pop("comments", None)
    proj.pop("effects", None)
    proj["version"] = PROJECT_VERSION
    proj["vars"] = proj.get("vars") if isinstance(proj.get("vars"), list) else []
    proj["acts"] = [act]
    return proj


def find_act(proj, aid):
    """-> 幕 dict 或 None（v1 单幕形状返回 None：它没有 acts）。"""
    if not isinstance(proj, dict): return None
    for a in proj.get("acts") or []:
        if isinstance(a, dict) and a.get("aid") == aid: return a
    return None


# ---- 语音轨（需求「时间轴语音轨」）-------------------------------------
# 设计：语音 clip **不存进 tracks**，而是由 dialogue[i].voice 实时投影出来。
#
# 为什么不存进 tracks：
#   · 存了就有两份真相（dialogue 里的 t 和 clip 的 start），必然不一致
#   · 默认要「跟台词走」，存下来就得每次改台词都同步更新 clip，容易漏
#   · 删除台词时还要记得删 clip，多一处能出错的地方
#
# 代价：语音轨是**只读投影**，用户的编辑动作要翻译成对 dialogue 的修改
#（移动 -> 改 dialogue[i].t 或 voice["t"]）。这是有意的：
# 语音的真相永远在 dialogue 里，只有一处。

VOICE_TRACK_ID = "vc1"


def voice_clips(proj, aid=None):
    """把 dialogue 里的 voice 投影成时间轴 clip 列表（按起点排序）。

    返回 [{"id","t","dur","path","role","text","detach","src_index"}]：
      id        稳定的 clip 标识（"vc:<台词索引>"），拖动/选中都靠它
      t         起始时刻（detach=False 时 = 台词 t；True 时 = voice["t"]）
      src_index 对应 dialogue 里的下标（回写用）
    """
    out = []
    try:
        view = act_view(proj, aid)
    except Exception:
        view = proj if isinstance(proj, dict) else {}
    dl = view.get("dialogue")
    if not isinstance(dl, list):
        return out
    for i, it in enumerate(dl):
        if not isinstance(it, dict):
            continue
        v = it.get("voice")
        if not isinstance(v, dict) or not v.get("path"):
            continue
        dur = _num(v.get("dur"), 0.0)
        if dur <= 0:
            # 没有时长就画不出长度；退一步用 1 秒占位（比不显示好）
            dur = 1.0
        detach = v.get("detach") is True
        if detach:
            t = _num(v.get("t"), _num(it.get("t"), 0.0))
        else:
            t = _num(it.get("t"), 0.0)
        out.append({
            "id": "vc:%d" % i,
            "t": round(max(0.0, t), 3),
            "dur": round(dur, 3),
            "path": v.get("path", ""),
            "role": str(it.get("who") or ""),
            "text": str(v.get("text") or it.get("text") or ""),
            "detach": detach,
            "src_index": i,
        })
    out.sort(key=lambda c: (c["t"], c["src_index"]))
    return out


def voice_track(proj, aid=None):
    """语音轨的轨道描述（时间轴渲染用）。没有语音时返回 None。"""
    clips = voice_clips(proj, aid)
    if not clips:
        return None
    n_det = sum(1 for c in clips if c["detach"])
    name = "语音轨"
    if n_det:
        name += "（%d 段已脱离台词）" % n_det
    return {"id": VOICE_TRACK_ID, "kind": "voice", "name": name,
            "clips": clips, "voice_projection": True}


def voice_set_detach(proj, index, detach, t=None, aid=None):
    """把第 index 条台词的语音「脱离/绑回」台词。

    detach=True  时 t 必须给（否则用台词当前 t 当起点）
    detach=False 时清掉 t（回到跟台词走）
    """
    view = act_view(proj, aid)
    dl = view.get("dialogue")
    if not isinstance(dl, list) or not (0 <= index < len(dl)):
        return False, "台词下标越界", None
    it = dl[index]
    v = it.get("voice")
    if not isinstance(v, dict) or not v.get("path"):
        return False, "该台词没有语音", None
    if detach:
        start = _num(t, _num(it.get("t"), 0.0))
        v["detach"] = True
        v["t"] = round(max(0.0, start), 3)
        return True, "已脱离台词（起点 %.2fs）" % v["t"], {
            "index": index, "detach": True, "t": v["t"]}
    v.pop("detach", None)
    v.pop("t", None)
    return True, "已绑回台词（跟随台词 %.2fs）" % _num(it.get("t"), 0.0), {
        "index": index, "detach": False, "t": _num(it.get("t"), 0.0)}


def voice_move(proj, index, new_t, aid=None):
    """移动一段语音到 new_t。

    detach=False -> 改成挪台词本身（改 dialogue[index].t），保持对齐
    detach=True  -> 只改语音的 t（台词不动）
    """
    view = act_view(proj, aid)
    dl = view.get("dialogue")
    if not isinstance(dl, list) or not (0 <= index < len(dl)):
        return False, "台词下标越界", None
    it = dl[index]
    v = it.get("voice")
    if not isinstance(v, dict) or not v.get("path"):
        return False, "该台词没有语音", None
    nt = round(max(0.0, _num(new_t, 0.0)), 3)
    if v.get("detach") is True:
        v["t"] = nt
        return True, "语音移到 %.2fs（台词未动）" % nt, {
            "index": index, "detach": True, "t": nt}
    it["t"] = nt
    return True, "台词移到 %.2fs（语音跟随）" % nt, {
        "index": index, "detach": False, "t": nt}


def act_ids(proj):
    """已声明的幕 id 列表（v1 单幕 -> ['a1'] 虚拟幕，便于 UI 统一展示）。"""
    if not isinstance(proj, dict): return []
    if not is_multi_act(proj): return ["a1"]
    return [a.get("aid") for a in proj["acts"]
            if isinstance(a, dict) and a.get("aid")]


def act_list(proj):
    """UI 用幕摘要列表：[{aid,name,clips,duration,choices}]。"""
    out = []
    if not isinstance(proj, dict): return out
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


def unreachable_acts(proj, entry=None):
    """从入口幕出发**沿着分支边**能到达不到的幕 id 列表（按声明顺序）。

    边的来源与导出器一致：choice.options[].target、jumps[].target、next。
    单幕工程 -> []（没有分支可言）。entry 缺省取第一幕（= a1，导出入口）。
    """
    if not isinstance(proj, dict) or not is_multi_act(proj): return []
    ids = act_ids(proj)
    if not ids: return []
    start = entry or ids[0]
    seen = {start}
    stack = [start]
    while stack:
        cur = stack.pop()
        a = find_act(proj, cur)
        if not a: continue
        outs = []
        with contextlib.suppress(Exception):
            for ch in (a.get("choices") or []):
                if isinstance(ch, dict):
                    for o in (ch.get("options") or []):
                        if isinstance(o, dict) and o.get("target"):
                            outs.append(str(o["target"]))
        with contextlib.suppress(Exception):
            for j in (a.get("jumps") or []):
                if isinstance(j, dict) and j.get("target"):
                    outs.append(str(j["target"]))
        if a.get("next"): outs.append(str(a["next"]))
        for t in outs:
            if t in ids and t not in seen:
                seen.add(t)
                stack.append(t)
    return [i for i in ids if i not in seen]


def act_duration(act):
    """幕内时长（该幕 tracks 的最大 clip 结束时刻）。"""
    if not isinstance(act, dict): return 0.0
    end = 0.0
    for t in (act.get("tracks") or []):
        if isinstance(t, dict):
            for c in (t.get("clips") or []):
                with contextlib.suppress(Exception):
                    end = max(end, float(c["start"]) + float(c["dur"]))
    return round(end, 3)


def add_act(proj, name=None, after_aid=None):
    """新增一幕后返回 (ok, msg, aid)。首次调用会把 v1 懒升级成 v2。

    新幕复制画布参数（在 proj 顶层，天然共享）+ 空双轨；不复制 tracks/
    dialogue（那是 duplicate_act 的语义）。"""
    if not isinstance(proj, dict): return False, "工程不是 dict", None
    _upgrade_to_v2(proj)
    aid = next_act_id(proj)
    act = new_act(aid, name or ("幕%d" % (len(proj["acts"]) + 1)))
    if after_aid:
        idx = next((i for i, a in enumerate(proj["acts"])
                    if a.get("aid") == after_aid), None)
        if idx is not None: proj["acts"].insert(idx + 1, act)
        else: proj["acts"].append(act)
    else: proj["acts"].append(act)
    return True, "", aid


def rename_act(proj, aid, name):
    """改幕显示名（aid 不变，引用不受影响）。返回 (ok, msg)。"""
    a = find_act(proj, aid)
    if a is None: return False, "幕不存在: %s" % aid
    a["name"] = str(name or aid)
    return True, ""


def duplicate_act(proj, aid):
    """复制一幕（含 tracks/dialogue/choices/jumps，引用按原样带走）。"""
    src = find_act(proj, aid)
    if src is None: return False, "幕不存在: %s" % aid, None
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
    if a is None: return False, "幕不存在: %s" % aid, 0
    if len(proj.get("acts") or []) <= 1: return False, "至少要保留一幕", 0
    proj["acts"].remove(a)
    cleared = 0
    for other in proj["acts"]:
        if not isinstance(other, dict): continue
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
    if kind not in ("video", "audio"): return False, "轨道类型必须是 video/audio", None
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
            if c["id"] == clip_id: return t, c
    return None, None


def find_track(proj, track_id, aid=None):
    """按 id 找轨道。多幕工程必须带 aid 才能定位当前幕的轨（aid=None
    只搜第一幕，当前幕非第一幕时找不到新轨）。"""
    for t in _tracks_of(proj, aid):
        if t["id"] == track_id: return t
    return None


def project_duration(proj, aid=None):
    """工程时长（秒）。

    F2-L5（v3.5）：加 aid 参数取**指定幕**的时长（多幕工程里一幕一条
    独立时间轴，总时长是各幕之和，但时间轴面板只显示当前幕）。
    aid=None 时行为与旧版完全一致（v1 顶层 tracks / v2 第一幕）。"""
    end = 0.0
    for t in _tracks_of(proj, aid):
        for c in t["clips"]:
            end = max(end, clip_end(c))
    return round(end, 3)


def _tracks_of(proj, aid=None):
    """F2-L1：取"当前幕"的 tracks（消费方零改动接入点）。

    v1 单幕 -> 顶层 tracks 原引用（行为与旧版全等）；
    v2 多幕 -> act_view(proj, aid)["tracks"]，aid=None 取第一幕。
    修改型函数（add_track/add_clip…）拿到的是该幕真实 list，写入即生效。"""
    if not isinstance(proj, dict): return []
    if is_multi_act(proj): return act_view(proj, aid)["tracks"]
    t = proj.get("tracks")
    return t if isinstance(t, list) else []


def _dialogue_of(proj, aid=None):
    """F2-L1：取"当前幕"的 dialogue list（同 _tracks_of 口径）。"""
    if not isinstance(proj, dict): return []
    if is_multi_act(proj): return act_view(proj, aid)["dialogue"]
    d = proj.get("dialogue")
    return d if isinstance(d, list) else []


def clips_at(proj, t, kind=None):
    out = []
    for tr in _tracks_of(proj):
        if kind and tr["kind"] != kind: continue
        for c in tr["clips"]:
            if c["start"] - EPS <= t < clip_end(c) - EPS: out.append(c)
    return out


def top_video_clip_at(proj, t):
    """最上层可见视频轨在 t 时刻的 clip（后加的轨在上）。"""
    for tr in reversed(_tracks_of(proj)):
        if tr["kind"] != "video" or tr.get("hidden"): continue
        for c in tr["clips"]:
            if c["start"] - EPS <= t < clip_end(c) - EPS: return c
    return None


def snap_points(proj, exclude_id=None):
    """所有 clip 边界 + 0，供吸附。"""
    pts = {0.0}
    for t in _tracks_of(proj):
        for c in t["clips"]:
            if c["id"] == exclude_id: continue
            pts.add(round(c["start"], 3))
            pts.add(round(clip_end(c), 3))
    return sorted(pts)


def snap_time(points, t, eps=SNAP_STEP * 3):
    """把 t 吸到最近的边界点（eps 留浮点余量：3.02-3.0=0.02000000000000046）。"""
    best, bd = t, eps
    for p in points:
        d = abs(p - t)
        if d <= bd: best, bd = p, d
    return round(best, 3)


def snap_points_multi(proj, exclude_ids=None):
    """snap_points 的多 id 版（成组移动时整组互不吸附）。"""
    ex = set(exclude_ids or ())
    pts = {0.0}
    for t in _tracks_of(proj):
        for c in t["clips"]:
            if c["id"] in ex: continue
            pts.add(round(c["start"], 3))
            pts.add(round(clip_end(c), 3))
    return sorted(pts)


def _overlap(a0, a1, b0, b1):
    return a0 + EPS < b1 - EPS and b0 + EPS < a1 - EPS


def _clash(proj, track, start, dur, exclude_id=None):
    """同轨重叠检查 -> 冲突 clip 或 None。"""
    for c in track["clips"]:
        if c["id"] == exclude_id: continue
        if _overlap(start, start + dur, c["start"], clip_end(c)): return c
    return None


def src_dur_of(clip):
    return float(clip["source"].get("src_dur") or 0.0)


def _src_limit_ok(clip, src_in, dur):
    """素材时长约束：image 无限长恒 OK；其余 src_in+dur <= src_dur+EPS。"""
    if clip["source"]["kind"] == "image": return True
    sd = src_dur_of(clip)
    if sd <= 0:          # 未知长度素材（探测失败）：不拦
        return True
    return src_in + dur <= sd + EPS


# ---------------- 编辑（原地改，返回 (ok, msg)） ----------------
def add_clip(proj, track_id, clip, aid=None):
    """加 clip 到指定轨。aid（F2-L1 可选）=目标幕，缺省当前幕/第一幕。"""
    tr = find_track(proj, track_id, aid)
    if tr is None and aid is not None:
        # aid 指定幕里没有 -> 不再回退第一幕（旧行为会把片段塞错幕）
        return False, "轨道不存在: %s" % track_id
    if tr is None: return False, "轨道不存在: %s" % track_id
    if tr.get("locked"): return False, "轨道已锁定"
    if clip["dur"] < MIN_DUR - EPS: return False, "clip 太短（<%.2fs）" % MIN_DUR
    if _clash(proj, tr, clip["start"], clip["dur"]): return False, "与同轨 clip 重叠"
    clip = dict(clip)
    clip["id"] = next_clip_id(proj)
    tr["clips"].append(clip)
    return True, clip["id"]


def remove_clip(proj, clip_id):
    tr, c = find_clip(proj, clip_id)
    if c is None: return False, "clip 不存在"
    if tr.get("locked"): return False, "轨道已锁定"
    tr["clips"].remove(c)
    return True, ""


def move_clip(proj, clip_id, new_start, new_track_id=None):
    """同轨/跨轨移动。跨轨换轨时校验目标轨类型不限、重叠不撞。"""
    tr, c = find_clip(proj, clip_id)
    if c is None: return False, "clip 不存在"
    if tr.get("locked"): return False, "轨道已锁定"
    dst = find_track(proj, new_track_id) if new_track_id else tr
    if dst is None: return False, "目标轨道不存在"
    if dst.get("locked"): return False, "目标轨道已锁定"
    new_start = round(max(0.0, float(new_start)), 3)
    if _clash(proj, dst, new_start, c["dur"], exclude_id=clip_id):
        return False, "与目标轨 clip 重叠"
    if dst is not tr:
        tr["clips"].remove(c)
        dst["clips"].append(c)
    c["start"] = new_start
    return True, ""


def _clash_any(proj, track, start, dur, exclude_ids):
    """同轨重叠检查，排除**一组** id（成组移动用：组内互不判撞）。"""
    ex = set(exclude_ids or ())
    for c in track["clips"]:
        if c["id"] in ex: continue
        if _overlap(start, start + dur, c["start"], clip_end(c)): return c
    return None


def move_clips(proj, clip_ids, delta):
    """成组平移（框选后拖动）：整组同位移，全部合法才落数据（原子）。

    与 move_clip 的三点区别：
    - 排除集是**整组**（组内互不判重叠，否则一起动就自己撞自己）；
    - 整组一起顶到 0 之前时按最左者整体右推，**保持相对间距**
      （逐条钳 0 会改变组内相对位置，等于把用户摆好的错位打乱）；
    - 锁定轨的 clip 留在原地（与「锁定轨可选中但不可拖」的单选语义一致），
      但**只有全部成员都锁着**才算失败回弹。
    任一可动成员撞组外片段 -> 整组不动（部分成功会把成组移动变成撕裂）。"""
    ids = [str(i) for i in (clip_ids or ())]
    if not ids: return False, "未选中片段"
    try:
        delta = float(delta)
    except (TypeError, ValueError):
        return False, "位移非法"
    moving = []
    for cid in ids:
        tr, c = find_clip(proj, cid)
        if c is None: return False, "clip 不存在"
        if tr.get("locked"): continue
        moving.append((tr, c))
    if not moving: return False, "轨道已锁定"
    if abs(delta) < EPS: return True, ""
    moved_ids = {c["id"] for _t, c in moving}
    lo = min(float(c["start"]) + delta for _t, c in moving)
    shift = -lo if lo < 0 else 0.0
    for tr, c in moving:
        ns = round(float(c["start"]) + delta + shift, 3)
        if _clash_any(proj, tr, ns, float(c["dur"]), moved_ids): return False, "与组外片段重叠"
    for tr, c in moving:
        c["start"] = round(float(c["start"]) + delta + shift, 3)
    return True, ""


def remove_clips(proj, clip_ids):
    """成组删除：逐个走 remove_clip（锁定轨静默跳过）。ok = 至少删掉一个。"""
    n = 0
    last_msg = ""
    for cid in list(clip_ids or ()):
        ok, msg = remove_clip(proj, cid)
        if ok: n += 1
        else: last_msg = msg
    if not n: return False, last_msg or "clip 不存在"
    return True, ""


def trim_clip(proj, clip_id, side, new_value):
    """side='start'：改起点（联动 src_in）；side='end'：改终点。
    new_value 是**时间轴坐标**（秒）。两端都保持素材偏移合法。"""
    tr, c = find_clip(proj, clip_id)
    if c is None: return False, "clip 不存在"
    if tr.get("locked"): return False, "轨道已锁定"
    v = round(max(0.0, float(new_value)), 3)
    if side == "start":
        d = v - c["start"]                      # >0 左端右移，<0 左扩
        new_in = c["src_in"] + d                # image 恒回 0
        if c["source"]["kind"] == "image": new_in = 0.0
        if new_in < -EPS: return False, "不能拖到 0 之前"
        new_dur = c["dur"] - d
        if new_dur < MIN_DUR - EPS: return False, "trim 后太短"
        if not _src_limit_ok(c, max(0.0, new_in), new_dur): return False, "超出素材长度"
        if _clash(proj, tr, v, new_dur, exclude_id=clip_id): return False, "与邻接 clip 重叠"
        c["start"], c["src_in"], c["dur"] = v, round(max(0.0, new_in), 3), new_dur
        return True, ""
    elif side == "end":
        new_dur = v - c["start"]
        if new_dur < MIN_DUR - EPS: return False, "trim 后太短"
        if not _src_limit_ok(c, c["src_in"], new_dur): return False, "超出素材长度"
        if _clash(proj, tr, c["start"], new_dur, exclude_id=clip_id):
            return False, "与邻接 clip 重叠"
        c["dur"] = round(new_dur, 3)
        return True, ""
    return False, "side 必须是 start/end"


def split_clip(proj, clip_id, t):
    """在时间轴 t 处切成两段。t 必须严格在 clip 内部。返回 (ok, msg, 右段id)。"""
    tr, c = find_clip(proj, clip_id)
    if c is None: return False, "clip 不存在", None
    if tr.get("locked"): return False, "轨道已锁定", None
    left = t - c["start"]
    right = clip_end(c) - t
    if left < MIN_DUR - EPS or right < MIN_DUR - EPS: return False, "切分后片段太短", None
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
    if t1 - t0 < MIN_DUR - EPS: return 0
    n = 0
    for tr in _tracks_of(proj):
        if tr.get("locked"): continue
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


def _sanitize_transition(raw):
    """转场载体（工程级，全片统一）。返回规范化 dict 或 None。

    None = 未设置 -> 导出走默认淡入淡出（enter/exit），与历史逐字节一致。
    dict 形态见 transitions.to_proj_transition。校验：
      · enter/exit 必须是引擎动画表里的名字（脏名退回默认 enter/exit，
        绝不让导出插一条引擎不认识的动画）
      · duration_ms 夹到 [0, 5000]
      · 「无转场」（enter/exit 皆空）合法，导出据此不插 setTransition
    """
    if not isinstance(raw, dict): return None
    enter = str(raw.get("enter") or "")
    exit_ = str(raw.get("exit") or "")
    try:
        import transitions as TR
    except Exception:
        TR = None
    # 引擎表校验：任一脏名 -> 整体退回默认（None），保守不写坏指令
    if TR is not None:
        if enter and not TR.valid_anim(enter): enter = ""
        if exit_ and not TR.valid_anim(exit_): exit_ = ""
    dur = _num(raw.get("duration_ms"), 300.0)
    dur = int(max(0, min(5000, dur)))
    name = str(raw.get("name") or "")
    vkind = str(raw.get("video_kind") or "fade")
    sdir = str(raw.get("slide_dir") or "")
    # 硬切（无转场）：enter/exit 都空，保留这个明确意图（区别于 None 默认）
    if not enter and not exit_:
        return {"name": name or "无转场", "enter": "", "exit": "",
                "duration_ms": 0, "video_kind": vkind, "slide_dir": ""}
    return {"name": name, "enter": enter, "exit": exit_,
            "duration_ms": dur, "video_kind": vkind, "slide_dir": sdir}


def _sanitize_effects(raw):
    """画面特效载体 effects=[{t,cmd}]（v3.5）。

    为什么需要：提示词包教 AI 用 `setTransform` 滤镜后，导入端若把整条
    指令当台词吞掉，玩家会看到一大串 JSON（实测 bug）。丢掉又会让
    「导入 → 导出」这条链路**静默丢失 AI 写的全部滤镜**。
    所以按时刻存原指令，导出时按时刻 emit 回真实指令。

    校验：t 为有限浮点 >=0；cmd 必须是 str 且首 token 属于
    `setTransform` / `setTempAnimation`（白名单，防脏值被执行）。脏项丢弃。
    """
    out = []
    if not isinstance(raw, list): return out
    for it in raw:
        if not isinstance(it, dict): continue
        t = _num(it.get("t"), -1.0)
        cmd = it.get("cmd")
        if t < 0 or not isinstance(cmd, str): continue
        s = cmd.strip().rstrip(";").strip()
        if not s or ":" not in s: continue
        head, _, body = s.partition(":")
        head = head.strip()
        if head not in ("setTransform", "setTempAnimation"): continue
        body = body.strip()
        # 参数体必须像样：setTransform 要求是 JSON 对象；
        # setTempAnimation 要求是 JSON 数组。空的/乱写的一律丢
        # （否则导出时会把脏指令原样吐给引擎）。
        if not body or body[0] not in "{[": continue
        out.append({"t": round(t, 3), "cmd": s})
    return out


def _sanitize_comments(raw):
    """工程级注释载体 comments=[{t,text}]（I3/task-67）：导入端 `//` 与
    `;xxx;` 注释行内容保留，导出端转合法 `;xxx;`。脏项丢弃，绝不抛。"""
    out = []
    if not isinstance(raw, list): return out
    for it in raw:
        if not isinstance(it, dict): continue
        t = _num(it.get("t"), -1.0)
        text = it.get("text")
        if t < 0 or not isinstance(text, str) or not text.strip(): continue
        out.append({"t": round(t, 3), "text": text.strip()})
    return out


def _sanitize_voice(raw):
    """台词级语音引用透传校验。

    形态（v3.6.2 后，需求「语音轴与角色台词轴相对齐」）：
        voice = {"path": "vocal/xxx.wav",      # 相对工程目录
                 "dur": 1.94,                  # 实测音频时长（秒）
                 "voice": "MyGO_千早爱音_v2pp", # 用的音色（可空）
                 "text": "你好",                # 合成时的文本（可空）
                 "detach": False,              # 是否脱离台词独立定位
                 "t": None}                    # detach 时的绝对起始时刻

    **detach 语义**（时间轴语音轨「默认跟台词，可解锁」）：
      detach=False（默认）—— 语音 clip 的起点 = 台词行的 t，实时算。
        拖语音 clip 等于拖台词（改 dialogue[i].t），永远对齐。
      detach=True        —— 语音有自己的 t，可偏离台词。用于「旁白提前」
        「台词前奏」这类精细演出。此时改台词 t 不再影响语音。

    只强制 path 与非负 dur —— 其余是**元信息**，缺失不该让整条台词被拒。
    脏值只丢字段不丢台词（与 motion/expression 同款）。
    """
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    out = {"path": path.strip()}
    dur = _num(raw.get("dur"), -1.0)
    if dur >= 0:
        out["dur"] = round(dur, 3)
    for k in ("voice", "text"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v
    # detach：只有显式 True 才脱离。脏值一律当 False（保守：保持跟台词）
    if raw.get("detach") is True:
        out["detach"] = True
        t = _num(raw.get("t"), -1.0)
        if t >= 0:
            out["t"] = round(t, 3)
        else:
            # 声明了 detach 却没给 t —— 不认这个 detach（否则语音无处安放）
            out.pop("detach", None)
    return out


def _sanitize_dialogue(raw):
    """台词表 dialogue：逐项校验 t/dur 浮点 >=0、who/text 字符串；脏项丢弃。

    A4：WebGAL 导出与舞台字幕共用。缺省/非 list 一律 []，绝不抛。
    H2a（task-47）：条目可选 motion/expression（台词级动作表情），复用
    _motion_value_ok 校验（motion_table id 或 Cubism 键名形态）；**脏值
    只丢字段不丢台词**。
    """
    out = []
    if not isinstance(raw, list): return out
    for it in raw:
        if not isinstance(it, dict): continue
        t = _num(it.get("t"), -1.0)
        dur = _num(it.get("dur"), -1.0)
        if t < 0 or dur < 0: continue
        who, text = it.get("who"), it.get("text")
        if not isinstance(who, str) or not isinstance(text, str): continue
        ent = {"t": round(t, 3), "dur": round(dur, 3),
               "who": who, "text": text}
        mv = _sanitize_preset(it.get("motion"),
                              lambda: _MT.motion_ids())
        ex = _sanitize_preset(it.get("expression"),
                              lambda: _MT.expression_ids())
        if mv: ent["motion"] = mv
        if ex: ent["expression"] = ex
        vc = _sanitize_voice(it.get("voice"))
        if vc: ent["voice"] = vc
        out.append(ent)
    return out


def _sanitize_tracks(tracks, proj_for_id):
    """校验一组轨道（sanitize 内部共用：v1 顶层与 v2 每幕）。"""
    out = []
    if not isinstance(tracks, list): return out
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
            if cc: clips.append(cc)
        out.append({
            "id": tid, "kind": tr["kind"],
            "name": str(tr.get("name") or tid),
            "hidden": bool(tr.get("hidden", False)),
            "locked": bool(tr.get("locked", False)),
            "clips": clips})
    return out


def _sanitize_str_list(raw, limit=0):
    """option.set 清洗：契约形态 = 字符串列表（形如 ["met=true", "route=x"]）。

    F2-L2/L3 补丁：**同时接受单个字符串**（"met=true;route=x"）——导入端从
    WebGAL 脚本里拿到的是分号串，直接写进来；旧实现只认 list，字符串会被
    silently 洗成 []（实测 bug：伪幕吸收的 option.set 全部丢失）。
    字符串按未转义分号切分后逐条清洗，语义与列表形态一致。"""
    items = []
    if isinstance(raw, list): items = raw
    elif isinstance(raw, str): items = [x for x in raw.split(";") if x is not None]
    out = []
    for x in items:
        if isinstance(x, str) and x.strip():
            s = x.strip()[:200]
            if limit and len(out) >= limit: break
            out.append(s)
    return out


def _sanitize_choices(raw, act_aids, warn=None):
    """F2-L1 §2.2：choices 清洗（上限/悬空引用容错，绝不抛）。"""
    out = []
    for ch in (raw if isinstance(raw, list) else []):
        if not isinstance(ch, dict): continue
        if len(out) >= MAX_CHOICES_PER_ACT: break
        cid = str(ch.get("cid") or "")
        if not cid: cid = next_id("c", [c["cid"] for c in out])
        t = ch.get("t")
        try:
            t = None if t is None else round(max(0.0, float(t)), 3)
        except (TypeError, ValueError):
            t = None
        opts = []
        for o in (ch.get("options") or []):
            if not isinstance(o, dict): continue
            if len(opts) >= MAX_OPTIONS_PER_CHOICE: break
            text = str(o.get("text") or "").strip()[:100]
            if not text: continue
            tgt = o.get("target")
            tgt = str(tgt) if tgt else None
            if tgt and act_aids and tgt not in act_aids:
                # 悬空引用：保留原名（往返保真），但标记
                if warn is not None: warn.append("选项「%s」目标幕不存在: %s" % (text, tgt))
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
        if not isinstance(j, dict): continue
        try:
            t = round(max(0.0, float(j.get("t"))), 3)
        except (TypeError, ValueError):
            continue
        when = str(j.get("when") or "").strip()[:200]
        if not when: continue
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
        if not isinstance(v, dict): continue
        name = str(v.get("name") or "").strip()
        if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name): continue
        out.append({"name": name, "default": v.get("default", 0)})
    return out


def sanitize_project(obj, warn=None):
    """脏文件容错：逐字段校验 + 补默认。不合法返回 None。

    F2-L1：支持两种形状读入——
    - v1（无 acts）：顶层 tracks/dialogue 校验后**保持 v1 形状输出**
      （零破坏：既有全部测试与消费方行为全等）
    - v2（有 acts）：逐幕校验，输出 v2

    warn（可选 list）：收集容错警告（悬空分支引用等）。"""
    if not isinstance(obj, dict) or obj.get("type") != PROJECT_TYPE: return None
    # 画布参数：脏值/越界一律回默认（宽容，别因一个坏字段拒整个工程）
    w = int(_num(obj.get("width"), 1920))
    h = int(_num(obj.get("height"), 1080))
    fps = _num(obj.get("fps"), 30.0)
    if not (16 <= w <= 7680): w = 1920
    if not (16 <= h <= 7680): h = 1080
    if not (1 <= fps <= 240): fps = 30.0

    acts_raw = obj.get("acts")
    if isinstance(acts_raw, list) and acts_raw:
        # ---------- v2 多幕 ----------
        proj = {"type": PROJECT_TYPE, "version": PROJECT_VERSION,
                "width": w, "height": h, "fps": fps,
                "vars": _sanitize_vars(obj.get("vars")), "acts": []}
        # 先定 aid 集合（choices/jumps 悬空校验需要）
        aids, seen = [], set()
        for i, a in enumerate(acts_raw):
            if not isinstance(a, dict): continue
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
            if not act["tracks"]: act["tracks"] = new_act()["tracks"]
            if "comments" in a: act["comments"] = _sanitize_comments(a.get("comments"))
            if "effects" in a: act["effects"] = _sanitize_effects(a.get("effects"))
            if act["next"] and act["next"] not in act_aids and warn is not None:
                warn.append("续幕目标不存在: %s" % act["next"])
            proj["acts"].append(act)
        if not proj["acts"]: return None
        _tr = _sanitize_transition(obj.get("transition"))
        if _tr is not None: proj["transition"] = _tr      # 工程级转场（未设不写键，老工程零 diff）
        return proj

    # ---------- v1 单幕（原行为逐字节保留）----------
    tracks = obj.get("tracks")
    if not isinstance(tracks, list): return None
    proj = {"type": PROJECT_TYPE, "version": 1, "width": w, "height": h,
            "fps": fps, "tracks": _sanitize_tracks(tracks, None)}
    if not proj["tracks"]: proj["tracks"] = new_project()["tracks"]     # 至少保证有默认双轨
    # A4：台词表（WebGAL 导出与舞台字幕共用）
    proj["dialogue"] = _sanitize_dialogue(obj.get("dialogue"))
    # I3（task-67）：工程级注释载体（导入保留 → 导出转合法语法）
    proj["comments"] = _sanitize_comments(obj.get("comments"))
    # v3.5：画面特效载体（导入保留 → 导出 emit 成真实 setTransform 指令）
    proj["effects"] = _sanitize_effects(obj.get("effects"))
    _tr = _sanitize_transition(obj.get("transition"))
    if _tr is not None: proj["transition"] = _tr      # 工程级转场（未设不写键，老工程零 diff）
    return proj


# G5（task-46）：Cubism 动作/表情键名形态（`角色/动作` 命名空间形态）。
# 自检规则，不依赖 motion_table：含 "/"、长度≤64、无空白/分号/引号/冒号。
_CUBISM_KEY_BAD_RE = re.compile(r"[\s;:\"']")


_LIB_MOTION_KEYS = None       # 素材库实有键名集合（惰性构建，进程级缓存）
_LIB_KEYS_STAMP = None


def _lib_motion_keys():
    """素材库里真实存在的 motion/expression 键名（含**裸键**）。

    为什么需要：2026-11-17 并入的改模整合包里，大批模型用裸键
    （`idle01` / `kime01` …，model.json 的键名本身就是裸的），而
    motion_table 的 id 全是 `anon/idle01` 这种前缀式。只用表内 id 判定
    会把裸键当脏值清掉（用户可见：导出游戏人物没动作）。

    判定要既放行真实键、又挡住编造值，所以额外读一次素材库 model.json
    的键名做白名单扩展。读 97~138 个 model.json 约几十毫秒，用 (路径,
    mtime, size) 戳缓存，避免每次 sanitize 都重扫。

    素材库不存在/读失败 -> 返回空集（退回纯形态判定，不阻断任何操作）。
    """
    global _LIB_MOTION_KEYS, _LIB_KEYS_STAMP
    try:
        lib = os.environ.get("MUPPET_ASSET_LIBRARY") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "webgal_games", "asset_library")
        fig = os.path.join(lib, "figure")
        if not os.path.isdir(fig): return _LIB_MOTION_KEYS or set()
        # 戳：figure 目录的 mtime + 子项数量（新增/删除素材会变）
        st = os.stat(fig)
        stamp = (st.st_mtime, len(os.listdir(fig)))
        if _LIB_MOTION_KEYS is not None and _LIB_KEYS_STAMP == stamp:
            return _LIB_MOTION_KEYS
        keys = set()
        import json as _json
        for role in os.listdir(fig):
            rd = os.path.join(fig, role)
            if not os.path.isdir(rd): continue
            for cos in os.listdir(rd):
                p = os.path.join(rd, cos, "model.json")
                if not os.path.isfile(p): continue
                try:
                    o = _json.load(open(p, encoding="utf-8"))
                except Exception:
                    continue
                mo = o.get("motions")
                if isinstance(mo, dict): keys |= {str(k) for k in mo}
                elif isinstance(mo, list):
                    keys |= {str(x.get("name")) for x in mo
                             if isinstance(x, dict) and x.get("name")}
                ex = o.get("expressions")
                if isinstance(ex, dict): keys |= {str(k) for k in ex}
                elif isinstance(ex, list):
                    keys |= {str(x.get("name")) for x in ex
                             if isinstance(x, dict) and x.get("name")}
        _LIB_MOTION_KEYS = keys
        _LIB_KEYS_STAMP = stamp
        return keys
    except Exception:
        return _LIB_MOTION_KEYS or set()


def _cubism_key_ok(v):
    """值是否是**真实可用**的 Cubism 键名。

    v3.5.13 修（用户实报「导出的游戏里人物都没动作」）：
    原先要求值里必须含 "/"（`anon/idle01` 前缀式）。但 2026-11-17 并入
    素材库的改模整合包里，大批模型用**裸键**（`idle01`/`kime01`…），
    于是 `_sanitize_preset` 把它们判成脏值清掉 —— 导入 48 条 motion
    全灭，导出脚本一个 `-motion` 都不写。

    现在的判据（任一成立即放行）：
      1) 形如 `anon/idle01`（含 "/"，无非法字符）
      2) 命中**素材库实有键名**（裸键 `kime01` 在这里通过）
      素材库读不到时（未装/路径异常）退回「纯标识符形态」兜底，
      否则合法裸键会被误杀——宁可放过一个编造值，也不能把真实动作清空
      （后者的用户可见后果是「人物全都不动」）。
      脏值（空白/分号/冒号/引号/反斜杠/路径特征）在任何档位都会被拒。
    """
    if not isinstance(v, str): return False
    s = v.strip()
    if not s or len(s) > 64: return False
    if _CUBISM_KEY_BAD_RE.search(s) or "\\" in s: return False
    if s.startswith(("/", ".")) or s.endswith("/") or ".." in s: return False
    # 1) 前缀式形态（anon/idle01）
    if "/" in s: return True
    # 2) 素材库实有键名（裸键走这条）—— 这是正常路径
    lib = _lib_motion_keys()
    if lib: return s in lib
    # 3) 素材库不可用时的兜底：只放行纯标识符（不让整条链路瘫掉）
    return bool(re.match(r"^[A-Za-z0-9_\u3040-\u30ff\u4e00-\u9fff\-]+$", s))


def _motion_value_ok(v, ids=None):
    """motion/expression 保留判定的纯函数（G5，供 _sanitize_preset 与测试共用）。

    规则=并集：值命中 motion_table id 集（ids，C2 现行为）**或**值为 Cubism
    键名形态（G5 放行，AI 直接引用素材库 meta.motions 的键名）。两者都不
    满足 -> False（脏值防线不降）。ids 传 None/空 = 只按键名形态判。
    表不可用时的"非空串透传"是 _sanitize_preset 的既有兜底，不在本函数。"""
    if not isinstance(v, str) or not v.strip(): return False
    if ids and v in ids: return True
    return _cubism_key_ok(v)


def _sanitize_preset(raw, ids_fn):
    """motion/expression 透传校验：C2 表内 id 或 G5 Cubism 键名才保留，否则 None。

    motion_table 不可用（import 失败）时宽松透传非空字符串，绝不因表缺失
    拒绝整个 clip（G5 的键名形态在此兜底下同样放行）。
    """
    if not isinstance(raw, str) or not raw.strip(): return None
    if _MT is None: return raw
    try:
        ids = ids_fn()
    except Exception:
        return raw
    return raw if _motion_value_ok(raw, ids) else None


# ---- 口型匹配（v3.6.4）------------------------------------------------
# 立绘 clip 上挂一个 `lip` 字段，指向某句语音（voice clip 的 id，即
# "vc:<台词下标>"）。渲染/导出时按语音的音频包络驱动嘴部参数。
#
# 为什么记 id 而不是直接记音频路径：
#   · 语音的真相在 dialogue 里（voice_clips 是投影），用户改台词时间、
#     换语音，lip 不用跟着改；id 会自动指到新的位置
#   · 记路径的话，同一句话换音色后立绘还指着旧文件
LIP_GAIN_MIN, LIP_GAIN_MAX = 0.3, 2.5


def _sanitize_lip(raw):
    """lip 字段校验：{"voice": "vc:3", "gain": 1.0}。脏值丢键不丢 clip。

    gain 是这句话的开口度倍率（同一角色不同情绪，喊叫要更开）。
    """
    if not isinstance(raw, dict): return None
    vid = raw.get("voice")
    if not isinstance(vid, str) or not vid.strip(): return None
    vid = vid.strip()
    # 只收本模块自己产生的投影 id 形态（vc:<数字>），防止塞进任意串
    if not re.match(r"^vc:\d+$", vid): return None
    out = {"voice": vid}
    g = raw.get("gain")
    if g is not None:
        n = _num(g, 1.0)
        out["gain"] = round(min(LIP_GAIN_MAX, max(LIP_GAIN_MIN, n)), 3)
    return out


def lip_bindings(proj, aid=None):
    """全量口型绑定：{figure_clip_id: {"voice","gain"}}（无绑定 = 空 dict）。

    导出/预览都用这一份口径，避免两处各写一遍遍历。
    """
    out = {}
    if not isinstance(proj, dict): return out
    for tr in _tracks_of(proj, aid):
        if not isinstance(tr, dict) or tr.get("kind") == "audio": continue
        for c in tr.get("clips") or []:
            if not isinstance(c, dict): continue
            lp = _sanitize_lip(c.get("lip"))
            if lp:
                cid = str(c.get("id") or "")
                if cid: out[cid] = lp
    return out


def voice_clip_by_id(proj, vid, aid=None):
    """按 "vc:<下标>" 取语音 clip dict；找不到返回 None。"""
    if not isinstance(vid, str): return None
    for c in voice_clips(proj, aid):
        if c.get("id") == vid: return c
    return None


# 滤镜（原「特效」页）：只收 setTransform 那套数值键，范围按引擎语义夹紧。
# 未知键一律丢弃（导出器不认识，留着只会让工程文件漂移）。
# v3.6.2：补齐 docx《webgal电影感滤镜1.21》用到的全部键（bevel 斜面浮雕
# 系列 / bloomThreshold / blur），引擎 TransformMap 里都有。
_FILTER_KEYS = {
    # key: (下限, 上限)  —— brightness/saturation 等是倍率，0.0~3.0 足够覆盖
    "brightness": (0.0, 3.0),
    "contrast": (0.0, 3.0),
    "saturation": (0.0, 3.0),
    "gamma": (0.1, 3.0),
    "bloom": (0.0, 2.0),
    "bloomBrightness": (0.0, 2.0),
    "bloomBlur": (0.0, 100.0),
    "bloomThreshold": (0.0, 100.0),
    "colorRed": (0.0, 255.0),
    "colorGreen": (0.0, 255.0),
    "colorBlue": (0.0, 255.0),
    # 斜面浮雕（漫画/赛博感边缘光）：strength / 厚度px / 角度° / 柔化 / 颜色
    "bevel": (0.0, 10.0),
    "bevelThickness": (0.0, 100.0),
    "bevelRotation": (-360.0, 360.0),
    "bevelSoftness": (0.0, 10.0),
    "bevelRed": (0.0, 255.0),
    "bevelGreen": (0.0, 255.0),
    "bevelBlue": (0.0, 255.0),
    # 高斯模糊（docx 里「特写用 20 / 大场景用 5」）
    "blur": (0.0, 100.0),
    "alpha": (0.0, 1.0),
}
_FILTER_BOOL_KEYS = ("glitchFilm", "rgbFilm", "oldFilm", "dotFilm")


def _sanitize_filter(raw):
    """滤镜参数校验：数值键夹到合法区间，布尔键只收真布尔。

    返回 None 表示"没有有效滤镜"（调用方据此不写 filter 键）。
    """
    if not isinstance(raw, dict): return None
    out = {}
    for k, (lo, hi) in _FILTER_KEYS.items():
        if k not in raw: continue
        v = raw.get(k)
        if isinstance(v, bool): continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv != fv:            # NaN
            continue
        out[k] = round(max(lo, min(hi, fv)), 4)
    for k in _FILTER_BOOL_KEYS:
        v = raw.get(k)
        if isinstance(v, bool) and v: out[k] = True
    return out or None


def _sanitize_clip(c, seen_clip_ids):
    if not isinstance(c, dict): return None
    src = c.get("source")
    if not isinstance(src, dict) or src.get("kind") not in _KINDS: return None
    path = str(src.get("path") or "")
    if not path: return None
    cid = str(c.get("id") or "")
    if cid in seen_clip_ids: cid = ""            # 让调用方后面重排；先保留 clip
    start = max(0.0, _num(c.get("start"), 0.0))
    dur = _num(c.get("dur"), 0.0)
    if dur < MIN_DUR - EPS: return None
    src_in = max(0.0, _num(c.get("src_in"), 0.0))
    _src_out = {"kind": src["kind"], "path": path,
                "w": int(_num(src.get("w"), 0)),
                "h": int(_num(src.get("h"), 0)),
                "fps": _num(src.get("fps"), 0.0),
                "src_dur": max(0.0, _num(src.get("src_dur"), 0.0))}
    # 素材库引用标记透传：音频页直接引用包内文件时记下相对路径，
    # 导出 WebGAL 时可直接写回素材库相对路径，省一次拷贝。
    for _k in ("lib_file", "lib_name"):
        _v = src.get(_k)
        if isinstance(_v, str) and _v.strip(): _src_out[_k] = _v.strip()
    out = {"id": cid,
           "source": _src_out,
           "start": round(start, 3), "src_in": round(src_in, 3),
           "dur": round(dur, 3), "speed": 1.0,
           "layout": _sanitize_layout(c.get("layout"))}
    # 滤镜（「特效」页改名后新增）：setTransform 参数的子集，脏值丢键不丢 clip
    _filt = _sanitize_filter(c.get("filter"))
    if _filt: out["filter"] = _filt
    # C2：motion / expression 透传（脏值丢键不丢 clip）
    mv = _sanitize_preset(c.get("motion"), _MT.motion_ids)
    ex = _sanitize_preset(c.get("expression"), _MT.expression_ids)
    if mv: out["motion"] = mv
    if ex: out["expression"] = ex
    # 口型匹配（v3.6.4）：figure clip 挂在哪句语音上。
    # 形状 {"voice": <voice_clip_id>, "gain": 0.5~2.0}
    lp = _sanitize_lip(c.get("lip"))
    if lp: out["lip"] = lp
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
    if not isinstance(clip, dict): return False
    src = clip.get("source") or {}
    sk = src.get("kind")
    if sk not in _FIGURE_SRC_KINDS: return False
    if sk == "puppet": return True
    tr_name = str((track or {}).get("name") or "").upper()
    return tr_name.startswith("FIG")


def figure_clips_at(proj, t):
    """时刻 t（含）生效的立绘 clip 列表（dict 引用，按轨道序）。"""
    out = []
    if not isinstance(proj, dict): return out
    try:
        tf = float(t)
    except (TypeError, ValueError):
        return out
    for tr in _tracks_of(proj):
        if not isinstance(tr, dict) or tr.get("kind") == "audio": continue
        for c in tr.get("clips") or []:
            if not is_figure_clip(c, tr): continue
            s = _num(c.get("start"), 0.0)
            if s <= tf < s + _num(c.get("dur"), 0.0): out.append(c)
    return out


def figure_intervals(proj, exclude_cid=None):
    """全部立绘 clip 的 (start, end, clip) 区间表（end 开区间右端）。"""
    iv = []
    if not isinstance(proj, dict): return iv
    for tr in _tracks_of(proj):
        if not isinstance(tr, dict) or tr.get("kind") == "audio": continue
        for c in tr.get("clips") or []:
            if exclude_cid is not None and c.get("id") == exclude_cid: continue
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
    if a1 <= a0: return 0
    ivs = figure_intervals(proj, exclude_cid=exclude_cid)
    pts = {a0, a1}
    for s, e, _c in ivs:
        if a0 <= s < a1: pts.add(s)
        if a0 < e <= a1: pts.add(e)
    sp = sorted(pts)
    best = 0
    for a, b in zip(sp, sp[1:]):
        if b <= a: continue
        mid = (a + b) / 2.0
        n = sum(1 for s, e, _c in ivs if s <= mid < e)
        if n > best: best = n
    return best


def figure_occupancy_note(proj, t0, t1, exclude_cid=None):
    """K1（task-69）后为**纯信息展示**文案（原 I1 拒绝报错用的占位清单）：
    取 [t0,t1) 内首次达到峰值人数的时刻，列出生效立绘的 轨道名@区间。"""
    ivs = [(s, e, c) for s, e, c in figure_intervals(proj, exclude_cid)
           if e > t0 and s < t1]
    if not ivs: return ""
    pts = sorted({t0} | {s for s, _e, _c in ivs if t0 <= s < t1}
                 | {e for _s, e, _c in ivs if t0 < e <= t1} | {t1})
    peak, peak_t = 0, t0
    for a, b in zip(pts, pts[1:]):
        if b <= a: continue
        mid = (a + b) / 2.0
        n = sum(1 for s, e, _c in ivs if s <= mid < e)
        if n > peak: peak, peak_t = n, mid
    if peak <= 0: return ""
    names = []
    for tr in _tracks_of(proj):
        for c in tr.get("clips") or []:
            if exclude_cid is not None and c.get("id") == exclude_cid: continue
            if not is_figure_clip(c, tr): continue
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
        if tr["id"] in used: tr["id"] = next_track_id(proj, tr["kind"])
        used.add(tr["id"])
        for c in tr["clips"]:
            if not c["id"] or c["id"] in used: c["id"] = next_clip_id(proj)
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
