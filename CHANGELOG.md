# 木偶工作室 更新日志（CHANGELOG）

> 给后续会话/维护者快速回忆用。按时间倒序，最新在最上。
> 目录铁律：V1 `D:\work\puppet\l2d_rig\` 已冻结不再改；V2 `D:\work\puppet\muppet_studio\` 一切用相对路径（以本目录为根）。

---

## v3.2.2（2026-11-13 10:45）视频工作台「模板」页占位

### 用户需求
「转场按钮旁边增加一个模板按钮，内部先内置一个模板导入，目前没有任何模板，
按钮先做出来，功能先不做」——纯 UI 占位，后续接模板系统时替换。

### 改动（video_workbench.py）
- Pivot 标签追加 `("template", "模板")`（转场右侧，共 7 页）。
- 模板页内容区内置单个「导入模板」PrimaryPushButton；点击 InfoBar 提示
  「模板功能敬请期待」，无任何导入逻辑。
- `_rebuild_asset_cards` 对 template 分支提前 return（不渲染素材卡片、
  隐藏空态提示）；其余 6 页行为零改动。
- 冒烟 + 回归：v3_ui 43 / video_integration 22 / video_menu 23 全绿。

### 待办（lead 记录，下一版顺带）
- installer.cs `LoadResources()` 解包的 %TEMP%\muppet_runtime_*/muppet_app_*
  副本只在成功路径清理，异常退出留垃圾（实测用户网络重试可堆 GB；今日清出
  109 个 7.7GB）。Main 加 finally 清理自有临时副本。

---

## v3.2.1（2026-11-12 23:30）视频工作台可用性修复 + 用户报障三连

### 面板可拖拽（用户需求「各模块的宽高应当可调节」，task-16）
- 视频工作台三列（素材库|预览|工程属性）改水平 QSplitter（最小 180/420/180），
  上方区域|时间轴改垂直 QSplitter（时间轴初始约 38%、最小 120），handle 6px
  配色与主界面一致（#3a3f4a / 悬停 #4d88ff），不可拖没。
- 尺寸记忆：QSettings 存 splitter sizes，启动恢复（坏数据容错）。
- 测试 wb_splitters_test 22 项（offscreen isCollapsible 恒真不可靠，改行为断言）。

### 时间轴高度拖不动（用户报障，lead 直修 clip_timeline.py）
- 根因两层：面板垂直 sizePolicy=Fixed（QSplitter 拒绝分配空间）+
  _update_height 把 minimumHeight 钉成全部轨道内容高并强制 resize。
- 修法：垂直 Preferred；minimumHeight 降为「工具条+刻度尺+一行」；
  **行高按可用高度自适应等比压缩**（下限 14px，剪映同款），拖大恢复默认，
  绘制与命中同源（压缩态点选精确）。哨兵 tl_height_test 13 项。

### 视频窗口菜单栏（用户需求带截图，task-17）
- 抽出 AssetMenuMixin，Workbench 与 FluentShellWindow 共用（属性名不变）。
- 视频窗口标题栏新增 文件/编辑/资产管理 三菜单：文件=新建/打开/保存/另存为/
  导出视频/最近工程(仅 video 类型)/退出（去掉 PSD/AE/JSON/GIF/导入图片）；
  编辑=撤销/重做（去掉全部木偶专属）；资产=整套复用含档位行、首击先填后弹。
- video_workbench 仅新增 save_as()；新建工程在 shell 侧实现（wb 无 act_new）。
- 测试 video_menu_test 23 项。

### 资产管理点击未响应 ~18s（用户报障，task-18）
- 档位行 engine_tier 在主线程 import torch（实测 11.8s）+ comfy 探测
  3 端口×4s 串行（6.2s）。三层修复：后台预热线程（daemon threading.Thread，
  批准偏离——QThread 在 torch import 期间析构 abort 0xC0000409）、菜单只读
  _TIER_CACHE（未就绪显示「正在检测档位…」）、comfy 并行探测 1s 超时 + 20s TTL。
- 测试 tier_thread_test 6 项（哨兵：stub 3s 探测主线程差值 -8ms）。

### 加速包点击弹「还没有配置下载地址」（用户报障）
- _start_asset_download 预检查只认顶层 url，parts 分卷资产（链接在
  parts[i].url）被误挡。条件改 `if not url and not a.get("parts")`。

### 非预设分辨率工程构造崩溃（lead 复现发现）
- _set_res_combo 调 ComboBox.insertText（qfluentwidgets 无此方法）→
  640×360 工程 AttributeError。改 insertItem + 同名去重。

### 退出竞态死锁（lead 全量回归抓到，py-spy 定案）
- 预热线程 import torch 持 OS loader lock 时进程退出（含 os._exit）卡死，
  flaky ~1/6。修复：构造 MainWindow 不再自动预热（脚本直构秒退安全）、
  预热改 run_app 启动 1.5s 后 singleShot + 菜单点开兜底，两处均查
  _APP_EXITING（aboutToQuit 置位）。asset_ui_test 连跑 6/6 干净。

### 测试维护
- parts_dl_test 9b 过期断言（硬编码 tag v3.1.0）改为 Release 下载路径+
  版本 tag 正则形态校验，升级 tag 不再失效。

---

## v3.2.0（2026-11-12 19:10）自动更新闭环 + 推理引擎四档优先级链

### 检查更新（欢迎页 → 后台下载 → 静默安装，全程零弹窗）
- 欢迎页左下角新增 32×32 圆形按钮（FluentIcon.UPDATE，悬停提示「检查更新」），
  点击才发网络请求：`update_checker.py` 按 raw.githubusercontent → ghfast.top →
  jsdelivr 三镜像回退拉 version.json（30s 总预算）→ 数字化 semver 比对 →
  有新版则后台下载新 installer（.part 断点续传、416 自愈、sha256 校验、MZ/PK
  文件头探测），完成后 InfoBar 提示「退出后自动安装」。
- 退出触发：`run_app` 挂 `aboutToQuit` → 有 pending 包就 DETACHED 拉起
  `MuppetStudio-Setup.exe --auto --dir <安装目录>`，异常全吞绝不卡退出。
- 安装器新增 `--auto [--dir]` 静默模式（dist/installer.cs RunAuto）：无 UI，
  缺 --dir 回退注册表 InstallPath；三层目录校验（存在/l2d_rig_qt.py+launcher.ini
  标志/黑名单与 DoFresh 同套）；只等待**引用该安装目录**的进程退出（≤60s，
  每 2s 轮询，不强杀）；复用 DoUpdate；成功自动重启应用。
  退出码 0=完成 / 2=无目录 / 3=校验失败 / 4=等待超时 / 5=更新失败，
  日志 %TEMP%\muppet_update.log（UTF8 无 BOM）。
- 测试：update_checker_test 61 PASS、installer auto 22 PASS、跨组件 E2E
  （真 spawn 假资源验契约）10 PASS、接线验收 6 PASS。
- 集成 bug（真包冒烟抓到）：--auto 不打 --no-pause 时失败路径的 Report 会弹
  无人点击的 MessageBox → 进程假死（日志已写完但不退出）。修复：RunAuto 顶部
  无条件 noPause=true，静默模式彻底封死任何弹窗路径；黑名单/不存在目录均秒回 rc=3。
- 发版约定：每次出新 installer 必须同步更新仓库根 version.json
  （version + installer_sha256 + notes），否则用户端检查不到新版。

### 推理引擎四档优先级链（用户拍板：加速包 → CUDA 运行时 → CPU 兜底）
- `engine_tier()` 返回 (tier, reason)：**accel_pack**（CUDA 可用且加速包
  done_marker 已装）→ **system_cuda** → **comfy**（触发条件与 v3.1.1 相同，
  AMD 路线不动）→ **cpu**。资产管理菜单顶部显示「当前拆图档位」。
- CPU 从 v3.1.1 的硬拒绝改为**最后兜底档**：前三档全不可用时允许拆图，
  但先弹确认（警告约 2-3 小时/张@512px），确认后写会话标志本会话不再弹，
  并按任务快照自动重投；env `MUPPET_ALLOW_CPU_ENGINE=1` 静默逃生保留；
  CLI 无确认途径仍拒绝（rc=2 + 文字指引）。
- seethrough_local 新增 `CpuConfirmationRequired(RuntimeError)`（继承旧类型，
  v3.1.1 捕获代码兼容）；installer.cs AmdGuide 文案同步新定位。
- 测试：engine_chain_test 44 PASS（档位矩阵/env 逃生/会话记住/CLI 拒绝/UI 接线）。

### 其他
- 资产管理按钮首击无响应修复（commit 93c9151，v3.1.1 tag 后随包）：菜单填充
  从 menu_opened 提前到 clicked，qfluentwidgets 首击链路不再弹空菜单。

---

## v3.1.1（2026-11-12 17:50）CPU 定位修正：只为打开软件，不是拆图通道

### 用户拍板
内置 CPU 版 torch 是**程序运行环境**（GUI/视频模块顶层依赖，装完即开），
**不作为拆图通道**——2-3 小时/张的 CPU 拆图不该"自动发生"。

### 行为修正
- `seethrough_local.decompose`：无 CUDA 时**默认拒绝**本地引擎拆图，
  报错文案给三条出路（CUDA 加速包 / ComfyUI+ROCm / 显式逃生阀
  `MUPPET_ALLOW_CPU_ENGINE=1`）。引擎路由本就"无 CUDA→comfy"，
  这次把 CPU 硬跑的口子也收进显式开关
- 反量化 CPU 路径**保留不删**（真实验证过：分层 24 层/91 分钟跑通，
  身体+头部组数值正常）——高级用户开逃生阀可用
- installer AmdGuide 重写：明确"拆图不走 CPU"，N 卡装 CUDA 包、
  A 卡走 ComfyUI+ROCm 两条路
- catalog 文案同步：runtime desc 不再宣传"CPU 推理栈"；cuda_pack desc
  标注"A 卡不适用此包"
- 资产 URL 对齐 tag v3.1.1

### 验证
- `cpu_engine_quick` 23 项（5b 改为断言逃生阀存在）
- `asset_catalog_test` / `asset_ui_test` 文案断言全过
- 安装包重打 411.1MB，v3.1.1 注入确认

### 3.1.1 内追加：资产管理按钮首击无响应修复（队友 finisher）
- 根因：qfw 弹出链 `mouseReleaseEvent → clicked(同步) → _showMenu → menu_opened`，
  重建挂在 menu_opened 上 = **先弹后填**，首击弹出的是空菜单
  （adjustSize 按 0 项算，视口高度≈0 → 体感"没反应"），第二次点击才可见
- 修法：重建时机挪到 `am.clicked`（直连槽在 _showMenu 之前同步执行，先填后弹）；
  `_rebuild_asset_menu` 内部与 v2.9.1 蜈蚣修复零改动
- `asset_btn_test` 12 项（首击即 9 项就绪/连点不重复/view 无残留/文本抽查）
  + `asset_ui_test` 14 项回归全过
- 安装包重打并替换 Release 资产（3.1.1 未分发过旧包，无用户影响）

## v3.1.0（2026-11-12 16:40）推理运行时两层化：CPU 内置开箱即跑 + CUDA 加速包应用内下载

### 架构决策（用户拍板）
- 安装包内置 **CPU 版 ML 栈**（torch 2.11.0+cpu + diffusers/transformers/
  accelerate/peft/sklearn）→ 任何人装完即可拆图，**不再要求自备 Python 环境**
- **CUDA 加速包**（torch cu130 + bitsandbytes，zip 1.87GB 分 3 卷）放 GitHub
  Release，「资产管理 → 推理加速」一键下载安装（N 卡提速回 14 分钟/张）
- A 卡适配 = 设备回退链的副产品（CUDA 不可用自动走 CPU）

### seethrough_local.py：设备回退链
- `pick_device()`：env `MUPPET_FORCE_DEVICE=cuda|cpu` 优先，否则自动探测
- CUDA 分支：**现有 nf4 路径逐行不变**（回归零风险）
- CPU 分支：`_load_pipeline_cpu` 纯 Python **nf4 反量化**加载
  - 格式实测：packed uint8 **高位 nibble 先**、qmap 16 值表在模型文件里
    （零 bitsandbytes 依赖）、block=64 absmax、shape 藏在 quant_state JSON
  - 金标准验证：与 CUDA bnb `dequantize_4bit` 比对，最大相对误差 0.12%
    （=bf16 舍入级）；`_fit_state_dict` 自动适配 text encoder 的
    `text_model.` 前缀变体（去/加两种 checkpoint 形态都遇到）
- 所有 `torch.cuda.*` 收进 `is_available`/cuda 分支守卫（静态断言覆盖）
- CPU 模式 decompose 开头打慢速预警（40-90 分钟/张 + 引导下载 CUDA 包）
- 实测：CPU 模型加载 285s、RSS 9.4GB（16GB 内存机器可行）

### asset_catalog / asset_downloader：分卷下载
- 新资产 `cuda_pack`（kind=accel, mode=parts）：3 卷 ×669MB、整包 sha256、
  done_marker=`torch/lib/cudnn64_*.dll` 纯文件探测（绝不 import torch）
- `runtime_root()`：env → <install>\runtime → sys.executable 回退（开发机）
- `_run_parts` 六道安全闸：torch 已 import 拒绝（防 DLL 锁）/磁盘预检/
  卷级断点续传/流式拼接/sha 失败**删全部卷**（防坏卷永久污染）/
  pre_delete 失败中止不解压
- `_download_file` 新增 **416 自愈**：残留 .part 超出服务器文件时
  丢弃重下（否则包更新后老用户永远拼不出正确文件）
- 资产管理菜单新增「— 推理加速 —」分组

### runtime.zip v2（build_runtime_v2.py）
- GUI 栈（本机）+ ML 栈（干净 venv 全量+排除法复制，重叠包本机优先）
- 排除法而非白名单：曾漏 `typing_extensions`（单文件包）导致自检失败，
  全量复制天然覆盖依赖闭包（functorch/pydantic/psutil 等）
- 自检扩展：ML import + `'cpu' in torch.__version__` 防 CUDA 泄漏 +
  fp32 matmul + safetensors 读写往返

### 验证
- `cpu_engine_quick` 23 项：设备链 5 组合、反量化数学（nibble 高低位/
  块缩放/shape）、state_dict 键处理、前缀变体、静态守卫、cuda_pack 目录
- `parts_dl_test` 24 项：本地 http.server 假分卷 E2E（下载/续传/拼接/
  sha 拒绝+删卷/在用保护/磁盘预检/镜像回退/asset_urls 形态）
- `asset_ui` 16+2 项：推理加速分组 + CUDA 包条目
- CUDA 包真实构建：zip 1.87GB（deflate1）sha256 已回填 catalog
- CPU 真实分解 + runtime v2 自检 + 全套回归（见下）

### 待办（本版内）
- runtime.zip v2 上传 + catalog URL/size 回填
- cuda_pack 3 卷上传 v3.1.0 Release（用户已批准放 git）

## v3.0.3（2026-11-12 13:04）欢迎页去掉「🏠 主页」装饰项

- 左侧栏「主页」标签是仿 AE 的**纯静态装饰**（本就在主页，点击无动作），
  连同其上方分隔线一并移除；侧栏只剩 新建项目 / 打开项目 + 底部条数提示
- `welcome_test` 41 项全过（布局无破坏）

## v3.0.2（2026-11-12 12:53）欢迎页视频编辑卡标注「测试功能」

- 类型对话框卡片标题：「视频编辑」→「**视频编辑（测试功能）**」
  （第一阶段刚落地，预览无声/无 ripple 等边界用户应知情）
- `welcome_test` 41 项（新增 4e2 标题断言）

## v3.0.1（2026-11-12 11:19）应用图标换 V3 版 + 启动器源码重建

### 图标
- 源图 `C:\Users\80715\Pictures\版本图标\v3_labeled.png`（1024×1024，
  绿发缝纫少女+V3 角标，转 RGB 规整）
- 覆盖 `anon_sewing_icon.png`（808KB）与 `anon_sewing_icon.ico`
  （141KB，16/24/32/48/64/128/256 七档，LANCZOS）——文件名不变，
  代码 ICON_PNG/ICON_ICO 常量零改动
- `/win32icon` 是编译期资源：三个 exe 全部重编（安装器经 bat、
  Uninstaller.exe 经 Uninstaller.cs、启动器见下）

### 启动器源码重建（重要）
- 发现历史问题：**木偶工作室.exe 的源码 launcher.cs 从未入库**
  （git 全历史只有 Uninstaller.cs/installer.cs），本次想重编换图标时找不到源
- 按 CHANGELOG v2.8.0 行为规格重写 `launcher.cs`（已入库）：
  ini→runtime→常见路径→PATH(排除 WindowsApps)→py -3 五级探测、
  探测成功写回 ini、6 库依赖体检（Retry/Cancel）、CreateNoWindow 启动
- 实测：双击语义启动成功拉起 `python.exe l2d_rig_qt.py`、ini 未被误写
- 注意：重写版行为对齐规格但未经用户机器长期验证，装机后留意启动问题反馈

### 验证
- verify_icons：三个 exe 各 7/7 ICO 帧命中且指纹一致
- 安装包 246.8MB @ 11:19，APP_VERSION=3.0.1 注入确认

## v3.0.0（2026-11-12 10:59）视频编辑模块第一阶段：剪映式工作台

### 需求
打开软件选「视频编辑」项目类型 → 进剪映式布局的视频工作台：
多轨时间轴（clip 块）、素材库、预览、导出。**木偶工程可作为动画片段直接上轨**。

### 新增模块（5 个文件）
- **video_model.py**（纯 Python 数据模型，无 Qt）：proj/tracks/clips 三层 JSON 结构；
  add/move/trim/split/remove/delete_range 全部**先校验后原地改**（同轨禁重叠、
  素材长度约束、锁定轨拒绝、MIN_DUR=0.05s）；吸附 snap_points/snap_time；
  sanitize_project 脏文件容错（nan/负值/坏 kind/坏 id 逐个剔除，坏轨重排不丢）
- **video_source.py**（队友 B）：FrameSource 统一接口 `frame(t)->RGB ndarray`；
  ImageSource（imdecode 中文路径兜底）/ VideoSource（≤16 帧顺序读、远跳 seek、
  LRU 64 帧缓存，重复取帧 0 解码，stats 可断言）/ PuppetSource（复用
  l2d_rig_core.render_frame，keyframes 塞 rig 走 sin 基线+覆盖路径，逐帧确定性）；
  probe_source 自动探测 kind/尺寸/时长；AudioLoader/waveform（ffmpeg 转 wav，
  找不到静默降级）；close 后取帧统一抛 SourceClosed
- **clip_timeline.py**（队友 A）：ClipTimelinePanel 多轨 clip 时间轴自绘面板：
  轨头列（👁/🔒 直切标志）、刻度尺 scrub、clip 色块按 kind 配色、拖移动/两端 trim
  （吸附命中画 #ffd166 辅助线、失败静默回弹）、✂切分/🗑删除/▶跟随工具条、
  Ctrl+滚轮 0.5~400px/s 缩放以鼠标为锚；编辑全走 video_model，成功恰发一次
  edit_committed（外层据此 push history）
- **video_export.py**：compose_frame 逐帧合成（**预览导出同一条代码路径**；
  fit 等比黑边、上层轨优先、隐藏轨跳过、坏帧不中断）；SourceManager 缓存池；
  mix_audio 全工程混音（wav 直读/ffmpeg 提取、44.1k 重采样、int16 饱和）；
  VideoExportWorker：cv2 写无声 mp4 → ffmpeg 合流 aac（无 ffmpeg 降级无声）
- **video_workbench.py**：剪映式布局壳——顶部 Pivot（媒体/音频/文本*/贴纸*/特效*/
  转场*，*=空页占位）+ 保存/导出按钮；左素材库（FlowLayout 卡片+真解码缩略图，
  双击上轨到末尾）；中预览（QTimer 逐帧、720p 上限缩放、播放条）；右属性
  （分辨率/帧率 + 选中 clip 起/入/时长 spin，拒绝回弹）；底部时间轴。
  撤销重做=工程快照栈（Ctrl+Z/Y，60 步上限，JSON 对比去重）；Ctrl+S 保存；
  工程存 `projects/<名>/video_project.json`

### 集成
- 欢迎页「视频编辑」卡**解禁**（type_id=video_edit）；历史表类型列显示「视频工程」
- 门控 `_welcome_gate`：新建视频 → FluentShellWindow（单页 FluentWindow 壳，
  同款标题栏）；打开目录按 `video_project.json` 分流视频/木偶窗口；
  **坏目录留在欢迎页不建窗**（木偶路径 find_meta 预校验、视频路径 load 失败回退）
- `load_recent` 兼容双类型工程目录；视频工作台写 recent 带 `type:"video"`
- 预览第一阶段**无声**（画面所见即所得），声音在导出里体现——已知边界，写入文档

### 验证（新增 6 套测试）
- `video_model_test` 53：构造/添加/移动/trim/split/delete_range/吸附/
  sanitize 脏数据 9 连/JSON 往返
- `video_source_test` 87（队友 B）：自造 mp4/png/wav/木偶目录，乱序 seek、
  缓存命中、中文路径、cv2 5.0 grab() 返回 bool 适配
- `clip_timeline_test` 93（队友 A）：hit test/拖拽/吸附/回弹/锁定/隐藏/缩放/
  scrub/QWheelEvent 8 参构造
- `video_export_test` 35：fit_paste 黑边/上层轨/缓存/混音饱和截断/Worker 端到端
- `video_integration_test` 22：**真实素材全链路** probe→帧源→合成→导出成片→
  回读抽帧验证颜色与帧数
- `v3_ui_test` 43：工作台构建/4 素材导入/Pivot 过滤/双击上轨/撤销重做/
  预览播放/spin 编辑与重叠回弹/保存打开往返/**门控四路由**（新建视频、
  视频目录、木偶目录、坏目录）/欢迎页类型列/recent 兼容
- 全套回归 **19 套 0 FAIL**（13 套 puppet 老件 + 6 套 v3 新件）
- offscreen 渲染 v3shot.png：布局六区全就位（Pivot/素材卡缩略图/预览帧/
  双色 clip 块/播放头/属性面板）

### 踩坑记录
- qfluentwidgets Pivot：`switched` 信号不存在→`currentItemChanged(str)`；
  `currentItem()` 返回 PivotItem 对象且**无 routeKey()**→用信号参数自行记录 tab
- FlowLayout.takeAt(0) 返回 widget 本体而非 QLayoutItem（`.widget()` 直接 AttributeError）
- NTFS 上 `f.truncate(3GB)` **不是稀疏文件**，真占磁盘把 C 盘写满 ENOSPC——
  测试大占位文件一律放 D 盘并测后 rmtree
- Qt 槽内异常默认被吞（stderr 丢弃时完全看不见），排查用最小复现脚本直调

## v2.12.1（2026-11-12 10:10）流程 bug 修复：「打开项目」不再跳过欢迎页

### 问题
欢迎页点「打开项目」直接进了工作台，目录选择框是在工作台里弹的。
正确流程：**在欢迎页选好工程**（且校验通过）才进工作台。

### 根因与修法
- 旧链路：`btn_open → browse_requested → _welcome_gate._enter(先建主窗) → act_open_project 弹框`
- 新链路：`btn_open → WelcomeWindow._on_browse 在本页弹 QFileDialog →
  is_project_dir 校验（layers_meta.json 或 *_layers.json，同 find_meta 规则）→
  通过才发 open_requested(dir)`，走历史项目同一条门控进工作台
- 取消 / 非工程目录都**留在欢迎页**（非法目录 InfoBar 报错，不建主窗）
- `browse_requested` 信号与门控接线整体删除（防回潮，测试断言信号不存在）

### 验证
- `welcome_test` 40 项（新增 5）：取消不发信号 / 非工程目录不发信号 /
  合法目录发 open_requested / 欢迎窗无 browse_requested / 门控不再调
  act_open_project（QFileDialog.getExistingDirectory 打桩注入返回值）
- 全套回归 **13 套 505 断言 0 FAIL**

## v2.12.0（2026-11-12 09:55）启动欢迎页：先选项目类型 / 打开历史项目

### 需求
打开软件后不再直接进工作台，先进**欢迎页**（仿 After Effects 主页）：
选「新建项目类型」（目前只有图片编辑）或双击历史项目，然后才进工作台。

### 新增 `welcome_window.py`
- **左侧栏**：新建项目（主按钮）/ 打开项目 / 🏠主页高亮 / 「最多 5 条」提示
- **右侧**：居中大标题「欢迎使用 木偶工作室」+ 最近使用项表格
  （名称 / 最近使用 / 大小 / 类型 四列）+ 右上筛选框（按名称子串过滤，
  全滤掉显示「没有匹配…」，空列表显示「暂无历史项目…」）
- 名称优先读 `rig_project.json` 的 name，否则目录名（与 Workbench 同规则）；
  最近使用取工程目录最新 mtime → 相对时间（刚刚/X分钟前/X小时前/X天前/日期）；
  大小 = 目录递归总字节 → KB/MB/GB
- 打开方式：双击行 或 选中按回车（`_RecentTable.keyPressEvent` 子类实现，
  不用实例属性覆写——PySide6 虚拟调用不认实例属性）
- **类型对话框** `_TypeDialog`（MessageBoxBase）：图片编辑卡（默选，蓝框）
  + 视频工程卡（置灰不可点，敬请期待），确定按钮「进入工作台」
- 本模块**不 import l2d_rig_qt**（脚本直跑时 `__main__` 双实例问题），
  最近列表/版本/图标由参数传入，交互结果用信号（new/open/browse_requested）发出

### `l2d_rig_qt.py` 启动门控
- 抽 `_welcome_gate()`：建欢迎窗 + 接三路信号到「建主窗→关欢迎→执行动作」
  （**先建后关**：反序会在两窗间隙触发 quitOnLastWindowClosed 把应用退掉）
- `run_app()` 改为先进 `_welcome_gate()`，选定才进工作台；
  测试与「文件→打开工程」内部调用仍可直接 `MainWindow()`，不受门控影响
- VERSION 2.11.3 → 2.12.0

### 验证
- `welcome_test` 35 项：基本渲染（标题含版本/行数/名称优先级/相对时间/
  KB+GB 大小/类型列/空态隐藏）、筛选（匹配显隐/全滤提示/清空恢复）、
  打开信号（双击+回车发目录）、类型对话框（默选/禁用卡不响应/按钮文案）、
  新建路由（打桩 exec 发 image_edit + 空列表提示）、
  **门控接线**（open/new/browse 三路都建主窗且关欢迎窗，打桩 load_dir/
  act_open_project/act_new 验证调用）、真实 load_dir 冒烟
- 全套回归 **13 套 500 断言 0 FAIL**
- offscreen 渲染 welshot.png 像素采样确认布局正确（侧栏 #262932 在左、
  主区 #1f2126 在右、青主按钮+灰按钮各就位）

## v2.11.3（2026-11-12 09:30）时间轴顶部拖拽条 + 图层标题精简

### 时间轴高度调节：滑块 → 分隔条式拖拽条
- 头部「高度」滑块**已移除**（占位、丑）
- 改为面板**顶部 2px 拖拽条**，样式与右侧「骨架节点/图层」分隔条同款
  （静置 `#3a3f4a`，悬停/拖拽高亮 `#4d88ff`，光标上下箭头）
