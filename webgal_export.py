# -*- coding: utf-8 -*-
"""把视频工程（video_model.PROJECT_TYPE 结构）导出为标准 WebGAL 4.5.x 游戏工程目录。

纯模块 + CLI，零必选第三方依赖：video_model / video_source / video_export / PIL /
numpy 全部 try 可选，任何一环失败自动降级（布局回默认、立绘退 PIL/灰色占位图），
绝不抛崩导出主流程。

用法（CLI）：
    python webgal_export.py --proj <video_edit JSON 路径> --out <导出目录>
                            [--name <游戏名>] [--dialect webgal|mygo]
    成功 rc=0；失败 rc=1 并打印原因。

API：
    export_game(proj, out_dir, proj_dir=None, game_name=None, dialect="webgal")
        -> (ok, msg, stats)
    stats = {"files": int 拷贝/生成文件数, "lines": int 脚本语句数,
             "warnings": [str, ...]}

WebGAL 格式硬规范（对照官方模板 WebGAL_Template/game/）：
    场景语句行  `命令:参数 -选项;`   注释行 `// ...`
    台词行      `名字:内容;`          空行允许
    config.txt  每行 `Key:Value;`（Game_name/Game_key/Title_img/Title_bgm/
                Default_Language:zh_CN）
    目录骨架    game/{scene,background,figure,bgm,vocal,video,tex,animation,template}
                全部写到 out_dir/game/ 下；入口脚本 scene/start.txt，config 在 game/config.txt

映射规则（v3.4 A1 布局契约）：
    1) 底轨（tracks 最前可见 video 轨）满幅无 alpha image clip -> `changeBg:名 -next;`
    2) 其余 video 轨 clip -> `changeFigure:名 -left/-center/-right;`（lay.x 三分位），
       clip 结束补 `changeFigure:none -槽位;`；|lay.y-0.5|>0.15 语句上方加调整提示注释
    3) audio clip：dur>=3s -> `bgm:名;`（新段自然替换，末尾不补停）；dur<3s -> `playEffect:名;`
    4) video clip -> `video:名 -next;`
    5) puppet clip -> 渲 clip 起始首帧（video_source RGBA 优先，退 PIL，再退 256x512
       灰占位图）存 figure/ 走规则 2；上级目录见 *.model3.json/Cubism导入指引.txt 时
       附 Cubism 指引并插 changeFigure .model3.json 注释
    6) 事件按时间升序；同刻先 bg 后 figure 后 audio（video 排在 figure 后 audio 前）
    7) 文件名安全化：保留中文与字母数字._-，空格转下划线，剔除引号冒号分号竖线问尖括号
       与路径分隔符；重名追加 _2/_3
    8) 空工程 -> 最小可运行骨架（注释 + intro 语句）
    9) dialect="mygo" -> NotImplementedError("MyGO 方言导出器 v3.5 提供")
   10) proj["dialogue"]=[{t,who,text,dur}]（A4 say 命令与舞台字幕共用的台词表）
       -> 时刻 t 输出台词行 `谁:内容;`；who 剔除 :; 与空白，text 中 ; 与换行转空格；
       脏条目跳过并记警告。台词事件同刻排在 audio 之后（prio 4）。
       H2a(task-47)：条目带 motion/expression 时，在该台词行前 emit 一条
       `changeFigure:<该时刻生效立绘引用> -槽位 -motion=X [-expression=Y];`
       切换行（同槽位同值连续台词去重只 emit 一次，prio 3.5；查不到生效
       立绘记警告「台词动作无落点」，台词照写）。
"""
import argparse
import glob
import json
import os
import random
import re
import shutil
import string
import struct
import sys
import zlib

AUDIO_BGM_MIN = 3.0          # 秒：>=3s 视作 bgm，否则音效
Y_DEV = 0.15                 # 立绘纵向偏离 0.5 超过该值 -> 插手动调整提示
DEFAULT_LAYOUT = {"x": 0.5, "y": 0.5, "scale": 1.0, "rot": 0.0}
GAME_SUBDIRS = ("scene", "background", "figure", "bgm", "vocal",
                "video", "tex", "animation", "template")
_EMPTY_SCRIPT = (";空白 WebGAL 工程;\n"
                "intro:你好|欢迎使用木偶工作室导出;\n")


# 台词正文里的「空格+减号」保护符（U+200B 零宽空格）。
# 官方 4.6.6 引擎的台词参数切分（index js 的 i0e）：
#     const b = / -/.exec(g);   // 第一个「空格+减号」= 参数起点
#     GU: e.split(" -")         // 按「空格+减号」切参数段
# 后果：台词正文里若含 "A - B"，引擎会把 "B" 当成**参数名**，
# 正文只剩 "A"，**后半段整段消失**（实测真 bug，用户实拍可见）。
#
# 为什么用零宽字符而不是反斜杠：
#   - 引擎只对 "\\;" 做反转义（g.replaceAll("\\;",";")），
#     "\\-" **不会**被还原 -> 正文会显示成 "A \- B"（带反斜杠，脏）
#   - 引擎对零宽/全角字符**不做任何清洗**（实测 js 里 0 处引用），
#     所以 U+200B 原样进 Text 渲染，但视觉上完全不可见
#   - 引擎的 / -/ 只匹配 ASCII 半角减号 -> 插了零宽就匹配不上，
#     整句安全地当正文显示
# 代价：导出文件里含不可见字符（调试时用 show 参数展开看）。
_ZWSP = "\u200b"


def _escape_say_body(text):
    """台词正文转义：把「空格+减号」写成「空格+零宽+减号」。

    配套：导入端 webgal_import._strip_say_args 按同一规则识别并还原，
    保证工程里存的是**用户原文**，导出/导入往返不丢字、不变形。

    v3.5 补丁（存量脏数据）：旧版本导入器把台词参数（-fontSize=small）
    当成了正文存进工程。这类正文里已经有「空格+减号」了，如果一律加
    零宽保护，引擎就不再把它当参数 -> **整段 "-fontSize=small" 显示
    在正文里**（比原来更糟）。
    所以这里先识别出「合法的参数形态」并**剥掉**（参数本来就该由
    引擎处理，不该出现在正文），剩下的才是真正文，再加保护。
    """
    s = str(text or "")
    if _ZWSP in s:
        return s                      # 已保护过，不重复插
    # 存量脏数据：正文里混着合法参数 -> 先剥离（引擎会自己读参数）
    s = _strip_known_say_args(s)
    return re.sub(r"(?<!\\) -", " " + _ZWSP + "-", s)


def _strip_known_say_args(text):
    """从台词正文里剥掉「合法的参数形态」片段（存量脏数据修复）。

    合法形态 = `-key=value` 或 `-key`（key 为纯标识符）。
    「- B 这样的内容」这种自然语言**不剥**（那是真正文）。
    引擎支持的台词参数（实测）：fontSize / notend / concat / clear /
    vocal / speaker / figureId 等，键名匹配即可。
    """
    s = str(text or "")
    m = re.search(r" -", s)
    if not m:
        return s
    head, argpart = s[:m.start()], s[m.start():]
    keep = []
    for seg in argpart.split(" -"):
        seg = seg.strip()
        if not seg:
            continue
        if re.match(r"^[A-Za-z_]\w*(?:=.*)?$", seg):
            continue                  # 合法参数 -> 丢掉（交给引擎）
        keep.append(seg)
    if not keep:
        return head
    return head + " - " + " - ".join(keep)


def _comment(text):
    """H1d（task-61）注释行合法化——官方 4.6.6 合法独立注释 = `;xxx;`
    （首尾都带分号）。两轮实测教训（2026-11-14 headless 实拍 + 引擎 i0e
    源码分析，index js）：
      1) `//` 前缀（带不带分号）不是官方注释——会被当台词渲染（二创 fork
         习惯）；引擎按 (?<!\\\\); 全局切句，句子首段 trim 为空才判 comment。
      2) 只写 `;xxx`（缺尾分号）更糟：注释句子会一路吞到下一个分号，
         把后面的 changeFigure 等真实语句吞进注释里（人物消失实测）。
    所以必须 `;xxx;`。注释体内 ASCII 分号转全角（防切句错位），ASCII
    冒号也转全角（防我方 import 端往返时误判台词行）。"""
    t = str(text).strip().replace("\n", " ")
    if t.startswith("//"):
        t = t[2:].strip()
    if t.startswith(";"):
        t = t[1:].strip()
    t = t.rstrip(";").replace(";", "；").replace(":", "：")
    return ";%s;" % t

# 文件名保留集：字母数字、中文（含扩展 A）、._- 与下划线；其余剔除（空格另转下划线）。
_OK_CHAR = re.compile(r"[0-9A-Za-z._\-\u4e00-\u9fff\u3400-\u4dbf\u3007]")
# 台词/语句通用校验（测试与自检共用）：命令:参数 -选项; 或 名字:内容;
_STMT = re.compile(r"^[^:;\s]+:.+;$")
_COMMENT = re.compile(r"^//.*$")


# ================= 小工具 =================
def _f(x, d=0.0):
    try:
        v = float(x)
        if v != v or v in (float("inf"), float("-inf")):
            return d
        return v
    except (TypeError, ValueError):
        return d


def _layout(c, t=None):
    """A1 数据读取硬契约：经 video_model.layout_at 取合并默认后布局；
    模块缺失/抛异常一律回退中心默认。"""
    try:
        import video_model as VM
        lay = VM.layout_at(c, t)
        if not isinstance(lay, dict):
            raise TypeError("layout_at 返回非 dict")
    except Exception:
        lay = DEFAULT_LAYOUT
    return lay


def _slot(x):
    """lay.x 三分位 -> WebGAL 立绘槽位。"""
    x = _f(x, 0.5)
    if x < 1.0 / 3.0:
        return "left"
    if x > 2.0 / 3.0:
        return "right"
    return "center"


# K1（task-69）平移：x 恰在三分位快捷值（0.2/0.5/0.8 ± 0.02）→ 维持旧
# -left/-center/-right 形态（≤3 人导出逐字节=现状锚）；非三分位 → 角色
# 稳定 -id 句柄 + -transform 中心原点绝对像素定位（step0 取证：官方
# demo_parallel_animation 语法；none -id 精确退场已实拍验证）。
# 已知限制：本批只平移 position；scale/rot 不进 -transform（WebGAL 立绘
# 缩放由引擎内置管理，v3.5 后续卡再评估）。
_SLOT_XS = (0.2, 0.5, 0.8)
_SLOT_EPS = 0.02


def _is_slot_x(x):
    return any(abs(_f(x, 0.5) - sx) <= _SLOT_EPS for sx in _SLOT_XS)


def _who_export(path):
    """角色粒度（与导入端 webgal_import._who_of 同口径；惰性 import 防循环）。"""
    try:
        import webgal_import as _WI
        return _WI._who_of(path)
    except Exception:
        return os.path.splitext(os.path.basename(
            str(path).replace("\\", "/").rstrip("/")))[0] or "fig"


def _fig_id(who):
    return "muppet_" + (safe_name(who) or "fig")


def _transform_of(lay, W, H):
    """非三分位 layout -> -transform 紧凑 JSON（position 中心原点像素）。

    附带 `muppet:{w,h}` 画布维度侧键：官方解析器只读 position/scale 等已知
    键（未知键无害），回环时导入端据此精确还原归一化 x/y，不受
    import_script 默认画布尺寸影响。"""
    x = _f(lay.get("x"), 0.5)
    y = _f(lay.get("y"), 0.5)
    px = int(round((x - 0.5) * W))
    py = int(round((0.5 - y) * H))
    return ('{"position":{"x":%d,"y":%d},"muppet":{"w":%d,"h":%d}}'
            % (px, py, int(round(W)), int(round(H))))


def safe_name(name):
    """规则 7：空格→下划线；只保留中文/字母数字/._-（即剔除 `":;|?*<>` 与 / \\ 等）。"""
    s = str(name or "").replace(" ", "_")
    return "".join(ch for ch in s if _OK_CHAR.match(ch))


class _NamePool:
    """按目标目录分池防重名：冲突追加 _2/_3（扩展名前）。"""

    def __init__(self):
        self._used = {}

    def alloc(self, cat, filename):
        used = self._used.setdefault(cat, set())
        base, ext = os.path.splitext(filename)
        if not base:
            base = "asset"
        name = filename
        i = 2
        while name.lower() in used:
            name = "%s_%d%s" % (base, i, ext)
            i += 1
        used.add(name.lower())
        return name


def _abs_path(p, proj_dir):
    p = str(p or "")
    # v3.5.13：`color:black` 这类**伪路径**（引擎内置纯色背景）不是文件，
    # 不能拼 proj_dir —— 拼了会变成 D:\...\color:black，于是 "文件缺失"
    # 警告 + 背景被跳过（实测导出的包开场没背景）。原样返回，交给调用方
    # 按前缀识别。
    if p.startswith("color:"):
        return p
    if not p or os.path.isabs(p):
        return p
    return os.path.join(proj_dir, p) if proj_dir else p


def _src_filename(path):
    """源文件 basename（无扩展则按 kind 补默认扩展，保证脚本引用可读）。"""
    b = os.path.basename(str(path or "").replace("\\", "/").rstrip("/\\"))
    return b or "asset"


# ================= PNG 输出（可选依赖全降级）=================
def _write_png_flat(path, w, h, rgba):
    """纯标准库写单色 RGBA PNG（zlib+struct），PIL/numpy 都不在时的最后兜底。"""
    row = b"\x00" + bytes(rgba) * w
    data = zlib.compress(row * h, 9)

    def chunk(tag, payload):
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", data)
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def _save_arr_png(img, out_png):
    """ndarray/ PIL.Image -> RGBA PNG。失败抛异常由上层降级。"""
    from PIL import Image
    if not isinstance(img, Image.Image):
        import numpy as np
        a = np.asarray(img)
        if a.ndim == 2:
            a = np.dstack([a, a, a])
        if a.shape[2] == 3:
            a = np.dstack([a, np.full(a.shape[:2], 255, np.uint8)])
        elif a.shape[2] > 4:
            a = a[:, :, :4]
        img = Image.fromarray(a.astype("uint8"), "RGBA")
    img.convert("RGBA").save(out_png, "PNG")


def _placeholder_png(out_png):
    """256x512 灰色占位立绘。"""
    try:
        from PIL import Image
        Image.new("RGBA", (256, 512), (128, 128, 128, 255)).save(out_png, "PNG")
    except Exception:
        _write_png_flat(out_png, 256, 512, (128, 128, 128, 255))


def _emit_puppet_png(clip, src_path, out_png):
    """规则 5 三级降级：video_source 渲帧 -> PIL 打开原图 -> 灰占位图。
    返回 'rendered'/'pil'/'placeholder'。"""
    try:
        import video_source as VS
        s = VS.open_source(dict(clip.get("source") or {}))
        try:
            img = s.frame(max(0.0, _f(clip.get("src_in"))))   # clip 起始=首帧
        finally:
            try:
                s.close()
            except Exception:
                pass
        _save_arr_png(img, out_png)
        return "rendered"
    except Exception:
        pass
    try:
        from PIL import Image
        with Image.open(src_path) as im:
            im.convert("RGBA").save(out_png, "PNG")
        return "pil"
    except Exception:
        pass
    _placeholder_png(out_png)
    return "placeholder"


