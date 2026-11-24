# -*- coding: utf-8 -*-
"""视频工作台（v3.0.0）：剪映式布局壳。

    ┌─ Pivot：媒体 | 音频 | 文本* | 滤镜 | 转场* | 模板*（*=敬请期待）─┐
    │ 左素材库      │  中预览 + 播放条            │ 右属性面板        │
    │ [添加素材]    │  ┌────────────────┐        │ 工程：分辨率/帧率  │
    │ 卡片流式网格  │  │   画面 fit      │        │ 时长(只读)        │
    │               │  └────────────────┘        │ 选中 clip：        │
    │               │  [▶] 00:01.2/00:04.0 ─●──  │  起/入/时长 spin  │
    ├───────────────┴────────────────────────────┴──────────────────┤
    │ ClipTimelinePanel（clip 多轨时间轴，clip_timeline.py）          │
    └───────────────────────────────────────────────────────────────┘

第一阶段范围（与用户拍板一致）：
  - 单视频轨 + 单音频轨（可加轨），素材：图片 / mp4 / 音频 / 木偶工程
  - clip 移动/trim/切分/删除/吸附；撤销重做（工程快照栈）
  - 预览**无声**（画面逐帧合成）；导出 mp4 含混音
  - 工程存 projects/<名>/video_project.json
"""
import contextlib
import json
import os
import threading
import time

import numpy as np

from PySide6.QtCore import Qt, QTimer, QSize, QSettings, QEvent, QThread
from PySide6.QtGui import (QImage, QPixmap, QIcon, QKeySequence, QShortcut,
                           QPainter)
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                               QFileDialog, QStackedLayout, QFrame, QSplitter,
                               QMessageBox, QApplication)
from qfluentwidgets import (Pivot, CardWidget, PrimaryPushButton, PushButton,
                            ToolButton, ComboBox, SpinBox, DoubleSpinBox,
                            StrongBodyLabel, BodyLabel, CaptionLabel,
                            LineEdit, ScrollArea, InfoBar, InfoBarPosition,
                            MessageBoxBase, ProgressBar, FluentIcon,
                            FlowLayout, PillPushButton)

import video_model as V
import video_export as VE
import ui_theme as TH
import audio_library as AL
import character_library as CL
import model_thumb as MT
from stage_view import StageView
from script_editor import ScriptEditor
import ui_toast as TOAST

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJ_BASE = os.path.join(_HERE, "projects")


def _recent_file():
    """最近工程文件：写 `.runtime/`，读时新位置优先、回退旧位置。

    与 l2d_rig_qt._runtime_file 同语义，但这里**不能 import 主模块**
    （video_workbench 会被主模块 import，反向依赖成环），故就地实现一份。
    """
    d = os.path.join(_HERE, ".runtime")
    new = os.path.join(d, "recent_projects.json")
    old = os.path.join(_HERE, "recent_projects.json")
    try:
        os.makedirs(d, exist_ok=True)
        return new
    except Exception:
        return old                      # 只读目录等异常时退回根目录


def _recent_read():
    """最近工程读取路径：RECENT_FILE 存在就读它（测试注入优先），
    否则回退根目录旧位置。现算，不预存——预存副本不会跟着 RECENT_FILE 变。"""
    with contextlib.suppress(Exception):
        if os.path.isfile(RECENT_FILE):
            return RECENT_FILE
    new = os.path.join(_HERE, ".runtime", "recent_projects.json")
    old = os.path.join(_HERE, "recent_projects.json")
    if os.path.isfile(new):
        return new
    if os.path.isfile(old):
        return old
    return RECENT_FILE


RECENT_FILE = _recent_file()
META_NAME = "video_project.json"

DEFAULT_DUR = {"image": 3.0, "video": 0, "puppet": 0, "audio": 0}  # 0=用素材全长

# ---------- 面板 splitter（可拖拽调宽高）----------
SETTINGS_NAME = "muppet_workbench.ini"
# 三栏：素材库 | 预览 | 属性
# （v3.6.2 背景改成素材库里的页签，不再是独立栏 —— 用户要求「背景放
#   这个分类里面」，指的是和角色/音频/特效并列的页签）
SPLIT_H_DEFAULT = [300, 880, 260]
SPLIT_H_MIN = [180, 420, 180]
SPLIT_V_DEFAULT = [560, 340]         # 上方区域 | 时间轴（约 38%）
SPLIT_V_MIN_TIMELINE = 120

# handle 颜色跟随主题：idle split_handle / hover+drag accent。
# 用 objectName 限定作用域，避免污染子控件里的其它 QSplitter。
def _inner_bg():
    """内层容器底色。

    浅色主题：transparent —— 半透明白只在最外层面板叠一层，否则
    三层 0.72 alpha 会叠成死白，磨砂渐变全被盖掉。
    深色主题：panel_bg —— 深色面板近不透明，叠多层也不会发白，
    但绝不能透明（透明会露出 Qt 默认白底，深色主题会瞎）。
    """
    return "transparent" if TH.is_light() else TH.t("panel_bg")


def _split_qss():
    h = TH.t("split_handle")
    a = TH.t("accent")
    return (
        "QSplitter#wbHSplit::handle { background: %s; }"
        "QSplitter#wbHSplit::handle:hover, QSplitter#wbHSplit::handle:active"
        " { background: %s; }"
        "QSplitter#wbVSplit::handle { background: %s; }"
        "QSplitter#wbVSplit::handle:hover, QSplitter#wbVSplit::handle:active"
        " { background: %s; }" % (h, a, h, a)
    )


_SPLIT_QSS = _split_qss()

# 存活的 VideoWorkbench 实例，切主题时逐个重刷
_WORKBENCHES = []


def _refresh_all():
    """ui_theme 切主题时回调：重算 SPLIT_QSS 并重刷每个工作台。"""
    global _SPLIT_QSS
    _SPLIT_QSS = _split_qss()
    for wb in list(_WORKBENCHES):
        with contextlib.suppress(Exception): wb._apply_theme()


TH.register(_refresh_all)


def fmt_time(t):
    t = max(0.0, float(t))
    return "%02d:%04.1f" % (int(t // 60), t % 60)


def unique_video_name(base="视频工程"):
    os.makedirs(PROJ_BASE, exist_ok=True)
    n, name = 1, base
    while os.path.exists(os.path.join(PROJ_BASE, name)):
        n += 1
        name = "%s%d" % (base, n)
    return name


class _AssetCard(CardWidget):
    """素材库卡片：缩略图 + 名称 + 时长；双击=加到时间轴。"""

    activated = None  # 用回调即可，不做信号

    def __init__(self, source, thumb_qpixmap=None, on_add=None, parent=None):
        super().__init__(parent)
        self.source = source
        self.on_add = on_add
        self.setFixedSize(112, 132)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(3)
        self.img_lbl = QLabel(self)
        self.img_lbl.setFixedSize(100, 58)
        self.img_lbl.setAlignment(Qt.AlignCenter)
        self.img_lbl.setStyleSheet("background:%s;border-radius:4px;"
                                   "color:%s;" % (TH.t("thumb_bg"),
                                                  TH.t("text_dim")))
        if thumb_qpixmap is not None and not thumb_qpixmap.isNull():
            self.img_lbl.setPixmap(thumb_qpixmap.scaled(
                self.img_lbl.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation))
        else:
            self.img_lbl.setText(source["kind"][:4])
        lay.addWidget(self.img_lbl)
        nm = os.path.basename(str(source["path"]).rstrip("\\/")) or "工程"
        t = BodyLabel(nm[:14], self)
        t.setStyleSheet("color:%s;" % TH.t("text"))
        f = t.font()
        f.setPixelSize(11)
        t.setFont(f)
        lay.addWidget(t)
        d = source.get("src_dur") or 0.0
        cap = CaptionLabel("图片" if source["kind"] == "image"
                           else fmt_time(d), self)
        cap.setStyleSheet("color:%s;" % TH.t("text_dim"))
        lay.addWidget(cap)
        lay.addStretch(1)

    def mouseDoubleClickEvent(self, ev):
        if self.on_add:
            self.on_add(self.source)
        super().mouseDoubleClickEvent(ev)


class _AudioCard(CardWidget):
    """素材库音频卡片：名称 + 时长 + 试听按钮；双击上轨。

    用户要求「直接引用包中的，供用户试听和拖入时间轴」：
      · 卡片数据来自 audio_library（读 asset_index.json），不复制文件；
      · 单击 ▶ 原地试听（再点停止），不打断时间轴播放；
      · 双击 / 点「+」把这条音频作为 source 加到音频轨。
    """

    def __init__(self, item, on_add=None, on_preview=None, parent=None):
        super().__init__(parent)
        self.item = item
        self.on_add = on_add
        self.on_preview = on_preview
        self.setFixedSize(158, 76)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(4)
        nm = BodyLabel(str(item.get("name") or "")[:16], self)
        nm.setStyleSheet("color:%s;" % TH.t("text"))
        f = nm.font()
        f.setPixelSize(11)
        nm.setFont(f)
        nm.setToolTip(str(item.get("file") or ""))
        row.addWidget(nm, 1)
        self.btn_play = ToolButton(self)
        self.btn_play.setIcon(FluentIcon.PLAY.icon())
        self.btn_play.setFixedSize(24, 22)
        self.btn_play.setToolTip("试听")
        self.btn_play.clicked.connect(self._preview)
        row.addWidget(self.btn_play)
        lay.addLayout(row)

        # 第二行：时长 + 类型徽标 + 上轨按钮
        row2 = QHBoxLayout()
        row2.setSpacing(4)
        dur = float(item.get("dur") or 0.0)
        d = CaptionLabel(fmt_time(dur) if dur > 0 else "—", self)
        d.setStyleSheet("color:%s;" % TH.t("text_dim"))
        row2.addWidget(d)
        kind = "BGM" if item.get("kind") == "bgm" else "SFX"
        k = CaptionLabel(kind, self)
        k.setStyleSheet("color:%s;" % TH.t("accent"))
        row2.addWidget(k)
        row2.addStretch(1)
        btn_add = ToolButton(self)
        btn_add.setIcon(FluentIcon.ADD.icon())
        btn_add.setFixedSize(24, 22)
        btn_add.setToolTip("加入音频轨")
        btn_add.clicked.connect(self._add)
        row2.addWidget(btn_add)
        lay.addLayout(row2)

    def _preview(self):
        if self.on_preview:
            self.on_preview(self)

    def _add(self):
        if self.on_add:
            self.on_add(self)

    def set_playing(self, playing):
        """试听状态换个图标，用户能看出哪条在响。"""
        self.btn_play.setIcon(
            (FluentIcon.PAUSE if playing else FluentIcon.PLAY).icon())

    def mouseDoubleClickEvent(self, ev):
        if self.on_add:
            self.on_add(self)
        super().mouseDoubleClickEvent(ev)


class _RoleHeaderCard(CardWidget):
    """角色页的角色标题卡：中文名 + 模型数，点击展开/收起该角色的模型。"""

    def __init__(self, role, expanded=False, on_toggle=None, parent=None):
        super().__init__(parent)
        self.role = role
        self.on_toggle = on_toggle
        self.setFixedSize(300, 52)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(6)
        arrow = "▾" if expanded else "▸"
        self.lbl = StrongBodyLabel("%s %s" % (arrow, role["cn"]), self)
        self.lbl.setStyleSheet("color:%s;" % TH.t("text_hi"))
        lay.addWidget(self.lbl, 1)
        cap = CaptionLabel("%d 套" % role["count"], self)
        cap.setStyleSheet("color:%s;" % TH.t("text_dim"))
        lay.addWidget(cap)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.on_toggle:
            self.on_toggle(self.role["slug"])
        super().mousePressEvent(e)


class _AuthorHeaderCard(CardWidget):
    """角色页的第二级标题卡：改模作者 + 该作者在本角色下的模型数。

    用户要求「父分类用角色名，子分类用改模作者」——角色展开后先看到作者
    分组，再点作者展开它的模型。比角色标题卡窄一档、缩进一级，层级一眼看得出来。
    """

    INDENT = 18

    def __init__(self, author, count, expanded=False, on_toggle=None,
                 parent=None):
        super().__init__(parent)
        self.author = author
        self.on_toggle = on_toggle
        self.setFixedSize(300 - self.INDENT, 40)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 4)
        lay.setSpacing(6)
        arrow = "▾" if expanded else "▸"
        self.lbl = BodyLabel("%s %s" % (arrow, CL.author_label(author)), self)
        self.lbl.setStyleSheet("color:%s; font-size:12px;" % TH.t("text_hi"))
        lay.addWidget(self.lbl, 1)
        cap = CaptionLabel("%d 套" % count, self)
        cap.setStyleSheet("color:%s; font-size:11px;" % TH.t("text_dim"))
        lay.addWidget(cap)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.on_toggle:
            self.on_toggle(self.author)
        super().mousePressEvent(e)


class _AudioGroupCard(CardWidget):
    """音频页的分组标题卡：组名 + 条数，点击展开/收起该组。"""

    def __init__(self, group, expanded=False, on_toggle=None, parent=None):
        super().__init__(parent)
        self.group = group
        self.on_toggle = on_toggle
        self.setFixedSize(300, 46)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 5, 10, 5)
        lay.setSpacing(6)
        arrow = "▾" if expanded else "▸"
        self.lbl = StrongBodyLabel("%s %s" % (arrow, group["name"]), self)
        self.lbl.setStyleSheet("color:%s;" % TH.t("text_hi"))
        lay.addWidget(self.lbl, 1)
        cap = CaptionLabel("%d 条" % len(group["items"]), self)
        cap.setStyleSheet("color:%s;" % TH.t("text_dim"))
        lay.addWidget(cap)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.on_toggle:
            self.on_toggle(self.group["id"])
        super().mousePressEvent(e)


class _MarqueeLabel(QWidget):
    """单行文字：放得下就静态显示；放不下且鼠标悬停时横向滚动。

    需求池第3条：「这个改模人信息显示不全，改成鼠标挪上去时文字滚动展示，
    鼠标移开停止动效」。
    """

    SPEED = 2          # 每次步进像素
    INTERVAL = 50      # 步进间隔 ms
    PAUSE_MS = 900     # 到尾部停一下再回卷

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self._text = str(text or "")
        self._offset = 0
        self._timer = None
        self._hover = False
        self._pause_left = 0
        f = self.font()
        f.setPixelSize(10)
        self.setFont(f)
        self.setMouseTracking(True)
        self.setMinimumHeight(16)
        self.setToolTip(self._text)

    def setText(self, text):
        self._text = str(text or "")
        self._offset = 0
        self.setToolTip(self._text)
        self.update()

    def text(self):
        return self._text

    def _text_w(self):
        fm = self.fontMetrics()
        return fm.horizontalAdvance(self._text)

    def _needs_scroll(self):
        return self._text_w() > self.width() - 2

    def enterEvent(self, e):
        self._hover = True
        if self._needs_scroll():
            self._start()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self._stop()
        self._offset = 0
        self.update()
        super().leaveEvent(e)

    def _start(self):
        if self._timer is None:
            from PySide6.QtCore import QTimer
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
        self._pause_left = self.PAUSE_MS
        self._timer.start(self.INTERVAL)

    def _stop(self):
        if self._timer is not None:
            self._timer.stop()

    def _tick(self):
        if not self._hover:
            self._stop()
            return
        gap = 24                      # 尾部留白，回卷更自然
        limit = self._text_w() - max(1, self.width() - 2) + gap
        if limit <= 0:
            self._stop()
            return
        if self._offset >= limit:
            if self._pause_left > 0:
                self._pause_left -= self.INTERVAL
            else:
                self._offset = 0
                self._pause_left = self.PAUSE_MS
        else:
            self._offset += self.SPEED
        self.update()

    def resizeEvent(self, e):
        if not self._needs_scroll():
            self._stop()
            self._offset = 0
        super().resizeEvent(e)

    def paintEvent(self, e):
        from PySide6.QtGui import QPainter
        from PySide6.QtCore import Qt as _Qt
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        p.setFont(self.font())
        fm = p.fontMetrics()
        y = (self.height() + fm.ascent() - fm.descent()) // 2
        if self._needs_scroll() and self._offset > 0:
            p.drawText(-self._offset, y, self._text)
        else:
            avail = self.width() - 2
            txt = fm.elidedText(self._text, _Qt.ElideRight, avail)
            p.drawText(0, y, txt)
        p.end()


class _ModelCard(CardWidget):
    """角色页的单个模型卡：缩略图 + 服装名 + 上轨。

    作者已经作为上一级分组标题显示，这里名字只留服装名，避免
    「长崎素世·Mujica演出服（北极暖水港）」在作者组里重复一遍。
    """

    def __init__(self, model, thumb_qpixmap=None, on_add=None, parent=None,
                 show_author=False, on_add_persona=None):
        super().__init__(parent)
        self.model = model
        self.on_add = on_add
        self.on_add_persona = on_add_persona
        self.setFixedSize(124, 150)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(5, 5, 5, 5)
        lay.setSpacing(3)
        self.img = QLabel(self)
        self.img.setFixedSize(112, 96)
        self.img.setAlignment(Qt.AlignCenter)
        self.img.setStyleSheet("background:%s;border-radius:4px;"
                               % TH.t("thumb_bg"))
        if thumb_qpixmap is not None and not thumb_qpixmap.isNull():
            self.img.setPixmap(thumb_qpixmap.scaled(
                self.img.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation))
        else:
            self.img.setText(model["slug"][:4])
        lay.addWidget(self.img)
        # 作者已在上一级分组标题里显示，默认只留服装名
        if show_author:
            txt = CL.display_name(model)
        else:
            txt = str(model.get("costume") or model.get("name") or "")
        # 需求池第3条：名字太长显示不全 -> 悬停时横向滚动
        t = _MarqueeLabel(txt, self)
        t.setStyleSheet("color:%s;background:transparent;" % TH.t("text"))
        lay.addWidget(t)
        self.name_lbl = t
        # 两个上轨按钮：+ 加入默认轨（既有行为），人格 加入独立新轨
        btn_row = QWidget(self)
        br = QHBoxLayout(btn_row)
        br.setContentsMargins(0, 0, 0, 0)
        br.setSpacing(2)
        self.btn_add = ToolButton(self)
        self.btn_add.setIcon(FluentIcon.ADD.icon())
        self.btn_add.setFixedSize(22, 18)
        self.btn_add.setToolTip("加入时间轴（默认轨）")
        self.btn_add.clicked.connect(self._add)
        br.addWidget(self.btn_add)
        self.btn_persona = ToolButton(self)
        self.btn_persona.setIcon(FluentIcon.COPY.icon())
        self.btn_persona.setFixedSize(22, 18)
        self.btn_persona.setToolTip("作为新人格加入（新建独立时间轴）")
        self.btn_persona.clicked.connect(self._add_persona)
        br.addWidget(self.btn_persona)
        lay.addWidget(btn_row, 0, Qt.AlignRight)

    def _add(self):
        if self.on_add:
            self.on_add(self)

    def _add_persona(self):
        if getattr(self, "on_add_persona", None):
            self.on_add_persona(self)

    def mouseDoubleClickEvent(self, ev):
        if self.on_add:
            self.on_add(self)
        super().mouseDoubleClickEvent(ev)


class _TemplateCard(CardWidget):
    """模板页卡片：标题 + 说明 + 点击触发（A4 模板页）。"""

    def __init__(self, title, desc, on_click, parent=None):
        super().__init__(parent)
        self.on_click = on_click
        self.title = str(title)
        self.setFixedSize(168, 148)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        t = BodyLabel(title, self)
        t.setStyleSheet("color:%s; font-size:13px;" % TH.t("text_hi"))
        lay.addWidget(t)
        d = CaptionLabel(desc, self)
        d.setWordWrap(True)
        d.setStyleSheet("color:%s; font-size:11px;" % TH.t("text_dim"))
        lay.addWidget(d, 1)
        b = PushButton("应用", self)
        b.setFixedHeight(26)
        b.clicked.connect(self._click)
        lay.addWidget(b)

    def _click(self):
        if self.on_click:
            self.on_click()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._click()
        super().mousePressEvent(e)


class _FxRow(CardWidget):
    """已应用效果的一行：名称 + 时刻 + 删除按钮。

    用在工程属性面板的「已应用的滤镜 / 特效」区。
    kind: "effect"（proj["effects"] 里一条指令）/ "filter"（clip 上的滤镜）
    """

    def __init__(self, text, sub="", on_remove=None, parent=None,
                 tooltip=""):
        super().__init__(parent)
        self.on_remove = on_remove
        self.setFixedHeight(38)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 4, 4)
        lay.setSpacing(4)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        t = CaptionLabel(str(text or ""), self)
        t.setStyleSheet("color:%s;font-size:11px;" % TH.t("text"))
        col.addWidget(t)
        if sub:
            s = CaptionLabel(str(sub), self)
            s.setStyleSheet("color:%s;font-size:9px;" % TH.t("text_dim"))
            col.addWidget(s)
        lay.addLayout(col, 1)
        self.btn_del = ToolButton(self)
        self.btn_del.setIcon(FluentIcon.DELETE.icon())
        self.btn_del.setFixedSize(20, 20)
        self.btn_del.setToolTip("删除这一条")
        self.btn_del.clicked.connect(self._remove)
        lay.addWidget(self.btn_del)
        if tooltip:
            self.setToolTip(tooltip)

    def _remove(self):
        if self.on_remove:
            self.on_remove()


class _BgCateCard(CardWidget):
    """背景页的分类标题卡：分类名 + 张数，点击展开/收起（同一时刻一个）。

    与 _FxCateCard 同款交互（用户要求背景「和角色一样分类」）——首屏只
    建分类标题（39 个），点开才解码缩略图，避免一次建 400 张卡卡顿。
    """

    def __init__(self, cat, count, expanded=False, on_toggle=None,
                 parent=None):
        super().__init__(parent)
        self.cat = cat
        self.on_toggle = on_toggle
        self.setFixedSize(300, 46)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 5, 10, 5)
        lay.setSpacing(6)
        arrow = "▾" if expanded else "▸"
        lbl = StrongBodyLabel("%s %s" % (arrow, cat), self)
        lbl.setStyleSheet("color:%s;" % TH.t("text_hi"))
        lay.addWidget(lbl, 1)
        cap = CaptionLabel("%d 张" % count, self)
        cap.setStyleSheet("color:%s;" % TH.t("text_dim"))
        lay.addWidget(cap)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.on_toggle:
            self.on_toggle(self.cat)
        super().mousePressEvent(e)


class _BgCard(CardWidget):
    """背景栏的缩略图卡片：点一下=设为当前时刻的背景。

    比素材卡小（背景栏窄）：缩略图 96x54（16:9），名字单行省略。
    """

    def __init__(self, bg, thumb_qpixmap=None, on_click=None, parent=None):
        super().__init__(parent)
        self.bg = bg
        self.on_click = on_click
        self.setFixedSize(108, 92)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(5, 5, 5, 5)
        lay.setSpacing(2)
        self.img = QLabel(self)
        self.img.setFixedSize(96, 54)
        self.img.setAlignment(Qt.AlignCenter)
        self.img.setStyleSheet("background:%s;border-radius:3px;"
                               % TH.t("thumb_bg"))
        if thumb_qpixmap is not None and not thumb_qpixmap.isNull():
            self.img.setPixmap(thumb_qpixmap)
        else:
            self.img.setText("无图")
        lay.addWidget(self.img)
        name = str(bg.get("name") or os.path.basename(bg.get("full") or ""))
        lbl = CaptionLabel(name, self)
        lbl.setStyleSheet("color:%s;font-size:9px;" % TH.t("text"))
        lbl.setToolTip("%s\n%s" % (name, bg.get("cat") or ""))
        # 单行省略（背景名可能很长）
        with contextlib.suppress(Exception):
            lbl.setText(lbl.fontMetrics().elidedText(
                name, Qt.ElideRight, 96))
        lay.addWidget(lbl)
        self.setToolTip("%s\n分类：%s" % (name, bg.get("cat") or "-"))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.on_click:
            self.on_click(self.bg)
        super().mousePressEvent(e)


class _FxCateCard(CardWidget):
    """特效页的分类标题卡：分类名 + 条数，点击展开/收起（默认收起）。

    206 个特效全展开会建 206 个卡片（实测明显卡顿），所以按分类折叠，
    首屏只建 6 个标题卡。
    """

    def __init__(self, cat, count, expanded=False, on_toggle=None,
                 parent=None):
        super().__init__(parent)
        self.cat = cat
        self.on_toggle = on_toggle
        self.setFixedSize(300, 46)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 5, 10, 5)
        lay.setSpacing(6)
        arrow = "▾" if expanded else "▸"
        lbl = StrongBodyLabel("%s %s" % (arrow, cat), self)
        lbl.setStyleSheet("color:%s;" % TH.t("text_hi"))
        lay.addWidget(lbl, 1)
        cap = CaptionLabel("%d 个" % count, self)
        cap.setStyleSheet("color:%s;" % TH.t("text_dim"))
        lay.addWidget(cap)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.on_toggle:
            self.on_toggle(self.cat)
        super().mousePressEvent(e)


class _FxCard(CardWidget):
    """特效卡片：名字 + 一句说明 + 点击插入当前时刻。

    比模板卡窄一点、矮一点（206 个），名字过长用省略号（tooltip 给全文）。
    """

    def __init__(self, eff, on_apply=None, parent=None):
        super().__init__(parent)
        self.eff = eff
        self.on_apply = on_apply
        self.setFixedSize(220, 84)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(3)
        name = str(eff.get("name") or "")
        t = BodyLabel(name, self)
        t.setStyleSheet("color:%s; font-size:12px; font-weight:600;"
                        % TH.t("text_hi"))
        t.setToolTip(name)
        lay.addWidget(t)
        desc = str(eff.get("desc") or "")
        d = CaptionLabel(desc, self)
        d.setWordWrap(True)
        d.setStyleSheet("color:%s; font-size:10px;" % TH.t("text_dim"))
        d.setToolTip(desc)
        lay.addWidget(d, 1)
        # 类型小标（滤镜/镜头/复合），让人一眼知道会改什么
        kind = str(eff.get("kind") or "")
        ktxt = {"transform": "滤镜", "animation": "镜头",
                "combo": "复合"}.get(kind, "")
        if ktxt:
            k = CaptionLabel(ktxt, self)
            k.setStyleSheet("color:%s; font-size:9px;" % TH.t("text_dim"))
            lay.addWidget(k, 0, Qt.AlignRight)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.on_apply:
            self.on_apply(self.eff)
        super().mousePressEvent(e)


