#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针：设置页「自动补跑（重试）」卡片是否真的渲染出来、是否真的由配置驱动。

为什么单独做一个探针：
  `verify_task_retry.py` 只能证明"源码里有这些字符串 / 后端策略算得对"，
  它碰不到真实控件。而这类卡片最常见的坏法是**渲染出来了但值是错的**
  （读不到配置 → 回填成硬编码默认值），字符串检查对此完全瞎。

★ 内置正对照：把 config 里的 `retry_toast_quiet` 翻成 False 再刷新，
  说明文字**必须**跟着变成「弹窗：每轮都弹」。若这条不成立，说明文字就是写死的，
  那么上面"读到 True"的那条检查毫无意义。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

OK = FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {extra}")


def main():
    from app.application import App
    import app.config as C

    cfg = C.load_config()
    sched = cfg.get("schedule", {})
    want_iv = str(sched.get("retry_interval_minutes", 30))
    want_on = bool(sched.get("retry_enabled", True))
    want_quiet = bool(cfg.get("options", {}).get("retry_toast_quiet", True))
    print(f"配置：retry_enabled={want_on} 间隔={want_iv} "
          f"窗口={sched.get('retry_window_minutes')} 仅关键节点弹窗={want_quiet}")
    print()

    app = App()
    # ★ 必须 `update_idletasks` 多轮：
    #   `winfo_ismapped()` 是 Tk 的实际映射状态，必须等 idle 任务（几何传播、
    #   窗口映射）跑完才返回 1。光 `update()` 不够 —— 在不同机器/负载下，
    #   卡片本身已映射（先创建的 widget）但卡片内后创建/后 pack 的开关还是 0，
    #   会让"开关已显示"恒红。`_diag_retry_sw.py` 用 16 轮 settle 验证过：
    #   sw 与全部祖先都 ismapped=1、viewable=1、geo 36x20。
    #   不加 `deiconify` 是为了保持探针 headless（不开窗闪一下）。
    for _ in range(8):
        app.update_idletasks()
    try:
        # ★ 必须走 _switch_module 再 goto —— 直接 goto 会被侧边栏回调重置回概览页
        app._switch_module("traework")
        for _ in range(8):
            app.update_idletasks()
        app.goto("traework", "settings")
        for _ in range(12):
            app.update_idletasks()

        page = app.page
        check("落在 SettingsPage", type(page).__name__ == "SettingsPage",
              f"实际 {type(page).__name__}")

        card = getattr(page, "card_retry", None)
        check("存在 card_retry 卡片", card is not None)
        if card is None:
            return
        check("卡片已显示", bool(card.winfo_ismapped()))
        # 裁切判据：pack 空间不够会把孩子**压扁**，此时 height < reqheight
        check("卡片未被压扁（height >= reqheight）",
              card.winfo_height() >= card.winfo_reqheight(),
              f"{card.winfo_height()} < {card.winfo_reqheight()}")

        check("重试间隔回填正确", page._retry_iv.get() == want_iv,
              f"实际 {page._retry_iv.get()!r} 期望 {want_iv!r}")
        check("补跑持续回填为小时文本",
              page._retry_win.get().replace(".", "").isdigit(),
              f"实际 {page._retry_win.get()!r}")
        check("补跑开关回填正确", bool(page._retry_on.get()) == want_on,
              f"实际 {page._retry_on.get()} 期望 {want_on}")

        sw = getattr(page, "_retry_toast_sw", None)
        check("存在「仅在关键节点弹窗」开关", sw is not None)
        if sw is not None:
            check("弹窗策略开关回填正确", bool(sw.value) == want_quiet,
                  f"实际 {sw.value} 期望 {want_quiet}")
            check("开关已显示", bool(sw.winfo_ismapped()))

        hint = page._retry_hint.cget("text")
        print(f"  hint = {hint}")
        check("说明文字含弹窗策略（与开关一致）",
              ("弹窗：仅关键节点" in hint) == want_quiet
              and ("弹窗：每轮都弹" in hint) == (not want_quiet),
              f"hint={hint!r}")

        # ---- 正对照：配置翻面 → 文案必须跟着翻 ----
        cfg2 = C.load_config()
        cfg2.setdefault("options", {})["retry_toast_quiet"] = not want_quiet
        C.save_config(cfg2)
        try:
            page._load_retry()
            for _ in range(8):
                app.update_idletasks()
            # ★ `_load_retry()` 会**销毁并重建** retry 卡片 → 老 widget 都失效
            #   → `page._retry_hint` 已变为新建的 Label，但**旧的引用**变 None。
            #   必须重新走 `_retry_hint` 属性抓（settings 重建时会重绑）。
            hint_lbl = getattr(page, "_retry_hint", None)
            hint2 = hint_lbl.cget("text") if hint_lbl else ""
            print(f"  hint(翻转后) = {hint2}")
            check("正对照：配置翻转后文案跟着变",
                  ("弹窗：每轮都弹" in hint2) == want_quiet,
                  f"hint2={hint2!r}")
        finally:
            cfg3 = C.load_config()
            cfg3.setdefault("options", {})["retry_toast_quiet"] = want_quiet
            C.save_config(cfg3)
            page._load_retry()
            for _ in range(8):
                app.update_idletasks()
        check("已恢复原配置", bool(C.load_config().get(
            "options", {}).get("retry_toast_quiet")) == want_quiet)
    finally:
        try:
            app.destroy()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
    print()
    print(f"RESULT: {'OK' if FAIL == 0 else 'FAIL'}  {OK} OK / {FAIL} FAIL")
    sys.exit(0 if FAIL == 0 else 1)
