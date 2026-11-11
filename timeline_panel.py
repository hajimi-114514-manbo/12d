# -*- coding: utf-8 -*-
"""时间轴面板（v2.11.0）。

结构：
    ┌───────────────────────────────────────────────┐
    │ [▶] 0.00s / 4.00s   时长[4.0]s  帧率[30]  [+] │  ← 头部工具条
    ├──────────┬────────────────────────────────────┤
    │ head/旋转│ ◆        ◆          ◆             │  ← 轨道行
    │ head/Y位移│    ◆         ◆                   │
    │ body/Y位移│ ◆                              ◆ │
    └──────────┴────────────────────────────────────┘
                 ↑ 播放头竖线贯穿

交互：
  - 点时间轴空白处：跳转播放头（不发信号，由外层 redraw）
  - 拖播放头：连续 scrub
  - 点关键帧：选中（高亮）
  - 拖关键帧：改时间（吸附到 0.01s）
  - 双击关键帧：删除
  - 右键关键帧：删除 / 改缓动

刻意不做的事（v2.11.0 范围）：
  - 不做框选多个关键帧
  - 不做曲线编辑器（bezier 手柄）
  - 不做自动关键帧模式
"""
import math

from PySide6.QtCore import Qt, QRectF, QPointF, Signal, QSize
from PySide6.QtGui import (QPainter, QColor, QPen, QBrush, QPainterPath,
                           QFont, QFontMetrics)
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QSizePolicy, QMenu, QInputDialog, QDoubleSpinBox,
                               QSpinBox)

import anim as A

# 配色（跟随应用暗色主题）
C_BG = QColor("#1b1f27")
C_ROW_A = QColor("#20252e")
C_ROW_B = QColor("#1d222a")
C_GRID = QColor("#2c3340")
C_TEXT = QColor("#9aa4b2")
C_TEXT_HI = QColor("#e6edf3")
C_HEAD = QColor("#252b35")
C_PLAYHEAD = QColor("#4d88ff")
C_KF = QColor("#ffd166")
C_KF_SEL = QColor("#ff6b6b")
C_LANE = QColor("#171b21")

ROW_H = 22          # 每行高度
HEAD_H = 34         # 头部工具栏高度
TIME_H = 18         # 时间刻度高度
NAME_W = 132        # 左侧轨道名列宽
LEFT_PAD = 8        # 时间轴左内边距


