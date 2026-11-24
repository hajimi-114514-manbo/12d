# -*- coding: utf-8 -*-
"""DSH 联动远程控制（A4 / task-27）。

纯 stdlib HTTP 服务：把外部（DSH / 脚本 / 浏览器）发来的命令，安全地派发到
视频工作台的**主线程**执行。**HTTP 线程绝不碰任何 Qt 对象**——这是本模块的
硬边界（task-18 tier 检测线程化教训：跨线程碰 Qt 会崩）。

线程模型（照 task-27 地图第 4 节结论）：
    HTTP 线程解析 JSON -> 把 (cmd, args, reply_queue) 放进模块级队列
    -> _schedule(wb, drain) 排回主线程（QObject.after，本机无 after 时用
    QTimer.singleShot(0,...)，均跨线程安全）-> 主线程 drain 逐条执行并把
    {ok,msg,data} 写进 reply_queue -> HTTP 线程 reply.get(timeout) 返回。
    dispatch 抛异常也回 {ok:false,msg}，不静默吞掉。

安全边界：只绑定 **127.0.0.1**、**无鉴权**——仅本机进程可用，勿用于暴露到
公网（无 token / 无鉴权，任何本机程序都能下发命令）。
"""
import json
import os
import queue
import re
import threading
import time
import tempfile

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import contextlib
import ui_toast as TOAST

DEFAULT_PORT = 8765
_MAX_BODY = 2 * 1024 * 1024      # 请求体 2MB 上限（防内存 DoS）
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPLY_TIMEOUT = 10.0      # HTTP 线程等待主线程回包的超时（秒），可被测试调小

_SERVER = None
_THREAD = None
_WB = None
_PORT = DEFAULT_PORT
_PENDING = queue.Queue()       # (cmd, args, reply_queue)
_VM = None                     # video_model 缓存（惰性 import，保持本模块 stdlib-only）


def _port():
    try:
        p = int(os.environ.get("MUPPET_REMOTE_PORT", "") or DEFAULT_PORT)
        return p if 1 <= p <= 65535 else DEFAULT_PORT
    except Exception:
        return DEFAULT_PORT


def _read_version():
    try:
        with open(os.path.join(_HERE, "version.json"), encoding="utf8") as f:
            return str(json.load(f).get("version", "?"))
    except Exception:
        return "?"


def _vm():
    """惰性拿 video_model（纯 Python，无 Qt/cv2）。"""
    global _VM
    if _VM is None:
        import video_model as _m
        _VM = _m
    return _VM


def _mt():
    """惰性拿 motion_table（纯数据，C2 动作/表情表）。"""
    try:
        import motion_table as _m
        return _m
    except Exception:
        return None


def _make_source(kind, path):
    """构造 source dict：优先用 video_source 探测真实尺寸/时长。"""
    V = _vm()
    with contextlib.suppress(Exception):
        import video_source as VS
        src = VS.probe_source(path)
        if isinstance(src, dict) and src.get("path"):
            src["kind"] = kind
            return src
    return V.new_source(kind, path)


def _refresh(wb):
    """命令改动后的轻量刷新（失败不抛，命令仍返回成功）。"""
    with contextlib.suppress(Exception): wb._after_edit()


def _track_by_name(wb, name):
    V = _vm()
    for tr in wb.proj.get("tracks", []):
        if tr.get("name") == name: return tr
    return None


# ---------------- 命令实现（全部在主线程执行） ----------------
def _cmd_project_new(wb, args):
    V = _vm()
    w = int(args.get("w") or 1920)
    h = int(args.get("h") or 1080)
    fps = float(args.get("fps") or 30)
    with contextlib.suppress(Exception): wb._sources.close_all()
    wb.proj = V.new_project(w, h, fps)
    wb.proj["dialogue"] = []
    with contextlib.suppress(Exception): wb._apply_project(None)
    with contextlib.suppress(Exception): wb.push_history()
    return True, "已新建工程 %dx%d @%gfps" % (w, h, fps), {"width": w, "height": h, "fps": fps}


def _cmd_scene_set_bg(wb, args):
    V = _vm()
    path = str(args.get("path") or "")
    if not path or not os.path.isfile(path): return False, "背景图不存在: %s" % path, None
    dur = float(args.get("dur") or 10.0)
    tr = _track_by_name(wb, "BG")
    if tr is None:
        ok, msg, tid = V.add_track(wb.proj, "video", "BG")
        if not ok: return False, msg, None
        tr = V.find_track(wb.proj, tid)
    src = _make_source("image", path)
    clip = V.new_clip(src, 0.0, dur)
    clip["layout"] = dict(V.default_layout())   # 满幅默认布局
    ok, cid = V.add_clip(wb.proj, tr["id"], clip)
    if not ok: return False, cid, None
    _refresh(wb)
    return True, "已设置背景 %s" % os.path.basename(path), {"id": cid}


def _fig_track_for(wb, path):
    """K1（task-69）：按角色（who）粒度复用/新建立绘轨。

    FIG-<who> 已存在 -> 复用（同角色换装/重入，杜绝双轨叠影）；否则新建。
    who 解析复用 webgal_import._who_of（单一口径）。复用轨时旧 clip 按
    驻场语义处理：新入场点截断旧 clip，被新区间整体覆盖的旧 clip 移除。"""
    V = _vm()
    try:
        import webgal_import as WI
        who = WI._who_of(path)
    except Exception:
        who = os.path.splitext(os.path.basename(
            str(path).replace("\\", "/").rstrip("/")))[0] or "fig"
    name = "FIG-%s" % who
    tr = _track_by_name(wb, name)
    if tr is None:
        n = 2
        while _track_by_name(wb, name) is not None:
            name = "FIG-%s-%d" % (who, n)
            n += 1
            if n > 99: break
        ok, msg, tid = V.add_track(wb.proj, "video", name)
        if not ok: return None
        tr = V.find_track(wb.proj, tid)
    return tr


def _retire_overlapping(wb, tr, start, dur):
    """同轨换装/重入：旧 clip 结束于新入场点，或被新区间覆盖则移除。"""
    V = _vm()
    ne = start + dur
    for oc in list(tr.get("clips") or []):
        os_ = float(oc.get("start") or 0.0)
        oe = os_ + float(oc.get("dur") or 0.0)
        if oe <= start or os_ >= ne: continue
        if os_ < start: oc["dur"] = round(start - os_, 3)
        else: V.remove_clip(wb.proj, str(oc.get("id") or ""))


# ---- 立绘自动排布（figure.add 不指定坐标时按同屏人数分位）----
# 保留 AI 显式坐标接口：args 里给了 x/y/scale 就完全按给定值，绝不动。
_AUTO_SLOT_X = {
    1: (0.5,),
    2: (0.28, 0.72),
    3: (0.2, 0.5, 0.8),
}


def _auto_slot_x(n, index):
    """同屏 n 人、当前第 index 个入场 -> x 坐标。

    1 人居中；2/3 人用预设（与剧本 left/center/right 观感一致）；
    >=4 人沿 [0.15, 0.85] 等分，避免叠在正中。index 从 0 起。
    """
    if n <= 0: return 0.5
    if n in _AUTO_SLOT_X: arr = _AUTO_SLOT_X[n]
    else: arr = tuple(0.15 + 0.70 * i / float(n - 1) for i in range(n))
    idx = max(0, min(n - 1, int(index)))
    return round(arr[idx], 3)


def _on_screen_count(wb, V, start, exclude_track_id=None):
    """start 时刻在场立绘 clip 数（各 FIG 轨在该时刻生效的 clip）。

    只数 puppet 源、非隐藏视频轨；排除正在加入的轨（同角色换装不算
    新增人数）。用于给 figure.add 决定第几个入场。
    """
    n = 0
    eps = 1e-6
    try:
        tracks = V._tracks_of(wb.proj)
    except Exception:
        return 0
    for tr in tracks:
        if not isinstance(tr, dict) or tr.get("hidden"): continue
        if tr.get("kind") != "video": continue
        if exclude_track_id and tr.get("id") == exclude_track_id: continue
        if not str(tr.get("name") or "").upper().startswith("FIG"): continue
        for c in (tr.get("clips") or []):
            s = float(c.get("start") or 0.0)
            e = s + float(c.get("dur") or 0.0)
            if s - eps <= start < e - eps:
                n += 1
                break
    return n


def _on_screen_fig_clips(wb, V, start):
    """start 时刻在场立绘 clip 列表 [(track_id, clip), ...]（FIG 轨、可见）。

    同屏判定：clip 区间覆盖 start。用于自动分位与重排。"""
    out = []
    eps = 1e-6
    try:
        tracks = V._tracks_of(wb.proj)
    except Exception:
        return out
    for tr in tracks:
        if not isinstance(tr, dict) or tr.get("hidden"): continue
        if tr.get("kind") != "video": continue
        if not str(tr.get("name") or "").upper().startswith("FIG"): continue
        for c in (tr.get("clips") or []):
            s = float(c.get("start") or 0.0)
            e = s + float(c.get("dur") or 0.0)
            if s - eps <= start < e - eps:
                out.append((tr.get("id"), c))
                break
    return out


def _auto_group_count(wb, V, start, exclude_track_id=None):
    """start 时刻同屏**自动分位组**人数（layout.auto_x=True 的在场 clip）。

    显式坐标 / move 过的角色不算——它们不占自动槽。"""
    n = 0
    for tid, c in _on_screen_fig_clips(wb, V, start):
        if exclude_track_id and tid == exclude_track_id: continue
        if bool((c.get("layout") or {}).get("auto_x")): n += 1
    return n


def _reflow_auto_figures(wb, V, start):
    """把 start 时刻同屏的**自动分位**角色重新均匀排布。

    只动 layout["auto_x"]=True 的 clip（figure.add 未指定 x 时打的标）；
    AI 显式给过坐标 / figure.move 手动调过的（auto_x=False）保持原位，
    且不占自动分位的槽——自动组在剩余空间里均分。
    逐个入场时每次都重排，最终一定均匀（2人 0.28/0.72、3人 0.2/0.5/0.8）。"""
    pairs = _on_screen_fig_clips(wb, V, start)
    autos = [(tid, c) for tid, c in pairs
             if bool((c.get("layout") or {}).get("auto_x"))]
    if not autos: return
    # 按入场顺序稳定排序（start 相同按 clip id），保证先来的在左
    autos.sort(key=lambda p: (float(p[1].get("start") or 0.0),
                              str(p[1].get("id") or "")))
    n = len(autos)
    for i, (_tid, c) in enumerate(autos):
        lay = dict(V.layout_at(c))
        lay["x"] = _auto_slot_x(n, i)
        lay["auto_x"] = True
        c["layout"] = lay


