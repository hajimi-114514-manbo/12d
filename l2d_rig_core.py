# -*- coding: utf-8 -*-
"""伪L2D 骨架引擎（核心层，无 UI）

语义规范：采用 Adobe Character Animator 官方图层命名规范
  (helpx.adobe.com/adobe-character-animator/desktop/creating-and-controlling-puppets/prepare-artwork.html)
  —— 这是行业事实标准，输出的 PSD 可被 Character Animator 全自动绑定，
     也可被 Live2D Cubism Editor 自动转成部件。

三块：
  1) 语义映射   图层名 -> CA 标签（+ 前缀 = 可独立形变）
  2) 支点自动定位
     - 连接型（头发贴头皮）：该层 alpha 中距父层 alpha 最近的像素
     - 叠放型（眼睛组）：该组 alpha 并集包围盒中心
     - 身体/头：与下方组的接触点
  3) 导出：PSD（含 Head/Mouth/Body 分组）/ AE 脚本 / 自研 JSON
"""
import json
import math
import os
import numpy as np
import cv2
from PIL import Image
from scipy import ndimage

# ---------------- ① 语义映射（CA 官方标签）----------------
CA_TAG = {
    "topwear":     "Body",
    "neck":        "Neck",
    "face":        "Head",
    "nose":        "Nose",
    "mouth":       "Mouth",
    "ears-l":      "Head_ears",
    "ears-r":      "Head_ears",
    "eyebrow-l":   "+Left Eyebrow",
    "eyebrow-r":   "+Right Eyebrow",
    "eyewhite-l":  "Left Eye",
    "eyewhite-r":  "Right Eye",
    "irides-l":    "+Left Pupil",
    "irides-r":    "+Right Pupil",
    "eyelash-l":   "Left Eyelid Top",
    "eyelash-r":   "Right Eyelid Top",
    "front_hair":  "+Hair Front",
    "back_hair":   "+Hair Back",
}

# 骨架节点（用于本地引擎 / AE / JSON），父节点构成层级
NODE_PARENT = {
    "body":     "root",
    "head":     "body",
    "hair_back": "head",
    "hair_front": "head",
    "eye_left":  "head",
    "eye_right": "head",
    "lid_left":  "head",
    "lid_right": "head",
    "brow_left": "head",
    "brow_right": "head",
}
NODE_OF_TAG = {
    "Body": "body", "Neck": "body",
    "Head": "head", "Nose": "head", "Mouth": "head", "Head_ears": "head",
    "+Hair Back": "hair_back", "+Hair Front": "hair_front",
    "Left Eye": "eye_left", "+Left Pupil": "eye_left",
    "Left Eyelid Top": "lid_left", "+Left Eyebrow": "brow_left",
    "Right Eye": "eye_right", "+Right Pupil": "eye_right",
    "Right Eyelid Top": "lid_right", "+Right Eyebrow": "brow_right",
}
# 支点定位方式
PIVOT_MODE = {"body": "bbox_bottom", "head": "contact",
              "hair_back": "contact", "hair_front": "contact",
              "eye_left": "bbox_bottom", "eye_right": "bbox_bottom",
              "lid_left": "bbox_bottom", "lid_right": "bbox_bottom",
              "brow_left": "bbox_center", "brow_right": "bbox_center"}

# 语义深度表（远->近，数值取自 SeeThrough 实测深度中位数）。
# PSD 导入没有真实深度，按 CA 标签查这张表定叠放顺序。
TAG_DEPTH = {
    "+Hair Back": 0.90,
    "Neck": 0.64, "Body": 0.60,
    "Head": 0.55, "Head_ears": 0.54, "Mouth": 0.53, "Nose": 0.47,
    "+Left Eyebrow": 0.43, "+Right Eyebrow": 0.42,
    "Left Eye": 0.34, "Right Eye": 0.33,
    "+Left Pupil": 0.31, "+Right Pupil": 0.30,
    "Left Eyelid Top": 0.27, "Right Eyelid Top": 0.24,
    "+Hair Front": 0.14,
}
DEFAULT_DEPTH = 0.50

