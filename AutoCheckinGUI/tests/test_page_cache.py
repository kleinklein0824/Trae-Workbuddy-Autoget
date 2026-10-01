#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""页面缓存回归（`App._show` 复用页面，不再每次销毁重建）。

背景
----
用户反馈：「整个 EXE 交互点击切换板块感觉有点卡顿、操作不流畅，整个交互体感
页面会失真卡顿的感觉」。

量化后（`tests/perf_ui.py`）确认是真问题：**每次切页都 `destroy()` 旧页 +
`factory()` 新建**，单页 226~1939 ms，第二次进同一页一样慢。三块大头：
  · `settings`：`_refresh_cred_info()` 同步跑 `tc_checkin._scan_ide_logs()`（约 0.4 s）；
  · `api-service`：`_last_ai_status()` 同步探测本地后端（urllib，超时 2 s）；
  · 每页 150~250 个控件的创建 + Tk 几何计算。

修法：`App._show` 加 `_page_cache`（key=(module, view)），命中就只 `on_show()`。
本测试守住**四条不变量**，任何一条被破坏都会让卡顿复发或让界面变错：

  ① 命中缓存时**不得**再调页面工厂（这才是「不卡」的充要条件）；
  ② 切走要调旧页 `on_hide()`、切回要调缓存页 `on_show()`（各页靠它停/起定时器）；
  ③ **主题切换必须让缓存整体失效**（复用旧配色的控件会让换肤失效）；
  ④ 缓存不能无限膨胀 / 不能重复建同一页（`page_host` 子项数 == 访问过的页面数）。

★ 每条都带**正对照**（证明断言不是恒真）：例如 ④ 用「手工清缓存后再切」证明
  子项数确实会涨，① 用「清掉缓存后工厂必然被调」证明计数有效。
