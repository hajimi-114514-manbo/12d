# -*- coding: utf-8 -*-
"""参考音频管理对话框：按角色分组列出 + 导入 + 试听 + 删除。

参考音频是什么
--------------
GPT-SoVITS 是**克隆式** TTS：音色权重决定「像谁」，参考音频决定
「用哪段话克隆」。没有参考音频就出不了声。

为什么单独一个界面（而不是塞进音色库）
------------------------------------
音色是「权重」，参考音频是「素材」，来源和版权口径都不同：
  · 权重：一份就够，CC-BY-NC，用户自备
  · 参考音频：可以有很多段（换着试），文件名通常就是说话内容

界面上最重要的能力是**试听** —— 段数多的时候，用户得能听一遍就知道
哪段最清楚、最适合当参考。

文件名用「说话内容」
--------------------
参考音频通常要配 prompt_text（参考音频里说的那句字）。文件名就是
那句话，不用另记；段号命名（001_xxs_ys.wav）看不出内容。
"""
import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QWidget,
                               QFileDialog, QLabel, QScrollArea)
from qfluentwidgets import (CardWidget, PrimaryPushButton, PushButton,
                            StrongBodyLabel, BodyLabel, CaptionLabel,
                            ComboBox, LineEdit, MessageBoxBase)

import ui_theme as TH
import ui_toast as TOAST


def _refs():
    """惰性 import：voice_refs 会读用户数据目录。"""
    import voice_refs
    return voice_refs


def _player():
    """惰性 import 播放器（voice_synth 依赖 QtMultimedia）。"""
    import voice_synth
    return voice_synth


class _RefRow(CardWidget):
    """一段参考音频：文件名 + 时长 + 试听 + 删除。"""

    def __init__(self, role, ref, on_play=None, on_remove=None, parent=None):
        super().__init__(parent)
        self.role = role
        self.ref = ref
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)

        name = str(ref.get("name") or "")
        t = BodyLabel(name, self)
        t.setToolTip(ref.get("path") or "")
        t.setStyleSheet("color:%s;" % TH.t("text"))
        lay.addWidget(t, 1)

        info = CaptionLabel("%.2f 秒" % float(ref.get("dur") or 0), self)
        info.setStyleSheet("color:%s;" % TH.t("text_dim"))
        lay.addWidget(info)

        if on_play:
            b = PushButton("试听", self)
            b.setFixedHeight(26)
            b.clicked.connect(lambda _=False: on_play(ref.get("path")))
            lay.addWidget(b)
        if on_remove:
            b = PushButton("删除", self)
            b.setFixedHeight(26)
            b.clicked.connect(
                lambda _=False, n=name: on_remove(role, n))
            lay.addWidget(b)


