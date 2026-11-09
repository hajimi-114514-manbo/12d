# 木偶工作室 更新日志（CHANGELOG）

> 给后续会话/维护者快速回忆用。按时间倒序，最新在最上。
> 目录铁律：V1 `D:\work\puppet\l2d_rig\` 已冻结不再改；V2 `D:\work\puppet\muppet_studio\` 一切用相对路径（以本目录为根）。

---

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
