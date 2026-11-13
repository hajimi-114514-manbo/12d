# -*- coding: utf-8 -*-
"""视频工作台（v3.0.0）：剪映式布局壳。

    ┌─ Pivot：媒体 | 音频 | 文本* | 贴纸* | 特效* | 转场* | 模板*（*=敬请期待）─┐
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
import json
import os
import time

import numpy as np

from PySide6.QtCore import Qt, QTimer, QSize, QSettings
from PySide6.QtGui import QImage, QPixmap, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                               QFileDialog, QStackedLayout, QFrame, QSplitter)
from qfluentwidgets import (Pivot, CardWidget, PrimaryPushButton, PushButton,
                            ToolButton, ComboBox, SpinBox, DoubleSpinBox,
                            StrongBodyLabel, BodyLabel, CaptionLabel,
                            LineEdit, ScrollArea, InfoBar, InfoBarPosition,
                            MessageBoxBase, ProgressBar, FluentIcon,
                            FlowLayout)

import video_model as V
import video_export as VE

_HERE = os.path.dirname(os.path.abspath(__file__))
PROJ_BASE = os.path.join(_HERE, "projects")
RECENT_FILE = os.path.join(_HERE, "recent_projects.json")
META_NAME = "video_project.json"

DEFAULT_DUR = {"image": 3.0, "video": 0, "puppet": 0, "audio": 0}  # 0=用素材全长

# ---------- 面板 splitter（可拖拽调宽高）----------
SETTINGS_NAME = "muppet_workbench.ini"
SPLIT_H_DEFAULT = [300, 880, 260]     # 素材库 | 预览 | 属性
SPLIT_H_MIN = [180, 420, 180]
SPLIT_V_DEFAULT = [560, 340]         # 上方区域 | 时间轴（约 38%）
SPLIT_V_MIN_TIMELINE = 120

# handle 颜色与主界面统一：idle #3a3f4a / hover+drag #4d88ff。
# 用 objectName 限定作用域，避免污染子控件里的其它 QSplitter。
_SPLIT_QSS = (
    "QSplitter#wbHSplit::handle { background: #3a3f4a; }"
    "QSplitter#wbHSplit::handle:hover, QSplitter#wbHSplit::handle:active"
    " { background: #4d88ff; }"
    "QSplitter#wbVSplit::handle { background: #3a3f4a; }"
    "QSplitter#wbVSplit::handle:hover, QSplitter#wbVSplit::handle:active"
    " { background: #4d88ff; }"
)


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
        self.img_lbl.setStyleSheet("background:#14161b;border-radius:4px;"
                                   "color:#8a93a2;")
        if thumb_qpixmap is not None and not thumb_qpixmap.isNull():
            self.img_lbl.setPixmap(thumb_qpixmap.scaled(
                self.img_lbl.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation))
        else:
            self.img_lbl.setText(source["kind"][:4])
        lay.addWidget(self.img_lbl)
        nm = os.path.basename(str(source["path"]).rstrip("\\/")) or "工程"
        t = BodyLabel(nm[:14], self)
        t.setStyleSheet("color:#c8d1dc;")
        f = t.font()
        f.setPixelSize(11)
        t.setFont(f)
        lay.addWidget(t)
        d = source.get("src_dur") or 0.0
        cap = CaptionLabel("图片" if source["kind"] == "image"
                           else fmt_time(d), self)
        cap.setStyleSheet("color:#8a93a2;")
        lay.addWidget(cap)
        lay.addStretch(1)

    def mouseDoubleClickEvent(self, ev):
        if self.on_add:
            self.on_add(self.source)
        super().mouseDoubleClickEvent(ev)


class VideoWorkbench(QWidget):
    """视频编辑工作台。project_dirty_changed(str) 通知外层改标题。"""

    def __init__(self, parent=None, proj=None, proj_dir=None):
        super().__init__(parent)
        self.proj = proj or V.new_project()
        self.proj_dir = proj_dir
        self.assets = []            # source dict 列表
        self._sel_clip = None       # 选中 clip id
        self._sources = VE.SourceManager()
        self._hist = []
        self._hist_i = -1
        self._t = 0.0
        self._playing = False
        self._tick_busy = False
        self._loading = False       # spin 回填防回环

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._advance)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # ---------- 顶部 Pivot ----------
        self.pivot = Pivot(self)
        for key, text in [("media", "媒体"), ("audio", "音频"),
                          ("text", "文本"), ("sticker", "贴纸"),
                          ("effect", "特效"), ("transition", "转场"),
                          ("template", "模板")]:
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
        # ---------- 中部三列（QSplitter 可拖拽调宽）----------
        hsplit = QSplitter(Qt.Horizontal, self)
        hsplit.setObjectName("wbHSplit")
        hsplit.setChildrenCollapsible(False)
        hsplit.setHandleWidth(6)
        hsplit.setStyleSheet(_SPLIT_QSS)

        # 左：素材库
        left = QFrame(self)
        left.setObjectName("assetPanel")
        left.setMinimumWidth(SPLIT_H_MIN[0])
        left.setStyleSheet("#assetPanel{background:#20232a;border-radius:8px;}")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 10, 10, 10)
        ll.setSpacing(8)
        lh = QHBoxLayout()
        lh.addWidget(StrongBodyLabel("素材库", left))
        lh.addStretch(1)
        ll.addLayout(lh)
        ab = QHBoxLayout()
        self.btn_add_file = PushButton("添加素材文件", left)
        self.btn_add_file.clicked.connect(self._add_files)
        self.btn_add_puppet = PushButton("导入木偶工程", left)
        self.btn_add_puppet.clicked.connect(self._add_puppet)
        ab.addWidget(self.btn_add_file)
        ab.addWidget(self.btn_add_puppet)
        ll.addLayout(ab)
        self.asset_scroll = ScrollArea(left)
        self.asset_scroll.setWidgetResizable(True)
        self.asset_host = QWidget()
        self.asset_flow = FlowLayout(self.asset_host, needAni=False)
        self.asset_flow.setContentsMargins(0, 0, 0, 0)
        self.asset_scroll.setWidget(self.asset_host)
        ll.addWidget(self.asset_scroll, 1)
        self.asset_empty = CaptionLabel("还没有素材\n点上方按钮导入，或双击卡片上轨",
                                        left)
        self.asset_empty.setAlignment(Qt.AlignCenter)
        ll.addWidget(self.asset_empty)
        hsplit.addWidget(left)

        # 中：预览
        center = QWidget(self)
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)
        self.preview = QLabel(center)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(320, 180)
        self.preview.setStyleSheet("background:#0d0f13;border-radius:6px;"
                                   "color:#5a6272;")
        self.preview.setText("预览")
        cl.addWidget(self.preview, 1)
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
        right.setObjectName("propPanel")
        right.setMinimumWidth(SPLIT_H_MIN[2])
        right.setStyleSheet("#propPanel{background:#20232a;border-radius:8px;}")
        rl = QVBoxLayout(right)
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
        self.spin_fps = SpinBox(right)
        self.spin_fps.setRange(1, 120)
        self.spin_fps.setValue(30)
        self.spin_fps.valueChanged.connect(self._fps_changed)
        r2.addWidget(self.spin_fps, 1)
        rl.addLayout(r2)
        self.dur_lbl = CaptionLabel("总时长：00:00.0", right)
        rl.addWidget(self.dur_lbl)
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
            sp = DoubleSpinBox(right)
            sp.setRange(0.0, 600.0)
            sp.setSingleStep(0.1)
            sp.setDecimals(2)
            sp.setSuffix(" s")
            sp.valueChanged.connect(lambda _v, k=lab: self._clip_spin_changed(k))
            setattr(self, "spin_" + lab, sp)
            row.addWidget(sp, 1)
            rl.addLayout(row)
        self.btn_del_clip = PushButton("删除选中片段", right)
        self.btn_del_clip.clicked.connect(self._delete_selected)
        rl.addWidget(self.btn_del_clip)
        rl.addStretch(1)
        hsplit.addWidget(right)
        hsplit.setStretchFactor(0, 0)
        hsplit.setStretchFactor(1, 2)      # 预览列 stretch 最大
        hsplit.setStretchFactor(2, 0)
        hsplit.setSizes(SPLIT_H_DEFAULT)
        self._hsplit = hsplit

        # ---------- 顶部区域（pv + 三列 splitter）----------
        top = QWidget(self)
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(0)
        tv.addWidget(pv)
        tv.addWidget(hsplit, 1)

        # ---------- 底部：clip 时间轴 ----------
        import clip_timeline as CT
        self.timeline = CT.ClipTimelinePanel(self, wb=self)
        self.timeline.seeked.connect(self.seek)
        self.timeline.selection_changed.connect(self._on_selection)
        self.timeline.edit_committed.connect(self._after_edit)
        self.timeline.setMinimumHeight(SPLIT_V_MIN_TIMELINE)

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
            try:
                os.makedirs(os.path.dirname(_settings_path), exist_ok=True)
            except Exception:
                pass
        self._settings = QSettings(_settings_path, QSettings.IniFormat)
        hsplit.splitterMoved.connect(lambda *a: self._save_splitter_sizes())
        vsplit.splitterMoved.connect(lambda *a: self._save_splitter_sizes())
        self._restore_splitter_sizes()

        v.addWidget(vsplit)

        # 快捷键：Ctrl+S 保存 / Ctrl+Z Y 撤销重做 / 空格播放
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save_project)
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.redo)
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self.toggle_play)

        self._apply_project(None)
        self._seed_history()

    # ================= splitter 尺寸持久化 =================
    def _save_splitter_sizes(self):
        """拖拽结束（splitterMoved）：把两个 splitter 的尺寸写入 QSettings。"""
        try:
            self._settings.setValue("splitter/horizontal", self._hsplit.sizes())
            self._settings.setValue("splitter/vertical", self._vsplit.sizes())
            self._settings.sync()
        except Exception:
            pass

    def _restore_splitter_sizes(self):
        """构造时恢复：校验列表长度与正数，否则用默认尺寸。"""
        try:
            hs = self._settings.value("splitter/horizontal")
            if isinstance(hs, (list, tuple)) and len(hs) == 3 \
                    and all(int(v) > 0 for v in hs):
                self._hsplit.setSizes([int(v) for v in hs])
            vs = self._settings.value("splitter/vertical")
            if isinstance(vs, (list, tuple)) and len(vs) == 2 \
                    and all(int(v) > 0 for v in vs):
                self._vsplit.setSizes([int(v) for v in vs])
        except Exception:
            pass

    # ================= 工程生命周期 =================
    def _refresh_all(self):
        """工程结构/属性变化后的统一刷新：时间轴 + 传输条 + 属性 + 预览。"""
        try:
            self.timeline.refresh()
        except Exception:
            pass
        self._update_transport()
        self._fill_clip_props()
        if not self._playing:
            self.render_frame(self._t)

    def _apply_project(self, _ignored):
        p = self.proj
        self.timeline.set_project(p)
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
        try:
            os.makedirs(self.proj_dir, exist_ok=True)
            with open(os.path.join(self.proj_dir, META_NAME), "w",
                      encoding="utf8") as f:
                json.dump(self.proj, f, ensure_ascii=False, indent=1)
            self._record_recent()
            InfoBar.success("已保存", os.path.basename(self.proj_dir),
                            parent=self, position=InfoBarPosition.TOP,
                            duration=2000)
            return True
        except Exception as e:
            InfoBar.error("保存失败", repr(e), parent=self,
                          position=InfoBarPosition.TOP, duration=4000)
            return False

    def save_as(self):
        """另存为：选目录 -> 写 video_project.json -> 切 proj_dir -> _record_recent。

        复用 save_project(ask=True) 的落盘逻辑，避免两份写盘代码。
        """
        return self.save_project(ask=True)

    def _record_recent(self):
        try:
            try:
                items = json.load(open(RECENT_FILE, encoding="utf8"))
                assert isinstance(items, list)
            except Exception:
                items = []
            d = os.path.normpath(self.proj_dir)
            items = [it for it in items
                     if os.path.normpath(it.get("dir", "")) != d]
            items.insert(0, {"dir": d, "name": os.path.basename(d),
                             "type": "video", "layers": 0})
            json.dump(items[:100], open(RECENT_FILE, "w", encoding="utf8"),
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
            InfoBar.error("打开失败", "video_project.json 读取异常：%r" % e,
                          parent=self, position=InfoBarPosition.TOP,
                          duration=4000)
            return False
        p = V.sanitize_project(obj)
        if p is None:
            InfoBar.error("打开失败", "不是合法的视频工程文件", parent=self,
                          position=InfoBarPosition.TOP, duration=4000)
            return False
        V.normalize_ids(p)
        self._sources.close_all()
        self.proj = p
        self.proj_dir = d
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
                InfoBar.warning("跳过素材", "%s\n%r" % (os.path.basename(p), e),
                                parent=self, position=InfoBarPosition.TOP,
                                duration=3000)
                continue
            if any(a["path"] == src["path"] and a["kind"] == src["kind"]
                   for a in self.assets):
                continue
            self.assets.append(src)
            n += 1
        if n:
            InfoBar.success("已导入", "%d 个素材" % n, parent=self,
                            position=InfoBarPosition.TOP, duration=2000)
        self._rebuild_asset_cards()

    def _thumb(self, src):
        """卡片缩略图：image/puppet/video 首帧。失败返回 None。"""
        try:
            import video_source as VS
            s = VS.open_source(src)
            try:
                arr = s.frame(0.0)
                qimg = QImage(np.ascontiguousarray(arr).data, arr.shape[1],
                              arr.shape[0], 3 * arr.shape[1],
                              QImage.Format_RGB888).copy()
                return QPixmap.fromImage(qimg)
            finally:
                s.close()
        except Exception:
            return None

    def _rebuild_asset_cards(self):
        while self.asset_flow.count():
            it = self.asset_flow.takeAt(0)
            # qfluentwidgets FlowLayout.takeAt 返回的就是 widget 本体（非 QLayoutItem）
            wi = it.widget() if hasattr(it, "widget") else it
            if isinstance(wi, QWidget):
                wi.deleteLater()
        filt = self._asset_tab
        if filt == "template":
            # v3.2.2：模板页占位——目前没有任何模板，只内置一个「导入模板」
            # 入口按钮（功能未实现，点击提示敬请期待）。后续接模板列表时替换本分支。
            btn = PrimaryPushButton("导入模板", self.asset_host)
            btn.clicked.connect(lambda: InfoBar.warning(
                "模板", "模板功能敬请期待", parent=self,
                position=InfoBarPosition.TOP, duration=2500))
            self.asset_flow.addWidget(btn)
            self.asset_empty.setVisible(False)
            return
        for src in self.assets:
            is_audio = src["kind"] == "audio"
            if filt == "audio" and not is_audio:
                continue
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

    def _filter_assets(self, key):
        self._asset_tab = str(key) if key else "media"
        self._rebuild_asset_cards()

    def _add_asset_to_timeline(self, src):
        """双击卡片：追加到对应轨末尾。"""
        kind = src["kind"]
        tid = "a1" if kind == "audio" else "v1"
        tr = V.find_track(self.proj, tid)
        if tr is None:
            ok, _m, tid = V.add_track(self.proj,
                                      "audio" if kind == "audio" else "video")
            if not ok:
                return
            tr = V.find_track(self.proj, tid)
        end = max([V.clip_end(c) for c in tr["clips"]] or [0.0])
        dur = DEFAULT_DUR.get(kind) or src.get("src_dur") or 3.0
        dur = max(V.MIN_DUR, min(dur, src["src_dur"]) if src.get("src_dur")
                  and kind != "image" else dur)
        clip = V.new_clip(src, end, dur)
        ok, cid = V.add_clip(self.proj, tid, clip)
        if not ok:
            InfoBar.warning("上轨失败", cid, parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
            return
        self.push_history()
        self.timeline.refresh()
        self._after_edit()

    # ================= 预览播放 =================
    def _preview_scale(self):
        return min(1.0, 720.0 / max(1, self.proj["height"]))

    def render_frame(self, t):
        arr = VE.compose_frame(self.proj, t, self._sources.get)
        sc = self._preview_scale()
        if sc < 1.0:
            import cv2
            arr = cv2.resize(arr, (max(2, int(arr.shape[1] * sc)),
                                   max(2, int(arr.shape[0] * sc))),
                             interpolation=cv2.INTER_AREA)
        qimg = QImage(np.ascontiguousarray(arr).data, arr.shape[1],
                      arr.shape[0], 3 * arr.shape[1],
                      QImage.Format_RGB888).copy()
        self.preview.setPixmap(QPixmap.fromImage(qimg))

    def seek(self, t):
        self._t = float(max(0.0, min(t, V.project_duration(self.proj))))
        self.timeline.set_time(self._t)
        self.render_frame(self._t)
        self._update_transport()

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

    def stop_play(self):
        self._playing = False
        self.timer.stop()
        self.btn_play.setIcon(FluentIcon.PLAY.icon())

    def _update_transport(self):
        dur = V.project_duration(self.proj)
        self.time_lbl.setText("%s / %s" % (fmt_time(self._t), fmt_time(dur)))
        self.sld_pos.setValue(int(round(self._t / dur * 1000)) if dur > 0 else 0)
        self.dur_lbl.setText("总时长：%s" % fmt_time(dur))

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
        self._loading = True
        if on:
            self.clip_info.setText("%s\n%s" % (
                os.path.basename(str(c["source"]["path"]).rstrip("\\/")),
                c["source"]["kind"]))
            self.spin_start.setValue(c["start"])
            self.spin_in.setValue(c["src_in"])
            self.spin_dur.setValue(c["dur"])
        else:
            self.clip_info.setText("未选中")
            for sp in (self.spin_start, self.spin_in, self.spin_dur):
                sp.setValue(0.0)
        self._loading = False

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
            InfoBar.warning("修改被拒", msg, parent=self,
                            position=InfoBarPosition.TOP, duration=2500)
            self._fill_clip_props()
            return
        self.push_history()
        self.timeline.refresh()
        self._after_edit()

    def _delete_selected(self):
        if self._sel_clip:
            ok, _m = V.remove_clip(self.proj, self._sel_clip)
            if ok:
                self._sel_clip = None
                self.push_history()
                self.timeline.refresh()
                self._after_edit()

    def _after_edit(self):
        """任何成功编辑后：刷新预览/时长/属性。"""
        self._update_transport()
        self._fill_clip_props()
        if not self._playing:
            self.render_frame(self._t)

    # ================= 导出 =================
    def _open_export_dialog(self):
        dlg = _ExportDialog(self, self.proj)
        if dlg.exec():
            path, scale = dlg.result_values()
            self._start_export(path, scale)

    def _start_export(self, path, scale):
        self.stop_play()
        self.act_export.setEnabled(False)
        self._export_worker = VE.VideoExportWorker(self.proj, path, scale,
                                                   parent=self)
        self._export_worker.progress.connect(self._export_progress)
        self._export_worker.done.connect(self._export_done)
        self._export_worker.start()

    def _export_progress(self, pct):
        self.act_export.setText("%d%%" % pct)

    def _export_done(self, ok, msg):
        self.act_export.setEnabled(True)
        self.act_export.setText("导出")
        (InfoBar.success if ok else InfoBar.error)(
            "导出完成" if ok else "导出失败", msg, parent=self,
            position=InfoBarPosition.TOP, duration=5000)


class _ExportDialog(MessageBoxBase):
    def __init__(self, parent, proj):
        super().__init__(parent)
        self.viewLayout.addWidget(StrongBodyLabel("导出视频"))
        r = QHBoxLayout()
        r.addWidget(CaptionLabel("保存路径"))
        default = os.path.join(
            PROJ_BASE, (os.path.basename(parent.proj_dir)
                        if getattr(parent, "proj_dir", None)
                        else unique_video_name()) + ".mp4")
        self.ed_path = LineEdit()
        self.ed_path.setText(default)
        r.addWidget(self.ed_path, 1)
        self.viewLayout.addLayout(r)
        r2 = QHBoxLayout()
        r2.addWidget(CaptionLabel("分辨率"))
        self.cmb_out = ComboBox()
        self.cmb_out.addItems(["工程原始 (%d×%d)" % (proj["width"], proj["height"]),
                               "1080p (75%)", "720p (50%)", "480p (33%)"])
        r2.addWidget(self.cmb_out, 1)
        self.viewLayout.addLayout(r2)
        self.yesButton.setText("开始导出")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(480)

    def result_values(self):
        scale = (1.0, 0.75, 0.5, 0.33)[max(0, self.cmb_out.currentIndex())]
        return os.path.normpath(self.ed_path.text().strip()), scale

# 注：窗口壳（FluentWindow 包装）在 l2d_rig_qt.FluentShellWindow 里做，
# 本模块只出纯 QWidget 工作台，避免双向 import。
