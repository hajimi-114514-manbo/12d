# -*- coding: utf-8 -*-
"""WebGAL 运行时包（WebGAL-*-web.zip）本地导入与状态查询（E2a，task-40）。

纯模块 + CLI，零必选第三方依赖（zipfile/shutil/json 足够），不 import Qt/cv2。
task-36 的 E2 会在此基础上接模板页卡片 / playable 导出 / remote 命令。

目录约定：
    运行时解压落点 = <templates 父目录>/webgal_runtime/
    默认父目录 = 本模块所在目录的 templates/；env MUPPET_TEMPLATES_DIR 覆盖父目录
    （即设 MUPPET_TEMPLATES_DIR 后落点为 <env>/webgal_runtime/）。

API：
    status()
        -> {"installed": bool, "version": str|None, "path": str, "files": int}
        读 webgal_runtime/；version 探测顺序：package.json 的 version 字段
        -> index.html 正文里的版本号 -> 资源文件名里的版本号；都探不到返回
        None，但 installed 仍为 True（只要有内容）。
    import_zip(zip_path, confirm_overwrite=False)
        -> (ok, msg, data)
        is_zipfile 校验 -> namelist 找 index.html（允许单顶层目录自动剥离，
        与 asset_downloader._extract_zip 同语义）-> zip-slip 防护（abspath
        + os.sep 前缀）-> 已安装且未 confirm 回 (False, "已安装需确认覆盖",
        {"needs_confirm": True}) -> 先解到临时目录再原子换入 webgal_runtime/
        -> data 含 {"version": str|None, "files": int}。

CLI：
    python runtime_pack.py --status
    python runtime_pack.py --import <webgal-*-web.zip> [--confirm]
    成功 rc=0，失败 rc=1。
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile

_VERSION_RE = re.compile(r"v?\d+\.\d+(?:\.\d+)*")


# ================= 目录解析 =================
def templates_dir():
    """templates 父目录：env MUPPET_TEMPLATES_DIR 覆盖，否则模块同级 templates/。"""
    env = os.environ.get("MUPPET_TEMPLATES_DIR", "").strip()
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def runtime_dir():
    """WebGAL 运行时解压落点。"""
    return os.path.join(templates_dir(), "webgal_runtime")


# ================= 小工具 =================
def _has_content(d):
    if not os.path.isdir(d):
        return False
    try:
        for _root, _dirs, files in os.walk(d):
            if files:
                return True
    except OSError:
        return False
    return False


def _count_files(d):
    if not os.path.isdir(d):
        return 0
    n = 0
    try:
        for _root, _dirs, files in os.walk(d):
            n += len(files)
    except OSError:
        return 0
    return n


def _find_strip(names):
    """单顶层目录自动剥离：所有条目共享同一顶层且该顶层是目录时返回 'top/'。"""
    tops = {n.split("/")[0] for n in names if n.strip()}
    if len(tops) == 1:
        top = tops.pop()
        if any(n.startswith(top + "/") for n in names):
            return top + "/"
    return None


def _strip_name(name, strip):
    if strip and name.startswith(strip):
        return name[len(strip):]
    return name


def _read_version(d):
    """探测运行时版本：package.json -> index.html 正文 -> 资源文件名。探不到 None。"""
    if not os.path.isdir(d):
        return None
    # 1. package.json 元数据
    pj = os.path.join(d, "package.json")
    if os.path.isfile(pj):
        try:
            with open(pj, encoding="utf8") as f:
                v = json.load(f).get("version")
            if isinstance(v, str) and v.strip():
                return v.strip()
        except Exception:
            pass
    # 2. index.html 正文
    ih = os.path.join(d, "index.html")
    if os.path.isfile(ih):
        try:
            with open(ih, encoding="utf8", errors="ignore") as f:
                m = _VERSION_RE.search(f.read())
            if m:
                return m.group(0).lstrip("v")
        except Exception:
            pass
    # 3. 资源文件名
    try:
        for _root, _dirs, files in os.walk(d):
            for fn in files:
                m = _VERSION_RE.search(fn)
                if m:
                    return m.group(0).lstrip("v")
    except OSError:
        pass
    return None


def _extract_to(z, names, dest, strip):
    """解压到 dest（先建目录），逐条做 zip-slip 防护。返回落盘文件数。"""
    root = os.path.abspath(dest)
    count = 0
    for n in names:
        if n.endswith("/"):
            continue
        rel = _strip_name(n, strip)
        if not rel:
            continue
        target = os.path.join(dest, rel.replace("/", os.sep))
        abs_target = os.path.abspath(target)
        # zip-slip：必须落在 dest 内（带分隔符比较，防兄弟目录名前缀误判）
        if abs_target != root and not abs_target.startswith(root + os.sep):
            continue
        parent = os.path.dirname(target)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:
                continue
        try:
            with z.open(n) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            count += 1
        except OSError:
            continue
    return count


# ================= 状态 / 导入 =================
def status():
    """运行时包状态三态：未安装 / 已安装(版本未知) / 已安装(版本已知)。"""
    d = runtime_dir()
    installed = _has_content(d)
    return {
        "installed": installed,
        "version": _read_version(d) if installed else None,
        "path": d,
        "files": _count_files(d) if installed else 0,
    }


def import_zip(zip_path, confirm_overwrite=False):
    """导入 WebGAL 运行时 zip 到 webgal_runtime/。返回 (ok, msg, data)。"""
    zip_path = str(zip_path or "")
    if not zip_path or not os.path.isfile(zip_path):
        return False, "zip 文件不存在: %s" % (zip_path or "(空)"), {}
    if not zipfile.is_zipfile(zip_path):
        return False, "不是有效的 zip 文件: %s" % zip_path, {}

    dest = runtime_dir()
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            strip = _find_strip(names)
            has_index = any(_strip_name(n, strip) == "index.html" for n in names)
            if not has_index:
                return False, "压缩包内未找到 index.html", {}

            # 已安装需确认覆盖
            if _has_content(dest) and not confirm_overwrite:
                return False, "已安装需确认覆盖", {"needs_confirm": True}

            parent = os.path.dirname(dest) or "."
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                return False, "目标目录不可写: %r" % e, {}
            tmp = tempfile.mkdtemp(prefix=".webgal_rt_", dir=parent)
            try:
                nfiles = _extract_to(z, names, tmp, strip)
                if nfiles <= 0:
                    return False, "压缩包内没有可解压的文件", {}
                if os.path.isdir(dest):
                    shutil.rmtree(dest, ignore_errors=True)
                os.replace(tmp, dest)
                version = _read_version(dest)
                msg = "已导入 WebGAL 运行时（版本 %s，%d 个文件）" % (
                    version or "未知", nfiles)
                return True, msg, {"version": version, "files": nfiles}
            except Exception as e:
                shutil.rmtree(tmp, ignore_errors=True)
                return False, "导入失败: %r" % e, {}
            finally:
                if os.path.isdir(tmp):
                    shutil.rmtree(tmp, ignore_errors=True)
    except zipfile.BadZipFile as e:
        return False, "zip 读取失败: %r" % e, {}
    except Exception as e:
        return False, "导入异常: %r" % e, {}


# ================= CLI =================
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="WebGAL 运行时包导入与状态查询")
    ap.add_argument("--status", action="store_true", help="查询运行时状态")
    ap.add_argument("--import", dest="import_zip", default=None,
                    help="导入 WebGAL 运行时 zip")
    ap.add_argument("--confirm", action="store_true",
                    help="覆盖已安装的运行时（需确认）")
    a = ap.parse_args(argv)

    if a.status:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if a.import_zip:
        ok, msg, data = import_zip(a.import_zip, a.confirm)
        print(msg)
        if data:
            print("data: %s" % json.dumps(data, ensure_ascii=False))
        return 0 if ok else 1
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
