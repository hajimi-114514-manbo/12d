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
import threading
import time
import tempfile

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    try:
        import video_source as VS
        src = VS.probe_source(path)
        if isinstance(src, dict) and src.get("path"):
            src["kind"] = kind
            return src
    except Exception:
        pass
    return V.new_source(kind, path)


def _refresh(wb):
    """命令改动后的轻量刷新（失败不抛，命令仍返回成功）。"""
    try:
        wb._after_edit()
    except Exception:
        pass


def _track_by_name(wb, name):
    V = _vm()
    for tr in wb.proj.get("tracks", []):
        if tr.get("name") == name:
            return tr
    return None


def _new_fig_track(wb):
    V = _vm()
    n = 1
    while True:
        name = "FIG-%d" % n
        if _track_by_name(wb, name) is None:
            break
        n += 1
    ok, msg, tid = V.add_track(wb.proj, "video", name)
    if not ok:
        raise RuntimeError(msg or "新建立绘轨失败")
    return V.find_track(wb.proj, tid)


# ---------------- 命令实现（全部在主线程执行） ----------------
def _cmd_project_new(wb, args):
    V = _vm()
    w = int(args.get("w") or 1920)
    h = int(args.get("h") or 1080)
    fps = float(args.get("fps") or 30)
    try:
        wb._sources.close_all()
    except Exception:
        pass
    wb.proj = V.new_project(w, h, fps)
    wb.proj["dialogue"] = []
    try:
        wb._apply_project(None)
    except Exception:
        pass
    try:
        wb.push_history()
    except Exception:
        pass
    return True, "已新建工程 %dx%d @%gfps" % (w, h, fps), {"width": w, "height": h, "fps": fps}


def _cmd_scene_set_bg(wb, args):
    V = _vm()
    path = str(args.get("path") or "")
    if not path or not os.path.isfile(path):
        return False, "背景图不存在: %s" % path, None
    dur = float(args.get("dur") or 10.0)
    tr = _track_by_name(wb, "BG")
    if tr is None:
        ok, msg, tid = V.add_track(wb.proj, "video", "BG")
        if not ok:
            return False, msg, None
        tr = V.find_track(wb.proj, tid)
    src = _make_source("image", path)
    clip = V.new_clip(src, 0.0, dur)
    clip["layout"] = dict(V.default_layout())   # 满幅默认布局
    ok, cid = V.add_clip(wb.proj, tr["id"], clip)
    if not ok:
        return False, cid, None
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
            if n > 99:
                break
        ok, msg, tid = V.add_track(wb.proj, "video", name)
        if not ok:
            return None
        tr = V.find_track(wb.proj, tid)
    return tr


def _retire_overlapping(wb, tr, start, dur):
    """同轨换装/重入：旧 clip 结束于新入场点，或被新区间覆盖则移除。"""
    V = _vm()
    ne = start + dur
    for oc in list(tr.get("clips") or []):
        os_ = float(oc.get("start") or 0.0)
        oe = os_ + float(oc.get("dur") or 0.0)
        if oe <= start or os_ >= ne:
            continue
        if os_ < start:
            oc["dur"] = round(start - os_, 3)
        else:
            V.remove_clip(wb.proj, str(oc.get("id") or ""))


def _cmd_figure_add(wb, args):
    V = _vm()
    path = str(args.get("path") or "")
    if not path or not os.path.exists(path):
        return False, "素材路径不存在: %s" % path, None
    kind = args.get("kind")
    if not kind:
        # 目录含 rig_character.psd（木偶工程）或 model.json（Cubism2 整合包
        # 模型，H1 导出分支消费）-> puppet；否则 image
        kind = "puppet" if (os.path.isdir(path) and (
            os.path.isfile(os.path.join(path, "rig_character.psd"))
            or os.path.isfile(os.path.join(path, "model.json")))) \
            else "image"
    x = float(args.get("x", 0.5))
    y = float(args.get("y", 0.7))
    scale = float(args.get("scale", 0.6))
    # K1（task-69）：缺省时长=实际到时间轴末尾（驻场规约：不写时长不得
    # 让角色到期悄悄消失；退场只认显式 figure.remove/脚本 none 声明）。
    dur = float(args.get("dur") or 3600.0)
    try:
        start = float(args.get("start", 0.0))
    except (TypeError, ValueError):
        start = 0.0
    # K1：同屏人数上限撤销——不再拒绝任何入场；同角色复用轨。
    tr = _fig_track_for(wb, path)
    if tr is None:
        return False, "建立绘轨失败", None
    _retire_overlapping(wb, tr, start, dur)
    src = _make_source(kind, path)
    clip = V.new_clip(src, start, dur)
    lay = clip["layout"]
    lay["x"], lay["y"], lay["scale"] = x, y, scale
    # C2 + G5：motion/expression 接受表内 id 或 Cubism 键名形态（统一走
    # video_model._motion_value_ok 口径，与 sanitize 一致，不再只认表内 id）
    MT = _mt()
    for key in ("motion", "expression"):
        v = args.get(key)
        if not v:
            continue
        ids = None
        if MT is not None:
            ids = set(MT.motion_ids() if key == "motion"
                      else MT.expression_ids())
        if V._motion_value_ok(str(v), ids):
            clip[key] = str(v)
    ok, cid = V.add_clip(wb.proj, tr["id"], clip)
    if not ok:
        return False, cid, None
    _refresh(wb)
    return True, "已添加立绘 %s(%s) -> %s" % (os.path.basename(path), kind, cid), \
        {"id": cid, "kind": kind, "track": tr["name"]}


