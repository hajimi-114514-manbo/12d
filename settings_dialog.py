# -*- coding: utf-8 -*-
"""设置对话框：快捷键自定义 + 界面主题切换。

入口：「编辑 → 设置…」（木偶工程编辑器和视频工作台各有一个）。
两页：
  · 快捷键 —— 按分组列出当前窗口的快捷键，点键位按钮进入录入态，
    按下新键立即生效并写 shortcuts.json；同窗口内撞键直接拒绝。
  · 外观   —— 浅色（半透明磨砂）/ 深色，选中立即调用 ui_theme.set_mode，
    与「文件 → 界面主题」是同一份配置。

为什么用 MessageBoxBase：项目里其它对话框（导出参数、未保存退出）都是
这个基类，遮罩 + 居中面板 + 底部按钮的行为已经和整体风格一致；
设置面板比它们宽（最小 680），内容多的快捷键页放进 ScrollArea。
"""
import contextlib

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget

from qfluentwidgets import (MessageBoxBase, PushButton, RadioButton,
                            CaptionLabel, BodyLabel, StrongBodyLabel,
                            SegmentedWidget, ScrollArea,
                            InfoBar, InfoBarPosition)

import ui_theme as TH
import shortcut_prefs as SP


# 只按修饰键时不触发录入（用户可能正按着 Ctrl 准备组合键）
_MODIFIER_KEYS = {
    Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta,
    Qt.Key_AltGr, Qt.Key_CapsLock, Qt.Key_NumLock, Qt.Key_ScrollLock,
    Qt.Key_Super_L, Qt.Key_Super_R,
}


class _KeyButton(PushButton):
    """键位按钮：点一下进入录入态，再按一个键就改键位。"""

    captured = Signal(str)

    _active = None            # 全局只允许一个按钮处于录入态

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self._capturing = False
        self._before = text
        self.setText(text)
        self.setFixedWidth(150)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setToolTip("点击后按下新键（Esc 取消）")
        self.clicked.connect(self.start_capture)

    # ---------- 录入态 ----------
    def start_capture(self):
        if _KeyButton._active is not None and _KeyButton._active is not self:
            with contextlib.suppress(Exception):
                _KeyButton._active._stop_capture()
        _KeyButton._active = self
        self._capturing = True
        self._before = self.text()
        self.setText("按下新键…")
        self.setStyleSheet("PushButton{border:1px solid %s;}" % TH.t("accent"))
        self.setFocus(Qt.MouseFocusReason)
        # 抓键盘：即使焦点被遮罩层抢走，按键也先送到这里
        with contextlib.suppress(Exception):
            self.grabKeyboard()

    def _stop_capture(self, restore=True):
        if not self._capturing:
            return
        self._capturing = False
        with contextlib.suppress(Exception):
            self.releaseKeyboard()
        if restore:
            self.setText(self._before)
        self.setStyleSheet("")
        if _KeyButton._active is self:
            _KeyButton._active = None

    def cancel_capture(self):
        self._stop_capture(restore=True)

    # ---------- 事件 ----------
    def keyPressEvent(self, e):
        if not self._capturing:
            return super().keyPressEvent(e)
        if e.isAutoRepeat():
            return
        k = e.key()
        if k in _MODIFIER_KEYS:
            return                       # 纯修饰键：继续等主键
        if k == Qt.Key_Escape:
            self._stop_capture(restore=True)
            return
        seq = QKeySequence(e.keyCombination()).toString(
            QKeySequence.PortableText)
        if not seq:
            return
        self._stop_capture(restore=False)
        self.captured.emit(seq)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        if self._capturing:
            self._stop_capture(restore=True)


