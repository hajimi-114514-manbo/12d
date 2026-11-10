# -*- coding: utf-8 -*-
"""
l2d_rig 离线自检 / 回归测试（V1.0）

用途
----
在不依赖 ComfyUI / GPU 的前提下跑通引擎全链路，作为 V1.0 的回归测试。
覆盖：
  * l2d_rig_core 全部：load_layers / build_rig / render_frame /
    export_psd / export_ae_jsx / export_json
  * decompose.py 的 collect（隔离测试：把 C_OUT 指到临时目录，绝不碰真实 ComfyUI）

安全边界（重要）
----------------
* 绝不调用 decompose.decompose()（会拉起 ComfyUI + 提交 GPU 任务）。
  测试里会先把 decompose.decompose 替换成哨兵，一旦被调用立即报错。
* 绝不读取/写入真实 ComfyUI 输出目录 D:\\ComfyUI\\main\\output。
* 纯命令行运行，不 import l2d_rig_studio，不弹任何窗口。

运行方式
--------
    D:\\Python312\\python.exe D:\\work\\puppet\\l2d_rig\\selftest.py

退出码
------
    全部 PASS -> 0；任何 FAIL -> 1（可接进 CI / 提交前检查）

测试数据（都已存在于磁盘）
--------------------------
  A: D:\\视频\\26114\\anon_platform\\seethrough    layers_meta.json + layers_full/ 全画布层
  B: D:\\视频\\26114\\test_decompose               layers_meta.json + layer_*.png 按 bbox 裁剪层

产物
----
  D:\\scripts\\temp\\selftest_out\\
"""
import os
import sys
import json
import shutil
import tempfile

import numpy as np
from PIL import Image

sys.path.insert(0, r"D:\work\puppet\l2d_rig")
import l2d_rig_core as C          # noqa: E402
import decompose                  # noqa: E402  （只测 collect，不测 decompose()）

# --- 中文输出在 Windows 控制台别炸 ---
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TMP_ROOT = r"D:\scripts\temp"
OUT_DIR = os.path.join(TMP_ROOT, "selftest_out")

SRC_A = r"D:\视频\26114\anon_platform\seethrough"
SRC_B = r"D:\视频\26114\test_decompose"

# 非空像素占比（渲染覆盖度）的合格区间
COV_MIN, COV_MAX = 0.40, 0.70
# 帧间平均像素差的有效阈值（实测约 11.7，留足余量但远大于浮点噪声）
DIFF_MIN = 1.0
# 节点数下限
NODE_MIN = 5


class Checker:
    """极简 PASS/FAIL 记录器，最后汇总。"""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def section(self, title):
        print("\n" + "=" * 72)
        print(title)
        print("=" * 72)

    def check(self, name, cond, detail=""):
        tag = "PASS" if cond else "FAIL"
        line = "%s  %s" % (tag, name)
        if detail:
            line += "   [%s]" % detail
        print(line)
        if cond:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(name)
        return bool(cond)

    def summary(self):
        print("\n" + "#" * 72)
        print("# 汇总: %d PASS / %d FAIL" % (self.passed, self.failed))
        if self.failures:
            print("# 失败项:")
            for f in self.failures:
                print("#   - " + f)
        print("#" * 72)


def coverage_and_diff(rig, t0=0.0, t1=2.3):
    """返回 (渲染非空像素占比, 两帧平均像素差)。"""
    f0 = C.render_frame(rig, t0)
    f1 = C.render_frame(rig, t1)
    cov = float((f0.sum(axis=2) > 0).mean())
    diff = float(np.abs(f0.astype(np.int16) - f1.astype(np.int16)).mean())
    return cov, diff


def check_source(ck, tag, src_dir):
    """对单个真实数据源跑 load -> build -> 断言。"""
    ck.section("数据源 [%s]  %s" % (tag, src_dir))

    meta = os.path.join(src_dir, "layers_meta.json")
    ck.check("%s: layers_meta.json 存在" % tag, os.path.isfile(meta), meta)

    layers, W, H = C.load_layers(meta, src_dir)
    ck.check("%s: load_layers 载入层数 > 0" % tag, len(layers) > 0,
             "%d 层 %dx%d" % (len(layers), W, H))

    rig = C.build_rig(layers, W, H)
    n_nodes = len(rig["nodes"])
    ck.check("%s: build_rig 节点数 >= %d" % (tag, NODE_MIN), n_nodes >= NODE_MIN,
             "节点=%d %s" % (n_nodes, [n["node"] for n in rig["nodes"]]))

    cov, diff = coverage_and_diff(rig)
    ck.check("%s: 渲染非空占比 ∈ [%.0f%%, %.0f%%]" % (tag, COV_MIN * 100, COV_MAX * 100),
             COV_MIN <= cov <= COV_MAX, "实测=%.3f" % cov)
    ck.check("%s: t=0.0 与 t=2.3 渲染有差异" % tag, diff > DIFF_MIN,
             "平均像素差=%.2f" % diff)
    return rig


