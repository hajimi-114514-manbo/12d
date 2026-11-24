# -*- coding: utf-8 -*-
"""参考音频库：GPT-SoVITS 克隆音色用的「样本音频」。

参考音频是什么
--------------
GPT-SoVITS 是**克隆式** TTS：音色权重（ckpt+pth）决定「像谁」，而
**参考音频**决定「这一次用哪一段话来克隆」。没有参考音频就出不了声
（引擎报 `ref_audio_path is required`）。

之前的做法很粗：只用引擎自带的 `ref.wav`，或者 `voices/` 目录里的
第一个 wav —— 换角色音色时参考音频还是别人的，克隆出来的音色会串。

现在参考音频是一等公民：按**角色**分目录存放，一个角色可以有多段，
合成时按角色挑一段。

放在哪
------
<语音引擎根>/refs/<角色名>/<说话内容>.wav

文件名用**说话内容**（用户的要求）—— 「怎么了怎么了，性感的我让你
害羞了吗.wav」这种。好处：
  · 试听时一眼知道这段是什么
  · 参考音频通常要配 prompt_text（参考音频里说的那句字），
    文件名就是那句话，不用另记
  · 段号命名（001_xxs_ys.wav）看不出内容，重名也没法排查

为什么不做成「音色的一部分」
--------------------------
参考音频和音色权重是**两码事**：
  · 权重是「这个角色的声音」，一份就够（CC-BY-NC，不可二次配布）
  · 参考音频是「用哪段话克隆」，可以有很多段
而且参考音频的来源更杂（用户自己从动画里分离的、官方提供的），
版权口径可能不同，混在一起不好管。

对外 API：
    refs_dir()                    -> 参考音频根目录
    roles()                      -> ["千早爱音", ...] 有参考音频的角色
    refs_for(role)               -> [{"name","path","dur","size"}]
    find_ref(role, name)         -> dict|None
    import_ref(src, role, name) -> (ok, msg)
    remove_ref(role, name)       -> (ok, msg)
    pick_ref(role, name="")      -> str  给某个角色挑一段参考（合成用）
    select_ref(role, name)       -> (ok,msg,info)  **选定**该角色用哪一段
    clear_selection(role="")     -> (ok,msg,data)  清掉已选（留空=全部）
    selection_of(role)           -> str  该角色已选的文件名
    selections()                 -> dict 全部已选
    stats()                      -> 统计
"""
import contextlib
import hashlib
import io
import os
import re
import shutil

import voice_paths as VP

# Windows 文件名非法字符
_BAD_CHARS = '<>:"/\\|?*'
# 参考音频时长下限（太短的克隆不出稳定音色；GPT-SoVITS 官方建议 3~10 秒）
_MIN_DUR = 1.0
# 上限（太长会让 /tts 变慢，且超过 30 秒引擎会自己切）
_MAX_DUR = 30.0


def _clean(name):
    """把任意字符串变成可用作文件名的名字（保留中文）。"""
    s = str(name or "").strip()
    for c in _BAD_CHARS:
        s = s.replace(c, "_")
    s = re.sub(r"\s+", " ", s)
    return s.rstrip(". ")


def _voice_base(root=None):
    """语音素材的根目录（参考音频与音色权重共用）。

    优先级：
      1. 显式传入的 root
      2. MUPPET_VOICE_DIR 环境变量
      3. 引擎目录（ensure_voice）—— **只在引擎已装时有效**
      4. 用户数据目录 %APPDATA%/MuppetStudio/voice（兜底）

    为什么要有第 4 步：参考音频是**素材**，用户可能先导入参考音频、
    后装引擎。如果 refs_root 依赖 ensure_voice()，引擎没装时
    refs_root() 返回 ""，用户导入的参考音频就「消失」了。
    素材和工具应该解耦 —— 引擎是「工具」，参考音频是「素材」。
    """
    if root:
        return root
    env = os.environ.get("MUPPET_VOICE_DIR", "").strip()
    if env:
        return env
    # voice_dir() 返回第一个候选（优先 %APPDATA%\MuppetStudio\voice），
    # **不要求引擎已装** —— 这正是我们要的（素材先于工具）。
    try:
        d = VP.voice_dir(scan=False) or ""
        if d:
            return d
    except Exception:
        pass
    return os.path.join(os.path.expandvars(r"%APPDATA%"),
                        "MuppetStudio", "voice")


