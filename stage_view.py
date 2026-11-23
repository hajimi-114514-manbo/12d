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
import time

import numpy as np
from PySide6.QtCore import Qt, Signal, QRectF, QTimer
from PySide6.QtGui import (QImage, QPixmap, QPainter, QColor, QPen, QFont,
                           QLinearGradient as _LinearGrad)
from PySide6.QtWidgets import QWidget, QSizePolicy

import video_model as V
import video_export as VE
import ui_theme as TH

# ---- 主题色（v3.x：跟随 ui_theme；占位值导入时立刻被 _refresh_colors 覆盖）----
# paintEvent 每帧读全局名，TH 切主题时统一 update() 全部控件即刻生效。
# 注意：_draw_subtitle 里的黑描边/白字/蓝名牌是**合成画面的一部分**
# （预览与导出共用），属语义色，不跟随界面主题。
C_CANVAS = QColor("#0d0f13")   # 画布外底板（TH.qcolor("canvas_bg")）
C_ACCENT = QColor("#4d88ff")   # 选中虚线框 + 缩放手柄（TH.qcolor("accent")）


def _refresh_colors():
    global C_CANVAS, C_ACCENT
    C_CANVAS = TH.qcolor("canvas_bg")
    C_ACCENT = TH.qcolor("accent")


TH.register(_refresh_colors)
_refresh_colors()


