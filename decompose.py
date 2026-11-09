# -*- coding: utf-8 -*-
"""SeeThrough 拆分客户端（被骨架工作室调用）

SeeThrough 不是本地库，它跑在 ComfyUI 的 HTTP API 上，所以本模块负责：
  1) 确保 ComfyUI 在跑（不在就拉起来）
  2) 把图片送进 ComfyUI input，提交 SeeThrough 工作流
  3) 轮询结果，收集 17 层 PNG + layers_meta.json
  4) 交给 l2d_rig_core 建骨架

本机已验证可用的配置（8GB 显存 / RTX 4060 Laptop）：
  quant_mode=nf4  group_offload=False  cache_tag_embeds=False  auto_download=False
  resolution=1024  depth=768  steps=30  use_lama=False
  约 480 秒 / 张
"""
import json
import os
import sys
import subprocess
import time
import urllib.request
import numpy as np
from PIL import Image

API = "http://127.0.0.1:8188"
_ROOT = os.path.dirname(os.path.abspath(__file__))

def _find_comfy():
    """探测 ComfyUI 安装目录：环境变量 COMFY_DIR > 应用同目录 comfy.ini >
    常见安装位置。找到含 main.py 的目录才算数。"""
    cands = []
    env = os.environ.get("COMFY_DIR")
    if env:
        cands.append(env)
    ini = os.path.join(_ROOT, "comfy.ini")
    if os.path.exists(ini):
        for ln in open(ini, encoding="utf8"):
            ln = ln.strip()
            if ln.lower().startswith("dir="):
                cands.append(ln[4:].strip())
    cands += [r"D:\ComfyUI\main", r"D:\ComfyUI",
              os.path.join(_ROOT, "ComfyUI")]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "main.py")):
            return c
    return cands[0] if cands[0] else r"D:\ComfyUI\main"

COMFY_DIR = _find_comfy()
C_IN = os.path.join(COMFY_DIR, "input")
C_OUT = os.path.join(COMFY_DIR, "output")
CKPT = "SDXL\\waiIllustriousSDXL_v170.safetensors"
LORA = "chihaya_anon_fullclothes_ver.safetensors"
PY = sys.executable if hasattr(sys, "executable") else r"D:\Python312\python.exe"
LOG = os.path.join(_ROOT, "comfy_launch.log")


# ---------------- ComfyUI 保活 ----------------
def is_up(port=8188, timeout=5):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/system_stats" % port, timeout=timeout).read()
        return True
    except Exception:
        return False


def ensure_comfy(wait=180, verbose=print):
    """ComfyUI 不在就跑起来，等它就绪"""
    if is_up():
        verbose("ComfyUI 已在运行")
        return True
    verbose("ComfyUI 未运行，正在启动…")
    subprocess.Popen([PY, "main.py", "--listen", "0.0.0.0", "--port", "8188"],
                     cwd=COMFY_DIR,
                     stdout=open(LOG, "a"), stderr=subprocess.STDOUT)
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(3)
        if is_up():
            verbose("ComfyUI 就绪，耗时 %.0fs" % (time.time() - t0))
            return True
    verbose("ComfyUI 启动超时")
    return False


# ---------------- HTTP ----------------
def post(path, payload, timeout=120):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def get(path, timeout=60):
    return json.loads(urllib.request.urlopen(API + path, timeout=timeout).read().decode())


# ---------------- 工作流 ----------------
def build_graph(img_name, resolution, depth_res, steps, seed, use_lama):
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": img_name, "upload": "image"}},
        "2": {"class_type": "SeeThrough_LoadLayerDiffModel",
              "inputs": {"model": "layerdifforg/seethroughv0.0.2_layerdiff3d",
                         "vae_ckpt": "", "unet_ckpt": "",
                         "quant_mode": "nf4", "cache_tag_embeds": False,
                         "group_offload": False, "auto_download": False}},
        "3": {"class_type": "SeeThrough_LoadDepthModel",
              "inputs": {"model": "layerdifforg/seethroughv0.0.1_marigold",
                         "quant_mode": "nf4", "cache_tag_embeds": False,
                         "group_offload": False, "auto_download": False}},
        "4": {"class_type": "SeeThrough_GenerateLayers",
              "inputs": {"image": ["1", 0], "layerdiff_model": ["2", 0],
                         "seed": seed, "resolution": resolution,
                         "num_inference_steps": steps}},
        "5": {"class_type": "SeeThrough_GenerateDepth",
              "inputs": {"layers": ["4", 0], "depth_model": ["3", 0],
                         "seed": seed, "resolution_depth": depth_res}},
        "6": {"class_type": "SeeThrough_PostProcess",
              "inputs": {"layers_depth": ["5", 0], "tblr_split": True,
                         "use_lama": use_lama}},
        "7": {"class_type": "SeeThrough_SavePSD",
              "inputs": {"parts": ["6", 0], "filename_prefix": "st_auto"}},
        "8": {"class_type": "SeeThrough_PartsToLayers", "inputs": {"parts": ["6", 0]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"images": ["6", 1], "filename_prefix": "st_auto_preview"}},
    }


