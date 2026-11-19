# -*- coding: utf-8 -*-
"""高级演出特效库（纯数据，无 Qt / 无 IO 副作用）。

来源：《WebGAL 高级演出特效代码整合（去重最终版）——北风的猫5306制作》
（B站北风的猫 5306 教学整合版 3.1.1 附带）。共 206 条，按 6 类组织。

每条：
    cat   分类显示名（滤镜特效 / 镜头动画 / 恋爱场景 / 复合特效 / 重置指令 / 情绪与状态）
    kind  "transform"（setTransform 滤镜）/ "animation"（setTempAnimation 镜头）
          / "combo"（两者都有）
    name  特效名
    desc  一句话说明（取自 docx 注释的「效果：」行）
    code  完整 WebGAL 指令（已去行尾分号，写入脚本时补 ;）

用法：
    from stage_effects import STAGE_EFFECTS, CATEGORIES, by_name
    for e in STAGE_EFFECTS:      # e["name"] / e["code"] ...
        ...
"""

# 分类展示顺序（模板页分组标题按此排列）
CATEGORIES = [
    ('滤镜特效', 50),
    ('镜头动画', 13),
    ('恋爱场景', 32),
    ('复合特效', 29),
    ('情绪与状态', 76),
    ('重置指令', 6),
]

CREDIT = "北风的猫5306"

