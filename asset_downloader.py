# -*- coding: utf-8 -*-
"""资产管理 - 下载器（断点续传 + 进度显示 + 自动解压）。

设计：
    - 后台线程下载，绝不阻塞 UI（5GB 模型直接下会卡死主线程）
    - .part 文件断点续传（与安装器同一套语义：HTTP Range）
    - 多镜像依次尝试（hf-mirror 优先，官方兜底）
    - 下载完校验大小；zip 自动解压到 dest
    - 全程进度回调到 Qt 信号（主线程更新进度条）

与 asset_catalog 的分工：
    asset_catalog 只管"有哪些资产、装没装"
    asset_downloader 只管"怎么下、下到哪、解压"
"""
import os
import re
import shutil
import threading
import zipfile

import numpy as np  # noqa: F401  (保持与应用一致的依赖导入顺序)

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QWidget)
from qfluentwidgets import (ProgressBar, BodyLabel, CaptionLabel, PushButton,
                            InfoBar, InfoBarPosition)

# 模型多镜像（与安装器保持一致：国内镜像优先）
HOSTS = ["https://hf-mirror.com/", "https://huggingface.co/"]


def human_mb(n):
    """字节 -> 人类可读"""
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0 or unit == "GB":
            if unit == "B":
                return "%d B" % int(n)
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f GB" % n


def asset_file_urls(asset):
    """资产 -> 候选下载 URL 列表。

    asset["url"] 非空时优先用它（运行时 zip 等自有资源）；
    否则按 repo 拼 hf 镜像地址（模型目录会打包成 tar/zip，见 download_plan）。
    """
    url = (asset.get("url") or "").strip()
    if url:
        return [url]
    repo = asset.get("repo") or ""
    if not repo:
        return []
    # 模型以打包形式提供（单文件下载，避免几十个文件的复杂续传）
    name = repo.split("/")[-1] + ".zip"
    return [h + "24yearsold/" + repo.split("/")[-1] + "/resolve/main/" + name
            for h in HOSTS]