class _ImportRefDialog(QDialog):
    """导入一段参考音频：选文件 + 选角色 + 命名。"""

    def __init__(self, roles, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导入参考音频")
        self.setObjectName("importRef")
        self.resize(560, 260)
        self.result_path = ""
        self.result_role = ""
        self.result_name = ""

        vl = QVBoxLayout(self)
        vl.setContentsMargins(20, 18, 20, 16)
        vl.setSpacing(10)

        title = StrongBodyLabel("导入参考音频", self)
        f = title.font()
        f.setPixelSize(17)
        f.setBold(True)
        title.setFont(f)
        vl.addWidget(title)

        hint = CaptionLabel(
            "参考音频是「用哪段话克隆音色」的素材。一段 3~10 秒、"
            "人声清楚无杂音的音频最好。\n"
            "文件名建议用**这段话的原文**（合成时可以配 prompt_text）。",
            self)
        hint.setWordWrap(True)
        hint.setStyleSheet("color:%s;" % TH.t("text_dim"))
        vl.addWidget(hint)

        r1 = QHBoxLayout()
        r1.addWidget(CaptionLabel("音频文件"))
        self.ed_path = LineEdit(self)
        self.ed_path.setPlaceholderText("选择 .wav 文件")
        self.ed_path.setReadOnly(True)
        r1.addWidget(self.ed_path, 1)
        b1 = PushButton("浏览…", self)
        b1.clicked.connect(self._pick)
        r1.addWidget(b1)
        vl.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(CaptionLabel("角色"))
        self.cb_role = ComboBox(self)
        self.cb_role.addItems([str(r) for r in roles] or ["千早爱音"])
        self.cb_role.setEditable(True)
        self.cb_role.setCurrentText(str(roles[0]) if roles else "千早爱音")
        r2.addWidget(self.cb_role, 1)
        vl.addLayout(r2)

        r3 = QHBoxLayout()
        r3.addWidget(CaptionLabel("文件名"))
        self.ed_name = LineEdit(self)
        self.ed_name.setPlaceholderText("留空则用源文件名（通常就是说话内容）")
        r3.addWidget(self.ed_name, 1)
        vl.addLayout(r3)

        self.lbl_check = CaptionLabel("", self)
        self.lbl_check.setWordWrap(True)
        self.lbl_check.setStyleSheet("color:%s;" % TH.t("text_dim"))
        vl.addWidget(self.lbl_check)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = PushButton("取消", self)
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.btn_ok = PrimaryPushButton("导入", self)
        self.btn_ok.clicked.connect(self.accept)
        btns.addWidget(self.btn_ok)
        vl.addLayout(btns)

    def _pick(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选择参考音频", "", "音频文件 (*.wav);;所有文件 (*)")
        if not p:
            return
        self.ed_path.setText(p)
        if not self.ed_name.text().strip():
            self.ed_name.setText(os.path.splitext(os.path.basename(p))[0])
        self._check()

    def _check(self):
        VR = _refs()
        ok, msg, info = VR.validate_ref(self.ed_path.text().strip())
        self.lbl_check.setText(msg)
        self.lbl_check.setStyleSheet(
            "color:%s;" % (TH.t("text") if ok else TH.t("text_mute")))
        self.btn_ok.setEnabled(bool(ok))
        return ok

    def accept(self):
        if not self._check():
            return
        self.result_path = self.ed_path.text().strip()
        self.result_role = self.cb_role.currentText().strip()
        self.result_name = self.ed_name.text().strip()
        super().accept()


class VoiceRefsDialog(QDialog):
    """参考音频管理：按角色分组 + 导入 + 试听 + 删除。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("参考音频")
        self.setObjectName("voiceRefs")
        self.resize(760, 560)

        vl = QVBoxLayout(self)
        vl.setContentsMargins(20, 18, 20, 16)
        vl.setSpacing(10)

        head = QHBoxLayout()
        title = StrongBodyLabel("参考音频", self)
        f = title.font()
        f.setPixelSize(18)
        f.setBold(True)
        title.setFont(f)
        head.addWidget(title)
        head.addStretch(1)
        self.summary = CaptionLabel("", self)
        self.summary.setStyleSheet("color:%s;" % TH.t("text_dim"))
        head.addWidget(self.summary)
        vl.addLayout(head)

        hint = CaptionLabel(
            "GPT-SoVITS 是克隆式 TTS：音色权重决定「像谁」，"
            "参考音频决定「用哪段话克隆」。\n"
            "合成时会按角色自动挑一段；这里可以试听、导入、删除。"
            "引擎自带的 ref.wav 是凑友希娜的声音，别的角色用它会串音色。",
            self)
        hint.setWordWrap(True)
        hint.setStyleSheet("color:%s;" % TH.t("text_dim"))
        vl.addWidget(hint)

        # ---- 工具条 ----
        bar = QHBoxLayout()
        self.btn_import = PrimaryPushButton("导入参考音频", self)
        self.btn_import.clicked.connect(self._do_import)
        bar.addWidget(self.btn_import)
        bar.addStretch(1)
        self.btn_stop = PushButton("停止播放", self)
        self.btn_stop.clicked.connect(self._stop)
        bar.addWidget(self.btn_stop)
        vl.addLayout(bar)

        # ---- 角色列表 ----
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        holder = QWidget()
        self.list_lay = QVBoxLayout(holder)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(8)
        self.list_lay.addStretch(1)
        self.scroll.setWidget(holder)
        vl.addWidget(self.scroll, 1)

        self.reload()

    # ---------- 数据 ----------
    def reload(self):
        VR = _refs()
        while self.list_lay.count() > 1:
            item = self.list_lay.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        roles = VR.roles()
        st = VR.stats()
        self.summary.setText(
            "%d 个角色 / %d 段 / 共 %.1f 秒" % (st["roles"], st["count"],
                                            st["dur"]))
        if not roles:
            lbl = BodyLabel("还没有参考音频。点「导入参考音频」把 wav 导进来。",
                            self)
            lbl.setStyleSheet("color:%s;" % TH.t("text_dim"))
            self.list_lay.insertWidget(0, lbl)
            return
        for role in roles:
            card = CardWidget(self)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(12, 10, 12, 10)
            cl.setSpacing(6)
            head = StrongBodyLabel(role, card)
            f = head.font()
            f.setPixelSize(14)
            f.setBold(True)
            head.setFont(f)
            cl.addWidget(head)
            for ref in VR.refs_for(role):
                cl.addWidget(_RefRow(role, ref, self._play, self._remove, card))
            self.list_lay.insertWidget(self.list_lay.count() - 1, card)

    # ---------- 动作 ----------
    def _play(self, path):
        if not path:
            return
        try:
            VS = _player()
            VS.play(path)
        except Exception as e:
            TOAST.toast(self, "播放失败：%s" % e)

    def _stop(self):
        try:
            VS = _player()
            VS.stop_play()
        except Exception:
            pass

    def _remove(self, role, name):
        VR = _refs()
        ok, msg = VR.remove_ref(role, name)
        if ok:
            TOAST.toast(self, "已删除 %s" % name)
            self.reload()
        else:
            TOAST.toast(self, "删除失败：%s" % msg)

    def _do_import(self):
        VR = _refs()
        dlg = _ImportRefDialog(VR.roles(), self)
        if dlg.exec() != QDialog.Accepted:
            return
        ok, msg, info = VR.import_ref(dlg.result_path, dlg.result_role,
                                      dlg.result_name)
        if ok:
            TOAST.toast(self, "已导入 %s（%.2f 秒）"
                        % (info["name"], info["dur"]))
            self.reload()
        else:
            TOAST.toast(self, "导入失败：%s" % msg)