- 热区 6px（条 + 头部上留白），按住上下拖即改面板高度（84–720px）
- 拖高时行高仍同步放大（最多基准 2.5 倍），轨道真正撑开
- 高度继续走 `anim.panel_h` 持久化，下次打开工程恢复

### 图层标题精简
- 「图层（顶=最前，拖动重排；PS 式：眼睛显隐 / 缩略图重命名 / 锁定，右键菜单）」
  → **「图层」**——功能用户已经熟了，长说明占一行没必要

### 验证
- `v2113b_test` 27 项：滑块彻底移除（属性/控件/标签/方法四层检查）、
  拖拽条像素色（静置/悬停/恢复/横贯整宽）、拖拽 ±80px 改高、上下限夹紧、
  行高放大、悬停/leave 状态机、持久化往返（真实 rig_project.json，260 存读 +
  旧工程缺省回自适应）、头部布局
- 全套回归 12 套全 PASS（anim 63 + timeline 46 + v2112 28 + persist 18 +
  render_kf 17 + v2113b 27 + mesh 96 + layer_menu 52 + proj_nav 24 +
  transform 45 + asset_catalog 23 + asset_ui）

## v2.11.2（2026-11-12 00:35）时间轴体验三项：高度可拖 / 字号自适应 / 字色提亮

### 1. 时间轴高度可拖拽
- 面板**上边缘 5px 热区**可拖动调高（光标变上下箭头），范围 84–720px
- 不限行数：把面板拖高时**行高同步放大**（最多到基准 2.5 倍）填满可用空间，
  轨道被真正撑开，关键帧更容易点中 —— 而不是行高不变、下面留一片空白
