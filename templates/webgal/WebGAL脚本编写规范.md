# WebGAL 场景脚本编写规范

> 本文档是**给网页端 AI 当提示词材料**的格式说明书。请严格按本规范生成
> WebGAL 场景脚本文本（纯文本 `.txt`），再由视频编辑器「文件 → 导入脚本…」
> 导入成时间轴工程。**只支持标准 WebGAL**，不含任何方言专属内容。

## 1. 行格式（三种）

脚本由若干行组成，**每行一条语句，以英文分号 `;` 结尾**：

| 类型 | 写法 | 说明 |
|---|---|---|
| 命令行 | `命令:参数 -选项;` | 执行场景动作 |
| 台词行 | `名字:内容;` | 角色/旁白说话 |
| 注释行 | `;说明文字;` | 首尾都带分号，体内不用 ASCII 分号/冒号（会被引擎切句错乱）。旧式 `//xxx` 导入端仍兼容（自动转换），但**直接进引擎的脚本禁用**——`//` 行会被 4.6.6 渲染成台词 |

空行允许。**命令名以白名单判定**：见第 2 节的「支持命令」。凡是含冒号、
但命令名不在该白名单里的行，一律按**台词行**处理。

## 2. 支持命令表

### 2.1 支持的命令（会真正生成时间轴）

| 命令 | 参数写法 | 生成效果 |
|---|---|---|
| `changeBg` | `changeBg:图片路径 -next;` | 切换背景；新背景从当前时刻起，上一背景到此结束 |
| `changeFigure` | `changeFigure:图片路径 -left/-center/-right;` | 显示立绘；同槽位遇 `changeFigure:none` 或脚本结束消失 |
| `changeFigure` | `changeFigure:none -left/-center/-right;` | 结束该槽位当前立绘 |
| `bgm` | `bgm:音频路径;` | 背景音乐；新段自动替换前一段 |
| `playEffect` | `playEffect:音频路径;` | 音效，固定时长 2 秒 |
| `video` | `video:视频路径 -next;` | 播放视频，固定时长 5 秒 |
| `intro` | `intro:标题\|副标题;` | 开场介绍，按台词规则占时间 |

`changeFigure` 的 `-motion=` / `-expression=` 选项（可选，写在路径之后）：

```
changeFigure:figure/角色.png -left -motion=idle -expression=smile;
```

### 2.2 会被忽略的语句（写不写都行，导入时跳过并记警告，**不会报错**）

`choose`、`branch`、`label`、`jumpLabel`、`callScene`、`changeScene`、
`setVar`、`unlockCg`、`unlockBgm`、`setTextbox`

> 分支与变量**不会落地**到时间轴，请在脚本里用时间顺序表达流程，不要用分支。

## 3. 槽位与动作映射表

### 3.1 立绘槽位 -> 水平位置

| 选项 | layout x | 说明 |
|---|---|---|
| `-left` | `0.2` | 画面左侧 |
| `-center` | `0.5` | 画面中央（缺省） |
| `-right` | `0.8` | 画面右侧 |

立绘统一：`y = 0.7`，`scale = 0.6`。每个立绘**独立一轨**，互不影响。

### 3.2 motion / expression 的合法 id

**以 `templates/webgal/motion_table.json` 为准。写之前先读该文件**：

- 动作 id：读 `motions[].id`
- 表情 id：读 `expressions[].id`

当前合法值（如该文件更新，以文件为准）：

- `motions[].id`：`idle`、`blink`、`nod`、`shake_head`、`wave`、`shrug`、`bow`
- `expressions[].id`：`smile`、`sad`、`angry`、`surprised`、`shy`

不在表里的 id 会被忽略（不报错）。

## 4. 时间估算规则

导入器用一条**时间游标**把离散脚本折成连续时间轴，请按此预估节奏、
按语义分段，让每段时长符合预期：

