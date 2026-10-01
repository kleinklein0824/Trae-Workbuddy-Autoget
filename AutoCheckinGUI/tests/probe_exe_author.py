#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在**冻结 exe** 上验收「奖励作者」收款码是否完整显示。

源码态已由 `probe_author_card.py` 验过（几何 + 截图）；这里再上一层：
直接启动打包好的 exe，点侧栏作者入口，截图确认弹窗里的收款码整张可见。

⚠️ 坐标是**逻辑坐标**（不调 set_dpi_aware）：
   tk 报的屏幕/窗口几何、GetWindowRect、SetCursorPos 三者一致；
   ImageGrab 抓的是**物理像素**，裁剪时按 k=物理/逻辑 换算。

⚠️ **只能手动跑，不要加进 `_run_suite.py`**：它会启动 GUI、抢前台焦点并
   模拟点击。若窗口没能真正置前会**主动放弃点击**（返回码 3），以免误点
   到用户其它窗口上。

用法：python tests/probe_exe_author.py [exe路径]
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from win_tree_probe import tree, windows  # noqa: E402

DEFAULT_EXE = os.path.join(GUI_DIR, "dist", "TraeBuddy签到助手",
                           "TraeBuddy签到助手.exe")
SHOTS = os.path.join(HERE, "_shots")

u = ctypes.windll.user32
u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.GetWindowRect.restype = wintypes.BOOL
u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
u.SetCursorPos.restype = wintypes.BOOL

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def force_foreground(hwnd):
    """把窗口拉到前台并确认成功。

    ⚠️ 必须先确认**真的**在前台再点击，否则点击会落到别的窗口上
    （本项目踩过：盲点落到了用户的浏览器里）。
    """
    u.ShowWindow(hwnd, 9)                     # SW_RESTORE
    # 前台锁：先敲一下 ALT 再 SetForegroundWindow，成功率明显更高
    u.keybd_event(0x12, 0, 0, 0)
    u.keybd_event(0x12, 0, 2, 0)
    time.sleep(0.1)
    u.BringWindowToTop(hwnd)
    u.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    return int(u.GetForegroundWindow()) == int(hwnd)


def find_app_window(pid, timeout=60):
    """等主窗口出现，返回 (hwnd, left, top, w, h)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        for row in windows(pids=set(tree(pid))):
            if row["visible"] and row["cls"] == "TkTopLevel" and row["w"] > 300:
                r = wintypes.RECT()
                u.GetWindowRect(row["hwnd"], ctypes.byref(r))
                return (row["hwnd"], r.left, r.top,
                        r.right - r.left, r.bottom - r.top)
        time.sleep(0.5)
    return None


def click(x, y):
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    u.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    u.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def grab(name, box=None):
    from PIL import ImageGrab
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    img = ImageGrab.grab()
    if box:
        img = img.crop(box)
    img.save(path)
    print("  截图 →", path)
    return path


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXE
    print("exe =", exe)
    if not os.path.exists(exe):
        print("exe 不存在")
        return 2

    p = subprocess.Popen([exe])
    try:
        info = find_app_window(p.pid)
        if not info:
            print("[FAIL] 未找到主窗口")
            return 1
        hwnd, L, T, W, H = info
        print(f"主窗口 hwnd={hwnd} {W}x{H} @({L},{T})")

        if not force_foreground(hwnd):
            print("[SKIP] 未能把窗口置前，放弃点击（避免误点到其它窗口）")
            return 3
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        L, T, W, H = r.left, r.top, r.right - r.left, r.bottom - r.top
        print(f"已置前：{W}x{H} @({L},{T})")
        time.sleep(1.0)
        grab("60_exe_main.png")

        # 侧栏作者入口在最底部左侧：点窗口左下角内侧一点
        cx, cy = L + 95, T + H - 22
        print(f"点击作者入口 @({cx},{cy})")
        click(cx, cy)
        time.sleep(1.8)

        # 弹窗 380x662，水平居中于主窗、垂直偏上
        from PIL import ImageGrab
        probe = ImageGrab.grab()
        pw, ph = probe.size
        k = pw / float(u.GetSystemMetrics(0) or pw)   # 物理/逻辑
        box = (int((L + (W - 380) / 2 - 40) * k), int((T - 140) * k),
               int((L + (W + 380) / 2 + 40) * k), int((T + H) * k))
        box = (max(0, box[0]), max(0, box[1]),
               min(pw, box[2]), min(ph, box[3]))
        grab("61_exe_author.png", box)
        return 0
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
