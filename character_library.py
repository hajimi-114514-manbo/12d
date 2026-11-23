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

导入 API（v3.6.3，用户要的「导入模型」按钮）：
    register_role(cn)          -> slug    新建角色分类，返回 slug
    custom_roles()             -> [{"slug","cn"}]   用户自建的分类
    add_model(...)             -> (ok, msg, entry)  复制进素材库 + 写索引 + 刷新

**导入落盘规则（用户明确：「导入就直接复制存入木偶内部」）**：
    源目录整份复制到 <素材库>/figure/<角色>/<目录名>/，再往 asset_index.json
    追加一条 model 条目。复制而不是引用——这样素材库自包含，用户把库目录
    拷走/换机器都能用，不依赖源目录还在不在。
"""
import contextlib
import io
import json
import os
import re
import shutil
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
if "lib" in _ROLE_ORDER: _ROLE_ORDER["lib"] = 999

_HERE = os.path.dirname(os.path.abspath(__file__))
_CUSTOM_ROLES_FILE = os.path.join(_HERE, ".runtime", "custom_roles.json")

# Windows 目录名禁区：这些名字建出来的目录会各种解析错乱（CON/NUL/COM1…）
_WIN_RESERVED = (
    {"con", "prn", "aux", "nul"}
    | {"com%d" % i for i in range(1, 10)}
    | {"lpt%d" % i for i in range(1, 10)}
)
_SLUG_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _slug_of(s):
    """把任意名字变成可作目录名的 slug：去 Windows 非法字符、去首尾点和空白。

    保留汉字——素材库目录名本来就有中文（Harmony_Parade_爱音），中文 slug
    在磁盘和索引里都没问题，比生成 role_a3f9 这种天书可读。
    """
    s = _SLUG_BAD.sub("", str(s or ""))
    s = s.strip().strip(".")
    s = re.sub(r"\s+", "_", s)
    if s.lower() in _WIN_RESERVED:
        s = "_" + s
    return s or "model"


# ---- 自定义角色分类（用户「新建分类」）-------------------------------------
def _custom_roles_path():
    """分类登记文件路径。

    放程序目录的 .runtime，不放素材库：素材库是**素材数据**，分类登记是
    应用的**用户数据**，两者不该混（用户清素材库不该把分类登记带走）。

    **只读目录兜底**：装到 Program Files 时程序目录不可写，.runtime 建不出来
    -> 退回用户数据目录（APPDATA/MuppetStudio）。同 l2d_rig_qt._runtime_file
    的处理，不兜底的话「新建分类」在只读安装下会直接失败。

    可用 env MUPPET_CUSTOM_ROLES 覆盖（测试用，避免污染真实环境）。
    """
    env = os.environ.get("MUPPET_CUSTOM_ROLES", "").strip()
    if env: return env
    d = os.path.dirname(_CUSTOM_ROLES_FILE)
    try:
        os.makedirs(d, exist_ok=True)
        if os.access(d, os.W_OK):
            return _CUSTOM_ROLES_FILE
    except Exception:
        pass
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        alt = os.path.join(base, "MuppetStudio", "custom_roles.json")
        try:
            os.makedirs(os.path.dirname(alt), exist_ok=True)
            return alt
        except Exception:
            pass
    return _CUSTOM_ROLES_FILE


def _load_custom_roles():
    try:
        raw = json.load(io.open(_custom_roles_path(), encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list): return []
    out = []
    for it in raw:
        if not isinstance(it, dict): continue
        slug = str(it.get("slug") or "").strip()
        if not slug: continue
        out.append({"slug": slug, "cn": str(it.get("cn") or slug)})
    return out


def _save_custom_roles(items):
    try:
        d = os.path.dirname(_custom_roles_path())
        os.makedirs(d, exist_ok=True)
        with io.open(_custom_roles_path(), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def custom_roles():
    """用户自建的分类 [{"slug","cn"}]（不含内置 ROLE_INFO）。"""
    return _load_custom_roles()


def _sync_role_tables():
    """把自定义分类并进 _ROLE_NAME / _ROLE_ORDER（注册后调用）。

    自定义分类排在内置之后，保证内置角色（MyGO 五人等）的展示顺序稳定。
    """
    global _ROLE_NAME, _ROLE_ORDER
    _ROLE_NAME = dict(ROLE_INFO)
    order = [s for s, _ in ROLE_INFO]
    for it in _load_custom_roles():
        _ROLE_NAME.setdefault(it["slug"], it["cn"])
        order.append(it["slug"])
    _ROLE_ORDER = {s: i for i, s in enumerate(order)}
    if "lib" in _ROLE_ORDER: _ROLE_ORDER["lib"] = 999


def register_role(cn):
    """新建角色分类，返回它的 slug（同名已存在则直接复用）。

    slug 由分类名生成（保留汉字）。撞内置 slug 或已注册 slug 时加序号。
    """
    cn = str(cn or "").strip()
    if not cn: raise ValueError("分类名不能为空")
    items = _load_custom_roles()
    for it in items:
        if it["cn"] == cn: return it["slug"]
    base = _slug_of(cn)
    slug, n = base, 2
    while slug in _ROLE_NAME or any(it["slug"] == slug for it in items):
        slug = "%s_%d" % (base, n); n += 1
    items.append({"slug": slug, "cn": cn})
    if not _save_custom_roles(items):
        raise IOError("自定义分类写入失败：%s" % _custom_roles_path())
    _sync_role_tables()
    return slug

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

    两个坑（都踩过）：
      · 有的包只留了「改模作者.txt」，文件名里没有作者名 —— 剥掉标签后
        只剩空串，不能再把标签本身（"改模作者"）当作者名返回。
      · 同名标签文件可能和真作者文件并存（「改模作者.txt」+「改模作者：
        bilibili北极暖水港.txt」），os.listdir 顺序不可靠 —— 必须优先取
        能解析出真名的那一个。
    """
    if not d or not os.path.isdir(d): return ""
    try:
        names = os.listdir(d)
    except Exception:
        return ""
    best = ""
    for n in names:
        if not any(h in n for h in _AUTHOR_FILE_HINTS): continue
        stem = os.path.splitext(n)[0]
        # 「改模作者：bilibili北极暖水港」/「改模作者：B站xxx」/「改模作者.txt」
        for sep in (":", "：", "-", "—"):
            if sep in stem:
                stem = stem.split(sep, 1)[1]
                break
        a = stem.strip()
        for pre in ("bilibili", "Bilibili", "BILIBILI", "B站", "b站"):
            if a.startswith(pre): a = a[len(pre):]
        a = a.strip(" _-—·.")
        # 剥完只剩标签本身 => 这个文件没写作者名，跳过
        if not a or a in _AUTHOR_FILE_HINTS: continue
        # 只有一个作者名，取第一个有效即可；无效的已被跳过
        return a
    return best


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
    if not d: return None
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
    if len(parts) < 2 or parts[0] != "figure": return None
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
    if not isinstance(raw, list): return []
    out = []
    for it in raw:
        if not isinstance(it, dict) or it.get("kind") != "model": continue
        rel = str(it.get("file_rel") or it.get("file") or "").replace(
            "\\", "/").strip()
        if not rel: continue
        slug = _role_of(rel)
        if not slug: continue
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
    if not p: return []
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
    if not root: return
    for m in cache:
        d = os.path.join(root, str(m["file"]).replace("/", os.sep))
        a = _author_from_dir(d)
        if not a: a = _AUTHOR_OF.get(m.get("dir", ""), "")
        m["author"] = a
        m["_dir_abs"] = d


