# WebGAL 剧本生成 · 网页端 AI 提示词包

> **用法**：把本文件全文复制粘贴给网页端 DeepSeek（或通过附件上传本文件，
> md/txt 均可读），然后另起一句给出你的剧情文本，AI 即按规范产出
> WebGAL 场景脚本（代码块）。把代码块内容存成 `.txt`，回到木偶工作室
> 「视频编辑器 → 文件 → 导入脚本…」即可自动铺成时间轴；也可以把 txt
> 直接丢给桌面端 DSH，由它调用「script.import」导入。

## 给 AI 的指令（粘贴后自动生效，无需改写）

你是 WebGAL 场景脚本撰写助手。请把用户提供的剧情文本转换成 WebGAL 脚本：
1. 严格遵循下方《WebGAL 场景脚本编写规范》的全部格式与时间规则；
2. `-motion=` / `-expression=`：PNG 静态立绘用文末映射表 A 段的程序化 id；
   model 立绘（角色/服装目录）用映射表 B 段——可直接写素材库条目
   meta.motions 的键名（`<命名空间>/<动作名>`，如 `taki/idle01`），
   优先从推荐 21 条挑（idle/怒4/笑4/悲2/惊/正经2/思2/别/决意/哭/nf 系，
   情绪面全覆盖）；键名前缀只是来源分组，**动作跨角色通用**（taki/angry01
   也能给 anon 播）；需要更细情绪再查附件 motion_keys.json 全量 664+232；
   **只写表内真实键名，禁止自造**（规范 5.3）；
3. 用时间顺序表达流程，**不要使用 choose/branch/setVar 等分支语句**；
4. **同屏角色数无上限、立绘持续在场**（规范第 8 节，旧 3 人上限已作废）：
    角色上屏后一直留在画面，只有显式 `changeFigure:none -槽位;` 才退场，
    说话人轮替/新角色入场都不会挤走任何人；多人同屏可任意平移布置，
    群戏 4 人以上直接写，不用错峰；
5. **model 引用必须带角色前缀**（规范 5.1）：`casual-2023`、`school_*`、
   `live_*` 等服装名跨角色撞名，裸服装名会命中库序第一个角色（张冠李戴）；
   写角色的台词/动作时，changeFigure 一律用 `角色/服装` 全路径；
6. 资源路径按第 5 节约定书写（background/figure/bgm/video 子目录）；
   **文件名优先从文末「素材库清单」选真实存在的名**（配合规范 5.1 的
   tags 检索思路：先 kind，再地点/时段/mood）；库里没有的画面按
   5.1 占位命名规则自拟，避免与库内重名；
7. 对话场景默认**两名角色同屏**（说话人+听话人 left/right，规范 5.2）：
   每行只更新说话人的 -motion/-expression，听话人保持 idle 微动；
   退场是显式操作（`changeFigure:none -槽位;`），对话轮替绝不产生移除；
8. 产出的脚本用代码块输出，代码块内除场景切换注释（`;…;` 首尾分号
   合法形态）外不写解释文字。

---

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


---

## 附：素材库清单（真实可用资源，优先从中选名）

> 库根 = `asset_index.json` 所在目录；下表文件名均可在脚本里按「子目录/本名.扩展」直接引用（game 目录缺失时导入器自动回退到库）。

### 背景 bg（1053 张，按分类目录）

