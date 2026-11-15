# -*- coding: utf-8 -*-
"""WebGAL 场景脚本 -> 视频编辑工程（D1，task-33）。

纯模块 + CLI，无 Qt。把网页端 AI 按《WebGAL 脚本编写规范.md》生成的
WebGAL 场景脚本文本，反向解析成 video_model 的工程 dict（可被视频编辑器
直接载入）。与 webgal_export.py（工程 -> 脚本）互为逆运算。

WebGAL 场景脚本格式（对照官方模板与 webgal_export 的产出）：
    每行一条语句，以分号 `;` 结尾：
        命令行   `命令:参数 -选项;`
        台词行   `名字:内容;`
        注释行   `// ...`
    空行允许。无法与命令区分时按命令白名单判定（见 _COMMANDS），
    含冒号但不在白名单的行一律按台词。

时间游标算法（离散脚本 -> 连续时间轴的核心，与编写规范第 4 节逐字一致）：
    游标 t 从 0 开始，按脚本出现顺序推进：
      - 台词 / intro 行：t += max(2.5, 0.4 * 内容字符数)
      - video: 行：        t += 5.0
      - changeBg：         新背景 clip 起点 = 当前 t；上一背景 clip 终点 = t（BG 轨）
      - changeFigure:file -槽位：
          立绘 clip 起点 = 当前 t；同一槽位遇 changeFigure:none 或脚本结束即终止
          （FIG-n 轨，每个立绘独立一轨，互不干扰）
          -left/-center/-right -> layout x = 0.2 / 0.8 / 0.5，y=0.7，scale=0.6
          -motion=/-expression= -> clip["motion"] / clip["expression"]（值原样，不校验表）
      - bgm:file：           音频轨新段起点 = 当前 t，上一段截到 t
      - playEffect:file：    独立音频 clip，dur = 2
      - 脚本结束：所有未闭合的 clip（背景/立绘/bgm）终点 = 最终 t

资源路径：相对 game_dir 解析成绝对路径；kind 按扩展名判 image/video/audio；
尺寸/时长用 video_source.probe_source 探测（探测失败给 0，绝不抛崩）。

不支持的语句（choose/branch/label/jumpLabel/callScene/changeScene/setVar/
unlockCg/unlockBgm/setTextbox）降级为跳过并记入 warnings，不报错。

产出 proj 必过 video_model.sanitize_project 回环校验（type/width/height/
tracks/dialogue 齐全）。

API：
    import_script(script_path, game_dir, width=1920, height=1080, fps=30)
        -> (ok, msg, proj)
    msg 形如「导入 N 条语句->X 轨 Y clip Z 台词，警告 K」；
    文件不存在 / 全空 -> (False, 原因, None)。

CLI：
    python webgal_import.py --script <场景脚本.txt> --game <WebGAL game 目录>
                            [--out <proj.json>]
    成功 rc=0；失败 rc=1 并打印原因。
"""
import argparse
import json
import os
import re
import sys

# 命令白名单：含冒号行中，head 命中这些才算命令，否则按台词。
_COMMANDS = frozenset((
    "changeBg", "changeFigure", "bgm", "playEffect", "video", "intro",
    "setTextbox", "choose", "branch", "jumpLabel", "label", "callScene",
    "changeScene", "setVar", "unlockCg", "unlockBgm",
    # F2-L3（v3.5）：if 是真实的条件跳转命令（§8-2 实测，jumpIf 不存在）
    "if",
))
# 明确不支持的语句：跳过 + 记警告，不报错。
_UNSUPPORTED = frozenset((
    "branch", "jumpLabel", "label", "callScene",
    "unlockCg", "unlockBgm", "setTextbox",
))
_SLOT_X = {"left": 0.2, "center": 0.5, "right": 0.8}
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")
_VID_EXT = (".mp4", ".webm", ".mov", ".avi", ".mkv")
_AUD_EXT = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac")


def _kind_of(path):
    low = str(path or "").lower()
    if low.endswith(_VID_EXT):
        return "video"
    if low.endswith(_AUD_EXT):
        return "audio"
    return "image"


def _resolve(game_dir, rel):
    """资源路径：绝对路径原样，相对路径按 game_dir 解析成绝对路径。"""
    p = str(rel or "").strip()
    if not p:
        return p
    if os.path.isabs(p):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(str(game_dir or ""), p))


def _probe(path, kind):
    """探测素材尺寸/时长；video_source 不可用或失败 -> 0 兜底，不抛。"""
    try:
        import video_source as VS
        src = VS.probe_source(path)
        if isinstance(src, dict) and src.get("path"):
            src["kind"] = kind
            return src
    except Exception:
        pass
    return {"kind": kind, "path": str(path), "w": 0, "h": 0,
            "fps": 0.0, "src_dur": 0.0}


# ================= 素材库（G2 产物 asset_index.json）消费（G4） =================
# 索引 = task-42 build_asset_library.py 产出的 1418 条素材清单（只读消费，
# 产物本身不属于本模块）。两处消费：
#   1) 导入回退：脚本引用的资源在 game_dir 找不到 -> 按文件名匹配索引，
#      命中直接用素材库绝对路径（bg/bgm/sfx 用 file；model 用 orig 所在目录，
#      交给 webgal_export 的 Cubism2 分支处理）。
#   2) remote_control assets.query：HTTP 线程纯 py 过滤查询。
# 性能：1418 条不每次全量 parse——模块级缓存 (mtime,size) 双判失效；
# 匹配索引（name/file stem 的 exact/ci/sanitized 三档）惰性构建、随缓存同生命周期。
# 线程安全：缓存替换是"构建完整 dict 后单次赋值"（GIL 原子），读侧拿到旧/新
# 快照都自洽；并发最坏是重复构建一次，无锁无阻塞。
# env MUPPET_ASSET_INDEX：覆盖索引路径（指 json 文件或 asset_library 目录都行），
# 供测试与特殊部署。

_LIB_MISSING_MSG = "素材库未构建：先跑 build_asset_library.py"
_LIB_STEM_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_LIB_KEEP_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")

# {"path", "mtime", "size", "items", "maps"}：maps=三档匹配索引，None=未建
_LIB_CACHE = {"path": None, "mtime": -1.0, "size": -1,
              "items": None, "maps": None}


def asset_index_path():
    """asset_index.json 绝对路径。定位统一委托 asset_paths（v3.4.2 起）：
    env 覆盖 > 程序目录/asset_library > 开发机 ../webgal_games/asset_library
    > 注册表手工指定。只给路径，不保证存在。"""
    try:
        import asset_paths as _AP
        return _AP.asset_index_path()
    except Exception:
        # 兜底（asset_paths 缺失也不炸）：保持旧行为
        env = os.environ.get("MUPPET_ASSET_INDEX", "").strip()
        here = os.path.dirname(os.path.abspath(__file__))
        if env:
            p = os.path.normpath(env)
            if os.path.isdir(p):
                p = os.path.join(p, "asset_index.json")
            return p
        return os.path.normpath(os.path.join(
            here, "..", "webgal_games", "asset_library", "asset_index.json"))


