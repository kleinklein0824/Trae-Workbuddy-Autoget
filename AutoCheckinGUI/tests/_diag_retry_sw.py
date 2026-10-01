#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：设置页「自动补跑」卡里的弹窗策略开关为什么 winfo_ismapped() == 0。

`probe_retry_card` 有一条 `check("开关已显示", bool(sw.winfo_ismapped()))` 恒失败，
而同卡片的 `card_retry` 本身 ismapped 正常 —— 要分清是
  (a) 控件真的没显示出来（用户点不到，**真 bug**），还是
  (b) 判据用错了（比如 W.Switch 是 Canvas 子类，ismapped 语义不同）。

做法：把从 sw 一路到 toplevel 的**每一层**的 class / manager / ismapped /
geometry / reqsize 全打出来；再按物理坐标截一张该卡片的图做目视对照。
"""
import os
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
_PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
             else os.path.dirname(os.path.dirname(GUI_DIR)))
for _p in (HERE, _PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

OUT = os.path.join(HERE, "_shots")


def settle(root, n=16):
    for _ in range(n):
        try:
            root.update_idletasks()
            root.update()
        except tk.TclError:
            return


def chain(w):
    out = []
    cur = w
    while cur is not None:
        out.append(cur)
        try:
            cur = cur.master
        except Exception:  # noqa: BLE001
            break
    return out


def dump(w, tag):
    try:
        cls = w.winfo_class()
    except tk.TclError:
        cls = "?"
    try:
        mgr = w.winfo_manager() or "-"
    except tk.TclError:
        mgr = "?"
    try:
        mapped = w.winfo_ismapped()
        vis = w.winfo_viewable()
        geo = "%dx%d+%d+%d" % (w.winfo_width(), w.winfo_height(),
                               w.winfo_x(), w.winfo_y())
        req = "%dx%d" % (w.winfo_reqwidth(), w.winfo_reqheight())
    except tk.TclError:
        mapped = vis = "?"
        geo = req = "?"
    print("  %-22s %-10s mgr=%-6s ismapped=%s viewable=%s geo=%-18s req=%s"
          % (tag, cls, mgr, mapped, vis, geo, req))


def main():
    from app.application import App
    import _shot_util as SU

    os.makedirs(OUT, exist_ok=True)
    app = App()
    app.geometry("1180x900+80+40")
    app.deiconify()
    settle(app, 20)
    app._switch_module("traework")
    settle(app, 8)
    app.goto("traework", "settings")
    settle(app, 16)
    page = app.page
    print("页面 = %s" % type(page).__name__)

    sw = getattr(page, "_retry_toast_sw", None)
    card = getattr(page, "card_retry", None)
    print("\n== 祖先链：从开关一路到 toplevel ==")
    if sw is not None:
        for i, w in enumerate(chain(sw)):
            dump(w, ("sw" if i == 0 else "  ^%d" % i))
    else:
        print("  !! 没有 _retry_toast_sw")

    print("\n== 同一张卡里的对照控件 ==")
    if card is not None:
        dump(card, "card_retry")
    for nm in ("_retry_iv", "_retry_win", "_retry_on", "_retry_hint"):
        w = getattr(page, nm, None)
        if w is None:
            print("  %-22s (None)" % nm)
            continue
        # _num_field / _retry_on 返回的可能是 Entry / BooleanVar
        if hasattr(w, "winfo_class"):
            dump(w, nm)
        else:
            print("  %-22s 不是控件（%s）" % (nm, type(w).__name__))

    # ---- 目视对照：把该卡片按物理坐标截下来 ----
    if card is not None:
        try:
            cv = page.scroll.canvas
            total = float(cv.bbox("all")[3])
            cv.yview_moveto(max(0.0, (card.winfo_y() - 20) / total))
        except Exception as e:  # noqa: BLE001
            print("  滚动失败 %r" % (e,))
        settle(app, 18)
        app.lift()
        try:
            app.focus_force()
        except tk.TclError:
            pass
        settle(app, 18)
        k = SU.dpi_scale(app)
        img = SU.grab_window(app, k)
        p = os.path.join(OUT, "retry_card_ctx.png")
        img.save(p)
        print("\n已保存整窗图 %s (%dx%d)  ← 看「自动补跑」卡里有没有开关"
              % (p, img.width, img.height))
        ix, iy = SU.to_image_xy(app, card)
        print("卡片在图内物理坐标 = (%d,%d)，卡片 %dx%d"
              % (ix, iy, card.winfo_width(), card.winfo_height()))
        c = img.crop((max(0, ix - 10), max(0, iy - 10),
                      min(img.width, ix + int(card.winfo_width() * k) + 10),
                      min(img.height, iy + int(card.winfo_height() * k) + 10)))
        p2 = os.path.join(OUT, "retry_card_only.png")
        c.save(p2)
        print("已保存卡片图 %s (%dx%d)" % (p2, c.width, c.height))

    app.destroy()
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
