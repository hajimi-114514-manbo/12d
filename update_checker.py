# -*- coding: utf-8 -*-
"""更新检查器（task-13）：GitHub 版本比对 + 后台静默下载新版安装包。

约定：
  - 本模块只依赖 PySide6.QtCore（QThread/Signal），**不 import l2d_rig_qt /
    welcome_window**（__main__ 双实例坑）；被谁 import 都行，方向单向。
  - 全程容错：任何网络/磁盘失败只发信号（status/failed），绝不弹窗、绝不抛。
  - 下载目录 %TEMP%\\muppet_update，.part 断点续传（独立实现，与
    asset_downloader 同思路但不 import 它）。
  - 下载成功后 pending_installer() 可取安装包路径；应用退出时由调用方
    （负责 close 流程的一方）调 launch_update(install_dir)：
        MuppetStudio-Setup.exe --auto --dir <安装目录>
    然后调用方退出应用。--auto 静默模式由 installer.cs 侧实现。

版本源（仓库根 version.json）：
    {"version": "3.1.1", "installer_sha256": "<hex>", "notes": "..."}
获取链三镜像依次尝试，总预算 30s（任一 200 即用）。
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request

from PySide6.QtCore import QThread, Signal

# ---------------- 常量（测试可整体替换这些模块级变量造假服务器） ----------------
OWNER_REPO = "hajimi-114514-manbo/12d"
BRANCH = "main"
REPO_VERSION_URLS = [
    "https://raw.githubusercontent.com/%s/%s/version.json" % (OWNER_REPO, BRANCH),
    "https://ghfast.top/https://raw.githubusercontent.com/%s/%s/version.json" % (OWNER_REPO, BRANCH),
    "https://cdn.jsdelivr.net/gh/%s@%s/version.json" % (OWNER_REPO, BRANCH),
]
RELEASE_URL_TPL = ("https://github.com/%s/releases/download/v{ver}/"
                   "MuppetStudio-Setup.exe" % OWNER_REPO)
MIRROR_PREFIX = "https://ghfast.top/"
INSTALLER_NAME = "MuppetStudio-Setup.exe"
FETCH_BUDGET = 30.0          # 三镜像总时长预算（秒）
UA = {"User-Agent": "Mozilla/5.0 (MuppetStudio)"}

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# ---------------- 下载目录 ----------------
_DIR_OVERRIDE = None


def set_update_dir(d):
    """测试钩子：重定向下载目录；None 恢复默认 %TEMP%\\muppet_update。"""
    global _DIR_OVERRIDE
    _DIR_OVERRIDE = d


def update_dir():
    d = _DIR_OVERRIDE or os.path.join(
        os.environ.get("TEMP") or tempfile.gettempdir(), "muppet_update")
    os.makedirs(d, exist_ok=True)
    return d


def installer_path():
    return os.path.join(update_dir(), INSTALLER_NAME)


# ---------------- 版本 ----------------
def parse_ver(s):
    """"3.1.1"/"v3.1.1"/"3.1"/任意脏串 -> (3,1,1) 三元组；脏输入回 (0,0,0)。"""
    nums = re.findall(r"\d+", str(s or ""))
    if not nums:
        return (0, 0, 0)
    t = tuple(int(x) for x in nums[:3])
    while len(t) < 3:
        t = t + (0,)
    return t


def is_newer(remote, local):
    """semver 数字三元组比较（3.1.10 > 3.1.9），字符串比较是错的。"""
    return parse_ver(remote) > parse_ver(local)


def fetch_version_json(timeout=None):
    """按 REPO_VERSION_URLS 顺序取 version.json；全挂/超时 -> None。
    timeout 是三源共享的**总预算**（默认 FETCH_BUDGET=30s）。"""
    budget = float(timeout) if timeout else FETCH_BUDGET
    deadline = time.monotonic() + budget
    for url in REPO_VERSION_URLS:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=left) as resp:
                code = getattr(resp, "status", 200)
                if code != 200:
                    continue
                obj = json.loads(resp.read().decode("utf-8-sig", "ignore"))
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue                      # 单镜像失败静默换下一个
    return None


# ---------------- 安装包下载（.part 断点续传，独立实现） ----------------
def _open_range(url, have):
    """带 Range 打开远端；416（.part 比服务器新，包换过）-> 删残留从头来。
    返回 (resp, have)；失败 (None, 0)。resp 需调用方 close。"""
    req = urllib.request.Request(url, headers=dict(UA))
    if have > 0:
        req.add_header("Range", "bytes=%d-" % have)
    try:
        return urllib.request.urlopen(req, timeout=60), have
    except Exception as e:
        code = getattr(e, "code", None)
        if code == 416 and have > 0:
            part = None                    # 调用方重试前自行删 .part
            raise _StalePart(e)
        raise


class _StalePart(Exception):
    """残留 .part 与服务器文件不符（416）。"""


def _download_installer(urls, target, on_progress, cancel):
    """多镜像下载 target（.part 续传）。成功 True；失败/取消 False。"""
    part = target + ".part"
    for url in urls:
        while True:                        # 416 时删残留重试一轮
            have = os.path.getsize(part) if os.path.exists(part) else 0
            resp = None
            try:
                try:
                    resp, have = _open_range(url, have)
                except _StalePart:
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                    continue
                status = getattr(resp, "status", 200)
                mode = "ab" if status == 206 else "wb"
                if status != 206:
                    have = 0
                total = 0
                try:
                    cr = resp.headers.get("Content-Range") or ""
                    mm = re.search(r"/(\d+)\s*$", cr)
                    if mm:
                        total = int(mm.group(1))
                    elif resp.headers.get("Content-Length"):
                        total = have + int(resp.headers["Content-Length"])
                except Exception:
                    pass
                got = have
                with open(part, mode) as f:
                    while True:
                        if cancel():
                            return False
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        on_progress(got, total or got)
                if total and got < total:
                    on_progress(got, total)
                    break                  # 不完整 -> 换镜像再续
                # 完成 -> .part 转正
                if os.path.exists(target):
                    try:
                        os.remove(target)
                    except OSError:
                        pass
                os.replace(part, target)
                return True
            except Exception:
                break                      # 本镜像失败 -> 下一个
            finally:
                try:
                    if resp is not None:
                        resp.close()
                except Exception:
                    pass
        # 换镜像时 .part 保留：下一轮自动 Range 续传
    return False


def _exe_head_ok(path):
    """PE(MZ) 或 zip(PK\\x03\\x04) 头探测；其余（404 的 html、半截垃圾）拒。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return head[:2] == b"MZ" or head == b"PK\x03\x04"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---------------- 成功后待拉起状态（模块级） ----------------
