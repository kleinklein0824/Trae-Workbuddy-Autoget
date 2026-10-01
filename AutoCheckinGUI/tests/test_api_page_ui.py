# -*- coding: utf-8 -*-
"""API 服务页新增 UI 的离线测试。

覆盖本次「加强 API 服务板块」新增的四块能力，全部不联网：

  1. 账号池状态映射（_pool_state）—— 新旧两种 row schema 都要正确
  2. 云端转发卡渲染（_paint_cloud）—— 就绪/无账号/不可转发三种分支
  3. AI 诊断卡渲染（_render_diag）—— 空态 / 加载 / 有结论+动作
  4. 诊断动作分发（_dispatch_action）—— 白名单动作映射到项目既有能力，
     高风险动作必须走模态确认，绝不静默执行

跑法：python tests/test_api_page_ui.py
"""
from __future__ import annotations

import json
import os
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.services import diagnose as DG          # noqa: E402
from app.theme import Theme                      # noqa: E402

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


# --------------------------------------------------------------------------
# 轻量替身：只实现页面真正用到的那几个接口
# --------------------------------------------------------------------------

class FakeLocalPool:
    def __init__(self, rows=None, models=None):
        self._rows = rows or {}
        self._models = models or []

    def get_row(self, kind):
        return self._rows.get(kind)

    def enabled_kinds(self):
        return [k for k, r in self._rows.items() if r.get("enabled")]

    def probe(self, kind):
        return False, "未探测", []

    def status(self):
        return list(self._rows.values())

    def model_ids(self):
        return list(self._models)

    def count_call(self, kind):
        pass


class FakeSvc:
    """ApiService 的最小替身，只暴露 API 服务页用到的那部分。"""

    def __init__(self, pool_rows=None, forwardable=0, running=False,
                 prefer_local=True):
        self._status = {
            "running": running, "port": 7864, "total_requests": 3,
            "active_uid": None, "pool_count": len(pool_rows or []),
            "pool_forwardable": forwardable,
            "cloud_ready": forwardable > 0,
            "last_error": None,
            "pool": list(pool_rows or []),
            "local": [], "local_models": [],
        }
        self.local = FakeLocalPool()
        self._prefer_local = prefer_local
        self.model = "glm-5.2"
        self.port = 7864
        self.api_key = ""
        self.running = running
        self.actions_run = []

    # --- 页面读的配置 ---
    @property
    def prefer_local(self):
        return self._prefer_local

    def pool_uids(self):
        return [r["uid"] for r in self._status["pool"]]

    def status(self):
        return dict(self._status)

    def ai_status(self):
        return {"local_ready": False, "local_kind": None,
                "local_detail": "没有启用任何本地后端",
                "prefer_local": self._prefer_local, "local_model": "",
                "cloud_available": True,
                "stats": {"local": 0, "cloud": 0, "failed": 0, "fallback": 0}}

    def set_ai_prefs(self, prefer_local=None, local_model=None, persist=True):
        if prefer_local is not None:
            self._prefer_local = bool(prefer_local)
        return {"prefer_local": self._prefer_local, "ai_local_model": ""}

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
        if "model" in kw:
            self.model = kw["model"]
        return {}

    def stop(self):
        self.running = False
        return self.status()

    def clear_cooldowns(self):
        return 0

    def reload_accounts(self):
        return []


class FakeApp(tk.Tk):
    """App 的最小替身：提供 ui_call / toast / accounts / refresh_accounts。

    继承 tk.Tk 是因为 `W.Modal(master, ...)` 需要一个真实窗口做父级。
    """

    def __init__(self):
        super().__init__()
        self.withdraw()
        self.toasts = []
        self.refreshed = 0
        self.page = None
        self.module = "traework"
        self.view = "api-service"
        self.goto_calls = []

    def ui_call(self, fn):
        fn()

    def toast(self, kind, text, ms=3200):
        self.toasts.append((kind, text))

    def accounts(self):
        return {"traework": [], "workbuddy": [], "traecn": []}

    def refresh_accounts(self, on_done=None):
        self.refreshed += 1
        if on_done:
            on_done()

    def goto(self, module, view):
        self.goto_calls.append((module, view))


