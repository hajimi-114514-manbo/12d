# -*- coding: utf-8 -*-
"""语音模块的 DSH 联动命令（v3.6.2 后新增）。

为什么单独一个模块
------------------
remote_control.py 已经 1997 行，再往里塞语音逻辑会让它更难读。
语音这块的特点是：**全部是纯文件系统 / 网络 I/O，不碰 Qt**，
所以可以整块拆出来，由 remote_control 转调。

**只暴露已真跑通的能力**（需求池【语音V1】已实测端到端）：
    voice.status    引擎状态 + 服务连通性
    voice.probe     探测指定 host:port
    voice.list      已导入的音色
    voice.stats     音色统计
    voice.validate  校验一对音色文件
    voice.import    导入一对音色
    voice.remove    删除一个音色
    voice.refs      参考音频库（按角色列出）
    voice.refs.add  导入一段参考音频
    voice.refs.rm   删除一段参考音频
    voice.refs.pick 给某角色挑一段参考音频（**只查询**，不改状态）
    voice.refs.sel  **选定**某角色用哪段参考音频（写入记忆，合成会用它）
    voice.refs.unsel 清掉某个角色的已选（留空 = 清掉全部）

**不暴露 voice.map / voice.synth / voice.batch** —— 那是 V2/V3 的事，
还没实现。列出来 AI 会照着调然后报「未知命令」，比不列更糟。

命令约定（与 remote_control 其余命令一致）：
    返回 (ok: bool, msg: str, data: dict|None)
"""
import os
import sys


def _voice_paths():
    import voice_paths
    return voice_paths


def _voice_probe():
    import voice_probe
    return voice_probe


def _voice_library():
    import voice_library
    return voice_library


def _refs_module():
    import voice_refs
    return voice_refs


# ---------------- 读命令 ----------------
def _voice_status(_wb, args):
    """语音状态：引擎齐不齐 + 服务通不通。

    这是 AI 判断「能不能配音」的第一入口。先看引擎，再看服务。
    """
    VP = _voice_probe()
    r = VP.probe_local(timeout=float(args.get("timeout") or 2.0))
    svc = r.service                       # ProbeResult | None
    data = {
        "engine_ready": bool(r.engine),
        "engine_dir": r.dir or "",
        "python": r.python or "",
        "api": r.api or "",
        "service_ok": bool(svc.ok) if svc else False,
        "service_msg": (svc.msg if svc else "") or "",
        "msg": r.msg or "",
    }
    # 引擎不在时也把候选位置报出来，方便 AI 告诉用户「去哪装」
    try:
        data["candidates"] = [c["dir"] for c in
                               _voice_paths().describe().get("candidates", [])]
    except Exception:
        data["candidates"] = []
    ok = bool(r.engine)
    return ok, ("语音引擎就绪" if ok else "语音引擎未就绪"), data


def _voice_probe_cmd(_wb, args):
    """探测指定地址的语音服务。

    args:
      host: 主机（默认 127.0.0.1）
      port: 端口（默认 9880）
      timeout: 超时秒（默认 4）
    """
    VP = _voice_probe()
    host = str(args.get("host") or VP.DEFAULT_HOST)
    port = int(args.get("port") or VP.DEFAULT_PORT)
    timeout = float(args.get("timeout") or 4.0)
    r = VP.probe(host, port, timeout=timeout)
    return bool(r.ok), (r.msg or ""), r.to_dict()


def _voice_list(_wb, _args):
    """列出已导入的音色（同名 ckpt+pth 成对才算完整）。"""
    VL = _voice_library()
    rows = VL.voices()
    items = [{"stem": r["stem"], "ok": bool(r["ok"]),
              "size": int(r.get("size") or 0),
              "ckpt": r.get("ckpt") or "", "pth": r.get("pth") or ""}
             for r in rows]
    complete = sum(1 for r in items if r["ok"])
    return True, "共 %d 个音色（完整 %d）" % (len(items), complete), {
        "voices": items, "total": len(items), "complete": complete}


def _voice_stats(_wb, _args):
    """音色统计（总数 / 完整数 / 占用体积）。"""
    VL = _voice_library()
    st = VL.stats()
    return True, ("%d 个音色，占 %.1f MB" %
                  (st["total"], st["size"] / 1048576.0)), st


def _voice_validate(_wb, args):
    """校验一对音色文件能不能导入（纯检查，不动文件）。

    args:
      ckpt: .ckpt 路径（GPT 侧）
      pth:  .pth 路径（SoVITS 侧）
    """
    VL = _voice_library()
    ck = str(args.get("ckpt") or "")
    pt = str(args.get("pth") or "")
    ok, msg = VL.validate_pair(ck, pt)
    return ok, msg, {"ckpt": ck, "pth": pt, "valid": bool(ok)}


# ---------------- 写命令 ----------------
def _voice_import(_wb, args):
    """导入一对音色到引擎的 voices/ 目录。

    args:
      ckpt: .ckpt 路径（GPT 侧）
      pth:  .pth 路径（SoVITS 侧）
      name: 显示名（留空用文件名；重名自动加序号）
    """
    VL = _voice_library()
    ck = str(args.get("ckpt") or "")
    pt = str(args.get("pth") or "")
    name = str(args.get("name") or "")
    ok, msg = VL.import_voice(ck, pt, name=name)
    return ok, msg, {"stem": msg.replace("已导入音色：", "") if ok else ""}


def _voice_remove(_wb, args):
    """删除一个音色（两个文件一起删）。

    args:
      stem: 音色名（voice.list 查到的）
    """
    VL = _voice_library()
    stem = str(args.get("stem") or args.get("name") or "").strip()
    if not stem:
        return False, "缺少 stem（音色名）", None
    ok, msg = VL.remove_voice(stem)
    return ok, msg, {"stem": stem} if ok else None


