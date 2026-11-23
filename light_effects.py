# -*- coding: utf-8 -*-
"""氛围光效库（light_effects.py）——用官方 WebGAL 真支持的滤镜拼出「布光」观感。

## 为什么不是「灯光系统」

用户看的那套教学视频里，UP 主跑的是他自己的 **webgal-terre 二改引擎**，
那套「布光编辑器 / 人物投影 / 体积光束」是他引擎的扩展能力。

我扒过我们打包的官方 WebGAL 4.6.6（index-Dcp3ZA1M.js）：
  · 滤镜注册表一共 12 个：blur / oldFilm / dotFilm / reflectionFilm /
    glitchFilm / rgbFilm / godrayFilm / shockwave / adjustment /
    radiusAlpha / bevel / bloom
  · `lightColor` / `lightAlpha` 那几十处命中**全是 bevel 的浮雕参数**，
    不是布光；`lighting` 唯一一次出现是 SVG 属性名表里的 lighting-color
  · `godrayFilm` 在映射表里是 `isBoolean`——只能**开关**，
    angle/gain/alpha 全是写死的 defaults，传参无效

所以做一套「可调光强/光色/角度」的布光 UI 会是**假功能**：编辑器里调半天，
导出后官方引擎直接忽略未知字段，用户什么也看不到。这类假功能比不做更糟。

## 那这里做什么

把 12 个真滤镜里**与光相关的那几个**（bevel 边缘光 / bloom 泛光 /
godrayFilm 体积光 / adjustment 调色）组合成一套「氛围光效」——
每条都是一组调好的参数，卡片式一点即用，导出的 setTransform 官方引擎
**真会渲染**。

参数设计依据（都是实测过的语义）：
  · bevel —— `color.a - light` 判边缘，lightColor 决定边缘光颜色。
    **引擎画布 opaque 时 alpha 差恒 0、bevel 是空操作**（v3.6.4 实测：
    抓 #pixiCanvas 的 alpha 通道全 255）。所以 bevel 相关的效果在
    导出视频里主要靠 bloom 出观感，这里仍保留 bevel 参数是为了让
    **预览页**（透明画布）也能看出边缘光。
  · bloom —— 阈值提取 + 模糊 + 相加，是「光晕」的真正来源。
    threshold 是 0~1 域（直传，不能再除 100）。
  · godrayFilm —— 开关型，角度固定 30°、gain 0.5。只能"有/无"。
  · adjustment —— 调色，光效的"色温"全靠它（colorRed/Green/Blue 是乘子）。
"""