def _cmd_figure_add(wb, args):
    V = _vm()
    path = str(args.get("path") or "")
    if not path or not os.path.exists(path): return False, "素材路径不存在: %s" % path, None
    kind = args.get("kind")
    if not kind:
        # 目录含 rig_character.psd（木偶工程）或 model.json（Cubism2 整合包
        # 模型，H1 导出分支消费）-> puppet；否则 image
        kind = "puppet" if (os.path.isdir(path) and (
            os.path.isfile(os.path.join(path, "rig_character.psd"))
            or os.path.isfile(os.path.join(path, "model.json")))) \
            else "image"
    # 自动排布：AI 没指定坐标时按同屏人数分位；**显式给了就完全尊重**
    # （保留让 AI 安排具体位置的接口）。判断依据：args 里是否真的存在
    # 这个键（None 也算没给），而不是看值是不是默认值——这样 AI 想放在
    # 正中也能显式写 x=0.5 明确表达。
    has_x = args.get("x") is not None
    has_y = args.get("y") is not None
    has_scale = args.get("scale") is not None
    dur = float(args.get("dur") or 3600.0)
    try:
        start = float(args.get("start", 0.0))
    except (TypeError, ValueError):
        start = 0.0
    # K1：同屏人数上限撤销——不再拒绝任何入场；同角色复用轨。
    tr = _fig_track_for(wb, path)
    if tr is None: return False, "建立绘轨失败", None
    if not has_x or not has_y:
        # 新 clip 初值按「同屏自动组人数+1」；加入后统一重排到均匀位
        n = _auto_group_count(wb, V, start, exclude_track_id=tr["id"]) + 1
        idx = n - 1
        if not has_x:
            args = dict(args)
            args["x"] = _auto_slot_x(n, idx)
        if not has_y:
            args = dict(args)
            args["y"] = 0.7
    x = float(args.get("x"))
    y = float(args.get("y"))
    scale = float(args.get("scale") if has_scale else 0.6)
    _retire_overlapping(wb, tr, start, dur)
    src = _make_source(kind, path)
    clip = V.new_clip(src, start, dur)
    lay = clip["layout"]
    lay["x"], lay["y"], lay["scale"] = x, y, scale
    # 自动分位标记：AI 未指定 x -> True（参与后续重排）；显式给了 -> False
    lay["auto_x"] = not has_x
    # C2 + G5：motion/expression 接受表内 id 或 Cubism 键名形态（统一走
    # video_model._motion_value_ok 口径，与 sanitize 一致，不再只认表内 id）
    MT = _mt()
    for key in ("motion", "expression"):
        v = args.get(key)
        if not v: continue
        ids = None
        if MT is not None:
            ids = set(MT.motion_ids() if key == "motion"
                      else MT.expression_ids())
        if V._motion_value_ok(str(v), ids): clip[key] = str(v)
    ok, cid = V.add_clip(wb.proj, tr["id"], clip)
    if not ok: return False, cid, None
    # 加入后重排同屏自动组：逐个入场也能最终均匀（用户选定语义）
    if not has_x:
        _reflow_auto_figures(wb, V, start)
        x = float(V.layout_at(clip)["x"])
    _refresh(wb)
    auto = not has_x          # 是否由系统自动分位（AI 未指定 x）
    pos_note = "（自动分位 x=%.2f）" % x if auto else ""
    return True, "已添加立绘 %s(%s) -> %s%s" % (
        os.path.basename(path), kind, cid, pos_note), \
        {"id": cid, "kind": kind, "track": tr["name"],
         "x": round(x, 3), "y": round(y, 3), "scale": round(scale, 3),
         "auto_layout": auto}


def _cmd_figure_move(wb, args):
    V = _vm()
    cid = str(args.get("clip_id") or "")
    _tr, c = V.find_clip(wb.proj, cid)
    if c is None: return False, "找不到片段: %s" % cid, None
    # K1（task-69）：同屏上限撤销——start 位移只做时间移动不做人数检查；
    # x/y/scale/rot 直通任意值（无 snap，平移支持）。
    if args.get("start") is not None:
        try:
            new_start = float(args.get("start"))
        except (TypeError, ValueError):
            return False, "start 非法", None
        if new_start < 0: return False, "start 非法", None
        c["start"] = round(new_start, 3)
    lay = V.layout_at(c)
    for k in ("x", "y", "scale", "rot"):
        if args.get(k) is not None: lay[k] = float(args[k])
    # 手动调过位置 -> 退出自动重排组（AI 的安排优先，后续加人不再动它）
    if args.get("x") is not None: lay["auto_x"] = False
    c["layout"] = lay
    with contextlib.suppress(Exception): wb.push_history()
    with contextlib.suppress(Exception): wb.render_frame(wb._t)
    return True, "已移动 %s" % cid, dict(lay)


def _cmd_figure_remove(wb, args):
    V = _vm()
    cid = str(args.get("clip_id") or "")
    ok, msg = V.remove_clip(wb.proj, cid)
    if not ok: return False, msg or "删除失败", None
    _refresh(wb)
    return True, "已删除 %s" % cid, None


def _cmd_motion_play(wb, args):
    V = _vm()
    cid = str(args.get("clip_id") or "")
    _tr, c = V.find_clip(wb.proj, cid)
    if c is None: return False, "找不到片段: %s" % cid, None
    MT = _mt()
    name = args.get("name")
    if name:
        if MT is None: return False, "motion_table 不可用", None
        if str(name) not in MT.motion_ids():
            return True, "动作 %s 不在预置列表（仅提示）" % name, None
        c["motion"] = str(name)          # C2：set + seek 组合
        with contextlib.suppress(Exception): wb.push_history()
    wb.seek(c["start"])
    return True, "已设置并跳转 %s 起点" % cid, {"t": float(c["start"]),
                                               "motion": c.get("motion", "")}


def _cmd_motion_set(wb, args):
    """motion.set{clip_id,name}：校验 catalog，写 clip["motion"]。"""
    V = _vm()
    cid = str(args.get("clip_id") or "")
    _tr, c = V.find_clip(wb.proj, cid)
    if c is None: return False, "找不到片段: %s" % cid, None
    MT = _mt()
    if MT is None: return False, "motion_table 不可用", None
    name = str(args.get("name") or "")
    ids = MT.motion_ids()
    if name not in ids: return False, "动作 %s 不在预置列表" % name, {"options": ids}
    c["motion"] = name
    with contextlib.suppress(Exception):
        wb.push_history()
        wb.render_frame(wb._t)
    return True, "已设置动作 %s" % name, {"motion": name}


def _cmd_expression_set(wb, args):
    """expression.set{clip_id,name}：校验 catalog，写 clip["expression"]。"""
    V = _vm()
    cid = str(args.get("clip_id") or "")
    _tr, c = V.find_clip(wb.proj, cid)
    if c is None: return False, "找不到片段: %s" % cid, None
    MT = _mt()
    if MT is None: return False, "motion_table 不可用", None
    name = str(args.get("name") or "")
    ids = MT.expression_ids()
    if name not in ids: return False, "表情 %s 不在预置列表" % name, {"options": ids}
    c["expression"] = name
    with contextlib.suppress(Exception):
        wb.push_history()
        wb.render_frame(wb._t)
    return True, "已设置表情 %s" % name, {"expression": name}


def _cmd_catalog_get(wb, _args=None):
    """catalog.get{}：原样返回 motion_table.catalog()（DSH 直读）。"""
    MT = _mt()
    if MT is None: return False, "motion_table 不可用", None
    return True, "", MT.catalog()


def _cmd_say(wb, args):
    text = str(args.get("text") or "")
    who = str(args.get("who") or "旁白")
    try:
        t = float(args.get("t", getattr(wb, "_t", 0.0)))
    except Exception:
        t = float(getattr(wb, "_t", 0.0))
    try:
        dur = float(args.get("dur", 3.0))
    except Exception:
        dur = 3.0
    entry = {"t": round(t, 3), "dur": round(dur, 3), "who": who, "text": text}
    # H2a（task-47）：可选台词级动作/表情。校验走 video_model._sanitize_preset
    # （motion_table id 或 Cubism 键名形态；表不可用透传非空串）——脏值静默
    # 不写入，与 sanitize 往返结果一致，防 say 绕过校验落脏字段。
    V = _vm()
    MT = _mt()

    def _ids(fn):
        return set(getattr(MT, fn)()) if MT is not None else set()
    mv = V._sanitize_preset(args.get("motion"), lambda: _ids("motion_ids"))
    ex = V._sanitize_preset(args.get("expression"),
                            lambda: _ids("expression_ids"))
    if mv: entry["motion"] = mv
    if ex: entry["expression"] = ex
    wb.proj.setdefault("dialogue", []).append(entry)
    with contextlib.suppress(Exception): wb.push_history()
    with contextlib.suppress(Exception): wb.render_frame(wb._t)
    return True, "已添加台词" + ("（带动作）" if mv or ex else ""), \
        {"t": round(t, 3), "dur": round(dur, 3), "who": who,
         "motion": mv or "", "expression": ex or ""}


def _cmd_timeline_seek(wb, args):
    try:
        t = float(args.get("t", 0.0))
    except Exception:
        return False, "t 非法", None
    wb.seek(t)
    return True, "已跳转 %.3f" % t, {"t": t}


def _cmd_snapshot(wb, args):
    try:
        import video_export as VE
    except Exception as e:
        return False, "视频导出模块不可用: %s" % e, None
    try:
        arr = VE.compose_frame(wb.proj, wb._t, wb._sources.get)
    except Exception as e:
        return False, "合成失败: %s" % e, None
    try:
        from PIL import Image
        path = os.path.join(tempfile.gettempdir(),
                            "muppet_snap_%d.png" % int(time.time()))
        Image.fromarray(arr).save(path)
    except Exception as e:
        return False, "保存快照失败: %s" % e, None
    return True, "快照已保存", {"path": path}


