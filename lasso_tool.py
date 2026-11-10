# -*- coding: utf-8 -*-
"""多边形套索选取（L1 基础版）

用途：在画布上画一个多边形，把**当前选中图层**裁剪成"只保留选区内的像素"。
像 PS 的多边形套索 + 剪切。

实现原理（引擎零改动）：
    图层的实际内容 = "img"（按 bbox 裁剪的 uint8 RGBA）+ "bbox"（摆放坐标）。
    选区是一个多边形 -> 用 cv2.fillPoly 生成 0/255 掩码 -> 与 img 的 alpha 相乘，
    再把 img 裁到选区与 bbox 的交集、bbox 相应收紧。渲染/导出（PSD·AE·JSON）
    全部自动正确——它们只认 img+bbox 这两个字段。

坐标约定：
    多边形点集用**图像全分辨率坐标**（与 layer['bbox'] 同一坐标系），
    不是画布 widget 坐标——画布显示的是 LOD 帧，缩放/平移都不影响选取结果。

不做什么（L1 范围）：
    - 不做加选/减选（多选区布尔运算）
    - 不做羽化
    - 不做选区蚂蚁线（画布侧画的是蓝色虚线折线）
    - 不做"还原原图"（裁剪后原像素已丢弃）
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


def clip_layer_by_polygon(layer, points):
    """把图层裁剪到多边形选区内。返回新 layer dict，或 None（选区无效/无交集）。

    - 不修改入参 layer（返回新 dict），调用方负责替换 + 压历史
    - bbox 收紧到选区与原 bbox 的交集，img 同步裁剪
    - alpha 与掩码相乘：选区外的像素变全透明
    """
    img = layer.get("img")
    if img is None or getattr(img, "ndim", 0) != 3 or img.shape[2] < 4:
        return None
    bbox = layer.get("bbox")
    if not bbox:
        return None
    x0, y0, x1, y1 = [int(v) for v in bbox]
    h, w = img.shape[:2]
    # 掩码画在图层自己的坐标系（bbox 左上角为原点）
    local = [(px - x0, py - y0) for px, py in points]
    mask = polygon_mask(local, w, h)
    if mask is None:
        return None
    keep = mask > 0
    if not keep.any():
        return None                                  # 选区与图层无交集
    ys, xs = np.nonzero(keep)
    nx0, ny0 = int(xs.min()), int(ys.min())
    nx1, ny1 = int(xs.max()) + 1, int(ys.max()) + 1
    sub = img[ny0:ny1, nx0:nx1].copy()
    m2 = mask[ny0:ny1, nx0:nx1]
    # alpha 与掩码相乘（保留原 alpha 的软边，乘 0/255 掩码）
    sub[:, :, 3] = ((sub[:, :, 3].astype(np.uint16) * m2.astype(np.uint16)) // 255).astype(np.uint8)
    if not (sub[:, :, 3] > 0).any():
        return None                                  # 裁完全透明 -> 视为无有效选区
    new = dict(layer)
    new["img"] = np.ascontiguousarray(sub)
    new["bbox"] = (x0 + nx0, y0 + ny0, x0 + nx1, y0 + ny1)
    return new
