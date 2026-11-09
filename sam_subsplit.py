# -*- coding: utf-8 -*-
r"""SAM 智能子拆分 —— 用 SAM 自动掩码把单个图层按视觉块拆成子层。

与 `l2d_rig_core.subsplit_layer`（连通域启发式）输出同构：
    name / tag / node / depth / path / img(uint8 HxWx4) / bbox(4元组)

设计要点：
  - **懒加载**：torch / SAM 模型只在首次调用时 import + 构建 + 载权重，
    模块级 `_SAM` 单例缓存；顶层不 import torch，不拖慢应用启动。
  - 权重默认在 `models\sam\sam_vit_b_01ec64.pth`；缺失时抛中文错误。
  - SAM 的 SamAutomaticMaskGenerator 自己会把图像 resize 到 1024 长边，
    所以直接把裁剪层的 RGB 喂进去即可，掩码坐标与裁剪层一致。
"""
import os
import sys
import numpy as np

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
# 权重路径（模块级常量，便于测试 monkeypatch / 补装脚本定位）
# mobile_sam（TinyViT，38.8MB）：可整包内置进安装器；vit_b 357MB 下载太重
SAM_WEIGHTS = os.path.join(APP_ROOT, "models", "sam", "mobile_sam.pt")

# 模块级单例缓存：_SAM 为 None 表示尚未加载
_SAM = None
_SAM_DEVICE = None


def _log(verbose, msg):
    if verbose:
        try:
            verbose(msg)
        except Exception:
            pass


def _load_sam(verbose=print):
    """懒加载 SAM vit_b：返回 (model, device)。模块级单例缓存，不重复构建。"""
    global _SAM, _SAM_DEVICE
    if _SAM is not None:
        return _SAM, _SAM_DEVICE
    ckpt = SAM_WEIGHTS
    if not ckpt or not os.path.exists(ckpt):
        raise FileNotFoundError(
            "SAM 权重未下载：%s，请在安装器勾选下载或运行补装脚本" % ckpt)
    # ---- 以下 torch/SAM 只在真正加载时才 import ----
    import torch
    # 注入 see-through 核心库路径（顺带 mock pycocotools、备份 utils/modules 冲突）
    import seethrough_local as SL
    SL._import_core()
    from modules.sam.build_sam import build_sam_vit_t
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # torch 2.6+ 默认 weights_only=True，官方 mobile_sam.pt 是纯 tensor dict 可直载；
    # 若加载报错（含非标对象）再回退 weights_only=False
    try:
        sam = build_sam_vit_t(checkpoint=ckpt)
    except Exception:
        import functools
        _tl = torch.load
        torch.load = functools.partial(_tl, weights_only=False)
        try:
            sam = build_sam_vit_t(checkpoint=ckpt)
        finally:
            torch.load = _tl
    sam.to(dev)
    sam.eval()
    # 兼容 shim：vendored predictor.py 是 HQ-SAM 改版，总给 encoder 传
    # get_interm_embeds 并解包 (features, interm)；vendored TinyViT.forward
    # 本身已返回 (features, interm_embeddings) 二元组、只是不认该 kwarg——
    # 包装吸收参数、原样透传即可（再包一层会变 ((f,i), None) 致 decoder 收到
    # tuple 崩溃，曾踩；`_enc(x)` 调自身也会无限递归，曾踩）。
    if type(sam.image_encoder).__name__ == "TinyViT":
        _enc = sam.image_encoder
        _orig_fwd = _enc.forward

        def _enc_fwd(x, get_interm_embeds=False):
            return _orig_fwd(x)
        _enc.forward = _enc_fwd
    _SAM = sam
    _SAM_DEVICE = dev
    _log(verbose, "SAM mobile_sam 已加载 -> %s" % dev)
    return _SAM, _SAM_DEVICE


