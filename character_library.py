# -*- coding: utf-8 -*-
"""角色 / 模型索引（供视频编辑「角色」页：按角色分组展开，缩略图选择）。

数据来源：素材库 asset_index.json 的 model 条目（138 套，11 个角色）。

「改模作者」信息的来源：原始改模压缩包的**包名**里写着作者
（例：「B站北极暖水港老师的改模和动作使用请@.zip」）。包内全部模型
都归该作者。索引里没有这个字段，所以此处用 MOD_AUTHORS 显式登记
「模型目录名 -> 作者」，只登记确知来源的；没有记录的不显示括号。

对外 API：
    roles()                    -> [{"slug","name","cn","count"}]
    models_of(slug)            -> [dict]  该角色全部模型（已排序）
    all_models()               -> [dict]
    model_path(m)              -> str     模型目录绝对路径
    thumb_path(m)              -> str     缩略图绝对路径（可能不存在）
    display_name(m)            -> str     「服装名（作者）」
    refresh(force=False)       -> None
"""
import io
import json
import os
import re
import threading

import asset_paths as AP

# 角色 slug -> 中文名（顺序即 UI 展示顺序：MyGO 五人 + Ave Mujica + 其他）
ROLE_INFO = [
    ("tomori", "高松灯"),
    ("anon", "千早爱音"),
    ("soyo", "长崎素世"),
    ("taki", "椎名立希"),
    ("rana", "要乐奈"),
    ("sakiko", "丰川祥子"),
    ("mutsumi", "若叶睦"),
    ("uika", "三角初华"),
    ("umiri", "八幡海铃"),
    ("nyamu", "祐天寺若麦"),
    ("mana", "纯田真奈"),
    ("lib", "其他"),          # 未定角色的模型（如其他企划的改模）
]
_ROLE_NAME = dict(ROLE_INFO)
_ROLE_ORDER = {s: i for i, (s, _) in enumerate(ROLE_INFO)}
# 「其他」永远排最后（模型数可能很多，但优先级最低）
if "lib" in _ROLE_ORDER:
    _ROLE_ORDER["lib"] = 999

# ---- 改模作者登记 ----------------------------------------------------------
# 键 = 模型目录名（figure/<角色>/ 下那一层），值 = 作者名。
# 来源：合并时用的原压缩包包名。当前只合并过「北极暖水港」这一个包，
# 其内层 zip 解出的目录就是下面这些（2026-11-18 核对 asset_index 后加条目，
# id 序号 > 97 的 41 条即该包）。
_AUTHOR_BEIJI = "北极暖水港"
_MOD_BY_AUTHOR = {
    _AUTHOR_BEIJI: [
        "千早爱音_Sweets×Flower Tea Party!联动_v2",
        "千早爱音_月之森_v1.0",
        "千早爱音_花咲川_v1.0",
        "千早爱音_花咲川_夏校服_v1.0",
        "祥化睦头人_mutsumi_saki_338_school_winter-2023_rip_v1",
        "若叶睦_月之森jh冬装_v1",
        "丰川祥子-2023常服-被绑架",
        "丰川祥子-羽丘冬装-草莓版",
        "丰川祥子_TeaParty联动_黑裙_v1.0",
        "丰川祥子_娇羞版_夏季校服_V1",
        "丰川祥子_娇羞版_夏季校服_V1.2",
        "丰川祥子_月之森JK_v1",
        "丰川祥子_月之森JK夏装_v1",
        "丰川祥子_短款女仆装_Soyo签名版_v1.8",
        "丰川祥子_短款女仆装_通用款_v1.8",
        "丰川祥子_花咲川_v1.0",
        "丰川祥子_花咲川_夏校服_v1.0",
        "丰川祥子_赛博牢祥_v2.1",
        "丰川祥子_逆转裁判_v1",
        "丰川祥子_素魂祥_整合包_v1",
        "丰川祥子_赛博牢祥_V1",
        "丰川祥子_赛博牢祥_v2",
        "长崎素世-冬装校服-被绑架",
        "长崎素世-月之森初中校服_v2",
        "长崎素世_wego_2024_联名",
        "长崎素世_wego联名_2024_v2",
        "长崎素世_wego联名_2024_v2α",
        "椎名立希_短发_V1",
        "椎名立希_娇羞版_夏季校服_V1",
        "椎名立希_灯的身体，立希的灵魂_2023常服_V2",
        "高松灯_立希的身体，灯的灵魂_2023常服_V2",
        "高松灯_力量型_V2",
        "高松灯_力量型_夏季校服_V2",
        "高松灯_力量型_夏季校服_V3",
        "高松灯_力量型_夏季校服_V5",
        "高松灯_月之森_v1.0",
        "高松灯_花咲川_v1.0",
        "高松灯_花咲川_夏校服_v1.0",
        "三角初华_羽丘高一_v1.0",
        "八幡海铃_米色水手服_v1.0",
        "八幡海铃_羽丘高一_v1.0",
    ],
}

