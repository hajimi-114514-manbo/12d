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
import contextlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
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
# v3.5.8（task-74 追加 A）：打字机"稳定区增量带"——a==1 的前缀字符像素
# 恒定（alpha-over 满足结合律：先叠进透明带再叠 canvas == 逐字直接叠
# canvas，PIL u8 混合与 float 混合的舍入差 ≤1 级，subtitle_burn 断言内）。
# (W, H, text) -> {"img": PIL RGBA 带, "drawn": 已画前缀字符数, "arr":
#                  np 拷贝（drawn 变化才刷新）, "x0","y0","used_x1", ...}
#
# **必须线程局部**（v3.5.8 并行导出）：这条缓存是**有状态增量累积**——
# drawn 单调推进、p/al 缓冲原地累加。多线程共享会互相推进 drawn（A 线程
# 画到第 10 字时 B 线程以为已画 10 字而跳过自己的 1..9 字 -> 缺字/串色，
# 实测 maxdiff 154~186）。thread-local 后每线程独立累积，语义与单线程
# 完全一致（只是多占一点内存）。
_SUB_LOCAL = threading.local()
# v3.5.8 并行导出：混合中间缓冲**必须线程局部**（同 _SUB_LOCAL.typing 的教训：
# 共享 scratch 会被两个线程先后覆盖 -> 串色）。按 (h, w, dtype) 复用，省掉
# 每帧一次全宽临时分配（1080p 面板带 6MB）。
_BUF_LOCAL = threading.local()


def _scratch(shape, dtype):
    """线程局部复用缓冲（键含 dtype，u8/u16 不互踩）。"""
    d = getattr(_BUF_LOCAL, "bufs", None)
    if d is None: d = _BUF_LOCAL.bufs = {}
    key = (shape[0], shape[1], shape[2] if shape[2:] else 1, dtype)
    buf = d.get(key)
    if buf is None:
        if len(d) > 8: d.clear()
        buf = d[key] = np.empty(shape, dtype)
    return buf


def _sub_typing_cache():
    c = getattr(_SUB_LOCAL, "typing", None)
    if c is None: c = _SUB_LOCAL.typing = {}
    return c


_SUB_TYPING_MAX = 8
_FONT_CACHE = {}
# v3.5.8 并行导出：字幕静态缓存（面板/布局/整句）是只读幂等的「算一次存
# 起来」，多线程共享仅需保证字典结构操作互斥（读多写少，锁粒度极小）。
_SUB_LOCK = threading.RLock()

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
    with contextlib.suppress(OSError):
        if os.path.isdir(rt):
            out.extend(os.path.join(rt, f) for f in sorted(os.listdir(rt))
                       if f.lower().startswith("resourcehanrounded")
                       and f.lower().endswith((".ttf", ".otf", ".ttc")))
    out.extend((r"C:\Windows\Fonts\msyh.ttc",
                r"C:\Windows\Fonts\msyhbd.ttc",
                r"C:\Windows\Fonts\simhei.ttf"))
    return out


def _get_font(px, bold=False):
    key = (int(px), bool(bold))
    f = _FONT_CACHE.get(key)
    if f is not None: return f
    mod = _pil()
    if mod is None: return None
    ImageFont = mod[2]
    px = max(1, int(px))
    f = None
    if bold:
        for p in _font_files():
            if "msyhbd" in p:
                with contextlib.suppress(Exception):
                    f = ImageFont.truetype(p, px)
                    break
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


def _band_premul(band):
    """band(RGBA) -> (pre, inv, amin, amax)「混合辅助件」（字幕提速，本档）。

    pre = round(src*a/255)（u8 直写混合的唯一被加数）、
    inv = (255-a) 展成 3 通道（u8，逐帧不再 cvtColor+subtract）、
    amin/amax = 全幅 alpha 极值（逐帧不再 min/any 扫交错平面）。

    逐帧混合退化成 **两条 u8 pass**：
        out = sat_u8(pre + round(dst*inv/255))
    旧 u16 管线是 6 条 pass（cvtColor/subtract/multiply/add/convertScaleAbs
    +min/any 扫描），1080p 面板带（1920×322）实测 4.97→0.48ms。

    分母仍是 255（cv2.multiply 的 scale=1/255，与 float 原式
    src*a/255 + dst*(1-a/255) 只差两次 ±0.5 舍入 ≤1 灰阶；早期 /256 版
    之所以半透明边缘偏亮 0.4%，是 pre 与 dst 项分母不一致——这里两项都
    走 /255，subtitle_burn 27 项 + typing 缓存 8 项 + blend 精度探针把关）。

    **纯空间预乘**：时间性滤镜的逐帧变化在滤镜层缓存（_FX_TIMEVARYING_KEYS）
    之外，与字幕静态缓存无关。"""
    import cv2
    rgb = band[..., :3]
    a = np.ascontiguousarray(band[..., 3])
    if a.size == 0:                        # 空带（0 行/0 列）：给个恒等辅助件
        z = np.zeros(band.shape[:2] + (3,), np.uint8)
        return (z, z, 0, 0)
    amin = int(a.min())
    amax = int(a.max())
    a3 = cv2.cvtColor(a, cv2.COLOR_GRAY2BGR)
    pre = cv2.multiply(rgb, a3, dtype=cv2.CV_8U, scale=1.0 / 255.0)
    inv = cv2.subtract(255, a3)
    return (pre, inv, amin, amax)


def _blend_band(canvas, y0, band, x0=0, pre=None):
    """band（RGBA）alpha 混合进 canvas 的 y0..、x0.. 区域（就地）。

    字幕提速：`pre` 传 `_band_premul` 的辅助件 → 两条 u8 pass 定点混合（见
    该函数注释）；传 None（老调用点/测试手搭的 band）时现算辅助件，结果
    同一条算式。全透明跳过 / 全不透明直写用缓存里的 amin/amax 判定：
    切片区域用全幅极值做**保守**判定——全 0 切片恒等、全 255 切片必然
    仍可直写，判不出只是少走快路径，像素结果不变。"""
    import cv2
    h, w = band.shape[:2]
    reg = canvas[y0:y0 + h, x0:x0 + w]
    if reg.size == 0: return
    if pre is None or not (isinstance(pre, tuple) and len(pre) == 4):
        pre = _band_premul(band)
    p, inv, amin, amax = pre
    if amax == 0: return
    if amin == 255:
        reg[...] = band[..., :3]
        return
    if p.shape[1] != w:                    # 打字带按 used_x1 切片
        p = p[:, :w]
        inv = inv[:, :w]
    buf = _scratch(reg.shape, np.uint8)
    cv2.multiply(reg, inv, dst=buf, dtype=cv2.CV_8U, scale=1.0 / 255.0)
    cv2.add(p, buf, dst=reg)


def _panel_layer(W, H):
    """底部渐变面板（textbox.scss TextBox_Background 复刻）。缓存 (W,H)。"""
    key = (W, H)
    with _SUB_LOCK:
        hit = _SUB_PANEL_CACHE.get(key)
    if hit is not None: return hit
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
    out = (y0, band, _band_premul(band))
    with _SUB_LOCK:
        if len(_SUB_PANEL_CACHE) >= 4: _SUB_PANEL_CACHE.clear()
        _SUB_PANEL_CACHE[key] = out
    return out


def _dialog_layout(W, H, text):
    """台词排版：字距 0.2em 逐字符定位 + 贪心换行（≤4 行）。
    返回 [(ch, x, y), ...]（y = 行顶），缓存 (W,H,text)。"""
    key = (W, H, text)
    with _SUB_LOCK:
        hit = _SUB_LAYOUT_CACHE.get(key)
    if hit is not None: return hit
    mod = _pil()
    body = _get_font(_body_px(H), bold=False)
    if mod is None or body is None: return []
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
            if lines >= 4: break
            continue
        try:
            w = body.getlength(ch) + sp
        except Exception:
            w = _body_px(H)
        if x > x0 and x + w > x1:
            lines += 1
            y += line_h
            x = x0
            if lines >= 4: break
        out.append((ch, x, y))
        x += w
    with _SUB_LOCK:
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
    with _SUB_LOCK:
        hit = _SUB_TEXT_CACHE.get(("name",) + key)
    if hit is not None: return hit
    mod = _pil()
    if mod is None or not who: return (0, None)
    Image, ImageDraw, _ = mod
    scale = _engine_scale(H)
    name_px = max(14, int(round(_body_px(H) * 0.85)))
    font = _get_font(name_px, bold=True)
    if font is None: return (0, None)
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
        if a <= 0: continue
        yy = ly + int(round(2 * scale))
        if 0 <= yy < bh: d.point((x0 + i, yy), fill=(255, 255, 255, a))
    band = np.asarray(img).copy()
    img.close()
    out = (y0, band, _band_premul(band))
    with _SUB_LOCK:
        if len(_SUB_TEXT_CACHE) >= _SUB_CACHE_MAX:
            _SUB_TEXT_CACHE.pop(next(iter(_SUB_TEXT_CACHE)), None)
        _SUB_TEXT_CACHE[("name",) + key] = out
    return out


def _full_text_band(W, H, text):
    """打字完成后的整句图层。缓存 (W,H,text)。"""
    key = (W, H, text)
    with _SUB_LOCK:
        hit = _SUB_TEXT_CACHE.get(key)
    if hit is not None: return hit
    mod = _pil()
    body = _get_font(_body_px(H), bold=False)
    if mod is None or body is None: return (0, None)
    Image, ImageDraw, _ = mod
    chars = _dialog_layout(W, H, text)
    if not chars: return (0, None)
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
    out = (y0, x0, band, _band_premul(band))
    with _SUB_LOCK:
        if len(_SUB_TEXT_CACHE) >= _SUB_CACHE_MAX:
            _SUB_TEXT_CACHE.pop(next(iter(_SUB_TEXT_CACHE)), None)
        _SUB_TEXT_CACHE[key] = out
    return out


def _tile_of_char(ch, body, body_px, alpha):
    """单字符三层文字的 RGBA tile 数组（与 _draw_char_now 同源构造）。
    返回 (arr, pad, bw, bh) 或 None。"""
    mod = _pil()
    if mod is None or body is None or alpha <= 0.02: return None
    Image, ImageDraw, _ = mod
    stroke = max(2, int(round(0.12 * body_px)))
    soff = max(1, int(round(0.05 * body_px)))
    bb = body.getbbox(ch) or (0, 0, int(body_px), int(body_px * 1.2))
    pad = stroke + soff + 2
    bw = int(bb[2] - bb[0]) + pad * 2
    bh = int(sum(body.getmetrics())) + pad * 2
    if bw <= 0 or bh <= 0: return None
    tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    _draw_engine_text(d, pad - bb[0], pad, ch, body, body_px, alpha=alpha)
    arr = np.asarray(tile)
    return arr, pad, bw, bh


