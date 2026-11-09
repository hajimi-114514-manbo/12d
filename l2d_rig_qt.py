# -*- coding: utf-8 -*-
"""伪L2D 骨架工作室 V2.0 —— PySide6 + qfluentwidgets（照 Vocal2Midi 风格）

功能对齐 V1.0，并新增左上角菜单：撤销/重做/新建/导入PSD/保存/另存为。
SeeThrough 拆分走 QThread，绝不阻塞 UI。
引擎接口全部复用 l2d_rig_core / decompose（v2 副本）。
"""
import os
import sys
import copy
import json
import time
import shutil
import tempfile
import numpy as np
from PIL import Image

from PySide6.QtWidgets import (QApplication, QWidget, QHBoxLayout, QVBoxLayout,
                               QGridLayout, QSizePolicy, QFileDialog, QLabel,
                               QStackedWidget, QListWidgetItem, QTreeWidgetItem,
                               QSplitter)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QPoint, QSize, QEvent, QRect
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QKeySequence, QShortcut, QIcon, QCursor

from qfluentwidgets import (FluentWindow, FluentIcon, PushButton,
                            PrimaryPushButton, ToolButton, ToggleToolButton,
                            TransparentToolButton, TransparentToggleToolButton,
                            TransparentDropDownPushButton, CheckBox,
                            DropDownPushButton, Action, RoundMenu, ListWidget,
                            TreeWidget, Slider, BodyLabel, CaptionLabel,
                            StrongBodyLabel, FluentStyleSheet, setTheme, Theme,
                            MessageBox, InfoBar, InfoBarPosition)


class ToggleDropDownButton(TransparentDropDownPushButton):
    """下拉按钮自管理开合状态，不依赖 isVisible() 时机：
    - Popup 抓取失效（frameless 常见）：第二次点击的 release 会到达按钮 -> 按 _open 标志 close
    - Popup 抓取正常：按下已把菜单关掉（aboutToHide），随后 release 到达按钮 -> 250ms 去抖不重开
    """

    menu_opened = Signal(object)     # 携带菜单对象，看门狗据此启动

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._open = False
        self._closed_at = 0.0

    def _showMenu(self):
        menu = self.menu()
        if menu is None:
            return
        if self._open:
            if menu.isVisible():
                menu.close()
            self.note_closed()          # RoundMenu.close() 不发 aboutToHide，主动记账
            return
        if menu.isVisible():
            # _open=False 但菜单可见 = 外部途径关不掉/失步，按"用户想关"处理
            menu.close()
            self.note_closed()
            return
        if time.monotonic() - self._closed_at < 0.25:
            return                    # 刚被 Popup 抓取机制关过，忽略这次"穿透"的 release
        super()._showMenu()
        self._open = True
        self.menu_opened.emit(menu)

    def note_closed(self):
        self._open = False
        self._closed_at = time.monotonic()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import l2d_rig_core as C
import decompose

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")


def zh_err(e):
    """把常见英文异常翻成人话中文，其余保留原始信息"""
    s = str(e)
    if "zero-size array" in s or "empty array" in s:
        return "图层数据为空：PSD 里存在不含有效像素的图层（空配饰层）"
    if isinstance(e, FileNotFoundError) or "No such file" in s:
        return "找不到文件：%s" % s.split("] ")[-1]
    if isinstance(e, PermissionError) or "being used by another process" in s:
        return "文件被其他程序占用，请先关闭它再重试"
    if "cannot identify image file" in s:
        return "文件不是有效的图像/PSD 格式"
    if isinstance(e, ImportError):
        return "缺少依赖库：%s" % s
    if isinstance(e, MemoryError):
        return "内存不足，请关闭其他程序后重试"
    return "%s：%s" % (type(e).__name__, s[:160])
V2 = os.path.dirname(os.path.abspath(__file__))
# 输出/示例数据全部相对应用目录：整个文件夹搬到哪都不影响
OUT_BASE = os.path.join(V2, "seethrough_out")
PREVIEW_SCALE = 0.5          # 播放/画布用 LOD 分辨率（导出 PSD/帧/MP4 仍是全分辨率）
ICON_PNG = os.path.join(V2, "laowu_icon.png")
ICON_ICO = os.path.join(V2, "laowu_icon.ico")
DEFAULT_DIR = os.path.join(V2, "示例工程")
# ffmpeg 回退：应用自带优先，其次历史上放过的共享位置
FFMPEG_CANDIDATES = [os.path.join(V2, "ffmpeg.exe"),
                     r"D:\BaiduNetdiskDownload\Vocal2Midi\ffmpeg.exe"]


# ---------------- 历史栈（撤销/重做） ----------------
class History:
    """快照式历史：存 (layers 顺序, draw_order, 各节点支点)"""

    def __init__(self, limit=60):
        self.limit = limit
        self.undo_stack = []
        self.redo_stack = []

    def snapshot(self, studio):
        if studio.rig is None:
            return None
        return {
            "order": [l["name"] for l in studio.layers],
            "draw_order": list(studio.rig.get("draw_order", [])),
            "pivots": {n["node"]: list(n["pivot"]) for n in studio.rig["nodes"]},
        }

    def push(self, studio):
        snap = self.snapshot(studio)
        if snap is None:
            return
        self.undo_stack.append(snap)
        if len(self.undo_stack) > self.limit:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def apply(self, studio, snap):
        if snap is None or studio.rig is None:
            return
        by = {l["name"]: l for l in studio.layers}
        studio.layers = [by[n] for n in snap["order"] if n in by]
        studio.rig["draw_order"] = list(snap["draw_order"])
        for n in studio.rig["nodes"]:
            if n["node"] in snap["pivots"]:
                n["pivot"] = list(snap["pivots"][n["node"]])
        studio.reload_widgets()

    def undo(self, studio):
        if not self.undo_stack:
            return False
        self.redo_stack.append(self.snapshot(studio))
        self.apply(studio, self.undo_stack.pop())
        return True

    def redo(self, studio):
        if not self.redo_stack:
            return False
        self.undo_stack.append(self.snapshot(studio))
        self.apply(studio, self.redo_stack.pop())
        return True


