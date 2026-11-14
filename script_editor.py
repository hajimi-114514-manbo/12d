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
_COL_T, _COL_WHO, _COL_TEXT, _COL_DUR = 0, 1, 2, 3
_COL_MOTION, _COL_EXPR = 4, 5
_NCOL = 6
_HEADERS = ("时刻(s)", "名字", "内容", "时长(s)", "动作", "表情")


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

    # ---------- 数据 ----------
    def _dialogue(self):
        try:
            d = self.wb.proj.get("dialogue")
        except Exception:
            return []
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