# SeeThrough 的配饰层 CA 表里没有，按 raw 名归入合理节点（头饰跟头走、
# 服饰跟身体走），否则全塞 other 节点会静止在 root 上不随头部旋转。
RAW_NODE = {
    "headwear": "head", "eyewear": "head", "earwear": "head",
    "neckwear": "body", "handwear": "body", "bottomwear": "body",
    "legwear": "body", "footwear": "body", "wings": "body", "tail": "body",
    "objects": "other",
}

# SeeThrough SavePSD 的原始层名没有 CA 标签（irides-l/face...），查 TAG_DEPTH 前
# 必须先过 CA_TAG 归一化；配饰类 CA 表里没有，单独给一张 raw 名深度表。
RAW_DEPTH = {
    "wings": 0.95, "tail": 0.95,
    "footwear": 0.62, "legwear": 0.61, "bottomwear": 0.60,
    "neckwear": 0.57, "handwear": 0.58, "earwear": 0.50,
    "headwear": 0.12, "eyewear": 0.28, "objects": 0.30,
}


def depth_for_name(name):
    """按语义表推断深度：raw 名 -> CA 标签 -> TAG_DEPTH；配饰走 RAW_DEPTH。"""
    key = name.replace(" ", "_")
    tag = CA_TAG.get(key, CA_TAG.get(name, None))
    if tag is not None and tag in TAG_DEPTH:
        return TAG_DEPTH[tag]
    # 层名本身就是 CA 标签（PSD 往返导入："Hair Front" / "+Hair Front"）
    for cand in (name, "+" + name.lstrip("+"), tag):
        if cand in TAG_DEPTH:
            return TAG_DEPTH[cand]
    base = key.split("-")[0]          # handwear-l -> handwear
    if base in RAW_DEPTH:
        return RAW_DEPTH[base]
    return DEFAULT_DEPTH


def load_layers(meta_path, img_dir):
    """meta 里的 filename 是 ComfyUI 原始名；实际可能已归拢到 layers_full/ 或 layer_<名>.png"""
    meta = json.load(open(meta_path, encoding="utf8"))
    W, H = int(meta["width"]), int(meta["height"])
    out = []
    for it in meta["layers"]:
        name = it["name"]
        # SeeThrough 的层名带空格（"front hair"），先归一化再查映射表
        key = name.replace(" ", "_")
        tag = CA_TAG.get(key, CA_TAG.get(name, name))
        node = NODE_OF_TAG.get(tag, "other")
        # PSD 导入时层名本身就是 CA 标签（如 "Hair Front"，+ 已被剥掉），
        # NODE_OF_TAG 的键带 + 前缀，所以两种形态都要试
        if node == "other":
            raw = name.lstrip("+").strip()
            for cand in (raw, "+" + raw, raw.replace(" ", "_"), "+" + raw.replace(" ", "_")):
                node = NODE_OF_TAG.get(cand, "other")
                if node != "other":
                    tag = cand
                    break
        if node == "other":
            # 配饰类：headwear-l -> headwear -> head/body
            base = key.replace("-", "_").split("_")[0]
            node = RAW_NODE.get(base, "other")
        base = key
        cands = [
            os.path.join(img_dir, it["filename"]),
            os.path.join(img_dir, "layer_" + base + ".png"),
            os.path.join(img_dir, "layers_full", base + ".png"),
            os.path.join(img_dir, "layers_full", tag.replace("+", "").replace(" ", "_") + ".png"),
        ]
        p = next((c for c in cands if os.path.exists(c)), None)
        if p is None:
            print("  [warn] 缺图层:", name)
            continue
        # 性能核心：层数据以 bbox 裁剪的 uint8 RGBA 存储（原全画布 float32
        # 单帧渲染 689ms/层内存 21MB；裁剪 uint8 + 子区域混合后可提速 ~20 倍）
        a = np.asarray(Image.open(p).convert("RGBA"))
        x0, y0 = max(0, int(it["left"])), max(0, int(it["top"]))
        x1, y1 = min(W, int(it["right"])), min(H, int(it["bottom"]))
        x1 = max(x0 + 1, x1); y1 = max(y0 + 1, y1)
        if a.shape[1] == W and a.shape[0] == H:
            a = a[y0:y1, x0:x1]                    # layers_full：按 bbox 裁出
        elif (a.shape[1], a.shape[0]) != (x1 - x0, y1 - y0):
            a = np.asarray(Image.fromarray(a).resize((x1 - x0, y1 - y0), Image.LANCZOS))
        # bbox 收紧：meta 的 bbox 可能是整画布（PSD 导入常见），不收紧的话
        # 每层都是全幅 warp+混合，播放直接退化回卡顿。
        # SeeThrough SavePSD 层带 0<alpha<=16 的弱噪点散落全幅，
        # 必须用阈值+每行/列至少2强像素，否则 any()>0 等于没收紧（实测过）。
        strong = a[:, :, 3] > 16
        rows = np.where(strong.sum(axis=1) >= 2)[0]
        cols = np.where(strong.sum(axis=0) >= 2)[0]
        if len(rows) and len(cols):
            ry0 = max(0, int(rows[0]) - 1)
            ry1 = min(a.shape[0], int(rows[-1]) + 2)
            cx0 = max(0, int(cols[0]) - 1)
            cx1 = min(a.shape[1], int(cols[-1]) + 2)
            if (ry0, ry1, cx0, cx1) != (0, a.shape[0], 0, a.shape[1]):
                a = a[ry0:ry1, cx0:cx1]
                x0 += cx0; y0 += ry0
                x1 = x0 + (cx1 - cx0); y1 = y0 + (ry1 - ry0)
        out.append({"name": name, "tag": tag, "node": node, "path": p,
                    "img": np.ascontiguousarray(a),   # 裁剪后的 uint8 RGBA
                    "bbox": (x0, y0, x1, y1),
                    "depth": float(it.get("depth_median", 0.5))})
    return out, W, H


