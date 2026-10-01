# -*- coding: utf-8 -*-
"""SOLO 上游转发协议（Python 实现）

本模块把参考项目（Trae-workbuddyAssistant 2.4.6，Tauri/Rust）中
``src-tauri/src/api_server/{payload,routes,sse,pool,mod}.rs`` 的协议逻辑
一比一移植为纯 Python，使「API 服务」的云端模型转发真实可用：

    payload.rs  -> prepare_llm_chat_body / model_config / normalize_tool_choice / normalize_tools
    routes.rs   -> make_upstream_request（28 个请求头）/ static_models / uuid_like_id
    sse.rs      -> SoloSseParser / stream_convert / aggregate
    pool.rs     -> seeded_hex（machine_id 派生）
    mod.rs      -> ErrKind / classify_error / classify_solo_error

设计约定
--------
* ``AGENT_HOST`` 走 ``llm_utils_chat`` 端点（消耗 IDE 积分 product_id 208）。
  参考项目已放弃 ``create_agent_task``（服务端要求 aha 加密），详见
  ``docs/api-credit-analysis.md`` 附录 D。
* **不发送** ``x-bridge-transport: aha``（参考实现亦未发送），明文 JSON 才会被接受。
* 代理：urllib 默认会读系统代理，若系统代理指向本机端口会形成回环
  （参考项目因此在进程内设置 ``NO_PROXY=*``）。这里统一使用禁用代理的
  opener，并在请求期间临时清除 ``*_proxy`` 环境变量。
* 读超时：SSE 流式不能设读超时，否则长回答会被截断。

所有常量都允许被上层覆盖（``AGENT_HOST``、``FUNCTION`` 等），
以便官方协议变化时无需改动业务代码。
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

# --------------------------------------------------------------------------
# 常量（对应 mod.rs 顶部）
# --------------------------------------------------------------------------

AGENT_HOST = "https://trae-api-cn.mchost.guru"
EP_LLM_CHAT = "/api/agent/v3/llm_utils_chat"
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
IDE_VERSION = "0.1.50"
IDE_VERSION_CODE = "20260811"
FUNCTION = "solo_work_lite"
DEFAULT_MODEL = "deepseek-v4-flash"
REFERER_BASE = "https://trae-api-cn.mchost.guru"

# 连接/写超时；读超时为 None（SSE 不设读超时）
CONNECT_TIMEOUT = 10.0
WRITE_TIMEOUT = 30.0
READ_TIMEOUT: Optional[float] = None

MAX_BODY_BYTES = 8 << 20  # 8MB

# 请求头里的固定假设备信息（与参考实现一致，保证指纹稳定）
DEVICE_TYPE = "windows"
DEVICE_BRAND = "CREFG-XX"
DEVICE_CPU = "Intel"
OS_VERSION = "Windows 11 Home China"
USER_AGENT = "TraeClient/TTNet"
ACCEPT_ENCODING = "gzip, deflate, br, zstd"

_U64_MASK = 0xFFFFFFFFFFFFFFFF
_SEED_MUL = 0x517CC1B727220A95
_SEED_ADD = 0x9E3779B97F4A7C15


# --------------------------------------------------------------------------
# 模型名映射（payload.rs model_config）
# --------------------------------------------------------------------------

# 客户端可见模型名（小写）-> (config_name, model_name)
_MODEL_CONFIG: Dict[str, Tuple[str, str]] = {
    "deepseek-v4-flash": ("DeepSeek-V4-Flash", "deepseek_v4_flash__dev"),
    "deepseek-v4-flash-official": (
        "DeepSeek-V4-Flash-Official",
        "DeepSeek-V4-Flash-Official__dev",
    ),
    "deepseek-v4-pro": ("DeepSeek-V4-Pro", "deepseek_v4_pro__dev"),
    "glm-5.2": ("glm-5.2", "glm-5.2__dev"),
    "glm-5.3": ("glm-5.3", "glm-5.3__dev"),
    "doubao-seed-2.1-pro": ("Doubao-Seed-2.1-Pro", "Doubao-Seed-2.1-Pro__dev"),
    "seed-code-pro-0430": ("Doubao-Seed-2.1-Pro", "Doubao-Seed-2.1-Pro__dev"),
    "doubao-seed-2.1-turbo": ("Doubao-Seed-2.1-Turbo", "Doubao-Seed-2.1-Turbo__dev"),
    "kimi-k2.7-code": ("kimi-k2.7-code", "kimi-k2.7-code__dev"),
    "minimax-m3": ("minimax-m3", "minimax-m3__dev"),
}

_DEFAULT_CONFIG = ("DeepSeek-V4-Flash", "deepseek_v4_flash__dev")


def model_config(model: str) -> Tuple[str, str]:
    """模型显示名 -> (config_name, 内部 model_name)，大小写不敏感。"""
    return _MODEL_CONFIG.get((model or "").strip().lower(), _DEFAULT_CONFIG)


# --------------------------------------------------------------------------
# 随机 ID（payload.rs gen_uuid_like / routes.rs uuid_like_id）
# --------------------------------------------------------------------------

def uuid_like_id() -> str:
    """32 位十六进制（对应 routes.rs uuid_like_id）。"""
    nanos = time.time_ns()
    seed = ((nanos & _U64_MASK) * _SEED_MUL) & _U64_MASK
    a = seed.to_bytes(8, "little")
    b = ((seed + _SEED_ADD) & _U64_MASK).to_bytes(8, "little")
    return (a + b).hex()


def gen_uuid_like() -> str:
    """8-4-4-4-12 形式的类 UUID（对应 payload.rs gen_uuid_like）。"""
    h = uuid_like_id()
    return "{}-{}-{}-{}-{}".format(h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def now_ts() -> int:
    return int(time.time())


def seeded_hex(n: int, seed: str, salt: str) -> str:
    """确定性派生 hex 字符串（pool.rs seeded_hex，与 device_proxy 的算法一致）。

    用于从 uid 派生 ``machine_id``，保证同一账号始终得到同一设备标识。
    """
    data = ("%s:%s" % (salt, seed)).encode("utf-8")
    need = (n + 1) // 2
    out = bytearray()
    i = 0
    while len(out) < need:
        h = hashlib.sha256()
        h.update(data)
        h.update(i.to_bytes(4, "big"))
        out.extend(h.digest())
        i += 1
    return out[:need].hex()[:n]


def machine_id_for(uid: str) -> str:
    """uid -> 64 位 hex machine_id。"""
    return seeded_hex(64, uid, "mach")


# --------------------------------------------------------------------------
# 请求体改写（payload.rs）
# --------------------------------------------------------------------------

def normalize_tool_choice(obj: Dict[str, Any]) -> None:
    """规范化 tool_choice；``none`` 语义会连带删除 tools/functions。"""

    def _suppress() -> None:
        obj.pop("tools", None)
        obj.pop("functions", None)

    if "tool_choice" not in obj:
        return
    tc = obj.pop("tool_choice")

    if isinstance(tc, str):
        if tc.strip().lower() == "none":
            _suppress()
        else:
            obj["tool_choice"] = tc
        return

    if isinstance(tc, dict):
        typ = str(tc.get("type") or "").lower()
        if typ == "none":
            _suppress()
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            fn = tc.get("function")
            name = ""
            if isinstance(fn, dict):
                name = str(fn.get("name") or "")
            if not name:
                name = str(tc.get("name") or "")
            name = name.strip() or "auto"
            obj["tool_choice"] = name
        # 其他类型：什么都不做


def normalize_tools(obj: Dict[str, Any]) -> None:
    """tools[].function.parameters 由对象转成 JSON 字符串。"""
    raw = obj.get("tools")
    if raw is None:
        return
    if not isinstance(raw, list):
        return
    if not raw:
        obj.pop("tools", None)
        return

    out: List[Any] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        if not isinstance(fn, dict):
            continue
        params = fn.get("parameters")
        if isinstance(params, (dict, list)):
            fn["parameters"] = json.dumps(params, ensure_ascii=False)
        out.append(item)

    if not out:
        obj.pop("tools", None)
    else:
        obj["tools"] = out


def prepare_llm_chat_body(
    src: bytes,
    default_model: str,
    uid: str,
    device_id: str,
    machine_id: str,
) -> bytes:
    """OpenAI 请求体 -> ``llm_utils_chat`` 请求体。

    ``llm_utils_chat`` 消耗 IDE 积分（product_id 208）。
    解析失败时原样返回（与参考实现一致）。
    """
    try:
        obj = json.loads(src.decode("utf-8"))
    except Exception:
        return src
    if not isinstance(obj, dict):
        return src

    # messages: content 字符串 -> [{type:text, text:...}]
    #           assistant 的 tool_calls[].function -> function_call
    msgs = obj.get("messages")
    if isinstance(msgs, list):
        for mi in msgs:
            if not isinstance(mi, dict):
                continue
            role = str(mi.get("role") or "")

            if role == "assistant":
                tcs = mi.get("tool_calls")
                if isinstance(tcs, list):
                    kept: List[Any] = []
                    for tc in tcs:
                        if not isinstance(tc, dict):
                            continue
                        if "function" in tc:
                            tc["function_call"] = tc.pop("function")
                        fc = tc.get("function_call")
                        name = ""
                        if isinstance(fc, dict):
                            name = str(fc.get("name") or "")
                        if name.strip():
                            kept.append(tc)
                    if not kept:
                        mi.pop("tool_calls", None)
                    else:
                        mi["tool_calls"] = kept

            content = mi.get("content")
            if isinstance(content, str):
                mi["content"] = [{"type": "text", "text": content}]

    # model -> config_name + model_name
    raw_model = obj.get("model")
    model = str(raw_model).strip() if isinstance(raw_model, str) else ""
    if not model:
        model = default_model or DEFAULT_MODEL
    config_name, model_name = model_config(model)

    normalize_tool_choice(obj)
    normalize_tools(obj)

    # llm_utils_chat 必需字段
    obj["config_name"] = config_name
    obj["model_name"] = model_name
    obj["stream"] = True
    obj["function"] = FUNCTION
    obj["max_tokens"] = 4096
    obj["conversation_id"] = gen_uuid_like()
    obj["user_id"] = uid
    obj["session_id"] = gen_uuid_like()
    obj["device_id"] = device_id
    obj["machine_id"] = machine_id
    obj["project_id"] = gen_uuid_like()
    obj["workspace_id"] = "e04cdd"
    obj["prompt_max_tokens"] = 168000
    obj["mode"] = "FunctionCall"
    obj["ide_version"] = IDE_VERSION
    obj["ide_version_code"] = IDE_VERSION_CODE
    obj["app_id"] = APP_ID
    obj["package_type"] = "stable_cn"

    try:
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except Exception:
        return src


# --------------------------------------------------------------------------
# 错误分类（mod.rs）
# --------------------------------------------------------------------------

class ErrKind:
    """上游错误类别（与参考项目 cooldown 状态机对齐）。"""

    NONE = "none"
    PLAN_LIMIT = "PlanLimit"
    SOFT_RATE = "SoftRate"
    SESSION_DEAD = "SessionDead"
    NOT_FOUND = "NotFound"
    SERVER = "Server"
    CLIENT = "Client"

    #: 类别 -> 冷却秒数
    COOLDOWN = {
        "none": 0,
        "PlanLimit": 12 * 3600,
        "SoftRate": 60,
        "SessionDead": 24 * 3600,
        "NotFound": 60,
        "Server": 10 * 60,
        "Client": 10 * 60,
    }

    @classmethod
    def cooldown_seconds(cls, kind: str) -> int:
        return cls.COOLDOWN.get(kind, 0)

    @classmethod
    def is_cooldown(cls, kind: str) -> bool:
        return cls.cooldown_seconds(kind) > 0


def classify_error(status: int, body: str) -> str:
    """按 HTTP 状态码 + body 判定错误类别。"""
    text = body or ""
    low = text.lower()
    if '"code":1005' in text or ("1005" in text and "plan" in low):
        return ErrKind.PLAN_LIMIT
    if status == 401:
        return ErrKind.SESSION_DEAD
    if status == 429:
        return ErrKind.SOFT_RATE
    if status == 404:
        return ErrKind.NOT_FOUND
    if status >= 500:
        return ErrKind.SERVER
    if status >= 400:
        return ErrKind.CLIENT
    return ErrKind.NONE


def classify_solo_error(code: int, msg: str) -> str:
    """按 SOLO 业务错误码 + message 判定（流内 error 事件）。"""
    low = (msg or "").lower()
    if code == 1005 or "plan" in low:
        return ErrKind.PLAN_LIMIT
    # 4001 模型配置不存在 —— 模型问题而非账号问题，不冷却
    if code == 4001 or "model config is empty" in low:
        return ErrKind.NONE
    if code == 4008 or "quota" in low or "exceeded" in low or "rate" in low:
        return ErrKind.SOFT_RATE
    if code == 401:
        return ErrKind.SESSION_DEAD
    if code == 429:
        return ErrKind.SOFT_RATE
    if code == 404:
        return ErrKind.NOT_FOUND
    if code >= 500:
        return ErrKind.SERVER
    if code >= 400:
        return ErrKind.CLIENT
    return ErrKind.SERVER


# --------------------------------------------------------------------------
# SSE 解析（sse.rs）
# --------------------------------------------------------------------------

class SoloEvent:
    """SOLO SSE 单事件。"""

    __slots__ = (
        "event",
        "response",
        "reasoning",
        "tool_calls",
        "usage",
        "finish_reason",
        "error_code",
        "error_message",
    )

    def __init__(self, event: str) -> None:
        self.event = event
        self.response = ""
        self.reasoning = ""
        self.tool_calls: Optional[Any] = None
        self.usage: Optional[Any] = None
        self.finish_reason = ""
        self.error_code: Optional[int] = None
        self.error_message = ""


def parse_solo_line(event: str, data: str) -> Optional[SoloEvent]:
    """解析一个完整的 SSE 事件块。JSON 解析失败返回 None。"""
    ev = SoloEvent((event or "").strip())
    if not data:
        return ev
    try:
        raw = json.loads(data)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return ev

    name = ev.event
    if name in ("output", "thought"):
        # create_agent_task 用 event:thought，llm_utils_chat 用 event:output
        resp = raw.get("response")
        if not isinstance(resp, str) and name == "thought":
            resp = raw.get("thought")
        if isinstance(resp, str):
            ev.response = resp
        rc = raw.get("reasoning_content")
        if isinstance(rc, str):
            ev.reasoning = rc
        tc = raw.get("tool_calls")
        if tc is not None:
            ev.tool_calls = tc
    elif name == "token_usage":
        ev.usage = raw
    elif name == "done":
        fr = raw.get("finish_reason")
        if isinstance(fr, str):
            ev.finish_reason = fr
    elif name == "turn_completion":
        ev.finish_reason = "stop"
    elif name == "error":
        code = raw.get("code")
        ev.error_code = int(code) if isinstance(code, (int, float)) else None
        msg = raw.get("message")
        ev.error_message = msg if isinstance(msg, str) else ""
    return ev


class SoloSseParser:
    """跨行累积的 SSE 解析器（对应 sse.rs SseState + scan_line）。"""

    __slots__ = ("event", "data")

    def __init__(self) -> None:
        self.event = ""
        self.data = ""

    def reset(self) -> None:
        self.event = ""
        self.data = ""

    def feed(self, line: str) -> Optional[SoloEvent]:
        """喂入一行（已 trim_end），空行时返回解析出的事件。"""
        if line == "":
            if not self.event:
                self.reset()
                return None
            ev = parse_solo_line(self.event, self.data)
            self.reset()
            return ev
        if line.startswith("event:"):
            self.event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            self.data += line[len("data:"):]
        return None

    def feed_bytes(self, data: bytes) -> Iterator[SoloEvent]:
        """把原始字节流按行喂入，逐事件产出（自动处理 UTF-8 跨行）。"""
        for raw in data.split(b"\n"):
            try:
                line = raw.decode("utf-8", "replace").rstrip()
            except Exception:
                continue
            ev = self.feed(line)
            if ev is not None:
                yield ev


def _convert_tool_calls(tc: Any) -> List[Any]:
    """SOLO tool_calls -> OpenAI tool_calls（去掉 namespace/partial_arguments）。"""
    out: List[Any] = []
    if not isinstance(tc, list):
        return out
    for call in tc:
        if not isinstance(call, dict):
            continue
        c = dict(call)
        fc = c.pop("function_call", None)
        if fc is not None:
            c["function"] = fc
        fn = c.get("function")
        if isinstance(fn, dict):
            clean = dict(fn)
            clean.pop("namespace", None)
            clean.pop("partial_arguments", None)
            c["function"] = clean
        out.append(c)
    return out


def _chunk(chat_id: str, delta: Dict[str, Any], finish: str, usage: Optional[Any]) -> str:
    """构造一条 OpenAI SSE chunk。"""
    choice: Dict[str, Any] = {"index": 0, "delta": delta}
    if finish:
        choice["finish_reason"] = finish
    chunk: Dict[str, Any] = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": now_ts(),
        "model": "",
        "choices": [choice],
    }
    if usage is not None:
        chunk["usage"] = usage
    return "data: %s\n\n" % json.dumps(chunk, ensure_ascii=False)


def stream_convert(
    lines: Iterable[str],
    chat_id: str,
    emit: Callable[[str], None],
) -> Optional[Tuple[int, str]]:
    """SOLO SSE -> OpenAI SSE chunks，逐条通过 ``emit`` 输出。

    返回 ``(code, message)`` 表示流内出现 error 事件；否则返回 ``None``。
    """
    parser = SoloSseParser()
    pending_usage: Optional[Any] = None
    saw_done = False
    error_info: Optional[Tuple[int, str]] = None

    for line in lines:
        ev = parser.feed(line.rstrip() if line else "")
        if ev is None:
            continue
        name = ev.event
        if name in ("output", "thought"):
            delta: Dict[str, Any] = {}
            if ev.response:
                delta["content"] = ev.response
            if ev.reasoning:
                delta["reasoning_content"] = ev.reasoning
            tcs = _convert_tool_calls(ev.tool_calls) if ev.tool_calls is not None else []
            if tcs:
                delta["tool_calls"] = tcs
            if delta:
                emit(_chunk(chat_id, delta, "", pending_usage))
        elif name == "token_usage":
            pending_usage = ev.usage if ev.usage is not None else {}
        elif name in ("done", "turn_completion"):
            emit(_chunk(chat_id, {}, ev.finish_reason, pending_usage))
            emit("data: [DONE]\n\n")
            saw_done = True
        elif name == "error":
            code = ev.error_code if ev.error_code is not None else 0
            error_info = (code, ev.error_message)
            err_chunk = {
                "error": {
                    "message": ev.error_message,
                    "type": "api_error",
                    "code": code,
                }
            }
            emit("data: %s\n\n" % json.dumps(err_chunk, ensure_ascii=False))
            emit("data: [DONE]\n\n")
            saw_done = True

    if not saw_done:
        emit("data: [DONE]\n\n")
    return error_info


def aggregate(lines: Iterable[str], chat_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[int, str]]]:
    """非流式聚合：读完整个 SOLO SSE，合成单个 ``chat.completion``。"""
    parser = SoloSseParser()
    content: List[str] = []
    reasoning: List[str] = []
    finish_reason = "stop"
    usage: Optional[Any] = None
    error_info: Optional[Tuple[int, str]] = None

    for line in lines:
        ev = parser.feed(line.rstrip() if line else "")
        if ev is None:
            continue
        name = ev.event
        if name in ("output", "thought"):
            content.append(ev.response)
            reasoning.append(ev.reasoning)
        elif name == "token_usage":
            usage = ev.usage if ev.usage is not None else {}
        elif name in ("done", "turn_completion"):
            if ev.finish_reason:
                finish_reason = ev.finish_reason
        elif name == "error":
            error_info = (
                ev.error_code if ev.error_code is not None else 0,
                ev.error_message,
            )

    if error_info is not None:
        return None, error_info

    message: Dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content),
    }
    reasoning_text = "".join(reasoning)
    if reasoning_text:
        message["reasoning_content"] = reasoning_text

    resp: Dict[str, Any] = {
        "id": chat_id,
        "object": "chat.completion",
        "created": now_ts(),
        "model": "",
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason}
        ],
    }
    if usage is not None:
        resp["usage"] = usage
    return resp, None


# --------------------------------------------------------------------------
# 上游 HTTP 请求（routes.rs make_upstream_request）
# --------------------------------------------------------------------------

class UpstreamError(Exception):
    """上游返回了 HTTP 错误状态（携带 code / body）。"""

    def __init__(self, status: int, body: str) -> None:
        super().__init__("upstream status=%s" % status)
        self.status = status
        self.body = body


class UpstreamTransportError(Exception):
    """传输层错误（DNS/超时/TLS 等），已带中文诊断信息。"""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    """禁用所有代理的 opener（避免经系统代理形成 127.0.0.1 回环）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    return ctx