def _tile_over_pband(p, al, x, y, arr, pad):
    """tile 以与 _draw_char_now **完全相同的算式序**累积进 premul float
    带（p=rgb*a 累积、al=a 累积）。premul 空间 source-over 线性可结合：
    over(P, T1⊕T2) == over(over(P,T1),T2)（浮点舍入 ≤1ulp），
    这是稳定区带缓存与逐字路径逐像素一致的正确性根基——
    PIL ImageDraw 直接叠 PIL 带是 lerp 语义，相邻字半透明交叠处
    实测差 236 级，故带必须用本函数积累。"""
    ta = arr[..., 3:4].astype(np.float32) / 255.0
    trgb = arr[..., :3].astype(np.float32)
    h, w = p.shape[:2]
    px, py = int(x) - pad, int(y) - pad
    sx0, sy0 = max(0, px), max(0, py)
    sx1, sy1 = min(w, px + arr.shape[1]), min(h, py + arr.shape[0])
    if sx1 <= sx0 or sy1 <= sy0: return
    sub_a = ta[sy0 - py:sy1 - py, sx0 - px:sx1 - px]
    sub_rgb = trgb[sy0 - py:sy1 - py, sx0 - px:sx1 - px]
    ep = p[sy0:sy1, sx0:sx1]
    ep *= (1.0 - sub_a)
    ep += sub_rgb * sub_a
    ea = al[sy0:sy1, sx0:sx1]
    ea *= (1.0 - sub_a)
    ea += sub_a


def _typing_band(W, H, text, chars, n_stable):
    """打字机稳定前缀增量带（v3.5.8 追加 A，premul float 版）。

    返回 dict {"arr","aux","x0","y0","used_x1","drawn"} 或 None。
    带几何取整句 bbox（与 _full_text_band 同式，区域随台词固定不漂移）；
    n_stable 增长时只把**新稳定的字符**经 _tile_over_pband 增量累积并
    刷新 arr/aux；n_stable 不变时零绘制零刷新；n_stable 回退（scrub
    往回拖）时重建。arr 是 premul 语义 band（rgb=p 已是 0..255 显示量级
    的 src*a，故 aux.pre 直接取 p，不再像 straight band 那样重乘 a），
    aux.inv/amin/amax 由 al 生成，供 _blend_band 两条 u8 pass。
    调用方把活跃字符（n_stable..vis）继续逐字直接叠 canvas。"""
    mod = _pil()
    body = _get_font(_body_px(H), bold=False)
    if mod is None or body is None or n_stable <= 0: return None
    key = (W, H, text)
    _tc = _sub_typing_cache()
    e = _tc.get(key)
    if e is None or n_stable < e["drawn"]:
        body_px = _body_px(H)
        stroke = max(2, int(round(0.12 * body_px)))
        soff = max(1, int(round(0.05 * body_px)))
        xs = [c[1] for c in chars]
        ys = [c[2] for c in chars]
        x0 = max(0, int(min(xs) - stroke - soff - 2))
        x1 = min(W, int(max(xs) + max(body.getlength(c[0]) for c in chars)
                         + stroke + soff + 2))
        y0 = max(0, int(min(ys) - stroke - soff - 2))
        y1 = min(H, int(max(ys) + int(sum(body.getmetrics()))
                        + stroke + soff + 2))
        if x1 <= x0 or y1 <= y0: return None
        e = {"p": np.zeros((y1 - y0, x1 - x0, 3), np.float32),
             "al": np.zeros((y1 - y0, x1 - x0, 1), np.float32),
             "drawn": 0, "arr": None, "aux": None,
             "x0": x0, "y0": y0, "used_x1": x0}
        _tc.pop(key, None)
        while len(_tc) >= _SUB_TYPING_MAX:
            _tc.pop(next(iter(_tc)), None)
        _tc[key] = e
    if n_stable > e["drawn"]:
        body_px = _body_px(H)
        for ch, x, y in chars[e["drawn"]:n_stable]:
            t = _tile_of_char(ch, body, body_px, 1.0)
            if t is not None:
                arr, pad, _bw, _bh = t
                _tile_over_pband(e["p"], e["al"], x - e["x0"], y - e["y0"],
                                 arr, pad)
        e["drawn"] = n_stable
        a_u8 = np.clip(e["al"] * 255.0, 0.0, 255.0).astype(np.uint8)
        pre = np.rint(np.clip(e["p"], 0.0, 255.0)).astype(np.uint8)
        e["arr"] = np.dstack([pre[..., 0], pre[..., 1], pre[..., 2],
                              a_u8[..., 0]])
        # premul 带：p 本身已是 src*a（0..255 显示量级），直接当 pre，
        # 不能再走 _band_premul（那会把 a 乘第二遍）
        e["aux"] = (pre,
                    np.repeat(np.subtract(255, a_u8, dtype=np.uint8), 3, axis=2),
                    int(a_u8.min()), int(a_u8.max()))
        last_x = chars[n_stable - 1][1]
        e["used_x1"] = min(e["arr"].shape[1],
                           int(last_x - e["x0"]
                               + body.getlength(chars[n_stable - 1][0])
                               + 0.2 * body_px
                               + 2 * (int(round(0.12 * body_px))
                                      + int(round(0.05 * body_px)) + 2)))
    if e["arr"] is None or e["drawn"] <= 0: return None
    return e


def _draw_char_now(canvas, x, y, ch, alpha):
    """打字中的单个渐显字符（小图层局部混合）。"""
    mod = _pil()
    body = _get_font(_body_px(canvas.shape[0]), bold=False)
    if mod is None or body is None or alpha <= 0.02: return
    Image, ImageDraw, _ = mod
    body_px = _body_px(canvas.shape[0])
    stroke = max(2, int(round(0.12 * body_px)))
    soff = max(1, int(round(0.05 * body_px)))
    bb = body.getbbox(ch) or (0, 0, int(body_px), int(body_px * 1.2))
    pad = stroke + soff + 2
    bw = int(bb[2] - bb[0]) + pad * 2
    bh = int(sum(body.getmetrics())) + pad * 2
    if bw <= 0 or bh <= 0: return
    tile = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    _draw_engine_text(d, pad - bb[0], pad, ch, body, body_px, alpha=alpha)
    arr = np.asarray(tile)
    H, W = canvas.shape[:2]
    px, py = int(x) - pad, int(y) - pad
    sx0, sy0 = max(0, px), max(0, py)
    sx1, sy1 = min(W, px + bw), min(H, py + bh)
    if sx1 <= sx0 or sy1 <= sy0: return
    sub = arr[sy0 - py:sy1 - py, sx0 - px:sx1 - px]
    sub_a = sub[..., 3:4].astype(np.float32) / 255.0
    reg = canvas[sy0:sy1, sx0:sx1]
    reg[:] = (sub[..., :3] * sub_a + reg * (1 - sub_a)).astype(np.uint8)


def _typewriter_per(text, dur):
    n = max(1, len(text))
    p = _TYPEWRITER_MS / 1000.0
    budget = max(0.3, float(dur) * 0.7 - _TYPEWRITER_FADE)
    return min(p, budget / n)


def _over_rgba_band(dst, src, dy, dx=0):
    """src RGBA 以 source-over 叠到 dst RGBA 的 (dy,dx) 区域（就地，非预乘）。
    v3.5.9：panel+name 预合成用（结合律成立，差异仅 uint8 舍入 ≤2 灰阶）。"""
    h, w = src.shape[:2]
    reg = dst[dy:dy + h, dx:dx + w]
    if reg.size == 0: return
    a_s = src[..., 3:4].astype(np.float32) / 255.0
    a_d = reg[..., 3:4].astype(np.float32) / 255.0
    out_a = a_s + a_d * (1 - a_s)
    num = (src[..., :3].astype(np.float32) * a_s
           + reg[..., :3].astype(np.float32) * a_d * (1 - a_s))
    denom = np.maximum(out_a, 1e-6)
    reg[..., :3] = np.clip(num / denom, 0, 255).astype(np.uint8)
    reg[..., 3] = np.clip(out_a[..., 0] * 255, 0, 255).astype(np.uint8)


_PANEL_NAMED_CACHE = {}


def _panel_named_layer(W, H, who):
    """panel+name 预合成（v3.5.9）：名字同句不变 → 缓存 (W,H,who)，
    每帧 blend 从 3 次（panel/name/text）减到 2 次。who 为空退化为 panel。"""
    if not who: return _panel_layer(W, H)
    key = (W, H, who)
    with _SUB_LOCK:
        hit = _PANEL_NAMED_CACHE.get(key)
    if hit is not None: return hit
    y0, band, bpre = _panel_layer(W, H)
    if band is None or not band.size: return (y0, band, bpre)
    ny0, nband, npre = _name_band(W, H, who)
    if nband is None or not nband.size: return (y0, band, bpre)
    band = band.copy()                    # 不污染 _panel_layer 的共享缓存
    _over_rgba_band(band, nband, ny0 - y0)
    bpre = _band_premul(band)             # 预乘重算（一次性，随缓存存放）
    out = (y0, band, bpre)
    with _SUB_LOCK:
        if len(_PANEL_NAMED_CACHE) >= 16: _PANEL_NAMED_CACHE.clear()
        _PANEL_NAMED_CACHE[key] = out
    return out


def _draw_dialogue(canvas, proj, t, aid):
    """把 t 时刻活跃台词画到 canvas（引擎皮肤：面板 + 名字 + 打字机）。"""
    mod = _pil()
    if mod is None: return
    try:
        dlg = V.act_view(proj, aid).get("dialogue") or []
    except Exception:
        return
    eps = 1e-6
    active = None
    for d in dlg:
        if not isinstance(d, dict): continue
        try:
            t0 = float(d.get("t"))
            dur = float(d.get("dur"))
        except Exception:
            continue
        if t0 - eps <= t < t0 + dur - eps: active = d
    if active is None: return
    who = str(active.get("who") or "")
    text = str(active.get("text") or "")
    if not text: return
    H, W = canvas.shape[:2]
    # v3.5.9：panel+name 预合成缓存（名字同句不变），blend 3→2 次
    y0, band, bpre = _panel_named_layer(W, H, who)
    if band is not None and band.size: _blend_band(canvas, y0, band, pre=bpre)
    chars = _dialog_layout(W, H, text)
    if not chars: return
    n = len(chars)
    t_el = t - float(active.get("t"))
    per = _typewriter_per(text, float(active.get("dur") or 3.0))
    fade = _TYPEWRITER_FADE
    if t_el >= (n - 1) * per + fade:
        sy0, sx0, sband, spre = _full_text_band(W, H, text)
        if sband is not None and sband.size:
            _blend_band(canvas, sy0, sband, sx0, pre=spre)
        return
    vis = min(n, int(t_el / per) + 1)
    # v3.5.8 追加 A：a==1 的稳定前缀走增量带（每帧一次区域 blend，零
    # 重复 PIL 绘制）；仍在 fade 中的尾部字符逐字直接叠 canvas（原路径）。
    n_stable = 0
    if t_el >= fade: n_stable = min(vis, int((t_el - fade) / per) + 1)
    if n_stable > 0:
        e = _typing_band(W, H, text, chars, n_stable)
        if e is not None:
            ux = e["used_x1"]
            band = e["arr"][:, :ux]
            if band.size:
                # aux 是全幅的，_blend_band 里按 band 宽切 pre/inv 两列
                _blend_band(canvas, e["y0"], band, e["x0"], pre=e["aux"])
    for i in range(n_stable, vis):
        ch, x, y = chars[i]
        a = min(1.0, max(0.0, (t_el - i * per) / fade))
        if a > 0.02: _draw_char_now(canvas, x, y, ch, a)