- `background/商店/` — 89 张（白天×11 / 夜晚×9 / 其他×69）如 `KTV包间.png`、`cafe工作区域.png`、`乐器店.png`
- `background/路边/` — 72 张（白天×19 / 夜晚×18 / 其他×35）如 `初华岛森林小道（夜晚）.jpg`、`初华岛森林小道（黄昏）.jpg`、`初华岛港口（阴天）.png`
- `background/学校、工作/羽丘/` — 59 张（白天×16 / 夜晚×12 / 其他×31）如 `仓库（夜晚）.png`、`仓库（白天）.png`、`体育仓库2.png`
- `background/演出、排练/其他演出场地/` — 59 张（白天×6 / 夜晚×8 / 其他×45）如 `MANA_美奈美_演出地.png`、`bg00165.png.png`、`bg00774.png.png`
- `background/其他背景/` — 56 张（白天×10 / 夜晚×4 / 其他×42）如 `UFO1.png`、`UFO2.png`、`中世纪广场.png`
- `background/自然风光/` — 49 张（白天×11 / 夜晚×5 / 其他×33）如 `向日葵花田.png`、`天空（放晴）.png`、`天空（明月）.png`
- `background/角色生活地点/ave mujica/` — 49 张（白天×9 / 夜晚×14 / 其他×26）如 `初华家外（夜晚）.png`、`初华家外（白天）.png`、`初华家客厅（夜晚）.png`
- `background/角色生活地点/mygo/` — 45 张（白天×8 / 夜晚×9 / 其他×28）如 `灯家客厅.jpg`、`灯家楼下天桥（夜晚）.png`、`灯家楼下天桥（黄昏）.png`
- `background/套图/` — 44 张（白天×0 / 夜晚×0 / 其他×44）如 `bg00571.png.png`、`bg00572.png.png`、`bg00573.png.png`
- `background/学校、工作/花咲川/` — 37 张（白天×10 / 夜晚×4 / 其他×23）如 `体育馆（晚上）.png`、`体育馆（白天）.png`、`教学楼后方小道.png`
- `background/公园/` — 35 张（白天×5 / 夜晚×7 / 其他×23）如 `bg00772.png.png`、`bg00952.png.png`、`万圣节树林.png`
- `background/交通/` — 34 张（白天×9 / 夜晚×7 / 其他×18）如 `丰川迈巴赫（夜晚）.jpg`、`丰川迈巴赫（黎明）.jpg`、`大巴（晚上）.png`
- `background/角色生活地点/popipa/` — 29 张（白天×9 / 夜晚×13 / 其他×7）如 `多惠卧室（晚上）.png`、`多惠卧室（白天）.png`、`多惠家客厅（晚上）.png`
- `background/演出、排练/CiRCLE/` — 27 张（白天×2 / 夜晚×3 / 其他×22）如 `CiRCLE休息室.png`、`CiRCLE休息室（花束）.png`、`CiRCLE前台（晚上）.png`
- `background/游乐园/` — 26 张（白天×6 / 夜晚×4 / 其他×16）如 `动物园1.png`、`动物园2.png`、`博物馆.png`
- `background/角色生活地点/hello happy world/` — 26 张（白天×8 / 夜晚×7 / 其他×11）如 `妮可莉娜卧室.png`、`弦卷心卧室.png`、`弦卷心家会议室.png`
- `background/角色生活地点/roselia/` — 25 张（白天×6 / 夜晚×13 / 其他×6）如 `亚子的房间（晚上）.png`、`亚子的房间（白天）.png`、`有希那客厅（晚上）.png`
- `background/学校、工作/月之森/` — 23 张（白天×8 / 夜晚×0 / 其他×15）如 `体育馆.png`、`操场.jpg`、`新园艺部.png`
- `background/演出、排练/Mujica相关/` — 23 张（白天×0 / 夜晚×1 / 其他×22）如 `G：wave化妆间大灯关.png`、`G：wave化妆间无吉他.jpg`、`G：wave化妆间有吉他.png`
- `background/桥/` — 22 张（白天×4 / 夜晚×8 / 其他×10）如 `堤坝.png`、`星象馆外廊桥（夜晚）.jpg`、`星象馆外廊桥（白天）.jpg`
- `background/体育/` — 19 张（白天×6 / 夜晚×3 / 其他×10）如 `体育场（夜晚）.png`、`体育场（白天）.png`、`体育馆走廊（晚上）.png`
- `background/游戏内/` — 19 张（白天×0 / 夜晚×0 / 其他×19）如 `bg00379.png.png`、`bg00567.png.png`、`bg00707.png.png`
- `background/学校、工作/大学/` — 18 张（白天×2 / 夜晚×1 / 其他×15）如 `四叶女子大学.png`、`四叶女子大学图书馆.png`、`四叶女子大学食堂.png`
- `background/神社/` — 18 张（白天×1 / 夜晚×1 / 其他×16）如 `bg00128.png`、`bg00129.png`、`bg00130.png`
- `background/演出、排练/RiNG/` — 16 张（白天×2 / 夜晚×2 / 其他×12）如 `Crychic练习室.png`、`RING前台视角2.jpg`、`RING门口视角2（雨）.png`
- `background/角色生活地点/afterglow/` — 14 张（白天×5 / 夜晚×6 / 其他×3）如 `兰家客厅黄昏.png`、`兰家客厅（晚上）.png`、`兰家客厅（白天）.png`
- `background/演出、排练/其他排练场地/` — 13 张（白天×1 / 夜晚×1 / 其他×11）如 `Roselia排练室.png`、`Roselia音乐事务所会议室.png`、`chuchu的录音室.png`
- `background/未分类/` — 12 张（白天×0 / 夜晚×1 / 其他×11）如 `bg00347.png`、`bg00369.png`、`bg00383.png.png`
- `background/角色生活地点/raise a suilen/` — 12 张（白天×2 / 夜晚×5 / 其他×5）如 `chuchu家泳池（晚上）.png`、`chuchu家（晚上）.png`、`chuchu家（生日会）.png`
- `background/医院/` — 11 张（白天×0 / 夜晚×0 / 其他×11）如 `儿童医院.png`、`儿童医院大厅.png`、`医院前台.png`
- `background/学校、工作/工作场所/` — 11 张（白天×2 / 夜晚×0 / 其他×9）如 `Roselia音乐事务所.png`、`体育委员会办公室（开灯）.png`、`体育委员会办公室（熄灯）.png`
- `background/角色生活地点/mofonica/` — 11 张（白天×3 / 夜晚×4 / 其他×4）如 `七深的画室（晚上）.png`、`七深的画室（深夜）.png`、`七深的画室（生日会）.png`
- `background/演出、排练/SPACE/` — 10 张（白天×3 / 夜晚×1 / 其他×6）如 `SPACE后台.png`、`SPACE（废弃夜晚）.jpg`、`SPACE（废弃白天）.jpg`
- `background/演出、排练/其他live house/` — 9 张（白天×0 / 夜晚×0 / 其他×9）如 `Galaxy拉面馆.png`、`Galaxy舞台（无演出）.png`、`Galaxy舞台（演出中）.png`
- `background/角色生活地点/pastel palettes/` — 9 张（白天×5 / 夜晚×3 / 其他×1）如 `伊芙家客厅（白天）.png`、`伊芙的房间（晚上）.png`、`伊芙的房间（白天）.png`
- `background/机场/` — 8 张（白天×0 / 夜晚×0 / 其他×8）如 `候机室.png`、`候机室2.png`、`候机室走廊.jpg`
- `background/` — 6 张（白天×0 / 夜晚×0 / 其他×6）如 `WebGAL_MyGO_Enter.webp`、`WebGAL_MyGO_Title_Image.webp`、`bg.webp`
- `background/学校、工作/` — 4 张（白天×0 / 夜晚×0 / 其他×4）如 `乐奈小学.png`、`乐奈小学教室1.png`、`乐奈小学教室2.png`
- `background/角色生活地点/` — 4 张（白天×1 / 夜晚×1 / 其他×2）如 `冰川家客厅（晚上）.png`、`冰川家客厅（白天）.png`、`千圣和花音的合租房.png`