- 高度被记住（`panel_h`），下次打开工程恢复

### 2. 字号自适应
- 头部新增「字号」下拉：**紧凑 85% / 标准 100% / 大 125% / 特大 150%**
- 行高、刻度区高度、名列宽、关键帧菱形、播放头三角、字体大小**全部按比例缩放**
- 字号下限 9px，保证最小档也能读
- 大字号下提示文字自动隐藏，避免把窗口撑出屏幕

### 3. 字色提亮（原来刻度几乎看不清）
| 用途 | 旧色 | 新色 | 亮度 |
|---|---|---|---|
| 刻度/次要文字 | `#9aa4b2` | `#c8d1dc` | 163 → **208** |
| 行名/主文字 | `#e6edf3` | `#f2f6fa` | 232 → **245** |
| 网格线 | `#2c3340` | `#3a4356` | 与底色差 36（原来几乎同色） |

- 新增整秒刻度色 `#8fa3bd`，秒内次刻度稍暗，层级更清楚
- 刻度文字从 `setPointSize(7)` 改为 `setPixelSize(11*scale)`，可控且更大

### 持久化
- `rig_project.json` 的 `anim` 段新增 `ui_scale` / `panel_h`
- 老工程（无这些字段）回落到 1.0 / 自适应；脏值（`"abc"`、负数、越界）全部安全回落

### 验证（46 项新断言）
- `tl_v2112_test` 28 项：拖拽高度（+100px 精确）、上下限夹紧、各档缩放尺寸、
  字号随缩放变化、颜色亮度阈值、各档位绘制不崩、坐标换算
- `persist_test` 18 项：存档字段、读回应用、老工程容错、脏值回落、
  **4 档缩放 × 3 种高度 = 12 种组合全部往返正确**
- 全套回归 10 套全 PASS

## v2.11.1（2026-11-12 00:10）更换软件图标（缝纫少女 mutsumi）

- 图标换成新的缝纫主题插画：绿发少女 + 剪刀 + 线轴 + 花布，和「木偶工作室」的定位一致
- 从 1024×1024 原图生成 7 个尺寸的 ICO（16/24/32/48/64/128/256），PNG 保留 1024 原尺寸
- **三个 exe 全部重新编译**（`/win32icon` 是编译期嵌入，光换 .ico 文件不会更新已有 exe —— 这是之前踩过的坑）：
  - `木偶工作室.exe`（启动器）
  - `Uninstaller.exe`（卸载器）
  - `MuppetStudio-Setup.exe`（安装器）
- 验证方式：从 PE 资源里逐字节比对 ICO 的 7 帧位图，三个 exe **7/7 全部命中且完全一致**

## v2.11.0（2026-11-11 23:50）关键帧动画 + 时间轴：动画从"看"变成"做"

**这是 v3.0 的第一块基石**。此前动画只有几个写死的 `math.sin` 循环，用户既不能加轨道也不能打关键帧；现在可以做动画并导出。

### 新增：关键帧 + 时间轴（anim.py / timeline_panel.py）
- **数据层 `anim.py`（纯函数，无 Qt 无 IO）**：节点 → 参数（rot/tx/ty/sc/sc_y）→ 按时间排序的关键帧列表；支持 `linear / in / out / inout / hold` 五种缓动；首尾帧外取端点值不外推；全部接口容错（脏数据不抛异常）
- **时间轴面板（画布底部）**：头部工具条（`+ 关键帧` / `清空动画` / 时长 / 帧率）；自绘轨道区（时间刻度按 1-2-5 规整、行交替底色、行名、关键帧连线）；黄色菱形关键帧点、蓝色播放头带顶部三角
- **交互**：拖播放头 scrub；点时间轴空白跳转；点关键帧选中；拖关键帧改时间（0.01s 吸附）；双击删除；右键改缓动/删除
- **打帧语义**：`+ 关键帧` 给当前选中的骨架节点打帧，值取当前时刻的 sin 基线 —— **打帧不改变当前画面**

### 渲染接入（向后兼容是硬约束）
- `animation_for(node, t, nd, tracks=None)` 新增可选 `tracks`：先算原 sin 基线，再用关键帧覆盖**用户显式打过帧的通道**
- **无关键帧的工程逐像素一致**（17 项断言验证，含 `np.array_equal` 全等比较）—— 老工程打开行为完全不变
- 关键帧存在 `rig["keyframes"]`，渲染时由 `_cum_transforms` 取出传入，天然支持存档与导出

### 撤销/重做
- `History` 快照新增 `keyframes` 字段，**深拷贝值记录**（和 bbox/names 同样的坑：引用快照会被后续编辑污染）
- 所有时间轴操作遵守项目既有约定「**先改数据，再 push**」

### 时长 / 帧率可调
- 替换写死的 `% 10.0` 循环与固定 30fps：时间轴头部可设时长 0.2–600s、帧率 1–120
- 播放回绕、滑块映射、MP4/GIF 导出全部改用可调值

### 存档
- `rig_project.json` 新增 `keyframes` 与 `anim{duration,fps}` 字段
- 读回带容错：老工程（无这些字段）→ 空关键帧 + 默认 4s/30fps；坏 JSON 不挡工程打开

### 验证（126 项新断言）
- `anim_test`：63 项（插值/缓动端点/乱序/同 t 去重/单调性/容错/JSON 往返/nan-inf 拦截）
- `render_kf_test`：17 项（**逐像素向后兼容**、关键帧生效、位移幅度实测 ±36px、动态改帧、旋转、sc_y 语义、脏数据不崩）
- `timeline_test`：46 项（面板构建、打帧、seek、插值、撤销/重做、深拷贝隔离、删除、时长帧率、存档往返、坐标换算、高度自适应）
- 全套回归 9 套全 PASS（含 mesh 96 项、layer_menu、transform、asset 系列）

### 已知限制（v2.11.0 范围）
- 不支持框选多个关键帧 / 曲线编辑器（bezier 手柄）/ 自动关键帧模式
- 网格形变仍是"一次性烘焙"，尚不可关键帧化（v3.0 计划）

## v2.10.2（2026-11-11 23:10）拆分进度移到底栏 + 导航栏修复重叠/支持拖宽

### 拆分进度挪位
- 进度以前"塞进导航栏"，但导航栏默认是隐藏的，用户根本看不见 —— 现在改为**画布底栏播放条右侧**的进度条（`qfluentwidgets.ProgressBar`）+ 百分比文字，空闲时自动隐藏不占位
- 保留 `progress` 的抗倒退逻辑（只增不减），结束时统一清除

### 导航栏两个问题
1. **修重叠**：展开导航栏后，标题栏按钮（文件/编辑/资产管理）压在工程列表上 —— 根因是库把标题栏钉在窗口顶部整宽（`titleBar` 覆盖层），而导航栏在 `hBoxLayout` 里只占内容区。修法是展开时把标题栏整体右移到导航栏右缘、宽度相应减少；折叠时还原
2. **修顶部空白**：库只给内容区加了 48 的顶部边距（`widgetLayout.setContentsMargins(0,48,0,0)`），导航栏没加，导致第一项被标题栏压住像"空白"。现在给导航栏也补 48 顶部边距

### 导航栏宽度可调
- 导航栏右缘挂一条 4px 拖拽把手（悬停变蓝），拖动实时调宽，范围 120–420px
- **双击把手恢复默认 140px**
- 窗口 resize 时把手与标题栏偏移自动重算
- 折叠时把手自动隐藏，避免在空白处留一条热区

### 验证
- 导航布局 14 项断言全 PASS：展开后 `titleBar.x == nav.width`（不重叠）、按钮全局 x ≥ 导航栏右缘、把手贴右缘、宽度 220/120(夹紧)/420(夹紧)、折叠后标题栏回 x=0 且把手隐藏
- 进度条 12 项断言全 PASS：空闲隐藏、35% 显示、位置在 time_lbl 右侧且同一行、防倒退、结束清零并隐藏
- 回归 7 套全 PASS（layer_menu / finisher / proj_nav / transform / mesh(96) / asset_catalog / asset_ui）

## v2.10.1（2026-11-11 22:50）去掉左上角返回按钮 + 安装器标题带版本号

### 主窗口
- 去掉左上角那个 ← 返回按钮：单页应用没有"返回"语义，留着纯属噪音
- 实现走 `navigationInterface.setReturnButtonVisible(False)`，库版本不兼容时兜底直接隐藏 `panel.returnButton`

### 安装器
- 窗口标题从「木偶工作室 安装向导」改为「**木偶工作室 v2.10.0 安装向导**」，方便用户一眼确认装的是哪个版本
- 版本号不再手写：`installer.cs` 里留 `@@APP_VERSION@@` 占位常量，打包时由 `dist/inject_version.py` 从 `l2d_rig_qt.py` 的 `VERSION` 读取注入，单一版本源不跑偏
- 新增 `dist/inject_version.py`（打包脚本自动调用，独立脚本比内联 python -c 可靠）

### 验证
- 主窗口实测：`returnButton.isHidden() == True`、`isVisible() == False`，标题 `木偶工作室 v2.10.1` ✅
- 安装器实测：`EnumWindows` 读到标题 `木偶工作室 v2.10.1 安装向导` ✅
- 回归 6 套：finisher / proj_nav / transform / mesh(96) / asset_catalog 全 PASS；layer_menu 的 21c 是计时抖动项，重跑 ALL PASS ✅

## v2.10.0（2026-11-11 22:30）安装包内置 Python 运行时：装完即用，不再要求系统 Python

**核心目标**：用户双击安装完，直接双击「木偶工作室.exe」就能打开，不需要自己装 Python。