def clear_subtitle_cache():
    """换工程/换幕时清台词图层缓存（测试与外层管理用）。

    v3.5.8：打字增量带是**线程局部**的，这里只能清当前线程那份。导出
    并行线程的缓存随线程结束自然释放（每个导出任务用新线程池），因此
    跨工程导出不会有脏数据；同线程反复导出由本函数清理。"""
    with _SUB_LOCK:
        _SUB_PANEL_CACHE.clear()
        _SUB_LAYOUT_CACHE.clear()
        _SUB_TEXT_CACHE.clear()
        _FONT_CACHE.clear()
        _PANEL_NAMED_CACHE.clear()     # v3.5.9 panel+name 预合成
    _sub_typing_cache().clear()


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
    if hit is not None: return hit if hit != "bad" else None
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
        if m: target = m.group(1).rstrip(";").strip()
        if not isinstance(params, dict): raise ValueError
        out = (target, dict(params))
    except Exception:
        _FX_PARSE_CACHE[key] = "bad"
        return None
    if len(_FX_PARSE_CACHE) > 512: _FX_PARSE_CACHE.clear()
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
        if not isinstance(ef, dict): continue
        try:
            et = float(ef.get("t"))
        except Exception:
            continue
        if et > t + eps: continue
        parsed = _parse_effect_cmd(ef.get("cmd"))
        if parsed: out[parsed[0]] = parsed[1]
    return out


def _fx_num(v, d=0.0):
    if isinstance(v, bool): return d
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
        if dx: rgb[y:y + hh] = np.roll(rgb[y:y + hh], dx, axis=1)


def _fx_rgb_shift(rgb, ctx):
    """rgbFilm：R/B 通道水平分离（色差重影）。"""
    off = 2 + int(round(2 * math.sin(ctx["t"] * 9.0)))
    if off == 0: return
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
        if len(_VIGN_CACHE) > 16: _VIGN_CACHE.clear()
        _VIGN_CACHE[key] = fall
    rgb[:] *= fall


_GAMMA_LUTS = {}

# sepia 基础矩阵（pixi SepiaFilter 色调）。引擎只偏移一部分原色，
# 全强度混入会吃掉原有色相（对照实测），在 _fx_color 里折半混入。
_FX_SEPIA_M = np.array([[0.393, 0.769, 0.189],
                        [0.349, 0.686, 0.168],
                        [0.272, 0.534, 0.131]], np.float32)


def _gamma_lut(g):
    """gamma 256 级 LUT（缓存；255*(i/255)^g，替代全帧 np.power）。

    v3.5.8：同时缓存 uint8 型（cv2.LUT 用，SIMD 查表比 numpy 花式索引
    快 ~7ms/帧@1080p）。uint8 量化误差 ≤1 灰阶（实测 maxdiff 0.998），
    在 qa 等价性容差（fx_map_test 9 区段 tol 2）内。"""
    g = max(0.1, g)
    ent = _GAMMA_LUTS.get(g)
    if ent is None:
        if len(_GAMMA_LUTS) > 32: _GAMMA_LUTS.clear()
        i = np.arange(256, dtype=np.float32)
        f = (255.0 * np.power(np.clip(i / 255.0, 0.0, 1.0), g)
             ).astype(np.float32)
        u8 = np.clip(f, 0.0, 255.0).astype(np.uint8)
        ent = (f, u8)
        _GAMMA_LUTS[g] = ent
    return ent


def _gamma_apply(rgb, lut_u8):
    """float 域 0..255（可越界）数组就地 gamma：clip→u8→cv2.LUT→写回。
    LUT 输出天然在 [0,255]，调用方无需再 clip。
    v3.5.8：clip+astype 合并为 cv2.multiply 饱和转换（10.7→5.3ms，
    舍入 vs 截断 ≤1 灰阶——本就在 256 级量化域内，qa 容差 2 通过）。"""
    import cv2
    u8 = cv2.multiply(rgb, 1.0, dtype=cv2.CV_8U)
    rgb[:] = cv2.LUT(u8, lut_u8)


def _fx_color(rgb, p):
    """sepia / brightness / contrast / saturation / gamma / colorRGB 调色链。

    v3.5.7 提速：①sepia 折半混入本身也是仿射
    （rgb*(1-0.5k) + (rgb@S.T*1.07+8)*(0.5k) = rgb@M_sep.T + c_sep），与
    brightness/contrast/colorRGB/saturation 全部合并成单个 3x3 矩阵 +
    偏移，一次 matmul 替代原来的 5 趟全帧扫描；②gamma 用 256 级 LUT
    查表替代全帧 np.power。gamma 从链中间挪到仿射之后（仅 sepia/gamma
    **同时**使用时像素与 v3.5.6 有微小观感差，其余情况逐像素等价）。

    v3.5.8 提速：③仿射矩阵乘 cv2.transform（OpenCV SIMD 多核；实测
    13.5 vs 16.4ms/帧@1080p，maxdiff 6e-5；原地 dst= 反而慢 7ms，
    保持分配新数组）；④gamma 走 uint8 cv2.LUT（15.8 vs 23.1ms，
    量化误差 ≤1 灰阶在容差内）；⑤LUT 后输出天然有界，省一次全帧 clip。"""
    import cv2
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
        if ga != 1.0: _gamma_apply(rgb, _gamma_lut(ga)[1])
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
    rgb[:] = cv2.transform(rgb, M)
    if c.any(): rgb += c
    ga = _fx_num(p.get("gamma"), 1.0)
    if ga != 1.0:
        _gamma_apply(rgb, _gamma_lut(ga)[1])   # 输出有界，免尾部 clip
        return
    np.clip(rgb, 0, 255, out=rgb)


_FX_BLOOM_K = 1.1          # bloom 校准系数（对照引擎实测调整）


def _fx_bloom(rgb, p):
    """bloom：亮区提取 → 高斯模糊 → 叠加泛光。

    v3.5.7 提速：整条管线在 **1/4 分辨率**上做（亮区提取/模糊本来就是要
    大团低频的泛光，降采样视觉等效），模糊核同比例缩小再放大叠加，
    全帧只留最后一次加法。"""
    import cv2
    b = _fx_num(p.get("bloom"))
    if b <= 0: return
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
    if k % 2 == 0: k += 1
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
    if p.get("glitchFilm"): _fx_glitch(rgb, ctx)
    if p.get("rgbFilm"): _fx_rgb_shift(rgb, ctx)
    b = _fx_num(p.get("blur"))
    if b > 0:
        k = max(1, int(round(b))) * 2 + 1
        h, w = rgb.shape[:2]
        # v3.5.7：大核模糊在 1/2 分辨率做（核同比例缩小再放大），视觉等效；
        # 小核（k<9）直接全帧，省两次 resize。
        if k >= 9 and h >= 4 and w >= 4:
            kh = max(1, k // 2)
            if kh % 2 == 0: kh += 1
            small = cv2.resize(rgb, (w // 2, h // 2),
                               interpolation=cv2.INTER_AREA)
            rgb[:] = cv2.resize(cv2.GaussianBlur(small, (kh, kh), 0), (w, h),
                                interpolation=cv2.INTER_LINEAR)
        else: rgb[:] = cv2.GaussianBlur(rgb, (k, k), 0)
    # v3.5.7：sepia 已并入 _fx_color 的合并矩阵（见其 docstring）
    _fx_color(rgb, p)
    _fx_bloom(rgb, p)
    ns = _fx_num(p.get("noise"))
    if ns > 0: _fx_noise(rgb, min(1.0, ns), ctx)
    if p.get("oldFilm"): _fx_oldfilm(rgb, ctx)
    if p.get("vignetting"): _fx_vignette(rgb, _fx_num(p.get("vignettingAlpha"), 0.5))
    np.clip(rgb, 0, 255, out=rgb)


def _fx_chain_rgba(frgba, p, ctx):
    """fig 层滤镜（非预乘 RGBA：调色作用于 rgb，alpha 参数乘 alpha 通道）。"""
    import cv2
    rgb = frgba[..., :3].astype(np.float32)
    _fx_chain(rgb, p, ctx)
    # v3.5.8：饱和转换替代 clip+astype（≤1 灰阶舍入差，见 _gamma_apply）
    frgba[..., :3] = cv2.multiply(rgb, 1.0, dtype=cv2.CV_8U)
    al = _fx_num(p.get("alpha"), 1.0)
    if al != 1.0:
        al = min(1.0, max(0.0, al))
        frgba[..., 3] = (frgba[..., 3].astype(np.float32) * al).astype(np.uint8)


# v3.5.8（task-74）：静态源贴层缓存——ImageSource/占位卡的 frame() 恒返回
# 同一 ndarray（is_static），其 resize/warp/crop 结果逐帧相同，缓存后每帧
# 只剩 alpha-over（46ms → ~5ms @1080p 实测目标）。键含 id(img)：
# SourceManager 持有 source 实例、source 持有静态数组引用，id 在使用期
# 内不复用；close_all 清缓存兜底。上限 8 条（1080p float 层 ~25MB/条，
# 立绘子矩形远小于全帧，实际占用低一个量级）。
_PASTE_CACHE = {}
_PASTE_CACHE_MAX = _PASTE_CACHE_BASE = 8


def _paste_prep(img, lay, W, H):
    """_paste_rgba 的无副作用前置段：resize/旋转/裁剪（**保持 u8**）。
    返回 (sub_rgb_u8, sub_a_u8, dx0, dy0, dx1, dy1, opaque) 或 None（画面外）。
    v3.5.8：不再提前转 float——opaque（alpha 全 255，如剪影占位卡/不透明
    贴图）源在 _paste_over 走直接拷贝快路径（实测 25.9→1.2ms），float
    转换只在真混合时付出。"""
    import cv2
    x, y, sc, rot = lay["x"], lay["y"], lay["scale"], lay["rot"]
    if img.ndim == 2: img = np.dstack([img] * 3)
    h, w = img.shape[:2]
    if img.shape[2] == 3: img = np.dstack([img, np.full((h, w), 255, np.uint8)])
    s0 = max(W / w, H / h) if lay.get("cover") else min(W / w, H / h)
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
    if dx1 <= dx0 or dy1 <= dy0: return None
    sx0, sy0 = dx0 - ox, dy0 - oy
    sub_rgb = rgb[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)]
    sub_a = a[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)]
    opaque = bool(sub_a.min() == 255)
    return sub_rgb, sub_a, dx0, dy0, dx1, dy1, opaque