_pending_installer = None


def pending_installer():
    """已下载待安装的安装包路径；无则 None。"""
    global _pending_installer
    p = _pending_installer
    if p and not os.path.isfile(p):
        _pending_installer = None
        return None
    return p


def set_pending_installer(p):
    """测试/恢复用；None 清空。"""
    global _pending_installer
    _pending_installer = p


def launch_update(install_dir):
    """DETACHED 拉起 Setup.exe --auto --dir <install_dir>，成功 True。
    调用方随后负责退出应用。没有已下载包返回 False。"""
    exe = pending_installer()
    if not exe:
        return False
    argv = [exe, "--auto", "--dir", str(install_dir)]
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         cwd=os.path.dirname(exe) or None)
        return True
    except Exception:
        return False


# ---------------- 后台线程 ----------------
class UpdateThread(QThread):
    """点「检查更新」后的全流程后台线程（主线程零阻塞）。
    信号：status(str 人话) / progressed(int done, int total) /
          ready(str 安装包路径) / failed(str) / uptodate(str 远端版本)。"""

    status = Signal(str)
    progressed = Signal(int, int)
    ready = Signal(str)
    failed = Signal(str)
    uptodate = Signal(str)

    def __init__(self, local_version="", parent=None):
        super().__init__(parent)
        self.local_version = str(local_version or "")
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _cancelled(self):
        return self._cancel

    def run(self):
        try:
            self._run()
        except Exception as e:
            # 兜底：线程里任何意外都不许崩应用
            self.failed.emit("检查更新异常：%s" % str(e)[:120])

    def _run(self):
        self.status.emit("正在检查更新…")
        meta = fetch_version_json()
        if self._cancelled():
            return
        if meta is None:
            self.failed.emit("网络不可用，检查更新失败")
            return
        remote = str(meta.get("version") or "")
        if parse_ver(remote) == (0, 0, 0):
            self.failed.emit("版本源格式异常")
            return
        if not is_newer(remote, self.local_version):
            self.uptodate.emit(remote)
            return
        self.status.emit("发现新版本 v%s，开始后台下载…" % remote)
        url = RELEASE_URL_TPL.format(ver=remote)
        urls = [url, MIRROR_PREFIX + url]
        target = installer_path()
        # 上一轮已下好且仍是要的版本：直接复用
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            try:
                if self._verify(target, meta):
                    set_pending_installer(target)
                    self.ready.emit(target)
                    return
            except OSError:
                pass
        ok = _download_installer(urls, target,
                                 lambda g, t: self.progressed.emit(int(g), int(t)),
                                 self._cancelled)
        if self._cancelled():
            self.status.emit("已取消下载（部分文件保留，可续传）")
            return
        if not ok or not os.path.isfile(target):
            self.failed.emit("下载失败（网络或镜像不可用）")
            return
        try:
            if not _exe_head_ok(target):
                os.remove(target)
                self.failed.emit("安装包格式异常（非 PE/zip），已删除")
                return
            if not self._verify(target, meta):
                os.remove(target)
                self.failed.emit("sha256 校验不符，坏包已删除")
                return
        except OSError as e:
            self.failed.emit("校验读写失败：%s" % str(e)[:80])
            return
        set_pending_installer(target)
        self.status.emit("下载完成")
        self.ready.emit(target)

    def _verify(self, target, meta):
        """sha256 校验；version.json 未声明则跳过（status 注明）。"""
        want = str(meta.get("installer_sha256") or "").strip().lower()
        if not want:
            self.status.emit("清单未声明 sha256，跳过校验")
            return True
        try:
            got = _sha256(target)
        except OSError:
            return False
        return got == want
