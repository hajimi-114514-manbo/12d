# -*- coding: utf-8 -*-
"""model_thumb.py：渲染并缓存模型缩略图。

用户需求链：
  ① 「这个太惊悚了，换成组合完的图」—— texture atlas 是散件，不能用
  ② 「改成在软件内实时渲染，并且只渲染用户看得见的」
  ③ 「渲染完就保存稳定的那一帧，下次就直接放截图不用渲染了」

实现（全部基于对 templates/webgal/preview/stage_preview.html 的实测）：
  · 宿主页用成品模板，注入 __MUPPET_ES__ / __MUPPET_LIB2__ / __MUPPET_LIB4__
    三个 token + __muppetSend 桥
  · 命令入口 window.__muppetOnCmd(JSON string)
  · 抓图 window.__muppetGrab() -> dataURL（preserveDrawingBuffer 就是为它）
  · 状态 window.__muppetState() -> {booted,sdk2,modelInfo:{w,h},models:[...]}
  · 渲染成功存 <模型目录>/_thumb.png，之后直读文件、不再渲染

关键坑（都踩过，别回头）：
  · 引擎三前提：window.live2dPromise / window.renderPromiseResolve / #root
    → 成品模板已自带，别自己造宿主页
  · SDK 必须真能加载（路径映射错一个字符就报
    "Could not find Cubism 2 runtime"）
  · setTransform 吃**归一化**坐标（x*STAGE_W），scale 是相对 fit 的倍率

本模块不 import Qt：只给 HTML + 命令串；Qt 驱动在 video_workbench。
"""
import base64
import io
import json
import os

V_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.join(V_DIR, "templates", "webgal_runtime")
PREVIEW = os.path.join(V_DIR, "templates", "webgal", "preview")
TPL = os.path.join(PREVIEW, "stage_preview.html")
THUMB_NAME = "_thumb.png"
HOST_NAME = "_thumb_host.html"
# SDK 在页面里的相对前缀（server 要按这个映射到素材库 lib/）
LIBSQL = "libsql"


def runtime_es_rel():
    ad = os.path.join(RUNTIME, "assets")
    try:
        for fn in sorted(os.listdir(ad)):
            if fn.startswith("index.es-") and fn.endswith(".js"):
                return "assets/" + fn
    except OSError:
        pass
    return None


def lib_files():
    import asset_paths as AP
    try:
        root = AP.ensure_library(scan=False)
    except Exception:
        root = None
    if not root:
        return (None, None)
    d = os.path.join(root, "lib")
    a = os.path.join(d, "live2d.min.js")
    b = os.path.join(d, "live2dcubismcore.min.js")
    return (a if os.path.isfile(a) else None,
            b if os.path.isfile(b) else None)


def available():
    """能否渲染：模板 + 运行时 es + Cubism2 SDK 三者齐全。"""
    return (os.path.isfile(TPL) and bool(runtime_es_rel())
            and bool(lib_files()[0]))


def build_host_html():
    """生成宿主页（覆盖写），返回路径；不可用返回 None。"""
    es = runtime_es_rel()
    if not es or not os.path.isfile(TPL):
        return None
    l2, l4 = lib_files()
    html = io.open(TPL, encoding="utf-8").read()
    html = html.replace("__MUPPET_ES__", json.dumps("/" + es))
    html = html.replace("__MUPPET_LIB2__",
                        json.dumps("/%s/live2d.min.js" % LIBSQL if l2 else None))
    html = html.replace("__MUPPET_LIB4__",
                        json.dumps("/%s/live2dcubismcore.min.js" % LIBSQL
                                   if l4 else None))
    html = html.replace(
        "<head>",
        "<head><script>window.__muppetEvents=[];"
        "window.__muppetSend=function(s){"
        "try{window.__muppetEvents.push(s);}catch(e){}};</script>", 1)
    p = os.path.join(PREVIEW, HOST_NAME)
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(html)
    return p


def url_quote_path(rel):
    """把相对路径按段做 URL 编码（中文目录名必须编码，否则 fetch 报
    Network error；这是改模包模型全部渲染失败的根因）。"""
    import urllib.parse
    parts = str(rel or "").replace("\\", "/").split("/")
    return "/".join(urllib.parse.quote(p, safe="") for p in parts if p)


def model_json_rel(root, model_dir):
    """模型目录 -> 相对素材库根的 model.json 路径（**未编码**）。

    兼容两种布局：
      · 标准 Cubism2：  figure/<角色>/<服装>/model.json
      · 改模包：        figure/<角色>/<服装>/live2d/.../model.json
    """
    try:
        rel = os.path.relpath(os.path.join(model_dir, "model.json"), root)
    except Exception:
        return None
    rel = rel.replace(os.sep, "/")
    return rel if not rel.startswith("..") else None


