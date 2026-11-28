# -*- coding: utf-8 -*-
"""口型匹配的 DSH 联动命令。

为什么单独一个模块
------------------
remote_control.py 已经 2035 行。口型这块逻辑自成一套（绑定校验、
语音解析、状态回读），塞进去只会让它更难读。

**这些命令要改工程数据（`wb.proj`），必须走主线程**（`run_command`），
不能进 `HTTP_ONLY` —— 与 voice_commands 那种纯文件 I/O 的命令不同。

只做**手动绑定**（用户/AI 明确指定哪个立绘跟哪句语音），不做自动绑定：
自动绑定是产品策略（一个角色 5 句台词、立绘跨全部 5 句时该跟哪句？），
先留给人/AI 决定。

命令一览
--------
    lip.list     列出当前幕所有口型绑定（AI 编排前先查）
    lip.status   口型能力状态：有多少立绘/语音/已绑定，渲染是否可用
    lip.bind     把一个立绘 clip 绑到某句语音上（可带 gain）
    lip.unbind   解除绑定（留空 = 清掉当前幕全部）

约定（与 remote_control 其余命令一致）：
    返回 (ok: bool, msg: str, data: dict|None)
"""
import contextlib
import os


def _vm():
    import video_model as VM
    return VM


def _lip():
    import lip_sync as LS
    return LS


# ---------------- 内部助手 ----------------
def _aid(wb):
    """当前幕 id（单幕工程返回 None，与既有命令口径一致）。"""
    return getattr(wb, "_cur_aid", None)


def _find_figure_clip(proj, cid):
    """按 id 找**立绘** clip -> (clip, track) 或 (None, None)。

    只认立绘（is_figure_clip 口径），不认背景/音频 —— 口型只对立绘有意义。
    """
    VM = _vm()
    cid = str(cid or "").strip()
    if not cid:
        return None, None
    for tr in VM._tracks_of(proj):
        if not isinstance(tr, dict):
            continue
        for c in (tr.get("clips") or []):
            if isinstance(c, dict) and str(c.get("id") or "") == cid:
                if VM.is_figure_clip(c, tr):
                    return c, tr
                return None, None          # 找到了但不是立绘
    return None, None


def _voice_brief(vc):
    """语音 clip -> 给 AI 看的摘要（别把整个 dict 倒出去）。"""
    if not isinstance(vc, dict):
        return None
    return {
        "voice_id": vc.get("id"),
        "t": vc.get("t"),
        "dur": vc.get("dur"),
        "role": vc.get("role"),
        "text": str(vc.get("text") or "")[:40],
        "path": vc.get("path"),
        "detach": bool(vc.get("detach")),
    }


def _figure_brief(c):
    """立绘 clip -> 摘要。"""
    if not isinstance(c, dict):
        return None
    src = c.get("source") or {}
    return {
        "clip_id": c.get("id"),
        "start": c.get("start"),
        "dur": c.get("dur"),
        "src": str(src.get("path") or "")[-48:],
        "lip": c.get("lip"),
    }


