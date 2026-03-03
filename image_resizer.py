#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片缩放工具 - 基于FFmpeg的图片缩放处理工具
支持单张对比预览、多张/文件夹批量处理
"""

import os
import sys
import subprocess
import threading
import configparser
import shutil
from pathlib import Path
from tkinter import *
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
import sv_ttk

# ─────────────────────────────────────────────
#  常量
# ─────────────────────────────────────────────
APP_NAME    = "图片缩放工具 v1.0"

# 修复单文件打包后配置保存到临时目录的BUG
# Nuitka --onefile 官方文档: sys.executable 指向临时解包目录，
# 只有 sys.argv[0] 保留用户启动时的原始 EXE 真实路径
if getattr(sys, 'frozen', False) or "__compiled__" in dir():
    # 打包后: 使用 sys.argv[0] 获取原始 EXE 所在的真实物理目录
    base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
else:
    # 源码运行所在目录
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
INI_FILE    = os.path.join(base_dir, "settings.ini")
IMG_EXTS    = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif")

DEFAULT_SETTINGS = {
    "ffmpeg_path"   : "ffmpeg",
    "output_dir"    : "",
    "width"         : "832",
    "height"        : "480",
    "scale_flags"   : "lanczos+accurate_rnd",
    "unsharp"       : "3:3:0.5:3:3:0.0",
    "output_format" : "png",
    "jpeg_quality"  : "95",
    "webp_quality"  : "95",
    "webp_preset"   : "photo",
    "avif_crf"      : "28",
    "avif_speed"    : "6",
    "use_unsharp"   : "1",
    "use_resize"    : "1",   # 0=仅压缩格式，1=同时调整分辨率
}

SCALE_FLAG_OPTIONS  = ["lanczos", "lanczos+accurate_rnd", "bicubic", "bilinear", "sinc"]
FORMAT_OPTIONS      = ["png", "jpg", "webp", "avif"]
PRESET_OPTIONS      = ["photo", "drawing", "default", "picture", "icon", "text"]


# ─────────────────────────────────────────────
#  INI 读写
# ─────────────────────────────────────────────
def load_settings():
    cfg = configparser.ConfigParser()
    s   = dict(DEFAULT_SETTINGS)
    if os.path.exists(INI_FILE):
        cfg.read(INI_FILE, encoding="utf-8")
        if cfg.has_section("settings"):
            for k in DEFAULT_SETTINGS:
                if cfg.has_option("settings", k):
                    s[k] = cfg.get("settings", k)
    return s


def save_settings(s: dict):
    cfg = configparser.ConfigParser()
    cfg["settings"] = s
    with open(INI_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)


# ─────────────────────────────────────────────
#  FFmpeg 调用
# ─────────────────────────────────────────────
def build_vf(s: dict) -> str:
    if s.get("use_resize", "1") == "1":
        scale = f"scale={s['width']}:{s['height']}:flags={s['scale_flags']}"
        if s.get("use_unsharp", "1") == "1" and s.get("unsharp"):
            return f"{scale},unsharp={s['unsharp']}"
        return scale
    else:
        # 仅转换格式，不改分辨率
        if s.get("use_unsharp", "1") == "1" and s.get("unsharp"):
            return f"unsharp={s['unsharp']}"
        return "copy"   # 无操作，ffmpeg 会找 -vf copy 出错，用下方 run_ffmpeg 处理


def build_output_args(s: dict, out_path: str) -> list:
    fmt  = s["output_format"]
    args = []
    if fmt == "jpg":
        # mjpeg quality: ffmpeg q:v 1(best)~31(worst), 从 quality% 转换
        q = max(1, int((100 - int(s["jpeg_quality"])) / 100 * 31))
        args = ["-c:v", "mjpeg", "-q:v", str(q)]
    elif fmt == "webp":
        args = ["-c:v", "libwebp", "-lossless", "0",
                "-quality", s["webp_quality"],
                "-preset", s["webp_preset"]]
    elif fmt == "avif":
        # libaom-av1 静态图片模式
        args = ["-c:v", "libaom-av1",
                "-crf", s["avif_crf"],
                "-b:v", "0",
                "-cpu-used", s["avif_speed"],
                "-still-picture", "1",
                "-f", "avif"]
    # png 默认无額外参数
    args.append(out_path)
    return args


def run_ffmpeg(ffmpeg: str, in_path: str, out_path: str, s: dict) -> tuple[bool, str]:
    """返回 (成功, 错误信息)"""
    vf  = build_vf(s)
    if vf == "copy":
        # 不需要任何滤镜，直接输出格式转换
        cmd = [ffmpeg, "-y", "-i", in_path] + build_output_args(s, out_path)
    else:
        cmd = [ffmpeg, "-y", "-i", in_path, "-vf", vf] + build_output_args(s, out_path)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return False, r.stderr[-500:]
        return True, ""
    except FileNotFoundError:
        return False, f"找不到 ffmpeg：{ffmpeg}"
    except subprocess.TimeoutExpired:
        return False, "处理超时"


# ─────────────────────────────────────────────
#  主窗口
# ─────────────────────────────────────────────
class App(Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1100x680")
        self.minsize(900, 580)

        # 应用 sv-ttk 现代暗色主题（必须在构建 UI 前调用）
        sv_ttk.set_theme("dark")

        self.settings  = load_settings()
        self._orig_pil  = None
        self._proc_pil  = None
        self._files     = []
        self._single_path  = None
        self._zoom           = 1.0
        self._pan_x          = 0
        self._pan_y          = 0
        self._drag_start     = None
        self._dragging_split = False
        self._is_dragging    = False
        self._hq_timer       = None

        self._build_ui()
        self._set_fonts()

        # Windows 深色标题栏（其他系统由 OS 主题控制）
        self.after(10, self._set_dark_titlebar)

    # ── 样式 ──────────────────────────────
    def _set_fonts(self):
        """sv-ttk 已应用主题，这里只覆盖字体"""
        st = ttk.Style(self)
        st.configure(".", font=("微软雅黑", 9))
        st.configure("TLabelframe.Label", font=("微软雅黑", 9, "bold"))

    def _set_dark_titlebar(self):
        """尝试将 Windows 标题栏改为深色（Win10 1809+ / Win11）"""
        try:
            import ctypes
            self.update()  # 确保窗口已创建
            # wm_frame() 返回实际框架窗口句柄（十六进制字符串）
            hwnd = int(self.wm_frame(), 16)
            val  = ctypes.c_int(1)
            for attr in (20, 19):  # 20=Win11, 19=Win10
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass   # Linux / macOS 忽略

    # ── 主界面布局 ──────────────────────────────
    def _build_ui(self):
        # 顶部工具栏
        toolbar = ttk.Frame(self)
        toolbar.pack(fill=X, padx=6, pady=4)
        ttk.Button(toolbar, text="📂 单张",    command=self._open_single).pack(side=LEFT, padx=2)
        ttk.Button(toolbar, text="📂 多张",    command=self._open_multi ).pack(side=LEFT, padx=2)
        ttk.Button(toolbar, text="📁 文件夹",  command=self._open_folder).pack(side=LEFT, padx=2)
        ttk.Separator(toolbar, orient=VERTICAL).pack(side=LEFT, padx=8, fill=Y, pady=2)
        ttk.Button(toolbar, text="▶ 开始处理", command=self._start_process).pack(side=LEFT, padx=2)
        # 最右：参数设置（最右）+ 关于
        self._settings_btn = ttk.Button(toolbar, text="⚙ 参数设置  ❯",
                                         command=self._toggle_settings)
        self._settings_btn.pack(side=RIGHT, padx=2)
        ttk.Button(toolbar, text="i 关于",
                   command=lambda: messagebox.showinfo("关于",
                       f"{APP_NAME}\n基于 FFmpeg + Pillow + sv-ttk")
                   ).pack(side=RIGHT, padx=2)

        # 主体：左(预览) + 右(设置)
        body = ttk.Frame(self)
        body.pack(fill=BOTH, expand=True, padx=6, pady=2)

        self._left  = ttk.Frame(body)
        self._left.pack(side=LEFT, fill=BOTH, expand=True, padx=(0,4))

        self._right = ttk.Frame(body, width=300)
        # 默认不显示（折叠状态）
        self._right.pack_propagate(False)
        self._settings_visible = False

        self._build_preview(self._left)
        self._build_settings(self._right)

        # 底部状态栏 + 进度
        bot = ttk.Frame(self)
        bot.pack(fill=X, padx=6, pady=4)
        self._progress = ttk.Progressbar(bot, mode="determinate")
        self._progress.pack(fill=X, pady=2)
        self._status = StringVar(value="就绪")
        ttk.Label(bot, textvariable=self._status, anchor=W).pack(fill=X)

    # ── 预览区 ────────────────────────────────
    def _build_preview(self, parent):
        self._nb = ttk.Notebook(parent)
        self._nb.pack(fill=BOTH, expand=True)

        # Tab1：单张对比
        self._tab_single = ttk.Frame(self._nb)
        self._nb.add(self._tab_single, text="单张对比")

        # Canvas用于对比滑块
        self._canvas = Canvas(self._tab_single, bg="#1c1c1c", highlightthickness=0)
        self._canvas.pack(fill=BOTH, expand=True)
        self._slider_x   = None
        self._slider_pct = DoubleVar(value=0.5)
        self._canvas.bind("<Configure>",       self._on_canvas_resize)
        self._canvas.bind("<ButtonPress-1>",   self._on_pan_start)
        self._canvas.bind("<B1-Motion>",       self._on_pan_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self._canvas.bind("<Double-Button-1>", self._reset_view)
        self._canvas.bind("<MouseWheel>",      self._on_zoom)   # Windows
        self._canvas.bind("<Button-4>",        self._on_zoom)   # Linux ↑
        self._canvas.bind("<Button-5>",        self._on_zoom)   # Linux ↓

        # 滑块只保留 Scale ，全宽无文字（标签画在 canvas 内部）
        sc_frame = ttk.Frame(self._tab_single)
        sc_frame.pack(fill=X, padx=4, pady=(2, 0))
        ttk.Scale(sc_frame, from_=0, to=1, variable=self._slider_pct,
                  orient=HORIZONTAL, command=lambda _: self._redraw_compare()
                  ).pack(fill=X, expand=True)

        # 按钮行（压缩内边距）
        btn_row = ttk.Frame(self._tab_single)
        btn_row.pack(fill=X, padx=4, pady=1)
        ttk.Button(btn_row, text="🔄 预览处理效果", command=self._preview_single).pack(side=LEFT, padx=2)
        ttk.Button(btn_row, text="💾 保存结果",   command=self._save_single   ).pack(side=LEFT, padx=2)
        ttk.Button(btn_row, text="⊙ 重置视图",      command=self._reset_view    ).pack(side=LEFT, padx=2)
        ttk.Label(btn_row, text="滚轮缩放 · 拖拽移动 · 双击重置",
                  foreground="#6c7086").pack(side=LEFT, padx=6)

        # 文件信息栏（压缩至最小）
        info_row = ttk.Frame(self._tab_single)
        info_row.pack(fill=X, padx=6, pady=0)
        self._file_info_var = StringVar(value="")
        ttk.Label(info_row, textvariable=self._file_info_var,
                  foreground="#89dceb", font=("微软雅黑", 8)).pack(side=LEFT)

        # Tab2：批量列表
        self._tab_batch = ttk.Frame(self._nb)
        self._nb.add(self._tab_batch, text="批量处理")

        list_frame = ttk.Frame(self._tab_batch)
        list_frame.pack(fill=BOTH, expand=True, padx=4, pady=4)
        sb = ttk.Scrollbar(list_frame)
        sb.pack(side=RIGHT, fill=Y)
        self._file_list = Listbox(list_frame, yscrollcommand=sb.set,
                                  bg="#2b2b2b", fg="#e0e0e0",
                                  selectbackground="#0067c0",
                                  selectforeground="#ffffff",
                                  font=("微软雅黑", 9), bd=0, highlightthickness=0)
        self._file_list.pack(fill=BOTH, expand=True)
        sb.config(command=self._file_list.yview)

        cnt_row = ttk.Frame(self._tab_batch)
        cnt_row.pack(fill=X, padx=4, pady=2)
        self._file_count = StringVar(value="未导入文件")
        ttk.Label(cnt_row, textvariable=self._file_count).pack(side=LEFT)
        ttk.Button(cnt_row, text="清空列表", command=self._clear_files).pack(side=RIGHT)

    # ── 设置面板 ──────────────────────────────
    def _build_settings(self, parent):
        canvas = Canvas(parent, bg="#1c1c1c", highlightthickness=0)
        vsb    = ttk.Scrollbar(parent, orient=VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=RIGHT, fill=Y)
        canvas.pack(side=LEFT, fill=BOTH, expand=True)

        inner = ttk.Frame(canvas)
        win   = canvas.create_window((0,0), window=inner, anchor="nw")

        def on_frame_conf(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(win, width=canvas.winfo_width())
        inner.bind("<Configure>", on_frame_conf)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))

        p = {"padx": 6, "pady": 3, "fill": X}

        # ── FFmpeg路径 ──
        frm = ttk.LabelFrame(inner, text="FFmpeg 设置")
        frm.pack(**{**p, "pady": (6, 2)})
        ttk.Label(frm, text="ffmpeg 路径").pack(anchor=W, padx=4)
        row = ttk.Frame(frm); row.pack(fill=X, padx=4, pady=2)
        self._ffmpeg_var = StringVar(value=self.settings["ffmpeg_path"])
        ttk.Entry(row, textvariable=self._ffmpeg_var).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(row, text="浏览", command=self._browse_ffmpeg, width=5).pack(side=RIGHT)

        # ── 输出目录 ──
        frm2 = ttk.LabelFrame(inner, text="输出目录")
        frm2.pack(**p)
        row2 = ttk.Frame(frm2); row2.pack(fill=X, padx=4, pady=2)
        self._outdir_var = StringVar(value=self.settings["output_dir"])
        ttk.Entry(row2, textvariable=self._outdir_var).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(row2, text="浏览", command=self._browse_outdir, width=5).pack(side=RIGHT)
        ttk.Label(frm2, text="留空则在原图目录创建 output_result 子文件夹",
                  foreground="#6c7086", font=("微软雅黑",8)).pack(anchor=W, padx=4, pady=(0,2))

        # ── 分辨率 ──
        frm3 = ttk.LabelFrame(inner, text="目标分辨率")
        frm3.pack(**p)

        # 启用开关行
        self._use_resize = IntVar(value=int(self.settings.get("use_resize", "1")))
        en_row = ttk.Frame(frm3); en_row.pack(fill=X, padx=4, pady=(4, 0))
        resize_cb = ttk.Checkbutton(en_row, text="启用分辨率调整",
                                    variable=self._use_resize,
                                    command=self._on_resize_toggle)
        resize_cb.pack(side=LEFT)
        ttk.Label(en_row, text="（不勾选=仅转换格式/压缩）",
                  foreground="#6c7086", font=("微软雅黑", 8)).pack(side=LEFT, padx=4)

        # 内部内容区域（可折叠）
        self._resize_body = ttk.Frame(frm3)
        self._resize_body.pack(fill=X)

        # 宽高输入行
        row3 = ttk.Frame(self._resize_body); row3.pack(fill=X, padx=4, pady=2)
        ttk.Label(row3, text="宽").pack(side=LEFT)
        self._w_var = StringVar(value=self.settings["width"])
        ttk.Entry(row3, textvariable=self._w_var, width=6).pack(side=LEFT, padx=4)
        ttk.Label(row3, text="高").pack(side=LEFT)
        self._h_var = StringVar(value=self.settings["height"])
        ttk.Entry(row3, textvariable=self._h_var, width=6).pack(side=LEFT, padx=4)

        # 横/竖屏切换按钮
        def _swap_wh():
            w, h = self._w_var.get(), self._h_var.get()
            self._w_var.set(h); self._h_var.set(w)

        orient_row = ttk.Frame(self._resize_body); orient_row.pack(fill=X, padx=4, pady=(0,2))
        self._orient = StringVar(value="landscape")
        def _set_orient(o):
            self._orient.set(o)
            w, h = self._w_var.get(), self._h_var.get()
            wi, hi = int(w) if w.isdigit() else 0, int(h) if h.isdigit() else 0
            if o == "landscape" and wi < hi:
                _swap_wh()
            elif o == "portrait" and wi > hi:
                _swap_wh()

        ttk.Radiobutton(orient_row, text="🖥 横屏", variable=self._orient,
                        value="landscape", command=lambda: _set_orient("landscape")
                        ).pack(side=LEFT, padx=4)
        ttk.Radiobutton(orient_row, text="📱 竖屏", variable=self._orient,
                        value="portrait",  command=lambda: _set_orient("portrait")
                        ).pack(side=LEFT, padx=4)
        ttk.Button(orient_row, text="⇄ 互换", command=_swap_wh
                   ).pack(side=LEFT, padx=4)

        # 分辨率快捷预设（自动跟随横/竖屏）
        def _apply_preset(lw, lh):
            if self._orient.get() == "portrait":
                self._w_var.set(lh); self._h_var.set(lw)
            else:
                self._w_var.set(lw); self._h_var.set(lh)

        presets = [("480P-Wan2.2","832","480"), ("720P","1280","720"), ("1080P","1920","1080")]
        row3b = ttk.Frame(self._resize_body); row3b.pack(fill=X, padx=4, pady=2)
        for name, w, h in presets:
            ttk.Button(row3b, text=name,
                       command=lambda ww=w, hh=h: _apply_preset(ww, hh)
                       ).pack(side=LEFT, padx=2)

        # 初始化横/竖屏状态
        try:
            wi = int(self.settings["width"]); hi = int(self.settings["height"])
            self._orient.set("portrait" if wi < hi else "landscape")
        except Exception:
            pass

        self._on_resize_toggle()   # 初始化显/隐

        # ── 缩放算法 ──
        frm4 = ttk.LabelFrame(inner, text="缩放算法")
        frm4.pack(**p)
        self._flags_var = StringVar(value=self.settings["scale_flags"])
        cb = ttk.Combobox(frm4, textvariable=self._flags_var,
                          values=SCALE_FLAG_OPTIONS, state="readonly")
        cb.pack(fill=X, padx=4, pady=2)

        # ── 锐化 ──
        frm5 = ttk.LabelFrame(inner, text="后期锐化（unsharp）")
        frm5.pack(**p)
        self._use_unsharp = IntVar(value=int(self.settings.get("use_unsharp","1")))
        ttk.Checkbutton(frm5, text="启用锐化", variable=self._use_unsharp).pack(anchor=W, padx=4)
        row5 = ttk.Frame(frm5); row5.pack(fill=X, padx=4, pady=2)
        ttk.Label(row5, text="参数").pack(side=LEFT)
        self._unsharp_var = StringVar(value=self.settings["unsharp"])
        ttk.Entry(row5, textvariable=self._unsharp_var).pack(side=LEFT, fill=X, expand=True, padx=4)

        presets_s = [("轻微","3:3:0.5:3:3:0.0"),("中等","5:5:0.8:5:5:0.0"),("强烈","5:5:1.5:5:5:0.0")]
        row5b = ttk.Frame(frm5); row5b.pack(fill=X, padx=4, pady=2)
        for name, val in presets_s:
            ttk.Button(row5b, text=name,
                       command=lambda v=val: self._unsharp_var.set(v)
                       ).pack(side=LEFT, padx=2)

        # ── 输出格式 ──
        frm6 = ttk.LabelFrame(inner, text="输出格式")
        frm6.pack(**p)
        self._fmt_var = StringVar(value=self.settings["output_format"])
        fmt_row = ttk.Frame(frm6); fmt_row.pack(fill=X, padx=4, pady=2)
        for fmt in FORMAT_OPTIONS:
            ttk.Radiobutton(fmt_row, text=fmt.upper(), variable=self._fmt_var,
                            value=fmt, command=self._on_fmt_change).pack(side=LEFT, padx=4)

        # — JPEG 参数区 —
        self._jpeg_frame = ttk.Frame(frm6)
        r_jpeg = ttk.Frame(self._jpeg_frame); r_jpeg.pack(fill=X, padx=4, pady=2)
        ttk.Label(r_jpeg, text="质量 (1-100)", width=11).pack(side=LEFT)
        self._jpeg_q_var = StringVar(value=self.settings["jpeg_quality"])
        ttk.Scale(r_jpeg, from_=1, to=100, variable=self._jpeg_q_var,
                  orient=HORIZONTAL).pack(side=LEFT, fill=X, expand=True, padx=4)
        ttk.Label(r_jpeg, textvariable=self._jpeg_q_var, width=4).pack(side=LEFT)
        # 快捷预设
        jp_pre = ttk.Frame(self._jpeg_frame); jp_pre.pack(fill=X, padx=4, pady=(0,2))
        for lbl, val in [("95-极佳","95"),("85-高质","85"),("75-标准","75"),("60-小文件","60")]:
            ttk.Button(jp_pre, text=lbl, command=lambda v=val: self._jpeg_q_var.set(v)).pack(side=LEFT, padx=2)

        # — WebP 参数区 —
        self._webp_frame = ttk.Frame(frm6)
        r_wq = ttk.Frame(self._webp_frame); r_wq.pack(fill=X, padx=4, pady=2)
        ttk.Label(r_wq, text="质量 (1-100)", width=11).pack(side=LEFT)
        self._webp_q_var = StringVar(value=self.settings["webp_quality"])
        ttk.Scale(r_wq, from_=1, to=100, variable=self._webp_q_var,
                  orient=HORIZONTAL).pack(side=LEFT, fill=X, expand=True, padx=4)
        ttk.Label(r_wq, textvariable=self._webp_q_var, width=4).pack(side=LEFT)
        r_wp = ttk.Frame(self._webp_frame); r_wp.pack(fill=X, padx=4, pady=(0,2))
        ttk.Label(r_wp, text="Preset", width=11).pack(side=LEFT)
        self._webp_preset_var = StringVar(value=self.settings["webp_preset"])
        ttk.Combobox(r_wp, textvariable=self._webp_preset_var,
                     values=PRESET_OPTIONS, state="readonly", width=10).pack(side=LEFT, padx=4)

        # — AVIF 参数区 —
        self._avif_frame = ttk.Frame(frm6)
        r_ac = ttk.Frame(self._avif_frame); r_ac.pack(fill=X, padx=4, pady=2)
        ttk.Label(r_ac, text="CRF (0-63)", width=11).pack(side=LEFT)
        self._avif_crf_var = StringVar(value=self.settings["avif_crf"])
        ttk.Scale(r_ac, from_=0, to=63, variable=self._avif_crf_var,
                  orient=HORIZONTAL).pack(side=LEFT, fill=X, expand=True, padx=4)
        ttk.Label(r_ac, textvariable=self._avif_crf_var, width=4).pack(side=LEFT)
        ttk.Label(self._avif_frame,
                  text="CRF 越小=质量越好文件越大；推荐 20-35",
                  foreground="#6c7086", font=("微软雅黑",8)).pack(anchor=W, padx=4)
        r_asp = ttk.Frame(self._avif_frame); r_asp.pack(fill=X, padx=4, pady=2)
        ttk.Label(r_asp, text="Speed (0-8)", width=11).pack(side=LEFT)
        self._avif_spd_var = StringVar(value=self.settings["avif_speed"])
        ttk.Scale(r_asp, from_=0, to=8, variable=self._avif_spd_var,
                  orient=HORIZONTAL).pack(side=LEFT, fill=X, expand=True, padx=4)
        ttk.Label(r_asp, textvariable=self._avif_spd_var, width=4).pack(side=LEFT)
        ttk.Label(self._avif_frame,
                  text="Speed 越小=编码越慢质量越好；推荐 4-6",
                  foreground="#6c7086", font=("微软雅黑",8)).pack(anchor=W, padx=4)
        # AVIF 快捷
        avif_pre = ttk.Frame(self._avif_frame); avif_pre.pack(fill=X, padx=4, pady=(0,2))
        for lbl, crf, spd in [("20-极佳","20","4"),("28-推荐","28","6"),("35-小文件","35","7")]:
            ttk.Button(avif_pre, text=lbl,
                       command=lambda c=crf, s=spd: (self._avif_crf_var.set(c), self._avif_spd_var.set(s))
                       ).pack(side=LEFT, padx=2)

        self._on_fmt_change()

        # ── 按钮 ──
        btn_frame = ttk.Frame(inner)
        btn_frame.pack(**{**p, "pady": 6})
        ttk.Button(btn_frame, text="💾 保存设置",    command=self._save_settings_ui).pack(fill=X, pady=2)
        ttk.Button(btn_frame, text="↺ 恢复默认设置", command=self._reset_settings  ).pack(fill=X, pady=2)

    # ── 参数面板折叠 ──────────────────────────
    def _toggle_settings(self):
        if self._settings_visible:
            self._right.pack_forget()
            self._settings_btn.config(text="⚙ 参数设置  ❯")
        else:
            self._right.pack(side=RIGHT, fill=Y)
            self._settings_btn.config(text="⚙ 参数设置  ❮")
        self._settings_visible = not self._settings_visible

    # ── 分辨率开关 ─────────────────────
    def _on_resize_toggle(self):
        if self._use_resize.get():
            self._resize_body.pack(fill=X)
        else:
            self._resize_body.pack_forget()

    # ── 格式切换 ─────────────────────────
    def _on_fmt_change(self):
        fmt = self._fmt_var.get()
        # 全部隐藏再按需显示
        for f in (self._jpeg_frame, self._webp_frame, self._avif_frame):
            f.pack_forget()
        if fmt == "jpg":
            self._jpeg_frame.pack(fill=X, padx=4, pady=2)
        elif fmt == "webp":
            self._webp_frame.pack(fill=X, padx=4, pady=2)
        elif fmt == "avif":
            self._avif_frame.pack(fill=X, padx=4, pady=2)

    # ── 浏览按钮 ─────────────────────────────
    def _browse_ffmpeg(self):
        p = filedialog.askopenfilename(title="选择ffmpeg可执行文件",
                                       filetypes=[("可执行文件","*.exe ffmpeg"),("全部","*")])
        if p: self._ffmpeg_var.set(p)

    def _browse_outdir(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d: self._outdir_var.set(d)

    # ── 设置读写 ─────────────────────────────
    def _collect_settings(self) -> dict:
        s = dict(self.settings)
        s["ffmpeg_path"]   = self._ffmpeg_var.get().strip()
        s["output_dir"]    = self._outdir_var.get().strip()
        s["width"]         = self._w_var.get().strip()
        s["height"]        = self._h_var.get().strip()
        s["scale_flags"]   = self._flags_var.get().strip()
        s["use_unsharp"]   = str(self._use_unsharp.get())
        s["unsharp"]       = self._unsharp_var.get().strip()
        s["output_format"] = self._fmt_var.get()
        # 各格式独立参数
        s["jpeg_quality"]  = str(int(float(self._jpeg_q_var.get())))
        s["webp_quality"]  = str(int(float(self._webp_q_var.get())))
        s["webp_preset"]   = self._webp_preset_var.get()
        s["avif_crf"]      = str(int(float(self._avif_crf_var.get())))
        s["avif_speed"]    = str(int(float(self._avif_spd_var.get())))
        s["use_resize"]    = str(self._use_resize.get())
        return s

    def _save_settings_ui(self):
        self.settings = self._collect_settings()
        save_settings(self.settings)
        self._status.set("✅ 设置已保存")

    def _reset_settings(self):
        if not messagebox.askyesno("确认", "恢复默认设置？"):
            return
        self.settings = dict(DEFAULT_SETTINGS)
        self._ffmpeg_var.set(self.settings["ffmpeg_path"])
        self._outdir_var.set(self.settings["output_dir"])
        self._w_var.set(self.settings["width"])
        self._h_var.set(self.settings["height"])
        self._flags_var.set(self.settings["scale_flags"])
        self._use_unsharp.set(int(self.settings["use_unsharp"]))
        self._unsharp_var.set(self.settings["unsharp"])
        self._fmt_var.set(self.settings["output_format"])
        self._jpeg_q_var.set(self.settings["jpeg_quality"])
        self._webp_q_var.set(self.settings["webp_quality"])
        self._webp_preset_var.set(self.settings["webp_preset"])
        self._avif_crf_var.set(self.settings["avif_crf"])
        self._avif_spd_var.set(self.settings["avif_speed"])
        self._use_resize.set(int(self.settings.get("use_resize", "1")))
        # 重置横/竖屏状态
        try:
            wi = int(self.settings["width"]); hi = int(self.settings["height"])
            self._orient.set("portrait" if wi < hi else "landscape")
        except Exception:
            pass
        self._on_resize_toggle()
        self._on_fmt_change()
        save_settings(self.settings)
        self._status.set("↺ 已恢复默认设置")

    # ── 文件导入 ─────────────────────────────
    def _open_single(self):
        p = filedialog.askopenfilename(
            title="选择图片",
            filetypes=[("图片文件"," ".join(f"*{e}" for e in IMG_EXTS)), ("全部","*")]
        )
        if not p: return
        self._load_single_image(p)
        self._nb.select(self._tab_single)

    def _open_multi(self):
        ps = filedialog.askopenfilenames(
            title="选择多张图片",
            filetypes=[("图片文件"," ".join(f"*{e}" for e in IMG_EXTS)), ("全部","*")]
        )
        if not ps: return
        self._files = list(ps)
        self._update_file_list()
        self._nb.select(self._tab_batch)

    def _open_folder(self):
        d = filedialog.askdirectory(title="选择图片文件夹")
        if not d: return
        self._files = [str(p) for p in Path(d).iterdir()
                       if p.suffix.lower() in IMG_EXTS]
        self._files.sort()
        self._update_file_list()
        self._nb.select(self._tab_batch)

    def _update_file_list(self):
        self._file_list.delete(0, END)
        for f in self._files:
            self._file_list.insert(END, os.path.basename(f))
        self._file_count.set(f"共 {len(self._files)} 张图片")

    def _clear_files(self):
        self._files = []
        self._file_list.delete(0, END)
        self._file_count.set("未导入文件")

    # ── 辅助函数 ──────────────────────────
    @staticmethod
    def _fmt_size(n_bytes: int) -> str:
        if n_bytes < 1024:
            return f"{n_bytes} B"
        elif n_bytes < 1024 ** 2:
            return f"{n_bytes/1024:.1f} KB"
        else:
            return f"{n_bytes/1024**2:.2f} MB"

    def _update_file_info(self, orig_bytes: int, out_bytes: int):
        """orig 和 out 单位均为字节"""
        ratio = (1 - out_bytes / orig_bytes) * 100 if orig_bytes else 0
        sign  = "⇓" if ratio >= 0 else "⇑"
        self._file_info_var.set(
            f"原文件：{self._fmt_size(orig_bytes)}  →  "
            f"处理后：{self._fmt_size(out_bytes)}  "
            f"{sign} {abs(ratio):.1f}%"
        )

    # ── 单张预览 ─────────────────────────────
    def _load_single_image(self, path: str):
        try:
            self._orig_pil    = Image.open(path).convert("RGB")
            self._proc_pil    = None
            self._single_path = path
            self._slider_pct.set(0.5)
            self._reset_view()
            orig_sz = os.path.getsize(path)
            self._file_info_var.set(
                f"原文件：{self._fmt_size(orig_sz)}  ({self._orig_pil.width}×{self._orig_pil.height}px)  ――  尚未预览"
            )
            self._status.set(f"已加载：{os.path.basename(path)}  ({self._orig_pil.width}×{self._orig_pil.height})  {self._fmt_size(orig_sz)}")
        except Exception as e:
            messagebox.showerror("错误", str(e))

    def _save_single(self):
        """保存单张处理结果（重新调用FFmpeg输出文件）"""
        if self._single_path is None:
            messagebox.showwarning("提示", "请先导入单张图片")
            return
        s   = self._collect_settings()
        fmt = s["output_format"]
        ext = "." + fmt
        init_dir  = s["output_dir"].strip() or os.path.dirname(self._single_path)
        init_file = Path(self._single_path).stem + ext
        out_path  = filedialog.asksaveasfilename(
            title="保存处理结果",
            initialdir=init_dir,
            initialfile=init_file,
            defaultextension=ext,
            filetypes=[("PNG","*.png"),("JPEG","*.jpg"),("WebP","*.webp"),("AVIF","*.avif"),("全部","*")]
        )
        if not out_path:
            return
        self._status.set("正在保存…")
        self.update_idletasks()
        ok, err = run_ffmpeg(s["ffmpeg_path"], self._single_path, out_path, s)
        if ok:
            orig_sz = os.path.getsize(self._single_path)
            out_sz  = os.path.getsize(out_path)
            self._update_file_info(orig_sz, out_sz)
            self._status.set(f"✅ 已保存：{out_path}")
            messagebox.showinfo("保存完成", f"已保存到：\n{out_path}\n\n"
                                f"原文件：{self._fmt_size(orig_sz)}  →  "
                                f"输出：{self._fmt_size(out_sz)}  "
                                f"(压缩 {(1-out_sz/orig_sz)*100:.1f}%)")
        else:
            self._status.set("⚠ 保存失败")
            messagebox.showerror("FFmpeg 错误", err)

    def _preview_single(self):
        if self._orig_pil is None:
            messagebox.showwarning("提示", "请先导入单张图片")
            return
        s = self._collect_settings()
        import tempfile
        # tmp_out 后缀必须匹配输出格式，否则 FFmpeg 会按扩展名判断容器，AVIF/JPG/WebP 认识不了 .png
        out_ext = "." + s["output_format"]
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tin:
            tmp_in = tin.name
        with tempfile.NamedTemporaryFile(suffix=out_ext, delete=False) as tout:
            tmp_out = tout.name
        try:
            self._orig_pil.save(tmp_in)
            ok, err = run_ffmpeg(s["ffmpeg_path"], tmp_in, tmp_out, s)
            if not ok:
                messagebox.showerror("FFmpeg 错误", err)
                return
            self._proc_pil = Image.open(tmp_out).convert("RGB")
            # 更新文件信息栏
            orig_sz = os.path.getsize(self._single_path) if self._single_path else 0
            out_sz  = os.path.getsize(tmp_out)
            self._update_file_info(orig_sz, out_sz)
            self._status.set(f"预览完成 → {self._proc_pil.width}×{self._proc_pil.height}")
        finally:
            for f in (tmp_in, tmp_out):
                try: os.unlink(f)
                except: pass
        self._slider_pct.set(0.5)
        self._redraw_compare()

    # ── 视图事件 ─────────────────────────────
    def _on_canvas_resize(self, e):
        self._redraw_compare()

    def _on_zoom(self, e):
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw <= 1: return
        factor = 1.15 if (getattr(e, 'delta', 0) > 0 or e.num == 4) else 1 / 1.15
        # 以鼠标为中心缩放
        self._pan_x = int((e.x - cw // 2) * (1 - factor) + self._pan_x * factor)
        self._pan_y = int((e.y - ch // 2) * (1 - factor) + self._pan_y * factor)
        self._zoom  = max(0.05, min(40.0, self._zoom * factor))
        # 快速渲染 + 200ms后高质量重绘
        self._is_dragging = True
        self._redraw_compare()
        if self._hq_timer:
            self.after_cancel(self._hq_timer)
        self._hq_timer = self.after(200, self._hq_redraw)

    def _hq_redraw(self):
        self._is_dragging = False
        self._hq_timer    = None
        self._redraw_compare()

    def _on_pan_start(self, e):
        cw = self._canvas.winfo_width()
        split_x = int(cw * self._slider_pct.get())
        # 点击分割线左右 22px 内 → 拖动分割线
        if abs(e.x - split_x) <= 22:
            self._dragging_split = True
        else:
            self._dragging_split = False
            self._drag_start     = (e.x, e.y, self._pan_x, self._pan_y)
        self._is_dragging = True

    def _on_pan_drag(self, e):
        if self._dragging_split:
            cw = self._canvas.winfo_width()
            if cw > 0:
                self._slider_pct.set(max(0.0, min(1.0, e.x / cw)))
            self._redraw_compare()
        elif self._drag_start:
            sx, sy, px0, py0 = self._drag_start
            self._pan_x = px0 + (e.x - sx)
            self._pan_y = py0 + (e.y - sy)
            self._redraw_compare()

    def _on_drag_end(self, e):
        self._is_dragging    = False
        self._dragging_split = False
        self._drag_start     = None
        self._redraw_compare()  # 释放后高质量重绘

    def _reset_view(self, e=None):
        self._zoom  = 1.0
        self._pan_x = 0
        self._pan_y = 0
        self._redraw_compare()

    def _redraw_compare(self):
        self._canvas.delete("all")
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return

        pct     = self._slider_pct.get()
        split_x = int(cw * pct)

        def _render(pil_img):
            """把 pil_img 以当前 zoom/pan 渲染到 cw×ch 画布上"""
            iw, ih  = pil_img.size
            base_s  = min(cw / iw, ch / ih)   # 适应画布的基础缩放
            s       = base_s * self._zoom
            nw      = max(1, int(iw * s))
            nh      = max(1, int(ih * s))
            # 拖动时用 BILINEAR（快），准确时用 LANCZOS（好）
            resample = (Image.Resampling.BILINEAR
                        if self._is_dragging else
                        Image.Resampling.LANCZOS)
            resized = pil_img.resize((nw, nh), resample)
            x0 = (cw - nw) // 2 + self._pan_x
            y0 = (ch - nh) // 2 + self._pan_y
            # 裁剪到画布范围
            cx0 = max(0, x0);  cy0 = max(0, y0)
            sx0 = max(0, -x0); sy0 = max(0, -y0)
            pw  = min(nw - sx0, cw - cx0)
            ph  = min(nh - sy0, ch - cy0)
            bg  = Image.new("RGB", (cw, ch), (28, 28, 28))
            if pw > 0 and ph > 0:
                bg.paste(resized.crop((sx0, sy0, sx0 + pw, sy0 + ph)), (cx0, cy0))
            return bg

        orig_surf  = _render(self._orig_pil) if self._orig_pil else \
                     Image.new("RGB", (cw, ch), (24, 24, 37))
        right_src  = self._proc_pil if self._proc_pil else self._orig_pil
        right_surf = _render(right_src) if right_src else \
                     Image.new("RGB", (cw, ch), (24, 24, 37))

        # 左右合成
        combined = orig_surf.copy()
        combined.paste(right_surf.crop((split_x, 0, cw, ch)), (split_x, 0))
        tk_img = ImageTk.PhotoImage(combined)
        self._canvas.create_image(0, 0, anchor=NW, image=tk_img)
        self._canvas._tk_img = tk_img   # 防GC

        # 分隔线 + 手柄
        self._canvas.create_line(split_x, 0, split_x, ch, fill="#0067c0", width=2)
        self._canvas.create_rectangle(split_x - 28, ch // 2 - 12,
                                       split_x + 28, ch // 2 + 12,
                                       fill="#2b2b2b", outline="#0067c0", width=1)
        self._canvas.create_text(split_x, ch // 2, text="◀  ▶",
                                  fill="#e0e0e0", font=("微软雅黑", 9, "bold"))

        # 顶部居中：缩放比例
        self._canvas.create_text(cw // 2, 8, anchor=N,
                                  text=f"🔍 {self._zoom * 100:.0f}%",
                                  fill="#909090", font=("微软雅黑", 9))

        # canvas 底部浮层：◄ 原图（左下） 处理后 ►（右下），对齐下方滑块
        self._canvas.create_text(8, ch - 6, anchor=SW,
                                  text="◄ 原图",
                                  fill="#5a9a6a", font=("微软雅黑", 9, "bold"))
        lbl = "处理后 ►" if self._proc_pil else "点击预览 ►"
        self._canvas.create_text(cw - 8, ch - 6, anchor=SE,
                                  text=lbl,
                                  fill="#9a5a5a", font=("微软雅黑", 9, "bold"))


    # ── 处理 ─────────────────────────────────
    def _start_process(self):
        s = self._collect_settings()
        cur_tab = self._nb.index(self._nb.select())

        if cur_tab == 0:  # 单张
            if self._orig_pil is None:
                messagebox.showwarning("提示", "请先导入单张图片"); return
            # 需要知道原文件路径——如果没有则先保存临时文件再处理并另存
            messagebox.showinfo("提示", "单张模式请使用「预览处理效果」查看，\n批量输出请切换到「批量处理」标签页导入文件")
            return
        else:  # 批量
            if not self._files:
                messagebox.showwarning("提示", "请先导入文件"); return
            threading.Thread(target=self._batch_run, args=(s,), daemon=True).start()

    def _batch_run(self, s: dict):
        files  = list(self._files)
        total  = len(files)
        errors = []

        self._progress.configure(maximum=total, value=0)

        for i, fpath in enumerate(files):
            self._status.set(f"处理 {i+1}/{total}：{os.path.basename(fpath)}")

            # 确定输出目录
            out_dir = s["output_dir"].strip()
            if not out_dir:
                out_dir = os.path.join(os.path.dirname(fpath), "output_result")
            os.makedirs(out_dir, exist_ok=True)

            ext     = "." + s["output_format"]
            out_name = Path(fpath).stem + ext
            out_path = os.path.join(out_dir, out_name)

            ok, err = run_ffmpeg(s["ffmpeg_path"], fpath, out_path, s)
            if not ok:
                errors.append(f"{os.path.basename(fpath)}: {err}")

            self._progress["value"] = i + 1

        if errors:
            msg = f"完成（{total - len(errors)}/{total} 成功），以下文件失败：\n" + "\n".join(errors[:10])
            messagebox.showerror("部分失败", msg)
            self._status.set(f"⚠ 完成，{len(errors)} 个失败")
        else:
            self._status.set(f"✅ 全部完成！共处理 {total} 张，输出：{out_dir}")
            messagebox.showinfo("完成", f"全部 {total} 张处理完成！\n输出目录：{out_dir}")


# ─────────────────────────────────────────────
#  启动
# ─────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.mainloop()
