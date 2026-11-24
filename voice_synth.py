# -*- coding: utf-8 -*-
"""台词级语音合成与试听（v3.6.2，需求「语音轴与角色台词轴相对齐」）。

这一层负责「一句话 -> 一个 wav 文件」，产物落在工程目录的 vocal/ 下，
台词项的 voice 字段记录它（path + 实测时长 + 用的音色）。

分层理由
--------
· 合成要发 HTTP 到语音服务（阻塞几秒），**不能**在 UI 线程做
· 音色切换、批量合成是同一套逻辑，UI 只是调用方
· 以后做 V3「台词批量合成」直接复用这里

与工程的关系
------------
    <工程目录>/vocal/<act>_<序号>_<谁>.wav    合成产物
    dialogue[i]["voice"] = {"path": "vocal/xxx.wav", "dur": 1.94, ...}

导出时（需求池【语音V4】）把 vocal/ 拷进 WebGAL 包，start.txt 里每句
台词前 emit vocal:xxx.wav。**本模块只管生成，不管导出**。

对外 API：
    synth_line(proj_dir, act, index, who, text, voice_stem, host, port,
               timeout) -> (ok, msg, info)
    voice_path_of(proj, dialogue_item, act=None) -> str 绝对路径（不存在返回 ""）
    play(path) -> bool     试听（QMediaPlayer，UI 层用）
"""
import io
import json
import os
import re
import urllib.parse
import urllib.request

# 合成服务默认地址（与 voice_probe.DEFAULT_* 一致）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9880

# 语种与音色侧别收进枚举（voice_probe.Lang / voice_probe.VoiceSide）：
# 以前 "zh"/"ckpt"/"pth" 这些字面量散在好几个文件里，改一次要找半天。
try:
    from voice_probe import Lang, VoiceSide
except Exception:                      # 极端情况：探���模块不可用
    class _Lang(str):
        ZH = "zh"
        JA = "ja"
        EN = "en"
        AUTO = "auto"
    Lang = _Lang()

    class _Side(str):
        GPT = "ckpt"
        SOVITS = "pth"
    VoiceSide = _Side()

# 试听播放器（模块级单例，理由见 play()）
_PLAYER = None
_OUTPUT = None


def _safe_name(s):
    """把任意字符串变成安全的文件名片段。"""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(s or ""))
    s = s.strip().strip(".")
    s = re.sub(r"\s+", "_", s)
    return s or "x"


def _ext_for(who):
    """按角色名猜扩展名：日文角色名 -> 日文句，其余中文。

    猜不准就让用户改（这不是静默出错的地方，会在 UI 上标出来）。
    判据：名字里含日文假名/汉字读音标记就当 ja。
    """
    w = str(who or "")
    # 平假名 / 片假名 -> 日文
    if re.search(r"[\u3040-\u309f\u30a0-\u30ff]", w):
        return Lang.JA
    return Lang.ZH


def _vocal_dir(proj_dir):
    d = os.path.join(str(proj_dir or ""), "vocal")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def voice_file(proj_dir, act, index, who):
    """合成产物的绝对路径（<工程>/vocal/<act>_<序号>_<谁>.wav）。

    文件名带 act + 序号，保证同一工程内多幕多句不重名。
    """
    d = _vocal_dir(proj_dir)
    return os.path.join(d, "%s_%03d_%s.wav" % (_safe_name(act or "a0"),
                                               int(index),
                                               _safe_name(who or "旁白")))