# ---------------- 读命令 ----------------
def _lip_status(wb, args):
    """口型能力总览：AI 判断「这里能不能用口型」的第一入口。

    args:
      role: 可选，只看这个角色的语音
    """
    VM = _vm()
    LS = _lip()
    proj = getattr(wb, "proj", None) or {}
    aid = _aid(wb)
    try:
        figs = []
        for tr in VM._tracks_of(proj, aid):
            if not isinstance(tr, dict) or tr.get("kind") == "audio":
                continue
            for c in (tr.get("clips") or []):
                if VM.is_figure_clip(c, tr):
                    figs.append(c)
        voices = VM.voice_clips(proj, aid)
        binds = VM.lip_bindings(proj, aid)
    except Exception as e:
        return False, "读工程失败: %s: %s" % (type(e).__name__, e), None

    n_bound = 0
    for c in figs:
        if str(c.get("id") or "") in binds:
            n_bound += 1
    # 口型只对 Live2D（cubism2/model.json）有效：定格图和 PNG 立绘没有嘴部参数
    live2d = 0
    for c in figs:
        src = c.get("source") or {}
        p = str(src.get("path") or "")
        if p.lower().replace("\\", "/").rstrip("/").endswith(("model.json",
                                                              ".json")):
            live2d += 1
    data = {
        "figures": len(figs),
        "figures_live2d": live2d,
        "voices": len(voices),
        "bound": n_bound,
        "usable": bool(figs and voices),
        "note": "",
        "cache": LS.stats(),
    }
    if not voices:
        data["note"] = "当前幕没有语音，口型无处可绑（先配台词语音）"
    elif not figs:
        data["note"] = "当前幕没有立绘，没有可绑的对象"
    elif not live2d:
        data["note"] = ("当前幕立绘都不是 Live2D 模型 —— 口型只对 Live2D 有效，"
                        "定格图/PNG 立绘没有嘴部参数")
    msg = "立绘 %d（Live2D %d）/ 语音 %d / 已绑定 %d" % (
        len(figs), live2d, len(voices), n_bound)
    return True, msg, data


def _lip_list(wb, args):
    """列出当前幕的口型绑定 + 可绑的语音候选。

    AI 编排前先查这个，拿到 clip_id / voice_id 再 bind。
    """
    VM = _vm()
    proj = getattr(wb, "proj", None) or {}
    aid = _aid(wb)
    try:
        binds = VM.lip_bindings(proj, aid)
        voices = VM.voice_clips(proj, aid)
        figs = []
        for tr in VM._tracks_of(proj, aid):
            if not isinstance(tr, dict) or tr.get("kind") == "audio":
                continue
            for c in (tr.get("clips") or []):
                if VM.is_figure_clip(c, tr):
                    figs.append(c)
    except Exception as e:
        return False, "读工程失败: %s: %s" % (type(e).__name__, e), None

    vmap = {str(v.get("id")): v for v in voices}
    bound, free = [], []
    for c in figs:
        cid = str(c.get("id") or "")
        lp = binds.get(cid)
        if lp:
            vc = vmap.get(str(lp.get("voice")))
            item = _figure_brief(c)
            item["voice"] = _voice_brief(vc)
            item["gain"] = lp.get("gain", 1.0)
            if vc is None:
                # 绑了但语音被删了 —— 明确报出来，别让 AI 以为还正常
                item["broken"] = True
            bound.append(item)
        else:
            free.append(_figure_brief(c))
    data = {"bound": bound, "unbound": free,
            "voices": [_voice_brief(v) for v in voices],
            "count": {"bound": len(bound), "unbound": len(free),
                      "voices": len(voices)}}
    msg = "已绑定 %d / 未绑定 %d，可绑语音 %d 句" % (
        len(bound), len(free), len(voices))
    return True, msg, data


