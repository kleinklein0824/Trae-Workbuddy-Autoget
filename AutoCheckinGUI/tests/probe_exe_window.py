#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在冻结 exe 上校验窗口外观（无需解锁屏幕）。

通过 EnumWindows 找到属于该进程（**含子孙进程**）的顶层窗口，检查：
  * 可见的 TkTopLevel 恰好 1 个（多于 1 说明有多余外框窗口）
  * 非客户区 == 0x0（否则就是两层外框没修掉）
  * 样式里没有 WS_CAPTION / WS_THICKFRAME
  * 扩展样式含 WS_EX_APPWINDOW（任务栏图标）、不含 WS_EX_TOOLWINDOW
  * 托盘窗口类 `TraeBuddyTrayWnd` 存在
  * 最大化后**恰好铺满工作区**（= 屏幕减任务栏）

    ⚠️ 判据是「等于工作区」，不是「不超过屏幕」。铺满整屏（含任务栏区域）
    的窗口同样满足"不超过屏幕"，却恰恰说明**没有**遵守工作区约束 ——
    旧判据会把这种情况当成通过。

⚠️ 单文件版（onefile）会把 GUI 跑在 fork 出的**子进程**里，
只按父 PID 找窗口会找不到；这里用 Toolhelp32 遍历进程树。
另外 onefile 首次运行要解包整个运行时，**给足 60 秒**。

用法：
    python tests/probe_exe_window.py [exe路径]
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from win_tree_probe import tree, windows  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
DEFAULT_EXE = os.path.join(GUI_DIR, "dist", "TraeBuddy签到助手",
                           "TraeBuddy签到助手.exe")

