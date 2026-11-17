# -*- coding: utf-8 -*-
"""视频工作台（v3.0.0）：剪映式布局壳。

    ┌─ Pivot：媒体 | 音频 | 文本* | 贴纸* | 特效* | 转场* | 模板*（*=敬请期待）─┐
    │ 左素材库      │  中预览 + 播放条            │ 右属性面板        │
    │ [添加素材]    │  ┌────────────────┐        │ 工程：分辨率/帧率  │
    │ 卡片流式网格  │  │   画面 fit      │        │ 时长(只读)        │
    │               │  └────────────────┘        │ 选中 clip：        │
    │               │  [▶] 00:01.2/00:04.0 ─●──  │  起/入/时长 spin  │
    ├───────────────┴────────────────────────────┴──────────────────┤
    │ ClipTimelinePanel（clip 多轨时间轴，clip_timeline.py）          │
    └───────────────────────────────────────────────────────────────┘

第一阶段范围（与用户拍板一致）：
  - 单视频轨 + 单音频轨（可加轨），素材：图片 / mp4 / 音频 / 木偶工程
  - clip 移动/trim/切分/删除/吸附；撤销重做（工程快照栈）
  - 预览**无声**（画面逐帧合成）；导出 mp4 含混音
  - 工程存 projects/<名>/video_project.json
"""
import json
import os
import threading
import time

import numpy as np

from PySide6.QtCore import Qt, QTimer, QSize, QSettings, QEvent
from PySide6.QtGui import QImage, QPixmap, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                               QFileDialog, QStackedLayout, QFrame, QSplitter,
                               QMessageBox, QApplication)
from qfluentwidgets import (Pivot, CardWidget, PrimaryPushButton, PushButton,
                            ToolButton, ComboBox, SpinBox, DoubleSpinBox,
                            StrongBodyLabel, BodyLabel, CaptionLabel,
                            LineEdit, ScrollArea, InfoBar, InfoBarPosition,
                            MessageBoxBase, ProgressBar, FluentIcon,
                            FlowLayout, PillPushButton)

import video_model as V
import video_export as VE
from stage_view import StageView
from script_editor import ScriptEditor

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJ_BASE = os.path.join(_HERE, "projects")
RECENT_FILE = os.path.join(_HERE, "recent_projects.json")
META_NAME = "video_project.json"

DEFAULT_DUR = {"image": 3.0, "video": 0, "puppet": 0, "audio": 0}  # 0=用素材全长

# ---------- 面板 splitter（可拖拽调宽高）----------
SETTINGS_NAME = "muppet_workbench.ini"
SPLIT_H_DEFAULT = [300, 880, 260]     # 素材库 | 预览 | 属性
SPLIT_H_MIN = [180, 420, 180]
SPLIT_V_DEFAULT = [560, 340]         # 上方区域 | 时间轴（约 38%）
SPLIT_V_MIN_TIMELINE = 120

# handle 颜色与主界面统一：idle #3a3f4a / hover+drag #4d88ff。
# 用 objectName 限定作用域，避免污染子控件里的其它 QSplitter。
_SPLIT_QSS = (
    "QSplitter#wbHSplit::handle { background: #3a3f4a; }"
    "QSplitter#wbHSplit::handle:hover, QSplitter#wbHSplit::handle:active"
    " { background: #4d88ff; }"
    "QSplitter#wbVSplit::handle { background: #3a3f4a; }"
    "QSplitter#wbVSplit::handle:hover, QSplitter#wbVSplit::handle:active"
    " { background: #4d88ff; }"
)


