# -*- coding: utf-8 -*-
"""素材库音频索引（供视频编辑「音频」页列出包内音频，试听并拖进时间轴）。

为什么单独一层：
  · asset_index.json 是 1512 条的全量索引，视频工作台只需要其中的音频
    （bgm 286 + sfx 35），没必要每次都全量解析；
  · 解析结果按索引文件的 mtime 缓存，切 Pivot 页签不重复读盘；
  · 客观数据与 UI 解耦，纯函数可单测（不 import Qt）。

对外 API：
    items()                 -> [dict]  全部音频条目（已排序）
    by_kind(kind)           -> [dict]  按 "bgm"/"sfx" 过滤
    find(rel)               -> dict|None  按 file 相对路径查
    audio_path(item)        -> str     绝对路径（文件不存在仍返回路径）
    refresh(force=False)    -> [dict]  强制重读索引
    search(query)           -> [dict]  按文件名/标签模糊搜
"""
import io
import json
import os
import re
import threading

import asset_paths as AP

# 音频类 kind（asset_index.json 里 bgm 与 sfx 都是音频）
AUDIO_KINDS = ("bgm", "sfx")
AUDIO_EXTS = (".mp3", ".ogg", ".wav", ".m4a", ".flac", ".aac", ".opus")

_lock = threading.Lock()
_cache = None            # list[dict]
_cache_stamp = None      # (索引路径, mtime, size)


def _index_path():
    """素材库 asset_index.json 路径；库不可用返回 None。"""
    try:
        d = AP.ensure_library(scan=False)
    except Exception:
        d = None
    if not d: return None
    p = os.path.join(d, "asset_index.json")
    return p if os.path.isfile(p) else None


