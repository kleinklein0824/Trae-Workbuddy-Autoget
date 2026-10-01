#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全按键 / 全模块健康审计：把界面上**每一个可交互控件**都过一遍。

为什么需要它
------------
2026-09-18 用户要求「彻底检查一下 exe 所有按键模块的所有功能是否健康完善」。
`audit_pages.py` 只能发现「页面渲染抛异常」，发现不了这些**静默**缺陷：
  * 按钮没有绑定 command（点了完全没反应，但界面看起来正常）；
  * 按钮被容器压扁 / 压窄（`winfo_height() < winfo_reqheight()`），
    文字只剩半行 —— 本项目已经栽过 4 次（见 `W.auto_wrap` 与 `Modal.fit_height`）；
  * 按钮被挤出父容器右边缘（窄窗口下「开始签到」整个看不见）；
  * 开关控件（`W.Switch`，Canvas 自绘）没有绑 `<Button-1>`，点了不翻转。

判据为什么必须这样写
--------------------
pack/grid 空间不够时会把控件**压扁**，此时 `winfo_height()` 返回的是**被压后**
的高度 —— 所以「`y + height` 有没有超出容器」这类判据**天生是瞎的**（压扁时
它反而"没超出"）。唯一可靠的做法是拿 `winfo_reqheight()`（控件自己要求的高度）
去比 `winfo_height()`（实际分到的高度）。

本脚本**内置正对照**：故意造一个被压扁的按钮、一个没绑 command 的按钮，
判据必须把它们报出来；报不出来说明判据是死的，直接判 FAIL。

