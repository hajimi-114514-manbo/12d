# -*- coding: utf-8 -*-
"""H3a：WebEngine 实时预览桥接核心（v3.5，WebGAL/Cubism 模型真动预览）。

背景：v3.4 的 Cubism2 预览是剪影占位卡（task-50 H2r）；H3 用 WebEngine 宿主页
+ runtime 自带 pixi-live2d-display（J2 路线 A 结论）把**真模型真动作**渲染进
预览区。本模块是桥核心：muppet:// 单源 scheme（白名单文件服务）+ QWebChannel
命令/事件（协议 docs/H3桥接协议设计.md D1/D5）+ apply_state diff 驱动同步。

红线与纪律：
  - 第三方 js（WebGAL 引擎包、live2d SDK）**零进 git**：宿主页里的 runtime /
    lib 路径全部运行时解析注入（token 替换），仓库里只有自写 html/css/js 胶水。
  - 无 WebEngine 环境：HAS_WEBENGINE=False、WebPreviewView=None，
    import 本模块**零副作用**（不建 profile/不注册 scheme/不起线程）；
    上层一律先问 available()。
  - 协议幂等：v3 是状态同步器不是事件重放器；页面按 model 槽位串行化
    （loadModel 在飞时后续命令排队），迟到命令被新状态安全覆盖。
  - 导出不回抓 WebEngine 帧（D4）：本模块只影响预览视觉，合成/导出链
    继续走 video_export.compose_frame。
  - 退出拆解（实测）：页面真加载后 deleteLater 链拆解在 offscreen 下必现
    退出 0xC0000005；正确姿势 = close/stop 后 setParent(None) 摘除引用并
    泄漏到进程结束（OS 回收）。H3b 接线时应用退出路径同法处理。

命令（v3→页面）：loadModel{modelRef,modelId} / clearModel{modelId?} /
    setTransform{modelId,x,y,scale,rot} / playMotion{modelId,name,loop,fadeMs?} /
    setExpression{modelId,name,fadeMs?}
事件（页面→v3）：boot{} / ready{modelId} / error{code,msg,ref?}
    envelope：{v,ref,evt,data}；命令 envelope：{v,id,cmd,args}。

figure 口径：video_model.figure_clips_at（I1 统一函数，不另造遍历）；
layout 口径：video_model.layout_at（x/y 中心归一、scale=fit 倍率、rot 度）。
"""
import hashlib
import json
import os
import posixpath
import re
import time

from PySide6.QtCore import Qt, QUrl, QByteArray, QBuffer, QIODevice, QObject, \
    Signal, Slot, QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout
import contextlib

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView              # noqa
    from PySide6.QtWebEngineCore import (                               # noqa
        QWebEnginePage, QWebEngineProfile, QWebEngineScript,
        QWebEngineUrlScheme, QWebEngineUrlSchemeHandler,
        QWebEngineUrlRequestJob)
    from PySide6.QtWebChannel import QWebChannel                       # noqa
    HAS_WEBENGINE = True
except ImportError:
    HAS_WEBENGINE = False

_V_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(_V_DIR, "templates", "webgal_runtime")
STAGE_TEMPLATE_DIR = os.path.join(_V_DIR, "templates", "webgal", "preview")
HOST_PAGE = "stage_preview.html"
STAGE_BASE = "muppet://stage"
STAGE_W, STAGE_H = 1280, 720          # 页面逻辑舞台（WebGAL 原生 16:9）

_MIME = {
    ".html": b"text/html; charset=utf-8",
    ".js": b"application/javascript; charset=utf-8",
    ".mjs": b"application/javascript; charset=utf-8",
    ".json": b"application/json; charset=utf-8",
    ".css": b"text/css; charset=utf-8",
    ".png": b"image/png", ".jpg": b"image/jpeg", ".jpeg": b"image/jpeg",
    ".webp": b"image/webp", ".gif": b"image/gif", ".ico": b"image/x-icon",
    ".moc": b"application/octet-stream",
    ".moc3": b"application/octet-stream",
    ".mtn": b"application/octet-stream",
    ".ttf": b"font/ttf", ".woff2": b"font/woff2",
}


def available():
    """唯一能力入口（UI 只问它）。"""
    return HAS_WEBENGINE


def runtime_ready(runtime_dir=None):
    """WebGAL 运行时是否已就位（v3.5，H3b 接线用）。

    为什么需要单独一个判断：`available()` 只看 WebEngine 可选件在不在，
    但**运行时素材（templates/webgal_runtime）是与安装包分离下载的**。
    只查 available() 会出现「实时预览开关亮着 -> 点开只有 no_runtime
    错误页 / 全空白」的坏体验（用户实测反馈）。

    判定标准（与 runtime_pack 的目录约定一致）：
      存在 index.html **且** 存在 game/config.txt（引擎必需文件）
    返回 bool；不抛。
    """
    d = runtime_dir or RUNTIME_DIR
    try:
        if not os.path.isfile(os.path.join(d, "index.html")): return False
        return os.path.isfile(os.path.join(d, "game", "config.txt"))
    except OSError:
        return False


def recommended_chromium_flags():
    """离屏/无 GPU 环境（CI、offscreen 测试）出真画面的 Chromium 旗标。
    实测矩阵（Qt 6.12 内嵌 Chromium，offscreen 平台）：
      - 默认/仅 --use-angle=swiftshader：GL 上下文创建即 CONTEXT_LOST，画面全空
      - 本组合：WebGL 走 ANGLE→SwiftShader 软渲染，模型像素真实出现
    有 GPU 的正常桌面不需要它；**本函数只返回字符串，绝不代设环境变量**
    （import 零副作用纪律）——由测试/无头宿主在 QApplication 创建前自取。"""
    return ("--disable-gpu --use-gl=angle --use-angle=swiftshader "
            "--enable-unsafe-swiftshader")


