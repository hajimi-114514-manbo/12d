# -*- coding: utf-8 -*-
"""构建 runtime.zip v2（v3.1.0）：GUI 栈（本机 D:\\Python312）+ CPU ML 栈（venv）。

与 v1 的差异：
  - 新增 torch(cpu)/diffusers/transformers/accelerate/peft/sklearn 等推理栈
    —— 装完即可用 CPU 跑 SeeThrough 全流程，不再要求用户自备 Python 环境
  - ML 栈从 D:\\build\\cpuenv（干净 venv，torch==2.11.0+cpu）复制，
    避免把本机 CUDA 版 torch 混进包里
  - 重叠包（numpy/networkx/packaging/charset_normalizer）用本机 GUI 栈版本
  - 自检扩展：ML 栈 import + fp32 matmul + safetensors 读写
打包后 zip 约 1.1-1.3GB，安装包约 800MB（<2GB 限制内）。
"""
import os
import shutil
import subprocess
import sys
import zipfile

SRC_PY = r'D:\Python312'
SRC_VENV = r'D:\build\cpuenv'
BUILD = r'D:\build\muppet_runtime_v2'
ZIP_OUT = r'D:\build\runtime.zip'
ZIP_BAK = r'D:\build\runtime_v1.zip'

CORE_FILES = ['python.exe', 'pythonw.exe', 'python312.dll', 'python3.dll',
              'vcruntime140.dll', 'vcruntime140_1.dll']

# GUI 栈（本机）
PKGS_LOCAL = ['PySide6', 'shiboken6', 'cv2', 'numpy', 'numpy.libs', 'PIL',
              'scipy', 'scipy.libs', 'qfluentwidgets', 'qframelesswindow',
              'darkdetect', 'psd_tools', 'pytoshop', 'networkx', 'packaging',
              'win32', 'win32com', 'win32comext', 'pywin32_system32',
              'win32_setctime', 'attr', 'attrs', 'charset_normalizer',
              'requests', 'urllib3', 'certifi', 'idna']
# ML 栈（venv，CPU 版）：全量复制 + 排除法（依赖闭包最全，防漏 typing_extensions
# 这类单文件包；重叠包 numpy/PIL/scipy/networkx/packaging/requests 等用本机版本）
VENV_SKIP = {'pip', 'setuptools', 'pkg_resources', '_distutils_hack',
             '__pycache__', 'distutils-precedence.pth'}
PKG_FILES_VENV = []
PTH_KEEP = ['pywin32.pth']

LIB_EXCLUDE_DIRS = {'test', 'tests', 'idlelib', 'tkinter', 'turtledemo',
                    'ensurepip', 'lib2to3', 'pydoc_data', 'site-packages',
                    'dist-packages'}
COPY_EXCL_DIRS = frozenset({'__pycache__', 'tests', 'test', 'notes',
                            'dev', 'docs', 'doc', 'include'})


def copytree_filtered(src, dst, excl_dirs=frozenset(), excl_suffix=()):
    n = 0
    if not os.path.isdir(src):
        return -1
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        parts = rel.split(os.sep) if rel != '.' else []
        dirs[:] = [d for d in dirs
                   if d not in excl_dirs
                   and not any(p in excl_dirs for p in parts)]
        target = os.path.join(dst, rel) if rel != '.' else dst
        os.makedirs(target, exist_ok=True)
        for f in files:
            if excl_suffix and f.lower().endswith(tuple(excl_suffix)):
                continue
            try:
                shutil.copy2(os.path.join(root, f),
                             os.path.join(target, f))
                n += 1
            except Exception:
                pass
    return n


def pkg_dist_info(sp_src, pkg, sp_dst):
    """把 pkg 的 dist-info 元数据目录一起带上（版本探测/依赖检查用）。"""
    cnt = 0
    for name in os.listdir(sp_src):
        if name.startswith(pkg + '-') and name.endswith('.dist-info'):
            cnt += copytree_filtered(os.path.join(sp_src, name),
                                     os.path.join(sp_dst, name),
                                     excl_suffix=('.pyc',))
    return cnt


