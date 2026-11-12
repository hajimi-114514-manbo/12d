# -*- coding: utf-8 -*-
"""欢迎页（v2.12.0）：启动后先选项目类型 / 打开历史项目，然后才进工作台。

布局仿 After Effects 主页：
    ┌──────────┬────────────────────────────────────┐
    │ 新建项目  │        欢迎使用 木偶工作室            │
    │ 打开项目  │  最近使用项          [筛选][____]    │
    │ ──────   │  ┌────────┬────────┬─────┬────┐    │
    │ 🏠 主页  │  │ 名称    │最近使用 │ 大小 │类型 │    │
    │          │  └────────┴────────┴─────┴────┘    │
    └──────────┴────────────────────────────────────┘

约定：本模块**不 import l2d_rig_qt**（脚本直跑时 __main__ 双实例问题），
最近列表 / 版本号 / 图标路径全部由 run_app 传进来；交互结果用信号发出。
"""
import json
import os
import time

from PySide6.QtCore import Qt, Signal, QSize, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QFrame,
                               QAbstractItemView, QHeaderView, QSizePolicy,
                               QFileDialog, QTableWidgetItem)
from qfluentwidgets import (PrimaryPushButton, PushButton, LineEdit,
                            TableWidget, StrongBodyLabel, BodyLabel,
                            CaptionLabel, MessageBoxBase, InfoBar,
                            InfoBarPosition, TogglePushButton,
                            ToolButton, FluentIcon)

# 更新检查（task-13）：只依赖它自己的 QtCore，方向单向，无循环 import
import update_checker as _UC

# 本文件与 l2d_rig_qt.py 同级（V2 根目录）；不 import 它（__main__ 双实例坑）
_V2 = os.path.dirname(os.path.abspath(__file__))

PAGE_SIZE = 10          # 最近列表每页行数
MAX_PER_TYPE = 50       # 每种类型（图片/视频）最多展示工程数


def is_project_dir(d):
    """工程目录判定：layers_meta.json / ComfyUI *_layers.json / video_project.json。"""
    if not d or not os.path.isdir(d):
        return False
    for fn in ("layers_meta.json", "video_project.json"):
        if os.path.isfile(os.path.join(d, fn)):
            return True
    try:
        return any(f.endswith("_layers.json") for f in os.listdir(d))
    except OSError:
        return False