def fmt_time(t):
    t = max(0.0, float(t))
    return "%02d:%04.1f" % (int(t // 60), t % 60)


def unique_video_name(base="视频工程"):
    os.makedirs(PROJ_BASE, exist_ok=True)
    n, name = 1, base
    while os.path.exists(os.path.join(PROJ_BASE, name)):
        n += 1
        name = "%s%d" % (base, n)
    return name


class _AssetCard(CardWidget):
    """素材库卡片：缩略图 + 名称 + 时长；双击=加到时间轴。"""

    activated = None  # 用回调即可，不做信号

    def __init__(self, source, thumb_qpixmap=None, on_add=None, parent=None):
        super().__init__(parent)
        self.source = source
        self.on_add = on_add
        self.setFixedSize(112, 132)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(3)
        self.img_lbl = QLabel(self)
        self.img_lbl.setFixedSize(100, 58)
        self.img_lbl.setAlignment(Qt.AlignCenter)
        self.img_lbl.setStyleSheet("background:#14161b;border-radius:4px;"
                                   "color:#8a93a2;")
        if thumb_qpixmap is not None and not thumb_qpixmap.isNull():
            self.img_lbl.setPixmap(thumb_qpixmap.scaled(
                self.img_lbl.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation))
        else:
            self.img_lbl.setText(source["kind"][:4])
        lay.addWidget(self.img_lbl)
        nm = os.path.basename(str(source["path"]).rstrip("\\/")) or "工程"
        t = BodyLabel(nm[:14], self)
        t.setStyleSheet("color:#c8d1dc;")
        f = t.font()
        f.setPixelSize(11)
        t.setFont(f)
        lay.addWidget(t)
        d = source.get("src_dur") or 0.0
        cap = CaptionLabel("图片" if source["kind"] == "image"
                           else fmt_time(d), self)
        cap.setStyleSheet("color:#8a93a2;")
        lay.addWidget(cap)
        lay.addStretch(1)

    def mouseDoubleClickEvent(self, ev):
        if self.on_add:
            self.on_add(self.source)
        super().mouseDoubleClickEvent(ev)


class _TemplateCard(CardWidget):
    """模板页卡片：标题 + 说明 + 点击触发（A4 模板页）。"""

    def __init__(self, title, desc, on_click, parent=None):
        super().__init__(parent)
        self.on_click = on_click
        self.setFixedSize(168, 148)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        t = BodyLabel(title, self)
        t.setStyleSheet("color:#e8ecf4; font-size:13px;")
        lay.addWidget(t)
        d = CaptionLabel(desc, self)
        d.setWordWrap(True)
        d.setStyleSheet("color:#8a93a2; font-size:11px;")
        lay.addWidget(d, 1)
        b = PushButton("应用", self)
        b.setFixedHeight(26)
        b.clicked.connect(self._click)
        lay.addWidget(b)

    def _click(self):
        if self.on_click:
            self.on_click()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._click()
        super().mousePressEvent(e)


class _RuntimeCard(CardWidget):
    """E2 模板页「WebGAL 运行时」状态卡：状态行 + 在线获取 / 导入压缩包。"""

    def __init__(self, on_online, on_import, parent=None):
        super().__init__(parent)
        self.on_online = on_online
        self.on_import = on_import
        self.setFixedSize(168, 196)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        t = BodyLabel("WebGAL 运行时", self)
        t.setStyleSheet("color:#e8ecf4; font-size:13px;")
        lay.addWidget(t)
        self.status_lbl = CaptionLabel("检测中…", self)
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color:#8a93a2; font-size:11px;")
        lay.addWidget(self.status_lbl, 1)
        b1 = PushButton("在线获取", self)
        b1.setFixedHeight(26)
        b1.clicked.connect(self.on_online)
        lay.addWidget(b1)
        b2 = PushButton("导入压缩包…", self)
        b2.setFixedHeight(26)
        b2.clicked.connect(self.on_import)
        lay.addWidget(b2)
        self.refresh()

    def refresh(self):
        """读 runtime_pack.status() 刷新状态文案（未安装/已装 vX.Y.Z+文件数）。"""
        try:
            import runtime_pack as RP
            st = RP.status()
        except Exception:
            self.status_lbl.setText("运行时不可用")
            return
        if st.get("installed"):
            self.status_lbl.setText("已装 %s\n%d 文件" % (
                st.get("version") or "未知版本", st.get("files", 0)))
        else:
            self.status_lbl.setText("未安装\n导入 zip 即用")


class VideoWorkbench(QWidget):
    """视频编辑工作台。project_dirty_changed(str) 通知外层改标题。"""

    def __init__(self, parent=None, proj=None, proj_dir=None):
        super().__init__(parent)
        self.proj = proj or V.new_project()
        self.proj_dir = proj_dir
        self.assets = []            # source dict 列表
        self._sel_clip = None       # 选中 clip id
        self._sources = VE.SourceManager()
        self._hist = []
        self._hist_i = -1
        self._t = 0.0
        self._playing = False
        self._tick_busy = False
        self._loading = False       # spin 回填防回环

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._advance)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # ---------- 顶部 Pivot ----------
        self.pivot = Pivot(self)
        for key, text in [("media", "媒体"), ("audio", "音频"),
                          ("text", "文本"), ("sticker", "贴纸"),
                          ("effect", "特效"), ("transition", "转场"),
                          ("template", "模板")]:
            self.pivot.addItem(key, text)
        self.pivot.setCurrentItem("media")
        self._asset_tab = "media"   # currentItemChanged 槽参数记录（PivotItem 无 routeKey）
        self.pivot.currentItemChanged.connect(self._filter_assets)
        pv = QWidget(self)
        pl = QHBoxLayout(pv)
        pl.setContentsMargins(12, 6, 12, 2)
        pl.addWidget(self.pivot)
        pl.addStretch(1)
        self.act_save = PushButton("保存工程", pv)
        self.act_save.setFixedHeight(30)
        self.act_save.clicked.connect(lambda: self.save_project())
        self.act_export = PrimaryPushButton("导出", pv)
        self.act_export.setIcon(FluentIcon.SEND.icon())
        self.act_export.setFixedHeight(30)
        self.act_export.clicked.connect(self._open_export_dialog)
        pl.addWidget(self.act_save)
        pl.addWidget(self.act_export)

        # ---------- F2-L5（v3.5）：幕 TabBar ----------
        # 多幕工程（v2）才出现；单幕工程（v1）**整行不建**（零 diff）。
        # 一幕一条独立时间轴，切 tab = 切当前幕游标。
        self._act_bar = None
        self._act_tabs = None
        self._cur_aid = None
        self._act_buttons = {}

        # ---------- 中部三列（QSplitter 可拖拽调宽）----------
        hsplit = QSplitter(Qt.Horizontal, self)
        hsplit.setObjectName("wbHSplit")
        hsplit.setChildrenCollapsible(False)
        hsplit.setHandleWidth(6)
        hsplit.setStyleSheet(_SPLIT_QSS)

        # 左：素材库
        left = QFrame(self)
        left.setObjectName("assetPanel")
        left.setMinimumWidth(SPLIT_H_MIN[0])
        left.setStyleSheet("#assetPanel{background:#20232a;border-radius:8px;}")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 10, 10, 10)
        ll.setSpacing(8)
        lh = QHBoxLayout()
        self.asset_header = StrongBodyLabel("素材库", left)
        lh.addWidget(self.asset_header)
        lh.addStretch(1)
        ll.addLayout(lh)
        ab = QHBoxLayout()
        self.btn_add_file = PushButton("添加素材文件", left)
        self.btn_add_file.clicked.connect(self._add_files)
        self.btn_add_puppet = PushButton("导入木偶工程", left)
        self.btn_add_puppet.clicked.connect(self._add_puppet)
        ab.addWidget(self.btn_add_file)
        ab.addWidget(self.btn_add_puppet)
        ll.addLayout(ab)
        self.asset_scroll = ScrollArea(left)
        self.asset_scroll.setWidgetResizable(True)
        self.asset_host = QWidget()
        # v3.4.7：viewport 默认浅底，深色面板中间露一整块白（属性面板
        # pscroll 修过同款问题，素材库这里漏了）；host 用 objectName 限定，
        # 不用通配选择器，避免盖掉 CardWidget 卡片自绘底色。
        self.asset_host.setObjectName("assetHost")
        self.asset_scroll.setStyleSheet(
            "QScrollArea::viewport{background:#20232a;}"
            "QWidget#assetHost{background:#20232a;}")
        self.asset_flow = FlowLayout(self.asset_host, needAni=False)
        self.asset_flow.setContentsMargins(0, 0, 0, 0)
        self.asset_scroll.setWidget(self.asset_host)
        ll.addWidget(self.asset_scroll, 1)
        self.asset_empty = CaptionLabel("还没有素材\n点上方按钮导入，或双击卡片上轨",
                                        left)
        self.asset_empty.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.asset_empty)
        # F1：文本页剧情编辑器常驻（tab 切换只 show/hide，表单内容不丢）
        self.script_editor = ScriptEditor(self, left)
        ll.addWidget(self.script_editor, 1)
        hsplit.addWidget(left)

        # 中：预览
        center = QWidget(self)
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)
        # A2：预览画布换成 StageView（可拖拽布置 + 选中联动），最小尺寸不变
        self.stage = StageView(center)
        self.stage.setMinimumSize(320, 180)
        self.stage.setStyleSheet("background:#0d0f13;border-radius:6px;")
        self.stage.clip_selected.connect(self._on_stage_select)
        self.stage.layout_changed.connect(self._on_stage_layout_changed)
        self.preview = self.stage          # 兼容旧引用
        # ---- H3（v3.5）：实时预览与 StageView 共存（设计 §5）----
        # QStackedLayout 同位切换：page0=StageView（永远在场，承担全部交互），
        # page1=WebPreviewView（懒创建，W A_TransparentForMouseEvents）。
        # available()==False 时**连开关都不创建**（设计 §2 硬规则 1、3），
        # 既有 UI 测试零 diff。
        self._webview = None
        self._preview_real = False
        self._stack = QStackedLayout()
        self._stack.setContentsMargins(0, 0, 0, 0)
        stack_host = QWidget(center)
        stack_host.setLayout(self._stack)
        self._stack.addWidget(self.stage)
        cl.addWidget(stack_host, 1)
        self._preview_badge = None
        try:
            import webgal_preview as WP
            self._wp = WP
            # v3.5：角标创建条件 = WebEngine 可用 **且** WebGAL 运行时已就位。
            # 只查 available() 会出现「开关常亮但页面空白」（装了 WebEngine
            # 可选件但运行时素材没下载 -> 点开只有 no_runtime 错误页）。
            if self._preview_runtime_ready():
                self._make_preview_badge(stack_host)
        except Exception:
            self._wp = None
        pb = QHBoxLayout()
        self.btn_play = ToolButton(center)
        self.btn_play.setIcon(FluentIcon.PLAY.icon())
        self.btn_play.clicked.connect(self.toggle_play)
        pb.addWidget(self.btn_play)
        self.time_lbl = CaptionLabel("00:00.0 / 00:00.0", center)
        self.time_lbl.setMinimumWidth(120)
        pb.addWidget(self.time_lbl)
        self.sld_pos = ProgressBar(center)      # 兼当进度显示（不可拖）
        self.sld_pos.setFixedHeight(6)
        self.sld_pos.setTextVisible(False)
        self.sld_pos.setRange(0, 1000)
        pb.addWidget(self.sld_pos, 1)
        cl.addLayout(pb)
        center.setMinimumWidth(SPLIT_H_MIN[1])
        hsplit.addWidget(center)

        # 右：属性
        right = QFrame(self)
        right.setObjectName("propPanel")
        right.setMinimumWidth(SPLIT_H_MIN[2])
        # F1 修复：内层 host 裸 QWidget 无背景 -> 暗主题下近白底浅灰字对比崩。
        # 给 host 定 objectName + 深色背景，ScrollArea viewport 同色，标签才可见。
        right.setStyleSheet(
            "#propPanel{background:#20232a;border-radius:8px;}"
            "#propHost{background:#20232a;}"
            "QScrollArea::viewport{background:#20232a;}")
        # 属性内容放进滚动容器：内容多时面板可被压缩，不把时间轴饿死
        host = QWidget(right)
        host.setObjectName("propHost")
        rl = QVBoxLayout(host)
        rl.setContentsMargins(12, 12, 12, 12)
        rl.setSpacing(8)
        rl.addWidget(StrongBodyLabel("工程属性", right))
        r1 = QHBoxLayout()
        r1.addWidget(CaptionLabel("分辨率", right))
        self.cmb_res = ComboBox(right)
        for r in ("1920×1080", "1080×1920", "1280×720", "720×1280",
                  "1080×1080"):
            self.cmb_res.addItem(r)
        self.cmb_res.currentIndexChanged.connect(self._res_changed)
        r1.addWidget(self.cmb_res, 1)
        rl.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(CaptionLabel("帧率", right))
        self.spin_fps = SpinBox(right)
        self.spin_fps.setRange(1, 120)
        self.spin_fps.setValue(30)
        self.spin_fps.valueChanged.connect(self._fps_changed)
        r2.addWidget(self.spin_fps, 1)
        rl.addLayout(r2)
        self.dur_lbl = CaptionLabel("总时长：00:00.0", right)
        rl.addWidget(self.dur_lbl)
        rl.addSpacing(10)
        self.clip_title = StrongBodyLabel("选中片段属性", right)
        rl.addWidget(self.clip_title)
        self.clip_info = CaptionLabel("未选中", right)
        self.clip_info.setWordWrap(True)
        rl.addWidget(self.clip_info)
        for lab in ("start", "in", "dur"):
            row = QHBoxLayout()
            row.addWidget(CaptionLabel({"start": "起点", "in": "素材入点",
                                        "dur": "时长"}[lab], right))
            sp = DoubleSpinBox(right)
            sp.setRange(0.0, 600.0)
            sp.setSingleStep(0.1)
            sp.setDecimals(2)
            sp.setSuffix(" s")
            sp.valueChanged.connect(lambda _v, k=lab: self._clip_spin_changed(k))
            setattr(self, "spin_" + lab, sp)
            row.addWidget(sp, 1)
            rl.addLayout(row)

        # ---- 布局组：X/Y/Scale/Rot + 六向对齐（A2 StageView）----
        self.lay_group = StrongBodyLabel("布局（舞台）", right)
        self.lay_group.setVisible(False)
        rl.addWidget(self.lay_group)
        for lab, key, rng in (("X", "x", (-0.5, 1.5)), ("Y", "y", (-0.5, 1.5)),
                              ("Scale", "scale", (0.1, 10.0)),
                              ("Rot", "rot", (-180.0, 180.0))):
            row = QHBoxLayout()
            row.addWidget(CaptionLabel(lab, right))
            sp = DoubleSpinBox(right)
            sp.setRange(rng[0], rng[1])
            sp.setSingleStep(1.0 if key == "rot" else 0.01)
            sp.setDecimals(1 if key == "rot" else 3)
            sp.setSuffix(" °" if key == "rot" else "")
            sp.valueChanged.connect(lambda _v, k=key: self._layout_spin_changed(k))
            setattr(self, "spin_lay_" + key, sp)
            row.addWidget(sp, 1)
            rl.addLayout(row)
        al = QHBoxLayout()
        for label, (nx, ny) in (("左上", (0.0, 0.0)), ("上", (0.5, 0.0)),
                                ("右上", (1.0, 0.0)), ("左下", (0.0, 1.0)),
                                ("下", (0.5, 1.0)), ("右下", (1.0, 1.0))):
            btn = PushButton(label, right)
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda _=False, a=nx, b=ny: self._align_clip(a, b))
            al.addWidget(btn)
        rl.addLayout(al)

        # ---- puppet 预置动作/表情：下拉 + 预览按钮（C2）----
        self.act_group = StrongBodyLabel("预置动作 / 表情", right)
        self.act_group.setVisible(False)
        rl.addWidget(self.act_group)
        ar = QHBoxLayout()
        self._motion_ids = [mid for mid, _ in self._preset_choices("motion")]
        self.cmb_motion = ComboBox(right)
        for _mid, name in self._preset_choices("motion"):
            self.cmb_motion.addItem(name)
        self.cmb_motion.currentIndexChanged.connect(self._motion_changed)
        ar.addWidget(self.cmb_motion, 1)
        self._expression_ids = [eid for eid, _ in self._preset_choices("expression")]
        self.cmb_expr = ComboBox(right)
        for _eid, name in self._preset_choices("expression"):
            self.cmb_expr.addItem(name)
        self.cmb_expr.currentIndexChanged.connect(self._expression_changed)
        ar.addWidget(self.cmb_expr, 1)
        self.btn_play_clip = ToolButton(right)
        self.btn_play_clip.setIcon(FluentIcon.PLAY.icon())
        self.btn_play_clip.setToolTip("在该片段起点预览")
        self.btn_play_clip.clicked.connect(self._play_clip_preview)
        ar.addWidget(self.btn_play_clip)
        rl.addLayout(ar)

        self.btn_del_clip = PushButton("删除选中片段", right)
        self.btn_del_clip.clicked.connect(self._delete_selected)
        rl.addWidget(self.btn_del_clip)
        rl.addStretch(1)
        pscroll = ScrollArea(right)
        pscroll.setWidgetResizable(True)
        # viewport 与内层 host 同色，避免滚动区露出白底
        pscroll.setStyleSheet(
            "QScrollArea::viewport{background:#20232a;}"
            "QScrollArea > QWidget > QWidget{background:#20232a;}")
        pscroll.setWidget(host)
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.addWidget(pscroll)
        hsplit.addWidget(right)
        hsplit.setStretchFactor(0, 0)
        hsplit.setStretchFactor(1, 2)      # 预览列 stretch 最大
        hsplit.setStretchFactor(2, 0)
        hsplit.setSizes(SPLIT_H_DEFAULT)
        self._hsplit = hsplit

        # ---------- 顶部区域（pv + 幕条 + 三列 splitter）----------
        top = QWidget(self)
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(0)
        tv.addWidget(pv)
        # F2-L5：幕 TabBar 的宿主（多幕时才建并插入）
        self._act_bar_host = QWidget(self)
        self._act_bar_layout = QHBoxLayout(self._act_bar_host)
        self._act_bar_layout.setContentsMargins(12, 2, 12, 2)
        self._act_bar_layout.setSpacing(6)
        self._act_bar_host.setVisible(False)
        tv.addWidget(self._act_bar_host)
        tv.addWidget(hsplit, 1)

        # ---------- 底部：clip 时间轴 ----------
        import clip_timeline as CT
        self.timeline = CT.ClipTimelinePanel(self, wb=self)
        self.timeline.seeked.connect(self.seek)
        self.timeline.selection_changed.connect(self._on_selection)
        self.timeline.edit_committed.connect(self._after_edit)
        self.timeline.setMinimumHeight(SPLIT_V_MIN_TIMELINE)

        # ---------- 垂直 splitter：上方区域 | 时间轴 ----------
        vsplit = QSplitter(Qt.Vertical, self)
        vsplit.setObjectName("wbVSplit")
        vsplit.setChildrenCollapsible(False)
        vsplit.setHandleWidth(6)
        vsplit.setStyleSheet(_SPLIT_QSS)
        vsplit.addWidget(top)
        vsplit.addWidget(self.timeline)
        vsplit.setStretchFactor(0, 3)
        vsplit.setStretchFactor(1, 1)
        vsplit.setSizes(SPLIT_V_DEFAULT)
        self._vsplit = vsplit

        # ---------- splitter 尺寸持久化 ----------
        # QSettings(IniFormat) 在没设 organization 时会退到**当前工作目录**，
        # 把 muppet_workbench.ini 落进安装目录/仓库。这里强制绝对路径：
        # SETTINGS_NAME 是绝对路径（测试用临时 ini）就直接用，否则写到
        # %APPDATA%\MuppetStudio\，不污染应用目录。
        _sname = SETTINGS_NAME
        if os.path.isabs(_sname):
            _settings_path = _sname
        else:
            _base = os.environ.get("APPDATA") or os.path.expanduser("~")
            _settings_path = os.path.join(_base, "MuppetStudio", _sname)
            try:
                os.makedirs(os.path.dirname(_settings_path), exist_ok=True)
            except Exception:
                pass
        self._settings = QSettings(_settings_path, QSettings.IniFormat)
        hsplit.splitterMoved.connect(lambda *a: self._save_splitter_sizes())
        vsplit.splitterMoved.connect(lambda *a: self._save_splitter_sizes())
        self._restore_splitter_sizes()

        v.addWidget(vsplit)

        # 快捷键：Ctrl+S 保存 / Ctrl+Z Y 撤销重做 / 空格播放
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save_project)
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.redo)
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self.toggle_play)
        # v3.5.2：时间轴键盘导航（之前只有空格/Del，左右全靠鼠标拖）
        # 左右 = ±1 帧（1/fps）；Shift = ±1 秒；Home/End = 首尾。
        # QShortcut 是 WindowShortcut：焦点在输入框里时 Qt 先发
        # ShortcutOverride 给输入框（它 accept 自己要的键），不会抢光标键。
        for keys, fn in (
                ("Left", lambda: self._seek_relative(-1)),
                ("Right", lambda: self._seek_relative(1)),
                ("Shift+Left", lambda: self._seek_relative(-1.0, True)),
                ("Shift+Right", lambda: self._seek_relative(1.0, True)),
                ("Home", lambda: self.seek(0.0)),
                ("End", lambda: self.seek(V.project_duration(
                    self.proj, getattr(self, "_cur_aid", None)))),
        ):
            QShortcut(QKeySequence(keys), self, activated=fn)

        self._apply_project(None)
        self._seed_history()

    # ================= splitter 尺寸持久化 =================
    def _save_splitter_sizes(self):
        """拖拽结束（splitterMoved）：把两个 splitter 的尺寸写入 QSettings。"""
        try:
            self._settings.setValue("splitter/horizontal", self._hsplit.sizes())
            self._settings.setValue("splitter/vertical", self._vsplit.sizes())
            self._settings.sync()
        except Exception:
            pass

    def _restore_splitter_sizes(self):
        """构造时恢复：校验列表长度与正数，否则用默认尺寸。"""
        try:
            hs = self._settings.value("splitter/horizontal")
            if isinstance(hs, (list, tuple)) and len(hs) == 3 \
                    and all(int(v) > 0 for v in hs):
                self._hsplit.setSizes([int(v) for v in hs])
            vs = self._settings.value("splitter/vertical")
            if isinstance(vs, (list, tuple)) and len(vs) == 2 \
                    and all(int(v) > 0 for v in vs):
                self._vsplit.setSizes([int(v) for v in vs])
        except Exception:
            pass

    # ================= 工程生命周期 =================
    def _refresh_all(self):
        """工程结构/属性变化后的统一刷新：时间轴 + 传输条 + 属性 + 预览。"""
        try:
            self.timeline.refresh()
        except Exception:
            pass
        self._update_transport()
        self._fill_clip_props()
        if not self._playing:
            self.render_frame(self._t)

    def _apply_project(self, _ignored):
        p = self.proj
        self.timeline.set_project(p)
        # F2-L5：载入/新建工程后重幕条（幕数可能变了）
        self._cur_aid = None
        self._refresh_act_bar()
        self.spin_fps.blockSignals(True)
        self.spin_fps.setValue(int(round(p["fps"])))
        self.spin_fps.blockSignals(False)
        self._set_res_combo(p["width"], p["height"])
        self._refresh_all()

    def _set_res_combo(self, w, h):
        want = "%d×%d" % (w, h)
        self.cmb_res.blockSignals(True)
        idx = self.cmb_res.findText(want)
        if idx < 0:
            # ComboBox 只有 insertItem，没有 insertText（用 insertText 会 AttributeError）
            if want not in [self.cmb_res.itemText(i)
                            for i in range(self.cmb_res.count())]:
                self.cmb_res.insertItem(0, want)
            idx = self.cmb_res.findText(want)
        self.cmb_res.setCurrentIndex(idx)
        self.cmb_res.blockSignals(False)

    def _res_changed(self, idx):
        if self._loading:
            return
        txt = self.cmb_res.currentText()
        try:
            w, h = (int(x) for x in txt.split("×"))
        except ValueError:
            return
        self._push_pending()
        self.proj["width"], self.proj["height"] = w, h
        self._refresh_all()

    def _fps_changed(self, v):
        if self._loading:
            return
        self._push_pending()
        self.proj["fps"] = float(v)
        self.timer.setInterval(max(1, int(round(1000.0 / v))))
        self._refresh_all()

    # ---------- 历史（工程快照栈，先改后推约定同 puppet 工作台） ----------
    def _seed_history(self):
        self._hist = [V.clone_project(self.proj)]
        self._hist_i = 0

    def push_history(self):
        snap = V.clone_project(self.proj)
        if self._hist_i >= 0 and json.dumps(self._hist[self._hist_i], sort_keys=True) \
                == json.dumps(snap, sort_keys=True):
            return
        self._hist = self._hist[:self._hist_i + 1]
        self._hist.append(snap)
        if len(self._hist) > 60:
            self._hist.pop(0)
        self._hist_i = len(self._hist) - 1
        self.project_dirty()

    def _push_pending(self):
        """属性面板连续改：先记录当前态再改（撤销能回到改之前）。"""
        self.push_history()

    def undo(self):
        if self._hist_i > 0:
            self._hist_i -= 1
            self._restore_hist()

    def redo(self):
        if self._hist_i < len(self._hist) - 1:
            self._hist_i += 1
            self._restore_hist()

    def _restore_hist(self):
        self.proj = V.clone_project(self._hist[self._hist_i])
        self._sel_clip = None
        self._sources.close_all()
        self._loading = True
        self._apply_project(None)
        self._loading = False
        self._refresh_all()
        _se = getattr(self, "script_editor", None)
        if _se is not None:      # F1：撤销/重做后台词表刷新
            _se.refresh()

    def project_dirty(self):
        self.project_dirty_changed()

    def project_dirty_changed(self):
        pass  # VideoWindow 覆写

    # ---------- 保存 / 打开 ----------
    def save_project(self, ask=False):
        if ask or not self.proj_dir:
            d = QFileDialog.getExistingDirectory(self, "选择工程目录", PROJ_BASE)
            if not d:
                return False
            self.proj_dir = d
        try:
            os.makedirs(self.proj_dir, exist_ok=True)
            with open(os.path.join(self.proj_dir, META_NAME), "w",
                      encoding="utf8") as f:
                json.dump(self.proj, f, ensure_ascii=False, indent=1)
            self._record_recent()
            InfoBar.success("已保存", os.path.basename(self.proj_dir),
                            parent=self, position=InfoBarPosition.TOP,
                            duration=2000)
            return True
        except Exception as e:
            InfoBar.error("保存失败", repr(e), parent=self,
                          position=InfoBarPosition.TOP, duration=4000)
            return False

    def save_as(self):
        """另存为：选目录 -> 写 video_project.json -> 切 proj_dir -> _record_recent。

        复用 save_project(ask=True) 的落盘逻辑，避免两份写盘代码。
        """
        return self.save_project(ask=True)

    def _record_recent(self):
        try:
            try:
                items = json.load(open(RECENT_FILE, encoding="utf8"))
                assert isinstance(items, list)
            except Exception:
                items = []
            d = os.path.normpath(self.proj_dir)
            items = [it for it in items
                     if os.path.normpath(it.get("dir", "")) != d]
            items.insert(0, {"dir": d, "name": os.path.basename(d),
                             "type": "video", "layers": 0})
            json.dump(items[:100], open(RECENT_FILE, "w", encoding="utf8"),
                      ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def is_video_project(d):
        return bool(d) and os.path.isfile(os.path.join(d, META_NAME))

    def load_dir(self, d):
        try:
            obj = json.load(open(os.path.join(d, META_NAME), encoding="utf8"))
        except Exception as e:
            InfoBar.error("打开失败", "video_project.json 读取异常：%r" % e,
                          parent=self, position=InfoBarPosition.TOP,
                          duration=4000)
            return False
        p = V.sanitize_project(obj)
        if p is None:
            InfoBar.error("打开失败", "不是合法的视频工程文件", parent=self,
                          position=InfoBarPosition.TOP, duration=4000)
            return False
        V.normalize_ids(p)
        self._sources.close_all()
        self.proj = p
        self.proj_dir = d
        self._sel_clip = None
        self._loading = True
        self._apply_project(None)
        self._loading = False
        self._seed_history()
        self.seek(0.0)
        return True

    # ================= 素材库 =================
    def _add_files(self):
        fs, _ = QFileDialog.getOpenFileNames(
            self, "添加素材（图片/视频/音频）", "",
            "素材文件 (*.png *.jpg *.jpeg *.bmp *.webp *.tga "
            "*.mp4 *.avi *.mov *.mkv *.webm *.mp3 *.wav *.m4a *.aac *.flac);;"
            "所有文件 (*)")
        self._import_paths(fs)

    def _add_puppet(self):
        d = QFileDialog.getExistingDirectory(self, "选择木偶工程目录",
                                             os.path.join(_HERE, "projects"))
        if d:
            self._import_paths([d])

    def _import_paths(self, paths):
        import video_source as VS
        n = 0
        for p in paths:
            try:
                src = VS.probe_source(p)
            except Exception as e:
                InfoBar.warning("跳过素材", "%s\n%r" % (os.path.basename(p), e),
                                parent=self, position=InfoBarPosition.TOP,
                                duration=3000)
                continue
            if any(a["path"] == src["path"] and a["kind"] == src["kind"]
                   for a in self.assets):
                continue
            self.assets.append(src)
            n += 1
        if n:
            InfoBar.success("已导入", "%d 个素材" % n, parent=self,
                            position=InfoBarPosition.TOP, duration=2000)
        self._rebuild_asset_cards()

    def _thumb(self, src):
        """卡片缩略图：image/puppet/video 首帧。失败返回 None。"""
        try:
            import video_source as VS
            s = VS.open_source(src)
            try:
                arr = s.frame(0.0)
                if arr.ndim == 3 and arr.shape[2] == 4:
                    # v3.4.0：ImageSource 可返回 RGBA（透明底立绘）；
                    # 缩略图把 alpha 合成到中性灰底再走 RGB888
                    a = arr[:, :, 3:4].astype(np.float32) * (1.0 / 255.0)
                    bg = np.full(arr[:, :, :3].shape, 60.0, np.float32)
                    arr = (arr[:, :, :3].astype(np.float32) * a +
                           bg * (1.0 - a)).astype(np.uint8)
                elif arr.ndim == 2:
                    arr = np.dstack([arr] * 3)
                qimg = QImage(np.ascontiguousarray(arr).data, arr.shape[1],
                              arr.shape[0], 3 * arr.shape[1],
                              QImage.Format_RGB888).copy()
                return QPixmap.fromImage(qimg)
            finally:
                s.close()
        except Exception:
            return None

    def _rebuild_asset_cards(self):
        while self.asset_flow.count():
            it = self.asset_flow.takeAt(0)
            # qfluentwidgets FlowLayout.takeAt 返回的就是 widget 本体（非 QLayoutItem）
            wi = it.widget() if hasattr(it, "widget") else it
            if isinstance(wi, QWidget):
                wi.deleteLater()
        filt = self._asset_tab
        if filt == "text":
            # F1：文本页 = 剧情编辑器
            self._show_script_editor(True)
            return
        self._show_script_editor(False)
        if filt == "template":
            # A4/E2 模板页：三张工程卡片 + 一张 WebGAL 运行时状态卡
            cards = [
                ("空白 WebGAL 工程", "新建空工程并清空台词，拖素材即可开始。",
                 self._load_template_blank),
                ("单背景+双立绘演示", "内置生成的渐变背景与两个剪影立绘，含示例台词。",
                 self._load_template_demo),
                ("导入 WebGAL 目录…", "选 WebGAL game 目录，反向导入背景与立绘。",
                 self._import_webgal_dir),
            ]
            for title, desc, fn in cards:
                self.asset_flow.addWidget(
                    _TemplateCard(title, desc, fn, self.asset_host))
            self._runtime_card = _RuntimeCard(self._runtime_online,
                                              self._runtime_import_zip,
                                              self.asset_host)
            self.asset_flow.addWidget(self._runtime_card)
            self.asset_empty.setVisible(False)
            return
        for src in self.assets:
            is_audio = src["kind"] == "audio"
            if filt == "audio" and not is_audio:
                continue
            if filt == "media" and is_audio:
                continue
            if filt not in ("audio", "media"):
                continue
            th = self._thumb(src) if src["kind"] in ("image", "video",
                                                     "puppet") else None
            card = _AssetCard(src, th, on_add=self._add_asset_to_timeline,
                              parent=self.asset_host)
            self.asset_flow.addWidget(card)
        self.asset_empty.setVisible(
            not any((self.pivot.currentItem() == "media") == (s["kind"] != "audio")
                    for s in self.assets))

    # ---------- A4 模板页（三张卡片背后的动作） ----------
    @staticmethod
    def _templates_dir():
        _base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(_base, "MuppetStudio", "templates")

    def _ensure_demo_assets(self):
        """生成/复用内置演示素材（渐变背景 + 两个半透明人形剪影），零外部版权素材。"""
        d = self._templates_dir()
        os.makedirs(d, exist_ok=True)
        bg = os.path.join(d, "demo_bg.png")
        fig_l = os.path.join(d, "demo_fig_left.png")
        fig_r = os.path.join(d, "demo_fig_right.png")
        if all(os.path.isfile(p) for p in (bg, fig_l, fig_r)):
            return bg, fig_l, fig_r
        from PIL import Image, ImageDraw
        W, H = 1920, 1080
        bg_img = Image.new("RGB", (W, H))
        px = bg_img.load()
        top = (38, 46, 78)
        bot = (120, 86, 96)
        for y in range(H):
            r = y / max(1, H - 1)
            row = tuple(int(top[i] + (bot[i] - top[i]) * r) for i in range(3))
            for x in range(W):
                px[x, y] = row
        bg_img.save(bg)

        def _silhouette(path, cx):
            canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            dr = ImageDraw.Draw(canvas)
            col = (24, 28, 40, 170)
            head_r = 74
            head_y = 300
            body_top = head_y + head_r - 6
            body_bot = 820
            half_top, half_bot = 96, 168
            dr.ellipse([cx - head_r, head_y - head_r, cx + head_r, head_y + head_r],
                       fill=col)
            dr.polygon([(cx - half_top, body_top), (cx + half_top, body_top),
                        (cx + half_bot, body_bot), (cx - half_bot, body_bot)],
                       fill=col)
            canvas.save(path)

        _silhouette(fig_l, int(W * 0.30))
        _silhouette(fig_r, int(W * 0.70))
        return bg, fig_l, fig_r

    def _src_for(self, path, kind="image"):
        """构造 source dict（尽量探测真实尺寸，探测不到用 0 兜底）。"""
        try:
            import video_source as VS
            src = VS.probe_source(path)
            if isinstance(src, dict) and src.get("path"):
                src["kind"] = kind
                return src
        except Exception:
            pass
        return V.new_source(kind, path)

    def _load_template_blank(self):
        """空白 WebGAL 工程：project.new 语义 + 清空台词。"""
        self._sources.close_all()
        self.proj = V.new_project()
        self.proj["dialogue"] = []
        self._loading = True
        self._apply_project(None)
        self._loading = False
        self._seed_history()
        self.seek(0.0)
        self.push_history()

    def _load_template_demo(self):
        """单背景+双立绘演示：BG 轨 + 两 FIG 轨 + 两条示例台词。"""
        bg, fig_l, fig_r = self._ensure_demo_assets()
        self._sources.close_all()
        proj = V.new_project()
        proj["tracks"] = []
        proj["dialogue"] = []
        ok, _m, tid = V.add_track(proj, "video", "BG")
        if ok:
            V.add_clip(proj, tid, V.new_clip(self._src_for(bg), 0.0, 10.0))
        for path, name, x in ((fig_l, "FIG-1", 0.30), (fig_r, "FIG-2", 0.70)):
            ok, _m, tid = V.add_track(proj, "video", name)
            if not ok:
                continue
            clip = V.new_clip(self._src_for(path), 0.0, 10.0)
            lay = clip["layout"]
            lay["x"], lay["y"], lay["scale"] = x, 0.70, 0.6
            V.add_clip(proj, tid, clip)
        proj["dialogue"] = [
            {"t": 0.5, "dur": 3.0, "who": "旁白",
             "text": "这是一个内置的演示模板。"},
            {"t": 4.0, "dur": 3.0, "who": "角色",
             "text": "背景与立绘均为程序生成，可自由编辑。"},
        ]
        self.proj = proj
        self.proj_dir = None
        self._loading = True
        self._apply_project(None)
        self._loading = False
        self._seed_history()
        self.seek(0.0)
        self.push_history()
        InfoBar.success("模板", "已载入「单背景+双立绘演示」", parent=self,
                       position=InfoBarPosition.TOP, duration=2500)

    def _import_webgal_dir(self):
        """导入 WebGAL game 目录：background→底轨，figure→上层轨（尽力而为）。"""
        d = QFileDialog.getExistingDirectory(self, "选择 WebGAL 游戏目录（含 game\\）")
        if not d:
            return
        game = os.path.join(d, "game")
        if not os.path.isdir(game):
            game = d
        bg_dir = os.path.join(game, "background")
        fig_dir = os.path.join(game, "figure")
        img_ext = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
        bg_files = sorted(f for f in os.listdir(bg_dir)
                          if os.path.isdir(bg_dir) and f.lower().endswith(img_ext)) \
            if os.path.isdir(bg_dir) else []
        fig_files = sorted(f for f in os.listdir(fig_dir)
                           if os.path.isdir(fig_dir) and f.lower().endswith(img_ext)) \
            if os.path.isdir(fig_dir) else []
        self._sources.close_all()
        proj = V.new_project()
        proj["tracks"] = []
        proj["dialogue"] = []
        n = 0
        if bg_files:
            ok, _m, tid = V.add_track(proj, "video", "BG")
            if ok:
                t0 = 0.0
                for fn in bg_files[:8]:
                    # 同轨不允许重叠：背景图依次首尾相接
                    V.add_clip(proj, tid,
                               V.new_clip(self._src_for(os.path.join(bg_dir, fn)),
                                          t0, 10.0))
                    n += 1
                    t0 += 10.0
        fig_idx = 1
        for fn in fig_files[:12]:
            ok, _m, tid = V.add_track(proj, "video", "FIG-%d" % fig_idx)
            if not ok:
                continue
            clip = V.new_clip(self._src_for(os.path.join(fig_dir, fn)), 0.0, 10.0)
            lay = clip["layout"]
            lay["x"], lay["y"], lay["scale"] = 0.5, 0.70, 0.6
            V.add_clip(proj, tid, clip)
            n += 1
            fig_idx += 1
        gname = ""
        cfg = os.path.join(game, "config.txt")
        if os.path.isfile(cfg):
            try:
                for line in open(cfg, encoding="utf8", errors="ignore"):
                    if line.startswith("Game_name:"):
                        gname = line.split(":", 1)[1].strip().rstrip(";")
                        break
            except Exception:
                pass
        self.proj = proj
        self.proj_dir = d
        self._loading = True
        self._apply_project(None)
        self._loading = False
        self._seed_history()
        self.seek(0.0)
        self.push_history()
        InfoBar.success("导入 WebGAL", "已导入 %d 个素材%s" % (
            n, ("（%s）" % gname) if gname else ""),
            parent=self, position=InfoBarPosition.TOP, duration=3000)

    # ---------- E2：WebGAL 运行时状态卡动作 ----------
    def _runtime_online(self):
        """在线获取 -> 指引到资产管理「WebGAL 引擎运行时」条目（E1 已注册）。"""
        InfoBar.info("WebGAL 运行时",
                     "请到「资产管理」菜单的「WebGAL 引擎运行时」条目下载",
                     parent=self, position=InfoBarPosition.TOP, duration=4000)

    def _runtime_import_confirm(self, zip_path, msg):
        """覆盖确认通过后，带 confirm=True 重跑 import_zip（后台线程）。"""
        self._runtime_busy = True

        def work():
            try:
                import runtime_pack as RP
                ok, msg2, _data = RP.import_zip(zip_path, confirm_overwrite=True)
            except Exception as e:
                ok, msg2 = False, "导入异常 %s: %s" % (type(e).__name__, e)

            def done():
                self._runtime_busy = False
                if ok:
                    self._refresh_runtime_cards()
                    InfoBar.success("运行时导入", msg2, parent=self,
                                    position=InfoBarPosition.TOP, duration=4000)
                else:
                    InfoBar.warning("运行时导入", msg2, parent=self,
                                    position=InfoBarPosition.TOP, duration=5000)
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self, done)

        threading.Thread(target=work, daemon=True).start()

    def _runtime_import_zip(self):
        """选 zip -> 后台导入；已装需确认覆盖 -> 主线程确认框后 confirm=True。"""
        if getattr(self, "_runtime_busy", False):
            InfoBar.info("导入中", "请等待当前导入完成", parent=self,
                         position=InfoBarPosition.TOP, duration=1800)
            return
        zip_path, _ = QFileDialog.getOpenFileName(
            self, "选择 WebGAL 运行时压缩包", "", "WebGAL 运行时压缩包 (*.zip)")
        if not zip_path:
            return
        self._runtime_busy = True

        def work():
            try:
                import runtime_pack as RP
                ok, msg, data = RP.import_zip(zip_path, confirm_overwrite=False)
            except Exception as e:
                ok, msg, data = False, "导入异常 %s: %s" % (type(e).__name__, e), None
            need_confirm = bool(data and data.get("needs_confirm"))

            def done():
                self._runtime_busy = False
                if need_confirm:
                    ret = QMessageBox.question(
                        self, "覆盖确认", "WebGAL 运行时已安装，是否覆盖？",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if ret == QMessageBox.Yes:
                        self._runtime_import_confirm(zip_path, msg)
                    else:
                        InfoBar.info("覆盖确认", "已取消覆盖", parent=self,
                                    position=InfoBarPosition.TOP, duration=2000)
                    return
                if ok:
                    self._refresh_runtime_cards()
                    InfoBar.success("运行时导入", msg, parent=self,
                                    position=InfoBarPosition.TOP, duration=4000)
                else:
                    InfoBar.warning("运行时导入", msg, parent=self,
                                    position=InfoBarPosition.TOP, duration=5000)
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self, done)

        threading.Thread(target=work, daemon=True).start()

    def _refresh_runtime_cards(self):
        """刷新运行时状态卡（未在模板页则忽略）。"""
        card = getattr(self, "_runtime_card", None)
        if card is not None:
            try:
                card.refresh()
            except Exception:
                pass

    def _show_script_editor(self, show):
        """文本 tab：显示剧情编辑器并隐藏素材库；其它 tab 反之。"""
        se = getattr(self, "script_editor", None)
        if se is None:
            return
        se.setVisible(bool(show))
        try:
            self.asset_scroll.setVisible(not show)
            self.asset_empty.setVisible(not show)
            self.btn_add_file.setVisible(not show)
            self.btn_add_puppet.setVisible(not show)
            self.asset_header.setText("剧情" if show else "素材库")
        except Exception:
            pass
        if show:
            se.refresh()

    def _filter_assets(self, key):
        self._asset_tab = str(key) if key else "media"
        self._rebuild_asset_cards()

    def _add_asset_to_timeline(self, src):
        """双击卡片：追加到对应轨末尾。"""
        kind = src["kind"]
        tid = "a1" if kind == "audio" else "v1"
        tr = V.find_track(self.proj, tid)
        if tr is None:
            ok, _m, tid = V.add_track(self.proj,
                                      "audio" if kind == "audio" else "video")
            if not ok:
                return
            tr = V.find_track(self.proj, tid)
        end = max([V.clip_end(c) for c in tr["clips"]] or [0.0])
        dur = DEFAULT_DUR.get(kind) or src.get("src_dur") or 3.0
        dur = max(V.MIN_DUR, min(dur, src["src_dur"]) if src.get("src_dur")
                  and kind != "image" else dur)
        clip = V.new_clip(src, end, dur)
        # K1（task-69）：同屏人数上限撤销——立绘上轨不再事前拦截。
        ok, cid = V.add_clip(self.proj, tid, clip)
        if not ok:
            InfoBar.warning("上轨失败", cid, parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
            return
        self.push_history()
        self.timeline.refresh()
        self._after_edit()

    # ================= 预览播放 =================
    def _preview_scale(self):
        return min(1.0, 720.0 / max(1, self.proj["height"]))

    def _sync_preset_sources(self, t):
        """把当前 t 时刻生效的 puppet clip 的 motion/expression 灌给其源。

        preset 是 clip 级状态，SourceManager 按素材缓存；每次 render 前按
        当前 clip 字段重算 -> seek/撤销/重做后不残留错值。
        """
        try:
            for tr in self.proj.get("tracks", []):
                if tr.get("kind") != "video" or tr.get("hidden"):
                    continue
                for c in (tr.get("clips") or []):
                    if not isinstance(c, dict):
                        continue
                    s0 = float(c.get("start", 0.0))
                    d = float(c.get("dur", 0.0))
                    if t < s0 - 1e-6 or t >= s0 + d - 1e-6:
                        continue
                    if (c.get("source") or {}).get("kind") != "puppet":
                        continue
                    try:
                        src = self._sources.get(c["source"])
                        src.set_preset(c.get("motion", ""),
                                       c.get("expression", ""))
                    except Exception:
                        pass
        except Exception:
            pass

    def render_frame(self, t):
        self._sync_preset_sources(t)
        # v3.5.7：实时态引擎自绘，跳过重型合成（numpy 帧纯浪费）
        if getattr(self, "_preview_real", False):
            self.stage.playing = bool(self._playing)
            self.stage.tick_busy = bool(self._tick_busy)
            _se = getattr(self, "script_editor", None)
            if _se is not None:
                _se.refresh_highlight()
            self._sync_real_preview(t)
            return
        # F2-L5（v3.5）：多幕工程只合成**当前幕**（一幕一条独立时间轴）
        # v3.5.4：subtitle=False —— 工作台预览的台词由 StageView.paintEvent
        # 画（widget 自适应字号）；视频导出走 compose_frame 的烧录版。
        # 两条路同规格（MyGO 风），但各画各的，避免预览双重字幕。
        # v3.5.7：fx=False —— 预览不渲染滤镜链（逐帧 numpy ~330ms/帧卡爆预览；
        # 滤镜效果看实时预览或导出成片）。
        arr = VE.compose_frame(self.proj, t, self._sources.get,
                               aid=getattr(self, "_cur_aid", None),
                               subtitle=False, fx=False)
        # 交互门控：播放/逐帧合成中 StageView 跳过鼠标，但照常刷新画面与框
        self.stage.playing = bool(self._playing)
        self.stage.tick_busy = bool(self._tick_busy)
        self.stage.set_frame(arr, self.proj, t,
                              getattr(self, "_cur_aid", None))
        # F1：台词表当前时刻生效行高亮
        _se = getattr(self, "script_editor", None)
        if _se is not None:
            _se.refresh_highlight()
        # H3：实时预览 diff 同步（非实时态内部零开销直接 return）
        self._sync_real_preview(t)

    # ================= F2-L5（v3.5）：多幕 UI =================
    # 一幕一条独立时间轴（设计 §2.2）。单幕工程（v1）**整个幕条不出现**
    # -> 既有 UI 零 diff（564+ 既有测试不受影响）。
    def _act_ids(self):
        """当前工程的幕 id 列表（v1 单幕 -> ['a1'] 虚拟幕）。"""
        try:
            return V.act_ids(self.proj)
        except Exception:
            return ["a1"]

    def _is_multi(self):
        try:
            return V.is_multi_act(self.proj)
        except Exception:
            return False

    def _act_name(self, aid):
        try:
            a = V.find_act(self.proj, aid)
            if a:
                return str(a.get("name") or aid)
        except Exception:
            pass
        return str(aid)

    def _refresh_act_bar(self):
        """按工程当前幕数重建幕条（单幕 -> 隐藏整行）。

        幕 id 变了才重建（增删/改名/载入工程时调用）。"""
        ids = self._act_ids()
        if not self._is_multi() or len(ids) <= 1:
            # 单幕：整行隐藏，不建任何按钮（零 diff）
            self._act_bar_host.setVisible(False)
            for b in list(self._act_buttons.values()):
                self._act_bar_layout.removeWidget(b)
                b.deleteLater()
            self._act_buttons = {}
            self._cur_aid = ids[0] if ids else "a1"
            self._apply_cur_act()
            return
        # **游标归一必须先做**：载入/新建工程会把 _cur_aid 清成 None，
        # 而下面「幕集合没变」的早退分支不碰游标 -> 游标一直 None，
        # 分支面板/时间轴拿不到当前幕（实测 bug）。
        if self._cur_aid not in ids:
            self._cur_aid = ids[0]
        # 幕 id 集合没变 -> 只刷新名字（改名场景）
        if set(self._act_buttons.keys()) == set(ids):
            for aid, b in self._act_buttons.items():
                b.setText(self._act_name(aid))
            self._sync_act_buttons()
            self._apply_cur_act()
            return
        for b in list(self._act_buttons.values()):
            self._act_bar_layout.removeWidget(b)
            b.deleteLater()
        self._act_buttons = {}
        for aid in ids:
            b = PillPushButton(self._act_name(aid), self._act_bar_host)
            b.setCheckable(True)
            b.setFixedHeight(26)
            b.setToolTip("切到这一幕（%s）" % aid)
            b.clicked.connect(lambda _c=False, a=aid: self._switch_act(a))
            self._act_bar_layout.addWidget(b)
            self._act_buttons[aid] = b
        self._act_bar_layout.addStretch(1)
        self._act_add_btn = PushButton("+ 新增幕", self._act_bar_host)
        self._act_add_btn.setFixedHeight(26)
        self._act_add_btn.clicked.connect(self._act_add)
        self._act_bar_layout.addWidget(self._act_add_btn)
        # 操作按钮：改名 / 复制 / 删除
        for text, fn in (("改名", self._act_rename),
                         ("复制", self._act_duplicate),
                         ("删除", self._act_remove)):
            btn = PushButton(text, self._act_bar_host)
            btn.setFixedHeight(26)
            btn.clicked.connect(lambda _c=False, f=fn: f())
            self._act_bar_layout.addWidget(btn)
        self._act_bar_host.setVisible(True)
        self._sync_act_buttons()
        # 重建后同样要同步一次（构造期 / 载入工程时组件内容是旧的）
        self._apply_cur_act()

    def _sync_act_buttons(self):
        """让当前幕的按钮处于选中态（其余取消）。"""
        for aid, b in (self._act_buttons or {}).items():
            try:
                b.blockSignals(True)
                b.setChecked(aid == self._cur_aid)
                b.blockSignals(False)
            except Exception:
                pass

    def _apply_cur_act(self):
        """把当前幕游标同步给所有幕感知组件（时间轴/剧情编辑器/舞台）。"""
        aid = self._cur_aid
        try:
            self.timeline.set_act(aid)
        except Exception:
            pass
        # 剧情编辑器必须整表重刷：台词表与分支区都是按幕取的数据，
        # 只 render_frame 不够（那只刷高亮）——切幕后表格会停在旧幕内容。
        try:
            self.script_editor.refresh()
        except Exception:
            pass
        try:
            self.render_frame(self._t)
        except Exception:
            pass
        try:
            self._fill_clip_props()
        except Exception:
            pass

    def _switch_act(self, aid):
        """切当前幕（时间轴/剧情编辑器/舞台都跟着切）。"""
        if aid == self._cur_aid:
            return
        self._cur_aid = aid
        self._sync_act_buttons()
        self._apply_cur_act()

    def _act_add(self):
        """新增一幕（复用 video_model.add_act，不自己造数据逻辑）。"""
        try:
            ok, msg, aid = V.add_act(self.proj, None, self._cur_aid)
        except Exception as e:
            InfoBar.warning("新增幕", "失败：%s" % e, parent=self,
                            position=InfoBarPosition.TOP, duration=4000)
            return
        if not ok:
            InfoBar.warning("新增幕", msg or "失败", parent=self,
                            position=InfoBarPosition.TOP, duration=4000)
            return
        self._cur_aid = aid
        self.push_history()
        self._refresh_act_bar()
        self._apply_cur_act()
        InfoBar.success("新增幕", "已创建 %s" % aid, parent=self,
                        position=InfoBarPosition.TOP, duration=3000)

    def _act_rename(self, aid=None):
        """重命名当前幕（弹简单输入框）。"""
        aid = aid or self._cur_aid
        old = self._act_name(aid)
        dlg = _ActNameDialog(self, old)
        if not dlg.exec():
            return
        name = dlg.value().strip()
        if not name:
            InfoBar.warning("重命名幕", "幕名不能为空", parent=self,
                            position=InfoBarPosition.TOP, duration=3000)
            return
        try:
            ok, msg = V.rename_act(self.proj, aid, name)
        except Exception as e:
            ok, msg = False, str(e)
        if not ok:
            InfoBar.warning("重命名幕", msg or "失败", parent=self,
                            position=InfoBarPosition.TOP, duration=4000)
            return
        self.push_history()
        self._refresh_act_bar()

    def _act_duplicate(self, aid=None):
        """复制当前幕。"""
        aid = aid or self._cur_aid
        try:
            ok, msg, new_aid = V.duplicate_act(self.proj, aid)
        except Exception as e:
            ok, msg, new_aid = False, str(e), None
        if not ok:
            InfoBar.warning("复制幕", msg or "失败", parent=self,
                            position=InfoBarPosition.TOP, duration=4000)
            return
        self._cur_aid = new_aid
        self.push_history()
        self._refresh_act_bar()
        self._apply_cur_act()

    def _act_remove(self, aid=None):
        """删除当前幕（级联清引用，提示清理了几处）。"""
        aid = aid or self._cur_aid
        try:
            ok, msg, cleared = V.remove_act(self.proj, aid)
        except Exception as e:
            ok, msg, cleared = False, str(e), None
        if not ok:
            InfoBar.warning("删除幕", msg or "失败", parent=self,
                            position=InfoBarPosition.TOP, duration=4000)
            return
        self.push_history()
        self._refresh_act_bar()
        self._apply_cur_act()
        # remove_act 返回的是**清理了几处引用**（int），不是列表
        n = int(cleared or 0)
        InfoBar.success("删除幕", ("已删除 %s" % aid) +
                        ("，并清理 %d 处引用" % n if n else ""),
                        parent=self, position=InfoBarPosition.TOP, duration=4000)

    # ================= H3 实时预览（v3.5，设计 §5） =================
    def _preview_runtime_ready(self):
        """WebGAL 运行时是否已就位（决定要不要给「实时预览」角标）。

        v3.5：只查 WebEngine（available()）会出现「开关常亮但页面空白」——
        用户装了 WebEngine 可选件但**运行时素材没下载**（当前发行形态
        运行时与安装包分离），点开只能得到 no_runtime 错误页。
        所以这里额外查运行时是否存在，查不到就**连开关都不出现**
        （与 available()==False 同等对待：宁可不出现，也不要一个点不动的开关）。"""
        WP = getattr(self, "_wp", None)
        if WP is None or not WP.available():
            return False
        fn = getattr(WP, "runtime_ready", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return True
        # 没有 runtime_ready()（旧版预览模块）-> 不阻断（保持兼容）
        return True

    def _make_preview_badge(self, host):
        """预览区右上角「实时预览」角标开关。
        仅在「WebEngine 可用 + WebGAL 运行时已就位」时创建。"""
        btn = PushButton("实时预览", host)
        btn.setCheckable(True)
        btn.setFixedHeight(26)
        btn.setToolTip("用 WebGAL 引擎实时渲染立绘（布置辅助，不影响导出画质）")
        btn.toggled.connect(self._toggle_real_preview)
        self._preview_badge = btn
        self._badge_host = host
        host.installEventFilter(self)
        # v3.5.7：创建时容器还没布局（宽 0），且实时页会盖住先创建的角标；
        # 用 _place_preview_badge（含 raise_）统一摆放。
        self._place_preview_badge()
        btn.show()

    def _place_preview_badge(self):
        """角标随容器 resize 重摆 + 常驻顶层（v3.5.7：实时态页面会盖住
        先创建的角标，必须 raise_；创建时容器还没布局，宽度 0 定位错）。"""
        b = getattr(self, "_preview_badge", None)
        h = getattr(self, "_badge_host", None)
        if b is None or h is None:
            return
        b.adjustSize()
        b.move(max(6, h.width() - b.width() - 12), 12)
        b.raise_()

    def eventFilter(self, obj, ev):
        if obj is getattr(self, "_badge_host", None) \
                and ev.type() == QEvent.Resize:
            self._place_preview_badge()
        return super().eventFilter(obj, ev)

    def _ensure_webview(self):
        """懒创建 WebPreviewView（首次开实时预览时才建，避免启动开销）。"""
        if self._webview is not None:
            return self._webview
        if not getattr(self, "_wp", None) or not self._wp.available():
            return None
        try:
            v = self._wp.WebPreviewView(parent=self)
            # 交互恒归 StageView（设计 §5）：实时页纯显示
            v.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            # 页面 module 就绪前下发命令会被 bootstrap 丢弃（其
            # `if (window.__muppetOnCmd)` 为假即丢）——而 boot() 只在
            # loadModel 里被调用，丢弃后引擎永不启动。故必须等页面
            # 就绪再推首帧状态（v3.5 实测根因）。
            self._preview_ready = False
            v.pageReady.connect(self._on_preview_page_ready)
            # J3 §8-7：连续机制级 error 达阈值 -> 引擎自动降级，
            # UI 侧取消勾选 + 置灰（会话内不再弹，置灰保持）。
            try:
                v.previewDisabled.connect(self._on_preview_disabled)
            except Exception:
                pass
            v.start()
            self._webview = v
            self._stack.addWidget(v)
            # v3.5.7：实时页加入后会盖住角标，重新摆到顶层
            self._place_preview_badge()
            return v
        except Exception as e:
            InfoBar.warning("实时预览", "初始化失败：%s" % e, parent=self,
                            position=InfoBarPosition.TOP, duration=4000)
            return None

    def _on_preview_disabled(self):
        """J3 §8-7：预览引擎降级（连续机制级 error）-> 取消勾选 + 置灰。"""
        try:
            if getattr(self, "_preview_real", False):
                self._preview_real = False
                if self._stack is not None:
                    self._stack.setCurrentIndex(0)   # 回 StageView
                b = getattr(self, "_preview_badge", None)
                if b is not None:
                    b.blockSignals(True)
                    b.setChecked(False)
                    b.blockSignals(False)
        except Exception:
            pass
        b = getattr(self, "_preview_badge", None)
        if b is not None:
            b.setEnabled(False)
            b.setToolTip("实时预览已自动停用：引擎连续报错（多半是模型文件缺失）"
                         "，修好模型后可重新开启")
        InfoBar.warning("实时预览", "引擎连续报错，已自动停用实时预览",
                        parent=self, position=InfoBarPosition.TOP, duration=8000)

    def _on_preview_page_ready(self, ok):
        """宿主页 loadFinished：module script 还需一点时间求值，延迟推首帧。"""
        if not ok:
            return
        self._preview_ready = True
        QTimer.singleShot(400, self._push_preview_state)

    def _push_preview_state(self, force=True):
        """把当前时刻的立绘状态推给实时预览（内部做越界/异常兜底）。"""
        v = self._webview
        if v is None or not getattr(self, "_preview_ready", False):
            return
        # v3.5.2：同 _sync_real_preview —— 幕游标是 `_cur_aid` 不是 `_aid`
        aid = getattr(self, "_cur_aid", None)
        try:
            v._aid = aid
        except Exception:
            pass
        try:
            view = V.act_view(self.proj, aid)
            v.apply_state(view, self._t, force=force)
        except TypeError:
            try:
                v.apply_state(self.proj, self._t, force=force)
            except Exception:
                pass
        except Exception:
            pass

    def _toggle_real_preview(self, on):
        """切实时/布局态。实时态=WebEngine 画面为底；布局态=QPainter 帧。"""
        if not on:
            self._preview_real = False
            self._stack.setCurrentIndex(0)          # 回 StageView（不销毁）
            if self._webview is not None:
                self._webview.clear_all()           # 清掉页面侧模型
            self.render_frame(self._t)              # 立即回填当前帧，不闪白
            btn = getattr(self, "_preview_badge", None)
            if btn is not None:
                btn.setToolTip("用 WebGAL 引擎实时渲染立绘（布置辅助，不影响导出画质）")
            self._place_preview_badge()
            return
        v = self._ensure_webview()
        if v is None:
            btn = self._preview_badge
            if btn is not None:
                btn.blockSignals(True)
                btn.setChecked(False)
                btn.blockSignals(False)
            InfoBar.info("实时预览",
                         "实时预览不可用：需要 WebEngine 能力包与 WebGAL 运行时",
                         parent=self, position=InfoBarPosition.TOP,
                         duration=4000)
            return
        self._preview_real = True
        self._stack.setCurrentIndex(1)
        btn = getattr(self, "_preview_badge", None)
        if btn is not None:
            btn.setToolTip("正在实时预览：再点一次返回普通预览（更快，不渲染滤镜）")
        self._place_preview_badge()
        # 页面已就绪就立刻推；否则等 pageReady 回调（见 _on_preview_page_ready）
        if getattr(self, "_preview_ready", False):
            self._push_preview_state(force=True)

    def _sync_real_preview(self, t):
        """render_frame 尾部调用：实时态下按 diff 同步（不实时态零开销）。"""
        if not self._preview_real or self._webview is None:
            return
        if not getattr(self, "_preview_ready", False):
            return                       # 页面未就绪：等 pageReady 后统一推
        # v3.5.2：当前幕要传对。
        # 原来这里写 getattr(self, "_aid", None) —— workbench 上根本没有
        # `_aid` 属性（幕游标叫 `_cur_aid`），永远拿到 None = 恒按第一幕
        # 取数据。单幕无感，多幕必错（切到 a2 预览还在推 a1 的背景/台词）。
        aid = getattr(self, "_cur_aid", None)
        try:
            self._webview._aid = aid      # 背景/字幕/滤镜三层也按幕取
        except Exception:
            pass
        try:
            view = V.act_view(self.proj, aid)
            self._webview.apply_state(view, t)
        except TypeError:
            try:
                self._webview.apply_state(self.proj, t)
            except Exception:
                pass
        except Exception:
            pass

    def seek(self, t):
        self._t = float(max(0.0, min(t, V.project_duration(self.proj))))
        self.timeline.set_time(self._t)
        self.render_frame(self._t)
        self._update_transport()

    def _seek_relative(self, direction, seconds=False):
        """v3.5.2：←/→ 逐帧走，Shift+←/→ ±1 秒，Home/End 由 seek() 直达。

        direction ±1；seconds=True 时步长 1 秒，否则 1 帧（1/fps）。
        播放中同样生效（时间轴/预览跟着跳）。"""
        step = 1.0 if seconds else 1.0 / max(1.0, float(
            self.proj.get("fps") or 30.0))
        self.seek(self._t + direction * step)

    def _advance(self):
        if self._tick_busy:
            return
        self._tick_busy = True
        try:
            step = 1.0 / float(self.proj["fps"])
            nt = self._t + step
            if nt >= V.project_duration(self.proj):
                self.stop_play()
                self.seek(V.project_duration(self.proj))
                return
            self._t = nt
            self.timeline.set_time(nt)
            self.render_frame(nt)
            self._update_transport()
        finally:
            self._tick_busy = False

    def toggle_play(self):
        if self._playing:
            self.stop_play()
        else:
            if V.project_duration(self.proj) <= 0:
                return
            if self._t >= V.project_duration(self.proj) - 1e-6:
                self.seek(0.0)
            self._playing = True
            self.btn_play.setIcon(FluentIcon.PAUSE.icon())
            self.timer.setInterval(max(1, int(round(1000.0 / self.proj["fps"]))))
            self.timer.start()

    def stop_play(self):
        self._playing = False
        self.timer.stop()
        self.btn_play.setIcon(FluentIcon.PLAY.icon())

    def _update_transport(self):
        dur = V.project_duration(self.proj)
        self.time_lbl.setText("%s / %s" % (fmt_time(self._t), fmt_time(dur)))
        self.sld_pos.setValue(int(round(self._t / dur * 1000)) if dur > 0 else 0)
        self.dur_lbl.setText("总时长：%s" % fmt_time(dur))

    # ================= 时间轴联动 =================
    def _on_selection(self, clip_id):
        self._sel_clip = clip_id
        self._fill_clip_props()

    def _fill_clip_props(self):
        tr, c = V.find_clip(self.proj, self._sel_clip) if self._sel_clip \
            else (None, None)
        on = c is not None
        for sp in (self.spin_start, self.spin_in, self.spin_dur):
            sp.setEnabled(on)
        self.btn_del_clip.setEnabled(on)
        # 布局组：有选中即显示
        self.lay_group.setVisible(on)
        # puppet 预置动作组：仅 puppet 片段显示
        is_puppet = bool(on and (c.get("source") or {}).get("kind") == "puppet")
        self.act_group.setVisible(is_puppet)
        self._loading = True
        if on:
            self.clip_info.setText("%s\n%s" % (
                os.path.basename(str(c["source"]["path"]).rstrip("\\/")),
                c["source"]["kind"]))
            self.spin_start.setValue(c["start"])
            self.spin_in.setValue(c["src_in"])
            self.spin_dur.setValue(c["dur"])
            lay = V.layout_at(c)          # 读布局只走 layout_at
            self.spin_lay_x.setValue(lay["x"])
            self.spin_lay_y.setValue(lay["y"])
            self.spin_lay_scale.setValue(lay["scale"])
            self.spin_lay_rot.setValue(lay["rot"])
            self._set_combo_by_data(self.cmb_motion, c.get("motion", ""),
                                   self._motion_ids)
            self._set_combo_by_data(self.cmb_expr, c.get("expression", ""),
                                   self._expression_ids)
        else:
            self.clip_info.setText("未选中")
            for sp in (self.spin_start, self.spin_in, self.spin_dur):
                sp.setValue(0.0)
            for sp in (self.spin_lay_x, self.spin_lay_y,
                       self.spin_lay_scale, self.spin_lay_rot):
                sp.setValue(0.0)
            self._set_combo_by_data(self.cmb_motion, "", self._motion_ids)
            self._set_combo_by_data(self.cmb_expr, "", self._expression_ids)
        self._loading = False

    def _set_combo_by_data(self, combo, val, ids):
        """按 id（并行列表）设置下拉当前项，不触发信号。"""
        try:
            combo.blockSignals(True)
            if val and val in ids:
                combo.setCurrentIndex(ids.index(val))
            combo.blockSignals(False)
        except Exception:
            pass

    # ---------- A2 StageView 联动 ----------
    def _on_stage_select(self, clip_id):
        """StageView 点选 -> 联动时间轴 + 刷新属性面板。"""
        cid = clip_id or None
        self._sel_clip = cid
        setter = getattr(self.timeline, "_set_sel", None)
        if setter is not None:
            try:
                setter(cid)
            except Exception:
                pass
        self._fill_clip_props()
        self.timeline.refresh()

    def _on_stage_layout_changed(self):
        """StageView 拖拽/手柄改布局后：压历史 + 重渲染 + 刷新时间轴。"""
        self.push_history()
        self.render_frame(self._t)
        try:
            self.timeline.refresh()
        except Exception:
            pass

    def _layout_spin_changed(self, key):
        """属性面板改布局：layout_at 读 -> 原地写 clip["layout"] -> 压历史。"""
        if self._loading or not self._sel_clip:
            return
        tr, c = V.find_clip(self.proj, self._sel_clip)
        if c is None:
            return
        lay = V.layout_at(c)
        val = getattr(self, "spin_lay_" + key).value()
        if key == "rot":
            lay["rot"] = float(val)
        else:
            lay[key] = float(val)
        if not isinstance(c.get("layout"), dict):
            c["layout"] = V.default_layout()
        c["layout"][key] = lay[key]
        self.push_history()
        self.render_frame(self._t)

    def _align_clip(self, nx, ny):
        """六向对齐：左上基准换算到中心归一化，写回 layout。"""
        if not self._sel_clip:
            return
        tr, c = V.find_clip(self.proj, self._sel_clip)
        if c is None:
            return
        if not isinstance(c.get("layout"), dict):
            c["layout"] = V.default_layout()
        c["layout"]["x"] = float(nx)
        c["layout"]["y"] = float(ny)
        self.push_history()
        self.render_frame(self._t)
        self._fill_clip_props()

    @staticmethod
    def _motion_names():
        """预置动作展示名（来自 motion_table.catalog，C2 数据文件驱动）。"""
        try:
            import motion_table as MT
            return [m["name"] for m in MT.catalog()["motions"]]
        except Exception:
            return ["idle"]

    @staticmethod
    def _expression_names():
        """预置表情展示名（来自 motion_table.catalog）。"""
        try:
            import motion_table as MT
            return [e["name"] for e in MT.catalog()["expressions"]]
        except Exception:
            return []

    @staticmethod
    def _preset_choices(kind):
        """(id, name) 列表，供下拉 addItem(name, id) 写回 clip 字段。"""
        try:
            import motion_table as MT
            cat = MT.catalog()
            if kind == "motion":
                return [(m["id"], m["name"]) for m in cat["motions"]]
            return [(e["id"], e["name"]) for e in cat["expressions"]]
        except Exception:
            return []

    def _motion_changed(self, _idx=0):
        """预置动作下拉 -> 写 clip["motion"]（合法 id）+ 压历史 + 重渲染。"""
        if self._loading or not self._sel_clip:
            return
        tr, c = V.find_clip(self.proj, self._sel_clip)
        if c is None:
            return
        idx = self.cmb_motion.currentIndex()
        if idx < 0 or idx >= len(self._motion_ids):
            return
        c["motion"] = self._motion_ids[idx]
        self.push_history()
        self.render_frame(self._t)

    def _expression_changed(self, _idx=0):
        """预置表情下拉 -> 写 clip["expression"] + 压历史 + 重渲染。"""
        if self._loading or not self._sel_clip:
            return
        tr, c = V.find_clip(self.proj, self._sel_clip)
        if c is None:
            return
        idx = self.cmb_expr.currentIndex()
        if idx < 0 or idx >= len(self._expression_ids):
            return
        c["expression"] = self._expression_ids[idx]
        self.push_history()
        self.render_frame(self._t)

    def _play_clip_preview(self):
        """在选中片段起点 seek 预览（不做导出依赖）。"""
        if not self._sel_clip:
            return
        tr, c = V.find_clip(self.proj, self._sel_clip)
        if c is None:
            return
        self.seek(c["start"])

    def _clip_spin_changed(self, which):
        if self._loading or not self._sel_clip:
            return
        tr, c = V.find_clip(self.proj, self._sel_clip)
        if c is None:
            return
        if which == "start":
            ok, msg = V.move_clip(self.proj, self._sel_clip,
                                  self.spin_start.value())
        elif which == "in":
            nv = self.spin_in.value()
            d = nv - c["src_in"]
            ok = V.trim_clip(self.proj, self._sel_clip, "start",
                             c["start"] + d)[0]
            msg = "入点越界"
        else:
            ok, msg = V.trim_clip(self.proj, self._sel_clip, "end",
                                  c["start"] + self.spin_dur.value())
        if not ok:
            InfoBar.warning("修改被拒", msg, parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
            self._fill_clip_props()
            return
        self.push_history()
        self.timeline.refresh()
        self._after_edit()

    def _delete_selected(self):
        if self._sel_clip:
            ok, _m = V.remove_clip(self.proj, self._sel_clip)
            if ok:
                self._sel_clip = None
                self.push_history()
                self.timeline.refresh()
                self._after_edit()

    def _after_edit(self):
        """任何成功编辑后：刷新预览/时长/属性。"""
        self._update_transport()
        self._fill_clip_props()
        if not self._playing:
            self.render_frame(self._t)

    # K1（task-69）：同屏人数上限撤销——原 I1u 事后超限提示整体移除
    # （figure_* 助手保留在 video_model，供未来纯信息展示用）。

    # ================= 导出 =================
    def _open_export_dialog(self):
        dlg = _ExportDialog(self, self.proj)
        if dlg.exec():
            # v3.5.8：自定义帧率非法时阻止导出
            if not dlg._fps_valid():
                InfoBar.warning("帧率无效", "帧率需为 1-120 的整数，请修正后再导出",
                                parent=self, position=InfoBarPosition.TOP,
                                duration=4000)
                return
            path, scale, fps, workers, animate = dlg.result_values()
            self._start_export(path, scale, fps, workers, animate)

    def _start_export(self, path, scale, fps=None, workers=None, animate=None):
        self.stop_play()
        self.act_export.setEnabled(False)
        # v3.5.11：L2D 立绘预渲染（**必须在主线程做**——渲染依赖
        # QWebEngine 事件循环，导出 QThread 里会死锁）。只渲染唯一状态
        # （真实工程 8 个 / 0.2s），导出线程逐帧只查缓存。
        # 失败/无 WebEngine 时静默跳过，导出自动回退占位卡，不阻断。
        self.act_export.setText("准备立绘…")
        QApplication.processEvents()
        n_states = 0
        try:
            import l2d_prefetch as LP
            n_states = LP.build_for_project(self.proj)
        except Exception:
            n_states = 0
        if n_states:
            InfoBar.info("立绘已预渲染",
                         "%d 个立绘状态已就绪（导出视频含完整 L2D）" % n_states,
                         parent=self, position=InfoBarPosition.TOP,
                         duration=4000)
        # 逐帧动画（方案 A）：泵必须在**主线程**起 —— QTimer 要活在有事件
        # 循环的线程里，而 VideoExportWorker.run() 在 QThread，那儿起不了。
        # 上面的定格预渲染在动画模式下**照旧执行**：它是泵的兜底，某帧泵
        # 超时/失败就退回定格图，画面不会缺（宁可少一次眨眼）。
        if animate is None:
            animate = True
        self._export_animate = False
        if animate and n_states:
            try:
                import l2d_pump as LPM
                eff_fps = float(fps) if fps else float(self.proj.get("fps") or 30.0)
                pm = LPM.pump()
                # ring 容量按线程数放大：并行导出乱序窗口 ≈2×workers 帧，
                # 盖不住就要走「回拖重载」那条慢且重的路
                pm.configure(True, eff_fps, VE.normalize_workers(workers))
                self._export_animate = bool(pm.start())
                if not self._export_animate:
                    InfoBar.warning("逐帧动画未启用",
                                    "渲染泵起不来（%s），本次导出改用定格立绘"
                                    % pm.reason(),
                                    parent=self, position=InfoBarPosition.TOP,
                                    duration=5000)
            except Exception:
                self._export_animate = False
        # v3.5.8：导出进度计时（滑动平均估速 -> 剩余时间）
        self._export_t0 = time.monotonic()
        self._export_last_t = self._export_t0
        self._export_last_pct = 0
        self._export_rates = []
        self._export_worker = VE.VideoExportWorker(self.proj, path, scale,
                                                   fps=fps, workers=workers,
                                                   l2d_animate=self._export_animate,
                                                   parent=self)
        self._export_worker.progress.connect(self._export_progress)
        self._export_worker.done.connect(self._export_done)
        self._export_worker.start()

    def _export_progress(self, pct):
        # v3.5.8：滑动平均估速，剩余≈(100-pct)/速率；前两次回调数据不足只显示百分比
        t = time.monotonic()
        last_t = getattr(self, "_export_last_t", None)
        last_pct = getattr(self, "_export_last_pct", None)
        rates = list(getattr(self, "_export_rates", []))
        if last_t is not None and last_pct is not None and t > last_t \
                and pct > last_pct:
            dt = t - last_t
            if dt > 0:
                inst = (pct - last_pct) / dt
                if inst > 0:
                    rates = (rates + [inst])[-3:]
        if last_t is not None:
            self._export_last_t = t
        self._export_last_pct = pct
        self._export_rates = rates
        if len(rates) >= 2:
            rate = sum(rates) / len(rates)
            if rate > 0 and pct < 100:
                remain = (100.0 - pct) / rate
                if remain > 0:
                    secs = int(round(remain))
                    self.act_export.setText("导出 %d%% · 剩约 %d 分 %d 秒"
                                            % (pct, secs // 60, secs % 60))
                    return
        self.act_export.setText("导出 %d%%" % pct)

    def _export_done(self, ok, msg):
        for _k in ("_export_t0", "_export_last_t", "_export_last_pct",
                   "_export_rates"):
            try:
                delattr(self, _k)
            except AttributeError:
                pass
        # 逐帧动画：收泵。stop() 会停 QTimer、把页面时钟交回墙上时间，并
        # 唤醒还在等的线程 —— 不 stop 的话下次预览/导出会接手一个被暂停
        # 的 ticker 和残留 slot。
        if getattr(self, "_export_animate", False):
            self._export_animate = False
            try:
                import l2d_pump as LPM
                pm = LPM.pump()
                st = dict(pm.stats)
                pm.stop()
                pm.configure(False)
                bad = st.get("timeouts", 0) + st.get("failed", 0)
                if bad:
                    msg = "%s（立绘动画 %d 帧，其中 %d 帧超时/失败已回落定格）" % (
                        msg, st.get("frames", 0), bad)
            except Exception:
                pass
        self.act_export.setEnabled(True)
        self.act_export.setText("导出")
        (InfoBar.success if ok else InfoBar.error)(
            "导出完成" if ok else "导出失败", msg, parent=self,
            position=InfoBarPosition.TOP, duration=5000)


class _ExportDialog(MessageBoxBase):
    """剪映式导出参数：分辨率（精确像素）/ 帧率 / 耗时预估。"""

    # v3.5.8：分辨率档位 scale（跟工程 1.0 起），帧率档位（None=跟工程）
    _RES_SCALES = [1.0, 0.6667, 0.5, 0.4444]
    _RES_TAGS = ["跟工程", "720p", "540p", "480p"]
    _FPS_VALS = [None, 60, 30, 24, 15]

    # v3.5.8：并行线程加速比（实测曲线粗略拟合，>8 按 2.1x）
    _SPEEDUP = {1: 1.0, 2: 1.6, 3: 1.85, 4: 2.0, 5: 2.1,
                6: 2.15, 7: 2.15, 8: 2.1}

    @staticmethod
    def _speedup(n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            return 1.0
        return _ExportDialog._SPEEDUP.get(n, 2.1 if n > 8 else 1.0)

    def __init__(self, parent, proj):
        super().__init__(parent)
        self.proj = proj
        self.viewLayout.addWidget(StrongBodyLabel("导出视频"))
        r = QHBoxLayout()
        r.addWidget(CaptionLabel("保存路径"))
        default = os.path.join(
            PROJ_BASE, (os.path.basename(parent.proj_dir)
                        if getattr(parent, "proj_dir", None)
                        else unique_video_name()) + ".mp4")
        self.ed_path = LineEdit()
        self.ed_path.setText(default)
        r.addWidget(self.ed_path, 1)
        self.viewLayout.addLayout(r)

        pw = int(proj.get("width", 1920))
        ph = int(proj.get("height", 1080))
        # 分辨率：精确像素，用 proj 实际宽高算目标并四舍五入到偶数
        res_labels = []
        for sc, tag in zip(self._RES_SCALES, self._RES_TAGS):
            tw = max(2, int(round(pw * sc / 2)) * 2)
            th = max(2, int(round(ph * sc / 2)) * 2)
            if tag == "跟工程":
                res_labels.append("跟工程 (%d×%d)" % (tw, th))
            else:
                res_labels.append("%d×%d (%s)" % (tw, th, tag))
        r2 = QHBoxLayout()
        r2.addWidget(CaptionLabel("分辨率"))
        self.cmb_out = ComboBox()
        self.cmb_out.addItems(res_labels)
        r2.addWidget(self.cmb_out, 1)
        self.viewLayout.addLayout(r2)

        # 帧率：第一项显示工程实际 fps，末项「自定义…」配数字输入（1-120）
        pfps = float(proj.get("fps", 30) or 30)
        fps_labels = ["跟工程 (%dfps)" % int(round(pfps))]
        fps_labels += ["%dfps" % f for f in self._FPS_VALS[1:]]
        fps_labels.append("自定义…")
        r3 = QHBoxLayout()
        r3.addWidget(CaptionLabel("帧率"))
        self.cmb_fps = ComboBox()
        self.cmb_fps.addItems(fps_labels)
        r3.addWidget(self.cmb_fps, 1)
        self.ed_fps = LineEdit(self)
        self.ed_fps.setPlaceholderText("1-120")
        self.ed_fps.setMaximumWidth(72)
        try:
            from PySide6.QtGui import QIntValidator
            self.ed_fps.setValidator(QIntValidator(1, 120, self.ed_fps))
        except Exception:
            pass
        self.ed_fps.editingFinished.connect(self._validate_fps)
        r3.addWidget(self.ed_fps)
        self.ed_fps.setVisible(False)
        self.viewLayout.addLayout(r3)

        # 并行线程：1..upper_w，default_w 项标「（推荐）」；选项取自
        # video_export.export_worker_limits()，不自己猜核数。
        try:
            import video_export as _VE
            dw, uw = _VE.export_worker_limits()
            dw, uw = int(dw), int(uw)
        except Exception:
            dw, uw = 1, max(1, int(os.cpu_count() or 1))
        if dw < 1 or dw > uw:
            dw = max(1, min(uw, dw))
        self._worker_dw, self._worker_uw = dw, uw
        w_labels = ["%d 线程%s" % (n, "（推荐）" if n == dw else "")
                    for n in range(1, uw + 1)]
        r4 = QHBoxLayout()
        r4.addWidget(CaptionLabel("并行线程"))
        self.cmb_workers = ComboBox()
        self.cmb_workers.addItems(w_labels)
        self.cmb_workers.setCurrentIndex(max(0, dw - 1))
        r4.addWidget(self.cmb_workers, 1)
        self.viewLayout.addLayout(r4)
        cpu = max(1, int(os.cpu_count() or 1))
        self.workers_hint = CaptionLabel(
            "线程越多越快，但受内存带宽限制，超过甜点反而变慢；"
            "默认值已按本机核数（%d 核）实测调过" % cpu, self)
        self.workers_hint.setWordWrap(True)
        self.workers_hint.setStyleSheet("color:#8a93a2; font-size:11px;")
        self.viewLayout.addWidget(self.workers_hint)

        # 立绘动画：逐帧（方案 A，主线程渲染泵）/ 定格（方案 B，预渲染图）。
        # 默认逐帧——用户明确要动画优先；代价写在提示里，别让人事后惊讶。
        r5 = QHBoxLayout()
        r5.addWidget(CaptionLabel("立绘动画"))
        self.cmb_anim = ComboBox()
        self.cmb_anim.addItems(["逐帧动画（人物会呼吸眨眼，较慢）",
                                "定格（快，人物不动）"])
        self.cmb_anim.setCurrentIndex(0)
        r5.addWidget(self.cmb_anim, 1)
        self.viewLayout.addLayout(r5)
        self.anim_hint = CaptionLabel(
            "逐帧：人物呼吸眨眼真的在动。开销随工程而变——合成越重的工程"
            "（视频轨/滤镜）越能被并行隐藏，实测只慢 10% 上下；素材简单的"
            "工程慢 2-3 倍。两种模式产物都可逐位复现。", self)
        self.anim_hint.setWordWrap(True)
        self.anim_hint.setStyleSheet("color:#8a93a2; font-size:11px;")
        self.viewLayout.addWidget(self.anim_hint)

        # 耗时预估（CaptionLabel，随任一下拉变化刷新）
        self.est_lbl = CaptionLabel("耗时预估：—", self)
        self.est_lbl.setWordWrap(True)
        self.est_lbl.setStyleSheet("color:#8a93a2; font-size:11px;")
        self.viewLayout.addWidget(self.est_lbl)

        self.cmb_out.currentIndexChanged.connect(self._refresh_estimate)
        self.cmb_fps.currentIndexChanged.connect(self._on_fps_changed)
        self.cmb_workers.currentIndexChanged.connect(self._refresh_estimate)
        self.cmb_anim.currentIndexChanged.connect(self._refresh_estimate)
        self._refresh_estimate()

        self.yesButton.setText("开始导出")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(480)

    def _fps_custom(self):
        return self.cmb_fps.currentIndex() >= len(self._FPS_VALS)

    def _fps_value(self):
        """当前所选帧率：档位返回数字（None=跟工程）；自定义解析输入，非法返回 None。"""
        if not self._fps_custom():
            return self._FPS_VALS[max(0, self.cmb_fps.currentIndex())]
        txt = (self.ed_fps.text() or "").strip()
        try:
            v = int(txt)
        except ValueError:
            return None
        return v if 1 <= v <= 120 else None

    def _fps_valid(self):
        """帧率是否合法（自定义时校验 1-120 整数）。"""
        if not self._fps_custom():
            return True
        txt = (self.ed_fps.text() or "").strip()
        try:
            v = int(txt)
        except ValueError:
            return False
        return 1 <= v <= 120

    def _on_fps_changed(self):
        custom = self._fps_custom()
        self.ed_fps.setVisible(custom)
        if custom:
            self.ed_fps.setFocus()
        self._refresh_estimate()

    def _validate_fps(self):
        if self._fps_custom() and not self._fps_valid():
            InfoBar.warning("帧率无效", "帧率需为 1-120 的整数",
                            parent=self, position=InfoBarPosition.TOP,
                            duration=3000)

    def _refresh_estimate(self):
        """按当前分辨率+帧率估算「预计约 X 分钟（N 帧）」。

        帧数 = 工程时长 × 所选帧率；每帧毫秒 ≈ 96.8×(所选高/1080)² + 5.3×(所选高/1080)²
        （线性外推；粗略预估，实际随素材复杂度浮动）。
        """
        try:
            p = self.proj
            dur = V.project_duration(p)
            sc = self._RES_SCALES[max(0, self.cmb_out.currentIndex())]
            th = max(2, int(round(int(p.get("height", 1080)) * sc / 2)) * 2)
            r2 = (th / 1080.0) ** 2
            fps = self._fps_value()
            if self._fps_custom() and fps is None:
                self.est_lbl.setText(
                    "帧率需为 1-120 的整数 · 粗略预估，实际随素材复杂度浮动")
                return
            if fps is None:
                fps = float(p.get("fps", 30) or 30)
            frames = max(1, int(round(dur * fps)))
            workers = self._workers_value()
            sp = self._speedup(workers)
            ms_per = (96.8 * r2 + 5.3 * r2) / sp
            animate = self._animate_value()
            if animate:
                # 逐帧动画开销实测（同一工程 still/anim 对比）：
                #  · 合成贵的工程（视频轨+滤镜）：主线程取图与 worker 合成
                #    天然重叠，只慢 6-12%；
                #  · 合成便宜的工程（静态图+立绘）：取图（~13ms/帧）成为
                #    瓶颈，慢 2-3 倍；
                #  · workers=1 时两者串行相加，最慢。
                # 取 2.0x 是区间中偏保守的一档：宁可让用户提前看到完成，
                # 也不要 ETA 一路往后跳。
                ms_per *= 2.0
            minutes = frames * ms_per / 60000.0
            self.est_lbl.setText(
                "预计约 %.1f 分钟（%d 帧）· %s · 粗略预估，实际随素材复杂度浮动"
                % (minutes, frames,
                   "含立绘逐帧动画" if animate else "立绘定格（快）"))
        except Exception:
            self.est_lbl.setText("耗时预估：— · 粗略预估，实际随素材复杂度浮动")

    def _workers_value(self):
        """当前所选并行线程数（1..upper_w）。"""
        try:
            return max(1, int(self.cmb_workers.currentIndex()) + 1)
        except Exception:
            return 1

    def _animate_value(self):
        """立绘动画开关：0=逐帧动画，1=定格。异常一律回默认（逐帧）。"""
        try:
            return self.cmb_anim.currentIndex() != 1
        except Exception:
            return True

    def result_values(self):
        scale = self._RES_SCALES[max(0, self.cmb_out.currentIndex())]
        fps = self._fps_value()
        workers = self._workers_value()
        return (os.path.normpath(self.ed_path.text().strip()), scale, fps,
                workers, self._animate_value())

class _ActNameDialog(MessageBoxBase):
    """F2-L5：幕重命名的单行输入对话框。"""

    def __init__(self, parent, old=""):
        super().__init__(parent)
        self.titleLabel.setText("重命名幕")
        self.contentLabel.setText("给这一幕起个名字：")
        self.ed = LineEdit(self)
        self.ed.setText(str(old or ""))
        self.ed.setPlaceholderText("幕名")
        self.viewLayout.addWidget(self.ed)
        self.yesButton.setText("确定")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(360)

    def value(self):
        return self.ed.text()


# 注：窗口壳（FluentWindow 包装）在 l2d_rig_qt.FluentShellWindow 里做，
# 本模块只出纯 QWidget 工作台，避免双向 import。
