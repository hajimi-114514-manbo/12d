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


# ==================== 网格变形（自由变换内部格点）====================
# 自由变换除了整体缩放/旋转，PS 还支持"变形(warp)"：内部格点任意拖。
# 这里用 n x n 个四边形块做分段双线性重映射 —— 能表达弯曲（发丝/布料褶皱），
# 而整体仿射只能表达平移+旋转+缩放+斜切。
GRID_N = 3                      # 每边 3 块 -> 4x4 = 16 个控制点


def mesh_init(st, n=None):
    """给状态补上 mesh 字段（局部偏移，全 0）。幂等：已有 mesh 时保留用户拖动结果。"""
    n = GRID_N if n is None else int(n)
    st = dict(st)
    need = (n + 1) * (n + 1)
    mesh = st.get("mesh")
    if not isinstance(mesh, list) or len(mesh) != need:
        mesh = [[0.0, 0.0] for _ in range(need)]
    st["mesh"] = mesh
    st["mesh_n"] = n
    return st


def _mesh_n(st, n):
    if n is not None:
        return int(n)
    return int(st.get("mesh_n") or GRID_N)


def _local_base(st, is_, js, n):
    """网格点 (is,js) 的**局部基准坐标**（未变形、未旋转，原点=中心）"""
    hw, hh = st["hw"], st["hh"]
    return (-hw + (2.0 * hw) * is_ / n, -hh + (2.0 * hh) * js / n)


def _local_off(st, is_, js, n):
    """网格点 (is,js) 的局部偏移（用户拖动量）"""
    mesh = st.get("mesh") or []
    k = js * (n + 1) + is_
    if 0 <= k < len(mesh):
        return float(mesh[k][0]), float(mesh[k][1])
    return 0.0, 0.0


def mesh_points(st, n=None):
    """16 个网格点的世界坐标（图像全分辨率），索引 = js*(n+1)+is。"""
    n = _mesh_n(st, n)
    st = mesh_init(st, n) if "mesh" not in st else st
    c, s = math.cos(st["ang"]), math.sin(st["ang"])
    out = []
    for js in range(n + 1):
        for is_ in range(n + 1):
            lx, ly = _local_base(st, is_, js, n)
            ox_, oy_ = _local_off(st, is_, js, n)
            lx += ox_
            ly += oy_
            out.append((st["cx"] + lx * c - ly * s, st["cy"] + lx * s + ly * c))
    return out


def mesh_hit(st, x, y, thr, n=None):
    """命中网格点 -> (is, js) 或 None。thr 为图像坐标下的命中半径。"""
    n = _mesh_n(st, n)
    pts = mesh_points(st, n)
    best, bestd = None, None
    for js in range(n + 1):
        for is_ in range(n + 1):
            px, py = pts[js * (n + 1) + is_]
            d = (px - x) ** 2 + (py - y) ** 2
            if d <= thr * thr and (bestd is None or d < bestd):
                bestd, best = d, (is_, js)
    return best


def drag_mesh(st, ix, js, x, y, n=None):
    """把网格点 (ix,js) 拖到世界坐标 (x,y)。只改该点的局部偏移，其余点不动。"""
    n = _mesh_n(st, n)
    st = mesh_init(st, n)
    c, s = math.cos(-st["ang"]), math.sin(-st["ang"])
    dx, dy = x - st["cx"], y - st["cy"]
    # 世界 -> 局部（逆旋转）
    lx = dx * c - dy * s
    ly = dx * s + dy * c
    bx, by = _local_base(st, ix, js, n)
    mesh = [list(p) for p in st["mesh"]]
    mesh[js * (n + 1) + ix] = [lx - bx, ly - by]
    out = dict(st)
    out["mesh"] = mesh
    out["mesh_n"] = n
    return out


