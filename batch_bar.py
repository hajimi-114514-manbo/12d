# -*- coding: utf-8 -*-
"""批量操作工具条（batch_bar.py）。

放在剧情编辑器顶部：用户正看着台词表时，一键做批处理。对标对方工具的
「一键 ID 补全 / 批量加 -next / 自动离场 / 批量滤镜 / 预位表情调整」。

**设计取舍**：不做成一个大对话框，而是常驻工具条 + 按钮直点。理由是
这些操作都是「跑完看结果、不满意就撤销」的模式，多一层确认反而烦；
每个动作都走 push_history()，Ctrl+Z 可回退。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout

from qfluentwidgets import (PushButton, PrimaryPushButton, CaptionLabel,
                            ComboBox, ToolTipFilter, ToolButton)
from qfluentwidgets import FluentIcon as FIF

import ui_theme as TH


class BatchBar(QWidget):
    """剧情编辑器顶部的批量操作条。"""

    # 请求执行某个批处理：name + 可选参数
    run_requested = Signal(str, dict)

    ACTIONS = [
        ("fix_ids", "一键 ID 补全",
         "给缺失/重复的 clip 与轨道 id 补齐。\n"
         "clip id 全工程唯一、轨道 id 幕内唯一——\n"
         "重复会让「查找 clip」命中错误的幕。"),
        ("add_next", "批量加 -next",
         "给连续台词加「播完自动继续」。\n"
         "只处理句间隔 ≤ 2.5 秒的连续句段；\n"
         "故意等玩家的停顿不会被动。"),
        ("auto_exit", "自动离场",
         "角色说完最后一句后，把它的立绘 clip\n"
         "收尾到「最后一句 + 0.3 秒」，避免人物\n"
         "一直杵在画面上不动。"),
        ("retime", "台词按字数调时",
         "按「约 5.5 字/秒」重算每句时长。\n"
         "默认只延长不缩短（已有的长句可能是\n"
         "故意留白），保证读得完。"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(6)

        title = CaptionLabel("批量", self)
        title.setStyleSheet("color:%s; font-size:11px; font-weight:600;"
                            % TH.t("text_hi"))
        lay.addWidget(title)

        self._btns = {}
        for name, label, tip in self.ACTIONS:
            b = PushButton(label, self)
            b.setFixedHeight(26)
            b.setToolTip(tip)
            b.installEventFilter(ToolTipFilter(b, showDelay=300))
            b.clicked.connect(lambda _=False, n=name: self._fire(n))
            lay.addWidget(b)
            self._btns[name] = b

        lay.addSpacing(10)
        # 滤镜批量：带一个「作用范围」下拉
        self.cmb_scope = ComboBox(self)
        self.cmb_scope.addItems(["全部片段", "只立绘", "只背景"])
        self.cmb_scope.setFixedHeight(26)
        self.cmb_scope.setMinimumWidth(96)
        self.cmb_scope.setToolTip("批量滤镜的作用范围")
        lay.addWidget(self.cmb_scope)
        self.btn_filter = PushButton("批量套当前滤镜", self)
        self.btn_filter.setFixedHeight(26)
        self.btn_filter.setToolTip(
            "把「滤镜」页当前选中的滤镜套到上面范围里的所有片段。\n"
            "没选滤镜时会提示先去滤镜页选一个。")
        self.btn_filter.installEventFilter(ToolTipFilter(self.btn_filter,
                                                         showDelay=300))
        self.btn_filter.clicked.connect(
            lambda: self._fire("apply_filter",
                               {"scope": self.cmb_scope.currentIndex()}))
        lay.addWidget(self.btn_filter)

        self.btn_clear = PushButton("清除全部滤镜", self)
        self.btn_clear.setFixedHeight(26)
        self.btn_clear.setToolTip("把范围内所有片段上的滤镜参数清空。")
        self.btn_clear.clicked.connect(
            lambda: self._fire("clear_filter",
                               {"scope": self.cmb_scope.currentIndex()}))
        lay.addWidget(self.btn_clear)

        lay.addStretch(1)

        self.lbl = CaptionLabel("", self)
        self.lbl.setStyleSheet("color:%s; font-size:11px;" % TH.t("text_dim"))
        lay.addWidget(self.lbl)

    def _fire(self, name, extra=None):
        self.run_requested.emit(name, extra or {})

    def report(self, text, ok=True):
        """显示执行结果（由工作台回填）。"""
        color = TH.t("text_hi") if ok else "#c0563a"
        self.lbl.setStyleSheet("color:%s; font-size:11px;" % color)
        self.lbl.setText(text)