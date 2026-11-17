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
        self._reloads = 0           # 重载+重走次数（ring 盖不住/回拖才触发）
        self._manual_on = None      # 页面手动时钟的本地镜像（幂等用）
        self._last_ms = 0.0
        self._last_sig = None         # 上次 (ref, motion, expression) 签名
        # 手动时钟下每个 slot 已推到的状态签名（render_animated 用，
        # 同签名不再重发 loadModel/playMotion，省 3-4 次 JS 往返）
        self._sig_by_mid = {}

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
    def _cmd(self, env, timeout=8.0):
        """发一条命令信封并拿回页面返回值（seek / setManualClock 有返回）。

        宿主页 `__muppetOnCmd` 只对这些新命令回传结果，其余返回
        undefined；这里统一 JSON.stringify 后回传再解析，避开
        QJsonValue 的类型分歧（runJavaScript 对 object 的转换不稳定）。"""
        js = ("(function(){try{"
              " var r=window.__muppetOnCmd(JSON.stringify(%s));"
              " return (r===undefined||r===null) ? '' : JSON.stringify(r);"
              "}catch(e){return 'ERR:'+(e&&e.message||e);}})()"
              % json.dumps(env))
        r = self._js(js, timeout=timeout)
        if not isinstance(r, str):
            return {"err": "no_return"}
        if r.startswith("ERR:"):
            self._last_err = r[:200]
            return {"err": r[4:][:200]}
        if not r:
            return {}
        try:
            return json.loads(r)
        except Exception as e:
            self._last_err = "cmd json: %r %s" % (e, r[:120])
            return {"err": "bad_json"}

    def _apply_state(self, ref, mo, ex, mid, timeout=25.0):
        """把 (模型, 动作, 表情) 推到页面 slot `mid` 并等模型真正进场。

        render() 与 render_animated() 共用（原先内联在 render 里）。
        居中 + scale=1：让模型以自身尺寸铺在透明舞台中间，输出即本体，
        真正的 layout 交回导出侧的 _paste_rgba 应用。"""
        cmds = [
            {"cmd": "loadModel", "args": {"modelId": mid, "modelRef": ref}},
            {"cmd": "setTransform", "args": {"modelId": mid, "x": 0.5,
                                             "y": 0.5, "scale": 1.0,
                                             "rot": 0.0}},
        ]
        if mo:
            cmds.append({"cmd": "playMotion",
                         "args": {"modelId": mid, "name": mo, "fadeMs": 200}})
        if ex:
            cmds.append({"cmd": "setExpression",
                         "args": {"modelId": mid, "name": ex, "fadeMs": 200}})
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
            return False
        deadline = time.time() + max(8.0, timeout - 2.0)
        while time.time() < deadline:
            if self._model_ready(mid):
                return True
            self._wait(lambda: False, 0.15)
        self._last_err = "model wait timeout (%s)" % mid
        return False

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
            mid = state.get("modelId") or ("m%d" % (abs(hash(ref)) % 997))
            mo = str(state.get("motion") or "")
            ex = str(state.get("expression") or "")
            if not self._apply_state(ref, mo, ex, mid, timeout):
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

    # ---------------- 逐帧动画（手动时钟，导出泵用） ----------------
    def set_manual(self, on):
        """开/关页面手动时钟。

        开 = 所有模型 `autoUpdate=false`（脱离 Ticker.shared 的墙上时间）
        + app.ticker 暂停，时间只能由 `seek` 推进；关 = 交还给墙上时钟
        （预览的正常播放语义）。

        **为什么非要做**：导出吞吐远高于实时（实测 100fps+），模型若继续
        吃墙上时间，成片里的呼吸/眨眼会被压缩成快进——画面在动但动得不对。
        宿主页原有的 setPaused 只关 app.ticker，**管不住模型时间**（模型
        的 onTickerUpdate 挂在 Ticker.shared 上，见 bundle），所以必须
        单独有这条。"""
        with self._lock:
            if on and not self._bootn():
                return False
            if self._manual_on == bool(on):
                return True        # 幂等：连 JS 往返都省掉（页面侧同样有
                                   # noop 保护，防止把相位重置成 0）
            r = self._cmd({"cmd": "setManualClock", "args": {"on": bool(on)}})
            if r.get("err"):
                self._last_err = "set_manual: %s" % r["err"]
                return False
            self._manual_on = bool(on)
            if not on:
                self._sig_by_mid.clear()     # 退出手动：slot 状态作废，防脏签名
            return True

    def seek(self, mid, k, dt_ms, max_steps=64, timeout=30.0):
        """把 slot `mid` 推进到第 k 步（每步 dt_ms），返回页面回执。

        `max_steps` 是单次调用的推进上限：长片段回拖时要从头重走上千步，
        一次 JS 调用连跑会把主线程独占好几秒，所以切片跑。"""
        return self._cmd({"cmd": "seek", "args": {
            "modelId": mid, "k": int(k), "dt": float(dt_ms),
            "max": int(max_steps)}}, timeout=timeout)

    def model_box(self, mid, margin=24):
        """问页面要该 slot 的立绘包围盒（画布坐标 [x,y,w,h]）。

        框来自**模型几何**而不是像素测量 —— 见 stage_preview.html 里
        `__muppetBox` 的注释（用像素量会让导出不可复现）。margin 给
        呼吸/发丝留出余量。失败 None。"""
        r = self._js("window.__muppetBox ? (function(){var b=window.__muppetBox(%s,%d);"
                     "return b?JSON.stringify(b):'';})() : ''"
                     % (json.dumps(str(mid)), int(margin)), timeout=4.0)
        if not isinstance(r, str) or not r:
            return None
        try:
            v = json.loads(r)
        except Exception:
            return None
        return tuple(int(x) for x in v) if len(v) == 4 else None

    def _grab_ready(self, roi, tries=6):
        """抓一帧，空图就重画再抓（**不推进相位**）。

        Live2D 模型刚 loadModel 完时纹理还在上传，第一次 render 画不出
        东西（实测 k=0 alpha 全 0，第二次才有画面）。`_render` 只在
        deltaTime 非零时结算参数，所以 `__muppetRedraw` 的重试不会让
        相位前进 —— 拿到的一定还是第 k 帧。

        为什么必须重试而不是放过：放过就会 fallback 到松散的几何框，
        于是「这一道用紧框还是松框」取决于预热快慢，导出立刻不可复现
        （实测同一模型两个 slot 一个 640×349、一个 720×624，整段全帧
        对不上）。"""
        img = None
        for i in range(tries):
            img = self.grab_rgba(roi)
            if img is not None and img.size and int(img[..., 3].max()) > 8:
                return img
            self._js("window.__muppetRedraw && window.__muppetRedraw()",
                     timeout=4.0)
            self._wait(lambda: False, 0.02)
        return img

    def render_animated(self, state, model_id=None, k=0, dt_ms=1000.0 / 30.0,
                        max_steps=64, roi=None, timeout=25.0):
        """推进到第 k 步并抓一帧 -> (RGBA uint8 或 None, 新 roi 或 None)。

        与 `render` 的本质区别：画面由**调用方给的时间**决定。第 k 帧永远
        等于「从 0 起走 k 步、每步 dt_ms」的结果（页面侧实现见
        stage_preview.html 的 clockAPI.seek），因此与请求到达顺序无关。

        roi 语义：调用方存好上一轮返回的框，下一帧原样传回来。roi=None
        （该道第一帧）时向页面要**模型几何框**（`model_box`），不再抓整张
        画布去量 alpha —— 那样会让"用哪一帧定框"随预热时序漂移，实测
        A/B 两 slot 量出的框差 1px，导致整段导出逐帧全不相同（可复现性
        杀手）。几何框与时序无关，所以逐帧动画**仍然逐位可复现**。

        代价：每帧都要 update+render 成对推进（bundle 里 update() 只累计
        deltaTime、参数在 _render 里结算，所以 N 次不渲染的 update 不等于
        N 帧动画），再加一次抓图。"""
        t0 = time.time()
        with self._lock:
            if not self._bootn():
                return None, None
            if not self.set_manual(True):        # 幂等；确保时钟已接管
                return None, None
            ref = state.get("ref") or ""
            if not ref:
                return None, None
            mid = model_id or ("m%d" % (abs(hash(ref)) % 997))
            mo = str(state.get("motion") or "")
            ex = str(state.get("expression") or "")
            sig = (ref, mo, ex)
            fresh = self._sig_by_mid.get(mid) != sig
            if fresh:
                if not self._apply_state(ref, mo, ex, mid, timeout):
                    return None, None
                self._sig_by_mid[mid] = sig
                roi = None                       # 换模型/动作：旧框作废
                # 首帧宽限：loadModel 后纹理上传是异步的，立刻 render 会
                # 拿到空画布（实测 "empty alpha (no model drawn)"）。
                self._wait(lambda: False, 0.05)
            first = False
            if roi is None:
                # 两道框：几何框（与时序无关，用来抓 k=0）→ 从 k=0 的
                # alpha 收出**紧框**（与定格模式的裁剪语义一致，人物不会
                # 因为留白变多而缩水）。之后整段沿用这个紧框。
                geo = self.model_box(mid)
                if geo is None:
                    full = self._grab_ready(None)   # 老缓存页没这两个函数
                    box = self._autocrop_box(full, margin=24) if full else None
                    if box is None:
                        return None, None
                    y0, y1, x0, x1 = box
                    img = np.ascontiguousarray(full[y0:y1, x0:x1])
                    return img, (x0, y0, x1 - x0, y1 - y0)
                roi = geo
                first = True
            deadline = t0 + max(10.0, timeout)
            reached = False
            reloaded = False
            r = {}
            while time.time() < deadline:
                r = self.seek(mid, k, dt_ms, max_steps)
                if r.get("err"):
                    self._last_err = "seek: %s" % r["err"]
                    return None, None
                if (r.get("restarted") or r.get("needReload")) and not reloaded:
                    # 两种情况必须**重载模型 + 从 0 重走**，不能就地抓图：
                    # ① restarted：回拖跨出 ring —— 物理（发丝/布料弹簧）带
                    #    速度状态，stopAllMotions 不倒推它，实测「重启后重走
                    #    到 k」与首次差 max=255、20% 像素；
                    # ② needReload：一步没走但 _solo 刚把本道从隐藏切回可见
                    #    —— v2 SDK 的 draw 依赖"本模型刚 update 过"，此时
                    #    直接 render 抓回来是**全透明空图**（实测 bbox 非零
                    #    占比 0.00000）。
                    # 重载后走 k+1 步与首次逐位相同（实测三个新 slot 的 k=0
                    # 完全一致）。代价 ~0.7s，只在 ring 盖不住时发生。
                    self._cmd({"cmd": "clearModel", "args": {"modelId": mid}})
                    self._sig_by_mid.pop(mid, None)
                    if not self._apply_state(ref, mo, ex, mid, timeout):
                        return None, None
                    self._sig_by_mid[mid] = sig
                    reloaded = True
                    self._reloads += 1
                    continue
                if r.get("reached") or int(r.get("head", -1)) >= int(k) + 1:
                    reached = True
                    break
            if not reached:
                self._last_err = "seek timeout (k=%d head=%s)" % (k, r.get("head"))
                return None, None
            self._renders += 1
            img = self._grab_ready(roi)
            self._last_ms = (time.time() - t0) * 1000.0
            if img is None or not getattr(img, "size", 0):
                return None, None
            if not first:
                return img, None
            # k=0：把几何框收到立绘实际占用的紧框。余量取 4px：既给发丝/
            # 呼吸留动量，又贴近定格路径的 2px —— 余量差太多会让两种模式
            # 的人物尺寸差 1-2%（用户切开关会看到人物忽大忽小）。
            gx, gy, gw, gh = roi
            box = self._autocrop_box(img, margin=4)
            if box is None:
                return img, (gx, gy, gw, gh)
            y0, y1, x0, x1 = box
            tx, ty = gx + x0, gy + y0
            tw, th = x1 - x0, y1 - y0
            tight = (tx, ty, tw, th)
            out = np.ascontiguousarray(img[y0:y1, x0:x1])
            return out, tight

    def _model_ready(self, mid=None):
        """宿主页里**这个 slot** 的模型是否已进 stage（loadModel 真正完成）。

        必须按 slot 判定：`__muppetState().models` 是**所有** slot 的 id
        列表，早先版本只数 `length`，于是多路立绘时 B 道看到 A 道的模型
        就报 ready（实测：trace_b 自己的模型还没加载完就开始 seek，
        前两次拿到 no_model，后续帧的定框也跟着漂 -> 导出不可复现）。

        __muppetState 是**函数**（返回快照对象），不是属性。"""
        if mid is None:
            r = self._js(
                "(function(){try{"
                " if (typeof window.__muppetState !== 'function') return false;"
                " var st = window.__muppetState();"
                " return !!(st && st.models && st.models.length);"
                "}catch(e){return false;}})()", timeout=1.5)
            return bool(r)
        r = self._js(
            "(function(){try{"
            " if (typeof window.__muppetState !== 'function') return false;"
            " var st = window.__muppetState();"
            " return !!(st && st.models && st.models.indexOf(%s) >= 0);"
            "}catch(e){return false;}})()" % json.dumps(str(mid)), timeout=1.5)
        return bool(r)

    def _autocrop(self, rgba):
        """裁到 alpha 非零包围盒 -> 干净的立绘本体（去掉大片透明边距）。

        导出侧按 layout 摆位时以「素材自身尺寸」计算 fit 基准；留着视口
        大小的透明边会让 fit 基准失真（人物缩水）。裁掉后与普通图片源
        语义一致，_paste_rgba 直接可用。"""
        box = self._autocrop_box(rgba)
        if box is None:
            return None
        y0, y1, x0, x1 = box
        return np.ascontiguousarray(rgba[y0:y1, x0:x1])

    def _autocrop_box(self, rgba, margin=0):
        """alpha 非零包围盒 (y0,y1,x0,x1)，**画布坐标**；空图返回 None。

        margin 给描边/发丝留余量。逐帧动画里这个框每帧算一次会带来两个
        问题：autocrop 本身 0.7ms/帧，以及头发丝出框让 bbox 逐帧微变 ->
        导出侧 fit 基准抖动。所以首帧算出框后固定沿用（见 l2d_pump）。"""
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
        if margin:
            y0 = max(0, y0 - margin)
            x0 = max(0, x0 - margin)
            y1 = min(rgba.shape[0], y1 + margin)
            x1 = min(rgba.shape[1], x1 + margin)
        return (y0, y1, x0, x1)

    def grab_rgba(self, roi=None):
        """抓一帧 -> (H,W,4) RGBA uint8。

        `roi=(x,y,w,h)`（画布坐标）时走页面侧固定框：只 drawImage+编码立绘
        包围盒，1080p 实测全链路 15.4→10.3ms（webp 更慢且透明区 RGB 会烂，
        已否决）。roi=None 抓整张画布。失败 None。"""
        if roi is None:
            js = "(window.__muppetGrab ? window.__muppetGrab(null) : " \
                 "(function(){try{var cv=document.querySelector('canvas');" \
                 "return cv?cv.toDataURL('image/png'):'';}catch(e){return '';}})())"
        else:
            js = "window.__muppetGrab ? window.__muppetGrab(%s) : ''" % (
                json.dumps([int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3])]),)
        url = self._js(js, timeout=8.0)
        return self._decode_dataurl(url)

    def _decode_dataurl(self, url):
        """data:image/png;base64,... -> RGBA uint8（失败 None）。"""
        import base64
        if not isinstance(url, str) or not url.startswith("data:image/png"):
            self._last_err = "toDataURL unavailable"
            return None
        try:
            raw = base64.b64decode(url.split(",", 1)[1])
        except Exception as e:
            self._last_err = "b64: %r" % (e,)
            return None
        try:
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
        """canvas.toDataURL() -> numpy RGBA（真正的透明底画面）。

        v3.6：实现挪到 `grab_rgba`（多了页面侧固定 ROI 一条快路），这里
        保留名字给既有调用点/测试用。"""
        return self.grab_rgba(None)

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
            self._sig_by_mid.clear()
            self._manual_on = None      # 页面没了，幂等镜像必须一起作废
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
        return {"renders": self._renders, "reloads": self._reloads,
                "last_ms": round(self._last_ms, 1),
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
