# -*- coding: utf-8 -*-
"""语音引擎路径解析 —— 全模块唯一口径（对标 asset_paths.py）。

为什么单独一个模块
------------------
「语音引擎装在哪」是语音功能能不能跑起来的唯一命门：引擎（Python 环境 +
GPT_SoVITS 代码 + 底模）任何一个零件找不到，合成就起不来。此前各模块各自
写死路径，那是**开发机布局**（D:\\gsv_slim\\work），真实用户装到任意目录
必然落空 —— 与 v3.4.2 素材库那个坑同源，所以按 asset_paths 的做法独立出来。

解析顺序（先命中先用）
----------------------
① env `MUPPET_VOICE_DIR`（指引擎根目录）—— **硬限定**，设了就只认它，
   供测试隔离与特殊部署
② 用户数据目录 `<APPDATA>/MuppetStudio/voice`（推荐位）
   —— 装到 Program Files 时程序目录不可写，用户数据目录总是可写
③ 程序目录 `<程序目录>/voice`
④ 程序目录旁的 `../voice`
⑤ 注册表 HKCU\\Software\\MuppetStudio 的 `VoiceDir`

引擎根目录长这样（与开发机一致，便于直接指向现成的裁剪版）：
    <root>/runtime/python.exe         引擎自己的 Python 3.9（含 CUDA 版 torch）
    <root>/GPT_SoVITS/                 GPT-SoVITS 项目代码
    <root>/GPT_SoVITS/pretrained_models/   底模（s1v3.ckpt / s2Gv2ProPlus.pth ...）
    <root>/voices/                     角色音色（ckpt+pth 成对）

**import 零副作用**：不建目录、不扫盘、不抛异常。
"""
import io
import json
import os
import string

_APPDIR = os.path.dirname(os.path.abspath(__file__))
_REG_KEY = r"Software\MuppetStudio"
_REG_VAL = "VoiceDir"

# 底模必须齐的标志性文件（缺任一 -> 这个目录不是引擎）
_MARKER = os.path.join("GPT_SoVITS", "pretrained_models", "s1v3.ckpt")


def _user_data_dir():
    """用户数据根目录（%APPDATA%/MuppetStudio）。取不到返回 ""。"""
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if not base:
        return ""
    return os.path.join(base, "MuppetStudio")


def _env_dir():
    """env MUPPET_VOICE_DIR -> 目录；未设 None。"""
    env = os.environ.get("MUPPET_VOICE_DIR", "").strip()
    if not env:
        return None
    return os.path.normpath(env)


