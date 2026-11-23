# -*- coding: utf-8 -*-
"""欢迎页「资产管理」对话框：列出所有可下载包 + 状态 + 一键下载/更新。

为什么单独一个模块：
  · 工作台里早有「资产管理」菜单（l2d_rig_qt.AssetMenuMixin），但那是**菜单**，
    挂在编辑页顶部，且按当前编辑页过滤（skeleton 页看不到 WebGAL 运行时）。
    欢迎页没有编辑页上下文，需要的是**全量、平铺、能看规模**的视图。
  · welcome_window 不能 import l2d_rig_qt（主模块，会拖起整个工作室 + torch），
    所以这里只依赖 asset_catalog / asset_downloader / asset_paths。

数据来源：asset_catalog.ASSETS（7 个包），状态走实时 is_installed 检查，
不用缓存——用户可能在对话框开着时刚下完。
"""
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QWidget,
                               QTableWidgetItem, QHeaderView, QAbstractItemView)

from qfluentwidgets import (PushButton, PrimaryPushButton, TableWidget,
                            StrongBodyLabel, CaptionLabel, BodyLabel,
                            InfoBar, InfoBarPosition, FluentIcon)

import ui_theme as TH
import ui_toast as TOAST

# base_dir = 本文件所在目录（与 l2d_rig_qt.V2 同义，但不用 import 主模块）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _catalog():
    """惰性 import：asset_catalog 会读 release 清单，放在模块级会拖慢启动。"""
    import asset_catalog as AC
    return AC


def _downloader():
    import asset_downloader as AD
    return AD


def _lib_dir():
    """素材库根（资产实际落盘处）。取不到就返回 None，由调用方兜底。"""
    try:
        import asset_paths as AP
        return AP.ensure_library(scan=False)
    except Exception:
        return None


def _human(n):
    """字节 -> 人类可读。"""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "-"
    if n <= 0: return "-"
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%.0f %s" % (n, u)) if u in ("B", "KB") else ("%.1f %s" % (n, u))
        n /= 1024.0
    return "%.1f GB" % n


# 每种包的类型中文名（asset_catalog 的 kind 字段）
_KIND_CN = {
    "runtime": "运行环境",
    "model": "模型",
    "accel": "推理加速",
    "python_pack": "组件包",
    "runtime_pack": "引擎运行时",
}


