# -*- coding: utf-8 -*-
"""语音音色管理：扫描 / 导入 / 配对检查。

音色是什么
----------
GPT-SoVITS 一个「音色」= **一对**权重文件：
    <名字>.ckpt   GPT 侧（文本 -> 韵律）
    <名字>.pth    SoVITS 侧（韵律 -> 语音）
两个文件**同名同目录**，缺一个就合成不出（引擎加载时会直接报错）。

放在哪
------
<语音引擎根>/voices/。引擎按「同名 ckpt+pth」自动配对，不需要登记。

为什么要有「导入」入口
----------------------
这 13 个角色音色是 **CC-BY-NC 许可，禁止二次配布**，HuggingFace 上也
没有（实测逐个精确搜索 13/13 全无）。所以：
  · 不能随安装包分发
  · 用户只能自己拿到 ckpt+pth 后导进来
木偶负责提供**导入与校验**，不负责提供内容。

对外 API：
    voices()                  -> [{"stem","ckpt","pth","ok","size"}]  已导入的音色
    voice_names()             -> [str]
    find_voice(stem)          -> dict|None
    import_voice(ckpt, pth)   -> (ok, msg)   导入一对音色
    validate_pair(ckpt, pth)  -> (ok, msg)   配对与基本完整性检查
"""
import contextlib
import hashlib
import io
import json
import os
import shutil

import voice_paths as VP

# 音色文件名里允许的字符（排除 Windows 非法字符）
_BAD_CHARS = '<>:"/\\|?*'
_MIN_CKPT = 50 * 1024 * 1024      # GPT 权重下限（约 148MB）
_MIN_PTH = 50 * 1024 * 1024       # SoVITS 权重下限（约 165MB）

# 侧别 -> (扩展名, 体积下限, 显示名)。收成表，以后加第三种侧别只改这里。
_SIDE_SPEC = {
    "ckpt": (".ckpt", _MIN_CKPT, "GPT 侧"),
    "pth":  (".pth",  _MIN_PTH,  "SoVITS 侧"),
}


def _clean_stem(s):
    """把任意字符串变成可用作文件名的 stem。"""
    s = str(s or "").strip()
    for c in _BAD_CHARS:
        s = s.replace(c, "_")
    s = s.rstrip(". ")
    return s


def _scan():
    """扫描 voices/ 目录，按同名配对 ckpt+pth。"""
    d = VP.voices_dir()
    if not d or not os.path.isdir(d):
        return []
    try:
        names = os.listdir(d)
    except Exception:
        return []
    stems = {}
    for n in names:
        st, ext = os.path.splitext(n)
        if ext.lower() not in (".ckpt", ".pth"):
            continue
        stems.setdefault(st, {})[ext.lower()] = os.path.join(d, n)
    out = []
    for st, pair in sorted(stems.items()):
        ck = pair.get(".ckpt", "")
        pt = pair.get(".pth", "")
        size = 0
        for p in (ck, pt):
            with contextlib.suppress(Exception):
                size += os.path.getsize(p)
        out.append({
            "stem": st,
            "ckpt": ck,
            "pth": pt,
            "ok": bool(ck) and bool(pt),
            "size": size,
        })
    return out


def voices():
    """已导入的音色列表（配对完整的在前，按 stem 排序）。"""
    rows = _scan()
    rows.sort(key=lambda r: (not r["ok"], r["stem"]))
    return rows


def voice_names():
    """配对完整的音色名（可直接用来选）。"""
    return [r["stem"] for r in _scan() if r["ok"]]


def find_voice(stem):
    """按 stem 查音色；找不到返回 None。"""
    st = str(stem or "").strip()
    for r in _scan():
        if r["stem"] == st:
            return r
    return None


def validate_pair(ckpt, pth):
    """校验一对音色是否可以导入。返回 (ok, msg)。

    检查项：
      · 两个文件都存在
      · stem 相同（引擎靠同名配对）
      · 体积合理（太小多半是下坏了/断点续传残留）
      · 扩展名正确（.ckpt 是 GPT 侧，.pth 是 SoVITS 侧）
    """
    ck = str(ckpt or "").strip()
    pt = str(pth or "").strip()
    if not ck or not pt:
        return False, "需要同时选择 .ckpt（GPT 侧）和 .pth（SoVITS 侧）两个文件"
    for p, side in ((ck, "ckpt"), (pt, "pth")):
        ext, min_bytes, label = _SIDE_SPEC[side]
        if not os.path.isfile(p):
            return False, "%s 不存在：%s" % (label, p)
        if os.path.splitext(p)[1].lower() != ext:
            return False, "%s 文件名后缀应为 %s（当前 %s）" % (
                label, ext, os.path.splitext(p)[1] or "无")
        sz = os.path.getsize(p)
        if sz < min_bytes:
            return False, "%s 体积异常（%.1f MB，正常约 150~170MB），可能没下完" % (
                label, sz / 1048576.0)
    ck_stem = os.path.splitext(os.path.basename(ck))[0]
    pt_stem = os.path.splitext(os.path.basename(pt))[0]
    if ck_stem != pt_stem:
        return False, ("两个文件名必须相同（引擎靠同名配对）：\n"
                       "  ckpt = %s\n  pth  = %s" % (ck_stem, pt_stem))
    return True, ck_stem


def import_voice(ckpt, pth, name=""):
    """导入一对音色到引擎的 voices/ 目录。返回 (ok, msg)。

    重名时**不覆盖**（改名加序号），避免用户误覆盖已有的音色。
    """
    ok, msg = validate_pair(ckpt, pth)
    if not ok:
        return False, msg
    stem = _clean_stem(name or msg)
    d = VP.voices_dir()
    if not d:
        return False, ("语音引擎未就绪：找不到 voices 目录\n"
                       "请先在资产管理里安装语音引擎")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        return False, "无法创建音色目录：%s" % e

    # 重名不覆盖
    base, n = stem, 2
    while (os.path.exists(os.path.join(d, base + ".ckpt"))
           or os.path.exists(os.path.join(d, base + ".pth"))):
        base = "%s_%d" % (stem, n)
        n += 1

    try:
        shutil.copy2(ckpt, os.path.join(d, base + ".ckpt"))
        shutil.copy2(pth, os.path.join(d, base + ".pth"))
    except Exception as e:
        # 复制失败要清掉半成品，不能留只有一半的音色
        for ext in (".ckpt", ".pth"):
            with contextlib.suppress(Exception):
                os.remove(os.path.join(d, base + ext))
        return False, "复制失败：%s" % e
    return True, "已导入音色：%s" % base


def remove_voice(stem):
    """删除一个音色（两个文件一起删）。返回 (ok, msg)。"""
    r = find_voice(stem)
    if not r:
        return False, "找不到音色：%s" % stem
    for p in (r["ckpt"], r["pth"]):
        if p and os.path.isfile(p):
            with contextlib.suppress(Exception):
                os.remove(p)
    return True, "已删除音色：%s" % r["stem"]


def stats():
    """概览：音色数、配对完整性、占用体积。"""
    rows = _scan()
    ok = [r for r in rows if r["ok"]]
    bad = [r for r in rows if not r["ok"]]
    return {
        "total": len(rows),
        "ok": len(ok),
        "incomplete": len(bad),
        "size": sum(r["size"] for r in rows),
        "dir": VP.voices_dir(),
        "engine_ready": VP.available(),
    }