# v3.5.8：_paste_over 的 float 工作区（消除逐帧大分配）。
# **必须线程局部**：帧级并行导出时多个线程同时跑 alpha-over，共享一块
# 模块级缓冲会互相踩（实测 12/12 帧串色，maxdiff 154）。改为 thread-local
# 后每线程各一套，单线程行为与之前完全相同（只是首次多分配一次）。
_OV_LOCAL = threading.local()


def _ov_buf(key, shape):
    bufs = getattr(_OV_LOCAL, "bufs", None)
    if bufs is None: bufs = _OV_LOCAL.bufs = {}
    b = bufs.get(key)
    if b is None or b.shape != shape:
        b = np.empty(shape, np.float32)
        bufs[key] = b
    return b


def _paste_over(frgba, prep):
    """alpha-over 累积（每帧必做部分）。opaque 源直接拷贝（语义与 float
    路径逐像素一致：a=1 时 out=sub、dst alpha 被覆盖为 255）。

    v3.5.8 提速：①alpha 包围盒裁剪——silhouette/立绘四周全透明边距在
    sa=0 处数学上恒等（oa=da → ro=dst），跳过不写回，结果逐像素不变；
    ②float 工作区复用（_ov_buf）+ 全程 out= 原地，消除 6 个大数组的
    逐帧分配。算子顺序与原实现一致 → 位级等价。"""
    sub_rgb, sub_a, dx0, dy0, dx1, dy1, opaque = prep
    if opaque:
        frgba[dy0:dy1, dx0:dx1, :3] = sub_rgb
        frgba[dy0:dy1, dx0:dx1, 3] = 255
        return
    # ① alpha 包围盒（两次 u8 reduction，微秒级）
    rows = np.any(sub_a, axis=1)
    if not rows.any(): return
    cols = np.any(sub_a, axis=0)
    r0 = int(np.argmax(rows))
    r1 = len(rows) - int(np.argmax(rows[::-1]))
    c0 = int(np.argmax(cols))
    c1 = len(cols) - int(np.argmax(cols[::-1]))
    if c1 <= c0 or r1 <= r0: return
    sub_rgb = sub_rgb[r0:r1, c0:c1]
    sub_a = sub_a[r0:r1, c0:c1]
    dx0 += c0
    dy0 += r0
    dx1 = min(dx1, dx0 + (c1 - c0))
    dy1 = min(dy1, dy0 + (r1 - r0))
    h, w = sub_a.shape
    if dx1 <= dx0 or dy1 <= dy0: return
    # ② 原地 float 合成（公式与原实现逐步对应；sub_a 是 2D，统一扩 3 通道形）
    sa = _ov_buf("sa", (h, w, 1))
    np.multiply(sub_a[..., None], 1.0 / 255.0, out=sa)
    dst = _ov_buf("dst", (h, w, 4))
    np.copyto(dst, frgba[dy0:dy1, dx0:dx1])
    sr = _ov_buf("sr", (h, w, 3))
    np.copyto(sr, sub_rgb)
    t = _ov_buf("t", (h, w, 1))
    oa = _ov_buf("oa", (h, w, 1))
    np.subtract(1.0, sa, out=t)                       # t = 1 - sa
    dsa = dst[..., 3:4]
    dsa *= (1.0 / 255.0)                              # da（原地）
    dr = dst[..., :3]
    dr *= dsa                                         # dst_rgb *= da
    dr *= t                                           # dst_rgb*da*(1-sa)
    sr *= sa                                          # sub_rgb*sa
    sr += dr                                          # num
    np.multiply(dsa, t, out=t)                        # t = da*(1-sa)
    np.add(sa, t, out=oa)                             # oa = sa + da*(1-sa)
    np.maximum(oa, 1e-3, out=oa)
    sr /= oa                                          # ro = num/oa
    np.clip(sr, 0, 255, out=sr)
    frgba[dy0:dy1, dx0:dx1, :3] = sr.astype(np.uint8)
    oa *= 255.0
    np.clip(oa, 0, 255, out=oa)
    frgba[dy0:dy1, dx0:dx1, 3] = oa[..., 0].astype(np.uint8)


def _paste_rgba(frgba, img, lay, static=False):
    """把素材帧（RGB/RGBA）按 layout 以 alpha-over 累积进透明立绘层。
    static=True（v3.5.8）：img 为恒定的静态源数组，resize/warp/crop 结果
    缓存复用；调用方必须保证该 img 在缓存期内不被原地改写。"""
    if not static:
        prep = _paste_prep(img, lay, frgba.shape[1], frgba.shape[0])
        if prep is not None: _paste_over(frgba, prep)
        return
    H, W = frgba.shape[:2]
    key = (id(img), round(float(lay["x"]), 6), round(float(lay["y"]), 6),
           round(float(lay["scale"]), 6), round(float(lay["rot"]), 6), W, H)
    # v3.5.8 并行正确性：键用 id(img)，但 id 在对象释放后可被复用——
    # 多 SourceManager 并行解码时，A 线程的素材死了、B 线程新对象可能
    # 落在同一地址 -> 命中别人的 prep 会串帧（实测 maxdiff 154）。
    # 缓存值同时持有 img 强引用，命中时 `is` 校验身份；不一致即视为
    # 未命中重算。顺带让缓存自己钉住静态帧，避免中途被 GC 复用地址。
    with _CACHE_LOCK:
        ent = _PASTE_CACHE.get(key)
    if ent is not None and ent[0] is img: prep = ent[1]
    else:
        prep = _paste_prep(img, lay, W, H)
        with _CACHE_LOCK:
            if len(_PASTE_CACHE) >= _PASTE_CACHE_MAX:
                # 旧写法是整体 clear()：新键一来就把别的线程正在用的全丢掉
                # （并行导出下等于永久 miss）。改 LRU 弃最旧 + 字节兜底。
                _PASTE_CACHE.pop(next(iter(_PASTE_CACHE)), None)
            _PASTE_CACHE[key] = (img, prep)
            _cache_shrink(_PASTE_CACHE, _PASTE_BUDGET_MB * 1048576, keep=2)
    if prep is not None: _paste_over(frgba, prep)


def _paste_cache_clear():
    with _CACHE_LOCK:
        _PASTE_CACHE.clear()


# ffmpeg 候选（与 l2d_rig_qt.FFMPEG_CANDIDATES 同步；勿 import 那边）
_HERE = os.path.dirname(os.path.abspath(__file__))
FFMPEG_CANDIDATES = [os.path.join(_HERE, "ffmpeg.exe"),
                     r"D:\BaiduNetdiskDownload\Vocal2Midi\ffmpeg.exe"]


def ffmpeg_path():
    return next((p for p in FFMPEG_CANDIDATES if os.path.exists(p)), None)


# ---------------- 单帧合成 ----------------
# v3.5.8 并行正确性：模块级缓存在多线程合成下共享（帧级并行 & bg∥fig
# 双线程），结构操作（LRU 淘汰 / clear / 写入）必须持锁——只在读写字典
# 结构时持锁，重计算与大数组运算都在锁外，不损失并行度。
_CACHE_LOCK = threading.RLock()

_BG_CACHE = {}

# v3.5.8（task-74）：bg∥fig 双线程池（模块级复用，避免逐帧建线程；
# 导出进程生命周期内 2 worker 常驻，成本可忽略）
_FX_POOL = None


def _fx_pool():
    global _FX_POOL
    if _FX_POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        _FX_POOL = ThreadPoolExecutor(max_workers=2,
                                      thread_name_prefix="mfx")
    return _FX_POOL


# v3.5.8（task-74）：层结果缓存——"静态输入 + 非时变滤镜"时整层滤镜
# 输出逐帧相同，缓存后 bg/fig pass 只剩一次 copy（10ms 级）。
# 拦截口径 = **时变算子黑名单**（读 ctx.t/seed 的四个）；其余键无论
# 导出链是否消费（如引擎侧 bevel*）都不影响"同参数同输出"，放行并把
# 参数值计入签名（值变即键变，正确性自洽）。上限 2 份防内存
# （float 1080p 层 ≈25MB/份、RGBA ≈33MB/份）。
_FX_TIMEVARYING_KEYS = frozenset({
    "glitchFilm", "rgbFilm", "noise", "oldFilm",
})
_BG_FX_CACHE = {}
_FIG_FX_CACHE = {}
# v3.5.8：上限 4 + LRU 淘汰（clear 全弃在多签名场景交替 thrash——
# 真实工程 4 个时刻 4 种背景+2 种 fig 滤镜签名，2 上限永远未命中）
_FX_CACHE_MAX = _FX_CACHE_BASE = 4
_CACHE_BUDGET_MB = 256        # bg+fig 两层缓存合计真实字节上限
_PASTE_BUDGET_MB = 192        # 贴层缓存（每条同时钉住源图 + prep）


def _entry_bytes(v):
    """缓存条目的实际占用：单层数组，或 (源图, prep) 两份。"""
    if isinstance(v, tuple): return sum(getattr(a, "nbytes", 0) for a in v)
    return getattr(v, "nbytes", 0)


def _cache_shrink(cache, budget_bytes, keep=1):
    """按**实际 nbytes**淘汰最旧条目（条数上限只是下限，内存才是硬账）。

    为什么必须按字节：1080p 一份 fig 层 W*H*4=8.3MB、bg 层 W*H*3=6.2MB，
    而贴层缓存一条是 (源图, prep) **两份**数组。实测 workers=8 时按条数
    兜底会放到 718MB 峰值，改按字节后压回预算内——宁可退回 miss 重算，
    也不让导出把机器压进交换分区（那样比 miss 慢得多）。"""
    if budget_bytes <= 0: return
    while len(cache) > keep:
        tot = sum(_entry_bytes(v) for v in cache.values())
        if tot <= budget_bytes: break
        cache.pop(next(iter(cache)), None)


def tune_cache_capacity(workers):
    """帧级并行导出前按线程数定层缓存容量，返回实际生效的 (fx, paste)。

    **为什么容量必须随线程数走**：立绘层缓存键含 `id(img)`、贴层缓存键同
    样（`id` 复用串帧的教训见 _paste_rgba 注释，值里持强引用 + `is` 校验是
    必须的，不能改成按内容去重）。而每个合成线程各有独立 SourceManager，同
    一素材在 N 个线程里就是 N 个不同对象 → **同时在用的键数随 N 线性增长**。
    固定 cap=4 是串行时代调的：workers=8 时任一线程刚写入就被别人逐出，
    立绘层 100% 未命中、每帧重算整层滤镜。60s 真实切片实测：
        w=4  94.3fps（fig 层 1544 命中 / 4 miss，4 线程刚好压线）
        w=8  18.7fps（fig 层 0 命中 / 1548 miss —— 全灭）
        w=8 把 cap 提到 20 → 106.7fps（5.7x，缺陷坐实在容量，不在 cv2）
    背景层缓存按 (路径+mtime+布局) 建键、能跨线程复用，所以不受影响——
    这正是「键里带对象地址的那些才需要扩容」的对照证据。

    扩容不是无限放大：条数下限取 2×线程数，**实际驻留由 _cache_shrink 按
    真实字节砍**（bg+fig 合计 _CACHE_BUDGET_MB、贴层 _PASTE_BUDGET_MB）。
    实测 1080p workers=8 按条数兜底放到 718MB 峰值，按字节兜底后压回
    预算内；4K 会先被字节砍到几条，宁可 miss 也不换页。"""
    try:
        nw = max(1, int(workers))
    except (TypeError, ValueError):
        return _FX_CACHE_MAX, _PASTE_CACHE_MAX
    cap_fx = max(_FX_CACHE_BASE, 2 * nw)
    cap_p = max(_PASTE_CACHE_BASE, 2 * nw)
    globals()["_FX_CACHE_MAX"] = cap_fx
    globals()["_PASTE_CACHE_MAX"] = cap_p
    return cap_fx, cap_p