def _cmd_webgal_export(wb, args):
    out_dir = str(args.get("out_dir") or "")
    if not out_dir: return False, "缺少 out_dir", None
    try:
        import webgal_export as WX
    except ImportError:
        return False, "WebGAL 导出器未就绪(task-26)", None
    exp = getattr(WX, "export_game", None)
    if exp is None: return False, "WebGAL 导出器未就绪(task-26)", None
    try:
        lb = args.get("live_badge", 0)
        try:
            lb = int(lb or 0)
        except (TypeError, ValueError):
            lb = 0
        res = exp(wb.proj, out_dir,
                  proj_dir=getattr(wb, "proj_dir", None),
                  game_name=(str(args.get("name")) if args.get("name") else None),
                  playable=bool(args.get("playable", False)),
                  style=bool(args.get("style", True)),
                  live_badge=lb)
    except AttributeError:
        return False, "WebGAL 导出器未就绪(task-26)", None
    except Exception as e:
        return False, "WebGAL 导出异常: %s" % e, None
    # 兼容二元组(旧)与三元组(ok,msg,stats)——task-26 正式版返回三元组
    try:
        if len(res) >= 3: ok, msg, stats = res[0], res[1], res[2]
        else: ok, msg, stats = res[0], res[1], None
    except (TypeError, ValueError, IndexError):
        return False, "WebGAL 导出器返回值异常: %r" % (res,), None
    data = {"out_dir": out_dir}
    if isinstance(stats, dict): data["stats"] = stats
    # F2-L5（v3.5）：导出前预检 —— 分支摘要 + 素材体检，**不阻断**，
    # 只是把「有什么问题」提前说清楚（用户之前是导出后才发现缺素材）。
    with contextlib.suppress(Exception):
        _pre = _branch_export_precheck(wb)
        if _pre:
            data["precheck"] = _pre
            if _pre.get("summary"): msg = ("%s\n%s" % (msg, _pre["summary"])).strip()
    return bool(ok), str(msg), data


def _branch_export_precheck(wb):
    """导出前预检：多幕分支摘要 + 素材体检缺失。

    返回 {} 表示没问题（单幕无分支且素材齐全）。**绝不抛**——
    预检是提示，不是闸门。
    """
    out = {"acts": 0, "choices": 0, "jumps": 0, "missing": [],
           "summary": ""}
    proj = getattr(wb, "proj", None)
    if not isinstance(proj, dict): return {}
    with contextlib.suppress(Exception):
        import video_model as V
        acts = [a for a in (proj.get("acts") or []) if isinstance(a, dict)]
        if acts:
            out["acts"] = len(acts)
            out["choices"] = sum(len(a.get("choices") or []) for a in acts)
            out["jumps"] = sum(len(a.get("jumps") or []) for a in acts)
            out["next"] = sum(1 for a in acts if a.get("next"))
    with contextlib.suppress(Exception):
        import asset_audit as AA
        rep = AA.audit_project(proj, getattr(wb, "proj_dir", None))
        out["missing"] = rep.get("missing") or []
        out["missing_total"] = len(out["missing"])
    bits = []
    if out["acts"] > 1:
        bits.append("%d 幕 / %d 个选择点 / %d 个条件跳转"
                    % (out["acts"], out["choices"], out["jumps"]))
    if out.get("missing_total"): bits.append("素材缺失 %d 个（导出时会跳过）" % out["missing_total"])
    out["summary"] = "；".join(bits)
    return out


def _cmd_state(wb, _args=None):
    tracks = len(wb.proj.get("tracks", []))
    clips = sum(len(t.get("clips", [])) for t in wb.proj.get("tracks", []))
    dialogue = len(wb.proj.get("dialogue") or [])
    return True, "", {
        "tracks": tracks, "clips": clips, "dialogue": dialogue,
        "t": float(getattr(wb, "_t", 0.0)),
        "playing": bool(getattr(wb, "_playing", False)),
        "port": _port(),
        "proj_w": int(wb.proj.get("width", 0)),
        "proj_h": int(wb.proj.get("height", 0)),
        "proj_fps": float(wb.proj.get("fps", 0.0)),
    }


def _warn_count(msg):
    """从 import_script 的 msg「…警告 K」里取 K；解析不到回 0。"""
    s = str(msg or "")
    i = s.find("警告")
    if i < 0: return 0
    digits = ""
    for ch in s[i + len("警告"):]:
        if ch.isdigit(): digits += ch
        else: break
    return int(digits) if digits else 0


def _parse_script_import(args):
    """script.import 的**解析部分**（纯 py，在 HTTP 线程跑，不碰 Qt）。

    返回 (ok, msg, proj)。game_dir 缺省推断：脚本父目录 basename=="game"
    取父目录，否则取父目录本身（让 webgal_import 内部再判）；两者都拿不到
    有效目录则 ok=False 提示手选。
    """
    sp = str(args.get("script_path") or "").strip()
    if not sp or not os.path.isfile(sp):
        return False, "脚本文件不存在: %s" % (sp or "(空)"), None
    gd = str(args.get("game_dir") or "").strip()
    if not gd or not os.path.isdir(gd):
        d = os.path.dirname(os.path.abspath(sp))
        gd = d if os.path.basename(d) == "game" else d
    if not gd or not os.path.isdir(gd):
        return False, "找不到有效的 game 资源目录，请在脚本参数里指定 game_dir", None
    try:
        import webgal_import as WI
    except ImportError:
        return False, "导入器未就绪（webgal_import 缺失）", None
    try:
        return WI.import_script(sp, gd)
    except Exception as e:
        return False, "导入异常 %s: %s" % (type(e).__name__, e), None


def _load_proj_on_main(wb, proj, msg):
    """script.import 的**载入部分**（主线程执行）：停播放 -> 换工程 -> 压历史。

    返回 (ok, msg, data)。data = {tracks, clips, dialogue, warnings}。
    """
    try:
        if getattr(wb, "_playing", False):
            with contextlib.suppress(Exception): wb.seek(0.0)
            with contextlib.suppress(Exception): wb.stop_play()
        wb._sources.close_all()
        wb.proj = proj
        wb._loading = True
        try:
            wb._apply_project(None)
        finally:
            wb._loading = False
        wb.push_history()
        tracks = len(proj.get("tracks", []))
        clips = sum(len(t.get("clips") or []) for t in proj.get("tracks", []))
        dialogue = len(proj.get("dialogue") or [])
        with contextlib.suppress(Exception):
            from qfluentwidgets import InfoBar, InfoBarPosition
            TOAST.toast_ok(wb, "脚本导入", msg, duration=4000)
        return True, msg, {"tracks": tracks, "clips": clips,
                           "dialogue": dialogue, "warnings": _warn_count(msg)}
    except Exception as e:
        return False, "载入工程失败: %s: %s" % (type(e).__name__, e), None


def _cmd_runtime_status(wb, _args=None):
    """runtime.status{} -> runtime_pack.status()（installed/version/path/files）。"""
    try:
        import runtime_pack as RP
    except ImportError:
        return False, "runtime_pack 未就绪", None
    try:
        return True, "", RP.status()
    except Exception as e:
        return False, "runtime 状态查询失败: %r" % e, None


def _runtime_import(args):
    """runtime.import 的**导入部分**（纯 py，在 HTTP 线程跑，不碰 Qt）。

    直接调 runtime_pack.import_zip；needs_confirm 时回 (False, ..., needs_confirm)。
    """
    try:
        import runtime_pack as RP
    except ImportError:
        return False, "runtime_pack 未就绪", None
    zip_path = str(args.get("zip_path") or "").strip()
    if not zip_path or not os.path.isfile(zip_path):
        return False, "zip 文件不存在: %s" % (zip_path or "(空)"), None
    try:
        return RP.import_zip(zip_path, confirm_overwrite=bool(args.get("confirm")))
    except Exception as e:
        return False, "runtime 导入异常: %s: %s" % (type(e).__name__, e), None


def _assets_query(args):
    """assets.query{kind?,tags?,limit?=30} 的查询部分（G4 / task-41）。

    **HTTP 线程直接执行**（同 script.import 解析段的做法）：webgal_import 是
    纯 py + 文件读，1418 条索引带 (mtime,size) 缓存过滤，不碰 Qt、不占主线程。
    返回 (ok, msg, data)，data={"total", "items":[...]}；索引缺失回
    ok=False + 指引（文案在 webgal_import.assets_query / load_asset_index）。"""
    try:
        import webgal_import as WI
    except ImportError:
        return False, "素材库消费端未就绪（webgal_import 缺失）", None
    kind = args.get("kind")
    kind = str(kind).strip() if kind not in (None, "") else None
    tags = args.get("tags")
    if isinstance(tags, str): tags = [tags]
    elif tags is not None and not isinstance(tags, (list, tuple)):
        return False, "tags 必须是字符串或字符串列表", None
    limit = args.get("limit", 30)
    if limit is None: limit = 30
    try:
        return WI.assets_query(kind=kind, tags=list(tags) if tags else None,
                               limit=limit)
    except (TypeError, ValueError) as e:
        return False, "查询参数非法: %s" % e, None
    except Exception as e:
        return False, "素材查询异常 %s: %s" % (type(e).__name__, e), None


# ================= F2-L4（v3.5）：多幕 / 分支 / 变量 命令组 =================
# 设计 docs/F2分支剧情设计.md §5。四组命名空间，全部主线程（走 _HANDLERS
# 统一派发），纯 dict 操作不碰 Qt 之外的东西。
#
# 与既有命令的关系：
#   - 单幕工程（v1 形状）**完全不受影响**：任何 act.* 首次调用会把工程
#     懒升级成 v2（video_model.add_act 内部完成），upgrade 字段在回执里
#     明确告知，避免 DSH 代理误以为工程结构突变。
#   - figure.* / say / motion.* 仍然只用 act_view 投影出的"当前幕"，
#     不需要感知幕的存在（L1 的 act_view 契约就是为此设计的）。
#
# 当前幕游标：存在 wb 上（_cur_aid），不进 proj——它是编辑器会话状态，
# 不该污染工程文件。缺失/无效时一律回落到第一幕。

def _cur_aid(wb):
    """当前幕 id（v1 单幕 -> 'a1'）。无效游标回第一幕。"""
    V = _vm()
    ids = V.act_ids(wb.proj)
    aid = getattr(wb, "_cur_aid", None)
    if aid and aid in ids: return aid
    return ids[0] if ids else "a1"


def _set_cur_aid(wb, aid):
    with contextlib.suppress(Exception): wb._cur_aid = aid


def _cmd_act_list(wb, _args=None):
    """act.list -> 幕摘要 + 当前幕。"""
    V = _vm()
    return True, "", {
        "acts": V.act_list(wb.proj),
        "cur": _cur_aid(wb),
        "multi": V.is_multi_act(wb.proj),
        "vars": V.sanitize_project(wb.proj).get("vars", []) if hasattr(
            V, "sanitize_project") else [],
    }


def _cmd_act_add(wb, args):
    """act.add{name?, after?} -> 新幕（首次调用把 v1 懒升级成 v2）。"""
    V = _vm()
    was_multi = V.is_multi_act(wb.proj)
    name = args.get("name")
    after = args.get("after")
    ok, msg, aid = V.add_act(wb.proj, str(name) if name else None,
                             str(after) if after else None)
    if not ok: return False, msg or "新增幕失败", None
    _set_cur_aid(wb, aid)
    _refresh(wb)
    data = {"aid": aid, "upgraded": (not was_multi)}
    return True, msg or ("已新增幕 %s" % aid), data


