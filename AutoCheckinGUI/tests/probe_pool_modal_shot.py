#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""截图：账号池明细弹窗（含滚动容器）—— 确认"滚动容器底色和弹窗不一致"。

背景
----
上轮为了修「账号溢出够不着」，在 `_show_pool_detail` 里套了一个
`W.ScrollFrame(m.content, t)`。但 `ScrollFrame` 的 frame / canvas / inner
底色都是 **`theme.bg`**，而 `Modal.content` 是 **`theme.bg_soft`**：

    浅色主题   bg = #f1f5f9（slate-100）   bg_soft = #ffffff
    暗色主题   bg = #09090b（zinc-950）    bg_soft = #18181b

而行是 `tk.Frame(body, bg=t.bg_soft)` + `pady=3` → 行与行之间会**露出
容器的底色**，浅色下就是一条条灰缝。这是观感缺陷，必须看图确认。

⚠️ 上一轮的 `verify_api_modals` 抓的 `30_modal_pool_detail.png` **内容是错的**
（抓到了无关的桌面画面）—— 所以那份"视觉证据"不可用。本脚本重新抓，
并且**额外抓一圈外扩边距**，用来核对裁剪坐标到底对不对。

跑法：python tests/probe_pool_modal_shot.py
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
        self.withdraw()
        self.toasts = []
        self.module = "traework"
        self.view = "api-service"

    def ui_call(self, fn):
        pass

    def toast(self, k, t, ms=3200):
        self.toasts.append((k, t))

    def accounts(self):
        return {"traework": [], "workbuddy": [], "traecn": []}

    def refresh_accounts(self, on_done=None):
        if on_done:
            on_done()

    def goto(self, m, v):
        pass


class FakeLocal:
    def get_row(self, kind):
        return {"enabled": False, "base_url": "", "ok": False,
                "detail": "", "models": []}

    def enabled_kinds(self):
        return []

    def status(self):
        return []

    def model_ids(self):
        return []


class FakeSvc:
    def __init__(self, rows):
        self._rows = rows
        self.model = "glm-5.2"
        self.port = 7864
        self.api_key = ""
        self.running = False
        self.local = FakeLocal()

    @property
    def prefer_local(self):
        return True

    def pool_uids(self):
        return [r["uid"] for r in self._rows]

    def status(self):
        return {"running": False, "port": 7864, "total_requests": 0,
                "active_uid": None, "pool_count": len(self._rows),
                "pool_forwardable": sum(1 for r in self._rows
                                        if r.get("can_forward")),
                "cloud_ready": True, "last_error": None,
                "pool": list(self._rows), "local": [], "local_models": []}

    def ai_status(self):
        return {"local_ready": False, "local_detail": "测试",
                "prefer_local": True, "local_model": "",
                "stats": {"local": 0, "cloud": 0, "failed": 0, "fallback": 0}}

    def model_choices(self):
        return [("glm-5.2", "glm-5.2", "cloud")]

    def is_local_model(self, m):
        return str(m).startswith("local/")

    def local_model_ids(self):
        return []

    def probe_local(self, kind, on_done=None):
        if on_done:
            on_done(kind, False, "未启用", [])

    def configure(self, **kw):
        return {}

    def set_ai_prefs(self, **kw):
        return {}

    def clear_cooldowns(self):
        return 0


def make_rows(n):
    out = []
    for i in range(n):
        out.append({
            "uid": f"test-uid-{i:02d}", "name": f"账号-{i:02d}",
            "platform": "traecn" if i % 2 == 0 else "workbuddy",
            "credits": 100.0 + i, "credits_expire_at": None, "err_count": 0,
            "cooling": False, "cooldown_until": 0, "disabled": False,
            "cooldown_reason": "", "requests": i, "can_forward": i % 3 != 0,
            "state": "available" if i % 3 else "cooling",
        })
    return out


def settle(app, n=20):
    for _ in range(n):
        app.update()
        time.sleep(0.03)


def grab(app, x, y, w, h, name, pad=0):
    from PIL import ImageGrab
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    img = ImageGrab.grab()
    sw = app.winfo_screenwidth()
    k = img.size[0] / sw if sw else 1.0
    img.crop((int((x - pad) * k), int((y - pad) * k),
              int((x + w + pad) * k), int((y + h + pad) * k))).save(path)
    return path, k


def main():
    from app.pages.api_service import ApiServicePage
    from app.theme import Theme
    from app.ui import widgets as W

    app = FakeApp()
    app.deiconify()
    app.geometry("1180x760+120+80")
    settle(app, 14)

    for dark, tag in ((False, "light"), (True, "dark")):
        t = Theme(dark)
        page = ApiServicePage(app, t, app, "traework")
        page._svc = FakeSvc(make_rows(12))
        page._enabled = {r["uid"] for r in make_rows(12)}
        page._show_pool_detail()
        m = page._modal
        settle(app, 26)
        if m is None:
            print(f"[{tag}] 弹窗未创建")
            continue

        # 确保弹窗在最前（上一轮抓错的很可能就是它被别的窗口压住）
        try:
            m.lift()
            m.focus_force()
        except tk.TclError:
            pass
        settle(app, 12)

        x, y = m.winfo_rootx(), m.winfo_rooty()
        w, h = m.winfo_width(), m.winfo_height()
        p, k = grab(app, x, y, w, h, f"50_pool_modal_{tag}.png")
        print(f"[{tag}] 几何 {w}x{h}+{x}+{y}  dpi缩放={k:.2f}  → {p}")

        # 顺带把「行」和「容器」的实际底色打出来，数值 + 截图两个方向都验
        inner = None
        for c in m.content.winfo_children():
            if hasattr(c, "inner"):
                inner = c
        if inner is not None:
            print(f"        content.bg={m.content.cget('bg')}"
                  f"  ScrollFrame.bg={inner.cget('bg')}"
                  f"  canvas.bg={inner.canvas.cget('bg')}"
                  f"  inner.bg={inner.inner.cget('bg')}")
            kids = inner.inner.winfo_children()
            if kids:
                print(f"        第一行 bg={kids[0].cget('bg')}"
                      f"（行与行之间露出的就是 ScrollFrame 的底色）")
        else:
            print("        ⚠️ 弹窗里没有 ScrollFrame")

        m.close()
        settle(app, 8)
        page.destroy()
        settle(app, 6)

    app.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
