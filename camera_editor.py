# -*- coding: utf-8 -*-
"""运镜编辑器面板（v3.6.2）。

对标对方工具的「便捷运镜」：用户能录多段路径、给背景与立绘各录一套
（独立构图）、还能在时间轴上拖关键帧、点播放看效果。

**为什么要独立一个面板**：引擎的 setTempAnimation 早就能表达多段运动，
但木偶之前只能靠导入 AI 写的原样指令，用户没法自己造。这个面板把
「用户拖出来的路径」翻译成那条指令，并让用户当场看到插值结果。

设计取舍（都是刻意的）：
  · **不做 3D 时间轴曲线编辑器**——对方那套是给专业运镜用的，我们
    先把「能录多段、能分背景/立绘、能看到结果」这三件核心做对。
  · 关键帧表用表格 + 数值输入，而不是纯拖拽画布：数值可复现、可测，
    而且和引擎的 duration 语义一一对应（拖拽反而难对齐）。
  · 预览用 camera_path.sample_at 采样后直接改 StageView 的显示，
    与导出口径同源（camera_path_test C1 有对拍断言）。
"""
import os

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QGridLayout,
                               QTableWidget, QTableWidgetItem, QHeaderView,
                               QAbstractItemView, QDoubleSpinBox, QSpinBox,
                               QScrollArea, QSizePolicy)
from qfluentwidgets import (BodyLabel, CaptionLabel, PushButton,
                            PrimaryPushButton, ComboBox, CardWidget,
                            TableWidget, LineEdit)

import camera_path as CP
import ui_theme as TH


