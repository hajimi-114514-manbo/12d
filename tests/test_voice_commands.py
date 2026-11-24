# -*- coding: utf-8 -*-
"""语音命令的 DSH 联动测试：真起 HTTP 服务、真发请求。

要验的：
  · 命令能被路由到（不是「未知命令」）
  · 走的是 HTTP 线程直跑（不排队、不卡 UI）
  · 返回结构统一（ok/msg/data）
  · 写操作真的生效（导入音色 -> 能查到 -> 删掉）
  · 错误参数给明确报错，不崩
"""
def _free_port():
    """挑一个空闲端口（避开默认 8765，可能被用户的木偶占着）。"""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request

# ---- 测试引导：向上找项目根（与 tests/ 里既有测试一致）----
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="voice_cmd_test_")
ENG = os.path.join(TMP, "voice")
os.makedirs(os.path.join(ENG, "GPT_SoVITS", "pretrained_models"), exist_ok=True)
os.makedirs(os.path.join(ENG, "voices"), exist_ok=True)
with open(os.path.join(ENG, "GPT_SoVITS", "pretrained_models", "s1v3.ckpt"),
          "wb") as f:
    f.write(b"x" * 1024)
os.environ["MUPPET_VOICE_DIR"] = ENG

# DSH 联动测试必须用独立端口：默认 8765 可能被**用户正在运行的
# 木偶工作室**占着（它的 DSH 服务在监听），测试的请求会打到
# 那个进程上，拿到它的环境而不是测试的 —— 症状是断言「引擎未就绪」
# 而直接调用明明是「就绪」。这里挑一个空闲端口互不干扰。
os.environ["MUPPET_REMOTE_PORT"] = str(_free_port())

import remote_control as RC
import voice_commands as VC

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


print("== 命令表注册")
chk(len(VC.HANDLERS) == 11, "11 个语音命令：%s" % sorted(VC.HANDLERS))
    for _c in ("voice.refs", "voice.refs.add", "voice.refs.rm",
               "voice.refs.pick"):
        chk(_c in VC.HANDLERS, "参考音频命令已注册：%s" % _c)
for name in VC.HANDLERS:
    chk(name in RC._HANDLERS, "%s 已注册到 remote_control" % name)
    chk(name.startswith("voice."), "%s 命名空间正确" % name)

print()
print("== 不该存在的命令（V2/V3 还没做）")
for bad in ("voice.map", "voice.synth", "voice.batch"):
    chk(bad not in RC._HANDLERS, "%s 未注册（还没实现，不该出现）" % bad)

print()
print("== 未知命令不崩")
ok, msg, data = VC.run("voice.nonexistent", {})
chk(ok is False, "未知命令 ok=False")
chk("未知" in msg, "有明确提示：%s" % msg)

print()
print("== 直接调用各读命令")
ok, msg, data = VC.run("voice.status", {})
chk(isinstance(ok, bool), "voice.status 返回 bool")
chk(data and "engine_ready" in data, "有 engine_ready 字段")
print("     ", msg)

ok, msg, data = VC.run("voice.list", {})
chk(ok is True, "voice.list 成功：%s" % msg)
chk(data and "voices" in data, "有 voices 列表")

ok, msg, data = VC.run("voice.stats", {})
chk(ok is True, "voice.stats 成功：%s" % msg)

print()
print("== 导入 -> 列出 -> 删除 全链路")
d = os.path.join(TMP, "src")
os.makedirs(d, exist_ok=True)
ck = os.path.join(d, "DSH测试音色.ckpt")
pt = os.path.join(d, "DSH测试音色.pth")
with open(ck, "wb") as f:
    f.write(b"c" * (60 * 1048576))
with open(pt, "wb") as f:
    f.write(b"p" * (60 * 1048576))

ok, msg, data = VC.run("voice.validate", {"ckpt": ck, "pth": pt})
chk(ok is True, "validate 通过：%s" % msg)

ok, msg, data = VC.run("voice.import", {"ckpt": ck, "pth": pt})
chk(ok is True, "import 成功：%s" % msg)
stem = (data or {}).get("stem") or ""
chk(stem == "DSH测试音色", "返回 stem：%s" % stem)

ok, msg, data = VC.run("voice.list", {})
names = [v["stem"] for v in (data or {}).get("voices", [])]
chk("DSH测试音色" in names, "列表里能查到：%s" % names)

ok, msg, data = VC.run("voice.remove", {"stem": "DSH测试音色"})
chk(ok is True, "remove 成功：%s" % msg)
ok, msg, data = VC.run("voice.list", {})
chk("DSH测试音色" not in [v["stem"] for v in (data or {}).get("voices", [])],
    "删干净了")

print()
print("== 错误参数不崩")
for args in ({}, {"ckpt": ck}, {"ckpt": "不存在.ckpt", "pth": pt}):
    ok, msg, data = VC.run("voice.validate", args)
    chk(ok is False, "validate 缺参/错参 -> False：%s" % msg[:50])
ok, msg, data = VC.run("voice.remove", {})
chk(ok is False, "remove 缺 stem -> False")

print()
print("== HTTP 端到端（真起 DSH 联动服务）")
port = RC.start(object())          # 传个假 wb：语音命令不需要它
print("     服务端口:", port)


def post(cmd, args=None):
    body = json.dumps({"cmd": cmd, "args": args or {}}).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%d/cmd" % port,
                                 data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


try:
    st, res = post("voice.status")
    chk(st == 200, "HTTP 200")
    chk(res.get("ok") is True, "voice.status ok=True：%s" % res.get("msg"))
    chk("data" in res and res["data"], "有 data")

    st, res = post("voice.list")
    chk(st == 200 and res.get("ok") is True, "voice.list ok：%s" % res.get("msg"))

    st, res = post("voice.probe", {"host": "127.0.0.1", "port": 1,
                                  "timeout": 1})
    chk(st == 200, "voice.probe HTTP 200")
    chk(res.get("ok") is False, "探测不存在的端口 -> ok=False")
    chk("连不上" in (res.get("msg") or ""), "有人话提示：%s" % res.get("msg"))

    st, res = post("voice.nonexistent")
    chk(st == 200, "未知语音命令也回 200（协议统一）")
    chk(res.get("ok") is False, "未知命令 ok=False")
finally:
    RC.stop()

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1)
print("语音命令 DSH 联动全部检查通过")
shutil.rmtree(TMP, ignore_errors=True)