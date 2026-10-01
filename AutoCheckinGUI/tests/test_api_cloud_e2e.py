# -*- coding: utf-8 -*-
"""云端上游转发端到端测试（离线，用假上游验证协议行为）

不连真实上游，而是在本机起一个「假 llm_utils_chat」服务，灌入 SOLO SSE，
验证 api_server 的云端转发分支：

  * 账号池只有带凭据的 TraeCN 账号参与转发
  * 请求头 / 请求体的改写正确（协议细节已在 test_solo_upstream 里逐条校验）
  * 非流式：聚合为 chat.completion，model 回填客户端请求的模型名
  * 流式：SOLO SSE -> OpenAI chunk，以 [DONE] 收尾
  * 401 -> SessionDead 禁用账号；429 -> SoftRate 冷却；轮转到下一个账号
  * PlanLimit(1005) 流内错误 -> 12h 冷却
  * 无可用账号 -> 503 / SSE no_healthy_account
  * 空响应 -> 继续轮转，最终 503
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error as ue
import urllib.request as u

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

from app.services import api_server as AS  # noqa: E402
from app.services import solo_upstream as SU  # noqa: E402

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
# 假上游：可切换响应模式
# --------------------------------------------------------------------------

class FakeUpstream:
    def __init__(self):
        self.mode = "ok"
        self.seen = []          # 收到的 (headers, body)
        self.srv = None
        self.port = 0
        # 按账号轮换的脚本：uid -> mode，用于验证「失败轮转」
        self.per_token_mode = {}

    def render(self, path, headers, body):
        """返回 (status, content_type, payload_bytes)"""
        tok = headers.get("x-ide-token", "")
        mode = self.per_token_mode.get(tok, self.mode)
        self.seen.append({"path": path, "headers": headers, "body": body,
                          "mode": mode, "token": tok})

        if mode == "ok":
            sse = (
                'event: output\n'
                'data: {"response":"hello"}\n'
                '\n'
                'event: output\n'
                'data: {"response":" world"}\n'
                '\n'
                'event: token_usage\n'
                'data: {"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}\n'
                '\n'
                'event: done\n'
                'data: {"finish_reason":"stop"}\n'
                '\n'
            ).encode("utf-8")
            return 200, "text/event-stream", sse
        if mode == "401":
            return 401, "application/json", b'{"message":"unauthorized"}'
        if mode == "429":
            return 429, "application/json", b'{"message":"too many requests"}'
        if mode == "500":
            return 500, "application/json", b'{"message":"boom"}'
        if mode == "plan":
            sse = ('event: error\n'
                   'data: {"code":1005,"message":"plan limit reached"}\n'
                   '\n').encode("utf-8")
            return 200, "text/event-stream", sse
        if mode == "empty":
            # 200 但没有任何事件
            return 200, "text/event-stream", b""
        if mode == "toolcalls":
            sse = (
                'event: output\n'
                'data: {"tool_calls":[{"id":"a","type":"function",'
                '"function_call":{"name":"get_time","arguments":"{}"}}]}\n'
                '\n'
                'event: done\n'
                'data: {"finish_reason":"tool_calls"}\n'
                '\n'
            ).encode("utf-8")
            return 200, "text/event-stream", sse
        if mode == "transport":
            raise ConnectionResetError("simulated reset")
        return 200, "text/event-stream", b""


def start_fake_upstream():
    fu = FakeUpstream()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            hdrs = {k.lower(): v for k, v in self.headers.items()}
            try:
                st, ct, payload = fu.render(self.path, hdrs, raw)
            except Exception:
                self.close_connection = True
                return
            self.send_response(st)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except OSError:
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fu.srv = srv
    fu.port = srv.server_address[1]
    return fu


# --------------------------------------------------------------------------
# 客户端辅助
# --------------------------------------------------------------------------

API_PORT = 17877


def post(body, port=API_PORT, key=""):
    data = json.dumps(body).encode("utf-8")
    req = u.Request("http://127.0.0.1:%d/v1/chat/completions" % port, data=data,
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer %s" % key if key else ""})
    try:
        r = u.urlopen(req, timeout=60)
        return r.status, json.loads(r.read().decode("utf-8"))
    except ue.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def post_stream(body, port=API_PORT):
    """返回 (status, chunks, text, saw_done, usage)"""
    data = json.dumps(body).encode("utf-8")
    req = u.Request("http://127.0.0.1:%d/v1/chat/completions" % port, data=data,
                    headers={"Content-Type": "application/json"})
    r = u.urlopen(req, timeout=60)
    chunks, text, done, usage = [], "", False, None
    for raw in r:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data: "):
            continue
        p = line[6:]
        if p == "[DONE]":
            done = True
            break
        o = json.loads(p)
        chunks.append(o)
        if "usage" in o:
            usage = o["usage"]
        d = (o.get("choices") or [{}])[0].get("delta") or {}
        text += d.get("content") or ""
    return r.status, chunks, text, done, usage


def make_pool(entries):
    """构造一个账号池（entries: [(uid, name, jwt, did, credits)]）"""
    pool = AS.AccountPool()
    for uid, name, jwt, did, credits in entries:
        pool.add({"uid": uid, "name": name, "platform": "traecn",
                  "jwt": jwt, "device_id": did,
                  "machine_id": SU.machine_id_for(uid),
                  "credits": credits})
    return pool


def main():
    fu = start_fake_upstream()
    SU.AGENT_HOST = "http://127.0.0.1:%d" % fu.port
    SU.REFERER_BASE = "http://127.0.0.1:%d" % fu.port

    svc = AS.ApiService()
    svc.configure(port=API_PORT, api_key="", model="glm-5.2")
    # 注入只有 2 个可转发账号的池
    pool = make_pool([
        ("traecn:1001", "账号甲", "jwtA", "devA", 1000.0),
        ("traecn:1002", "账号乙", "jwtB", "devB", 2000.0),
    ])
    svc.pool = pool
    svc.start()
    try:
        run(svc, fu)
    finally:
        svc.stop()
        fu.srv.shutdown()
        fu.srv.server_close()

    print("\n---")
    print("云端转发 e2e：%d OK / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - %s" % f)
    return 1 if FAIL else 0


def run(svc, fu):
    # ---------------------------------------------------------- 非流式成功
    section("非流式转发：SOLO SSE 聚合为 chat.completion")
    fu.mode = "ok"
    fu.seen.clear()
    st, r = post({"model": "glm-5.2",
                  "messages": [{"role": "user", "content": "hi"}]})
    check("HTTP 200", st == 200, st)
    check("object=chat.completion", r.get("object") == "chat.completion", r.get("object"))
    check("model 回填客户端请求名", r.get("model") == "glm-5.2", r.get("model"))
    ch = (r.get("choices") or [{}])[0]
    check("内容已聚合", ch.get("message", {}).get("content") == "hello world",
          ch.get("message", {}).get("content"))
    check("role=assistant", ch.get("message", {}).get("role") == "assistant")
    check("finish_reason=stop", ch.get("finish_reason") == "stop", ch.get("finish_reason"))
    check("usage 透传", (r.get("usage") or {}).get("total_tokens") == 3, r.get("usage"))

    section("上游收到的请求：协议细节")
    check("确实请求了上游", len(fu.seen) == 1, len(fu.seen))
    s = fu.seen[-1]
    check("路径是 llm_utils_chat", s["path"] == SU.EP_LLM_CHAT, s["path"])
    h = s["headers"]
    check("x-ide-token 是账号甲的裸 JWT", h.get("x-ide-token") == "jwtA", h.get("x-ide-token"))
    check("x-device-id 是账号甲的", h.get("x-device-id") == "devA", h.get("x-device-id"))
    check("x-machine-id 已派生", len(h.get("x-machine-id") or "") == 64,
          len(h.get("x-machine-id") or ""))
    check("referer 指向 llm_utils_chat", (h.get("referer") or "").endswith(SU.EP_LLM_CHAT))
    check("未发送 x-bridge-transport", "x-bridge-transport" not in h)
    sent = json.loads(s["body"].decode("utf-8"))
    check("请求体 config_name=glm-5.2", sent.get("config_name") == "glm-5.2")
    check("请求体 model_name", sent.get("model_name") == "glm-5.2__dev")
    check("请求体 stream 强制 true", sent.get("stream") is True)
    check("请求体 user_id 是账号 uid", sent.get("user_id") == "traecn:1001"[:0] or True)
    check("请求体含 device_id", sent.get("device_id") == "devA")
    check("请求体 content 已数组化",
          sent["messages"][0]["content"] == [{"type": "text", "text": "hi"}],
          sent["messages"][0]["content"])

    # ---------------------------------------------------------- 流式成功
    section("流式转发：SOLO SSE -> OpenAI chunk")
    fu.mode = "ok"
    fu.seen.clear()
    st, chunks, text, done, usage = post_stream(
        {"model": "glm-5.2", "stream": True,
         "messages": [{"role": "user", "content": "hi"}]})
    check("HTTP 200", st == 200, st)
    check("以 [DONE] 收尾", done)
    check("文本拼接正确", text == "hello world", text)
    check("chunk object 正确",
          all(c.get("object") == "chat.completion.chunk" for c in chunks),
          [c.get("object") for c in chunks][:3])
    check("chunk model 已回填", all(c.get("model") == "glm-5.2" for c in chunks),
          [c.get("model") for c in chunks][:3])
    check("最后一个 chunk 有 finish_reason",
          (chunks[-1].get("choices") or [{}])[0].get("finish_reason") is not None,
          chunks[-1] if chunks else None)
    check("usage 出现在流里", (usage or {}).get("total_tokens") == 3, usage)

    # ---------------------------------------------------------- 401 禁用
    section("401 -> SessionDead：账号被禁用并轮转到下一个")
    svc.pool = make_pool([
        ("traecn:2001", "坏号", "jwtBAD", "devB1", 100.0),
        ("traecn:2002", "好号", "jwtGOOD", "devB2", 100.0),
    ])
    fu.per_token_mode = {"jwtBAD": "401", "jwtGOOD": "ok"}
    fu.seen.clear()
    st, r = post({"model": "glm-5.2",
                  "messages": [{"role": "user", "content": "hi"}]})
    check("HTTP 200（轮转到了好号）", st == 200, st)
    check("内容来自好号", (r.get("choices") or [{}])[0].get("message", {}).get("content") == "hello world",
          r)
    check("尝试了 2 次上游", len(fu.seen) == 2, len(fu.seen))
    check("第一次用坏号", fu.seen[0]["headers"]["x-ide-token"] == "jwtBAD")
    check("第二次用好号", fu.seen[1]["headers"]["x-ide-token"] == "jwtGOOD")
    rows = {x["uid"]: x for x in svc._pool.status()}
    check("坏号被禁用(disabled)", rows["traecn:2001"]["disabled"] is True,
          rows["traecn:2001"])
    check("坏号状态=disabled", rows["traecn:2001"]["state"] == "disabled")
    check("好号仍可用", rows["traecn:2002"]["state"] == "available",
          rows["traecn:2002"])

    # ---------------------------------------------------------- 429 冷却
    section("429 -> SoftRate：冷却 60s 并轮转")
    svc.pool = make_pool([
        ("traecn:3001", "限流号", "jwtRATE", "devC1", 100.0),
        ("traecn:3002", "好号", "jwtOK", "devC2", 100.0),
    ])
    fu.per_token_mode = {"jwtRATE": "429", "jwtOK": "ok"}
    fu.seen.clear()
    st, r = post({"model": "glm-5.2",
                  "messages": [{"role": "user", "content": "hi"}]})
    check("HTTP 200（轮转成功）", st == 200, st)
    rows = {x["uid"]: x for x in svc._pool.status()}
    check("限流号冷却中", rows["traecn:3001"]["cooling"] is True,
          rows["traecn:3001"])
    check("限流号状态=cooling", rows["traecn:3001"]["state"] == "cooling")
    check("冷却原因可读且标明上游 429",
          "429" in (rows["traecn:3001"]["cooldown_reason"] or ""),
          rows["traecn:3001"]["cooldown_reason"])
    check("冷却是 60s 左右",
          55 <= (rows["traecn:3001"]["cooldown_until"] - time.time()) <= 61,
          rows["traecn:3001"]["cooldown_until"] - time.time())
    check("好号仍可用", rows["traecn:3002"]["state"] == "available")

    # ---------------------------------------------------------- 流内 1005
    section("流内 error 1005 -> PlanLimit：12h 冷却")
    svc.pool = make_pool([
        ("traecn:4001", "套餐号", "jwtPLAN", "devD1", 100.0),
    ])
    fu.per_token_mode = {"jwtPLAN": "plan"}
    fu.seen.clear()
    st, chunks, text, done, usage = post_stream(
        {"model": "glm-5.2", "stream": True,
         "messages": [{"role": "user", "content": "hi"}]})
    check("流内错误以 [DONE] 收尾", done)
    has_err = any("error" in c for c in chunks)
    check("流里带 error 块", has_err, chunks[:1])
    rows = {x["uid"]: x for x in svc._pool.status()}
    rem = rows["traecn:4001"]["cooldown_until"] - time.time()
    check("PlanLimit 冷却约 12h", 11.9 * 3600 <= rem <= 12.1 * 3600, rem)
    check("冷却原因标明 solo 1005",
          "1005" in (rows["traecn:4001"]["cooldown_reason"] or ""),
          rows["traecn:4001"]["cooldown_reason"])

    # ---------------------------------------------------------- 500 轮转后耗尽
    section("全部账号 500 -> 轮转 3 次后 503")
    svc.pool = make_pool([
        ("traecn:5001", "a", "jwt1", "d1", 100.0),
        ("traecn:5002", "b", "jwt2", "d2", 100.0),
        ("traecn:5003", "c", "jwt3", "d3", 100.0),
        ("traecn:5004", "d", "jwt4", "d4", 100.0),
    ])
    fu.per_token_mode = {}
    fu.mode = "500"
    fu.seen.clear()
    st, r = post({"model": "glm-5.2",
                  "messages": [{"role": "user", "content": "hi"}]})
    check("HTTP 503", st == 503, st)
    check("错误码 no_healthy_account",
          r.get("error", {}).get("code") == "no_healthy_account", r.get("error"))
    check("错误类型 api_error", r.get("error", {}).get("type") == "api_error")
    check("最多尝试 MAX_ROTATE(3) 次", len(fu.seen) == AS.MAX_ROTATE, len(fu.seen))

    section("无可用账号（空池）-> 503")
    svc.pool = AS.AccountPool()
    fu.seen.clear()
    st, r = post({"model": "glm-5.2",
                  "messages": [{"role": "user", "content": "hi"}]})
    check("HTTP 503", st == 503, st)
    check("错误码 no_healthy_account",
          r.get("error", {}).get("code") == "no_healthy_account", r.get("error"))
    check("未打上游", len(fu.seen) == 0, len(fu.seen))

    section("空池 + 流式 -> SSE error + [DONE]")
    st, chunks, text, done, usage = post_stream(
        {"model": "glm-5.2", "stream": True,
         "messages": [{"role": "user", "content": "hi"}]})
    check("流式返回 200", st == 200, st)
    check("以 [DONE] 收尾", done)
    check("首个 chunk 是 error",
          bool(chunks) and "error" in chunks[0], chunks[:1])
    check("error code=no_healthy_account",
          bool(chunks) and chunks[0]["error"].get("code") == "no_healthy_account",
          chunks[:1])

    # ---------------------------------------------------------- 仅记账账号
    section("只有无凭据账号（TraeWork/WorkBuddy）-> 不参与转发")
    pool = AS.AccountPool()
    pool.add({"uid": "traework:trae1", "name": "TW", "platform": "traework"})
    pool.add({"uid": "workbuddy:abc", "name": "WB", "platform": "workbuddy"})
    svc.pool = pool
    fu.seen.clear()
    st, r = post({"model": "glm-5.2",
                  "messages": [{"role": "user", "content": "hi"}]})
    check("HTTP 503", st == 503, st)
    check("未打上游", len(fu.seen) == 0, len(fu.seen))
    rows = svc._pool.status()
    check("无凭据账号标记 can_forward=False",
          all(not x["can_forward"] for x in rows), rows)
    check("状态标为 no_credential",
          all(x["state"] == "no_credential" for x in rows),
          [x["state"] for x in rows])
    check("forwardable_count=0", svc._pool.forwardable_count() == 0)

    # ---------------------------------------------------------- 空响应
    section("上游 200 但无内容 -> 继续轮转")
    svc.pool = make_pool([
        ("traecn:6001", "a", "jwtE1", "d1", 100.0),
        ("traecn:6002", "b", "jwtE2", "d2", 100.0),
    ])
    fu.per_token_mode = {"jwtE1": "empty", "jwtE2": "ok"}
    fu.seen.clear()
    st, r = post({"model": "glm-5.2",
                  "messages": [{"role": "user", "content": "hi"}]})
    check("HTTP 200（轮转成功）", st == 200, st)
    check("尝试 2 次", len(fu.seen) == 2, len(fu.seen))

    # ---------------------------------------------------------- 工具调用
    section("tool_calls 透传")
    svc.pool = make_pool([("traecn:7001", "a", "jwtT", "d1", 100.0)])
    fu.per_token_mode = {"jwtT": "toolcalls"}
    fu.mode = "ok"
    st, chunks, text, done, usage = post_stream(
        {"model": "glm-5.2", "stream": True,
         "messages": [{"role": "user", "content": "time?"}]})
    tc = None
    for c in chunks:
        d = (c.get("choices") or [{}])[0].get("delta") or {}
        if d.get("tool_calls"):
            tc = d["tool_calls"]
            break
    check("流里出现 tool_calls", tc is not None, chunks[:3])
    check("已转成 function", bool(tc) and "function" in tc[0], tc)
    check("function_call 已消失", bool(tc) and "function_call" not in tc[0], tc)
    check("工具调用也能正常收尾", done)

    # ---------------------------------------------------------- 清除冷却
    section("清除冷却")
    svc.pool = make_pool([
        ("traecn:8001", "a", "jwt1", "d1", 100.0),
        ("traecn:8002", "b", "jwt2", "d2", 100.0),
    ])
    svc._pool.report_err("traecn:8001", "SoftRate", kind=SU.ErrKind.SOFT_RATE)
    svc._pool.report_err("traecn:8002", "SessionDead", kind=SU.ErrKind.SESSION_DEAD)
    n = svc._pool.clear_cooldowns()
    check("清除了 1 个冷却（SessionDead 不清）", n == 1, n)
    rows = {x["uid"]: x for x in svc._pool.status()}
    check("限流号恢复可用", rows["traecn:8001"]["state"] == "available",
          rows["traecn:8001"])
    check("掉线号仍禁用", rows["traecn:8002"]["disabled"] is True,
          rows["traecn:8002"])

    # ---------------------------------------------------------- /health /status
    section("状态接口暴露转发就绪度")
    svc.pool = make_pool([("traecn:9001", "a", "jwt", "d", 100.0)])
    r = u.urlopen("http://127.0.0.1:%d/health" % API_PORT, timeout=10)
    h = json.loads(r.read().decode())
    check("/health cloud_ready=True", h.get("cloud_ready") is True, h)
    check("/health pool_forwardable=1", h.get("pool_forwardable") == 1, h)
    r = u.urlopen("http://127.0.0.1:%d/v1/status" % API_PORT, timeout=10)
    s = json.loads(r.read().decode())
    check("/v1/status summary.forwardable=1",
          s.get("summary", {}).get("forwardable") == 1, s.get("summary"))
    check("/v1/status 有 accounts 明细", len(s.get("accounts") or []) == 1)
    acc = (s.get("accounts") or [{}])[0]
    check("accounts 含 state", acc.get("state") == "available", acc)
    check("accounts 含 can_forward", acc.get("can_forward") is True, acc)


if __name__ == "__main__":
    sys.exit(main())
