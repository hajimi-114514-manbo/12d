# -*- coding: utf-8 -*-
"""Live2D 适配 Phase A：木偶工程 → Cubism/WebGAL 运行时目录骨架（纯导出模块）。

背景：.moc3 是 Cubism 专有格式，无法离线生成。本模块产出的是一套“可拼装”
骨架——用户在 Cubism Editor 里导入 PSD（CA 规范层名可自动转部件）绑参数、
导出 moc3 放回目录后，model3.json / motions / textures / parts_map 直接
拼成 WebGAL showLive2d 可跑的模型（详见导出的 Cubism导入指引.txt）。

只依赖标准库 + numpy/PIL/cv2（经由 l2d_rig_core），无 Qt。

格式对齐（已对照官方 Live2D/CubismSpecs + CubismWebFramework R4 运行时源码
+ WebGAL 所依赖 pixi-live2d-display@0.4.0 的打包解析器逐一验证）：
  - motion3.json：Target ∈ "Model"/"Parameter"/"PartOpacity"（注意：**不是**
    "Param"，那是运行时内部的字符串；JSON 里必须写 "Parameter"，
    否则 pixi-live2d-display 直接丢弃整条曲线）。
  - Segments 扁平编码：首点 [t0,v0]，之后按段类型码：
        0 线性段:   [0, t1, v1]                     （3 个数）
        1 贝塞尔段: [1, tc1,vc1, tc2,vc2, t1,v1]    （7 个数，tc1=t0+Δ/3,
                     tc2=t0+2Δ/3 —— AreBeziersRestricted=true 时控制点横坐标
                     必须三等分，运行时走纯 de Casteljau，时间线性映射）
        2 阶跃段:   [2, t1, v1]                     （3 个数，段内保持起点值）
  - Meta 必填：Duration/Fps/CurveCount/TotalSegmentCount/TotalPointCount；
    （规格里没有 TotalPersistentDuration，官方 schema additionalProperties=false）
  - model3.json：FileReferences.Motions = {组: [{Id,File,Priority}...]}
    （**不是** 名字→路径 的平 map；File 是键名），必填 Moc+Textures。

我们的动画语义 → Cubism 参数映射与单位换算见 parts_map.json（导出时生成，
含每图层 {layer, part_name, node, params:[{src,param,kind,coef}]}）：
  - rot  → 角度参数（单位：度；编辑器默认范围 ±30，超出需调范围）
  - tx   → X 类参数，归一化 v/(W/2)（kind=px_x：参数值 1.0 ≡ 半画布宽 px）
  - ty   → Y 类参数，归一化 v/(H/2)（kind=px_y；eye 例外见表）
  - sc   → 自定义缩放参数，v-1
  - sc_y → 眼/眼睑：ParamEyeL/R = open（我们 1=睁 0.06=闭；编辑器 1/0，
            绑定时把最小值设 0.06 或做重映射）；其它部件：v-1
"""
import json
import os
import re
import shutil

import anim as A
import l2d_rig_core as C

# ---------------- 常量 ----------------
MOTION_FPS = 30.0
IDLE_DURATION = 4.6          # = 眨眼周期 _blink_s（4.6s）；呼吸/发摆在此周期
                             # 上有 ≤1.3° 的相位回卷跳变，靠 loop fade 掩盖
IDLE_GROUP = "idle"
Kf_GROUP = "keyframes"       # 工程里唯一一段用户关键帧序列（v2.11 时间轴形态）
GUIDE_NAME = "Cubism导入指引.txt"

