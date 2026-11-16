# -*- coding: utf-8 -*-
"""视频工程导出与逐帧合成（v3.0.0 第一阶段）。

- compose_frame(proj, t, src_get)：按轨道时间线合成一帧 RGB（预览与导出共用，
  保证"所见即所得"是同一条代码路径）。
- VideoExportWorker(QThread)：cv2.VideoWriter 写无声 mp4 -> 有音轨时
  ffmpeg 合流（候选路径与 l2d_rig_qt 一致，但本模块不 import 它，常量复制）。
- 混音：音频轨 clip + 视频 clip 自带音（能探到就混）按 start 摆放相加，
  溢出截断、int16 饱和。

约定：第一阶段预览**无声**（QTimer 逐帧合成画面），声音在导出里体现。
"""
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import wave

import numpy as np

import video_model as V

# ---------------- 字幕烧录（v3.5.5：WebGAL 引擎对话框皮肤 + 打字机） ----------------
# 参照 = 引擎真实渲染截图（textbox.scss 北风主题，运行时确认生效）。
# 布局基准 2560×1440（入口 index.html 对 #root 强制 scale = 视口高/1440），
# scss 里全部 px 值按 scale 换算到画布：
#   面板：全宽贴底，高 430px@1440；118deg 蓝灰三色渐变
#   (147,165,207,.9)→(182,198,222,.82)@48%→(228,239,233,.82)，
#   叠双向 mask 渐隐（to bottom 0.04→0.28@10%→1@75%；to right 0.08→0.28@10%→1@42%）
#   文字区：左缩进 490px@2560，右 50px@1440，上 (1em+100px)，下 70px
#   正文：白字 + #2e2312 描边 0.12em + 右下阴影 + 字距 0.2em，左对齐，≤4 行
#   名字：85% 字号，面板顶部，下方白色渐隐分割线（min-width 50%）
#   打字机：逐字浮现（~60ms/字，自适应压缩保证打字+渐显 ≤ dur 的 70%），
#   单字 0.5s 渐显（引擎 TextDelayShow 的近似）。
# 性能：面板/布局/整句图层缓存；打字期间逐字符绘制，打完走整句缓存。

_SUB_PANEL_CACHE = {}      # (W, H) -> (y0, band)
_SUB_LAYOUT_CACHE = {}     # (W, H, text) -> [(ch, x, y), ...]
_SUB_TEXT_CACHE = {}       # (W, H, text) -> (y0, band)  整句（打完后的稳定态）
_SUB_CACHE_MAX = 24
_FONT_CACHE = {}

_TYPEWRITER_MS = 60        # 打字机每字符毫秒
_TYPEWRITER_FADE = 0.5     # 单字渐显秒数
_TEXT_BASE_PX = 55         # 正文字号（1440 布局基准 px，从引擎截图实测）

_FONT_CANDIDATES = (
    "ResourceHanRoundedCN-Regular",     # 引擎正文字体：资源圆体
    "msyh.ttc", "msyhbd.ttc", "simhei.ttf",
)

_APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _pil():
    """PIL 惰性加载；缺 Pillow 返回 None（无字幕降级，不崩导出）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        return Image, ImageDraw, ImageFont
    except Exception:
        return None


def _font_files():
    """runtime 字体优先（资源圆体 = 引擎同款），再退系统雅黑/黑体。"""
    rt = os.path.join(_APP_DIR, "templates", "webgal_runtime", "assets")
    out = []
    try:
        if os.path.isdir(rt):
            out.extend(os.path.join(rt, f) for f in sorted(os.listdir(rt))
                       if f.lower().startswith("resourcehanrounded")
                       and f.lower().endswith((".ttf", ".otf", ".ttc")))
    except OSError:
        pass
    out.extend((r"C:\Windows\Fonts\msyh.ttc",
                r"C:\Windows\Fonts\msyhbd.ttc",
                r"C:\Windows\Fonts\simhei.ttf"))
    return out


def _get_font(px, bold=False):
    key = (int(px), bool(bold))
    f = _FONT_CACHE.get(key)
    if f is not None:
        return f
    mod = _pil()
    if mod is None:
        return None
    ImageFont = mod[2]
    px = max(1, int(px))
    f = None
    if bold:
        for p in _font_files():
            if "msyhbd" in p:
                try:
                    f = ImageFont.truetype(p, px)
                    break
                except Exception:
                    pass
    if f is None:
        for p in _font_files():
            try:
                f = ImageFont.truetype(p, px)
                break
            except Exception:
                continue
    if f is None:
        try:
            f = ImageFont.load_default(size=px)
        except Exception:
            f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _engine_scale(H):
    return H / 1440.0


def _body_px(H):
    return max(16, min(110, int(round(H * _TEXT_BASE_PX / 1440))))


def _blend_band(canvas, y0, band, x0=0):
    """band（RGBA）alpha 混合进 canvas 的 y0..、x0.. 区域（就地）。"""
    a = band[..., 3:4].astype(np.float32) / 255.0
    reg = canvas[y0:y0 + band.shape[0], x0:x0 + band.shape[1]]
    reg[:] = (band[..., :3] * a + reg * (1 - a)).astype(np.uint8)


def _panel_layer(W, H):
    """底部渐变面板（textbox.scss TextBox_Background 复刻）。缓存 (W,H)。"""
    key = (W, H)
    hit = _SUB_PANEL_CACHE.get(key)
    if hit is not None:
        return hit
    scale = _engine_scale(H)
    hp = max(8, int(round(430 * scale)))
    y0 = H - hp
    import math
    ang = math.radians(118.0)
    ux, uy = math.sin(ang), -math.cos(ang)
    gx, gy = np.meshgrid(np.linspace(0, 1, W), np.linspace(0, 1, hp))
    proj = (gx - 0.5) * ux + (gy - 0.5) * uy
    lo, hi = float(proj.min()), float(proj.max())
    t = (proj - lo) / max(1e-6, hi - lo)
    c0 = np.array([147, 165, 207], np.float32); a0 = 0.90
    c1 = np.array([182, 198, 222], np.float32); a1 = 0.82
    c2 = np.array([228, 239, 233], np.float32); a2 = 0.82
    t48 = np.clip(t / 0.48, 0, 1)[..., None]
    rgb = c0 * (1 - t48) + c1 * t48
    a = a0 + (a1 - a0) * t48[..., 0]
    t52 = np.clip((t - 0.48) / 0.52, 0, 1)[..., None]
    rgb = rgb * (1 - t52) + c2 * t52
    a = a + (a2 - a) * t52[..., 0]
    m1 = np.interp(gy.ravel(), [0.0, 0.10, 0.75, 1.0],
                   [0.04, 0.28, 1.0, 1.0]).reshape(hp, W)
    m2 = np.interp(gx.ravel(), [0.0, 0.10, 0.42, 1.0],
                   [0.08, 0.28, 1.0, 1.0]).reshape(hp, W)
    alpha = np.clip(a * m1 * m2 * 255.0, 0, 255)
    band = np.dstack([rgb.astype(np.uint8),
                      alpha.astype(np.uint8)])
    out = (y0, band)
    if len(_SUB_PANEL_CACHE) >= 4:
        _SUB_PANEL_CACHE.clear()
    _SUB_PANEL_CACHE[key] = out
    return out


def _dialog_layout(W, H, text):
    """台词排版：字距 0.2em 逐字符定位 + 贪心换行（≤4 行）。
    返回 [(ch, x, y), ...]（y = 行顶），缓存 (W,H,text)。"""
    key = (W, H, text)
    hit = _SUB_LAYOUT_CACHE.get(key)
    if hit is not None:
        return hit
    mod = _pil()
    body = _get_font(_body_px(H), bold=False)
    if mod is None or body is None:
        return []
    scale = _engine_scale(H)
    x0 = int(round(490 * W / 2560))
    x1 = W - int(round(50 * scale))
    max_w = max(50, x1 - x0)
    sp = 0.2 * _body_px(H)
    y_first = H - int(round(430 * scale)) + int(round((_TEXT_BASE_PX + 100) * scale))
    line_h = sum(body.getmetrics()) or int(_body_px(H) * 1.35)
    out = []
    lines = 0
    x, y = x0, y_first
    for ch in text:
        if ch == "\n":
            lines += 1
            y += line_h
            x = x0
            if lines >= 4:
                break
            continue
        try:
            w = body.getlength(ch) + sp
        except Exception:
            w = _body_px(H)
        if x > x0 and x + w > x1:
            lines += 1
            y += line_h
            x = x0
            if lines >= 4:
                break
        out.append((ch, x, y))
        x += w
    if len(_SUB_LAYOUT_CACHE) >= 48:
        _SUB_LAYOUT_CACHE.pop(next(iter(_SUB_LAYOUT_CACHE)), None)
    _SUB_LAYOUT_CACHE[key] = out
    return out


def _draw_engine_text(d, x, y, ch, font, body_px, alpha=1.0):
    """引擎文字三层：右下阴影 → #2e2312 描边（0.12em）→ 白色本体。"""
    stroke = max(2, int(round(0.12 * body_px)))
    soff = max(1, int(round(0.05 * body_px)))
    fill = (255, 255, 255, int(255 * alpha))
    sf = (0, 0, 0, int(150 * alpha))
    st = (46, 35, 18, int(255 * alpha))
    d.text((x + soff, y + soff), ch, font=font, fill=sf, anchor="la",
           stroke_width=stroke, stroke_fill=sf)
    d.text((x, y), ch, font=font, fill=fill, anchor="la",
           stroke_width=stroke, stroke_fill=st)


