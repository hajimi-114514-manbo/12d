# -*- coding: utf-8 -*-
"""K2（task-73）素材体检：导入时/导出前把"缺什么"说清楚。

背景（用户实拍暴露）：脚本里 `changeBg:选择背景图片` 这类占位符/缺失素材，
过去要到**导出时**才在警告里冒出来（导出层 v3.5 已改为"缺素材不 emit
坏引用"），玩家拿到包才发现背景不对/人物不显示。本模块把问题暴露在
最早期：导入 msg 尾部摘要、DSH `assets.check` 命令、导出对话框预检
（预检接线在 workbench 侧，复用本模块，不写两套）。

口径与导出层严格一致（webgal_export.copy_in 的失败分支）：
  clip.source.path 解析到磁盘不存在 = 缺失。
本模块**只读**：不改 proj、不写盘、任何内部异常都吞成"无缺失"（体检
绝不比被检对象更脆弱）。

两类缺失：
  placeholder —— 名字像脚本作者留下的 UI 占位文本（"选择背景图片"、
      "请选择立绘"、"占位"、"placeholder"、"TODO"、"XXX"，或无扩展名
      的纯中文短语且素材库无同名）-> 导出会跳过，需要换成真素材；
  real —— 名字像正常素材（有扩展名/模型目录形态）但本地与素材库都
      找不到 -> 给素材库模糊候选（2-3 个），可能要下载/补录素材。
"""
import difflib
import os
import re
import contextlib

# 占位符特征词（中文 UI 话术为主——DS 生成脚本常见残留）
_PLACEHOLDER_WORDS = ("选择", "请输入", "请替换", "占位", "待定", "暂无",
                      "placeholder", "todo", "tbd", "xxx", "待补")
# 占位符判定里允许伴随的类别词（"选择背景图片" = 选择 + 背景图片）
_KIND_WORDS = ("背景", "立绘", "人物", "角色", "图片", "音乐", "音频",
               "音效", "视频", "文件", "素材", "bg", "figure", "image")

_EXT_KIND = {".png": "背景/立绘", ".jpg": "背景/立绘", ".jpeg": "背景/立绘",
             ".webp": "背景/立绘", ".gif": "背景/立绘",
             ".mp3": "音乐", ".ogg": "音乐", ".wav": "音效",
             ".mp4": "视频", ".webm": "视频"}


def _lazy_import_api(name):
    """惰性取 webgal_import 的库函数（单一素材库口径，不另造遍历）。"""
    try:
        import webgal_import as WI
        return getattr(WI, name, None)
    except Exception:
        return None


def lib_entries():
    """素材库全量条目（找不到返回 []，绝不抛）。
    load_asset_index 契约 = (ok, msg, list) 三元组，兼容裸 list 返回。"""
    f = _lazy_import_api("load_asset_index")
    if f is None: return []
    try:
        data = f()
        if isinstance(data, tuple):
            data = data[2] if len(data) == 3 else next(
                (x for x in reversed(data) if isinstance(x, list)), [])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def lib_root():
    """素材库索引路径（报告里给"去哪查"）。"""
    f = _lazy_import_api("asset_index_path")
    try:
        return f() if f else ""
    except Exception:
        return ""


def _clip_kind(track, src):
    """clip 的体检分类（与导出层消费口径一致）。"""
    sk = str(src.get("kind") or "")
    tname = str(track.get("name") or "").upper()
    if sk == "audio": return "audio"
    if sk == "video": return "video"
    if sk == "puppet": return "figure"
    # image：BG 轨/满幅底轨 = 背景，FIG 轨 = 立绘（沿用 I1 判定精神）
    if tname.startswith("FIG"): return "figure"
    if tname.startswith("BG") or tname in ("", "V1") or "底" in tname:
        return "background"
    return "background"


def _abs(path, proj_dir):
    """与导出层 _abs_path 同口径的解析（相对路径挂工程目录）。"""
    p = str(path or "")
    if not p: return ""
    if os.path.isabs(p): return p
    return os.path.join(proj_dir or "", p)


def is_placeholder(name, path):
    """占位符判定：UI 话术词命中，或**无扩展名**的纯中文短语且素材库
    无同名。带正常扩展名（.png/.mp3…）的引用一律不算占位符——那是
    真素材形态，找不到属"真缺失"。"""
    s = str(name or "")
    low = s.lower()
    if any(w in low or w in s for w in _PLACEHOLDER_WORDS): return True
    base = os.path.basename(str(path or s).replace("\\", "/").rstrip("/"))
    if os.path.splitext(base)[1]: return False
    if base and re.search(r"[\u4e00-\u9fff]", base):
        for e in lib_entries():
            en = str(e.get("name") or "")
            eb = os.path.basename(en)
            if en and (en == base or eb == base or
                       os.path.splitext(eb)[0] == base):
                return False
        return True
    return False