# ---------------- 写命令 ----------------
def _lip_bind(wb, args):
    """把一个立绘 clip 绑到某句语音上（**手动绑定**，不做自动推断）。

    args:
      clip_id:   立绘 clip 的 id（必填）
      voice_id:  语音 id，形如 "vc:0"（必填）；也可传 voice_index（整数）
      gain:      开口度倍率 0.3~2.5，默认 1.0（喊叫可调大）
      auto_pick: true 时在你没给 voice_id 的情况下，自动挑「与该立绘时间
                 重叠的第一句同角色语音」——**需要显式开启**，不默认做

    为什么 voice_id 优先、auto_pick 要显式开：自动挑容易挑错（一个角色
    多句台词时语义上该跟哪句只有人知道），所以默认要 AI 自己指定。
    """
    VM = _vm()
    proj = getattr(wb, "proj", None) or {}
    aid = _aid(wb)
    cid = str(args.get("clip_id") or "").strip()
    if not cid:
        return False, "缺少 clip_id（立绘 clip 的 id，先用 lip.list 查）", None

    clip, track = _find_figure_clip(proj, cid)
    if clip is None:
        return False, ("找不到立绘 clip: %s（口型只能绑立绘，背景/音频 clip "
                       "不支持；先用 lip.list 看可绑的 clip_id）" % cid), None

    voices = VM.voice_clips(proj, aid)
    if not voices:
        return False, "当前幕没有语音，无处可绑（先给台词配语音）", None

    vid = str(args.get("voice_id") or "").strip()
    if not vid:
        idx = args.get("voice_index")
        if idx is not None:
            with contextlib.suppress(TypeError, ValueError):
                i = int(idx)
                if 0 <= i < len(voices):
                    vid = str(voices[i].get("id"))
    if not vid and args.get("auto_pick"):
        # 只在显式要求时自动挑：与该立绘时间重叠的第一句同角色语音。
        # 角色名从轨道名推（FIG-<who>），与既有口径一致。
        s0 = float(clip.get("start") or 0.0)
        d0 = float(clip.get("dur") or 0.0)
        s1 = s0 + d0
        tn = str((track or {}).get("name") or "")
        who = tn[4:].strip() if tn.upper().startswith("FIG-") else ""
        for v in voices:
            vt = float(v.get("t") or 0.0)
            vd = float(v.get("dur") or 0.0)
            if vt < s1 - 1e-6 and vt + vd > s0 + 1e-6:
                if who and str(v.get("role") or "") != who:
                    continue
                vid = str(v.get("id"))
                break
        if not vid:
            return False, ("auto_pick 没找到与该立绘时间重叠的同角色语音；"
                           "请显式给 voice_id（lip.list 里的 voice_id）"), None

    if not vid:
        return False, ("缺少 voice_id（形如 vc:0）；可用 lip.list 查当前幕的"
                       "语音列表"), None

    vc = VM.voice_clip_by_id(proj, vid, aid)
    if vc is None:
        return False, ("找不到语音: %s（当前幕共 %d 句；用 lip.list 查）"
                       % (vid, len(voices))), None
    if not os.path.isfile(str(vc.get("path") or "")):
        # 语音文件不在了：明确拒绝，别写进一个永远没声音的绑定
        return False, ("语音文件不存在，拒绝绑定: %s"
                       % vc.get("path")), {"voice": _voice_brief(vc)}

    gain = 1.0
    if args.get("gain") is not None:
        with contextlib.suppress(TypeError, ValueError):
            gain = float(args.get("gain"))
    lp = VM._sanitize_lip({"voice": vid, "gain": gain})
    if not lp:
        return False, "voice_id 形态不合法（要 vc:<数字>）: %r" % (vid,), None

    clip["lip"] = lp
    data = {"clip": _figure_brief(clip), "voice": _voice_brief(vc),
            "gain": lp.get("gain", 1.0)}
    return True, ("已把立绘 %s 的口型绑到 %s（%s，%.1fs）"
                  % (cid, vid,
                     str(vc.get("text") or "")[:16] or vc.get("path"),
                     float(vc.get("dur") or 0.0))), data


def _lip_unbind(wb, args):
    """解除口型绑定。

    args:
      clip_id: 立绘 clip id；留空 = 清掉**当前幕全部**绑定
    """
    VM = _vm()
    proj = getattr(wb, "proj", None) or {}
    aid = _aid(wb)
    cid = str(args.get("clip_id") or "").strip()
    if not cid:
        n = 0
        for tr in VM._tracks_of(proj, aid):
            if not isinstance(tr, dict) or tr.get("kind") == "audio":
                continue
            for c in (tr.get("clips") or []):
                if isinstance(c, dict) and c.get("lip") is not None:
                    c.pop("lip", None)
                    n += 1
        return True, "已清掉 %d 个口型绑定" % n, {"cleared": n}

    clip, _tr = _find_figure_clip(proj, cid)
    if clip is None:
        return False, "找不到立绘 clip: %s" % cid, None
    if clip.get("lip") is None:
        return True, "立绘 %s 本来就没绑口型" % cid, {"cleared": 0}
    clip.pop("lip", None)
    return True, "已解除立绘 %s 的口型绑定" % cid, {"clip_id": cid, "cleared": 1}


