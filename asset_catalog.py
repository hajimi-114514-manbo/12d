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
  - 显式 url 资产可加 mirrors=[前缀...]：URL 候选 = 官方直连 + 前缀镜像（task-35）
"""
import os
import sys

# 1MB 阈值（exe 完整性判定）
_EXE_MIN = 1024 * 1024

# 下载镜像（与安装器一致：国内镜像优先，官方兜底）
HF_MIRRORS = ["https://hf-mirror.com/", "https://huggingface.co/"]
GH_MIRROR = "https://ghfast.top/"

# 模型仓库 -> 文件清单（相对 repo 根）。
# 这些文件是 HF 上 24yearsold 的公开模型，**不需要我们自己上传**，
# 直接用 hf-mirror 镜像地址下载即可（已实测 200 OK）。
_LAYERDIFF_FILES = [
    "LICENSE",
    "NOTICE",
    "README.md",
    "model_index.json",
    "scheduler/scheduler_config.json",
    "text_encoder/config.json",
    "text_encoder/model.safetensors",
    "text_encoder_2/config.json",
    "text_encoder_2/model.safetensors",
    "tokenizer/merges.txt",
    "tokenizer/special_tokens_map.json",
    "tokenizer/tokenizer_config.json",
    "tokenizer/vocab.json",
    "tokenizer_2/merges.txt",
    "tokenizer_2/special_tokens_map.json",
    "tokenizer_2/tokenizer_config.json",
    "tokenizer_2/vocab.json",
    "trans_vae/config.json",
    "trans_vae/diffusion_pytorch_model.safetensors",
    "unet/config.json",
    "unet/diffusion_pytorch_model.safetensors",
    "vae/config.json",
    "vae/diffusion_pytorch_model.safetensors",
]
_MARIGOLD_FILES = [
    "LICENSE",
    "NOTICE",
    "README.md",
    "model_index.json",
    "scheduler/scheduler_config.json",
    "text_encoder/config.json",
    "text_encoder/model.safetensors",
    "tokenizer/merges.txt",
    "tokenizer/special_tokens_map.json",
    "tokenizer/tokenizer_config.json",
    "tokenizer/vocab.json",
    "unet/config.json",
    "unet/diffusion_pytorch_model.safetensors",
    "vae/config.json",
    "vae/diffusion_pytorch_model.safetensors",
]

ASSETS = [
    {
        "id": "runtime",
        "name": "内置 Python 运行时",
        "desc": "无需自己装 Python，装完即可启动（含程序运行所需 CPU 版推理库，约 420MB）",
        "kind": "runtime",
        "size": 416261511,
        # 运行时是我们自己打包的，走 GitHub Release 资产
        # v3.1.0 起含 CPU 推理栈（torch 2.11.0+cpu + diffusers 全家桶）
        "url": ("https://github.com/hajimi-114514-manbo/12d/releases/"
                "download/v3.1.1/runtime.zip"),
        "dest": "runtime",
        # v3.1.0：判定升级——旧运行时（无 torch）必须可被识别为"未装"，
        # 否则老用户永远拿不到内置 CPU 推理栈
        "check": {"type": "files",
                  "marker": "Lib/site-packages/torch/__init__.py",
                  "min_bytes": 100000},
    },
    {
        "id": "model_layerdiff",
        "name": "分层模型 LayerDiff3D (nf4)",
        "desc": "拆分图层必需，约 3.4GB",
        "kind": "model",
        "repo": "24yearsold/seethroughv0.0.2_layerdiff3d_nf4",
        "files": _LAYERDIFF_FILES,
        "size": 3764505415,
        "url": "",
        "dest": "models/24yearsold/seethroughv0.0.2_layerdiff3d_nf4",
        "check": {"type": "files",
                  "marker": "unet/diffusion_pytorch_model.safetensors",
                  "min_bytes": 2700000000},
    },
    {
        "id": "model_marigold",
        "name": "深度模型 Marigold (nf4)",
        "desc": "生成深度信息，拆分图层必需，约 1.8GB",
        "kind": "model",
        "repo": "24yearsold/seethroughv0.0.1_marigold_nf4",
        "files": _MARIGOLD_FILES,
        "size": 1928811596,
        "url": "",
        "dest": "models/24yearsold/seethroughv0.0.1_marigold_nf4",
        "check": {"type": "files",
                  "marker": "unet/diffusion_pytorch_model.safetensors",
                  "min_bytes": 1400000000},
    },
    {
        "id": "model_sam",
        "name": "MobileSAM（精细拆分）",
        "desc": "智能分割辅助，约 40MB",
        "kind": "model",
        "size": 40728226,
        # 官方 MobileSAM 权重（与安装包曾内置的文件 SHA256 完全一致，已实测）
        "url": ("https://hf-mirror.com/dhkim2810/MobileSAM/resolve/main/"
                "mobile_sam.pt"),
        # 单文件 url 形态：dest 是存放目录，落盘文件名由 url 末段决定
        "dest": "models/sam",
        "check": {"type": "files", "marker": "mobile_sam.pt",
                  "min_bytes": 30000000},
    },
    {
        "id": "cuda_pack",
        "name": "CUDA 加速包（N 卡提速）",
        "desc": "N 卡拆图必备：装完重启后本地引擎走 CUDA nf4 快通道（约 14 分钟/张）。"
                "约 1.9GB 分 3 卷；A 卡不适用（拆图请走本机 ComfyUI+ROCm）",
        "kind": "accel",
        "mode": "parts",
        "size": 2007028506,
        # 分卷：cuda_pack.zip（2007028506 B，deflate1，torch cu130 + bitsandbytes）
        # 按 669009502 B 切 3 段纯字节卷；构建脚本 D:\scripts\temp\build_cuda_pack.py
        "parts": [
            {"url": ("https://github.com/hajimi-114514-manbo/12d/releases/"
                     "download/v3.1.1/cuda_pack.zip.part01"),
             "size": 669009502},
            {"url": ("https://github.com/hajimi-114514-manbo/12d/releases/"
                     "download/v3.1.1/cuda_pack.zip.part02"),
             "size": 669009502},
            {"url": ("https://github.com/hajimi-114514-manbo/12d/releases/"
                     "download/v3.1.1/cuda_pack.zip.part03"),
             "size": 669009502},
        ],
        "zip_sha256": ("7bf280ac8775d2836019993487e5acc1e6e40a3134f06f6f0f95"
                       "85e6f3f39a67"),
        "target": "site-packages",      # 相对 runtime 根；见 parts_target_dir
        "pre_delete": ["torch", "bitsandbytes"],
        # 已装判定：site-packages/torch/lib 下存在 cudnn64_*.dll（纯文件探测，
        # 绝不 import torch——那会加载 DLL 污染本进程）
        "done_marker": {"dir": "torch/lib", "prefix": "cudnn64_"},
        "check": {"type": "parts"},
    },
    {
        # H3-c（task-64）：PySide6-WebEngine 可选资产（J1 路线 b）。
        # 官方 pypi wheel 单包（wheel 本身就是 zip，解压即安装）；
        # sha256 钉死 pypi files.pythonhosted 官方 digest（2026-11-14 实测）。
        # 落地 = runtime_root\Lib\site-packages（与 cuda_pack 同款 target 语义）；
        # 已装判定 = site-packages\PySide6\Qt6WebEngineCore.dll 纯文件探测
        # （绝不 import QtWebEngineWidgets——那会在资产查询路径上拉起 Chromium）。
        # 注意：wheel 版本必须与整合包内 PySide6 严格同源（6.12.0.140 ↔ 6.12.0），
        # 升级 PySide6 时本条目必须同步换版重钉 sha256。
        "id": "webengine_pack",
        "name": "实时预览能力包（WebEngine，约130MB）",
        "desc": "「实时预览」需要的 PySide6-WebEngine 6.12.0.140 官方组件"
                "（Chromium 内核）。只做 Live2D 实时预览，不装不影响其他功能。",
        "kind": "python_pack",
        "mode": "wheel",
        "size": 131697708,
        "url": ("https://files.pythonhosted.org/packages/21/0d/260c7c286f2d4e8a"
                "49c099152a385414a872f022117f937824ca0a3f38e5/"
                "pyside6_webengine-6.12.0.140-cp310-abi3-win_amd64.whl"),
        # files.pythonhosted 直连在国内偶慢；tsinghua 镜像同 sha（pypi 同源），
        # 前缀式镜像：官方直连在前、镜像兜底在后
        "mirrors": ["https://pypi.tuna.tsinghua.edu.cn/"],
        "zip_sha256": ("f9b61b1d457a591ee590a6b80515f25a3e61e2576a2a0a70566380"
                       "f9d9de4ab0"),
        "target": "site-packages",
        # wheel 无 pre_delete（PySide6 已装 Essentials，WebEngine 是纯增量；
        # 重复安装 = 解压覆盖同版本文件，幂等安全）
        "pre_delete": [],
        # 已装判定：site-packages\PySide6\Qt6WebEngineCore.dll 在场
        # （wheel 内顶层 PySide6/ 解开即落 site-packages\PySide6\）
        "done_marker": {"dir": "PySide6", "prefix": "Qt6WebEngineCore"},
        "check": {"type": "parts"},
    },
    {
        "id": "webgal_runtime",
        "name": "WebGAL 引擎运行时（可选，约50MB）",
        "desc": "「导出可玩游戏」需要的 WebGAL 官方播放引擎（v4.6.6，网页版）。"
                "不进安装包，按需下载一次后离线可用；不用导出功能就不用装。",
        "kind": "runtime_pack",
        # 实测 Content-Length（2026-11-13，官方 release 直连）
        "size": 52837011,
        # 官方 release 原始包（web = 纯静态前端 zip）；sha256 钉死官方 digest，
        # 下载器单包模式解压前校验（fail-closed，坏包删除报错）
        "url": ("https://github.com/OpenWebGAL/WebGAL/releases/"
                "download/4.6.6/WebGAL-4.6.6-web.zip"),
        "zip_sha256": ("d8011933005f62397b3d1ea6cd14b8a7f58ce34a390000"
                       "afa48f12a2d8f0349a"),
        # 官方 web 包解包常见单顶层目录；asset_downloader._extract_zip
        # 的"单顶层自动剥离"保证 index.html 直接落在 dest 根
        "dest": "templates/webgal_runtime",
        "mirrors": [GH_MIRROR],   # 前缀式镜像：镜像不通自动回落官方直连
        "check": {"type": "files", "marker": "index.html", "min_bytes": 1},
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


def runtime_root(base_dir):
    """CUDA 包要落地的 Python 运行时根（其 Lib\\site-packages 即替换目标）。
    优先 MUPPET_RUNTIME_ROOT（测试/特殊部署）；
    其次 <base_dir>\\runtime（装机部署的内置运行时）；
    再回退 sys.executable 自身所在（开发机直跑场景）。"""
    try:
        r = os.environ.get("MUPPET_RUNTIME_ROOT", "").strip()
        if r and os.path.isdir(r):
            return r
        cand = os.path.join(base_dir or "", "runtime")
        if os.path.isdir(os.path.join(cand, "Lib", "site-packages")):
            return cand
        exe = os.path.dirname(sys.executable)
        if os.path.isdir(os.path.join(exe, "Lib", "site-packages")):
            return exe
        return cand
    except Exception:
        return ""


def parts_target_dir(asset, base_dir):
    """parts 资产的实际落地目录。target="site-packages" 是语义糖，
    展开为 runtime_root\\Lib\\site-packages。"""
    try:
        t = (asset.get("target") or "site-packages").strip()
        rr = runtime_root(base_dir)
        if not rr:
            return ""
        if t == "site-packages":
            return os.path.join(rr, "Lib", "site-packages")
        return os.path.join(rr, _norm(t))
    except Exception:
        return ""


def _parts_installed(asset, base_dir):
    """done_marker 纯文件探测：target/dir 下存在 prefix 开头的文件。"""
    try:
        dm = asset.get("done_marker") or {}
        d = os.path.join(parts_target_dir(asset, base_dir),
                         _norm(dm.get("dir", "")))
        if not os.path.isdir(d):
            return False
        pref = dm.get("prefix", "")
        return any(f.startswith(pref) for f in os.listdir(d))
    except Exception:
        return False


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
        # parts/wheel 两种模式共用 done_marker 纯文件探测
        # （parts=CUDA 分卷，wheel=python_pack 单包；都是解压进 site-packages）
        if asset.get("mode") in ("parts", "wheel"):
            return _parts_installed(asset, base_dir)
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
        if asset.get("mode") in ("parts", "wheel"):
            if not _parts_installed(asset, base_dir):
                return 0
            # 统计标志目录（避免 site-packages 全量 walk 太慢）：
            # parts=CUDA -> torch\；wheel=WebEngine -> PySide6\
            d = os.path.join(parts_target_dir(asset, base_dir),
                             _norm((asset.get("done_marker") or {})
                                   .get("dir", "")))
            total = 0
            for root, _dirs, files in os.walk(d):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except Exception:
                        pass
            return total
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


def asset_urls(asset):
    """资产 -> [(相对文件路径, [候选URL...]), ...]。

    两种资产形态：
      - 有 "files"（模型）：逐文件下载，路径相对 dest，URL 由 repo + 镜像拼出
      - 只有 "url"（zip 包）：单文件下载，相对路径为 ""，解压到 dest
    返回空列表表示未配置。
    """
    try:
        if not isinstance(asset, dict):
            return []
        files = asset.get("files") or []
        repo = asset.get("repo") or ""
        if files and repo:
            repo_path = repo.split("/", 1)[-1] if "/" in repo else repo
            out = []
            for f in files:
                urls = [m + repo + "/resolve/main/" + f for m in HF_MIRRORS]
                out.append((f, urls))
            return out
        parts = asset.get("parts") or []
        if parts:
            # 分卷资产：GitHub 直连 + ghfast 镜像回退
            out = []
            for i, p in enumerate(parts):
                u = (p.get("url") or "").strip()
                if u:
                    out.append(("part%02d" % (i + 1), [u, GH_MIRROR + u]))
            return out
        url = (asset.get("url") or "").strip()
        if url:
            urls = [url]
            # 显式单包资产的镜像（task-35 最小扩展）："mirrors" 列表元素
            # 以 "/" 结尾视为加速前缀（拼在完整 URL 前，同 cuda_pack parts 的
            # [url, GH_MIRROR+url] 语义），否则视为完整备用 URL。
            # 顺序：官方直连在前、镜像兜底在后；未声明 mirrors 的资产行为不变。
            for m in (asset.get("mirrors") or []):
                m = str(m or "").strip()
                if not m:
                    continue
                cand = m + url if m.endswith("/") else m
                if cand not in urls:
                    urls.append(cand)
            return [("", urls)]
        return []
    except Exception:
        return []


def asset_files_missing(asset, base_dir):
    """模型类资产：返回已存在且大小>0 的文件 vs 缺失文件。

    返回 (present, missing)，元素为相对路径字符串。非模型资产返回 ([], [])。
    """
    try:
        files = asset.get("files") or []
        if not files:
            return [], []
        root = installed_path(asset, base_dir)
        present, missing = [], []
        for f in files:
            p = os.path.join(root, _norm(f))
            try:
                if os.path.isfile(p) and os.path.getsize(p) > 0:
                    present.append(f)
                else:
                    missing.append(f)
            except Exception:
                missing.append(f)
        return present, missing
    except Exception:
        return [], list((asset or {}).get("files") or [])