# 目录名 -> 作者 反查表（内置登记，作为「目录里没写说明文件」时的回退）
_AUTHOR_OF = {}
for _a, _dirs in _MOD_BY_AUTHOR.items():
    for _d in _dirs:
        _AUTHOR_OF[_d] = _a

# 模型目录里可能出现的「作者说明」文件名前缀（作者名就在文件名里）
_AUTHOR_FILE_HINTS = ("改模作者", "作者", "credit", "author")


def _author_from_dir(d):
    """从模型目录里读作者：文件名形如「改模作者：bilibili北极暖水港.txt」。

    返回清洗后的作者名（去掉 bilibili/B站 前缀），读不到返回 ""。
    """
    if not d or not os.path.isdir(d):
        return ""
    try:
        names = os.listdir(d)
    except Exception:
        return ""
    for n in names:
        if not any(h in n for h in _AUTHOR_FILE_HINTS):
            continue
        stem = os.path.splitext(n)[0]
        # 「改模作者：bilibili北极暖水港」/「改模作者：B站xxx」/「改模作者.txt」
        for sep in (":", "：", "-", "—"):
            if sep in stem:
                stem = stem.split(sep, 1)[1]
                break
        a = stem.strip()
        for pre in ("bilibili", "Bilibili", "BILIBILI", "B站", "b站"):
            if a.startswith(pre):
                a = a[len(pre):]
        a = a.strip(" _-—·.")
        if a:
            return a
    return ""


# 缩略图候选文件名（模型目录自带预览图时优先用）
_PREVIEW_NAMES = ("预览图.jpg", "预览图.png", "预览.png", "预览.jpg",
                  "preview.png", "preview.jpg", "thumbnail.png",
                  "thumbnail.webp", "thumb.png", "cover.png", "icon.png")

_lock = threading.Lock()
_cache = None
_cache_stamp = None


def _index_path():
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


def _role_of(rel):
    """figure/<slug>/<dir> -> slug；不符合形态返回 None。"""
    parts = str(rel or "").replace("\\", "/").strip().split("/")
    if len(parts) < 2 or parts[0] != "figure":
        return None
    return parts[1] or None


def _costume_dir(rel):
    parts = str(rel or "").replace("\\", "/").strip().split("/")
    return parts[2] if len(parts) >= 3 else ""


def _load(p):
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for it in raw:
        if not isinstance(it, dict) or it.get("kind") != "model":
            continue
        rel = str(it.get("file_rel") or it.get("file") or "").replace(
            "\\", "/").strip()
        if not rel:
            continue
        slug = _role_of(rel)
        if not slug:
            continue
        cdir = _costume_dir(rel)
        name = str(it.get("name") or "")
        # 服装名 = 「角色·服装」里 · 之后那段；没有 · 就用目录名
        costume = name.split("·", 1)[1] if "·" in name else (cdir or name)
        out.append({
            "id": str(it.get("id") or rel),
            "slug": slug,
            "role_cn": _ROLE_NAME.get(slug, slug),
            "file": rel,
            "dir": cdir,
            "name": name,
            "costume": costume,
            "author": "",        # 由 _fill_author() 延迟填（要读目录，批量时慢）
            "tags": [str(t) for t in (it.get("tags") or [])],
            "meta": it.get("meta") if isinstance(it.get("meta"), dict) else {},
        })
    out.sort(key=lambda e: (_ROLE_ORDER.get(e["slug"], 99), e["costume"]))
    return out


def refresh(force=True):
    global _cache
    items(force=force)
    return _cache


def items(force=False):
    global _cache, _cache_stamp
    p = _index_path()
    if not p:
        return []
    st = _stamp(p)
    with _lock:
        if force or _cache is None or _cache_stamp != st:
            _cache = _load(p)
            _cache_stamp = st
            _enrich(_cache)
        return list(_cache)


def _enrich(cache):
    """补齐每条的 author / thumb（读目录，只在索引变化时跑一次）。"""
    try:
        root = AP.ensure_library(scan=False)
    except Exception:
        root = None
    if not root:
        return
    for m in cache:
        d = os.path.join(root, str(m["file"]).replace("/", os.sep))
        a = _author_from_dir(d)
        if not a:
            a = _AUTHOR_OF.get(m.get("dir", ""), "")
        m["author"] = a
        m["_dir_abs"] = d