def _role_info_all():
    """内置 + 自定义分类，按展示顺序（自定义排内置之后）。"""
    return list(ROLE_INFO) + [(it["slug"], it["cn"]) for it in _load_custom_roles()]


def roles():
    """角色列表（按 _role_info_all 顺序，只保留真的有模型的）。"""
    all_ = items()
    seen = {}
    for m in all_:
        seen.setdefault(m["slug"], 0)
        seen[m["slug"]] += 1
    out = []
    for slug, cn in _role_info_all():
        if slug in seen: out.append({"slug": slug, "cn": cn, "count": seen[slug]})
    # 未登记的角色兜底排在后面
    for slug, n in seen.items():
        if slug not in _ROLE_NAME: out.append({"slug": slug, "cn": slug, "count": n})
    return out


def models_of(slug):
    return [m for m in items() if m["slug"] == slug]


# 官方模型白名单：来自「5306 教学整合版 3.1.1」的 MyGO 模板 figure 清单。
# 用户明确规则：整合包里有的 = 官方原版，没有的 = 改模。
# 白名单落盘在 data/official_models.json（随仓库分发，不依赖本机整合包路径）。
_OFFICIAL_CACHE = None


def _official_whitelist():
    """懒加载白名单 {slug: set(目录名)}；文件缺失返回 {}。"""
    global _OFFICIAL_CACHE
    if _OFFICIAL_CACHE is not None: return _OFFICIAL_CACHE
    out = {}
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "data", "official_models.json")
    try:
        with io.open(p, encoding="utf-8") as f:
            raw = json.load(f)
        for slug, names in (raw or {}).items():
            out[str(slug)] = set(str(n) for n in (names or []))
    except Exception:
        out = {}
    _OFFICIAL_CACHE = out
    return out