class TimelinePanel(QWidget):
    """时间轴面板。所有改动都通过信号交给 Workbench 处理（保持单一数据源）。"""

    seeked = Signal(float)             # 播放头跳转（秒）
    keyframes_changed = Signal()       # 关键帧数据变了（需要 push history + 重绘）
    duration_changed = Signal(float)
    fps_changed = Signal(int)

    def __init__(self, parent=None, wb=None):
        super().__init__(parent)
        self.wb = wb if wb is not None else parent
        self._sel = None               # (node, param, t) 选中的关键帧
        self._drag = None              # 拖拽关键帧状态
        self._scrub = False
        self.setMouseTracking(True)
        self.setMinimumHeight(HEAD_H + TIME_H + ROW_H * 3)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ---- 头部工具条 ----
        head = QWidget(self)
        head.setFixedHeight(HEAD_H)
        head.setStyleSheet("background:%s;" % C_HEAD.name())
        hl = QHBoxLayout(head)
        hl.setContentsMargins(8, 0, 8, 0)
        hl.setSpacing(8)

        from qfluentwidgets import PushButton, CaptionLabel, BodyLabel
        self.btn_add = PushButton("+ 关键帧", head)
        self.btn_add.setFixedHeight(24)
        self.btn_add.setToolTip("在当前播放头位置，给选中的骨架节点打一个关键帧")
        self.btn_add.clicked.connect(self._add_kf_clicked)
        hl.addWidget(self.btn_add)

        self.btn_clear = PushButton("清空动画", head)
        self.btn_clear.setFixedHeight(24)
        self.btn_clear.clicked.connect(self._clear_all)
        hl.addWidget(self.btn_clear)

        hl.addSpacing(10)
        hl.addWidget(CaptionLabel("时长", head))
        self.spin_dur = QDoubleSpinBox(head)
        self.spin_dur.setRange(0.2, 600.0)
        self.spin_dur.setSingleStep(0.5)
        self.spin_dur.setDecimals(1)
        self.spin_dur.setSuffix(" s")
        self.spin_dur.setFixedWidth(84)
        self.spin_dur.setFixedHeight(24)
        self.spin_dur.valueChanged.connect(self._dur_changed)
        hl.addWidget(self.spin_dur)

        hl.addWidget(CaptionLabel("帧率", head))
        self.spin_fps = QSpinBox(head)
        self.spin_fps.setRange(1, 120)
        self.spin_fps.setFixedWidth(64)
        self.spin_fps.setFixedHeight(24)
        self.spin_fps.valueChanged.connect(self._fps_changed)
        hl.addWidget(self.spin_fps)

        hl.addStretch(1)
        self.hint = CaptionLabel("拖播放头 scrub ｜ 点关键帧选中 ｜ 拖它改时间 ｜ 双击删除 ｜ 右键改缓动", head)
        self.hint.setStyleSheet("color:%s;" % C_TEXT.name())
        hl.addWidget(self.hint)
        lay.addWidget(head)

        # ---- 轨道绘制区（自绘）----
        self.area = _TrackArea(self)
        lay.addWidget(self.area, 1)

        self.refresh()

    # ---------------- 数据访问 ----------------
    def _tracks(self):
        rig = getattr(self.wb, "rig", None)
        if not isinstance(rig, dict):
            return {}
        kf = rig.get("keyframes")
        return kf if isinstance(kf, dict) else {}

    def _rows(self):
        """[(node, param, track)] —— 时间轴显示的行。"""
        return A.all_tracks(self._tracks())

    def _dur(self):
        return max(0.2, float(getattr(self.wb, "duration", 4.0)))

    def _now(self):
        return float(getattr(self.wb, "t", 0.0))

    # ---------------- 刷新 ----------------
    def refresh(self):
        """外部数据变了以后调用：重设 spin 值 + 重绘 + 按行数调整高度。"""
        try:
            self.spin_dur.blockSignals(True)
            self.spin_dur.setValue(self._dur())
            self.spin_dur.blockSignals(False)
            self.spin_fps.blockSignals(True)
            self.spin_fps.setValue(int(getattr(self.wb, "fps", 30)))
            self.spin_fps.blockSignals(False)
        except Exception:
            pass
        n = max(1, len(self._rows()))
        self.setFixedHeight(HEAD_H + TIME_H + ROW_H * n + 6)
        self.area.update()

    # ---------------- 头部动作 ----------------
    def _dur_changed(self, v):
        self.duration_changed.emit(float(v))
        self.area.update()

    def _fps_changed(self, v):
        self.fps_changed.emit(int(v))

    def _clear_all(self):
        rig = getattr(self.wb, "rig", None)
        if not isinstance(rig, dict):
            return
        if not A.has_anim(rig.get("keyframes")):
            return
        rig["keyframes"] = {}
        self.wb.push_history()          # 先改后 push（与项目既有约定一致）
        self._sel = None
        self.keyframes_changed.emit()
        self.refresh()

    def _add_kf_clicked(self):
        """给"当前选中的骨架节点"在当前时间打关键帧。

        选中节点从 wb.tree 的当前项取；没选中就提示。
        """
        node = self._current_node()
        if not node:
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.warning("提示", "先在右侧「骨架节点」里选中一个节点",
                            parent=self, position=InfoBarPosition.TOP, duration=2000)
            return
        self.add_keyframe_for_node(node)

    def _current_node(self):
        try:
            it = self.wb.tree.currentItem()
            if it is None:
                return None
            n = it.data(Qt.UserRole)
            return n if isinstance(n, str) else None
        except Exception:
            return None

    def add_keyframe_for_node(self, node):
        """给某节点的"当前所有非静止通道"打帧。

        策略：对 rot/tx/ty/sc/sc_y 全部打帧（用户之后可单独删）。
        值取当前 t 下 animation_for 的基线值，这样"打帧"不改变当前画面。
        """
        rig = getattr(self.wb, "rig", None)
        if not isinstance(rig, dict):
            return
        t = self._now()
        nd = next((n for n in rig["nodes"] if n["node"] == node), None)
        if nd is None:
            return
        import l2d_rig_core as C
        base = C.animation_for(node, t, nd, None)   # 取 sin 基线（不含已有轨道）
        kf = A.clone_tracks(rig.get("keyframes"))
        for p in ("rot", "tx", "ty", "sc"):
            kf = A.set_keyframe(kf, node, p, t, float(base.get(p, A.DEFAULTS[p])))
        # sc_y 只有非 None 才打（None 表示"该参数不参与"）
        if base.get("sc_y") is not None:
            kf = A.set_keyframe(kf, node, "sc_y", t, float(base["sc_y"]))
        rig["keyframes"] = kf
        self.wb.push_history()          # 先改后 push（与项目既有约定一致）
        self.keyframes_changed.emit()
        self.refresh()

    # ---------------- 坐标换算 ----------------
    def lane_rect(self):
        """时间轴绘制区（去掉左侧列名与时间刻度）。"""
        rows = self._rows()
        h = HEAD_H + TIME_H + ROW_H * max(1, len(rows))
        return QRectF(NAME_W, HEAD_H + TIME_H,
                      max(1.0, self.width() - NAME_W - LEFT_PAD),
                      max(1.0, h - HEAD_H - TIME_H))

    def t_to_x(self, t):
        r = self.lane_rect()
        d = self._dur()
        return r.left() + r.width() * max(0.0, min(1.0, t / d))

    def x_to_t(self, x):
        r = self.lane_rect()
        if r.width() <= 0:
            return 0.0
        u = (x - r.left()) / r.width()
        return max(0.0, min(1.0, u)) * self._dur()


