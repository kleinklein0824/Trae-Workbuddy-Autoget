#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证无边框窗口：边框/任务栏/最大化/缩放，并统计 ctypes 回调异常。

用法：python tests/verify_frameless.py
"""
import ctypes
import os
import sys
import threading
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# ctypes 的回调异常是"静默"的（只 print），这里捕获它
_cb_errors = []
_orig_excepthook = sys.excepthook


def _hook(t, v, tb):
    if "ctypes" in str(t) or "callback" in str(v):
        _cb_errors.append(f"{t.__name__}: {v}")
        return
    _orig_excepthook(t, v, tb)


sys.excepthook = _hook

u = ctypes.windll.user32
u.GetWindowLongPtrW.restype = ctypes.c_void_p
u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


def hwnd_of(w):
    w.update_idletasks()
    fr = w.frame()
    return int(fr, 16) if isinstance(fr, str) else int(w.winfo_id())


def snapshot(w, tag):
    h = hwnd_of(w)
    st = u.GetWindowLongPtrW(h, -16) or 0
    ex = u.GetWindowLongPtrW(h, -20) or 0
    r = wintypes.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    cr = wintypes.RECT()
    u.GetClientRect(h, ctypes.byref(cr))
    nc_w = (r.right - r.left) - (cr.right - cr.left)
    nc_h = (r.bottom - r.top) - (cr.bottom - cr.top)
    print(f"  [{tag}]")
    print(f"    窗口 {r.right-r.left}x{r.bottom-r.top} @({r.left},{r.top})  "
          f"客户区 {cr.right-cr.left}x{cr.bottom-cr.top}")
    print(f"    非客户区 {nc_w}x{nc_h}   边框残留={nc_w or nc_h}")
    print(f"    WS_CAPTION={bool(st & 0x00C00000)}  "
          f"WS_THICKFRAME={bool(st & 0x00040000)}  "
          f"APPWINDOW={bool(ex & 0x00040000)}  "
          f"TOOLWINDOW={bool(ex & 0x00000080)}")
    return nc_w, nc_h, r


def main():
    import tkinter as tk

    root = tk.Tk()
    root.title("FramelessTest")
    root.geometry("1000x680+80+80")
    root.update()

    from app.ui.titlebar import (TitleBar, enable_edge_resize, make_frameless)

    make_frameless(root)
    root.update()
    tb = TitleBar(root, _theme(), on_close=root.destroy)
    tb.pack(fill="x", side="top")
    root.update()
    enable_edge_resize(root)
    root.update()

    print("=== 初始状态 ===")
    nc_w, nc_h, _ = snapshot(root, "初始")

    print("=== 最大化 ===")
    tb.maximize()
    for _ in range(12):
        root.update()
        root.update_idletasks()
    _, _, r1 = snapshot(root, "最大化")

    print("=== 还原 ===")
    tb.restore()
    for _ in range(12):
        root.update()
        root.update_idletasks()
    snapshot(root, "还原")

    print("=== 最小化 ===")
    root.update()
    tb.minimize()
    root.update()

    # 等待 UI 队列把回调跑完，再收尾
    def finish():
        print(f"=== ctypes 回调异常数 = {len(_cb_errors)} ===")
        for e in _cb_errors[:5]:
            print("   ", e)
        root.quit()

    root.after(400, finish)
    try:
        root.mainloop()
    except Exception:  # noqa: BLE001
        pass
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass

    ok = (nc_w == 0 and nc_h == 0 and not _cb_errors)
    print("RESULT:", "OK" if ok else "PROBLEM")
    return 0 if ok else 1


def _theme():
    from app.theme import Theme
    return Theme(False)


if __name__ == "__main__":
    sys.exit(main())
