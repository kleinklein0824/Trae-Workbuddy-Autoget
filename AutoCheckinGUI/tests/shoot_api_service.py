#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 服务页专项截图：验证「本地模型 × 接口配置」联动。

用假 Ollama 后端（NDJSON 协议）灌入 3 个模型，让页面里
「默认模型」下拉框能同时列出云端 + 本地模型，并截两张图：
  1) 默认模型 = 云端模型（走账号池）
  2) 默认模型 = local/ollama/<model>（走本地后端）
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SHOTS = os.path.join(HERE, "_shots")

from app.services import diagnose as DG
from app.services import local_backends as LB

W, H = 1180, 760
MODELS = ["moondream:latest", "qwen2.5:1.5b", "llama3.2:3b"]


class _FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._json({"models": [{"name": m, "size": 1_000_000_000,
                                    "details": {"family": m.split(":")[0]}}
                                   for m in MODELS]})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self._json({"model": "fake", "message": {"role": "assistant",
                                                 "content": "ok"},
                    "done": True})


from app.services import diagnose as DG
from app.services import local_backends as LB


def settle(app, n=16):
    for _ in range(n):
        app.update()
        time.sleep(0.05)


def dpi_scale(app):
    from PIL import ImageGrab
    sw = app.winfo_screenwidth()
    gw = ImageGrab.grab().size[0]
    return (gw / sw) if sw else 1.0


def place(app):
    sw, sh = app.winfo_screenwidth(), app.winfo_screenheight()
    x = max(0, (sw - W) // 2)
    y = max(0, (sh - H) // 4)
    app.deiconify()
    app.geometry(f"{W}x{H}+{x}+{y}")
    app.update_idletasks()
    app.lift()
    app.attributes("-topmost", True)
    settle(app, 8)
    return x, y


def grab(app, name, origin, k=1.0):
    from PIL import ImageGrab
    x, y = origin
    settle(app, 16)
    img = ImageGrab.grab().crop(
        (int(x * k), int(y * k), int((x + W) * k), int((y + H) * k)))
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, f"{name}.png")
    img.save(path)
    return path


def main():
    fake = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
    fp = fake.server_address[1]
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    fake_url = f"http://127.0.0.1:{fp}"
    print(f"假 Ollama → {fake_url}  模型 {MODELS}")

    from app.services import api_server as AS
    svc = AS.service()
    svc.set_local("ollama", fake_url, True)
    svc.set_local("llamacpp", enabled=False)
    print("探测：", svc._local.probe("ollama")[0:2])

    from app.application import App
    app = App()
    app.update()
    origin = place(app)
    k = dpi_scale(app)
    print(f"dpi scale = {k:.4f}")

    for _ in range(80):
        app.update()
        time.sleep(0.1)
        if app.accounts() is not None:
            break

    # 页面里的探测是异步的，进页面后等它完成
    app.goto("traework", "api-service")
    place(app)
    settle(app, 40)

    # 1) 云端默认
    page = app.page
    page._model_var.set("glm-5.2")
    page._svc.configure(model="glm-5.2", persist=False)
    page._rebuild_local_list()
    page._render_status(page._svc.status())
    p1 = grab(app, "20_api_cloud_default", origin, k)
    print(f"ok {p1}   候选数={len(page._svc.model_choices())}")

    # 2) 本地默认
    page._set_default_model("local/ollama/qwen2.5:1.5b")
    place(app)
    p2 = grab(app, "21_api_local_default", origin, k)
    print(f"ok {p2}   当前默认={page._model_var.get()}")

    # 3) 云端转发卡 + AI 诊断卡（本次新增）
    page._paint_cloud(page._svc.status())
    page._cloud_write("== 云端转发 ==\n可转发账号取决于本机 Trae CN 登录态\n")
    diag_res = {
        "ok": True,
        "summary": "登录令牌已过期，需要重新登录 Trae 客户端后再重试签到。",
        "causes": ["Trae CN 客户端未登录或登录已过期",
                   "本地缓存的令牌比真实登录态旧"],
        "suggestions": ["点「打开 Trae 客户端」确认登录状态",
                        "登录成功后点「重试签到」复跑一次"],
        "actions": [dict(key="open_client", **DG.ACTIONS["open_client"]),
                    dict(key="retry_checkin", **DG.ACTIONS["retry_checkin"]),
                    dict(key="rescan_tokens", **DG.ACTIONS["rescan_tokens"]),
                    dict(key="reset_device", **DG.ACTIONS["reset_device"])],
        "ai": {"ok": True, "route": "local", "target": "Ollama · qwen2.5:1.5b",
               "describe": "本地模型 · Ollama · qwen2.5:1.5b · 1.4s"},
        "ai_used": True,
    }
    page._diag_stamp = time.strftime("%H:%M:%S")
    page._render_diag(diag_res)
    page._refresh_ai_status()
    # 新卡片在页面下半部分，先把滚动区拉到底再截图
    try:
        page.scroll.canvas.yview_moveto(1.0)
    except Exception:  # noqa: BLE001
        pass
    place(app)
    p3 = grab(app, "22_api_cloud_and_diag", origin, k)
    print(f"ok {p3}   云端卡 + AI 诊断卡")

    # 4) 诊断卡下部的「可一键执行」动作按钮
    try:
        page.scroll.canvas.yview_moveto(1.0)
    except Exception:  # noqa: BLE001
        pass
    place(app)
    settle(app, 20)
    p4 = grab(app, "23_api_diag_actions", origin, k)
    print(f"ok {p4}   诊断动作按钮")

    # 5) 账号池明细模态框（先定位主窗，再开模态框，避免被 lift 到后面）
    p5 = None
    page._show_pool_detail()
    settle(app, 24)
    try:
        if page._modal is not None:
            page._modal.lift()
            page._modal.focus_force()
    except Exception:  # noqa: BLE001
        pass
    p5 = grab(app, "24_api_pool_detail", origin, k)
    print(f"ok {p5}   账号池明细")
    try:
        if page._modal is not None:
            page._modal.close()
    except Exception:  # noqa: BLE001
        pass

    app.destroy()
    fake.shutdown()
    # 本次跑测改了本地后端与默认模型 —— 复位为出厂默认，避免污染用户配置
    svc.set_local("ollama", base_url=LB.DEFAULT_URLS["ollama"],
                  enabled=False, persist=True)
    svc.set_local("llamacpp", base_url=LB.DEFAULT_URLS["llamacpp"],
                  enabled=False, persist=True)
    svc.configure(model="glm-5.2", port=7864, persist=True)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