def build_upstream_headers(
    jwt: str,
    device_id: str,
    machine_id: str,
    host: Optional[str] = None,
) -> Dict[str, str]:
    """构造 28 个上游请求头（顺序与参考实现一致，便于比对）。"""
    base = host or AGENT_HOST
    referer = "%s%s" % (REFERER_BASE, EP_LLM_CHAT)
    trace_id = "00-%s-%s-01" % (uuid_like_id(), uuid_like_id())
    request_id = "req_%s" % uuid_like_id()
    return {
        "content-type": "application/json",
        "accept": "*/*",
        "accept-encoding": ACCEPT_ENCODING,
        "user-agent": USER_AGENT,
        "x-ide-token": jwt,
        "x-app-id": APP_ID,
        "x-app-version": "default",
        "x-app-version-code": IDE_VERSION_CODE,
        "x-ide-version": IDE_VERSION,
        "x-ide-version-code": IDE_VERSION_CODE,
        "x-ide-version-type": "stable",
        "x-device-type": DEVICE_TYPE,
        "x-device-brand": DEVICE_BRAND,
        "x-device-cpu": DEVICE_CPU,
        "x-device-id": device_id,
        "x-machine-id": machine_id,
        "x-os-version": OS_VERSION,
        "request-traffic-type": "prod",
        "package-type": "stable_cn",
        "x-lgw-req-sdk-type": "3",
        "x-lscbd-aid": "787976",
        "x-lscbd-platform": "windows",
        "x-ss-dp": "787976",
        "app-version": IDE_VERSION,
        "x-custom-trace-id": trace_id[:16],
        "x-flow-traceparent": "04-%s-%s-01" % (trace_id[3:35], uuid_like_id()),
        "x-tt-trace-id": trace_id,
        "x-request-id": request_id,
        "referer": referer,
    }


