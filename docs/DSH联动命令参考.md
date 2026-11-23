# 木偶工作室 · DSH 联动命令参考

> **给 AI 看的接口文档。** 你可以通过 HTTP 命令通道直接操作木偶工作室的
> 大部分功能——建工程、上素材、剪辑、加立绘、写台词、套滤镜、做运镜、
> 打光、加投影、批量处理、导出视频/音轨/字幕。
>
> 不需要模拟鼠标点按钮。所有命令都是**幂等可重试**的，失败会返回明确原因。
>
> 端点：`POST http://127.0.0.1:8765/command`
> 请求体：`{"cmd": "<命令名>", "args": {...}}`
> 响应体：`{"ok": true/false, "msg": "人类可读说明", "data": {...}}`
>
> 通道需在应用内「DSH 联动」菜单里打开。

---

## 目录

1. [快速上手](#1-快速上手)
2. [工程与存档](#2-工程与存档)
3. [轨道与剪辑](#3-轨道与剪辑)
4. [场景 / 幕 / 分支](#4-场景--幕--分支)
5. [立绘与角色](#5-立绘与角色)
6. [台词](#6-台词)
7. [运镜（多段路径）](#7-运镜多段路径)
8. [氛围光效](#8-氛围光效)
9. [人物投影](#9-人物投影)
10. [滤镜 / 特效 / 转场](#10-滤镜--特效--转场)
11. [批量操作](#11-批量操作)
12. [导出](#12-导出)
13. [素材查询](#13-素材查询)
14. [历史与播放](#14-历史与播放)
15. [完整示例：从零做一段带运镜和光效的戏](#15-完整示例从零做一段带运镜和光效的戏)
16. [AI 使用时必须知道的限制](#16-ai-使用时必须知道的限制)

---

## 1. 快速上手

最小可用流程：

```json
{"cmd": "project.new", "args": {"width": 1920, "height": 1080, "fps": 30}}
{"cmd": "clip.add", "args": {"path": "D:/素材/教室.png", "start": 0, "dur": 5}}
{"cmd": "figure.add", "args": {"path": "D:/素材/素世.model", "x": 0.5, "y": 0.7}}
{"cmd": "say", "args": {"who": "素世", "text": "……", "dur": 2.5, "t": 0.5}}
{"cmd": "export.run", "args": {"kind": "video"}}
```

**通用约定**
- 时间单位一律是**秒**（浮点）
- 坐标 `x`/`y` 是**相对舞台的 0~1 比例**（0.5 = 正中）
- `scale` 是倍数（1.0 = 原始尺寸）
- 所有命令在主线程执行；成功会自动存档到撤销历史（`history.do undo` 可回退）
- 返回 `ok: false` 时 `msg` 里一定有可行动的说明

---

## 2. 工程与存档

### `project.new`
新建空白工程。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| width | int | 1920 | 画布宽 |
| height | int | 1080 | 画布高 |
| fps | number | 30 | 帧率（1~120） |

```json
{"cmd": "project.new", "args": {"width": 1920, "height": 1080, "fps": 30}}
```

### `project.save`
保存当前工程。

| 参数 | 类型 | 说明 |
|---|---|---|
| path | string | 可选，给了就「另存为」 |

### `state.get`
查当前工程状态（**每次操作前先查一次是个好习惯**）。

返回 `data`：`width` / `height` / `fps` / `duration` / `track_count` /
`clip_count` / `dialogue_count` / `act_id` / `playhead` 等。

---

## 3. 轨道与剪辑

### `track.list`
列出当前幕所有轨道与 clip。**编排前必查。**

返回 `data.tracks[]`，每条含 `id` / `name` / `kind` / `hidden` / `clips[]`；
每个 clip 含 `id` / `start` / `dur` / `src_in` / `kind` / `path` /
`motion` / `expression` / `filter` / `layout`。

### `track.add`
新建轨道。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| kind | string | video | `video` 或 `audio` |
| name | string | 自动 | 轨道名（**立绘轨请用 `FIG-<角色名>`**） |
| act | string | 当前幕 | 目标幕 id |

### `clip.add`
往轨道加 clip。**这是最常用的命令。**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| path | string | — | **必填**，素材绝对路径 |
| track | string | 自动 | 轨道名或 id。不给则按素材类型自动选 |
| start | number | 0 | 起点（秒） |
| dur | number | 素材长度 | 时长（秒） |
| src_in | number | 0 | 素材内入点（秒） |
| kind | string | 按扩展名推断 | `image`/`video`/`audio`/`puppet` |
| x, y, scale | number | — | 可选，直接设布局 |

```json
{"cmd": "clip.add", "args": {"path": "D:/素材/夜空.mp4", "start": 2.0, "dur": 6.0}}
{"cmd": "clip.add", "args": {"path": "D:/素材/bgm.mp3", "start": 0, "dur": 30}}
{"cmd": "clip.add", "args": {"path": "D:/素材/素世.png", "track": "FIG-素世",
                            "start": 0, "dur": 5, "x": 0.5, "y": 0.7, "scale": 1.0}}
```

### `clip.move`
| 参数 | 说明 |
|---|---|
| id | clip id |
| start | 新起点（秒）**或** |
| delta | 相对偏移（秒） |
| track | 可选，消歧用 |

### `clip.trim`
| 参数 | 说明 |
|---|---|
| id | clip id |
| dur | 新时长 |
| src_in | 新入点 |
| track | 可选，消歧用 |

### `clip.split`
在 t 处切两段。

| 参数 | 说明 |
|---|---|
| id | clip id |
| t | 切点（秒），必须在 clip 内部 |
| track | 可选，消歧用 |

### `clip.remove`
| 参数 | 说明 |
|---|---|
| id | clip id |
| track | 可选，消歧用 |

> ⚠️ **关于 clip id 重复**：导入的旧工程里 id 可能重复（不同轨上同 id）。
> 此时 `clip.move/trim/split/remove` **不带 `track` 会明确拒绝**并告诉你
> 重复出现在哪些轨——这是防止静默改错。两个解法：
> ① 传 `track` 指定；② 先跑 `batch.run action=fix_ids` 统一修唯一。

---

## 4. 场景 / 幕 / 分支

| 命令 | 参数 | 说明 |
|---|---|---|
| `scene.set_bg` | `path` 或 `name` | 设置背景（自动建/找 BG 轨） |
| `act.list` | — | 列出所有幕 |
| `act.add` | `name` | 新增一幕（工程自动升级为多幕 v2） |
| `act.switch` | `act` | 切换当前幕 |
| `act.rename` | `act`, `name` | 改幕名 |
| `act.duplicate` | `act` | 复制一幕 |
| `act.remove` | `act` | 删除一幕（级联清引用） |
| `act.next` | `act`, `next` | 设幕尾续接目标 |
| `act.script` | `act` | 内存预览该幕脚本（不落盘） |
| `choice.set` | `act`, `cid`, `prompt`, `options[]` | 设置选择点 |
| `choice.remove` | `act`, `cid` | 删选择点 |
| `jump.set` | `act`, `t`, `when`, `target` | 条件跳转 |
| `jump.remove` | `act`, `t` | 删条件跳转 |
| `var.set` | `name`, `value` | 设工程级变量 |
| `var.list` | — | 列出变量 |

---

## 5. 立绘与角色

### `figure.add`
加一条立绘 clip。

| 参数 | 类型 | 说明 |
|---|---|---|
| path | string | **必填**，图片/木偶工程目录/Cubism2 模型目录 |
| x, y | number | 0~1 相对坐标。**不给会按同屏人数自动分位** |
| scale | number | 缩放 |
| dur | number | 时长（默认 3600） |
| start | number | 起点 |
| motion | string | 动作键名 |
| expression | string | 表情键名 |

```json
{"cmd": "figure.add", "args": {"path": "D:/素材/素世", "x": 0.35, "y": 0.7}}
```

### `figure.move`
`id`（clip id）+ `x`/`y`/`scale` 任一。

### `figure.remove`
`id` 或 `who`（角色名）。

### `motion.set` / `motion.play` / `expression.set`
| 命令 | 参数 |
|---|---|
| `motion.set` | `who` 或 `id`, `motion` |
| `motion.play` | `who` 或 `id`, `name` |
| `expression.set` | `who` 或 `id`, `expression` |

> 动作/表情键名跨角色通用（664 个 Cubism 动作共享）。
> 传 `catalog.get` 带 `kind=motion` 可列出可用键名。

### `catalog.get`
| 参数 | 说明 |
|---|---|
| kind | `motion` / `expression` / `bg` / `bgm` / `model` |

---

## 6. 台词

### `say`
| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| who | string | "" | 角色名，空 = 旁白 |
| text | string | — | **必填**，台词正文 |
| dur | number | 2.0 | 显示时长（秒） |
| t | number | 播放头 | 出现时刻 |
| motion | string | — | 本句触发的动作 |
| expression | string | — | 本句触发的表情 |

```json
{"cmd": "say", "args": {"who": "素世", "text": "为什么要演奏春日影！",
                        "dur": 2.5, "t": 3.0, "expression": "exp_angry02"}}
```

> 台词正文里如果要用 WebGAL 参数（如 `-next`），直接写在 `text` 里即可，
> 导出时会原样保留。

---

## 7. 运镜（多段路径）

### `camera.apply`
应用一段多段运镜。背景与立绘可**各录一套**（独立构图）或整体联动。

| 参数 | 类型 | 说明 |
|---|---|---|
| preset | string | 模板名（见下） |
| keyframes | array | 直接给关键帧（**优先于 preset**） |
| target | string | `both`（默认）/ `bg-main` / `fig-center` |
| t | number | 生效时刻，默认播放头 |

**可用模板**：`缓慢推进` `缓慢拉远` `向左取景` `向右取景` `向上取景`
`向下取景` `向左移动` `人物特写` `恢复全部运镜`

**关键帧格式**（引擎 v1 语义）：
```json
[
  {"duration": 0,   "scale": {"x": 1.0, "y": 1.0}},
  {"duration": 800, "scale": {"x": 1.15, "y": 1.15}},
  {"duration": 700, "scale": {"x": 1.3, "y": 1.3}}
]
```
- **第 1 帧是起点**，它的 `duration` **不计入总时长**
- 后续每帧的 `duration` 是「从上一帧过渡过来耗时（毫秒）」
- 结束后保持末帧（不回弹）
- 可用通道：`scale{x,y}` / `position{x,y}` / `rotation` / `alpha` / `blur`

```json
{"cmd": "camera.apply", "args": {"preset": "缓慢推进", "target": "both", "t": 1.0}}

{"cmd": "camera.apply", "args": {
  "target": "bg-main",
  "keyframes": [{"duration":0,"scale":{"x":1,"y":1}},
                {"duration":1200,"scale":{"x":1.25,"y":1.25},
                 "position":{"x":-80,"y":0}}]}}
```

**独立构图的写法**：分别调两次，一次 `target=bg-main`（背景路径），
一次 `target=fig-center`（人物路径）。两层互不干扰，这就是「背景推近
同时人物平移」那种效果。

返回 `data.paths[]` 里有每条生成的指令，便于你校验。

---

## 8. 氛围光效

### `light.list`
**选预设前先查这个。** 返回 13 条预设 + **能力边界**。

返回 `data`：
- `presets[]`：`name` / `desc` / `target` / `note`
- `supported[]`：能做什么
- `unsupported[]`：**不能做什么（务必读）**
- `reason`：为什么不能

### `light.apply`
| 参数 | 类型 | 说明 |
|---|---|---|
| preset | string | 预设名（**必填**） |
| t | number | 生效时刻，默认播放头 |
| layer | string | `both`（默认）/ `bg` / `fig` |

**13 条预设**

| 名称 | 效果 | 层 |
|---|---|---|
| 黄昏长影 | 低角度暖光斜照，长影与暖调 | 两层 |
| 晨曦斜照 | 清晨金色低强度光，通透 | 两层 |
| 烛火暖光 | 强烈橙红暖光，室内夜戏 | 两层 |
| 月夜银蓝 | 低饱和冷蓝，安静月光 | 两层 |
| 雨夜钴蓝 | 深冷蓝，湿冷雨夜 | 两层 |
| 舞台双束聚光 | 开体积光 + 强泛光 | 两层 |
| 舞台侧逆光 | 侧后方轮廓光，人物边缘发亮 | 两层 |
| 探照窄束 | 高对比硬光，戏剧化 | 两层 |
| 柔光箱日常 | 均匀柔和室内光 | 两层 |
| 窗边百叶光 | 窗格明暗节奏，午后 | 两层 |
| 霓虹紫光 | 赛博感紫粉光源 | 两层 |
| 灵体逆光 | 人物半透明 + 强轮廓光 | **仅立绘** |
| 恢复默认光 | 清掉所有光效 | 两层 |

```json
{"cmd": "light.list", "args": {}}
{"cmd": "light.apply", "args": {"preset": "黄昏长影", "t": 2.0}}
```

> 光效是**成对写入**的（背景一条 + 立绘一条），因为打光要让两层协调。
> 返回 `data.cmds[]` 是实际写入的指令。

---

## 9. 人物投影

### `shadow.add`
给角色加地面投影。

| 参数 | 类型 | 说明 |
|---|---|---|
| who | string | 角色名（对应 `FIG-<角色>` 轨）；不给取第一条立绘 |
| concentration | number | 影子浓度 0~1，默认按当前光效自动定 |
| softness | number | 边缘柔度 0.01~0.99，默认 0.55 |

```json
{"cmd": "shadow.add", "args": {"who": "素世", "concentration": 0.45}}
```

会做的事：
1. 生成一张柔边椭圆阴影 PNG（存到工程目录 `_shadow_<角色>.png`）
2. 建 `SHADOW-<角色>` 轨，clip 覆盖人物的时间范围
3. **把投影轨移到人物轨下面**（保证影子在人物之下）

返回 `data`：`who` / `track` / `png` / `concentration` / `softness` / `layout`

> 浓度会自动跟随最近应用的光效（暗场景影子淡、亮场景浓）。
> 先 `light.apply` 再 `shadow.add` 效果更协调。

### `shadow.remove`
| 参数 | 说明 |
|---|---|
| who | 只删这个角色的投影；不给则删全部 `SHADOW-*` 轨 |

---

## 10. 滤镜 / 特效 / 转场

### `film.filter`
电影感滤镜（16 个，成对的 bg+fig 参数）。

| 参数 | 说明 |
|---|---|
| list | `true` 列出全部（含说明） |
| name | 要套用的滤镜名 |

```json
{"cmd": "film.filter", "args": {"list": true}}
{"cmd": "film.filter", "args": {"name": "火光/爆炸"}}
```

作用范围：有选中 clip 就只作用它，否则整幕所有可见视频 clip。

### `stage.effect`
206 个演出特效（推近/震动/故障/樱花/暗角…）。

| 参数 | 说明 |
|---|---|
| list | `true` 列出分类与数量 |
| name | 特效名 |
| code | 直接给指令原文（如 `setTransform:{...} -target=bg-main;`） |

```json
{"cmd": "stage.effect", "args": {"list": true}}
{"cmd": "stage.effect", "args": {"name": "赛博朋克/电子故障风格"}}
```

### `transition.set`
全片转场（8 个引擎原生转场）。

| 参数 | 说明 |
|---|---|
| list | `true` 列出全部 |
| name | 转场名 |

---

## 11. 批量操作

### `batch.run`
一次改一批东西。

| action | 作用 | 额外参数 |
|---|---|---|
| `fix_ids` | 修 clip/轨道 id 重复或缺失 | — |
| `add_next` | 连续台词加 `-next`（播完自动继续） | `max_gap`（默认 2.5 秒） |
| `auto_exit` | 角色说完最后一句自动收尾立绘 | — |
| `retime` | 按字数（约 5.5 字/秒）重算台词时长 | `chars_per_sec` / `min_dur` / `keep_longer` |
| `apply_filter` | 批量套滤镜 | `filter`（参数字典）/ `scope`（all/fig/bg） |
| `clear_filter` | 批量清滤镜 | `scope` |

```json
{"cmd": "batch.run", "args": {"action": "fix_ids"}}
{"cmd": "batch.run", "args": {"action": "add_next"}}
{"cmd": "batch.run", "args": {"action": "retime", "keep_longer": true}}
{"cmd": "batch.run", "args": {"action": "apply_filter",
                              "filter": {"brightness": 0.85, "contrast": 1.1},
                              "scope": "fig"}}
```

返回 `data.stats` 是各动作的统计（改了几处），`data.changed` 表示是否真有改动。

> 全部是**幂等**的：跑两遍不会叠两层 `-next`、不会重复加后缀。
> 传入的工程数据不会被原地修改（内部用副本），失败不留半成品。

---

## 12. 导出

### `export.run`
| 参数 | 类型 | 说明 |
|---|---|---|
| kind | string | `video`（mp4）/ `audio`（音轨）/ `srt`（字幕）/ `webgal`（WebGAL 工程） |
| path | string | 可选，默认存到工程目录 |

```json
{"cmd": "export.run", "args": {"kind": "srt"}}
{"cmd": "export.run", "args": {"kind": "audio", "path": "D:/out/bgm.wav"}}
{"cmd": "export.run", "args": {"kind": "video", "path": "D:/out/成片.mp4"}}
```

- `video` 是**异步**的（后台线程），返回 `data.async: true`；进度看应用界面
- `audio`/`srt` 是同步的，返回时文件已就绪
- `audio` 在当前 ffmpeg 不支持目标编码时会**自动回落 WAV**，`data.path` 是实际产出路径

### `webgal.export`
导出完整 WebGAL 工程（可玩包）。

| 参数 | 说明 |
|---|---|
| dir | 输出目录 |
| playable | `true` 导出可直接双击运行的包 |

---

## 13. 素材查询

### `assets.query`
查素材库。

| 参数 | 说明 |
|---|---|
| kind | `bg` / `bgm` / `sfx` / `model` |
| q | 关键词 |
| limit | 最多返回几条 |

```json
{"cmd": "assets.query", "args": {"kind": "bg", "q": "教室", "limit": 10}}
```

### `assets.check`
体检当前工程引用到的素材是否齐全（缺几个、占位符几个）。

### `runtime.status`
查 WebGAL 引擎运行时状态（是否已导入运行时包、版本、路径）。
导出可玩包前可先查一次。

### `runtime.import`
导入 WebGAL 运行时包。

| 参数 | 说明 |
|---|---|
| path | 运行时 zip 路径 |
| url | 或给下载地址 |

---

## 14. 历史与播放

### `history.do`
| 参数 | 说明 |
|---|---|
| action | `undo` / `redo` |

**所有改动类命令都会进撤销栈**，改错了直接 undo。

### `timeline.seek`
| 参数 | 说明 |
|---|---|
| t | 跳到第几秒 |

### `timeline.play`
| 参数 | 说明 |
|---|---|
| action | `play` / `pause` / `stop` / `toggle` |
| t | 给了就直接 seek 到该时刻 |

### `snapshot`
截当前预览帧为 PNG，返回路径。**做完效果后自查用。**

---

## 15. 完整示例：从零做一段带运镜和光效的戏

```json
[
  {"cmd": "project.new", "args": {"width": 1920, "height": 1080, "fps": 30}},

  {"cmd": "clip.add", "args": {"path": "D:/素材/教室_黄昏.png",
                               "start": 0, "dur": 8}},

  {"cmd": "figure.add", "args": {"path": "D:/素材/素世", "x": 0.5, "y": 0.68,
                                 "scale": 1.0, "dur": 8}},

  {"cmd": "light.apply", "args": {"preset": "黄昏长影", "t": 0}},

  {"cmd": "shadow.add", "args": {"who": "素世"}},

  {"cmd": "camera.apply", "args": {"preset": "缓慢推进", "target": "both", "t": 0}},

  {"cmd": "say", "args": {"who": "素世", "text": "……为什么。",
                          "dur": 2.5, "t": 0.5,
                          "expression": "exp_sad01"}},

  {"cmd": "say", "args": {"who": "", "text": "（夕阳把教室染成橘色）",
                          "dur": 2.0, "t": 3.2}},

  {"cmd": "batch.run", "args": {"action": "auto_exit"}},

  {"cmd": "export.run", "args": {"kind": "srt"}},
  {"cmd": "export.run", "args": {"kind": "video"}}
]
```

---

## 16. AI 使用时必须知道的限制

### ⚠️ 灯光：不是完整布光系统

教学视频里那种「点光源/柔光箱/聚光分别调光强光色角度」**做不到**。
原因：那套功能属于 `webgal-terre` 二改引擎，而我们打包的官方 WebGAL 4.6.6
滤镜注册表只有 12 个（blur / oldFilm / dotFilm / reflectionFilm /
glitchFilm / rgbFilm / godrayFilm / shockwave / adjustment / radiusAlpha /
bevel / bloom），**不含布光与投影**。

`light.apply` 是用这四个真滤镜（bevel 边缘光 / bloom 泛光 / godrayFilm
体积光 / adjustment 调色）**拼出**的氛围效果——导出后引擎真会渲染，
但它不是可调光源。

**做不到的**（`light.list` 返回的 `unsupported` 字段里也有）：
- 多光源独立布光、光源位置 X/Y、照射范围
- 体积光束的强弱与角度（`godrayFilm` 只能开或关，参数写死 30°/0.5）
- 人物投影的**光影物理**（`shadow.add` 是贴图，不是真光照）

### ⚠️ 投影是贴图

`shadow.add` 铺的是一张柔边椭圆 PNG，不是按光照计算的影子。
好处是官方引擎真能渲染；代价是它不会随立绘动作每帧变化。

### ⚠️ 台词时长

`retime` 用「约 5.5 字/秒」估算，默认**只延长不缩短**（已有的长句可能
是故意留白）。要强制缩短传 `keep_longer: false`。

### ⚠️ clip id 可能重复

旧工程导入后 `clip id` 可能跨轨重复。此时改 clip 的命令**不带 `track`
会明确拒绝**（这是保护，不是 bug）。解法：先 `batch.run action=fix_ids`。

### ✅ 建议的操作顺序

1. `state.get` / `track.list` —— 先看现状
2. 内容（clip / figure / say）
3. 氛围（`light.apply` → `shadow.add` → `camera.apply`）
4. 收尾（`batch.run` 各 action）
5. 自查（`snapshot` 看画面）
6. 导出

### ✅ 出错了怎么办

- 任何命令 `ok: false` 时读 `msg`，里面一定有原因和可行动建议
- 改错了 `history.do undo`
- 拿不准就 `track.list` / `state.get` 再查一次现状