def is_official(m):
    """是不是「官方原版」模型。

    规则（用户 2026-11-19 明确）：以「5306 教学整合版 3.1.1」的
    MyGO 模板 figure 清单为准 —— 在清单里的（按 角色 slug + 目录名
    匹配）是官方原版，不在清单里的是改模。

    白名单缺失时回退到结构特征判定（.chara 布局 + 无作者署名），
    保证老装机/独立分发也能大致正确。
    """
    m = m or {}
    wl = _official_whitelist()
    slug = str(m.get("slug") or "")
    d = m.get("_dir_abs") or model_path(m)
    name = os.path.basename(d.rstrip("\\/")) if d else ""
    if wl: return name in wl.get(slug, set())
    # ---- 回退：白名单文件缺失时用结构特征 ----
    if str(m.get("author") or "").strip(): return False
    if not d or not os.path.isdir(d): return False
    if os.path.isdir(os.path.join(d, "live2d")): return False
    p = os.path.join(d, "model.json")
    if not os.path.isfile(p): return False
    try:
        with io.open(p, encoding="utf-8") as f:
            dd = json.load(f)
    except Exception:
        return False
    return str(dd.get("model") or "").startswith(".chara/")


# 官方原版分组的显示名（作者分组里空作者不再叫「未署名」）
OFFICIAL_LABEL = "官方原版"


def authors_of(slug):
    """该角色下的作者分组：[{"author": 作者名, "items": [模型...]}, ...]

    用户要求「父分类用角色名，子分类用改模作者」——角色展开后先按作者
    分组，再列模型。官方原版单独归到「官方原版」一组并排在最前（用户
    反馈过：官方的不能混进作者组里当「未署名」）；真正没署名的改模归到
    「未署名」排最后。作者组之间按模型数降序、名字升序，避免每次重建
    顺序乱跳。
    """
    ms = models_of(slug)
    groups = {}
    for m in ms:
        if is_official(m): key = "__official__"
        else: key = str(m.get("author") or "").strip()
        groups.setdefault(key, []).append(m)
    out = []
    for key, arr in groups.items():
        out.append({"author": key, "items": arr})
    # 官方最前；无署名最后；其余按数量降序、作者名升序
    out.sort(key=lambda g: (0 if g["author"] == "__official__"
                            else (2 if g["author"] == "" else 1),
                            -len(g["items"]),
                            g["author"]))
    return out


def author_label(a):
    """作者分组显示名：官方组、未署名组、真作者名。"""
    a = str(a or "").strip()
    if a == "__official__": return OFFICIAL_LABEL
    return a if a else "未署名"


def all_models():
    return items()


