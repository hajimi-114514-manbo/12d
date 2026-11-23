# -*- coding: utf-8 -*-
"""人物投影（character_shadow.py）——用贴图实现「地面上的人物影子」。

## 为什么用贴图而不是引擎特性

官方 WebGAL 4.6.6 的滤镜注册表里没有投影（详见 light_effects.py 顶部的
查证结论）。但投影本质是**一张铺在地面的图**，用普通的 image clip 就能
实现——引擎完全支持，导出后真能看到。

## 做法

  1. `make_shadow_png()` 生成一张渐变椭圆阴影（RGBA，中心深、边缘透明）
  2. 用 `place_shadow()` 算出该把它放在哪个 clip 的什么位置/尺寸
  3. 调用方把它当成普通 image clip 加到时间轴上（轨道名 `SHADOW-<角色>`）

## 为什么是椭圆而不是人物剪影

人物剪影需要按立绘 alpha 轮廓重建——技术上可行，但投影在真实光照下
本来就是**柔化模糊**的，剪影反而假。而且剪影贴图会随立绘动作每帧变化，
一张静态贴图对不上。所以用「柔边椭圆 + 贴地透视」，
这也是很多视觉小说引擎的实际做法。

## 参数语义（对齐教学视频里对方工具的概念）

  · 浓度 concentration —— 阴影中心的不透明度（0~1）
  · 柔度 softness —— 边缘羽化程度，越大越糊
  · 宽度 width_pct —— 相对人物宽度的比例（蹲/立不同）
  · 高度 height_pct —— 相对宽度的比例（透视压扁）
  · 偏移 offset_y —— 相对人物底部再往下多少（贴地）
  · 颜色 color —— 纯黑或偏冷/偏暖的暗色（环境光影响）
"""

import os
import tempfile

import numpy as np

# 默认参数（贴近真实观感：中心不浓、边缘很软）
DEFAULTS = {
    "concentration": 0.38,
    "softness": 0.55,
    "width_pct": 0.62,
    "height_pct": 0.22,
    "offset_y": 0.02,
    "color": (0, 0, 0),
}