def _stamp(p):
    try:
        st = os.stat(p)
        return (p, int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        return (p, 0, 0)


def _load(p):
    """读索引，只留下音频条目并规范化。"""
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception:
        return []
    if not isinstance(raw, list): return []
    out = []
    for it in raw:
        if not isinstance(it, dict): continue
        kind = str(it.get("kind") or "")
        if kind not in AUDIO_KINDS: continue
        rel = str(it.get("file") or "").replace("\\", "/").strip()
        if not rel: continue
        if not rel.lower().endswith(AUDIO_EXTS):
            # 索引里偶尔有同名但非音频的条目，按扩展名再筛一道
            continue
        meta = it.get("meta") if isinstance(it.get("meta"), dict) else {}
        dur = meta.get("duration_s")
        try:
            dur = float(dur) if dur is not None else 0.0
        except (TypeError, ValueError):
            dur = 0.0
        name = str(it.get("name") or os.path.splitext(
            os.path.basename(rel))[0])
        tags = it.get("tags") if isinstance(it.get("tags"), list) else []
        out.append({
            "id": str(it.get("id") or rel),
            "kind": kind,
            "file": rel,
            "name": name,
            "tags": [str(t) for t in tags],
            "dur": dur,
            "size": int(meta.get("size") or 0) if meta.get("size") else 0,
        })
    # 排序：先 bgm 后 sfx，再按名字自然序（数字开头按数值排更符合直觉）
    def _key(e):
        return (0 if e["kind"] == "bgm" else 1,
                _natkey(e["name"]))
    out.sort(key=_key)
    return out


def _natkey(s):
    """自然排序键：'03_Normal' < '10_X'（数字段按数值比）。"""
    import re
    parts = re.split(r"(\d+)", str(s).lower())
    return tuple((int(x) if x.isdigit() else x) for x in parts)


def items(force=False):
    """全部音频条目（带缓存）。库不可用返回 []。"""
    global _cache, _cache_stamp
    p = _index_path()
    if not p: return []
    st = _stamp(p)
    with _lock:
        if force or _cache is None or _cache_stamp != st:
            _cache = _load(p)
            _cache_stamp = st
        return list(_cache)


def refresh(force=True):
    return items(force=True)


def by_kind(kind):
    return [e for e in items() if e["kind"] == kind]


def find(rel):
    rel = str(rel or "").replace("\\", "/").strip()
    if not rel: return None
    for e in items():
        if e["file"] == rel: return e
    return None


def audio_path(item):
    """条目对应的绝对路径（不校验存在性，交给调用方判空）。"""
    try:
        root = AP.ensure_library(scan=False)
    except Exception:
        root = None
    if not root: return ""
    rel = str((item or {}).get("file") or "").replace("/", os.sep)
    return os.path.join(root, rel) if rel else ""


def search(query):
    """按名字 / 标签 / 相对路径模糊搜（大小写无关）。"""
    q = str(query or "").strip().lower()
    if not q: return items()
    out = []
    for e in items():
        hay = " ".join([e["name"].lower(), e["file"].lower(),
                        " ".join(e["tags"]).lower()])
        if q in hay: out.append(e)
    return out


def stats():
    """给状态栏/调试用的汇总。"""
    all_ = items()
    bgm = sum(1 for e in all_ if e["kind"] == "bgm")
    return {"total": len(all_), "bgm": bgm, "sfx": len(all_) - bgm,
            "library": AP.ensure_library(scan=False) or ""}


# ---------------------------------------------------------------- 分组
# 音频页默认按组折叠展示（一次画 321 张会卡近 1 秒）。
# 分组依据：素材库 tags 里的情绪/场景词 + 用途兜底。
_GROUP_DEFS = [
    # (组 id, 中文名, 命中函数)
    ("calm", "平静", lambda e: _has(e, ("calm", "平静", "放松", "轻松"))),
    ("daily", "日常", lambda e: _has(e, ("daily", "日常", "bright", "明快"))),
    ("sad", "悲伤", lambda e: _has(e, ("sad", "悲伤", "emotional", "感动",
                                      "流泪"))),
    ("tense", "紧张", lambda e: _has(e, ("tense", "紧张", "不安", "serious",
                                        "严肃", "战斗"))),
    ("rain", "雨天", lambda e: _has(e, ("rain", "雨天", "雨"))),
    ("band", "乐队排练", lambda e: _has(e, ("band", "排练", "live", "演出"))),
    ("mujica_ost", "Mujica OST",
     lambda e: "mujica_ost" in str(e.get("file") or "")),
    ("mygo_ost", "MyGO OST",
     lambda e: "mygo" in str(e.get("file") or "").lower()),
    ("sfx", "音效", lambda e: e.get("kind") == "sfx"),
    # 兜底组按名字首字符细分（184 条一次展开也会卡，切两刀到几十条）
    ("other_0", "其他 BGM · 数字/符号",
     lambda e: bool(re.match(r"^[0-9\W_]", str(e.get("name") or "")))),
    ("other_a", "其他 BGM · A-M",
     lambda e: bool(re.match(r"^[A-Ma-m]", str(e.get("name") or "")))),
    ("other_n", "其他 BGM · N-Z",
     lambda e: bool(re.match(r"^[N-Zn-z]", str(e.get("name") or "")))),
    ("other", "其他 BGM · 其余", lambda e: True),      # 兜底，必须最后
]


def _has(e, words):
    hay = " ".join([str(e.get("name") or ""), str(e.get("file") or ""),
                    " ".join(str(t) for t in (e.get("tags") or []))]).lower()
    return any(str(w).lower() in hay for w in words)


def groups():
    """按组切分音频：返回 [{"id","name","items"}]，空组剔除。

    每首只归**第一个命中的组**（顺序即优先级），保证不重复。
    """
    all_ = items()
    buckets = {gid: [] for gid, _, _ in _GROUP_DEFS}
    for e in all_:
        for gid, _cn, pred in _GROUP_DEFS:
            try:
                if pred(e):
                    buckets[gid].append(e)
                    break
            except Exception:
                continue
    out = []
    for gid, cn, _pred in _GROUP_DEFS:
        arr = buckets.get(gid) or []
        if arr: out.append({"id": gid, "name": cn, "items": arr})
    return out


def group_of(gid):
    for g in groups():
        if g["id"] == gid: return g
    return None
