# -*- coding: utf-8 -*-
"""离屏 L2D 渲染器（v3.5.11）。

问题：导出视频里 Cubism2 立绘曾是剪影占位卡（用户报「保龄球模型」），
而**实时预览里是完整的**——因为预览走 WebGAL 运行时自带的
pixi-live2d-display（WebGL 真渲染），导出侧却没有渲染器可用（`.moc`
是专有格式，Python 无解析器）。

本模块复用 webgal_preview 那套已验证的机制（muppet:// scheme + 宿主页
stage_preview.html + PIXI.Application(preserveDrawingBuffer)），把
「推状态给页面显示」变成「推状态 + 抓一帧像素回来」，供导出逐帧取图。

关键实测结论（都踩过了）：
- QWebEngine 离屏 WebGL 可行：`move(-2000,-2000)+show()` 或正常 show
  都能真渲染并 grab；`WA_DontShowOnScreen` **不可用**（WebGL 不出图）。
- 宿主页的 PIXI.Application 已开 preserveDrawingBuffer，`view.grab()`
  能拿到完整合成结果——比 canvas.toDataURL 更稳（无需页面配合）。
- 必须等宿主页 `boot()` 完成（runtime 主包 + live2dPromise）再 loadModel，
  否则命令被丢弃。
- 同一进程只能有一个 QWebEngineView 在跑渲染，全程加锁。

对外 API：
    rd = shared_renderer()          # 进程级单例（懒创建）
    rd.ready()                      # runtime/SDK 是否就位
    ref = rd.register(model_dir)    # 注册模型目录 -> scheme 引用
    img = rd.render(state)          # {ref, layout, motion, expression, t}
                                    # -> (H,W,4) RGBA uint8 或 None
    rd.close()
"""
import os
import sys
import time
import json
import threading

import numpy as np

_V_DIR = os.path.dirname(os.path.abspath(__file__))
if _V_DIR not in sys.path:
    sys.path.insert(0, _V_DIR)

# 默认画布：导出侧 1080p 竖构图立绘（由调用方按需指定）
DEFAULT_SIZE = (1080, 1920)

_RENDERER = None
_RENDERER_LOCK = threading.Lock()


def _qt_imports():
    from PySide6.QtCore import Qt, QTimer, QEventLoop, QBuffer, QByteArray, \
        QIODevice
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication, QWidget
    return Qt, QTimer, QEventLoop, QBuffer, QByteArray, QIODevice, QImage, \
        QApplication, QWidget