def synth_line(proj_dir, act, index, who, text, host=None, port=None,
               timeout=300.0, text_lang=None, prompt_lang=None,
               ref_role=None, ref_name=""):
    """合成一句话，落盘到工程 vocal/。返回 (ok, msg, info)。

    info = {"path": 相对工程目录的路径, "dur": 实测时长, "file": 绝对路径}

    **必须先切音色** —— 服务默认加载的是 weight.json 里那对，用户选的音色
    要通过 /set_gpt_weights + /set_sovits_weights 换过去。这里不做换音色
    （那是上层的事，voice_commands / UI 负责），只负责合成当前服务已加载的音色。
    """
    host = str(host or DEFAULT_HOST)
    port = int(port or DEFAULT_PORT)
    text = str(text or "").strip()
    if not text:
        return False, "台词内容为空", None
    if not os.path.isdir(str(proj_dir or "")):
        return False, "工程目录不存在", None

    # 参考音频：按**角色**从参考音频库里挑一段（voice_refs）。
    # 为什么不能一律用引擎的 ref.wav —— 那是别的角色的声音，拿它克隆会串音色。
    ref, ref_note = _pick_ref_for(role=ref_role, name=ref_name)
    if not ref:
        return False, ("找不到参考音频 —— 合成必须有参考音频。"
                       + (ref_note or "请检查语音引擎安装是否完整")), None

    lang = text_lang or _ext_for(who)
    body = {
        "text": text,
        "text_lang": lang,
        "ref_audio_path": ref,
        "prompt_lang": prompt_lang or lang,
        "prompt_text": "",
        "top_k": 15, "top_p": 1, "temperature": 1,
        "text_split_method": "cut5", "batch_size": 1,
        "speed_factor": 1.0, "seed": 42,
        "media_type": "wav", "streaming_mode": False,
    }
    req = urllib.request.Request(
        "http://%s:%d/tts" % (host, port),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(300).decode("utf-8", "replace")
        except Exception:
            detail = ""
        return False, "合成失败（HTTP %s）%s" % (e.code, detail), None
    except Exception as e:
        return False, "合成请求失败：%s" % e, None

    if not raw or raw[:4] != b"RIFF":
        return False, "合成返回的不是 wav（可能是错误信息）", None

    path = voice_file(proj_dir, act, index, who)
    try:
        with open(path, "wb") as f:
            f.write(raw)
    except Exception as e:
        return False, "写入失败：%s" % e, None

    dur = _wav_duration(raw)
    rel = os.path.relpath(path, str(proj_dir)).replace("\\", "/")
    return True, "已合成 %.2fs" % dur, {"path": rel, "dur": round(dur, 3),
                                       "file": path}


def _wav_duration(raw):
    """从 wav 字节算时长（秒）；失败返回 0.0。"""
    try:
        import wave
        with wave.open(io.BytesIO(raw), "rb") as w:
            sr = w.getframerate()
            nf = w.getnframes()
            return (nf / float(sr)) if sr else 0.0
    except Exception:
        return 0.0


def _pick_ref_for(role=None, name=""):
    """给某个角色挑一段参考音频。返回 (路径, 提示语)。

    顺序：
      1. 参考音频库里该角色的指定段 / 第一段（voice_refs）
      2. 引擎自带的 ref.wav（退路 —— 但那是别的角色的声音，会串音色，
         所以提示语要说清楚）
      3. 都没有 -> ("", 提示)
    """
    role = str(role or "").strip()
    try:
        import voice_refs as VR
        if role:
            p = VR.pick_ref(role, name)
            if p:
                return p, ""
            fb = VR.fallback_ref()
            if fb:
                return fb, ("角色「%s」没有参考音频，已退回引擎自带 ref.wav"
                            "（音色会串，建议先导入该角色的参考音频）" % role)
            return "", ("角色「%s」没有参考音频，也没有引擎自带的 ref.wav"
                        "（请在音色库里导入该角色的参考音频）" % role)
        # 没指定角色：老行为（引擎 ref.wav）
        p = _find_ref_audio()
        if p:
            return p, ""
        return "", "找不到参考音频（ref.wav）—— 合成必须有参考音频"
    except Exception as e:
        p = _find_ref_audio()
        if p:
            return p, ""
        return "", "查找参考音频失败：%s" % e


def _find_ref_audio():
    """找参考音频：引擎自带的 ref.wav（角色音色目录里的 *.wav 也可）。

    找不到返回 ""。合成必须有参考音频 —— GPT-SoVITS 是「克隆音色」，
    没有参考就出不了声。
    """
    # ref.wav 在**引擎目录**里；引擎没装时先看素材目录（refs/）。
    roots = []
    try:
        import voice_paths as VP
        eng = VP.ensure_voice(scan=False) or ""
        if eng:
            roots.append(eng)
    except Exception:
        pass
    try:
        import voice_refs as VR
        roots.append(VR._voice_base())
    except Exception:
        pass
    if not roots:
        return ""
    root = roots[0]
    cands = [os.path.join(root, "ref.wav")]
    vd = VP.voices_dir(root)
    if vd and os.path.isdir(vd):
        for n in sorted(os.listdir(vd)):
            if n.lower().endswith(".wav"):
                cands.append(os.path.join(vd, n))
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


def voice_path_of(proj_dir, item, act=None):
    """台词项的 voice 引用 -> 绝对路径；没有/不存在返回 ""。"""
    if not isinstance(item, dict):
        return ""
    v = item.get("voice")
    if not isinstance(v, dict):
        return ""
    rel = str(v.get("path") or "").strip()
    if not rel:
        return ""
    p = os.path.join(str(proj_dir or ""), rel.replace("/", os.sep))
    return p if os.path.isfile(p) else ""


def play(path):
    """试听一个音频文件（QMediaPlayer）。成功返回 True。

    播放器是**模块级单例** —— QMediaPlayer 反复 new 会占资源，而且切歌要
    能打断上一段。
    """
    global _PLAYER
    try:
        if not path or not os.path.isfile(path):
            return False
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    except Exception:
        return False
    try:
        if _PLAYER is None:
            _PLAYER = QMediaPlayer()
            _OUTPUT = QAudioOutput()
            _PLAYER.setAudioOutput(_OUTPUT)
        _PLAYER.setSource(QUrl.fromLocalFile(str(path)))
        _PLAYER.play()
        return True
    except Exception:
        return False


def stop_play():
    """停止试听（幂等）。"""
    global _PLAYER
    try:
        if _PLAYER is not None:
            _PLAYER.stop()
    except Exception:
        pass


def clear_voice(proj_dir, item):
    """清掉台词的语音引用，并删掉产物文件。返回 (ok, msg)。"""
    if not isinstance(item, dict):
        return False, "台词项无效"
    p = voice_path_of(proj_dir, item)
    item.pop("voice", None)
    if p:
        try:
            os.remove(p)
        except Exception:
            pass
    return True, "已清除语音"