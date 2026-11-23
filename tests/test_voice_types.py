# -*- coding: utf-8 -*-
"""类型重构的回归测试（v3.6.2 极客化）。

要守住三件事：
  1. ProbeResult / LocalStatus **同时**支持属性访问和 dict 访问
     —— 这是「不破坏既有调用方」的前提
  2. 枚举值能正常 JSON 序列化（DSH 联动回包要能发出去）
  3. 键名一个都没变（老工程、老脚本、老 DSH 调用全靠它）
"""
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT)

import voice_probe as VP
import voice_commands as VC

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


print("== ProbeResult：属性 + dict 两种访问")
r = VP.ProbeResult(ok=True, host="h", port=1, tcp=True, is_api=True,
                    chatty=True, msg="m", detail={"a": 1})
chk(r.ok is True, "属性访问 r.ok")
chk(r["ok"] is True, "dict 访问 r['ok']")
chk(r.get("msg") == "m", "get() 访问")
chk(len(r) == 8, "len() = 8")
chk(sorted(r) == sorted(["ok", "host", "port", "tcp", "is_api", "chatty",
                         "msg", "detail"]), "迭代出全部键")
try:
    r["不存在的键"]
    chk(False, "不存在的键应抛 KeyError")
except KeyError:
    chk(True, "不存在的键抛 KeyError")

print()
print("== LocalStatus：同样两种访问")
ls = VP.LocalStatus(engine=True, python="py", api="api", dir="d",
                     msg="m")
chk(ls.engine is True, "属性访问")
chk(ls["engine"] is True, "dict 访问")
chk(ls.get("dir") == "d", "get() 访问")
chk(len(ls) == 6, "len() = 6")

print()
print("== 键名没变（老调用方靠它）")
old_keys = {"ok", "host", "port", "tcp", "is_api", "chatty", "msg", "detail"}
chk(set(VP.ProbeResult().to_dict()) == old_keys, "ProbeResult 键集不变：%s"
    % sorted(VP.ProbeResult().to_dict()))
old_ls = {"engine", "python", "api", "dir", "service", "msg"}
chk(set(VP.LocalStatus().to_dict()) == old_ls, "LocalStatus 键集不变：%s"
    % sorted(VP.LocalStatus().to_dict()))

print()
print("== JSON 序列化（DSH 联动回包）")
s = json.dumps(r.to_dict(), ensure_ascii=False)
chk('"ok": true' in s, "可序列化：%s" % s[:60])
chk(json.loads(s)["msg"] == "m", "往返后 msg 正确")

print()
print("== 枚举：str 混入，JSON 里是普通字符串")
chk(json.dumps({"lang": VP.Lang.ZH}) == '{"lang": "zh"}',
    "Lang.ZH 序列化成 \"zh\"")
chk(json.dumps({"s": VP.VoiceSide.GPT}) == '{"s": "ckpt"}',
    "VoiceSide.GPT 序列化成 \"ckpt\"")
chk(str(VP.Lang.JA) == "ja", "str(Lang.JA) = 'ja'")

print()
print("== 端点常量")
chk(VP.EP_SET_GPT == "/set_gpt_weights", "EP_SET_GPT")
chk(VP.EP_SET_SOVITS == "/set_sovits_weights", "EP_SET_SOVITS")
chk(VP.EP_CONTROL == "/control", "EP_CONTROL")

print()
print("== 指纹常量与实际响应对应")
# 真实服务的响应文案必须都在指纹表里，否则探测会误判「不是语音服务」
for fp in ("command is required", "gpt weight path is required",
           "sovits weight path is required", "ref_audio_path is required"):
    chk(any(fp in f for f in VP._FINGERPRINTS), "指纹含 %r" % fp)

print()
print("== voice_commands 仍能用（老接口）")
ok, msg, data = VC.run("voice.list", {})
chk(ok is True, "voice.list 正常：%s" % msg)
chk(isinstance(data, dict), "data 是 dict（DSH 要 JSON 序列化）")

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("类型重构回归全部通过")