def _all_bases(root=None):
    """所有可能的素材根目录（按优先级）。

    参考音频可能落在两个地方：
      1. 引擎目录下的 refs/（跟引擎走 —— 这是主位置）
      2. 用户数据目录下的 voice\refs（引擎没装时的兜底）

    两个都收，是为了**迁移期不丢东西**：用户先在没引擎的机器上导了参考音频，
    后来装了引擎，素材位置变了，但不该「看不见」。
    """
    out = []
    if root:
        out.append(root)
    else:
        env = os.environ.get("MUPPET_VOICE_DIR", "").strip()
        if env:
            out.append(os.path.normpath(env))
        try:
            eng = VP.ensure_voice(scan=False) or ""
            if eng:
                out.append(eng)
        except Exception:
            pass
    # 用户数据目录**始终**要算进去 —— MUPPET_VOICE_DIR 硬限定了引擎位置，
    # 但素材可能已经落在用户数据目录（引擎没装时导入的）。漏掉它，
    # 设了 MUPPET_VOICE_DIR 的用户会突然「素材全没了」。
    try:
        appdir = os.path.join(os.path.expandvars(r"%APPDATA%"),
                              "MuppetStudio")
        out.append(os.path.join(appdir, "voice"))
    except Exception:
        pass
    seen = set()
    uniq = []
    for b in out:
        if b and b not in seen:
            seen.add(b)
            uniq.append(b)
    return uniq


def refs_root(root=None):
    """参考音频根目录：<voice>/refs（主位置）。

    写入用这个；读取用 refs_roots()（两个位置都读）。
    """
    base = _voice_base(root)
    if not base:
        return ""
    return os.path.join(base, "refs")


def refs_roots(root=None):
    """所有可读的参考音频根目录。"""
    out = []
    for b in _all_bases(root):
        d = os.path.join(b, "refs")
        if os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def _role_dir(role, root=None):
    d = refs_root(root)
    if not d:
        return ""
    return os.path.join(d, _clean(role))


