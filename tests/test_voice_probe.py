# -*- coding: utf-8 -*-
"""voice_probe 测试：所有探测分支都要覆盖。

重点不是「成功路径」（真服务已实测过），而是**失败时不误判**：
  · 端口不通 -> 明确说连不上（不能说「服务可用」）
  · 端口通但不是语音服务 -> 明确说「不像语音服务」
  · 是 HTTP 但返回完全不相干的内容 -> 不能误判
用假 HTTP 服务模拟，不依赖真引擎。
"""
import http.server
import os
import socket
import socketserver
import sys
import threading

# ---- 测试引导：向上找项目根（与 tests/ 里既有测试一致）----
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT)

import voice_probe as VPR

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


print("== parse_addr")
for s, want in [("127.0.0.1:9880", ("127.0.0.1", 9880)),
                ("127.0.0.1", ("127.0.0.1", 9880)),
                ("", ("127.0.0.1", 9880)),
                ("localhost:9999", ("localhost", 9999)),
                ("http://127.0.0.1:9880/", ("127.0.0.1", 9880)),
                (":9880", ("127.0.0.1", 9880)),
                ("192.168.1.5:abc", ("192.168.1.5:abc", 9880))]:
    got = VPR.parse_addr(s)
    chk(got == want, "parse_addr(%r) = %s" % (s, got))

print()
print("== 端口不通")
p = free_port()
r = VPR.probe("127.0.0.1", p, timeout=1.5)
chk(r["ok"] is False, "不通时 ok=False")
chk(r["tcp"] is False, "tcp=False")
chk("连不上" in r["msg"], "人话提示：%s" % r["msg"])


class _FakeGPT(http.server.BaseHTTPRequestHandler):
    """假冒 GPT-SoVITS：缺参数时返回同样的指纹文案。"""

    def do_GET(self):
        if self.path.startswith("/set_gpt_weights"):
            body = b'{"message":"gpt weight path is required"}'
            self.send_response(400)
        elif self.path.startswith("/control"):
            body = b'{"message":"command is required"}'
            self.send_response(400)
        else:
            body = b'{"ok":true}'
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _FakeOther(http.server.BaseHTTPRequestHandler):
    """假冒普通网页服务（不该被误判成语音服务）。"""

    def do_GET(self):
        body = b"<html><body>Hello, this is nginx</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _FakeBare(http.server.BaseHTTPRequestHandler):
    """返回 200 但内容为空 —— 最容易误判的情况。"""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def serve(handler):
    port = free_port()
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    srv.allow_reuse_address = True
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, port


print()
print("== 真语音服务（假服务模拟指纹）")
srv, p = serve(_FakeGPT)
try:
    r = VPR.probe("127.0.0.1", p, timeout=4)
    chk(r["ok"] is True, "识别为可用")
    chk(r["tcp"] and r["is_api"], "tcp/is_api 都为 True")
    chk("可用" in r["msg"], "提示可用：%s" % r["msg"])
    chk(r["detail"]["set_gpt_weights"]["status"] == 400, "记录了 400")
finally:
    srv.shutdown()
    srv.server_close()

print()
print("== 普通网页服务（不能误判）")
srv, p = serve(_FakeOther)
try:
    r = VPR.probe("127.0.0.1", p, timeout=4)
    chk(r["ok"] is False, "未误判为可用")
    chk(r["tcp"] is True, "但承认端口通")
    chk(r["is_api"] is False, "is_api=False")
    chk("不像 GPT-SoVITS" in r["msg"], "提示不像语音服务：%s" % r["msg"])
finally:
    srv.shutdown()
    srv.server_close()

print()
print("== 空响应 HTTP（最易误判）")
srv, p = serve(_FakeBare)
try:
    r = VPR.probe("127.0.0.1", p, timeout=4)
    chk(r["ok"] is False, "空响应不误判")
    chk(r["is_api"] is False, "is_api=False")
finally:
    srv.shutdown()
    srv.server_close()

print()
print("== probe_local（本机引擎不存在时）")
os.environ["MUPPET_VOICE_DIR"] = os.path.join(
    os.environ.get("TEMP", "."), "绝对不存在的引擎目录_xyz")
r = VPR.probe_local()
chk(r["engine"] is False, "engine=False")
chk(r["python"] == "" and r["api"] == "", "python/api 为空")
chk("没有语音引擎" in r["msg"], "人话提示：%s" % r["msg"][:60])

print()
print("== wait_ready 超时（服务一直不起）")
p = free_port()
r = VPR.wait_ready("127.0.0.1", p, max_s=2.0, tick_s=0.3)
chk(r["ok"] is False, "超时返回 ok=False")
chk("仍未就绪" in r["msg"], "提示超时：%s" % r["msg"])

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("voice_probe 全部检查通过")