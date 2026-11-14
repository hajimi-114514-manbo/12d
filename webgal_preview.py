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

from PySide6.QtCore import Qt, QUrl, QByteArray, QBuffer, QIODevice, QObject, \
    Signal, Slot, QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout

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
    if low.endswith(".exp.json") or low.endswith(".physics.json"):
        return _MIME[".json"]
    return _MIME.get(os.path.splitext(low)[1], b"application/octet-stream")


def _slug(s):
    out = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", str(s or "")).strip("_")
    return out or "model"


def _find_runtime_es(runtime_dir):
    """运行时解析 index.es-*.js（哈希名不硬编码、不进 git）。"""
    try:
        for fn in sorted(os.listdir(os.path.join(runtime_dir, "assets"))):
            if fn.startswith("index.es-") and fn.endswith(".js"):
                return "runtime/assets/" + fn
    except OSError:
        pass
    return None


def _find_lib_dir(lib_dir=None):
    """live2d SDK 目录候选链（官方 runtime 包**不含** SDK，许可件用户自备，
    见 runtime index.html「加载IIFE插件」段）：
    显式参数 → 环境变量 MUPPET_LIVE2D_DIR → runtime/lib → 本地预研原型 lib。
    含 live2d.min.js 才算命中；全无 -> None（页面报 error 降级，不崩）。"""
    cands = []
    if lib_dir:
        cands.append(lib_dir)
    env = os.environ.get("MUPPET_LIVE2D_DIR")
    if env:
        cands.append(env)
    cands.append(os.path.join(RUNTIME_DIR, "lib"))
    cands.append(os.path.join(os.path.dirname(_V_DIR), "webgal_games",
                              "preview_proto", "lib"))
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "live2d.min.js")):
            return c
    return None


def _model_root_of(outfit_dir):
    """模型目录 -> (scheme 白名单根, key, 引用相对路径)。
    角色根含 .mtn_exp 时上提到角色根：动作/表情文件在角色级共享
    （model.json 里 ../.mtn_exp/... 引用必须落在白名单内——
    与 webgal_export H1 整角色根拷贝同理）。"""
    d = os.path.normpath(str(outfit_dir))
    if os.path.isfile(d):
        d = os.path.dirname(d)
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
    if _SCHEME_REGISTERED or not HAS_WEBENGINE:
        return
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
        DEBOUNCE_MS = 80

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
            self._ids = 0
            self._degraded = None             # 不可恢复降级原因（no_runtime）
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
            if self._degraded:
                return
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
            if qurl.scheme() != "muppet" or qurl.host() != "stage":
                return None, False
            path = posixpath.normpath("/" + qurl.path().lstrip("/"))
            if ".." in path.split("/"):
                return None, False
            if path in ("/", "/" + HOST_PAGE):
                return "@host", True
            if path.startswith("/runtime/"):
                rel = path[len("/runtime/"):]
                return (self._under(self._runtime, rel), True) \
                    if self._under_ok(self._runtime, rel) else (None, False)
            if path.startswith("/lib/"):
                rel = path[len("/lib/"):]
                if not self._lib:
                    return None, False
                return (self._under(self._lib, rel), True) \
                    if self._under_ok(self._lib, rel) else (None, False)
            if path.startswith("/model/"):
                key, _s, sub = path[len("/model/"):].partition("/")
                root = self._model_roots.get(key)
                if root is None or not sub:
                    return None, False
                return (self._under(root, sub), True) \
                    if self._under_ok(root, sub) else (None, False)
            return None, False

        @staticmethod
        def _under_ok(root, rel):
            if not rel:
                return False
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
                if not os.path.isfile(p):
                    return None, None
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
        def apply_state(self, proj, t, force=False):
            """入口：按 figure_clips_at 口径算在场立绘，diff 后只发差量。
            force=True 跳过防抖（工程切换/离场急清）。"""
            if self._degraded:
                return
            if force:
                self._do_apply(proj, t)
            else:
                self._pending_apply = (proj, t)
                self._deb.start()

        def _flush_apply(self):
            if self._pending_apply:
                proj, t = self._pending_apply
                self._pending_apply = None
                self._do_apply(proj, t)

        def _do_apply(self, proj, t):
            import video_model as VM
            figs = VM.figure_clips_at(proj, t)
            target = {}
            for c in figs:
                cid = str(c.get("id"))
                src = c.get("source") or {}
                ref = self.register_model_dir(src.get("path") or "")
                lay = VM.layout_at(c)
                target[cid] = {
                    "ref": ref,
                    "layout": (round(float(lay["x"]), 4),
                               round(float(lay["y"]), 4),
                               round(float(lay["scale"]), 4),
                               round(float(lay["rot"]), 3)),
                    "motion": str(c.get("motion") or ""),
                    "expression": str(c.get("expression") or ""),
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
                if cid not in target:
                    self._send("clearModel", {"modelId": cid})
            self._figstate = target

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
            if isinstance(e, dict):
                self._record_event(e)

        def _record_event(self, e):
            self._events.append(e)
            self.eventReceived.emit(json.dumps(e, ensure_ascii=False))

        def events_of(self, evt):
            return [e for e in self._events if e.get("evt") == evt]

        # ---------- 截图（验收/目检用；不进导出链，D4） ----------
        def grab_shot(self, out_path):
            img = self.view.grab()
            return bool(img.save(out_path))

else:
    WebPreviewView = None
