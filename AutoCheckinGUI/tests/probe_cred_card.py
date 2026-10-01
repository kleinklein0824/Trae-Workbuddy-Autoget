#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设置页「账号凭据自动刷新」卡片：渲染 + 防裁切探针。

为什么要单开这个探针
--------------------
本次改动把这张卡从「3 行说明」扩成「总览 1 段 + 3 个平台 × 4 行」，
卡片高度涨了 4 倍多。本项目在「定尺寸容器 + 变长内容 = 静默够不着 /
被压扁」上已经踩过 3 次坑，所以必须**实测**，不能靠眼睛：
  · 每行文字是否真的排下来了（``winfo_reqheight()`` vs ``winfo_height()``）；
  · 卡片底部是否落在滚动区域内（用户能不能滚到它）。

★ 判据必须自带正对照：第 5 节会把一段长文本塞进固定 30px 高的框里 ——
  此时**必须**报出裁切。否则「全绿」毫无意义（分不清「真的没问题」和
  「断言从未生效」）。
"""
import os
import sys
import tkinter as tk
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
_PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
             else os.path.dirname(os.path.dirname(GUI_DIR)))
for _p in (_PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

TITLE = "账号凭据自动刷新"

OK = FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  OK   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s   %s" % (label, extra))


class FakeApp(tk.Tk):
    """SettingsPage 需要的最小宿主（对照 tests/probe_all_modals.py）。"""

    def __init__(self):
        super().__init__()
        self.withdraw()
        self.dark = False

    def ui_call(self, fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def toast(self, *a, **k):
        pass

    def refresh_task(self, *a, **k):
        pass

    def set_theme(self, *a, **k):
        pass

    def refresh_accounts(self, on_done=None, *a, **k):
        if on_done:
            on_done()

    def _refresh_topbar(self):
        pass


def settle(root, n=8):
    for _ in range(n):
        try:
            root.update_idletasks()
            root.update()
        except tk.TclError:
            return


def labels_in(w):
    """按视觉顺序收集子树里所有 Label 的 (控件, 文本)。"""
    out = []
    for c in w.winfo_children():
        if c.winfo_class() == "Label":
            try:
                t = str(c.cget("text"))
            except tk.TclError:
                t = ""
            if t:
                out.append((c, t))
        out.extend(labels_in(c))
    return out


def clip_problems(container):
    """哪些 Label 被压扁了。判据必须比 reqheight（见 MEMORY-detail.md）。"""
    bad = []
    for w, t in labels_in(container):
        try:
            req, got = w.winfo_reqheight(), w.winfo_height()
        except tk.TclError:
            continue
        if req > got + 1:
            bad.append((t[:40], req, got))
    return bad


def find_card(page):
    for w in page.body.winfo_children():
        if not hasattr(w, "body"):
            continue
        try:
            if any(str(s.cget("text")) == TITLE
                   for s in w.body.winfo_children()):
                return w
        except tk.TclError:
            pass
    return None


def main():
    from app.pages.settings import SettingsPage
    from app.theme import Theme

    app = FakeApp()
    app.deiconify()
    app.geometry("1180x760+120+80")
    settle(app, 12)

    page = SettingsPage(app, Theme(False), app, module="traework")
    page.pack(fill="both", expand=True)
    settle(app, 16)

    print("== 1. 卡片存在 ==")
    card = find_card(page)
    check("找到「%s」卡片" % TITLE, card is not None)
    if card is None:
        app.destroy()
        print("\n结果：%d OK / %d FAIL" % (OK, FAIL + 1))
        return 1

    print("\n== 2. 卡片文字（人工复核措辞）==")
    _texts = [t for _w, t in labels_in(card.body)]
    for t in _texts:
        print("   | %s" % t)
    joined = "\n".join(_texts)
    check("卡片带「捕获方式」总览段",
          any("桌面端捕获" in t for t in _texts))
    check("TraeCN 行写明「捕获方式：桌面端」",
          any("TraeCN（IDE）" in t and "桌面端" in t for t in _texts))
    check("WorkBuddy 行写明「捕获方式：桌面端」",
          any("WorkBuddy（桌面端）" in t and "捕获方式：桌面端" in t
              for t in _texts))
    check("TraeWork 行写明「捕获方式：本地浏览器会话」",
          any("TraeWork CN（网页签到）" in t and "本地浏览器会话" in t
              for t in _texts))
    check("【负对照】卡片里不再出现旧标签「（网页会话）」",
          "（网页会话）" not in joined)
    check("【负对照】卡片里不再出现「打开一次桌面端并登录」",
          "打开一次桌面端并登录" not in joined)
    check("每个平台都写了「为什么」（共 3 条）",
          joined.count("为什么：") == 3, joined.count("为什么："))

    print("\n== 3. 没有被压扁 ==")
    bad = clip_problems(card.body)
    check("卡片内 %d 个 Label 全部完整渲染（reqheight <= height）"
          % len(labels_in(card.body)), not bad,
          "被压扁 %d 处：%s" % (len(bad), bad[:2]))

    print("\n== 4. 卡片在滚动区域内（能滚到）==")
    try:
        bbox = page.scroll.canvas.bbox("all")
        bottom = card.winfo_y() + card.winfo_height()
        print("     卡片底部 y=%d，scrollregion=%s，bbox=%s"
              % (bottom, page.scroll.canvas.cget("scrollregion"), bbox))
        check("卡片底部落在 scrollregion 内（用户能滚到）",
              bool(bbox) and bbox[3] + 2 >= bottom,
              "bbox=%s bottom=%d" % (bbox, bottom))
    except Exception as e:  # noqa: BLE001
        check("读 scrollregion", False, repr(e))

    print("\n== 5. 【正对照】判据本身必须是活的 ==")
    # 把一段长文本塞进固定 30px 高的框里 → 必然被压扁，判据**必须**报出来。
    holder = tk.Frame(page.body, height=30, bg=Theme(False).bg_soft)
    holder.pack_propagate(False)
    tk.Label(holder, text=("长文本" * 60) + "\n" * 6,
             justify="left").pack(fill="both", expand=True)
    settle(app, 8)
    forced = clip_problems(holder)
    check("正对照：塞进 30px 高的框里，判据报出裁切", bool(forced),
          "判据没报出 → 上面第 3 节的「全绿」不可信")
    if forced:
        print("       （报出 %d 处，例如 req=%d got=%d）"
              % (len(forced), forced[0][1], forced[0][2]))
    holder.destroy()

    app.destroy()
    print("\n结果：%d OK / %d FAIL" % (OK, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
