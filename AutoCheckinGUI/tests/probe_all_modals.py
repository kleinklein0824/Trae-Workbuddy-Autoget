#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全量弹窗审计：把**每一个** `W.Modal` 实例化点都打开一遍，量「有没有部件被压扁」。

为什么单独写这个
---------------
`test_modal_fit` 只覆盖了 `accounts.py` 里的 4 个弹窗，`verify_api_modals`
覆盖「账号池明细」。但全项目一共有 **8 处** `W.Modal(...)` 调用，
剩下 3 个（「退出应用」、「⚠ 高风险操作确认」、设置页 `_ask_confirm`）
**从来没被任何回归打开过** —— 而它们恰恰是同一类缺陷的高危区：
定尺寸 `Modal` + 用户/配置可控的变长文本。

判据直接复用 `test_modal_fit.squeezed()`（**单一来源**）：
比 `winfo_reqheight()`（孩子真正需要的高度）vs `winfo_height()`（实际分到的）。
⚠️ 不能用「最后一个孩子的下边缘」当判据 —— pack 会把孩子压扁，
`winfo_height()` 返回的是被压后的值，那种判据**天生是瞎的**。

跑法：python tests/probe_all_modals.py
"""
from __future__ import annotations

import os
import sys
import time
import tkinter as tk
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

# 判据与假 App 都从既有回归复用，避免两处定义各自漂移
from test_modal_fit import FakeApp, settle, squeezed  # noqa: E402

ACC = {"display": "Klein", "uid": "3956523050480058",
       "token": "x" * 700, "has_did": True,
       "exp": time.time() + 29.9 * 86400,
       "profile": "trae1", "id": "trae1", "key": "traework:trae1"}

ACC_LONG = dict(ACC,
                display="klein.2571543714@qq.com（Trae CN 专业版 长期有效）")

# 设置页 `_confirm_reset_code` 的实际 body（全项目最长的一段）
BODY_RESET = ("将对本机全部 Trae 系列目录执行：\n\n"
              "  · 轮换 machineid 与 storage.json telemetry 标识\n"
              "  · 清除 storage.json 全部登录凭据键（强制登出所有账号）\n"
              "  · 每个文件自动生成带时间戳的备份\n\n"
              "仅操作用户数据文件，无需管理员权限。\n"
              "请先完全关闭 Trae 再执行；执行后需重新登录账号。")


def make_page(mod_name, cls_name, module):
    """返回一个构造页面的函数。

    ⚠️ 模块名和类名是**两回事**：`app.pages.accounts` 是模块，
    `AccountsPage` 是里面的类。第一版把类名当模块名去 `import_module`
    → 全部 `ModuleNotFoundError`（11 个弹窗一个都没测到，却因为
    "打开失败"也计入问题，看起来像 11 个真缺陷）。这里显式分开。
    """
    def build(app, theme):
        from importlib import import_module
        mod = import_module("app.pages." + mod_name)
        cls = getattr(mod, cls_name)
        page = cls(app, theme, app, module)
        page.pack(fill="both", expand=True)
        return page
    return build


class FakeHost(tk.Tk):
    """给 `App._on_close` 用的最小宿主。

    `_on_close` 是 **App** 上的方法（不是页面的），直接调未绑定函数
    `App._on_close(host)`，host 只需有 `.theme` / `.page` / `.dark`。

    ⚠️ **必须 deiconify + 给几何**，不能只 `withdraw()`：
    `Modal.show()` 会调 `self.transient(master)`，而 Windows 下
    **transient 子窗会跟着被隐藏的父窗一起隐藏** → 弹窗根本没映射，
    量到的几何是 `1x1`、每个部件都被"压扁"成 1px —— 那是**探测假象**，
    不是缺陷。第一版就是这么误报了「退出应用」。
    """

    def __init__(self, theme):
        super().__init__()
        self.theme = theme
        self.dark = False
        self.page = None
        self.geometry("1180x760+120+80")
        self.deiconify()


def cases():
    """返回 [(标签, 开弹窗的函数)]；函数签名 (app, W, T) -> (modal, cleanup)。"""
    out = []

    # ---- accounts.py 的 4 个（已被 test_modal_fit 覆盖，这里做交叉确认）----
    def acc_page(app, T):
        return make_page("accounts", "AccountsPage", "traecn")(app, T)

    def mk_acc(label, method, arg):
        def go(app, W, T):
            page = acc_page(app, T)
            settle(app, 8)
            created = []

            class Rec(W.Modal):
                def __init__(self, *a, **kw):
                    super().__init__(*a, **kw)
                    created.append(self)

            orig, W.Modal = W.Modal, Rec
            try:
                getattr(page, method)(arg) if arg is not None \
                    else getattr(page, method)()
            finally:
                W.Modal = orig
            return (created[-1] if created else None), page
        return (label, go)

    out.append(mk_acc("① TraeCN 令牌信息 560x290", "_show_token", ACC))
    out.append(mk_acc("② 移除令牌快照 430x190", "_remove_wb", ACC))
    out.append(mk_acc("③ 移除账号 430x200", "_remove_traework", ACC))
    out.append(mk_acc("④ 手动新增 TraeWork 470x270", "_add_traework", None))
    out.append(mk_acc("⑤ TraeCN 令牌信息 / 长展示名", "_show_token", ACC_LONG))

    # ---- api_service.py ----
    def go_pool(app, W, T):
        page = make_page("api_service", "ApiServicePage", "api-service")(app, T)
        settle(app, 10)
        created = []

        class Rec(W.Modal):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                created.append(self)

        orig, W.Modal = W.Modal, Rec
        try:
            page._show_pool_detail()
        finally:
            W.Modal = orig
        return (created[-1] if created else None), page

    out.append(("⑥ 账号池明细 560x420", go_pool))

    def go_risky(key):
        def go(app, W, T):
            page = make_page("api_service", "ApiServicePage", "api-service")(app, T)
            settle(app, 10)
            created = []

            class Rec(W.Modal):
                def __init__(self, *a, **kw):
                    super().__init__(*a, **kw)
                    created.append(self)

            orig, W.Modal = W.Modal, Rec
            try:
                page._run_action(key)
            finally:
                W.Modal = orig
            return (created[-1] if created else None), page
        return go

    out.append(("⑦ 高风险确认 480x250 (reset_device)", go_risky("reset_device")))
    out.append(("⑧ 高风险确认 480x250 (clear_state)", go_risky("clear_state")))

    # ---- settings.py ----
    def go_settings(title, body):
        def go(app, W, T):
            page = make_page("settings", "SettingsPage", "traework")(app, T)
            settle(app, 10)
            created = []

            class Rec(W.Modal):
                def __init__(self, *a, **kw):
                    super().__init__(*a, **kw)
                    created.append(self)

            orig, W.Modal = W.Modal, Rec
            try:
                page._ask_confirm(title, body, lambda: None)
            finally:
                W.Modal = orig
            return (created[-1] if created else None), page
        return go

    out.append(("⑨ 设置页确认框 520x340（最长 body）",
                go_settings("确认重置设备码", BODY_RESET)))

    # ---- application.py ----
    def go_close(busy):
        def go(app, W, T):
            from app.application import App
            host = FakeHost(T)
            if busy:
                host.page = SimpleNamespace(busy=True)
            settle(host, 4)
            created = []

            class Rec(W.Modal):
                def __init__(self, *a, **kw):
                    super().__init__(*a, **kw)
                    created.append(self)

            orig, W.Modal = W.Modal, Rec
            try:
                App._on_close(host)
            finally:
                W.Modal = orig
            return (created[-1] if created else None), host
        return go

    out.append(("⑩ 退出应用 420x170（空闲）", go_close(False)))
    out.append(("⑪ 退出应用 420x170（任务运行中，两行文案）", go_close(True)))

    return out


def dump_parts(m):
    """打印各部件 req vs got。"""
    try:
        outer = m.winfo_children()[0]
        kids = outer.winfo_children()
    except (tk.TclError, IndexError):
        print("       （拿不到部件）")
        return
    for nm, p in zip(["head", "sep", "content", "footer"], kids):
        try:
            print("       %-8s req=%4d  got=%4d%s"
                  % (nm, p.winfo_reqheight(), p.winfo_height(),
                     "   <<< 被压扁" if p.winfo_reqheight() > p.winfo_height() + 1
                     else ""))
        except tk.TclError:
            pass


def main():
    from app.ui import widgets as W
    from app.theme import Theme

    app = FakeApp()
    # SettingsPage 会读 `self.app.dark` / `refresh_task` / `set_theme`
    # （`_theme_var` 与「刷新任务」按钮的初始化）→ 宿主必须提供，
    # 否则「设置页确认框」这个弹窗根本构造不出来，就测不到它。
    app.dark = False
    app.refresh_task = lambda *a, **kw: None
    app.set_theme = lambda *a, **kw: None
    app.deiconify()
    app.geometry("1180x760+120+80")
    settle(app, 12)

    # ★ 正对照：把修复打成空操作，判据**必须**报红。
    # 没有正对照的"全绿"不可信 —— 你分不清"真的没问题"和"断言从未生效"。
    # 跑法：PROBE_NO_FIT=1 python tests/probe_all_modals.py
    if os.environ.get("PROBE_NO_FIT"):
        W.Modal.fit_height = lambda self, **kw: self._mh
        print("（正对照已开启：fit_height 变成空操作，下面必须出现被压扁的弹窗）")

    bad = []
    for label, go in cases():
        print("\n== %s ==" % label)
        try:
            m, owner = go(app, W, Theme(False))
        except Exception as e:  # noqa: BLE001
            print("       !! 打开失败：%r" % (e,))
            bad.append((label, "打开失败 %r" % (e,)))
            continue
        try:
            if m is None:
                print("       !! 弹窗未创建（可能是该分支没走到）")
                bad.append((label, "未创建"))
                continue
            settle(app, 18)
            print("       几何 %dx%d  自然高度 req=%d"
                  % (m.winfo_width(), m.winfo_height(), m.winfo_reqheight()))
            dump_parts(m)
            sq = squeezed(m)
            if sq:
                print("       !! 被压扁：" + "、".join(
                    "%s %d->%d" % (n, a, b) for n, a, b in sq))
                bad.append((label, "压扁 " + "、".join(
                    "%s %d->%d" % (n, a, b) for n, a, b in sq)))
            else:
                print("       OK 全部部件都拿到需要的高度")
        finally:
            try:
                m.close()
            except Exception:  # noqa: BLE001
                pass
            settle(app, 4)
            try:
                owner.destroy()
            except Exception:  # noqa: BLE001
                pass
            settle(app, 4)

    try:
        app.destroy()
    except Exception:  # noqa: BLE001
        pass

    print("\n" + "=" * 70)
    if bad:
        print("有问题弹窗 %d 个：" % len(bad))
        for lb, why in bad:
            print("  - %s → %s" % (lb, why))
    else:
        print("全部弹窗：没有任何部件被压扁")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
