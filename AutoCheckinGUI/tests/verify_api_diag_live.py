#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 服务页「AI 诊断」真实链路验证（离线）。

用假 Ollama 后端 + 项目真实的 diagnose/dispatcher，走一遍页面里
「点诊断 → 页面渲染结论/原因/动作」的完整链路，证明：

  1. 本地模型就绪时，诊断**真的**走本地（route == local）
  2. 页面的 _collect_checkin_evidence 能从 history.jsonl 拿证据
  3. 页面的 _diag_done → _render_diag 能把结果显示成结论+动作按钮
  4. 不消耗任何云端积分（mock 掉云端，若被调用会断言失败）

跑法：python tests/verify_api_diag_live.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

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
# 假 Ollama：/api/tags 给模型列表，/api/chat 返回一段诊断文本
# --------------------------------------------------------------------------

_AI_REPLY = """结论：Trae CN 登录令牌已过期，服务端拒绝了签到请求。
可能原因：
1. Trae CN 客户端长时间未打开，令牌已失效
2. 本地缓存的令牌比真实登录态旧
建议动作：
1. 打开 Trae CN 客户端并重新登录
2. 登录成功后点「重试签到」复跑一次
"""


class FakeOllama:
    def __init__(self, models=("qwen2.5:7b",)):
        self.models = list(models)
        self.chats = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/api/tags"):
                    b = json.dumps({"models": [{"name": m}
                                               for m in srv.models]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                try:
                    srv.chats.append(json.loads(raw))
                except ValueError:
                    srv.chats.append({})
                b = json.dumps({
                    "model": "fake",
                    "message": {"role": "assistant", "content": _AI_REPLY},
                    "done": True,
                    "prompt_eval_count": 11,
                    "eval_count": 42,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


def main():
    fake = FakeOllama()
    print("假 Ollama → %s" % fake.url)

    # --- 把本地后端指到假 Ollama 上（走项目真实配置路径） ---
    from app.services import api_server as AS
    svc = AS.service()
    svc.set_local("ollama", fake.url, True)
    svc.set_local("llamacpp", enabled=False)
    svc.set_ai_prefs(prefer_local=True, persist=False)

    from app.services import ai_dispatch as AD
    from app.services import local_backends as LB
    d = AD.dispatcher()
    d.set_local_pool(svc.local)
    d.set_settings_getter(lambda: {"prefer_local": True, "local_model": ""})
    d.invalidate_probe()

    # 云端调用换成一个「一旦被调用就报错」的桩，确保本地优先真的不烧积分
    cloud_calls = []

    def boom(*a, **kw):
        cloud_calls.append(1)
        raise AssertionError("本地就绪时不应调用云端")

    d.set_cloud_fn(boom)

    kind, detail = d.local_ready(fresh=True)
    section("本地后端就绪")
    check("本地已就绪", kind == "ollama", "%r %r" % (kind, detail))
    # local_ready 返回的是「说明」而不是模型名，模型名从池里取
    row = svc.local.get_row("ollama") or {}
    check("探测到模型", row.get("models") == ["qwen2.5:7b"],
          row.get("models"))

    # --- 造一份「签到失败」的证据，落进**临时根目录**（不碰真实数据）---
    section("证据收集（从 history.jsonl）")
    import tempfile
    from app import config as CFG
    _real_root = CFG.ROOT
    _td = tempfile.mkdtemp(prefix="apidiag_")
    CFG.ROOT = _td
    os.makedirs(os.path.join(_td, "logs"), exist_ok=True)
    hist = os.path.join(_td, "logs", "history.jsonl")
    fails = [
        {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "platform": "traecn",
         "account": "Klein", "status": "fail",
         "detail": "HTTP 401 unauthorized：登录令牌已过期"},
        {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "platform": "traework",
         "account": "trae1", "status": "ok", "detail": "已领取"},
    ]
    with open(hist, "w", encoding="utf-8") as f:
        for r in fails:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    try:
        # --- 建页面，从落盘历史拿证据 ---
        import tkinter as tk
        from app.pages.api_service import ApiServicePage
        from app.theme import Theme

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

        app = FakeApp()
        page = ApiServicePage(app, Theme(False), app, "traework")
        page._svc = svc
        page._enabled = set(svc.pool_uids())

        results, logs = page._collect_checkin_evidence()
        check("从 history.jsonl 拿到证据", len(results) >= 2, len(results))
        check("含失败记录",
              any((r.get("status") == "fail") for r in results), results)

        # --- 跑真实诊断（页面同款调用） ---
        section("真实诊断链路（本地优先）")
        from app.services import diagnose as DG
        out = DG.diagnose(results=results, logs=logs, root=str(ROOT),
                          use_ai=True, timeout=60.0)
        ai = out.get("ai") or {}
        check("AI 已使用", out.get("ai_used") is True, out.get("ai_error"))
        check("走向为本地", ai.get("route") == "local", ai)
        check("未触碰云端", cloud_calls == [], cloud_calls)
        check("结论非空", bool(out.get("summary")), out.get("summary"))
        check("解析出建议动作", len(out.get("suggestions") or []) >= 1,
              out.get("suggestions"))
        check("给出可执行动作", len(out.get("actions") or []) >= 1,
              [a.get("key") for a in out.get("actions") or []])
        print("     结论：%s" % out.get("summary"))
        print("     动作：%s" % [a.get("key") for a in out.get("actions") or []])

        # --- 页面渲染 ---
        section("页面渲染诊断结果")
        page._diag_done(out)
        labels = _all_text(page._diag_holder)
        check("结论已上屏", any("令牌已过期" in s or "结论" in s
                                for s in labels), labels[:8])
        check("标记为本地模型", any("本地模型" in s for s in labels), labels)
        check("动作按钮已上屏",
              any("打开 Trae" in s or "重试" in s for s in labels), labels)

        # 假 Ollama 确实收到了请求
        check("假 Ollama 收到 chat 请求", len(fake.chats) >= 1, len(fake.chats))
        if fake.chats:
            msgs = fake.chats[0].get("messages") or []
            check("请求含 system 提示",
                  any(m.get("role") == "system" for m in msgs), msgs)
            check("请求含用户证据",
                  any("401" in (m.get("content") or "") for m in msgs), msgs)

        app.destroy()
    finally:
        # 恢复临时根目录
        CFG.ROOT = _real_root
        try:
            import shutil
            shutil.rmtree(_td, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        # 本地后端复位（恢复出厂：不启用 + 默认地址）
        svc.set_local("ollama", base_url=LB.DEFAULT_URLS["ollama"],
                      enabled=False, persist=True)
        svc.set_local("llamacpp", base_url=LB.DEFAULT_URLS["llamacpp"],
                      enabled=False, persist=True)
        svc.set_ai_prefs(prefer_local=True, persist=True)
        fake.stop()

    print("\n---")
    print("API 诊断真实链路：%d OK / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - %s" % f)
    return 1 if FAIL else 0


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
            lbl = getattr(w, "_label", None)
            if lbl:
                out.append(str(lbl))
    except Exception:  # noqa: BLE001
        pass
    return out


if __name__ == "__main__":
    sys.exit(main())
