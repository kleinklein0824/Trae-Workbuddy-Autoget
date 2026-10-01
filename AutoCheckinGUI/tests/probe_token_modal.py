#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测「TraeCN 令牌信息」模态框（`accounts.py:_show_token`）会不会溢出。

背景：`Modal` 是**定尺寸**的（这里 560x290），自身**不带滚动容器**。
「账号池明细」已经因此踩过一次 —— 7 个账号就溢出 +123px，多出来的既看不到
也滚不动。`_show_token` 结构上同源：5 行、每行一个 `wraplength=380` 的值
Label（`uid` 和脱敏令牌用 `f_mono`），行高随内容变长而变。

本脚本只**测量**，不改任何产品代码，也不碰真实 `config.json`
（账号字典是现造的，只喂给 `_show_token`）。

跑法：python tests/probe_token_modal.py
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

# 一个"像真的"的 JWT：3 段、总长约 700 字符（实测本机 IDE 日志里就是这个量级）
JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
       + "eyJzdWIiOiIzOTU2NTIzMDUwNDgwMDU4IiwibmFtZSI6IktsZWluIiwiaWF0Ijox"
       + "NzI2NDAwMDAwLCJleHAiOjE3NTc5MzYwMDAsImF1ZCI6ImNsb3VkLWNsaWVudCIs"
       + "ImlzcyI6ImFwaS50cmFlLmNuIiwic2NvcGUiOiJ1c2VyIn0."
       + "sR7kQ2mXp9vT1nL4bY8cD3fG6hJ0kM5pQ7rS2tU9vW4xZ1aB8cE6fH3jK5mN7pR9sT2"
       + "uV4wX6yA8bC1dE3fG5hJ7kL9mN2pQ4rS6tU8vW1xY3zA5bC7dE9fG2hJ4kL6mN8p")


class FakeApp(tk.Tk):
    """只实现 `_show_token` 用得到的接口。"""

    def __init__(self):
        super().__init__()
        self.withdraw()
        self.module = "traecn"

    def ui_call(self, fn):
        # ⚠️ 必须**丢弃**回调，不能在调用线程里直接执行。
        # `AccountsPage.build()` → `on_show()` 会起一个 `acct-scan` 后台线程，
        # 它回来时调 `ui_call`；在这里 `fn()` 就等于「后台线程碰 Tk」→
        # `RuntimeError: main thread is not in main loop`（Tkinter 红线 #4）。
        # 探测只关心 `_show_token` 的几何，扫描结果无关紧要。
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


def deep_find(root, pred):
    out = []
    try:
        for w in root.winfo_children():
            if pred(w):
                out.append(w)
            out.extend(deep_find(w, pred))
    except Exception:  # noqa: BLE001
        pass
    return out


def measure(page, W, created, acc, tag):
    """打开 `_show_token` 并返回 (是否溢出, 溢出像素, 可滚容器数)。

    ⚠️ 不能读 `page._modal` —— 那是 **api_service.py 私有**的约定，
    `_show_token` 不设它。这里靠临时包一层 `W.Modal` 来抓实例。
    """
    created.clear()
    page._show_token(acc)
    m = created[-1] if created else None
    settle(page.app, 20)
    if m is None:
        print(f"  {tag}: 模态框未创建")
        return None

    content = m.content
    content.update_idletasks()

    # ★ 判据：`content.winfo_reqheight()`（孩子真正需要的高度）vs
    # `winfo_height()`（实际分到的）。
    #
    # ⚠️ **别用「最后一个孩子的下边缘」当判据** —— 会被骗：空间不够时 pack
    # 会把后面的孩子**压扁**（`winfo_height()` 返回被压后的高度），所以
    # 「y + height」永远不超过容器，看起来永远不溢出。
    # 实测正对照（300 字符昵称）：reqhigh 280 / height 208，明显裁掉 72px，
    # 而下边缘法算出的是 -3px（假阴性）。
    need = content.winfo_reqheight()
    got = content.winfo_height()
    over = need - got

    # 弹窗里有没有"真的能滚"的容器（行为化判据，不看类型）
    scrollable = []
    for w in deep_find(m, lambda x: hasattr(x, "canvas")):
        cv = getattr(w, "canvas", None)
        if cv is not None and W._can_scroll_canvas(cv):
            scrollable.append(w)

    print(f"  {tag}:")
    print(f"      弹窗几何        = {m.winfo_width()}x{m.winfo_height()}"
          f"  (期望 560x290)")
    print(f"      内容需求/实得   = {need} / {got} px   → 溢出 {over:+d} px")
    print(f"      可滚容器        = {len(scrollable)}")
    verdict = "溢出且够不着（缺陷）" if over > 2 and not scrollable else "OK"
    print(f"      判定            = {verdict}")
    m.close()
    settle(page.app, 6)
    return over, len(scrollable)