def thumb_file(model_dir):
    return os.path.join(model_dir or "", THUMB_NAME) if model_dir else ""


def has_thumb(model_dir):
    p = thumb_file(model_dir)
    return bool(p) and os.path.isfile(p)


def del_thumb(model_dir):
    p = thumb_file(model_dir)
    try:
        if p and os.path.isfile(p):
            os.remove(p)
            return True
    except Exception:
        pass
    return False


def crop_png_bytes(raw, pad_ratio=0.03, dst_ratio=(3, 4)):
    """裁掉 PNG 的透明边，再按目标宽高比补成整齐画幅。

    · 找 alpha>8 的包围盒（角色实际占的区域）
    · 按 pad_ratio 留白
    · 补成 dst_ratio（默认 3:4 竖幅，正好配卡片）
    失败就原样返回（宁可不裁也不报错）。
    """
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(raw)).convert("RGBA")
        w, h = im.size
        bbox = im.getbbox()          # 基于 alpha 的包围盒
        if not bbox:
            return raw
        x0, y0, x1, y1 = bbox
        cw, ch = x1 - x0, y1 - y0
        if cw < 8 or ch < 8:
            return raw
        # 留白
        pw = int(cw * pad_ratio) + 2
        ph = int(ch * pad_ratio) + 2
        x0 = max(0, x0 - pw)
        y0 = max(0, y0 - ph)
        x1 = min(w, x1 + pw)
        y1 = min(h, y1 + ph)
        im = im.crop((x0, y0, x1, y1))
        cw, ch = im.size
        # 补成目标比例（居中，透明填充）
        tw, th = dst_ratio
        want = tw / float(th)
        have = cw / float(ch)
        if abs(have - want) > 0.02:
            if have > want:
                # 太宽 -> 加高
                nh = int(round(cw / want))
                canvas = Image.new("RGBA", (cw, nh), (0, 0, 0, 0))
                canvas.paste(im, (0, (nh - ch) // 2))
            else:
                # 太高 -> 加宽
                nw = int(round(ch * want))
                canvas = Image.new("RGBA", (nw, ch), (0, 0, 0, 0))
                canvas.paste(im, ((nw - cw) // 2, 0))
            im = canvas
        # 缩到合适尺寸（卡片 112x96 两倍屏够用）
        maxw = 360
        if im.width > maxw:
            im = im.resize((maxw, int(round(im.height * maxw / im.width))),
                           Image.LANCZOS)
        out = _io.BytesIO()
        im.save(out, "PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return raw


def save_data_url(data_url, model_dir, min_bytes=3000):
    """data:image/png;base64,... -> _thumb.png（太小的空白图丢弃）。"""
    if not data_url or not model_dir:
        return ""
    head = "data:image/png;base64,"
    if not str(data_url).startswith(head):
        return ""
    try:
        raw = base64.b64decode(str(data_url)[len(head):])
    except Exception:
        return ""
    if len(raw) < min_bytes:
        return ""
    # 裁掉透明边：否则角色在卡片里只有中间一小块（用户实报「显示太小」）
    raw = crop_png_bytes(raw)
    try:
        p = thumb_file(model_dir)
        with open(p, "wb") as f:
            f.write(raw)
        return p
    except Exception:
        return ""


def cmd_load(model_id, model_ref):
    """loadModel 命令（页面用 JSON.parse 收，所以传字符串）。

    model_ref 里的中文/空格必须 URL 编码，否则页面 fetch 报 Network error
    （实测：所有「改模包」模型都栽在这，因为它们目录名带中文）。
    已经是 http(s) 或已编码过的（无可编码字符）原样透传。
    """
    ref = str(model_ref or "")
    if ref and not ref.lower().startswith(("http://", "https://")):
        # 去掉前导 / 后按段编码，再补回前导 /
        lead = "/" if ref.startswith("/") else ""
        ref = lead + url_quote_path(ref.lstrip("/"))
    return json.dumps({"v": 1, "id": 1, "cmd": "loadModel",
                       "args": {"modelRef": ref, "modelId": model_id}},
                      ensure_ascii=False)


def cmd_transform(model_id, x=0.5, y=0.42, scale=1.0, rot=0.0):
    """setTransform：x/y 是归一化中心坐标，scale 是相对 fit 的倍率。"""
    return json.dumps({"v": 1, "id": 2, "cmd": "setTransform",
                       "args": {"modelId": model_id, "x": x, "y": y,
                                "scale": scale, "rot": rot}},
                      ensure_ascii=False)


def cmd_clear(model_id=""):
    return json.dumps({"v": 1, "id": 3, "cmd": "clearModel",
                       "args": {"modelId": model_id}}, ensure_ascii=False)