class StageView(QWidget):
    clip_selected = Signal(str)      # 选中的 clip id（空串=取消）
    layout_changed = Signal()        # 拖拽/属性改动后通知外层 push_history + 重渲染

    _HANDLE = 8                       # 手柄边长（widget 像素）
    _OUT = 0.05                       # x/y 越界钳（归一化）
    _SCL_MIN, _SCL_MAX = 0.1, 10.0
    # v3.5.2：pixmap 重建最小间隔（毫秒）。预览 30fps 足够（33ms/帧），
    # 这里取 28 给高刷屏留余量；仅节流 pixmap 转换，末帧用定时器补偿。
    _FRAME_MIN_MS = 28.0

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
    def set_frame(self, arr, proj, t, aid=None):
        """喂一帧（画布全分辨率 ndarray）+ 工程 + 时刻。

        F2-L5（v3.5）：加 aid —— 多幕工程里舞台预览只显示**当前幕**
        （一幕一条独立时间轴）。v1 单幕传 None 即可，行为与旧版全等。

        v3.5.2 性能：pixmap 重建可以节流，但**末帧必须落地**。
        做法：距上次重建不足 _FRAME_MIN_MS 时只记脏标记 + 起一个短定时器，
        到点再补建；期间 _frame/t 已经是最新值，画面不会错，只是晚 ≤33ms。
        连续拖时间轴时省掉大量中间帧的降采样+fromImage（实测 2ms/次）。"""
        self._frame = arr
        self.proj = proj
        self._aid = aid
        self.t = float(t)
        now = time.perf_counter()
        since = (now - getattr(self, "_last_build", 0.0)) * 1000.0
        if self._pixmap is None or since >= self._FRAME_MIN_MS:
            self._last_build = now
            self._pending_frame = False
            self._build_pixmap()
        else:
            # 太近：先不建，等定时器补（保证最终一定画出最新帧）
            self._pending_frame = True
            if not getattr(self, "_flush_timer", None):
                self._flush_timer = QTimer(self)
                self._flush_timer.setSingleShot(True)
                self._flush_timer.timeout.connect(self._flush_frame)
            delay = max(1, int(self._FRAME_MIN_MS - since))
            self._flush_timer.start(delay)
        self.update()

    def _flush_frame(self):
        """延时补建：把节流期间攒下的最新帧画出来。"""
        if not getattr(self, "_pending_frame", False): return
        self._pending_frame = False
        self._last_build = time.perf_counter()
        self._build_pixmap()
        self.update()

    def _view(self):
        """当前幕投影（v1 = 顶层，v2 = 该幕的 tracks/dialogue）。

        必须走 act_view：v2 多幕工程顶层没有 tracks/dialogue，
        直接 proj.get("tracks") 会让舞台预览全空。"""
        if not self.proj: return {"tracks": [], "dialogue": []}
        try:
            return V.act_view(self.proj, getattr(self, "_aid", None))
        except Exception:
            return {"tracks": [], "dialogue": []}

    def _build_pixmap(self):
        if self._frame is None:
            self._pixmap = None
            return
        h, w = self._frame.shape[:2]
        if w <= 0 or h <= 0:
            self._pixmap = None
            return
        # v3.5.2 性能重构（实测数据驱动，microbench 见 perf_probe.py）：
        # 原来 = ascontiguousarray(全量) + QImage.copy(1920x1080)【3.2ms】
        #        + QPixmap.fromImage + paintEvent 里再 SmoothTransformation。
        # 三项都是**固定开销**，空工程也要 3.9ms/帧 —— "预览卡"的主因。
        # 关键发现：**QImage.copy() 是最贵的一步且多余** ——
        # QPixmap.fromImage() 本来就会拷像素，QImage 只是中间视图。
        # 新做法：
        #   1) numpy 步进降采样到「显示尺寸附近」——纯 stride 切片，零拷贝
        #   2) QImage 只包一层视图（不 copy），fromImage 时 Qt 自己拷
        step = self._downscale_step(w, h)
        if step > 1: small = self._block_downscale(self._frame, step)
        else:
            small = (self._frame if self._frame.flags["C_CONTIGUOUS"]
                     else np.ascontiguousarray(self._frame))
        sh, sw = small.shape[:2]
        qimg = QImage(small.data, sw, sh, 3 * sw, QImage.Format_RGB888)
        self._pixmap = QPixmap.fromImage(qimg)

    @staticmethod
    def _block_downscale(arr, step):
        """步进取样降采样，输出**连续**数组。

        尝试过的方案（都在 1080p 上实测过，别走回头路）：
          - `arr[::step, ::step]`：非连续视图，QImage 不吃，还得
            ascontiguousarray 拷一遍（1.81ms，比全量拷还贵）-> 否
          - 块平均（reshape + mean + astype）：画质最好但 53ms，
            float32 中间数组 + 两次全量遍历，比拷全图贵 30 倍 -> 否
          - `arr[::step, ::step].copy()`：直接拷贝到连续内存，
            **一步到位 0.9ms**，画质用取样的底噪换取速度，预览够用 -> 采用
        """
        h, w = arr.shape[:2]
        h2, w2 = (h // step) * step, (w // step) * step
        if h2 <= 0 or w2 <= 0: return arr
        core = arr[:h2, :w2]
        return np.ascontiguousarray(core[::step, ::step])

    def _downscale_step(self, w, h):
        """降到「显示尺寸 * 1.5」所需的整数步长（1 = 不降）。

        预览区在 1500×940 窗口里约 750×420；画布 1920×1080 需约 2.5x
        降采样。用整数步长（1/2/3/4）而不是精确缩放：便宜且够清晰，
        paintEvent 再用 SmoothPixmapTransform 轻微拉伸补足。"""
        tw, th = self._target_px()
        if tw <= 0 or th <= 0: return 1
        step = 1
        while (w // (step + 1) >= tw and h // (step + 1) >= th):
            step += 1
        return step

    def _target_px(self):
        """预览区实际显示尺寸（画布等比 fit 进 widget）。

        v3.5.2 实测：**不要**在这里加冗余系数。原打算乘 1.5 保画质，结果
        730px 的预览区算出 1095px 目标 —— 1920 降不下来（step 仍是 1），
        每帧白付 1.75ms 的 fromImage 全量拷贝。乘 0.9 让索引正好跨过
        整数步长门槛（1920/2=960 < 730*0.9=657? 不，960>657 仍不够；
        真实效果是 1080 高度先掉到 step=2），实测 set_frame 4.0->2.7ms。
        轻微放大由 paintEvent 的 SmoothPixmapTransform 补，肉眼无差别。"""
        try:
            W = int((self.proj or {}).get("width") or 1920)
            H = int((self.proj or {}).get("height") or 1080)
        except Exception:
            return (0, 0)
        cw, ch = self.width(), self.height()
        if W <= 0 or H <= 0 or cw < 8 or ch < 8: return (0, 0)
        s = min(cw / float(W), ch / float(H))
        return (max(1, int(W * s)), max(1, int(H * s)))

    def _geom(self):
        """fit：画布像素 -> widget 像素。返回 (ox, oy, scale, dw, dh)。"""
        cw, ch = self.width(), self.height()
        if self.proj is None or self._pixmap is None: return (0, 0, 0.0, 0, 0)
        W, H = int(self.proj["width"]), int(self.proj["height"])
        if W <= 0 or H <= 0 or cw < 4 or ch < 4: return (0, 0, 0.0, 0, 0)
        s = min(cw / W, ch / H)
        dw, dh = int(W * s), int(H * s)
        return ((cw - dw) // 2, (ch - dh) // 2, s, dw, dh)

    def _c2w(self, cx, cy):
        ox, oy, s, _, _ = self._geom()
        return (ox + cx * s, oy + cy * s)

    def _w2c(self, wx, wy):
        ox, oy, s, _, _ = self._geom()
        if s <= 0: return (0.0, 0.0)
        return ((wx - ox) / s, (wy - oy) / s)

    # ---------- 命中 ----------
    def _active_clips(self):
        """t 时刻生效的视频 clip，按叠放顺序自顶向下（topmost 优先）。"""
        eps = 1e-6
        out = []
        if not self.proj: return out
        for ti, tr in enumerate(self._view()["tracks"]):
            if tr.get("kind") != "video" or tr.get("hidden"): continue
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
        if not self.proj: return out
        for d in (self._view()["dialogue"] or []):
            if not isinstance(d, dict): continue
            try:
                t = float(d.get("t"))
                dur = float(d.get("dur"))
            except Exception:
                continue
            if t - eps <= self.t < t + dur - eps: out.append(d)
        return out

    def _rect_widget(self, c):
        lay = V.layout_at(c)
        src = c.get("source") or {}
        w, h = int(src.get("w") or 0), int(src.get("h") or 0)
        if w <= 0 or h <= 0 or not self.proj: return None
        px, py, pw, ph = VE.layer_rect(w, h, int(self.proj["width"]),
                                       int(self.proj["height"]), lay)
        x0, y0 = self._c2w(px, py)
        x1, y1 = self._c2w(px + pw, py + ph)
        return (x0, y0, x1, y1)

    def _clip(self, cid):
        if not self.proj: return None
        for tr in self._view()["tracks"]:
            for c in tr.get("clips", []):
                if c.get("id") == cid: return c
        return None

    def _hit(self, wx, wy):
        for c in self._active_clips():
            r = self._rect_widget(c)
            if r is None: continue
            if r[0] <= wx <= r[2] and r[1] <= wy <= r[3]: return c["id"]
        return None

    def _is_handle(self, wx, wy):
        if not self._selected: return False
        r = self._rect_widget(self._clip(self._selected))
        if r is None: return False
        return (abs(wx - r[2]) <= self._HANDLE and
                abs(wy - r[3]) <= self._HANDLE)

    def _busy(self):
        return bool(self.playing or self.tick_busy)

    # ---------- 写回 ----------
    def _write_layout(self, cid, lay):
        c = self._clip(cid)
        if c is None: return
        if not isinstance(c.get("layout"), dict): c["layout"] = V.default_layout()
        for k in ("x", "y", "scale", "rot"):
            c["layout"][k] = lay.get(k, c["layout"].get(k))

    # ---------- 鼠标 ----------
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or self._busy(): return
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
        if not self._drag or self._busy(): return
        wx, wy = e.position().x(), e.position().y()
        c = self._clip(self._drag["clip_id"])
        if c is None: return
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
        """台词正文字体（MyGO 风大字，随 widget 宽度缩放，钳 14-32pt）。

        v3.5.2：缓存 QFont —— 原来每帧 new 一个 QFont（构造+设置属性
        有成本），而字号只随宽度变，宽度不变就该复用。"""
        cw = max(1, self.width())
        px = max(14, min(32, cw // 38))
        cache = getattr(self, "_font_cache", None)
        if cache is not None and cache[0] == px: return cache[1]
        f = QFont("Microsoft YaHei", px)
        self._font_cache = (px, f)
        return f

    def _wrap_text(self, painter, text, max_w):
        """QFontMetrics 手算自动换行（禁 QGraphicsTextItem 场景层）。
        逐字符累积贪心断行；超长无空格中文串也能断。返回行列表。

        v3.5.2：结果按 (text, max_w, fontpx) 缓存。台词 300 字时原实现
        每帧要做 300 次 horizontalAdvance（字体度量不便宜）；拖动播放头/
        播放时同一句台词会被重算几十次。缓存后只在换句或改窗口宽时重算。"""
        if not text: return [""]
        fm = painter.fontMetrics()
        fpx = self._dialog_font().pointSize()
        key = (text, int(max_w), fpx)
        cache = getattr(self, "_wrap_cache", None)
        if cache is not None and cache[0] == key: return cache[1]
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
        out = lines or [""]
        self._wrap_cache = (key, out)
        return out

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
        if not items: return
        try:
            d = items[-1]
            who = str(d.get("who") or "")
            text = str(d.get("text") or "")
        except Exception:
            return
        if not text: return
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
                    if dx == 0 and dy == 0: continue
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
        painter.fillRect(self.rect(), C_CANVAS)
        ox, oy, s, dw, dh = self._geom()
        if self._pixmap is not None and dw > 0 and dh > 0:
            # v3.5.2 性能：pixmap 已在 _build_pixmap 里缩到接近显示尺寸，
            # 这里**直接 drawPixmap 到目标矩形**，不再每帧 SmoothTransformation
            # 缩放（那是每次重绘一次的 CPU 密集操作，比合成本身贵两个数量级）。
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawPixmap(QRectF(ox, oy, dw, dh), self._pixmap,
                               QRectF(self._pixmap.rect()))
        if self._selected:
            r = self._rect_widget(self._clip(self._selected))
            if r is not None:
                painter.setPen(QPen(C_ACCENT, 2, Qt.DashLine))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(QRectF(r[0], r[1], r[2] - r[0], r[3] - r[1]))
                painter.setPen(Qt.NoPen)
                painter.setBrush(C_ACCENT)
                painter.drawRect(QRectF(r[2] - self._HANDLE,
                                        r[3] - self._HANDLE,
                                        self._HANDLE, self._HANDLE))
        self._draw_subtitle(painter)
        painter.end()

