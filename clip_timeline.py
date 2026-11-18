# -*- coding: utf-8 -*-
"""clip 多轨时间轴面板（v3.0.0 剪辑工作台第一阶段）。

数据模型唯一事实源是 video_model.py：面板**从不直接改 clip 数据**，
所有编辑一律走 VM.move_clip / trim_clip / split_clip / remove_clip
（吸附查询走 VM.snap_points / snap_time）。模型原地改 + 校验拒绝，
校验失败时数据未被改动，面板静默回弹（清掉预览偏移重画即可）。

布局：
    ┌───────────────────────────────────────────────┐
    │ [切分] [删除] [跟随]  80px/s   ...提示 │  ← 34px 工具条（FluentIcon 图标）
    ├──────────┬────────────────────────────────────┤
    │ (头列)   │  0s   1s   2s   3s  ← 22px 刻度尺  │
    │ 视频轨1  │ ▓▓clip▓▓   ▓▓clip▓▓    │  44px/行  │
    │   👁 🔒  │  ← 150px 固定轨道头列（v3.4.7 起为矢量自绘图标）             │
    │ 音频轨1  │ ▒▒▒波形▒▒▒             │  26px/行  │
    └──────────┴────────────────────────────────────┘

交互（与规格一致）：
  - 刻度尺 / 空白按下：播放头 scrub，连续发 seeked（吸附 0.01s），
    松手发 playhead_drag_stopped
  - 点 clip：选中（单选）；点空白：取消选中 + scrub
  - 拖 clip 主体：move_clip（实时预览位移，吸附到邻 clip 边界，
    命中时画 #ffd166 竖直辅助线；失败静默回弹）
  - 拖 clip 左右 6px 边缘：trim_clip('start'/'end')，同样吸附
  - 双击 clip：clip_activated(str)（重命名/属性由外层做）
  - Delete 键 / [🗑]：remove_clip 选中项
  - [✂]：在播放头处 split_clip 选中 clip（播放头不在内部则无变化）
  - [▶]：follow_playhead 开关；set_time 时自动横向跟随
  - Ctrl+滚轮：pixels_per_second 0.5~400 缩放，以鼠标时刻为锚
  - 锁定轨：clip 可选中但不可拖 / 不可 trim / 不可删
  - 隐藏轨：整行半透明（alpha 0.35），不可交互

约定：任何**成功**的模型操作后发一次 edit_committed()，
外层负责 push history + 重绘其它视图（先改数据、后 push）。

横向滚动条由外层负责（面板提供 content_width()/maybe_scroll_to）；
若外层把面板塞进 QScrollArea 内（宽度 > 视口），跟随滚动直接调
外层水平滚动条；否则用面板内部 view_t0 偏移。
"""
import os

from PySide6.QtCore import Qt, QPoint, QPointF, QRectF, QSize, Signal
from PySide6.QtGui import (QPainter, QColor, QPen, QBrush, QPainterPath,
                           QFont)
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
                               QSizePolicy, QAbstractScrollArea, QScrollBar)

import video_model as VM
import timeline_panel as TP          # 复用配色 / _nice_step，无循环依赖
import ui_theme as TH

# ---------------- 配色（跟随 ui_theme：深色 / 浅色磨砂白） ----------------
# 这些模块级名字由 _refresh_colors() 在切主题时整体重新赋值，
# 自绘代码每帧读全局名，自动跟随。
C_BG = TP.C_BG
C_ROW_A = TP.C_ROW_A
C_ROW_B = TP.C_ROW_B
C_HEAD_BG = TP.C_HEAD
C_LANE = TP.C_LANE
C_GRID = TP.C_GRID
C_TEXT = TP.C_TEXT
C_TEXT_HI = TP.C_TEXT_HI
C_PLAYHEAD = TP.C_PLAYHEAD
C_SNAP = QColor("#ffd166")           # 吸附辅助线
C_SEL_EDGE = QColor("#f2f6fa")       # 选中描边（主选中）
C_SEL_EDGE_DIM = QColor(242, 246, 250, 130)   # 多选非主选中（淡一档）
C_MARQUEE = QColor(120, 170, 255)    # 框选矩形
C_MARQUEE_FILL = QColor(120, 170, 255, 36)
C_WAVE = QColor("#bfe3cc")           # 波形折线
C_CLIP_EDGE = QColor("#10151d")      # clip 暗描边
C_OTHER = QColor("#5b6b82")          # 未知类型 clip 的兜底色
C_ICON = QColor("#f2f6fa")           # 轨道头图标（眼/锁）主色
C_ICON_LOCK = QColor("#ffd166")      # 锁定态金色
C_ICON_CUT = QColor("#ff7878")       # 隐藏态红斜杠
C_ICON_HOVER = QColor(255, 255, 255, 36)       # hover 底色
C_ICON_LOCK_FILL = QColor(255, 209, 102, 60)   # 锁体的淡金色填充
C_LOCKED_ROW = QColor(255, 209, 102, 18)       # 锁定行淡金底
C_GRID_SUB = QColor(58, 67, 86, 110)           # 秒内次刻度网格线
C_ICON_HOVER_A = 36                  # (保留给外部引用)
C_LOCKED_ROW_A = 18

# source.kind -> clip 底色
CLIP_COLORS = {
    "video": QColor("#3d6dbf"),
    "image": QColor("#c97b3d"),
    "puppet": QColor("#8a5fc0"),
    "audio": QColor("#3fa06b"),
}


def _refresh_colors():
    """按当前主题重算配色，并重刷所有存活面板。"""
    global C_BG, C_ROW_A, C_ROW_B, C_HEAD_BG, C_LANE, C_GRID, C_TEXT
    global C_TEXT_HI, C_PLAYHEAD, C_SNAP, C_SEL_EDGE, C_SEL_EDGE_DIM
    global C_MARQUEE, C_MARQUEE_FILL, C_WAVE, C_CLIP_EDGE, C_OTHER
    global C_ICON, C_ICON_LOCK, C_ICON_CUT, C_ICON_HOVER, C_ICON_LOCK_FILL
    global C_LOCKED_ROW, C_GRID_SUB, C_ICON_HOVER_A, C_LOCKED_ROW_A
    C_BG = TP.C_BG
    C_ROW_A = TP.C_ROW_A
    C_ROW_B = TP.C_ROW_B
    C_HEAD_BG = TP.C_HEAD
    C_LANE = TP.C_LANE
    C_GRID = TP.C_GRID
    C_TEXT = TP.C_TEXT
    C_TEXT_HI = TP.C_TEXT_HI
    C_PLAYHEAD = TP.C_PLAYHEAD
    C_SNAP = TH.qcolor("tl_snap")
    C_SEL_EDGE = TH.qcolor("tl_sel_edge")
    _se = TH.qcolor("tl_sel_edge")
    C_SEL_EDGE_DIM = QColor(_se.red(), _se.green(), _se.blue(), 130)
    C_MARQUEE = TH.qcolor("accent")
    C_MARQUEE_FILL = QColor(C_MARQUEE.red(), C_MARQUEE.green(),
                            C_MARQUEE.blue(), 36)
    C_WAVE = TH.qcolor("tl_wave")
    C_CLIP_EDGE = TH.qcolor("tl_clip_edge")
    C_OTHER = TH.qcolor("tl_link")
    C_ICON = TH.qcolor("tl_icon")
    C_ICON_LOCK = TH.qcolor("tl_icon_lock")
    C_ICON_CUT = TH.qcolor("tl_icon_cut")
    C_ICON_HOVER_A = int(TH.t("tl_icon_hover"))
    C_LOCKED_ROW_A = int(TH.t("tl_locked_row"))
    C_ICON_HOVER = QColor(C_ICON.red(), C_ICON.green(), C_ICON.blue(),
                          C_ICON_HOVER_A)
    C_ICON_LOCK_FILL = QColor(C_ICON_LOCK.red(), C_ICON_LOCK.green(),
                              C_ICON_LOCK.blue(), 60)
    C_LOCKED_ROW = QColor(C_ICON_LOCK.red(), C_ICON_LOCK.green(),
                          C_ICON_LOCK.blue(), C_LOCKED_ROW_A)
    C_GRID_SUB = QColor(C_GRID.red(), C_GRID.green(), C_GRID.blue(), 110)
    for k, tok in (("video", "clip_video"), ("image", "clip_image"),
                   ("puppet", "clip_puppet"), ("audio", "clip_audio")):
        CLIP_COLORS[k] = TH.qcolor(tok)
    for p in _PANELS:
        try:
            p._apply_theme()
        except Exception:
            pass