# ---------------- v3.7.0：自动匹配 / 切段 / 一键重算 / 覆盖率 ----------------
# 为什么需要这一组
# ----------------
# v3.6.x 只有「手动一句一句绑」。实测本片 11 个立绘 clip 里 8 个绑了、但绑的
# 那句往往只覆盖片段的一小段（c21 100 秒只绑了 2.18 秒的 vc:56），全片口型
# 覆盖率只有 2.3%。手工绑既干不完、也发现不了「一个片段跨多句」这种结构性
# 问题 —— 所以补三个能力：自动匹配（补未绑）、按句边界切段（治跨多句）、
# 覆盖率查询（把「嘴到底动没动」变成可读数字）。


def _ve():
    """懒加载 video_export（覆盖率口径必须与导出**同一份实现**）。"""
    import video_export as VE
    return VE


def _cover_of(proj, clip, aid, tr=None):
    """单 clip 覆盖率（走导出层同一口径，绝不另写一套）。"""
    try:
        return _ve().lip_coverage(proj, clip, aid, tr)
    except Exception as e:
        return {"clip_id": str((clip or {}).get("id") or ""),
                "error": "%s: %s" % (type(e).__name__, e)}


def _figures(VM, proj, aid):
    """[(track, clip)] —— 当前幕全部立绘 clip。"""
    out = []
    for tr in VM._tracks_of(proj, aid):
        if not isinstance(tr, dict) or tr.get("kind") == "audio":
            continue
        for c in (tr.get("clips") or []):
            if isinstance(c, dict) and VM.is_figure_clip(c, tr):
                out.append((tr, c))
    return out


def _touch(wb, structural=False):
    """写工程后统一收尾：记撤销点 + 让时间轴/预览跟上。

    失败一律静默 —— 命令本身已经成功，不能因为 UI 刷新出错就报失败。
    """
    for name, kw in (("push_history", {}), ("_after_edit", {})):
        fn = getattr(wb, name, None)
        if callable(fn):
            try:
                fn(**kw)
            except Exception:
                pass
    if structural:
        rf = getattr(wb, "render_frame", None)
        if callable(rf):
            try:
                rf(float(getattr(wb, "_t", 0.0)) or 0.0)
            except Exception:
                pass


