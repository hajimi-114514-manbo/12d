# -*- coding: utf-8 -*-
"""多边形套索选取（L1 + L2 实用版）

用途：在画布上画多边形，把**当前选中图层**裁剪成"只保留选区内的像素"。
像 PS 的多边形套索 + 剪切。

实现原理（引擎零改动）：
    图层的实际内容 = "img"（按 bbox 裁剪的 uint8 RGBA）+ "bbox"（摆放坐标）。
    选区是一个多边形 -> 用 cv2.fillPoly 生成 0/255 掩码 -> 与 img 的 alpha 相乘，
    再把 img 裁到选区与 bbox 的交集、bbox 相应收紧。渲染/导出（PSD·AE·JSON）
    全部自动正确——它们只认 img+bbox 这两个字段。

L2 新增：
    - **多选区布尔**：加选(union) / 减选(subtract)，最终掩码 = 所有加选区并集 - 所有减选区
    - **羽化**：对掩码做高斯模糊，得到 0..255 的软边 alpha（PS 的"羽化"）
    - **保留原图**：裁剪时把原始 img 另存一份，供"还原"与"在原图上多次选取"

坐标约定：
    多边形点集用**图像全分辨率坐标**（与 layer['bbox'] 同一坐标系），
    不是画布 widget 坐标——画布显示的是 LOD 帧，缩放/平移都不影响选取结果。

L1 未做（可后续加）：套索自动吸附边缘、跨图层布尔运算、选区蚂蚁线（画布侧画的是蓝色虚线）。
"""
import numpy as np

try:
    import cv2
except Exception:                                   # pragma: no cover
    cv2 = None


def polygon_mask(points, w, h):
    """多边形 -> 0/255 uint8 掩码（图像坐标，w/h 为掩码宽高）。

    points: [(x, y), ...] 图像坐标；w/h: 掩码尺寸
    返回 None 表示参数无效（点数不足 / 无 cv2）。
    """
    if cv2 is None or not points or len(points) < 3 or w <= 0 or h <= 0:
        return None
    pts = np.array([[int(round(x)), int(round(y))] for x, y in points], np.int32)
    mask = np.zeros((h, w), np.uint8)
    cv2.fillPoly(mask, [pts], 255)
    return mask


def combine_masks(add_masks, sub_masks, w, h, feather=0):
    """多选区布尔合并：并集(加选) - 并集(减选)，可选羽化。

    add_masks/sub_masks: 0/255 uint8 掩码列表
    feather: 羽化半径(像素)，0 = 不羽化。羽化用高斯模糊得到软边 alpha。
    返回 uint8 掩码（0..255，软边时中间值）。
    """
    if not add_masks:
        return None
    acc = np.zeros((h, w), np.uint8)
    for m in add_masks:
        if m is not None:
            acc = np.maximum(acc, m)
    for m in sub_masks or []:
        if m is not None:
            acc = cv2.subtract(acc, m)
    if feather and feather > 0 and cv2 is not None:
        k = int(feather) * 2 + 1
        acc = cv2.GaussianBlur(acc, (k, k), feather)
    return acc


def invert_mask(mask):
    """掩码取反（0<->255），用于"减去选区"（PS 的剪切语义：
    保留选区**外**的内容，移除选区内的）。"""
    if mask is None:
        return None
    return (255 - mask).astype(np.uint8)