class AssetManagerDialog(QDialog):
    """可下载包总览：名称 / 类型 / 体积 / 状态 / 操作。

    actions:
      · 未装      -> 「下载」（走 asset_downloader.AssetDownloadDialog，有进度）
      · 已装      -> 显示「已安装」+ 占用
      · 缺少文件  -> 「补全」（部分解压失败时）
    """

    COLS = ("名称", "类型", "下载体积", "状态", "操作")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("资产管理")
        self.setObjectName("assetManager")
        self.resize(860, 520)
        self._busy = set()          # 正在下载的 asset id（防重入）
        self._assets = []

        vl = QVBoxLayout(self)
        vl.setContentsMargins(20, 18, 20, 16)
        vl.setSpacing(10)

        # ---- 顶部标题 + 汇总 ----
        head = QHBoxLayout()
        title = StrongBodyLabel("资产管理", self)
        f = title.font()
        f.setPixelSize(18)
        f.setBold(True)
        title.setFont(f)
        head.addWidget(title)
        head.addStretch(1)
        self.summary = CaptionLabel("", self)
        self.summary.setStyleSheet("color:%s;" % TH.t("text_dim"))
        head.addWidget(self.summary)
        vl.addLayout(head)

        hint = CaptionLabel(
            "这些是「按需下载」的能力包，不进安装包；下完离线可用。", self)
        hint.setStyleSheet("color:%s;" % TH.t("text_dim"))
        vl.addWidget(hint)

        # ---- 表格 ----
        self.table = TableWidget(self)
        self.table.setColumnCount(len(self.COLS))
        self.table.setHorizontalHeaderLabels(list(self.COLS))
        self.table.verticalHeader().setVisible(False)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setWordWrap(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        vl.addWidget(self.table, 1)

        # ---- 底部 ----
        foot = QHBoxLayout()
        self.status = CaptionLabel("", self)
        self.status.setStyleSheet("color:%s;" % TH.t("text_dim"))
        foot.addWidget(self.status, 1)
        btn_refresh = PushButton("刷新状态", self)
        btn_refresh.clicked.connect(self.reload)
        foot.addWidget(btn_refresh)
        btn_close = PushButton("关闭", self)
        btn_close.clicked.connect(self.accept)
        foot.addWidget(btn_close)
        vl.addLayout(foot)

        self.reload()

    # ---------------------------------------------------------------- 数据
    def _fetch(self):
        """读清单 + 实时状态。任何一步失败都不让对话框炸掉。"""
        AC = _catalog()
        rows = []
        for a in AC.ASSETS:
            aid = str(a.get("id") or "")
            # **不按素材库门控**：不同资产的落点不同（语音底模落引擎目录，
            # 不依赖素材库）。曾经一律 `if lib else False`，结果没素材库时
            # 连语音底模也永远显示「未装」，用户点了下不动。
            try:
                inst = bool(AC.is_installed(a, _BASE_DIR))
            except Exception:
                inst = False
            try:
                sz = float(AC.installed_size(a, _BASE_DIR))
            except Exception:
                sz = 0.0
            # 部分文件缺失（解压中断）——标记出来，别让人以为装好了
            # 注意：asset_files_missing 返回 (已存在列表, 缺失列表)，不是 bool。
            missing = False
            try:
                mf, md = AC.asset_files_missing(a, _BASE_DIR)
                missing = bool(mf) or bool(md)
            except Exception:
                missing = False
            rows.append({
                "asset": a,
                "id": aid,
                "name": str(a.get("name") or aid),
                "kind": str(a.get("kind") or "model"),
                "size": float(a.get("size") or 0),
                "installed": inst,
                "inst_size": sz,
                "missing": missing and not inst,
            })
        # 顺序：未装在前（该干的活先看到），同组按体积降序
        rows.sort(key=lambda r: (r["installed"], -r["size"]))
        return rows

    def reload(self):
        self._assets = self._fetch()
        self._fill_table()
        self._update_summary()

    def _update_summary(self):
        n = len(self._assets)
        done = sum(1 for r in self._assets if r["installed"])
        # 注意：asset 的 size 是**下载包**体积，installed_size 是**解压后**体积，
        # 两者不可相加比较（实测解压后普遍更大：webengine 125MB -> 641MB）。
        # 下载量只算未装的，占用只算已装的。
        need = sum(r["size"] for r in self._assets if not r["installed"])
        got = sum(r["inst_size"] for r in self._assets if r["installed"])
        self.summary.setText("已安装 %d / %d 个，占磁盘 %s；未装还需下载 %s"
                             % (done, n, _human(got), _human(need)))
        # 落点有两个（素材库 / 语音引擎目录），只说一个会误导
        try:
            import voice_paths as VP
            vdir = VP.voice_dir()
        except Exception:
            vdir = ""
        self.status.setText("素材库根：%s　|　语音引擎：%s"
                            % (_lib_dir() or "未找到素材库",
                               vdir or "未找到"))

    def _fill_table(self):
        self.table.setRowCount(len(self._assets))
        for r, it in enumerate(self._assets):
            self.table.setItem(r, 0, QTableWidgetItem(it["name"]))
            self.table.setItem(r, 1, QTableWidgetItem(
                _KIND_CN.get(it["kind"], it["kind"])))
            self.table.setItem(r, 2, QTableWidgetItem(_human(it["size"])))

            if it["installed"]: st = "已安装（%s）" % _human(it["inst_size"])
            elif it["missing"]: st = "文件不完整"
            elif it["id"] in self._busy: st = "下载中…"
            else: st = "未安装"
            self.table.setItem(r, 3, QTableWidgetItem(st))

            # 操作列：放按钮
            # 语音类资产要两个入口（下载底模 / 管理音色），所以用容器装多个
            # 按钮，而不是一个 cell 只能塞一个 widget。
            extra = str(it["asset"].get("extra_action") or "")
            if it["installed"] and not extra:
                w = CaptionLabel("—", self)
                w.setAlignment(Qt.AlignCenter)
            else:
                box = QWidget(self)
                bl = QHBoxLayout(box)
                bl.setContentsMargins(4, 2, 4, 2)
                bl.setSpacing(4)
                if not it["installed"]:
                    txt = "补全" if it["missing"] else "下载"
                    b = PrimaryPushButton(txt, box)
                    b.setFixedHeight(28)
                    b.clicked.connect(
                        lambda _=False, x=it: self._start_download(x))
                    bl.addWidget(b)
                if extra == "voices":
                    vb = PushButton("音色管理", box)
                    vb.setFixedHeight(28)
                    vb.setToolTip("导入 / 删除角色音色（ckpt + pth 成对）")
                    vb.clicked.connect(lambda _=False: self._open_voices())
                    bl.addWidget(vb)
                w = box
            self.table.setCellWidget(r, 4, w)
        self.table.resizeRowsToContents()

    def _open_voices(self):
        """打开语音音色管理对话框。"""
        try:
            import voice_manager as VM
        except Exception as e:
            TOAST.toast_warn(self, "音色管理", "模块缺失: %s" % e, duration=4000)
            return
        try:
            dlg = VM.VoiceManagerDialog(self)
            dlg.exec()
            self.reload()
        except Exception as e:
            TOAST.toast_warn(self, "音色管理", "打开失败: %s" % e, duration=4000)

    # ---------------------------------------------------------------- 动作
    def _start_download(self, it):
        a = it["asset"]
        aid = it["id"]
        if aid in self._busy: return
        url = a.get("url") or ""
        # **files 模式也算有下载地址**：HF 仓库的逐文件模型（LayerDiff3D /
        # Marigold / MobileSAM / 语音底模）走 repo+files，由 asset_urls 现拼
        # URL，没有 url 字段。曾经这里只认 url/parts -> 这些资产点「下载」
        # 直接报「还没有配置下载地址」，是死路。
        if not url and not a.get("parts") and not a.get("files"):
            TOAST.toast_warn(self, "资产下载", "%s 还没有配置下载地址" % it["name"], duration=4000)
            return
        try:
            AD = _downloader()
        except Exception as e:
            TOAST.toast_warn(self, "资产下载", "下载模块缺失: %s" % e, duration=4000)
            return

        # 防重入：和资产菜单同款判断，避免双线程抢同一个 .part
        try:
            prog = AD.AssetDownloadThread.active_progress(aid)
        except Exception:
            prog = None
        if prog is not None:
            got, total = prog
            pct = (" %d%%" % max(0, min(100, int(got * 100 / total)))) if total else ""
            TOAST.toast_info(self, "资产下载", "%s 已在下载中%s" % (it["name"], pct), duration=4000)
            return

        self._busy.add(aid)
        self._fill_table()
        dlg = AD.AssetDownloadDialog(a, _BASE_DIR, self)
        try:
            dlg.start()
            dlg.exec()
        except Exception as e:
            TOAST.toast_warn(self, "资产下载", "启动失败: %s" % e, duration=4000)
        finally:
            self._busy.discard(aid)
            # 下载窗口关掉后刷新状态（可能刚装好，也可能失败）
            QTimer.singleShot(200, self.reload)