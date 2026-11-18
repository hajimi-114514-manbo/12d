# -*- coding: utf-8 -*-
"""界面主题：深色 / 浅色（半透明磨砂白）。

设计要点
--------
磨砂走**视觉层**，不调用 DWM/Acrylic：
  · qfluentwidgets 的 FluentWindow 自己画不透明底，实测 Mica/Acrylic/
    SetWindowCompositionAttribute 都被盖住，透不出桌面（probe 已验证）；
  · 真模糊还得逐帧合成，工程一大就掉帧。
所以浅色主题 = 画一层柔和的浅色渐变窗底，再把面板画成半透明白
（rgba 0.66~0.86 的竖向渐变 + 白色描边），底下渐变透上来就是磨砂质感。
零额外开销，任何机器上效果一致。

用法
----
1) 启动时：`ui_theme.apply(app)`（内部 setTheme + 全局 QSS）。
2) 模块自己注册刷新函数：
       import ui_theme as TH
       def _apply_theme():
           w.setStyleSheet(...TH.t("panel_bg")...)
       TH.register(_apply_theme)
   切换主题时 TH 会依次调用所有注册函数，再刷新一遍所有控件。
3) 需要临时取色：`TH.t("text")` 取 QSS 串，`TH.qcolor("text")` 取 QColor。
"""
import io
import json
import os

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

import qfluentwidgets as qw
from qfluentwidgets import Theme, setTheme

