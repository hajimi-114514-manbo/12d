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

from PySide6.QtCore import Qt, QTimer, QSize, QSettings
from PySide6.QtGui import QImage, QPixmap, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                               QFileDialog, QStackedLayout, QFrame, QSplitter,
                               QMessageBox)
from qfluentwidgets import (Pivot, CardWidget, PrimaryPushButton, PushButton,
                            ToolButton, ComboBox, SpinBox, DoubleSpinBox,
                            StrongBodyLabel, BodyLabel, CaptionLabel,
                            LineEdit, ScrollArea, InfoBar, InfoBarPosition,
                            MessageBoxBase, ProgressBar, FluentIcon,
                            FlowLayout)

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
        cl.addWidget(self.stage, 1)
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

        # ---------- 顶部区域（pv + 三列 splitter）----------
        top = QWidget(self)
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(0)
        tv.addWidget(pv)
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
        arr = VE.compose_frame(self.proj, t, self._sources.get)
        # 交互门控：播放/逐帧合成中 StageView 跳过鼠标，但照常刷新画面与框
        self.stage.playing = bool(self._playing)
        self.stage.tick_busy = bool(self._tick_busy)
        self.stage.set_frame(arr, self.proj, t)
        # F1：台词表当前时刻生效行高亮
        _se = getattr(self, "script_editor", None)
        if _se is not None:
            _se.refresh_highlight()

    def seek(self, t):
        self._t = float(max(0.0, min(t, V.project_duration(self.proj))))
        self.timeline.set_time(self._t)
        self.render_frame(self._t)
        self._update_transport()

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
            path, scale = dlg.result_values()
            self._start_export(path, scale)

    def _start_export(self, path, scale):
        self.stop_play()
        self.act_export.setEnabled(False)
        self._export_worker = VE.VideoExportWorker(self.proj, path, scale,
                                                   parent=self)
        self._export_worker.progress.connect(self._export_progress)
        self._export_worker.done.connect(self._export_done)
        self._export_worker.start()

    def _export_progress(self, pct):
        self.act_export.setText("%d%%" % pct)

    def _export_done(self, ok, msg):
        self.act_export.setEnabled(True)
        self.act_export.setText("导出")
        (InfoBar.success if ok else InfoBar.error)(
            "导出完成" if ok else "导出失败", msg, parent=self,
            position=InfoBarPosition.TOP, duration=5000)


class _ExportDialog(MessageBoxBase):
    def __init__(self, parent, proj):
        super().__init__(parent)
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
        r2 = QHBoxLayout()
        r2.addWidget(CaptionLabel("分辨率"))
        self.cmb_out = ComboBox()
        self.cmb_out.addItems(["工程原始 (%d×%d)" % (proj["width"], proj["height"]),
                               "1080p (75%)", "720p (50%)", "480p (33%)"])
        r2.addWidget(self.cmb_out, 1)
        self.viewLayout.addLayout(r2)
        self.yesButton.setText("开始导出")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(480)

    def result_values(self):
        scale = (1.0, 0.75, 0.5, 0.33)[max(0, self.cmb_out.currentIndex())]
        return os.path.normpath(self.ed_path.text().strip()), scale

# 注：窗口壳（FluentWindow 包装）在 l2d_rig_qt.FluentShellWindow 里做，
# 本模块只出纯 QWidget 工作台，避免双向 import。
