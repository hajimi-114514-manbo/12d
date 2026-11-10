# -*- coding: utf-8 -*-
"""伪L2D 骨架工作室 V1.0 —— tkinter 窗体界面

左：图层列表（勾选显隐）
中：预览画布（可拖拽调支点；可拖入图片自动 SeeThrough 拆分）
右：骨架节点表（改父节点 / 改支点 / 看层）
下：播放控制 + 一键拆分 + 导出 PSD / AE 脚本 / JSON
"""
import os
import sys
import math
import json
import threading
import numpy as np
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import l2d_rig_core as C
import decompose

# 拖拽支持（tkinterdnd2 已装；万一缺失则退化为仅按钮选图）
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    DND_OK = True
except Exception:
    DND_OK = False

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
OUT_BASE = r"D:\视频\26114\seethrough_out"

STATE = {"dir": r"D:\视频\26114\anon_platform\seethrough",
         "meta": "layers_meta.json",
         "play": False, "t": 0.0, "show_pivots": True,
         "show_boxes": False, "scale": 0.45}


class Studio(TkinterDnD.Tk if DND_OK else tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("伪L2D 骨架工作室")
        self.geometry("1280x760")
        self.minsize(1080, 640)
        self._set_app_icon()

        self.rig = None
        self.layers = []
        self.W = self.H = 0
        self.photo = None
        self.drag = None
        self._busy = False
        self._pmsg = None          # worker -> UI 的进度消息（轮询取走）
        self._job = None           # 拆分任务状态，主线程轮询收尾

        self._build_ui()
        self.load_default()
        self._poll()

    def _set_app_icon(self):
        """窗口与任务栏图标（laowu_icon）"""
        ico = r"D:\work\puppet\l2d_rig\laowu_icon.ico"
        png = r"D:\work\puppet\l2d_rig\laowu_icon.png"
        try:
            if os.path.exists(ico):
                self.iconbitmap(ico)          # Windows 窗口图标
            if os.path.exists(png):
                img = Image.open(png).resize((32, 32), Image.LANCZOS)
                self._icon_photo = ImageTk.PhotoImage(img)
                self.iconphoto(True, self._icon_photo)   # 任务栏/Linux
        except Exception:
            pass                              # 图标失败不影响启动

    # ---------- UI ----------
    def _build_ui(self):
        self.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=3)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(2, weight=1)

        left = ttk.LabelFrame(self, text="图层")
        left.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        self.layer_box = tk.Listbox(left, selectmode="extended", exportselection=False)
        self.layer_box.grid(row=0, column=0, sticky="nsew")
        self.layer_box.bind("<Double-Button-1>", self._on_layer_sel)
        # 拖拽重排：按住图层上下拖动，松手生效（列表顶部=最前层）
        self._dl_idx = None
        self.layer_box.bind("<ButtonPress-1>", self._dl_press)
        self.layer_box.bind("<B1-Motion>", self._dl_motion)
        self.layer_box.bind("<ButtonRelease-1>", self._dl_release)
        ttk.Button(left, text="显示/隐藏全部", command=self._toggle_all).grid(row=1, column=0, sticky="ew")

        mid = ttk.Frame(self)
        mid.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        self.cv = tk.Canvas(mid, bg="#14161c", highlightthickness=0)
        self.cv.grid(row=0, column=0, sticky="nsew")
        self.cv.bind("<B1-Motion>", self._on_drag)
        self.cv.bind("<Button-1>", self._on_press)
        self.cv.bind("<Configure>", lambda e: self._redraw())
        bar = ttk.Frame(mid)
        bar.grid(row=1, column=0, sticky="ew")
        self.var_pivots = tk.BooleanVar(value=True)
        self.var_boxes = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="显示支点", variable=self.var_pivots,
                        command=self._redraw).pack(side="left", padx=4)
        ttk.Checkbutton(bar, text="显示包围盒", variable=self.var_boxes,
                        command=self._redraw).pack(side="left", padx=4)
        ttk.Button(bar, text="自动支点", command=self._auto_pivot).pack(side="right", padx=4)

        right = ttk.LabelFrame(self, text="骨架节点")
        right.grid(row=0, column=2, sticky="nsew", padx=4, pady=4)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        cols = ("node", "parent", "pivot", "layers")
        self.tree = ttk.Treeview(right, columns=cols, show="headings", selectmode="browse")
        for c, t, w in zip(cols, ("节点", "父节点", "支点(x,y)", "包含层"), (90, 70, 110, 150)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", self._edit_pivot)
        self.tree.bind("<<TreeviewSelect>>", self._on_node_sel)

        bottom = ttk.Frame(self)
        bottom.grid(row=1, column=0, columnspan=3, sticky="ew", padx=4, pady=4)
        self.btn_play = ttk.Button(bottom, text="播放", command=self._toggle_play)
        self.btn_play.pack(side="left", padx=4)
        ttk.Button(bottom, text="选择目录", command=self._pick_dir).pack(side="left", padx=4)
        self.btn_decomp = ttk.Button(bottom, text="拆分图片", command=self._on_decompose)
        self.btn_decomp.pack(side="left", padx=4)
        self.scrub = tk.Scale(bottom, from_=0, to=10, orient="horizontal",
                              command=self._on_scrub)
        self.scrub.pack(side="left", fill="x", expand=True, padx=8)
        self.time_lbl = ttk.Label(bottom, text="0.00s", width=8)
        self.time_lbl.pack(side="left", padx=4)
        ttk.Button(bottom, text="导出 PSD", command=self._export_psd).pack(side="right", padx=4)
        ttk.Button(bottom, text="导出 AE 脚本", command=self._export_ae).pack(side="right", padx=4)
        ttk.Button(bottom, text="导出 JSON", command=self._export_json).pack(side="right", padx=4)
        ttk.Button(bottom, text="保存预览", command=self._save_preview).pack(side="right", padx=4)
        ttk.Button(bottom, text="重新加载", command=self.load_default).pack(side="right", padx=4)

        # 进度条（拆分时显示）
        self.progress_lbl = ttk.Label(self, text="", anchor="w")
        self.progress_lbl.grid(row=2, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 4))

        # 拖拽：把图片拖进画布即拆分
        if DND_OK:
            self.cv.drop_target_register(DND_FILES)
            self.cv.dnd_bind("<<Drop>>", self._on_drop)
            self.cv.dnd_bind("<<DropEnter>>", self._on_drop_enter)
            self.cv.dnd_bind("<<DropLeave>>", self._on_drop_leave)

    # ---------- 一键拆分 ----------
    def _on_decompose(self):
        p = filedialog.askopenfilename(
            title="选择要拆分的图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff")])
        if p:
            self.start_decompose(p)

    def _on_drop(self, e):
        self.cv.configure(bg="#14161c")
        p = e.data.strip("{}")
        if p.lower().endswith(IMG_EXT):
            self.start_decompose(p)
        else:
            messagebox.showwarning("不支持的文件", "请拖入图片文件")

    def _on_drop_enter(self, e):
        if not self._busy:
            self.cv.configure(bg="#1f2a3a")

    def _on_drop_leave(self, e):
        self.cv.configure(bg="#14161c")

    def start_decompose(self, img_path):
        if self._busy:
            messagebox.showinfo("正在拆分", "请等待当前任务完成")
            return
        if not os.path.exists(img_path):
            messagebox.showerror("文件不存在", img_path)
            return
        self._busy = True
        self.btn_decomp.configure(state="disabled")
        self._set_progress("准备中…")
        out_dir = os.path.join(OUT_BASE, os.path.splitext(os.path.basename(img_path))[0])
        job = {"state": "running", "meta": None, "err": None, "out": out_dir}
        self._job = job

        def work():
            # 线程内绝不触碰任何 tkinter 对象（Windows 上会崩），只写普通变量
            try:
                job["meta"] = decompose.decompose(img_path, out_dir,
                                                  verbose=self._progress_from_thread)
                job["state"] = "done" if job["meta"] else "fail"
            except Exception as e:
                job["err"] = "%s: %s" % (type(e).__name__, e)
                job["state"] = "fail"

        threading.Thread(target=work, daemon=True).start()

    def _progress_from_thread(self, msg):
        self._pmsg = str(msg)          # 只赋值，由主线程 _poll 取走

    def _set_progress(self, text):
        self.progress_lbl.configure(text=text[-120:])

    def _poll(self):
        """主线程心跳：取进度消息 + 收尾已完成的拆分任务"""
        if self._pmsg is not None:
            m, self._pmsg = self._pmsg, None
            self._set_progress(m)
        j = self._job
        if j is not None and j["state"] != "running":
            self._job = None
            self._busy = False
            self.btn_decomp.configure(state="normal")
            if j["state"] == "done":
                STATE["dir"] = j["out"]
                STATE["meta"] = "layers_meta.json"
                self._set_progress("完成，已载入骨架")
                self.load_default()
                messagebox.showinfo("拆分完成", "已拆分层并生成骨架：\n%s" % j["out"])
            else:
                self._set_progress("拆分失败")
                messagebox.showerror("拆分失败", (j["err"] or "SeeThrough 未返回结果")[:500])
        self.after(200, self._poll)

    # ---------- 加载 ----------
    def _pick_dir(self):
        d = filedialog.askdirectory(initialdir=STATE["dir"])
        if d:
            STATE["dir"] = d
            STATE["meta"] = "layers_meta.json"
            self.load_default()

    def load_default(self):
        d = STATE["dir"]
        m = os.path.join(d, STATE["meta"])
        if not os.path.exists(m):
            messagebox.showerror("找不到元数据", m)
            return
        try:
            self.layers, self.W, self.H = C.load_layers(m, d)
        except Exception as e:
            messagebox.showerror("载入失败", str(e))
            return
        self.rig = C.build_rig(self.layers, self.W, self.H)
        # 列表顺序与 draw_order 对齐：顶部=最前层（draw_order 是远->近，取倒序）
        pos = {nm: k for k, nm in enumerate(self.rig["draw_order"])}
        self.layers.sort(key=lambda l: -pos.get(l["name"], 0))
        self.layer_box.delete(0, "end")
        self.visible = {}
        for l in self.layers:
            self.layer_box.insert("end", "● %s  [%s]" % (l["name"], l["tag"]))
            self.visible[l["name"]] = True
        self._fill_tree()
        self._redraw()
        print("载入 %d 层 %dx%d，节点 %d 个" % (len(self.layers), self.W, self.H, len(self.rig["nodes"])))

    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        for n in self.rig["nodes"]:
            self.tree.insert("", "end", iid=n["node"],
                             values=(n["node"], n["parent"],
                                     "%.0f,%.0f" % tuple(n["pivot"]),
                                     ", ".join(n["layers"])))

    # ---------- 绘制 ----------
    def _scale(self):
        return STATE["scale"]

    def _render_small(self, t):
        if self.rig is None:
            return None
        sc = self._scale()
        sw, sh = max(1, int(self.W * sc)), max(1, int(self.H * sc))
        img = C.render_frame(self.rig, t,
                             hidden={l["name"] for l in self.layers
                                     if not self.visible.get(l["name"], True)})
        if (img.shape[0], img.shape[1]) != (sh, sw):
            img = np.asarray(Image.fromarray(img).resize((sw, sh), Image.LANCZOS))
        return img

    def _redraw(self):
        if self.rig is None:
            return
        img = self._render_small(STATE["t"])
        cw = max(1, self.cv.winfo_width())
        ch = max(1, self.cv.winfo_height())
        sw, sh = img.shape[1], img.shape[0]
        pad = 10
        s = min((cw - 2 * pad) / sw, (ch - 2 * pad) / sh)
        s = max(s, 0.02)
        dw, dh = int(sw * s), int(sh * s)
        disp = np.asarray(Image.fromarray(img).resize((dw, dh), Image.LANCZOS))
        self.photo = ImageTk.PhotoImage(Image.fromarray(disp))
        ox = (cw - dw) // 2
        oy = (ch - dh) // 2
        self.cv.delete("all")
        self.cv.create_image(ox + dw // 2, oy + dh // 2, image=self.photo)
        k = dw / float(sw)
        if self.var_pivots.get():
            for n in self.rig["nodes"]:
                if n["node"] == "root":
                    continue
                x, y = n["pivot"]
                cx, cy = ox + x * k, oy + y * k
                self.cv.create_oval(cx - 7, cy - 7, cx + 7, cy + 7,
                                    outline="#ffd166", width=2, fill="#ff5c5c")
                self.cv.create_text(cx + 10, cy - 10, text=n["node"],
                                    fill="#ffd166", anchor="w")
        if self.var_boxes.get():
            for n in self.rig["nodes"]:
                al = n.get("_alpha")
                if al is None:
                    continue
                ys, xs = np.where(al > 0.5)
                if not len(xs):
                    continue
                x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
                self.cv.create_rectangle(ox + x0 * k, oy + y0 * k,
                                         ox + x1 * k, oy + y1 * k,
                                         outline="#5cc8ff", width=1)

    # ---------- 交互 ----------
    def _node_at(self, cx, cy):
        if self.rig is None or not self.photo:
            return None
        cw, ch = self.cv.winfo_width(), self.cv.winfo_height()
        sw, sh = self.photo.width(), self.photo.height
        pad = 10
        s = min((cw - 2 * pad) / sw, (ch - 2 * pad) / sh)
        ox, oy = (cw - sw * s) // 2, (ch - sh * s) // 2
        inv = 1.0 / s
        best, bestd = None, 14.0
        for n in self.rig["nodes"]:
            if n["node"] == "root":
                continue
            x, y = n["pivot"]
            px, py = ox + x * s, oy + y * s
            d = math.hypot(px - cx, py - cy)
            if d < bestd:
                bestd, best = d, n["node"]
        return best

    def _on_press(self, e):
        n = self._node_at(e.x, e.y)
        self.drag = n
        if n:
            self.tree.selection_set(n)
            self._on_node_sel()

    def _on_drag(self, e):
        if not self.drag:
            return
        n = next(x for x in self.rig["nodes"] if x["node"] == self.drag)
        cw, ch = self.cv.winfo_width(), self.cv.winfo_height()
        sw, sh = self.photo.width(), self.photo.height
        pad = 10
        s = min((cw - 2 * pad) / sw, (ch - 2 * pad) / sh)
        ox, oy = (cw - sw * s) // 2, (ch - sh * s) // 2
        x = (e.x - ox) / s
        y = (e.y - oy) / s
        n["pivot"] = [max(0, min(self.W, x)), max(0, min(self.H, y))]
        self.tree.set(self.drag, "pivot", "%.0f,%.0f" % tuple(n["pivot"]))
        self._redraw()

    def _auto_pivot(self):
        # 重新按接触点/包围盒算法自动定位所有支点
        for n in self.rig["nodes"]:
            ls = [l for l in self.layers if l["node"] == n["node"]]
            if not ls:
                continue
            ua = np.maximum.reduce([l["alpha"] for l in ls])
            ys, xs = np.where(ua > 0.5)
            if not len(xs):
                continue
            bx0, bx1, by0, by1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
            if n["node"] in ("eye_left", "eye_right"):
                n["pivot"] = [(bx0 + bx1) / 2.0, (by0 + by1) / 2.0]
            elif n["node"] == "body":
                n["pivot"] = [(bx0 + bx1) / 2.0, float(by1)]
            else:
                par = n["parent"]
                pls = [l for l in self.layers if l["node"] == par]
                if pls:
                    p_ua = np.maximum.reduce([l["alpha"] for l in pls])
                    d = C._dist_to(p_ua)
                    m = ua > 0.5
                    idx = int(np.argmin(d[m]))
                    ys2, xs2 = np.where(m)
                    n["pivot"] = [float(xs2[idx]), float(ys2[idx])]
        self._fill_tree()
        self._redraw()

    def _save_preview(self):
        if self.rig is None:
            return
        img = C.render_frame(self.rig, STATE["t"])
        p = self._out("preview_t%.2f.png" % STATE["t"])
        Image.fromarray(img).save(p)
        messagebox.showinfo("已保存", p)

    def _edit_pivot(self, e):
        sel = self.tree.selection()
        if not sel:
            return
        n = next(x for x in self.rig["nodes"] if x["node"] == sel[0])
        txt = simple_ask(self, "改支点", "%s 的新支点 (x,y):" % n["node"],
                         "%.0f,%.0f" % tuple(n["pivot"]))
        if txt:
            try:
                a, b = [float(v) for v in txt.replace("，", ",").split(",")]
                n["pivot"] = [max(0, min(self.W, a)), max(0, min(self.H, b))]
                self._fill_tree()
                self._redraw()
            except Exception as ex:
                messagebox.showerror("格式错误", "要写成 x,y，例如 525,413")

    def _on_node_sel(self, e=None):
        """选中骨架节点时，高亮该节点包含的图层"""
        sel = self.tree.selection()
        if not sel or self.rig is None:
            return
        n = next((x for x in self.rig["nodes"] if x["node"] == sel[0]), None)
        if not n:
            return
        self.layer_box.selection_clear(0, "end")
        for i, l in enumerate(self.layers):
            if l["name"] in n["layers"]:
                self.layer_box.selection_set(i)

    def _dl_press(self, e):
        self._dl_idx = self.layer_box.nearest(e.y)

    def _dl_motion(self, e):
        if self._dl_idx is None or self.rig is None:
            return
        i = self.layer_box.nearest(e.y)
        if 0 <= i < len(self.layers) and i != self._dl_idx:
            item = self.layers.pop(self._dl_idx)     # 整条字典一起移动
            self.layers.insert(i, item)
            self._dl_idx = i
            self._sync_order_from_list()
            # 每次移动都重建列表，保证视觉行号与数据行号一致
            self._refresh_layer_marks()
            self.layer_box.selection_clear(0, "end")
            self.layer_box.selection_set(i)
            self._redraw()

    def _dl_release(self, e):
        self._dl_idx = None
        self._refresh_layer_marks()

    def _sync_order_from_list(self):
        """列表顶部=最前层 -> draw_order 是它的倒序（远->近）"""
        if self.rig is not None:
            self.rig["draw_order"] = [l["name"] for l in reversed(self.layers)]

    def _on_layer_sel(self, e=None):
        """双击图层名切换该层显隐（单击只选中）"""
        if e is None:
            return
        i = self.layer_box.nearest(e.y)
        if 0 <= i < len(self.layers):
            nm = self.layers[i]["name"]
            self.visible[nm] = not self.visible.get(nm, True)
            self._refresh_layer_marks()
            self._redraw()

    def _refresh_layer_marks(self):
        """图层列表用前缀符号显示显隐状态"""
        sel = set(self.layer_box.curselection())
        self.layer_box.delete(0, "end")
        for i, l in enumerate(self.layers):
            mark = "●" if self.visible.get(l["name"], True) else "○"
            self.layer_box.insert("end", "%s %s  [%s]" % (mark, l["name"], l["tag"]))
            if i in sel:
                self.layer_box.selection_set(i)

    def _toggle_all(self):
        """一个按钮切换：有任一隐藏 -> 全显示；全可见 -> 全隐藏"""
        vals = [self.visible.get(k, True) for k in self.visible]
        target = not all(vals) if vals else True
        for k in self.visible:
            self.visible[k] = target
        self._refresh_layer_marks()
        self._redraw()

    # ---------- 播放 ----------
    def _toggle_play(self):
        STATE["play"] = not STATE["play"]
        self.btn_play.configure(text="暂停" if STATE["play"] else "播放")
        if STATE["play"]:
            self._tick()

    def _tick(self):
        if not STATE["play"]:
            return
        STATE["t"] = (STATE["t"] + 0.033) % 10.0
        self.scrub.set(STATE["t"])
        self.time_lbl.configure(text="%.2fs" % STATE["t"])
        self._redraw()
        self.after(33, self._tick)

    def _on_scrub(self, v):
        STATE["t"] = float(v)
        self.time_lbl.configure(text="%.2fs" % STATE["t"])
        self._redraw()

    # ---------- 导出 ----------
    def _out(self, name):
        return os.path.join(STATE["dir"], name)

    def _export_psd(self):
        try:
            C.export_psd(self.rig, self._out("rig_character.psd"))
            messagebox.showinfo("完成", "PSD 已导出：\n%s" % self._out("rig_character.psd"))
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _export_ae(self):
        C.export_ae_jsx(self.rig, self._out("rig_build.jsx"))
        messagebox.showinfo("完成", "AE 脚本已导出：\n%s" % self._out("rig_build.jsx"))

    def _export_json(self):
        C.export_json(self.rig, self._out("rig_spec.json"))
        messagebox.showinfo("完成", "骨架 JSON 已导出：\n%s" % self._out("rig_spec.json"))


def simple_ask(parent, title, prompt, default=""):
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.grab_set()
    ttk.Label(win, text=prompt).pack(padx=12, pady=(12, 4))
    var = tk.StringVar(value=default)
    e = ttk.Entry(win, textvariable=var)
    e.pack(padx=12, pady=4)
    e.focus_set()
    res = []

    def ok():
        res.append(var.get())
        win.destroy()

    ttk.Button(win, text="确定", command=ok).pack(pady=(4, 12))
    win.wait_window()
    return res[0] if res else None


if __name__ == "__main__":
    st = Studio()
    st.mainloop()
