# WebGAL 剧本生成 · 网页端 AI 提示词包

> **用法**：把本文件 + 同目录 `WebGAL脚本编写规范.md` **两个附件一起上传**
> 给网页端 DeepSeek（md/txt 均可读），然后另起一句给出你的剧情文本，
> AI 即按规范产出 WebGAL 场景脚本（代码块）。把代码块内容存成 `.txt`，
> 回到木偶工作室「视频编辑器 → 文件 → 导入脚本…」即可自动铺成时间轴；
> 也可以把 txt 直接丢给桌面端 DSH，由它调用「script.import」导入。
>
> **为什么是两个文件**：规范是可执行语法（行格式/命令表/时间规则/路径约定），
> 本包是"给 AI 的指令 + 真实可用数据"（素材清单、动作表情键名）。
> 两者分开维护，避免同一段内容两处各存一份、改一处漏一处。
> **只喂本文件而不喂规范 = AI 不知道语法，产出不可用。**

## 给 AI 的指令（粘贴后自动生效，无需改写）

你是 WebGAL 场景脚本撰写助手。请把用户提供的剧情文本转换成 WebGAL 脚本：
0. **先读附件《WebGAL 场景脚本编写规范》**——下文凡写"规范 X 节"
   均指该附件；**本包不再重复规范正文**。规范目录：1. 行格式（三种）、2. 支持命令表、3. 槽位与动作映射表、4. 时间估算规则、5. 资源路径约定、6. 完整示例脚本、7. 限制说明、8. 驻场与同屏规约（工作室规约，K4 更新）、9. 导出包视觉风格（AI 无需感知，纯说明）、10. Live2D 运行前提（AI 无需感知，纯说明）。
1. 严格遵循规范的**全部**格式与时间规则；
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
   **文件名必须从文末「素材库清单」逐字照抄**（规范 5.1.1 铁律）：
   - 一个字都不许改 —— 全角括号就是全角 `（）`，库写「晚上」就不能写
     「夜晚」，库写 `RiNG` 就不能写 `RING`，扩展名 `.png` 不能写成 `.jpg`；
   - 正文列的是**完整路径**（含分类子目录），整条抄，别只留文件名；
   - **不许"意思差不多"**：库只有「（雨）」版就写「（雨）」，只有「黄昏」
     就别写「傍晚」；**禁止自造形近名**（写成「素世家客厅（夜晚）」这种
     近义改写 = 必然缺素材，导入报告会报「背景缺 N」）；
   - 库里**确实没有**的画面，用占位命名 `background/占位_<场景描述>.png`
     （必须带 `占位_` 前缀，否则会被判成真缺失并导出时跳过）；
7. 对话场景默认**两名角色同屏**（说话人+听话人 left/right，规范 5.2）：
   每行只更新说话人的 -motion/-expression，听话人保持 idle 微动；
   退场是显式操作（`changeFigure:none -槽位;`），对话轮替绝不产生移除；
8. **默认不写画面特效**。`setTransform`（滤镜）/`setTempAnimation`（运镜）
   是 MyGO 二创引擎的扩展指令，**官方 WebGAL 4.6.6 不保证支持**。只在
   用户明确要求特效、且确认目标引擎支持时才用——届时查附件
   WebGAL演出特效参考.md（用法速查）；221 条实码在仓库
   `templates/webgal/effect_table.json`（体积超限未随包附带）；
9. 产出的脚本用代码块输出，代码块内除场景切换注释（`;…;` 首尾分号
   合法形态）外不写解释文字。

---

## 附：素材库清单（真实可用资源，优先从中选名）

> 库根 = `asset_index.json` 所在目录；下表文件名均可在脚本里按「子目录/本名.扩展」直接引用（game 目录缺失时导入器自动回退到库）。

### 背景 bg（1053 张，按分类目录）

> **抄写规则**：每行第一个反引号是目录，后面样例是**完整路径**。同目录里没列出的文件，路径 = 「该行目录 + 文件名」，**文件名必须与库里完全一致（全角括号/时段措辞/大小写/扩展名都不许改）**。