def _wav_info(path):
    """读 wav 的时长/采样率/声道；读不出返回 None。

    **自己解析 RIFF 头，不用 wave 模块** —— wave 只认 PCM（format 1），
    遇到 32-bit 浮点（format 3）直接抛 wave.Error('unknown format: 3')。
    而 32-bit float WAV 是很常见的（ffmpeg 默认输出、K重音轨切片、
    很多切片工具都这样）。踩过：千早爱音的参考音频就是这个格式，
    用 wave 读会让整批参考音频「读不出来」。

    支持 format 1（PCM）和 format 3（IEEE float），以及 WAVE_FORMAT_EXTENSIBLE
    （0xFFFE，按子格式判断）。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return None
            fmt = None
            data_size = None
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid = hdr[:4]
                try:
                    csz = int.from_bytes(hdr[4:8], "little")
                except Exception:
                    break
                if cid == b"fmt ":
                    body = f.read(csz)
                    if len(body) >= 16:
                        tag, ch, sr, br, ba, bits = \
                            int.from_bytes(body[0:2], "little"), \
                            int.from_bytes(body[2:4], "little"), \
                            int.from_bytes(body[4:8], "little"), \
                            int.from_bytes(body[8:12], "little"), \
                            int.from_bytes(body[12:14], "little"), \
                            int.from_bytes(body[14:16], "little")
                        # WAVE_FORMAT_EXTENSIBLE: 真实格式在 SubFormat 前两字节
                        if tag == 0xFFFE and len(body) >= 26:
                            tag = int.from_bytes(body[24:26], "little")
                        fmt = {"tag": tag, "ch": ch, "sr": sr, "bits": bits}
                elif cid == b"data":
                    data_size = csz
                    break
                else:
                    f.seek(csz + (csz & 1), 1)
            if not fmt or not data_size or fmt["sr"] <= 0:
                return None
            bps = max(1, fmt["bits"] // 8)
            frames = data_size // (bps * max(1, fmt["ch"]))
            return {"dur": frames / float(fmt["sr"]), "sr": fmt["sr"],
                    "ch": fmt["ch"], "bits": fmt["bits"], "tag": fmt["tag"]}
    except Exception:
        return None


def roles(root=None):
    """有参考音频的角色名列表（所有根目录合并）。"""
    out = []
    for d in refs_roots(root):
        try:
            for n in sorted(os.listdir(d)):
                p = os.path.join(d, n)
                if os.path.isdir(p) and n not in out:
                    wavs = [f for f in os.listdir(p)
                            if f.lower().endswith(".wav")]
                    if wavs:
                        out.append(n)
        except Exception:
            pass
    return out


def refs_for(role, root=None):
    """某角色的参考音频列表（所有根目录合并）。

    返回 [{"name","path","dur","size","sr","ch"}]，按名字排序。
    """
    out = []
    seen = set()
    for base in _all_bases(root):
        d = os.path.join(base, "refs", _clean(role))
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except Exception:
            continue
        for n in names:
            if not n.lower().endswith(".wav"):
                continue
            p = os.path.join(d, n)
            if p in seen:
                continue
            seen.add(p)
            # 按**内容**去重，不按路径 —— 同一段素材可能同时存在于
            # 引擎目录和用户数据目录（迁移期、或用户手动拷了一份）。
            # 按路径去重会把它算两遍（统计翻倍、列表出现两条一样的）。
            try:
                fp = fingerprint(p)
            except Exception:
                fp = p
            key = (n, fp) if fp else p
            if key in seen:
                continue
            seen.add(key)
            info = _wav_info(p) or {"dur": 0.0, "sr": 0, "ch": 0, "bits": 0}
            e = {"name": n, "path": p, "dur": round(info["dur"], 3),
                 "size": os.path.getsize(p), "sr": info["sr"],
                 "ch": info["ch"], "bits": info["bits"]}
            # prompt_text 一起给出来 —— DSH 联动要求「必须有」，
            # AI 得能从列表里看出哪段配不出文本（不能选那些）
            e["prompt_text"] = prompt_of(p)
            e["prompt_ok"] = bool(e["prompt_text"])
            out.append(e)
    return out


def find_ref(role, name, root=None):
    """按名字找某角色的一段参考音频。"""
    for r in refs_for(role, root):
        if r["name"] == name or r["name"] == _clean(name) + ".wav":
            return r
    return None


def validate_ref(path):
    """检查一段参考音频能不能用。返回 (ok, msg, info)。"""
    p = str(path or "")
    if not p or not os.path.isfile(p):
        return False, "文件不存在", None
    if not p.lower().endswith(".wav"):
        return False, "不是 wav 文件", None
    info = _wav_info(p)
    if not info:
        return False, "wav 读不出来（格式不对或已损坏）", None
    if info["dur"] < _MIN_DUR:
        return False, ("太短了（%.2f 秒，至少要 %.0f 秒）—— "
                       "参考音频太短克隆不出稳定音色"
                       % (info["dur"], _MIN_DUR)), info
    if info["dur"] > _MAX_DUR:
        return False, ("太长了（%.1f 秒，超过 %.0f 秒）—— "
                       "建议切到 30 秒以内" % (info["dur"], _MAX_DUR)), info
    return True, "%.2f 秒 / %dHz / %d 声道" % (
        info["dur"], info["sr"], info["ch"]), info


def import_ref(src, role, name="", root=None):
    """导入一段参考音频到 <voice>/refs/<角色>/。

    name 留空时用源文件名（去掉扩展名）—— 源文件名通常就是说话内容。

    返回 (ok, msg, info)。
    """
    src = str(src or "")
    role = _clean(role)
    if not role:
        return False, "角色名不能为空", None
    ok, msg, info = validate_ref(src)
    if not ok:
        return False, msg, None

    d = _role_dir(role, root)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        return False, "无法创建参考音频目录：%s" % e, None

    stem = _clean(name or os.path.splitext(os.path.basename(src))[0])
    if not stem:
        return False, "名字不能为空", None
    dst = os.path.join(d, stem + ".wav")
    # 重名不覆盖 —— 参考音频常有同一句话的多个版本
    if os.path.exists(dst):
        i = 2
        while os.path.exists(os.path.join(d, "%s_%d.wav" % (stem, i))):
            i += 1
        dst = os.path.join(d, "%s_%d.wav" % (stem, i))
    try:
        shutil.copy2(src, dst)
    except Exception as e:
        return False, "复制失败：%s" % e, None

    return True, "已导入 %s（%.2f 秒）" % (os.path.basename(dst), info["dur"]), {
        "role": role, "name": os.path.basename(dst), "path": dst,
        "dur": round(info["dur"], 3), "sr": info["sr"], "ch": info["ch"],
    }


def remove_ref(role, name, root=None):
    """删除一段参考音频。"""
    r = find_ref(role, name, root)
    if not r:
        return False, "找不到这段参考音频"
    try:
        os.remove(r["path"])
    except Exception as e:
        return False, "删除失败：%s" % e
    # 角色目录空了顺手删掉，别留空壳
    d = _role_dir(role, root)
    with contextlib.suppress(Exception):
        if d and os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    return True, "已删除 %s" % r["name"]


# ---- 已选的参考音频（跨会话记住）---------------------------------------
# <voice>/refs/_selected.json  ->  {"角色": "文件名.wav"}
# 为什么要记：用户对同一个角色会反复用同一段（哪段清楚、哪段带杂音，
# 试一次就知道了），每次都从列表里挑太烦。DSH 联动和 UI 共用这份口径。

_SEL_NAME = "_selected.json"


def _sel_path(root=None):
    d = refs_root(root)
    return os.path.join(d, _SEL_NAME) if d else ""


def _load_selection(root=None):
    """读已选表；文件坏了就当空的（不抛）。"""
    p = _sel_path(root)
    if not p or not os.path.isfile(p):
        return {}
    try:
        import json
        d = json.load(io.open(p, encoding="utf-8"))
        return dict(d) if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_selection(d, root=None):
    """写已选表。"""
    p = _sel_path(root)
    if not p:
        return False, "找不到参考音频目录"
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        import json
        io.open(p, "w", encoding="utf-8").write(
            json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True))
        return True, "已保存"
    except Exception as e:
        return False, "保存失败：%s" % e


def select_ref(role, name, root=None):
    """把某个角色的参考音频「选定」为 name。

    name 必须是该角色**已有**的一段（防止选到不存在的）。
    """
    role = _clean(role)
    if not role:
        return False, "角色名不能为空", None
    name = str(name or "").strip()
    if not name:
        return False, "缺少参考音频文件名", None
    r = find_ref(role, name, root)
    if not r:
        return False, "角色「%s」没有这段参考音频：%s" % (role, name), None
    d = _load_selection(root)
    d[role] = r["name"]
    ok, msg = _save_selection(d, root)
    if not ok:
        return False, msg, None
    return True, "已选定 %s / %s（%.2f 秒）" % (role, r["name"], r["dur"]), {
        "role": role, "name": r["name"], "path": r["path"],
        "dur": r["dur"],
    }


def clear_selection(role, root=None):
    """清掉某个角色的已选（回到「取第一段」的兜底行为）。

    role 留空 -> 清掉全部。
    """
    role = _clean(role)
    d = _load_selection(root)
    if not role:
        if not d:
            return True, "本来就没有已选项", {"cleared": []}
        cleared = sorted(d.keys())
        ok, msg = _save_selection({}, root)
        return ok, ("已清空 %d 个角色的选择" % len(cleared)) if ok else msg,             {"cleared": cleared}
    if role not in d:
        return True, "角色「%s」本来就没有已选项" % role, {"cleared": []}
    d.pop(role, None)
    ok, msg = _save_selection(d, root)
    return ok, ("已清掉 %s 的选择" % role) if ok else msg, {"cleared": [role]}


def selection_of(role, root=None):
    """某角色已选的参考音频文件名（没选返回 ""）。"""
    return str(_load_selection(root).get(_clean(role)) or "")


def selections(root=None):
    """全部已选表 {"角色": "文件名"}。"""
    return dict(_load_selection(root))


def pick_ref(role, name="", root=None):
    """给某个角色挑一段参考音频（合成时用）。

    顺序：
      1. 显式传入的 name（调用方明确指定，最高优先）
      2. **该角色已选的**（select_ref 记下来的，跨会话保持）
      3. 该角色的第一段（按名字排序）
      4. "" —— 调用方自己决定报错还是回退

    「已选」为什么要有：用户会反复用同一个角色，每次都从 3 段里挑太烦；
    而且选哪段对克隆效果影响很大（有的段清楚、有的段带背景音）。
    记下来之后，DSH 联动和 UI 都能用同一个口径。

    **回退是有代价的**：引擎的 ref.wav 是别的角色的声音，用它克隆会串音色。
    所以调用方拿到 "" 时应该明确报错，而不是静默用别人的声音。
    """
    if name:
        r = find_ref(role, name, root)
        if r:
            return r["path"]
        # 指定了但找不到 —— 不静默换别的，让调用方知道
        return ""
    sel = selection_of(role, root)
    if sel:
        r = find_ref(role, sel, root)
        if r:
            return r["path"]
    lst = refs_for(role, root)
    if lst:
        return lst[0]["path"]
    return ""


def fallback_ref(root=None):
    """引擎自带的 ref.wav（最后的退路）。

    注意：这个 ref.wav 是**凑友希娜**的声音（引擎自带的通用参考）——
    拿它克隆别的角色会串音色，所以只在实在没有该角色参考音频时用，
    而且调用方要把这件事告诉用户。
    """
    # ref.wav 在**引擎目录**里，不在素材目录里 —— 两者是不同的东西：
    #   素材目录 refs/  : %APPDATA%\MuppetStudio\voice\refs（用户导入的）
    #   引擎目录        : 装了引擎的那一层（引擎自带 ref.wav）
    # 之前用 _voice_base() 找错了地方，导致「回退到引擎 ref.wav」永远失败。
    bases = []
    if root:
        bases.append(root)
    try:
        eng = VP.ensure_voice(scan=False) or ""
        if eng:
            bases.append(eng)
    except Exception:
        pass
    bases.append(_voice_base(root))
    for b in bases:
        if not b:
            continue
        p = os.path.join(b, "ref.wav")
        if os.path.isfile(p):
            return p
    return ""


def stats(root=None):
    """统计。"""
    d = refs_root(root)
    out = {"root": d or "", "roots": refs_roots(root),
           "roles": 0, "count": 0, "dur": 0.0, "size": 0}
    for role in roles(root):
        out["roles"] += 1
        for r in refs_for(role, root):
            out["count"] += 1
            out["dur"] += r["dur"]
            out["size"] += r["size"]
    out["dur"] = round(out["dur"], 2)
    return out


def fingerprint(path):
    """文件 sha256（用于去重/校验）。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                h.update(b)
    except OSError:
        return ""
    return h.hexdigest()


