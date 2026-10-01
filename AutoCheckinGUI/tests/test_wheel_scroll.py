#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""滚轮回归：指针落在页面**任意子控件**上，滚轮都要能滚动页面。

为什么值得单独一条回归
----------------------
老实现把 `<MouseWheel>` 逐个绑到 ScrollFrame 的子树控件上。它坏在一个 Tk 的
基本事实上：**事件不沿父子链冒泡**。Tk 只按 bindtags 派发
（控件 → 控件类 → 顶层 → "all"），父控件不在子控件的 bindtags 里。
所以事件落在卡片里的 Label / 按钮上时，绑在内容 frame 上的处理函数
**根本不会触发** —— 用户感受就是「滚轮没反应，只能按住右边滚动条拖」。

这条测试直接断言「往页面**深处**的控件发一个滚轮事件，页面 yview 必须变化」，
老实现必然失败。

用法：python tests/test_wheel_scroll.py
"""
import os
import sys
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app.ui import widgets as W  # noqa: E402

PAGES = [
    ("traework", "dashboard"), ("traework", "checkin"),
    ("traework", "accounts"), ("traework", "credits"),
    ("traecn", "dashboard"), ("traecn", "checkin"),
    ("traecn", "accounts"), ("traecn", "credits"),
    ("workbuddy", "dashboard"), ("workbuddy", "checkin"),
    ("workbuddy", "accounts"), ("workbuddy", "credits"),
    ("traework", "logs"), ("traework", "settings"),
    ("traework", "api-service"),
]

ok = 0
fail = 0
skip = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label}  {extra}")


def settle(app, n=8):
    for _ in range(n):
        app.update()
        time.sleep(0.02)


def belongs_to(widget, sf):
    """widget 是否归属于 `sf`（往上走先遇到 sf 而不是别的滚动容器）。"""
    w = widget
    while w is not None:
        if w is sf:
            return True
        if isinstance(w, W.ScrollFrame):
            return False        # 先撞上别的滚动容器（嵌套列表那种）
        w = getattr(w, "master", None)
    return False


def deepest(app, sf, min_w=30, min_h=8):
    """找 `sf` 内部**当前可见**的最深子控件。

    两个必须的过滤，否则测试会自己骗自己：
      * **必须在可视区内**：长页面里最深的控件往往已经滚出屏幕，
        指针根本不在那儿，路由自然找不到容器（这是测试的错，不是产品的）；
      * **必须归属 sf**：api-service 页里嵌了一个列表滚动容器，
        落在它里面的控件该滚的是那个列表，不是整页。
    """
    cv = sf.canvas
    vx0, vy0 = cv.winfo_rootx(), cv.winfo_rooty()
    vx1, vy1 = vx0 + cv.winfo_width(), vy0 + cv.winfo_height()
    best = [None]

    def walk(w, depth):
        for c in w.winfo_children():
            try:
                if not c.winfo_ismapped():
                    continue
                cw, ch = c.winfo_width(), c.winfo_height()
                cx = c.winfo_rootx() + cw // 2
                cy = c.winfo_rooty() + ch // 2
            except tk.TclError:
                continue
            if cw < min_w or ch < min_h:
                continue
            if not (vx0 <= cx < vx1 and vy0 <= cy < vy1):
                continue
            if not belongs_to(c, sf):
                continue
            score = (depth, cw * ch)
            if best[0] is None or score > best[0][0]:
                best[0] = (score, c)
            walk(c, depth + 1)

    walk(sf.inner, 0)
    return best[0][1] if best[0] else None


def scroll_point(sf):
    """在 `sf` 的可视区里找一个「滚轮应该滚这一页」的指针点。

    沿可视区竖直中线自上而下扫，返回第一个满足下面两条的点：
      * 指针下确实是一个**内容子控件**（不是 canvas / inner 本身）——
        测的才是「指针停在页面内容上」这条真实路径；
      * 全局路由判定这个点属于 `sf`，而不是被嵌套的原生滚动框接管。
    返回 (指针下的控件, (x_root, y_root))；找不到返回 None。
    """
    cv = sf.canvas
    try:
        vx0, vy0 = cv.winfo_rootx(), cv.winfo_rooty()
        vw, vh = cv.winfo_width(), cv.winfo_height()
        top = cv.winfo_toplevel()
    except tk.TclError:
        return None
    if vw <= 4 or vh <= 4:
        return None
    x = vx0 + vw // 2
    for frac in (0.12, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        y = vy0 + int(vh * frac)
        under = W.deepest_at(top, x, y)
        if under is None or under in (cv, sf.inner, sf):
            continue
        if W.wheel_container(under) is sf:
            return under, (x, y)
    return None


def point_over(sf, widget, x=None):
    """在 `sf` 的可视区里找一个**确实落在 `widget` 上**的指针点。

    必须扫描，不能直接取控件中心：Canvas 会**裁剪**嵌进去的窗口，但
    `winfo_rootx/rooty` 报的是"不被裁剪时它应该在哪"。长页面里日志文本框
    就在页面最底部（实测 inner 高 1632、可视区只有 458），它的中心在
    窗口外面老远 —— 直接拿中心去问路由，得到的是"指针在页面外"。
    那是取点取错了，不是产品的问题。
    """
    cv = sf.canvas
    try:
        vx0, vy0 = cv.winfo_rootx(), cv.winfo_rooty()
        vw, vh = cv.winfo_width(), cv.winfo_height()
        top = cv.winfo_toplevel()
    except tk.TclError:
        return None
    if vw <= 4 or vh <= 4:
        return None
    if x is None:
        x = widget.winfo_rootx() + max(1, widget.winfo_width() // 2)
    x = min(max(x, vx0 + 4), vx0 + vw - 5)
    for i in range(1, 20):
        y = vy0 + int(vh * i / 20.0)
        if W.deepest_at(top, x, y) is widget:
            return x, y
    return None


def _within(under, container):
    """`under` 是不是在 `container` 的子树里（含自身）。"""
    w = under
    for _ in range(64):
        if w is None:
            return False
        if w is container:
            return True
        w = getattr(w, "master", None)
    return False


def point_in_container(app, sf, container):
    """在 `sf` 的可视区里找一个**落在 `container` 子树内**的指针点。

    为什么不能用 `point_over(sf, container)`：容器里通常还铺着子控件，
    `deepest_at` 返回的是那个子控件而不是容器本身，`is container` 会永远
    不成立。判据得换成「是否在 container 子树内」。
    """
    cv = sf.canvas
    try:
        vx0, vy0 = cv.winfo_rootx(), cv.winfo_rooty()
        vw, vh = cv.winfo_width(), cv.winfo_height()
        top = cv.winfo_toplevel()
        tx0, ty0 = container.winfo_rootx(), container.winfo_rooty()
        tw, th = container.winfo_width(), container.winfo_height()
    except tk.TclError:
        return None
    if vw <= 4 or vh <= 4 or tw <= 4 or th <= 4:
        return None
    for x in (tx0 + max(2, tw // 2), tx0 + 4, tx0 + tw - 5):
        if not (vx0 + 2 <= x <= vx0 + vw - 3):
            continue
        for i in range(1, 20):
            y = ty0 + int(th * i / 20.0)
            if not (vy0 + 2 <= y <= vy0 + vh - 3):
                continue
            if _within(W.deepest_at(top, x, y), container):
                return x, y
    return None


def reveal(app, sf, container, steps=24):
    """把 `sf` 逐段下滚，直到 `container` 进入可视区并取到落点。

    长页面里嵌套列表一开始可能在视口外 —— 而 Canvas 裁剪后 `winfo_rooty`
    仍报未裁剪的位置，直接取中心会得到"指针在页面外"。
    """
    for i in range(steps + 1):
        sf.canvas.yview_moveto(i / float(steps))
        settle(app, 3)
        pt = point_in_container(app, sf, container)
        if pt is not None:
            return pt
    return None


def send_wheel(app, widget, delta, point=None):
    """往 `widget` 发一个滚轮事件，走**完整派发链**（控件类绑定 + 全局路由）。

    为什么发在 `widget` 上而不是顶层窗口上：真实滚轮事件落在指针下的那个
    控件上，它的 **类绑定**（`tk.Text` 自己会滚）先跑，然后才轮到
    `"all"` 里的全局路由。发在顶层窗口上会跳过类绑定，测不出
    「文本框自己滚、页面不跟着滚」这条。

    `point` 是**指针的屏幕坐标**（rootx/rooty）。默认取 `widget` 中心。
    为什么要显式给点：滚动之后控件中心可能已经移出可视区，那时"指针在
    页面外"，路由按设计什么都不滚 —— 那是测试的错，不是产品的。
    """
    if point is None:
        point = (widget.winfo_rootx() + max(1, widget.winfo_width() // 2),
                 widget.winfo_rooty() + max(1, widget.winfo_height() // 2))
    x_root, y_root = point
    try:
        rx = x_root - widget.winfo_rootx()
        ry = y_root - widget.winfo_rooty()
    except tk.TclError:
        rx = ry = 5
    widget.event_generate("<MouseWheel>", delta=delta, x=rx, y=ry,
                          rootx=x_root, rooty=y_root, when="now")
    settle(app, 4)
    return point


def yview(sf):
    try:
        return sf.canvas.yview()
    except tk.TclError:
        return (0.0, 1.0)


def find_text(w):
    for c in w.winfo_children():
        if isinstance(c, tk.Text):
            return c
        r = find_text(c)
        if r is not None:
            return r
    return None


def main():
    global skip
    from app.application import App

    app = App()
    app.update()
    # 首渲染是 after(400) 延迟的
    for _ in range(50):
        app.update()
        time.sleep(0.05)

    # 窗口放矮一点，保证内容溢出、页面真的能滚
    sw, sh = app.winfo_screenwidth(), app.winfo_screenheight()
    app.geometry("1180x460+%d+%d" % (max(0, (sw - 1180) // 2), 60))
    settle(app, 12)
    print("窗口 %dx%d" % (app.winfo_width(), app.winfo_height()))

    # ---------------- A. 路由单元 ----------------
    print()
    print("== A. 全局路由 wheel_container / deepest_at ==")
    app.goto("traework", "dashboard")
    settle(app, 10)
    page = app.page
    sf = page.scroll
    deep = deepest(app, sf)
    check("找得到页面深处的控件", deep is not None)
    if deep is not None:
        got = W.wheel_container(deep)
        check("深处控件 -> 路由到本页滚动容器", got is sf,
              f"{type(got).__name__}")
    check("页面外的控件（标题）-> 不路由到滚动容器",
          W.wheel_container(page._title_lbl) is None)
    check("None -> 返回 None", W.wheel_container(None) is None)
    if deep is not None:
        px = deep.winfo_rootx() + deep.winfo_width() // 2
        py = deep.winfo_rooty() + deep.winfo_height() // 2
        hits = W.frames_at(px, py)
        check("按几何位置能找到指针下的滚动容器", sf in hits,
              f"{len(hits)} 个候选")
        check("最内层优先（本页排第一）", bool(hits) and hits[0] is sf)
        check("指针在页面外 -> 找不到任何容器",
              W.frames_at(app.winfo_rootx() + 2, app.winfo_rooty() + 2) == [])

        # 几何定位：不依赖 winfo_containing，被别的窗口挡住也算得对
        under = W.deepest_at(app, px, py)
        check("几何定位找得到指针下的控件", under is not None,
              f"{type(under).__name__ if under is not None else None}")
        check("几何定位结果归属本页（指针在深处控件上）",
              under is not None and W.wheel_container(under) is sf,
              f"{type(under).__name__ if under is not None else None}")
        check("几何定位不会越出顶层窗口（页面外的点）",
              W.deepest_at(app, app.winfo_rootx() - 50,
                           app.winfo_rooty() + 5) is app)

    # ---------------- B. 逐页真实滚轮 ----------------
    print()
    print("== B. 逐页：往页面深处的控件发滚轮，页面必须滚动 ==")
    tested = 0
    for module, view in PAGES:
        key = f"{module}/{view}"
        try:
            if module != app.module:
                app._switch_module(module)
                settle(app, 4)
            app.goto(module, view)
            settle(app, 10)
            page = app.page
            sf = getattr(page, "scroll", None)
            if sf is None:
                print(f"  SKIP {key}（页面没有滚动容器）")
                skip += 1
                continue
            sf.scroll_top()
            settle(app, 4)
            first, last = yview(sf)
            if (last - first) >= 0.999:
                print(f"  SKIP {key}（内容装得下，无需滚动）")
                skip += 1
                continue
            hit = scroll_point(sf)
            if hit is None:
                print(f"  SKIP {key}（可视区全被嵌套滚动框盖住，测不到本页）")
                skip += 1
                continue
            under, pt = hit

            W._WHEEL["acc"] = 0.0
            before = yview(sf)[0]
            try:
                send_wheel(app, under, -120, pt)
            except tk.TclError as e:
                # ★ 目标控件在**刷新过程中被重建**了（2026-09-25 修）。
                #   账号页/日志页会在「后台扫描结果到达」时重渲染
                #   （`reload()` 把行 destroy 重建），而测试取点与发事件之间
                #   正好隔着这个窗口 → `bad window path name`。
                #   这是**时序**问题，不是滚轮路由坏了（指针在真实用户手里
                #   不会"指向一个已被销毁的控件"）。所以记 SKIP 说清原因，
                #   绝不报 FAIL —— 否则会周期性红，且红得毫无信息量。
                print(f"  SKIP {key}（目标控件在刷新中被重建，本轮测不出：{e}）")
                skip += 1
                continue
            after = yview(sf)[0]
            if after <= before:
                check(f"{key} 滚轮下滚", False,
                      f"yview {before:.3f} -> {after:.3f}"
                      f"（指针下是 {under.winfo_class()}）")
                continue

            # 下滚之后指针下的控件/位置都变了，重新取一个点再上滚
            hit2 = scroll_point(sf)
            if hit2 is None:
                check(f"{key} 滚轮上滚", False, "滚动后找不到可测点")
                continue
            under2, pt2 = hit2
            send_wheel(app, under2, 120, pt2)
            back = yview(sf)[0]
            if back >= after:
                check(f"{key} 滚轮上滚", False,
                      f"yview {after:.3f} -> {back:.3f}"
                      f"（指针下是 {under2.winfo_class()}）")
                continue
            check(f"{key} 滚轮上下都生效", True)
            tested += 1
        except tk.TclError as e:  # noqa: BLE001
            # ★ 目标控件在刷新中被重建（2026-09-25 修）：账号页/日志页会在
            #   「后台扫描结果到达」时 `reload()` → 把行 destroy 重建，
            #   而测试「取点 → 发事件」之间正好隔着这个窗口 →
            #   `bad window path name`。这是**时序**问题，不是滚轮路由坏了
            #   （真实用户的指针不会"指向一个已被销毁的控件"）。
            #   所以记 SKIP 并说清原因，绝不报 FAIL —— 否则会周期性红。
            if "bad window path name" in str(e):
                print(f"  SKIP {key}（目标控件在刷新中被重建，本轮测不出）")
                skip += 1
            else:
                check(f"{key} 滚轮", False, repr(e))
        except Exception as e:  # noqa: BLE001
            check(f"{key} 滚轮", False, repr(e))

    print(f"  （实际验证 {tested} 页，跳过 {skip} 页）")
    check("至少验证了 8 个页面（避免测试空转）", tested >= 8, f"only {tested}")

    # ---------------- C. 原生可滚动控件不被抢 ----------------
    print()
    print("== C. 指针在可滚动的文本框上：只滚文本框，不滚页面 ==")
    app.goto("traework", "logs")
    settle(app, 12)
    page = app.page
    sf = page.scroll
    # 关掉 5s 自动刷新：它会在测量中间重建内容 / 把光标挪到末尾，
    # 让断言随机失败（是测试的噪声，不是产品行为）
    try:
        page._auto.set(False)
        if page._tick_id:
            page.after_cancel(page._tick_id)
            page._tick_id = None
    except Exception:  # noqa: BLE001
        pass

    txt = find_text(page)
    check("日志页有 Text 控件", txt is not None)
    if txt is not None:
        # 日志卡片在页面最底部：先把页面滚到底，文本框才进可视区
        sf.canvas.yview_moveto(1.0)
        settle(app, 6)
        pt = point_over(sf, txt)
        check("能取到一个落在日志文本框上的指针点", pt is not None)
        if pt is None:
            print("  SKIP 文本框不在可视区，测不出")
        else:
            under = W.deepest_at(app, pt[0], pt[1])
            check("几何定位确实落在文本框上（不是页面背景）", under is txt,
                  f"{type(under).__name__}")
            # ★ 先判**前提**再断言（2026-09-25 修）：
            #   「文本框能滚时交给它自己」的前提是**文本框自己确实能滚**。
            #   内容装得下时 `wheel_container` 本来就会往上交给页面 ——
            #   那是**正确行为**；旧写法却先断言它必须等于自己 → 必然红。
            #   而内容长度是**随环境变的**（日志条数 / 账号数），
            #   所以这条会「有时红」：单独跑绿、套件里红，最难查的那一类。
            # 日志框默认滚到末尾，先拉回顶部，否则只能往上滚、测不出下滚
            txt.yview_moveto(0)
            settle(app, 6)
            t_first, t_last = txt.yview()
            p_before = yview(sf)[0]
            if (t_last - t_first) >= 0.999:
                print("  SKIP 文本框内容装得下，测不出（含「路由判定」那条）")
            else:
                check("路由判定：文本框能滚时交给它自己",
                      W.wheel_container(txt) is txt,
                      f"{type(W.wheel_container(txt)).__name__}")
                W._WHEEL["acc"] = 0.0
                send_wheel(app, txt, -120, pt)
                t_after = txt.yview()[0]
                p_after = yview(sf)[0]
                check("文本框自己滚了", t_after > t_first,
                      f"{t_first:.3f} -> {t_after:.3f}")
                check("页面没有被一起滚（不双滚）",
                      abs(p_after - p_before) < 1e-6,
                      f"{p_before:.3f} -> {p_after:.3f}")

    # ---------------- D. 不足一格的小 delta 要累积 ----------------
    print()
    print("== D. delta 不是 120 的整数倍时也要能滚（累积零头） ==")
    app.goto("traework", "settings")
    settle(app, 12)
    page = app.page
    sf = page.scroll
    sf.scroll_top()
    settle(app, 4)
    first, last = yview(sf)
    if (last - first) >= 0.999:
        print("  SKIP 设置页内容装得下")
    else:
        hit = scroll_point(sf)
        if hit is None:
            print("  SKIP 设置页找不到可测点")
        else:
            under, pt = hit
            W._WHEEL["acc"] = 0.0
            before = yview(sf)[0]
            for _ in range(3):          # 3 × 40 = 120 → 恰好一格
                send_wheel(app, under, -40, pt)
            after = yview(sf)[0]
            check("3 次 delta=40 累积成一次滚动", after > before,
                  f"yview {before:.3f} -> {after:.3f}"
                  "（直接 int(delta/120) 的老实现会一直滚不动）")

    # ---------------- E. 全局路由自身的代价 ----------------
    print()
    print("== E. 全局路由的代价：注册表不泄漏 + 单帧耗时 ==")
    # 路由是**进程级**的，每次滚轮都要跑一遍几何下钻，还得靠 `_FRAMES` 找容器。
    # 这两个地方退化都**不会报错**，只会让滚轮越用越卡（典型：页面销毁后没摘出
    # 注册表 → 每帧遍历的容器越来越多）。所以固化成断言。
    frames_before = len(W._FRAMES)
    for module, view in PAGES[:6]:
        try:
            app.goto(module, view)
        except Exception:
            continue
        settle(app, 2)
    app.goto("traework", "settings")
    settle(app, 10)
    frames_after = len(W._FRAMES)
    check("反复切页后注册表没有无界增长",
          frames_after <= frames_before + 8,
          f"{frames_before} -> {frames_after}"
          "（WeakSet 应该让已销毁的页面自动掉出）")
    check("注册表规模有硬上限（每页 1 个 + 嵌套几个）",
          frames_after <= 40, f"{frames_after} 个")

    page = app.page
    sf = page.scroll
    sf.scroll_top()
    settle(app, 4)
    hit = scroll_point(sf)
    if hit is None:
        print("  SKIP 设置页找不到可测点，跳过单帧耗时")
    else:
        under, pt = hit

        class _Ev:
            delta = -120
            num = None

        ev = _Ev()
        ev.widget = sf.inner          # 路由只拿它取 winfo_toplevel()
        ev.x_root, ev.y_root = pt

        # 预热几次：首次调用会带上一堆 Tcl 侧的惰性初始化，不预热会虚高
        for _ in range(3):
            W._route_wheel(ev)

        N = 30
        t0 = time.perf_counter()
        for _ in range(N):
            W._route_wheel(ev)
        per_ms = (time.perf_counter() - t0) * 1000 / N
        # 实测 ~0.4ms。给 8 倍余量：既容忍机器抖动，又能抓住"几何下钻被写成
        # 全树遍历"这类数量级退化（那会到几十 ms）。
        check("单帧路由耗时 < 3ms", per_ms < 3.0, f"{per_ms:.3f}ms/帧")

    # ---------------- F. 嵌套滚动容器：最内层优先（真实页面）----------------
    print()
    print("== F. api-service 页的嵌套列表：指针在列表上时该谁滚 ==")
    # A 段只把「最内层优先」当**单元**验过；真实页面上还有一层复杂度：
    # 嵌套列表是放在固定高度 holder 里的 ScrollFrame。这里补上真实场景。
    app.goto("traework", "api-service")
    settle(app, 14)
    page = app.page
    outer = page.scroll
    inner = getattr(page, "_pool_list", None)
    check("api-service 页存在嵌套的滚动列表", inner is not None)
    if inner is None:
        print("  SKIP 找不到嵌套列表")
    else:
        # (1) 常态：列表内容装得下 → 滚轮应该**穿过**它去滚外层页面。
        #     这是本机大多数时候的状态（账号池只有一两行）。
        outer.canvas.yview_moveto(0.0)
        settle(app, 6)
        pt = reveal(app, outer, inner.inner)
        check("能把嵌套列表滚进可视区并取到落点", pt is not None)
        if pt is None:
            pass
        elif W._can_scroll_canvas(inner.canvas):
            print("  SKIP 账号池列表内容超出，改由下面 (2) 覆盖")
        else:
            before = yview(outer)[0]
            W._WHEEL["acc"] = 0.0
            send_wheel(app, W.deepest_at(app, pt[0], pt[1]), -120, pt)
            after = yview(outer)[0]
            check("列表装得下时，滚轮穿过它去滚外层页面", after > before,
                  f"页面 yview {before:.3f} -> {after:.3f}")

        # (2) 列表内容超出时：滚的必须是**列表本身**，外层页面不能跟着双滚。
        #     自己往列表里塞几行（只动控件树，不碰真实 config.json）。
        rows = []
        try:
            for i in range(24):
                lbl = tk.Label(inner.inner, text=f"test-row-{i:02d}",
                               anchor="w", height=1)
                lbl.pack(fill="x")
                rows.append(lbl)
            inner.resync()
            settle(app, 10)
        except tk.TclError:
            pass
        can_inner = W._can_scroll_canvas(inner.canvas)
        check("构造出内容超出的嵌套列表", can_inner)
        if can_inner:
            outer.canvas.yview_moveto(0.0)
            settle(app, 6)
            pt2 = reveal(app, outer, inner.inner)
            check("列表超出后仍能取到落点", pt2 is not None)
            if pt2 is not None:
                under = W.deepest_at(app, pt2[0], pt2[1])
                got = W.wheel_container(under)
                check("路由判定：指针在列表上时交给列表", got is inner,
                      f"{type(got).__name__}")
                inner.canvas.yview_moveto(0.0)
                settle(app, 4)
                i_before, p_before = yview(inner)[0], yview(outer)[0]
                W._WHEEL["acc"] = 0.0
                send_wheel(app, under, -120, pt2)
                i_after, p_after = yview(inner)[0], yview(outer)[0]
                check("滚的是列表本身", i_after > i_before,
                      f"列表 yview {i_before:.3f} -> {i_after:.3f}")
                check("外层页面没有被一起滚（不双滚）",
                      abs(p_after - p_before) < 1e-6,
                      f"页面 yview {p_before:.3f} -> {p_after:.3f}")
        for lbl in rows:                    # 只销毁控件，不碰文件系统
            try:
                lbl.destroy()
            except tk.TclError:
                pass

        # (3) 反向：**装得下**的嵌套容器，路由必须跳过它、继续往上找到外层页面。
        #     这里不能用 `_pool_list` —— 本机账号池本来就有真实行、内容确实超出，
        #     删掉自己塞的测试行它照样能滚（断言前提不成立）。
        #     用 `_local_list`：未探测时显示「尚未探测」空状态，是页面上真实存在的
        #     "嵌套但滚不动"的容器，正好是这条要测的形态。
        local = getattr(page, "_local_list", None)
        check("api-service 页还有第二个嵌套容器（本地模型清单）", local is not None)
        if local is not None:
            settle(app, 6)
            can_local = W._can_scroll_canvas(local.canvas)
            got2 = W.wheel_container(local.inner)
            if can_local:
                print("  SKIP 本地模型清单内容超出（已探测过），跳过")
            else:
                check("装得下的嵌套容器：路由跳过它去找外层页面", got2 is outer,
                      f"拿到 {type(got2).__name__}（是它自己={got2 is local}）")

    # ---------------- G. 滚不动的嵌套容器必须让路（合成场景，可复现）----------------
    print()
    print("== G. 嵌套容器装得下 → 路由跳过它去找外层（合成场景） ==")
    # F 段那两个嵌套容器在本机**都有内容、都能滚**，所以"跳过滚不动的嵌套容器"
    # 这条分支靠真实页面只能 SKIP。搭一个最小场景把它固定下来：
    # 外层内容超高（一定可滚）+ 内层只有一行（一定装得下）。
    win = tk.Toplevel(app)
    win.geometry("400x300")
    outer2 = W.ScrollFrame(win, app.theme)
    outer2.pack(fill="both", expand=True)
    tk.Label(outer2.inner, text="占位", height=40).pack(fill="x")
    inner2 = W.ScrollFrame(outer2.inner, app.theme)
    inner2.pack(fill="x")
    tk.Label(inner2.inner, text="短内容").pack()
    settle(app, 10)
    check("合成：外层内容超高、可滚", W._can_scroll_canvas(outer2.canvas))
    check("合成：内层装得下、不可滚", not W._can_scroll_canvas(inner2.canvas))
    got3 = W.wheel_container(inner2.inner)
    check("滚不动的嵌套容器被跳过，路由到外层页面", got3 is outer2,
          f"拿到 {type(got3).__name__}（是内层自己={got3 is inner2}）")
    try:
        win.destroy()
    except tk.TclError:
        pass

    # ---------------- H. 弹窗打开时不能滚到背后的页面 ----------------
    print()
    print("== H. 模态框盖在页面上时，滚轮不能滚到背后的页面 ==")
    # 这是「按几何派发」自己的反面：**矩形判断不知道窗口遮挡**。模态框是独立
    # Toplevel、居中盖在页面上（`Modal.show()`），它的矩形落在页面 canvas 之内
    # —— 不按活动窗口过滤的话，指针明明在弹窗上也会命中背后那一页。
    # 实测缺陷：弹窗 520x260+508+200，弹窗中心命中"本页"，yview 0.5000 → 0.5334。
    app.goto("traework", "settings")
    settle(app, 16)
    page = app.page
    sf = page.scroll
    if (yview(sf)[1] - yview(sf)[0]) >= 0.999:
        print("  SKIP 设置页内容装得下，测不出误滚")
    else:
        sf.canvas.yview_moveto(0.5)          # 上下都能滚，误滚才看得出来
        settle(app, 8)
        m = W.Modal(app, app.theme, "滚轮测试弹窗", 520, 260)
        tk.Label(m.content, text="弹窗内容", font=app.theme.f_small,
                 bg=app.theme.bg_soft, fg=app.theme.fg).pack()
        m.show()
        settle(app, 12)
        mx = m.winfo_rootx() + m.winfo_width() // 2
        my = m.winfo_rooty() + m.winfo_height() // 2
        under = W.deepest_at(m, mx, my)
        check("弹窗已打开，指针下是弹窗里的控件", under is not None)
        check("（前提）该点几何上确实落在背后页面的 canvas 内",
              any(h is sf for h in W.frames_at(mx, my)),
              "几何判断不知道遮挡 —— 所以必须靠活动窗口过滤")
        check("按活动窗口过滤后，背后页面不在候选里",
              all(h is not sf for h in W.frames_at(mx, my, m)))
        before = yview(sf)[0]
        W._WHEEL["acc"] = 0.0
        (under or m).event_generate("<MouseWheel>", delta=-120, x=10, y=10,
                                    rootx=mx, rooty=my, when="now")
        settle(app, 6)
        after = yview(sf)[0]
        check("弹窗打开时，滚轮不会滚到背后的页面",
              abs(after - before) < 1e-6,
              f"页面 yview {before:.4f} -> {after:.4f}")

        # 反向：弹窗**自己**的滚动容器必须照常能滚 —— 别把过滤做过头。
        tall = W.ScrollFrame(m.content, app.theme)
        tall.pack(fill="both", expand=True)
        for i in range(30):
            tk.Label(tall.inner, text=f"modal-row-{i:02d}").pack(fill="x")
        tall.resync()
        settle(app, 10)
        check("构造出弹窗内可滚的容器", W._can_scroll_canvas(tall.canvas))
        if W._can_scroll_canvas(tall.canvas):
            pt = point_in_container(app, tall, tall.inner)
            check("能取到落在弹窗内滚动容器上的点", pt is not None)
            if pt is not None:
                t_before = yview(tall)[0]
                W._WHEEL["acc"] = 0.0
                send_wheel(app, W.deepest_at(m, pt[0], pt[1]), -120, pt)
                t_after = yview(tall)[0]
                check("弹窗自己的滚动容器照常能滚", t_after > t_before,
                      f"弹窗内 yview {t_before:.3f} -> {t_after:.3f}")
        try:
            m.close()
        except Exception:  # noqa: BLE001
            pass
        settle(app, 4)

    app.destroy()
    print()
    print(f"结果：{ok} OK / {fail} FAIL / {skip} SKIP")
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()
