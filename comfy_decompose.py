# -*- coding: utf-8 -*-
"""SeeThrough ComfyUI 客户端 —— A 卡 / 无 CUDA 的拆图通道

与 seethrough_local.py（N 卡进程内 nf4 引擎）互补。本模块只走 ComfyUI 的
HTTP API，全程用 /upload/image 送图、/view 取结果，**不依赖 ComfyUI 的安装
目录**，所以官方绿色版、ComfyUI Desktop、自装版都能直接用。

A 卡要点（实测自 custom_nodes/ComfyUI-See-through/nodes.py v0.2.2）：
  · quant_mode 必须 "none"（bf16）。"nf4" 依赖 bitsandbytes，CUDA 专属，
    AMD/ROCm 下会直接报错 —— 这是本模块存在的唯一原因。
  · auto_download=True：模型不在本地时由插件首次运行自动从 HuggingFace 拉，
    合计约 12.5GB（layerdiff3d 9.46GB + marigold 3.05GB），需耐心等待。
  · SeeThrough_SavePSD 节点**不产出 PSD**，它写分层 PNG + *_layers.json，
    PSD 由插件前端 "Download PSD" 按钮在浏览器里合成。我们要的正是 PNG+json，
    与本项目 load_layers 的契约完全一致，无需 PSD 中间格式。

节点签名（提交给 /prompt 的 API 格式）：
  SeeThrough_LoadLayerDiffModel(model, vae_ckpt, unet_ckpt, quant_mode,
                                cache_tag_embeds, group_offload, auto_download)
  SeeThrough_LoadDepthModel(model, quant_mode, cache_tag_embeds, group_offload,
                            auto_download)
  SeeThrough_GenerateLayers(image, layerdiff_model, seed, resolution,
                            num_inference_steps) -> (layers, preview)
  SeeThrough_GenerateDepth(layers, depth_model, seed, resolution_depth)
                            -> (layers_depth, preview)
  SeeThrough_PostProcess(layers_depth, tblr_split, use_lama) -> (parts, preview)
  SeeThrough_SavePSD(parts, filename_prefix) -> (info_file,)   # OUTPUT_NODE
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_API_PORTS = (8188, 8000, 8189)
PLUGIN_NODES = (
    "SeeThrough_LoadLayerDiffModel",
    "SeeThrough_GenerateLayers",
    "SeeThrough_PostProcess",
    "SeeThrough_SavePSD",
)

# 默认 repo id 与插件 DEFAULT_* 保持一致（combo 输入必须命中列表内取值）
LAYERDIFF_REPO = "layerdifforg/seethroughv0.0.2_layerdiff3d"
DEPTH_REPO = "layerdifforg/seethroughv0.0.1_marigold"
LAYERDIFF_NF4 = "24yearsold/seethroughv0.0.2_layerdiff3d_nf4"
DEPTH_NF4 = "24yearsold/seethroughv0.0.1_marigold_nf4"


# ---------------- 基础 HTTP ----------------
def _req(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)


def get_json(api, path, timeout=60):
    with _req(api + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf8", "ignore"))


def get_bytes(api, path, timeout=180):
    with _req(api + path, timeout=timeout) as r:
        return r.read()


# ---------------- 发现服务 ----------------
def probe(timeout=4, ports=DEFAULT_API_PORTS):
    """返回可用的 ComfyUI base url，没有则 None。
    环境变量 COMFY_API 是**限定**而非优先：指定了就只连它，失败返回 None。
    （曾经把它当优先，结果"服务器没开"的探测回退到本机 8188，
    把一个本该失败的测试变成了真提交任务——配置就该被尊重。）"""
    env = os.environ.get("COMFY_API")
    cands = [env.rstrip("/")] if env else [
        "http://127.0.0.1:%d" % p for p in ports]
    for base in cands:
        try:
            get_json(base, "/system_stats", timeout=timeout)
            return base
        except Exception:
            continue
    return None


def device_info(api, timeout=8):
    """[(name, type)] —— ROCm 下 torch 也把 GPU 报成 cuda，故不能据此判卡；
    仅用于日志展示。"""
    try:
        d = get_json(api, "/system_stats", timeout=timeout)
        return [(x.get("name", "?"), x.get("type", "?"))
                for x in d.get("devices", [])]
    except Exception:
        return []


def has_plugin(api, timeout=20):
    """See-through 插件是否已装（缺节点时给出精确指引，而不是让 ComfyUI 报 400）"""
    try:
        info = get_json(api, "/object_info", timeout=timeout)
    except Exception:
        return None
    return [n for n in PLUGIN_NODES if n in info]


# ---------------- 工作流 ----------------
def build_graph(img_name, quant="none", resolution=1024, depth_res=768,
                steps=30, seed=42, use_lama=False, auto_download=True,
                prefix="muppet"):
    """quant='none' 给 A 卡/ROCm；'nf4' 仅 N 卡且显存紧张时用。
    use_lama 默认 False：LaMa 需要额外权重，关掉走 OpenCV 回退也能出层。"""
    ld_repo = LAYERDIFF_NF4 if quant == "nf4" else LAYERDIFF_REPO
    dp_repo = DEPTH_NF4 if quant == "nf4" else DEPTH_REPO
    return {
        "1": {"class_type": "LoadImage",
              "inputs": {"image": img_name}},   # upload 是前端控件，API 不认
        "2": {"class_type": "SeeThrough_LoadLayerDiffModel",
              "inputs": {"model": ld_repo, "vae_ckpt": "", "unet_ckpt": "",
                         "quant_mode": quant, "cache_tag_embeds": True,
                         "group_offload": False,
                         "auto_download": bool(auto_download)}},
        "3": {"class_type": "SeeThrough_LoadDepthModel",
              "inputs": {"model": dp_repo, "quant_mode": quant,
                         "cache_tag_embeds": True, "group_offload": False,
                         "auto_download": bool(auto_download)}},
        "4": {"class_type": "SeeThrough_GenerateLayers",
              "inputs": {"image": ["1", 0], "layerdiff_model": ["2", 0],
                         "seed": int(seed), "resolution": int(resolution),
                         "num_inference_steps": int(steps)}},
        "5": {"class_type": "SeeThrough_GenerateDepth",
              "inputs": {"layers": ["4", 0], "depth_model": ["3", 0],
                         "seed": int(seed),
                         "resolution_depth": int(depth_res)}},
        "6": {"class_type": "SeeThrough_PostProcess",
              "inputs": {"layers_depth": ["5", 0], "tblr_split": True,
                         "use_lama": bool(use_lama)}},
        "7": {"class_type": "SeeThrough_SavePSD",
              "inputs": {"parts": ["6", 0], "filename_prefix": prefix}},
    }


def upload(api, img_path, timeout=120):
    """用 /upload/image 送图（免猜 input 目录）。返回 ComfyUI 侧的文件名。"""
    from PIL import Image
    import io
    name = "muppet_in_%d.png" % int(time.time())
    buf = io.BytesIO()
    Image.open(img_path).convert("RGB").save(buf, format="PNG")
    # 手工拼 multipart，避免额外依赖
    boundary = "----muppetboundary"
    head = ("--%s\r\nContent-Disposition: form-data; name=\"image\"; "
            "filename=\"%s\"\r\nContent-Type: image/png\r\n\r\n" % (boundary, name)).encode()
    tail = ("\r\n--%s--\r\n" % boundary).encode()
    data = head + buf.getvalue() + tail
    req = urllib.request.Request(
        api + "/upload/image", data=data,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode("utf8", "ignore"))
    return resp.get("name", name)


def submit(api, img_name, graph_kw, client_id="muppet-studio", timeout=120):
    """提交 /prompt。校验失败(400)时把 ComfyUI 的错误翻译成一句人话。"""
    payload = json.dumps({"prompt": build_graph(img_name, **graph_kw),
                          "client_id": client_id}).encode()
    req = urllib.request.Request(api + "/prompt", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf8", "ignore"))["prompt_id"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf8", "ignore")
        try:
            j = json.loads(detail)
            errs = []
            for it in j.get("node_errors", {}).values():
                for err in it.get("errors", []):
                    errs.append("%s: %s" % (err.get("type"), err.get("detail", "")))
            if j.get("error"):
                errs.append(j["error"].get("message", ""))
            detail = "; ".join(x for x in errs if x) or detail[:300]
        except Exception:
            detail = detail[:300]
        raise RuntimeError("工作流被拒绝(%s): %s" % (e.code, detail))


def queue_position(api, pid, timeout=15):
    try:
        q = get_json(api, "/queue", timeout=timeout)
        for i, item in enumerate(q.get("queue_running", []) or []):
            if item[1] == pid:
                return "running", 0
        for i, item in enumerate(q.get("queue_pending", []) or []):
            if item[1] == pid:
                return "pending", i + 1
    except Exception:
        pass
    return None, None


def wait_done(api, pid, timeout=3600, verbose=print, on_progress=None,
              poll=10):
    """轮询 /history。A 卡首跑含模型下载，timeout 给足 1 小时。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            h = get_json(api, "/history/" + pid, timeout=30)
        except Exception:
            time.sleep(poll)
            continue
        if pid in h:
            it = h[pid]
            st = it.get("status", {})
            s = st.get("status_str")
            verbose("状态=%s 用时 %.0fs" % (s, time.time() - t0))
            if s == "error":
                for m in st.get("messages", [])[:4]:
                    verbose("  错误: " + json.dumps(m, ensure_ascii=False)[:400])
                return None
            return it
        state, pos = queue_position(api, pid)
        if state == "pending":
            verbose("排队中（前面还有 %d 个任务）…" % pos)
        elif on_progress:
            on_progress(int((time.time() - t0)))
        time.sleep(poll)
    verbose("等待超时（A 卡首跑要下 12.5GB 模型，可重跑续上）")
    return None