# ---- 角色名归一化 -------------------------------------------------------
# 音色名和参考音频目录里的角色名**写法可能不一样**：
#   音色      Mujica_豊川祥子_白_v2pp   （丰川祥子 = 日文原字）
#   参考音频  丰川祥子                （简体中文）
# 所以要归一化后再比。
#
# 归一化做三件事：
#   1. 去前后缀（MyGO_ / Mujica_ / _v2pp / _白 / _黒 / _夹 …）
#   2. 简繁统一（用 opencc，引擎自带；没有就退回原样）
#   3. 去掉所有非字母数字汉字的字符
#
# 匹配时用**最长匹配**：角色「长崎素世_夹」必须优先于「长崎素世」，
# 否则夹子音的参考音频会被普通的那份抢走。

# 只剥**版本号**后缀。_白/_黒/_夹 是**服装/版本区分**，属于角色身份的一部分，
# 不能剥 —— 否则「长崎素世_夹」和「长崎素世」归一化后一样，
# 最长匹配就分不出夹子音那份参考音频了。
_SFX = ("_v2proplus", "_v2pp", "_v2", "_v3", "_v4", "_v1")
_PREFIX = ("mygo_", "mujica_", "ave_", "roselia_", "afterglow_",
           "ppp_", "hhw_", "ras_", "pasupare_", "monika_")

