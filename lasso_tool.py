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


def expand_curve_points(points, samples=14):
    """控制点 -> Catmull-Rom 样条加密点（曲线套索 / PS 曲线套索的核心）。

    points 视为**闭合**多边形（套索语义），首尾环绕插值；
    每段之间插入 samples 个采样点。返回的密集点集仍走 polygon_mask，
    填出的选区边界就是光滑曲线。控制点 <= 2 时原样返回（无法成曲线）。
    """
    n = len(points)
    if n < 3:
        return list(points)
    arr = np.array([[float(x), float(y)] for x, y in points], np.float64)
    out = []
    for i in range(n):
        p0 = arr[(i - 1) % n]
        p1 = arr[i]
        p2 = arr[(i + 1) % n]
        p3 = arr[(i + 2) % n]
        for k in range(samples):
            t = k / float(samples)
            t2, t3 = t * t, t * t * t
            # 标准均匀 Catmull-Rom 基
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t +
                       (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 +
                       (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t +
                       (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 +
                       (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append((float(x), float(y)))
    return out


def selection_points(sel):
    """取一个选区 dict 的**生效点集**：curve=True 时做样条加密。

    所有掩码生成处统一走这里，保证绘制预览 / 复制 / 剪切 / 裁剪
    用的是同一份边界（曾出现预览是曲线、掩码还是折线的不一致）。
    """
    pts = sel.get("points") or []
    if sel.get("curve"):
        return expand_curve_points(pts)
    return pts


def combine_masks(add_masks, sub_masks, w, h, feather=0, invert=False):
    """多选区布尔合并：并集(加选) - 并集(减选)，可选羽化、可选反选。

    add_masks/sub_masks: 0/255 uint8 掩码列表
    feather: 羽化半径(像素)，0 = 不羽化。羽化用高斯模糊得到软边 alpha。
    invert: True = 反选（PS 的"选择反向"，Ctrl+Shift+I）：
            选区边界之外全部选中。羽化在反选**之后**做，边缘才自然。
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
    if invert:
        acc = invert_mask(acc)
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


def extract_selection(layer, selections, feather=0, invert=False):
    """提取选区内的像素（PS 的"复制选区内容"）。

    返回 (rgba, x, y)：
        rgba = 选区内容（uint8 HxWx4，尺寸=选区外接矩形）
        x, y = 该内容在画布全分辨率坐标里的左上角位置
    与 clip_layer_by_mask 的区别：这里**不裁原层**，只是"取一块出来"，
    用于复制到剪贴板 / 粘贴成新图层。
    invert=True 时取选区之外的内容（配合"反选"）。
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
        pts = selection_points(sel)
        op = sel.get("op", "add")
        local = [(px - x0, py - y0) for px, py in pts]
        m = polygon_mask(local, w, h)
        if m is None:
            continue
        (sub_masks if op == "sub" else add_masks).append(m)
    mask = combine_masks(add_masks, sub_masks, w, h, feather=feather, invert=invert)
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


def inpaint_holes(img_rgba, radius=3, method="telea", holes=None):
    """对 RGBA 图里的透明洞做内容感知填充（Mocha Remove 模块的简化版）。

    - img_rgba: uint8 HxWx4
    - holes: 可选 uint8 掩码（洞=255，与 img_rgba 同尺寸）。传入时**直接用它**
      作为 cv2.inpaint 的掩码，不再按 alpha==0 找洞——调用方可先用连通域区分
      "被包围的洞"与"bbox 外的外部透明区"，只把要填的洞喂进来，外部区绝不填。
      holes=None 时保持旧行为：洞 = alpha==0 的像素。
    - 返回新的 uint8 HxWx4：洞内 RGB 用 cv2.inpaint 从周边填充，alpha 置 255
    - 洞外像素必须逐字节不变（holes 模式下，掩码外的透明像素也保持不动）
    - 无洞 / 全洞 / 洞面积过小(<4px) 时原样返回副本
    - radius 传给 cv2.inpaint；method "telea"|"ns"

    实现要点：cv2.inpaint 需要 BGR uint8 + 掩码(洞=255)；先把 RGB 通道和
    alpha 分开，inpaint 只填 RGB，alpha 直接置 255（洞边界自然过渡可不管，
    调用方会先做闭运算决定洞范围）。
    """
    if cv2 is None:
        return np.array(img_rgba, copy=True)
    arr = np.asarray(img_rgba)
    if arr.ndim != 3 or arr.shape[2] != 4:
        return np.array(img_rgba, copy=True)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    out = arr.copy()
    if holes is None:
        hole = arr[:, :, 3] == 0
    else:
        hm = np.asarray(holes)
        if hm.ndim != 2 or hm.shape != arr.shape[:2]:
            return out                      # 掩码尺寸不符：原样返回副本
        if hm.dtype != np.uint8:
            hm = np.clip(hm, 0, 255).astype(np.uint8)
        hole = hm > 0
    if not hole.any():
        return out
    h, w = arr.shape[:2]
    n = int(hole.sum())
    if n >= h * w or n < 4:
        # 全洞 / 洞太小：原样返回副本
        return out
    # cv2.inpaint 要 BGR uint8 + 单通道掩码(洞=255)
    bgr = arr[:, :, :3][:, :, ::-1].copy()
    mask = hole.astype(np.uint8) * 255
    flags = cv2.INPAINT_NS if str(method).lower() == "ns" else cv2.INPAINT_TELEA
    filled = cv2.inpaint(bgr, mask, max(1, int(radius)), flags)
    # 只把洞内 RGB 换成填充结果（BGR->RGB），alpha 置 255
    out[:, :, :3] = np.where(hole[:, :, None], filled[:, :, ::-1], out[:, :, :3])
    out[:, :, 3] = np.where(hole, 255, out[:, :, 3]).astype(np.uint8)
    return out

