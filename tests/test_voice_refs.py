# -*- coding: utf-8 -*-
"""参考音频库的回归测试。

覆盖：
  · 参考音频按角色分目录、按「说话内容」命名
  · 导入 / 校验 / 删除 / 挑选
  · 角色名归一化（简繁、前后缀、最长匹配）
  · RIFF 头解析（PCM + IEEE float 都要能读 —— 32-bit float 是常见格式，
    Python 的 wave 模块读不了，踩过）
  · 素材目录与引擎目录解耦（引擎没装时素材不该「消失」）
  · DSH 命令接线
"""
import io
import os
import shutil
import sys
import tempfile
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


TMP = tempfile.mkdtemp(prefix="refs_test_")
os.environ["MUPPET_VOICE_DIR"] = TMP
# 隔离用户数据目录：voice_refs._all_bases() 总会把它算进去，
# 而那里面有**真的**参考音频（导入到 %APPDATA% 的那 14 段），
# 不隔离的话测试会数到别人的素材。
_APPDATA = os.environ.get("APPDATA", "")
os.environ["APPDATA"] = os.path.join(TMP, "appdata")
os.makedirs(os.environ["APPDATA"], exist_ok=True)

import voice_refs as VR


def make_wav(path, dur=4.0, sr=32000, ch=1, bits=16, fmt=1):
    """造一个 wav（fmt=3 是 32-bit float，wave 模块读不了那种）。"""
    import struct
    frames = int(dur * sr)
    bps = bits // 8
    if fmt == 3:
        data = struct.pack("<%df" % (frames * ch),
                           *([0.1] * (frames * ch)))
    elif bits == 8:
        data = struct.pack("<%dB" % (frames * ch),
                           *([128] * (frames * ch)))
    else:
        data = struct.pack("<%dh" % (frames * ch),
                           *([1600] * (frames * ch)))

    def chunk(tag, body):
        # 标准 RIFF 块：tag(4) + size(4) + data(size)
        # **没有 CRC** —— 我一开始多写了一个 4 字节 CRC，解析器按标准
        # 布局读就会错位 4 字节（fmt 块读完指到 data 块头前 4 字节，
        # 于是把 data 的 size 读成了垃圾）。真实 WAV 也没有 CRC。
        return tag + struct.pack("<I", len(body)) + body

    hdr = b"RIFF" + struct.pack("<I", 4 + (8 + 16) + (8 + len(data))) \
        + b"WAVE"
    fmt_body = struct.pack("<HHIIHH", fmt, ch, sr, sr * ch * bps,
                           ch * bps, bits)
    if fmt == 0xFFFE:
        fmt_body += struct.pack("<H", 22) + b"\x00" * 22
        fmt_body = fmt_body[:16] + struct.pack("<H", fmt) + fmt_body[18:]
    out = hdr + chunk(b"fmt ", fmt_body) + chunk(b"data", data)
    with open(path, "wb") as f:
        f.write(out)
    return path


print("== RIFF 头解析（wave 模块读不了的格式也要能读）")
for fmt, bits, label in ((1, 16, "PCM 16bit"), (1, 8, "PCM 8bit"),
                         (3, 32, "IEEE float 32bit")):
    p = make_wav(os.path.join(TMP, "t_%d.wav" % fmt), dur=2.5, fmt=fmt,
                 bits=bits)
    info = VR._wav_info(p)
    chk(info is not None, "%s 能解析出信息" % label)
    if info:
        chk(abs(info["dur"] - 2.5) < 0.05,
            "%s 时长对：%.2f" % (label, info["dur"]))
        chk(info["sr"] == 32000, "%s 采样率对" % label)
    try:
        wave.open(p, "rb")
        w_ok = True
    except Exception:
        w_ok = False
    if fmt == 3:
        chk(not w_ok, "（32-bit float 确实读不了 wave 模块 —— 这就是要自己解析的原因）")

print()
print("== 导入 / 列表 / 挑选 / 删除")
src = make_wav(os.path.join(TMP, "src1.wav"), dur=4.0)
ok, msg, info = VR.import_ref(src, "千早爱音", "你又进入蟑螂模式了")
chk(ok, "导入成功：%s" % msg)
chk(info and info["name"].endswith(".wav"), "文件名带扩展名")
chk("你又进入蟑螂模式了" in (info or {}).get("name", ""),
    "按说话内容命名：%s" % (info or {}).get("name"))