def warp_mesh(orig_img, orig_bbox, st, n=None):
    """分段双线性网格变形。返回 (img, bbox) 或 None。

    反向映射：输出像素 -> 落在变形后哪个网格块/块内参数 -> 该块在源矩形的
    对应位置（双线性）-> cv2.remap 采样。块内连续、块间共享边 -> 无接缝。

    关键坐标系（曾错在此）：remap 的 map 必须是**源图自身像素坐标**
    （即 0..w-1, 0..h-1），不能混入世界/画布偏移 ox,oy；ox,oy 只用于
    决定输出画布尺寸和最后把 bbox 还原成图像坐标。
    """
    if cv2 is None or orig_img is None or orig_img.ndim != 3:
        return None
    n = _mesh_n(st, n)
    st = mesh_init(st, n)
    x0, y0, x1, y1 = [int(v) for v in orig_bbox]
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0 or st["hw"] < 1.0 or st["hh"] < 1.0:
        return None
    dst_world = np.array(mesh_points(st, n), np.float64)    # 变形后世界坐标
    ox, oy = int(math.floor(dst_world[:, 0].min())), int(math.floor(dst_world[:, 1].min()))
    ow = max(1, int(math.ceil(dst_world[:, 0].max())) - ox)
    oh = max(1, int(math.ceil(dst_world[:, 1].max())) - oy)
    if ow * oh > 80_000_000:
        return None
    dst = dst_world - np.array([ox, oy], np.float64)        # 输出画布局部坐标
    # 源图里每个网格点的位置（源图 = orig_img，0..w-1 / 0..h-1）
    src = np.array([[w * a / float(n), h * b / float(n)]
                    for b in range(n + 1) for a in range(n + 1)], np.float64)
    idx = lambda a, b: b * (n + 1) + a
    map_x = np.full((oh, ow), -1.0, np.float32)
    map_y = np.full((oh, ow), -1.0, np.float32)
    yy, xx = np.mgrid[0:oh, 0:ow]
    px = xx.astype(np.float64)
    py = yy.astype(np.float64)
    for js in range(n):
        for is_ in range(n):
            d00 = dst[idx(is_, js)]
            d10 = dst[idx(is_ + 1, js)]
            d11 = dst[idx(is_ + 1, js + 1)]
            d01 = dst[idx(is_, js + 1)]
            if not (np.isfinite([d00, d10, d11, d01]).all()):
                continue
            xs_ = (d00[0], d10[0], d11[0], d01[0])
            ys_ = (d00[1], d10[1], d11[1], d01[1])
            bx0 = max(0, int(math.floor(min(xs_))))
            bx1 = min(ow, int(math.ceil(max(xs_))) + 1)
            by0 = max(0, int(math.floor(min(ys_))))
            by1 = min(oh, int(math.ceil(max(ys_))) + 1)
            if bx1 <= bx0 or by1 <= by0:
                continue
            # 每块拆两个三角形，各自仿射（输出局部坐标 -> 源图坐标）
            for tri in ((0, 1, 2), (0, 2, 3)):
                dsel = np.float32([d00, d10, d11, d01])[list(tri)]
                ssel = np.float32([src[idx(is_, js)], src[idx(is_ + 1, js)],
                                   src[idx(is_ + 1, js + 1)], src[idx(is_, js + 1)]])[list(tri)]
                try:
                    M = cv2.getAffineTransform(dsel, ssel)
                except cv2.error:
                    continue
                subpx = px[by0:by1, bx0:bx1]
                subpy = py[by0:by1, bx0:bx1]
                inside = _in_tri(subpx, subpy, dsel[0], dsel[1], dsel[2])
                if not inside.any():
                    continue
                sx = M[0, 0] * subpx + M[0, 1] * subpy + M[0, 2]
                sy = M[1, 0] * subpx + M[1, 1] * subpy + M[1, 2]
                mx = map_x[by0:by1, bx0:bx1]
                my = map_y[by0:by1, bx0:bx1]
                mx[inside] = sx[inside]
                my[inside] = sy[inside]
    out = cv2.remap(orig_img, map_x, map_y, cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    a = out[:, :, 3]
    ys2, xs2 = np.nonzero(a > 0)
    if len(xs2) == 0:
        return None
    nx0, ny0 = int(xs2.min()), int(ys2.min())
    nx1, ny1 = int(xs2.max()) + 1, int(ys2.max()) + 1
    sub = np.ascontiguousarray(out[ny0:ny1, nx0:nx1])
    return sub, (ox + nx0, oy + ny0, ox + nx1, oy + ny1)


def _in_tri(px, py, a, b, c):
    """批量判断点是否在三角形 abc 内（含边界，重心法）。"""
    def sign(ax, ay, bx, by, cx, cy):
        return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    d1 = sign(b[0], b[1], c[0], c[1], px, py)
    d2 = sign(c[0], c[1], a[0], a[1], px, py)
    d3 = sign(a[0], a[1], b[0], b[1], px, py)
    neg = (d1 < 0) | (d2 < 0) | (d3 < 0)
    pos = (d1 > 0) | (d2 > 0) | (d3 > 0)
    return ~(neg & pos)