| 语句 | 时间推进 |
|---|---|
| 台词 / `intro` | `t += max(2.5, 0.4 × 内容字符数)` |
| `video:` | `t += 5.0` |
| `changeBg` | 新背景起点 = 当前 t；上一背景终点 = t |
| `changeFigure` | 立绘起点 = 当前 t；`none` 或脚本结束终止 |
| `bgm:` | 新段起点 = 当前 t；上一 bgm 段截到 t |
| `playEffect:` | 固定占 2 秒（不额外推进游标） |

- 台词时长下限 **2.5 秒**，每字 **0.4 秒**（按内容字符数计）。
- 语句按出现顺序排列；同刻的先后建议：先背景、再立绘、再音频、再台词。
- 脚本结束时刻即为工程总时长，所有未结束的片段自动收尾。

## 5. 资源路径约定

资源路径**相对 WebGAL 游戏目录（`game/`）**解析，可用以下子目录：

| 目录 | 用途 | 支持格式 |
|---|---|---|
| `background/` | 背景图 | `png` / `webp` / `jpg` |
| `figure/` | 立绘图 | `png` / `webp` / `jpg` |
| `bgm/` | 背景音乐、音效 | `mp3` / `wav` |
| `video/` | 视频 | `mp4` |

路径示例：`changeBg:background/天空.png -next;`、
`bgm:bgm/主题.mp3;`、`video:video/过场.mp4 -next;`。
请确保文件真实存在，缺失文件只会记警告，不会中断导入。

### 5.1 素材库（整合包资源，优先从中选名）

本机有一份预构建素材库（`build_asset_library.py` 从整合包扫描生成，
约 1400 条，索引 `asset_index.json`）。导入资源按**两级**查找：

1. **游戏目录（`game/`）**：脚本写的相对路径直接找，找到即用（现行默认）；
2. 找不到时**素材库回退**：把引用路径去掉目录和扩展名，拿文件名匹配库内
   条目的 name/文件名（精确 > 大小写不敏感 > 去空格标点安全化），命中即
   直接用该素材的本地绝对路径，导入报告记「素材库回退命中 N」——这是
   信息不是警告。

**选素材思路（给 AI 的检索路线）**：
- 先定 kind：`bg` 背景 / `bgm` 音乐 / `sfx` 音效 / `model` 立绘（Live2D）/ `video`；
- 再用 tags 收窄：先地点/场景（学校、交通、公园…），再时段（白天/夜晚，
  中英等价，如 `night`=`夜晚`），音乐类再加 mood（`sad`/`悲伤`、`calm`/`平静`…）；
  多个 tags 是**全部命中**语义；
- 连着 DSH 时直接查（返回的 `name`/`file` 就是可用素材）：

  ```json
  {"cmd": "assets.query", "args": {"kind": "bg", "tags": ["学校", "白天"], "limit": 8}}
  ```

  没连 DSH 就直读索引 JSON（绝对路径见提示词包「素材库清单」节尾）。
- 脚本里写命中素材**本名**、带原分类子目录，如
  `changeBg:background/教室（白天）.png -next;`。

**model（Live2D 角色）写法**：路径写「角色/服装」目录名（带不带
`/model.json` 后缀都行，导入端等价）：

```
changeFigure:figure/anon/school_summer-2023 -left -motion=anon/idle01 -expression=anon/smile;
```

导出时自动带上 model.json 与整套角色资源。model 立绘的 `-motion=` /
`-expression=` **可直接写素材库该条目 `meta.motions` / `meta.expressions`
里的键名**（`角色/动作` 形式，如 `taki/idle01`），导出时精确匹配落地；
写 3.2 节的预置 id（`idle`、`smile` 等）同样有效（走 motion_table
通道，PNG 静态立绘用这套）。

### 5.2 每句配动作：对话场景默认两名角色同屏（推荐模式，task-47）

用户玩法规约：**对话场景默认两名角色同屏**——说话人+听话人，
`-left` / `-right` 各占一位（第三槽 `-center` 留给中途插入的角色/
第三人反应）。每行台词只更新**说话人**的动作表情，听话人保持
`idle` 微动即可：

