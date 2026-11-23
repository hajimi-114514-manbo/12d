# -*- coding: utf-8 -*-
"""语音音色管理对话框：列出已导入音色 + 导入新的（ckpt+pth 成对）。

为什么要有这个界面
------------------
角色音色是 **CC-BY-NC 许可、禁止二次配布** 的训练产物，HuggingFace 上
也查不到（2026-11-23 实测：13 个本地音色逐个精确搜索，13/13 全无）。
所以木偶**不能提供音色内容**，只能提供「导入 + 校验」的通道 —— 用户从
自己合法持有的渠道拿到 ckpt+pth 后导进来。

界面上的每一条约束都对应引擎的真实要求：
  · 必须**同名**的一对文件（引擎按同名配对）
  · .ckpt 是 GPT 侧、.pth 是 SoVITS 侧
  · 体积下限检查（半截文件会被引擎加载到一半才崩）
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QWidget,
                               QFileDialog, QLabel)
from qfluentwidgets import (CardWidget, PrimaryPushButton, PushButton,
                            StrongBodyLabel, BodyLabel, CaptionLabel,
                            MessageBoxBase, ComboBox, LineEdit)

import ui_theme as TH
import ui_toast as TOAST


def _human(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "-"
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%.0f %s" % (n, u)) if u in ("B", "KB") else "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f GB" % n


def _voice_lib():
    """惰性 import：voice_library 依赖 voice_paths（会读用户目录）。"""
    import voice_library
    return voice_library


class _VoiceRow(CardWidget):
    """一行音色：名字 + 体积 + 状态 + 删除按钮。"""

    def __init__(self, row, on_remove=None, parent=None):
        super().__init__(parent)
        self.row = row
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)
        name = str(row.get("stem") or "")
        t = BodyLabel(name, self)
        t.setStyleSheet("color:%s;" % TH.t("text"))
        lay.addWidget(t, 1)
        if row.get("ok"):
            s = CaptionLabel("%s　完整" % _human(row.get("size")), self)
            s.setStyleSheet("color:%s;" % TH.t("text_dim"))
        else:
            s = CaptionLabel("缺少配对文件（需要同名 .ckpt + .pth）", self)
            s.setStyleSheet("color:%s;" % TH.t("text_mute"))
        lay.addWidget(s)
        if on_remove:
            b = PushButton("删除", self)
            b.setFixedHeight(26)
            b.clicked.connect(lambda _=False, n=name: on_remove(n))
            lay.addWidget(b)


class VoiceManagerDialog(QDialog):
    """语音音色管理（导入 / 列表 / 删除）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("语音音色")
        self.setObjectName("voiceManager")
        self.resize(720, 520)

        vl = QVBoxLayout(self)
        vl.setContentsMargins(20, 18, 20, 16)
        vl.setSpacing(10)

        head = QHBoxLayout()
        title = StrongBodyLabel("语音音色", self)
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
            "音色是「同名的一对」权重文件：.ckpt（GPT 侧）+ .pth（SoVITS 侧），"
            "每个约 150~170MB。\n"
            "这些内容受 CC-BY-NC 许可限制、不可二次配布，木偶不提供，"
            "请自行导入合法取得的音色。", self)
        hint.setWordWrap(True)
        hint.setStyleSheet("color:%s;" % TH.t("text_dim"))
        vl.addWidget(hint)

        # ---- 导入区 ----
        imp = QWidget(self)
        il = QVBoxLayout(imp)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(6)

        r1 = QHBoxLayout()
        r1.addWidget(CaptionLabel("GPT 侧"))
        self.ed_ckpt = LineEdit(imp)
        self.ed_ckpt.setPlaceholderText("选择 .ckpt 文件")
        self.ed_ckpt.setReadOnly(True)
        r1.addWidget(self.ed_ckpt, 1)
        self.btn_ckpt = PushButton("浏览…", imp)
        self.btn_ckpt.clicked.connect(self._pick_ckpt)
        r1.addWidget(self.btn_ckpt)
        il.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(CaptionLabel("SoVITS 侧"))
        self.ed_pth = LineEdit(imp)
        self.ed_pth.setPlaceholderText("选择 .pth 文件（须与上面同名）")
        self.ed_pth.setReadOnly(True)
        r2.addWidget(self.ed_pth, 1)
        self.btn_pth = PushButton("浏览…", imp)
        self.btn_pth.clicked.connect(self._pick_pth)
        r2.addWidget(self.btn_pth)
        il.addLayout(r2)

        r3 = QHBoxLayout()
        r3.addWidget(CaptionLabel("显示名"))
        self.ed_name = LineEdit(imp)
        self.ed_name.setPlaceholderText("留空则用文件名")
        r3.addWidget(self.ed_name, 1)
        self.btn_import = PrimaryPushButton("导入音色", imp)
        self.btn_import.clicked.connect(self._do_import)
        r3.addWidget(self.btn_import)
        il.addLayout(r3)

        self.lbl_check = CaptionLabel("", imp)
        self.lbl_check.setWordWrap(True)
        self.lbl_check.setStyleSheet("color:%s;" % TH.t("text_dim"))
        il.addWidget(self.lbl_check)
        vl.addWidget(imp)

        vl.addWidget(StrongBodyLabel("已导入", self))
        self.list_host = QWidget(self)
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(4)
        vl.addWidget(self.list_host, 1)

        self.empty = CaptionLabel("还没有导入任何音色", self)
        self.empty.setAlignment(Qt.AlignCenter)
        vl.addWidget(self.empty)

        foot = QHBoxLayout()
        self.status = CaptionLabel("", self)
        self.status.setStyleSheet("color:%s;" % TH.t("text_dim"))
        foot.addWidget(self.status, 1)
        btn_refresh = PushButton("刷新", self)
        btn_refresh.clicked.connect(self.reload)
        foot.addWidget(btn_refresh)
        btn_close = PushButton("关闭", self)
        btn_close.clicked.connect(self.accept)
        foot.addWidget(btn_close)
        vl.addLayout(foot)

        self._ckpt = ""
        self._pth = ""
        self.reload()
        # 初始就是「什么都没选」——导入按钮必须是灰的。
        # 不调 _check() 的话按钮停在 Qt 默认的 enabled，用户一点就报
        # 「需要同时选择两个文件」，像是坏了。
        self._check()

    # ---------------------------------------------------------------- 动作
    def _pick_ckpt(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选择 GPT 侧权重（.ckpt）", os.path.expanduser("~"),
            "GPT 权重 (*.ckpt)")
        if not p:
            return
        self._ckpt = p
        self.ed_ckpt.setText(p)
        if not self.ed_name.text().strip():
            self.ed_name.setText(os.path.splitext(os.path.basename(p))[0])
        self._check()

    def _pick_pth(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选择 SoVITS 侧权重（.pth）", os.path.expanduser("~"),
            "SoVITS 权重 (*.pth)")
        if not p:
            return
        self._pth = p
        self.ed_pth.setText(p)
        self._check()

    def _check(self):
        """实时校验（不等用户点导入才知道选错没）。"""
        VL = _voice_lib()
        ok, msg = VL.validate_pair(self._ckpt, self._pth)
        if not self._ckpt and not self._pth:
            self.lbl_check.setText("")
            self.btn_import.setEnabled(False)
            return
        if ok:
            self.lbl_check.setText("配对可用，将导入为「%s」" % msg)
            self.lbl_check.setStyleSheet("color:%s;" % TH.t("text_dim"))
            self.btn_import.setEnabled(True)
        else:
            self.lbl_check.setText(msg)
            self.lbl_check.setStyleSheet("color:%s;" % TH.t("text_mute"))
            self.btn_import.setEnabled(False)

    def _do_import(self):
        VL = _voice_lib()
        ok, msg = VL.import_voice(self._ckpt, self._pth,
                                  name=self.ed_name.text().strip())
        if not ok:
            TOAST.toast_err(self, "导入失败", msg, duration=5000)
            self.status.setText(msg)
            return
        TOAST.toast_ok(self, "导入完成", msg, duration=2500)
        self._ckpt = self._pth = ""
        self.ed_ckpt.clear()
        self.ed_pth.clear()
        self.ed_name.clear()
        self.reload()

    def _remove(self, stem):
        VL = _voice_lib()
        ok, msg = VL.remove_voice(stem)
        if not ok:
            TOAST.toast_warn(self, "删除失败", msg, duration=3000)
            return
        TOAST.toast_ok(self, "已删除", stem, duration=1800)
        self.reload()

    # ---------------------------------------------------------------- 数据
    def reload(self):
        while self.list_layout.count():
            it = self.list_layout.takeAt(0)
            w = it.widget()
            if isinstance(w, QWidget):
                w.deleteLater()
        VL = _voice_lib()
        rows = VL.voices()
        for r in rows:
            self.list_layout.addWidget(_VoiceRow(r, on_remove=self._remove,
                                                parent=self.list_host))
        st = VL.stats()
        self.summary.setText("%d 个（完整 %d，不完整 %d）"
                             % (st["total"], st["ok"], st["incomplete"]))
        self.empty.setVisible(not rows)
        self.status.setText("目录：%s" % (st["dir"] or "未找到（引擎未安装）"))
        if not st["engine_ready"]:
            self.status.setText("语音引擎未就绪 —— 请先在资产管理里安装引擎，"
                                "否则导入的音色无处可放")


class _ImportVoiceDialog(MessageBoxBase):
    """便捷入口：一步选两个文件 + 导入（从别处调用时用）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewLayout.addWidget(StrongBodyLabel("导入音色", self))
        d = CaptionLabel("选择同名的一对权重文件（.ckpt + .pth）", self)
        d.setStyleSheet("color:%s;" % TH.t("text_dim"))
        self.viewLayout.addWidget(d)
        self.yesButton.setText("打开音色管理")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(420)

    def accept(self):
        dlg = VoiceManagerDialog(self.parentWidget())
        dlg.exec()
        super().accept()