class AssetDownloadThread(QThread):
    """后台下载线程：断点续传 + 进度信号 + 完成后可选解压。"""

    progressed = Signal(int, int)      # (已下载字节, 总字节)
    status = Signal(str)               # 状态文案
    finished_ok = Signal(str)          # 成功，携带落盘/解压路径
    failed = Signal(str)               # 失败原因

    def __init__(self, asset, base_dir, parent=None):
        super().__init__(parent)
        self.asset = asset
        self.base_dir = base_dir
        self._cancel = False

    def cancel(self):
        self._cancel = True

    # ---------- 内部 ----------
    def _dest_dir(self):
        import asset_catalog as AC
        return AC.installed_path(self.asset, self.base_dir)

    def _download_one(self, url, part_path, total_hint):
        """单 URL 断点续传。返回 True=完成。"""
        import urllib.request
        have = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        if have > 0:
            req.add_header("Range", "bytes=%d-" % have)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            self.status.emit("镜像不通：%s" % str(e)[:60])
            return False
        code = getattr(resp, "status", 200)
        if code == 206:
            mode = "ab"
        else:
            # 服务器不支持 Range -> 从头来
            have = 0
            mode = "wb"
        # 总大小：Content-Range 或 Content-Length
        total = total_hint
        try:
            cr = resp.headers.get("Content-Range") or ""
            m = re.search(r"/(\d+)", cr)
            if m:
                total = int(m.group(1))
            elif resp.headers.get("Content-Length"):
                total = have + int(resp.headers["Content-Length"])
        except Exception:
            pass
        got = have
        with open(part_path, mode) as f:
            while True:
                if self._cancel:
                    self.status.emit("已取消")
                    return False
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                self.progressed.emit(got, total or got)
        # 大小校验
        if total and got < total:
            self.status.emit("不完整（%s / %s），可重试续传" % (human_mb(got), human_mb(total)))
            return False
        return True

    def _extract_zip(self, zip_path, dest_dir):
        """解压到目标目录。zip 内若有单一顶层目录，剥掉它（避免 runtime/runtime/）。"""
        self.status.emit("正在解压…")
        os.makedirs(dest_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            # 探测是否需要剥顶层目录
            tops = {n.split("/")[0] for n in names if n.strip()}
            strip = None
            if len(tops) == 1:
                top = tops.pop()
                if any(n.startswith(top + "/") for n in names):
                    strip = top + "/"
            for i, n in enumerate(names):
                if self._cancel:
                    return False
                if n.endswith("/"):
                    continue
                rel = n[len(strip):] if strip and n.startswith(strip) else n
                if not rel:
                    continue
                target = os.path.join(dest_dir, rel.replace("/", os.sep))
                # 防 zip slip
                if not os.path.abspath(target).startswith(os.path.abspath(dest_dir)):
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(n) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if i % 50 == 0:
                    self.status.emit("解压中 %d/%d" % (i, len(names)))
        return True

    def run(self):
        try:
            urls = asset_file_urls(self.asset)
            if not urls:
                self.failed.emit("该资产没有配置下载地址")
                return
            dest_dir = self._dest_dir()
            parent = os.path.dirname(dest_dir.rstrip("\\/")) or self.base_dir
            try:
                os.makedirs(parent, exist_ok=True)
            except Exception:
                pass
            # 下载到父目录下的 .part（同盘，解压后再搬）
            base_name = os.path.basename(dest_dir.rstrip("\\/")) or "asset"
            part = os.path.join(parent, base_name + ".zip.part")
            total_hint = int(self.asset.get("size") or 0)
            ok = False
            for u in urls:
                if self._cancel:
                    self.failed.emit("已取消")
                    return
                self.status.emit("连接 %s" % u.split("/")[2])
                if self._download_one(u, part, total_hint):
                    ok = True
                    break
            if not ok:
                self.failed.emit("所有镜像都失败；已下载部分保留，可重试续传")
                return
            # 校验
            if self._cancel:
                self.failed.emit("已取消")
                return
            self.status.emit("校验完成，准备解压")
            if zipfile.is_zipfile(part):
                if not self._extract_zip(part, dest_dir):
                    self.failed.emit("解压失败")
                    return
            else:
                # 不是 zip：直接当单个文件放到 dest_dir
                os.makedirs(dest_dir, exist_ok=True)
                single = os.path.join(dest_dir, base_name)
                if os.path.exists(single):
                    os.remove(single)
                shutil.move(part, single)
            # 清理 .part
            try:
                if os.path.exists(part):
                    os.remove(part)
            except Exception:
                pass
            self.finished_ok.emit(dest_dir)
        except Exception as e:
            self.failed.emit("%s: %s" % (type(e).__name__, e))


class AssetDownloadDialog(QDialog):
    """下载进度对话框：进度条 + 速度 + 取消。"""

    def __init__(self, asset, base_dir, parent=None):
        super().__init__(parent)
        self.asset = asset
        self.base_dir = base_dir
        self.worker = None
        self.setWindowTitle("下载 %s" % asset.get("name", asset.get("id", "")))
        self.setFixedSize(460, 180)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        self.title = BodyLabel("准备下载…")
        lay.addWidget(self.title)
        self.bar = ProgressBar(self)
        self.bar.setValue(0)
        lay.addWidget(self.bar)
        self.detail = CaptionLabel("")
        lay.addWidget(self.detail)
        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = PushButton("取消", self)
        self.btn_cancel.clicked.connect(self._on_cancel)
        row.addWidget(self.btn_cancel)
        lay.addLayout(row)

    def start(self):
        self.worker = AssetDownloadThread(self.asset, self.base_dir, self)
        self.worker.progressed.connect(self._on_progress)
        self.worker.status.connect(self._on_status)
        self.worker.finished_ok.connect(self._on_ok)
        self.worker.failed.connect(self._on_fail)
        self.worker.start()

    def _on_progress(self, got, total):
        pct = int(got * 100 / total) if total > 0 else 0
        self.bar.setValue(max(0, min(100, pct)))
        self.detail.setText("%s / %s  (%d%%)" % (human_mb(got), human_mb(total), pct))

    def _on_status(self, msg):
        self.title.setText(msg)

    def _on_ok(self, path):
        self.bar.setValue(100)
        self.detail.setText("已安装到 %s" % path)
        self.btn_cancel.setText("完成")
        try:
            self.btn_cancel.clicked.disconnect()
        except Exception:
            pass
        self.btn_cancel.clicked.connect(self.accept)
        InfoBar.success("下载完成", "%s 已就位" % self.asset.get("name", ""),
                        parent=self.parent() or self,
                        position=InfoBarPosition.TOP, duration=3000)

    def _on_fail(self, why):
        self.title.setText("下载失败")
        self.detail.setText(str(why)[:200])
        self.btn_cancel.setText("关闭")
        try:
            self.btn_cancel.clicked.disconnect()
        except Exception:
            pass
        self.btn_cancel.clicked.connect(self.reject)

    def _on_cancel(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.title.setText("正在取消…")
            self.btn_cancel.setEnabled(False)
        else:
            self.reject()

    def closeEvent(self, e):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        super().closeEvent(e)