### 安装包内置运行时（runtime）
- 安装包把便携式 Python 运行时（8575 个文件 / 642MB 解压后）作为**内嵌资源**打包，安装时自动解压到 `<安装目录>\runtime\`
- 全新安装（`--fresh`）与更新（`--update`）都会确保运行时就位；已存在则**内部直接跳过**，不重复解压
- 解压后**自动重写 `launcher.ini`**，把 `python=` 指向内置运行时的绝对路径——修掉"装完提示找不到 Python"的老毛病（原来打进包里的 launcher.ini 带着开发机路径 `D:\Python312\python.exe`，用户机器上根本不存在）
- 安装包体积从 49MB -> 246MB（含 233MB 运行时）

### 资产管理：模型改为按需下载（不再塞进安装包）
- 新增 `asset_catalog.py` 的真实文件清单：LayerDiff3D（23 个文件 / 3.76GB）、Marigold（15 个文件 / 1.93GB），**清单与真实模型目录逐文件对齐**，避免"漏下文件导致应用读不到"
- 全部 38 个模型文件的镜像 URL 实测可达（`hf-mirror.com`，HEAD 全部 200）
- `asset_downloader.py` 支持三种形态：逐文件（模型，含断点续传）、单文件（sam 的 `mobile_sam.pt`）、zip 包（runtime）
- **下载后严格校验**：清单里每个文件都必须真实落地且 > 0 字节，否则报错而不是"假装成功"——这是防"读不到文件却无限重下"的关键闸门
- sam 改用官方 MobileSAM 权重源（`hf-mirror.com/dhkim2810/MobileSAM`），与原先内置的 `mobile_sam.pt` **SHA256 完全一致**（40728226 字节）

### 修复
- 资产下载器补上 GitHub Release 的 302 跳转跟随（原来会静默失败），并加 ghfast 镜像兜底
- 安装器 `ExtractRuntime` 全程走 `InvokeOnUI` 更新状态，工作线程安全
- `asset_ui_test` 卡死修复：测试原先真的触发了 620MB 下载 + `dlg.exec()` 模态循环，offscreen 下永不返回；改为打桩验证调用

### 安装器界面
- **左侧 banner 换成新插画**（白发少女 + 铃兰，1680×2240 原图裁成 220×400，比例精确 0.550）
- 修掉误导文案：复选框原来写「下载模型权重（nf4 量化，约 5.3 GB，需联网且支持断点续传）」，但安装器早已不再下载模型 —— 改为「检查并整理已有模型（模型改由应用内「资产管理」下载）」，另外 4 处状态文案同步修正
- 验证方式：`PrintWindow` 直接抓窗口位图，确认 banner 资源 sha256 与源文件逐字节一致（`4d620fa4e10568d6`）

### 验证
- 内置 runtime 独立跑 Python 3.12.10 ✅，PySide6/shiboken6/cv2/numpy/PIL/scipy/qfluentwidgets/psd_tools 全部导入成功 ✅
- 全新安装实测：8575 个文件全量解压到 `runtime\`，`launcher.ini` 正确指向内置 python ✅
- 内置 runtime 导入应用 `IMPORT_OK`，VERSION=2.9.1（升级前），GUI 主窗口创建成功、标题正确、事件循环正常退出 ✅
- 资产下载 E2E：sam 真实下载 40728226 字节、位置正确、二次下载 **0.00 秒**不重下 ✅

## v2.9.1（2026-11-11 18:00）修资产菜单"蜈蚣"叠加 + 安装器去掉模型下载

- **修 bug（用户反馈）**：每点一次「资产管理」，菜单就像叠了一层，越点越长成"蜈蚣"。
  - **根因**：`RoundMenu.clear()` 只清 `actions` 与 `_subMenus`，**`view`(QListWidget) 里的行会残留**。实测连续重建 6 次，view 行数 4→5→6→7 单调增长，视觉上就是一层层叠加的菜单。
  - **修法**：新增 `_asset_clear_menu()` —— 除了 `clear()` 还显式 `while view.count(): view.takeItem(0)`，并丢弃旧子菜单引用。实测连续重建 6 次，view 稳定 **9 行 / 7 个 action 不变**。
  - 菜单**保持原来的平铺样式**（不改成子菜单，用户明确要求）。
- **安装器去掉模型下载（架构调整落地）**：`DoUpdate` 里原三分支（完整/不完整就重下/没有就下）改为——
  - 模型完整 → 提示"完整性检查通过，未改动"；
  - 不完整 → **`RepairModels` 归位整理 + 报告缺几个文件**，提示「打开应用后在顶部资产管理里下载」（不再调 `DownloadModels`）；
  - 完全没有 → 提示"安装后到资产管理下载 LayerDiff3D 与 Marigold"。
  - 按用户要求：**安装时依旧检查模型文件并分类归位**，只是不再负责下载。
- **安全加固**：`asset_downloader._extract_zip` 的 zip slip 校验补 `os.sep`。原写法 `startswith(abspath(dest))` 是字符串前缀匹配，当 dest 末段名是兄弟目录名前缀时（如 `models/sam` vs `models/samEVIL`），恶意 zip 里的 `../samEVIL/x` 能写出目标目录。由队友测试实测复现后修复。
- **新增回归**：`asset_download_test.py`（队友交付，**44 项全 PASS**）——用本地 `http.server` 动态生成 zip，实测**断点续传真的生效**（服务器收到 `Range: bytes=110-`，起点精确等于预写半截长度）、服务器不支持 Range 时降级全量、截断包正确判失败、cancel 235ms 退出、`.part` 清理、进度信号、zip slip 两种变体拦截。
- **实测**：八套回归全绿（layer_menu / finisher / proj_nav / transform_test / mesh_test 96 / asset_catalog 22 / asset_ui 13 / asset_download 44）。注：layer_menu 的 `21c` 是性能项，偶发波动（单跑 3 次全过，37.9~43.2ms vs 基线 26.7~29.6ms）。
- **版本号**：bug 修复 -> patch+1 -> **v2.9.1**。

## v2.9.0（2026-11-11 17:20）资产管理中心 + 安装包不再下模型

- **「资产管理」菜单**（顶栏 `文件 / 编辑 / 资产管理`，编辑右侧）：下拉列出全部可下载资产，**已下载的显示 ✔ 和实际占用**，未下载的显示 ⬇ 和体积，点一下即下载。分两组：运行环境 / 模型。菜单每次打开都实时重建（本地可能刚下完或用户手删了目录），底部有「刷新状态」。
  - 实测渲染：`— 运行环境 —` / `⬇ 内置 Python 运行时（591 MB）` / `— 模型 —` / `⬇ 分层模型 LayerDiff3D (nf4)（3242 MB）` / `⬇ 深度模型 Marigold (nf4)（1812 MB）` / `✔ MobileSAM（精细拆分）（已下载 39 MB）` / `刷新状态`
- **架构调整（用户要求）**：**取消"安装包下载模型"这条路径**。安装包今后只装程序本体，装完即可打开；Python 运行时 + 全部模型都移到资产管理里按需下载。这样用户装完不会被 5.3GB 下载卡住，体验更好。
- **新增三个模块**：
  - `asset_catalog.py`（队友交付，22/22 自测通过）：纯数据层。定义 ASSETS（runtime 620MB / LayerDiff3D 3.4GB / Marigold 1.9GB / MobileSAM 40MB），提供 `is_installed` / `installed_size` / `missing_assets` / `installed_path` / `asset_by_id`，全部容错不抛异常。
  - `asset_downloader.py`：后台线程下载（5GB 直接在主线程会卡死 UI），支持 **HTTP Range 断点续传**、多镜像依次尝试（hf-mirror 优先）、`.part` 残留复用、zip 自动解压（智能剥掉单层顶层目录）、防 zip slip、可取消。配套 `AssetDownloadDialog`（进度条 + 已下/总量 + 百分比 + 取消）。
  - 两个方法 `_rebuild_asset_menu` / `_add_asset_action` / `_start_asset_download` 挂在 Workbench 上。
- **修**：`FluentIcon.SYSTEM` 不存在（改用 `DEVELOPER_TOOLS`）；三个资产方法缩进错误被嵌成局部函数（`_add_asset_action` / `_start_asset_download` 现为正常类方法）；资产按钮最初插错容器（Workbench 内部的 `tb_lay` 不是顶栏，真正顶栏在 `MainWindow` 的 `tbar.insertWidget`），已修正到 `insertWidget(3, ...)`。
- **实测**：资产 UI 专项 13 项全 PASS（按钮存在/文字/菜单分组完整/四类资产齐全/已装显示✔未装显示⬇/未配 url 点击不崩）；asset_catalog 22/22；七套回归全绿（layer_menu / finisher / proj_nav / transform_test / mesh_test 96 项 / asset_catalog / asset_ui）。
- **版本号**：体系性架构调整 -> minor+1 -> **v2.9.0**。

## v2.8.0（2026-11-11 16:10）启动器 Python 环境自适应 + 修模型"假就位"误报

- **启动器 Python 环境自适应**（解决真实用户反馈：装到 `F:\木偶工作室` 启动报"找不到 Python: D:\Python312\python.exe"）：原启动器把 `D:\Python312\python.exe` 写死为回退值，换台机器就崩。现在按顺序探测：
  1. `launcher.ini` 里显式 `python=` 且文件存在 → 用它
  2. 同目录 `runtime\python.exe`（为将来内置便携运行时预留）
  3. 常见安装位置（`C:\Python312/311/310/313/39`、`D:\` 同款、`Program Files`、`Program Files (x86)`）
  4. `PATH` 里的 `python.exe`（**主动排除 WindowsApps 商店别名**，避免弹应用商店）
  5. Windows `py` 启动器：`py -3 -c "import sys;print(sys.executable)"` 问出真实路径
  - 探测成功会**自动写回 launcher.ini**，下次直接命中，用户无需手改配置。
  - 全都找不到时不再只报"找不到 Python"，而是弹出**可操作的引导**：列出所需库（PySide6/opencv/numpy/torch/diffusers/transformers/peft/accelerate/bitsandbytes/scikit-image）+ 完整 pip 命令 + N 卡 CUDA 版 torch 的安装建议。
  - 新增**依赖体检**：找到 Python 后用 `importlib.util.find_spec` 检查 6 个关键库，缺哪个就明确列出并给出对应 pip 命令，不再"黑屏闪退"。
  - 实测：ini 写死不存在的 `Z:\nope\python.exe` → 自动探测到真实解释器 → 写回 ini → 脚本正常执行（exit 0）。
- **修"模型已下载就位"误报**（用户反馈截图：更新完成提示"模型已下载就位"，但实际目录只有 7 个文件 / 0.01GB，缺 32 个）：
  - 根因：`HasModels()` 只检查**单个 unet 权重文件是否存在**，残缺模型也被判为"已装好"，于是更新流程跳过一切模型处理、还打印"已下载就位"。
  - 修法：`HasModels()` 改为走 `FindBadModels()` **全量 manifest 校验**——每个文件都存在且大小正确才算装好。
  - 实测：残缺目录（ceshi）判定 `BAD=32`，完整目录判定 `COMPLETE`，两类结果正确区分。
- 同时规范化了 `installer.cs` 的双倍空行（多次脚本化编辑的残留）。
- **实测**：五套回归全绿（layer_menu / finisher / proj_nav / transform_test / mesh_test 96 项）；启动器自适应探测、缺库体检、模型完整性判定均端到端验证通过。
- **版本号**：环境自适应属体系性增强 -> minor+1 -> **v2.8.0**。

## v2.7.2（2026-11-11 15:35）修自由变换拖动"对象飞走"

- **修 bug（用户反馈）**：自由变换里用鼠标拖动位移时，对象会**飞走**（位移远大于鼠标移动，越拖越失控）。
  - **根因**：拖动逻辑是**增量式**——`drag_move` 每次执行 `cx += (x - start_x)`，而 `start_x` 记的是**按下时**的坐标、拖动过程中不更新。于是每帧都会把"从按下点到当前点"的整段位移**再累加一次**，鼠标稍微一动位移就指数级放大。
  - **修法**：改为**绝对基准重算**——按下时用 `deepcopy` 存一份状态快照 `_ft_drag_base`，拖动期间每帧都从这份快照重新计算目标状态（而不是在当前状态上叠加）。拖动结束/取消时清空快照。平移、控制点缩放、框外旋转、网格点变形**四种拖动全部统一**走这个机制。
- **实测（回归验证）**：鼠标平移 (60,40)，对象位移 **59.2 × 40.1**（旧 bug 下会放大数倍乃至失控）；二次拖动 (-20,-16) → **-20.9 × -17.4**；拖角缩放 hw 318.5 → 337.6 → 357.7 随鼠标线性增长；旋转 90° 稳定（delta 1.47 rad）；尺寸/角度在纯平移时严格不变。五套回归（layer_menu / finisher / proj_nav / transform_test / mesh_test 96 项）全绿。
- **版本号**：bug 修复 -> patch+1 -> **v2.7.2**。

## v2.7.0（2026-11-11 14:40）网格变形 + 安装器「修复模型」按钮

- **自由变换网格变形（PS 的「变形」模式，回答用户"内部格点能否变换"——可以）**：选项栏新增「网格」开关（自绘 3×3 网格图标）。开启后变换框内画出 4×4=16 个格点，**拖动任一内部格点即可做局部弯曲**（发丝飘动、布料褶皱这类不规则形变），整体缩放/旋转仍照常可用，两者正交叠加。新增 `transform_tool` 的 5 个纯函数：`mesh_init/mesh_points/mesh_hit/drag_mesh/warp_mesh`（`GRID_N=3`，状态里加 `mesh` 字段存局部偏移）。
  - 实现：`warp_mesh` 用**分段双线性反向映射**——每块四边形拆两个三角形各自求仿射，`cv2.remap` 采样；块内连续、块间共享边所以无接缝。
  - **修掉一个自己引入的坐标 bug**：初版把输出画布局部坐标又加了一次世界偏移 `ox/oy`，导致非原点 bbox 的恒等变形被截断（规律精确吻合「输出宽 = ow − ox」）。改为严格区分「源图坐标（0..w-1）」与「画布局部坐标」，恒等时 bbox 精确原样返回。这个 bug 由队友独立写的 `mesh_test.py` 用「offset bbox 恒等哨兵」抓到。
  - 实测：纯函数层 **96 项断言全 PASS**（含旋转态拖点、n=4、脏 mesh 容错、不可变性、洞区 alpha 传播；性能 1000×1000 恒等 107.5ms / 拖动 142.1ms）+ UI 接线后五套回归全绿。
- **安装器「修复模型」按钮**（用户要求：解决"读不到下载内容却一直下载"）：新增按钮 + CLI `--repair <dir>`，一次执行三步：
  1. **把程序文件更新到最新**（复用 `ExtractOverwrite`，绝不动 models/ 与用户产物）
  2. **扫描并整理模型目录**：把**平铺**在 `models/24yearsold/` 顶层的文件归位到正确 repo 子目录；同名文件（`model_index.json` / `LICENSE` 等两个 repo 都有）不靠名字猜，改用**大小匹配**唯一确定归属；清理 `.part` 残留与空目录
  3. **只补缺失/损坏的文件**（`DownloadModels` 本就跳过大小正确的，故只下缺的）
- **为什么会有"一直下载"**：这正是 v2.6.1 修的那个老 bug 的现场——旧 `DownloadModels` 漏了 repo 子目录，两个 repo 的同名文件互相覆盖，而 `HasModels` 查的是带子目录的路径 → 永远判定"未安装" → 每次安装都重下 5.3GB。修复按钮把这类历史装坏的目录一次收拾干净。
- **实测（模拟真实故障）**：造平铺文件 + `.part` 残留 → `--check-models` 报 `BAD=38` → `--repair` 输出 `UPDATED=325 ERR=0 / MOVED=2 BAD=36`（`model_index.json`+`LICENSE` 正确归位到 marigold 子目录、`.part` 已清、程序文件已更新、顶层空目录已删）→ 6/6 断言全 PASS。
- **版本号**：新增功能体系 -> minor+1 -> **v2.7.0**。
- 本次**未推送 GitHub、未上传 Release**（按要求）。

## v2.6.1（2026-11-11 13:50）自由变换实时预览 + 安装器模型完整性检查

- **自由变换实时预览**（用户反馈「变换时画面不是实时更新的」）：拖动控制点/移动/旋转、以及在选项栏输入宽高角度时，画面**立刻**跟着变换，不再只更新选项栏数字。实现：`redraw()` 里临时把正在变换的层替换成 `warpAffine` 结果再渲染，**渲染后立即恢复**（`try/finally` 保证异常也不污染图层数据），复用 `render_frame_rgba` 的合成逻辑（depth/叠放顺序全一致）。实测像素级验证：拖动中原图与预览差异 75606 px、**提交后与拖动预览差异 0 px**（所见即所得）；拖动中/释放后图层 `img`/`bbox` 均未被污染；取消/撤销均正确回原。
- **安装包模型完整性检查**（用户要求「更新时检查模型完整性，完整走老路，不完整覆盖坏模型重下」）：
  - 更新时新增 `FindBadModels()`：遍历 manifest 校验每个文件**存在 + 大小**，完整则明确提示「模型完整性检查通过，未改动」并**绝不触碰 models/**；不完整则**只重下坏的那些**（`DownloadModels` 本来就跳过大小正确的、覆盖重下不符的），修完复检。
  - 无网络时不静默失败，提示「检测到模型不完整但无法连接下载源，可稍后联网重跑自动修复」。
  - 安装界面勾选框文案区分三态：「模型完整（5.3 GB），无需下载」/「检测到 N 个模型文件不完整，本次更新将自动修复」/「未检测到模型…顺带下载」。
  - 新增 CLI 钩子 `--check-models <dir>`（输出 `COMPLETE` 或 `BAD=N` + 坏文件列表），便于自动化验证。
- **顺带修掉一个潜伏老 bug**：`DownloadModels` 的落盘路径把 `file` 直接拼到 `models/24yearsold/` 顶层，**漏了 repo 子目录**——而 `HasModels`、应用加载路径（`seethrough_local.py` 的 `MODELS_DIR + NF4_*`）、本机真实布局都是 `models/24yearsold/<repo后缀>/`。即全新安装勾选下载会**下错位置、应用加载不到**（本次加完整性检查时才暴露：完整目录被误报 BAD=38）。新增 `ModelDest()` 统一三处路径。
- **安装界面加最小化按钮**（用户要求「给最小化按钮，让用户可以在后台下载」）：`win.MinimizeBox = true`（最大化仍禁用），5.3GB 下载时可缩到任务栏后台跑。已用 Win32 确认 `MINIMIZEBOX=true`、最小化后窗口存活（`IsIconic=true`）。
- **实测**：`--check-models` 三场景全对——完整目录 `COMPLETE`(rc=0)、空目录 `BAD=38`、硬链接完整结构+截断 1 个文件 `BAD=1` 且精确指出文件名；自由变换实时预览像素级验证 3/3 + 安全不污染 8/8；四套回归（layer_menu / finisher / proj_nav / transform_test）全绿 rc=0。
- 本次**未推送 GitHub、未重打包公布**（按要求）。

## v2.6.0（2026-11-11 12:40）自由变换（Ctrl+T）

- **自由变换**（编辑菜单 + `Ctrl+T`）：对**当前选中图层**开启 PS 式交互式变换框——拖 8 个控制点缩放（角点=对侧角固定，Shift 等比；边点=单轴）、框内拖动=移动、框角外拖动=旋转；`Enter`/双击提交，`Esc` 取消。提交用 `cv2.warpAffine` 重采样图层像素并按 alpha 收紧 bbox（引擎零改动，渲染/PSD/AE/JSON 导出自动正确），可撤销、首次变换保留原图（右键「还原原图」可用）、锁定层拒绝。
- **变换状态模型**（新模块 `transform_tool.py`，纯函数无 Qt 依赖）：`{cx, cy, hw, hh, ang}` 中心+半宽高+旋转角（图像全分辨率坐标）。8 控制点、命中测试、三种拖拽、仿射求解全从这 5 个数推导，且始终是仿射（矩形→平行四边形，三点定 `getAffineTransform`）。
- **选项栏三态**：画布上方选项栏按模式切控件组——套索模式显示「羽化/曲线/重新应用」，自由变换模式显示「宽/高/角度」输入框 + ✓提交/✕取消（PS 在选项栏输入数值的行为，拖动控制点时输入框实时回填）；都不激活时整条隐藏。`_ft_spin_syncing` 防 spin↔画布死循环。
- **两个真 bug 修复**：
  - `warp_state` 返回 bbox 时把原点 `(x0,y0)` **多加了一次**（ox/oy 已是图像坐标）——提交后图层会跳位 `(原bbox.x0, 原bbox.y0)`。由队友（ui-integrator2）写的独立回归 `transform_test.py`（36 项）抓到 12b「恒等变换位置不变」，我此前的测试只查尺寸没查位置所以漏过——刚体平移下尺寸/面积/alpha 全对，只有位置断言能命中。已修：`return sub, (ox+nx0, oy+ny0, ox+nx1, oy+ny1)`。
  - `ft_commit` 先 emit 后清状态，槽里刷新选项栏时 `ft_active()` 仍为 True → 提交后选项栏不隐藏。改为先清 `_ft` 再 emit。
- **附带改进**：图层选中变化（`_on_current_layer_changed`）即时刷新编辑菜单可用性（自由变换/图层补洞项），不再只靠菜单打开时刷新——快捷键路径不经过菜单，必须实时跟上。图层补洞的启用判定从「alpha==0 像素数≥16」改为「`find_enclosed_holes` 找到被包围的洞」，与执行逻辑一致（bbox 角上的外部透明不再误触发）。
- **编辑菜单现为 10 项**：撤销/重做 ─ 复制/剪切/粘贴/反选/取消选择 ─ 图层补洞 ─ 自由变换 ─ 拆分图片。两处旧断言（layer_menu_test 22c、finisher_test C3）同步更新为 10 项。
- **实测**：自由变换 UI 专项 26/26（菜单项/进入退出/状态初始化/选项栏三态切换/拖角放大/选项栏回填/Enter 提交/像素放大/bbox 位置正确/渲染/撤销/旋转/Esc 取消不改层/锁定拒绝/输入宽高角度生效/切换选中刷新）+ 可见性回归 8/8 + `transform_test.py` 36/36（纯函数层：状态/四角/控制点/命中/三种拖拽/恒等·放大·旋转·退化·不改输入·洞图 alpha 传播/组合/叉积面积）；四套回归全绿 rc=0。截图核对：白色虚线框 + 8 方块控制点 + 中心锚点，选项栏「宽 696 / 高 746 / 角度 0」实时显示。
- **版本号**：新增功能 -> minor+1 -> **v2.6.0**。
- 本次**未推送 GitHub、未重打包安装包**（按要求）。

## v2.5.0（2026-11-11 11:55）曲线套索 + 取消选择 + 图层补洞（Mocha 集成第一批）

- **曲线套索（PS 曲线套索）**：选项栏新增「曲线」开关（自绘 S 形贝塞尔图标 `make_curve_icon`）。开启后圈选的控制点之间用 **Catmull-Rom 样条**加密（每段 14 采样点，`lasso_tool.expand_curve_points`），选区边界变成光滑曲线——用 4 个点就能圈出圆润轮廓。闭合后画布显示平滑曲线轮廓 + 空心圈标出控制点（可继续拖动调整）；预览与掩码走同一份加密点集（`selection_points`），不会出现"预览是曲线、裁剪是折线"的不一致。与加选/减选/羽化/反选/复制/剪切/粘贴/应用裁剪全部兼容（选区 dict 多个 `curve` 标记，所有掩码生成处统一走 `selection_points`）。实测：4 控制点圈发丝，折线 106141 px vs 曲线 144377 px（平滑外扩，符合预期）。
- **「取消选择」菜单项**（编辑菜单，`Ctrl+D`）：清除选区（含反选标记），与 PS 一致——无选区时置灰。
- **图层补洞（Mocha Remove 模块简化版）**：编辑菜单新增「图层补洞」。选中图层后，自动找**被不透明像素包围的透明洞**（`find_enclosed_holes`：连通域分解，触边的透明分量=外部不填，不触边且 ≥16px 的才是洞），用 `cv2.inpaint`（Telea）从周边像素填 RGB、洞内 alpha 置 255；洞外逐字节不变、可撤销、锁定层跳过、无洞时菜单项置灰并提示。用途：移动/剪切图层后底下露出的空洞一键补上。新依赖：无（cv2 5.0.0 已具备）。实测：合成层 30×30 洞 900px 全填满、撤销精确回到带洞基线、纯外部透明不误填。
- **协作**：`lasso_tool.inpaint_holes` 纯函数由队友（ui-integrator2）实现并自测 11/11；`holes` 可选参数（调用方传连通域掩码，区分洞/外部）接口对齐后接线。
- **编辑菜单现为 9 项**：撤销/重做 ─ 复制/剪切/粘贴/反选/取消选择 ─ 图层补洞 ─ 拆分图片。两处旧断言（layer_menu_test 22c、finisher_test C3）同步更新为 9 项。
- **实测**：曲线套索专项 19/19（开关同步/curve 标记/样条加密 56 点/面积对比/取消选择/Ctrl+D/洞检测 4 例）+ 补洞专项 5/5（基线/填满/洞外不变/撤销×2）；三套回归全绿 rc=0。
- **版本号**：成体系新功能（Mocha 能力集成第一批）-> minor+1 -> **v2.5.0**。
- 本次**未推送 GitHub、未重打包安装包**（按要求）。

## v2.4.0（2026-11-11 10:55）套索图标 PS 化 + 编辑菜单选区四项

- **多边形套索图标**：改用**自绘 PS 风格图标**（`make_lasso_icon()`）。qfluentwidgets 没有套索图标，之前借用 `FluentIcon.EDIT`（铅笔）。现在画的是「直线段围成的套索环 + 收口甩出的绳尾 + 起点方块标记」，即 Photoshop 多边形套索的视觉特征；256px 渲染由 Qt 平滑缩放，深色工具栏上用浅灰 `#dcdcdc`。
- **「编辑」菜单新增选区四项**（PS 语义）：复制 `Ctrl+C` / 剪切 `Ctrl+X` / 粘贴 `Ctrl+V` / 反选 `Ctrl+Shift+I`。
  - **复制/剪切/反选需要激活的选区**：菜单每次打开按选区状态刷新可用性（`_sync_edit_menu_state`），无选区时置灰——与 PS 一致。粘贴按剪贴板是否有图像启用。
  - 菜单顺序：撤销 / 重做 / ─ / 复制 / 剪切 / 粘贴 / 反选 / ─ / 拆分图片。
