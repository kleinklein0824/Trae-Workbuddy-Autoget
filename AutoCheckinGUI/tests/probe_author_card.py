#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作者卡片「奖励作者」收款码显示验收：几何数字 + 截图。

为什么需要它
-----------
用户要求把真实收款码图片放进「奖励作者」区域并**保证完整显示**。
"完整"是个视觉结论，必须两个方向都验：
  * 数字：图片 Label 的右/下边界必须落在弹窗内（不被裁切）；
  * 截图：亮/暗主题各来一张，肉眼确认。

跑法：python tests/probe_author_card.py
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

SHOTS = os.path.join(HERE, "_shots")


class FakeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self._toasts = []

    def toast(self, k, t, ms=3200):
        self._toasts.append((k, t))


def settle(app, n=16):
    for _ in range(n):
        app.update()
        time.sleep(0.03)


def find_image_label(widget):
    """在控件树里找带 image 的 Label。"""
    out = []
    try:
        if isinstance(widget, tk.Label) and widget.cget("image"):
            out.append(widget)
    except Exception:  # noqa: BLE001
        pass
    for ch in widget.winfo_children():
        out.extend(find_image_label(ch))
    return out


def run(app, dark):
    from app.theme import Theme
    from app.ui.author_card import show_author_modal, _work_area

    theme = Theme(dark)
    win = show_author_modal(app, theme)
    settle(app, 20)

    wx, wy = win.winfo_rootx(), win.winfo_rooty()
    ww, wh = win.winfo_width(), win.winfo_height()
    wl, wt, wr, wb = _work_area(win)
    in_area = (wx >= wl - 1 and wy >= wt - 1
               and wx + ww <= wr + 1 and wy + wh <= wb + 1)
    print(f"  工作区 {wl},{wt}-{wr},{wb}  弹窗 {ww}x{wh} @({wx},{wy})"
          f" 落在工作区内:{in_area}")
    labels = find_image_label(win)
    ok = True
    if not labels:
        print("  [FAIL] 未找到收款码图片 Label（是否走了占位兜底？）")
        ok = False
        info = None
    else:
        lb = labels[0]
        lx, ly = lb.winfo_rootx(), lb.winfo_rooty()
        lw, lh = lb.winfo_width(), lb.winfo_height()
        right_ok = (lx + lw) <= (wx + ww) + 1
        bottom_ok = (ly + lh) <= (wy + wh) + 1
        inside = (lx >= wx - 1) and (ly >= wy - 1) and right_ok and bottom_ok
        print(f"  图片 {lw}x{lh} @({lx},{ly})  右界{lx+lw}<= {wx+ww} :{right_ok}"
              f"  下界{ly+lh}<= {wy+wh} :{bottom_ok}")
        ok = inside and in_area and lw >= 150 and lh >= 200
        if not ok:
            print("  [FAIL] 图片未完整显示 / 弹窗超出工作区")
        info = (lw, lh)

    os.makedirs(SHOTS, exist_ok=True)
    name = "50_author_%s.png" % ("dark" if dark else "light")
    path = os.path.join(SHOTS, name)
    try:
        # 截图前临时置顶：overrideredirect 弹窗可能被别的窗口盖住，
        # 否则抓到的会是别人家的窗口（本项目踩过"截图内容对不上"的坑）
        win.attributes("-topmost", True)
        win.lift()
        settle(app, 8)
        from PIL import ImageGrab
        img = ImageGrab.grab()
        sw = app.winfo_screenwidth()
        k = img.size[0] / sw if sw else 1.0
        img.crop((int(wx * k), int(wy * k),
                  int((wx + ww) * k), int((wy + wh) * k))).save(path)
        print("  截图 →", path)
    except Exception as e:  # noqa: BLE001
        print("  截图失败：", e)
    finally:
        try:
            win.attributes("-topmost", False)
        except Exception:  # noqa: BLE001
            pass

    win.destroy()
    settle(app, 6)
    return ok, info


def main():
    app = FakeApp()
    app.geometry("1180x760+120+80")
    settle(app, 14)

    print("== 作者卡片收款码显示验收 ==")
    results = []
    for dark in (False, True):
        tag = "暗色" if dark else "亮色"
        print(f"-- {tag}主题 --")
        results.append(run(app, dark))

    app.destroy()
    passed = sum(1 for ok, _ in results if ok)
    print("\n通过 %d / 失败 %d" % (passed, len(results) - passed))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
