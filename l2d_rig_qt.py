# -*- coding: utf-8 -*-
"""伪L2D 骨架工作室 V2.0 —— PySide6 + qfluentwidgets（照 Vocal2Midi 风格）

功能对齐 V1.0。标题栏左上角两个下拉按钮：「文件」（新建/打开/导入/保存/另存为/导出 PSD·AE·JSON·MP4·GIF）与「编辑」（撤销/重做/拆分图片）。
SeeThrough 拆分走 QThread，绝不阻塞 UI。
引擎接口全部复用 l2d_rig_core / decompose（v2 副本）。
"""
import os
import sys
import copy
import json
import math
import re
import time
import threading
import shutil
import tempfile
import numpy as np
from PIL import Image

from PySide6.QtWidgets import (QApplication, QWidget, QHBoxLayout, QVBoxLayout,
                               QGridLayout, QSizePolicy, QFileDialog, QLabel,
                               QStackedWidget, QListWidgetItem, QTreeWidgetItem,
                               QSplitter, QStyledItemDelegate, QAbstractItemView,
                               QStyle, QButtonGroup)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QPoint, QPointF, QSize, QEvent, QRect
from PySide6.QtGui import (QImage, QPixmap, QPainter, QPainterPath, QColor, QKeySequence,
                           QShortcut, QIcon, QCursor, QPen, QBrush,
                           QIntValidator, QPolygon)

from qfluentwidgets import (FluentWindow, FluentIcon, PushButton,
                            PrimaryPushButton, ToolButton, ToggleToolButton,
                            TransparentToolButton, TransparentToggleToolButton,
                            TransparentDropDownPushButton, CheckBox,
                            DropDownPushButton, Action, RoundMenu, ListWidget,
                            ListItemDelegate,
                            TreeWidget, Slider, BodyLabel, CaptionLabel,
                            StrongBodyLabel, FluentStyleSheet, setTheme, Theme,
                            MessageBox, InfoBar, InfoBarPosition,
                            MessageBoxBase, SubtitleLabel, LineEdit,
                            ProgressBar)

import anim as A          # v2.11.0 关键帧动画数据层
from timeline_panel import TimelinePanel   # v2.11.0 时间轴面板

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
# 单一版本源：每次小更新 patch+1（v2.0.1、v2.0.2…），成体系大功能 minor+1
VERSION = "3.2.2"
# 输出/示例数据全部相对应用目录：整个文件夹搬到哪都不影响
OUT_BASE = os.path.join(V2, "seethrough_out")
PREVIEW_SCALE = 0.5          # 播放/画布用 LOD 分辨率（导出 PSD/帧/MP4 仍是全分辨率）
ICON_PNG = os.path.join(V2, "anon_sewing_icon.png")
ICON_ICO = os.path.join(V2, "anon_sewing_icon.ico")
DEFAULT_DIR = os.path.join(V2, "示例工程")
# ffmpeg 回退：应用自带优先，其次历史上放过的共享位置
FFMPEG_CANDIDATES = [os.path.join(V2, "ffmpeg.exe"),
                     r"D:\BaiduNetdiskDownload\Vocal2Midi\ffmpeg.exe"]

PROJ_BASE = os.path.join(V2, "projects")   # 新建工程默认根目录

def find_enclosed_holes(alpha):
    """在 alpha 通道里找"被不透明像素包围的洞"（图层补洞用）。

    算法：对透明区做连通域分解；**触到图像边界**的分量是"外部"（bbox 外的
    空角，不该填），不触边界的分量才是"洞"。单次连通域分解，O(像素) 一遍。
    返回 (mask uint8 洞=255, 洞面积, 洞数量) 或 None（无洞）。
    面积 <16px 的分量忽略（抗锯齿噪点）。
    """
    import cv2
    trans = (alpha == 0).astype(np.uint8)
    if not trans.any():
        return None
    n, lab, stats, _ = cv2.connectedComponentsWithStats(trans, 8)
    if n <= 1:
        return None
    h, w = alpha.shape
    # 哪些分量触边 = 外部
    border = np.concatenate([lab[0, :], lab[-1, :], lab[:, 0], lab[:, -1]])
    outside = set(int(v) for v in border if v != 0)
    keep = np.zeros((h, w), np.uint8)
    cnt = 0
    for i in range(1, n):
        if i in outside:
            continue
        if stats[i, cv2.CC_STAT_AREA] >= 16:
            keep[lab == i] = 255
            cnt += 1
    if not keep.any():
        return None
    return keep, int((keep > 0).sum()), cnt

def make_mesh_icon(color="#dcdcdc", px=64):
    """选项栏「网格变形」开关图标：3×3 网格 + 中心点偏移（示意弯曲）。"""
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = float(px)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.5, s * 0.07))
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    # 4x4 点阵，中心几个点偏移 -> 网格线弯曲
    N = 3
    off = {(1, 1): (0.03, -0.03), (2, 1): (0.02, -0.02),
           (1, 2): (0.03, 0.02), (2, 2): (-0.02, 0.03)}
    def pt(a, b):
        ox_, oy_ = off.get((a, b), (0.0, 0.0))
        return QPoint(int((0.16 + 0.68 * a / N + ox_) * s),
                      int((0.16 + 0.68 * b / N + oy_) * s))
    for b in range(N + 1):
        p.drawPolyline(QPolygon([pt(a, b) for a in range(N + 1)]))
    for a in range(N + 1):
        p.drawPolyline(QPolygon([pt(a, b) for b in range(N + 1)]))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    for b in range(N + 1):
        for a in range(N + 1):
            p.drawEllipse(pt(a, b), max(1.5, s * 0.05), max(1.5, s * 0.05))
    p.end()
    return QIcon(pm)

def make_curve_icon(color="#dcdcdc", px=64):
    """选项栏「曲线套索」开关图标：一条 S 形贝塞尔曲线 + 两端控制点。"""
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = float(px)
    pen = QPen(QColor(color))
    pen.setWidthF(max(2.0, s * 0.10))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()
    path.moveTo(0.16 * s, 0.80 * s)
    path.cubicTo(0.62 * s, 0.72 * s, 0.38 * s, 0.28 * s, 0.84 * s, 0.20 * s)
    p.drawPath(path)
    d = max(4.0, s * 0.16)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    for cx, cy in ((0.16, 0.80), (0.84, 0.20)):
        p.drawEllipse(QPointF(cx * s, cy * s), d / 2, d / 2)
    p.end()
    return QIcon(pm)

def make_lasso_icon(color="#dcdcdc", px=64):
    """PS 风格「多边形套索」图标（自绘）。

    qfluentwidgets 没有套索图标，用 QPainter 画一个：
      直线段围成的套索环（多边形套索的特征）+ 收口处甩出的绳尾 + 起点方块标记。
    深色工具栏上用浅灰；渲染 64px 由 Qt 平滑缩放到按钮尺寸。
    """
    from PySide6.QtGui import QPainterPath
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = float(px)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.8, s * 0.08))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    # 套索环：直线段（多边形套索 = 逐点连线），收口回到起点
    ring = [(0.32, 0.19), (0.63, 0.15), (0.86, 0.38), (0.76, 0.67),
            (0.47, 0.80), (0.23, 0.60)]
    poly = QPolygon([QPoint(int(x * s), int(y * s)) for x, y in ring])
    poly.append(QPoint(int(ring[0][0] * s), int(ring[0][1] * s)))
    p.drawPolyline(poly)
    # 绳尾：从收口点（0.23,0.60）往左下甩一小段曲线
    tail = QPainterPath()
    tail.moveTo(0.23 * s, 0.60 * s)
    tail.cubicTo(0.08 * s, 0.73 * s, 0.18 * s, 0.90 * s, 0.40 * s, 0.88 * s)
    p.drawPath(tail)
    # 起点标记（PS 套索的锚点小方块）
    d = max(3.0, s * 0.13)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRect(int(0.32 * s - d / 2), int(0.19 * s - d / 2), int(d), int(d))
    p.end()
    return QIcon(pm)

def sanitize_proj_name(name, maxlen=60):
    """清洗工程名：去掉路径/文件名非法字符与首尾空白，截断 maxlen。"""
    name = (name or "").strip()
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "")
    return name.strip()[:maxlen]