def _reg_dir():
    """注册表里用户手工指定的引擎目录；读不到返回 None。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
            v, _t = winreg.QueryValueEx(k, _REG_VAL)
        v = str(v or "").strip()
        return os.path.normpath(v) if v else None
    except Exception:
        return None


def _is_engine(d):
    """目录像语音引擎吗：有底模标志文件才算。"""
    return bool(d) and os.path.isfile(os.path.join(d, _MARKER))


def candidates():
    """候选目录列表（按优先级）。env 设置时**只返回它**（硬限定）。"""
    env = _env_dir()
    if env is not None:
        return [env]
    out = []
    ud = _user_data_dir()
    if ud:
        out.append(os.path.join(ud, "voice"))
    out.append(os.path.join(_APPDIR, "voice"))
    out.append(os.path.normpath(os.path.join(_APPDIR, "..", "voice")))
    reg = _reg_dir()
    if reg:
        out.append(reg)
    return out


def voice_dir(scan=False):
    """语音引擎根目录（已存在的优先）。都不存在 -> 返回推荐部署位。

    不扫盘（保持本函数轻量、可任意调用）；需要"扫盘并记住"时用
    ensure_voice()。
    """
    for d in candidates():
        if _is_engine(d):
            return d
    cs = candidates()
    return cs[0] if cs else os.path.join(_APPDIR, "voice")


# 已装引擎的档位目录名（资产管理装到 voice/<tier>/）
TIERS = ("zh-basic", "zh-full", "ja", "zh-ja")


def _installed_tier(root):
    """在引擎根下找出已装的档位子目录（装了就返回档位名，没装返回 ""）。"""
    if not root or not os.path.isdir(root):
        return ""
    for t in TIERS:
        if _is_engine(os.path.join(root, t)):
            return t
    return ""


def installed_tier():
    """当前装了哪一档引擎（没装返回 ""）。

    资产管理把四档装到 voice/<档位>/ 各自独立（用户可能先试「中文基础」，
    不满意再装「中日双语」，覆盖就白下 3GB），所以要按档位找。
    """
    env = _env_dir()
    if env is not None:
        t = _installed_tier(env)
        if t:
            return t
        return "" if _is_engine(env) else ""
    for d in candidates():
        t = _installed_tier(d)
        if t:
            return t
        if _is_engine(d):
            return ""
    return ""


def ensure_voice(scan=True):
    """取得可用的引擎目录；找不到返回 None。

    找不到时**不扫盘**（全盘扫会卡几十秒）—— 语音引擎体积大、位置特殊，
    由用户从资产管理里装，比扫盘靠谱。
    """
    env = _env_dir()
    if env is not None:
        if _is_engine(env):
            return env
        t = _installed_tier(env)
        return os.path.join(env, t) if t else None
    for d in candidates():
        t = _installed_tier(d)
        if t:
            return os.path.join(d, t)
        if _is_engine(d):
            return d
    if scan:
        found = scan_for_voice()
        if found:
            return found
    return None


def scan_for_voice(max_candidates=2000):
    """扫盘找语音引擎（浅层，有界）。

    找法：常见位置 + 盘符根下一层的 voice/engine 命名目录。
    **不做全盘递归**（那会卡几十秒）。
    """
    seen = set()
    n = 0

    def check(d):
        nonlocal n
        n += 1
        if n > max_candidates:
            return None
        k = os.path.normcase(os.path.normpath(d))
        if k in seen:
            return None
        seen.add(k)
        return d if _is_engine(d) else None

    for d in candidates():
        r = check(d)
        if r:
            return r
    roots = [os.path.dirname(_APPDIR), _APPDIR]
    for ch in string.ascii_uppercase:
        roots.append("%s:\\" % ch)
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            subs = sorted(os.listdir(root))
        except Exception:
            continue
        for sub in subs:
            if n > max_candidates:
                return None
            low = sub.lower()
            if low not in ("voice", "voice_engine", "gpt_sovits",
                           "gpt-sovits", "gsv"):
                continue
            p = os.path.join(root, sub)
            if os.path.isdir(p):
                r = check(p)
                if r:
                    return r
    return None


def engine_python(root=None):
    """引擎自带的 Python 解释器路径；没有返回 ""。

    引擎要 CUDA 版 torch，与木偶内置的 CPU 版运行时**不是一套**，
    必须用它自己的解释器起 api_v2.py。
    """
    r = root or voice_dir()
    if not r:
        return ""
    for rel in (os.path.join("runtime", "python.exe"),
                os.path.join("runtime", "python", "python.exe"),
                os.path.join("runtime", "python.exe")):
        p = os.path.join(r, rel)
        if os.path.isfile(p):
            return p
    return ""


def pretrained_dir(root=None):
    """底模目录（<engine>/GPT_SoVITS/pretrained_models）。"""
    r = root or voice_dir()
    if not r:
        return ""
    return os.path.join(r, "GPT_SoVITS", "pretrained_models")


def code_dir(root=None):
    """GPT-SoVITS 项目代码目录（<engine>/GPT_SoVITS）。"""
    r = root or voice_dir()
    if not r:
        return ""
    return os.path.join(r, "GPT_SoVITS")


def voices_dir(root=None):
    """音色目录（<engine>/voices）—— 角色 ckpt+pth 成对放这里。"""
    r = root or voice_dir()
    if not r:
        return ""
    return os.path.join(r, "voices")


def api_entry(root=None):
    """api_v2.py 路径（引擎的 HTTP 服务入口）。"""
    c = code_dir(root)
    if not c:
        return ""
    for rel in ("api_v2.py", os.path.join("api_v2.py")):
        p = os.path.join(c, rel)
        if os.path.isfile(p):
            return p
    # api_v2.py 在引擎根的情况
    r = root or voice_dir()
    p = os.path.join(r, "api_v2.py")
    return p if os.path.isfile(p) else ""


def available():
    """语音引擎是否就绪（底模 + 解释器 + api 入口三者齐）。

    UI 用它决定「配音」按钮是否可点。缺任何一样都返回 False。
    """
    try:
        r = ensure_voice(scan=False)
        if not r:
            return False
        return bool(engine_python(r)) and bool(api_entry(r))
    except Exception:
        return False


def describe():
    """诊断用：候选列表 + 各自状态。"""
    rows = []
    for d in candidates():
        rows.append({"dir": d, "ok": _is_engine(d),
                     "has_python": bool(engine_python(d)),
                     "has_api": bool(api_entry(d))})
    return {"resolved": voice_dir(), "env_locked": bool(_env_dir()),
            "available": available(), "tier": installed_tier(),
            "candidates": rows}