def _cmd_act_switch(wb, args):
    """act.switch{aid} -> 切当前幕（只切编辑器游标，不改工程内容）。"""
    V = _vm()
    aid = str(args.get("aid") or "")
    if not aid or aid not in V.act_ids(wb.proj):
        return False, "幕不存在: %s" % (aid or "(空)"), \
            {"available": V.act_ids(wb.proj)}
    _set_cur_aid(wb, aid)
    a = V.find_act(wb.proj, aid)
    _refresh(wb)
    data = {"aid": aid, "name": (a or {}).get("name"),
            "duration": V.act_duration(a) if a else 0.0}
    return True, "已切到幕 %s" % aid, data


def _cmd_act_rename(wb, args):
    """act.rename{aid?, name} -> 改显示名（aid 不变）。"""
    V = _vm()
    aid = str(args.get("aid") or _cur_aid(wb))
    name = str(args.get("name") or "").strip()
    if not name: return False, "name 不能为空", None
    ok, msg = V.rename_act(wb.proj, aid, name)
    return bool(ok), msg, ({"aid": aid, "name": name} if ok else None)


def _cmd_act_duplicate(wb, args):
    """act.duplicate{aid?} -> 复制一幕（含 tracks/dialogue/choices/jumps）。"""
    V = _vm()
    aid = str(args.get("aid") or _cur_aid(wb))
    ok, msg, new_aid = V.duplicate_act(wb.proj, aid)
    if not ok: return False, msg, None
    _set_cur_aid(wb, new_aid)
    _refresh(wb)
    return True, msg or ("已复制为 %s" % new_aid), {"aid": new_aid, "from": aid}


def _cmd_act_remove(wb, args):
    """act.remove{aid?} -> 删一幕，级联清引用（至少保留一幕）。"""
    V = _vm()
    aid = str(args.get("aid") or _cur_aid(wb))
    ok, msg, cleared = V.remove_act(wb.proj, aid)
    if not ok: return False, msg, None
    if getattr(wb, "_cur_aid", None) == aid: _set_cur_aid(wb, V.act_ids(wb.proj)[0])
    _refresh(wb)
    return True, msg or "已删除幕 %s" % aid, {"cleared_refs": cleared}


def _act_of(wb, args):
    """取目标幕：显式 aid > 当前幕。返回 (act_dict|None, aid)。"""
    V = _vm()
    aid = str(args.get("aid") or _cur_aid(wb))
    return V.find_act(wb.proj, aid), aid


def _cmd_choice_set(wb, args):
    """choice.set{aid?, cid?, t?, prompt?, options[]} -> 增/改一个选择点。

    options: [{text, target, set?, when?}]，上限 MAX_OPTIONS_PER_CHOICE，
             choices 总量上限 MAX_CHOICES_PER_ACT（video_model 常量）。
    cid 省略=新建；给了且存在=原位更新。
    """
    V = _vm()
    act, aid = _act_of(wb, args)
    if act is None: return False, "幕不存在: %s" % aid, {"available": V.act_ids(wb.proj)}
    opts = args.get("options")
    if opts is not None and not isinstance(opts, (list, tuple)):
        return False, "options 必须是列表", None
    cid = str(args.get("cid") or "").strip()
    if cid:
        target = None
        for ch in act.get("choices") or []:
            if ch.get("cid") == cid:
                target = ch
                break
        if target is None: return False, "选择点不存在: %s" % cid, None
    else:
        if len(act.get("choices") or []) >= V.MAX_CHOICES_PER_ACT:
            return False, "本幕选择点已达上限 %d" % V.MAX_CHOICES_PER_ACT, None
        cid = "c%d" % (len(act.get("choices") or []) + 1)
        target = {"cid": cid, "t": None, "prompt": "", "options": []}
        act.setdefault("choices", []).append(target)
    if args.get("t") is not None:
        try:
            target["t"] = max(0.0, float(args["t"]))
        except (TypeError, ValueError):
            return False, "t 非法", None
    if args.get("prompt") is not None: target["prompt"] = str(args["prompt"])
    if opts is not None:
        if len(opts) > V.MAX_OPTIONS_PER_CHOICE:
            return False, "选项数超上限 %d" % V.MAX_OPTIONS_PER_CHOICE, None
        clean = []
        for o in opts:
            if not isinstance(o, dict): return False, "options 元素必须是对象", None
            txt = str(o.get("text") or "").strip()
            if not txt: return False, "选项 text 不能为空", None
            e = {"text": txt, "target": str(o.get("target") or "")}
            if o.get("set"):
                # option.set 契约 = 字符串列表（video_model._sanitize_str_list）。
                # 接受 "k=v;k2=v" 字符串或 ["k=v","k2=v"] 列表两种入参。
                sv = o["set"]
                if isinstance(sv, (list, tuple)):
                    e["set"] = [str(x).strip() for x in sv
                                if str(x or "").strip()]
                else:
                    e["set"] = [x.strip() for x in str(sv).split(";")
                                if x.strip()]
            if o.get("when"): e["when"] = str(o["when"])
            clean.append(e)
        target["options"] = clean
    wb.push_history() if hasattr(wb, "push_history") else None
    _refresh(wb)
    return True, "已写入选择点 %s（%d 选项）" % (cid, len(target["options"])), \
        {"aid": aid, "cid": cid, "choice": dict(target)}


def _cmd_choice_remove(wb, args):
    """choice.remove{aid?, cid} -> 删选择点。"""
    V = _vm()
    act, aid = _act_of(wb, args)
    if act is None: return False, "幕不存在: %s" % aid, None
    cid = str(args.get("cid") or "")
    lst = act.get("choices") or []
    for i, ch in enumerate(lst):
        if ch.get("cid") == cid:
            lst.pop(i)
            _refresh(wb)
            return True, "已删除选择点 %s" % cid, {"aid": aid, "cid": cid}
    return False, "选择点不存在: %s" % cid, None


def _cmd_jump_set(wb, args):
    """jump.set{aid?, t, when?, target} -> 设条件跳转（if:cond:target;）。"""
    V = _vm()
    act, aid = _act_of(wb, args)
    if act is None: return False, "幕不存在: %s" % aid, None
    try:
        t = max(0.0, float(args.get("t", 0.0)))
    except (TypeError, ValueError):
        return False, "t 非法", None
    target_scene = str(args.get("target") or "").strip()
    if not target_scene: return False, "target 不能为空", None
    # target 允许是本工程幕 id（导出时映射成 scene 名），也允许是外部 scene 名
    V.act_ids(wb.proj)
    j = {"t": t, "when": str(args.get("when") or ""), "target": target_scene}
    act.setdefault("jumps", []).append(j)
    wb.push_history() if hasattr(wb, "push_history") else None
    _refresh(wb)
    return True, "已设跳转 @%.2fs -> %s" % (t, target_scene), \
        {"aid": aid, "jump": j}


def _cmd_jump_remove(wb, args):
    """jump.remove{aid?, index} -> 按下标删跳转。"""
    V = _vm()
    act, aid = _act_of(wb, args)
    if act is None: return False, "幕不存在: %s" % aid, None
    try:
        idx = int(args.get("index", -1))
    except (TypeError, ValueError):
        return False, "index 非法", None
    lst = act.get("jumps") or []
    if not (0 <= idx < len(lst)): return False, "index 越界（共 %d 条）" % len(lst), None
    lst.pop(idx)
    _refresh(wb)
    return True, "已删除第 %d 条跳转" % idx, {"aid": aid}


def _cmd_act_next(wb, args):
    """act.next{aid?, target|null} -> 设/清单幕结尾的续幕（导出 changeScene）。"""
    V = _vm()
    act, aid = _act_of(wb, args)
    if act is None: return False, "幕不存在: %s" % aid, None
    tgt = args.get("target")
    if tgt in (None, ""):
        act["next"] = None
        _refresh(wb)
        return True, "已清除幕 %s 的续接" % aid, {"aid": aid, "next": None}
    tgt = str(tgt)
    ids = V.act_ids(wb.proj)
    if tgt not in ids: return False, "目标幕不存在: %s" % tgt, {"available": ids}
    if tgt == aid: return False, "不能接回自己", None
    act["next"] = tgt
    _refresh(wb)
    return True, "幕 %s 结尾续接到 %s" % (aid, tgt), {"aid": aid, "next": tgt}


def _cmd_var_set(wb, args):
    """var.set{name, default?} -> 声明/更新一个分支变量（工程级）。"""
    V = _vm()
    name = str(args.get("name") or "").strip()
    if not name: return False, "name 不能为空", None
    # 变量名要做标识符安全化（导出会写进 setVar:name=...;）
    if not re.match(r"^[A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*$", name):
        return False, "name 只能由字母/数字/下划线/汉字组成且不以数字开头", None
    if not V.is_multi_act(wb.proj):
        # 变量是 v2 特性：v1 工程先懒升级，保证 sanitize 能保留 vars
        V.add_act(wb.proj, "幕一")
    vars_ = wb.proj.get("vars")
    if not isinstance(vars_, list):
        vars_ = []
        wb.proj["vars"] = vars_
    default = args.get("default", 0)
    for v in vars_:
        if v.get("name") == name:
            v["default"] = default
            _refresh(wb)
            return True, "已更新变量 %s" % name, {"name": name, "default": default}
    vars_.append({"name": name, "default": default})
    wb.push_history() if hasattr(wb, "push_history") else None
    _refresh(wb)
    return True, "已声明变量 %s = %r" % (name, default), \
        {"name": name, "default": default, "total": len(vars_)}


def _cmd_var_list(wb, _args=None):
    """var.list -> 全部变量声明。"""
    vars_ = wb.proj.get("vars")
    return True, "", {"vars": list(vars_) if isinstance(vars_, list) else []}


def _cmd_act_script(wb, args):
    """act.script -> 生成当前工程的 WebGAL 分支脚本预览（不写盘）。

    只跑导出器的脚本生成部分（branch=False 的轻量路径），给 DSH 代理
    在对话里直接看结果。aid 省略=全部幕。
    """
    V = _vm()
    try:
        import webgal_export as WE
    except ImportError:
        return False, "导出器未就绪（webgal_export 缺失）", None
    fn = getattr(WE, "render_branch_script", None)
    if fn is None: return False, "导出器尚未实现多幕脚本预览（F2-L2 未落地）", None
    try:
        res = fn(wb.proj, getattr(wb, "_proj_dir", None))
    except Exception as e:
        return False, "脚本预览异常: %s: %s" % (type(e).__name__, e), None
    if isinstance(res, tuple) and len(res) >= 2:
        ok, data = res[0], res[1]
        return bool(ok), "" if ok else str(data), \
            (data if isinstance(data, dict) else {"script": data})
    return True, "", {"script": res}