def _mime_of(path):
    low = path.lower()
    if low.endswith(".exp.json") or low.endswith(".physics.json"): return _MIME[".json"]
    return _MIME.get(os.path.splitext(low)[1], b"application/octet-stream")


def _slug(s):
    out = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", str(s or "")).strip("_")
    return out or "model"


def _find_runtime_es(runtime_dir):
    """运行时解析 index.es-*.js（哈希名不硬编码、不进 git）。"""
    with contextlib.suppress(OSError):
        for fn in sorted(os.listdir(os.path.join(runtime_dir, "assets"))):
            if fn.startswith("index.es-") and fn.endswith(".js"):
                return "runtime/assets/" + fn
    return None


def _asset_lib_core_dir():
    """素材库 core 目录（v3.4.2 起主位，定位统一委托 asset_paths：程序目录/
    asset_library > 开发机布局 > 注册表；env MUPPET_ASSET_INDEX 硬限定）。"""
    try:
        import asset_paths as _AP
        return _AP.core_dir()
    except Exception:
        env = os.environ.get("MUPPET_ASSET_INDEX", "").strip()
        if env:
            p = os.path.normpath(env)
            root = p if os.path.isdir(p) else os.path.dirname(p)
        else:
            root = os.path.normpath(os.path.join(
                _V_DIR, "..", "webgal_games", "asset_library"))
        return os.path.join(root, "lib")


def _find_lib_dir(lib_dir=None):
    """live2d SDK 目录候选链（官方 runtime 包**不含** SDK，许可件用户自备，
    见 runtime index.html「加载IIFE插件」段）：
    显式参数 → 环境变量 MUPPET_LIVE2D_DIR → runtime/lib → **素材库 lib/**
    （v3.4.2 起主位，用户装完即有）→ 本地预研原型 lib。
    含 live2d.min.js 才算命中；全无 -> None（页面报 error 降级，不崩）。"""
    cands = []
    if lib_dir: cands.append(lib_dir)
    env = os.environ.get("MUPPET_LIVE2D_DIR")
    if env: cands.append(env)
    cands.append(os.path.join(RUNTIME_DIR, "lib"))
    cands.append(_asset_lib_core_dir())
    cands.append(os.path.join(os.path.dirname(_V_DIR), "webgal_games",
                              "preview_proto", "lib"))
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "live2d.min.js")): return c
    return None


def _model_root_of(outfit_dir):
    """模型目录 -> (scheme 白名单根, key, 引用相对路径)。
    角色根含 .mtn_exp 时上提到角色根：动作/表情文件在角色级共享
    （model.json 里 ../.mtn_exp/... 引用必须落在白名单内——
    与 webgal_export H1 整角色根拷贝同理）。"""
    d = os.path.normpath(str(outfit_dir))
    if os.path.isfile(d): d = os.path.dirname(d)
    parent = os.path.dirname(d)
    root = parent if os.path.isdir(os.path.join(parent, ".mtn_exp")) else d
    key = _slug(os.path.basename(root)) + "-" + \
        hashlib.md5(os.path.normcase(root).encode("utf8")).hexdigest()[:8]
    rel = os.path.relpath(os.path.join(d, "model.json"), root)
    return root, key, rel.replace("\\", "/")


_BOOTSTRAP_JS = """
(function () {
  window.__muppetEvtQ = [];
  window.__muppetSend = function (s) {
    if (window.__bridge) { window.__bridge.postEvent(s); }
    else { window.__muppetEvtQ.push(s); }
  };
  (function wait() {
    if (typeof QWebChannel === "undefined") { setTimeout(wait, 40); return; }
    new QWebChannel(qt.webChannelTransport, function (ch) {
      window.__bridge = ch.objects.bridge;
      window.__bridge.cmdReceived.connect(function (j) {
        if (window.__muppetOnCmd) { window.__muppetOnCmd(j); }
      });
      var q = window.__muppetEvtQ; window.__muppetEvtQ = [];
      q.forEach(function (s) { window.__bridge.postEvent(s); });
    });
  })();
})();
"""

_SCHEME_REGISTERED = False


def _ensure_scheme_registered():
    """muppet:// 注册（进程一次；必须在任何 profile 创建之前）。"""
    global _SCHEME_REGISTERED
    if _SCHEME_REGISTERED or not HAS_WEBENGINE: return
    sc = QWebEngineUrlScheme(b"muppet")
    fl = QWebEngineUrlScheme.Flag
    sc.setFlags(fl.SecureScheme | fl.CorsEnabled | fl.FetchApiAllowed)
    QWebEngineUrlScheme.registerScheme(sc)
    _SCHEME_REGISTERED = True


