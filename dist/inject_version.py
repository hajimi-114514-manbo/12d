# -*- coding: utf-8 -*-
"""把 VERSION 注入 installer.cs —— 由打包脚本调用。

用法: python inject_version.py <l2d_rig_qt.py 路径> <installer.cs 路径>
"""
import io
import re
import sys


def main():
    if len(sys.argv) < 3:
        print('用法: inject_version.py <src_py> <installer_cs>')
        return 1
    src, cs = sys.argv[1], sys.argv[2]

    t = io.open(src, encoding='utf-8', errors='replace').read()
    m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', t, re.M)
    ver = m.group(1) if m else '0.0.0'

    s = io.open(cs, encoding='utf-8', errors='replace').read()
    if '@@APP_VERSION@@' not in s and ('"' + ver + '"') in s:
        print('APP_VERSION=' + ver + ' (already)')
        return 0
    s2 = s.replace('@@APP_VERSION@@', ver)
    io.open(cs, 'w', encoding='utf-8', newline='').write(s2)
    print('APP_VERSION=' + ver)
    return 0


if __name__ == '__main__':
    sys.exit(main())