### BGM bgm（286 首，按 mood/场景 tag）

- **flac**（23 首）：`01.祈り`、`02.番狂わせ`、`03.失楽園`、`04.忘却`、`05.ティータイム？` 等23首
- **calm**（16 首）：`11_Rain`、`11_Rain_2`、`13_Shittori`、`13_Shittori_2`、`14_Shittori_on_hanabi` 等16首
- **ban_g_dream**（14 首）：`crychic_01`、`crychic_01_vocal`、`mygo_01_bridge`、`mygo_01_bridge_2`、`mygo_01_chorus` 等14首
- **band**（14 首）：`crychic_01`、`crychic_01_vocal`、`mygo_01_bridge`、`mygo_01_bridge_2`、`mygo_01_chorus` 等14首
- **bright**（13 首）：`04_Nobiri`、`04_Nobiri_2`、`07_Comedy`、`07_Comedy_2`、`21_Enjoy` 等13首
- **serious**（9 首）：`05_Sirious`、`05_Sirious_2`、`30_Serious_2`、`30_Serious_2_2`、`45_serious_3` 等9首
- **严肃**（9 首）：`05_Sirious`、`05_Sirious_2`、`30_Serious_2`、`30_Serious_2_2`、`45_serious_3` 等9首
- **daily**（8 首）：`04_Nobiri`、`04_Nobiri_2`、`21_Enjoy`、`21_Enjoy_2`、`Enjoy` 等8首
- **tense**（8 首）：`15_Unrest`、`17_Pinch`、`59_unrest_2`、`Pinch`、`Unrest` 等8首
- **emotional**（6 首）：`10_Kandou`、`10_Kandou_2`、`Kandou(感动)`、`Kandou(感动)_2`、`Kandou(感动)_2_3` 等6首
- **rain**（6 首）：`11_Rain`、`11_Rain_2`、`41_Rain_drizzle`、`50_Rain_Umbrella`、`ショパン「雨だれ」` 等6首
- **sad**（6 首）：`13_Shittori`、`13_Shittori_2`、`14_Shittori_on_hanabi`、`Sad`、`Shittori_on_hanabi湿润-烟花背景` 等6首
- **不安**（6 首）：`15_Unrest`、`59_unrest_2`、`Unrest`、`Unrest_2`、`広がる不安` 等6首
- **悲伤**（6 首）：`13_Shittori`、`13_Shittori_2`、`14_Shittori_on_hanabi`、`Sad`、`Shittori_on_hanabi湿润-烟花背景` 等6首
- **感动**（6 首）：`10_Kandou`、`10_Kandou_2`、`Kandou(感动)`、`Kandou(感动)_2`、`Kandou(感动)_2_3` 等6首
- **雨天**（6 首）：`11_Rain`、`11_Rain_2`、`41_Rain_drizzle`、`50_Rain_Umbrella`、`ショパン「雨だれ」` 等6首
- （另有 188 首无 mood 标注，按文件名挑）

