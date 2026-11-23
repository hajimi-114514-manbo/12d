# -*- coding: utf-8 -*-
"""电影感滤镜预设库（纯数据，无 Qt / 无 IO 副作用）。

来源：《webgal电影感滤镜1.21（新增多种低模糊滤镜）.docx》
（B站北风的猫 5306 教学整合版 3.1.1 附带，原作者另有其人与 KanameVZ /
草莓大福no 等友情提供，见每个条目的 credit）。

每个滤镜是**一对** setTransform 参数：
    bg   —— 背景层（-target=bg-main）
    fig  —— 立绘层（-target=fig-<槽位>）
这样「背景压暗 + 人物提亮」这类电影感光效才成立（只调一层会脱节）。

用法：
    from film_filters import FILM_FILTERS, by_name
    for f in FILM_FILTERS:      # f["name"] / f["desc"] / f["bg"] / f["fig"]
        ...
    p = by_name("火光/爆炸↑")
"""
# 来源标注（UI 说明里带上，尊重原作者）
CREDIT_MAIN = "北风的猫5306"
CREDIT_KANAME = "KanameVZ"
CREDIT_STRAWBERRY = "草莓大福no"

# 每个条目：
#   name  显示名（去掉 docx 里的分号/箭头等噪声）
#   desc  一句话说明（用于卡片副标题）
#   bg    背景层参数（None = 该滤镜只调人物）
#   fig   人物层参数（None = 该滤镜只调背景）
#   credit 作者
#   note  额外操作提示（如「特写用高斯模糊20」）
FILM_FILTERS = [
    {
        "name": "火光/爆炸",
        "desc": "暖橙强光，背景压暗突出人物轮廓光",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.9, "contrast": 0.8, "saturation": 0.5,
               "gamma": 0.6, "colorRed": 255, "colorGreen": 218,
               "colorBlue": 184, "bloom": 1, "bloomBrightness": 0.8,
               "bloomBlur": 8, "bevelRed": 255, "bevelGreen": 194,
               "bevelBlue": 125},
        "fig": {"brightness": 0.75, "contrast": 1.1, "saturation": 0.8,
                "gamma": 1, "colorRed": 255, "colorGreen": 234,
                "colorBlue": 217, "bloom": 0.6, "bloomBrightness": 0.7,
                "bloomBlur": 4, "bevel": 2, "bevelThickness": 20,
                "bevelRotation": 80, "bevelRed": 255, "bevelGreen": 201,
                "bevelBlue": 125},
    },
    {
        "name": "黑暗（有灯光）",
        "desc": "夜戏有光源，人物保留细节",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.7, "contrast": 0.8, "saturation": 0.7,
               "gamma": 0.5, "colorRed": 255, "colorGreen": 255,
               "colorBlue": 255, "bloom": 1, "bloomBrightness": 0.7,
               "bloomBlur": 10},
        "fig": {"brightness": 0.6, "contrast": 1.1, "saturation": 0.9,
                "gamma": 0.9, "colorRed": 255, "colorGreen": 255,
                "colorBlue": 255, "bloom": 0.2, "bloomBrightness": 0.9,
                "bloomBlur": 10, "bevel": 1, "bevelThickness": 18,
                "bevelRotation": 30, "bevelRed": 204, "bevelGreen": 204,
                "bevelBlue": 204},
    },
    {
        "name": "清晨/黄昏（轻）",
        "desc": "淡暖色日光，日常对话首选",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.7, "contrast": 0.8, "saturation": 0.7,
               "gamma": 0.5, "colorRed": 255, "colorGreen": 234,
               "colorBlue": 214, "bloom": 1, "bloomBrightness": 0.7,
               "bloomBlur": 10},
        "fig": {"brightness": 0.6, "contrast": 1.1, "saturation": 0.9,
                "gamma": 0.9, "colorRed": 255, "colorGreen": 234,
                "colorBlue": 217, "bloom": 0.4, "bloomBrightness": 0.9,
                "bloomBlur": 10, "bevel": 1, "bevelThickness": 10,
                "bevelRotation": 30, "bevelRed": 255, "bevelGreen": 238,
                "bevelBlue": 224},
    },
    {
        "name": "黑暗（无灯光）",
        "desc": "纯暗场，人物只剩轮廓",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.7, "contrast": 0.8, "saturation": 0.7,
               "gamma": 0.5, "colorRed": 222, "colorGreen": 222,
               "colorBlue": 222, "bloom": 1, "bloomBrightness": 0.7,
               "bloomBlur": 10},
        "fig": {"brightness": 0.4, "contrast": 1.1, "saturation": 0.9,
                "gamma": 0.7, "colorRed": 255, "colorGreen": 255,
                "colorBlue": 255, "bloom": 0.2, "bloomBrightness": 0.9,
                "bloomBlur": 10, "bevel": 0.2, "bevelThickness": 18,
                "bevelRotation": 30},
    },
    {
        "name": "白天",
        "desc": "标准日景，亮度适中不发灰",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.7, "contrast": 1.1, "saturation": 0.8,
               "gamma": 0.5, "colorRed": 255, "colorGreen": 255,
               "colorBlue": 255, "bloom": 0.5, "bloomBrightness": 0.8,
               "bloomBlur": 10},
        "fig": {"brightness": 0.6, "contrast": 1.1, "saturation": 0.8,
                "gamma": 0.6, "colorRed": 255, "colorGreen": 255,
                "colorBlue": 255, "bloom": 0.7, "bloomBrightness": 0.6,
                "bloomBlur": 10, "bevel": 1, "bevelThickness": 18,
                "bevelRotation": 30},
    },
    {
        "name": "黄昏（重）·室外",
        "desc": "强烈夕照，暖橙压全场",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.9, "contrast": 0.8, "saturation": 0.7,
               "gamma": 0.5, "colorRed": 255, "colorGreen": 205,
               "colorBlue": 171, "bloom": 0.7, "bloomBrightness": 0.8,
               "bloomBlur": 10},
        "fig": {"brightness": 0.4, "contrast": 1.2, "saturation": 0.7,
                "gamma": 0.8, "colorRed": 255, "colorGreen": 213,
                "colorBlue": 176, "bloom": 0.5, "bloomBlur": 10,
                "bevel": 1, "bevelThickness": 18, "bevelRotation": 30},
    },
    {
        "name": "黄昏（重）·室内",
        "desc": "室内夕照，比室外版略亮",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.9, "contrast": 0.8, "saturation": 0.7,
               "gamma": 0.5, "colorRed": 255, "colorGreen": 203,
               "colorBlue": 171, "bloom": 0.7, "bloomBrightness": 0.9,
               "bloomBlur": 10},
        "fig": {"brightness": 0.5, "contrast": 1.2, "saturation": 0.7,
                "gamma": 0.8, "colorRed": 255, "colorGreen": 208,
                "colorBlue": 176, "bloom": 0.5, "bloomBlur": 10,
                "bevel": 1, "bevelThickness": 18, "bevelRotation": 30},
    },
    {
        "name": "面光·黄昏",
        "desc": "暖光打脸，面部更立体",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.7, "contrast": 0.8, "saturation": 0.7,
               "gamma": 0.5, "colorRed": 255, "colorGreen": 203,
               "colorBlue": 171, "bloom": 0.6, "bloomBrightness": 0.8,
               "bloomBlur": 10},
        "fig": {"brightness": 0.5, "contrast": 1, "saturation": 0.7,
                "gamma": 1, "colorRed": 255, "colorGreen": 208,
                "colorBlue": 176, "bloom": 0.4, "bloomBrightness": 1.8,
                "bloomBlur": 10, "bevel": 0.3, "bevelThickness": 18},
    },
    {
        "name": "面光·白天",
        "desc": "冷白面光，清爽纪实感",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.8, "contrast": 0.8, "saturation": 0.7,
               "gamma": 0.5, "colorRed": 227, "colorGreen": 227,
               "colorBlue": 227, "bloom": 0.7, "bloomBrightness": 0.8,
               "bloomBlur": 10},
        "fig": {"brightness": 0.75, "contrast": 1, "saturation": 0.8,
                "gamma": 0.7, "colorRed": 255, "colorGreen": 255,
                "colorBlue": 255, "bloom": 0.5, "bloomBrightness": 1,
                "bloomBlur": 10, "bevel": 0.5, "bevelThickness": 18},
    },
    {
        "name": "面光·清晨黄昏",
        "desc": "柔暖面光，清晨/黄昏通用",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.7, "contrast": 0.8, "saturation": 0.7,
               "gamma": 0.5, "colorRed": 255, "colorGreen": 234,
               "colorBlue": 214, "bloom": 1, "bloomBrightness": 0.7,
               "bloomBlur": 10},
        "fig": {"brightness": 0.8, "contrast": 1.1, "saturation": 0.9,
                "gamma": 0.8, "colorRed": 242, "colorGreen": 216,
                "colorBlue": 194, "bloom": 0.5, "bloomBrightness": 0.8,
                "bloomBlur": 10, "bevel": 1, "bevelThickness": 10,
                "bevelRotation": 30},
    },
    {
        "name": "淡色黄昏",
        "desc": "高饱和暖黄，KanameVZ 提供",
        "credit": CREDIT_KANAME,
        "bg": None,
        "fig": {"brightness": 1, "contrast": 1, "saturation": 1.5,
                "colorRed": 255, "colorGreen": 251, "colorBlue": 217,
                "bloomThreshold": 10, "bevel": 1, "bevelThickness": 15,
                "bevelSoftness": 1},
        "note": "背景按景别另加高斯模糊：特写 blur=20，大场景 blur=5",
    },
    {
        "name": "黑夜微光",
        "desc": "极暗环境留一点光，草莓大福no 提供",
        "credit": CREDIT_STRAWBERRY,
        "bg": None,
        "fig": {"brightness": 0.55, "contrast": 1.05, "saturation": 0.85,
                "gamma": 0.75, "colorRed": 210, "colorGreen": 215,
                "colorBlue": 235, "bloom": 0.15, "bloomBrightness": 0.8,
                "bloomBlur": 8},
    },
    {
        "name": "室内暖光",
        "desc": "室内暖黄灯，适合日常/居家戏",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.85, "contrast": 0.95, "saturation": 0.9,
               "gamma": 0.7, "colorRed": 255, "colorGreen": 226,
               "colorBlue": 190, "bloom": 0.6, "bloomBrightness": 0.8,
               "bloomBlur": 10, "bevelRed": 255, "bevelGreen": 210,
               "bevelBlue": 170},
        "fig": {"brightness": 0.8, "contrast": 1.05, "saturation": 0.95,
                "gamma": 0.85, "colorRed": 255, "colorGreen": 235,
                "colorBlue": 210, "bloom": 0.4, "bloomBrightness": 0.9,
                "bloomBlur": 8, "bevel": 1, "bevelThickness": 12,
                "bevelRotation": 30},
    },
    {
        "name": "清爽室内白天",
        "desc": "通透室内，虚影不参与调色",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.8, "contrast": 1.05, "saturation": 0.85,
               "gamma": 0.6, "colorRed": 240, "colorGreen": 245,
               "colorBlue": 255, "bloom": 0.5, "bloomBrightness": 0.8,
               "bloomBlur": 10},
        "fig": {"brightness": 0.75, "contrast": 1.05, "saturation": 0.85,
                "gamma": 0.7, "colorRed": 245, "colorGreen": 248,
                "colorBlue": 255, "bloom": 0.45, "bloomBrightness": 0.85,
                "bloomBlur": 10, "bevel": 1, "bevelThickness": 12,
                "bevelRotation": 30},
    },
    {
        "name": "夜晚室内白光",
        "desc": "夜戏室内白炽灯，冷白偏青",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.65, "contrast": 0.95, "saturation": 0.75,
               "gamma": 0.55, "colorRed": 225, "colorGreen": 235,
               "colorBlue": 255, "bloom": 0.7, "bloomBrightness": 0.75,
               "bloomBlur": 10},
        "fig": {"brightness": 0.6, "contrast": 1.05, "saturation": 0.85,
                "gamma": 0.8, "colorRed": 235, "colorGreen": 242,
                "colorBlue": 255, "bloom": 0.3, "bloomBrightness": 0.9,
                "bloomBlur": 10, "bevel": 1, "bevelThickness": 15,
                "bevelRotation": 30},
    },
    {
        "name": "白天室外晴天",
        "desc": "晴朗户外，高亮高饱和",
        "credit": CREDIT_MAIN,
        "bg": {"brightness": 0.9, "contrast": 1.1, "saturation": 1.0,
               "gamma": 0.6, "colorRed": 255, "colorGreen": 252,
               "colorBlue": 240, "bloom": 0.6, "bloomBrightness": 0.9,
               "bloomBlur": 10},
        "fig": {"brightness": 0.85, "contrast": 1.1, "saturation": 1.0,
                "gamma": 0.75, "colorRed": 255, "colorGreen": 250,
                "colorBlue": 240, "bloom": 0.5, "bloomBrightness": 0.9,
                "bloomBlur": 10, "bevel": 1, "bevelThickness": 15,
                "bevelRotation": 30},
    },
]


def by_name(name):
    """按显示名查预设；找不到返回 None。"""
    for f in FILM_FILTERS:
        if f["name"] == name: return f
    return None


def apply_to_clip(clip, preset):
    """把预设写进 clip：背景层参数给背景 clip、人物层参数给立绘 clip。

    返回实际写入的参数 dict（可能为空）。调用方负责判断该 clip 是不是
    背景/立绘（本模块不碰工程结构）。"""
    if not isinstance(clip, dict) or preset is None: return {}
    tr_name = str(preset.get("_kind") or "")
    filt = None
    if tr_name == "bg": filt = preset.get("bg")
    elif tr_name == "fig": filt = preset.get("fig")
    if not filt: return {}
    clip["filter"] = dict(filt)
    return clip["filter"]