- **反选（选择反向）**：`lasso_tool.combine_masks(..., invert=True)` 把合并掩码取反——选区变成「多边形之外的全部区域」，之后复制/剪切/应用裁剪都作用在未被圈住的部分；羽化在反选**之后**做，软边才自然。画布上用**橙黄虚线描出画布边框**表示「框外已选中」，再点一次可切回（`act_invert_selection` 是开关）。选区清空时反选标记自动复位。
- **实测**：专项 18/18（图标非空且有内容、菜单七项齐全且顺序正确、无选区三项禁用、有选区三项启用、反选掩码 380716 = 总像素 449722 − 正常掩码 69006 分毫不差、反选可切换、反选提取成功）+ 真实快捷键 Ctrl+Shift+I 生效 + 反选视觉截图核对；三套回归全绿（layer_menu_test 两处旧断言"编辑菜单恰 3 项"已同步更新为 7 项）。
- **版本号**：新增功能 -> minor+1 -> **v2.4.0**。
- 本次**未推送 GitHub、未重打包安装包**（按要求）。

## v2.3.0（2026-11-10 21:45）套索选区状态 + 复制/剪切/粘贴（PS 风格）

- **套索改为 PS 式两段操作**：闭合选区 -> **进入"选区激活"状态**（画布显示选区边框，可继续加选/减选、拖顶点调整形状），**不再立即裁剪**。
  - `Ctrl+C` 复制选区内容 / `Ctrl+X` 剪切（同时从原层抠掉）/ `Ctrl+V` 粘贴为**顶部新图层**（列表顶=最前，名字 `pasted`/`pasted_2`…）
  - `Delete` / `Backspace` / `Enter` 才**真正裁剪**原图层；`Esc` 取消选区
  - 粘贴的位置=剪贴板记录的原位置（越界自动夹到画布内），可粘到其它程序（PNG 进系统剪贴板），也能从别的程序粘图进来（无元数据时按"Image"图层处理）
- **剪贴板带元数据**：PNG 之外用自定义 MIME `application/x-muppet-layer` 存 tag/node/depth/尺寸/位置，粘贴时恢复成同类型图层。
- **选区状态单一真源**：状态存在 Workbench（`_selection_data`/`_selection_active`），画布只负责绘制、通过 `selection_changed` 信号同步。**曾因两边各存一份导致复制/剪切/应用全部无效**（画布有选区但 Workbench 不知道）。
- **新增 `lasso_tool.extract_selection(layer, selections, feather)`**：取出选区内容但不裁原层（复制/粘贴用），与 `clip_layer_by_mask`（裁剪用）分工明确。
- **快捷键**：Ctrl+C / Ctrl+X / Ctrl+V / Del / Backspace / Enter / Esc（QShortcut 注册在 MainWindow）。
- **实测**：真实鼠标流程 8/8（双击闭合进入选区态、闭合不立即裁剪、Ctrl+C 剪贴板有图+元数据、Ctrl+V 新增顶层图层且选中、Ctrl+X 原层被剪小、Delete 应用裁剪、应用后清选区、粘贴层可被移动工具拖）；三套回归全绿 rc=0。
- **版本号**：新增功能体系 -> minor+1 -> **v2.3.0**。

## v2.2.1（2026-11-10 21:35）羽化控件移到画布上方选项栏（PS 风格）

- **羽化控件位置**：从左侧竖排工具栏移到**画布上方的横条选项栏**（PS 的选项栏位置），内容 = 「羽化 [数字框] px」+ 右侧「✓ 重新应用选区」按钮。**未选套索工具时整条隐藏**（`setVisible`，不占空间）。
- **工具栏回到 44px**：只留移动 / 套索两个图标按钮（之前为塞下羽化控件被撑到 72px，且标签竖排错位）。
- **修 NumSpinBox 数字显示问题**：① `setFixedHeight(26)` 会把 33px 高的内部 editor 压扁并错位（实测 editor 变成 20x33 且 y 偏移 32）——改用 setMinimum/MaximumHeight(33)；② qfw `SpinButton` 默认较宽（两个约占 60px），把 editor 挤成 29px 只能露出「0」——箭头改 `setFixedWidth(16)`，editor 恢复到 54px，数字完整可见。
- **版本号**：UI 调整 -> patch+1 -> **v2.2.1**。

## v2.2.0（2026-11-10 21:20）套索 L2 实用版 + 复制图层

- **多边形套索 L2**（在 v2.1.0 的 L1 基础上）：
  - **加选/减选布尔**：Shift = 加选（默认）、Alt = 减选；画布侧累积已闭合选区，提交时一次性计算 `掩码 = 所有加选区并集 - 所有减选区`（`lasso_tool.combine_masks`）。加选=蓝色实线边框，减选=红色虚线（PS 配色）。
  - **羽化**：工具栏加"羽化"数字框（0..20 px），对掩码做高斯模糊得到 0..255 软边 alpha。
  - **保留原图/还原**：首次裁剪时把原始 img/bbox 另存到 `_orig_img`/`_orig_bbox`，右键菜单出现「还原原图」可恢复；也支持在原图基础上多次选取（每次裁剪继承原图备份）。
  - **选区可视化 + 顶点可拖**：已闭合选区绘制边框，**拖动顶点可调整形状**，点工具栏 ✓ 按钮（重新应用选区）重新裁剪。
- **新增「复制图层」**：图层右键菜单里（拆分此层之后、删除之前）新增「复制图层」。复制品紧邻源层**上方**（列表顶=最前），名字 `原名_copy`（重名则 `_copy_2`…）；img 共享同一份 numpy 数组（不 deepcopy，图片数据不修改），tag/node/depth 照抄；visible 同步、locked 默认 False；draw_order 与 rig['_layers'] 同步；复制后自动选中，可直接用移动工具拖动；可撤销。
- **工具栏加宽 44→72px**：容纳"羽化"标签 + 数字框 + 重新应用按钮。
- **实测**：L2 专项 12/12（布尔减选挖空验证、羽化软边像素、保留原图+还原、加减选提交、羽化控件同步、顶点命中/空白不命中、重新应用生效）+ 复制图层 12/12（层数+1、img 同引用、紧邻源层上方、visible 同步、locked=False、draw_order/_layers 同步、选中、二次复制 _copy_2、撤销恢复、可命中、菜单项顺序=置顶/重命名/还原原图/锁定/拆分/复制图层/删除）；四套回归全绿 rc=0。
- **版本号**：新增功能 -> minor+1 -> **v2.2.0**。

## v2.1.0（2026-11-10 20:15）多边形套索工具（L1）

