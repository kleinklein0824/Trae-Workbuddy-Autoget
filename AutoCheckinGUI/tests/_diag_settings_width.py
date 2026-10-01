#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读诊断：设置页内容**横向**是否溢出视口（截图看着像被裁，要用数字定性）。

判据（都是「比大小」，不看截图）：
  A. 卡片宽 vs 滚动画布宽
  B. 卡片内层 body 宽 vs 卡片画布宽
  C. 每个 Label 的 `winfo_reqwidth` vs body 宽（超出即横向溢出）
  D. 对照组：另一个卡片（平台开关）是否同样溢出
     —— 只有凭据卡溢出 = 本次改动引入；两个都溢出 = 页面级既有问题。
"""
import os
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
_PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
             else os.path.dirname(os.path.dirname(GUI_DIR)))
for _p in (_PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def settle(root, n=12):
    for _ in range(n):
        try:
            root.update_idletasks()
            root.update()
        except tk.TclError:
            return


def labels_in(w):
    out = []
    for c in w.winfo_children():
        if c.winfo_class() == "Label":
            try:
                t = str(c.cget("text"))
            except tk.TclError:
                t = ""
            out.append((c, t))
        out.extend(labels_in(c))
    return out


def main():
    from app.application import App
    from app.ui import widgets as W

    app = App()
    app.geometry("1180x900+80+40")
    app.deiconify()
    settle(app, 20)
    app.goto("traework", "settings")
    settle(app, 20)
    # 页面上有延迟的 after 回调（如任务状态刷新）可能把页面又切回概览，
    # 所以重试几次直到确实是设置页。
    for _ in range(6):
        if app.page is not None and type(app.page).__name__ == "SettingsPage":
            break
        app.goto("traework", "settings")
        settle(app, 12)
    page = app.page
    if page is None or type(page).__name__ != "SettingsPage":
        print("!! 没找到 SettingsPage（当前 %r）" % (type(page).__name__,))
        return 1

    cv = page.scroll.canvas
    inner = page.scroll.inner
    print("窗口 %dx%d" % (app.winfo_width(), app.winfo_height()))
    print("滚动画布宽 = %d ; inner 宽 = %d ; scrollregion=%s"
          % (cv.winfo_width(), inner.winfo_width(), cv.cget("scrollregion")))
    print("body 宽 = %d ; body reqwidth = %d"
          % (page.body.winfo_width(), page.body.winfo_reqwidth()))

    cards = [w for w in page.body.winfo_children() if isinstance(w, W.Card)]
    print("\n共 %d 张卡片" % len(cards))
    for i, card in enumerate(cards):
        title = ""
        for _c, t in labels_in(card.body)[:1]:
            title = t[:16]
        cw, bw = card._cv.winfo_width(), card.body.winfo_width()
        print("\n[%d] %-18s 卡片宽=%d  卡片画布宽=%d  body宽=%d  body_req宽=%d"
              % (i, title, card.winfo_width(), cw, bw, card.body.winfo_reqwidth()))
        if bw > cw + 4:
            print("     !! body 比卡片画布宽 %d px（超出）" % (bw - cw))
        over = []
        for c, t in labels_in(card.body):
            try:
                req, got = c.winfo_reqwidth(), c.winfo_width()
                wrap = int(c.cget("wraplength") or 0)
            except tk.TclError:
                continue
            if got > bw + 4 or req > bw + 4:
                over.append((t[:30], req, got, wrap))
        if over:
            print("     !! %d 个 Label 横向超出 body 宽 %d：" % (len(over), bw))
            for t, req, got, wrap in over[:4]:
                print("        req=%d got=%d wraplength=%d  %r"
                      % (req, got, wrap, t))
        else:
            print("     OK 卡片内无横向溢出")

    # 凭据卡逐行 dump：wraplength 必须跟着实际宽度走，否则文字会被右边缘裁掉
    print("\n=== 凭据卡逐行（wraplength 应 ≈ width）===")
    for i, card in enumerate(cards):
        t0 = (labels_in(card.body) or [(None, "")])[0][1]
        if "账号凭据" not in t0:
            continue
        for c, t in labels_in(card.body):
            try:
                req, got = c.winfo_reqwidth(), c.winfo_width()
                wrap = int(c.cget("wraplength") or 0)
            except tk.TclError:
                continue
            bad = "  <<< wraplength 未跟上" if (wrap and wrap > got + 8) else ""
            print("   req=%4d got=%4d wrap=%4d%s  %r"
                  % (req, got, wrap, bad, t[:34]))

    app.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
