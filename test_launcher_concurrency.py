# -*- coding: utf-8 -*-
"""启动器并发回归测试（game_server.py）。

背景：原实现用单线程 socketserver.TCPServer，WebGAL 切场景时同时拉
背景 / Live2D 贴图 / 表情 / 音频，请求被完全串行化。实测 8 个并发请求
总耗时 520ms、最后一个等 508ms；多线程后 11ms 完成（约 47 倍）。

本测试断言：8 个并发请求必须能同时被服务（并发度 > 1），且总耗时远小于
各请求耗时之和（即真的重叠执行，而不是排队）。

跑法：D:/Python312/python.exe test_launcher_concurrency.py
"""
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import game_server  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_payload(root, name, size_kb):
    """造一个够大的文件，避免小文件几微秒结束、测不出串行。"""
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(size_kb * 1024))
    return path


def hammer(port, path, n):
    """n 个线程同时 GET。

    关键：并发度必须按「实际传输窗口」算，不能按线程启动时刻——
    单线程服务器上 8 个线程也会同时提交请求（那一刻都在等进行），
    但它们是被排进队列逐个服务的。真正的证据是**响应体读取期间**
    有多少请求重叠。

    返回 (wall, max_concurrency, last_ms, sum_ms)。
    """
    url = "http://127.0.0.1:%d/%s" % (port, urllib.parse.quote(path))
    urllib.request.urlopen(url, timeout=60).read()      # 预热

    spans = []          # (开始读响应体的时刻, 读完的时刻)
    lock = threading.Lock()

    def one():
        r = urllib.request.urlopen(url, timeout=120)
        t0 = time.time()            # 头到了 = 服务器开始真正服务这个请求
        r.read()
        t1 = time.time()
        with lock:
            spans.append((t0, t1))

    t0 = time.time()
    ths = [threading.Thread(target=one) for _ in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = time.time() - t0

    spans.sort()
    maxc = max(sum(1 for s1, e1 in spans if s1 <= s0 < e1) for s0, _ in spans)
    dur = sorted((e - s) * 1000 for s, e in spans)
    return wall, maxc, dur[-1], sum(dur)


def main():
    root = tempfile.mkdtemp(prefix="launcher_conc_")
    try:
        make_payload(root, "index.html", 4)
        big = "assets/big.bin"
        make_payload(root, big, 512)

        cwd = os.getcwd()
        os.chdir(root)
        port = free_port()
        httpd = game_server.Server(("127.0.0.1", port), game_server.Quiet)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            fails = []

            # 1) 结构性判据：服务类必须是多线程（最可靠，不受时序噪声影响）
            base = type(httpd).__mro__[1].__name__
            if "Threading" not in base:
                fails.append("服务类不是多线程：MRO=%s" % [c.__name__ for c in type(httpd).__mro__])
            print("服务类 MRO:", " -> ".join(c.__name__ for c in type(httpd).__mro__[:3]))

            # 2) 行为判据（实测区分度干净）：
            #      多线程  conc=8  overlap=6.53x
            #      单线程  conc=1  overlap=0.19x
            #    不用「8 并发墙钟 / 单请求墙钟」——8 个并行传输共享带宽，
            #    墙钟天然被带宽饱和拉高（实测多线程也有 4.6x），不可作判据。
            make_payload(root, big, 16 * 1024)
            w8, maxc, last, ssum = hammer(port, big, 8)
            overlap = ssum / max(w8 * 1000, 0.001)
            print("8 并发 16MB: wall=%.1fms conc=%d 传输之和=%.1fms 重叠=%.2fx"
                  % (w8 * 1000, maxc, ssum, overlap))
            if maxc < 4:
                fails.append("传输窗口并发度 %d < 4，请求被串行化或受限" % maxc)
            if overlap < 2.0:
                fails.append("重叠比仅 %.2fx，请求在排队执行（串行实测 0.19x）" % overlap)

            # 3) 中文路径不能 404（真实包全是中文文件名）
            zh = "background/羽丘礼堂通常.png"
            make_payload(root, zh, 32)
            u = "http://127.0.0.1:%d/%s" % (port, urllib.parse.quote(zh))
            got = urllib.request.urlopen(u, timeout=30)
            if got.status != 200:
                fails.append("中文路径返回 %d" % got.status)
            print("中文路径:", got.status, len(got.read()), "bytes")

            if fails:
                print("\nFAIL")
                for f in fails:
                    print("  -", f)
                return 1
            print("\nPASS  启动器并发正常")
            return 0
        finally:
            httpd.shutdown()
            httpd.server_close()
            os.chdir(cwd)
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())