### 音效 sfx（35 条）

- `106011582-1-30216`、`1082962038-1-30232.bgm`、`1082966615-1-30232`、`1082972521-1-30232`
- `1092546529_nb3-1-30280`、`1136992030_nb3-1-30280`、`1140402403_nb3-1-30280`、`1152377866_nb3-1-30280`
- `1163123162_nb3-1-30280`、`1176977629_nb3-1-30280`、`12136340_da3-1-30232`、`1308796736-1-30280`
- `1474493970-1-30232`、`1526688938-1-30280`、`1648025303-1-30280`、`25752374765-1-30280`
- `27145603469-1-30232`、`27145603469-1-30232AA`、`27145603469-1-30232BB`、`28393672611-1-30232`
- `28460843415-1-30280`、`315686963_nb2-1-30280`、`316957110-1-30280`、`32502581500-1-30280`
- `37119480_da2-1-30216`、`564151839-1-30280`、`582962314-1-30280`、`716780759_nb2-1-30280`
- `731135920-1-30280`、`78896502-1-30216`、`A1`、`AAAA834900-1-30280`
- `到第二1`、`夜晚书`、`大雨还在下`

### 立绘 model（97 套 / 11 角色，Live2D 整合包）

- **taki**（椎名立希） — 16 套服装（arbeit、birthday_2024_ssr、casual-2023、collabo_a_ur 等）
  - motions 键名示例：`anon/angry01`、`anon/angry02`、`anon/angry03`、`anon/angry04`、`anon/bye01`
- **tomori**（高松灯） — 16 套服装（2024_furisode、birthday_2024_ssr、casual-2023、collabo_a_ur 等）
  - motions 键名示例：`anon/angry01`、`anon/angry02`、`anon/angry03`、`anon/angry04`、`anon/bye01`
- **anon**（千早爱音） — 15 套服装（birthday_2024_ssr、casual-2023、collabo_a_ur、dream_festival_3_ur 等）
  - motions 键名示例：`anon/angry01`、`anon/angry02`、`anon/angry03`、`anon/angry04`、`anon/bye01`
- **rana**（要乐奈） — 14 套服装（birthday_2024_ssr、casual-2023、collabo_a_ur、dream_festival_3_ur 等）
  - motions 键名示例：`anon/angry01`、`anon/angry02`、`anon/angry03`、`anon/angry04`、`anon/bye01`
- **soyo**（长崎素世） — 14 套服装（birthday_2024_ssr、casual-2023、collabo_a_ur、dream_festival_3_ur 等）
  - motions 键名示例：`anon/angry01`、`anon/angry02`、`anon/angry03`、`anon/angry04`、`anon/bye01`
- **uika**（三角初华） — 6 套服装（casual-2023、casual-2023_nocap、event_297_story_01、school_summer-2023 等）
  - motions 键名示例：`anon/angry01`、`anon/angry02`、`anon/angry03`、`anon/angry04`、`anon/bye01`
- **sakiko**（丰川祥子） — 5 套服装（casual-2023、event_297_story_01、jh_school_winter-2023、school_summer-2023 等）
  - motions 键名示例：`anon/angry01`、`anon/angry02`、`anon/angry03`、`anon/angry04`、`anon/bye01`
- **mutsumi**（若叶睦） — 4 套服装（casual-2023、event_297_story_01、school_summer-2023、school_winter-2023）
  - motions 键名示例：`anon/angry01`、`anon/angry02`、`anon/angry03`、`anon/angry04`、`anon/bye01`
- **umiri**（八幡海铃） — 4 套服装（casual-2023、event_297_story_01、school_summer-2023、school_winter-2023）
  - motions 键名示例：`anon/angry01`、`anon/angry02`、`anon/angry03`、`anon/angry04`、`anon/bye01`