def _fx_sig(p):
    """滤镜参数字典 -> 可哈希签名；None = 不可缓存（空/含时变算子）。"""
    if not p: return None
    ks = []
    for k, v in p.items():
        if k in _FX_TIMEVARYING_KEYS: return None
        try:
            ks.append((k, round(float(v), 6)))
        except (TypeError, ValueError):
            ks.append((k, str(v)))
    return tuple(sorted(ks))


def _fx_cache_put(cache, key, layer):
    # LRU：命中/新写入都移到队尾，超限弃最旧（不整体 clear）
    with _CACHE_LOCK:
        cache.pop(key, None)
        while len(cache) >= _FX_CACHE_MAX:
            cache.pop(next(iter(cache)), None)
        cache[key] = layer.copy()
        _fx_budget_shrink()


def _fx_budget_shrink():
    """bg+fig 两份层缓存**合计**不超 _CACHE_BUDGET_MB（调用方须持锁）。

    条数上限保证 N 个线程的键能同时驻留，字节上限保证不把机器压进换页——
    两者缺一不可：只给条数，workers=8 实测峰值 718MB；只给字节，8 线程
    互相逐出回到 18.7fps。逐出顺序取条目多的那边（并列取 fig，它单份更大，
    先弃它省得多）。"""
    budget = _CACHE_BUDGET_MB * 1048576
    while len(_BG_FX_CACHE) + len(_FIG_FX_CACHE) > 2:
        tot = (sum(_entry_bytes(v) for v in _BG_FX_CACHE.values())
               + sum(_entry_bytes(v) for v in _FIG_FX_CACHE.values()))
        if tot <= budget: break
        if len(_FIG_FX_CACHE) >= len(_BG_FX_CACHE) and _FIG_FX_CACHE:
            _FIG_FX_CACHE.pop(next(iter(_FIG_FX_CACHE)), None)
        elif _BG_FX_CACHE: _BG_FX_CACHE.pop(next(iter(_BG_FX_CACHE)), None)
        else: _FIG_FX_CACHE.pop(next(iter(_FIG_FX_CACHE)), None)


def _fx_cache_get(cache, key):
    """线程安全取缓存层（返回内部数组引用，调用方只读、需要写就 copy）。"""
    with _CACHE_LOCK:
        return cache.get(key)


def _fx_cache_clear():
    with _CACHE_LOCK:
        _BG_FX_CACHE.clear()
        _FIG_FX_CACHE.clear()


def _bg_cache_key(jobs, W, H):
    """静态背景缓存键（仅 image 源：路径+mtime+布局+画布尺寸）。
    视频/puppet 源逐帧变化，一律不缓存。"""
    ks = []
    for img, lay, srcd in jobs:
        if not isinstance(srcd, dict) or srcd.get("kind") != "image": return None
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
    if not jobs: return
    key = _bg_cache_key(jobs, W, H)
    if key is not None:
        with _CACHE_LOCK:
            cached = _BG_CACHE.get(key)
        if cached is not None:
            bgc[...] = cached
            return
    for img, lay, _sd in jobs:
        paste_layer(bgc, img, lay)
    if key is not None:
        # v3.5.8：LRU@4（原 clear@2——与 _BG_FX_CACHE 上限对齐，多背景
        # 工程不再互相踢废；fx_map 9-5/9-6 的 len==1/2 断言兼容）
        with _CACHE_LOCK:
            _BG_CACHE.pop(key, None)
            while len(_BG_CACHE) >= 4:
                _BG_CACHE.pop(next(iter(_BG_CACHE)), None)
            _BG_CACHE[key] = bgc.copy()


def source_key(src):
    return (src.get("kind"), os.path.normpath(str(src.get("path") or "")))


def fit_paste(canvas, img):
    """等比缩放居中贴到画布（fit，黑边）——第一阶段唯一适配模式。"""
    H, W = canvas.shape[:2]
    h, w = img.shape[:2]
    if h == 0 or w == 0: return canvas
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
        if c["start"] - V.EPS <= t < c["start"] + c["dur"] - V.EPS: return c
    return None


def _over_rgba(dst3, rgb, a):
    """rgb(u8 HxWx3) 按 u8 alpha a(HxW) source-over 混合进 dst3（就地）。
    out = round(rgb*a/255 + dst*(255-a)/255)。

    v3.6.2 提速（实测驱动）：原 float32 逐像素路径（astype→乘加→回写）
    1080p 全帧 43ms，是普通预览卡顿的主因（探针 perf_probe_blend.py）；
    改 cv2 SIMD u8 饱和运算后 5.3ms，最大像素差 1、>1 占比 0%（视觉无感，
    且导出本就重编码）。入参常是画布的**不连续视图**，cv2 要求连续 Mat：
    先 ascontiguousarray 拷进工作区（纯 memcpy，比 float 数学便宜一个量级），
    算完一次拷回视图。alpha 由调用点直接给 u8（省掉原 float 归一的分配）。

    dst3 是 float32（FX 路径 _paste_bg_layer 的浮点背景层）时保留旧
    float32 公式：cv2 不接受混合类型输入，且该路径低频（有整层缓存），
    不值得为它再优化。"""
    if dst3.dtype == np.uint8:
        import cv2
        a3 = cv2.cvtColor(np.ascontiguousarray(a), cv2.COLOR_GRAY2BGR)
        fg = cv2.multiply(np.ascontiguousarray(rgb), a3,
                          dtype=cv2.CV_8U, scale=1.0 / 255.0)
        inv = cv2.subtract(255, a3)
        bg = cv2.multiply(np.ascontiguousarray(dst3), inv,
                          dtype=cv2.CV_8U, scale=1.0 / 255.0)
        cv2.add(fg, bg, dst=fg)
        dst3[...] = fg
        return
    a2f = np.asarray(a, np.float32)[..., None] * (1.0 / 255.0)
    fg = rgb.astype(np.float32)
    out = fg * a2f + dst3.astype(np.float32) * (1.0 - a2f)
    np.add(out, 0.5, out=out)
    dst3[...] = out.astype(np.uint8)


def layer_rect(w, h, W, H, lay):
    """按 layout 计算素材在画布上的外接矩形（StageView 命中/画框与
    paste_layer 共用同一数学，取整一致）。

    - **cover=True 基准 s0=max(W/w,H/h)（铺满并裁掉溢出），否则 min（fit
      留边）**；实际缩放 = s0*lay["scale"]
    - 中心放 (x*W, y*H)
    - rot==0：精确矩形；rot!=0：旋转后的外接矩形（与 paste_layer 同式）
    返回 (px, py, pw, ph) 画布像素坐标。

    这里**必须**认 cover：paste_layer/_paste_prep 先按 cover 把图放大到
    1920×1440，再拿本函数的矩形决定贴多大——早先本函数硬编码 min，
    于是 4:3 背景放大后只贴进 1365×1024 的 fit 框，左右各剩 277px 黑边
    （用户报「背景没有占满画面」）。旧 cover 用例没抓到是因为拿 16:9 图
    测的：cover 后尺寸恰好等于画布，两种口径结果相同。"""
    try:
        x, y = lay["x"], lay["y"]
        sc, rot = lay["scale"], lay["rot"]
    except Exception:
        return (0, 0, 0, 0)
    if w <= 0 or h <= 0 or W <= 0 or H <= 0: return (0, 0, 0, 0)
    s0 = max(W / w, H / h) if lay.get("cover") else min(W / w, H / h)
    s = s0 * sc
    nw = max(1, int(round(w * s)))
    nh = max(1, int(round(h * s)))
    cx, cy = x * W, y * H
    if rot == 0.0: return (int(round(cx - nw / 2.0)), int(round(cy - nh / 2.0)), nw, nh)
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
    if is_default and img.ndim == 2: img = np.dstack([img] * 3)
    if is_default and img.ndim == 3 and img.shape[2] == 3:
        if img.shape[0] == H and img.shape[1] == W: canvas[...] = img
        else: fit_paste(canvas, img)
        return canvas
    # ---- 通用路径 ----
    if img.ndim == 2: img = np.dstack([img] * 3)
    if img.shape[2] >= 4: rgb, alpha = img[:, :, :3], img[:, :, 3]
    else: rgb, alpha = img[:, :, :3], None
    h, w = rgb.shape[:2]
    if h == 0 or w == 0: return canvas
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
        if dx1 <= dx0 or dy1 <= dy0: return canvas
        sx0, sy0 = dx0 - ox, dy0 - oy
        sub_rgb = rgb[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)]
        dst = canvas[dy0:dy1, dx0:dx1]
        if alpha is None: dst[...] = sub_rgb                  # 全不透明：直贴
        else:
            a2 = alpha[sy0:sy0 + (dy1 - dy0), sx0:sx0 + (dx1 - dx0)]
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
                            borderValue=0)
    else:
        a2 = cv2.warpAffine(np.full((nh, nw), 255, np.uint8), M, (rw, rh),
                            flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT,
                            borderValue=0)
    dx0, dy0 = max(0, ox), max(0, oy)
    dx1, dy1 = min(W, ox + rw), min(H, oy + rh)
    if dx1 <= dx0 or dy1 <= dy0: return canvas
    sx0, sy0 = dx0 - ox, dy0 - oy
    sl = (slice(sy0, sy0 + (dy1 - dy0)), slice(sx0, sx0 + (dx1 - dx0)))
    _over_rgba(canvas[dy0:dy1, dx0:dx1], rgb[sl], a2[sl])
    return canvas