# ---------------- 结果收集 ----------------
def _view(api, filename, timeout=180):
    return get_bytes(api, "/view?filename=%s&type=output&t=%d" % (
        urllib.parse.quote(filename), int(time.time())), timeout=timeout)


def _info_from_history(hist):
    """从本次 history 里捞 SavePSD 落的 *_layers.json 文件名。
    优先用它而不是全局 seethrough_psd_info.log —— 后者是"最后一次跑"的
    全局状态，多人/多任务并发时会拿到别人的结果。"""
    for nid, out in (hist or {}).items():
        for key in ("text", "images", "gifs", "files"):
            for v in (out.get(key) or []):
                cands = [v] if isinstance(v, str) else (
                    [v.get("filename"), v.get("url")] if isinstance(v, dict) else [])
                for c in cands:
                    c = str(c or "")
                    if c.endswith("_layers.json"):
                        return os.path.basename(c.split("?")[0])
    return None


def collect(api, out_dir, prefix="muppet", verbose=print, hist=None, since=None):
    """json 名来源：本次 history（准）> 全局 seethrough_psd_info.log（回退）。
    实测插件 SavePSD 的 info_path 字符串**不会进 /history.outputs**（那里只有
    注册了 ui 的图像类输出），所以常态走回退路径 —— 而全局 log 是"最后一次跑"
    的状态，必须用 json 里的 prefix/timestamp 校验归属，否则并发时会静默导入
    别人的分层。"""
    os.makedirs(out_dir, exist_ok=True)
    info_name = _info_from_history(hist.get("outputs") if hist else None)
    src = "本次 history"
    if not info_name:
        try:
            info_name = _view(api, "seethrough_psd_info.log", timeout=30).decode(
                "utf8", "ignore").strip()
            src = "全局 info.log"
        except Exception as e:
            verbose("读不到 seethrough_psd_info.log：%s" % e)
            return None
    if not info_name.endswith(".json"):
        verbose("info 名异常: %r" % info_name[:80])
        return None
    try:
        meta = json.loads(_view(api, info_name, timeout=60).decode("utf8", "ignore"))
    except Exception as e:
        verbose("读不到 %s：%s" % (info_name, e))
        return None
    # 归属校验一：前缀
    mp = str(meta.get("prefix", ""))
    if prefix and mp and mp != prefix:
        verbose("清单前缀 %r ≠ 本次 %r（%s），ComfyUI 可能被别的任务同时使用，"
                "已放弃收集以免导入他人结果" % (mp, prefix, src))
        return None
    # 归属校验二：时间戳不早于本次提交（json 里形如 20261110_001902_uid）
    ts = str(meta.get("timestamp", ""))
    if since and len(ts) >= 15:
        try:
            t = time.mktime(time.strptime(ts[:15], "%Y%m%d_%H%M%S"))
            if t < since - 120:
                verbose("清单时间 %s 早于本次提交，判定为陈旧结果" % ts)
                return None
        except ValueError:
            pass
    layers = meta.get("layers", [])
    if not layers:
        verbose("%s 里没有层" % info_name)
        return None
    verbose("结果清单 %s（%s，前缀 %s，时间 %s）" % (info_name, src, mp, ts))
    with open(os.path.join(out_dir, "layers_meta.json"), "w", encoding="utf8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    n = 0
    for it in layers:
        for key, suffix in (("filename", ""), ("depth_filename", "_depth")):
            src_name = it.get(key)
            if not src_name:
                continue
            dst = os.path.join(out_dir, src_name)
            if os.path.exists(dst) and os.path.getsize(dst) > 0:
                n += 1
                continue
            try:
                with open(dst, "wb") as f:
                    f.write(_view(api, src_name))
                n += 1
            except Exception as e:
                verbose("  取回失败 %s: %s" % (src_name, e))
    verbose("收集 %d 层 -> %s" % (len(layers), out_dir))
    return os.path.join(out_dir, "layers_meta.json")


# ---------------- 一站式 ----------------
def decompose(img_path, out_dir, quant="none", resolution=1024, depth_res=768,
              steps=30, seed=42, use_lama=False, auto_download=True,
              prefix="muppet", verbose=print, on_progress=None, timeout=3600):
    """发现服务 -> 校验插件 -> 上传 -> 提交 -> 等待 -> 收集。
    返回 layers_meta.json 路径；失败返回 None（原因已走 verbose 播报）。"""
    api = probe()
    if not api:
        verbose("没找到在跑的 ComfyUI（试过端口 %s）。"
                "请先启动 ComfyUI，或看安装目录的 A卡拆图指引.txt"
                "（含官方下载 https://www.comfy.org/download 与插件安装步骤）。"
                % ", ".join(str(p) for p in DEFAULT_API_PORTS))
        return None
    devs = device_info(api)
    verbose("已连接 ComfyUI %s%s" % (api, (" 设备: " + ", ".join(
        "%s(%s)" % d for d in devs)) if devs else ""))
    nodes = has_plugin(api)
    if nodes is None:
        verbose("ComfyUI 无响应 /object_info")
        return None
    if len(nodes) < len(PLUGIN_NODES):
        verbose("ComfyUI 在跑但缺 See-through 插件（只找到 %d/%d 个节点）。"
                "请在 Manager 里搜索 see-through 安装后重启，"
                "或按 A卡拆图指引.txt 把镜像 zip 解压到 custom_nodes 后重启。"
                % (len(nodes), len(PLUGIN_NODES)))
        return None
    if not os.path.exists(img_path):
        verbose("图片不存在: %s" % img_path)
        return None
    verbose("上传原图…")
    name = upload(api, img_path)
    since = time.time()          # 提交时刻，供 collect 做陈旧结果校验
    kw = dict(quant=quant, resolution=resolution, depth_res=depth_res,
              steps=steps, seed=seed, use_lama=use_lama,
              auto_download=auto_download, prefix=prefix)
    verbose("提交工作流（quant_mode=%s%s）…" % (
        quant, "，模型不在本地时会自动从 HuggingFace 下载约 12.5GB，"
               "落点在 C 盘 ~/.cache\\huggingface（可用 HF_HOME 改盘），可能很久"
        if auto_download else ""))
    pid = submit(api, name, kw)
    verbose("已提交 prompt_id=%s" % pid[:12])
    hist = wait_done(api, pid, timeout=timeout, verbose=verbose,
                     on_progress=on_progress)
    if hist is None:
        return None
    return collect(api, out_dir, prefix=prefix, verbose=verbose,
                   hist=hist, since=since)


if __name__ == "__main__":
    import sys
    img = sys.argv[1] if len(sys.argv) > 1 else "test_input.png"
    out = sys.argv[2] if len(sys.argv) > 2 else "comfy_out"
    q = sys.argv[3] if len(sys.argv) > 3 else "none"
    r = decompose(img, out, quant=q)
    print("RESULT:", r)
