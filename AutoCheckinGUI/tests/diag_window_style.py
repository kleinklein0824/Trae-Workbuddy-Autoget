#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断窗口样式：确认「两层外框」与「没有任务栏图标」的根因。

不改动任何代码，只打印 Win32 的 style / exstyle / 窗口矩形。
用法：python tests/diag_window_style.py
"""
import ctypes
import os
import sys
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

GWL_STYLE, GWL_EXSTYLE = -16, -20

WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_BORDER = 0x00800000
WS_DLGFRAME = 0x00400000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_POPUP = 0x80000000

WS_EX_APPWINDOW = 0x00040000      # 强制出现在任务栏
WS_EX_TOOLWINDOW = 0x00000080     # 不出现在任务栏（与 APPWINDOW 相斥）
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_STATICEDGE = 0x00020000
WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_LAYERED = 0x00080000

u = ctypes.windll.user32
u.GetWindowLongPtrW.restype = ctypes.c_void_p
u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]


def names(val, table, label):
    hits = [n for n, bit in table.items() if val & bit]
    print(f"  {label} = 0x{val & 0xFFFFFFFF:08X}")
    print(f"    -> {hits if hits else '(无)'}")


def main():
    import tkinter as tk

    style_table = {
        "WS_POPUP": WS_POPUP, "WS_CAPTION": WS_CAPTION,
        "WS_THICKFRAME": WS_THICKFRAME, "WS_SYSMENU": WS_SYSMENU,
        "WS_BORDER": WS_BORDER, "WS_DLGFRAME": WS_DLGFRAME,
        "WS_MINIMIZEBOX": WS_MINIMIZEBOX, "WS_MAXIMIZEBOX": WS_MAXIMIZEBOX,
    }
    ex_table = {
        "WS_EX_APPWINDOW": WS_EX_APPWINDOW,
        "WS_EX_TOOLWINDOW": WS_EX_TOOLWINDOW,
        "WS_EX_WINDOWEDGE": WS_EX_WINDOWEDGE,
        "WS_EX_CLIENTEDGE": WS_EX_CLIENTEDGE,
        "WS_EX_STATICEDGE": WS_EX_STATICEDGE,
        "WS_EX_DLGMODALFRAME": WS_EX_DLGMODALFRAME,
        "WS_EX_LAYERED": WS_EX_LAYERED,
    }

    root = tk.Tk()
    root.geometry("900x620+60+60")
    root.update()

    from app.ui.titlebar import make_frameless
    make_frameless(root)
    root.update()

    hwnd = int(root.frame(), 16) if isinstance(root.frame(), str) else int(root.winfo_id())
    print(f"hwnd = 0x{hwnd:X}")

    st = u.GetWindowLongPtrW(hwnd, GWL_STYLE) or 0
    ex = u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0
    names(st, style_table, "STYLE")
    names(ex, ex_table, "EXSTYLE")

    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    cr = wintypes.RECT()
    u.GetClientRect(hwnd, ctypes.byref(cr))
    print(f"  窗口矩形 = {r.right - r.left} x {r.bottom - r.top}")
    print(f"  客户区   = {cr.right - cr.left} x {cr.bottom - cr.top}")
    print(f"  非客户区 = {(r.right - r.left) - (cr.right - cr.left)} x "
          f"{(r.bottom - r.top) - (cr.bottom - cr.top)}")

    # 是否有系统菜单/标题栏（决定任务栏是否显示图标）
    print(f"  任务栏会显示: {bool(ex & WS_EX_APPWINDOW) and not (ex & WS_EX_TOOLWINDOW)}")
    print(f"  边缘边线残留: "
          f"{bool(ex & (WS_EX_CLIENTEDGE | WS_EX_STATICEDGE | WS_EX_WINDOWEDGE))}")

    root.destroy()
    print("DONE")


if __name__ == "__main__":
    main()
