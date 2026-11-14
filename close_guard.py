# -*- coding: utf-8 -*-
"""B1 未保存退出确认对话框（三按钮：保存 / 确认退出 / 取消）。

纯 UI + 纯逻辑拆分，**import 无副作用**（不创建 QApplication、不弹窗）。
接线（closeEvent 调 decide）由调用方负责，本模块只提供能力。

返回值约定（decide / handle_choice 共用）：
    "exit"      —— 允许退出（无未保存内容，或用户确认退出）
    "cancel"    —— 不退出（用户取消 / 关闭 / 保存失败）
    "save_exit" —— 已保存并退出（用户点保存且 do_save 返回 True）
"""
from qfluentwidgets import (MessageBoxBase, PushButton, StrongBodyLabel)
from PySide6.QtWidgets import QWidget

EXIT = "exit"
CANCEL = "cancel"
SAVE_EXIT = "save_exit"


def handle_choice(which, do_save):
    """纯逻辑：三按钮选择 -> 结果字符串（不碰 UI，便于单测）。

    which: "save" / "exit" / "cancel"
    do_save: 保存回调，仅 "save" 分支在主线程同步调用；
             返回 True 才算保存成功，异常按失败处理。
    返回 "save_exit" / "exit" / "cancel"。
    """
    if which == "exit":
        return EXIT
    if which == "cancel":
        return CANCEL
    # save
    try:
        ok = bool(do_save())
    except Exception:
        ok = False
    return SAVE_EXIT if ok else CANCEL


class UnsavedCloseDialog(MessageBoxBase):
    """「有未保存内容，确认退出?」三按钮对话框。

    choice 记录用户选择：save / exit / cancel（默认 cancel，覆盖 Esc/关窗）。
    """

    def __init__(self, parent=None):
        fallback = None
        if parent is None:
            # MessageBoxBase 需要非 None 父窗口（会读 parent.width()）
            fallback = QWidget()
            parent = fallback
        super().__init__(parent)
        self._fallback_parent = fallback
        self.choice = CANCEL
        self.viewLayout.addWidget(StrongBodyLabel("有未保存内容，确认退出?"))

        # 三按钮：保存(yesButton) / 确认退出(自建) / 取消(cancelButton)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.btn_exit = PushButton("确认退出", self.buttonGroup)
        self.btn_exit.setObjectName("exitButton")
        self.btn_exit.setStyleSheet(
            "#exitButton{background:#b3261e;color:#ffffff;"
            "border-radius:6px;}"
            "#exitButton:hover{background:#d33a31;}"
        )
        # 插到「取消」之前，顺序：保存 | 确认退出 | 取消
        idx = self.buttonLayout.indexOf(self.cancelButton)
        self.buttonLayout.insertWidget(max(0, idx), self.btn_exit)

        # 接管按钮：断开基类默认 accept，保证 choice 与行为一致
        self.yesButton.clicked.disconnect()
        self.cancelButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._on_save)
        self.btn_exit.clicked.connect(self._on_exit)
        self.cancelButton.clicked.connect(self._on_cancel)

        self.widget.setMinimumWidth(420)

    def _on_save(self):
        self.choice = "save"
        self.accept()

    def _on_exit(self):
        self.choice = "exit"
        self.accept()

    def _on_cancel(self):
        self.choice = "cancel"
        self.reject()


def decide(parent, dirty_check, do_save):
    """未保存退出确认。

    dirty_check() 假（或异常）-> 直接返回 "exit"，不弹窗。
    真 -> 弹模态 exec()，按用户选择返回 "save_exit" / "exit" / "cancel"。
    do_save 仅在用户点「保存」时于主线程同步调用（save_project 自带模态），
    返回 True 才 "save_exit"。
    """
    try:
        dirty = bool(dirty_check())
    except Exception:
        dirty = False
    if not dirty:
        return EXIT
    dlg = UnsavedCloseDialog(parent)
    dlg.exec()
    return handle_choice(dlg.choice, do_save)
