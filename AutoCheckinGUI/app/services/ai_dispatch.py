# -*- coding: utf-8 -*-
"""AI 调度器：本地优先的统一 AI 调用门面。

设计目标
--------
项目里所有「要问模型」的地方（失败诊断、日志解读、建议生成……）
都不应该各自去拼后端，而是统一走这里，于是得到三条一致的行为：

1. **本地优先**：只要任一本地后端（Ollama / llama.cpp）探测可用，
   所有 AI 调用默认走本地 **不消耗任何云端积分**。
2. **自动回落**：本地后端临时挂了 / 超时 / 报错，自动改走云端账号池，
   调用方不需要写 try/except 两套逻辑。
3. **可解释**：每次调用都返回「实际走了谁」，UI 能如实显示，
   不会让用户以为在用本地其实偷偷烧了积分。

与 `api_server` 的分工
---------------------
* `api_server` 是**对外**的 OpenAI 兼容 HTTP 服务（给别的软件用）。
* 本模块是**对内**的调用门面（本软件自己用）。

两者共用同一份后端配置（`LocalPool` 与账号池），所以 UI 里改一次配置，
对外服务与对内调度同时生效。

选择顺序
--------
```
force_local=True            -> 只用本地（失败即报错，不烧云端积分）
force_cloud=True            -> 只用云端
默认（自动）                 -> 本地（就绪时）-> 云端 -> 全失败则报错
```
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import local_backends as LB
from . import solo_upstream as SU

#: 单次调用的默认超时（本地模型可能较慢，给宽一点）
DEFAULT_TIMEOUT = 120.0

#: 本地探测结果的缓存时长（秒）—— 避免每次调用都探一遍
PROBE_TTL = 30.0

#: 默认系统提示词：让模型以「运维助手」的口吻回答
DEFAULT_SYSTEM_PROMPT = (
    "你是一名自动化运维助手，负责诊断「Trae / WorkBuddy 每日签到」脚本的故障。"
    "请用简体中文、简洁准确、条理清晰地回答，"
    "只依据给定材料推断，不要编造不存在的日志或接口。"
)


class Route:
    """一次调用的实际去向（用于 UI 如实展示）。"""

    LOCAL = "local"
    CLOUD = "cloud"
    NONE = "none"


class DispatchResult:
    """一次 AI 调用的结果。"""

    __slots__ = ("text", "route", "target", "usage", "error",
                 "local_attempted", "elapsed")

    def __init__(self, text="", route=Route.NONE, target="", usage=None,
                 error=None, local_attempted=False, elapsed=0.0):
        self.text = text
        self.route = route          # local | cloud | none
        self.target = target        # 具体后端说明，如 "Ollama · qwen2.5:7b"
        self.usage = usage or {}
        self.error = error
        self.local_attempted = local_attempted
        self.elapsed = elapsed

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error

    def describe(self) -> str:
        """给 UI 用的一句话描述。"""
        if not self.ok:
            return "调用失败：%s" % (self.error or "无可用后端")
        where = "本地模型" if self.route == Route.LOCAL else "云端账号池"
        return "%s · %s · %.1fs" % (where, self.target, self.elapsed)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text, "route": self.route, "target": self.target,
            "usage": self.usage, "error": self.error, "ok": self.ok,
            "local_attempted": self.local_attempted,
            "elapsed": round(self.elapsed, 3),
            "describe": self.describe(),
        }


class AIDispatcher:
    """AI 调用调度器：本地优先 + 自动回落云端。

    依赖注入而不是直接引用 `api_server`，避免模块循环导入：
    上层把 `LocalPool` 和一个「云端调用函数」传进来即可。
    """

    def __init__(self, local_pool=None, cloud_fn=None, settings_getter=None):
        """
        local_pool      : LocalPool 实例（提供 backend_for / enabled_kinds / rows）
        cloud_fn        : (messages, model, system) -> (text, info_dict) 的云端调用函数
                          返回 info 里可带 {"route":"cloud","target":...,"usage":...}
        settings_getter : () -> dict，读取用户偏好（prefer_local / 指定模型）
        """
        self._lock = threading.RLock()
        self._local = local_pool
        self._cloud_fn = cloud_fn
        self._settings = settings_getter or (lambda: {})
        # 本地可用性缓存：kind -> (ok, detail, checked_at)
        self._probe_cache: Dict[str, Tuple[bool, str, float]] = {}
        # 统计
        self.stats = {"local": 0, "cloud": 0, "failed": 0, "fallback": 0}

    # ---------------- 配置读取 ----------------

    def set_local_pool(self, local_pool):
        with self._lock:
            self._local = local_pool
            self._probe_cache.clear()

    def set_cloud_fn(self, fn):
        with self._lock:
            self._cloud_fn = fn

    def set_settings_getter(self, fn):
        with self._lock:
            self._settings = fn or (lambda: {})

    def _prefs(self) -> Dict[str, Any]:
        try:
            data = self._settings() or {}
        except Exception:  # noqa: BLE001
            data = {}
        return data if isinstance(data, dict) else {}

    # ---------------- 本地可用性 ----------------

    def local_ready(self, fresh=False) -> Tuple[Optional[str], str]:
        """返回 (可用的 kind, 说明)。没有任何本地后端可用时 kind 为 None。

        探测带 TTL 缓存：签到诊断这类连续调用不会每次都打后端。
        """
        lp = self._local
        if lp is None:
            return None, "未接入本地后端"
        kinds = lp.enabled_kinds()
        if not kinds:
            return None, "没有启用任何本地后端"

        now = time.time()
        for kind in kinds:
            with self._lock:
                cached = self._probe_cache.get(kind)
            if cached and not fresh and (now - cached[2]) < PROBE_TTL:
                ok, detail = cached[0], cached[1]
            else:
                row = lp.get_row(kind)
                ok, detail = False, "未探测"
                if row and row.get("ok"):
                    ok, detail = True, row.get("detail") or "已连接"
                else:
                    ok, detail, _models = lp.probe(kind)
                with self._lock:
                    self._probe_cache[kind] = (ok, detail, time.time())
            if ok:
                return kind, detail
        return None, "本地后端均不可用"

    def invalidate_probe(self):
        with self._lock:
            self._probe_cache.clear()

    def pick_local_model(self, kind) -> str:
        """挑一个本地模型名：优先用户指定的，否则取该后端第一个可用模型。"""
        prefs = self._prefs()
        want = str(prefs.get("local_model") or "").strip()
        lp = self._local
        if lp is None:
            return ""
        if want:
            # 允许三种写法：完整 local/<kind>/<name>、裸 <name>
            k, name = LB.split_model(want)
            if k == kind and name:
                return name
            if k is None and want:
                return want
        row = lp.get_row(kind)
        models = list((row or {}).get("models") or [])
        return models[0] if models else ""

    # ---------------- 调用 ----------------

    def ask(self, prompt, system=None, history=None, model=None,
            force_local=False, force_cloud=False, temperature=None,
            max_tokens=None, timeout=None) -> DispatchResult:
        """问一个模型，返回 DispatchResult。

        prompt    : 用户内容（字符串）
        system    : 系统提示词（默认用 DEFAULT_SYSTEM_PROMPT）
        history   : 额外的历史消息 [{"role":..., "content":...}, ...]
        model     : 指定模型名。`local/...` 前缀强制走本地；
                    云端名强制走云端；为空则按下面的策略自动选。
        """
        prefs = self._prefs()
        prefer_local = bool(prefs.get("prefer_local", True))
        timeout = timeout or DEFAULT_TIMEOUT

        want_local = force_local or (
            not force_cloud and model and str(model).startswith(LB.LOCAL_PREFIX))
        want_cloud_only = force_cloud or (
            not force_local and model and not str(model).startswith(LB.LOCAL_PREFIX)
            and model)

        messages = self._build_messages(prompt, system, history)
        t0 = time.time()
        local_attempted = False
        local_err = ""

        # ---------- 本地优先 ----------
        if not want_cloud_only and (want_local or prefer_local or not model):
            kind, detail = self.local_ready()
            if kind:
                local_attempted = True
                res = self._call_local(kind, messages, model, temperature,
                                       max_tokens, timeout)
                if res.ok:
                    res.elapsed = time.time() - t0
                    with self._lock:
                        self.stats["local"] += 1
                    return res
                local_err = res.error or "本地调用失败"
                with self._lock:
                    self.stats["fallback"] += 1
                # 本地刚失败 -> 让下次探测重新判断，避免一直踩同一个坑
                self.invalidate_probe()
                if force_local or want_local:
                    # 明确要求本地时不偷偷烧云端积分
                    res.elapsed = time.time() - t0
                    with self._lock:
                        self.stats["failed"] += 1
                    return res
            elif force_local or want_local:
                with self._lock:
                    self.stats["failed"] += 1
                return DispatchResult(route=Route.NONE, error=detail,
                                      local_attempted=False,
                                      elapsed=time.time() - t0)

        # ---------- 云端 ----------
        if not force_local:
            res = self._call_cloud(messages, model, timeout)
            if res.ok:
                res.local_attempted = local_attempted
                res.elapsed = time.time() - t0
                with self._lock:
                    self.stats["cloud"] += 1
                return res
            if not local_err:
                local_err = res.error or "云端调用失败"

        with self._lock:
            self.stats["failed"] += 1
        return DispatchResult(route=Route.NONE,
                              error=local_err or "没有可用的 AI 后端",
                              local_attempted=local_attempted,
                              elapsed=time.time() - t0)

    # ---------------- 内部：本地 ----------------

    @staticmethod
    def _build_messages(prompt, system, history) -> List[Dict[str, str]]:
        msgs: List[Dict[str, str]] = []
        sys_text = system if system is not None else DEFAULT_SYSTEM_PROMPT
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})
        for h in (history or []):
            if isinstance(h, dict) and h.get("role") and h.get("content"):
                msgs.append({"role": h["role"], "content": h["content"]})
        msgs.append({"role": "user", "content": str(prompt or "")})
        return msgs

    def _call_local(self, kind, messages, model, temperature, max_tokens,
                    timeout) -> DispatchResult:
        lp = self._local
        if lp is None:
            return DispatchResult(route=Route.NONE, error="未接入本地后端")
        # 模型名归一：'local/ollama/xxx' 只用后面的 xxx
        name = ""
        if model and str(model).startswith(LB.LOCAL_PREFIX):
            k, n = LB.split_model(model)
            name = n if k == kind else ""
        if not name:
            name = self.pick_local_model(kind)
        if not name:
            return DispatchResult(route=Route.NONE,
                                  error="%s 上没有可用模型" % kind)
        try:
            be = lp.backend_for(kind)
        except Exception as e:  # noqa: BLE001
            return DispatchResult(route=Route.NONE, error="本地后端不可用：%s" % e)

        body = {"messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        try:
            text, usage = be.chat(body, name)
        except LB.BackendError as e:
            return DispatchResult(
                route=Route.LOCAL,
                target="%s · %s" % (LB.KIND_LABELS.get(kind, kind), name),
                error="本地后端报错：%s" % e)
        except Exception as e:  # noqa: BLE001
            return DispatchResult(
                route=Route.LOCAL,
                target="%s · %s" % (LB.KIND_LABELS.get(kind, kind), name),
                error="本地调用异常：%r" % e)

        with self._lock:
            self._local.count_call(kind)
        return DispatchResult(
            text=(text or "").strip(),
            route=Route.LOCAL,
            target="%s · %s" % (LB.KIND_LABELS.get(kind, kind), name),
            usage=usage or {},
        )

    # ---------------- 内部：云端 ----------------

    def _call_cloud(self, messages, model, timeout) -> DispatchResult:
        fn = self._cloud_fn
        if fn is None:
            return DispatchResult(route=Route.NONE, error="云端转发未接入")
        want = model if (model and not str(model).startswith(LB.LOCAL_PREFIX)) else None
        try:
            text, info = fn(messages, want, timeout)
        except Exception as e:  # noqa: BLE001
            return DispatchResult(route=Route.CLOUD, error="云端调用失败：%s" % e)
        info = info or {}
        if not (text or "").strip():
            return DispatchResult(route=Route.CLOUD,
                                  target=info.get("target") or "账号池",
                                  error=info.get("error") or "云端返回空响应")
        return DispatchResult(
            text=text.strip(),
            route=Route.CLOUD,
            target=info.get("target") or "账号池",
            usage=info.get("usage") or {},
        )

    # ---------------- 状态 ----------------

    def status(self) -> Dict[str, Any]:
        kind, detail = self.local_ready()
        prefs = self._prefs()
        with self._lock:
            stats = dict(self.stats)
        return {
            "local_ready": kind is not None,
            "local_kind": kind,
            "local_detail": detail,
            "prefer_local": bool(prefs.get("prefer_local", True)),
            "local_model": prefs.get("local_model") or (
                self.pick_local_model(kind) if kind else ""),
            "cloud_available": self._cloud_fn is not None,
            "stats": stats,
        }


# --------------------------------------------------------------------------
# 模块级单例
# --------------------------------------------------------------------------

_DISPATCHER: Optional[AIDispatcher] = None
_DISPATCHER_LOCK = threading.Lock()


def dispatcher() -> AIDispatcher:
    """取全局调度器；首次调用时自动接上 api_server 的本地池与云端转发。"""
    global _DISPATCHER
    with _DISPATCHER_LOCK:
        if _DISPATCHER is None:
            _DISPATCHER = AIDispatcher()
            _wire_defaults(_DISPATCHER)
        return _DISPATCHER


def _wire_defaults(d: AIDispatcher) -> None:
    """把调度器接到 api_server 的本地池与云端账号池上。

    放在函数里做延迟导入，避免 `api_server` ↔ `ai_dispatch` 循环导入。
    """
    try:
        from . import api_server as AS
    except Exception:  # noqa: BLE001
        return
    svc = AS.service()
    d.set_local_pool(svc.local)
    d.set_cloud_fn(_make_cloud_fn(svc))
    d.set_settings_getter(_make_settings_getter(svc))


def _make_settings_getter(svc):
    def _get():
        try:
            return {
                "prefer_local": svc.prefer_local,
                "local_model": svc.ai_local_model,
            }
        except Exception:  # noqa: BLE001
            return {}
    return _get


def _make_cloud_fn(svc):
    """构造云端调用函数：直接复用 API 服务的账号池转发逻辑。

    复用而不是重写，保证「对外 HTTP 服务」与「对内 AI 调度」走的
    永远是同一条协议、同一份账号池冷却状态。
    """
    def _call(messages, model, timeout):
        pool = svc.pool
        raw = {"messages": messages, "model": model or svc.model}
        data = __import__("json").dumps(raw, ensure_ascii=False).encode("utf-8")

        tried = set()
        last_err = ""
        for _ in range(AS_MAX_ROTATE):
            acc = pool.pick_excluding(tried)
            if acc is None:
                break
            tried.add(acc["uid"])
            converted = SU.prepare_llm_chat_body(
                data, svc.model, acc["uid"], acc["device_id"], acc["machine_id"])
            try:
                resp = SU.make_upstream_request(
                    acc["jwt"], acc["device_id"], acc["machine_id"], converted)
            except SU.UpstreamError as e:
                kind = SU.classify_error(e.status, e.body)
                pool.report_err(acc["uid"], reason="upstream %s" % e.status,
                                kind=kind)
                last_err = "上游 HTTP %s" % e.status
                continue
            except SU.UpstreamTransportError as e:
                pool.report_err(acc["uid"], reason="transport",
                                kind=SU.ErrKind.SERVER)
                last_err = str(e)
                continue

            resp_obj, err = SU.aggregate(SU.iter_upstream_lines(resp), "ai-dispatch")
            if err:
                code, msg = err
                kind = SU.classify_solo_error(code, msg)
                if kind != SU.ErrKind.NONE:
                    pool.report_err(acc["uid"], reason="solo %s" % code, kind=kind)
                last_err = "上游错误 %s: %s" % (code, msg)
                continue
            text = ""
            if resp_obj:
                msg_obj = (resp_obj.get("choices") or [{}])[0].get("message") or {}
                text = msg_obj.get("content") or ""
            if not text.strip():
                pool.report_err(acc["uid"], reason="empty response",
                                kind=SU.ErrKind.SERVER)
                last_err = "上游返回空响应"
                continue
            pool.report_ok(acc["uid"])
            return text, {
                "target": "%s（%s）" % (acc.get("name") or acc["uid"][:8],
                                        "云端账号池"),
                "usage": resp_obj.get("usage") if resp_obj else {},
                "uid": acc["uid"],
            }
        reasons = SU.diagnose_summary(pool.diagnose())
        detail = last_err or "没有可转发的账号"
        if reasons:
            detail += "｜池状态：%s" % reasons
        return "", {"error": detail, "target": "账号池"}
    return _call


# 与 api_server.MAX_ROTATE 保持一致（此处不做导入，避免循环依赖）
AS_MAX_ROTATE = 3


def ask(prompt, **kwargs) -> DispatchResult:
    """便捷入口：等价于 `dispatcher().ask(prompt, **kwargs)`。"""
    return dispatcher().ask(prompt, **kwargs)


def status() -> Dict[str, Any]:
    return dispatcher().status()


__all__ = ["AIDispatcher", "DispatchResult", "Route", "ask", "dispatcher",
           "status", "DEFAULT_SYSTEM_PROMPT"]
