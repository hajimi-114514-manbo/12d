# -*- coding: utf-8 -*-
"""伪L2D 骨架工作室 V2.0 —— PySide6 + qfluentwidgets（照 Vocal2Midi 风格）

功能对齐 V1.0。标题栏左上角两个下拉按钮：「文件」（新建/打开/导入/拆分/保存/另存为/导出 PSD·AE·JSON·MP4·GIF）与「编辑」（撤销/重做）。
SeeThrough 拆分走 QThread，绝不阻塞 UI。
引擎接口全部复用 l2d_rig_core / decompose（v2 副本）。
"""
import os
import sys
import copy
import json
import re
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
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QKeySequence, QShortcut, QIcon, QCursor, QPen, QBrush

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

    def _drawDropDownIcon(self, painter, rect):
        """标题栏按钮去掉右侧下拉小箭头（用户要求，纯文字更干净）。
        基类 DropDownButtonBase.paintEvent 会调这个方法画箭头；这里空实现即可。
        注意：不影响 RoundMenu 菜单项自己的箭头。"""
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import l2d_rig_core as C

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
    """快照式历史。存图层**对象引用**（删除操作可恢复）+ 顺序 + 支点。
    不变式：undo_stack 栈顶 == 当前状态；载入时播一个基线快照。
    undo = 把当前弹给 redo、应用新栈顶（即上一状态）。"""

    def __init__(self, limit=60):
        self.limit = limit
        self.undo_stack = []
        self.redo_stack = []

    def snapshot(self, studio):
        if studio.rig is None:
            return None
        return {
            "layers": list(studio.layers),                       # 对象引用
            # bbox 值记录：移动工具是**原地改 bbox**，对象引用快照无法回退
            # （undo 恢复的是同一对象，bbox 已被改掉）。按 name 键存值。
            "bboxes": {l["name"]: tuple(l["bbox"]) for l in studio.layers},
            "draw_order": list(studio.rig.get("draw_order", [])),
            "pivots": {n["node"]: list(n["pivot"]) for n in studio.rig["nodes"]},
            "node_layers": {n["node"]: list(n["layers"]) for n in studio.rig["nodes"]},
            "_layers": list(studio.rig.get("_layers", [])),
        }

    def seed(self, studio):
        """载入后播基线：undo_stack=[当前]"""
        self.undo_stack.clear()
        self.redo_stack.clear()
        snap = self.snapshot(studio)
        if snap is not None:
            self.undo_stack.append(snap)

    def push(self, studio):
        snap = self.snapshot(studio)
        if snap is None or not self.undo_stack:
            return
        prev = self.undo_stack[-1]
        if [id(x) for x in snap["layers"]] == [id(x) for x in prev["layers"]] and \
           snap["draw_order"] == prev["draw_order"] and \
           snap["pivots"] == prev["pivots"] and \
           snap.get("bboxes") == prev.get("bboxes"):
            return                               # 无变化不压栈
        self.undo_stack.append(snap)
        if len(self.undo_stack) > self.limit:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def apply(self, studio, snap):
        if snap is None or studio.rig is None:
            return
        studio.layers = list(snap["layers"])
        # 回退 bbox（移动工具的撤销靠这里，缺省则对象引用不变）
        bbs = snap.get("bboxes", {})
        for l in studio.layers:
            if l["name"] in bbs:
                l["bbox"] = tuple(bbs[l["name"]])
        studio.rig["draw_order"] = list(snap["draw_order"])
        studio.rig["_layers"] = list(snap["_layers"])
        for n in studio.rig["nodes"]:
            if n["node"] in snap["pivots"]:
                n["pivot"] = list(snap["pivots"][n["node"]])
            if n["node"] in snap["node_layers"]:
                n["layers"] = list(snap["node_layers"][n["node"]])
        studio.rig.pop("_lite", None)           # 结构变了，LOD 缓存作废
        studio.reload_widgets()

    def undo(self, studio):
        if len(self.undo_stack) < 2:
            return False
        self.redo_stack.append(self.undo_stack.pop())
        self.apply(studio, self.undo_stack[-1])
        return True

    def redo(self, studio):
        if not self.redo_stack:
            return False
        snap = self.redo_stack.pop()
        self.undo_stack.append(snap)
        self.apply(studio, snap)
        return True