# ================= 底轨背景判定 =================
def _has_alpha(path):
    """图片含有效 alpha（存在透明/半透明像素）判 True；PIL 不可用或读失败按无 alpha。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            if "A" not in im.getbands() and "transparency" not in im.info:
                return False
            ex = im.getextrema()
            ai = im.getbands().index("A") if "A" in im.getbands() else None
            if ai is not None and isinstance(ex, tuple) and len(ex) > ai:
                lo, hi = ex[ai]
                return lo < 255          # 全不透明的 RGBA 视作无 alpha
            return True                  # 仅 transparency 标记
    except Exception:
        return False


def _is_fullscreen(lay, src, W, H):
    """满幅：布局居中且 scale 接近 1，fit 后覆盖 >=97% 画布；尺寸未知回信默认布局。"""
    x, y, sc = _f(lay.get("x"), 0.5), _f(lay.get("y"), 0.5), _f(lay.get("scale"), 1.0)
    if abs(x - 0.5) > Y_DEV or abs(y - 0.5) > Y_DEV or sc < 0.95:
        return False
    w, h = _f(src.get("w")), _f(src.get("h"))
    if w <= 0 or h <= 0 or W <= 0 or H <= 0:
        return True
    s0 = min(W / w, H / h)
    return (w * s0 * sc) >= W * 0.97 and (h * s0 * sc) >= H * 0.97


# ================= Cubism 检测（规则 5 附注）=================
def _cubism_hint_dir(src_path):
    """source.path 的上级目录存在 *.model3.json 或 Cubism导入指引.txt -> 返回该目录。"""
    try:
        p = os.path.abspath(str(src_path or ""))
        parent = os.path.dirname(p) if not os.path.isdir(p) else p
        # 目录型 puppet：模型文件通常就在工程目录内，本级与上级都查
        for d in (parent, os.path.dirname(parent)):
            if not d or not os.path.isdir(d):
                continue
            try:
                entries = os.listdir(d)
            except OSError:
                continue
            names = [str(e) for e in entries]
            if any(n.endswith(".model3.json") for n in names) or \
               "Cubism导入指引.txt" in names:
                return d
    except Exception:
        pass
    return None


_CUBISM_GUIDE = """\
Cubism 模型接入 WebGAL 指引（木偶工作室导出）
=================================================
1. 本次导出仅包含 puppet  clip 的首帧静态立绘（figure/ 下 PNG）。
2. 若源目录含 Live2D Cubism 工程：用 Cubism Editor 将 .cmo3 另存/构建出 .moc3，
   并把 .moc3、物理、表情、动作 json 与贴图一并放入 figure/<立绘名>/ 子目录，
   形成 <名>.model3.json 入口文件。
3. 场景脚本里把 changeFigure:<名>.png 换成：
   changeFigure:<名>/<名>.model3.json -motion=idle -expression=表情名;