- **nyamu**（祐天寺若麦） — 2 套服装（casual-2023、event_297_story_01）
  - motions 键名示例：`anon/angry01`、`anon/angry02`、`anon/angry03`、`anon/angry04`、`anon/bye01`
- **mana**（纯田真奈） — 1 套服装（sumimi）
  - motions 键名示例：`anon/angry01`、`anon/angry02`、`anon/angry03`、`anon/angry04`、`anon/bye01`

---

> **全量清单**（本表只是骨架）：连 DSH 用 `{"cmd":"assets.query","args":{"kind":"bg","tags":["夜晚"],"limit":30}}`，或直接读索引 `D:\work\puppet\webgal_games\asset_library\asset_index.json`（UTF-8 JSON 数组，字段 id/kind/file/name/tags/meta）。


---

## 附：动作 / 表情映射表（与 motion_table.json 实时同步）

### A. 程序化预置（PNG 静态立绘 / puppet 工程用 `-motion=` id）

| id | 名称 | 时长(s) | 循环 |
|---|---|---|---|
| `idle` | 待机呼吸 | 4.0 | 是 |
| `blink` | 眨眼 | 2.0 | 是 |
| `nod` | 点头 | 2.0 | 是 |
| `shake_head` | 摇头 | 2.0 | 是 |
| `wave` | 挥手 | 2.0 | 是 |
| `shrug` | 耸肩 | 2.0 | 是 |
| `bow` | 鞠躬 | 3.0 | 是 |

| id | 表情 | 过渡(s) |
|---|---|---|
| `smile` | 微笑 | 0.4 |
| `sad` | 难过 | 0.5 |
| `angry` | 生气 | 0.3 |
| `surprised` | 惊讶 | 0.25 |
| `shy` | 害羞 | 0.4 |

### B. 模型动作库（model 立绘用；跨角色通用，任意命名空间可配任意模型）

**推荐 21 条（先从这里挑，情绪面已覆盖）**：

| 动作键 | 含义 | | 表情键示例 | 含义 |
|---|---|---|---|---|
| `anon/idle01` | 待机01 | | `anon/surprised01` | 惊讶01 |
| `anon/angry01` | 生气01 | | `anon/serious01` | 严肃01 |
| `anon/angry02` | 生气02 | | `anon/serious02` | 严肃02 |
| `anon/angry03` | 生气03 | | `anon/thinking01` | 思考01 |
| `anon/angry04` | 生气04 | | `anon/thinking02` | 思考02 |
| `anon/smile01` | 微笑01 | | `anon/bye01` | 再见01 |
| `anon/smile02` | 微笑02 | | `anon/kime01` | 决意01 |
| `anon/smile03` | 微笑03 | | `anon/cry01` | 哭泣01 |
| `anon/smile04` | 微笑04 | | `anon/nf01` | nf01 |
| `anon/sad01` | 难过01 | | `anon/nnf03` | nnf03 |
| `anon/sad02` | 难过02 | | | |

> 键名前缀（命名空间）只是来源分组：`taki/angry01` 也能给爱音的模型播。把前缀换成台词角色自己的名字读起来最自然（如 soyo 的戏用 `soyo/smile02`）。

**命名空间 ↔ 角色对照**（共 664 动作 / 232 表情，全量键名见附件 `motion_keys.json` 或 `motion_table.json` 的 cubism 条目）：

| 命名空间 | 角色 | 动作数 | 表情数 |
|---|---|---|---|
| `anon` | 千早爱音 | 44 | 28 |
| `mana` | 纯田真奈 | 27 | 9 |
| `mutsumi` | 若叶睦 | 85 | 18 |
| `nyamu` | 祐天寺若麦 | 61 | 13 |
| `rana` | 要乐奈 | 36 | 19 |
| `sakiko` | 丰川祥子 | 107 | 29 |
| `soyo` | 长崎素世 | 59 | 33 |
| `taki` | 椎名立希 | 44 | 30 |
| `tomori` | 高松灯 | 100 | 30 |
| `uika` | 三角初华 | 55 | 11 |
| `umiri` | 八幡海铃 | 46 | 12 |

> **规则**：`-motion=` / `-expression=` 只认表内真实键名（未知键导出记警告并跳过）；推荐层不够细再翻 motion_keys.json 全量 664+232；**禁止自造键名**（想要更强烈的情绪就换相邻真实编号，如 angry02/angry03）。

> 自定义动作/表情：编辑 motion_table.json 追加条目即可（重启或等待自动加载），本包与规范中的清单以该文件为准。
