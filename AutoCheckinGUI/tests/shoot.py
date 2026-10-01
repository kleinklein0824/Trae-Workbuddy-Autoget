#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""截图工具（全屏抓取 + 按窗口外框裁剪）。

`app.winfo_rootx()` 返回的是**客户区**左上角，不是窗口外框左上角，
直接用它会漏掉边框宽度导致右侧被裁。这里改为全屏抓取后按实际位置裁剪，
并用多次采样取窗口的真实可见区域。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SHOTS = os.path.join(HERE, "_shots")
W, H = 1180, 760

TARGETS = [
    ("traework", "dashboard", "01_dashboard_light"),
    ("traework", "checkin", "02_checkin_light"),
    ("traework", "accounts", "03_accounts_light"),
    ("traework", "credits", "04_credits_light"),
    ("workbuddy", "dashboard", "05_wb_dashboard_light"),
    ("workbuddy", "accounts", "06_wb_accounts_light"),
    ("traework", "logs", "07_logs_light"),
    ("traework", "settings", "08_settings_light"),
    ("traework", "dashboard", "09_dashboard_dark", True),
    ("traework", "checkin", "10_checkin_dark", True),
]


def settle(app, n=16):
    for _ in range(n):
        app.update()
        time.sleep(0.05)


def dpi_scale(app):
    """返回 物理像素 / 逻辑像素 的缩放比。

    Tk 的 winfo_screenwidth() 返回逻辑像素（受 DPI 缩放影响），
    而 ImageGrab 抓的是物理像素。两者不一致时按逻辑坐标裁剪会偏。
    """
    from PIL import ImageGrab
    sw = app.winfo_screenwidth()
    gw = ImageGrab.grab().size[0]
    return (gw / sw) if sw else 1.0


def place(app):
    sw, sh = app.winfo_screenwidth(), app.winfo_screenheight()
    x = max(0, (sw - W) // 2)
    y = max(0, (sh - H) // 4)
    app.deiconify()
    app.geometry(f"{W}x{H}+{x}+{y}")
    app.update_idletasks()
    app.lift()
    app.attributes("-topmost", True)
    settle(app, 8)
    return x, y


def grab(app, name, origin, k=1.0):
    from PIL import ImageGrab
    x, y = origin
    settle(app, 14)
    full = ImageGrab.grab()
    box = (int(x * k), int(y * k), int((x + W) * k), int((y + H) * k))
    img = full.crop(box)
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, f"{name}.png")
    img.save(path)
    return path


def main():
    from app.application import App
    from app import config as C

    app = App()
    app.update()
    origin = place(app)
    k = dpi_scale(app)
    print(f"dpi scale = {k:.4f}")

    for _ in range(80):
        app.update()
        time.sleep(0.1)
        if C.peek_accounts() is not None:
            break

    made = []
    for t in TARGETS:
        module, view, name = t[0], t[1], t[2]
        want_dark = len(t) > 3 and t[3]
        try:
            app.set_theme(want_dark)
            if module != app.module:
                app._switch_module(module)
                app.update()
            app.goto(module, view)
            place(app)
            p = grab(app, name, origin, k)
            made.append(p)
            print(f"ok {name}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {name}: {e!r}")

    app.destroy()
    print("\n".join(made))
    return 0


if __name__ == "__main__":
    sys.exit(main())
