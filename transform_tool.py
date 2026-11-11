# -*- coding: utf-8 -*-
"""自由变换（PS 的 Ctrl+T）纯函数层。

状态 = 中心 + 半宽半高 + 旋转角（图像全分辨率坐标）：
    {"cx", "cy", "hw", "hh", "ang"}
好处：8 个控制点、命中判定、仿射求解都从这五个数直接算，且**始终是仿射**
（矩形经仿射=平行四边形，cv2.getAffineTransform 三点对可）。

与图层的衔接（引擎零改动）：
    图层内容 = img（bbox 裁剪 RGBA）+ bbox。变换就是
    用 cv2.warpAffine 把原 img 映到目标平行四边形，再按 alpha 收紧 bbox。
    渲染 / PSD·AE·JSON 导出全部自动正确。

坐标一律图像全分辨率坐标（与 layer['bbox'] 同系），不是 widget 坐标。
"""
import math
import numpy as np

try:
    import cv2
except Exception:                                   # pragma: no cover
    cv2 = None

# 控制点标识：四角 + 四边中点
HANDLES = [("tl", -1, -1), ("tr", 1, -1), ("br", 1, 1), ("bl", -1, 1),
           ("t", 0, -1), ("r", 1, 0), ("b", 0, 1), ("l", -1, 0)]


def state_from_bbox(bbox):
    """图层 bbox -> 初始变换状态（不缩放不旋转）"""
    x0, y0, x1, y1 = [float(v) for v in bbox]
    return {"cx": (x0 + x1) / 2.0, "cy": (y0 + y1) / 2.0,
            "hw": (x1 - x0) / 2.0, "hh": (y1 - y0) / 2.0, "ang": 0.0}


def corners(st):
    """状态 -> 四角（图像坐标）[tl, tr, br, bl]"""
    c, s = math.cos(st["ang"]), math.sin(st["ang"])
    hw, hh = st["hw"], st["hh"]
    def loc(lx, ly):
        return (st["cx"] + lx * c - ly * s, st["cy"] + lx * s + ly * c)
    return [loc(-hw, -hh), loc(hw, -hh), loc(hw, hh), loc(-hw, hh)]


def handle_points(st):
    """8 个控制点的图像坐标 [(name, x, y), ...]"""
    pts = corners(st)
    tl, tr, br, bl = pts
    mid = lambda a, b: ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    return [("tl", tl[0], tl[1]), ("tr", tr[0], tr[1]),
            ("br", br[0], br[1]), ("bl", bl[0], bl[1]),
            ("t", mid(tl, tr)[0], mid(tl, tr)[1]),
            ("r", mid(tr, br)[0], mid(tr, br)[1]),
            ("b", mid(bl, br)[0], mid(bl, br)[1]),
            ("l", mid(tl, bl)[0], mid(tl, bl)[1])]


def to_local(st, x, y):
    """图像坐标 -> 变换框自身坐标系（以中心为原点、未旋转）"""
    dx, dy = x - st["cx"], y - st["cy"]
    c, s = math.cos(-st["ang"]), math.sin(-st["ang"])
    return (dx * c - dy * s, dx * s + dy * c)


def hit(st, x, y, thr):
    """命中测试 -> ("handle", name) / ("move", None) / ("rotate", None) / None
    thr：图像坐标下的命中半径（调用方按缩放换算成 widget 像素）"""
    for name, hx, hy in handle_points(st):
        if (hx - x) ** 2 + (hy - y) ** 2 <= thr * thr:
            return ("handle", name)
    lx, ly = to_local(st, x, y)
    if abs(lx) <= st["hw"] and abs(ly) <= st["hh"]:
        return ("move", None)
    # 框外一点 = 旋转（PS 自由变换：鼠标移到框角外侧出现弧形箭头）
    if abs(lx) <= st["hw"] * 1.35 and abs(ly) <= st["hh"] * 1.35:
        return ("rotate", None)
    return None