def _clamp(v, lo, hi, d=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        f = d
    if f != f: f = d
    return max(lo, min(hi, f))


def make_shadow_rgba(w=256, h=96, concentration=0.38, softness=0.55,
                     color=(0, 0, 0)):
    """生成渐变椭圆阴影 RGBA 数组（H, W, 4），uint8。

    用椭圆距离场做羽化：
      · d = 归一化到 [0,1] 的「椭圆半径」（中心 0、边缘 1）
      · alpha = concentration * (1 - smoothstep(t))
        其中 t = clip((d - inner) / (1 - inner))，inner = 1 - softness
      · **softness 越大 → inner 越小 → 衰减起点越靠中心 → 整张图越糊**

    第一版的坑（测试 B5/B6 抓到）：我写成 `inner = 1 - soft` 却让 t 从
    inner 才开始涨，导致 softness=0.55 时中心 45% 半径是个**平台区**，
    从中心往外前 8 像素 alpha 一模一样（153,153,...）；而且 softness
    越大平台越大、非零像素反而更少，和"越糊"完全相反。
    正确做法是：softness 直接决定「平台区占比」——
    softness=0 时几乎全平（硬边椭圆），softness=1 时从中心就开始衰减。
    所以 inner 应该是 `1 - softness` 没错，但**衰减必须从 d=0 就开始**
    参与，即 t = d / 1，再按 softness 调整曲线陡度。

    最终采用两段式（清晰可控，都实测过）：
      t = d                      （椭圆半径本身）
      k = softness               （衰减带宽）
      a = 1 - smoothstep((t - (1-k)) / k)  ，t < 1-k 时 a=1
    但为了让"越糊"真的让非零面积变大，这里**把椭圆半径整体放大**
    再衰减——糊的本质是轮廓外扩+变淡。
    """
    w = max(8, int(w))
    h = max(8, int(h))
    conc = _clamp(concentration, 0.0, 1.0, 0.38)
    soft = _clamp(softness, 0.01, 0.99, 0.55)
    if conc <= 0: return np.zeros((h, w, 4), np.uint8)

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    rx, ry = w / 2.0, h / 2.0
    d = np.sqrt(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2)

    # ---- 羽化曲线（第四版；前三版都被数据否掉，教训写在下面）----
    #
    # 语义澄清（绕了四次才定下来，过程留档）：
    #   「糊」= **过渡带更宽 + 尾巴拖更长**，不是「更早开始衰减」。
    #   硬边阴影 = 平台大 + 断崖；软边 = 平台小 + 缓慢下降 + 长尾。
    #
    # 四次迭代的问题：
    #   v1 inner=1-soft*0.92   -> soft=.55 时中心 49% 全平，实心椭圆
    #   v2 spread 压缩 d       -> d 最大只到 .77，边缘永远到不了 0
    #   v3 inner=.55*(1-soft)  -> soft 越大反而更早衰减，非零占比却不变
    #   v4 plateau=1-soft*.9   -> soft=.1 时 plateau=.91，80% 处仍满值！
    #                              peak 太靠边 = 断崖来得太晚，不像"硬边"
    #
    # v5（当前）：**平台固定很小**，陡度完全由 softness 控制。
    #   plateau = 0.10 恒定 —— 无论软硬，中心一小块是实的（真实阴影都这样）
    #   g（曲线次幂）：soft 越小 -> g 越大 -> 前段掉得越快（断崖）
    #                  soft 越大 -> g 越小 -> 全程平缓（雾状）
    #   这样"非零占比"和"陡度"就不会打架：前者由 g 决定，
    #   后者由平台固定值保证始终有实心核心。
    plateau = 0.10
    t = np.clip(d, 0.0, 1.0)
    u = np.clip((t - plateau) / max(1e-6, (1.0 - plateau)), 0.0, 1.0)

    g = 0.35 + (1.0 - soft) * 2.6      # soft=0.1 -> g≈2.69（陡）；soft=.9 -> g≈.61（缓）
    fall = (1.0 - u * u * (3.0 - 2.0 * u)) ** g

    a = (conc * fall * 255.0).astype(np.float32)
    out = np.zeros((h, w, 4), np.uint8)
    r, g, b = [_clamp(c, 0, 255, 0) for c in (color + (0, 0, 0))[:3]]
    out[:, :, 0] = int(r)
    out[:, :, 1] = int(g)
    out[:, :, 2] = int(b)
    out[:, :, 3] = np.clip(a, 0, 255).astype(np.uint8)
    return out


def make_shadow_png(path=None, w=256, h=96, **kw):
    """把阴影写成 PNG，返回路径。path=None 时落临时文件。"""
    from PIL import Image
    arr = make_shadow_rgba(w, h,
                           concentration=kw.get("concentration", 0.38),
                           softness=kw.get("softness", 0.55),
                           color=kw.get("color", (0, 0, 0)))
    img = Image.fromarray(arr, mode="RGBA")
    if path is None:
        fd, path = tempfile.mkstemp(suffix="_shadow.png", prefix="muppet_")
        os.close(fd)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    img.save(path)
    return path


def place_shadow(fig_layout, proj_w, proj_h, **kw):
    """算出投影 clip 的布局（与 engine 的 layout 契约一致）。

    fig_layout: 人物 clip 的 layout（含 x/y/scale，x/y 是 0~1 相对坐标）
    返回 dict，可直接塞进 clip["layout"]：
        {x, y, scale, cover: False, _shadow: {...}}

    x/y 相对舞台；这里让影子中心对准人物脚下：
      · 水平：与人物同 x
      · 垂直：人物 y + 人物身高的一半再往下一点（人物 y 是中心点）
    """
    p = dict(DEFAULTS)
    for k in ("concentration", "softness", "width_pct",
              "height_pct", "offset_y"):
        if k in kw: p[k] = kw[k]
    p["concentration"] = _clamp(p["concentration"], 0, 1, 0.38)
    p["softness"] = _clamp(p["softness"], 0.01, 0.99, 0.55)
    p["width_pct"] = _clamp(p["width_pct"], 0.1, 2.0, 0.62)
    p["height_pct"] = _clamp(p["height_pct"], 0.05, 1.0, 0.22)
    p["offset_y"] = _clamp(p["offset_y"], -0.5, 0.5, 0.02)

    fl = fig_layout or {}
    fx = _clamp(fl.get("x", 0.5), 0.0, 1.0, 0.5)
    fy = _clamp(fl.get("y", 0.5), 0.0, 1.0, 0.5)
    fsc = _clamp(fl.get("scale", 1.0), 0.05, 8.0, 1.0)

    return {
        "x": round(fx, 4),
        # 人物 y 是中心；脚在中心下方约半个立绘高。这里用固定经验值
        # 0.18（人物立绘通常占画面 60% 高、中心在 0.5 附近 -> 脚约 0.68），
        # 再叠加用户给的 offset_y 微调。
        "y": round(min(0.98, max(0.02, fy + 0.18 + p["offset_y"])), 4),
        "scale": round(fsc * p["width_pct"], 4),
        "cover": False,
        "_shadow": p,
    }


def shadow_track_name(who):
    """投影专用的轨道名（导出/预览按 FIG 同款规则识别）。"""
    who = str(who or "").strip() or "default"
    return "SHADOW-%s" % who


def preset_concentration(name):
    """按环境光给一个合理的默认浓度（暗场景影子淡、亮场景影子浓）。"""
    table = {
        "黄昏长影": 0.42, "晨曦斜照": 0.30, "烛火暖光": 0.46,
        "月夜银蓝": 0.34, "雨夜钴蓝": 0.28, "舞台双束聚光": 0.50,
        "舞台侧逆光": 0.44, "探照窄束": 0.55, "柔光箱日常": 0.22,
        "窗边百叶光": 0.32, "霓虹紫光": 0.40, "灵体逆光": 0.18,
    }
    return table.get(str(name or ""), 0.38)