# ---------------------------------------------------------------- 主题令牌
# 深色 = 现有的暗色工作台配色（保持原样，老用户零感知）
# 浅色 = 半透明磨砂白
PALETTES = {
    "dark": {
        # 窗口 / 面板
        "window_bg": "#1b1f27",
        "window_flat": "#1b1f27",
        "panel_bg": "rgba(32,35,42,0.94)",
        "panel_border": "rgba(58,63,74,0.9)",
        "card_bg": "#252a33",
        "canvas_bg": "#0d0f13",
        "thumb_bg": "#14161b",
        # 文字
        "text": "#c8d1dc",
        "text_dim": "#8a93a2",
        "text_hi": "#f2f6fa",
        # 强调
        "accent": "#4d88ff",
        "accent_hover": "#6a9bff",
        "split_handle": "#3a3f4a",
        "row_bg": "#242935",
        "row_hover": "#262c38",
        "row_press": "#2a3142",
        "text_mute": "#6b7382",
        # 时间轴
        "tl_bg": "#1b1f27",
        "tl_row_a": "#20252e",
        "tl_row_b": "#1d222a",
        "tl_grid": "#3a4356",
        "tl_lane": "#171b21",
        "tl_head": "#252b35",
        "tl_text": "#c8d1dc",
        "tl_text_hi": "#f2f6fa",
        "tl_tick_major": "#8fa3bd",
        "tl_handle": "#3a3f4a",
        "tl_playhead": "#4d88ff",
        "tl_kf": "#ffd166",
        "tl_kf_sel": "#ff6b6b",
        "tl_wave": "#bfe3cc",
        "tl_snap": "#ffd166",
        "tl_clip_edge": "#10151d",
        "tl_sel_edge": "#f2f6fa",
        "tl_kf_inner": "#3a2f10",
        "tl_link": "#5b6b82",
        "tl_icon": "#f2f6fa",
        "tl_icon_lock": "#ffd166",
        "tl_icon_cut": "#ff7878",
        "tl_icon_hover": 36,
        "tl_locked_row": 18,
        # 剧本编辑器
        "ed_bg": "#1b1e24",
        "ed_border": "#262b34",
        "ed_head": "#23262e",
        "ed_sel": "#2f3d5c",
        "ed_sel_fg": "#f2f6fa",
        "ed_text": "#e8ecf4",
        "ed_text_dim": "#c8d1dc",
        # 欢迎页
        "wel_bg": "#1f2126",
        "wel_side": "#262932",
        "wel_card": "#24272e",
        "wel_card_sel": "#263042",
        "wel_card_border": "#2c3038",
        # 提示气泡
        "tooltip_bg": "#2b3140",
        "tooltip_fg": "#e6e6e6",
        "tooltip_border": "#3c4354",
        # 片段色（深色底上用亮一点的）
        "clip_video": "#3d6dbf",
        "clip_image": "#c97b3d",
        "clip_puppet": "#8a5fc0",
        "clip_audio": "#3fa06b",
    },
    "light": {
        # 窗口底：柔和斜向渐变（磨砂的"底"）。面板是半透明白叠上去，
        # 渐变从面板底下透出来才有层次 —— 纯 Qt 绘制，不依赖 DWM 模糊。
        "window_bg": ("qlineargradient(x1:0,y1:0,x2:1,y2:1,"
                      " stop:0 #c9d6e8, stop:0.35 #e2eaf4,"
                      " stop:0.62 #d6e2f0, stop:1 #cfdcee)"),
        "window_flat": "#dde6f1",
        # 面板：半透明白（alpha 必须够低才看得见底色渐变，否则一片死白）
        # 单一 rgba 纯色：QSS 里 gradient+rgba 与父控件合成不稳，
        # 纯 rgba 才稳定透出底下的窗口渐变（实测验证）
        "panel_bg": "rgba(255,255,255,0.72)",
        "panel_border": "rgba(255,255,255,0.92)",
        "panel_solid": "#eef3f9",
        "card_bg": "rgba(255,255,255,0.55)",
        "canvas_bg": "#dfe6f0",
        "thumb_bg": "#eef1f6",
        # 文字：白底必须够深
        "text": "#2b3240",
        "text_dim": "#5b6577",
        "text_hi": "#11151c",
        # 强调：白底上 #4d88ff 太浅，压深一档
        "accent": "#2f6fe0",
        "accent_hover": "#1f5ccb",
        "split_handle": "#c6ccd8",
        "row_bg": "#ffffff",
        "row_hover": "#f0f4fa",
        "row_press": "#e6edf8",
        "text_mute": "#8b94a6",
        # 时间轴：浅灰底 + 白轨道
        "tl_bg": "#f3f6fb",
        "tl_row_a": "#fdfefe",
        "tl_row_b": "#f5f8fc",
        "tl_grid": "#d8dfea",
        "tl_lane": "#eaeff7",
        "tl_head": "#eef2f9",
        "tl_text": "#48505f",
        "tl_text_hi": "#12161d",
        "tl_tick_major": "#39424f",
        "tl_handle": "#c6ccd8",
        "tl_playhead": "#2f6fe0",
        "tl_kf": "#d99b12",
        "tl_kf_sel": "#d94141",
        "tl_wave": "#2e8b57",
        "tl_snap": "#d99b12",
        "tl_clip_edge": "#ffffff",
        "tl_sel_edge": "#1b1f27",
        "tl_kf_inner": "#fff6df",
        "tl_link": "#9aa5b8",
        "tl_icon": "#141821",
        "tl_icon_lock": "#b07d00",
        "tl_icon_cut": "#c0392b",
        "tl_icon_hover": 30,
        "tl_locked_row": 26,
        # 剧本编辑器
        "ed_bg": "#ffffff",
        "ed_border": "#dde1e9",
        "ed_head": "#eff3f8",
        "ed_sel": "#d7e5ff",
        "ed_sel_fg": "#12161d",
        "ed_text": "#1b1f26",
        "ed_text_dim": "#4a5364",
        # 欢迎页
        "wel_bg": "#f1f4f9",
        "wel_side": "rgba(255,255,255,0.80)",
        "wel_card": "rgba(255,255,255,0.82)",
        "wel_card_sel": "#e3edff",
        "wel_card_border": "#dbe0e9",
        # 提示气泡
        "tooltip_bg": "#ffffff",
        "tooltip_fg": "#1b1f26",
        "tooltip_border": "#d3d9e3",
        # 片段色（白底略压深，避免发飘）
        "clip_video": "#3563ad",
        "clip_image": "#b9682d",
        "clip_puppet": "#7b4fb0",
        "clip_audio": "#33935f",
    },
}

DEFAULT_MODE = "dark"        # 默认沿用原来的暗色，白主题由用户在菜单里选
MODES = ("dark", "light")

_mode = DEFAULT_MODE
_reloaders = []          # 各模块注册的「重刷自己的样式」函数
_prefs_path = None


# ---------------------------------------------------------------- 配置持久化
def _prefs_file():
    global _prefs_path
    if _prefs_path:
        return _prefs_path
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "MuppetStudio")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    _prefs_path = os.path.join(d, "ui_theme.json")
    return _prefs_path


def load_mode():
    """读上次选的主题；文件坏了/没有就回默认。

    注意用 utf-8-sig：带 BOM 的文件（记事本/PowerShell 写的）直接
    utf-8 解会抛 JSONDecodeError，主题就会静默回退默认值。
    """
    for enc in ("utf-8-sig", "utf-8"):
        try:
            with io.open(_prefs_file(), encoding=enc) as f:
                d = json.load(f)
            m = d.get("mode")
            if m in MODES:
                return m
            break
        except Exception:
            continue
    return DEFAULT_MODE


