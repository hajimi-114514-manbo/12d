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
# 字色整体调亮：原来 #9aa4b2 在深底上偏灰暗，刻度几乎看不清
C_BG = QColor("#1b1f27")
C_ROW_A = QColor("#20252e")
C_ROW_B = QColor("#1d222a")
C_GRID = QColor("#3a4356")          # 网格线提亮，原来 #2c3340 几乎与底色同色
C_TEXT = QColor("#c8d1dc")          # 刻度/次要文字：明显提亮
C_TEXT_HI = QColor("#f2f6fa")       # 行名/主文字：接近纯白
C_HEAD = QColor("#252b35")
C_PLAYHEAD = QColor("#4d88ff")
C_KF = QColor("#ffd166")
C_KF_SEL = QColor("#ff6b6b")
C_LANE = QColor("#171b21")
C_TICK_MAJOR = QColor("#8fa3bd")    # 整秒刻度文字（比秒内小刻度更亮）
# 顶部高度拖拽条：与右侧面板 QSplitter::handle 同款配色（SPLIT_QSS）
C_HANDLE = QColor("#3a3f4a")
C_HANDLE_HL = QColor("#4d88ff")

# 尺寸（v2.11.2：全部可在运行时缩放，见 TimelinePanel.ui_scale）
ROW_H = 24          # 每行高度（基准值）
HEAD_H = 38         # 头部工具栏高度（基准值）
TIME_H = 22         # 时间刻度高度（基准值）
NAME_W = 150        # 左侧轨道名列宽（基准值）
LEFT_PAD = 8        # 时间轴左内边距

# 高度可调范围（整个面板的像素高）
PANEL_MIN_H = HEAD_H + TIME_H + ROW_H  # 最小：头部 + 刻度 + 1 行
PANEL_MAX_H = 720

