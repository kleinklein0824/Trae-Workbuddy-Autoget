#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验**安装版** exe（`Output/*_Setup_*.exe`）。

⚠️ 为什么不能直接用 `probe_exe_window.py`：
   安装向导是一个**普通带边框的 Tk 窗口**（`tk.Tk()` + `resizable(False,False)`），
   不是无边框主程序。`probe_exe_window.py` 的那几条判据
   （非客户区 0x0 / 无 WS_CAPTION / 有托盘窗口 `TraeBuddyTrayWnd`）
   对向导**不成立**，直接跑必然 FAIL —— 那不是缺陷，是判据用错了对象。
   主程序的三份产物（onedir / onefile / 安装后目录）才用 `probe_exe_window.py`。

这里验两件事，都不改动用户系统：
  1. **向导能起来**：恰好 1 个可见 TkTopLevel，标题含「安装」，
     客户区尺寸在设计值附近（DPI 125% 下约为 700×575）。
  2. **内嵌 payload 完整**：onefile 会把自己解包到 `%TEMP%\\_MEIxxxx`，
     其中必须存在 `payload.zip`，且**字节数与 `Output/payload.zip` 一致**
     —— 这一条能抓到「打包漏了 payload / payload 被截断」这类致命问题。

用法：
    python tests/verify_installer.py [setup.exe]
