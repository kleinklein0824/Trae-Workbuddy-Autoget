#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动真实 exe，切到 TraeWork 账号页并截图，验证「账号列表能显示已有账号」。

为什么需要它：exe 的数据目录定位是运行期行为，脚本模式测不出问题
（脚本模式 ROOT 一定对）。只有真的把 exe 跑起来，才能确认
「exe 找到了项目里的 config.json」这件事。

用法：python tests/shoot_exe_accounts.py [输出的 png 路径]
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
EXE = os.path.join(GUI, "dist", "TraeBuddy签到助手", "TraeBuddy签到助手.exe")

u32 = ctypes.windll.user32
g32 = ctypes.windll.gdi32


def find_window(pid, timeout=25):
    """等 exe 的主窗口出现，返回 hwnd。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def cb(hwnd, _):
            wpid = wt.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid and u32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                u32.GetClassNameW(hwnd, buf, 256)
                if buf.value == "TkTopLevel":
                    found.append(hwnd)
            return True

        u32.EnumWindows(cb, 0)
        if found:
            return found[0]
        time.sleep(0.3)
    return None


def grab(hwnd, out):
    """PrintWindow 截整窗为 BMP。"""
    r = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return False
    hdc = u32.GetWindowDC(hwnd)
    mdc = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    g32.SelectObject(mdc, bmp)
    u32.PrintWindow(hwnd, mdc, 2)

    class BMI(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    bi = BMI()
    bi.biSize = ctypes.sizeof(BMI)
    bi.biWidth, bi.biHeight = w, -h
    bi.biPlanes, bi.biBitCount = 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    hdr = (b"BM" + (14 + 40 + w * h * 4).to_bytes(4, "little") + b"\0\0\0\0"
           + (54).to_bytes(4, "little"))
    ih = ((40).to_bytes(4, "little") + w.to_bytes(4, "little")
          + h.to_bytes(4, "little") + (1).to_bytes(2, "little")
          + (32).to_bytes(2, "little") + (0).to_bytes(4, "little")
          + (w * h * 4).to_bytes(4, "little") + b"\0" * 16)
    with open(out, "wb") as f:
        f.write(hdr)
        f.write(ih)
        f.write(buf.raw)
    g32.DeleteObject(bmp)
    g32.DeleteDC(mdc)
    u32.ReleaseDC(hwnd, hdc)
    return True


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        HERE, "_shots", "exe_accounts.png")
    if not os.path.isfile(EXE):
        print(f"找不到 exe：{EXE}")
        return 1
    os.makedirs(os.path.dirname(out), exist_ok=True)
    print(f"启动 {EXE}")
    proc = subprocess.Popen([EXE], cwd=os.path.dirname(EXE))
    try:
        hwnd = find_window(proc.pid)
        if not hwnd:
            print("RESULT: FAIL —— 未找到主窗口")
            return 1
        # 等账号缓存（后台线程扫 IDE 日志，首次较慢）
        time.sleep(8)
        if not grab(hwnd, out):
            print("RESULT: FAIL —— 截图失败")
            return 1
        print(f"已保存 {out}")
        print("RESULT: OK")
        return 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