ok2, msg2, info2 = VR.import_ref(src, "千早爱音", "你又进入蟑螂模式了")
chk(ok2 and info2["name"] != info["name"],
    "重名不覆盖，自动加序号：%s" % (info2 or {}).get("name"))

chk("千早爱音" in VR.roles(), "角色出现在列表里")
lst = VR.refs_for("千早爱音")
chk(len(lst) == 2, "该角色有 2 段：%d" % len(lst))
chk(all(x["dur"] > 0 for x in lst), "每段都有时长")

picked = VR.pick_ref("千早爱音")
chk(picked and os.path.isfile(picked), "pick_ref 选到了存在的文件")
chk(VR.pick_ref("千早爱音", lst[1]["name"]) == lst[1]["path"],
    "pick_ref 能指定段")

ok3, msg3 = VR.remove_ref("千早爱音", lst[0]["name"])
chk(ok3, "删除成功：%s" % msg3)
chk(len(VR.refs_for("千早爱音")) == 1, "删完剩 1 段")

print()
print("== 校验")
short = make_wav(os.path.join(TMP, "short.wav"), dur=0.3)
ok4, msg4, _ = VR.validate_ref(short)
chk(not ok4, "太短被拒：%s" % msg4)
ok5, msg5, _ = VR.validate_ref(os.path.join(TMP, "不存在.wav"))
chk(not ok5, "不存在被拒")
notwav = os.path.join(TMP, "x.mp3")
open(notwav, "wb").write(b"xx")
ok6, msg6, _ = VR.validate_ref(notwav)
chk(not ok6, "非 wav 被拒")

print()
print("== 角色名归一化")
chk(VR.normalize_role("MyGO_千早爱音_v2pp") == "千早爱音",
    "去前缀和版本后缀")
chk(VR.normalize_role("Mujica_豊川祥子_白_v2pp") == "丰川祥子白",
    "繁 -> 简（内置表）")
chk(VR.normalize_role("MyGO_长崎素世_夹_v2pp") == "长崎素世夹",
    "_夹 是身份的一部分，不能剥")

cands = ["丰川祥子", "千早爱音", "长崎素世", "长崎素世_夹", "高松灯"]
chk(VR.match_role(cands, "MyGO_长崎素世_夹_v2pp") == "长崎素世_夹",
    "最长匹配：夹子音优先于普通")
chk(VR.match_role(cands, "Mujica_豊川祥子_白_v2pp") == "丰川祥子",
    "简繁匹配")
chk(VR.match_role(cands, "MyGO_千早爱音_v2pp") == "千早爱音",
    "完全匹配")

print()
print("== 素材目录与引擎目录解耦")
# 引擎目录没有 s1v3.ckpt 标志 -> ensure_voice 认不出引擎
chk(not os.path.isfile(os.path.join(TMP, "GPT_SoVITS", "pretrained_models",
                                    "s1v3.ckpt")),
    "当前目录不是有效引擎目录")
chk(bool(VR.refs_root()), "refs_root 仍然给得出路径：%s" % VR.refs_root())
chk(len(VR.refs_for("千早爱音")) == 1,
    "引擎没装时素材照样能读到（不消失）")

print()
print("== 选定（跨会话记住，合成会用它）")
lst2 = VR.refs_for("千早爱音")
chk(len(lst2) >= 1, "有可选的段")
if lst2:
    target = lst2[0]["name"]
    chk(VR.selection_of("千早爱音") == "", "初始没有已选")
    ok_s, msg_s, info_s = VR.select_ref("千早爱音", target)
    chk(ok_s, "选定成功：%s" % msg_s)
    chk(VR.selection_of("千早爱音") == target,
        "selection_of 记住了：%s" % VR.selection_of("千早爱音"))
    chk(VR.pick_ref("千早爱音").endswith(target),
        "pick_ref 优先用已选")
    chk("千早爱音" in VR.selections(), "selections() 里有这个角色")

    ok_e, msg_e, _ = VR.select_ref("千早爱音", "根本不存在的.wav")
    chk(not ok_e, "选不存在的段被拒：%s" % msg_e)

    ok_c, msg_c, data_c = VR.clear_selection("千早爱音")
    chk(ok_c, "清除成功：%s" % msg_c)
    chk(VR.selection_of("千早爱音") == "", "清除后 selection_of 为空")
    chk(VR.pick_ref("千早爱音").endswith(lst2[0]["name"]),
        "清除后回到第一段")

    # 指定 name 的优先级高于「已选」
    if len(lst2) >= 2:
        VR.select_ref("千早爱音", lst2[0]["name"])
        chk(VR.pick_ref("千早爱音", lst2[1]["name"]).endswith(lst2[1]["name"]),
            "显式指定 name 的优先级高于已选")
        VR.clear_selection("千早爱音")