# ---------------- ② 支点自动定位 ----------------
def _union_alpha(layers, W, H):
    """由裁剪层拼出该节点组的联合 alpha（全画布 float 0/1）"""
    m = np.zeros((H, W), np.float32)
    for l in layers:
        x0, y0, x1, y1 = l["bbox"]
        a = l["img"][:, :, 3]
        m[y0:y1, x0:x1] = np.maximum(m[y0:y1, x0:x1], (a > 127).astype(np.float32))
    return m


def _contact_point(child_alpha, parent_alpha):
    d = ndimage.distance_transform_edt(~(parent_alpha > 0.5))
    m = child_alpha > 0.5
    if not m.any():
        ys, xs = np.where(parent_alpha > 0.5)
        return float(xs.mean()), float(ys.mean())
    idx = int(np.argmin(d[m]))
    ys, xs = np.where(m)
    return float(xs[idx]), float(ys[idx])


def _dist_to(parent_alpha):
    """到 parent 层 alpha 的距离场（供 UI 复用接触点算法）"""
    return ndimage.distance_transform_edt(~(parent_alpha > 0.5))


def detect_pivots(layers, W, H):
    by_node = {}
    for l in layers:
        by_node.setdefault(l["node"], []).append(l)
    pivots = {}
    for node, ls in by_node.items():
        ua = _union_alpha(ls, W, H)
        ys, xs = np.where(ua > 0.5)
        if len(ys) == 0:
            # 整组都是空层/噪点层：支点退化为画布中心，不能让 min() 炸空数组
            pivots[node] = [W / 2.0, H / 2.0]
            continue
        bx0, bx1, by0, by1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
        mode = PIVOT_MODE.get(node, "bbox_center")
        if mode == "bbox_center":
            pivots[node] = [(bx0 + bx1) / 2.0, (by0 + by1) / 2.0]
        elif mode == "bbox_bottom":
            pivots[node] = [(bx0 + bx1) / 2.0, float(by1)]
        elif mode == "bbox_top":
            # 眼睛：支点在框顶边中点（保留模式，供非压扁类 rig 用）
            pivots[node] = [(bx0 + bx1) / 2.0, float(by0)]
        elif mode == "contact":
            par = NODE_PARENT.get(node)
            if par in by_node:
                pivots[node] = list(_contact_point(ua, _union_alpha(by_node[par], W, H)))
            else:
                pivots[node] = [(bx0 + bx1) / 2.0, (by0 + by1) / 2.0]
        else:
            pivots[node] = [(bx0 + bx1) / 2.0, (by0 + by1) / 2.0]
    return pivots, by_node