4. 建议同步在 WebGAL 编辑器里预览动效与槽位缩放。
"""


# ================= Cubism2 模型目录（H1 task-43）=================
# 整合包立绘全是 Cubism2 模型：figure/<角色>/<服装>/model.json +
# .chara/model.moc + ../.mtn_exp 下 .mtn/.exp.json。WebGAL 官方运行时
# 原生支持 Cubism2（lib\live2d.min.js），导出方向直接可用——**不需要
# .moc3 手工步骤**，也因此不写 Cubism导入指引.txt（与 Cubism4 分支互斥）。

_C2_MTN_EXP = ".mtn_exp"


def _is_cubism2_dir(path):
    """path 是目录且其中含 model.json（明文 Cubism2 入口）-> True。"""
    try:
        return os.path.isdir(path) and os.path.isfile(
            os.path.join(path, "model.json"))
    except Exception:
        return False


def _cubism2_role_root(model_dir):
    """向上找含 .mtn_exp 的角色根目录。

    整合包结构：figure/anon/school_summer-2023/（model.json 在此），
    motions 键引用 ../.mtn_exp/xxx.mtn —— 即动作在 path 父级的父级
    （角色根 anon/）下。角色根判定：path 父级的父级含 .mtn_exp 目录；
    找不到则返回 model_dir 本身（退化：无 .mtn_exp 的工程也能导）。"""
    try:
        d = os.path.abspath(model_dir)
        parent = os.path.dirname(d)
        grand = os.path.dirname(parent)
        if grand and os.path.isdir(os.path.join(grand, _C2_MTN_EXP)):
            return grand
        if parent and os.path.isdir(os.path.join(parent, _C2_MTN_EXP)):
            return parent
    except Exception:
        pass
    return os.path.abspath(model_dir)


def _parse_cubism2_json(model_json_path):
    """解析 model.json -> {"motions": [键名...], "expressions": [键名...]}。

    两种真实形态（H1b task-49）：
    1) 标准 Cubism2：FileReferences.Motions 是 {键名: [{file}...]} 字典，
       Expressions 是 {键名: {...}} —— 命中且非空则**优先**采用；
    2) WebGAL 整合包顶层扁平格式（无 FileReferences 或解析为空时回退）：
       顶层 motions 是 {键名: [{file}...]}，顶层 expressions 是
       [{"name": 键名, ...}] 或 {键名: {...}}（与 build_asset_library
       .meta_model 同源读法，asset_index 的 meta.motions 即出自这里）。
    任何异常返回空表（不炸导出）。"""
    out = {"motions": [], "expressions": []}
    try:
        with open(model_json_path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            return out
        fr = obj.get("FileReferences") or {}
        mo = fr.get("Motions")
        if isinstance(mo, dict):
            out["motions"] = [str(k) for k in mo.keys()]
        ex = fr.get("Expressions")
        if isinstance(ex, dict):
            out["expressions"] = [str(k) for k in ex.keys()]
        # ---- 扁平格式回退（标准形态缺席/为空才读）----
        if not out["motions"]:
            mo2 = obj.get("motions")
            if isinstance(mo2, dict):
                out["motions"] = [str(k) for k in mo2.keys()]
            elif isinstance(mo2, list):      # [{name,file}] 变体
                out["motions"] = [str(m["name"]) for m in mo2
                                  if isinstance(m, dict) and m.get("name")]
        if not out["expressions"]:
            ex2 = obj.get("expressions")
            if isinstance(ex2, list):
                out["expressions"] = [
                    (str(e["name"]) if isinstance(e, dict) and e.get("name")
                     else str(e)) for e in ex2
                    if (isinstance(e, dict) and e.get("name"))
                    or isinstance(e, str)]
            elif isinstance(ex2, dict):
                out["expressions"] = [str(k) for k in ex2.keys()]
    except Exception:
        pass
    return out


def _match_motion_name(want, names):
    """clip 的 motion/expression 值 -> 键名表真实键名。

    三级匹配：精确 > 键名末段前缀（"idle" 命中 "anon/idle01"——键名末段
    以 want 开头，大小写不敏感）> 模糊（difflib get_close_matches）。

    **角色前缀归一化**（v3.5.12 修）：键名表里的键是 `<角色>/<表情>`
    形式（素材统一命名，11 个角色共用一套池，每个角色目录里都有一份完整
    副本），而工程里存的可能是**别的**角色前缀（台词级动作按角色并入
    每个模型的引用集，见 _collect_puppet_motion_refs）。例如工程里写
    `soyo/default`，而某个模型的键名表里该表情叫 `anon/default`——
    按原逻辑精确匹配失败、末段前缀也不匹配（"soyo/default" 不是
    "anon/default" 的前缀），最后落到 fallback，**键表被清空**，
    引擎于是请求一堆根本不存在的文件（404 -> 人物动作卡顿）。

    这里补一条：**去掉角色前缀后按末段名再匹配一次**（"soyo/default"
    -> "default" 命中 "anon/default"）。未命中返回 None。"""
    want = str(want or "").strip()
    if not want or not names:
        return None
    lw = want.lower()
    for n in names:                       # 1) 精确
        if str(n) == want:
            return n
    for n in names:                       # 2) 末段前缀（idle -> anon/idle01）
        seg = str(n).split("/")[-1].lower()
        if seg.startswith(lw):
            return n
    # 2b) 角色前缀归一化：工程里的名字与键名表角色前缀不同时按末段比
    wseg = lw.split("/")[-1]
    if "/" in lw:
        for n in names:                   # 2b-1) 末段精确
            if str(n).split("/")[-1].lower() == wseg:
                return n
        for n in names:                   # 2b-2) 末段前缀
            if str(n).split("/")[-1].lower().startswith(wseg):
                return n
    try:                                  # 3) 模糊
        import difflib
        m = difflib.get_close_matches(lw, [str(n).split("/")[-1].lower()
                                           for n in names],
                                      n=1, cutoff=0.6)
        if m:
            seg2orig = {str(n).split("/")[-1].lower(): n for n in names}
            return seg2orig[m[0]]
    except Exception:
        pass
    return None


def _collect_puppet_motion_refs(proj):
    """预扫工程：收集 Cubism2 puppet clip 实际引用的 motion/expression。

    返回 {model_dir 绝对规范路径: {"motions": set(原始值), "expressions": set}}。
    v3.4.9 性能修复的输入：引擎加载模型会**全量预载 model.json 里定义的
    全部动作/表情文件**（664 动作池 = 每模型 ~900 个 HTTP 请求，实测
    timing_probe），场景切换重载时再来一遍 —— 导出时按剧本引用瘦身
    model.json + 按需拷池文件，把请求量从 ~900/模型 降到个位数。
    """
    refs = {}
    if not isinstance(proj, dict):
        return refs
    for tr in proj.get("tracks") or []:
        if not isinstance(tr, dict):
            continue
        for c in tr.get("clips") or []:
            if not isinstance(c, dict):
                continue
            src = c.get("source") or {}
            if src.get("kind") != "puppet":
                continue
            path = str(src.get("path") or "")
            if not path or not _is_cubism2_dir(path):
                continue
            key = os.path.normpath(os.path.abspath(path))
            slot = refs.setdefault(key, {"motions": set(),
                                         "expressions": set()})
            if c.get("motion"):
                slot["motions"].add(str(c["motion"]))
            if c.get("expression"):
                slot["expressions"].add(str(c["expression"]))
    # 台词级动作（规则 10：dialogue 项自带 motion/expression，导出时按
    # fig_intervals 落到对应立绘）——who->模型映射是动态的，保守做法：
    # 把全部台词动作并入**每个** Cubism2 模型的引用集（通常几十条，
    # 仍远小于全池 897；瘦身收益不受实质影响）。
    dlg_m, dlg_e = set(), set()
    for d in proj.get("dialogue") or []:
        if isinstance(d, dict):
            if d.get("motion"):
                dlg_m.add(str(d["motion"]))
            if d.get("expression"):
                dlg_e.add(str(d["expression"]))
    if dlg_m or dlg_e:
        for slot in refs.values():
            slot["motions"] |= dlg_m
            slot["expressions"] |= dlg_e
    return refs


def _slim_cubism2_json(obj, used, warn, tag):
    """按 used（原始动作/表情值集合）瘦身 Cubism2 model.json 定义。

    - motions：标准形态 FileReferences.Motions 或顶层扁平 motions，
      键名表经 _match_motion_name 三级匹配选中真实键；保底 idle01
      （末段前缀匹配）防止剧本漏写 -motion 时引擎无动作可播。
    - expressions：dict / list-of-{name} 两种形态同法；保底 default。
    - 返回 True=有瘦身（写入新 json）；False=结构不认识，保持原样。
    选中键的**文件拷贝**由 _copy_cubism2_model 按重写后的定义执行。"""
    try:
        fr = obj.get("FileReferences") if isinstance(obj, dict) else None
        mo = (fr or {}).get("Motions") if fr else obj.get("motions")
        mo_is_fr = bool(fr and isinstance(fr.get("Motions"), dict))
        if not isinstance(mo, dict) or not mo:
            return False
        keys = [str(k) for k in mo.keys()]

        def _pick(raws, fallback):
            sel = set()
            for raw in raws or ():
                k = _match_motion_name(raw, keys)
                if k:
                    sel.add(k)
            if not sel and fallback:          # 保底：idle01 / default
                k = _match_motion_name(fallback, keys)
                if k:
                    sel.add(k)
            return sel

        sel_m = _pick(used.get("motions"), "idle01")
        new_mo = {k: mo[k] for k in mo.keys() if k in sel_m}
        if not new_mo:                        # 一个都没选中：保底首个键
            first = next(iter(mo.keys()))
            new_mo = {first: mo[first]}
        if mo_is_fr:
            fr["Motions"] = new_mo
        else:
            obj["motions"] = new_mo

        # expressions：dict 或 list-of-{name} 两种形态
        ex = (fr or {}).get("Expressions") if fr else obj.get("expressions")
        ex_is_fr = bool(fr and "Expressions" in fr)
        if isinstance(ex, dict) and ex:
            ekeys = [str(k) for k in ex.keys()]
            sel_e = _pick(used.get("expressions"), "default")
            new_ex = {k: ex[k] for k in ex.keys() if k in sel_e}
            if ex_is_fr:
                fr["Expressions"] = new_ex
            else:
                obj["expressions"] = new_ex
        elif isinstance(ex, list) and ex:
            names = [str(e.get("name")) for e in ex
                     if isinstance(e, dict) and e.get("name")]
            sel_e = _pick(used.get("expressions"), "default")
            new_ex = [e for e in ex if isinstance(e, dict)
                      and str(e.get("name")) in sel_e]
            if ex_is_fr:
                fr["Expressions"] = new_ex
            else:
                obj["expressions"] = new_ex
        return True
    except Exception as e:
        warn.append("Cubism2 model.json 瘦身失败 %s: %r（保持原样）" % (tag, e))
        return False


def _add_default_expression(obj, role_root, warn=None):
    """把「默认表情」补进 model.json 的 expressions 键表。

    背景（实测）：剧本里 `-expression=<role>/default` 是最常见的写法，但
    `default` **不是**任何 Cubism2 模型 model.json 的真实键名（瘦身后
    更不会有）。而素材库里它**确实存在**：
        <role>/.mtn_exp/expressions/__base__/<role>/default.exp.json
    导出时若不把它写进键表、也不按需拷贝，引擎运行时请求这个文件必然
    404（`ExpressionManager: Failed to load expression`）——
    **用户看到的现象就是「场景切换时人物动作卡顿」**。

    返回 True=已补进键表。"""
    try:
        role_dir = os.path.basename(os.path.abspath(role_root))
        rel = os.path.join(".mtn_exp", "expressions", "__base__",
                            role_dir, "default.exp.json")
        src_f = os.path.normpath(os.path.join(role_root, rel))
        if not os.path.isfile(src_f):
            return False
        if not os.path.abspath(src_f).startswith(
                os.path.abspath(role_root) + os.sep):
            return False
        file_def = "../.mtn_exp/expressions/__base__/%s/default.exp.json" \
            % role_dir
        name = "%s/default" % role_dir
        fr = obj.get("FileReferences") if isinstance(obj, dict) else None
        ex = (fr or {}).get("Expressions") if fr else obj.get("expressions")
        if isinstance(ex, list):
            if any(isinstance(e, dict)
                   and str(e.get("name") or "").endswith("/default")
                   for e in ex):
                return False
            ex.append({"name": name, "file": file_def})
            return True
        if isinstance(ex, dict):
            if any(str(k).endswith("/default") for k in ex):
                return False
            ex[name] = {"File": file_def}
            return True
        # 键表缺失：补一个 list 形态（与真实素材一致）
        if fr is not None:
            fr["Expressions"] = [{"name": name, "file": file_def}]
        else:
            obj["expressions"] = [{"name": name, "file": file_def}]
        return True
    except Exception as e:
        if warn is not None:
            warn.append("补 default 表情失败: %r" % (e,))
        return False


def _copy_cubism2_model(model_dir, figure_root, stats, warn, used=None,
                        used_costumes=None, used_by_costume=None):
    """把 Cubism2 角色根拷进 figure/，返回目标子目录名（相对 figure/）。

    v3.4.9 两种模式：
    - used=None（默认，向后兼容）：整棵 copytree（含 .mtn_exp 全量池），
      旧行为逐字节不变——Studio 预览/未预扫路径用。
    - used=引用集合（export_game 预扫传入）：**瘦身导出**——
      只拷服装目录（model.json + .chara）+ 按引用逐文件拷 .mtn_exp，
      model.json 的 motions/expressions 重写为选中子集。
      引擎加载模型会全量预载定义里全部动作/表情文件（每模型 ~900 请求），
      瘦身后每模型降到个位数请求，人物加载/场景切换不再卡。

    返回形如 "anon/school_summer-2023"（start.txt 里
    changeFigure:anon/school_summer-2023/model.json 引用）"""
    role_root = _cubism2_role_root(model_dir)
    rel = os.path.relpath(model_dir, role_root).replace("\\", "/")
    rel_root = os.path.relpath(role_root,
                               os.path.dirname(role_root)).replace("\\", "/")
    dst_root = os.path.join(figure_root, safe_name(rel_root) or "model")
    try:
        if used is None:
            shutil.copytree(role_root, dst_root, dirs_exist_ok=True)
        else:
            # ---- 瘦身导出 ----
            # 1) 角色根全树拷，**排除 .mtn_exp 动作池**（池按需拷）——
            #    .chara / 多服装目录等共享文件保持原结构（.chara 可能在
            #    角色根，也可能在服装目录内，两种真实形态都覆盖）
            #
            #    **只拷「用到的服装目录」**（v3.5.12 修）：早先把角色根
            #    整棵拷过去，于是 soyo 有 14 个服装目录、sakiko 有 5 个，
            #    但池文件只按**用到的模型**按需拷（11-12 个）——没用到的
            #    服装目录里那份**原始的 232 表情 / 664 动作**的 model.json
            #    照样在包里，引擎一旦扫到就会请求几千个**并不存在**的池
            #    文件（404 -> 人物动作卡顿），白白多占几十 MB。
            #    used_costumes=None（未预扫）时保持整棵拷（旧行为）。
            def _ignore_unused(src, names):
                """跳过**用不到的服装目录**。

                shutil.copytree 的 ignore 回调语义（实测）：`src` 是当前
                要处理的**目录**，`names` 是它下面的条目名；返回的名字集
                会被跳过。所以要跳过「角色根下的未用服装目录」，必须在
                **角色根这一层**的回调里返回那些目录名 —— 判断依据是
                「当前 src 就是角色根」，而不是「src 的父目录是角色根」
                （后者是错的：角色根的父目录是素材库 figure/，永远匹配不上
                真正的角色根）。
                """
                if used_costumes is None:
                    return set()
                src_r = os.path.normcase(os.path.normpath(
                    src.rstrip("\\/")))
                root_r = os.path.normcase(os.path.normpath(role_root))
                if src_r != root_r:
                    return set()
                skip = set()
                for n in names:
                    if n in (".", "") or n.startswith("."):
                        continue
                    # 角色根下的条目：服装目录（子目录）才需要过滤；
                    # 根级文件（model.json/.chara 等共享文件）都保留
                    if os.path.isdir(os.path.join(src, n)) \
                            and n not in used_costumes:
                        skip.add(n)
                return skip

            def _ignore_all(src, names):
                # .mtn_exp 动作池按需拷（不走全树）+ 未使用的服装目录跳过
                r = set(shutil.ignore_patterns(_C2_MTN_EXP)(src, names))
                r |= _ignore_unused(src, names)
                return r

            shutil.copytree(role_root, dst_root,
                            ignore=_ignore_all,
                            dirs_exist_ok=True)
            dst_costume = os.path.join(dst_root, *(
                safe_name(p) for p in rel.split("/") if p not in (".", ""))) \
                if rel not in (".", "") else dst_root
            # 2) 瘦身 model.json（读源 -> 改写 -> 写目标）
            #
            #    **本角色「用到的」每个服装目录都要瘦身**（v3.5.12 修）：
            #    早先只瘦身当前处理的那个 model_dir，同角色下**其它**用到
            #    的服装目录仍是原始的 232 表情 / 664 动作，引擎一扫就请求
            #    几百个不存在的池文件（404 -> 人物动作卡顿）。
            #    used_by_costume: {服装目录名: {"motions":set,"expressions":set}}
            def _slim_one(src_dir, dst_dir, used_refs, tag):
                src_json = os.path.join(src_dir, "model.json")
                try:
                    o = json.load(open(src_json, "r", encoding="utf-8"))
                except Exception as e:
                    warn.append("Cubism2 model.json 读取失败 %s: %r" %
                                (src_json, e))
                    return None
                if not isinstance(o, dict):
                    return None
                if _slim_cubism2_json(o, used_refs or {}, warn, tag):
                    # 保底 default 表情必须在**写盘前**补进键表：它不是
                    # 任何模型 model.json 的真实键名（瘦身更不会有），但
                    # 剧本里 `-expression=<role>/default` 是常见写法，而
                    # 素材库里它**确实存在**：
                    #   <role>/.mtn_exp/expressions/__base__/<role>/default.exp.json
                    # 不补 -> 引擎运行时 404（ExpressionManager: Failed to
                    # load expression）-> **人物动作卡顿**。
                    _add_default_expression(o, role_root, warn)
                    try:
                        with open(os.path.join(dst_dir, "model.json"),
                                  "w", encoding="utf-8") as f:
                            json.dump(o, f, ensure_ascii=False, indent=2)
                        stats["slimmed_models"] = stats.get(
                            "slimmed_models", 0) + 1
                    except OSError as e:
                        warn.append("Cubism2 model.json 写入失败 %s: %r"
                                    % (dst_dir, e))
                return o

            obj = _slim_one(model_dir, dst_costume, used or {},
                            os.path.relpath(os.path.join(model_dir,
                                                        "model.json"),
                                            role_root))
            # 本角色其它用到的服装目录：同样瘦身
            if used_by_costume:
                for _cd in sorted(used_by_costume):
                    if _cd == os.path.basename(os.path.abspath(model_dir)):
                        continue        # 上面已处理
                    _sd = os.path.join(role_root, _cd)
                    _dd = os.path.join(dst_root, safe_name(_cd))
                    if os.path.isdir(_sd) and os.path.isdir(_dd):
                        _slim_one(_sd, _dd, used_by_costume[_cd],
                                  os.path.relpath(os.path.join(
                                      _sd, "model.json"), role_root))
            # 3) 池文件按需拷：遍历**瘦身后的** motions/expressions 定义
            #    （此时 obj 已是子集；未瘦身则保持整树兜底拷）
            if isinstance(obj, dict):
                fr = obj.get("FileReferences") or {}
                def _iter_defs():
                    mo_d = fr.get("Motions") or obj.get("motions") or {}
                    if isinstance(mo_d, dict):
                        for arr in mo_d.values():
                            for it in (arr or []):
                                yield it.get("file")
                    ex_d = fr.get("Expressions") or obj.get(
                        "expressions") or {}
                    if isinstance(ex_d, dict):
                        for it in ex_d.values():
                            yield it.get("file")
                    elif isinstance(ex_d, list):
                        for it in ex_d:
                            if isinstance(it, dict):
                                yield it.get("file")
                defs = [f_ for f_ in _iter_defs() if f_]
                n_pool = 0
                for f_ in defs:
                    # file 相对**服装目录**（如 ../.mtn_exp/motions/x.mtn）
                    src_f = os.path.normpath(os.path.join(model_dir, f_))
                    dst_f = os.path.normpath(os.path.join(dst_costume, f_))
                    # 防路径逃逸：源必须在角色根内，目标必须在 dst_root 内
                    try:
                        if not os.path.abspath(src_f).startswith(
                                os.path.abspath(role_root) + os.sep):
                            continue
                        if not os.path.abspath(dst_f).startswith(
                                os.path.abspath(dst_root) + os.sep):
                            continue
                        if os.path.isfile(src_f):
                            os.makedirs(os.path.dirname(dst_f),
                                        exist_ok=True)
                            shutil.copy2(src_f, dst_f)
                            n_pool += 1
                    except OSError as e:
                        warn.append("池文件拷贝失败 %s: %r" % (f_, e))
                stats["pool_files"] = stats.get("pool_files", 0) + n_pool
        stats["files"] += 1
    except Exception as e:
        warn.append("Cubism2 模型目录拷贝失败 %s: %r" % (role_root, e))
        return None
    # start.txt 引用路径 = <角色名>/<model_dir 相对角色根>/model.json
    parts = [safe_name(rel_root)]
    if rel not in (".", ""):
        parts.extend(safe_name(p) for p in rel.split("/") if p)
    return "/".join(parts)


# ================= I2 MyGO 风字幕皮肤（task-58） =================
# DOM 实测结论（2026-11-14，headless Chrome 驱动 runtime 4.6.6 真实导出包，
# 推进到台词屏 dump；探测件 D:\scripts\temp\i2_probe\dom3.html）：
#   - 台词框容器 [class*="TextBox_Container"]；底框是独立子层
#     [class*="TextBox_Background"]（inline opacity:0.75 —— 样式表 !important
#     可压 inline，去底框成立）
#   - 台词文本容器 [class*="_text_"]（inline font-size:205%）；每字符 span
#     三层：_zhanwei_（占位实字）+ _outer_（引擎自带描边位）+ _inner_（本体），
#     对容器统一 color/text-shadow 即可全层生效
#   - 名牌 [class*="TextBox_showName"]（另有 _ShowName_Background 底层层），
#     字级 _outerName_/_innerName_
#   - 4.6.6 **没有** .textbox/.textboxFont 旧全局类；全部 CSS module 类带
#     hash 后缀（如 _TextBox_main_p1zxt_18），一律 [class*="Xxx"] 属性选择器
#     抗 hash
#   - 引擎点击推进层 #FullScreenClick z-index:12 → 水印 z 取极大且
#     pointer-events:none（不挡点击）
SKIN_STYLE_MARK = "muppet-skin-v1"
NOTE_MARK = "muppet-note"      # K1（task-69）：导出器自产提示注释统一标记，
                               # 导入端按此过滤——防回环把提示当用户注释吸回
                               # 再导出导致重复/乱序（回环逐字节锚的前置）


def _note(text):
    """导出器生成的提示注释（y-dev/可用动作/Cubism2 说明等）：合法 ;…;
    形态 + 统一 NOTE_MARK，导入端 _comment_text 会跳过带标记行。"""
    return _comment("%s [%s]" % (text, NOTE_MARK))

SKIN_CSS = ("/* 木偶工作室 MyGO 风字幕皮肤（" + SKIN_STYLE_MARK + "） */\n"
            + """/* 1) 去底框：台词框/名牌背景层全透明，去毛玻璃与投影 */""")
SKIN_CSS += """
[class*="TextBox_Background"], [class*="ShowName_Background"] {
  background: transparent !important;
  opacity: 1 !important;
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
  box-shadow: none !important;
}
/* 2) 台词：白色 + 八方向 2px 黑描边 + 加粗放宽（压过 zhanwei/outer/inner 全层） */
[class*="TextBox_Container"] [class*="_text_"],
[class*="TextBox_Container"] [class*="_text_"] * {
  color: #ffffff !important;
  text-shadow: -2px -2px 0 #000, 2px -2px 0 #000, -2px 2px 0 #000,
               2px 2px 0 #000, -2px 0 0 #000, 2px 0 0 #000,
               0 -2px 0 #000, 0 2px 0 #000,
               0 0 8px rgba(0, 0, 0, 0.6) !important;
  font-weight: 900 !important;
  line-height: 1.6 !important;
}
/* 3) 名牌：蓝色圆角胶囊白字，保持引擎原位（台词框左上）。
   不强制居中——实测 showName 是"底+字"双层绝对叠放结构，两层内容宽度
   不同，left:50% 各自居中会错位分离（2026-11-14 实拍验证）；
   MyGO 参考图名牌本就在左上，原位即目标效果。 */
[class*="TextBox_showName"] {
  background: rgba(76, 136, 255, 0.92) !important;
  border: none !important;
  border-radius: 999px !important;
  color: #ffffff !important;
  opacity: 1 !important;
  width: -moz-fit-content !important;
  width: fit-content !important;
  padding: 0.18em 1.2em !important;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.35) !important;
}
[class*="TextBox_showName"] * {
  color: #ffffff !important;
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.45) !important;
}
"""


def _merge_missing(src_dir, dst_dir, warn):
    """把 src_dir 里「dst_dir 还没有」的文件补进 dst_dir（**绝不覆盖已有文件**）。

    用途：playable 导出拷贝运行时 game/ 时，只补引擎必需资源
    （template/template.json、animation/animationTable.json、
    userStyleSheet.css 等），保留我们生成的 scene/start.txt、config.txt、
    background/、figure/ 等导出内容。返回补入文件数。
    """
    n = 0
    try:
        os.makedirs(dst_dir, exist_ok=True)
    except OSError as e:
        warn.append("补全目录失败 %s: %r" % (dst_dir, e))
        return 0
    for root, _dirs, files in os.walk(src_dir):
        rel = os.path.relpath(root, src_dir)
        target = dst_dir if rel == "." else os.path.join(dst_dir, rel)
        for fn in files:
            s = os.path.join(root, fn)
            d = os.path.join(target, fn)
            if os.path.exists(d):
                continue                    # 已有（导出内容优先）
            try:
                os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
                shutil.copy2(s, d)
                n += 1
            except OSError as e:
                warn.append("补全文件失败 %s: %r" % (fn, e))
    return n


def _app_version():
    """水印里的木偶工作室版本。

    优先级：① l2d_rig_qt.py 的 VERSION 常量（**程序真实版本，唯一版本源**）
            ② version.json 的 version 字段（**发布/更新检查用**，可能与
               程序版本不同步——例如 v3.4.0 定版时用户拍板"不上传、保持
               version.json=3.3.0"，此时不能拿它当程序版本）
            ③ "?"

    历史 bug：只读 version.json 导致 v3.4.0 导出的包水印仍显示 v3.3.0。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    # ① 程序 VERSION 常量（正则抽，避免 import l2d_rig_qt 拉起整个 Qt 应用）
    try:
        with open(os.path.join(here, "l2d_rig_qt.py"), encoding="utf8",
                  errors="ignore") as f:
            for line in f:
                s = line.strip()
                if s.startswith("VERSION") and "=" in s:
                    v = s.split("=", 1)[1].strip().strip("'\"")
                    if v and v[0].isdigit():
                        return v
    except OSError:
        pass
    # ② version.json 兜底
    try:
        with open(os.path.join(here, "version.json"), encoding="utf8") as f:
            return str(json.load(f).get("version", "?"))
    except Exception:
        return "?"


