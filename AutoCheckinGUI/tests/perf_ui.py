#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测量交互性能：页面切换耗时、窗口缩放重排耗时、滚轮滚动帧耗时。

优化前的主要症状是「点一下要等一会儿才有反应」，因此这里直接量化
  * 首次渲染每页的耗时
  * 重复切换（缓存命中）的耗时
  * 模拟窗口从 1180x760 拉到最大化再还原，每次重排耗时
  * 连续滚轮的每帧耗时

用法：python tests/perf_ui.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# ctypes 回调异常是静默的，这里显式计数
_cb_errors = []
_orig_hook = sys.excepthook


def _hook(t, v, tb):
    if "ctypes" in str(t) or "callback" in str(v):
        _cb_errors.append(f"{t.__name__}: {v}")
        return
    _orig_hook(t, v, tb)


sys.excepthook = _hook

PAGES = [
    ("traework", "dashboard"), ("traework", "checkin"),
    ("traework", "accounts"), ("traework", "credits"),
    ("traework", "logs"), ("traework", "settings"),
    ("traework", "api-service"),
]


def bench(app, label, fn, rounds=3):
    times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn()
        app.update()
        app.update_idletasks()
        times.append((time.perf_counter() - t0) * 1000)
    best = min(times)
    avg = sum(times) / len(times)
    print(f"  {label:<34} best={best:7.1f}ms  avg={avg:7.1f}ms")
    return best


def main():
    from app.application import App

    app = App()
    app.update()
    # 等首屏与后台预热跑完
    for _ in range(30):
        app.update()
        time.sleep(0.03)

    print("=== 首次进入某页（要建页：控件创建 + Tk 几何计算）===")
    total = 0.0
    for module, view in PAGES:
        def first(m=module, v=view):
            # 先把该页从缓存里摘掉并销毁，确保这一轮真的是「从零建页」
            key = (m, v)
            old = app._page_cache.pop(key, None)
            if old is not None and old is not app.page:
                try:
                    old.destroy()
                except Exception:
                    pass
            app._show(m, v)
        ms = bench(app, f"{module}/{view}", first)
        total += ms
    print(f"  {'合计（每页各一次）':<34} {total:7.1f}ms")

    print("=== 再次切换（页面缓存命中：只跑 on_show 刷新数据）===")
    # ★ 2026-09-22 起页面是**复用**的（`App._show` 走 `_page_cache`），
    #   所以「切页体感」由这一组数字决定，上面那组只在第一次进页面时发生。
    for module, view in PAGES:
        bench(app, f"{module}/{view} 缓存命中",
              lambda m=module, v=view: app._show(m, v))

    print("=== 窗口尺寸变化后的重排 ===")
    app._show("traework", "dashboard")

    def grow():
        app.geometry("1900x1050")
        app.update_idletasks()
        app._do_resize()

    def shrink():
        app.geometry("1180x760")
        app.update_idletasks()
        app._do_resize()

    bench(app, "放大到 1900x1050", grow)
    bench(app, "还原到 1180x760", shrink)

    print("=== 连续滚轮（30 帧）===")
    page = app.page
    if page is not None and hasattr(page, "scroll"):
        sf = page.scroll

        # ⚠️ 这里以前直接调 `sf._on_wheel(...)` —— 那个方法已经删掉了。
        # 现在滚轮由 `app.ui.widgets` 里的**全局路由**处理，所以基准必须走
        # `_route_wheel()` 本身：它每次都做一遍几何下钻（`deepest_at` 要找
        # 指针下有没有能自己滚的原生控件），那才是真正的每帧成本。
        # 只测 `canvas.yview_scroll()` 会把这个开销完全漏掉。
        from app.ui import widgets as W

        cv = sf.canvas
        cx, cy = cv.winfo_rootx(), cv.winfo_rooty()
        cw, ch = cv.winfo_width(), cv.winfo_height()

        class _E:
            delta = -120
            num = None
            widget = None

        # 指针放在页面可视区中间：既在 ScrollFrame 里，又尽量避开内嵌的
        # 原生滚动控件（否则路由会让路给它们，测不到页面滚动）。
        _E.widget = sf.inner
        px, py = cx + max(2, cw // 2), cy + max(2, ch // 2)

        t0 = time.perf_counter()
        for _ in range(30):
            _E.x_root, _E.y_root = px, py
            W._route_wheel(_E())
            app.update_idletasks()
        dt = (time.perf_counter() - t0) * 1000
        print(f"  {'30 帧滚动':<34} {dt:7.1f}ms   单帧 {dt/30:5.2f}ms")

    print(f"=== ctypes 回调异常数 = {len(_cb_errors)} ===")
    for e in _cb_errors[:5]:
        print("   ", e)

    app._closing = True
    try:
        app.destroy()
    except Exception:  # noqa: BLE001
        pass
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