# ---------------- 预览画布 ----------------
class RigCanvas(QWidget):
    pivot_dragged = Signal(str, float, float)      # node, x, y
    pivot_released = Signal()
    image_dropped = Signal(str)
    move_grabbed = Signal(str)                     # 移动工具：按下图层名
    move_dragged = Signal(str, float, float)       # 图层名, dx, dy（全分辨率像素）
    move_released = Signal()

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
        self.setFocusPolicy(Qt.StrongFocus)
        self._geom = (0, 0, 1.0, 0, 0)   # ox, oy, s, dw, dh
        # Photoshop 风格缩放：fit=适合窗口(Ctrl+0)，manual=手动倍率(Ctrl+1/±/滚轮)
        self.zoom_mode = "fit"
        self.zoom_factor = 1.0          # manual 模式的绝对缩放（1.0=实际像素）
        self.pan = [0.0, 0.0]           # manual 模式相对居中的平移
        self._space = False             # 空格按住 -> 拖拽平移
        self._panning = None            # 平移起点 (x,y,panx,pany)
        # 工具："pan"=默认（左键拖支点）；"move"=移动图层（左键按 bbox 命中选取并拖动）
        self.tool = "pan"
        self._layers_ref = []           # [(name, bbox_full, visible)] 列表前=叠放顶
        self._mv = None                 # 移动拖拽态 (name, 按下点全分辨率坐标)

    def set_tool(self, t):
        self.tool = t
        if t == "move":
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.unsetCursor()
        self.update()

    def set_layers(self, ref):
        """Workbench 每次 redraw 同步：[(name, bbox(全分辨率), visible)]，
        顺序=图层列表顺序（前=叠放最上），命中测试按此序取第一个。"""
        self._layers_ref = ref

    def _to_full(self, pos):
        """widget 坐标 -> 全分辨率图像坐标（画布显示的是 LOD 帧）"""
        ox, oy, s, dw, dh = self._geom
        sc = PREVIEW_SCALE
        ix = (pos.x() - ox) / s / sc
        iy = (pos.y() - oy) / s / sc
        return ix, iy

    def set_frame(self, img, pivots, hint=""):
        """img 为 HxWx4 **预乘** RGBA（render_frame_rgba premul=True 直出，
        省全画布除法）；Qt 按 ARGB32_Premultiplied 消费，透明处露棋盘格衬底"""
        h, w = img.shape[:2]
        arr = np.ascontiguousarray(img[:, :, [2, 1, 0, 3]])  # RGBA->BGRA（通道序不变）
        qimg = QImage(arr.data, w, h, 4 * w, QImage.Format.Format_ARGB32_Premultiplied)
        self.pixmap = QPixmap.fromImage(qimg.copy())
        self.img_w, self.img_h = w, h
        self.pivots = dict(pivots)
        self.hint = hint
        self.update()

    def _layout(self):
        cw, ch = self.width(), self.height()
        if not self.pixmap or cw < 4 or ch < 4:
            return
        if self.zoom_mode == "fit":
            s = min((cw - 20) / self.img_w, (ch - 20) / self.img_h)
            s = max(s, 0.02)
            dw, dh = int(self.img_w * s), int(self.img_h * s)
            ox, oy = (cw - dw) // 2, (ch - dh) // 2
        else:
            s = max(0.02, min(16.0, self.zoom_factor))
            dw, dh = int(self.img_w * s), int(self.img_h * s)
            # 默认居中，再叠加平移量
            ox = (cw - dw) // 2 + self.pan[0]
            oy = (ch - dh) // 2 + self.pan[1]
        self._geom = (ox, oy, s, dw, dh)

    def zoom_display(self):
        """当前显示倍率（相对实际像素），供 UI 角标"""
        if not self.pixmap:
            return 1.0
        self._layout()
        return self._geom[2]

    def set_fit(self):
        self.zoom_mode = "fit"
        self.pan = [0.0, 0.0]
        self.update()

    def set_actual(self):
        self.zoom_mode = "manual"
        self.zoom_factor = 1.0
        self.pan = [0.0, 0.0]
        self.update()

    def zoom_step(self, up, anchor=None):
        """逐级 ×2 / ÷2（PS 的 Ctrl+= / Ctrl+-）。anchor 为 widget 坐标。"""
        self._layout()
        old = self._geom[2]
        new = min(16.0, max(0.02, old * (2.0 if up else 0.5)))
        self._apply_zoom(new, old, anchor)

    def _apply_zoom(self, new_s, old_s, anchor):
        cw, ch = self.width(), self.height()
        if self.zoom_mode == "fit":
            # 从 fit 切入 manual：以调用方传入的当前 fit 倍率为基准，
            # 避免画面从"适合窗口"突跳到默认 100%
            self.zoom_mode = "manual"
            self.zoom_factor = old_s
            self.pan = [0.0, 0.0]
        if abs(new_s - old_s) < 1e-9:
            self.update()
            return
        if anchor is None:
            anchor = QPoint(cw // 2, ch // 2)
        # 以锚点为中心缩放：锚点在 widget 坐标下对应的图像坐标保持不变
        ox, oy, s, dw, dh = self._geom
        ix = (anchor.x() - ox) / s
        iy = (anchor.y() - oy) / s
        self.zoom_factor = new_s
        dw2 = self.img_w * new_s
        dh2 = self.img_h * new_s
        self.pan[0] = anchor.x() - ix * new_s - (cw - dw2) / 2.0
        self.pan[1] = anchor.y() - iy * new_s - (ch - dh2) / 2.0
        self.update()

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
        # 图像区域先铺 PS 风格棋盘格：透明背景处透出，表示"这里透明"。
        # 2x2 格平铺画刷一次 fillRect（逐格循环曾给每帧加 ~15ms）
        if not hasattr(self, "_checker_brush"):
            tile = QPixmap(32, 32)
            tp = QPainter(tile)
            c1, c2 = QColor("#3a3f4a"), QColor("#2c303a")
            tp.fillRect(0, 0, 16, 16, c1); tp.fillRect(16, 16, 16, 16, c1)
            tp.fillRect(16, 0, 16, 16, c2); tp.fillRect(0, 16, 16, 16, c2)
            tp.end()
            self._checker_brush = QBrush(tile)
        p.setClipRect(ox, oy, dw, dh)
        p.fillRect(ox, oy, dw, dh, self._checker_brush)
        # 缩小用 Fast（源是 2x LOD 降采样，双线性无视觉增益且省 ~8ms）；放大仍 Smooth
        xform = Qt.FastTransformation if s < 1.0 else Qt.SmoothTransformation
        p.drawPixmap(QPoint(ox, oy), self.pixmap.scaled(
            dw, dh, Qt.IgnoreAspectRatio, xform))
        p.setClipping(False)
        for node, (x, y) in self.pivots.items():
            cx, cy = ox + x * s, oy + y * s
            p.setPen(QColor("#ffd166"))
            p.setBrush(QColor("#ff5c5c"))
            p.drawEllipse(QPoint(int(cx), int(cy)), 7, 7)
            p.setPen(QColor("#ffd166"))
            p.drawText(int(cx) + 10, int(cy) - 8, node)
        # 左下角缩放角标（PS 风格）
        pct = int(round(s * 100))
        tag = ("适合窗口  %d%%" % pct) if self.zoom_mode == "fit" else ("%d%%" % pct)
        f = p.font(); f.setPointSize(9); p.setFont(f)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(tag) + 14
        rect = self.rect().adjusted(6, self.height() - 26, 6 + tw, self.height() - 8)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 22, 28, 190))
        p.drawRoundedRect(rect, 4, 4)
        p.setPen(QColor("#c8cfdb"))
        p.drawText(rect, Qt.AlignCenter, tag)
        p.end()

    def _node_at(self, pos):
        best, bestd = None, 14.0
        ox, oy, s, dw, dh = self._geom
        for node, (x, y) in self.pivots.items():
            d = ((ox + x * s) - pos.x()) ** 2 + ((oy + y * s) - pos.y()) ** 2
            if d < bestd ** 2:
                bestd, best = d ** 0.5, node
        return best

    def _layer_at(self, ix, iy):
        """移动工具命中测试：按图层列表顺序（前=最上）找第一个包含该点的可见层"""
        for name, (x0, y0, x1, y1), vis in self._layers_ref:
            if vis and x0 <= ix < x1 and y0 <= iy < y1:
                return name
        return None

    def mousePressEvent(self, e):
        if e.button() == Qt.MiddleButton or self._space:
            # 平移：中键 或 空格+左键（任何工具模式下都保留）
            self._panning = (e.position().x(), e.position().y(), self.pan[0], self.pan[1])
            self.setCursor(Qt.ClosedHandCursor if self._space else Qt.OpenHandCursor)
            return
        if e.button() == Qt.LeftButton:
            if self.tool == "move":
                ix, iy = self._to_full(e.position().toPoint())
                name = self._layer_at(ix, iy)
                if name:
                    self._mv = (name, ix, iy)
                    self.move_grabbed.emit(name)
                return
            n = self._node_at(e.position().toPoint())
            if n:
                self.drag_node = n

    def mouseMoveEvent(self, e):
        if self._panning:
            px, py, bpx, bpy = self._panning
            self.pan[0] = bpx + (e.position().x() - px)
            self.pan[1] = bpy + (e.position().y() - py)
            self.update()
            return
        if self._mv:
            name, sx, sy = self._mv
            ix, iy = self._to_full(e.position().toPoint())
            self.move_dragged.emit(name, ix - sx, iy - sy)
            return
        if self.drag_node:
            ox, oy, s, dw, dh = self._geom
            x = (e.position().x() - ox) / s
            y = (e.position().y() - oy) / s
            x = max(0.0, min(self.img_w - 1.0, x))
            y = max(0.0, min(self.img_h - 1.0, y))
            self.pivot_dragged.emit(self.drag_node, x, y)

    def mouseReleaseEvent(self, e):
        if self._panning:
            self._panning = None
            self.unsetCursor()
            if self.tool == "move":
                self.setCursor(Qt.SizeAllCursor)
            return
        if self._mv:
            self._mv = None
            self.move_released.emit()
            return
        if self.drag_node:
            self.drag_node = None
            self.pivot_released.emit()

    def wheelEvent(self, e):
        """Ctrl+滚轮 = 以光标为锚缩放（PS 习惯）；无 Ctrl 时不拦截（留给滚动区）"""
        if not (e.modifiers() & Qt.ControlModifier):
            e.ignore()
            return
        if not self.pixmap:
            e.ignore()
            return
        self._layout()
        old = self._geom[2]
        # 滚轮一格约 1.25 倍（比逐级细腻）
        steps = e.angleDelta().y() / 120.0
        new = min(16.0, max(0.02, old * (1.25 ** steps)))
        self._apply_zoom(new, old, e.position().toPoint())
        e.accept()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self._space = True
            self.setCursor(Qt.OpenHandCursor)
            return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self._space = False
            if not self._panning:
                self.unsetCursor()
            return
        super().keyReleaseEvent(e)

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
# 本地引擎 verbose 消息对照表；未命中的消息保持上次值。
PROGRESS_ANCHORS = [
    # --- 本地引擎（进程内 nf4）---
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
    # --- ComfyUI 路线（A 卡/ROCm）---
    ("已连接 ComfyUI", 5),
    ("上传原图", 8),
    ("提交工作流", 12),
    ("已提交 prompt_id", 15),
    ("状态=", 85),
    ("收集 ", 95),
]


