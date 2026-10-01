#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全页面布局审计：找出「控件被裁掉 / 内容没拉伸」这类**不报错但看得见**的 UI 缺陷。

不靠肉眼看截图 —— 截图会被拼接伪影、缩放、遮挡干扰，判断不可靠。
这里用可断言的几何判据，遍历 15 个页面逐一检查：

  1. **[未拉伸]** 卡片内层 body 宽度 < 卡片内容宽度
     → Card 的 body 靠 canvas 的 window item 承载，宽度由 _sync_size 的
       itemconfigure(width=...) 决定。没跟上 canvas 宽度时，body 右侧的
       控件会被父容器裁掉，表现为「按钮/文字凭空消失」且没有任何报错。
       （真实事故：「开始签到」按钮和「全不选」整个不见了）

  2. **[纵向裁切]** body 内容需求高度 > 卡片实际高度
     → 卡片底部的提示行被切掉。（真实事故：12 张统计卡的提示行少了 5px）

  3. **[未换行]** wraplength=0 且文本很长的 Label
     → tk.Label 默认不换行，长路径/长 ID 会冲出右边缘被裁。

  4. **[横向溢出]** 子控件右边缘超出卡片宽度

被 pack_forget/grid_remove 隐藏的控件不参与布局，一律跳过（不算问题）。

用法：python tests/test_layout_clip.py     # 0 = 全部通过
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
sys.path.insert(0, GUI)
sys.path.insert(0, os.path.dirname(GUI))

import tkinter as tk  # noqa: E402

from app.application import App  # noqa: E402
from app.theme import Theme  # noqa: E402
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

TEXTY = (tk.Label, tk.Message)


def pump(app, n=6, dt=0.08):
    for _ in range(n):
        app.update()
        time.sleep(dt)


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


def _path(w):
    """控件的 Tk 路径 + 所属页面，便于定位问题控件。"""
    try:
        p = str(w)
    except Exception:  # noqa: BLE001
        return "?"
    # 只保留后半段，太长
    parts = p.split(".")
    return "." + ".".join(parts[-4:]) if len(parts) > 4 else p


def _owner(w):
    """向上找到所属的页面类名。"""
    cur = w
    while cur is not None:
        cls = type(cur).__name__
        if cls.endswith("Page"):
            return cls
        cur = getattr(cur, "master", None)
    return "?"


def check_page(app, key):
    issues = []
    for w in walk(app):
        if not isinstance(w, W.Card):
            continue
        # 被 pack_forget/grid_remove 隐藏的卡片不参与布局，
        # 它的高度当然是 1px —— 那是正常的，不能算裁切。
        try:
            if not w.winfo_manager():
                continue
        except tk.TclError:
            continue
        try:
            w.update_idletasks()
            # ★ 关键判据：内层 body 必须被拉伸到卡片宽度。
            # Card 的 body 靠 canvas 的 window item 承载，宽度由 _sync_size
            # 的 itemconfigure(width=...) 决定。一旦没跟上 canvas 宽度，
            # body 右侧的内容会被父容器裁掉 —— 控件凭空消失、且不报错。
            cw = w._cv.winfo_width()
            bw = w.body.winfo_width()
            if cw > 60 and bw > 1 and bw < cw - w.padding * 2 - 8:
                issues.append(f"[未拉伸] body 宽 {bw} < 卡片内容宽 "
                              f"{cw - w.padding * 2}  归属={_owner(w)}"
                              f"  路径={_path(w)}")
            need = w.body.winfo_reqheight() + w.padding * 2
            have = w._cv.winfo_height()
            if need > have + 2:
                issues.append(f"[纵向裁切] 卡片需 {need}px / 实际 {have}px"
                              f"  首个标题≈{_first_text(w)!r}"
                              f"  归属={_owner(w)}  路径={_path(w)}")
        except tk.TclError:
            continue
        # 横向溢出 + 未换行的长文本
        try:
            cw = w.body.winfo_width()
            for c in walk(w.body):
                if isinstance(c, TEXTY):
                    txt = str(c.cget("text") or "")
                    if not txt:
                        continue
                    wrap = int(c.cget("wraplength") or 0)
                    req = c.winfo_reqwidth()
                    if wrap == 0 and req > cw + 4 and len(txt) > 24:
                        issues.append(
                            f"[未换行] wraplength=0 需求宽 {req} > 容器 {cw}"
                            f"  {txt[:26]!r}…")
                try:
                    rx = c.winfo_rootx() - w.winfo_rootx()
                    rw = c.winfo_width()
                    if rw > 1 and rx + rw > w.winfo_width() + 4:
                        issues.append(
                            f"[横向溢出] {type(c).__name__} 右边缘 "
                            f"{rx + rw} > 卡片 {w.winfo_width()}")
                except tk.TclError:
                    pass
        except tk.TclError:
            pass
    return issues


def _first_text(card):
    for c in walk(card.body):
        if isinstance(c, TEXTY):
            t = str(c.cget("text") or "").strip()
            if t:
                return t[:18]
    return ""


app = App()
app.geometry("1240x860+40+20")
pump(app, 14)

# ---------------------------------------------------------------- 判据自检（正对照）
# ★ 没有正对照的「0 处」不可信 —— 你分不清「真的没问题」和「断言从未生效」。
#   本项目已经踩过：一个判据天生是瞎的（用 `winfo_height()` 判裁切），
#   而断言全绿。所以这里先造一个**必然被裁切**的卡片，check_page 必须报出来；
#   报不出来就直接以退出码 2 失败，绝不给出「合计问题：0」。
_probe_host = tk.Frame(app, height=30, bg="#ffffff")
_probe_host.pack(fill="x")
_probe_host.pack_propagate(False)
_probe_card = W.Card(_probe_host, Theme(False))
_probe_card.pack(fill="both", expand=True)
tk.Label(_probe_card.body, text=("判据自检用的超长文本 " * 30) + "\n" * 5,
         justify="left").pack(fill="x")
pump(app, 8)
_probe_issues = check_page(app, "selfcheck")
_probe_host.destroy()
pump(app, 4)
if not any("纵向裁切" in i for i in _probe_issues):
    print("FATAL 判据自检失败：故意压扁的卡片没有被报出来 → 下面的「0 处」不可信")
    print("      实际返回：%r" % (_probe_issues,))
    app.destroy()
    sys.exit(2)
print(f"判据自检 OK：故意压扁的卡片被报出 {len(_probe_issues)} 处问题\n")

total = 0
for mod, view in PAGES:
    app.goto(mod, view)
    pump(app, 14)
    issues = check_page(app, f"{mod}/{view}")
    tag = "OK  " if not issues else "FAIL"
    print(f"{tag} {mod}/{view}  ({len(issues)} 处)")
    for i in issues:
        print("       " + i)
    total += len(issues)

print(f"\n合计问题：{total}")
app.destroy()
sys.exit(0 if total == 0 else 1)