# ---------------- 参考音频 ----------------
def _voice_refs(_wb, args):
    """参考音频库：按角色列出。

    args:
      role: 只看某个角色（留空列全部）
    """
    VR = _refs_module()
    role = str(args.get("role") or "").strip()
    sel = VR.selections()
    if role:
        items = [{"role": role, "refs": VR.refs_for(role),
                  "selected": sel.get(role, "")}]
    else:
        items = [{"role": r, "refs": VR.refs_for(r),
                  "selected": sel.get(r, "")} for r in VR.roles()]
    return True, "共 %d 个角色" % len(items), {
        "roles": items,
        "selected": sel,
        "stats": VR.stats(),
    }


def _voice_refs_add(_wb, args):
    """导入一段参考音频。

    args:
      path: 源音频路径（wav）
      role: 角色名（必填）
      name: 显示名（留空用源文件名；文件名通常就是说话内容）
    """
    VR = _refs_module()
    p = str(args.get("path") or args.get("src") or "")
    role = str(args.get("role") or "").strip()
    name = str(args.get("name") or "")
    if not p:
        return False, "缺少 path（源音频路径）", None
    if not role:
        return False, "缺少 role（角色名）", None
    ok, msg, info = VR.import_ref(p, role, name)
    return ok, msg, info


def _voice_refs_rm(_wb, args):
    """删除一段参考音频。

    args:
      role: 角色名
      name:  参考音频文件名（voice.refs 查到的）
    """
    VR = _refs_module()
    role = str(args.get("role") or "").strip()
    name = str(args.get("name") or "").strip()
    if not role or not name:
        return False, "缺少 role / name", None
    ok, msg = VR.remove_ref(role, name)
    return ok, msg, {"role": role, "name": name} if ok else None


def _voice_refs_pick(_wb, args):
    """给某角色挑一段参考音频（合成会用它）。

    args:
      role: 角色名
      name: 指定段（留空取该角色的第一段）
    """
    VR = _refs_module()
    role = str(args.get("role") or "").strip()
    name = str(args.get("name") or "")
    p = VR.pick_ref(role, name)
    sel = VR.selection_of(role)
    if not p:
        fb = VR.fallback_ref()
        return False, ("角色「%s」没有参考音频" % role), {
            "role": role,
            "fallback": fb,
            "hint": ("会退回引擎自带 ref.wav（凑友希娜的声音，音色会串）"
                     if fb else "引擎也没有 ref.wav"),
        }
    return True, "当前会用到 %s" % os.path.basename(p), {
        "role": role, "path": p, "name": os.path.basename(p),
        # 是「选定的」还是「兜底取第一段」—— 让 AI 能分清
        "selected": sel or "",
        "is_selected": bool(sel) and os.path.basename(p) == sel,
    }


def _voice_refs_sel(_wb, args):
    """**选定**某角色用哪段参考音频（写入记忆，合成会用它）。

    和 voice.refs.pick 的区别：pick 只是「问一下现在会用哪段」，
    sel 是「就这么定下来」——写进 refs/_selected.json，跨会话保持。

    args:
      role: 角色名（必填）
      name: 参考音频文件名（必填，须是该角色已有的段；
            先 voice.refs 看有哪些）
    """
    VR = _refs_module()
    role = str(args.get("role") or "").strip()
    name = str(args.get("name") or "").strip()
    if not role:
        return False, "缺少 role（角色名）", None
    if not name:
        return False, "缺少 name（参考音频文件名）", None
    ok, msg, info = VR.select_ref(role, name)
    if ok and info:
        info["pick"] = VR.pick_ref(role)
    return ok, msg, info


def _voice_refs_unsel(_wb, args):
    """清掉某个角色的已选（回到「取第一段」的兜底行为）。

    args:
      role: 角色名（留空 = 清掉全部）
    """
    VR = _refs_module()
    role = str(args.get("role") or "").strip()
    ok, msg, data = VR.clear_selection(role)
    return ok, msg, data


# ---------------- 注册表 ----------------
# 供 remote_control 直接转调；这些命令都不碰 Qt，走 HTTP 线程直跑。
HANDLERS = {
    "voice.status": _voice_status,
    "voice.probe": _voice_probe_cmd,
    "voice.list": _voice_list,
    "voice.stats": _voice_stats,
    "voice.validate": _voice_validate,
    "voice.import": _voice_import,
    "voice.remove": _voice_remove,
    "voice.refs": _voice_refs,
    "voice.refs.add": _voice_refs_add,
    "voice.refs.rm": _voice_refs_rm,
    "voice.refs.pick": _voice_refs_pick,
    "voice.refs.sel": _voice_refs_sel,
    "voice.refs.unsel": _voice_refs_unsel,
}

# 走 HTTP 线程直跑的命令（纯 I/O，不进主线程队列）。
# 与 remote_control 里 assets.query / runtime.import 同款处理 ——
# 探测要等 TCP 超时（最长 4s），进主线程队列会把 UI 卡住。
HTTP_ONLY = frozenset(HANDLERS.keys())


def run(cmd, args):
    """按名字执行语音命令。返回 (ok, msg, data)。

    未知命令返回 (False, "未知语音命令: xxx", None) —— 不抛异常。
    """
    fn = HANDLERS.get(str(cmd or ""))
    if fn is None:
        return False, "未知语音命令: %s" % cmd, None
    try:
        return fn(None, dict(args or {}))
    except Exception as e:
        return False, "语音命令异常: %s: %s" % (type(e).__name__, e), None