class SettingsDialog(MessageBoxBase):
    """设置面板：快捷键 / 外观。"""

    def __init__(self, parent, scope="rig"):
        super().__init__(parent)
        self.scope = scope
        self._key_buttons = []        # [(ShortcutItem, _KeyButton)]
        self.widget.setMinimumWidth(680)

        self.viewLayout.setSpacing(10)
        self.viewLayout.addWidget(StrongBodyLabel("设置", self))
        self.viewLayout.addWidget(CaptionLabel(
            "快捷键与界面主题会立即生效并记住，下次启动沿用。", self))

        # ---- 分段切换 ----
        self.seg = SegmentedWidget(self)
        self.seg.addItem("keys", "快捷键")
        self.seg.addItem("theme", "外观")
        self.viewLayout.addWidget(self.seg)

        # ---- 内容页 ----
        self.stack = QStackedWidget(self)
        self.page_keys = self._build_keys_page()
        self.page_theme = self._build_theme_page()
        self.stack.addWidget(self.page_keys)
        self.stack.addWidget(self.page_theme)
        self.stack.setMinimumHeight(420)
        self.viewLayout.addWidget(self.stack, 1)

        self.seg.setCurrentItem("keys")
        self.seg.currentItemChanged.connect(self._on_seg_changed)

        # ---- 底部按钮：完成（取消按钮隐藏，点遮罩即关闭）----
        self.yesButton.setText("完成")
        self.hideCancelButton()

    # ---------- 快捷键页 ----------
    def _build_keys_page(self):
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        area = ScrollArea(page)
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        ilay = QVBoxLayout(inner)
        ilay.setContentsMargins(2, 2, 10, 2)
        ilay.setSpacing(4)

        items = SP.REGISTRY.items(self.scope)
        if not items:
            ilay.addWidget(CaptionLabel("当前窗口没有可改键位的快捷键。"))
        last_group = None
        for it in items:
            if it.group != last_group:
                last_group = it.group
                cap = CaptionLabel(last_group)
                cap.setStyleSheet(
                    "color:%s;font-size:11px;background:transparent;"
                    "padding-top:6px;" % TH.t("text_dim"))
                ilay.addWidget(cap)
            row = QWidget(inner)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(4, 2, 4, 2)
            rl.setSpacing(8)
            rl.addWidget(BodyLabel(it.label, row), 1)
            btn = _KeyButton(SP.REGISTRY.key_of(self.scope, it.sid), row)
            btn.captured.connect(
                lambda seq, _it=it: self._on_key_captured(_it, seq))
            rl.addWidget(btn, 0, Qt.AlignRight)
            ilay.addWidget(row)
            self._key_buttons.append((it, btn))
        ilay.addStretch(1)
        area.setWidget(inner)
        lay.addWidget(area, 1)

        # 底部：说明 + 恢复默认
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        self.lbl_tip = CaptionLabel(
            "点击键位后按新键即可修改，Esc 取消录入"
            "（Esc 本身只能通过「恢复默认」还原）。", page)
        self.lbl_tip.setStyleSheet(
            "color:%s;background:transparent;" % TH.t("text_dim"))
        bottom.addWidget(self.lbl_tip, 1)
        self.btn_reset = PushButton("恢复默认", page)
        self.btn_reset.clicked.connect(self._on_reset)
        bottom.addWidget(self.btn_reset, 0, Qt.AlignRight)
        lay.addLayout(bottom)
        return page

    def _on_key_captured(self, item, seq):
        ok, msg = SP.REGISTRY.set_key(self.scope, item.sid, seq)
        if not ok:
            InfoBar.warning(
                "键位冲突",
                "「%s」与「%s」冲突，换一个键位。" % (item.label, msg),
                parent=self, position=InfoBarPosition.TOP, duration=3000)
            self._sync_key_texts()
            return
        self._sync_key_texts()
        InfoBar.success("已改键位", "%s → %s" % (item.label, seq),
                        parent=self, position=InfoBarPosition.TOP,
                        duration=2000)

    def _sync_key_texts(self):
        for it, btn in self._key_buttons:
            if not getattr(btn, "_capturing", False):
                btn.setText(SP.REGISTRY.key_of(self.scope, it.sid))

    def _on_reset(self):
        SP.REGISTRY.reset(self.scope)
        self._sync_key_texts()
        InfoBar.success("已恢复默认", "本窗口的快捷键已全部回到默认值。",
                        parent=self, position=InfoBarPosition.TOP,
                        duration=2000)

    # ---------- 外观页 ----------
    def _build_theme_page(self):
        page = QWidget(self)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 8, 4, 0)
        lay.setSpacing(10)
        lay.addWidget(BodyLabel("界面主题", page))
        self.rb_light = RadioButton("浅色（半透明磨砂）", page)
        self.rb_dark = RadioButton("深色", page)
        for rb in (self.rb_light, self.rb_dark):
            rb.setMinimumHeight(32)
            lay.addWidget(rb)
        (self.rb_dark if TH.mode() == "dark" else self.rb_light).setChecked(True)
        self.rb_light.clicked.connect(lambda: self._set_theme("light"))
        self.rb_dark.clicked.connect(lambda: self._set_theme("dark"))
        self.lbl_theme_hint = CaptionLabel(
            "主题切换立即生效，与「文件 → 界面主题」是同一份配置。"
            "深色更适合长时间剪辑，浅色磨砂更接近纸面。", page)
        self.lbl_theme_hint.setWordWrap(True)
        self.lbl_theme_hint.setStyleSheet(
            "color:%s;background:transparent;" % TH.t("text_dim"))
        lay.addWidget(self.lbl_theme_hint)
        lay.addStretch(1)
        return page

    def _set_theme(self, mode):
        if mode == TH.mode():
            return
        TH.set_mode(mode)
        self._refresh_colors()

    def _refresh_colors(self):
        """切主题后重设显式指定过颜色的文字（QSS 管不到的 self.styleSheet）。"""
        dim = "color:%s;background:transparent;" % TH.t("text_dim")
        self.lbl_tip.setStyleSheet(dim)
        self.lbl_theme_hint.setStyleSheet(dim)
        for cap in self.page_keys.findChildren(CaptionLabel):
            cap.setStyleSheet(
                "color:%s;font-size:11px;background:transparent;"
                "padding-top:6px;" % TH.t("text_dim"))
        for it, btn in self._key_buttons:
            if not getattr(btn, "_capturing", False):
                btn.setStyleSheet("")

    # ---------- 分段切换 ----------
    def _on_seg_changed(self, route):
        if _KeyButton._active is not None:
            _KeyButton._active.cancel_capture()
        self.stack.setCurrentIndex(0 if route == "keys" else 1)

    def reject(self):
        if _KeyButton._active is not None:
            _KeyButton._active.cancel_capture()
        super().reject()


def open_settings(parent, scope="rig"):
    """打开设置对话框（模态）。parent 建议传所在顶层窗口。"""
    dlg = SettingsDialog(parent, scope)
    dlg.exec()
    return dlg