class _RuntimeCard(CardWidget):
    """E2 模板页「WebGAL 运行时」状态卡：状态行 + 在线获取 / 导入压缩包。"""

    def __init__(self, on_online, on_import, parent=None):
        super().__init__(parent)
        self.on_online = on_online
        self.on_import = on_import
        self.setFixedSize(168, 196)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        t = BodyLabel("WebGAL 运行时", self)
        t.setStyleSheet("color:%s; font-size:13px;" % TH.t("text_hi"))
        lay.addWidget(t)
        self.status_lbl = CaptionLabel("检测中…", self)
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color:%s; font-size:11px;" % TH.t("text_dim"))
        lay.addWidget(self.status_lbl, 1)
        b1 = PushButton("在线获取", self)
        b1.setFixedHeight(26)
        b1.clicked.connect(self.on_online)
        lay.addWidget(b1)
        b2 = PushButton("导入压缩包…", self)
        b2.setFixedHeight(26)
        b2.clicked.connect(self.on_import)
        lay.addWidget(b2)
        self.refresh()

    def refresh(self):
        """读 runtime_pack.status() 刷新状态文案（未安装/已装 vX.Y.Z+文件数）。"""
        try:
            import runtime_pack as RP
            st = RP.status()
        except Exception:
            self.status_lbl.setText("运行时不可用")
            return
        if st.get("installed"):
            self.status_lbl.setText("已装 %s\n%d 文件" % (
                st.get("version") or "未知版本", st.get("files", 0)))
        else:
            self.status_lbl.setText("未安装\n导入 zip 即用")


class _FrostBackground(QWidget):
    """磨砂底色层：画主题的窗口渐变（深色主题是纯色）。

    存在的理由：FluentWindow 的 stackedWidget 会把子控件的 QSS 背景
    盖住（实测设了 background 也看不见），自绘 paintEvent 才可靠。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self._grad = None
        self.refresh()

    def refresh(self):
        """按当前主题重建渐变（切主题时调用）。"""
        from PySide6.QtGui import QLinearGradient, QColor
        g = QLinearGradient(0, 0, 1, 1)
        g.setCoordinateMode(QLinearGradient.ObjectBoundingMode)
        if TH.is_light():
            # 冷蓝 → 暖白 → 冷蓝：制造可见的明暗起伏，面板透上来才有层次
            g.setColorAt(0.0, QColor("#a8bcd8"))
            g.setColorAt(0.28, QColor("#d9e4f2"))
            g.setColorAt(0.55, QColor("#c2d1e6"))
            g.setColorAt(0.78, QColor("#e3ebf6"))
            g.setColorAt(1.0, QColor("#b4c6de"))
        else:
            c = TH.qcolor("window_bg")
            g.setColorAt(0.0, c)
            g.setColorAt(1.0, c)
        self._grad = g
        self.update()

    def paintEvent(self, e):
        if self._grad is None:
            self.refresh()
        p = QPainter(self)
        p.fillRect(self.rect(), self._grad)


class _FrostBackground(QWidget):
    """磨砂底色层（保留类名供外部兼容；实际绘制已并入 VideoWorkbench）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self._grad = None
        self.refresh()

    def refresh(self):
        from PySide6.QtGui import QLinearGradient, QColor
        g = QLinearGradient(0, 0, 1, 1)
        g.setCoordinateMode(QLinearGradient.ObjectBoundingMode)
        if TH.is_light():
            g.setColorAt(0.0, QColor("#c9d6e8"))
            g.setColorAt(0.35, QColor("#e2eaf4"))
            g.setColorAt(0.62, QColor("#d6e2f0"))
            g.setColorAt(1.0, QColor("#cfdcee"))
        else:
            c = TH.qcolor("window_bg")
            g.setColorAt(0.0, c)
            g.setColorAt(1.0, c)
        self._grad = g
        self.update()

    def paintEvent(self, e):
        if self._grad is None:
            self.refresh()
        p = QPainter(self)
        p.fillRect(self.rect(), self._grad)