def _lip_auto(wb, args):
    """为立绘 clip 自动匹配语音（时间重叠 + 角色）。

    args:
      overwrite: 默认 False —— 只补 `lip` 为空的；true = 全部按重叠重挑
      gain:      统一增益（默认 1.0；overwrite 时保留原有 gain）
      who:       只处理某角色的立绘轨
      dry_run:   只出计划、不改工程
    只写 `clip["lip"]`（一句「主语音」），片段内**其余句子靠导出的重叠
    合并自动补上**（见 video_export._lip_seq_for v3.7.0）。
    """
    VM = _vm()
    proj = getattr(wb, "proj", None) or {}
    aid = _aid(wb)
    overwrite = bool(args.get("overwrite"))
    dry = bool(args.get("dry_run"))
    want_who = str(args.get("who") or "").strip().lower()
    try:
        gain = float(args.get("gain") or 0.0)
    except (TypeError, ValueError):
        gain = 0.0
    if gain <= 0:
        gain = 0.0

    planned, skipped, applied = [], [], 0
    for tr, c in _figures(VM, proj, aid):
        who = VM.figure_who(tr)
        if want_who and who.lower() != want_who:
            continue
        cid = str(c.get("id") or "")
        cur = VM._sanitize_lip(c.get("lip"))
        if cur and not overwrite:
            continue                        # 已绑定，尊重人工结果
        voices, role_filtered = VM.overlap_voices(proj, c, aid, tr)
        if not voices:
            skipped.append({"clip_id": cid, "why": "片段内没有重叠语音"})
            continue
        first = voices[0]
        g = gain or float((cur or {}).get("gain") or 1.0)
        lp = VM._sanitize_lip({"voice": first.get("id"), "gain": g})
        if not lp:
            skipped.append({"clip_id": cid, "why": "语音不可用: %s" %
                            first.get("id")})
            continue
        cover = _cover_of(proj, c, aid, tr)
        planned.append({
            "clip_id": cid, "who": who, "voice": lp.get("voice"),
            "gain": lp.get("gain"),
            "text": str(first.get("text") or "")[:40],
            "voices": [v.get("id") for v in voices],
            "role_filtered": role_filtered,
            "cover_pct": cover.get("cover_pct"),
        })
        if not dry:
            c["lip"] = lp
            applied += 1

    if not dry and applied:
        _touch(wb)
    msg = ("[试算] 可自动匹配 %d 个立绘" if dry else "自动匹配 %d 个立绘") % (
        len(planned) if dry else applied)
    if skipped:
        msg += "，跳过 %d" % len(skipped)
    return True, msg, {"applied": applied if not dry else 0,
                       "planned": planned, "skipped": skipped,
                       "dry_run": dry}


def _plan_cuts(VM, proj, c, aid, tr, min_dur, max_parts):
    """算一个跨句立绘片段的切点 -> (cuts, voices)。

    只在**语音句起点**切，且切点距片段两端至少 min_dur，避免切出碎渣；
    段数上限 max_parts（默认 6），超了只切前 max_parts-1 刀。
    """
    s0, d0, s1 = VM.clip_span(c)
    voices, _rf = VM.overlap_voices(proj, c, aid, tr)
    cuts = []
    for v in voices[1:]:
        t = float(v.get("t") or 0.0)
        if s0 + min_dur - 1e-6 <= t <= s1 - min_dur + 1e-6:
            cuts.append(round(t, 3))
    cuts = sorted(set(cuts))
    if len(cuts) + 1 > max_parts:
        cuts = cuts[:max(0, max_parts - 1)]
    return cuts, voices


