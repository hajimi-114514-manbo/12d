# -*- coding: utf-8 -*-
"""菜单边界回归测试（qfluentwidgets 顶层菜单不夹左边界）。

背景：库的 4 处落点计算只写
    x = min(pos.x() - margins.left(), rect.right() - w)
只挡右越界，不挡左越界。按钮贴左（标题栏菜单按钮）时 pos.x()=0、
左边距 12，算出 x=-12，整块菜单被推出屏幕，表现是「每一项文字只剩右半截」。

本测试同时守住「只修越界、不干扰正常位置」——后者比前者更容易写错，
第一版补丁就曾把中间位置的菜单整体右推了 12px。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPoint

app = QApplication(sys.argv)

import l2d_rig_qt as M
from qfluentwidgets import RoundMenu, Action, FluentIcon
from qfluentwidgets.common.screen import getCurrentScreenGeometry
from qfluentwidgets.components.widgets.menu import (
    MenuAnimationManager, MenuAnimationType)

SCREEN = getCurrentScreenGeometry()
MANAGERS = (MenuAnimationType.DROP_DOWN,
            MenuAnimationType.PULL_UP,
            MenuAnimationType.FADE_IN_DROP_DOWN)

fails = []


def chk(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def mkmenu(n=8, long=False):
    m = RoundMenu(parent=None)
    for i in range(n):
        t = ("很长的菜单项名称用来撑宽菜单%d" % i) if long else ("项%d" % i)
        m.addAction(Action(FluentIcon.ADD, t))
    m.adjustSize()
    return m


def main():
    print("屏幕:", SCREEN)

    print("\n=== 1. 补丁可装且幂等 ===")
    chk(M._install_menu_edge_fix(), "首次安装返回 True")
    chk(M._install_menu_edge_fix(), "重复安装幂等")
    import qfluentwidgets.components.widgets.menu as qm
    chk(getattr(qm, "_muppet_edge_fixed", False), "已打标记")

    print("\n=== 2. 贴左弹出：不能跑出屏幕左边界 ===")
    m = mkmenu()
    ml = m.layout().contentsMargins().left()
    for mt in MANAGERS:
        mgr = MenuAnimationManager.make(m, mt)
        p = mgr._endPosition(QPoint(0, 40))
        name = str(mt).split('.')[-1]
        print("  %-20s x=%4d（未修复会是 %d）" % (name, p.x(), -ml))
        chk(p.x() >= SCREEN.left(),
            "%s 左不越界（x=%d >= %d）" % (name, p.x(), SCREEN.left()))

    print("\n=== 3. 正常/靠右位置：位置必须与库原算法一致 ===")
    for x in (300, 400, 600):
        mgr = MenuAnimationManager.make(m, MenuAnimationType.DROP_DOWN)
        p = mgr._endPosition(QPoint(x, 40))
        expect = x - ml                       # 库原算法
        chk(p.x() == expect,
            "pos.x=%d -> x=%d（期望 %d，不能被改动）" % (x, p.x(), expect))

    print("\n=== 4. 靠右超屏：不能越出右边界 ===")
    for x in (SCREEN.right() - 20, SCREEN.right() + 300):
        mgr = MenuAnimationManager.make(m, MenuAnimationType.DROP_DOWN)
        p = mgr._endPosition(QPoint(x, 40))
        chk(p.x() + m.width() <= SCREEN.right() + 1,
            "pos.x=%d 右不越界（右边=%d）" % (x, p.x() + m.width()))

    print("\n=== 5. 超长菜单不崩 ===")
    m3 = mkmenu(14, long=True)
    p = MenuAnimationManager.make(
        m3, MenuAnimationType.DROP_DOWN)._endPosition(QPoint(0, 40))
    chk(isinstance(p.x(), int), "超长菜单返回有效坐标（宽=%d）" % m3.width())

    print("\n=== 6. adjustPosition 也被覆盖 ===")
    m2 = mkmenu(4)
    m2.move(QPoint(-50, 40))
    m2.adjustPosition()
    chk(m2.x() >= SCREEN.left(), "adjustPosition 后 x=%d 不左越界" % m2.x())

    print("\n=== 7. 上边界夹取 ===")
    m4 = mkmenu(20)
    p = MenuAnimationManager.make(
        m4, MenuAnimationType.PULL_UP)._endPosition(QPoint(200, SCREEN.bottom()))
    chk(p.y() >= SCREEN.top() + 4 - 1, "贴底弹出时 y=%d 不越上边界" % p.y())

    print("\n" + "=" * 58)
    if fails:
        print("FAIL %d 项" % len(fails))
        return 1
    print("PASS 菜单边界全部检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())