```
changeFigure:figure/taki/live_sr_01/model.json -left -motion=taki/idle01;
changeFigure:figure/anon/school_summer-2023/model.json -right -motion=anon/idle01;
立希:……（说话，带情绪动作）
changeFigure:figure/taki/live_sr_01/model.json -left -motion=taki/angry01 -expression=taki/angry01;
立希:别发呆了，考试时间不多了！
changeFigure:figure/anon/school_summer-2023/model.json -right -motion=anon/bye01 -expression=anon/smile;
爱音:抱歉抱歉，马上回来。
```

要点：
- 同一角色重复 `changeFigure` 是**更新动作/表情**（引擎原生支持，导入
  端切成新片段），不是重新入场，更不是移除；
- 每行只动说话人：听话人的行可以省略（保持他上屏时的状态），写则只更
  新他（比如被点到时的反应）；
- 纯旁白/独白场景可以单独立绘或不上立绘；
- **退场是显式操作**：只有剧情明确让角色离开，才写
  `changeFigure:none -槽位;`；对话轮替绝不产生移除（详见第 8 节驻场规约）。

**缺素材时的占位命名规则**：库里也没有想要的画面时，在对应子目录下按
「场景（时段或状态）」风格自拟文件名（如 `background/屋顶（黄昏）.png`），
**避免与库内条目重名**（重名会回退命中不相干的图）；缺文件只记警告不
中断，之后把同名图片放进 `game/background/` 重新导入即可落地。

**服装名跨角色撞名必须带角色前缀**：`casual-2023`、`school_*-2023`、
`live_*` 这类服装名是多个角色共有的，裸名解析按素材库序命中**第一个**
角色（如 anon）——素世的戏写裸名会出爱音的立绘。引用 model 必须写
`角色/服装` 或 `角色/服装/model.json` 全路径；**裸服装名视为指向库序
第一个，剧本禁用**。

### 5.3 动作/表情键名：跨角色共享库（K4）

model 立绘的 `-motion=` / `-expression=` 键名来自整合包**共享动作库**：
共 **664 个动作 + 232 个表情**，键名形如 `<命名空间>/<动作名>`
（如 `anon/angry01`）。

- **跨角色通用**：命名空间只是键名的分组来源，**不限制用法**——
  `taki/angry01` 同样可以给爱音（anon）的模型播；每个模型都自带全量
  动作/表情文件（已实测 664/232 全部存在）。
- **命名空间 ↔ 角色对照**：anon=千早爱音、mana=纯田真奈、mutsumi=若叶睦、
  nyamu=祐天寺若麦、rana=要乐奈、sakiko=丰川祥子、soyo=长崎素世、
  taki=椎名立希、tomori=高松灯、uika=三角初华、umiri=八幡海铃。
- **优先用推荐层**（21 条，情绪面全覆盖）：
  `idle01`、`angry01`~`angry04`、`smile01`~`smile04`、`sad01`、`sad02`、
  `surprised01`、`serious01`、`serious02`、`thinking01`、`thinking02`、
  `bye01`、`kime01`、`cry01`、`nf01`、`nnf03`——前缀任意命名空间均可
  （如 `anon/idle01`、`soyo/smile02`、`taki/angry01`）。
- 推荐层不够细再查全表：附件 `motion_keys.json`（664+232 全量键名，
  按命名空间排序），或 `motion_table.json` 的 `kind=cubism*` 条目。
- **不要自造键名**：`-motion=` 只认表内真实键名（未知键导出会记警告
  并跳过该参数）。想要"更生气一点"就换 `angry02`/`angry03` 这类真实
  键，而不是发明 `angry08`。
- PNG 静态立绘 / puppet 工程仍用 3.2 节预置 id（`idle`/`nod`/`smile` 等
  程序化动作，与 cubism 键名两套通道并存）。

## 6. 完整示例脚本