def _lip_split(wb, args):
    """把跨多句的立绘片段按语音句边界切段，每段绑自己那句。

    args:
      clip_id:  只处理这个片段；留空 = 当前幕所有跨句立绘片段
      min_dur:  最短段长（默认 0.8s），切点离片段两端不足它的不切
      max_parts: 单片段最多切几段（默认 6）
      dry_run:  只出计划
    切分用 VM.split_clip（**会新建 clip、不动 source**），motion/expression
    与 layout 原样继承，所以动作/构图在切点前后连续。
    """
    VM = _vm()
    proj = getattr(wb, "proj", None) or {}
    aid = _aid(wb)
    dry = bool(args.get("dry_run"))
    cid = str(args.get("clip_id") or "").strip()
    try:
        min_dur = float(args.get("min_dur") or 0.8)
    except (TypeError, ValueError):
        min_dur = 0.8
    try:
        max_parts = int(args.get("max_parts") or 6)
    except (TypeError, ValueError):
        max_parts = 6
    if min_dur <= 0:
        min_dur = 0.8
    max_parts = max(2, min(12, max_parts))

    targets = []
    for tr, c in _figures(VM, proj, aid):
        if cid and str(c.get("id") or "") != cid:
            continue
        cuts, voices = _plan_cuts(VM, proj, c, aid, tr, min_dur, max_parts)
        if cuts:
            targets.append((tr, c, cuts, voices))
    if cid and not targets:
        _c, _t = _find_figure_clip(proj, cid)
        if _c is None:
            return False, "找不到立绘 clip: %s" % cid, None
        return True, "立绘 %s 未跨多句，无需切段" % cid, {"segments": []}

    plan = []
    for tr, c, cuts, voices in targets:
        plan.append({"clip_id": str(c.get("id") or ""),
                     "start": c.get("start"), "dur": c.get("dur"),
                     "cuts": cuts,
                     "voices": [v.get("id") for v in voices]})
    if dry:
        return True, "[试算] %d 个片段需按句切段" % len(plan), {
            "dry_run": True, "plan": plan}

    done = []
    for tr, c, cuts, voices in targets:
        cid0 = str(c.get("id") or "")
        motion = c.get("motion", "")
        expr = c.get("expression", "")
        speed = c.get("speed", None)
        ids, cur = [], cid0
        failed = ""
        for cut in cuts:
            try:
                ok, m, rid = VM.split_clip(proj, cur, cut)
            except Exception as e:
                ok, m, rid = False, "%s: %s" % (type(e).__name__, e), None
            if not ok or not rid:
                failed = str(m or "切分失败")
                break
            _tr2, rc = VM.find_clip(proj, rid)
            if isinstance(rc, dict):
                # 动作/表情/速度与母片段一致 —— 切段只改时间边界，不改表演
                rc["motion"] = motion
                rc["expression"] = expr
                if speed is not None:
                    rc["speed"] = speed
            ids.append(cur)
            cur = rid
        if failed:                          # 中途失败：已切的段留着，如实报
            done.append({"clip_id": cid0, "error": failed, "segments": ids})
            continue
        ids.append(cur)
        segs = []
        for sid in ids:
            _tr3, sc = VM.find_clip(proj, sid)
            if not isinstance(sc, dict):
                continue
            vs, rf = VM.overlap_voices(proj, sc, aid, tr)
            lp = (VM._sanitize_lip({"voice": vs[0].get("id"), "gain": 1.0})
                  if vs else None)
            if lp:
                sc["lip"] = lp
            cov = _cover_of(proj, sc, aid, tr)
            segs.append({"clip_id": sid, "start": sc.get("start"),
                         "dur": sc.get("dur"),
                         "voice": (lp or {}).get("voice"),
                         "voices": [v.get("id") for v in vs],
                         "cover_pct": cov.get("cover_pct")})
        done.append({"clip_id": cid0, "segments": segs})

    n_seg = sum(len(d.get("segments") or []) for d in done)
    if n_seg:
        _touch(wb, structural=True)
    return True, "已切段 %d 个片段 -> %d 段" % (len(done), n_seg), {
        "splits": done}


def _lip_recalc(wb, args):
    """一键重算口型：切段治跨句 + 自动匹配补未绑 + 报前后覆盖率。

    args:
      split:     默认 True，是否按句边界切分跨句片段
      overwrite: 默认 True，是否重挑已绑定的主语音
      min_dur / max_parts / dry_run: 透传
    """
    VM = _vm()
    proj = getattr(wb, "proj", None) or {}
    aid = _aid(wb)
    dry = bool(args.get("dry_run"))
    do_split = args.get("split")
    do_split = True if do_split is None else bool(do_split)
    overwrite = args.get("overwrite")
    overwrite = True if overwrite is None else bool(overwrite)

    before = _lip_cover(wb, {"quiet": True})
    b = (before[2] or {}).get("total") or {}
    out = {"before": b, "split": None, "auto": None, "after": None}

    if do_split:
        sp = _lip_split(wb, {"dry_run": dry,
                             "min_dur": args.get("min_dur"),
                             "max_parts": args.get("max_parts")})
        out["split"] = sp[2]
    au = _lip_auto(wb, {"dry_run": dry, "overwrite": overwrite,
                        "gain": args.get("gain")})
    out["auto"] = au[2]

    if dry:
        return True, "[试算] 口型重算方案已生成（未改工程）", out
    after = _lip_cover(wb, {"quiet": True})
    a = (after[2] or {}).get("total") or {}
    out["after"] = a
    msg = "口型重算完成：覆盖率 %s -> %s，嘴动帧 %d -> %d" % (
        "%.1f%%" % (b.get("cover_pct") or 0.0),
        "%.1f%%" % (a.get("cover_pct") or 0.0),
        int(b.get("nonzero") or 0), int(a.get("nonzero") or 0))
    return True, msg, out