class L2DRenderer(object):
    """离屏逐帧渲染 Carnism2 立绘。线程安全（内部串行化）。"""

    def __init__(self, size=DEFAULT_SIZE):
        self._size = (int(size[0]), int(size[1]))
        self._lock = threading.RLock()
        self._view = None
        self._preview = None          # webgal_preview.WebPreviewView
        self._ready = False
        self._booted = False
        self._boot_wait = 0.0
        self._last_err = ""
        self._renders = 0
        self._last_ms = 0.0
        self._last_sig = None         # 上次 (ref, motion, expression) 签名

    # ---------------- 初始化 ----------------
    def ready(self):
        """runtime + live2d SDK 是否就位（不建窗口，纯静态检查）。"""
        try:
            import webgal_preview as WP
            return bool(WP.available()) and bool(WP.runtime_ready())
        except Exception:
            return False

    def _ensure_view(self):
        """懒建离屏 WebPreviewView（移出屏幕，不打扰用户）。"""
        if self._preview is not None:
            return True
        try:
            import webgal_preview as WP
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is None:
                return False
            # 复用预览视图：它把 scheme/宿主页/WebChannel 都装好了
            self._preview = WP.WebPreviewView()
            v = self._preview
            # 移出屏幕（实测 WA_DontShowOnScreen 会让 WebGL 不出图）
            v.resize(self._size[0], self._size[1])
            v.move(-20000, -20000)
            v.show()
            self._view = v
            return True
        except Exception as e:
            self._last_err = "ensure_view: %r" % (e,)
            self._preview = None
            return False

    def _wait(self, cond, timeout):
        """在 Qt 事件循环里等条件成立（渲染/页面加载都是异步的）。"""
        Qt, QTimer, QEventLoop, *_ = _qt_imports()
        loop = QEventLoop()
        state = {"done": False}

        def tick():
            if cond():
                state["done"] = True
                loop.quit()

        timer = QTimer()
        timer.timeout.connect(tick)
        timer.start(30)
        QTimer.singleShot(30, tick)
        QTimer.singleShot(int(max(50, timeout * 1000)), loop.quit)
        loop.exec()
        timer.stop()
        return state["done"]

    def _bootn(self, timeout=25.0):
        """等宿主页 module script 求值完成（命令入口 __muppetOnCmd 出现）。

        实测：页面 loadFinished 后 module 还需 ~1-2s 才把 __muppetOnCmd 挂上
        （runtime 主包 body 很大）；检测太早会误判"未就绪"（我踩过）。
        """
        if self._booted:
            return True
        if not self._ensure_view():
            return False
        t0 = time.time()
        self._preview.start()
        ok = self._wait(lambda: self._api_ready(), timeout)
        self._booted = ok
        self._boot_wait = time.time() - t0
        if not ok:
            self._last_err = "boot timeout (%.1fs)" % self._boot_wait
        return ok

    def _js(self, code, timeout=6.0):
        """同步执行 JS 并拿回结果（阻塞式，导出逐帧要用）。"""
        if self._preview is None:
            return None
        Qt, QTimer, QEventLoop, *_ = _qt_imports()
        # WebPreviewView 内部真正的 QWebEngineView 是 .view（见 grab_shot）
        wv = getattr(self._preview, "view", None)
        page = wv.page() if wv is not None else None
        if page is None:
            self._last_err = "_js: no page"
            return None
        box = {"v": None, "done": False}
        loop = QEventLoop()

        def cb(res):
            box["v"] = res
            box["done"] = True
            loop.quit()

        try:
            page.runJavaScript(code, cb)
        except Exception as e:
            self._last_err = "_js: %r" % (e,)
            return None
        QTimer.singleShot(int(timeout * 1000), loop.quit)
        loop.exec()
        return box["v"]

    def _api_ready(self):
        """宿主页命令入口是否就绪（module script 已求值）。"""
        r = self._js("(function(){try{return !!(window.__muppetOnCmd);}"
                     "catch(e){return false;}})()", timeout=2.0)
        return bool(r)

    # ---------------- 模型注册 ----------------
    def register(self, model_dir):
        """注册模型目录 -> scheme 引用（如 model/ab12cd34/relative/dir）。"""
        if self._preview is None and not self._ensure_view():
            return None
        try:
            return self._preview.register_model_dir(model_dir)
        except Exception as e:
            self._last_err = "register: %r" % (e,)
            return None

    # ---------------- 渲染 ----------------
    def render(self, state, timeout=25.0):
        """渲染一帧 -> (H,W,4) RGBA uint8；失败 None。

        state = {ref, layout, motion, expression, t, stage}
        产物约定（**为了能直接喂导出侧的 _paste_rgba**）：
        - 输出**透明底、未应用 layout 的立绘本体**：stage 里模型被居中、
          按立绘自身宽高铺满，导出的 layout（x/y/scale/rot）由 _paste_rgba
          再应用一次（与占位卡/图片源同一条路径，语义统一）。
        - 因此这里传的 layout 恒为「居中、scale=1、rot=0」，真正的位置
          变换交回导出侧。
        """
        with self._lock:
            if not self._bootn():
                return None
            ref = state.get("ref") or ""
            if not ref:
                return None
            mid = "m%d" % (abs(hash(ref)) % 997)

            def _cmd(cmd, **args):
                return {"cmd": cmd, "args": args}

            # 居中 + scale=1：让模型以自身尺寸铺在透明舞台中间，输出即本体。
            cmds = [
                _cmd("loadModel", modelId=mid, modelRef=ref),
                _cmd("setTransform", modelId=mid, x=0.5, y=0.5,
                     scale=1.0, rot=0.0),
            ]
            mo = str(state.get("motion") or "")
            if mo:
                cmds.append(_cmd("playMotion", modelId=mid, name=mo,
                                 fadeMs=200))
            ex = str(state.get("expression") or "")
            if ex:
                cmds.append(_cmd("setExpression", modelId=mid, name=ex,
                                 fadeMs=200))
            js = (
                "(function(){\n"
                " try{\n"
                "  var cs = %s;\n"
                "  for (var i=0;i<cs.length;i++){"
                "window.__muppetOnCmd(JSON.stringify(cs[i]));}\n"
                "  return 'ok';\n"
                " }catch(e){ return 'ERR:'+(e&&e.message||e); }\n"
                "})()" % json.dumps(cmds)
            )
            r = self._js(js, timeout=timeout)
            if isinstance(r, str) and r.startswith("ERR:"):
                self._last_err = r
                return None
            deadline = time.time() + max(8.0, timeout - 2.0)
            while time.time() < deadline:
                if self._model_ready():
                    break
                self._wait(lambda: False, 0.15)
            else:
                self._last_err = "model wait timeout"
                return None
            # v3.5.11 性能：原先是固定 _wait(0.25) 死等 250ms（占单帧耗时
            # 的 94%，实测 render 265ms 里只有 ~24ms 是真实工作）。
            # 现在按须等待：仅在**姿态/动作变化**时给 ticker 一两帧刷新，
            # 同 state 复用时直接抓（pixi 已渲染好，画面就是当前帧）。
            sigc = (ref, mo, ex)
            if sigc != self._last_sig:
                self._wait(lambda: False, 0.05)   # 换动作/表情才等一帧
                self._last_sig = sigc
            t0 = time.time()
            img = self._grab()
            self._renders += 1
            self._last_ms = (time.time() - t0) * 1000.0
            if img is None:
                return None
            return self._autocrop(img)

    def _model_ready(self):
        """宿主页是否已经有模型进 stage（loadModel 真正完成）。

        __muppetState 是**函数**（返回快照对象），不是属性——调用它取
        models 数组。"""
        r = self._js(
            "(function(){try{"
            " if (typeof window.__muppetState !== 'function') return false;"
            " var st = window.__muppetState();"
            " return !!(st && st.models && st.models.length);"
            "}catch(e){return false;}})()", timeout=1.5)
        return bool(r)

    def _autocrop(self, rgba):
        """裁到 alpha 非零包围盒 -> 干净的立绘本体（去掉大片透明边距）。

        导出侧按 layout 摆位时以「素材自身尺寸」计算 fit 基准；留着视口
        大小的透明边会让 fit 基准失真（人物缩水）。裁掉后与普通图片源
        语义一致，_paste_rgba 直接可用。"""
        a = rgba[..., 3]
        rows = np.any(a > 8, axis=1)
        if not rows.any():
            self._last_err = "empty alpha (no model drawn)"
            return None
        cols = np.any(a > 8, axis=0)
        ys = np.nonzero(rows)[0]
        xs = np.nonzero(cols)[0]
        y0, y1 = int(ys[0]), int(ys[-1]) + 1
        x0, x1 = int(xs[0]), int(xs[-1]) + 1
        # 留 2px 余量，避免裁掉半透明描边
        y0, x0 = max(0, y0 - 2), max(0, x0 - 2)
        y1 = min(rgba.shape[0], y1 + 2)
        x1 = min(rgba.shape[1], x1 + 2)
        return np.ascontiguousarray(rgba[y0:y1, x0:x1])

    def _grab(self):
        """抓一帧 -> numpy RGBA。

        **优先从 canvas 取（toDataURL）**：宿主页 PIXI.Application 已开
        preserveDrawingBuffer，canvas 里是**透明底立绘本体**；
        view.grab() 抓的是整个 QWidget（含深色窗口背景，alpha 全 255，
        裁不出立绘边界——实测踩过）。toDataURL 失败才退回 grab。
        """
        img = self._grab_canvas()
        if img is not None:
            return img
        return self._grab_widget()

    def _grab_canvas(self):
        """canvas.toDataURL() -> numpy RGBA（真正的透明底画面）。"""
        import base64
        url = self._js(
            "(function(){try{"
            " var cv=document.querySelector('canvas');"
            " if(!cv) return '';"
            " return cv.toDataURL('image/png');"
            "}catch(e){return '';}})()", timeout=8.0)
        if not isinstance(url, str) or not url.startswith("data:image/png"):
            self._last_err = "toDataURL unavailable"
            return None
        try:
            raw = base64.b64decode(url.split(",", 1)[1])
        except Exception as e:
            self._last_err = "b64: %r" % (e,)
            return None
        try:
            import cv2
            from PIL import Image
            import io as _io
            img = Image.open(_io.BytesIO(raw))
            arr = np.array(img)
            if arr.ndim == 3 and arr.shape[2] == 3:
                arr = np.dstack([arr, np.full(arr.shape[:2], 255, np.uint8)])
            return np.ascontiguousarray(arr)
        except Exception as e:
            self._last_err = "decode: %r" % (e,)
            return None

    def _grab_widget(self):
        """view.grab() -> numpy RGBA。"""
        Qt, QTimer, QEventLoop, QBuffer, QByteArray, QIODevice, QImage, \
            QApplication, QWidget = _qt_imports()
        wv = getattr(self._preview, "view", None)
        if wv is None:
            self._last_err = "grab: no view"
            return None
        try:
            pix = wv.grab()
        except Exception as e:
            self._last_err = "grab: %r" % (e,)
            return None
        if pix is None or pix.isNull():
            return None
        img = pix.toImage().convertToFormat(QImage.Format_RGBA8888)
        w, h = img.width(), img.height()
        if w <= 0 or h <= 0:
            return None
        ptr = img.bits()
        try:
            buf = np.frombuffer(ptr, np.uint8, count=w * h * 4)
        except TypeError:
            buf = np.frombuffer(bytes(ptr), np.uint8)[:w * h * 4]
        # **必须 copy**：QImage.bits() 返回的是 Qt 内部缓冲的视图，QImage
        # 离开作用域后内存即被释放/复用——直接把视图交出去会让下游拿到
        # 悬垂数据（实测保存成 0 字节/进程崩溃）。
        return np.array(buf.reshape(h, w, 4), copy=True)

    # ---------------- 生命周期 ----------------
    def close(self):
        with self._lock:
            p = self._preview
            self._preview = None
            self._view = None
            self._booted = False
            if p is not None:
                try:
                    p.clear_all()
                except Exception:
                    pass
                # v3.5.11：先把内部 QWebEngineView 的页面置空再销毁，
                # 否则 Qt 报 "WebEnginePage still not deleted"
                wv = getattr(p, "view", None)
                if wv is not None:
                    try:
                        wv.setPage(None)
                    except Exception:
                        pass
                    try:
                        wv.deleteLater()
                    except Exception:
                        pass
                try:
                    p.close()
                    p.deleteLater()
                except Exception:
                    pass
                # 让 Qt 真正处理 deleteLater（否则退到解释器清理时报错）
                try:
                    from PySide6.QtWidgets import QApplication
                    app = QApplication.instance()
                    if app is not None:
                        for _ in range(8):
                            app.processEvents()
                except Exception:
                    pass

    def stats(self):
        return {"renders": self._renders, "last_ms": round(self._last_ms, 1),
                "boot_wait": round(self._boot_wait, 2),
                "ready": self._ready if hasattr(self, "_ready") else None,
                "err": self._last_err[:200]}


def shared_renderer(size=None):
    """进程级单例渲染器（导出/预览共用；多个 QWebEngineView 会抢资源）。"""
    global _RENDERER
    with _RENDERER_LOCK:
        if _RENDERER is None:
            try:
                _RENDERER = L2DRenderer(size or DEFAULT_SIZE)
            except Exception:
                _RENDERER = None
        return _RENDERER


def shutdown():
    global _RENDERER
    with _RENDERER_LOCK:
        if _RENDERER is not None:
            try:
                _RENDERER.close()
            except Exception:
                pass
            _RENDERER = None