def progress_for_msg(msg):
    """返回该日志消息对应的百分比锚点，未知消息返回 None"""
    for key, pct in PROGRESS_ANCHORS:
        if key in msg:
            return pct
    return None


_CUDA_CACHE = [None]


def _cuda_available():
    """惰性检测 CUDA 可用性（结果缓存；torch 只在首次调用时 import）"""
    if _CUDA_CACHE[0] is None:
        try:
            import torch
            _CUDA_CACHE[0] = bool(torch.cuda.is_available())
        except Exception:
            _CUDA_CACHE[0] = False
    return _CUDA_CACHE[0]


def find_meta(d):
    """工程元数据路径：优先 layers_meta.json；
    兼容在 ComfyUI 里手动跑工作流产出的 *_layers.json（A 卡备用路线，取最新一份）。"""
    m = os.path.join(d, "layers_meta.json")
    if os.path.isfile(m):
        return m
    try:
        cands = [os.path.join(d, f) for f in os.listdir(d)
                 if f.endswith("_layers.json")]
    except Exception:
        return None
    return max(cands, key=os.path.getmtime) if cands else None


# ---------------- 后台拆分线程（照 fluent_worker 模式） ----------------
def _engine_mode():
    """选拆分引擎：
       local = 进程内 SeeThrough（nf4，需 CUDA，N 卡默认，最快）
       comfy = 本机 ComfyUI + See-through 插件（bf16，A 卡/ROCm 或无 CUDA 时）
    环境变量 MUPPET_ENGINE=local|comfy 可强制（N 卡想跑全精度质量更高时用 comfy）。"""
    force = os.environ.get("MUPPET_ENGINE", "").strip().lower()
    if force in ("local", "comfy"):
        return force
    return "local" if _cuda_available() else "comfy"


