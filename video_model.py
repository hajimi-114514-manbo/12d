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
    }

规则（第一阶段拍板）：
  - 同一轨内 clip **不允许重叠**：移动/trim 撞邻接直接拒绝（返回错误），
    不做 ripple——可预测比花哨重要。
  - image 是无限长素材：src_in 恒 0，trim 两端只动 start/dur。
  - video/puppet/audio 受 src_dur 上限约束：src_in + dur <= src_dur + EPS。
  - 时间吸附精度 0.01s；重叠判定容差 EPS=0.005s。
"""
import copy

EPS = 0.005            # 重叠/边界判定容差（秒）
MIN_DUR = 0.05         # clip 最短时长（秒）
SNAP_STEP = 0.01       # 吸附/对齐精度（秒）

PROJECT_TYPE = "video_edit"


# ---------------- 构造 ----------------
def new_project(width=1920, height=1080, fps=30):
    """默认一条视频轨 + 一条音频轨。"""
    return {
        "type": PROJECT_TYPE, "version": 1,
        "width": int(width), "height": int(height), "fps": float(fps),
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
            "dur": round(float(dur), 3), "speed": 1.0}


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
    return proj


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
    return {"id": cid,
            "source": {"kind": src["kind"], "path": path,
                       "w": int(_num(src.get("w"), 0)),
                       "h": int(_num(src.get("h"), 0)),
                       "fps": _num(src.get("fps"), 0.0),
                       "src_dur": max(0.0, _num(src.get("src_dur"), 0.0))},
            "start": round(start, 3), "src_in": round(src_in, 3),
            "dur": round(dur, 3), "speed": 1.0}


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
