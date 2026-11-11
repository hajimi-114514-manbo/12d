# -*- coding: utf-8 -*-
"""资产管理：可下载资产清单 + 本地状态判定（纯数据层，无 Qt/无网络）。

给「资产管理」菜单做后端清单层：
  - ASSETS：内置运行时 / LayerDiff3D / Marigold / MobileSAM 四类资产定义
  - asset_id / installed_path / is_installed / installed_size /
    missing_assets / asset_by_id：纯函数，全部容错（不抛异常）

约定：
  - dest 用 "/" 分隔，函数内部转 os.sep
  - check.type == "exe"   -> base_dir/check.path 是文件且 > 1MB
  - check.type == "files" -> base_dir/dest/check.marker 存在且 >= min_bytes
"""
import os

# 1MB 阈值（exe 完整性判定）
_EXE_MIN = 1024 * 1024

ASSETS = [
    {
        "id": "runtime",
        "name": "内置 Python 运行时",
        "desc": "无需自己装 Python，安装后即可启动（约 600MB）",
        "kind": "runtime",
        "size": 620000000,
        "url": "",
        "dest": "runtime",
        "check": {"type": "exe", "path": "runtime/python.exe"},
    },
    {
        "id": "model_layerdiff",
        "name": "分层模型 LayerDiff3D (nf4)",
        "desc": "拆分图层必需，约 3.4GB",
        "kind": "model",
        "repo": "24yearsold/seethroughv0.0.2_layerdiff3d_nf4",
        "size": 3400000000,
        "url": "",
        "dest": "models/24yearsold/seethroughv0.0.2_layerdiff3d_nf4",
        "check": {"type": "files",
                  "marker": "unet/diffusion_pytorch_model.safetensors",
                  "min_bytes": 2900000000},
    },
    {
        "id": "model_marigold",
        "name": "深度模型 Marigold (nf4)",
        "desc": "生成深度信息，拆分图层必需，约 1.9GB",
        "kind": "model",
        "repo": "24yearsold/seethroughv0.0.1_marigold_nf4",
        "size": 1900000000,
        "url": "",
        "dest": "models/24yearsold/seethroughv0.0.1_marigold_nf4",
        "check": {"type": "files",
                  "marker": "unet/diffusion_pytorch_model.safetensors",
                  "min_bytes": 1500000000},
    },
    {
        "id": "model_sam",
        "name": "MobileSAM（精细拆分）",
        "desc": "智能分割辅助，约 40MB",
        "kind": "model",
        "size": 40000000,
        "url": "",
        "dest": "models/sam",
        "check": {"type": "files", "marker": "mobile_sam.pt",
                  "min_bytes": 30000000},
    },
]


def asset_id(a):
    """资产 id（容错：非 dict / 缺字段返回 None）。"""
    try:
        return a.get("id") if isinstance(a, dict) else None
    except Exception:
        return None


def _norm(p):
    """路径分隔符归一（dest/marker 用 "/"，Windows 要转 os.sep）。"""
    try:
        return p.replace("/", os.sep)
    except Exception:
        return p


def installed_path(asset, base_dir):
    """资产装好后应处的目录/文件路径：os.path.join(base_dir, dest)。"""
    try:
        if not isinstance(asset, dict) or not base_dir:
            return ""
        return os.path.join(base_dir, _norm(asset.get("dest", "")))
    except Exception:
        return ""


def _check_path(asset, base_dir):
    """check 指向的绝对路径（exe 或 files 的 marker）。"""
    try:
        chk = asset.get("check") or {}
        ctype = chk.get("type")
        if ctype == "exe":
            return os.path.join(base_dir, _norm(chk.get("path", "")))
        # files：marker 相对 dest
        return os.path.join(base_dir, _norm(asset.get("dest", "")),
                            _norm(chk.get("marker", "")))
    except Exception:
        return ""


def is_installed(asset, base_dir):
    """是否已下载完整（bool）。任何异常都返回 False。"""
    try:
        if not isinstance(asset, dict) or not base_dir:
            return False
        chk = asset.get("check") or {}
        ctype = chk.get("type")
        p = _check_path(asset, base_dir)
        if not p or not os.path.isfile(p):
            return False
        n = os.path.getsize(p)
        if ctype == "exe":
            return n > _EXE_MIN
        if ctype == "files":
            return n >= int(chk.get("min_bytes", 0))
        return False
    except Exception:
        return False


def installed_size(asset, base_dir):
    """已占用字节数（int）。未装 / 异常返回 0。"""
    try:
        if not isinstance(asset, dict) or not base_dir:
            return 0
        if not is_installed(asset, base_dir):
            return 0
        chk = asset.get("check") or {}
        if chk.get("type") == "exe":
            p = _check_path(asset, base_dir)
            return os.path.getsize(p) if os.path.isfile(p) else 0
        # files：递归统计 dest 目录
        d = installed_path(asset, base_dir)
        total = 0
        for root, _dirs, files in os.walk(d):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
        return total
    except Exception:
        return 0


def missing_assets(base_dir):
    """未装资产列表（保持 ASSETS 顺序）。异常返回 []。"""
    try:
        return [a for a in ASSETS if not is_installed(a, base_dir)]
    except Exception:
        return []


def asset_by_id(aid):
    """按 id 查资产；找不到返回 None。"""
    try:
        for a in ASSETS:
            if a.get("id") == aid:
                return a
    except Exception:
        return None
    return None
