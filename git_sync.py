# -*- coding: utf-8 -*-
"""git 版本同步（task-71）：对比本地 v3 版本 vs git 远端，远端新则备份后覆盖。

用户定死的三条规则：
1. **只比版本号**（l2d_rig_qt.py 的 VERSION），哪个新用哪个；不比文件内容。
2. **工作区有未提交改动 -> 直接拒绝**，绝不自动覆盖在途工作，不提供强制覆盖。
3. **只在用户主动点更新按钮时才比对** —— 本模块 import 时零网络、零 git 调用，
   没有任何自动检测/轮询/启动检查。所有函数都是被显式调用的纯函数式入口。

设计要点：
- 无 Qt 依赖（可被后台线程调用）；所有失败都返回 (ok=False, msg)，**绝不抛异常**。
- 覆盖前必备份到 %TEMP%\\muppet_gitsync\\<时间戳>\\，附 manifest.json 便于回滚。
- 只 checkout `git diff --name-only HEAD origin/main` 清单内的文件，
  **不用 reset --hard / 不 checkout 整分支**，本地 HEAD 保持可回滚。
"""
import json
import os
import shutil
import subprocess
import time

# ---------------- 常量（测试可整体替换） ----------------
PROJ_DIR = os.path.dirname(os.path.abspath(__file__))
GIT_BIN = "git"
REMOTE = "origin"
BRANCH = "main"
VERSION_FILE = "l2d_rig_qt.py"          # 单一版本源
BACKUP_ROOT = os.path.join(
    os.environ.get("TEMP") or "/tmp", "muppet_gitsync")
TIMEOUT = 20                            # 单条 git 命令超时（秒）


# ---------------- 版本工具 ----------------
def parse_version(s):
    """'3.4.0' / 'v3.4.0' -> (3, 4, 0)；非法返回 None。"""
    if not s:
        return None
    t = str(s).strip().lstrip("vV")
    parts = []
    for seg in t.split(".")[:4]:
        num = ""
        for ch in seg:
            if ch.isdigit():
                num += ch
            else:
                break
        if not num:
            return None
        parts.append(int(num))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def cmp_version(a, b):
    """返回 -1/0/1；任一非法返回 None。数字三元组比较（3.10.0 > 3.9.9）。"""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    return (pa > pb) - (pa < pb)


def _read_version_from_text(text):
    """从 l2d_rig_qt.py 源码文本里抽 VERSION = "x.y.z"。"""
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("VERSION") and "=" in s:
            v = s.split("=", 1)[1].strip().strip("'\"")
            if parse_version(v):
                return v
    return None


def local_version(proj_dir=None):
    p = os.path.join(proj_dir or PROJ_DIR, VERSION_FILE)
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            return _read_version_from_text(f.read())
    except OSError:
        return None


# ---------------- git 工具 ----------------
def _git(proj_dir, *args, timeout=TIMEOUT):
    """跑一条 git 命令 -> (rc, stdout, stderr)；git 缺失/超时也返回 rc!=0。"""
    cmd = [GIT_BIN] + list(args)
    try:
        r = subprocess.run(cmd, cwd=proj_dir, capture_output=True, text=True,
                           timeout=timeout, errors="ignore")
        return r.returncode, r.stdout or "", r.stderr or ""
    except FileNotFoundError:
        return 127, "", "未找到 git 命令"
    except subprocess.TimeoutExpired:
        return 124, "", "git 命令超时"
    except OSError as e:
        return 126, "", str(e)


def is_repo(proj_dir=None):
    d = proj_dir or PROJ_DIR
    return os.path.isdir(os.path.join(d, ".git"))


def dirty_files(proj_dir=None):
    """未提交改动列表（含未跟踪）；非仓库/git 不可用返回 None。"""
    d = proj_dir or PROJ_DIR
    if not is_repo(d):
        return None
    rc, out, _err = _git(d, "status", "--porcelain")
    if rc != 0:
        return None
    files = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # porcelain 格式：两个状态列 + 一个空格 + 路径
        #   已跟踪改动 -> " M path"（M 在第 2 列，第 1 列是空格）
        #   未跟踪     -> "?? path"
        # 因此固定按前 3 字符切（XY + 空格），不能用 strip 后的偏移。
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:                      # 重命名：取新名
            path = path.split(" -> ", 1)[1]
        path = path.strip()
        if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
            path = path[1:-1]
        if path:
            files.append(path)
    return files


def remote_version(proj_dir=None, fetch=True):
    """远端 VERSION：优先读 origin/<branch>:l2d_rig_qt.py。失败返回 None。"""
    d = proj_dir or PROJ_DIR
    if not is_repo(d):
        return None
    if fetch:
        rc, _o, _e = _git(d, "fetch", REMOTE, BRANCH)
        if rc != 0:
            # fetch 失败（无网）仍尝试用已有的远端引用
            pass
    rc, out, _e = _git(d, "show", "%s/%s:%s" % (REMOTE, BRANCH, VERSION_FILE))
    if rc != 0:
        return None
    return _read_version_from_text(out)


