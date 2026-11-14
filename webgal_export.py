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
    未命中返回 None。"""
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


def _copy_cubism2_model(model_dir, figure_root, stats, warn):
    """把 Cubism2 角色根整棵拷进 figure/，返回目标子目录名（相对 figure/）。

    - 角色根含 model.json 服装目录 + .chara/ + .mtn_exp/（隐藏目录
      shutil 默认会拷，dirs_exist_ok=True 幂等去重：同角色根多服装只拷一次）
    - 返回形如 "anon/school_summer-2023"（start.txt 里
      changeFigure:anon/school_summer-2023/model.json 引用）"""
    role_root = _cubism2_role_root(model_dir)
    rel = os.path.relpath(model_dir, role_root).replace("\\", "/")
    rel_root = os.path.relpath(role_root,
                               os.path.dirname(role_root)).replace("\\", "/")
    dst_root = os.path.join(figure_root, safe_name(rel_root) or "model")
    try:
        shutil.copytree(role_root, dst_root, dirs_exist_ok=True)
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


def _app_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "version.json"), encoding="utf8") as f:
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
_L2D_FILES = ("live2dcubismcore.min.js", "live2d.min.js")
_L2D_MARK = "muppet-live2d-core-v1"
_L2D_WARN = ("未找到 Live2D core js（专有许可官方不随包），人物将不显示——"
             "请设置 MUPPET_LIVE2D_CORE_DIR 指向含 live2d.min.js+"
             "live2dcubismcore.min.js 的目录")
_L2D_CACHE = {"dir": None, "searched": False}   # 命中即缓存；searched 防重复扫盘


def _l2d_valid(d):
    return bool(d) and all(os.path.isfile(os.path.join(d, f))
                           for f in _L2D_FILES)


def _find_live2d_core(rt_dir=None):
    """查找顺序：①env MUPPET_LIVE2D_CORE_DIR ②runtime 树 lib/（未来版本
    自带则直接用）③env MUPPET_WEBGAL_PACK_DIR（**限定**：设了就只扫它，
    同 COMFY_API 语义，测试隔离用）④默认整合包位（用户本地尽力扫描，
    路径硬编码仅为发现、不入库）。命中缓存模块级；env 优先于缓存。"""
    env = os.environ.get("MUPPET_LIVE2D_CORE_DIR", "").strip()
    if env and _l2d_valid(env):
        return env
    if rt_dir:
        d = os.path.join(rt_dir, "lib")
        if _l2d_valid(d):
            return d
    if _L2D_CACHE["dir"] and _l2d_valid(_L2D_CACHE["dir"]):
        return _L2D_CACHE["dir"]
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


def export_game(proj, out_dir, proj_dir=None, game_name=None, dialect="webgal",
                playable=False, style=True, live_badge=0):
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
    """
    if dialect == "mygo":
        raise NotImplementedError("MyGO 方言导出器 v3.5 提供")
    if dialect != "webgal":
        return False, "未知方言: %r（仅 webgal / mygo）" % (dialect,), \
               {"files": 0, "lines": 0, "warnings": []}
    if not isinstance(proj, dict):
        return False, "proj 必须是 dict（video_edit 工程结构）", \
               {"files": 0, "lines": 0, "warnings": []}

    stats = {"files": 0, "lines": 0, "warnings": []}
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

    pool = _NamePool()
    events = []                      # (t, prio, seq, [行, ...])
    seq = 0
    first_bg = None                  # config Title_img
    first_bgm = None                 # config Title_bgm
    cubism_seen = False
    # H2a：台词级动作的"落点"采集——每条立绘事件登记区间 (t0, t1, slot, ref)，
    # 台词带 motion/expression 时按时刻找回生效引用（见规则 10）。
    fig_intervals = []
    dialogue_prev_action = {}        # slot -> (ref, motion, expression) 连续去重
    copy_map = {"background": os.path.join(game_root, "background"),
                "figure": os.path.join(game_root, "figure"),
                "bgm": os.path.join(game_root, "bgm"),
                "video": os.path.join(game_root, "video")}

    def copy_in(cat, src_path, out_name):
        dst = os.path.join(copy_map[cat], out_name)
        try:
            if src_path and os.path.isfile(src_path):
                shutil.copyfile(src_path, dst)
                stats["files"] += 1
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

    def _exit_none(t1, slot):
        """显式离场时刻的 none 事件（收尾时刻跳过）。"""
        nonlocal seq
        if t1 < _proj_end - 0.005:
            events.append((t1, 1, seq, ["changeFigure:none -%s;" % slot]))
            seq += 1

    def figure_event(c, t0, t1, lay, out_name, slot, extra=None):
        nonlocal seq, cubism_seen
        lines = []
        y = _f(lay.get("y"), 0.5)
        if abs(y - 0.5) > Y_DEV:
            lines.append(_note("提示：该立绘纵向位置 y=%.2f 需在 WebGAL "
                               "编辑器中手动调整" % y))
        # C2：clip 带 motion/expression 时，changeFigure 行尾分号前追加参数
        # （puppet 与 image 立绘共用此函数，故都加）。
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
        _exit_none(t1, slot)
        fig_intervals.append((t0, t1, slot, out_name))

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
                if tr_kind == "video" and tr.get("id") == bottom_tid and \
                        (_is_bg_name(tr) or _is_fullscreen(lay, src, W, H)) \
                        and not _has_alpha(path):
                    # ---- 规则 1：背景 ----
                    # BG 命名轨直接是背景（WebGAL changeBg 为 cover 模式，
                    # 4:3 图进 16:9 画布也是合法背景，不做几何覆盖率否决）
                    nm = pool.alloc("background", safe_name(raw_name) or "bg.png")
                    copy_in("background", path, nm)
                    if first_bg is None:
                        first_bg = nm
                    events.append((t0, 0, seq, ["changeBg:%s -next;" % nm]))
                    seq += 1
                else:
                    # 非底轨/非满幅 image -> 立绘
                    slot = _slot(lay.get("x"))
                    nm = pool.alloc("figure", safe_name(raw_name) or "fig.png")
                    copy_in("figure", path, nm)
                    figure_event(c, t0, t1, lay, nm, slot)
            elif sk == "puppet":
                # ---- H1 task-43：Cubism2 模型目录分支（优先于渲染/Cubism4） ----
                # path 指向含 model.json 的目录 = Cubism2 整合包模型：
                # 整角色根拷进 figure/，changeFigure 引用 model.json。
                # WebGAL 运行时原生支持 Cubism2（lib\live2d.min.js），
                # 直接可玩——不写 Cubism导入指引.txt、不插 .moc3 注释。
                if _is_cubism2_dir(path):
                    slot = _slot(lay.get("x"))
                    rel_ref = _copy_cubism2_model(path, copy_map["figure"],
                                                  stats, warn)
                    if rel_ref:
                        stats["model_dirs"] = stats.get("model_dirs", 0) + 1
                        mdir = os.path.join(path, "model.json")
                        info = _parse_cubism2_json(mdir)
                        lines = []
                        y = _f(lay.get("y"), 0.5)
                        if abs(y - 0.5) > Y_DEV:
                            lines.append(_comment(
                                "提示：该立绘纵向位置 y=%.2f 需在 "
                                "WebGAL 编辑器中手动调整" % y))
                        line = "changeFigure:%s/model.json -%s" % (rel_ref, slot)
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
                            lines.append(_comment(
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
                        _exit_none(t1, slot)
                        fig_intervals.append(
                            (t0, t1, slot, "%s/model.json" % rel_ref))
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
                figure_event(c, t0, t1, lay, nm, slot, extra=extra)
            elif sk == "video":
                # ---- 规则 4 ----
                nm = pool.alloc("video", safe_name(raw_name) or "clip.mp4")
                copy_in("video", path, nm)
                events.append((t0, 2, seq, ["video:%s -next;" % nm]))
                seq += 1
            elif sk == "audio" and tr_kind == "audio":
                # ---- 规则 3 ----
                nm = pool.alloc("bgm", safe_name(raw_name) or "a.mp3")
                copy_in("bgm", path, nm)
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
            return act[0][2], act[0][3]
        best, bi = -1, -1
        for i, iv in enumerate(act):
            s = _score(iv)
            if s > best:
                best, bi = s, i
        if best <= 0:
            return None
        return act[bi][2], act[bi][3]

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
                slot, ref = hit
                key = (ref, mo or "", ex or "")
                if dialogue_prev_action.get(slot) != key:
                    line = "changeFigure:%s -%s" % (ref, slot)
                    if mo:
                        line += " -motion=%s" % mo
                    if ex:
                        line += " -expression=%s" % ex
                    events.append((t, 3.5, seq, [line + ";"]))
                    seq += 1
                    dialogue_prev_action[slot] = key
        events.append((t, 4, seq, ["%s:%s;" % (who or "旁白", text)]))
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

    # ---- 规则 6：事件排序输出 ----
    events.sort(key=lambda e: (round(_f(e[0]), 3), e[1], e[2]))
    stmt_lines = []
    for _, _, _, lines in events:
        stmt_lines.extend(lines)

    if not stmt_lines:
        script = _EMPTY_SCRIPT
        stats["lines"] = 1
    else:
        script = "\n".join(stmt_lines) + "\n"
        stats["lines"] = sum(1 for l in stmt_lines
                             if l and not l.startswith("//")
                             and not l.startswith(";"))

    scene_path = os.path.join(game_root, "scene", "start.txt")
    with open(scene_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(script)

    # ---- config.txt（Key:Value;）----
    gname = game_name or proj.get("name") or "木偶工作室导出"
    cfg = ["Game_name:%s;" % gname,
           "Game_key:%s;" % _rand_key(),
           "Title_img:%s;" % (first_bg or ""),
           "Title_bgm:%s;" % (first_bgm or ""),
           "Default_Language:zh_CN;"]
    with open(os.path.join(game_root, "config.txt"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write("\n".join(cfg) + "\n")

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
                if entry == "game":        # 跳过运行时自带 game/，导出内容层占位
                    continue
                src = os.path.join(rt_dir, entry)
                dst = os.path.join(out_dir, entry)
                try:
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                    stats["files"] += 1
                except Exception as e:
                    warn.append("playable 拷贝失败 %s: %r" % (entry, e))
        stats["playable"] = True
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
