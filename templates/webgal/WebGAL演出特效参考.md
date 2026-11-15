# WebGAL 高级演出特效参考（扩展包，默认不启用）

> **来源与署名**：本文档整理自 B 站 UP 主「北风的猫5306」制作的两份教程：
> 《WebGAL 高级演出特效代码整合（去重最终版）》《webgal电影感滤镜1.21》。
> 结构化数据见仓库 `templates/webgal/effect_table.json`（221 条，
> 体积超限未随投喂包附带）。版权归原作者，此处仅为
> 便于检索的收录。

## ✅ 引擎兼容性（2026-11-15 实测更新）

**`setTransform` 滤镜已实测被本引擎支持，可以正常使用。**

实测方式：用本工作室导出包（WebGAL 木偶工作室 Engine v3.4.3，基于官方
WebGAL v4.6.6）真实运行一条
`setTransform:{"brightness":0.7,"saturation":0.2,"colorRed":180,...}
 -target=bg-main`——画面明显变暗、降饱和、偏蓝（截图存档
`D:\scripts\temp\ui_shots\f_C_filtered.png`）。引擎 bundle 内亦有完整
实现链：`setTransform` 是官方命令枚举第 29 号，滤镜工厂注册了
oldFilm / dotFilm / reflectionFilm / glitchFilm / rgbFilm / godray /
bloom / vignetting / bevel / blur / pixelSize 等全部参数（grep
`index-Dcp3ZA1M.js` 实证）。

历史备注：本文档 v3.4.6 收录时曾标注「官方 4.6.6 不保证支持」——那是
未实测的保守判断（教程原文只写了「要求 4.5.13+」），v3.4.8 实测推翻。

`setTempAnimation`（运镜）与恋爱场景专用动画未逐条实测，仍按「按需
验证」使用；滤镜类（本表 50 条 + 电影感 15 组 + 复合类里的滤镜部分）
可放心用。

**使用提醒（重要）**：场景/氛围结束时必须重置（见下方「典型用法」
后的重置指令），否则滤镜会一直带到下一个场景。

## 快速查表

| 分类 | 条数 | 说明 |
|---|---|---|
| 滤镜特效（`setTransform`） | 50 | 色调/泛光/故障/老电影/暗角等静态画面风格 |
| 动画特效（`setTempAnimation`） | 88 | 推近拉远/摇摄/倾斜/震动等镜头运动 |
| 恋爱场景专用 | 32 | 心跳/脸红/柔光/樱花等情绪化演出 |
| 复合特效（滤镜+动画） | 29 | 两者同时使用 |
| 通用重置指令 | 2 | 一键还原背景/立绘的所有特效 |
| 补充附录 | 4 | 变体版本（fig-left/fig-right 等） |
| 电影感滤镜（环境光配对） | 16 | 背景+人物成对使用，共 15 个场景 |

## 三个核心指令

### `setTransform` —— 静态滤镜（可带过渡时长）

```webgal
setTransform:{参数对象} -target=作用对象 -duration=过渡毫秒;
```

- `-duration=0` 立即生效；`-duration=2000` 平滑过渡 2 秒
- 结尾加 `-next` 则等特效播完再进下一句

### `setTempAnimation` —— 一次性动画

```webgal
setTempAnimation:[关键帧数组] -target=作用对象;
```

关键帧数组里每项一个 `{"duration": 毫秒, ...目标属性}`。总时长 = 各帧 duration 之和。

### 作用对象（`-target=`）

| 值 | 作用 |
|---|---|
| `bg-main` | 背景 |
| `fig-center` | 中间立绘 |
| `fig-left` | 左侧立绘 |
| `fig-right` | 右侧立绘 |

## 常用参数速查

### 色调
`colorRed` / `colorGreen` / `colorBlue`（0-255）、`brightness`、`contrast`、
`saturation`、`gamma`（1.0 为基准）、`sepia`（棕褐色，0-1）

### 泛光
`bloom`（强度）、`bloomBrightness`、`bloomBlur`（模糊半径）、`bloomThreshold`

### 灯光/大气
`godrayFilm`（上帝之光布尔）、`lightX` / `lightY` / `lightAngle` /
`lightIntensity`（光束位置与强度）

### 故障/复古
`glitchFilm`（故障错位）、`rgbFilm`（RGB 分离）、`oldFilm`（老电影噪点划痕）、
`dotFilm`、`reflectionFilm`（水波反射）、`noise`

### 变形/镜头
`blur`、`pixelSize`（像素化）、`shakePower` / `shakeSpeed`（震动）、
`shockwaveFilter`（冲击波）、`vignetting` / `vignettingAlpha` /
`vignettingBlur`（暗角）、`radiusAlphaFilter`（圆形遮罩聚焦）

### 边缘光（倒角）
`bevel`（强度）、`bevelThickness`、`bevelRotation`（**约 80 为居中**）、
`bevelRed` / `bevelGreen` / `bevelBlue`

## 典型用法

**受冲击场景**（画面震动 + 变红 + 暗角）：
```webgal
setTransform:{"shakePower": 20, "shakeSpeed": 5, "colorRed": 255, "colorGreen": 150, "colorBlue": 150, "vignetting": true, "vignettingAlpha": 0.5, "shockwaveFilter": 0.5} -target=bg-main -duration=300;
```

**缓慢推近**（电影感运镜）：
```webgal
setTempAnimation:[{"duration": 0, "scale": {"x": 1, "y": 1}}, {"duration": 3000, "scale": {"x": 1.2, "y": 1.2}}] -target=bg-main;
```

**场景结束后必须重置**，否则特效会带到下一场：
```webgal
setTransform:{"alpha":1,"scale":{"x":1,"y":1},"position":{"x":0,"y":0},"rotation":0,"blur":0,"brightness":1,"contrast":1,"saturation":1,"gamma":1,"colorRed":255,"colorGreen":255,"colorBlue":255,"oldFilm":false,"dotFilm":false,"reflectionFilm":false,"glitchFilm":false,"rgbFilm":false,"godrayFilm":false,"bloom":0,"shockwaveFilter":0,"radiusAlphaFilter":0,"bevel":0,"sepia":0,"noise":0,"vignetting":false,"vignettingAlpha":0,"shakePower":0,"pixelSize":1} -target=bg-main -duration=500;
```

## 电影感滤镜（背景 + 人物成对）

仓库 `templates/webgal/effect_table.json`（未随包附带）里 `film_light`
分类的每一项都有 `bg`（背景）和 `fig`
（人物）两行代码，**必须成对使用**才协调。场景包括：火光/爆炸、黑暗（有灯光）、
清晨/黄昏（轻）、黑暗（无灯光）、白天、黄昏（重·室外）、黄昏（重·室内）、
面光系列等 15 组。

## 完整清单

全部 221 条的完整代码、参数、适用场景说明都在仓库
`templates/webgal/effect_table.json`（体积超限未随投喂包附带，请自取）：

```json
{
  "categories": [
    {"id": "cat_id", "name": "分类名",
     "items": [{
       "no": 1,
       "name": "赛博朋克/电子故障风格",
       "cmd": "setTransform",
       "code": "setTransform:{...};",     // 直接可用
       "target": "bg-main",
       "duration": 0,
       "next": false,
       "params": {...},                   // 解析后的参数对象
       "notes": ["适用：...", "效果：..."],
       "source": "出处文档"
     }]}
  ]
}
```

按 `cmd`、`target`、`notes` 里的关键词（如「逃跑」「战斗」「悲伤」）检索即可。