def unique_proj_name(base, base_dir=None):
    """返回不重名的工程名：重名自动加 _2/_3…"""
    base = sanitize_proj_name(base) or "工程"
    base_dir = base_dir or PROJ_BASE
    cand, n = base, 2
    while os.path.isdir(os.path.join(base_dir, cand)):
        cand = "%s_%d" % (base, n)
        n += 1
    return cand

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
            # （undo 恢复的是同一对象，bbox 已被改掉）。用**对象 id** 做键
            # （层名会变，不能拿名字当键）。
            "bboxes": {id(l): tuple(l["bbox"]) for l in studio.layers},
            # 图层名值记录：改名同样是**原地改 dict['name']**，对象引用快照
            # 回退不了。用**下标**做键（名字自己会变，不能拿名字当键）。
            "names": [l["name"] for l in studio.layers],
            "draw_order": list(studio.rig.get("draw_order", [])),
            "pivots": {n["node"]: list(n["pivot"]) for n in studio.rig["nodes"]},
            "node_layers": {n["node"]: list(n["layers"]) for n in studio.rig["nodes"]},
            "_layers": list(studio.rig.get("_layers", [])),
            # v2.11.0：关键帧是可变嵌套数据 -> 必须做**深拷贝值记录**，
            # 否则 undo 拿到的是同一个引用（和 bboxes/names 一样的坑）
            "keyframes": A.clone_tracks(studio.rig.get("keyframes")),
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
           snap.get("bboxes") == prev.get("bboxes") and \
           snap.get("names") == prev.get("names") and \
           snap.get("keyframes") == prev.get("keyframes"):
            return                               # 无变化不压栈
        self.undo_stack.append(snap)
        if len(self.undo_stack) > self.limit:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def apply(self, studio, snap):
        if snap is None or studio.rig is None:
            return
        studio.layers = list(snap["layers"])
        # 回退图层名（改名的撤销）：按**对象 id** 对应回原名——
        # 名字已被改过，不能用名字去索引 names 列表
        names = snap.get("names")
        if names:
            # snap["layers"] 与 names 同序；用 id 建立当前名->原名 映射
            cur2orig = {}
            for l, orig in zip(studio.layers, names):
                cur2orig[id(l)] = orig
            # 先迁移 visible/locked 的键（必须在改 name **之前**做，
            # 否则 cur==orig 判断失效、键迁不回去）
            for dname in ("visible", "locked"):
                d = getattr(studio, dname, None)
                if isinstance(d, dict):
                    for l in studio.layers:
                        cur = l["name"]
                        orig = cur2orig.get(id(l))
                        if orig and cur != orig and cur in d and orig not in d:
                            d[orig] = d.pop(cur)
            # 再把名字改回原名
            for l in studio.layers:
                orig = cur2orig.get(id(l))
                if orig is not None and l["name"] != orig:
                    l["name"] = orig
        # 回退 bbox（移动工具的撤销靠这里）；键是对象 id（层名可能已变）
        bbs = snap.get("bboxes", {})
        for l in studio.layers:
            bb = bbs.get(id(l))
            if bb is not None:
                l["bbox"] = tuple(bb)
        studio.rig["draw_order"] = list(snap["draw_order"])
        studio.rig["_layers"] = list(snap["_layers"])
        for n in studio.rig["nodes"]:
            if n["node"] in snap["pivots"]:
                n["pivot"] = list(snap["pivots"][n["node"]])
            if n["node"] in snap["node_layers"]:
                n["layers"] = list(snap["node_layers"][n["node"]])
        studio.rig.pop("_lite", None)           # 结构变了，LOD 缓存作废
        # v2.11.0：恢复关键帧（深拷贝，避免后续编辑污染历史快照）
        if "keyframes" in snap:
            studio.rig["keyframes"] = A.clone_tracks(snap.get("keyframes"))
        studio.reload_widgets()
        # 时间轴面板跟着刷新（撤销/重做后关键帧点要跟着变）
        panel = getattr(studio, "anim_panel", None)
        if panel is not None:
            try:
                panel.refresh()
            except Exception:
                pass

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
    lasso_committed = Signal(object)               # 多边形套索：提交选区点集（图像坐标）
    selection_changed = Signal(object)            # 选区状态变化（进入/更新选区，尚未裁剪）
    ft_committed = Signal(object)                 # 自由变换：提交最终状态（图像坐标）
    ft_changed = Signal(object)                   # 自由变换：拖动中状态变化（选项栏实时显示）
    ft_cancelled = Signal()                       # 自由变换：取消（Esc）

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
        # 工具："pan"=默认（左键拖支点）；"move"=移动图层（只拖当前选中层）；"lasso"=多边形套索
        self.tool = "pan"
        self._layers_ref = []           # [(name, bbox_full, (visible, locked))] 列表前=叠放顶
        self.selected_name = None       # 当前选中图层名（图层列表 currentItem 同步）
        self._mv = None                 # 移动拖拽态 (name, 按下点全分辨率坐标)
        # 多边形套索：_lasso 是当前正在画的顶点；_lasso_done 是已闭合的选区列表
        # [{"points": [...], "op": "add"|"sub"}]，多次圈选可累积（PS 的加选/减选）
        self._lasso = []
        self._lasso_done = []
        self._lasso_drag = None         # 顶点拖拽态 (索引, 起点)
        self._lasso_op = "add"          # 当前圈的选区操作：add / sub
        self._selection_active = False  # 选区已激活（闭合后等用户复制/剪切/应用）
        self._selection_invert = False  # 反选显示（选区=多边形之外）
        self._lasso_curve = False       # 曲线套索模式（选项栏开关）
        # 自由变换（Ctrl+T）：状态 dict{cx,cy,hw,hh,ang}（图像坐标）
        self._ft = None                 # None = 未激活
        self._ft_drag = None            # ("handle",name)/("move",)/("rotate",) + 按下点
        self._ft_drag_base = None       # 拖动基准快照（每帧从它重算，避免增量累积飞走）

    def set_tool(self, t):
        if t != self.tool:
            self._lasso = []          # 换工具清掉未闭合的选区
            self._lasso_done = []     # 已闭合的选区也清掉
        self.tool = t
        if t == "move":
            self.setCursor(Qt.SizeAllCursor)
        elif t == "lasso":
            self.setCursor(Qt.CrossCursor)
        else:
            self.unsetCursor()
        self.update()

    def set_layers(self, ref):
        """Workbench 每次 redraw 同步：[(name, bbox(全分辨率), (visible, locked))]，
        顺序=图层列表顺序（前=叠放最上），命中测试按此序取第一个。
        兼容旧式三元组 (name, bbox, visible)。"""
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
        # ---- 自由变换（Ctrl+T）：虚线框 + 8 控制点 + 中心标记 ----
        if self._ft is not None:
            import transform_tool as TT
            sc = PREVIEW_SCALE
            def _fw(px, py):
                return QPoint(int(ox + px * sc * s), int(oy + py * sc * s))
            pts = [_fw(x, y) for x, y in TT.corners(self._ft)]
            pen = QPen(QColor("#ffffff"), 1, Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            poly = QPolygon(pts)
            poly.append(pts[0])
            p.drawPolyline(poly)
            # 中心标记（PS 的锚点）
            c0 = _fw(self._ft["cx"], self._ft["cy"])
            p.drawLine(c0.x() - 5, c0.y(), c0.x() + 5, c0.y())
            p.drawLine(c0.x(), c0.y() - 5, c0.x(), c0.y() + 5)
            # 8 控制点：白底黑框小方块
            p.setPen(QPen(QColor("#20242c"), 1))
            p.setBrush(QColor("#ffffff"))
            for _, hx, hy in TT.handle_points(self._ft):
                q = _fw(hx, hy)
                p.drawRect(q.x() - 4, q.y() - 4, 8, 8)
            # ---- 网格变形模式：画 (n+1)x(n+1) 网格线 + 圆点控制点 ----
            if getattr(self, "_ft_mesh_on", False):
                st_m = TT.mesh_init(self._ft)
                n_m = int(st_m.get("mesh_n") or TT.GRID_N)
                mp = [_fw(x, y) for x, y in TT.mesh_points(st_m, n_m)]
                at = lambda a, b: mp[b * (n_m + 1) + a]
                # 横线（沿 is 方向）与竖线（沿 js 方向）
                p.setPen(QPen(QColor("#4d88ff"), 1, Qt.DotLine))
                p.setBrush(Qt.NoBrush)
                for b in range(n_m + 1):
                    p.drawPolyline(QPolygon([at(a, b) for a in range(n_m + 1)]))
                for a in range(n_m + 1):
                    p.drawPolyline(QPolygon([at(a, b) for b in range(n_m + 1)]))
                # 网格点：蓝点白边（与白色方块控制点区分，避免混淆）
                p.setPen(QPen(QColor("#ffffff"), 1))
                p.setBrush(QColor("#4d88ff"))
                for q in mp:
                    p.drawEllipse(q, 3, 3)
        # ---- 多边形套索：绘制已闭合选区（蚂蚁线）+ 当前正在画的路径 ----
        if self._lasso_done or self._lasso:
            sc = PREVIEW_SCALE
            def _w(px, py):
                return QPoint(int(ox + px * sc * s), int(oy + py * sc * s))
            # 已闭合选区：加选=蓝色实线，减选=红色虚线（PS 的加/减选配色）
            # curve 选区：用样条加密后的点绘制，预览与掩码边界一致
            inv = bool(getattr(self, "_selection_invert", False))
            for sel in self._lasso_done:
                raw = sel.get("points", [])
                if sel.get("curve"):
                    import lasso_tool
                    dense = lasso_tool.expand_curve_points(raw)
                    pts = [_w(px, py) for px, py in dense]
                else:
                    pts = [_w(px, py) for px, py in raw]
                if len(pts) < 2:
                    continue
                sub = sel.get("op") == "sub"
                p.setPen(QPen(QColor("#e05c5c" if sub else "#4d88ff"), 1,
                              Qt.DashLine if sub else Qt.SolidLine))
                p.setBrush(Qt.NoBrush)
                poly = QPolygon(pts)
                poly.append(pts[0])
                p.drawPolyline(poly)
                # 曲线模式：控制点画空心圈（PS 贝塞尔编辑点的样子）
                if sel.get("curve"):
                    p.setPen(QPen(QColor("#4d88ff"), 1))
                    p.setBrush(Qt.NoBrush)
                    for cp in (_w(px, py) for px, py in raw):
                        p.drawEllipse(cp, 3, 3)
            # 反选态：额外描出画布边框（橙黄虚线），表示"框外全部被选中"
            if inv and self._lasso_done:
                p.setPen(QPen(QColor("#f0a020"), 1, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                p.drawRect(ox, oy, max(1, dw - 1), max(1, dh - 1))
            # 正在画的路径
            if self._lasso:
                pts = [_w(px, py) for px, py in self._lasso]
                p.setPen(QPen(QColor("#4d88ff"), 1, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                if len(pts) >= 2:
                    poly = QPolygon(pts)
                    poly.append(pts[0])
                    p.drawPolyline(poly)
                p.setBrush(QColor("#4d88ff"))
                for pt in pts:
                    p.drawEllipse(pt, 2, 2)
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

    def _selected_layer_hit(self, ix, iy):
        """移动工具命中测试：只认当前选中层。

        规则（用户要求：图层的空白区域也能拖）：
          - 选中层存在、可见、未锁定 → **画布任意位置按下都拖它**，
            包括图层 bbox 之外的空白处（PS 里拖已选对象的手感）；
          - 图层 bbox **之内**的透明像素同样可拖（bbox 即判定范围，不看 alpha），
            实测图层 bbox 内有 20%~71% 是透明像素，若按 alpha 判定会大片拖不动；
          - 选中层被隐藏 / 锁定 / 已删除 → 不可拖（返回 None，什么都不发生，无 InfoBar）。
        """
        name = self.selected_name
        if not name:
            return None
        for item in self._layers_ref:
            nm = item[0]
            extra = item[2]
            if isinstance(extra, tuple):
                vis, locked = extra
            else:
                vis, locked = extra, False
            if nm == name:
                if vis and not locked:
                    return name
                return None            # 选中层被隐藏/锁定：不可拖
        return None                    # 选中层已不在画布数据里（刚被删）

    def lasso_reapply(self):
        """顶点拖动后重新提交选区（形状变了，需要重新裁剪）"""
        if self._lasso_done:
            self.lasso_committed.emit(list(self._lasso_done))

    # ---------- 自由变换（Ctrl+T）----------
    def ft_begin(self, bbox):
        """进入自由变换：bbox=图层当前 bbox（图像坐标）"""
        import transform_tool as TT
        self._ft = TT.state_from_bbox(bbox)
        self._ft_drag = None
        self._ft_drag_base = None
        self.update()

    def ft_active(self):
        return self._ft is not None

    def ft_commit(self):
        if self._ft is not None:
            st = dict(self._ft)
            # 先清状态再 emit：槽函数里会刷新选项栏可见性，
            # 若此时 _ft 仍在，ft_active() 为 True -> 提交后选项栏不会隐藏
            self._ft = None
            self._ft_drag = None
            self.update()
            self.ft_committed.emit(st)

    def ft_cancel(self):
        if self._ft is not None:
            self._ft = None
            self._ft_drag = None
            self._ft_drag_base = None
            self.update()
            self.ft_cancelled.emit()

    def _ft_hit(self, pos):
        """widget 坐标 -> 变换框命中。

        优先级：网格点（网格模式开启时）> 8 个边框控制点 > 框内移动 > 角外旋转。
        网格点优先是因为它面积小、且只在自己位置上出现，不会和边框点打架。
        """
        if self._ft is None:
            return None
        import transform_tool as TT
        ix, iy = self._to_full(pos)
        ox, oy, s, dw, dh = self._geom
        thr = 8.0 / max(0.01, s * PREVIEW_SCALE)   # 8 widget px 折算回图像坐标
        if getattr(self, "_ft_mesh_on", False):
            mh = TT.mesh_hit(self._ft, ix, iy, thr)
            if mh is not None:
                return ("mesh", mh)                 # mh = (is, js)
        return TT.hit(self._ft, ix, iy, thr)

    def _ft_mesh_toggle(self, on):
        """开关网格变形（PS 的「变形」模式）：开启时补 mesh 字段并在画布画网格。"""
        import transform_tool as TT
        self._ft_mesh_on = bool(on)
        if self._ft is not None and on:
            self._ft = TT.mesh_init(self._ft)
            self.ft_changed.emit(dict(self._ft))    # 触发重渲染 + 选项栏同步
        self.update()

    def _lasso_vertex_at(self, pos, radius=7):
        """命中已闭合选区的顶点：返回 (选区索引, 顶点索引) 或 None。"""
        sc = PREVIEW_SCALE
        ox, oy, s, dw, dh = self._geom
        for si, sel in enumerate(self._lasso_done):
            for vi, (px, py) in enumerate(sel.get("points", [])):
                wx = ox + px * sc * s
                wy = oy + py * sc * s
                if (wx - pos.x()) ** 2 + (wy - pos.y()) ** 2 <= radius * radius:
                    return (si, vi)
        return None

    def mousePressEvent(self, e):
        if e.button() == Qt.MiddleButton or self._space:
            # 平移：中键 或 空格+左键（任何工具模式下都保留）
            self._panning = (e.position().x(), e.position().y(), self.pan[0], self.pan[1])
            self.setCursor(Qt.ClosedHandCursor if self._space else Qt.OpenHandCursor)
            return
        if e.button() == Qt.LeftButton:
            if self._ft is not None:
                # 自由变换激活：左键只用于操作变换框
                h = self._ft_hit(e.position().toPoint())
                if h is not None:
                    kind, name = h
                    ix, iy = self._to_full(e.position().toPoint())
                    self._ft_drag = (kind, name, ix, iy)
                    # 拖动基准快照：拖动期间每帧从它重算（绝对定位，不累积）
                    import copy as _copy
                    self._ft_drag_base = _copy.deepcopy(dict(self._ft))
                self.update()
                return
            if self.tool == "lasso":
                # 已闭合选区的顶点可拖动（调整选区形状）
                hit = self._lasso_vertex_at(e.position().toPoint())
                if hit is not None:
                    self._lasso_drag = hit
                    return
                # 多边形套索：左键加点，双击闭合（mouseDoubleClickEvent 负责闭合）
                # Shift = 加选(默认)，Alt = 减选
                op = "sub" if (e.modifiers() & Qt.AltModifier) else "add"
                self._lasso_op = op
                self._lasso.append(self._to_full(e.position().toPoint()))
                self.update()
                return
            if self.tool == "move":
                ix, iy = self._to_full(e.position().toPoint())
                name = self._selected_layer_hit(ix, iy)
                if name:
                    self._mv = (name, ix, iy)
                    self.move_grabbed.emit(name)
                # 没按在选中层 bbox 内：什么都不发生（不自动换层、无 InfoBar）
                return
            n = self._node_at(e.position().toPoint())
            if n:
                self.drag_node = n

    def mouseDoubleClickEvent(self, e):
        """多边形套索：双击闭合并提交选区；自由变换：双击=提交（PS 行为）"""
        if self._ft is not None and e.button() == Qt.LeftButton:
            self.ft_commit()
            return
        if self.tool == "lasso" and e.button() == Qt.LeftButton:
            self._commit_lasso()
            return
        super().mouseDoubleClickEvent(e)

    def _commit_lasso(self, op=None):
        """闭合多边形：进入"选区激活"状态（PS 式两段操作）。

        流程：
            闭合选区 -> **进入选区状态**（画布显示选区边框，可继续加减选、拖顶点）
            -> Ctrl+C 复制 / Ctrl+X 剪切 / Ctrl+V 粘贴（新建顶部图层）
            -> 按 Delete 或 Enter 才**真正裁剪**原图层；Esc 取消选区。
        op: "add"=加选（默认）、"sub"=减选（Alt 修饰）。
        """
        pts = list(self._lasso)
        # 双击最后一点闭合时，鼠标按下已加过一个重复顶点 —— 去掉末尾重复点
        if len(pts) >= 2 and abs(pts[-1][0] - pts[-2][0]) < 0.5 and \
           abs(pts[-1][1] - pts[-2][1]) < 0.5:
            pts = pts[:-1]
        self._lasso = []
        self.update()
        if len(pts) < 3:
            return
        if op is None:
            op = getattr(self, "_lasso_op", "add")
        # curve 模式：控制点少而稀，实际选区边界由 Catmull-Rom 样条加密（PS 曲线套索）
        self._lasso_done.append({"points": pts, "op": op,
                                 "curve": bool(getattr(self, "_lasso_curve", False))})
        self._selection_active = True       # 进入选区状态（不立即裁剪）
        self.selection_changed.emit(list(self._lasso_done))

    def lasso_apply_selection(self):
        """把当前选区真正应用到图层（裁剪）——Delete / Enter 触发"""
        if self._lasso_done:
            self.lasso_committed.emit(list(self._lasso_done))
            self._lasso_done = []
            self._selection_active = False
            self.update()

    def lasso_cancel_selection(self):
        """取消选区（Esc）——不裁剪"""
        self._lasso_done = []
        self._selection_active = False
        self._selection_invert = False
        self.update()
        self.selection_changed.emit([])     # 通知 Workbench 清状态

    def lasso_reset(self):
        """外部要求清空选区（切工具/取消）"""
        if self._lasso or self._lasso_done:
            self._lasso = []
            self._lasso_done = []
            self._selection_active = False
            self._selection_invert = False
            self.update()

    def mouseMoveEvent(self, e):
        if self._panning:
            px, py, bpx, bpy = self._panning
            self.pan[0] = bpx + (e.position().x() - px)
            self.pan[1] = bpy + (e.position().y() - py)
            self.update()
            return
        if self._ft is not None and self._ft_drag is not None:
            # 自由变换拖动：控制点缩放 / 框内平移 / 角外旋转 / 网格点变形
            #
            # 关键：所有拖动都从**按下时的快照** (_ft_drag_base) 重算，而不是在
            # 当前状态上做增量。曾用增量式（每帧 cx += dx），而 dx 是"按下点到
            # 当前点"的累计量 —— 每帧都从按下点重算一次增量，鼠标一动位移就
            # 指数级累积，表现为"对象飞走"。绝对重算彻底消除该问题。
            import transform_tool as TT
            kind, name, sx, sy = self._ft_drag
            base = getattr(self, "_ft_drag_base", None) or self._ft
            ix, iy = self._to_full(e.position().toPoint())
            keep_ar = bool(e.modifiers() & Qt.ShiftModifier)
            if kind == "handle":
                self._ft = TT.drag_handle(base, name, ix, iy, keep_aspect=keep_ar)
            elif kind == "mesh":
                # name 是 (is, js)：拖网格点做局部变形（自由变换的"变形"模式）
                is_, js_ = name
                self._ft = TT.drag_mesh(base, is_, js_, ix, iy)
            elif kind == "move":
                self._ft = TT.drag_move(base, ix, iy, sx, sy)
            elif kind == "rotate":
                self._ft = TT.drag_rotate(base, ix, iy, sx, sy)
            self.ft_changed.emit(dict(self._ft))
            self.update()
            return
        if self._lasso_drag is not None:
            # 拖动已闭合选区的顶点
            si, vi = self._lasso_drag
            try:
                ix, iy = self._to_full(e.position().toPoint())
                self._lasso_done[si]["points"][vi] = (ix, iy)
                self.update()
            except Exception:
                pass
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
        if self._ft_drag is not None:
            self._ft_drag = None
            self._ft_drag_base = None          # 拖动结束，丢弃基准快照
            return
        if self._lasso_drag is not None:
            self._lasso_drag = None
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
        # 空格 -> 临时平移（任何工具模式下都可用）
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self._space = True
            self.setCursor(Qt.OpenHandCursor)
            return
        # 自由变换激活：Enter/双击=提交，Esc=取消（优先于其它按键逻辑）
        if self._ft is not None:
            if e.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.ft_commit()
                return
            if e.key() == Qt.Key_Escape:
                self.ft_cancel()
                return
            super().keyPressEvent(e)
            return
        # 套索工具：Enter 闭合选区（有选区则应用裁剪）、Esc 取消
        if self.tool == "lasso":
            if e.key() in (Qt.Key_Return, Qt.Key_Enter):
                if self._lasso_done:
                    self.lasso_apply_selection()   # 已有选区 -> 应用裁剪
                else:
                    self._commit_lasso()            # 正在画 -> 闭合
                return
            if e.key() == Qt.Key_Escape:
                if self._lasso_done:
                    self.lasso_cancel_selection()
                else:
                    self._lasso = []
                    self.update()
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

# ---------------- 最近工程（持久化：存储上限 100；菜单只显 5，欢迎页按类型分页展示） ----------------
RECENT_FILE = os.path.join(V2, "recent_projects.json")
RECENT_MAX = 100
RECENT_MENU_MAX = 5

def load_recent():
    try:
        items = json.load(open(RECENT_FILE, encoding="utf8"))
    except Exception:
        return []
    out = []
    for it in items:
        d = it.get("dir")
        if d and (os.path.isfile(os.path.join(d, "layers_meta.json"))
                  or os.path.isfile(os.path.join(d, "video_project.json"))):
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
    """选拆分引擎通道（触发条件自 v2.4 起保持不变，task-15 只在其后追加 CPU 兜底）：
       local = 进程内 SeeThrough（有 CUDA：加速包/系统 CUDA 的 nf4 快档；
               无 CUDA：CPU 兜底档，确认门在 seethrough_local.decompose）
       comfy = 本机 ComfyUI + See-through 插件（bf16，A 卡/ROCm 或无 CUDA 时）
    完整四档链判定见 engine_tier()。
    环境变量 MUPPET_ENGINE=local|comfy 可强制（N 卡想跑全精度质量更高时用 comfy）。"""
    force = os.environ.get("MUPPET_ENGINE", "").strip().lower()
    if force in ("local", "comfy"):
        return force
    return "local" if _cuda_available() else "comfy"


# ---------------- 引擎档位判定（task-15 四档优先级链） ----------------
TIER_LABELS = {
    "accel_pack": "CUDA 加速包",
    "system_cuda": "系统 CUDA",
    "comfy": "ComfyUI（A 卡/ROCm）",
    "cpu": "CPU 兜底（需确认）",
}

def _accel_pack_installed():
    """加速包是否已落地（asset_catalog done_marker 纯文件探测，
    绝不 import torch——那会加载 DLL 污染当前进程）。"""
    try:
        import asset_catalog as AC
        a = AC.asset_by_id("cuda_pack")
        return bool(a) and AC.is_installed(a, V2)
    except Exception:
        return False

_COMFY_PROBE_TTL = 20.0            # 探测结果缓存有效期（秒）
_COMFY_PROBE_CACHE = {"ok": False, "ts": 0.0}


def _probe_comfy_parallel(timeout=1.0):
    """并行探测 3 个 localhost 端口，返回首个可用 base 或 None。

    串行探测 3 端口 × 超时是资产管理菜单卡 18s 的主因之一（task-18），
    这里用线程池并行，总耗时≈单端口超时。COMFY_API 限定时只探它一个。
    """
    try:
        import comfy_decompose as CD
    except Exception:
        return None
    env = os.environ.get("COMFY_API")
    cands = [env.rstrip("/")] if env else [
        "http://127.0.0.1:%d" % p for p in CD.DEFAULT_API_PORTS]
    hits = [None] * len(cands)

    def _try(i, base):
        try:
            CD.get_json(base, "/system_stats", timeout=timeout)
            hits[i] = base
        except Exception:
            pass

    threads = [threading.Thread(target=_try, args=(i, b), daemon=True)
               for i, b in enumerate(cands)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return next((h for h in hits if h), None)


def _comfy_available(timeout=1.0):
    """本机是否有在跑的 ComfyUI（三端口并行短超时探测 + TTL 20s 缓存）。

    旧实现串行探 3 端口 × 4s = 6.2s（防火墙下 12s），会让资产管理菜单
    冻结。改为并行 1s 超时并 TTL 缓存，重复调用不重复探测。"""
    now = time.time()
    c = _COMFY_PROBE_CACHE
    if now - c["ts"] < _COMFY_PROBE_TTL:
        return c["ok"]
    ok = _probe_comfy_parallel(timeout) is not None
    c["ok"] = ok
    c["ts"] = now
    return ok


# ---------------- 档位后台预热（task-18：资产管理菜单不冻结） ----------------
# 菜单重建路径只读缓存，绝不在主线程 import torch / socket。
_TIER_CACHE = {"tier": None, "reason": None, "ts": 0.0, "done": False}
_TIER_WARM_STARTED = [False]        # 预热去重：预热中不重复 spawn
_TIER_WARMER = [None]               # 持引用防预热线程被 GC
# v3.2.1 退出保护：预热线程 import torch 会长时间持有 OS loader lock，
# 此时进程退出（含 os._exit）可能卡死在终止序列（py-spy 实锤的竞态）。
# 应用开始退出后不再启动新预热；已在途的无法打断，但窗口被压到最小。
_APP_EXITING = [False]


class _TierWarmWorker:
    """后台跑一次 engine_tier()（含 import torch / cuda 检测 / comfy 探测），
    结果写 _TIER_CACHE。启动后绝不阻塞主线程。

    用 daemon 的 threading.Thread 而非 QThread：预热含 ~12s 的 torch import，
    若用 QThread，进程退出时线程仍在运行，QThread 析构会触发
    "QThread: Destroyed while thread is still running" 并 abort（实测回归
    退出码 0xC0000409）。daemon 线程随进程退出自然结束，无此问题。"""

    def __init__(self):
        self._t = threading.Thread(target=self._run, daemon=True,
                                   name="tier-warmup")

    def start(self):
        self._t.start()

    def _run(self):
        if _APP_EXITING[0]:
            _TIER_WARM_STARTED[0] = False
            return
        try:
            tier, reason = engine_tier()
        except Exception:
            tier, reason = None, "档位检测失败"
        _TIER_CACHE["tier"] = tier
        _TIER_CACHE["reason"] = reason
        _TIER_CACHE["ts"] = time.time()
        _TIER_CACHE["done"] = True
        _TIER_WARM_STARTED[0] = False


def _tier_display():
    """返回菜单要显示的档位文案；无缓存返回 None（菜单显示"正在检测档位…"）。

    纯读 _TIER_CACHE，不触发任何 import torch / socket / 探测。"""
    c = _TIER_CACHE
    if not c.get("done"):
        return None
    tier = c.get("tier")
    if tier:
        return TIER_LABELS.get(tier, tier)
    return "检测失败"


def _start_tier_warmup():
    """启动后台档位预热（去重）：已有缓存或预热中则直接返回。"""
    if _APP_EXITING[0] or _TIER_CACHE.get("done") or _TIER_WARM_STARTED[0]:
        return
    _TIER_WARM_STARTED[0] = True
    w = _TierWarmWorker()
    _TIER_WARMER[0] = w
    try:
        w.start()
    except Exception:
        _TIER_WARM_STARTED[0] = False

def engine_tier(probe_comfy=True, comfy_timeout=2.0):
    """四档优先级链判定，返回 (tier, reason)，tier ∈ TIER_LABELS。

       1. accel_pack   CUDA 加速包已装 且 CUDA 可用 → local nf4 快档
       2. system_cuda  当前 Python 的 torch 检出 CUDA → local nf4 快档
       3. comfy        无 CUDA 但 ComfyUI 在跑（A 卡路线，触发条件不动）
       4. cpu          前三档全不可用 → CPU 最后兜底（需确认，门槛在
                       seethrough_local.decompose：UI 弹框确认或 env 逃生阀）

    说明：档 1/2 在实现上收敛为同一个"local CUDA"，按"加速包已装"标记区分
    来源展示；MUPPET_FORCE_DEVICE=cpu 视为用户手动落到 CPU 档。
    probe_comfy=True 时会做 localhost 探测（三端口并行 + TTL 20s 缓存，
    一般毫秒级）。

    注意（task-18）：本函数包含 import torch 与 socket 探测，**只应在
    后台线程调用**；菜单/UI 显示走 _tier_display()（纯读 _TIER_CACHE）。"""
    if os.environ.get("MUPPET_FORCE_DEVICE", "").strip().lower() == "cpu":
        return "cpu", "MUPPET_FORCE_DEVICE 已把设备钉为 CPU（需确认）"
    if _cuda_available():
        if _accel_pack_installed():
            return "accel_pack", "CUDA 加速包已装，本地引擎走 nf4 快档（约 14 分钟/张）"
        return "system_cuda", "系统 CUDA 可用，本地引擎走 nf4 快档"
    if probe_comfy and _comfy_available(min(1.0, float(comfy_timeout))):
        return "comfy", "无 CUDA，检测到在跑的 ComfyUI（A 卡/ROCm 路线）"
    return "cpu", "无 CUDA 且无在跑的 ComfyUI，只剩 CPU 兜底档"

def _is_cpu_confirm_error(e):
    """识别 CPU 兜底确认门异常。不用 except 具体类：ComfyUI 分支不 import
    seethrough_local，异常处理路径里类型可能尚未加载；类名匹配兜底。"""
    if type(e).__name__ == "CpuConfirmationRequired":
        return True
    try:
        import seethrough_local as SL
        return isinstance(e, SL.CpuConfirmationRequired)
    except Exception:
        return False

class DecomposeWorker(QThread):
    log_signal = Signal(str)
    done_signal = Signal(object, str)     # meta or None, out_dir
    need_cpu_confirm = Signal()           # CPU 兜底档待用户确认（task-15）

    def __init__(self, img_path, out_dir):
        super().__init__()
        self.img_path = img_path
        self.out_dir = out_dir

    def _tier_label(self):
        # 已确定走 local 通道，无需再探 ComfyUI（probe_comfy=False 不打扰 GUI 线程）
        try:
            tier, _ = engine_tier(probe_comfy=False)
            return TIER_LABELS.get(tier, tier)
        except Exception:
            return "本地 nf4"

    def _run_local(self):
        import seethrough_local as SL
        self.log_signal.emit("引擎：本地进程内 SeeThrough（%s）…" % self._tier_label())
        return SL.decompose(self.img_path, self.out_dir,
                            verbose=self.log_signal.emit)

    def _run_comfy(self):
        import comfy_decompose as CD
        self.log_signal.emit(
            "引擎：ComfyUI + See-through 插件（bf16，A 卡/ROCm 路线）…")

        def prog(sec):
            # 无中间进度可查，按耗时线性估：700s 视作正常完成点
            pct = int(15 + min(sec, 700) / 700.0 * 65)
            self.log_signal.emit(
                "#pct=%d ComfyUI 推理中 %ds（A 卡首跑含模型下载会更久）" % (pct, sec))

        return CD.decompose(self.img_path, self.out_dir, quant="none",
                            auto_download=True, prefix="muppet",
                            verbose=self.log_signal.emit,
                            on_progress=prog, timeout=3600)

    def run(self):
        # 四档链（task-15）：有 CUDA → local（加速包/系统档）；无 CUDA 仍默认
        # comfy（触发条件不动）；仅当 ComfyUI 也不可用（没在跑）才落到
        # CPU 兜底——确认门由 seethrough_local 抛出，这里转成 UI 信号。
        mode = _engine_mode()
        try:
            if mode == "local":
                meta = self._run_local()
            elif _comfy_available(timeout=4.0):
                meta = self._run_comfy()
            else:
                self.log_signal.emit(
                    "未发现在跑的 ComfyUI：尝试 CPU 最后兜底档…")
                meta = self._run_local()
            self.done_signal.emit(meta, self.out_dir)
        except Exception as e:
            if _is_cpu_confirm_error(e):
                self.log_signal.emit("CPU 兜底档需要你的确认（约 2-3 小时/张）")
                self.need_cpu_confirm.emit()
                return
            self.log_signal.emit("异常: %s: %s" % (type(e).__name__, e))
            self.done_signal.emit(None, self.out_dir)

# ---------------- 新建画布尺寸对话框 ----------------
class NumSpinBox(QWidget):
    """替代 qfluentwidgets SpinBox（1.11.3 缺陷：InlineSpinBoxBase 建了
    hBoxLayout 只装 up/down 按钮，没把 QSpinBox 自己的 lineEdit 装进去，
    lineEdit 被挤成 1x31——数字区不可见不可点，"宽高输入框无法使用"）。

    结构：qfw LineEdit(QIntValidator) + 右侧上下箭头（复用 qfw 的
    SpinButton，就是原 SpinBox 那两个箭头的实现，支持长按连发）。
    对外接口兼容：value() / setValue() / setRange()。
    """

    valueChanged = Signal(int)

    def __init__(self, minimum=1, maximum=8192, value=1024, parent=None):
        super().__init__(parent)
        from qfluentwidgets.components.widgets.spin_box import SpinButton, SpinIcon
        self._min = int(minimum)
        self._max = int(maximum)
        self._value = max(self._min, min(self._max, int(value)))
        self._guard = False             # 程序化 setText 时不回环触发

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)

        self.editor = LineEdit(self)
        self.editor.setValidator(QIntValidator(self._min, self._max, self))
        self.editor.setFixedHeight(33)
        self.editor.setText(str(self._value))
        self.editor.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lay.addWidget(self.editor, 1)

        self.upBtn = SpinButton(SpinIcon.UP, self)
        self.downBtn = SpinButton(SpinIcon.DOWN, self)
        for b in (self.upBtn, self.downBtn):
            b.setAutoRepeat(True)       # 长按连续 ±1
            b.setAutoRepeatDelay(400)
            b.setAutoRepeatInterval(80)
            # qfw SpinButton 默认较宽（两个约占 60px），会把 editor 挤成 29px
            # 只显示数字，箭头窄一点即可
            b.setFixedWidth(16)
        self.upBtn.clicked.connect(lambda: self.stepBy(1))
        self.downBtn.clicked.connect(lambda: self.stepBy(-1))
        lay.addWidget(self.upBtn)
        lay.addWidget(self.downBtn)

        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.editingFinished.connect(self._normalize)

    # ---- 对外接口（兼容 qfw SpinBox 调用方） ----
    def value(self):
        return self._value

    def setValue(self, v):
        v = max(self._min, min(self._max, int(v)))
        if v == self._value and self.editor.text() == str(v):
            return
        self._value = v
        self._guard = True
        self.editor.setText(str(v))
        self._guard = False
        self.valueChanged.emit(v)

    def setRange(self, minimum, maximum):
        self._min, self._max = int(minimum), int(maximum)
        self.editor.setValidator(QIntValidator(self._min, self._max, self))
        self.setValue(self._value)

    def stepBy(self, n):
        self.setValue(self._value + int(n))

    # ---- 内部 ----
    def _on_text_changed(self, text):
        """过滤非法输入：validator 已拦非数字；中间态（空串/超范围）不更新，
        editingFinished 统一规范化。"""
        if self._guard:
            return
        t = text.strip()
        if not t:
            return                      # 空串等失焦规范化回退
        try:
            v = int(t)
        except ValueError:
            return
        if self._min <= v <= self._max:
            self._value = v
            self.valueChanged.emit(v)

    def _normalize(self):
        """失焦/回车规范化：空串或超范围 -> 回退旧值。"""
        t = self.editor.text().strip()
        try:
            v = int(t)
        except ValueError:
            v = None
        if v is None or not (self._min <= v <= self._max):
            self.setValue(self._value)
        elif str(v) != self.editor.text():
            self.setValue(v)

class NewCanvasDialog(MessageBoxBase):
    """「文件 → 新建」弹窗：输入工程名 + 画布宽高（像素）。默认 1024×1024，1..8192。"""

    def __init__(self, parent=None, default_name="工程"):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel("新建工程", self)
        self.viewLayout.addWidget(self.titleLabel)

        # 工程名输入（顶部一行）
        name_row = QHBoxLayout()
        name_row.addWidget(BodyLabel("工程名", self))
        self.name_edit = LineEdit(self)
        self.name_edit.setFixedWidth(240)
        self.name_edit.setText(default_name)
        name_row.addWidget(self.name_edit)
        name_row.addStretch(1)
        self.viewLayout.addLayout(name_row)

        self.hint = CaptionLabel("设置画布尺寸（像素），之后拖入或导入的图片将按此画布摆放。",
                                 self)
        self.viewLayout.addWidget(self.hint)

        row1 = QHBoxLayout()
        row1.addWidget(BodyLabel("宽度", self))
        self.w_spin = NumSpinBox(1, 8192, 1024, self)
        self.w_spin.setMinimumWidth(140)
        row1.addWidget(self.w_spin)
        row1.addStretch(1)
        self.viewLayout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(BodyLabel("高度", self))
        self.h_spin = NumSpinBox(1, 8192, 1024, self)
        self.h_spin.setMinimumWidth(140)
        row2.addWidget(self.h_spin)
        row2.addStretch(1)
        self.viewLayout.addLayout(row2)

        self.yesButton.setText("创建")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(360)

    def validate(self):
        # 工程名空则不允许关闭（MessageBoxBase 在 yes 时先调 validate）
        if not sanitize_proj_name(self.name_edit.text()):
            InfoBar.warning("工程名不能为空", "请输入工程名后再创建",
                            parent=self, position=InfoBarPosition.TOP, duration=2500)
            return False
        return super().validate()

    def project_name(self):
        return sanitize_proj_name(self.name_edit.text())

    def canvas_size(self):
        # 注意：绝不能叫 size()——那会覆盖 QWidget.size()，
        # 基类 MaskDialogBase.__init__ 内部调 self.size() 时控件还没建好（实测崩）
        return self.w_spin.value(), self.h_spin.value()

# ---------------- 工程名输入弹窗（另存为用） ----------------
class ProjectNameDialog(MessageBoxBase):
    """单行输入弹窗：默认填当前工程名，用户可改。"""

    def __init__(self, parent=None, title="另存为工程", prompt="工程名：",
                 default_name="工程"):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(title, self)
        self.viewLayout.addWidget(self.titleLabel)
        row = QHBoxLayout()
        row.addWidget(BodyLabel(prompt, self))
        self.name_edit = LineEdit(self)
        self.name_edit.setFixedWidth(260)
        self.name_edit.setText(default_name)
        row.addWidget(self.name_edit)
        row.addStretch(1)
        self.viewLayout.addLayout(row)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(380)

    def validate(self):
        if not sanitize_proj_name(self.name_edit.text()):
            InfoBar.warning("工程名不能为空", "请输入工程名后再保存",
                            parent=self, position=InfoBarPosition.TOP, duration=2500)
            return False
        return super().validate()

    def project_name(self):
        return sanitize_proj_name(self.name_edit.text())

# ---------------- PS 风格图层列表委托 ----------------
class LayerItemDelegate(ListItemDelegate):
    """图层列表项绘制：眼睛(24px) | 缩略图(36px) | 名称+tag | 锁定标记(24px)。

    继承 qfluentwidgets ListItemDelegate（保留其 hover/press/selected 机制与
    qfluentwidgets 的 setItemDelegate 契约），只接管绘制与点击区域：
      - 眼睛区单击 -> 切 visible（只刷该行 + redraw，不重建列表）
      - 右缘锁定区单击 -> 切 locked（移动/拆分/删除/置顶/画布命中全部跳过）
      - 缩略图区双击 -> 进入重命名（createEditor/setEditorData/setModelData）
    """

    EYE_W = 24
    THUMB = 36
    LOCK_W = 24
    ROW_H = 48

    def __init__(self, parent, wb):
        super().__init__(parent)
        self.wb = wb
        self._thumb_cache = {}

    def sizeHint(self, option, index):
        return QSize(0, self.ROW_H)

    def paint(self, painter, option, index):
        name = index.data(Qt.UserRole)
        wb = self.wb
        # 图层查表：原来每次 paint 都 next(...) 线性扫 layers（17 行 x 17 层 = 289 次比较），
        # 改用 wb 维护的 name->layer 字典（重命名/删层时同步）。
        layer = wb.layer_by_name(name)
        if not name or layer is None:
            return
        vis = wb.visible.get(name, True)
        locked = wb.locked.get(name, False)
        rect = option.rect
        row = index.row()
        is_sel = row in self.selectedRows
        is_press = self.pressedRow == row
        is_hover = self.hoverRow == row
        if is_sel:
            bg = QColor("#2f3d5c")
        elif is_press:
            bg = QColor("#2a3142")
        elif is_hover:
            bg = QColor("#262c38")
        else:
            bg = QColor("#242935")
        painter.fillRect(rect, bg)
        # 选中指示条（保留 qfluentwidgets 的选中视觉线索）
        if is_sel:
            painter.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()),
                             QColor("#4d88ff"))
        # ---- 眼睛区 ----
        ex = rect.left() + 12
        ey = rect.center().y()
        painter.setPen(QPen(QColor("#8a93a5"), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPoint(ex, ey), 6, 3.5)
        if vis:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#e6ebf5"))
            painter.drawEllipse(QPoint(ex, ey), 2.2, 1.6)
        else:
            painter.setPen(QPen(QColor("#e6ebf5"), 2))
            painter.drawLine(QPoint(ex - 7, ey - 2), QPoint(ex + 7, ey + 2))
        # ---- 缩略图区（棋盘底 + 缩放后的层图）----
        tx = rect.left() + self.EYE_W + 4
        ty = rect.center().y() - self.THUMB // 2
        painter.fillRect(QRect(tx, ty, self.THUMB, self.THUMB), QColor("#1b1f27"))
        pix = self._thumb(layer)
        if pix is not None:
            # _thumb 返回的已是缩放好的小图，直接绘制（不再每次重缩放）
            painter.drawPixmap(tx + 2, ty + 2, pix)
        # ---- 名称 + tag ----
        tx2 = tx + self.THUMB + 10
        tw = max(1, rect.right() - tx2 - self.LOCK_W - 6)
        painter.setPen(QColor("#e6ebf5") if vis else QColor("#6b7382"))
        painter.drawText(QRect(tx2, rect.top() + 4, tw, 20),
                         Qt.AlignLeft | Qt.AlignVCenter, name)
        tag = layer.get("tag")
        if tag:
            painter.setPen(QColor("#7c8593"))
            painter.drawText(QRect(tx2, rect.top() + 24, tw, 18),
                             Qt.AlignLeft | Qt.AlignVCenter, tag)
        # ---- 锁定标记（右缘）----
        if locked:
            lx = rect.right() - 12
            ly = rect.center().y()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#e6c068"))
            painter.drawRoundedRect(QRect(lx - 5, ly - 2, 10, 8), 2, 2)
            painter.drawArc(QRect(lx - 5, ly - 9, 10, 11), 0, 180 * 16)

    def _thumb(self, layer):
        """返回**已缩放到 THUMB-4 的小图**（直接绘制用）。
        优化点：原实现每次 paint 都对原图做一次 scaled() 重缩放
        （637x706 -> 32x32 SmoothTransformation），17 行重绘 = 17 次缩放，
        这就是"隐藏所有图层后画布卡很久"的主因。现在缩放结果直接缓存。"""
        img = layer.get("img")
        if img is None or getattr(img, "ndim", 0) != 3 or img.shape[2] != 4:
            return None
        h, w = img.shape[:2]
        if w <= 0 or h <= 0:
            return None
        arr = np.ascontiguousarray(img)
        if arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype(np.uint8)
        key = (layer["name"], arr.shape[1], arr.shape[0])
        cache = self._thumb_cache
        pix = cache.get(key)
        if pix is not None:
            return pix
        qimg = QImage(arr.data, w, h, 4 * w, QImage.Format.Format_RGBA8888)
        full = QPixmap.fromImage(qimg.copy())
        side = max(1, self.THUMB - 4)
        pix = full.scaled(side, side, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        if len(cache) > 400:
            cache.clear()
        cache[key] = pix
        return pix

    def editorEvent(self, event, model, option, index):
        name = index.data(Qt.UserRole)
        if not name:
            return super().editorEvent(event, model, option, index)
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            x = event.position().x() - option.rect.left()
            if x < self.EYE_W:
                self.wb.toggle_visible(name)
                self.wb._select_in_list(name)
                return True
            if x > option.rect.width() - self.LOCK_W:
                self.wb.toggle_lock(name)
                self.wb._select_in_list(name)
                return True
            return False
        if event.type() == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
            x = event.position().x() - option.rect.left()
            if self.EYE_W <= x < self.EYE_W + self.THUMB:
                self.wb.start_rename_edit(name)
                return True
        return super().editorEvent(event, model, option, index)

    def createEditor(self, parent, option, index):
        return LineEdit(parent)

    def setEditorData(self, editor, index):
        editor.setText(index.data(Qt.UserRole) or "")

    def setModelData(self, editor, model, index):
        old = index.data(Qt.UserRole)
        new = editor.text().strip()
        if old and new:
            self.wb.rename_layer(old, new)

# ---------------- 资产管理菜单 mixin（MainWindow 与视频窗壳共用） ----------------
class AssetMenuMixin:
    """「资产管理」菜单的构建与下载：MainWindow / 视频窗壳共用同一套。

    抽成 mixin 的原因：视频编辑窗口也要有同款资产管理菜单，但逻辑完全
    一致（运行环境 / 模型 / 推理加速 / 刷新状态 / 当前拆图档位行）。
    宿主需提供 self.asset_menu（RoundMenu）与 self.asset_btn（按钮），
    由 _setup_asset_menu() 统一创建。
    """

    def _setup_asset_menu(self, parent=None):
        """创建「资产管理」按钮 + 菜单并挂好 clicked -> 重建（先填后弹）。

        首击无响应修复（v3.1.1）：重建挂在 clicked 上，qfw 的弹出链是
        mouseReleaseEvent -> PushButton.mouseReleaseEvent（此处同步发
        clicked）-> _showMenu（弹菜单），直接连接的槽同步执行，保证
        「先填后弹」。挂 menu_opened 会在弹菜单之后重建，首击弹出空菜单。
        """
        p = parent if parent is not None else self
        am = ToggleDropDownButton("资产管理", p)
        self.asset_menu = RoundMenu(parent=p)
        am.clicked.connect(lambda _=False: self._rebuild_asset_menu())
        self.asset_menu_ref = self.asset_menu
        am.setMenu(self.asset_menu)
        self.asset_btn = am
        self.asset_menu_btn = am
        # v3.2.1：构造时**不**预热——直构 MainWindow 的脚本/测试秒退会和
        # torch import 的 loader lock 竞态（py-spy 实锤 os._exit 卡死）。
        # 预热改由两处触发：run_app 启动 1.5s 后 singleShot；菜单点开时
        # 缓存为空兜底触发（均带 _APP_EXITING 检查）。
        return am

    def _asset_clear_menu(self):
        """彻底清空资产菜单（含 view 残留行）。

        坑：RoundMenu.clear() 只清 actions 与 _subMenus，view(QListWidget)
        里的行会残留，每重建一次就多一行，菜单越点越长（"蜈蚣"效应）。
        这里显式把 view 也清空，并丢掉旧子菜单的引用。
        """
        menu = getattr(self, "asset_menu", None)
        if menu is None:
            return
        try:
            menu.clear()
        except Exception:
            pass
        try:
            while menu.view.count() > 0:
                menu.view.takeItem(0)
        except Exception:
            pass
        # 清掉旧的子菜单引用（防累积持有已废弃的 RoundMenu）
        try:
            self._asset_subs = []
        except Exception:
            pass

    def _rebuild_asset_menu(self):
        """构建「资产管理」菜单（平铺样式）。

        每次打开菜单都重建（本地可能刚下载完、或用户手动删过目录），
        所以状态走 asset_catalog 的实时检查而不是缓存。
        清空走 _asset_clear_menu —— 必须先清 view 残留行，否则每点一次
        菜单就多一行，会越点越长（蜈蚣效应）。
        """
        try:
            import asset_catalog as AC
        except Exception as e:
            self._asset_clear_menu()
            self.asset_menu.addAction(Action(
                FluentIcon.INFO, "资产清单模块缺失: %s" % e))
            return
        self._asset_clear_menu()
        base = V2
        try:
            miss = {a["id"] for a in AC.missing_assets(base)}
        except Exception:
            miss = set()

        # task-15 / task-18：档位行只读 _TIER_CACHE，**绝不在菜单重建路径上
        # import torch / socket**。首次无缓存显示"正在检测档位…"并触发一次
        # 后台预热（_start_tier_warmup 去重，预热中不重复 spawn）；预热完成
        # 后下次打开菜单自然显示真实档位。
        try:
            tier = _tier_display()
            if tier:
                self.asset_menu.addAction(Action(
                    FluentIcon.UPDATE, "当前拆图档位：%s" % tier))
                self.asset_menu.addSeparator()
            else:
                self.asset_menu.addAction(Action(
                    FluentIcon.UPDATE, "正在检测档位…"))
                self.asset_menu.addSeparator()
                _start_tier_warmup()
        except Exception:
            pass

        # 运行环境（最影响"装完能不能打开"）
        self.asset_menu.addAction(Action(FluentIcon.DEVELOPER_TOOLS, "— 运行环境 —"))
        for a in AC.ASSETS:
            if (a.get("kind") or "") == "runtime":
                self._add_asset_action(a, a.get("id") not in miss)
        self.asset_menu.addSeparator()
        # 模型
        self.asset_menu.addAction(Action(FluentIcon.FOLDER, "— 模型 —"))
        for a in AC.ASSETS:
            if (a.get("kind") or "model") not in ("runtime", "accel"):
                self._add_asset_action(a, a.get("id") not in miss)
        # 推理加速（CUDA 分卷包，N 卡用户提速）
        accel = [a for a in AC.ASSETS if (a.get("kind") or "") == "accel"]
        if accel:
            self.asset_menu.addSeparator()
            self.asset_menu.addAction(
                Action(FluentIcon.UP, "— 推理加速 —"))
            for a in accel:
                self._add_asset_action(a, a.get("id") not in miss)
        self.asset_menu.addSeparator()
        self.asset_menu.addAction(Action(
            FluentIcon.SYNC, "刷新状态",
            triggered=self._rebuild_asset_menu))

    def _add_asset_action(self, a, done, menu=None):
        """往指定菜单加一条资产项：已装显示 ✔+占用，未装显示 ⬇+体积并可点击下载。"""
        import asset_catalog as AC
        menu = menu if menu is not None else self.asset_menu
        mb = a.get("size", 0) / (1024.0 * 1024.0)
        if done:
            try:
                got = AC.installed_size(a, V2) / (1024.0 * 1024.0)
            except Exception:
                got = mb
            text = "✔ %s（已下载 %.0f MB）" % (a.get("name", a["id"]), got)
            menu.addAction(Action(FluentIcon.ACCEPT, text))
        else:
            text = "⬇ %s（%.0f MB）" % (a.get("name", a["id"]), mb)
            act = Action(FluentIcon.DOWNLOAD, text,
                         triggered=lambda _=False, x=a: self._start_asset_download(x))
            menu.addAction(act)

    def _start_asset_download(self, a):
        """启动资产下载（后台线程）。下载器在 asset_downloader.py，
        不在主线程做网络：5GB 会卡死 UI。"""
        try:
            import asset_downloader as AD
        except Exception as e:
            InfoBar.warning("资产下载", "下载模块缺失: %s" % e, parent=self,
                            position=InfoBarPosition.TOP, duration=4000)
            return
        url = a.get("url") or ""
        # parts 资产（如 CUDA 加速包）的链接在 parts[i]["url"]，顶层无 url 字段，
        # 不能只认 a["url"]——否则 parts 资产会被误挡在「还没有配置下载地址」。
        if not url and not a.get("parts"):
            InfoBar.warning("资产下载",
                            "%s 还没有配置下载地址" % a.get("name", a["id"]),
                            parent=self, position=InfoBarPosition.TOP,
                            duration=4000)
            return
        dlg = AD.AssetDownloadDialog(a, V2, self)
        try:
            dlg.start()
            dlg.exec()
        except Exception as e:
            InfoBar.warning("资产下载", "启动失败: %s" % e, parent=self,
                            position=InfoBarPosition.TOP, duration=4000)


# ---------------- 工作台页 ----------------
class Workbench(AssetMenuMixin, QWidget):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.rig = None
        self.layers = []
        self.W = self.H = 0
        self.visible = {}
        self.locked = {}               # 图层名 -> 是否锁定（PS 锁标记）
        self.history = History()
        self.worker = None
        self.busy = False
        self._mv_orig = None          # 移动工具按下时的原始 bbox
        self._selected = None         # 当前选中图层名（列表/画布共用）
        self._new_canvas = None       # 「新建」弹窗指定的画布尺寸 (W,H)，首张拖入图消费
        # 选区状态（唯一真源在此，画布只负责绘制）
        self._selection_data = []     # [{"points":[...], "op":"add"/"sub"}]
        self._selection_active = False
        self._selection_invert = False   # 反选（Ctrl+Shift+I）：选区=多边形之外
        # 自由变换（Ctrl+T）
        self._ft_layer = None         # 正在变换的图层名
        self._ft_orig = None          # {"img","bbox"} 原图层备份（warp 源）
        self._ft_spin_syncing = False # 选项栏回填时防死循环
        self.proj_name = None         # 当前工程名（新建/载入时写回）
        self.proj_dir = None          # 当前工程目录（新建时指向 projects\<名>\）
        self._build()
        # 启动即空工作台（不自动载入任何工程）
        self.cur_dir = None
        self.act_new(False)   # 启动初始化：不弹尺寸对话框

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        # 三列宽度可拖：QSplitter 原生分隔条（1px 细线）
        split = QSplitter(Qt.Horizontal, self)
        split.setHandleWidth(1)
        split.setChildrenCollapsible(False)
        split.setStyleSheet(SPLIT_QSS)
        root.addWidget(split)

        # ===== v2.11.0 时间轴（底部面板）=====
        # 结构：root(H) -> _outer(QWidget, V) -> [split(1), timeline(0)]
        # 注意：TimelinePanel 的父必须是 _outer 而不是 self，
        # 否则会被 Workbench 直接管理、脱离 _outer 布局（曾导致真实窗口看不到）。
        self.anim_panel = None
        try:
            _outer = QWidget(self)
            _outer.setObjectName("workbenchMain")
            _ol = QVBoxLayout(_outer)
            _ol.setContentsMargins(0, 0, 0, 0)
            _ol.setSpacing(4)
            root.removeWidget(split)
            _ol.addWidget(split, 1)
            self.anim_panel = TimelinePanel(_outer, wb=self)
            self.anim_panel.seeked.connect(self._on_timeline_seek)
            self.anim_panel.keyframes_changed.connect(self._on_keyframes_changed)
            self.anim_panel.duration_changed.connect(self._on_duration_changed)
            self.anim_panel.fps_changed.connect(self._on_fps_changed)
            _ol.addWidget(self.anim_panel, 0)
            root.addWidget(_outer)
        except Exception:
            # 时间轴构建失败不能挡住主界面
            import traceback
            traceback.print_exc()
            if split.parent() is None or split.parent() is not self:
                pass
            root.addWidget(split)
            self.anim_panel = None

        # 菜单/导出按钮（临时父容器，MainWindow 会把它们插进标题栏）
        self.toolbar_widget = QWidget()
        tb_lay = QHBoxLayout(self.toolbar_widget)
        tb_lay.setContentsMargins(0, 0, 0, 0)
        tb_lay.setSpacing(6)
        menu = ToggleDropDownButton("文件", self)
        m = RoundMenu(parent=self)
        act_new = Action(FluentIcon.ADD, "新建", triggered=lambda: self.act_new(True))
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
        em.addSeparator()
        # ===== 选区操作（PS 风格）=====
        # 复制 / 剪切 / 反选 需要激活的选区（菜单打开时按选区状态启用/禁用）；
        # 粘贴只要有剪贴板图像就可用（不依赖选区）。
        self.act_copy = Action(FluentIcon.COPY, "复制  Ctrl+C",
                               triggered=lambda: self.act_copy_selection(False))
        self.act_cut = Action(FluentIcon.CUT, "剪切  Ctrl+X",
                              triggered=lambda: self.act_copy_selection(True))
        self.act_paste = Action(FluentIcon.PASTE, "粘贴  Ctrl+V",
                                triggered=self.act_paste_selection)
        self.act_invert = Action(FluentIcon.SYNC, "反选  Ctrl+Shift+I",
                                 triggered=self.act_invert_selection)
        self.act_deselect = Action(FluentIcon.CANCEL, "取消选择  Ctrl+D",
                                   triggered=self.act_deselect_selection)
        for _a in (self.act_copy, self.act_cut, self.act_invert, self.act_deselect):
            _a.setEnabled(False)          # 默认禁用，菜单打开时按选区状态刷新
        for _a in (self.act_copy, self.act_cut, self.act_paste, self.act_invert,
                   self.act_deselect):
            em.addAction(_a)
        em.addSeparator()
        # 图层补洞（Mocha Remove 简化版）：填充当前层的透明洞
        self.act_inpaint = Action(FluentIcon.BROOM, "图层补洞",
                                  triggered=self.act_inpaint_layer)
        self.act_inpaint.setEnabled(False)   # 需要选中图层
        em.addAction(self.act_inpaint)
        em.addSeparator()
        # 自由变换（PS Ctrl+T）：需要选中图层
        self.act_free_transform_item = Action(
            FluentIcon.ROTATE, "自由变换  Ctrl+T",
            triggered=self.act_free_transform)
        self.act_free_transform_item.setEnabled(False)
        em.addAction(self.act_free_transform_item)
        em.addSeparator()
        # 拆图入口（此前按钮删除后无入口；无 CUDA 时点击给 A 卡路线引导）
        em.addAction(Action(FluentIcon.ZOOM, "拆分图片（SeeThrough）",
                            triggered=self._pick_image))
        edit.setMenu(em)
        # 菜单每次打开都按当前选区状态刷新可用性（PS 里没选区时复制/剪切是灰的）
        edit.menu_opened.connect(self._sync_edit_menu_state)
        self.edit_btn = edit
        self.edit_ref = em

        # ===== 资产管理（编辑右侧）：运行时 / 模型 的下载与状态 =====
        # 安装包不再下载模型，装完即可打开；缺什么在这里按需下载。
        # 按钮/菜单构建与逻辑在 AssetMenuMixin，MainWindow 与视频窗壳共用。
        self._setup_asset_menu()
        am = self.asset_btn
        menu.setFixedHeight(30)
        edit.setFixedHeight(30)
        self.asset_btn.setFixedHeight(30)
        tb_lay.addWidget(menu)
        tb_lay.addWidget(edit)

        # 兜底：下拉菜单打开期间轮询光标，移出菜单矩形 250ms 即收起。
        # 状态全部由按钮自己记账（menu_opened / aboutToHide），不依赖 isVisible 时机。
        self._dropdowns = [(menu, m), (edit, em), (am, self.asset_menu)]     # 看门狗管理所有下拉按钮
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
        self.canvas.lasso_committed.connect(self._on_lasso_committed)
        self.canvas.selection_changed.connect(self._on_selection_changed)
        self.canvas.ft_committed.connect(self._on_ft_committed)
        # 拖动中：既回填选项栏数字，也重渲染画面（实时预览 warp 后的图层）
        self.canvas.ft_changed.connect(self._on_ft_changed)
        self.canvas.ft_cancelled.connect(self._on_ft_cancelled)
        # 工具互斥：移动 / 多边形套索。
        # **不用 QButtonGroup 的 exclusive 模式**——它不允许取消已选中的按钮
        # （setChecked(False) 被吞），会破坏"取消移动工具回到左键拖支点"的行为。
        # 改为自己维护：点谁就取消其它。
        self.tool_bar = QWidget()
        _tbl = QVBoxLayout(self.tool_bar)
        _tbl.setContentsMargins(4, 6, 4, 6)
        _tbl.setSpacing(4)
        self.btn_move = TransparentToggleToolButton(FluentIcon.MOVE, self)
        self.btn_move.setToolTip("移动工具：在画布上拖动图层（Ctrl+Z 撤销）。\n"
                                 "未选中时左键仍用于拖动骨架支点。")
        # 默认不选中：保持原有"左键拖支点"行为，点按钮才进入移动模式
        self.btn_move.toggled.connect(self._on_tool_toggled)
        _tbl.addWidget(self.btn_move)
        # 多边形套索
        self.btn_lasso = TransparentToggleToolButton(make_lasso_icon(), self)
        self.btn_lasso.setToolTip("多边形套索：左键逐点圈选，双击(或 Enter)闭合，"
                                  "把选中的图层裁剪成只剩选区内像素（Esc 取消）。")
        self.btn_lasso.toggled.connect(self._on_tool_toggled)
        _tbl.addWidget(self.btn_lasso)
        _tbl.addStretch(1)
        self.tool_bar.setFixedWidth(44)
        self.tool_bar.setStyleSheet(
            "background:#252a33; border-radius:8px;")

        # ===== 画布上方选项栏（PS 风格）：按模式显示不同控件组 =====
        # 套索模式 -> 羽化/曲线/重新应用；自由变换模式 -> 宽/高/角度/提交/取消。
        # 两者都不激活时整条隐藏（setVisible），不占空间。
        self.canvas_options = QWidget()
        _opl = QHBoxLayout(self.canvas_options)
        _opl.setContentsMargins(8, 3, 8, 3)
        _opl.setSpacing(6)
        self.lasso_feather = 0
        # ---- 套索控件组 ----
        self.opt_lasso_group = QWidget(self.canvas_options)
        _olg = QHBoxLayout(self.opt_lasso_group)
        _olg.setContentsMargins(0, 0, 0, 0)
        _olg.setSpacing(6)
        _olg.addWidget(CaptionLabel("羽化", self.opt_lasso_group))
        self.spin_feather = NumSpinBox(minimum=0, maximum=20, value=0,
                                       parent=self.opt_lasso_group)
        self.spin_feather.setToolTip("羽化半径（像素）：选区边缘软化，0=硬边")
        self.spin_feather.setFixedWidth(96)
        # 高度不能固定：内部 editor 高 33，setFixedHeight(26) 会把它压扁错位
        self.spin_feather.setMinimumHeight(33)
        self.spin_feather.setMaximumHeight(33)
        self.spin_feather.valueChanged.connect(
            lambda v: setattr(self, "lasso_feather", int(v)))
        _olg.addWidget(self.spin_feather)
        _olg.addWidget(CaptionLabel("px", self.opt_lasso_group))
        # 曲线模式（PS 曲线套索）：控制点之间用 Catmull-Rom 样条平滑
        self.btn_curve = TransparentToggleToolButton(make_curve_icon(),
                                                     self.opt_lasso_group)
        self.btn_curve.setToolTip("曲线套索：闭合后控制点间用样条曲线平滑（少点圈大形）")
        self.btn_curve.setFixedSize(28, 28)
        self.btn_curve.toggled.connect(
            lambda on: setattr(self.canvas, "_lasso_curve", bool(on)))
        _olg.addWidget(self.btn_curve)
        _olg.addStretch(1)
        # 顶点拖动后重新应用选区
        self.btn_lasso_apply = TransparentToolButton(FluentIcon.ACCEPT,
                                                     self.opt_lasso_group)
        self.btn_lasso_apply.setToolTip("重新应用选区（拖动顶点调整形状后点此）")
        self.btn_lasso_apply.setFixedSize(24, 24)
        self.btn_lasso_apply.clicked.connect(lambda: self.canvas.lasso_reapply())
        _olg.addWidget(self.btn_lasso_apply)
        _opl.addWidget(self.opt_lasso_group)
        # ---- 自由变换控件组（宽/高/角度 + 提交/取消）----
        self.opt_ft_group = QWidget(self.canvas_options)
        _ofg = QHBoxLayout(self.opt_ft_group)
        _ofg.setContentsMargins(0, 0, 0, 0)
        _ofg.setSpacing(6)
        _ofg.addWidget(CaptionLabel("宽", self.opt_ft_group))
        self.spin_ft_w = NumSpinBox(minimum=1, maximum=99999, value=100,
                                    parent=self.opt_ft_group)
        self.spin_ft_w.setFixedWidth(96)
        self.spin_ft_w.setMinimumHeight(33)
        self.spin_ft_w.setMaximumHeight(33)
        self.spin_ft_w.valueChanged.connect(self._on_ft_spin_edited)
        _ofg.addWidget(self.spin_ft_w)
        _ofg.addWidget(CaptionLabel("高", self.opt_ft_group))
        self.spin_ft_h = NumSpinBox(minimum=1, maximum=99999, value=100,
                                    parent=self.opt_ft_group)
        self.spin_ft_h.setFixedWidth(96)
        self.spin_ft_h.setMinimumHeight(33)
        self.spin_ft_h.setMaximumHeight(33)
        self.spin_ft_h.valueChanged.connect(self._on_ft_spin_edited)
        _ofg.addWidget(self.spin_ft_h)
        _ofg.addWidget(CaptionLabel("角度", self.opt_ft_group))
        self.spin_ft_ang = NumSpinBox(minimum=-360, maximum=360, value=0,
                                      parent=self.opt_ft_group)
        self.spin_ft_ang.setFixedWidth(96)
        self.spin_ft_ang.setMinimumHeight(33)
        self.spin_ft_ang.setMaximumHeight(33)
        self.spin_ft_ang.valueChanged.connect(self._on_ft_spin_edited)
        _ofg.addWidget(self.spin_ft_ang)
        _ofg.addWidget(CaptionLabel("°", self.opt_ft_group))
        _ofg.addStretch(1)
        self.btn_ft_cancel = TransparentToolButton(FluentIcon.CANCEL,
                                                   self.opt_ft_group)
        self.btn_ft_cancel.setToolTip("取消自由变换（Esc）")
        self.btn_ft_cancel.setFixedSize(24, 24)
        self.btn_ft_cancel.clicked.connect(lambda: self.canvas.ft_cancel())
        _ofg.addWidget(self.btn_ft_cancel)
        # 网格变形开关（PS 自由变换的"变形"模式）：开启后拖内部格点做局部弯曲
        self.btn_ft_mesh = TransparentToggleToolButton(make_mesh_icon(),
                                                       self.opt_ft_group)
        self.btn_ft_mesh.setToolTip("变形（网格）：开启后拖动内部格点做局部弯曲，"
                                    "适合发丝/布料等不规则形变")
        self.btn_ft_mesh.setFixedSize(28, 28)
        self.btn_ft_mesh.toggled.connect(self.canvas._ft_mesh_toggle)
        _ofg.addWidget(self.btn_ft_mesh)
        self.btn_ft_commit = TransparentToolButton(FluentIcon.ACCEPT,
                                                   self.opt_ft_group)
        self.btn_ft_commit.setToolTip("提交自由变换（Enter）")
        self.btn_ft_commit.setFixedSize(24, 24)
        self.btn_ft_commit.clicked.connect(lambda: self.canvas.ft_commit())
        _ofg.addWidget(self.btn_ft_commit)
        self.opt_ft_group.setVisible(False)
        _opl.addWidget(self.opt_ft_group)
        self.canvas_options.setFixedHeight(38)
        self.canvas_options.setVisible(False)      # 默认隐藏（非套索/变换）

        canvas_row = QHBoxLayout()
        canvas_row.setContentsMargins(0, 0, 0, 0)
        canvas_row.setSpacing(6)
        canvas_row.addWidget(self.tool_bar)
        canvas_row.addWidget(self.canvas, 1)
        leftLay.addWidget(self.canvas_options)   # PS 风格选项栏（默认隐藏）
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
        # 拆分进度条：放在时间标签右侧（原来挤在导航栏，现在归位到画布底栏）
        self.pbar = ProgressBar(self)
        self.pbar.setRange(0, 100)
        self.pbar.setValue(0)
        self.pbar.setFixedHeight(8)
        self.pbar.setMinimumWidth(120)
        self.pbar.setMaximumWidth(220)
        self.pbar.setToolTip("拆分进度")
        self.pbar.setVisible(False)          # 空闲时不占位，拆分时才出现
        row.addWidget(self.pbar)
        self.pbar_lbl = CaptionLabel("")
        self.pbar_lbl.setVisible(False)
        row.addWidget(self.pbar_lbl)
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
        botLay.addWidget(StrongBodyLabel("图层"))
        self.layer_list = ListWidget()
        self.layer_list.setDragDropMode(ListWidget.InternalMove)
        self.layer_list.setDefaultDropAction(Qt.MoveAction)
        # PS 风格委托：自己画眼睛/缩略图/名称/锁标；编辑只由缩略图双击触发
        self.layer_list.setItemDelegate(LayerItemDelegate(self.layer_list, self))
        self.layer_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.layer_list.model().rowsMoved.connect(self._on_rows_moved)
        self.layer_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.layer_list.customContextMenuRequested.connect(self._layer_menu)
        # 选中层同步：画布移动工具只拖列表当前选中层（currentItem 是唯一真源）
        self.layer_list.currentItemChanged.connect(self._on_current_layer_changed)
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
        # v2.11.0 时间轴设置（替换原来写死的 10s / 30fps）
        self.duration = 4.0
        self.fps = 30

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
        self.locked = {}               # 载入新工程：锁定状态清空
        self.cur_dir = d
        self.proj_name = self._read_proj_name(d)
        self.proj_dir = d
        # v2.11.0：读回关键帧与时间轴设置（老工程没有这些字段 -> 空/默认）
        self._load_anim_settings(d)
        self._record_recent(d)
        self.reload_widgets()
        self.history.seed(self)      # 新不变式：undo_stack 栈底=载入基线
        self._notify_nav(self.proj_name, d)
        return True

    def _read_proj_name(self, d):
        """读工程名：优先 rig_project.json 的 name，否则用目录名。"""
        try:
            p = json.load(open(os.path.join(d, "rig_project.json"), encoding="utf8"))
            nm = sanitize_proj_name(p.get("name") or "")
            if nm:
                return nm
        except Exception:
            pass
        return os.path.basename(os.path.normpath(d))

    # ---------- v2.11.0 关键帧动画：载入/初始状态 ----------
    def _load_anim_settings(self, d):
        """从 rig_project.json 读回 keyframes / duration / fps / 时间轴界面偏好。

        老工程没有这些字段时：keyframes={}（走原来的 sin 动画），
        duration=4.0、fps=30、ui_scale=1.0、panel_h=None（自适应）。
        全程容错，不让脏文件挡住工程打开。
        """
        kf, dur, f = {}, 4.0, 30
        ui_scale, panel_h = 1.0, None
        try:
            p = json.load(open(os.path.join(d, "rig_project.json"), encoding="utf8"))
            if isinstance(p.get("keyframes"), dict):
                kf = A.clone_tracks(p["keyframes"])
            an = p.get("anim")
            if isinstance(an, dict):
                d2 = an.get("duration")
                if isinstance(d2, (int, float)) and 0.2 <= float(d2) <= 600:
                    dur = float(d2)
                f2 = an.get("fps")
                if isinstance(f2, int) and 1 <= f2 <= 120:
                    f = f2
                # v2.11.2 界面偏好
                s2 = an.get("ui_scale")
                if isinstance(s2, (int, float)) and 0.5 <= float(s2) <= 2.0:
                    ui_scale = float(s2)
                h2 = an.get("panel_h")
                if isinstance(h2, (int, float)) and 80 <= float(h2) <= 720:
                    panel_h = int(h2)
        except Exception:
            pass
        self.rig["keyframes"] = kf
        self.duration = dur
        self.fps = f
        panel = getattr(self, "anim_panel", None)
        if panel is not None:
            try:
                # 先恢复界面偏好，再 refresh（refresh 里会按 ui_scale 重算高度）
                panel.set_ui_scale(ui_scale)
                panel._manual_h = panel_h
                panel._resize_panel()
                panel.refresh()
            except Exception:
                pass

    def _notify_nav(self, name, d):
        """把工程加进/刷新导航栏（MainWindow 维护 _proj_items）。"""
        fn = getattr(getattr(self, "win", None), "add_project_entry", None)
        if fn is None:
            return
        try:
            fn(name, d)
        except Exception:
            pass

    def _write_project_placeholder(self, d, name, W, H):
        """写最小工程占位：rig_project.json + 空 layers_meta.json，导航一点就能 load。"""
        os.makedirs(d, exist_ok=True)
        json.dump({"name": name, "width": W, "height": H,
                   "draw_order": [], "nodes": [], "layers": []},
                  open(os.path.join(d, "rig_project.json"), "w", encoding="utf8"),
                  ensure_ascii=False, indent=2)
        json.dump({"width": W, "height": H, "layers": []},
                  open(os.path.join(d, "layers_meta.json"), "w", encoding="utf8"),
                  ensure_ascii=False, indent=2)

    def reload_widgets(self):
        # 图层名索引重建（delegate 靠它 O(1) 查层，避免每次 paint 线性扫）
        self._sync_layer_index()
        # clear 会触发 currentItemChanged 把选中层洗成 None，先屏蔽；
        # 重建列表后按 _selected 恢复选中（保持"列表选中=画布可拖层"一致）
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        for l in self.layers:
            # 委托负责绘制眼睛/缩略图/tag；item 文本只作可访问性兜底
            it = QListWidgetItem(l["name"])
            it.setData(Qt.UserRole, l["name"])
            self.layer_list.addItem(it)
        if self._selected:
            for i in range(self.layer_list.count()):
                it = self.layer_list.item(i)
                if it is not None and it.data(Qt.UserRole) == self._selected:
                    self.layer_list.setCurrentItem(it)
                    break
        self.layer_list.blockSignals(False)
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
        # 移动工具命中测试数据：列表顺序=前为叠放顶，与视觉一致；
        # 第三元素 (visible, locked)：锁定层不可被画布命中
        self.canvas.set_layers([(l["name"], l["bbox"],
                                 (self.visible.get(l["name"], True),
                                  self.locked.get(l["name"], False)))
                                for l in self.layers])
        # 自由变换实时预览：临时把变换层换成 warp 结果渲染，渲染后恢复。
        # 复用 render_frame_rgba 的合成逻辑（depth/叠放顺序全部一致），
        # 拖动中每帧 warp 一次 -> 画面跟着框走，而不是只更新选项栏数字。
        _ft_restore = None
        if self.canvas.ft_active():
            try:
                _ft_restore = self._apply_ft_preview()
            except Exception:
                _ft_restore = None
        # 播放走半分辨率 LOD + 预乘直出（省出口除法，~30ms/帧），透明区画布显示棋盘格
        try:
            img = C.render_frame_rgba(self.rig, self.t, hidden=hidden,
                                      scale=PREVIEW_SCALE, premul=True)
        finally:
            # 无论渲染是否异常都必须恢复：预览只借道渲染，绝不污染图层数据
            if _ft_restore is not None:
                self._restore_ft_preview(_ft_restore)
        sc = PREVIEW_SCALE
        piv = {n["node"]: (n["pivot"][0] * sc, n["pivot"][1] * sc)
               for n in self.rig["nodes"] if n["node"] != "root"}
        self.canvas.set_frame(img, piv)

    def _apply_ft_preview(self):
        """把正在变换的层临时替换成 warp 预览结果。返回恢复用的快照，失败返回 None。"""
        import transform_tool as TT
        name = getattr(self, "_ft_layer", None)
        orig = getattr(self, "_ft_orig", None)
        st = getattr(self.canvas, "_ft", None)
        if not name or orig is None or st is None:
            return None
        layer = self.layer_by_name(name)
        if layer is None:
            return None
        # 网格模式用 warp_mesh（分段双线性，能弯曲）；否则 warp_state（整体仿射）
        if getattr(self.canvas, "_ft_mesh_on", False):
            res = TT.warp_mesh(orig["img"], orig["bbox"], st)
        else:
            res = TT.warp_state(orig["img"], orig["bbox"], st)
        if res is None:
            return None
        new_img, new_bbox = res
        snap = {"layer": layer, "img": layer["img"], "bbox": layer["bbox"]}
        layer["img"] = new_img
        layer["bbox"] = new_bbox
        self.rig.pop("_lite", None)          # 预览期每帧作废 LOD（否则用旧尺寸）
        return snap

    def _restore_ft_preview(self, snap):
        """恢复 _apply_ft_preview 临时改动的层（渲染后立刻调用，图层数据不被污染）"""
        layer = snap["layer"]
        layer["img"] = snap["img"]
        layer["bbox"] = snap["bbox"]
        self.rig.pop("_lite", None)

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
    def _on_tool_toggled(self, checked):
        """工具按钮切换：手动维护互斥（不用 QButtonGroup 的 exclusive，
        它会吞掉 setChecked(False)、导致无法取消工具回到支点拖拽）。
        画布上方的选项栏（羽化等）只在套索工具激活时显示（PS 风格）。"""
        btn = self.sender()
        if checked:
            for other in (self.btn_move, self.btn_lasso):
                if other is not btn and other.isChecked():
                    other.blockSignals(True)
                    other.setChecked(False)
                    other.blockSignals(False)
            self.canvas.set_tool("lasso" if btn is self.btn_lasso else "move")
        else:
            # 取消选中 -> 回到默认（支点拖拽）
            if not (self.btn_move.isChecked() or self.btn_lasso.isChecked()):
                self.canvas.set_tool("pan")
        self._sync_canvas_options()

    def _sync_canvas_options(self):
        """画布上方选项栏：套索模式显示羽化/曲线组，自由变换模式显示宽高角组。
        两者都不激活时整条隐藏。自由变换优先（激活时套索组让位）。"""
        try:
            ft = self.canvas.ft_active()
            lasso = (self.canvas.tool == "lasso") and not ft
            self.canvas_options.setVisible(ft or lasso)
            self.opt_ft_group.setVisible(ft)
            self.opt_lasso_group.setVisible(lasso)
            if ft:
                self._update_ft_spins()
        except Exception:
            pass

    def _update_ft_spins(self):
        """把当前变换状态回填到宽/高/角度输入框（拖动控制点时实时刷新）。
        _ft_spin_syncing 防止 spin 与画布互相触发死循环。"""
        st = getattr(self.canvas, "_ft", None)
        if st is None:
            return
        self._ft_spin_syncing = True
        try:
            self.spin_ft_w.setValue(max(1, int(round(st["hw"] * 2))))
            self.spin_ft_h.setValue(max(1, int(round(st["hh"] * 2))))
            self.spin_ft_ang.setValue(int(round(math.degrees(st["ang"]))) % 360)
        finally:
            self._ft_spin_syncing = False

    def _on_ft_changed(self, st):
        """拖动控制点/移动/旋转：回填选项栏数字 + 重渲染画面（实时预览）。"""
        self._update_ft_spins()
        self.redraw()

    def _on_ft_spin_edited(self, *args):
        """宽/高/角度输入框改变 -> 反算变换状态（PS 在选项栏输入数值）。
        以图层原始 bbox 为基准（未变换时的宽高），避免多次输入累积漂移。"""
        if getattr(self, "_ft_spin_syncing", False):
            return
        st = getattr(self.canvas, "_ft", None)
        if st is None:
            return
        import transform_tool as TT
        orig = getattr(self, "_ft_orig", None)
        if orig is None:
            return
        ob = orig["bbox"]
        ow, ohh = ob[2] - ob[0], ob[3] - ob[1]
        ang = math.radians(self.spin_ft_ang.value())
        hw = max(1.0, self.spin_ft_w.value() / 2.0)
        hh = max(1.0, self.spin_ft_h.value() / 2.0)
        self.canvas._ft = {"cx": st["cx"], "cy": st["cy"],
                           "hw": hw, "hh": hh, "ang": ang}
        self.canvas.update()
        self.redraw()          # 输入数值也要实时预览（与拖动控制点一致）

    # ---------- 选区：复制 / 剪切 / 粘贴（PS 风格）----------
    def _selection_layer(self):
        """当前选区所属的图层。
        选区状态由 Workbench 持有（self._selection_active / self._selection_data），
        画布只负责画 —— 两边各存一份会导致状态不同步（曾导致复制无效）。"""
        if not getattr(self, "_selection_active", False):
            return None
        sels = getattr(self, "_selection_data", None)
        if not sels:
            return None
        name = self._selected
        if not name:
            return None
        return self.layer_by_name(name)

    def _on_selection_changed(self, selections):
        """画布选区变化 -> Workbench 持有（选区状态的唯一真源）。

        选区激活后不立即裁剪：用户可继续加减选、拖顶点，
        然后 Ctrl+C/X 复制剪切、Ctrl+V 粘贴、Delete/Enter 才真正裁剪。
        """
        self._selection_data = list(selections or [])
        self._selection_active = bool(self._selection_data)
        if not self._selection_active:
            self._selection_invert = False      # 选区没了，反选标记也清掉
        # 同步画布（画布用它画选区边框）
        self.canvas._lasso_done = list(self._selection_data)
        self.canvas._selection_active = self._selection_active
        self.canvas._selection_invert = self._selection_invert
        self.canvas.update()
        self._sync_canvas_options()
        self._sync_edit_menu_state()

    def _clear_selection(self):
        """清除选区状态（复制/剪切后、应用后、Esc）"""
        self._selection_data = []
        self._selection_active = False
        self._selection_invert = False
        self.canvas.lasso_cancel_selection()
        self._sync_edit_menu_state()

    def _sync_edit_menu_state(self):
        """刷新「编辑」菜单里选区相关项的可用性（PS：没选区时复制/剪切/反选是灰的）。"""
        try:
            has_sel = (bool(getattr(self, "_selection_active", False))
                       and bool(getattr(self, "_selection_data", None)))
            for a in (getattr(self, "act_copy", None),
                      getattr(self, "act_cut", None),
                      getattr(self, "act_invert", None),
                      getattr(self, "act_deselect", None)):
                if a is not None:
                    a.setEnabled(has_sel)
            p = getattr(self, "act_paste", None)
            if p is not None:
                md = QApplication.clipboard().mimeData()
                ok = bool(md and md.hasImage())
                p.setEnabled(ok)
            # 图层补洞：需要选中图层且该层有透明洞（bbox 范围内）
            ip = getattr(self, "act_inpaint", None)
            if ip is not None:
                layer = self.layer_by_name(self._selected) if self._selected else None
                ok2 = False
                if layer is not None and not self.locked.get(layer["name"]):
                    a = layer["img"][:, :, 3]
                    # 有"被包围的洞"才算可补（bbox 角上的外部透明不算洞）
                    ok2 = find_enclosed_holes(a) is not None
                ip.setEnabled(ok2)
            # 自由变换：需要选中图层（未锁定、有 img）；已在变换中则禁用（避免套娃）
            ft = getattr(self, "act_free_transform_item", None)
            if ft is not None:
                layer = self.layer_by_name(self._selected) if self._selected else None
                ok3 = (layer is not None and layer.get("img") is not None
                       and not self.locked.get(layer["name"])
                       and not self.canvas.ft_active())
                ft.setEnabled(ok3)
        except Exception:
            pass

    def act_deselect_selection(self):
        """取消选择（Ctrl+D，PS 的选择 > 取消选择）"""
        if not getattr(self, "_selection_active", False):
            return
        self._clear_selection()
        InfoBar.info("取消选择", "选区已清除", parent=self,
                     position=InfoBarPosition.TOP, duration=1500)

    def act_inpaint_layer(self):
        """图层补洞：填充当前选中图层的透明洞（Mocha Remove 简化版）。

        洞 = 图层 bbox 内 alpha==0 但被不透明像素包围的区域。
        实现：cv2.morphologyEx 闭运算找到"被包围的洞" -> cv2.inpaint 填 RGB
        -> 洞内 alpha 置 255。bbox 外的大片透明不算洞（不填）。
        """
        if self.rig is None:
            return
        name = self._selected
        if not name:
            InfoBar.info("图层补洞", "请先选中一个图层", parent=self,
                         position=InfoBarPosition.TOP, duration=2000)
            return
        layer = self.layer_by_name(name)
        if layer is None:
            return
        if self.locked.get(name):
            InfoBar.warning("图层已锁定", "「%s」已锁定，无法补洞" % name, parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
            return
        import lasso_tool
        img = layer.get("img")
        if img is None or getattr(img, "ndim", 0) != 3:
            return
        holes = find_enclosed_holes(img[:, :, 3])
        if holes is None:
            InfoBar.info("图层补洞", "该图层没有被包围的透明洞", parent=self,
                         position=InfoBarPosition.TOP, duration=2000)
            return
        hole_mask, hole_area, hole_cnt = holes
        new_rgba = lasso_tool.inpaint_holes(img, radius=3, holes=hole_mask)
        newL = dict(layer)
        newL["img"] = new_rgba
        # 替换 layers / rig['_layers']（新 dict，历史快照才能正确撤销）
        for arr in (self.layers, self.rig.get("_layers", [])):
            for i, x in enumerate(arr):
                if x["name"] == name:
                    arr[i] = newL
        if self.rig.get("_layers") is not self.layers:
            self.rig["_layers"] = [newL if x is layer else x
                                    for x in self.rig.get("_layers", [])]
        self.rig.pop("_lite", None)
        self._sync_layer_index()
        self.history.push(self)
        self.reload_widgets()
        self._select_in_list(name)
        InfoBar.success("图层补洞", "%s：填充了 %d 个洞（%d 像素）" % (
            name, hole_cnt, hole_area), parent=self,
            position=InfoBarPosition.TOP, duration=2500)

    def _replace_layer(self, name, newL):
        """把 layers / rig['_layers'] 里名为 name 的层换成 newL（新 dict）。

        历史快照存的是**对象引用**，原地改 img/bbox 会让旧快照看到新内容、
        撤销失效 —— 所以所有像素级/几何级编辑都必须用新 dict 替换（同一套路）。
        """
        for arr in (self.layers, self.rig.get("_layers", [])):
            for i, x in enumerate(arr):
                if x["name"] == name:
                    arr[i] = newL
        if self.rig.get("_layers") is not self.layers:
            self.rig["_layers"] = [newL if x["name"] == name else x
                                    for x in self.rig.get("_layers", [])]
        self.rig.pop("_lite", None)          # LOD 缓存作废
        self._sync_layer_index()

    def act_free_transform(self):
        """自由变换（Ctrl+T，PS 的编辑 > 自由变换）。

        对**当前选中图层**开启交互式变换框：拖 8 个控制点缩放（Shift 等比）、
        框内拖动=移动、框角外拖动=旋转；Enter/双击提交，Esc 取消。
        提交=用 cv2.warpAffine 重采样图层像素并收紧 bbox（引擎零改动）。
        """
        if self.rig is None:
            return
        if self.canvas.ft_active():
            return                              # 已在变换中：不重复进入
        name = self._selected
        if not name:
            InfoBar.info("自由变换", "请先在图层面板选中一个图层", parent=self,
                         position=InfoBarPosition.TOP, duration=2500)
            return
        layer = self.layer_by_name(name)
        if layer is None or layer.get("img") is None:
            return
        if self.locked.get(name):
            InfoBar.warning("图层已锁定", "「%s」已锁定，无法变换" % name, parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
            return
        self._ft_layer = name
        self._ft_orig = {"img": layer["img"], "bbox": tuple(layer["bbox"])}
        self.canvas.ft_begin(layer["bbox"])
        self.redraw()                      # 进入变换：渲染一次（此后拖动实时预览）
        self._sync_canvas_options()
        self._sync_edit_menu_state()
        InfoBar.info("自由变换", "%s：拖控制点缩放(Shift 等比)/框内移动/角外旋转，"
                     "Enter 提交、Esc 取消" % name, parent=self,
                     position=InfoBarPosition.TOP, duration=3200)

    def _on_ft_committed(self, st):
        """变换提交：warp 原图层 -> 新 img/bbox -> 替换 + 压历史。"""
        name = getattr(self, "_ft_layer", None)
        orig = getattr(self, "_ft_orig", None)
        if not name or orig is None:
            return
        import transform_tool as TT
        # 与预览同一套：网格模式走 warp_mesh，否则 warp_state
        # （预览与提交必须同算法，否则"所见非所得"）
        if getattr(self.canvas, "_ft_mesh_on", False):
            res = TT.warp_mesh(orig["img"], orig["bbox"], st)
        else:
            res = TT.warp_state(orig["img"], orig["bbox"], st)
        if res is None:
            InfoBar.warning("自由变换", "变换结果退化（过小或空），未应用", parent=self,
                            position=InfoBarPosition.TOP, duration=3000)
            self._ft_layer = None
            self._ft_orig = None
            self._sync_canvas_options()
            return
        new_img, new_bbox = res
        layer = self.layer_by_name(name)
        newL = dict(layer) if layer is not None else {}
        newL["name"] = name
        newL["img"] = new_img
        newL["bbox"] = new_bbox
        # 保留原图备份（与套索裁剪同一套「还原原图」机制）
        if layer is not None and "_orig_img" not in layer:
            newL["_orig_img"] = orig["img"]
            newL["_orig_bbox"] = tuple(orig["bbox"])
        self._replace_layer(name, newL)
        self.history.push(self)
        self.reload_widgets()
        self._select_in_list(name)
        self._ft_layer = None
        self._ft_orig = None
        self._sync_canvas_options()
        InfoBar.success("已变换", "%s：%d×%d" % (name, new_bbox[2]-new_bbox[0],
                                                 new_bbox[3]-new_bbox[1]),
                        parent=self, position=InfoBarPosition.TOP, duration=2200)

    def _on_ft_cancelled(self):
        """变换取消：图层未动，只清状态 + 重渲染（画面从预览帧切回原图）"""
        self._ft_layer = None
        self._ft_orig = None
        self.redraw()                      # ft_active 已 False -> 渲染未改动的原图层
        self._sync_canvas_options()
        self._sync_edit_menu_state()

    def act_invert_selection(self):
        """反选（Ctrl+Shift+I，PS 的"选择 > 反向"）。

        反转后：选区变成「多边形之外的全部区域」——复制/剪切/应用裁剪都会
        作用在未被圈住的部分。需要先有激活的选区。
        """
        if not getattr(self, "_selection_active", False) or not self._selection_data:
            InfoBar.info("反选", "没有激活的选区（先用套索工具圈选并闭合）",
                         parent=self, position=InfoBarPosition.TOP, duration=2500)
            self._sync_edit_menu_state()
            return
        self._selection_invert = not getattr(self, "_selection_invert", False)
        self.canvas._selection_invert = self._selection_invert
        self.canvas.update()
        self._sync_canvas_options()
        InfoBar.success("已反选" if self._selection_invert else "已取消反选",
                        "选区 = 多边形外全部区域" if self._selection_invert
                        else "选区 = 多边形内区域",
                        parent=self, position=InfoBarPosition.TOP, duration=2200)

    def act_copy_selection(self, cut=False):
        """Ctrl+C / Ctrl+X：把选区内容复制到系统剪贴板（cut=True 则同时从原层抠掉）。"""
        # 正在画但没闭合 -> 自动闭合（用户直接按 Ctrl+C 的常见情形）
        if len(getattr(self.canvas, "_lasso", []) or []) >= 3:
            self.canvas._commit_lasso()
            app = QApplication.instance()
            if app is not None:
                app.processEvents()
        layer = self._selection_layer()
        if layer is None:
            if not self._selected:
                InfoBar.info("复制", "请先在图层面板选中一个图层", parent=self,
                             position=InfoBarPosition.TOP, duration=2500)
            else:
                InfoBar.info("复制", "没有激活的选区（先用套索工具圈选并双击闭合）",
                             parent=self, position=InfoBarPosition.TOP, duration=2500)
            return
        import lasso_tool
        sels = self._selection_data
        res = lasso_tool.extract_selection(layer, sels, feather=self.lasso_feather,
                                            invert=getattr(self, "_selection_invert", False))
        if res is None:
            InfoBar.warning("复制", "选区与图层没有重叠", parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
            return
        rgba, px, py = res
        # PNG 进剪贴板（可粘到其它程序）；另用自定义 MIME 记住图层元信息
        from PySide6.QtGui import QImage
        from PySide6.QtCore import QMimeData, QByteArray
        h, w = rgba.shape[:2]
        arr = np.ascontiguousarray(rgba[:, :, [2, 1, 0, 3]])   # RGBA->BGRA
        qimg = QImage(arr.data, w, h, 4 * w, QImage.Format.Format_RGBA8888)
        mime = QMimeData()
        mime.setImageData(qimg)
        meta = {"tag": layer.get("tag", "Image"), "node": layer.get("node", "other"),
                "depth": layer.get("depth", 0.1), "w": int(w), "h": int(h),
                "x": int(px), "y": int(py)}
        mime.setData("application/x-muppet-layer",
                     QByteArray(json.dumps(meta).encode("utf8")))
        QApplication.clipboard().setMimeData(mime)
        # 剪切：**移除选中的部分**（保留选区外的），选区内容进剪贴板
        # —— PS 语义。注意与"应用选区裁剪"方向相反：那个是只保留选区内。
        if cut:
            mask = self._selection_mask(layer)
            # 选区落在空白处 -> 没有可剪切的内容，明确提示
            if mask is not None and not ((layer["img"][:, :, 3] > 0) & (mask > 0)).any():
                InfoBar.warning("剪切", "选区落在图层的空白处，没有可剪切的内容",
                                parent=self, position=InfoBarPosition.TOP, duration=3000)
                return
            newL = (lasso_tool.clip_layer_by_mask(layer, mask, invert=True)
                    if mask is not None else None)
            if newL is not None:
                for arr2 in (self.layers, self.rig.get("_layers", [])):
                    for i, x in enumerate(arr2):
                        if x["name"] == layer["name"]:
                            arr2[i] = newL
                if self.rig.get("_layers") is not self.layers:
                    self.rig["_layers"] = [newL if x is layer else x
                                            for x in self.rig.get("_layers", [])]
                self.rig.pop("_lite", None)
                self._sync_layer_index()
                self.reload_widgets()
                self._select_in_list(layer["name"])
        self._clear_selection()
        self.history.push(self)
        InfoBar.success("已剪切" if cut else "已复制",
                        "选区内容 %dx%d" % (w, h), parent=self,
                        position=InfoBarPosition.TOP, duration=2000)

    def _selection_mask(self, layer):
        """当前选区在指定图层坐标系里的掩码（0..255）"""
        import lasso_tool
        bbox = layer.get("bbox")
        if not bbox:
            return None
        x0, y0, x1, y1 = [int(v) for v in bbox]
        h, w = layer["img"].shape[:2]
        add_masks, sub_masks = [], []
        for sel in self._selection_data or []:
            pts = lasso_tool.selection_points(sel)
            op = sel.get("op", "add")
            local = [(px - x0, py - y0) for px, py in pts]
            m = lasso_tool.polygon_mask(local, w, h)
            if m is None:
                continue
            (sub_masks if op == "sub" else add_masks).append(m)
        return lasso_tool.combine_masks(add_masks, sub_masks, w, h,
                                        feather=self.lasso_feather,
                                        invert=getattr(self, "_selection_invert", False))

    def act_paste_selection(self):
        """Ctrl+V：把剪贴板里的选区内容作为**顶部新图层**插入。"""
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            return
        meta = {}
        raw = mime.data("application/x-muppet-layer")
        if raw:
            try:
                meta = json.loads(bytes(raw.data()).decode("utf8"))
            except Exception:
                meta = {}
        img = mime.imageData()
        if img is None or img.isNull():
            InfoBar.info("粘贴", "剪贴板里没有图像", parent=self,
                         position=InfoBarPosition.TOP, duration=2000)
            return
        img = img.convertToFormat(QImage.Format.Format_RGBA8888)
        w, h = img.width(), img.height()
        ptr = img.constBits()
        buf = np.frombuffer(ptr, np.uint8, img.sizeInBytes()).reshape(
            h, img.bytesPerLine() // 4, 4)[:, :w].copy()
        rgba = buf[:, :, [2, 1, 0, 3]].copy()      # BGRA->RGBA
        if self.rig is None:
            # 空工作台 -> 建一个 rig（尺寸取剪贴板内容或默认 1024）
            W = int(meta.get("x", 0)) + w
            H = int(meta.get("y", 0)) + h
            self.rig = {"width": max(W, 1024), "height": max(H, 1024), "fps": 30.0,
                        "nodes": [], "_layers": [], "draw_order": []}
            self.W, self.H = self.rig["width"], self.rig["height"]
            self.history.seed(self)
        # 粘贴位置：剪贴板记录的原位置（PS 行为），越界则夹到画布内
        px = int(meta.get("x", 0))
        py = int(meta.get("y", 0))
        px = max(0, min(px, self.W - w))
        py = max(0, min(py, self.H - h))
        name = "pasted"
        existing = {l["name"] for l in self.layers}
        if name in existing:
            n = 2
            while "%s_%d" % (name, n) in existing:
                n += 1
            name = "%s_%d" % (name, n)
        layer = {
            "name": name,
            "tag": meta.get("tag", "Image"),
            "node": meta.get("node", "other"),
            "path": "",
            "img": np.ascontiguousarray(rgba),
            "bbox": (px, py, px + w, py + h),
            "depth": float(meta.get("depth", 0.1)),
        }
        self.layers.insert(0, layer)              # 顶部=最前
        self.rig["_layers"] = list(self.layers)
        self.rig["draw_order"] = [l["name"] for l in reversed(self.layers)]
        self.visible[name] = True
        self.locked[name] = False
        self.rig.pop("_lite", None)
        self._sync_layer_index()
        self.reload_widgets()
        self._select_in_list(name)
        self.history.push(self)
        InfoBar.success("已粘贴", "新图层 %s（%dx%d）" % (name, w, h), parent=self,
                        position=InfoBarPosition.TOP, duration=2200)

    def _on_lasso_committed(self, selections):
        """多边形套索提交：把当前选中图层裁剪到选区内。

        selections: [{"points": [...], "op": "add"|"sub"}, ...]
            —— 画布侧累积的已闭合选区；"add" 是加选、"sub" 是减选，
            最终掩码 = 所有加选区并集 - 所有减选区（PS 的布尔运算）。

        实现：lasso_tool 生成布尔掩码 -> 与原图 alpha 相乘 -> 收紧 bbox，
        替换 layers / rig['_layers'] 里的该层；
        历史快照存的是**对象引用**，所以用**新 dict**替换（原地改 img
        会让旧快照的引用看到新内容、撤销失效）——与图层改名同一套路。
        首次裁剪时把原图另存到 _orig_img，供「还原」使用。
        """
        if self.rig is None or not selections:
            return
        name = self._selected
        if not name:
            InfoBar.info("多边形套索", "请先在图层面板选中一个图层", parent=self,
                         position=InfoBarPosition.TOP, duration=2500)
            return
        layer = self.layer_by_name(name)
        if layer is None:
            return
        if self.locked.get(name):
            InfoBar.warning("图层已锁定", "「%s」已锁定，无法裁剪" % name, parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
            return
        import lasso_tool
        bbox = layer.get("bbox")
        img = layer.get("img")
        if not bbox or img is None:
            return
        x0, y0, x1, y1 = [int(v) for v in bbox]
        h, w = img.shape[:2]
        # 各选区掩码画在图层自身坐标系
        add_masks, sub_masks = [], []
        for sel in selections:
            pts = lasso_tool.selection_points(sel)
            op = sel.get("op", "add")
            local = [(px - x0, py - y0) for px, py in pts]
            m = lasso_tool.polygon_mask(local, w, h)
            if m is None:
                continue
            (sub_masks if op == "sub" else add_masks).append(m)
        mask = lasso_tool.combine_masks(add_masks, sub_masks, w, h,
                                        feather=self.lasso_feather,
                                        invert=getattr(self, "_selection_invert", False))
        if mask is None:
            InfoBar.warning("选区无效", "选区与图层没有重叠，未做任何修改",
                            parent=self, position=InfoBarPosition.TOP, duration=3000)
            return
        # 选区与图层实际重叠像素为 0 -> 明确提示，别静默改层
        if not (mask > 0).any():
            InfoBar.warning("选区无效", "选区与图层没有重叠，未做任何修改", parent=self,
                            position=InfoBarPosition.TOP, duration=3000)
            return
        # 有重叠但重叠处全透明（图层 bbox 的空白角很常见）-> 也提示，不清空图层
        if not ((img[:, :, 3] > 0) & (mask > 0)).any():
            InfoBar.warning("选区为空", "选区落在图层的空白处，没有可保留的内容",
                            parent=self, position=InfoBarPosition.TOP, duration=3000)
            return
        # L2：保留原图（首次裁剪时存），供「还原」与"在原图上再次选取"
        if "_orig_img" not in layer or layer.get("_orig_bbox") is None:
            layer["_orig_img"] = img
            layer["_orig_bbox"] = tuple(bbox)
        newL = lasso_tool.clip_layer_by_mask(layer, mask)
        if newL is None:
            InfoBar.warning("选区无效", "选区与图层没有重叠，或图层完全被裁空",
                            parent=self, position=InfoBarPosition.TOP, duration=3000)
            return
        # _orig_img/_orig_bbox 随新 dict 传递（dict(layer) 是浅拷贝，引用仍在）
        newL["_orig_img"] = layer["_orig_img"]
        newL["_orig_bbox"] = layer["_orig_bbox"]
        # 替换 layers 与 rig['_layers'] 里的该层（两者可能是同一个 list）
        for arr in (self.layers, self.rig.get("_layers", [])):
            for i, x in enumerate(arr):
                if x["name"] == name:
                    arr[i] = newL
        if self.rig.get("_layers") is not self.layers:
            self.rig["_layers"] = [newL if x is layer else x
                                    for x in self.rig.get("_layers", [])]
        self.rig.pop("_lite", None)          # LOD 缓存作废
        self._sync_layer_index()
        self.history.push(self)
        self.reload_widgets()
        self._select_in_list(name)
        self._clear_selection()              # 裁剪完成 -> 选区状态清空
        InfoBar.success("已裁剪图层", "%s：保留选区内像素" % name, parent=self,
                        position=InfoBarPosition.TOP, duration=2500)

    def _layer_restore_original(self, name):
        """还原图层到裁剪前的原始像素（L2：保留原图）。"""
        layer = self.layer_by_name(name)
        if layer is None or not layer.get("_orig_img") is not None:
            InfoBar.info("还原原图", "该图层没有裁剪记录", parent=self,
                         position=InfoBarPosition.TOP, duration=2000)
            return
        orig_img = layer["_orig_img"]
        orig_bbox = layer["_orig_bbox"]
        newL = dict(layer)
        newL["img"] = orig_img
        newL["bbox"] = tuple(orig_bbox)
        for arr in (self.layers, self.rig.get("_layers", [])):
            for i, x in enumerate(arr):
                if x["name"] == name:
                    arr[i] = newL
        if self.rig.get("_layers") is not self.layers:
            self.rig["_layers"] = [newL if x is layer else x
                                    for x in self.rig.get("_layers", [])]
        self.rig.pop("_lite", None)
        self._sync_layer_index()
        self.history.push(self)
        self.reload_widgets()
        self._select_in_list(name)
        InfoBar.success("已还原原图", name, parent=self,
                        position=InfoBarPosition.TOP, duration=2000)

    def toggle_visible(self, name):
        """切换某层显隐（delegate 的眼睛点击入口）。
        只刷行 + 重绘画布，不重建列表。"""
        if not name:
            return
        self.visible[name] = not self.visible.get(name, True)
        self._refresh_layer_rows()
        self.redraw()

    def _refresh_layer_rows(self):
        """只重画图层列表的行（不重建 item）：可见性/锁定/名字变化后用，
        比 reload_widgets 快得多（后者 clear+新建 17 个 item + 全部重绘）。"""
        try:
            vp = self.layer_list.viewport()
        except Exception:
            return
        vp.update()

    def layer_by_name(self, name):
        """按名字取图层（带缓存字典，避免 delegate 每次 paint 线性扫描）。
        缓存 key 是**当前**名字；改名/删层/重排后由 _sync_layer_index 重建。"""
        if not name:
            return None
        idx = getattr(self, "_layer_index", None)
        if idx is None:
            idx = self._sync_layer_index()
        l = idx.get(name)
        if l is not None:
            return l
        # 缓存可能过期（改名等），重建一次再查
        idx = self._sync_layer_index()
        return idx.get(name)

    def _sync_layer_index(self):
        """重建 name->layer 字典索引（重命名/增删/重排后调用）"""
        idx = {l["name"]: l for l in self.layers}
        self._layer_index = idx
        return idx

    def _on_current_layer_changed(self, cur, _prev=None):
        """图层列表 currentItem 变化 -> 同步画布选中层（移动工具只拖该层）。"""
        self._selected = cur.data(Qt.UserRole) if cur is not None else None
        self.canvas.selected_name = self._selected
        # 选中层变了 -> 编辑菜单里"自由变换/图层补洞"的可用性要立刻跟上
        # （菜单打开时也会刷，但选中变化就刷更健壮：快捷键不经过菜单）
        self._sync_edit_menu_state()

    def _on_move_grab(self, name):
        """按下命中选中层：记原始 bbox（累计偏移基准，避免逐帧增量误差）。
        锁定层直接拒绝（防线上是 hit-test，这里是第二道防线——
        避免将来有代码绕过 hit-test 直调槽函数时把锁定层拖走）。"""
        self._mv_orig = None
        if self.locked.get(name):
            return
        for l in self.layers:
            if l["name"] == name:
                self._mv_orig = tuple(l["bbox"])
                break

    def _on_move_drag(self, name, dx, dy):
        if self._mv_orig is None:
            return
        if self.locked.get(name):        # 第二道防线：锁定层不动
            return
        x0, y0, x1, y1 = self._mv_orig
        nx0, ny0 = int(round(x0 + dx)), int(round(y0 + dy))
        for l in self.layers:
            if l["name"] == name:
                # 必须 int：渲染切片 img[y0:y1] 只吃整数 bbox（load_layers 契约）
                l["bbox"] = (nx0, ny0, nx0 + int(x1 - x0), ny0 + int(y1 - y0))
                self._sync_lite_bbox(l)
                break
        self.redraw()

    def _sync_lite_bbox(self, layer):
        """_lite 缓存里存的是各层裁剪图+bbox，源层 bbox 变了必须同步，
        否则预览仍画旧位置（曾踩：移动后画面不动，看起来像"只改了层级"）。
        与 _lite_rig 同式重算该层，避免每次拖拽事件重建全部层。"""
        import cv2
        for lite in self.rig.get("_lite", {}).values():
            W, H = lite["width"], lite["height"]
            sc = W / float(self.rig["width"])
            x0, y0, x1, y1 = layer["bbox"]
            nx0, ny0 = int(round(x0 * sc)), int(round(y0 * sc))
            nx1, ny1 = int(round(x1 * sc)), int(round(y1 * sc))
            nx1, ny1 = max(nx0 + 1, min(W, nx1)), max(ny0 + 1, min(H, ny1))
            for ll in lite["_layers"]:
                if ll["name"] == layer["name"]:
                    ll["bbox"] = (nx0, ny0, nx1, ny1)
                    ll["img"] = cv2.resize(layer["img"], (nx1 - nx0, ny1 - ny0),
                                           interpolation=cv2.INTER_AREA)

    def _select_in_list(self, name):
        """程序化选中列表项（会经 currentItemChanged 同步到画布）。"""
        for i in range(self.layer_list.count()):
            it = self.layer_list.item(i)
            if it is not None and it.data(Qt.UserRole) == name:
                self.layer_list.setCurrentItem(it)
                return

    def rename_layer(self, old, new):
        """兼容入口：delegate.setModelData 调用，转给 _rename_layer。"""
        self._rename_layer(old, new)

    def _rename_layer(self, old, new):
        """重命名图层（PS 双击缩略图 / 右键菜单触发）。

        清洗非法字符、重名自动 _2/_3；同步 layers 的 name、visible/locked 的键、
        rig draw_order、rig nodes[].layers 引用；history.push 可撤销。
        改 name 时**新建层 dict 替换**，不能原地改——历史快照存对象引用，
        原地改会让撤销拿不到旧名（对象引用快照方案的死角）。"""
        if self.rig is None or not old:
            return
        obj = next((l for l in self.layers if l["name"] == old), None)
        if obj is None:
            return
        base = sanitize_proj_name(new)
        if not base:
            return
        name = base
        n = 2
        existing = {l["name"] for l in self.layers if l["name"] != old}
        while name in existing:
            name = "%s_%d" % (base, n)
            n += 1
        if name == old:
            return
        # 新建层 dict 替换（img/path 等其余字段共享引用）
        new_obj = dict(obj)
        new_obj["name"] = name
        idx = next(i for i, l in enumerate(self.layers) if l is obj)
        self.layers[idx] = new_obj
        # rig["_layers"] 与 self.layers 可能是同一 list（load_dir 后），
        # 也可能是独立 list（_drop_add_layer/_apply_subsplit 后），两种都要处理
        if self.rig["_layers"] is not self.layers:
            self.rig["_layers"] = [new_obj if l is obj else l
                                   for l in self.rig["_layers"]]
        # visible / locked 字典键迁移（值保持；不存在则跳过）
        if old in self.visible:
            self.visible[name] = self.visible.pop(old)
        locked = getattr(self, "locked", {})
        if old in locked:
            locked[name] = locked.pop(old)
        # rig nodes[].layers 里引用该层名的地方（字符串列表，必须单独替换）
        for nd in self.rig["nodes"]:
            if old in nd["layers"]:
                k = nd["layers"].index(old)
                nd["layers"][k] = name
        # draw_order（字符串列表）
        self.rig["draw_order"] = [name if x == old else x for x in self.rig["draw_order"]]
        # 选中态跟随（否则移动工具拖不到改名后的层）
        if self._selected == old:
            self._selected = name
            self.canvas.selected_name = name
        self.rig.pop("_lite", None)
        self.reload_widgets()
        self.history.push(self)

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
            self._sync_layer_index()          # 重排后索引失效，重建
            self.rig["draw_order"] = list(reversed(names))
            self.history.push(self)
            self.redraw()

    # ---------- 复制图层 ----------
    def _layer_copy(self, name):
        """复制图层：新层紧邻源层上方（列表索引更小，列表顶=最前）。

        浅拷贝 dict（img 共享同一份数组引用，图片数据不修改），tag/node/depth
        照抄；visible 复制可见状态、locked 默认 False。同步 visible/locked/
        draw_order/_layers，history.push 可撤销。"""
        if self.rig is None:
            return
        src = self.layer_by_name(name)
        if src is None:
            return
        idx = next((i for i, l in enumerate(self.layers) if l is src), None)
        if idx is None:
            return
        # 新名：原名_copy，冲突则 _copy_2/_copy_3…
        base = name + "_copy"
        new_name = base
        n = 2
        existing = {l["name"] for l in self.layers}
        while new_name in existing:
            new_name = "%s_%d" % (base, n)
            n += 1
        # 浅拷贝 dict（img 共享引用）
        new_layer = dict(src)
        new_layer["name"] = new_name
        # 紧邻源层上方插入（列表顶=最前）
        self.layers.insert(idx, new_layer)
        # rig["_layers"] 可能与 self.layers 是同一 list，也可能是独立 list
        if self.rig["_layers"] is not self.layers:
            self.rig["_layers"].insert(idx, new_layer)
        # visible 复制可见状态；locked 默认 False
        self.visible[new_name] = self.visible.get(name, True)
        self.locked[new_name] = False
        # draw_order 按插入后的 layers 顺序重算（末位=最前=最后绘制）
        self.rig["draw_order"] = [l["name"] for l in reversed(self.layers)]
        self._sync_layer_index()
        self.rig.pop("_lite", None)
        self.reload_widgets()
        self.history.push(self)
        self._select_in_list(new_name)
        InfoBar.success("已复制图层", new_name, parent=self,
                        position=InfoBarPosition.TOP, duration=2500)

    # ---------- 图层右键菜单（置顶 / 重命名 / 复制 / 拆分 / 删除） ----------
    def _layer_menu(self, pos):
        if self.rig is None:
            return
        it = self.layer_list.itemAt(pos)
        if it is None:
            return
        name = it.data(Qt.UserRole)
        layer = self.layer_by_name(name)
        menu = RoundMenu(parent=self.layer_list)
        menu.addAction(Action(FluentIcon.UP, "置顶",
                              triggered=lambda: self._layer_to_top(name)))
        menu.addAction(Action(FluentIcon.EDIT, "重命名",
                              triggered=lambda: self.start_rename_edit(name)))
        # 还原原图（L2：套索裁剪前保存了原始像素，可还原）
        if layer and layer.get("_orig_img") is not None:
            menu.addAction(Action(FluentIcon.UPDATE, "还原原图",
                                  triggered=lambda: self._layer_restore_original(name)))
        menu.addAction(Action(text=("解锁" if self.locked.get(name) else "锁定"),
                              triggered=lambda: self.toggle_lock(name)))
        # 拆分此层：两级子菜单（A 连通域 / B SAM）
        sub = RoundMenu("拆分此层", parent=self)
        sub.addAction(Action(FluentIcon.CUT, "基础拆分（连通域）",
                             triggered=lambda: self._layer_subsplit(name, "basic")))
        sub.addAction(Action(FluentIcon.SEARCH, "智能拆分（SAM）",
                             triggered=lambda: self._layer_subsplit(name, "sam")))
        menu.addMenu(sub)
        menu.addAction(Action(FluentIcon.COPY, "复制图层",
                              triggered=lambda: self._layer_copy(name)))
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
        if self.locked.get(name):
            InfoBar.warning("图层已锁定", "%s 已锁定，无法拆分（取消锁定后重试）" % name,
                            parent=self, position=InfoBarPosition.TOP, duration=3000)
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
        if self.locked.get(name):
            InfoBar.warning("图层已锁定", "%s 已锁定，无法置顶（取消锁定后重试）" % name,
                            parent=self, position=InfoBarPosition.TOP, duration=3000)
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
        if self.locked.get(name):
            InfoBar.warning("图层已锁定", "%s 已锁定，无法删除（取消锁定后重试）" % name,
                            parent=self, position=InfoBarPosition.TOP, duration=3000)
            return
        self.layers = [l for l in self.layers if l["name"] != name]
        self.rig["draw_order"] = [n for n in self.rig["draw_order"] if n != name]
        self.rig["_layers"] = [l for l in self.rig["_layers"] if l["name"] != name]
        for nd in self.rig["nodes"]:
            if name in nd["layers"]:
                nd["layers"].remove(name)
        if self._selected == name:
            self._selected = None
            self.canvas.selected_name = None
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
            # 「新建」弹窗指定的画布尺寸优先；否则画布=图片尺寸（自适应）
            cw, ch = getattr(self, "_new_canvas", None) or (w, h)
            self.rig = {"width": cw, "height": ch, "fps": 30.0,
                        "nodes": [], "_layers": [], "draw_order": []}
            self.W, self.H = cw, ch
            self.cur_dir = None
            self._new_canvas = None      # 消费掉，避免后续再建 rig 时误用旧尺寸
            # 无弹窗来源的空画布拖图：以图片名建工程目录（中文文件名保留中文）
            if not self.proj_name and not self.proj_dir:
                stem = os.path.splitext(os.path.basename(img_path))[0]
                pname, pd = self._create_project_dir(stem, self.W, self.H)
                self.proj_name, self.proj_dir = pname, pd
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
        """显示/隐藏全部图层。
        性能：原来调 reload_widgets() 重建全部 item（clear + 17 次新建 + 17 行重绘），
        实测真窗口 60~100ms，体感"卡一下"。这里改成**只刷行**——
        可见状态变了但列表结构没变，委托重画眼睛即可。"""
        vals = [self.visible.get(k, True) for k in self.visible]
        target = not all(vals) if vals else True
        for k in self.visible:
            self.visible[k] = target
        self._refresh_layer_rows()
        self.redraw()

    def _toggle_play(self):
        self.playing = not self.playing
        self.btn_play.setText("暂停" if self.playing else "播放")
        if self.playing:
            self.timer.start()
        else:
            self.timer.stop()

    def _tick(self):
        # v2.11.0：循环上限用可调 duration（原来写死 10.0）
        dur = max(0.2, float(getattr(self, "duration", 4.0)))
        self.t = (self.t + 0.033) % dur
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(self.t / dur * 1000)))
        self.slider.blockSignals(False)
        self.time_lbl.setText("%.2fs" % self.t)
        self.redraw()

    def _on_scrub(self, v):
        if self.playing:
            return
        dur = max(0.2, float(getattr(self, "duration", 4.0)))
        self.t = (v / 1000.0) * dur
        self.time_lbl.setText("%.2fs" % self.t)
        self.redraw()

    # ---------- v2.11.0 时间轴联动 ----------
    def push_history(self):
        """时间轴改关键帧前先存快照（等价于 history.push(self)）。"""
        try:
            self.history.push(self)
        except Exception:
            pass

    def _on_timeline_seek(self, t):
        """拖播放头 / 点时间轴空白：跳到该时刻。"""
        self.t = max(0.0, min(float(getattr(self, "duration", 4.0)), float(t)))
        self.playing = False
        try:
            self.timer.stop()
            self.btn_play.setText("播放")
        except Exception:
            pass
        dur = max(0.2, float(getattr(self, "duration", 4.0)))
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(self.t / dur * 1000)))
        self.slider.blockSignals(False)
        self.time_lbl.setText("%.2fs" % self.t)
        self.redraw()

    def _on_keyframes_changed(self):
        """关键帧变了：清 LOD 缓存（不改像素，但保险）并重绘。"""
        try:
            if isinstance(self.rig, dict):
                self.rig.pop("_lite", None)
        except Exception:
            pass
        self.redraw()
        # 时间轴自己的播放头位置也跟着刷新
        if getattr(self, "anim_panel", None) is not None:
            try:
                self.anim_panel.area.update()
            except Exception:
                pass

    def _on_duration_changed(self, d):
        """时长改了：把当前 t 夹回范围内，滑块重映射。"""
        self.duration = max(0.2, float(d))
        if self.t > self.duration:
            self.t = self.duration
        dur = self.duration
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(self.t / dur * 1000)))
        self.slider.blockSignals(False)
        self.time_lbl.setText("%.2fs" % self.t)
        self.redraw()

    def _on_fps_changed(self, f):
        """帧率改了：只影响导出（MP4/GIF 取帧间隔）。"""
        self.fps = max(1, int(f))

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
        for it in items[:RECENT_MENU_MAX]:
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

    def _create_project_dir(self, name, W, H):
        """建工程目录 + 占位工程（act_new 与空画布拖图共用，勿复制粘贴）。
        name 已保证唯一；返回 (proj_name, 目录)。"""
        proj_name = unique_proj_name(name)
        d = os.path.join(PROJ_BASE, proj_name)
        os.makedirs(d, exist_ok=True)
        self._write_project_placeholder(d, proj_name, W, H)
        self._notify_nav(proj_name, d)
        return proj_name, d

    def act_new(self, ask_size=False):
        """新建工程。ask_size=True（菜单触发）弹窗问工程名+画布尺寸；
        False（启动初始化/程序化调用）直接清空为空白工作台，不弹窗、不建工程。"""
        if not ask_size:
            self._clear_workbench()
            return
        dlg = NewCanvasDialog(self, default_name=unique_proj_name("工程"))
        if not dlg.exec():
            return                          # 取消：保持当前工作台不动
        W, H = dlg.canvas_size()
        name = dlg.project_name()
        if not W or not name:
            self._clear_workbench()
            return
        # 建工程目录 + 占位工程 + 空画布 rig（新建即有画布）
        self._clear_workbench()
        proj_name, d = self._create_project_dir(name, W, H)
        self.rig = {"width": W, "height": H, "fps": 30.0,
                    "nodes": [], "_layers": [], "draw_order": []}
        self.W, self.H = W, H
        self.cur_dir = d
        self.proj_name = proj_name
        self.proj_dir = d
        self.reload_widgets()
        self.history.seed(self)
        self.prog.setText("新工程 %s（%d×%d）：拖入图片开始" % (proj_name, W, H))

    def _clear_workbench(self):
        """清空工作台（不建工程目录）。"""
        self.rig = None
        self.layers = []
        self.visible = {}
        self.locked = {}
        self.cur_dir = None
        self.proj_name = None
        self.proj_dir = None
        self._new_canvas = None
        self._selected = None
        self.canvas.selected_name = None
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
        return {"name": getattr(self, "proj_name", None),
                "width": self.W, "height": self.H,
                "draw_order": self.rig["draw_order"],
                # v2.11.0：关键帧动画 + 时间轴设置
                "keyframes": getattr(self.rig, "get", lambda *a: None)("keyframes") or {},
                "anim": {"duration": float(getattr(self, "duration", 4.0)),
                          "fps": int(getattr(self, "fps", 30)),
                          # v2.11.2：时间轴界面偏好（字号缩放 / 面板高度）
                          "ui_scale": float(getattr(
                              getattr(self, "anim_panel", None), "ui_scale", 1.0)),
                          "panel_h": getattr(
                              getattr(self, "anim_panel", None), "_manual_h", None)},
                "nodes": [{"node": n["node"], "parent": n["parent"],
                           "pivot": n["pivot"], "layers": n["layers"]}
                          for n in self.rig["nodes"]]}

    def act_save(self):
        if self.rig is None or not getattr(self, "cur_dir", None):
            self.act_saveas()
            return
        self._write_project(self.cur_dir)
        self._notify_nav(getattr(self, "proj_name", None), self.cur_dir)

    def act_saveas(self):
        """另存为：默认工程名 = 当前工程名，用户可改；落到 projects/<名>/。"""
        base = getattr(self, "proj_name", None) or "工程"
        dlg = ProjectNameDialog(self, default_name=base)
        if not dlg.exec():
            return
        proj_name = dlg.project_name() or "工程"
        # 与当前工程同名 -> 原地覆盖；否则重名自动加序号
        if proj_name != (getattr(self, "proj_name", None) or ""):
            proj_name = unique_proj_name(proj_name)
        d = os.path.join(PROJ_BASE, proj_name)
        os.makedirs(d, exist_ok=True)
        self.proj_name = proj_name
        self._write_project(d)
        self.cur_dir = d
        self.proj_dir = d
        self._notify_nav(proj_name, d)

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
        # v2.11.0：导出时长/帧率用时间轴上的可调设置（原来写死 10s/30fps）
        self.export_worker = ExportWorker(
            self.rig, hidden, kind, p,
            seconds=max(0.2, float(getattr(self, "duration", 4.0))),
            fps=max(1, int(getattr(self, "fps", 30))))
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
        # task-15 四档链：先告知当前命中的档位。无 CUDA 默认仍走 ComfyUI
        # （触发条件不动）；ComfyUI 也不可用才是 CPU 兜底——确认框由 worker
        # 捕获 CpuConfirmationRequired 后经 need_cpu_confirm 信号在这条主线程上弹。
        # 注意 engine_tier 首次会惰性 import torch（几秒，与旧版 _engine_mode
        # 相同成本）+ localhost 探测（无服务时毫秒级）。
        try:
            tier, _why = engine_tier()
        except Exception:
            tier = "comfy" if _engine_mode() == "comfy" else "system_cuda"
        if tier == "comfy":
            InfoBar.info("引擎档位：%s" % TIER_LABELS["comfy"],
                         "前提：本机 ComfyUI 已启动且装了 See-through 插件"
                         "（安装器勾选「A 卡拆图支持」可代下 ComfyUI 安装器与插件（也可自行下载））。"
                         "推理用 bf16，首次运行插件会自动下载约 12.5GB 模型，耗时较长且可重跑续传。",
                         parent=self, position=InfoBarPosition.TOP, duration=9000)
        elif tier == "cpu":
            InfoBar.warning("引擎档位：%s" % TIER_LABELS["cpu"],
                            "未检测到可用 CUDA，也没发现在跑的 ComfyUI。"
                            "即将弹出 CPU 兜底确认框：约 2-3 小时/张（CUDA 约 14 分钟/张），"
                            "装 CUDA 加速包或启动 ComfyUI 都快得多。",
                            parent=self, position=InfoBarPosition.TOP, duration=9000)
        self.busy = True
        self.prog.setText("准备中…")
        self._nav_pct = 0
        self.set_nav_progress(0)
        out = os.path.join(OUT_BASE, os.path.splitext(os.path.basename(img_path))[0])
        self._decompose_job = (img_path, out)   # CPU 确认通过后重试用
        self._start_worker(img_path, out)

    def _start_worker(self, img_path, out):
        old = getattr(self, "worker", None)
        if old is not None:
            # CPU 确认后重启时旧线程刚发完信号还没退出：持引用防 QThread 析构崩溃
            self._retired_workers = getattr(self, "_retired_workers", [])
            self._retired_workers = [w for w in self._retired_workers if w.isRunning()] + [old]
        self.worker = DecomposeWorker(img_path, out)
        self.worker.log_signal.connect(self._on_decompose_log)
        self.worker.done_signal.connect(self._decompose_done)
        self.worker.need_cpu_confirm.connect(self._ask_cpu_confirm)
        self.worker.start()

    def _ask_cpu_confirm(self):
        """CPU 兜底确认框（worker 捕获 CpuConfirmationRequired 后触发，主线程）。
        「仍要用 CPU」→ 置 seethrough_local 会话标志（本次会话不再弹）并重投
        同一任务；取消/关闭 → 释放 busy，提示更快的档位。"""
        if getattr(self, "_cpu_box_open", False):
            return
        self._cpu_box_open = True
        try:
            import seethrough_local as SL
            if SL.cpu_engine_confirmed():
                # 已确认过（会话或 env）：静默放行，直接重投
                self._restart_decompose()
                return
            box = MessageBox(
                "CPU 兜底拆图确认",
                "四档引擎链（加速包 → 系统 CUDA → ComfyUI → CPU）只剩最后的 CPU 档。\n"
                "CPU 实测约 2-3 小时/张 @512px、24 层（CUDA 本地引擎约 14 分钟/张），"
                "内存需求 ~10GB，期间请避免高负载。\n\n"
                "仍要用 CPU 兜底拆这张图吗？\n"
                "（确认后本次会话内不再重复询问。更快：「资产管理 → 推理加速」装 CUDA 加速包；"
                "A 卡启动带 See-through 插件的 ComfyUI 后重新拆图。）",
                self)
            chosen = [False]
            box.yesSignal.connect(lambda: chosen.__setitem__(0, True))
            box.exec()
            if chosen[0]:
                SL.confirm_cpu_engine()
                self._restart_decompose()
            else:
                self.busy = False
                self.set_nav_progress(None)
                self.prog.setText("已取消 CPU 兜底拆图")
                InfoBar.warning("已取消", "可安装 CUDA 加速包或启动 ComfyUI 后重试",
                                parent=self, position=InfoBarPosition.TOP,
                                duration=5000)
        except Exception as e:
            self.busy = False
            self.prog.setText("CPU 确认流程异常: %s" % e)
        finally:
            self._cpu_box_open = False

    def _restart_decompose(self):
        """CPU 确认通过后重投原始拆图任务（busy 保持 True，进度不清）。"""
        job = getattr(self, "_decompose_job", None)
        if not job:
            self.busy = False
            self.prog.setText("没有可重试的拆图任务")
            return
        img_path, out = job
        self.prog.setText("CPU 兜底档启动中…")
        self._start_worker(img_path, out)

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
        """拆分进度显示在画布底栏（播放条右侧）的进度条上。

        pct=None 表示结束：清空并隐藏进度条。
        历史：这块进度原先是"塞进导航栏"，但导航栏本身默认隐藏，
        用户根本看不到，所以挪回工作台底栏。
        """
        # ---- 新位置：底栏进度条 ----
        try:
            if pct is None:
                self._nav_pct = None
                self.pbar.setVisible(False)
                self.pbar_lbl.setVisible(False)
                self.pbar.setValue(0)
                self.pbar_lbl.setText("")
            else:
                cur = getattr(self, "_nav_pct", None)
                if cur is not None and pct < cur:
                    pct = cur                      # 防倒退
                self._nav_pct = pct
                self.pbar.setVisible(True)
                self.pbar_lbl.setVisible(True)
                self.pbar.setValue(int(pct))
                self.pbar_lbl.setText("%d%%" % int(pct))
        except Exception:
            pass
        # ---- 兼容旧通道：仍通知 nav_setter（外部可能有依赖）----
        setter = getattr(self, "nav_setter", None)
        if setter is None:
            return
        if pct is None:
            setter("", "")
            return
        setter("拆分进度 %d%%" % pct, "拆分中 %d%%" % pct)

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
        self.setWindowTitle("木偶工作室 v" + VERSION)
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
        # 去掉左上角返回按钮（用户需求：单页应用没有"返回"语义，留着是噪音）
        try:
            self.navigationInterface.setReturnButtonVisible(False)
        except Exception:
            # 库版本差异兜底：直接隐藏控件
            try:
                self.navigationInterface.panel.returnButton.setVisible(False)
            except Exception:
                pass
        # 去掉导航栏「工作台」按钮（用户需求）：保留 addSubInterface 让 wb 进
        # stackedWidget，但把导航项隐藏（最小改动，内容显示不受影响）。
        self.nav_item = self.navigationInterface.widget("workbench")
        if self.nav_item is not None:
            self.nav_item.setVisible(False)
        # 工程列表（导航项）
        self._proj_items = {}          # routeKey -> 工程目录
        # 拆分进度注入：工作台按钮已隐藏，写进工作台状态栏（prog）即可，进度不丢
        def _set_nav(text, tip):
            try:
                self.wb.prog.setText(text)
            except Exception:
                pass
        self.wb.nav_setter = _set_nav
        # 导航栏：默认隐藏（单页应用常驻导航无益，用户要求直接去掉）。
        # 标题栏侧栏按钮可唤出，唤出时 140px 完整显示工程列表（图标+文字）。
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
        # 导航栏顶部留出标题栏高度：库只给内容区加了 48 的顶部边距
        # （widgetLayout.setContentsMargins(0,48,0,0)），导航栏没加，
        # 所以展开后第一项会被标题栏压住、看起来像"顶部空白"。
        try:
            ni.setContentsMargins(0, 48, 0, 0)
        except Exception:
            pass
        # 导航栏内部的滚动区/布局也补一下顶部间距（不同版本结构不同，逐个试）
        try:
            panel = ni.panel
            for attr in ('scrollWidget', 'viewport', 'vBoxLayout'):
                o = getattr(panel, attr, None)
                if o is None:
                    continue
                if hasattr(o, 'setContentsMargins'):
                    o.setContentsMargins(0, 48, 0, 0)
                    break
        except Exception:
            pass
        # 导航栏宽度调节：在导航栏右缘挂一条 4px 拖拽把手（悬停变蓝）。
        # 原来宽度被 setFixedWidth(140) 钉死，用户想拉宽看长工程名没法弄。
        self.nav_min_w = 120
        self.nav_max_w = 420
        self._nav_resize = None          # 拖拽起始信息
        self.nav_grip = QWidget(self)
        self.nav_grip.setObjectName("navGrip")
        self.nav_grip.setFixedWidth(4)
        self.nav_grip.setCursor(Qt.SizeHorCursor)
        self.nav_grip.setStyleSheet(
            "#navGrip{background:transparent;}"
            "#navGrip:hover{background:#4d88ff;}")
        self.nav_grip.setToolTip("拖动调节导航栏宽度（双击恢复默认 140）")
        self.nav_grip.installEventFilter(self)
        self.nav_grip.setVisible(False)   # 导航栏显示时才显示
        self._nav_grip_shown = False
        # 启动扫描 projects\ 把已有工程加进导航栏
        self._scan_projects()
        # ---- 标题栏左上角工具条（DSH 风格）：折叠按钮 + 菜单 + 导出 ----
        self.btn_collapse = TransparentToggleToolButton(SidebarIcon(), self)
        self.btn_collapse.setFixedSize(32, 30)
        self.btn_collapse.setToolTip("显示/隐藏导航栏")
        self.btn_collapse.clicked.connect(self._toggle_nav)
        tbar = self.titleBar.hBoxLayout
        tbar.insertWidget(0, self.btn_collapse, 0, Qt.AlignLeft | Qt.AlignVCenter)
        tbar.insertWidget(1, self.wb.menu_btn, 0, Qt.AlignLeft | Qt.AlignVCenter)
        tbar.insertWidget(2, self.wb.edit_btn, 0, Qt.AlignLeft | Qt.AlignVCenter)
        tbar.insertWidget(3, self.wb.asset_btn, 0, Qt.AlignLeft | Qt.AlignVCenter)
        # 快捷键
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.wb.act_undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.wb.act_redo)
        # PS 风格选区操作：复制/剪切/粘贴（套索选区激活时有效）
        QShortcut(QKeySequence("Ctrl+C"), self, activated=self.wb.act_copy_selection)
        QShortcut(QKeySequence("Ctrl+X"), self,
                  activated=lambda: self.wb.act_copy_selection(True))
        QShortcut(QKeySequence("Ctrl+V"), self, activated=self.wb.act_paste_selection)
        # 反选（PS 的选择反向）
        QShortcut(QKeySequence("Ctrl+Shift+I"), self,
                  activated=self.wb.act_invert_selection)
        # 自由变换（PS Ctrl+T）
        QShortcut(QKeySequence("Ctrl+T"), self,
                  activated=self.wb.act_free_transform)
        # 取消选择（PS Ctrl+D）
        QShortcut(QKeySequence("Ctrl+D"), self,
                  activated=self.wb.act_deselect_selection)
        # Delete / Enter：把选区真正裁剪到图层；Esc：取消选区
        QShortcut(QKeySequence("Del"), self,
                  activated=lambda: self.wb.canvas.lasso_apply_selection())
        QShortcut(QKeySequence("Backspace"), self,
                  activated=lambda: self.wb.canvas.lasso_apply_selection())
        QShortcut(QKeySequence("Return"), self,
                  activated=lambda: self.wb.canvas.lasso_apply_selection())
        QShortcut(QKeySequence("Enter"), self,
                  activated=lambda: self.wb.canvas.lasso_apply_selection())
        QShortcut(QKeySequence("Esc"), self,
                  activated=lambda: self.wb.canvas.lasso_cancel_selection())
        # Photoshop 风格画布缩放快捷键
        cv = self.wb.canvas
        QShortcut(QKeySequence("Ctrl+0"), self, activated=cv.set_fit)
        QShortcut(QKeySequence("Ctrl+1"), self, activated=cv.set_actual)
        QShortcut(QKeySequence("Ctrl+="), self, activated=lambda: cv.zoom_step(True))
        QShortcut(QKeySequence("Ctrl+-"), self, activated=lambda: cv.zoom_step(False))

    def _toggle_nav(self):
        """折叠/展开最左导航栏：直接隐藏，布局自动把空间让给内容区"""
        self.nav_collapsed = not self.nav_collapsed
        ni = self.navigationInterface
        ni.setVisible(not self.nav_collapsed)
        # 标题栏是浮在最上层的覆盖层，导航栏展开后会与标题栏按钮重叠
        # （库把标题栏钉在窗口顶部整宽，不再给导航栏让位），
        # 所以这里把标题栏整体右移到导航栏右侧，并把导航栏提到标题栏之上。
        self._sync_titlebar_offset()
        # 把手显隐跟随折叠状态：折叠时必须隐藏，否则在空处留一条热区
        self._place_nav_grip()
        if not self.nav_collapsed:
            ni.raise_()
            try:
                self.nav_grip.raise_()
            except Exception:
                pass

    # ---------------- 导航栏宽度调节 ----------------
    NAV_W_DEFAULT = 140

    def _sync_titlebar_offset(self):
        """导航栏展开时把标题栏左移出导航栏宽度，避免按钮被压住。"""
        try:
            tb = self.titleBar
            if self.nav_collapsed:
                tb.setContentsMargins(0, 0, 0, 0)
                tb.setGeometry(0, 0, self.width(), tb.height() or 48)
            else:
                w = int(getattr(self, "nav_width", self.NAV_W_DEFAULT))
                # 让标题栏从导航栏右缘开始，宽度相应减少
                tb.setContentsMargins(0, 0, 0, 0)
                tb.setGeometry(w, 0, max(0, self.width() - w), tb.height() or 48)
            tb.raise_()
        except Exception:
            pass

    def _place_nav_grip(self):
        """把拖拽把手贴到导航栏右缘（竖直撑满内容区）。"""
        try:
            if self.nav_collapsed or not hasattr(self, "nav_grip"):
                self.nav_grip.setVisible(False)
                return
            w = int(getattr(self, "nav_width", self.NAV_W_DEFAULT))
            top = 48                      # 标题栏高度
            h = max(1, self.height() - top)
            self.nav_grip.setGeometry(w - 2, top, 4, h)
            self.nav_grip.setVisible(True)
            self.nav_grip.raise_()
        except Exception:
            pass

    def _apply_nav_width(self, w):
        """把导航栏宽度设为 w（夹在 [120, 420]）并刷新标题栏/把手。"""
        try:
            w = int(max(self.nav_min_w, min(self.nav_max_w, int(w))))
            if w == int(getattr(self, "nav_width", 0)):
                return
            self.nav_width = w
            ni = self.navigationInterface
            try:
                ni.setExpandWidth(w)
            except Exception:
                pass
            for tgt in (ni, ni.panel):
                tgt.setMinimumWidth(w)
                tgt.setFixedWidth(w)
            self._sync_titlebar_offset()
            self._place_nav_grip()
            ni.raise_()
        except Exception:
            pass

    def eventFilter(self, obj, ev):
        """导航栏宽度拖拽把手。"""
        try:
            if obj is getattr(self, "nav_grip", None):
                et = ev.type()
                if et == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
                    self._nav_resize = {"x": int(ev.globalPosition().x()),
                                        "w": int(self.nav_width)}
                    return True
                if et == QEvent.MouseMove and self._nav_resize is not None:
                    dx = int(ev.globalPosition().x()) - self._nav_resize["x"]
                    self._apply_nav_width(self._nav_resize["w"] + dx)
                    return True
                if et == QEvent.MouseButtonRelease and self._nav_resize is not None:
                    self._nav_resize = None
                    return True
                if et == QEvent.MouseButtonDblClick:
                    self._apply_nav_width(self.NAV_W_DEFAULT)
                    return True
        except Exception:
            pass
        return super().eventFilter(obj, ev)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # 窗口尺寸变化时把手与标题栏偏移都要跟着重算
        try:
            self._sync_titlebar_offset()
            self._place_nav_grip()
        except Exception:
            pass

    def add_project_entry(self, name, d):
        """导航栏加入/更新工程项。同目录只一项（更新文字）。"""
        try:
            d = os.path.normpath(d)
        except Exception:
            return
        name = name or os.path.basename(d)
        ni = self.navigationInterface
        items = getattr(self, "_proj_items", None)
        if items is None:
            items = self._proj_items = {}
        for key, dd in list(items.items()):
            if os.path.normpath(dd) == d:
                try:
                    ni.widget(key).setText(name)
                except Exception:
                    pass
                return
        n = len(items) + 1
        key = "proj:%d" % n
        while key in items:
            n += 1
            key = "proj:%d" % n
        try:
            ni.addItem(key, FluentIcon.FOLDER, name,
                       onClick=lambda checked=False, dd=d: self.wb.load_dir(dd),
                       tooltip=d)
            items[key] = d
        except Exception:
            pass

    def _scan_projects(self):
        """启动时扫描 PROJ_BASE 目录，把已有工程加进导航栏。"""
        if not os.path.isdir(PROJ_BASE):
            return
        for fn in sorted(os.listdir(PROJ_BASE)):
            d = os.path.join(PROJ_BASE, fn)
            if os.path.isdir(d):
                self.add_project_entry(fn, d)

class FluentShellWindow(AssetMenuMixin, FluentWindow):
    """v3 视频工程用的单页 FluentWindow 壳：同款标题栏/图标，导航隐藏。

    混入 AssetMenuMixin，让视频窗口顶部也有同款「资产管理」菜单。
    """

    def __init__(self):
        super().__init__()
        self.resize(1440, 860)
        from PySide6.QtGui import QIcon
        if os.path.exists(ICON_ICO):
            self.setWindowIcon(QIcon(ICON_ICO))
        elif os.path.exists(ICON_PNG):
            self.setWindowIcon(QIcon(ICON_PNG))
        self.navigationInterface.setMenuButtonVisible(False)
        try:
            self.navigationInterface.setReturnButtonVisible(False)
        except Exception:
            try:
                self.navigationInterface.panel.returnButton.setVisible(False)
            except Exception:
                pass
        self.navigationInterface.setVisible(False)

    def setCentralInterface(self, w, text="视频工程"):
        w.setObjectName("centralIface")
        self.addSubInterface(w, FluentIcon.VIDEO, text)
        item = self.navigationInterface.widget("centralIface")
        if item is not None:
            item.setVisible(False)


def _video_new_project(wb, set_proj):
    """文件 → 新建工程：新建空工程并落盘。

    video_workbench 当前没有 act_new，用现有 API 组合：换全新工程 ->
    _apply_project 刷新界面 -> save_project(ask=True) 选目录落盘。
    """
    import video_model as VM
    wb.proj = VM.new_project()
    wb.proj_dir = None
    try:
        wb._apply_project(None)
    except Exception:
        pass
    if wb.save_project(ask=True):
        set_proj(os.path.basename(wb.proj_dir))


def _video_open_project(win, wb, set_proj):
    """文件 → 打开工程：选目录 -> 校验是视频工程 -> load_dir -> 更新标题。"""
    import video_workbench as VW
    d = QFileDialog.getExistingDirectory(win,
                                         "打开工程（选择含 video_project.json 的文件夹）",
                                         VW.PROJ_BASE)
    if not d:
        return
    if not VW.VideoWorkbench.is_video_project(d):
        InfoBar.error("不是视频工程", "所选文件夹里没有 video_project.json：\n%s" % d,
                      parent=win, position=InfoBarPosition.TOP, duration=5000)
        return
    if wb.load_dir(d):
        set_proj(os.path.basename(d))


def _video_save(wb, set_proj):
    """文件 → 保存：落盘当前工程并同步标题。"""
    if wb.save_project():
        set_proj(os.path.basename(wb.proj_dir))


def _video_save_as(win, wb, set_proj):
    """文件 → 另存为：选目录 -> save_as 落盘 -> 同步标题。"""
    if wb.save_as():
        set_proj(os.path.basename(wb.proj_dir))


def _video_open_recent(win, wb, d, set_proj):
    """最近工程 → 打开：失效目录给提示，成功更新标题。"""
    import video_workbench as VW
    if not os.path.isfile(os.path.join(d, VW.META_NAME)):
        InfoBar.warning("工程已失效", "目录或 video_project.json 不存在：\n%s" % d,
                        parent=win, position=InfoBarPosition.TOP, duration=4000)
        return
    if wb.load_dir(d):
        set_proj(os.path.basename(d))


def _build_video_menus(win, wb, set_proj):
    """视频窗口顶部三菜单（文件/编辑/资产管理），挂 shell 标题栏左上横排。

    裁剪范围（用户确认）：文件去掉导入图片/PSD/AE/JSON/GIF；编辑只留
    撤销重做；资产管理整套复用 AssetMenuMixin。
    """
    # ---- 文件 ----
    fb = ToggleDropDownButton("文件", win)
    mf = RoundMenu(parent=win)
    mf.addAction(Action(FluentIcon.ADD, "新建工程",
                        triggered=lambda: _video_new_project(wb, set_proj)))
    mf.addAction(Action(FluentIcon.FOLDER, "打开工程",
                        triggered=lambda: _video_open_project(win, wb, set_proj)))
    mf.addAction(Action(FluentIcon.SAVE, "保存",
                        triggered=lambda: _video_save(wb, set_proj)))
    mf.addAction(Action(FluentIcon.SAVE_AS, "另存为",
                        triggered=lambda: _video_save_as(win, wb, set_proj)))
    mf.addAction(Action(FluentIcon.PLAY, "导出视频",
                        triggered=wb._open_export_dialog))
    mf.addSeparator()
    # 最近工程：只列 type=video 的工程，点击=打开工程同款流程
    recent = RoundMenu("最近工程", parent=win)
    recent.setIcon(FluentIcon.FOLDER)

    def _rebuild_recent():
        recent.clear()
        items = [it for it in load_recent() if it.get("type") == "video"]
        if not items:
            _a = Action("暂无最近工程")
            _a.setEnabled(False)
            recent.addAction(_a)
            return
        for it in items[:RECENT_MENU_MAX]:
            recent.addAction(Action(FluentIcon.FOLDER, it.get("name") or it["dir"],
                                    triggered=lambda d=it["dir"]:
                                    _video_open_recent(win, wb, d, set_proj)))

    _rebuild_recent()
    mf.addMenu(recent)
    mf.addSeparator()
    mf.addAction(Action(FluentIcon.CANCEL, "退出", triggered=win.close))
    fb.setMenu(mf)
    fb.menu_opened.connect(lambda _m=None: _rebuild_recent())
    fb.setFixedHeight(30)

    # ---- 编辑：只留撤销/重做 ----
    eb = ToggleDropDownButton("编辑", win)
    me = RoundMenu(parent=win)
    me.addAction(Action(FluentIcon.HISTORY, "撤销  Ctrl+Z", triggered=wb.undo))
    me.addAction(Action(FluentIcon.HISTORY, "重做  Ctrl+Y", triggered=wb.redo))
    eb.setMenu(me)
    eb.setFixedHeight(30)

    # ---- 资产管理：复用 mixin（同款首击先填后弹）----
    win._setup_asset_menu(win)
    win.asset_btn.setFixedHeight(30)

    # 挂到标题栏左上角横排（与 MainWindow 同款机制）
    tbar = win.titleBar.hBoxLayout
    tbar.insertWidget(0, fb, 0, Qt.AlignLeft | Qt.AlignVCenter)
    tbar.insertWidget(1, eb, 0, Qt.AlignLeft | Qt.AlignVCenter)
    tbar.insertWidget(2, win.asset_btn, 0, Qt.AlignLeft | Qt.AlignVCenter)
    return fb, eb, win.asset_btn


def _open_video_window(proj_dir=None):
    """新建/打开视频工程。返回 (win, wb)；打开失败返回 None（留在欢迎页）。"""
    import video_workbench as VW
    win = FluentShellWindow()
    wb = VW.VideoWorkbench(None)
    win.setCentralInterface(wb)
    # 标题闭包：打开 / 另存为 / 改脏都走它（抽出可复用，避免散落）
    title = {"name": "未命名视频工程"}

    def set_proj(name, dirty=False):
        title["name"] = name or "未命名视频工程"
        win.setWindowTitle("木偶工作室 v%s - %s%s" % (
            VERSION, title["name"], "*" if dirty else ""))

    wb.project_dirty_changed = lambda: set_proj(title["name"], True)
    file_btn, edit_btn, _ = _build_video_menus(win, wb, set_proj)
    win.file_btn = file_btn          # 供测试/外部访问三个菜单按钮
    win.edit_btn = edit_btn
    if proj_dir:
        if wb.load_dir(proj_dir):
            set_proj(os.path.basename(proj_dir))
            return win, wb
        win.deleteLater()
        return None
    set_proj("未命名视频工程")
    return win, wb


def _welcome_gate():
    """v2.12.0 启动门控：建欢迎页并接好「进工作台」路由。
    返回 (wel, holder)；holder["main"] = 选定后创建的主窗（测试可查）。"""
    from welcome_window import WelcomeWindow
    import video_workbench as VW
    icon = ICON_ICO if os.path.exists(ICON_ICO) else (
        ICON_PNG if os.path.exists(ICON_PNG) else "")
    wel = WelcomeWindow(items=load_recent(), version=VERSION, icon_path=icon)
    holder = {}

    def _enter(fn):
        """建主窗 -> 关欢迎页 -> 执行进入动作（顺序勿换：先有后关，
        否则 quitOnLastWindowClosed 会在两窗间隙里把应用退掉）"""
        win = MainWindow()
        holder["main"] = win
        win.show()
        wel.close()
        try:
            fn(win)
        except Exception:
            pass

    def _enter_video(proj_dir=None):
        r = _open_video_window(proj_dir)
        if r is None:                 # 打开失败：留在欢迎页，不切窗
            return False
        win, _wb = r
        holder["main"] = win
        win.show()
        wel.close()
        return True

    def _on_new(type_id):
        if type_id == "video_edit":
            _enter_video(None)
        else:
            _enter(lambda win: win.wb.act_new(ask_size=False))

    def _on_open(d):
        if VW.VideoWorkbench.is_video_project(d):
            _enter_video(d)
            return
        # 木偶工程：先校验目录有效，无效留在欢迎页（别先建窗再失败）
        if not (os.path.isdir(d) and find_meta(d)):
            return

        def _load(win):
            if win.wb.load_dir(d):
                InfoBar.success("已打开", os.path.basename(d), parent=win.wb,
                                position=InfoBarPosition.TOP, duration=2000)
        _enter(_load)

    wel.new_requested.connect(_on_new)
    wel.open_requested.connect(_on_open)
    # v2.12.1：「打开项目」改在欢迎页内选目录（_on_browse 选完发 open_requested），
    # 不再走 browse_requested -> 先建主窗再弹框 的旧路（流程 bug）
    return wel, holder


def _run_pending_update():
    """退出时静默应用已下载的新版（task-13/14 契约：Setup --auto --dir <安装目录>）。

    pending_installer() 只在欢迎页「检查更新」下载完成时才有值；没有则零开销返回。
    launch_update 是 DETACHED 拉起，本进程随即正常退出，安装器会等待我们结束。
    任何异常一律吞掉：更新失败绝不能卡住或拖慢用户退出。
    """
    try:
        import update_checker as U
        if not U.pending_installer():
            return
        # 安装根目录 = 本文件所在目录（installer 的 MARKERS 也认这一点）
        U.launch_update(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass


def run_app():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    setTheme(Theme.DARK)
    # qfluentwidgets 的 setTheme 不管 QToolTip：暗色下 tooltip 仍是系统默认
    # 黑字深底看不清，这里显式给一套浅色字深色底（只影响 tooltip，不动其它控件）
    app.setStyleSheet(
        "QToolTip { color:#e6e6e6; background-color:#2b3140;"
        " border:1px solid #3c4354; padding:4px 6px; }")
    # 启动先进欢迎页（选项目类型 / 打开历史项目），选定后才进工作台。
    # 测试与内部调用仍可直接 MainWindow()，不受门控影响。
    wel, _holder = _welcome_gate()
    wel.show()
    # 欢迎页「检查更新」下好的新包，在进程真正退出前拉起静默安装
    app.aboutToQuit.connect(_run_pending_update)
    # v3.2.1：退出序列开始后禁止新起档位预热（torch import 持 loader lock
    # 时退出会卡死，py-spy 实锤）；并在启动 1.5s 后就预热——用户点资产
    # 菜单时缓存已就绪，且预热窗口远离正常退出时刻。
    app.aboutToQuit.connect(lambda: _APP_EXITING.__setitem__(0, True))
    QTimer.singleShot(1500, _start_tier_warmup)
    sys.exit(app.exec())

if __name__ == "__main__":
    run_app()
