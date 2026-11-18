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
    if not d:
        return None
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
    if not isinstance(raw, list):
        return []
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or "")
        if kind not in AUDIO_KINDS:
            continue
        rel = str(it.get("file") or "").replace("\\", "/").strip()
        if not rel:
            continue
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
    if not p:
        return []
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
    if not rel:
        return None
    for e in items():
        if e["file"] == rel:
            return e
    return None


def audio_path(item):
    """条目对应的绝对路径（不校验存在性，交给调用方判空）。"""
    try:
        root = AP.ensure_library(scan=False)
    except Exception:
        root = None
    if not root:
        return ""
    rel = str((item or {}).get("file") or "").replace("/", os.sep)
    return os.path.join(root, rel) if rel else ""


def search(query):
    """按名字 / 标签 / 相对路径模糊搜（大小写无关）。"""
    q = str(query or "").strip().lower()
    if not q:
        return items()
    out = []
    for e in items():
        hay = " ".join([e["name"].lower(), e["file"].lower(),
                        " ".join(e["tags"]).lower()])
        if q in hay:
            out.append(e)
    return out


def stats():
    """给状态栏/调试用的汇总。"""
    all_ = items()
    bgm = sum(1 for e in all_ if e["kind"] == "bgm")
    return {"total": len(all_), "bgm": bgm, "sfx": len(all_) - bgm,
            "library": AP.ensure_library(scan=False) or ""}