def save_mode(mode):
    try:
        # newline="" + encoding 不带 BOM：文件给别的工具读也干净
        with io.open(_prefs_file(), "w", encoding="utf-8", newline="") as f:
            json.dump({"mode": mode}, f)
    except Exception:
        pass


# ---------------------------------------------------------------- 取色
def mode():
    return _mode


def is_light():
    return _mode == "light"


def palette():
    return PALETTES[_mode]


def t(key):
    """取 QSS 可用的颜色串（可直接塞进 setStyleSheet）。"""
    return PALETTES[_mode][key]


def qcolor(key):
    """取 QColor（供 paintEvent 里画）。支持 rgba(...) 与 #rrggbb。"""
    return to_qcolor(t(key))


def to_qcolor(s):
    s = str(s).strip()
    if s.startswith("rgba(") and s.endswith(")"):
        parts = [p.strip() for p in s[5:-1].split(",")]
        try:
            r, g, b = (int(float(x)) for x in parts[:3])
            a = float(parts[3]) if len(parts) > 3 else 1.0
            if a <= 1.0:
                a = int(round(a * 255))
            else:
                a = int(a)
            return QColor(r, g, b, a)
        except Exception:
            return QColor("#000000")
    if s.startswith("qlineargradient"):
        # 渐变串没法变成单色：退到第一个 stop 的颜色，够用
        i = s.find("#")
        if i >= 0:
            return QColor(s[i:i + 7])
        return QColor("#ffffff")
    return QColor(s)


# ---------------------------------------------------------------- 注册 / 应用
def register(fn):
    """注册一个「重刷样式」回调（模块级），切主题时调用。"""
    if fn not in _reloaders:
        _reloaders.append(fn)


def apply(app=None):
    """启动时调用一次：套上 qfluentwidgets 主题 + 全局 QSS。"""
    global _mode
    _mode = load_mode()
    _set_qt_theme()
    _apply_global_qss(app)
    for fn in list(_reloaders):
        try:
            fn()
        except Exception:
            pass


def set_mode(m, app=None, save=True):
    """切主题：改全局 → 重刷所有注册模块 → 全量重绘。"""
    global _mode
    if m not in MODES or m == _mode:
        return
    _mode = m
    if save:
        save_mode(m)
    _set_qt_theme()
    _apply_global_qss(app)
    for fn in list(_reloaders):
        try:
            fn()
        except Exception:
            pass
    _repaint_all()


def _set_qt_theme():
    try:
        setTheme(Theme.LIGHT if _mode == "light" else Theme.DARK)
    except Exception:
        pass
    # qfluentwidgets 的 setTheme 只推 qconfig，标题栏/导航栏这些库内
    # 自绘控件要重新 apply 各自的样式表才会换色（实测切主题后标题栏
    # 会残留上一个主题的底色）。
    _reapply_library_sheets()


def _reapply_library_sheets():
    """让 qfluentwidgets 的整体样式表按当前主题重刷一遍。"""
    try:
        from qfluentwidgets.common.style_sheet import FluentStyleSheet
        from qfluentwidgets import FluentWindow
        app = QApplication.instance()
        if app is None:
            return
        for w in app.allWidgets():
            if not isinstance(w, FluentWindow):
                continue
            try:
                FluentStyleSheet.FLUENT_WINDOW.apply(w)
            except Exception:
                pass
            for attr in ("titleBar", "navigationInterface"):
                o = getattr(w, attr, None)
                if o is not None:
                    try:
                        o.update()
                        o.repaint()
                    except Exception:
                        pass
    except Exception:
        pass


def _apply_global_qss(app=None):
    app = app or QApplication.instance()
    if app is None:
        return
    # tooltip 固定白底黑字（用户确认「默认白底黑字也可以」）。
    # 为什么不用主题令牌：Qt 原生 QToolTip 不吃 qfluentwidgets 的主题，
    # 深色主题下会出现浅底浅字 / 刺眼白板两种情况，反而更难读。
    # 固定白底黑字在两套主题下都清楚，也不会因切主题而变化。
    app.setStyleSheet(
        "QToolTip { color:#1b1f26; background-color:#ffffff;"
        " border:1px solid #c8cdd6; padding:4px 6px; }")


def _repaint_all():
    """自绘控件不会因 palette 变化自动重画，统一 update 一遍。"""
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            w.update()
        except Exception:
            pass


def toggle():
    set_mode("light" if _mode == "dark" else "dark")