def remote_commit(proj_dir=None):
    d = proj_dir or PROJ_DIR
    rc, out, _e = _git(d, "rev-parse", "%s/%s" % (REMOTE, BRANCH))
    return out.strip() if rc == 0 else None


def local_commit(proj_dir=None):
    d = proj_dir or PROJ_DIR
    rc, out, _e = _git(d, "rev-parse", "HEAD")
    return out.strip() if rc == 0 else None


# ---------------- 检测 ----------------
def state(proj_dir=None, fetch=True):
    """只读检测（不写盘）。任何失败返回 ok=False + msg，绝不抛异常。

    返回 dict:
      ok / local_version / remote_version / relation
      relation ∈ local_newer | remote_newer | same | unknown
      dirty / dirty_files / git_ok / msg
    """
    d = proj_dir or PROJ_DIR
    res = {"ok": False, "local_version": None, "remote_version": None,
           "relation": "unknown", "dirty": False, "dirty_files": [],
           "git_ok": False, "msg": ""}
    if not os.path.isdir(d):
        res["msg"] = "工程目录不存在：%s" % d
        return res
    if not is_repo(d):
        res["msg"] = "不是 git 仓库（缺少 .git）：%s" % d
        return res
    res["git_ok"] = True
    lv = local_version(d)
    res["local_version"] = lv
    if lv is None:
        res["msg"] = "读不到本地版本号（%s 里没有 VERSION）" % VERSION_FILE
        return res
    dirty = dirty_files(d)
    if dirty is None:
        res["msg"] = "git status 执行失败（git 不可用？）"
        return res
    res["dirty"] = bool(dirty)
    res["dirty_files"] = dirty
    rv = remote_version(d, fetch=fetch)
    res["remote_version"] = rv
    if rv is None:
        res["msg"] = "读不到远端版本（网络或远端引用不可用）"
        return res
    c = cmp_version(lv, rv)
    if c is None:
        res["relation"] = "unknown"
        res["msg"] = "版本号无法比较：本地 %r / 远端 %r" % (lv, rv)
        return res
    res["relation"] = ("same" if c == 0
                       else ("local_newer" if c > 0 else "remote_newer"))
    res["ok"] = True
    if res["relation"] == "remote_newer":
        res["msg"] = "远端 %s 比本地 %s 新，可以更新" % (rv, lv)
    elif res["relation"] == "local_newer":
        res["msg"] = "本地 %s 比远端 %s 新，无需更新" % (lv, rv)
    else:
        res["msg"] = "本地与远端版本相同（%s）" % lv
    if res["dirty"]:
        res["msg"] += "；但检测到 %d 个未提交改动，请先提交后再更新" % len(dirty)
    return res


def check(proj_dir=None):
    """state() 的别名（语义：只读检测）。"""
    return state(proj_dir=proj_dir, fetch=True)


def diff_files(proj_dir=None):
    """远端相对本地改动的文件清单（git diff --name-only HEAD origin/main）。"""
    d = proj_dir or PROJ_DIR
    rc, out, _e = _git(d, "diff", "--name-only", "HEAD",
                       "%s/%s" % (REMOTE, BRANCH))
    if rc != 0:
        return []
    return [x.strip() for x in out.splitlines() if x.strip()]


