#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针：验证 exe 启动后窗口**真的有图标**（`assets/app.ico` 真的被找到）。

为什么需要单独一条
------------------
`titlebar.set_window_icon()` 用 `WM_SETICON` 把图标下发给窗口，而
`ui/resources.app_icon_ico()` **拿不到文件时是静默 `return` 的** ——
于是「exe 里没有 assets/app.ico」这个缺陷表现为**任务栏/窗口空白图标**，
没有任何报错，也不影响任何功能测试。
★ 而且它**只在 exe 上显形**：源码运行按源码目录能找到文件，一切正常。
（2026-09-22 实测：`build_exe.spec` 的 `datas` 只扫 `app/`，漏了 `GUI_DIR/assets/`
→ 窗口与托盘图标全空。见 README「打包成 exe」。）

判据
----
`SendMessageW(hwnd, WM_GETICON, 0/1/2, 0)` 返回**非零 HICON**。
（`WM_SETICON` 成功下发后，`WM_GETICON` 就能拿回来。）

★ 找窗口的方式很关键：**不能按 `Popen` 的 pid 匹配**。
  onefile 的引导进程会**再拉一个子进程**，窗口属于子进程 ——
  只比对父进程 pid 永远找不到（实测症状是「FAIL 没找到窗口」，
  看起来像程序没起来，其实是探针找错了）。
  正解：按「窗口类 = `TkTopLevel` + 标题 = 应用标题」枚举，
  再用 `QueryFullProcessImageNameW` 反查镜像路径确认是本程序。

用法：
    python tests/probe_exe_icon.py <exe 路径> [更多 exe ...]
    # 不给参数则默认测当前 dist/ 下的三个产物

退出码：0 = 全部有图标；非 0 = 有产物没图标或没找到窗口。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)

APP_TITLE = "Trae & Buddy 签到助手"
WM_GETICON = 0x007F
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_S = 60.0            # onefile 要解包，给足时间

_u = ctypes.windll.user32
_k = ctypes.windll.kernel32


def _image_of(pid):
    """反查进程的镜像路径（用于确认窗口属于本程序，而不是别的同名窗口）。"""
    h = _k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if _k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _k.CloseHandle(h)


def find_app_window(marker):
    """找本程序可见的主窗口。`marker` 是 exe 文件名里的一段（如 TraeBuddy）。"""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        try:
            if not _u.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(64)
            _u.GetClassNameW(hwnd, cls, 64)
            if cls.value != "TkTopLevel":
                return True
            txt = ctypes.create_unicode_buffer(256)
            _u.GetWindowTextW(hwnd, txt, 256)
            if APP_TITLE not in txt.value:
                return True
            pid = wt.DWORD()
            _u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if marker and marker not in _image_of(pid.value):
                return True
            found.append((hwnd, pid.value))
        except Exception:  # noqa: BLE001
            pass
        return True

    _u.EnumWindows(_cb, 0)
    return found[0] if found else None


def probe(exe):
    """返回 (ok, 说明)。"""
    if not os.path.exists(exe):
        return False, "文件不存在"
    marker = os.path.splitext(os.path.basename(exe))[0][:8]
    proc = subprocess.Popen([exe])
    try:
        hit = None
        deadline = time.time() + WAIT_S
        while time.time() < deadline:
            time.sleep(0.5)
            hit = find_app_window(marker)
            if hit:
                break
        if hit is None:
            return False, f"{WAIT_S:.0f} 秒内没找到本程序的可见窗口"
        hwnd, pid = hit
        vals = [_u.SendMessageW(hwnd, WM_GETICON, i, 0) for i in (0, 1, 2)]
        detail = "pid=%d ICON_SMALL/BIG/SMALL2 = %s" % (
            pid, [hex(v or 0) for v in vals])
        return bool(any(vals)), detail
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=20)
        except Exception:  # noqa: BLE001
            pass


def main():
    targets = sys.argv[1:]
    if not targets:
        targets = [
            os.path.join(GUI_DIR, "dist", "TraeBuddy签到助手",
                         "TraeBuddy签到助手.exe"),
            os.path.join(GUI_DIR, "dist", "TraeBuddy签到助手-单文件.exe"),
        ]
    ok_n = fail_n = 0
    for exe in targets:
        rel = os.path.relpath(exe, GUI_DIR) if os.path.isabs(exe) else exe
        ok, detail = probe(exe)
        print("  %-4s %-46s %s" % ("OK" if ok else "FAIL", rel, detail))
        if ok:
            ok_n += 1
        else:
            fail_n += 1
    print()
    print(f"RESULT: {'OK' if fail_n == 0 else 'FAIL'}  {ok_n} OK / {fail_n} FAIL")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