def build_rig(layers, W, H):
    pivots, by_node = detect_pivots(layers, W, H)
    rig = {"width": W, "height": H, "fps": 30.0, "nodes": []}
    for node in sorted(by_node.keys()):
        rig["nodes"].append({
            "node": node, "parent": NODE_PARENT.get(node, "root"),
            "pivot": pivots.get(node, [W / 2.0, H / 2.0]),
            "tags": sorted({l["tag"] for l in by_node[node]}),
            "layers": [l["name"] for l in by_node[node]],
            "_alpha": _union_alpha(by_node[node], W, H),
        })
    rig["_layers"] = layers
    # 眨眼几何（实测：睫毛条底边贴着眼框底边，且条带比眼框高）：
    # eye 与 lid 都用"底边锚定纵向压扁"——支点在各自 bbox 底边，sc_y=s。
    # 闭眼瞬间睫毛条塌成下缘一条线（上眼睑滑到下睫毛线），同时眼白瞳孔
    # 压成细缝并被这条线盖住 -> 真实的"上睫毛向下闭合"。眉毛完全不动。
    node_of = {n["node"]: n for n in rig["nodes"]}
    for side in ("left", "right"):
        e = node_of.get("eye_" + side)
        if e is not None:
            ys, _ = np.where(e["_alpha"] > 0.5)
            e["blink_h"] = float(ys.max() - ys.min()) if len(ys) else 0.0
    # 显式绘制顺序（远->近）。默认按深度：depth_median 大的先画。
    # 之前隐式按节点名字母序，head 会盖住 hair_front（遮挡 bug），现在以此列表为准，
    # UI 拖拽图层重排写的就是它。
    rig["draw_order"] = [l["name"] for l in sorted(layers, key=lambda x: -x["depth"])]
    return rig


# ---------------- ③ 骨架渲染 ----------------
def _affine(rot_deg, tx, ty, sc, pivot, sc_y=None):
    """绕 pivot 旋转+缩放（sc_y 可与 sc 不同，供眼睛纵向眨眼），再整体平移 tx/ty。
    正确形式：M = T(pivot+t) @ (R @ S) @ T(-pivot)
    注意别漏掉左边的 T(pivot) —— 写成 T(t) @ R @ T(-pivot) 会把支点搬走、整张图飞出画布。"""
    if sc_y is None:
        sc_y = sc
    r = math.radians(rot_deg)
    c, s = math.cos(r), math.sin(r)
    px, py = pivot
    a00, a01 = c * sc, -s * sc_y
    a10, a11 = s * sc, c * sc_y
    return np.array([
        [a00, a01, (tx + px) - a00 * px - a01 * py],
        [a10, a11, (ty + py) - a10 * px - a11 * py],
        [0.0, 0.0, 1.0],
    ], np.float64)


def _blink_s(t):
    """眨眼开合系数：1=全开，0.06=闭合。周期 4.6s，闭合占 0.16s。"""
    period, d = 4.6, 0.16
    ph = t % period
    if ph < d:
        return 1.0 - 0.94 * (ph / d)
    elif ph < 2 * d:
        return 0.06 + 0.94 * ((ph - d) / d)
    return 1.0


