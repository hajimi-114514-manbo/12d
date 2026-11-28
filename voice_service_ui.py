# -*- coding: utf-8 -*-
"""语音服务设置对话框（需求池【语音V1】的界面）。

用户在这里做的事：
  1. 看本机引擎齐不齐（目录/python/api_v2.py）
  2. 填一个 host:port（本机或远程）
  3. 点「检测」—— 后台探测，不卡 UI
  4. 看结论：能用 / 连不上 / 不是语音服务，以及每一步的原始结果

设计取舍
--------
· **检测放后台线程**：探测要等 TCP 超时（最长 4~8 秒），在主线程做会
  让窗口假死。用 QThread。
· **结果分「结论」和「明细」两层**：结论一句话给人看，明细折叠给排障用。
· 地址记忆到用户数据目录，下次打开自动填。
"""
import json
import os

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QWidget,
                               QFileDialog)
from qfluentwidgets import (CardWidget, PrimaryPushButton, PushButton,
                            StrongBodyLabel, CaptionLabel, LineEdit,
                            ProgressBar)

import ui_theme as TH
import ui_toast as TOAST


def _settings_path():
    """地址记忆文件（用户数据目录，别写程序目录——Program Files 不可写）。"""
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        d = os.path.join(base, "MuppetStudio")
        try:
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, "voice_service.json")
        except Exception:
            pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".runtime", "voice_service.json")


def load_addr():
    """读记住的地址；没有返回 ""。"""
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            d = json.load(f)
        return str(d.get("addr") or "")
    except Exception:
        return ""


def save_addr(addr):
    """记住地址。失败静默（记不住不影响本次使用）。"""
    try:
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump({"addr": str(addr or "")}, f, ensure_ascii=False)
        return True
    except Exception:
        return False


def _probe():
    import voice_probe
    return voice_probe


def _paths():
    """惰性 import：voice_paths 会读用户目录。"""
    import voice_paths
    return voice_paths


class _ProbeThread(QThread):
    """后台探测（TCP 超时最长几秒，不能放主线程）。"""

    done = Signal(object)

    def __init__(self, addr, parent=None):
        super().__init__(parent)
        self.addr = addr

    def run(self):
        try:
            VP = _probe()
            host, port = VP.parse_addr(self.addr)
            r = VP.probe(host, port, timeout=4.0)
        except Exception as e:
            # 异常也要发一个**同类型**的结果，否则槽函数里的属性访问会炸
            r = VP.ProbeResult(msg="探测异常：%s" % e)
        self.done.emit(r)