def _cmd_figure_move(wb, args):
    V = _vm()
    cid = str(args.get("clip_id") or "")
    _tr, c = V.find_clip(wb.proj, cid)
    if c is None:
        return False, "找不到片段: %s" % cid, None
    # K1（task-69）：同屏上限撤销——start 位移只做时间移动不做人数检查；
    # x/y/scale/rot 直通任意值（无 snap，平移支持）。
    if args.get("start") is not None:
        try:
            new_start = float(args.get("start"))
        except (TypeError, ValueError):
            return False, "start 非法", None
        if new_start < 0:
            return False, "start 非法", None
        c["start"] = round(new_start, 3)
    lay = V.layout_at(c)
    for k in ("x", "y", "scale", "rot"):
        if args.get(k) is not None:
            lay[k] = float(args[k])
    c["layout"] = lay
    try:
        wb.push_history()
    except Exception:
        pass
    try:
        wb.render_frame(wb._t)
    except Exception:
        pass
    return True, "已移动 %s" % cid, dict(lay)


def _cmd_figure_remove(wb, args):
    V = _vm()
    cid = str(args.get("clip_id") or "")
    ok, msg = V.remove_clip(wb.proj, cid)
    if not ok:
        return False, msg or "删除失败", None
    _refresh(wb)
    return True, "已删除 %s" % cid, None


def _cmd_motion_play(wb, args):
    V = _vm()
    cid = str(args.get("clip_id") or "")
    _tr, c = V.find_clip(wb.proj, cid)
    if c is None:
        return False, "找不到片段: %s" % cid, None
    MT = _mt()
    name = args.get("name")
    if name:
        if MT is None:
            return False, "motion_table 不可用", None
        if str(name) not in MT.motion_ids():
            return True, "动作 %s 不在预置列表（仅提示）" % name, None
        c["motion"] = str(name)          # C2：set + seek 组合
        try:
            wb.push_history()
        except Exception:
            pass
    wb.seek(c["start"])
    return True, "已设置并跳转 %s 起点" % cid, {"t": float(c["start"]),
                                               "motion": c.get("motion", "")}


def _cmd_motion_set(wb, args):
    """motion.set{clip_id,name}：校验 catalog，写 clip["motion"]。"""
    V = _vm()
    cid = str(args.get("clip_id") or "")
    _tr, c = V.find_clip(wb.proj, cid)
    if c is None:
        return False, "找不到片段: %s" % cid, None
    MT = _mt()
    if MT is None:
        return False, "motion_table 不可用", None
    name = str(args.get("name") or "")
    ids = MT.motion_ids()
    if name not in ids:
        return False, "动作 %s 不在预置列表" % name, {"options": ids}
    c["motion"] = name
    try:
        wb.push_history()
        wb.render_frame(wb._t)
    except Exception:
        pass
    return True, "已设置动作 %s" % name, {"motion": name}


def _cmd_expression_set(wb, args):
    """expression.set{clip_id,name}：校验 catalog，写 clip["expression"]。"""
    V = _vm()
    cid = str(args.get("clip_id") or "")
    _tr, c = V.find_clip(wb.proj, cid)
    if c is None:
        return False, "找不到片段: %s" % cid, None
    MT = _mt()
    if MT is None:
        return False, "motion_table 不可用", None
    name = str(args.get("name") or "")
    ids = MT.expression_ids()
    if name not in ids:
        return False, "表情 %s 不在预置列表" % name, {"options": ids}
    c["expression"] = name
    try:
        wb.push_history()
        wb.render_frame(wb._t)
    except Exception:
        pass
    return True, "已设置表情 %s" % name, {"expression": name}


