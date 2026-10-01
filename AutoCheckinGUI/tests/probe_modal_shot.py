#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""截图对比：「TraeCN 令牌信息」弹窗在 fit_height 前后，底部按钮是否被切。

几何数字说 footer 只分到 16px 而按钮需要 39px —— 但"被切"是个视觉结论，
必须看图确认（本项目有过"靠肉眼误判"的教训，所以这里两个方向都验：
数字 + 截图）。

跑法：python tests/probe_modal_shot.py
"""
from __future__ import annotations

import os
import sys
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SHOTS = os.path.join(HERE, "_shots")


class FakeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.module = "traecn"

    def ui_call(self, fn):
        pass

    def toast(self, k, t, ms=3200):
        pass

    def accounts(self):
        return {"traework": [], "workbuddy": [], "traecn": []}

    def refresh_accounts(self, on_done=None):
        if on_done:
            on_done()

    def goto(self, m, v):
        pass

    def _refresh_topbar(self):
        pass


def settle(app, n=16):
    for _ in range(n):
        app.update()
        time.sleep(0.03)


ACC = {"display": "Klein", "uid": "3956523050480058",
       "token": "x" * 700, "has_did": True,
       "exp": time.time() + 29.9 * 86400}


def shot(app, W, name, disable_fit):
    from PIL import ImageGrab
    from app.pages.accounts import AccountsPage
    from app.theme import Theme

    page = AccountsPage(app, Theme(False), app, "traecn")
    page.pack(fill="both", expand=True)
    settle(app, 10)

    created = []
    orig_modal, orig_fit = W.Modal, W.Modal.fit_height

    class RecModal(orig_modal):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

    if disable_fit:
        RecModal.fit_height = lambda self, **kw: self._mh
    W.Modal = RecModal
    try:
        page._show_token(ACC)
        settle(app, 20)
        m = created[-1]
        x, y = m.winfo_rootx(), m.winfo_rooty()
        w, h = m.winfo_width(), m.winfo_height()
        os.makedirs(SHOTS, exist_ok=True)
        path = os.path.join(SHOTS, name)
        img = ImageGrab.grab()
        sw = app.winfo_screenwidth()
        k = img.size[0] / sw if sw else 1.0
        # 多抓 10px 下边，好看清"按钮是不是被切掉"
        img.crop((int(x * k), int(y * k),
                  int((x + w) * k), int((y + h + 10) * k))).save(path)
        print(f"  {name}: 弹窗 {w}x{h}  → {path}")
        m.close()
        settle(app, 6)
    finally:
        W.Modal = orig_modal
        W.Modal.fit_height = orig_fit
        page.destroy()
        settle(app, 6)
    return path


def main():
    from app.ui import widgets as W

    app = FakeApp()
    app.deiconify()
    app.geometry("1180x760+120+80")
    settle(app, 14)

    print("== 截图对比：令牌信息弹窗底部按钮 ==")
    shot(app, W, "40_token_modal_no_fit.png", True)     # 现状：290 高
    shot(app, W, "41_token_modal_fit.png", False)       # 修复：313 高

    app.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