# --------------------------------------------------------------------------
# 1. 账号池状态映射
# --------------------------------------------------------------------------

def test_pool_state():
    section("账号池状态映射（_pool_state）")
    from app.pages.api_service import ApiServicePage as P
    from app.pages.api_service import POOL_STATE_CN

    cases = [
        ({"state": "available"}, ("就绪", "green")),
        ({"state": "cooling"}, ("冷却中", "amber")),
        ({"state": "no_credential"}, ("无登录态", "slate")),
        ({"state": "expired"}, ("积分过期", "amber")),
        ({"state": "no_credits"}, ("零积分", "amber")),
        ({"state": "disabled"}, ("已禁用", "red")),
    ]
    for row, want in cases:
        got = P._pool_state(row)
        check("新 schema %s" % row["state"], got == want, "%r != %r" % (got, want))

    # 老 schema 兼容（只有 disabled/cooling 布尔）
    check("老 schema disabled",
          P._pool_state({"disabled": True}) == ("已禁用", "red"))
    check("老 schema cooling",
          P._pool_state({"cooling": True}) == ("冷却中", "amber"))
    check("老 schema 就绪",
          P._pool_state({}) == ("就绪", "green"))

    # 未知状态不炸，原样回退
    got = P._pool_state({"state": "weird"})
    check("未知状态安全回退", got == ("weird", "slate"), got)

    check("状态表覆盖全部 6 种", len(POOL_STATE_CN) == 6, len(POOL_STATE_CN))


# --------------------------------------------------------------------------
# 2. 云端转发卡
# --------------------------------------------------------------------------

def _new_page(svc, app=None):
    app = app or FakeApp()
    theme = Theme(False)
    from app.pages.api_service import ApiServicePage
    # 构造真实页面：BasePage 需要 (master, theme, app, module)
    page = ApiServicePage(app, theme, app, "traework")
    # 页面自己的 _svc 是全局单例，这里替换成替身
    page._svc = svc
    page._enabled = set(svc.pool_uids())
    return app, page, app


def test_cloud_card():
    section("云端转发卡渲染（_paint_cloud）")

    # 分支 a：可转发
    rows = [{"uid": "u1", "name": "Klein", "platform": "traecn",
             "credits": 120.0, "credits_expire_at": None, "err_count": 0,
             "cooling": False, "cooldown_until": 0, "disabled": False,
             "cooldown_reason": "", "requests": 1, "can_forward": True,
             "state": "available"}]
    svc = FakeSvc(pool_rows=rows, forwardable=1)
    root, page, app = _new_page(svc)
    try:
        page._paint_cloud(svc.status())
        page.update_idletasks()
        check("就绪分支不抛异常", page._cloud_badge._label == "就绪",
              page._cloud_badge._label)
        info = page._cloud_info.cget("text")
        check("明细含可转发数", "1 / 1" in info, info)
    finally:
        root.destroy()

    # 分支 b：无账号
    svc = FakeSvc(pool_rows=[], forwardable=0)
    root, page, app = _new_page(svc)
    try:
        page._paint_cloud(svc.status())
        check("无账号分支", page._cloud_badge._label == "无账号",
              page._cloud_badge._label)
    finally:
        root.destroy()

    # 分支 c：有账号但不可转发
    rows = [{"uid": "u2", "name": "TW", "platform": "traework",
             "credits": None, "err_count": 0, "cooling": False,
             "disabled": False, "cooldown_reason": "", "requests": 0,
             "can_forward": False, "state": "no_credential"}]
    svc = FakeSvc(pool_rows=rows, forwardable=0)
    root, page, app = _new_page(svc)
    try:
        page._paint_cloud(svc.status())
        check("不可转发分支", page._cloud_badge._label == "不可转发",
              page._cloud_badge._label)
        check("提示需要 Trae CN 登录态",
              "Trae CN" in page._cloud_info.cget("text"),
              page._cloud_info.cget("text"))
    finally:
        root.destroy()