def _cmd_assets_check(wb, args):
    """K2（task-73）素材体检（纯读，无 Qt）。args: {kind?, limit?}

    ok 恒 True = 命令执行成功；缺失与否看 data.total / msg（与
    assets.query 读命令惯例一致）。"""
    import asset_audit as A
    if wb is None or getattr(wb, "proj", None) is None: return False, "无工程，无法体检", None
    pdir = getattr(wb, "proj_dir", None) or getattr(wb, "_proj_dir", None)
    rep = A.audit_project(wb.proj, proj_dir=pdir)
    kind = str(args.get("kind") or "").strip().lower()
    full = rep["missing"]
    if kind: full = [m for m in full if m["kind"] == kind]
    limit = args.get("limit")
    try:
        limit = int(limit) if limit else 0
    except (TypeError, ValueError):
        limit = 0
    shown = full[:limit] if limit > 0 else full
    summary = A.format_report(dict(rep, missing=full))
    data = {"missing": shown, "total": len(full),
            "placeholder": rep["placeholder"], "real": rep["real"],
            "counts": rep["counts"], "lib": rep["lib"], "summary": summary}
    if not full: return True, "素材体检：无缺失", data
    return True, "素材体检缺 %d 项（占位符 %d / 真缺失 %d）——%s" % (
        len(full), rep["placeholder"], rep["real"], summary or "详见 data"), data


# ================= v3.6.2：运镜 / 光效 / 投影 / 批量操作 的联动入口 =================
# 这四个能力原本只在 GUI 里能点。DSH 要真正「用」它们，必须有命令通道：
# AI 不能（也不该）去模拟鼠标点按钮。所以这里把它们的**核心产出**
# 暴露成命令——运镜写 effects、光效写 effects、投影建贴图轨、批处理改工程。


def _cmd_camera_apply(wb, args):
    """应用一段多段运镜（写 proj["effects"] 的 setTempAnimation）。

    args:
      preset: 模板名（缓慢推进/缓慢拉远/向左取景/…/恢复全部运镜）
      keyframes: 直接给关键帧数组（优先于 preset），形如
                 [{"duration":0,"scale":{"x":1,"y":1}},
                  {"duration":800,"scale":{"x":1.15,"y":1.15}}]
      target: "bg-main"（背景/整个舞台）| "fig-center"（立绘）| "both"
      t: 生效时刻（秒），默认当前播放头
    返回 data 里带上生成的指令，便于 AI 校验。
    """
    import camera_path as CP
    import video_model as V  # noqa: F401
    target = str(args.get("target") or "both")
    t = args.get("t")
    try:
        t = float(t) if t is not None else float(getattr(wb, "_t", 0.0) or 0.0)
    except (TypeError, ValueError):
        t = float(getattr(wb, "_t", 0.0) or 0.0)

    kfs = args.get("keyframes")
    preset = str(args.get("preset") or "")
    paths = []
    # target 归一：both=两层都写；bg-main/fig-center 只写那一层
    if target in ("both", "", None): want_layers = ("stage", "fig")
    elif str(target).startswith("bg"): want_layers = ("stage",)
    else: want_layers = ("fig",)
    for layer, tgt in (("stage", CP.TARGET_STAGE), ("fig", CP.TARGET_FIG)):
        if layer not in want_layers: continue
        if isinstance(kfs, list) and kfs: use = CP.normalize_keys(kfs)
        elif preset: use = CP.template_keys(preset, layer)
        else: return False, "需要 preset 或 keyframes 之一", None
        if not use:
            return False, "找不到模板: %s（可用：%s）" % (
                preset, ", ".join(CP.TEMPLATE_NAMES)), None
        if CP.total_ms(use) <= 0 and preset != "恢复全部运镜": continue
        paths.append({"level": layer, "target": tgt,
                      "cmd": CP.build_cmd(use, target=tgt),
                      "keyframes": use, "total_ms": CP.total_ms(use)})
    if not paths: return False, "这段运镜没有时长（全是瞬切），没有可应用的内容", None

    from PySide6.QtWidgets import QApplication
    wb._t = t
    effs = wb.proj.get("effects")
    if not isinstance(effs, list): effs = wb.proj["effects"] = []
    for p in paths:
        effs.append({"t": round(t, 3), "cmd": p["cmd"]})
    with contextlib.suppress(Exception): effs.sort(key=lambda x: float(x.get("t") or 0.0))
    wb.push_history()
    wb._after_edit()
    QApplication.processEvents()
    return True, "已应用运镜：%s（%.2fs，%d 条路径）" % (
        preset or "自定义关键帧", t, len(paths)), {
            "t": t, "paths": [{"level": p["level"], "target": p["target"],
                               "cmd": p["cmd"], "total_ms": p["total_ms"]}
                              for p in paths]}


def _cmd_light_apply(wb, args):
    """应用一条氛围光效预设（成对写 bg + fig 的 setTransform）。

    args:
      preset: 预设名（黄昏长影/烛火暖光/月夜银蓝/舞台双束聚光/…）
      t: 生效时刻，默认播放头
      layer: "both"（默认）| "bg" | "fig"
    """
    import light_effects as LE
    name = str(args.get("preset") or args.get("name") or "")
    p = LE.by_name(name)
    if not p:
        return False, "未知光效: %s（可用：%s）" % (
            name, ", ".join(LE.LIGHT_NAMES)), None
    try:
        _t = args.get("t")
        t = float(_t) if _t is not None else float(getattr(wb, "_t", 0.0) or 0.0)
    except (TypeError, ValueError):
        t = float(getattr(wb, "_t", 0.0) or 0.0)
    layer = str(args.get("layer") or "both")
    effs = wb.proj.get("effects")
    if not isinstance(effs, list): effs = wb.proj["effects"] = []
    cmds = []
    for lay, tgt in (("bg", "bg-main"), ("fig", "fig-center")):
        if layer != "both" and layer != lay: continue
        cmd = LE.to_transform_cmd(name, target=tgt)
        if cmd:
            cmds.append({"layer": lay, "target": tgt, "cmd": cmd})
            effs.append({"t": round(t, 3), "cmd": cmd})
    if not cmds: return False, "该预设没有可作用的层（target 不匹配）", None
    with contextlib.suppress(Exception): effs.sort(key=lambda x: float(x.get("t") or 0.0))
    wb._cur_light_name = name      # 供加投影时决定影子浓度
    wb.push_history()
    wb._after_edit()
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    return True, "已应用光效「%s」（%.2fs，%d 条）" % (name, t, len(cmds)), {
        "t": t, "preset": name, "cmds": [c["cmd"] for c in cmds],
        "limits": LE.LIMITS["unsupported"],
    }


def _cmd_light_list(wb, args):
    """列出可用的光效预设 + 能力边界（AI 选预设前应先查这个）。"""
    import light_effects as LE
    data = {
        "presets": [{"name": p["name"], "desc": p["desc"],
                     "target": p["target"], "note": p["note"]}
                    for p in LE.LIGHT_PRESETS],
        "supported": LE.LIMITS["supported"],
        "unsupported": LE.LIMITS["unsupported"],
        "reason": LE.LIMITS["reason"],
    }
    return True, "共 %d 条光效预设" % len(LE.LIGHT_PRESETS), data


def _cmd_shadow_add(wb, args):
    """给角色加地面投影（贴图实现，官方引擎真能渲染）。

    args:
      who: 角色名（对应 FIG-<角色> 轨）；不传则取第一条立绘
      concentration/softness/width_pct: 可选覆盖默认参数
    """
    import character_shadow as CS
    import video_model as V
    who = str(args.get("who") or "").strip()
    aid = getattr(wb, "_cur_aid", None)
    figs = []
    for tr in V._tracks_of(wb.proj, aid):
        if not isinstance(tr, dict) or tr.get("kind") != "video": continue
        nm = str(tr.get("name") or "")
        if not nm.upper().startswith("FIG"): continue
        ch = nm[4:].lstrip("-_")
        if who and ch != who: continue
        for c in (tr.get("clips") or []):
            figs.append((ch, tr, c))
    if not figs: return False, "没有找到立绘 clip（先 figure.add）", None
    ch, tr, c = figs[0]
    lay = V.layout_at(c)
    cur_light = getattr(wb, "_cur_light_name", None)
    conc = args.get("concentration")
    if conc is None: conc = CS.preset_concentration(cur_light) if cur_light else 0.38
    try:
        conc = float(conc)
    except (TypeError, ValueError):
        conc = 0.38
    soft = args.get("softness")
    try:
        soft = float(soft) if soft is not None else 0.55
    except (TypeError, ValueError):
        soft = 0.55
    s_layout = CS.place_shadow(lay, wb.proj.get("width", 1920),
                               wb.proj.get("height", 1080),
                               concentration=conc, softness=soft)
    import os as _os
    base = getattr(wb, "proj_dir", None) or _os.path.expanduser("~")
    png = CS.make_shadow_png(_os.path.join(base, "_shadow_%s.png"
                                          % (ch or "default")),
                             concentration=conc, softness=soft)
    sname = CS.shadow_track_name(ch)
    strack = None
    for t2 in V._tracks_of(wb.proj, aid):
        if isinstance(t2, dict) and str(t2.get("name")) == sname:
            strack = t2
            break
    created = False
    if strack is None:
        try:
            _ok, _msg, _tid = V.add_track(wb.proj, "video", sname, aid=aid)
            if _ok:
                for t2 in V._tracks_of(wb.proj, aid):
                    if isinstance(t2, dict) and str(t2.get("id")) == str(_tid):
                        strack = t2
                        break
                created = True
        except Exception:
            strack = None
    if strack is None: return False, "创建投影轨失败", None
    try:
        sc = V.new_clip(V.new_source("image", png),
                        float(c.get("start") or 0.0),
                        float(c.get("dur") or 5.0))
        sc["layout"] = dict(s_layout)
        strack.setdefault("clips", []).append(sc)
        trucks = wb.proj.get("tracks") or []
        if strack in trucks and tr in trucks:
            trucks.remove(strack)
            trucks.insert(trucks.index(tr), strack)
    except Exception as e:
        return False, "添加投影失败: %s" % e, None
    wb.push_history()
    with contextlib.suppress(Exception): wb.timeline.refresh()
    wb._after_edit()
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    return True, "已给「%s」加投影（浓度 %.0f%%）" % (ch, conc * 100), {
        "who": ch, "track": sname, "png": png,
        "concentration": conc, "softness": soft,
        "layout": s_layout, "track_created": created,
    }


def _cmd_shadow_remove(wb, args):
    """移除投影轨。args: who（只删这个角色）或不传（删全部 SHADOW-*）。"""
    who = str(args.get("who") or "").strip()
    want = ("SHADOW-%s" % who).upper() if who else "SHADOW-"
    tracks = wb.proj.get("tracks") or []
    keep, removed = [], []
    for t in tracks:
        nm = str(t.get("name") or "").upper()
        if nm.startswith(want): removed.append(t.get("name"))
        else: keep.append(t)
    if not removed: return True, "没有匹配的投影轨", {"removed": []}
    wb.proj["tracks"] = keep
    wb.push_history()
    with contextlib.suppress(Exception): wb.timeline.refresh()
    wb._after_edit()
    return True, "已移除 %d 条投影轨" % len(removed), {"removed": removed}


