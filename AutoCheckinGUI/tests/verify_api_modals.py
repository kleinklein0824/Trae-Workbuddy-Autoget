#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""专门验证 API 服务页两个新模态框**真的能显示**（含几何与可见性）。

之前的截图脚本抓不到模态框，是因为 `place()` 在模态框打开后又对主窗
调了一次 `-topmost` / `lift`，把 `overrideredirect` 的 Toplevel 压到了后面。
本脚本不碰主窗层级，只开模态框然后：
  1) 断言它是 visible、尺寸与预期一致、位置在屏幕内
  2) 用 ImageGrab 按**模态框自己的几何**裁剪，直接抓它

跑法：python tests/verify_api_modals.py
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
PASS = 0
FAIL = 0
FAILS: list = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  OK   %s" % name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  FAIL %s   %s" % (name, extra))


def section(t):
    print("\n== %s ==" % t)


def settle(app, n=20):
    for _ in range(n):
        app.update()
        time.sleep(0.04)


def raise_modal(app, m):
    """抓图前把弹窗提到最前。

    ⚠️ `ImageGrab.grab()` 抓的是**屏幕**，不是某个窗口。弹窗被别的窗口压住时
    抓到的就是别的窗口 —— 而且**没有任何断言会失败**：Tk 层面的几何/可见性
    断言全是真的，只有证据图是错的。本项目就抓出过一张和本程序完全无关的
    PNG（`30_modal_pool_detail.png`），差点被当成"已确认"。
    """
    try:
        m.lift()
        m.focus_force()
    except tk.TclError:
        pass
    settle(app, 10)


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


class FakeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.toasts = []
        self.page = None
        self.module = "traework"
        self.view = "api-service"

    def ui_call(self, fn):
        fn()

    def toast(self, k, t, ms=3200):
        self.toasts.append((k, t))

    def accounts(self):
        return {"traework": [], "workbuddy": [], "traecn": []}

    def refresh_accounts(self, on_done=None):
        if on_done:
            on_done()

    def goto(self, m, v):
        pass


ROWS = [
    {"uid": "3956523050480058", "name": "Klein", "platform": "traecn",
     "credits": 1250.0, "credits_expire_at": None, "err_count": 0,
     "cooling": False, "cooldown_until": 0, "disabled": False,
     "cooldown_reason": "", "requests": 12, "can_forward": True,
     "state": "available"},
    {"uid": "83992452-51ec-4353-9399-98591a64f25a", "name": "KK",
     "platform": "workbuddy", "credits": None, "err_count": 0,
     "cooling": False, "cooldown_until": 0, "disabled": False,
     "cooldown_reason": "", "requests": 3, "can_forward": False,
     "state": "no_credential"},
    {"uid": "traecn-bad", "name": "Broken", "platform": "traecn",
     "credits": 0.0, "err_count": 2, "cooling": True, "cooldown_until": 0,
     "disabled": False, "cooldown_reason": "solo 1005 套餐额度受限",
     "requests": 5, "can_forward": True, "state": "cooling"},
]


def grab_rect(x, y, w, h, name):
    from PIL import ImageGrab
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    img = ImageGrab.grab()
    sw = tk._default_root.winfo_screenwidth() if tk._default_root else None
    k = img.size[0] / sw if sw else 1.0
    img.crop((int(x * k), int(y * k),
              int((x + w) * k), int((y + h) * k))).save(path)
    return path