def suggest(name, kind, limit=3):
    """素材库模糊候选：difflib 相近名 + 子串命中，去重截断。"""
    stem = os.path.splitext(os.path.basename(
        str(name or "").replace("\\", "/")))[0]
    names = []
    for e in lib_entries():
        en = str(e.get("name") or "")
        if not en: continue
        ek = str(e.get("kind") or "")
        # kind 对齐：figure/background 都收 model/image/bg 邻近类
        if kind == "audio" and ek not in ("bgm", "sfx", "audio"): continue
        if kind == "video" and ek != "video": continue
        if kind in ("figure", "background") and ek in ("bgm", "sfx",
                                                       "audio", "video"):
            continue
        names.append(en)
    out = []
    with contextlib.suppress(Exception):
        out += difflib.get_close_matches(stem, names, n=limit, cutoff=0.3)
    low = stem.lower()
    for en in names:
        if len(out) >= limit: break
        el = en.lower()
        if (low and (low in el or el in low) or
                (len(low) >= 2 and low[:2] in el)):
            if en not in out: out.append(en)
    return out[:limit]


def audit_project(proj, proj_dir=None):
    """体检主入口（纯读）。返回
    {"missing":[{"kind","name","path","category","cid","suggestions"}],
     "counts":{"background":n,"figure":n,"audio":n,"video":n},
     "placeholder":n, "real":n, "lib":"<索引路径>"}
    任何异常 -> 空报告（体检绝不抛）。"""
    rep = {"missing": [], "counts": {"background": 0, "figure": 0,
                                     "audio": 0, "video": 0},
           "placeholder": 0, "real": 0, "lib": lib_root()}
    try:
        if not isinstance(proj, dict): return rep
        # v1 顶层 tracks / v2 多幕 acts[].tracks 双形态（与 video_model
        # _tracks_of 同思路，但这里只读不写）
        tracks_all = []
        acts = proj.get("acts")
        if isinstance(acts, list) and acts:
            for a in acts:
                if isinstance(a, dict):
                    for tr in (a.get("tracks") or []):
                        tracks_all.append(tr)
        else:
            for tr in (proj.get("tracks") or []):
                tracks_all.append(tr)
        seen = set()
        for tr in tracks_all:
            if not isinstance(tr, dict): continue
            for c in (tr.get("clips") or []):
                if not isinstance(c, dict): continue
                src = c.get("source") or {}
                sk = src.get("kind")
                if sk not in ("image", "puppet", "audio", "video"): continue
                path = _abs(src.get("path"), proj_dir)
                # v3.5.13：纯色背景（`changeBg:black` 导入成
                # path="color:black"）是引擎内置关键字，**没有对应文件**，
                # 不算缺失素材。早先会报「背景缺 1（black）」并提示相近
                # 候选，纯粹是噪声——用户被误导去找一张不存在的图。
                if str(src.get("path") or "").startswith("color:"): continue
                if not path or os.path.exists(path): continue
                k = _clip_kind(tr, src)
                name = os.path.basename(str(src.get("path") or "").
                                        replace("\\", "/").rstrip("/")) \
                    or str(src.get("path") or "")
                # 同一缺失文件只报一次（多 clip 引用同素材）
                key = (k, name)
                if key in seen: continue
                seen.add(key)
                cat = "placeholder" if is_placeholder(name, path) else "real"
                rep["missing"].append({
                    "kind": k, "name": name,
                    "path": str(src.get("path") or ""),
                    "category": cat, "cid": str(c.get("id") or ""),
                    "suggestions": suggest(name, k) if cat == "real" else []})
                rep["counts"][k] += 1
                rep[cat] += 1
    except Exception:
        return {"missing": [], "counts": {"background": 0, "figure": 0,
                                          "audio": 0, "video": 0},
                "placeholder": 0, "real": 0, "lib": ""}
    return rep


_LABEL = {"background": "背景", "figure": "立绘", "audio": "音频",
          "video": "视频"}


def format_report(rep, max_names=3):
    """人读摘要（导入 msg 尾部 / 对话框列表用），无缺失返回空串。"""
    try:
        if not rep or not rep.get("missing"): return ""
        parts = []
        for k in ("background", "figure", "audio", "video"):
            n = rep["counts"].get(k, 0)
            if n:
                names = [m["name"] for m in rep["missing"]
                         if m["kind"] == k][:max_names]
                more = "等" if n > max_names else ""
                parts.append("%s缺 %d（%s%s）" % (_LABEL[k], n,
                                                "、".join(names), more))
        tail = []
        if rep.get("placeholder"):
            tail.append("其中 %d 个像脚本作者的占位符，导出会跳过"
                        % rep["placeholder"])
        real_items = [m for m in rep["missing"] if m["category"] == "real"]
        if real_items:
            with_sug = sum(1 for m in real_items if m["suggestions"])
            t = "%d 个素材库找不到" % len(real_items)
            if with_sug: t += "（%d 个有相近候选）" % with_sug
            tail.append(t)
        return "素材体检：" + "；".join(parts) + \
            ("；" + "，".join(tail) if tail else "")
    except Exception:
        return ""


def audit_hint(proj, proj_dir=None):
    """一步式：体检 + 摘要（无缺失=空串）。供 import msg 尾部追加。"""
    try:
        return format_report(audit_project(proj, proj_dir))
    except Exception:
        return ""