# --------------------------------------------------------------------------
# 3. AI 诊断卡渲染
# --------------------------------------------------------------------------

def test_diag_render():
    section("AI 诊断卡渲染（_render_diag）")

    svc = FakeSvc()
    root, page, app = _new_page(svc)
    try:
        # 空态
        page._render_diag(None)
        page.update_idletasks()
        check("空态渲染不抛异常", page._diag_holder.winfo_exists() == 1)

        # 加载态
        page._render_diag({"loading": True})
        page.update_idletasks()
        check("加载态渲染", page._diag_holder.winfo_exists() == 1)

        # 有完整结果（AI 走本地）
        res = {
            "ok": True,
            "summary": "登录令牌已过期，需要重新登录 Trae 客户端。",
            "causes": ["令牌过期", "账号在别处退出登录"],
            "suggestions": ["打开 Trae 客户端确认登录", "重试签到"],
            "actions": [dict(key="open_client", **DG.ACTIONS["open_client"]),
                        dict(key="retry_checkin",
                             **DG.ACTIONS["retry_checkin"]),
                        dict(key="reset_device", **DG.ACTIONS["reset_device"])],
            "ai": {"ok": True, "route": "local",
                   "target": "Ollama · qwen2.5:7b",
                   "describe": "本地模型 · Ollama · qwen2.5:7b · 1.2s"},
            "ai_used": True,
        }
        page._render_diag(res)
        page.update_idletasks()
        labels = _all_text(page._diag_holder)
        check("结论已渲染", any("登录令牌已过期" in s for s in labels),
              labels[:6])
        check("原因已渲染", any("令牌过期" in s for s in labels), labels)
        check("动作按钮已渲染",
              any("打开 Trae 客户端" in s for s in labels), labels)
        check("高风险动作带⚠标记",
              any(s.startswith("⚠") for s in labels), labels)
        check("走向标记为本地模型",
              any("本地模型" in s for s in labels), labels)

        # AI 失败（纯规则）
        res2 = dict(res, ai={"ok": False, "error": "本地超时"})
        page._render_diag(res2)
        page.update_idletasks()
        labels = _all_text(page._diag_holder)
        check("AI 失败时显示规则诊断",
              any("规则诊断" in s for s in labels), labels[:6])
    finally:
        root.destroy()


def _all_text(widget):
    out = []
    try:
        for w in widget.winfo_children():
            try:
                txt = w.cget("text")
                if txt:
                    out.append(str(txt))
            except Exception:  # noqa: BLE001
                pass
            out.extend(_all_text(w))
            # Badge 是 Canvas，文字存在 _label 上
            lbl = getattr(w, "_label", None)
            if lbl:
                out.append(str(lbl))
    except Exception:  # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------------
# 4. 动作分发
# --------------------------------------------------------------------------