# 简繁对照（只覆盖本作会用到的角色名）。
# 不用 opencc —— 开发机的 Python 3.12 没装，而引擎 runtime 里的 opencc
# 只在**引擎进程**里可用（工作室进程读不到）。一张小表够用，
# 真遇到表外的字就退回「原样 + 双向都试」。
_T2S = {
    "豊": "丰", "川": "川", "祥": "祥", "子": "子",
    "愛": "爱", "早": "早", "千": "千",
    "長": "长", "崎": "崎", "素": "素", "世": "世",
    "椎": "椎", "名": "名", "立": "立", "希": "希",
    "高": "高", "松": "松", "灯": "灯",
    "初": "初", "華": "华", "三": "三", "角": "角",
    "八": "八", "幡": "幡", "海": "海", "鈴": "铃",
    "若": "若", "葉": "叶", "睦": "睦",
    "祐": "祐", "天": "天", "寺": "寺", "若": "若", "麥": "麦",
    "要": "要", "楽": "乐", "奈": "奈",
}


def _t2s(s):
    """简 -> 繁 的反向（繁 -> 简）。没在表里的字原样保留。"""
    return "".join(_T2S.get(ch, ch) for ch in s)


def normalize_role(name):
    """把角色名归一化，用于跨来源匹配。

    "Mujica_豊川祥子_白_v2pp" -> "丰川祥子白"
    "丰川祥子"               -> "丰川祥子"
    """
    s = str(name or "").strip()
    if not s:
        return ""
    low = s.lower()
    for pre in _PREFIX:
        if low.startswith(pre):
            s = s[len(pre):]
            low = s.lower()
            break
    for suf in _SFX:
        if low.endswith(suf):
            s = s[:-len(suf)]
            low = s.lower()
            break
    s = _t2s(s)
    # 只保留字母数字和汉字
    s = "".join(ch for ch in s if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return s.lower()


def match_role(candidates, target):
    """在候选角色名里找出最匹配 target 的那个。

    candidates: ["千早爱音", "长崎素世", "长崎素世_夹", ...]
    target:     "长崎素世_夹"

    规则：
      1. 归一化后完全相等 -> 命中
      2. 归一化后**互为前缀**，取更长的那个（长崎素世_夹 优先于 长崎素世）
      3. 否则不匹配
    """
    t = normalize_role(target)
    if not t:
        return None
    best = None
    for c in candidates:
        cn = normalize_role(c)
        if not cn:
            continue
        if cn == t:
            return c
        if cn.startswith(t) or t.startswith(cn):
            if best is None or len(cn) > len(normalize_role(best)):
                best = c
    return best


def role_index(refs=None):
    """建立「参考音频角色」索引，供跨来源匹配。

    返回 {"归一化名": 原始角色名}
    """
    import voice_refs as _self
    out = {}
    for r in (_self.roles() if refs is None else refs):
        n = normalize_role(r)
        if n and n not in out:
            out[n] = r
    return out


# ---- prompt_text（参考音频里说的那句话）--------------------------------
# 为什么必须填
# ------------
# GPT-SoVITS 的 /tts 有个 prompt_text 参数，它是**参考音频里说的那句话**。
# 传空会让引擎降级到 naive_infer（日志里那句
# "Warning: Prompt free is not supported batch_infer! switch to naive_infer"），
# 参考音频的文本韵律不参与对齐，合成容易吞字、口齿不清。
#
# 为什么能从文件名推出来
# ----------------------
# 我们的参考音频**就是用说话内容命名的**（voice_refs 的设计约定），
# 所以文件名去掉扩展名就是那段话本身，不用再单独存一份 prompt_text。
#
# 为什么不用另存 json
# -------------------
# 单独存「文件名 -> 文本」的映射会有一致性问题（改名了就不同步）。
# 文件名即文本，只有一份真相。
#
# 什么情况推不出来
# ----------------
#   · 引擎自带的 ref.wav（名字是 ref，不代表任何话）-> 返回 ""
#   · 用户用纯编号命名的段（001_xxx.wav）-> 返回 ""（调用方要知道这是降级）

# 文件名里不该进 prompt_text 的东西：编号前缀、时间戳后缀
_BAD_NAME = re.compile(
    r"^(\d{2,3}[_-])|"                      # 开头编号 001_
    r"([_-]\d+\.?\d*s.*$)|"                # 结尾 _182.42s_4.2s
    r"(\.wav$)",                             # 扩展名
    re.I)


def prompt_of(path):
    """从参考音频路径推出 prompt_text（那段话本身）。

    推不出来返回 ""（调用方要按「降级到 naive_infer」处理并告诉用户）。
    """
    p = str(path or "")
    if not p:
        return ""
    name = os.path.splitext(os.path.basename(p))[0].strip()
    if not name:
        return ""
    # 引擎自带的通用参考音频：名字不代表内容
    if name.lower() in ("ref", "reference", "ref_audio"):
        return ""
    # 纯编号 / 时间戳命名 -> 不可用
    if _BAD_NAME.search(os.path.basename(p)):
        cleaned = _BAD_NAME.sub("", os.path.basename(p))
        cleaned = os.path.splitext(cleaned)[0].strip("_- ")
        if not cleaned or cleaned.isdigit():
            return ""
        return cleaned
    return name


def prompt_for_role(role, name="", root=None):
    """给某角色挑参考音频，并返回 (路径, prompt_text)。

    两者配套返回，避免调用方只拿路径却忘了 prompt_text。
    """
    p = pick_ref(role, name, root)
    return p, prompt_of(p) if p else ""


def require_prompt(role, name="", root=None, override=""):
    """严格版：**必须**有 prompt_text，否则拒绝。

    DSH 联动（AI 控制）用这个，而不是 prompt_for_role —— 因为 AI 看不见
    听感，一旦静默降级成 naive_infer，它只会觉得「输出好像有点怪」，
    没法自己发现是参考音频的问题。宁可报错让它换个参考音频。

    返回 (ok, path, prompt_text, msg)：
      ok=True  ->  path 和 prompt_text 都可用
      ok=False ->  msg 说明差什么（path 可能仍非空，但 prompt_text 缺失）

    override 显式传了文本时以它为准（AI 可以从 voice.refs 的
    `prompt_text` 字段取，或自己写）。
    """
    p = pick_ref(role, name, root)
    if not p:
        return False, "", "", ("角色「%s」没有参考音频" % role)
    pt = str(override or "").strip() or prompt_of(p)
    if not pt:
        return False, p, "", (
            "参考音频「%s」推不出 prompt_text（文件名不是说话内容）。"
            "DSH 联动要求必须提供 prompt_text —— 请改用按内容命名的参考音频，"
            "或在 args.prompt_text 里显式给出「参考音频里说的那句话」。"
            % os.path.basename(p))
    return True, p, pt, "ok"
