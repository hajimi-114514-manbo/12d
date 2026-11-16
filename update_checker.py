# -*- coding: utf-8 -*-
"""更新检查器（task-13）：GitHub 版本比对 + 后台静默下载新版。

两条下载链路（v3.5.3 起**增量包优先**）：
  1) 增量包（首选）：version.json 的 "patches" 里有 from == 本机版本 的条目，
     就只下几十~几百 KB 的 zip（只含真改过的文件），校验 sha256 后解到暂存
     目录；应用退出时拉起一个 detached 的 .bat 覆盖安装目录并自动重启。
  2) 完整安装包（兜底）：没有对应增量包（跨多版本 / 清单没写 / 增量包坏了）
     时，走老链路下 Setup.exe，退出时拉起 --auto 静默安装。

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
  - 增量包成功后 pending_patch() 可取 {zip, stage, from, to, files}；退出时调
    launch_patch(install_dir)，它会拉起 _apply_patch.bat（等本进程退出 →
    备份被覆盖的旧文件 → robocopy 覆盖 → 重启程序），随后调用方退出。

版本源（仓库根 version.json）：
    {"version": "3.1.1", "installer_sha256": "<hex>", "notes": "...",
     "patches": [{"from": "3.5.2", "to": "3.5.3",
                  "file": "MuppetStudio-Update-v3.5.3-from-v3.5.2.zip",
                  "sha256": "<hex>", "size": 123456, "files": 8}]}
获取链三镜像依次尝试，总预算 30s（任一 200 即用）。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile

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
                   "MuppetStudio-Setup-v{ver}.exe" % OWNER_REPO)
MIRROR_PREFIX = "https://ghfast.top/"
# 安装包文件名。发布版带版本号（用户要求"给安装包名字打上版本号"），
# 便于下载后一眼看出是哪个版本；老版本无参调用走 INSTALLER_NAME 保持兼容。
INSTALLER_NAME = "MuppetStudio-Setup.exe"
INSTALLER_NAME_TPL = "MuppetStudio-Setup-v{ver}.exe"
# 增量包命名（与 D:\scripts\temp\make_incr_pack.py 产物一致；发布脚本会照着写清单）
PATCH_NAME_TPL = "MuppetStudio-Update-v{to}-from-v{from}.zip"
PATCH_URL_TPL = ("https://github.com/%s/releases/download/v{to}/"
                 "MuppetStudio-Update-v{to}-from-v{from}.zip" % OWNER_REPO)
PATCH_MAX_BYTES = 256 * 1024 * 1024   # 增量包解压后总大小上限（防呆，非业务限制）
PATCH_MAX_ENTRIES = 20000             # 条目数上限（防 zip 炸弹）
# 发布包里给"人工覆盖"用的两个文件：自动更新时不需要，解包阶段就跳过
PATCH_SKIP_NAMES = frozenset(("UPDATE.txt", "_apply_update.bat"))
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


def installer_name(ver=None):
    """安装包文件名。给版本号时用带版本号名，否则用默认名。"""
    if ver:
        return INSTALLER_NAME_TPL.format(ver=str(ver))
    return INSTALLER_NAME


def installer_path(ver=None):
    return os.path.join(update_dir(), installer_name(ver))


# ---------------- 增量包：路径与清单解析 ----------------
def patch_name(to_ver, from_ver):
    """增量包文件名（与发布脚本同名规则；'from' 是关键字，用 dict 展开）。"""
    return PATCH_NAME_TPL.format(to=str(to_ver), **{"from": str(from_ver)})


def patch_url(to_ver, from_ver):
    return PATCH_URL_TPL.format(to=str(to_ver), **{"from": str(from_ver)})


def patch_path(to_ver, from_ver):
    return os.path.join(update_dir(), patch_name(to_ver, from_ver))


def stage_dir(to_ver):
    """增量包解压暂存目录（应用退出前解好，.bat 只负责拷文件+重启）。"""
    name = re.sub(r"[^0-9A-Za-z._-]", "_", str(to_ver or "0")) or "0"
    return os.path.join(update_dir(), "stage-v%s" % name)


def _human_size(n):
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "?"
    if n <= 0:
        return "?"
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1048576.0)


human_size = _human_size            # 公开别名（欢迎页/测试用）


_VER_RE = re.compile(r"\d+")


def _patch_items(meta):
    """从 version.json 里取出所有增量包条目（兼容单条 "patch" 与 "patches"）。"""
    if not isinstance(meta, dict):
        return []
    out = []
    single = meta.get("patch")
    if isinstance(single, dict):
        out.append(single)
    raw = meta.get("patches")
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                out.append(item)
    return out


def find_patch(meta, local_version):
    """在本机版本对应的增量包条目；没有/不合法返回 None。

    只认 **from 与本机版本完全相等** 的条目（跨版本不拼链路，直接回落到整包）。
    条目必须自带 sha256（fail-closed：清单被代理整体伪造时至少挡掉无声明包）。
    """
    local = parse_ver(local_version)
    if local == (0, 0, 0):
        return None
    remote = str(meta.get("version") or "") if isinstance(meta, dict) else ""
    for item in _patch_items(meta):
        frm = str(item.get("from") or "")
        if parse_ver(frm) != local:
            continue
        to = str(item.get("to") or remote or "")
        # to 必须等于远端最新版，防止清单里残留的旧条目被误用
        if parse_ver(to) != parse_ver(remote) or parse_ver(to) == (0, 0, 0):
            continue
        sha = str(item.get("sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            continue
        name = os.path.basename(str(item.get("file") or "").strip()
                                or patch_name(to, frm))
        if not name.lower().endswith(".zip"):
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        try:
            nfiles = int(item.get("files") or 0)
        except (TypeError, ValueError):
            nfiles = 0
        return {"from": frm, "to": to, "file": name, "sha256": sha,
                "size": size, "files": nfiles}
    return None


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
    timeout 是三源共享的**总预算**（默认 FETCH_BUDGET=30s）。
    成功时把命中源记入 last_source（供 sha 交叉校验排除自身）。"""
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
                global last_source
                last_source = url
                return obj
        except Exception:
            continue                      # 单镜像失败静默换下一个
    return None