def load_asset_index():
    """读索引（带缓存）。返回 (ok, msg, items)；缺失/损坏 ok=False 不抛。
    缓存失效判据 (mtime, size)：任一变化即重读并丢弃匹配索引。"""
    global _LIB_CACHE
    p = asset_index_path()
    try:
        st = os.stat(p)
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        return False, _LIB_MISSING_MSG, None
    c = _LIB_CACHE
    if c["path"] == p and c["mtime"] == mtime and c["size"] == size \
            and c["items"] is not None:
        return True, "", c["items"]
    try:
        with open(p, "r", encoding="utf-8") as f:
            items = json.load(f)
        if not isinstance(items, list):
            raise ValueError("asset_index.json 顶层不是列表")
    except Exception as e:
        return False, "素材索引读取失败: %s: %s" % (type(e).__name__,
                                                   str(e)[:120]), None
    root = os.path.dirname(p)
    _LIB_CACHE = {"path": p, "mtime": mtime, "size": size,
                  "items": items, "maps": None}
    _LIB_CACHE["root"] = root
    return True, "", items


def _lib_root():
    """asset_library 根目录（索引内 file 相对它）。"""
    return _LIB_CACHE.get("root") or os.path.dirname(asset_index_path())


def _lib_sanitize(s):
    """安全化：去扩展名、小写、只留字母数字汉字（空格/-/_/全角标点全去掉）。"""
    s = os.path.basename(str(s or "").replace("\\", "/"))
    s = _LIB_STEM_RE.sub("", s)
    return _LIB_KEEP_RE.sub("", s.lower())


def _lib_maps(items):
    """构建/取用三档匹配索引 {exact:{k:e}, ci:{k:e}, san:{k:e}}。
    键 = 条目 name 与 file/file_rel 的无扩展名 basename；同键先到先得。"""
    c = _LIB_CACHE
    if c.get("maps") is not None:
        return c["maps"]
    exact, ci, san = {}, {}, {}
    for e in items:
        if not isinstance(e, dict):
            continue
        keys = [str(e.get("name") or "")]
        for fld in ("file", "file_rel"):
            v = str(e.get(fld) or "")
            if v:
                keys.append(os.path.splitext(
                    os.path.basename(v.replace("\\", "/")))[0])
        for k in keys:
            k = k.strip()
            if not k:
                continue
            exact.setdefault(k, e)
            ci.setdefault(k.lower(), e)
            s = _lib_sanitize(k)
            if s:
                san.setdefault(s, e)
    maps = {"exact": exact, "ci": ci, "san": san}
    c["maps"] = maps
    return maps


def _lib_lookup(rel):
    """引用路径 -> 素材库条目（**全路径精确** > 精确名 > 大小写 > 安全化）。
    无索引/无命中 None。

    全路径档（第九幕实测 bug 补）：服装名跨角色撞名（casual-2023 有六套），
    剧本合规写 figure/soyo/casual-2023 时 basename 检索会把角色前缀丢掉、
    撞库序第一个（anon）——只有 file/file_rel 全路径比对能消歧。
    特例（G5/5.2 回环）：H1 导出的 changeFigure 写 "figure/角色/服装/model.json"，
    basename stem 恒为 "model"——此时改用**父目录名**（服装名）查，导入自己
    导出的工程时回退才能命中 model 条目。"""
    norm = str(rel or "").replace("\\", "/").rstrip("/")
    ok, _msg, items = load_asset_index()
    if not ok or not items:
        return None
    # ---- 第 0 档：全路径比对 file / file_rel ----
    cand = norm[5:] if norm.lower().startswith("game/") else norm
    if cand.lower().endswith("/model.json"):
        cand = cand[: -len("/model.json")]
    cand_ci = cand.lower()
    for e in items:
        for f in (e.get("file"), e.get("file_rel")):
            if f and str(f).replace("\\", "/").rstrip("/").lower() == cand_ci:
                return e
    key = os.path.basename(norm)
    stem = os.path.splitext(key)[0]
    keys = [stem, key]
    if stem.lower() == "model":
        parent = os.path.basename(os.path.dirname(norm))
        if parent:
            keys = [parent, os.path.splitext(parent)[0]] + keys
    maps = _lib_maps(items)
    for tier in (maps["exact"], maps["ci"], maps["san"]):
        for k in keys:
            if not k:
                continue
            k2 = (k if tier is maps["exact"]
                  else k.lower() if tier is maps["ci"] else _lib_sanitize(k))
            if k2:
                e = tier.get(k2)
                if e is not None:
                    return e
    return None


def _lib_kind(e):
    """条目 kind -> clip source kind。model 条目必须是 **puppet**：
    webgal_export 的 Cubism2 模型分支只在 sk=="puppet" 且 path 是含
    model.json 的目录时触发（G5 e2e 实测：image kind 会掉进 PNG 立绘
    分支，model.json 引用与角色根拷贝都丢）。"""
    k = str((e or {}).get("kind") or "")
    return {"bg": "image", "bgm": "audio", "sfx": "audio",
            "video": "video", "model": "puppet"}.get(k, "")


def _resolve_asset(game_dir, rel):
    """解析引用路径并做素材库回退。返回 (绝对路径, 命中条目或 None)。
    game_dir 下存在 -> 原样；否则按文件名查索引（bg/bgm/sfx/video ->
    <asset_library>/<file>；model -> dirname(orig)）；库路径也不存在 ->
    维持 game_dir 解析结果（现行为，交给下游警告/占位处理）。"""
    p = _resolve(game_dir, rel)
    if p and os.path.exists(p):
        return p, None
    e = _lib_lookup(rel)
    if e is None:
        return p, None
    kind = str(e.get("kind") or "")
    if kind == "model":
        orig = str(e.get("orig") or "")
        cand = os.path.dirname(os.path.normpath(orig)) if orig else ""
    else:
        f = str(e.get("file") or "")
        cand = os.path.normpath(os.path.join(_lib_root(), f.replace("/", os.sep))) \
            if f else ""
    if cand and os.path.exists(cand):
        return cand, e
    return p, None


def _entry_motions(e, n=8):
    m = (e or {}).get("meta") or {}
    return (m.get("motions") or [])[:n]


