# -*- coding: utf-8 -*-
"""打印 l2d_rig_qt.py 里的 VERSION 常量（供 重新打包.bat 拼安装包文件名）。

用法: python get_version.py <l2d_rig_qt.py 路径>
输出: 3.4.4    （读不到则输出空行，bat 侧判空中止）
"""
import io
import re
import sys


def main():
    if len(sys.argv) < 2:
        print('')
        return 1
    try:
        t = io.open(sys.argv[1], encoding='utf-8', errors='replace').read()
    except OSError:
        print('')
        return 1
    m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', t, re.M)
    print(m.group(1) if m else '')
    return 0


if __name__ == '__main__':
    sys.exit(main())
