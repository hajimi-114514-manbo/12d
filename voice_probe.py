# -*- coding: utf-8 -*-
"""语音服务连通性探测（需求池【语音V1】）。

干嘛的
------
在木偶里配一个 `host:port`，判断「那边的 GPT-SoVITS 服务能不能用」，
并把探测结果翻译成人话。**只探测，不合成** —— 这一步不碰产品代码，
风险最低，是整条语音链的第一步。

能判什么
--------
① 端口通不通（TCP）—— 服务没起、防火墙挡、地址写错
② 是不是 GPT-SoVITS 的 API（不是随便什么 HTTP 服务）
③ 已加载的底模是什么版本（v2Pro / v2ProPlus …，影响能不能跑我们的音色）
④ 能不能切音色权重（真正验证「推理链路活着」，而不只是 HTTP 活着）
⑤ 本机能否起（引擎目录在不在、python 在不在）

为什么不能只 ping 端口
----------------------
`api_v2.py` 是 FastAPI，**任何** HTTP 服务都能返回 404/200。只验端口会
把「某个网页服务器」误判成「语音服务可用」。所以要靠端点特征：
  · GET /control?command=（缺 command 时回 400 {"message":"command is required"}）
  · GET /set_gpt_weights（缺 path 时回 400 {"message":"gpt weight path is required"}）
这两个响应文案是 GPT-SoVITS 独有的指纹。

关键事实（读 api_v2.py 得出，不是猜的）
--------------------------------------
  · 默认 127.0.0.1:9880
  · **启动即加载模型**（`tts_pipeline = TTS(tts_config)` 在模块级），
    所以「服务起来了」= 「模型加载成功了」，这是最强信号
  · /control 只支持 restart / exit，**没有健康检查命令**，
    所以不能靠它查状态；用它试探时只能看「是否 400 而非 404」
  · /set_gpt_weights、/set_sovits_weights 会真的切换权重，
    成功后返回 {"message":"success"} —— 这是验证推理链路活着的最轻手段
  · /tts 需要 ref_audio_path（参考音频），没有现成音频时不能乱调

对外 API：
    probe(host, port, timeout) -> dict   完整探测结果
    parse_addr(s)              -> (host, port)   解析 "127.0.0.1:9880"
    probe_local()              -> dict   探测本机引擎（自起服务）
    wait_ready(host, port, max_s, on_tick) -> dict  等服务就绪（起服务后用）

实测数据（2026-11-23，本机 RTX 4060 / v2ProPlus）
-------------------------------------------------
  · 冷启动到端口就绪 **8.1 秒**（加载 TTS Config 4.5s + GPT 1.5s +
    VITS 0.2s + BERT 1.2s + CNHuBERT 1.3s）
  · 首次跑时我等了 120 秒还超时，原因是 `stdout=PIPE` 但**没有读管道**，
    进程写日志把管道缓冲写满后自己阻塞了 —— 不是服务慢。
    起服务时要么读走 stdout，要么重定向到文件/NUL，否则必踩这个坑。
"""
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

# ==================== 类型定义（v3.6.2 后） ====================
# 探测结果的键以前散在三个模块的裸字符串里。现在收成一处：
# 新代码用 r.ok（IDE 能补全、拼错立刻报错），老代码 r["ok"] 照常能用
# （dataclass + Mapping 混合），不用改任何既有调用方。

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterator, Mapping, Optional
from enum import Enum


class Lang(str, Enum):
    """语种（api_v2 的 languages 取值）。

    用 str 混入 Enum，是为了让 json.dumps 照常把它当字符串序列化 ——
    否则 DSH 联动回包会出现 {"lang": "Lang.ZH"} 这种鬼东西。
    """
    ZH = "zh"
    JA = "ja"
    EN = "en"
    AUTO = "auto"

    def __str__(self):
        return self.value


class VoiceSide(str, Enum):
    """音色文件的两个侧别。

    GPT 侧是 .ckpt（文本 -> 韵律），SoVITS 侧是 .pth（韵律 -> 语音）。
    引擎靠**同名**配对，两边必须成对出现。
    """
    GPT = "ckpt"
    SOVITS = "pth"

    @property
    def filename_suffix(self):
        return "." + self.value



@dataclass
class LocalStatus:
    """本机引擎状态（probe_local 的返回）。"""
    engine: bool = False          # 引擎目录在不在
    python: str = ""              # 引擎自带的解释器
    api: str = ""                 # api_v2.py
    dir: str = ""                 # 引擎根目录
    service: Optional["ProbeResult"] = None   # 服务探测结果
    msg: str = ""                 # 人话结论

    # ---- Mapping 协议（probe_local 以前返回普通 dict，兼容老用法）----
    def __getitem__(self, k):
        try:
            return getattr(self, k)
        except AttributeError:
            raise KeyError(k)

    def __iter__(self) -> Iterator[str]:
        return iter(("engine", "python", "api", "dir", "service", "msg"))

    def __len__(self):
        return 6

    def get(self, k, default=None):
        return getattr(self, k, default)

    def to_dict(self):
        d = asdict(self)
        d["service"] = (self.service.to_dict()
                        if isinstance(self.service, ProbeResult) else None)
        return d