- `background/商店/` — 89 张（白天11 / 夜晚9） 如 `KTV包间.png`、`cafe工作区域.png`、`乐器店.png`
- `background/路边/` — 72 张（白天19 / 夜晚18） 如 `初华岛森林小道（夜晚）.jpg`、`初华岛森林小道（黄昏）.jpg`、`初华岛港口（阴天）.png`
- `background/学校、工作/羽丘/` — 59 张（白天16 / 夜晚12） 如 `仓库（夜晚）.png`、`仓库（白天）.png`、`体育仓库2.png`
- `background/演出、排练/其他演出场地/` — 59 张（白天6 / 夜晚8） 如 `MANA_美奈美_演出地.png`、`bg00165.png.png`、`bg00774.png.png`
- `background/其他背景/` — 56 张（白天10 / 夜晚4） 如 `UFO1.png`、`UFO2.png`、`中世纪广场.png`
- `background/自然风光/` — 49 张（白天11 / 夜晚5） 如 `向日葵花田.png`、`天空（放晴）.png`、`天空（明月）.png`
- `background/角色生活地点/ave mujica/` — 49 张（白天9 / 夜晚14） 如 `初华家外（夜晚）.png`、`初华家外（白天）.png`、`初华家客厅（夜晚）.png`
- `background/角色生活地点/mygo/` — 45 张（白天8 / 夜晚9） 如 `灯家客厅.jpg`、`灯家楼下天桥（夜晚）.png`、`灯家楼下天桥（黄昏）.png`
- `background/套图/` — 44 张 如 `bg00571.png.png`、`bg00572.png.png`、`bg00573.png.png`
- `background/学校、工作/花咲川/` — 37 张（白天10 / 夜晚4） 如 `体育馆（晚上）.png`、`体育馆（白天）.png`、`教学楼后方小道.png`
- `background/公园/` — 35 张（白天5 / 夜晚7） 如 `bg00772.png.png`、`bg00952.png.png`、`万圣节树林.png`
- `background/交通/` — 34 张（白天9 / 夜晚7） 如 `丰川迈巴赫（夜晚）.jpg`、`丰川迈巴赫（黎明）.jpg`、`大巴（晚上）.png`
- `background/角色生活地点/popipa/` — 29 张（白天9 / 夜晚13） 如 `多惠卧室（晚上）.png`、`多惠卧室（白天）.png`、`多惠家客厅（晚上）.png`
- `background/演出、排练/CiRCLE/` — 27 张（白天2 / 夜晚3） 如 `CiRCLE休息室.png`、`CiRCLE休息室（花束）.png`、`CiRCLE前台（晚上）.png`
- `background/游乐园/` — 26 张（白天6 / 夜晚4） 如 `动物园1.png`、`动物园2.png`、`博物馆.png`
- `background/角色生活地点/hello happy world/` — 26 张（白天8 / 夜晚7） 如 `妮可莉娜卧室.png`、`弦卷心卧室.png`、`弦卷心家会议室.png`
- `background/角色生活地点/roselia/` — 25 张（白天6 / 夜晚13） 如 `亚子的房间（晚上）.png`、`亚子的房间（白天）.png`、`有希那客厅（晚上）.png`
- `background/学校、工作/月之森/` — 23 张（白天8） 如 `体育馆.png`、`操场.jpg`、`新园艺部.png`
- `background/演出、排练/Mujica相关/` — 23 张（夜晚1） 如 `G：wave化妆间大灯关.png`、`G：wave化妆间无吉他.jpg`、`G：wave化妆间有吉他.png`
- `background/桥/` — 22 张（白天4 / 夜晚8） 如 `堤坝.png`、`星象馆外廊桥（夜晚）.jpg`、`星象馆外廊桥（白天）.jpg`
- `background/体育/` — 19 张（白天6 / 夜晚3） 如 `体育场（夜晚）.png`、`体育场（白天）.png`、`体育馆走廊（晚上）.png`
- `background/游戏内/` — 19 张 如 `bg00379.png.png`、`bg00567.png.png`、`bg00707.png.png`
- `background/学校、工作/大学/` — 18 张（白天2 / 夜晚1） 如 `四叶女子大学.png`、`四叶女子大学图书馆.png`、`四叶女子大学食堂.png`
- `background/神社/` — 18 张（白天1 / 夜晚1） 如 `bg00128.png`、`bg00129.png`、`bg00130.png`
- `background/演出、排练/RiNG/` — 16 张（白天2 / 夜晚2） 如 `Crychic练习室.png`、`RING前台视角2.jpg`、`RING门口视角2（雨）.png`
- `background/角色生活地点/afterglow/` — 14 张（白天5 / 夜晚6） 如 `兰家客厅黄昏.png`、`兰家客厅（晚上）.png`、`兰家客厅（白天）.png`
- `background/演出、排练/其他排练场地/` — 13 张（白天1 / 夜晚1） 如 `Roselia排练室.png`、`Roselia音乐事务所会议室.png`、`chuchu的录音室.png`
- `background/未分类/` — 12 张（夜晚1） 如 `bg00347.png`、`bg00369.png`、`bg00383.png.png`
- `background/角色生活地点/raise a suilen/` — 12 张（白天2 / 夜晚5） 如 `chuchu家泳池（晚上）.png`、`chuchu家（晚上）.png`、`chuchu家（生日会）.png`
- `background/医院/` — 11 张 如 `儿童医院.png`、`儿童医院大厅.png`、`医院前台.png`
- `background/学校、工作/工作场所/` — 11 张（白天2） 如 `Roselia音乐事务所.png`、`体育委员会办公室（开灯）.png`、`体育委员会办公室（熄灯）.png`
- `background/角色生活地点/mofonica/` — 11 张（白天3 / 夜晚4） 如 `七深的画室（晚上）.png`、`七深的画室（深夜）.png`、`七深的画室（生日会）.png`
- `background/演出、排练/SPACE/` — 10 张（白天3 / 夜晚1） 如 `SPACE后台.png`、`SPACE（废弃夜晚）.jpg`、`SPACE（废弃白天）.jpg`
- `background/演出、排练/其他live house/` — 9 张 如 `Galaxy拉面馆.png`、`Galaxy舞台（无演出）.png`、`Galaxy舞台（演出中）.png`
- `background/角色生活地点/pastel palettes/` — 9 张（白天5 / 夜晚3） 如 `伊芙家客厅（白天）.png`、`伊芙的房间（晚上）.png`、`伊芙的房间（白天）.png`
- `background/机场/` — 8 张 如 `候机室.png`、`候机室2.png`、`候机室走廊.jpg`
- `background/` — 6 张 如 `WebGAL_MyGO_Enter.webp`、`WebGAL_MyGO_Title_Image.webp`、`bg.webp`
- `background/学校、工作/` — 4 张 如 `乐奈小学.png`、`乐奈小学教室1.png`、`乐奈小学教室2.png`
- `background/角色生活地点/` — 4 张（白天1 / 夜晚1） 如 `冰川家客厅（晚上）.png`、`冰川家客厅（白天）.png`、`千圣和花音的合租房.png`

