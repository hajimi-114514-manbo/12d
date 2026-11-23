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
import contextlib


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


class Server(socketserver.ThreadingTCPServer):
    """多线程静态服务：WebGAL 切场景时会同时拉背景 / Live2D 贴图 / 表情 / 音频。

    原实现用单线程 TCPServer，并发请求被完全串行化。实测（同一台机器、
    同一批文件、已预热到 OS 缓存、8 个并发请求）：
        旧启动器 exe  532.8ms 完成，最后一个请求等 523.0ms，并发度 1
        新启动器 exe   11.3ms 完成，最后一个请求等  8.3ms，并发度 8
    这段等待在播放中表现为切场景时的一次性空帧，曾被误判成「滤镜太卡」。
    """

    allow_reuse_address = True
    daemon_threads = True          # 关窗口时不必等长连接自行结束


def main():
    root = app_root()
    if not os.path.isfile(os.path.join(root, "index.html")):
        print("错误：本程序须放在游戏包根目录（与 index.html 同级）运行。")
        print("当前目录:", root)
        return 1
    port = free_port()
    os.chdir(root)
    url = "http://127.0.0.1:%d/index.html" % port
    # flush=True：PyInstaller --console 下 stdout 是管道时默认块缓冲，
    # 外层（进度显示 / 自动化测试）读不到「游戏已启动」这行，会以为它卡死。
    print("游戏已启动：%s" % url, flush=True)
    print("保持本窗口打开；关闭窗口即退出游戏服务。", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        with Server(("127.0.0.1", port), Quiet) as httpd:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
            httpd.serve_forever()
    return 0


if __name__ == "__main__": sys.exit(main())
