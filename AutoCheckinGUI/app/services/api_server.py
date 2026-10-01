#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 API 服务：把账号池 / 本地模型包装成 OpenAI 兼容接口。

设计说明
--------
参考项目里有一个「API 服务」板块：在本机起一个 OpenAI 兼容的 HTTP 服务，
把多个账号组成轮转池，客户端（各类 AI 编辑器/脚本）只需指向
`http://127.0.0.1:<port>/v1` 即可，由服务端负责选号、冷却与失败切换。

**两种后端，同一个端口，按模型名自动路由**：

  * **账号池**（云端 IDE 积分）—— 模型名是 `glm-5` / `deepseek-v4-pro` 等。
    转发协议在 `app/services/solo_upstream.py`（从参考项目的
    `payload.rs` / `routes.rs` / `sse.rs` / `pool.rs` 一比一移植），
    真正调用 `llm_utils_chat` 端点，**消耗 TraeCN 的 IDE 积分**。
    只有 TraeCN 账号带得上游凭据（jwt + device_id），因此只有它们参与转发；
    TraeWork / WorkBuddy 账号在池里只作展示，状态标为 `no_credential`。
  * **本地模型**（Ollama / llama.cpp）—— 模型名以 `local/` 开头，
    例如 `local/ollama/llama3.2`、`local/llamacpp/qwen2.5-7b`。
    这部分走 `app/services/local_backends.py`，**是真实可用的**。

客户端不需要关心走哪条路，只要挑了对应的模型名即可。

对外能力：
  GET  /health                健康检查（含各后端状态）
  GET  /v1/models             模型列表（云端 + 已启用的本地模型）
  GET  /v1/status             账号池详情（每个账号的状态与冷却原因）
  POST /v1/chat/completions   聊天补全（支持 stream=true）
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import local_backends as LB
from . import solo_upstream as SU

# 参考项目内置的模型清单（保持一致的命名，便于客户端直接套用）
MODEL_OPTIONS = [
    "glm-5.2", "glm-5.3", "glm-5-turbo", "glm-5",
    "deepseek-v4-flash", "deepseek-v4-pro",
    "kimi-k2.7-code", "kimi-k3",
    "doubao-seed-2.1-pro", "doubao-seed-2.1-turbo", "doubao-seed-2.0-code",
    "minimax-m3", "qwen-3.7-plus",
]

DEFAULT_PORT = 7864
COOLDOWN_SECONDS = 60          # 单账号出错后的冷却时间
MAX_ERR_BEFORE_DISABLE = 5     # 连续错误达到该值则临时禁用

#: 上游失败切换次数（对应参考实现 MAX_ROTATE）
MAX_ROTATE = 3
#: 单个请求体上限（对应参考实现 8MB）
MAX_BODY_BYTES = SU.MAX_BODY_BYTES


