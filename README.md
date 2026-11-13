# 木偶工作室（Muppet Studio）

> **v3.2.2** —— 把动漫立绘自动拆成分层骨架并做动画（伪 L2D），同时内置剪映式视频编辑工作台的 PySide6 桌面应用。
>
> 当前版本：v3.2.2（单一版本源见 `l2d_rig_qt.py` 的 `VERSION` 常量，窗口标题引用它；每次小更新 patch+1，成体系大功能 minor+1）。
>
> 语义规范采用 **Adobe Character Animator（CA）官方图层命名规范**，所以导出的 PSD 既能被
> Character Animator 全自动绑定，也能被 Live2D Cubism Editor 自动转成部件。

---

## 安装与启动

**推荐**：从 [GitHub Releases](https://github.com/hajimi-114514-manbo/12d/releases) 下载 `MuppetStudio-Setup.exe`（约 411MB）。
安装包**内置 Python 运行时**（含 CPU 版 torch 推理栈），装完即可打开软件、编辑、做视频，**不需要自备 Python 环境**。

装完后双击 `木偶工作室.exe`；源码运行则在工作目录执行：

```
D:\Python312\python.exe l2d_rig_qt.py
```

启动后先进**欢迎页**（选项目类型 / 打开历史项目），选定后才进入对应工作台。

### 拆图对显卡的要求（四档引擎优先级链）

| 档位 | 条件 | 速度 | 说明 |
|---|---|---|---|
| 1. CUDA 加速包 | 应用内「资产管理」下载过 CUDA 加速包（N 卡） | ~14 分钟/张 | 首选，装完自动生效 |
| 2. 系统 CUDA | 本机 Python 的 torch 能检测到 NVIDIA CUDA | ~14 分钟/张 | 开发者/已装环境用户 |
| 3. ComfyUI | 本机跑着 ComfyUI（AMD 走 ROCm） | ~15 分钟/张 | A 卡官方路线 |
| 4. CPU 兜底 | 以上全无 | **2~3 小时/张** | 每次拆图前弹确认，会话内记住 |

> 内置运行时的 CPU torch 主要保证**软件能打开**；拆图默认不会悄悄走 CPU 慢速通道。

---

## 欢迎页

- **新建项目**：图片编辑（骨架工作室）/ 视频编辑（测试功能）
- **打开项目**：在欢迎页内直接选目录（校验工程标志后进入对应工作台，失败留在原地）
- **最近使用项**：按 全部/图片工程/视频工程 分页展示（每种工程最多 50 个），支持筛选
- **左下角圆形「检查更新」按钮**：见下文自动更新

---

## 图片编辑工作台（骨架工作室）

### 主流程

1. 把图片**拖进画布**（png/jpg/jpeg/webp/bmp/tif/tiff）或点「拆分图片」→ 自动拆成 CA 命名规范的图层并建骨架
2. 右侧**骨架节点表**（双击支点单元格微调）+ **图层列表**（拖动重排、双击显隐、右键菜单：置顶/重命名/锁定/复制/删除、基础·智能拆分）
3. 左侧画布预览内置动画（呼吸/头摆/发摆/眨眼），播放条逐帧预览
4. 文件菜单导出 PSD / AE 脚本 / JSON / MP4 / GIF

### 画布缩放（Photoshop 风格）

| 操作 | 效果 |
|---|---|
| `Ctrl+0` | 适合窗口 |
| `Ctrl+1` | 实际像素（LOD 图的 100%） |
| `Ctrl+=` / `Ctrl+-` | 放大 / 缩小一级 |
| `Ctrl+滚轮` | 以光标为锚点缩放（2%~1600%） |
| 空格+左键 / 中键拖拽 | 平移 |

预览用半分辨率 LOD 提速；导出 PSD/MP4 全分辨率、GIF 半分辨率，不受预览缩放影响。
画布区铺 PS 风格棋盘格，透明区域露棋盘格。

### 自由变换 / 套索

- 移动工具旁有套索与自由变换：变换可缩放/旋转图层（选项栏输入宽高角度），提交即重采样，Ctrl+Z 可撤销
- 图层右键：智能拆分（MobileSAM）、基础拆分（连通域）、补洞

### 动画时间轴

底部时间轴：关键帧菱形点（拖改时间、双击删除、右键改缓动），播放头 scrub，时长/帧率/字号选项。

---

## 视频编辑工作台（测试功能）

剪映式三区布局，**面板全部可拖拽调节**（水平：素材库|预览|工程属性；垂直：上方区域|时间轴；尺寸自动记忆）：

- **顶部 Pivot**：媒体 | 音频 | 文本* | 贴纸* | 特效* | 转场* | 模板*（*=占位，敬请期待；模板页已内置「导入模板」入口）
- **多轨 clip 时间轴**：双击素材卡片上轨；拖移/两端 trim/切分/删除/吸附（刻度、clip 边界、播放头）；轨道显隐/锁定；自动跟随播放头
- **预览**：实时合成（letterbox 适配），空格播放/暂停；**当前版本预览无声**（导出视频带音频）
- **工程属性**：分辨率（预设+自定义）/ 帧率 / clip 起点·入点·时长
- **导出**：后台线程合成 mp4 + AAC 音轨混流（cv2 失败自动走 ffmpeg），进度弹窗
- **快捷键**：Ctrl+S 保存 / Ctrl+Z·Ctrl+Y 撤销重做（60 步快照）/ 空格播放
- 工程文件：目录内 `video_project.json`
- 标题栏同样有 **文件 / 编辑 / 资产管理** 菜单（视频向裁剪版）

---

## 资产管理（标题栏「资产管理」菜单）

实时状态 + 应用内下载（断点续传、分卷、sha256 校验）：

- **运行环境**：内置 Python 运行时（安装包已含，也可单独下载/修复）
- **模型**：LayerDiff3D (nf4, ~3.4GB)、Marigold 深度 (nf4, ~1.9GB)、MobileSAM（拆图辅助）
- **推理加速**：CUDA 加速包（N 卡提速，~2GB 三卷分卷下载，覆盖安装到内置运行时）
- **当前拆图档位**行：显示命中引擎链的哪一档（后台检测，菜单秒开不卡顿）

---

## 自动更新

欢迎页左下角圆形按钮 →「检查更新」：

1. 通过 GitHub（raw → ghfast → jsdelivr 三镜像）读取 `version.json` 比对版本号
2. 有新版则**后台静默下载**新安装包（断点续传 + sha256 校验），不阻塞任何操作
3. 退出应用时自动拉起 `Setup --auto` **静默安装**并重启应用（全程无窗口，日志 `%TEMP%\muppet_update.log`）

> 维护约定：每次发版必须同步更新仓库根 `version.json`（version + installer_sha256 + notes），否则用户检查不到新版。

---

## 导出格式（图片工作台）

| 格式 | 产物 | 说明 |
|---|---|---|
| PSD | `rig_character.psd` | CA 分组结构，Character Animator 全自动绑定 / Live2D 自动转部件 |
| AE 脚本 | `rig_build.jsx` | 节点预合成 + 父子嵌套 + 动画表达式 |
| JSON | `rig_spec.json` | 自研骨架描述 |
| MP4 | `rig_anim.mp4` | 10 秒/30fps 黑底合成（视频格式不支持透明） |
| GIF | `rig_anim.gif` | 15fps 透明背景，半分辨率直出 |

---

## 已知限制

1. **SeeThrough 是生成式分解**：拆出的层拼回与原画有细节差异，不是无损切分
2. **输入图背景要干净**：白底/纯色底效果最好，复杂背景容易拆出碎片层
3. **静态平面层，无网格形变**：每层只能旋转/位移/缩放
4. **眨眼是压扁近似**：纵向缩放模拟，非逐像素眼皮形变
5. **视频预览无声**（v3 测试功能）：音频只在导出 mp4 中混流生效
6. **CPU 拆图很慢**（2~3 小时/张）：仅兜底档，需确认后才跑

---

## 目录结构

```
D:\work\puppet\muppet_studio\
├─ l2d_rig_qt.py          主程序（界面/菜单/引擎链路由/AssetMenuMixin）
├─ welcome_window.py      欢迎页（项目类型/最近工程/检查更新按钮）
├─ update_checker.py      检查更新（version.json 比对 + 后台下载 + 拉起静默安装）
├─ l2d_rig_core.py        骨架引擎（CA 标签/支点/渲染/PSD·AE·JSON 导出）
├─ seethrough_local.py    本地拆分引擎（CUDA nf4 / CPU fp32 / 确认门）
├─ decompose.py           ComfyUI 拆分引擎（HTTP，AMD 路线）
├─ comfy_decompose.py     ComfyUI 提交/探测
├─ asset_catalog.py       资产清单（运行时/模型/CUDA 加速包分卷）
├─ asset_downloader.py    资产下载器（断点续传/分卷/sha256/覆盖解压）
├─ video_workbench.py     视频编辑工作台（剪映式布局/时间轴联动/导出）
├─ video_model.py         视频工程数据模型（纯函数：clip 增删改/吸附/校验）
├─ video_source.py        帧源解码（图片/视频 LRU 缓存/木偶动画）+ 音频波形
├─ video_export.py        视频合成导出（画布 letterbox + AAC 混流）
├─ clip_timeline.py       clip 多轨时间轴面板
├─ timeline_panel.py      图片工作台动画时间轴
├─ lasso_tool.py          套索/补洞
├─ transform_tool.py      自由变换
├─ see-through\           SeeThrough 核心库（自包含）
├─ models\                nf4 模型（资产管理下载至此，不再打进安装包）
├─ projects\              图片工程默认目录
├─ dist\                  安装包工程（installer.cs / 重新打包.bat）
├─ version.json           更新数据源（发版必须更新）
├─ 木偶工作室.exe          启动器（launcher.cs，多级 python 探测）
└─ README.md              本文档
```

---

## 依赖

开发机为 `D:\Python312`；**最终用户无需任何 Python 环境**（安装包内置 runtime）。

| 组 | 包 |
|---|---|
| 界面 | PySide6、PySide6-Fluent-Widgets、PySideSix-Frameless-Window |
| 骨架引擎 | numpy、scipy、opencv、pillow、psd-tools |
| 本地拆分 | torch（CUDA 或 CPU）、diffusers、peft/accelerate、bitsandbytes（CUDA nf4 路径）、scikit-learn、transformers、safetensors |
| ComfyUI 引擎 | 仅标准库 + numpy + Pillow（需本机 ComfyUI 在 127.0.0.1:8188） |