u = ctypes.windll.user32
for _fn, _at, _rt in (
    ("GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
    ("GetWindowTextLengthW", [wintypes.HWND], ctypes.c_int),
    ("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
    ("IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
    ("GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
    ("GetClientRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
):
    _f = getattr(u, _fn)
    _f.argtypes = _at
    _f.restype = _rt

u.IsWindowVisible = u.IsWindowVisible          # 保持引用可读性
u.GetWindowLongPtrW.restype = ctypes.c_void_p
u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
SW_MAXIMIZE = 3
SW_RESTORE = 9
SPI_GETWORKAREA = 0x0030

# ⚠️ `SystemParametersInfoW` 必须显式声明原型，否则 RECT 指针会被当成 int
# 而静默写坏内存（本项目在别处踩过同类 ctypes 坑）。
u.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                    ctypes.POINTER(wintypes.RECT),
                                    wintypes.UINT]
u.SystemParametersInfoW.restype = wintypes.BOOL


def work_area():
    """桌面工作区（已扣掉任务栏）。

    ⚠️ 本脚本**故意不调 `SetProcessDpiAwareness`** —— 它依赖 DPI 虚拟化的
    **逻辑坐标**（125% 下 1920x1080 读成 1536x864、工作区读成 1536x816）。
    `win_tree_probe.py` 才是需要 `SetProcessDpiAwareness(2)` 的那个。

    正对照
    ------
    设 `PROBE_FAKE_WA=WxH` 可把工作区伪造成指定值。用途是验证「最大化必须
    铺满工作区」这条判据**真的会红** —— 否则它打印的 OK 可能只是因为断言
    从未生效（本项目在「定尺寸弹窗裁切」上就吃过一次假阴性：判据本身是盲的，
    正对照才发现）。
    例：`PROBE_FAKE_WA=1536x864 python tests/probe_exe_window.py <exe>`
    → 应报 `最大化未铺满工作区：1536x816 != 工作区 1536x864` 且 `RESULT: FAIL`。
    """
    fake = os.environ.get("PROBE_FAKE_WA")
    if fake:
        w, _, h = fake.partition("x")
        return int(w), int(h)
    r = wintypes.RECT()
    if not u.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
        return None
    return r.right - r.left, r.bottom - r.top

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def probe(pids):
    """列出属于给定 pid 集合的窗口（含样式与客户区尺寸）。"""
    found = []
    titles = {}

    def cb(hwnd, _lp):
        wpid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value not in pids:
            return True
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        u.GetWindowTextW(hwnd, buf, n + 2)
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        r = wintypes.RECT()
        c = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        u.GetClientRect(hwnd, ctypes.byref(c))
        found.append(dict(
            hwnd=hwnd, title=buf.value, cls=cls.value,
            visible=bool(u.IsWindowVisible(hwnd)),
            style=u.GetWindowLongPtrW(hwnd, GWL_STYLE) or 0,
            exstyle=u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0,
            w=r.right - r.left, h=r.bottom - r.top,
            cw=c.right - c.left, ch=c.bottom - c.top,
        ))
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def main():
    exe = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXE
    if not os.path.exists(exe):
        print("RESULT: SKIP (未找到 exe:", exe, ")")
        return 2
    exe = os.path.abspath(exe)
    print("exe =", exe)

    p = subprocess.Popen([exe], cwd=os.path.dirname(exe))
    pid = p.pid
    print("pid:", pid)
    ok = True
    try:
        hwnd = None
        # onefile 首启要解包整个运行时，给足 60 秒
        for attempt in range(40):
            time.sleep(1.5)
            if p.poll() is not None:
                print("!! 进程退出，码 =", p.returncode)
                return 1
            wins = [f for f in probe(set(tree(pid)))
                    if f["visible"] and f["w"] > 200 and f["cls"] == "TkTopLevel"]
            if wins:
                hwnd = wins[0]["hwnd"]
                print("[%d] 找到 TkTopLevel %d 个" % (attempt, len(wins)))
                break
        if hwnd is None:
            print("RESULT: FAIL (60 秒内没有出现 TkTopLevel 窗口)")
            return 1

        pids = set(tree(pid))
        allw = probe(pids)
        tks = [f for f in allw if f["cls"] == "TkTopLevel" and f["visible"]]
        print("进程树 =", sorted(pids))
        print("可见 TkTopLevel 数 =", len(tks))
        if len(tks) != 1:
            print("  !! 期望恰好 1 个（多于 1 说明还有多余外框窗口）")
            ok = False

        f = [x for x in allw if x["hwnd"] == hwnd][0]
        nc_w, nc_h = f["w"] - f["cw"], f["h"] - f["ch"]
        print("  窗口 %dx%d  客户区 %dx%d  非客户区 %dx%d"
              % (f["w"], f["h"], f["cw"], f["ch"], nc_w, nc_h))
        print("  CAPTION=%s THICKFRAME=%s APPWINDOW=%s TOOLWINDOW=%s" % (
            bool(f["style"] & WS_CAPTION), bool(f["style"] & WS_THICKFRAME),
            bool(f["exstyle"] & WS_EX_APPWINDOW),
            bool(f["exstyle"] & WS_EX_TOOLWINDOW)))
        if nc_w or nc_h:
            print("  !! 非客户区应为 0x0（否则就是两层外框）")
            ok = False
        if f["style"] & WS_CAPTION:
            print("  !! 仍带 WS_CAPTION")
            ok = False
        if not (f["exstyle"] & WS_EX_APPWINDOW):
            print("  !! 缺少 WS_EX_APPWINDOW，任务栏不会显示")
            ok = False
        if f["exstyle"] & WS_EX_TOOLWINDOW:
            print("  !! 带着 WS_EX_TOOLWINDOW，会被踢出任务栏")
            ok = False

        tray = [x for x in allw if x["cls"] == "TraeBuddyTrayWnd"]
        print("  托盘窗口 TraeBuddyTrayWnd =", len(tray))
        if not tray:
            print("  !! 没找到托盘窗口，托盘图标可能未创建")
            ok = False

        # ---- 最大化 ----
        # ⚠️ 这里量的是「程序自己的最大化逻辑有没有遵守工作区」，不是内核行为。
        #   实测（2026-09-18，用独立采样脚本跑收敛曲线）：
        #     · 程序**启动完成**后 ShowWindow(SW_MAXIMIZE) → 立刻就是 1536x816 并保持；
        #     · 但**重建后第一次**启动时，主线程被启动期的重活占住（账号扫描 /
        #       playwright 预热），程序那段"最大化后修正到工作区"的回调被推迟，
        #       窗口会**长时间**停在内核给的整屏尺寸 1536x864 上。
        #   后者的读数与"程序真不遵守工作区"完全一样 —— 只靠"采样到稳定"分不开，
        #   早先连续 3 次（0.9s）、后来放宽到连续 10 次（3.0s）都照样假失败。
        #
        # ★ 所以改成**先让程序跑热，再做一次"还原 → 最大化"作为判定用动作**：
        #   程序会在这次最大化时重新算一遍尺寸。真坏了的话第二次照样是 864，
        #   判据不会因此失效（不是"等到对为止"，是"重新量一次"）。
        time.sleep(2.0)                      # 给启动期的重活一点时间
        u.ShowWindow(hwnd, SW_MAXIMIZE)      # 触发一次最大化路径（不作为判定）
        time.sleep(0.8)
        _w0 = [x for x in probe(pids) if x["hwnd"] == hwnd]
        _pre = (_w0[0]["w"], _w0[0]["h"]) if _w0 else None
        u.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.6)
        u.ShowWindow(hwnd, SW_MAXIMIZE)      # ← 判定用
        # 采样到尺寸稳定：连续 6 次一致（约 1.8s）才算收敛。
        g = None
        prev = None
        stable = 0
        series = []
        for _ in range(50):                 # 最长约 15s
            time.sleep(0.3)
            cur = [x for x in probe(pids) if x["hwnd"] == hwnd]
            cur = cur[0] if cur else None
            if cur is None:
                continue
            key = (cur["w"], cur["h"])
            series.append(key)
            if key == prev:
                stable += 1
                if stable >= 5:             # 连续 6 次一致（约 1.8s）才算收敛
                    g = cur
                    break
            else:
                stable = 0
            prev = key
        if g is None:
            g = [x for x in probe(pids) if x["hwnd"] == hwnd]
            g = g[0] if g else None
            if series:
                # 把采样序列打出来：真失败时要能看出是"卡在整屏"还是"一直抖"
                print(f"  (注：尺寸始终未收敛，末尾采样 {series[-6:]}，取最后值判定)")
        if g:
            sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
            wa = work_area()
            print("  最大化后 %dx%d（屏幕 %dx%d，工作区 %s）"
                  % (g["w"], g["h"], sw, sh,
                     "%dx%d" % wa if wa else "?"))
            if _pre and _pre != (g["w"], g["h"]):
                # 冷启动的铁证：第一次最大化读到的是别的值，第二次才正确。
                # 看到这行就说明"还原→最大化"这一步确实在兜冷启动。
                print(f"  (注：预热时读数为 {_pre[0]}x{_pre[1]}，"
                      f"还原后重新最大化才得到上面这个值 —— 属于启动期未跑完)")
            if g["w"] > sw or g["h"] > sh:
                print("  !! 最大化尺寸超出屏幕")
                ok = False
            # ★ 判据必须是「恰好铺满**工作区**」而不是「不超过屏幕」：
            # 一个铺满整屏（含任务栏区域）的窗口同样满足"不超过屏幕"，
            # 却说明**没有**遵守工作区 —— 旧判据会把这种情况放过。
            if wa is None:
                # ⚠️ 读不到工作区时**不能静默跳过** —— 那样这个 gate 会
                # 在判据失效的情况下照样打印 RESULT: OK（"仪器失明"型缺陷，
                # 本项目已在「截图当证据」上踩过一次）。读不到就是**不能认证**。
                print("  !! 无法读取工作区（SystemParametersInfoW 失败），"
                      "最大化判据失效，不能认证")
                ok = False
            else:
                wa_w, wa_h = wa
                if (wa_w, wa_h) == (sw, sh):
                    # 任务栏自动隐藏时工作区 == 屏幕，判据退化为"铺满整屏"。
                    # 仍然有效，只是分辨力下降，这里点明以免读者误以为它验了任务栏。
                    print("  (注：工作区 == 屏幕，任务栏可能自动隐藏)")
                if g["w"] > wa_w + 2 or g["h"] > wa_h + 2:
                    print("  !! 最大化尺寸超出工作区（未遵守工作区约束）")
                    ok = False
                if (g["w"], g["h"]) != (wa_w, wa_h):
                    print("  !! 最大化未铺满工作区：%dx%d != 工作区 %dx%d"
                          % (g["w"], g["h"], wa_w, wa_h))
                    ok = False
            if g["w"] - g["cw"] or g["h"] - g["ch"]:
                print("  !! 最大化后非客户区不为 0x0")
                ok = False
        u.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.8)
    finally:
        try:
            p.terminate()
            p.wait(timeout=8)
        except Exception:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True)
            except Exception:
                pass

    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
