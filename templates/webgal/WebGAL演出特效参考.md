# WebGAL 高级演出特效参考（扩展包，默认不启用）

> **来源与署名**：本文档整理自 B 站 UP 主「北风的猫5306」制作的两份教程：
> 《WebGAL 高级演出特效代码整合（去重最终版）》《webgal电影感滤镜1.21》。
> 结构化数据见同目录 `effect_table.json`（221 条）。版权归原作者，此处仅为
> 便于检索的收录。

## ⚠️ 引擎兼容性（先读这段）

这些指令**不属于官方 WebGAL 标准命令集**，是 MyGO 二创引擎的扩展：

- 原文档明确要求 **WebGAL 4.5.13 及以上**，且提到 mygo2.5 引擎；
- 官方 WebGAL 4.6.6 对这组指令**不保证支持**——可能报错或被静默忽略；
- 因此本扩展包**默认不启用**，导出器也不会自动生成这些指令。

**什么时候可以用**：你的目标运行环境确认是支持该扩展的二创引擎时，
可以按本文档查表，把代码直接粘进 WebGAL 脚本。

**本工作室脚本规范第 7 节仍然有效**：默认只写标准 WebGAL 指令。用特效
属于「明知扩展环境」的例外情况。

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

`effect_table.json` 里 `film_light` 分类的每一项都有 `bg`（背景）和 `fig`
（人物）两行代码，**必须成对使用**才协调。场景包括：火光/爆炸、黑暗（有灯光）、
清晨/黄昏（轻）、黑暗（无灯光）、白天、黄昏（重·室外）、黄昏（重·室内）、
面光系列等 15 组。

## 完整清单

全部 221 条的完整代码、参数、适用场景说明都在 `effect_table.json`：

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