def _classify_transport_error(exc: BaseException, host: Optional[str] = None) -> str:
    """把底层异常翻译成中文诊断（对应 routes.rs 的 err_str 分支）。"""
    base = host or AGENT_HOST
    text = ("%s" % exc).lower()
    if "dns" in text or "resolve" in text or "name resolution" in text or "getaddrinfo" in text:
        return "DNS解析失败（%s 无法解析），请检查网络或代理设置: %s" % (base, exc)
    if "timed out" in text or "timeout" in text:
        return "连接超时（%s 10秒内未响应），请检查网络连通性: %s" % (base, exc)
    if "tls" in text or "certificate" in text or "ssl" in text or "cert verify" in text:
        return "TLS证书验证失败: %s" % exc
    return "传输错误: %s" % exc


def make_upstream_request(
    jwt: str,
    device_id: str,
    machine_id: str,
    body: bytes,
    host: Optional[str] = None,
) -> Any:
    """向 ``llm_utils_chat`` 发起请求，返回可迭代的响应对象。

    成功返回 ``http.client.HTTPResponse``（调用方负责 ``close()``）；
    HTTP 错误抛 :class:`UpstreamError`，传输错误抛 :class:`UpstreamTransportError`。
    """
    base = host or AGENT_HOST
    url = "%s%s" % (base, EP_LLM_CHAT)
    headers = build_upstream_headers(jwt, device_id, machine_id, base)

    # 临时清空代理环境变量，双保险（opener 本身已禁用代理）
    saved: Dict[str, Optional[str]] = {}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        if name in os.environ:
            saved[name] = os.environ.pop(name)
    saved["NO_PROXY"] = os.environ.get("NO_PROXY")
    os.environ["NO_PROXY"] = "*"

    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        opener = _no_proxy_opener()
        resp = opener.open(req, timeout=CONNECT_TIMEOUT)
        return resp
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", "replace")
        except Exception:
            err_body = ""
        raise UpstreamError(int(e.code), err_body)
    except Exception as e:
        raise UpstreamTransportError(_classify_transport_error(e, base))
    finally:
        for name, val in saved.items():
            if name == "NO_PROXY":
                if val is None:
                    os.environ.pop("NO_PROXY", None)
                else:
                    os.environ["NO_PROXY"] = val
            elif val is not None:
                os.environ[name] = val


