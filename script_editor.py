# -*- coding: utf-8 -*-
"""F1 文本页剧情编辑器（台词表 UI）。

工作台 Pivot「文本」标签页的实装：台词列表（列=时刻/名字/内容/时长，按 t
排序）+ 工具栏（添加/编辑/保存/删除）+ 表单（who 补全、多行内容、dur、t）。

与工作台的约定（workbench 只挂，本模块自持 UI）：
    - 持有 workbench 引用 `wb`，读 `wb.proj["dialogue"]` / `wb._t`；
    - 所有写路径：改 proj["dialogue"]（t/dur 非负 float、who/text str，规则
      对齐 video_model._sanitize_dialogue；添加时空 text 拒收）
      -> wb.push_history() -> wb.render_frame(wb._t)（字幕即时可见）；
    - 点行 -> wb.seek(t) 联动 StageView；当前 _t 生效行高亮；
    - 与时间轴零耦合：不选 clip 也能用，dialogue 不进 tracks。

编辑器 widget 常驻（tab 切换只 show/hide + refresh），表单内容不丢。
"""
from PySide6.QtCore import Qt, QStringListModel
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
                               QTableWidget, QTableWidgetItem,
                               QAbstractItemView, QHeaderView,
                               QPlainTextEdit, QCompleter)
from qfluentwidgets import (PushButton, StrongBodyLabel, CaptionLabel,
                            LineEdit, DoubleSpinBox, ComboBox)

import video_model as V

try:
    import motion_table as MT
except Exception:                              # pragma: no cover
    MT = None

_HL_COLOR = "#2f3d5c"          # 当前时刻生效行高亮
# v3.4.7：QTableWidget / QPlainTextEdit 是裸 Qt 控件，不吃 qfluentwidgets
# 深色主题 —— 深色工作台里是一整块系统默认白底 + 小号系统默认字体，
# 与 fluent 组件（深底、14px）拼在一起又白又字小。统一铺深色 + 字号。
_PANEL_QSS = (
    "QTableWidget{background:#1b1e24;color:#e8ecf4;gridline-color:#262b34;"
    "border:1px solid #262b34;border-radius:6px;"
    "selection-background-color:#2f3d5c;selection-color:#f2f6fa;}"
    "QTableWidget::item{padding:2px 4px;}"
    "QHeaderView::section{background:#23262e;color:#c8d1dc;"
    "border:none;border-right:1px solid #262b34;border-bottom:1px solid #262b34;"
    "padding:4px 6px;font-size:12px;}"
    "QTableCornerButton::section{background:#23262e;border:none;}")
_EDITOR_QSS = (
    "QPlainTextEdit{background:#1b1e24;color:#e8ecf4;"
    "border:1px solid #262b34;border-radius:6px;"
    "selection-background-color:#2f3d5c;selection-color:#f2f6fa;}")
_COL_T, _COL_WHO, _COL_TEXT, _COL_DUR = 0, 1, 2, 3
_COL_MOTION, _COL_EXPR = 4, 5
_NCOL = 6
_HEADERS = ("时刻(s)", "名字", "内容", "时长(s)", "动作", "表情")


def _fluent_font(w, px=13):
    """裸控件对齐 fluent 视觉字号（BodyLabel 默认 14px 略大，表格 13 合适）。"""
    f = w.font()
    f.setPixelSize(px)
    return f