def _cmd_batch_run(wb, args):
    """执行一个批量脚本操作。

    args:
      action: fix_ids | add_next | auto_exit | retime
              | apply_filter | clear_filter
      scope:  apply_filter/clear_filter 用：all | fig | bg
      filter: apply_filter 用，参数字典；不传则用「当前滤镜」
      max_gap: add_next 用（默认 2.5 秒）
    """
    import batch_ops as BO
    action = str(args.get("action") or "")
    aid = getattr(wb, "_cur_aid", None)
    try:
        if action in ("apply_filter", "clear_filter"):
            scope = str(args.get("scope") or "all")
            which = scope if scope in ("all", "fig", "bg") else "all"
            filt = None
            if action == "apply_filter":
                filt = args.get("filter")
                if not isinstance(filt, dict) or not filt:
                    filt = getattr(wb, "_cur_filter_preset", None)
                if not filt:
                    return False, ("没有可用的滤镜参数：传 filter={...}，"
                                   "或先在滤镜页选一个"), None
            newp, st = BO.apply_filter_batch(wb.proj, filt, which=which,
                                             aid=aid)
            msg = ("%s %d 个片段" % ("套用滤镜到" if filt else "清除滤镜",
                                     st["updated"]))
        elif action == "fix_ids":
            newp, st = BO.fix_ids(wb.proj, aid=aid)
            msg = "补全 %d 个 clip id、%d 个轨道 id" % (
                st["clips_fixed"], st["tracks_fixed"])
        elif action == "add_next":
            kw = {}
            if args.get("max_gap") is not None:
                with contextlib.suppress((TypeError, ValueError)):
                    kw["max_gap"] = float(args["max_gap"])
            newp, st = BO.add_next_to_dialogue(wb.proj, aid=aid, **kw)
            msg = "为 %d 句连续台词加上 -next" % st["updated"]
        elif action == "auto_exit":
            newp, st = BO.auto_exit_figures(wb.proj, aid=aid)
            msg = "收尾 %d 条立绘轨（%d 个 clip）" % (
                st["tracks_touched"], st["clips_trimmed"])
        elif action == "retime":
            kw = {}
            for k in ("chars_per_sec", "min_dur", "keep_longer"):
                if args.get(k) is not None: kw[k] = args[k]
            newp, st = BO.retime_dialogue(wb.proj, aid=aid, **kw)
            msg = "按字数调整 %d 句台词时长" % st["updated"]
        else:
            return False, ("未知 action: %s（可用：fix_ids / add_next / "
                           "auto_exit / retime / apply_filter / clear_filter）"
                           % action), None
    except Exception as e:
        return False, "批处理异常: %s: %s" % (type(e).__name__, e), None

    changed = any((st or {}).values())
    if changed:
        wb.proj = newp
        wb.push_history()
        with contextlib.suppress(Exception): wb.timeline.refresh()
        wb._after_edit()
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
    return True, msg + ("" if changed else "（无改动）"), {
        "action": action, "stats": st, "changed": changed,
    }


def _cmd_track_add(wb, args):
    """新建轨道。args: kind=video|audio, name, act(aid 可选)。"""
    V = _vm()
    kind = str(args.get("kind") or "video")
    if kind not in ("video", "audio"): return False, "kind 只能是 video/audio", None
    name = str(args.get("name") or ("%s轨" % kind))
    aid = args.get("act") or getattr(wb, "_cur_aid", None)
    ok, msg, tid = V.add_track(wb.proj, kind, name, aid=aid)
    if not ok: return False, msg, None
    wb.push_history()
    wb._after_edit()
    return True, "已建轨 %s（%s）" % (name, tid), {"track_id": tid, "name": name}


def _cmd_track_list(wb, args):
    """列出当前幕的轨道与 clip（AI 编排前先查）。"""
    V = _vm()
    aid = getattr(wb, "_cur_aid", None)
    out = []
    for tr in V._tracks_of(wb.proj, aid):
        if not isinstance(tr, dict): continue
        clips = []
        for c in (tr.get("clips") or []):
            if not isinstance(c, dict): continue
            src = c.get("source") or {}
            clips.append({
                "id": c.get("id"), "start": c.get("start"),
                "dur": c.get("dur"), "src_in": c.get("src_in"),
                "kind": src.get("kind"),
                "path": src.get("path"),
                "motion": c.get("motion"),
                "expression": c.get("expression"),
                "filter": c.get("filter"),
                "layout": c.get("layout"),
            })
        out.append({"id": tr.get("id"), "name": tr.get("name"),
                    "kind": tr.get("kind"), "hidden": tr.get("hidden"),
                    "clips": clips})
    return True, "共 %d 条轨道" % len(out), {"tracks": out,
                                        "duration": V.project_duration(wb.proj)}


