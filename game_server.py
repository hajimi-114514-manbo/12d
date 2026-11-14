# -*- coding: utf-8 -*-
"""启动游戏.exe 的源码（task-62/H1e，PyInstaller 编译后随可玩包分发）。

背景：官方 WebGAL 4.6.6 是 ES module 构建，file:// 双击 index.html 黑屏
（CORS 拒绝加载模块）；终端玩家机器上没有 Python——所以启动器必须自包含。
本脚本：定位自身所在目录（=游戏包根）→ 找空闲端口 → 起静态 http 服务 →
自动开浏览器。关窗口即停服。零第三方依赖（标准库），PyInstaller --console
单文件编译（约 3-5MB），拷进导出包改名「启动游戏.exe」。
"""
import http.server
import os
import socket
import socketserver
import sys
import threading
import webbrowser


def app_root():
    """exe/脚本所在目录 = 游戏包根（frozen 时取 exe 旁，不取临时解包目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def main():
    root = app_root()
    if not os.path.isfile(os.path.join(root, "index.html")):
        print("错误：本程序须放在游戏包根目录（与 index.html 同级）运行。")
        print("当前目录:", root)
        return 1
    port = free_port()
    os.chdir(root)
    url = "http://127.0.0.1:%d/index.html" % port
    print("游戏已启动：%s" % url)
    print("保持本窗口打开；关闭窗口即退出游戏服务。")
    try:
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("127.0.0.1", port), Quiet) as httpd:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
            httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