用法：python tests/audit_buttons.py
产物：tests/audit_buttons_result.txt
"""
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
sys.path.insert(0, GUI)

import tkinter as tk  # noqa: E402

OUT = os.path.join(HERE, "audit_buttons_result.txt")
LOG = []

# 全部页面矩阵：三个模块 × 四个视图，外加三个全局视图
PAGES = []
for _m in ("traework", "traecn", "workbuddy"):
    for _v in ("dashboard", "checkin", "accounts", "credits"):
        PAGES.append((_m, _v))
PAGES += [("traework", "logs"), ("traework", "settings"), ("traework", "api-service")]

# 「安全调用」白名单：这些按钮点了只重画/只切换选择，不会改数据、不会开浏览器。
# 其余按钮（移除 / 重置设备码 / 清空历史 / 登录 / 捕获…）**只做静态检查**，
# 绝不替用户按下 —— 审计脚本不能有副作用。
SAFE_INVOKE = {
    "刷新", "全选", "全不选", "去设置", "复制", "复制示例", "复制模型 ID",
    "刷新状态", "账号池明细", "查看池明细", "查看令牌", "清空", "清除冷却",
}

ok = fail = 0
ISSUES = []


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        LOG.append(f"  OK   {label}")
    else:
        fail += 1
        LOG.append(f"  FAIL {label}   {extra}")
        ISSUES.append(f"{label} :: {extra}")


def pump(app, n=8, dt=0.06):
    for _ in range(n):
        app.update()
        time.sleep(dt)


# ---------------------------------------------------------------- 判据

def collect_widgets(root):
    """收集 root 子树里所有可交互控件（含自绘的 Switch）。"""
    from app.ui import widgets as W
    out = []

    def walk(w):
        if isinstance(w, (tk.Button, tk.Checkbutton, tk.Radiobutton,
                          tk.Entry, tk.Spinbox, tk.Scale, W.Switch)):
            out.append(w)
        for c in w.winfo_children():
            walk(c)

    walk(root)
    return out


def label_of(w):
    try:
        return str(w.cget("text")).strip()
    except Exception:  # noqa: BLE001
        return ""


def wiring_problem(w):
    """控件有没有真的「接上线」。返回问题描述或 None。"""
    from app.ui import widgets as W
    if isinstance(w, W.Switch):
        # 自绘开关：必须有 <Button-1> 绑定，否则点了不翻转
        try:
            if not w.bind("<Button-1>"):
                return "Switch 没有绑定 <Button-1>（点了不翻转）"
        except Exception:  # noqa: BLE001
            return "Switch 的绑定读不出来"
        return None
    if isinstance(w, tk.Button):
        try:
            cmd = str(w.cget("command") or "").strip()
        except Exception:  # noqa: BLE001
            cmd = ""
        if not cmd:
            return "按钮没有绑定 command（点了没反应）"
        return None
    if isinstance(w, (tk.Checkbutton, tk.Radiobutton)):
        try:
            cmd = str(w.cget("command") or "").strip()
            var = str(w.cget("variable") or "").strip()
        except Exception:  # noqa: BLE001
            return None
        if not cmd and not var:
            return "勾选框既没绑 command 也没绑 variable（点了没反应）"
        return None
    return None


def geometry_problem(w):
    """控件有没有被压扁 / 压窄。返回问题描述或 None。

    必须比 `winfo_reqheight()`：pack 空间不足会把控件压扁，
    `winfo_height()` 返回的正是**被压后**的高度。
    """
    try:
        if not w.winfo_ismapped():
            return None
        if w.winfo_width() <= 1 or w.winfo_height() <= 1:
            return None                      # 还没完成布局
        req_h, act_h = w.winfo_reqheight(), w.winfo_height()
        req_w, act_w = w.winfo_reqwidth(), w.winfo_width()
    except Exception:  # noqa: BLE001
        return None
    if act_h + 1 < req_h:
        return f"被压扁：实际高 {act_h} < 需求 {req_h}"
    if act_w + 1 < req_w:
        return f"被压窄：实际宽 {act_w} < 需求 {req_w}"
    return None


def overflow_problem(w):
    """控件有没有被挤出**父容器**右边缘。返回问题描述或 None。"""
    try:
        par = w.master
        if par is None or not w.winfo_ismapped():
            return None
        pw = par.winfo_width()
        if pw <= 1:
            return None
        x = w.winfo_rootx() - par.winfo_rootx()
        right = x + w.winfo_width()
    except Exception:  # noqa: BLE001
        return None
    if right > pw + 3:
        return f"右溢出父容器：右沿 {right} > 父宽 {pw}"
    return None


# ---------------------------------------------------------------- 主流程

def main():
    global ok, fail
    from app.application import App
    from app.ui import widgets as W

    app = App()
    app.geometry("1240x860+40+20")
    pump(app, 14)

    cb_errors = []

    def report(exc, val, tb):
        cb_errors.append("".join(traceback.format_exception(exc, val, tb)))

    app.report_callback_exception = report

    LOG.append("=" * 72)
    LOG.append("一、判据自检（正对照 / 负对照）—— 判据本身必须是活的")
    LOG.append("=" * 72)
    # ★ 正对照的控件必须**真的被 pack 进可见层级**：没映射的控件
    #   `winfo_ismapped()` 为假，判据会提前返回 None —— 那样"正对照通过"
    #   就变成了"判据根本没跑"，反而掩盖了问题（第一版就踩了这个坑）。
    # 正对照 1：故意造一个被压扁的按钮（容器固定 12px 高，按钮需求约 25px）
    squashed_box = tk.Frame(app.page_host, width=220, height=12)
    squashed_box.pack(side="bottom")
    squashed_box.pack_propagate(False)
    squashed = W.button(squashed_box, app.theme, "正对照·被压扁",
                        command=lambda: None, small=True)
    squashed.pack(fill="both", expand=True)
    pump(app, 8)
    prob = geometry_problem(squashed)
    check("【正对照】被压扁的按钮必须被 geometry_problem 报出",
          prob is not None, prob)
    # 正对照 2：故意造一个没绑 command 的按钮
    bare = tk.Button(squashed_box, text="正对照·没接线")
    bare.pack()
    pump(app, 2)
    prob2 = wiring_problem(bare)
    check("【正对照】没绑 command 的按钮必须被 wiring_problem 报出",
          prob2 is not None, prob2)
    # 负对照：正常尺寸容器里的正常按钮不该被误报
    good_box = tk.Frame(app.page_host)
    good_box.pack(side="bottom")
    good = W.button(good_box, app.theme, "负对照·正常", command=lambda: None)
    good.pack()
    pump(app, 8)
    check("【负对照】正常按钮不该被判据误报",
          wiring_problem(good) is None and geometry_problem(good) is None,
          f"{wiring_problem(good)} / {geometry_problem(good)}")
    squashed_box.destroy()
    good_box.destroy()
    pump(app, 4)

    LOG.append("")
    LOG.append("=" * 72)
    LOG.append("二、逐页审计每一个可交互控件")
    LOG.append("=" * 72)

    total = 0
    per_page = {}
    for module, view in PAGES:
        key = f"{module}/{view}"
        cb_errors.clear()
        try:
            if module != app.module:
                app._switch_module(module)
                pump(app, 6)
            app.goto(module, view)
            pump(app, 12)
            page = app.page
            widgets = collect_widgets(page)
            per_page[key] = len(widgets)
            total += len(widgets)

            bad = []
            for w in widgets:
                cls = type(w).__name__
                txt = label_of(w)
                tag = f"{cls}[{txt[:16] or '—'}]"
                for fn, kind in ((wiring_problem, "接线"),
                                 (geometry_problem, "布局"),
                                 (overflow_problem, "溢出")):
                    p = fn(w)
                    if p:
                        bad.append(f"{kind}: {tag} → {p}")
            if cb_errors:
                bad.append(f"回调异常 {len(cb_errors)} 处：" +
                           cb_errors[0].strip().splitlines()[-1][:120])
            if bad:
                for b in bad[:8]:
                    LOG.append(f"  FAIL {key}: {b}")
                    ISSUES.append(f"{key}: {b}")
                fail += 1
            else:
                LOG.append(f"  OK   {key}：{len(widgets)} 个可交互控件全部健康")
                ok += 1
        except Exception:  # noqa: BLE001
            fail += 1
            LOG.append(f"  FAIL {key}: 审计抛异常\n" +
                       traceback.format_exc().replace("\n", "\n       "))
            ISSUES.append(f"{key}: 审计抛异常")

    LOG.append("")
    LOG.append(f"  小计：{len(PAGES)} 个页面，{total} 个可交互控件")
    for k, v in per_page.items():
        LOG.append(f"      {k:26s} {v:3d}")

    LOG.append("")
    LOG.append("=" * 72)
    LOG.append("三、顶栏 / 标题栏 / 侧栏")
    LOG.append("=" * 72)
    tb = app.topbar
    check("顶栏「一键签到」按钮存在", hasattr(tb, "_btn_main"))
    check("顶栏「一键签到」绑了 quick_checkin",
          "quick_checkin" in str(tb._btn_main.cget("command")),
          tb._btn_main.cget("command"))
    check("顶栏按钮文案写明范围（全部平台）",
          "全部平台" in str(tb._btn_main.cget("text")),
          tb._btn_main.cget("text"))
    check("顶栏主题切换按钮存在且已接线",
          hasattr(tb, "_btn_theme") and bool(tb._btn_theme.cget("command")))
    for nm, w in (("顶栏一键签到", tb._btn_main), ("顶栏主题", tb._btn_theme)):
        check(f"{nm} 未被压扁", geometry_problem(w) is None, geometry_problem(w))
    bar_btns = [w for w in tb.winfo_children() for w in w.winfo_children()
                if isinstance(w, tk.Button)]
    check("顶栏所有按钮都有 command",
          all(str(w.cget("command") or "").strip() for w in bar_btns),
          [w.cget("text") for w in bar_btns if not str(w.cget("command") or "").strip()])

    tbar = app.titlebar
    tbar_btns = collect_widgets(tbar)
    check("标题栏控件都被接线",
          all(wiring_problem(w) is None for w in tbar_btns),
          [label_of(w) for w in tbar_btns if wiring_problem(w)])
    check("标题栏控件未被压扁",
          all(geometry_problem(w) is None for w in tbar_btns),
          [f"{label_of(w)}:{geometry_problem(w)}" for w in tbar_btns
           if geometry_problem(w)])

    sb = app.sidebar
    sb_btns = [w for w in collect_widgets(sb)]
    check("侧栏控件都被接线",
          all(wiring_problem(w) is None for w in sb_btns),
          [label_of(w) for w in sb_btns if wiring_problem(w)])

    LOG.append("")
    LOG.append("=" * 72)
    LOG.append("四、安全调用：真的按一下（只限无副作用的白名单）")
    LOG.append("=" * 72)
    invoked = 0
    for module, view in PAGES:
        try:
            if module != app.module:
                app._switch_module(module)
                pump(app, 5)
            app.goto(module, view)
            pump(app, 10)
            page = app.page
            for w in collect_widgets(page):
                if not isinstance(w, tk.Button):
                    continue
                txt = label_of(w)
                if txt not in SAFE_INVOKE:
                    continue
                if str(w.cget("state")) == "disabled":
                    continue
                cb_errors.clear()
                try:
                    w.invoke()
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    LOG.append(f"  FAIL {module}/{view} 「{txt}」调用抛异常: {e!r}")
                    ISSUES.append(f"{module}/{view} 「{txt}」调用抛异常")
                    continue
                pump(app, 4)
                invoked += 1
                if cb_errors:
                    fail += 1
                    LOG.append(f"  FAIL {module}/{view} 「{txt}」触发回调异常: "
                               f"{cb_errors[0].strip().splitlines()[-1][:120]}")
                    ISSUES.append(f"{module}/{view} 「{txt}」触发回调异常")
            # 页面可能被安全调用切走了，回到本页继续
            if app.page is not page:
                app.goto(module, view)
                pump(app, 6)
        except Exception:  # noqa: BLE001
            fail += 1
            LOG.append(f"  FAIL 安全调用 {module}/{view} 抛异常\n" +
                       traceback.format_exc().replace("\n", "\n       "))
    LOG.append(f"  实际按下 {invoked} 个无副作用按钮，全部未抛异常"
               if invoked else "  （没有可安全调用的按钮）")
    if invoked:
        ok += 1

    try:
        app.destroy()
    except Exception:  # noqa: BLE001
        pass

    LOG.append("")
    LOG.append("=" * 72)
    LOG.append(f"结果：{ok} OK / {fail} FAIL（共审计 {total} 个可交互控件）")
    if ISSUES:
        LOG.append("问题清单：")
        for i in ISSUES:
            LOG.append("  - " + i)
    LOG.append("=" * 72)

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(LOG))
    print("\n".join(LOG))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(LOG) + "\n\nFATAL:\n" + traceback.format_exc())
        raise
