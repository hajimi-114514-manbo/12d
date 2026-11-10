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
}
NODE_OF_TAG = {
    "Body": "body", "Neck": "body",
    "Head": "head", "Nose": "head", "Mouth": "head", "Head_ears": "head",
    "+Hair Back": "hair_back", "+Hair Front": "hair_front",
    "Left Eye": "eye_left", "+Left Pupil": "eye_left",
    "Left Eyelid Top": "eye_left", "+Left Eyebrow": "eye_left",
    "Right Eye": "eye_right", "+Right Pupil": "eye_right",
    "Right Eyelid Top": "eye_right", "+Right Eyebrow": "eye_right",
}
# 支点定位方式
PIVOT_MODE = {"body": "bbox_bottom", "head": "contact",
              "hair_back": "contact", "hair_front": "contact",
              "eye_left": "bbox_center", "eye_right": "bbox_center"}


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
        a = np.asarray(Image.open(p).convert("RGBA")).astype(np.float32)
        x0, y0 = int(it["left"]), int(it["top"])
        x1, y1 = int(it["right"]), int(it["bottom"])
        # 两种来源要区分：
        #  - layers_full/*.png 已是全画布（W x H），直接用
        #  - ComfyUI 原始输出是按 bbox 裁剪的，需要贴回原位
        # 否则全画布的图会被二次偏移/裁掉
        if not (a.shape[1] == W and a.shape[0] == H):
            bw, bh = max(1, x1 - x0), max(1, y1 - y0)
            if (a.shape[1], a.shape[0]) != (bw, bh):
                a = np.asarray(Image.fromarray(a.astype(np.uint8)).resize(
                    (bw, bh), Image.LANCZOS)).astype(np.float32)
            full = np.zeros((H, W, 4), np.float32)
            full[y0:y1, x0:x1] = a
            a = full
        out.append({"name": name, "tag": tag, "node": node, "path": p,
                    "rgba": a, "alpha": a[:, :, 3] / 255.0,
                    "bbox": (x0, y0, x1, y1),
                    "depth": float(it.get("depth_median", 0.5))})
    return out, W, H


# ---------------- ② 支点自动定位 ----------------
def _union_alpha(layers):
    m = np.zeros(layers[0]["alpha"].shape, np.float32)
    for l in layers:
        m = np.maximum(m, l["alpha"])
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
        ua = _union_alpha(ls)
        ys, xs = np.where(ua > 0.5)
        bx0, bx1, by0, by1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
        mode = PIVOT_MODE.get(node, "bbox_center")
        if mode == "bbox_center":
            pivots[node] = [(bx0 + bx1) / 2.0, (by0 + by1) / 2.0]
        elif mode == "bbox_bottom":
            pivots[node] = [(bx0 + bx1) / 2.0, float(by1)]
        elif mode == "contact":
            par = NODE_PARENT.get(node)
            if par in by_node:
                pivots[node] = list(_contact_point(ua, _union_alpha(by_node[par])))
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
            "_alpha": _union_alpha(by_node[node]),
        })
    rig["_layers"] = layers
    # 显式绘制顺序（远->近）。默认按深度：depth_median 大的先画。
    # 之前隐式按节点名字母序，head 会盖住 hair_front（遮挡 bug），现在以此列表为准，
    # UI 拖拽图层重排写的就是它。
    rig["draw_order"] = [l["name"] for l in sorted(layers, key=lambda x: -x["depth"])]
    return rig


# ---------------- ③ 骨架渲染 ----------------
def _affine(rot_deg, tx, ty, sc, pivot):
    """绕 pivot 旋转+缩放，再整体平移 tx/ty。
    正确形式：M = T(pivot+t) @ R @ T(-pivot)
    这样 R=I、t=0 时得到单位阵（图层原地不动）。
    注意别漏掉左边的 T(pivot) —— 写成 T(t) @ R @ T(-pivot) 会把支点搬走、整张图飞出画布。"""
    r = math.radians(rot_deg)
    c, s = math.cos(r), math.sin(r)
    px, py = pivot
    return np.array([
        [c * sc, -s * sc, (tx + px) - c * sc * px + s * sc * py],
        [s * sc,  c * sc, (ty + py) - s * sc * px - c * sc * py],
        [0.0, 0.0, 1.0],
    ], np.float64)


def animation_for(node, t):
    a = {"rot": 0.0, "tx": 0.0, "ty": 0.0, "sc": 1.0}
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
        period, d = 4.6, 0.16
        ph = t % period
        if ph < d:
            s = 1.0 - 0.94 * (ph / d)
        elif ph < 2 * d:
            s = 0.06 + 0.94 * ((ph - d) / d)
        else:
            s = 1.0
        a["sc"] = s
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
            a = animation_for(ch, t)
            cum[ch] = cum[node] @ _affine(a["rot"], a["tx"], a["ty"], a["sc"], nd["pivot"])
            queue.append(ch)
    return cum


def render_frame(rig, t, hidden=None):
    """按 rig['draw_order']（远->近）绘制；hidden 是要跳过的层名集合。
    注意：早期版本按节点名字母序绘制，head 会盖住 hair_front；
    现在统一走 draw_order，UI 拖拽重排写的就是它。"""
    return render_frame_ordered(rig, t, hidden)


def render_frame_ordered(rig, t, hidden=None):
    """按 rig['draw_order']（远->近）绘制；每层仍应用其所属节点的累积变换。
    未列进 draw_order 的层按原节点顺序补在最后。"""
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
        rgba = L["rgba"]
        h, w = rgba.shape[:2]
        M = cum.get(node_of.get(ln, "root"), np.eye(3))[:2, :]
        warped = cv2.warpAffine(rgba, M, (w, h), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        al = warped[:, :, 3:4] / 255.0
        canvas = canvas * (1 - al) + warped[:, :, :3] * al
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
        ls = by_tag.get(tag, [])
        if not ls:
            return None
        a = _union_alpha(ls)
        arr = np.zeros((H, W, 4), np.uint8)
        arr[:, :, 3] = (a * 255).astype(np.uint8)
        for l in ls:
            src = l["rgba"].astype(np.uint8)
            al = (l["alpha"][:, :, None] * 255).astype(np.uint8)
            arr[:, :, :3] = np.clip(arr[:, :, :3] * (1 - al / 255.0)
                                    + src[:, :, :3] * (al / 255.0), 0, 255).astype(np.uint8)
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
            a("  var f_%s = proj.items.importItem(new File('%s'));" % (ln, ln))
            a("  try { f_%s.mainSource.colorPremultiplied = false; } catch(e){}" % ln)
            a("  var l_%s = c_%s.layers.add(f_%s); l_%s.name='%s';" % (ln, nd["node"], ln, ln, ln))
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
    here = r"D:\视频\26114\anon_platform\seethrough"
    meta = os.path.join(here, "layers_meta.json")
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