# 每条：name / desc / target（bg 或 fig 或 both）/ params / note
LIGHT_PRESETS = [
    {
        "name": "黄昏长影",
        "desc": "低角度暖光斜照，长影与暖调",
        "target": "both",
        "bg": {
            "brightness": 0.92,
            "contrast": 1.05,
            "saturation": 1.15,
            "gamma": 0.95,
            "colorRed": 255,
            "colorGreen": 226,
            "colorBlue": 196
        },
        "fig": {
            "brightness": 1.02,
            "contrast": 1.06,
            "saturation": 1.05,
            "colorRed": 255,
            "colorGreen": 232,
            "colorBlue": 205,
            "bevel": 1.6,
            "bevelThickness": 18,
            "bevelRotation": 80,
            "bevelRed": 255,
            "bevelGreen": 208,
            "bevelBlue": 150,
            "bloom": 0.35,
            "bloomBlur": 4,
            "bloomThreshold": 0.82
        },
        "note": "边缘光 80° 对应「光从左后方来」；调 bevelRotation 换方向",
    },
    {
        "name": "晨曦斜照",
        "desc": "清晨金色低强度光，通透",
        "target": "both",
        "bg": {
            "brightness": 1.06,
            "contrast": 1.02,
            "saturation": 1.05,
            "colorRed": 255,
            "colorGreen": 240,
            "colorBlue": 214
        },
        "fig": {
            "brightness": 1.04,
            "contrast": 1.03,
            "colorRed": 255,
            "colorGreen": 246,
            "colorBlue": 226,
            "bevel": 1.2,
            "bevelThickness": 14,
            "bevelRotation": 65,
            "bevelRed": 255,
            "bevelGreen": 240,
            "bevelBlue": 200,
            "bloom": 0.28,
            "bloomBlur": 3,
            "bloomThreshold": 0.84
        },
        "note": "亮度都 >1，是「提亮」而不是压暗；光色偏金",
    },
    {
        "name": "烛火暖光",
        "desc": "强烈的橙红暖光，适合室内夜戏",
        "target": "both",
        "bg": {
            "brightness": 0.88,
            "contrast": 1.1,
            "saturation": 1.25,
            "colorRed": 255,
            "colorGreen": 196,
            "colorBlue": 150
        },
        "fig": {
            "brightness": 1,
            "contrast": 1.08,
            "saturation": 1.15,
            "colorRed": 255,
            "colorGreen": 208,
            "colorBlue": 168,
            "bevel": 2.2,
            "bevelThickness": 16,
            "bevelRotation": 90,
            "bevelRed": 255,
            "bevelGreen": 186,
            "bevelBlue": 120,
            "bloom": 0.45,
            "bloomBlur": 5,
            "bloomThreshold": 0.8
        },
        "note": "烛火的暖靠 colorRed 拉满、Green/Blue 压低；bloom 抬亮度",
    },
    {
        "name": "月夜银蓝",
        "desc": "低饱和冷蓝，安静的月光",
        "target": "both",
        "bg": {
            "brightness": 0.88,
            "contrast": 1.08,
            "saturation": 0.72,
            "colorRed": 196,
            "colorGreen": 216,
            "colorBlue": 255
        },
        "fig": {
            "brightness": 0.96,
            "contrast": 1.06,
            "saturation": 0.82,
            "colorRed": 208,
            "colorGreen": 226,
            "colorBlue": 255,
            "bevel": 1.8,
            "bevelThickness": 15,
            "bevelRotation": 100,
            "bevelRed": 190,
            "bevelGreen": 222,
            "bevelBlue": 255,
            "bloom": 0.3,
            "bloomBlur": 4,
            "bloomThreshold": 0.82
        },
        "note": "冷光的做法：降低 saturation + Blue 拉高、Red 压低",
    },
    {
        "name": "雨夜钴蓝",
        "desc": "深冷蓝，湿冷的雨夜氛围",
        "target": "both",
        "bg": {
            "brightness": 0.78,
            "contrast": 1.15,
            "saturation": 0.65,
            "gamma": 0.9,
            "colorRed": 170,
            "colorGreen": 200,
            "colorBlue": 255
        },
        "fig": {
            "brightness": 0.9,
            "contrast": 1.12,
            "saturation": 0.78,
            "colorRed": 186,
            "colorGreen": 210,
            "colorBlue": 255,
            "bevel": 1.5,
            "bevelThickness": 14,
            "bevelRotation": 110,
            "bevelRed": 160,
            "bevelGreen": 196,
            "bevelBlue": 255,
            "bloom": 0.28,
            "bloomBlur": 4,
            "bloomThreshold": 0.83
        },
        "note": "gamma<1 压暗中间调，比单纯降 brightness 更通透",
    },
    {
        "name": "舞台双束聚光",
        "desc": "开启引擎体积光 + 强泛光，舞台感",
        "target": "both",
        "bg": {
            "brightness": 0.7,
            "contrast": 1.3,
            "saturation": 1.1,
            "colorRed": 255,
            "colorGreen": 236,
            "colorBlue": 210,
            "godrayFilm": 1
        },
        "fig": {
            "brightness": 0.95,
            "contrast": 1.18,
            "saturation": 1.05,
            "colorRed": 255,
            "colorGreen": 244,
            "colorBlue": 226,
            "bevel": 2,
            "bevelThickness": 20,
            "bevelRotation": 45,
            "bevelRed": 255,
            "bevelGreen": 250,
            "bevelBlue": 235,
            "bloom": 0.5,
            "bloomBlur": 5,
            "bloomThreshold": 0.78
        },
        "note": "**godrayFilm 只能开关**（引擎把它映射成 isBoolean，角度/强度不可调）。按背景压暗 + 强 bloom 制造追光感",
    },
    {
        "name": "舞台侧逆光",
        "desc": "侧后方轮廓光，人物边缘发亮",
        "target": "both",
        "bg": {
            "brightness": 0.68,
            "contrast": 1.32,
            "saturation": 0.95,
            "colorRed": 240,
            "colorGreen": 240,
            "colorBlue": 255
        },
        "fig": {
            "brightness": 0.98,
            "contrast": 1.12,
            "saturation": 0.95,
            "bevel": 3,
            "bevelThickness": 24,
            "bevelRotation": 135,
            "bevelRed": 255,
            "bevelGreen": 250,
            "bevelBlue": 240,
            "bloom": 0.5,
            "bloomBlur": 5,
            "bloomThreshold": 0.76
        },
        "note": "bevel 强度 3.0 是这套里最高的——轮廓光靠它；135° = 右后方来光",
    },
    {
        "name": "探照窄束",
        "desc": "高对比硬光，戏剧化",
        "target": "both",
        "bg": {
            "brightness": 0.58,
            "contrast": 1.48,
            "saturation": 0.95,
            "colorRed": 255,
            "colorGreen": 248,
            "colorBlue": 235,
            "godrayFilm": 1
        },
        "fig": {
            "brightness": 1,
            "contrast": 1.35,
            "saturation": 0.98,
            "bevel": 2.4,
            "bevelThickness": 22,
            "bevelRotation": 60,
            "bevelRed": 255,
            "bevelGreen": 252,
            "bevelBlue": 244,
            "bloom": 0.45,
            "bloomBlur": 4,
            "bloomThreshold": 0.8
        },
        "note": "背景压到 0.6、对比拉到 1.45，是这套里反差最大的",
    },
    {
        "name": "柔光箱日常",
        "desc": "均匀柔和的室内光，几乎无阴影",
        "target": "both",
        "bg": {
            "brightness": 1.1,
            "contrast": 0.96,
            "saturation": 1.04,
            "colorRed": 255,
            "colorGreen": 250,
            "colorBlue": 240
        },
        "fig": {
            "brightness": 1.05,
            "contrast": 0.98,
            "saturation": 1.02,
            "bevel": 0.6,
            "bevelThickness": 10,
            "bevelRotation": 45,
            "bevelRed": 255,
            "bevelGreen": 252,
            "bevelBlue": 246,
            "bloom": 0.2,
            "bloomBlur": 4,
            "bloomThreshold": 0.86
        },
        "note": "柔光 = 低对比(<1) + 大半径弱 bloom；bevel 只留一点点",
    },
    {
        "name": "窗边百叶光",
        "desc": "窗格状的明暗节奏，午后室内",
        "target": "both",
        "bg": {
            "brightness": 1.02,
            "contrast": 1.12,
            "saturation": 1.06,
            "colorRed": 255,
            "colorGreen": 244,
            "colorBlue": 224
        },
        "fig": {
            "brightness": 1,
            "contrast": 1.08,
            "colorRed": 255,
            "colorGreen": 248,
            "colorBlue": 234,
            "bevel": 1.4,
            "bevelThickness": 16,
            "bevelRotation": 75,
            "bevelRed": 255,
            "bevelGreen": 236,
            "bevelBlue": 200,
            "bloom": 0.28,
            "bloomBlur": 4,
            "bloomThreshold": 0.83
        },
        "note": "百叶的「条纹」官方引擎做不了（没有对应滤镜）；这里只还原它的明暗节奏与暖调",
    },
    {
        "name": "霓虹紫光",
        "desc": "赛博感的紫粉光源",
        "target": "both",
        "bg": {
            "brightness": 0.8,
            "contrast": 1.18,
            "saturation": 1.35,
            "colorRed": 255,
            "colorGreen": 180,
            "colorBlue": 245
        },
        "fig": {
            "brightness": 0.96,
            "contrast": 1.12,
            "saturation": 1.25,
            "colorRed": 250,
            "colorGreen": 190,
            "colorBlue": 250,
            "bevel": 2,
            "bevelThickness": 18,
            "bevelRotation": 90,
            "bevelRed": 255,
            "bevelGreen": 160,
            "bevelBlue": 255,
            "bloom": 0.5,
            "bloomBlur": 5,
            "bloomThreshold": 0.79
        },
        "note": "R/B 拉高、G 压低 = 品红；饱和度拉到 1.35 强化霓虹感",
    },
    {
        "name": "灵体逆光",
        "desc": "人物半透明 + 强轮廓光",
        "target": "fig",
        "fig": {
            "brightness": 1.15,
            "contrast": 0.92,
            "saturation": 0.85,
            "alpha": 0.62,
            "bevel": 3.5,
            "bevelThickness": 26,
            "bevelRotation": 270,
            "bevelRed": 220,
            "bevelGreen": 240,
            "bevelBlue": 255,
            "bloom": 0.55,
            "bloomBlur": 5,
            "bloomThreshold": 0.78
        },
        "note": "alpha 走容器级（只乘 .a），**不会**把 RGB 一起压暗；bevel 270° = 从正下方来光，做出「脚下透光」",
    },
    {
        "name": "恢复默认光",
        "desc": "清掉所有光效参数（回到原片）",
        "target": "both",
        "bg": {
            "brightness": 1,
            "contrast": 1,
            "saturation": 1,
            "gamma": 1,
            "colorRed": 255,
            "colorGreen": 255,
            "colorBlue": 255,
            "bevel": 0,
            "bevelThickness": 0,
            "bloom": 0,
            "bloomBrightness": 1,
            "bloomBlur": 0,
            "bloomThreshold": 0,
            "godrayFilm": 0,
            "blur": 0
        },
        "fig": {
            "brightness": 1,
            "contrast": 1,
            "saturation": 1,
            "gamma": 1,
            "colorRed": 255,
            "colorGreen": 255,
            "colorBlue": 255,
            "bevel": 0,
            "bevelThickness": 0,
            "bloom": 0,
            "bloomBrightness": 1,
            "bloomBlur": 0,
            "bloomThreshold": 0,
            "godrayFilm": 0,
            "alpha": 1
        },
        "note": "与引擎 isDefault 同口径的「全零」组合，导出后引擎会把滤镜整个移除",
    },
]

