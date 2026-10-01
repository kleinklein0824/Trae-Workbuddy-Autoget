#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在打包后的 exe 上做启动自检：起进程、抓窗口、截图、退出。"""
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.abspath(os.path.join(_HERE, "..", "dist", "TraeBuddy签到助手"))
EXE = os.path.join(GUI, "TraeBuddy签到助手.exe")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_shots", "exe")

os.makedirs(OUT, exist_ok=True)
print("exe exists:", os.path.exists(EXE))

p = subprocess.Popen([EXE], cwd=GUI,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
print("pid:", p.pid)

# 给 GUI 足够时间起来（PyInstaller 冷启动较慢）
time.sleep(18)
if p.poll() is not None:
    print("!! 进程已退出，退出码 =", p.returncode)
    print((p.stdout.read() or b"").decode("utf-8", "replace")[:3000])
    sys.exit(1)

print("进程存活，尝试截图…")
try:
    from PIL import ImageGrab
    img = ImageGrab.grab()
    path = os.path.join(OUT, "exe_gui.png")
    img.save(path)
    print("saved:", path, img.size)
except Exception as e:  # noqa: BLE001
    print("截图失败:", repr(e))

p.terminate()
try:
    p.wait(timeout=8)
except Exception:  # noqa: BLE001
    p.kill()
print("DONE")
