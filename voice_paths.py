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
import contextlib
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


# ---------------- v3.7.6 用户自选目录（外置引擎 / 外置语音包） ----------------
# **为什么要这个**：引擎动辄 4GB、语音包（音色 ckpt+pth）上百 MB 一个，
# 用户几乎不可能把它们放进程序目录：①装到 Program Files 时程序目录只读；
# ②C 盘常年告急，引擎/音色通常放在 D:/E: 的大盘上；③一个人可能有多套
# 引擎（中文基础档 / 中日双语档）或几组音色包，分目录存更方便搬迁。
# 所以两个位置都必须**用户可选 + 记住**：
#   · 引擎根目录 —— 含 GPT_SoVITS/ 与 runtime/ 的那个目录
#   · 语音包目录 —— 放 *.ckpt + *.pth 音色的目录（<引擎>/voices 只是默认）
#
# 存法：%APPDATA%/MuppetStudio/voice_paths.json
#   {"engine": "<引擎根>", "pack": "<语音包目录>"}
# **只存用户显式选过的键**；没选过就不写（保持旧的候选探测行为，零回归）。
_MEMO_NAME = "voice_paths.json"


def _memo_path():
    """用户自选目录的记忆文件；取不到用户目录时落到程序旁 .runtime。"""
    ud = _user_data_dir()
    if ud:
        with contextlib.suppress(Exception):
            os.makedirs(ud, exist_ok=True)
        return os.path.join(ud, _MEMO_NAME)
    return os.path.join(_APPDIR, ".runtime", _MEMO_NAME)


def _memo_load():
    """读记忆 {"engine": str, "pack": str}；缺失/损坏 -> {}。"""
    try:
        with io.open(_memo_path(), encoding="utf-8") as f:
            o = json.load(f)
    except Exception:
        return {}
    if not isinstance(o, dict):
        return {}
    out = {}
    for k in ("engine", "pack"):
        v = o.get(k)
        # 只认字符串（`{"engine": 123}` 这种脏数据不能变成 "123" 当路径）
        if isinstance(v, str) and v.strip():
            out[k] = os.path.normpath(v.strip())
    return out


def _memo_save(**kw):
    """写记忆（只更新传入的键，其余保留）。失败静默。"""
    cur = _memo_load()
    for k, v in kw.items():
        if k not in ("engine", "pack"):
            continue
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = os.path.normpath(str(v))
    try:
        p = _memo_path()
        with contextlib.suppress(Exception):
            os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def remember_engine_dir(d):
    """记住用户选的引擎根目录（传 None/"" 清除）。"""
    return _memo_save(engine=(d or None))


def remember_pack_dir(d):
    """记住用户选的语音包目录（传 None/"" 清除）。"""
    return _memo_save(pack=(d or None))


def remembered_engine_dir():
    """用户选过的引擎目录；没选过返回 None。**不校验存在性**。"""
    return _memo_load().get("engine")


def remembered_pack_dir():
    """用户选过的语音包目录；没选过返回 None。**不校验存在性**。"""
    return _memo_load().get("pack")


def engine_dir_ok(d):
    """该目录能不能当引擎根（对外暴露，UI 判「可保存」用）。

    与 _is_engine 的区别：_is_engine 只认底模标志文件（严格）；
    这里放宽一点 —— 只要**像个引擎**（有 GPT_SoVITS 目录或 runtime 目录
    或底模），就让用户先选上，缺什么由 describe/available 如实报告。
    否则用户选了半装好的目录会被直接拒绝，没法先指过去再补装。
    """
    if not d or not os.path.isdir(d):
        return False
    if _is_engine(d):
        return True
    for rel in (os.path.join("GPT_SoVITS"),
                os.path.join("runtime"),
                os.path.join("GPT_SoVITS", "pretrained_models")):
        if os.path.isdir(os.path.join(d, rel)):
            return True
    # 直接指到底模目录也算（有人在 <root>/GPT_SoVITS/pretrained_models 里选）
    if os.path.isfile(os.path.join(d, "s1v3.ckpt")):
        return True
    return False


def pack_dir_ok(d):
    """该目录能不能当语音包目录（存在即可；音色文件由上层扫描）。"""
    return bool(d) and os.path.isdir(d)


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
    """候选目录列表（按优先级）。env 设置时**只返回它**（硬限定）。

    v3.7.6：**用户显式选过的目录排在第一位** —— 那是他最明确的意思表示，
    不该被「%APPDATA% 下的推荐位」压过去。没选过时行为与旧版完全一致。
    """
    env = _env_dir()
    if env is not None:
        return [env]
    out = []
    memo = remembered_engine_dir()
    if memo:
        out.append(memo)
    ud = _user_data_dir()
    if ud:
        out.append(os.path.join(ud, "voice"))
    out.append(os.path.join(_APPDIR, "voice"))
    out.append(os.path.normpath(os.path.join(_APPDIR, "..", "voice")))
    reg = _reg_dir()
    if reg and reg not in out:
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
    """音色目录 —— 角色 ckpt+pth 成对放这里。

    v3.7.6：**用户选过的语音包目录优先**。语音包（音色权重）与引擎
    （推理程序）是两种东西：引擎 4GB 装哪都行，音色包可能来自另一块盘、
    另一台机器拷过来，甚至一套引擎配几组音色包。所以独立可选。
      ① root 显式传入
      ② 用户选的语音包目录（remembered_pack_dir）
      ③ <引擎根>/voices（旧默认，保持向后兼容）
    """
    if root:
        return root
    pack = remembered_pack_dir()
    if pack:
        return pack
    r = voice_dir()
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
    """诊断用：候选列表 + 各自状态 + 用户自选目录。"""
    rows = []
    for d in candidates():
        rows.append({"dir": d, "ok": _is_engine(d),
                     "has_python": bool(engine_python(d)),
                     "has_api": bool(api_entry(d))})
    return {"resolved": voice_dir(), "env_locked": bool(_env_dir()),
            "available": available(), "tier": installed_tier(),
            "engine_picked": remembered_engine_dir() or "",
            "pack_picked": remembered_pack_dir() or "",
            "voices_dir": voices_dir(),
            "candidates": rows}
