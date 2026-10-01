#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作者信息卡片。

内容沿用原项目 Trae-workbuddyAssistant 的做法：
  * 常量以 XOR 混淆存储（不是明文），运行时解码；
  * 解码后拼串做 SHA-256 自检，被篡改时给出明确警告；
  * 侧栏底部入口 → 弹出卡片：作者名 / GitHub / QQ / 打赏。
"""
import base64
import hashlib
import os
import tkinter as tk
import webbrowser

from . import resources
from .widgets import button  # noqa: F401  （保持与其它 UI 模块一致的导入面）

# ===== 作者信息（XOR 混淆 + SHA-256 自检） =====
_K = "Gp2#x9Kv7QzLm4Tw"
_N = "DBxXShY="                                    # 作者名
_U = "LwRGUwsDZFlQOA4kGFZ6FCgdHWgUXCIY"            # GitHub 链接
_I = "DBxXShY="                                    # GitHub 用户名
_Q = "dUUFEk0NeEEGZTo9HBo3GCo="                    # 联系方式（QQ / 邮箱）
_S = ("b1f1d399c4c74c64ea52a592d76224cf41c2f583227496f47a6a0961543aab7d")


def _d(s):
    """XOR 解码：base64 → 逐字节异或密钥 → utf-8。"""
    raw = base64.b64decode(s)
    return bytes(b ^ ord(_K[i % len(_K)]) for i, b in enumerate(raw)).decode("utf-8")


AUTHOR_NAME = _d(_N)
AUTHOR_GITHUB = _d(_U)
AUTHOR_GITHUB_ID = _d(_I)
AUTHOR_QQ = _d(_Q)


def author_intact():
    """完整性自检：作者信息被篡改时返回 False。"""
    try:
        sig = "|".join([AUTHOR_NAME, AUTHOR_GITHUB, AUTHOR_GITHUB_ID, AUTHOR_QQ])
        return hashlib.sha256(sig.encode("utf-8")).hexdigest() == _S
    except Exception:  # noqa: BLE001
        return False


def show_author_modal(app, theme):
    """弹出作者卡片（模态）。"""
    from .widgets import alive

    win = tk.Toplevel(app)
    win.overrideredirect(True)
    win.configure(bg=theme.border)

    # 「可用桌面区」要先量（排除任务栏）——收款码较高，弹窗必须整卡落在其中，
    # 否则底部说明/二维码会被任务栏盖住。
    wl, wt, wr, wb = _work_area(win)
    avail_h = max(240, (wb - wt) - 24)
    # 非图片部分的大致高度（头部 + 两行信息 + 打赏标题/说明/内边距）。
    # 实测 336，留点余量；据此反推收款码最多能占多高，超了就等比缩小。
    _CHROME_H = 344
    max_img_h = max(120, avail_h - _CHROME_H)

    # 先给一个临时几何；内容建好后按实际所需高度自适应
    # （奖励作者收款码较高，写死高度会把它裁掉）
    w, h = 380, 420
    app.update_idletasks()
    x = app.winfo_rootx() + (app.winfo_width() - w) // 2
    y = app.winfo_rooty() + (app.winfo_height() - h) // 3
    win.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")

    outer = tk.Frame(win, bg=theme.bg_elev)
    outer.pack(fill="both", expand=True, padx=1, pady=1)

    # ---- 头部：头像 + 作者名 ----
    head = tk.Frame(outer, bg=theme.bg_elev)
    head.pack(fill="x", padx=18, pady=(18, 10))
    av = tk.Canvas(head, width=48, height=48, bg=theme.bg_elev,
                   highlightthickness=0, bd=0)
    av.pack(side="left")
    av.create_oval(0, 0, 47, 47, fill=theme.bg_muted, outline="")
    av.create_text(24, 24, text=AUTHOR_NAME[:1].upper(), fill=theme.fg,
                   font=(theme.font_family, 16, "bold"))

    info = tk.Frame(head, bg=theme.bg_elev)
    info.pack(side="left", padx=(12, 0))
    tk.Label(info, text=AUTHOR_NAME, font=(theme.font_family, 12, "bold"),
             fg=theme.fg, bg=theme.bg_elev, anchor="w").pack(anchor="w")
    tk.Label(info, text="本项目作者 · Developer", font=theme.f_tiny,
             fg=theme.fg_dim, bg=theme.bg_elev, anchor="w").pack(anchor="w")

    tk.Button(head, text="✕", command=win.destroy, font=theme.f_small,
              fg=theme.fg_dim, bg=theme.bg_elev, relief="flat",
              activebackground=theme.bg_hover, bd=0, cursor="hand2").pack(
        side="right")

    # ---- 信息行：GitHub / QQ ----
    body = tk.Frame(outer, bg=theme.bg_elev)
    body.pack(fill="both", expand=True, padx=18)

    def row(label, value, action=None, action_text=""):
        r = tk.Frame(body, bg=theme.bg_soft, highlightthickness=1,
                     highlightbackground=theme.border)
        r.pack(fill="x", pady=3)
        inner = tk.Frame(r, bg=theme.bg_soft)
        inner.pack(fill="x", padx=10, pady=8)
        tk.Label(inner, text=label, font=theme.f_tiny, fg=theme.fg_dim,
                 bg=theme.bg_soft, anchor="w").pack(anchor="w")
        line = tk.Frame(inner, bg=theme.bg_soft)
        line.pack(fill="x")
        tk.Label(line, text=value, font=theme.f_small, fg=theme.fg,
                 bg=theme.bg_soft, anchor="w").pack(side="left")
        if action:
            b = tk.Button(line, text=action_text, font=theme.f_tiny,
                          fg=theme.fg_muted, bg=theme.bg_soft, relief="flat",
                          activebackground=theme.bg_hover, bd=0, cursor="hand2",
                          command=action)
            b.pack(side="right")
        return r

    def open_github():
        try:
            webbrowser.open(AUTHOR_GITHUB)
        except Exception:  # noqa: BLE001
            app.toast("error", "无法打开浏览器")

    def copy_qq():
        try:
            win.clipboard_clear()
            win.clipboard_append(AUTHOR_QQ)
            app.toast("success", "联系方式已复制")
        except tk.TclError:
            app.toast("error", "复制失败")

    row("GitHub", AUTHOR_GITHUB_ID, action=open_github, action_text="打开 ↗")
    row("联系方式", AUTHOR_QQ, action=copy_qq, action_text="复制")

    # ---- 打赏 ----
    rw = tk.Frame(body, bg=theme.bg_soft, highlightthickness=1,
                  highlightbackground=theme.border)
    rw.pack(fill="x", pady=(10, 0))
    tk.Label(rw, text="奖励作者", font=theme.f_card_title, fg=theme.fg,
             bg=theme.bg_soft, anchor="w").pack(fill="x", padx=10, pady=(10, 2))

    reward = _reward_photo(win, max_h=max_img_h)
    if reward is not None:
        photo, _iw, _ih = reward
        img = tk.Label(rw, image=photo, bg=theme.bg_soft, bd=0,
                       highlightthickness=0, cursor="hand2")
        img.image = photo          # ★ 必须持有引用：局部变量出栈后 PhotoImage
        img.pack(pady=6)           #   被 GC 回收，图片会显示成空白
        img.bind("<Button-1>", lambda e: _open_reward_full(app))
        tk.Label(rw, text="如果这个工具帮到了你，可以请作者喝杯咖啡\n"
                          "（点击图片可放大）",
                 font=theme.f_tiny, fg=theme.fg_dim, bg=theme.bg_soft,
                 justify="center").pack(pady=(0, 10))
    else:
        # 资源缺失时的兜底：保留占位图案，不影响卡片可用
        qr = tk.Canvas(rw, width=120, height=120, bg=theme.bg_soft,
                       highlightthickness=0, bd=0)
        qr.pack(pady=6)
        qr.create_rectangle(2, 2, 118, 118, outline=theme.border, fill=theme.bg)
        for i in range(0, 116, 8):
            for j in range(0, 116, 8):
                if (i // 8 + j // 8) % 2 == 0:
                    qr.create_rectangle(i + 3, j + 3, i + 9, j + 9,
                                        fill=theme.fg_muted, outline="")
        qr.create_text(60, 60, text="收款码", fill=theme.bg,
                       font=(theme.font_family, 9, "bold"))
        tk.Label(rw, text="如果这个工具帮到了你，可以请作者喝杯咖啡",
                 font=theme.f_tiny, fg=theme.fg_dim,
                 bg=theme.bg_soft).pack(pady=(0, 10))

    # ---- 尺寸自适应：按内容实际高度定窗口，并在可用桌面区内定位 ----
    win.update_idletasks()
    w = max(w, outer.winfo_reqwidth())
    h = outer.winfo_reqheight()
    app.update_idletasks()
    x = app.winfo_rootx() + (app.winfo_width() - w) // 2
    y = app.winfo_rooty() + max(0, (app.winfo_height() - h) // 3)
    x = min(max(wl, x), max(wl, wr - w))
    y = min(max(wt, y), max(wt, wb - h))
    win.geometry(f"{w}x{h}+{x}+{y}")

    # ---- 完整性自检 ----
    if not author_intact():
        app.toast("error", "作者信息校验失败：检测到源码被篡改，请尊重开源作者")

    # 关闭方式：点外部 / Esc
    win.bind("<Escape>", lambda e: win.destroy())

    def on_doc_click(e):
        if not (0 <= e.x_root - win.winfo_rootx() <= w
                and 0 <= e.y_root - win.winfo_rooty() <= h):
            win.destroy()

    app.bind("<Button-1>", on_doc_click, add="+")
    win.bind("<Destroy>", lambda e: _unbind(app, on_doc_click))
    win.focus_force()
    return win


def _unbind(app, fn):
    try:
        app.unbind("<Button-1>")
    except Exception:  # noqa: BLE001
        pass


def _work_area(win):
    """可用桌面区域 (left, top, right, bottom)，已排除任务栏。

    没有它时弹窗按整屏定位，底部会被任务栏盖住（收款码/说明文字被遮）。
    非 Windows 或取不到时退回整屏。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        r = _RECT()
        # SPI_GETWORKAREA = 0x0030
        if (ctypes.windll.user32.SystemParametersInfoW(0x0030, 0,
                                                       ctypes.byref(r), 0)
                and r.right > r.left and r.bottom > r.top):
            return r.left, r.top, r.right, r.bottom
    except Exception:  # noqa: BLE001
        pass
    return 0, 0, win.winfo_screenwidth(), win.winfo_screenheight()