# ================= H1e 可玩包启动器（task-62） =================
# 用户实机踩坑：双击 index.html（file://）**黑屏**——官方 4.6.6 是 ES module
# 构建，file:// 下 CORS 拒绝加载模块，React 不启动（只有静态水印 div 可见）。
# 必须经 http:// 起服务。终端玩家机器没有 Python → 方案自包含：
#   ① 主：lead 用 PyInstaller 编译 game_server.exe（找空闲端口→挂游戏目录
#      http.server→开浏览器→常驻），进 git 模板位
#      templates/webgal/launcher/game_server.exe，随安装包分发（我方自研工具，
#      不涉第三方素材红线）。导出时拷为产物根「启动游戏.exe」。
#   ② 兜底：exe 模板位不存在（开发态未编译）生成「启动游戏.bat」
#      （形态=lead 实测 8788 起服务+HTTP 200 验证过的 write_launch_bat.py）。
#      两个实测坑（lead 踩过，勿回退）：**必须 CRLF 行尾**（cmd 批处理解析器
#      靠 CR 定位行首，LF-only 会把标签/引号/中文切歪→"双击没用"）；
#      **延时用 ping 不用 timeout**（非控制台 stdin 下 timeout 报"不支持
#      输入重定向"并中断批处理）。
#   env MUPPET_LAUNCHER_EXE_TPL 覆盖模板位（测试 mock exe 分支 + 特殊部署）。
LAUNCH_BAT_LINES = [
    "@echo off",
    "chcp 936 >nul",
    'cd /d "%~dp0"',
    "set PORT=8788",
    ":findport",
    'netstat -an | findstr ":%PORT% " >nul 2>&1 && (set /a PORT+=1 & goto findport)',
    "echo 正在启动游戏服务（端口 %PORT%）...",
    'start "WebGAL 游戏服务(关闭此窗口=退出游戏)" /min cmd /c "py -3 -m http.server %PORT% 2>nul || python -m http.server %PORT% || (echo 本机没有 Python：请到 python.org 安装后重新双击本脚本。 & pause)"',
    "ping -n 3 127.0.0.1 >nul",
    'start "" "http://127.0.0.1:%PORT%/index.html"',
    "exit",
]


def _launch_bat_bytes():
    return ("\r\n".join(LAUNCH_BAT_LINES) + "\r\n").encode(
        "gbk", errors="replace")


def _inject_launcher(out_dir, warn):
    """playable 包写启动器：exe 优先，bat 兜底。返回 ("exe"|"bat"|None, 名)。"""
    tpl = os.environ.get("MUPPET_LAUNCHER_EXE_TPL", "").strip()
    if not tpl:
        tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "templates", "webgal", "launcher",
                           "game_server.exe")
    if os.path.isfile(tpl):
        try:
            shutil.copy2(tpl, os.path.join(out_dir, "启动游戏.exe"))
            return "exe", "启动游戏.exe"
        except Exception as e:
            warn.append("启动器 exe 拷贝失败，回落 bat: %r" % e)
    try:
        with open(os.path.join(out_dir, "启动游戏.bat"), "wb") as f:
            f.write(_launch_bat_bytes())
        return "bat", "启动游戏.bat"
    except Exception as e:
        warn.append("启动器 bat 写出失败: %r" % e)
        return None, None


def _runtime_version(rt_st, rt_dir):
    """水印第二行 WebGAL 版本：**以运行时树 webgal-engine.json 为准**
    （官方元数据，如 4.6.6）；runtime_pack.status().version 是包自身版本
    （实测可能是 1.5 这类打包号，不能当引擎版本用），只作兜底。"""
    try:
        with open(os.path.join(rt_dir, "webgal-engine.json"),
                  encoding="utf8") as f:
            obj = json.load(f)
        v = obj.get("webgalVersion") or obj.get("version")
        if v:
            return str(v)
    except Exception:
        pass
    v = (rt_st or {}).get("version")
    return str(v) if v else "4.x"


def _skin_watermark_html(app_ver, rt_ver, live_badge):
    """右上角引擎水印（两行小字半透明）+ 可选左下直播风角标。"""
    html = ('<div id="muppet-watermark" style="position:fixed;right:14px;'
            'top:10px;z-index:2147483000;pointer-events:none;'
            'text-align:right;font:12px/1.5 \'Segoe UI\',sans-serif;'
            'color:rgba(255,255,255,.78);'
            'text-shadow:0 1px 2px rgba(0,0,0,.8);">'
            'WebGAL 木偶工作室 Engine v%s<br>(Based on WebGAL v%s)</div>'
            % (app_ver, rt_ver))
    try:
        n = int(live_badge or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        html += ('<div id="muppet-live-badge" style="position:fixed;left:12px;'
                 'bottom:10px;z-index:2147483000;pointer-events:none;'
                 'font:13px/1 \'Segoe UI\',sans-serif;color:rgba(255,255,255,.9);'
                 'background:rgba(0,0,0,.38);padding:5px 12px;'
                 'border-radius:999px;">\U0001f464 %d人正在看</div>' % n)
    return html


# ================= H1f 版本哨兵（task-66） =================
# 用户两次被 WebGAL serviceworker 全量缓存坑：同端口同路径重导出后浏览器
# 仍跑旧 start.txt（旧引用 404 → 人物空，干净 headless 同包正常实证）。
# 哨兵注入 index.html <head> 最顶部（早于 module script/SW 注册）：
#   - build = 导出毫秒时间戳 + 包名 hash（每次导出必不同；index.html 因此
#     不再逐字节稳定——涉及 index.html 的回归锚只比 marker/顺序，不比全文）
#   - localStorage 旧值 != build 才清 caches + unregister + replace 刷新
#     一次（fresh 参数防死循环：刷新后 build 相同不再触发）
#   - 首次访问（无旧值）只登记不打扰；localStorage 异常 try 吞（file://）
SENTINEL_BUILD_KEY = "muppet_build"
_SENTINEL_JS = (
    '<script>\n'
    '(function(){var b="__BUILD__";try{'
    'var l=localStorage.getItem("muppet_build");'
    'if(l!==b){localStorage.setItem("muppet_build",b);'
    'caches&&caches.keys().then(k=>Promise.all(k.map(n=>caches.delete(n))));'
    'navigator.serviceWorker&&navigator.serviceWorker.getRegistrations()'
    '.then(rs=>{rs.forEach(r=>r.unregister());'
    'if(l!==null&&l!==undefined)'
    'location.replace(location.pathname+location.search'
    '+(location.search?"&":"?")+"fresh="+Date.now());});}'
    '}catch(e){}})();\n'
    '</script>')


def _build_id(out_dir):
    import hashlib as _h
    import time as _t
    name = os.path.basename(os.path.normpath(str(out_dir))) or "game"
    return "%d-%s" % (int(_t.time() * 1000),
                      _h.md5(name.encode("utf8")).hexdigest()[:8])


def _inject_sentinel_playable(out_dir, warn, stats):
    """head 最顶注入版本哨兵（幂等：已含 muppet_build 则跳过）。"""
    idx = os.path.join(out_dir, "index.html")
    try:
        with open(idx, encoding="utf8") as f:
            s = f.read()
    except OSError as e:
        warn.append("哨兵读 index.html 失败: %r" % e)
        return False
    if SENTINEL_BUILD_KEY in s:
        return True
    build = _build_id(out_dir)
    js = _SENTINEL_JS.replace("__BUILD__", build)
    if "<head>" in s:
        s = s.replace("<head>", "<head>\n" + js, 1)
    else:
        s = js + s
    try:
        with open(idx, "w", encoding="utf8", newline="") as f:
            f.write(s)
        stats["build"] = build
        return True
    except OSError as e:
        warn.append("哨兵写 index.html 失败: %r" % e)
        return False


def _inject_skin_playable(out_dir, app_ver, rt_ver, live_badge, warn):
    """playable 包：index.html 注入 <style>（</head> 前）+ 水印 div（</body>
    前）。幂等（SKIN_STYLE_MARK 已存在则跳过）。"""
    idx = os.path.join(out_dir, "index.html")
    try:
        with open(idx, encoding="utf8") as f:
            s = f.read()
    except OSError as e:
        warn.append("皮肤注入读 index.html 失败: %r" % e)
        return False
    if SKIN_STYLE_MARK in s:
        return True
    head = ("<!-- %s --><style>%s</style>" % (SKIN_STYLE_MARK, SKIN_CSS))
    body = _skin_watermark_html(app_ver, rt_ver, live_badge)
    if "</head>" in s:
        s = s.replace("</head>", head + "</head>", 1)
    else:
        s = head + s
    if "</body>" in s:
        s = s.replace("</body>", body + "</body>", 1)
    else:
        s = s + body
    try:
        with open(idx, "w", encoding="utf8", newline="") as f:
            f.write(s)
        return True
    except OSError as e:
        warn.append("皮肤注入写 index.html 失败: %r" % e)
        return False


def _inject_skin_content_only(game_root, warn):
    """非 playable 导出：皮肤写 game/style/muppet.css + start.txt 头部注释
    提示手动 link（内容包不含 index.html，无从注入）。"""
    d = os.path.join(game_root, "style")
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "muppet.css"), "w", encoding="utf-8",
                  newline="\n") as f:
            f.write(SKIN_CSS)
        sp = os.path.join(game_root, "scene", "start.txt")
        if os.path.isfile(sp):
            with open(sp, encoding="utf8") as f:
                s = f.read()
            tip = _comment("视觉皮肤（木偶工作室 MyGO 风）：把本游戏包的 "
                           "index.html </head> 前加 link 标签引入 "
                           'game/style/muppet.css（rel=stylesheet, '
                           'href=game/style/muppet.css）即生效 [%s]'
                           % SKIN_STYLE_MARK) + "\n"
            if SKIN_STYLE_MARK not in s:
                with open(sp, "w", encoding="utf8", newline="") as f:
                    f.write(tip + s)
        return True
    except OSError as e:
        warn.append("皮肤 css 写入失败: %r" % e)
        return False


# ================= H1c Live2D core 注入（task-60） =================
# 官方 WebGAL 运行时因专有许可**不带 Live2D core**（实测 runtime 树无 lib/），
# Cubism2 人物在导出包里 initLive2D 读不到 window.live2dPromise →
# undefined.then 崩 → 人物静默不显示（只剩背景台词）。修复：从用户本机
# 找到 live2d.min.js + live2dcubismcore.min.js 拷入导出包 lib/，并在
# module script 之前设置 window.live2dPromise（lead 演示包手动补丁写法，
# 已验证人物动作正常）。
# 版权红线：core js 只从用户本地磁盘拷进用户本地导出包，绝不进 git/安装包。
# v3.4.2「完全独立」：core 随素材库交付（asset_library/lib/），用户装完
# 即有、无需本机装过整合包；git/安装包仍然零 core（红线不变，素材库在
# 安装包之外、由用户自行部署）。
_L2D_FILES = ("live2dcubismcore.min.js", "live2d.min.js")
_L2D_MARK = "muppet-live2d-core-v1"
_L2D_WARN = ("未找到 Live2D core js（专有许可官方不随包），人物将不显示——"
             "请把 live2d.min.js + live2dcubismcore.min.js 放进素材库 "
             "asset_library/lib/，或设置 MUPPET_LIVE2D_CORE_DIR 指向该目录")
_L2D_CACHE = {"dir": None, "searched": False}   # 命中即缓存；searched 防重复扫盘


def _l2d_valid(d):
    return bool(d) and all(os.path.isfile(os.path.join(d, f))
                           for f in _L2D_FILES)


def _asset_lib_core_dir():
    """素材库里的 core 目录（v3.4.2 起的主依赖位）：asset_library/lib/。
    定位统一委托 asset_paths（程序目录/asset_library > 开发机布局 > 注册表，
    env MUPPET_ASSET_INDEX 硬限定）。返回路径，不保证存在。"""
    try:
        import asset_paths as _AP
        return _AP.core_dir()
    except Exception:
        env = os.environ.get("MUPPET_ASSET_INDEX", "").strip()
        if env:
            p = os.path.normpath(env)
            root = p if os.path.isdir(p) else os.path.dirname(p)
        else:
            here = os.path.dirname(os.path.abspath(__file__))
            root = os.path.normpath(os.path.join(
                here, "..", "webgal_games", "asset_library"))
        return os.path.join(root, "lib")


def _find_live2d_core(rt_dir=None):
    """查找顺序：①env MUPPET_LIVE2D_CORE_DIR ②runtime 树 lib/（未来版本
    自带则直接用）③**素材库 lib/**（v3.4.2：用户装完即可用，不再要求
    本机有整合包——"完全独立"用户诉求）④env MUPPET_WEBGAL_PACK_DIR
    （**限定**：设了就只扫它，同 COMFY_API 语义，测试隔离用）⑤默认整合包位
    （用户本地尽力扫描，路径硬编码仅为发现、不入库）。
    ③ 用 MUPPET_ASSET_INDEX 而非 MUPPET_WEBGAL_PACK_DIR 做隔离开关，
    这样"整合包不存在"不再等于"core 找不到"。
    命中缓存模块级；env 优先于缓存。"""
    env = os.environ.get("MUPPET_LIVE2D_CORE_DIR", "").strip()
    if env and _l2d_valid(env):
        return env
    if rt_dir:
        d = os.path.join(rt_dir, "lib")
        if _l2d_valid(d):
            return d
    if _L2D_CACHE["dir"] and _l2d_valid(_L2D_CACHE["dir"]):
        return _L2D_CACHE["dir"]
    d = _asset_lib_core_dir()
    if _l2d_valid(d):
        _L2D_CACHE["dir"] = d
        _L2D_CACHE["searched"] = True
        return d
    packenv = os.environ.get("MUPPET_WEBGAL_PACK_DIR", "").strip()
    roots = [packenv] if packenv else [r"D:\BaiduNetdiskDownload"]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        pats = [os.path.join(root, "*WebGAL整合包*", "webgal-main", "public",
                             "games", "*", "lib"),
                os.path.join(root, "*Derivative_Engine*", "*MyGO*", "lib")]
        for pat in pats:
            for d in glob.glob(pat):
                if _l2d_valid(d):
                    _L2D_CACHE["dir"] = d
                    _L2D_CACHE["searched"] = True
                    return d
    _L2D_CACHE["searched"] = True
    return None


def _l2d_scripts_html():
    """lead 演示包手动补丁原文（照抄）：两个 core script + live2dPromise
    设置，必须排在引擎 module script **之前**。"""
    return ('<script src="./lib/live2dcubismcore.min.js"></script>'
            '<script src="./lib/live2d.min.js"></script>'
            '<script>window.live2dPromise=Promise.resolve('
            '[window.Live2D, window.Live2DCubismCore]);</script>'
            '<!-- %s -->' % _L2D_MARK)


