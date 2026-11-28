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
import re
import threading

import numpy as np
import contextlib

_V_DIR = os.path.dirname(os.path.abspath(__file__))
if _V_DIR not in sys.path: sys.path.insert(0, _V_DIR)

# 默认画布：导出侧 1080p 竖构图立绘（由调用方按需指定）
DEFAULT_SIZE = (1080, 1920)

_RENDERER = None
_RENDERER_LOCK = threading.Lock()


# ---------------- 方案 B：姿态插值（Live2D 参数空间） ----------------
# 切 clip / 换表情时，页面在 beforeModelUpdate 里用「切换瞬间的姿态快照」把新
# 姿态按 smoothstep 拉过去 n 帧，从根上消除图像交叉溶解的双层半透明重影。
# 时长可用环境变量 MUPPET_POSE_MS 覆盖（0 = 关掉，退回硬切/纯图像淡入）。
POSE_MS = float(os.environ.get("MUPPET_POSE_MS", "400") or 0.0)

_POSE_PARAMS_BY_REF = {}
_MOC_PARAM_CACHE = {}
_MOC_PARAM_RE = re.compile(rb"PARAM_[A-Za-z0-9_]+")


def _moc_pose_params(model_dir):
    """从 Cubism2 模型目录的 model.json -> *.moc 里抽参数 id 名单。

    只读模型文件；异常或非 Cubism2 模型一律返回 []（页面侧就不做参数插值，
    自动退回原行为）。结果按 model.json 的 mtime 缓存。
    """
    try:
        p = str(model_dir or "").strip()
        if not p: return []
        if p.lower().startswith("file:"):
            from urllib.parse import urlparse, unquote
            from urllib.request import url2pathname
            p = url2pathname(unquote(urlparse(p).path))
            if len(p) > 2 and p[0] == "\\" and p[2] == ":":
                p = p[1:]
        p = p.replace("/", os.sep)
        if os.path.isdir(p): mj = os.path.join(p, "model.json")
        elif p.lower().endswith(".json"): mj = p
        else: return []
        if not os.path.isfile(mj): return []
        key = (os.path.normcase(os.path.abspath(mj)), os.path.getmtime(mj))
        if key in _MOC_PARAM_CACHE: return _MOC_PARAM_CACHE[key]
        with open(mj, "r", encoding="utf-8") as f:
            d = json.load(f)
        rel = str((d or {}).get("model") or (d or {}).get("file") or "")
        if not rel.lower().endswith(".moc"):
            _MOC_PARAM_CACHE[key] = []
            return []
        moc = os.path.join(os.path.dirname(mj), rel.replace("/", os.sep))
        if not os.path.isfile(moc):
            _MOC_PARAM_CACHE[key] = []
            return []
        with open(moc, "rb") as f: raw = f.read()
        names = sorted({m.decode("ascii", "ignore")
                        for m in _MOC_PARAM_RE.findall(raw)})
        _MOC_PARAM_CACHE[key] = names
        return names
    except Exception:
        return []



def _qt_imports():
    from PySide6.QtCore import Qt, QTimer, QEventLoop, QBuffer, QByteArray, \
        QIODevice
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication, QWidget
    return Qt, QTimer, QEventLoop, QBuffer, QByteArray, QIODevice, QImage, \
        QApplication, QWidget


# v3.7.3 渲染侧口型回读台账：mid -> {tag,has,now,skipped,expect,k}
# 只在下发/切换口型表后的那一帧采一次（一次 JS 往返），供导出侧
# video_export._lip_check_warn 做「页面到底有没有按我们的表画嘴」的校验。
_LIP_READBACK = {}
_LIP_READBACK_LOCK = threading.Lock()


def lip_readback():
    """返回回读台账快照（导出侧只读，加锁复制）。"""
    with _LIP_READBACK_LOCK:
        return {k: dict(v) for k, v in _LIP_READBACK.items()}


def clear_lip_readback():
    with _LIP_READBACK_LOCK:
        _LIP_READBACK.clear()