def roles():
    """角色列表（按 ROLE_INFO 顺序，只保留真的有模型的）。"""
    all_ = items()
    seen = {}
    for m in all_:
        seen.setdefault(m["slug"], 0)
        seen[m["slug"]] += 1
    out = []
    for slug, cn in ROLE_INFO:
        if slug in seen:
            out.append({"slug": slug, "cn": cn, "count": seen[slug]})
    # 未登记的角色兜底排在后面
    for slug, n in seen.items():
        if slug not in _ROLE_NAME:
            out.append({"slug": slug, "cn": slug, "count": n})
    return out


def models_of(slug):
    return [m for m in items() if m["slug"] == slug]


def all_models():
    return items()


def model_path(m):
    try:
        root = AP.ensure_library(scan=False)
    except Exception:
        root = None
    if not root:
        return ""
    rel = str((m or {}).get("file") or "").replace("/", os.sep)
    return os.path.join(root, rel) if rel else ""


def display_name(m):
    """UI 显示名：服装名（作者）。没有作者就只显示服装名。"""
    m = m or {}
    c = str(m.get("costume") or m.get("name") or m.get("dir") or "")
    a = str(m.get("author") or "")
    return "%s（%s）" % (c, a) if a else c


def thumb_path(m):
    """缩略图路径（找不到返回 ""）。

    优先级：
      1. **_thumb.png** —— L2D 实时渲染后落盘的合成图（最真实，用户要这个）
      2. 模型目录自带的「预览图.*」等现成合成图
      3. Cubism2 图集（.chara/texture_00）—— 散件图，仅作最后兜底
    """
    d = (m or {}).get("_dir_abs") or model_path(m)
    if not d or not os.path.isdir(d):
        return ""
    # 渲染产物优先
    t = os.path.join(d, "_thumb.png")
    if os.path.isfile(t):
        return t
    for n in _PREVIEW_NAMES:
        p = os.path.join(d, n)
        if os.path.isfile(p):
            return p
    # 改模包常见结构 ①：live2d/chara/<编号角色>/texture_*.png
    l2d = os.path.join(d, "live2d")
    if os.path.isdir(l2d):
        for dp, dn, fn in os.walk(l2d):
            for f in sorted(fn):
                if f.lower().endswith((".png", ".webp")) and "texture" in f.lower():
                    return os.path.join(dp, f)
    # 改模包常见结构 ②：data/textures/texture_*.png（紫铜Kris 部分模型）
    for sub in ("data", "textures"):
        dd = os.path.join(d, sub)
        if os.path.isdir(dd):
            for dp, dn, fn in os.walk(dd):
                for f in sorted(fn):
                    if f.lower().endswith((".png", ".webp")) and "texture" in f.lower():
                        return os.path.join(dp, f)
    # 兜底：模型目录下任意一层找 texture_*.png
    for dp, dn, fn in os.walk(d):
        if os.path.basename(dp).startswith("."):
            continue
        for f in sorted(fn):
            if f.lower().startswith("texture") and f.lower().endswith(
                    (".png", ".webp")):
                return os.path.join(dp, f)
    # 退到图集
    ch = os.path.join(d, ".chara")
    if os.path.isdir(ch):
        try:
            files = sorted(os.listdir(ch))
        except Exception:
            files = []
        for f in files:
            if f.lower().endswith((".png", ".webp", ".jpg")) and "texture" in f:
                return os.path.join(ch, f)
        for f in files:
            if f.lower().endswith((".png", ".webp", ".jpg")):
                return os.path.join(ch, f)
    for f in ("texture_00.png", "texture.png"):
        p = os.path.join(d, f)
        if os.path.isfile(p):
            return p
    return ""


def stats():
    all_ = items()
    roles_ = roles()
    with_author = sum(1 for m in all_ if m.get("author"))
    return {"models": len(all_), "roles": len(roles_),
            "with_author": with_author,
            "library": AP.ensure_library(scan=False) or ""}


def needs_render(m):
    """该模型是否需要 L2D 渲染。

    返回 False 的情况（都不该浪费渲染时间）：
      · 已经有 _thumb.png（渲染过）
      · 有现成合成预览图（预览图.jpg 等）
      · **没有 model.json**（空壳目录：原包解压缺失，渲染必然 404）
      · 目录不存在
    """
    d = (m or {}).get("_dir_abs") or model_path(m)
    if not d or not os.path.isdir(d):
        return False
    if os.path.isfile(os.path.join(d, "_thumb.png")):
        return False
    for n in _PREVIEW_NAMES:
        if os.path.isfile(os.path.join(d, n)):
            return False
    # 必须有真实 model.json 才能渲染（空壳目录跳过）
    if not os.path.isfile(os.path.join(d, "model.json")):
        return False
    return True


def renderable_models():
    """真正可渲染的模型（有 model.json 且缺合成图）。"""
    return [m for m in items() if needs_render(m)]
