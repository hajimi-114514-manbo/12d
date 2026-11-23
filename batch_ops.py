# -*- coding: utf-8 -*-
"""批量脚本操作（batch_ops.py）——纯函数，无 Qt 无 IO。

对标对方工具的「批量操作」工具条。挑的都是**纯数据变换、可独立测试、
不依赖引擎**的实用项，而不是把所有花哨功能都塞进来：

  · 一键 ID 补全      —— clip/轨道 id 缺失或重复时补齐，修掉「同 id 打架」
  · 批量加 -next      —— 连续台词串成「播完自动继续」，不用逐句点
  · 自动离场          —— 角色台词结束后自动清场（收尾不留残影）
  · 批量台词时长      —— 一句话读不完/太快，统一按字数重算 dur
  · 批量滤镜          —— 给选中的 clip/整幕套同一组滤镜参数

## 为什么不放进 video_model

这些是**编辑器批处理动作**（一次改一批、可撤销），不是工程数据模型本身。
放 video_model 会让那个纯数据层长出「批量策略」，也会污染它 1400 行的
核心职责。独立一层 + 独立测试更干净。

## 统一约定

所有函数都遵守：
  1. **入参不改**：在副本上操作（调用方拿返回值替换），便于撤销
  2. **幂等优先**：跑两遍结果一样（-next 不会叠两层、id 补全不重复加后缀）
  3. **脏数据容错**：非 dict / 缺字段一律跳过，绝不抛
  4. 返回 (新数据, 变更统计 dict)，统计供 UI 汇报「改了几处」
"""

import copy
import re

# 台词最短/最长时长（秒）——与 video_model.MIN_DUR 对齐的下限，上限防呆
MIN_DUR = 0.05
MAX_DUR = 120.0

# 中文按字数估时长的经验值：每秒约 5.5 字（含标点，偏保守）
CHARS_PER_SEC = 5.5