# ---------------- assets.query（DSH 命令的消费端实现） ----------------
def assets_query(kind=None, tags=None, limit=30):
    """素材库过滤查询（纯 py，HTTP 线程直接调）。返回 (ok, msg, data)：
       kind  精确过滤（bg/bgm/sfx/model/video）；
       tags  **交集语义**：列出的标签必须全部命中（中英均可，大小写不敏感）；
       limit 截断条数（默认 30；<=0 视为不限）；
       data  {"total": 过滤后总数, "items": [{"id","kind","name","tags","meta",
              "file"(或 model 条目的 "file_rel")}]}，meta 精简、model 保留
              motions/expressions 前 8 键名。
    索引缺失/损坏 -> (False, 原因, None)。"""
    ok, msg, items = load_asset_index()
    if not ok:
        return False, msg, None
    kind = str(kind).strip() if kind not in (None, "") else None
    if isinstance(tags, str):
        tags = [tags]
    want_tags = None
    if tags:
        try:
            want_tags = [str(t).strip().lower() for t in tags if str(t).strip()]
        except TypeError:
            return False, "tags 必须是字符串或字符串列表", None
        if not want_tags:
            want_tags = None
    try:
        limit = 30 if limit is None else int(limit)
    except (TypeError, ValueError):
        return False, "limit 必须是整数", None
    out = []
    for e in items:
        if not isinstance(e, dict):
            continue
        if kind is not None and e.get("kind") != kind:
            continue
        if want_tags is not None:
            low = {str(t).lower() for t in (e.get("tags") or [])}
            if not all(t in low for t in want_tags):
                continue
        meta_full = e.get("meta") or {}
        if e.get("kind") == "model":
            meta = {"motions": (meta_full.get("motions") or [])[:8],
                    "expressions": (meta_full.get("expressions") or [])[:8],
                    "textures": meta_full.get("textures")}
            item = {"id": e.get("id"), "kind": e.get("kind"),
                    "name": e.get("name"), "tags": e.get("tags") or [],
                    "file_rel": e.get("file_rel") or "", "meta": meta}
        else:
            meta = {k: meta_full.get(k) for k in ("w", "h", "duration_s")
                    if meta_full.get(k) is not None}
            item = {"id": e.get("id"), "kind": e.get("kind"),
                    "name": e.get("name"), "tags": e.get("tags") or [],
                    "file": e.get("file") or "", "meta": meta}
        out.append(item)
    total = len(out)
    if limit > 0:
        out = out[:limit]
    return True, "", {"total": total, "items": out}


# ---------------- F2-L3（v3.5）：分支语句解析辅助 ----------------
def _split_unescaped(s, sep, maxsplit=-1):
    """按**未转义**的 sep 切分（`\\|` `\\:` `\\,` `\\.` 是转义形态）。

    WebGAL choose 选项文本里这些字符会带反斜杠转义（§8-1 demo_escape），
    切分时必须尊重转义，否则选项会被切碎。"""
    out = []
    cur = []
    i = 0
    n = len(str(s))
    text = str(s)
    done = 0
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            cur.append(ch)
            cur.append(text[i + 1])
            i += 2
            continue
        if ch == sep and (maxsplit < 0 or done < maxsplit):
            out.append("".join(cur))
            cur = []
            done += 1
            i += 1
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