def sam_subsplit_layer(layer, min_area=2000, max_parts=6, verbose=print):
    """用 SAM 自动掩码把单个图层按视觉块拆成子层。

    输入：load_layers 产出的层 dict（含 "img": uint8 HxWx4 裁剪 RGBA、
          "bbox": (x0,y0,x1,y1)、"name"）。
    输出：子层 dict 列表（与 subsplit_layer 同构），拆不出（掩码 <2 个有效块）返回 []。

    算法：
      - 取裁剪层 RGB，交给 SamAutomaticMaskGenerator(1024 长边，内部 resize)
      - 每个掩码 ∩ 图层 alpha>16 = 候选域；面积 <min_area 丢弃
      - 升序贪心占地盘：小域（部件）优先认领像素，域间互斥；
        剩余散点并入最大域 -> 子层并集==原层 alpha（100% 覆盖）
      - 最终有效子层 <2 -> 返回 []
      - 按面积降序取前 max_parts；bbox 为 tight（相对全画布，原 bbox 平移），
        img 为该域像素其余 alpha 置 0，name = "%s-sam-%d"
      - 不修改输入 layer；子层浅拷贝继承 tag/node/depth/path 等其余键
      - 首次调用懒加载 SAM 权重（模块级单例缓存）
    """
    img = layer.get("img")
    if img is None or getattr(img, "ndim", 0) != 3 or img.shape[2] < 4 or img.size == 0:
        return []
    h, w = img.shape[:2]
    x0, y0 = int(layer["bbox"][0]), int(layer["bbox"][1])
    alpha = img[:, :, 3] > 16
    if not alpha.any():
        return []

    sam, dev = _load_sam(verbose=verbose)
    import torch                       # 局部 import，顶层不引入
    import cv2
    # SamAutomaticMaskGenerator 内部已 import torch，这里显式保证 no_grad 上下文
    from modules.sam.automatic_mask_generator import SamAutomaticMaskGenerator
    amg = SamAutomaticMaskGenerator(
        sam,
        points_per_side=16,
        pred_iou_thresh=0.75,
        stability_score_thresh=0.82,
        min_mask_region_area=200,
    )
    _log(verbose, "SAM 生成掩码（%s）…" % dev)
    with torch.no_grad():
        anns = amg.generate(img[:, :, :3])
    _log(verbose, "SAM 返回 %d 个掩码" % len(anns))

    # 每个掩码 ∩ 图层 alpha
    parts = []                       # (area, mask bool)
    for ann in anns:
        seg = ann["segmentation"]
        if not isinstance(seg, np.ndarray):
            continue
        if seg.shape != alpha.shape:
            seg = np.asarray(seg.astype(np.uint8))
            if seg.shape != alpha.shape:
                continue
        inter = np.logical_and(seg.astype(bool), alpha)
        area = int(inter.sum())
        if area >= min_area:
            parts.append((area, inter))

    if len(parts) < 2:
        return []
    # SAM 掩码多粒度嵌套（整块包含部件），IoU 去重治不了包含关系。
    # 改"升序贪心占地盘"：小掩码（部件）优先认领像素，大块只拿剩余——
    # 保证子层互斥且并集==原层 alpha（覆盖 100%，曾踩：只 IoU 去重致覆盖 222%）
    parts.sort(key=lambda t: t[0])
    claimed = np.zeros_like(alpha, dtype=bool)
    kept = []                        # (area, mask) 互斥域
    for area, m in parts:
        fresh = m & ~claimed
        fa = int(fresh.sum())
        if fa >= min_area:
            kept.append((fa, fresh))
            claimed |= m
    # 未认领的散点归入最大块（保持并集完整）
    rest = alpha & ~claimed
    ra = int(rest.sum())
    if kept and ra > 0:
        big_i = max(range(len(kept)), key=lambda i: kept[i][0])
        merged = kept[big_i][1] | rest
        kept[big_i] = (int(merged.sum()), merged)
    elif ra >= min_area:
        kept.append((ra, rest))
    if len(kept) < 2:
        return []
    kept.sort(key=lambda t: -t[0])
    kept = kept[:max_parts]

    base = {k: v for k, v in layer.items() if k != "img"}
    out = []
    for k, (area, m) in enumerate(kept, start=1):
        ys, xs = np.where(m)
        cx0, cx1 = int(xs.min()), int(xs.max()) + 1
        cy0, cy1 = int(ys.min()), int(ys.max()) + 1
        cx0 = max(0, min(w, cx0)); cx1 = max(cx0 + 1, min(w, cx1))
        cy0 = max(0, min(h, cy0)); cy1 = max(cy0 + 1, min(h, cy1))
        sub = np.where(m[:, :, None], img, 0).astype(np.uint8)[cy0:cy1, cx0:cx1]
        item = dict(base)
        item["name"] = "%s-sam-%d" % (layer["name"], k)
        item["img"] = np.ascontiguousarray(sub)
        item["bbox"] = (x0 + cx0, y0 + cy0, x0 + cx1, y0 + cy1)
        out.append(item)
    return out


# 模块底部不 import torch：所有重依赖都在函数内部按需 import。
