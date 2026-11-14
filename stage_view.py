# -*- coding: utf-8 -*-
"""A2 StageView 舞台画布（可拖拽布置 + 选中联动）。

画当前 t 时刻合成帧（QPixmap，由 workbench.render_frame 喂 ndarray），叠加：
  - 选中对象的虚线框 + 右下角 8px 手柄
  - 点选命中 = 自顶向下 layer_rect 反查（topmost 优先），emit clip_selected
  - 拖动主体 = 改 layout x/y（边界钳 ±0.05 出画）
  - 拖手柄 = 改 layout scale（钳 0.1..10，对锚点保持中心不动，v1 不做锚点）
  - release 后 emit layout_changed（外层 push_history）

播放中禁交互（playing / tick_busy 时跳过鼠标），但照常刷新框。

TODO(C 方案)：旋转/裁剪/透明度/关键帧等自由变换工具一律不做，
            布局关键帧只改 video_model.layout_at 内部采样插值。

布局读写契约：读一律走 video_model.layout_at；写一律原地改 clip["layout"]。
"""
import math

import numpy as np
from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import (QImage, QPixmap, QPainter, QColor, QPen, QFont,
                           QLinearGradient as _LinearGrad)
from PySide6.QtWidgets import QWidget, QSizePolicy

import video_model as V
import video_export as VE