# 节点动画属性 → Cubism 参数。tuple = (param_id, kind)
# kind: angle(度) / px_x(÷W/2) / px_y(÷H/2) / open01 / sc1(v-1) / raw(不换算)
NODE_PARAM_MAP = {
    "head":       {"rot": ("ParamAngleZ", "angle"),
                   "tx": ("ParamAngleX", "px_x"),
                   "ty": ("ParamAngleY", "px_x")},     # Y 位移按 H/2 归一
    "body":       {"rot": ("ParamBodyAngleZ", "angle"),
                   "tx": ("ParamBodyAngleX", "px_x"),
                   "ty": ("ParamBodyAngleY", "px_y")},
    "eye_left":   {"sc_y": ("ParamEyeLOpen", "open01"),
                   "tx": ("ParamEyeBallX", "px_x"),
                   "ty": ("ParamEyeBallY", "px_x"),
                   "rot": ("ParamEyeLRot", "angle")},
    "eye_right":  {"sc_y": ("ParamEyeROpen", "open01"),
                   "tx": ("ParamEyeBallX", "px_x"),
                   "ty": ("ParamEyeBallY", "px_x"),
                   "rot": ("ParamEyeRRot", "angle")},
    "lid_left":   {"sc_y": ("ParamEyeLOpen", "open01")},
    "lid_right":  {"sc_y": ("ParamEyeROpen", "open01")},
    "brow_left":  {"ty": ("ParamBrowLY", "px_x"),
                   "tx": ("ParamBrowLX", "px_x"),
                   "rot": ("ParamBrowLAngle", "angle")},
    "brow_right": {"ty": ("ParamBrowRY", "px_x"),
                   "tx": ("ParamBrowRX", "px_x"),
                   "rot": ("ParamBrowRAngle", "angle")},
    "hair_front": {"rot": ("ParamHairFront", "angle"),
                   "tx": ("ParamHairFrontX", "px_x"),
                   "ty": ("ParamHairFrontY", "px_x")},
    "hair_back":  {"rot": ("ParamHairBack", "angle"),
                   "tx": ("ParamHairBackX", "px_x"),
                   "ty": ("ParamHairBackY", "px_x")},
    "mouth":      {"rot": ("ParamMouthForm", "raw"),
                   "ty": ("ParamMouthOpenY", "open01")},
}
# 未收录节点/属性 → 自定义参数（Phase A 参考用；parts_map 会注明）
CUSTOM_TPL = "Param{node}{cap}"

# head.ty / 其它 Y 位移：kind px_x 实为“半轴归一”，Y 用 H/2 —— 见 _norm_val
PX_KINDS = {"px_x": 0, "px_y": 1}

EASE_BEZIER_K = {            # (k1,k2)：归一化值域 0..1 里的贝塞尔控制高度
    "in": (0.0, 1.0 / 3.0),          # B(τ)=τ²          ≡ u*u
    "out": (2.0 / 3.0, 1.0),         # B(τ)=1-(1-τ)²    ≡ easing in-out 的 out
    "inout": (0.0, 1.0),             # B(τ)=3τ²-2τ³     ≡ smoothstep，逐点相等
}