def _lip_seq_for(proj, clip, aid=None):
    """figure clip 的口型开口度序列 -> (list[float] | None, gain)。

    v3.6.4。返回的序列**按 clip 内相对时间**排列（第 k 个 = clip 第 k 帧），
    正好对上 PuppetSource.frame() 传的 `t - clip.start + src_in` 网格。

    缓存策略：按 (clip id, 语音 id, clip 起点, 时长, 增益, fps) 记一份，
    同一 clip 逐帧调用只算一次 —— 包络计算要读整段 wav，逐帧重算是灾难。

    没绑口型 / 语音缺失 / 素材读不了 -> (None, 1.0)，调用方保持「不控嘴」。
    """
    try:
        import video_model as VM
        import lip_sync as LS
    except Exception:
        return None, 1.0
    lp = VM._sanitize_lip((clip or {}).get("lip"))
    if not lp:
        return None, 1.0
    gain = float(lp.get("gain") or 1.0)
    try:
        fps = float((proj or {}).get("fps") or 30.0)
    except (TypeError, ValueError):
        fps = 30.0
    if fps <= 0:
        fps = 30.0
    cid = str((clip or {}).get("id") or "")
    # 片段内相对时间 -> 音频内时刻的偏移：
    # clip 第 0 帧对应工程时间 clip.start，而语音在工程时间 vc.t 开始，
    # 所以第 0 帧对应音频的 (clip.start - vc.t) 处；再扣掉 clip 自己的
    # src_in（源内裁剪起点）。
    start = float((clip or {}).get("start") or 0.0)
    dur = float((clip or {}).get("dur") or 0.0)
    if dur <= 0:
        return None, 1.0
    vc = VM.voice_clip_by_id(proj, lp.get("voice"), aid)
    if vc is None:
        return None, 1.0
    v_t = float(vc.get("t") or 0.0)
    src_in = float((clip or {}).get("src_in") or 0.0)
    # offset = 「片段第 0 帧对应音频里的哪一刻」：
    #   立绘 0.5s 入场、语音 1.0s 才开始 -> 第 0 帧对应音频 -0.5s（前 0.5s 静音）
    #   立绘 0s 入场、语音已经在 0s 前说过一半（src_in>0）-> 从音频 src_in 处取
    offset = (start - v_t) + src_in

    key = (cid, lp.get("voice"), round(start, 3), round(dur, 3),
           round(gain, 3), round(fps, 3), round(src_in, 3),
           str(vc.get("path") or ""))
    cache = _LIP_SEQ_CACHE
    hit = cache.get(key)
    if hit is not None:
        return hit[0], hit[1]
    env = LS.envelope(vc.get("path"), "")
    if env is None:
        return None, 1.0
    vals = env.bake(fps, dur, offset=offset)
    if len(cache) > 64:                 # 简单防膨胀（导出整片也就几十条）
        cache.clear()
    cache[key] = (vals, gain)
    return vals, gain


_LIP_SEQ_CACHE = {}


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
    if canvas is None: canvas = np.zeros((H, W, 3), np.uint8)
    else: canvas[...] = 0
    fx_map = _active_fx(proj, t, aid) if fx else None
    if not fx_map:
        # ---- 旧逐轨叠加路径（无滤镜：逐像素零 diff 保证）----
        for tr in V._tracks_of(proj, aid):      # 列表序 = 底→顶（后加的在上）
            if tr.get("kind") != "video" or tr.get("hidden"): continue
            clip = _track_clip_at(tr, t)
            if clip is None: continue
            try:
                src = src_get(clip["source"])
                # v3.5.11：L2D 源必须先按 clip 灌 motion/expression，否则
                # 查询预渲染缓存用的键是空的 -> miss -> 退回占位卡（剪影）。
                # FX 分支同样处理，两条路径都要有。
                setp = getattr(src, "set_preset", None)
                if callable(setp):
                    setp(clip.get("motion", ""), clip.get("expression", ""))
                # **clip["src_in"] 可能不存在**（手写/外部构造的 clip 不带它，
                # 只有 V.new_clip 会补）。原来直接下标取 -> KeyError -> 被
                # 外层 except 吞掉 -> 整条轨静默跳过 -> 画面全黑。
                # 这是既有健壮性缺口：测试用 compose_frame 造像素对照时
                # 一路黑屏，排查了很久才发现是这里。
                img = src.frame(max(0.0, t - clip["start"]
                                    + float(clip.get("src_in") or 0.0)))
            except Exception:
                continue
            if img is None or img.size == 0: continue
            paste_layer(canvas, img, V.layout_at(clip))
    else:
        # ---- v3.5.6 分层合成（滤镜按 target 作用层）----
        ctx = {"t": t,
               "seed": int(t * max(1.0, float(proj.get("fps") or 30))) & 0x7FFFFFFF}
        fig = None                                # v3.5.9：惰性分配（命中直接 over）
        bg_jobs, fig_jobs, main_jobs = [], [], []
        for tr in V._tracks_of(proj, aid):
            if tr.get("kind") != "video" or tr.get("hidden"): continue
            clip = _track_clip_at(tr, t)
            if clip is None: continue
            lay = V.layout_at(clip)
            is_fig = str(tr.get("name") or "").startswith("FIG")
            try:
                src = src_get(clip["source"])
                # v3.5.11：真 L2D 源按自身 clip 的 motion/expression 渲染
                # （渲染器出图是「透明底立绘本体」，layout 仍由 _paste_rgba
                # 应用，与图片源同一条路径）
                setp = getattr(src, "set_preset", None)
                if callable(setp):
                    setp(clip.get("motion", ""), clip.get("expression", ""))
                # v3.6.4 口型：把「本 clip 的开口度序列」挂到源上。
                # 序列在 _lip_seq_for() 里按工程算一次并缓存（同一 clip
                # 逐帧调用只算一遍）；没有绑定时返回 None，源会保持
                # 「不控嘴」，模型自己按动作/物理走。
                if hasattr(src, "_lip_vals"):
                    _lip, _gain = _lip_seq_for(proj, clip, aid)
                    src._lip_vals = _lip
                    src._lip_gain = _gain
                # 同旧路径：src_in 可能缺失，不能直接下标（否则 KeyError
                # 被外层吞掉 -> 该轨静默跳过 -> 合成全黑）
                img = src.frame(max(0.0, t - clip["start"]
                                    + float(clip.get("src_in") or 0.0)))
            except Exception:
                continue
            if img is None or img.size == 0: continue
            if bool(lay.get("cover")): bg_jobs.append((img, lay, clip.get("source")))
            elif is_fig:
                fig_jobs.append((img, lay,
                                 bool(getattr(src, "is_static", False))))
            else: main_jobs.append((img, lay))
        bg_fx = fx_map.get("bg-main") or fx_map.get("bg")
        fig_fx = None
        for k, v in fx_map.items():
            if k.startswith("fig"): fig_fx = v

        def _bg_pass():
            # v3.5.8：静态背景 + 纯空间滤镜 -> 整层结果缓存（bgc 是
            # float32 全帧，copy ~8ms，替代 paste+chain 的 ~60ms）
            bg_key = _bg_cache_key(bg_jobs, W, H)
            fsig = _fx_sig(bg_fx) if bg_key is not None else None
            ck2 = (bg_key, fsig) if fsig is not None else None
            # 命中前提：底层 _BG_CACHE 条目仍在——测试/清缓存语义下
            # （_BG_CACHE.clear()）派生的 fx 缓存一并失效，不留孤儿命中。
            if ck2 is not None:
                with _CACHE_LOCK:
                    hit = _BG_FX_CACHE.get(ck2) if bg_key in _BG_CACHE else None
                if hit is not None:
                    # v3.5.9 冗余修复：命中的是 uint8 层，**直写 canvas**——
                    # 旧路 hit(uint8)→bgc(float32)→canvas(uint8) 是双重转换
                    # 还白分配 25MB float32，这里省掉全部中间环节。
                    canvas[...] = hit
                    return
            # 非命中才分配 float32 bgc（命中路径零分配）
            bgc = np.zeros((H, W, 3), np.float32)
            _paste_bg_layer(bgc, bg_jobs, W, H)
            if bg_fx: _fx_chain(bgc, bg_fx, ctx)
            np.clip(bgc, 0, 255, out=bgc)
            canvas[...] = bgc
            if ck2 is not None:
                # 缓存存 uint8（与 canvas 的 float32→uint8 截断转换逐像素
                # 一致），命中时才能直写 canvas；存储体积也降到 1/4。
                _fx_cache_put(_BG_FX_CACHE, ck2, bgc.astype(np.uint8))

        def _fig_pass():
            nonlocal fig                        # v3.5.9：惰性分配需回写外层
            # v3.5.8：全部立绘为静态源（is_static）+ 纯空间 fig 滤镜 ->
            # 立绘层整层缓存（paste+chain ~80ms -> copy ~12ms）
            fsig = _fx_sig(fig_fx)
            fkey = None
            if fsig is not None and fig_jobs and \
                    all(st for _i, _l, st in fig_jobs):
                fkey = tuple(
                    (id(img), round(float(lay["x"]), 6),
                     round(float(lay["y"]), 6),
                     round(float(lay["scale"]), 6),
                     round(float(lay["rot"]), 6))
                    for img, lay, _st in fig_jobs) + (fsig, W, H)
            if fkey is not None:
                hit = _fx_cache_get(_FIG_FX_CACHE, fkey)
                if hit is not None:
                    # v3.5.9：跳过 fig 层分配与拷贝，直接随 bbox over 叠进
                    # canvas——命中帧消除 8.3MB 分配 + 全帧拷贝。
                    alpha = hit[..., 3]
                    rows_nz = np.any(alpha > 0, axis=1)
                    if rows_nz.any():
                        cols_nz = np.any(alpha > 0, axis=0)
                        ys = np.nonzero(rows_nz)[0]
                        xs = np.nonzero(cols_nz)[0]
                        by, bx = int(ys[0]), int(xs[0])
                        bh, bw = int(ys[-1]) - by + 1, int(xs[-1]) - bx + 1
                        sub_fig = hit[by:by + bh, bx:bx + bw]
                        sub = canvas[by:by + bh, bx:bx + bw]
                        a = sub_fig[..., 3:4].astype(np.float32) / 255.0
                        sub[...] = (sub_fig[..., :3] * a
                                    + sub * (1 - a)).astype(np.uint8)
                    return
            for img, lay, st in fig_jobs:
                if fig is None: fig = np.zeros((H, W, 4), np.uint8)   # 非命中才分配
                _paste_rgba(fig, img, lay, static=st)
            if fig_fx and fig is not None: _fx_chain_rgba(fig, fig_fx, ctx)
            if fkey is not None: _fx_cache_put(_FIG_FX_CACHE, fkey, fig)

        # v3.5.8（task-74）任务3：bg 层与 fig 层完全独立（各自数组、ctx
        # 只读、_BG_CACHE/_PASTE_CACHE 分别单线程访问），numpy/cv2 大
        # 操作释放 GIL——双线程并行；单层为空时保持串行省调度开销。
        if bg_jobs and fig_jobs:
            f1 = _fx_pool().submit(_bg_pass)
            f2 = _fx_pool().submit(_fig_pass)
            f1.result()               # 异常照常上抛（语义同串行）
            f2.result()
        else:
            _bg_pass()
            _fig_pass()
        # canvas 已由 _bg_pass 填好（命中=缓存直写、非命中=转换拷贝）
        # v3.5.9：fig 层 alpha-over 先裁剪到 alpha 非零包围盒——立绘通常
        # 只占画面一块，bbox 外的 fig 全透明（混合恒等，canvas 不变），
        # 全帧 numpy 复合运算只在有内容的区域做。实测 1080p 立绘占 40%
        # 画面时 71.7 → 31.7ms，**逐像素 maxdiff 0**（纯实现替换）。
        # fig 全透明/为 None（缓存命中已直接 over）时整个跳过。
        # bbox 用 numpy 行列 any（2.4ms）而非 cv2.findNonZero（7.0ms）。
        if fig is not None:
            alpha = fig[..., 3]
            rows_nz = np.any(alpha > 0, axis=1)
        else: rows_nz = np.zeros(0, bool)
        if rows_nz.any():
            cols_nz = np.any(alpha > 0, axis=0)
            ys = np.nonzero(rows_nz)[0]
            xs = np.nonzero(cols_nz)[0]
            by, bx = int(ys[0]), int(xs[0])
            bh, bw = int(ys[-1]) - by + 1, int(xs[-1]) - bx + 1
            sub_fig = fig[by:by + bh, bx:bx + bw]
            sub = canvas[by:by + bh, bx:bx + bw]
            a = sub_fig[..., 3:4].astype(np.float32) / 255.0
            sub[...] = (sub_fig[..., :3] * a + sub * (1 - a)).astype(np.uint8)
        for img, lay in main_jobs:                  # 其余轨仍在最上层
            paste_layer(canvas, img, lay)
    if subtitle: _draw_dialogue(canvas, proj, t, aid)
    # v3.6.2：转场的视频侧近似——在背景轨（最底可见视频轨）的片段进出
    # 边界把整帧向黑场淡化（fade-through-black）。
    #   · 未设置转场 / 显式「无转场」 -> 返回 1.0，整帧**逐像素零改动**
    #     （今天视频导出本就不淡入淡出，这条只新增，不改老行为）
    #   · slide/zoom/blur/engine_only 在视频里退化成淡化（精确动画以
    #     WebGAL 可玩导出为准——compose_frame 无状态，拿不到上一画面，
    #     做不了真交叉淡化，黑场淡化是合法且观感一致的近似）
    f = _scene_fade(proj, t, aid)
    if f < 1.0: np.multiply(canvas, f, out=canvas, casting="unsafe")
    return canvas