def _reward_photo(master, max_h=None):
    """加载奖励作者收款码，返回 (PhotoImage, 宽, 高)；缺失/失败返回 None。

    图片在构建期已按显示尺寸预渲染成 PNG，直接 1:1 加载即可 ——
    不依赖 Pillow（Tk 原生只认 PNG/GIF，不认 JPG）。
    当窗口放不下（小屏）时用 max_h 触发等比缩小：优先用 PIL 重采样
    （`PIL.Image` 随包提供），拿不到就退回原图，绝不因此报错。
    """
    path = resources.author_reward_png()
    if not path:
        return None
    if max_h:
        try:
            import base64
            import io

            from PIL import Image
            im = Image.open(path)
            iw, ih = im.size
            if ih > max_h:
                nw = max(1, round(iw * max_h / ih))
                im = im.resize((nw, max_h), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "PNG")
                photo = tk.PhotoImage(
                    master=master, data=base64.b64encode(buf.getvalue()))
                return photo, photo.width(), photo.height()
        except Exception:  # noqa: BLE001
            pass
    try:
        photo = tk.PhotoImage(master=master, file=path)
        return photo, photo.width(), photo.height()
    except Exception:  # noqa: BLE001
        return None


def _open_reward_full(app):
    """用系统默认看图程序打开收款码原图（便于放大扫码）。"""
    path = resources.author_reward_full_png() or resources.author_reward_png()
    if not path:
        app.toast("error", "未找到收款码图片")
        return
    try:
        if hasattr(os, "startfile"):          # Windows
            os.startfile(path)                # noqa: S606
        else:
            webbrowser.open("file://" + path)
    except Exception:  # noqa: BLE001
        app.toast("error", "无法打开图片")