if HAS_WEBENGINE:

    class _Bridge(QObject):
        """QWebChannel 对端：cmdReceived 下行（Python→页面）；
        postEvent 上行 Slot（页面→Python）。"""
        cmdReceived = Signal(str)

        def __init__(self, view):
            super().__init__(view)
            self._view = view

        @Slot(str)
        def postEvent(self, json_str):
            self._view._on_page_event(json_str)

    class _SchemeHandler(QWebEngineUrlSchemeHandler):
        """muppet://stage/... 单源白名单文件服务。
        / 或 /stage_preview.html -> 宿主模板（token 注入）
        /runtime/** -> runtime 根；/lib/** -> SDK 根；/model/<key>/** -> 注册根
        越界（../、盘符、未注册 key）-> fail()：Qt6 绑定无 HTTP 状态码通道，
        fetch reject 即 404 等价语义（协议 §8-6 验收按"请求失败"断言）。"""

        def __init__(self, view, parent=None):
            super().__init__(parent)
            self._view = view

        def requestStarted(self, job):
            """PySide6 把 C++ 虚函数 requestReply 暴露为可重写的
            requestStarted（实测：非信号，override 才生效）。"""
            self._on_request(job)

        def _on_request(self, job):
            path, ok = self._view.resolve(job.requestUrl())
            if not ok:
                self._view.blocked_count += 1
                job.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
                return
            data, mime = self._view.read_asset(path)
            if data is None:
                self._view.blocked_count += 1
                job.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
                return
            buf = QBuffer(job)
            buf.setData(QByteArray(data))
            buf.open(QIODevice.ReadOnly)
            job.reply(mime, buf)

    class _Page(QWebEnginePage):
        """内部页：转发 JS console（诊断链路；测试排障靠它）。"""
        jsConsole = Signal(str)

        def javaScriptConsoleMessage(self, level, message, line, source):
            self.jsConsole.emit("%s:%s %s" % (source, line, message))

    class WebPreviewView(QWidget):
        """实时预览视图：QWebEngineView + muppet:// 文件服务 + WebChannel 桥
        + apply_state diff 同步（scrub 80ms 防抖）。"""

        eventReceived = Signal(str)          # 页面事件 json（测试/上层观测口）
        pageReady = Signal(bool)             # 宿主页 loadFinished 透传
        jsLog = Signal(str)                  # 页面 console 转发（诊断用）
        previewDisabled = Signal()           # J3 §8-7：连续机制级 error
                                             # 达阈值 -> 会话内自动降级
                                             # （UI 收信号后关开关置灰，
                                             # 会话内不再弹，见 disabled()）
        DEBOUNCE_MS = 80
        ERROR_STREAK_LIMIT = 3               # §8-7 阈值：连续机制级 error 数
        # 机制级 = 装载/执行故障（用户改参数救不回来）；unknownMotion/
        # unknownExpression/cmdUnsupported 属参数级，不计数也不重置
        # （避免正常试错误降级）；no_runtime 走 start() 既有降级路径。
        MECHANISM_ERROR_CODES = ("boot_failed", "no_live2d_sdk", "cmd_failed")

        def __init__(self, runtime_dir=None, lib_dir=None, parent=None):
            super().__init__(parent)
            _ensure_scheme_registered()
            self._runtime = os.path.normpath(runtime_dir or RUNTIME_DIR)
            self._lib = _find_lib_dir(lib_dir)
            self._model_roots = {}
            self.blocked_count = 0
            self._sent = []                   # 命令 envelope 记录（测试断言用）
            self._events = []                 # 页面事件记录
            self._figstate = {}               # clip_id -> 已下发状态（diff 基）
            self._pending_apply = None
            self._last_apply_ms = 0.0         # 上次真正落地的时刻（节流基准）
            self._ids = 0
            # v3.5.2：背景/字幕/滤镜三层的 diff 基（不重复发同样的内容）
            self._bg_url = None
            self._sub_key = None
            self._filter_key = None
            self._aid = None                  # 当前幕（多幕工程由 wb 设置）
            self._degraded = None             # 不可恢复降级原因（no_runtime /
                                              # error_streak，J3 §8-7）
            self._err_streak = 0              # 连续机制级 error 计数（ready 清零）
            lay = QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            self.view = QWebEngineView(self)
            lay.addWidget(self.view)
            self.profile = QWebEngineProfile(self)          # 匿名 off-the-record
            self.profile.setPersistentCookiesPolicy(
                QWebEngineProfile.NoPersistentCookies)
            self.handler = _SchemeHandler(self, self.profile)
            self.profile.installUrlSchemeHandler(b"muppet", self.handler)
            self.page = _Page(self.profile, self.profile)   # 父=profile：销毁顺序正确
            self.view.setPage(self.page)
            self.page.loadFinished.connect(self.pageReady)
            self.page.jsConsole.connect(self.jsLog)
            self.bridge = _Bridge(self)
            self.channel = QWebChannel(self.page)
            self.channel.registerObject("bridge", self.bridge)
            self.page.setWebChannel(self.channel)
            self._inject_scripts()
            self._deb = QTimer(self)
            self._deb.setSingleShot(True)
            self._deb.setInterval(self.DEBOUNCE_MS)
            self._deb.timeout.connect(self._flush_apply)

        # ---------- 装载 ----------
        def _inject_scripts(self):
            ch = QWebEngineScript()
            ch.setName("qwebchannel")
            ch.setSourceUrl(QUrl("qrc:///qtwebchannel/qwebchannel.js"))
            ch.setInjectionPoint(QWebEngineScript.DocumentCreation)
            ch.setWorldId(QWebEngineScript.MainWorld)
            ch.setRunsOnSubFrames(False)
            bs = QWebEngineScript()
            bs.setName("muppet-boot")
            bs.setSourceCode(_BOOTSTRAP_JS)
            bs.setInjectionPoint(QWebEngineScript.DocumentCreation)
            bs.setWorldId(QWebEngineScript.MainWorld)
            bs.setRunsOnSubFrames(False)
            self.page.scripts().insert(ch)
            self.page.scripts().insert(bs)

        def start(self):
            """装载宿主页。runtime 缺失 -> 本地合成 error 事件（降级不抛）。"""
            if self._degraded: return
            if not _find_runtime_es(self._runtime):
                self._degraded = "no_runtime"
                self._record_event({"v": 1, "evt": "error",
                                    "data": {"code": "no_runtime",
                                             "msg": "WebGAL runtime 未安装: %s"
                                                    % self._runtime}})
                return
            self.view.load(QUrl(STAGE_BASE + "/" + HOST_PAGE))

        # ---------- scheme 资源 ----------
        def resolve(self, qurl):
            """QUrl -> (路径键 or 本地绝对路径, ok)。
            越界（.. / 绝对盘符 / 未注册 key / 非白名单根 / 非常规 host）
            -> (None, False)。纯函数化便于单测直查。"""
            if qurl.scheme() != "muppet" or qurl.host() != "stage": return None, False
            path = posixpath.normpath("/" + qurl.path().lstrip("/"))
            if ".." in path.split("/"): return None, False
            if path in ("/", "/" + HOST_PAGE): return "@host", True
            if path.startswith("/runtime/"):
                rel = path[len("/runtime/"):]
                return (self._under(self._runtime, rel), True) \
                    if self._under_ok(self._runtime, rel) else (None, False)
            if path.startswith("/lib/"):
                rel = path[len("/lib/"):]
                if not self._lib: return None, False
                return (self._under(self._lib, rel), True) \
                    if self._under_ok(self._lib, rel) else (None, False)
            if path.startswith("/model/"):
                key, _s, sub = path[len("/model/"):].partition("/")
                root = self._model_roots.get(key)
                if root is None or not sub: return None, False
                return (self._under(root, sub), True) \
                    if self._under_ok(root, sub) else (None, False)
            # v3.5.11：/game/** -> runtime 自带 game 资源（config.txt 等）。
            # 离屏渲染 L2D 时会 import 运行时主包，主包启动即请求 game/config.txt
            # 等文件；缺这条路由会 AxiosError: Network Error -> boot 中断 ->
            # window.PIXI 永不建立（实测根因）。
            if path.startswith("/game/"):
                rel = path[len("/game/"):]
                gdir = os.path.join(self._runtime, "game")
                return (self._under(gdir, rel), True) \
                    if self._under_ok(gdir, rel) else (None, False)
            return None, False

        @staticmethod
        def _under_ok(root, rel):
            if not rel: return False
            full = os.path.realpath(
                os.path.join(root, rel.replace("/", os.sep)))
            rr = os.path.realpath(root)
            return full.startswith(rr + os.sep) and os.path.isfile(full)

        @staticmethod
        def _under(root, rel):
            return os.path.join(root, rel.replace("/", os.sep))

        def read_asset(self, key_path):
            """resolve 产物 -> (bytes, mime)；宿主模板做 runtime/lib 路径注入。"""
            if key_path == "@host":
                p = os.path.join(STAGE_TEMPLATE_DIR, HOST_PAGE)
                if not os.path.isfile(p): return None, None
                html = open(p, encoding="utf8").read()
                es = _find_runtime_es(self._runtime)
                inject = {
                    "__MUPPET_ES__": json.dumps(
                        STAGE_BASE + "/" + es if es else None),
                    "__MUPPET_LIB2__": json.dumps(
                        STAGE_BASE + "/lib/live2d.min.js"
                        if self._lib else None),
                    "__MUPPET_LIB4__": json.dumps(
                        STAGE_BASE + "/lib/live2dcubismcore.min.js"
                        if self._lib else None),
                    "__MUPPET_STAGE__": json.dumps(STAGE_BASE),
                }
                for k, v in inject.items():
                    html = html.replace(k, v)
                return html.encode("utf8"), _MIME[".html"]
            try:
                return open(key_path, "rb").read(), _mime_of(key_path)
            except OSError:
                return None, None

        # ---------- 模型注册 ----------
        def register_model_dir(self, outfit_dir):
            root, key, rel = _model_root_of(outfit_dir)
            self._model_roots[key] = root
            return "model/%s/%s" % (key, rel)

        # ---------- 状态同步（diff 驱动，D5） ----------
        def apply_state(self, proj, t, force=False, playing=False):
            """入口：按 figure_clips_at 口径算在场立绘，节流后只发差量。

            **节流 = 前沿 + 尾沿，不是纯防抖**（v3.6.3 修）。

            纯防抖（每次调用都 `start()` 重置单次定时器）在播放时会被
            彻底饿死：`_advance` 每帧（1000/fps ≈ 33ms）调一次本函数，
            而 DEBOUNCE_MS=80 > 33ms，于是定时器被无限重启、永远不到期
            -> 播放期间实时预览**一帧都不动**，只有停播放（按空格）之后
            80ms 才补推一次。—— 用户报「实时预览要按空格才刷新」即此。

            现在的规则：
              · playing=True（正在播放）-> 不节流，每帧都推（画面要跟手）
              · 距上次落地 ≥ 节流间隔 -> **立刻**推（前沿）
              · 否则挂尾沿定时器；**已在计时就不再重置**，保证最多
                一个间隔一定落地一帧最新状态（不丢帧：推的是
                _pending_apply 里最新的那份）。
            force=True 跳过节流（工程切换/离场急清/页面就绪首推）。
            """
            if self._degraded: return
            if force:
                self._deb.stop()
                self._pending_apply = (proj, t)
                self._flush_apply()
                return
            self._pending_apply = (proj, t)
            if playing:
                # 播放中每帧都要跟手；scrub 才需要 80ms 节流
                self._deb.stop()
                self._flush_apply()
                return
            since = (time.perf_counter() - self._last_apply_ms) * 1000.0
            if since >= self.DEBOUNCE_MS:
                self._deb.stop()
                self._flush_apply()
            elif not self._deb.isActive():
                self._deb.start(max(1, int(self.DEBOUNCE_MS - since)))

        def _flush_apply(self):
            """把「最新」的待推状态落地一次（定时器尾沿与前沿共用）。"""
            self._deb.stop()
            if self._pending_apply:
                proj, t = self._pending_apply
                self._pending_apply = None
                self._last_apply_ms = time.perf_counter()
                self._do_apply(proj, t)

        def _do_apply(self, proj, t):
            import video_model as VM
            figs = VM.figure_clips_at(proj, t)
            target = {}
            # v3.6.9：**口型/动作连贯性**——slot 按「模型路径 + 轨道」派生，
            # 不再按 clip id（详见 VM.figure_slot_id 的注释）。
            # 同一角色跨片段共用一个 slot：模型实例不重建，动作只在 motion
            # 字符串真的变了才切。
            slots_map = VM.figure_slot_map(proj)
            for c in figs:
                cid = str(c.get("id"))
                src = c.get("source") or {}
                path = src.get("path") or ""
                ref = self.register_model_dir(path)
                slot_id = slots_map.get(cid) or cid
                lay = VM.layout_at(c)
                target[slot_id] = {
                    "ref": ref,
                    "layout": (round(float(lay["x"]), 4),
                               round(float(lay["y"]), 4),
                               round(float(lay["scale"]), 4),
                               round(float(lay["rot"]), 3)),
                    "motion": str(c.get("motion") or ""),
                    "expression": str(c.get("expression") or ""),
                    "clip_id": cid,
                }
            old = self._figstate
            for cid, st in target.items():
                prev = old.get(cid)
                if prev is None:
                    self._send("loadModel", {"modelRef": st["ref"],
                                             "modelId": cid})
                    self._send_transform(cid, st["layout"])
                    if st["motion"]:
                        self._send("playMotion", {"modelId": cid,
                                                  "name": st["motion"],
                                                  "loop": True})
                    if st["expression"]:
                        self._send("setExpression", {"modelId": cid,
                                                     "name": st["expression"]})
                else:
                    if prev["layout"] != st["layout"]:
                        self._send_transform(cid, st["layout"])
                    if prev["motion"] != st["motion"] and st["motion"]:
                        self._send("playMotion", {"modelId": cid,
                                                  "name": st["motion"],
                                                  "loop": True})
                    if (prev["expression"] != st["expression"]
                            and st["expression"]):
                        self._send("setExpression", {"modelId": cid,
                                                     "name": st["expression"]})
            for cid in old:
                if cid not in target: self._send("clearModel", {"modelId": cid})
            self._figstate = target
            # ---- v3.5.2：背景 / 字幕 / 滤镜三层 ----
            # 原来只推立绘，用户看到「实时预览没有背景和文字和滤镜」。
            self._apply_bg(proj, t)
            self._apply_subtitle(proj, t)
            self._apply_filter(proj, t)
            # v3.6.5：**先同步播放头时间**再算口型 —— 页面按工程时间取
            # 开口度（墙上时钟会和按片段内时间烘好的表错位）。
            self._send("setTime", {"t": round(float(t), 4)})
            self._apply_lip(proj, t)
            self._apply_camera(proj, t)      # v3.6.2：镜头动画

        # ---------- v3.6.4：口型匹配 ----------
        def _apply_lip(self, proj, t):
            """把每个立绘的口型绑定算成开口度表下发（diff，不重复发）。

            **vals 是「片段内相对时间」的序列**，因为页面的 seek(k, dt)
            也是从片段起点算 k —— 两边必须同一基准，否则嘴会整体错位。
            片段起点 = 立绘 clip 的 start（模型就是那一刻加载的）。
            """
            with contextlib.suppress(Exception):
                import video_model as VM
                import lip_sync as LS
                aid = getattr(self, "_aid", None)
                binds = VM.lip_bindings(proj, aid)
                if not hasattr(self, "_lip_sig"):
                    self._lip_sig = {}
                sig_now = {}
                fps = float((proj or {}).get("fps") or 30.0)
                figs = {str(c.get("id")): c
                        for c in VM.figure_clips_at(proj, t)}
                # 只处理**当前在场**的立绘：不在场的模型页面上也没有，
                # 发了也白发（还占 diff 表）
                # v3.6.9：立绘按 slot 复用后，口型也必须按 **slot** 下发，
                # 否则 setLip 发到一个不存在的 modelId（clip id）上，嘴就不动。
                slots_map = VM.figure_slot_map(proj)
                for cid, lp in binds.items():
                    fig = figs.get(cid)
                    if fig is None:
                        continue
                    slot_id = slots_map.get(cid) or cid
                    vc = VM.voice_clip_by_id(proj, lp.get("voice"), aid)
                    if vc is None:
                        continue
                    fig_start = float(fig.get("start") or 0.0)
                    # 语音在片段内的偏移：语音起点 - 立绘起点（可为负，
                    # 表示这句话在人物入场之前就说了）
                    off = float(vc.get("t") or 0.0) - fig_start
                    dur = float(fig.get("dur") or 0.0)
                    env = LS.envelope(vc.get("path"), "")
                    if env is None:
                        continue
                    vals = env.bake(fps, dur, offset=-off)
                    sig = (lp.get("voice"), round(off, 3), round(dur, 3),
                           round(float(lp.get("gain") or 1.0), 3),
                           len(vals), round(fig_start, 3))
                    sig_now[slot_id] = sig
                    if self._lip_sig.get(slot_id) == sig:
                        continue                      # 没变，不重发
                    self._send("setLip", {
                        "modelId": slot_id, "vals": vals, "fps": fps,
                        "gain": float(lp.get("gain") or 1.0),
                        # v3.6.5：片段起点 —— 页面用它把**工程时间**换算成
                        # 片段内时间。不下发的话页面只能拿墙上时钟猜，
                        # 嘴会越播越偏（用户报「做动作时嘴闭上不对口型」）。
                        "start": fig_start})
                # 解除：上一轮有、这轮没有的
                for sid in list(self._lip_sig):
                    if sid not in sig_now:
                        self._send("setLip", {"modelId": sid, "vals": []})
                self._lip_sig = sig_now

        # ---------- v3.5.2：三层推送（各自 diff，不重复发）----------
        def _apply_bg(self, proj, t):
            """当前时刻生效的最底层视频 clip 图片 -> setBackground。

            取「最底轨的第一个 image clip」作为背景（与导出层
            `changeBg` 语义一致：背景永远是底图）。
            资源走已有的 /model/<key>/ 静态通道（register_model_dir 同一个
            注册表，只是这里注册的是图片**所在目录**），不新开路由。
            """
            with contextlib.suppress(Exception):
                import video_model as VM
                want = ""
                for tr in VM._tracks_of(proj, getattr(self, "_aid", None)):
                    if tr.get("kind") != "video" or tr.get("hidden"): continue
                    for c in (tr.get("clips") or []):
                        s0 = float(c.get("start") or 0.0)
                        d0 = float(c.get("dur") or 0.0)
                        if not (s0 - 1e-6 <= t < s0 + d0 - 1e-6): continue
                        src = c.get("source") or {}
                        if str(src.get("kind") or "") != "image": continue
                        path = str(src.get("path") or "")
                        if path and os.path.isfile(path):
                            want = self._register_file(path)
                        break
                    if want: break
                if want == getattr(self, "_bg_url", None): return
                self._bg_url = want
                self._send("setBackground", {"url": want})

        def _register_file(self, path):
            """单个文件 -> 可被页面请求的相对 URL（复用 /model 通道）。"""
            try:
                d = os.path.dirname(os.path.abspath(path))
                base = os.path.basename(path)
                key = hashlib.sha1(
                    d.encode("utf-8", "ignore")).hexdigest()[:10]
                self._model_roots[key] = d
                return "model/%s/%s" % (key, base)
            except Exception:
                return ""

        def _apply_subtitle(self, proj, t):
            """当前时刻生效的台词 -> setSubtitle（取最后一条并集，与舞台一致）"""
            with contextlib.suppress(Exception):
                import video_model as VM
                view = VM.act_view(proj, getattr(self, "_aid", None))
                cur = None
                for d in (view.get("dialogue") or []):
                    s0 = float(d.get("t") or 0.0)
                    d0 = float(d.get("dur") or 0.0)
                    if s0 - 1e-6 <= t < s0 + d0 - 1e-6: cur = d
                key = ""
                if cur:
                    key = "%s\x00%s" % (cur.get("who") or "",
                                       cur.get("text") or "")
                if key == getattr(self, "_sub_key", None): return
                self._sub_key = key
                who = (cur or {}).get("who") or ""
                txt = (cur or {}).get("text") or ""
                self._send("setSubtitle", {"who": str(who), "text": str(txt)})

        def _filter_effects_cache(self, view):
            """把 setTransform 特效**预解析一次**并缓存（性能关键）。

            原来 _apply_filter 每帧都对全部 effects 跑正则+JSON 解析，
            而 apply_state 是每帧调用的。一个 220 条特效表 + 长工程的
            时间轴，逐帧解析就是用户说的「加载滤镜很卡」。

            缓存键用 `id(view["effects"])` + 长度 + 首尾元素 id：
            工程被改动时 video_model 会换列表对象/改长度，能可靠失效；
            纯播放（时间游标移动）不会变，直接命中。
            """
            import re as _re
            import json as _json
            effs = view.get("effects") or []
            try:
                key = (id(effs), len(effs),
                       id(effs[0]) if effs else 0,
                       id(effs[-1]) if effs else 0)
            except Exception:
                key = None
            if key is not None and key == getattr(self, "_fx_cache_key", None):
                return self._fx_cache
            parsed = []
            for ef in effs:
                try:
                    et = float(ef.get("t") or 0.0)
                except (TypeError, ValueError):
                    continue
                cmd = str(ef.get("cmd") or "")
                if not cmd.startswith("setTransform"): continue
                m = _re.search(r"\{.*\}", cmd)
                if not m: continue
                try:
                    params = _json.loads(m.group(0))
                except Exception:
                    continue
                if not isinstance(params, dict): continue
                tm = _re.search(r"-target=(\S+?)(?:\s|;|$)", cmd)
                target = tm.group(1) if tm else "bg-main"
                parsed.append((et, target.startswith("bg"), params))
            parsed.sort(key=lambda x: x[0])
            self._fx_cache_key = key
            self._fx_cache = parsed
            return parsed

        def _apply_filter(self, proj, t):
            """当前时刻生效的滤镜 -> setFilter（**按 target 分层**）。

            v3.6.2：**两条数据源都要读**。这是个真实断点（用户报「滤镜在
            实时预览里不生效」）：
              · proj["effects"] —— setTransform 指令（特效页插入的）
              · clip["filter"]  —— 滤镜页/电影滤镜套在片段上的参数
            此前只读前者，而滤镜页写的正是后者，两边完全没打通，用户套完
            滤镜在预览里看不到任何变化。

            v3.6.5 性能：effects 解析结果按「列表未变」缓存（见
            _filter_effects_cache），本函数退化成一次线性扫描 + 少量比较，
            不再逐帧正则+JSON。
            """
            try:
                import json as _json
                import video_model as VM
                aid = getattr(self, "_aid", None)
                view = VM.act_view(proj, aid)
                bg = {}
                fig = {}

                # ---- ① 先应用 setTransform 特效指令（已预解析）----
                # parsed 按 t 升序，扫到 > t 就可以停（原实现扫全表）
                for et, is_bg, params in self._filter_effects_cache(view):
                    if et > t + 1e-6: break
                    if is_bg: bg = params
                    else: fig = params

                # ---- ② 再叠加当前时刻在场片段的 clip["filter"] ----
                # 后写覆盖：片段上的滤镜是用户在滤镜页/电影滤镜里最近套的，
                # 应当盖过更早的 effects 指令。
                eps = 1e-6
                for tr in VM._tracks_of(proj, aid):
                    if not isinstance(tr, dict) or tr.get("hidden"): continue
                    if tr.get("kind") != "video": continue
                    for c in (tr.get("clips") or []):
                        f = c.get("filter")
                        if not isinstance(f, dict) or not f: continue
                        try:
                            s0 = float(c.get("start") or 0.0)
                            d0 = float(c.get("dur") or 0.0)
                        except (TypeError, ValueError):
                            continue
                        if not (s0 - eps <= t < s0 + d0 - eps): continue
                        # 判断是背景还是立绘（与导出器/工作台同口径）
                        is_bg = False
                        try:
                            nm = str(tr.get("name") or "").upper()
                            lay = VM.layout_at(c)
                            src = c.get("source") or {}
                            is_bg = nm.startswith("BG") or (
                                bool(lay.get("cover"))
                                and src.get("kind") == "image")
                        except Exception:
                            is_bg = False
                        if is_bg: bg = dict(f)
                        else: fig = dict(f)

                want = {"bg": bg, "fig": fig}
                key = _json.dumps(want, sort_keys=True)
                if key == getattr(self, "_filter_key", None): return
                self._filter_key = key
                self._send("setFilter", want)
            except Exception:
                pass

        def _camera_effects_cache(self, view):
            """预解析 setTempAnimation 关键帧（与 _apply_filter 同款缓存思路）。

            返回按 t 升序的 [(t0, kfs, total_ms, times)]，times 预先算好
            （见原注释里的引擎 v1 关键帧语义）。原来这段计算每帧都做一遍，
            加上正则+JSON 解析，是播放卡顿的来源之一。
            """
            import re as _re
            import json as _json
            effs = view.get("effects") or []
            try:
                key = (id(effs), len(effs),
                       id(effs[0]) if effs else 0,
                       id(effs[-1]) if effs else 0)
            except Exception:
                key = None
            if key is not None and key == getattr(self, "_cam_cache_key", None):
                return self._cam_cache
            out = []
            for ef in effs:
                try:
                    et = float(ef.get("t") or 0.0)
                except (TypeError, ValueError):
                    continue
                cmd = str(ef.get("cmd") or "")
                if not cmd.startswith("setTempAnimation"): continue
                m = _re.search(r"\[.*\]", cmd, _re.S)
                if not m: continue
                try:
                    kfs = _json.loads(m.group(0))
                except Exception:
                    continue
                if not (isinstance(kfs, list) and kfs): continue
                # 引擎 v1 关键帧语义：kf0 是起始状态（在 t0 生效），
                # kf1..n 是后续状态，从上一帧过渡过来耗时本帧 duration 毫秒。
                # times[0]=0（kf0 生效），times[i]=times[i-1]+kfs[i].duration。
                total_ms = 0.0
                times = [0.0]
                for i in range(1, len(kfs)):
                    try:
                        d = float(kfs[i].get("duration") or 0.0)
                    except (TypeError, ValueError):
                        d = 0.0
                    total_ms += d
                    times.append(total_ms)
                # 注意：**不要**在这里过滤 total_ms<=0 —— 单关键帧 /
                # duration 全 0 的镜头特效在引擎里是「瞬切到该状态」，
                # 原实现会走到后面的 `if total <= 1e-9: return` 分支，
                # 但**仍会先被 pick 中**（从而 _cam_key 有值）。
                # 提前 continue 会让这类特效完全不进候选（实测回归：
                # realtime_preview_test 3a 推送 setCamera 从 1 条变 0 条）。
                out.append((et, kfs, total_ms, times))
            out.sort(key=lambda x: x[0])
            self._cam_cache_key = key
            self._cam_cache = out
            return out

        def _apply_camera(self, proj, t):
            """当前时刻生效的 setTempAnimation -> setCamera（镜头动画）。

            v3.6.2 新增：206 个演出特效里 149 个是 setTempAnimation（推近/
            拉远/摇摄/震动/缩放），原来预览**直接跳过**，用户看不到。
            这里解析出该时刻正在播放的那条,把进度归一成 0..1 交给页面
            用 CSS transform 模拟（scale/translate/rotate）。
            多条同时生效时取最后一条（与引擎覆盖语义一致）。

            v3.6.5 性能：关键帧数组与段时长同样按「列表未变」缓存，
            不再逐帧正则+JSON（与 _apply_filter 同一套 cache 键）。
            """
            try:
                import json as _json
                import video_model as VM
                view = VM.act_view(proj, getattr(self, "_aid", None))
                # 走 _apply_filter 建好的缓存：它把 setTransform 过滤掉了，
                # 这里单独缓存 setTempAnimation（口径不同、不能混用）
                best = self._camera_effects_cache(view)
                # 取 t 之前最后一条（缓存已按 t 升序，扫到 >t 即停）
                picked = None
                for et, kfs, total_ms, times in best:
                    if et > t + 1e-6: break
                    picked = (et, kfs, total_ms, times)
                if not picked:
                    if getattr(self, "_cam_key", None) is not None:
                        self._cam_key = None
                        self._send("setCamera", {"active": False})
                    return
                t0, kfs, total_ms, times = picked
                total = total_ms / 1000.0
                if total <= 1e-9: return
                el = max(0.0, min(t - t0, total))    # 结束后保持末帧
                el_ms = el * 1000.0
                # 段定位：落在 [times[i], times[i+1]) 的段 i（段 i 从 kfs[i]
                # 过渡到 kfs[i+1]）；到末尾时取最后一段、p=1。
                idx = 0
                for i in range(len(times) - 1):
                    if el_ms >= times[i] - 1e-6: idx = i
                seg_start = times[idx]
                seg_end = (times[idx + 1] if idx + 1 < len(times)
                           else total_ms)
                seg_dur = seg_end - seg_start
                if seg_dur <= 1e-9: p = 1.0
                else: p = min(1.0, max(0.0, (el_ms - seg_start) / seg_dur))

                def _val(key, sub=None):
                    """取该段的属性值（首段无对应键时沿用上一个关键帧）。"""
                    for i in range(min(idx, len(kfs) - 1), -1, -1):
                        k = kfs[i]
                        v = k.get(key)
                        if sub:
                            if isinstance(v, dict) and v.get(sub) is not None:
                                return float(v.get(sub))
                        elif v is not None:
                            with contextlib.suppress((TypeError, ValueError)): return float(v)
                    return None

                def _lerp(a, b, r):
                    if a is None: return b
                    if b is None: return a
                    return a + (b - a) * r

                nxt = min(idx + 1, len(kfs) - 1)

                def _pair(key, sub=None):
                    a = _val(key, sub)
                    b = None
                    k2 = kfs[nxt]
                    v = k2.get(key)
                    if sub:
                        if isinstance(v, dict) and v.get(sub) is not None:
                            b = float(v.get(sub))
                    elif v is not None:
                        try:
                            b = float(v)
                        except (TypeError, ValueError):
                            b = None
                    return _lerp(a, b, p)

                sx = _pair("scale", "x")
                sy = _pair("scale", "y")
                px = _pair("position", "x")
                py = _pair("position", "y")
                rot = _pair("rotation")
                blur = _pair("blur")
                alpha = _pair("alpha")
                cam = {"active": True, "p": round(p, 4)}
                if sx is not None: cam["scaleX"] = round(sx, 4)
                if sy is not None: cam["scaleY"] = round(sy, 4)
                # position 是「舞台百分比偏移」（引擎里 ±50 约等于半个屏）
                if px is not None: cam["x"] = round(px / STAGE_W * 100.0, 3)
                if py is not None: cam["y"] = round(py / STAGE_H * 100.0, 3)
                if rot is not None: cam["rot"] = round(rot, 3)
                if blur is not None: cam["blur"] = round(max(0.0, min(30.0, blur)), 3)
                if alpha is not None: cam["alpha"] = round(max(0.0, min(1.0, alpha)), 3)
                key = _json.dumps(cam, sort_keys=True)
                if key == getattr(self, "_cam_key", None): return
                self._cam_key = key
                self._send("setCamera", cam)
            except Exception:
                pass

        def _send_transform(self, cid, lay):
            self._send("setTransform", {"modelId": cid, "x": lay[0],
                                        "y": lay[1], "scale": lay[2],
                                        "rot": lay[3]})

        def clear_all(self):
            self._figstate = {}
            self._pending_apply = None
            self._send("clearModel", {"modelId": ""})

        def _send(self, cmd, args):
            self._ids += 1
            env = {"v": 1, "id": self._ids, "cmd": cmd, "args": args}
            self._sent.append(env)
            self.bridge.cmdReceived.emit(json.dumps(env, ensure_ascii=False))

        # ---------- 页面事件 ----------
        def _on_page_event(self, json_str):
            try:
                e = json.loads(json_str)
            except Exception:
                return
            if isinstance(e, dict): self._record_event(e)

        def _record_event(self, e):
            self._events.append(e)
            self.eventReceived.emit(json.dumps(e, ensure_ascii=False))
            # J3 §8-7（缺口补齐，cpu-engine，lead 批准动本文件）：连续
            # 机制级 error 达阈值 -> 置降级标志 + 发 previewDisabled 一次。
            # 降级后 start()/apply_state 走 _degraded 既有零流量分支，
            # 会话内不重试不重弹（UI 侧收信号后置灰开关，用 disabled() 查询）。
            try:
                if not isinstance(e, dict): return
                evt = e.get("evt")
                if evt == "error":
                    code = str((e.get("data") or {}).get("code") or "")
                    if code in self.MECHANISM_ERROR_CODES: self._err_streak += 1
                    else: return              # 参数级：不计数不重置
                elif evt == "ready":
                    self._err_streak = 0    # 任一模型装载成功即重置
                    return
                else: return
                if (self._err_streak >= self.ERROR_STREAK_LIMIT
                        and not self._degraded):
                    self._degraded = "error_streak"
                    self.previewDisabled.emit()
            except Exception:
                pass                        # 降级机制自身绝不抛

        def disabled(self):
            """UI 侧置灰查询（J3 §8-7）：True=本会话实时预览已降级。"""
            return bool(self._degraded)

        def events_of(self, evt):
            return [e for e in self._events if e.get("evt") == evt]

        # ---------- 截图（验收/目检用；不进导出链，D4） ----------
        def grab_shot(self, out_path):
            img = self.view.grab()
            return bool(img.save(out_path))

else: WebPreviewView = None
