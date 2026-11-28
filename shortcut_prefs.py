# -*- coding: utf-8 -*-
"""快捷键注册表 + 用户自定义持久化。

为什么单独一个模块：
  · QShortcut 原来直接散在各窗口 __init__ 里，键位写死，用户改不了。
    要能改，先得有一张「谁是谁」的登记表 —— 每个快捷键有稳定的 sid
    （undo / redo / …），跟菜单文案解耦，改文案不影响存盘。
  · 改了要能存、重启要能恢复：存 %APPDATA%\\MuppetStudio\\shortcuts.json。
  · 木偶工程编辑器和视频工作台各有自己的 QShortcut，键位允许重复
    （不同窗口本来互不干扰），所以按 scope 分开存、分开查冲突。

存盘口径：**只存与默认不同的项**。这样以后改默认键位，没自定义过的
用户自动跟着变，不会因为老配置文件把新默认值锁死。
配置文件坏了/缺项一律静默回默认 —— 不能因为一个 json 把启动搞挂。
"""
import contextlib
import io
import json
import os

from PySide6.QtGui import QKeySequence, QShortcut

# scope -> 人话（设置对话框分组标题用）
SCOPE_LABELS = {
    "rig": "木偶工程编辑器",
    "video": "视频工作台",
}

_prefs_path = None
# {scope: {sid: portable_text}}，只含与默认不同的项
_overrides = None


# ---------------------------------------------------------------- 持久化
def _prefs_file():
    global _prefs_path
    if _prefs_path:
        return _prefs_path
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "MuppetStudio")
    with contextlib.suppress(Exception):
        os.makedirs(d, exist_ok=True)
    _prefs_path = os.path.join(d, "shortcuts.json")
    return _prefs_path


def _load():
    """读自定义键位。utf-8-sig 兼容记事本写出的 BOM 文件。"""
    global _overrides
    if _overrides is not None:
        return _overrides
    data = {}
    for enc in ("utf-8-sig", "utf-8"):
        try:
            with io.open(_prefs_file(), encoding=enc) as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for scope, items in raw.items():
                    if isinstance(items, dict):
                        data[str(scope)] = {
                            str(k): str(v) for k, v in items.items()
                            if isinstance(v, str) and v.strip()
                        }
            break
        except Exception:
            continue
    _overrides = data
    return _overrides


def _save():
    """写自定义键位（只写非默认项）。写失败静默 —— 不该阻断用户。"""
    try:
        payload = {}
        for scope, items in _load().items():
            if items:
                payload[scope] = dict(items)
        with io.open(_prefs_file(), "w", encoding="utf-8",
                     newline="") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def normalize(text):
    """把任意键位写法归一成 Qt 的 PortableText。

    空串/非法串返回 ""。QKeySequence 会顺手把 "Delete" 归成 "Del"、
    "Ctrl++" 归成 "Ctrl+="，冲突判断才不会误判。
    """
    if not text:
        return ""
    seq = QKeySequence(str(text))
    if seq.isEmpty():
        return ""
    return seq.toString(QKeySequence.PortableText)


# ---------------------------------------------------------------- 条目 / 注册表
class ShortcutItem:
    """一个可改键位的快捷键。"""

    __slots__ = ("scope", "sid", "label", "default", "group",
                 "shortcut", "callback")

    def __init__(self, scope, sid, label, default, group, shortcut, callback):
        self.scope = scope
        self.sid = sid
        self.label = label
        self.default = normalize(default)
        self.group = group or "通用"
        self.shortcut = shortcut
        self.callback = callback


class ShortcutRegistry:
    """全局登记表（进程内一份）。"""

    def __init__(self):
        # {(scope, sid): ShortcutItem}
        self._items = {}

    # ---------- 登记 ----------
    def bind(self, parent, scope, sid, label, default, callback,
             group="通用"):
        """创建一个 QShortcut 并登记，返回 QShortcut。

        同一 (scope, sid) 重复登记（窗口重建 / 工程重开）时**替换**旧项，
        不累积 —— 否则设置页会列出两份、旧对象还挂着已销毁的父控件。
        """
        key = self._effective(scope, sid, default)
        sc = QShortcut(QKeySequence(key), parent, activated=callback)
        item = ShortcutItem(scope, sid, label, default, group, sc, callback)
        self._items[(scope, sid)] = item
        return sc

    def unbind_window(self, scope):
        """窗口关闭时清掉该 scope 的登记（可选调用）。

        不调也不影响正确性（下次 bind 会替换），只是让设置页在窗口
        关掉后不再列出失效项。这里保留给将来用。
        """
        for k in [k for k in self._items if k[0] == scope]:
            self._items.pop(k, None)

    # ---------- 查询 ----------
    def items(self, scope=None):
        """按 scope 取条目，顺序 = 登记顺序（菜单顺序）。"""
        return [it for (sc, _sid), it in self._items.items()
                if scope is None or sc == scope]

    def find(self, scope, sid):
        return self._items.get((scope, sid))

    def key_of(self, scope, sid):
        """当前生效键位（PortableText）。没登记返回 ""。"""
        it = self.find(scope, sid)
        if it is None:
            return ""
        return normalize(it.shortcut.key().toString(
            QKeySequence.PortableText))

    def display(self, scope, sid):
        """给菜单/按钮显示的键位。没登记返回 ""。"""
        return self.key_of(scope, sid)

    def has_overrides(self, scope=None):
        d = _load()
        if scope is None:
            return any(d.values())
        return bool(d.get(scope))

    # ---------- 修改 ----------
    def _effective(self, scope, sid, default):
        """生效键 = 用户自定义（若合法）否则默认。"""
        ov = (_load().get(scope) or {}).get(sid)
        norm = normalize(ov) if ov else ""
        if norm:
            return norm
        return normalize(default)

    def conflict(self, scope, text, exclude_sid=None):
        """同 scope 里找占用了 text 的其它快捷键，返回 ShortcutItem 或 None。"""
        t = normalize(text)
        if not t:
            return None
        for (sc, sid), it in self._items.items():
            if sc != scope or sid == exclude_sid:
                continue
            if normalize(it.shortcut.key().toString(
                    QKeySequence.PortableText)) == t:
                return it
        return None

    def set_key(self, scope, sid, text):
        """改一个快捷键的键位，立即生效并写盘。

        返回 (ok, message)：
          · ok=False + message=冲突项中文名  -> 拒绝
          · ok=True  + message=""             -> 已生效
        """
        it = self.find(scope, sid)
        if it is None:
            return False, "该快捷键未登记"
        norm = normalize(text)
        if not norm:
            return False, "无效的键位"
        other = self.conflict(scope, norm, exclude_sid=sid)
        if other is not None:
            return False, other.label
        it.shortcut.setKey(QKeySequence(norm))
        ov = _load().setdefault(scope, {})
        if norm == it.default:
            ov.pop(sid, None)          # 改回默认值 -> 不留自定义记录
        else:
            ov[sid] = norm
        _save()
        return True, ""

    def reset(self, scope, sid=None):
        """恢复默认：sid 为空则恢复该 scope 全部。"""
        targets = ([it for (sc, _sid2), it in self._items.items()
                    if sc == scope and (_sid2 == sid or sid is None)])
        if sid is None:
            _load().pop(scope, None)
        else:
            (_load().get(scope) or {}).pop(sid, None)
        for it in targets:
            it.shortcut.setKey(QKeySequence(it.default))
        _save()

    def reload(self):
        """丢掉内存缓存重新读盘（测试用）。"""
        global _overrides
        _overrides = None


REGISTRY = ShortcutRegistry()