LIGHT_NAMES = tuple(p["name"] for p in LIGHT_PRESETS)


def by_name(name):
    for p in LIGHT_PRESETS:
        if p["name"] == name: return p
    return None


def params_for(name, layer="fig"):
    """取某条预设在某层的参数（layer = 'bg' / 'fig'）。无则 None。"""
    p = by_name(name)
    if not p: return None
    v = p.get(layer)
    return dict(v) if isinstance(v, dict) else None


def to_transform_cmd(name, target="fig-center", duration=0):
    """把预设翻成一条 setTransform 指令（target 决定作用层）。"""
    import json as _json
    layer = "bg" if str(target).startswith("bg") else "fig"
    params = params_for(name, layer)
    if not params: return None
    # 只发该层真正需要的键；alpha 只对 fig 有意义
    if layer == "bg": params.pop("alpha", None)
    body = _json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    return "setTransform:%s -target=%s -duration=%d;" % (
        body, target, int(duration))


# 能力边界（UI 上要如实展示，别让用户以为能调光）
LIMITS = {
    "supported": [
        "边缘光（bevel：颜色/粗细/方向/强度）",
        "泛光光晕（bloom：强度/亮度/模糊/阈值）",
        "体积光开关（godrayFilm：只能开或关，角度与强度固定 30°/0.5）",
        "色温与明暗（adjustment：亮度/对比/饱和/gamma/RGB 乘子）",
        "整体透明度（alpha，只影响人物层）",
    ],
    "unsupported": [
        "多光源独立布光（点光源/柔光箱/聚光分别调）——官方引擎无此能力",
        "光源位置 X/Y 与照射范围",
        "人物地面投影（影子浓度/柔度/距离）——官方引擎无此能力",
        "体积光束的强弱与角度",
    ],
    "reason": "教学视频里的布光编辑器属于 webgal-terre 二改引擎的扩展；"
              "我们打包的是官方 WebGAL 4.6.6，滤镜注册表只有 12 个，"
              "不含布光与投影。硬做会是假功能（编辑器可调、导出无效果）。",
}