def _cmd_clip_add(wb, args):
    """往轨道加 clip。

    args: track(name 或 id)、path、start、dur、src_in、kind(image/video/audio/puppet)
    不传 track 时按素材类型自动选（image/video -> 视频轨，audio -> 音频轨）。
    """
    V = _vm()
    import os as _os
    path = str(args.get("path") or "")
    if not path or not _os.path.exists(path): return False, "素材不存在: %s" % path, None
    kind = str(args.get("kind") or "")
    if not kind:
        ext = _os.path.splitext(path)[1].lower()
        if ext in (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"): kind = "audio"
        elif ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"): kind = "video"
        elif _os.path.isdir(path): kind = "puppet"
        else: kind = "image"
    aid = getattr(wb, "_cur_aid", None)
    want = str(args.get("track") or "")
    tr = None
    for t in V._tracks_of(wb.proj, aid):
        if not isinstance(t, dict): continue
        if want and (str(t.get("id")) == want or str(t.get("name")) == want):
            tr = t
            break
    if tr is None and want: return False, "找不到轨道: %s" % want, None
    if tr is None:
        # 自动选：音频找 audio 轨，其余找第一条 video 轨，都没有就建
        need_kind = "audio" if kind == "audio" else "video"
        for t in V._tracks_of(wb.proj, aid):
            if isinstance(t, dict) and t.get("kind") == need_kind:
                tr = t
                break
        if tr is None:
            ok, msg, tid = V.add_track(wb.proj, need_kind,
                                       "%s轨" % ("音频" if need_kind == "audio"
                                                else "视频"), aid=aid)
            if not ok: return False, "建轨失败: %s" % msg, None
            for t in V._tracks_of(wb.proj, aid):
                if isinstance(t, dict) and str(t.get("id")) == str(tid):
                    tr = t
                    break
    if tr is None: return False, "无法确定目标轨道", None
    try:
        src = V.new_source(kind, path)
        start = float(args.get("start") or 0.0)
        dur = args.get("dur")
        if dur is None: dur = float(src.get("src_dur") or 0.0) or 5.0
        dur = float(dur)
        c = V.new_clip(src, start, dur, src_in=float(args.get("src_in") or 0.0))
        # 布局：两种给法并存 —— ①整个 dict ②散着的 x/y/scale/rot
        # ①优先（dict 里的值直接覆盖），②作为 dict 缺省项补充。
        # **scale 在两条路上都生效**（原来只有②生效，两条路语义不一致，
        # 调用方会以为「传了就管用」）。
        lay = args.get("layout")
        c["layout"] = dict(c.get("layout") or {})
        if isinstance(lay, dict):
            c["layout"].update(lay)
        for k in ("x", "y", "scale", "rot"):
            v = args.get(k)
            if v is not None:
                c["layout"][k] = float(v)
        tr.setdefault("clips", []).append(c)
    except Exception as e:
        return False, "添加 clip 失败: %s: %s" % (type(e).__name__, e), None
    wb.push_history()
    wb._after_edit()
    return True, "已加 clip（%s %.2f~%.2fs）" % (kind, start, start + dur), {
        "clip_id": c.get("id"), "track": tr.get("name"),
        "start": start, "dur": dur,
    }


def _find_clip_checked(wb, cid, track_hint=None):
    """按 id 找 clip；找不到或有歧义时给出可行动的提示。

    **为什么要 track_hint**：clip id 只在「应该」全工程唯一，但导入的旧
    工程/手写的工程常有重复（batch fix_ids 就是修这个的）。重复时
    find_clip 只返回第一个，改到错误的轨上会静默失效——测试 B7/B8
    就是这么暴露的：move/trim 都报成功，但目标 clip 没变。
    传 track（名字或 id）可消歧。
    """
    V = _vm()
    aid = getattr(wb, "_cur_aid", None)
    hits = []
    for tr in V._tracks_of(wb.proj, aid):
        if not isinstance(tr, dict): continue
        for c in (tr.get("clips") or []):
            if isinstance(c, dict) and str(c.get("id")) == str(cid):
                hits.append((tr, c))
    if not hits: return None, None, "找不到 clip: %s" % cid
    if track_hint:
        want = str(track_hint)
        hits = [(t, c) for t, c in hits
                if str(t.get("id")) == want or str(t.get("name")) == want]
        if not hits: return None, None, "在轨道 %s 上找不到 clip %s" % (track_hint, cid)
    if len(hits) > 1:
        names = [t.get("name") for t, _ in hits]
        return None, None, ("clip id %s 在多个轨道重复（%s）——"
                            "请用 track 参数指定，或先跑 batch.run "
                            "action=fix_ids" % (cid, "、".join(str(n) for n in names)))
    return hits[0][0], hits[0][1], ""


def _cmd_clip_move(wb, args):
    """移动 clip。args: id, start（可选 delta 秒）, track（消歧）。"""
    cid = str(args.get("id") or "")
    if not cid: return False, "缺少 id", None
    tr, c, err = _find_clip_checked(wb, cid, args.get("track"))
    if c is None: return False, err, None
    if args.get("start") is not None: c["start"] = max(0.0, float(args["start"]))
    elif args.get("delta") is not None:
        c["start"] = max(0.0, float(c.get("start") or 0) + float(args["delta"]))
    else: return False, "需要 start 或 delta", None
    wb.push_history()
    wb._after_edit()
    return True, "已移动 clip 到 %.2fs（轨 %s）" % (c["start"], tr.get("name")), {
        "start": c["start"], "track": tr.get("name")}


def _cmd_clip_trim(wb, args):
    """裁剪 clip。args: id, dur / src_in, track（消歧）。"""
    cid = str(args.get("id") or "")
    tr, c, err = _find_clip_checked(wb, cid, args.get("track"))
    if c is None: return False, err, None
    if args.get("dur") is not None:
        d = float(args["dur"])
        if d <= 0: return False, "dur 必须 > 0", None
        c["dur"] = d
    if args.get("src_in") is not None: c["src_in"] = max(0.0, float(args["src_in"]))
    wb.push_history()
    wb._after_edit()
    return True, "已调整 clip（dur=%.2f src_in=%.2f，轨 %s）" % (
        c.get("dur") or 0, c.get("src_in") or 0, tr.get("name")), {
            "dur": c.get("dur"), "src_in": c.get("src_in"),
            "track": tr.get("name")}


def _cmd_clip_remove(wb, args):
    """删除 clip。args: id, track（消歧）。"""
    cid = str(args.get("id") or "")
    tr, c, err = _find_clip_checked(wb, cid, args.get("track"))
    if c is None: return False, err, None
    try:
        tr["clips"].remove(c)
    except ValueError:
        return False, "删除失败（不在该轨）", None
    wb.push_history()
    wb._after_edit()
    return True, "已删除 clip %s（轨 %s）" % (cid, tr.get("name")), {"id": cid}


def _cmd_clip_split(wb, args):
    """在 t 处切分 clip。args: id, t, track（消歧）。"""
    cid = str(args.get("id") or "")
    tr, c, err = _find_clip_checked(wb, cid, args.get("track"))
    if c is None: return False, err, None
    t = float(args.get("t") or 0.0)
    s0 = float(c.get("start") or 0)
    d0 = float(c.get("dur") or 0)
    tr_name = str(tr.get("name") or "")
    if not (s0 + 0.05 < t < s0 + d0 - 0.05):
        return False, "切点不在 clip 内部（%.2f 不在 %.2f~%.2f）" % (
            t, s0, s0 + d0), None
    V = _vm()
    # **split_clip 内部自己调 find_clip(proj, id)**，会绕过本层的 track
    # 消歧——重复 id 时切的是别的轨上那个。所以：
    #   ① 先跑 fix_ids 把 id 修唯一（返回新 proj，引用全变）
    #   ② 用 (轨名, start) 在新 proj 里重新定位目标 clip
    # 否则会出现「报成功但目标 clip 没变」（测试 B9 踩到）。
    with contextlib.suppress(Exception):
        import batch_ops as _BO
        _newp, _st = _BO.fix_ids(wb.proj)
        if any((_st or {}).values()):
            wb.proj = _newp
            tr = c = None
            for _t in V._tracks_of(wb.proj, getattr(wb, "_cur_aid", None)):
                if not isinstance(_t, dict) or \
                        str(_t.get("name")) != str(tr_name):
                    continue
                for _c in (_t.get("clips") or []):
                    if isinstance(_c, dict) and \
                            abs(float(_c.get("start") or 0) - s0) < 1e-6:
                        tr, c = _t, _c
                        break
                if c is not None: break
    if c is None: return False, "切分前重新定位失败（工程结构已变）", None
    cid2 = str(c.get("id"))
    try:
        # split_clip 返回 (ok, msg, 右段id) —— 3 元组，不是 2 元组
        r = V.split_clip(wb.proj, cid2, t)
        if isinstance(r, tuple) and len(r) >= 3: ok, msg = r[0], r[1]
        elif isinstance(r, tuple) and len(r) == 2: ok, msg = r
        else: ok, msg = bool(r), "已切分"
    except Exception as e:
        return False, "切分失败: %s: %s" % (type(e).__name__, e), None
    if not ok: return False, msg, None
    wb.push_history()
    wb._after_edit()
    return True, "已在 %.2fs 切分" % t, {"t": t, "detail": str(msg)}


def _cmd_film_filter(wb, args):
    """套电影感滤镜。args: name（预设名）/ list=true 列出全部。"""
    import film_filters as FF
    if args.get("list"):
        return True, "共 %d 个电影感滤镜" % len(FF.FILM_FILTERS), {
            "filters": [{"name": f["name"], "desc": f.get("desc")}
                        for f in FF.FILM_FILTERS]}
    name = str(args.get("name") or "")
    if not name: return False, "需要 name（传 list=true 可查看全部）", None
    if FF.by_name(name) is None: return False, "未知滤镜: %s" % name, None
    wb._apply_film_filter(name)
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    return True, "已套用「%s」" % name, {"name": name,
                                     "preset": getattr(wb, "_cur_filter_preset",
                                                       None)}


def _cmd_stage_effect(wb, args):
    """插入舞台特效（206 个演出特效之一）。args: name 或 code。"""
    import stage_effects as SE
    if args.get("list"):
        cats = [{"category": c, "count": len(SE.by_category(c))}
                for c, _n in SE.CATEGORIES]
        return True, "共 %d 个特效 / %d 个分类" % (
            len(SE.STAGE_EFFECTS), len(SE.CATEGORIES)), {
                "categories": cats, "count": len(SE.STAGE_EFFECTS)}
    name = str(args.get("name") or "")
    code = args.get("code")
    eff = None
    if name:
        eff = SE.by_name(name)
        if eff is None: return False, "未知特效: %s（传 list=true 查看分类）" % name, None
    elif code: eff = {"name": "自定义", "code": [str(code)]}
    else: return False, "需要 name 或 code（传 list=true 查看分类）", None
    wb._apply_stage_effect(eff)
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    return True, "已插入特效「%s」" % eff.get("name"), {"name": eff.get("name")}


def _cmd_transition_set(wb, args):
    """设全片转场。args: name（预设名）/ list=true 列出。"""
    import transitions as TR
    if args.get("list"):
        return True, "共 %d 个转场" % len(TR.catalog()), {
            "transitions": TR.catalog()}
    name = str(args.get("name") or "")
    if not name: return False, "需要 name（传 list=true 可查看）", None
    wb._apply_transition(name)
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    return True, "已设转场「%s」" % name, {"name": name}


def _cmd_export_run(wb, args):
    """导出。args: kind=video|audio|srt, path（可选）。
    video 会起后台线程；audio/srt 同步完成。
    """
    import os as _os
    kind = str(args.get("kind") or "video")
    base = getattr(wb, "proj_dir", None) or _os.path.expanduser("~")
    stem = _os.path.splitext(_os.path.basename(str(base)))[0] or "export"
    path = str(args.get("path") or "")
    if not path:
        ext = {"video": ".mp4", "audio": ".wav", "srt": ".srt"}.get(kind, ".mp4")
        path = _os.path.join(base, stem + ext)
    if kind == "video":
        try:
            wb._start_export(path, 1.0, None, None, None)
        except Exception as e:
            return False, "启动导出失败: %s" % e, None
        return True, "已启动视频导出 -> %s" % path, {"path": path,
                                                 "async": True}
    if kind in ("audio", "srt"):
        try:
            wb._export_sidecar(kind, path)
        except Exception as e:
            return False, "导出失败: %s" % e, None
        # 找到实际产出（可能降级成 .wav）
        real = path
        if not _os.path.isfile(real):
            alt = _os.path.splitext(path)[0] + ".wav"
            if _os.path.isfile(alt): real = alt
        return True, "已导出 %s -> %s" % (kind, real), {"path": real,
                                                    "exists": _os.path.isfile(real)}
    return False, "kind 只能是 video/audio/srt", None


def _cmd_project_save(wb, args):
    """保存工程。args: path（可选，另存为）。"""
    try:
        if args.get("path"): wb.save_as(str(args["path"]))
        else: wb.save_project()
    except Exception as e:
        return False, "保存失败: %s" % e, None
    return True, "已保存", {"dir": getattr(wb, "proj_dir", None)}


def _cmd_history(wb, args):
    """撤销/重做。args: action=undo|redo。"""
    a = str(args.get("action") or "undo")
    try:
        (wb.undo if a == "undo" else wb.redo)()
    except Exception as e:
        return False, "%s 失败: %s" % (a, e), None
    return True, "已%s" % ("撤销" if a == "undo" else "重做"), {"action": a}


def _cmd_timeline_play(wb, args):
    """播放控制。args: action=play|pause|stop|toggle, t（seek 用）。"""
    a = str(args.get("action") or "toggle")
    try:
        if args.get("t") is not None:
            wb.seek(float(args["t"]))
            return True, "已跳到 %.2fs" % float(args["t"]), {"t": wb._t}
        if a == "play":
            if not getattr(wb, "_playing", False): wb.toggle_play()
        elif a in ("pause", "stop"):
            if getattr(wb, "_playing", False): wb.toggle_play()
        else: wb.toggle_play()
    except Exception as e:
        return False, "播放控制失败: %s" % e, None
    return True, "已执行 %s" % a, {"playing": getattr(wb, "_playing", False),
                                "t": getattr(wb, "_t", 0.0)}


_HANDLERS = {
    "project.new": _cmd_project_new,
    "scene.set_bg": _cmd_scene_set_bg,
    "figure.add": _cmd_figure_add,
    "figure.move": _cmd_figure_move,
    "figure.remove": _cmd_figure_remove,
    "motion.play": _cmd_motion_play,
    "motion.set": _cmd_motion_set,
    "expression.set": _cmd_expression_set,
    "catalog.get": _cmd_catalog_get,
    "say": _cmd_say,
    "timeline.seek": _cmd_timeline_seek,
    "snapshot": _cmd_snapshot,
    "webgal.export": _cmd_webgal_export,
    "runtime.status": _cmd_runtime_status,
    "state.get": _cmd_state,
    # G4：assets.query 主线程兜底入口（正常走 _handle 的 HTTP 线程专用分支，
    # 这里保证 run_command 直调/D2 面板等场景同样可用——纯读，无 Qt）
    "assets.query": lambda wb, args: _assets_query(args),
    # K2（task-73）：素材体检（纯读；正常走 _handle 的 HTTP 线程分支，
    # 这里保证 run_command 直调同样可用）
    "assets.check": _cmd_assets_check,
    # ---- F2-L4（v3.5）：多幕 / 分支 / 变量 ----
    "act.list": _cmd_act_list,
    "act.add": _cmd_act_add,
    "act.switch": _cmd_act_switch,
    "act.rename": _cmd_act_rename,
    "act.duplicate": _cmd_act_duplicate,
    "act.remove": _cmd_act_remove,
    "act.next": _cmd_act_next,
    "act.script": _cmd_act_script,
    "choice.set": _cmd_choice_set,
    "choice.remove": _cmd_choice_remove,
    "jump.set": _cmd_jump_set,
    "jump.remove": _cmd_jump_remove,
    "var.set": _cmd_var_set,
    "var.list": _cmd_var_list,
    # ---- v3.6.2：运镜 / 光效 / 投影 / 批量操作 ----
    "camera.apply": _cmd_camera_apply,
    "light.apply": _cmd_light_apply,
    "light.list": _cmd_light_list,
    "shadow.add": _cmd_shadow_add,
    "shadow.remove": _cmd_shadow_remove,
    "batch.run": _cmd_batch_run,
    # ---- v3.6.2 第二批：轨道/剪辑/滤镜/特效/转场/导出/存档/历史/播放 ----
    "track.add": _cmd_track_add,
    "track.list": _cmd_track_list,
    "clip.add": _cmd_clip_add,
    "clip.move": _cmd_clip_move,
    "clip.trim": _cmd_clip_trim,
    "clip.remove": _cmd_clip_remove,
    "clip.split": _cmd_clip_split,
    "film.filter": _cmd_film_filter,
    "stage.effect": _cmd_stage_effect,
    "transition.set": _cmd_transition_set,
    "export.run": _cmd_export_run,
    "project.save": _cmd_project_save,
    "history.do": _cmd_history,
    "timeline.play": _cmd_timeline_play,
    # ---- 语音（v3.6.2 后）：转发到 voice_commands ----
    # 正常走 HTTP 线程直跑（见 _handle 的 voice.* 分支）；这里注册是为了
    # run_command 直调也能用（D2 面板 / 自动化脚本场景）。
    "voice.status": lambda wb, args: _voice_dispatch("voice.status", args),
    "voice.probe": lambda wb, args: _voice_dispatch("voice.probe", args),
    "voice.list": lambda wb, args: _voice_dispatch("voice.list", args),
    "voice.stats": lambda wb, args: _voice_dispatch("voice.stats", args),
    "voice.validate": lambda wb, args: _voice_dispatch("voice.validate", args),
    "voice.import": lambda wb, args: _voice_dispatch("voice.import", args),
    "voice.remove": lambda wb, args: _voice_dispatch("voice.remove", args),
    "voice.refs": lambda wb, args: _voice_dispatch("voice.refs", args),
    "voice.refs.add": lambda wb, args: _voice_dispatch("voice.refs.add", args),
    "voice.refs.rm": lambda wb, args: _voice_dispatch("voice.refs.rm", args),
    "voice.refs.pick": lambda wb, args: _voice_dispatch("voice.refs.pick", args),
}


def _voice_dispatch(cmd, args):
    """语音命令转调（voice_commands 独立模块，见其 docstring）。

    返回 (ok, msg, data)。这些命令全是纯文件系统 / 网络 I/O，不碰 Qt。
    """
    import voice_commands as VC
    return VC.run(cmd, args)


def run_command(wb, cmd, args):
    """主线程执行一条命令，返回 (ok, msg, data)。未知命令/异常都回 ok=False。"""
    fn = _HANDLERS.get(str(cmd or ""))
    if fn is None: return False, "未知命令: %s" % cmd, None
    try:
        return fn(wb, args)
    except Exception as e:
        return False, "命令执行异常: %s: %s" % (type(e).__name__, e), None


def drain():
    """主线程：排空待处理命令队列（由 _schedule 触发）。"""
    wb = _WB
    if wb is None: return
    while True:
        try:
            cmd, args, reply = _PENDING.get_nowait()
        except queue.Empty:
            return
        ok, msg, data = run_command(wb, cmd, args)
        with contextlib.suppress(Exception): reply.put({"ok": ok, "msg": msg, "data": data})


def _schedule(wb, fn):
    """把 fn 投递回 wb 所在线程（主线程）执行。

    优先 QObject.after；本机 PySide6 实测 QWidget 无 after，退化用
    QTimer.singleShot(0, wb, fn)（**必须带 receiver**，否则单 Shot 会绑定到
    调用方线程——HTTP 线程没有事件循环，定时器永不触发）。Qt 只在这里惰性
    import，模块 import 期保持纯 stdlib。"""
    with contextlib.suppress(Exception):
        wb.after(0, fn)
        return
    from PySide6.QtCore import QTimer
    QTimer.singleShot(0, wb, fn)


# ---------------- HTTP ----------------
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):     # 静音，不污染控制台
        pass

    def _send(self, code, payload):
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            with contextlib.suppress(Exception): self.close_connection = True

    def _handle(self, cmd, args):
        wb = _WB
        if wb is None:
            self._send(503, {"ok": False, "msg": "未绑定工作台"})
            return
        reply = queue.Queue()
        # 语音命令（voice_commands）：全部是纯文件系统 / 网络 I/O，**不碰 Qt**，
        # 且 voice.probe 要等 TCP 超时（最长 4s）—— 进主线程队列会把 UI 卡住。
        # 与 assets.query / runtime.import 同款处理：HTTP 线程直跑、直接回包。
        if str(cmd or "").startswith("voice."):
            try:
                ok, msg, data = _voice_dispatch(cmd, args)
            except Exception as e:
                ok, msg, data = False, "语音命令异常: %s" % e, None
            self._send(200, {"ok": ok, "msg": msg, "data": data})
            return
        # task-41/G4：assets.query 在 HTTP 线程纯 py 读索引过滤（不碰 Qt、
        # 不进主线程队列——高频查询别排队长）
        if cmd == "assets.query":
            try:
                ok, msg, data = _assets_query(args)
            except Exception as e:
                ok, msg, data = False, "素材查询异常: %s" % e, None
            self._send(200, {"ok": ok, "msg": msg, "data": data})
            return
        # task-36/E4：runtime.import 的导入在 HTTP 线程跑（runtime_pack 纯 py，
        # 只做文件系统，不碰 Qt），直接回包，无需回主线程。
        if cmd == "runtime.import":
            try:
                ok, msg, data = _runtime_import(args)
            except Exception as e:
                ok, msg, data = False, "runtime 导入异常: %s" % e, None
            self._send(200, {"ok": ok, "msg": msg, "data": data})
            return
        # task-34：script.import 的解析在 HTTP 线程跑（webgal_import 纯 py 无
        # Qt，阻塞不碰 UI，天然安全）；只有产出的 proj 载入回主线程。其余
        # 命令走通用入队 -> 主线程 drain。
        if cmd == "script.import":
            try:
                ok, msg, proj = _parse_script_import(args)
            except Exception as e:
                ok, msg, proj = False, "解析异常 %s: %s" % (type(e).__name__, e), None
            if not ok or proj is None:
                self._send(200, {"ok": bool(ok), "msg": msg, "data": None})
                return

            def _loader():
                try:
                    ok, lmsg, data = _load_proj_on_main(wb, proj, msg)
                    reply.put({"ok": ok, "msg": lmsg, "data": data})
                except Exception as e:
                    reply.put({"ok": False, "msg": "载入异常: %r" % e, "data": None})

            try:
                _schedule(wb, _loader)
            except Exception as e:
                self._send(500, {"ok": False, "msg": "调度失败: %s" % e})
                return
            try:
                res = reply.get(timeout=_REPLY_TIMEOUT)
            except queue.Empty:
                self._send(504, {"ok": False, "msg": "主线程处理超时(%gs)" % _REPLY_TIMEOUT})
                return
            self._send(200, res)
            return
        try:
            _PENDING.put((cmd, args, reply))
        except Exception as e:
            self._send(500, {"ok": False, "msg": "入队失败: %s" % e})
            return
        try:
            _schedule(wb, drain)
        except Exception as e:
            self._send(500, {"ok": False, "msg": "调度失败: %s" % e})
            return
        try:
            res = reply.get(timeout=_REPLY_TIMEOUT)
        except queue.Empty:
            self._send(504, {"ok": False, "msg": "主线程处理超时(%gs)" % _REPLY_TIMEOUT})
            return
        self._send(200, res)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ping":
            self._send(200, {"ok": True, "version": _read_version(), "port": _port()})
        elif path == "/state": self._handle("state.get", {})
        else: self._send(404, {"ok": False, "msg": "未知路由: %s" % path})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/cmd":
            self._send(404, {"ok": False, "msg": "未知路由: %s" % path})
            return
        # v3.4.0 安全加固（联网审计挂账清偿）：
        # 1) Origin 一律拒绝——浏览器跨站脚本打本机 API 的 CSRF 面；
        #    curl/DSH/本机脚本不发 Origin，功能不受影响。
        if self.headers.get("Origin"):
            self._send(403, {"ok": False, "msg": "拒绝带 Origin 的请求（仅本机工具直连）"})
            return
        # 2) Content-Type 若声明必须含 json（挡 text/plain 简单表单类请求）。
        ctype = str(self.headers.get("Content-Type") or "")
        if ctype and "json" not in ctype.lower():
            self._send(415, {"ok": False, "msg": "Content-Type 必须是 application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._send(400, {"ok": False, "msg": "Content-Length 非法"})
            return
        if length < 0:
            self._send(400, {"ok": False, "msg": "Content-Length 非法"})
            return
        if length > _MAX_BODY:
            self._send(413, {"ok": False, "msg": "请求体超过 %d 字节上限" % _MAX_BODY})
            return
        try:
            raw = self.rfile.read(length) if length > 0 else b""
            req = json.loads(raw.decode("utf8")) if raw else {}
            if not isinstance(req, dict): raise ValueError("请求体不是 JSON object")
        except Exception as e:
            self._send(400, {"ok": False, "msg": "JSON 解析失败: %s" % e})
            return
        cmd = str(req.get("cmd") or "")
        args = req.get("args")
        if not isinstance(args, dict): args = {}
        if not cmd:
            self._send(400, {"ok": False, "msg": "缺少 cmd"})
            return
        self._handle(cmd, args)


def start(wb):
    """启动 DSH 联动服务（模块级单例，幂等）。返回实际端口。"""
    global _SERVER, _THREAD, _WB, _PORT
    if _SERVER is not None: return _PORT
    _WB = wb
    _PORT = _port()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", _PORT), _Handler)
    except OSError as e:
        _WB = None
        raise RuntimeError("DSH 联动端口 %d 启动失败: %s" % (_PORT, e))
    server.daemon_threads = True
    _SERVER = server
    _THREAD = threading.Thread(target=server.serve_forever, daemon=True,
                               name="muppet-remote")
    _THREAD.start()
    return _PORT


def stop():
    """停止服务（幂等）：shutdown + 线程退出。"""
    global _SERVER, _THREAD, _WB
    server = _SERVER
    if server is None: return
    _SERVER = None
    _WB = None
    with contextlib.suppress(Exception): server.shutdown()
    with contextlib.suppress(Exception): server.server_close()
    t = _THREAD
    if t is not None and t.is_alive(): t.join(timeout=3)
    _THREAD = None


def is_running():
    return _SERVER is not None


if __name__ == "__main__":
    # 最小自测：起服务，等 Ctrl+C（不连工作台，仅验证端口/路由）
    import sys
    port = _port()
    s = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print("remote_control 自测监听 http://127.0.0.1:%d/ping" % port)
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        s.server_close()