STAGE_EFFECTS = [
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '赛博朋克/电子故障风格',
        "desc": '开启红绿蓝色彩分离、故障错位、老电影噪点，同时调整色调偏冷',
        "code": [
            'setTransform:{"glitchFilm": true, "rgbFilm": true, "oldFilm": true, "colorRed": 200, "colorGreen": 255, "colorBlue": 255, "brightness": 1.2} -target=bg-main -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '梦境/圣光/回忆杀',
        "desc": '开启上帝之光（Godray），配合较强的泛光（Bloom）和轻微模糊，营造朦胧感',
        "code": [
            'setTransform:{"godrayFilm": true, "lightX": 0.5, "lightY": -0.5, "lightAngle": 30, "lightIntensity": 1.0, "bloom": 1.5, "bloomBlur": 2, "blur": 1} -target=bg-main -duration=2000',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '受伤/濒死/强烈冲击',
        "desc": '屏幕剧烈震动（Shake），产生冲击波纹（Shockwave），画面变红并产生暗角（Vignetting）',
        "code": [
            'setTransform:{"shakePower": 20, "shakeSpeed": 5, "colorRed": 255, "colorGreen": 150, "colorBlue": 150, "vignetting": true, "vignettingAlpha": 0.5, "shockwaveFilter": 0.5} -target=bg-main -duration=300',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '复古黑白电视/监控画面',
        "desc": '开启老电影效果（噪点+划痕），去除饱和度（变黑白），增加对比度，增加暗角',
        "code": [
            'setTransform:{"oldFilm": true, "vignetting": true, "vignettingBlur": 0.5, "vignettingAlpha": 0.8, "noise": 0.5, "saturation": 0, "contrast": 1.5} -target=bg-main -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '水下/热浪扭曲效果',
        "desc": '开启反射滤镜（模拟水波），色调调整为青蓝色，轻微模糊',
        "code": [
            'setTransform:{"reflectionFilm": true, "colorRed": 200, "colorGreen": 220, "colorBlue": 255, "blur": 1} -target=bg-main -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '角色高光/觉醒状态（立绘）',
        "desc": '对立绘使用强烈的泛光，使其边缘发光并变亮',
        "code": [
            'setTransform:{"bloom": 1.5, "bloomBlur": 4, "bloomThreshold": 0.1, "brightness": 1.2} -target=fig-center -duration=500',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '复古胶片（背景）',
        "desc": '复古胶片风格背景滤镜',
        "code": [
            'setTransform:{"brightness":0.8,"contrast":1.2,"saturation":0.6,"gamma":0.9,"colorRed":255,"colorGreen":230,"colorBlue":200,"bloom":0.3,"bloomBlur":6,"sepia":0.7} -target=bg-main -duration=0 -next',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '复古胶片（人物）',
        "desc": '复古胶片风格人物滤镜',
        "code": [
            'setTransform:{"brightness":0.7,"contrast":1.3,"saturation":0.7,"gamma":1.0,"colorRed":255,"colorGreen":235,"colorBlue":210,"bloom":0.2,"bloomBlur":4,"bevel":1,"bevelThickness":15,"bevelRotation":45,"bevelRed":250,"bevelGreen":220,"bevelBlue":180} -target=fig-center -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '赛博朋克（背景）',
        "desc": '赛博朋克风格背景',
        "code": [
            'setTransform:{"brightness":0.9,"contrast":1.5,"saturation":1.8,"gamma":0.7,"colorRed":180,"colorGreen":200,"colorBlue":255,"bloom":1.2,"bloomBrightness":0.9,"bloomBlur":12,"glitchFilm":true,"red":[3,0],"blue":[-3,0]} -target=bg-main -duration=0 -next',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '赛博朋克（人物）',
        "desc": '赛博朋克风格人物滤镜',
        "code": [
            'setTransform:{"brightness":0.8,"contrast":1.4,"saturation":1.6,"gamma":0.8,"colorRed":200,"colorGreen":220,"colorBlue":255,"bloom":0.8,"bloomBlur":8,"bevel":2,"bevelThickness":20,"bevelRotation":60,"bevelRed":150,"bevelGreen":180,"bevelBlue":255} -target=fig-center -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '梦幻柔光（背景）',
        "desc": '梦幻柔光风格背景',
        "code": [
            'setTransform:{"brightness":1.1,"contrast":0.7,"saturation":1.3,"gamma":0.8,"colorRed":250,"colorGreen":240,"colorBlue":255,"bloom":1.5,"bloomBrightness":0.6,"bloomBlur":15} -target=bg-main -duration=0 -next',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '梦幻柔光（人物）',
        "desc": '梦幻柔光风格人物滤镜',
        "code": [
            'setTransform:{"brightness":1.0,"contrast":0.8,"saturation":1.2,"gamma":0.9,"colorRed":252,"colorGreen":245,"colorBlue":255,"bloom":1.0,"bloomBlur":10,"bevel":0.8,"bevelThickness":12,"bevelRotation":30,"bevelRed":255,"bevelGreen":250,"bevelBlue":255} -target=fig-center -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '冷色调极简（背景）',
        "desc": '冷色调极简风格背景',
        "code": [
            'setTransform:{"brightness":0.75,"contrast":1.2,"saturation":0.4,"gamma":0.8,"colorRed":180,"colorGreen":200,"colorBlue":255,"bloom":0.2,"bloomBlur":5} -target=bg-main -duration=0 -next',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '冷色调极简（人物）',
        "desc": '冷色调极简风格人物滤镜',
        "code": [
            'setTransform:{"brightness":0.7,"contrast":1.3,"saturation":0.5,"gamma":0.9,"colorRed":190,"colorGreen":210,"colorBlue":255,"bevel":1,"bevelThickness":10,"bevelRotation":40,"bevelRed":170,"bevelGreen":190,"bevelBlue":250} -target=fig-center -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '暖色调治愈（背景）',
        "desc": '暖色调治愈风格背景',
        "code": [
            'setTransform:{"brightness":0.95,"contrast":0.9,"saturation":1.4,"gamma":0.9,"colorRed":255,"colorGreen":220,"colorBlue":180,"bloom":0.7,"bloomBlur":8} -target=bg-main -duration=0 -next',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '暖色调治愈（人物）',
        "desc": '暖色调治愈风格人物滤镜',
        "code": [
            'setTransform:{"brightness":0.9,"contrast":1.0,"saturation":1.3,"gamma":1.0,"colorRed":255,"colorGreen":225,"colorBlue":190,"bloom":0.5,"bloomBlur":6,"bevel":1.2,"bevelThickness":14,"bevelRotation":50,"bevelRed":255,"bevelGreen":210,"bevelBlue":170} -target=fig-center -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '水墨风（背景）',
        "desc": '水墨风格背景',
        "code": [
            'setTransform:{"brightness":0.8,"contrast":1.6,"saturation":0.2,"gamma":0.7,"colorRed":200,"colorGreen":200,"colorBlue":200,"bloom":0.1,"bloomBlur":3} -target=bg-main -duration=0 -next',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '水墨风（人物）',
        "desc": '水墨风格人物滤镜',
        "code": [
            'setTransform:{"brightness":0.75,"contrast":1.7,"saturation":0.3,"gamma":0.8,"colorRed":210,"colorGreen":210,"colorBlue":210,"bevel":0.5,"bevelThickness":8,"bevelRotation":30,"bevelRed":180,"bevelGreen":180,"bevelBlue":180} -target=fig-center -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '油画质感（背景）',
        "desc": '油画质感风格背景',
        "code": [
            'setTransform:{"brightness":0.9,"contrast":0.8,"saturation":1.5,"gamma":0.9,"colorRed":245,"colorGreen":235,"colorBlue":225,"bloom":0.4,"bloomBlur":10,"pixelSize":3} -target=bg-main -duration=0 -next',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '油画质感（人物）',
        "desc": '油画质感风格人物滤镜',
        "code": [
            'setTransform:{"brightness":0.85,"contrast":0.9,"saturation":1.4,"gamma":1.0,"colorRed":248,"colorGreen":238,"colorBlue":228,"bloom":0.3,"bloomBlur":8,"pixelSize":2,"bevel":1,"bevelThickness":12,"bevelRotation":45,"bevelRed":240,"bevelGreen":230,"bevelBlue":220} -target=fig-center -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '电影感暗角（背景）',
        "desc": '电影感暗角风格背景',
        "code": [
            'setTransform:{"brightness":0.8,"contrast":1.3,"saturation":1.1,"gamma":0.8,"colorRed":250,"colorGreen":245,"colorBlue":240,"bloom":0.5,"bloomBlur":7,"vignetting":true,"vignettingAlpha":0.6,"vignettingBlur":0.8} -target=bg-main -duration=0 -next',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '电影感暗角（人物）',
        "desc": '电影感暗角风格人物滤镜',
        "code": [
            'setTransform:{"brightness":0.9,"contrast":1.2,"saturation":1.0,"gamma":0.9,"colorRed":252,"colorGreen":247,"colorBlue":242,"bloom":0.4,"bloomBlur":5,"bevel":1.1,"bevelThickness":15,"bevelRotation":35,"bevelRed":245,"bevelGreen":240,"bevelBlue":235} -target=fig-center -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '高光溢出（背景）',
        "desc": '高光溢出风格背景',
        "code": [
            'setTransform:{"brightness":1.3,"contrast":0.8,"saturation":1.2,"gamma":0.7,"colorRed":255,"colorGreen":250,"colorBlue":240,"bloom":1.8,"bloomBrightness":0.8,"bloomBlur":12} -target=bg-main -duration=0 -next',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '高光溢出（人物）',
        "desc": '高光溢出风格人物滤镜',
        "code": [
            'setTransform:{"brightness":1.2,"contrast":0.9,"saturation":1.1,"gamma":0.8,"colorRed":255,"colorGreen":252,"colorBlue":245,"bloom":1.2,"bloomBlur":9,"bevel":0.9,"bevelThickness":10,"bevelRotation":55,"bevelRed":255,"bevelGreen":248,"bevelBlue":238} -target=fig-center -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '清新日系（背景）',
        "desc": '清新日系风格背景',
        "code": [
            'setTransform:{"brightness":1.05,"contrast":0.85,"saturation":1.2,"gamma":0.95,"colorRed":245,"colorGreen":250,"colorBlue":255,"bloom":0.6,"bloomBlur":9} -target=bg-main -duration=0 -next',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '清新日系（人物）',
        "desc": '清新日系风格人物滤镜',
        "code": [
            'setTransform:{"brightness":1.0,"contrast":0.9,"saturation":1.1,"gamma":1.0,"colorRed":248,"colorGreen":252,"colorBlue":255,"bloom":0.5,"bloomBlur":7,"bevel":0.7,"bevelThickness":10,"bevelRotation":30,"bevelRed":240,"bevelGreen":245,"bevelBlue":250} -target=fig-center -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '圣光降临',
        "desc": '开启WebGAL内置的体积光特效',
        "code": [
            'setTransform:{"godrayFilm":true, "bloom":1} -target=bg-main -duration=1000',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '天使光环',
        "desc": '利用Bloom制造强烈的发光效果',
        "code": [
            'setTransform:{"bloom":2.0, "bloomBlur":6, "bloomThreshold":0.2, "brightness":1.2} -target=bg-main -duration=2000',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '梦境边缘·色差',
        "desc": '轻微的RGB色彩分离',
        "code": [
            'setTransform:{"rgbFilm":true, "blur":1} -target=bg-main -duration=1000',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '水下/波光粼粼',
        "desc": '适用于海边约会或内心深处',
        "code": [
            'setTransform:{"reflectionFilm":true} -target=bg-main -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '岁月静好·老照片',
        "desc": '泛黄的老电影效果',
        "code": [
            'setTransform:{"oldFilm":true, "sepia":0.5, "brightness":0.9} -target=bg-main -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '羞涩脸红·暖色滤镜',
        "desc": '画面缓慢变得温暖、泛红',
        "code": [
            'setTransform:{"colorRed":255, "colorGreen":200, "colorBlue":200, "saturation":1.3, "brightness":1.1} -target=bg-main -duration=1500',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '暧昧气氛·朦胧美',
        "desc": '永久开启轻微的模糊和泛光',
        "code": [
            'setTransform:{"blur":2, "bloom":1, "bloomBrightness":1.1, "bloomThreshold":0.1} -target=bg-main -duration=1000',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '视线模糊·含泪',
        "desc": '模拟感动或悲伤时眼眶含泪的模糊感',
        "code": [
            'setTransform:{"blur":4, "bloom":1.5, "brightness":1.1} -target=bg-main -duration=1500',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '恋爱脑·色彩增强',
        "desc": '大幅提升饱和度',
        "code": [
            'setTransform:{"saturation":1.5, "contrast":1.1, "brightness":1.05} -target=bg-main -duration=1000',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '病娇·精神污染',
        "desc": '病娇、黑化场景',
        "code": [
            'setTransform:{"glitchFilm":true, "rgbFilm":true, "colorRed":255, "colorGreen":200, "colorBlue":200} -target=bg-main -duration=200',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '黑化·深渊模式',
        "desc": '周围极黑，只有中心微亮',
        "code": [
            'setTransform:{"radiusAlphaFilter":0.4} -target=bg-main -duration=1500',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '耳鸣·视觉重影',
        "desc": '利用RGB分离模拟眩晕和耳鸣感',
        "code": [
            'setTransform:{"rgbFilm":true, "blur":3} -target=bg-main -duration=100',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '立绘·接吻前奏（凑近）',
        "desc": '立绘放大并下移',
        "code": [
            'setTransform:{"scale":{"x":1.4,"y":1.4},"position":{"y":200}} -target=fig-center -duration=1000',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '立绘·灵体化/幽灵',
        "desc": '幽灵、灵体化',
        "code": [
            'setTransform:{"alpha":0.6, "bloom":1, "brightness":1.2} -target=fig-center -duration=1000',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '立绘·暗淡退场',
        "desc": '变暗并透明',
        "code": [
            'setTransform:{"brightness":0.5, "alpha":0.5} -target=fig-center -duration=500',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '立绘·高光时刻（背景变暗）',
        "desc": '背景变暗，配合立绘发光使用',
        "code": [
            'setTransform:{"brightness":0.5} -target=bg-main -duration=500',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '立绘·高光时刻（立绘发光）',
        "desc": '立绘保持亮色发光',
        "code": [
            'setTransform:{"bloom":0.5, "brightness":1.1} -target=fig-center -duration=500',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '雨天窗户',
        "desc": '模糊+反射滤镜',
        "code": [
            'setTransform:{"blur":2, "reflectionFilm":true} -target=bg-main -duration=0',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '深夜模式',
        "desc": '整体变蓝变暗',
        "code": [
            'setTransform:{"colorRed":80, "colorGreen":80, "colorBlue":200, "brightness":0.5, "contrast":1.2} -target=bg-main -duration=2000',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '逢魔之时（黄昏）',
        "desc": '强烈的橙紫渐变',
        "code": [
            'setTransform:{"colorRed":255, "colorGreen":150, "colorBlue":100, "contrast":1.3} -target=bg-main -duration=2000',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '极速冲刺',
        "desc": '利用横向模糊制造速度线感',
        "code": [
            'setTransform:{"blur":10, "position":{"x":-50}} -target=bg-main -duration=200',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '电影宽银幕',
        "desc": '使用scale.y模拟电影遮幅效果',
        "code": [
            'setTransform:{"scale":{"y":0.85}} -target=bg-main -duration=1000',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '月下唯美·冷色调',
        "desc": '将画面调整为静谧的蓝紫色调',
        "code": [
            'setTransform:{"colorRed":180, "colorGreen":190, "colorBlue":255, "brightness":0.8, "contrast":1.2} -target=bg-main -duration=1500',
        ],
    },
    {
        "cat": '滤镜特效',
        "kind": 'transform',
        "name": '黄昏余晖·暖橙色',
        "desc": '将画面调整为强烈的橘红色调',
        "code": [
            'setTransform:{"colorRed":255, "colorGreen":210, "colorBlue":160, "brightness":1.05} -target=bg-main -duration=1500',
        ],
    },
    {
        "cat": '镜头动画',
        "kind": 'animation',
        "name": '缓慢推近',
        "desc": '制造压迫感或专注点',
        "code": [
            'setTempAnimation:[{"duration": 0, "scale": {"x": 1, "y": 1}}, {"duration": 3000, "scale": {"x": 1.2, "y": 1.2}}] -target=bg-main',
        ],
    },
    {
        "cat": '镜头动画',
        "kind": 'animation',
        "name": '快速拉远',
        "desc": '用于揭示全景或惊讶',
        "code": [
            'setTempAnimation:[{"duration": 0, "scale": {"x": 1.3, "y": 1.3}}, {"duration": 400, "scale": {"x": 1.0, "y": 1.0}}] -target=bg-main -next',
        ],
    },
    {
        "cat": '镜头动画',
        "kind": 'animation',
        "name": '横向摇摄·向右',
        "desc": '环视周围',
        "code": [
            'setTempAnimation:[{"duration": 0, "scale": {"x": 1.1, "y": 1.1}, "position": {"x": -50, "y": 0}}, {"duration": 4000, "position": {"x": 50, "y": 0}}] -target=bg-main',
        ],
    },
    {
        "cat": '镜头动画',
        "kind": 'animation',
        "name": '纵向摇摄·向上',
        "desc": '仰望天空或高楼',
        "code": [
            'setTempAnimation:[{"duration": 0, "scale": {"x": 1.1, "y": 1.1}, "position": {"x": 0, "y": 50}}, {"duration": 4000, "position": {"x": 0, "y": -50}}] -target=bg-main',
        ],
    },
    {
        "cat": '镜头动画',
        "kind": 'animation',
        "name": '镜头左摇',
        "desc": 'Pan Left',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0,"y":0}},{"duration":2000,"position":{"x":100,"y":0}}] -target=bg-main',
        ],
    },
    {
        "cat": '镜头动画',
        "kind": 'animation',
        "name": '镜头右摇',
        "desc": 'Pan Right',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0,"y":0}},{"duration":2000,"position":{"x":-100,"y":0}}] -target=bg-main',
        ],
    },
    {
        "cat": '镜头动画',
        "kind": 'animation',
        "name": '仰视天空',
        "desc": 'Tilt Up',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0,"y":0}},{"duration":2000,"position":{"x":0,"y":100},"scale":{"x":1.05,"y":1.05}}] -target=bg-main',
        ],
    },
    {
        "cat": '镜头动画',
        "kind": 'animation',
        "name": '俯视地面',
        "desc": 'Tilt Down',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0,"y":0}},{"duration":2000,"position":{"x":0,"y":-100},"scale":{"x":1.05,"y":1.05}}] -target=bg-main',
        ],
    },
    {
        "cat": '镜头动画',
        "kind": 'animation',
        "name": '荷兰式倾斜',
        "desc": '表现不安、疯狂或失衡（缓慢倾斜）',
        "code": [
            'setTempAnimation:[{"duration": 0, "rotation": 0, "scale": {"x": 1.2, "y": 1.2}}, {"duration": 2000, "rotation": 0.1}] -target=bg-main',
        ],
    },
    {
        "cat": '镜头动画',
        "kind": 'animation',
        "name": '急剧对焦',
        "desc": 'Re-Focus',
        "code": [
            'setTempAnimation:[{"duration": 0, "blur": 30}, {"duration": 800, "blur": 0}] -target=bg-main -next',
        ],
    },
    {
        "cat": '镜头动画',
        "kind": 'animation',
        "name": '失焦模糊',
        "desc": 'Out of Focus',
        "code": [
            'setTempAnimation:[{"duration": 0, "blur": 0}, {"duration": 1500, "blur": 20}] -target=bg-main -next',
        ],
    },
    {
        "cat": '镜头动画',
        "kind": 'animation',
        "name": '呼吸感镜头',
        "desc": '极其微小的缩放循环，用于打破静止画面',
        "code": [
            'setTempAnimation:[{"duration": 0, "scale": {"x": 1.0, "y": 1.0}}, {"duration": 2000, "scale": {"x": 1.03, "y": 1.03}}, {"duration": 2000, "scale": {"x": 1.0, "y": 1.0}}] -target=bg-main',
        ],
    },
    {
        "cat": '镜头动画',
        "kind": 'animation',
        "name": '水平震颤',
        "desc": 'Horizontal Shiver',
        "code": [
            'setTempAnimation:[{"duration": 0, "position": {"x": 0, "y": 0}}, {"duration": 100, "position": {"x": 5, "y": 0}}, {"duration": 100, "position": {"x": -5, "y": 0}}, {"duration": 100, "position": {"x": 5, "y": 0}}, {"duration": 100, "position": {"x": 0, "y": 0}}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '瞬间黑屏转场',
        "desc": 'Hard Cut Black',
        "code": [
            'setTempAnimation:[{"duration": 0, "brightness": 1}, {"duration": 100, "brightness": 0}, {"duration": 500, "brightness": 0}, {"duration": 200, "brightness": 1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '惊恐心跳',
        "desc": 'Heartbeat',
        "code": [
            'setTempAnimation:[{"duration": 0, "scale": {"x": 1, "y": 1}, "colorGreen": 255, "colorBlue": 255}, {"duration": 100, "scale": {"x": 1.05, "y": 1.05}, "colorGreen": 200, "colorBlue": 200}, {"duration": 300, "scale": {"x": 1, "y": 1}, "colorGreen": 255, "colorBlue": 255}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '心跳悸动（暗角版）',
        "desc": '恐惧、紧张、遇到危险、心动',
        "code": [
            'setTempAnimation:[{"duration": 0, "vignetting": true, "vignettingAlpha": 0, "colorRed": 255, "colorGreen": 255, "colorBlue": 255}, {"duration": 200, "vignettingAlpha": 0.6, "colorRed": 255, "colorGreen": 200, "colorBlue": 200}, {"duration": 1000, "vignettingAlpha": 0, "colorRed": 255, "colorGreen": 255, "colorBlue": 255}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '闪回白光',
        "desc": 'Bright Flashback',
        "code": [
            'setTempAnimation:[{"duration": 0, "brightness": 1, "blur": 0}, {"duration": 200, "brightness": 5, "blur": 10}, {"duration": 1500, "brightness": 1, "blur": 0}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '灰暗绝望',
        "desc": 'Despair/Grey',
        "code": [
            'setTempAnimation:[{"duration": 0, "saturation": 1}, {"duration": 2000, "saturation": 0, "brightness": 0.8}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '愤怒爆发',
        "desc": 'Rage',
        "code": [
            'setTempAnimation:[{"duration": 0, "colorGreen": 255, "colorBlue": 255}, {"duration": 200, "colorGreen": 100, "colorBlue": 100, "position": {"x": 10, "y": 10}}, {"duration": 200, "position": {"x": -10, "y": -10}}, {"duration": 200, "position": {"x": 0, "y": 0}}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '中毒/眩晕',
        "desc": 'Poisoned',
        "code": [
            'setTempAnimation:[{"duration": 0, "colorRed": 255, "colorGreen": 255, "colorBlue": 255, "blur": 0}, {"duration": 1000, "colorRed": 200, "colorGreen": 100, "colorBlue": 255, "blur": 5}, {"duration": 1000, "colorRed": 255, "colorGreen": 255, "colorBlue": 255, "blur": 0}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '冰冻/寒冷',
        "desc": 'Freezing',
        "code": [
            'setTempAnimation:[{"duration": 0, "colorRed": 255, "colorGreen": 255, "contrast": 1}, {"duration": 1000, "colorRed": 150, "colorGreen": 200, "contrast": 1.2}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '警报红光闪烁',
        "desc": 'Alarm',
        "code": [
            'setTempAnimation:[{"duration": 0, "colorGreen": 255, "colorBlue": 255}, {"duration": 500, "colorGreen": 100, "colorBlue": 100}, {"duration": 500, "colorGreen": 255, "colorBlue": 255}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '眨眼',
        "desc": 'Blinking',
        "code": [
            'setTempAnimation:[{"duration": 0, "brightness": 1}, {"duration": 100, "brightness": 0}, {"duration": 50, "brightness": 0}, {"duration": 100, "brightness": 1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '昏昏欲睡',
        "desc": 'Drowsy',
        "code": [
            'setTempAnimation:[{"duration": 0, "brightness": 1, "blur": 0}, {"duration": 3000, "brightness": 0.5, "blur": 10}, {"duration": 200, "brightness": 1, "blur": 0}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '灵感/发现',
        "desc": 'Idea',
        "code": [
            'setTempAnimation:[{"duration": 0, "brightness": 1}, {"duration": 100, "brightness": 1.5}, {"duration": 300, "brightness": 1}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '受击震动',
        "desc": 'Damage Shake',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0,"y":0}},{"duration":50,"position":{"x":20,"y":-15}},{"duration":50,"position":{"x":-15,"y":20}},{"duration":50,"position":{"x":-10,"y":-10}},{"duration":50,"position":{"x":0,"y":0}}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '致命一击',
        "desc": 'Critical Hit',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":1},{"duration":50,"brightness":-1},{"duration":50,"brightness":1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '躲闪',
        "desc": 'Dodge',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0,"y":0}},{"duration":100,"position":{"x":-200,"y":0}},{"duration":200,"position":{"x":0,"y":0}}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '后坐力',
        "desc": 'Recoil',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1}},{"duration":50,"scale":{"x":1.05,"y":1.05}},{"duration":200,"scale":{"x":1,"y":1}}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '跌倒',
        "desc": 'Fall Down',
        "code": [
            'setTempAnimation:[{"duration":0,"rotation":0,"position":{"y":0}},{"duration":600,"rotation":0.1,"position":{"y":300},"alpha":0}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '蓄力',
        "desc": 'Charge',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1},"brightness":1},{"duration":1000,"scale":{"x":0.95,"y":0.95},"brightness":0.7}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '冲击波释放',
        "desc": 'Release',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":0.95,"y":0.95},"brightness":0.7},{"duration":100,"scale":{"x":1.1,"y":1.1},"brightness":1.5},{"duration":300,"scale":{"x":1,"y":1},"brightness":1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '拔刀/斩击',
        "desc": 'Slash',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0,"y":0}},{"duration":50,"position":{"x":50,"y":50}},{"duration":50,"position":{"x":-50,"y":-50}},{"duration":50,"position":{"x":0,"y":0}}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '地震',
        "desc": 'Earthquake',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"y":0}},{"duration":100,"position":{"y":10}},{"duration":100,"position":{"y":-10}},{"duration":100,"position":{"y":10}},{"duration":100,"position":{"y":0}}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '时间停止',
        "desc": 'Time Stop',
        "code": [
            'setTempAnimation:[{"duration":0,"saturation":1}, {"duration":100,"saturation":0, "brightness": 0.8}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘跳跃',
        "desc": 'Figure Jump',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"y":0}},{"duration":150,"position":{"y":-50}},{"duration":150,"position":{"y":0}}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘震惊',
        "desc": 'Figure Shock',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1}},{"duration":50,"scale":{"x":1.1,"y":1.1},"position":{"x":10,"y":0}},{"duration":50,"scale":{"x":1.1,"y":1.1},"position":{"x":-10,"y":0}},{"duration":50,"scale":{"x":1.1,"y":1.1},"position":{"x":0,"y":0}}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘点头',
        "desc": 'Figure Nod',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"y":0}},{"duration":150,"position":{"y":20}},{"duration":150,"position":{"y":0}}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘摇头/拒绝',
        "desc": 'Figure Shake No',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0}},{"duration":100,"position":{"x":-15}},{"duration":100,"position":{"x":15}},{"duration":100,"position":{"x":0}}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘逼近',
        "desc": 'Figure Approach',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1}},{"duration":500,"scale":{"x":1.3,"y":1.3}}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘后退',
        "desc": 'Figure Retreat',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1}},{"duration":500,"scale":{"x":0.8,"y":0.8},"alpha":0.8}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘叹气/沮丧',
        "desc": 'Figure Sigh',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1}, "position":{"y":0}},{"duration":600,"scale":{"x":1.02,"y":0.98}, "position":{"y":5}},{"duration":600,"scale":{"x":1,"y":1}, "position":{"y":0}}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘闪亮登场',
        "desc": 'Figure Flash Enter',
        "code": [
            'setTempAnimation:[{"duration":0,"alpha":0,"brightness":2},{"duration":500,"alpha":1,"brightness":1}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘灵体化/幽灵',
        "desc": 'Ghost Mode',
        "code": [
            'setTempAnimation:[{"duration":0,"alpha":1}, {"duration":1000,"alpha":0.5}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘跌倒/被推倒',
        "desc": 'Figure Fall',
        "code": [
            'setTempAnimation:[{"duration":0,"rotation":0,"position":{"y":0}},{"duration":300,"rotation":-1.5,"position":{"y":300, "x": -200}}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘登场（滑入）',
        "desc": 'Fade In slide',
        "code": [
            'setTempAnimation:[{"duration":0,"alpha":0,"position":{"x":-100}},{"duration":600,"alpha":1,"position":{"x":0}}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘暗淡',
        "desc": 'Dim',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":1},{"duration":300,"brightness":0.6}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘点亮',
        "desc": 'Light Up',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":0.6},{"duration":300,"brightness":1}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘左右张望',
        "desc": 'Look Around',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0}},{"duration":400,"position":{"x":-30}},{"duration":800,"position":{"x":30}},{"duration":400,"position":{"x":0}}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '打雷闪电',
        "desc": 'Lightning / Thunder',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":1},{"duration":50,"brightness":3},{"duration":50,"brightness":1},{"duration":100,"brightness":2},{"duration":800,"brightness":1}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '日出/天亮',
        "desc": 'Sunrise',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":0.3,"colorBlue":200,"colorRed":100},{"duration":3000,"brightness":1,"colorBlue":255,"colorRed":255}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '日落/黄昏',
        "desc": 'Sunset',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":1,"colorBlue":255},{"duration":3000,"brightness":0.8,"colorBlue":150,"colorGreen":200}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '夜晚降临',
        "desc": 'Nightfall',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":1,"colorRed":255},{"duration":3000,"brightness":0.4,"colorRed":100,"colorGreen":100,"colorBlue":255}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '故障信号',
        "desc": 'Signal Interference',
        "code": [
            'setTempAnimation:[{"duration":0,"noise":0},{"duration":100,"noise":1},{"duration":100,"noise":0.2},{"duration":100,"noise":0.8},{"duration":100,"noise":0}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '逐渐石化',
        "desc": 'Petrification',
        "code": [
            'setTempAnimation:[{"duration":0,"saturation":1},{"duration":2000,"saturation":0}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '神秘氛围',
        "desc": 'Mystery',
        "code": [
            'setTempAnimation:[{"duration":0,"contrast":1,"brightness":1},{"duration":2000,"contrast":1.5,"brightness":0.8}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '梦幻柔光动画',
        "desc": 'Bloom Pulse',
        "code": [
            'setTempAnimation:[{"duration":0,"bloom":0},{"duration":1500,"bloom":1.5},{"duration":1500,"bloom":0}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '紧张对峙',
        "desc": 'Standoff',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1},"vignettingAlpha":0},{"duration":3000,"scale":{"x":1.1,"y":1.1},"vignettingAlpha":0.7}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '结束/谢幕',
        "desc": 'Fade to White',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":1,"alpha":1},{"duration":2000,"brightness":5,"alpha":0}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '拉镜聚焦',
        "desc": '电影级拉镜聚焦效果',
        "code": [
            'setTempAnimation:[{"duration": 0, "scale": { "x": 1.2, "y": 1.2 }, "blur": 20, "brightness": 2.0}, {"duration": 1500, "scale": { "x": 1.0, "y": 1.0 }, "blur": 0, "brightness": 1.0}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '苏醒/睁眼（主观视角）',
        "desc": '适用场景：开场、昏迷后醒来、切换到第一人称视角',
        "code": [
            'setTempAnimation:[{"duration": 0, "blur": 20, "brightness": 0},{"duration": 1500, "blur": 15, "brightness": 0.6},{"duration": 200, "blur": 20, "brightness": 0},{"duration": 1500, "blur": 5, "brightness": 0.8},{"duration": 1300, "blur": 0, "brightness": 1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '严重眩晕/醉酒',
        "desc": '适用场景：喝醉、中毒、生病、精神受到打击',
        "code": [
            'setTempAnimation:[{"duration": 0, "rotation": 0, "scale": {"x": 1, "y": 1}, "blur": 0},{"duration": 2000, "rotation": 0.05, "scale": {"x": 1.1, "y": 1.1}, "blur": 4},{"duration": 2000, "rotation": -0.05, "scale": {"x": 1.1, "y": 1.1}, "blur": 6},{"duration": 2000, "rotation": 0, "scale": {"x": 1, "y": 1}, "blur": 0}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '闪回/记忆重叠',
        "desc": '适用场景：突然想起某事、世界线变动、电子信号接入',
        "code": [
            'setTempAnimation:[{"duration": 0, "rgbFilm": false, "noise": 0, "brightness": 1},{"duration": 200, "rgbFilm": true, "red": [2, 0], "green": [-2, 0], "blue": [0, 2], "noise": 0.5, "brightness": 1.5},{"duration": 500, "rgbFilm": false, "noise": 0, "brightness": 1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '冲击波',
        "desc": 'Shockwave',
        "code": [
            'setTempAnimation:[{"duration":0,"shockwaveFilter":0},{"duration":1000,"shockwaveFilter":1,"amplitude":30,"wavelength":160}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '寒冷',
        "desc": 'Freeze',
        "code": [
            'setTempAnimation:[{"duration":0,"contrast":1,"colorRed":255},{"duration":2000,"contrast":1.2,"colorRed":180,"colorGreen":220}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '老电视开机',
        "desc": 'TV On',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"y":0},"brightness":5},{"duration":300,"scale":{"y":1},"brightness":1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '老电视关机',
        "desc": 'TV Off',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"y":1}},{"duration":300,"scale":{"y":0.01},"position":{"y":360}},{"duration":100,"scale":{"x":0},"alpha":0}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '对焦切换',
        "desc": '清晰→模糊→清晰',
        "code": [
            'setTempAnimation:[{"duration":0,"blur":0},{"duration":800,"blur":15},{"duration":1200,"blur":0}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '震动推近',
        "desc": '紧张对峙',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1.0,"y":1.0},"position":{"x":0,"y":0}},{"duration":2000,"scale":{"x":1.1,"y":1.1},"position":{"x":3,"y":3}},{"duration":50,"position":{"x":-3,"y":-3}},{"duration":50,"position":{"x":3,"y":3}},{"duration":50,"position":{"x":0,"y":0}}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '剧烈地震',
        "desc": 'Earthquake（剧烈版）',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0,"y":0}},{"duration":50,"position":{"x":20,"y":-20}},{"duration":50,"position":{"x":-20,"y":20}},{"duration":50,"position":{"x":15,"y":15}},{"duration":50,"position":{"x":-15,"y":-15}},{"duration":50,"position":{"x":0,"y":0}}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '车辆颠簸',
        "desc": 'Vehicle',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"y":0}},{"duration":200,"position":{"y":3},"scale":{"x":1.01,"y":1.01}},{"duration":200,"position":{"y":-3}},{"duration":200,"position":{"y":0}}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '回忆杀·纯白淡入',
        "desc": '从全白屏幕高亮模糊，缓慢过渡到清晰画面',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":5,"blur":10},{"duration":3000,"brightness":1,"blur":0}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '甜蜜眩晕·呼吸感',
        "desc": '画面带有节奏的微弱缩放和左右摇摆',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1},"rotation":0},{"duration":2500,"scale":{"x":1.02,"y":1.02},"rotation":0.015},{"duration":2500,"scale":{"x":1,"y":1},"rotation":-0.015}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '时间定格·一见钟情',
        "desc": '画面瞬间极高对比度+去色（仿佛时间停止）',
        "code": [
            'setTempAnimation:[{"duration":0,"saturation":1,"contrast":1},{"duration":100,"saturation":0,"contrast":1.5},{"duration":3000,"saturation":1,"contrast":1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '小鹿乱撞·快速心跳',
        "desc": '连续三次快速的震动和红光闪烁',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1}},{"duration":80,"scale":{"x":1.03,"y":1.03},"colorGreen":200},{"duration":80,"scale":{"x":1,"y":1},"colorGreen":255},{"duration":80,"scale":{"x":1.05,"y":1.05},"colorGreen":180},{"duration":200,"scale":{"x":1,"y":1},"colorGreen":255}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '七彩棱镜·炫光',
        "desc": '强烈的RGB分离动画',
        "code": [
            'setTempAnimation:[{"duration":0,"rgbFilm":true},{"duration":2000,"rgbFilm":true,"blur":2,"brightness":1.2},{"duration":2000,"rgbFilm":false,"blur":0,"brightness":1}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '灵魂出窍·升天',
        "desc": '背景半透明并缓慢向上飘动',
        "code": [
            'setTempAnimation:[{"duration":0,"alpha":1,"position":{"y":0}},{"duration":4000,"alpha":0,"position":{"y":-100}}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '像素重构',
        "desc": '画面从马赛克逐渐变清晰',
        "code": [
            'setTempAnimation:[{"duration":0,"pixelSize":20},{"duration":1500,"pixelSize":1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '压迫感·缓慢逼近',
        "desc": '镜头缓慢放大，同时画面变暗变红',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1},"brightness":1},{"duration":5000,"scale":{"x":1.3,"y":1.3},"brightness":0.6,"colorRed":255,"colorGreen":150,"colorBlue":150}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '心碎·冲击波',
        "desc": '通过Shockwave模拟心碎的瞬间震感',
        "code": [
            'setTempAnimation:[{"duration":0,"shockwaveFilter":0},{"duration":1500,"shockwaveFilter":0.5},{"duration":100,"shockwaveFilter":-1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '坏掉的电视',
        "desc": '信号干扰（Glitch + Noise）',
        "code": [
            'setTempAnimation:[{"duration":0,"glitchFilm":false},{"duration":100,"glitchFilm":true},{"duration":100,"glitchFilm":false},{"duration":100,"glitchFilm":true}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '不安的搏动',
        "desc": '画面整体颜色在正常和冷色之间缓慢脉冲',
        "code": [
            'setTempAnimation:[{"duration":0,"colorRed":255},{"duration":1000,"colorRed":150,"colorGreen":200},{"duration":1000,"colorRed":255}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '破碎预警',
        "desc": '高频的微小震动 + 故障',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0},"glitchFilm":false},{"duration":50,"position":{"x":5},"glitchFilm":true},{"duration":50,"position":{"x":-5}},{"duration":50,"position":{"x":0},"glitchFilm":false}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘·丝滑登场',
        "desc": '从下方滑入 + 透明度渐变',
        "code": [
            'setTempAnimation:[{"duration":0,"alpha":0,"position":{"y":100}},{"duration":600,"alpha":1,"position":{"y":0}}] -target=fig-center -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '立绘·生气/发抖',
        "desc": '极高频的小幅度震动',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0}},{"duration":30,"position":{"x":3}},{"duration":30,"position":{"x":-3}},{"duration":30,"position":{"x":3}},{"duration":30,"position":{"x":0}}] -target=fig-center',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '吃醋·世界扭曲',
        "desc": '画面进行扭曲旋转（Rotation）',
        "code": [
            'setTempAnimation:[{"duration":0,"rotation":0,"scale":{"x":1,"y":1}},{"duration":3000,"rotation":0.2,"scale":{"x":1.5,"y":1.5}}] -target=bg-main',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '聚光灯开启',
        "desc": '圆形遮罩从0扩大到全屏',
        "code": [
            'setTempAnimation:[{"duration":0,"radiusAlphaFilter":0},{"duration":1500,"radiusAlphaFilter":1.5}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '聚光灯关闭',
        "desc": '圆形遮罩收缩直至全黑',
        "code": [
            'setTempAnimation:[{"duration":0,"radiusAlphaFilter":1.5},{"duration":1500,"radiusAlphaFilter":0}] -target=bg-main -next',
        ],
    },
    {
        "cat": '情绪与状态',
        "kind": 'animation',
        "name": '清晨苏醒',
        "desc": '高亮度模糊 -> 清晰正常',
        "code": [
            'setTempAnimation:[{"duration":0,"blur":20,"brightness":2},{"duration":3000,"blur":0,"brightness":1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '瞬间心动·粉色冲击',
        "desc": '画面瞬间泛起粉色光晕并轻微放大',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1},"bloom":0},{"duration":150,"scale":{"x":1.05,"y":1.05},"bloom":2,"colorRed":255,"colorGreen":220,"colorBlue":230},{"duration":1000,"scale":{"x":1,"y":1},"bloom":0.5,"colorRed":255,"colorGreen":255,"colorBlue":255}] -target=bg-main -next',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '满眼都是你·聚光',
        "desc": '利用圆形遮罩，让周围变暗，只聚焦中心',
        "code": [
            'setTempAnimation:[{"duration":0,"radiusAlphaFilter":0},{"duration":2000,"radiusAlphaFilter":0.6}] -target=bg-main',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '心动悸动·温柔红光',
        "desc": '温柔红光收缩+轻微模糊',
        "code": [
            'setTempAnimation:[{"duration":0,"vignetting":true,"vignettingAlpha":0,"colorRed":255,"colorGreen":240,"colorBlue":245},{"duration":300,"vignettingAlpha":0.4,"colorRed":255,"colorGreen":230,"colorBlue":235},{"duration":900,"vignettingAlpha":0,"colorRed":255,"colorGreen":245,"colorBlue":250}] -target=bg-main -next',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '柔光环绕',
        "desc": '泛光渐变+轻微提亮',
        "code": [
            'setTempAnimation:[{"duration":0,"bloom":0,"brightness":1},{"duration":2000,"bloom":1.2,"bloomBlur":3,"brightness":1.05},{"duration":2000,"bloom":0,"brightness":1}] -target=bg-main',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '甜度升温',
        "desc": '色调变暖+轻微缩放',
        "code": [
            'setTempAnimation:[{"duration":0,"colorRed":255,"colorGreen":230,"colorBlue":220,"scale":{"x":1,"y":1}},{"duration":3000,"colorRed":255,"colorGreen":240,"colorBlue":225,"scale":{"x":1.02,"y":1.02}}] -target=bg-main',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '靠近对视',
        "desc": '立绘缓慢凑近镜头',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1},"position":{"x":0,"y":0}},{"duration":2500,"scale":{"x":1.2,"y":1.2},"position":{"x":0,"y":-10}}] -target=fig-center',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '害羞低头',
        "desc": '立绘轻微下移+透明度微调',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"y":0},"alpha":1},{"duration":800,"position":{"y":15},"alpha":0.95},{"duration":800,"position":{"y":0},"alpha":1}] -target=fig-center',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '脸颊微红',
        "desc": '立绘局部泛红+柔光',
        "code": [
            'setTempAnimation:[{"duration":0,"colorRed":255,"colorGreen":255,"colorBlue":255,"bloom":0},{"duration":1500,"colorRed":255,"colorGreen":235,"colorBlue":235,"bloom":0.8},{"duration":1500,"colorRed":255,"colorGreen":255,"colorBlue":255,"bloom":0}] -target=fig-center',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '落日余晖',
        "desc": '暖橙色调渐变+暗角',
        "code": [
            'setTempAnimation:[{"duration":0,"colorRed":255,"colorGreen":200,"colorBlue":150,"vignettingAlpha":0},{"duration":3000,"colorRed":255,"colorGreen":210,"colorBlue":160,"vignettingAlpha":0.3},{"duration":3000,"colorRed":255,"colorGreen":220,"colorBlue":180,"vignettingAlpha":0}] -target=bg-main',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '星光闪烁',
        "desc": '轻微亮度波动+泛光',
        "code": [
            'setTempAnimation:[{"duration":0,"bloom":0.5,"brightness":1},{"duration":1000,"bloom":0.8,"brightness":1.1},{"duration":1000,"bloom":0.5,"brightness":1},{"duration":1000,"bloom":0.8,"brightness":1.1},{"duration":1000,"bloom":0.5,"brightness":1}] -target=bg-main',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '花瓣飘落感',
        "desc": '背景轻微位移+模糊',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0,"y":0},"blur":0},{"duration":5000,"position":{"x":20,"y":-10},"blur":0.5},{"duration":5000,"position":{"x":-20,"y":10},"blur":0.5}] -target=bg-main',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '雨夜浪漫',
        "desc": '轻微模糊+冷调变暖',
        "code": [
            'setTempAnimation:[{"duration":0,"colorRed":200,"colorGreen":220,"colorBlue":255,"blur":1},{"duration":4000,"colorRed":230,"colorGreen":240,"colorBlue":255,"blur":0.5}] -target=bg-main',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '甜蜜回忆',
        "desc": '暖黄调+轻微噪点',
        "code": [
            'setTempAnimation:[{"duration":0,"sepia":0,"noise":0,"brightness":1},{"duration":1500,"sepia":0.3,"noise":0.1,"brightness":0.9},{"duration":3000,"sepia":0.3,"noise":0.1,"brightness":0.9},{"duration":1500,"sepia":0,"noise":0,"brightness":1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '初见闪回',
        "desc": '柔光爆发+快速对焦',
        "code": [
            'setTempAnimation:[{"duration":0,"bloom":2,"blur":10,"brightness":1.5},{"duration":1000,"bloom":0,"blur":0,"brightness":1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '细节回忆',
        "desc": '镜头缓慢推近+柔光',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1},"bloom":0},{"duration":3000,"scale":{"x":1.1,"y":1.1},"bloom":0.7},{"duration":2000,"scale":{"x":1.1,"y":1.1},"bloom":0.7},{"duration":3000,"scale":{"x":1,"y":1},"bloom":0}] -target=bg-main',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '告白瞬间',
        "desc": '强烈泛光+缓慢提亮',
        "code": [
            'setTempAnimation:[{"duration":0,"bloom":0,"brightness":1,"colorRed":255,"colorGreen":255,"colorBlue":255},{"duration":1500,"bloom":2,"brightness":1.2,"colorRed":255,"colorGreen":245,"colorBlue":250},{"duration":2000,"bloom":0,"brightness":1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '点头回应',
        "desc": '立绘轻微上下位移',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"y":0}},{"duration":300,"position":{"y":-10}},{"duration":300,"position":{"y":0}},{"duration":300,"position":{"y":-10}},{"duration":300,"position":{"y":0}}] -target=fig-center',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '拥抱预热（左立绘）',
        "desc": '双立绘缓慢靠近',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":-50,"y":0}},{"duration":3000,"position":{"x":0,"y":0}}] -target=fig-left',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '拥抱预热（右立绘）',
        "desc": '配合左立绘使用',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":50,"y":0}},{"duration":3000,"position":{"x":0,"y":0}}] -target=fig-right',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '心动确认',
        "desc": '立绘泛光+轻微旋转',
        "code": [
            'setTempAnimation:[{"duration":0,"bloom":0,"rotation":0},{"duration":1000,"bloom":1,"rotation":0.02},{"duration":1000,"bloom":1,"rotation":-0.02},{"duration":1000,"bloom":0,"rotation":0}] -target=fig-center',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '耳鬓厮磨',
        "desc": '立绘轻微侧移+缩放',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1},"position":{"x":0,"y":0}},{"duration":1800,"scale":{"x":1.1,"y":1.1},"position":{"x":-10,"y":5}}] -target=fig-center',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '温柔注视',
        "desc": '立绘轻微提亮+边缘发光',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":1,"bloom":0},{"duration":1500,"brightness":1.1,"bloom":1,"bloomThreshold":0.2},{"duration":1500,"brightness":1,"bloom":0}] -target=fig-center',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '樱花氛围',
        "desc": '背景粉白渐变+轻微模糊',
        "code": [
            'setTempAnimation:[{"duration":0,"colorRed":255,"colorGreen":245,"colorBlue":250,"blur":0},{"duration":2500,"colorRed":255,"colorGreen":250,"colorBlue":255,"blur":0.3},{"duration":2500,"colorRed":255,"colorGreen":245,"colorBlue":250,"blur":0}] -target=bg-main',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '牵手升温',
        "desc": '整体色调变暖+轻微震动',
        "code": [
            'setTempAnimation:[{"duration":0,"colorRed":255,"colorGreen":230,"colorBlue":220,"shakePower":0},{"duration":500,"colorRed":255,"colorGreen":220,"colorBlue":210,"shakePower":2},{"duration":500,"colorRed":255,"colorGreen":230,"colorBlue":220,"shakePower":0}] -target=bg-main',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '月下告白',
        "desc": '冷调渐变+泛光',
        "code": [
            'setTempAnimation:[{"duration":0,"colorRed":200,"colorGreen":220,"colorBlue":255,"bloom":0},{"duration":3000,"colorRed":220,"colorGreen":230,"colorBlue":255,"bloom":1.5},{"duration":3000,"colorRed":230,"colorGreen":240,"colorBlue":255,"bloom":0}] -target=bg-main',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '害羞躲闪',
        "desc": '立绘快速侧移+恢复',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0,"y":0}},{"duration":300,"position":{"x":15,"y":0}},{"duration":300,"position":{"x":-10,"y":0}},{"duration":300,"position":{"x":0,"y":0}}] -target=fig-center',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '氛围升温',
        "desc": '暗角渐变+暖调',
        "code": [
            'setTempAnimation:[{"duration":0,"vignettingAlpha":0,"colorRed":255,"colorGreen":240,"colorBlue":230},{"duration":4000,"vignettingAlpha":0.5,"colorRed":255,"colorGreen":230,"colorBlue":220}] -target=bg-main',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '惊喜瞬间',
        "desc": '亮度突发+泛光',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":1,"bloom":0},{"duration":200,"brightness":1.5,"bloom":1.8},{"duration":800,"brightness":1,"bloom":0}] -target=bg-main -next',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '并肩同行（左立绘）',
        "desc": '双立绘同步轻微位移',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0,"y":0}},{"duration":5000,"position":{"x":30,"y":0}}] -target=fig-left',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '并肩同行（右立绘）',
        "desc": '配合左立绘使用',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0,"y":0}},{"duration":5000,"position":{"x":30,"y":0}}] -target=fig-right',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '温柔告别',
        "desc": '立绘缓慢淡化+背景柔光',
        "code": [
            'setTempAnimation:[{"duration":0,"alpha":1,"bloom":0},{"duration":3000,"alpha":0.8,"bloom":0.7},{"duration":2000,"alpha":0.5,"bloom":0.5}] -target=fig-center',
        ],
    },
    {
        "cat": '恋爱场景',
        "kind": 'animation',
        "name": '告白成功',
        "desc": '全画面泛光+暖调爆发',
        "code": [
            'setTempAnimation:[{"duration":0,"bloom":0,"brightness":1,"colorRed":255,"colorGreen":240,"colorBlue":230},{"duration":1500,"bloom":2,"brightness":1.3,"colorRed":255,"colorGreen":220,"colorBlue":210},{"duration":1500,"bloom":0,"brightness":1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'transform',
        "name": '复古回忆（背景滤镜）',
        "desc": '复古回忆风格滤镜',
        "code": [
            'setTransform:{"brightness":0.7,"contrast":1.2,"saturation":0.5,"sepia":0.8,"bloom":0.4} -target=bg-main -duration=0 -next',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '复古回忆（背景动画）',
        "desc": '缓慢模糊动画',
        "code": [
            'setTempAnimation:[{"duration":0,"blur":0},{"duration":5000,"blur":3}] -target=bg-main',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'transform',
        "name": '复古回忆（人物滤镜）',
        "desc": '复古回忆风格人物滤镜',
        "code": [
            'setTransform:{"brightness":0.6,"contrast":1.3,"saturation":0.6,"sepia":0.7,"bevel":0.8} -target=fig-center -duration=0',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '复古回忆（人物动画）',
        "desc": '轻微缩放动画',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1}},{"duration":4000,"scale":{"x":1.02,"y":1.02}}] -target=fig-center',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'transform',
        "name": '赛博波动（背景滤镜）',
        "desc": '赛博波动风格背景滤镜',
        "code": [
            'setTransform:{"brightness":0.9,"contrast":1.5,"saturation":1.8,"colorRed":180,"colorGreen":200,"colorBlue":255,"glitchFilm":true} -target=bg-main -duration=0 -next',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '赛博波动（背景动画）',
        "desc": '色彩漂移动画',
        "code": [
            'setTempAnimation:[{"duration":0,"colorRed":180},{"duration":1000,"colorRed":200},{"duration":1000,"colorRed":180}] -target=bg-main',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'transform',
        "name": '赛博波动（人物滤镜）',
        "desc": '赛博波动风格人物滤镜',
        "code": [
            'setTransform:{"brightness":0.8,"contrast":1.4,"saturation":1.6,"colorRed":200,"colorGreen":220,"colorBlue":255} -target=fig-center -duration=0',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '赛博波动（人物动画）',
        "desc": '闪烁动画',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":0.8},{"duration":500,"brightness":1.2},{"duration":500,"brightness":0.8}] -target=fig-center',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '渐变显示（背景）',
        "desc": 'Fade In',
        "code": [
            'setTempAnimation:[{"duration":0,"alpha":0,"brightness":0.5},{"duration":2000,"alpha":1,"brightness":1}] -target=bg-main -next',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '渐变显示（人物）',
        "desc": 'Fade In slide',
        "code": [
            'setTempAnimation:[{"duration":0,"alpha":0,"position":{"y":50}},{"duration":2500,"alpha":1,"position":{"y":0}}] -target=fig-center',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '轻微旋转',
        "desc": 'Slow Rotate',
        "code": [
            'setTempAnimation:[{"duration":0,"rotation":0},{"duration":6000,"rotation":0.05},{"duration":6000,"rotation":-0.05},{"duration":6000,"rotation":0}] -target=fig-center',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '快速抖动',
        "desc": 'Shake Strong',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0,"y":0}},{"duration":50,"position":{"x":15,"y":-10}},{"duration":50,"position":{"x":-20,"y":15}},{"duration":50,"position":{"x":10,"y":-5}},{"duration":50,"position":{"x":0,"y":0}}] -target=bg-main -next',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '模糊渐变',
        "desc": 'Blur Transition',
        "code": [
            'setTempAnimation:[{"duration":0,"blur":0},{"duration":1000,"blur":15},{"duration":1000,"blur":0}] -target=bg-main -next',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '色彩漂移（背景）',
        "desc": 'Color Drift',
        "code": [
            'setTempAnimation:[{"duration":0,"colorRed":255,"colorGreen":255,"colorBlue":255},{"duration":1500,"colorRed":240,"colorGreen":255,"colorBlue":240},{"duration":1500,"colorRed":255,"colorGreen":240,"colorBlue":250},{"duration":1500,"colorRed":255,"colorGreen":255,"colorBlue":255}] -target=bg-main -next',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '色彩漂移（人物）',
        "desc": 'Color Drift人物版',
        "code": [
            'setTempAnimation:[{"duration":0,"colorRed":255,"colorGreen":255,"colorBlue":255},{"duration":1500,"colorRed":245,"colorGreen":255,"colorBlue":245},{"duration":1500,"colorRed":255,"colorBlue":255},{"duration":1500,"colorRed":255,"colorGreen":255,"colorBlue":255}] -target=fig-center',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '闪烁强调',
        "desc": 'Flash Highlight',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":1},{"duration":100,"brightness":1.8},{"duration":100,"brightness":1},{"duration":100,"brightness":1.8},{"duration":100,"brightness":1}] -target=fig-center -next',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '左右摇摆',
        "desc": 'Swing Left-Right',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"x":0}},{"duration":800,"position":{"x":-20}},{"duration":1600,"position":{"x":20}},{"duration":800,"position":{"x":0}}] -target=fig-center',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '聚焦效果',
        "desc": 'Focus In',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1},"blur":5},{"duration":1500,"scale":{"x":1.2,"y":1.2},"blur":0}] -target=fig-center',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '散焦退场',
        "desc": 'Defocus Exit',
        "code": [
            'setTempAnimation:[{"duration":0,"alpha":1,"blur":0,"scale":{"x":1,"y":1}},{"duration":1500,"alpha":0,"blur":10,"scale":{"x":0.9,"y":0.9}}] -target=fig-center -next',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '地震震动（长版）',
        "desc": 'Earthquake Long',
        "code": [
            'setTempAnimation:[{"duration":0,"position":{"y":0}},{"duration":100,"position":{"y":20}},{"duration":100,"position":{"y":-20}},{"duration":100,"position":{"y":15}},{"duration":100,"position":{"y":-15}},{"duration":100,"position":{"y":10}},{"duration":100,"position":{"y":-10}},{"duration":200,"position":{"y":0}}] -target=bg-main',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '高光闪烁（背景）',
        "desc": 'Bloom Pulse',
        "code": [
            'setTempAnimation:[{"duration":0,"bloom":0.5},{"duration":800,"bloom":1.5},{"duration":800,"bloom":0.5}] -target=bg-main -next',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '高光闪烁（人物）',
        "desc": 'Bloom Pulse人物版',
        "code": [
            'setTempAnimation:[{"duration":0,"bloom":0.3},{"duration":800,"bloom":1.0},{"duration":800,"bloom":0.3}] -target=fig-center',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '慢动作特写（立绘）',
        "desc": '慢动作特写立绘部分',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1.0,"y":1.0},"brightness":1.0},{"duration":800,"scale":{"x":1.1,"y":1.1},"brightness":1.1,"blur":0.5},{"duration":2000,"scale":{"x":1.0,"y":1.0},"brightness":1.0,"blur":0}] -target=fig-center',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '慢动作特写（背景）',
        "desc": '慢动作特写背景部分',
        "code": [
            'setTempAnimation:[{"duration":0,"brightness":1.0,"blur":0},{"duration":800,"brightness":0.8,"blur":2},{"duration":2000,"brightness":1.0,"blur":0}] -target=bg-main',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '时间停止（灰度+暗角）',
        "desc": '时间停止效果',
        "code": [
            'setTempAnimation:[{"duration":0,"saturation":1,"vignettingAlpha":0},{"duration":300,"saturation":0,"vignettingAlpha":0.5},{"duration":5000,"saturation":0,"vignettingAlpha":0.5}] -target=bg-main',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '神秘氛围动画',
        "desc": '紫色调+对比度',
        "code": [
            'setTempAnimation:[{"duration":0,"contrast":1,"brightness":1,"colorGreen":255},{"duration":2000,"contrast":1.5,"brightness":0.8,"colorGreen":150}] -target=bg-main',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '水下波纹',
        "desc": '扭曲泛蓝',
        "code": [
            'setTempAnimation:[{"duration":0,"reflectionFilm":true,"colorRed":200,"colorGreen":220,"colorBlue":255,"blur":1},{"duration":1000,"reflectionFilm":true,"colorRed":200,"colorGreen":220,"colorBlue":255,"blur":1.5},{"duration":1000,"reflectionFilm":true,"colorRed":200,"colorGreen":220,"colorBlue":255,"blur":1}] -target=bg-main',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '热浪扭曲',
        "desc": '沙漠效果',
        "code": [
            'setTempAnimation:[{"duration":0,"reflectionFilm":true,"colorRed":255,"colorGreen":240,"colorBlue":200,"blur":0.5},{"duration":500,"reflectionFilm":true,"colorRed":255,"colorGreen":240,"colorBlue":200,"blur":1},{"duration":500,"reflectionFilm":true,"colorRed":255,"colorGreen":240,"colorBlue":200,"blur":0.5}] -target=bg-main',
        ],
    },
    {
        "cat": '复合特效',
        "kind": 'animation',
        "name": '雨滴效果',
        "desc": '背景模糊+震动',
        "code": [
            'setTempAnimation:[{"duration":0,"blur":0,"position":{"x":0,"y":0}},{"duration":50,"blur":0.5,"position":{"x":2,"y":2}},{"duration":50,"blur":0.5,"position":{"x":-2,"y":-2}},{"duration":50,"blur":0.5,"position":{"x":2,"y":2}},{"duration":50,"blur":0,"position":{"x":0,"y":0}}] -target=bg-main',
        ],
    },
    {
        "cat": '重置指令',
        "kind": 'transform',
        "name": '完全重置（背景）',
        "desc": '万能还原药水',
        "code": [
            'setTransform:{"alpha":1,"scale":{"x":1,"y":1},"position":{"x":0,"y":0},"rotation":0,"blur":0,"brightness":1,"contrast":1,"saturation":1,"gamma":1,"colorRed":255,"colorGreen":255,"colorBlue":255,"oldFilm":false,"dotFilm":false,"reflectionFilm":false,"glitchFilm":false,"rgbFilm":false,"godrayFilm":false,"bloom":0,"shockwaveFilter":0,"radiusAlphaFilter":0,"bevel":0,"sepia":0,"noise":0,"vignetting":false,"vignettingAlpha":0,"shakePower":0,"pixelSize":1} -target=bg-main -duration=500',
        ],
    },
    {
        "cat": '重置指令',
        "kind": 'transform',
        "name": '完全重置（立绘）',
        "desc": '立绘重置',
        "code": [
            'setTransform:{"alpha":1,"scale":{"x":1,"y":1},"position":{"x":0,"y":0},"rotation":0,"blur":0,"brightness":1,"contrast":1,"saturation":1,"gamma":1,"colorRed":255,"colorGreen":255,"colorBlue":255,"bloom":0,"bevel":0} -target=fig-center -duration=500',
        ],
    },
    {
        "cat": '重置指令',
        "kind": 'transform',
        "name": '「失去高光·绝望」（setTransform版本）',
        "desc": '来源： 没问题，完全理解.docx - 第三章',
        "code": [
            'setTransform:{"saturation":0, "brightness":0.8, "contrast":1.3} -target=bg-main -duration=300',
        ],
    },
    {
        "cat": '重置指令',
        "kind": 'animation',
        "name": '「同步呼吸」（fig-left 版本）',
        "desc": '来源： 几十种高级演出特效.docx - 恋爱场景第6条',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1}},{"duration":2000,"scale":{"x":1.03,"y":1.03}},{"duration":2000,"scale":{"x":1,"y":1}}] -target=fig-left',
        ],
    },
    {
        "cat": '重置指令',
        "kind": 'animation',
        "name": '「同步呼吸」（fig-right 版本）',
        "desc": '来源： 几十种高级演出特效.docx - 恋爱场景第6条',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1}},{"duration":2000,"scale":{"x":1.03,"y":1.03}},{"duration":2000,"scale":{"x":1,"y":1}}] -target=fig-right',
        ],
    },
    {
        "cat": '重置指令',
        "kind": 'animation',
        "name": '「心动共鸣」',
        "desc": '来源： 几十种高级演出特效.docx - 恋爱场景第29条',
        "code": [
            'setTempAnimation:[{"duration":0,"scale":{"x":1,"y":1},"colorRed":255,"colorGreen":245,"colorBlue":250},{"duration":1000,"scale":{"x":1.02,"y":1.02,"colorRed":255,"colorGreen":235,"colorBlue":240}},{"duration":1000,"scale":{"x":1,"y":1,"colorRed":255,"colorGreen":245,"colorBlue":250}}] -target=bg-main',
        ],
    },
]


def by_name(name):
    """按名字查特效；找不到返回 None。"""
    for e in STAGE_EFFECTS:
        if e["name"] == name:
            return e
    return None


def by_category(cat):
    """取某分类下全部特效（保持 docx 原顺序）。"""
    return [e for e in STAGE_EFFECTS if e["cat"] == cat]