class _TrackArea(QWidget):
    """自绘的轨道区：时间刻度 + 行背景 + 关键帧点 + 播放头。"""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ---- 命中测试 ----
    def _row_at(self, y):
        rows = self.panel._rows()
        top = HEAD_H + TIME_H
        i = int((y - top) // ROW_H)
        if 0 <= i < len(rows):
            return i
        return None

    def _kf_at(self, x, y, px=7.0):
        """返回 (node, param, t) 或 None。"""
        i = self._row_at(y)
        if i is None:
            return None
        rows = self.panel._rows()
        node, param, track = rows[i]
        for kf in track:
            kx = self.panel.t_to_x(kf["t"])
            if abs(kx - x) <= px:
                return (node, param, kf["t"])
        return None

    # ---- 绘制 ----
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), C_BG)

        rows = self.panel._rows()
        dur = self.panel._dur()
        lane = self.panel.lane_rect()
        now = self.panel._now()

        # 左侧列名区背景
        p.fillRect(QRectF(0, HEAD_H, NAME_W, self.height() - HEAD_H), C_LANE)
        # 时间刻度区
        p.fillRect(QRectF(0, HEAD_H, self.width(), TIME_H), C_HEAD)

        # ---- 时间刻度 ----
        font = QFont()
        font.setPointSize(7)
        p.setFont(font)
        p.setPen(QPen(C_TEXT))
        fm = QFontMetrics(font)
        # 选一个"好看"的刻度间隔：保证最多 ~10 条主刻度
        step = _nice_step(dur / 10.0)
        t = 0.0
        while t <= dur + 1e-9:
            x = self.panel.t_to_x(t)
            p.setPen(QPen(C_GRID, 1))
            p.drawLine(QPointF(x, HEAD_H + TIME_H - 4),
                       QPointF(x, self.height()))
            p.setPen(QPen(C_TEXT))
            s = ("%.2f" % t).rstrip("0").rstrip(".") + "s"
            p.drawText(QPointF(x + 2, HEAD_H + TIME_H - 5), s)
            t += step

        # ---- 行 ----
        for i, (node, param, track) in enumerate(rows):
            y0 = HEAD_H + TIME_H + i * ROW_H
            bg = C_ROW_A if i % 2 == 0 else C_ROW_B
            p.fillRect(QRectF(0, y0, self.width(), ROW_H), bg)
            # 行名
            p.setPen(QPen(C_TEXT_HI))
            label = "%s / %s" % (node, A.PARAM_LABELS.get(param, param))
            p.drawText(QRectF(6, y0, NAME_W - 10, ROW_H),
                       Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(label, Qt.ElideRight, NAME_W - 12))
            # 分隔线
            p.setPen(QPen(C_GRID, 1))
            p.drawLine(QPointF(0, y0 + ROW_H - 0.5),
                       QPointF(self.width(), y0 + ROW_H - 0.5))
            # 关键帧之间的连线（直观看出插值）
            if len(track) >= 2:
                p.setPen(QPen(QColor("#4a5568"), 1.5))
                for a, b in zip(track, track[1:]):
                    p.drawLine(QPointF(self.panel.t_to_x(a["t"]), y0 + ROW_H / 2),
                               QPointF(self.panel.t_to_x(b["t"]), y0 + ROW_H / 2))
            # 关键帧菱形
            cy = y0 + ROW_H / 2
            for kf in track:
                kx = self.panel.t_to_x(kf["t"])
                sel = (self.panel._sel == (node, param, round(kf["t"], 6)))
                _draw_diamond(p, kx, cy, 5.0,
                              C_KF_SEL if sel else C_KF,
                              C_TEXT_HI if sel else QColor("#3a2f10"))
            # 名字列右侧分隔
            p.setPen(QPen(C_GRID, 1))
            p.drawLine(QPointF(NAME_W - 0.5, y0), QPointF(NAME_W - 0.5, y0 + ROW_H))

        # ---- 播放头 ----
        px = self.panel.t_to_x(now)
        p.setPen(QPen(C_PLAYHEAD, 1.5))
        p.drawLine(QPointF(px, HEAD_H), QPointF(px, self.height()))
        # 顶部小三角
        path = QPainterPath()
        path.moveTo(px - 5, HEAD_H)
        path.lineTo(px + 5, HEAD_H)
        path.lineTo(px, HEAD_H + 7)
        path.closeSubpath()
        p.setBrush(QBrush(C_PLAYHEAD))
        p.setPen(Qt.NoPen)
        p.drawPath(path)
        p.end()

    # ---- 交互 ----
    def mousePressEvent(self, ev):
        x, y = ev.position().x(), ev.position().y()
        if ev.button() == Qt.RightButton:
            hit = self._kf_at(x, y)
            if hit:
                self._context_menu(hit, ev)
            return
        if ev.button() != Qt.LeftButton:
            return
        hit = self._kf_at(x, y)
        if hit is not None:
            self.panel._sel = (hit[0], hit[1], round(hit[2], 6))
            self.panel._drag = {"hit": hit, "x0": x,
                                "t0": hit[2], "moved": False}
            self.update()
            return
        if y >= HEAD_H:
            # 点空白 = 跳播放头
            self.panel._scrub = True
            self.panel.seeked.emit(self.panel.x_to_t(x))
            self.update()

    def mouseMoveEvent(self, ev):
        x, y = ev.position().x(), ev.position().y()
        d = self.panel._drag
        if d is not None:
            if not d["moved"] and abs(x - d["x0"]) > 2:
                d["moved"] = True          # 标记已拖动（松手时才 push）
            if d["moved"]:
                dx = x - d["x0"]
                lane = self.panel.lane_rect()
                dt = 0.0
                if lane.width() > 0:
                    dt = dx / lane.width() * self.panel._dur()
                self._move_kf(d["hit"], d["t0"] + dt)
            return
        if self.panel._scrub:
            self.panel.seeked.emit(self.panel.x_to_t(x))
            self.update()
            return
        # 悬停光标
        if self._kf_at(x, y):
            self.setCursor(Qt.SizeHorCursor)
        elif y >= HEAD_H:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()

    def mouseReleaseEvent(self, ev):
        d = self.panel._drag
        self.panel._drag = None
        self.panel._scrub = False
        if d is not None and d.get("moved"):
            # 拖动结束才记录历史（拖动过程中数据已逐帧更新）
            self.panel.wb.push_history()
            self.panel.keyframes_changed.emit()
            self.panel.refresh()
        self.update()

    def mouseDoubleClickEvent(self, ev):
        hit = self._kf_at(ev.position().x(), ev.position().y())
        if hit is None:
            return
        self._delete_kf(hit)

    # ---- 关键帧操作 ----
    def _move_kf(self, hit, new_t):
        node, param, old_t = hit
        rig = getattr(self.panel.wb, "rig", None)
        if not isinstance(rig, dict):
            return
        new_t = max(0.0, min(self.panel._dur(), round(new_t, 2)))
        if abs(new_t - old_t) < 1e-9:
            return
        kf = A.clone_tracks(rig.get("keyframes"))
        nt = kf.get(node, {}).get(param, [])
        # 取老帧的值与缓动
        val, ease = None, "linear"
        for k in nt:
            if abs(k["t"] - old_t) < 0.05:
                val, ease = k["v"], k.get("ease", "linear")
                break
        if val is None:
            return
        kf = A.remove_keyframe(kf, node, param, old_t, eps=0.05)
        kf = A.set_keyframe(kf, node, param, new_t, val, ease)
        rig["keyframes"] = kf
        # 更新选中与拖拽锚点
        self.panel._sel = (node, param, round(new_t, 6))
        if self.panel._drag is not None:
            self.panel._drag["hit"] = (node, param, new_t)
            self.panel._drag["t0"] = new_t
        self.panel.refresh()
        self.update()

    def _delete_kf(self, hit):
        node, param, t = hit
        rig = getattr(self.panel.wb, "rig", None)
        if not isinstance(rig, dict):
            return
        rig["keyframes"] = A.remove_keyframe(
            A.clone_tracks(rig.get("keyframes")), node, param, t)
        self.panel.wb.push_history()        # 先改后 push
        self.panel._sel = None
        self.panel.keyframes_changed.emit()
        self.panel.refresh()

    def _context_menu(self, hit, ev):
        node, param, t = hit
        from qfluentwidgets import RoundMenu, Action, FluentIcon
        m = RoundMenu(parent=self)
        m.addAction(Action(FluentIcon.DELETE, "删除这个关键帧",
                           triggered=lambda: self._delete_kf(hit)))
        m.addSeparator()
        cur_ease = "linear"
        rig = getattr(self.panel.wb, "rig", None)
        if isinstance(rig, dict):
            for k in rig.get("keyframes", {}).get(node, {}).get(param, []):
                if abs(k["t"] - t) < 0.05:
                    cur_ease = k.get("ease", "linear")
                    break
        for ease in A.EASES:
            label = ("● " if ease == cur_ease else "    ") + A.EASE_LABELS[ease]
            m.addAction(Action(FluentIcon.SYNC, label,
                               triggered=lambda e=ease: self._set_ease(hit, e)))
        m.exec(ev.globalPosition().toPoint())

    def _set_ease(self, hit, ease):
        node, param, t = hit
        rig = getattr(self.panel.wb, "rig", None)
        if not isinstance(rig, dict):
            return
        kf = A.clone_tracks(rig.get("keyframes"))
        nt = kf.get(node, {}).get(param, [])
        for k in nt:
            if abs(k["t"] - t) < 0.05:
                k["ease"] = ease
                break
        rig["keyframes"] = kf
        self.panel.wb.push_history()        # 先改后 push
        self.panel.keyframes_changed.emit()
        self.panel.refresh()
        self.update()


def _draw_diamond(p, cx, cy, r, fill, edge):
    path = QPainterPath()
    path.moveTo(cx, cy - r)
    path.lineTo(cx + r, cy)
    path.lineTo(cx, cy + r)
    path.lineTo(cx - r, cy)
    path.closeSubpath()
    p.setBrush(QBrush(fill))
    p.setPen(QPen(edge, 1))
    p.drawPath(path)


def _nice_step(raw):
    """把 raw 秒规整成 1/2/5 x 10^n 的"好看"刻度间隔。"""
    if raw <= 0:
        return 1.0
    e = math.floor(math.log10(raw))
    base = 10 ** e
    for m in (1, 2, 5, 10):
        if raw <= base * m:
            return base * m
    return base * 10
