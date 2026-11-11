# 木偶工作室 更新日志（CHANGELOG）

> 给后续会话/维护者快速回忆用。按时间倒序，最新在最上。
> 目录铁律：V1 `D:\work\puppet\l2d_rig\` 已冻结不再改；V2 `D:\work\puppet\muppet_studio\` 一切用相对路径（以本目录为根）。

---

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
