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
import sys
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

    # ---------- 活跃下载登记表（task-20：菜单项显示下载中百分比） ----------
    # key = asset id，value = {"got": int, "total": int}。
    # 并发说明：CPython 的单键 dict 赋值/读是原子的（GIL），登记表只有
    # 单键 set/del 与读，不需要锁。登记在 run() 入口、清除在 finally
    # 兜底（成功/失败/取消/异常全路径必清，防泄漏成"永远下载中"）。
    _ACTIVE = {}

    @classmethod
    def active_progress(cls, aid):
        """查询资产当前下载进度。进行中返回 (got, total)；无活跃下载返回 None。
        total<=0（未知总大小）返回 (got, 0)——菜单层负责容错显示。"""
        try:
            st = cls._ACTIVE.get(aid)
            if st is None:
                return None
            return (int(st.get("got", 0)), int(st.get("total", 0)))
        except Exception:
            return None

    def __init__(self, asset, base_dir, parent=None):
        super().__init__(parent)
        self.asset = asset
        self.base_dir = base_dir
        self._cancel = False

    def cancel(self):
        self._cancel = True

    # ---------- 登记表维护（task-20） ----------
    def _register_active(self):
        """run() 入口登记：菜单层由此得知"该资产正在下载"。"""
        try:
            AssetDownloadThread._ACTIVE[self.asset.get("id")] = {
                "got": 0, "total": int(self.asset.get("size") or 0)}
        except Exception:
            pass

    def _clear_active(self):
        """出口清除（成功/失败/取消/异常全路径经 finally 到这里，幂等）。"""
        try:
            AssetDownloadThread._ACTIVE.pop(self.asset.get("id"), None)
        except Exception:
            pass

    def _emit_progress(self, got, total):
        """progressed 信号统一出口：先更新登记表再发信号。
        （信号是跨线程队列投递，接收方在主线程，不能在接收侧回写登记表
        ——这里在下载线程内同步写，保证菜单重建时读到的是最新值。）"""
        try:
            st = AssetDownloadThread._ACTIVE.get(self.asset.get("id"))
            if st is not None:
                st["got"] = int(got)
                st["total"] = int(total)
            else:
                # run() 尚未登记（理论上不可能）——兜底登记防查询落空
                AssetDownloadThread._ACTIVE[self.asset.get("id")] = {
                    "got": int(got), "total": int(total)}
        except Exception:
            pass
        self.progressed.emit(got, total)   # _emit_progress 出口，勿直呼

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
                self._emit_progress(got, total or got)
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
                # 防 zip slip：必须带分隔符比较，否则 dest 末段名是兄弟目录名前缀时
                # 字符串前缀匹配会让 ../destEVIL/x 逃出 dest（实测可写出目录）
                if not os.path.abspath(target).startswith(
                        os.path.abspath(dest_dir) + os.sep):
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(n) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if i % 50 == 0:
                    self.status.emit("解压中 %d/%d" % (i, len(names)))
        return True

    def run(self):
        """执行下载。四种形态：
           逐文件（模型）：asset 有 "files" -> 按相对路径逐个下（支持逐文件续传）
           zip   （运行时/sam）：asset 有 "url" -> 下整包再解压
           分卷  （CUDA 包）：asset mode=="parts" -> 多卷下载拼接解压覆盖
           wheel （python_pack）：asset mode=="wheel" -> 下载官方 wheel
                 （zip 格式）-> sha256 校验 -> 解压到 parts_target_dir
        """
        self._register_active()          # task-20：登记活跃下载（菜单显示进度用）
        try:
            import asset_catalog as AC
            if self.asset.get("mode") == "parts":
                self._run_parts()
                return
            if self.asset.get("mode") == "wheel":
                self._run_wheel()
                return
            plan = AC.asset_urls(self.asset)
            if not plan:
                self.failed.emit("该资产没有配置下载地址")
                return
            dest_dir = self._dest_dir()
            try:
                os.makedirs(dest_dir, exist_ok=True)
            except Exception:
                pass

            is_multi = bool(self.asset.get("files"))
            if is_multi:
                self._run_multi_file(plan, dest_dir)
            else:
                urls = plan[0][1] or []
                first = urls[0] if urls else ""
                # 以 .zip 结尾才当压缩包；否则是单文件（如 mobile_sam.pt）
                low = first.lower().split("?")[0]
                if low.endswith(".zip"):
                    self._run_single_zip(urls, dest_dir)
                else:
                    self._run_single_file(urls, dest_dir)
        except Exception as e:
            self.failed.emit("%s: %s" % (type(e).__name__, e))
        finally:
            self._clear_active()         # 成功/失败/取消/异常全路径必清

    # ---------- 逐文件模式（模型）----------
    def _run_multi_file(self, plan, dest_dir):
        total_files = len(plan)
        done_bytes = 0
        total_bytes = int(self.asset.get("size") or 0)
        for idx, (rel, urls) in enumerate(plan):
            if self._cancel:
                self.failed.emit("已取消")
                return
            target = os.path.join(dest_dir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(target) or dest_dir, exist_ok=True)
            self.status.emit("下载 %d/%d：%s" % (idx + 1, total_files, rel))
            ok = False
            for u in urls:
                if self._cancel:
                    self.failed.emit("已取消")
                    return
                try:
                    size = self._download_file(u, target, done_bytes, total_bytes)
                    if size is not None:
                        done_bytes += size
                        self._emit_progress(done_bytes, total_bytes or done_bytes)
                        ok = True
                        break
                except Exception as e:
                    self.status.emit("镜像失败：%s" % str(e)[:60])
            if not ok:
                self.failed.emit("下载失败：%s（已下部分保留，可重试续传）" % rel)
                return
        # ---- 下载后严格校验：清单里每个文件都必须真实落地且大小 > 0 ----
        # 这一步是防"读完不到文件却反复重下"的关键：如果落盘位置与清单/应用
        # 读取路径不一致，这里会立刻报出来，而不是让调用方以为成功。
        try:
            import asset_catalog as AC
            present, missing = AC.asset_files_missing(self.asset, self.base_dir)
        except Exception as e:
            self.failed.emit("校验失败：%s" % e)
            return
        if missing:
            self.status.emit("校验：%d 个文件未落地" % len(missing))
            self.failed.emit("下载后校验未通过：缺 %d 个文件（如 %s）"
                             % (len(missing), missing[0]))
            return
        self.finished_ok.emit(dest_dir)

    def _download_file(self, url, target, base_bytes=0, total_bytes=0):
        """单文件断点续传（.part）。成功返回文件大小，失败返回 None。"""
        import urllib.request
        part = target + ".part"
        have = os.path.getsize(part) if os.path.exists(part) else 0
        # 目标已存在且 .part 不存在：视为已完成（大小>0 即认）
        if os.path.isfile(target) and os.path.getsize(target) > 0 and not os.path.exists(part):
            return os.path.getsize(target)

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        if have > 0:
            req.add_header("Range", "bytes=%d-" % have)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            # 416 = 本地 .part 超出服务器文件（包更新过）：丢弃残留从头再下，
            # 否则残留永远拼不出正确文件 -> 无限失败
            code = getattr(e, "code", None)
            if code == 416 and have > 0:
                self.status.emit("残留分卷与服务器不符，重新下载")
                try:
                    os.remove(part)
                except OSError:
                    pass
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"})
                try:
                    resp = urllib.request.urlopen(req, timeout=30)
                except Exception:
                    return None
                have = 0
            else:
                return None
        code = getattr(resp, "status", 200)
        mode = "ab" if code == 206 else "wb"
        if code != 206:
            have = 0
        total = 0
        try:
            cr = resp.headers.get("Content-Range") or ""
            mm = re.search(r"/(\d+)", cr)
            if mm:
                total = int(mm.group(1))
            elif resp.headers.get("Content-Length"):
                total = have + int(resp.headers["Content-Length"])
        except Exception:
            pass
        got = have
        with open(part, mode) as f:
            while True:
                if self._cancel:
                    f.flush()
                    return None
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                self._emit_progress(base_bytes + got,
                                    total_bytes or (base_bytes + got))
        if total and got < total:
            self.status.emit("不完整 %s/%s" % (human_mb(got), human_mb(total)))
            return None
        # 校验通过 -> .part 转正
        if os.path.exists(target):
            try:
                os.remove(target)
            except Exception:
                pass
        os.replace(part, target)
        return got

    # ---------- 单文件模式（如 mobile_sam.pt）----------
    def _run_single_file(self, urls, dest_dir):
        """把单个文件下到 dest_dir/<url 末段文件名>，下完做 check 校验。"""
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception:
            pass
        fname = ""
        for u in urls:
            seg = u.split("?")[0].rstrip("/").split("/")[-1]
            if seg:
                fname = seg
                break
        if not fname:
            self.failed.emit("无法从下载地址推断文件名")
            return
        target = os.path.join(dest_dir, fname)
        size = None
        for u in urls:
            if self._cancel:
                self.failed.emit("已取消")
                return
            self.status.emit("连接 %s" % u.split("/")[2])
            try:
                sz = self._download_file(u, target, 0,
                                         int(self.asset.get("size") or 0))
            except Exception as e:
                self.status.emit("镜像失败：%s" % str(e)[:60])
                sz = None
            if sz is not None:
                size = sz
                break
        if size is None:
            self.failed.emit("所有镜像都失败；已下载部分保留，可重试续传")
            return
        # 校验
        try:
            import asset_catalog as AC
            if not AC.is_installed(self.asset, self.base_dir):
                self.failed.emit("下载后校验未通过：%s 不在预期位置或过小" % target)
                return
        except Exception as e:
            self.failed.emit("校验失败：%s" % e)
            return
        self.finished_ok.emit(dest_dir)

    # ---------- 单包模式（运行时 / sam）----------
    def _run_single_zip(self, urls, dest_dir):
        import zipfile
        parent = os.path.dirname(dest_dir.rstrip("\\/")) or self.base_dir
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception:
            pass
        base_name = os.path.basename(dest_dir.rstrip("\\/")) or "asset"
        part = os.path.join(parent, base_name + ".zip.part")
        size = None
        for u in urls:
            if self._cancel:
                self.failed.emit("已取消")
                return
            self.status.emit("连接 %s" % u.split("/")[2])
            try:
                sz = self._download_file(u, os.path.join(parent, base_name + ".zip"),
                                        0, int(self.asset.get("size") or 0))
            except Exception as e:
                self.status.emit("镜像失败：%s" % str(e)[:60])
                sz = None
            if sz is not None:
                size = sz
                break
        if size is None:
            self.failed.emit("所有镜像都失败；已下载部分保留，可重试续传")
            return
        zpath = os.path.join(parent, base_name + ".zip")
        # v3.4.0 安全加固：清单声明了 zip_sha256 就必须先校验再解压（fail-closed）
        want = str(self.asset.get("zip_sha256") or "").strip().lower()
        if want:
            import hashlib
            self.status.emit("校验整包 sha256…")
            h = hashlib.sha256()
            try:
                with open(zpath, "rb") as f:
                    while True:
                        blk = f.read(1024 * 1024)
                        if not blk:
                            break
                        h.update(blk)
            except OSError as e:
                self.failed.emit("校验读取失败：%s" % str(e)[:80])
                return
            if h.hexdigest().lower() != want:
                try:
                    os.remove(zpath)
                except OSError:
                    pass
                self.failed.emit("sha256 校验不符，坏包已删除（镜像内容可能被篡改）")
                return
        if zipfile.is_zipfile(zpath):
            if not self._extract_zip(zpath, dest_dir):
                self.failed.emit("解压失败")
                return
            try:
                os.remove(zpath)
            except Exception:
                pass
        # 解压后校验：check 指向的目标必须存在且够大（否则报错而非静默成功）
        try:
            import asset_catalog as AC
            if not AC.is_installed(self.asset, self.base_dir):
                self.failed.emit("解压后校验未通过：目标文件不在预期位置 %s" % dest_dir)
                return
        except Exception as e:
            self.failed.emit("校验失败：%s" % e)
            return
        self.finished_ok.emit(dest_dir)

    # ---------- wheel 模式（python_pack，H3-c WebEngine）----------
    def _run_wheel(self):
        """官方 wheel（zip 格式）单包下载 -> sha256 fail-closed 校验 ->
        解压到 parts_target_dir（runtime_root\\Lib\\site-packages）。

        与 _run_parts 共享的安全闸语义（各自按需收紧）：
          1. sha256 未声明 -> 拒装（python_pack 直接进 site-packages，
             宁可不装不可装坏）
          2. sha256 不符 -> 删坏包报错
          3. 解压后 done_marker 探测失败 -> 报错（防"验证不过就 finished"）
        与 cuda_pack 的差异：wheel 无分卷（单包断点续传），无 pre_delete
        （PySide6 已装 Essentials，WebEngine 是纯增量，重复解压幂等）。
        """
        import hashlib
        import asset_catalog as AC
        asset = self.asset
        plan = AC.asset_urls(asset)
        urls = (plan[0][1] or []) if plan else []
        if not urls:
            self.failed.emit("该资产没有配置下载地址")
            return
        target = AC.parts_target_dir(asset, self.base_dir)
        if not target:
            self.failed.emit("找不到运行时目录（runtime\\Lib\\site-packages）")
            return
        # 磁盘预检：size 已知按 1.2x 估，未知按 300MB 兜底（WebEngine wheel 量级）
        need = int(asset.get("size") or 300 * 2 ** 20)
        try:
            drive = os.path.splitdrive(os.path.abspath(target))[0] + os.sep
            free = shutil.disk_usage(drive).free
            if free < need * 1.2:
                self.failed.emit("磁盘空间不足：约需 %s，%s 仅剩 %s"
                                 % (human_mb(need * 1.2), drive, human_mb(free)))
                return
        except Exception:
            pass
        # 下载（单包断点续传；.part 落临时工作目录，与 cuda_pack 同款隔离）
        work = os.path.join(os.environ.get("TEMP") or target,
                            "muppet_wheel_dl")
        try:
            os.makedirs(work, exist_ok=True)
        except Exception as e:
            self.failed.emit("无法创建下载目录：%s" % e)
            return
        fname = os.path.basename(urls[0].split("?")[0]) or "pack.whl"
        dl_path = os.path.join(work, fname)
        size = None
        for u in urls:
            if self._cancel:
                self.failed.emit("已取消")
                return
            self.status.emit("连接 %s" % u.split("/")[2])
            try:
                sz = self._download_file(u, dl_path, 0,
                                         int(asset.get("size") or 0))
            except Exception as e:
                self.status.emit("镜像失败：%s" % str(e)[:60])
                sz = None
            if sz is not None:
                size = sz
                break
        if size is None:
            self.failed.emit("所有镜像都失败；已下载部分保留，可重试续传")
            return
        # sha256 fail-closed（python_pack 直接进 site-packages，同 parts 安全策略）
        want = (asset.get("zip_sha256") or "").strip()
        if not want:
            self.failed.emit("python_pack 清单未声明 zip_sha256，拒绝安装"
                             "（安全策略）")
            return
        self.status.emit("校验 sha256…")
        h = hashlib.sha256()
        with open(dl_path, "rb") as f:
            while True:
                b = f.read(4 * 1024 * 1024)
                if not b:
                    break
                h.update(b)
        if h.hexdigest().lower() != want.lower():
            try:
                os.remove(dl_path)      # 坏包删除（.part 保留续传语义不受影响）
            except OSError:
                pass
            self.failed.emit("校验失败（sha256 不符，坏包已删除）——重试将"
                             "从断点重新校验下载")
            return
        if not zipfile.is_zipfile(dl_path):
            self.failed.emit("下载结果不是有效 wheel/zip（包可能损坏），请重试")
            return
        # 解压到 site-packages（wheel 顶层多目录：PySide6/ + dist-info/，
        # 单顶层剥离逻辑不会误触发；重复安装 = 覆盖同版本文件，幂等）
        self.status.emit("解压到运行环境…")
        if not self._extract_zip(dl_path, target):
            self.failed.emit("解压失败（可重试）")
            return
        shutil.rmtree(work, ignore_errors=True)
        # 落地校验：done_marker 必须出现（PySide6\Qt6WebEngineCore.dll）
        if not AC.is_installed(asset, self.base_dir):
            self.failed.emit("解压后校验未通过：未找到 WebEngine 标志文件"
                             "（%s）。包内容可能与预期不符" % target)
            return
        self.finished_ok.emit(target)

    # ---------- 分卷模式（CUDA 加速包）----------
    def _run_parts(self):
        """多卷下载 -> 字节拼接成 zip -> sha256 校验 -> pre_delete -> 解压覆盖。

        安全闸（全部 fail-fast，宁可不装不可装坏）：
          1. 本进程已 import torch -> 拒绝（DLL 被占用，替换必半途中断）
          2. 磁盘预检不足 -> 拒绝
          3. 任一卷下不全 -> 保留 .part 可续传，不进入解压
          4. sha256 不符 -> 删坏包（防下次拼接用坏卷）报错
          5. pre_delete 失败（文件锁）-> 报错，不解压
          6. 解压后 done_marker 探测失败 -> 报错（沿用"验证不过就 failed"的
             防无限重下载闸门）
        """
        import hashlib
        import asset_catalog as AC
        asset = self.asset
        parts = asset.get("parts") or []
        if not parts:
            self.failed.emit("分卷清单为空")
            return
        if "torch" in sys.modules:
            self.failed.emit("当前会话已加载推理引擎(torch)，CUDA 包的文件替换会被 "
                             "DLL 锁定。已下载部分已保留：重启应用后不要拆图，"
                             "直接到「资产管理→推理加速」重新点击安装即可"
                             "（自动续传，几秒完成）")
            return
        target = AC.parts_target_dir(asset, self.base_dir)
        if not target:
            self.failed.emit("找不到运行时目录（runtime\\Lib\\site-packages）")
            return
        # 磁盘预检：卷大小已知按 1.2x 估，未知按 4GB 兜底
        need = sum(int(p.get("size") or 0) for p in parts) or 4 * 2 ** 30
        try:
            drive = os.path.splitdrive(os.path.abspath(target))[0] + os.sep
            free = shutil.disk_usage(drive).free
            if free < need * 1.2:
                self.failed.emit("磁盘空间不足：约需 %s，%s 仅剩 %s"
                                 % (human_mb(need * 1.2), drive, human_mb(free)))
                return
        except Exception:
            pass
        work = os.path.join(os.environ.get("TEMP") or target, "muppet_cuda_dl")
        try:
            os.makedirs(work, exist_ok=True)
        except Exception as e:
            self.failed.emit("无法创建下载目录：%s" % e)
            return
        total_bytes = sum(int(p.get("size") or 0) for p in parts)
        done = 0
        for i, p in enumerate(parts):
            if self._cancel:
                self.failed.emit("已取消")
                return
            url = (p.get("url") or "").strip()
            fname = os.path.basename(url.split("?")[0]) or ("part%02d" % (i + 1))
            dest = os.path.join(work, fname)
            urls = [url, AC.GH_MIRROR + url]
            sz = None
            for u in urls:
                self.status.emit("下载卷 %d/%d（%s）" % (i + 1, len(parts), fname))
                try:
                    sz = self._download_file(u, dest, done, total_bytes)
                except Exception as e:
                    self.status.emit("镜像失败：%s" % str(e)[:60])
                    sz = None
                if sz is not None:
                    break
            if sz is None:
                self.failed.emit("卷 %d/%d 下载失败；已下部分保留，可重试续传"
                                 % (i + 1, len(parts)))
                return
            done += sz
            self._emit_progress(done, total_bytes or done)
        # 拼接（流式，不把 2.6GB 读进内存）
        zip_path = os.path.join(work, "cuda_pack.zip")
        self.status.emit("合并分卷…")
        try:
            with open(zip_path, "wb") as out:
                for p in parts:
                    fname = os.path.basename(
                        (p.get("url") or "").split("?")[0]) or "part"
                    with open(os.path.join(work, fname), "rb") as f:
                        shutil.copyfileobj(f, out, 1024 * 1024)
        except Exception as e:
            self.failed.emit("分卷合并失败：%r" % e)
            return
        # 整包校验（v3.4.0 fail-closed：分卷包直接替换 torch DLL，
        # 清单未声明 sha256 一律拒装，宁缺毋滥）
        want = (asset.get("zip_sha256") or "").strip()
        note = ""
        if not want:
            try:
                os.remove(zip_path)
            except OSError:
                pass
            self.failed.emit("加速包清单未声明 zip_sha256，拒绝安装（安全策略）")
            return
        self.status.emit("校验整包 sha256…")
        h = hashlib.sha256()
        with open(zip_path, "rb") as f:
            while True:
                b = f.read(4 * 1024 * 1024)
                if not b:
                    break
                h.update(b)
        if h.hexdigest().lower() != want.lower():
            try:
                os.remove(zip_path)
            except OSError:
                pass
            # 坏包必须连分卷一起删：只删 zip 留着错卷，下次拼接还是坏的
            for p in parts:
                fn = os.path.basename(
                    (p.get("url") or "").split("?")[0]) or "part"
                for cand in (os.path.join(work, fn),
                             os.path.join(work, fn + ".part")):
                    try:
                        os.remove(cand)
                    except OSError:
                        pass
            self.failed.emit("校验失败（sha256 不符，坏包与分卷已删除）——"
                             "重试将完整重新下载")
            return
        if not zipfile.is_zipfile(zip_path):
            self.failed.emit("合并结果不是有效 zip（某卷可能损坏），请重试")
            return
        # 删旧
        self.status.emit("替换旧 torch（先清理 CPU 版）…")
        for name in (asset.get("pre_delete") or []):
            d = os.path.join(target, name.replace("/", os.sep))
            if os.path.isdir(d):
                try:
                    shutil.rmtree(d)
                except Exception as e:
                    self.failed.emit("无法删除旧 %s（文件被占用？请完全关闭"
                                     "应用后重试）：%s" % (name, e))
                    return
        # 解压
        if not self._extract_zip(zip_path, target):
            self.failed.emit("解压失败（可重试）")
            return
        shutil.rmtree(work, ignore_errors=True)
        # 落地校验：done_marker 必须出现
        if not AC.is_installed(asset, self.base_dir):
            self.failed.emit("解压后校验未通过：未找到 CUDA 标志文件（%s）。"
                             "包内容可能与预期不符" % target)
            return
        self.finished_ok.emit(target + note)

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
