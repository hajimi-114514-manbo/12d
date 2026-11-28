# -*- coding: utf-8 -*-
"""命令面板（Ctrl+K）：打字搜索 + 模糊匹配 + 回车执行。

为什么值得做：
  · 素材库有 12 个角色 / 41 个改模作者 / 17 个滤镜 / 206 个特效 /
    1053 张背景 —— 靠鼠标翻找很慢，一个命令面板能省掉大量点击；
  · 也顺带把「不知道这个功能在哪」的问题解决掉（打字就能找到）。

匹配算法用的是 **子序列模糊匹配**（fzf / VS Code 那种）：
  输入 "hl" 能命中 "高松灯"，输入 "ffx" 能命中 "电影感滤镜"。
  打分规则（从高到低）：
    1. 完全相等（忽略大小写）
    2. 前缀匹配
    3. 子序列匹配，命中位置越靠前、越连续，分数越高
    4. 全部字符连续出现的（子串）比跳着匹配更优
"""
import contextlib
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"D:\work\puppet\muppet_studio")

from PySide6.QtCore import Qt, QEvent, Signal  # noqa: E402
from PySide6.QtGui import QKeySequence, QShortcut  # noqa: E402
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,  # noqa: E402
                               QLineEdit, QFrame, QApplication)

from qfluentwidgets import (BodyLabel, CaptionLabel, StrongBodyLabel,  # noqa: E402
                            FluentIcon)

import ui_theme as TH  # noqa: E402


# ==================== 模糊匹配 ====================

# 拼音首字母表（只覆盖素材库实际用到的字）。
# 为什么需要：角色名全是汉字（高松灯 / 千早爱音），用户下意识会打
# "gst" 而不是"高松灯"。没有这张表，"gst" 一个都匹配不上。
# 不引第三方拼音库 —— 保持零依赖，且只需覆盖素材库用字 + 常用字。
_PINYIN = {
    "高": "g", "松": "s", "灯": "d", "千": "q", "早": "z", "爱": "a", "音": "y",
    "长": "c", "崎": "q", "素": "s", "世": "s", "椎": "z", "名": "m", "立": "l",
    "希": "x", "要": "y", "乐": "l", "奈": "n", "丰": "f", "川": "c", "祥": "x",
    "子": "z", "若": "r", "叶": "y", "睦": "m", "三": "s", "角": "j", "初": "c",
    "华": "h", "八": "b", "幡": "f", "海": "h", "铃": "l", "祐": "y", "天": "t",
    "寺": "s", "若": "r", "麦": "m", "纯": "c", "田": "t", "真": "z", "希": "x",
    "夜": "y", "早": "z", "北": "b", "松": "s", "月": "y", "水": "s", "港": "g",
    "灯": "d", "极": "j", "矢": "s", "铜": "t", "紫": "z", "皇": "h", "明": "m",
    "暖": "n", "麦": "m", "寺": "s", "他": "t", "依": "y", "其": "q", "八": "b",
    "三世": "ss", "叶": "y", "角": "j", "铃": "l",
}


def _pinyin_initials(text):
    """取每个汉字的拼音首字母（非汉字原样保留）。

    "高松灯" -> "gst"
    """
    out = []
    for ch in str(text or ""):
        p = _PINYIN.get(ch)
        if p:
            out.append(p)
        else:
            out.append(ch.lower())
    return "".join(out)


def fuzzy_score(query, text):
    """子序列模糊匹配打分。命中返回分数（越大越好），不命中返回 None。

    例：fuzzy_score("hl", "高松灯") -> 有分；("zz", "高松灯") -> None

    评分思路（VS Code / fzf 同款）：
      · 完全相等          1000
      · 前缀匹配          900 - 长度差
      · 首字母/连续子串     700
      · 一般子序列匹配      500 - 跳过的字数 - 分散度

    中文额外走**拼音首字母**通道："gst" 命中 "高松灯"。
    """
    if not query:
        return 1.0
    q = query.lower()
    t = text.lower()
    if q == t:
        return 1000.0
    # 拼音首字母通道（汉字名专用）："gsd" 命中 "高松灯"
    py = _pinyin_initials(text)
    if py and py != t:
        if q == py:
            # 拼音完全相等是很强的信号（"gsd" 唯一对应"高松灯"），给最高分
            return 950.0
        if py.startswith(q):
            return 880.0 - (len(py) - len(q)) * 0.1
        if q in py:
            return 680.0 - py.index(q) * 2.0
    if t.startswith(q):
        return 900.0 - (len(t) - len(q)) * 0.1
    # 子串
    pos = t.find(q)
    if pos >= 0:
        return 700.0 - pos * 2.0 - (len(t) - len(q)) * 0.1
    # 子序列
    qi = 0
    hits = []
    for ti, ch in enumerate(t):
        if qi < len(q) and ch == q[qi]:
            hits.append(ti)
            qi += 1
            if qi >= len(q):
                break
    if qi < len(q):
        return None
    # 分散度：命中位置之间的跳空越大越差
    spread = hits[-1] - hits[0] - (len(hits) - 1)
    first = hits[0]
    return 500.0 - first * 3.0 - spread * 1.5