def _unesc_choose(s):
    """choose 选项文本反转义：`\\:`->`:`、`\\,`->`,`、`\\.`->`.`、
    `\\|`->`|`、`\\\\`->`\\`。"""
    out = []
    i = 0
    text = str(s or "")
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] in ":, .|\\".replace(" ", ""):
            out.append(text[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_literal(v):
    """setVar 右值 -> Python 值（数字/布尔/字符串）。

    导出端写的默认值形态：`"stay"`（带引号字符串）/ `0` / `1.5`。
    其它一律按去引号字符串保留（表达式不求值——A4 放弃项）。"""
    s = str(v or "").strip()
    if not s:
        return ""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    low = s.lower()
    if low in ("true", "false"):
        return 1 if low == "true" else 0
    try:
        if "." in s or "e" in low:
            return float(s)
        return int(s)
    except (TypeError, ValueError):
        return s


def _split_line(raw):
    """一行 -> (head, rest, err)。注释/空行/无冒号行分别处理。"""
    line = raw.strip()
    if not line or line.startswith("//"):
        return None, None, "skip"
    if line.endswith(";"):
        line = line[:-1]
    if ":" not in line:
        return None, None, "no-colon"
    head, rest = line.split(":", 1)
    return head.strip(), rest.strip(), None


_EXPORT_HINT_MARKS = ("muppet-skin-v1", "muppet-live2d-core-v1",
                      "muppet-note")


def _comment_text(raw):
    """I3（task-67）注释行识别与内容保留（防 DS 惯性）：
    `//xxx`（旧教学/DS 习惯）与 `;xxx;`（引擎合法形态，H1d 实测语法）
    都算注释，内容进 proj["comments"] 载体，导出端经 _comment() 统一
    转合法 `;xxx;`。返回 None = 不是注释。语句行不会以 ; 开头，无歧义。

    导出器自加的提示注释（含 skin/live2d marker）不回收——它们由导出端
    每次自动前置，收进 comments 会导致往返重复叠加（回环不稳实测根因）。"""
    line = str(raw).strip()
    if not line:
        return None
    if line.startswith("//"):
        text = line[2:].strip() or None
    elif line.startswith(";"):
        text = line.strip(";").strip() or None
    else:
        return None
    if text and any(mk in text for mk in _EXPORT_HINT_MARKS):
        return None
    return text


def _parse_change_figure(arg):
    """changeFigure 参数 -> (path, slot, motion, expression, fid, transform)。

    K1（task-69）平移：-id 句柄（none 按 id 精确退场）与 -transform JSON
    （position 中心原点绝对像素 → 还原 layout.x/y）。-transform 值若含
    空格会被 token 切碎，json 解析失败按无 transform 处理（导出端产出
    紧凑 JSON，正常不会踩到）。"""
    path = slot = None
    motion = expression = None
    fid = None
    transform = None
    toks = str(arg or "").split()
    if not toks:
        return path, slot, motion, expression, fid, transform
    path = toks[0]
    for tk in toks[1:]:
        tk_clean = tk[1:] if tk.startswith("-") else tk
        if tk_clean in _SLOT_X:
            slot = tk_clean
        elif tk.startswith("-motion="):
            motion = tk[len("-motion="):]
        elif tk.startswith("-expression="):
            expression = tk[len("-expression="):]
        elif tk.startswith("-id="):
            fid = tk[len("-id="):]
        elif tk.startswith("-transform="):
            try:
                transform = json.loads(tk[len("-transform="):])
            except Exception:
                transform = None
    return path, slot, motion, expression, fid, transform


def _parse_file(arg):
    """命令参数首 token 即资源路径（其余为选项，这里忽略）。"""
    toks = str(arg or "").split()
    return toks[0] if toks else ""


def _num_ok(v):
    """K1：transform position 数值判定（int/float 可，str 数字亦可）。"""
    try:
        float(v)
        return v is not None
    except (TypeError, ValueError):
        return False


def _who_of(path):
    """K1（task-69）分轨粒度：**角色（who）**而非素材 basename。

    根因案例：figure/soyo/casual-2023/model.json 与
    figure/anon/casual-2023/model.json 的 basename 同为 model.json →
    旧版挤同一条 FIG-model.json 轨，单轨单活跃 clip → 后来者把前人挤下
    时间轴（"一人说话另一人消失"的导入侧根源）。

    规则（边界清单）：
      figure/<ch>/<of>[/model.json]  -> who=<ch>（角色目录）
      figure/<of>[/model.json]       -> who=<of>（单层无角色目录，退化为该目录名）
      figure/xx.png 等带扩展名文件   -> who=文件 stem（静态立绘一名一角色）
      绝对路径/盘符/反斜杠           -> 统一分隔符后同上；"figure" 锚点大小写不敏感
      空/异常输入                    -> "fig"（兜底单轨，不抛）
    """
    p = str(path or "").replace("\\", "/").rstrip("/")
    if not p:
        return "fig"
    segs = [s for s in p.split("/") if s]
    if not segs:
        return "fig"
    last = segs[-1]
    if last.lower() == "model.json" and len(segs) >= 2:
        segs = segs[:-1]
        last = segs[-1]
    if "." in last:                       # 文件（png/webp/…）-> stem
        return os.path.splitext(last)[0] or "fig"
    low = [s.lower() for s in segs]
    if "figure" in low:
        fi = len(low) - 1 - low[::-1].index("figure")
        rest = segs[fi + 1:]
        if rest:
            return rest[0]                # <ch>（双层）或 <of>（单层退化）
    # K1：无 figure 锚点的裸角色引用（导出回环的 "soyo/casual-2023"
    # 形态）——两级以上取倒数第二级为角色，单级取自身。
    if len(segs) >= 2:
        return segs[-2]
    return last or "fig"


class _Builder:
    """按时间游标把语句增量折叠进一个工程 dict。"""

    def __init__(self, width, height, fps):
        try:
            import video_model as VM
        except Exception as e:
            raise ImportError("video_model 不可用: %s" % e)
        self.VM = VM
        self.proj = {"type": VM.PROJECT_TYPE, "version": 1,
                     "width": int(width), "height": int(height),
                     "fps": float(fps), "tracks": [], "dialogue": []}
        self.t = 0.0
        self.warnings = []
        self.stmt_n = 0
        self.lib_hits = 0        # G4：素材库回退命中计数（记 info 不进警告）
        self.comments = []       # I3（task-67）：// 与 ;…; 注释行内容载体
        # K1（task-69）：-id 句柄 -> who 映射（none -id 精确退场用）
        self._id_fig = {}
        # 轨道登记：name -> tid
        self._tid = {}
        # 未闭合 clip：track_id -> clip
        self._open = {}
        # 槽位 -> 当前立绘名
        self._slot_fig = {"left": None, "center": None, "right": None}
        # 立绘名 -> (track_id, clip)
        self._fig = {}
        # ---- F2-L3（v3.5）：分支折叠载体 ----
        self._choices = []       # [{cid,t,prompt,options}] -> 幕 choices
        self._jumps = []         # [{t,when,target}]      -> 幕 jumps
        self._vars = []          # [{name,default}]       -> 工程 vars
        self._next = None        # 幕尾续接（changeScene）
        self._branch_end = False  # 本文件出现分叉语句：后续语句仍在同幕积累
        self._pending_sets = []  # 伪幕里的 setVar（供 option.set 吸收）
        self._scene_name = None  # 本文件对应 scene 名（多文件导入时设）
        self._aid = "a1"

    # ---- 轨道 ----
    def _track(self, name, kind="video"):
        tid = self._tid.get(name)
        if tid is None:
            ok, msg, tid = self.VM.add_track(self.proj, kind, name)
            if not ok:
                self.warnings.append("建轨失败 %s: %s" % (name, msg))
                tid = None
            else:
                self._tid[name] = tid
        return tid

    def _close(self, tid):
        c = self._open.pop(tid, None)
        if c is not None and c.get("dur") is not None:
            c["dur"] = round(max(0.0, self.t - c["start"]), 3)
            if c["dur"] < self.VM.MIN_DUR - self.VM.EPS:
                c["dur"] = max(self.VM.MIN_DUR, c["dur"])

    def _new_clip(self, tid, src, start):
        clip = self.VM.new_clip(src, start, self.VM.MIN_DUR)
        ok, cid = self.VM.add_clip(self.proj, tid, clip)
        if not ok:
            self.warnings.append("上轨失败 %s: %s" % (tid, cid))
            return None
        return self.VM.find_clip(self.proj, cid)[1]

    # ---- 资源定位（game_dir 优先，缺失回退素材库，G4） ----
    def _locate(self, game_dir, rel):
        """引用 -> (绝对路径, source kind)。game_dir 有文件按原样；命中素材库
        则用库路径且 kind 按条目（model 条目 -> orig 目录，交给导出端 Cubism2
        分支）。双 miss 返回 game_dir 解析结果（维持现行为）。"""
        k = _kind_of(rel)
        p, e = _resolve_asset(game_dir, rel)
        if e is not None:
            self.lib_hits += 1
            k = _lib_kind(e) or k
        return p, k

    # ---- 命令处理 ----
    def do_change_bg(self, game_dir, arg):
        path, k = self._locate(game_dir, _parse_file(arg))
        bt = self._track("BG", "video")
        if bt is None:
            return
        self._close(bt)
        src = _probe(path, k)
        clip = self._new_clip(bt, src, self.t)
        if clip is not None:
            clip["layout"] = dict(self.VM.default_layout())
            self._open[bt] = clip

    def do_change_figure(self, game_dir, arg):
        path, slot, motion, expression, fid, transform = \
            _parse_change_figure(arg)
        if not path:
            return
        if not str(path).lower().endswith(("none",)):
            # K1（task-69）：按角色（who）分轨——同角色换装/更新关旧开新，
            # 不同角色各占一轨，绝不再互相挤掉（旧版按 basename 分轨，
            # model.json 撞名全员挤一轨 = 互藏 bug 导入侧根因）。
            who = _who_of(path)
            ft = self._track("FIG-%s" % who, "video")
            if ft is None:
                return
            self._close(ft)
            self._fig.pop(who, None)
            p2, k2 = self._locate(game_dir, path)
            # K1（task-69）：导出回环的裸角色引用（soyo/casual-2023/
            # model.json，无 figure/ 前缀——H1 导出行形态）在 game 根找不到
            # 时，试 figure/ 变体定位。
            if not os.path.exists(p2) and not str(path).replace(
                    "\\", "/").startswith("figure/"):
                alt = _resolve(game_dir, "figure/" + str(path))
                if os.path.exists(alt):
                    p2 = alt
            # K1（task-69）：model.json 引用 = Cubism2 模型（整合包/H1 形态）
            # ——升级为 puppet 并指向含 model.json 的目录，导出走 Cubism2
            # 分支（角色目录引用天然稳定，杜绝 model.json 裸名撞车丢角色）。
            if str(p2).lower().endswith("model.json"):
                k2 = "puppet"
                p2 = os.path.dirname(os.path.normpath(str(p2)))
            elif k2 == "image" and os.path.isdir(p2) and \
                    os.path.isfile(os.path.join(p2, "model.json")):
                k2 = "puppet"
            src = _probe(p2, k2)
            clip = self._new_clip(ft, src, self.t)
            if clip is None:
                return
            lay = dict(self.VM.default_layout())
            lay["x"] = _SLOT_X.get(slot, 0.5) if slot else 0.5
            lay["y"] = 0.7
            lay["scale"] = 0.6
            # K1 平移反向：-transform position（中心原点绝对像素）→ x/y。
            # 画布维度优先取导出器侧键 muppet{w,h}（回环精确还原），
            # 无侧键退回本工程画布。
            pos = ((transform or {}).get("position") or {})
            if isinstance(pos, dict):
                _mu = (transform or {}).get("muppet") or {}
                _W = _num_ok(_mu.get("w")) and float(_mu["w"]) \
                    or float(self.proj["width"] or 1920)
                _H = _num_ok(_mu.get("h")) and float(_mu["h"]) \
                    or float(self.proj["height"] or 1080)
                if _num_ok(pos.get("x")):
                    lay["x"] = max(0.0, min(1.0,
                                            0.5 + float(pos["x"]) / _W))
                if _num_ok(pos.get("y")):
                    lay["y"] = max(0.0, min(1.0,
                                            0.5 - float(pos["y"]) / _H))
            clip["layout"] = lay
            if motion is not None:
                clip["motion"] = motion
            if expression is not None:
                clip["expression"] = expression
            self._open[ft] = clip
            self._fig[who] = (ft, clip)
            if fid:
                self._id_fig[fid] = who
            if slot in self._slot_fig:
                self._slot_fig[slot] = who
        else:
            # changeFigure:none -槽位 / -id=xxx：显式退场（K1 两种句柄形态）
            who = self._id_fig.get(fid) if fid else None
            if who is None:
                who = self._slot_fig.get(slot)
            if who and who in self._fig:
                ft, _c = self._fig.pop(who)
                self._close(ft)
                if slot in self._slot_fig:
                    self._slot_fig[slot] = None
                for k in [k for k, v in self._id_fig.items() if v == who]:
                    self._id_fig.pop(k, None)

    def do_video(self, game_dir, arg):
        path, k = self._locate(game_dir, _parse_file(arg))
        vt = self._track("VID", "video")
        if vt is None:
            return
        src = _probe(path, k)
        clip = self._new_clip(vt, src, self.t)
        if clip is not None:
            clip["dur"] = 5.0
        self.t += 5.0

    def do_bgm(self, game_dir, arg):
        path, k = self._locate(game_dir, _parse_file(arg))
        at = self._track("BGM", "audio")
        if at is None:
            return
        self._close(at)
        src = _probe(path, k)
        clip = self._new_clip(at, src, self.t)
        if clip is not None:
            self._open[at] = clip

    def do_play_effect(self, game_dir, arg):
        path, k = self._locate(game_dir, _parse_file(arg))
        at = self._track("SFX", "audio")
        if at is None:
            return
        src = _probe(path, k)
        clip = self._new_clip(at, src, self.t)
        if clip is not None:
            clip["dur"] = 2.0

    def do_intro(self, _game_dir, arg):
        text = str(arg or "").strip()
        if not text:
            return
        dur = max(2.5, 0.4 * len(text))
        self.proj["dialogue"].append(
            {"t": round(self.t, 3), "dur": round(dur, 3),
             "who": "旁白", "text": text})
        self.t += dur

    # ---- F2-L3（v3.5）：分支语句落地 ----
    # 设计 docs/F2分支剧情设计.md §4。全部**折叠回 v2 工程**（幕列表 + choices
    # + jumps + vars），不引入新形状。单文件导入（import_script）行为不变：
    # 没有 choose/setVar/if 时，下面这些方法根本不会被调用。
    def do_choose(self, game_dir, arg):
        """`choose:文本1:scene1.txt|文本2:scene2.txt;` -> choices 节点。

        目标带 .txt = 跨文件跳转（映射成幕 aid 待 BFS 后回填）；不带 = 同文件
        label（A4 放弃项，记警告但保留原名）。选项文本反转义（\\: \\, \\.）。"""
        self.stmt_n = self.stmt_n          # 已计数
        opts = []
        # 按未转义的 | 切分
        parts = _split_unescaped(str(arg or ""), "|")
        for p in parts:
            p = p.strip()
            if not p:
                continue
            # 按未转义的 : 切成 文本:目标（只切第一个）
            seg = _split_unescaped(p, ":", maxsplit=1)
            if len(seg) < 2:
                self.warnings.append("choose 选项格式非法（缺目标）: %s" % p[:40])
                continue
            txt = _unesc_choose(seg[0].strip())
            tgt = seg[1].strip()
            if txt:
                opts.append({"text": txt, "target": tgt})
        if not opts:
            self.warnings.append("choose 无可解析选项，已跳过")
            return
        cid = "c%d" % (len(self._choices) + 1)
        self._choices.append({"cid": cid, "t": None, "prompt": "",
                              "options": opts})
        # choose 之后剧情在此分叉：本幕在这一刻收尾
        self._branch_end = True

    def do_choose_prompt(self, text):
        """choose 前一行旁白若是提示语，挂到最近一个选择点上。"""
        if not self._choices:
            return False
        t = str(text or "").strip()
        if not t:
            return False
        self._choices[-1]["prompt"] = t
        return True

    def do_set_var(self, _game_dir, arg):
        """`setVar:名=表达式;` -> vars 声明（工程级，只增不覆盖已有默认）。"""
        s = str(arg or "").strip()
        if "=" not in s:
            self.warnings.append("setVar 格式非法（缺 =）: %s" % s[:40])
            return
        name, val = s.split("=", 1)
        name = name.strip()
        val = val.strip()
        if not name:
            return
        # -global 选项：剥掉（我们统一工程级，无作用域概念）
        for flag in ("-global", "-local"):
            if val.endswith(flag):
                val = val[:-len(flag)].strip()
        for v in self._vars:
            if v["name"] == name:
                return                     # 已声明：不覆盖（首声明=默认值）
        self._vars.append({"name": name, "default": _parse_literal(val)})
        # 伪幕识别：本文件若只含 setVar + changeScene，则这些 setVar 属于
        # 某个 option.set 副作用 -> 由 _finalize 吸收回 option.set
        self._pending_sets.append("%s=%s" % (name, val))

    def do_if(self, game_dir, arg):
        """`if:表达式:scene.txt;` -> jumps 节点（单条件，实测语法）。"""
        s = str(arg or "").strip()
        seg = _split_unescaped(s, ":", maxsplit=1)
        if len(seg) < 2:
            self.warnings.append("if 格式非法（缺目标）: %s" % s[:40])
            return
        cond = seg[0].strip()
        tgt = seg[1].strip()
        if not cond or not tgt:
            return
        self._jumps.append({"t": round(self.t, 3), "when": cond, "target": tgt})
        self._branch_end = True

    def do_change_scene(self, _game_dir, arg):
        """`changeScene:x.txt;` -> 幕尾 next（导出反向：next -> changeScene）。"""
        tgt = str(arg or "").strip()
        if tgt:
            self._next = tgt
            self._branch_end = True

    # ---- 台词 ----
    def do_dialogue(self, who, text):
        dur = max(2.5, 0.4 * len(text))
        self.proj["dialogue"].append(
            {"t": round(self.t, 3), "dur": round(dur, 3),
             "who": who or "旁白", "text": text})
        self.t += dur

    # ---- 主循环 ----
    def run(self, script_path, game_dir):
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return False, "脚本文件不存在: %s" % script_path, None
        except OSError as e:
            return False, "脚本读取失败: %r" % e, None

        for raw in lines:
            ct = _comment_text(raw)
            if ct is not None:
                # I3：注释不再丢弃，进工程级载体（导出端转合法 ;xxx;）
                self.comments.append({"t": round(self.t, 3), "text": ct})
                continue
            head, rest, err = _split_line(raw)
            if err:
                continue
            if head is None:
                continue
            self.stmt_n += 1
            if head in _COMMANDS:
                fn = {"changeBg": self.do_change_bg,
                      "changeFigure": self.do_change_figure,
                      "bgm": self.do_bgm,
                      "playEffect": self.do_play_effect,
                      "video": self.do_video,
                      "intro": self.do_intro,
                      # F2-L3（v3.5）：分支语句落地
                      "choose": self.do_choose,
                      "setVar": self.do_set_var,
                      "if": self.do_if,
                      "changeScene": self.do_change_scene}.get(head)
                if fn is None:
                    if head in _UNSUPPORTED:
                        self.warnings.append("跳过不支持的语句: %s" % head)
                    else:
                        self.warnings.append("跳过无法识别的命令: %s" % head)
                    continue
                try:
                    fn(game_dir, rest)
                except Exception as e:
                    self.warnings.append("语句处理异常 %s: %r" % (head, e))
            else:
                # 台词行
                self.do_dialogue(head, rest)

        if self.stmt_n == 0:
            return False, "脚本为空或无可识别语句: %s" % script_path, None

        # 脚本结束：闭合所有未结束的 clip
        for tid in list(self._open):
            self._close(tid)

        # I3（task-67）：注释载体落工程（sanitize 白名单保留，导出转 ;xxx;）
        if self.comments:
            self.proj["comments"] = list(self.comments)

        # K1（task-69）：同屏人数上限撤销——>3 扫描/警告整体移除。

        # 回环校验
        ok, msg = self._finalize()
        if not ok:
            return False, msg, None
        # F2-L3：v2 多幕工程里 tracks/dialogue 在 acts[0] 内（顶层不再有），
        # 统计必须走 act_view 投影，否则 KeyError（旧断言炸过）。
        view = self.VM.act_view(self.proj)
        n_track = len(view.get("tracks") or [])
        n_clip = sum(len(t.get("clips") or []) for t in view.get("tracks") or [])
        n_dlg = len(view.get("dialogue") or [])
        n_act = len(self.proj.get("acts") or []) or 1
        full = "导入 %d 条语句->%d 轨 %d clip %d 台词，警告 %d" % (
            self.stmt_n, n_track, n_clip, n_dlg, len(self.warnings))
        if n_act > 1:
            full = full.replace("，警告", "（%d 幕），警告" % n_act) \
                if "（" not in full else full
            full = "导入 %d 条语句->%d 幕 %d 轨 %d clip %d 台词，警告 %d" % (
                self.stmt_n, n_act, n_track, n_clip, n_dlg, len(self.warnings))
        if self.lib_hits:
            # G4 信息计数（不是警告）：素材库回退让脚本引用的缺失资源落了地
            full += "；素材库回退命中 %d" % self.lib_hits
        if self.warnings:
            full += "：" + "；".join(self.warnings)
        return True, full, self.proj

    def _finalize(self):
        # ---- F2-L3：分支载体折叠进工程（v2 多幕形状）----
        # 本文件只要有 choices/jumps/next/vars 任一，就升级成 v2：
        #   - 本文件的 tracks/dialogue 收进 acts[0]（video_model 懒升级）
        #   - choices/jumps/next 挂到该幕；vars 挂工程级
        if self._choices or self._jumps or self._vars or self._next:
            try:
                self._fold_branch()
            except Exception as e:
                self.warnings.append("分支折叠失败（退化为单幕）: %r" % e)
        try:
            import video_model as VM
            proj = VM.sanitize_project(self.proj)
        except Exception as e:
            return False, "video_model 校验失败: %r" % e
        if proj is None:
            return False, "产出工程不合法（sanitize_project 返回 None）"
        self.proj = proj
        return True, ""

    def _fold_branch(self):
        """把 choices/jumps/next/vars 折叠成 v2 多幕工程（原地改 self.proj）。"""
        VM = self.VM
        # 目标是 scene 文件名 -> 幕 aid 的映射（多文件导入时由调用方填）
        s2a = getattr(self, "_scene_to_aid", None) or {}

        def _map_target(t):
            """scene 文件名 -> 幕 aid；映射不到就保留原名（往返不丢）。"""
            t = str(t or "").strip()
            if not t:
                return ""
            if t in s2a:
                return s2a[t]
            for k, v in s2a.items():
                if os.path.basename(k).lower() == os.path.basename(t).lower():
                    return v
            return t

        # 升级成 v2（把顶层 tracks/dialogue 收进 acts[0]）
        VM._upgrade_to_v2(self.proj)
        act = self.proj["acts"][0]
        if self._scene_name:
            act["name"] = os.path.splitext(os.path.basename(
                self._scene_name))[0]
            act["aid"] = getattr(self, "_aid", "a1")
        # 伪幕吸收：本文件若只含 setVar(+changeScene)，说明是被 choose 指来的
        # 副作用片段 -> 吸收成对应 option.set，不生成为幕（往返闭环关键）
        if self._pending_sets and getattr(self, "_absorb", False):
            self._absorbed_set = ";".join(self._pending_sets)
        for ch in self._choices:
            clean_opts = []
            for o in ch.get("options") or []:
                tgt = str(o.get("target") or "").strip()
                setexpr = o.get("set")
                # 伪幕吸收（导出端 option.set -> 伪幕文件；导入端反向折叠）：
                # 选项目标是伪幕（只含 setVar+changeScene 的小文件）时，
                # 把它的 setVar 并成 option.set，目标改指真 scene、
                pm = getattr(self, "_pseudo_map", None) or {}
                key = os.path.basename(tgt.replace("\\", "/")).lower()
                if key in pm:
                    psets, preal = pm[key]
                    if psets:
                        setexpr = ";".join(psets)
                    tgt = preal or tgt
                e = {"text": o.get("text"), "target": _map_target(tgt)}
                if setexpr:
                    e["set"] = setexpr
                if o.get("when"):
                    e["when"] = o["when"]
                clean_opts.append(e)
            act.setdefault("choices", []).append(
                {"cid": ch.get("cid"), "t": ch.get("t"),
                 "prompt": ch.get("prompt") or "", "options": clean_opts})
        for j in self._jumps:
            act.setdefault("jumps", []).append(
                {"t": j.get("t"), "when": j.get("when"),
                 "target": _map_target(j.get("target"))})
        if self._next:
            act["next"] = _map_target(self._next)
        if self._vars:
            existing = self.proj.get("vars") or []
            names = {v.get("name") for v in existing if isinstance(v, dict)}
            for v in self._vars:
                if v.get("name") not in names:
                    existing.append(v)
                    names.add(v.get("name"))
            self.proj["vars"] = existing


def import_script(script_path, game_dir, width=1920, height=1080, fps=30):
    """WebGAL 场景脚本 -> 视频工程 dict。返回 (ok, msg, proj)。

    单文件入口，行为与旧版完全一致（无 choose/setVar/if 时不升级 v2）。
    多文件（多幕）请用 import_project_dir。"""
    if not script_path or not os.path.isfile(str(script_path)):
        return False, "脚本文件不存在: %s" % script_path, None
    try:
        b = _Builder(width, height, fps)
    except Exception as e:
        return False, "构建器初始化失败: %r" % e, None
    return b.run(str(script_path), game_dir)


# ================= F2-L3：多文件（多幕）导入 =================
def _scan_branch_targets(script_path):
    """扫一个 scene 文件里所有跳转目标（choose 选项 / if 目标 / changeScene）。

    返回 (targets:set[str], sets:[str], has_only_set_and_scene:bool)。
    纯文本扫，不建工程——BFS 收集文件用。"""
    targets = set()
    sets = []
    other_stmts = 0
    try:
        lines = open(script_path, encoding="utf-8", errors="ignore").readlines()
    except OSError:
        return targets, sets, False
    for raw in lines:
        if _comment_text(raw) is not None:
            continue
        head, rest, err = _split_line(raw)
        if err or head is None:
            continue
        if head == "choose":
            for p in _split_unescaped(str(rest or ""), "|"):
                seg = _split_unescaped(p.strip(), ":", maxsplit=1)
                if len(seg) >= 2:
                    t = seg[1].strip()
                    if t and t.lower().endswith(".txt"):
                        targets.add(t)
            other_stmts += 1
        elif head == "if":
            seg = _split_unescaped(str(rest or ""), ":", maxsplit=1)
            if len(seg) >= 2:
                t = seg[1].strip()
                if t and t.lower().endswith(".txt"):
                    targets.add(t)
            other_stmts += 1
        elif head == "changeScene":
            t = str(rest or "").strip()
            if t:
                targets.add(t)
            # changeScene 不算"其它内容"（伪幕识别用）
        elif head == "setVar":
            sets.append(str(rest or "").strip())
        elif head in ("intro", "setTextbox"):
            other_stmts += 1
        else:
            other_stmts += 1
    only_set_scene = (other_stmts == 0 and bool(sets))
    return targets, sets, only_set_scene


def import_project_dir(game_dir, entry="game/scene/start.txt",
                       width=1920, height=1080, fps=30):
    """F2-L3：WebGAL 工程目录（多 scene）-> v2 多幕工程。返回 (ok, msg, proj)。

    流程：
      1. 从入口 scene 文件 BFS 收集所有被引用 scene（visited 防环）
      2. 每个文件建一个 _Builder（= 一幕），记录 scene -> aid 映射
      3. 伪幕识别：只含 setVar(+changeScene) 且无其它语句的文件，且被某个
         choose 指向 -> 不生成为幕，setVar 吸收回对应 option.set
      4. 合并所有幕成 v2 工程（vars 并集、choice 目标映射成 aid）

    单文件工程（无跳转）等价于 import_script，行为零变化。"""
    game_dir = str(game_dir or "").rstrip("\\/")
    entry_path = entry if os.path.isabs(entry) else os.path.join(
        game_dir, entry.replace("/", os.sep))
    if not os.path.isfile(entry_path):
        alt = os.path.join(game_dir, "scene", os.path.basename(entry_path))
        if os.path.isfile(alt):
            entry_path = alt
        else:
            return False, "入口脚本不存在: %s" % entry_path, None

    scene_root = os.path.dirname(entry_path)

    def _resolve_target(t):
        base = os.path.basename(str(t or "").replace("\\", "/"))
        p = os.path.join(scene_root, base)
        return p if os.path.isfile(p) else None

    # ---- 1. BFS 收集 ----
    order = []
    visited = set()
    queue = [entry_path]
    pending = {}
    while queue:
        p = queue.pop(0)
        key = os.path.normcase(os.path.abspath(p))
        if key in visited:
            continue
        visited.add(key)
        order.append(p)
        tgts, sets, only_sets = _scan_branch_targets(p)
        pending[p] = {"targets": tgts, "sets": sets, "only_sets": only_sets}
        for t in tgts:
            rp = _resolve_target(t)
            if rp:
                queue.append(rp)

    # ---- 2. 伪幕识别 ----
    # 被 choose 指向 + 只含 setVar/changeScene -> 伪幕（吸收成 option.set）
    pointed = set()
    for p, info in pending.items():
        for t in info["targets"]:
            rp = _resolve_target(t)
            if rp:
                pointed.add(os.path.normcase(os.path.abspath(rp)))
    pseudo = {p for p in order
              if pending[p]["only_sets"]
              and os.path.normcase(os.path.abspath(p)) in pointed}

    real_scenes = [p for p in order if p not in pseudo]
    if not real_scenes:
        return False, "没有任何可导入的幕文件", None

    # ---- 3. 每文件一幕 ----
    scene_to_aid = {}
    for i, p in enumerate(real_scenes):
        aid = "a%d" % (i + 1)
        scene_to_aid[os.path.basename(p)] = aid
        scene_to_aid[p] = aid

    # 伪幕 -> 它是哪个选项的目标：文件名 a1__c1_0.txt 可解析，兜底靠内容匹配
    def _pseudo_owner(p):
        """伪幕归属 (目标 scene 里的 option)：返回 (target_scene_path, sets)。"""
        info = pending.get(p) or {}
        return info.get("sets") or []

    # 伪幕吸收表：{伪幕 basename(小写): (sets列表, 真目标scene名)}
    # **必须在 builder.run 之前建好**——run 内部 _finalize -> _fold_branch 会
    # 立刻把 choices 搬进 acts，之后再改 builder._choices 就没用了（实测 bug）。
    pseudo_map = {}
    for pp in pseudo:
        info = pending.get(pp) or {}
        real = None
        try:
            for raw in open(pp, encoding="utf-8", errors="ignore").readlines():
                h2, r2, e2 = _split_line(raw)
                if e2 or h2 is None:
                    continue
                if h2 == "changeScene":
                    real = str(r2 or "").strip()
                    break
        except OSError:
            pass
        pseudo_map[os.path.basename(pp).lower()] = (info.get("sets") or [], real)

    builders = []
    all_warn = []
    for i, p in enumerate(real_scenes):
        aid = "a%d" % (i + 1)
        try:
            b = _Builder(width, height, fps)
        except Exception as e:
            return False, "构建器初始化失败: %r" % e, None
        b._scene_name = os.path.basename(p)
        b._aid = aid
        b._scene_to_aid = scene_to_aid
        b._pseudo_map = pseudo_map          # 供 _fold_branch 吸收用
        b.run(p, game_dir)
        builders.append(b)
        all_warn.extend(b.warnings)

    if not builders:
        return False, "没有成功导入任何幕", None

    # ---- 4. 合并成 v2 ----
    VM = None
    try:
        import video_model as VM
    except Exception as e:
        return False, "video_model 不可用: %s" % e, None

    base = builders[0]
    proj = base.proj
    VM._upgrade_to_v2(proj)
    # 第一幕的 aid/名字已经是 a1；其余幕追加
    acts = proj["acts"]
    if acts:
        acts[0]["aid"] = "a1"
        acts[0]["name"] = os.path.splitext(base._scene_name)[0]
    for b in builders[1:]:
        p2 = b.proj
        VM._upgrade_to_v2(p2)
        a = p2["acts"][0]
        a["aid"] = b._aid
        a["name"] = os.path.splitext(b._scene_name)[0]
        acts.append(a)
        # vars 并集
        for v in (p2.get("vars") or []):
            names = {x.get("name") for x in (proj.get("vars") or [])
                     if isinstance(x, dict)}
            if isinstance(v, dict) and v.get("name") not in names:
                proj.setdefault("vars", []).append(v)
    # 二次映射：把目标里的 scene 文件名换成 aid（各 builder 已做过一次，
    # 这里补一次防止跨文件引用在 builder 阶段还没拿到完整映射）
    for a in acts:
        for ch in (a.get("choices") or []):
            for o in (ch.get("options") or []):
                t = str(o.get("target") or "")
                if t in scene_to_aid:
                    o["target"] = scene_to_aid[t]
                elif os.path.basename(t) in scene_to_aid:
                    o["target"] = scene_to_aid[os.path.basename(t)]
        for j in (a.get("jumps") or []):
            t = str(j.get("target") or "")
            if t in scene_to_aid:
                j["target"] = scene_to_aid[t]
            elif os.path.basename(t) in scene_to_aid:
                j["target"] = scene_to_aid[os.path.basename(t)]
        nx = a.get("next")
        if nx:
            if nx in scene_to_aid:
                a["next"] = scene_to_aid[nx]
            elif os.path.basename(str(nx)) in scene_to_aid:
                a["next"] = scene_to_aid[os.path.basename(str(nx))]

    ok, msg = True, ""
    try:
        fixed = VM.sanitize_project(proj)
        if fixed is None:
            ok, msg = False, "合并后工程不合法"
        else:
            proj = fixed
    except Exception as e:
        ok, msg = False, "合并校验失败: %r" % e

    n_act = len(proj.get("acts") or [])
    n_pseudo = len(pseudo)
    return ok, "导入 %d 幕%s，警告 %d" % (
        n_act, ("（吸收 %d 个伪幕）" % n_pseudo) if n_pseudo else "",
        len(all_warn)), proj


def _absorb_pseudo(builder, scene_path, pending, resolve_target, pseudo_set):
    """把某幕 choose 选项指向的伪幕吸收回 option.set（原地改 builder 的 choices）。

    伪幕形如 a1__c1_0.txt，内容 = setVar* + changeScene:<真目标>。
    吸收后：该选项 set = "k=v;k2=v2"，target = 真目标（不再是伪幕）。"""
    resolve = resolve_target
    for ch in builder._choices:
        for o in (ch.get("options") or []):
            tgt = str(o.get("target") or "").strip()
            if not tgt:
                continue
            rp = resolve(tgt)
            if not rp or rp not in pseudo_set:
                continue
            info = pending.get(rp) or {}
            sets = info.get("sets") or []
            if sets:
                o["set"] = ";".join(sets)
            # 真目标：伪幕里的 changeScene
            real = None
            try:
                for raw in open(rp, encoding="utf-8",
                                errors="ignore").readlines():
                    head, rest, err = _split_line(raw)
                    if err or head is None:
                        continue
                    if head == "changeScene":
                        real = str(rest or "").strip()
                        break
            except OSError:
                pass
            o["target"] = real or ""


# ================= CLI =================
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="WebGAL 场景脚本 -> 视频编辑工程（反向导入）")
    ap.add_argument("--script", required=True, help="WebGAL 场景脚本 .txt 路径")
    ap.add_argument("--game", required=True, help="WebGAL game 目录（资源根）")
    ap.add_argument("--out", default=None, help="可选：导出 proj.json")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=float, default=30)
    a = ap.parse_args(argv)
    ok, msg, proj = import_script(a.script, a.game, a.width, a.height, a.fps)
    if not ok:
        print("导入失败: %s" % msg)
        return 1
    for w in proj.get("dialogue", []):
        pass
    if a.out:
        try:
            with open(a.out, "w", encoding="utf-8") as f:
                json.dump(proj, f, ensure_ascii=False, indent=1)
            print("已写出工程: %s" % a.out)
        except OSError as e:
            print("写出工程失败: %r" % e)
            return 1
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