def _inject_live2d_core(out_dir, rt_dir, warn, stats, playable):
    """core 拷贝 + index.html 注入（playable）/ muppet 提示注释（非 playable
    内容包）。返回 True=已注入或此前已注入。"""
    src_dir = _find_live2d_core(rt_dir)
    if not src_dir:
        warn.append(_L2D_WARN)
        stats["live2d_core"] = False
        return False
    libdst = os.path.join(out_dir, "lib")
    try:
        os.makedirs(libdst, exist_ok=True)
        for f in _L2D_FILES:
            dst = os.path.join(libdst, f)
            if not (os.path.isfile(dst) and
                    os.path.getsize(dst) == os.path.getsize(
                        os.path.join(src_dir, f))):
                shutil.copy2(os.path.join(src_dir, f), dst)
        stats["live2d_files"] = len(os.listdir(libdst))
    except Exception as e:
        warn.append("Live2D core 拷贝失败: %r" % e)
        stats["live2d_core"] = False
        return False
    if playable:
        idx = os.path.join(out_dir, "index.html")
        try:
            with open(idx, encoding="utf8") as f:
                s = f.read()
        except OSError as e:
            warn.append("Live2D core 读 index.html 失败: %r" % e)
            stats["live2d_core"] = False
            return False
        if _L2D_MARK not in s:
            tag = '<script type="module"'
            html = _l2d_scripts_html()
            if tag in s:
                s = s.replace(tag, html + "\n    " + tag, 1)
            elif "</head>" in s:
                s = s.replace("</head>", html + "</head>", 1)
            else:
                s = html + s
            try:
                with open(idx, "w", encoding="utf8", newline="") as f:
                    f.write(s)
            except OSError as e:
                warn.append("Live2D core 写 index.html 失败: %r" % e)
                stats["live2d_core"] = False
                return False
    else:
        # 内容包：start.txt 头部注释给出手动补丁三行（用户覆盖到自己 runtime）
        sp = os.path.join(out_dir, "game", "scene", "start.txt")
        try:
            if os.path.isfile(sp):
                with open(sp, encoding="utf8") as f:
                    s = f.read()
            if _L2D_MARK not in s:
                tip = _comment(
                    "Live2D 运行前提：把本包 lib/ 两个 js 拷到你的 WebGAL 根目录 "
                    "lib/，并在 index.html 的 <script type=module 标签之前加三行"
                    "补丁——先两个 core script（src=./lib/live2dcubismcore.min.js "
                    "与 ./lib/live2d.min.js），再一行 "
                    "window.live2dPromise=Promise.resolve("
                    "[window.Live2D, window.Live2DCubismCore]) 收尾；"
                    "或用桌面端 playable 导出自动完成 [%s]" % _L2D_MARK) + "\n"
                with open(sp, "w", encoding="utf8", newline="") as f:
                    f.write(tip + s)
        except OSError as e:
            warn.append("Live2D core 提示注释写入失败: %r" % e)
    stats["live2d_core"] = True
    return True


# ================= 主导出 =================
def _rand_key():
    n = random.randint(6, 10)
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(n))


def _load_json_proj(path):
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    try:
        import video_model as VM
        proj = VM.sanitize_project(obj)
        if proj is None:
            raise ValueError("不是有效的 video_edit 工程 JSON")
        return VM.normalize_ids(proj)
    except ImportError:
        if not isinstance(obj, dict) or "tracks" not in obj:
            raise ValueError("不是有效的 video_edit 工程 JSON")
        return obj


# ================= F2-L2（v3.5）：多幕 / 分支导出 =================
# 设计文档 docs/F2分支剧情设计.md §3。全部用**标准 WebGAL 语法**，零方言：
#   - 多幕 -> game/scene/<safe(aid)>.txt，首幕同时写 start.txt（引擎固定入口）
#   - choice -> `choose:文本1:scene1.txt|文本2:scene2.txt;`（§8-1 已核实）
#   - option.set -> **伪幕**：生成 <aid>__c<cid>_<i>.txt，内容是若干
#     `setVar:…;` + `changeScene:<真目标>;`。零方言、组合任意（§3.2）
#   - option.when -> 同上伪幕 + 头部 `if:cond:…;` 兜底（§8-3 未证实，
#     无 when 时不生成，绝不猜语法）
#   - jumps -> 幕内 t 处 `if:表达式:target.txt;`（§8-2：if 是真实命令，
#     jumpIf 不存在）
#   - next -> 幕尾 `changeScene:<target>.txt;`
#   - vars -> **只写 start.txt 头部** `setVar:名=默认值;`（§3.2：各幕不重复
#     声明，避免播放中被重置）
#
# 稳定性契约：伪幕/scene 文件名由 (aid, cid, option_index) 纯函数决定，
# 同工程两次导出逐字节一致（回归可断言）。

_BRANCH_RESERVED = "__"


def _scene_filename(aid, pool=None):
    """幕 id -> scene 文件名（安全化 + 去重）。start 保留给首幕。"""
    s = safe_name(str(aid or "act")) or "act"
    name = s + ".txt"
    if pool is not None:
        name = pool.alloc("scene", name)
    return name


def _pseudo_name(aid, cid, idx):
    """伪幕文件名（纯函数，保证两次导出同名）。"""
    a = safe_name(str(aid or "a")) or "a"
    c = safe_name(str(cid or "c")) or "c"
    return "%s%s%s_%d.txt" % (a, _BRANCH_RESERVED, c, int(idx))


def _esc_choose_text(t):
    """choose 选项文本转义（§8-1 demo_escape 实测）：`：`→`\\:`、
    `,`→`\\,`、`.`→`\\.`；另外我们自己还要护住分隔符 `:` 和 `|`。"""
    s = str(t or "").replace("\n", " ")
    for a, b in (("\\", "\\\\"), (":", "\\:"), ("|", "\\|"),
                 (",", "\\,"), (".", "\\.")):
        s = s.replace(a, b)
    return s


def _branch_target_scene(target, aid_to_scene):
    """分支目标 -> scene 文件名。

    目标可以是幕 id（本工程内跳转，映射成 scene 文件）或外部 scene 名
    （如 "chapter2.txt"，原样保留）。空目标 -> None（不 emit）。"""
    t = str(target or "").strip()
    if not t:
        return None
    if t in aid_to_scene:
        return aid_to_scene[t]
    if t.lower().endswith(".txt"):
        return t
    # 未匹配任何幕 id：当作外部 scene 名，补 .txt（并交给调用方记警告）
    return safe_name(t) + ".txt" if safe_name(t) else None


def _emit_branch_tail(act, ctx):
    """生成一幕结尾的分支语句 + 伪幕文件内容。

    返回 (tail_lines, pseudo_files)：
      tail_lines   = 追加到该幕 scene 文件末尾的行
      pseudo_files = [(文件名, [行...])]  伪幕（option.set 副作用）
    单幕工程（act 为 None / 无 choices/jumps/next）返回 ([], [])，调用方
    跳过整段 -> 逐字节等于旧输出。"""
    if not isinstance(act, dict):
        return [], []
    aid_to_scene = ctx.get("aid_to_scene") or {}
    scene_of_self = ctx.get("scene_of_self") or ""

    tail = []
    pseudo = []

    # ---- jumps：幕内条件跳转 ----
    for j in (act.get("jumps") or []):
        if not isinstance(j, dict):
            continue
        tgt = _branch_target_scene(j.get("target"), aid_to_scene)
        if not tgt:
            continue
        if tgt == scene_of_self:
            continue                     # 跳自己 = 无操作，跳过（防死循环）
        cond = str(j.get("when") or "").strip()
        if not cond:
            # 无条件跳转：幕内任意时刻无条件跳走会截断后续剧情，
            # 语义上等价于 next；为安全起见按幕尾 changeScene 处理。
            continue
        # 条件里出现裸分号会切断语句，转全角（§8-6 注释同样的防护）
        cond = cond.replace(";", "；")
        tail.append(_note("条件跳转 @%.2fs" % _f(j.get("t"))))
        tail.append("if:%s:%s;" % (cond, tgt))

    # ---- choices：选择点 ----
    for ch in (act.get("choices") or []):
        if not isinstance(ch, dict):
            continue
        cid = ch.get("cid")
        opts = [o for o in (ch.get("options") or []) if isinstance(o, dict)]
        if not opts:
            continue
        prompt = str(ch.get("prompt") or "").strip()
        t_ch = ch.get("t")
        if prompt:
            # prompt 用旁白台词展示（可选，§3.2）
            tail.append(_note("选择点 @%s" % ("幕尾" if t_ch is None
                                            else "%.2fs" % _f(t_ch))))
            ptxt = prompt.replace(";", "；").replace("\\n", " ")
            tail.append("旁白:%s;" % ptxt)
        parts = []
        for i, o in enumerate(opts):
            txt = _esc_choose_text(o.get("text"))
            setexpr = o.get("set")
            # option.set 契约 = 字符串列表（video_model._sanitize_str_list）；
            # 兼容早期/外部传来的单个 "k=v;k2=v" 字符串形态。
            if isinstance(setexpr, (list, tuple)):
                set_list = [str(x).strip() for x in setexpr if str(x or "").strip()]
            elif setexpr:
                set_list = [x.strip() for x in str(setexpr).split(";")
                            if x.strip()]
            else:
                set_list = []
            when = str(o.get("when") or "").strip()
            real = _branch_target_scene(o.get("target"), aid_to_scene)
            if set_list or when:
                # 伪幕：无条件副作用 + 跳转；when 无对应语法时退化为
                # 伪幕内 if + 兜底（绝不在 choose 行写未证实语法）
                pname = _pseudo_name(ctx.get("aid"), cid, i)
                plines = []
                if when:
                    plines.append(_note("选项条件（导出为伪幕 if 判断）"))
                    w = when.replace(";", "；")
                    plines.append("if:%s:%s;" % (w, real or ctx.get(
                        "fallback_scene", "")))
                for kv in set_list:
                    plines.append("setVar:%s;" % kv.replace(";", "；"))
                if real:
                    plines.append("changeScene:%s;" % real)
                elif ctx.get("fallback_scene"):
                    plines.append("changeScene:%s;" % ctx["fallback_scene"])
                pseudo.append((pname, plines))
                tgt = pname
            else:
                tgt = real
            if not tgt:
                continue
            parts.append("%s:%s" % (txt, tgt))
        if parts:
            tail.append("choose:%s;" % "|".join(parts))

    # ---- next：幕尾续接 ----
    nxt = act.get("next")
    if nxt:
        tgt = _branch_target_scene(nxt, aid_to_scene)
        if tgt and tgt != scene_of_self:
            tail.append("changeScene:%s;" % tgt)

    return tail, pseudo


def _collect_vars_lines(proj):
    """工程级 vars -> start.txt 头部 setVar 初始化行（只写一次）。"""
    out = []
    for v in (proj.get("vars") or []):
        if not isinstance(v, dict):
            continue
        name = str(v.get("name") or "").strip()
        if not name:
            continue
        dflt = v.get("default", 0)
        if isinstance(dflt, bool):
            dflt = 1 if dflt else 0
        elif isinstance(dflt, (int, float)):
            dflt = dflt
        else:
            dflt = '"%s"' % str(dflt).replace('"', "").replace(";", "；")
        out.append("setVar:%s=%s;" % (name, dflt))
    return out


def export_branch_game(proj, out_dir, proj_dir=None, game_name=None,
                       playable=False, style=True, live_badge=0):
    """F2-L2 编排器：v2 多幕工程 -> 多 scene 文件 + 分支语句的标准 WebGAL 包。

    流程：
      1. 建目录骨架（只一次）
      2. 预先分配"幕 id -> scene 文件名"映射（首幕 = start.txt）
      3. 逐幕递归调 export_game（共享 ctx：名字池/资源去重/first_bg/stats）
      4. vars 初始化行插到 start.txt 头部（仅当有 vars）
      5. 统一收尾：playable 运行时拷贝 / 皮肤 / Live2D core / 启动器
    返回 (ok, msg, stats)，stats 额外带 scenes 数量。
    """
    acts = [a for a in (proj.get("acts") or []) if isinstance(a, dict)]
    if not acts:
        return False, "多幕工程里没有有效幕", {"files": 0, "lines": 0,
                                          "warnings": []}
    out_dir = str(out_dir)
    game_root = os.path.join(out_dir, "game")

    stats = {"files": 0, "lines": 0, "warnings": []}
    warn = stats["warnings"]
    try:
        for d in GAME_SUBDIRS:
            os.makedirs(os.path.join(game_root, d), exist_ok=True)
    except OSError as e:
        return False, "创建目录失败: %r" % e, stats

    # 幕 id -> scene 文件名（首幕 = start.txt，保持引擎入口约定）
    aid_to_scene = {}
    pool = _NamePool()
    for i, a in enumerate(acts):
        aid = str(a.get("aid") or ("a%d" % (i + 1)))
        if i == 0:
            aid_to_scene[aid] = "start.txt"
        else:
            aid_to_scene[aid] = _scene_filename(aid, pool)

    ctx = {"stats": stats, "pool": pool, "aid_to_scene": aid_to_scene,
           "puppet_refs": _collect_puppet_motion_refs(proj)}

    # 逐幕生成
    for a in acts:
        aid = str(a.get("aid") or "")
        scene_name = aid_to_scene.get(aid) or _scene_filename(aid, pool)
        view = {"type": proj.get("type", "video_edit"),
                "width": proj.get("width", 1920),
                "height": proj.get("height", 1080),
                "fps": proj.get("fps", 30.0),
                "tracks": a.get("tracks") or [],
                "dialogue": a.get("dialogue") or [],
                "comments": a.get("comments") or []}
        ctx["act"] = a
        ctx["aid"] = aid
        ctx["scene_of_self"] = scene_name
        ctx["fallback_scene"] = aid_to_scene.get(str(a.get("next") or ""), "")
        try:
            ok, msg, _st = export_game(view, out_dir, proj_dir=proj_dir,
                                       game_name=game_name, dialect="webgal",
                                       playable=False, style=False,
                                       live_badge=live_badge,
                                       _scene=scene_name, _ctx=ctx)
        except Exception as e:
            warn.append("幕 %s 导出异常: %s: %s" % (aid, type(e).__name__, e))
            continue
        if not ok:
            warn.append("幕 %s 导出失败: %s" % (aid, msg))
        stats["scenes"] = stats.get("scenes", 0) + 1

    # vars 初始化插到 start.txt 头部（只此一处，避免播放中被各幕重置）
    var_lines = _collect_vars_lines(proj)
    start_path = os.path.join(game_root, "scene", "start.txt")
    if var_lines and os.path.isfile(start_path):
        try:
            with open(start_path, "r", encoding="utf-8") as f:
                body = f.read()
            head = [_note("变量初始化（全局仅此一处）")] + var_lines
            with open(start_path, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(head) + "\n" + body)
            stats["lines"] = stats.get("lines", 0) + len(var_lines)
        except OSError as e:
            warn.append("变量初始化写入失败: %r" % e)

    # ---- 统一收尾：playable / 皮肤 / Live2D core ----
    if playable:
        try:
            import runtime_pack as RP
            rt_st = RP.status()
        except ImportError:
            return False, "未安装 WebGAL 运行时：runtime_pack 缺失", stats
        except Exception as e:
            return False, "playable 读取运行时状态失败: %r" % e, stats
        if not rt_st.get("installed"):
            return False, "未安装 WebGAL 运行时：请先在模板页导入压缩包或资产管理下载", stats
        rt_dir = rt_st.get("path")
        if rt_dir and os.path.isdir(rt_dir):
            for entry in sorted(os.listdir(rt_dir)):
                src = os.path.join(rt_dir, entry)
                dst = os.path.join(out_dir, entry)
                try:
                    if os.path.isdir(src):
                        if entry == "game":
                            _merge_missing(src, dst, warn)
                        else:
                            shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                    stats["files"] += 1
                except Exception as e:
                    warn.append("playable 拷贝失败 %s: %r" % (entry, e))
        stats["playable"] = True
        # v3.5.13：字体子集替换（同上，两处 playable 路径都要做）
        _slim_runtime_fonts(out_dir, warn, stats)
        lk, _ln = _inject_launcher(out_dir, warn)
        if lk:
            stats["launcher"] = lk
        _inject_sentinel_playable(out_dir, warn, stats)
        if style:
            if _inject_skin_playable(out_dir, _app_version(),
                                     _runtime_version(rt_st, rt_dir),
                                     live_badge, warn):
                stats["skin"] = "injected"
            else:
                stats["skin"] = "failed"
        else:
            stats["skin"] = "off"
        if stats.get("model_dirs", 0) > 0:
            _inject_live2d_core(out_dir, rt_dir, warn, stats, playable=True)
    else:
        if style and _inject_skin_content_only(game_root, warn):
            stats["skin"] = "css-only"
        if stats.get("model_dirs", 0) > 0:
            _inject_live2d_core(out_dir, None, warn, stats, playable=False)

    n_scenes = stats.get("scenes", 0)
    msg = "已导出 WebGAL 分支工程到 %s（%d 幕，语句 %d，文件 %d，警告 %d%s）" % (
        game_root, n_scenes, stats["lines"], stats["files"], len(warn),
        "，含启动器(双击「启动游戏」打开)" if stats.get("playable") else "")
    return True, msg, stats