print()
print("== prompt_text 推导（参考音频里说的那句话）")
# 传空 prompt_text 会让引擎降级 naive_infer，参考音频的文本韵律不参与对齐，
# 合成容易吞字。我们的参考音频就是用说话内容命名的，所以从文件名推导。
chk(VR.prompt_of(r"C:\x\refs\千早爱音\怎么了怎么了，性感的我让你害羞了吗.wav")
    == "怎么了怎么了，性感的我让你害羞了吗", "正常命名能推出内容")
chk(VR.prompt_of(r"C:\x\refs\千早爱音\你好，我是千早爱音.wav")
    == "你好，我是千早爱音", "中文标点保留")
chk(VR.prompt_of(r"C:\x\ref.wav") == "",
    "引擎自带的 ref.wav 推不出（预期降级）")
chk(VR.prompt_of(r"C:\x\refs\a\050_182.42s_4.2s.wav") == "",
    "编号+时间戳命名推不出（预期降级）")
chk(VR.prompt_of(r"C:\x\refs\a\001_006.75s_8.8s.wav") == "",
    "纯编号命名推不出")
chk(VR.prompt_of("") == "", "空路径返回空")

# prompt_for_role 两者配套返回
VP2, VPT2 = VR.prompt_for_role("千早爱音")
chk(VP2 and VPT2, "prompt_for_role 同时给路径和文本")
chk(VPT2 == os.path.splitext(os.path.basename(VP2))[0],
    "文本就是文件名（去掉扩展名）")

print()
print("== synth_line 把 prompt_text 传下去")
import voice_synth as VS
_src = io.open(os.path.join(ROOT, "voice_synth.py"), encoding="utf-8").read()
chk("prompt_text=None" in _src, "synth_line 有 prompt_text 参数")
chk("VR.prompt_of(ref)" in _src, "默认从参考音频推导")
chk('"prompt_text": pt,' in _src, "请求体用 pt 而不是空串")
chk("prompt_text_ok" in _src, "info 里带 prompt_text_ok")
chk("降级" in _src or "naive" in _src, "降级时有提示文案")

print()
print("== require_prompt：DSH 联动必须有 prompt_text，不能静默降级")
# 造一段「名字推不出文本」的参考音频（模拟 ref.wav）
_nr = os.path.join(TMP, "refs", "测试角色")
os.makedirs(_nr, exist_ok=True)
_bad = make_wav(os.path.join(_nr, "001_002.5s_3.0s.wav"), dur=3.5)
_good = make_wav(os.path.join(_nr, "今天天气很好.wav"), dur=3.5)

_ok, _p, _pt, _msg = VR.require_prompt("测试角色", "001_002.5s_3.0s.wav")
chk(not _ok, "编号命名 -> require_prompt 拒绝")
chk("prompt_text" in _msg, "拒绝时说明了原因：%s" % _msg[:50])
chk(_p, "但仍给出路径（方便排查）")

_ok2, _p2, _pt2, _msg2 = VR.require_prompt("测试角色", "今天天气很好.wav")
chk(_ok2, "内容命名 -> require_prompt 通过")
chk(_pt2 == "今天天气很好", "prompt 推导对：%r" % _pt2)

_ok3, _p3, _pt3, _msg3 = VR.require_prompt(
    "测试角色", "001_002.5s_3.0s.wav", override="我自己写的文本")
chk(_ok3 and _pt3 == "我自己写的文本",
    "显式 override 能救回编号命名的段")