def main():
    from app.pages.api_service import ApiServicePage
    from app.theme import Theme
    from app.ui import widgets as W

    app = FakeApp()
    svc = FakeSvc(ROWS)
    page = ApiServicePage(app, Theme(False), app, "traework")
    page._svc = svc
    page._enabled = {r["uid"] for r in ROWS}

    # 让主窗可见（模态框位置是相对主窗算的）
    app.deiconify()
    app.geometry("1180x760+120+80")
    settle(app, 12)

    # ---------- 1) 账号池明细 ----------
    section("账号池明细模态框")
    page._show_pool_detail()
    m = page._modal
    settle(app, 24)
    check("模态框已创建", m is not None)
    check("模态框可见", bool(m.winfo_ismapped()), m.winfo_ismapped())
    geo = m.winfo_geometry()
    check("尺寸为 560x420", geo.startswith("560x420"), geo)
    check("宽度匹配", m.winfo_width() == 560, m.winfo_width())
    check("高度匹配", m.winfo_height() == 420, m.winfo_height())
    # 位置应在屏幕内
    x, y = m.winfo_rootx(), m.winfo_rooty()
    check("位置在屏幕内", x >= 0 and y >= 0, (x, y))
    # 内容里有 3 个账号
    labels = _texts(m.content)
    check("列出 3 个账号",
          sum(1 for s in labels if s in ("Klein", "KK", "Broken")) == 3,
          labels)
    check("显示「可转发」", any("可转发" in s for s in labels), labels)
    check("显示「不可转发」", any("不可转发" in s for s in labels), labels)
    check("显示「冷却中」", any("冷却中" in s for s in labels), labels)
    check("显示冷却原因", any("1005" in s for s in labels), labels)
    raise_modal(app, m)
    p1 = grab_rect(x, y, 560, 420, "30_modal_pool_detail.png")
    print("     截图：%s" % p1)
    m.close()
    settle(app, 8)
    check("关闭后已销毁", not m.winfo_exists())

    # ---------- 2) 高风险确认 ----------
    section("高风险操作确认模态框")
    page._modal = None
    page._run_action("reset_device")
    m2 = page._modal
    settle(app, 24)
    check("确认框已创建", m2 is not None)
    check("确认框可见", bool(m2.winfo_ismapped()), m2.winfo_ismapped())
    geo2 = m2.winfo_geometry()
    check("尺寸为 480x250", geo2.startswith("480x250"), geo2)
    labels2 = _texts(m2.content) + _texts(m2.footer)
    check("含风险说明", any("改动本机" in s for s in labels2), labels2)
    check("含二次确认问句", any("确定" in s for s in labels2), labels2)
    check("含确认按钮", any("确认执行" in s for s in labels2), labels2)
    check("含取消按钮", any("取消" in s for s in labels2), labels2)
    x2, y2 = m2.winfo_rootx(), m2.winfo_rooty()
    raise_modal(app, m2)
    p2 = grab_rect(x2, y2, 480, 250, "31_modal_risky_confirm.png")
    print("     截图：%s" % p2)
    m2.close()
    settle(app, 8)

    # ---------- 3) 账号多到装不下时，弹窗内容必须能滚 ----------
    section("账号池明细：账号多到溢出时必须能滚")
    # 弹窗固定 560x420（可用区约 338px），每个账号占 3~4 行约 60px ——
    # 实测 **7 个账号就溢出**。Modal 自身不带滚动容器，所以这个弹窗必须
    # 自己套一个，否则多出来的账号既看不到、也滚不动（滚轮在弹窗上没落点）。
    page._modal = None
    many = []
    for i in range(12):
        r = dict(ROWS[i % len(ROWS)])
        r["uid"] = f"test-uid-{i:02d}"
        r["name"] = f"账号-{i:02d}"
        many.append(r)
    page._svc = FakeSvc(many)
    page._show_pool_detail()
    m3 = page._modal
    settle(app, 30)
    check("弹窗已创建", m3 is not None)
    if m3 is not None:
        # 判据要**行为化**：断言"存在一个真的能滚的容器"，而不是"存在一个
        # ScrollFrame" —— 后者只是个类型检查，装个不会滚的替身也能骗过去。
        cands = _find(m3, lambda w: isinstance(w, W.ScrollFrame))
        scrollable = [w for w in cands
                      if getattr(w, "canvas", None) is not None
                      and W._can_scroll_canvas(w.canvas)]
        check("弹窗里有一个真的能滚的容器（否则溢出的账号够不着）",
              bool(scrollable),
              f"ScrollFrame {len(cands)} 个，其中可滚 {len(scrollable)} 个")
        if scrollable:
            sf = scrollable[0]
            # 往容器中心发滚轮：必须滚**弹窗自己**的容器，而不是背后的页面
            cv = sf.canvas
            px = cv.winfo_rootx() + cv.winfo_width() // 2
            py = cv.winfo_rooty() + cv.winfo_height() // 2
            under = W.deepest_at(m3, px, py)
            check("指针下取到了弹窗里的控件", under is not None)
            if under is not None:
                check("路由判定：交给弹窗自己的容器",
                      W.wheel_container(under) is sf,
                      type(W.wheel_container(under)).__name__)
            before = sf.canvas.yview()[0]
            W._WHEEL["acc"] = 0.0
            (under or sf.inner).event_generate(
                "<MouseWheel>", delta=-120, x=10, y=10,
                rootx=px, rooty=py, when="now")
            settle(app, 8)
            after = sf.canvas.yview()[0]
            check("滚轮真的滚动了弹窗内容", after > before,
                  f"yview {before:.3f} -> {after:.3f}")
            # 滚到底：最后一个账号必须可达（这正是缺陷的直观表现）
            sf.canvas.yview_moveto(1.0)
            settle(app, 6)
            first, last = sf.canvas.yview()
            check("能滚到最底部（最后一个账号可达）",
                  last >= 0.999, f"yview 末端 {last:.3f}")
        last_row = _find(m3, lambda w: (w.cget("text")
                                        if _has_text(w) else "") == "账号-11")
        check("最底部那个账号（账号-11）确实存在", bool(last_row))
        m3.close()
        settle(app, 6)

    app.destroy()

    print("\n---")
    print("API 服务页模态框：%d OK / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - %s" % f)
    return 1 if FAIL else 0


def _has_text(w):
    try:
        w.cget("text")
        return True
    except Exception:  # noqa: BLE001
        return False


def _find(root, pred):
    """在控件树里找第一个满足 pred 的控件（深度优先）。"""
    out = []
    try:
        for w in root.winfo_children():
            if pred(w):
                out.append(w)
                continue
            out.extend(_find(w, pred))
    except Exception:  # noqa: BLE001
        pass
    return out


def _texts(widget):
    out = []
    try:
        for w in widget.winfo_children():
            try:
                t = w.cget("text")
                if t:
                    out.append(str(t))
            except Exception:  # noqa: BLE001
                pass
            out.extend(_texts(w))
            lbl = getattr(w, "_label", None)
            if lbl:
                out.append(str(lbl))
    except Exception:  # noqa: BLE001
        pass
    return out


if __name__ == "__main__":
    sys.exit(main())