# ---------------- v3.7.5 取像素通道的实测记录（**结论：保持同步 PNG**）----
# 导出逐帧动画时渲染泵是唯一瓶颈（实测 239s 工程：导出 123.7 s，其中
# 泵服务 6212 帧 × 18.7 ms = 116 s，占 94%）。泵每帧 18.7 ms =
#   seek 3.55 ms（WebGL 渲染，必须主线程，无法挪走）
# + grab_rgba 13.56 ms（取像素，其中页面侧 toDataURL('image/png') 编码 7.9 ms）
# + 泵/队列开销 ~1.6 ms
#
# 试过把「取像素」整块搬到 Web Worker 后台（页面复用 2D canvas 取原始
# RGBA 字节 -> Worker 压缩 -> 回传），结论是**反而更慢，已回退**：
#   · Worker + deflate（无损，像素 maxdiff=0）：Worker 内压缩约 20 ms/帧
#     （deflate 比 PNG 慢），主线程虽只占 2 ms，但导出线程必须**等这一帧
#     的像素**才能继续合成 -> 整片 116 s 变成 159 s（泵 53.5 -> 39.0 fps）。
#   · Worker + convertToBlob('image/png')：约 15 ms/帧，且 OffscreenCanvas
#     颜色空间转换有损（3 个像素差 1），破坏「导出逐位可复现」。
#   · WebP 21-33 ms、JPEG 11.9 ms（且有损）、原始 RGBA 字符串直传 20.6 ms。
#   => PNG 编码放主线程反而是最快的，**保留原路径**。
# 真正的提速点在别处：编码档可选（avc1 快 42%）与合成侧缓存。


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
        # v3.7.3 口型表签名：mid -> (lip_tag, 表长)。
        # 为什么必须单独记：同一 model_path 在**整条时间线上共用一个 slot**，
        # 相邻 clip 换台词但 ref/motion/expression 一模一样时，原来只有
        # fresh 分支才下发 setLip —— 新 clip 会继续用上一个 clip 的表，
        # 按 clip 内偏移去取就整体错位/越界（projects.mp4 465.7s 起嘴定死）。
        self._lip_sig_by_mid = {}
        # mid -> {"tag","vals","k","gain"}：刚下发过表，下一帧顺手回读页面
        # 的 lipNow / lipHookStat，供导出侧做「渲染侧回读校验」。
        self._lip_pend = {}

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
        if self._preview is not None: return True
        try:
            import webgal_preview as WP
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is None: return False
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
        if self._booted: return True
        if not self._ensure_view(): return False
        t0 = time.time()
        self._preview.start()
        ok = self._wait(lambda: self._api_ready(), timeout)
        self._booted = ok
        self._boot_wait = time.time() - t0
        if not ok: self._last_err = "boot timeout (%.1fs)" % self._boot_wait
        return ok

    def _js(self, code, timeout=6.0):
        """同步执行 JS 并拿回结果（阻塞式，导出逐帧要用）。"""
        if self._preview is None: return None
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
        if self._preview is None and not self._ensure_view(): return None
        try:
            ref = self._preview.register_model_dir(model_dir)
            if ref:
                # 方案 B：顺手把该模型的参数 id 名单备好（Cubism2 的 moc 里是
                # 明文 PARAM_*，抽一次缓存住），loadModel 时下发页面做姿态插值。
                # 抽不到就下发 []，页面自动降级为不插值（等价旧行为）。
                _POSE_PARAMS_BY_REF[ref] = _moc_pose_params(model_dir)
            return ref
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
        if not isinstance(r, str): return {"err": "no_return"}
        if r.startswith("ERR:"):
            self._last_err = r[:200]
            return {"err": r[4:][:200]}
        if not r: return {}
        try:
            return json.loads(r)
        except Exception as e:
            self._last_err = "cmd json: %r %s" % (e, r[:120])
            return {"err": "bad_json"}

    def _apply_state(self, ref, mo, ex, mid, timeout=25.0, lip=None,
                     lip_fps=30.0, lip_gain=1.0, lip_tag=""):
        """把 (模型, 动作, 表情[, 口型]) 推到页面 slot `mid` 并等模型真正进场。

        render() 与 render_animated() 共用（原先内联在 render 里）。
        居中 + scale=1：让模型以自身尺寸铺在透明舞台中间，输出即本体，
        真正的 layout 交回导出侧的 _paste_rgba 应用。

        v3.6.4：lip 是**开口度序列**（list[float]，按片段内帧序）。
        传了就先下发 setLip，之后 seek 每步都会按 k 取一帧写进嘴部参数；
        不传则解除绑定（回到「不控嘴」，由模型自己的动作/物理决定）。
        表要在这里下发而不是每帧发：序列可能几千个 float，逐帧发会拖死
        IPC（每帧一次 JSON 往返）。
        """
        cmds = [
            {"cmd": "loadModel", "args": {"modelId": mid, "modelRef": ref,
                                     "poseParams": _POSE_PARAMS_BY_REF.get(ref) or []}},
            {"cmd": "setTransform", "args": {"modelId": mid, "x": 0.5,
                                             "y": 0.5, "scale": 1.0,
                                             "rot": 0.0}},
        ]
        if mo:
            cmds.append({"cmd": "playMotion",
                         "args": {"modelId": mid, "name": mo, "fadeMs": POSE_MS}})
        if ex:
            cmds.append({"cmd": "setExpression",
                         "args": {"modelId": mid, "name": ex, "fadeMs": POSE_MS}})
        if lip:
            cmds.append({"cmd": "setLip",
                         "args": {"modelId": mid, "vals": list(lip),
                                  "fps": float(lip_fps),
                                  "gain": float(lip_gain),
                                  "tag": str(lip_tag or "")}})
        elif lip_tag and self._lip_sig_by_mid.get(mid):
            # 换了 clip 且新 clip 没有台词（lip=None）：必须**显式解除**，
            # 否则页面还挂着上一个 clip 的表，按新 clip 内偏移去取就是错位的嘴。
            cmds.append({"cmd": "setLip",
                         "args": {"modelId": mid, "vals": [],
                                  "fps": float(lip_fps),
                                  "gain": float(lip_gain),
                                  "tag": str(lip_tag or "")}})
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
            if self._model_ready(mid): return True
            self._wait(lambda: False, 0.15)
        self._last_err = "model wait timeout (%s)" % mid
        return False

    def _set_expression(self, mid, ex, keep_phase=True, timeout=8.0):
        """只换表情：补发一条 setExpression，**不重载模型、不清 roi**。

        方案 C 的落点。页面侧 setExpression 收到 keepPhase=True 时只改表情
        权重，不写 s.k、不 _resetClock —— 相位（elapsedTime）保持连续，
        动作/呼吸/物理因此不会「归零再起步」。

        ex 为空串 = 清空表情（回到模型默认脸），走页面侧 resetExpression。
        """
        r = self._cmd({"cmd": "setExpression", "args": {
            "modelId": mid, "name": str(ex or ""), "fadeMs": POSE_MS,
            "keepPhase": bool(keep_phase)}}, timeout=timeout)
        if r.get("err"):
            self._last_err = "setExpression: %s" % r["err"]
            return False
        return True

    def _lip_mark_pending(self, mid, tag, k, n):
        """登记「口型表已下发、等高值帧回读」的待验记录。

        v3.7.4：没有这条记录，导出侧没法区分「表压根没到页面」（真故障）
        与「表在手上、只是在等第一个有开口度的帧」（静音前奏长的 clip，
        如 c21 前 21s 全 0）。两者混为一谈就会误报 LIPWARN。
        """
        with _LIP_READBACK_LOCK:
            _LIP_READBACK[mid] = {"tag": str(tag or ""),
                                  "has": None, "now": None, "skipped": None,
                                  "expect": None, "expect1": None,
                                  "k": (int(k) if k is not None else 0),
                                  "len": int(n or 0), "pending": True}

    def _set_lip(self, mid, lip, lip_fps, lip_gain, tag="", timeout=10.0,
                 k=None):
        """只重下发口型表（不动模型/动作/表情），并登记回读探针。

        v3.7.3 新增：同一 ref 连续多个 clip 共用 slot 时，动作与表情都可能
        不变，只有台词变了 —— 这时既不走 loadModel 也不走 playMotion，
        旧代码里 setLip 只在 fresh 分支发，页面就一直用上一个 clip 的表。
        """
        vals = list(lip) if lip else []
        r = self._cmd({"cmd": "setLip",
                       "args": {"modelId": mid, "vals": vals,
                                "fps": float(lip_fps),
                                "gain": float(lip_gain),
                                "tag": str(tag or "")}}, timeout=timeout)
        if r.get("err"):
            self._last_err = "setLip: %s" % r["err"]
            return False
        self._lip_sig_by_mid[mid] = (str(tag or ""), len(vals))
        # v3.7.4：表一下发就登记一条「已落地待验」记录（见 _lip_mark_pending）。
        self._lip_mark_pending(mid, tag, k, len(vals))
        if vals and k is not None:
            self._lip_pend[mid] = {"tag": str(tag or ""), "vals": vals,
                                   "k": int(k), "gain": float(lip_gain)}
        else:
            self._lip_pend.pop(mid, None)
        return True

    def _lip_probe(self, mid, timeout=6.0):
        """回读页面口型证据（hasLip / lipNow / lipHookStat），供回读校验。

        只读，不写任何参数；失败返回 None，绝不影响出图。
        """
        try:
            r = self._js("(function(){ try{ var f=window.__muppetClock;"
                         " if(!f) return ''; return JSON.stringify("
                         "f(%s)); }catch(e){ return ''; } })()"
                         % json.dumps(mid), timeout=timeout)
        except Exception:
            return None
        if not r:
            return None
        try:
            return json.loads(r) if isinstance(r, str) else r
        except Exception:
            return None

    def render(self, state, timeout=25.0):
        """渲染一帧 -> (H,W,4) RGBA uint8；失败 None。

        state = {ref, layout, motion, expression, t, stage}
        产物约定（**为了能直接喂导出侧的 _paste_rgba**）：
        - 输出**透明底、未应用 layout 的立绘本体**：stage 里模型被居中、
          按立绘自身宽高铺满，导出的 layout（x/y/scale/rot）由 _paste_rgba
          再应用一次（与占位卡/图片源同一条路径，语义统一）。
        - 因此这里传的 layout 恒为「居中、scale=1、rot=0」，真正的位置
          变换交回导出侧。

        v3.6.4 **确定性修复**（实测驱动的重构，别退回自动时钟）：
        原来这里不开手动时钟，模型挂 Ticker.shared 吃墙上时间 ——
        连续抓 6 次同一参数，画面尺寸会一路 333→332→331→330 地漂，
        逐次 maxdiff=255。那意味着：
          · 呼吸/眨眼/发丝物理在两次抓图之间推进了
          · 任何像素级验证都无法归因（分不清是参数变了还是渲染在动）
          · 导出重复跑同一帧会得到不同结果
        现在改为：开手动时钟 → 每帧 seek 到**同一相位**（k 由 state.t
        决定，默认 0）→ 抓图。同一 state 永远同一画面。
        """
        with self._lock:
            if not self._bootn(): return None
            ref = state.get("ref") or ""
            if not ref: return None
            mid = state.get("modelId") or ("m%d" % (abs(hash(ref)) % 997))
            mo = str(state.get("motion") or "")
            ex = str(state.get("expression") or "")
            lip = state.get("lip")
            # 先接管时钟：之后画面只按 seek 的相位走，不吃墙上时间
            if not self.set_manual(True):
                return None
            sigc = (ref, mo, ex, id(lip) if lip else None,
                    self._lip_sig_of(lip))
            fresh = self._sig_by_mid.get(mid) != (ref, mo, ex)
            if fresh or sigc != self._last_sig:
                if not self._apply_state(ref, mo, ex, mid, timeout, lip=lip,
                                         lip_fps=state.get("lip_fps") or 30.0,
                                         lip_gain=state.get("lip_gain") or 1.0):
                    return None
                if fresh:
                    self._sig_by_mid[mid] = (ref, mo, ex)
                self._last_sig = sigc
                # 首帧宽限：纹理上传是异步的，立刻 render 会拿到空画布
                self._wait(lambda: False, 0.05)
            # 固定相位：k 由 state 给（静止渲染恒 0），dt 用帧时长
            k = int(state.get("k") or 0)
            dt_ms = float(state.get("dt") or (1000.0 / 30.0))
            r = self.seek(mid, k, dt_ms, 64)
            if r.get("err"):
                self._last_err = "render seek: %s" % r["err"]
                return None
            # 口型已由宿主页 _bindLip 挂在 internalModel 的 beforeModelUpdate
            # 事件上写入（动作结算后、coreModel.update() 烘焙前），seek 内部的
            # render 会触发钩子。这里**不再**显式写嘴——seek 之后再写会在
            # 帧尾留下脏参数（下一帧 loadParam 才还原），且旧的
            # __muppetSetMouth 顺序是为「无钩子」架构设计的，与钩子叠加会
            # 互相干扰。
            t0 = time.time()
            img = self._grab_ready(None)
            self._renders += 1
            self._last_ms = (time.time() - t0) * 1000.0
            if img is None: return None
            return self._autocrop(img)

    @staticmethod
    def _lip_sig_of(lip):
        """口型表的轻量指纹（只取几个点，别每次哈希整个列表）。"""
        if not lip:
            return None
        try:
            n = len(lip)
            if n == 0:
                return None
            return (n, round(float(lip[0]), 3),
                    round(float(lip[n // 2]), 3),
                    round(float(lip[-1]), 3))
        except Exception:
            return None

    def _write_mouth(self, mid, v, advance=False):
        """把开口度 v 写进页面 slot `mid` 的嘴部参数（并重画）。

        必须走页面内函数：宿主页是 `<script type="module">`，里面的
        slots / coreModel 都是模块私有，外部 runJavaScript 在全局作用域，
        直接写会报 "slots is not defined"（实测踩了很多次）。

        advance=False（默认）**不推进动作相位** —— 导出要求同一 k 永远
        同一画面。开 advance 会让页面 update 一次、elapsedTime 累积，
        实测连续 5 次同参数画面每次差 255。实测不 update 也能让画布吃
        到新参数（maxdiff=255），所以导出走默认。
        """
        return self._js(
            "(function(){try{"
            " var r = window.__muppetSetMouth(%s, %s, %s);"
            " return r ? JSON.stringify(r) : '';"
            "}catch(e){return 'ERR:'+(e&&e.message||e);}})()"
            % (float(v), json.dumps(mid),
               "false" if not advance else "true"), timeout=8.0)

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
            if on and not self._bootn(): return False
            if self._manual_on == bool(on):
                return True        # 幂等：连 JS 往返都省掉（页面侧同样有
                                   # noop 保护，防止把相位重置成 0）
            r = self._cmd({"cmd": "setManualClock", "args": {"on": bool(on)}})
            if r.get("err"):
                self._last_err = "set_manual: %s" % r["err"]
                return False
            self._manual_on = bool(on)
            if not on:
                self._sig_by_mid.clear()        # 退出手动：slot 状态作废，防脏签名
                # v3.7.3：口型表签名同步作废 —— 页面 lipTable 还在，但相位
                # 基准已换（不再是导出逐帧泵），留着签名会挡掉重下发
                self._lip_sig_by_mid.clear()
                self._lip_pend.clear()
            return True

    def seek(self, mid, k, dt_ms, max_steps=64, timeout=30.0, allow_restart=True):
        """把 slot `mid` 推进到第 k 步（每步 dt_ms），返回页面回执。

        `max_steps` 是单次调用的推进上限：长片段回拖时要从头重走上千步，
        一次 JS 调用连跑会把主线程独占好几秒，所以切片跑。

        `allow_restart=False` 时**禁用回拖重启**——clip 切换时 t_local 从 0
        重新开始，k 变小，页面侧会误判「回拖」而 _restartMotion。导出动作
        连续性要求新 clip 从当前相位继续走（由 FORCE playMotion 接管），
        不需要重启。此时调用方应保证已先把 s.k 归零（见 resetPhase）。"""
        return self._cmd({"cmd": "seek", "args": {
            "modelId": mid, "k": int(k), "dt": float(dt_ms),
            "max": int(max_steps), "allowRestart": bool(allow_restart)}},
            timeout=timeout)

    def model_box(self, mid, margin=24):
        """问页面要该 slot 的立绘包围盒（画布坐标 [x,y,w,h]）。

        框来自**模型几何**而不是像素测量 —— 见 stage_preview.html 里
        `__muppetBox` 的注释（用像素量会让导出不可复现）。margin 给
        呼吸/发丝留出余量。失败 None。"""
        r = self._js("window.__muppetBox ? (function(){var b=window.__muppetBox(%s,%d);"
                     "return b?JSON.stringify(b):'';})() : ''"
                     % (json.dumps(str(mid)), int(margin)), timeout=4.0)
        if not isinstance(r, str) or not r: return None
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
        对不上）。

        v3.7.5 实测记录（**为什么这里仍是同步 PNG 抓图**）：
        试过把像素编码搬到 Web Worker 后台（原始 RGBA -> deflate ->
        回传），像素确实逐位一致（maxdiff=0），但**吞吐反而更差**：
          同步 toDataURL('image/png')：页面 7.94ms/帧 -> 泵 18.7ms/帧
          Worker deflate 无损压缩：Worker 内约 20ms/帧（deflate 比 PNG
            慢），主线程虽然只占 2ms，但**导出线程要等这一帧的像素**，
            整片实测从 116s 变成 159s（泵 53.5 -> 39.0 帧/秒）。
        Worker 里 convertToBlob('image/png') 也更慢（~15ms），且
        OffscreenCanvas 颜色空间转换有损（3 个像素差 1，破坏逐位复现）。
        结论：PNG 编码放主线程反而是最快的，保留原路径。"""
        img = None
        for i in range(tries):
            img = self.grab_rgba(roi)
            if img is not None and img.size and int(img[..., 3].max()) > 8: return img
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
            if not self._bootn(): return None, None
            if not self.set_manual(True):        # 幂等；确保时钟已接管
                return None, None
            ref = state.get("ref") or ""
            if not ref: return None, None
            mid = model_id or ("m%d" % (abs(hash(ref)) % 997))
            mo = str(state.get("motion") or "")
            ex = str(state.get("expression") or "")
            # **动作连续性修复**（pump 配合）：
            # state_key 现在只按 model_path，同一模型在整条导出时间线上共用
            # 一个 slot。这里把「ref/expression 变了」（必须重载）和「只有
            # motion 变了」（用 FORCE 切换、保留相位）分开判断，避免每个
            # 新 clip 都把动作从头再播一遍。
            #
            # **方案 C（v3.7.1，动作切换生硬修复）**：再把「换模型」与
            # 「换表情」拆开。原来 sig=(ref, ex) 一体判断，expression 一变
            # 就 fresh -> loadModel 重建模型：姿态/物理/呼吸/眨眼全部归零、
            # 单次 ~0.7s。实测 hspdz_uika_mv 的 36 个 clip 边界里 33 个走了
            # 这条路 —— 切点后第 1~2 帧速度只剩片内中位的 29%（先顿），
            # 第 6~9 帧冲到 214%（再冲），就是用户看到的「生硬切换」。
            # 现在：只有 **ref 变了**才 need_load；只有 ex 变了就补发一条
            # setExpression（页面侧 keepPhase=True：不重置相位、不动时钟、
            # 不清 roi），模型不重建、动作不断档。
            sig = (ref, ex)
            prev_sig = self._sig_by_mid.get(mid)
            need_load = prev_sig is None or prev_sig[0] != ref
            ex_changed = (prev_sig is not None) and (not need_load) \
                and prev_sig[1] != ex
            fresh = need_load       # 语义收窄：fresh 现在只表示「必须重载」
            lip = state.get("lip")
            lip_gain = float(state.get("lip_gain") or 1.0)
            motion_switch = state.get("motion_switch") or False
            # ===== v3.7.3 口型表「按签名下发」=====
            # 原来只有 fresh 分支才发 setLip。但同一 model_path 在整条时间线
            # 上共用一个 slot：相邻 clip 换台词时 ref/motion/expression 往往
            # 一字不变（projects.mp4 的 c21/c39/c41 就是同 ref 同动作同表情），
            # 于是新 clip 的表**从来没下发过**，页面继续用上一个 clip 的表，
            # 按 clip 内偏移去取 —— 表长不匹配就越界、夹到末值，嘴从此定死
            # （465.7s 之后一条横线）。现在改成：表签名（lip_tag + 表长）一变
            # 就下发，与是否 fresh 无关。
            lip_tag = str(state.get("lip_tag") or "")
            lip_fps = 1000.0 / max(1.0, float(dt_ms))
            lip_sig = (lip_tag, len(lip) if lip else 0)
            lip_changed = self._lip_sig_by_mid.get(mid) != lip_sig
            if fresh:
                # 重载 = 页面模型重建：旧表签名一律作废（防脏签名挡住重发）
                had_tbl = bool(self._lip_sig_by_mid.pop(mid, None))
                if not self._apply_state(ref, mo, ex, mid, timeout,
                                         lip=lip, lip_fps=lip_fps,
                                         lip_gain=lip_gain, lip_tag=lip_tag):
                    return None, None
                self._sig_by_mid[mid] = sig
                self._lip_sig_by_mid[mid] = lip_sig
                if lip:
                    self._lip_mark_pending(mid, lip_tag, k, len(lip))
                    self._lip_pend[mid] = {"tag": lip_tag, "vals": list(lip),
                                           "k": int(k), "gain": lip_gain}
                elif had_tbl:
                    # 新 clip 没台词但旧表还挂着：显式解除，别让旧表继续画嘴
                    self._set_lip(mid, None, lip_fps, lip_gain,
                                  tag=lip_tag, k=None)
                roi = None                       # 换模型/动作：旧框作废
                # 首帧宽限：loadModel 后纹理上传是异步的，立刻 render 会
                # 拿到空画布（实测 "empty alpha (no model drawn)"）。
                self._wait(lambda: False, 0.05)
            elif motion_switch and mo:
                # 同模型、同表达式，只换 motion：FORCE 切换（优先级 3 > NORMAL 2），
                # 让新动作立刻接管而**不重置 elapsedTime/相位**。与预览
                # stage_preview.html 的 playMotion FORCE 路径一致。
                self._cmd({"cmd": "playMotion", "args": {
                    "modelId": mid, "name": mo, "fadeMs": POSE_MS, "keepPhase": True}})
                # **clip 切换连续性**：新 clip 的 t_local 从 0 开始，k 会比
                # 上一 clip 的 k 小。seek 默认会判定「回拖」触发 _restartMotion，
                # 把动作又重置了。这里**不重置 s.k**，而是把 k 映射到「当前相位
                # + 新 clip 内偏移」——动作继续推进，不从头走。
                # 具体映射在 seek 里做（allowRestart=False 时 target < s.k
                # 不重启，而是继续推进到 s.k + target）。
                if ex_changed:
                    # 动作与表情同时换：表情单独补一条，**不重载模型**
                    self._set_expression(mid, ex)
                self._sig_by_mid[mid] = sig
            elif ex_changed:
                # **方案 C**：只换表情 -> 不重载、不清 roi、不重置相位。
                # 表情按页面 expressionFadingDuration 自行淡入，动作/物理/
                # 呼吸沿着同一条 lane 继续走（这正是「生硬」的另一半）。
                self._set_expression(mid, ex)
                self._sig_by_mid[mid] = sig
            # v3.7.3：非重载路径下的口型换表（**与上面对动作/表情的分支并列，
            # 不是 else**）—— 同一 ref 连续多个 clip 就是靠这一条把新台词表
            # 送进页面。放到最后是因为它只动嘴参数，与动作/表情互不影响。
            if (not fresh) and lip_changed:
                self._set_lip(mid, lip, lip_fps, lip_gain,
                              tag=lip_tag, k=k)
            first = False
            if roi is None:
                # 两道框：几何框（与时序无关，用来抓 k=0）→ 从 k=0 的
                # alpha 收出**紧框**（与定格模式的裁剪语义一致，人物不会
                # 因为留白变多而缩水）。之后整段沿用这个紧框。
                geo = self.model_box(mid)
                if geo is None:
                    full = self._grab_ready(None)   # 老缓存页没这两个函数
                    box = self._autocrop_box(full, margin=24) if full else None
                    if box is None: return None, None
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
                r = self.seek(mid, k, dt_ms, max_steps,
                              allow_restart=not motion_switch)
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
                    # 模型已 clear：口型表签名同样作废，重发一遍当前表
                    had_tbl2 = bool(self._lip_sig_by_mid.pop(mid, None))
                    if not self._apply_state(ref, mo, ex, mid, timeout,
                                             lip=lip, lip_fps=lip_fps,
                                             lip_gain=lip_gain,
                                             lip_tag=lip_tag):
                        return None, None
                    self._sig_by_mid[mid] = sig
                    self._lip_sig_by_mid[mid] = lip_sig
                    if lip:
                        self._lip_mark_pending(mid, lip_tag, k, len(lip))
                        self._lip_pend[mid] = {"tag": lip_tag,
                                               "vals": list(lip), "k": int(k),
                                               "gain": lip_gain}
                    elif had_tbl2:
                        self._set_lip(mid, None, lip_fps, lip_gain,
                                      tag=lip_tag)
                    reloaded = True
                    self._reloads += 1
                    continue
                if r.get("reached") or int(r.get("head", -1)) >= int(k) + 1:
                    reached = True
                    break
            if not reached:
                self._last_err = "seek timeout (k=%d head=%s)" % (k, r.get("head"))
                return None, None
            # 口型由宿主页 _bindLip 的 beforeModelUpdate 钩子在烘焙前写入，
            # seek 内部的 render 已经触发过了。这里**不再**显式写嘴——旧写法
            # 会在帧尾留下脏参数、且与钩子叠加会互相干扰。
            # ===== v3.7.3 渲染侧回读 =====
            # 刚下发/切换过口型表的这一帧，顺手问页面一次：手上是哪张表、
            # 钩子写进去多少、有没有因为越界被跳过。每张表只采一次
            # （一次 JS 往返），不影响逐帧耗时；失败只丢证据，不丢画面。
            # v3.7.4：回读必须落在**有开口度的那一帧**才有判伪能力。
            # 换表帧通常是 clip 首帧（k=0），而口型表首值普遍是 0 —— 那时
            # 回读只能证明「表在手上」，证明不了「值真写进了模型」。所以
            # 现在把 pend 留在 lane 上，等到本表第一个高值帧（期望 ≥0.2）
            # 再采一次；此时 expect 非零，now 与 expect 不一致才是真问题。
            # 一直等不到高值帧的（整段静音）就不采，只留 has 层面的证据。
            pend = self._lip_pend.get(mid)
            if pend is not None:
                vals = pend.get("vals") or []
                gn = float(pend.get("gain") or 1.0)
                k0 = max(0, int(pend.get("k") or 0))
                kk = max(k0, int(k or 0))
                # 当前帧在本表网格里的期望开口度（probe 读的是当前帧的
                # lipNow，必须同帧对齐，不能用换表帧的 k）
                exp = None
                if vals and kk < len(vals):
                    exp = float(vals[kk]) * gn
                elif vals:
                    exp = float(vals[-1]) * gn
                if exp is None or exp >= 0.2:
                    self._lip_pend.pop(mid, None)
                    try:
                        pr = self._lip_probe(mid, timeout=6.0) or {}
                        with _LIP_READBACK_LOCK:
                            _LIP_READBACK[mid] = {
                                "tag": pend.get("tag") or "",
                                "has": bool(pr.get("hasLip")),
                                "now": pr.get("lipNow"),
                                "skipped": (pr.get("lipHookStat") or {}).get(
                                    "skipped"),
                                "expect": exp,
                                # 相邻帧期望值：钩子在 beforeModelUpdate 里
                                # 按 lipK 写，与抓帧用的 k 可能差 1 帧，
                                # 校验时两个都算通过，避免 ±1 帧误报
                                "expect1": (
                                    float(vals[kk - 1]) * gn
                                    if (vals and 0 < kk <= len(vals)) else None),
                                "k": kk,
                                "len": len(vals),
                            }
                    except Exception:
                        pass
            self._renders += 1
            img = self._grab_ready(roi)
            self._last_ms = (time.time() - t0) * 1000.0
            if img is None or not getattr(img, "size", 0): return None, None
            if not first: return img, None
            # k=0：把几何框收到立绘实际占用的紧框。余量取 4px：既给发丝/
            # 呼吸留动量，又贴近定格路径的 2px —— 余量差太多会让两种模式
            # 的人物尺寸差 1-2%（用户切开关会看到人物忽大忽小）。
            gx, gy, gw, gh = roi
            box = self._autocrop_box(img, margin=4)
            if box is None: return img, (gx, gy, gw, gh)
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
        if box is None: return None
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
        if img is not None: return img
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
        if pix is None or pix.isNull(): return None
        img = pix.toImage().convertToFormat(QImage.Format_RGBA8888)
        w, h = img.width(), img.height()
        if w <= 0 or h <= 0: return None
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
            self._lip_sig_by_mid.clear()
            self._lip_pend.clear()
            clear_lip_readback()        # v3.7.3：回读台账一并清（防跨工程脏证据）
            self._manual_on = None      # 页面没了，幂等镜像必须一起作废
            if p is not None:
                with contextlib.suppress(Exception): p.clear_all()
                # v3.5.11：先把内部 QWebEngineView 的页面置空再销毁，
                # 否则 Qt 报 "WebEnginePage still not deleted"
                wv = getattr(p, "view", None)
                if wv is not None:
                    with contextlib.suppress(Exception): wv.setPage(None)
                    with contextlib.suppress(Exception): wv.deleteLater()
                with contextlib.suppress(Exception):
                    p.close()
                    p.deleteLater()
                # 让 Qt 真正处理 deleteLater（否则退到解释器清理时报错）
                with contextlib.suppress(Exception):
                    from PySide6.QtWidgets import QApplication
                    app = QApplication.instance()
                    if app is not None:
                        for _ in range(8):
                            app.processEvents()

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
            with contextlib.suppress(Exception): _RENDERER.close()
            _RENDERER = None
