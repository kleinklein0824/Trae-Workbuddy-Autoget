#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断 `Modal` 固定高度下「谁被压扁了」。

起因：`Modal.fit_height()` 让「TraeCN 令牌信息」从 290 长到 313 —— 说明
自然布局本来就是 313，290 这个硬编码高度**从一开始就在压某个部分**。
压的是 head？content？还是 footer（那就会切到按钮）？

本脚本只测量，不改产品代码。
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


class FakeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.module = "traecn"

    # `AccountsPage.build()` → `on_show()` → `reload()` 会用到这几个接口
    def ui_call(self, fn):
        pass          # 后台扫描线程回来时不能碰 Tk（见 probe_token_modal 注释）

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


def report(app, W, theme, w, h, filler_h):
    m = W.Modal(app, theme, "测试标题", width=w, height=h)
    tk.Label(m.content, text="内容占位", font=theme.f_small,
             bg=theme.bg_soft).pack(anchor="w")
    m.show()
    settle(app)
    outer = m.winfo_children()[0]
    parts = outer.winfo_children()          # head, 分隔线, content, footer
    names = ["head", "sep", "content", "footer"]

    print(f"\n  弹窗 {w}x{h}  (内容占位 {filler_h}px)")
    print(f"    toplevel 自然高度 winfo_reqheight = {m.winfo_reqheight()}"
          f"   实际高度 = {m.winfo_height()}")
    total_need = 0
    for nm, p in zip(names, parts):
        try:
            need = p.winfo_reqheight()
            got = p.winfo_height()
        except tk.TclError:
            continue
        squeezed = "  ← 被压扁 %d px" % (need - got) if need > got + 1 else ""
        print(f"    {nm:<8} req={need:>4}  got={got:>4}{squeezed}")
        total_need += need
    print(f"    chrome 合计 req = {total_need}（含 1px 分隔线）")
    print(f"    加上外层 padx/pady=1 边框 → 自然高度应 ≥ {total_need + 2}")
    m.close()
    settle(app, 6)
    return m


def dump(m, tag):
    """打印一个真实 Modal 的 head / sep / content / footer 的 req vs got。"""
    outer = m.winfo_children()[0]
    parts = outer.winfo_children()
    names = ["head", "sep", "content", "footer"]
    print(f"\n  {tag}")
    print(f"    几何={m.winfo_width()}x{m.winfo_height()}"
          f"   toplevel.reqheight={m.winfo_reqheight()}"
          f"   outer.reqheight={outer.winfo_reqheight()}")
    for nm, p in zip(names, parts):
        try:
            need, got = p.winfo_reqheight(), p.winfo_height()
        except tk.TclError:
            continue
        flag = "  ← 被压扁 %d" % (need - got) if need > got + 1 else ""
        print(f"    {nm:<8} req={need:>4}  got={got:>4}{flag}")


def real_dialog(app, W, tag, opener, disable_fit):
    """用真实页面方法造弹窗，比较「不调用 fit_height」与「调用」。"""
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
        opener(page)
        settle(app, 16)
        if created:
            dump(created[-1], tag)
            created[-1].close()
            settle(app, 6)
    finally:
        W.Modal = orig_modal
        W.Modal.fit_height = orig_fit
        page.destroy()
        settle(app, 6)


ACC = {"display": "Klein", "uid": "3956523050480058",
       "token": "x" * 700, "has_did": True,
       "exp": time.time() + 29.9 * 86400,
       "profile": "trae1", "id": "trae1", "key": "traework:trae1"}

ACC_LONG = dict(ACC, display="klein.2571543714@qq.com（Trae CN 专业版 长期有效）")


def main():
    from app.theme import Theme
    from app.ui import widgets as W

    app = FakeApp()
    app.deiconify()
    app.geometry("1180x760+120+80")
    settle(app, 12)

    cases = [
        ("令牌信息 560x290", lambda p: p._show_token(ACC)),
        ("移除令牌快照 430x190", lambda p: p._remove_wb(ACC)),
        ("移除账号 430x200", lambda p: p._remove_traework(ACC)),
        ("手动新增 470x270", lambda p: p._add_traework()),
    ]

    print("== 真实弹窗：固定高度下各部分是否被压扁 ==")
    for tag, opener in cases:
        print(f"\n--- {tag} ---")
        real_dialog(app, W, "① 不调用 fit_height（现状）", opener, True)
        real_dialog(app, W, "② 调用 fit_height", opener, False)

    print("\n\n== 长展示名（用户可控）==")
    for tag, opener in [
        ("令牌信息 / 长昵称", lambda p: p._show_token(ACC_LONG)),
        ("移除令牌快照 / 长昵称", lambda p: p._remove_wb(ACC_LONG)),
        ("移除账号 / 长昵称", lambda p: p._remove_traework(ACC_LONG)),
    ]:
        print(f"\n--- {tag} ---")
        real_dialog(app, W, "① 不调用 fit_height（现状）", opener, True)
        real_dialog(app, W, "② 调用 fit_height", opener, False)

    app.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
