#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验证：真实 HTTP API 服务 + 假 Ollama 后端。

模拟的是用户实际使用路径：
  1) 起一个假 Ollama（NDJSON 协议，端口随机）
  2) 起真实 ApiService，把 local/ollama 指向假后端
  3) 用 requests 走真实 HTTP：
       GET  /health              -> local 数组里有 ollama 且 ok=True
       GET  /v1/models           -> 含 local/ollama/<model>
       POST /v1/chat/completions (非流式, model=local/ollama/x)
       POST /v1/chat/completions (stream=True) 逐块收到 SSE
       POST /v1/chat/completions (model=某云端模型) -> 仍走账号池分支
  4) 断言全部通过后关服务

为什么必须真起服务：`_resolve_local` / 路由分支 / SSE 头 / 分块逻辑
都只在真实 handler 里跑，直接调 backend 类是测不到的。
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GUI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

from app.services import api_server as AS          # noqa: E402
from app.services import local_backends as LB      # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(f"{'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra else ""))


# ---------------------------------------------------------------- 假 Ollama
class _FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200, ctype="application/json"):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._json({"models": [
                {"name": "moondream:latest", "size": 1737_000_000,
                 "details": {"family": "moondream"}},
                {"name": "qwen2.5:1.5b", "size": 986_000_000,
                 "details": {"family": "qwen2"}},
            ]})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if not self.path.startswith("/api/chat"):
            self.send_response(404)
            self.end_headers()
            return
        msgs = body.get("messages") or [{}]
        hint = msgs[-1].get("content", "")
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for piece in ("本地", "模型", "已", "收到"):
                line = json.dumps({"model": "fake", "message": {
                    "role": "assistant", "content": piece}, "done": False},
                    ensure_ascii=False)
                self.wfile.write((line + "\n").encode("utf-8"))
                self.wfile.flush()
            tail = json.dumps({"model": "fake", "message": {
                "role": "assistant", "content": ""}, "done": True,
                "prompt_eval_count": 7, "eval_count": 4}, ensure_ascii=False)
            self.wfile.write((tail + "\n").encode("utf-8"))
            self.wfile.flush()
        else:
            self._json({
                "model": "fake",
                "message": {"role": "assistant",
                            "content": f"[本地] {hint}"},
                "done": True, "prompt_eval_count": 7, "eval_count": 4,
            })


