#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只截「账号凭据自动刷新」卡片（亮/暗各一张），滚到它再裁它。

★ 抓图前必须 `lift() + focus_force() + settle()`：`ImageGrab.grab()` 抓的是
  **屏幕**，窗口被别的窗口压住时会抓到别人的内容 —— Tk 层断言全真、只有 PNG 是错的。
  所以这里先抢焦点、再多次 pump 等它真的到前台，最后才 grab。
  结论仍以 `probe_cred_card.py` 的数值断言为准，本脚本只做**观感确认**。

★★ 坐标必须走 `_shot_util`（2026-09-18 修）
------------------------------------------------
本机真实屏幕 1920x1080、系统缩放 125%，而 Tk 进程 `GetProcessDpiAwareness
= UNAWARE` → 被 Windows **DPI 虚拟化**：`winfo_rootx/width` 全是**虚拟坐标**
（1536x864 体系），而 `ImageGrab.grab(bbox=...)` 走 GDI 按**物理像素**裁剪。
本脚本原先直接 `grab(bbox=(winfo_rootx, ...))` → 只抓到窗口**左上 80%** 区域，
右侧 20% 落在图外，**看起来就像"卡片文字被裁"**，白查了好几轮。
（旧注释里"实测卡片宽 922 vs 实际 ~1140"也是同一根因：1140 ≈ 922 × 1.25。）

正确做法：`_shot_util.grab_window(app)` —— 它用
`全屏抓图宽 / winfo_screenwidth()` 算出真实缩放比再换算。
**别用** `winfo_fpixels('1i')/96` 或 `GetDpiForWindow()`：被虚拟化的进程里
它们同样返回 96（看着"缩放 1.0"，是假的）。
"""
import os
import sys
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
_PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
             else os.path.dirname(os.path.dirname(GUI_DIR)))
for _p in (HERE, _PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

OUT = os.path.join(HERE, "_shots")
TITLE = "账号凭据自动刷新"


def settle(root, n=10, dt=0.05):
    for _ in range(n):
        try:
            root.update_idletasks()
            root.update()
        except tk.TclError:
            return
        time.sleep(dt)


def find_card(page):
    for w in page.body.winfo_children():
        if not hasattr(w, "body"):
            continue
        try:
            if any(str(s.cget("text")) == TITLE for s in w.body.winfo_children()):
                return w
        except tk.TclError:
            pass
    return None


def main():
    from app.application import App
    from app.ui import widgets as W

    os.makedirs(OUT, exist_ok=True)
    app = App()
    app.geometry("1180x900+80+40")
    app.deiconify()
    settle(app, 20)

    saved = []
    for dark in (False, True):
        if dark:
            app.set_theme("dark", persist=False)
            settle(app, 14)
        app.goto("traework", "settings")
        settle(app, 20)
        page = None
        for p in (app.pages.values() if hasattr(app, "pages") else []):
            if type(p).__name__ == "SettingsPage":
                page = p
        if page is None:
            page = app.page
        card = find_card(page)
        if card is None:
            print("!! 没找到卡片（%s）" % ("dark" if dark else "light"))
            continue

        # 把卡片滚到视口顶部
        try:
            canvas = page.scroll.canvas
            total = float(canvas.bbox("all")[3])
            y = card.winfo_y()
            canvas.yview_moveto(max(0.0, (y - 24) / total))
        except Exception as e:  # noqa: BLE001
            print("!! 滚动失败：%r" % (e,))
        settle(app, 16)

        app.lift()
        try:
            app.focus_force()
        except tk.TclError:
            pass
        settle(app, 16)

        import _shot_util as SU
        # ★ 本窗口是**无边框**的（overrideredirect），所以 `winfo_rootx/rooty`
        #   就是窗口左上角 —— 但**尺寸单位仍是虚拟坐标**，必须乘 DPI 缩放比，
        #   否则只抓到左上 80%（右侧文字落在图外，看着像被裁）。
        wx, wy = app.winfo_rootx(), app.winfo_rooty()
        ww, wh = app.winfo_width(), app.winfo_height()
        k = SU.dpi_scale(app)
        print("      窗口 (%d,%d) %dx%d；卡片 rootx=%d rooty=%d %dx%d；"
              "DPI 缩放比=%.3f"
              % (wx, wy, ww, wh, card.winfo_rootx(), card.winfo_rooty(),
                 card.winfo_width(), card.winfo_height(), k))
        img = SU.grab_window(app, k)
        # 正对照：图宽必须 ≈ 逻辑宽 × 缩放比；若等于逻辑宽就说明漏乘了
        expect_w = int(ww * k)
        if abs(img.width - expect_w) > 2:
            print("      !! 图宽 %d ≠ 预期 %d（可能漏乘 DPI 缩放比）"
                  % (img.width, expect_w))
        name = "cred_card_%s.png" % ("dark" if dark else "light")
        path = os.path.join(OUT, name)
        img.save(path)
        saved.append(path)
        print("已保存 %s  (%dx%d)" % (name, img.width, img.height))

    app.destroy()
    print("\n".join(saved))


if __name__ == "__main__":
    main()
