# -*- coding: utf-8 -*-
r"""SeeThrough 本地 runner —— 完全脱离 ComfyUI 的拆分引擎

直接调用 vendored 的 see-through 核心库（纯 diffusers 代码），模型用
D:\work\puppet\l2d_rig\models\24yearsold\ 下的 nf4 量化副本，进程内完成
LayerDiff 分层 + Marigold 深度 + 后处理 + 落盘。

与旧 ComfyUI HTTP 路径（decompose.py，已下线删除）输出格式完全一致：
  out_dir/layers_meta.json + layer_<名>.png + layer_<名>_depth.png + seethrough_preview.png

依赖（D:\Python312 全部已装）：torch/diffusers/peft/accelerate/bitsandbytes/sklearn/cv2
显存峰值 ~3.3GB（nf4），单次全流程 ~14 分钟。

用法：
    import seethrough_local
    meta = seethrough_local.decompose(img_path, out_dir, verbose=print)
"""
import json
import os
import sys
import random
import types
from datetime import datetime

ST_ROOT = os.path.dirname(os.path.abspath(__file__))
# 模型目录：优先自带 models\（唯一实际使用的路径）；
# ../l2d_rig/models 的 V1 回退仅为兼容旧部署保留——V1 已于 2026-11-10 退役，
# 源码归档在本仓库 legacy/v1/，本地目录已删除，该回退不会再命中。
MODELS_DIR = os.path.join(ST_ROOT, "models")
if not os.path.isdir(MODELS_DIR):
    MODELS_DIR = os.path.normpath(os.path.join(ST_ROOT, "..", "l2d_rig", "models"))
ST_CORE = os.path.join(ST_ROOT, "see-through")   # 核心库已随应用自包含
ST_COMMON = os.path.join(ST_CORE, "common")

NF4_LAYERDIFF = "24yearsold/seethroughv0.0.2_layerdiff3d_nf4"
NF4_DEPTH = "24yearsold/seethroughv0.0.1_marigold_nf4"


# ---------------- 设备选择（v3.1.0 CPU 回退链） ----------------
def pick_device():
    """MUPPET_FORCE_DEVICE=cuda|cpu 强制优先；否则自动探测。
    CPU 分支用纯 torch fp32 + nf4 反量化，A 卡/无卡机器可跑（慢，见 decompose 预警）。"""
    force = os.environ.get("MUPPET_FORCE_DEVICE", "").strip().lower()
    if force in ("cuda", "cpu"):
        return force
    return "cuda" if torch.cuda.is_available() else "cpu"


def _empty_cache():
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

VALID_BODY_PARTS_V2 = [
    "hair", "headwear", "face", "eyes", "eyewear", "ears", "earwear",
    "nose", "mouth", "neck", "neckwear", "topwear", "handwear",
    "bottomwear", "legwear", "footwear", "tail", "wings", "objects",
]


def _import_core():
    """sys.path 注入 + utils/modules 冲突备份（照抄 nodes.py 的做法）"""
    try:
        import pycocotools  # noqa
    except ImportError:
        _mock = types.ModuleType("pycocotools")
        _mask = types.ModuleType("pycocotools.mask")
        _mock.mask = _mask
        sys.modules["pycocotools"] = _mock
        sys.modules["pycocotools.mask"] = _mask
    backup = {}
    for prefix in ("utils", "modules"):
        for key in list(sys.modules.keys()):
            if key == prefix or key.startswith(prefix + "."):
                backup[key] = sys.modules.pop(key)
    if ST_COMMON not in sys.path:
        sys.path.insert(0, ST_COMMON)
    if ST_CORE not in sys.path:
        sys.path.insert(1, ST_CORE)
    import cv2  # noqa
    from modules.layerdiffuse.diffusers_kdiffusion_sdxl import KDiffusionStableDiffusionXLPipeline
    from modules.layerdiffuse.layerdiff3d import UNetFrameConditionModel
    from modules.layerdiffuse.vae import TransparentVAE
    from modules.marigold import MarigoldDepthPipeline
    from utils.cv import center_square_pad_resize, img_alpha_blending, smart_resize
    from utils.torchcv import cluster_inpaint_part
    for key, mod in backup.items():
        if key not in sys.modules:
            sys.modules[key] = mod
    return dict(cv2=cv2, KDiffusionStableDiffusionXLPipeline=KDiffusionStableDiffusionXLPipeline,
                UNetFrameConditionModel=UNetFrameConditionModel, TransparentVAE=TransparentVAE,
                MarigoldDepthPipeline=MarigoldDepthPipeline,
                center_square_pad_resize=center_square_pad_resize,
                img_alpha_blending=img_alpha_blending, smart_resize=smart_resize,
                cluster_inpaint_part=cluster_inpaint_part)