def render_branch_script(proj, proj_dir=None):
    """act.script 命令用的**纯内存脚本预览**（不写盘）。

    返回 (ok, data)：data = {"scenes": [{name, aid, lines:[...]}],
    "vars": [...], "warnings": [...]}。给 DSH 代理在对话里直接看导出结果。
    单幕工程也能用（返回单个 start.txt 内容）。"""
    res = {"scenes": [], "vars": [], "warnings": []}
    for v in (proj.get("vars") or []):
        if isinstance(v, dict) and v.get("name"):
            res["vars"].append(dict(v))
    acts = [a for a in (proj.get("acts") or []) if isinstance(a, dict)]
    if not acts:
        # 单幕：直接渲染成脚本
        try:
            view = {"type": proj.get("type"), "width": proj.get("width"),
                    "height": proj.get("height"), "fps": proj.get("fps"),
                    "tracks": proj.get("tracks") or [],
                    "dialogue": proj.get("dialogue") or [],
                    "comments": proj.get("comments") or []}
            lines, pseudo, _w = _render_scene_lines(view, proj_dir, "start.txt",
                                                    {}, None)
            res["scenes"].append({"name": "start.txt", "aid": "a1",
                                  "lines": lines})
            for pn, pl in pseudo:
                res["scenes"].append({"name": pn, "aid": "a1", "lines": pl})
        except Exception as e:
            return False, {"error": "%s: %s" % (type(e).__name__, e),
                           "warnings": res["warnings"]}
        return True, res

    aid_to_scene = {}
    pool = _NamePool()
    for i, a in enumerate(acts):
        aid = str(a.get("aid") or ("a%d" % (i + 1)))
        aid_to_scene[aid] = "start.txt" if i == 0 else _scene_filename(aid, pool)
    for a in acts:
        aid = str(a.get("aid") or "")
        try:
            view = {"type": proj.get("type"), "width": proj.get("width"),
                    "height": proj.get("height"), "fps": proj.get("fps"),
                    "tracks": a.get("tracks") or [],
                    "dialogue": a.get("dialogue") or [],
                    "comments": a.get("comments") or []}
            lines, pseudo, w = _render_scene_lines(
                view, proj_dir, aid_to_scene[aid], aid_to_scene, a)
            res["scenes"].append({"name": aid_to_scene[aid], "aid": aid,
                                  "lines": lines})
            for pn, pl in pseudo:
                res["scenes"].append({"name": pn, "aid": aid, "lines": pl})
            res["warnings"].extend(w)
        except Exception as e:
            res["warnings"].append("幕 %s 渲染异常: %s" % (aid, e))
    return True, res


def _render_scene_lines(view, proj_dir, scene_name, aid_to_scene, act):
    """把一幕渲染成脚本行（不写盘）。供 render_branch_script 用。

    复用与落盘路径相同的 _emit_branch_tail；主语句体走一个极简的子集：
    只有台词（预览够用），完整映射以落盘路径为准。
    返回 (lines, pseudo, warnings)。"""
    lines = []
    warns = []
    for d in (view.get("dialogue") or []):
        if not isinstance(d, dict):
            continue
        txt = str(d.get("text") or "").replace(";", "；").replace("\n", " ")
        who = str(d.get("who") or "旁白").replace(":", "").replace(";", "")
        if txt:
            lines.append("%s:%s;" % (who or "旁白", txt))
    if act:
        ctx = {"aid": str(act.get("aid") or ""), "aid_to_scene": aid_to_scene,
               "scene_of_self": scene_name,
               "fallback_scene": aid_to_scene.get(str(act.get("next") or ""), "")}
        tail, pseudo = _emit_branch_tail(act, ctx)
        lines.extend(tail)
    else:
        pseudo = []
    return lines, pseudo, warns


# ================= 单幕导出（原有行为，逐字节不变） =================
def _slim_runtime_fonts(out_dir, warn, stats):
    """把导出包里运行时自带的大字体换成**预先子集化的版本**。

    背景（实测）：WebGAL 4.6.6 运行时带三个中文 TTF，合计 36.08 MB：
        资源圆体 ResourceHanRoundedCN  13.98 MB
        思源宋体 SourceHanSerifCN      12.41 MB
        WebgalUI OPPOSans              9.68 MB
    它们**全部**会在首屏被真正下载（不是声明了不下载 —— 用 performance
    API 实测到 1093/1365/1770 ms 三个请求）。首屏关键资源 37.6 MB 里
    36.1 MB 是字体，这就是「首次打开卡、刷新一下就顺」的直接原因。

    `templates/webgal/fonts/` 里是预生成的子集（共 7.88 MB，覆盖 GB2312
    全集 6763 字 + 日文常用汉字 + 假名 + 标点 + 剧本专有字符，实测剧本
    100% 覆盖、0 个未覆盖字符）。

    为什么预生成而不在导出时按剧本子集化：用户的 runtime.zip 里**没有**
    fontTools（实测确认），导出跑在用户机器上，依赖它会直接失败。

    做法：只改**导出产物**里的 assets/*.ttf，不动用户机器上的运行时本体
    （那个是共享的，改了会影响别的工程）。替换后同步改 index.html/CSS 里
    的文件名引用（原文件名带 webpack 内容 hash 后缀）。
    返回省下的字节数。"""
    fonts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "templates", "webgal", "fonts")
    if not os.path.isdir(fonts_dir):
        return 0
    assets = os.path.join(out_dir, "assets")
    if not os.path.isdir(assets):
        return 0
    # 子集文件名（无 hash）-> 内容
    slim = {}
    for f in os.listdir(fonts_dir):
        p = os.path.join(fonts_dir, f)
        if os.path.isfile(p) and f.lower().endswith(".ttf"):
            slim[f] = p
    if not slim:
        return 0
    saved = 0
    renamed = []                   # (原文件名, 新文件名)
    for f in sorted(os.listdir(assets)):
        if not f.lower().endswith(".ttf"):
            continue
        src = os.path.join(assets, f)
        # 原文件名形如 ResourceHanRoundedCN-Regular-C1HdCLVq.ttf
        # 子集名形如 ResourceHanRoundedCN.ttf -> 按前缀匹配
        hit = None
        for stem in slim:
            if f.startswith(os.path.splitext(stem)[0]):
                hit = stem
                break
        if hit is None:
            continue
        old_sz = os.path.getsize(src)
        new_sz = os.path.getsize(slim[hit])
        if new_sz >= old_sz:
            continue               # 子集反而更大就不换
        try:
            shutil.copy2(slim[hit], src)   # 保持原文件名，免改引用
            saved += old_sz - new_sz
            stats["fonts_slimmed"] = stats.get("fonts_slimmed", 0) + 1
        except Exception as e:
            warn.append("字体子集替换失败 %s: %r" % (f, e))
    if saved > 0:
        stats["fonts_saved_mb"] = round(saved / 1048576.0, 1)
    return saved


