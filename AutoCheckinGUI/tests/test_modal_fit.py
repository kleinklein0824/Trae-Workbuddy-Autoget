#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：定尺寸弹窗里放「变长内容」时，任何一部分都不能被压扁。

真实缺陷（2.4.6 起就存在）
------------------------
`Modal` 是**定尺寸**的（`show()` 里 `geometry(f"{w}x{h}")`）。pack 在空间
不够时会**压扁**后面的孩子 —— `winfo_height()` 返回的是被压后的高度，所以
"看起来装得下"，实际内容/按钮被裁掉，且**没有任何报错**。

实测「TraeCN 令牌信息」（560x290）：

    head     req= 33  got= 33
    sep      req=  1  got=  1
    content  req=178  got=178
    footer   req= 39  got= 16   ← 被压扁 23px，「关闭」按钮下沿被切掉

对比截图：`tests/_shots/40_token_modal_no_fit.png`（切）
vs `tests/_shots/41_token_modal_fit.png`（正常）。

修法：`Modal.fit_height()`（按内容自然高度**只增不减**），在这几个弹窗的
`show()` 之前调用。注意**不能用滚动容器**代替：`ScrollFrame` 的 canvas 用
`theme.bg`，而弹窗内容是 `theme.bg_soft`（浅色下 #f1f5f9 vs #ffffff），
套进去会露出一圈灰底；而且滚动条常驻会白白缩进 10px。这里内容只有固定
几行，**长高到刚好放下**才是对的观感。

判据是**行为化的**：断言"每一部分都拿到了它需要的高度"，而不是
"调用了 fit_height"（后者是纯实现检查，装个空函数也能骗过去）。

跑法：python tests/test_modal_fit.py
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

PASS = 0
FAIL = 0
FAILS: list = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  OK   %s" % name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  FAIL %s   %s" % (name, extra))


def section(t):
    print("\n== %s ==" % t)


def settle(app, n=16):
    for _ in range(n):
        app.update()
        time.sleep(0.03)


class FakeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.module = "traecn"

    def ui_call(self, fn):
        # ⚠️ 必须丢弃：`AccountsPage.build()` → `on_show()` 会起后台扫描线程，
        # 它回来时调 `ui_call`；在这里直接 `fn()` 就是「后台线程碰 Tk」→
        # `RuntimeError: main thread is not in main loop`（Tkinter 红线 #4）。
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


PARTS = ["head", "sep", "content", "footer"]


def squeezed(m):
    """返回 [(部件名, 需要高度, 实得高度)]，只列**被压扁**的。"""
    out = []
    try:
        outer = m.winfo_children()[0]
        kids = outer.winfo_children()
    except (tk.TclError, IndexError):
        return out
    for nm, p in zip(PARTS, kids):
        try:
            need, got = p.winfo_reqheight(), p.winfo_height()
        except tk.TclError:
            continue
        if need > got + 1:
            out.append((nm, need, got))
    return out


def open_dialog(W, page, opener, disable_fit=False):
    """打开一个真实弹窗，返回 (Modal 实例, 还原函数)。"""
    created = []
    orig_modal = W.Modal
    orig_fit = W.Modal.fit_height

    class RecModal(orig_modal):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

    if disable_fit:
        # 正对照：把修复**变成空操作**，看判据会不会红
        RecModal.fit_height = lambda self, **kw: self._mh
    W.Modal = RecModal

    def restore():
        W.Modal = orig_modal
        W.Modal.fit_height = orig_fit

    opener(page)
    return (created[-1] if created else None), restore


ACC = {"display": "Klein", "uid": "3956523050480058",
       "token": "x" * 700, "has_did": True,
       "exp": time.time() + 29.9 * 86400,
       "profile": "trae1", "id": "trae1", "key": "traework:trae1"}

# 展示名是用户可控的：邮箱 + 后缀，足够长到折行
ACC_LONG = dict(ACC,
                display="klein.2571543714@qq.com（Trae CN 专业版 长期有效）")


CASES = [
    ("TraeCN 令牌信息（560x290）", lambda p: p._show_token(ACC)),
    ("移除令牌快照（430x190）", lambda p: p._remove_wb(ACC)),
    ("移除账号（430x200）", lambda p: p._remove_traework(ACC)),
    ("手动新增 TraeWork 账号（470x270）", lambda p: p._add_traework()),
]

LONG_CASES = [
    ("TraeCN 令牌信息 / 长展示名", lambda p: p._show_token(ACC_LONG)),
    ("移除令牌快照 / 长展示名", lambda p: p._remove_wb(ACC_LONG)),
    ("移除账号 / 长展示名", lambda p: p._remove_traework(ACC_LONG)),
]