class AuthorButton(tk.Frame):
    """侧栏底部的作者入口：头像 + 名字 + 「详情」。"""

    def __init__(self, master, theme, app, command=None):
        super().__init__(master, bg=theme.bg_sidebar, cursor="hand2")
        self.theme = theme
        self.app = app
        self.command = command
        inner = tk.Frame(self, bg=theme.bg_sidebar)
        inner.pack(fill="x", padx=8, pady=6)

        av = tk.Canvas(inner, width=26, height=26, bg=theme.bg_sidebar,
                       highlightthickness=0, bd=0)
        av.pack(side="left")
        av.create_oval(0, 0, 25, 25, fill=theme.bg_muted, outline="")
        av.create_text(13, 13, text=AUTHOR_NAME[:1].upper(), fill=theme.fg_muted,
                       font=(theme.font_family, 9, "bold"))
        self._av = av

        txt = tk.Frame(inner, bg=theme.bg_sidebar)
        txt.pack(side="left", padx=(8, 0), fill="x", expand=True)
        self._name = tk.Label(txt, text=AUTHOR_NAME, font=theme.f_tiny,
                              fg=theme.fg_muted, bg=theme.bg_sidebar, anchor="w")
        self._name.pack(anchor="w")
        self._hint = tk.Label(txt, text="点击查看作者信息", font=theme.f_tiny,
                              fg=theme.fg_dim, bg=theme.bg_sidebar, anchor="w")
        self._hint.pack(anchor="w")

        for wdg in (self, inner, av, txt, self._name, self._hint):
            wdg.bind("<Button-1>", lambda e: self._click())
            wdg.bind("<Enter>", lambda e: self._hover(True))
            wdg.bind("<Leave>", lambda e: self._hover(False))

    def _click(self):
        if self.command:
            self.command()

    def _hover(self, on):
        c = self.theme.bg_hover if on else self.theme.bg_sidebar
        for wdg in (self, self._av):
            try:
                wdg.configure(bg=c)
            except tk.TclError:
                pass

    def refresh(self, theme):
        self.theme = theme
        try:
            self.configure(bg=theme.bg_sidebar)
        except tk.TclError:
            pass
