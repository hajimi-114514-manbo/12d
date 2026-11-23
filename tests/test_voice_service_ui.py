# -*- coding: utf-8 -*-
"""语音服务 UI 回归：对话框构建、检测联动、菜单接线。

不依赖真服务（探测用假服务），验证 UI 逻辑。
"""
import http.server
import os
import socket
import socketserver
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# ---- 测试引导：向上找项目根（与 tests/ 里既有测试一致）----
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT)

TMP = os.path.join(os.environ.get("TEMP", "."), "voice_svc_ui_test")
os.makedirs(TMP, exist_ok=True)
os.environ["MUPPET_VOICE_DIR"] = os.path.join(TMP, "没有引擎")

from PySide6.QtWidgets import QApplication, QWidget
app = QApplication(sys.argv)
HOST = QWidget()
HOST.resize(900, 600)

import voice_service_ui as VSU
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


class _FakeGPT(http.server.BaseHTTPRequestHandler):
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
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


print("== 对话框构建")
d = VSU.VoiceServiceDialog(HOST)
chk(d is not None, "能构建")
chk(d.ed_addr is not None, "有地址输入框")
chk(d.ed_addr.text() != "", "地址有默认值：%s" % d.ed_addr.text())
chk(d.btn_probe is not None, "有检测按钮")
chk(not d.progress.isVisible(), "进度条默认隐藏")
chk("尚未检测" in d.lbl_result.text(), "初始结论：%s" % d.lbl_result.text())

print()
print("== 本机引擎状态（不存在时）")
chk("没有语音引擎" in d.lbl_engine.text() or "可以连" in d.lbl_engine.text(),
    "提示本机无引擎：%s" % d.lbl_engine.text()[:60])

print()
print("== 检测：真服务（假服务模拟）")
srv_port = free_port()
srv = socketserver.ThreadingTCPServer(("127.0.0.1", srv_port), _FakeGPT)
srv.allow_reuse_address = True
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
try:
    d.ed_addr.setText("127.0.0.1:%d" % srv_port)
    d._do_probe()
    # 检测可能快到「查的时候已结束」，所以判断「按钮禁用 + 文案变化」，
    # 进度条可见性单独用 setVisible 的直接观察（不是 isVisible，
    # 因为父窗口没 show 时 isVisible 恒为 False）。
    chk(not d.btn_probe.isEnabled(), "检测中按钮禁用")
    chk(not d.progress.isHidden() or d._thread is None,
        "检测中进度条可见（或已结束）")
    chk("检测中" in d.lbl_result.text(), "显示检测中")
    # 等线程结束
    import time
    t0 = time.time()
    while d._thread is not None and time.time() - t0 < 20:
        app.processEvents()
        time.sleep(0.05)
    app.processEvents()
    chk(d._thread is None, "线程已结束")
    chk(d.btn_probe.isEnabled(), "按钮恢复可用")
    chk(not d.progress.isVisible(), "进度条隐藏")
    chk("✓" in d.lbl_result.text(), "结论打勾：%s" % d.lbl_result.text())
    chk("可用" in d.badge.text(), "徽标显示可用：%s" % d.badge.text())
    chk("set_gpt_weights" in d.lbl_detail.text(), "明细有端点信息")
finally:
    srv.shutdown()
    srv.server_close()

print()
print("== 检测：连不上")
p = free_port()
d.ed_addr.setText("127.0.0.1:%d" % p)
d._do_probe()
import time
t0 = time.time()
while d._thread is not None and time.time() - t0 < 20:
    app.processEvents()
    time.sleep(0.05)
app.processEvents()
chk("×" in d.lbl_result.text(), "结论打叉：%s" % d.lbl_result.text())
chk("不可用" in d.badge.text(), "徽标显示不可用")

print()
print("== 地址记忆")
VSU.save_addr("192.168.1.9:9880")
chk(VSU.load_addr() == "192.168.1.9:9880", "地址能记住：%s" % VSU.load_addr())
VSU.save_addr("127.0.0.1:9880")

print()
print("== 菜单接线")
import l2d_rig_qt as M
chk(hasattr(M.AssetMenuMixin, "_open_voice_service")
    or hasattr(M.MainWindow, "_open_voice_service"), "方法存在")
from qfluentwidgets import FluentIcon
chk(hasattr(FluentIcon, "MICROPHONE"), "MICROPHONE 图标存在")

print()
print("=" * 50)
if fails:
    print("失败 %d 项" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("语音服务 UI 检查全部通过")