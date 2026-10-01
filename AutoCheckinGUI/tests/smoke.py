#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 冒烟测试：不进入事件循环，逐页构建并截图，验证无异常。

用法：python tests/smoke.py
截图输出到 tests/_shots/
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SHOTS = os.path.join(HERE, "_shots")

PAGES = [
    ("traework", "dashboard"), ("traework", "checkin"),
    ("traework", "accounts"), ("traework", "credits"),
    ("traecn", "dashboard"), ("traecn", "checkin"), ("traecn", "accounts"),
    ("traecn", "credits"),
    ("workbuddy", "dashboard"), ("workbuddy", "checkin"),
    ("workbuddy", "accounts"), ("workbuddy", "credits"),
    ("traework", "logs"), ("traework", "settings"),
]


def screenshot(app, name):
    """用 Tk 自带的 postscript 无法截全窗，改用系统层截图（PIL 可用时）。

    ★ 坐标必须乘 DPI 缩放比（走 `_shot_util`）：本机系统缩放 125%，Tk 进程
      被 Windows **虚拟化** → `winfo_rootx/width` 是虚拟坐标，而
      `ImageGrab.grab(bbox=...)` 走 GDI 按**物理像素**裁剪。漏乘的话只抓到
      窗口左上 80% 区域（右侧内容落在图外，看着像被裁）。
    """
    try:
        import _shot_util as SU
        from PIL import ImageGrab
        app.update()
        k = SU.dpi_scale(app)
        x, y = app.winfo_rootx(), app.winfo_rooty()
        w, h = app.winfo_width(), app.winfo_height()
        os.makedirs(SHOTS, exist_ok=True)
        img = ImageGrab.grab(bbox=(int(x * k), int(y * k),
                                   int((x + w) * k), int((y + h) * k)))
        img.save(os.path.join(SHOTS, f"{name}.png"))
        return True
    except Exception as e:  # noqa: BLE001
        print(f"    (截图失败: {e!r})")
        return False


def main():
    from app.application import App
    app = App()
    app.update()
    failures = []
    for module, view in PAGES:
        key = f"{module}_{view}"
        try:
            # 走与用户点击一致的路径：先切模块，再切视图
            if module != app.module:
                app._switch_module(module)
                app.update()
            app.goto(module, view)
            for _ in range(3):
                app.update()
            label = f"{module}/{view}"
            print(f"  OK  {label}")
            screenshot(app, key)
        except Exception:  # noqa: BLE001
            failures.append((key, traceback.format_exc()))
            print(f"  FAIL {key}")
    # 主题切换各页再跑一遍
    try:
        app.set_theme(True)
        for _ in range(3):
            app.update()
        print("  OK  深色主题切换")
        screenshot(app, "dark_traework_dashboard")
    except Exception:  # noqa: BLE001
        failures.append(("dark_theme", traceback.format_exc()))
        print("  FAIL 深色主题切换")
    try:
        app.set_theme(False)
        app.update()
    except Exception:  # noqa: BLE001
        pass

    print()
    if failures:
        print(f"失败 {len(failures)} 项：")
        for k, tb in failures:
            print(f"\n--- {k} ---\n{tb}")
        app.destroy()
        return 1
    print(f"全部 {len(PAGES)} 个页面构建通过")
    app.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