def clip_layer_by_mask(layer, mask, origin=(0, 0), invert=False):
    """按掩码裁剪图层。返回新 layer dict，或 None（无有效内容）。

    - mask 是图层**自身坐标系**（相对 bbox 左上角）的 0..255 掩码
    - origin: 掩码对应的原点在 bbox 坐标系里的偏移（本函数按 bbox 裁剪，
      传入 (0,0) 表示掩码与 bbox 同尺寸）
    - invert=True：取反掩码 —— 用于**剪切**（保留选区外、去掉选区内）
    - 不修改入参 layer（返回新 dict）
    """
    img = layer.get("img")
    if img is None or getattr(img, "ndim", 0) != 3 or img.shape[2] < 4:
        return None
    bbox = layer.get("bbox")
    if not bbox:
        return None
    x0, y0, x1, y1 = [int(v) for v in bbox]
    h, w = img.shape[:2]
    if mask.shape[:2] != (h, w):
        return None
    if invert:
        mask = invert_mask(mask)
    keep = mask > 0
    if not keep.any():
        return None                                  # 选区与图层无交集
    ys, xs = np.nonzero(keep)
    nx0, ny0 = int(xs.min()), int(ys.min())
    nx1, ny1 = int(xs.max()) + 1, int(ys.max()) + 1
    sub = img[ny0:ny1, nx0:nx1].copy()
    m2 = mask[ny0:ny1, nx0:nx1]
    # alpha 与掩码相乘（保留原 alpha 的软边；羽化的中间值自然形成软过渡）
    sub[:, :, 3] = ((sub[:, :, 3].astype(np.uint16) * m2.astype(np.uint16)) // 255).astype(np.uint8)
    if not (sub[:, :, 3] > 0).any():
        return None                                  # 裁完全透明 -> 视为无有效选区
    new = dict(layer)
    new["img"] = np.ascontiguousarray(sub)
    new["bbox"] = (x0 + nx0, y0 + ny0, x0 + nx1, y0 + ny1)
    return new


def clip_layer_by_polygon(layer, points, feather=0):
    """L1 兼容入口：单多边形裁剪（feather=0 时与原实现一致）。"""
    img = layer.get("img")
    if img is None or getattr(img, "ndim", 0) != 3 or img.shape[2] < 4:
        return None
    bbox = layer.get("bbox")
    if not bbox:
        return None
    x0, y0, x1, y1 = [int(v) for v in bbox]
    h, w = img.shape[:2]
    local = [(px - x0, py - y0) for px, py in points]
    mask = polygon_mask(local, w, h)
    if mask is None:
        return None
    if feather and feather > 0:
        mask = combine_masks([mask], [], w, h, feather)
    return clip_layer_by_mask(layer, mask)


def extract_selection(layer, selections, feather=0):
    """提取选区内的像素（PS 的"复制选区内容"）。

    返回 (rgba, x, y)：
        rgba = 选区内容（uint8 HxWx4，尺寸=选区外接矩形）
        x, y = 该内容在画布全分辨率坐标里的左上角位置
    与 clip_layer_by_mask 的区别：这里**不裁原层**，只是"取一块出来"，
    用于复制到剪贴板 / 粘贴成新图层。
    """
    img = layer.get("img")
    if img is None or getattr(img, "ndim", 0) != 3 or img.shape[2] < 4:
        return None
    bbox = layer.get("bbox")
    if not bbox:
        return None
    x0, y0, x1, y1 = [int(v) for v in bbox]
    h, w = img.shape[:2]
    add_masks, sub_masks = [], []
    for sel in selections or []:
        pts = sel.get("points") or []
        op = sel.get("op", "add")
        local = [(px - x0, py - y0) for px, py in pts]
        m = polygon_mask(local, w, h)
        if m is None:
            continue
        (sub_masks if op == "sub" else add_masks).append(m)
    mask = combine_masks(add_masks, sub_masks, w, h, feather=feather)
    if mask is None:
        return None
    keep = mask > 0
    if not keep.any():
        return None
    ys, xs = np.nonzero(keep)
    nx0, ny0 = int(xs.min()), int(ys.min())
    nx1, ny1 = int(xs.max()) + 1, int(ys.max()) + 1
    sub = img[ny0:ny1, nx0:nx1].copy()
    m2 = mask[ny0:ny1, nx0:nx1]
    sub[:, :, 3] = ((sub[:, :, 3].astype(np.uint16) * m2.astype(np.uint16)) // 255).astype(np.uint8)
    if not (sub[:, :, 3] > 0).any():
        return None
    return np.ascontiguousarray(sub), x0 + nx0, y0 + ny0