- **新工具**：工具栏加「多边形套索」按钮（与移动工具互斥）。用法：图层面板选中一个图层 -> 点套索按钮 -> 画布上左键逐点圈选 -> 双击或 Enter 闭合 -> 该图层被裁剪成"只剩选区内像素"。Esc 取消。画布上用蓝色虚线折线 + 顶点显示当前选区。
- **原理（引擎零改动）**：图层内容 = "img"（按 bbox 裁剪的 uint8 RGBA）+ "bbox"（摆放坐标）。选区多边形 -> cv2.fillPoly 生成掩码 -> img 的 alpha 与掩码相乘 -> img 裁到选区与 bbox 的交集、bbox 相应收紧。渲染/导出（PSD·AE·JSON·MP4·GIF）全部自动正确——它们只认 img+bbox。新增模块 `lasso_tool.py`（polygon_mask / clip_layer_by_polygon），与引擎解耦可独立测试。
- **坐标处理**：多边形点集用**图像全分辨率坐标**（与 layer['bbox'] 同一坐标系），画布显示的是 LOD 帧 -> 全分辨率→LOD→widget 两级换算（`ox + px*sc*s`）。曾因少乘一次 sc 导致顶点错位（实测选区点算出来 1034 而非 259），已修。
- **可撤销**：裁剪用**新 dict 替换**图层（不原地改 img）——历史快照存的是对象引用，原地改会让旧快照看到新内容、撤销失效（与图层改名同一套路）；裁剪后 `rig.pop('_lite')` 作废 LOD 缓存。
- **工具互斥自己实现**：**不用 QButtonGroup 的 exclusive 模式**——它会吞掉 setChecked(False)，导致无法取消工具回到"左键拖骨架支点"（回归测试 24f 直接抓到）。改为 `_on_tool_toggled` 手动维护互斥。
- **双击闭合去重**：双击最后一点时鼠标按下已加过一个重复顶点，闭合时去掉末尾重合点（阈值 0.5px）。
- **L1 范围说明**（未做，可后续加）：加选/减选（布尔运算）、羽化、蚂蚁线选区、还原原图、套索自动吸附边缘、跨图层布尔运算。
- **实测**：套索专项 13/13（裁剪面积比、无交集/点数不足返回 None、原层不被修改、bbox 与 img 一致、信号路径、渲染、撤销恢复）+ 交互路径 3/3（点击加点/Esc 取消/Enter 闭合/双击闭合）+ 工具切换 6/6；四套回归（layer_menu 51 项 / finisher / proj_nav / rename）全绿 rc=0。
- **版本号**：本次是新增功能，按规则 minor+1 -> **v2.1.0**。

## v2.0.2（2026-11-10 19:44）图层面板 PS 化 + 性能优化 + 版本号机制

- **图层面板 PS 化（QStyledItemDelegate 委托绘制）**：行结构 = 眼睛(24px) | 缩略图(36px，棋盘底) | 名称+tag | 锁标。眼睛单击切显隐、行尾点击切锁定（锁定层移动/删除/拆分/置顶全部跳过并提示「图层已锁定」）、双击缩略图区进重命名。
- **图层重命名**（_rename_layer / delegate 的 setModelData 调用）：清洗非法字符 \ / : * ? " < > |、限长 60、重名自动 _2/_3 后缀；同步 layers 字典的 name、visible/locked 的**键**、rig['draw_order']、rig['nodes'][*]['layers']、当前选中态。改名可撤销。
- **History 快照补 names 键**：改名和移动一样是**原地改图层属性**，对象引用快照回退不了。快照新增 "names": [每层名]（按**下标**存，名字自己会变不能当键），apply 时按**对象 id** 对应回原名，并同步 visible/locked 的键迁移；bboxes 的键也从层名改成对象 id（改名会让层名键失效）。**教训：对象引用快照方案里，任何"原地改图层属性"的工具都必须补值记录**。
- **性能：隐藏/显示全部图层卡顿已修**（用户反馈「点击隐藏所有图层后画布要好久才能更新」）。三处根因与修法：① _toggle_all 原来调 
eload_widgets() 重建全部 item（clear + 17 次新建 + 17 行重绘）→ 改成**只刷行**（viewport.update()）；② delegate 的 _thumb 返回原图尺寸 pixmap、paint 时每次都 scaled(SmoothTransformation) 重缩放（637x706→32x32，17 行 = 17 次缩放）→ 改为**直接缓存缩放后的小图**；③ paint 里 
ext(l for l in wb.layers ...) 线性查找（17 行 × 17 层 = 289 次比较）→ 改为 wb.layer_by_name() 的 O(1) 字典索引。**实测真窗口：平均 64.6ms → 23.6ms，最大 97.8ms → 33.4ms**。
- **修一个真 bug**：delegate 的眼睛点击调 wb.toggle_visible(name)，但 Workbench **根本没有这个方法** → 点眼睛会 AttributeError 崩溃。已补 	oggle_visible()（切 visible + 只刷行 + redraw）。
- **移动工具空白区可拖**：实测图层 bbox 内有 20%~71% 是透明像素（按 alpha 判定会大片拖不动），且画布上不在任何 bbox 内的空白处也应当能拖当前选中层（PS 拖已选对象的手感）。改为：**只要有选中层且它可见未锁定，画布任意位置按下都拖它**。
- **锁定第二道防线**：之前只有画布 hit-test 拦锁定层，绕过 hit-test 直调 _on_move_grab/_on_move_drag 能拖锁定层（qa-runner 指出的边界）。现在两个槽函数内部也检查 locked。
- **版本号机制**：单一版本源 = l2d_rig_qt.py 的 VERSION 常量（窗口标题 "木偶工作室 v" + VERSION 引用它），README 同步；此后每次小更新 patch+1，成体系大功能 minor+1。
- **测试**：layer_menu_test 44 项 / finisher_test 全项 / proj_nav_test 全绿 rc=0；qa-runner 独立适配了移动工具新语义的断言（24c2 系列 + 24h 锁定），并如实报告了锁定防线的边界。

## 版本号规范

- **单一版本源**：`l2d_rig_qt.py` 的 `VERSION` 常量，窗口标题引用它。
- **递增规则**：每次小更新 patch+1（v2.0.1、v2.0.2…）；成体系大功能 minor+1；破坏性/架构级 major+1。
- **CHANGELOG 新节标题格式**：`## vX.Y.Z（日期 时段）主题`。
- 历史各节（2026-11-09~10）为 v2.0.0 开发期记录，不回溯补号。

## v2.0.1（2026-11-10 下午）工程化 + 新建弹窗 + 移动工具修正

- **新建弹窗加工程名**：「文件 → 新建」弹窗现在可输入工程名 + 画布宽高（1..8192，默认 1024×1024）。确认后立刻创建 `projects\<工程名>\` 目录、写占位 rig_project.json、并在**导航栏新增该工程项**（点它直接载入）；「工作台」按钮已从导航栏移除。保存时默认建议当前工程名，用户可改（改名会建新目录并同步导航项）。
- **NumSpinBox（修 qfluentwidgets 的坏控件）**：实测 qfw 1.11.3 的 `SpinBox`/`CompactSpinBox`/`DoubleSpinBox` **全部**把内部 QLineEdit 挤成 **1px 宽**（`QRect(0,1,1,31)`），数字区完全不可见、点不进——这就是用户报的"宽高输入框无法使用"。试过 `hBoxLayout.insertWidget(0, lineEdit)`：lineEdit 可见了，但 QSpinBox 失去对它的引用，`setValue()` 不刷新文本、手改文本也不回写 value，功能坏，否决。最终自研 `NumSpinBox`（qfw LineEdit + QIntValidator + 复用 qfw SpinButton 箭头、支持长按连发），对外接口 value/setValue/setRange 与 SpinBox 兼容。
- **移动工具语义**：改为**只拖图层面板当前选中层**（不再自动命中最上层）。且**图层的空白区域也能拖**——bbox 内的透明像素（实测图层 bbox 内 20%~71% 是透明区）可拖，画布上不在任何 bbox 内的空白处按下也拖当前选中层（PS 拖已选对象的手感）；隐藏层/锁定层不可拖。
- **拆分图片入口移到「编辑」菜单**（文件菜单 10 项、编辑菜单 3 项）。
- **空画布拖图自适应**：没建过工程时直接拖图 → 画布尺寸=图片尺寸、工程名=图片文件名（中文保留）、自动建工程目录+导航项；已有画布时旧行为不变（超尺寸图等比缩放居中）。
- **修一个潜伏 bug**：菜单改版时 `em.aboutToHide.connect(lambda: self._menu_about_to_hide(exp))` 残留引用已删除的 `exp` 变量，aboutToHide 触发即 NameError（此前侥幸不炸是因为 RoundMenu.close() 不发该信号）。
- **测试**：finisher_test 37 项 / layer_menu_test 39 项 / proj_nav_test 全绿 rc=0；SpinBox 真窗口截图确认真实渲染（非 offscreen）。
- **版本号规范**（用户新定规矩）：此后每次小更新都要递增版本号（patch+1，如 v2.0.1、v2.0.2；成体系大功能 minor+1）。单一版本源 = l2d_rig_qt.py 的 VERSION 常量，窗口标题引用它；CHANGELOG 新节标题用 `## vX.Y.Z（日期 时段）主题`。历史各节（2026-11-09~10）为 v2.0.0 开发期记录，不回溯补号。

## 2026-11-10（下午3）新建弹窗：自定义画布尺寸

- **用户需求原文**：「文件下拉菜单选择新建后，需要弹窗让用户输入长和宽(像素)，根据输入创建画布尺寸」。
- **实现**：新增 `NewCanvasDialog(MessageBoxBase)`——标题 + 说明 + 两行 `BodyLabel` + `SpinBox`（宽/高，range 1..8192，默认 1024×1024），yesButton 文字「创建」。`act_new(ask_size=False)`：菜单触发传 `True` 弹窗，取消则直接 `return` 保持当前工作台不动；启动初始化 `act_new(False)` 不弹（**这个默认值是有意的**：任何遗漏的无参调用都不会阻塞）。弹窗结果存 `wb._new_canvas = (W, H)`，`_drop_add_layer` 建 rig 时消费（优先用它，没有则沿用首图尺寸=旧行为），消费后置 `None` 防止后续误用。
- **踩坑两个（重点记）**：
  - **对话框方法绝不能命名 `size()`**——会覆盖 `QWidget.size()`，而基类 `MaskDialogBase.__init__` 第 28 行内部就调 `self.size()`，那时 `w_spin` 还没创建，直接 `AttributeError`。已改名 `canvas_size()`。
  - **offscreen 测试弹窗不能用 `app.activeModalWidget()` 找对话框**——不可靠（定时器找不到对象 → `exec` 永远等待，实测卡死 10 分钟被 kill）。可靠做法：monkeypatch 对话框类为 Spy 子类，`__init__` 里把实例 append 进列表，定时器里对 `captured[-1]` 设值并 `yesButton.click()`。另外 `MaskDialogBase.done()` 有 100ms 淡出动画依赖事件循环推进，click 后要多 `processEvents`。
- **MessageBoxBase API 备忘（1.11.3）**：无标题属性，用 `viewLayout.addWidget(SubtitleLabel(...))` 自建；`validate()` 返回 `False` 可阻止关闭（本次没用上）；`SpinBox` 注意是 **B 大写**（`Spinbox` 导入会报错）。
- **测试**：弹窗专项 9/9（启动不弹 / 确认后 `_new_canvas` / 首图后画布=指定尺寸 / bbox 在画布内 / 取消不动 / 默认 1024 / 上下限钳制）；`layer_menu_test` 32 项 ALL PASS rc=0。
- **打包**：本轮 Setup 由 Lead 重打包。

## 2026-11-10（下午2）PS 风格工具栏 + 移动工具 + 去箭头

- **用户需求原文**：「顶层按钮下拉小箭头去掉」+「左侧导航栏右侧加一列工具栏，参考 Photoshop，先内置一个移动按钮，选中后可移动选定图层」。
- **去箭头**：`ToggleDropDownButton` 类内覆写 `_drawDropDownIcon(self, painter, rect): pass`（基类 `DropDownButtonBase.paintEvent` 调它画右缘 10×10 箭头）。**只影响标题栏「文件」「编辑」两个按钮**，`RoundMenu` 菜单项自己的箭头不动。冒烟 9/9（含 `grab()` 触发 `paintEvent` 验证覆写被调用）。
- **工具栏布局**：`Workbench` 左列改为 `canvas_row = [tool_bar(44px 竖排, #252a33 圆角) + canvas]`，位于导航栏右侧、画布左侧（视觉顺序：导航｜工具栏｜画布）。先内置一个按钮 `btn_move`（`TransparentToggleToolButton` + `FluentIcon.MOVE`），toggle 语义。
- **关键设计决策**：
  - **移动 = 原地改 `l["bbox"]`**（bbox 即摆放坐标，img 是裁剪像素）——渲染/导出/PSD/AE/JSON 全链路自动正确，**零 `l2d_rig_core` 改动**。
  - **History 快照新增 `bboxes` 值记录**：原快照存对象引用，原地改 bbox 无法回退——这是对象引用快照方案的死角，**以后再加任何原地改图层属性的工具都要记得这点**；`push` 的「无变化不压栈」判断同步纳入 bboxes。
  - **默认工具是 pan 不是 move**：保持原有「左键拖支点」行为不破坏（`btn_move` 默认不选中）。这点曾错过：一开始默认 `checked=True`，会把支点拖拽整个禁掉，属于行为回归。
  - **拖动语义（PS Move 工具）**：按下命中最上可见层（`_layers_ref` 按图层列表序，前=叠放顶）→ 立即选中该层（同步列表）→ 按住期间从按下点累计偏移（避免逐帧增量误差）；中键/空格平移在任何模式下保留。
  - **canvas 不直接碰 layers 结构**：通过 `move_grabbed` / `move_dragged` / `move_released` 三信号回 `Workbench`，`redraw` 时 `set_layers` 同步命中数据 `[(name, bbox, visible)]`。
- **顺带修的潜伏 bug**：菜单改版时 `em.aboutToHide.connect(...(exp))` 残留引用已删除的 `exp` 变量，`aboutToHide` 触发会 NameError（此前侥幸不炸是因为 `RoundMenu.close()` 不发该信号，看门狗路径才发）。已改 `edit`。
- **测试**：移动工具冒烟 10/10（默认 pan / 激活 move / 命中 / 平移 / 越界保宽高 / 撤销重做 / 列表同步）；回归脚本由 qa-runner 并行适配（comfy_route 的 V1 selftest 断言因 V1 退役改 SKIP；layer_menu 渲染阈值 +20→+40ms，因基线随机器负载漂移 25→56ms）。
- **打包**：本轮未重打包 Setup（等 qa 全绿后 Lead 统一提交打包）。

## 2026-11-10（下午）顶层菜单改版：文件/编辑双按钮

- **用户需求原文**：「顶层菜单按钮改名为'文件'，去掉导出按钮，将导出按钮内的功能合并到'文件'按钮中。在文件按钮旁边新增'编辑'按钮，将文件按钮中的撤销和重做功能挪到编辑按钮里」。
- **最终结构**：标题栏 = 折叠按钮 + 「文件」 + 「编辑」。
  - 「文件」菜单顺序：新建 → 打开工程 → 最近打开（子菜单）→ 导入 PSD → 拆分图片（SeeThrough）→ ─分隔─ → 保存 → 另存为 → ─分隔─ → 导出 PSD → 导出 AE 脚本 → 导出 JSON → 导出 MP4 → 导出 GIF。
  - 「编辑」菜单只有两项：撤销 Ctrl+Z → 重做 Ctrl+Y。