_PANELS = []
TH.register(_refresh_colors)
_refresh_colors()

# ---------------- 尺寸基准 ----------------
HEAD_H = 34                 # 工具条高度
RULER_H = 22                # 刻度尺高度
NAME_W = 150                # 左侧轨道头列宽（固定）
HSCROLL_H = 12              # v3.5.2：水平滚动条高度（内容超视口时出现）
ROW_VIDEO = 44              # 视频轨行高基准（×ui_scale）
ROW_AUDIO = 26              # 音频轨行高基准（×ui_scale）
EDGE_GRAB = 6               # clip 两端 trim 命中宽度（px，×ui_scale）
EYE_W = 24                  # 头列按钮宽度（v3.4.7：20→24，点击更好命中）
PPS_DEFAULT = 80.0
PPS_MIN = 0.5
PPS_MAX = 400.0
WHEEL_UP_FACTOR = 1.25
WHEEL_DOWN_FACTOR = 0.8
WHEEL_SCROLL_FACTOR = 3.0      # v3.5.2：无 Ctrl 滚轮的横向滚动速度（像素/格）
DRAG_THRESHOLD = 2          # 超过 2px 才算拖动（区分点选）
SNAP_EPS = VM.SNAP_STEP * 3     # 与 video_model.snap_time 默认 eps 一致

_HIDDEN_ALPHA = 90              # 隐藏轨 clip 透明度（0.35×255）


def _fmt_sec(t):
    """刻度文字：1.50 -> '1.5s'，2.00 -> '2s'。"""
    return ("%.2f" % t).rstrip("0").rstrip(".") + "s"


def _base_color(clip):
    return CLIP_COLORS.get(clip.get("source", {}).get("kind"), C_OTHER)


def _wave_of(clip):
    w = clip.get("source", {}).get("wave")
    return w if isinstance(w, (list, tuple)) and len(w) >= 2 else None