完整路径范本（照此格式写 `changeBg:background/.../文件名.png -next;`）：

- `background/WebGAL_MyGO_Enter.webp`
- `background/交通/丰川迈巴赫（夜晚）.jpg`
- `background/体育/体育场（夜晚）.png`
- `background/公园/bg00772.png.png`
- `background/其他背景/UFO1.png`
- `background/医院/儿童医院.png`
- `background/商店/KTV包间.png`
- `background/套图/bg00571.png.png`

### BGM bgm（286 首，按 mood/场景 tag）

- **calm**（16 首）：`11_Rain`、`11_Rain_2`、`13_Shittori`、`13_Shittori_2`、`14_Shittori_on_hanabi` 等16首
- **ban_g_dream**（别名：band）（14 首）：`crychic_01`、`crychic_01_vocal`、`mygo_01_bridge`、`mygo_01_bridge_2`、`mygo_01_chorus` 等14首
- **bright**（13 首）：`04_Nobiri`、`04_Nobiri_2`、`07_Comedy`、`07_Comedy_2`、`21_Enjoy` 等13首
- **serious**（别名：严肃）（9 首）：`05_Sirious`、`05_Sirious_2`、`30_Serious_2`、`30_Serious_2_2`、`45_serious_3` 等9首
- **daily**（8 首）：`04_Nobiri`、`04_Nobiri_2`、`21_Enjoy`、`21_Enjoy_2`、`Enjoy` 等8首
- **tense**（8 首）：`15_Unrest`、`17_Pinch`、`59_unrest_2`、`Pinch`、`Unrest` 等8首
- **emotional**（别名：感动）（6 首）：`10_Kandou`、`10_Kandou_2`、`Kandou(感动)`、`Kandou(感动)_2`、`Kandou(感动)_2_3` 等6首
- **rain**（别名：雨天）（6 首）：`11_Rain`、`11_Rain_2`、`41_Rain_drizzle`、`50_Rain_Umbrella`、`ショパン「雨だれ」` 等6首
- **sad**（别名：悲伤）（6 首）：`13_Shittori`、`13_Shittori_2`、`14_Shittori_on_hanabi`、`Sad`、`Shittori_on_hanabi湿润-烟花背景` 等6首
- **不安**（6 首）：`15_Unrest`、`59_unrest_2`、`Unrest`、`Unrest_2`、`広がる不安` 等6首
- **comedy**（别名：搞笑）（5 首）：`07_Comedy`、`07_Comedy_2`、`Comedy`、`Comedy_2`、`喜剧-混乱`
- **日常**（5 首）：`04_Nobiri`、`04_Nobiri_2`、`Nobiri(悠闲)`、`Nobiri(悠闲)_2`、`Normal`
- **night**（别名：夜晚）（4 首）：`38_GoodNight`、`38_GoodNight_2`、`46_GoodNight_normal`、`GoodNight`
- **ending**（别名：结尾）（3 首）：`09_Ending`、`09_Ending_2`、`Ending`
- **pre-live**（别名：演出前）（3 首）：`23_BeforeLive`、`BeforeLive`、`BeforeLive_2`
- **愉快**（3 首）：`21_Enjoy`、`21_Enjoy_2`、`Enjoy`
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
- **tomori**（高松灯） — 16 套服装（2024_furisode、birthday_2024_ssr、casual-2023、collabo_a_ur 等）
- **anon**（千早爱音） — 15 套服装（birthday_2024_ssr、casual-2023、collabo_a_ur、dream_festival_3_ur 等）
- **rana**（要乐奈） — 14 套服装（birthday_2024_ssr、casual-2023、collabo_a_ur、dream_festival_3_ur 等）
- **soyo**（长崎素世） — 14 套服装（birthday_2024_ssr、casual-2023、collabo_a_ur、dream_festival_3_ur 等）
- **uika**（三角初华） — 6 套服装（casual-2023、casual-2023_nocap、event_297_story_01、school_summer-2023 等）
- **sakiko**（丰川祥子） — 5 套服装（casual-2023、event_297_story_01、jh_school_winter-2023、school_summer-2023 等）
- **mutsumi**（若叶睦） — 4 套服装（casual-2023、event_297_story_01、school_summer-2023、school_winter-2023）
- **umiri**（八幡海铃） — 4 套服装（casual-2023、event_297_story_01、school_summer-2023、school_winter-2023）
- **nyamu**（祐天寺若麦） — 2 套服装（casual-2023、event_297_story_01）
- **mana**（纯田真奈） — 1 套服装（sumimi）