def _scene_fade(proj, t, aid):
    """t 时刻整帧淡化系数 0..1（1=不淡）。见 compose_frame 调用点注释。"""
    tr = proj.get("transition") if isinstance(proj, dict) else None
    if not isinstance(tr, dict): return 1.0
    if not (tr.get("enter") or tr.get("exit")): return 1.0
    try:
        dur = float(tr.get("duration_ms") or 300) / 1000.0
    except (TypeError, ValueError):
        dur = 0.3
    dur = max(0.05, min(2.0, dur))
    tracks = V._tracks_of(proj, aid)
    bottom = None
    for trk in tracks:
        if isinstance(trk, dict) and trk.get("kind") == "video" \
                and not trk.get("hidden"):
            bottom = trk
            break
    if bottom is None: return 1.0
    f = 1.0
    eps = 1e-6
    for c in (bottom.get("clips") or []):
        s = float(c.get("start") or 0.0)
        e = s + float(c.get("dur") or 0.0)
        if s - eps <= t < e - eps:
            if t - s < dur: f = min(f, (t - s) / dur)
            if e - t < dur: f = min(f, (e - t) / dur)
            break
    return max(0.0, min(1.0, f))


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
            with contextlib.suppress(Exception): s.close()
        self._cache.clear()
        # v3.5.8：source 实例释放后其静态数组 id 可能被复用——贴层缓存
        # 必须随之清空（正确性兜底，见 _PASTE_CACHE 注释）。
        _paste_cache_clear()
        _fx_cache_clear()


# ---------------- 混音 ----------------
def _place(buf, samples, at_sample):
    """把 samples 放到 buf[at_sample:]，饱和截断。"""
    n = len(samples)
    i0 = max(0, at_sample)
    if i0 >= len(buf): return
    take = min(n, len(buf) - i0)
    buf[i0:i0 + take] = np.clip(buf[i0:i0 + take].astype(np.int64)
                                + samples[:take].astype(np.int64),
                                -32768, 32767).astype(np.int16)


def extract_audio(path):
    """任意媒体 -> (int16 mono, sr)。wav 直读；其余 ffmpeg 转；失败 (None, 0)。"""
    if not path or not os.path.isfile(path): return None, 0
    if path.lower().endswith(".wav"):
        try:
            with wave.open(path, "rb") as wf:
                sr = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
            a = np.frombuffer(raw, dtype=np.int16)
            if a.size % max(1, wf.getnchannels()): a = a[:-(a.size % wf.getnchannels())]
            ch = max(1, wf.getnchannels())
            mono = a.reshape(-1, ch).mean(axis=1).astype(np.int16) if ch > 1 else a
            return mono, sr
        except Exception:
            return None, 0
    ff = ffmpeg_path()
    if not ff: return None, 0
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
        with contextlib.suppress(OSError): os.remove(tmp)


def mix_audio(proj, sr=44100):
    """整工程混音 -> int16 ndarray（时长=project_duration）。无音频返回 None。

    **每个 clip 只取它自己那一段**（v3.6.1 修 BGM 串音）：
      · 起点 = 素材的 src_in（被 trim 掉的头不要）
      · 长度 = clip 的 dur（乘 speed 换算成素材时长）
    早先的实现直接把整条素材塞进 buf，导致「3 秒片段用了 4 分钟的歌 ->
    后面一直在响」，相邻/重叠片段也互相串音。
    """
    dur = V.project_duration(proj)
    if dur <= 0: return None
    total = int(dur * sr) + sr // 2
    buf = np.zeros(total, np.int16)
    any_a = False
    for tr in proj.get("tracks", []):
        if tr.get("hidden"):          # 隐藏轨不参与混音（与画面一致）
            continue
        for c in (tr.get("clips") or []):
            src = c.get("source") or {}
            kind = src.get("kind")
            if kind in ("puppet", "image"): continue
            path = src.get("path")
            if not path or str(path).startswith("color:"): continue
            samples, s2 = extract_audio(path)
            if samples is None or not len(samples): continue          # 素材没声音轨，正常
            # 先统一采样率
            if s2 != sr and s2 > 0:
                k = sr / float(s2)
                idx = (np.arange(int(len(samples) * k)) / k).astype(np.int32)
                idx = np.clip(idx, 0, len(samples) - 1)
                samples = samples[idx]

            # ---- 关键：按 clip 语义切片 ----
            speed = float(c.get("speed") or 1.0)
            if speed <= 0: speed = 1.0
            clip_dur = float(c.get("dur") or 0.0)
            src_in = max(0.0, float(c.get("src_in") or 0.0))
            # 素材内起点（秒）-> 采样索引
            s0 = int(round(src_in * sr))
            s0 = max(0, min(s0, len(samples)))
            # 该 clip 需要的素材长度（speed>1 则消耗更多素材）
            need = int(round(clip_dur * sr * speed))
            s1 = s0 + need
            seg = samples[s0:s1]
            if not len(seg): continue
            if speed != 1.0:
                # 变速：重采样到 clip_dur 的采样数
                want = int(round(clip_dur * sr))
                if want <= 0: continue
                idx2 = (np.arange(want) * (len(seg) / float(want))
                        ).astype(np.int32)
                idx2 = np.clip(idx2, 0, len(seg) - 1)
                seg = seg[idx2]

            # ---- 放到时间轴对应位置，越界安全截断 ----
            at = int(round(float(c.get("start") or 0.0) * sr))
            if at < 0:
                seg = seg[-at:]
                at = 0
            if at >= total or not len(seg): continue
            take = min(len(seg), total - at)
            _place(buf, seg[:take], at)
            any_a = True
    return buf if any_a and np.any(buf) else None


def write_wav(path, samples, sr=44100):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())