def _rel_time(ts):
    """时间戳 -> AE 风格相对时间（刚刚 / X分钟前 / X小时前 / X天前 / 日期）。"""
    if not ts:
        return "—"
    d = max(0, time.time() - ts)
    if d < 60:
        return "刚刚"
    if d < 3600:
        return "%d分钟前" % int(d // 60)
    if d < 86400:
        return "%d小时前" % int(d // 3600)
    if d < 30 * 86400:
        return "%d天前" % int(d // 86400)
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _proj_mtime(d):
    """工程"最近使用"时间：meta/工程 json/目录三者取最新 mtime。"""
    best = 0.0
    for fn in ("layers_meta.json", "rig_project.json", "video_project.json"):
        try:
            best = max(best, os.path.getmtime(os.path.join(d, fn)))
        except OSError:
            pass
    try:
        best = max(best, os.path.getmtime(d))
    except OSError:
        pass
    return best


def _dir_size(d):
    """目录总大小（字节）。工程目录可能上 GB，walk 一次可接受（欢迎页只算 ≤5 个）。"""
    tot = 0
    try:
        for root, _dirs, files in os.walk(d):
            for fn in files:
                try:
                    tot += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
    except OSError:
        pass
    return tot


def _human_size(n):
    if n <= 0:
        return "—"
    for unit, div in (("GB", 2 ** 30), ("MB", 2 ** 20), ("KB", 2 ** 10)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % n


def _proj_display_name(d, fallback=""):
    """工程名：优先 rig_project.json 的 name，否则目录名（与 Workbench 同规则）。"""
    try:
        p = json.load(open(os.path.join(d, "rig_project.json"),
                           encoding="utf8"))
        nm = (p.get("name") or "").strip()
        if nm:
            return nm
    except Exception:
        pass
    return fallback or os.path.basename(d)


class _TypeCard(QFrame):
    """项目类型卡片：可选中（蓝框），disabled 卡片置灰不可选。"""

    clicked = Signal(str)

    def __init__(self, type_id, title, desc, enabled=True, parent=None):
        super().__init__(parent)
        self.type_id = type_id
        self.enabled = enabled
        self.selected = False
        self.setObjectName("typeCard")
        self.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        t = StrongBodyLabel(title if enabled else title + "（敬请期待）", self)
        dsc = CaptionLabel(desc, self)
        dsc.setWordWrap(True)
        lay.addWidget(t)
        lay.addWidget(dsc)
        if not enabled:
            t.setEnabled(False)
            dsc.setEnabled(False)
        self._refresh()

    def _refresh(self):
        if not self.enabled:
            ss = "#typeCard{background:#24272e;border:1px solid #2c3038;border-radius:8px;}"
        elif self.selected:
            ss = "#typeCard{background:#263042;border:2px solid #4d88ff;border-radius:8px;}"
        else:
            ss = "#typeCard{background:#24272e;border:1px solid #3a3f4a;border-radius:8px;}"
        self.setStyleSheet(ss)

    def mousePressEvent(self, ev):
        if self.enabled and ev.button() == Qt.LeftButton:
            self.clicked.emit(self.type_id)
            ev.accept()
            return
        super().mousePressEvent(ev)


class _TypeDialog(MessageBoxBase):
    """选择项目类型。目前只有「图片编辑」；后续 v3 加视频工程。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.title_lbl = StrongBodyLabel("选择项目类型", self)
        self.viewLayout.addWidget(self.title_lbl)
        self._cards = []
        self.chosen = "image_edit"
        c1 = _TypeCard("image_edit", "图片编辑",
                       "把一张插画分解成图层、绑骨架，做伪 L2D 动画",
                       enabled=True, parent=self)
        c2 = _TypeCard("video_edit", "视频编辑（测试功能）",
                       "多轨时间轴剪辑：图片/视频/音频/木偶动画合成导出",
                       enabled=True, parent=self)
        for c in (c1, c2):
            c.clicked.connect(self._pick)
            self.viewLayout.addWidget(c)
            self._cards.append(c)
        c1.selected = True
        c1._refresh()
        self.yesButton.setText("进入工作台")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(420)

    def _pick(self, type_id):
        for c in self._cards:
            c.selected = (c.type_id == type_id and c.enabled)
            c._refresh()
        self.chosen = type_id


class _RecentTable(TableWidget):
    """历史项目表：回车打开选中行（双击之外的常规路径）。"""

    enter_pressed = Signal(int)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            it = self.currentItem()
            if it is not None:
                self.enter_pressed.emit(it.row())
                ev.accept()
                return
        super().keyPressEvent(ev)


class WelcomeWindow(QWidget):
    """启动欢迎页。信号：
        new_requested(type_id)   新建某类型项目（当前只会是 image_edit）
        open_requested(dir)      打开工程目录（历史行 或 「打开项目」选好的目录）
    「打开项目」在**本页**弹目录选择框，选完才发信号——绝不先把用户换进工作台
    （v2.12.0 流程 bug：browse 先建主窗再弹框，工作台直接露出来了）。
    """

    new_requested = Signal(str)
    open_requested = Signal(str)

    def __init__(self, items=None, version="", icon_path="", parent=None):
        super().__init__(parent)
        self.setObjectName("welcomeRoot")
        self.setWindowTitle("木偶工作室" + (" v" + version if version else ""))
        if icon_path and os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.items = [it for it in (items or [])
                      if it.get("dir") and os.path.isdir(it["dir"])]
        self._tab = "all"          # all | image | video
        self._page = 0
        self._filt = ""
        self._rows = []            # 当前页行 -> 工程目录（_open_row 用）
        self._version = str(version or "")
        self._upd_thread = None    # UpdateThread（检查更新，task-13）
        self.resize(1060, 660)
        self.setStyleSheet(
            "#welcomeRoot{background:#1f2126;}"
            "QFrame#sideBar{background:#262932;border-radius:0px;}")

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---------- 左侧栏 ----------
        side = QFrame(self)
        side.setObjectName("sideBar")
        side.setFixedWidth(260)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(24, 28, 24, 24)
        sl.setSpacing(10)
        self.btn_new = PrimaryPushButton("新建项目", side)
        self.btn_new.setFixedHeight(38)
        self.btn_new.clicked.connect(self._on_new)
        sl.addWidget(self.btn_new)
        self.btn_open = PushButton("打开项目", side)
        self.btn_open.setFixedHeight(38)
        self.btn_open.clicked.connect(self._on_browse)
        sl.addWidget(self.btn_open)
        sl.addStretch(1)
        tip = CaptionLabel("每种工程最多保留 50 个", side)
        tip.setStyleSheet("color:#8a93a2;")
        sl.addWidget(tip)
        # ---------- 左下角：检查更新（task-13）----------
        upd_row = QHBoxLayout()
        upd_row.setSpacing(10)
        _up_icon = getattr(FluentIcon, "UPDATE", None) or \
            getattr(FluentIcon, "SYNC", None)
        self.btn_update = ToolButton(_up_icon, side)
        self.btn_update.setObjectName("btnUpdate")
        self.btn_update.setFixedSize(32, 32)
        self.btn_update.setIconSize(QSize(18, 18))
        if _up_icon is not None:
            self.btn_update.setIcon(_up_icon)   # 双保险：旧版构造签名只认 parent
        self.btn_update.setCursor(Qt.PointingHandCursor)
        self.btn_update.setToolTip("检查更新")
        self.btn_update.setStyleSheet(
            "#btnUpdate{border-radius:16px;background:#2c3038;}"
            "#btnUpdate:hover{background:#3a4150;}")
        self.btn_update.clicked.connect(self._on_check_update)
        upd_row.addWidget(self.btn_update)
        self.update_lbl = CaptionLabel("", side)
        self.update_lbl.setStyleSheet("color:#8a93a2;")
        self.update_lbl.setWordWrap(False)
        upd_row.addWidget(self.update_lbl, 1)
        upd_row.addStretch(1)
        sl.addLayout(upd_row)
        root.addWidget(side)

        # ---------- 右侧主区 ----------
        main = QWidget(self)
        ml = QVBoxLayout(main)
        ml.setContentsMargins(40, 36, 40, 28)
        ml.setSpacing(12)
        self.title = StrongBodyLabel("欢迎使用 木偶工作室", main)
        f = self.title.font()
        f.setPixelSize(28)
        f.setBold(True)
        self.title.setFont(f)
        self.title.setAlignment(Qt.AlignHCenter)
        self.title.setStyleSheet("color:#f2f6fa;")
        ml.addWidget(self.title)
        ml.addSpacing(22)

        head = QHBoxLayout()
        h_lbl = StrongBodyLabel("最近使用项", main)
        h_lbl.setStyleSheet("color:#c8d1dc;")
        head.addWidget(h_lbl)
        head.addSpacing(16)
        # 类型分段：全部 | 图片工程 | 视频工程
        self._tabs = {}
        for key, text in (("all", "全部"), ("image", "图片工程"),
                          ("video", "视频工程")):
            tb = TogglePushButton(text, main)
            tb.setChecked(key == "all")
            tb.clicked.connect(lambda _c=False, k=key: self._set_tab(k))
            head.addWidget(tb)
            self._tabs[key] = tb
        head.addStretch(1)
        cap = CaptionLabel("筛选", main)
        cap.setStyleSheet("color:#8a93a2;")
        head.addWidget(cap)
        self.ed_filter = LineEdit(main)
        self.ed_filter.setPlaceholderText("筛选最近的文件")
        self.ed_filter.setFixedWidth(220)
        self.ed_filter.setClearButtonEnabled(True)
        self.ed_filter.textChanged.connect(self._filter)
        head.addWidget(self.ed_filter)
        ml.addLayout(head)

        self.table = _RecentTable(main)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["名称", "最近使用", "大小", "类型"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(240)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 90)
        self.table.itemDoubleClicked.connect(self._on_dbl)
        self.table.enter_pressed.connect(self._open_row)
        ml.addWidget(self.table, 1)

        self.empty_lbl = BodyLabel(
            "暂无历史项目 —— 点击左侧「新建项目」开始", main)
        self.empty_lbl.setAlignment(Qt.AlignCenter)
        self.empty_lbl.setStyleSheet("color:#8a93a2;")
        self.empty_lbl.hide()
        ml.addWidget(self.empty_lbl)

        # ---------- 分页条 ----------
        pager = QHBoxLayout()
        pager.addStretch(1)
        self.btn_prev = PushButton("‹ 上一页", main)
        self.page_lbl = CaptionLabel("第 1 / 1 页", main)
        self.page_lbl.setStyleSheet("color:#c8d1dc;")
        self.btn_next = PushButton("下一页 ›", main)
        self.btn_prev.clicked.connect(lambda: self._goto_page(self._page - 1))
        self.btn_next.clicked.connect(lambda: self._goto_page(self._page + 1))
        pager.addWidget(self.btn_prev)
        pager.addSpacing(12)
        pager.addWidget(self.page_lbl)
        pager.addSpacing(12)
        pager.addWidget(self.btn_next)
        pager.addStretch(1)
        ml.addLayout(pager)

        root.addWidget(main, 1)
        self._populate()

    # ---------------- 数据 ----------------
    @staticmethod
    def _is_video(d):
        return os.path.isfile(os.path.join(d, "video_project.json"))

    def _typed_pool(self):
        """按类型各截前 MAX_PER_TYPE 个，合并后保持原顺序（新在前）。"""
        vi = ii = 0
        pool = []
        for it in self.items:
            d = it.get("dir") or ""
            if not d or not os.path.isdir(d):
                continue
            vid = self._is_video(d)
            if vid:
                vi += 1
                if vi > MAX_PER_TYPE:
                    continue
            else:
                ii += 1
                if ii > MAX_PER_TYPE:
                    continue
            pool.append((it, vid))
        return pool

    def _filtered(self):
        t = self._filt.strip().casefold()
        out = []
        for it, vid in self._typed_pool():
            if self._tab == "video" and not vid:
                continue
            if self._tab == "image" and vid:
                continue
            if t:
                nm = _proj_display_name(
                    it["dir"], it.get("name") or "").casefold()
                if t not in nm:
                    continue
            out.append((it, vid))
        return out

    def _set_tab(self, key):
        if self._tab == key:
            return
        self._tab = key
        for k, tb in self._tabs.items():
            tb.blockSignals(True)
            tb.setChecked(k == key)
            tb.blockSignals(False)
        self._page = 0
        self._refresh_view()

    def _goto_page(self, p):
        rows = self._filtered()
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        p = max(0, min(p, pages - 1))
        if p != self._page:
            self._page = p
            self._refresh_view()

    def _populate(self):
        self._page = 0
        self._refresh_view()

    def _refresh_view(self):
        rows = self._filtered()
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._page = min(self._page, pages - 1)
        chunk = rows[self._page * PAGE_SIZE:(self._page + 1) * PAGE_SIZE]
        self.table.setRowCount(len(chunk))
        self._rows = []
        for r, (it, vid) in enumerate(chunk):
            d = it["dir"]
            name = _proj_display_name(d, it.get("name") or "")
            cells = [name, _rel_time(_proj_mtime(d)),
                     _human_size(_dir_size(d)),
                     "视频工程" if vid else "图片工程"]
            for c, txt in enumerate(cells):
                wi = QTableWidgetItem(txt)
                wi.setToolTip(d if c == 0 else "")
                self.table.setItem(r, c, wi)
            self._rows.append(d)
        self.page_lbl.setText("第 %d / %d 页" % (self._page + 1, pages))
        self.btn_prev.setEnabled(self._page > 0)
        self.btn_next.setEnabled(self._page < pages - 1)
        if not rows:
            if self._filt.strip():
                self.empty_lbl.setText("没有匹配「%s」的项目"
                                       % self._filt.strip())
            elif self._tab != "all":
                self.empty_lbl.setText("该类型还没有工程 —— 点击左侧「新建项目」开始")
            else:
                self.empty_lbl.setText("暂无历史项目 —— 点击左侧「新建项目」开始")
        self.empty_lbl.setVisible(not rows)

    # ---------------- 交互 ----------------
    def _open_row(self, r):
        if r < 0 or r >= len(self._rows):
            return
        d = self._rows[r]
        if not os.path.isdir(d):
            return
        self.open_requested.emit(d)

    def _on_dbl(self, item):
        self._open_row(item.row())

    def _on_new(self):
        dlg = _TypeDialog(self)
        if dlg.exec():
            self.new_requested.emit(dlg.chosen)

    def _on_browse(self):
        """「打开项目」：在欢迎页内弹目录框，选**且校验通过**才发 open_requested；
        取消/非法目录都留在本页——绝不先把用户换进工作台。"""
        start = os.path.join(_V2, "projects")
        if not os.path.isdir(start):
            start = _V2
        d = QFileDialog.getExistingDirectory(
            self, "打开工程（选择含 layers_meta.json 的文件夹）", start)
        if not d:
            return
        if not is_project_dir(d):
            InfoBar.error("不是工程目录",
                          "所选文件夹里没有 layers_meta.json"
                          "（也没有 ComfyUI 的 *_layers.json）：\n%s" % d,
                          parent=self, position=InfoBarPosition.TOP,
                          duration=5000)
            return
        self.open_requested.emit(os.path.normpath(d))

    def _filter(self, text):
        self._filt = text or ""
        self._page = 0
        self._refresh_view()

    # ---------------- 检查更新（task-13，点击才发网络请求，不阻塞启动） ----------------
    def _on_check_update(self):
        th = self._upd_thread
        if th is not None and th.isRunning():
            return                       # 已在检查/下载：忽略重复点击
        p = _UC.pending_installer()
        if p:
            InfoBar.success("新版已下载", "退出后自动安装\n%s" % p,
                            parent=self, position=InfoBarPosition.TOP,
                            duration=6000)
            return
        self.btn_update.setEnabled(False)
        self.update_lbl.setText("检查中…")
        th = _UC.UpdateThread(self._version, parent=self)
        self._upd_thread = th
        th.status.connect(self._on_upd_status)
        th.progressed.connect(self._on_upd_progress)
        th.uptodate.connect(self._on_upd_uptodate)
        th.ready.connect(self._on_upd_ready)
        th.failed.connect(self._on_upd_failed)
        th.finished.connect(lambda: self.btn_update.setEnabled(True))
        th.start()

    def _on_upd_status(self, text):
        self.update_lbl.setText(text)

    def _on_upd_progress(self, done, total):
        pct = int(done * 100 / total) if total else 0
        self.btn_update.setToolTip("下载中 %d%%" % pct)

    def _on_upd_uptodate(self, ver):
        self.update_lbl.setText("已是最新版本 v%s" % ver)
        self.btn_update.setToolTip("检查更新")
        QTimer.singleShot(6000, lambda: self.update_lbl.setText("")
                          if self.update_lbl.text().startswith("已是最新版本")
                          else None)

    def _on_upd_ready(self, path):
        self.update_lbl.setText("新版已下载，退出后自动安装")
        self.btn_update.setToolTip("检查更新")
        InfoBar.success("新版已下载", "退出后自动安装\n%s" % path,
                        parent=self, position=InfoBarPosition.TOP,
                        duration=8000)

    def _on_upd_failed(self, msg):
        self.update_lbl.setText("")
        self.btn_update.setToolTip("检查更新")
        InfoBar.warning("检查更新", msg, parent=self,
                        position=InfoBarPosition.TOP, duration=5000)