def _name_band(W, H, who):
    """名字区（面板顶部）：85% 字号 + 白色渐隐分割线。缓存 (W,H,who)。"""
    key = (W, H, who)
    hit = _SUB_TEXT_CACHE.get(("name",) + key)
    if hit is not None:
        return hit
    mod = _pil()
    if mod is None or not who:
        return (0, None)
    Image, ImageDraw, _ = mod
    scale = _engine_scale(H)
    name_px = max(14, int(round(_body_px(H) * 0.85)))
    font = _get_font(name_px, bold=True)
    if font is None:
        return (0, None)
    hp = int(round(430 * scale))
    panel_top = H - hp
    x0 = int(round(490 * W / 2560))
    top = panel_top + int(round(26 * scale))
    h = int(round(90 * scale))
    y0 = panel_top + int(round(26 * scale)) - 8
    bh = h + 24
    img = Image.new("RGBA", (W, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    _draw_engine_text(d, x0, top - y0, who, font, name_px, alpha=1.0)
    # 分割线：showName 底部 2px 白，border-image 渐隐（5% 处最白 → 50% 消失）
    ly = top + h - int(round(2 * scale)) - y0
    lx1 = x0 + int(round((W - x0) * 0.5))
    seg = max(1, lx1 - x0)
    for i in range(seg):
        t = i / seg
        a = int(255 * (0.95 if t < 0.05 else max(0.0, 1.0 - (t - 0.05) / 0.45)) * 0.3)
        if a <= 0:
            continue
        yy = ly + int(round(2 * scale))
        if 0 <= yy < bh:
            d.point((x0 + i, yy), fill=(255, 255, 255, a))
    band = np.asarray(img).copy()
    img.close()
    out = (y0, band)
    if len(_SUB_TEXT_CACHE) >= _SUB_CACHE_MAX:
        _SUB_TEXT_CACHE.pop(next(iter(_SUB_TEXT_CACHE)), None)
    _SUB_TEXT_CACHE[("name",) + key] = out
    return out


def _full_text_band(W, H, text):
    """打字完成后的整句图层。缓存 (W,H,text)。"""
    key = (W, H, text)
    hit = _SUB_TEXT_CACHE.get(key)
    if hit is not None:
        return hit
    mod = _pil()
    body = _get_font(_body_px(H), bold=False)
    if mod is None or body is None:
        return (0, None)
    Image, ImageDraw, _ = mod
    chars = _dialog_layout(W, H, text)
    if not chars:
        return (0, None)
    body_px = _body_px(H)
    stroke = max(2, int(round(0.12 * body_px)))
    soff = max(1, int(round(0.05 * body_px)))
    xs = [c[1] for c in chars]
    ys = [c[2] for c in chars]
    x0 = max(0, int(min(xs) - stroke - soff - 2))
    x1 = min(W, int(max(xs) + max(body.getlength(c[0]) for c in chars)
                     + stroke + soff + 2))
    y0 = max(0, int(min(ys) - stroke - soff - 2))
    y1 = min(H, int(max(ys) + int(sum(body.getmetrics())) + stroke + soff + 2))
    img = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for ch, x, y in chars:
        _draw_engine_text(d, x - x0, y - y0, ch, body, body_px, alpha=1.0)
    band = np.asarray(img).copy()
    img.close()
    out = (y0, x0, band)
    if len(_SUB_TEXT_CACHE) >= _SUB_CACHE_MAX:
        _SUB_TEXT_CACHE.pop(next(iter(_SUB_TEXT_CACHE)), None)
    _SUB_TEXT_CACHE[key] = out
    return out


def _draw_char_now(canvas, x, y, ch, alpha):
    """打字中的单个渐显字符（小图层局部混合）。"""
    mod = _pil()
    body = _get_font(_body_px(canvas.shape[0]), bold=False)
    if mod is None or body is None or alpha <= 0.02:
        return
    Image, ImageDraw, _ = mod
    body_px = _body_px(canvas.shape[0])
    stroke = max(2, int(round(0.12 * body_px)))
    soff = max(1, int(round(0.05 * body_px)))
    bb = body.getbbox(ch) or (0, 0, int(body_px), int(body_px * 1.2))
    pad = stroke + soff + 2
    bw = int(bb[2] - bb[0]) + pad * 2
    bh = int(sum(body.getmetrics())) + pad * 2
    if bw <= 0 or bh <= 0:
        return
    tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    _draw_engine_text(d, pad - bb[0], pad, ch, body, body_px, alpha=alpha)
    arr = np.asarray(tile)
    H, W = canvas.shape[:2]
    px, py = int(x) - pad, int(y) - pad
    sx0, sy0 = max(0, px), max(0, py)
    sx1, sy1 = min(W, px + bw), min(H, py + bh)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    sub = arr[sy0 - py:sy1 - py, sx0 - px:sx1 - px]
    sub_a = sub[..., 3:4].astype(np.float32) / 255.0
    reg = canvas[sy0:sy1, sx0:sx1]
    reg[:] = (sub[..., :3] * sub_a + reg * (1 - sub_a)).astype(np.uint8)


def _typewriter_per(text, dur):
    n = max(1, len(text))
    p = _TYPEWRITER_MS / 1000.0
    budget = max(0.3, float(dur) * 0.7 - _TYPEWRITER_FADE)
    return min(p, budget / n)


def _draw_dialogue(canvas, proj, t, aid):
    """把 t 时刻活跃台词画到 canvas（引擎皮肤：面板 + 名字 + 打字机）。"""
    mod = _pil()
    if mod is None:
        return
    try:
        dlg = V.act_view(proj, aid).get("dialogue") or []
    except Exception:
        return
    eps = 1e-6
    active = None
    for d in dlg:
        if not isinstance(d, dict):
            continue
        try:
            t0 = float(d.get("t"))
            dur = float(d.get("dur"))
        except Exception:
            continue
        if t0 - eps <= t < t0 + dur - eps:
            active = d
    if active is None:
        return
    who = str(active.get("who") or "")
    text = str(active.get("text") or "")
    if not text:
        return
    H, W = canvas.shape[:2]
    y0, band = _panel_layer(W, H)
    if band is not None and band.size:
        _blend_band(canvas, y0, band)
    if who:
        ny0, nband = _name_band(W, H, who)
        if nband is not None and nband.size:
            _blend_band(canvas, ny0, nband)
    chars = _dialog_layout(W, H, text)
    if not chars:
        return
    n = len(chars)
    t_el = t - float(active.get("t"))
    per = _typewriter_per(text, float(active.get("dur") or 3.0))
    fade = _TYPEWRITER_FADE
    if t_el >= (n - 1) * per + fade:
        sy0, sx0, sband = _full_text_band(W, H, text)
        if sband is not None and sband.size:
            _blend_band(canvas, sy0, sband, sx0)
        return
    vis = min(n, int(t_el / per) + 1)
    for i in range(vis):
        ch, x, y = chars[i]
        a = min(1.0, max(0.0, (t_el - i * per) / fade))
        if a > 0.02:
            _draw_char_now(canvas, x, y, ch, a)


def clear_subtitle_cache():
    """换工程/换幕时清台词图层缓存（测试与外层管理用）。"""
    _SUB_PANEL_CACHE.clear()
    _SUB_LAYOUT_CACHE.clear()
    _SUB_TEXT_CACHE.clear()
    _FONT_CACHE.clear()


# ---------------- 滤镜映射（v3.5.6 立项：setTransform → numpy 逐帧） ----------------
# 数据链：webgal_import 把 setTransform 原样留档到 proj["effects"]（[{t, cmd}]），
# cmd 形如 `setTransform:{...json 参数...} -target=bg-main -duration=2000`。
# 引擎语义：同 target 的新 setTransform 覆盖旧状态并持续到再被覆盖；
# -duration 是过渡动画时长（本版忽略过渡，直接切换）。
# 视频分层合成：背景层（cover/BG 轨，黑底）→ bg-main 滤镜 → 立绘层（FIG-*
# 轨，透明底）→ fig-* 滤镜 → 其余轨按列表序叠上 → 字幕。
# 无活跃滤镜时走旧逐轨叠加路径（逐像素零 diff 保证）。
# 参数语义按常见 pixi filter 近似；验收 = 引擎截图对照（webgal_engine_shot.py）。

_FX_PARSE_CACHE = {}


def _parse_effect_cmd(cmd):
    """`setTransform:{json} -target=xx -duration=N` → (target, params)；非法 None。"""
    key = str(cmd or "")
    hit = _FX_PARSE_CACHE.get(key)
    if hit is not None:
        return hit if hit != "bad" else None
    try:
        head, rest = key.split(":", 1)
        if head.strip() != "setTransform":
            _FX_PARSE_CACHE[key] = "bad"
            return None
        rest = rest.strip()
        end = rest.find(" -")
        js = rest[:end] if end >= 0 else rest.rstrip(";").strip()
        params = json.loads(js)
        target = "bg-main"
        m = re.search(r"-target=(\S+)", rest)
        if m:
            target = m.group(1).rstrip(";").strip()
        if not isinstance(params, dict):
            raise ValueError
        out = (target, dict(params))
    except Exception:
        _FX_PARSE_CACHE[key] = "bad"
        return None
    if len(_FX_PARSE_CACHE) > 512:
        _FX_PARSE_CACHE.clear()
    _FX_PARSE_CACHE[key] = out
    return out


def _active_fx(proj, t, aid):
    """t 时刻生效的滤镜状态 {target: params}（t 之前最后一条同 target 覆盖）。"""
    out = {}
    try:
        eff = proj.get("effects") or []
    except Exception:
        return out
    eps = 1e-6
    for ef in eff:
        if not isinstance(ef, dict):
            continue
        try:
            et = float(ef.get("t"))
        except Exception:
            continue
        if et > t + eps:
            continue
        parsed = _parse_effect_cmd(ef.get("cmd"))
        if parsed:
            out[parsed[0]] = parsed[1]
    return out


def _fx_num(v, d=0.0):
    if isinstance(v, bool):
        return d
    try:
        return float(v)
    except Exception:
        return d


def _fx_glitch(rgb, ctx):
    """glitchFilm：随机行块水平错位（按帧种子确定性）。"""
    rng = np.random.default_rng(ctx["seed"] ^ 0x5EED)
    h = rgb.shape[0]
    for _ in range(6):
        y = int(rng.integers(0, max(1, h - 16)))
        hh = int(rng.integers(3, 16))
        dx = int(rng.integers(-24, 25))
        if dx:
            rgb[y:y + hh] = np.roll(rgb[y:y + hh], dx, axis=1)


def _fx_rgb_shift(rgb, ctx):
    """rgbFilm：R/B 通道水平分离（色差重影）。"""
    off = 2 + int(round(2 * math.sin(ctx["t"] * 9.0)))
    if off == 0:
        return
    rgb[..., 0] = np.roll(rgb[..., 0], off, axis=1)
    rgb[..., 2] = np.roll(rgb[..., 2], -off, axis=1)


def _fx_noise(rgb, ns, ctx):
    rng = np.random.default_rng(ctx["seed"] ^ 0xC0FFEE)
    rgb[:] += rng.normal(0.0, 30.0 * ns, rgb.shape).astype(np.float32)


def _fx_oldfilm(rgb, ctx):
    """oldFilm：亮度轻微闪烁（老电影颤动）。噪点由 noise 参数单独控制。"""
    rgb[:] *= 0.93 + 0.07 * math.sin(ctx["t"] * 12.0)


_VIGN_CACHE = {}


def _fx_vignette(rgb, va):
    key = (rgb.shape[0], rgb.shape[1], round(va, 3))
    fall = _VIGN_CACHE.get(key)
    if fall is None:
        h, w = rgb.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        cx, cy = w / 2.0, h / 2.0
        r = np.sqrt(((xx - cx) / max(1.0, cx)) ** 2 +
                    ((yy - cy) / max(1.0, cy)) ** 2) / 1.4142
        fall = 1.0 - min(1.0, max(0.0, va)) * np.clip((r - 0.55) / 0.45, 0, 1)
        fall = fall.astype(np.float32)[..., None]
        if len(_VIGN_CACHE) > 16:
            _VIGN_CACHE.clear()
        _VIGN_CACHE[key] = fall
    rgb[:] *= fall


_GAMMA_LUTS = {}

# sepia 基础矩阵（pixi SepiaFilter 色调）。引擎只偏移一部分原色，
# 全强度混入会吃掉原有色相（对照实测），在 _fx_color 里折半混入。
_FX_SEPIA_M = np.array([[0.393, 0.769, 0.189],
                        [0.349, 0.686, 0.168],
                        [0.272, 0.534, 0.131]], np.float32)


def _gamma_lut(g):
    """gamma 256 级 LUT（缓存；255*(i/255)^g，替代全帧 np.power）。"""
    g = max(0.1, g)
    lut = _GAMMA_LUTS.get(g)
    if lut is None:
        if len(_GAMMA_LUTS) > 32:
            _GAMMA_LUTS.clear()
        i = np.arange(256, dtype=np.float32)
        lut = (255.0 * np.power(np.clip(i / 255.0, 0.0, 1.0), g)
               ).astype(np.float32)
        _GAMMA_LUTS[g] = lut
    return lut


def _fx_color(rgb, p):
    """sepia / brightness / contrast / saturation / gamma / colorRGB 调色链。

    v3.5.7 提速：①sepia 折半混入本身也是仿射
    （rgb*(1-0.5k) + (rgb@S.T*1.07+8)*(0.5k) = rgb@M_sep.T + c_sep），与
    brightness/contrast/colorRGB/saturation 全部合并成单个 3x3 矩阵 +
    偏移，一次 matmul 替代原来的 5 趟全帧扫描；②gamma 用 256 级 LUT
    查表替代全帧 np.power。gamma 从链中间挪到仿射之后（仅 sepia/gamma
    **同时**使用时像素与 v3.5.6 有微小观感差，其余情况逐像素等价）。"""
    sp = _fx_num(p.get("sepia"))
    br = _fx_num(p.get("brightness"), 1.0)
    ct = _fx_num(p.get("contrast"), 1.0)
    cr = _fx_num(p.get("colorRed"), 255.0) / 255.0
    cg = _fx_num(p.get("colorGreen"), 255.0) / 255.0
    cb = _fx_num(p.get("colorBlue"), 255.0) / 255.0
    sa = _fx_num(p.get("saturation"), 1.0)
    if sp <= 0 and br == 1.0 and ct == 1.0 \
            and (cr, cg, cb) == (1.0, 1.0, 1.0) and sa == 1.0:
        ga = _fx_num(p.get("gamma"), 1.0)
        if ga != 1.0:
            rgb[:] = _gamma_lut(ga)[
                np.clip(rgb, 0.0, 255.0).astype(np.uint8)]
            np.clip(rgb, 0, 255, out=rgb)
        return
    M = np.eye(3, dtype=np.float32)
    c = np.zeros(3, np.float32)
    if sp > 0:                          # sepia：折半混入褐色调（引擎校准）
        k = min(1.0, sp) * 0.5
        M = M * (1.0 - k) + (_FX_SEPIA_M.T * 1.07) * k
        c += 8.0 * k
    if br != 1.0:
        M *= br
        c *= br                         # 偏移同受后续算子作用
    if ct != 1.0:                       # (x-127.5)*ct+127.5 = ct*x + (1-ct)*127.5
        M *= ct
        c *= ct
        c += (1.0 - ct) * 127.5
    M[0] *= cr                          # colorRGB：逐通道乘 → 行缩放
    M[1] *= cg
    M[2] *= cb
    c[0] *= cr                          # 偏移同受通道乘作用（v3.5.7 修复：
    c[1] *= cg                          # 合并时漏缩放 c，contrast+colorRGB
    c[2] *= cb                          # 组合偏色 ~2.5 灰阶，qa-runner 坐实）
    if sa != 1.0:                       # 饱和：向 luma(当前结果) 收缩
        W = np.array([0.299, 0.587, 0.114], np.float32)
        M = sa * M + (1.0 - sa) * np.outer(np.ones(3, np.float32), W @ M)
        c = sa * c + (1.0 - sa) * float(W @ c) * np.ones(3, np.float32)
    rgb[:] = rgb @ M.T
    if c.any():
        rgb += c
    ga = _fx_num(p.get("gamma"), 1.0)
    if ga != 1.0:
        rgb[:] = _gamma_lut(ga)[
            np.clip(rgb, 0.0, 255.0).astype(np.uint8)]
    np.clip(rgb, 0, 255, out=rgb)


_FX_BLOOM_K = 1.1          # bloom 校准系数（对照引擎实测调整）


def _fx_bloom(rgb, p):
    """bloom：亮区提取 → 高斯模糊 → 叠加泛光。

    v3.5.7 提速：整条管线在 **1/4 分辨率**上做（亮区提取/模糊本来就是要
    大团低频的泛光，降采样视觉等效），模糊核同比例缩小再放大叠加，
    全帧只留最后一次加法。"""
    import cv2
    b = _fx_num(p.get("bloom"))
    if b <= 0:
        return
    bb = max(1, int(round(_fx_num(p.get("bloomBlur"), 4))))
    bt = min(0.95, max(0.01, _fx_num(p.get("bloomThreshold"), 0.5)))
    bri = max(0.2, _fx_num(p.get("bloomBrightness"), 0.5))
    h, w = rgb.shape[:2]
    qw, qh = max(1, w // 4), max(1, h // 4)
    small = cv2.resize(rgb, (qw, qh), interpolation=cv2.INTER_AREA)
    lum = small @ np.array([0.299, 0.587, 0.114], np.float32)[..., None]
    thr = bt * 255.0
    mask = np.clip((lum - thr) / max(1.0, 255.0 - thr), 0.0, 1.0)
    k = max(1, (bb * 2 + 1) // 4)
    if k % 2 == 0:
        k += 1
    glow = cv2.resize(cv2.GaussianBlur(small * mask, (k, k), 0), (w, h),
                      interpolation=cv2.INTER_LINEAR)
    rgb[:] += glow * (b * bri * 0.5 * _FX_BLOOM_K)
    np.clip(rgb, 0, 255, out=rgb)


def _fx_chain(rgb, p, ctx):
    """bg 层滤镜链（rgb: float32 0..255，就地修改）。"""
    import cv2
    px = _fx_num(p.get("pixelSize"))
    if px >= 2:
        k = int(px)
        h, w = rgb.shape[:2]
        small = cv2.resize(rgb, (max(1, w // k), max(1, h // k)),
                           interpolation=cv2.INTER_LINEAR)
        rgb[:] = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
    if p.get("glitchFilm"):
        _fx_glitch(rgb, ctx)
    if p.get("rgbFilm"):
        _fx_rgb_shift(rgb, ctx)
    b = _fx_num(p.get("blur"))
    if b > 0:
        k = max(1, int(round(b))) * 2 + 1
        h, w = rgb.shape[:2]
        # v3.5.7：大核模糊在 1/2 分辨率做（核同比例缩小再放大），视觉等效；
        # 小核（k<9）直接全帧，省两次 resize。
        if k >= 9 and h >= 4 and w >= 4:
            kh = max(1, k // 2)
            if kh % 2 == 0:
                kh += 1
            small = cv2.resize(rgb, (w // 2, h // 2),
                               interpolation=cv2.INTER_AREA)
            rgb[:] = cv2.resize(cv2.GaussianBlur(small, (kh, kh), 0), (w, h),
                                interpolation=cv2.INTER_LINEAR)
        else:
            rgb[:] = cv2.GaussianBlur(rgb, (k, k), 0)
    # v3.5.7：sepia 已并入 _fx_color 的合并矩阵（见其 docstring）
    _fx_color(rgb, p)
    _fx_bloom(rgb, p)
    ns = _fx_num(p.get("noise"))
    if ns > 0:
        _fx_noise(rgb, min(1.0, ns), ctx)
    if p.get("oldFilm"):
        _fx_oldfilm(rgb, ctx)
    if p.get("vignetting"):
        _fx_vignette(rgb, _fx_num(p.get("vignettingAlpha"), 0.5))
    np.clip(rgb, 0, 255, out=rgb)


def _fx_chain_rgba(frgba, p, ctx):
    """fig 层滤镜（非预乘 RGBA：调色作用于 rgb，alpha 参数乘 alpha 通道）。"""
    rgb = frgba[..., :3].astype(np.float32)
    _fx_chain(rgb, p, ctx)
    frgba[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    al = _fx_num(p.get("alpha"), 1.0)
    if al != 1.0:
        al = min(1.0, max(0.0, al))
        frgba[..., 3] = (frgba[..., 3].astype(np.float32) * al).astype(np.uint8)


def _paste_rgba(frgba, img, lay):
    """把素材帧（RGB/RGBA）按 layout 以 alpha-over 累积进透明立绘层。"""
    import cv2
    H, W = frgba.shape[:2]
    x, y, sc, rot = lay["x"], lay["y"], lay["scale"], lay["rot"]
    if img.ndim == 2:
        img = np.dstack([img] * 3)
    h, w = img.shape[:2]
    if img.shape[2] == 3:
        img = np.dstack([img, np.full((h, w), 255, np.uint8)])
    s0 = min(W / w, H / h)
    s = s0 * sc
    nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
    rgb = cv2.resize(img[..., :3], (nw, nh), interpolation=interp)
    a = cv2.resize(img[..., 3], (nw, nh), interpolation=interp)
    ox, oy, rw, rh = layer_rect(w, h, W, H, lay)
    if abs(rot) > 1e-6:
        M = cv2.getRotationMatrix2D((nw / 2.0, nh / 2.0), rot, 1.0)
        M[0, 2] += rw / 2.0 - nw / 2.0
        M[1, 2] += rh / 2.0 - nh / 2.0
        rgb = cv2.warpAffine(rgb, M, (rw, rh),
                             flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0))
        a = cv2.warpAffine(a, M, (rw, rh),
                           flags=cv2.INTER_LINEAR, borderValue=0)
    dx0, dy0 = max(0, ox), max(0, oy)
    dx1, dy1 = min(W, ox + rw), min(H, oy + rh)
    if dx1 <= dx0 or dy1 <= dy0:
        return
    sx0, sy0 = dx0 - ox, dy0 - oy
    sub_rgb = rgb[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)].astype(np.float32)
    sub_a = a[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)].astype(np.float32)[..., None] / 255.0
    dst = frgba[dy0:dy1, dx0:dx1].astype(np.float32)
    dst_rgb, dst_a = dst[..., :3], dst[..., 3:4] / 255.0
    out_a = sub_a + dst_a * (1 - sub_a)
    rgb_over = (sub_rgb * sub_a + dst_rgb * dst_a * (1 - sub_a)) / np.maximum(1e-3, out_a)
    frgba[dy0:dy1, dx0:dx1, :3] = np.clip(rgb_over, 0, 255).astype(np.uint8)
    frgba[dy0:dy1, dx0:dx1, 3] = np.clip(out_a[..., 0] * 255, 0, 255).astype(np.uint8)


# ffmpeg 候选（与 l2d_rig_qt.FFMPEG_CANDIDATES 同步；勿 import 那边）
_HERE = os.path.dirname(os.path.abspath(__file__))
FFMPEG_CANDIDATES = [os.path.join(_HERE, "ffmpeg.exe"),
                     r"D:\BaiduNetdiskDownload\Vocal2Midi\ffmpeg.exe"]


def ffmpeg_path():
    return next((p for p in FFMPEG_CANDIDATES if os.path.exists(p)), None)


# ---------------- 单帧合成 ----------------
_BG_CACHE = {}


def _bg_cache_key(jobs, W, H):
    """静态背景缓存键（仅 image 源：路径+mtime+布局+画布尺寸）。
    视频/puppet 源逐帧变化，一律不缓存。"""
    ks = []
    for img, lay, srcd in jobs:
        if not isinstance(srcd, dict) or srcd.get("kind") != "image":
            return None
        pth = str(srcd.get("path") or "")
        try:
            mt = os.path.getmtime(pth)
        except OSError:
            return None
        ks.append((os.path.normpath(pth), round(mt, 3),
                   round(float(lay["x"]), 4), round(float(lay["y"]), 4),
                   round(float(lay["scale"]), 5), round(float(lay["rot"]), 3),
                   bool(lay.get("cover")), img.shape[:2]))
    return (W, H, tuple(ks))


def _paste_bg_layer(bgc, jobs, W, H):
    """bg_jobs 贴满背景层（v3.5.7：image 源的贴底结果按键缓存，跳过
    每帧重复的 resize+blend；滤镜在 bgc 副本上就地改，不污染缓存）。"""
    if not jobs:
        return
    key = _bg_cache_key(jobs, W, H)
    if key is not None:
        cached = _BG_CACHE.get(key)
        if cached is not None:
            bgc[...] = cached
            return
    for img, lay, _sd in jobs:
        paste_layer(bgc, img, lay)
    if key is not None:
        if len(_BG_CACHE) >= 2:
            _BG_CACHE.clear()          # 上限 2 份（float32 1080p ≈ 25MB/份）
        _BG_CACHE[key] = bgc.copy()


def source_key(src):
    return (src.get("kind"), os.path.normpath(str(src.get("path") or "")))


def fit_paste(canvas, img):
    """等比缩放居中贴到画布（fit，黑边）——第一阶段唯一适配模式。"""
    H, W = canvas.shape[:2]
    h, w = img.shape[:2]
    if h == 0 or w == 0:
        return canvas
    s = min(W / w, H / h)
    nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    import cv2
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    x, y = (W - nw) // 2, (H - nh) // 2
    canvas[y:y + nh, x:x + nw] = r
    return canvas


def _track_clip_at(tr, t):
    """该轨在 t 时刻生效的 clip（同轨不重叠 => 至多一个）。"""
    for c in tr["clips"]:
        if c["start"] - V.EPS <= t < c["start"] + c["dur"] - V.EPS:
            return c
    return None


def _over_rgba(dst3, rgb, a2f):
    """把 rgb(同 dst3 区域大小) 按逐像素 alpha a2f(float32 HxWx1, 0..1)
    混合进 dst3（就地 uint8）。out = fg*a + dst*(1-a)，四舍五入。"""
    fg = rgb.astype(np.float32)
    out = fg * a2f + dst3.astype(np.float32) * (1.0 - a2f)
    np.add(out, 0.5, out=out)
    dst3[...] = out.astype(np.uint8)


def layer_rect(w, h, W, H, lay):
    """按 layout 计算素材在画布上的外接矩形（StageView 命中/画框与
    paste_layer 共用同一数学，取整一致）。

    - fit 基准 s0=min(W/w,H/h)，实际缩放 = s0*lay["scale"]
    - 中心放 (x*W, y*H)
    - rot==0：精确矩形；rot!=0：旋转后的外接矩形（与 paste_layer 同式）
    返回 (px, py, pw, ph) 画布像素坐标。
    """
    try:
        x, y = lay["x"], lay["y"]
        sc, rot = lay["scale"], lay["rot"]
    except Exception:
        return (0, 0, 0, 0)
    if w <= 0 or h <= 0 or W <= 0 or H <= 0:
        return (0, 0, 0, 0)
    s0 = min(W / w, H / h)
    s = s0 * sc
    nw = max(1, int(round(w * s)))
    nh = max(1, int(round(h * s)))
    cx, cy = x * W, y * H
    if rot == 0.0:
        return (int(round(cx - nw / 2.0)), int(round(cy - nh / 2.0)), nw, nh)
    rad = math.radians(rot)
    ca, sa = abs(math.cos(rad)), abs(math.sin(rad))
    rw = int(math.ceil(nw * ca + nh * sa))
    rh = int(math.ceil(nh * ca + nw * sa))
    return (int(round(cx - rw / 2.0)), int(round(cy - rh / 2.0)), rw, rh)


def paste_layer(canvas, img, lay):
    """按 layout（video_model.layout_at 产物）把素材帧 img 叠进 RGB canvas。

    - 素材 3ch=恒不透明，4ch=带 alpha（out=fg*a+dst*(1-a)）
    - fit 基准 s0=min(W/w,H/h)，实际缩放 = s0*lay["scale"]
    - **cover=True（v3.5.5，WebGAL changeBg 语义）：基准改 max(W/w,H/h)，
      铺满裁剪**——引擎里背景就是铺满的，视频里同语义才和游戏画面一致
    - 中心放 (x*W, y*H)，允许越界（裁剪）
    - rot==0 快路径：resize + 平移贴；rot!=0 才上 warpAffine
    - 默认布局(0.5,0.5,1.0,0.0)+无 alpha 走**旧代码原路径**
      （整幅直贴 / fit_paste），保证旧工程逐像素一致。"""
    import cv2
    H, W = canvas.shape[:2]
    x, y, sc, rot = lay["x"], lay["y"], lay["scale"], lay["rot"]
    cover = bool(lay.get("cover"))
    # ---- 旧行为精确保持 ----
    is_default = (x == 0.5 and y == 0.5 and sc == 1.0 and rot == 0.0
                  and not cover)
    if is_default and img.ndim == 2:
        img = np.dstack([img] * 3)
    if is_default and img.ndim == 3 and img.shape[2] == 3:
        if img.shape[0] == H and img.shape[1] == W:
            canvas[...] = img
        else:
            fit_paste(canvas, img)
        return canvas
    # ---- 通用路径 ----
    if img.ndim == 2:
        img = np.dstack([img] * 3)
    if img.shape[2] >= 4:
        rgb, alpha = img[:, :, :3], img[:, :, 3]
    else:
        rgb, alpha = img[:, :, :3], None
    h, w = rgb.shape[:2]
    if h == 0 or w == 0:
        return canvas
    s0 = max(W / w, H / h) if cover else min(W / w, H / h)
    s = s0 * sc
    nw = max(1, int(round(w * s)))
    nh = max(1, int(round(h * s)))
    interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
    rgb = cv2.resize(rgb, (nw, nh), interpolation=interp)
    alpha = cv2.resize(alpha, (nw, nh), interpolation=interp) \
        if alpha is not None else None
    ox, oy, rw, rh = layer_rect(w, h, W, H, lay)
    if rot == 0.0:
        dx0, dy0 = max(0, ox), max(0, oy)
        dx1, dy1 = min(W, ox + rw), min(H, oy + rh)
        if dx1 <= dx0 or dy1 <= dy0:
            return canvas
        sx0, sy0 = dx0 - ox, dy0 - oy
        sub_rgb = rgb[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)]
        dst = canvas[dy0:dy1, dx0:dx1]
        if alpha is None:
            dst[...] = sub_rgb                  # 全不透明：直贴
        else:
            a2 = alpha[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)] \
                    .astype(np.float32)[:, :, None] * (1.0 / 255.0)
            _over_rgba(dst, sub_rgb, a2)
        return canvas
    # rot!=0：绕素材中心旋转到扩充画幅（透明边界），再平移贴
    rad = math.radians(rot)
    ca, sa = abs(math.cos(rad)), abs(math.sin(rad))
    M = cv2.getRotationMatrix2D((nw / 2.0, nh / 2.0), rot, 1.0)
    M[0, 2] += rw / 2.0 - nw / 2.0
    M[1, 2] += rh / 2.0 - nh / 2.0
    rgb = cv2.warpAffine(rgb, M, (rw, rh), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    if alpha is not None:
        # 旋转会把透明边界的 rgb 混进不透明边缘：alpha 同 warp 后必须再
        # 乘回（warp 后的 rgb 边缘已被 0 污染），混合才正确
        a2 = cv2.warpAffine(alpha, M, (rw, rh), flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT,
                            borderValue=0).astype(np.float32)
    else:
        a2 = cv2.warpAffine(np.full((nh, nw), 255, np.uint8), M, (rw, rh),
                            flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT,
                            borderValue=0).astype(np.float32)
    dx0, dy0 = max(0, ox), max(0, oy)
    dx1, dy1 = min(W, ox + rw), min(H, oy + rh)
    if dx1 <= dx0 or dy1 <= dy0:
        return canvas
    sx0, sy0 = dx0 - ox, dy0 - oy
    sl = (slice(sy0, sy0 + (dy1 - dy0)), slice(sx0, sx0 + (dx1 - dx0)))
    a2f = a2[sl][:, :, None] * (1.0 / 255.0)
    _over_rgba(canvas[dy0:dy1, dx0:dx1], rgb[sl], a2f)
    return canvas


def compose_frame(proj, t, src_get, canvas=None, aid=None, subtitle=True,
                  fx=True):
    """t 时刻一帧 RGB uint8（v3.4.0 起：**自底向上逐可见视频轨 RGBA 叠加**，
    不再只取最上层 clip；上层无 alpha 素材自然覆盖=旧语义，
    带 alpha 素材按 out=fg*a+dst*(1-a) 混合）。
    src_get(source_dict)->FrameSource（外层做缓存）。
    某轨素材缺失/解码失败 -> 跳过该轨继续合成（不中断导出/预览）。

    F2-L5（v3.5）：加 aid —— 多幕工程里一幕一条独立时间轴，合成只看
    **当前幕**的轨道。aid=None 时行为与旧版完全一致（v1 顶层 tracks /
    v2 第一幕）。

    v3.5.4：subtitle=True（默认）在合成末尾**烧录当前幕的活跃台词**。
    工作台预览调用点显式传 subtitle=False（Qt paintEvent 自己画）。

    v3.5.6：滤镜映射——proj["effects"] 里有 t 时刻生效的 setTransform 时，
    走**分层合成**：背景层（cover/BG 轨）→ bg-main 滤镜 → 立绘层（FIG-*
    轨，透明底）→ fig-* 滤镜 → 其余轨按列表序叠上。与引擎 target 语义
    一致（bg-main=背景、fig-*=立绘，互不波及）。无活跃滤镜 = 旧逐轨叠加
    路径，逐像素零 diff。

    v3.5.7：fx=False —— 工作台普通预览快路径，**不解析不渲染滤镜**
    （滤镜链逐帧 numpy ~330ms/帧卡爆预览），恒走旧逐轨叠加路径；滤镜
    效果看实时预览（引擎 GPU 渲染）或导出成片。默认 fx=True（导出/
    引擎对照不受影响）。另：image 源的背景贴层加了静态缓存
    （_paste_bg_layer），同工程逐帧导出只算一次 resize+blend。"""
    H, W = int(proj["height"]), int(proj["width"])
    if canvas is None:
        canvas = np.zeros((H, W, 3), np.uint8)
    else:
        canvas[...] = 0
    fx_map = _active_fx(proj, t, aid) if fx else None
    if not fx_map:
        # ---- 旧逐轨叠加路径（无滤镜：逐像素零 diff 保证）----
        for tr in V._tracks_of(proj, aid):      # 列表序 = 底→顶（后加的在上）
            if tr.get("kind") != "video" or tr.get("hidden"):
                continue
            clip = _track_clip_at(tr, t)
            if clip is None:
                continue
            try:
                src = src_get(clip["source"])
                img = src.frame(max(0.0, t - clip["start"] + clip["src_in"]))
            except Exception:
                continue
            if img is None or img.size == 0:
                continue
            paste_layer(canvas, img, V.layout_at(clip))
    else:
        # ---- v3.5.6 分层合成（滤镜按 target 作用层）----
        ctx = {"t": t,
               "seed": int(t * max(1.0, float(proj.get("fps") or 30))) & 0x7FFFFFFF}
        bgc = np.zeros((H, W, 3), np.float32)       # 背景层（黑底 RGB）
        fig = np.zeros((H, W, 4), np.uint8)         # 立绘层（非预乘 RGBA）
        bg_jobs, fig_jobs, main_jobs = [], [], []
        for tr in V._tracks_of(proj, aid):
            if tr.get("kind") != "video" or tr.get("hidden"):
                continue
            clip = _track_clip_at(tr, t)
            if clip is None:
                continue
            lay = V.layout_at(clip)
            is_fig = str(tr.get("name") or "").startswith("FIG")
            try:
                src = src_get(clip["source"])
                img = src.frame(max(0.0, t - clip["start"] + clip["src_in"]))
            except Exception:
                continue
            if img is None or img.size == 0:
                continue
            if bool(lay.get("cover")):
                bg_jobs.append((img, lay, clip.get("source")))
            elif is_fig:
                fig_jobs.append((img, lay))
            else:
                main_jobs.append((img, lay))
        _paste_bg_layer(bgc, bg_jobs, W, H)
        bg_fx = fx_map.get("bg-main") or fx_map.get("bg")
        if bg_fx:
            _fx_chain(bgc, bg_fx, ctx)
        np.clip(bgc, 0, 255, out=bgc)
        canvas[...] = bgc
        for img, lay in fig_jobs:
            _paste_rgba(fig, img, lay)
        fig_fx = None
        for k, v in fx_map.items():
            if k.startswith("fig"):
                fig_fx = v
        if fig_fx:
            _fx_chain_rgba(fig, fig_fx, ctx)
        a = fig[..., 3:4].astype(np.float32) / 255.0
        canvas[...] = (fig[..., :3] * a + canvas * (1 - a)).astype(np.uint8)
        for img, lay in main_jobs:                  # 其余轨仍在最上层
            paste_layer(canvas, img, lay)
    if subtitle:
        _draw_dialogue(canvas, proj, t, aid)
    return canvas


class SourceManager:
    """source dict -> FrameSource 缓存池；close_all 释放。"""

    def __init__(self):
        self._cache = {}

    def get(self, source):
        k = source_key(source)
        if k not in self._cache:
            import video_source as VS
            self._cache[k] = VS.open_source(source)
        return self._cache[k]

    def close_all(self):
        for s in self._cache.values():
            try:
                s.close()
            except Exception:
                pass
        self._cache.clear()


# ---------------- 混音 ----------------
def _place(buf, samples, at_sample):
    """把 samples 放到 buf[at_sample:]，饱和截断。"""
    n = len(samples)
    i0 = max(0, at_sample)
    if i0 >= len(buf):
        return
    take = min(n, len(buf) - i0)
    buf[i0:i0 + take] = np.clip(buf[i0:i0 + take].astype(np.int64)
                                + samples[:take].astype(np.int64),
                                -32768, 32767).astype(np.int16)


def extract_audio(path):
    """任意媒体 -> (int16 mono, sr)。wav 直读；其余 ffmpeg 转；失败 (None, 0)。"""
    if not path or not os.path.isfile(path):
        return None, 0
    if path.lower().endswith(".wav"):
        try:
            with wave.open(path, "rb") as wf:
                sr = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
            a = np.frombuffer(raw, dtype=np.int16)
            if a.size % max(1, wf.getnchannels()):
                a = a[:-(a.size % wf.getnchannels())]
            ch = max(1, wf.getnchannels())
            mono = a.reshape(-1, ch).mean(axis=1).astype(np.int16) if ch > 1 else a
            return mono, sr
        except Exception:
            return None, 0
    ff = ffmpeg_path()
    if not ff:
        return None, 0
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        subprocess.run(
            [ff, "-y", "-i", path, "-vn", "-ac", "1", "-ar", "44100",
             "-sample_fmt", "s16", tmp],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=0x08000000, timeout=120)
        return extract_audio(tmp)
    except Exception:
        return None, 0
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def mix_audio(proj, sr=44100):
    """整工程混音 -> int16 ndarray（时长=project_duration）。无音频返回 None。"""
    dur = V.project_duration(proj)
    if dur <= 0:
        return None
    buf = np.zeros(int(dur * sr) + sr // 2, np.int16)
    any_a = False
    for tr in proj["tracks"]:
        for c in tr["clips"]:
            kind = c["source"]["kind"]
            if kind in ("puppet", "image"):
                continue
            samples, s2 = extract_audio(c["source"]["path"])
            if samples is None:
                continue          # 素材没声音轨，正常
            if s2 != sr:
                k = sr / s2
                idx = (np.arange(int(len(samples) * k)) / k).astype(np.int32)
                idx = np.clip(idx, 0, len(samples) - 1)
                samples = samples[idx]
            at = int(round((c["start"] - c["src_in"]) * sr))
            if at >= 0:
                _place(buf, samples, at)
            else:                        # 素材头已被 trim 掉
                _place(buf, samples[-at:], 0)
            any_a = True
    return buf if any_a and np.any(buf) else None


def write_wav(path, samples, sr=44100):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())


# ---------------- 导出 Worker ----------------
try:
    from PySide6.QtCore import QThread, Signal

    class VideoExportWorker(QThread):
        """按轨道时间线导出 mp4。progress(pct)，done(ok, msg)。"""
        progress = Signal(int)
        done = Signal(bool, str)

        def __init__(self, proj, out_path, preview_scale=1.0, parent=None):
            super().__init__(parent)
            self.proj = proj
            self.path = out_path
            self.scale = float(preview_scale)
            self._cancel = False

        def cancel(self):
            self._cancel = True

        def run(self):
            import cv2
            sm = SourceManager()
            try:
                W = max(2, int(round(self.proj["width"] * self.scale)) & ~1)
                H = max(2, int(round(self.proj["height"] * self.scale)) & ~1)
                fps = float(self.proj["fps"])
                n = max(1, int(round(V.project_duration(self.proj) * fps)))
                vw = cv2.VideoWriter(self.path, cv2.VideoWriter_fourcc(*"mp4v"),
                                    fps, (W, H))
                if not vw.isOpened():
                    self.done.emit(False, "无法创建视频编码器（cv2 mp4v）")
                    return
                canvas = np.zeros((H, W, 3), np.uint8)
                for i in range(n):
                    if self._cancel:
                        vw.release()
                        self.done.emit(False, "已取消")
                        return
                    t = i / fps
                    frame = compose_frame(self.proj, t, sm.get)
                    if frame.shape[0] != H or frame.shape[1] != W:
                        frame = cv2.resize(frame, (W, H), interpolation=cv2.INTER_AREA)
                    vw.write(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
                    if i % max(1, n // 50) == 0:
                        self.progress.emit(int(i / n * 80))
                vw.release()
                self.progress.emit(80)
                # 混音合流
                audio = mix_audio(self.proj) if self.scale >= 1.0 else None
                if audio is None or not np.any(audio):
                    self.done.emit(True, self.path)
                    return
                ff = ffmpeg_path()
                if not ff:
                    self.done.emit(True, self.path + "（未找到 ffmpeg，导出为无声）")
                    return
                wavp = tempfile.mktemp(suffix=".wav")
                write_wav(wavp, audio)
                finalp = tempfile.mktemp(suffix=".mp4")
                r = subprocess.run(
                    [ff, "-y", "-i", self.path, "-i", wavp,
                     "-c:v", "copy", "-c:a", "aac", "-shortest", finalp],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, creationflags=0x08000000, timeout=600)
                self.progress.emit(98)
                if r.returncode == 0 and os.path.isfile(finalp):
                    os.replace(finalp, self.path)
                    msg = "已导出（含音轨）"
                else:
                    msg = "音轨合流失败，输出无声视频"
                for p in (wavp, finalp):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                self.done.emit(True, "%s %s" % (self.path, msg))
            except Exception as e:
                self.done.emit(False, "导出异常: %r" % e)
            finally:
                sm.close_all()
except ImportError:      # 无 Qt 环境（纯单测）也能 import 本模块
    VideoExportWorker = None