def iter_upstream_lines(resp: Any) -> Iterator[str]:
    """把上游响应逐行产出（SSE 需要，不能一次性 read）。"""
    try:
        for raw in resp:
            yield raw.decode("utf-8", "replace")
    finally:
        try:
            resp.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 模型清单（routes.rs static_models）
# --------------------------------------------------------------------------

STATIC_MODEL_NAMES = [
    "doubao-seed-2.1-pro",
    "doubao-seed-2.1-turbo",
    "doubao-seed-2.0-code",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "glm-5.2",
    "glm-5.3",
    "glm-5-turbo",
    "glm-5",
    "kimi-k2.7-code",
    "kimi-k3",
    "kimi-k2.6",
    "minimax-m3",
    "qwen-3.7-plus",
    "sagitta",
    "aquila",
]


def static_models() -> List[Dict[str, Any]]:
    """上游模型清单（OpenAI /v1/models 格式）。"""
    return [
        {
            "id": name,
            "object": "model",
            "created": 1753600000,
            "owned_by": "trae-solo",
            "context_length": 131072,
        }
        for name in STATIC_MODEL_NAMES
    ]


def model_ids() -> List[str]:
    return list(STATIC_MODEL_NAMES)


def openai_error(code: str, msg: str) -> Dict[str, Any]:
    """构造 OpenAI 风格错误体。"""
    return {"error": {"message": msg, "type": "api_error", "code": code}}