def fuzzy_filter(query, items, key=lambda x: x):
    """按模糊匹配排序过滤 items。返回 [(score, item)]，分数高的在前。

    key(item) 返回**用于匹配的文本**（命令面板里传的是去掉「角色：」这类
    前缀的核心词）—— 前缀不该参与匹配，否则搜「gsd」会被「模型：」
    的拼音前缀干扰。
    """
    if not query:
        return [(1000.0 - len(str(key(i))), i) for i in items]
    out = []
    for it in items:
        s = fuzzy_score(query, str(key(it)))
        if s is not None:
            out.append((s, it))
    out.sort(key=lambda x: (-x[0], str(key(x[1]))))
    return out


# ==================== 面板 UI ====================

class _CmdRow(QFrame):
    """命令面板的一行：标题 + 右侧分类标签。"""

    def __init__(self, title, hint="", group="", parent=None):
        super().__init__(parent)
        self.setObjectName("cmdRow")
        self.setFixedHeight(34)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(8)
        self.lbl = BodyLabel(title, self)
        self.lbl.setStyleSheet("color:%s; font-size:13px; background:transparent;"
                               % TH.t("text_hi"))
        lay.addWidget(self.lbl, 1)
        self.group_lbl = CaptionLabel(group, self)
        self.group_lbl.setStyleSheet(
            "color:%s; font-size:11px; background:transparent;"
            % TH.t("text_dim"))
        lay.addWidget(self.group_lbl)
        self.hint = hint
        self._selected = False
        self._restyle()

    def setText(self, title, hint="", group=""):
        """复用时更新内容（不重建 widget）。"""
        self.lbl.setText(str(title))
        self.group_lbl.setText(str(group))
        self.group_lbl.setVisible(bool(group))
        self.hint = hint

    def _restyle(self):
        if self._selected:
            self.setStyleSheet(
                "QFrame#cmdRow{background:%s;border-radius:6px;}" % TH.t("accent_soft"))
        else:
            self.setStyleSheet("QFrame#cmdRow{background:transparent;}")

    def setSelected(self, sel):
        if self._selected == sel:
            return
        self._selected = sel
        self._restyle()