# 命中 fetch_version_json 的源 URL（模块级，测试可重置）。
last_source = None


def cross_check_sha(expected_sha, budget=15.0):
    """安全加固（v3.4.0 联网审计）：ghfast/jsdelivr 属第三方代理，单一源可被
    整体伪造（假 version + 假 sha 配套）。下载校验通过后**换一个源**再取一次
    version.json 比对 installer_sha256。
    返回 (verdict, sha_or_None)：
      "agree"       另一源声明的 sha 与 expected 一致
      "conflict"    另一源声明的 sha 不一致 -> 调用方必须删包拒装
      "unreachable" 其余源都不可达/未声明 sha -> 宽容通过（离线优先）
    """
    want = str(expected_sha or "").strip().lower()
    if not want:
        return "unreachable", None
    deadline = time.monotonic() + max(1.0, float(budget))
    checked = False
    for url in REPO_VERSION_URLS:
        if url == last_source:
            continue
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
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        other = str(obj.get("installer_sha256") or "").strip().lower()
        if not other:
            continue                      # 该源未声明 sha：换下一个源
        checked = True
        if other == want:
            return "agree", other
        return "conflict", other
    return ("agree" if checked else "unreachable"), None


def cross_check_patch_sha(expected_sha, from_ver, to_ver, budget=15.0):
    """增量包的换源交叉校验：另一源的 patches 里 **同一 from→to** 声明的
    sha256 必须一致。返回 (verdict, sha_or_None)，语义同 cross_check_sha。

    只在对方源确实声明了同一条增量包时才判定（对方源还没更新清单不算冲突）。
    """
    want = str(expected_sha or "").strip().lower()
    if not want:
        return "unreachable", None
    wf, wt = parse_ver(from_ver), parse_ver(to_ver)
    deadline = time.monotonic() + max(1.0, float(budget))
    checked = False
    for url in REPO_VERSION_URLS:
        if url == last_source:
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=left) as resp:
                if getattr(resp, "status", 200) != 200:
                    continue
                obj = json.loads(resp.read().decode("utf-8-sig", "ignore"))
        except Exception:
            continue
        remote = str(obj.get("version") or "") if isinstance(obj, dict) else ""
        for item in _patch_items(obj):
            if parse_ver(item.get("from")) != wf:
                continue
            if parse_ver(item.get("to") or remote) != wt:
                continue
            other = str(item.get("sha256") or "").strip().lower()
            if not other:
                continue                  # 该源没写 sha：换下一个源
            checked = True
            return ("agree" if other == want else "conflict"), other
    return ("agree" if checked else "unreachable"), None


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