- **实现要点**（`l2d_rig_qt.py _build`）：原 `exp`（`ToggleDropDownButton`「导出」）删除，其 5 个 Action 直接 `addAction` 进 `m`（文件菜单）；撤销/重做两个 Action 从 `m` 移到新 `RoundMenu em` 并挂 `edit_btn`。对象名 `wb.exp_btn/exp_ref` → `wb.edit_btn/edit_ref`。
- **坑（必看）**：`_dropdowns` 看门狗列表要同步换成 `(menu, m), (edit, em)`——**漏掉这个的话编辑菜单不会自动收起**（光标移出 250ms 收起机制按这个列表管理）。`MainWindow` 标题栏 `insertWidget(2, wb.exp_btn)` 改 `wb.edit_btn`。
- **快捷键不变**：Ctrl+Z / Ctrl+Y 仍是 `QShortcut` 直接绑 `act_undo / act_redo`，与菜单位置无关。
- **测试**：冒烟 4 项全过（文件菜单内容 / 编辑菜单内容 / 标题栏控件序列 / 撤销+导出功能仍正常）；回归适配由 qa-runner 并行（`layer_menu_test` 中引用旧菜单结构的断言需按新结构改）。
- **打包**：本轮未重打包 Setup（纯 UI 改动，等 qa 回归全绿后 Lead 统一打包提交）。

## 2026-11-10（凌晨）应用内集成 ComfyUI 路线（A 卡一键拆图）

- **动机**：A 卡无 CUDA，进程内 nf4 引擎（bitsandbytes 专属）跑不了。新增 `comfy_decompose.py`，通过本机 ComfyUI 的 HTTP API 提交 See-through 插件工作流，A 卡用户应用内一键拆图，体验对齐 N 卡。
- **引擎自动选路** `l2d_rig_qt.py _engine_mode()`：有 CUDA→local（进程内 nf4，最快）；无 CUDA→comfy（`quant_mode=none` 即 bf16，`auto_download=True`）。环境变量 `MUPPET_ENGINE=local|comfy` 可强制（N 卡想跑全精度更高画质时用 comfy）；`COMFY_API` 可指定服务地址（默认试 `127.0.0.1` 的 8188/8000/8189）。
- **关键事实（别再搞错）**：插件 v0.2.2 的 `SeeThrough_SavePSD` 节点**不产出 PSD**，它写分层 PNG + `*_layers.json`，PSD 是插件前端 "Download PSD" 按钮在浏览器里用 ag-psd 合成的。我们要的正是 PNG+json，与 `load_layers` 契约一致，所以根本不需要 PSD 中间格式。安装器旧指引里"工作流末尾导出分层 PSD"那句是错的，已改。
- **收集方案（零目录依赖）**：`/view?filename=seethrough_psd_info.log&type=output` 拿到本次 json 名 → 再 `/view` 拿 json → 逐层 `/view` 取 PNG。好处是**不需要知道 ComfyUI 装在哪**，官方绿色版/Desktop/自装版通吃；上传用 `/upload/image`，同理不猜 input 目录。
- **归属双校验（踩坑）**：`/history` 拿不到 SavePSD 的 json 路径——实测本次成功 prompt 的 outputs 是空 `{}`（ComfyUI 只把注册了 ui 的图像类输出写进 history）。而插件写的 `output/seethrough_psd_info.log` 是"最后一次跑"的状态 → `comfy_decompose.collect()` 已加**归属双校验**：json 里的 `prefix` 必须等于本次前缀、`timestamp` 不得早于提交时刻（留 120s 余量），任一不符就放弃收集并提示"ComfyUI 可能被别的任务同时使用"，绝不静默导入别人的分层。实测三分支：正确前缀收 29 层 / 错前缀拒绝 / 未来 since 判陈旧拒绝，全对。
- **节点签名 ground truth（实测自本机 `custom_nodes/ComfyUI-See-through/nodes.py` v0.2.2，非网查）**：`QUANT_MODES=["none","nf4"]` 默认 none；`DEFAULT_LAYERDIFF_REPO=layerdifforg/seethroughv0.0.2_layerdiff3d`、`DEFAULT_DEPTH_REPO=layerdifforg/seethroughv0.0.1_marigold`（**注意深度模型默认 repo 是 layerdifforg 不是 24yearsold**）；nf4 版是 `24yearsold/seethroughv0.0.{1_marigold,2_layerdiff3d}_nf4`。`_resolve_model_path` 支持 "org/repo" 写法按 basename 回退到本地目录。
- **端到端实测数据（N 卡机器用 junction 复用应用已下好的 nf4 模型，零下载）**：推理 733s、产出 29 层 + 58 张 PNG（含 depth）、`load_layers` 读出 29 层 1024×1024、`build_rig` 得 11 个骨架节点、渲染非空 0.230。进度估算用的 700s 线性模型正好贴合。
- **零下载测试法（N 卡机器验证 comfy 链路用）**：`New-Item -ItemType Junction D:\ComfyUI\main\models\SeeThrough -> D:\work\puppet\muppet_studio\models\24yearsold`，插件即可复用应用已下好的 nf4 模型（它们是标准 diffusers 目录，含 model_index.json），不必下 12.5GB。ComfyUI 侧解析模型目录走 `folder_paths.get_folder_paths("SeeThrough")`。
- **体量事实**：SeeThrough 全精度模型 layerdiff3d 9.46GB + marigold 3.05GB = 12.5GB（是 nf4 5.31GB 的 2.4 倍），A 卡首跑由插件 `auto_download` 自动拉，可中断重跑续传。
- **顺带修的产品级 bug**：之前删"拆分图片"按钮后没留任何 UI 入口，导致应用**完全无法触发拆图（N 卡也受影响）**。已恢复为菜单「拆分图片（SeeThrough）」，并加 `#pct=` 前缀日志支持估算进度条（comfy 路线无中间进度可查，按 700s 线性估）。
- **打开工程/load_dir 兼容 ComfyUI 输出目录**：`find_meta()` 优先 `layers_meta.json`，否则取最新 `*_layers.json`，A 卡用户也可自己在 ComfyUI 跑工作流、再用「打开工程」直接选那个输出目录。
- **安装包侧决定**：保留「A 卡拆图支持」代下选项（ComfyUI 安装器 172MB + 插件 zip + 生成的 `A卡拆图指引.txt`），但**测试端不跑这条下载链路**（体积大、依赖外网镜像，用户也可自行下载，链接都在指引里）。GPU 自动选路线逻辑不变，N 卡用户流程零影响（Setup 49.28MB）。
- **事故记录（务必别再犯）：`COMFY_API` 被写成"优先"而不是"限定"**。测"ComfyUI 没开时该报错"这条分支，用 `COMFY_API=http://127.0.0.1:8` 指死端口，但 probe() 是 `[env] + [8188,8000,8189]` 依次试——死端口失败后**回退到本机真在跑的 8188**，于是本该失败的探测变成了真提交一个 quant=none 的拆图任务（GPU 100%、显存 7.8GB）。已改为：**设了 `COMFY_API` 就只连它，失败直接 None**（配置就该被尊重）。教训：测"服务不可用"分支时，绝不能让发现逻辑有回退余地；先单测 `probe()` 返回 None，才允许继续跑上层。
- **`/interrupt` 对 SeeThrough 节点无效**：插件的推理节点不在 interrupt checkpoint 上，连发 6 次 interrupt 任务照跑、队列照满。唯一止损手段是重启服务进程（记下原命令行 `D:\Python312\python.exe main.py --listen 0.0.0.0 --port 8188`、cwd `D:\ComfyUI\main`，kill 后按原样拉起，验证 `/system_stats` 与空队列即恢复）。
- **子代理三路交叉验证补出的事实**（zip 哈希 + 本机源码 + 实时 `/object_info`，与本机结论一致）：`QUANT_MODES=["none","nf4"]`；`use_lama` 是 **required** 不是 optional；`LoadImage` 的 API 输入**只有 `image`**（`upload` 是前端控件，已从图里删掉）；`auto_download` 落点是 **C 盘 `%USERPROFILE%\.cache\huggingface\hub`，不是 `ComfyUI\models`**（12.5GB 会吃掉 C 盘，指引已教用 `HF_HOME` 改盘、`HF_ENDPOINT=https://hf-mirror.com` 走镜像）；`use_lama=true` 还会经 torch.hub 额外下 `lama_large_512px.ckpt`，所以应用默认关；README 里 Marigold 写成 `24yearsold/…`（无 _nf4 后缀）不准确，以代码为准。
- **结构校验优先于跑推理**：拿 `/object_info` 逐节点比对"我们送的 key 是否都在 required/optional、required 是否有缺项"，比跑一次 13 分钟的推理更快也更硬（改一行图就能秒验）。已作为 comfy 路由单测的第 2 项固化。
- **qa-runner 独立验证**：`comfy_route_test.py` 42 项断言全绿（发现层/交叉校验/find_meta/引擎选路/worker 分流/无 CUDA 不拦截/py_compile），全程只发 GET，未碰 GPU 任务。
- **测试脚本自身两个缺陷已修**（都是我的锅，非产品问题）：
  1. `layer_menu_test.py` 不显式收尾 → Qt 在解释器退出阶段随机崩，退出码翻成 `0xC0000005`/`0xC0000409` 而断言其实全 PASS（qa 实测 4 次里 2 崩）。已加 `worker.stop()+w.close()+deleteLater()+processEvents()` 并 `sys.exit(0 if not fails else 1)`，让**退出码只反映断言结果**；连跑 3 次 rc 均为 0。
  2. 第 22 项渲染阈值 +15ms 贴线（高负载时 Δ 实测能到 16~20ms 误报 FAIL，降载后 Δ 9~15ms）。真回归量级远大于此，放宽到 +20ms；修完实测 Δ≈9-10ms 稳定通过。
  3. 口径提醒：该脚本 `ck()` 实际只有 **17 条**，标签编号跳到 23——"23 项"是最大标签不是条数，以后别按 23 计数。

## 2026-11-09（深夜4）AMD 卡支持 + 安装前 GPU 自动选路线

- **目标**：`"在安装前监测是 a 卡还是 n 卡,根据监测结果自动选择路线；改 a 卡代码影响 n 卡吗"`。
- **结论（先答用户）**：改 A 卡适配**不影响 N 卡**——三铁律：纯增量代码分支 + 运行时下载（不进 Setup 内嵌包）；GPU 检测只读 WMI 无副作用且异常回退 N 卡；双显卡**有 N 卡优先 N 卡**（本地引擎直接拆图），避免误下 172MB ComfyUI。Setup 实测仍 49.27MB，N 卡下载量不变。
- **`GpuVendor()`（installer.cs，CLI 钩子 `--gpu` 输出 `nvidia|amd|other|unknown | 名称`）**：只读 `Win32_VideoController` 的 Name/AdapterCompatibility，匹配 nvidia / amd|radeon|advanced micro；**双显卡有 N 卡 → 返回 nvidia**（不误勾 A 卡包）。此外留测试钩子 `MUPPET_FAKE_GPU=amd|nvidia|other`（签名内 return，仅测 A 卡 GUI 用）。
- **GUI 新增**：窗口 330→400px；显卡名标签 + `amdChk` 复选框。N 卡：复选框禁用+文案"当前为 N 卡可直接拆图"；A 卡：**自动勾选**+nf4 模型框置灰跳过（"A 卡不可用"）；其他/未知：默认不勾、提示按需勾选。
- **AMD 路线附件（`DownloadAmdExtras`，运行时下载到安装目录 `comfyui_setup\`）**：ComfyUI 官方安装器（`download.comfy.org/windows/nsis/x64`，实测 172.45MB，含 ROCm 支持，降级 dl.todesktop）+ See-through 插件（`ghfast.top` 镜像 master 分支 zip，实测 7.30MB）+ 生成的 `AMD_使用说明.txt`（Audit HIP SDK 只给官方链接不自动装；驱动/ROCm 依赖由 ComfyUI 首跑自拉）。E2E 实测两条路线 exit=0：N 路线无 comfyui_setup、A 路线 ComfyUI-Setup 172.45MB + 插件 7.30MB + 说明书 1195 字节。
- **新增通用断点续传下载 `DownloadToFile([mirrors], dest, minBytes, progress)`**：`.part` 断点+Range 续传，多镜像依次尝试，替代原先死板的单个 URL 下载。
- **显存/模型体量事实（实测，写进 CHANGELOG 勿再重查）**：`layerdifforg/seethroughv0.0.2_layerdiff3d` 全精度 **9.46GB(24 文件)**、`24yearsold/seethroughv0.0.1_marigold` **3.05GB(16 文件)**，合计 **12.5GB**（vs nf4 5.31GB）——A 卡路线体积是 nf4 的 2.4 倍。
- **拆图入口丢失 + 修复（关键回归）**：允许 local-only 后删了"拆分图片"按钮，当时没补 UI 入口→**应用完全无法触发拆图（N 卡也受影响）**。本轮修复：载入菜单加 `menu → 拆分图片（SeeThrough）`（`FluentIcon.ZOOM`），复连`_pick_image`；`start_decompose` 前加 `_cuda_available()`（惰性 cache `torch.cuda.is_available`，一次 import）拦截——无 CUDA（A 卡/核显）弹 A 卡引导 InfoBar 不启动 worker；有 CUDA 走原 `DecomposeWorker`。无 CUDA 拦截后编辑/删除/撤销/导出仍正常（PASS 5）。
- **测试**：`layer_menu_test.py` 第 22 项改中位数抗噪（本机 DSH/QQ 常态吃 CPU，单轮噪声达 20ms；实测干净 17 层 5 轮 25.4ms 极稳、加 2 层中位差仅 7.4ms<15ms，判定非回归）。全 23 项 ALL PASS。
- **待办**：维护者确认是否需要"应用内集成 ComfyUI 回退路径"（git 的 decompose.py 还在，但 workflow 须按 jtydhr88/ComfyUI-See-through 新插件重写；角色是 A 卡主路线而非 N 卡兜底）。

## 2026-11-09（深夜3）安装器"网络诊断"按钮

- 背景：用户问"下载时开着加速器怎么办，能否放刷新 IP 功能"。**程序无法自动刷新公网 IP**（路由器/光猫持有，本机无红拨权限；rasdial 仅 PPPoE 直连有效且需管理员），落地为**检测+指引+断点续传重试**三件套。
- `DiagnoseNetwork()`（installer.cs，CLI 钩子 `--diag`）：
  1. 虚拟网卡检测：描述含 tap/tun/leigod/biubiu/vpn/netease 或 **26.x 地址**（雷神 TUN 特征）→ 提示关加速器/加白名单；
  2. 镜像实测：Range 拉 1MB 计时，<0.5MB/s 报过慢并指引；
  3. **429 识别**：镜像限流时明确提示"红拨宽带/重启路由器刷新公网 IP，再点安装可断点续传"。
- GUI 按钮排改为 安装(90)/网络诊断(210)/退出(330)；下载失败弹窗也带同样指引。
- 实测首跑即抓到真问题：Famatech Radmin VPN 网卡在场 + hf-mirror 对本机 IP 429（高频探测触发）——两分支全部实战验证。

## 2026-11-09（深夜2）SAM 定案 mobile_sam + 占地盘算法 + 能力边界

- **权重选型**：vit_b 357MB 下载太慢（加速器干扰时 0.02MB/s）；改 **mobile_sam 38.8MB**（TinyViT，`build_sam_vit_t`），源 `https://hf-mirror.com/dhkim2810/MobileSAM/resolve/main/mobile_sam.pt`（26 秒下完）。已就位 `models\sam\mobile_sam.pt`，**计划直接塞进安装包**（不再走"勾选下载"）。新增运行时依赖 **timm**（tiny_vit_sam 需要，pip 已装 1.0.31）。
- **vendored HQ-SAM 改版三坑**（sam_subsplit.py `_load_sam` shim 已全修）：
  1. `predictor.py` 总给 encoder 传 `get_interm_embeds` 并解包二元组——TinyViT 不认该 kwarg，需包一层吸收；
  2. 包装时**必须捕获赋值前的 bound forward** 直接调，`_enc(x)` 会再进 `__call__`→`self.forward`（=包装本身）无限递归；
  3. vendored `TinyViT.forward` **本来就返回 `(features, interm)` 二元组**，包装原样透传，再包一层会让 decoder 收到 tuple 崩 `repeat_interleave`。
