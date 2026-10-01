#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地后端协议测试（不依赖真实 Ollama / llama.cpp 进程）。

起两个**最小的假后端**，严格按真实协议应答，用来验证
`app/services/local_backends.py` 的协议转换是否正确：

  * FakeOllama   —— /api/tags + /api/chat（NDJSON 流）
  * FakeLlama    —— /v1/models + /v1/chat/completions（SSE 流）

这两条路径必须逐字段验证，因为真实后端不一定装在这台机器上，
而协议写错了客户端就完全用不了。

用法：
    python tests/test_local_backends.py
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import local_backends as LB  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (f"   {extra}" if extra else ""))


# --------------------------------------------------------------------------
# 假 Ollama：/api/tags 与 /api/chat（NDJSON）
# --------------------------------------------------------------------------

class _OllamaHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/tags":
            self._json(200, {"models": [
                {"name": "llama3.2:latest", "size": 2019393189,
                 "details": {"family": "llama"}},
                {"name": "moondream:latest", "size": 1736,
                 "details": {"family": "phi"}},
            ]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path != "/api/chat":
            self._json(404, {"error": "not found"})
            return
        stream = bool(body.get("stream"))
        echo = f"收到{len(body.get('messages') or [])}条消息"
        if not stream:
            self._json(200, {
                "model": body.get("model"),
                "message": {"role": "assistant", "content": echo},
                "done": True,
                "prompt_eval_count": 11,
                "eval_count": 7,
            })
            return
        # NDJSON 逐行
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for piece in ("你", "好", "世界"):
            line = json.dumps({"message": {"role": "assistant",
                                           "content": piece},
                               "done": False}) + "\n"
            self.wfile.write(line.encode())
            self.wfile.flush()
        self.wfile.write((json.dumps({"done": True, "prompt_eval_count": 5,
                                      "eval_count": 3}) + "\n").encode())
        self.wfile.flush()


# --------------------------------------------------------------------------
# 假 llama.cpp：/v1/models 与 /v1/chat/completions（SSE）
# --------------------------------------------------------------------------

class _LlamaHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [
                {"id": "qwen2.5-7b-instruct-q4_k_m.gguf", "object": "model"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return
        if not body.get("stream"):
            self._json(200, {
                "id": "cmpl-1", "object": "chat.completion",
                "created": 1, "model": body.get("model"),
                "choices": [{"index": 0,
                             "message": {"role": "assistant",
                                         "content": "llama 非流式回复"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4,
                          "total_tokens": 13},
            })
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for piece in ("llama", " 流式", " 回复"):
            obj = {"id": "cmpl-1", "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {"content": piece},
                                "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def main():
    print("=== 1. 假 Ollama ===")
    osrv, ourl = serve(_OllamaHandler)
    be = LB.make_backend("ollama", ourl)
    models = be.list_models()
    check("解析 /api/tags", len(models) == 2, f"{[m['id'] for m in models]}")
    check("取到 family 字段", models[0].get("family") == "llama")

    ok, detail, n = LB.probe("ollama", ourl)
    check("probe 成功", ok and n == 2, detail)

    text, usage = be.chat({"messages": [{"role": "user", "content": "hi"}]},
                          "llama3.2:latest")
    check("非流式内容正确", text == "收到1条消息", repr(text))
    check("非流式 usage 映射 (prompt_eval_count→prompt_tokens)",
          usage["prompt_tokens"] == 11 and usage["completion_tokens"] == 7,
          str(usage))

    chunks, last = [], None
    for item in be.chat_stream({"messages": [{"role": "user",
                                              "content": "hi"}]},
                               "llama3.2:latest"):
        if isinstance(item, dict):
            last = item
        else:
            chunks.append(item)
    check("NDJSON 流式拼接", "".join(chunks) == "你好世界", repr("".join(chunks)))
    check("流式 usage 取自 done 帧",
          last and last["prompt_tokens"] == 5, str(last))

    # options 映射
    payload = be._payload({"messages": [], "temperature": 0.3,
                           "max_tokens": 128, "top_p": 0.9}, "m", False)
    check("temperature 进入 options", payload["options"]["temperature"] == 0.3)
    check("max_tokens→num_predict", payload["options"]["num_predict"] == 128)
    osrv.shutdown()

    print()
    print("=== 2. 假 llama.cpp ===")
    lsrv, lurl = serve(_LlamaHandler)
    be2 = LB.make_backend("llamacpp", lurl)
    m2 = be2.list_models()
    check("解析 /v1/models", len(m2) == 1, str(m2))
    ok2, d2, n2 = LB.probe("llamacpp", lurl)
    check("probe 成功", ok2, d2)

    t2, u2 = be2.chat({"messages": [{"role": "user", "content": "hi"}]}, "m")
    check("非流式内容", t2 == "llama 非流式回复", repr(t2))
    check("usage 直接透传", u2["total_tokens"] == 13, str(u2))

    c2, last2 = [], None
    for item in be2.chat_stream({"messages": [{"role": "user",
                                               "content": "hi"}]}, "m"):
        if isinstance(item, dict):
            last2 = item
        else:
            c2.append(item)
    check("SSE 流式拼接", "".join(c2) == "llama 流式 回复", repr("".join(c2)))
    check("末尾补 usage", last2 is not None, str(last2))
    lsrv.shutdown()

    print()
    print("=== 3. 模型名路由 ===")
    cases = [
        ("glm-5", (None, "glm-5")),
        ("local/ollama/llama3.2", ("ollama", "llama3.2")),
        ("local/llamacpp/qwen.gguf", ("llamacpp", "qwen.gguf")),
        ("local/llama3.2", (None, "llama3.2")),
    ]
    for mid, want in cases:
        got = LB.split_model(mid)
        check(f"split_model({mid!r})", got == want, f"got={got}")
    check("qualify 反向拼装",
          LB.qualify("ollama", "x") == "local/ollama/x")

    print()
    print("=== 4. 错误处理 ===")
    try:
        bad = LB.make_backend("nope")
        check("未知后端应报错", False)
    except LB.BackendError:
        check("未知后端应报错", True)

    ok3, d3, n3 = LB.probe("ollama", "http://127.0.0.1:1")
    check("连不上时给出可读原因",
          (not ok3) and "无法连接" in d3, d3)

    # 「连接被拒绝」必须立刻返回，不能重试拖时间
    t0 = time.time()
    ok4, d4, _ = LB.probe("ollama", "http://127.0.0.1:1")
    refused_elapsed = time.time() - t0
    check("连接被拒不会重试空等",
          (not ok4) and refused_elapsed < 5, f"{refused_elapsed:.1f}s")

    # 超时才应该重试（用不可路由地址制造超时）
    t0 = time.time()
    ok5, d5, _ = LB.probe("ollama", "http://10.255.255.1:11434")
    timeout_elapsed = time.time() - t0
    check("超时会重试（耗时明显更长）",
          (not ok5) and timeout_elapsed > refused_elapsed,
          f"超时 {timeout_elapsed:.1f}s vs 拒绝 {refused_elapsed:.1f}s")

    print()
    print(f"===== 通过 {len(PASS)} / 失败 {len(FAIL)} =====")
    if FAIL:
        for f in FAIL:
            print("  失败：", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