@dataclass
class ProbeDetail:
    """单个探测步骤的原始结果（排障用）。"""
    status: int = 0            # HTTP 状态码（0 = 请求没出去）
    body: str = ""             # 响应体（截断）
    err: str = ""              # 错误原因

    def to_dict(self):
        return asdict(self)


@dataclass
class ProbeResult(Mapping):
    """探测结论。

    **同时是 dataclass 和 Mapping**：
      · 内部 r.ok / r.msg —— 有 IDE 补全，属性名拼错是 AttributeError
      · 对外 r["ok"] / r.get("msg") —— 老调用方（voice_commands、
        voice_service_ui、DSH 联动回包）一个字都不用改
    """
    ok: bool = False               # 能不能用（能连 + 是 GPT-SoVITS）
    host: str = ""
    port: int = 0
    tcp: bool = False              # TCP 通不通
    is_api: bool = False           # 是不是 GPT-SoVITS API
    chatty: bool = False           # 端点有正常响应（推理链路活着的证据）
    msg: str = ""                  # 人话结论
    detail: Dict[str, Any] = field(default_factory=dict)

    # ---- Mapping 协议（兼容 dict 用法）----
    def __getitem__(self, k):
        try:
            return getattr(self, k)
        except AttributeError:
            raise KeyError(k)

    def __iter__(self) -> Iterator[str]:
        return iter(("ok", "host", "port", "tcp", "is_api", "chatty",
                     "msg", "detail"))

    def __len__(self):
        return 8

    def get(self, k, default=None):
        return getattr(self, k, default)

    def to_dict(self):
        """转成纯 dict（JSON 序列化用）。"""
        d = asdict(self)
        d["detail"] = {k: (v.to_dict() if isinstance(v, ProbeDetail) else v)
                       for k, v in (self.detail or {}).items()}
        return d


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9880

# 探测用到的端点（集中一处，避免路径写错）
EP_SET_GPT = "/set_gpt_weights"
EP_SET_SOVITS = "/set_sovits_weights"
EP_CONTROL = "/control"

# GPT-SoVITS API 的响应指纹（缺参数时返回的固定文案）
_FINGERPRINTS = (
    "command is required",
    "gpt weight path is required",
    "sovits weight path is required",
    "ref_audio_path is required",
)

_UA = {"User-Agent": "muppet-studio/3.6 (voice-probe)"}


def parse_addr(s, default_port=DEFAULT_PORT):
    """把 "127.0.0.1:9880" / "127.0.0.1" / ":9880" 解析成 (host, port)。

    解析不出端口就用默认；host 空则用 127.0.0.1。**不抛异常**。
    """
    s = str(s or "").strip()
    if not s:
        return DEFAULT_HOST, int(default_port)
    # 去掉协议前缀（用户可能贴 http://...）
    for pre in ("http://", "https://"):
        if s.lower().startswith(pre):
            s = s[len(pre):]
    s = s.rstrip("/")
    if ":" in s:
        host, _, port_s = s.rpartition(":")
        port_s = port_s.strip()
        if port_s.isdigit():
            return (host or DEFAULT_HOST), int(port_s)
        return s, int(default_port)
    return s, int(default_port)