def export_game(proj, out_dir, proj_dir=None, game_name=None, dialect="webgal",
                playable=False, style=True, live_badge=0,
                _scene="start.txt", _ctx=None):
    """把视频工程 dict 导出为 WebGAL 工程目录。返回 (ok, msg, stats)。

    playable=True（E3）：要求本地已导入 WebGAL 运行时（runtime_pack），
    导出时把运行时树（index.html/assets/lib/…）拷入 out_dir，**跳过运行时自带
    game/ 目录**（导出内容层 game/ 占位）。产出根目录「启动游戏」exe/bat
    （H1e：官方 4.6.6 是 ES module，file:// 双击 index.html 会黑屏，必须经
    启动器起本地 http 服务后自动开浏览器）。运行时
    未安装 -> (False, 提示先装, 空 stats)。playable=False 行为逐字节不变。
    style=True（I2/task-58）：注入 MyGO 风字幕皮肤——playable 时直接改
    index.html（<style>+水印），非 playable 时写 game/style/muppet.css +
    start.txt 头注释；live_badge>0 追加左下"N人正在看"角标（默认关）。

    ---- F2-L2（v3.5）多幕分支导出 ----
    工程是 v2 多幕时：本函数负责**编排**（见文件尾部 export_branch_game），
    逐幕递归调用自身（`_scene` = 该幕的 scene 文件名，`_ctx` = 跨幕共享
    上下文：名字池/已拷贝资源/first_bg/stats/finalize 标记），保证：
      - 资源只拷一份（多幕共用 background/figure/bgm），不重复占体积
      - 单幕工程路径**逐字节不变**（不传 `_scene`/`_ctx` 即旧行为）
    """
    if dialect == "mygo":
        raise NotImplementedError("MyGO 方言导出器 v3.5 提供")
    if dialect != "webgal":
        return False, "未知方言: %r（仅 webgal / mygo）" % (dialect,), \
               {"files": 0, "lines": 0, "warnings": []}
    if not isinstance(proj, dict):
        return False, "proj 必须是 dict（video_edit 工程结构）", \
               {"files": 0, "lines": 0, "warnings": []}

    # F2-L2：v2 多幕 -> 交给编排器（单幕工程不走这里，行为零变化）
    if _ctx is None and isinstance(proj.get("acts"), list) and proj.get("acts"):
        return export_branch_game(proj, out_dir, proj_dir=proj_dir,
                                  game_name=game_name, playable=playable,
                                  style=style, live_badge=live_badge)

    ctx = _ctx if _ctx is not None else {}
    stats = ctx.get("stats") or {"files": 0, "lines": 0, "warnings": []}
    ctx["stats"] = stats
    warn = stats["warnings"]
    out_dir = str(out_dir)
    game_root = os.path.join(out_dir, "game")

    # 目录骨架（9 子目录）
    try:
        for d in GAME_SUBDIRS:
            os.makedirs(os.path.join(game_root, d), exist_ok=True)
    except OSError as e:
        return False, "创建目录失败: %r" % e, stats

    tracks = proj.get("tracks") or []
    W, H = _f(proj.get("width"), 1920), _f(proj.get("height"), 1080)
    # 底轨 = 最前的"轨名以 BG 开头"的可见 video 轨（导入器/用户语义命名），
    # 否则退回列表最前的可见 video 轨
    def _is_bg_name(tr):
        return str(tr.get("name") or "").upper().startswith("BG")
    bottom_tid = None
    for tr in tracks:
        if isinstance(tr, dict) and tr.get("kind") == "video" \
                and not tr.get("hidden") and _is_bg_name(tr):
            bottom_tid = tr.get("id")
            break
    if bottom_tid is None:
        for tr in tracks:
            if isinstance(tr, dict) and tr.get("kind") == "video" \
                    and not tr.get("hidden"):
                bottom_tid = tr.get("id")
                break

    # F2-L2：资源池跨幕共享——多幕导出时同一背景/人物只拷一份，
    # 幕间只共用文件名（scene 文件里各自引用同一份资源）。
    pool = ctx.get("pool") or _NamePool()
    ctx["pool"] = pool
    events = []                      # (t, prio, seq, [行, ...])
    _NONE_PENDING = []              # 待定 none 事件（v3.4.4：需后向判定）
    seq = 0
    # 多幕时 first_bg/first_bgm 也要跨幕持续（config 只写一份）
    first_bg = ctx.get("first_bg")   # config Title_img
    first_bgm = ctx.get("first_bgm")  # config Title_bgm
    cubism_seen = ctx.get("cubism_seen", False)
    # H2a：台词级动作的"落点"采集——每条立绘事件登记区间 (t0, t1, slot, ref)，
    # 台词带 motion/expression 时按时刻找回生效引用（见规则 10）。
    fig_intervals = []
    dialogue_prev_action = {}        # slot -> (ref, motion, expression) 连续去重
    copy_map = {"background": os.path.join(game_root, "background"),
                "figure": os.path.join(game_root, "figure"),
                "bgm": os.path.join(game_root, "bgm"),
                "video": os.path.join(game_root, "video")}
    # v3.4.9 性能修复：预扫 Cubism2 puppet clip 实际引用的动作/表情，
    # 导出时按引用瘦身 model.json + 按需拷池文件（引擎会全量预载定义，
    # 664 动作池 = 每模型 ~900 请求，场景切换重载再一遍 —— 播放卡顿根源）。
    # F2-L2：多幕时合并全工程引用（同一模型在不同幕用不同动作，必须都保留）。
    puppet_refs = ctx.get("puppet_refs")
    if puppet_refs is None:
        puppet_refs = _collect_puppet_motion_refs(proj)
        ctx["puppet_refs"] = puppet_refs
    copied = ctx.setdefault("copied", set())   # 已拷资源（跨幕去重）

    def copy_in(cat, src_path, out_name):
        dst = os.path.join(copy_map[cat], out_name)
        key = (cat, out_name.lower())
        if key in copied and os.path.isfile(dst):
            return True                    # 多幕复用：已拷过，跳过重复 IO
        try:
            if src_path and os.path.isfile(src_path):
                shutil.copyfile(src_path, dst)
                stats["files"] += 1
                copied.add(key)
                return True
        except Exception as e:
            warn.append("拷贝失败 %s: %r" % (out_name, e))
        warn.append("源文件缺失未拷入: %s" % (src_path or "(空路径)",))
        return False

    # K1（task-69）驻场语义：立绘 clip 的终点 == 工程终点（脚本自然收尾）
    # **不是显式退场**，不 emit changeFigure:none；只有终点早于工程终点
    # （中途显式退场/换装）才在该刻 emit none（必带槽位）。
    _proj_end = 0.0
    for _tr0 in tracks:
        for _c0 in (_tr0.get("clips") or []):
            if not isinstance(_c0, dict):
                continue
            _proj_end = max(_proj_end, _f(_c0.get("start")) + _f(_c0.get("dur")))
    for _d0 in (proj.get("dialogue") or []):
        if not isinstance(_d0, dict):          # 脏数据（非 dict 条目）跳过
            continue
        _proj_end = max(_proj_end, _f(_d0.get("t")) + _f(_d0.get("dur")))

    def _exit_none(t1, slot, ref=None, fid=None):
        """显式离场时刻的 none 事件（收尾时刻跳过）。

        v3.4.4 闪烁修复：**先登记，不立即发**。是否真的需要 none 取决于
        "这个角色之后还回不回来"——同角色换动作被拆成多个 clip 时，每个
        clip 结束都发 none 会让引擎在每句台词前卸载+重载 Live2D 模型
        （引擎 changeFigure 去重只在"同槽位同内容"时生效，none 把槽位清空
        后必然触发重载 + 200ms 淡入 = 人物闪烁）。
        真正的退场只在「该角色之后再不出现」时才发 none；还会回来的
        （换动作/换装）直接由新的 changeFigure 覆盖，引擎自行替换。

        ref = 该立绘的引用（角色名），供后向判定用。
        fid = K1（task-69）平移态的角色句柄（非三分位才有）：后向判定
              按 id 而非槽位（none -id 精确退场，step0 实拍已验证引擎支持）。
        """
        nonlocal seq
        if t1 < _proj_end - 0.005:
            _NONE_PENDING.append((t1, 1, seq, slot, ref, fid))
            seq += 1

    def figure_event(c, t0, t1, lay, out_name, slot, extra=None, fid=None):
        nonlocal seq, cubism_seen
        lines = []
        y = _f(lay.get("y"), 0.5)
        if abs(y - 0.5) > Y_DEV:
            lines.append(_note("提示：该立绘纵向位置 y=%.2f 需在 WebGAL "
                               "编辑器中手动调整" % y))
        # C2：clip 带 motion/expression 时，changeFigure 行尾分号前追加参数
        # （puppet 与 image 立绘共用此函数，故都加）。
        # K1（task-69）：非三分位 + 有角色句柄 → -id/-transform 平移形态。
        use_fid = fid if (fid and not _is_slot_x(lay.get("x"))) else None
        if use_fid:
            line = "changeFigure:%s -id=%s -transform=%s" % (
                out_name, use_fid, _transform_of(lay, W, H))
        else:
            line = "changeFigure:%s -%s" % (out_name, slot)
        if c.get("motion"):
            line += " -motion=%s" % c["motion"]
        if c.get("expression"):
            line += " -expression=%s" % c["expression"]
        lines.append(line + ";")
        if extra:
            lines.extend(extra)
        events.append((t0, 1, seq, lines))
        seq += 1
        _exit_none(t1, slot, out_name, use_fid)
        fig_intervals.append((t0, t1, slot, out_name, use_fid, lay))

    for tr in tracks:
        if not isinstance(tr, dict) or tr.get("hidden"):
            continue
        tr_kind = tr.get("kind")
        for c in (tr.get("clips") or []):
            if not isinstance(c, dict):
                continue
            src = c.get("source") or {}
            sk = src.get("kind")
            path = _abs_path(src.get("path"), proj_dir)
            t0 = _f(c.get("start"))
            dur = _f(c.get("dur"))
            t1 = t0 + dur
            lay = _layout(c, t0)
            raw_name = _src_filename(path)

            if sk == "image":
                # v3.5.13：纯色背景（导入端把 `changeBg:black` 记成
                # path="color:black"）**优先于一切文件判断**——它没有真实
                # 文件，_is_fullscreen/_has_alpha 都会因 stat 失败而误判。
                # 引擎原生支持 changeBg:black（实测真黑屏），原样写回即可。
                _cp = str(path)
                if _cp.startswith("color:"):
                    _kw = (_cp.split(":", 1)[1] or "black").strip() or "black"
                    events.append((t0, 0, seq, ["changeBg:%s;" % _kw]))
                    seq += 1
                    continue
                if tr_kind == "video" and tr.get("id") == bottom_tid and \
                        (_is_bg_name(tr) or _is_fullscreen(lay, src, W, H)) \
                        and not _has_alpha(path):
                    # ---- 规则 1：背景 ----
                    # BG 命名轨直接是背景（WebGAL changeBg 为 cover 模式，
                    # 4:3 图进 16:9 画布也是合法背景，不做几何覆盖率否决）
                    nm = pool.alloc("background", safe_name(raw_name) or "bg.png")
                    if not copy_in("background", path, nm):
                        # v3.5：源文件缺失（素材库没命中 / 脚本里是占位名）
                        # -> **不 emit 坏引用**。emit 出去引擎会去找一个
                        # 不存在的文件，背景加载失败（黑屏/残留旧背景），
                        # 用户看到的现象就是"背景不对"。
                        # copy_in 已记警告，这里再补一条更明确的原因。
                        warn.append("背景素材缺失，已跳过该背景: %s"
                                    % (raw_name or path))
                        continue
                    if first_bg is None:
                        first_bg = nm
                    events.append((t0, 0, seq, ["changeBg:%s -next;" % nm]))
                    seq += 1
                else:
                    # 非底轨/非满幅 image -> 立绘
                    slot = _slot(lay.get("x"))
                    nm = pool.alloc("figure", safe_name(raw_name) or "fig.png")
                    if not copy_in("figure", path, nm):
                        # v3.5：素材缺失 -> 不 emit 坏引用（同背景逻辑：
                        # emit 出去引擎找不到文件，人物不显示）
                        warn.append("立绘素材缺失，已跳过该立绘: %s"
                                    % (raw_name or path))
                        continue
                    figure_event(c, t0, t1, lay, nm, slot,
                                 fid=_fig_id(_who_export(path)))
            elif sk == "puppet":
                # ---- H1 task-43：Cubism2 模型目录分支（优先于渲染/Cubism4） ----
                # path 指向含 model.json 的目录 = Cubism2 整合包模型：
                # 整角色根拷进 figure/，changeFigure 引用 model.json。
                # WebGAL 运行时原生支持 Cubism2（lib\live2d.min.js），
                # 直接可玩——不写 Cubism导入指引.txt、不插 .moc3 注释。
                if _is_cubism2_dir(path):
                    slot = _slot(lay.get("x"))
                    used = puppet_refs.get(
                        os.path.normpath(os.path.abspath(path)))
                    # 只拷**用到的服装目录**（同角色可能有十几个服装，
                    # 全拷进来 = 几十 MB 体积 + 几千个不存在的池文件引用）
                    _role_root = _cubism2_role_root(path)
                    _used_costumes = None
                    _used_by_costume = None
                    if isinstance(puppet_refs, dict) and puppet_refs:
                        _rr = os.path.normpath(os.path.abspath(_role_root))
                        _used_costumes = set()
                        _used_by_costume = {}
                        for _k, _v in puppet_refs.items():
                            # puppet_refs 的 key 是**模型目录**
                            # （figure/<role>/<costume>），其父目录才是角色根
                            _kd = os.path.normpath(os.path.abspath(_k))
                            if os.path.dirname(_kd) == _rr:
                                _cn = os.path.basename(_kd)
                                _used_costumes.add(_cn)
                                _used_by_costume[_cn] = _v
                    rel_ref = _copy_cubism2_model(path, copy_map["figure"],
                                                  stats, warn, used=used,
                                                  used_costumes=_used_costumes,
                                                  used_by_costume=_used_by_costume)
                    if rel_ref:
                        stats["model_dirs"] = stats.get("model_dirs", 0) + 1
                        mdir = os.path.join(path, "model.json")
                        info = _parse_cubism2_json(mdir)
                        lines = []
                        y = _f(lay.get("y"), 0.5)
                        if abs(y - 0.5) > Y_DEV:
                            lines.append(_note(
                                "提示：该立绘纵向位置 y=%.2f 需在 "
                                "WebGAL 编辑器中手动调整" % y))
                        line = "changeFigure:%s/model.json -%s" % (rel_ref, slot)
                        # K1（task-69）平移：非三分位 → 角色稳定 -id + -transform
                        _fid_c2 = _fig_id(str(rel_ref).split("/")[0])
                        _use_fid = _fid_c2 if not _is_slot_x(lay.get("x")) \
                            else None
                        if _use_fid:
                            line = ("changeFigure:%s/model.json -id=%s "
                                    "-transform=%s"
                                    % (rel_ref, _use_fid,
                                       _transform_of(lay, W, H)))
                        # motion/expression 与键名表三级匹配：
                        # 精确 > 后缀 > 模糊；命中写真实键名，未命中不写选项
                        # 并注释可用名（前 8 个）。
                        mv = _match_motion_name(c.get("motion"),
                                                info["motions"])
                        ev = _match_motion_name(c.get("expression"),
                                                info["expressions"])
                        if mv:
                            line += " -motion=%s" % mv
                        elif c.get("motion"):
                            lines.append(_note(
                                "可用动作: %s" % ", ".join(info["motions"][:8])))
                        if ev:
                            line += " -expression=%s" % ev
                        elif c.get("expression"):
                            lines.append(_note(
                                "可用表情: %s" %
                                ", ".join(info["expressions"][:8])))
                        lines.append(line + ";")
                        lines.append(_note(
                            "WebGAL 运行时原生支持 Cubism2，"
                            "直接可玩（无需 .moc3 手工步骤）"))
                        events.append((t0, 1, seq, lines))
                        seq += 1
                        _exit_none(t1, slot, "%s/model.json" % rel_ref,
                                   _use_fid)
                        fig_intervals.append(
                            (t0, t1, slot, "%s/model.json" % rel_ref,
                             _use_fid, lay))
                    else:
                        warn.append("Cubism2 模型拷贝失败，clip 跳过: %s" % path)
                    continue
                # ---- 规则 5：puppet 渲首帧 -> figure ----
                slot = _slot(lay.get("x"))
                stem = safe_name(os.path.splitext(raw_name)[0]) or "puppet"
                nm = pool.alloc("figure", stem + ".png")
                fig_stem = os.path.splitext(nm)[0]   # 去重后的实名（hiyori_2 等）
                out_png = os.path.join(copy_map["figure"], nm)
                mode = _emit_puppet_png(c, path, out_png)
                if mode == "placeholder" and not os.path.isfile(path):
                    warn.append("puppet 素材不可达，用占位立绘: %s" % (path or "(空)"))
                elif mode == "pil":
                    warn.append("puppet 渲染失败，退 PIL 原图: %s" % path)
                if mode != "placeholder" or os.path.isfile(out_png):
                    stats["files"] += 1
                extra = None
                hint_dir = _cubism_hint_dir(path)
                if hint_dir is not None and not cubism_seen:
                    cubism_seen = True
                    try:
                        guide_src = os.path.join(hint_dir, "Cubism导入指引.txt")
                        with open(os.path.join(out_dir, "Cubism导入指引.txt"),
                                  "w", encoding="utf-8") as gf:
                            if os.path.isfile(guide_src):
                                with open(guide_src, "r", encoding="utf-8",
                                          errors="ignore") as gs:
                                    gf.write(gs.read())
                            else:
                                gf.write(_CUBISM_GUIDE)
                    except Exception as e:
                        warn.append("Cubism指引写出失败: %r" % e)
                if hint_dir is not None:
                    extra = [_note("完成 .moc3 后可改用 "
                                   "changeFigure:%s/%s.model3.json "
                                   "-motion=idle -expression=xx"
                                   % (fig_stem, fig_stem))]
                figure_event(c, t0, t1, lay, nm, slot, extra=extra,
                             fid=_fig_id(_who_export(path)))
            elif sk == "video":
                # ---- 规则 4 ----
                nm = pool.alloc("video", safe_name(raw_name) or "clip.mp4")
                if not copy_in("video", path, nm):
                    warn.append("视频素材缺失，已跳过: %s" % (raw_name or path))
                    continue
                events.append((t0, 2, seq, ["video:%s -next;" % nm]))
                seq += 1
            elif sk == "audio" and tr_kind == "audio":
                # ---- 规则 3 ----
                nm = pool.alloc("bgm", safe_name(raw_name) or "a.mp3")
                if not copy_in("bgm", path, nm):
                    warn.append("音频素材缺失，已跳过: %s" % (raw_name or path))
                    continue
                if dur >= AUDIO_BGM_MIN:
                    if first_bgm is None:
                        first_bgm = nm
                    events.append((t0, 3, seq, ["bgm:%s;" % nm]))
                else:
                    events.append((t0, 3, seq, ["playEffect:%s;" % nm]))
                seq += 1
            else:
                warn.append("忽略无法映射的 clip: track=%s source.kind=%r id=%r"
                            % (tr.get("id"), sk, c.get("id")))

    # ---- 规则 10：台词表 dialogue -> 台词行（+ H2a 台词级动作切换行） ----
    _ref_keys_cache = {}

    def _ref_keys(ref):
        """导出包里某个立绘引用的动作/表情键名表。

        ref 形如 "soyo/casual-2023/model.json"（相对 figure/ 根）。
        **读的是导出后的 model.json**（已瘦身 + 已补 default），所以键表
        与包里实际存在的文件严格一致 —— 台词级动作据此校验，引用的一定
        存在。非 model.json 引用（png 立绘）返回 None（无表可校验）。

        注意：台词循环在 clip 循环**之后**跑，此时 model.json 已落盘。
        """
        r = str(ref or "").replace("\\", "/")
        if not r.lower().endswith(".json"):
            return None
        if r in _ref_keys_cache:
            return _ref_keys_cache[r]
        p = os.path.join(copy_map["figure"], *r.split("/"))
        info = None
        try:
            if os.path.isfile(p):
                info = _parse_cubism2_json(p)
        except Exception:
            info = None
        _ref_keys_cache[r] = info
        return info

    def _dialogue_figure(t, who):
        """台词动作落点：时刻 t 的生效立绘 -> (slot, ref) 或 None。

        匹配优先：ref 文件名 stem / model.json 的父目录名（角色）与 who
        精确(3) > 互相包含(2) > who 出现在 ref(1)；单条生效立绘无名字线索
        也采用（单角色常见情形）；多条且全不命中 -> None（歧义不猜，
        调用方记「台词动作无落点」警告）。"""
        act = [iv for iv in fig_intervals if iv[0] <= t < iv[1]]
        if not act:
            return None

        def _score(iv):
            if not who:
                return 0
            ref = str(iv[3]).replace("\\", "/")
            fn = ref.rstrip("/").split("/")[-1]
            base = fn.rsplit(".", 1)[0] if "." in fn else fn
            parent = ref.rstrip("/").split("/")[-2].lower() \
                if "/" in ref.rstrip("/") else ""
            wl = who.lower()
            if wl in (base.lower(), parent):
                return 3
            for c in (base.lower(), parent):
                if c and (wl in c or c in wl):
                    return 2
            return 1 if wl in ref.lower() else 0

        if len(act) == 1:
            return act[0][2], act[0][3], act[0][4], act[0][5]
        best, bi = -1, -1
        for i, iv in enumerate(act):
            s = _score(iv)
            if s > best:
                best, bi = s, i
        if best <= 0:
            return None
        return act[bi][2], act[bi][3], act[bi][4], act[bi][5]

    for d in (proj.get("dialogue") or []):
        if not isinstance(d, dict):
            warn.append("忽略非法 dialogue 条目: %r" % (d,))
            continue
        who = re.sub(r"[:;\s]", "", str(d.get("who") or ""))
        text = str(d.get("text") or "").replace(";", " ")
        text = re.sub(r"[\r\n]+", " ", text).strip()
        if not text:
            warn.append("忽略空台词条目: %r" % (d,))
            continue
        t = _f(d.get("t"))
        mo = d.get("motion") if isinstance(d.get("motion"), str) and \
            d.get("motion") else None
        ex = d.get("expression") if isinstance(d.get("expression"), str) \
            and d.get("expression") else None
        if mo or ex:
            hit = _dialogue_figure(t, who)
            if hit is None:
                warn.append("台词动作无落点: %s @%.2fs" % (who or "旁白", t))
            else:
                slot, ref, fid, dlay = hit
                key = (ref, mo or "", ex or "")
                if dialogue_prev_action.get(slot) != key:
                    # K1：平移态角色——切换行沿用该角色 -id 句柄与位置
                    # （绝不裸 changeFigure：无槽位默认 center 会顶人）
                    if fid:
                        line = "changeFigure:%s -id=%s -transform=%s" % (
                            ref, fid, _transform_of(dlay, W, H))
                    else:
                        line = "changeFigure:%s -%s" % (ref, slot)
                    # 台词级动作也要**校验键名**：这里写的是原样字符串，
                    # 若工程里存的是 "soyo/default"（素材里真实存在，但
                    # 不是任何 model.json 的键名），引擎运行时会请求一个
                    # 并不存在的文件 -> 404 -> **人物动作卡顿**。
                    # clip 级走 _match_motion_name（匹配不到就不写选项），
                    # 台词级之前是裸写，两者口径不一致 —— 这里对齐。
                    _mo_out, _ex_out = mo, ex
                    _keys = _ref_keys(ref)
                    if _keys:
                        _m2 = _match_motion_name(mo, _keys["motions"])
                        _e2 = _match_motion_name(ex, _keys["expressions"])
                        if mo and not _m2:
                            _mo_out = None
                        if ex and not _e2:
                            _ex_out = None
                        # default 保底：键名表里没有但素材里有 -> 直接引用
                        # （_add_default_expression 已把它写进 model.json）
                        if mo and not _m2 and str(mo).endswith("/default"):
                            _mo_out = mo
                        if ex and not _e2 and str(ex).endswith("/default"):
                            _ex_out = ex
                    if _mo_out:
                        line += " -motion=%s" % _mo_out
                    if _ex_out:
                        line += " -expression=%s" % _ex_out
                    events.append((t, 3.5, seq, [line + ";"]))
                    seq += 1
                    dialogue_prev_action[slot] = key        # v3.5：台词正文里的「空格+减号」要转义——引擎用 / -/ 切参数，
        # 不转义的话 "A - B" 会被误切，B 段整段消失（实测真 bug）。
        events.append((t, 4, seq,
                       ["%s:%s;" % (who or "旁白", _escape_say_body(text))]))
        seq += 1

    # K1（task-69）：同屏人数上限撤销——原 I1 >3 警告扫描整体移除。

    # ---- I3（task-67）：工程级注释 comments -> 引擎合法形态 `;xxx;` ----
    # 导入端 `//`（DS 惯性）与 `;xxx;` 都收进载体，这里统一经 _comment()
    # 收口（体内 ASCII 分号/冒号转全角），同刻排最前（prio 0，段落头注释）。
    for cm in (proj.get("comments") or []):
        if not isinstance(cm, dict):
            continue
        ctxt = str(cm.get("text") or "").strip()
        if not ctxt:
            continue
        events.append((_f(cm.get("t")), -1, seq, [_comment(ctxt)]))
        seq += 1

    # ---- v3.5：画面特效 effects -> 原样 emit 回引擎指令 ----
    # 导入端把 setTransform/setTempAnimation 留档到 proj["effects"]（编辑器
    # 没有滤镜轨，不能落时间轴），导出时必须按时刻还原，否则「导入 → 导出」
    # 会静默丢掉 AI 写的全部滤镜。这些是**引擎原生指令**，不经 _comment()，
    # 原样直出（prio -0.5：排在注释之后、其他语句之前）。
    for ef in (proj.get("effects") or []):
        if not isinstance(ef, dict):
            continue
        cmd = str(ef.get("cmd") or "").strip()
        if not cmd:
            continue
        head = cmd.split(":", 1)[0].strip()
        if head not in ("setTransform", "setTempAnimation"):
            continue
        if ":" not in cmd:
            continue
        body = cmd.rstrip(";").strip()
        # 分号已被裁掉，补回语句结束符
        events.append((_f(ef.get("t")), -0.5, seq, [body + ";"]))
        seq += 1

    # ---- v3.4.4 闪烁修复：none 事件后向判定 ----
    # 引擎 changeFigure 的去重只在「同槽位同内容」时成立（ee=false -> 只换
    # 动作/表情，模型实例不重建）；一旦发出 none，槽位被清空，后续同角色
    # 的 changeFigure 必然 ee=true -> 卸载旧模型 + 重载 + 200ms 淡入 = 闪烁。
    #
    # 判定口径：**按（槽位, 角色）配对**。同角色的连续 clip（换动作）首尾
    # 相接时，clip 结束不是退场——角色还在，只是换了个动作，必须由新的
    # changeFigure 直接覆盖（同 key setStage 引擎自行替换实例）。
    # 只有「该槽位上、该角色之后不再出现」才发 none（真退场）。
    #
    # 为什么必须带槽位：none 是**槽位级**指令（changeFigure:none -center），
    # 它清空的是那个槽位。判定必须回答："清空这个槽位之后，这个角色还会
    # 回到这个槽位吗？"——如果角色只是换到了别的槽位，那它确实离开了这个
    # 槽位，none 应当保留（否则旧模型还占着槽位，新角色挤进来会重叠）。
    if _NONE_PENDING:
        # 收集所有真实 changeFigure：(时刻, ref, slot, id)
        _fig_events = []
        for _t, _p, _s, _ls in events:
            for _l in _ls:
                if _l.startswith("changeFigure:") and \
                        not _l.startswith("changeFigure:none"):
                    _m = re.match(
                        r"changeFigure:(\S+)\s+-(left|center|right)", _l)
                    if _m:
                        _fig_events.append((_t, _m.group(1), _m.group(2),
                                            None))
                        continue
                    # K1 平移形态：changeFigure:<ref> -id=<fid> [-transform=…]
                    _mi = re.match(
                        r"changeFigure:(\S+)\s+-id=(\S+?)(?:\s|$)", _l)
                    if _mi:
                        _fig_events.append((_t, _mi.group(1), None,
                                            _mi.group(2)))
        _keep = []
        for _ev in _NONE_PENDING:
            _t, _prio, _seq, _sl, _ref, _fid = _ev
            if _fid:
                # 平移态：按 id 判定"之后还回不回这个句柄"（与槽位无关）
                _again = any(t >= _t - 0.005 and i == _fid
                             for (t, _r, _s2, i) in _fig_events)
                if _again:
                    continue
                _keep.append((_t, _prio, _seq,
                              ["changeFigure:none -id=%s;" % _fid]))
                continue
            # 该角色之后还会回到**同一槽位**吗？
            # 用 >= ：同角色 clip 首尾相接时（上一 clip 结束点 == 下一 clip
            # 起点），新的 changeFigure 与 none **同一时刻**，那也是"继续
            # 在场"（只是换动作），不能判成退场。
            _same_slot_again = any(
                t >= _t - 0.005 and s == _sl and r == _ref
                for (t, r, s, _i) in _fig_events)
            if _same_slot_again:
                continue      # 同角色还会回这个槽位 -> 不是退场 -> 丢弃 none
            # 该角色不再回这个槽位：none 表示真退场，保留
            _keep.append((_t, _prio, _seq,
                          ["changeFigure:none -%s;" % _sl]))
        events.extend(_keep)

    # ---- 规则 6：事件排序输出 ----
    events.sort(key=lambda e: (round(_f(e[0]), 3), e[1], e[2]))
    stmt_lines = []
    for _, _, _, lines in events:
        stmt_lines.extend(lines)

    # ---- F2-L2：本幕的分支尾巴（choice / jumps / next）----
    # 只在该幕确有分支时追加；单幕工程（_ctx 为 None）完全不进这段，
    # 逐字节输出与旧版一致。
    # 注意：分支数据挂在**幕**上（ctx["act"]），不在传进来的 view 里——
    # 编排器给的是幕视图（tracks/dialogue），acts 键根本不在这层。
    act = ctx.get("act") if _ctx is not None else None
    tail, pseudo = _emit_branch_tail(act, ctx) if act else ([], [])
    if tail:
        if not stmt_lines:
            stmt_lines = list(_EMPTY_SCRIPT.splitlines())
        stmt_lines.extend(tail)

    if not stmt_lines:
        script = _EMPTY_SCRIPT
        stats["lines"] = 1
    else:
        script = "\n".join(stmt_lines) + "\n"
        stats["lines"] = stats.get("lines", 0) + sum(
            1 for l in stmt_lines
            if l and not l.startswith("//") and not l.startswith(";"))

    scene_path = os.path.join(game_root, "scene", _scene)
    with open(scene_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(script)
    # 伪幕（option.set 副作用片段）落在同目录的小文件
    for _pname, _plines in pseudo:
        with open(os.path.join(game_root, "scene", _pname), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("\n".join(_plines) + "\n")
        stats["files"] += 1

    # ---- config.txt（Key:Value;）----
    # F2-L2：多幕时只写一次（第一幕写完后 _ctx 标记，后续幕跳过）
    if not ctx.get("cfg_written"):
        gname = game_name or proj.get("name") or "木偶工作室导出"
        cfg = ["Game_name:%s;" % gname,
               "Game_key:%s;" % _rand_key(),
               "Title_img:%s;" % (first_bg or ""),
               "Title_bgm:%s;" % (first_bgm or ""),
               "Default_Language:zh_CN;"]
        with open(os.path.join(game_root, "config.txt"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("\n".join(cfg) + "\n")
        ctx["cfg_written"] = True
    ctx["first_bg"] = first_bg
    ctx["first_bgm"] = first_bgm
    ctx["cubism_seen"] = cubism_seen

    # F2-L2：多幕时运行时段（playable/皮肤/core）由编排器统一收尾，
    # 各幕只负责生成 scene 文件。
    if _ctx is not None:
        return True, "", stats

    # ---- 规则 11（E3）：playable=True -> 把运行时树拷入 out_dir ----
    if playable:
        try:
            import runtime_pack as RP
            rt_st = RP.status()
        except ImportError:
            return False, "未安装 WebGAL 运行时：runtime_pack 缺失", \
                {"files": 0, "lines": 0, "warnings": []}
        except Exception as e:
            return False, "playable 读取运行时状态失败: %r" % e, \
                {"files": 0, "lines": 0, "warnings": []}
        if not rt_st.get("installed"):
            return False, "未安装 WebGAL 运行时：请先在模板页导入压缩包或资产管理下载", \
                {"files": 0, "lines": 0, "warnings": []}
        rt_dir = rt_st.get("path")
        if rt_dir and os.path.isdir(rt_dir):
            for entry in sorted(os.listdir(rt_dir)):
                src = os.path.join(rt_dir, entry)
                dst = os.path.join(out_dir, entry)
                try:
                    if os.path.isdir(src):
                        if entry == "game":
                            # game/ 特殊：运行时的 game/ 含引擎必需资源
                            # （template/template.json、animation/animationTable.json、
                            # userStyleSheet.css 等），之前整目录跳过导致引擎启动
                            # 时三个 404。改为**只补缺失文件**（不覆盖导出
                            # 内容：scene/start.txt、config.txt、background/
                            # 等同名项必须保留我们的版本）。
                            _merge_missing(src, dst, warn)
                        else:
                            shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                    stats["files"] += 1
                except Exception as e:
                    warn.append("playable 拷贝失败 %s: %r" % (entry, e))
        stats["playable"] = True
        # v3.5.13：运行时自带 36MB 中文字体全部会在首屏下载（实测三个请求
        # 1093/1365/1770ms），换成预生成的子集（7.9MB）——见 _slim_runtime_fonts
        _slim_runtime_fonts(out_dir, warn, stats)
        # H1e（task-62）：file:// 双击黑屏根治——写自包含启动器（exe 优先/bat 兜底）
        lk, _ln = _inject_launcher(out_dir, warn)
        if lk:
            stats["launcher"] = lk
        # H1f（task-66）：版本哨兵（head 最顶，早于皮肤与 SW 注册）
        _inject_sentinel_playable(out_dir, warn, stats)
        # I2：可玩包皮肤注入（style=True 默认开）
        if style:
            if _inject_skin_playable(out_dir, _app_version(),
                                     _runtime_version(rt_st, rt_dir),
                                     live_badge, warn):
                stats["skin"] = "injected"
            else:
                stats["skin"] = "failed"
        else:
            stats["skin"] = "off"
        # H1c：包内有 Cubism2 模型才需要 Live2D core（纯背景台词包零变化）
        if stats.get("model_dirs", 0) > 0:
            _inject_live2d_core(out_dir, rt_dir, warn, stats, playable=True)
    elif style:
        # 非 playable：内容包皮肤以 muppet.css + start.txt 注释交付
        if _inject_skin_content_only(game_root, warn):
            stats["skin"] = "css-only"
    # H1c 非 playable：core 写 out_dir/lib/ + start.txt 手动补丁注释
    if not playable and stats.get("model_dirs", 0) > 0:
        _inject_live2d_core(out_dir, None, warn, stats, playable=False)

    msg = "已导出 WebGAL 工程到 %s（语句 %d，文件 %d，警告 %d%s）" % (
        game_root, stats["lines"], stats["files"], len(warn),
        "，含启动器(双击「启动游戏」打开)" if stats.get("playable") else "")
    return True, msg, stats


# ================= CLI =================
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="视频工程 -> WebGAL 4.5.x 游戏工程目录导出")
    ap.add_argument("--proj", required=True, help="video_edit 工程 JSON 路径")
    ap.add_argument("--out", required=True, help="WebGAL 工程输出目录")
    ap.add_argument("--name", default=None, help="游戏名（config Game_name）")
    ap.add_argument("--dialect", default="webgal", help="webgal（默认）或 mygo")
    ap.add_argument("--playable", action="store_true",
                    help="把本地 WebGAL 运行时一并拷入输出（需先导入运行时）")
    ap.add_argument("--no-style", action="store_true",
                    help="不注入 MyGO 风字幕皮肤（默认注入）")
    ap.add_argument("--live-badge", type=int, default=0,
                    help="左下「N人正在看」角标，0=关（默认）")
    a = ap.parse_args(argv)
    try:
        try:
            proj = _load_json_proj(a.proj)
        except FileNotFoundError:
            raise FileNotFoundError("工程文件不存在: %s" % a.proj)
        ok, msg, stats = export_game(proj, a.out,
                                     proj_dir=os.path.dirname(
                                         os.path.abspath(a.proj)) or None,
                                     game_name=a.name, dialect=a.dialect,
                                     playable=a.playable,
                                     style=not a.no_style,
                                     live_badge=a.live_badge)
        for w in stats.get("warnings", []):
            print("警告: %s" % w)
        if ok:
            print(msg)
            return 0
        print("导出失败: %s" % msg)
        return 1
    except NotImplementedError as e:
        print("导出失败: %s" % e)
        return 1
    except Exception as e:
        print("导出失败: %r" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
