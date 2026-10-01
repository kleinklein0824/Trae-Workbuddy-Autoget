#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终极定案（v2，用 _shot_util 正确换算物理坐标）：
同一次运行里 量几何 → 按物理坐标抓整窗图 → 扫像素，全部对齐。

v1 的失败原因：`ImageGrab.grab(bbox=Tk逻辑坐标)` 在 DPI 虚拟化进程里
只抓到窗口左上 80% 区域 → 扫不到引言文字（暗像素 0 个）。
v2 用 `_shot_util.grab_window()`（自动乘 1.25），坐标就对齐了。

判据：
  · 引言第一行的最右暗像素 x  ≈ 引言左边界 + 渲染器算出的行宽
    （**不是**顶到图像右边缘）
  · 该 x 明显小于图像右边缘（说明右边还有空白 → 没被裁）
"""
import os
import sys
import tkinter as tk
import tkinter.font as tkfont

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
_PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
             else os.path.dirname(os.path.dirname(GUI_DIR)))
for _p in (_PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

OUT = os.path.join(HERE, "_shots")
OK = FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  OK   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s   %s" % (label, extra))


def settle(root, n=14):
    for _ in range(n):
        try:
            root.update_idletasks()
            root.update()
        except tk.TclError:
            return


def safe_text(w):
    try:
        return str(w.cget("text")) if w.winfo_class() == "Label" else ""
    except tk.TclError:
        return ""


def labels_in(w):
    out = []
    for c in w.winfo_children():
        if c.winfo_class() == "Label":
            out.append(c)
        out.extend(labels_in(c))
    return out


def greedy(font, text, wrap):
    lines, cur = [], ""
    for ch in text:
        if font.measure(cur + ch) > wrap and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def main():
    from app import config as _C  # noqa: F401
    from app.application import App
    import _shot_util as SU

    os.makedirs(OUT, exist_ok=True)
    app = App()
    app.geometry("1180x900+80+40")
    app.deiconify()
    settle(app, 20)
    for _ in range(6):
        app.goto("traework", "settings")
        settle(app, 14)
        if app.page is not None and type(app.page).__name__ == "SettingsPage":
            break
    page = app.page
    card = None
    for w in page.body.winfo_children():
        if not hasattr(w, "body"):
            continue
        if any(safe_text(s) == "账号凭据自动刷新"
               for s in w.body.winfo_children()):
            card = w
            break
    if card is None:
        print("!! 没找到凭据卡")
        app.destroy()
        return 1
    try:
        cv = page.scroll.canvas
        total = float(cv.bbox("all")[3])
        cv.yview_moveto(max(0.0, (card.winfo_y() - 20) / total))
    except Exception as e:  # noqa: BLE001
        print("!! 滚动失败 %r" % (e,))
    settle(app, 18)
    app.lift()
    try:
        app.focus_force()
    except tk.TclError:
        pass
    settle(app, 18)

    intro = None
    for c in labels_in(card.body):
        if safe_text(c).startswith("三个平台的登录凭据寿命"):
            intro = c
            break
    if intro is None:
        print("!! 没找到引言 Label")
        app.destroy()
        return 1

    font = tkfont.Font(font=intro.cget("font"))
    ls = font.metrics("linespace")
    wrap = int(intro.cget("wraplength") or 0)
    lines = greedy(font, safe_text(intro), wrap)
    lw = [font.measure(x) for x in lines]

    scale = SU.dpi_scale(app)
    print("DPI 缩放比（物理/虚拟）= %.4f" % scale)
    print("窗口 Tk 几何 = %dx%d @ (%d,%d)"
          % (app.winfo_width(), app.winfo_height(),
             app.winfo_rootx(), app.winfo_rooty()))
    print("引言 Tk 几何 = %dx%d @ (%d,%d)  req=%dx%d  wraplength=%d"
          % (intro.winfo_width(), intro.winfo_height(),
             intro.winfo_rootx(), intro.winfo_rooty(),
             intro.winfo_reqwidth(), intro.winfo_reqheight(), wrap))
    print("渲染器口径：%d 行，宽度 %s" % (len(lines), lw))
    for i, x in enumerate(lines):
        print("   L%d %4dpx %s" % (i + 1, lw[i], x))

    # ---- 按**物理**坐标抓完整窗口 ----
    img = SU.grab_window(app, scale)
    p = os.path.join(OUT, "final_window_fixed.png")
    img.save(p)
    gray = img.convert("L")
    W, H = img.size
    print("\n整窗图（物理）= %dx%d -> %s" % (W, H, p))

    # 引言第一行在图里的 y 带 / x 起点
    ix, iy = SU.to_image_xy(app, intro)
    y0, y1 = iy, iy + int(ls * scale) + 3
    px = gray.load()
    right, cnt = -1, 0
    for y in range(max(0, y0), min(H, y1)):
        for x in range(ix + 2, W - 4):
            if px[x, y] < 180:
                cnt += 1
                if x > right:
                    right = x
    expect = ix + int(lw[0] * scale)
    print("\n引言第一行图内 y=%d..%d，x 起点=%d" % (y0, y1, ix))
    print("  暗像素 %d 个；最右暗像素 x=%d" % (cnt, right))
    print("  预期右端 = %d（起点 %d + 行宽 %d × %.2f）"
          % (expect, ix, lw[0], scale))
    print("  图像右边缘 = %d" % (W - 1))

    print("\n== 判据 ==")
    check("扫到了引言文字（暗像素 > 0）", cnt > 0, "cnt=%d" % cnt)
    d = abs(right - expect) if right >= 0 else 9999
    check("第一行最右像素(%d) ≈ 预期右端(%d)，相差 %dpx（容差 10）"
          % (right, expect, d), d <= 10)
    check("第一行没有顶到图像右边缘（右边还有 %dpx 空白）"
          % (W - 1 - right), right >= 0 and (W - 1 - right) >= 20,
          "right=%d W=%d" % (right, W - 1))
    check("引言控件给足宽度（req %d <= got %d）"
          % (intro.winfo_reqwidth(), intro.winfo_width()),
          intro.winfo_reqwidth() <= intro.winfo_width() + 1)
    check("引言控件给足高度（req %d <= got %d）"
          % (intro.winfo_reqheight(), intro.winfo_height()),
          intro.winfo_reqheight() <= intro.winfo_height() + 1)

    app.destroy()
    print("\n结果：%d OK / %d FAIL" % (OK, FAIL))
    sys.stdout.flush()
    os._exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    sys.exit(main())