def _safe(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def _sanitize(s, fallback="part"):
    s = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", str(s or "").strip())
    s = s.strip("_")
    return s or fallback


def _part_name(layer):
    """CA 标签 → 编辑器部件名（去 + 前缀、空格转下划线）。"""
    return _sanitize(str(layer.get("tag") or layer.get("name") or "").lstrip("+"),
                     _sanitize(layer.get("name", "part")))


def _norm_val(v, kind, W, H):
    if kind == "angle":
        return float(v)
    if kind == "px_x":
        return float(v) / max(1.0, W / 2.0)
    if kind == "px_y":
        return float(v) / max(1.0, H / 2.0)
    if kind == "open01":
        return float(min(1.0, max(0.0, v)))
    if kind == "sc1":
        return float(v) - 1.0
    return float(v)


def _param_of(node, prop, W, H):
    """(node,prop) → (param_id, kind, coef_note)；无标准名走自定义 Param。"""
    m = NODE_PARAM_MAP.get(node, {}).get(prop)
    if m is None:
        pid = "Param%s_%s" % (_sanitize(node, "Node"), prop)
        kind = "angle" if prop == "rot" else ("open01" if prop == "sc_y" and node.startswith(("eye", "lid")) else ("sc1" if prop.startswith("sc") else ("px_x" if prop == "tx" else "px_y" if prop == "ty" else "raw")))
        return pid, kind, "custom"
    pid, kind = m
    # 表里 Y 位移统一写 px_x 占位，按属性名修正半轴
    if prop == "ty" and kind == "px_x" and not pid.endswith(("X",)):
        kind = "px_y"
    return pid, kind, ""


def _coef_note(kind, W, H):
    if kind == "angle":
        return "度（编辑器默认范围 ±30）"
    if kind == "px_x":
        return "1.0 = %.1f px（半画布宽）" % (W / 2.0)
    if kind == "px_y":
        return "1.0 = %.1f px（半画布高）" % (H / 2.0)
    if kind == "open01":
        return "0..1；注意我们眨眼闭合值=0.06 而非 0"
    if kind == "sc1":
        return "v-1；建议编辑器参数范围 ±0.5"
    return "原值直传"


# ---------------- 曲线编码 ----------------
def _r(x):
    return round(float(x), 6)


def _segs_from_keyframes(ks, kind, W, H):
    """一条关键帧轨道 → motion3 Segments（精确编码，无采样误差）。

    段映射：linear→0；hold→2(stepped)；in/out/inout→受限贝塞尔(1)，
    控制横坐标强制 t0+Δ/3、t0+2Δ/3，控制高度经 EASE_BEZIER_K 精确复现
    anim.py 的缓动函数（推导：τ=(t-t0)/Δt 且 x(τ) 线性 ⇒ 值曲线即
    (1-τ)³v0+3(1-τ)²τ·c1+3(1-τ)τ²·c2+τ³v1 ≡ ease(τ)，逐点相等）。
    """
    ks = A.sort_track(ks)
    if not ks:
        return [], 0, 0
    v0 = _norm_val(ks[0]["v"], kind, W, H)
    segs = [_r(ks[0]["t"]), _r(v0)]
    n_seg = n_pt = 0
    for a, b in zip(ks, ks[1:]):
        ta, tb = _safe(a, "t", default=0.0), _safe(b, "t", default=0.0)
        va = _norm_val(_safe(a, "v", default=0.0), kind, W, H)
        vb = _norm_val(_safe(b, "v", default=0.0), kind, W, H)
        if tb - ta <= 1e-9:
            continue
        ease = A.normalize_ease(a.get("ease"))
        if ease == "hold":
            segs += [2, _r(tb), _r(vb)]
            n_seg += 1
            n_pt += 1
        elif ease in EASE_BEZIER_K:
            d = tb - ta
            k1, k2 = EASE_BEZIER_K[ease]
            c1 = va + (vb - va) * k1
            c2 = va + (vb - va) * k2
            segs += [1, _r(ta + d / 3.0), _r(c1), _r(ta + 2.0 * d / 3.0),
                     _r(c2), _r(tb), _r(vb)]
            n_seg += 1
            n_pt += 3
        else:                                   # linear 兜底
            segs += [0, _r(tb), _r(vb)]
            n_seg += 1
            n_pt += 1
    return segs, n_seg, n_pt


def _segs_from_samples(ts, vs):
    """等间隔采样折线 → 线性段序列；相邻共线合并（省段数，不引入新误差）。"""
    pts = list(zip(ts, vs))
    if len(pts) < 2:
        if not pts:
            return [], 0, 0
        return [_r(pts[0][0]), _r(pts[0][1])], 0, 0
    keep = [pts[0]]
    for i in range(1, len(pts) - 1):
        t0, v0 = keep[-1]
        t1, v1 = pts[i]
        t2, v2 = pts[i + 1]
        # 中间点到保留点连线的相对偏离 <1e-6 视为共线（含同值平台）
        cross = (t1 - t0) * (v2 - v0) - (t2 - t0) * (v1 - v0)
        span = max(abs(v2 - v0), 1e-9)
        if abs(cross) / (abs(t2 - t0) * span + 1e-12) > 1e-6:
            keep.append(pts[i])
    keep.append(pts[-1])
    segs = [_r(keep[0][0]), _r(keep[0][1])]
    n_seg = n_pt = 0
    for _t0, _v0, (t1, v1) in zip(keep, keep[1:], keep[1:]):
        segs += [0, _r(t1), _r(v1)]
        n_seg += 1
        n_pt += 1
    return segs, n_seg, n_pt


def _curve(target, pid, segs):
    """一条 curve dict（带编辑器习惯的 Version/SegmentOptions 冗余键，
    运行时只读 Target/Id/Segments/Fade*，冗余无害，真实编辑器导出同样带）。"""
    return {"Version": 1, "Id": pid, "Target": target,
            "SegmentOptions": [], "Segments": segs}


def _motion(curves, duration, loop):
    n_seg = n_pt = 0
    for c in curves:                       # 复核每曲线的段/点数（自算，不信任调用方）
        s = c["Segments"]
        i, pts, segs = 2, 1, 0
        while i < len(s):
            typ = s[i]
            if typ == 1:
                if i + 6 >= len(s):
                    raise ValueError("segments 越界")
                pts += 3
                segs += 1
                i += 7
            elif typ in (0, 2, 3):
                if i + 2 >= len(s):
                    raise ValueError("segments 越界")
                pts += 1
                segs += 1
                i += 3
            else:
                raise ValueError("未知段类型 %r" % typ)
        n_seg += segs
        n_pt += pts
    dur = round(max(float(duration), 0.2), 4)
    return {
        "Version": 3,
        "Meta": {
            "Duration": dur,
            "Fps": MOTION_FPS,
            "Loop": bool(loop),
            "AreBeziersRestricted": True,
            "FadeInTime": 0.5,
            "FadeOutTime": 0.5,
            "CurveCount": len(curves),
            "TotalSegmentCount": n_seg,
            "TotalPointCount": n_pt,
            "UserDataCount": 0,
            "TotalUserDataSize": 0,
        },
        "Curves": curves,
    }


# ---------------- 待机循环（内置动画采样） ----------------
def _idle_curves(rig, W, H, fps=MOTION_FPS, duration=IDLE_DURATION):
    """按 l2d_rig_core.animation_for 的内置 sin 基线逐帧采样 → 曲线。
    与内置动画同源 ⇒ WebGAL 里的 idle ≈ 我们工作台里“无关键帧”的播放。"""
    n = max(4, int(round(duration * fps)))
    ts = [i / fps for i in range(n + 1)]
    ts[-1] = duration
    curves = []
    seen = set()
    for nd in rig.get("nodes", []):
        node = nd.get("node")
        if not node:
            continue
        props = list(NODE_PARAM_MAP.get(node, {}).keys())
        if not props:
            continue
        series = {p: [] for p in props}
        for t in ts:
            a = C.animation_for(node, t, nd, None)
            for p in props:
                series[p].append(a.get(p))
        for p in props:
            vals = series[p]
            if any(v is None for v in vals):
                continue
            if all(abs(v - vals[0]) < 1e-12 for v in vals):
                continue                     # 全程不动的属性不发曲线
            pid, kind, _ = _param_of(node, p, W, H)
            if pid in seen:
                continue
            seen.add(pid)
            vnorm = [_norm_val(v, kind, W, H) for v in vals]
            segs, _, _ = _segs_from_samples(ts, vnorm)
            if segs:
                curves.append(_curve("Parameter", pid, segs))
    return curves


# ---------------- 关键帧序列 ----------------
def _kf_curves(rig, tracks, W, H):
    """工程用户关键帧 → 曲线（精确编码）。返回 (curves, duration)。

    节点顺序：先 rig["nodes"]（已知节点、稳定序），再补 tracks 里
    rig 没有的节点（如 "other" 层关键帧），否则用户打在未知节点上的
    关键帧会被静默丢弃。"""
    curves = []
    seen = set()
    max_t = 0.0
    nodes_order = [nd.get("node") for nd in rig.get("nodes", []) if nd.get("node")]
    nodes_order += [n for n in sorted((tracks or {}).keys())
                    if n not in nodes_order]
    for node in nodes_order:
        ntr = (tracks or {}).get(node)
        if not isinstance(ntr, dict):
            continue
        for p in A.PARAMS:
            ks = ntr.get(p)
            if not (isinstance(ks, list) and ks):
                continue
            ks = A.sort_track(ks)
            max_t = max(max_t, _safe(ks[-1], "t", default=0.0))
            pid, kind, _ = _param_of(node, p, W, H)
            if pid in seen:
                continue                     # 同参数多来源：首个节点优先
            seen.add(pid)
            segs, _, _ = _segs_from_keyframes(ks, kind, W, H)
            if segs:
                curves.append(_curve("Parameter", pid, segs))
    return curves, max(0.2, max_t)


# ---------------- 部件不透明度占位 ----------------
def _opacity_curves(rig, duration):
    """每图层 PartOpacity 恒 1 占位（简报：无对应参数的部件“恒1占位即可”）。
    作用：a) 部件清单在 motion 里可见，绑定编辑器时好对名字；
          b) 显式声明可见性，避免 pose/物理把未驱动部件淡黑。"""
    out = []
    seen = set()
    for L in rig.get("_layers", []):
        pn = _part_name(L)
        if pn in seen:
            continue
        seen.add(pn)
        out.append(_curve("PartOpacity", pn, [0.0, 1.0, 0, _r(duration), 1.0]))
    return out


# ---------------- 产物写出 ----------------
def _write_json(obj, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _copy_textures(rig, tex_dir, W, H):
    """图层源 png 拷进 textures/，返回 parts_map 用记录列表。"""
    os.makedirs(tex_dir, exist_ok=True)
    records = []
    used = {}
    for L in rig.get("_layers", []):
        src = L.get("path")
        fn = _sanitize(L.get("tag") or L.get("name"), "layer")
        if fn in used:
            used[fn] += 1
            fn = "%s_%d" % (fn, used[fn])
        else:
            used[fn] = 0
        dst_name = fn + ".png"
        rel = "textures/" + dst_name
        copied = False
        if src and os.path.isfile(src):
            try:
                shutil.copy2(src, os.path.join(tex_dir, dst_name))
                copied = True
            except OSError:
                pass
        if not copied:                        # 源丢失：纯 alpha 占位图（不阻断导出）
            try:
                import PIL.Image as _I
                _I.new("RGBA", (max(1, W), max(1, H)), (0, 0, 0, 0)).save(
                    os.path.join(tex_dir, dst_name))
            except Exception:
                rel = ""
        records.append({"layer": L.get("name"), "tag": L.get("tag"),
                        "part_name": _part_name(L), "node": L.get("node"),
                        "texture": rel, "copied": copied,
                        "depth": L.get("depth")})
    return records


def _param_table(rig, W, H):
    """节点 → 参数映射（含单位系数），供 parts_map 与绑定参考。"""
    rows = []
    for nd in rig.get("nodes", []):
        node = nd.get("node")
        params = []
        prop_src = []
        for p in A.PARAMS:
            pid, kind, custom = _param_of(node, p, W, H)
            prop_src.append((p, pid, kind, custom))
        seen = set()
        for p, pid, kind, custom in prop_src:
            if pid in seen:
                continue
            seen.add(pid)
            params.append({"src": p, "param": pid, "kind": kind,
                           "coef": _coef_note(kind, W, H),
                           "custom": bool(custom)})
        rows.append({"node": node, "parent": nd.get("parent"),
                     "pivot": [round(float(nd["pivot"][0]), 3),
                               round(float(nd["pivot"][1]), 3)] if nd.get("pivot") else None,
                     "layers": nd.get("layers", []), "params": params})
    return rows


GUIDE_TEXT = """木偶工作室 → Live2D 迁移指引（Phase A 骨架）
====================================================
本目录是【运行时骨架 + 绑定参考】，不含 .moc3 —— moc3 是 Cubism 专有
格式，只能在 Cubism Editor 里导出。步骤：

1. 用 Cubism Editor 新建项目 →「编辑」→「多图层文件」导入同目录的
   <name>.psd（层名遵循 Adobe Character Animator 规范，Editor 会自动
   识别成对应部件分组）。
2. 打开 parts_map.json：每个图层 {layer, part_name, node, params}。
   按表给部件绑定参数：head 旋转→ParamAngleZ、眨眼→ParamEyeLOpen/...
   注意各 kind 的单位换算（angle=度；px_* 已按半画布归一：1.0=W/2 px；
   open01 里我们眨眼闭合值=0.06，编辑器把参数最小值设 0.06 即可复用）。
3. 自定义参数（ParamHairFront/ParamHairBack/Param*_tx 等）在 Editor 里
   按 parts_map 的 param 名新建，并给对应部件做好变形绑定（发丝摆动等）。
4. 文件 →「导出 mocos」，把 <你的模型>.moc3 放进本目录，并把导出的
   贴图图集覆盖/替换 textures/（或改 model3.json 的 Textures 列表指
   向实际图集文件；Moc 字段也改成实际 moc3 文件名）。
   motions/*.motion3.json 已按官方 motion3 规范生成：
     idle      = 工作台内置呼吸/眨眼/摆头的 30fps 采样，循环
     keyframes = 时间轴上打的用户关键帧序列（缓动精确转贝塞尔）
   绑好参数后这两个 motion 会直接驱动你的新模型（参数名对得上即可）。
5. WebGAL：把整个目录放进 WebGAL 的 live2d/ 资源目录，脚本里用
     showLive2d:live2d/<目录名>/<name>.model3.json
   （运行时为 pixi-live2d-display Cubism4；Target 字符串与段编码已按
   其解析器验证：Parameter/PartOpacity、线性0/贝塞尔1/阶跃2、
   AreBeziersRestricted=true 控制点三等分）。

工作台侧提醒：本目录由 l2d_export.py 生成，重跑导出会覆盖
model3.json / motions / parts_map.json / textures，moc3 与手工调整不会被覆盖
（Editor 工程文件请另行保存）。
"""


# ---------------- 公开 API ----------------
def export_project(rig, out_dir, psd_path=None, name=None,
                   duration=None, fps=None, tracks=None):
    """从内存 rig dict（l2d_rig_core.build_rig 产物 + rig["keyframes"]）导出。

    返回 (ok, msg)。WebGAL/Cubism 骨架见模块 docstring。
    psd_path：给了且存在 → 拷成 <name>.psd；不存在 → 照常导出并在 msg 注明。
    """
    notes = []
    try:
        if not isinstance(rig, dict) or "nodes" not in rig:
            return False, "rig 不是 build_rig 产物 dict"
        W = int(rig.get("width") or 0)
        H = int(rig.get("height") or 0)
        if W <= 0 or H <= 0:
            return False, "rig 尺寸非法 %dx%d" % (W, H)
        name = _sanitize(name or rig.get("name") or "muppet", "muppet")
        tr = tracks if tracks is not None else rig.get("keyframes")
        if not isinstance(tr, dict):
            tr = {}

        os.makedirs(out_dir, exist_ok=True)
        mot_dir = os.path.join(out_dir, "motions")
        tex_dir = os.path.join(out_dir, "textures")

        # ---- motions ----
        group_refs = {}
        idle = _motion(_idle_curves(rig, W, H) +
                       _opacity_curves(rig, IDLE_DURATION),
                       IDLE_DURATION, loop=True)
        _write_json(idle, os.path.join(mot_dir, "idle.motion3.json"))
        group_refs[IDLE_GROUP] = [{"Id": "", "Priority": 1,
                                   "File": "motions/idle.motion3.json"}]
        if A.has_anim(tr):
            curves, kdur = _kf_curves(rig, tr, W, H)
            if curves:
                km = _motion(curves, kdur, loop=False)
                _write_json(km, os.path.join(mot_dir, "keyframes.motion3.json"))
                group_refs[Kf_GROUP] = [{"Id": "", "Priority": 1,
                                         "File": "motions/keyframes.motion3.json"}]
            else:
                notes.append("tracks 非空但未产出曲线（全部未知节点?）")
        else:
            notes.append("工程无用户关键帧：仅生成 idle")

        # ---- textures ----
        recs = _copy_textures(rig, tex_dir, W, H)
        missing = [r["layer"] for r in recs if not r.get("copied")]
        if missing:
            notes.append("%d 图层源文件缺失已用占位图: %s" %
                         (len(missing), ",".join(map(str, missing[:3]))))
        textures = [r["texture"] for r in recs if r.get("texture")]

        # ---- parts_map ----
        pm = {
            "_units": {"angle": _coef_note("angle", W, H),
                       "px_x": _coef_note("px_x", W, H),
                       "px_y": _coef_note("px_y", W, H),
                       "open01": _coef_note("open01", W, H),
                       "sc1": _coef_note("sc1", W, H)},
            "_blink_note": "眨眼闭眼值=0.06（core._blink_s），编辑器 EyeLOpen 绑定时把最小值设 0.06 或重映射",
            "_texture_note": "textures/ 为逐图层源图（参考）；Editor 导出 moc3 时生成自己的图集，请以实际图集替换 model3.json 的 Textures",
            "draw_order": list(rig.get("draw_order", [])),
            "layers": recs,
            "nodes": _param_table(rig, W, H),
        }
        _write_json(pm, os.path.join(out_dir, "parts_map.json"))

        # ---- model3.json ----
        eye_ids = []
        for row in pm["nodes"]:
            for p in row["params"]:
                if p["param"] in ("ParamEyeLOpen", "ParamEyeROpen") \
                        and p["param"] not in eye_ids:
                    eye_ids.append(p["param"])
        groups = []
        if eye_ids:
            groups.append({"Target": "Parameter", "Name": "EyeBlink",
                           "Ids": eye_ids})
        model3 = {
            "Version": 3,
            "FileReferences": {
                "Moc": "%s.moc3" % name,             # 占位：Editor 导出后改名/替换
                "Textures": textures,
                "Physics": "",
                "DisplayModels": ["%s.cdi3.json" % name],
                "Motions": group_refs,
            },
            "Groups": groups,
            "HitAreas": [],
        }
        _write_json(model3, os.path.join(out_dir, "%s.model3.json" % name))

        # ---- psd ----
        if psd_path:
            if os.path.isfile(psd_path):
                shutil.copy2(psd_path, os.path.join(out_dir, "%s.psd" % name))
            else:
                notes.append("psd_path 不存在，跳过拷贝: %s" % psd_path)

        # ---- guide ----
        with open(os.path.join(out_dir, GUIDE_NAME), "w", encoding="utf-8-sig") as f:
            f.write(GUIDE_TEXT.replace("<name>", name))

        msg = "已导出 %s：%d curves(idle)+%s, %d textures" % (
            name, idle["Meta"]["CurveCount"],
            "keyframes" if Kf_GROUP in group_refs else "无kf",
            len(textures))
        if notes:
            msg += "；备注: " + "；".join(notes)
        return True, msg
    except Exception as e:
        return False, "导出异常 %s: %s" % (type(e).__name__, str(e)[:160])


def read_project_rig(proj_dir):
    """从工程目录还原运行期 rig dict（语义与 l2d_rig_qt.load_dir 一致：
    load_layers → build_rig → 注入 keyframes/duration/fps）。"""
    meta = os.path.join(proj_dir, "layers_meta.json")
    if not os.path.isfile(meta):
        try:
            cands = [f for f in os.listdir(proj_dir) if f.endswith("_layers.json")]
        except OSError:
            cands = []
        if not cands:
            raise ValueError("目录无 layers_meta.json/*_layers.json: %s" % proj_dir)
        meta = os.path.join(proj_dir, cands[0])
    layers, W, H = C.load_layers(meta, proj_dir)
    rig = C.build_rig(layers, W, H)
    dur, f = IDLE_DURATION, 30.0
    tracks = {}
    proj_json = os.path.join(proj_dir, "rig_project.json")
    name = os.path.basename(os.path.normpath(proj_dir))
    if os.path.isfile(proj_json):
        try:
            p = json.load(open(proj_json, encoding="utf8"))
        except Exception:
            p = {}
        if isinstance(p.get("keyframes"), dict):
            tracks = A.clone_tracks(p["keyframes"])
        an = p.get("anim")
        if isinstance(an, dict):
            d2 = an.get("duration")
            if isinstance(d2, (int, float)) and 0.2 <= float(d2) <= 600:
                dur = float(d2)
            f2 = an.get("fps")
            if isinstance(f2, int) and 1 <= f2 <= 120:
                f = float(f2)
        if p.get("name"):
            name = str(p["name"])
    rig["keyframes"] = tracks
    return rig, {"duration": dur, "fps": f, "name": name}


def export_project_dir(proj_dir, out_dir, psd_path=None, name=None):
    """主入口：吃工程目录（含 layers_meta.json / rig_project.json），
    产出 WebGAL/Cubism 骨架到 out_dir。返回 (ok, msg)。"""
    try:
        rig, meta = read_project_rig(proj_dir)
    except Exception as e:
        return False, "读取工程失败: %s" % str(e)[:160]
    # 有用户关键帧时，关键帧 motion 用工程 duration 上限截尾
    return export_project(rig, out_dir, psd_path=psd_path,
                          name=name or meta["name"],
                          duration=meta["duration"], fps=meta["fps"],
                          tracks=rig["keyframes"])


# ---------------- motion3 求值（验收对拍用，也便于第三方自检） ----------------
def eval_motion(motion_obj, param_id, t, target="Parameter"):
    """在 t 处求 motion3.json 曲线值；无此曲线返回 None。
    实现与 Cubism 运行时逐分支对齐（线性/贝塞尔(de Casteljau)/阶跃）。"""
    for c in motion_obj.get("Curves", []):
        if c.get("Target") == target and c.get("Id") == param_id:
            return _eval_segments(c.get("Segments", []), t)
    return None


def _eval_segments(s, t):
    if len(s) < 2:
        return None
    i = 2
    p0t, p0v = s[0], s[1]
    if t < p0t:
        return p0v
    while i < len(s):
        typ = s[i]
        if typ == 1:
            t1, v1, t2, v2, t3, v3 = s[i + 1:i + 7]
            if t <= t3 or i + 7 >= len(s):
                tt = 0.0 if t3 <= p0t else (t - p0t) / (t3 - p0t)
                tt = min(1.0, max(0.0, tt))
                a = (1 - tt) ** 3 * p0v + 3 * (1 - tt) ** 2 * tt * v1 \
                    + 3 * (1 - tt) * tt ** 2 * v2 + tt ** 3 * v3
                return a
            p0t, p0v = t3, v3
            i += 7
        elif typ == 0:
            t1, v1 = s[i + 1], s[i + 2]
            if t <= t1 or i + 3 >= len(s):
                k = 0.0 if t1 <= p0t else (t - p0t) / (t1 - p0t)
                return p0v + (v1 - p0v) * min(1.0, max(0.0, k))
            p0t, p0v = t1, v1
            i += 3
        elif typ == 2:
            t1, v1 = s[i + 1], s[i + 2]
            if t < t1:
                return p0v
            p0t, p0v = t1, v1
            i += 3
        elif typ == 3:
            t1, v1 = s[i + 1], s[i + 2]
            if t <= t1:
                return v1
            p0t, p0v = t1, v1
            i += 3
        else:
            return None
    return p0v