def _in(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class ClipTimelinePanel(QWidget):
    """clip 多轨时间轴面板。编辑全部委托 video_model。"""

    seeked = Signal(float)                 # 播放头跳转（已吸附 0.01s）
    selection_changed = Signal(object)     # clip id (str) 或 None
    edit_committed = Signal()              # 任何成功的模型操作
    playhead_drag_stopped = Signal()       # scrub 结束
    clip_activated = Signal(str)           # 双击 clip（外层可接重命名/属性）
    scale_changed = Signal(float)          # pps 变了（外层可同步滚动条范围）
    track_flags_changed = Signal(str)      # 轨道 hidden/locked 被点击切换

    def __init__(self, parent=None, proj=None, wb=None):
        super().__init__(parent)
        # wb：外层工作台引用（当前仅存储，数据一律走 self.proj + video_model）
        self.wb = wb
        # 存引用不拷贝：外层改数据后 refresh() 即可
        self.proj = proj if isinstance(proj, dict) else VM.new_project()
        self.pps = float(PPS_DEFAULT)          # pixels_per_second
        self.ui_scale = 1.0
        self.view_t0 = 0.0                     # 内部视图起点（秒）
        self.follow_playhead = False
        self._t = 0.0                          # 播放头时刻
        # 选择模型：_sels = 有序集合（可多选，框选/Ctrl 加选/Shift 连选），
        # _sel = 主选中（_sels 最后一个）。**保留 _sel 是给既有调用点**：
        # 外层 _on_selection、属性面板、paint 单选描边、测试断言都读它，
        # 单选场景下两者等价，多选时 _sel 仍是"最后操作的那个"。
        self._sels = []
        self._sel = None                       # 选中 clip id

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ---- 34px 工具条 ----
        # v3.4.7：emoji（✂🗑▶）在部分 Windows 字体配置下渲染成方块或黑白
        # 单色字形，与 Fluent 按钮风格不齐；统一换成 FluentIcon 图标按钮。
        from qfluentwidgets import PushButton, CaptionLabel, FluentIcon
        head = QWidget(self)
        head.setObjectName("clipTlHead")
        head.setStyleSheet("QWidget#clipTlHead{background:%s;}" % C_HEAD_BG.name())
        self._head = head
        hl = QHBoxLayout(head)
        hl.setContentsMargins(8, 0, 8, 0)
        hl.setSpacing(8)

        f_btn = QFont()
        f_btn.setPixelSize(11)
        self.btn_split = PushButton(FluentIcon.CUT, "切分", head)
        self.btn_split.setToolTip("在播放头位置切分选中的 clip")
        self.btn_split.clicked.connect(self.split_selected_at_playhead)
        self.btn_delete = PushButton(FluentIcon.DELETE, "删除", head)
        self.btn_delete.setToolTip("删除选中的 clip")
        self.btn_delete.clicked.connect(self.remove_selected)
        self.btn_follow = PushButton(FluentIcon.PLAY, "跟随", head)
        self.btn_follow.setCheckable(True)
        self.btn_follow.setToolTip("播放头移动时自动横向滚动跟随")
        self.btn_follow.toggled.connect(self._on_follow_toggled)
        for b in (self.btn_split, self.btn_delete, self.btn_follow):
            b.setFixedHeight(22)
            b.setFont(f_btn)
            hl.addWidget(b)

        hl.addSpacing(6)
        self.lbl_pps = CaptionLabel("%.0f px/s" % self.pps, head)
        hl.addWidget(self.lbl_pps)

        hl.addStretch(1)
        # v3.5.2：hint 文案**不能长**——它是最小宽度的隐形推手（实测：
        # 加长到 80 字后 sizeHint 宽从 1100 涨到 1506，把整个窗口撑宽，
        # 连带 clip_timeline_test 的 grab 宽度断言红）。快捷键明细放
        # tooltip，标题行只留最要紧的几个。
        self.hint = CaptionLabel(
            "拖播放头 ｜ 拖 clip 移动 ｜ Ctrl+滚轮缩放 ｜ ←→ 帧 ｜ Del 删除",
            head)
        self.hint.setStyleSheet("color:%s;" % C_TEXT.name())
        self.hint.setToolTip(
            "拖播放头 scrub；拖 clip 移动；拖两端 trim；Ctrl+滚轮缩放；\n"
            "无 Ctrl 滚轮 / 底部滑块 = 横向滚动（内容超视口时出现）；\n"
            "← → = 前后 1 帧；Shift+← → = 前后 1 秒；Home / End = 首尾；\n"
            "Del / Backspace = 删除选中 clip；空格 = 播放/暂停。")
        hl.addWidget(self.hint)

        lay.addWidget(head)

        # ---- 自绘时间区 ----
        self.area = _ClipArea(self)
        lay.addWidget(self.area, 1)

        # ---- v3.5.2：水平滚动条（滑块）----
        # 之前注释写"横向滚动由外层负责"，但工作台里时间轴是直接放在
        # QSplitter 上的，**根本没有外层滚动容器** —— 内容超过视口时
        # 既没有滑块可拖，无 Ctrl 滚轮也滚不动（实测 bug）。
        # 现在面板自带一条水平滚动条：内容宽超过视口宽时出现，
        # 值域 0..(content_width - viewport)，单位像素。
        from PySide6.QtWidgets import QStyle, QStyleFactory
        self.hbar = QScrollBar(Qt.Horizontal, self)
        self.hbar.setStyle(QStyleFactory.create("Fusion"))
        self.hbar.setFixedHeight(HSCROLL_H)
        self.hbar.setRange(0, 0)            # 内容不足时整条隐藏/禁用
        self.hbar.valueChanged.connect(self._on_hscroll)
        lay.addWidget(self.hbar)
        self._hbar_busy = False             # 防递归（程序设置时同步回来）

        head.setFixedHeight(HEAD_H)
        self._update_height()
        # v3.2.1：垂直 Preferred（旧 Fixed 会让 QSplitter 拒绝把空间给时间轴，
        # 上下把手完全拖不动）；理想高度由 sizeHint 提供。
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._sync_buttons()
        _PANELS.append(self)
        self._apply_theme()

    def _apply_theme(self):
        """重刷跟随主题的 QSS（切主题时由模块级 _refresh_colors 调用）。"""
        try:
            self._head.setStyleSheet(
                "QWidget#clipTlHead{background:%s;}" % C_HEAD_BG.name())
            self.hint.setStyleSheet("color:%s;" % C_TEXT.name())
        except Exception:
            pass
        try:
            self.update()
        except Exception:
            pass

    # ================= 数据 / 状态 =================
    def set_project(self, proj):
        """存引用不拷贝；重置选择，保留播放头时刻（越界由 set_time 钳）。"""
        if isinstance(proj, dict) and proj.get("type") == VM.PROJECT_TYPE:
            self.proj = proj
        self._sels = []
        self._sel = None
        self.view_t0 = 0.0
        self._sync_buttons()
        self.refresh()

    def set_act(self, aid):
        """F2-L5（v3.5）：切当前幕（多幕工程的幕游标）。

        时间轴只显示**当前幕**的轨道 —— 一幕一条独立时间轴（设计 §2.2）。
        单幕工程（v1）传什么都无影响（act_view 投影回顶层 tracks）。"""
        self._aid = aid
        self._sels = []
        self._sel = None
        self.refresh()

    def _tracks(self):
        """当前幕的轨道列表（v1 = 顶层 tracks，v2 = act_view 投影）。

        **必须走 act_view**：v2 多幕工程顶层没有 tracks，直接
        `proj.get("tracks")` 会拿到空列表 -> 时间轴整个空白。
        act_view 对 v1 返回顶层**原引用**（旧行为全等），对 v2 返回
        该幕的真实 list（写入即生效）。"""
        try:
            return VM.act_view(self.proj, getattr(self, "_aid", None))["tracks"]
        except Exception:
            t = self.proj.get("tracks")
            return t if isinstance(t, list) else []

    def refresh(self):
        self._update_height()
        self._sync_hbar()           # v3.5.2：内容/视口变化后重算滑块
        self._sync_buttons()
        self.area.update()

    def set_time(self, t):
        self._t = max(0.0, float(t))
        if self.follow_playhead:
            self.maybe_scroll_to(self._t)
        self.area.update()          # 只 update，量小

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._sync_hbar()           # 视口宽变了，滑块范围/可见性要重算

    def current_time(self):
        return float(self._t)

    def selected_clip_id(self):
        return self._sel

    # ================= 缩放 / 坐标 =================
    def set_pixels_per_second(self, v):
        v = _in(float(v), PPS_MIN, PPS_MAX)
        if abs(v - self.pps) < 1e-9:
            return
        self.pps = v
        self.lbl_pps.setText("%.0f px/s" % v)
        self.scale_changed.emit(v)
        self._sync_hbar()           # 缩放后内容宽变了，滑块跟着重算
        self.area.update()

    def set_ui_scale(self, s):
        self.ui_scale = _in(float(s), 0.5, 2.0)
        self._update_height()
        self.area.update()

    def scaled_ruler_h(self):
        return max(16, int(round(RULER_H * self.ui_scale)))

    def row_h(self, track):
        h = self._row_h_default(track)
        # v3.2.1：面板可用高度不足时按比例压缩行高（下限 14px），
        # 让 splitter 拖小高度后内容自适应，而不是钉死把手。
        tracks = self._tracks()
        if len(tracks) > 1:
            # v3.5.2：可用高度扣掉水平滚动条（12px），别把它挤进行里
            avail = (self.height() - HEAD_H - self.scaled_ruler_h()
                     - HSCROLL_H - 4)
            total = sum(self._row_h_default(t) for t in tracks)
            if avail > 0 and total > avail:
                h = max(14, int(round(h * avail / total)))
        return h

    def _row_h_default(self, track):
        base = ROW_VIDEO if track.get("kind") == "video" else ROW_AUDIO
        return max(14, int(round(base * self.ui_scale)))

    def edge_grab_px(self):
        return max(4.0, EDGE_GRAB * self.ui_scale)

    def row_geometry(self):
        """[(y0, h, track)] —— y 为 area 坐标（刻度尺下方按序铺行）。"""
        y = self.scaled_ruler_h()
        out = []
        for tr in self._tracks():
            h = self.row_h(tr)
            out.append((y, h, tr))
            y += h
        return out

    def rows_bottom(self):
        g = self.row_geometry()
        return g[-1][0] + g[-1][1] if g else self.scaled_ruler_h()

    def ideal_height(self):
        """全部行按默认（未压缩）行高铺开的理想高度——只作 sizeHint。"""
        return (HEAD_H + self.scaled_ruler_h() + HSCROLL_H
                + sum(self._row_h_default(t)
                      for t in self._tracks()) + 4)

    def sizeHint(self):
        return QSize(600, self.ideal_height())

    def _update_height(self):
        # v3.2.1：高度交给 splitter/用户拖。minimumHeight 只保证
        # 「工具条+刻度尺+滚动条+一行(14px)」可读；不再强制 resize 回
        # 内容高度（旧逻辑把 splitter 把手钉死，时间轴完全拖不动）。
        self.setMinimumHeight(HEAD_H + self.scaled_ruler_h()
                              + HSCROLL_H + 14 + 4)

    def t_to_x(self, t):
        return NAME_W + (float(t) - self.view_t0) * self.pps

    def x_to_t(self, x):
        return (float(x) - NAME_W) / self.pps + self.view_t0

    def content_width(self):
        """外层放滚动容器时建议的面板宽。"""
        return int(NAME_W + (VM.project_duration(
            self.proj, getattr(self, "_aid", None)) + 2.0) * self.pps)

    def zoom_range_seconds(self):
        """当前视口能容纳的秒数。"""
        return max(0.5, (max(1, self.area.width()) - NAME_W) / self.pps)

    def scroll_host(self):
        """兼容保留：横向滚动现在由面板自带的 hbar 负责。"""
        p = self.parentWidget()
        while p is not None:
            if isinstance(p, QAbstractScrollArea):
                return p
            p = p.parentWidget()
        return None

    # ================= v3.5.2：水平滚动条（滑块） =================
    def _viewport_w(self):
        """视口宽 = 面板宽。未布局（宽度 0/未 show）时退回 sizeHint 宽度，
        否则 hbar 的 span 恒为 0（脱机测试/刚构造时滚不动）。"""
        w = self.width()
        if w < NAME_W + 40:
            w = max(int(self.sizeHint().width()), NAME_W + 600)
        return w

    def _sync_hbar(self):
        """把 view_t0 / 内容宽同步到滚动条（程序设置，不触发回环）。"""
        bar = getattr(self, "hbar", None)
        if bar is None:
            return
        cw = self.content_width()
        vw = self._viewport_w()
        span = max(0, cw - vw)
        self._hbar_busy = True
        try:
            bar.setRange(0, span)
            bar.setPageStep(max(1, vw * 3 // 4))
            bar.setSingleStep(max(1, int(self.pps)))     # 一秒一格
            bar.setValue(int(round(self.view_t0 * self.pps)))
            bar.setVisible(span > 0)
        finally:
            self._hbar_busy = False

    def _on_hscroll(self, v):
        """拖滑块 -> view_t0 跟着走。"""
        if getattr(self, "_hbar_busy", False):
            return
        self.view_t0 = max(0.0, v / max(1e-9, self.pps))
        self.area.update()

    def _hscroll_px(self, dx):
        """滚轮/外部的像素级横向滚动（dx>0 右移）。带值域钳位。"""
        bar = getattr(self, "hbar", None)
        if bar is None:
            return
        bar.setValue(int(_in(bar.value() + int(dx),
                             bar.minimum(), bar.maximum())))

    def maybe_scroll_to(self, t):
        """把时刻 t 滚进可视区（follow 模式下 set_time 自动调）。

        v3.5.2：走自带 hbar。注意**先算目标 view_t0 再套到滑块**，
        不要反过来用 bar.value() 反推 —— 未布局时 width 为 0，
        hbar 的 max 会被算成 0，反推会把 view_t0 截回原值（滚不动）。"""
        t = max(0.0, float(t))
        bar = getattr(self, "hbar", None)
        if bar is not None:
            vw = self._viewport_w()
            # 可用内容宽 = 面板宽 - 左侧轨道头列（NAME_W）。忘了减它会让
            # 播放头停在视口右侧外（实测：x=1210 > 宽 1100）。
            lane = max(1.0, vw - NAME_W)
            x = self.t_to_x(t)                 # 屏坐标（已含 view_t0）
            new_t0 = self.view_t0
            if x > vw - 40:
                new_t0 = max(0.0, t - (lane - 40) / self.pps)
            elif x < NAME_W + 40:
                new_t0 = max(0.0, t - 40 / self.pps)
            # 不越内容末尾（避免滚到空白区）
            max_t0 = max(0.0, (self.content_width() - vw) / self.pps)
            new_t0 = min(new_t0, max_t0)
            if abs(new_t0 - self.view_t0) < 1e-9:
                return
            self.view_t0 = new_t0
            self._sync_hbar()                  # 滑块跟上（含值域重算）
            self.area.update()
            return
        # 兜底：无 hbar 的旧形态（外部滚动宿主）
        host = self.scroll_host()
        win = self.zoom_range_seconds()
        edge = win * 0.1
        if host is not None and host.width() > 0:
            hb = host.horizontalScrollBar()
            if hb is not None:
                x = self.t_to_x(t)
                if x > hb.value() + host.viewport().width() - 40:
                    hb.setValue(int(x - host.viewport().width() + 40))
                elif x < hb.value() + 40:
                    hb.setValue(max(0, int(x - 40)))
                return
        if t > self.view_t0 + win - edge:
            self.view_t0 = max(0.0, t - win + edge)
        elif t < self.view_t0 + edge:
            self.view_t0 = max(0.0, t - edge)
        self.area.update()

    def clip_at_point(self, pt):
        """命中测试（面板坐标 QPoint -> clip dict | None），供外层/测试。"""
        return self.area.hit_clip(float(pt.x()),
                                  float(pt.y()) - self._head.height())

    # ================= 选择 =================
    def _set_sel(self, cid, emit=True):
        """单选入口（点 clip / 外部调用）。多选集合跟着重置为这一个。"""
        self._sels = [] if cid is None else [cid]
        self._apply_sel(cid, emit)

    def _apply_sel(self, cid, emit=True):
        if self._sel == cid and list(self._sels) == ([] if cid is None else [cid]):
            return
        self._sel = cid
        self._sync_buttons()
        if emit:
            self.selection_changed.emit(cid)
        self.area.update()

    def set_selection(self, ids, primary=None, emit=True):
        """批量设置多选集合。primary 缺省取最后一个（= 主选中）。"""
        seen, out = set(), []
        for i in (ids or ()):
            if i is not None and i not in seen:
                seen.add(i)
                out.append(i)
        prim = primary if primary is not None else (out[-1] if out else None)
        # 框选每挪一像素都会回调这里；集合没变就不发信号（否则外层
        # 属性面板每秒被刷几十次，还会往 selection_changed 灌重复事件）
        if out == self._sels and prim == self._sel:
            return
        self._sels = out
        self._sel = prim
        self._sync_buttons()
        if emit:
            self.selection_changed.emit(self._sel)
        self.area.update()

    def selected_clip_ids(self):
        """多选集合（顺序稳定）。单选调用点继续用 selected_clip_id()。"""
        return list(self._sels)

    def _toggle_sel(self, cid):
        """Ctrl+点：加选/去选。"""
        if cid in self._sels:
            self._sels = [i for i in self._sels if i != cid]
            prim = self._sels[-1] if self._sels else None
        else:
            self._sels = self._sels + [cid]
            prim = cid
        self.set_selection(self._sels, prim)

    def _select_range(self, cid):
        """Shift+点：连选。

        **同轨**时取时间区间内该轨全部 clip（剪映/PR 口径，最符合直觉）；
        主选中与被点 clip **不同轨**时只收这两个端点——跨轨按时间扫会
        莫名其妙捞进别的轨，用户很难预期。"""
        tr_c, c_t = VM.find_clip(self.proj, cid)
        if not self._sel or cid is None or cid == self._sel or c_t is None:
            self._set_sel(cid)
            return
        a = self._clip_start(self._sel)
        b = float(c_t["start"])
        lo, hi = (a, b) if a <= b else (b, a)
        tr_p, _c = VM.find_clip(self.proj, self._sel)
        if tr_p is not None and tr_p.get("id") == tr_c.get("id"):
            ids = [c["id"] for c in tr_c["clips"]
                   if lo - 1e-9 <= float(c["start"]) <= hi + 1e-9]
        else:
            ids = [self._sel, cid]
        if not ids:
            self._set_sel(cid)
            return
        self.set_selection(ids, cid)

    def _clip_start(self, cid):
        tr, c = VM.find_clip(self.proj, cid)
        return float(c["start"]) if c else 0.0

    def _sync_buttons(self):
        has = bool(self._sels) or self._sel is not None
        self.btn_split.setEnabled(has)
        self.btn_delete.setEnabled(has)

    # ================= 模型操作（全部走 video_model） =================
    def _selected_clip(self):
        if not self._sel:
            return None
        tr, c = VM.find_clip(self.proj, self._sel)
        return c

    def split_selected_at_playhead(self):
        """[✂] / 外部调用：在播放头处切分**所有**选中 clip。

        多选时逐个切（框选一批再切一刀是常见剪映用法）；任一成功只发
        **一次** edit_committed（外层据此 push 一条历史，撤销才能一步回到
        切分前 —— 每个 clip 发一次会把一次操作拆成 N 条历史）。
        失败（太短/锁定/越界）静默跳过，与单选语义一致。"""
        ids = self._sels or ([self._sel] if self._sel else [])
        if not ids:
            return False
        n = 0
        for cid in ids:
            ok, _msg, _rid = VM.split_clip(self.proj, cid, self._t)
            if ok:
                n += 1
        if n:
            self.edit_committed.emit()
            self.refresh()
        return bool(n)

    def remove_selected(self):
        """[🗑] / Delete：删除选中 clip（多选成组删），失败（锁定等）静默。"""
        ids = self._sels or ([self._sel] if self._sel else [])
        if not ids:
            return False
        ok, _msg = VM.remove_clips(self.proj, ids)
        if ok:
            self._set_sel(None)
            self.edit_committed.emit()
            self.refresh()
        return ok

    def select_all(self):
        """Ctrl+A：选中当前幕全部可见轨的 clip（隐藏轨排除）。"""
        ids = [c["id"] for tr in self._tracks() if not tr.get("hidden")
               for c in tr["clips"]]
        if not ids:
            return False
        self.set_selection(ids, ids[-1] if ids else None)
        return True

    def _on_follow_toggled(self, checked):
        self.follow_playhead = bool(checked)
        if self.follow_playhead:
            self.maybe_scroll_to(self._t)

    def snap_guide_x(self):
        """当前吸附辅助线 x（area 坐标），无则 None。"""
        d = getattr(self.area, "_drag", None)
        return d.get("snap_x") if d else None

    # ================= 键盘 =================
    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.remove_selected()
            ev.accept()
            return
        if ev.key() == Qt.Key_A and ev.modifiers() & Qt.ControlModifier:
            self.select_all()           # Ctrl+A：全选当前幕可见 clip
            ev.accept()
            return
        super().keyPressEvent(ev)


class _ClipArea(QWidget):
    """自绘时间区：刻度尺 + 轨道行（头列 + lane）+ clip + 播放头 + 辅助线。"""

    EDGE_MOVE = "move"
    EDGE_START = "trim_start"
    EDGE_END = "trim_end"

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self._drag = None                # 拖拽状态 dict
        self._scrub = False
        # 框选状态：{"x0","y0","x1","y1","active","add"}。
        # **按下时不 active**：空白处按下后若没移动就松手，按老语义
        # 走「取消选中 + scrub 播放头」（测试 3d/11f/17a 锁的就是这个）；
        # 移动超过阈值才升级为框选。
        self._marquee = None
        # v3.4.7：头列眼睛/锁按钮 hover 反馈（"eye"/"lock"，None=无）
        self._hover_icon = None
        self._hover_row = None           # (y0, h) hover 所在行
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(60)

    # ---------------- 几何 ----------------
    def t_to_x(self, t):
        return self.panel.t_to_x(t)

    def x_to_t(self, x):
        return self.panel.x_to_t(x)

    def _row_at(self, y):
        """(track, y0, h) 或 None（刻度尺/行以下）。"""
        if y < self.panel.scaled_ruler_h():
            return None
        for y0, h, tr in self.panel.row_geometry():
            if y0 <= y < y0 + h:
                return tr, y0, h
        return None

    def _icon_rects(self, y0, h):
        """头列 👁 / 🔒 命中矩形。"""
        x = NAME_W - 2 * EYE_W
        return (QRectF(x, y0, EYE_W, h), QRectF(x + EYE_W, y0, EYE_W, h))

    def _hover_at(self, x, y):
        """悬停点落在哪个头列按钮上 -> ("eye"/"lock", (y0,h))；无则 (None,None)。"""
        if x >= NAME_W:
            return None, None
        row = self._row_at(y)
        if row is None:
            return None, None
        tr, y0, h = row
        r_eye, r_lock = self._icon_rects(y0, h)
        if r_eye.contains(QPointF(x, y)):
            return "eye", (y0, h)
        if r_lock.contains(QPointF(x, y)):
            return "lock", (y0, h)
        return None, None

    def leaveEvent(self, ev):
        self._hover_icon = None
        self._hover_row = None
        self.update()
        super().leaveEvent(ev)

    def _hit_clip(self, x, y):
        """命中 clip（不筛 hidden/locked）-> (track, clip, zone) 或 None。"""
        row = self._row_at(y)
        if row is None:
            return None
        tr, y0, h = row
        if x < NAME_W:
            return None
        for c in tr["clips"]:
            cx0 = self.t_to_x(c["start"])
            cx1 = cx0 + c["dur"] * self.panel.pps
            if cx0 - 1.0 <= x <= cx1 + 1.0:
                eg = self.panel.edge_grab_px()
                if x <= cx0 + eg:
                    zone = self.EDGE_START
                elif x >= cx1 - eg:
                    zone = self.EDGE_END
                else:
                    zone = self.EDGE_MOVE
                return tr, c, zone
        return None

    def hit_clip(self, x, y):
        """供外层/测试：只返回 clip dict 或 None。"""
        h = self._hit_clip(x, y)
        return h[1] if h else None

    def _preview_geom(self, c):
        """拖拽中：返回该 clip 的预览 (start, dur)；否则返回实际数据。"""
        d = self._drag
        if d is None or not d.get("moved"):
            return float(c["start"]), float(c["dur"])
        # 成组拖动：组内成员整体平移同一个 delta（dur 不变）
        if d.get("group") and c["id"] in d["group"]:
            s0, dur0 = d["group"][c["id"]]
            return s0 + d["delta"], dur0
        if d.get("pv") is None or d["id"] != c["id"]:
            return float(c["start"]), float(c["dur"])
        if d["kind"] == self.EDGE_MOVE:
            return d["pv"], d["dur0"]
        if d["kind"] == self.EDGE_START:
            end = d["start0"] + d["dur0"]
            return d["pv"], max(0.05, end - d["pv"])
        return d["start0"], max(0.05, d["pv"] - d["start0"])

    # ---------------- 事件 ----------------
    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        if self._drag is not None or self._scrub:
            self._finish_drag()
        self._marquee = None            # 新按下作废上一发挂起的空白点击
        x, y = ev.position().x(), ev.position().y()
        ruler_h = self.panel.scaled_ruler_h()

        # 刻度尺：scrub
        if y < ruler_h:
            if x >= NAME_W:
                self._begin_scrub(x)
            return

        # 头列：👁 / 🔒
        if x < NAME_W:
            row = self._row_at(y)
            if row is None:
                return
            tr, y0, h = row
            r_eye, r_lock = self._icon_rects(y0, h)
            if r_eye.contains(QPointF(x, y)):
                tr["hidden"] = not tr.get("hidden", False)
                self.panel.track_flags_changed.emit(tr["id"])
                self.panel.refresh()
            elif r_lock.contains(QPointF(x, y)):
                tr["locked"] = not tr.get("locked", False)
                self.panel.track_flags_changed.emit(tr["id"])
                self.panel.refresh()
            return

        hit = self._hit_clip(x, y)
        if hit is None:
            # 空白（含行以下）：**先挂起**，不立刻取消/scrub。
            # 没移动就松手 -> 老语义（取消选中 + scrub 播放头）；
            # 移动超阈值 -> 升级为框选。隐藏轨整行完全不可交互。
            row = self._row_at(y)
            if row is not None and row[0].get("hidden"):
                return
            self._marquee = {"x0": x, "y0": y, "x1": x, "y1": y,
                             "active": False,
                             "add": bool(ev.modifiers() & Qt.ControlModifier)}
            return
        tr, c, zone = hit
        if tr.get("hidden"):
            return                       # 隐藏轨不可交互
        mods = ev.modifiers()
        if mods & Qt.ControlModifier:
            self.panel._toggle_sel(c["id"])   # 加选/去选，不进拖拽
            return
        if mods & Qt.ShiftModifier:
            self.panel._select_range(c["id"])
            return
        if c["id"] in self.panel._sels and len(self.panel._sels) > 1:
            # 组内点击（未按修饰键）：保持整组选中可成组拖，但把被点的
            # 提为主选中 -> 属性面板跟着显示它（剪映/PR 一致）
            self.panel.set_selection(self.panel._sels, c["id"])
        else:
            self.panel._set_sel(c["id"])
        if tr.get("locked"):
            return                       # 锁定轨：可选中，不可拖/trim
        grp = {}
        if zone == self.EDGE_MOVE and len(self.panel._sels) > 1 \
                and c["id"] in self.panel._sels:
            # 成组拖动：只收**可动**成员（锁定轨/隐藏轨的留在原地）
            for cid in self.panel._sels:
                t2, c2 = VM.find_clip(self.panel.proj, cid)
                if c2 is None or t2.get("locked") or t2.get("hidden"):
                    continue
                grp[cid] = (float(c2["start"]), float(c2["dur"]))
        self._drag = {
            "kind": zone, "id": c["id"], "c": c,
            "x0px": x, "t0": self.x_to_t(x),
            "start0": float(c["start"]), "dur0": float(c["dur"]),
            "pv": None, "snap_x": None, "moved": False,
            "group": grp,
        }

    def _begin_scrub(self, x):
        self._scrub = True
        self.panel.seeked.emit(self._scrub_t(x))
        self.panel.area.update()

    def _marquee_select(self, mods):
        """框选命中：矩形与 clip 矩形**相交**即选中（剪映/PR 口径，
        比"完全包含"好用——长 clip 拖一小段也能捞进来）。
        Ctrl 按住 = 并集（在已有选择上追加），否则整盘替换。"""
        mq = self._marquee
        rx0, rx1 = sorted((mq["x0"], mq["x1"]))
        ry0, ry1 = sorted((mq["y0"], mq["y1"]))
        hit_ids = []
        for y0, rh, tr in self.panel.row_geometry():
            if tr.get("hidden"):
                continue
            if ry1 < y0 or ry0 > y0 + rh:      # 矩形没盖到这一行
                continue
            for c in tr["clips"]:
                cx0 = self.t_to_x(c["start"])
                cx1 = cx0 + float(c["dur"]) * self.panel.pps
                if cx1 >= rx0 and cx0 <= rx1:
                    hit_ids.append(c["id"])
        if mods & Qt.ControlModifier:
            merged = list(self.panel._sels)
            for i in hit_ids:
                if i not in merged:
                    merged.append(i)
            self.panel.set_selection(merged,
                                     hit_ids[-1] if hit_ids else
                                     (merged[-1] if merged else None))
        else:
            self.panel.set_selection(hit_ids,
                                     hit_ids[-1] if hit_ids else None)

    def _edge_autoscroll(self, x):
        """v3.5.2：拖拽/scrub 中光标贴左右缘 -> 内容自动横向滚。

        返回 True 表示滚了（调用方随后基于新 view_t0 重算时刻）。"""
        w = self.width()
        edge = 24
        if x >= w - edge:
            self.panel._hscroll_px(12)
            return True
        if x <= NAME_W + edge:
            self.panel._hscroll_px(-12)
            return True
        return False

    def _scrub_t(self, x):
        return round(max(0.0, self.x_to_t(x)), 2)   # 吸附到 0.01

    def mouseMoveEvent(self, ev):
        x, y = ev.position().x(), ev.position().y()
        mq = self._marquee
        if mq is not None and self._drag is None and not self._scrub:
            if not mq["active"] and (abs(x - mq["x0"]) > DRAG_THRESHOLD
                                     or abs(y - mq["y0"]) > DRAG_THRESHOLD):
                mq["active"] = True      # 升级为框选（此前的 scrub 挂起作废）
            if mq["active"]:
                self._edge_autoscroll(x)
                mq["x1"], mq["y1"] = x, y
                self._marquee_select(ev.modifiers())
                self.update()
            return
        d = self._drag
        if d is not None:
            if not d["moved"] and abs(x - d["x0px"]) > DRAG_THRESHOLD:
                d["moved"] = True
            if d["moved"]:
                # v3.5.2：贴边自动横滚（光标在左/右缘时内容跟着走，
                # 否则拖超出视口的 clip 根本看不见 -> 必须有滑块+自动滚）
                self._edge_autoscroll(x)
                self._update_preview(d, x)
            return
        if self._scrub:
            self._edge_autoscroll(x)
            self.panel.seeked.emit(self._scrub_t(x))
            self.update()
            return
        # v3.4.7：头列按钮 hover 反馈（眼睛/锁高亮）
        key, row = self._hover_at(x, y)
        if key != self._hover_icon or row != self._hover_row:
            self._hover_icon = key
            self._hover_row = row
            self.update()
        # 悬停光标
        hit = self._hit_clip(x, y)
        if hit is not None and not hit[0].get("hidden") and not hit[0].get("locked") \
                and hit[2] in (self.EDGE_START, self.EDGE_END):
            self.setCursor(Qt.SizeHorCursor)
        elif hit is not None and not hit[0].get("hidden"):
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()

    def _update_preview(self, d, x):
        p = self.panel
        dt = p.x_to_t(x) - d["t0"]
        if d.get("group"):
            # 成组移动：吸附整组的**公共位移 delta**，而不是逐条各自吸附
            # （逐条吸会把组内相对间距打散，用户摆好的错位就没了）。
            # 候选吸点 = 组内每个成员的左/右边界，delta 取使其中某个边界
            # 落到组外吸点上的最小调整量。
            ids = set(d["group"])
            pts = VM.snap_points_multi(p.proj, exclude_ids=ids)
            raw = dt
            best, bd = raw, SNAP_EPS
            for cid, (s0, du0) in d["group"].items():
                for edge in (s0 + raw, s0 + raw + du0):
                    for q in pts:
                        cand = raw + (q - edge)
                        if abs(cand - raw) <= bd:
                            best, bd = cand, abs(cand - raw)
            best = round(best, 3)
            d["delta"] = best
            d["pv"] = round(max(0.0, d["start0"] + best), 3)
            d["snap_x"] = None
            if bd <= SNAP_EPS:
                for cid, (s0, du0) in d["group"].items():
                    for q in pts:
                        if abs((s0 + best) - q) <= 1e-6:
                            d["snap_x"] = p.t_to_x(q)
                            break
                    if d["snap_x"] is not None:
                        break
            self.update()
            return
        pts = VM.snap_points(p.proj, exclude_id=d["id"])
        if d["kind"] == self.EDGE_MOVE:
            raw = d["start0"] + dt
            a = VM.snap_time(pts, raw)
            b = VM.snap_time(pts, raw + d["dur0"]) - d["dur0"]
            da, db = abs(a - raw), abs(b - raw)
            if da <= db:
                d["pv"], d["snap_x"] = a, (p.t_to_x(a) if da <= SNAP_EPS else None)
            else:
                d["pv"], d["snap_x"] = b, (p.t_to_x(b + d["dur0"]) if db <= SNAP_EPS else None)
        elif d["kind"] == self.EDGE_START:
            raw = d["start0"] + dt
            a = VM.snap_time(pts, raw)
            d["pv"] = a
            d["snap_x"] = p.t_to_x(a) if abs(a - raw) <= SNAP_EPS else None
        else:  # trim_end
            end0 = d["start0"] + d["dur0"]
            raw = end0 + dt
            a = VM.snap_time(pts, raw)
            d["pv"] = a
            d["snap_x"] = p.t_to_x(a) if abs(a - raw) <= SNAP_EPS else None
        d["pv"] = round(max(0.0, d["pv"]), 3)
        self.update()

    def mouseReleaseEvent(self, ev):
        mq, self._marquee = self._marquee, None
        if mq is not None and self._drag is None:
            if mq["active"]:
                self.update()            # 框选结果已在 move 中实时生效
                return
            # 没移动 = 空白处点击：走老语义（取消选中 + scrub 播放头）
            x = mq["x0"]
            self.panel._set_sel(None)
            self._begin_scrub(x)
        if self._scrub:
            self._scrub = False
            self.panel.playhead_drag_stopped.emit()
        self._finish_drag()

    def _finish_drag(self):
        d, self._drag = self._drag, None
        if d is None or not d.get("moved"):
            self.update()
            return
        p = self.panel
        if d.get("group"):
            # 成组移动：任一成员撞组外 -> 整组回弹（move_clips 原子）
            delta = d.get("delta")
            if delta is not None and abs(delta) > 1e-9:
                ok, _msg = VM.move_clips(p.proj, list(d["group"]), delta)
                if ok:
                    p.edit_committed.emit()
            p.refresh()
            return
        if not d.get("moved") or d.get("pv") is None:
            self.update()
            return
        if d["kind"] == self.EDGE_MOVE:
            if abs(d["pv"] - d["start0"]) > 1e-9:
                ok, _msg = VM.move_clip(p.proj, d["id"], d["pv"])
                if ok:
                    p.edit_committed.emit()
        else:
            side = "start" if d["kind"] == self.EDGE_START else "end"
            cur = d["start0"] if side == "start" else d["start0"] + d["dur0"]
            if abs(d["pv"] - cur) > 1e-9:
                ok, _msg = VM.trim_clip(p.proj, d["id"], side, d["pv"])
                if ok:
                    p.edit_committed.emit()
        # 失败：模型未改数据 -> 静默回弹（清掉预览重画即可）
        p.refresh()

    def mouseDoubleClickEvent(self, ev):
        x, y = ev.position().x(), ev.position().y()
        hit = self._hit_clip(x, y)
        if hit is not None and not hit[0].get("hidden"):
            self.panel.clip_activated.emit(hit[1]["id"])

    def wheelEvent(self, ev):
        """Ctrl+滚轮：以鼠标时刻为锚缩放 pps（0.5~400）。
        v3.5.2：无 Ctrl = 横向滚动（内容超视口时；以前这句被"交给外层
        滚动容器"注释接住，但外层根本没有滚动容器 -> 白滚）。"""
        if ev.modifiers() & Qt.ControlModifier:
            dy = ev.angleDelta().y()
            if dy == 0:
                return
            x = ev.position().x()
            if x < NAME_W:
                x = float(NAME_W)
            t_anchor = self.panel.x_to_t(x)
            f = WHEEL_UP_FACTOR if dy > 0 else WHEEL_DOWN_FACTOR
            new_pps = _in(self.panel.pps * f, PPS_MIN, PPS_MAX)
            if abs(new_pps - self.panel.pps) < 1e-9:
                ev.accept()
                return
            self.panel.pps = new_pps
            self.panel.view_t0 = max(0.0, t_anchor - (x - NAME_W) / new_pps)
            self.panel.lbl_pps.setText("%.0f px/s" % new_pps)
            self.panel.scale_changed.emit(new_pps)
            self.panel.refresh()
            ev.accept()
            return
        # v3.5.2：横向滚动（Shift 竖滚轮 / 触摸板横滚 / 普通竖滚轮都算）
        dy = ev.angleDelta().y()
        dx = ev.angleDelta().x()
        px = int((dx if abs(dx) >= abs(dy) else dy)
                 * WHEEL_SCROLL_FACTOR * -1)
        self.panel._hscroll_px(px)
        ev.accept()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.panel.remove_selected()
            ev.accept()
            return
        super().keyPressEvent(ev)

    # ---------------- 绘制 ----------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pal = self.panel
        s = pal.ui_scale
        w, h = self.width(), self.height()
        ruler_h = pal.scaled_ruler_h()

        p.fillRect(QRectF(0, 0, w, h), C_BG)

        t_vis0 = pal.view_t0
        t_vis1 = pal.x_to_t(max(w, NAME_W + 1))

        # ---- 轨道行：底色 + 头列 ----
        rows = pal.row_geometry()
        p.fillRect(QRectF(0, ruler_h, NAME_W, h - ruler_h), C_LANE)
        for i, (y0, rh, tr) in enumerate(rows):
            if y0 > h:
                break
            p.fillRect(QRectF(NAME_W, y0, w - NAME_W, rh),
                       C_ROW_A if i % 2 == 0 else C_ROW_B)

        # ---- 刻度尺 + 竖向网格 ----
        p.fillRect(QRectF(0, 0, w, ruler_h), C_HEAD_BG)
        self._draw_ruler(p, w, ruler_h, t_vis0, t_vis1, s)

        # ---- clip ----
        for y0, rh, tr in rows:
            self._draw_row_clips(p, tr, y0, rh, s)

        # ---- 行分隔 / 头列分隔 ----
        p.setPen(QPen(C_GRID, 1))
        for y0, rh, tr in rows:
            p.drawLine(QPointF(0, y0 + rh - 0.5), QPointF(w, y0 + rh - 0.5))
        p.drawLine(QPointF(NAME_W - 0.5, ruler_h), QPointF(NAME_W - 0.5, h))

        # ---- 吸附辅助线 ----
        d = self._drag
        if d is not None and d.get("moved") and d.get("snap_x") is not None:
            p.setPen(QPen(C_SNAP, 1))
            p.drawLine(QPointF(d["snap_x"], ruler_h), QPointF(d["snap_x"], h))

        # ---- 框选矩形（画在 clip 之上、播放头之下）----
        mq = self._marquee
        if mq is not None and mq.get("active"):
            rx0, rx1 = sorted((mq["x0"], mq["x1"]))
            ry0, ry1 = sorted((mq["y0"], mq["y1"]))
            p.setPen(QPen(C_MARQUEE, 1))
            p.setBrush(QBrush(C_MARQUEE_FILL))
            p.drawRect(QRectF(rx0, ry0, rx1 - rx0, ry1 - ry0))

        # ---- 播放头 ----
        px = pal.t_to_x(pal._t)
        if 0 <= px <= w:
            p.setPen(QPen(C_PLAYHEAD, max(1.5, 1.8 * s)))
            p.drawLine(QPointF(px, 0), QPointF(px, h))
            tri = max(5.0, 5.5 * s)
            path = QPainterPath()
            path.moveTo(px - tri, 0)
            path.lineTo(px + tri, 0)
            path.lineTo(px, tri * 1.4)
            path.closeSubpath()
            p.setBrush(QBrush(C_PLAYHEAD))
            p.setPen(Qt.NoPen)
            p.drawPath(path)

        p.end()

    def _draw_ruler(self, p, w, ruler_h, t0, t1, s):
        pal = self.panel
        f = QFont()
        f.setPixelSize(max(9, int(round(11 * s))))
        p.setFont(f)
        fm = p.fontMetrics()
        major = TP._nice_step(max(0.05, 90.0 / pal.pps))
        minor = major / 5.0
        draw_minor = minor * pal.pps >= 6.0

        t = max(0.0, (t0 // minor) * minor)
        while t <= t1 + minor:
            if t < 0:
                t += minor
                continue
            x = pal.t_to_x(t)
            if NAME_W - 1 <= x <= w:
                is_major = abs(t / major - round(t / major)) < 1e-6
                p.setPen(QPen(C_GRID if is_major else C_GRID_SUB, 1))
                if is_major:
                    p.drawLine(QPointF(x, ruler_h - 6), QPointF(x, self.height()))
                    p.setPen(QPen(TP.C_TICK_MAJOR if abs(t - round(t)) < 1e-6 else C_TEXT))
                    txt = _fmt_sec(t)
                    tw = fm.horizontalAdvance(txt)
                    p.drawText(QPointF(min(x + 3, w - tw - 2), ruler_h - 7), txt)
                elif draw_minor:
                    p.drawLine(QPointF(x, ruler_h - 4), QPointF(x, ruler_h))
            if t > t1:
                break
            t += minor if draw_minor else major
        p.setPen(QPen(C_GRID, 1))
        p.drawLine(QPointF(0, ruler_h - 0.5), QPointF(w, ruler_h - 0.5))

    def _draw_row_clips(self, p, tr, y0, rh, s):
        pal = self.panel
        hidden = bool(tr.get("hidden"))
        w = self.width()
        inset = 3.0 * s if rh >= 30 else 1.5 * s
        cy = y0 + inset
        ch = rh - 2 * inset
        name_font = pal.lbl_pps.font()          # CaptionLabel 字号
        fm = pal.lbl_pps.fontMetrics()

        p.save()
        p.setClipRect(QRectF(NAME_W, y0, max(1.0, w - NAME_W), rh))
        for c in tr["clips"]:
            st, du = self._preview_geom(c)
            x0 = pal.t_to_x(st)
            wpx = du * pal.pps
            if x0 + wpx < NAME_W or x0 > w:
                continue
            kind = c.get("source", {}).get("kind")
            col = QColor(_base_color(c))
            if hidden:
                col.setAlpha(_HIDDEN_ALPHA)
            radius = min(4.0 * s, ch / 2.0, max(1.0, wpx / 2.0))
            rect = QRectF(x0, cy, max(2.0, wpx), ch)
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            p.setPen(QPen(C_CLIP_EDGE, 1))
            p.setBrush(QBrush(col))
            p.drawPath(path)
            if pal._sel == c["id"]:
                p.setPen(QPen(C_SEL_EDGE, 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)
            elif c["id"] in pal._sels:
                # 多选（非主选中）：淡一档描边，与单选可区分；框选一批
                # 之后能一眼看出捞到了哪几个
                p.setPen(QPen(C_SEL_EDGE_DIM, 1.2))
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)

            # video/puppet：左上角文件名（画不下降级省略）
            if kind in ("video", "puppet") and not hidden and wpx > 26:
                p.setPen(QPen(C_TEXT_HI))
                p.setFont(name_font)
                label = os.path.basename(str(c.get("source", {}).get("path", "")))
                avail = wpx - 8
                if avail > 6 and label:
                    p.drawText(QRectF(x0 + 4, cy + 1, avail, ch * 0.5),
                               Qt.AlignLeft | Qt.AlignVCenter,
                               fm.elidedText(label, Qt.ElideRight, max(0, int(avail))))

            # audio：波形（无 wave 字段则纯色，上面已画）
            wave = _wave_of(c)
            if kind == "audio" and wave is not None and wpx > 3:
                n = len(wave)
                path2 = QPainterPath()
                mid = cy + ch / 2.0
                for i, v in enumerate(wave):
                    try:
                        fv = _in(float(v), 0.0, 1.0)
                    except (TypeError, ValueError):
                        fv = 0.5
                    vx = x0 + (wpx - 1.0) * (i / (n - 1.0))
                    vy = mid - (fv - 0.5) * (ch - 3.0)
                    if i == 0:
                        path2.moveTo(vx, vy)
                    else:
                        path2.lineTo(vx, vy)
                wc = QColor(C_WAVE)
                if hidden:
                    wc.setAlpha(_HIDDEN_ALPHA)
                p.setPen(QPen(wc, max(1.0, 1.2 * s)))
                p.setBrush(Qt.NoBrush)
                p.drawPath(path2)
        p.restore()

        # ---- 头列：轨名 + 👁 + 🔒 ----
        if tr.get("locked"):
            p.fillRect(QRectF(0, y0, NAME_W, rh), C_LOCKED_ROW)
        f = QFont()
        f.setPixelSize(max(10, int(round(12 * s))))
        p.setFont(f)
        fm2 = p.fontMetrics()
        p.setPen(QPen(C_TEXT_HI))
        label = str(tr.get("name") or tr.get("id") or "")
        p.drawText(QRectF(8, y0, NAME_W - 58, rh), Qt.AlignVCenter | Qt.AlignLeft,
                   fm2.elidedText(label, Qt.ElideRight, NAME_W - 62))
        r_eye, r_lock = self._icon_rects(y0, rh)
        # v3.4.7：emoji（👁🔒）换成矢量自绘——不依赖系统 emoji 字体，
        # 状态对比更清晰；hover 行画淡色圆角底 + 图标提亮。
        hover_key, hover_row = self._hover_icon, self._hover_row
        on_row = (hover_row == (y0, rh))
        # 眼睛 active = 轨道可见（hidden=False）——睁眼亮态；隐藏时暗态+红斜杠
        self._draw_track_icon(p, r_eye, "eye", not hidden,
                              "eye" if on_row else None)
        self._draw_track_icon(p, r_lock, "lock", bool(tr.get("locked")),
                              "lock" if on_row else None)

    def _draw_track_icon(self, p, rect, key, active, hover):
        """轨道头按钮矢量图标。active=该状态生效；hover="eye"/"lock" 时画底。"""
        s = self.panel.ui_scale
        if hover:
            p.fillRect(QRectF(rect.x() + 2, rect.y() + 3,
                              rect.width() - 4, rect.height() - 6),
                       C_ICON_HOVER)
        cx = rect.x() + rect.width() / 2.0
        cy = rect.y() + rect.height() / 2.0
        r = 5.5 * s                      # 图标基准半径
        if key == "eye":
            # 眼形：两条二次贝塞尔构成的杏仁形 + 实心瞳孔；隐藏态降透明
            # 并画一条「闭眼」斜杠，与睁眼对比明显（不依赖 emoji 字体）。
            base = QColor(C_ICON)
            base.setAlpha(235 if active else 120)
            pen = QPen(base, max(1.2, 1.3 * s))
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            w, hh = r * 1.25, r * 0.95
            eye = QPainterPath()
            eye.moveTo(cx - w, cy)
            eye.quadTo(cx, cy - hh * 2.0, cx + w, cy)
            eye.quadTo(cx, cy + hh * 2.0, cx - w, cy)
            p.drawPath(eye)
            if active:
                p.setBrush(QBrush(base))
                p.drawEllipse(QPointF(cx, cy), r * 0.42, r * 0.42)
            else:
                cut = QPen(C_ICON_CUT, max(1.2, 1.3 * s))
                cut.setCapStyle(Qt.RoundCap)
                p.setPen(cut)
                p.drawLine(QPointF(cx - w * 0.85, cy + r * 0.75),
                           QPointF(cx + w * 0.85, cy - r * 0.75))
        else:
            # 锁：上半圆锁梁 + 圆角锁体；锁定态金色，未锁灰且锁体开口
            # （描边不填充），一眼区分。
            body_w = r * 1.5
            body_h = r * 1.15
            body_y = cy - r * 0.15
            col = C_ICON_LOCK if active else C_ICON
            col.setAlpha(235 if active else 130)
            pen = QPen(col, max(1.2, 1.3 * s))
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            shank_w = r * 0.85
            shank = QRectF(cx - shank_w / 2.0, cy - r * 1.35,
                           shank_w, r * 1.35)
            p.drawArc(shank, 0, 180 * 16)
            body = QRectF(cx - body_w / 2.0, body_y, body_w, body_h)
            path = QPainterPath()
            path.addRoundedRect(body, 2.2 * s, 2.2 * s)
            if active:
                p.setBrush(QBrush(C_ICON_LOCK_FILL))
                p.drawPath(path)
                keyhole = QPointF(cx, body_y + body_h * 0.5)
                p.setBrush(QBrush(col))
                p.drawEllipse(keyhole, r * 0.16, r * 0.16)
            else:
                p.drawPath(path)


