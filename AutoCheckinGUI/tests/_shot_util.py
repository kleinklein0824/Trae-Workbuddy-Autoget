#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""截图工具（共享）：把「Tk 虚拟坐标」正确换算成「GDI 物理像素」。

★ 为什么必须有这个文件
----------------------
本机真实屏幕 **1920x1080**，系统缩放 **125%**；而 Tk 进程
`GetProcessDpiAwareness = UNAWARE` → 被 Windows **DPI 虚拟化**：

    Tk  winfo_screenwidth()      = 1536   （= 1920 / 1.25，虚拟坐标）
    Win GetSystemMetrics(0)      = 1536   （同样被虚拟化）
    GetWindowRect(hwnd)          = 1180x900 @ (80,40)  ← 虚拟坐标
    全屏 ImageGrab.grab(all_screens=True) = 1920x1080   ← **物理像素**

而 `PIL.ImageGrab.grab(bbox=...)` 走 GDI BitBlt，**按物理像素**裁剪。
所以直接拿 `winfo_rootx/rooty/width/height` 去 grab：

    抓到的只有窗口左上 1/1.25 = 80% 的区域 → 右边、下边的内容落在图外。

**症状极具迷惑性**：截出来的图里文字"到右边缘就断了"，看起来像界面被裁，
而所有 Tk 层断言（`reqwidth <= width`）都是绿的。本机踩过一次：
花了好几轮去查"设置页文字是不是被裁"，最后发现是抓图坐标系错了。

★ 正确做法
----------
    from _shot_util import dpi_scale, grab_window
    img = grab_window(app)          # 自动乘缩放比，抓完整窗口

判据：全屏抓图宽 / `winfo_screenwidth()` —— 两者不等就说明存在虚拟化，
差值就是缩放比（本机 1920/1536 = 1.25）。**不要**用
`winfo_fpixels('1i')/96` 或 `GetDpiForWindow()`，在被虚拟化的进程里
它们同样返回 96（看着"缩放 1.0"，是假的）。
"""
import ctypes

_SCALE_CACHE = {}


def dpi_scale(root=None):
    """返回 GDI 物理像素 / Tk 虚拟坐标 的比值（本机 1.25）。

    算法：全屏抓图的宽度 ÷ Tk 报的屏幕宽度。
    全屏抓图走 GDI（物理），Tk 走虚拟化坐标 —— 比值就是缩放比。
    抓图失败时退回 1.0。
    """
    if "s" in _SCALE_CACHE:
        return _SCALE_CACHE["s"]
    s = 1.0
    try:
        from PIL import ImageGrab
        import tkinter as tk
        if root is not None:
            vw = root.winfo_screenwidth()
        else:
            vw = tk.Tk().winfo_screenwidth()
        full = ImageGrab.grab(all_screens=True)
        if vw:
            s = full.width / float(vw)
    except Exception:  # noqa: BLE001
        s = 1.0
    if abs(s - 1.0) < 0.02:
        s = 1.0
    _SCALE_CACHE["s"] = s
    return s


def physical_rect(win):
    """把窗口的 Tk 几何换算成 GDI 物理 bbox（left, top, right, bottom）。"""
    s = dpi_scale(win)
    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    return (int(x * s), int(y * s), int((x + w) * s), int((y + h) * s))


def grab_window(win, scale=None):
    """抓**完整**窗口（含被虚拟化掉的那 20%）。返回 PIL.Image。"""
    from PIL import ImageGrab
    if scale is None:
        scale = dpi_scale(win)
    l, t, r, b = physical_rect(win)
    return ImageGrab.grab(bbox=(l, t, r, b))


def grab_widget(win, widget):
    """抓单个控件（按物理坐标）。返回 PIL.Image。"""
    from PIL import ImageGrab
    s = dpi_scale(win)
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    w, h = widget.winfo_width(), widget.winfo_height()
    return ImageGrab.grab(bbox=(int(x * s), int(y * s),
                                int((x + w) * s), int((y + h) * s)))


def to_image_xy(win, widget):
    """控件左上角在 `grab_window(win)` 那张图里的像素坐标。"""
    s = dpi_scale(win)
    return (int((widget.winfo_rootx() - win.winfo_rootx()) * s),
            int((widget.winfo_rooty() - win.winfo_rooty()) * s))