def _num(v, d=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return d
    if f != f or f in (float("inf"), float("-inf")): return d
    return f


def _clone(obj):
    try:
        return copy.deepcopy(obj)
    except Exception:
        return obj


# ---------------- 1. 一键 ID 补全 ----------------
def fix_ids(proj, aid=None):
    """给 clip / 轨道补全缺失或重复的 id，保证「clip id 全工程唯一、
    轨道 id 幕内唯一」（这是 v3.5.0 定下的约定，重复会让 find_clip 命中
    错误的幕/轨）。

    实现要点：**两趟**。
      第一趟统计每个 id 出现次数（先看清全局有哪些冲突）；
      第二趟按「某个 id 只出现一次」才保留，其余全部换新编号。
    一趟边扫边判会把「自己」也算进冲突里，永远判不清（第一版就错在这）。

    返回 (新 proj, {"clips_fixed": n, "tracks_fixed": n})。
    """
    p = _clone(proj)
    n_clip = 0
    n_track = 0

    # 把 v1/v2 两种形状统一成「幕列表」，免得两处各写一遍
    views = []
    if isinstance(p.get("acts"), list) and p.get("acts"):
        views = [a for a in p["acts"] if isinstance(a, dict)]
    else: views = [p]

    # ---- 第一趟：统计 clip id 频次（全工程，跨幕唯一） ----
    clip_freq = {}
    for view in views:
        for tr in (view.get("tracks") or []):
            if not isinstance(tr, dict): continue
            for c in (tr.get("clips") or []):
                if isinstance(c, dict) and c.get("id"):
                    cid = str(c["id"])
                    clip_freq[cid] = clip_freq.get(cid, 0) + 1
    # 保留所有已存在的 id，新 id 不能撞上去
    taken = set(clip_freq.keys())

    def _fresh_clip_id():
        i = 1
        while True:
            cand = "c%d" % i
            if cand not in taken:
                taken.add(cand)
                return cand
            i += 1

    # ---- 第二趟：轨道 id（幕内唯一）+ clip id（全工程唯一） ----
    for view in views:
        seen_track = set()
        for tr in (view.get("tracks") or []):
            if not isinstance(tr, dict): continue
            tid = str(tr.get("id") or "")
            if not tid or tid in seen_track:
                base = tid or "t"
                k = 1
                cand = "%s%d" % (base, k)
                while cand in seen_track:
                    k += 1
                    cand = "%s%d" % (base, k)
                tr["id"] = cand
                n_track += 1
            seen_track.add(str(tr.get("id")))
            for c in (tr.get("clips") or []):
                if not isinstance(c, dict): continue
                cid = str(c.get("id") or "")
                if not cid or clip_freq.get(cid, 0) > 1:
                    c["id"] = _fresh_clip_id()
                    n_clip += 1
    return p, {"clips_fixed": n_clip, "tracks_fixed": n_track}


# ---------------- 2. 批量加 -next ----------------
def add_next_to_dialogue(proj, aid=None, only_continuous=True,
                         min_gap=0.05, max_gap=2.5):
    """给**连续**台词追加 `-next` 参数（播完自动进下一句）。

    WebGAL 语义：`-next` 让当前句播完自动继续；不带就是等玩家点击。
    批量加的场景是「一段连续的快速对话」——逐句点太累。

    only_continuous=True 时只处理「与下一句间隔 <= max_gap 秒」的句子，
    避免把「故意停下来等玩家」的停顿也变成自动推进。
    **max_gap 默认 2.5 秒**：实测正常对话的句间留白常在 1~2 秒（上一句
    读完、下一句还没起），定 0.5 秒会把绝大部分连续对话判成"不连续"，
    批量功能形同虚设。想更严格传小值即可。
    幂等：已经有 -next 的不重复加。

    返回 (新 proj, {"updated": n})
    """
    p = _clone(proj)
    n = 0
    views = []
    if isinstance(p.get("acts"), list) and p.get("acts"):
        views = [a for a in p["acts"] if isinstance(a, dict)]
    else: views = [p]

    for view in views:
        dlg = view.get("dialogue")
        if not isinstance(dlg, list): continue
        # 按时刻排序后判断间隔（原列表可能乱序）
        items = [(i, d) for i, d in enumerate(dlg)
                 if isinstance(d, dict) and isinstance(d.get("text"), str)]
        items.sort(key=lambda x: _num(x[1].get("t"), 0.0))
        for k, (idx, d) in enumerate(items):
            if "-next" in str(d.get("text") or ""): continue            # 幂等
            if only_continuous:
                if k + 1 >= len(items): continue        # 最后一句不加
                t0 = _num(d.get("t"), 0.0)
                t1 = _num(items[k + 1][1].get("t"), 0.0)
                gap = t1 - (t0 + _num(d.get("dur"), 0.0))
                if gap < -min_gap or gap > max_gap: continue
            d["text"] = str(d.get("text") or "").rstrip() + " -next"
            n += 1
    return p, {"updated": n}


# ---------------- 3. 自动离场 ----------------
def auto_exit_figures(proj, aid=None):
    """让「该角色最后一句台词之后」的立绘 clip 提前收尾（自动离场）。

    场景：一段戏里某角色说完最后一句就该退场，但 clip 铺到轨道末尾，
    导出后人物会杵在那儿不动。这里把该角色最后一句台词时刻之后的
    FIG-<角色> clip 终点收到「最后一句结束处 + 余量」。

    判定口径与导出一致：台词 who 与轨道名（FIG-<角色>）匹配。
    幂等：已经收过的不会继续收（只在 clip 终点确实超过目标时才动）。

    返回 (新 proj, {"tracks_touched": n, "clips_trimmed": n})
    """
    p = _clone(proj)
    n_clip = 0
    n_tr = 0
    views = []
    if isinstance(p.get("acts"), list) and p.get("acts"):
        views = [a for a in p["acts"] if isinstance(a, dict)]
    else: views = [p]

    LINGER = 0.3     # 收尾余量（秒）：留一点缓冲，别切得太生硬

    for view in views:
        dlg = [d for d in (view.get("dialogue") or [])
               if isinstance(d, dict) and isinstance(d.get("who"), str)]
        if not dlg: continue
        # 每个角色的「最后一句结束时刻」
        last_end = {}
        for d in dlg:
            who = str(d.get("who") or "").strip()
            if not who: continue
            end = _num(d.get("t"), 0.0) + _num(d.get("dur"), 0.0)
            if who not in last_end or end > last_end[who]: last_end[who] = end
        for tr in (view.get("tracks") or []):
            if not isinstance(tr, dict) or tr.get("kind") != "video": continue
            name = str(tr.get("name") or "")
            m = re.match(r"^FIG[-_](.+)$", name)
            if not m: continue
            who = m.group(1)
            target_end = last_end.get(who)
            if target_end is None: continue            # 该角色没台词（可能全程无对白的立绘），不动
            touched = False
            for c in (tr.get("clips") or []):
                if not isinstance(c, dict): continue
                start = _num(c.get("start"), 0.0)
                dur = _num(c.get("dur"), 0.0)
                end = start + dur
                want = target_end + LINGER
                if end > want + 0.05 and want > start + MIN_DUR:
                    c["dur"] = round(want - start, 3)
                    n_clip += 1
                    touched = True
            if touched: n_tr += 1
    return p, {"tracks_touched": n_tr, "clips_trimmed": n_clip}


# ---------------- 4. 批量台词时长 ----------------
def retime_dialogue(proj, aid=None, chars_per_sec=CHARS_PER_SEC,
                    min_dur=0.8, keep_longer=True):
    """按字数重算每句台词时长（读得完、不赶）。

    keep_longer=True 时**只延长不缩短**——已有的长句可能是故意留白，
    自动缩短会破坏节奏。

    返回 (新 proj, {"updated": n})
    """
    p = _clone(proj)
    n = 0
    cps = max(1.0, _num(chars_per_sec, CHARS_PER_SEC))
    views = []
    if isinstance(p.get("acts"), list) and p.get("acts"):
        views = [a for a in p["acts"] if isinstance(a, dict)]
    else: views = [p]

    for view in views:
        for d in (view.get("dialogue") or []):
            if not isinstance(d, dict): continue
            text = str(d.get("text") or "")
            # 去掉参数（-next 等）再数字数，避免「-next」被算进朗读时长
            spoken = re.sub(r"\s-\w[\w-]*(=\S+)?", "", text).strip()
            if not spoken: continue
            need = max(min_dur, len(spoken) / cps)
            need = min(MAX_DUR, need)
            cur = _num(d.get("dur"), 0.0)
            if keep_longer and cur >= need - 1e-6: continue
            d["dur"] = round(need, 3)
            n += 1
    return p, {"updated": n}


# ---------------- 5. 批量滤镜 ----------------
def apply_filter_batch(proj, filt, clip_ids=None, tracks=None, aid=None,
                       which="all"):
    """给一批 clip 套同一组滤镜参数。

    filt: 参数字典（如 {"brightness": 0.8, "bloom": 0.5}）；None = 清除
    clip_ids: 指定 clip id 列表；None = 用 tracks/which 决定范围
    tracks: 轨道名列表（如 ["FIG-素世"]）；None = 全部
    which: "all" / "fig" / "bg" —— 按轨道类型粗筛

    返回 (新 proj, {"updated": n})
    """
    p = _clone(proj)
    n = 0
    ids = set(str(x) for x in (clip_ids or []))
    tnames = set(str(x) for x in (tracks or []))
    views = []
    if isinstance(p.get("acts"), list) and p.get("acts"):
        views = [a for a in p["acts"] if isinstance(a, dict)]
    else: views = [p]

    for view in views:
        for tr in (view.get("tracks") or []):
            if not isinstance(tr, dict) or tr.get("kind") != "video": continue
            nm = str(tr.get("name") or "")
            if tnames and nm not in tnames: continue
            if which == "fig" and not nm.upper().startswith("FIG"): continue
            if which == "bg" and nm.upper().startswith("FIG"): continue
            for c in (tr.get("clips") or []):
                if not isinstance(c, dict): continue
                if ids and str(c.get("id") or "") not in ids: continue
                if filt is None:
                    if "filter" in c:
                        c.pop("filter", None)
                        n += 1
                else:
                    c["filter"] = dict(filt)
                    n += 1
    return p, {"updated": n}


# ---------------- 汇总：一次跑完常用组合 ----------------
def run_preset(proj, name, aid=None, **kw):
    """按预设名跑一个批处理。返回 (proj, stats, 人类可读说明)。"""
    if name == "fix_ids":
        p, st = fix_ids(proj, aid=aid)
        return p, st, "补全 %d 个 clip id、%d 个轨道 id" % (
            st["clips_fixed"], st["tracks_fixed"])
    if name == "add_next":
        p, st = add_next_to_dialogue(proj, aid=aid, **kw)
        return p, st, "为 %d 句连续台词加上 -next" % st["updated"]
    if name == "auto_exit":
        p, st = auto_exit_figures(proj, aid=aid)
        return p, st, "收尾 %d 条立绘轨（%d 个 clip）" % (
            st["tracks_touched"], st["clips_trimmed"])
    if name == "retime":
        p, st = retime_dialogue(proj, aid=aid, **kw)
        return p, st, "按字数调整 %d 句台词时长" % st["updated"]
    return proj, {}, "未知的批处理：%s" % name