# -*- coding: utf-8 -*-
"""素材库（asset_library）位置解析 —— 全模块唯一口径。

为什么需要这个模块
------------------
v3.4.2 让 Live2D core 随素材库交付后，"素材库在哪"成了产品能不能跑起来的
唯一命门。此前各模块各自写死 `程序目录/../webgal_games/asset_library`，
那是**开发机布局**（D:\\work\\puppet\\muppet_studio 与 D:\\work\\puppet\\
webgal_games 平级）。真实用户把程序装到任意目录（安装器让用户自选路径），
那个相对推断必然落空 —— 装完就用不了素材库，等于没做到"完全独立"。

解析顺序（先命中先用）
----------------------
① env `MUPPET_ASSET_INDEX`（指 json 或 asset_library 目录都行）
   **硬限定**：一旦设置就只认它，不再往下找。语义同 MUPPET_WEBGAL_PACK_DIR，
   测试用它做完全隔离。
② **记住的目录**（`asset_library_dir.json`，见下）
③ `<程序目录>/asset_library`      —— 推荐部署位：解压到程序目录旁边即可
④ `<程序目录>/../webgal_games/asset_library` —— 开发机布局（源码树）
⑤ `<程序目录>/../asset_library`
⑥ 注册表 HKCU\\Software\\MuppetStudio 的 `AssetLibraryDir`（用户手工指定）

「记住 + 扫盘」（v3.5.13，用户要求）
-----------------------------------
用户原话：「asset_library 的目录直接让 v3 记住，不要每次导入都选整合包
位置了。具体实现就第一次打开视频编辑扫盘确认 asset_library 的位置，然后
记住，如果之后原位置找不到文件了就再扫，扫不到就让导出 webgal 按钮灰掉
不可用」。

- `ensure_library(scan=True)`：先走上面 ①~⑥，全落空则**扫盘**找含
  `asset_index.json` 的目录；命中就写进 `<用户数据>/asset_library_dir.json`
  记住，下次直接用（不再扫盘）。
- 记住的目录失效（被删/移动）时自动重新扫盘；扫不到返回 None。
- 扫盘范围有界：常见盘符的浅层目录 + 已知命名（webgal_games /
  asset_library），**不做全盘递归**（那会卡几十秒）。
- `available()` 给 UI 用：False 就让「导出 WebGAL」按钮灰掉。

本模块 **import 零副作用**（不建目录、不扫盘、不报错），可被任何模块安全引入。
扫盘只发生在显式调用 `ensure_library()` / `rescan()` 时。
"""
import io
import json
import os
import string
import contextlib

_APPDIR = os.path.dirname(os.path.abspath(__file__))
_REG_KEY = r"Software\MuppetStudio"
_REG_VAL = "AssetLibraryDir"

# 记住的目录（用户数据，不是程序目录：程序升级/换盘不该丢这个记忆）
_MEMO_NAME = "asset_library_dir.json"


def _memo_path():
    """记忆文件路径。

    优先用户数据目录（APPDATA/MuppetStudio），退到程序目录。
    为什么不用程序目录优先：安装到 Program Files 时那里不可写。
    """
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        d = os.path.join(base, "MuppetStudio")
        with contextlib.suppress(Exception):
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, _MEMO_NAME)
    return os.path.join(_APPDIR, _MEMO_NAME)


def _is_lib(d):
    """目录像素材库吗：有 asset_index.json 才算。"""
    return bool(d) and os.path.isfile(os.path.join(d, "asset_index.json"))


def _env_dir():
    """env MUPPET_ASSET_INDEX -> 目录（传 json 则取父目录）；未设 None。"""
    env = os.environ.get("MUPPET_ASSET_INDEX", "").strip()
    if not env: return None
    p = os.path.normpath(env)
    if os.path.isfile(p) or p.lower().endswith(".json"): return os.path.dirname(p)
    return p