退出码：0 = 全部通过；1 = 有失败项；2 = 未找到 exe（跳过）。
"""
import ctypes
import glob
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from win_tree_probe import tree  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
DEFAULT_SETUP = os.path.join(GUI_DIR, "Output", "TraeBuddy签到助手_Setup_2.6.8.exe")
PAYLOAD_SRC = os.path.join(GUI_DIR, "Output", "payload.zip")

# 设计尺寸 560x460（逻辑像素）；DPI 125% 下 Tk 会放大到约 700x575。
# ★ 2026-09-22：420 → 460 —— 向导里新增了「数据在哪 / 账号迁移 / 任务注册」
#   三行提示，内容自然高度 442 > 420，会把按钮行裁掉 22px。
DESIGN_W, DESIGN_H = 560, 460
TOL = 1.9          # 容忍到 1.9 倍（覆盖 100%~200% 缩放）

u = ctypes.windll.user32
u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u.GetWindowTextW.restype = ctypes.c_int
u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u.GetClassNameW.restype = ctypes.c_int
u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u.IsWindowVisible.argtypes = [wintypes.HWND]
u.IsWindowVisible.restype = wintypes.BOOL

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def list_windows(pids):
    out = []

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
        r, c = wintypes.RECT(), wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        u.GetClientRect(hwnd, ctypes.byref(c))
        out.append(dict(hwnd=hwnd, title=buf.value, cls=cls.value,
                        visible=bool(u.IsWindowVisible(hwnd)),
                        w=r.right - r.left, h=r.bottom - r.top,
                        cw=c.right - c.left, ch=c.bottom - c.top))
        return True

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def _mei_dirs():
    return {d for d in glob.glob(os.path.join(tempfile.gettempdir(), "_MEI*"))
            if os.path.isdir(d)}


def main():
    setup = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SETUP
    if not os.path.exists(setup):
        print("RESULT: SKIP (未找到安装包:", setup, ")")
        return 2
    setup = os.path.abspath(setup)
    print("安装包 =", setup, "%.1f MB" % (os.path.getsize(setup) / 1048576))

    want = None
    if os.path.exists(PAYLOAD_SRC):
        want = os.path.getsize(PAYLOAD_SRC)
        print("参照 payload.zip =", want, "字节")
    else:
        print("!! 找不到 Output/payload.zip，跳过 payload 尺寸比对")

    before = _mei_dirs()
    p = subprocess.Popen([setup], cwd=os.path.dirname(setup))
    print("pid:", p.pid)
    ok = True
    try:
        hwnd, info = None, None
        for attempt in range(40):          # onefile 解包 + 起 Tk，最多等 60s
            time.sleep(1.5)
            if p.poll() is not None:
                print("!! 向导进程已退出，码 =", p.returncode)
                return 1
            wins = [f for f in list_windows(set(tree(p.pid)))
                    if f["visible"] and f["cls"] == "TkTopLevel" and f["w"] > 200]
            if wins:
                hwnd, info = wins[0]["hwnd"], wins[0]
                print("[%d] 向导窗口已出现" % attempt)
                break

        # ---- 判据 1：向导窗口 ----
        if hwnd is None:
            print("[向导窗口] FAIL —— 60 秒内没有出现可见的 TkTopLevel")
            ok = False
        else:
            pids = set(tree(p.pid))
            tks = [f for f in list_windows(pids)
                   if f["cls"] == "TkTopLevel" and f["visible"]]
            print("[向导窗口] 可见 TkTopLevel 数 =", len(tks))
            print("           标题 =", repr(info["title"]))
            print("           窗口 %dx%d  客户区 %dx%d"
                  % (info["w"], info["h"], info["cw"], info["ch"]))
            if len(tks) != 1:
                print("           FAIL 期望恰好 1 个可见顶层窗口")
                ok = False
            if "安装" not in info["title"]:
                print("           FAIL 标题里没有「安装」")
                ok = False
            lo_w, hi_w = DESIGN_W * 0.9, DESIGN_W * TOL
            lo_h, hi_h = DESIGN_H * 0.9, DESIGN_H * TOL
            if not (lo_w <= info["cw"] <= hi_w and lo_h <= info["ch"] <= hi_h):
                print("           FAIL 客户区 %dx%d 偏离设计值 %dx%d 过多"
                      % (info["cw"], info["ch"], DESIGN_W, DESIGN_H))
                ok = False
            if ok:
                print("           OK")

        # ---- 判据 2：内嵌 payload ----
        new_mei = _mei_dirs() - before
        found = None
        for d in sorted(new_mei):
            z = os.path.join(d, "payload.zip")
            if os.path.exists(z):
                found = (d, z, os.path.getsize(z))
                break
        if not found:
            print("[内嵌 payload] FAIL 解包目录里没有 payload.zip"
                  "（新增解包目录 %d 个）" % len(new_mei))
            ok = False
        else:
            d, z, size = found
            print("[内嵌 payload] %s" % z)
            print("               %d 字节 (%.1f MB)" % (size, size / 1048576))
            if want is not None and size != want:
                print("               FAIL 与 Output/payload.zip 不一致"
                      "（差 %d 字节）" % (size - want))
                ok = False
            else:
                print("               OK 与参照一致")
    finally:
        # ★ 必须**按进程树**杀（`/T`），而且要**先**杀树再 terminate。
        #   这个安装包是 PyInstaller `--onefile`：引导进程 `p` 会把真正的向导
        #   拉起成**子进程**并等它退出。只 `p.terminate()` 只会杀掉引导进程，
        #   留下一个**孤儿向导窗口** —— 2026-09-19 实测连跑两次都残留一个
        #   `TraeBuddy签到助手_Setup_2.x` 进程，挡住后续 PyInstaller 重建，
        #   只能手动 taskkill。
        #   旧代码把 `taskkill /T` 放在 `except` 里，而 `terminate()` 不会抛异常
        #   → 那条兜底**从来没执行过**。
        try:
            # 只在引导进程还活着时才按 pid 杀树：onefile 的引导进程会**等**子进程
            # 退出，所以「向导还在 → 引导进程也还在」；加这道判断是为了避免
            # 引导进程已退出后 pid 被复用、误杀无关进程。
            if p.poll() is None:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            p.terminate()
            p.wait(timeout=8)
        except Exception:  # noqa: BLE001
            pass
        # 向导是普通窗口，退出后 onefile 会自行清理 _MEI；残留不强求

    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