class CameraPathPanel(QWidget):
    """多段路径运镜编辑器。"""

    # 用户点了「应用到当前时刻」：把生成的指令交给工作台落到 effects
    applied = Signal(dict)          # {"paths": [{"level","cmd"}, ...]}
    # 预览：采样结果交给工作台作用到舞台（与导出口径同源）
    preview_sampled = Signal(dict)  # {"stage": {...}, "fig": {...}, "ms": int}
    preview_reset = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._kfs = {"stage": CP.template_keys("缓慢推进", "stage"),
                     "fig": CP.template_keys("缓慢推进", "fig")}
        self._level = "stage"        # 当前编辑哪一层（背景/立绘）
        self._playing = False
        self._play_start = 0
        self._play_ms = 0

        self._timer = QTimer(self)
        self._timer.setInterval(33)      # ~30fps，够看
        self._timer.timeout.connect(self._on_tick)

        self._build()

    # ---------------- UI ----------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # 顶部：模板 + 层级切换
        top = QHBoxLayout()
        top.addWidget(CaptionLabel("快捷模板"))
        self.cmb_tpl = ComboBox()
        self.cmb_tpl.addItems(list(CP.TEMPLATE_NAMES))
        self.cmb_tpl.setMinimumWidth(120)
        top.addWidget(self.cmb_tpl)
        self.btn_load = PushButton("载入模板")
        top.addWidget(self.btn_load)

        top.addSpacing(16)
        top.addWidget(CaptionLabel("编辑层"))
        self.cmb_level = ComboBox()
        self.cmb_level.addItems(["背景（整个舞台）", "立绘（人物）"])
        top.addWidget(self.cmb_level)
        top.addStretch(1)
        root.addLayout(top)

        # 提示：独立构图说明
        hint = CaptionLabel(
            "背景与立绘各录一套路径 = 「独立构图」（对方工具的做法）；"
            "只想整体动就把两层录成一样。时长单位毫秒，"
            "第 1 帧是起点（它的时长不参与总长，与引擎语义一致）。", self)
        hint.setWordWrap(True)
        hint.setStyleSheet("color:%s; font-size:11px;" % TH.t("text_dim"))
        root.addWidget(hint)

        # 关键帧表
        self.tbl = TableWidget(self)
        self.tbl.setColumnCount(7)
        self.tbl.setHorizontalHeaderLabels(
            ["#", "时长ms", "缩放X", "缩放Y", "位移X", "位移Y", "旋转°"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tbl.setMinimumHeight(150)
        self.tbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self.tbl, 1)

        # 关键帧操作
        ops = QHBoxLayout()
        self.btn_add = PushButton("＋ 添加段")
        self.btn_del = PushButton("－ 删除段")
        self.btn_up = PushButton("上移")
        self.btn_down = PushButton("下移")
        for b in (self.btn_add, self.btn_del, self.btn_up, self.btn_down):
            ops.addWidget(b)
        ops.addStretch(1)
        self.lbl_total = CaptionLabel("总时长 0 ms", self)
        self.lbl_total.setStyleSheet("color:%s; font-size:11px;"
                                     % TH.t("text_hi"))
        ops.addWidget(self.lbl_total)
        root.addLayout(ops)

        # 精调区（改选中帧的数值）
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.addWidget(CaptionLabel("时长ms"), 0, 0)
        self.sp_dur = QSpinBox(self)
        self.sp_dur.setRange(0, 60000)
        self.sp_dur.setSingleStep(100)
        grid.addWidget(self.sp_dur, 1, 0)

        grid.addWidget(CaptionLabel("缩放X"), 0, 1)
        self.sp_sx = QDoubleSpinBox(self)
        self.sp_sx.setRange(0.05, 8.0)
        self.sp_sx.setSingleStep(0.05)
        self.sp_sx.setDecimals(3)
        grid.addWidget(self.sp_sx, 1, 1)

        grid.addWidget(CaptionLabel("缩放Y"), 0, 2)
        self.sp_sy = QDoubleSpinBox(self)
        self.sp_sy.setRange(0.05, 8.0)
        self.sp_sy.setSingleStep(0.05)
        self.sp_sy.setDecimals(3)
        grid.addWidget(self.sp_sy, 1, 2)

        grid.addWidget(CaptionLabel("位移X"), 0, 3)
        self.sp_px = QDoubleSpinBox(self)
        self.sp_px.setRange(-4000, 4000)
        self.sp_px.setSingleStep(10)
        self.sp_px.setDecimals(1)
        grid.addWidget(self.sp_px, 1, 3)

        grid.addWidget(CaptionLabel("位移Y"), 0, 4)
        self.sp_py = QDoubleSpinBox(self)
        self.sp_py.setRange(-4000, 4000)
        self.sp_py.setSingleStep(10)
        self.sp_py.setDecimals(1)
        grid.addWidget(self.sp_py, 1, 4)

        grid.addWidget(CaptionLabel("旋转°"), 0, 5)
        self.sp_rot = QDoubleSpinBox(self)
        self.sp_rot.setRange(-180, 180)
        self.sp_rot.setSingleStep(1)
        self.sp_rot.setDecimals(1)
        grid.addWidget(self.sp_rot, 1, 5)
        root.addLayout(grid)

        # 播放与落地
        bot = QHBoxLayout()
        self.btn_play = PrimaryPushButton("▶ 预览")
        self.btn_reset = PushButton("重置预览")
        self.btn_apply = PrimaryPushButton("应用到当前时刻")
        bot.addWidget(self.btn_play)
        bot.addWidget(self.btn_reset)
        bot.addStretch(1)
        bot.addWidget(self.btn_apply)
        root.addLayout(bot)

        self.lbl_state = CaptionLabel("", self)
        self.lbl_state.setStyleSheet("color:%s; font-size:11px;"
                                     % TH.t("text_dim"))
        self.lbl_state.setWordWrap(True)
        root.addWidget(self.lbl_state)

        # ---- 接线 ----
        self.cmb_level.currentIndexChanged.connect(self._on_level_changed)
        self.btn_load.clicked.connect(self._on_load_tpl)
        self.btn_add.clicked.connect(self._on_add)
        self.btn_del.clicked.connect(self._on_del)
        self.btn_up.clicked.connect(lambda: self._on_move(-1))
        self.btn_down.clicked.connect(lambda: self._on_move(1))
        self.tbl.itemSelectionChanged.connect(self._on_pick)
        for sp in (self.sp_dur, self.sp_sx, self.sp_sy,
                   self.sp_px, self.sp_py, self.sp_rot):
            sp.valueChanged.connect(self._on_spin)
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_reset.clicked.connect(self.reset_preview)
        self.btn_apply.clicked.connect(self.apply_now)

        self._refresh_table()

    # ---------------- 数据读写 ----------------
    def keys(self, level=None):
        return self._kfs[level or self._level]

    def set_keys(self, kfs, level=None):
        lv = level or self._level
        self._kfs[lv] = CP.normalize_keys(kfs)
        self._refresh_table()

    def _on_level_changed(self, idx):
        self._level = "fig" if idx == 1 else "stage"
        self._refresh_table()

    def _on_load_tpl(self):
        name = self.cmb_tpl.currentText()
        k = CP.template_keys(name, self._level)
        if k is None: return
        self.set_keys(k)
        self.lbl_state.setText("已载入模板「%s」（%s层）。可继续加段、改数值。"
                               % (name, "立绘" if self._level == "fig" else "背景"))

    # ---------------- 表格 ----------------
    def _refresh_table(self):
        kfs = self.keys()
        self.tbl.setRowCount(len(kfs))
        for i, kf in enumerate(kfs):
            sc = kf.get("scale") or CP.DEFAULTS["scale"]
            po = kf.get("position") or CP.DEFAULTS["position"]
            vals = [str(i + 1),
                    "%.0f" % float(kf.get("duration") or 0),
                    "%.3f" % float(sc.get("x", 1.0)),
                    "%.3f" % float(sc.get("y", 1.0)),
                    "%.1f" % float(po.get("x", 0.0)),
                    "%.1f" % float(po.get("y", 0.0)),
                    "%.1f" % float(kf.get("rotation") or 0.0)]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 0: it.setTextAlignment(Qt.AlignCenter)
                else: it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.tbl.setItem(i, c, it)
        self.lbl_total.setText("总时长 %d ms（%d 段）"
                               % (int(CP.total_ms(kfs)), len(kfs)))
        if self.tbl.rowCount() and not self.tbl.selectedItems(): self.tbl.selectRow(0)

    def _cur_row(self):
        rows = self.tbl.selectionModel().selectedRows() \
            if self.tbl.selectionModel() else []
        if rows: return rows[0].row()
        return 0 if self.tbl.rowCount() else -1

    def _on_pick(self):
        r = self._cur_row()
        kfs = self.keys()
        if r < 0 or r >= len(kfs): return
        kf = kfs[r]
        sc = kf.get("scale") or CP.DEFAULTS["scale"]
        po = kf.get("position") or CP.DEFAULTS["position"]
        blockers = (self.sp_dur, self.sp_sx, self.sp_sy,
                    self.sp_px, self.sp_py, self.sp_rot)
        for w in blockers:
            w.blockSignals(True)
        self.sp_dur.setValue(int(kf.get("duration") or 0))
        self.sp_sx.setValue(float(sc.get("x", 1.0)))
        self.sp_sy.setValue(float(sc.get("y", 1.0)))
        self.sp_px.setValue(float(po.get("x", 0.0)))
        self.sp_py.setValue(float(po.get("y", 0.0)))
        self.sp_rot.setValue(float(kf.get("rotation") or 0.0))
        for w in blockers:
            w.blockSignals(False)

    def _on_spin(self):
        r = self._cur_row()
        kfs = self.keys()
        if r < 0 or r >= len(kfs): return
        kf = kfs[r]
        kf["duration"] = float(self.sp_dur.value())
        kf["scale"] = {"x": float(self.sp_sx.value()),
                       "y": float(self.sp_sy.value())}
        kf["position"] = {"x": float(self.sp_px.value()),
                          "y": float(self.sp_py.value())}
        kf["rotation"] = float(self.sp_rot.value())
        # 保持选中行
        sel = r
        self._refresh_table()
        if 0 <= sel < self.tbl.rowCount(): self.tbl.selectRow(sel)

    def _on_add(self):
        kfs = self.keys()
        base = dict(kfs[-1]) if kfs else {}
        base["duration"] = 800
        kfs.append(base)
        self._refresh_table()
        self.tbl.selectRow(len(kfs) - 1)

    def _on_del(self):
        kfs = self.keys()
        if len(kfs) <= 2:
            self.lbl_state.setText("至少保留 2 段（起点 + 终点）。")
            return
        r = self._cur_row()
        if 0 <= r < len(kfs):
            kfs.pop(r)
            self._refresh_table()

    def _on_move(self, dy):
        kfs = self.keys()
        r = self._cur_row()
        j = r + dy
        if r < 0 or not (0 <= j < len(kfs)): return
        kfs[r], kfs[j] = kfs[j], kfs[r]
        self._refresh_table()
        self.tbl.selectRow(j)

    # ---------------- 预览 ----------------
    def toggle_play(self):
        if self._playing:
            self._stop()
            return
        self._play_ms = 0
        self._playing = True
        self.btn_play.setText("■ 停止")
        self._timer.start()

    def _stop(self):
        self._playing = False
        self._timer.stop()
        self.btn_play.setText("▶ 预览")

    def _on_tick(self):
        total = max(CP.total_ms(self.keys("stage")),
                    CP.total_ms(self.keys("fig")))
        if total <= 0:
            self._stop()
            return
        self._play_ms += self._timer.interval()
        if self._play_ms > total: self._play_ms = 0            # 循环播
        self._emit_preview(self._play_ms)

    def _emit_preview(self, ms):
        """把当前采样结果交给工作台去作用于舞台（与导出同源）。"""
        st = CP.sample_at(self.keys("stage"), ms)
        fg = CP.sample_at(self.keys("fig"), ms)
        self.preview_sampled.emit({"stage": st, "fig": fg, "ms": ms})

    def reset_preview(self):
        self._stop()
        self.preview_reset.emit()
        self.lbl_state.setText("已重置预览。")

    def apply_now(self):
        """把两层各自生成一条 setTempAnimation，交给工作台落到当前时刻。"""
        cmds = []
        for lv, target in (("stage", CP.TARGET_STAGE),
                           ("fig", CP.TARGET_FIG)):
            kfs = self.keys(lv)
            if CP.total_ms(kfs) <= 0: continue
            cmds.append({"level": lv,
                         "cmd": CP.build_cmd(kfs, target=target)})
        if not cmds:
            self.lbl_state.setText("当前路径没有时长（全是瞬切），没有可应用的运镜。")
            return
        self.applied.emit({"paths": cmds})