- **占地盘算法**（替换 IoU 去重）：SAM 掩码多粒度嵌套（整块包含部件），只去重会重叠（实测 topwear 覆盖 222%）。改为升序贪心：小掩码优先认领像素、大掩码拿剩余、散点并入最大块 → 子层互斥、并集≈原 alpha（实测重叠=0）。
- **能力边界（实测，别给用户过度承诺）**：back hair 5 块/topwear 6 块/face 3 块 OK（每层 ~1.5s GPU）；**front hair、eyewhite-l 拆不出**——93% 像素是单一主块+碎片，mobile_sam 无发绺语义（vit_b 同理）。UI 提示"未能拆出≥2有效块"如实反馈。
- 待办：把 mobile_sam.pt 打进安装包 zip（Setup 体积 12.85→~51MB）；安装器去掉 SAM 下载项。

## 2026-11-09（深夜）渲染预乘管线 + 别名根除 + 性能优化

- **预乘 alpha 管线**：`render_frame_rgba(..., premul=True)` 跳过出口除法直出预乘 RGBA，画布 `set_frame` 用 `Format_ARGB32_Premultiplied` 消费；GIF/导出仍走 straight（出口一次除法）。
  - 坑①：预乘累加必须带衰减 `C += src*a` 是错的，正确是 `C = C*(1-a) + src*a`（曾漏衰减致绿色不压暗）。
  - 坑②：`build_rig` 曾 `rig["_layers"] = layers` **直接别名**传入列表——对两者各 append 一次即产生重复引用（拆分/撤销连环错）。已改 `list(layers)` 拷贝；`_drop_add_layer` 同步改拷贝。凡见 `_layers"] = self.layers`（无 list()）即地雷。
- 画布绘制优化：棋盘格从逐格循环（~289 次 fillRect/帧）改 2x2 平铺 QBrush 一次 fill；缩小路径 FastTransformation（源本是 2x LOD，双线性无增益）。
- **机器噪声教训**：DSH 自身+QQ 常态吃 4 核，渲染计时波动 25→60ms。测试断言改**相对基线**（同场 load_dir 后对比，增量 <15ms），绝对阈值无意义。
- 回归资产：`D:\scripts\temp\layer_menu_test.py`（23 项：菜单结构/拆分/撤销重做/拖入/导出）、`zoom_edge_test.py`。

## 2026-11-09（晚）PS 布局 + 图层右键菜单 + 透明背景 + 再拆分 + 安装器双模式

**布局（l2d_rig_qt.py `_build`）**
- 改为 PS 风格两栏：左=画布整列（含播放条），右侧垂直 QSplitter：上=骨架节点表、下=图层列表；外层水平 splitter 比例 3:2，内层 280:320，均可拖。
- 「保存当前帧」按钮+方法已删（与 GIF 导出零耦合，ExportWorker 独立路径）。
- 「拆分图片」按钮已删；拆分能力仍在 DecomposeWorker/start_decompose，入口由 Lead 另行安排。

**图层右键菜单（`_layer_menu`）**
- 置顶 / 拆分此层（子菜单：基础拆分·连通域 / 智能拆分·SAM）/ 删除。
- 删除、拆分均可 Ctrl+Z 撤销。

**History 重构（关键）**
- 快照存**图层对象引用**（非名字），故删除/拆分可完整恢复；新增 `seed()` 载入后播基线。
- 不变式：undo_stack 栈顶==当前状态；undo=弹顶给 redo、应用新栈顶。push 前比较 id 列表防无变化压栈。
- apply 会恢复 layers/draw_order/_layers/node_layers/pivots 并 `rig.pop("_lite")`。

**透明背景**
- 引擎新增 `C.render_frame_rgba`（RGBA，背景 alpha=0）；`render_frame(..., bg=(0,0,0))` 保留为黑底合成兼容接口。
- 画布 `set_frame` 收 RGBA→ARGB32；`paintEvent` 图像区先铺 PS 棋盘格（#3a3f4a/#2c303a）再叠图。
- GIF 导出真透明：`_gif_frames` 255 色 FASTOCTREE + alpha<128→索引 255 + disposal=2。MP4 仍黑底（格式无透明）。
- 坑：`_blend_rgba` 必须 Porter-Duff 且 alpha 出口乘回 255——曾用 0-1 公式写进 0-255 通道，整帧 alpha=1 全黑。

**图层再拆分（模型不支持递归拆分，走启发式两级）**
- A `C.subsplit_layer(layer, min_area=400, dilate=3)`：alpha 连通域，子层名 `原-1/-2` 面积降序，<2 块返回 []。实测 SeeThrough 部件层多为单连通块→A 常返回 []，属正常。
- B `sam_subsplit.sam_subsplit_layer(layer, min_area=2000, max_parts=6)`：懒加载单例 SAM vit_b（顶层不 import torch），权重 `models\sam\sam_vit_b_01ec64.pth`（357MB，未在场 raise 中文提示）；amg 掩码∩alpha、IoU>0.9 去重。
- UI 接线 `_layer_subsplit` + `_apply_subsplit`（子层就地替换原层，保持列表位置）。
- 拖入图片=`_drop_add_layer` 新增图层（空工作台先建空 rig+seed 基线），不再触发拆分。

**安装器（dist\installer.cs → MuppetStudio-Setup.exe）**
- 双模式：更新（注册表 HKCU\Software\MuppetStudio\InstallPath + 常见位置探测旧装，校验标志文件，只覆盖程序文件，绝不动 models/ 与 seethrough_out/）/ 全新（选目录）。
- 更新时检测模型：缺则复选框启用「顺带下载」；下载前 `CheckLinkAvailable` 探测镜像（GET+Range 探针，不通弹窗告知、应用照常装）。
- CLI 测试钩子：`--detect / --update <dir> [--models] / --fresh <dir> [--models] / --probe / --probe-url <url>` + `MUPPET_SETUP_REPORT` 环境变量收报告。
- 守卫：拒绝盘根/Windows 本身及子树/System32/用户目录本身；覆盖写带 5 次重试（防杀软锁）；zip-slip 检查；Setup 在目标目录内则自复制到 temp 重启。
- 坑：.NET Framework 4.0 默认 TLS1.0，必须显式开 TLS1.2/1.3（纯 int 强转 SecurityProtocolType），否则镜像探测报 SecureChannelFailure。
- 坑：PowerShell here-string 里 `r'C:\'` 反斜杠贴引号会炸，写临时 .py 文件跑。

---

## 2026-11-09（下午）画布 PS 缩放 + 导航默认隐藏

- 画布 PS 式缩放：fit/manual、Ctrl+0/1/±、Ctrl+滚轮锚点缩放、空格/中键平移、左下倍率角标；`zoom_edge_test.py` 30 项。
- 导航栏默认隐藏，标题栏按钮唤出 140px（`setCollapsible(False)`+双层 setFixedWidth 钉宽；qfluentwidgets panel eventFilter 会自动 expand 到 322px 是根因）。
- 坑：offscreen 下 itemWidget 宽 312≠面板 140，文字有裁切风险，真实机器需肉眼确认或把宽度约束下推到绘制项。

## 2026-11-09（上午）打开工程/最近打开 + 原生 exe 启动器 + 安装包

- 菜单：打开工程（选目录）、最近打开（recent_projects.json，去重置顶上限 5，menu_opened 懒重建）。
- 启动器为 C# 原生 exe（launcher.ini 配 python/script，CreateNoWindow，失败弹框），bat/vbs 全删。
- 安装包：内嵌 zip+manifest，hf-mirror/HF 双镜像 Range 断点续传+大小校验；卸载器三层安全校验（标志+ini 内容+路径黑名单）selftest 18 PASS。

## 2026-11-08 前后 V2 主体成型

- SeeThrough 本地引擎（vendored `see-through/` + nf4 模型 `models/24yearsold/`）：铁律——加载后必须 `.to("cpu")+empty_cache`（bitsandbytes nf4 参数 from_pretrained 即落 GPU，4.8GB 常驻触发 WDDM 分页，症状 util 100% 功耗 31W）。全流程 774s。ComfyUI HTTP 为回退。
- 骨架引擎 `l2d_rig_core.py`：CA_TAG/NODE_OF_TAG/depth_for_name、build_rig、draw_order（远→近，列表顶=最前）、pivot 三模式、`_affine` 支持非均匀 sc_y；眨眼=eye+lid 底锚纵向压扁（`_blink_s` 周期 4.6s），brow 独立静止。
- 渲染性能 689ms→20ms：bbox 裁剪 uint8 RGBA + 子矩形 warp + `_lite_rig` 半分辨率 LOD 缓存（结构变更必须 pop _lite）；纯平移快速路径。
- 坑：cv2.warpAffine 裁剪源 M2=T(-r)@M@T(x0,y0)；cv2.transform 点集必须 (N,1,2) 否则整帧全黑。
- PSD 导入自动排序（语义深度表 TAG_DEPTH + tight-save + 空层剔除）；拖图即拆；导航进度百分比。
- blink 节点拆分（eye/lid/brow 三类）、eye 不再连眉缩放消失。
- GitHub: https://github.com/hajimi-114514-manbo/12d （models/ gitignore）。

## V1（l2d_rig，冻结）

- tkinter 骨架工作室，`selftest.py` 31 PASS 回归基线。任何 V2 改动不得触碰 V1 文件。

---

## 测试约定

- 不涉拆图的验证一律用 `示例工程\`（17 层+layers_meta.json），`Q.save_recent` 打桩防污染 recent_projects.json。
- offscreen 跑 Qt：`QT_QPA_PLATFORM=offscreen`，鼠标用 sendEvent press+release，`_cursor_pos()` 注入口。
- 回归三件套：zoom_edge_test(30) / V1 selftest(31) / Uninstaller selftest(18)。

## 2026-11-10（09:55） V1（骨架工作室 v1.0）退役归档 + 安装器迁入仓库

- **V1 退役**：D:\work\puppet\l2d_rig\（13 个文件，~700KB：l2d_rig_studio.py 主程序、旧 decompose.py、selftest.py、骨架工作室.exe、README_骨架工作室.md 等）本地目录已删除。删除前**先归档进本仓库 legacy/v1/ 并推送 GitHub**——用户原以为"上传到 GitHub 后"，实测仓库里只有 V2，先补归档才删，避免永久丢失。
- **安全性核查（删前做过）**：V2 引擎模型目录 seethrough_local.py MODELS_DIR **优先用自带 models\**（sam 38.8MB + nf4 5.3GB 都在 V2），../l2d_rig/models 回退只在自带目录缺失时触发——而 V1 的 models 目录早已不存在，回退本就是死代码。删除不影响任何功能。laowu_icon.ico/png V2 有自己的副本。grep 全库确认无 D:\work\puppet\l2d_rig 绝对路径引用。
- **注意**：qa 回归三件套里的"V1 selftest（31 项）"跑的是 l2d_rig\selftest.py，V1 删除后该项**永久不可再跑**（代码已归档在 legacy/v1/，需要时可从仓库恢复）。回归改为两项：layer_menu_test + Uninstaller selftest。
- **安装器源码迁入仓库**（原来只在本机 D:\work\puppet\dist，无版本控制，仓库恢复后装不出安装包）：dist\installer.cs（51KB，GPU 检测/AMD 下载/断点续传/指引）+ dist\重新打包.bat 移入 muppet_studio\dist\，与既有 Uninstaller.cs 同一约定；旧 D:\work\puppet\dist 已删除。Setup exe 原计划也提交进仓库，实测直连 GitHub 上传 49MB blob 两次被掐（12 分钟 0 进展 / 卡死 6%），小提交秒过——已从 git 摘出（gitignore），改为走 GitHub Release 资产分发；创建 Release 需要的 token 因凭据失效暂缓，可浏览器拖拽上传（Steam++ 加速下最简单）。
- **重打包.bat 两个坑（记牢）**：
  1. **.bat 必须纯 ASCII 注释**：cmd 按 ANSI(GBK) 读 .bat，UTF-8 中文注释会被误解析成命令（'uppetStudio-Setup.exe（exe' is not recognized...），原 bat 全英文注释所以从没出过问题；
  2. **不能用相对路径 ..\* 打包**：7z/cmd 会把它解析到 D:\ 根（去扫 Config.Msi/pnpm-store/.BIN），必须用 %~dp0 拼绝对路径 %APP%\*。
- **7z 排除清单新增 -xr!dist -xr!legacy**：dist 防止 49MB exe 被打进 zip（Setup 自包含变 ~100MB），legacy 防止退役代码随包分发。两者都实测生效（49.89MB → 49.29MB）。