import numpy as np
import torch
import cv2
from PIL import Image


def _log_vram(label):
    if torch.cuda.is_available():
        a = torch.cuda.memory_allocated() / 1024 ** 3
        print("[ST VRAM] %s: allocated=%.2fGB" % (label, a), flush=True)


def _cast_non_quantized_params(model, dtype):
    try:
        import bitsandbytes as bnb
        bnb_types = (bnb.nn.Linear4bit, bnb.nn.Linear8bitLt)
    except ImportError:
        bnb_types = ()
    for module in model.modules():
        if bnb_types and isinstance(module, bnb_types):
            continue
        for param in module.parameters(recurse=False):
            if param.data.is_floating_point():
                param.data = param.data.to(dtype=dtype)
        for buf in module.buffers(recurse=False):
            if buf.data.is_floating_point():
                buf.data = buf.data.to(dtype=dtype)


def seed_everything(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


# ---------------- CPU 分支：nf4 反量化加载器 ----------------
# 与 CUDA 上 bnb 原生 dequantize 金标准比对过（D:\scripts\temp\nf4_dequant_probe.py，
# 最大相对误差 0.12% = bf16 舍入级）。序列化格式：
#   xxx.weight        uint8 packed，**高位 nibble 先**
#   xxx.weight.absmax fp32，block=64
#   xxx.weight.quant_map      16 值 nf4 表（在模型文件里，零 bnb 依赖）
#   xxx.weight.quant_state.bitsandbytes__nf4   JSON（真实 shape/blocksize）

def _dequant_nf4(packed_u8, absmax, qmap, block=64):
    flat = packed_u8.reshape(-1)
    hi = (flat >> 4).to(torch.int64)
    lo = (flat & 0x0F).to(torch.int64)
    nib = torch.stack((hi, lo), dim=1).reshape(-1)
    vals = qmap.to(torch.float32)[nib]
    am = absmax.to(torch.float32).reshape(-1, 1)
    return (vals.reshape(-1, block) * am).reshape(-1)


def _load_state_dir(d):
    from safetensors.torch import load_file
    index = os.path.join(d, "model.safetensors.index.json")
    sd = {}
    if os.path.isfile(index):
        with open(index, encoding="utf8") as f:
            shards = sorted(set(json.load(f)["weight_map"].values()))
    else:
        shards = sorted(fn for fn in os.listdir(d) if fn.endswith(".safetensors"))
    for sh in shards:
        sd.update(load_file(os.path.join(d, sh)))
    return sd


def _dequant_state_dict(sd):
    """量化层替换为 fp32 反量化值；剔除 absmax/quant_map/quant_state 辅助键。"""
    out = {}
    skip_suffix = (".absmax", ".quant_map", ".original_shape", ".original_dtype")
    for k, v in sd.items():
        if k.endswith(skip_suffix) or ".quant_state." in k:
            continue
        if k + ".absmax" in sd and k + ".quant_map" in sd:
            qs_key = k + ".quant_state.bitsandbytes__nf4"
            shape = None
            if qs_key in sd:
                try:
                    shape = tuple(json.loads(bytes(sd[qs_key].tolist()))["shape"])
                except Exception:
                    shape = None
            flat = _dequant_nf4(v, sd[k + ".absmax"], sd[k + ".quant_map"])
            out[k] = flat.reshape(shape).float() if shape else flat.float()
        else:
            out[k] = v.float() if v.is_floating_point() else v
    return out


def _load_component_cpu(dirpath, verbose):
    """组件目录 → fp32 模型（config + safetensors，不经过 bitsandbytes）。"""
    cfg = json.load(open(os.path.join(dirpath, "config.json"), encoding="utf8"))
    cfg.pop("quantization_config", None)
    cls_name = cfg.get("_class_name") or (cfg.get("architectures") or [None])[0]
    model = None
    if cls_name in ("CLIPTextModel", "CLIPTextModelWithProjection"):
        import transformers
        from transformers import CLIPTextConfig
        Cls = getattr(transformers, cls_name)
        model = Cls(CLIPTextConfig(**{k: v for k, v in cfg.items()
                                      if not k.startswith("_")}))
    elif cls_name == "UNetFrameConditionModel":
        from modules.layerdiffuse.layerdiff3d import UNetFrameConditionModel as Cls
    elif cls_name == "TransparentVAE":
        from modules.layerdiffuse.vae import TransparentVAE as Cls
    elif cls_name == "AutoencoderKL":
        from diffusers import AutoencoderKL as Cls
    else:
        raise RuntimeError("CPU 分支不识别组件类: %s (%s)" % (cls_name, dirpath))
    sd = _dequant_state_dict(_load_state_dir(dirpath))
    if model is None:
        model = Cls.from_config(cfg)
    _fit_state_dict(model, sd, dirpath)
    return model.to(torch.float32)


def _fit_state_dict(model, sd, dirpath):
    """手动 load 需匹配模型期望的 key 前缀（transformers from_pretrained 会自动转换，
    我们没有那层转换）：依次尝试 原样 / 去 text_model. 前缀 / 加 text_model. 前缀。"""
    def allowed(u):
        return u.startswith("visual_projection.") or u in ("logit_scale", "logit_bias")
    variants = [sd]
    if any(k.startswith("text_model.") for k in sd):
        variants.append({(k[len("text_model."):] if k.startswith("text_model.") else k): v
                         for k, v in sd.items()})
    if not any(k.startswith("text_model.") for k in sd):
        _top = ("text_projection", "visual_projection", "logit_scale", "logit_bias")
        variants.append({("text_model." + k if k.split(".")[0] not in _top else k): v
                         for k, v in sd.items()})
    last = None
    for i, var in enumerate(variants):
        try:
            missing, unexpected = model.load_state_dict(var, strict=False)
        except Exception:
            continue
        unexpected = [u for u in unexpected if not allowed(u)]
        if not missing and not unexpected:
            if i > 0:
                print("  [CPU] %s 已自动适配 key 前缀变体 %d"
                      % (os.path.basename(dirpath), i), flush=True)
            return
        if last is None or len(missing) + len(unexpected) < len(last[0]) + len(last[1]):
            last = (missing, unexpected)
    raise RuntimeError("CPU 反量化加载键不匹配 %s missing=%d%s unexpected=%d%s" % (
        os.path.basename(dirpath), len(last[0]), list(last[0])[:5],
        len(last[1]), list(last[1])[:5]))


def _load_pipeline_cpu(verbose):
    """CPU 分支组装 pipeline。内存需求 ~20GB（fp32 全模型常驻）。"""
    import time as _time
    K = _import_core()
    pl_dir = _resolve(NF4_LAYERDIFF)
    dp_dir = _resolve(NF4_DEPTH)
    verbose("未检测到 CUDA：CPU 推理模式（fp32 反量化加载，约 2-3 小时/张）…")
    t0 = _time.time()
    from transformers import AutoTokenizer
    trans_vae = _load_component_cpu(os.path.join(pl_dir, "trans_vae"), verbose)
    unet = _load_component_cpu(os.path.join(pl_dir, "unet"), verbose)
    vae = _load_component_cpu(os.path.join(pl_dir, "vae"), verbose)
    text_encoder = _load_component_cpu(os.path.join(pl_dir, "text_encoder"), verbose)
    text_encoder_2 = _load_component_cpu(os.path.join(pl_dir, "text_encoder_2"), verbose)
    pipeline = K["KDiffusionStableDiffusionXLPipeline"](
        unet=unet, vae=vae, trans_vae=trans_vae,
        text_encoder=text_encoder, text_encoder_2=text_encoder_2,
        tokenizer=AutoTokenizer.from_pretrained(pl_dir, subfolder="tokenizer"),
        tokenizer_2=AutoTokenizer.from_pretrained(pl_dir, subfolder="tokenizer_2"),
        scheduler=None)
    pipeline._st_group_offload = False
    d_unet = _load_component_cpu(os.path.join(dp_dir, "unet"), verbose)
    d_vae = _load_component_cpu(os.path.join(dp_dir, "vae"), verbose)
    d_te = _load_component_cpu(os.path.join(dp_dir, "text_encoder"), verbose)
    marigold = K["MarigoldDepthPipeline"](
        unet=d_unet, vae=d_vae, text_encoder=d_te,
        tokenizer=AutoTokenizer.from_pretrained(dp_dir, subfolder="tokenizer"),
        scheduler=None)
    marigold._st_group_offload = False
    verbose("CPU 模型加载完成（%.0f 秒）" % (_time.time() - t0))
    return pipeline, marigold, K


def _resolve(model_name):
    p = os.path.join(MODELS_DIR, model_name.replace("/", os.sep))
    if os.path.isdir(p):
        return p
    raise FileNotFoundError("模型目录不存在: %s（请先复制 nf4 模型到 %s）" % (p, MODELS_DIR))


# ---------------- 模型加载 ----------------
def load_models(verbose=print):
    if pick_device() == "cpu":
        return _load_pipeline_cpu(verbose)
    K = _import_core()
    dev = torch.device("cuda")
    dtype = torch.bfloat16
    verbose("加载 LayerDiff 模型 (nf4)…")
    pretrained = _resolve(NF4_LAYERDIFF)
    trans_vae = K["TransparentVAE"].from_pretrained(pretrained, subfolder="trans_vae", local_files_only=True)
    unet = K["UNetFrameConditionModel"].from_pretrained(pretrained, subfolder="unet", local_files_only=True)
    pipeline = K["KDiffusionStableDiffusionXLPipeline"].from_pretrained(
        pretrained, trans_vae=trans_vae, unet=unet, scheduler=None, local_files_only=True)
    pipeline.vae.to(dtype=dtype)
    pipeline.trans_vae.to(dtype=dtype)
    _cast_non_quantized_params(pipeline.unet, dtype)
    _cast_non_quantized_params(pipeline.text_encoder, dtype)
    _cast_non_quantized_params(pipeline.text_encoder_2, dtype)
    pipeline._st_group_offload = False
    _log_vram("LayerDiff loaded")

    verbose("加载 Marigold 深度模型 (nf4)…")
    pretrained = _resolve(NF4_DEPTH)
    dunet = K["UNetFrameConditionModel"].from_pretrained(pretrained, subfolder="unet", local_files_only=True)
    marigold = K["MarigoldDepthPipeline"].from_pretrained(pretrained, unet=dunet, local_files_only=True)
    marigold.vae.to(dtype=dtype)
    _cast_non_quantized_params(marigold.unet, dtype)
    _cast_non_quantized_params(marigold.text_encoder, dtype)
    marigold._st_group_offload = False
    _log_vram("Depth loaded")

    # 关键：bitsandbytes 4bit 参数在 from_pretrained 时已落在 GPU，两个模型
    # 常驻 4.8GB，推理时再上卡会顶满 8GB 触发 WDDM 分页（实测龟速 50 分钟）。
    # 加载完全部强制下卡，推理阶段各函数按需 to(dev)/to(cpu)。
    for comp in (pipeline.unet, pipeline.vae, pipeline.trans_vae,
                 pipeline.text_encoder, pipeline.text_encoder_2,
                 marigold.unet, marigold.vae, marigold.text_encoder):
        comp.to("cpu")
    _empty_cache()
    _log_vram("All components offloaded to CPU after load")
    return pipeline, marigold, K


# ---------------- 分层 ----------------
class LayersData:
    def __init__(self, layer_dict, fullpage, input_img, resolution, pad_size, pad_pos):
        self.layer_dict, self.fullpage, self.input_img = layer_dict, fullpage, input_img
        self.resolution, self.pad_size, self.pad_pos = resolution, pad_size, pad_pos
        self.scale = pad_size[0] / resolution


class LayersDepthData:
    def __init__(self, layer_dict, depth_dict, fullpage, resolution):
        self.layer_dict, self.depth_dict = layer_dict, depth_dict
        self.fullpage, self.resolution = fullpage, resolution


def _crop_head(img, xywh):
    x, y, w, h = xywh
    ih, iw = img.shape[:2]
    x1, y1, x2, y2 = x, y, x + w, y + h
    if w < iw // 2:
        px = min(iw - x - w, x, w // 5)
        x1 = min(max(x - px, 0), iw)
        x2 = min(max(x + w + px, 0), iw)
    if h < ih // 2:
        py = min(ih - y - h, y, h // 5)
        y2 = min(max(y + h + py, 0), ih)
        y1 = min(max(y - py, 0), ih)
    return img[y1:y2, x1:x2], (x1, y1, x2, y2)


def generate_layers(pipeline, K, img_np, seed, resolution, steps, verbose):
    dev = torch.device(pick_device())
    offload = torch.device("cpu")
    seed_everything(seed)
    if img_np.shape[-1] == 3:
        img_np = np.concatenate([img_np, np.full((*img_np.shape[:2], 1), 255, np.uint8)], axis=-1)
    input_img = img_np.copy()
    fullpage, pad_size, pad_pos = K["center_square_pad_resize"](input_img, resolution, return_pad_info=True)
    scale = pad_size[0] / resolution
    tag_version = pipeline.unet.get_tag_version()
    verbose("分层推理开始 tag=%s res=%d steps=%d" % (tag_version, resolution, steps))

    has_cached = len(pipeline._cached_prompt_embeds) > 0
    if has_cached:
        encode_fn = pipeline.encode_cropped_prompt_77tokens_cached
    else:
        pipeline.text_encoder.to(dev)
        pipeline.text_encoder_2.to(dev)
        encode_fn = pipeline.encode_cropped_prompt_77tokens

    if tag_version == "v3":
        body_tags = ["front hair", "back hair", "head", "neck", "neckwear",
                     "topwear", "handwear", "bottomwear", "legwear", "footwear",
                     "tail", "wings", "objects"]
        head_tags = ["headwear", "face", "irides", "eyebrow", "eyewhite",
                     "eyelash", "eyewear", "ears", "earwear", "nose", "mouth"]
        body_embeds, body_pooled = encode_fn(body_tags)
        head_embeds, head_pooled = encode_fn(head_tags)
    else:
        prompt_embeds, pooled = encode_fn(VALID_BODY_PARTS_V2)
    if not has_cached:
        pipeline.text_encoder.to(offload)
        pipeline.text_encoder_2.to(offload)

    pipeline.unet.to(dev)
    pipeline.vae.to(dev)
    pipeline.trans_vae.to(dev)
    _empty_cache()
    _log_vram("LayerDiff on GPU, diffusion begin")
    rng = torch.Generator(device=dev).manual_seed(seed)
    layer_dict = {}

    if tag_version == "v2":
        out = pipeline(strength=1.0, num_inference_steps=steps, batch_size=1,
                       generator=rng, guidance_scale=1.0,
                       prompt_embeds=prompt_embeds, pooled_prompt_embeds=pooled,
                       fullpage=fullpage)
        for rst, tag in zip(out.images, VALID_BODY_PARTS_V2):
            layer_dict[tag] = rst
    else:
        out = pipeline(strength=1.0, num_inference_steps=steps, batch_size=1,
                       generator=rng, guidance_scale=1.0,
                       prompt_embeds=body_embeds, pooled_prompt_embeds=body_pooled,
                       fullpage=fullpage, group_index=0)
        _log_vram("body diffusion end")
        verbose("身体组完成")
        for rst, tag in zip(out.images, body_tags):
            layer_dict[tag] = rst
        head_img = out.images[2]
        nz = cv2.findNonZero((head_img[..., -1] > 15).astype(np.uint8))
        if nz is not None:
            hx0, hy0, hw, hh = cv2.boundingRect(nz)
            hx = int(hx0 * scale) - pad_pos[0]
            hy = int(hy0 * scale) - pad_pos[1]
            input_head, (hx1, hy1, hx2, hy2) = _crop_head(input_img, [hx, hy, int(hw * scale), int(hh * scale)])
            hx1 = int(hx1 / scale + pad_pos[0] / scale)
            hy1 = int(hy1 / scale + pad_pos[1] / scale)
            ih, iw = input_head.shape[:2]
            input_head, hps, hpp = K["center_square_pad_resize"](input_head, resolution, return_pad_info=True)
            out = pipeline(strength=1.0, num_inference_steps=steps, batch_size=1,
                           generator=rng, guidance_scale=1.0,
                           prompt_embeds=head_embeds, pooled_prompt_embeds=head_pooled,
                           fullpage=input_head, group_index=1)
            verbose("头部组完成")
            canvas = np.zeros((resolution, resolution, 4), dtype=np.uint8)
            coords = np.array([hpp[1], hpp[1] + ih, hpp[0], hpp[0] + iw])
            py1, py2, px1, px2 = (coords / scale).astype(np.int64)
            scale_size = (int(hps[0] / scale), int(hps[1] / scale))
            for rst, tag in zip(out.images, head_tags):
                rst = K["smart_resize"](rst, scale_size)[py1:py2, px1:px2]
                full = canvas.copy()
                full[hy1:hy1 + rst.shape[0], hx1:hx1 + rst.shape[1]] = rst
                layer_dict[tag] = full

    pipeline.unet.to(offload)
    pipeline.vae.to(offload)
    pipeline.trans_vae.to(offload)
    _empty_cache()
    verbose("分层完成 %d 层" % len(layer_dict))
    return LayersData(layer_dict, fullpage, input_img, resolution, pad_size, pad_pos)


# ---------------- 深度 ----------------
def generate_depth(marigold, K, layers, seed, resolution_depth, verbose):
    dev = torch.device(pick_device())
    offload = torch.device("cpu")
    layer_dict, fullpage, resolution = layers.layer_dict, layers.fullpage, layers.resolution
    verbose("深度推理开始…")
    empty = np.zeros((resolution, resolution, 4), dtype=np.uint8)
    blended_alpha = np.zeros((resolution, resolution), dtype=np.float32)
    compose_list = {"eyes": ["eyewhite", "irides", "eyelash", "eyebrow"],
                    "hair": ["back hair", "front hair"]}
    img_list = []
    for tag in VALID_BODY_PARTS_V2:
        if tag in layer_dict:
            ta = layer_dict[tag].copy()
            ta[..., -1][ta[..., -1] < 15] = 0
            img_list.append(ta)
        else:
            img_list.append(empty.copy())
    compose_dict = {}
    for c, clist in compose_list.items():
        imlist, taglist = [], []
        for t in clist:
            if t in layer_dict:
                ta = layer_dict[t].copy()
                ta[..., -1][ta[..., -1] < 15] = 0
                imlist.append(ta)
                taglist.append(t)
        if imlist:
            composed = K["img_alpha_blending"](imlist, premultiplied=False)
            img_list[VALID_BODY_PARTS_V2.index(c)] = composed
            compose_dict[c] = {"taglist": taglist, "imlist": imlist}
    for img in img_list:
        blended_alpha += img[..., -1].astype(np.float32) / 255
    blended_alpha = np.clip(blended_alpha, 0, 1) * 255
    blended_alpha = blended_alpha.astype(np.uint8)
    fpd = fullpage.copy()
    fpd[..., -1] = blended_alpha
    img_list.append(fpd)

    if resolution_depth > 0 and resolution_depth != resolution:
        dres = max(64, (resolution_depth // 8) * 8)
        inputs = [K["smart_resize"](im, (dres, dres)) for im in img_list]
    else:
        inputs, dres = img_list, resolution

    marigold.unet.to(dev)
    marigold.vae.to(dev)
    _empty_cache()
    seed_everything(seed)
    pipe_out = marigold(color_map=None, show_progress_bar=False, img_list=inputs)
    depth_pred = pipe_out.depth_tensor.to(device="cpu", dtype=torch.float32).numpy()
    if dres != resolution:
        depth_pred = np.stack([K["smart_resize"](d, (resolution, resolution)) for d in depth_pred])
    marigold.unet.to(offload)
    marigold.vae.to(offload)
    _empty_cache()

    depth_dict = {}
    for ii, tag in enumerate(VALID_BODY_PARTS_V2):
        depth = depth_pred[ii]
        if tag in compose_dict:
            mask_accum = np.zeros((resolution, resolution), bool)
            for t, im in zip(compose_dict[tag]["taglist"][::-1], compose_dict[tag]["imlist"][::-1]):
                mask_local = im[..., -1] > 15
                mask_invis = mask_accum & mask_local
                depth_local = np.full((resolution, resolution), 1.0, dtype=np.float32)
                depth_local[mask_local] = depth[mask_local]
                vis = mask_local & ~mask_invis
                if np.any(vis):
                    depth_local[mask_invis] = np.median(depth[vis])
                mask_accum = mask_accum | mask_local
                depth_dict[t] = depth_local
        else:
            depth_dict[tag] = np.clip(depth, 0, 1).astype(np.float32)
    verbose("深度完成 %d 张" % len(depth_dict))
    return LayersDepthData(layer_dict, depth_dict, fullpage, resolution)


# ---------------- 后处理（照抄 nodes.py PostProcess） ----------------
def _label_lr_split(labels, stats, id1, id2):
    label1 = (labels == id1).astype(np.uint8) * 255
    label2 = (labels == id2).astype(np.uint8) * 255
    x1 = stats[id1][0] + stats[id1][2] / 2
    x2 = stats[id2][0] + stats[id2][2] / 2
    if x2 < x1:
        return label2, label1, stats[id2], stats[id1]
    return label1, label2, stats[id1], stats[id2]


def _process_cuts(img, depth, src_xyxy, tgt_bbox, mask=None):
    tx1, ty1, tx2, ty2 = tgt_bbox[:4]
    tx2 += tx1
    ty2 += ty1
    img = img[ty1:ty2, tx1:tx2].copy()
    depth = depth[ty1:ty2, tx1:tx2]
    depth_median = 1.0
    if mask is not None:
        mask = (mask[ty1:ty2, tx1:tx2].copy() > 15).astype(np.uint8)
        element = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3), (1, 1))
        mask = cv2.dilate(mask, element)
        img[..., -1] *= mask
        depth = 1 - (1 - depth) * mask
        if np.any(mask):
            depth_median = float(np.median(depth[mask > 0]))
    fxyxy = [tx1 + src_xyxy[0], ty1 + src_xyxy[1], tx2 + src_xyxy[0], ty2 + src_xyxy[1]]
    return img, depth, fxyxy, depth_median


def _part_lr_split(tag, part_info):
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        part_info["mask"].astype(np.uint8) * 255, connectivity=8)
    tag2pinfo = {}
    if len(stats) > 2:
        stats = np.array(stats)
        order = np.argsort(stats[..., -1])[::-1][1:]
        ml, mr, sl, sr = _label_lr_split(labels, stats, order[0], order[1])
        # 与 nodes.py 完全一致：画面左(sl+ml)命名 "-r"（角色右），画面右(sr+mr)命名 "-l"
        img, depth, xyxy, dm = _process_cuts(part_info["img"], part_info["depth"],
                                             part_info["xyxy"], sl, mask=ml)
        tag2pinfo["%s-r" % tag] = {"img": img, "xyxy": xyxy, "depth": depth,
                                   "depth_median": dm, "tag": "%s-r" % tag}
        img, depth, xyxy, dm = _process_cuts(part_info["img"], part_info["depth"],
                                             part_info["xyxy"], sr, mask=mr)
        tag2pinfo["%s-l" % tag] = {"img": img, "xyxy": xyxy, "depth": depth,
                                   "depth_median": dm, "tag": "%s-l" % tag}
    else:
        tag2pinfo[tag] = part_info
    return tag2pinfo


def _tag_lr_split(tag, tag2pinfo):
    if tag in tag2pinfo:
        tag2pinfo.update(_part_lr_split(tag, tag2pinfo.pop(tag)))


def _compute_depth_median(pdict):
    img = pdict.pop("img")
    pdict.pop("mask", None)
    depth = pdict.pop("depth")
    mask = img[..., -1] > 10
    depth_median = float(np.median(depth[mask])) if np.any(mask) else 1.0
    nz = cv2.findNonZero(mask.astype(np.uint8))
    if nz is not None:
        xywh = cv2.boundingRect(nz)
        cx1, cy1 = int(xywh[0]), int(xywh[1])
        cx2, cy2 = cx1 + int(xywh[2]), cy1 + int(xywh[3])
        depth = depth[cy1:cy2, cx1:cx2]
        img = img[cy1:cy2, cx1:cx2]
        if "xyxy" in pdict:
            ox, oy = pdict["xyxy"][0], pdict["xyxy"][1]
            pdict["xyxy"] = [ox + cx1, oy + cy1, ox + cx2, oy + cy2]
        else:
            pdict["xyxy"] = [cx1, cy1, cx2, cy2]
    depth = np.round(np.clip(depth, 0, 1) * 255).astype(np.uint8)
    pdict["depth_median"] = depth_median
    pdict["img"] = img
    pdict["depth"] = depth
    return pdict


def post_process(ld, K, verbose, tblr_split=True, use_lama=False):
    layer_dict, depth_dict = ld.layer_dict, ld.depth_dict
    fullpage, resolution = ld.fullpage, ld.resolution
    verbose("后处理：拆分与聚类…")
    tag2pinfo = {}
    for tag in layer_dict:
        img = layer_dict[tag]
        if tag not in depth_dict:
            continue
        mask = img[..., -1] > 10
        if not np.any(mask):
            continue
        tag2pinfo[tag] = {"img": img, "depth": depth_dict[tag],
                          "xyxy": [0, 0, resolution, resolution], "mask": mask, "tag": tag}

    if "eyes" in tag2pinfo:
        pi = tag2pinfo.pop("eyes")
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
            pi["mask"].astype(np.uint8) * 255, connectivity=8)
        stats_arr = np.array(stats)
        if len(stats_arr[..., -1]) >= 5:
            order = np.argsort(stats_arr[..., -1])[::-1][1:]
            ml, mr, sl, sr = _label_lr_split(labels, stats_arr, order[0], order[1])
            img, depth, xyxy, _ = _process_cuts(pi["img"], pi["depth"], pi["xyxy"], sl)
            tag2pinfo["eyer"] = {"img": img, "xyxy": xyxy, "depth": depth}
            img, depth, xyxy, _ = _process_cuts(pi["img"], pi["depth"], pi["xyxy"], sr)
            tag2pinfo["eyel"] = {"img": img, "xyxy": xyxy, "depth": depth}
            if len(order) >= 4:
                bl, br, sl, sr = _label_lr_split(labels, stats_arr, order[2], order[3])
                img, depth, xyxy, _ = _process_cuts(pi["img"], pi["depth"], pi["xyxy"], sl)
                tag2pinfo["browr"] = {"img": img, "xyxy": xyxy, "depth": depth}
                img, depth, xyxy, _ = _process_cuts(pi["img"], pi["depth"], pi["xyxy"], sr)
                tag2pinfo["browl"] = {"img": img, "xyxy": xyxy, "depth": depth}
        else:
            tag2pinfo["eyes"] = pi

    if tblr_split:
        _tag_lr_split("handwear", tag2pinfo)
        for t in ["eyewhite", "irides", "eyelash", "eyebrow"]:
            _tag_lr_split(t, tag2pinfo)
        _tag_lr_split("ears", tag2pinfo)
        if "hair" in tag2pinfo:
            pi = tag2pinfo.pop("hair")
            try:
                parts = K["cluster_inpaint_part"](inpaint="cv2" if not use_lama else "lama", **pi)
                parts.sort(key=lambda x: x["depth_median"])
                tag2pinfo["hairf"] = parts[0]
                tag2pinfo["hairb"] = parts[1]
            except Exception as e:
                verbose("头发聚类失败(%s)，保持原样" % e)
                tag2pinfo["hair"] = pi

    for rt in ("nose", "mouth"):
        if rt in tag2pinfo:
            pi = tag2pinfo[rt]
            sh, sw = pi["img"].shape[:2]
            fh, fw = fullpage.shape[:2]
            if sh == fh and sw == fw:
                pi["img"][..., :3] = fullpage[..., :3]
            else:
                x1, y1 = pi["xyxy"][0], pi["xyxy"][1]
                crop = fullpage[y1:min(y1 + sh, fh), x1:min(x1 + sw, fw), :3]
                pi["img"][:crop.shape[0], :crop.shape[1], :3] = crop

    for tag in list(tag2pinfo.keys()):
        pi = tag2pinfo[tag]
        if "img" in pi and "depth" in pi:
            _compute_depth_median(pi)
        pi["tag"] = tag

    if "face" in tag2pinfo:
        fdm = tag2pinfo["face"]["depth_median"]
        for t in ["nose", "mouth", "eyes", "eyel", "eyer"]:
            if t in tag2pinfo and tag2pinfo[t]["depth_median"] > fdm:
                tag2pinfo[t]["depth_median"] = fdm - 0.001
        for t in ["earr", "earl", "ears"]:
            if t in tag2pinfo:
                tag2pinfo[t]["depth_median"] = fdm + 0.001

    verbose("后处理完成 %d 层" % len(tag2pinfo))
    return {"tag2pinfo": tag2pinfo, "frame_size": fullpage.shape[:2]}


# ---------------- 落盘（与 decompose.collect 输出兼容） ----------------
def save_parts(parts, out_dir, K, verbose):
    import json
    tag2pinfo = parts["tag2pinfo"]
    ch, cw = parts["frame_size"]
    os.makedirs(out_dir, exist_ok=True)
    order = sorted(tag2pinfo.keys(), key=lambda t: tag2pinfo[t].get("depth_median", 1), reverse=True)
    entries = []
    for tag in order:
        pi = tag2pinfo[tag]
        img = pi.get("img")
        if img is None:
            continue
        x1, y1, x2, y2 = [int(v) for v in pi.get("xyxy", [0, 0, img.shape[1], img.shape[0]])]
        fn = "layer_%s.png" % tag.replace(" ", "_")
        Image.fromarray(img).save(os.path.join(out_dir, fn))
        e = {"name": tag, "filename": fn, "left": x1, "top": y1, "right": x2, "bottom": y2,
             "depth_median": float(pi.get("depth_median", 1))}
        dep = pi.get("depth")
        if dep is not None:
            dfn = "layer_%s_depth.png" % tag.replace(" ", "_")
            if dep.ndim == 2:
                Image.fromarray(dep, mode="L").save(os.path.join(out_dir, dfn))
            else:
                Image.fromarray(dep).save(os.path.join(out_dir, dfn))
            e["depth_filename"] = dfn
        entries.append(e)
    meta = {"prefix": "st_local", "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "layers": entries, "width": int(cw), "height": int(ch)}
    mp = os.path.join(out_dir, "layers_meta.json")
    json.dump(meta, open(mp, "w", encoding="utf8"), indent=2, ensure_ascii=False)
    # 预览 = 按深度从远到近叠回
    try:
        drawables = [tag2pinfo[t] for t in order if tag2pinfo[t].get("img") is not None]
        if drawables:
            blended = K["img_alpha_blending"](drawables, premultiplied=False,
                                              final_size=(int(cw), int(ch)))
            Image.fromarray(blended[..., :3]).save(os.path.join(out_dir, "seethrough_preview.png"))
    except Exception as e:
        verbose("预览生成失败(不影响结果): %s" % e)
    verbose("已保存 %d 层 -> %s" % (len(entries), out_dir))
    return mp


def decompose(img_path, out_dir, resolution=1024, depth_resolution=768, steps=30,
              seed=42, use_lama=False, verbose=print):
    """完整本地流程。返回 layers_meta.json 路径或 None。"""
    if pick_device() == "cpu":
        verbose("[!] CPU 推理模式：无 CUDA 环境，全流程实测约 2-3 小时/张"
                "（内存需求 ~10GB；N 卡用户可在「资产管理」下载 CUDA 加速包"
                "提速到约 14 分钟）。")
    if not os.path.exists(img_path):
        raise FileNotFoundError(img_path)
    img = np.asarray(Image.open(img_path).convert("RGB"))
    pipeline = marigold = None
    try:
        pipeline, marigold, K = load_models(verbose)
        ld = generate_layers(pipeline, K, img, seed, resolution, steps, verbose)
        ldep = generate_depth(marigold, K, ld, seed, depth_resolution, verbose)
        parts = post_process(ldep, K, verbose, use_lama=use_lama)
        meta = save_parts(parts, out_dir, K, verbose)
        return meta
    except Exception as e:
        import traceback
        verbose("本地拆分异常: %s" % e)
        traceback.print_exc()
        return None
    finally:
        del pipeline, marigold
        _empty_cache()


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ST_ROOT, "test_input.png")
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ST_ROOT, "seethrough_out", "cli_test")
    r = decompose(src, dst)
    print("RESULT:", r)