def mask_api_key(key):
    """API Key 打码：保留前 4 后 4。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


class _ModelCatalog:
    """模型清单：静态上游清单 ∪ 已启用本地模型。

    与 `model_choices()` 的区别是这里**不依赖**本地后端探测结果，
    只反映「配置上可用」，用于 /v1/models 与默认模型的合法性判断。
    """

    def __init__(self, local_pool=None):
        self._local = local_pool

    def cloud_ids(self):
        """云端可见模型名（含静态清单里本模块未列入的项，按需并集）。"""
        seen = list(MODEL_OPTIONS)
        for name in SU.model_ids():
            if name not in seen:
                seen.append(name)
        return seen

    def local_ids(self):
        if self._local is None:
            return []
        return self._local.model_ids()

    def all_ids(self):
        return self.cloud_ids() + self.local_ids()

    def resolve(self, model, default=None):
        """归一化模型名：空值回落到 default，再回落到静态默认模型。"""
        name = (model or "").strip()
        if name:
            return name
        return (default or "").strip() or MODEL_OPTIONS[0]


class LocalPool:
    """本地模型池：管理已启用的 Ollama / llama.cpp 后端。

    与 AccountPool 不同，这里没有「轮转」语义 —— 本地后端是按
    **模型名**精确选择的，所以只需按 kind 找到对应后端并转发即可。

    实例是 lazy 的：只有真的被调用时才去连服务，避免进页面就卡住。
    """

    def __init__(self):
        self._lock = threading.Lock()
        # kind -> {"base_url":..., "enabled":bool, "models":[名...],
        #          "ok":bool, "detail":str, "checked_at":float}
        self._rows = {}
        for kind in LB.BACKENDS:
            self._rows[kind] = {
                "kind": kind,
                "label": LB.KIND_LABELS.get(kind, kind),
                "base_url": LB.DEFAULT_URLS.get(kind, ""),
                "enabled": False,
                "models": [],
                "ok": False,
                "detail": "未探测",
                "checked_at": 0.0,
                "calls": 0,
            }
        self._instances = {}

    # ---- 配置 ----

    def configure(self, kind, base_url=None, enabled=None):
        with self._lock:
            row = self._rows.get(kind)
            if row is None:
                return
            if base_url:
                row["base_url"] = base_url.rstrip("/")
                self._instances.pop(kind, None)      # 地址变了要重建
            if enabled is not None:
                row["enabled"] = bool(enabled)
                if not enabled:
                    row["ok"] = False
                    row["detail"] = "未启用"

    def rows(self):
        with self._lock:
            return [dict(r) for r in self._rows.values()]

    def enabled_kinds(self):
        with self._lock:
            return [k for k, r in self._rows.items() if r["enabled"]]

    def get_row(self, kind):
        with self._lock:
            r = self._rows.get(kind)
            return dict(r) if r else None

    def base_url(self, kind):
        with self._lock:
            r = self._rows.get(kind)
            return r["base_url"] if r else ""

    # ---- 探测 ----

    def probe(self, kind, timeout=None):
        """探测单个后端，把结果写回状态并返回 (ok, detail, models)。"""
        url = self.base_url(kind)
        if not url:
            detail = "未配置服务地址"
            self._set_state(kind, False, detail, [])
            return False, detail, []
        try:
            be = LB.make_backend(kind, url)
            ok, detail, _ = be.probe()
            models = []
            if ok:
                try:
                    models = [m["id"] for m in be.list_models()]
                except LB.BackendError:
                    models = []
            self._set_state(kind, ok, detail, models)
            return ok, detail, models
        except LB.BackendError as e:
            self._set_state(kind, False, str(e), [])
            return False, str(e), []

    def probe_async(self, kind, on_done=None):
        """后台探测，避免阻塞界面（Ollama 冷启动时 /api/tags 可能等几秒）。"""
        def work():
            ok, detail, models = self.probe(kind)
            if on_done:
                try:
                    on_done(kind, ok, detail, models)
                except Exception:  # noqa: BLE001
                    pass

        t = threading.Thread(target=work, daemon=True,
                             name=f"probe-{kind}")
        t.start()
        return t

    def _set_state(self, kind, ok, detail, models):
        with self._lock:
            row = self._rows.get(kind)
            if row:
                row["ok"] = bool(ok)
                row["detail"] = detail
                row["checked_at"] = time.time()
                if models:
                    row["models"] = list(models)

    # ---- 调用 ----

    def backend_for(self, kind):
        """取（或惰性创建）后端实例。"""
        with self._lock:
            inst = self._instances.get(kind)
            if inst is not None:
                return inst
            url = self._rows.get(kind, {}).get("base_url")
        if not url:
            raise LB.BackendError(f"{LB.KIND_LABELS.get(kind, kind)} 未配置地址")
        inst = LB.make_backend(kind, url)
        with self._lock:
            self._instances[kind] = inst
        return inst

    def count_call(self, kind):
        with self._lock:
            row = self._rows.get(kind)
            if row:
                row["calls"] += 1

    def model_ids(self):
        """已启用后端的 `local/<kind>/<model>` 形式模型名列表。

        没探测过（models 为空）时退化为只给出后端占位名，
        这样客户端至少能把模型列出来。
        """
        out = []
        with self._lock:
            rows = [dict(r) for r in self._rows.values() if r["enabled"]]
        for r in rows:
            kind, label = r["kind"], r["label"]
            names = r["models"] or ([f"{kind}-default"] if r["ok"] else [])
            if not names:
                # 连不上也给出一个占位，调用时会返回明确的后端错误
                names = [f"{kind}-default"]
            for n in names:
                out.append(LB.qualify(kind, n))
        return out

    def status(self):
        with self._lock:
            return [dict(r) for r in self._rows.values()]


class AccountPool:
    """账号池：负责选号轮转、错误计数与冷却。

    纯内存结构，进程退出即清空；账号来源是签到模块已有的账号枚举结果。

    账号分两类：

    * **可转发账号**（TraeCN）—— 带 `jwt` + `device_id`，能真正调用
      上游 `llm_utils_chat`，`llm_utils_chat` 消耗 IDE 积分。
    * **仅记账账号**（TraeWork / WorkBuddy）—— 目前没有可用的转发凭据，
      在池里只作为「候选」呈现，`can_forward` 为 False，选号时会被跳过。
    """

    def __init__(self, accounts=None):
        self._lock = threading.Lock()
        self._rows = {}
        self._cursor = 0
        for acc in (accounts or []):
            self.add(acc)

    def add(self, acc):
        """acc 需含 uid / name，可选 credits / jwt / device_id。"""
        uid = str(acc.get("uid") or acc.get("id") or "")
        if not uid:
            return
        jwt = str(acc.get("jwt") or "").strip()
        with self._lock:
            self._rows[uid] = {
                "uid": uid,
                "name": acc.get("name") or acc.get("display") or uid[:8] + "****",
                "platform": acc.get("platform") or "",
                "credits": acc.get("credits"),
                # ---- 上游转发凭据（仅 TraeCN 有）----
                "jwt": jwt,
                "device_id": str(acc.get("device_id") or "").strip(),
                "machine_id": str(acc.get("machine_id") or "").strip(),
                "credits_expire_at": acc.get("credits_expire_at"),
                # ---- 状态机 ----
                "err_count": 0,
                "cooling_until": 0.0,
                "cooldown_reason": None,
                "disabled": False,
                "requests": 0,
            }

    def set_members(self, uids):
        """按 uid 白名单重建池（保留已有统计）。"""
        uids = [str(u) for u in (uids or [])]
        with self._lock:
            kept = {u: self._rows[u] for u in uids if u in self._rows}
            self._rows = kept
            self._cursor = 0

    # ---- 可用性判定 ----

    @staticmethod
    def _healthy(row, now):
        if row.get("disabled"):
            return False
        until = row.get("cooling_until") or 0.0
        return not (until > 0 and now < until)

    @staticmethod
    def _usable_credits(row, now):
        """积分过期 / 归零的账号跳过，避免无效请求。"""
        exp = row.get("credits_expire_at")
        if isinstance(exp, (int, float)) and exp > 0 and exp < now:
            return False
        c = row.get("credits")
        if isinstance(c, (int, float)) and c <= 0.0:
            return False
        return True

    def can_forward(self, row):
        """该账号是否具备真实转发能力（有令牌 + 设备号）。"""
        return bool(row.get("jwt")) and bool(row.get("device_id"))

    def pick(self):
        """轮转选一个可用账号；全不可用返回 None。"""
        now = time.time()
        with self._lock:
            uids = list(self._rows.keys())
            if not uids:
                return None
            n = len(uids)
            for i in range(n):
                uid = uids[(self._cursor + i) % n]
                row = self._rows[uid]
                if not self._healthy(row, now):
                    continue
                if row["err_count"] >= MAX_ERR_BEFORE_DISABLE:
                    continue
                self._cursor = (self._cursor + i + 1) % n
                row["requests"] += 1
                return dict(row)
            return None

    def pick_excluding(self, tried):
        """挑选**可转发**且健康的账号，跳过 tried 集合。

        对齐参考实现 pool.rs::pick_excluding：优先积分过期时间最近者，
        有过期信息的优先于没有的，同过期时间比积分多者优先。
        """
        tried = set(tried or ())
        now = time.time()
        with self._lock:
            best = None
            for uid, row in self._rows.items():
                if uid in tried:
                    continue
                if not self.can_forward(row):
                    continue
                if not self._healthy(row, now):
                    continue
                if not self._usable_credits(row, now):
                    continue
                if best is None:
                    best = row
                    continue
                be = isinstance(best.get("credits_expire_at"), (int, float))
                ee = isinstance(row.get("credits_expire_at"), (int, float))
                if ee and not be:
                    best = row
                elif ee and be:
                    if row["credits_expire_at"] < best["credits_expire_at"]:
                        best = row
                    elif row["credits_expire_at"] == best["credits_expire_at"]:
                        if (row.get("credits") or 0.0) > (best.get("credits") or 0.0):
                            best = row
            if best is None:
                return None
            best["requests"] += 1
            return dict(best)

    def report_ok(self, uid):
        """转发成功：清空错误计数与冷却。"""
        with self._lock:
            row = self._rows.get(str(uid))
            if row:
                row["err_count"] = 0
                row["cooling_until"] = 0.0
                row["cooldown_reason"] = None

    def report_err(self, uid, reason="", kind=None):
        """转发失败：按类别冷却（对齐参考实现 pool.rs::note_error）。

        * SessionDead -> 直接禁用（需要重新登录取令牌）
        * PlanLimit / SoftRate / NotFound -> 按各自时长冷却并清零计数
        * 其他 -> 累加计数，连续 3 次才冷却（避免偶发失败误杀）
        """
        with self._lock:
            row = self._rows.get(str(uid))
            if not row:
                return
            if kind == SU.ErrKind.SESSION_DEAD:
                row["disabled"] = True
                row["cooldown_reason"] = reason or SU.ErrKind.SESSION_DEAD
                return
            if kind in (SU.ErrKind.PLAN_LIMIT, SU.ErrKind.SOFT_RATE, SU.ErrKind.NOT_FOUND):
                secs = SU.ErrKind.cooldown_seconds(kind)
                row["cooling_until"] = time.time() + secs
                row["cooldown_reason"] = reason or kind
                row["err_count"] = 0
                return
            row["err_count"] += 1
            if row["err_count"] >= 3:
                secs = SU.ErrKind.cooldown_seconds(kind or SU.ErrKind.SERVER) or COOLDOWN_SECONDS
                row["cooling_until"] = time.time() + secs
                row["cooldown_reason"] = reason or "consecutive_errors"
                row["err_count"] = 0

    def clear_cooldowns(self):
        """清除全部冷却与错误计数，返回被清除的账号数。

        与参考实现一致：**不清除 disabled**（SessionDead 需要重新登录）。
        """
        n = 0
        with self._lock:
            for row in self._rows.values():
                if row["cooling_until"] > 0 or row["err_count"] > 0:
                    row["cooling_until"] = 0.0
                    row["cooldown_reason"] = None
                    row["err_count"] = 0
                    n += 1
        return n

    def diagnose(self):
        """诊断每个账号被跳过/可用的原因（用于「没有可用账号」排查）。"""
        now = time.time()
        with self._lock:
            out = []
            for row in self._rows.values():
                if row.get("disabled"):
                    reason = "disabled(SessionDead)"
                elif not self.can_forward(row):
                    reason = "no_credential(仅记账)"
                elif row["cooling_until"] > now:
                    reason = "cooldown(remaining=%ds)" % int(row["cooling_until"] - now)
                elif not self._usable_credits(row, now):
                    exp = row.get("credits_expire_at")
                    if isinstance(exp, (int, float)) and exp > 0 and exp < now:
                        reason = "credits_expired"
                    else:
                        reason = "zero_credits"
                else:
                    reason = "healthy"
                out.append({
                    "uid": row["uid"],
                    "name": row["name"],
                    "disabled": bool(row.get("disabled")),
                    "until": int(row["cooling_until"] or 0),
                    "credits": row.get("credits"),
                    "credits_expire_at": row.get("credits_expire_at"),
                    "reason": reason,
                })
            return out

    def status(self):
        """返回池内每行的实时状态快照。"""
        now = time.time()
        with self._lock:
            out = []
            for row in self._rows.values():
                exp = row.get("credits_expire_at")
                credits = row.get("credits")
                if row.get("disabled"):
                    state = "disabled"
                elif (row["cooling_until"] or 0) > now:
                    state = "cooling"
                elif isinstance(exp, (int, float)) and exp > 0 and exp < now:
                    state = "expired"
                elif isinstance(credits, (int, float)) and credits <= 0.0:
                    state = "no_credits"
                elif not self.can_forward(row):
                    state = "no_credential"
                else:
                    state = "available"
                out.append({
                    "uid": row["uid"],
                    "name": row["name"],
                    "platform": row["platform"],
                    "credits": credits,
                    "credits_expire_at": exp,
                    "err_count": row["err_count"],
                    "cooling": (row["cooling_until"] or 0) > now,
                    "cooldown_until": row["cooling_until"] or 0,
                    "disabled": bool(row.get("disabled")),
                    "cooldown_reason": row["cooldown_reason"],
                    "requests": row["requests"],
                    "can_forward": self.can_forward(row),
                    "state": state,
                })
            return out

    def count(self):
        with self._lock:
            return len(self._rows)

    def forwardable_count(self):
        with self._lock:
            return sum(1 for r in self._rows.values() if self.can_forward(r))


class _Handler(BaseHTTPRequestHandler):
    """OpenAI 兼容的最小路由。"""

    server_version = "TraeBuddyLocalAPI/1.0"

    # 关闭默认的逐请求日志，避免刷屏（改由父服务自行统计）
    def log_message(self, fmt, *args):  # noqa: A003
        return

    # ---------- 工具 ----------

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _send_stream_start(self):
        """SSE 响应头。

        注意协议版本：BaseHTTPRequestHandler 默认 HTTP/1.0，此时响应没有
        Content-Length 就是「读到 EOF 为止」。若再声明 `Connection:
        keep-alive`，客户端会一直等连接关闭，`data: [DONE]` 之后仍挂着不返回
        （实测 urllib 会卡到超时）。所以这里必须用 `Connection: close`，
        让 `[DONE]` 之后的 EOF 成为正常收尾信号。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        # 有些客户端（浏览器 fetch）需要它才允许读流
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _send_chunk(self, obj):
        """发一个 SSE data 块。"""
        data = json.dumps(obj, ensure_ascii=False)
        try:
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
        except OSError:
            raise

    def _send_done(self):
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            pass

    def _auth_ok(self):
        key = self.server.api_key
        if not key:
            return True
        got = self.headers.get("Authorization") or ""
        if got.lower().startswith("bearer "):
            got = got[7:].strip()
        return got == key

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8"))
        except (ValueError, OSError):
            return {}

    # ---------- 路由 ----------

    def do_OPTIONS(self):  # noqa: N802
        """预检：方便浏览器端直接调用。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/health", "/v1/health"):
            lp = self.server.local_pool
            self._send(200, {
                "status": "ok",
                "port": self.server.server_address[1],
                "pool": self.server.pool.count(),
                "pool_forwardable": self.server.pool.forwardable_count(),
                "cloud_ready": self.server.pool.forwardable_count() > 0,
                "uptime": round(time.time() - self.server.started, 1),
                "local": [{"kind": r["kind"], "enabled": r["enabled"],
                           "ok": r["ok"], "models": len(r["models"]),
                           "detail": r["detail"]}
                          for r in lp.status()],
            })
            return
        if path == "/v1/models":
            if not self._auth_ok():
                self._send(401, {"error": {"message": "invalid api key"}})
                return
            now = int(time.time())
            cat = _ModelCatalog(self.server.local_pool)
            data = [{"id": m, "object": "model", "created": now,
                     "owned_by": "trae-buddy"} for m in cat.cloud_ids()]
            # 本地模型：owned_by 标成 ollama / llamacpp，方便客户端区分
            for mid in self.server.local_pool.model_ids():
                kind, _name = LB.split_model(mid)
                data.append({"id": mid, "object": "model", "created": now,
                             "owned_by": kind or "local"})
            self._send(200, {"object": "list", "data": data})
            return
        if path in ("/v1/local/status", "/local/status"):
            rows = self.server.local_pool.status()
            self._send(200, {"object": "list", "data": rows})
            return
        if path in ("/v1/status", "/status"):
            # 与参考实现对齐的状态详情：池内每个账号的状态与冷却原因
            pool = self.server.pool.status()
            cooling = sum(1 for p in pool if p.get("cooling"))
            disabled = sum(1 for p in pool if p.get("disabled"))
            available = sum(1 for p in pool
                            if p.get("state") == "available")
            self._send(200, {
                "running": True,
                "total_requests": self.server.total_requests,
                "active_uid": self.server.active_uid,
                "last_error": self.server.last_error,
                "summary": {
                    "total_accounts": len(pool),
                    "available": available,
                    "cooling": cooling,
                    "disabled": disabled,
                    "forwardable": self.server.pool.forwardable_count(),
                },
                "accounts": pool,
            })
            return
        self._send(404, {"error": {"message": f"not found: {self.path}"}})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        if path != "/v1/chat/completions":
            self._send(404, {"error": {"message": f"not found: {self.path}"}})
            return
        if not self._auth_ok():
            self._send(401, {"error": {"message": "invalid api key"}})
            return

        body = self._read_body()
        model = body.get("model") or self.server.default_model
        want_stream = bool(body.get("stream"))
        self.server.total_requests += 1

        kind, real_name = LB.split_model(model)
        if kind is not None or model.startswith(LB.LOCAL_PREFIX):
            self._handle_local(body, model, kind, real_name, want_stream)
            return
        self._handle_pool(body, model, want_stream)

    # ---------- 本地模型 ----------

    def _resolve_local(self, kind):
        """确定「用哪个本地后端」；kind 为空时按唯一启用者推断。"""
        lp = self.server.local_pool
        if kind and kind in LB.BACKENDS:
            row = lp.get_row(kind)
            if row and not row["enabled"]:
                raise LB.BackendError(
                    f"{row['label']} 未在「本地模型」中启用")
            return kind
        enabled = lp.enabled_kinds()
        if not enabled:
            raise LB.BackendError(
                "还没有启用任何本地后端，请在「API 服务 → 本地模型」中开启")
        if len(enabled) > 1:
            raise LB.BackendError(
                "启用了多个本地后端，请在模型名里指明，"
                f"例如 local/{enabled[0]}/<模型名>")
        return enabled[0]

    def _handle_local(self, body, model, kind, real_name, want_stream):
        lp = self.server.local_pool
        try:
            kind = self._resolve_local(kind)
            if kind not in LB.BACKENDS:
                raise LB.BackendError(f"未知的本地后端：{kind}")
            be = lp.backend_for(kind)
            lp.count_call(kind)
            self.server.active_uid = f"{kind}:{real_name}"
            if want_stream:
                self._stream_local(be, body, real_name or model, kind, model)
            else:
                text, usage = be.chat(body, real_name or model)
                self.server.last_error = None
                self._send(200, {
                    "id": f"chatcmpl-{int(time.time()*1000)}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }],
                    "usage": usage,
                })
        except LB.BackendError as e:
            self.server.last_error = str(e)
            self._send(502, {"error": {"message": str(e),
                                       "type": "local_backend_error"}})
        except Exception as e:  # noqa: BLE001
            self.server.last_error = repr(e)
            self._send(502, {"error": {"message": repr(e),
                                       "type": "local_backend_error"}})

    def _stream_local(self, be, body, real_name, kind, model):
        """SSE 流式转发本地后端。"""
        self._send_stream_start()
        cid = f"chatcmpl-{int(time.time()*1000)}"
        created = int(time.time())

        def chunk(delta, finish=None):
            return {
                "id": cid, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta,
                             "finish_reason": finish}],
            }

        try:
            self._send_chunk(chunk({"role": "assistant", "content": ""}))
            usage = None
            for item in be.chat_stream(body, real_name):
                if isinstance(item, dict):
                    usage = item          # 最后一个一定是 usage
                    continue
                self._send_chunk(chunk({"content": item}))
            self._send_chunk(chunk({}, finish="stop"))
            if usage:
                self._send_chunk({"id": cid, "object": "chat.completion.chunk",
                                  "created": created, "model": model,
                                  "choices": [], "usage": usage})
            self.server.last_error = None
        except LB.BackendError as e:
            # 头已经发出去了，只能以 SSE 错误块收尾
            self.server.last_error = str(e)
            self._send_chunk({"error": {"message": str(e),
                                        "type": "local_backend_error"}})
        except Exception as e:  # noqa: BLE001
            self.server.last_error = repr(e)
            self._send_chunk({"error": {"message": repr(e)}})
        finally:
            self._send_done()

    # ---------- 账号池 ----------

    def _handle_pool(self, body, model, want_stream):
        """云端转发：多账号轮转 + 失败冷却。

        对齐参考实现 routes.rs：最多尝试 MAX_ROTATE 个账号。
        连接成功（HTTP 200）后即开始转换，**中途出错只冷却不轮换**
        （因为响应头已经发出去了，无法再换账号重来）。
        所有账号都不可用时：流式返回 SSE 错误块 + [DONE]，非流式返回 503。
        """
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_BODY_BYTES:
            self._send(413, {"error": {"message": "request body exceeds 8MB limit",
                                       "type": "api_error",
                                       "code": "request_too_large"}})
            return

        tried = set()
        pool = self.server.pool
        chat_id = f"chatcmpl-{int(time.time())}"
        if want_stream:
            self._send_stream_start()

        for _ in range(MAX_ROTATE):
            acc = pool.pick_excluding(tried)
            if acc is None:
                break
            tried.add(acc["uid"])
            self.server.active_uid = acc["uid"]

            converted = SU.prepare_llm_chat_body(
                raw, self.server.default_model, acc["uid"],
                acc["device_id"], acc["machine_id"],
            )

            try:
                resp = SU.make_upstream_request(
                    acc["jwt"], acc["device_id"], acc["machine_id"], converted,
                )
            except SU.UpstreamError as e:
                kind = SU.classify_error(e.status, e.body)
                pool.report_err(acc["uid"], reason=f"upstream {e.status}", kind=kind)
                self.server.last_error = (
                    f"uid={acc['uid']} status={e.status} "
                    f"body={SU.safe_slice(e.body, 200)}")
                continue
            except SU.UpstreamTransportError as e:
                kind = SU.classify_error(e.status, str(e))
                pool.report_err(acc["uid"], reason="transport", kind=kind)
                self.server.last_error = f"uid={acc['uid']} {e}"
                continue

            # 已连上上游：进入转换阶段，此后不再换账号
            lines = SU.iter_upstream_lines(resp)
            if want_stream:
                held = []
                err = SU.stream_convert(lines, chat_id, held.append)
                if err:
                    code, msg = err
                    kind = SU.classify_solo_error(code, msg)
                    if kind != SU.ErrKind.NONE:
                        pool.report_err(acc["uid"], reason=f"solo {code}", kind=kind)
                    self.server.last_error = f"uid={acc['uid']} code={code} msg={msg}"
                else:
                    pool.report_ok(acc["uid"])
                    self.server.last_error = None
                self._emit_solo(held, model)
                return

            aggregated, err = SU.aggregate(lines, chat_id)
            if err:
                code, msg = err
                kind = SU.classify_solo_error(code, msg)
                if kind != SU.ErrKind.NONE:
                    pool.report_err(acc["uid"], reason=f"solo {code}", kind=kind)
                self.server.last_error = f"uid={acc['uid']} code={code} msg={msg}"
                continue
            if aggregated is None or not self._has_content(aggregated):
                # 上游 200 却什么都没给：按服务端错误记一次并轮转下一个账号
                # （对齐参考实现 (None, None) => note_error(Server) + continue）
                pool.report_err(acc["uid"], reason="empty response",
                                kind=SU.ErrKind.SERVER)
                self.server.last_error = f"uid={acc['uid']} 上游返回空响应"
                continue

            pool.report_ok(acc["uid"])
            self.server.last_error = None
            aggregated["model"] = model
            self._send(200, aggregated)
            return

        # 走到这里说明没有任何账号成功 —— 记录诊断并回报
        diag = SU.diagnose_summary(pool.diagnose())
        self.server.last_error = "没有可转发的账号（账号池为空或全部不可用）"
        self.server.write_diag(
            "NO_HEALTHY_ACCOUNT tried=%d pool=%d reasons=[%s]"
            % (len(tried), pool.count(), diag))
        if want_stream:
            self._send_chunk({
                "error": {"message": "no healthy account available",
                          "type": "api_error",
                          "code": "no_healthy_account"},
            })
            self._send_done()
            return
        self._send(503, {"error": {"message": "no healthy account available",
                                   "type": "api_error",
                                   "code": "no_healthy_account"}})

    @staticmethod
    def _has_content(aggregated):
        """判断聚合结果是否真的带了内容（空响应要轮转，不能当成功返回）。"""
        choices = aggregated.get("choices")
        if not isinstance(choices, list) or not choices:
            return False
        msg = choices[0].get("message") or {}
        if (msg.get("content") or "").strip():
            return True
        if (msg.get("reasoning_content") or "").strip():
            return True
        # 只有工具调用没有文本也算有效响应
        if msg.get("tool_calls"):
            return True
        return False

    def _emit_solo(self, lines, model):
        """把 solo 上游产出的 SSE 行原样/改名后转发给客户端。

        solo_upstream 产出的 chunk 里 `model` 是空串（与参考实现一致，
        因为上游响应里不带模型名），这里统一回填客户端请求的模型名，
        让客户端能正常识别。`data: [DONE]` 与原样错误块直接透传。
        """
        cid = f"chatcmpl-{int(time.time()*1000)}"
        for line in lines:
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):].strip()
            if payload == "[DONE]":
                self._send_done()
                continue
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if "error" in obj:
                self._send_chunk(obj)
                continue
            # 回填 id / model（上游给的是服务端临时 id，改成客户端可见的值）
            obj["model"] = model
            if not obj.get("id") or str(obj.get("id")).startswith("chatcmpl-"):
                obj["id"] = cid
            self._send_chunk(obj)


class _Server(ThreadingHTTPServer):
    """带状态位的 HTTP 服务。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, pool, api_key="", default_model=None,
                 local_pool=None):
        super().__init__(addr, handler)
        self.pool = pool
        self.local_pool = local_pool or LocalPool()
        self.api_key = api_key or ""
        self.default_model = default_model or MODEL_OPTIONS[0]
        self.started = time.time()
        self.total_requests = 0
        self.active_uid = None
        self.last_error = None

    def write_diag(self, line):
        """把一行诊断写进运行日志（便于排查「没有可用账号」）。"""
        try:
            from .. import config as C
            import os
            path = os.path.join(C.ROOT, "logs",
                                "api_server_%s.log" % time.strftime("%Y-%m-%d"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            stamp = time.strftime("%H:%M:%S")
            with open(path, "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (stamp, line))
        except Exception:  # noqa: BLE001
            pass


class ApiService:
    """API 服务门面：供 GUI 调用。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._srv = None
        self._thread = None
        self._pool = AccountPool()
        self._local = LocalPool()
        self._port = DEFAULT_PORT
        self._api_key = ""
        self._model = MODEL_OPTIONS[0]
        self._enabled_uids = []
        # 「本地优先」偏好：本地后端就绪时，本软件内部所有 AI 调用走本地
        self._prefer_local = True
        self._ai_local_model = ""
        # 配置/账号变更后需要重建池
        self.reload_accounts()
        self._load_local_settings()

    # ---------- 账号池 ----------

    @property
    def pool(self):
        return self._pool

    @pool.setter
    def pool(self, new_pool):
        """替换账号池。

        必须让正在运行的服务同步换到新池上 —— `_Server` 在构造时就绑定了
        池对象，若只改 `svc._pool`，服务仍在读写旧池（表现为「改完账号池没反应」）。
        """
        self._pool = new_pool
        srv = self._srv
        if srv is not None and new_pool is not None:
            srv.pool = new_pool

    # ---------- 配置 ----------

    def configure(self, port=None, api_key=None, model=None, persist=True):
        """更新运行时配置；persist=True 时同步落盘。

        端口 / Key / 默认模型三者共用同一个设置文件，任何一项变化都
        整体重写一次，保证重启 GUI 后「接口配置」与「本地模型」一致。
        """
        with self._lock:
            if port:
                self._port = int(port)
            if api_key is not None:
                self._api_key = api_key
            if model:
                self._model = model
            srv = self._srv
        if srv:
            srv.api_key = self._api_key
            srv.default_model = self._model
        if persist:
            self.save_local_settings()

    # ---------- 本地模型配置持久化 ----------

    @staticmethod
    def _settings_path():
        """GUI 侧设置文件，与共享的 config.json 分开存放。

        理由：config.json 是命令行版与计划任务共用的，
        往里塞 GUI 专属的界面设置会污染它；这里单独一个文件更干净。
        """
        import os
        from .. import config as C
        return os.path.join(C.ROOT, "logs", "gui_settings.json")

    def _load_local_settings(self):
        """读回 logs/gui_settings.json（本地后端 + 默认模型 + 端口/Key）。"""
        import os
        path = self._settings_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        data = data or {}
        local = data.get("local_models") or {}
        for kind, cfg in local.items():
            if kind not in LB.BACKENDS:
                continue
            self._local.configure(kind,
                                  base_url=cfg.get("base_url"),
                                  enabled=cfg.get("enabled"))
        # 默认模型：可能是云端名，也可能是 local/<kind>/<model>
        model = (data.get("default_model") or "").strip()
        if model:
            self._model = model
        try:
            port = int(data.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if port:
            self._port = port
        if data.get("api_key") is not None:
            self._api_key = str(data.get("api_key") or "")
        # 本地优先偏好（默认开启；只有显式写了 false 才关）
        if data.get("prefer_local") is not None:
            self._prefer_local = bool(data.get("prefer_local"))
        if data.get("ai_local_model") is not None:
            self._ai_local_model = str(data.get("ai_local_model") or "")

    def save_local_settings(self):
        """把本地模型 + 默认模型 + 端口/Key 写到 logs/gui_settings.json。

        「默认模型」既可能是云端名（走账号池），也可能是
        `local/<kind>/<model>`（走本地后端），两者共用同一条配置，
        所以存在同一个文件里、一起读写。
        """
        import os
        path = self._settings_path()
        try:
            data = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            data["local_models"] = {
                r["kind"]: {"base_url": r["base_url"],
                            "enabled": r["enabled"]}
                for r in self._local.status()
            }
            data["default_model"] = self._model
            data["port"] = self._port
            data["api_key"] = self._api_key
            data["prefer_local"] = self._prefer_local
            data["ai_local_model"] = self._ai_local_model
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except OSError:
            return False

    # ---------- 模型名 ----------

    @property
    def port(self):
        with self._lock:
            return self._port

    @property
    def api_key(self):
        with self._lock:
            return self._api_key

    @property
    def model(self):
        return self._model

    @property
    def prefer_local(self):
        """本地优先开关：本地后端就绪时，AI 调用默认走本地。"""
        return self._prefer_local

    @prefer_local.setter
    def prefer_local(self, value):
        self._prefer_local = bool(value)

    @property
    def ai_local_model(self):
        """AI 调度默认使用的本地模型名（空 = 用后端第一个可用模型）。"""
        return self._ai_local_model

    @ai_local_model.setter
    def ai_local_model(self, value):
        self._ai_local_model = str(value or "")

    def set_ai_prefs(self, prefer_local=None, local_model=None, persist=True):
        """更新「本地优先」偏好；persist=True 时落盘。"""
        if prefer_local is not None:
            self._prefer_local = bool(prefer_local)
        if local_model is not None:
            self._ai_local_model = str(local_model or "")
        if persist:
            self.save_local_settings()
        return {"prefer_local": self._prefer_local,
                "ai_local_model": self._ai_local_model}

    def model_choices(self):
        """「默认模型」下拉框的全部候选：云端模型 + 已启用本地模型。

        返回 (value, label, group) 三元组列表，group ∈ {"cloud","local"}。
        上游模型名与下拉框显示名分离：value 是真正传给服务端的名字，
        label 只是给人看的（本地模型前面会加「本地 ·」）。
        """
        out = [(m, m, "cloud") for m in MODEL_OPTIONS]
        for mid in self._local.model_ids():
            kind, name = LB.split_model(mid)
            label = f"本地 · {LB.KIND_LABELS.get(kind, kind)} · {name}"
            out.append((mid, label, "local"))
        return out

    def is_local_model(self, name):
        """判断一个模型名是否走本地后端。"""
        if not name:
            return False
        kind, _ = LB.split_model(name)
        return kind is not None or name.startswith(LB.LOCAL_PREFIX)

    # ---------- 本地模型门面 ----------

    @property
    def local(self):
        return self._local

    def set_local(self, kind, base_url=None, enabled=None, persist=True):
        self._local.configure(kind, base_url=base_url, enabled=enabled)
        if persist:
            self.save_local_settings()
        return self._local.get_row(kind)

    def probe_local(self, kind, on_done=None):
        """后台探测本地后端；on_done(kind, ok, detail, models)。"""
        return self._local.probe_async(kind, on_done)

    def local_rows(self):
        return self._local.status()

    def local_model_ids(self):
        return self._local.model_ids()

    def reload_accounts(self, uids=None):
        """从签到模块重新枚举账号并同步到池。

        uids 非空时只保留这些账号（账号池选择）。
        """
        accs = self._collect()
        self._pool = AccountPool(accs)
        if uids is not None:
            self._enabled_uids = list(uids)
            self._pool.set_members(uids)
        else:
            self._enabled_uids = [a["uid"] for a in accs]

    def set_pool(self, uids):
        self._enabled_uids = list(uids or [])
        self._pool.set_members(self._enabled_uids)

    def pool_uids(self):
        return list(self._enabled_uids)

    @staticmethod
    def _collect():
        """聚合三个平台的账号（失败静默，尽量多拿到）。

        只有 TraeCN 账号带得上游转发凭据（jwt + device_id）：
        `tc_checkin.collect_accounts` 会去扫 IDE 日志拿最新 JWT，
        并用 `logs/traecn_device.json` 里的设备号。首次调用约 10~15 秒，
        所以外层要放到后台线程里跑。
        """
        out = []
        try:
            from .. import config as C
            cfg = C.load_config()
        except Exception:  # noqa: BLE001
            cfg = None
        try:
            from .. import config as C
            for a in C.list_traework_accounts(cfg):
                out.append({"uid": a["key"], "name": a["display"],
                            "platform": "traework"})
            for a in C.list_workbuddy_accounts(cfg):
                out.append({"uid": a["key"], "name": a["display"],
                            "platform": "workbuddy"})
            for a in C.list_traecn_accounts(cfg):
                out.append({"uid": a["key"], "name": a["display"],
                            "platform": "traecn"})
        except Exception:  # noqa: BLE001
            pass
        # 给 TraeCN 账号补上转发凭据（jwt / device_id / machine_id）
        try:
            ApiService._attach_traecn_credentials(out)
        except Exception:  # noqa: BLE001
            pass
        return out

    @staticmethod
    def _attach_traecn_credentials(rows, log=None):
        """为 traecn 行补 jwt / device_id / machine_id，返回补上的数量。

        凭据来源与签到完全一致（同一份令牌缓存 + 同一个设备号），
        所以「签到现在用的账号」和「API 服务能转发的账号」永远对齐。
        """
        targets = [r for r in rows if r.get("platform") == "traecn"]
        if not targets:
            return 0
        import os
        import sys
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            import tc_checkin as TC
        except Exception:  # noqa: BLE001
            return 0

        try:
            accounts = TC.collect_accounts(log=log or (lambda *a, **k: None))
        except Exception:  # noqa: BLE001
            return 0

        by_uid = {}
        for a in accounts or []:
            uid = str(a.get("uid") or "")
            if uid:
                by_uid[uid] = a

        n = 0
        for row in targets:
            # 池里的 uid 是 config 侧的 "traecn:<uid>"，这里取回纯 uid
            raw_uid = str(row.get("uid") or "")
            if raw_uid.startswith("traecn:"):
                raw_uid = raw_uid.split(":", 1)[1]
            a = by_uid.get(raw_uid)
            if not a:
                continue
            token = str(a.get("token") or "")
            token = token.split(" ", 1)[-1].strip() if " " in token else token.strip()
            did = str(a.get("did") or "").strip()
            row["jwt"] = token
            row["device_id"] = did
            row["machine_id"] = SU.machine_id_for(raw_uid) if raw_uid else ""
            row["credits_expire_at"] = a.get("exp")
            if a.get("name"):
                row["name"] = a["name"]
            n += 1
        return n

    # ---------- 生命周期 ----------

    def start(self):
        with self._lock:
            if self._srv is not None:
                return self.status()
            srv = _Server(("127.0.0.1", self._port), _Handler, self._pool,
                          api_key=self._api_key, default_model=self._model,
                          local_pool=self._local)
            self._srv = srv
            self._thread = threading.Thread(target=srv.serve_forever,
                                            daemon=True, name="api-server")
            self._thread.start()
            return self.status()

    def stop(self):
        with self._lock:
            srv, self._srv = self._srv, None
            self._thread = None
        if srv:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:  # noqa: BLE001
                pass
        return self.status()

    @property
    def running(self):
        return self._srv is not None

    def clear_cooldowns(self):
        return self._pool.clear_cooldowns()

    def ai_status(self):
        """AI 调度器状态（本地优先是否就绪）。失败静默，不影响主状态。"""
        try:
            from . import ai_dispatch as AD
            return AD.dispatcher().status()
        except Exception:  # noqa: BLE001
            return {"local_ready": False, "local_detail": "调度器未就绪",
                    "prefer_local": self._prefer_local}

    def status(self):
        srv = self._srv
        local = self._local.status()
        pool = self._pool.status()
        forwardable = self._pool.forwardable_count()
        cloud_ready = forwardable > 0
        if srv is None:
            return {"running": False, "port": self._port,
                    "total_requests": 0, "active_uid": None,
                    "pool_count": self._pool.count(),
                    "pool_forwardable": forwardable,
                    "cloud_ready": cloud_ready,
                    "last_error": None,
                    "pool": pool, "local": local,
                    "local_models": self._local.model_ids()}
        return {
            "running": True,
            "port": srv.server_address[1],
            "total_requests": srv.total_requests,
            "active_uid": srv.active_uid,
            "pool_count": srv.pool.count(),
            "pool_forwardable": srv.pool.forwardable_count(),
            "cloud_ready": srv.pool.forwardable_count() > 0,
            "last_error": srv.last_error,
            "pool": srv.pool.status(),
            "local": local,
            "local_models": self._local.model_ids(),
        }


# 全局单例：GUI 各页面共享同一个服务实例
_SERVICE = None
_SERVICE_LOCK = threading.Lock()


def service():
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = ApiService()
        return _SERVICE
