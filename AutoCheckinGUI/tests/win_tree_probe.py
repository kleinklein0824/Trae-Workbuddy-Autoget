#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""枚举一个进程及其子孙进程创建的顶层窗口。

单文件版（onefile）会 fork 出一个子进程承载真正的 GUI，
所以只按父 PID 找窗口会找不到——必须把进程树都算上。

用法：
    python tests/win_tree_probe.py <exe路径> [等待秒数]
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

u = ctypes.windll.user32
k32 = ctypes.windll.kernel32

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def snapshot():
    """返回 [(pid, ppid, exename), ...]"""
    h = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if h == -1:
        return []
    out = []
    e = PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    ok = k32.Process32FirstW(h, ctypes.byref(e))
    while ok:
        out.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile))
        ok = k32.Process32NextW(h, ctypes.byref(e))
    k32.CloseHandle(h)
    return out


def tree(root):
    """广度优先收集 root 及其所有子孙 pid。"""
    procs = snapshot()
    kids = {}
    for pid, ppid, name in procs:
        kids.setdefault(ppid, []).append(pid)
    seen, frontier = [], [root]
    while frontier:
        p = frontier.pop(0)
        seen.append(p)
        frontier.extend(kids.get(p, []))
    return seen


def windows(pids=None):
    """列出顶层窗口。

    `pids=None` 表示**不限进程**（全屏扫描）。为什么需要这个：
    签到进程会在结束时用另一个进程把卡片弹出来，父进程随即退出 ——
    只按父 PID（或它的进程树）找窗口会一无所获，因为那时子进程已经
    被挂到别的父进程下了。这时只能全屏扫、再按窗口特征认人。
    """
    rows = []

    def cb(hwnd, _lp):
        try:
            wp = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(wp))
            if pids is not None and wp.value not in pids:
                return True
            # 标题长度必须夹紧：窗口可能正在被销毁，GetWindowTextLengthW
            # 偶发返回垃圾值，拿它去 create_unicode_buffer 会直接崩掉进程。
            n = int(u.GetWindowTextLengthW(hwnd))
            n = max(0, min(n, 512))
            b = ctypes.create_unicode_buffer(n + 2)
            u.GetWindowTextW(hwnd, b, n + 2)
            c = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, c, 256)
            r = wintypes.RECT()
            if not u.GetWindowRect(hwnd, ctypes.byref(r)):
                return True
            rows.append(dict(
                pid=wp.value, hwnd=hwnd, title=b.value, cls=c.value,
                visible=bool(u.IsWindowVisible(hwnd)),
                w=r.right - r.left, h=r.bottom - r.top))
        except Exception:  # noqa: BLE001
            pass
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return rows


def set_dpi_aware():
    """把本进程声明为 per-monitor DPI 感知（幂等，失败不抛）。

    ⚠️ **读窗口几何之前必须调用它**，否则 `GetWindowRect` 返回的是被 DPI
    虚拟化过的**逻辑坐标**：125% 缩放下，一个 423×280 的物理窗口会读成
    338×224（正好 ÷1.25）。于是"宽度应该等于 423"这类断言会莫名其妙地失败，
    看起来像窗口尺寸算错了，其实只是读法不对 —— 这个坑已经踩过两次。

    注意：**不要**在本模块 import 时自动调用。`probe_exe_window.py` 那类
    探测依赖逻辑坐标（它断言"最大化后 1536×816"，正是 1920/1.25），
    全局改掉会把那边弄坏。需要物理像素的调用方自己显式调一次。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return True
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            return True
        except Exception:  # noqa: BLE001
            return False


def exe_of(pid):
    """按 pid 取可执行文件名（取不到返回空串）。"""
    for p, _pp, name in snapshot():
        if p == pid:
            return name
    return ""


def main():
    exe = sys.argv[1]
    wait = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0
    exe = os.path.abspath(exe)
    print("exe =", exe)
    p = subprocess.Popen([exe], cwd=os.path.dirname(exe))

    t0 = time.time()
    found = []
    while time.time() - t0 < wait:
        time.sleep(1.5)
        pids = set(tree(p.pid))
        rows = windows(pids)
        found = [r for r in rows if r["visible"] and r["w"] > 200]
        if found:
            break
    pids = set(tree(p.pid))
    rows = windows(pids)
    print("进程树 pid =", sorted(pids))
    print("窗口总数 =", len(rows))
    for r in rows:
        mark = "*" if r in found else " "
        print("%s pid=%d %s %r %s vis=%s %dx%d" % (
            mark, r["pid"], hex(r["hwnd"]), r["title"], r["cls"],
            r["visible"], r["w"], r["h"]))
    print("RESULT:", "OK" if found else "FAIL (未出现可见窗口)")
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                   capture_output=True)
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