def test_action_dispatch():
    section("诊断动作分发（_dispatch_action）")

    svc = FakeSvc()
    root, page, app = _new_page(svc)
    try:
        # 安全动作：重扫令牌
        page._dispatch_action("rescan_tokens")
        check("rescan_tokens 不抛异常", True)

        # 安全动作：重新发现账号 → 触发 app.refresh_accounts
        n0 = app.refreshed
        page._dispatch_action("rescan_accounts")
        check("rescan_accounts 触发刷新", app.refreshed > n0, app.refreshed)

        # 安全动作：打开日志（只调 open_path，不真开窗）
        page._dispatch_action("open_logs")
        check("open_logs 不抛异常", True)

        # 安全动作：网络检测（真实读 socket，但只测 127.0.0.1:1）
        msg = page._check_network()
        check("check_network 返回描述", isinstance(msg, str) and msg, msg)

        # 安全动作：清除今日状态 —— 必须跑在临时根目录，绝不碰真实 logs/
        import tempfile
        from app import config as CFG
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "logs"))
            with open(os.path.join(td, "logs", "state.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"traecn:x": "2026-01-01"}, f)
            old_root = CFG.ROOT
            CFG.ROOT = td
            try:
                page._dispatch_action("clear_state")
            finally:
                CFG.ROOT = old_root
        check("clear_state 不抛异常", True)

        # 安全动作：启用本地模型 → 触发探测
        page._dispatch_action("enable_local_model")
        check("enable_local_model 不抛异常", True)

        # 未知动作不炸
        msg = page._dispatch_action("no_such_action")
        check("未知动作安全返回", "未知动作" in msg, msg)
    finally:
        root.destroy()


def test_risky_requires_modal():
    section("高风险动作必须二次确认")

    svc = FakeSvc()
    root, page, app = _new_page(svc)
    try:
        # 高风险动作：reset_device / clear_state
        for key in ("reset_device", "clear_state"):
            check("%s 标记为 risky" % key, DG.is_risky(key) is True)

        # _run_action 对高风险动作应弹出 Modal，而不是直接执行
        calls = []
        page._do_action = lambda k: calls.append(k)
        page._run_action("reset_device")
        check("高风险动作未直接执行", calls == [], calls)
        check("弹出了确认模态框", page._modal is not None)
        # 关闭它（否则 audit / 后续会残留 grab）
        if page._modal is not None:
            try:
                page._modal.close()
            except Exception:  # noqa: BLE001
                pass

        # 安全动作应直接执行（无需确认）
        page._run_action("open_logs")
        check("安全动作直接执行", calls == ["open_logs"], calls)
    finally:
        root.destroy()


def test_pool_detail_modal():
    section("账号池明细模态框")

    rows = [{"uid": "u1", "name": "Klein", "platform": "traecn",
             "credits": 120.0, "credits_expire_at": None, "err_count": 0,
             "cooling": False, "cooldown_until": 0, "disabled": False,
             "cooldown_reason": "", "requests": 1, "can_forward": True,
             "state": "available"},
            {"uid": "u2", "name": "TW", "platform": "traework",
             "credits": None, "err_count": 0, "cooling": False,
             "disabled": False, "cooldown_reason": "", "requests": 0,
             "can_forward": False, "state": "no_credential"}]
    svc = FakeSvc(pool_rows=rows, forwardable=1)
    root, page, app = _new_page(svc)
    try:
        page._show_pool_detail()
        check("弹出了明细模态框", page._modal is not None)
        if page._modal is not None:
            labels = _all_text(page._modal.content)
            check("含账号名", any("Klein" in s for s in labels), labels)
            check("含「可转发」标记", any("可转发" in s for s in labels), labels)
            check("含「不可转发」标记", any("不可转发" in s for s in labels), labels)
            check("含状态中文", any("就绪" in s for s in labels), labels)
            check("含积分", any("120" in s for s in labels), labels)
            page._modal.close()
    finally:
        root.destroy()


def test_prefer_local_switch():
    section("本地优先开关")

    svc = FakeSvc(prefer_local=True)
    root, page, app = _new_page(svc)
    try:
        page._toggle_prefer_local(False)
        check("关闭本地优先已落盘", svc.prefer_local is False, svc.prefer_local)
        page._toggle_prefer_local(True)
        check("开启本地优先已落盘", svc.prefer_local is True, svc.prefer_local)
        check("提示文案已给出", bool(app.toasts), app.toasts)
    finally:
        root.destroy()


def main():
    test_pool_state()
    test_cloud_card()
    test_diag_render()
    test_action_dispatch()
    test_risky_requires_modal()
    test_pool_detail_modal()
    test_prefer_local_switch()

    print("\n---")
    print("API 服务页 UI：%d OK / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