def _cmd_catalog_get(wb, _args=None):
    """catalog.get{}：原样返回 motion_table.catalog()（DSH 直读）。"""
    MT = _mt()
    if MT is None:
        return False, "motion_table 不可用", None
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
    if mv:
        entry["motion"] = mv
    if ex:
        entry["expression"] = ex
    wb.proj.setdefault("dialogue", []).append(entry)
    try:
        wb.push_history()
    except Exception:
        pass
    try:
        wb.render_frame(wb._t)
    except Exception:
        pass
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
    if not out_dir:
        return False, "缺少 out_dir", None
    try:
        import webgal_export as WX
    except ImportError:
        return False, "WebGAL 导出器未就绪(task-26)", None
    exp = getattr(WX, "export_game", None)
    if exp is None:
        return False, "WebGAL 导出器未就绪(task-26)", None
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
        if len(res) >= 3:
            ok, msg, stats = res[0], res[1], res[2]
        else:
            ok, msg, stats = res[0], res[1], None
    except (TypeError, ValueError, IndexError):
        return False, "WebGAL 导出器返回值异常: %r" % (res,), None
    data = {"out_dir": out_dir}
    if isinstance(stats, dict):
        data["stats"] = stats
    return bool(ok), str(msg), data


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
    if i < 0:
        return 0
    digits = ""
    for ch in s[i + len("警告"):]:
        if ch.isdigit():
            digits += ch
        else:
            break
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
            try:
                wb.seek(0.0)
            except Exception:
                pass
            try:
                wb.stop_play()
            except Exception:
                pass
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
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.success("脚本导入", msg, parent=wb,
                            position=InfoBarPosition.TOP, duration=4000)
        except Exception:
            pass
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
    if isinstance(tags, str):
        tags = [tags]
    elif tags is not None and not isinstance(tags, (list, tuple)):
        return False, "tags 必须是字符串或字符串列表", None
    limit = args.get("limit", 30)
    if limit is None:
        limit = 30
    try:
        return WI.assets_query(kind=kind, tags=list(tags) if tags else None,
                               limit=limit)
    except (TypeError, ValueError) as e:
        return False, "查询参数非法: %s" % e, None
    except Exception as e:
        return False, "素材查询异常 %s: %s" % (type(e).__name__, e), None


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
}


def run_command(wb, cmd, args):
    """主线程执行一条命令，返回 (ok, msg, data)。未知命令/异常都回 ok=False。"""
    fn = _HANDLERS.get(str(cmd or ""))
    if fn is None:
        return False, "未知命令: %s" % cmd, None
    try:
        return fn(wb, args)
    except Exception as e:
        return False, "命令执行异常: %s: %s" % (type(e).__name__, e), None


def drain():
    """主线程：排空待处理命令队列（由 _schedule 触发）。"""
    wb = _WB
    if wb is None:
        return
    while True:
        try:
            cmd, args, reply = _PENDING.get_nowait()
        except queue.Empty:
            return
        ok, msg, data = run_command(wb, cmd, args)
        try:
            reply.put({"ok": ok, "msg": msg, "data": data})
        except Exception:
            pass


def _schedule(wb, fn):
    """把 fn 投递回 wb 所在线程（主线程）执行。

    优先 QObject.after；本机 PySide6 实测 QWidget 无 after，退化用
    QTimer.singleShot(0, wb, fn)（**必须带 receiver**，否则单 Shot 会绑定到
    调用方线程——HTTP 线程没有事件循环，定时器永不触发）。Qt 只在这里惰性
    import，模块 import 期保持纯 stdlib。"""
    try:
        wb.after(0, fn)
        return
    except Exception:
        pass
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
            try:
                self.close_connection = True
            except Exception:
                pass

    def _handle(self, cmd, args):
        wb = _WB
        if wb is None:
            self._send(503, {"ok": False, "msg": "未绑定工作台"})
            return
        reply = queue.Queue()
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
        elif path == "/state":
            self._handle("state.get", {})
        else:
            self._send(404, {"ok": False, "msg": "未知路由: %s" % path})

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
            if not isinstance(req, dict):
                raise ValueError("请求体不是 JSON object")
        except Exception as e:
            self._send(400, {"ok": False, "msg": "JSON 解析失败: %s" % e})
            return
        cmd = str(req.get("cmd") or "")
        args = req.get("args")
        if not isinstance(args, dict):
            args = {}
        if not cmd:
            self._send(400, {"ok": False, "msg": "缺少 cmd"})
            return
        self._handle(cmd, args)


def start(wb):
    """启动 DSH 联动服务（模块级单例，幂等）。返回实际端口。"""
    global _SERVER, _THREAD, _WB, _PORT
    if _SERVER is not None:
        return _PORT
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
    if server is None:
        return
    _SERVER = None
    _WB = None
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass
    t = _THREAD
    if t is not None and t.is_alive():
        t.join(timeout=3)
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
