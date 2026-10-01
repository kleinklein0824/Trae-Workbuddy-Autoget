#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""截图验证：设置页「计划任务」区在三种状态下的呈现，以及 toast 弹窗。

不依赖真实计划任务：直接调 _paint_task 注入状态数据后截图。
"""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 同目录的 _shot_util

import tkinter as tk  # noqa: E402

from app.application import App  # noqa: E402

SHOTS = os.path.join(os.path.dirname(__file__), "_shots")
os.makedirs(SHOTS, exist_ok=True)


def pump(app, n=10, dt=0.1):
    for _ in range(n):
        app.update()
        time.sleep(dt)


def shot(widget, name, pad=8):
    """用 Pillow ImageGrab 按控件屏幕坐标裁剪截图。

    截图前必须把窗口抬到最前并 update，否则 ImageGrab 抓到的是
    被遮挡区域（表现为抓到别的卡片）。这里用 winfo_rootx/rooty 取
    控件在屏幕上的绝对坐标，并做一次 lift + update。

    ★ 坐标必须乘 DPI 缩放比（走 `_shot_util`）：本机缩放 125%，Tk 进程被
      Windows 虚拟化 → `winfo_*` 是虚拟坐标，而 ImageGrab 按物理像素裁剪。
      漏乘的话只抓到左上 80% 区域（右侧内容落在图外，看着像被裁）。
    """
    import _shot_util as SU
    from PIL import ImageGrab
    win = widget.winfo_toplevel()
    try:
        win.lift()
        win.attributes("-topmost", True)
        win.update()
        time.sleep(0.25)
    except tk.TclError:
        pass
    widget.update_idletasks()
    k = SU.dpi_scale(win)
    x = widget.winfo_rootx() - pad
    y = widget.winfo_rooty() - pad
    w = widget.winfo_width() + pad * 2
    h = widget.winfo_height() + pad * 2
    img = ImageGrab.grab(bbox=(int(x * k), int(y * k),
                               int((x + w) * k), int((y + h) * k)))
    path = os.path.join(SHOTS, name)
    img.save(path)
    try:
        win.attributes("-topmost", False)
    except tk.TclError:
        pass
    return path


ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label}  {extra}")


app = App()
app.geometry("1240x860+40+20")
pump(app, 14)

app.goto("traework", "settings")
pump(app, 16)
page = app.page
assert type(page).__name__ == "SettingsPage"

CARD = page.card_task
# 把该卡滚进可视区域，否则截图会抓到滚动视口外的空白/其它卡片
try:
    page.scroll.canvas.update_idletasks()
    total = page.scroll.canvas.bbox("all")
    if total:
        frac = max(0.0, min(1.0, CARD.winfo_y() / max(1, total[3])))
        page.scroll.canvas.yview_moveto(frac)
        pump(app, 6)
except Exception as e:  # noqa: BLE001
    print("(滚动失败)", e)
CARD.update_idletasks()
print("卡片几何:", CARD.winfo_width(), "x", CARD.winfo_height(),
      " rooty =", CARD.winfo_rooty())

print("\n== 场景 A：从未运行（0x41303）==")
page._paint_task({
    "installed": True, "state": "Ready", "times": "09:00,21:00",
    "triggers": "MSFT_TaskDailyTrigger,MSFT_TaskLogonTrigger,MSFT_TaskEventTrigger",
    "lastRun": "11/30/1999 00:00:00", "result": 267011,
    "nextRun": "09/16/2026 09:00:00",
})
pump(app, 8)
status_a = page._task_status.cget("text")
detail_a = page._task_detail.cget("text")
print("  状态行:", repr(status_a))
print("  详情:", repr(detail_a))
check("A 状态行标为需检查", "需检查" in status_a, status_a)
check("A 详情含『从未成功运行过』", "从未成功运行过" in detail_a)
check("A 详情翻译了 267011", "从未运行过" in detail_a)
p1 = shot(CARD, "40_task_never_ran.png")
print("  截图:", p1)

print("\n== 场景 B：上次失败（找不到程序）==")
page._paint_task({
    "installed": True, "state": "Ready", "times": "09:00,21:00",
    "triggers": "MSFT_TaskDailyTrigger,MSFT_TaskLogonTrigger",
    "lastRun": "09/16/2026 09:00:12", "result": 2147942402,
    "nextRun": "09/16/2026 21:00:00",
})
pump(app, 8)
status_b = page._task_status.cget("text")
detail_b = page._task_detail.cget("text")
print("  状态行:", repr(status_b))
print("  详情:", repr(detail_b))
check("B 状态行标为需检查", "需检查" in status_b)
check("B 详情含『上次运行失败』", "上次运行失败" in detail_b)
check("B 详情翻译了找不到程序", "找不到程序" in detail_b)
p2 = shot(CARD, "41_task_failed.png")
print("  截图:", p2)

print("\n== 场景 C：正常（结果码 0）==")
page._paint_task({
    "installed": True, "state": "Ready", "times": "08:30,20:15",
    "triggers": "MSFT_TaskDailyTrigger,MSFT_TaskLogonTrigger,MSFT_TaskEventTrigger",
    "lastRun": "09/16/2026 01:37:26", "result": 0,
    "nextRun": "09/16/2026 08:30:00",
})
pump(app, 8)
status_c = page._task_status.cget("text")
detail_c = page._task_detail.cget("text")
print("  状态行:", repr(status_c))
print("  详情:", repr(detail_c))
check("C 状态行是已安装", "已安装" in status_c, status_c)
check("C 详情无告警", "⚠" not in detail_c)
check("C 详情显示新时间 08:30,20:15", "08:30,20:15" in detail_c)
check("C 详情翻译 0 为成功", "0（成功）" in detail_c)
p3 = shot(CARD, "42_task_healthy.png")
print("  截图:", p3)

print("\n== 场景 D：未安装 ==")
page._paint_task({"installed": False})
pump(app, 8)
status_d = page._task_status.cget("text")
print("  状态行:", repr(status_d))
check("D 状态行是未安装", "未安装" in status_d)
p4 = shot(CARD, "43_task_not_installed.png")
print("  截图:", p4)

print("\n== 场景 F：任务指向了别的副本（配置写不进去）==")
page._paint_task({
    "installed": True, "state": "Ready", "times": "09:00,21:00",
    "triggers": "MSFT_TaskDailyTrigger,MSFT_TaskLogonTrigger",
    "lastRun": "09/16/2026 12:10:13", "result": 0,
    "nextRun": "09/16/2026 21:00:00",
    # 指向另一个目录里的 exe —— 界面改的设置永远到不了那份配置
    "exe": r"D:\somewhere\else\TraeBuddy\TraeBuddy.exe",
})
pump(app, 8)
status_f = page._task_status.cget("text")
detail_f = page._task_detail.cget("text")
print("  状态行:", repr(status_f))
print("  详情:", repr(detail_f))
check("F 状态行标为需检查", "需检查" in status_f, status_f)
check("F 提示任务指向不是当前程序", "指向的不是当前程序" in detail_f)
check("F 给出修复指引", "安装 / 更新任务" in detail_f)
p6 = shot(CARD, "45_task_target_mismatch.png")
print("  截图:", p6)

print("\n== 场景 E：时间输入框 + 保存按钮（整块）==")
CARD2 = page.card_time
pump(app, 4)
p5 = shot(CARD2, "44_time_save_card.png")
print("  截图:", p5)
check("时间输入框内容正确", page._time_entry.get() in ("09:00, 21:00", "09:00,21:00"),
      page._time_entry.get())

app.destroy()
print()
print(f"结果：{ok} OK / {fail} FAIL")
sys.exit(0 if fail == 0 else 1)