# ---------------- 预览画布 ----------------
class RigCanvas(QWidget):
    pivot_dragged = Signal(str, float, float)      # node, x, y
    pivot_released = Signal()
    image_dropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(360, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.pixmap = None
        self.img_w = self.img_h = 0
        self.pivots = {}          # node -> (x, y)
        self.drag_node = None
        self.hint = ""
        self.setAcceptDrops(True)
        self._geom = (0, 0, 1.0, 0, 0)   # ox, oy, s, dw, dh

    def set_frame(self, img, pivots, hint=""):
        h, w = img.shape[:2]
        qimg = QImage(np.ascontiguousarray(img).data, w, h, 3 * w,
                      QImage.Format.Format_RGB888)
        self.pixmap = QPixmap.fromImage(qimg.copy())
        self.img_w, self.img_h = w, h
        self.pivots = dict(pivots)
        self.hint = hint
        self.update()

    def _layout(self):
        cw, ch = self.width(), self.height()
        if not self.pixmap or cw < 4 or ch < 4:
            return
        s = min((cw - 20) / self.img_w, (ch - 20) / self.img_h)
        s = max(s, 0.02)
        dw, dh = int(self.img_w * s), int(self.img_h * s)
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self._geom = (ox, oy, s, dw, dh)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1b1f27"))
        if not self.pixmap:
            if self.hint:
                p.setPen(QColor("#8a93a5"))
                f = p.font(); f.setPointSize(13); p.setFont(f)
                p.drawText(self.rect(), Qt.AlignCenter, self.hint)
            return
        self._layout()
        ox, oy, s, dw, dh = self._geom
        p.drawPixmap(QPoint(ox, oy), self.pixmap.scaled(
            dw, dh, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
        for node, (x, y) in self.pivots.items():
            cx, cy = ox + x * s, oy + y * s
            p.setPen(QColor("#ffd166"))
            p.setBrush(QColor("#ff5c5c"))
            p.drawEllipse(QPoint(int(cx), int(cy)), 7, 7)
            p.setPen(QColor("#ffd166"))
            p.drawText(int(cx) + 10, int(cy) - 8, node)
        p.end()

    def _node_at(self, pos):
        best, bestd = None, 14.0
        ox, oy, s, dw, dh = self._geom
        for node, (x, y) in self.pivots.items():
            d = ((ox + x * s) - pos.x()) ** 2 + ((oy + y * s) - pos.y()) ** 2
            if d < bestd ** 2:
                bestd, best = d ** 0.5, node
        return best

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            n = self._node_at(e.position().toPoint())
            if n:
                self.drag_node = n

    def mouseMoveEvent(self, e):
        if self.drag_node:
            ox, oy, s, dw, dh = self._geom
            x = (e.position().x() - ox) / s
            y = (e.position().y() - oy) / s
            x = max(0.0, min(self.img_w - 1.0, x))
            y = max(0.0, min(self.img_h - 1.0, y))
            self.pivot_dragged.emit(self.drag_node, x, y)

    def mouseReleaseEvent(self, e):
        if self.drag_node:
            self.drag_node = None
            self.pivot_released.emit()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p.lower().endswith(IMG_EXT):
                self.image_dropped.emit(p)
                break


# ---------------- 最近工程（持久化，最多 5 条） ----------------
RECENT_FILE = os.path.join(V2, "recent_projects.json")
RECENT_MAX = 5


def load_recent():
    try:
        items = json.load(open(RECENT_FILE, encoding="utf8"))
    except Exception:
        return []
    out = []
    for it in items:
        d = it.get("dir")
        if d and os.path.isfile(os.path.join(d, "layers_meta.json")):
            out.append(it)
        if len(out) >= RECENT_MAX:
            break
    return out


def save_recent(items):
    try:
        json.dump(items[:RECENT_MAX], open(RECENT_FILE, "w", encoding="utf8"),
                  ensure_ascii=False, indent=2)
    except Exception:
        pass      # 持久化失败不影响使用


# ---------------- 拆分进度锚点（日志文本 -> 百分比，单调不回退） ----------------
# 本地引擎与 ComfyUI 引擎的 verbose 消息共用一张表；未命中的消息保持上次值。
PROGRESS_ANCHORS = [
    ("加载 LayerDiff 模型", 3),
    ("加载 Marigold 深度模型", 8),
    ("分层推理开始", 10),
    ("身体组完成", 45),
    ("头部组完成", 60),
    ("深度推理开始", 65),
    ("深度完成", 80),
    ("后处理：拆分与聚类", 85),
    ("后处理完成", 88),
    ("已保存", 95),
    # ComfyUI 引擎
    ("ComfyUI 未运行", 2),
    ("ComfyUI 就绪", 5),
    ("已送入 ComfyUI", 7),
    ("已提交 prompt_id", 10),
    ("状态=success", 95),
    ("收集到", 98),
]


def progress_for_msg(msg):
    """返回该日志消息对应的百分比锚点，未知消息返回 None"""
    for key, pct in PROGRESS_ANCHORS:
        if key in msg:
            return pct
    return None


# ---------------- 后台拆分线程（照 fluent_worker 模式） ----------------
class DecomposeWorker(QThread):
    log_signal = Signal(str)
    done_signal = Signal(object, str)     # meta or None, out_dir

    def __init__(self, img_path, out_dir, engine="local"):
        super().__init__()
        self.img_path = img_path
        self.out_dir = out_dir
        self.engine = engine

    def run(self):
        try:
            meta = None
            if self.engine == "local":
                try:
                    import seethrough_local as SL
                    self.log_signal.emit("本地引擎（不依赖 ComfyUI）…")
                    meta = SL.decompose(self.img_path, self.out_dir,
                                         verbose=self.log_signal.emit)
                except Exception as e:
                    self.log_signal.emit("本地引擎失败(%s)，回退 ComfyUI" % e)
                if meta is None:
                    self.log_signal.emit("改用 ComfyUI 引擎…")
                    meta = decompose.decompose(self.img_path, self.out_dir,
                                               verbose=self.log_signal.emit)
            else:
                meta = decompose.decompose(self.img_path, self.out_dir,
                                            verbose=self.log_signal.emit)
            self.done_signal.emit(meta, self.out_dir)
        except Exception as e:
            self.log_signal.emit("异常: %s: %s" % (type(e).__name__, e))
            self.done_signal.emit(None, self.out_dir)


# ---------------- 工作台页 ----------------
class Workbench(QWidget):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.rig = None
        self.layers = []
        self.W = self.H = 0
        self.visible = {}
        self.history = History()
        self.worker = None
        self.busy = False
        self._build()
        # 启动即空工作台（不自动载入任何工程）
        self.cur_dir = None
        self.act_new()

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        # 三列宽度可拖：QSplitter 原生分隔条（1px 细线）
        split = QSplitter(Qt.Horizontal, self)
        split.setHandleWidth(1)
        split.setChildrenCollapsible(False)
        split.setStyleSheet(SPLIT_QSS)
        root.addWidget(split)

        # 左列：图层（菜单/导出按钮由 MainWindow 挪进标题栏）
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 4, 0)
        left.setSpacing(6)
        self.toolbar_widget = QWidget()
        tb_lay = QHBoxLayout(self.toolbar_widget)
        tb_lay.setContentsMargins(0, 0, 0, 0)
        tb_lay.setSpacing(6)
        menu = ToggleDropDownButton("菜单", self)
        m = RoundMenu(parent=self)
        m.addAction(Action(FluentIcon.HISTORY, "撤销  Ctrl+Z", triggered=self.act_undo))
        m.addAction(Action(FluentIcon.HISTORY, "重做  Ctrl+Y", triggered=self.act_redo))
        m.addSeparator()
        act_new = Action(FluentIcon.ADD, "新建", triggered=self.act_new)
        act_open = Action(FluentIcon.FOLDER, "打开工程", triggered=self.act_open_project)
        act_import = Action(FluentIcon.FOLDER_ADD, "导入 PSD", triggered=self.act_import)
        # 「最近打开」子菜单：hover 向右弹出（库内置 400ms 防抖）。
        # insertMenu(before, menu) 插在 before 之前，且要求 before 已在列表中，
        # 以 act_import 为锚点插入，最终顺序：新建 -> 打开工程 -> 最近打开 -> 导入 PSD。
        # RoundMenu.exec 不发 aboutToShow，故在按钮 menu_opened 时重建内容。
        self.recent_menu = RoundMenu("最近打开", parent=self)
        self.recent_menu.setIcon(FluentIcon.FOLDER)
        self._rebuild_recent_menu()
        m.addAction(act_new)
        m.addAction(act_open)
        m.addAction(act_import)
        m.insertMenu(act_import, self.recent_menu)
        m.addSeparator()
        m.addAction(Action(FluentIcon.SAVE, "保存", triggered=self.act_save))
        m.addAction(Action(FluentIcon.SAVE_AS, "另存为", triggered=self.act_saveas))
        menu.setMenu(m)
        self.menu_btn = menu
        self.menu_ref = m          # 防止被 GC
        menu.menu_opened.connect(lambda _m: self._rebuild_recent_menu())

        exp = ToggleDropDownButton("导出", self)
        em = RoundMenu(parent=self)
        em.addAction(Action(FluentIcon.FOLDER, "导出 PSD", triggered=self.act_export_psd))
        em.addAction(Action(FluentIcon.CODE, "导出 AE 脚本", triggered=self.act_export_ae))
        em.addAction(Action(FluentIcon.SYNC, "导出 JSON", triggered=self.act_export_json))
        em.addSeparator()
        em.addAction(Action(FluentIcon.PLAY, "导出 MP4", triggered=self._choose_mp4))
        em.addAction(Action(FluentIcon.IMAGE_EXPORT, "导出 GIF", triggered=self._choose_gif))
        exp.setMenu(em)
        self.exp_btn = exp
        self.exp_ref = em
        menu.setFixedHeight(30)
        exp.setFixedHeight(30)
        tb_lay.addWidget(menu)
        tb_lay.addWidget(exp)

        # 兜底：下拉菜单打开期间轮询光标，移出菜单矩形 250ms 即收起。
        # 状态全部由按钮自己记账（menu_opened / aboutToHide），不依赖 isVisible 时机。
        self._dropdowns = [(menu, m), (exp, em)]     # 看门狗管理所有下拉按钮
        self._menu_open = False
        menu.menu_opened.connect(self._menu_about_to_show)
        exp.menu_opened.connect(self._menu_about_to_show)
        self._menu_timer = QTimer(self)
        self._menu_timer.setInterval(120)
        self._menu_timer.timeout.connect(self._menu_watch)
        self._menu_out_since = None
        self._menu_cur = None
        m.aboutToHide.connect(lambda: self._menu_about_to_hide(menu))
        em.aboutToHide.connect(lambda: self._menu_about_to_hide(exp))
        left.addWidget(StrongBodyLabel("图层（顶=最前，拖动重排，双击显隐）"))
        self.layer_list = ListWidget()
        self.layer_list.setDragDropMode(ListWidget.InternalMove)
        self.layer_list.setDefaultDropAction(Qt.MoveAction)
        self.layer_list.itemDoubleClicked.connect(self._toggle_item)
        self.layer_list.model().rowsMoved.connect(self._on_rows_moved)
        left.addWidget(self.layer_list, 1)
        b = PushButton("显示/隐藏全部", self)
        b.clicked.connect(self._toggle_all)
        left.addWidget(b)
        lw = QWidget()
        lw.setLayout(left)
        lw.setMinimumWidth(140)
        split.addWidget(lw)

        # 中列：画布 + 播放
        mid = QVBoxLayout()
        mid.setSpacing(6)
        mid.setContentsMargins(4, 0, 4, 0)
        self.canvas = RigCanvas()
        self.canvas.pivot_dragged.connect(self._on_pivot_drag)
        self.canvas.pivot_released.connect(self._push_history)
        self.canvas.image_dropped.connect(self.start_decompose)
        mid.addWidget(self.canvas, 1)
        row = QHBoxLayout()
        self.btn_play = PushButton("播放", self)
        self.btn_play.clicked.connect(self._toggle_play)
        row.addWidget(self.btn_play)
        self.btn_decomp = PrimaryPushButton("拆分图片", self)
        self.btn_decomp.clicked.connect(self._pick_image)
        row.addWidget(self.btn_decomp)
        self.chk_local = CheckBox("本地引擎", self)
        self.chk_local.setChecked(True)
        self.chk_local.setToolTip("勾选=进程内直接跑 SeeThrough（模型在 models\\，无需 ComfyUI）\n"
                                  "取消=走 ComfyUI HTTP（本地引擎失败时也会自动回退）")
        row.addWidget(self.chk_local)
        self.slider = Slider(Qt.Horizontal, self)
        self.slider.setRange(0, 1000)
        self.slider.valueChanged.connect(self._on_scrub)
        row.addWidget(self.slider, 1)
        self.time_lbl = BodyLabel("0.00s")
        row.addWidget(self.time_lbl)
        mid.addLayout(row)
        self.prog = CaptionLabel("")
        mid.addWidget(self.prog)
        mw = QWidget()
        mw.setLayout(mid)
        mw.setMinimumWidth(260)
        split.addWidget(mw)

        # 右列：节点表 + 导出
        right = QVBoxLayout()
        right.setSpacing(6)
        right.setContentsMargins(4, 0, 0, 0)
        right.addWidget(StrongBodyLabel("骨架节点"))
        self.tree = TreeWidget()
        self.tree.setHeaderLabels(["节点", "父", "支点", "层数"])
        self.tree.itemDoubleClicked.connect(self._edit_pivot)
        right.addWidget(self.tree, 1)
        # 三个导出已并入顶部「导出」下拉菜单，这里只留单帧保存
        b = PushButton("保存当前帧", self)
        b.clicked.connect(self._save_frame)
        right.addWidget(b)
        rw = QWidget()
        rw.setLayout(right)
        rw.setMinimumWidth(180)
        split.addWidget(rw)
        # 初始宽度比例（左:中:右 = 1:3:1.1），仍可拖拽
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 1)
        split.setSizes([230, 640, 250])

        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self.t = 0.0
        self.playing = False

    def _menu_about_to_show(self, menu):
        self._menu_open = True
        self._menu_cur = menu
        self._menu_out_since = None
        self._menu_timer.start()

    def _menu_about_to_hide(self, btn):
        btn.note_closed()
        # 只在没有其它菜单仍打开时才复位（互斥菜单切换时不误停）
        if not any(b._open for b, _ in self._dropdowns):
            self._menu_open = False
            self._menu_cur = None
            self._menu_timer.stop()

    def _cursor_pos(self):
        return QCursor.pos()      # 单独成方法便于测试注入

    def _menu_watch(self):
        """光标连续 250ms 不在当前打开菜单的「菜单∪按钮」矩形内 -> 收起。
        状态以按钮自记账 _open 为准；发现记账与可见性失步则自愈（不依赖 aboutToHide）。"""
        if not self._menu_open:
            return
        # 自愈：_open 但菜单其实不可见（点了菜单项/被别处关掉）
        for b, mn in self._dropdowns:
            if b._open and not mn.isVisible():
                b.note_closed()
        pair = next(((b, mn) for b, mn in self._dropdowns if b._open), None)
        if pair is None:
            self._menu_open = False
            self._menu_cur = None
            self._menu_timer.stop()
            return
        btn, mnu = pair
        cur = self._cursor_pos()
        mrect = QRect(mnu.mapToGlobal(QPoint(0, 0)), mnu.size())
        brect = QRect(btn.mapToGlobal(QPoint(0, 0)), btn.size())
        if mrect.contains(cur) or brect.contains(cur):
            self._menu_out_since = None
            return
        if mrect.united(brect).adjusted(-6, -6, 6, 6).contains(cur):
            self._menu_out_since = None
            return
        now = time.monotonic()
        if self._menu_out_since is None:
            self._menu_out_since = now
        elif now - self._menu_out_since > 0.25:
            mnu.close()
            btn.note_closed()        # 不指望 aboutToHide，直接记账

    # ---------- 数据 ----------
    def load_dir(self, d, push=True):
        m = os.path.join(d, "layers_meta.json")
        if not os.path.exists(m):
            InfoBar.error("找不到元数据", "目录里没有 layers_meta.json：%s" % d,
                          parent=self, position=InfoBarPosition.TOP, duration=4000)
            return False
        try:
            self.layers, self.W, self.H = C.load_layers(m, d)
        except Exception as e:
            InfoBar.error("载入失败", zh_err(e), parent=self,
                          position=InfoBarPosition.TOP, duration=5000)
            return False
        self.rig = C.build_rig(self.layers, self.W, self.H)
        pos = {nm: k for k, nm in enumerate(self.rig["draw_order"])}
        self.layers.sort(key=lambda l: -pos.get(l["name"], 0))
        self.visible = {l["name"]: True for l in self.layers}
        self.cur_dir = d
        self._record_recent(d)
        self.reload_widgets()
        self.history.undo_stack.clear()
        self.history.redo_stack.clear()
        return True

    def reload_widgets(self):
        self.layer_list.clear()
        for l in self.layers:
            mark = "●" if self.visible.get(l["name"], True) else "○"
            it = QListWidgetItem("%s %s [%s]" % (mark, l["name"], l["tag"]))
            it.setData(Qt.UserRole, l["name"])
            self.layer_list.addItem(it)
        self.tree.clear()
        if self.rig:
            for n in self.rig["nodes"]:
                it = QTreeWidgetItem()
                it.setText(0, n["node"])
                it.setText(1, n["parent"])
                it.setText(2, "%.0f,%.0f" % tuple(n["pivot"]))
                it.setText(3, str(len(n["layers"])))
                it.setData(0, Qt.UserRole, n["node"])
                self.tree.addTopLevelItem(it)
        self.redraw()

    def redraw(self):
        if self.rig is None:
            self.canvas.set_frame(np.zeros((8, 8, 3), np.uint8), {},
                                  "拖入图片开始\n（或点「拆分图片」）")
            return
        hidden = {l["name"] for l in self.layers if not self.visible.get(l["name"], True)}
        # 播放走半分辨率 LOD：全分辨率单帧 ~690ms，LOD 后 ~20-30ms，30fps 才跑得动
        img = C.render_frame(self.rig, self.t, hidden=hidden, scale=PREVIEW_SCALE)
        sc = PREVIEW_SCALE
        piv = {n["node"]: (n["pivot"][0] * sc, n["pivot"][1] * sc)
               for n in self.rig["nodes"] if n["node"] != "root"}
        self.canvas.set_frame(img, piv)

    # ---------- 交互 ----------
    def _on_pivot_drag(self, node, x, y):
        n = next((m for m in self.rig["nodes"] if m["node"] == node), None)
        if n:
            # 画布坐标是 LOD 像素，换算回全分辨率
            n["pivot"] = [x / PREVIEW_SCALE, y / PREVIEW_SCALE]
            self.redraw()

    def _push_history(self):
        snap = self.history.snapshot(self)
        if snap:
            self.history.push(self)

    def _toggle_item(self, item):
        nm = item.data(Qt.UserRole)
        self.visible[nm] = not self.visible.get(nm, True)
        self.reload_widgets()

    def _on_rows_moved(self, *a):
        # InternalMove 完成后按视觉顺序重建 layers/draw_order
        names = []
        for i in range(self.layer_list.count()):
            it = self.layer_list.item(i)
            if it is not None:
                names.append(it.data(Qt.UserRole))
        by = {l["name"]: l for l in self.layers}
        if len(names) == len(self.layers) and all(n in by for n in names):
            self.layers = [by[n] for n in names]
            self.rig["draw_order"] = list(reversed(names))
            self.history.push(self)
            self.redraw()

    def _toggle_all(self):
        vals = [self.visible.get(k, True) for k in self.visible]
        target = not all(vals) if vals else True
        for k in self.visible:
            self.visible[k] = target
        self.reload_widgets()

    def _toggle_play(self):
        self.playing = not self.playing
        self.btn_play.setText("暂停" if self.playing else "播放")
        if self.playing:
            self.timer.start()
        else:
            self.timer.stop()

    def _tick(self):
        self.t = (self.t + 0.033) % 10.0
        self.slider.blockSignals(True)
        self.slider.setValue(int(self.t * 100))
        self.slider.blockSignals(False)
        self.time_lbl.setText("%.2fs" % self.t)
        self.redraw()

    def _on_scrub(self, v):
        if self.playing:
            return
        self.t = v / 100.0
        self.time_lbl.setText("%.2fs" % self.t)
        self.redraw()

    def _edit_pivot(self, item, col):
        if col != 2 or self.rig is None:
            return
        node = item.data(Qt.UserRole)
        n = next((m for m in self.rig["nodes"] if m["node"] == node), None)
        if not n:
            return
        x, y = n["pivot"]
        n["pivot"] = [(x + 10) % max(1, self.W), y]
        self.history.push(self)
        self.reload_widgets()

    def _save_frame(self):
        if self.rig is None:
            return
        img = C.render_frame(self.rig, self.t)
        p = os.path.join(getattr(self, "cur_dir", V2), "preview_t%.2f.png" % self.t)
        Image.fromarray(img).save(p)
        InfoBar.success("已保存", p, parent=self,
                        position=InfoBarPosition.TOP, duration=2500)

    # ---------- 菜单动作 ----------
    def act_undo(self):
        if not self.history.undo(self):
            InfoBar.info("撤销", "没有更多可撤销的操作", parent=self,
                         position=InfoBarPosition.TOP, duration=1800)

    def act_redo(self):
        if not self.history.redo(self):
            InfoBar.info("重做", "没有可重做的操作", parent=self,
                         position=InfoBarPosition.TOP, duration=1800)

    def _record_recent(self, d):
        """把工程目录写入最近列表（去重、置顶、上限 5）。无效目录不记。"""
        if not d or not os.path.isfile(os.path.join(d, "layers_meta.json")):
            return
        d = os.path.normpath(d)
        name = os.path.basename(d)
        n = 0
        try:
            meta = json.load(open(os.path.join(d, "layers_meta.json"), encoding="utf8"))
            n = len(meta.get("layers", []))
        except Exception:
            pass
        items = [it for it in load_recent() if os.path.normpath(it["dir"]) != d]
        items.insert(0, {"dir": d, "name": name, "layers": n})
        save_recent(items[:RECENT_MAX])

    def _rebuild_recent_menu(self):
        """按持久化列表重建「最近打开」子菜单（每次菜单打开时懒刷新）"""
        menu = getattr(self, "recent_menu", None)
        if menu is None:
            return
        menu.clear()
        items = load_recent()
        if not items:
            act = Action("暂无最近工程")
            act.setEnabled(False)
            menu.addAction(act)
            return
        for it in items[:RECENT_MAX]:
            label = it.get("name") or it["dir"]
            if it.get("layers"):
                label = "%s  (%d 层)" % (label, it["layers"])
            act = Action(FluentIcon.FOLDER, label, triggered=self._make_open_recent(it["dir"]))
            act.setToolTip(it["dir"])
            menu.addAction(act)

    def _make_open_recent(self, d):
        """闭包捕获目录，避免 lambda 延迟绑定错乱"""
        def open_():
            if not os.path.isfile(os.path.join(d, "layers_meta.json")):
                InfoBar.warning("工程已失效", "目录或 layers_meta.json 不存在：\n%s" % d,
                                parent=self, position=InfoBarPosition.TOP, duration=4000)
                return
            if self.load_dir(d):
                InfoBar.success("已打开", os.path.basename(d), parent=self,
                                position=InfoBarPosition.TOP, duration=2000)
        return open_

    def act_open_project(self):
        """打开工程目录（含 layers_meta.json 的文件夹，如 示例工程\）"""
        start = getattr(self, "cur_dir", None) or V2
        d = QFileDialog.getExistingDirectory(self, "打开工程（选择含 layers_meta.json 的文件夹）", start)
        if not d:
            return
        if not os.path.isfile(os.path.join(d, "layers_meta.json")):
            InfoBar.error("不是工程目录", "所选文件夹里没有 layers_meta.json：\n%s" % d,
                          parent=self, position=InfoBarPosition.TOP, duration=5000)
            return
        if self.load_dir(d):
            InfoBar.success("已打开工程", os.path.basename(d), parent=self,
                            position=InfoBarPosition.TOP, duration=2000)

    def act_new(self):
        self.rig = None
        self.layers = []
        self.visible = {}
        self.cur_dir = None
        self.reload_widgets()
        self.prog.setText("空白工作台：拖入图片开始")

    def act_import(self):
        p, _ = QFileDialog.getOpenFileName(self, "导入 PSD", "",
                                           "Photoshop 文件 (*.psd *.psb)")
        if p:
            self.import_psd(p)

    def import_psd(self, psd_path):
        try:
            from psd_tools import PSDImage
            psd = PSDImage.open(psd_path)
            W, H = psd.width, psd.height
            out = os.path.join(tempfile.gettempdir(), "l2d_psd_import")
            # 清掉上次导入的残留，避免旧层文件混入
            if os.path.isdir(out):
                shutil.rmtree(out, ignore_errors=True)
            os.makedirs(out, exist_ok=True)
            entries = []
            seen_fn = set()

            def walk(layers):
                for layer in layers:
                    if not layer.visible:
                        continue
                    if layer.is_group():
                        walk(layer)          # 递归进组（SeeThrough 导出的 PSD 有 Head/Mouth 组）
                        continue
                    im = layer.composite()
                    if im is None:
                        continue
                    im = im.convert("RGBA")
                    # tight 保存：composite() 就是图层自身尺寸，别贴全画布
                    #（贴全画布会让每层 bbox=整幅，渲染优化全部失效 -> 播放卡）
                    lx, ty, rx, by = layer.left, layer.top, layer.right, layer.bottom
                    cl, ct = max(0, lx), max(0, ty)
                    cr, cb = min(W, rx), min(H, by)
                    if cr <= cl or cb <= ct:
                        continue
                    if (cl, ct, cr, cb) != (lx, ty, rx, by):
                        im = im.crop((cl - lx, ct - ty, cr - lx, cb - ty))
                    raw = layer.name
                    name = raw.lstrip("+").strip()   # CA 的 + 前缀不进文件名
                    fn = "layer_%s.png" % name.replace(" ", "_")
                    k = 2
                    while fn in seen_fn:
                        fn = "layer_%s_%d.png" % (name.replace(" ", "_"), k)
                        k += 1
                    seen_fn.add(fn)
                    # 空层跳过：SeeThrough 会把角色没戴的配饰也输出成只有
                    # 几十个弱噪点的空层，留着会拖慢渲染还会让支点检测踩空数组
                    arr = np.asarray(im)
                    if int((arr[:, :, 3] > 16).sum()) < 64:
                        continue
                    im.save(os.path.join(out, fn))
                    # 叠放顺序按语义表：原始名先过 CA_TAG 归一化再查 TAG_DEPTH，
                    # 配饰类（handwear/eyewear/wings...）走 RAW_DEPTH，不信任 PSD 图层序
                    depth = C.depth_for_name(raw)
                    entries.append({"name": name, "filename": fn,
                                    "left": cl, "top": ct, "right": cr, "bottom": cb,
                                    "depth_median": depth})
            walk(psd)
            if not entries:
                raise ValueError("PSD 中没有含有效像素的图层（可能全是空层或不可见层）")
            json.dump({"width": W, "height": H, "layers": entries},
                      open(os.path.join(out, "layers_meta.json"), "w", encoding="utf8"),
                      ensure_ascii=False)
            if self.load_dir(out):
                InfoBar.success("导入成功", "%d 层" % len(entries), parent=self,
                                position=InfoBarPosition.TOP, duration=2500)
        except Exception as e:
            InfoBar.error("导入失败", zh_err(e), parent=self,
                          position=InfoBarPosition.TOP, duration=6000)

    def _project_payload(self):
        return {"width": self.W, "height": self.H,
                "draw_order": self.rig["draw_order"],
                "nodes": [{"node": n["node"], "parent": n["parent"],
                           "pivot": n["pivot"], "layers": n["layers"]}
                          for n in self.rig["nodes"]]}

    def act_save(self):
        if self.rig is None or not getattr(self, "cur_dir", None):
            self.act_saveas()
            return
        self._write_project(self.cur_dir)

    def act_saveas(self):
        d = QFileDialog.getExistingDirectory(self, "选择工程目录", V2)
        if d:
            self._write_project(d)

    def _write_project(self, d):
        try:
            json.dump(self._project_payload(),
                      open(os.path.join(d, "rig_project.json"), "w", encoding="utf8"),
                      ensure_ascii=False, indent=2)
            for l in self.layers:
                src = l["path"]
                dst = os.path.join(d, os.path.basename(src))
                if os.path.abspath(src) != os.path.abspath(dst):
                    shutil.copy2(src, dst)
            InfoBar.success("已保存", os.path.join(d, "rig_project.json"),
                            parent=self, position=InfoBarPosition.TOP, duration=2500)
        except Exception as e:
            InfoBar.error("保存失败", str(e)[:200], parent=self,
                          position=InfoBarPosition.TOP, duration=4000)

    # ---------- 导出 ----------
    def _need_rig(self):
        if self.rig is None:
            InfoBar.warning("提示", "工作台是空的", parent=self,
                            position=InfoBarPosition.TOP, duration=1800)
            return False
        return True

    def act_export_psd(self):
        if self._need_rig():
            C.export_psd(self.rig, os.path.join(self.cur_dir or V2, "rig_character.psd"))
            InfoBar.success("完成", "rig_character.psd", parent=self,
                            position=InfoBarPosition.TOP, duration=2000)

    def act_export_ae(self):
        if self._need_rig():
            C.export_ae_jsx(self.rig, os.path.join(self.cur_dir or V2, "rig_build.jsx"))
            InfoBar.success("完成", "rig_build.jsx", parent=self,
                            position=InfoBarPosition.TOP, duration=2000)

    def act_export_json(self):
        if self._need_rig():
            C.export_json(self.rig, os.path.join(self.cur_dir or V2, "rig_spec.json"))
            InfoBar.success("完成", "rig_spec.json", parent=self,
                            position=InfoBarPosition.TOP, duration=2000)

    # ---------- 视频/GIF 导出（后台线程） ----------
    def _choose_mp4(self):
        self._choose_video("mp4")

    def _choose_gif(self):
        self._choose_video("gif")

    def _choose_video(self, kind):
        if not self._need_rig():
            return
        if getattr(self, "export_worker", None) is not None and self.export_worker.isRunning():
            InfoBar.info("导出中", "请等待当前导出完成", parent=self,
                         position=InfoBarPosition.TOP, duration=1800)
            return
        default = os.path.join(self.cur_dir or V2,
                               "rig_anim." + ("mp4" if kind == "mp4" else "gif"))
        flt = "MP4 视频 (*.mp4)" if kind == "mp4" else "GIF 动图 (*.gif)"
        p, _ = QFileDialog.getSaveFileName(self, "导出 " + kind.upper(), default, flt)
        if not p:
            return
        if not p.lower().endswith("." + kind):
            p += "." + kind
        hidden = {l["name"] for l in self.layers if not self.visible.get(l["name"], True)}
        self.export_worker = ExportWorker(self.rig, hidden, kind, p)
        self.export_worker.progress.connect(self._export_progress)
        self.export_worker.done.connect(self._export_done)
        self.prog.setText("导出 %s：0%%" % kind.upper())
        self.export_worker.start()

    def _export_progress(self, i, n):
        self.prog.setText("导出中 %d/%d (%.0f%%)" % (i, n, 100.0 * i / n))

    def _export_done(self, ok, msg):
        if ok:
            self.prog.setText("导出完成")
            InfoBar.success("导出完成", msg, parent=self,
                            position=InfoBarPosition.TOP, duration=3500)
        else:
            self.prog.setText("导出失败")
            InfoBar.error("导出失败", msg[:200], parent=self,
                          position=InfoBarPosition.TOP, duration=5000)

    # ---------- 拆分 ----------
    def _pick_image(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择图片", "",
                                           "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)")
        if p:
            self.start_decompose(p)

    def start_decompose(self, img_path):
        if self.busy:
            InfoBar.info("正在拆分", "请等待当前任务", parent=self,
                         position=InfoBarPosition.TOP, duration=1800)
            return
        if not os.path.exists(img_path):
            InfoBar.error("文件不存在", img_path, parent=self,
                          position=InfoBarPosition.TOP, duration=3000)
            return
        self.busy = True
        self.btn_decomp.setEnabled(False)
        self.prog.setText("准备中…")
        self._nav_pct = 0
        self.set_nav_progress(0)
        out = os.path.join(OUT_BASE, os.path.splitext(os.path.basename(img_path))[0])
        engine = "local" if self.chk_local.isChecked() else "comfy"
        self.worker = DecomposeWorker(img_path, out, engine=engine)
        self.worker.log_signal.connect(self._on_decompose_log)
        self.worker.done_signal.connect(self._decompose_done)
        self.worker.start()

    def _on_decompose_log(self, msg):
        msg = str(msg)
        self.prog.setText(msg[-120:])
        pct = progress_for_msg(msg)
        if pct is not None:
            self.set_nav_progress(pct)   # 单调不回退在 set_nav_progress 里保证

    def set_nav_progress(self, pct):
        """导航栏「工作台」按钮显示/清除拆分百分比。pct=None 恢复原文字。"""
        setter = getattr(self, "nav_setter", None)
        if setter is None:
            return
        if pct is None:
            self._nav_pct = None
            setter("工作台", "")
            return
        cur = getattr(self, "_nav_pct", None)
        if cur is not None and pct < cur:
            pct = cur                      # 防倒退
        self._nav_pct = pct
        setter("工作台 %d%%" % pct, "拆分中 %d%%" % pct)

    def _decompose_done(self, meta, out_dir):
        self.busy = False
        self.btn_decomp.setEnabled(True)
        self.set_nav_progress(None)      # 无论成败都清掉导航栏百分比
        if meta:
            self.prog.setText("完成，已载入骨架")
            self.load_dir(out_dir)
            InfoBar.success("拆分完成", out_dir, parent=self,
                            position=InfoBarPosition.TOP, duration=3000)
        else:
            self.prog.setText("拆分失败")
            InfoBar.error("拆分失败", "SeeThrough 未返回结果", parent=self,
                          position=InfoBarPosition.TOP, duration=4000)


# ---------------- 后台导出线程（MP4/GIF 渲染要几十秒，绝不阻塞 UI） ----------------
class ExportWorker(QThread):
    progress = Signal(int, int)          # 当前帧, 总帧
    done = Signal(bool, str)             # 成功?, 消息/路径

    def __init__(self, rig, hidden, kind, path, seconds=10.0, fps=30, gif_scale=0.5):
        super().__init__()
        self.rig, self.hidden, self.kind, self.path = rig, hidden, kind, path
        self.seconds, self.fps, self.gif_scale = seconds, fps, gif_scale

    def run(self):
        import cv2
        try:
            n = max(2, int(self.seconds * self.fps))
            W, H = self.rig["width"], self.rig["height"]
            if self.kind == "mp4":
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                vw = cv2.VideoWriter(self.path, fourcc, self.fps, (W, H))
                if not vw.isOpened():
                    return self._ffmpeg_fallback(n, W, H)
                for i in range(n):
                    img = C.render_frame(self.rig, i / self.fps, hidden=self.hidden)
                    vw.write(cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
                    self.progress.emit(i + 1, n)
                vw.release()
                self.done.emit(True, self.path)
            else:
                # GIF：LOD 直出（不再全分辨率渲染后缩放），fps 降到 15
                gf = min(15, self.fps)
                gn = max(2, int(self.seconds * gf))
                sc = self.gif_scale
                rgb = [C.render_frame(self.rig, i / gf, hidden=self.hidden, scale=sc)
                       for i in range(gn)]
                self.progress.emit(1, gn)
                # 全局调色板：只量化首帧，其余帧套同一 palette（FASTOCTREE 比逐帧 ADAPTIVE 快数倍）
                base = Image.fromarray(rgb[0]).quantize(colors=256, method=Image.Quantize.FASTOCTREE)
                frames = [base]
                for j, arr in enumerate(rgb[1:], 2):
                    frames.append(Image.fromarray(arr).quantize(palette=base, dither=Image.Dither.NONE))
                    self.progress.emit(j, gn)
                frames[0].save(self.path, save_all=True, append_images=frames[1:],
                               duration=int(1000 / gf), loop=0, optimize=True)
                self.done.emit(True, self.path)
        except Exception as e:
            self.done.emit(False, "%s: %s" % (type(e).__name__, e))

    def _ffmpeg_fallback(self, n, W, H):
        """cv2 打不开 mp4v 时：PNG 序列 + ffmpeg.exe 合成（自带优先，回退共享位置）"""
        import subprocess, tempfile
        ff = next((c for c in FFMPEG_CANDIDATES if os.path.exists(c)), None)
        if ff is None:
            self.done.emit(False, "cv2 无法写 MP4 且未找到 ffmpeg.exe（可放一份到应用目录）")
            return
        d = tempfile.mkdtemp(prefix="l2d_frames_")
        try:
            for i in range(n):
                img = C.render_frame(self.rig, i / self.fps, hidden=self.hidden)
                Image.fromarray(img).save(os.path.join(d, "f%04d.png" % i))
                self.progress.emit(i + 1, n)
            subprocess.run([ff, "-y", "-framerate", str(self.fps),
                            "-i", os.path.join(d, "f%04d.png"),
                            "-pix_fmt", "yuv420p", self.path],
                           check=True, capture_output=True)
            self.done.emit(True, self.path)
        except Exception as e:
            self.done.emit(False, "ffmpeg 合成失败: %s" % e)
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ---------------- 主窗 ----------------
# 1px 深色细线分隔条（默认 6px 白条太抢眼）
SPLIT_QSS = ("QSplitter::handle { background: #3a3f4a; }"
             "QSplitter::handle:hover { background: #4d88ff; }")


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("木偶工作室 V2.0")
        self.resize(1280, 780)
        from PySide6.QtGui import QIcon
        if os.path.exists(ICON_ICO):
            self.setWindowIcon(QIcon(ICON_ICO))
        elif os.path.exists(ICON_PNG):
            self.setWindowIcon(QIcon(ICON_PNG))
        self.wb = Workbench(self)
        self.wb.setObjectName("workbench")
        self.addSubInterface(self.wb, FluentIcon.BRUSH, "工作台")
        self.navigationInterface.setMenuButtonVisible(False)
        # 拆分进度注入导航栏「工作台」按钮（addSubInterface 不返回导航项，
        # 实测用 widget(objectName) 取；setText/setToolTip 往返验证可用）
        self.nav_item = self.navigationInterface.widget("workbench")
        if self.nav_item is not None:
            def _set_nav(text, tip):
                self.nav_item.setText(text)
                self.nav_item.setToolTip(tip)
            self.wb.nav_setter = _set_nav
        # 导航栏也作为可拖模块：把 [导航栏 | 内容区] 装进外层 splitter。
        # 注意：NavigationInterface 的 sizeHint().width() 是 -1（无效），
        # 直接进 splitter 会被当成自然宽 0、把剩余空间全塞给它（实测停在 322px），
        # 所以包一层容器 widget 并给 Expanding 策略。
        self.hBoxLayout.removeWidget(self.navigationInterface)
        self.widgetLayout.removeWidget(self.stackedWidget)
        nav_box = QWidget()
        nb = QHBoxLayout(nav_box)
        nb.setContentsMargins(0, 0, 0, 0)
        nb.setSpacing(0)
        nb.addWidget(self.navigationInterface)
        nav_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        nav_box.setMinimumWidth(48)
        outer = QSplitter(Qt.Horizontal, self)
        outer.setHandleWidth(1)
        outer.setChildrenCollapsible(False)
        outer.setStyleSheet(SPLIT_QSS)
        outer.addWidget(nav_box)
        outer.addWidget(self.stackedWidget)
        outer.setStretchFactor(0, 0)
        outer.setStretchFactor(1, 1)
        outer.setSizes([48, 1232])
        self.widgetLayout.addWidget(outer)
        self.outer_split = outer
        self.nav_box = nav_box
        # ---- 标题栏左上角工具条（DSH 风格）：折叠按钮 + 菜单 + 导出 ----
        self.btn_collapse = TransparentToggleToolButton(FluentIcon.LAYOUT, self)
        self.btn_collapse.setFixedSize(32, 30)
        self.btn_collapse.setToolTip("显示/隐藏导航栏")
        self.btn_collapse.clicked.connect(self._toggle_nav)
        tbar = self.titleBar.hBoxLayout
        tbar.insertWidget(0, self.btn_collapse, 0, Qt.AlignLeft | Qt.AlignVCenter)
        tbar.insertWidget(1, self.wb.menu_btn, 0, Qt.AlignLeft | Qt.AlignVCenter)
        tbar.insertWidget(2, self.wb.exp_btn, 0, Qt.AlignLeft | Qt.AlignVCenter)
        # 快捷键
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.wb.act_undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.wb.act_redo)

    def _toggle_nav(self):
        """折叠/展开最左导航栏（splitter 首面板隐藏后句柄一并消失，空间自动让给内容）"""
        vis = not self.nav_box.isVisible()
        self.nav_box.setVisible(vis)


def run_app():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    setTheme(Theme.DARK)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run_app()