def self_check():
    print('[自检] 新运行时 import…', flush=True)
    code = (
        "import numpy; print('numpy', numpy.__version__)\n"
        "import cv2; print('cv2', cv2.__version__)\n"
        "import scipy; print('scipy', scipy.__version__)\n"
        "import PySide6; print('PySide6 ok')\n"
        "import qfluentwidgets; print('qfluentwidgets ok')\n"
        "import torch; print('torch', torch.__version__)\n"
        "assert 'cpu' in torch.__version__ or not torch.cuda.is_available(), 'CUDA torch leaked'\n"
        "import diffusers, transformers, accelerate, peft, sklearn, safetensors\n"
        "print('ml imports ok', diffusers.__version__, transformers.__version__)\n"
        "a = torch.randn(64, 64, dtype=torch.float32)\n"
        "b = (a @ a.T).sum().item()\n"
        "print('matmul ok', round(b, 1))\n"
        "import tempfile, os\n"
        "from safetensors.torch import save_file, load_file\n"
        "p = os.path.join(tempfile.mkdtemp(), 'x.safetensors')\n"
        "save_file({'w': a}, p); c = load_file(p)\n"
        "assert torch.equal(c['w'], a); print('safetensors ok')\n"
        "print('SELF_CHECK_OK')\n"
    )
    py = os.path.join(BUILD, 'python.exe')
    env = dict(os.environ)
    env.pop("MUPPET_FORCE_DEVICE", None)
    r = subprocess.run([py, '-c', code], capture_output=True, timeout=600,
                       env=env)
    out = (r.stdout or b'').decode('utf-8', 'replace') + \
          (r.stderr or b'').decode('utf-8', 'replace')
    try:
        print(out.strip())
    except Exception:
        print(out.strip().encode('ascii', 'backslashreplace').decode('ascii'))
    return r.returncode == 0 and 'SELF_CHECK_OK' in out


def main():
    if os.path.isdir(BUILD):
        print('清空旧构建…')
        shutil.rmtree(BUILD, ignore_errors=True)
    os.makedirs(BUILD)

    print('[1/6] 解释器核心…')
    for f in CORE_FILES:
        s = os.path.join(SRC_PY, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(BUILD, f))

    print('[2/6] 标准库…')
    copytree_filtered(os.path.join(SRC_PY, 'DLLs'), os.path.join(BUILD, 'DLLs'))
    copytree_filtered(os.path.join(SRC_PY, 'Lib'), os.path.join(BUILD, 'Lib'),
                      excl_dirs=LIB_EXCLUDE_DIRS, excl_suffix=('.pyc',))

    sp_dst = os.path.join(BUILD, 'Lib', 'site-packages')
    os.makedirs(sp_dst, exist_ok=True)

    print('[3/6] GUI 栈（本机）…')
    sp_local = os.path.join(SRC_PY, 'Lib', 'site-packages')
    for pkg in PKGS_LOCAL:
        s = os.path.join(sp_local, pkg)
        if os.path.isdir(s):
            copytree_filtered(s, os.path.join(sp_dst, pkg),
                              excl_dirs=COPY_EXCL_DIRS, excl_suffix=('.pyc',))
        else:
            print('    (缺) %s' % pkg)

    print('[4/6] ML 栈（venv CPU 版，全量+排除）…', flush=True)
    sp_venv = os.path.join(SRC_VENV, 'Lib', 'site-packages')
    for pkg in sorted(os.listdir(sp_venv)):
        if pkg in VENV_SKIP or pkg.startswith('~'):
            continue
        s = os.path.join(sp_venv, pkg)
        if os.path.isdir(s):
            if pkg.endswith('.dist-info'):
                copytree_filtered(s, os.path.join(sp_dst, pkg),
                                  excl_suffix=('.pyc',))
                continue
            if os.path.exists(os.path.join(sp_dst, pkg)):
                continue          # 重叠包本机版本优先（numpy/PIL/scipy 等）
            n = copytree_filtered(s, os.path.join(sp_dst, pkg),
                                  excl_dirs=COPY_EXCL_DIRS,
                                  excl_suffix=('.pyc',))
            print('    %-22s %d 文件' % (pkg, n), flush=True)
        elif pkg.endswith('.py'):
            if not os.path.exists(os.path.join(sp_dst, pkg)):
                shutil.copy2(s, os.path.join(sp_dst, pkg))
                print('    %-22s 单文件' % pkg, flush=True)
    for f in PTH_KEEP:
        s = os.path.join(sp_local, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(sp_dst, f))

    print('[5/6] 自检…')
    if not self_check():
        print('!! 自检失败，不打包')
        return 1

    print('[6/6] 打包 zip…')
    if os.path.exists(ZIP_OUT):
        if not os.path.exists(ZIP_BAK):
            os.replace(ZIP_OUT, ZIP_BAK)
            print('  旧版已备份 ->', ZIP_BAK)
        else:
            os.remove(ZIP_OUT)
    cnt = 0
    with zipfile.ZipFile(ZIP_OUT, 'w', zipfile.ZIP_DEFLATED,
                         compresslevel=6) as z:
        for root, dirs, files in os.walk(BUILD):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, BUILD)
                z.write(full, rel.replace(os.sep, '/'))
                cnt += 1
    sz = os.path.getsize(ZIP_OUT)
    print('=' * 50)
    print('文件数 %d' % cnt)
    print('zip 产物 %s (%.0f MB)' % (ZIP_OUT, sz / 2 ** 20))
    return 0


if __name__ == '__main__':
    sys.exit(main())