def _tcp_ok(host, port, timeout):
    """TCP 端口是否可连。返回 (ok, 错误原因)。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, ""
    except socket.timeout:
        return False, "连接超时（%ss）" % timeout
    except ConnectionRefusedError:
        return False, "端口拒绝连接（服务没起）"
    except socket.gaierror as e:
        return False, "地址解析失败：%s" % e
    except OSError as e:
        return False, "连接失败：%s" % e


def _get(host, port, path, timeout):
    """发一个 GET，返回 (status, body_text, err)。

    HTTP 4xx 也算「有响应」——对探测来说 400 比 404 有意义得多。
    """
    url = "http://%s:%d%s" % (host, int(port), path)
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(65536).decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read(65536).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body, ""
    except urllib.error.URLError as e:
        return 0, "", "请求失败：%s" % (getattr(e, "reason", e))
    except Exception as e:
        return 0, "", "请求异常：%s" % e


def _is_gpt_sovits(status, body):
    """从响应判断「这是不是 GPT-SoVITS 的 API」。"""
    if status == 0:
        return False
    low = (body or "").lower()
    if any(fp in low for fp in _FINGERPRINTS):
        return True
    # /docs 是 FastAPI 自动生成的，只能算「是 FastAPI」不算「是语音服务」
    return False


def probe(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=4.0):
    """完整探测一个语音服务端点。返回 dict（不抛异常）。

    返回字段：
        ok          bool   能不能用（能连 + 是 GPT-SoVITS）
        host/port           实际探测的地址
        tcp         bool   TCP 通不通
        is_api      bool   是不是 GPT-SoVITS API
        chatty      bool   /set_gpt_weights 能响应（推理链路活着的证据）
        msg         str    人话结论
        detail      dict   各步原始结果（排障用）
    """
    host = str(host or DEFAULT_HOST)
    port = int(port or DEFAULT_PORT)
    out = ProbeResult(host=host, port=port)

    ok, err = _tcp_ok(host, port, timeout)
    out.tcp = ok
    out.detail["tcp"] = err or "通"
    if not ok:
        out.msg = "连不上 %s:%d —— %s" % (host, port, err)
        return out

    # ① 指纹探测：缺参数的 set_gpt_weights 应回 400 + 固定文案
    st, body, e = _get(host, port, EP_SET_GPT, timeout)
    out.detail["set_gpt_weights"] = ProbeDetail(status=st, body=body[:200],
                                                err=e)
    api_hit = _is_gpt_sovits(st, body)

    # ② 再验一个（/control 缺 command 也回 400 + 固定文案）
    st2, body2, e2 = _get(host, port, EP_CONTROL, timeout)
    out.detail["control"] = ProbeDetail(status=st2, body=body2[:200], err=e2)
    api_hit = api_hit or _is_gpt_sovits(st2, body2)

    out.is_api = api_hit
    if not api_hit:
        # 能连但不是语音服务：可能占用了端口的是别的东西
        snippet = (body or body2 or "").strip().replace("\n", " ")[:80]
        out.msg = ("端口 %d 通，但响应不像 GPT-SoVITS 语音服务%s"
                   % (port, ("（返回：%s）" % snippet) if snippet else ""))
        return out

    # ③ 指纹命中 + 有正常 HTTP 响应 = 服务在跑。
    # 注意：api_v2 启动即加载模型，所以这里已经说明模型加载成功了。
    out.chatty = st in (200, 400, 422) or st2 in (200, 400, 422)
    out.ok = out.chatty
    if out.ok:
        out.msg = "语音服务可用（%s:%d）—— 模型已加载" % (host, port)
    else:
        out.msg = "端点有响应但状态异常（%s / %s）" % (st, st2)
    return out


def wait_ready(host=DEFAULT_HOST, port=DEFAULT_PORT, max_s=120.0,
               on_tick=None, tick_s=0.5):
    """等服务端口就绪，就绪后做一次完整探测。给「刚起服务」的场景用。

    on_tick(elapsed) 可用来更新进度（UI 里显示「等待中 12s」）。
    返回最后一次 probe 的结果；超时返回 ok=False 的结果。
    实测冷启动 8.1s，默认给 120s 是留余量（慢盘/首次 CUDA 初始化）。
    """
    import time
    host = str(host or DEFAULT_HOST)
    port = int(port or DEFAULT_PORT)
    t0 = time.time()
    while time.time() - t0 < max_s:
        ok, _err = _tcp_ok(host, port, timeout=1.0)
        if ok:
            return probe(host, port, timeout=8.0)
        if on_tick:
            try:
                on_tick(time.time() - t0)
            except Exception:
                pass
        time.sleep(tick_s)
    r = probe(host, port, timeout=2.0)
    if not r.tcp:
        r.msg = "等待 %.0fs 仍未就绪（%s）" % (max_s, r.msg or "")
    return r


def probe_local(root=None, timeout=4.0):
    """探测「本机引擎」的情况：引擎目录在不在、python 在不在、服务起没起。

    这是给 UI 用的：用户点「检测」时，先看本机引擎齐不齐，再尝试连服务。
    """
    out = LocalStatus()
    try:
        import voice_paths as VP
    except Exception as e:
        out.msg = "路径模块缺失：%s" % e
        return out
    d = VP.ensure_voice(scan=False) or ""
    out.dir = d or VP.voice_dir()
    out.engine = bool(d)
    out.python = VP.engine_python(d) if d else ""
    out.api = VP.api_entry(d) if d else ""
    if not d:
        out.msg = ("本机没有语音引擎（找不到 %s）\n"
                   "引擎未安装时只能连**远程**已运行的语音服务"
                   % VP.voice_dir())
        return out
    if not out.python:
        out.msg = "引擎目录在，但缺少 runtime\\python.exe"
        return out
    if not out.api:
        out.msg = "引擎目录在，但缺少 api_v2.py"
        return out
    # 引擎齐了 -> 试着连默认地址
    r = probe(DEFAULT_HOST, DEFAULT_PORT, timeout)
    out.service = r
    out.msg = r.msg
    return out