def animation_for(node, t, nd=None):
    """nd 为 rig 节点字典（提供 blink_h/blink_dy 等预计算参数）"""
    a = {"rot": 0.0, "tx": 0.0, "ty": 0.0, "sc": 1.0, "sc_y": None}
    if node == "body":
        a["ty"] = 3.0 * math.sin(t * 2.1)
    elif node == "head":
        a["rot"] = 1.4 * math.sin(t * 0.9)
        a["ty"] = 1.2 * math.sin(t * 2.1)
    elif node == "hair_front":
        a["rot"] = 2.6 * math.sin(t * 1.5)
    elif node == "hair_back":
        a["rot"] = 1.9 * math.sin(t * 1.1 + 0.7)
    elif node in ("eye_left", "eye_right"):
        # 眼睛：只纵向压扁（支点在框底，眼缝塌到下缘），不再连眉毛睫毛一起缩
        a["sc_y"] = _blink_s(t)
    elif node in ("lid_left", "lid_right"):
        # 眼睑：与眼睛同曲线，底边锚定塌成一条线 = 闭合的上眼睑
        a["sc_y"] = _blink_s(t)
    elif node in ("brow_left", "brow_right"):
        # 眉毛：静止（旧版跟着眼睛一起缩放消失，这就是修正点）
        pass
    return a


def _cum_transforms(rig, t):
    """BFS 自 root 向下，算每个节点的累积 3x3 变换"""
    children = {}
    for n in rig["nodes"]:
        children.setdefault(n["parent"], []).append(n["node"])
    cum = {"root": np.eye(3)}
    queue = ["root"]
    seen = {"root"}
    i = 0
    while i < len(queue):
        node = queue[i]; i += 1
        for ch in children.get(node, []):
            if ch in seen:
                continue
            seen.add(ch)
            nd = next(n for n in rig["nodes"] if n["node"] == ch)
            a = animation_for(ch, t, nd)
            cum[ch] = cum[node] @ _affine(a["rot"], a["tx"], a["ty"], a["sc"],
                                          nd["pivot"], a.get("sc_y"))
            queue.append(ch)
    return cum


def _lite_rig(rig, sc):
    """半分辨率简化 rig（播放/GIF 用）。裁剪图缓存，支点/顺序每次从源同步。"""
    cache = rig.setdefault("_lite", {})
    lite = cache.get(sc)
    if lite is None:
        W, H = max(1, int(round(rig["width"] * sc))), max(1, int(round(rig["height"] * sc)))
        layers = []
        for l in rig["_layers"]:
            x0, y0, x1, y1 = l["bbox"]
            nx0, ny0 = int(round(x0 * sc)), int(round(y0 * sc))
            nx1, ny1 = int(round(x1 * sc)), int(round(y1 * sc))
            nx1, ny1 = max(nx0 + 1, min(W, nx1)), max(ny0 + 1, min(H, ny1))
            img = cv2.resize(l["img"], (nx1 - nx0, ny1 - ny0), interpolation=cv2.INTER_AREA)
            layers.append({"name": l["name"], "tag": l["tag"], "node": l["node"],
                           "path": l["path"], "img": img, "bbox": (nx0, ny0, nx1, ny1),
                           "depth": l["depth"]})
        lite = {"width": W, "height": H, "fps": rig["fps"], "_layers": layers,
                "nodes": [{"node": n["node"], "parent": n["parent"], "layers": n["layers"],
                           "pivot": list(n["pivot"])} for n in rig["nodes"]],
                "draw_order": list(rig.get("draw_order", []))}
        cache[sc] = lite
    # 源 rig 的支点/绘制顺序可能被 UI 改过，同步进 lite
    src = {n["node"]: n for n in rig["nodes"]}
    for n in lite["nodes"]:
        s = src.get(n["node"])
        if s is not None:
            n["pivot"] = [s["pivot"][0] * sc, s["pivot"][1] * sc]
            n["layers"] = s["layers"]
    lite["draw_order"] = list(rig.get("draw_order", lite["draw_order"]))
    return lite