class StageView(QWidget):
    clip_selected = Signal(str)      # 选中的 clip id（空串=取消）
    layout_changed = Signal()        # 拖拽/属性改动后通知外层 push_history + 重渲染

    _HANDLE = 8                       # 手柄边长（widget 像素）
    _OUT = 0.05                       # x/y 越界钳（归一化）
    _SCL_MIN, _SCL_MAX = 0.1, 10.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._frame = None
        self._pixmap = None
        self.proj = None
        self.t = 0.0
        self._selected = None
        self._drag = None              # dict(mode, clip_id, start_lay, start_wx, start_wy)
        self.playing = False           # workbench 设置：播放中禁交互
        self.tick_busy = False         # workbench 设置：逐帧合成中禁交互

    # ---------- 帧 ----------
    def set_frame(self, arr, proj, t):
        """喂一帧（画布全分辨率 ndarray）+ 工程 + 时刻。"""
        self._frame = arr
        self.proj = proj
        self.t = float(t)
        self._build_pixmap()
        self.update()

    def _build_pixmap(self):
        if self._frame is None:
            self._pixmap = None
            return
        arr = np.ascontiguousarray(self._frame)
        h, w = arr.shape[:2]
        if w <= 0 or h <= 0:
            self._pixmap = None
            return
        qimg = QImage(arr.data, w, h, 3 * w,
                      QImage.Format_RGB888).copy()
        self._pixmap = QPixmap.fromImage(qimg)

    def _geom(self):
        """fit：画布像素 -> widget 像素。返回 (ox, oy, scale, dw, dh)。"""
        cw, ch = self.width(), self.height()
        if self.proj is None or self._pixmap is None:
            return (0, 0, 0.0, 0, 0)
        W, H = int(self.proj["width"]), int(self.proj["height"])
        if W <= 0 or H <= 0 or cw < 4 or ch < 4:
            return (0, 0, 0.0, 0, 0)
        s = min(cw / W, ch / H)
        dw, dh = int(W * s), int(H * s)
        return ((cw - dw) // 2, (ch - dh) // 2, s, dw, dh)

    def _c2w(self, cx, cy):
        ox, oy, s, _, _ = self._geom()
        return (ox + cx * s, oy + cy * s)

    def _w2c(self, wx, wy):
        ox, oy, s, _, _ = self._geom()
        if s <= 0:
            return (0.0, 0.0)
        return ((wx - ox) / s, (wy - oy) / s)

    # ---------- 命中 ----------
    def _active_clips(self):
        """t 时刻生效的视频 clip，按叠放顺序自顶向下（topmost 优先）。"""
        eps = 1e-6
        out = []
        if not self.proj:
            return out
        for ti, tr in enumerate(self.proj.get("tracks", [])):
            if tr.get("kind") != "video" or tr.get("hidden"):
                continue
            for c in tr.get("clips", []):
                if c["start"] - eps <= self.t < c["start"] + c["dur"] - eps:
                    out.append((ti, c["start"], c))
        out.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [c for _, _, c in out]

    def _active_dialogues(self):
        """t 时刻生效的台词条目（与 _active_clips 同款时间过滤）。

        A4：dialogue = [{"t","dur","who","text"}]，WebGAL 导出与字幕共用。"""
        eps = 1e-6
        out = []
        if not self.proj:
            return out
        for d in (self.proj.get("dialogue") or []):
            if not isinstance(d, dict):
                continue
            try:
                t = float(d.get("t"))
                dur = float(d.get("dur"))
            except Exception:
                continue
            if t - eps <= self.t < t + dur - eps:
                out.append(d)
        return out

    def _rect_widget(self, c):
        lay = V.layout_at(c)
        src = c.get("source") or {}
        w, h = int(src.get("w") or 0), int(src.get("h") or 0)
        if w <= 0 or h <= 0 or not self.proj:
            return None
        px, py, pw, ph = VE.layer_rect(w, h, int(self.proj["width"]),
                                       int(self.proj["height"]), lay)
        x0, y0 = self._c2w(px, py)
        x1, y1 = self._c2w(px + pw, py + ph)
        return (x0, y0, x1, y1)

    def _clip(self, cid):
        if not self.proj:
            return None
        for tr in self.proj.get("tracks", []):
            for c in tr.get("clips", []):
                if c.get("id") == cid:
                    return c
        return None

    def _hit(self, wx, wy):
        for c in self._active_clips():
            r = self._rect_widget(c)
            if r is None:
                continue
            if r[0] <= wx <= r[2] and r[1] <= wy <= r[3]:
                return c["id"]
        return None

    def _is_handle(self, wx, wy):
        if not self._selected:
            return False
        r = self._rect_widget(self._clip(self._selected))
        if r is None:
            return False
        return (abs(wx - r[2]) <= self._HANDLE and
                abs(wy - r[3]) <= self._HANDLE)

    def _busy(self):
        return bool(self.playing or self.tick_busy)

    # ---------- 写回 ----------
    def _write_layout(self, cid, lay):
        c = self._clip(cid)
        if c is None:
            return
        if not isinstance(c.get("layout"), dict):
            c["layout"] = V.default_layout()
        for k in ("x", "y", "scale", "rot"):
            c["layout"][k] = lay.get(k, c["layout"].get(k))

    # ---------- 鼠标 ----------
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or self._busy():
            return
        wx, wy = e.position().x(), e.position().y()
        if self._is_handle(wx, wy):
            c = self._clip(self._selected)
            self._drag = {"mode": "scale", "clip_id": self._selected,
                          "start_lay": dict(V.layout_at(c)),
                          "start_wx": wx, "start_wy": wy}
            self.setCursor(Qt.SizeHorCursor)
            e.accept()
            return
        hit = self._hit(wx, wy)
        if hit is not None:
            self._selected = hit
            self.clip_selected.emit(hit)
            self._drag = {"mode": "move", "clip_id": hit,
                          "start_lay": dict(V.layout_at(self._clip(hit))),
                          "start_wx": wx, "start_wy": wy}
            self.setCursor(Qt.ClosedHandCursor)
        else:
            self._selected = None
            self.clip_selected.emit("")
        self.update()
        e.accept()

    def mouseMoveEvent(self, e):
        if not self._drag or self._busy():
            return
        wx, wy = e.position().x(), e.position().y()
        c = self._clip(self._drag["clip_id"])
        if c is None:
            return
        lay = dict(self._drag["start_lay"])
        cx, cy = self._w2c(wx, wy)
        if self._drag["mode"] == "move":
            sx, sy = self._w2c(self._drag["start_wx"], self._drag["start_wy"])
            W = int(self.proj["width"]) if self.proj else 1
            H = int(self.proj["height"]) if self.proj else 1
            lay["x"] = max(-self._OUT, min(1.0 + self._OUT,
                                           lay["x"] + (cx - sx) / W))
            lay["y"] = max(-self._OUT, min(1.0 + self._OUT,
                                           lay["y"] + (cy - sy) / H))
        else:   # scale：对中心保持不动，按距离比缩放
            ccx, ccy = (lay["x"] * self.proj["width"],
                        lay["y"] * self.proj["height"]) \
                if self.proj else (0.0, 0.0)
            sx0, sy0 = self._w2c(self._drag["start_wx"], self._drag["start_wy"])
            d0 = max(1e-6, math.hypot(sx0 - ccx, sy0 - ccy))
            d1 = max(1e-6, math.hypot(cx - ccx, cy - ccy))
            lay["scale"] = max(self._SCL_MIN,
                               min(self._SCL_MAX, lay["scale"] * (d1 / d0)))
        self._write_layout(self._drag["clip_id"], lay)
        self.update()
        e.accept()

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = None
            self.unsetCursor()
            self.layout_changed.emit()
        self.update()

    # ---------- 绘制 ----------
    # MyGO 风台词皮肤常量（I2u；预览视觉，导出以 WebGAL 引擎渲染为准）
    _DLG_MAX_LINES = 4          # 高度自适应上限行数（2-4 行）
    _SUB_PAD_X = 28             # 正文两侧内边距（widget 像素）
    _SUB_MARGIN_B = 26          # 文字块距 widget 底边
    _SUB_OUTLINE = 3            # 黑色描边宽度（8 方向偏移）

    def _dialog_font(self):
        """台词正文字体（MyGO 风大字，随 widget 宽度缩放，钳 14-32pt）。"""
        cw = max(1, self.width())
        return QFont("Microsoft YaHei", max(14, min(32, cw // 38)))

    def _wrap_text(self, painter, text, max_w):
        """QFontMetrics 手算自动换行（禁 QGraphicsTextItem 场景层）。
        逐字符累积贪心断行；超长无空格中文串也能断。返回行列表。"""
        if not text:
            return [""]
        fm = painter.fontMetrics()
        lines, cur, cur_w = [], "", 0
        for ch in text:
            w = fm.horizontalAdvance(ch)
            if ch == "\n":
                lines.append(cur)
                cur, cur_w = "", 0
                continue
            if cur and cur_w + w > max_w:
                lines.append(cur)
                cur, cur_w = ch, w
            else:
                cur += ch
                cur_w += w
        lines.append(cur)
        return lines or [""]

    def _draw_subtitle(self, painter):
        """底部台词（MyGO 风，I2u）：**无底框**、白色大描边字直接压画面。

        【预览皮肤，导出效果以 WebGAL 引擎为准】——本函数被 preview 与
        MP4 渲染共用，一处改两处生效。

        视觉规格（对齐用户提供的 MyGO 二创截图）：
          - **无底框**：不画对话框背景，文字直接压在画面上
          - 正文白色 + 黑色描边（8 方向偏移 drawText，约 3px）+ 加粗大字号
          - 名字 = 蓝色圆角胶囊牌（#4c88ff）白字，居中于文字块上方；
            who 为空（旁白）不画牌，纯文字
          - 文字水平居中贴底，QFontMetrics 手算自动换行（禁 QGraphicsTextItem）
          - 静态整行绘制，同台词行数逐帧不变 -> 不抖
        """
        items = self._active_dialogues()
        if not items:
            return
        try:
            d = items[-1]
            who = str(d.get("who") or "")
            text = str(d.get("text") or "")
        except Exception:
            return
        if not text:
            return
        painter.setFont(self._dialog_font())
        fm = painter.fontMetrics()
        line_h = fm.height()
        cw = max(1, self.width())
        ch = max(1, self.height())
        pad_x = self._SUB_PAD_X
        margin_b = self._SUB_MARGIN_B
        outline = self._SUB_OUTLINE
        max_w = cw - 2 * pad_x
        lines = self._wrap_text(painter, text, max_w)[:self._DLG_MAX_LINES]
        block_h = len(lines) * line_h
        block_bottom = ch - margin_b
        block_top = block_bottom - block_h
        # ---- 正文：白字 + 8 方向黑描边，逐行 ----
        for i, ln in enumerate(lines):
            y = block_top + i * line_h
            rect = QRectF(pad_x, y, cw - 2 * pad_x, line_h)
            for dx in (-outline, 0, outline):
                for dy in (-outline, 0, outline):
                    if dx == 0 and dy == 0:
                        continue
                    painter.setPen(QColor(0, 0, 0, 235))
                    painter.drawText(rect.translated(dx, dy),
                                     Qt.AlignLeft | Qt.AlignVCenter, ln)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, ln)
        # ---- 名字牌：蓝色圆角胶囊，居中于文字块上方；旁白不画 ----
        if who:
            nf = QFont(self._dialog_font())
            nf.setBold(True)
            nf.setPointSizeF(max(10.0, painter.font().pointSizeF() - 1.0))
            painter.setFont(nf)
            nfm = painter.fontMetrics()
            nw = nfm.horizontalAdvance(who) + 28
            nh = nfm.height() + 10
            nx = (cw - nw) / 2.0
            ny = block_top - nh - 10
            nb = QRectF(nx, ny, nw, nh)
            radius = nh / 2.0                     # 胶囊：半径=半高
            painter.setPen(QPen(QColor(255, 255, 255, 60), 1))
            painter.setBrush(QColor(76, 136, 255, 235))   # #4c88ff
            painter.drawRoundedRect(nb, radius, radius)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(nb, Qt.AlignCenter, who)

    def paintEvent(self, e):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0d0f13"))
        ox, oy, s, dw, dh = self._geom()
        if self._pixmap is not None and dw > 0 and dh > 0:
            painter.drawPixmap(ox, oy, self._pixmap.scaled(
                dw, dh, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        if self._selected:
            r = self._rect_widget(self._clip(self._selected))
            if r is not None:
                painter.setPen(QPen(QColor("#4d88ff"), 2, Qt.DashLine))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(QRectF(r[0], r[1], r[2] - r[0], r[3] - r[1]))
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#4d88ff"))
                painter.drawRect(QRectF(r[2] - self._HANDLE,
                                        r[3] - self._HANDLE,
                                        self._HANDLE, self._HANDLE))
        self._draw_subtitle(painter)
        painter.end()