> **动作/表情键名跨角色通用**：下面这 40 个键名可给**任何**角色播（`taki/angry01` 也能给 anon 用）；键名前缀只是来源分组，不代表只能该角色用。完整 664+232 条见附件 `motion_keys.json`，日常写脚本用推荐 21 条即可。

- 共享键名示例：`anon/angry01`、`anon/angry02`、`anon/angry03`、`anon/angry04`、`anon/bye01`、`anon/cry01`、`anon/cry02`、`anon/gacha_b2024_01`、`anon/gacha_c_a_01`、`anon/gacha_df3_01`、`anon/gacha_e235_01`、`anon/gacha_e253_01`

---

> **全量清单**（本表只是骨架）：连 DSH 用 `{"cmd":"assets.query","args":{"kind":"bg","tags":["夜晚"],"limit":30}}`，或直接读索引 `D:\work\puppet\webgal_games\asset_library\asset_index.json`（UTF-8 JSON 数组，字段 id/kind/file/name/tags/meta）。


---

## 附：动作 / 表情映射表（与仓库 `templates/webgal/motion_table.json` 同步）

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

**命名空间 ↔ 角色对照**（共 664 动作 / 232 表情，写 -motion= 直接查随包附件 `motion_keys.json`——全量键名权威字典；完整定义在仓库 `templates/webgal/motion_table.json` 的 cubism 条目，体积超限未随包附带）：

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

> 自定义动作/表情：编辑仓库 `templates/webgal/motion_table.json` 追加条目即可（重启或等待自动加载），本包与规范中的清单以该文件为准；该文件体积超限不随投喂包附带。