def model_path(m):
    try:
        root = AP.ensure_library(scan=False)
    except Exception:
        root = None
    if not root: return ""
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
    if not d or not os.path.isdir(d): return ""
    # 渲染产物优先
    t = os.path.join(d, "_thumb.png")
    if os.path.isfile(t): return t
    for n in _PREVIEW_NAMES:
        p = os.path.join(d, n)
        if os.path.isfile(p): return p
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
        if os.path.basename(dp).startswith("."): continue
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
            if f.lower().endswith((".png", ".webp", ".jpg")): return os.path.join(ch, f)
    for f in ("texture_00.png", "texture.png"):
        p = os.path.join(d, f)
        if os.path.isfile(p): return p
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
    if not d or not os.path.isdir(d): return False
    if os.path.isfile(os.path.join(d, "_thumb.png")): return False
    for n in _PREVIEW_NAMES:
        if os.path.isfile(os.path.join(d, n)): return False
    # 必须有真实 model.json 才能渲染（空壳目录跳过）
    if not os.path.isfile(os.path.join(d, "model.json")): return False
    return True


def renderable_models():
    """真正可渲染的模型（有 model.json 且缺合成图）。"""
    return [m for m in items() if needs_render(m)]


# ============================ 导入模型（v3.6.3）============================
def _load_index_items():
    """读素材库索引（完整列表）。索引缺失/损坏抛异常，由调用方转成人话。"""
    p = _index_path()
    if not p: raise IOError("素材库索引不存在")
    with io.open(p, encoding="utf-8-sig") as f:
        j = json.load(f)
    if not isinstance(j, list): raise ValueError("索引顶层不是数组")
    return j


def _index_model_dirs(items):
    """索引里已有的 model 相对目录集合（用于目标目录撞名判断）。"""
    out = set()
    for e in items:
        if not isinstance(e, dict) or e.get("kind") != "model": continue
        rel = str(e.get("file_rel") or e.get("file") or "").replace("\\", "/")
        if rel: out.add(rel)
    return out


def _next_model_id(items):
    """接续现有 model_xxxx 编号，避免 id 撞车。"""
    mx = 0
    for e in items:
        m = re.match(r"^model_(\d+)_", str((e or {}).get("id") or ""))
        if m: mx = max(mx, int(m.group(1)))
    return mx + 1


def _parse_meta(model_dir):
    """读 model.json 的 motions/expressions 填进索引 meta。

    复用 webgal_export 的解析（导出和索引必须同一口径，别各写一套）。
    **懒导入**：只在导入动作时跑一次，不把重依赖拉进本模块的加载路径。
    """
    try:
        import webgal_export as WE
        p = os.path.join(model_dir, "model.json")
        if os.path.isfile(p):
            return WE._parse_cubism2_json(p) or {}
    except Exception:
        pass
    return {}


def _append_index_entry(entry):
    """把一条条目拼接进 asset_index.json 末尾。

    **原文拼接而非整份重写**：现有索引 3.5MB，整份重写会因格式化细节差异
    产生巨大且无意义的 diff（实测 indent 猜错就差 10 万字节）。拼接保证
    其余部分逐字节不动，只在末尾多出新条目。

    写前解析校验、写后回读比对，不一致就抛——调用方负责清理已复制的目录。
    """
    p = _index_path()
    if not p: raise IOError("素材库索引不存在")
    text = io.open(p, "rb").read().decode("utf-8")
    prev = json.loads(text)
    if not isinstance(prev, list): raise ValueError("索引顶层不是数组")

    # 与现有条目同风格：json.dumps(indent=2)，整体再缩进 1 格放进数组
    body = json.dumps(entry, ensure_ascii=False, indent=2)
    piece = "\r\n".join(" " + ln for ln in body.split("\n"))

    close = text.rfind("]")
    if close < 0: raise ValueError("索引结尾没有 ]")
    head = text[:close].rstrip()
    if head == "[":
        new_text = "[\r\n" + piece + "\r\n]"
    else:
        new_text = head + ",\r\n" + piece + "\r\n]"

    if json.loads(new_text) != prev + [entry]:
        raise ValueError("索引写回校验失败：回读内容与预期不符")
    with io.open(p, "wb") as f:
        f.write(new_text.encode("utf-8"))