def _reg_dir():
    """注册表里用户手工指定的素材库目录；读不到返回 None。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
            v, _t = winreg.QueryValueEx(k, _REG_VAL)
        v = str(v or "").strip()
        return os.path.normpath(v) if v else None
    except Exception:
        return None


def candidates():
    """候选目录列表（按优先级）。env 设置时**只返回它**（硬限定）。"""
    env = _env_dir()
    if env is not None: return [env]
    out = []
    memo = _memo_dir()
    if memo: out.append(memo)
    out.extend([
        os.path.join(_APPDIR, "asset_library"),
        os.path.normpath(os.path.join(_APPDIR, "..", "webgal_games",
                                      "asset_library")),
        os.path.normpath(os.path.join(_APPDIR, "..", "asset_library")),
    ])
    reg = _reg_dir()
    if reg: out.append(reg)
    return out


def _memo_dir():
    """读记住的素材库目录；没记/文件坏 -> None。**不校验存在性**（调用方判）。"""
    try:
        o = json.load(io.open(_memo_path(), encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(o, dict): return None
    d = o.get("dir")
    # 必须是字符串（`{"dir": 123}` 这种脏数据不能变成 "123" 当路径用）
    if not isinstance(d, str): return None
    d = d.strip()
    return os.path.normpath(d) if d else None


def remember_dir(d):
    """把素材库目录记住（写用户数据）。失败静默（记忆丢了不影响本次使用）。"""
    if not d: return False
    try:
        json.dump({"dir": os.path.normpath(str(d)), "ts": int(__import__(
            "time").time())},
            io.open(_memo_path(), "w", encoding="utf-8"),
            ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def forget_dir():
    """清除记忆（测试/用户改路径时用）。"""
    try:
        p = _memo_path()
        if os.path.isfile(p): os.remove(p)
        return True
    except Exception:
        return False


def _scan_roots():
    """扫盘起点：所有存在的盘符根 + 程序目录的父级。

    有界：只列**根目录的直接子目录**（浅层），不递归——全盘递归会卡几十秒，
    用户点开视频编辑就得等，那是不可接受的。真实部署位置几乎都在盘符根
    或程序目录旁边一层。
    """
    roots = []
    # 程序目录及其父级（安装到 D:\xxx\MuppetStudio 时，父级常放素材库）
    roots.append(os.path.dirname(_APPDIR))
    roots.append(_APPDIR)
    # 盘符根
    for ch in string.ascii_uppercase:
        d = "%s:\\" % ch
        if os.path.isdir(d): roots.append(d)
    # 去重保序
    seen = set()
    out = []
    for r in roots:
        k = os.path.normcase(os.path.normpath(r))
        if k in seen: continue
        seen.add(k)
        out.append(r)
    return out


# 已知会含素材库的目录名（优先查这些）
_HINT_NAMES = ("asset_library", "webgal_games")


def scan_for_library(max_candidates=4000):
    """扫盘找素材库（有 asset_index.json 的目录）。返回目录或 None。

    策略（有界、按优先级）：
      1) 扫盘起点的**直接子目录**，命中就返回
      2) 对名字像素材库的目录（webgal_games），再看它下一层
    最多考察 max_candidates 个目录，超了直接放弃（宁可让 UI 灰掉，
    也不让用户点一下卡半分钟）。
    """
    seen = set()
    n = 0

    def check(d):
        """命中返回目录。"""
        nonlocal n
        n += 1
        k = os.path.normcase(os.path.normpath(d))
        if k in seen: return None
        seen.add(k)
        if _is_lib(d): return d
        return None

    # 先看已知候选（快路径，绝大多数情况这里就命中）
    for d in candidates():
        r = check(d)
        if r: return r

    roots = _scan_roots()
    # 第一层：根的直接子目录
    for root in roots:
        if n > max_candidates: return None
        try:
            subs = os.listdir(root)
        except Exception:
            continue
        # 先查名字像素材库的
        subs.sort(key=lambda x: (0 if x.lower() in _HINT_NAMES else 1, x.lower()))
        for sub in subs:
            if n > max_candidates: return None
            p = os.path.join(root, sub)
            if not os.path.isdir(p): continue
            r = check(p)
            if r: return r
            # 名字像容器目录的，再往下一层找 asset_library
            if sub.lower() in _HINT_NAMES:
                with contextlib.suppress(Exception):
                    for sub2 in os.listdir(p):
                        if n > max_candidates: return None
                        p2 = os.path.join(p, sub2)
                        if os.path.isdir(p2):
                            r = check(p2)
                            if r: return r
    return None


def ensure_library(scan=True, force_rescan=False):
    """取得可用的素材库目录；失效时（可选）扫盘并记住。返回目录或 None。

    用户要求的行为：第一次打开视频编辑扫盘确认位置 -> 记住 ->
    之后直接用；原位置失效了再扫；扫不到返回 None（UI 据此灰掉导出按钮）。
    """
    env = _env_dir()
    if env is not None:
        # env 硬限定：不扫盘、不记忆，只认它（测试隔离语义）
        return env if _is_lib(env) else None

    if not force_rescan:
        # 记忆优先
        memo = _memo_dir()
        if memo and _is_lib(memo): return memo
        # 其余已知候选
        for d in candidates():
            if _is_lib(d):
                # 顺手记住（下次少走分支）
                if d != memo: remember_dir(d)
                return d

    if not scan: return None

    found = scan_for_library()
    if found:
        remember_dir(found)
        return found
    return None


def available():
    """素材库是否可用（UI 用它决定「导出 WebGAL」是否可点）。

    **同时**要求 asset_index.json 存在且 figure/ 目录在——只有索引没有
    素材文件的情况（用户删了一半）导出的包必然是坏的，那种情况同样该灰。
    """
    try:
        d = ensure_library(scan=False)
        if not d: return False
        if not _is_lib(d): return False
        return os.path.isdir(os.path.join(d, "figure"))
    except Exception:
        return False


def asset_library_dir():
    """素材库根目录（已存在的优先）。都不存在 -> 推荐部署位（可能不存在）。

    v3.5.13：记忆目录排在最前（见模块 docstring「记住 + 扫盘」）。
    这里**不扫盘**（保持本函数轻量、可任意调用）；需要"扫盘并记住"时用
    ensure_library()。"""
    cs = candidates()
    for d in cs:
        if _is_lib(d): return d
    return cs[0] if cs else os.path.join(_APPDIR, "asset_library")


def asset_index_path():
    """asset_index.json 绝对路径（只给路径，不保证存在）。"""
    return os.path.join(asset_library_dir(), "asset_index.json")


def core_dir():
    """Live2D core 目录（素材库内 lib/，v3.4.2 起随库交付）。"""
    return os.path.join(asset_library_dir(), "lib")


def describe():
    """诊断用：候选列表 + 各自是否存在（联动排查/日志）。"""
    rows = []
    for d in candidates():
        rows.append({"dir": d, "ok": _is_lib(d),
                     "has_lib_subdir": os.path.isdir(os.path.join(d, "lib"))})
    return {"resolved": asset_library_dir(), "env_locked": bool(_env_dir()),
            "remembered": _memo_dir(), "memo_file": _memo_path(),
            "available": available(), "candidates": rows}