def _lip_cover(wb, args):
    """口型覆盖率查询：嘴在「有语音的时长」里到底动了几成。

    args:
      clip_id: 只查这个片段；留空 = 当前幕全部立绘
      min_pct: 只列覆盖率低于此值的问题片段（默认 0 = 全列）
    覆盖率 = 语音窗口内非零开口帧 / 语音窗口总帧（与导出 LIPWARN 同口径）。
    """
    VM = _vm()
    proj = getattr(wb, "proj", None) or {}
    aid = _aid(wb)
    quiet = bool(args.get("quiet"))
    cid = str(args.get("clip_id") or "").strip()
    try:
        min_pct = float(args.get("min_pct") or 0.0)
    except (TypeError, ValueError):
        min_pct = 0.0

    rows, bad = [], 0
    tf = tnz = tvf = 0
    for tr, c in _figures(VM, proj, aid):
        if cid and str(c.get("id") or "") != cid:
            continue
        cov = _cover_of(proj, c, aid, tr)
        if cov.get("error"):
            rows.append({"clip_id": str(c.get("id") or ""),
                         "error": cov["error"]})
            continue
        tf += int(cov.get("frames") or 0)
        tnz += int(cov.get("nonzero") or 0)
        tvf += int(cov.get("voice_frames") or 0)
        pct = cov.get("cover_pct")
        if pct is None:
            pct = 0.0
        if cov.get("voice_frames") and pct < 50.0:
            bad += 1
        if min_pct and pct >= min_pct:
            continue
        rows.append({
            "clip_id": cov.get("clip_id"), "start": cov.get("start"),
            "dur": cov.get("dur"), "mode": cov.get("mode"),
            "voices": cov.get("voices"), "frames": cov.get("frames"),
            "nonzero": cov.get("nonzero"),
            "voice_frames": cov.get("voice_frames"),
            "cover_pct": pct,
        })

    total = {"frames": tf, "nonzero": tnz, "voice_frames": tvf,
             "cover_pct": (round(float(tnz) * 100.0 / tvf, 1) if tvf else 0.0),
             "cover_of_all_pct": (round(float(tnz) * 100.0 / tf, 1)
                                  if tf else 0.0)}
    if quiet:
        return True, "", {"clips": rows, "total": total}
    msg = "口型覆盖率 %.1f%%（语音窗口 %d 帧 / 嘴动 %d 帧），低于 50%% 的片段 %d 个" % (
        total["cover_pct"], tvf, tnz, bad)
    return True, msg, {"clips": rows, "total": total}


# ---------------- 注册表 ----------------
HANDLERS = {
    "lip.status": _lip_status,
    "lip.list": _lip_list,
    "lip.bind": _lip_bind,
    "lip.unbind": _lip_unbind,
    "lip.auto": _lip_auto,
    "lip.split": _lip_split,
    "lip.recalc": _lip_recalc,
    "lip.cover": _lip_cover,
}


def run(wb, cmd, args):
    """按名字执行口型命令。返回 (ok, msg, data)。

    未知命令返回 (False, ...)，不抛。
    """
    fn = HANDLERS.get(str(cmd or ""))
    if fn is None:
        return False, "未知口型命令: %s" % cmd, None
    try:
        return fn(wb, dict(args or {}))
    except Exception as e:
        return False, "口型命令异常: %s: %s" % (type(e).__name__, e), None