class ScriptEditor(QWidget):
    """台词表编辑器。"""

    def __init__(self, wb, parent=None):
        super().__init__(parent)
        self.wb = wb
        self._rows = []          # 按 t 排序的台词 dict（展示用，持有原引用）
        self._editing_obj = None  # 正在编辑的台词 dict（None=新增）

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        lay.addWidget(StrongBodyLabel("剧情", self))

        # ---- 列表 ----
        self.table = QTableWidget(0, _NCOL, self)
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(_PANEL_QSS)          # v3.4.7 深色适配
        self.table.setFont(_fluent_font(self.table))  # v3.4.7 字体统一
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(_COL_T, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_WHO, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_TEXT, QHeaderView.Stretch)
        hh.setSectionResizeMode(_COL_DUR, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_MOTION, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_EXPR, QHeaderView.ResizeToContents)
        self.table.cellClicked.connect(self._on_click)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        lay.addWidget(self.table, 1)

        # ---- 工具栏 ----
        tb = QHBoxLayout()
        self.btn_add = PushButton("添加台词", self)
        self.btn_add.clicked.connect(self._begin_add)
        tb.addWidget(self.btn_add)
        self.btn_edit = PushButton("编辑选中", self)
        self.btn_edit.clicked.connect(self._begin_edit)
        tb.addWidget(self.btn_edit)
        self.btn_save = PushButton("保存", self)
        self.btn_save.clicked.connect(self._save)
        tb.addWidget(self.btn_save)
        self.btn_del = PushButton("删除选中", self)
        self.btn_del.clicked.connect(self.delete_selected)
        tb.addWidget(self.btn_del)
        tb.addStretch(1)
        lay.addLayout(tb)

        # ---- 表单 ----
        self.ed_who = LineEdit(self)
        self.ed_who.setPlaceholderText("名字（可空=旁白；候选见下拉补全）")
        self.ed_who.setMaximumWidth(220)
        self.cpl_who = QCompleter([], self)
        self.cpl_who.setWidget(self.ed_who)
        self.ed_who.textChanged.connect(lambda _t: self.cpl_who.setCompletionPrefix(
            self.ed_who.text()))
        f1 = QHBoxLayout()
        f1.addWidget(CaptionLabel("名字", self))
        f1.addWidget(self.ed_who, 1)
        lay.addLayout(f1)

        self.ed_content = QPlainTextEdit(self)
        self.ed_content.setPlaceholderText("台词内容（不能为空）")
        self.ed_content.setMaximumHeight(110)
        self.ed_content.setStyleSheet(_EDITOR_QSS)          # v3.4.7 深色适配
        self.ed_content.setFont(_fluent_font(self.ed_content))
        lay.addWidget(self.ed_content, 1)

        f2 = QHBoxLayout()
        f2.addWidget(CaptionLabel("时刻(s)", self))
        self.spin_t = DoubleSpinBox(self)
        self.spin_t.setRange(0.0, 600.0)
        self.spin_t.setSingleStep(0.1)
        self.spin_t.setDecimals(1)
        f2.addWidget(self.spin_t)
        f2.addWidget(CaptionLabel("时长(s)", self))
        self.spin_dur = DoubleSpinBox(self)
        self.spin_dur.setRange(0.0, 600.0)
        self.spin_dur.setSingleStep(0.1)
        self.spin_dur.setDecimals(1)
        f2.addWidget(self.spin_dur)
        f2.addStretch(1)
        lay.addLayout(f2)
        # 动作/表情（可选，空=不指定；并行 id 列表方案，不用 userData）
        f3 = QHBoxLayout()
        self._motion_choices = [("", "（不指定）")] + self._catalog_choices("motion")
        self._expression_choices = [("", "（不指定）")] + self._catalog_choices("expression")
        self._motion_ids = [c[0] for c in self._motion_choices]
        self._expression_ids = [c[0] for c in self._expression_choices]
        f3.addWidget(CaptionLabel("动作", self))
        self.cmb_motion = ComboBox(self)
        for _cid, name in self._motion_choices:
            self.cmb_motion.addItem(name)
        f3.addWidget(self.cmb_motion, 1)
        f3.addWidget(CaptionLabel("表情", self))
        self.cmb_expr = ComboBox(self)
        for _eid, name in self._expression_choices:
            self.cmb_expr.addItem(name)
        f3.addWidget(self.cmb_expr, 1)
        lay.addLayout(f3)
        self._mode_lbl = CaptionLabel("新增模式", self)
        lay.addWidget(self._mode_lbl)

        # ---- F2-L5（v3.5）：分支编辑区（多幕工程才出现）----
        # 挂左列（窄栏）：控件全部纵向堆叠，最小宽度压到 150，
        # 任何窗口宽度都不会被裁成 "..."。
        self.branch_panel = _BranchPanel(wb, self)
        self.branch_panel.setMinimumWidth(150)
        self.branch_panel.setVisible(False)
        lay.addWidget(self.branch_panel, 1)

    # ---------- 数据 ----------
    def _dialogue(self):
        """当前幕的台词列表（**原引用**，写入即生效）。

        F2-L5（v3.5）：必须走 act_view —— v2 多幕工程顶层没有 dialogue，
        直接 proj.get("dialogue") 会拿到空列表 -> 剧情编辑器整块空白。
        act_view 对 v1 返回顶层**原引用**（旧行为全等），对 v2 返回
        该幕的真实 list（新增/编辑/删除都落到该幕）。"""
        try:
            d = V.act_view(self.wb.proj,
                            getattr(self.wb, "_cur_aid", None))["dialogue"]
        except Exception:
            d = self.wb.proj.get("dialogue")
        return d if isinstance(d, list) else []

    def _valid(self, item):
        """与 video_model._sanitize_dialogue 同规则的单条校验，返回清洗后 dict。"""
        if not isinstance(item, dict):
            return None
        cleaned = V._sanitize_dialogue([item])
        return cleaned[0] if cleaned else None

    def _valid_obj(self, item):
        """合法则返回**原对象引用**（供编辑/删除按 is 定位）；非法 None。"""
        return item if self._valid(item) is not None else None

    @staticmethod
    def _catalog_choices(kind):
        """(id, name) 候选；motion_table 不可用时回退空列表。"""
        try:
            if MT is None:
                return []
            cat = MT.catalog()
            if kind == "motion":
                return [(m["id"], m["name"]) for m in cat["motions"]]
            return [(e["id"], e["name"]) for e in cat["expressions"]]
        except Exception:
            return []

    def _set_combo_by_data(self, combo, val, ids):
        """按 id（并行列表）设置下拉当前项，不触发信号。"""
        try:
            combo.blockSignals(True)
            if val and val in ids:
                combo.setCurrentIndex(ids.index(val))
            combo.blockSignals(False)
        except Exception:
            pass

    def _cur_choice(self, combo, ids):
        """取当前下拉对应 id（越界回空串）。"""
        idx = combo.currentIndex()
        if 0 <= idx < len(ids):
            return ids[idx]
        return ""

    def refresh(self):
        """按 proj["dialogue"] 重渲染列表（按 t 排序）+ 更新候选。

        self._rows 持有**原对象引用**（脏行跳过），编辑/删除据此按 is 定位。
        """
        rows = []
        for it in self._dialogue():
            o = self._valid_obj(it)
            if o is not None:
                rows.append(o)
        rows.sort(key=lambda x: (float(x["t"]), float(x["dur"])))
        self._rows = rows
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table.setItem(i, _COL_T, QTableWidgetItem("%.1f" % float(r["t"])))
            self.table.setItem(i, _COL_WHO, QTableWidgetItem(str(r.get("who") or "")))
            self.table.setItem(i, _COL_TEXT, QTableWidgetItem(str(r.get("text") or "")))
            self.table.setItem(i, _COL_DUR, QTableWidgetItem("%.1f" % float(r["dur"])))
            self.table.setItem(i, _COL_MOTION,
                               QTableWidgetItem(str(r.get("motion") or "—")))
            self.table.setItem(i, _COL_EXPR,
                               QTableWidgetItem(str(r.get("expression") or "—")))
        # who 候选 = 现有 who 去重（自由输入不受影响）
        names = sorted({str(r.get("who")) for r in rows if r.get("who")})
        self.cpl_who.setModel(QStringListModel(names, self.cpl_who))
        # F2-L5：分支区跟着刷（切幕/增删幕后会变）
        try:
            self.branch_panel.refresh()
        except Exception:
            pass
        self.refresh_highlight()

    def refresh_highlight(self):
        """当前 _t 落在 [t, t+dur) 的行高亮。"""
        try:
            t = float(getattr(self.wb, "_t", 0.0))
        except Exception:
            t = 0.0
        for i, r in enumerate(self._rows):
            active = float(r["t"]) <= t < float(r["t"]) + float(r["dur"])
            color = QColor(_HL_COLOR) if active else QColor(0, 0, 0, 0)
            for c in range(_NCOL):
                it = self.table.item(i, c)
                if it is not None:
                    it.setBackground(color)

    def active_row_index(self):
        """当前 _t 生效行在 self._rows 中的下标；无则 -1。"""
        try:
            t = float(getattr(self.wb, "_t", 0.0))
        except Exception:
            t = 0.0
        for i, r in enumerate(self._rows):
            if float(r["t"]) <= t < float(r["t"]) + float(r["dur"]):
                return i
        return -1

    # ---------- 行操作 ----------
    def _row_obj(self, row):
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def _selected_row(self):
        """当前行；无 current 则取第一个选中行。"""
        row = self.table.currentRow()
        if row >= 0:
            return row
        rs = self.table.selectedRows()
        return rs[0].row() if rs else -1

    def _on_click(self, row, _col):
        self._seek_row(row)

    def _seek_row(self, row):
        obj = self._row_obj(row)
        if obj is not None:
            try:
                self.wb.seek(float(obj["t"]))
            except Exception:
                pass

    def _on_double_click(self, row, _col):
        self._begin_edit(row)

    def _begin_add(self):
        self._editing_obj = None
        self._mode_lbl.setText("新增模式（保存后追加）")
        self.ed_who.clear()
        self.ed_content.clear()
        try:
            self.spin_t.setValue(round(float(getattr(self.wb, "_t", 0.0)), 1))
        except Exception:
            self.spin_t.setValue(0.0)
        self.spin_dur.setValue(3.0)

    def _begin_edit(self, row=None):
        if row is None:
            row = self._selected_row()
        obj = self._row_obj(row)
        if obj is None:
            return False
        self._editing_obj = obj
        self._mode_lbl.setText("编辑模式（保存=原位替换）")
        self.ed_who.setText(str(obj.get("who") or ""))
        self.ed_content.setPlainText(str(obj.get("text") or ""))
        self.spin_t.setValue(round(float(obj["t"]), 1))
        self.spin_dur.setValue(round(float(obj["dur"]), 1))
        return True

    def _save(self):
        who = self.ed_who.text()
        text = self.ed_content.toPlainText()
        t = self.spin_t.value()
        dur = self.spin_dur.value()
        raw = {"t": t, "dur": dur, "who": who, "text": text}
        cleaned = self._valid(raw)
        if cleaned is None:
            # 添加时空 text 拒收；其它脏值也拒
            if not text.strip():
                self._warn("台词内容不能为空")
            else:
                self._warn("台词字段非法（t/dur 需非负、who/text 需字符串）")
            return False
        if self._editing_obj is None:
            return self.add_row(who, text, dur, t)
        return self.update_row(self._editing_obj, who, text, dur, t)

    def add_row(self, who, text, dur, t):
        """新增台词：写 proj['dialogue'] -> push_history -> render。"""
        cleaned = self._valid({"t": t, "dur": dur, "who": who, "text": text})
        if cleaned is None or not str(text).strip():
            self._warn("台词内容不能为空")
            return False
        self._dialogue().append(dict(cleaned))
        self._after_write()
        return True

    def update_row(self, obj, who, text, dur, t):
        """原位替换：在 proj['dialogue'] 中按引用定位并替换。"""
        cleaned = self._valid({"t": t, "dur": dur, "who": who, "text": text})
        if cleaned is None or not str(text).strip():
            self._warn("台词内容不能为空")
            return False
        dlg = self._dialogue()
        idx = self._index_of(dlg, obj)
        if idx < 0:
            return False
        dlg[idx] = dict(cleaned)
        self._editing_obj = None
        self._after_write()
        return True

    def delete_selected(self):
        row = self._selected_row()
        obj = self._row_obj(row)
        if obj is None:
            return False
        dlg = self._dialogue()
        idx = self._index_of(dlg, obj)
        if idx < 0:
            return False
        del dlg[idx]
        self._editing_obj = None
        self._after_write()
        return True

    @staticmethod
    def _index_of(lst, obj):
        for i, x in enumerate(lst):
            if x is obj:
                return i
        return -1

    def _after_write(self):
        try:
            self.wb.push_history()
        except Exception:
            pass
        try:
            self.wb.render_frame(self.wb._t)
        except Exception:
            pass
        self.refresh()

    def _warn(self, msg):
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.warning("剧情编辑", msg, parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
        except Exception:
            pass

    def seek_row_index(self, row):
        """供测试/外部：按行下标 seek（点行联动）。"""
        self._seek_row(row)


# ================= F2-L5（v3.5）：分支编辑区 =================
class _BranchPanel(QWidget):
    """当前幕的分支编辑区（选择点 / 条件跳转 / 幕尾续接）。

    只在多幕工程（v2）出现；单幕工程整块隐藏 -> 既有 UI 零 diff。
    编辑的数据挂在**幕**上（choices/jumps/next），写路径经
    video_model._sanitize_* 清洗，保证与 L1 的契约一致。
    """

    def __init__(self, wb, parent=None):
        super().__init__(parent)
        self.wb = wb
        self._rows = []          # 当前幕的 choice 原对象引用

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(6)

        head = QHBoxLayout()
        self.lbl = StrongBodyLabel("分支", self)
        head.addWidget(self.lbl)
        head.addStretch(1)
        self.btn_add_choice = PushButton("添加选择点", self)
        self.btn_add_choice.setFixedHeight(26)
        self.btn_add_choice.clicked.connect(self._add_choice)
        head.addWidget(self.btn_add_choice)
        self.btn_del_choice = PushButton("删除选中选择点", self)
        self.btn_del_choice.setFixedHeight(26)
        self.btn_del_choice.clicked.connect(self._del_choice)
        head.addWidget(self.btn_del_choice)
        lay.addLayout(head)

        # ---- 选择点表 ----
        # v3.5 实测：本面板挂在**左列**（素材库列，默认 300px / 最小 180px），
        # 一行塞 5 列 + 5 行表单会把每个控件压到几十像素、文本全变 "..."，
        # 面板自身 minSizeHint 505 > 可用宽度 321 还会被硬裁。
        # 故表格列宽全部 Interactive 定死窄值（选中行靠下方表单看全），
        # 表单/跳转/续接一律纵向堆叠 —— 任何宽度 >=180 都能正常用。
        self.tbl = QTableWidget(0, 5, self)
        self.tbl.setHorizontalHeaderLabels(
            ("提示", "选项文本", "→幕", "副作用", "条件"))
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl.setTextElideMode(Qt.ElideRight)
        # 至少露出 3 行（表头 24 + 3×24）：左列高度紧张，靠 setMinimumHeight
        # 向父布局要空间，否则被压到 1 行，多选项时看不见有几条。
        self.tbl.setMinimumHeight(100)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setStyleSheet(_PANEL_QSS)
        self.tbl.setFont(_fluent_font(self.tbl, 12))
        hh = self.tbl.horizontalHeader()
        hh.setMinimumSectionSize(36)
        for col, w in ((0, 52), (2, 44), (3, 62), (4, 52)):
            hh.setSectionResizeMode(col, QHeaderView.Interactive)
            self.tbl.setColumnWidth(col, w)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl.cellClicked.connect(self._on_click)
        lay.addWidget(self.tbl, 1)

        # ---- 选择点表单（纵向，编辑选中的那一条）----
        # 每行控件压到 28px：左列高度有限（约 430px 给整个分支区），
        # 不压的话表格只剩 1 行可见（实测 2 个选项看不全）。
        _ED_H = 28

        def _row(label, widget):
            r = QHBoxLayout()
            r.setSpacing(6)
            lbl = CaptionLabel(label, self)
            lbl.setFixedWidth(58)
            r.addWidget(lbl)
            try:
                widget.setFixedHeight(_ED_H)
            except Exception:
                pass
            r.addWidget(widget, 1)
            return r

        self.ed_prompt = LineEdit(self)
        self.ed_prompt.setPlaceholderText("提示语（可选，如「你的选择？」）")
        lay.addLayout(_row("提示语", self.ed_prompt))
        self.ed_opt_text = LineEdit(self)
        self.ed_opt_text.setPlaceholderText("选项文本（不能为空）")
        lay.addLayout(_row("选项文本", self.ed_opt_text))
        tr = QHBoxLayout()
        tr.setSpacing(6)
        lbl = CaptionLabel("目标幕", self)
        lbl.setFixedWidth(58)
        tr.addWidget(lbl)
        self.cmb_target = ComboBox(self)
        self.cmb_target.setPlaceholderText("选择目标幕")
        self.cmb_target.setFixedHeight(_ED_H)
        tr.addWidget(self.cmb_target, 1)
        self.btn_save = PushButton("保存选项", self)
        self.btn_save.setFixedHeight(_ED_H)
        self.btn_save.clicked.connect(self._save_option)
        tr.addWidget(self.btn_save)
        lay.addLayout(tr)
        self.ed_set = LineEdit(self)
        self.ed_set.setPlaceholderText("副作用，形如 route=alone;met=1")
        lay.addLayout(_row("副作用", self.ed_set))
        self.ed_when = LineEdit(self)
        self.ed_when.setPlaceholderText("条件，形如 met==1（空=总是）")
        lay.addLayout(_row("条件", self.ed_when))

        # ---- 条件跳转（纵向两行）----
        jr1 = QHBoxLayout()
        jr1.setSpacing(6)
        jl = CaptionLabel("条件跳转", self)
        jl.setFixedWidth(58)
        jr1.addWidget(jl)
        self.spin_jt = DoubleSpinBox(self)
        self.spin_jt.setRange(0.0, 9999.0)
        self.spin_jt.setDecimals(2)
        self.spin_jt.setFixedWidth(66)
        self.spin_jt.setFixedHeight(_ED_H)
        self.spin_jt.setToolTip("跳转判定时刻（秒）")
        jr1.addWidget(self.spin_jt)
        self.ed_jwhen = LineEdit(self)
        self.ed_jwhen.setPlaceholderText("条件，如 flag==1")
        self.ed_jwhen.setFixedHeight(_ED_H)
        jr1.addWidget(self.ed_jwhen, 1)
        lay.addLayout(jr1)
        jr2 = QHBoxLayout()
        jr2.setSpacing(6)
        sp = QWidget(self)
        sp.setFixedWidth(58)
        jr2.addWidget(sp)
        self.cmb_jtarget = ComboBox(self)
        self.cmb_jtarget.setPlaceholderText("跳转目标幕")
        self.cmb_jtarget.setFixedHeight(_ED_H)
        jr2.addWidget(self.cmb_jtarget, 1)
        self.btn_add_jump = PushButton("添加", self)
        self.btn_add_jump.setFixedHeight(_ED_H)
        self.btn_add_jump.setToolTip("把上面这条条件跳转加到当前幕")
        self.btn_add_jump.clicked.connect(self._add_jump)
        jr2.addWidget(self.btn_add_jump)
        self.btn_del_jump = PushButton("删末条", self)
        self.btn_del_jump.setFixedHeight(_ED_H)
        self.btn_del_jump.setToolTip("删掉最后一条条件跳转")
        self.btn_del_jump.clicked.connect(self._del_jump)
        jr2.addWidget(self.btn_del_jump)
        lay.addLayout(jr2)
        self.lbl_jumps = CaptionLabel("", self)
        lay.addWidget(self.lbl_jumps)

        # ---- 幕尾续接 ----
        nr = QHBoxLayout()
        nr.setSpacing(6)
        nl = CaptionLabel("幕尾续接", self)
        nl.setFixedWidth(58)
        nr.addWidget(nl)
        self.cmb_next = ComboBox(self)
        self.cmb_next.setPlaceholderText("（无，播完即停）")
        self.cmb_next.setFixedHeight(_ED_H)
        nr.addWidget(self.cmb_next, 1)
        self.btn_next = PushButton("设置", self)
        self.btn_next.setFixedHeight(_ED_H)
        self.btn_next.setToolTip("本幕播完后自动进入哪一幕")
        self.btn_next.clicked.connect(self._set_next)
        nr.addWidget(self.btn_next)
        lay.addLayout(nr)

    # ---------- 数据 ----------
    def _act(self):
        """当前幕 dict（v2 多幕才有；v1 返回 None）。"""
        try:
            aid = getattr(self.wb, "_cur_aid", None)
            if not aid or not V.is_multi_act(self.wb.proj):
                return None
            return V.find_act(self.wb.proj, aid)
        except Exception:
            return None

    def _act_ids(self):
        try:
            return V.act_ids(self.wb.proj)
        except Exception:
            return []

    def _act_names(self):
        """[(aid, 幕名)] —— 下拉用。"""
        out = []
        try:
            for a in (self.wb.proj.get("acts") or []):
                if isinstance(a, dict):
                    out.append((str(a.get("aid")), str(a.get("name") or a.get("aid"))))
        except Exception:
            pass
        return out

    def _fill_target_combo(self, combo, cur=None):
        """填幕列表下拉（保留当前值）。

        v3.5 实测 bug：cur 为空时原实现保持 index 0 -> 下拉显示
        「幕一（a1）」，可数据其实是 None（幕尾=播完即停 / 尚未选目标），
        用户会误以为已经设成 a1 而直接点保存。空值必须回落 placeholder。
        """
        try:
            combo.blockSignals(True)
            combo.clear()
            for aid, name in self._act_names():
                combo.addItem("%s（%s）" % (name, aid), userData=aid)
            idx = combo.findData(cur) if cur else -1
            combo.setCurrentIndex(idx if idx >= 0 else -1)
            combo.blockSignals(False)
        except Exception:
            pass

    # ---------- 渲染 ----------
    def refresh(self):
        """按当前幕重渲染分支区。"""
        act = self._act()
        if act is None:
            self.setVisible(False)
            return
        self.setVisible(True)
        names = self._act_names()
        self._fill_target_combo(self.cmb_target)
        self._fill_target_combo(self.cmb_jtarget)
        self._fill_target_combo(self.cmb_next, act.get("next"))

        choices = [c for c in (act.get("choices") or [])
                   if isinstance(c, dict)]
        self._rows = choices
        # 一行 = 一个选项（choice 有多个选项时占多行）
        opt_rows = []
        for ci, ch in enumerate(choices):
            for oi, o in enumerate(ch.get("options") or []):
                if isinstance(o, dict):
                    opt_rows.append((ci, oi, ch, o))
        self.tbl.setRowCount(len(opt_rows))
        for i, (ci, oi, ch, o) in enumerate(opt_rows):
            prompt = str(ch.get("prompt") or "") if oi == 0 else ""
            self.tbl.setItem(i, 0, QTableWidgetItem(prompt))
            self.tbl.setItem(i, 1, QTableWidgetItem(str(o.get("text") or "")))
            self.tbl.setItem(i, 2, QTableWidgetItem(str(o.get("target") or "")))
            self.tbl.setItem(i, 3, QTableWidgetItem(
                ";".join(o.get("set") or []) if isinstance(o.get("set"), list)
                else str(o.get("set") or "")))
            self.tbl.setItem(i, 4, QTableWidgetItem(str(o.get("when") or "")))
        self._opt_rows = opt_rows
        self.lbl_jumps.setText("（当前 %d 条跳转）" % len(act.get("jumps") or []))

    def _on_click(self, row, _col):
        """点行 -> 回填表单（编辑那个选项）。"""
        try:
            ci, oi, ch, o = self._opt_rows[row]
        except Exception:
            return
        self._sel = (ci, oi, ch, o)
        self.ed_prompt.setText(str(ch.get("prompt") or ""))
        self.ed_opt_text.setText(str(o.get("text") or ""))
        self._fill_target_combo(self.cmb_target, o.get("target"))
        sv = o.get("set")
        self.ed_set.setText(";".join(sv) if isinstance(sv, list) else str(sv or ""))
        self.ed_when.setText(str(o.get("when") or ""))

    # ---------- 写 ----------
    def _after_write(self):
        try:
            self.wb.push_history()
        except Exception:
            pass
        try:
            self.wb.render_frame(self.wb._t)
        except Exception:
            pass
        self.refresh()

    def _add_choice(self):
        act = self._act()
        if act is None:
            return
        choices = act.setdefault("choices", [])
        if len(choices) >= V.MAX_CHOICES_PER_ACT:
            self._warn("本幕选择点已达上限 %d" % V.MAX_CHOICES_PER_ACT)
            return
        cid = "c%d" % (len(choices) + 1)
        choices.append({"cid": cid, "t": None, "prompt": "",
                        "options": [{"text": "选项1", "target": ""}]})
        self._after_write()

    def _del_choice(self):
        act = self._act()
        if act is None:
            return
        row = self.tbl.currentRow()
        try:
            ci, oi, ch, o = self._opt_rows[row]
        except Exception:
            return
        ch["options"] = [x for i, x in enumerate(ch.get("options") or [])
                         if i != oi]
        if not ch["options"]:
            act["choices"] = [x for x in (act.get("choices") or []) if x is not ch]
        self._after_write()

    def _save_option(self):
        act = self._act()
        if act is None:
            return
        sel = getattr(self, "_sel", None)
        if sel is None:
            self._warn("先在表里点一行选中要编辑的选项")
            return
        ci, oi, ch, o = sel
        o["text"] = self.ed_opt_text.text().strip() or "选项"
        o["target"] = str(self.cmb_target.currentData() or "")
        ch["prompt"] = self.ed_prompt.text().strip()
        sv = self.ed_set.text().strip()
        if sv:
            o["set"] = [x.strip() for x in sv.split(";") if x.strip()]
        else:
            o.pop("set", None)
        w = self.ed_when.text().strip()
        if w:
            o["when"] = w
        else:
            o.pop("when", None)
        self._sanitize_act(act)
        self._after_write()

    def _add_jump(self):
        act = self._act()
        if act is None:
            return
        when = self.ed_jwhen.text().strip()
        tgt = str(self.cmb_jtarget.currentData() or "")
        if not when or not tgt:
            self._warn("条件跳转需要条件与目标幕")
            return
        act.setdefault("jumps", []).append(
            {"t": round(float(self.spin_jt.value()), 3),
             "when": when, "target": tgt})
        self._sanitize_act(act)
        self._after_write()

    def _del_jump(self):
        act = self._act()
        if act is None:
            return
        js = act.get("jumps") or []
        if not js:
            return
        js.pop()
        self._sanitize_act(act)
        self._after_write()

    def _set_next(self):
        act = self._act()
        if act is None:
            return
        tgt = str(self.cmb_next.currentData() or "")
        act["next"] = tgt or None
        self._sanitize_act(act)
        self._after_write()

    def _sanitize_act(self, act):
        """按 video_model 的契约清洗当前幕的分支数据（防脏值进工程）。"""
        try:
            aids = self._act_ids()
            warn = []
            act["choices"] = V._sanitize_choices(act.get("choices"), aids, warn)
            act["jumps"] = V._sanitize_jumps(act.get("jumps"), aids, warn)
        except Exception:
            pass

    def _warn(self, msg):
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.warning("分支编辑", msg, parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
        except Exception:
            pass


# 注：窗口壳（FluentWindow 包装）在 l2d_rig_qt.FluentShellWindow 里做，
# 本模块只出纯 QWidget 工作台，避免双向 import。