def check_export(ck, tag, rig):
    """导出 PSD / AE 脚本 / JSON 到 OUT_DIR，并回读校验。"""
    ck.section("导出测试 [%s]  -> %s" % (tag, OUT_DIR))
    psd_path = os.path.join(OUT_DIR, "%s_rig_character.psd" % tag)
    jsx_path = os.path.join(OUT_DIR, "%s_rig_build.jsx" % tag)
    json_path = os.path.join(OUT_DIR, "%s_rig_spec.json" % tag)

    C.export_psd(rig, psd_path)
    ck.check("%s: export_psd 产物存在" % tag,
             os.path.isfile(psd_path) and os.path.getsize(psd_path) > 0,
             "%s (%d bytes)" % (os.path.basename(psd_path),
                                 os.path.getsize(psd_path) if os.path.isfile(psd_path) else -1))

    C.export_ae_jsx(rig, jsx_path)
    ck.check("%s: export_ae_jsx 产物存在" % tag,
             os.path.isfile(jsx_path) and os.path.getsize(jsx_path) > 0,
             "%d bytes" % (os.path.getsize(jsx_path) if os.path.isfile(jsx_path) else -1))

    C.export_json(rig, json_path)
    ck.check("%s: export_json 产物存在" % tag,
             os.path.isfile(json_path) and os.path.getsize(json_path) > 0,
             "%d bytes" % (os.path.getsize(json_path) if os.path.isfile(json_path) else -1))

    # --- PSD 回读：断言存在 Body 组和 Head 组 ---
    from psd_tools import PSDImage
    psd = PSDImage.open(psd_path)
    group_names = []

    def walk(layer):
        if layer.is_group():
            group_names.append(layer.name)
            for child in layer:
                walk(child)

    for layer in psd:
        walk(layer)
    ck.check("%s: PSD 回读含 Body 组" % tag, "Body" in group_names,
             "groups=%s" % group_names)
    ck.check("%s: PSD 回读含 Head 组" % tag, "Head" in group_names,
             "groups=%s" % group_names)

    # --- JSON 回读：节点数应与 rig 一致 ---
    with open(json_path, "r", encoding="utf8") as f:
        spec = json.load(f)
    ck.check("%s: JSON 节点数与 rig 一致" % tag,
             len(spec.get("nodes", [])) == len(rig["nodes"]),
             "json=%d rig=%d" % (len(spec.get("nodes", [])), len(rig["nodes"])))


def make_fake_dir(layers_meta, extra_files=None):
    d = tempfile.mkdtemp(prefix="l2d_selftest_", dir=TMP_ROOT)
    with open(os.path.join(d, "layers_meta.json"), "w", encoding="utf8") as f:
        json.dump(layers_meta, f, ensure_ascii=False)
    for name, img in (extra_files or {}).items():
        img.save(os.path.join(d, name))
    return d


def check_boundary(ck):
    ck.section("边界测试")

    # (1) 只有 1 层的假 meta：build_rig 不崩
    W = H = 128
    arr = np.zeros((H, W, 4), np.uint8)
    arr[40:80, 40:80, :] = (255, 0, 0, 255)
    d1 = make_fake_dir(
        {"width": W, "height": H, "layers": [
            {"name": "face", "filename": "layer_face.png",
             "left": 40, "top": 40, "right": 80, "bottom": 80,
             "depth_median": 0.5}]},
        {"layer_face.png": Image.fromarray(arr)},
    )
    try:
        layers1, W1, H1 = C.load_layers(os.path.join(d1, "layers_meta.json"), d1)
        ck.check("1层: load_layers 载入 1 层", len(layers1) == 1,
                 "%d 层 %dx%d" % (len(layers1), W1, H1))
        rig1 = C.build_rig(layers1, W1, H1)
        ck.check("1层: build_rig 不崩且有节点", len(rig1["nodes"]) >= 1,
                 "节点=%s" % [n["node"] for n in rig1["nodes"]])
    except Exception as e:
        ck.check("1层: build_rig 不崩", False, "%s: %s" % (type(e).__name__, e))
    finally:
        shutil.rmtree(d1, ignore_errors=True)

    # (2) 空 layers 列表：load_layers 返回 0 层，不抛异常
    d2 = make_fake_dir({"width": 64, "height": 64, "layers": []})
    try:
        layers2, W2, H2 = C.load_layers(os.path.join(d2, "layers_meta.json"), d2)
        ck.check("空layers: load_layers 返回 0 层", len(layers2) == 0,
                 "%d 层 %dx%d" % (len(layers2), W2, H2))
    except Exception as e:
        ck.check("空layers: load_layers 返回 0 层", False,
                 "抛异常 %s: %s" % (type(e).__name__, e))
    finally:
        shutil.rmtree(d2, ignore_errors=True)