# refs_for 每条都带 prompt_ok，AI 能看出哪段能选
for _x in VR.refs_for("测试角色"):
    chk("prompt_ok" in _x, "refs_for 条目带 prompt_ok：%s" % _x["name"])

print()
print("== DSH 命令接线")
import voice_commands as VC
for c in ("voice.refs", "voice.refs.add", "voice.refs.rm",
          "voice.refs.pick", "voice.refs.sel", "voice.refs.unsel"):
    chk(c in VC.HANDLERS, "命令已注册：%s" % c)
ok7, msg7, data7 = VC.run("voice.refs", {})
chk(ok7, "voice.refs 可用：%s" % msg7)
chk("selected" in (data7 or {}), "voice.refs 返回 selected 表")
_roles7 = (data7 or {}).get("roles") or []
if _roles7:
    chk("selected" in _roles7[0], "每个角色条目带 selected 字段")
ok8, msg8, data8 = VC.run("voice.refs.pick", {"role": "千早爱音"})
chk(ok8, "voice.refs.pick 可用：%s" % msg8)
# sel / unsel 行为
ok_s2, msg_s2, data_s2 = VC.run("voice.refs.sel",
                                {"role": "千早爱音", "name": lst2[0]["name"]})
chk(ok_s2, "voice.refs.sel 可用：%s" % msg_s2)
chk(VR.selection_of("千早爱音") == lst2[0]["name"], "sel 写入了记忆")
_pk = VC.run("voice.refs.pick", {"role": "千早爱音"})
chk((_pk[2] or {}).get("is_selected") is True, "pick 报告 is_selected")
ok_u, msg_u, _ = VC.run("voice.refs.unsel", {"role": "千早爱音"})
chk(ok_u, "voice.refs.unsel 可用：%s" % msg_u)
chk(VR.selection_of("千早爱音") == "", "unsel 清掉了记忆")
ok_m, msg_m, _ = VC.run("voice.refs.sel", {"role": "千早爱音"})
chk(not ok_m, "sel 缺 name 被拒")

# **DSH 强制**：选定一段配不出 prompt_text 的，必须拒绝
_ok_bad, _msg_bad, _d_bad = VC.run(
    "voice.refs.sel", {"role": "测试角色", "name": "001_002.5s_3.0s.wav"})
chk(not _ok_bad, "sel 目标配不出 prompt_text -> 拒绝")
chk("prompt_text" in (_msg_bad or ""),
    "拒绝原因说清了：%s" % (_msg_bad or "")[:60])

# pick 要告诉 AI 这段能不能用（usable）
_ok_pk, _msg_pk, _d_pk = VC.run("voice.refs.pick", {"role": "千早爱音"})
chk((_d_pk or {}).get("usable") is True, "pick 报告 usable=True")
chk("prompt_text" in (_d_pk or {}), "pick 带 prompt_text")

ok9, msg9, _ = VC.run("voice.refs.pick", {"role": "根本没有这个角色"})
chk(not ok9, "不存在的角色 -> False 并说明原因")
chk("ref.wav" in (msg9 or "") or "没有参考音频" in (msg9 or ""),
    "提示说清楚了：%s" % (msg9 or "")[:60])

import remote_control as RC
chk("voice.refs" in RC._HANDLERS, "remote_control 已转发 voice.refs")
chk("voice.refs.sel" in RC._HANDLERS, "remote_control 转发 voice.refs.sel")
chk("voice.refs.unsel" in RC._HANDLERS, "remote_control 转发 voice.refs.unsel")

print()
print("== 语音合成按角色选参考音频")
import voice_synth as VS
got, note = VS._pick_ref_for(role="千早爱音")
chk(got and "千早爱音" in got, "按角色选中该角色的参考音频")
got2, note2 = VS._pick_ref_for(role="不存在的角色")
if got2:
    chk(got2.endswith("ref.wav"), "没有时回退到引擎 ref.wav")
    chk("串" in note2 or "ref.wav" in note2, "回退时提示音色会串")
else:
    chk("没有参考音频" in note2, "没有时明确报错")

if _APPDATA:
    os.environ["APPDATA"] = _APPDATA
shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("参考音频库全部检查通过")