def submit(img_path, resolution=1024, depth_res=768, steps=30, seed=42,
           use_lama=False, verbose=print):
    if not os.path.exists(img_path):
        raise FileNotFoundError(img_path)
    name = "st_in_%d.png" % int(time.time())
    dst = os.path.join(C_IN, name)
    Image.open(img_path).convert("RGB").save(dst)
    verbose("已送入 ComfyUI: %s" % name)
    pid = post("/prompt", {"prompt": build_graph(name, resolution, depth_res,
                                                 steps, seed, use_lama),
                           "client_id": "l2d-studio"})["prompt_id"]
    verbose("已提交 prompt_id=%s" % pid[:8])
    return pid


def wait_done(pid, timeout=1800, verbose=print):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            h = get("/history/" + pid)
        except Exception:
            time.sleep(5)
            continue
        if pid in h:
            it = h[pid]
            st = it.get("status", {})
            verbose("状态=%s 用时 %.0fs" % (st.get("status_str"), time.time() - t0))
            if st.get("status_str") == "error":
                for m in st.get("messages", []):
                    verbose("  错误: " + json.dumps(m, ensure_ascii=False)[:500])
                return None
            return it
        time.sleep(15)
    verbose("超时")
    return None


def collect(prefix, out_dir, verbose=print):
    """把 ComfyUI 输出目录里属于该 prefix 的层文件收进 out_dir。

    注意：ComfyUI 存的文件名形如 `st_auto_<时间戳>_<hash>_<层名>.png`，
    不能按 prefix 简单裁剪（会把时间戳留在 tag 里），必须读 layers_meta.json
    的 filename 字段来搬运 —— 那里才是权威的 层名->文件 映射。
    """
    import shutil
    os.makedirs(out_dir, exist_ok=True)
    meta_src = None
    for f in sorted(os.listdir(C_OUT)):
        if f.startswith(prefix) and f.endswith("_layers.json"):
            meta_src = os.path.join(C_OUT, f)
    if not meta_src:
        verbose("没找到 layers.json")
        return None
    meta = json.load(open(meta_src, encoding="utf8"))
    shutil.copy2(meta_src, os.path.join(out_dir, "layers_meta.json"))
    n = 0
    for it in meta["layers"]:
        name = it["name"]
        src = os.path.join(C_OUT, it["filename"])
        if not os.path.exists(src):
            verbose("  缺文件 %s" % it["filename"])
            continue
        tag = name.replace(" ", "_")
        shutil.copy2(src, os.path.join(out_dir, "layer_" + tag + ".png"))
        df = it.get("depth_filename")
        if df:
            dp = os.path.join(C_OUT, df)
            if os.path.exists(dp):
                shutil.copy2(dp, os.path.join(out_dir, "layer_" + tag + "_depth.png"))
        n += 1
    pv = os.path.join(C_OUT, prefix + "_preview_00001_.png")
    if os.path.exists(pv):
        shutil.copy2(pv, os.path.join(out_dir, "seethrough_preview.png"))
    verbose("收集到 %d 层 + 元数据 -> %s" % (n, out_dir))
    return os.path.join(out_dir, "layers_meta.json")


def decompose(img_path, out_dir, resolution=1024, depth_res=768, steps=30,
              seed=42, use_lama=False, verbose=print):
    """完整流程：保活 -> 提交 -> 等待 -> 收集"""
    if not ensure_comfy(verbose=verbose):
        return None
    pid = submit(img_path, resolution, depth_res, steps, seed, use_lama, verbose)
    it = wait_done(pid, verbose=verbose)
    if it is None:
        return None
    return collect("st_auto", out_dir, verbose)


if __name__ == "__main__":
    img = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_ROOT, "test_input.png")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(_ROOT, "seethrough_out", "cli_test")
    r = decompose(img, out)
    print("RESULT:", r)
