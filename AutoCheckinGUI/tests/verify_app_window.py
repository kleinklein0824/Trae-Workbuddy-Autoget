#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验证：真实启动 App，检查窗口边框/任务栏/托盘/最大化后内容可见。

用法：python tests/verify_app_window.py
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_cb_errors = []
_orig = sys.excepthook


def _hook(t, v, tb):
    if "ctypes" in str(t) or "callback" in str(v):
        _cb_errors.append(f"{t.__name__}: {v}")
        return
    _orig(t, v, tb)


sys.excepthook = _hook

u = ctypes.windll.user32
u.GetWindowLongPtrW.restype = ctypes.c_void_p
u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


def info(h, tag):
    st = u.GetWindowLongPtrW(h, -16) or 0
    ex = u.GetWindowLongPtrW(h, -20) or 0
    r = wintypes.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    cr = wintypes.RECT()
    u.GetClientRect(h, ctypes.byref(cr))
    nc = ((r.right - r.left) - (cr.right - cr.left),
          (r.bottom - r.top) - (cr.bottom - cr.top))
    print(f"  [{tag}] rect={r.right-r.left}x{r.bottom-r.top} "
          f"client={cr.right-cr.left}x{cr.bottom-cr.top} nc={nc[0]}x{nc[1]}")
    print(f"         CAPTION={bool(st & 0x00C00000)} THICKFRAME={bool(st & 0x00040000)} "
          f"APPWINDOW={bool(ex & 0x00040000)} TOOLWINDOW={bool(ex & 0x00000080)}")
    return nc


def main():
    from app.application import App

    app = App()
    app.update()
    for _ in range(40):
        app.update()
        time.sleep(0.03)

    hwnd = None
    try:
        fr = app.frame()
        hwnd = int(fr, 16) if isinstance(fr, str) else int(app.winfo_id())
    except Exception:  # noqa: BLE001
        pass
    print(f"hwnd = 0x{hwnd:X}")

    print("=== 窗口外观 ===")
    nc0 = info(hwnd, "初始")

    print("=== 任务栏 = 有窗口样式吗 ===")
    ex = u.GetWindowLongPtrW(hwnd, -20) or 0
    print(f"  WS_EX_APPWINDOW = {bool(ex & 0x00040000)}  (True 才会出现在任务栏)")

    print("=== 托盘图标 ===")
    print(f"  app.tray = {app.tray is not None}")
    if app.tray is not None:
        print(f"  托盘 hwnd = {hex(app.tray._hwnd) if app.tray._hwnd else None}")
        print(f"  图标句柄已加载 = {bool(app.tray._hicon)}")

    print("=== 最大化后内容可见性 ===")
    app.titlebar.maximize()
    for _ in range(20):
        app.update()
        time.sleep(0.02)
    info(hwnd, "最大化")

    # 检查当前页的滚动容器是否覆盖整个可用区
    page = app.page
    if page is not None and hasattr(page, "scroll"):
        sf = page.scroll
        app.update_idletasks()
        cw = sf.canvas.winfo_width()
        ch = sf.canvas.winfo_height()
        sr = sf.canvas.cget("scrollregion")
        bbox = sf.canvas.bbox("all")
        print(f"  滚动画布 {cw}x{ch}  scrollregion={sr}")
        print(f"  内容 bbox = {bbox}")
        if bbox:
            content_w = bbox[2] - bbox[0]
            print(f"  内容宽度 {content_w} vs 画布宽度 {cw} "
                  f"→ {'横向已适配' if content_w <= cw + 2 else '⚠ 横向溢出'}")
        # 卡片是否量到了高度
        cards = [w for w in sf.inner.winfo_children()
                 if w.winfo_class() == "Frame"]
        heights = [c.winfo_height() for c in cards[:6]]
        print(f"  前 6 个板块高度 = {heights}")

    print("=== 还原 ===")
    app.titlebar.restore()
    for _ in range(20):
        app.update()
        time.sleep(0.02)
    info(hwnd, "还原")

    print(f"=== ctypes 回调异常数 = {len(_cb_errors)} ===")
    for e in _cb_errors[:5]:
        print("   ", e)

    app._closing = True
    try:
        app.destroy()
    except Exception:  # noqa: BLE001
        pass
    ok = (nc0[0] == 0 and nc0[1] == 0 and not _cb_errors)
    print("RESULT:", "OK" if ok else "PROBLEM")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