class VideoWorkbench(QWidget):
    """视频编辑工作台。project_dirty_changed(str) 通知外层改标题。"""

    def _frost_tile(self):
        """生成磨砂噪点纹理（64x64，±6 的极轻抖动），缓存复用。

        磨砂质感来自细微噪点，不是渐变 —— 纯渐变看起来只是渐变。
        面板半透明叠上去、噪点透出来，才有磨砂玻璃的观感。
        """
        tile = getattr(self, "_frost_pm", None)
        if (tile is not None
                and getattr(self, "_frost_pm_light", None) == TH.is_light()):
            return tile
        import random
        from PySide6.QtGui import QPixmap, QImage, QColor
        n = 64
        img = QImage(n, n, QImage.Format_ARGB32)
        rnd = random.Random(20261118)      # 固定种子：每次一致，不会闪
        base = 205 if TH.is_light() else 30
        for y in range(n):
            for x in range(n):
                d = rnd.randint(-5, 5)
                v = max(0, min(255, base + d))
                a = 15 if TH.is_light() else 12
                img.setPixelColor(x, y, QColor(v, v, v, a))
        tile = QPixmap.fromImage(img)
        self._frost_pm = tile
        self._frost_pm_light = TH.is_light()
        return tile

    def paintEvent(self, e):
        """自绘磨砂底：主题渐变 + 细微噪点纹理。

        深色主题退化成纯色 + 噪点（暗色下噪点几乎不可见，无副作用）。
        """
        from PySide6.QtGui import QLinearGradient, QColor
        p = QPainter(self)
        g = QLinearGradient(0, 0, self.width(), self.height())
        if TH.is_light():
            # 冷白 → 微冷 → 冷白：幅度小但够看见起伏（太深会透成灰玻璃）
            g.setColorAt(0.0, QColor("#cfdcec"))
            g.setColorAt(0.28, QColor("#eef3f9"))
            g.setColorAt(0.55, QColor("#dee8f4"))
            g.setColorAt(0.78, QColor("#f2f6fb"))
            g.setColorAt(1.0, QColor("#d5e1ef"))
        else:
            c = TH.qcolor("window_bg")
            g.setColorAt(0.0, c)
            g.setColorAt(1.0, c)
        p.fillRect(self.rect(), g)
        # 噪点纹理平铺：磨砂的"颗粒感"
        p.drawTiledPixmap(self.rect(), self._frost_tile())

    def _apply_theme(self):
        """切主题时重刷那些在 setStyleSheet 里写死颜色的部件。"""
        self._frost_pm = None       # 噪点纹理按主题重生成
        with contextlib.suppress(Exception): self.update()
        with contextlib.suppress(Exception):
            global _SPLIT_QSS
            _SPLIT_QSS = _split_qss()
            for sp in (getattr(self, "_hsplit", None),
                       getattr(self, "_vsplit", None)):
                if sp is not None:
                    sp.setStyleSheet(_SPLIT_QSS)
        try:
            # 只让最外层面板带半透明白（磨砂卡片），内层 viewport/host
            # 必须透明：三层都上 0.66 alpha 会叠成纯白，层次全没了。
            qss = ("#assetPanel{background:%s;border-radius:8px;}"
                   "QWidget#videoWorkbenchRoot{background:transparent;}"
                   % TH.t("panel_bg"))
            self._left_panel.setStyleSheet(qss)
            ib = _inner_bg()
            self.asset_scroll.setStyleSheet(
                "QScrollArea{background:%s;border:none;}"
                "QScrollArea::viewport{background:%s;}"
                "QScrollArea > QWidget > QWidget{background:%s;}"
                "QWidget#assetHost{background:%s;}" % (ib, ib, ib, ib))
            self.stage.setStyleSheet("background:%s;border-radius:6px;"
                                     % TH.t("canvas_bg"))
        except Exception:
            pass
        try:
            ib = _inner_bg()
            self._right_panel.setStyleSheet(
                "#propPanel{background:%s;border-radius:8px;}"
                "#propHost{background:%s;}"
                "QScrollArea::viewport{background:%s;}"
                % (TH.t("panel_bg"), ib, ib))
            self._prop_scroll.setStyleSheet(
                "QScrollArea{background:%s;border:none;}"
                "QScrollArea::viewport{background:%s;}"
                "QScrollArea > QWidget > QWidget{background:%s;}"
                % (ib, ib, ib))
        except Exception:
            pass
        for attr, extra in (("workers_hint", " font-size:11px;"),
                            ("anim_hint", " font-size:11px;"),
                            ("est_lbl", " font-size:11px;")):
            w = getattr(self, attr, None)
            if w is not None:
                with contextlib.suppress(Exception):
                    w.setStyleSheet("color:%s;%s" % (TH.t("text_dim"), extra))
        with contextlib.suppress(Exception): self.update()

    def __init__(self, parent=None, proj=None, proj_dir=None):
        super().__init__(parent)
        self.proj = proj or V.new_project()
        self.proj_dir = proj_dir
        with contextlib.suppress(Exception):
            self.timeline.set_proj_dir(self.proj_dir or "")
        self.assets = []            # source dict 列表
        self._sel_clip = None       # 选中 clip id
        self._sources = VE.SourceManager()
        self._hist = []
        self._hist_i = -1
        self._t = 0.0
        self._playing = False
        self._tick_busy = False
        self._loading = False       # spin 回填防回环
        self._role_open = None      # 角色页：当前展开的角色 slug（None=全部收起）
        self._author_open = set()   # 角色页：当前展开的作者名集合
        self._author_open_role = None   # 上面这组作者属于哪个角色
        self._audio_open = None     # 音频页：当前展开的分组 id（None=全部收起）
        self._audio_player = None   # 素材库试听播放器（QMediaPlayer，懒创建）
        self._audio_out = None      # QAudioOutput
        self._previewing = None     # 正在试听的 _AudioCard（停止时复位图标）
        # 预览混音（用户要求「播放视频预览时要有声音」）
        self._mix_player = None     # 预览音轨播放器
        self._mix_out = None        # 预览音轨输出
        self._mix_file = None       # 混音 wav 路径（缓存）
        self._mix_key = None        # 缓存对应工程指纹
        self._mix_busy = False      # 后台混音中
        self._mix_pending = False   # 混音期间被要求播放（完成后自动起播）
        self._mix_result = None      # 后台混音结果 ("path", fp)

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._advance)

        # 磨砂底：工作台自己画主题渐变，三栏半透明白面板叠在上面。
        # 不能用 QSS 铺底（FluentWindow 的容器会盖住子控件的 QSS 背景），
        # 自绘 paintEvent 才可靠；也顺带避免 alpha 叠三层变纯白。
        self.setAttribute(Qt.WA_StyledBackground, True)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # ---------- 顶部 Pivot ----------
        # 磨砂底：整页铺一层主题渐变，三栏面板都是半透明白叠在上面，
        # 渐变从面板底下透出来 → 层次感（纯 Qt 绘制，不依赖 DWM 模糊）
        self.setObjectName("videoWorkbenchRoot")
        self.setStyleSheet(
            "QWidget#videoWorkbenchRoot{background:%s;}" % TH.t("window_bg"))
        self.pivot = Pivot(self)
        # Pivot 页签：去掉「贴纸」（用户要求），「特效」改为「滤镜」。
        # v3.6.2：模板页改成特效库（页签名同步）；背景单独成一页签
        # （用户要求「背景放这个分类里面」——和角色/音频并列，不是独立栏）。
        for key, text in [("media", "角色"), ("audio", "音频"),
                          ("text", "文本"), ("bg", "背景"),
                          ("filter", "滤镜"), ("transition", "转场"),
                          ("template", "特效")]:
            self.pivot.addItem(key, text)
        self.pivot.setCurrentItem("media")
        self._asset_tab = "media"   # currentItemChanged 槽参数记录（PivotItem 无 routeKey）
        self.pivot.currentItemChanged.connect(self._filter_assets)
        pv = QWidget(self)
        pl = QHBoxLayout(pv)
        pl.setContentsMargins(12, 6, 12, 2)
        pl.addWidget(self.pivot)
        pl.addStretch(1)
        self.act_save = PushButton("保存工程", pv)
        self.act_save.setFixedHeight(30)
        self.act_save.clicked.connect(lambda: self.save_project())
        self.act_export = PrimaryPushButton("导出", pv)
        self.act_export.setIcon(FluentIcon.SEND.icon())
        self.act_export.setFixedHeight(30)
        self.act_export.clicked.connect(self._open_export_dialog)
        pl.addWidget(self.act_save)
        pl.addWidget(self.act_export)

        # ---------- F2-L5（v3.5）：幕 TabBar ----------
        # 多幕工程（v2）才出现；单幕工程（v1）**整行不建**（零 diff）。
        # 一幕一条独立时间轴，切 tab = 切当前幕游标。
        self._act_bar = None
        self._act_tabs = None
        self._cur_aid = None
        self._act_buttons = {}

        # ---------- 中部三列（QSplitter 可拖拽调宽）----------
        hsplit = QSplitter(Qt.Horizontal, self)
        hsplit.setObjectName("wbHSplit")
        hsplit.setChildrenCollapsible(False)
        hsplit.setHandleWidth(6)
        hsplit.setStyleSheet(_SPLIT_QSS)

        # 左：素材库
        left = QFrame(self)
        self._left_panel = left
        left.setObjectName("assetPanel")
        left.setMinimumWidth(SPLIT_H_MIN[0])
        left.setStyleSheet("#assetPanel{background:%s;border-radius:8px;}"
                           % TH.t("panel_bg"))
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 10, 10, 10)
        ll.setSpacing(8)
        lh = QHBoxLayout()
        self.asset_header = StrongBodyLabel("素材库", left)
        lh.addWidget(self.asset_header)
        lh.addStretch(1)
        # 角色页专用「导入模型」按钮：只在这一页出现（其余页签导模型没意义）。
        # 挂在标题行右侧，和「素材库」同一行，不额外占高度。
        self.btn_import_model = PushButton("导入模型", left)
        self.btn_import_model.setToolTip("把模型复制进素材库（分类 / 改模人 / 缩略图）")
        self.btn_import_model.clicked.connect(self._import_model)
        self.btn_import_model.setVisible(False)
        lh.addWidget(self.btn_import_model)
        ll.addLayout(lh)
        # v3.6.1：左栏不再放「添加素材文件 / 导入木偶工程」按钮
        # （用户要求去掉）。功能入口移到「文件」菜单，见
        # l2d_rig_qt._build_video_menus。这里保留占位属性，避免
        # _show_script_editor 等旧调用点报 AttributeError。
        self.btn_add_file = None
        self.btn_add_puppet = None
        self.asset_scroll = ScrollArea(left)
        # v3.6.2：运镜编辑器常驻在特效页左栏顶部（整宽，不流动布局）。
        # 放在 ScrollArea 之前，避免被 FlowLayout 当卡片挤扁；
        # 只在特效页可见，其他页隐藏（_switch_asset_page 里控制）。
        try:
            import camera_editor as CE
            self._cam_panel = CE.CameraPathPanel(left)
            self._cam_panel.applied.connect(self._apply_camera_path)
            self._cam_panel.preview_sampled.connect(self._on_camera_preview)
            self._cam_panel.preview_reset.connect(self._on_camera_reset)
            self._cam_panel.setVisible(False)     # 默认不在特效页
            ll.addWidget(self._cam_panel)
        except Exception:
            self._cam_panel = None
        self.asset_scroll.setWidgetResizable(True)
        self.asset_host = QWidget()
        # v3.4.7：viewport 默认浅底，深色面板中间露一整块白（属性面板
        # pscroll 修过同款问题，素材库这里漏了）；host 用 objectName 限定，
        # 不用通配选择器，避免盖掉 CardWidget 卡片自绘底色。
        self.asset_host.setObjectName("assetHost")
        # 内层底色按主题：浅色=transparent（避免三层白叠成死白），
        # 深色=panel_bg（深色下透明会露出 Qt 默认白底）
        _ib = _inner_bg()
        self.asset_scroll.setStyleSheet(
            "QScrollArea{background:%s;border:none;}"
            "QScrollArea::viewport{background:%s;}"
            "QScrollArea > QWidget > QWidget{background:%s;}"
            "QWidget#assetHost{background:%s;}" % (_ib, _ib, _ib, _ib))
        self.asset_flow = FlowLayout(self.asset_host, needAni=False)
        self.asset_flow.setContentsMargins(0, 0, 0, 0)
        self.asset_scroll.setWidget(self.asset_host)
        ll.addWidget(self.asset_scroll, 1)
        self.asset_empty = CaptionLabel("还没有素材\n点上方按钮导入，或双击卡片上轨",
                                        left)
        self.asset_empty.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.asset_empty)
        # F1：文本页剧情编辑器常驻（tab 切换只 show/hide，表单内容不丢）
        self.script_editor = ScriptEditor(self, left)
        ll.addWidget(self.script_editor, 1)

        # v3.6.2：批量操作工具条（对标对方工具的「批量操作」）。
        # 挂在剧情编辑器**下方**：用户正看着台词表时，一键做批处理。
        # 做成常驻条而不是对话框——这些操作是「跑完看结果、不满意撤销」
        # 的模式，多一层确认反而烦；每次执行都 push_history 可回退。
        try:
            import batch_bar as BB
            self._batch_bar = BB.BatchBar(left)
            self._batch_bar.run_requested.connect(self._run_batch)
            self._batch_bar.setVisible(False)     # 仅在文本页显示
            ll.addWidget(self._batch_bar)
        except Exception:
            self._batch_bar = None

        # ---- v3.6.2：背景页签（用户要求「背景放这个分类里面」）----
        # 和其它素材页签并列（角色/音频/背景/特效），切到「背景」页时
        # 显示这个面板、隐藏素材卡片区。不是独立栏——用户明确要页签。
        # 数据 = 素材库 kind=bg（1053 张），点缩略图=换当前时刻的背景。
        self.bg_panel = QWidget(left)
        self.bg_panel.setObjectName("bgPanel")
        self.bg_panel.setStyleSheet(
            "QWidget#bgPanel{background:transparent;}")
        bl = QVBoxLayout(self.bg_panel)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        _bgr = QHBoxLayout()
        self.bg_count = CaptionLabel("", self.bg_panel)
        self.bg_count.setStyleSheet("color:%s;" % TH.t("text_dim"))
        _bgr.addWidget(self.bg_count)
        _bgr.addStretch(1)
        bl.addLayout(_bgr)
        # 分类下拉（按目录分组：商店/路边/学校、工作·羽丘/…）
        from qfluentwidgets import ComboBox as _CB
        self.bg_combo = _CB(self.bg_panel)
        self.bg_combo.addItem("全部")
        self.bg_combo.currentIndexChanged.connect(self._rebuild_bg_cards)
        bl.addWidget(self.bg_combo)
        self.bg_scroll = ScrollArea(self.bg_panel)
        self.bg_scroll.setWidgetResizable(True)
        self.bg_host = QWidget()
        self.bg_host.setObjectName("bgHost")
        _ibg = _inner_bg()
        self.bg_scroll.setStyleSheet(
            "QScrollArea{background:%s;border:none;}"
            "QScrollArea::viewport{background:%s;}"
            "QScrollArea > QWidget > QWidget{background:%s;}"
            "QWidget#bgHost{background:%s;}" % (_ibg, _ibg, _ibg, _ibg))
        self.bg_flow = FlowLayout(self.bg_host, needAni=False)
        self.bg_flow.setContentsMargins(0, 0, 0, 0)
        self.bg_scroll.setWidget(self.bg_host)
        bl.addWidget(self.bg_scroll, 1)
        self.bg_empty = CaptionLabel(
            "素材库里没有背景图\n（资产管理里确认素材库位置）", self.bg_panel)
        self.bg_empty.setAlignment(Qt.AlignCenter)
        bl.addWidget(self.bg_empty)
        ll.addWidget(self.bg_panel, 1)
        self.bg_panel.setVisible(False)     # 默认在「角色」页，背景面板隐藏
        self._bg_items = []          # 背景条目缓存（首次展开时读索引）

        hsplit.addWidget(left)

        # 中：预览
        center = QWidget(self)
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)
        # A2：预览画布换成 StageView（可拖拽布置 + 选中联动），最小尺寸不变
        self.stage = StageView(center)
        self.stage.setMinimumSize(320, 180)
        self.stage.setStyleSheet("background:%s;border-radius:6px;"
                                 % TH.t("canvas_bg"))
        self.stage.clip_selected.connect(self._on_stage_select)
        self.stage.layout_changed.connect(self._on_stage_layout_changed)
        self.preview = self.stage          # 兼容旧引用
        # ---- H3（v3.5）：实时预览与 StageView 共存（设计 §5）----
        # QStackedLayout 同位切换：page0=StageView（永远在场，承担全部交互），
        # page1=WebPreviewView（懒创建，W A_TransparentForMouseEvents）。
        # available()==False 时**连开关都不创建**（设计 §2 硬规则 1、3），
        # 既有 UI 测试零 diff。
        self._webview = None
        self._preview_real = False
        self._stack = QStackedLayout()
        self._stack.setContentsMargins(0, 0, 0, 0)
        stack_host = QWidget(center)
        stack_host.setLayout(self._stack)
        self._stack.addWidget(self.stage)
        cl.addWidget(stack_host, 1)
        self._preview_badge = None
        try:
            import webgal_preview as WP
            self._wp = WP
            # v3.5：角标创建条件 = WebEngine 可用 **且** WebGAL 运行时已就位。
            # 只查 available() 会出现「开关常亮但页面空白」（装了 WebEngine
            # 可选件但运行时素材没下载 -> 点开只有 no_runtime 错误页）。
            if self._preview_runtime_ready():
                self._make_preview_badge(stack_host)
        except Exception:
            self._wp = None
        pb = QHBoxLayout()
        self.btn_play = ToolButton(center)
        self.btn_play.setIcon(FluentIcon.PLAY.icon())
        self.btn_play.clicked.connect(self.toggle_play)
        pb.addWidget(self.btn_play)
        self.time_lbl = CaptionLabel("00:00.0 / 00:00.0", center)
        self.time_lbl.setMinimumWidth(120)
        pb.addWidget(self.time_lbl)
        self.sld_pos = ProgressBar(center)      # 兼当进度显示（不可拖）
        self.sld_pos.setFixedHeight(6)
        self.sld_pos.setTextVisible(False)
        self.sld_pos.setRange(0, 1000)
        pb.addWidget(self.sld_pos, 1)
        cl.addLayout(pb)
        center.setMinimumWidth(SPLIT_H_MIN[1])
        hsplit.addWidget(center)

        # 右：属性
        right = QFrame(self)
        self._right_panel = right
        right.setObjectName("propPanel")
        right.setMinimumWidth(SPLIT_H_MIN[2])
        # F1 修复：内层 host 裸 QWidget 无背景 -> 暗主题下近白底浅灰字对比崩。
        # 给 host 定 objectName + 深色背景，ScrollArea viewport 同色，标签才可见。
        # 半透明白只上最外层 propPanel 一层（内层按主题，见 _inner_bg）
        _pib = _inner_bg()
        right.setStyleSheet(
            "#propPanel{background:%s;border-radius:8px;}"
            "#propHost{background:%s;}"
            "QScrollArea::viewport{background:%s;}"
            % (TH.t("panel_bg"), _pib, _pib))
        # 属性内容放进滚动容器：内容多时面板可被压缩，不把时间轴饿死
        host = QWidget(right)
        host.setObjectName("propHost")
        rl = QVBoxLayout(host)
        rl.setContentsMargins(12, 12, 12, 12)
        rl.setSpacing(8)
        rl.addWidget(StrongBodyLabel("工程属性", right))
        r1 = QHBoxLayout()
        r1.addWidget(CaptionLabel("分辨率", right))
        self.cmb_res = ComboBox(right)
        for r in ("1920×1080", "1080×1920", "1280×720", "720×1280",
                  "1080×1080"):
            self.cmb_res.addItem(r)
        self.cmb_res.currentIndexChanged.connect(self._res_changed)
        r1.addWidget(self.cmb_res, 1)
        rl.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(CaptionLabel("帧率", right))
        self.spin_fps = TH.fix_spinbox(SpinBox(right))
        self.spin_fps.setRange(1, 120)
        self.spin_fps.setValue(30)
        self.spin_fps.valueChanged.connect(self._fps_changed)
        r2.addWidget(self.spin_fps, 1)
        rl.addLayout(r2)
        self.dur_lbl = CaptionLabel("总时长：00:00.0", right)
        rl.addWidget(self.dur_lbl)

        # ---- v3.6.2：已应用的滤镜 / 特效管理（用户要求「能删除」）----
        # 分两块：
        #   · 特效（proj["effects"]）：按时刻的 setTransform / setTempAnimation，
        #     一个工程可能堆几十条，逐条列出来删
        #   · 滤镜（clip["filter"]）：挂在片段上的调色参数，列「哪个片段带了
        #     滤镜」+ 一键清除
        # 原来只能覆盖不能删：点了滤镜/特效后再想取消，只能撤销，撤销栈一长
        # 就找不回来了。
        _fx_title = QHBoxLayout()
        _fx_title.addWidget(StrongBodyLabel("已应用的滤镜 / 特效", right))
        _fx_title.addStretch(1)
        self.fx_count_lbl = CaptionLabel("", right)
        self.fx_count_lbl.setStyleSheet("color:%s;" % TH.t("text_dim"))
        _fx_title.addWidget(self.fx_count_lbl)
        rl.addLayout(_fx_title)

        self.fx_empty_lbl = CaptionLabel("当前工程没有特效，也没有片段带滤镜",
                                         right)
        self.fx_empty_lbl.setStyleSheet("color:%s;font-size:11px;"
                                        % TH.t("text_dim"))
        self.fx_empty_lbl.setWordWrap(True)
        rl.addWidget(self.fx_empty_lbl)

        # 特效列表容器（动态填 _FxRow）
        self.fx_rows_host = QWidget(right)
        self.fx_rows_lay = QVBoxLayout(self.fx_rows_host)
        self.fx_rows_lay.setContentsMargins(0, 0, 0, 0)
        self.fx_rows_lay.setSpacing(3)
        rl.addWidget(self.fx_rows_host)

        # 滤镜列表容器
        self.flt_rows_host = QWidget(right)
        self.flt_rows_lay = QVBoxLayout(self.flt_rows_host)
        self.flt_rows_lay.setContentsMargins(0, 0, 0, 0)
        self.flt_rows_lay.setSpacing(3)
        rl.addWidget(self.flt_rows_host)

        # 批量删除按钮
        _fx_btns = QHBoxLayout()
        self.btn_clear_effects = PushButton("清空所有特效", right)
        self.btn_clear_effects.setFixedHeight(26)
        self.btn_clear_effects.setToolTip(
            "删除当前幕里全部 setTransform / setTempAnimation 特效指令")
        self.btn_clear_effects.clicked.connect(self._clear_all_effects)
        _fx_btns.addWidget(self.btn_clear_effects)
        self.btn_clear_filters = PushButton("清空所有滤镜", right)
        self.btn_clear_filters.setFixedHeight(26)
        self.btn_clear_filters.setToolTip("删除所有片段上挂的滤镜参数")
        self.btn_clear_filters.clicked.connect(self._clear_all_filters)
        _fx_btns.addWidget(self.btn_clear_filters)
        rl.addLayout(_fx_btns)

        rl.addSpacing(10)
        self.clip_title = StrongBodyLabel("选中片段属性", right)
        rl.addWidget(self.clip_title)
        self.clip_info = CaptionLabel("未选中", right)
        self.clip_info.setWordWrap(True)
        rl.addWidget(self.clip_info)
        for lab in ("start", "in", "dur"):
            row = QHBoxLayout()
            row.addWidget(CaptionLabel({"start": "起点", "in": "素材入点",
                                        "dur": "时长"}[lab], right))
            sp = TH.fix_spinbox(DoubleSpinBox(right))
            sp.setRange(0.0, 600.0)
            sp.setSingleStep(0.1)
            sp.setDecimals(2)
            sp.setSuffix(" s")
            sp.valueChanged.connect(lambda _v, k=lab: self._clip_spin_changed(k))
            setattr(self, "spin_" + lab, sp)
            row.addWidget(sp, 1)
            rl.addLayout(row)

        # ---- 布局组：X/Y/Scale/Rot + 六向对齐（A2 StageView）----
        self.lay_group = StrongBodyLabel("布局（舞台）", right)
        self.lay_group.setVisible(False)
        rl.addWidget(self.lay_group)
        for lab, key, rng in (("X", "x", (-0.5, 1.5)), ("Y", "y", (-0.5, 1.5)),
                              ("Scale", "scale", (0.1, 10.0)),
                              ("Rot", "rot", (-180.0, 180.0))):
            row = QHBoxLayout()
            row.addWidget(CaptionLabel(lab, right))
            sp = TH.fix_spinbox(DoubleSpinBox(right))
            sp.setRange(rng[0], rng[1])
            sp.setSingleStep(1.0 if key == "rot" else 0.01)
            sp.setDecimals(1 if key == "rot" else 3)
            sp.setSuffix(" °" if key == "rot" else "")
            sp.valueChanged.connect(lambda _v, k=key: self._layout_spin_changed(k))
            setattr(self, "spin_lay_" + key, sp)
            row.addWidget(sp, 1)
            rl.addLayout(row)
        al = QHBoxLayout()
        for label, (nx, ny) in (("左上", (0.0, 0.0)), ("上", (0.5, 0.0)),
                                ("右上", (1.0, 0.0)), ("左下", (0.0, 1.0)),
                                ("下", (0.5, 1.0)), ("右下", (1.0, 1.0))):
            btn = PushButton(label, right)
            btn.setFixedHeight(28)
            btn.clicked.connect(lambda _=False, a=nx, b=ny: self._align_clip(a, b))
            al.addWidget(btn)
        rl.addLayout(al)

        # ---- puppet 预置动作/表情：下拉 + 预览按钮（C2）----
        self.act_group = StrongBodyLabel("预置动作 / 表情", right)
        self.act_group.setVisible(False)
        rl.addWidget(self.act_group)
        ar = QHBoxLayout()
        self._motion_ids = [mid for mid, _ in self._preset_choices("motion")]
        self.cmb_motion = ComboBox(right)
        for _mid, name in self._preset_choices("motion"):
            self.cmb_motion.addItem(name)
        self.cmb_motion.currentIndexChanged.connect(self._motion_changed)
        ar.addWidget(self.cmb_motion, 1)
        self._expression_ids = [eid for eid, _ in self._preset_choices("expression")]
        self.cmb_expr = ComboBox(right)
        for _eid, name in self._preset_choices("expression"):
            self.cmb_expr.addItem(name)
        self.cmb_expr.currentIndexChanged.connect(self._expression_changed)
        ar.addWidget(self.cmb_expr, 1)
        self.btn_play_clip = ToolButton(right)
        self.btn_play_clip.setIcon(FluentIcon.PLAY.icon())
        self.btn_play_clip.setToolTip("在该片段起点预览")
        self.btn_play_clip.clicked.connect(self._play_clip_preview)
        ar.addWidget(self.btn_play_clip)
        rl.addLayout(ar)

        self.btn_del_clip = PushButton("删除选中片段", right)
        self.btn_del_clip.clicked.connect(self._delete_selected)
        rl.addWidget(self.btn_del_clip)
        rl.addStretch(1)
        self._prop_scroll = pscroll = ScrollArea(right)
        pscroll.setWidgetResizable(True)
        # 内层底色按主题（同素材库）
        _ib2 = _inner_bg()
        pscroll.setStyleSheet(
            "QScrollArea{background:%s;border:none;}"
            "QScrollArea::viewport{background:%s;}"
            "QScrollArea > QWidget > QWidget{background:%s;}"
            % (_ib2, _ib2, _ib2))
        pscroll.setWidget(host)
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.addWidget(pscroll)
        hsplit.addWidget(right)
        hsplit.setStretchFactor(0, 0)
        hsplit.setStretchFactor(1, 2)      # 预览列 stretch 最大
        hsplit.setStretchFactor(2, 0)
        hsplit.setSizes(SPLIT_H_DEFAULT)
        self._hsplit = hsplit

        # ---------- 顶部区域（pv + 幕条 + 三列 splitter）----------
        top = QWidget(self)
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(0)
        tv.addWidget(pv)
        # F2-L5：幕 TabBar 的宿主（多幕时才建并插入）
        self._act_bar_host = QWidget(self)
        self._act_bar_layout = QHBoxLayout(self._act_bar_host)
        self._act_bar_layout.setContentsMargins(12, 2, 12, 2)
        self._act_bar_layout.setSpacing(6)
        self._act_bar_host.setVisible(False)
        tv.addWidget(self._act_bar_host)
        tv.addWidget(hsplit, 1)

        # ---------- 底部：clip 时间轴 ----------
        import clip_timeline as CT
        self.timeline = CT.ClipTimelinePanel(self, wb=self)
        self.timeline.seeked.connect(self.seek)
        self.timeline.selection_changed.connect(self._on_selection)
        # 面板 docstring 约定「任何成功的模型操作后发 edit_committed，
        # 外层负责 push history」——原来只连了 _after_edit（刷新），
        # 时间轴上的拖动/删除/切分**从没进过撤销栈**（撤销会直接跳回
        # 更早的状态）。多选成组编辑一次改一片，这个缺陷会被放大成
        # "删了十几个片段撤销不回来"，所以这里补上 push。
        self.timeline.edit_committed.connect(self._on_timeline_edit)
        self.timeline.setMinimumHeight(SPLIT_V_MIN_TIMELINE)
        # 语音轨读 wav 波形要绝对路径 -> 把工程目录同步给时间轴
        with contextlib.suppress(Exception):
            self.timeline.set_proj_dir(getattr(self, "proj_dir", "") or "")

        # ---------- 垂直 splitter：上方区域 | 时间轴 ----------
        vsplit = QSplitter(Qt.Vertical, self)
        vsplit.setObjectName("wbVSplit")
        vsplit.setChildrenCollapsible(False)
        vsplit.setHandleWidth(6)
        vsplit.setStyleSheet(_SPLIT_QSS)
        vsplit.addWidget(top)
        vsplit.addWidget(self.timeline)
        vsplit.setStretchFactor(0, 3)
        vsplit.setStretchFactor(1, 1)
        vsplit.setSizes(SPLIT_V_DEFAULT)
        self._vsplit = vsplit

        # ---------- splitter 尺寸持久化 ----------
        # QSettings(IniFormat) 在没设 organization 时会退到**当前工作目录**，
        # 把 muppet_workbench.ini 落进安装目录/仓库。这里强制绝对路径：
        # SETTINGS_NAME 是绝对路径（测试用临时 ini）就直接用，否则写到
        # %APPDATA%\MuppetStudio\，不污染应用目录。
        _sname = SETTINGS_NAME
        if os.path.isabs(_sname):
            _settings_path = _sname
        else:
            _base = os.environ.get("APPDATA") or os.path.expanduser("~")
            _settings_path = os.path.join(_base, "MuppetStudio", _sname)
            with contextlib.suppress(Exception):
                os.makedirs(os.path.dirname(_settings_path), exist_ok=True)
        self._settings = QSettings(_settings_path, QSettings.IniFormat)
        hsplit.splitterMoved.connect(lambda *a: self._save_splitter_sizes())
        vsplit.splitterMoved.connect(lambda *a: self._save_splitter_sizes())
        self._restore_splitter_sizes()

        v.addWidget(vsplit)
        _WORKBENCHES.append(self)
        self._apply_theme()
        # 首次渲染左栏：构造时没调过 _rebuild_asset_cards，
        # 否则「角色」页一片空白 + 显示"还没有素材"（用户实报）。
        with contextlib.suppress(Exception): self._rebuild_asset_cards()

        # 快捷键：Ctrl+S 保存 / Ctrl+Z Y 撤销重做 / 空格播放
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save_project)
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.redo)
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self.toggle_play)
        # v3.5.2：时间轴键盘导航（之前只有空格/Del，左右全靠鼠标拖）
        # 左右 = ±1 帧（1/fps）；Shift = ±1 秒；Home/End = 首尾。
        # QShortcut 是 WindowShortcut：焦点在输入框里时 Qt 先发
        # ShortcutOverride 给输入框（它 accept 自己要的键），不会抢光标键。
        for keys, fn in (
                ("Left", lambda: self._seek_relative(-1)),
                ("Right", lambda: self._seek_relative(1)),
                ("Shift+Left", lambda: self._seek_relative(-1.0, True)),
                ("Shift+Right", lambda: self._seek_relative(1.0, True)),
                ("Home", lambda: self.seek(0.0)),
                ("End", lambda: self.seek(V.project_duration(
                    self.proj, getattr(self, "_cur_aid", None)))),
        ):
            QShortcut(QKeySequence(keys), self, activated=fn)

        self._apply_project(None)
        self._seed_history()
        # v3.6.2：工程属性里「已应用的滤镜 / 特效」首屏填充
        with contextlib.suppress(Exception): self._refresh_fx_panel()
        # v3.6.2：背景页签**懒渲染** —— 首次切到「背景」时才读索引建卡
        # （1053 条目建 400 张卡有开销，不该拖慢启动）。这里只清空占位。

    # ================= splitter 尺寸持久化 =================
    def _save_splitter_sizes(self):
        """拖拽结束（splitterMoved）：把两个 splitter 的尺寸写入 QSettings。"""
        with contextlib.suppress(Exception):
            self._settings.setValue("splitter/horizontal", self._hsplit.sizes())
            self._settings.setValue("splitter/vertical", self._vsplit.sizes())
            self._settings.sync()

    def _restore_splitter_sizes(self):
        """构造时恢复：校验列表长度与正数，否则用默认尺寸。"""
        with contextlib.suppress(Exception):
            hs = self._settings.value("splitter/horizontal")
            if isinstance(hs, (list, tuple)) and len(hs) == 3 \
                    and all(int(v) > 0 for v in hs):
                self._hsplit.setSizes([int(v) for v in hs])
            vs = self._settings.value("splitter/vertical")
            if isinstance(vs, (list, tuple)) and len(vs) == 2 \
                    and all(int(v) > 0 for v in vs):
                self._vsplit.setSizes([int(v) for v in vs])

    # ================= 工程生命周期 =================
    def _refresh_all(self):
        """工程结构/属性变化后的统一刷新：时间轴 + 传输条 + 属性 + 预览。"""
        with contextlib.suppress(Exception): self.timeline.refresh()
        self._update_transport()
        self._fill_clip_props()
        with contextlib.suppress(Exception): self._refresh_fx_panel()
        if not self._playing:
            self.render_frame(self._t)

    def _apply_project(self, _ignored):
        p = self.proj
        self.timeline.set_project(p)
        # F2-L5：载入/新建工程后重幕条（幕数可能变了）
        self._cur_aid = None
        self._refresh_act_bar()
        self.spin_fps.blockSignals(True)
        self.spin_fps.setValue(int(round(p["fps"])))
        self.spin_fps.blockSignals(False)
        self._set_res_combo(p["width"], p["height"])
        self._refresh_all()

    def _set_res_combo(self, w, h):
        want = "%d×%d" % (w, h)
        self.cmb_res.blockSignals(True)
        idx = self.cmb_res.findText(want)
        if idx < 0:
            # ComboBox 只有 insertItem，没有 insertText（用 insertText 会 AttributeError）
            if want not in [self.cmb_res.itemText(i)
                            for i in range(self.cmb_res.count())]:
                self.cmb_res.insertItem(0, want)
            idx = self.cmb_res.findText(want)
        self.cmb_res.setCurrentIndex(idx)
        self.cmb_res.blockSignals(False)

    def _res_changed(self, idx):
        if self._loading:
            return
        txt = self.cmb_res.currentText()
        try:
            w, h = (int(x) for x in txt.split("×"))
        except ValueError:
            return
        self._push_pending()
        self.proj["width"], self.proj["height"] = w, h
        self._refresh_all()

    def _fps_changed(self, v):
        if self._loading:
            return
        self._push_pending()
        self.proj["fps"] = float(v)
        self.timer.setInterval(max(1, int(round(1000.0 / v))))
        self._refresh_all()

    # ---------- 历史（工程快照栈，先改后推约定同 puppet 工作台） ----------
    def _seed_history(self):
        self._hist = [V.clone_project(self.proj)]
        self._hist_i = 0

    def push_history(self):
        snap = V.clone_project(self.proj)
        if self._hist_i >= 0 and json.dumps(self._hist[self._hist_i], sort_keys=True) \
                == json.dumps(snap, sort_keys=True):
            return
        self._hist = self._hist[:self._hist_i + 1]
        self._hist.append(snap)
        if len(self._hist) > 60:
            self._hist.pop(0)
        self._hist_i = len(self._hist) - 1
        self.project_dirty()

    def _push_pending(self):
        """属性面板连续改：先记录当前态再改（撤销能回到改之前）。"""
        self.push_history()

    def undo(self):
        if self._hist_i > 0:
            self._hist_i -= 1
            self._restore_hist()

    def redo(self):
        if self._hist_i < len(self._hist) - 1:
            self._hist_i += 1
            self._restore_hist()

    def _restore_hist(self):
        self.proj = V.clone_project(self._hist[self._hist_i])
        self._sel_clip = None
        self._sources.close_all()
        self._loading = True
        self._apply_project(None)
        self._loading = False
        self._refresh_all()
        _se = getattr(self, "script_editor", None)
        if _se is not None:      # F1：撤销/重做后台词表刷新
            _se.refresh()

    def project_dirty(self):
        self.project_dirty_changed()

    def project_dirty_changed(self):
        pass  # VideoWindow 覆写

    # ---------- 保存 / 打开 ----------
    def save_project(self, ask=False):
        if ask or not self.proj_dir:
            d = QFileDialog.getExistingDirectory(self, "选择工程目录", PROJ_BASE)
            if not d:
                return False
            self.proj_dir = d
            with contextlib.suppress(Exception):
                self.timeline.set_proj_dir(self.proj_dir or "")
        try:
            os.makedirs(self.proj_dir, exist_ok=True)
            with open(os.path.join(self.proj_dir, META_NAME), "w",
                      encoding="utf8") as f:
                json.dump(self.proj, f, ensure_ascii=False, indent=1)
            self._record_recent()
            TOAST.toast_ok(self, "已保存", os.path.basename(self.proj_dir), duration=2000)
            return True
        except Exception as e:
            TOAST.toast_err(self, "保存失败", repr(e), duration=4000)
            return False

    def save_as(self):
        """另存为：选目录 -> 写 video_project.json -> 切 proj_dir -> _record_recent。

        复用 save_project(ask=True) 的落盘逻辑，避免两份写盘代码。
        """
        return self.save_project(ask=True)

    def _record_recent(self):
        """把当前视频工程写进最近列表（去重、置顶、剪掉失效条目）。

        2026-11-23 修：原实现直接 items[:100] 落盘，且不校验目录是否存在。
        视频工程动辄几百 MB，一旦目录被删（临时工程、清理、移动）条目会永远
        留在列表里点不开；欢迎页 load_recent 的存活过滤只在读时生效，不会
        回写清理，于是脏条目会一直跟着文件走。
        这里改成写入前先剔除「目录不存在」的条目，上限对齐主模块 RECENT_MAX
        （原来是硬编码 100，和 l2d_rig_qt.RECENT_MAX 两个数字各改各的）。
        """
        try:
            try:
                items = json.load(open(_recent_read(), encoding="utf8"))
                assert isinstance(items, list)
            except Exception:
                items = []

            # 先剔失效条目（目录没了 / 空 dir），顺便把损坏项丢掉
            alive = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                pd = str(it.get("dir") or "")
                if pd and os.path.isdir(pd):
                    alive.append(it)

            d = os.path.normpath(self.proj_dir)
            alive = [it for it in alive
                     if os.path.normpath(it.get("dir", "")) != d]
            alive.insert(0, {"dir": d, "name": os.path.basename(d),
                             "type": "video", "layers": 0})

            try:
                from l2d_rig_qt import RECENT_MAX as _MAX
            except Exception:
                _MAX = 100
            json.dump(alive[:_MAX], open(RECENT_FILE, "w", encoding="utf8"),
                      ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def is_video_project(d):
        return bool(d) and os.path.isfile(os.path.join(d, META_NAME))

    def load_dir(self, d):
        try:
            obj = json.load(open(os.path.join(d, META_NAME), encoding="utf8"))
        except Exception as e:
            TOAST.toast_err(self, "打开失败", "video_project.json 读取异常：%r" % e, duration=4000)
            return False
        p = V.sanitize_project(obj)
        if p is None:
            TOAST.toast_err(self, "打开失败", "不是合法的视频工程文件", duration=4000)
            return False
        V.normalize_ids(p)
        self._sources.close_all()
        self.proj = p
        self.proj_dir = d
        with contextlib.suppress(Exception):
            self.timeline.set_proj_dir(self.proj_dir or "")
        self._sel_clip = None
        self._loading = True
        self._apply_project(None)
        self._loading = False
        self._seed_history()
        self.seek(0.0)
        return True

    # ================= 素材库 =================
    def _add_files(self):
        fs, _ = QFileDialog.getOpenFileNames(
            self, "添加素材（图片/视频/音频）", "",
            "素材文件 (*.png *.jpg *.jpeg *.bmp *.webp *.tga "
            "*.mp4 *.avi *.mov *.mkv *.webm *.mp3 *.wav *.m4a *.aac *.flac);;"
            "所有文件 (*)")
        self._import_paths(fs)

    def _add_puppet(self):
        d = QFileDialog.getExistingDirectory(self, "选择木偶工程目录",
                                             os.path.join(_HERE, "projects"))
        if d:
            self._import_paths([d])

    def _import_paths(self, paths):
        import video_source as VS
        n = 0
        for p in paths:
            try:
                src = VS.probe_source(p)
            except Exception as e:
                TOAST.toast_warn(self, "跳过素材", "%s\n%r" % (os.path.basename(p), e))
                continue
            if any(a["path"] == src["path"] and a["kind"] == src["kind"]
                   for a in self.assets):
                continue
            self.assets.append(src)
            n += 1
        if n:
            TOAST.toast_ok(self, "已导入", "%d 个素材" % n, duration=2000)
        self._rebuild_asset_cards()

    def _thumb(self, src):
        """卡片缩略图：image/puppet/video 首帧。失败返回 None。"""
        try:
            import video_source as VS
            s = VS.open_source(src)
            try:
                arr = s.frame(0.0)
                if arr.ndim == 3 and arr.shape[2] == 4:
                    # v3.4.0：ImageSource 可返回 RGBA（透明底立绘）；
                    # 缩略图把 alpha 合成到中性灰底再走 RGB888
                    a = arr[:, :, 3:4].astype(np.float32) * (1.0 / 255.0)
                    bg = np.full(arr[:, :, :3].shape, 60.0, np.float32)
                    arr = (arr[:, :, :3].astype(np.float32) * a +
                           bg * (1.0 - a)).astype(np.uint8)
                elif arr.ndim == 2:
                    arr = np.dstack([arr] * 3)
                qimg = QImage(np.ascontiguousarray(arr).data, arr.shape[1],
                              arr.shape[0], 3 * arr.shape[1],
                              QImage.Format_RGB888).copy()
                return QPixmap.fromImage(qimg)
            finally:
                s.close()
        except Exception:
            return None

    def _viewport_y_of(self, widget):
        """widget 在滚动视口里的相对 Y（滚动条拉了多少就减多少）。"""
        try:
            sb = self.asset_scroll.verticalScrollBar()
            return widget.y() - sb.value()
        except Exception:
            return None

    def _find_card_widget(self, pred):
        """在 asset_flow 里找第一个满足 pred 的卡片 widget。"""
        with contextlib.suppress(Exception):
            for i in range(self.asset_flow.count()):
                it = self.asset_flow.itemAt(i)
                w = it.widget() if hasattr(it, "widget") else it
                if w is not None and pred(w):
                    return w
        return None

    def _rebuild_keeping(self, keep_pred):
        """重建卡片，并让满足 keep_pred 的那个卡片保持在原视口位置。"""
        from PySide6.QtCore import QTimer
        w0 = self._find_card_widget(keep_pred)
        vy = self._viewport_y_of(w0) if w0 is not None else None
        self._rebuild_asset_cards()
        if vy is None:
            return

        def _fix():
            w1 = self._find_card_widget(keep_pred)
            if w1 is None:
                return
            with contextlib.suppress(Exception):
                sb = self.asset_scroll.verticalScrollBar()
                sb.setValue(max(0, w1.y() - vy))

        QTimer.singleShot(0, _fix)
        QTimer.singleShot(40, _fix)

    def _rebuild_asset_cards(self):
        """重建左栏卡片。

        重建会清空再添加，FlowLayout 高度瞬时归零又恢复，
        QScrollArea 会把滚动条重置到顶部（用户抱怨「展开列表就跳回去」）。
        这里保存重建前的滚动位置，布局稳定后再恢复。
        """
        from PySide6.QtCore import QTimer
        try:
            _sb = self.asset_scroll.verticalScrollBar()
            _old_pos = _sb.value()
        except Exception:
            _sb = None
            _old_pos = 0

        # 记住重建前视口顶部是「第几个卡片」—— 展开/收起会改变布局高度，
        # 单纯记像素值会在新布局里指向别的内容（实测偏到 161）。
        _anchor_idx = -1
        _anchor_dy = 0
        if _sb is not None and _old_pos > 0:
            try:
                _y = _old_pos
                _acc = 0
                for _i in range(self.asset_flow.count()):
                    _it = self.asset_flow.itemAt(_i)
                    _w = _it.widget() if hasattr(_it, "widget") else _it
                    if _w is None:
                        continue
                    _h = _w.height() or 60
                    if _acc + _h > _y:
                        _anchor_idx = _i
                        _anchor_dy = _y - _acc
                        break
                    _acc += _h
            except Exception:
                _anchor_idx = -1

        self._rebuild_asset_cards_inner()

        if _sb is not None and _old_pos > 0:
            def _restore():
                try:
                    if _anchor_idx >= 0 and _anchor_idx < self.asset_flow.count():
                        # 按「第 N 个卡片」定位：累加它前面各卡高度
                        acc = 0
                        for i in range(_anchor_idx):
                            it = self.asset_flow.itemAt(i)
                            w = it.widget() if hasattr(it, "widget") else it
                            if w is not None:
                                acc += (w.height() or 60)
                        _sb.setValue(max(0, acc - _anchor_dy))
                    else:
                        _sb.setValue(_old_pos)
                except Exception:
                    pass
            QTimer.singleShot(0, _restore)
            QTimer.singleShot(40, _restore)

    def _rebuild_asset_cards_inner(self):
        while self.asset_flow.count():
            it = self.asset_flow.takeAt(0)
            # qfluentwidgets FlowLayout.takeAt 返回的就是 widget 本体（非 QLayoutItem）
            wi = it.widget() if hasattr(it, "widget") else it
            if isinstance(wi, QWidget):
                wi.deleteLater()
        filt = self._asset_tab
        # 「导入模型」只在角色页可见：统一在这里判定，避免每个 return 分支
        # 各写一遍（漏一个就会出现「音频页也能导入模型」这种怪状态）。
        # **必须在 roles() 判空之前设**：空素材库正是最需要导入入口的时点，
        # 早退分支若跳过了它，用户会对着「没有模型」的提示找不到按钮。
        with contextlib.suppress(Exception):
            self.btn_import_model.setVisible(filt == "media")
        # v3.6.2：运镜编辑器只在特效页出现（其他页它没意义，还占掉半屏）
        with contextlib.suppress(Exception):
            if getattr(self, "_cam_panel", None) is not None:
                self._cam_panel.setVisible(filt == "template")
        if filt == "text":
            # F1：文本页 = 剧情编辑器
            self._show_script_editor(True)
            self._show_bg_panel(False)
            return
        self._show_script_editor(False)
        if filt == "bg":
            # v3.6.2：背景页签 —— 切到这一页只显示背景面板
            self._show_bg_panel(True)
            self._rebuild_bg_cards()
            return
        self._show_bg_panel(False)
        if filt == "template":
            # v3.6.2：模板页改成「特效」页 —— 206 个高级演出特效。
            # 原来的新建/演示/导入 WebGAL/运行时四个入口挪到「文件」菜单
            # （用户要求模板换成特效；这些功能本来就该在菜单里）。
            self._rebuild_effect_cards()
            return
        if filt == "media":
            # v3.5.16：媒体页改为「角色」页 —— 按角色分组，展开选模型
            self._rebuild_role_cards()
            return
        if filt == "audio":
            # 音频页 = 素材库音频（用户要求「直接引用包中的，供试听和拖入」）
            self._rebuild_audio_cards()
            return
        if filt == "filter":
            # 滤镜页：列出可用的画面滤镜（此前「特效」页是空的）
            self._rebuild_filter_cards()
            return
        if filt == "transition":
            # 转场页：列出引擎真实支持的转场预设（此前整页是空的）
            self._rebuild_transition_cards()
            return
        for src in self.assets:
            is_audio = src["kind"] == "audio"
            if filt == "media" and is_audio:
                continue
            if filt not in ("audio", "media"):
                continue
            th = self._thumb(src) if src["kind"] in ("image", "video",
                                                     "puppet") else None
            card = _AssetCard(src, th, on_add=self._add_asset_to_timeline,
                              parent=self.asset_host)
            self.asset_flow.addWidget(card)
        self.asset_empty.setVisible(
            not any((self.pivot.currentItem() == "media") == (s["kind"] != "audio")
                    for s in self.assets))

    # ---------- 角色页：角色 → 改模作者 → 模型 ----------
    def _rebuild_role_cards(self):
        """角色页：三级结构 —— 角色（父）→ 改模作者（子）→ 模型卡。

        用户要求「父分类用角色名，子分类用改模作者」：
          ① 先列角色，点开一个角色；
          ② 角色展开后按作者分组（作者标题卡，缩进一级）；
          ③ 点作者展开它名下的模型卡（缩略图）。
        角色同一时刻只展开一个（列表短、找得快）；作者可同时展开多个，
        方便同一角色跨作者对比，展开状态按 (角色, 作者) 记住。
        """
        roles = CL.roles()
        if not roles:
            self.asset_empty.setText(
                "素材库里没有模型\n（确认素材库位置：资产管理里可见；也可以直接点右上角「导入模型」）")
            self.asset_empty.setVisible(True)
            return
        opened = self._role_open
        if opened is not None and not any(r["slug"] == opened for r in roles):
            opened = self._role_open = None
        # 角色变了就清掉上一角色的作者展开记录
        if opened != getattr(self, "_author_open_role", None):
            self._author_open = set()
            self._author_open_role = opened
        open_auth = getattr(self, "_author_open", None)
        if open_auth is None:
            open_auth = self._author_open = set()

        for r in roles:
            exp = (r["slug"] == opened)
            hdr = _RoleHeaderCard(
                r, expanded=exp,
                on_toggle=lambda slug, _r=r: self._toggle_role(slug),
                parent=self.asset_host)
            self.asset_flow.addWidget(hdr)
            if not exp:
                continue
            for g in CL.authors_of(r["slug"]):
                a = g["author"]
                aexp = (a in open_auth)
                self.asset_flow.addWidget(_AuthorHeaderCard(
                    a, len(g["items"]), expanded=aexp,
                    on_toggle=lambda au, _s=r["slug"]: self._toggle_author(au),
                    parent=self.asset_host))
                if not aexp:
                    continue
                for m in g["items"]:
                    pm = self._model_thumb_pixmap(m)
                    card = _ModelCard(m, pm, on_add=self._model_to_timeline,
                                      on_add_persona=self._model_to_timeline_persona,
                                      parent=self.asset_host)
                    self.asset_flow.addWidget(card)
        self.asset_empty.setVisible(False)

    # ---------- 导入模型（用户需求：命名 / 分类 / 改模人 / 缩略图）----------
    def _import_model(self):
        """打开导入对话框，把模型复制进素材库后刷新角色页。"""
        presets = getattr(self, "_role_open", "") or ""
        dlg = _ImportModelDialog(self, preset_role=presets)
        if not dlg.exec():
            return
        src, slug, author, costume, tmode, tsrc = dlg.values()
        # 名字含 Windows 非法字符时先挡下来（目录名建出来就删不掉）
        if not CL._slug_of(costume):
            TOAST.toast_warn(self, "命名不合法", "命名里没有可用字符，换一个")
            return
        ok, msg, entry = CL.add_model(src, slug, author, costume,
                                      thumb_mode=tmode, thumb_src=tsrc)
        if not ok:
            TOAST.toast_err(self, "导入失败", msg, duration=5000)
            return
        # 缓存里旧缩略图作废（新目录可能已有 _thumb.png）
        with contextlib.suppress(Exception):
            self._thumb_cache = {}
        # 导入后自动展开到该角色、该作者，让用户立刻看到新模型
        with contextlib.suppress(Exception):
            self._role_open = slug
            self._author_open = {author} if author else {""}
            self._author_open_role = slug
        self._rebuild_asset_cards()
        TOAST.toast_ok(self, "导入完成", msg, duration=3000)
        # 缩略图渠道①「导入后渲染」：这里只负责如实告知，不假装成功
        if tmode == "render":
            self._render_imported_thumb(entry)

    def _render_imported_thumb(self, entry):
        """导入后渲染缩略图（渠道①）。

        渲染需要 WebEngine + WebGAL 运行时 + Cubism SDK 三者齐全。缺任何
        一样都**如实告知**并保留占位，不谎报成功——用户看到空图比看到
        「已渲染」的假提示要好。

        实测（2026-11-23）：这条路能真出画面——渲染 332x607 RGBA，
        立绘区 38503 种颜色，归一后 _thumb.png 360x480 / 155KB。
        """
        try:
            import l2d_renderer as LR
        except Exception as e:
            TOAST.toast_warn(self, "无法渲染缩略图",
                             "渲染模块不可用：%s" % e, duration=4000)
            return
        try:
            import webgal_preview as WP
            has_we = getattr(WP, "HAS_WEBENGINE", False)
            has_rt = bool(WP._find_runtime_es(WP.RUNTIME_DIR))
            has_sdk = bool(WP._find_lib_dir())
        except Exception:
            has_we = has_rt = has_sdk = False
        if not (has_we and has_rt and has_sdk):
            miss = []
            if not has_we: miss.append("WebEngine 能力包")
            if not has_rt: miss.append("WebGAL 运行时")
            if not has_sdk: miss.append("Cubism SDK")
            TOAST.toast_warn(self, "无法渲染缩略图",
                             "缺少：%s\n可在资产管理里补齐，或导入时直接选一张图"
                             % "、".join(miss), duration=5000)
            return
        try:
            info = entry or {}
            d = CL.model_path(info)
            if not d or not os.path.isdir(d):
                TOAST.toast_warn(self, "无法渲染缩略图", "模型目录不存在")
                return
            ok = self._grab_model_thumb(d)
            if ok:
                TOAST.toast_ok(self, "缩略图已生成", "渲染完成", duration=2000)
            else:
                TOAST.toast_warn(self, "渲染未成功",
                                 "抓图失败，可稍后重试或直接选一张图",
                                 duration=4000)
        except Exception as e:
            TOAST.toast_warn(self, "渲染失败", str(e)[:120], duration=4000)

    def _grab_model_thumb(self, model_dir):
        """渲染单帧存成 <model_dir>/_thumb.png。返回 True/False。

        **走 l2d_renderer，不走 webgal_preview.grab_shot**：
        view.grab() 抓的是 QWidget 控件画面，抓不到 WebGL canvas 的内容
        （实测抓到 1280x720 全是窗口底色，唯一色=1；l2d_renderer._grab()
        的注释里记着这条坑）。l2d_renderer 走页面侧 canvas.toDataURL()
        （宿主页 PIXI 开了 preserveDrawingBuffer），拿到的是**透明底立绘本体**。

        渲染器是进程级单例（一个进程只能有一个 QWebEngineView 在跑渲染），
        所以用 shared_renderer()，不自己 new。
        """
        try:
            import l2d_renderer as LR
            r = LR.shared_renderer()
            if r is None:
                return False
            ref = r.register(model_dir)
            if not ref:
                return False
            img = r.render({"ref": ref, "modelId": "thumb1",
                            "layout": (0.5, 0.5, 1.0, 0.0),
                            "motion": "", "expression": ""}, timeout=40.0)
            if img is None:
                return False
            # 画面是空的（只有透明背景）不算成功
            if int((img[:, :, 3] > 8).sum()) < 100:
                return False
            import model_thumb as _MT
            from PIL import Image
            import io as _io
            buf = _io.BytesIO()
            Image.fromarray(img, "RGBA").save(buf, "PNG")
            raw = _MT.crop_png_bytes(buf.getvalue())
            with open(_MT.thumb_file(model_dir), "wb") as f:
                f.write(raw)
            return os.path.isfile(_MT.thumb_file(model_dir))
        except Exception:
            return False

    def _toggle_role(self, slug):
        """展开/收起某角色（同一时刻只展开一个）。

        重建后要让被点的角色标题停在原位（需求池第2条），
        否则列表会跳回顶部、用户找不到刚才点的那个角色。
        """
        self._role_open = None if self._role_open == slug else slug
        # 换角色 => 作者展开状态归零（新角色的作者组合不同）
        self._author_open = set()
        self._author_open_role = self._role_open
        self._rebuild_keeping(
            lambda w: (type(w).__name__ == "_RoleHeaderCard"
                       and getattr(w, "role", {}).get("slug") == slug))

    def _toggle_author(self, author):
        """展开/收起某个作者分组（可同时展开多个）。"""
        s = getattr(self, "_author_open", None)
        if s is None:
            s = self._author_open = set()
        if author in s:
            s.discard(author)
        else:
            s.add(author)
        self._rebuild_keeping(
            lambda w: (type(w).__name__ == "_AuthorHeaderCard"
                       and getattr(w, "author", None) == author))

    def _model_thumb_pixmap(self, m):
        """模型缩略图 QPixmap（带缓存）。读不到返回 None。"""
        cache = getattr(self, "_thumb_cache", None)
        if cache is None:
            cache = self._thumb_cache = {}
        key = m.get("file") or m.get("id")
        if key in cache:
            return cache[key]
        pm = None
        try:
            p = CL.thumb_path(m)
            if p and os.path.isfile(p):
                pm = QPixmap(p)
                if pm.isNull():
                    pm = None
        except Exception:
            pm = None
        cache[key] = pm
        return pm

    def _model_to_timeline(self, card):
        """把选中的模型作为 puppet 素材加到时间轴（引用素材库目录）。"""
        m = card.model
        d = CL.model_path(m)
        if not d or not os.path.isdir(d):
            TOAST.toast_warn(self, "加入失败", "模型目录不存在，请检查素材库。")
            return
        try:
            src = V.new_source("puppet", d, src_dur=0.0)
        except Exception as e:
            TOAST.toast_warn(self, "加入失败", str(e))
            return
        with contextlib.suppress(Exception):
            src["lib_file"] = m.get("file")
            src["lib_name"] = m.get("name")
            src["lib_author"] = m.get("author") or ""
            src["lib_role"] = m.get("slug")
        self._add_asset_to_timeline(src)
        TOAST.toast_ok(self, "已加入", CL.display_name(m), duration=1800)

    def _model_to_timeline_persona(self, card):
        """把模型作为「新人格」加入：建一条独立视频轨（新时间轴）再放片段。

        需求「同一角色有不同人格，需要多时间轴，同场景两个相同角色同屏」：
        同轨不允许重叠，所以两份同角色必须分轨。这里为该角色新建一条
        以角色名命名的视频轨，把片段放进去 —— 多条视频轨在合成时从底
        到顶叠加，自然形成同屏多份相同角色，各自可独立调位置/动作/时长。
        """
        m = card.model
        d = CL.model_path(m)
        if not d or not os.path.isdir(d):
            TOAST.toast_warn(self, "加入失败", "模型目录不存在，请检查素材库。")
            return
        try:
            src = V.new_source("puppet", d, src_dur=0.0)
        except Exception as e:
            TOAST.toast_warn(self, "加入失败", str(e))
            return
        with contextlib.suppress(Exception):
            src["lib_file"] = m.get("file")
            src["lib_name"] = m.get("name")
            src["lib_author"] = m.get("author") or ""
            src["lib_role"] = m.get("slug")
        # 为该角色建一条独立视频轨，命名体现「人格/副本」
        role_cn = m.get("role_cn") or m.get("slug") or "角色"
        ok, _m, tid = V.add_track(self.proj, "video", name=None,
                                  aid=getattr(self, "_cur_aid", None))
        if not ok:
            TOAST.toast_warn(self, "建轨失败", _m)
            return
        tr = V.find_track(self.proj, tid,
                          aid=getattr(self, "_cur_aid", None))
        if tr is not None:
            n = len([t for t in V._tracks_of(self.proj,
                         getattr(self, "_cur_aid", None))
                     if t["kind"] == "video"
                     and str(t.get("name") or "").startswith(role_cn)]) + 1
            tr["name"] = "%s·人格%d" % (role_cn, n)
        self._add_asset_to_timeline(src, track_id=tid)
        TOAST.toast_ok(self, "已作为新人格加入", "%s（独立时间轴）" % role_cn, duration=1800)

    # ---------- 音频页：素材库音频试听 + 上轨 ----------
    def _rebuild_audio_cards(self):
        """渲染音频页：默认只列分组标题，点开才画该组卡片。

        性能：一次性建 321 张卡要 590ms + 布局 340ms（实测），
        用户体感是「点了没反应」。改成分组折叠后，首屏只建 8~9 张
        标题卡（<10ms），展开一组最多约 60ms。
        """
        groups = AL.groups()
        if not groups:
            self.asset_empty.setText(
                "素材库里没有音频\n（确认素材库位置：资产管理里可见）")
            self.asset_empty.setVisible(True)
            return
        opened = self._audio_open
        if opened is not None and not any(g["id"] == opened for g in groups):
            opened = self._audio_open = None
        for g in groups:
            exp = (g["id"] == opened)
            hdr = _AudioGroupCard(
                g, expanded=exp,
                on_toggle=lambda gid: self._toggle_audio_group(gid),
                parent=self.asset_host)
            self.asset_flow.addWidget(hdr)
            if not exp:
                continue
            for it in g["items"]:
                card = _AudioCard(it, on_add=self._audio_item_to_timeline,
                                  on_preview=self._toggle_audio_preview,
                                  parent=self.asset_host)
                self.asset_flow.addWidget(card)
        self.asset_empty.setVisible(False)

    def _toggle_audio_group(self, gid):
        """展开/收起某个音频分组（同一时刻只展开一组），标题保持原位。"""
        self._audio_open = None if self._audio_open == gid else gid
        self._rebuild_keeping(
            lambda w: (type(w).__name__ == "_AudioGroupCard"
                       and getattr(w, "group", {}).get("id") == gid))

    def _ensure_audio_player(self):
        """懒建 QMediaPlayer。QtMultimedia 缺失时返回 None（不影响上轨）。"""
        if self._audio_player is not None:
            return self._audio_player
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PySide6.QtCore import QUrl
            p = QMediaPlayer(self)
            out = QAudioOutput(self)
            out.setVolume(0.8)
            p.setAudioOutput(out)
            p.playbackStateChanged.connect(self._on_preview_state)
            self._audio_player = p
            self._audio_out = out
            return p
        except Exception:
            return None

    def _on_preview_state(self, st):
        """播放结束/停止时把卡片图标复原。"""
        with contextlib.suppress(Exception):
            from PySide6.QtMultimedia import QMediaPlayer
            if st == QMediaPlayer.PlaybackState.StoppedState:
                if self._previewing is not None:
                    self._previewing.set_playing(False)
                    self._previewing = None

    def _toggle_audio_preview(self, card):
        """试听：点同一条=停止，点别的=切过去。"""
        pl = self._ensure_audio_player()
        if pl is None:
            TOAST.toast_warn(self, "无法试听", "缺少 QtMultimedia 组件，但仍可加入时间轴。")
            return
        from PySide6.QtCore import QUrl
        if self._previewing is card:
            pl.stop()
            card.set_playing(False)
            self._previewing = None
            return
        if self._previewing is not None:
            with contextlib.suppress(Exception): self._previewing.set_playing(False)
        path = AL.audio_path(card.item)
        if not path or not os.path.isfile(path):
            TOAST.toast_warn(self, "试听失败", "音频文件不在素材库里：%s" % card.item)
            return
        pl.setSource(QUrl.fromLocalFile(path))
        pl.play()
        card.set_playing(True)
        self._previewing = card

    def _stop_audio_preview(self):
        if self._audio_player is not None:
            with contextlib.suppress(Exception): self._audio_player.stop()
        if self._previewing is not None:
            with contextlib.suppress(Exception): self._previewing.set_playing(False)
            self._previewing = None

    def _audio_item_to_timeline(self, card):
        """把素材库音频作为 source 加到音频轨（引用素材库原文件）。"""
        it = card.item
        path = AL.audio_path(it)
        if not path or not os.path.isfile(path):
            TOAST.toast_warn(self, "加入失败", "音频文件不在素材库里，请检查素材库位置。")
            return
        dur = float(it.get("dur") or 0.0)
        try:
            src = V.new_source("audio", path, src_dur=dur)
        except Exception as e:
            TOAST.toast_warn(self, "加入失败", str(e))
            return
        # 记下它来自素材库，导出时可直接复用相对路径
        with contextlib.suppress(Exception):
            src["lib_file"] = it.get("file")
            src["lib_name"] = it.get("name")
        was = self._asset_tab
        self._add_asset_to_timeline(src)
        self._asset_tab = was

    # ---------- 滤镜页 ----------
    def _rebuild_filter_cards(self):
        """滤镜页：列出画面滤镜预设（点卡片=应用到选中 clip / 整片）。

        滤镜以 setTransform 参数形式存 clip["filter"]，导出器负责写成
        WebGAL 的 setTransform 行；预览侧走 video_export 的合成。
        """
        presets = self._filter_presets()
        for name, desc, fn in presets:
            card = _TemplateCard(name, desc, fn, self.asset_host)
            self.asset_flow.addWidget(card)
        self.asset_empty.setVisible(False)

    def _filter_presets(self):
        """滤镜预设表：(名字, 说明, 应用回调)。

        v3.6.2：改为《webgal电影感滤镜1.21》的 17 个电影感预设（源自
        B站北风的猫 5306 整合包，作者见 film_filters.CREDIT_*）。
        每个预设是「背景 + 人物」成对参数——电影感靠两层配合，只调一层
        会脱节。应用时按 clip 所在轨类型分别取 bg / fig 参数。
        """
        import film_filters as FF
        out = [("原片", "不做任何处理", lambda: self._apply_filter(None))]
        for f in FF.FILM_FILTERS:
            desc = f.get("desc") or ""
            credit = f.get("credit") or ""
            if credit:
                desc = "%s｜%s" % (desc, credit)
            out.append((f["name"], desc,
                        (lambda name=f["name"]:
                         self._apply_film_filter(name))))
        return out

    def _clip_is_bg(self, clip, tr):
        """该 clip 是否算「背景层」（决定套用 bg 还是 fig 参数）。"""
        try:
            name = str((tr or {}).get("name") or "").upper()
            if name.startswith("BG"):
                return True
            src = clip.get("source") or {}
            lay = V.layout_at(clip)
            # 满幅 + cover + 无 alpha = 背景（与导出器口径一致）
            return bool(lay.get("cover")) and src.get("kind") == "image"
        except Exception:
            return False

    def _apply_film_filter(self, name):
        """把电影感预设套到目标 clip(s)：背景用 bg 参数、立绘用 fig 参数。"""
        import film_filters as FF
        preset = FF.by_name(name)
        if preset is None:
            return
        # v3.6.2：记下"当前滤镜"，供批量工具条的「批量套当前滤镜」使用。
        # 存的是合成后的单层参数（bg/fig 在批量时按 clip 类型各自取——这里
        # 退化成"人物层优先"，因为批量套用最常见的目的就是统一人物观感）。
        with contextlib.suppress(Exception):
            self._cur_filter_preset = (preset.get("fig")
                                       or preset.get("bg") or None)
            self._cur_filter_name = name
        cid = getattr(self, "_sel_clip", None)
        targets = []
        want_bg = preset.get("bg")
        want_fig = preset.get("fig")
        # 先定应用范围：有选中就只作用于选中 clip，否则整幕可见视频 clip
        if cid:
            try:
                tr, c = V.find_clip(self.proj, cid)
            except Exception:
                tr, c = None, None
            if isinstance(c, dict):
                targets = [(tr, c)]
        if not targets:
            for tr in V._tracks_of(self.proj,
                                   getattr(self, "_cur_aid", None)):
                if tr.get("kind") != "video" or tr.get("hidden"):
                    continue
                for c in (tr.get("clips") or []):
                    targets.append((tr, c))
        if not targets:
            TOAST.toast_warn(self, "没有可应用的对象", "先选中一个片段，或让时间轴上有画面片段。", duration=2500)
            return
        n_bg = n_fig = 0
        for tr, c in targets:
            is_bg = self._clip_is_bg(c, tr)
            filt = want_bg if is_bg else want_fig
            if not filt:
                continue
            c["filter"] = dict(filt)
            if is_bg:
                n_bg += 1
            else:
                n_fig += 1
        self.push_history()
        self.timeline.refresh()
        self._after_edit()
        parts = []
        if n_bg:
            parts.append("背景 %d 个" % n_bg)
        if n_fig:
            parts.append("人物 %d 个" % n_fig)
        if not parts:
            TOAST.toast_warn(self, "该滤镜不适用",
                       "「%s」只调%s层，当前没有对应片段。"
                            % (name, "背景" if want_bg else "人物"))
            return
        TOAST.toast_ok(self, "滤镜已应用", "%s：%s" % (name, "，".join(parts)), duration=2500)

    def _apply_filter(self, filt):
        """把滤镜写到目标 clip（有选中写选中，否则整幕所有可见 clip）。"""
        targets = []
        cid = getattr(self, "_sel_clip", None)
        if cid:
            # find_clip 返回 (track, clip) 二元组，取 clip；取不到就不加
            try:
                _tr, _c = V.find_clip(self.proj, cid)
            except Exception:
                _tr, _c = None, None
            if isinstance(_c, dict):
                targets = [_c]
        if not targets:
            for tr in self.proj.get("tracks", []):
                if tr.get("kind") != "video" or tr.get("hidden"):
                    continue
                for c in (tr.get("clips") or []):
                    targets.append(c)
        if not targets:
            TOAST.toast_warn(self, "没有可应用的对象", "先选中一个片段，或让时间轴上有画面片段。", duration=2500)
            return
        for c in targets:
            if filt is None:
                c.pop("filter", None)
            else:
                c["filter"] = dict(filt)
        self.push_history()
        self.timeline.refresh()
        self._after_edit()
        TOAST.toast_ok(self, "滤镜已应用", "清除滤镜" if filt is None
             else "应用到 %d 个片段" % len(targets),
                 duration=2000)

    # ---------- 已应用的滤镜 / 特效（删除管理） ----------
    def _fx_effect_label(self, ef):
        """把一条 effects 指令折成便于阅读的「名字 + 关键参数」摘要。"""
        import re as _re
        cmd = str((ef or {}).get("cmd") or "")
        try:
            t = float((ef or {}).get("t") or 0.0)
        except (TypeError, ValueError):
            t = 0.0
        head = cmd.split(":", 1)[0].strip() if ":" in cmd else cmd[:16]
        tm = _re.search(r"-target=(\S+?)(?:\s|;|$)", cmd)
        target = tm.group(1) if tm else ""
        # 参数摘要：挑几个最能说明效果的键
        params = ""
        m = _re.search(r"[{\(\[](.+?)[\}\)\]]", cmd, _re.S)
        if m:
            body = m.group(1)
            keys = _re.findall(r'"([A-Za-z]+)"\s*:', body)
            show = [k for k in keys
                    if k in ("brightness", "contrast", "saturation", "gamma",
                             "bevel", "bloom", "blur", "oldFilm", "glitchFilm",
                             "rgbFilm", "dotFilm", "scale", "shakePower",
                             "position", "alpha", "colorRed")]
            params = ", ".join(show[:4])
        if head == "setTempAnimation":
            kind = "镜头动画"
        elif head == "setTransform":
            kind = "滤镜"
        else:
            kind = head
        tgt_txt = {"bg-main": "背景", "fig-center": "立绘中",
                   "fig-left": "立绘左", "fig-right": "立绘右"}.get(
                       target, target)
        parts = [kind]
        if tgt_txt:
            parts.append(tgt_txt)
        if params:
            parts.append(params)
        return " · ".join(parts), "%.2fs" % t

    def _refresh_fx_panel(self):
        """重建「已应用的滤镜 / 特效」列表（工程属性面板里）。

        v3.6.5 性能（用户报「加载滤镜很卡」）：原来每次 _after_edit 都
        无条件清空并重建所有行 —— 220 条特效 = 220 个 _FxRow QWidget 的
        创建与销毁，这才是卡顿的真正来源（不是渲染，渲染实测只 +0.13ms）。

        现在先算「内容签名」（特效条数 + 每条 cmd 摘要 + 滤镜 clip id/参数），
        签名没变就直接 return，不碰任何控件。纯播放/拖播放头/改无关属性
        等大量 _after_edit 调用都会命中，只有真增删改才重建。
        """
        if not hasattr(self, "fx_count_lbl"):
            return
        aid = getattr(self, "_cur_aid", None)
        effects = list(V.act_view(self.proj, aid).get("effects") or [])
        # 片段上的滤镜
        clips_with_filter = []
        for tr in V._tracks_of(self.proj, aid):
            for c in (tr.get("clips") or []):
                f = c.get("filter")
                if isinstance(f, dict) and f:
                    clips_with_filter.append((tr, c, f))

        # ---- 内容签名：变了才重建（幂等短路，最大收益点）----
        try:
            sig = (
                len(effects),
                tuple(str(ef.get("cmd") or "")[:80] for ef in effects),
                tuple((str(c.get("id")), tuple(sorted(f.keys())))
                      for _t, c, f in clips_with_filter),
            )
        except Exception:
            sig = None
        if sig is not None and sig == getattr(self, "_fx_panel_sig", None):
            return
        self._fx_panel_sig = sig

        # ---- 清空旧行 ----
        for lay in (getattr(self, "fx_rows_lay", None),
                    getattr(self, "flt_rows_lay", None)):
            if lay is None:
                continue
            while lay.count():
                it = lay.takeAt(0)
                w = it.widget() if hasattr(it, "widget") else it
                if isinstance(w, QWidget):
                    w.deleteLater()

        n_eff = len(effects)
        n_flt = len(clips_with_filter)
        self.fx_count_lbl.setText("特效 %d · 滤镜 %d" % (n_eff, n_flt))
        empty = (n_eff == 0 and n_flt == 0)
        self.fx_empty_lbl.setVisible(empty)
        self.fx_rows_host.setVisible(n_eff > 0)
        self.flt_rows_host.setVisible(n_flt > 0)
        self.btn_clear_effects.setEnabled(n_eff > 0)
        self.btn_clear_filters.setEnabled(n_flt > 0)

        # ---- 特效行 ----
        for idx, ef in enumerate(effects):
            title, sub = self._fx_effect_label(ef)
            cmd = str(ef.get("cmd") or "")
            row = _FxRow(title, sub,
                         on_remove=(lambda i=idx: self._remove_effect(i)),
                         parent=self.fx_rows_host,
                         tooltip=cmd[:300])
            self.fx_rows_lay.addWidget(row)

        # ---- 滤镜行 ----
        for tr, c, f in clips_with_filter:
            # 参数摘要
            keys = [k for k in ("brightness", "contrast", "saturation", "gamma",
                                "bevel", "bloom", "blur", "colorRed",
                                "colorGreen", "colorBlue", "oldFilm",
                                "glitchFilm", "rgbFilm", "dotFilm")
                    if k in f]
            src = (c.get("source") or {})
            nm = os.path.basename(str(src.get("path") or "").rstrip("\\/")) \
                or str(c.get("id") or "")
            sub = "%s · %.2fs" % (nm[:18], float(c.get("start") or 0.0))
            row = _FxRow("滤镜 · " + (", ".join(keys[:5]) or "（空）"), sub,
                         on_remove=(lambda cid=c.get("id"):
                                    self._remove_clip_filter(cid)),
                         parent=self.flt_rows_host)
            self.flt_rows_lay.addWidget(row)

    def _remove_effect(self, idx):
        """删掉 proj["effects"] 里第 idx 条（当前幕）。"""
        aid = getattr(self, "_cur_aid", None)
        view = V.act_view(self.proj, aid)
        effs = view.get("effects")
        if not isinstance(effs, list) or not (0 <= idx < len(effs)):
            return
        try:
            name, _sub = self._fx_effect_label(effs[idx])
            del effs[idx]
        except Exception:
            return
        self.push_history()
        self._after_edit()
        self.render_frame(self._t)
        self._refresh_fx_panel()
        TOAST.toast_ok(self, "已删除特效", name or "特效已移除", duration=1800)

    def _remove_clip_filter(self, clip_id):
        """删掉某片段上的滤镜。"""
        try:
            _tr, c = V.find_clip(self.proj, clip_id)
        except Exception:
            _tr, c = None, None
        if not isinstance(c, dict) or "filter" not in c:
            return
        c.pop("filter", None)
        self.push_history()
        self._after_edit()
        self.render_frame(self._t)
        self._refresh_fx_panel()
        TOAST.toast_ok(self, "已删除滤镜", "片段滤镜已清除", duration=1800)

    def _clear_all_effects(self):
        """清空当前幕全部特效（二次确认）。"""
        aid = getattr(self, "_cur_aid", None)
        view = V.act_view(self.proj, aid)
        effs = view.get("effects")
        if not isinstance(effs, list) or not effs:
            return
        n = len(effs)
        box = MessageBox("清空所有特效",
                         "确定删除当前幕的全部 %d 条特效指令吗？\n"
                         "（可用撤销恢复）" % n, self)
        if not box.exec():
            return
        effs.clear()
        self.push_history()
        self._after_edit()
        self.render_frame(self._t)
        self._refresh_fx_panel()
        TOAST.toast_ok(self, "已清空", "删除 %d 条特效" % n, duration=2000)

    def _clear_all_filters(self):
        """清空所有片段上的滤镜（二次确认）。"""
        aid = getattr(self, "_cur_aid", None)
        hits = []
        for tr in V._tracks_of(self.proj, aid):
            for c in (tr.get("clips") or []):
                if isinstance(c.get("filter"), dict) and c["filter"]:
                    hits.append(c)
        if not hits:
            return
        n = len(hits)
        box = MessageBox("清空所有滤镜",
                         "确定删除 %d 个片段上的滤镜吗？\n"
                         "（可用撤销恢复）" % n, self)
        if not box.exec():
            return
        for c in hits:
            c.pop("filter", None)
        self.push_history()
        self._after_edit()
        self.render_frame(self._t)
        self._refresh_fx_panel()
        TOAST.toast_ok(self, "已清空", "删除 %d 个片段的滤镜" % n, duration=2000)

    # ---------- 背景栏数据（背景页签用；页面在左栏内） ----------
    def _load_bg_items(self):
        """读素材库 asset_index 里 kind=bg 的条目（首次调用时缓存）。"""
        if self._bg_items:
            return self._bg_items
        try:
            import asset_paths as AP
            import json as _json
            root = AP.ensure_library(scan=False)
            if not root:
                return []
            p = os.path.join(root, "asset_index.json")
            with open(p, encoding="utf-8-sig") as f:
                idx = _json.load(f)
        except Exception:
            return []
        out = []
        for e in idx:
            if not isinstance(e, dict) or e.get("kind") != "bg":
                continue
            rel = str(e.get("file") or "").replace("\\", "/")
            if not rel:
                continue
            full = os.path.join(root, rel.replace("/", os.sep))
            if not os.path.isfile(full):
                continue
            # 分类 = 父目录名（"background/商店/xx.webp" -> "商店"）
            parts = rel.split("/")
            cat = parts[-2] if len(parts) >= 3 else "未分类"
            # 二级目录（"学校、工作/羽丘"）合并成 "学校、工作·羽丘"
            if len(parts) >= 4:
                cat = "%s·%s" % (parts[-3], parts[-2])
            out.append({"full": full, "rel": rel, "name": str(e.get("name") or ""),
                        "cat": cat})
        self._bg_items = out
        return out

    def _bg_categories(self):
        """背景分类列表（按条目数降序）。"""
        from collections import Counter
        c = Counter(b["cat"] for b in self._load_bg_items())
        return [k for k, _ in c.most_common()]

    def _rebuild_bg_cards(self, *_a):
        """渲染背景页：**按分类折叠**（和角色页同款交互）。

        用户反馈「背景按钮点击太慢」——原来一次建 400 张缩略图卡（每张
        都要 QImageReader 解码），切页要几秒。改成分组折叠：
          · 首屏只建分类标题卡（39 个，几十 ms）
          · 点开某分类才解码并建该分类的缩略图（最多几十张）
          · 同一时刻只展开一个分类（与角色页一致，方便找）
        分类下拉同步：选中某分类等价于展开它。
        """
        items = self._load_bg_items()
        if not items:
            self.bg_empty.setText("素材库里没有背景图\n"
                                  "（资产管理里确认素材库位置）")
            self.bg_empty.setVisible(True)
            self.bg_count.setText("")
            return
        # 首次填充分类下拉（除「全部」外按条数降序）
        if self.bg_combo.count() <= 1:
            with contextlib.suppress(Exception):
                self.bg_combo.blockSignals(True)
                for cat in self._bg_categories():
                    self.bg_combo.addItem(cat)
                self.bg_combo.blockSignals(False)
        # 清空
        while self.bg_flow.count():
            it = self.bg_flow.takeAt(0)
            w = it.widget() if hasattr(it, "widget") else it
            if isinstance(w, QWidget):
                w.deleteLater()
        # 分类 -> 条目
        from collections import Counter, OrderedDict
        groups = OrderedDict()
        for b in items:
            groups.setdefault(b["cat"], []).append(b)
        # 打开状态：下拉选了具体分类 -> 展开它；否则用点击记忆
        sel = ""
        try:
            sel = self.bg_combo.currentText() if self.bg_combo.count() else ""
        except Exception:
            sel = ""
        opened = getattr(self, "_bg_open", None)
        if opened is None:
            opened = self._bg_open = None
        if sel and sel not in ("", "全部"):
            opened = sel
            self._bg_open = sel
        elif sel in ("", "全部"):
            pass          # 保持点击记忆
        self.bg_count.setText("%d 张 / %d 类" % (len(items), len(groups)))
        for cat, arr in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            exp = (cat == opened)
            self.bg_flow.addWidget(_BgCateCard(cat, len(arr), expanded=exp,
                                               on_toggle=self._toggle_bg_cat,
                                               parent=self.bg_host))
            if not exp:
                continue
            for b in arr:
                pm = self._bg_thumb(b["full"])
                self.bg_flow.addWidget(_BgCard(b, pm,
                                               on_click=self._apply_background,
                                               parent=self.bg_host))
        self.bg_empty.setVisible(False)

    def _toggle_bg_cat(self, cat):
        """展开/收起某个背景分类（同一时刻只展开一个，与角色页一致）。"""
        self._bg_open = None if getattr(self, "_bg_open", None) == cat else cat
        # 下拉回到「全部」——展开状态由 _bg_open 表达，避免两处状态打架
        with contextlib.suppress(Exception):
            if self.bg_combo.currentIndex() != 0:
                self.bg_combo.blockSignals(True)
                self.bg_combo.setCurrentIndex(0)
                self.bg_combo.blockSignals(False)
        self._rebuild_bg_keeping(cat)

    def _rebuild_bg_keeping(self, cat):
        """重建背景页，并让被点的分类标题卡停在原视口位置。"""
        from PySide6.QtCore import QTimer
        w0 = None
        for i in range(self.bg_flow.count()):
            it = self.bg_flow.itemAt(i)
            w = it.widget() if hasattr(it, "widget") else it
            if type(w).__name__ == "_BgCateCard" and getattr(w, "cat", None) == cat:
                w0 = w
                break
        vy = None
        if w0 is not None:
            try:
                # 用 mapTo 拿视口内 y；QPoint 未在本模块导入，用 Qt 的
                # globalPos 换算（等价且不加 import）
                from PySide6.QtCore import QPoint as _QP
                vy = w0.mapTo(self.bg_scroll.viewport(), _QP(0, 0)).y()
            except Exception:
                vy = None
        self._rebuild_bg_cards()
        if vy is None:
            return

        def _fix():
            for i in range(self.bg_flow.count()):
                it = self.bg_flow.itemAt(i)
                w = it.widget() if hasattr(it, "widget") else it
                if type(w).__name__ == "_BgCateCard" \
                        and getattr(w, "cat", None) == cat:
                    with contextlib.suppress(Exception):
                        sb = self.bg_scroll.verticalScrollBar()
                        sb.setValue(max(0, w.y() - vy))
                    return
        QTimer.singleShot(0, _fix)
        QTimer.singleShot(40, _fix)

    def _bg_thumb(self, path):
        cache = getattr(self, "_bg_thumb_cache", None)
        if cache is None:
            cache = self._bg_thumb_cache = {}
        if path in cache:
            return cache[path]
        pm = None
        try:
            from PySide6.QtGui import QPixmap, QImageReader
            r = QImageReader(path)
            r.setScaledSize(QSize(96, 54))     # 直接让 Qt 解码到小尺寸（快）
            img = r.read()
            if not img.isNull():
                pm = QPixmap.fromImage(img)
        except Exception:
            pm = None
        cache[path] = pm
        return pm

    def _apply_background(self, bg):
        """点背景卡：把该图设为**当前时刻**生效的背景。

        做法：写一个 cover 满幅 image clip 到最底视频轨，从当前时刻起
        延续到工程末尾（或下一个背景 clip 起点前）。这样既有即时预览，
        导出时也自然变成 changeBg。
        """
        t = float(getattr(self, "_t", 0.0) or 0.0)
        try:
            import video_source as VS
            src = VS.probe_source(bg["full"])
        except Exception as e:
            TOAST.toast_warn(self, "背景加载失败", str(e)[:120])
            return
        # 找/建底轨（BG 命名优先，否则最前可见视频轨）
        tid = None
        aid = getattr(self, "_cur_aid", None)
        for tr in V._tracks_of(self.proj, aid):
            if tr.get("kind") == "video" and not tr.get("hidden") \
                    and str(tr.get("name") or "").upper().startswith("BG"):
                tid = tr.get("id")
                break
        if tid is None:
            ok, _m, tid = V.add_track(self.proj, "video", "BG", aid=aid)
            if not ok:
                TOAST.toast_warn(self, "建立背景轨失败", _m, duration=2500)
                return
        tr = V.find_track(self.proj, tid, aid)
        # 时长：到工程末尾（至少 5s），并让位于当前时刻之后的旧背景 clip
        dur_end = max(V.project_duration(self.proj, aid), t + 5.0)
        dur = max(V.MIN_DUR, dur_end - t)
        clip = V.new_clip(src, t, dur)
        lay = clip["layout"]
        lay["x"], lay["y"], lay["scale"] = 0.5, 0.5, 1.0
        lay["cover"] = True          # 背景铺满（WebGAL changeBg 语义）
        # 与同轨后续背景冲突时，把新 clip 截到下一个起点
        nxt = [float(c.get("start") or 0.0)
               for c in (tr.get("clips") or [])
               if float(c.get("start") or 0.0) > t + 1e-6]
        if nxt:
            clip["dur"] = round(min(float(clip["dur"]), min(nxt) - t), 3)
            if clip["dur"] < V.MIN_DUR:
                clip["dur"] = V.MIN_DUR
        ok, cid = V.add_clip(self.proj, tid, clip, aid=aid)
        if not ok:
            TOAST.toast_warn(self, "背景插入失败", cid, duration=2500)
            return
        self.push_history()
        self.timeline.refresh()
        self.render_frame(self._t)
        self._after_edit()
        TOAST.toast_ok(self, "背景已设置", bg.get("name") or os.path.basename(
            bg["full"]),
                 duration=1800)
    def _rebuild_effect_cards(self):
        """特效页：按分类分组列出 206 个演出特效，点卡片=在当前时刻插入。

        数据源 stage_effects（来自《WebGAL 高级演出特效代码整合》docx）。
        特效是**按时刻生效的指令**，写进 proj["effects"]（[{t,cmd}]），
        导出时按时刻原样 emit 回引擎——这条链路 webgal_import 已经在用
        （导入端把 setTransform/setTempAnimation 留档到 effects）。
        """
        import stage_effects as SE
        cur_aid = getattr(self, "_cur_aid", None)
        # v3.6.2：运镜编辑器是左栏顶部的常驻面板（构建期插入，见 _build），
        # 这里只管下面的现成特效库。编辑器不入流动布局——FlowLayout 会把
        # 整宽面板挤成卡片大小。
        # 分类折叠状态（默认全收起：206 张卡全展开太慢）
        opened = getattr(self, "_fx_open", None)
        if opened is None:
            opened = self._fx_open = set()

        # v3.6.2：**氛围光效**置顶（用官方引擎真支持的滤镜拼出的布光观感）。
        # 它和下面的「206 个演出特效」是两套东西：光效库只有 13 条、每条
        # 是精调过的双层参数（bg+fig），点一下同时作用于两层。
        with contextlib.suppress(Exception):
            import light_effects as LE
            exp_light = "氛围光效" in opened
            self.asset_flow.addWidget(
                _FxCateCard("氛围光效", len(LE.LIGHT_PRESETS),
                            expanded=exp_light,
                            on_toggle=self._toggle_fx_cat,
                            parent=self.asset_host))
            if exp_light:
                for p in LE.LIGHT_PRESETS:
                    self.asset_flow.addWidget(
                        _FxCard({"name": p["name"], "desc": p["desc"],
                                 "kind": "transform", "_light": p},
                                self._apply_light_preset,
                                parent=self.asset_host))

        for cat, cnt in SE.CATEGORIES:
            items = SE.by_category(cat)
            if not items:
                continue
            exp = cat in opened
            self.asset_flow.addWidget(_FxCateCard(cat, len(items), expanded=exp,
                                                  on_toggle=self._toggle_fx_cat,
                                                  parent=self.asset_host))
            if not exp:
                continue
            for e in items:
                self.asset_flow.addWidget(_FxCard(e, self._apply_stage_effect,
                                                  parent=self.asset_host))
        self.asset_empty.setVisible(False)

    def _toggle_fx_cat(self, cat):
        s = getattr(self, "_fx_open", None)
        if s is None:
            s = self._fx_open = set()
        if cat in s:
            s.discard(cat)
        else:
            s.add(cat)
        self._rebuild_keeping(
            lambda w: (type(w).__name__ == "_FxCateCard"
                       and getattr(w, "cat", None) == cat))

    def _apply_stage_effect(self, eff):
        """把特效指令插入当前时刻（写 proj["effects"]）。"""
        t = float(getattr(self, "_t", 0.0) or 0.0)
        codes = [str(c).strip().rstrip(";") for c in (eff.get("code") or [])
                 if str(c).strip()]
        if not codes:
            return
        effs = self.proj.get("effects")
        if not isinstance(effs, list):
            effs = self.proj["effects"] = []
        for c in codes:
            effs.append({"t": round(t, 3), "cmd": c + ";"})
        # effects 需按时刻有序（导出/合成都按序扫）
        with contextlib.suppress(Exception): effs.sort(key=lambda x: float(x.get("t") or 0.0))
        self.push_history()
        self._after_edit()
        TOAST.toast_ok(self, "特效已插入", "%s（%.2fs，%d 条指令）" % (eff.get("name") or "特效", t,
                                     len(codes)),
                 duration=2200)

    # ---------- v3.6.2：多段路径运镜 ----------
    # ---------- v3.6.2：批量脚本操作 ----------
    # ---------- v3.6.2：氛围光效 / 人物投影 ----------
    def _apply_light_preset(self, eff):
        """把一条氛围光效预设落到当前时刻（背景 + 立绘各一条 setTransform）。

        与「206 个演出特效」的区别：那些是单条指令、单一 target；光效预设
        是**成对**的（bg 参数给背景、fig 参数给立绘），因为光效要让两层
        协调才像"打光"，只调一层会脱节。
        """
        import light_effects as LE
        p = (eff or {}).get("_light") or LE.by_name((eff or {}).get("name"))
        if not p:
            return
        t = float(getattr(self, "_t", 0.0) or 0.0)
        effs = self.proj.get("effects")
        if not isinstance(effs, list):
            effs = self.proj["effects"] = []
        n = 0
        target = p.get("target", "both")
        for layer, tgt in (("bg", "bg-main"), ("fig", "fig-center")):
            if target not in ("both", layer) and not (target == "fig"
                                                      and layer == "fig"):
                if target == "bg" and layer == "fig":
                    continue
                if target == "fig" and layer == "bg":
                    continue
            cmd = LE.to_transform_cmd(p["name"], target=tgt)
            if not cmd:
                continue
            effs.append({"t": round(t, 3), "cmd": cmd})
            n += 1
        if not n:
            return
        with contextlib.suppress(Exception): effs.sort(key=lambda x: float(x.get("t") or 0.0))
        self.push_history()
        self._after_edit()
        TOAST.toast_ok(self, "光效已应用", "%s（%.2fs，%d 条指令）" % (p["name"], t, n), duration=2400)

    def _add_shadow(self, who=None):
        """给指定角色加地面投影（用贴图 clip 实现，官方引擎真能渲染）。

        为什么用贴图：官方 WebGAL 4.6.6 的滤镜注册表里没有投影能力
        （见 light_effects.LIMITS）。但投影本质是「一张铺在地面的图」，
        用普通 image clip 就能做到，导出后真能看到。
        """
        import character_shadow as CS
        aid = getattr(self, "_cur_aid", None)
        # 找该角色的立绘 clip（按 FIG-<角色> 轨名）
        who = str(who or "").strip()
        figs = []
        for tr in V._tracks_of(self.proj, aid):
            if not isinstance(tr, dict) or tr.get("kind") != "video":
                continue
            nm = str(tr.get("name") or "")
            if not nm.upper().startswith("FIG"):
                continue
            ch = nm[4:].lstrip("-_")
            if who and ch != who:
                continue
            for c in (tr.get("clips") or []):
                figs.append((ch, tr, c))
        if not figs:
            TOAST.toast_warn(self, "没有立绘", "先给角色上一条立绘 clip，再加投影")
            return
        ch, tr, c = figs[0]
        lay = V.layout_at(c)
        # 按当前环境光决定影子浓度（暗场景影子淡）
        cur_light = getattr(self, "_cur_light_name", None)
        conc = CS.preset_concentration(cur_light) if cur_light else 0.38
        s_layout = CS.place_shadow(lay, self.proj.get("width", 1920),
                                   self.proj.get("height", 1080),
                                   concentration=conc)
        # 生成贴图
        png = CS.make_shadow_png(
            os.path.join(getattr(self, "proj_dir", "") or
                         os.path.expanduser("~"),
                         "_shadow_%s.png" % (ch or "default")),
            concentration=conc, softness=0.55)
        # 建一条 SHADOW-<角色> 轨，clip 覆盖人物 clip 的时间范围
        sname = CS.shadow_track_name(ch)
        strack = None
        for t2 in V._tracks_of(self.proj, aid):
            if isinstance(t2, dict) and str(t2.get("name")) == sname:
                strack = t2
                break
        if strack is None:
            # add_track 返回 (ok, msg, track_id) —— 是 id 字符串不是轨道字典，
            # 要按 id 回查真实轨道对象（否则拿到 str 后续全崩）
            try:
                _ok, _msg, _tid = V.add_track(self.proj, "video", sname, aid=aid)
                if _ok:
                    for t2 in V._tracks_of(self.proj, aid):
                        if isinstance(t2, dict) and str(t2.get("id")) == str(_tid):
                            strack = t2
                            break
            except Exception:
                strack = None
        if strack is None:
            TOAST.toast_warn(self, "加投影失败", "无法创建投影轨")
            return
        try:
            sc = V.new_clip(V.new_source("image", png),
                            float(c.get("start") or 0.0),
                            float(c.get("dur") or 5.0))
            sc["layout"] = dict(s_layout)
            strack.setdefault("clips", []).append(sc)
            # 投影必须在人物**下面**：把它移到人物轨之前
            with contextlib.suppress(Exception):
                trucks = self.proj.get("tracks") or []
                if strack in trucks and tr in trucks:
                    trucks.remove(strack)
                    trucks.insert(trucks.index(tr), strack)
        except Exception as e:
            TOAST.toast_warn(self, "加投影失败", str(e))
            return
        self.push_history()
        self.timeline.refresh()
        self._after_edit()
        TOAST.toast_ok(self, "投影已添加", "%s（浓度 %.0f%%）" % (sname, conc * 100), duration=2600)

    def _clear_shadows(self):
        """移除所有投影轨（SHADOW-*）。"""
        tracks = self.proj.get("tracks") or []
        keep = [t for t in tracks if not str(t.get("name") or "")
                .upper().startswith("SHADOW")]
        n = len(tracks) - len(keep)
        if n <= 0:
            TOAST.toast_info(self, "没有投影", "当前工程里没有投影轨", duration=2000)
            return
        self.proj["tracks"] = keep
        self.push_history()
        self.timeline.refresh()
        self._after_edit()
        TOAST.toast_ok(self, "已移除投影", "清理 %d 条投影轨" % n, duration=2200)

    def _run_batch(self, name, extra=None):
        """执行一个批处理动作（由 batch_bar 触发）。

        统一流程：算新工程 -> 有改动才替换 + 存档 + 刷新；无改动如实告知。
        **一定走 push_history()**：批量改一批东西，用户很可能要撤销。
        """
        import batch_ops as BO
        extra = extra or {}
        aid = getattr(self, "_cur_aid", None)
        try:
            if name == "apply_filter":
                filt = getattr(self, "_cur_filter_preset", None)
                if not filt:
                    self._batch_report("先去「滤镜」页选一个滤镜再批量套用", False)
                    return
                scope = int(extra.get("scope") or 0)
                which = {0: "all", 1: "fig", 2: "bg"}.get(scope, "all")
                newp, st = BO.apply_filter_batch(self.proj, filt, which=which,
                                                 aid=aid)
                msg = "套用滤镜到 %d 个片段（%s）" % (
                    st["updated"], {0: "全部", 1: "只立绘",
                                    2: "只背景"}.get(scope, "全部"))
            elif name == "clear_filter":
                which = {0: "all", 1: "fig",
                         2: "bg"}.get(int(extra.get("scope") or 0), "all")
                newp, st = BO.apply_filter_batch(self.proj, None, which=which,
                                                 aid=aid)
                msg = "清除 %d 个片段的滤镜" % st["updated"]
            else:
                newp, st, msg = BO.run_preset(self.proj, name, aid=aid)
        except Exception as e:
            self._batch_report("批处理失败：%s" % e, False)
            return
        if not any((st or {}).values()):
            self._batch_report("%s —— 没有需要改动的地方" % msg, True)
            return
        self.proj = newp
        self.push_history()
        with contextlib.suppress(Exception): self.timeline.refresh()
        self._after_edit()
        self._batch_report(msg, True)
        TOAST.toast_ok(self, "批量操作完成", msg, duration=2600)

    def _batch_report(self, text, ok=True):
        bar = getattr(self, "_batch_bar", None)
        if bar is not None:
            with contextlib.suppress(Exception):
                bar.report(text, ok)
                return

    def _apply_camera_path(self, payload):
        """把运镜编辑器生成的多段路径落到当前时刻。

        payload = {"paths": [{"level": "stage"/"fig", "cmd": "setTempAnimation:..."}]}
        背景与立绘各一条 → 这就是对方的「独立构图」：两层用各自的 keyframes，
        引擎按 -target 分别作用，互不干扰。
        """
        paths = (payload or {}).get("paths") or []
        if not paths:
            return
        t = float(getattr(self, "_t", 0.0) or 0.0)
        effs = self.proj.get("effects")
        if not isinstance(effs, list):
            effs = self.proj["effects"] = []
        n = 0
        for p in paths:
            cmd = str(p.get("cmd") or "").strip()
            if not cmd.startswith("setTempAnimation"):
                continue
            effs.append({"t": round(t, 3),
                         "cmd": cmd if cmd.endswith(";") else cmd + ";"})
            n += 1
        if not n:
            return
        with contextlib.suppress(Exception): effs.sort(key=lambda x: float(x.get("t") or 0.0))
        self.push_history()
        self._after_edit()
        TOAST.toast_ok(self, "运镜已应用", "%.2fs · %d 条路径（%s）" % (
                t, n, "背景+立绘独立构图" if n > 1 else "单层"),
                 duration=2600)

    def _on_camera_preview(self, data):
        """运镜编辑器预览：把采样结果作用到实时预览的舞台上。

        走同一条 setCamera 通道（页面里用 CSS transform 作用在 #stagebox），
        所以编辑器看到的 = 预览页看到的 = 导出后引擎跑的（口径同源）。
        """
        if not getattr(self, "_preview_real", False):
            return
        wv = getattr(self, "_webview", None)
        if wv is None:
            return
        st = (data or {}).get("stage") or {}
        sc = st.get("scale") or {}
        po = st.get("position") or {}
        cam = {"active": True, "p": float(st.get("_p") or 0),
               "scaleX": float(sc.get("x", 1.0) or 1.0),
               "scaleY": float(sc.get("y", 1.0) or 1.0),
               "x": float(po.get("x", 0.0) or 0.0) / 1280.0 * 100.0,
               "y": float(po.get("y", 0.0) or 0.0) / 720.0 * 100.0}
        with contextlib.suppress(Exception): wv._send("setCamera", cam)

    def _on_camera_reset(self):
        wv = getattr(self, "_webview", None)
        if wv is not None:
            with contextlib.suppress(Exception): wv._send("setCamera", {"active": False})
    def _rebuild_transition_cards(self):
        """转场页：列出引擎真实支持的转场预设（点卡片=设为全片转场）。

        转场是**工程级**选择（WebGAL 的 setTransition 一次设置全程生效），
        所以这里不是逐 clip 应用，而是写 proj["transition"]；导出侧
        （WebGAL + 视频）据此还原。当前已选中的预设卡片高亮。
        """
        import transitions as TR
        cur = (self.proj or {}).get("transition") or {}
        cur_name = str(cur.get("name") or "")
        for p in TR.catalog():
            # 已选中：标题加 ✓ 前缀（_TemplateCard 无选中态，文字前缀最稳）
            title = p["name"]
            if cur_name and cur_name == p["name"]:
                title = "✓ " + p["name"]
            card = _TemplateCard(
                title, p["desc"],
                (lambda pn=p["name"]: self._apply_transition(pn)),
                self.asset_host)
            self.asset_flow.addWidget(card)
        self.asset_empty.setVisible(False)

    def _apply_transition(self, name):
        """把某预设写入工程转场（None/无效名 = 恢复默认淡入淡出）。"""
        import transitions as TR
        t = TR.to_proj_transition(name)
        if t is None:
            TOAST.toast_warn(self, "转场不可用", "引擎不支持该转场动画。", duration=2500)
            return
        self.proj["transition"] = t
        self.push_history()
        self._after_edit()
        # 转场页需要重刷高亮
        if self._asset_tab == "transition":
            self._rebuild_asset_cards()
        TOAST.toast_ok(self, "转场已设置", "已恢复默认淡入淡出" if not t["enter"] and not t["exit"]
             else "%s（%dms，WebGAL 导出与视频导出都会带上）"
             % (name, t["duration_ms"]),
                 duration=2500)

    # ---------- A4 模板页（三张卡片背后的动作） ----------
    @staticmethod
    def _templates_dir():
        _base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(_base, "MuppetStudio", "templates")

    def _ensure_demo_assets(self):
        """生成/复用内置演示素材（渐变背景 + 两个半透明人形剪影），零外部版权素材。"""
        d = self._templates_dir()
        os.makedirs(d, exist_ok=True)
        bg = os.path.join(d, "demo_bg.png")
        fig_l = os.path.join(d, "demo_fig_left.png")
        fig_r = os.path.join(d, "demo_fig_right.png")
        if all(os.path.isfile(p) for p in (bg, fig_l, fig_r)):
            return bg, fig_l, fig_r
        from PIL import Image, ImageDraw
        W, H = 1920, 1080
        bg_img = Image.new("RGB", (W, H))
        px = bg_img.load()
        top = (38, 46, 78)
        bot = (120, 86, 96)
        for y in range(H):
            r = y / max(1, H - 1)
            row = tuple(int(top[i] + (bot[i] - top[i]) * r) for i in range(3))
            for x in range(W):
                px[x, y] = row
        bg_img.save(bg)

        def _silhouette(path, cx):
            canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            dr = ImageDraw.Draw(canvas)
            col = (24, 28, 40, 170)
            head_r = 74
            head_y = 300
            body_top = head_y + head_r - 6
            body_bot = 820
            half_top, half_bot = 96, 168
            dr.ellipse([cx - head_r, head_y - head_r, cx + head_r, head_y + head_r],
                       fill=col)
            dr.polygon([(cx - half_top, body_top), (cx + half_top, body_top),
                        (cx + half_bot, body_bot), (cx - half_bot, body_bot)],
                       fill=col)
            canvas.save(path)

        _silhouette(fig_l, int(W * 0.30))
        _silhouette(fig_r, int(W * 0.70))
        return bg, fig_l, fig_r

    def _src_for(self, path, kind="image"):
        """构造 source dict（尽量探测真实尺寸，探测不到用 0 兜底）。"""
        with contextlib.suppress(Exception):
            import video_source as VS
            src = VS.probe_source(path)
            if isinstance(src, dict) and src.get("path"):
                src["kind"] = kind
                return src
        return V.new_source(kind, path)

    def _load_template_blank(self):
        """空白 WebGAL 工程：project.new 语义 + 清空台词。"""
        self._sources.close_all()
        self.proj = V.new_project()
        self.proj["dialogue"] = []
        self._loading = True
        self._apply_project(None)
        self._loading = False
        self._seed_history()
        self.seek(0.0)
        self.push_history()

    def _load_template_demo(self):
        """单背景+双立绘演示：BG 轨 + 两 FIG 轨 + 两条示例台词。"""
        bg, fig_l, fig_r = self._ensure_demo_assets()
        self._sources.close_all()
        proj = V.new_project()
        proj["tracks"] = []
        proj["dialogue"] = []
        ok, _m, tid = V.add_track(proj, "video", "BG")
        if ok:
            V.add_clip(proj, tid, V.new_clip(self._src_for(bg), 0.0, 10.0))
        for path, name, x in ((fig_l, "FIG-1", 0.30), (fig_r, "FIG-2", 0.70)):
            ok, _m, tid = V.add_track(proj, "video", name)
            if not ok:
                continue
            clip = V.new_clip(self._src_for(path), 0.0, 10.0)
            lay = clip["layout"]
            lay["x"], lay["y"], lay["scale"] = x, 0.70, 0.6
            V.add_clip(proj, tid, clip)
        proj["dialogue"] = [
            {"t": 0.5, "dur": 3.0, "who": "旁白",
             "text": "这是一个内置的演示模板。"},
            {"t": 4.0, "dur": 3.0, "who": "角色",
             "text": "背景与立绘均为程序生成，可自由编辑。"},
        ]
        self.proj = proj
        self.proj_dir = None
        with contextlib.suppress(Exception):
            self.timeline.set_proj_dir(self.proj_dir or "")
        self._loading = True
        self._apply_project(None)
        self._loading = False
        self._seed_history()
        self.seek(0.0)
        self.push_history()
        TOAST.toast_ok(self, "模板", "已载入「单背景+双立绘演示」", duration=2500)

    def _import_webgal_dir(self):
        """导入 WebGAL game 目录：background→底轨，figure→上层轨（尽力而为）。"""
        d = QFileDialog.getExistingDirectory(self, "选择 WebGAL 游戏目录（含 game\\）")
        if not d:
            return
        game = os.path.join(d, "game")
        if not os.path.isdir(game):
            game = d
        bg_dir = os.path.join(game, "background")
        fig_dir = os.path.join(game, "figure")
        img_ext = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
        bg_files = sorted(f for f in os.listdir(bg_dir)
                          if os.path.isdir(bg_dir) and f.lower().endswith(img_ext)) \
            if os.path.isdir(bg_dir) else []
        fig_files = sorted(f for f in os.listdir(fig_dir)
                           if os.path.isdir(fig_dir) and f.lower().endswith(img_ext)) \
            if os.path.isdir(fig_dir) else []
        self._sources.close_all()
        proj = V.new_project()
        proj["tracks"] = []
        proj["dialogue"] = []
        n = 0
        if bg_files:
            ok, _m, tid = V.add_track(proj, "video", "BG")
            if ok:
                t0 = 0.0
                for fn in bg_files[:8]:
                    # 同轨不允许重叠：背景图依次首尾相接
                    V.add_clip(proj, tid,
                               V.new_clip(self._src_for(os.path.join(bg_dir, fn)),
                                          t0, 10.0))
                    n += 1
                    t0 += 10.0
        fig_idx = 1
        for fn in fig_files[:12]:
            ok, _m, tid = V.add_track(proj, "video", "FIG-%d" % fig_idx)
            if not ok:
                continue
            clip = V.new_clip(self._src_for(os.path.join(fig_dir, fn)), 0.0, 10.0)
            lay = clip["layout"]
            lay["x"], lay["y"], lay["scale"] = 0.5, 0.70, 0.6
            V.add_clip(proj, tid, clip)
            n += 1
            fig_idx += 1
        gname = ""
        cfg = os.path.join(game, "config.txt")
        if os.path.isfile(cfg):
            with contextlib.suppress(Exception):
                for line in open(cfg, encoding="utf8", errors="ignore"):
                    if line.startswith("Game_name:"):
                        gname = line.split(":", 1)[1].strip().rstrip(";")
                        break
        self.proj = proj
        self.proj_dir = d
        with contextlib.suppress(Exception):
            self.timeline.set_proj_dir(self.proj_dir or "")
        self._loading = True
        self._apply_project(None)
        self._loading = False
        self._seed_history()
        self.seek(0.0)
        self.push_history()
        TOAST.toast_ok(self, "导入 WebGAL",
                 "已导入 %d 个素材%s" % (
            n, ("（%s）" % gname) if gname else ""))

    # ---------- E2：WebGAL 运行时状态卡动作 ----------
    def _runtime_online(self):
        """在线获取 -> 指引到资产管理「WebGAL 引擎运行时」条目（E1 已注册）。"""
        TOAST.toast_info(self, "WebGAL 运行时", "请到「资产管理」菜单的「WebGAL 引擎运行时」条目下载", duration=4000)

    def _runtime_import_confirm(self, zip_path, msg):
        """覆盖确认通过后，带 confirm=True 重跑 import_zip（后台线程）。"""
        self._runtime_busy = True

        def work():
            try:
                import runtime_pack as RP
                ok, msg2, _data = RP.import_zip(zip_path, confirm_overwrite=True)
            except Exception as e:
                ok, msg2 = False, "导入异常 %s: %s" % (type(e).__name__, e)

            def done():
                self._runtime_busy = False
                if ok:
                    self._refresh_runtime_cards()
                    TOAST.toast_ok(self, "运行时导入", msg2, duration=4000)
                else:
                    TOAST.toast_warn(self, "运行时导入", msg2, duration=5000)
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self, done)

        threading.Thread(target=work, daemon=True).start()

    def _runtime_import_zip(self):
        """选 zip -> 后台导入；已装需确认覆盖 -> 主线程确认框后 confirm=True。"""
        if getattr(self, "_runtime_busy", False):
            TOAST.toast_info(self, "导入中", "请等待当前导入完成", duration=1800)
            return
        zip_path, _ = QFileDialog.getOpenFileName(
            self, "选择 WebGAL 运行时压缩包", "", "WebGAL 运行时压缩包 (*.zip)")
        if not zip_path:
            return
        self._runtime_busy = True

        def work():
            try:
                import runtime_pack as RP
                ok, msg, data = RP.import_zip(zip_path, confirm_overwrite=False)
            except Exception as e:
                ok, msg, data = False, "导入异常 %s: %s" % (type(e).__name__, e), None
            need_confirm = bool(data and data.get("needs_confirm"))

            def done():
                self._runtime_busy = False
                if need_confirm:
                    ret = QMessageBox.question(
                        self, "覆盖确认", "WebGAL 运行时已安装，是否覆盖？",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if ret == QMessageBox.Yes:
                        self._runtime_import_confirm(zip_path, msg)
                    else:
                        TOAST.toast_info(self, "覆盖确认", "已取消覆盖", duration=2000)
                    return
                if ok:
                    self._refresh_runtime_cards()
                    TOAST.toast_ok(self, "运行时导入", msg, duration=4000)
                else:
                    TOAST.toast_warn(self, "运行时导入", msg, duration=5000)
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self, done)

        threading.Thread(target=work, daemon=True).start()

    def _refresh_runtime_cards(self):
        """刷新运行时状态卡（未在模板页则忽略）。"""
        card = getattr(self, "_runtime_card", None)
        if card is not None:
            with contextlib.suppress(Exception): card.refresh()

    def _show_script_editor(self, show):
        """文本 tab：显示剧情编辑器并隐藏素材库；其它 tab 反之。"""
        se = getattr(self, "script_editor", None)
        if se is None:
            return
        se.setVisible(bool(show))
        # v3.6.2：批量操作条只在文本页出现（它作用于台词/片段，别的页没意义）
        with contextlib.suppress(Exception):
            bb = getattr(self, "_batch_bar", None)
            if bb is not None:
                bb.setVisible(bool(show))
        try:
            self.asset_scroll.setVisible(not show)
            self.asset_empty.setVisible(not show)
            # v3.6.2：文本页也要把背景面板收起来（否则文本页会叠在上面）
            bp = getattr(self, "bg_panel", None)
            if bp is not None and show:
                bp.setVisible(False)
            if self.btn_add_file is not None:
                self.btn_add_file.setVisible(not show)
            if self.btn_add_puppet is not None:
                self.btn_add_puppet.setVisible(not show)
            self.asset_header.setText("剧情" if show else "素材库")
        except Exception:
            pass
        if show:
            se.refresh()

    def _show_bg_panel(self, show):
        """背景 tab：只显示背景面板，隐藏素材卡片区（与文本页互斥）。"""
        bp = getattr(self, "bg_panel", None)
        if bp is None:
            return
        try:
            bp.setVisible(bool(show))
            self.asset_scroll.setVisible(not show)
            self.asset_empty.setVisible(False if show else
                                        self.asset_empty.isVisible())
            self.asset_header.setText("背景" if show else "素材库")
            if show:
                # 头行交给面板自己的计数标签；「素材库」标题保留也没关系
                pass
        except Exception:
            pass

    def _filter_assets(self, key):
        self._asset_tab = str(key) if key else "media"
        self._rebuild_asset_cards()

    def _add_asset_to_timeline(self, src, track_id=None):
        """双击卡片：追加到对应轨末尾。track_id 指定时直接用该轨。"""
        kind = src["kind"]
        if track_id is None:
            tid = "a1" if kind == "audio" else "v1"
        else:
            tid = track_id
        tr = V.find_track(self.proj, tid,
                          aid=getattr(self, "_cur_aid", None))
        if tr is None:
            ok, _m, tid = V.add_track(self.proj,
                                      "audio" if kind == "audio" else "video")
            if not ok:
                return
            tr = V.find_track(self.proj, tid,
                              aid=getattr(self, "_cur_aid", None))
        end = max([V.clip_end(c) for c in tr["clips"]] or [0.0])
        dur = DEFAULT_DUR.get(kind) or src.get("src_dur") or 3.0
        dur = max(V.MIN_DUR, min(dur, src["src_dur"]) if src.get("src_dur")
                  and kind != "image" else dur)
        clip = V.new_clip(src, end, dur)
        # K1（task-69）：同屏人数上限撤销——立绘上轨不再事前拦截。
        ok, cid = V.add_clip(self.proj, tid, clip,
                             aid=getattr(self, "_cur_aid", None))
        if not ok:
            TOAST.toast_warn(self, "上轨失败", cid, duration=2500)
            return
        self.push_history()
        self.timeline.refresh()
        self._after_edit()

    # ================= 预览播放 =================
    def _preview_scale(self):
        return min(1.0, 720.0 / max(1, self.proj["height"]))

    def _sync_preset_sources(self, t):
        """把当前 t 时刻生效的 puppet clip 的 motion/expression 灌给其源。

        preset 是 clip 级状态，SourceManager 按素材缓存；每次 render 前按
        当前 clip 字段重算 -> seek/撤销/重做后不残留错值。
        """
        try:
            for tr in self.proj.get("tracks", []):
                if tr.get("kind") != "video" or tr.get("hidden"):
                    continue
                for c in (tr.get("clips") or []):
                    if not isinstance(c, dict):
                        continue
                    s0 = float(c.get("start", 0.0))
                    d = float(c.get("dur", 0.0))
                    if t < s0 - 1e-6 or t >= s0 + d - 1e-6:
                        continue
                    if (c.get("source") or {}).get("kind") != "puppet":
                        continue
                    with contextlib.suppress(Exception):
                        src = self._sources.get(c["source"])
                        src.set_preset(c.get("motion", ""),
                                       c.get("expression", ""))
        except Exception:
            pass

    def render_frame(self, t):
        self._sync_preset_sources(t)
        # v3.5.7：实时态引擎自绘，跳过重型合成（numpy 帧纯浪费）
        if getattr(self, "_preview_real", False):
            self.stage.playing = bool(self._playing)
            self.stage.tick_busy = bool(self._tick_busy)
            _se = getattr(self, "script_editor", None)
            if _se is not None:
                _se.refresh_highlight()
            self._sync_real_preview(t)
            return
        # F2-L5（v3.5）：多幕工程只合成**当前幕**（一幕一条独立时间轴）
        # v3.5.4：subtitle=False —— 工作台预览的台词由 StageView.paintEvent
        # 画（widget 自适应字号）；视频导出走 compose_frame 的烧录版。
        # 两条路同规格（MyGO 风），但各画各的，避免预览双重字幕。
        # v3.5.7：fx=False —— 预览不渲染滤镜链（逐帧 numpy ~330ms/帧卡爆预览；
        # 滤镜效果看实时预览或导出成片）。
        arr = VE.compose_frame(self.proj, t, self._sources.get,
                               aid=getattr(self, "_cur_aid", None),
                               subtitle=False, fx=False)
        # 交互门控：播放/逐帧合成中 StageView 跳过鼠标，但照常刷新画面与框
        self.stage.playing = bool(self._playing)
        self.stage.tick_busy = bool(self._tick_busy)
        self.stage.set_frame(arr, self.proj, t,
                              getattr(self, "_cur_aid", None))
        # F1：台词表当前时刻生效行高亮
        _se = getattr(self, "script_editor", None)
        if _se is not None:
            _se.refresh_highlight()
        # H3：实时预览 diff 同步（非实时态内部零开销直接 return）
        self._sync_real_preview(t)

    # ================= F2-L5（v3.5）：多幕 UI =================
    # 一幕一条独立时间轴（设计 §2.2）。单幕工程（v1）**整个幕条不出现**
    # -> 既有 UI 零 diff（564+ 既有测试不受影响）。
    def _act_ids(self):
        """当前工程的幕 id 列表（v1 单幕 -> ['a1'] 虚拟幕）。"""
        try:
            return V.act_ids(self.proj)
        except Exception:
            return ["a1"]

    def _is_multi(self):
        try:
            return V.is_multi_act(self.proj)
        except Exception:
            return False

    def _act_name(self, aid):
        with contextlib.suppress(Exception):
            a = V.find_act(self.proj, aid)
            if a:
                return str(a.get("name") or aid)
        return str(aid)

    def _refresh_act_bar(self):
        """按工程当前幕数重建幕条（单幕 -> 隐藏整行）。

        幕 id 变了才重建（增删/改名/载入工程时调用）。"""
        ids = self._act_ids()
        if not self._is_multi() or len(ids) <= 1:
            # 单幕：整行隐藏，不建任何按钮（零 diff）
            self._act_bar_host.setVisible(False)
            for b in list(self._act_buttons.values()):
                self._act_bar_layout.removeWidget(b)
                b.deleteLater()
            self._act_buttons = {}
            self._cur_aid = ids[0] if ids else "a1"
            self._apply_cur_act()
            return
        # **游标归一必须先做**：载入/新建工程会把 _cur_aid 清成 None，
        # 而下面「幕集合没变」的早退分支不碰游标 -> 游标一直 None，
        # 分支面板/时间轴拿不到当前幕（实测 bug）。
        if self._cur_aid not in ids:
            self._cur_aid = ids[0]
        # 幕 id 集合没变 -> 只刷新名字（改名场景）
        if set(self._act_buttons.keys()) == set(ids):
            for aid, b in self._act_buttons.items():
                b.setText(self._act_name(aid))
            self._sync_act_buttons()
            self._apply_cur_act()
            return
        for b in list(self._act_buttons.values()):
            self._act_bar_layout.removeWidget(b)
            b.deleteLater()
        self._act_buttons = {}
        for aid in ids:
            b = PillPushButton(self._act_name(aid), self._act_bar_host)
            b.setCheckable(True)
            b.setFixedHeight(26)
            b.setToolTip("切到这一幕（%s）" % aid)
            b.clicked.connect(lambda _c=False, a=aid: self._switch_act(a))
            self._act_bar_layout.addWidget(b)
            self._act_buttons[aid] = b
        self._act_bar_layout.addStretch(1)
        self._act_add_btn = PushButton("+ 新增幕", self._act_bar_host)
        self._act_add_btn.setFixedHeight(26)
        self._act_add_btn.clicked.connect(self._act_add)
        self._act_bar_layout.addWidget(self._act_add_btn)
        # 操作按钮：改名 / 复制 / 删除
        for text, fn in (("改名", self._act_rename),
                         ("复制", self._act_duplicate),
                         ("删除", self._act_remove)):
            btn = PushButton(text, self._act_bar_host)
            btn.setFixedHeight(26)
            btn.clicked.connect(lambda _c=False, f=fn: f())
            self._act_bar_layout.addWidget(btn)
        self._act_bar_host.setVisible(True)
        self._sync_act_buttons()
        # 重建后同样要同步一次（构造期 / 载入工程时组件内容是旧的）
        self._apply_cur_act()

    def _sync_act_buttons(self):
        """让当前幕的按钮处于选中态（其余取消）。"""
        for aid, b in (self._act_buttons or {}).items():
            with contextlib.suppress(Exception):
                b.blockSignals(True)
                b.setChecked(aid == self._cur_aid)
                b.blockSignals(False)

    def _apply_cur_act(self):
        """把当前幕游标同步给所有幕感知组件（时间轴/剧情编辑器/舞台）。"""
        aid = self._cur_aid
        with contextlib.suppress(Exception): self.timeline.set_act(aid)
        # 剧情编辑器必须整表重刷：台词表与分支区都是按幕取的数据，
        # 只 render_frame 不够（那只刷高亮）——切幕后表格会停在旧幕内容。
        with contextlib.suppress(Exception): self.script_editor.refresh()
        with contextlib.suppress(Exception): self.render_frame(self._t)
        with contextlib.suppress(Exception): self._fill_clip_props()

    def _switch_act(self, aid):
        """切当前幕（时间轴/剧情编辑器/舞台都跟着切）。"""
        if aid == self._cur_aid:
            return
        self._cur_aid = aid
        self._sync_act_buttons()
        self._apply_cur_act()

    def _act_add(self):
        """新增一幕（复用 video_model.add_act，不自己造数据逻辑）。"""
        try:
            ok, msg, aid = V.add_act(self.proj, None, self._cur_aid)
        except Exception as e:
            TOAST.toast_warn(self, "新增幕", "失败：%s" % e, duration=4000)
            return
        if not ok:
            TOAST.toast_warn(self, "新增幕", msg or "失败", duration=4000)
            return
        self._cur_aid = aid
        self.push_history()
        self._refresh_act_bar()
        self._apply_cur_act()
        TOAST.toast_ok(self, "新增幕", "已创建 %s" % aid)

    def _act_rename(self, aid=None):
        """重命名当前幕（弹简单输入框）。"""
        aid = aid or self._cur_aid
        old = self._act_name(aid)
        dlg = _ActNameDialog(self, old)
        if not dlg.exec():
            return
        name = dlg.value().strip()
        if not name:
            TOAST.toast_warn(self, "重命名幕", "幕名不能为空")
            return
        try:
            ok, msg = V.rename_act(self.proj, aid, name)
        except Exception as e:
            ok, msg = False, str(e)
        if not ok:
            TOAST.toast_warn(self, "重命名幕", msg or "失败", duration=4000)
            return
        self.push_history()
        self._refresh_act_bar()

    def _act_duplicate(self, aid=None):
        """复制当前幕。"""
        aid = aid or self._cur_aid
        try:
            ok, msg, new_aid = V.duplicate_act(self.proj, aid)
        except Exception as e:
            ok, msg, new_aid = False, str(e), None
        if not ok:
            TOAST.toast_warn(self, "复制幕", msg or "失败", duration=4000)
            return
        self._cur_aid = new_aid
        self.push_history()
        self._refresh_act_bar()
        self._apply_cur_act()

    def _act_remove(self, aid=None):
        """删除当前幕（级联清引用，提示清理了几处）。"""
        aid = aid or self._cur_aid
        try:
            ok, msg, cleared = V.remove_act(self.proj, aid)
        except Exception as e:
            ok, msg, cleared = False, str(e), None
        if not ok:
            TOAST.toast_warn(self, "删除幕", msg or "失败", duration=4000)
            return
        self.push_history()
        self._refresh_act_bar()
        self._apply_cur_act()
        # remove_act 返回的是**清理了几处引用**（int），不是列表
        n = int(cleared or 0)
        TOAST.toast_ok(self, "删除幕", ("已删除 %s" % aid) +
                        ("，并清理 %d 处引用" % n if n else ""),
                 duration=4000)

    # ================= H3 实时预览（v3.5，设计 §5） =================
    def _preview_runtime_ready(self):
        """WebGAL 运行时是否已就位（决定要不要给「实时预览」角标）。

        v3.5：只查 WebEngine（available()）会出现「开关常亮但页面空白」——
        用户装了 WebEngine 可选件但**运行时素材没下载**（当前发行形态
        运行时与安装包分离），点开只能得到 no_runtime 错误页。
        所以这里额外查运行时是否存在，查不到就**连开关都不出现**
        （与 available()==False 同等对待：宁可不出现，也不要一个点不动的开关）。"""
        WP = getattr(self, "_wp", None)
        if WP is None or not WP.available():
            return False
        fn = getattr(WP, "runtime_ready", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return True
        # 没有 runtime_ready()（旧版预览模块）-> 不阻断（保持兼容）
        return True

    def _make_preview_badge(self, host):
        """预览区右上角「实时预览」角标开关。
        仅在「WebEngine 可用 + WebGAL 运行时已就位」时创建。"""
        btn = PushButton("实时预览", host)
        btn.setCheckable(True)
        btn.setFixedHeight(26)
        btn.setToolTip("用 WebGAL 引擎实时渲染立绘（布置辅助，不影响导出画质）")
        btn.toggled.connect(self._toggle_real_preview)
        self._preview_badge = btn
        self._badge_host = host
        host.installEventFilter(self)
        # v3.5.7：创建时容器还没布局（宽 0），且实时页会盖住先创建的角标；
        # 用 _place_preview_badge（含 raise_）统一摆放。
        self._place_preview_badge()
        btn.show()

    def _place_preview_badge(self):
        """角标随容器 resize 重摆 + 常驻顶层（v3.5.7：实时态页面会盖住
        先创建的角标，必须 raise_；创建时容器还没布局，宽度 0 定位错）。"""
        b = getattr(self, "_preview_badge", None)
        h = getattr(self, "_badge_host", None)
        if b is None or h is None:
            return
        b.adjustSize()
        b.move(max(6, h.width() - b.width() - 12), 12)
        b.raise_()

    def eventFilter(self, obj, ev):
        if obj is getattr(self, "_badge_host", None) \
                and ev.type() == QEvent.Resize:
            self._place_preview_badge()
        return super().eventFilter(obj, ev)

    def _ensure_webview(self):
        """懒创建 WebPreviewView（首次开实时预览时才建，避免启动开销）。"""
        if self._webview is not None:
            return self._webview
        if not getattr(self, "_wp", None) or not self._wp.available():
            return None
        try:
            v = self._wp.WebPreviewView(parent=self)
            # 交互恒归 StageView（设计 §5）：实时页纯显示
            v.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            # 页面 module 就绪前下发命令会被 bootstrap 丢弃（其
            # `if (window.__muppetOnCmd)` 为假即丢）——而 boot() 只在
            # loadModel 里被调用，丢弃后引擎永不启动。故必须等页面
            # 就绪再推首帧状态（v3.5 实测根因）。
            self._preview_ready = False
            v.pageReady.connect(self._on_preview_page_ready)
            # J3 §8-7：连续机制级 error 达阈值 -> 引擎自动降级，
            # UI 侧取消勾选 + 置灰（会话内不再弹，置灰保持）。
            with contextlib.suppress(Exception):
                v.previewDisabled.connect(self._on_preview_disabled)
            v.start()
            self._webview = v
            self._stack.addWidget(v)
            # v3.5.7：实时页加入后会盖住角标，重新摆到顶层
            self._place_preview_badge()
            return v
        except Exception as e:
            TOAST.toast_warn(self, "实时预览", "初始化失败：%s" % e, duration=4000)
            return None

    def _on_preview_disabled(self):
        """J3 §8-7：预览引擎降级（连续机制级 error）-> 取消勾选 + 置灰。"""
        try:
            if getattr(self, "_preview_real", False):
                self._preview_real = False
                if self._stack is not None:
                    self._stack.setCurrentIndex(0)   # 回 StageView
                b = getattr(self, "_preview_badge", None)
                if b is not None:
                    b.blockSignals(True)
                    b.setChecked(False)
                    b.blockSignals(False)
        except Exception:
            pass
        b = getattr(self, "_preview_badge", None)
        if b is not None:
            b.setEnabled(False)
            b.setToolTip("实时预览已自动停用：引擎连续报错（多半是模型文件缺失）"
                         "，修好模型后可重新开启")
        TOAST.toast_warn(self, "实时预览", "引擎连续报错，已自动停用实时预览", duration=8000)

    def _on_preview_page_ready(self, ok):
        """宿主页 loadFinished：module script 还需一点时间求值，延迟推首帧。"""
        if not ok:
            return
        self._preview_ready = True
        QTimer.singleShot(400, self._push_preview_state)

    def _push_preview_state(self, force=True):
        """把当前时刻的立绘状态推给实时预览（内部做越界/异常兜底）。"""
        v = self._webview
        if v is None or not getattr(self, "_preview_ready", False):
            return
        # v3.5.2：同 _sync_real_preview —— 幕游标是 `_cur_aid` 不是 `_aid`
        aid = getattr(self, "_cur_aid", None)
        with contextlib.suppress(Exception): v._aid = aid
        try:
            view = V.act_view(self.proj, aid)
            v.apply_state(view, self._t, force=force)
        except TypeError:
            with contextlib.suppress(Exception): v.apply_state(self.proj, self._t, force=force)
        except Exception:
            pass

    def _toggle_real_preview(self, on):
        """切实时/布局态。实时态=WebEngine 画面为底；布局态=QPainter 帧。"""
        if not on:
            self._preview_real = False
            self._stack.setCurrentIndex(0)          # 回 StageView（不销毁）
            if self._webview is not None:
                self._webview.clear_all()           # 清掉页面侧模型
            self.render_frame(self._t)              # 立即回填当前帧，不闪白
            btn = getattr(self, "_preview_badge", None)
            if btn is not None:
                btn.setToolTip("用 WebGAL 引擎实时渲染立绘（布置辅助，不影响导出画质）")
            self._place_preview_badge()
            return
        v = self._ensure_webview()
        if v is None:
            btn = self._preview_badge
            if btn is not None:
                btn.blockSignals(True)
                btn.setChecked(False)
                btn.blockSignals(False)
            TOAST.toast_info(self, "实时预览", "实时预览不可用：需要 WebEngine 能力包与 WebGAL 运行时", duration=4000)
            return
        self._preview_real = True
        self._stack.setCurrentIndex(1)
        btn = getattr(self, "_preview_badge", None)
        if btn is not None:
            btn.setToolTip("正在实时预览：再点一次返回普通预览（更快，不渲染滤镜）")
        self._place_preview_badge()
        # 页面已就绪就立刻推；否则等 pageReady 回调（见 _on_preview_page_ready）
        if getattr(self, "_preview_ready", False):
            self._push_preview_state(force=True)

    def _sync_real_preview(self, t):
        """render_frame 尾部调用：实时态下按 diff 同步（不实时态零开销）。"""
        if not self._preview_real or self._webview is None:
            return
        if not getattr(self, "_preview_ready", False):
            return                       # 页面未就绪：等 pageReady 后统一推
        # v3.5.2：当前幕要传对。
        # 原来这里写 getattr(self, "_aid", None) —— workbench 上根本没有
        # `_aid` 属性（幕游标叫 `_cur_aid`），永远拿到 None = 恒按第一幕
        # 取数据。单幕无感，多幕必错（切到 a2 预览还在推 a1 的背景/台词）。
        aid = getattr(self, "_cur_aid", None)
        try:
            self._webview._aid = aid      # 背景/字幕/滤镜三层也按幕取
        except Exception:
            pass
        try:
            view = V.act_view(self.proj, aid)
            self._webview.apply_state(view, t)
        except TypeError:
            with contextlib.suppress(Exception): self._webview.apply_state(self.proj, t)
        except Exception:
            pass

    def seek(self, t):
        self._t = float(max(0.0, min(t, V.project_duration(self.proj))))
        self.timeline.set_time(self._t)
        self.render_frame(self._t)
        self._update_transport()
        # 画面跳转后音频跟上
        self._sync_mix()

    def _seek_relative(self, direction, seconds=False):
        """v3.5.2：←/→ 逐帧走，Shift+←/→ ±1 秒，Home/End 由 seek() 直达。

        direction ±1；seconds=True 时步长 1 秒，否则 1 帧（1/fps）。
        播放中同样生效（时间轴/预览跟着跳）。"""
        step = 1.0 if seconds else 1.0 / max(1.0, float(
            self.proj.get("fps") or 30.0))
        self.seek(self._t + direction * step)

    def _advance(self):
        if self._tick_busy:
            return
        self._tick_busy = True
        try:
            step = 1.0 / float(self.proj["fps"])
            nt = self._t + step
            if nt >= V.project_duration(self.proj):
                self.stop_play()
                self.seek(V.project_duration(self.proj))
                return
            self._t = nt
            self.timeline.set_time(nt)
            self.render_frame(nt)
            self._update_transport()
        finally:
            self._tick_busy = False

    def toggle_play(self):
        if self._playing:
            self.stop_play()
        else:
            if V.project_duration(self.proj) <= 0:
                return
            if self._t >= V.project_duration(self.proj) - 1e-6:
                self.seek(0.0)
            self._playing = True
            self.btn_play.setIcon(FluentIcon.PAUSE.icon())
            self.timer.setInterval(max(1, int(round(1000.0 / self.proj["fps"]))))
            self.timer.start()
            # 预览要有声音（用户要求）：准备混音并起播
            self._ensure_mix_async(then_play=True)

    def stop_play(self):
        self._playing = False
        self.timer.stop()
        self.btn_play.setIcon(FluentIcon.PLAY.icon())
        self._stop_mix()

    def _update_transport(self):
        dur = V.project_duration(self.proj)
        self.time_lbl.setText("%s / %s" % (fmt_time(self._t), fmt_time(dur)))
        self.sld_pos.setValue(int(round(self._t / dur * 1000)) if dur > 0 else 0)
        self.dur_lbl.setText("总时长：%s" % fmt_time(dur))

    # ================= 预览声音 =================
    def _mix_cache_dir(self):
        d = os.path.join(os.environ.get("TEMP") or "/tmp", "muppet_preview_mix")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            return (os.environ.get("TEMP") or "/tmp")
        return d

    def _mix_fingerprint(self):
        """工程音频指纹：内容不变就不重混（省的每次播放都等）。"""
        try:
            import hashlib
            import json as _json
            parts = []
            for tr in self.proj.get("tracks", []):
                for c in (tr.get("clips") or []):
                    k = (c.get("source") or {}).get("kind")
                    if k in ("puppet", "image"):
                        continue
                    parts.append((k, (c.get("source") or {}).get("path"),
                                  round(float(c.get("start") or 0), 3),
                                  round(float(c.get("src_in") or 0), 3),
                                  round(float(c.get("dur") or 0), 3)))
            blob = _json.dumps(parts, sort_keys=True, ensure_ascii=False)
            return hashlib.md5(blob.encode("utf-8")).hexdigest()
        except Exception:
            return None

    def _ensure_mix_async(self, then_play=False):
        """准备预览音轨（有缓存直接用，否则后台混音）。

        跨线程回调踩过两个坑（都不稳，别再回头）：
          ① work() 里 QTimer.singleShot(0, done) —— 非主线程创建的 timer
             不保证被主线程事件循环派发，_mix_busy 永远 True
          ② 局部 QObject + Signal —— 对象可能被 GC，且依赖事件循环
        改用**主线程轮询共享变量**，最简单最可靠。
        """
        from PySide6.QtCore import QTimer
        fp = self._mix_fingerprint()
        if fp is None:
            return
        if (self._mix_file and os.path.isfile(self._mix_file)
                and self._mix_key == fp):
            if then_play:
                self._start_mix_playback()
            return
        if self._mix_busy:
            self._mix_pending = self._mix_pending or then_play
            return
        self._mix_busy = True
        self._mix_pending = self._mix_pending or then_play
        self._mix_result = None            # 后台线程写这里：(path, fp)

        def work():
            with contextlib.suppress(Exception):
                import video_export as _VE
                buf = _VE.mix_audio(self.proj, 44100)
                if buf is not None and len(buf) > 0:
                    p = os.path.join(self._mix_cache_dir(), "mix_%s.wav" % fp)
                    _VE.write_wav(p, buf, 44100)
                    if os.path.isfile(p) and os.path.getsize(p) > 44:
                        self._mix_result = (p, fp)
                        return
            self._mix_result = ("", fp)

        import threading
        threading.Thread(target=work, daemon=True).start()

        # 主线程轮询：每 100ms 看后台是否写完（最多等 120 秒）
        ticks = {"n": 0}

        def check():
            ticks["n"] += 1
            res = getattr(self, "_mix_result", None)
            if res is None and ticks["n"] < 1200:
                QTimer.singleShot(100, check)
                return
            self._mix_busy = False
            path, fpr = res if res else ("", fp)
            if path:
                self._mix_file = path
                self._mix_key = fpr
            want = self._mix_pending
            self._mix_pending = False
            if want:
                self._start_mix_playback()

        QTimer.singleShot(100, check)

    def _ensure_mix_player(self):
        if self._mix_player is not None:
            return self._mix_player
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            p = QMediaPlayer(self)
            out = QAudioOutput(self)
            out.setVolume(0.9)
            p.setAudioOutput(out)
            self._mix_player = p
            self._mix_out = out
            return p
        except Exception:
            return None

    def _start_mix_playback(self):
        """从当前播放头开始放音轨。"""
        if not self._mix_file or not os.path.isfile(self._mix_file):
            return
        pl = self._ensure_mix_player()
        if pl is None:
            return
        with contextlib.suppress(Exception):
            from PySide6.QtCore import QUrl
            pl.setSource(QUrl.fromLocalFile(self._mix_file))
            pl.setPosition(int(round(self._t * 1000)))
            pl.play()

    def _sync_mix(self):
        """画面与音频对齐（seek 后调用）。"""
        if self._mix_player is None or not self._playing:
            return
        try:
            want = int(round(self._t * 1000))
            cur = self._mix_player.position()
            if abs(cur - want) > 180:      # 偏差超过 180ms 才纠偏，避免抖动
                self._mix_player.setPosition(want)
        except Exception:
            pass

    def _stop_mix(self):
        if self._mix_player is not None:
            with contextlib.suppress(Exception): self._mix_player.stop()

    # ================= 时间轴联动 =================
    def _on_selection(self, clip_id):
        self._sel_clip = clip_id
        self._fill_clip_props()

    def _fill_clip_props(self):
        tr, c = V.find_clip(self.proj, self._sel_clip) if self._sel_clip \
            else (None, None)
        on = c is not None
        for sp in (self.spin_start, self.spin_in, self.spin_dur):
            sp.setEnabled(on)
        self.btn_del_clip.setEnabled(on)
        # 布局组：有选中即显示
        self.lay_group.setVisible(on)
        # puppet 预置动作组：仅 puppet 片段显示
        is_puppet = bool(on and (c.get("source") or {}).get("kind") == "puppet")
        self.act_group.setVisible(is_puppet)
        self._loading = True
        if on:
            self.clip_info.setText("%s\n%s" % (
                os.path.basename(str(c["source"]["path"]).rstrip("\\/")),
                c["source"]["kind"]))
            self.spin_start.setValue(c["start"])
            self.spin_in.setValue(c["src_in"])
            self.spin_dur.setValue(c["dur"])
            lay = V.layout_at(c)          # 读布局只走 layout_at
            self.spin_lay_x.setValue(lay["x"])
            self.spin_lay_y.setValue(lay["y"])
            self.spin_lay_scale.setValue(lay["scale"])
            self.spin_lay_rot.setValue(lay["rot"])
            self._set_combo_by_data(self.cmb_motion, c.get("motion", ""),
                                   self._motion_ids)
            self._set_combo_by_data(self.cmb_expr, c.get("expression", ""),
                                   self._expression_ids)
        else:
            self.clip_info.setText("未选中")
            for sp in (self.spin_start, self.spin_in, self.spin_dur):
                sp.setValue(0.0)
            for sp in (self.spin_lay_x, self.spin_lay_y,
                       self.spin_lay_scale, self.spin_lay_rot):
                sp.setValue(0.0)
            self._set_combo_by_data(self.cmb_motion, "", self._motion_ids)
            self._set_combo_by_data(self.cmb_expr, "", self._expression_ids)
        self._loading = False

    def _set_combo_by_data(self, combo, val, ids):
        """按 id（并行列表）设置下拉当前项，不触发信号。"""
        with contextlib.suppress(Exception):
            combo.blockSignals(True)
            if val and val in ids:
                combo.setCurrentIndex(ids.index(val))
            combo.blockSignals(False)

    # ---------- A2 StageView 联动 ----------
    def _on_stage_select(self, clip_id):
        """StageView 点选 -> 联动时间轴 + 刷新属性面板。"""
        cid = clip_id or None
        self._sel_clip = cid
        setter = getattr(self.timeline, "_set_sel", None)
        if setter is not None:
            with contextlib.suppress(Exception): setter(cid)
        self._fill_clip_props()
        self.timeline.refresh()

    def _on_stage_layout_changed(self):
        """StageView 拖拽/手柄改布局后：压历史 + 重渲染 + 刷新时间轴。"""
        self.push_history()
        self.render_frame(self._t)
        with contextlib.suppress(Exception): self.timeline.refresh()

    def _layout_spin_changed(self, key):
        """属性面板改布局：layout_at 读 -> 原地写 clip["layout"] -> 压历史。"""
        if self._loading or not self._sel_clip:
            return
        tr, c = V.find_clip(self.proj, self._sel_clip)
        if c is None:
            return
        lay = V.layout_at(c)
        val = getattr(self, "spin_lay_" + key).value()
        if key == "rot":
            lay["rot"] = float(val)
        else:
            lay[key] = float(val)
        if not isinstance(c.get("layout"), dict):
            c["layout"] = V.default_layout()
        c["layout"][key] = lay[key]
        self.push_history()
        self.render_frame(self._t)

    def _align_clip(self, nx, ny):
        """六向对齐：左上基准换算到中心归一化，写回 layout。"""
        if not self._sel_clip:
            return
        tr, c = V.find_clip(self.proj, self._sel_clip)
        if c is None:
            return
        if not isinstance(c.get("layout"), dict):
            c["layout"] = V.default_layout()
        c["layout"]["x"] = float(nx)
        c["layout"]["y"] = float(ny)
        self.push_history()
        self.render_frame(self._t)
        self._fill_clip_props()

    @staticmethod
    def _motion_names():
        """预置动作展示名（来自 motion_table.catalog，C2 数据文件驱动）。"""
        try:
            import motion_table as MT
            return [m["name"] for m in MT.catalog()["motions"]]
        except Exception:
            return ["idle"]

    @staticmethod
    def _expression_names():
        """预置表情展示名（来自 motion_table.catalog）。"""
        try:
            import motion_table as MT
            return [e["name"] for e in MT.catalog()["expressions"]]
        except Exception:
            return []

    @staticmethod
    def _preset_choices(kind):
        """(id, name) 列表，供下拉 addItem(name, id) 写回 clip 字段。"""
        try:
            import motion_table as MT
            cat = MT.catalog()
            if kind == "motion":
                return [(m["id"], m["name"]) for m in cat["motions"]]
            return [(e["id"], e["name"]) for e in cat["expressions"]]
        except Exception:
            return []

    def _motion_changed(self, _idx=0):
        """预置动作下拉 -> 写 clip["motion"]（合法 id）+ 压历史 + 重渲染。"""
        if self._loading or not self._sel_clip:
            return
        tr, c = V.find_clip(self.proj, self._sel_clip)
        if c is None:
            return
        idx = self.cmb_motion.currentIndex()
        if idx < 0 or idx >= len(self._motion_ids):
            return
        c["motion"] = self._motion_ids[idx]
        self.push_history()
        self.render_frame(self._t)

    def _expression_changed(self, _idx=0):
        """预置表情下拉 -> 写 clip["expression"] + 压历史 + 重渲染。"""
        if self._loading or not self._sel_clip:
            return
        tr, c = V.find_clip(self.proj, self._sel_clip)
        if c is None:
            return
        idx = self.cmb_expr.currentIndex()
        if idx < 0 or idx >= len(self._expression_ids):
            return
        c["expression"] = self._expression_ids[idx]
        self.push_history()
        self.render_frame(self._t)

    def _play_clip_preview(self):
        """在选中片段起点 seek 预览（不做导出依赖）。"""
        if not self._sel_clip:
            return
        tr, c = V.find_clip(self.proj, self._sel_clip)
        if c is None:
            return
        self.seek(c["start"])

    def _clip_spin_changed(self, which):
        if self._loading or not self._sel_clip:
            return
        tr, c = V.find_clip(self.proj, self._sel_clip)
        if c is None:
            return
        if which == "start":
            ok, msg = V.move_clip(self.proj, self._sel_clip,
                                  self.spin_start.value())
        elif which == "in":
            nv = self.spin_in.value()
            d = nv - c["src_in"]
            ok = V.trim_clip(self.proj, self._sel_clip, "start",
                             c["start"] + d)[0]
            msg = "入点越界"
        else:
            ok, msg = V.trim_clip(self.proj, self._sel_clip, "end",
                                  c["start"] + self.spin_dur.value())
        if not ok:
            TOAST.toast_warn(self, "修改被拒", msg, duration=2500)
            self._fill_clip_props()
            return
        self.push_history()
        self.timeline.refresh()
        self._after_edit()

    def _delete_selected(self):
        # 多选（框选/Ctrl 加选）时成组删；单选集合就一个元素，行为同旧。
        ids = self.timeline.selected_clip_ids() or (
            [self._sel_clip] if self._sel_clip else [])
        if not ids:
            return
        ok, _m = V.remove_clips(self.proj, ids)
        if ok:
            self._sel_clip = None
            self.push_history()
            self.timeline.refresh()
            self._after_edit()

    def _on_timeline_edit(self):
        """时间轴一次成功编辑（拖/裁/切/删）：先 push 快照再刷新。

        面板约定是「先改数据、后发 edit_committed」，所以这里 push 的是
        **改之后**的状态，撤销栈因此能停在编辑前那一格（push_history 自带
        与栈顶相同则跳过的去重，成组操作只发一次信号也只进一条）。"""
        self.push_history()
        self._after_edit()

    def _after_edit(self):
        """任何成功编辑后：刷新预览/时长/属性。"""
        self._update_transport()
        self._fill_clip_props()
        # v3.6.2：滤镜/特效列表跟着工程变（插入特效、套滤镜、删片段都要刷）
        with contextlib.suppress(Exception): self._refresh_fx_panel()
        if not self._playing:
            self.render_frame(self._t)

    # K1（task-69）：同屏人数上限撤销——原 I1u 事后超限提示整体移除
    # （figure_* 助手保留在 video_model，供未来纯信息展示用）。

    # ================= 导出 =================
    def _open_export_dialog(self):
        dlg = _ExportDialog(self, self.proj)
        if dlg.exec():
            kind = dlg.export_kind()
            # 仅音轨 / SRT：不做视频合成，也不需要帧率校验与立绘预渲染
            if kind in ("audio", "srt"):
                self._export_sidecar(kind, dlg.sidecar_path())
                return
            # v3.5.8：自定义帧率非法时阻止导出
            if not dlg._fps_valid():
                TOAST.toast_warn(self, "帧率无效", "帧率需为 1-120 的整数，请修正后再导出", duration=4000)
                return
            path, scale, fps, workers, animate = dlg.result_values()
            self._start_export(path, scale, fps, workers, animate)

    def _export_sidecar(self, kind, path):
        """导出附属产物：仅音轨 / SRT 字幕（v3.6.2）。

        两者都不走 VideoExportWorker——没有逐帧合成，混音/写字幕都是
        秒级操作，直接在主线程做完并如实汇报结果（含降级说明）。
        """
        import video_export as VE
        if not path:
            TOAST.toast_warn(self, "路径为空", "请先填写保存路径")
            return
        with contextlib.suppress(Exception):
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        aid = getattr(self, "_cur_aid", None)
        try:
            if kind == "srt":
                ok, msg, n = VE.export_srt(self.proj, path, aid=aid)
            else:
                ok, msg, real = VE.export_audio_only(self.proj, path)
                path = real
        except Exception as e:
            ok, msg = False, "导出失败：%s" % e
        if ok:
            TOAST.toast_ok(self, "导出完成", "%s\n%s" % (msg, path), duration=5000)
            with contextlib.suppress(Exception):
                os.startfile(os.path.dirname(os.path.abspath(path)))
        else:
            TOAST.toast_warn(self, "未导出", msg, duration=5000)

    def _start_export(self, path, scale, fps=None, workers=None, animate=None):
        self.stop_play()
        self.act_export.setEnabled(False)
        # v3.5.11：L2D 立绘预渲染（**必须在主线程做**——渲染依赖
        # QWebEngine 事件循环，导出 QThread 里会死锁）。只渲染唯一状态
        # （真实工程 8 个 / 0.2s），导出线程逐帧只查缓存。
        # 失败/无 WebEngine 时静默跳过，导出自动回退占位卡，不阻断。
        self.act_export.setText("准备立绘…")
        QApplication.processEvents()
        n_states = 0
        try:
            import l2d_prefetch as LP
            n_states = LP.build_for_project(self.proj)
        except Exception:
            n_states = 0
        if n_states:
            TOAST.toast_info(self, "立绘已预渲染", "%d 个立绘状态已就绪（导出视频含完整 L2D）" % n_states, duration=4000)
        # 逐帧动画（方案 A）：泵必须在**主线程**起 —— QTimer 要活在有事件
        # 循环的线程里，而 VideoExportWorker.run() 在 QThread，那儿起不了。
        # 上面的定格预渲染在动画模式下**照旧执行**：它是泵的兜底，某帧泵
        # 超时/失败就退回定格图，画面不会缺（宁可少一次眨眼）。
        if animate is None:
            animate = True
        self._export_animate = False
        if animate and n_states:
            try:
                import l2d_pump as LPM
                eff_fps = float(fps) if fps else float(self.proj.get("fps") or 30.0)
                pm = LPM.pump()
                # ring 容量按线程数放大：并行导出乱序窗口 ≈2×workers 帧，
                # 盖不住就要走「回拖重载」那条慢且重的路
                pm.configure(True, eff_fps, VE.normalize_workers(workers))
                self._export_animate = bool(pm.start())
                if not self._export_animate:
                    TOAST.toast_warn(self, "逐帧动画未启用", "渲染泵起不来（%s），本次导出改用定格立绘"
                                    % pm.reason(),
                               duration=5000)
            except Exception:
                self._export_animate = False
        # v3.5.8：导出进度计时（滑动平均估速 -> 剩余时间）
        self._export_t0 = time.monotonic()
        self._export_last_t = self._export_t0
        self._export_last_pct = 0
        self._export_rates = []
        self._export_worker = VE.VideoExportWorker(self.proj, path, scale,
                                                   fps=fps, workers=workers,
                                                   l2d_animate=self._export_animate,
                                                   parent=self)
        self._export_worker.progress.connect(self._export_progress)
        self._export_worker.done.connect(self._export_done)
        self._export_worker.start()

    def _export_progress(self, pct):
        # v3.5.8：滑动平均估速，剩余≈(100-pct)/速率；前两次回调数据不足只显示百分比
        t = time.monotonic()
        last_t = getattr(self, "_export_last_t", None)
        last_pct = getattr(self, "_export_last_pct", None)
        rates = list(getattr(self, "_export_rates", []))
        if last_t is not None and last_pct is not None and t > last_t \
                and pct > last_pct:
            dt = t - last_t
            if dt > 0:
                inst = (pct - last_pct) / dt
                if inst > 0:
                    rates = (rates + [inst])[-3:]
        if last_t is not None:
            self._export_last_t = t
        self._export_last_pct = pct
        self._export_rates = rates
        if len(rates) >= 2:
            rate = sum(rates) / len(rates)
            if rate > 0 and pct < 100:
                remain = (100.0 - pct) / rate
                if remain > 0:
                    secs = int(round(remain))
                    self.act_export.setText("导出 %d%% · 剩约 %d 分 %d 秒"
                                            % (pct, secs // 60, secs % 60))
                    return
        self.act_export.setText("导出 %d%%" % pct)

    def _export_done(self, ok, msg):
        for _k in ("_export_t0", "_export_last_t", "_export_last_pct",
                   "_export_rates"):
            with contextlib.suppress(AttributeError): delattr(self, _k)
        # 逐帧动画：收泵。stop() 会停 QTimer、把页面时钟交回墙上时间，并
        # 唤醒还在等的线程 —— 不 stop 的话下次预览/导出会接手一个被暂停
        # 的 ticker 和残留 slot。
        if getattr(self, "_export_animate", False):
            self._export_animate = False
            with contextlib.suppress(Exception):
                import l2d_pump as LPM
                pm = LPM.pump()
                st = dict(pm.stats)
                pm.stop()
                pm.configure(False)
                bad = st.get("timeouts", 0) + st.get("failed", 0)
                if bad:
                    msg = "%s（立绘动画 %d 帧，其中 %d 帧超时/失败已回落定格）" % (
                        msg, st.get("frames", 0), bad)
        self.act_export.setEnabled(True)
        self.act_export.setText("导出")
        (InfoBar.success if ok else InfoBar.error)(
            "导出完成" if ok else "导出失败", msg, parent=self,
            position=InfoBarPosition.TOP, duration=5000)


class _ExportDialog(MessageBoxBase):
    """剪映式导出参数：分辨率（精确像素）/ 帧率 / 耗时预估。"""

    # v3.5.8：分辨率档位 scale（跟工程 1.0 起），帧率档位（None=跟工程）
    _RES_SCALES = [1.0, 0.6667, 0.5, 0.4444]
    _RES_TAGS = ["跟工程", "720p", "540p", "480p"]
    _FPS_VALS = [None, 60, 30, 24, 15]

    # v3.5.8：并行线程加速比（实测曲线粗略拟合，>8 按 2.1x）
    _SPEEDUP = {1: 1.0, 2: 1.6, 3: 1.85, 4: 2.0, 5: 2.1,
                6: 2.15, 7: 2.15, 8: 2.1}

    @staticmethod
    def _speedup(n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            return 1.0
        return _ExportDialog._SPEEDUP.get(n, 2.1 if n > 8 else 1.0)

    def __init__(self, parent, proj):
        super().__init__(parent)
        self.proj = proj
        self.viewLayout.addWidget(StrongBodyLabel("导出"))

        # v3.6.2：导出内容（视频 / 仅音轨 / SRT 字幕）。
        # 放在最前面——它决定下面哪些参数有意义，也决定默认扩展名。
        rk = QHBoxLayout()
        rk.addWidget(CaptionLabel("导出内容"))
        self.cmb_kind = ComboBox()
        self.cmb_kind.addItems(["视频（MP4）", "仅音轨（WAV/M4A）",
                                "字幕（SRT）"])
        rk.addWidget(self.cmb_kind, 1)
        self.viewLayout.addLayout(rk)

        r = QHBoxLayout()
        r.addWidget(CaptionLabel("保存路径"))
        default = os.path.join(
            PROJ_BASE, (os.path.basename(parent.proj_dir)
                        if getattr(parent, "proj_dir", None)
                        else unique_video_name()) + ".mp4")
        self._default_stem = os.path.splitext(default)[0]
        self.ed_path = LineEdit()
        self.ed_path.setText(default)
        r.addWidget(self.ed_path, 1)
        self.viewLayout.addLayout(r)

        # v3.6.2：视频专属参数装进一个容器，选「仅音轨/字幕」时整块隐藏
        # （而不是逐个控件 setVisible —— 它们挂在 QHBoxLayout 上，没有
        #  可整体隐藏的 widget，逐个藏会留下空行和标签）
        self.video_box = QWidget(self)
        _vb = QVBoxLayout(self.video_box)
        _vb.setContentsMargins(0, 0, 0, 0)
        _vb.setSpacing(8)
        self.viewLayout.addWidget(self.video_box)

        pw = int(proj.get("width", 1920))
        ph = int(proj.get("height", 1080))
        # 分辨率：精确像素，用 proj 实际宽高算目标并四舍五入到偶数
        res_labels = []
        for sc, tag in zip(self._RES_SCALES, self._RES_TAGS):
            tw = max(2, int(round(pw * sc / 2)) * 2)
            th = max(2, int(round(ph * sc / 2)) * 2)
            if tag == "跟工程":
                res_labels.append("跟工程 (%d×%d)" % (tw, th))
            else:
                res_labels.append("%d×%d (%s)" % (tw, th, tag))
        r2 = QHBoxLayout()
        r2.addWidget(CaptionLabel("分辨率"))
        self.cmb_out = ComboBox()
        self.cmb_out.addItems(res_labels)
        r2.addWidget(self.cmb_out, 1)
        _vb.addLayout(r2)

        # 帧率：第一项显示工程实际 fps，末项「自定义…」配数字输入（1-120）
        pfps = float(proj.get("fps", 30) or 30)
        fps_labels = ["跟工程 (%dfps)" % int(round(pfps))]
        fps_labels += ["%dfps" % f for f in self._FPS_VALS[1:]]
        fps_labels.append("自定义…")
        r3 = QHBoxLayout()
        r3.addWidget(CaptionLabel("帧率"))
        self.cmb_fps = ComboBox()
        self.cmb_fps.addItems(fps_labels)
        r3.addWidget(self.cmb_fps, 1)
        self.ed_fps = LineEdit(self)
        self.ed_fps.setPlaceholderText("1-120")
        self.ed_fps.setMaximumWidth(72)
        with contextlib.suppress(Exception):
            from PySide6.QtGui import QIntValidator
            self.ed_fps.setValidator(QIntValidator(1, 120, self.ed_fps))
        self.ed_fps.editingFinished.connect(self._validate_fps)
        r3.addWidget(self.ed_fps)
        self.ed_fps.setVisible(False)
        _vb.addLayout(r3)

        # 并行线程：1..upper_w，default_w 项标「（推荐）」；选项取自
        # video_export.export_worker_limits()，不自己猜核数。
        try:
            import video_export as _VE
            dw, uw = _VE.export_worker_limits()
            dw, uw = int(dw), int(uw)
        except Exception:
            dw, uw = 1, max(1, int(os.cpu_count() or 1))
        if dw < 1 or dw > uw:
            dw = max(1, min(uw, dw))
        self._worker_dw, self._worker_uw = dw, uw
        w_labels = ["%d 线程%s" % (n, "（推荐）" if n == dw else "")
                    for n in range(1, uw + 1)]
        r4 = QHBoxLayout()
        r4.addWidget(CaptionLabel("并行线程"))
        self.cmb_workers = ComboBox()
        self.cmb_workers.addItems(w_labels)
        self.cmb_workers.setCurrentIndex(max(0, dw - 1))
        r4.addWidget(self.cmb_workers, 1)
        _vb.addLayout(r4)
        cpu = max(1, int(os.cpu_count() or 1))
        self.workers_hint = CaptionLabel(
            "线程越多越快，但受内存带宽限制，超过甜点反而变慢；"
            "默认值已按本机核数（%d 核）实测调过" % cpu, self)
        self.workers_hint.setWordWrap(True)
        self.workers_hint.setStyleSheet("color:%s; font-size:11px;"
                                        % TH.t("text_dim"))
        _vb.addWidget(self.workers_hint)

        # 立绘动画：逐帧（方案 A，主线程渲染泵）/ 定格（方案 B，预渲染图）。
        # 默认逐帧——用户明确要动画优先；代价写在提示里，别让人事后惊讶。
        r5 = QHBoxLayout()
        r5.addWidget(CaptionLabel("立绘动画"))
        self.cmb_anim = ComboBox()
        self.cmb_anim.addItems(["逐帧动画（人物会呼吸眨眼，较慢）",
                                "定格（快，人物不动）"])
        self.cmb_anim.setCurrentIndex(0)
        r5.addWidget(self.cmb_anim, 1)
        _vb.addLayout(r5)
        self.anim_hint = CaptionLabel(
            "逐帧：人物呼吸眨眼真的在动。开销随工程而变——合成越重的工程"
            "（视频轨/滤镜）越能被并行隐藏，实测只慢 10% 上下；素材简单的"
            "工程慢 2-3 倍。两种模式产物都可逐位复现。", self)
        self.anim_hint.setWordWrap(True)
        self.anim_hint.setStyleSheet("color:%s; font-size:11px;"
                                     % TH.t("text_dim"))
        _vb.addWidget(self.anim_hint)

        # 耗时预估（CaptionLabel，随任一下拉变化刷新）
        self.est_lbl = CaptionLabel("耗时预估：—", self)
        self.est_lbl.setWordWrap(True)
        self.est_lbl.setStyleSheet("color:%s; font-size:11px;"
                                       % TH.t("text_dim"))
        self.viewLayout.addWidget(self.est_lbl)

        self.cmb_out.currentIndexChanged.connect(self._refresh_estimate)
        self.cmb_fps.currentIndexChanged.connect(self._on_fps_changed)
        self.cmb_workers.currentIndexChanged.connect(self._refresh_estimate)
        self.cmb_anim.currentIndexChanged.connect(self._refresh_estimate)
        self.cmb_kind.currentIndexChanged.connect(self._on_kind_changed)
        self._refresh_estimate()

        self.yesButton.setText("开始导出")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(480)

    # ---- v3.6.2：导出内容（视频 / 仅音轨 / SRT） ----
    _KINDS = ("video", "audio", "srt")
    _KIND_EXT = {"video": ".mp4", "audio": ".wav", "srt": ".srt"}

    def export_kind(self):
        try:
            return self._KINDS[max(0, min(2, self.cmb_kind.currentIndex()))]
        except Exception:
            return "video"

    def sidecar_path(self):
        """仅音轨 / SRT 的保存路径（自动补对应扩展名）。

        空输入必须返回空串——normpath("") 会得到 "."，拼上扩展名就成了
        "..wav" 这种会在当前目录落怪文件的路径（UI 测试 19 抓到）。
        """
        raw = (self.ed_path.text() or "").strip()
        if not raw:
            return ""
        p = os.path.normpath(raw)
        want = self._KIND_EXT[self.export_kind()]
        if os.path.splitext(p)[1].lower() != want:
            p = os.path.splitext(p)[0] + want
        return p

    def _on_kind_changed(self):
        kind = self.export_kind()
        is_video = (kind == "video")
        # 视频专属参数整块显隐（分辨率/帧率/线程/立绘动画）
        self.video_box.setVisible(is_video)
        # 扩展名跟着换，但保留用户已改过的目录与文件名主体。
        # 空输入不动——否则 splitext("")[0] + ext 会凭空造出 ".wav"
        cur = (self.ed_path.text() or "").strip()
        if cur:
            stem = os.path.splitext(cur)[0]
            if stem:                      # 只有 ".mp4" 这种纯扩展名也别造空主体
                self.ed_path.setText(stem + self._KIND_EXT[kind])
        if is_video:
            self._refresh_estimate()
        elif kind == "audio":
            self.est_lbl.setText(
                "只导出整工程混音，不做画面合成（秒级完成）。"
                "与「导出视频」里的音轨逐样本一致；隐藏轨不参与。"
                "改扩展名为 .m4a 可走 ffmpeg 编码，不可用时自动回落 WAV。")
        else:
            self.est_lbl.setText(
                "导出 SRT 字幕（UTF-8 BOM）。时间口径与画面烧录一致："
                "多条台词重叠时取后一条，相邻同内容自动合并；"
                "角色名作前缀，旁白不加。")
        self.yesButton.setText("开始导出" if is_video else "导出")

    def _fps_custom(self):
        return self.cmb_fps.currentIndex() >= len(self._FPS_VALS)

    def _fps_value(self):
        """当前所选帧率：档位返回数字（None=跟工程）；自定义解析输入，非法返回 None。"""
        if not self._fps_custom():
            return self._FPS_VALS[max(0, self.cmb_fps.currentIndex())]
        txt = (self.ed_fps.text() or "").strip()
        try:
            v = int(txt)
        except ValueError:
            return None
        return v if 1 <= v <= 120 else None

    def _fps_valid(self):
        """帧率是否合法（自定义时校验 1-120 整数）。"""
        if not self._fps_custom():
            return True
        txt = (self.ed_fps.text() or "").strip()
        try:
            v = int(txt)
        except ValueError:
            return False
        return 1 <= v <= 120

    def _on_fps_changed(self):
        custom = self._fps_custom()
        self.ed_fps.setVisible(custom)
        if custom:
            self.ed_fps.setFocus()
        self._refresh_estimate()

    def _validate_fps(self):
        if self._fps_custom() and not self._fps_valid():
            TOAST.toast_warn(self, "帧率无效", "帧率需为 1-120 的整数")

    def _refresh_estimate(self):
        """按当前分辨率+帧率估算「预计约 X 分钟（N 帧）」。

        帧数 = 工程时长 × 所选帧率；每帧毫秒 ≈ 96.8×(所选高/1080)² + 5.3×(所选高/1080)²
        （线性外推；粗略预估，实际随素材复杂度浮动）。
        """
        try:
            p = self.proj
            dur = V.project_duration(p)
            sc = self._RES_SCALES[max(0, self.cmb_out.currentIndex())]
            th = max(2, int(round(int(p.get("height", 1080)) * sc / 2)) * 2)
            r2 = (th / 1080.0) ** 2
            fps = self._fps_value()
            if self._fps_custom() and fps is None:
                self.est_lbl.setText(
                    "帧率需为 1-120 的整数 · 粗略预估，实际随素材复杂度浮动")
                return
            if fps is None:
                fps = float(p.get("fps", 30) or 30)
            frames = max(1, int(round(dur * fps)))
            workers = self._workers_value()
            sp = self._speedup(workers)
            ms_per = (96.8 * r2 + 5.3 * r2) / sp
            animate = self._animate_value()
            if animate:
                # 逐帧动画开销实测（同一工程 still/anim 对比）：
                #  · 合成贵的工程（视频轨+滤镜）：主线程取图与 worker 合成
                #    天然重叠，只慢 6-12%；
                #  · 合成便宜的工程（静态图+立绘）：取图（~13ms/帧）成为
                #    瓶颈，慢 2-3 倍；
                #  · workers=1 时两者串行相加，最慢。
                # 取 2.0x 是区间中偏保守的一档：宁可让用户提前看到完成，
                # 也不要 ETA 一路往后跳。
                ms_per *= 2.0
            minutes = frames * ms_per / 60000.0
            self.est_lbl.setText(
                "预计约 %.1f 分钟（%d 帧）· %s · 粗略预估，实际随素材复杂度浮动"
                % (minutes, frames,
                   "含立绘逐帧动画" if animate else "立绘定格（快）"))
        except Exception:
            self.est_lbl.setText("耗时预估：— · 粗略预估，实际随素材复杂度浮动")

    def _workers_value(self):
        """当前所选并行线程数（1..upper_w）。"""
        try:
            return max(1, int(self.cmb_workers.currentIndex()) + 1)
        except Exception:
            return 1

    def _animate_value(self):
        """立绘动画开关：0=逐帧动画，1=定格。异常一律回默认（逐帧）。"""
        try:
            return self.cmb_anim.currentIndex() != 1
        except Exception:
            return True

    def result_values(self):
        scale = self._RES_SCALES[max(0, self.cmb_out.currentIndex())]
        fps = self._fps_value()
        workers = self._workers_value()
        return (os.path.normpath(self.ed_path.text().strip()), scale, fps,
                workers, self._animate_value())

class _ImportModelDialog(MessageBoxBase):
    """导入模型对话框（用户需求：命名 + 一级分类 + 二级改模人 + 缩略图）。

    四个字段的顺序就是用户填写的顺序，从上到下一条直线，不用来回跳：
      ① 源目录（选一个含 model.json 的目录）
      ② 命名（服装名，索引里存「角色·服装名」）
      ③ 一级：角色分类（可新建）
      ④ 二级：改模人（可新建）
      ⑤ 缩略图：两个渠道（自己选图 / 导入后渲染）
    """

    THUMB_PICK, THUMB_RENDER = 0, 1
    THUMB_NONE = 2

    def __init__(self, parent, preset_role=""):
        super().__init__(parent)
        # 这个版本的 MessageBoxBase 只有 yesButton/cancelButton，
        # **没有** titleLabel/contentLabel —— 标题自己加。
        self.viewLayout.addWidget(StrongBodyLabel("导入模型", self))
        _sub = CaptionLabel("把模型复制进木偶素材库（复制后与源目录无关）", self)
        _sub.setStyleSheet("color:%s;" % TH.t("text_dim"))
        self.viewLayout.addWidget(_sub)
        self._src_dir = ""
        self.yesButton.setText("导入")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(520)

        # ① 源目录
        r = QHBoxLayout()
        r.addWidget(CaptionLabel("模型目录"))
        self.ed_src = LineEdit(self)
        self.ed_src.setPlaceholderText("选一个含 model.json 的模型目录")
        self.ed_src.setReadOnly(True)
        r.addWidget(self.ed_src, 1)
        self.btn_src = PushButton("浏览…", self)
        self.btn_src.clicked.connect(self._pick_src)
        r.addWidget(self.btn_src)
        self.viewLayout.addLayout(r)

        # ② 命名
        r = QHBoxLayout()
        r.addWidget(CaptionLabel("命名"))
        self.ed_name = LineEdit(self)
        self.ed_name.setPlaceholderText("服装名，例如：月之森JK")
        r.addWidget(self.ed_name, 1)
        self.viewLayout.addLayout(r)

        # ③ 一级：角色分类
        r = QHBoxLayout()
        r.addWidget(CaptionLabel("角色分类"))
        self.cmb_role = ComboBox(self)
        self._reload_roles(preset_role)
        r.addWidget(self.cmb_role, 1)
        self.btn_new_role = PushButton("新建…", self)
        self.btn_new_role.clicked.connect(self._new_role)
        r.addWidget(self.btn_new_role)
        self.viewLayout.addLayout(r)

        # ④ 二级：改模人
        # qfluentwidgets 的 ComboBox 不支持 setEditable（不是 QComboBox），
        # 所以「新建改模人」用一个输入框 + 按钮，而不是可编辑下拉。
        r = QHBoxLayout()
        r.addWidget(CaptionLabel("改模人"))
        self.cmb_author = ComboBox(self)
        self.cmb_author.addItem("（未署名）")
        for a in self._known_authors():
            self.cmb_author.addItem(a)
        r.addWidget(self.cmb_author, 1)
        self.btn_new_author = PushButton("新建…", self)
        self.btn_new_author.clicked.connect(self._new_author)
        r.addWidget(self.btn_new_author)
        self.viewLayout.addLayout(r)

        # ⑤ 缩略图：两个渠道
        self.viewLayout.addWidget(CaptionLabel("缩略图"))
        r = QHBoxLayout()
        self.cmb_thumb = ComboBox(self)
        self.cmb_thumb.addItems(["自己选一张图", "导入后渲染生成", "先不要"])
        r.addWidget(self.cmb_thumb, 1)
        self.btn_thumb = PushButton("选图…", self)
        self.btn_thumb.clicked.connect(self._pick_thumb)
        r.addWidget(self.btn_thumb)
        self.viewLayout.addLayout(r)
        self._thumb_path = ""
        self.lbl_thumb = CaptionLabel("", self)
        self.lbl_thumb.setStyleSheet("color:%s;" % TH.t("text_dim"))
        self.viewLayout.addWidget(self.lbl_thumb)
        self.cmb_thumb.currentIndexChanged.connect(self._on_thumb_mode)

        # 实时校验提示（离开对话框前就该知道能不能导）
        self.lbl_hint = CaptionLabel("", self)
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color:%s;" % TH.t("text_dim"))
        self.viewLayout.addWidget(self.lbl_hint)
        self.ed_src.textChanged.connect(self._refresh_hint)
        self.ed_name.textChanged.connect(self._refresh_hint)
        self._on_thumb_mode()
        self._refresh_hint()

    # ---------- 数据填充 ----------
    @staticmethod
    def _known_authors():
        """素材库里已出现过的改模人（去重、排序），供下拉直接选。"""
        out = set()
        try:
            for m in CL.all_models():
                a = str(m.get("author") or "").strip()
                if a: out.add(a)
        except Exception:
            pass
        return sorted(out)

    def _reload_roles(self, preset=""):
        self.cmb_role.clear()
        try:
            rs = CL.roles()
        except Exception:
            rs = []
        names = [r["cn"] for r in rs]
        self.cmb_role.addItems(names or ["（素材库无分类）"])
        self._role_slugs = [r["slug"] for r in rs]
        if preset:
            for i, r in enumerate(rs):
                if r["slug"] == preset or r["cn"] == preset:
                    self.cmb_role.setCurrentIndex(i)
                    return
        # 新素材库/空库时 roles() 为空——要能把「内置分类」都列出来才行，
        # 否则第一次导入根本选不到分类（实测用户第一台机器就是这个状态）。
        if not rs:
            self._populate_builtin_roles()
        elif not self._role_slugs:
            self._populate_builtin_roles()

    def _populate_builtin_roles(self):
        """空素材库兜底：列出内置 + 自定义分类（没有模型也该能选）。"""
        try:
            all_roles = CL._role_info_all()
        except Exception:
            all_roles = []
        if not all_roles:
            return
        self.cmb_role.clear()
        self.cmb_role.addItems([cn for _s, cn in all_roles])
        self._role_slugs = [s for s, _cn in all_roles]

    # ---------- 交互 ----------
    def _pick_src(self):
        d = QFileDialog.getExistingDirectory(self, "选择模型目录（含 model.json）",
                                             self._src_dir or "")
        if not d:
            return
        self._src_dir = d
        self.ed_src.setText(d)
        # 目录名当默认命名，用户可改
        if not self.ed_name.text().strip():
            self.ed_name.setText(os.path.basename(os.path.normpath(d)))
        self._refresh_hint()

    def _pick_thumb(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选一张缩略图", os.path.expanduser("~"),
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not p:
            return
        self._thumb_path = p
        self.lbl_thumb.setText("已选：%s" % os.path.basename(p))
        # 选了图就切到「自己选一张图」渠道
        self.cmb_thumb.setCurrentIndex(self.THUMB_PICK)

    def _on_thumb_mode(self, *_):
        is_pick = self.cmb_thumb.currentIndex() == self.THUMB_PICK
        self.btn_thumb.setEnabled(is_pick)
        if not is_pick:
            self.lbl_thumb.setText("")
        self._refresh_hint()

    def _new_role(self):
        """新建一级分类：输入名字 -> 立刻出现在下拉里。"""
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "新建角色分类", "分类名：")
        if not ok or not str(name).strip():
            return
        try:
            CL.register_role(str(name).strip())
        except Exception as e:
            TOAST.toast_err(self, "新建分类失败", str(e))
            return
        self._reload_roles(preset=str(name).strip())

    def _new_author(self):
        """新建二级「改模人」：加进下拉并选中。

        改模人不用持久化登记——它本来就是模型目录里的署名，导入时写进
        目录即可；这里只是让用户少打一次字。
        """
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "新建改模人", "改模人名称：")
        name = str(name or "").strip()
        if not ok or not name:
            return
        for i in range(self.cmb_author.count()):
            if self.cmb_author.itemText(i) == name:
                self.cmb_author.setCurrentIndex(i)
                return
        self.cmb_author.addItem(name)
        self.cmb_author.setCurrentIndex(self.cmb_author.count() - 1)

    def _refresh_hint(self, *_):
        """实时告诉用户「还差什么」，而不是点了导入才报错。"""
        self.yesButton.setEnabled(self._ready())
        missing = []
        if not self._src_dir:
            missing.append("选模型目录")
        elif not os.path.isfile(os.path.join(self._src_dir, "model.json")):
            missing.append("该目录没有 model.json")
        if not self.ed_name.text().strip():
            missing.append("填命名")
        if self.cmb_thumb.currentIndex() == self.THUMB_PICK \
                and not self._thumb_path:
            missing.append("选缩略图（或改成别的方式）")
        self.lbl_hint.setText("还需：%s" % "、".join(missing) if missing else "可以导入")

    def _ready(self):
        if not self._src_dir:
            return False
        if not os.path.isfile(os.path.join(self._src_dir, "model.json")):
            return False
        if not self.ed_name.text().strip():
            return False
        if self.cmb_thumb.currentIndex() == self.THUMB_PICK \
                and not self._thumb_path:
            return False
        return bool(self._role_slugs)

    # ---------- 结果 ----------
    def values(self):
        """-> (src_dir, role_slug, author, costume, thumb_mode, thumb_src)"""
        idx = self.cmb_role.currentIndex()
        slug = self._role_slugs[idx] if 0 <= idx < len(self._role_slugs) else ""
        au = self.cmb_author.currentText().strip()
        if au in ("（未署名）", ""):
            au = ""
        ti = self.cmb_thumb.currentIndex()
        mode = {0: "pick", 1: "render", 2: "none"}.get(ti, "none")
        return (self._src_dir, slug, au, self.ed_name.text().strip(),
                mode, self._thumb_path if mode == "pick" else "")


class _ActNameDialog(MessageBoxBase):
    """F2-L5：幕重命名的单行输入对话框。"""

    def __init__(self, parent, old=""):
        super().__init__(parent)
        self.titleLabel.setText("重命名幕")
        self.contentLabel.setText("给这一幕起个名字：")
        self.ed = LineEdit(self)
        self.ed.setText(str(old or ""))
        self.ed.setPlaceholderText("幕名")
        self.viewLayout.addWidget(self.ed)
        self.yesButton.setText("确定")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(360)

    def value(self):
        return self.ed.text()


# 注：窗口壳（FluentWindow 包装）在 l2d_rig_qt.FluentShellWindow 里做，
# 本模块只出纯 QWidget 工作台，避免双向 import。