# ---------------- 增量包：校验 + 安全解包 ----------------
def _zip_head_ok(path):
    """增量包必须是真 zip（PK\\x03\\x04）；404 的 html / 半截垃圾拒。"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def _safe_member_name(name):
    """zip 条目名归一化；绝对路径 / 盘符 / `..` 一律判非法（zip slip 防护）。"""
    n = str(name or "").replace("\\", "/").strip()
    if not n:
        return None
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def stage_patch(zip_path, to_ver="", stage=None):
    """把增量包安全解到暂存目录（**不碰安装目录**，安装目录由退出后的 .bat 覆盖）。

    在进程内先解好，好处是：坏包/坏路径当场发现（不会退出后才发现装不上），
    并且 .bat 不需要任何解压工具（只用 robocopy + copy）。
    返回 (ok, msg, stage_dir, file_count)。
    """
    stage = stage or stage_dir(to_ver)
    try:
        zf = zipfile.ZipFile(zip_path)
    except Exception as e:
        return False, "增量包不是有效 zip：%s" % str(e)[:60], "", 0
    with zf:
        try:
            infos = zf.infolist()
        except Exception as e:
            return False, "增量包目录读取失败：%s" % str(e)[:60], "", 0
        if len(infos) > PATCH_MAX_ENTRIES:
            return False, "增量包条目过多（%d），拒绝" % len(infos), "", 0
        plan = []
        total = 0
        for info in infos:
            if info.is_dir():
                continue
            rel = _safe_member_name(info.filename)
            if rel is None:
                return False, "增量包含非法路径条目：%s" % str(info.filename)[:60], "", 0
            if rel.split("/")[0] in PATCH_SKIP_NAMES:
                continue                  # 给人工安装用的说明/批处理，不进暂存
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                return False, "增量包含符号链接条目，拒绝", "", 0
            total += int(info.file_size or 0)
            if total > PATCH_MAX_BYTES:
                return False, "增量包解压后超过上限（%s）" % _human_size(PATCH_MAX_BYTES), "", 0
            plan.append((info, rel))
        if not plan:
            return False, "增量包是空的", "", 0
        if os.path.isdir(stage):
            try:
                shutil.rmtree(stage, ignore_errors=True)
            except OSError:
                pass
        try:
            os.makedirs(stage, exist_ok=True)
            for info, rel in plan:
                dst = os.path.join(stage, rel.replace("/", os.sep))
                d = os.path.dirname(dst)
                if d:
                    os.makedirs(d, exist_ok=True)
                with zf.open(info) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 256)
        except Exception as e:
            return False, "解包失败：%s" % str(e)[:80], "", 0
    return True, "", stage, len(plan)


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


# ---------------- 增量包：待应用状态 + 退出时覆盖 ----------------
_pending_patch = None


def pending_patch():
    """已下载并解包好、等应用退出就覆盖的增量包信息；无则 None。
    返回 dict：{zip, stage, from, to, files}。"""
    global _pending_patch
    st = _pending_patch
    if not st:
        return None
    if not os.path.isfile(st.get("zip") or "") or \
            not os.path.isdir(st.get("stage") or ""):
        _pending_patch = None             # 临时目录被清了：状态作废
        return None
    return dict(st)


def set_pending_patch(st):
    """测试/恢复用；None 清空。"""
    global _pending_patch
    _pending_patch = dict(st) if st else None


def clear_pending_patch():
    set_pending_patch(None)


# 退出时执行的覆盖脚本。注意：**纯 ASCII**（cmd 按控制台代码页读它，中文会乱），
# 安装目录/启动器中文名全部走命令行参数（命令行是 Unicode，不受此限）。
# 用 ping 代替 timeout 做等待：stdin 被重定向时 timeout 会直接报错退出。
_APPLY_BAT = "_apply_patch.bat"

_APPLY_BAT_TEXT = r"""@echo off
rem Muppet Studio incremental patch applier (generated by update_checker.py).
rem args: 1=parent pid 2=stage dir 3=install dir 4=backup dir 5=launcher exe 6=log
setlocal enabledelayedexpansion
set "PID=%~1"
set "STAGE=%~2"
set "DST=%~3"
set "BAK=%~4"
set "EXE=%~5"
set "LOG=%~6"
set /a TRIES=0
>>"%LOG%" echo [%DATE% %TIME%] wait for pid %PID% to exit
:wait
if %TRIES% GEQ 150 goto apply
set /a TRIES+=1
tasklist /NH /FI "PID eq %PID%" 2>nul | findstr /R /C:"^[^ ]*  *%PID% " >nul
if not errorlevel 1 (
  >nul ping -n 2 127.0.0.1
  goto wait
)
:apply
rem grace period: let the app's child processes release file handles
>nul ping -n 3 127.0.0.1
>>"%LOG%" echo [%DATE% %TIME%] applying patch
if not exist "%STAGE%\" goto relaunch
if not exist "%DST%\" goto relaunch
set /a N=0
for /r "%STAGE%" %%f in (*) do (
  set "REL=%%f"
  set "REL=!REL:%STAGE%\=!"
  if exist "%DST%\!REL!" (
    set "BDIR=%BAK%\!REL!"
    for %%d in ("!BDIR!") do set "BDIR=%%~dpd"
    if not exist "!BDIR!" mkdir "!BDIR!" >nul 2>&1
    copy /y "%DST%\!REL!" "%BAK%\!REL!" >nul 2>&1
    set /a N+=1
  )
)
>>"%LOG%" echo [%DATE% %TIME%] backed up !N! old file^(s^) to "%BAK%"
robocopy "%STAGE%" "%DST%" /E /IS /IT /NFL /NDL /NJH /NJS /R:1 /W:1 /XF UPDATE.txt _apply_update.bat >nul
set "RC=%ERRORLEVEL%"
>>"%LOG%" echo [%DATE% %TIME%] robocopy exit code %RC%
if %RC% GEQ 8 (
  >>"%LOG%" echo [%DATE% %TIME%] copy failed, restoring backup
  robocopy "%BAK%" "%DST%" /E /IS /IT /NFL /NDL /NJH /NJS /R:1 /W:1 >nul
  >>"%LOG%" echo [%DATE% %TIME%] restore exit code !ERRORLEVEL!
) else (
  >>"%LOG%" echo [%DATE% %TIME%] patch applied ok
)
:relaunch
if not exist "%EXE%" goto cleanup
>>"%LOG%" echo [%DATE% %TIME%] relaunch "%EXE%"
start "" "%EXE%"
:cleanup
if exist "%STAGE%\" rmdir /s /q "%STAGE%" >nul 2>&1
(goto) 2>nul & del "%~f0"
"""


def _patch_backup_dir(install_dir, to_ver):
    tag = re.sub(r"[^0-9A-Za-z._-]", "_", str(to_ver or "0")) or "0"
    return os.path.join(install_dir, "_backup_before_update_v%s" % tag)


def launch_patch(install_dir):
    """DETACHED 拉起覆盖脚本：等本进程退出 -> 备份旧文件 -> 覆盖安装目录 ->
    重启程序。成功 True；调用方随后负责退出应用。

    为什么不在进程内直接覆盖：运行中的 .exe/.pyd 会被锁；而且覆盖到一半崩了
    就装坏了。脚本里先备份、失败自动回滚。
    """
    st = pending_patch()
    if not st:
        return False
    dst = os.path.abspath(str(install_dir or "")).rstrip("\\/")
    if not dst or not os.path.isdir(dst):
        return False
    stage = st.get("stage") or ""
    if not os.path.isdir(stage):
        return False
    d = update_dir()
    bat = os.path.join(d, _APPLY_BAT)
    log = os.path.join(d, "patch_apply.log")
    bak = _patch_backup_dir(dst, st.get("to"))
    # 启动器中文名只走命令行参数，绝不写进 bat 正文（见 _APPLY_BAT_TEXT 注释）
    exe = os.path.join(dst, "木偶工作室.exe")
    try:
        with open(bat, "w", encoding="ascii", newline="\r\n") as f:
            f.write(_APPLY_BAT_TEXT)
    except OSError:
        return False
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    argv = [comspec, "/d", "/c", bat, str(os.getpid()),
            stage.rstrip("\\/"), dst, bak, exe, log]
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         cwd=d, close_fds=True)
        return True
    except Exception:
        return False


# ---------------- 后台线程 ----------------
class UpdateThread(QThread):
    """点「检查更新」后的全流程后台线程（主线程零阻塞）。

    信号：status(str 人话) / progressed(int done, int total) /
          ready(str 安装包路径) / patch_ready(str zip 路径, str 目标版本) /
          failed(str) / uptodate(str 远端版本)。
    v3.5.3 起优先发 patch_ready（增量包），只有增量包不可用时才发 ready（整包）。"""

    status = Signal(str)
    progressed = Signal(int, int)
    ready = Signal(str)
    patch_ready = Signal(str, str)
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
        # ---- 增量包优先（v3.5.3）：清单里有本机版本的增量包就只下几十~几百 KB ----
        patch = find_patch(meta, self.local_version)
        if patch is not None:
            handled = False
            try:
                handled = self._try_patch(meta, remote, patch)
            except Exception as e:
                self.status.emit("增量包处理异常，改下完整安装包：%s" % str(e)[:60])
            if handled:
                return
            if self._cancelled():
                return
            self.status.emit("改用完整安装包 v%s…" % remote)
        url = RELEASE_URL_TPL.format(ver=remote)
        urls = [url, MIRROR_PREFIX + url]
        target = installer_path(remote)
        # 上一轮已下好且仍是要的版本：直接复用
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            try:
                if self._verify(target, meta) and self._cross_ok(meta):
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
            if not self._cross_ok(meta):
                os.remove(target)
                self.failed.emit("多源 sha256 交叉校验不一致（镜像可能被篡改），包已删除")
                return
        except OSError as e:
            self.failed.emit("校验读写失败：%s" % str(e)[:80])
            return
        set_pending_installer(target)
        self.status.emit("下载完成")
        self.ready.emit(target)

    def _cross_ok(self, meta):
        """换源交叉校验 installer_sha256；conflict 拒绝，其余放行（离线宽容）。"""
        want = str(meta.get("installer_sha256") or "").strip().lower()
        verdict, _ = cross_check_sha(want)
        if verdict == "unreachable":
            self.status.emit("其余版本源不可达，未做交叉校验")
            return True
        return verdict == "agree"

    # ---------------- 增量包 ----------------
    def _try_patch(self, meta, remote, patch):
        """走增量包链路。True = 已处理（成功或用户取消），False = 让上层下整包。"""
        frm = patch.get("from")
        to = patch.get("to") or remote
        self.status.emit("发现新版本 v%s，下载增量包（%s）…"
                         % (to, _human_size(patch.get("size"))))
        try:
            target = patch_path(to, frm)
        except OSError:
            return False
        url = patch_url(to, frm)
        urls = [url, MIRROR_PREFIX + url]
        # 上一轮已下好且校验通过：直接复用，不重复下载
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            try:
                if self._verify_patch(target, patch) and self._cross_patch_ok(patch):
                    return self._finish_patch(target, to, patch)
            except OSError:
                pass
        ok = _download_installer(urls, target,
                                 lambda g, t: self.progressed.emit(int(g), int(t)),
                                 self._cancelled)
        if self._cancelled():
            self.status.emit("已取消下载（部分文件保留，可续传）")
            return True
        if not ok or not os.path.isfile(target):
            self.status.emit("增量包下载失败（可能还没发布），改下完整安装包…")
            return False
        if not _zip_head_ok(target):
            self._drop(target)
            self.status.emit("增量包格式异常（非 zip），已删除，改下完整安装包…")
            return False
        if not self._verify_patch(target, patch):
            self._drop(target)
            self.status.emit("增量包 sha256 校验不符，坏包已删除，改下完整安装包…")
            return False
        if not self._cross_patch_ok(patch):
            self._drop(target)
            self.status.emit("多源增量包校验不一致（镜像可能被篡改），包已删除，"
                             "改下完整安装包…")
            return False
        return self._finish_patch(target, to, patch)

    def _finish_patch(self, target, to_ver, patch):
        """解包到暂存目录并登记待应用状态；成功 True。"""
        ok, msg, stage, n = stage_patch(target, to_ver)
        if not ok:
            self.status.emit("%s，改下完整安装包…" % msg)
            return False
        set_pending_patch({"zip": target, "stage": stage,
                           "from": patch.get("from"), "to": to_ver, "files": n})
        self.status.emit("增量包就绪（%d 个文件）" % n)
        self.patch_ready.emit(target, to_ver)
        return True

    @staticmethod
    def _drop(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _verify_patch(self, target, patch):
        """增量包 sha256 校验（fail-closed，同整包策略）。"""
        want = str(patch.get("sha256") or "").strip().lower()
        if not want:
            self.status.emit("增量包清单未声明 sha256，拒绝（fail-closed）")
            return False
        try:
            got = _sha256(target)
        except OSError:
            return False
        return got == want

    def _cross_patch_ok(self, patch):
        """换源交叉校验增量包 sha256；同理 conflict 拒绝、不可达放行。"""
        verdict, _ = cross_check_patch_sha(patch.get("sha256"),
                                           patch.get("from"), patch.get("to"))
        if verdict == "unreachable":
            self.status.emit("其余版本源不可达，未做增量包交叉校验")
            return True
        return verdict == "agree"

    def _verify(self, target, meta):
        """sha256 校验（fail-closed，v3.4.0 安全加固）：version.json 未声明
        installer_sha256 一律拒绝——镜像代理可整体伪造清单，缺声明视为不可信。"""
        want = str(meta.get("installer_sha256") or "").strip().lower()
        if not want:
            self.status.emit("清单未声明 sha256，拒绝安装（fail-closed）")
            return False
        try:
            got = _sha256(target)
        except OSError:
            return False
        return got == want
