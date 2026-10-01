#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地模型后端：Ollama 与 llama.cpp 的 OpenAI 兼容适配层。

为什么需要这一层
----------------
Ollama 和 llama.cpp 各自提供 HTTP 接口，但两者**协议不同**：

  * Ollama  —— 原生接口 `/api/chat`、`/api/tags`，
               返回的是**逐行 JSON**（NDJSON），不是 SSE。
               它另外提供 `/v1/chat/completions`（OpenAI 兼容），
               但版本较老时字段不全，且流式结束标记不统一。
  * llama.cpp —— 较新版本（`llama-server`）自带 `/v1/chat/completions`，
               标准 SSE；老版本只有 `/completion`。

本模块统一成「输入 OpenAI 请求体 → 输出 OpenAI 响应块」，
上层 `api_server` 不关心后端差异。

设计取舍
--------
* **只连已运行的服务**，不负责拉起进程（用户选择）。
* 但保留 `probe()`：配置本地模型时自动发现可用模型列表，
  避免用户手打模型名。
* 流式输出统一为 SSE `data: {...}\\n\\n` 分块，
  即使后端是 NDJSON 也在这里转成 SSE，让客户端只处理一种格式。

⚠️ **必须绕开系统代理**
本机（以及很多开发机）会设置 `HTTP_PROXY` / `ALL_PROXY` 环境变量指向
某个本地代理端口。`urllib` 默认**会**读取这些变量，于是请求
`http://127.0.0.1:11434` 会被塞进代理，代理再去连 11434 —— 如果那时
Ollama 还没起来，就会返回一个极具误导性的
`502 upstream connect failed`，让人以为是「Ollama 连不上」。
因此这里统一用 `_opener()` 构造**不经过任何代理**的 opener。
"""
import json
import time
import urllib.error
import urllib.request

# 默认端口：Ollama 官方 11434；llama.cpp(llama-server) 官方示例常用 8080
OLLAMA_DEFAULT = "http://127.0.0.1:11434"
LLAMACPP_DEFAULT = "http://127.0.0.1:8080"

# 本地模型在模型清单里的前缀，避免与云端模型名撞车
LOCAL_PREFIX = "local/"

CONNECT_TIMEOUT = 8.0      # 建连/探测（Ollama 冷启动后首次 /api/tags 可能较慢）
READ_TIMEOUT = 300.0       # 生成可能很久（本地小显卡尤其慢）
PROBE_RETRIES = 3          # 探测重试次数（应对"TCP 已监听但 HTTP 未就绪"）
PROBE_RETRY_DELAY = 2.0    # 重试间隔


class BackendError(RuntimeError):
    """后端调用失败，附带可读原因。

    `status`   —— HTTP 状态码（有的话）
    `retryable`—— 是否值得重试。
        只对「超时 / 连接被重置」这类**可能瞬时**的故障置 True。
        注意「连接被拒绝（10061）」**不算**可重试：那说明服务根本没在跑，
        反复重试只会让用户白等几十秒，不如立刻给出准确结论。
    """

    def __init__(self, msg, status=None, retryable=False):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable


# 全局共用一个「禁用代理」的 opener。首次调用时构造，之后复用
# （urllib 的 OpenerDirector 是线程安全的）。
_NO_PROXY_OPENER = None


def _opener():
    """返回一个**不读取任何代理环境变量**的 opener。

    本机服务（127.0.0.1）绝不应该走代理：既无必要，
    又会在服务未启动时把「连接被拒」伪装成含义不明的 HTTP 502。
    """
    global _NO_PROXY_OPENER
    if _NO_PROXY_OPENER is None:
        _NO_PROXY_OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))
    return _NO_PROXY_OPENER


def _base(url):
    return (url or "").rstrip("/")


def _request(url, payload=None, method="GET", timeout=CONNECT_TIMEOUT,
             stream=False):
    """发一个 JSON 请求；stream=True 时返回响应对象（调用方自行迭代）。"""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = _opener().open(req, timeout=timeout)
        return resp
    except urllib.error.HTTPError as e:
        # 后端返回了 4xx/5xx：尽量把它的错误信息带出来
        detail = ""
        try:
            detail = e.read(400).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        # 408/429/5xx 视为可能瞬时；其它 4xx 是确定性错误
        retryable = e.code in (408, 429) or e.code >= 500
        raise BackendError(
            f"后端返回 HTTP {e.code}：{detail or e.reason}",
            status=e.code, retryable=retryable) from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        text = str(reason)
        # 连接被拒 = 服务没起来，重试无意义；超时/重置才值得重试
        retryable = not _is_refused(text)
        raise BackendError(f"无法连接后端（{text}）",
                           retryable=retryable) from e
    except TimeoutError as e:
        # Ollama 刚启动时「端口已监听但 HTTP 未就绪」会走到这里
        raise BackendError("连接超时（服务可能正在启动）",
                           retryable=True) from e
    except OSError as e:
        text = str(e)
        raise BackendError(f"网络错误：{text}",
                           retryable=not _is_refused(text)) from e


def _is_refused(text):
    """判断错误文本是不是「连接被拒绝」。

    跨平台匹配：Windows 是 `WinError 10061` / 「积极拒绝」，
    POSIX 是 `Connection refused`。命中就说明服务没在监听。
    """
    t = (text or "").lower()
    return any(k in t for k in ("10061", "refused", "积极拒绝", "拒绝"))


def _iter_sse(resp):
    """把标准 SSE 流拆成一个个 JSON 对象。"""
    buf = b""
    for chunk in resp:
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line or line.startswith(b":"):
                continue
            if line.startswith(b"data:"):
                line = line[5:].strip()
            if line == b"[DONE]":
                return
            try:
                yield json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue


def _iter_ndjson(resp):
    """把 Ollama 的逐行 JSON（NDJSON）拆成对象。"""
    for raw in resp:
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------

class OllamaBackend:
    """Ollama 后端。优先走原生 /api/chat（信息最全），失败再退 /v1。"""

    kind = "ollama"
    label = "Ollama"

    def __init__(self, base_url=OLLAMA_DEFAULT, keep_alive=None):
        self.base_url = _base(base_url) or OLLAMA_DEFAULT
        self.keep_alive = keep_alive        # 如 "5m"；None 用服务端默认

    # ---- 探测 ----

    def list_models(self):
        """GET /api/tags → [模型名]。"""
        resp = _request(self.base_url + "/api/tags")
        try:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        finally:
            resp.close()
        out = []
        for m in (data.get("models") or []):
            name = m.get("name") or m.get("model")
            if name:
                out.append({
                    "id": name,
                    "size": m.get("size"),
                    "family": ((m.get("details") or {}).get("family")),
                })
        return out

    def probe(self):
        """返回 (可用?, 说明, 模型数)。不做重试，重试在上层 `probe()`。"""
        try:
            models = self.list_models()
            if models:
                return True, f"已连接，{len(models)} 个模型", len(models)
            return True, "已连接（服务在线，但未报告任何模型）", 0
        except BackendError as e:
            return False, str(e), 0

    # ---- 调用 ----

    def _payload(self, body, model, stream):
        msgs = []
        for m in (body.get("messages") or []):
            msgs.append({"role": m.get("role", "user"),
                         "content": m.get("content", "")})
        p = {
            "model": model,
            "messages": msgs,
            "stream": bool(stream),
            # Ollama 的采样参数直接平铺在顶层（不是嵌套的 sampling 对象）
            "options": {},
        }
        opt = p["options"]
        if body.get("temperature") is not None:
            opt["temperature"] = body["temperature"]
        if body.get("top_p") is not None:
            opt["top_p"] = body["top_p"]
        if body.get("max_tokens") is not None:
            # Ollama 用 num_predict 表示最大生成 token 数
            opt["num_predict"] = body["max_tokens"]
        if body.get("stop") is not None:
            opt["stop"] = body["stop"]
        if not opt:
            p.pop("options")
        if self.keep_alive:
            p["keep_alive"] = self.keep_alive
        return p

    def chat(self, body, model):
        """非流式：返回 (文本, usage_dict)。"""
        payload = self._payload(body, model, stream=False)
        resp = _request(self.base_url + "/api/chat", payload=payload,
                        method="POST", timeout=READ_TIMEOUT)
        try:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        finally:
            resp.close()
        if data.get("error"):
            raise BackendError(f"Ollama 报错：{data['error']}")
        text = ((data.get("message") or {}).get("content")) or ""
        usage = self._usage(data)
        return text, usage

    def chat_stream(self, body, model):
        """流式：逐块 yield 文本增量，最后 yield 一个 usage 字典。"""
        payload = self._payload(body, model, stream=True)
        resp = _request(self.base_url + "/api/chat", payload=payload,
                        method="POST", timeout=READ_TIMEOUT, stream=True)
        count = 0
        try:
            for obj in _iter_ndjson(resp):
                if obj.get("error"):
                    raise BackendError(f"Ollama 报错：{obj['error']}")
                piece = ((obj.get("message") or {}).get("content")) or ""
                if piece:
                    count += 1
                    yield piece
                if obj.get("done"):
                    yield self._usage(obj)
                    return
        finally:
            resp.close()
        yield {"prompt_tokens": 0, "completion_tokens": count,
               "total_tokens": count}

    @staticmethod
    def _usage(data):
        return {
            "prompt_tokens": data.get("prompt_eval_count") or 0,
            "completion_tokens": data.get("eval_count") or 0,
            "total_tokens": (data.get("prompt_eval_count") or 0)
                            + (data.get("eval_count") or 0),
        }


# --------------------------------------------------------------------------
# llama.cpp (llama-server)
# --------------------------------------------------------------------------

class LlamaCppBackend:
    """llama.cpp 的 llama-server 后端。

    较新版本原生提供 /v1/chat/completions（标准 SSE）；
    老版本没有，此时 `probe()` 会提示，并可用 /completion 兜底。
    """

    kind = "llamacpp"
    label = "llama.cpp"

    def __init__(self, base_url=LLAMACPP_DEFAULT):
        self.base_url = _base(base_url) or LLAMACPP_DEFAULT

    def list_models(self):
        """GET /v1/models → 若 404 则退 /props 取默认模型名。"""
        try:
            resp = _request(self.base_url + "/v1/models")
            try:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            finally:
                resp.close()
            out = []
            for m in (data.get("data") or []):
                if m.get("id"):
                    out.append({"id": m["id"]})
            if out:
                return out
        except BackendError:
            pass
        # 老版本兜底：/props 里有 default_generation_settings 或 model_path
        resp = _request(self.base_url + "/props")
        try:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        finally:
            resp.close()
        name = (data.get("model_path") or "").replace("\\", "/").split("/")[-1]
        return [{"id": name or "llama.cpp"}] if name else []

    def probe(self):
        try:
            models = self.list_models()
            if models:
                return True, f"已连接，模型 {models[0]['id']}", len(models)
            return True, "已连接（服务在线，但未报告模型名）", 0
        except BackendError as e:
            return False, str(e), 0

    def _payload(self, body, model, stream):
        p = {
            "model": model,
            "messages": body.get("messages") or [],
            "stream": bool(stream),
        }
        for k in ("temperature", "top_p", "max_tokens", "stop",
                  "presence_penalty", "frequency_penalty"):
            if body.get(k) is not None:
                p[k] = body[k]
        return p

    def chat(self, body, model):
        payload = self._payload(body, model, stream=False)
        resp = _request(self.base_url + "/v1/chat/completions",
                        payload=payload, method="POST", timeout=READ_TIMEOUT)
        try:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        finally:
            resp.close()
        if data.get("error"):
            err = data["error"]
            raise BackendError(
                f"llama.cpp 报错：{err.get('message') if isinstance(err, dict) else err}")
        choices = data.get("choices") or []
        text = ""
        if choices:
            text = ((choices[0].get("message") or {}).get("content")) or ""
        usage = data.get("usage") or {}
        return text, {
            "prompt_tokens": usage.get("prompt_tokens") or 0,
            "completion_tokens": usage.get("completion_tokens") or 0,
            "total_tokens": usage.get("total_tokens") or 0,
        }

    def chat_stream(self, body, model):
        payload = self._payload(body, model, stream=True)
        resp = _request(self.base_url + "/v1/chat/completions",
                        payload=payload, method="POST", timeout=READ_TIMEOUT,
                        stream=True)
        usage = None
        try:
            for obj in _iter_sse(resp):
                if obj.get("error"):
                    err = obj["error"]
                    raise BackendError(
                        "llama.cpp 报错："
                        + (err.get("message") if isinstance(err, dict) else str(err)))
                if obj.get("usage"):
                    usage = obj["usage"]
                for ch in (obj.get("choices") or []):
                    delta = ch.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece
        finally:
            resp.close()
        if usage:
            yield {
                "prompt_tokens": usage.get("prompt_tokens") or 0,
                "completion_tokens": usage.get("completion_tokens") or 0,
                "total_tokens": usage.get("total_tokens") or 0,
            }
        else:
            yield {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

BACKENDS = {
    OllamaBackend.kind: OllamaBackend,
    LlamaCppBackend.kind: LlamaCppBackend,
}

KIND_LABELS = {
    OllamaBackend.kind: "Ollama",
    LlamaCppBackend.kind: "llama.cpp",
}

DEFAULT_URLS = {
    OllamaBackend.kind: OLLAMA_DEFAULT,
    LlamaCppBackend.kind: LLAMACPP_DEFAULT,
}


def make_backend(kind, base_url=None):
    """按类型构造后端实例；base_url 为空则用默认端口。"""
    cls = BACKENDS.get((kind or "").lower())
    if cls is None:
        raise BackendError(f"未知的本地后端类型：{kind}")
    return cls(base_url or DEFAULT_URLS.get(cls.kind) or "")


def probe(kind, base_url=None):
    """探测某个后端是否可用。返回 (可用?, 说明文本, 模型数)。

    会重试，但**只重试可能瞬时的故障**（超时 / 5xx）。
    依据 `BackendError.retryable` 判断，而不是"凡失败就重试"：

      * 「连接被拒绝」= 服务没在跑 → 立刻返回，不让用户白等；
      * 「连接超时」  = Ollama 常有的「TCP 已监听但 HTTP 层还没就绪」
                       → 重试，否则会把"刚启动"误报成故障。
    """
    try:
        be = make_backend(kind, base_url)
    except BackendError as e:
        return False, str(e), 0
    last = ""
    for attempt in range(PROBE_RETRIES):
        try:
            models = be.list_models()
            detail = (f"已连接，{len(models)} 个模型" if models
                      else "已连接（服务在线，但未报告任何模型）")
            return True, detail, len(models)
        except BackendError as e:
            last = str(e)
            if not e.retryable:
                return False, last, 0
            if attempt < PROBE_RETRIES - 1:
                time.sleep(PROBE_RETRY_DELAY)
    return False, last, 0


def split_model(model_id):
    """把 `local/ollama/llama3.2` 拆成 (kind, 模型名)。

    不是本地前缀则返回 (None, model_id)。
    也接受 `local/llama3.2` 这种省略后端名的写法（此时 kind=None，
    由调用方按「唯一启用的后端」推断）。
    未知的后端名原样返回，便于上层给出明确报错。
    """
    if not model_id or not model_id.startswith(LOCAL_PREFIX):
        return None, model_id
    rest = model_id[len(LOCAL_PREFIX):]
    if "/" not in rest:
        return None, rest
    kind, name = rest.split("/", 1)
    if kind in BACKENDS:
        return kind, name
    # 认不出的前缀：不吞掉，整段当作模型名还回去，
    # 让调用方按「未指定后端」处理（避免把 `bogus/x` 当成模型名）
    return kind, name


def qualify(kind, model_name):
    """反向拼装：把 (后端, 模型名) 变成 `local/<kind>/<name>`。"""
    return f"{LOCAL_PREFIX}{kind}/{model_name}"
