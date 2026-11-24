# -*- coding: utf-8 -*-
"""构建分档语音引擎包（可复现）。

用法
----
    python tools/build_voice_engine.py --tier zh-basic --out D:\\build\\voice_zh
    python tools/build_voice_engine.py --tier zh-full
    python tools/build_voice_engine.py --tier ja
    python tools/build_voice_engine.py --tier zh-ja

设计要点（都是实测踩出来的，别随便改）
----------------------------------------
1. **必须重写 site-packages/users.pth**
   原来它写死了开发机源码路径（D:\\BaiduNetdiskDownload\\GPT-SoVITS\\...）。
   .pth 在 site-packages 初始化时就把这些路径塞进 sys.path，**优先级高于
   api_v2.py 的 sys.path.append(now_dir)** —— 结果引擎永远加载开发机那份，
   我们裁剪/打过的补丁根本不生效（实测：改 chinese2.py 的 is_g2pw 无效）。
   但**不能直接删**：它同时让 `import text` / `from tools.i18n` 找得到。
   做法：改写成相对路径（相对 site-packages），3 级上 = 引擎根。

2. **必须打 4 处引擎补丁**（都在 tools/voice_engine_patch/ 下有备份）
   ① text/g2pw/onnx_api.py：onnxruntime 改懒加载
      —— 模块级 import 会让「不带 onnxruntime 的档」一 import 就挂。
   ② text/cleaner.py：语言模块按可用性挑选
      —— 原来 language_module_map 写死 {"zh","ja","en","ko"} 并动态
         __import__，缺某个语言的前端依赖就直接崩。
   ③ text/chinese2.py：is_g2pw 走环境变量，默认 False
   ④ TTS_infer_pack/TTS.py：BigVGAN 改懒加载
      —— v2ProPlus 不用 BigVGAN，但原来模块级 import。

3. **不能删的包（删了就起不来）**
   - mecab / wordfreq / ipadic：**jieba_fast 的依赖链**。
     chinese2 -> jieba_fast -> wordfreq/mecab.py -> `import mecab`
     而 wordfreq 会为**所有**语言建 analyzer，日文那个也要 ipadic。
     （我一开始按「mecab 是日文分词器」把它跟韩文系一起删了，
      结果中文合成直接挂，报错只说 No module named 'mecab'。）
   - AR/：TTS.py:24 import AR.models.t2s_lightning_module ->
     pytorch_lightning -> torchmetrics。幸而这俩加起来只有 8MB。
   - configs/：tts_infer.yaml 在里面（底模路径/device/version）。
   - utils.py / sv.py / process_ckpt.py：被当**顶层模块** import 的
     （feature_extractor/cnhubert.py:16 `import utils`）。
   - fast_langdetect / split_lang：TextPreprocessor.py:12 无条件
     import LangSegmenter。
   - 底模：s1v3.ckpt + v2Pro/s2Gv2ProPlus.pth + chinese-hubert-base +
     chinese-roberta-wwm-ext-large + sv/。

4. **torch 由用户自己 pip 装**（这是分档的核心）
   - CPU : torch==2.0.0 + torchaudio==2.0.2  （164MB）
   - CUDA: torch==2.0.0+cu118                    （2.5GB）
   **必须按这个顺序装**：
     pip install torch==2.0.0 --index-url https://download.pytorch.org/whl/cpu
     pip install torchaudio==2.0.2      # 只在 PyPI 上有 cp39 win wheel
     pip install torchmetrics==0.11.4    # **必须最后装**！
   原因：pip 装 torch 会把旧版 torchmetrics 当不兼容依赖**卸载掉**
   （实测装完 torch 后 torchmetrics 消失 -> import 链断 ->
    pytorch_lightning.callbacks 报 ModuleNotFoundError）。
   torchaudio 只在 PyPI 上有 cp39 win wheel，pytorch 的 cpu 源里没有 2.0.x。

5. **引擎整目录必须同盘**
   tools/i18n/i18n.py 用 os.path.relpath 计算路径，跨盘（C: 放引擎、
   D: 放代码）直接抛 ValueError。

6. **weight.json 要清空**
   它指向一个具体音色（Roselia_湊友希那），我们不带音色。
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# ---- 引擎源位置（打包机上有裁剪好的引擎）----
DEFAULT_SRC = os.environ.get("MUPPET_VOICE_SRC", r"D:\gsv_slim\work")

# ---- 各档的组成 ----
TIERS = {
    "zh-basic": {
        "name": "语音引擎（中文基础档）",
        "desc": "纯中文 AI 配音。不含 G2PW（多音字判读会退化）、不含日文资源。",
        "keep_g2pw": False,
        "keep_ja": False,
    },
    "zh-full": {
        "name": "语音引擎（中文完整档）",
        "desc": "加 G2PW（onnxruntime + G2PWModel，共 1.06GB），多音字判读准确。",
        "keep_g2pw": True,
        "keep_ja": False,
    },
    "ja": {
        "name": "语音引擎（日文档）",
        "desc": "日文 AI 配音（pyopenjtalk + ipadic + ja_userdic）。不含 G2PW。",
        "keep_g2pw": False,
        "keep_ja": True,
    },
    "zh-ja": {
        "name": "语音引擎（中日双语档）",
        "desc": "中日都做。G2PW + 日文资源都带。",
        "keep_g2pw": True,
        "keep_ja": True,
    },
}

# ---- 必留的第三方包（推理链路硬依赖，删了起不来）----
MUST_KEEP_SITE = [
    # 中文分词链：jieba_fast -> wordfreq -> mecab（+ipadic 给日文 analyzer）
    "jieba_fast", "jieba", "wordfreq", "mecab", "ipadic",
    # 多语切分：TextPreprocessor.py:12 无条件 import LangSegmenter
    "split_lang", "fast_langdetect", "fasttext",
    # 训练框架链：TTS.py:24 -> AR -> pytorch_lightning -> torchmetrics
    "pytorch_lightning", "lightning_utilities", "torchmetrics",
    # 推理必需
    "transformers", "tokenizers", "safetensors", "huggingface_hub",
    "numpy", "scipy", "librosa", "numba", "llvmlite", "soundfile",
    "pydantic", "fastapi", "uvicorn", "starlette", "anyio", "h11",
    "tqdm", "yaml", "packaging", "einops", "matplotlib", "pandas",
    "sentencepiece", "jieba", "cn2an", "pypinyin", "wordsegment",
    "g2p_en", "inflect", "eng_to_ipa", "modelscope", "ctranslate2",
    "av", "ffmpeg", "kaldi", "opencc", "requests", "charset_normalizer",
]

# ---- 可以删的（推理链路不用）----
DROP_SITE = [
    # 训练/数据集相关
    "deepspeech", "datasets1", "datasets", "accelerate", "evaluate",
    "peft", "x_transformers", "megatron_lm", "text_to_speech",
    "text_to_image", "text_to_video", "image_to_image", "image_to_text",
    "speechcommands", "librispeech", "librilitts", "commonvoice",
    "voxceleb1", "vctk", "vctk1", "musdb_hq", "vqa", "libritts",
    "torchao", "hqq", "awq", "gptq", "quanto", "eetq", "aqlm",
    "fbgemm_fp8", "hqq", "autoawq", "optimum",
]


def human(n):
    return "%.2f GB" % (n / 1073741824) if n >= 1073741824 else "%.0f MB" % (n / 1048576)


def dsize(p):
    if os.path.isfile(p):
        return os.path.getsize(p)
    t = 0
    for dp, dn, fn in os.walk(p):
        for f in fn:
            try:
                t += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return t


def patch_engine(gsv):
    """应用仓库里的引擎补丁（tools/voice_engine_patches/）。

    补丁以**文件**形式存在仓库里（不是内联在本脚本中）：
      · 一眼能看出改了哪几行
      · 引擎升级后能看出补丁是否还能 apply
      · 引擎源码本身在仓库外（且被 .gitignore 排除），只能靠补丁复现
    本函数是幂等的（已打过就跳过）。
    """
    print("== 应用引擎补丁")
    here = os.path.dirname(os.path.abspath(__file__))
    apply_script = os.path.join(here, "apply_voice_patches.py")
    if not os.path.isfile(apply_script):
        print("   !! 找不到 apply_voice_patches.py，跳过（引擎可能没补丁）")
        return
    import subprocess
    engine = os.path.abspath(os.path.join(gsv, os.pardir))
    try:
        # 显式指定编码：Windows 默认 GBK，而补丁脚本输出含中文注释 ->
        # 不指定会 UnicodeDecodeError（实测）。
        r = subprocess.run([sys.executable, apply_script, "--engine", engine],
                           capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
        for line in (r.stdout or "").splitlines():
            print("   " + line.strip())
        if r.returncode != 0:
            print("   !! 补丁应用返回 %s：%s"
                  % (r.returncode, (r.stderr or "")[-300:]))
    except Exception as e:
        print("   !! 调用补丁脚本失败：%s" % e)


def rewrite_users_pth(sp):
    """把 users.pth 改成引擎自身路径（关键！否则加载开发机源码）。"""
    p = os.path.join(sp, "users.pth")
    rels = ["../../..", "../../../GPT_SoVITS", "../../../GPT_SoVITS/BigVGAN",
            "../../../tools", "../../../tools/asr", "../../../tools/uvr5"]
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(rels) + "\n")
    print("   users.pth 已重写为引擎自身路径")


def fix_tts_config(gsv):
    """tts_infer.yaml 指向实际带的底模；weight.json 清空。"""
    import re
    p = os.path.join(gsv, "configs", "tts_infer.yaml")
    if not os.path.isfile(p):
        return
    s = io.open(p, encoding="utf-8").read()
    s = s.replace("GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/"
                  "s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt",
                  "GPT_SoVITS/pretrained_models/s1v3.ckpt")
    s = s.replace("GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/"
                  "s2G2333k.pth",
                  "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth")
    s = s.replace("GPT_SoVITS/pretrained_models/s1bert25hz-2kh-longer-"
                  "epoch=68e-step=50232.ckpt",
                  "GPT_SoVITS/pretrained_models/s1v3.ckpt")
    s = s.replace("GPT_SoVITS/pretrained_models/s2G488k.pth",
                  "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth")
    s = re.sub(r"(?m)^(\s*version:\s*)v2\s*$", r"\1v2ProPlus", s)
    io.open(p, "w", encoding="utf-8", newline="\n").write(s)
    print("   tts_infer.yaml 已指向 s1v3.ckpt + v2Pro/s2Gv2ProPlus.pth")


def build(tier, src, out, keep_voices=False):
    cfg = TIERS[tier]
    print("=== 构建 %s（%s）" % (tier, cfg["name"]))
    if os.path.isdir(out):
        shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(src, out, ignore=shutil.ignore_patterns(
        "logs", "output", "TEMP", "_voices_out", "_regress_out",
        "__pycache__", "*.pyc", "_engine_src_backup",
        # _slim_test 是本脚本的实验残留（在源目录里会跟着被复制进来，
        # 实测多带 3.6GB）
        "_slim_test", "_slim*"))
    # 音色：不带（CC-BY-NC 不可二次配布）
    for n in os.listdir(out):
        if n.lower().startswith(("voices", "gpt_weights", "sovits_weights")):
            p = os.path.join(out, n)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
    print("   - 音色（CC-BY-NC 不可二次配布）")

    sp = os.path.join(out, "runtime", "Lib", "site-packages")
    gsv = os.path.join(out, "GPT_SoVITS")

    # torch 系（用户自己 pip 装）
    for n in list(os.listdir(sp)):
        if n.lower().startswith(("torch", "nvidia", "triton")):
            p = os.path.join(sp, n)
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) \
                else os.remove(p)
    print("   - torch 系（用户自己 pip 装）")

    # 韩文系
    for n in ("mecab_ko_dic", "eunjeon", "pymecab", "python_mecab_ko",
              "python_mecab_ko_dic", ".load-order-python_mecab_ko-1.3.7"):
        p = os.path.join(sp, n)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    print("   - 韩文系")

    # G2PW
    if not cfg["keep_g2pw"]:
        rm2(os.path.join(sp, "onnxruntime"))
        rm2(os.path.join(gsv, "text", "G2PWModel"))
        print("   - G2PW 全套（1.06GB）")
    # 日文
    if not cfg["keep_ja"]:
        for n in ("pyopenjtalk",
                  "libmecab-8cdb4f7dd82fbfc9e0d5e8feeea571e4.dll"):
            rm2(os.path.join(sp, n))
        rm2(os.path.join(gsv, "text", "ja_userdic"))
        print("   - 日文资源（192MB）")

    # 训练/webui（闭包外）
    for n in ("download.py", "export_torch_script.py",
              "export_torch_script_v3.py", "export_torch_script_v3v4.py",
              "inference_cli.py", "inference_gui.py", "inference_webui.py",
              "inference_webui_fast.py", "onnx_export.py", "s1_train.py",
              "s2_train.py", "s2_train_v3.py", "s2_train_v3_lora.py",
              "stream_v2pro.py"):
        rm2(os.path.join(gsv, n))
    rm2(os.path.join(gsv, "prepare_datasets"))
    print("   - 训练/导出/webui")

    # 配置与权重指向
    rewrite_users_pth(sp)
    fix_tts_config(gsv)
    wj = os.path.join(out, "weight.json")
    if os.path.isfile(wj):
        io.open(wj, "w", encoding="utf-8").write("{}")

    # 引擎补丁
    patch_engine(gsv)

    total = dsize(out)
    print()
    print("   完成：%s  ->  %s" % (out, human(total)))
    print("   用户还需 pip 装 torch（CPU 164MB / CUDA 2.5GB）")
    return total


def rm2(p):
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.isfile(p):
        try:
            os.remove(p)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", required=True, choices=sorted(TIERS))
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    out = a.out or os.path.join(r"D:\build", "voice_" + a.tier)
    build(a.tier, a.src, out)


if __name__ == "__main__":
    main()