# ---------------- 备份 / 回滚 ----------------
def _backup_files(proj_dir, files):
    """把将被覆盖的文件备份到 BACKUP_ROOT/<时间戳>/，返回备份目录。"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_ROOT, stamp)
    n = 1
    while os.path.isdir(dst):
        dst = os.path.join(BACKUP_ROOT, "%s_%d" % (stamp, n))
        n += 1
    os.makedirs(dst, exist_ok=True)
    saved = []
    for rel in files:
        src = os.path.join(proj_dir, rel.replace("/", os.sep))
        if not os.path.isfile(src):
            continue
        out = os.path.join(dst, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        try:
            shutil.copy2(src, out)
            saved.append(rel)
        except OSError:
            pass
    manifest = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "proj_dir": proj_dir,
        "local_head": local_commit(proj_dir),
        "remote_commit": remote_commit(proj_dir),
        "files": saved,
    }
    try:
        with open(os.path.join(dst, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return dst, saved


def rollback(backup_dir, proj_dir=None):
    """从备份目录还原覆盖过的文件 -> (ok, msg, restored[])。"""
    d = proj_dir or PROJ_DIR
    if not backup_dir or not os.path.isdir(backup_dir):
        return False, "备份目录不存在：%r" % backup_dir, []
    man_p = os.path.join(backup_dir, "manifest.json")
    files = []
    if os.path.isfile(man_p):
        try:
            with open(man_p, encoding="utf-8") as f:
                files = json.load(f).get("files") or []
        except (OSError, ValueError):
            files = []
    if not files:
        # 没清单就整目录扫
        for root, _ds, fs in os.walk(backup_dir):
            for fn in fs:
                if fn == "manifest.json":
                    continue
                files.append(os.path.relpath(os.path.join(root, fn),
                                             backup_dir).replace(os.sep, "/"))
    restored = []
    for rel in files:
        src = os.path.join(backup_dir, rel.replace("/", os.sep))
        dst = os.path.join(d, rel.replace("/", os.sep))
        if not os.path.isfile(src):
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            restored.append(rel)
        except OSError:
            pass
    return True, "已从备份还原 %d 个文件" % len(restored), restored


# ---------------- 覆盖 ----------------
def pull_overwrite(proj_dir=None):
    """远端更新则备份后覆盖本地差异文件 -> (ok, msg, report)。

    严格顺序：dirty 拒绝 -> 版本比较(local_newer/same 拒绝) -> 备份 -> checkout。
    绝不抛异常。
    """
    d = proj_dir or PROJ_DIR
    report = {"backup_dir": None, "files": [], "skipped": [],
              "local_version": None, "remote_version": None,
              "local_head": None, "remote_commit": None}
    try:
        st = state(proj_dir=d, fetch=True)
        report["local_version"] = st.get("local_version")
        report["remote_version"] = st.get("remote_version")
        if not st.get("ok"):
            return False, st.get("msg") or "检测失败", report
        # 规则 2：脏工作区一律拒绝
        if st.get("dirty"):
            names = st.get("dirty_files") or []
            head = "、".join(os.path.basename(x) for x in names[:5])
            more = "等 %d 个文件" % len(names) if len(names) > 5 else ""
            return (False,
                    "检测到 %d 个未提交改动（%s%s），请先提交或暂存后再更新。"
                    "为保护在途工作，本次不做任何改动。" % (len(names), head, more),
                    report)
        rel = st.get("relation")
        if rel == "local_newer":
            return False, ("本地版本 %s 比远端 %s 新，无需更新（未做任何改动）"
                           % (st.get("local_version"), st.get("remote_version"))), report
        if rel == "same":
            return False, "本地与远端版本相同（%s），无需更新" % st.get(
                "local_version"), report
        if rel != "remote_newer":
            return False, "无法确定版本关系，已中止（%s）" % (st.get("msg") or ""), report
        # 取差异清单
        files = diff_files(d)
        if not files:
            return False, "远端虽版本更新，但没有差异文件可覆盖", report
        report["local_head"] = local_commit(d)
        report["remote_commit"] = remote_commit(d)
        # 规则 3：先备份
        bdir, saved = _backup_files(d, files)
        report["backup_dir"] = bdir
        report["files"] = list(files)
        # 只 checkout 差异文件（不 reset --hard / 不 checkout 整分支）
        rc, _o, err = _git(d, "checkout", "%s/%s" % (REMOTE, BRANCH), "--", *files)
        if rc != 0:
            return (False,
                    "git checkout 失败：%s（备份在 %s，可用 rollback 还原）"
                    % (err.strip() or "unknown", bdir), report)
        # 校验文件真的落地了
        missing = [f for f in files
                   if not os.path.exists(os.path.join(d, f.replace("/", os.sep)))]
        report["skipped"] = missing
        return (True,
                "已从远端 %s 覆盖 %d 个文件（原 %s）。备份：%s。重启应用生效。"
                % (st.get("remote_version"), len(files), st.get("local_version"),
                   bdir), report)
    except Exception as e:                     # 兜底：任何意外都不抛给 UI
        return False, "更新过程异常：%s" % e, report


# ---------------- CLI（手工排查用，非自动触发） ----------------
def _main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    d = None
    if "--dir" in argv:
        i = argv.index("--dir")
        if i + 1 < len(argv):
            d = argv[i + 1]
    if "--pull" in argv:
        ok, msg, rep = pull_overwrite(d)
        print(("OK " if ok else "FAIL ") + msg)
        if rep.get("backup_dir"):
            print("backup:", rep["backup_dir"])
        return 0 if ok else 1
    if "--rollback" in argv:
        i = argv.index("--rollback")
        bdir = argv[i + 1] if i + 1 < len(argv) else ""
        ok, msg, _r = rollback(bdir, d)
        print(("OK " if ok else "FAIL ") + msg)
        return 0 if ok else 1
    st = state(d)
    for k in ("ok", "local_version", "remote_version", "relation", "dirty",
              "git_ok", "msg"):
        print("%-14s %s" % (k + ":", st.get(k)))
    if st.get("dirty_files"):
        print("dirty_files:   %s" % ", ".join(st["dirty_files"][:10]))
    return 0 if st.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