def add_model(src_dir, role_slug, author, costume, thumb_mode="none",
              thumb_src=""):
    """导入一个模型：整份复制进素材库 + 写索引 + 刷新缓存。

    用户要求「导入就直接复制存入木偶内部」——**复制**而非引用，素材库
    自包含：用户把库目录拷走或换机器都能用，不依赖源目录还在不在。

    参数
      src_dir     源模型目录（必须含 model.json，否则不是有效模型）
      role_slug   目标角色分类 slug（内置 ROLE_INFO 或 register_role 建的）
      author      改模人（空串 = 未署名）
      costume     服装名（界面上的「命名」；索引 name 存「角色·服装名」）
      thumb_mode  "none" 不处理 | "pick" 用用户选的图 | "render" 稍后渲染
      thumb_src   thumb_mode=="pick" 时用户选的图片路径

    返回 (ok, msg, entry)。失败 ok=False、msg 是人话、entry=None。
    **不抛异常**——UI 要的是能直接显示的失败信息，不是栈。
    """
    src_dir = os.path.abspath(str(src_dir or ""))
    # ---- 校验源目录 ----
    if not src_dir or not os.path.isdir(src_dir):
        return False, "源目录不存在或不是目录：%s" % src_dir, None
    if not os.path.isfile(os.path.join(src_dir, "model.json")):
        return False, "源目录里没有 model.json，不是有效的模型目录", None

    # ---- 素材库 ----
    try:
        root = AP.ensure_library(scan=False)
    except Exception:
        root = None
    if not root or not os.path.isdir(root):
        return False, "素材库未找到，请先在资产管理里确认素材库位置", None

    slug = str(role_slug or "").strip()
    if not slug: return False, "请选择角色分类", None
    if slug not in _ROLE_NAME: return False, "未知角色分类：%s" % slug, None

    author = str(author or "").strip()
    costume = _slug_of(costume) if str(costume or "").strip() \
        else _slug_of(os.path.basename(src_dir))

    # ---- 目标目录名（撞名加序号）----
    try:
        items = _load_index_items()
    except Exception as e:
        return False, "素材库索引读取失败：%s" % e, None
    taken = _index_model_dirs(items)
    role_dir = os.path.join(root, "figure", slug)
    base = _slug_of(os.path.basename(os.path.normpath(src_dir)))
    name, n = base, 2
    while ("%s/%s" % (slug, name)) in taken or os.path.isdir(
            os.path.join(role_dir, name)):
        name = "%s_%d" % (base, n); n += 1
    rel = "figure/%s/%s" % (slug, name)
    dst = os.path.join(root, rel.replace("/", os.sep))

    # ---- 复制 ----
    try:
        if os.path.exists(dst):
            return False, "目标已存在，未写入：%s" % rel, None
        shutil.copytree(src_dir, dst)
    except Exception as e:
        return False, "复制失败：%s" % e, None

    # ---- 作者署名 ----
    # 写进目录，`_author_from_dir` 自然认到——不用改索引 schema，
    # 也不用在 _enrich 里加一条特判。
    if author:
        try:
            with io.open(os.path.join(dst, "改模作者：%s.txt" % author),
                         "w", encoding="utf-8") as f:
                f.write("改模作者：%s\n（由木偶工作室导入时写入）\n" % author)
        except Exception:
            pass                      # 署名写不进去不影响导入

    # ---- 缩略图：渠道② 用户自己选图 ----
    if thumb_mode == "pick" and thumb_src and os.path.isfile(thumb_src):
        try:
            import model_thumb as MT
            MT.save_image_file(thumb_src, dst)
        except Exception:
            pass                      # 缩略图失败不阻断导入

    # ---- 索引条目 ----
    entry = {
        "id": "model_%04d_%s_%s" % (_next_model_id(items), slug, name),
        "kind": "model",
        "file": rel,
        "file_rel": rel,
        "copied": True,
        "name": "%s·%s" % (_ROLE_NAME.get(slug, slug), costume),
        "tags": [slug] + ([author] if author else []),
        "meta": _parse_meta(dst),
    }

    # ---- 写回索引 ----
    try:
        _append_index_entry(entry)
    except Exception as e:
        with contextlib.suppress(Exception):
            shutil.rmtree(dst)      # 索引没写成就不留半成品目录
        return False, "索引写入失败：%s" % e, None

    refresh(force=True)
    return True, "已导入：%s" % rel, entry


# 启动时把已登记的自定义分类并进角色表（用户上次建的分类不能丢）
_sync_role_tables()