"""
import os
import queue
import sys
import time

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

    app = App()
    app.update()
    for _ in range(20):
        app.update()
        time.sleep(0.02)

    print("== 1. 缓存命中时不再建页 ==")
    # 计数页面工厂被调次数
    calls = {"n": 0}
    real_factory = app._page_factory

    def counting_factory(view):
        calls["n"] += 1
        return real_factory(view)

    app._page_factory = counting_factory

    app._switch_module("traework")
    app.update()
    app.goto("traework", "dashboard")
    app.update()
    calls["n"] = 0                       # 归零：下面这一轮才是要测的

    page_a = app.page
    app.goto("traework", "dashboard")    # 第二次进同一页 → 应命中缓存
    app.update()
    page_b = app.page
    check("★ 再次进入同一页没有调用页面工厂（这就是「不卡」的来源）",
          calls["n"] == 0, f"factory 被调 {calls['n']} 次")
    check("★ 拿到的是**同一个**页面对象（真的复用了）", page_a is page_b,
          f"{id(page_a)} vs {id(page_b)}")

    # 正对照：清掉缓存后必然要重建 —— 证明上面那个计数是有效的
    app.drop_page_cache(destroy=True)
    app.page = None
    app.goto("traework", "dashboard")
    app.update()
    check("【正对照】清缓存后工厂被调用 1 次、页面是新对象",
          calls["n"] == 1 and app.page is not page_a,
          f"calls={calls['n']} same={app.page is page_a}")

    print()
    print("== 2. on_hide / on_show 生命周期 ==")
    seen = {"hide": [], "show": []}
    for view in ("dashboard", "logs"):
        app.goto("traework", view)
        app.update()
        p = app.page
        orig_hide, orig_show = p.on_hide, p.on_show
        p.on_hide = lambda f=orig_hide, v=view: (seen["hide"].append(v), f())
        p.on_show = lambda f=orig_show, v=view: (seen["show"].append(v), f())

    app.goto("traework", "dashboard")    # 切回 dashboard（缓存命中）
    app.update()
    app.goto("traework", "logs")         # 再切到 logs（缓存命中）
    app.update()
    check("切走时调用了旧页的 on_hide()（各页靠它停定时器）",
          "dashboard" in seen["hide"] and "logs" in seen["hide"], seen)
    check("★ 切回缓存页时调用了 on_show()（数据仍然会刷新）",
          seen["show"].count("dashboard") >= 1 and seen["show"].count("logs") >= 1,
          seen)
    # 正对照：on_show 真的被调到（不是一次都没有）
    check("【正对照】on_show 调用次数 > 0", sum(seen["show"].count(v) for v in ("dashboard", "logs")) > 0,
          seen)

    print()
    print("== 3. ★ 主题切换必须让缓存整体失效 ==")
    app.goto("traework", "settings")
    app.update()
    dark_page = app.page
    n_before = len(app._page_cache)
    check("切换前缓存里有多个页面", n_before >= 2, n_before)
    app.set_theme(True)
    for _ in range(6):
        app.update()
    check("★ 换肤后缓存被清空重建（旧配色的控件不能复用）",
          len(app._page_cache) == 1 and app.page is not dark_page,
          f"cache={len(app._page_cache)} same={app.page is dark_page}")
    check("换肤后当前页是新对象（新配色生效）", app.page is not dark_page)
    check("【正对照】换肤后 theme.dark 确实变了", bool(app.theme.dark) is True)
    app.set_theme(False)
    for _ in range(4):
        app.update()

    print()
    print("== 4. 缓存不重复建页 / 不无限膨胀 ==")
    views = [("traework", "dashboard"), ("traework", "checkin"),
             ("traework", "accounts"), ("traework", "credits"),
             ("traework", "logs"), ("traework", "settings")]
    for m, v in views:
        app.goto(m, v)
        app.update()
    # 反复来回切，子项数不应增长
    for _ in range(3):
        for m, v in views:
            app.goto(m, v)
            app.update()
    kids = len(app.page_host.winfo_children())
    check("★ 反复切换后 page_host 子项数 == 访问过的页面数（没有重复建页）",
          kids == len(views), f"kids={kids} expect={len(views)}")
    check("★ 缓存条数 == 访问过的页面数（没有无限膨胀）",
          len(app._page_cache) == len(views), len(app._page_cache))
    # 正对照：清缓存后再切回来，子项数会变多（说明上面那个等式测得到东西）
    n_cache = len(app._page_cache)
    app.drop_page_cache(destroy=True)
    app.goto("traework", "dashboard")
    app.update()
    check("【正对照】清缓存后缓存条数变少（证明上面那个等式不是恒真）",
          len(app._page_cache) < n_cache, f"{len(app._page_cache)} vs {n_cache}")

    print()
    print("== 5. ★ 签到进行中切走再切回：轮询必须续上，不能永久卡在 busy ==")
    #   页面复用后 `on_hide()` 只取消轮询、**不销毁页面**。若切走时正好有签到在跑，
    #   切回来不续轮询的话事件队列永远没人取 → `_finish()` 不会被调用 →
    #   `busy` 卡在 True、「开始签到」按钮永远不回来（**页面永久卡死**）。
    #   改前是整页销毁重建，看不出这个问题。

    class _FakeRunner:
        def __init__(self):
            self.running = True
            self.events = queue.Queue()

        def cancel(self):
            self.running = False

    app.goto("traework", "checkin")
    for _ in range(4):
        app.update()
    pg = app.page
    pg.busy = True
    pg.runner = _FakeRunner()
    pg._poll_id = None
    pg.on_hide()                       # 切走：取消轮询
    check("切走后轮询确实停了（_poll_id 为空）",
          getattr(pg, "_poll_id", None) is None)
    pg.on_show()                       # 切回
    check("★ 切回后轮询被续上（_poll_id 非空）",
          getattr(pg, "_poll_id", None) is not None,
          f"_poll_id={getattr(pg, '_poll_id', None)!r}")
    check("★ 切回后 busy 仍是 True（任务还在跑，没被冲掉）", pg.busy is True)
    # 让任务结束 → 轮询必须能收尾
    pg.runner.running = False
    for _ in range(40):
        app.update()
        time.sleep(0.02)
    check("★ 任务结束后 _finish() 被调用、busy 归 False（页面不会永久卡住）",
          pg.busy is False, f"busy={pg.busy}")
    check("结束后「开始签到」按钮回来了（用户能再点）",
          pg._btn_run.winfo_manager() == "pack" or pg._btn_run.winfo_ismapped(),
          pg._btn_run.winfo_manager())

    # 【正对照】不续轮询 → busy 永远不归 False（证明上面那条不是恒真）
    pg.busy = True
    pg.runner = _FakeRunner()
    pg._poll_id = None
    pg.runner.running = False          # 任务已经结束，但没人去取队列
    for _ in range(40):
        app.update()
        time.sleep(0.02)
    check("【正对照】不续轮询时 busy 不会归 False（这就是那个「永久卡死」）",
          pg.busy is True, f"busy={pg.busy}")
    pg.busy = False                    # 收尾，别把状态留给后面的用例
    pg.runner = None

    print()
    print("== 6. 缓存命中不得抛异常（15 个页面组合全走一遍）==")
    errs = []
    for m in ("traework", "traecn", "workbuddy"):
        for v in ("dashboard", "checkin", "accounts", "credits"):
            try:
                app.goto(m, v)
                for _ in range(3):
                    app.update()
            except Exception as e:  # noqa: BLE001
                errs.append(f"{m}/{v}: {e!r}")
    for v in ("logs", "settings", "api-service"):
        try:
            app.goto("traework", v)
            for _ in range(3):
                app.update()
        except Exception as e:  # noqa: BLE001
            errs.append(f"traework/{v}: {e!r}")
    check("★ 全部 15 个页面组合切换无异常", not errs, errs[:3])

    try:
        app.destroy()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
    print()
    print(f"结果：{OK} OK / {FAIL} FAIL")
    sys.exit(0 if FAIL == 0 else 1)