def safe_slice(s: str, n: int) -> str:
    return s[:n] if len(s) > n else s


def diagnose_summary(diag: Iterable[Dict[str, Any]], host: Optional[str] = None) -> str:
    """把池诊断压成一行摘要（写入日志便于排查）。"""
    parts: List[str] = []
    now = now_ts()
    for d in diag:
        uid = str(d.get("uid") or "")
        credits = d.get("credits")
        credits_str = ("%.0f" % credits) if isinstance(credits, (int, float)) else "N/A"
        until = int(d.get("until") or 0)
        cd_str = (",cd=%ds" % max(0, until - now)) if until > 0 else ""
        exp = d.get("credits_expire_at")
        exp_str = (",exp=%s" % exp) if isinstance(exp, (int, float)) and exp > 0 else ""
        dis_str = ",DIS" if d.get("disabled") else ""
        parts.append(
            "%s(%s:%s,cr=%s%s%s%s)"
            % (d.get("name") or "?", uid[:8], d.get("reason") or "?", credits_str, cd_str, exp_str, dis_str)
        )
    return ", ".join(parts)


__all__ = [
    "AGENT_HOST",
    "EP_LLM_CHAT",
    "APP_ID",
    "IDE_VERSION",
    "IDE_VERSION_CODE",
    "FUNCTION",
    "DEFAULT_MODEL",
    "REFERER_BASE",
    "MAX_BODY_BYTES",
    "ErrKind",
    "SoloEvent",
    "SoloSseParser",
    "UpstreamError",
    "UpstreamTransportError",
    "aggregate",
    "build_upstream_headers",
    "classify_error",
    "classify_solo_error",
    "diagnose_summary",
    "gen_uuid_like",
    "iter_upstream_lines",
    "machine_id_for",
    "make_upstream_request",
    "model_config",
    "model_ids",
    "now_ts",
    "openai_error",
    "parse_solo_line",
    "prepare_llm_chat_body",
    "safe_slice",
    "seeded_hex",
    "static_models",
    "stream_convert",
    "uuid_like_id",
]
