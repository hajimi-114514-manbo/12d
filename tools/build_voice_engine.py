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
    """打 4 处分档发行补丁。幂等（已打过就跳过）。"""
    print("== 打引擎补丁")
    # ① onnxruntime 懒加载
    p = os.path.join(gsv, "text", "g2pw", "onnx_api.py")
    s = io.open(p, encoding="utf-8").read()
    if "_get_onnxruntime" not in s:
        s = s.replace("import numpy as np\nimport onnxruntime\nimport requests",
                      "import numpy as np\nimport requests")
        s = s.replace("onnxruntime.set_default_logger_severity(3)",
                      "_LAZY_ONNX = None")
        s = s.replace("""onnxruntime.set_default_logger_severity(3)
try:
    onnxruntime.preload_dlls()
except Exception:
    pass
""", """# onnxruntime 改懒加载（分档发行补丁）：模块级 import 会让
# 「不带 onnxruntime 的档」一 import 就挂；改成用时才 import。
_ONNX = None


def _get_onnxruntime():
    global _ONNX
    if _ONNX is None:
        try:
            import onnxruntime as _ort
        except ImportError as e:
            raise ImportError(
                "G2PW 需要 onnxruntime，当前档位未包含它。"
                "中文发音可用（多音字判读退化）。") from e
        try:
            _ort.set_default_logger_severity(3)
            _ort.preload_dlls()
        except Exception:
            pass
        _ONNX = _ort
    return _ONNX
""")
        s = s.replace("onnxruntime.InferenceSession",
                      "_get_onnxruntime().InferenceSession")
        io.open(p, "w", encoding="utf-8", newline="\n").write(s)
        print("   ① onnxruntime 懒加载")
    else:
        print("   ① 已打过")

    # ② cleaner 语言模块按可用性挑选
    p = os.path.join(gsv, "text", "cleaner.py")
    s = io.open(p, encoding="utf-8").read()
    if "_available_language_modules" not in s:
        helper = '''
def _available_language_modules(mapping):
    """挑出当前档位真的装了的前端语言模块（分档发行补丁）。

    原来 language_module_map 写死并用 __import__ 动态载入，缺某个语言的
    前端依赖就直接崩，把整条合成链路带崩。改成 import 失败就跳过该语言。
    完整安装时结果与原来完全一样。
    """
    import warnings as _warnings
    only = os.environ.get("MUPPET_VOICE_LANGS", "").strip()
    want = set(x.strip() for x in only.split(",") if x.strip()) if only else None
    out = {}
    for lang, mod in list(mapping.items()):
        if want is not None and lang not in want:
            continue
        try:
            __import__("text." + mod, fromlist=[mod])
        except Exception as e:
            _warnings.warn("语言 %s 前端不可用（%s），已跳过"
                          % (lang, type(e).__name__))
            continue
        out[lang] = mod
    return out

'''
        i = s.find("\ndef ")
        s = s[:i] + "\n" + helper + s[i:]
        s = s.replace('''    if language not in language_module_map:
        language = "en"
        text = " "''',
                      '''    language_module_map = _available_language_modules(language_module_map)
    if language not in language_module_map:
        language = "en" if "en" in language_module_map else (
            next(iter(language_module_map), "en"))
        text = " "''')
        io.open(p, "w", encoding="utf-8", newline="\n").write(s)
        print("   ② cleaner 语言模块过滤")
    else:
        print("   ② 已打过")

    # ③ is_g2pw 环境变量
    p = os.path.join(gsv, "text", "chinese2.py")
    s = io.open(p, encoding="utf-8").read()
    if "is_g2pw = True" in s:
        s = s.replace(
            "is_g2pw = True  # True if is_g2pw_str.lower() == 'true' else False",
            "is_g2pw = os.environ.get('is_g2pw', 'False').lower() == 'true'")
        io.open(p, "w", encoding="utf-8", newline="\n").write(s)
        print("   ③ is_g2pw 环境变量")
    else:
        print("   ③ 已打过")

    # ④ BigVGAN 懒加载
    p = os.path.join(gsv, "TTS_infer_pack", "TTS.py")
    s = io.open(p, encoding="utf-8").read()
    if "\nfrom BigVGAN.bigvgan import BigVGAN" in s:
        s = s.replace("from BigVGAN.bigvgan import BigVGAN", """def _import_bigvgan():
    \"\"\"BigVGAN 只在 v3/v4 的 vocoder 路径用（v2ProPlus 不用）。
    改懒加载：模块级 import 会让「不带 BigVGAN 的档」一 import 就挂。\"\"\"
    try:
        from BigVGAN.bigvgan import BigVGAN
        return BigVGAN
    except ImportError as e:
        raise ImportError(
            "该功能需要 BigVGAN（v3/v4 声码器），当前档位未包含它。") from e""")
        s = s.replace("self.vocoder = BigVGAN.from_pretrained(",
                      "self.vocoder = _import_bigvgan().from_pretrained(")
        io.open(p, "w", encoding="utf-8", newline="\n").write(s)
        print("   ④ BigVGAN 懒加载")
    else:
        print("   ④ 已打过")


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