def drag_handle(st, name, x, y, keep_aspect=False):
    """拖动某个控制点 -> 新状态。
    角点：对侧角固定（PS 行为），keep_aspect(Shift) 保持原宽高比。
    边点：只改一个轴，对侧边固定。"""
    sx = next(s for n, s, _ in HANDLES if n == name)
    sy = next(t for n, _, t in HANDLES if n == name)
    lx, ly = to_local(st, x, y)
    hw, hh, ang, c, s = st["hw"], st["hh"], st["ang"], math.cos(st["ang"]), math.sin(st["ang"])
    if sx != 0 and sy != 0:                    # 角点
        ax, ay = -sx * hw, -sy * hh            # 对侧角（局部坐标）
        nhw = max(1.0, (lx - ax) * sx / 2.0)
        nhh = max(1.0, (ly - ay) * sy / 2.0)
        if keep_aspect and hw > 0:
            r = hh / hw
            nhh = max(1.0, nhw * r)
        ncx_l, ncy_l = (lx + ax) / 2.0, (ly + ay) / 2.0
    else:                                      # 边点
        if sx != 0:
            ax = -sx * hw
            nhw = max(1.0, (lx - ax) * sx / 2.0)
            nhh = hh
            ncx_l, ncy_l = (lx + ax) / 2.0, 0.0
        else:
            ay = -sy * hh
            nhh = max(1.0, (ly - ay) * sy / 2.0)
            nhw = hw
            ncx_l, ncy_l = 0.0, (ly + ay) / 2.0
    # 局部中心位移 -> 全局
    gcx = st["cx"] + (ncx_l * c - ncy_l * s)
    gcy = st["cy"] + (ncx_l * s + ncy_l * c)
    return {"cx": gcx, "cy": gcy, "hw": nhw, "hh": nhh, "ang": ang}


def drag_move(st, x, y, start_x, start_y):
    """框内拖动 = 平移"""
    return {"cx": st["cx"] + (x - start_x), "cy": st["cy"] + (y - start_y),
            "hw": st["hw"], "hh": st["hh"], "ang": st["ang"]}


def drag_rotate(st, x, y, start_x, start_y):
    """框外拖动 = 绕中心旋转（角度差即增量）"""
    a0 = math.atan2(start_y - st["cy"], start_x - st["cx"])
    a1 = math.atan2(y - st["cy"], x - st["cx"])
    return {"cx": st["cx"], "cy": st["cy"], "hw": st["hw"], "hh": st["hh"],
            "ang": st["ang"] + (a1 - a0)}


def warp_state(orig_img, orig_bbox, st):
    """把原图层按状态变换。返回 (img, bbox) 或 None（退化/全空）。

    源 = 原 bbox 的矩形，目标 = 状态四角（平行四边形），三点定仿射。
    输出画布取目标四角的外接矩形（留 1px 余量），warp 后按 alpha 收紧。
    """
    if cv2 is None or orig_img is None or orig_img.ndim != 3:
        return None
    x0, y0, x1, y1 = [int(v) for v in orig_bbox]
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0 or st["hw"] < 1.0 or st["hh"] < 1.0:
        return None
    pts = corners(st)
    src = np.float32([[0, 0], [w, 0], [w, h]])
    dst = np.float32([pts[0], pts[1], pts[2]])
    M = cv2.getAffineTransform(src, dst)
    arr = np.array(pts, np.float64)
    minx, miny = arr[:, 0].min(), arr[:, 1].min()
    maxx, maxy = arr[:, 0].max(), arr[:, 1].max()
    ox, oy = int(math.floor(minx)), int(math.floor(miny))
    ow = max(1, int(math.ceil(maxx)) - ox)
    oh = max(1, int(math.ceil(maxy)) - oy)
    if ow * oh > 80_000_000:                     # 防失控：>8000 万像素拒绝
        return None
    M2 = M.copy()
    M2[0, 2] -= ox
    M2[1, 2] -= oy
    out = cv2.warpAffine(orig_img, M2, (ow, oh), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    a = out[:, :, 3]
    ys, xs = np.nonzero(a > 0)
    if len(xs) == 0:
        return None
    nx0, ny0 = int(xs.min()), int(ys.min())
    nx1, ny1 = int(xs.max()) + 1, int(ys.max()) + 1
    sub = np.ascontiguousarray(out[ny0:ny1, nx0:nx1])
    # ox/oy 已是图像坐标（目标四角最小值），nx*/ny* 是输出画布局部偏移；
    # 不能再加 x0/y0 —— 曾多算一次导致提交后图层整体平移 (原bbox.x0, y0)
    return sub, (ox + nx0, oy + ny0, ox + nx1, oy + ny1)