# 界面缩放档位（0.8 = 紧凑，1.0 = 标准，1.25 = 大，1.5 = 特大）
SCALE_LEVELS = (0.85, 1.0, 1.25, 1.5)


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
        # v2.11.2 界面缩放：所有尺寸 = 基准值 × ui_scale
        self.ui_scale = 1.0
        self._manual_h = None          # 用户拖拽设定的面板高度（None=按行数自适应）
        self._h_drag = None            # 高度拖拽状态
        self._h_hot = False            # 鼠标悬在顶部拖拽条上（高亮用）
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(PANEL_MIN_H)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 0)   # 顶部留 2px 画高度拖拽条（同分隔条样式）
        lay.setSpacing(0)
        self._lay = lay

        # ---- 头部工具条 ----
        head = QWidget(self)
        head.setObjectName("tlHead")
        head.setStyleSheet("QWidget#tlHead{background:%s;}" % C_HEAD.name())
        self._head = head
        hl = QHBoxLayout(head)
        hl.setContentsMargins(8, 0, 8, 0)
        hl.setSpacing(8)
        self._hl = hl

        from qfluentwidgets import PushButton, CaptionLabel
        self.btn_add = PushButton("+ 关键帧", head)
        self.btn_add.setToolTip("在当前播放头位置，给选中的骨架节点打一个关键帧")
        self.btn_add.clicked.connect(self._add_kf_clicked)
        hl.addWidget(self.btn_add)

        self.btn_clear = PushButton("清空动画", head)
        self.btn_clear.clicked.connect(self._clear_all)
        hl.addWidget(self.btn_clear)

        hl.addSpacing(10)
        self.lbl_dur = CaptionLabel("时长", head)
        hl.addWidget(self.lbl_dur)
        self.spin_dur = QDoubleSpinBox(head)
        self.spin_dur.setRange(0.2, 600.0)
        self.spin_dur.setSingleStep(0.5)
        self.spin_dur.setDecimals(1)
        self.spin_dur.setSuffix(" s")
        self.spin_dur.valueChanged.connect(self._dur_changed)
        hl.addWidget(self.spin_dur)

        self.lbl_fps = CaptionLabel("帧率", head)
        hl.addWidget(self.lbl_fps)
        self.spin_fps = QSpinBox(head)
        self.spin_fps.setRange(1, 120)
        self.spin_fps.valueChanged.connect(self._fps_changed)
        hl.addWidget(self.spin_fps)

        hl.addSpacing(10)
        # 字号档位：紧凑 / 标准 / 大 / 特大
        self.lbl_scale = CaptionLabel("字号", head)
        hl.addWidget(self.lbl_scale)
        from PySide6.QtWidgets import QComboBox
        self.cmb_scale = QComboBox(head)
        for s in SCALE_LEVELS:
            self.cmb_scale.addItem("%d%%" % int(round(s * 100)), s)
        self.cmb_scale.setCurrentIndex(SCALE_LEVELS.index(1.0))
        self.cmb_scale.currentIndexChanged.connect(self._scale_changed)
        hl.addWidget(self.cmb_scale)

        hl.addStretch(1)
        self.hint = CaptionLabel("拖播放头 scrub ｜ 点菱形选中 ｜ 拖它改时间 ｜ 双击删除 ｜ 右键改缓动", head)
        hl.addWidget(self.hint)

        lay.addWidget(head)

        # ---- 轨道绘制区（自绘）----
        self.area = _TrackArea(self)
        lay.addWidget(self.area, 1)

        self._apply_scale()
        self.refresh()

    # ---------------- 缩放/尺寸 ----------------
    def _apply_scale(self):
        """按 ui_scale 统一设置所有控件尺寸与字体。"""
        s = float(self.ui_scale)
        fpx = max(9, int(round(12 * s)))          # 基础字号
        # 头部控件高度
        ctl_h = max(20, int(round(26 * s)))
        self._head.setFixedHeight(max(28, int(round(HEAD_H * s))))
        for w in (self.btn_add, self.btn_clear):
            w.setFixedHeight(ctl_h)
        for w in (self.spin_dur,):
            w.setFixedWidth(max(70, int(round(88 * s))))
            w.setFixedHeight(ctl_h)
        self.spin_fps.setFixedWidth(max(52, int(round(66 * s))))
        self.spin_fps.setFixedHeight(ctl_h)
        self.cmb_scale.setFixedWidth(max(66, int(round(84 * s))))
        self.cmb_scale.setFixedHeight(ctl_h)
        # 字体：所有头部控件 + 提示文字
        f = QFont()
        f.setPixelSize(fpx)
        for w in (self.btn_add, self.btn_clear, self.spin_dur, self.spin_fps,
                  self.cmb_scale, self.lbl_dur, self.lbl_fps, self.lbl_scale,
                  self.hint):
            try:
                w.setFont(f)
            except Exception:
                pass
        # 提示文字提亮（原来用 C_TEXT.name() 太暗）
        try:
            self.hint.setStyleSheet("color:%s;" % C_TEXT.name())
        except Exception:
            pass
        # 大字号时提示文字可能撑宽布局 -> 空间不够就隐藏，避免把窗口撑出屏幕
        try:
            need = sum(w.sizeHint().width() for w in
                       (self.btn_add, self.btn_clear, self.lbl_dur,
                        self.spin_dur, self.lbl_fps, self.spin_fps,
                        self.lbl_scale, self.cmb_scale))
            self.hint.setVisible(need < max(600, self.width() - 240))
        except Exception:
            pass
        self.setMinimumWidth(0)       # 面板本身不参与撑宽
        # 面板高度重算
        self._resize_panel()
        try:
            self.area.update()
        except Exception:
            pass

    def _scale_changed(self, idx):
        try:
            s = self.cmb_scale.itemData(idx) or 1.0
        except Exception:
            s = 1.0
        self.set_ui_scale(float(s))

    def set_ui_scale(self, s):
        """外部/持久化用：设置界面缩放并重排。"""
        s = max(SCALE_LEVELS[0], min(SCALE_LEVELS[-1], float(s)))
        self.ui_scale = s
        # 同步下拉框（不回环）
        try:
            self.cmb_scale.blockSignals(True)
            idx = min(range(len(SCALE_LEVELS)),
                      key=lambda i: abs(SCALE_LEVELS[i] - s))
            self.cmb_scale.setCurrentIndex(idx)
            self.cmb_scale.blockSignals(False)
        except Exception:
            pass
        self._apply_scale()

    def scaled_row_h(self):
        """行高：正常按缩放算；用户把面板拖高时，行高跟着放大填满可用空间。

        这样"拖高面板"是真的把轨道撑开（更容易点关键帧），
        而不是行高不变、下面留一片空白。
        """
        base = max(16, int(round(ROW_H * self.ui_scale)))
        if self._manual_h is None:
            return base
        n = max(1, len(self._rows()))
        avail = (int(self._manual_h) - self._head.height()
                 - self.scaled_time_h() - 6)
        if avail <= 0:
            return base
        per = avail // n
        # 最多放大到基准的 2.5 倍，避免一两条轨道被拉得夸张
        return max(base, min(int(round(base * 2.5)), int(per)))

    def scaled_time_h(self):
        return max(16, int(round(TIME_H * self.ui_scale)))

    def scaled_name_w(self):
        return max(90, int(round(NAME_W * self.ui_scale)))

    def _content_height(self):
        """按当前行数 + 缩放算出的自然高度。"""
        n = max(1, len(self._rows()))
        return (self._head.height() + self.scaled_time_h()
                + self.scaled_row_h() * n + 6)

    def _resize_panel(self):
        """面板高度：用户拖过/动过滑块就用手动值，否则按行数自适应。"""
        if self._manual_h is not None:
            h = int(self._manual_h)
        else:
            h = self._content_height()
        h = max(PANEL_MIN_H, min(PANEL_MAX_H, h))
        self.setFixedHeight(h)
        self.update()               # 顶部拖拽条随宽度重画

    # ---- 顶部高度拖拽条（v2.11.3：替代滑块，样式同 QSplitter 分隔条）----
    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        hot = self._h_hot or (self._h_drag is not None)
        p.fillRect(0, 0, self.width(), 2, C_HANDLE_HL if hot else C_HANDLE)
        p.end()

    # ---- 高度拖拽（面板上边缘：2px 可见拖拽条 + 6px 热区）----
    def _hit_h_edge(self, pos):
        return pos.y() <= 6

    def _set_hot(self, hot):
        if self._h_hot != hot:
            self._h_hot = hot
            self.update()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._hit_h_edge(ev.position()):
            self._h_drag = {"y0": int(ev.globalPosition().y()),
                            "h0": int(self.height())}
            self.setCursor(Qt.SizeVerCursor)
            self._set_hot(True)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._h_drag is not None:
            dy = int(ev.globalPosition().y()) - self._h_drag["y0"]
            # 面板在底部：向上拖（dy<0）变高
            new_h = self._h_drag["h0"] - dy
            new_h = max(PANEL_MIN_H, min(PANEL_MAX_H, new_h))
            self._manual_h = new_h
            self._resize_panel()
            ev.accept()
            return
        # 悬停到顶部拖拽条：高亮 + 调整光标
        hot = self._hit_h_edge(ev.position())
        self._set_hot(hot)
        if hot:
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._h_drag is not None:
            self._h_drag = None
            self.unsetCursor()
            self._set_hot(self._hit_h_edge(ev.position()))
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def leaveEvent(self, ev):
        self._set_hot(False)
        super().leaveEvent(ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        try:
            self.area.update()
        except Exception:
            pass


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
        self._resize_panel()          # 行数/缩放变了都要重算高度
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
        """时间轴绘制区（去掉左侧列名与时间刻度）。

        高度取**实际可用高度**（面板可能被用户拖高/拖矮），
        这样轨道行会按可用空间铺开，不会出现下方大块空白。
        """
        head_h = self._head.height()
        time_h = self.scaled_time_h()
        name_w = self.scaled_name_w()
        avail = max(1.0, float(self.height() - head_h - time_h))
        return QRectF(name_w, head_h + time_h,
                      max(1.0, self.width() - name_w - LEFT_PAD),
                      avail)

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
    def _geom(self):
        """当前几何参数（全部按 panel.ui_scale 缩放）。"""
        p = self.panel
        return (p._head.height(), p.scaled_time_h(),
                p.scaled_row_h(), p.scaled_name_w())

    def _row_at(self, y):
        head_h, time_h, row_h, _ = self._geom()
        rows = self.panel._rows()
        i = int((y - (head_h + time_h)) // row_h)
        if 0 <= i < len(rows):
            return i
        return None

    def _kf_at(self, x, y, px=None):
        """返回 (node, param, t) 或 None。"""
        if px is None:
            px = 7.0 * float(self.panel.ui_scale)
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

        panel = self.panel
        rows = panel._rows()
        dur = panel._dur()
        now = panel._now()
        head_h, time_h, row_h, name_w = self._geom()
        s = float(panel.ui_scale)
        lane = panel.lane_rect()

        # 左侧列名区背景
        p.fillRect(QRectF(0, head_h, name_w, self.height() - head_h), C_LANE)
        # 时间刻度区
        p.fillRect(QRectF(0, head_h, self.width(), time_h), C_HEAD)

        # ---- 字体（随缩放自适应，且最小 9px 保证可读）----
        f_tick = QFont()
        f_tick.setPixelSize(max(9, int(round(11 * s))))
        f_row = QFont()
        f_row.setPixelSize(max(10, int(round(12 * s))))

        # ---- 时间刻度 ----
        p.setFont(f_tick)
        fm = QFontMetrics(f_tick)
        step = _nice_step(dur / max(4.0, 10.0 * s))
        t = 0.0
        while t <= dur + 1e-9:
            x = panel.t_to_x(t)
            # 格子线：从刻度区底部通到底
            p.setPen(QPen(C_GRID, 1))
            p.drawLine(QPointF(x, head_h + time_h - 4),
                       QPointF(x, self.height()))
            # 整秒刻度用更亮的颜色，秒内的次刻度用稍暗的
            is_major = abs(t - round(t)) < 1e-6
            p.setPen(QPen(C_TICK_MAJOR if is_major else C_TEXT))
            txt = ("%.2f" % t).rstrip("0").rstrip(".") + "s"
            tw = fm.horizontalAdvance(txt)
            p.drawText(QPointF(min(x + 3, self.width() - tw - 2),
                               head_h + time_h - 6), txt)
            t += step

        # ---- 行 ----
        p.setFont(f_row)
        fm_row = QFontMetrics(f_row)
        kf_r = max(4.0, 5.0 * s)
        for i, (node, param, track) in enumerate(rows):
            y0 = head_h + time_h + i * row_h
            if y0 > self.height():
                break
            bg = C_ROW_A if i % 2 == 0 else C_ROW_B
            p.fillRect(QRectF(0, y0, self.width(), row_h), bg)
            # 行名（提亮 + 按缩放取字宽）
            p.setPen(QPen(C_TEXT_HI))
            label = "%s / %s" % (node, A.PARAM_LABELS.get(param, param))
            p.drawText(QRectF(8, y0, name_w - 14, row_h),
                       Qt.AlignVCenter | Qt.AlignLeft,
                       fm_row.elidedText(label, Qt.ElideRight, name_w - 16))
            # 行分隔线
            p.setPen(QPen(C_GRID, 1))
            p.drawLine(QPointF(0, y0 + row_h - 0.5),
                       QPointF(self.width(), y0 + row_h - 0.5))
            # 关键帧之间的连线
            if len(track) >= 2:
                p.setPen(QPen(QColor("#5b6b82"), max(1.0, 1.5 * s)))
                for a, b in zip(track, track[1:]):
                    p.drawLine(QPointF(panel.t_to_x(a["t"]), y0 + row_h / 2.0),
                               QPointF(panel.t_to_x(b["t"]), y0 + row_h / 2.0))
            # 关键帧菱形
            cy = y0 + row_h / 2.0
            for kf in track:
                kx = panel.t_to_x(kf["t"])
                sel = (panel._sel == (node, param, round(kf["t"], 6)))
                _draw_diamond(p, kx, cy, kf_r,
                              C_KF_SEL if sel else C_KF,
                              C_TEXT_HI if sel else QColor("#3a2f10"))
            # 名字列右侧分隔
            p.setPen(QPen(C_GRID, 1))
            p.drawLine(QPointF(name_w - 0.5, y0),
                       QPointF(name_w - 0.5, y0 + row_h))

        # ---- 播放头 ----
        px = panel.t_to_x(now)
        p.setPen(QPen(C_PLAYHEAD, max(1.5, 1.8 * s)))
        p.drawLine(QPointF(px, head_h), QPointF(px, self.height()))
        # 顶部小三角
        tri = max(5.0, 5.5 * s)
        path = QPainterPath()
        path.moveTo(px - tri, head_h)
        path.lineTo(px + tri, head_h)
        path.lineTo(px, head_h + tri * 1.4)
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
        if y >= self._geom()[0]:
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
        elif y >= self._geom()[0]:
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