def main():
    fake = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
    fake_port = fake.server_address[1]
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    fake_url = f"http://127.0.0.1:{fake_port}"
    print(f"假 Ollama 监听 {fake_url}")

    svc = AS.ApiService()
    svc.set_local("ollama", fake_url, True)

    # probe 是同步的，直接调
    ok, detail, models = svc._local.probe("ollama")
    check("探测假 Ollama 成功", ok, detail)
    check("探测返回 2 个模型", len(models or []) == 2, f"models={models}")
    check("探测模型名为字符串列表",
          all(isinstance(m, str) for m in (models or [])), str(models))

    svc.start()
    time.sleep(0.6)
    port = svc.status()["port"]
    base = f"http://127.0.0.1:{port}"
    print(f"API 服务监听 {base}")

    import urllib.request as u
    opener = u.build_opener(u.ProxyHandler({}))

    def get(path):
        with opener.open(base + path, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def post(payload, timeout=30):
        req = u.Request(base + "/v1/chat/completions",
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    try:
        # --- /health
        st, health = get("/health")
        check("/health 200", st == 200)
        kinds = {row["kind"]: row for row in health.get("local", [])}
        check("/health 含 ollama 行", "ollama" in kinds)
        check("/health ollama ok=True", kinds.get("ollama", {}).get("ok") is True)
        check("/health 报出模型数 2",
              kinds.get("ollama", {}).get("models") == 2,
              str(kinds.get("ollama", {}).get("models")))

        # --- /v1/models
        st, models = get("/v1/models")
        ids = [m["id"] for m in models.get("data", [])]
        check("/v1/models 200", st == 200)
        check("/v1/models 含 local/ollama/moondream:latest",
              "local/ollama/moondream:latest" in ids, str(ids[:6]))
        check("本地模型 owned_by=ollama",
              next((m.get("owned_by") for m in models["data"]
                    if m["id"] == "local/ollama/moondream:latest"), "") == "ollama")

        # --- 非流式
        st, r1 = post({"model": "local/ollama/moondream:latest",
                       "messages": [{"role": "user", "content": "你好"}]})
        text = r1["choices"][0]["message"]["content"]
        check("本地非流式 200", st == 200)
        check("本地非流式内容正确", text == "[本地] 你好", text)
        check("本地非流式 usage 映射",
              r1.get("usage", {}).get("prompt_tokens") == 7
              and r1["usage"]["completion_tokens"] == 4,
              str(r1.get("usage")))
        check("本地非流式 object 字段",
              r1.get("object") == "chat.completion", r1.get("object"))

        # --- 流式
        req = u.Request(base + "/v1/chat/completions",
                        data=json.dumps({
                            "model": "local/ollama/qwen2.5:1.5b",
                            "messages": [{"role": "user", "content": "hi"}],
                            "stream": True}).encode("utf-8"),
                        headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=30) as r:
            ctype = r.headers.get("Content-Type", "")
            raw = r.read().decode("utf-8")
        check("流式 Content-Type 是 SSE", "text/event-stream" in ctype, ctype)
        check("流式以 [DONE] 收尾", raw.rstrip().endswith("data: [DONE]"),
              raw[-30:].replace("\n", "\\n"))
        chunks = [json.loads(l[6:]) for l in raw.splitlines()
                  if l.startswith("data: ") and l[6:] != "[DONE]"]
        joined = "".join(c["choices"][0].get("delta", {}).get("content", "")
                         for c in chunks if c.get("choices"))
        check("流式拼接内容正确", joined == "本地模型已收到", joined)
        check("流式 chunk object 正确",
              all(c.get("object") == "chat.completion.chunk"
                  for c in chunks if c.get("choices")),
              "")
        usage_chunk = [c for c in chunks if c.get("usage")]
        check("流式末块带 usage",
              bool(usage_chunk) and usage_chunk[0]["usage"]["prompt_tokens"] == 7,
              str(usage_chunk[0]["usage"]) if usage_chunk else "无")
        check("流式有 finish_reason=stop",
              any(c["choices"][0].get("finish_reason") == "stop"
                  for c in chunks
                  if c.get("choices") and not c["choices"][0].get("delta",
                                                                   {}).get("content")),
              "")

        # --- 云端模型仍走账号池分支
        # 上游转发已真实接入（solo_upstream.llm_utils_chat）。关键是要确认它
        # **没有**被误判成 local/ 前缀（否则会报 local_backend_error）。
        # 两种正确结果都接受：
        #   * HTTP 200 + chat.completion  -> 本机有可转发的 TraeCN 账号，真调通了
        #   * 503 no_healthy_account      -> 没有可转发账号，如实回报
        try:
            st, r2 = post({"model": "claude-sonnet-4",
                           "messages": [{"role": "user", "content": "hi"}]})
            etype = r2.get("error", {}).get("type", "")
            ecode = r2.get("error", {}).get("code", "")
            pooled = (st == 200 and r2.get("object") == "chat.completion") or (
                etype == "api_error" and ecode == "no_healthy_account")
            detail = f"HTTP {st} object={r2.get('object')} "
            detail += f"type={etype} code={ecode}"
        except u.HTTPError as e:
            err = json.loads(e.read().decode("utf-8") or "{}")
            etype = err.get("error", {}).get("type", "")
            ecode = err.get("error", {}).get("code", "")
            pooled = etype == "api_error" and ecode == "no_healthy_account"
            detail = f"HTTP {e.code} type={etype} code={ecode}"
        check("非 local/ 前缀仍路由到账号池分支（不再报 not_implemented）",
              pooled, detail)

        # --- 显式禁用后的可读报错
        svc.set_local("ollama", fake_url, False)
        try:
            st, r3 = post({"model": "local/ollama/moondream:latest",
                           "messages": [{"role": "user", "content": "x"}]})
            err = r3
            code = st
        except u.HTTPError as e:
            code = e.code
            err = json.loads(e.read().decode("utf-8") or "{}")
        msg = err.get("error", {}).get("message", "")
        check("禁用后返回错误", code >= 400, f"HTTP {code} {msg[:110]}")
        check("错误类型是 local_backend_error",
              err.get("error", {}).get("type") == "local_backend_error",
              err.get("error", {}).get("type", ""))
        check("错误信息可读且提到 Ollama 或未启用",
              ("未启用" in msg or "Ollama" in msg or "本地" in msg), msg[:110])

        # --- 接口配置 × 本地模型：把本地模型设为「默认模型」
        # 这是本次改动的核心：默认模型下拉框里既有云端模型，也有本地模型；
        # 选定 local/<kind>/<model> 后，请求体不带 model 时应自动走本地后端。
        svc.set_local("ollama", fake_url, True)
        svc._local.probe("ollama")

        choices = svc.model_choices()
        values = [v for v, _l, _g in choices]
        check("默认模型候选含全部云端模型",
              all(m in values for m in AS.MODEL_OPTIONS),
              f"{len(AS.MODEL_OPTIONS)} 个云端模型")
        check("默认模型候选含 local/ollama/<model>",
              "local/ollama/moondream:latest" in values, str(values[-4:]))
        groups = {g for _v, _l, g in choices}
        check("候选分组含 cloud 与 local",
              {"cloud", "local"} <= groups, str(groups))
        label = next((l for v, l, _g in choices
                      if v == "local/ollama/moondream:latest"), "")
        check("本地模型标签含「本地 ·」前缀", label.startswith("本地 ·"), label)
        check("本地模型标签含后端名 Ollama", "Ollama" in label, label)
        check("is_local_model 识别 local/ 前缀",
              svc.is_local_model("local/ollama/x") is True)
        check("is_local_model 不误判云端模型",
              svc.is_local_model("claude-sonnet-4") is False)

        # 把默认模型切成本地模型
        svc.configure(model="local/ollama/moondream:latest")
        check("configure 后 svc.model 是本地模型",
              svc.model == "local/ollama/moondream:latest", svc.model)
        check("configure 后服务端 default_model 同步",
              svc._srv.default_model == "local/ollama/moondream:latest",
              svc._srv.default_model)

        # 关键断言：请求体不带 "model"，应回落到默认模型 → 走本地后端
        st, r5 = post({"messages": [{"role": "user", "content": "缺省模型"}]})
        txt5 = r5["choices"][0]["message"]["content"]
        check("省略 model 时用默认模型（本地）", st == 200
              and txt5 == "[本地] 缺省模型", f"HTTP {st} {txt5}")
        check("省略 model 时返回的 model 字段是本地名",
              r5.get("model") == "local/ollama/moondream:latest",
              str(r5.get("model")))

        # 换回云端默认：同样省略 model 时应回到账号池分支
        # （真调通 -> 200 chat.completion；无可转发账号 -> 503 no_healthy_account）
        svc.configure(model="glm-5.2")
        try:
            st, r6 = post({"messages": [{"role": "user", "content": "hi"}]})
            etype = r6.get("error", {}).get("type", "")
            ecode = r6.get("error", {}).get("code", "")
            back_to_pool = (st == 200
                            and r6.get("object") == "chat.completion") or (
                etype == "api_error" and ecode == "no_healthy_account")
            d6 = f"HTTP {st} object={r6.get('object')} type={etype} code={ecode}"
        except u.HTTPError as e:
            err6 = json.loads(e.read().decode("utf-8") or "{}")
            etype = err6.get("error", {}).get("type", "")
            ecode = err6.get("error", {}).get("code", "")
            back_to_pool = etype == "api_error" and ecode == "no_healthy_account"
            d6 = f"HTTP {e.code} type={etype} code={ecode}"
        check("默认模型切回云端后省略 model 仍走账号池", back_to_pool, d6)

        # --- 配置持久化：default_model 要写进 gui_settings.json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            orig_path_fn = AS.ApiService.__dict__["_settings_path"]
            AS.ApiService._settings_path = staticmethod(
                lambda: os.path.join(td, "gui_settings.json"))
            try:
                svc.configure(model="local/ollama/qwen2.5:1.5b")
                svc.save_local_settings()
                with open(os.path.join(td, "gui_settings.json"),
                          "r", encoding="utf-8") as f:
                    saved = json.load(f)
                check("设置文件写入 default_model",
                      saved.get("default_model") == "local/ollama/qwen2.5:1.5b",
                      str(saved.get("default_model")))
                check("设置文件写入 local_models.ollama",
                      (saved.get("local_models") or {}).get("ollama", {})
                      .get("base_url") == fake_url,
                      str(saved.get("local_models")))
                check("设置文件写入 port", saved.get("port") == port,
                      str(saved.get("port")))

                # 新实例应读回该默认模型
                svc2 = AS.ApiService()
                check("新实例读回 default_model",
                      svc2.model == "local/ollama/qwen2.5:1.5b", svc2.model)
                check("新实例读回端口", svc2.port == port, str(svc2.port))
            finally:
                AS.ApiService._settings_path = orig_path_fn
        svc.configure(model="glm-5.2")

        # --- 后端连不上时的错误分类
        svc.set_local("ollama", "http://127.0.0.1:1", True)
        t0 = time.time()
        try:
            st, r4 = post({"model": "local/ollama/moondream:latest",
                           "messages": [{"role": "user", "content": "x"}]},
                          timeout=10)
            check("连不上后端时报错", st >= 400, f"{st} {str(r4)[:120]}")
        except u.HTTPError as e:
            err = json.loads(e.read().decode("utf-8") or "{}")
            check("连不上后端时报错", e.code == 502,
                  f"HTTP {e.code} {json.dumps(err, ensure_ascii=False)[:130]}")
        except Exception as e:  # noqa: BLE001
            check("连不上后端时报错", False, repr(e))
        dt = time.time() - t0
        check("连接被拒快速失败（<5s）", dt < 5.0, f"{dt:.1f}s")

    finally:
        svc.stop()
        fake.shutdown()

    print(f"\n合计 {len(OK) + len(FAIL)} 项：通过 {len(OK)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
