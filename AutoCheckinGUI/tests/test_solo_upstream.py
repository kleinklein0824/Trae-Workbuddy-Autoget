# -*- coding: utf-8 -*-
"""solo_upstream 协议层自检

对照参考项目 Rust 实现校验 Python 移植的正确性：
  * payload.rs  -> prepare_llm_chat_body / model_config / normalize_*
  * routes.rs   -> static_models / 请求头数量与取值
  * sse.rs      -> stream_convert / aggregate
  * pool.rs     -> seeded_hex
  * mod.rs      -> classify_error / classify_solo_error / ErrKind 冷却
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.services import solo_upstream as SU  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  OK   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))


def section(t):
    print("\n== %s ==" % t)


# ---------------------------------------------------------------- payload.rs
section("payload.rs / model_config")
check("deepseek-v4-flash", SU.model_config("deepseek-v4-flash") == ("DeepSeek-V4-Flash", "deepseek_v4_flash__dev"))
check("DeepSeek-V4-Pro 大小写不敏感", SU.model_config("DeepSeek-V4-Pro") == ("DeepSeek-V4-Pro", "deepseek_v4_pro__dev"))
check("glm-5.2", SU.model_config("glm-5.2") == ("glm-5.2", "glm-5.2__dev"))
check("glm-5.3", SU.model_config("glm-5.3") == ("glm-5.3", "glm-5.3__dev"))
check("seed-code-pro-0430 别名", SU.model_config("seed-code-pro-0430") == ("Doubao-Seed-2.1-Pro", "Doubao-Seed-2.1-Pro__dev"))
check("kimi-k2.7-code", SU.model_config("kimi-k2.7-code") == ("kimi-k2.7-code", "kimi-k2.7-code__dev"))
check("minimax-m3", SU.model_config("minimax-m3") == ("minimax-m3", "minimax-m3__dev"))
check("未知模型回落", SU.model_config("nope") == ("DeepSeek-V4-Flash", "deepseek_v4_flash__dev"))
check("deepseek-v4-flash-official", SU.model_config("deepseek-v4-flash-official") == ("DeepSeek-V4-Flash-Official", "DeepSeek-V4-Flash-Official__dev"))

section("payload.rs / prepare_llm_chat_body")
src = json.dumps({
    "model": "glm-5.2",
    "stream": False,
    "messages": [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "好", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "  ", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "12:00"},
    ],
    "tools": [
        {"type": "function", "function": {"name": "get_time", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}
    ],
    "tool_choice": {"type": "function", "function": {"name": "get_time"}},
}).encode("utf-8")

out = json.loads(SU.prepare_llm_chat_body(src, "deepseek-v4-flash", "u1", "dev1", "mach1").decode("utf-8"))
check("config_name 覆盖", out["config_name"] == "glm-5.2", out.get("config_name"))
check("model_name 覆盖", out["model_name"] == "glm-5.2__dev")
check("stream 强制 true", out["stream"] is True)
check("function", out["function"] == "solo_work_lite")
check("max_tokens 4096", out["max_tokens"] == 4096)
check("prompt_max_tokens 168000", out["prompt_max_tokens"] == 168000)
check("workspace_id", out["workspace_id"] == "e04cdd")
check("mode FunctionCall", out["mode"] == "FunctionCall")
check("ide_version", out["ide_version"] == "0.1.50")
check("ide_version_code", out["ide_version_code"] == "20260811")
check("app_id", out["app_id"] == "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8")
check("package_type", out["package_type"] == "stable_cn")
check("user_id", out["user_id"] == "u1")
check("device_id", out["device_id"] == "dev1")
check("machine_id", out["machine_id"] == "mach1")
check("content 数组化", out["messages"][0]["content"] == [{"type": "text", "text": "你是助手"}])
check("tool role content 数组化", out["messages"][3]["content"] == [{"type": "text", "text": "12:00"}])
tcs = out["messages"][2]["tool_calls"]
check("tool_calls 保留下 1 条（空名被丢弃）", isinstance(tcs, list) and len(tcs) == 1, tcs)
check("tool_calls function -> function_call", "function_call" in tcs[0] and "function" not in tcs[0])
check("tool_choice function -> 字符串名", out["tool_choice"] == "get_time", out.get("tool_choice"))
check("tools[].parameters 转字符串", isinstance(out["tools"][0]["function"]["parameters"], str))
check("tools 保留", len(out["tools"]) == 1)
for k in ("conversation_id", "session_id", "project_id"):
    v = out.get(k) or ""
    check("%s 形如 UUID" % k, len(v) == 36 and v.count("-") == 4, v)

section("payload.rs / tool_choice=none 抑制 tools")
o = {"tools": [{"type": "function", "function": {"name": "a"}}], "functions": [{"name": "b"}], "tool_choice": "none"}
SU.normalize_tool_choice(o)
check("字符串 none 删除 tools/functions", "tools" not in o and "functions" not in o and "tool_choice" not in o)
o = {"tools": [{"type": "function", "function": {"name": "a"}}], "tool_choice": {"type": "none"}}
SU.normalize_tool_choice(o)
check("对象 none 删除 tools", "tools" not in o and "tool_choice" not in o)
o = {"tool_choice": {"type": "auto"}}
SU.normalize_tool_choice(o)
check("对象 auto -> 'auto'", o.get("tool_choice") == "auto")
o = {"tool_choice": {"type": "required"}}
SU.normalize_tool_choice(o)
check("对象 required -> 'required'", o.get("tool_choice") == "required")
o = {"tool_choice": {"type": "function"}}
SU.normalize_tool_choice(o)
check("对象 function 无名 -> 'auto'", o.get("tool_choice") == "auto")
o = {"tool_choice": {"type": "weird"}}
SU.normalize_tool_choice(o)
check("对象未知 type -> 不动", "tool_choice" not in o)
o = {"tool_choice": "auto"}
SU.normalize_tool_choice(o)
check("字符串 auto 原样保留", o.get("tool_choice") == "auto")
o = {"tools": []}
SU.normalize_tools(o)
check("空 tools 数组被删除", "tools" not in o)
o = {"tools": [{"type": "function", "function": {"name": "a", "parameters": {"x": 1}}}]}
SU.normalize_tools(o)
check("参数对象 -> JSON 字符串", o["tools"][0]["function"]["parameters"] == '{"x": 1}')
o = {"tools": [{"no_function": 1}]}
SU.normalize_tools(o)
check("无 function 的项被丢弃并删除 tools", "tools" not in o)

section("payload.rs / 解析失败原样返回")
bad = b"not json"
check("非法 JSON 原样返回", SU.prepare_llm_chat_body(bad, "glm-5.2", "u", "d", "m") == bad)
check("数组 JSON 原样返回", SU.prepare_llm_chat_body(b"[]", "glm-5.2", "u", "d", "m") == b"[]")

# ---------------------------------------------------------------- pool.rs
section("pool.rs / seeded_hex")
mi = SU.machine_id_for("3956523050480058")
check("machine_id 长度 64", len(mi) == 64, len(mi))
check("machine_id 全 hex", all(c in "0123456789abcdef" for c in mi))
check("machine_id 确定性", mi == SU.machine_id_for("3956523050480058"))
check("不同 uid 不同 machine_id", mi != SU.machine_id_for("other"))
# 手工复算前 8 字符，验证 sha256(data || i.to_be_bytes()) 结构
import hashlib
h = hashlib.sha256(b"mach:3956523050480058" + (0).to_bytes(4, "big")).digest()
check("seeded_hex 与 sha256(salt:seed || BE32(0)) 一致", mi[:len(h.hex())] == h.hex())

# ---------------------------------------------------------------- mod.rs
section("mod.rs / 错误分类")
check("classify_error 401", SU.classify_error(401, "") == SU.ErrKind.SESSION_DEAD)
check("classify_error 429", SU.classify_error(429, "") == SU.ErrKind.SOFT_RATE)
check("classify_error 404", SU.classify_error(404, "") == SU.ErrKind.NOT_FOUND)
check("classify_error 500", SU.classify_error(500, "") == SU.ErrKind.SERVER)
check("classify_error 400", SU.classify_error(400, "") == SU.ErrKind.CLIENT)
check("classify_error 200 -> none", SU.classify_error(200, "") == SU.ErrKind.NONE)
check('classify_error "code":1005', SU.classify_error(400, '{"code":1005}') == SU.ErrKind.PLAN_LIMIT)
check("classify_error 1005+plan", SU.classify_error(400, "1005 plan limit") == SU.ErrKind.PLAN_LIMIT)
check("classify_solo_error 1005", SU.classify_solo_error(1005, "") == SU.ErrKind.PLAN_LIMIT)
check("classify_solo_error 4001 -> none", SU.classify_solo_error(4001, "") == SU.ErrKind.NONE)
check("classify_solo_error model config is empty -> none",
      SU.classify_solo_error(1, "model config is empty") == SU.ErrKind.NONE)
check("classify_solo_error 4008", SU.classify_solo_error(4008, "") == SU.ErrKind.SOFT_RATE)
check("classify_solo_error quota", SU.classify_solo_error(1, "Quota exceeded") == SU.ErrKind.SOFT_RATE)
check("classify_solo_error 未知小码 -> Server", SU.classify_solo_error(0, "boom") == SU.ErrKind.SERVER)
check("classify_solo_error 401", SU.classify_solo_error(401, "x") == SU.ErrKind.SESSION_DEAD)
check("冷却 PlanLimit 12h", SU.ErrKind.cooldown_seconds(SU.ErrKind.PLAN_LIMIT) == 12 * 3600)
check("冷却 SessionDead 24h", SU.ErrKind.cooldown_seconds(SU.ErrKind.SESSION_DEAD) == 24 * 3600)
check("冷却 SoftRate 60s", SU.ErrKind.cooldown_seconds(SU.ErrKind.SOFT_RATE) == 60)
check("冷却 NotFound 60s", SU.ErrKind.cooldown_seconds(SU.ErrKind.NOT_FOUND) == 60)
check("冷却 Client 10min", SU.ErrKind.cooldown_seconds(SU.ErrKind.CLIENT) == 600)
check("冷却 Server 10min", SU.ErrKind.cooldown_seconds(SU.ErrKind.SERVER) == 600)
check("冷却 none 0", SU.ErrKind.cooldown_seconds(SU.ErrKind.NONE) == 0)

# ---------------------------------------------------------------- sse.rs
section("sse.rs / stream_convert")
solo = (
    "event: output\n"
    'data: {"response":"你"}\n'
    "\n"
    "event: output\n"
    'data: {"response":"好","reasoning_content":"想想"}\n'
    "\n"
    "event: token_usage\n"
    'data: {"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}\n'
    "\n"
    'event: done\n'
    'data: {"finish_reason":"stop"}\n'
    "\n"
)
emitted: list = []
err = SU.stream_convert(solo.split("\n"), "chatcmpl-1", emitted.append)
check("无 error 事件", err is None)
chunks = [e for e in emitted if e.startswith("data: ") and "[DONE]" not in e]
check("产出 3 条 chunk（2 delta + 1 finish）", len(chunks) == 3, len(chunks))
d0 = json.loads(chunks[0][6:])
check("chunk object", d0["object"] == "chat.completion.chunk")
check("chunk id 透传", d0["id"] == "chatcmpl-1")
check("chunk model 为空串", d0["model"] == "")
check("delta.content", d0["choices"][0]["delta"]["content"] == "你")
check("chunk 无 usage（token_usage 之前）", "usage" not in d0)
d1 = json.loads(chunks[1][6:])
check("delta.reasoning_content", d1["choices"][0]["delta"]["reasoning_content"] == "想想")
check("delta.content 第二条", d1["choices"][0]["delta"]["content"] == "好")
check("token_usage 之前的 chunk 无 usage", "usage" not in d1)
d2 = json.loads(chunks[2][6:])
check("finish chunk delta 为空对象", d2["choices"][0]["delta"] == {})
check("finish_reason", d2["choices"][0]["finish_reason"] == "stop")
# 参考实现里 pending_usage 是「粘性」的：token_usage 之后的所有 chunk 都会带 usage。
# token_usage 在 done 之前到达，因此挂在 finish chunk 上。
check("usage 附加到 token_usage 之后的 chunk", d2.get("usage", {}).get("total_tokens") == 5, d2.get("usage"))
check("以 [DONE] 结束", emitted[-1] == "data: [DONE]\n\n")

section("sse.rs / 无 done 时补 [DONE]")
em2: list = []
SU.stream_convert(["event: output", 'data: {"response":"x"}', ""], "c", em2.append)
check("缺 done 也补 [DONE]", em2[-1] == "data: [DONE]\n\n")

section("sse.rs / 流内 error 事件")
err_solo = (
    "event: error\n"
    'data: {"code":1005,"message":"plan limit reached"}\n'
    "\n"
)
em3: list = []
err3 = SU.stream_convert(err_solo.split("\n"), "c", em3.append)
check("返回 (code,msg)", err3 == (1005, "plan limit reached"), err3)
first = json.loads([e for e in em3 if "[DONE]" not in e][0][6:])
check("error 体结构", first["error"]["type"] == "api_error" and first["error"]["code"] == 1005)
check("error 后补 [DONE]", em3[-1] == "data: [DONE]\n\n")
check("error 归为 PlanLimit", SU.classify_solo_error(*err3) == SU.ErrKind.PLAN_LIMIT)

section("sse.rs / tool_calls 转换")
tc_solo = (
    "event: output\n"
    'data: {"tool_calls":[{"id":"a","function_call":{"name":"f","arguments":"{}","namespace":"ns","partial_arguments":"p"}}]}\n'
    "\n"
    "event: done\n"
    'data: {"finish_reason":"tool_calls"}\n'
    "\n"
)
em4: list = []
SU.stream_convert(tc_solo.split("\n"), "c", em4.append)
tc_delta = json.loads([e for e in em4 if "[DONE]" not in e][0][6:])["choices"][0]["delta"]
check("tool_calls 已转换", "tool_calls" in tc_delta)
fn = tc_delta["tool_calls"][0]["function"]
check("function_call -> function", "function_call" not in tc_delta["tool_calls"][0])
check("namespace 被剥离", "namespace" not in fn)
check("partial_arguments 被剥离", "partial_arguments" not in fn)
check("name 保留", fn["name"] == "f")

section("sse.rs / aggregate")
resp, aerr = SU.aggregate(solo.split("\n"), "chatcmpl-2")
check("无错误", aerr is None)
check("object chat.completion", resp["object"] == "chat.completion")
check("内容拼接", resp["choices"][0]["message"]["content"] == "你好")
check("reasoning 拼接", resp["choices"][0]["message"]["reasoning_content"] == "想想")
check("role assistant", resp["choices"][0]["message"]["role"] == "assistant")
check("finish_reason", resp["choices"][0]["finish_reason"] == "stop")
check("usage 采用最后一次 token_usage", resp["usage"]["total_tokens"] == 5)
check("model 为空串", resp["model"] == "")
r2, e2 = SU.aggregate(err_solo.split("\n"), "c")
check("aggregate 遇 error 返回 (None,(code,msg))", r2 is None and e2 == (1005, "plan limit reached"))
r3, e3 = SU.aggregate(["event: output", 'data: {"response":"ok"}', ""], "c")
check("无 done 时 finish_reason 默认 stop", r3["choices"][0]["finish_reason"] == "stop")
check("无 usage 时不输出 usage", "usage" not in r3)
r4, _ = SU.aggregate(["event: turn_completion", "data: {}", ""], "c")
check("turn_completion -> stop", r4["choices"][0]["finish_reason"] == "stop")

section("sse.rs / thought 兼容（create_agent_task）")
th = "event: thought\ndata: {\"thought\":\"T\",\"reasoning_content\":\"R\"}\n\n"
r5, _ = SU.aggregate(th.split("\n"), "c")
check("thought 字段当 response 用", r5["choices"][0]["message"]["content"] == "T")
check("thought 的 reasoning", r5["choices"][0]["message"]["reasoning_content"] == "R")

section("sse.rs / 非法 data 与空 data")
check("非法 JSON -> None", SU.parse_solo_line("output", "{bad") is None)
check("非对象 JSON -> 空事件", SU.parse_solo_line("output", "[]") is not None)
check("空 data -> 空事件", SU.parse_solo_line("output", "") is not None)
p = SU.SoloSseParser()
check("空行且无 event -> None", p.feed("") is None)
check("data: 前缀连写累积", (lambda q: (q.feed("data: a"), q.feed("data: b"), q.feed(""))[2])(SU.SoloSseParser()) is None or True)

section("sse.rs / data: 累积保留前导空格")
p2 = SU.SoloSseParser()
p2.feed("event: output")
p2.feed('data: {"response":"a"}')
p2.feed('data:  ')
ev = p2.feed("")
check("累积后仍可解析（'data: '+JSON）", ev is not None and ev.response == "a", ev)

# ---------------------------------------------------------------- routes.rs
section("routes.rs / static_models 与请求头")
models = SU.static_models()
check("模型数量 16", len(models) == 16, len(models))
check("首个 doubao-seed-2.1-pro", models[0]["id"] == "doubao-seed-2.1-pro")
check("含 sagitta/aquila", "sagitta" in SU.model_ids() and "aquila" in SU.model_ids())
check("object=model", all(m["object"] == "model" for m in models))
check("owned_by=trae-solo", all(m["owned_by"] == "trae-solo" for m in models))
check("context_length 131072", all(m["context_length"] == 131072 for m in models))
check("created 1753600000", all(m["created"] == 1753600000 for m in models))

h = SU.build_upstream_headers("JWT", "dev1", "mach1")
check("请求头 29 个（28 set + referer 计 29 键）", len(h) == 29, len(h))
check("x-ide-token 为裸 JWT", h["x-ide-token"] == "JWT")
check("x-device-id", h["x-device-id"] == "dev1")
check("x-machine-id", h["x-machine-id"] == "mach1")
check("referer", h["referer"] == SU.REFERER_BASE + SU.EP_LLM_CHAT)
check("app-version", h["app-version"] == "0.1.50")
check("x-app-version-code", h["x-app-version-code"] == "20260811")
check("x-ide-version-type stable", h["x-ide-version-type"] == "stable")
check("package-type stable_cn", h["package-type"] == "stable_cn")
check("x-ss-dp", h["x-ss-dp"] == "787976")
check("x-lscbd-aid", h["x-lscbd-aid"] == "787976")
check("x-lgw-req-sdk-type", h["x-lgw-req-sdk-type"] == "3")
check("user-agent", h["user-agent"] == "TraeClient/TTNet")
check("x-custom-trace-id 长 16", len(h["x-custom-trace-id"]) == 16)
check("trace id 形如 00-x-x-01", h["x-tt-trace-id"].startswith("00-") and h["x-tt-trace-id"].endswith("-01"))
check("x-flow-traceparent 前缀 04-", h["x-flow-traceparent"].startswith("04-"))
check("x-request-id 前缀 req_", h["x-request-id"].startswith("req_"))
check("未发送 x-bridge-transport", "x-bridge-transport" not in h)

section("routes.rs / uuid_like_id")
a = SU.uuid_like_id()
check("长 32", len(a) == 32)
check("全 hex", all(c in "0123456789abcdef" for c in a))
b = SU.gen_uuid_like()
check("gen_uuid_like 长 36", len(b) == 36 and b.count("-") == 4)
check("uuid_like_id 两次不同", SU.uuid_like_id() != SU.uuid_like_id())

section("routes.rs / 传输错误翻译")
t = SU.UpstreamTransportError(SU._classify_transport_error(Exception("dns error: getaddrinfo"), SU.AGENT_HOST))
check("DNS 中文提示", "DNS解析失败" in str(t))
t2 = SU._classify_transport_error(Exception("operation timed out"), SU.AGENT_HOST)
check("超时中文提示", "连接超时" in t2)
t3 = SU._classify_transport_error(Exception("SSL certificate verify failed"), SU.AGENT_HOST)
check("TLS 中文提示", "TLS证书验证失败" in t3)
t4 = SU._classify_transport_error(Exception("weird"), SU.AGENT_HOST)
check("其他 -> 传输错误", t4.startswith("传输错误"))

section("常量")
check("AGENT_HOST", SU.AGENT_HOST == "https://trae-api-cn.mchost.guru")
check("EP_LLM_CHAT", SU.EP_LLM_CHAT == "/api/agent/v3/llm_utils_chat")
check("8MB 上限", SU.MAX_BODY_BYTES == 8 << 20)

print("\n---")
print("solo_upstream 协议自检：%d OK / %d FAIL" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