class _DirRow(QWidget):
    """一行「标签 + 路径输入 + 浏览 + 清除」。

    外置引擎/语音包都用它 —— 两处交互完全一样（选目录、记住、能清空退回
    自动探测），抽出来免得两份代码走样。
    """

    def __init__(self, label, placeholder, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        cap = CaptionLabel(label, self)
        cap.setFixedWidth(72)
        lay.addWidget(cap)
        self.ed = LineEdit(self)
        self.ed.setPlaceholderText(placeholder)
        lay.addWidget(self.ed, 1)
        self.btn_browse = PushButton("浏览…", self)
        lay.addWidget(self.btn_browse)
        self.btn_clear = PushButton("自动", self)
        self.btn_clear.setToolTip("清除手工选择，退回自动探测")
        self.btn_clear.setFixedWidth(56)
        lay.addWidget(self.btn_clear)

    def text(self):
        return (self.ed.text() or "").strip()

    def set_text(self, s):
        self.ed.setText(s or "")


class VoiceServiceDialog(QDialog):
    """语音服务设置与连通性检测。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("语音服务")
        self.setObjectName("voiceService")
        self.resize(760, 660)
        self._thread = None

        vl = QVBoxLayout(self)
        vl.setContentsMargins(20, 18, 20, 16)
        vl.setSpacing(10)

        head = QHBoxLayout()
        t = StrongBodyLabel("语音服务", self)
        f = t.font()
        f.setPixelSize(18)
        f.setBold(True)
        t.setFont(f)
        head.addWidget(t)
        head.addStretch(1)
        self.badge = CaptionLabel("", self)
        self.badge.setStyleSheet("color:%s;" % TH.t("text_dim"))
        head.addWidget(self.badge)
        vl.addLayout(head)

        hint = CaptionLabel(
            "配音走 GPT-SoVITS 的 HTTP 服务（api_v2.py），默认 127.0.0.1:9880。\n"
            "可以连本机（自己起引擎），也可以连局域网里另一台跑着服务的机器。",
            self)
        hint.setWordWrap(True)
        hint.setStyleSheet("color:%s;" % TH.t("text_dim"))
        vl.addWidget(hint)

        # ---- 外置目录（v3.7.6）：引擎与语音包都可自选 ----
        self.dir_card = CardWidget(self)
        dl = QVBoxLayout(self.dir_card)
        dl.setContentsMargins(14, 10, 14, 10)
        dl.setSpacing(6)
        dl.addWidget(StrongBodyLabel("外置目录", self))
        cap2 = CaptionLabel(
            "引擎（约 4GB）与语音包（音色 ckpt+pth）通常放在别的盘上，"
            "不必塞进程序目录。选一次会记住，下次自动用它。", self.dir_card)
        cap2.setWordWrap(True)
        cap2.setStyleSheet("color:%s;" % TH.t("text_dim"))
        dl.addWidget(cap2)

        self.row_engine = _DirRow(
            "引擎目录", "含 GPT_SoVITS\\ 与 runtime\\ 的目录", self.dir_card)
        self.row_engine.btn_browse.clicked.connect(self._pick_engine)
        self.row_engine.btn_clear.clicked.connect(self._clear_engine)
        self.row_engine.ed.editingFinished.connect(self._apply_engine_typed)
        dl.addWidget(self.row_engine)

        self.row_pack = _DirRow(
            "语音包目录", "放 *.ckpt 与 *.pth 的目录（留空=引擎内置 voices）",
            self.dir_card)
        self.row_pack.btn_browse.clicked.connect(self._pick_pack)
        self.row_pack.btn_clear.clicked.connect(self._clear_pack)
        self.row_pack.ed.editingFinished.connect(self._apply_pack_typed)
        dl.addWidget(self.row_pack)

        self.lbl_dir_tip = CaptionLabel("", self.dir_card)
        self.lbl_dir_tip.setWordWrap(True)
        self.lbl_dir_tip.setStyleSheet("color:%s;" % TH.t("text_dim"))
        dl.addWidget(self.lbl_dir_tip)
        vl.addWidget(self.dir_card)

        # ---- 本机引擎状态 ----
        self.engine_card = CardWidget(self)
        el = QVBoxLayout(self.engine_card)
        el.setContentsMargins(14, 10, 14, 10)
        el.setSpacing(4)
        el.addWidget(StrongBodyLabel("本机引擎", self))
        self.lbl_engine = CaptionLabel("", self.engine_card)
        self.lbl_engine.setWordWrap(True)
        self.lbl_engine.setStyleSheet("color:%s;" % TH.t("text_dim"))
        el.addWidget(self.lbl_engine)
        vl.addWidget(self.engine_card)

        # ---- 地址 + 检测 ----
        row = QHBoxLayout()
        row.addWidget(CaptionLabel("服务地址"))
        self.ed_addr = LineEdit(self)
        self.ed_addr.setPlaceholderText("127.0.0.1:9880")
        self.ed_addr.setText(load_addr() or "127.0.0.1:9880")
        row.addWidget(self.ed_addr, 1)
        self.btn_probe = PrimaryPushButton("检测", self)
        self.btn_probe.clicked.connect(self._do_probe)
        row.addWidget(self.btn_probe)
        vl.addLayout(row)

        self.progress = ProgressBar(self)
        self.progress.setVisible(False)
        vl.addWidget(self.progress)

        # ---- 结论 ----
        self.result_card = CardWidget(self)
        rl = QVBoxLayout(self.result_card)
        rl.setContentsMargins(14, 10, 14, 10)
        rl.setSpacing(4)
        self.lbl_result = StrongBodyLabel("尚未检测", self.result_card)
        self.lbl_result.setWordWrap(True)
        rl.addWidget(self.lbl_result)
        self.lbl_detail = CaptionLabel("", self.result_card)
        self.lbl_detail.setWordWrap(True)
        self.lbl_detail.setStyleSheet("color:%s;" % TH.t("text_dim"))
        rl.addWidget(self.lbl_detail)
        vl.addWidget(self.result_card, 1)

        foot = QHBoxLayout()
        self.status = CaptionLabel("", self)
        self.status.setStyleSheet("color:%s;" % TH.t("text_dim"))
        foot.addWidget(self.status, 1)
        b3 = PushButton("关闭", self)
        b3.clicked.connect(self.accept)
        foot.addWidget(b3)
        vl.addLayout(foot)

        self._refresh_engine()
        self._load_dirs()

    # ------------------------------------------------------- 外置目录（v3.7.6）
    def _load_dirs(self):
        """把已记住的目录填进输入框，并刷新提示。"""
        try:
            VP = _paths()
            self.row_engine.set_text(VP.remembered_engine_dir() or "")
            self.row_pack.set_text(VP.remembered_pack_dir() or "")
        except Exception:
            pass
        self._refresh_dir_tip()

    def _refresh_dir_tip(self):
        """目录卡片下方的状态行：当前解析到哪、语音包目录在哪。"""
        try:
            VP = _paths()
            eng = VP.remembered_engine_dir() or ""
            pack = VP.remembered_pack_dir() or ""
            vdir = VP.voices_dir() or ""
            parts = []
            if eng:
                parts.append("引擎已指定：%s" % eng)
            else:
                parts.append("引擎：自动探测（%s）" % (VP.voice_dir() or "未找到"))
            parts.append("语音包：%s" % (vdir or "未找到"))
            if pack:
                parts.append("（手工指定）")
            self.lbl_dir_tip.setText("\n".join(parts))
        except Exception as e:
            self.lbl_dir_tip.setText("目录状态读取失败：%s" % e)

    def _pick_engine(self):
        d = QFileDialog.getExistingDirectory(
            self, "选择语音引擎根目录（含 GPT_SoVITS\\ 与 runtime\\）",
            self.row_engine.text() or os.path.expanduser("~"))
        if not d:
            return
        self._set_engine(d)

    def _apply_engine_typed(self):
        """用户手打/粘贴路径后回车也要生效。"""
        t = self.row_engine.text()
        if t:
            self._set_engine(t)

    def _set_engine(self, d):
        VP = _paths()
        if not VP.engine_dir_ok(d):
            TOAST.toast_warn(
                self, "目录不像语音引擎",
                "该目录下没找到 GPT_SoVITS\\ 或 runtime\\。\n"
                "请选引擎的根目录（里面应有 GPT_SoVITS 与 runtime 两个子目录）。",
                duration=5000)
            self.row_engine.set_text(VP.remembered_engine_dir() or "")
            return
        VP.remember_engine_dir(d)
        self.row_engine.set_text(d)
        self._refresh_dir_tip()
        self._refresh_engine()
        TOAST.toast_ok(self, "引擎目录已设", d, duration=2500)

    def _clear_engine(self):
        VP = _paths()
        VP.remember_engine_dir(None)
        self.row_engine.set_text("")
        self._refresh_dir_tip()
        self._refresh_engine()
        self.status.setText("引擎目录已清除，恢复自动探测")

    def _pick_pack(self):
        d = QFileDialog.getExistingDirectory(
            self, "选择语音包目录（放 *.ckpt 与 *.pth）",
            self.row_pack.text() or os.path.expanduser("~"))
        if not d:
            return
        self._set_pack(d)

    def _apply_pack_typed(self):
        t = self.row_pack.text()
        if t:
            self._set_pack(t)

    def _set_pack(self, d):
        VP = _paths()
        if not VP.pack_dir_ok(d):
            TOAST.toast_warn(self, "目录不存在", d, duration=4000)
            self.row_pack.set_text(VP.remembered_pack_dir() or "")
            return
        VP.remember_pack_dir(d)
        self.row_pack.set_text(d)
        self._refresh_dir_tip()
        # 立刻报一下这个包里认到几个音色，用户能马上判断选对没
        n_ok = n_all = 0
        try:
            import voice_library as VL
            rows = VL.voices()
            n_all = len(rows)
            n_ok = sum(1 for r in rows if r.get("ok"))
        except Exception:
            pass
        if n_all:
            TOAST.toast_ok(self, "语音包目录已设",
                           "%d 个音色（完整 %d）" % (n_all, n_ok), duration=3000)
        else:
            TOAST.toast_warn(self, "语音包目录已设",
                             "目录里暂未发现 .ckpt/.pth 音色", duration=3500)

    def _clear_pack(self):
        VP = _paths()
        VP.remember_pack_dir(None)
        self.row_pack.set_text("")
        self._refresh_dir_tip()
        self.status.setText("语音包目录已清除，用引擎内置 voices")

    # ---------------------------------------------------------------- 内部
    def _refresh_engine(self):
        """显示本机引擎状态（不连服务，纯本地检查）。"""
        try:
            VP = _probe()
            r = VP.probe_local(timeout=1.0)
        except Exception as e:
            self.lbl_engine.setText("检查失败：%s" % e)
            return
        self.engine_card_state = r
        if r.engine:
            lines = ["引擎目录：%s" % (r.dir or "")]
            if r.python:
                lines.append("解释器：%s" % r.python)
            else:
                lines.append("解释器：**缺失**（runtime\\python.exe）")
            if r.api:
                lines.append("接口：%s" % r.api)
            else:
                lines.append("接口：**缺失**（api_v2.py）")
            self.lbl_engine.setText("\n".join(lines))
        else:
            self.lbl_engine.setText(
                "%s\n可以连**远程**已运行的语音服务（在下面填地址）。"
                % (r.msg or "本机没有语音引擎"))

    def _thread_running(self):
        """线程还在跑吗。

        C++ 对象被销毁后 isRunning() 会抛 RuntimeError，这里一律当「没在跑」
        —— 保守但不会崩。
        """
        t = self._thread
        if t is None:
            return False
        try:
            return bool(t.isRunning())
        except RuntimeError:
            return False

    def _do_probe(self):
        if self._thread is not None and self._thread_running():
            return
        addr = self.ed_addr.text().strip()
        if not addr:
            TOAST.toast_warn(self, "检测", "请填服务地址", duration=2500)
            return
        save_addr(addr)
        self.btn_probe.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)      # 不确定进度条（不知道要等多久）
        self.lbl_result.setText("检测中…")
        self.lbl_result.setStyleSheet("")
        self.lbl_detail.setText("")
        self.status.setText("正在连 %s" % addr)

        th = _ProbeThread(addr, self)
        th.done.connect(self._on_probe_done)
        # **不连** th.deleteLater：Qt 会在 finished 之后销毁 C++ 对象，
        # 之后任何 self._thread.isRunning() 都抛
        # "Internal C++ object already deleted"（实测踩过）。
        # 对象交给 Python 引用计数回收即可 —— 它没有 parent，不会泄漏。
        self._thread = th
        th.start()

    def _on_probe_done(self, r):
        self.btn_probe.setEnabled(True)
        self.progress.setVisible(False)
        ok = bool(r.ok)
        msg = str(r.msg or "")
        if ok:
            self.lbl_result.setText("✓ " + msg)
            self.lbl_result.setStyleSheet("color:%s;" % TH.t("text"))
        else:
            self.lbl_result.setText("× " + msg)
            self.lbl_result.setStyleSheet("color:%s;" % TH.t("text_mute"))

        # 明细（排障用）：把每一步的原始结果摊开
        det = r.detail or {}
        if det:
            parts = []
            for k, v in det.items():
                if isinstance(v, dict):
                    parts.append("%s: HTTP %s %s" % (
                        k, v.get("status"), (v.get("body") or "")[:80]))
                else:
                    parts.append("%s: %s" % (k, v))
            self.lbl_detail.setText("\n".join(parts))
        else:
            self.lbl_detail.setText("")

        self.badge.setText("可用" if ok else "不可用")
        self.badge.setStyleSheet(
            "color:%s;" % (TH.t("text") if ok else TH.t("text_mute")))
        self.status.setText("%s:%s" % (r.host, r.port))
        self._thread = None