class DecomposeWorker(QThread):
    log_signal = Signal(str)
    done_signal = Signal(object, str)     # meta or None, out_dir

    def __init__(self, img_path, out_dir):
        super().__init__()
        self.img_path = img_path
        self.out_dir = out_dir

    def run(self):
        mode = _engine_mode()
        try:
            if mode == "local":
                import seethrough_local as SL
                self.log_signal.emit("引擎：本地进程内 SeeThrough（nf4）…")
                meta = SL.decompose(self.img_path, self.out_dir,
                                    verbose=self.log_signal.emit)
            else:
                import comfy_decompose as CD
                self.log_signal.emit(
                    "引擎：ComfyUI + See-through 插件（bf16，A 卡/ROCm 路线）…")

                def prog(sec):
                    # 无中间进度可查，按耗时线性估：700s 视作正常完成点
                    pct = int(15 + min(sec, 700) / 700.0 * 65)
                    self.log_signal.emit(
                        "#pct=%d ComfyUI 推理中 %ds（A 卡首跑含模型下载会更久）" % (pct, sec))

                meta = CD.decompose(self.img_path, self.out_dir, quant="none",
                                    auto_download=True, prefix="muppet",
                                    verbose=self.log_signal.emit,
                                    on_progress=prog, timeout=3600)
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
        self._mv_orig = None          # 移动工具按下时的原始 bbox
        self._selected = None         # 当前选中图层名（列表/画布共用）
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

        # 菜单/导出按钮（临时父容器，MainWindow 会把它们插进标题栏）
        self.toolbar_widget = QWidget()
        tb_lay = QHBoxLayout(self.toolbar_widget)
        tb_lay.setContentsMargins(0, 0, 0, 0)
        tb_lay.setSpacing(6)
        menu = ToggleDropDownButton("文件", self)
        m = RoundMenu(parent=self)
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
        # 拆图入口（此前按钮删除后无入口；无 CUDA 时点击给 A 卡路线引导）
        m.addAction(Action(FluentIcon.ZOOM, "拆分图片（SeeThrough）",
                           triggered=self._pick_image))
        m.addSeparator()
        m.addAction(Action(FluentIcon.SAVE, "保存", triggered=self.act_save))
        m.addAction(Action(FluentIcon.SAVE_AS, "另存为", triggered=self.act_saveas))
        # 导出功能并入「文件」（原独立"导出"按钮已移除）
        m.addSeparator()
        m.addAction(Action(FluentIcon.FOLDER, "导出 PSD", triggered=self.act_export_psd))
        m.addAction(Action(FluentIcon.CODE, "导出 AE 脚本", triggered=self.act_export_ae))
        m.addAction(Action(FluentIcon.SYNC, "导出 JSON", triggered=self.act_export_json))
        m.addAction(Action(FluentIcon.PLAY, "导出 MP4", triggered=self._choose_mp4))
        m.addAction(Action(FluentIcon.IMAGE_EXPORT, "导出 GIF", triggered=self._choose_gif))
        menu.setMenu(m)
        self.menu_btn = menu
        self.menu_ref = m          # 防止被 GC
        menu.menu_opened.connect(lambda _m: self._rebuild_recent_menu())

        edit = ToggleDropDownButton("编辑", self)
        em = RoundMenu(parent=self)
        em.addAction(Action(FluentIcon.HISTORY, "撤销  Ctrl+Z", triggered=self.act_undo))
        em.addAction(Action(FluentIcon.HISTORY, "重做  Ctrl+Y", triggered=self.act_redo))
        edit.setMenu(em)
        self.edit_btn = edit
        self.edit_ref = em
        menu.setFixedHeight(30)
        edit.setFixedHeight(30)
        tb_lay.addWidget(menu)
        tb_lay.addWidget(edit)

        # 兜底：下拉菜单打开期间轮询光标，移出菜单矩形 250ms 即收起。
        # 状态全部由按钮自己记账（menu_opened / aboutToHide），不依赖 isVisible 时机。
        self._dropdowns = [(menu, m), (edit, em)]     # 看门狗管理所有下拉按钮
        self._menu_open = False
        menu.menu_opened.connect(self._menu_about_to_show)
        edit.menu_opened.connect(self._menu_about_to_show)
        self._menu_timer = QTimer(self)
        self._menu_timer.setInterval(120)
        self._menu_timer.timeout.connect(self._menu_watch)
        self._menu_out_since = None
        self._menu_cur = None
        m.aboutToHide.connect(lambda: self._menu_about_to_hide(menu))
        em.aboutToHide.connect(lambda: self._menu_about_to_hide(edit))
        # ===== 左列：画布（占整个左边，PS 风格）=====
        leftLay = QVBoxLayout()
        leftLay.setContentsMargins(0, 0, 4, 0)
        leftLay.setSpacing(6)
        self.canvas = RigCanvas()
        self.canvas.pivot_dragged.connect(self._on_pivot_drag)
        self.canvas.pivot_released.connect(self._push_history)
        self.canvas.image_dropped.connect(self._drop_add_layer)
        self.canvas.move_grabbed.connect(self._on_move_grab)
        self.canvas.move_dragged.connect(self._on_move_drag)
        self.canvas.move_released.connect(self._push_history)
        # PS 风格竖排工具栏：导航栏右侧、画布左侧。先内置移动工具一个按钮。
        self.tool_bar = QWidget()
        _tbl = QVBoxLayout(self.tool_bar)
        _tbl.setContentsMargins(4, 6, 4, 6)
        _tbl.setSpacing(4)
        self.btn_move = TransparentToggleToolButton(FluentIcon.MOVE, self)
        self.btn_move.setToolTip("移动工具：在画布上拖动图层（Ctrl+Z 撤销）。\n"
                                 "未选中时左键仍用于拖动骨架支点。")
        # 默认不选中：保持原有"左键拖支点"行为，点按钮才进入移动模式
        self.btn_move.toggled.connect(
            lambda on: self.canvas.set_tool("move" if on else "pan"))
        _tbl.addWidget(self.btn_move)
        _tbl.addStretch(1)
        self.tool_bar.setFixedWidth(44)
        self.tool_bar.setStyleSheet(
            "background:#252a33; border-radius:8px;")
        canvas_row = QHBoxLayout()
        canvas_row.setContentsMargins(0, 0, 0, 0)
        canvas_row.setSpacing(6)
        canvas_row.addWidget(self.tool_bar)
        canvas_row.addWidget(self.canvas, 1)
        leftLay.addLayout(canvas_row, 1)
        row = QHBoxLayout()
        self.btn_play = PushButton("播放", self)
        self.btn_play.clicked.connect(self._toggle_play)
        row.addWidget(self.btn_play)
        self.slider = Slider(Qt.Horizontal, self)
        self.slider.setRange(0, 1000)
        self.slider.valueChanged.connect(self._on_scrub)
        row.addWidget(self.slider, 1)
        self.time_lbl = BodyLabel("0.00s")
        row.addWidget(self.time_lbl)
        leftLay.addLayout(row)
        self.prog = CaptionLabel("")
        leftLay.addWidget(self.prog)
        lw = QWidget()
        lw.setLayout(leftLay)
        lw.setMinimumWidth(360)
        split.addWidget(lw)

        # ===== 右侧：垂直分割（上=骨架节点，下=图层）=====
        vsplit = QSplitter(Qt.Vertical, self)
        vsplit.setHandleWidth(1)
        vsplit.setChildrenCollapsible(False)
        vsplit.setStyleSheet(SPLIT_QSS)

        topLay = QVBoxLayout()
        topLay.setContentsMargins(4, 0, 0, 4)
        topLay.setSpacing(6)
        topLay.addWidget(StrongBodyLabel("骨架节点"))
        self.tree = TreeWidget()
        self.tree.setHeaderLabels(["节点", "父", "支点", "层数"])
        self.tree.itemDoubleClicked.connect(self._edit_pivot)
        topLay.addWidget(self.tree, 1)
        topWidget = QWidget()
        topWidget.setLayout(topLay)
        vsplit.addWidget(topWidget)

        botLay = QVBoxLayout()
        botLay.setContentsMargins(4, 4, 0, 0)
        botLay.setSpacing(6)
        botLay.addWidget(StrongBodyLabel("图层（顶=最前，拖动重排，双击显隐，右键菜单）"))
        self.layer_list = ListWidget()
        self.layer_list.setDragDropMode(ListWidget.InternalMove)
        self.layer_list.setDefaultDropAction(Qt.MoveAction)
        self.layer_list.itemDoubleClicked.connect(self._toggle_item)
        self.layer_list.model().rowsMoved.connect(self._on_rows_moved)
        self.layer_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.layer_list.customContextMenuRequested.connect(self._layer_menu)
        botLay.addWidget(self.layer_list, 1)
        ball = PushButton("显示/隐藏全部", self)
        ball.clicked.connect(self._toggle_all)
        botLay.addWidget(ball)
        botWidget = QWidget()
        botWidget.setLayout(botLay)
        vsplit.addWidget(botWidget)

        split.addWidget(vsplit)
        self.vsplit = vsplit
        # 左画布 : 右面板 = 3 : 2；右侧 骨架 : 图层 均分
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([820, 420])
        vsplit.setSizes([280, 320])

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
        m = find_meta(d)
        if not m:
            InfoBar.error("找不到元数据",
                          "目录里没有 layers_meta.json（或 ComfyUI 的 *_layers.json）：%s" % d,
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
        self.history.seed(self)      # 新不变式：undo_stack 栈底=载入基线
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
        if self.rig is None or not self.rig.get("_layers"):
            self.canvas.set_frame(np.zeros((8, 8, 4), np.uint8), {},
                                  "拖入图片开始")
            return
        hidden = {l["name"] for l in self.layers if not self.visible.get(l["name"], True)}
        # 移动工具命中测试数据：列表顺序=前为叠放顶，与视觉一致
        self.canvas.set_layers([(l["name"], l["bbox"],
                                 self.visible.get(l["name"], True))
                                for l in self.layers])
        # 播放走半分辨率 LOD + 预乘直出（省出口除法，~30ms/帧），透明区画布显示棋盘格
        img = C.render_frame_rgba(self.rig, self.t, hidden=hidden,
                                  scale=PREVIEW_SCALE, premul=True)
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

    # ---------- 移动工具 ----------
    def _on_move_grab(self, name):
        """按下命中图层：记原始 bbox（累计偏移基准，避免逐帧增量误差），并同步选中列表项"""
        self._mv_orig = None
        for l in self.layers:
            if l["name"] == name:
                self._mv_orig = tuple(l["bbox"])
                break
        self._select_in_list(name)

    def _on_move_drag(self, name, dx, dy):
        if self._mv_orig is None:
            return
        x0, y0, x1, y1 = self._mv_orig
        nx0, ny0 = int(round(x0 + dx)), int(round(y0 + dy))
        for l in self.layers:
            if l["name"] == name:
                # 必须 int：渲染切片 img[y0:y1] 只吃整数 bbox（load_layers 契约）
                l["bbox"] = (nx0, ny0, nx0 + int(x1 - x0), ny0 + int(y1 - y0))
                break
        self.redraw()

    def _select_in_list(self, name):
        self._selected = name
        for i in range(self.layer_list.count()):
            it = self.layer_list.item(i)
            if it is not None and it.data(Qt.UserRole) == name:
                self.layer_list.setCurrentItem(it)
                return

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

    # ---------- 图层右键菜单（置顶 / 拆分 / 删除） ----------
    def _layer_menu(self, pos):
        if self.rig is None:
            return
        it = self.layer_list.itemAt(pos)
        if it is None:
            return
        name = it.data(Qt.UserRole)
        menu = RoundMenu(parent=self.layer_list)
        menu.addAction(Action(FluentIcon.UP, "置顶",
                              triggered=lambda: self._layer_to_top(name)))
        # 拆分此层：两级子菜单（A 连通域 / B SAM）
        sub = RoundMenu("拆分此层", parent=self)
        sub.addAction(Action(FluentIcon.CUT, "基础拆分（连通域）",
                             triggered=lambda: self._layer_subsplit(name, "basic")))
        sub.addAction(Action(FluentIcon.SEARCH, "智能拆分（SAM）",
                             triggered=lambda: self._layer_subsplit(name, "sam")))
        menu.addMenu(sub)
        menu.addAction(Action(FluentIcon.DELETE, "删除",
                              triggered=lambda: self._layer_delete(name)))
        self._layer_ctx_menu = menu      # 防 GC
        self._layer_ctx_sub = sub
        menu.exec(self.layer_list.mapToGlobal(pos))

    def _layer_subsplit(self, name, mode):
        """把某层按其视觉块再拆成子层。mode: basic=连通域(免费快) / sam=SAM智能。
        子层就地替换原层（保持列表位置=叠放层次），Ctrl+Z 可撤销。"""
        if self.rig is None:
            return
        obj = next((l for l in self.layers if l["name"] == name), None)
        if obj is None:
            return
        try:
            if mode == "basic":
                parts = C.subsplit_layer(obj)
            else:
                import sam_subsplit
                parts = sam_subsplit.sam_subsplit_layer(obj, verbose=lambda m: self.prog.setText(str(m)))
        except FileNotFoundError as e:
            InfoBar.warning("无法智能拆分", str(e), parent=self,
                            position=InfoBarPosition.TOP, duration=6000)
            return
        except Exception as e:
            InfoBar.error("拆分失败", zh_err(e), parent=self,
                          position=InfoBarPosition.TOP, duration=5000)
            return
        if not parts:
            if mode == "basic":
                InfoBar.info("无法再拆分",
                             "%s 是单一连通整体（SeeThrough 已按部件分层），"
                             "可试“智能拆分（SAM）”按视觉块细分" % name,
                             parent=self, position=InfoBarPosition.TOP, duration=4500)
            else:
                InfoBar.info("SAM 未分出多块", "%s 未能拆出≥2个有效块" % name,
                             parent=self, position=InfoBarPosition.TOP, duration=3500)
            return
        self._apply_subsplit(obj, parts)
        InfoBar.success("已拆分", "%s → %d 个子层（Ctrl+Z 可撤销）" % (name, len(parts)),
                        parent=self, position=InfoBarPosition.TOP, duration=3000)

    def _apply_subsplit(self, obj, parts):
        """用子层列表就地替换原层。"""
        name = obj["name"]
        idx = self.layers.index(obj)
        vis = self.visible.get(name, True)
        # 列表顺序：parts 已按面积降序（大块在前=更靠列表顶=更前层）
        new_layers = self.layers[:idx] + parts + self.layers[idx + 1:]
        self.layers = new_layers
        for p in parts:
            self.visible[p["name"]] = vis
        self.visible.pop(name, None)
        # 引擎结构同步
        self.rig["_layers"] = [l for l in self.rig["_layers"] if l["name"] != name] + parts
        self.rig["draw_order"] = [l["name"] for l in reversed(self.layers)]
        for nd in self.rig["nodes"]:
            if name in nd["layers"]:
                k = nd["layers"].index(name)
                nd["layers"][k:k + 1] = [p["name"] for p in parts]
        self.rig.pop("_lite", None)      # LOD 缓存作废
        self.reload_widgets()
        self.history.push(self)

    def _layer_to_top(self, name):
        """置顶 = 移到列表第一行 = draw_order 末位（最前层）"""
        obj = next((l for l in self.layers if l["name"] == name), None)
        if obj is None:
            return
        self.layers.remove(obj)
        self.layers.insert(0, obj)
        self.rig["draw_order"] = [l["name"] for l in reversed(self.layers)]
        self.reload_widgets()
        self.history.push(self)

    def _layer_delete(self, name):
        """删除图层：从 layers/draw_order/所属节点/_layers 摘除。
        历史快照存对象引用，撤销可完整恢复。"""
        if self.rig is None:
            return
        obj = next((l for l in self.layers if l["name"] == name), None)
        if obj is None:
            return
        self.layers = [l for l in self.layers if l["name"] != name]
        self.rig["draw_order"] = [n for n in self.rig["draw_order"] if n != name]
        self.rig["_layers"] = [l for l in self.rig["_layers"] if l["name"] != name]
        for nd in self.rig["nodes"]:
            if name in nd["layers"]:
                nd["layers"].remove(name)
        self.rig.pop("_lite", None)      # LOD 缓存里有被删层，作废重建
        self.reload_widgets()
        self.history.push(self)
        InfoBar.info("已删除图层", "%s（Ctrl+Z 可撤销）" % name, parent=self,
                     position=InfoBarPosition.TOP, duration=2500)

    # ---------- 拖入图片 = 新增一个图层（不再触发拆分） ----------
    def _drop_add_layer(self, img_path):
        """把拖入的图片作为新图层加到工作台顶部（列表首位=最前层）。
        空工作台时先建一个只有 root 语义的空 rig，方便直接拖图。"""
        try:
            pil = Image.open(img_path).convert("RGBA")
            w, h = pil.size
            arr = np.asarray(pil)
        except Exception as e:
            InfoBar.error("无法读取图片", zh_err(e), parent=self,
                          position=InfoBarPosition.TOP, duration=4000)
            return
        if arr.ndim != 3 or arr.shape[2] != 4 or w <= 0 or h <= 0:
            InfoBar.error("图片格式不支持", img_path, parent=self,
                          position=InfoBarPosition.TOP, duration=4000)
            return

        # 空工作台 -> 建空 rig（只有 root 语义），并播基线快照让 Ctrl+Z 能回空
        if self.rig is None:
            self.rig = {"width": w, "height": h, "fps": 30.0,
                        "nodes": [], "_layers": [], "draw_order": []}
            self.W, self.H = w, h
            self.cur_dir = None
            self.history.seed(self)

        W, H = self.W, self.H
        # 图片比画布大则等比缩小，否则原样居中
        sw, sh = w, h
        if sw > W or sh > H:
            sc = min(float(W) / float(sw), float(H) / float(sh))
            sw = max(1, int(round(sw * sc)))
            sh = max(1, int(round(sh * sc)))
            pil = pil.resize((sw, sh), Image.LANCZOS)
            arr = np.asarray(pil)
        x0 = max(0, (W - sw) // 2)
        y0 = max(0, (H - sh) // 2)
        x1 = min(W, x0 + sw)
        y1 = min(H, y0 + sh)

        # 图层名：文件名去扩展名 + 序号防重（image / image_2 / image_3 …）
        # \w 在 str 模式下已是 Unicode 感知（含中文），保留 . - 其余替换为 _
        stem = os.path.splitext(os.path.basename(img_path))[0]
        base = re.sub(r"[^\w.\-]+", "_", (stem or "image").strip(),
                      flags=re.UNICODE) or "image"
        name = base
        n = 2
        existing = {l["name"] for l in self.layers}
        while name in existing:
            name = "%s_%d" % (base, n)
            n += 1

        layer = {
            "name": name,
            "tag": "Image",
            "node": "other",
            "path": img_path,
            "img": np.ascontiguousarray(arr),
            "bbox": (x0, y0, x1, y1),
            "depth": 0.10,
        }

        # 列表顶=最前层；draw_order = reversed(layers)（末位=最前=最后绘制）
        self.layers.insert(0, layer)
        self.rig["_layers"] = list(self.layers)   # 拷贝不断引用：_layers 与 layers 是两个独立列表
        self.rig["draw_order"] = [l["name"] for l in reversed(self.layers)]
        self.rig.pop("_lite", None)          # LOD 缓存作废
        self.visible[name] = True
        self.reload_widgets()
        self.history.push(self)
        self.redraw()
        InfoBar.success("已添加图层", name, parent=self,
                        position=InfoBarPosition.TOP, duration=2500)

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
        if not find_meta(d):
            InfoBar.error("不是工程目录",
                          "所选文件夹里没有 layers_meta.json（也没有 ComfyUI 的 *_layers.json）：\n%s" % d,
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
        # 无 CUDA 不再阻断：改走 ComfyUI 路线，只把前提条件讲清楚
        if _engine_mode() == "comfy":
            InfoBar.info("本机无 NVIDIA CUDA，走 ComfyUI 路线拆分",
                         "前提：本机 ComfyUI 已启动且装了 See-through 插件"
                         "（安装器勾选「A 卡拆图支持」可代下 ComfyUI 安装器与插件（也可自行下载））。"
                         "推理用 bf16，首次运行插件会自动下载约 12.5GB 模型，耗时较长且可重跑续传。",
                         parent=self, position=InfoBarPosition.TOP, duration=9000)
        self.busy = True
        self.prog.setText("准备中…")
        self._nav_pct = 0
        self.set_nav_progress(0)
        out = os.path.join(OUT_BASE, os.path.splitext(os.path.basename(img_path))[0])
        self.worker = DecomposeWorker(img_path, out)
        self.worker.log_signal.connect(self._on_decompose_log)
        self.worker.done_signal.connect(self._decompose_done)
        self.worker.start()

    def _on_decompose_log(self, msg):
        msg = str(msg)
        self.prog.setText(msg[-120:])
        if msg.startswith("#pct="):          # 引擎侧直接上报百分比（估算法）
            try:
                self.set_nav_progress(int(msg[5:].split()[0]))
            except Exception:
                pass
            return
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


def _gif_frames(rgba_list):
    """RGBA 帧序列 -> GIF P 帧：255 色量化 + 索引 255 专属透明。
    返回 (frames, trans_index)。alpha<128 的像素一律映射到透明索引。"""
    TRANS = 255
    frames = []
    for i, arr in enumerate(rgba_list):
        rgb = arr[:, :, :3]
        if i == 0:
            base = Image.fromarray(rgb).quantize(colors=255, method=Image.Quantize.FASTOCTREE)
            pal = base.getpalette()[:765]          # 255*3
        q = Image.fromarray(rgb).quantize(palette=base, dither=Image.Dither.NONE)
        idx = np.array(q, dtype=np.uint8)
        idx[arr[:, :, 3] < 128] = TRANS
        im = Image.new("P", q.size)
        full = pal + [0, 0, 0]                     # 索引 255 = 透明色（RGB 0,0,0）
        im.putpalette(full)
        im.paste(Image.fromarray(idx, mode="P"))
        frames.append(im)
    return frames, TRANS


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
                # GIF：LOD 直出 + **透明背景**（255 色量化 + 索引 255 专属透明）
                gf = min(15, self.fps)
                gn = max(2, int(self.seconds * gf))
                sc = self.gif_scale
                rgba = [C.render_frame_rgba(self.rig, i / gf, hidden=self.hidden, scale=sc)
                        for i in range(gn)]
                self.progress.emit(1, gn)
                frames, trans_idx = _gif_frames(rgba)
                frames[0].save(self.path, save_all=True, append_images=frames[1:],
                               duration=int(1000 / gf), loop=0, optimize=True,
                               transparency=trans_idx, disposal=2)
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


class SidebarIcon(QIcon):
    """DSH 同款「侧栏面板」图标：圆角矩形 + 左侧分隔竖线，白色线条。
    用 QIcon 的 pixmap 回调按需生成，选中态（折叠时）自动高亮。"""

    def __init__(self):
        super().__init__()
        self.addPixmap(self._make(False), QIcon.Normal, QIcon.Off)
        self.addPixmap(self._make(True), QIcon.Normal, QIcon.On)

    @staticmethod
    def _make(active):
        pm = QPixmap(20, 20)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        col = QColor("#e8ecf4") if not active else QColor("#4d88ff")
        pen = QPen(col, 1.6)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(2.5, 3.5, 15, 13, 3, 3)
        p.drawLine(8, 4.5, 8, 15.5)
        p.end()
        return pm


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
        # 导航栏：默认隐藏（单页应用常驻导航无益，用户要求直接去掉）。
        # 标题栏侧栏按钮可唤出，唤出时 140px 完整显示「工作台」（图标+文字）。
        # 库机制：isCompacted 时 paintEvent 只画图标，必须 EXPAND 模式；
        # setCollapsible(False) 关自动展开/收起，双层定宽钉死 140。
        self.nav_width = 140
        ni = self.navigationInterface
        ni.setCollapsible(False)
        ni.setExpandWidth(self.nav_width)
        ni.setMinimumExpandWidth(0)
        try:
            ni.expand(useAni=False)
        except Exception:
            pass
        for tgt in (ni, ni.panel):
            tgt.setMinimumWidth(self.nav_width)
            tgt.setFixedWidth(self.nav_width)
        self.nav_collapsed = True
        ni.setVisible(False)
        # ---- 标题栏左上角工具条（DSH 风格）：折叠按钮 + 菜单 + 导出 ----
        self.btn_collapse = TransparentToggleToolButton(SidebarIcon(), self)
        self.btn_collapse.setFixedSize(32, 30)
        self.btn_collapse.setToolTip("显示/隐藏导航栏")
        self.btn_collapse.clicked.connect(self._toggle_nav)
        tbar = self.titleBar.hBoxLayout
        tbar.insertWidget(0, self.btn_collapse, 0, Qt.AlignLeft | Qt.AlignVCenter)
        tbar.insertWidget(1, self.wb.menu_btn, 0, Qt.AlignLeft | Qt.AlignVCenter)
        tbar.insertWidget(2, self.wb.edit_btn, 0, Qt.AlignLeft | Qt.AlignVCenter)
        # 快捷键
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.wb.act_undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.wb.act_redo)
        # Photoshop 风格画布缩放快捷键
        cv = self.wb.canvas
        QShortcut(QKeySequence("Ctrl+0"), self, activated=cv.set_fit)
        QShortcut(QKeySequence("Ctrl+1"), self, activated=cv.set_actual)
        QShortcut(QKeySequence("Ctrl+="), self, activated=lambda: cv.zoom_step(True))
        QShortcut(QKeySequence("Ctrl+-"), self, activated=lambda: cv.zoom_step(False))

    def _toggle_nav(self):
        """折叠/展开最左导航栏：直接隐藏，布局自动把空间让给内容区"""
        self.nav_collapsed = not self.nav_collapsed
        self.navigationInterface.setVisible(not self.nav_collapsed)


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
