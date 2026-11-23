# -*- coding: utf-8 -*-
"""统一的顶部提示条封装。

**为什么单独一个模块**：qfluentwidgets 的 InfoBar 每次都要传
`parent / position / duration` 三个固定参数，项目里 183 处调用全在重复它们，
把业务文字淹没了。这里收成 4 个薄函数，读代码时只剩「标题 + 内容」。

**语义与直接调 InfoBar 完全一致**，不做任何加工：
  ・position 固定 TOP（全项目一致，没有例外）
  ・duration 默认 3000ms，需要的话照传
  ・调用方传 parent（多数传 self），原样透传

不重写 InfoBar，只做参数收敛 —— 出问题时能一行改回原来的写法。
"""
from qfluentwidgets import InfoBar, InfoBarPosition

TOP = InfoBarPosition.TOP
DEFAULT_MS = 3000


def _show(kind, parent, title, msg, duration=DEFAULT_MS, **kw):
    return getattr(InfoBar, kind)(title, msg, parent=parent, position=TOP,
                                  duration=duration, **kw)


def toast_info(parent, title, msg="", duration=DEFAULT_MS, **kw):
    """普通提示。"""
    return _show("info", parent, title, msg, duration, **kw)


def toast_ok(parent, title, msg="", duration=DEFAULT_MS, **kw):
    """成功提示。"""
    return _show("success", parent, title, msg, duration, **kw)


def toast_warn(parent, title, msg="", duration=DEFAULT_MS, **kw):
    """警告：操作没做成，但程序还能继续。"""
    return _show("warning", parent, title, msg, duration, **kw)


def toast_err(parent, title, msg="", duration=DEFAULT_MS, **kw):
    """错误：需要用户注意的失败。"""
    return _show("error", parent, title, msg, duration, **kw)