def sweep(page, W, created, key, lengths, tag, filler="x"):
    """扫 `key` 的长度，找出「开始溢出」的临界点。

    溢出不看字符数而看**渲染宽度是否超过 `wraplength=380`** —— 只有换行才会
    把行高顶起来。ASCII 与中文的字宽差一倍，所以 `filler` 要分开传。
    """
    print(f"  {tag}")
    first_bad = None
    for n in lengths:
        acc = {"display": "Klein", "uid": "3956523050480058",
               "token": JWT, "has_did": True,
               "exp": time.time() + 29.9 * 86400}
        acc[key] = filler * n
        created.clear()
        page._show_token(acc)
        m = created[-1] if created else None
        settle(page.app, 12)
        if m is None:
            print(f"      n={n:>4}  模态框未创建")
            continue
        need = m.content.winfo_reqheight()
        got = m.content.winfo_height()
        over = need - got
        flag = "← 溢出" if over > 2 else ""
        print(f"      n={n:>4}  reqhigh={need:>4}  got={got:>4}"
              f"  over={over:+5d}  {flag}")
        if over > 2 and first_bad is None:
            first_bad = n
        m.close()
        settle(page.app, 4)
    if first_bad is None:
        print(f"      → 扫到 {lengths[-1]} 仍未溢出")
    else:
        print(f"      → 从 n={first_bad} 起开始溢出")
    return first_bad


CASES = [
    ("① 现实值（短昵称 / 16 位 uid / 真 JWT）", {
        "display": "Klein", "uid": "3956523050480058",
        "token": JWT, "has_did": True, "exp": time.time() + 29.9 * 86400,
    }),
    ("② 长昵称（邮箱式 40 字符）", {
        "display": "klein.2571543714@qq.com",
        "uid": "3956523050480058",
        "token": JWT, "has_did": True, "exp": time.time() + 29.9 * 86400,
    }),
    ("③ uid 是 UUID（36 字符）", {
        "display": "Klein", "uid": "83992452-51ec-4353-9399-98591a64f25a",
        "token": JWT, "has_did": True, "exp": time.time() + 29.9 * 86400,
    }),
    ("④ 最坏组合：长昵称 + UUID + 40 字符令牌（不脱敏）", {
        "display": "klein.2571543714@qq.com",
        "uid": "83992452-51ec-4353-9399-98591a64f25a",
        "token": "x" * 40,          # len == 40 → masked 分支不生效，整串显示
        "has_did": False, "exp": 0,
    }),
    # ↓ 正对照：这条**必须**报溢出。如果它报 OK，说明测量方法本身是瞎的，
    #   上面 4 条的 "OK" 就毫无意义（探测仪器的自检，比结论更重要）。
    ("⑤ 正对照：300 字符昵称（必须报溢出）", {
        "display": "很长的账号昵称" * 30,
        "uid": "83992452-51ec-4353-9399-98591a64f25a",
        "token": "x" * 40, "has_did": False, "exp": 0,
    }),
]


def main():
    from app.pages.accounts import AccountsPage
    from app.theme import Theme
    from app.ui import widgets as W

    app = FakeApp()
    app.deiconify()
    app.geometry("1180x760+120+80")
    page = AccountsPage(app, Theme(False), app, "traecn")
    page.pack(fill="both", expand=True)
    settle(app, 14)

    # 抓 `_show_token` 造出来的 Modal 实例（它不写 page._modal）
    created = []
    orig_modal = W.Modal

    class RecModal(orig_modal):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

    W.Modal = RecModal

    print("== 「TraeCN 令牌信息」模态框溢出探测 ==")
    print(f"   （Modal 定尺寸 560x290，自身不带滚动容器）\n")

    bad = []
    for tag, acc in CASES:
        r = measure(page, W, created, acc, tag)
        if r is None:
            bad.append(tag)
            continue
        over, nscroll = r
        if over > 2 and nscroll == 0:
            bad.append(tag)
        print()

    print("== 临界点扫描（哪个字段长到什么程度才会溢出）==")
    sweep(page, W, created, "display",
          [10, 20, 30, 40, 50, 60, 70, 80], "display（f_small，非等宽）")
    print()
    sweep(page, W, created, "uid",
          [16, 24, 32, 40, 48, 56, 64], "uid（f_mono，等宽）")
    print()
    sweep(page, W, created, "display",
          [5, 10, 15, 20, 25, 30], "display（中文昵称）", filler="签")

    W.Modal = orig_modal

    print("---")
    if bad:
        print(f"结论：{len(bad)} 个场景溢出且无滚动容器 → 需要修：")
        for b in bad:
            print(f"  - {b}")
    else:
        print("结论：所有场景都不溢出（或已有可滚容器）→ 无需修改。")
    app.destroy()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
