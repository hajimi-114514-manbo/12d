# -*- coding: utf-8 -*-
"""clip 多轨时间轴面板（v3.0.0 剪辑工作台第一阶段）。

数据模型唯一事实源是 video_model.py：面板**从不直接改 clip 数据**，
所有编辑一律走 VM.move_clip / trim_clip / split_clip / remove_clip
（吸附查询走 VM.snap_points / snap_time）。模型原地改 + 校验拒绝，
校验失败时数据未被改动，面板静默回弹（清掉预览偏移重画即可）。

布局：
    ┌───────────────────────────────────────────────┐
    │ [✂ 切分] [🗑 删除] [▶ 跟随]  80px/s   ...提示 │  ← 34px 工具条
    ├──────────┬────────────────────────────────────┤
    │ (头列)   │  0s   1s   2s   3s  ← 22px 刻度尺  │
    │ 视频轨1  │ ▓▓clip▓▓   ▓▓clip▓▓    │  44px/行  │
    │   👁 🔒  │  ← 150px 固定轨道头列             │
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
                               QSizePolicy, QAbstractScrollArea)

import video_model as VM
import timeline_panel as TP          # 复用配色 / _nice_step，无循环依赖

# ---------------- 配色（沿用工作台暗色主题） ----------------
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
C_SEL_EDGE = QColor("#f2f6fa")       # 选中描边
C_WAVE = QColor("#bfe3cc")           # 波形折线
C_CLIP_EDGE = QColor("#10151d")      # clip 暗描边

# source.kind -> clip 底色（规格拍板）
CLIP_COLORS = {
    "video": QColor("#3d6dbf"),
    "image": QColor("#c97b3d"),
    "puppet": QColor("#8a5fc0"),
    "audio": QColor("#3fa06b"),
}

# ---------------- 尺寸基准 ----------------
HEAD_H = 34                 # 工具条高度
RULER_H = 22                # 刻度尺高度
NAME_W = 150                # 左侧轨道头列宽（固定）
ROW_VIDEO = 44              # 视频轨行高基准（×ui_scale）
ROW_AUDIO = 26              # 音频轨行高基准（×ui_scale）
EDGE_GRAB = 6               # clip 两端 trim 命中宽度（px，×ui_scale）
EYE_W = 20                  # 头列按钮宽度
PPS_DEFAULT = 80.0
PPS_MIN = 0.5
PPS_MAX = 400.0
WHEEL_UP_FACTOR = 1.25
WHEEL_DOWN_FACTOR = 0.8
DRAG_THRESHOLD = 2          # 超过 2px 才算拖动（区分点选）
SNAP_EPS = VM.SNAP_STEP * 3     # 与 video_model.snap_time 默认 eps 一致

_HIDDEN_ALPHA = 90              # 隐藏轨 clip 透明度（0.35×255）


def _fmt_sec(t):
    """刻度文字：1.50 -> '1.5s'，2.00 -> '2s'。"""
    return ("%.2f" % t).rstrip("0").rstrip(".") + "s"


def _base_color(clip):
    return CLIP_COLORS.get(clip.get("source", {}).get("kind"),
                           QColor("#5b6b82"))


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
        self._sel = None                       # 选中 clip id

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ---- 34px 工具条 ----
        from qfluentwidgets import PushButton, CaptionLabel
        head = QWidget(self)
        head.setObjectName("clipTlHead")
        head.setStyleSheet("QWidget#clipTlHead{background:%s;}" % C_HEAD_BG.name())
        self._head = head
        hl = QHBoxLayout(head)
        hl.setContentsMargins(8, 0, 8, 0)
        hl.setSpacing(8)

        f_btn = QFont()
        f_btn.setPixelSize(11)
        self.btn_split = PushButton("✂ 切分", head)
        self.btn_split.setToolTip("在播放头位置切分选中的 clip")
        self.btn_split.clicked.connect(self.split_selected_at_playhead)
        self.btn_delete = PushButton("🗑 删除", head)
        self.btn_delete.setToolTip("删除选中的 clip")
        self.btn_delete.clicked.connect(self.remove_selected)
        self.btn_follow = PushButton("▶ 跟随", head)
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
        self.hint = CaptionLabel(
            "拖播放头 scrub ｜ 拖 clip 移动 ｜ 拖两端 trim ｜ Ctrl+滚轮缩放 ｜ Del 删除", head)
        self.hint.setStyleSheet("color:%s;" % C_TEXT.name())
        hl.addWidget(self.hint)

        lay.addWidget(head)

        # ---- 自绘时间区 ----
        self.area = _ClipArea(self)
        lay.addWidget(self.area, 1)

        head.setFixedHeight(HEAD_H)
        self._update_height()
        # v3.2.1：垂直 Preferred（旧 Fixed 会让 QSplitter 拒绝把空间给时间轴，
        # 上下把手完全拖不动）；理想高度由 sizeHint 提供。
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._sync_buttons()

    # ================= 数据 / 状态 =================
    def set_project(self, proj):
        """存引用不拷贝；重置选择，保留播放头时刻（越界由 set_time 钳）。"""
        if isinstance(proj, dict) and proj.get("type") == VM.PROJECT_TYPE:
            self.proj = proj
        self._sel = None
        self.view_t0 = 0.0
        self._sync_buttons()
        self.refresh()

    def refresh(self):
        self._update_height()
        self._sync_buttons()
        self.area.update()

    def set_time(self, t):
        self._t = max(0.0, float(t))
        if self.follow_playhead:
            self.maybe_scroll_to(self._t)
        self.area.update()          # 只 update，量小

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
        tracks = self.proj.get("tracks", [])
        if len(tracks) > 1:
            avail = self.height() - HEAD_H - self.scaled_ruler_h() - 4
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
        for tr in self.proj.get("tracks", []):
            h = self.row_h(tr)
            out.append((y, h, tr))
            y += h
        return out

    def rows_bottom(self):
        g = self.row_geometry()
        return g[-1][0] + g[-1][1] if g else self.scaled_ruler_h()

    def ideal_height(self):
        """全部行按默认（未压缩）行高铺开的理想高度——只作 sizeHint。"""
        return (HEAD_H + self.scaled_ruler_h()
                + sum(self._row_h_default(t)
                      for t in self.proj.get("tracks", [])) + 4)

    def sizeHint(self):
        return QSize(600, self.ideal_height())

    def _update_height(self):
        # v3.2.1：高度交给 splitter/用户拖。minimumHeight 只保证
        # 「工具条+刻度尺+一行(14px)」可读；不再强制 resize 回内容高度
        # （旧逻辑把 splitter 把手钉死，时间轴完全拖不动）。
        self.setMinimumHeight(HEAD_H + self.scaled_ruler_h() + 14 + 4)

    def t_to_x(self, t):
        return NAME_W + (float(t) - self.view_t0) * self.pps

    def x_to_t(self, x):
        return (float(x) - NAME_W) / self.pps + self.view_t0

    def content_width(self):
        """外层放滚动容器时建议的面板宽。"""
        return int(NAME_W + (VM.project_duration(self.proj) + 2.0) * self.pps)

    def zoom_range_seconds(self):
        """当前视口能容纳的秒数。"""
        return max(0.5, (max(1, self.area.width()) - NAME_W) / self.pps)

    def scroll_host(self):
        """最近的滚动宿主（横向滚动由外层负责，这里只做跟随）。"""
        p = self.parentWidget()
        while p is not None:
            if isinstance(p, QAbstractScrollArea):
                return p
            p = p.parentWidget()
        return None

    def maybe_scroll_to(self, t):
        """把时刻 t 滚进可视区（外层可调；follow 开关打开时 set_time 自动调）。"""
        t = max(0.0, float(t))
        host = self.scroll_host()
        win = self.zoom_range_seconds()
        edge = win * 0.1
        if host is not None and host.width() > 0:
            bar = host.horizontalScrollBar()
            if bar is not None:
                x = self.t_to_x(t)            # 面板坐标 == 内容坐标
                if x > bar.value() + host.viewport().width() - 40:
                    bar.setValue(int(x - host.viewport().width() + 40))
                elif x < bar.value() + 40:
                    bar.setValue(max(0, int(x - 40)))
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
        if self._sel == cid:
            return
        self._sel = cid
        self._sync_buttons()
        if emit:
            self.selection_changed.emit(cid)
        self.area.update()

    def _sync_buttons(self):
        has = self._sel is not None
        self.btn_split.setEnabled(has)
        self.btn_delete.setEnabled(has)

    # ================= 模型操作（全部走 video_model） =================
    def _selected_clip(self):
        if not self._sel:
            return None
        tr, c = VM.find_clip(self.proj, self._sel)
        return c

    def split_selected_at_playhead(self):
        """[✂] 按钮 / 外部调用：在播放头处切分选中 clip，失败静默。"""
        if not self._sel:
            return False
        ok, _msg, _rid = VM.split_clip(self.proj, self._sel, self._t)
        if ok:
            self.edit_committed.emit()
            self.refresh()
        return ok

    def remove_selected(self):
        """[🗑] / Delete：删除选中 clip，失败（锁定等）静默。"""
        if not self._sel:
            return False
        ok, _msg = VM.remove_clip(self.proj, self._sel)
        if ok:
            self._set_sel(None)
            self.edit_committed.emit()
            self.refresh()
        return ok

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
        if (d is None or not d.get("moved") or d.get("pv") is None
                or d["id"] != c["id"]):
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
            # 空白（含行以下）：取消选中 + scrub；隐藏轨整行不可交互
            row = self._row_at(y)
            if row is not None and row[0].get("hidden"):
                return
            self.panel._set_sel(None)
            self._begin_scrub(x)
            return
        tr, c, zone = hit
        if tr.get("hidden"):
            return                       # 隐藏轨不可交互
        self.panel._set_sel(c["id"])
        if tr.get("locked"):
            return                       # 锁定轨：可选中，不可拖/trim
        self._drag = {
            "kind": zone, "id": c["id"], "c": c,
            "x0px": x, "t0": self.x_to_t(x),
            "start0": float(c["start"]), "dur0": float(c["dur"]),
            "pv": None, "snap_x": None, "moved": False,
        }

    def _begin_scrub(self, x):
        self._scrub = True
        self.panel.seeked.emit(self._scrub_t(x))
        self.panel.area.update()

    def _scrub_t(self, x):
        return round(max(0.0, self.x_to_t(x)), 2)   # 吸附到 0.01

    def mouseMoveEvent(self, ev):
        x, y = ev.position().x(), ev.position().y()
        d = self._drag
        if d is not None:
            if not d["moved"] and abs(x - d["x0px"]) > DRAG_THRESHOLD:
                d["moved"] = True
            if d["moved"]:
                self._update_preview(d, x)
            return
        if self._scrub:
            self.panel.seeked.emit(self._scrub_t(x))
            self.update()
            return
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
        if self._scrub:
            self._scrub = False
            self.panel.playhead_drag_stopped.emit()
        self._finish_drag()

    def _finish_drag(self):
        d, self._drag = self._drag, None
        if d is None or not d.get("moved") or d.get("pv") is None:
            self.update()
            return
        p = self.panel
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
        """Ctrl+滚轮：以鼠标时刻为锚缩放 pps（0.5~400）。"""
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
        ev.ignore()         # 无 Ctrl：交给外层滚动容器

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
                p.setPen(QPen(C_GRID if is_major else QColor(58, 67, 86, 110), 1))
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
            p.fillRect(QRectF(0, y0, NAME_W, rh), QColor(255, 209, 102, 18))
        f = QFont()
        f.setPixelSize(max(10, int(round(12 * s))))
        p.setFont(f)
        fm2 = p.fontMetrics()
        p.setPen(QPen(C_TEXT_HI))
        label = str(tr.get("name") or tr.get("id") or "")
        p.drawText(QRectF(8, y0, NAME_W - 58, rh), Qt.AlignVCenter | Qt.AlignLeft,
                   fm2.elidedText(label, Qt.ElideRight, NAME_W - 62))
        r_eye, r_lock = self._icon_rects(y0, rh)
        fi = QFont()
        fi.setPixelSize(max(10, int(round(12 * s))))
        p.setFont(fi)
        p.setPen(QPen(QColor(242, 246, 250, 255 if not hidden else 90)))
        p.drawText(r_eye, Qt.AlignCenter, "👁")
        p.setPen(QPen(QColor(242, 246, 250, 255 if tr.get("locked") else 90)))
        p.drawText(r_lock, Qt.AlignCenter, "🔒")