def check_collect(ck):
    ck.section("decompose.collect 隔离测试（不碰真实 ComfyUI）")

    # 哨兵：证明测试绝不会调用 decompose()
    def _boom(*a, **k):
        raise AssertionError("selftest 不允许调用 decompose.decompose()（会拉起 ComfyUI）")

    old_decompose = decompose.decompose
    decompose.decompose = _boom

    fake_out = tempfile.mkdtemp(prefix="l2d_collect_src_", dir=TMP_ROOT)
    fake_dst = tempfile.mkdtemp(prefix="l2d_collect_dst_", dir=TMP_ROOT)
    old_c_out = decompose.C_OUT
    decompose.C_OUT = fake_out
    try:
        # 伪造 ComfyUI 输出。注意：collect 读 layers_meta.json 的 filename 字段
        # （权威的 层名->文件 映射）来搬运，因此这里必须按 filename 建文件。
        entries = [
            {"name": "face", "filename": "st_auto_00001_face.png",
             "depth_filename": "st_auto_00001_face_depth.png"},
            {"name": "front hair", "filename": "st_auto_00001_front hair.png"},
        ]
        with open(os.path.join(fake_out, "st_auto_00001_layers.json"), "w",
                  encoding="utf8") as f:
            json.dump({"width": 8, "height": 8, "layers": entries}, f,
                      ensure_ascii=False)
        Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(
            os.path.join(fake_out, "st_auto_00001_face.png"))
        Image.new("RGBA", (8, 8), (0, 255, 0, 255)).save(
            os.path.join(fake_out, "st_auto_00001_face_depth.png"))
        Image.new("RGBA", (8, 8), (0, 0, 255, 255)).save(
            os.path.join(fake_out, "st_auto_00001_front hair.png"))
        Image.new("RGB", (8, 8), (10, 20, 30)).save(
            os.path.join(fake_out, "st_auto_preview_00001_.png"))

        msgs = []
        meta = decompose.collect("st_auto", fake_dst, verbose=msgs.append)

        ck.check("collect: 返回 meta 路径", bool(meta) and os.path.isfile(meta),
                 str(meta))
        ck.check("collect: 写出 layers_meta.json",
                 os.path.isfile(os.path.join(fake_dst, "layers_meta.json")))
        ck.check("collect: 收集到 layer_face.png",
                 os.path.isfile(os.path.join(fake_dst, "layer_face.png")),
                 ", ".join(sorted(os.listdir(fake_dst))))
        ck.check("collect: 收集到 layer_face_depth.png",
                 os.path.isfile(os.path.join(fake_dst, "layer_face_depth.png")))
        ck.check("collect: 层名空格归一化为 layer_front_hair.png",
                 os.path.isfile(os.path.join(fake_dst, "layer_front_hair.png")))
        ck.check("collect: 收集到 seethrough_preview.png",
                 os.path.isfile(os.path.join(fake_dst, "seethrough_preview.png")))
    except Exception as e:
        ck.check("collect: 执行不抛异常", False, "%s: %s" % (type(e).__name__, e))
    finally:
        decompose.decompose = old_decompose
        decompose.C_OUT = old_c_out
        shutil.rmtree(fake_out, ignore_errors=True)
        shutil.rmtree(fake_dst, ignore_errors=True)


def main():
    ck = Checker()
    ck.section("l2d_rig 离线自检（V1.0）")
    print("python :", sys.version.split()[0])
    print("core   :", C.__file__)
    print("tmp out:", OUT_DIR)

    os.makedirs(OUT_DIR, exist_ok=True)

    # --- 1. 两个真实数据源 ---
    rig_a = check_source(ck, "A", SRC_A)
    rig_b = check_source(ck, "B", SRC_B)

    # --- 2. 导出 + 回读 ---
    check_export(ck, "A", rig_a)
    check_export(ck, "B", rig_b)

    # --- 3. 边界 ---
    check_boundary(ck)

    # --- 4. collect 隔离测试 ---
    check_collect(ck)

    ck.summary()
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())