# ---------------- 仅音轨导出 / SRT 字幕导出（v3.6.2） ----------------
def export_audio_only(proj, path, sr=44100, fmt=None):
    """只导出整工程混音，不做任何视频合成。

    复用 mix_audio()（已按 clip 语义切片、修过 BGM 串音），所以产物与
    「导出视频」里的音轨**逐样本一致**——不是另写一套混音。

    fmt=None 时按扩展名推断：.wav 直写；其余（.m4a/.aac/.mp3…）先落临时
    wav 再交 ffmpeg 编码。本机 ffmpeg 是纯音频裁剪版（只有 aac/pcm，
    无视频编码器），正好够用；找不到 ffmpeg 就降级为同名 .wav 并如实告知。

    返回 (ok, msg, real_path)。无音频轨时 ok=False，不产出空文件。
    """
    samples = mix_audio(proj, sr=sr)
    if samples is None or not len(samples): return False, "工程里没有可用的音频轨（或全部静音）", path
    ext = os.path.splitext(str(path))[1].lower()
    want = (fmt or ext.lstrip(".") or "wav").lower()
    if want in ("wav", ""):
        try:
            write_wav(path, samples, sr=sr)
        except Exception as e:
            return False, "写入失败：%s" % e, path
        return True, "已导出音轨（%d 采样 / %.1f 秒）" % (
            len(samples), len(samples) / float(sr)), path

    ff = ffmpeg_path()
    if not ff:
        alt = os.path.splitext(str(path))[0] + ".wav"
        try:
            write_wav(alt, samples, sr=sr)
        except Exception as e:
            return False, "写入失败：%s" % e, alt
        return True, "未找到 ffmpeg，已改存 WAV：%s" % os.path.basename(alt), alt
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        write_wav(tmp, samples, sr=sr)
        codec = {"m4a": "aac", "aac": "aac", "mp3": "libmp3lame",
                 "ogg": "libvorbis", "flac": "flac"}.get(want, "aac")
        r = subprocess.run([ff, "-y", "-i", tmp, "-c:a", codec, str(path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0 or not os.path.isfile(path) \
                or os.path.getsize(path) <= 0:
            # 编码器不支持（本机裁剪版常见）→ 回落 WAV，绝不产 0 字节假成功
            alt = os.path.splitext(str(path))[0] + ".wav"
            write_wav(alt, samples, sr=sr)
            return True, ("%s 编码不可用，已改存 WAV：%s"
                          % (codec, os.path.basename(alt))), alt
        return True, "已导出音轨（%s）" % want, path
    except Exception as e:
        return False, "导出失败：%s" % e, path
    finally:
        with contextlib.suppress(OSError): os.remove(tmp)


def _srt_time(sec):
    """秒 -> SRT 时间戳 HH:MM:SS,mmm（毫秒逗号分隔，SRT 规范）。"""
    ms = int(round(max(0.0, float(sec)) * 1000.0))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def dialogue_events(proj, aid=None):
    """把台词表折成「不重叠的字幕事件」列表 [(t0, t1, who, text)]。

    **口径必须与画面烧录一致**（_draw_dialogue）：
      · 活跃判定 t0 - eps <= t < t0 + dur - eps
      · 多条同时活跃取**最后一条**（后写覆盖）
    所以这里不能简单按每条台词的 [t, t+dur] 直接输出——那样重叠段会
    出现两条字幕并存，而画面上只显示后一条。做法：收集所有边界时刻，
    在每个小区间里问一次「此刻画面显示哪条」，再把相邻同内容的区间合并。
    """
    try:
        dlg = V.act_view(proj, aid).get("dialogue") or []
    except Exception:
        dlg = []
    eps = 1e-6
    items = []
    for d in dlg:
        if not isinstance(d, dict): continue
        try:
            t0 = float(d.get("t"))
            dur = float(d.get("dur"))
        except (TypeError, ValueError):
            continue
        text = str(d.get("text") or "")
        if dur <= 0 or not text.strip(): continue
        items.append((t0, t0 + dur, str(d.get("who") or ""), text))
    if not items: return []
    # 所有边界（含工程末尾），去重排序
    bounds = sorted({round(x, 6) for it in items for x in (it[0], it[1])})
    events = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        if b - a <= eps: continue
        mid = (a + b) / 2.0
        active = None
        for t0, t1, who, text in items:      # 顺序遍历 → 后者覆盖前者
            if t0 - eps <= mid < t1 - eps: active = (who, text)
        if active is None: continue
        who, text = active
        if events and events[-1][2] == who and events[-1][3] == text \
                and abs(events[-1][1] - a) <= eps:
            events[-1] = (events[-1][0], b, who, text)   # 合并同内容相邻段
        else: events.append((a, b, who, text))
    return events


def export_srt(proj, path, aid=None, with_name=True):
    """导出 SRT 字幕（UTF-8 with BOM，兼容主流播放器/剪辑软件）。

    with_name=True 时把角色名作为前缀「名字：正文」，与画面上的名字牌对应；
    旁白（who 为空）不加前缀。返回 (ok, msg, n_events)。
    """
    evs = dialogue_events(proj, aid=aid)
    if not evs: return False, "工程里没有台词，未生成字幕", 0
    lines = []
    for i, (t0, t1, who, text) in enumerate(evs, 1):
        body = text.replace("\r\n", "\n").replace("\r", "\n")
        if with_name and who.strip(): body = "%s：%s" % (who.strip(), body)
        lines.append(str(i))
        lines.append("%s --> %s" % (_srt_time(t0), _srt_time(t1)))
        lines.append(body)
        lines.append("")
    try:
        with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
            f.write("\n".join(lines))
    except Exception as e:
        return False, "写入失败：%s" % e, 0
    return True, "已导出 %d 条字幕" % len(evs), len(evs)


# ---------------- 导出并行度（v3.5.8） ----------------
def export_worker_limits():
    """帧级并行线程的 (默认值, 上限)。UI 下拉与 Worker 归一化共用同一口径。

    **实测数据说了算**（1080p 真实工程 60s 切片 1800 帧端到端，含写盘混音）：
        层缓存容量修好前：1 线程 ~86fps / 4 线程 94 / **8 线程 18.7（塌方）**
        修好后（tune_cache_capacity）：4 线程 76-110 / 8 线程 83-104，
        产物 sha256 与单线程逐位一致。
    8 线程当年的塌方**不是核数不够**，是共享 LRU 容量按串行时代调的 cap=4：
    立绘层缓存键含 id(img)，每线程一套独立 SourceManager → 8 线程 8 个键
    装进 4 格缓存，互相逐出、每帧重算整层滤镜。这条已在
    tune_cache_capacity 修掉，别再按「8 档必然更慢」的老结论收上限。

    现在两档的真实差别是内存：8 线程峰值 ~748MB（4 线程 ~400MB），
    主因同样是每线程各自解码一套素材，不是缓存预算（实测把预算从 256MB
    压到 96MB 峰值仍是 ~700MB，fps 反而掉 6%）。所以：
      - 默认 = clamp(核数//4, 2, 4)：稳妥档，内存不翻倍
      - 上限 = clamp(核数//2, 4, 8)：给内存充裕的机器手动加速
    再往上（12/23 档）实测无增益（受内存带宽与 GIL 限制），不放出。"""
    try:
        cores = os.cpu_count() or 4
    except Exception:
        cores = 4
    cores = max(1, int(cores))
    default = max(2, min(4, cores // 4))
    upper = max(default, min(8, cores // 2))
    return default, upper


def normalize_workers(workers=None):
    """用户选择 -> 合法线程数；None/非法/越界一律回落到默认值。"""
    default, upper = export_worker_limits()
    if workers is None: return default
    try:
        w = int(workers)
    except (TypeError, ValueError):
        return default
    if w < 1: return default
    return max(1, min(upper, w))


# ---------------- 导出 Worker ----------------
try:
    from PySide6.QtCore import QThread, Signal
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor
    import ctypes

    # 帧级并行合成的 thread-local 状态（每合成线程独立 SourceManager）。
    # 建过的 sm 登记进 _TLS_SMS，导出结束统一 close（thread-local 无法
    # 从外部枚举，靠登记表防解码句柄泄漏）。
    _TLS = threading.local()
    _TLS_LOCK = threading.Lock()
    _TLS_SMS = []

    class VideoExportWorker(QThread):
        """按轨道时间线导出 mp4。progress(pct)，done(ok, msg)。"""
        progress = Signal(int)
        done = Signal(bool, str)

        def __init__(self, proj, out_path, preview_scale=1.0, fps=None,
                     quality=None, workers=None, l2d_animate=None, parent=None):
            super().__init__(parent)
            self.proj = proj
            self.path = out_path
            self.scale = float(preview_scale)
            # v3.5.8 导出参数（剪映式）：fps=None 跟工程帧率；否则按新帧率
            # 采样（帧数=时长×fps，t=i/fps 照常取时间线时刻，快放/慢放感
            # 由素材自身在 t 时刻的画面决定，与播放速度无关）。
            self.fps_override = None
            if fps is not None:
                with contextlib.suppress((TypeError, ValueError)):
                    f = float(fps)
                    if 1.0 <= f <= 120.0: self.fps_override = f
            # quality 保留占位：cv2 mp4v 后端实测不支持 QUALITY（set 后
            # 输出字节不变，H264 四元组假成功 0 字节），恒用默认质量。
            self.quality = quality
            # workers：帧级并行线程数。None/非法 -> 自动（核数//4，2..4）。
            # 用户可在导出对话框选；上限由 export_worker_limits() 给（UI 用
            # 同一个函数算下拉上限，避免两边口径不一致）。
            self.workers = normalize_workers(workers)
            # l2d_animate：立绘逐帧动画。**泵本身由主线程起停**
            # （l2d_pump.pump().start/stop 只能活在有事件循环的主线程），
            # worker 只拿这个标志做两件事：① False 时显式关掉全局开关，
            # 防上一次导出残留的 enabled 泄漏进来；② cancel 时叫醒阻塞在
            # 泵上的合成线程。None（脚本/测试直接 run()）= 不动全局状态。
            self.l2d_animate = None if l2d_animate is None else bool(l2d_animate)
            self._cancel = False

        def cancel(self):
            self._cancel = True
            # 叫醒可能卡在泵上的合成线程：泵在主线程被卡住的请求会占着
            # 窗口，不 abort 的话取消按钮要等到 REQUEST_TIMEOUT 才生效。
            with contextlib.suppress(Exception):
                import l2d_pump as LPM
                LPM.pump().abort()

        def _compose_parallel(self, t):
            """帧级并行合成（v3.5.8）：每线程独立 SourceManager。

            v3.5.8 定稿：**不降优先级**。线程数已由用户在导出对话框选定
            （上限按核数探测），选多少就满速用多少——不再为「移鼠标不卡」
            做 BELOW_NORMAL 妥协（用户明确：既然可选就不考虑鼠标卡顿）。"""
            sm = getattr(_TLS, "sm", None)
            if sm is None:
                sm = _TLS.sm = SourceManager()
                with _TLS_LOCK:
                    _TLS_SMS.append(sm)
            return compose_frame(self.proj, t, sm.get)

        def run(self):
            import cv2
            # 封住 cv2 内部线程池：帧级并行时嵌套多线程互相争用（实测 6/8
            # 线程退化），单帧限 1 内部线程 + N 合成线程 = 甜点。
            cv2.setNumThreads(1)
            if self.l2d_animate is False:
                # 显式关：别让上一次导出残留的 enabled=True 泄漏进这次定格导出
                with contextlib.suppress(Exception):
                    import l2d_pump as LPM
                    LPM.pump().configure(False)
            try:
                W = max(2, int(round(self.proj["width"] * self.scale)) & ~1)
                H = max(2, int(round(self.proj["height"] * self.scale)) & ~1)
                fps = self.fps_override or float(self.proj["fps"])
                n = max(1, int(round(V.project_duration(self.proj) * fps)))
                vw = cv2.VideoWriter(self.path, cv2.VideoWriter_fourcc(*"mp4v"),
                                    fps, (W, H))
                if not vw.isOpened():
                    self.done.emit(False, "无法创建视频编码器（cv2 mp4v）")
                    return
                # ---- v3.5.8 帧级流水：NW 线程乱序合成、按序写编码器 ----
                # 滑动窗口（NW*2）防止 n 帧 futures 全量积压内存。
                # NW 由用户选（对话框），归一化时已按核数封顶。
                nw = self.workers
                # 层缓存容量随线程数定档（否则 8 线程互相逐出 → 每帧重算滤镜，
                # 实测 94fps→18.7fps；依据见 tune_cache_capacity 注释）
                tune_cache_capacity(nw)
                pool = ThreadPoolExecutor(max_workers=nw)
                window = deque()
                nxt = 0
                written = 0
                while written < n:
                    if self._cancel:
                        pool.shutdown(wait=False, cancel_futures=True)
                        vw.release()
                        self.done.emit(False, "已取消")
                        return
                    while nxt < n and len(window) < nw * 2:
                        window.append(pool.submit(self._compose_parallel,
                                                  nxt / fps))
                        nxt += 1
                    frame = window.popleft().result()
                    if frame.shape[0] != H or frame.shape[1] != W:
                        frame = cv2.resize(frame, (W, H),
                                           interpolation=cv2.INTER_AREA)
                    vw.write(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
                    written += 1
                    if written % max(1, n // 50) == 0:
                        self.progress.emit(int(written / n * 80))
                pool.shutdown(wait=False)
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
                else: msg = "音轨合流失败，输出无声视频"
                for p in (wavp, finalp):
                    with contextlib.suppress(OSError): os.remove(p)
                self.done.emit(True, "%s %s" % (self.path, msg))
            except Exception as e:
                self.done.emit(False, "导出异常: %r" % e)
            finally:
                cv2.setNumThreads(0)     # 恢复 cv2 全局默认（预览还要用）
                with _TLS_LOCK:
                    for sm in _TLS_SMS:
                        sm.close_all()
                    _TLS_SMS.clear()
except ImportError:      # 无 Qt 环境（纯单测）也能 import 本模块
    VideoExportWorker = None