def run_cases(app, W, cases, disable_fit, label):
    from app.pages.accounts import AccountsPage
    from app.theme import Theme

    for tag, opener in cases:
        page = AccountsPage(app, Theme(False), app, "traecn")
        page.pack(fill="both", expand=True)
        settle(app, 8)
        m, restore = open_dialog(W, page, opener, disable_fit)
        try:
            settle(app, 18)
            if m is None:
                check(f"{label}｜{tag}：弹窗已打开", False, "未创建")
                continue
            sq = squeezed(m)
            detail = ("全部部件都拿到需要的高度"
                      if not sq else
                      "被压扁：" + "、".join(f"{n} {a}->{b}" for n, a, b in sq))
            check(f"{label}｜{tag}：没有被压扁的部件", not sq, detail)
            # 顺带把尺寸打出来，方便看修复前后差多少
            print(f"       （几何 {m.winfo_width()}x{m.winfo_height()}，{detail}）")
            m.close()
            settle(app, 6)
        finally:
            restore()
            page.destroy()
            settle(app, 6)


def main():
    from app.ui import widgets as W
    from app.theme import Theme

    app = FakeApp()
    app.deiconify()
    app.geometry("1180x760+120+80")
    settle(app, 12)

    # ---------- A) 现实数据：四个弹窗都不许压扁 ----------
    section("A) 现实数据：弹窗各部分都不能被压扁")
    run_cases(app, W, CASES, False, "现实值")

    # ---------- B) 用户可控的长展示名 ----------
    section("B) 长展示名（用户可控）：同样不能被压扁")
    run_cases(app, W, LONG_CASES, False, "长昵称")

    # ---------- C) 正对照：关掉修复，判据必须变红 ----------
    section("C) 正对照：把 fit_height 变成空操作，必须测出压扁")
    page = None
    try:
        from app.pages.accounts import AccountsPage
        page = AccountsPage(app, Theme(False), app, "traecn")
        page.pack(fill="both", expand=True)
        settle(app, 8)
        m, restore = open_dialog(W, page, lambda p: p._show_token(ACC),
                                 disable_fit=True)
        settle(app, 18)
        try:
            if m is None:
                check("正对照：弹窗已打开", False, "未创建")
            else:
                sq = squeezed(m)
                names = [n for n, _a, _b in sq]
                check("正对照：确实测出了被压扁的部件（否则判据是瞎的）",
                      bool(sq), "没有压扁 —— 判据可能失效")
                check("正对照：被压扁的是 footer（「关闭」按钮所在）",
                      "footer" in names, f"压扁的是 {names}")
                print(f"       （压扁明细：{sq}）")
                m.close()
                settle(app, 6)
        finally:
            restore()
    finally:
        if page is not None:
            page.destroy()
            settle(app, 6)

    # ---------- D) fit_height 的语义 ----------
    section("D) fit_height 语义：只增不减 / 受上限约束 / 内容小则不动")
    m = W.Modal(app, Theme(False), "语义测试", width=400, height=500)
    tk.Label(m.content, text="很短", font=Theme(False).f_small,
             bg=Theme(False).bg_soft).pack(anchor="w")
    settle(app, 6)
    h = m.fit_height()
    check("内容远小于基准高度时保持基准（不缩）", h == 500, f"得到 {h}")
    m.close()
    settle(app, 6)

    m = W.Modal(app, Theme(False), "语义测试", width=400, height=200)
    tk.Label(m.content, text="很长的内容" * 60, font=Theme(False).f_small,
             bg=Theme(False).bg_soft, wraplength=340, justify="left").pack(
        anchor="w")
    settle(app, 6)
    h = m.fit_height(max_h=320)
    check("内容超高时向上长高", h > 200, f"得到 {h}")
    check("长高受 max_h 约束", h <= 320, f"得到 {h}")
    m.close()
    settle(app, 6)

    m = W.Modal(app, Theme(False), "语义测试", width=400, height=200)
    tk.Label(m.content, text="很长的内容" * 60, font=Theme(False).f_small,
             bg=Theme(False).bg_soft, wraplength=340, justify="left").pack(
        anchor="w")
    settle(app, 6)
    h = m.fit_height(min_h=460)
    check("min_h 可以抬高下限", h >= 460, f"得到 {h}")
    m.close()
    settle(app, 6)

    app.destroy()

    print("\n---")
    print("弹窗自适应高度：%d OK / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