```
;开场：天空背景 + 左侧立绘;
changeBg:background/天空.png -next;
changeFigure:figure/角色A.png -left -motion=idle -expression=smile;
旁白:这是故事的开始，天空晴朗，微风拂面。
角色A:你好，欢迎来到这里。
changeFigure:figure/角色B.png -right -motion=idle -expression=sad;
角色B:我等了很久，终于等到你了。
bgm:bgm/主题.mp3;
旁白:背景音乐响起，故事正式展开。
角色B:接下来，请听我说。
playEffect:bgm/提示音.wav;
video:video/过场.mp4 -next;
changeBg:background/黄昏.png -next;
角色B:天色已晚，我们先休息吧。
changeFigure:none -left;
changeFigure:none -right;
```

> 注意：`changeFigure:none -槽位;` 是**显式退场**，只在剧情真的让角色离场时写；
> 台词/动作/表情更新绝不附带移除（详见 5.2 驻场规约）。

## 7. 限制说明

- **分支/变量不落地**：`choose`/`branch`/`label`/`jumpLabel`/`callScene`/
  `changeScene`/`setVar` 等语句导入时跳过并记警告，请改用时间顺序表达流程。
- **同轨不重叠**：导入器自动保证同一轨内片段不重叠；相邻背景会首尾相接，
  不必手工排版时间。
- **未闭合片段**：脚本结束时，背景/立绘/bgm 若未显式结束，会自动收尾到
  脚本末尾时刻。
- **只支持标准 WebGAL**：不要写任何方言专属命令或滤镜语法。

## 8. 驻场与同屏规约（工作室规约，K4 更新）

同屏角色数**没有上限**（WebGAL 引擎本身 6 槽位；旧「最多 3 人」规约
已作废）：群戏可以让 4 人及以上同时在屏。

- **立绘持续在场**：角色一旦上屏就保持在场，直到显式写
  `changeFigure:none -槽位;` 退场——引擎与导入链**不会**因为说话人
  轮替、新角色入场或时间推进而自动移除任何角色；
- **可任意平移**：多人同屏时立绘位置/缩放自由布置（工作室时间轴侧
  每个立绘片段支持任意 x/y/scale/rot，导出 WebGAL 时按槽位与变换还原），
  不必挤在 left/center/right 三槽的旧心智里；
- 对话场景仍**推荐**两名角色同屏的节奏（5.2 节）——那是叙事建议，
  不是限制；第三人插入、群戏反应镜头都合法。

## 9. 导出包视觉风格（AI 无需感知，纯说明）

木偶工作室导出的可玩包默认注入 MyGO 风字幕皮肤：**台词无底框**（大号
白字黑描边直接压在画面上）、**说话人=蓝色圆角胶囊名牌**（台词框左上）、
右上角引擎水印；非可玩内容包以 `game/style/muppet.css` 交付（手动 link
生效）。这一切由导出器自动完成，**脚本里不需要写任何样式语句**，也不
要用 `setVar`/自定义命令尝试控制外观。关闭方式：桌面端导出参数
`style=False`，或 DSH `webgal.export` 的 `args.style=false`。

## 10. Live2D 运行前提（AI 无需感知，纯说明）

官方 WebGAL 运行时因专有许可**不自带 Live2D core**（live2d.min.js /
live2dcubismcore.min.js）。导出含 Live2D 立绘（model 条目）的可玩包时，
导出器会自动从用户本机找到这两个 js 并注入导出包（拷贝进 `lib/` +
在引擎脚本前设置 `window.live2dPromise`），人物即可正常显示与播放动作。
若用户本机没有 Live2D core（从未装过整合包/二创引擎），导出会带警告
提示设置环境变量 `MUPPET_LIVE2D_CORE_DIR` 指向含这两个 js 的目录。
脚本层面**无需任何特殊写法**：按第 5.1/5.2 节正常引用 model 立绘即可。

**运行方式**：官方 4.6.6 是 ES module 构建，浏览器 `file://` 直开
index.html 会黑屏——**双击导出包根目录的「启动游戏」**（exe 或 bat）
→ 自动起本地服务并打开浏览器。分发时把整个导出包文件夹一起给用户。