class CommandPalette(QWidget):
    """命令面板浮层：输入框 + 结果列表。"""

    RENDER_LIMIT = 60        # 最多渲染多少行（见 _render 的说明）

    invoked = Signal(object)          # 触发选中的条目
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("cmdPalette")
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._rows = []
        self._pool = []                # 复用的行 widget（见 _render）
        self._more_lbl = None          # 「还有 N 条」提示
        self._stretch = None           # 末尾留白（只加一次）
        self._items = []               # 当前过滤后的条目
        self._all = []                 # 原始条目（过滤的基准，见 _on_text）
        self._sel = 0
        self._built = False
        self._closing = False        # 防 close_panel <-> focusOut 递归

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.frame = QFrame(self)
        self.frame.setObjectName("cmdFrame")
        self.frame.setStyleSheet(
            "QFrame#cmdFrame{background:%s;border-radius:10px;"
            "border:1px solid %s;}" % (TH.t("panel_bg"), TH.t("panel_border")))
        fl = QVBoxLayout(self.frame)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)

        # 顶部：搜索框
        top = QWidget(self.frame)
        tl = QHBoxLayout(top)
        tl.setContentsMargins(14, 10, 14, 8)
        tl.setSpacing(8)
        ic = QLabel(top)
        ic.setPixmap(FluentIcon.SEARCH.icon().pixmap(16, 16))
        tl.addWidget(ic)
        self.edit = QLineEdit(top)
        self.edit.setPlaceholderText("输入命令…（Ctrl+K 打开）")
        self.edit.setStyleSheet(
            "QLineEdit{color:%s;background:transparent;border:none;"
            "font-size:15px;}" % TH.t("text_hi"))
        self.edit.textChanged.connect(self._on_text)
        tl.addWidget(self.edit, 1)
        fl.addWidget(top)

        self.list_host = QWidget(self.frame)
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(6, 0, 6, 8)
        self.list_layout.setSpacing(1)
        fl.addWidget(self.list_host)

        outer.addWidget(self.frame)
        self.setFixedWidth(560)

    # ---------- 构建 ----------
    def set_items(self, items):
        """items: [(title, hint, group, payload)] 或
        [(title, hint, group, payload, search_key)]

        search_key 是**用于匹配的核心词**（不含「角色：」这类前缀）。
        """
        self._all = list(items)
        self._items = list(items)
        self._sel = 0
        self._render()

    @staticmethod
    def _match_key(item):
        """取条目的匹配键：优先 search_key（第 5 项），否则用标题。"""
        if len(item) >= 5 and item[4]:
            return item[4]
        return item[0]

    def _render(self):
        """渲染结果列表。

        **用 widget 池复用，不反复创建/销毁**（两个原因）：
          1. 崩溃：deleteLater() 是延迟的，若工作台先被销毁，那些 pending
             事件会打到已失效的 C++ 对象上 -> access violation（实测
             退出时崩）。改成复用就没有 pending 删除。
          2. 性能：每敲一个键都要建 60 个 QWidget，池化后只改文字。
        """
        limit = min(len(self._items), self.RENDER_LIMIT)
        while len(self._pool) < limit:
            r = _CmdRow("", "", "", self.list_host)
            self._pool.append(r)
            self.list_layout.addWidget(r)
        for i, r in enumerate(self._pool):
            if i < limit:
                item = self._items[i]
                r.setText(item[0], item[1], item[2])
                r.setSelected(i == self._sel)
                r.setVisible(True)
            else:
                r.setVisible(False)
        self._rows = self._pool[:limit]
        if len(self._items) > limit:
            if self._more_lbl is None:
                self._more_lbl = CaptionLabel("", self.list_host)
                self._more_lbl.setStyleSheet(
                    "color:%s; font-size:11px; background:transparent;"
                    % TH.t("text_dim"))
                self.list_layout.insertWidget(self.list_layout.count(), self._more_lbl)
            self._more_lbl.setText("  … 还有 %d 条，继续输入以缩小范围"
                                   % (len(self._items) - limit))
            self._more_lbl.setVisible(True)
        elif self._more_lbl is not None:
            self._more_lbl.setVisible(False)
        # 末尾留白：只加一次（每次都 addStretch 会越积越多）
        if self._stretch is None:
            self._stretch = self.list_layout.addStretch(1)
        self._adjust_height()

    def _adjust_height(self):
        n = max(1, len(self._rows))
        h = 64 + min(n, 9) * 36
        self.frame.setFixedHeight(h)
        self.setFixedHeight(h)

    # ---------- 交互 ----------
    def _on_text(self, text):
        """按输入过滤。

        关键：**必须从原始列表 _all 过滤**，不能从上一次过滤结果 _items
        继续过滤 —— 否则清空输入后回不到全部（实测清空后仍只剩 1 行）。
        """
        q = text.strip()
        scored = fuzzy_filter(q, self._all, key=self._match_key)
        self._items = [it for _s, it in scored]
        self._sel = 0
        self._render()
        # 高亮第一个
        if self._rows:
            self._rows[0].setSelected(True)

    def _move(self, d):
        if not self._rows:
            return
        n = len(self._rows)          # 只在**已渲染的行**里移动
        old = self._sel
        self._sel = (self._sel + d) % n
        if old != self._sel:
            self._rows[old].setSelected(False)
            self._rows[self._sel].setSelected(True)

    def _accept(self):
        if not self._items:
            return
        item = self._items[self._sel]
        payload = item[3]
        self.close_panel()
        self.invoked.emit(payload)

    def open_panel(self, items=None):
        """打开面板。items 为 None 时用调用方给的 _all_items()。"""
        if items is not None:
            self.set_items(items)
        elif hasattr(self, "_all_items"):
            self.set_items(self._all_items())
        self.edit.clear()
        self._on_text("")
        # 焦点：Popup 窗口默认不抢键盘焦点，不显式 activate + setFocus
        # 的话输入框收不到按键，打字没反应（踩过）。
        self.show()
        self.raise_()
        self.edit.setFocus(Qt.PopupFocusReason)
        self.edit.selectAll()
        # 居中于父窗口
        p = self.parentWidget()
        if p is not None:
            g = p.geometry()
            self.move(g.center().x() - self.width() // 2,
                      g.center().y() - self.height() // 2)

    def close_panel(self):
        if self._closing:
            return
        self._closing = True
        try:
            self.hide()
            self.closed.emit()
        finally:
            self._closing = False

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key_Escape,):
            self.close_panel()
            return
        if k in (Qt.Key_Return, Qt.Key_Enter):
            self._accept()
            return
        if k == Qt.Key_Down:
            self._move(1)
            return
        if k == Qt.Key_Up:
            self._move(-1)
            return
        super().keyPressEvent(e)