def render_frame(rig, t, hidden=None, scale=None):
    """按 rig['draw_order']（远->近）绘制。scale=0.5 走半分辨率简化 rig（播放用）。"""
    if scale:
        rig = _lite_rig(rig, scale)
    hidden = hidden or set()
    W, H = rig["width"], rig["height"]
    canvas = np.zeros((H, W, 3), np.float32)
    cum = _cum_transforms(rig, t)
    node_of = {}
    for nd in rig["nodes"]:
        for ln in nd["layers"]:
            node_of[ln] = nd["node"]
    by_name = {l["name"]: l for l in rig["_layers"]}
    seen = set()
    for ln in rig.get("draw_order", []) + list(node_of.keys()):
        if ln in seen or ln in hidden or ln not in by_name:
            continue
        seen.add(ln)
        L = by_name[ln]
        img = L["img"]                       # 裁剪 uint8 RGBA
        x0, y0, x1, y1 = L["bbox"]
        M = cum.get(node_of.get(ln, "root"), np.eye(3))[:2, :].astype(np.float32)
        # 快速路径：纯平移（body 呼吸及跟随它的配饰）跳过 warpAffine，直接切片贴
        if (M[0, 0] == 1.0 and M[0, 1] == 0.0 and M[1, 0] == 0.0
                and M[1, 1] == 1.0):
            itx = int(round(M[0, 2])); ity = int(round(M[1, 2]))
            sx0 = max(0, x0 + itx); sy0 = max(0, y0 + ity)
            sx1 = min(W, x1 + itx); sy1 = min(H, y1 + ity)
            if sx1 <= sx0 or sy1 <= sy0:
                continue
            src = img[sy0 - ity - y0:sy1 - ity - y0, sx0 - itx - x0:sx1 - itx - x0]
            a = src[:, :, 3:4].astype(np.float32) * (1.0 / 255.0)
            sub = canvas[sy0:sy1, sx0:sx1]
            sub *= (1.0 - a)
            sub += src[:, :, :3].astype(np.float32) * a
            continue
        # 层 bbox 四角变换 -> 只处理受影响子矩形（旋转小、位移小时远小于全画布）
        # cv2.transform 的点集必须是 (N,1,2)：直接给 (1,4,2) 会被当成
        # 1 个 4 通道点、只返回第一个角，子矩形退化致整帧全黑（曾踩）
        corners = cv2.transform(
            np.array([x0, y0, x1, y0, x0, y1, x1, y1], np.float32).reshape(4, 1, 2),
            M)[:, 0, :]
        rx0 = max(0, int(np.floor(corners[:, 0].min())) - 1)
        rx1 = min(W - 1, int(np.ceil(corners[:, 0].max())) + 1)
        ry0 = max(0, int(np.floor(corners[:, 1].min())) - 1)
        ry1 = min(H - 1, int(np.ceil(corners[:, 1].max())) + 1)
        if rx1 <= rx0 or ry1 <= ry0:
            continue
        # 源数组是裁剪图：其 (0,0) 对应画布 (x0,y0)。
        # 完整映射 = T(-(rx0,ry0)) @ M @ T(x0,y0)
        M2 = M.copy()
        M2[0, 2] = M[0, 0] * x0 + M[0, 1] * y0 + M[0, 2] - rx0
        M2[1, 2] = M[1, 0] * x0 + M[1, 1] * y0 + M[1, 2] - ry0
        warped = cv2.warpAffine(img, M2, (rx1 - rx0 + 1, ry1 - ry0 + 1),
                                flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        a = warped[:, :, 3:4].astype(np.float32) * (1.0 / 255.0)
        sub = canvas[ry0:ry1 + 1, rx0:rx1 + 1]
        sub *= (1.0 - a)
        sub += warped[:, :, :3].astype(np.float32) * a
    return np.clip(canvas, 0, 255).astype(np.uint8)


def render_static(rig):
    return render_frame(rig, 0.0)


# ---------------- ④ 导出 PSD（CA 分组结构）----------------
def export_psd(rig, out_path):
    """按 CA 分组结构导出 PSD：Body 组 / Head 组（内含 Mouth 组）
    注意 psd-tools 签名：create_group(name=...)、create_pixel_layer_from_array(array, name=...)"""
    from psd_tools import PSDImage
    W, H = rig["width"], rig["height"]
    psd = PSDImage.new("RGB", (W, H))
    by_tag = {}
    for l in rig["_layers"]:
        by_tag.setdefault(l["tag"], []).append(l)

    def arr_of(tag):
        """把该 tag 的裁剪层贴回全画布 uint8 RGBA（导出用，不做动画）"""
        ls = by_tag.get(tag, [])
        if not ls:
            return None
        arr = np.zeros((H, W, 4), np.uint8)
        for l in ls:
            x0, y0, x1, y1 = l["bbox"]
            src = l["img"]
            dst = arr[y0:y1, x0:x1]
            a = src[:, :, 3:4].astype(np.float32) * (1.0 / 255.0)
            out = dst[:, :, :3].astype(np.float32) * (1 - a) + src[:, :, :3] * a
            dst[:, :, :3] = out.astype(np.uint8)
            dst[:, :, 3] = np.maximum(dst[:, :, 3], src[:, :, 3])
        return arr

    def add(group, tag):
        a = arr_of(tag)
        if a is None:
            return
        group.create_pixel_layer_from_array(a, name=tag)

    body = psd.create_group(name="Body")
    head = psd.create_group(name="Head")
    mouth = head.create_group(name="Mouth")

    for tag in ("Body", "Neck"):
        add(body, tag)
    # Head 组内：从底到顶
    for tag in ("+Hair Back", "Head", "Head_ears", "Nose"):
        add(head, tag)
    add(mouth, "Mouth")
    for tag in ("Left Eye", "+Left Pupil", "Left Eyelid Top", "+Left Eyebrow",
                "Right Eye", "+Right Pupil", "Right Eyelid Top", "+Right Eyebrow",
                "+Hair Front"):
        add(head, tag)
    psd.save(out_path)
    return out_path


# ---------------- ⑤ 导出 AE 脚本 ----------------
def _jsid(s):
    """层名转合法 JS 标识符（"back hair" / "eyebrow-l" 直接当变量名会让 AE 解析失败）"""
    import re
    return "v_" + re.sub(r"[^A-Za-z0-9_]", "_", s)


def export_ae_jsx(rig, out_path, dur=10.0, fps=30):
    A = []
    a = A.append
    a("// 伪L2D 骨架工程（l2d_rig_core 自动生成，语义规范 = Adobe Character Animator）")
    a("(function(){")
    a("  var proj = app.project;")
    a("  while (proj.numItems > 0) { proj.item(1).remove(); }")
    a("  var MW = %d, MH = %d;" % (rig["width"], rig["height"]))
    a("  var main = proj.items.addComp('l2d_rig', MW, MH, 1.0, %.1f, %d);" % (dur, fps))
    a("  main.bgColor = [0.10, 0.10, 0.12];")
    a("")
    for nd in rig["nodes"]:
        if nd["node"] == "root":
            continue
        a("  // ---- %s (父 %s) ----" % (nd["node"], nd["parent"]))
        a("  var c_%s = proj.items.addComp('node_%s', MW, MH, 1.0, %.1f, %d);"
          % (nd["node"], nd["node"], dur, fps))
        for ln in nd["layers"]:
            v = _jsid(ln)
            a("  var f_%s = proj.items.importItem(new File('%s'));" % (v, ln))
            a("  try { f_%s.mainSource.colorPremultiplied = false; } catch(e){}" % v)
            a("  var l_%s = c_%s.layers.add(f_%s); l_%s.name='%s';"
              % (v, nd["node"], v, v, ln.replace("'", "")))
        a("  var h_%s = c_%s.layers.add(c_%s); h_%s.name='%s_holder';"
          % (nd["node"], nd["node"], nd["node"], nd["node"], nd["node"]))
        a("  h_%s.property('Anchor Point').setValue([%.1f, %.1f]);" % (nd["node"], nd["pivot"][0], nd["pivot"][1]))
        a("  h_%s.property('Position').setValue([MW/2, MH/2]);" % nd["node"])
        a("")
    for nd in rig["nodes"]:
        if nd["node"] == "root":
            continue
        p = nd["parent"]
        if p == "root":
            a("  var L_%s = main.layers.add(c_%s); L_%s.name='%s';" % (nd["node"], nd["node"], nd["node"], nd["node"]))
        else:
            a("  var L_%s = c_%s.layers.add(c_%s); L_%s.name='%s';" % (nd["node"], p, nd["node"], nd["node"], nd["node"]))
    a("")
    for nd in rig["nodes"]:
        n = nd["node"]
        if n == "root":
            continue
        if n == "body":
            a("  L_%s.property('Position').expression = '[value[0], value[1] + 3*Math.sin(time*2.1)]';" % n)
        elif n == "head":
            a("  L_%s.property('Rotation').expression = 'wiggle(0.9, 1.6)[0]';" % n)
        elif n == "hair_front":
            a("  L_%s.property('Rotation').expression = 'wiggle(1.5, 3.0)[0]';" % n)
        elif n == "hair_back":
            a("  L_%s.property('Rotation').expression = 'wiggle(1.1, 2.4)[0]';" % n)
        elif n in ("eye_left", "eye_right"):
            expr = ("var p=4.6,d=0.16,ph=time%p,s=1;"
                    "if(ph<d){s=1-0.94*(ph/d);} else if(ph<2*d){s=0.06+0.94*((ph-d)/d);} "
                    "[value[0], s*value[1]/100];")
            a("  L_" + n + ".property('Scale').expression = '" + expr + "';")
        elif n in ("lid_left", "lid_right"):
            # 与眼睛同曲线：底边锚定纵向塌缩（JSX 锚点已从 pivot 写入）
            expr = ("var p=4.6,d=0.16,ph=time%p,s=1;"
                    "if(ph<d){s=1-0.94*(ph/d);} else if(ph<2*d){s=0.06+0.94*((ph-d)/d);} "
                    "[value[0], s*value[1]/100];")
            a("  L_" + n + ".property('Scale').expression = '" + expr + "';")
    a("  proj.saveAs(new File('%s'));" % out_path.replace("\\", "/"))
    a("})();")
    s = "\n".join(A)
    open(out_path, "w", encoding="utf-8").write(s)
    return s


def export_json(rig, out_path):
    out = {"width": rig["width"], "height": rig["height"], "fps": 30, "nodes": []}
    for n in rig["nodes"]:
        out["nodes"].append({"name": n["node"], "parent": n["parent"],
                             "pivot": [round(n["pivot"][0], 2), round(n["pivot"][1], 2)],
                             "tags": n["tags"], "layers": n["layers"]})
    json.dump(out, open(out_path, "w", encoding="utf8"), ensure_ascii=False, indent=2)
    return out_path


# ---------------- 自检 ----------------
def self_test():
    # 相对应用目录：示例工程在 示例工程\，搬家不受影响
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "示例工程")
    meta = os.path.join(here, "layers_meta.json")
    if not os.path.exists(meta):
        print("找不到示例工程:", meta)
        return
    layers, W, H = load_layers(meta, here)
    print("载入 %d 层，画布 %dx%d" % (len(layers), W, H))
    rig = build_rig(layers, W, H)
    print("骨架节点：")
    for n in rig["nodes"]:
        print("   %-11s 父=%-6s 支点=(%.0f,%.0f) 层=%s"
              % (n["node"], n["parent"], n["pivot"][0], n["pivot"][1], n["layers"]))
    for t in (0.0, 2.3, 4.6):
        Image.fromarray(render_frame(rig, t)).save(
            os.path.join(here, "QA_rig_%.1f.jpg" % t), quality=92)
        print("   渲染 t=%.1f 完成" % t)
    jsx = export_ae_jsx(rig, os.path.join(here, "rig_build.jsx"))
    print("   AE 脚本 %d 行" % len(jsx.splitlines()))
    export_json(rig, os.path.join(here, "rig_spec.json"))
    print("   JSON 已导出")
    try:
        p = export_psd(rig, os.path.join(here, "rig_character.psd"))
        print("   PSD 已导出:", os.path.basename(p))
    except Exception as e:
        print("   PSD 导出失败:", type(e).__name__, str(e)[:160])


if __name__ == "__main__":
    self_test()
