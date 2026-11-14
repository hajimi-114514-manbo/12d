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
② `<程序目录>/asset_library`      —— 推荐部署位：解压到程序目录旁边即可
③ `<程序目录>/../webgal_games/asset_library` —— 开发机布局（源码树）
④ `<程序目录>/../asset_library`
⑤ 注册表 HKCU\\Software\\MuppetStudio 的 `AssetLibraryDir`（用户手工指定）

判定"是素材库"的标准只有一个：目录里有 `asset_index.json`。
都不命中时返回 ②（最自然的用户放置位），由调用方按"缺失"降级处理。

本模块 **import 零副作用**（不建目录、不读盘、不报错），可被任何模块安全引入。
"""
import os

_APPDIR = os.path.dirname(os.path.abspath(__file__))
_REG_KEY = r"Software\MuppetStudio"
_REG_VAL = "AssetLibraryDir"


def _is_lib(d):
    """目录像素材库吗：有 asset_index.json 才算。"""
    return bool(d) and os.path.isfile(os.path.join(d, "asset_index.json"))


def _env_dir():
    """env MUPPET_ASSET_INDEX -> 目录（传 json 则取父目录）；未设 None。"""
    env = os.environ.get("MUPPET_ASSET_INDEX", "").strip()
    if not env:
        return None
    p = os.path.normpath(env)
    if os.path.isfile(p) or p.lower().endswith(".json"):
        return os.path.dirname(p)
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
    if env is not None:
        return [env]
    out = [
        os.path.join(_APPDIR, "asset_library"),
        os.path.normpath(os.path.join(_APPDIR, "..", "webgal_games",
                                      "asset_library")),
        os.path.normpath(os.path.join(_APPDIR, "..", "asset_library")),
    ]
    reg = _reg_dir()
    if reg:
        out.append(reg)
    return out


def asset_library_dir():
    """素材库根目录（已存在的优先）。都不存在 -> 推荐部署位（可能不存在）。"""
    cs = candidates()
    for d in cs:
        if _is_lib(d):
            return d
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
            "candidates": rows}
