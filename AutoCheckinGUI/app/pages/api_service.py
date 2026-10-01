#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 服务页：本地 OpenAI 兼容服务。

支持两条后端链路，**同一个端口、按模型名自动路由**：

  1. **账号池轮转** —— 云端 IDE 积分，模型名如 `glm-5`、`deepseek-v4-pro`。
  2. **本地模型**   —— Ollama / llama.cpp，模型名以 `local/` 开头，
     如 `local/ollama/llama3.2`、`local/llamacpp/qwen2.5-7b`。

页面结构：
  顶部动作（启动/停止）→ 4 张状态卡 → 错误条 → 积分体系说明
  → 左「接口配置」+ 中「账号池选择」+ 右「本地模型」
  → 「云端转发」+「AI 诊断（本地优先）」两张卡
  → 运行中显示「池实时状态」表格
"""
import threading
import tkinter as tk

from .. import config as C
from ..services import api_server as A
from ..services import diagnose as DG
from ..services import local_backends as LB
from ..ui import widgets as W
from .base import BasePage

POLL_MS = 3000          # 运行中每 3 秒刷新一次状态（与参考项目一致）

#: 账号池 row["state"] → (中文, tone)
#:
#: 服务层给出的 6 种状态（见 api_server.AccountPool.diagnose）：
#:   available / no_credential / cooling / expired / no_credits / disabled
POOL_STATE_CN = {
    "available":     ("就绪",   "green"),
    "no_credential": ("无登录态", "slate"),
    "cooling":       ("冷却中", "amber"),
    "expired":       ("积分过期", "amber"),
    "no_credits":    ("零积分", "amber"),
    "disabled":      ("已禁用", "red"),
}


class ApiServicePage(BasePage):
    title = "API 服务"
    desc = ("OpenAI 兼容接口：账号池云端转发 + 本地模型（Ollama / llama.cpp）"
            "，并内置本地优先的 AI 故障诊断")

    def build(self):
        t = self.theme
        self.set_title(self.title, self.desc)
        self._svc = A.service()
        self._poll_id = None
        self._enabled = set(self._svc.pool_uids())
        # 本地后端的控件引用：kind -> {"switch","url","state","models"}
        self._local_widgets = {}
        # AI 诊断卡的运行态（避免重复发起；后台线程只投递结果）
        self._diag_busy = False
        self._diag_result = None
        self._diag_stamp = ""
        # 云端转发测试的运行态与模态框引用（防 GC）
        self._cloud_busy = False
        self._modal = None
        # ★ AI 调度状态缓存（2026-09-22）：`ai_status()` 会去打本地后端
        #   （Ollama / llama.cpp 的 GET /api/tags，连接超时 2s）。放进渲染路径
        #   就是「点一下卡两秒」—— 实测本页平均 1.9s，是全应用最卡的一页。
        #   现在渲染只读缓存，探测在后台线程做，回来再局部刷新。
        self._ai_status_cache = None
        self._ai_probing = False

        # 顶部动作按钮随运行状态切换
        self._act_btn = self.add_action("启动服务", self._on_toggle,
                                        variant="primary", icon="▶")
        self.add_action("刷新账号", self._reload, variant="outline", icon="⟳")

        # ---- 状态卡 ----
        self.stats_box = tk.Frame(self.body, bg=t.bg)
        self.stats_box.pack(fill="x")
        self._cards = {}
        specs = [
            ("state", "运行状态", "-", "slate"),
            ("reqs", "总请求数", "0", "brand"),
            ("active", "当前处理", "—", "blue"),
            ("pool", "池内账号", "0", "violet"),
            ("local", "本地模型", "0", "green"),
        ]
        for i, (key, label, val, tone) in enumerate(specs):
            c = W.StatCard(self.stats_box, t, label, value=val,
                           hint="", tone=tone)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            self.stats_box.columnconfigure(i, weight=1)
            self._cards[key] = c

        # ---- 错误提示条（默认隐藏）----
        self.err_bar = tk.Frame(self.body, bg=t.red_bg)
        self._err_lbl = tk.Label(self.err_bar, text="", font=t.f_small,
                                 fg=t.red_fg, bg=t.red_bg, anchor="w",
                                 justify="left", wraplength=880)
        self._err_lbl.pack(fill="x", padx=10, pady=7)

        # ---- 说明面板 ----
        self.info_bar = tk.Frame(self.body, bg=t.amber_bg)
        self.info_bar.pack(fill="x", pady=(12, 0))
        head = tk.Frame(self.info_bar, bg=t.amber_bg)
        head.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(head, text="ℹ 积分体系说明", font=t.f_small, fg=t.amber_fg,
                 bg=t.amber_bg, anchor="w").pack(side="left")
        tk.Label(head, text="账号池消耗 IDE 积分；本地模型不耗积分", font=t.f_tiny,
                 fg=t.amber_fg, bg=t.amber_bg).pack(side="right")
        tk.Label(self.info_bar,
                 text="IDE 积分（Trae CN）· product_id 208 · llm_utils_chat"
                      "　｜　Work 积分未采用（product_id 209 · 需 TTNet 加密）"
                      "　｜　本地模型走 Ollama / llama.cpp，算力在你自己机器上",
                 font=t.f_tiny, fg=t.amber_fg, bg=t.amber_bg, anchor="w").pack(
            fill="x", padx=10, pady=(0, 8))

        # ---- 三栏 ----
        cols = tk.Frame(self.body, bg=t.bg)
        cols.pack(fill="both", expand=True, pady=(12, 0))
        cols.columnconfigure(0, weight=3, uniform="col")
        cols.columnconfigure(1, weight=3, uniform="col")
        cols.columnconfigure(2, weight=4, uniform="col")

        self._build_config(cols)
        self._build_pool(cols)
        self._build_local(cols)

        # ---- 云端转发 + AI 诊断（左右并排，追加在原有三栏之后）----
        extra = tk.Frame(self.body, bg=t.bg)
        extra.pack(fill="both", expand=True, pady=(12, 0))
        extra.columnconfigure(0, weight=1, uniform="ex")
        extra.columnconfigure(1, weight=1, uniform="ex")
        self._build_cloud(extra)
        self._build_diag(extra)

        # ---- 池实时状态（运行中显示）----
        self.pool_card = W.Card(self.body, t, padding=14)
        self.pool_body = self.pool_card.body

        self._render_status(self._svc.status())
        # 进页面后台探测一次本地后端，回填真实模型名
        self.after(200, self._probe_all_local)
        # 首次进页面也刷一遍 AI 调度状态（本地是否就绪）
        self.after(260, self._refresh_ai_status)

    # ---------------- 接口配置 ----------------

    def _build_config(self, parent):
        t = self.theme
        card = W.Card(parent, t, padding=14)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        b = card.body
        tk.Label(b, text="⚙ 接口配置", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(fill="x", pady=(0, 10))

        tk.Label(b, text="监听端口", font=t.f_tiny, fg=t.fg_dim,
                 bg=t.bg_soft, anchor="w").pack(fill="x")
        self._port_entry = self._entry(b, str(A.DEFAULT_PORT))

        tk.Label(b, text="API Key（留空则不鉴权）", font=t.f_tiny, fg=t.fg_dim,
                 bg=t.bg_soft, anchor="w").pack(fill="x", pady=(8, 0))
        self._key_entry = self._entry(b, "")
        self._key_entry.configure(show="●")

        tk.Label(b, text="默认模型", font=t.f_tiny, fg=t.fg_dim,
                 bg=t.bg_soft, anchor="w").pack(fill="x", pady=(8, 0))
        self._model_var = tk.StringVar(value=self._svc.model)
        om = tk.OptionMenu(b, self._model_var,
                           *[m for m, _l, _g in self._svc.model_choices()])
        om.configure(font=t.f_small, bg=t.bg_input, fg=t.fg,
                     activebackground=t.bg_hover, relief="flat",
                     highlightthickness=1, highlightbackground=t.border, bd=0,
                     anchor="w")
        om["menu"].configure(font=t.f_small, bg=t.bg_soft, fg=t.fg)
        om.pack(fill="x", pady=(2, 0))
        self._model_menu = om
        # 下拉框里同时含云端与本地模型，这里给一行「当前走向」提示
        self._route_lbl = tk.Label(b, text="", font=t.f_tiny, fg=t.fg_dim,
                                   bg=t.bg_soft, anchor="w", justify="left",
                                   wraplength=380)
        self._route_lbl.pack(fill="x", pady=(3, 0))
        self._paint_route_hint()

        W.auto_wrap(tk.Label(b, text="上游：llm_utils_chat（IDE 积分，product_id 208）",
                             font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                             anchor="w", justify="left")).pack(
            fill="x", pady=(6, 10))

        # 使用方式
        tk.Label(b, text="使用方式 & 配置示例", font=t.f_small, fg=t.fg_muted,
                 bg=t.bg_soft, anchor="w").pack(fill="x")
        self._usage = tk.Label(b, text="", font=t.f_mono, fg=t.fg_muted,
                               bg=t.bg_soft, anchor="w", justify="left",
                               wraplength=380)
        self._usage.pack(fill="x", pady=(4, 8))

        btns = tk.Frame(b, bg=t.bg_soft)
        btns.pack(fill="x")
        W.button(btns, t, "保存配置", command=self._save_cfg,
                 variant="primary", small=True).pack(side="left")
        W.button(btns, t, "复制示例", command=self._copy_example,
                 variant="outline", small=True).pack(side="left", padx=(6, 0))

    def _entry(self, master, value):
        t = self.theme
        e = tk.Entry(master, font=t.f_small, bg=t.bg_input, fg=t.fg,
                     relief="flat", highlightthickness=1,
                     highlightbackground=t.border, highlightcolor=t.accent)
        e.pack(fill="x", ipady=4, pady=(2, 0))
        if value:
            e.insert(0, value)
        return e

    # ---- 默认模型：云端 + 本地合并成一个下拉框 ----

    def _refresh_model_menu(self):
        """重建「默认模型」下拉框的候选（云端模型 + 已启用本地模型）。

        本地模型是探测后才有的，所以每次探测完 / 开关后端后都要回来刷一遍，
        否则新模型不会出现在下拉框里。当前选中值若仍存在则保留。
        """
        om = getattr(self, "_model_menu", None)
        if om is None:
            return
        try:
            menu = om["menu"]
            menu.delete(0, "end")
        except tk.TclError:
            return
        choices = self._svc.model_choices()
        values = [v for v, _l, _g in choices]
        labels = {v: l for v, l, _g in choices}
        for val in values:
            menu.add_command(
                label=labels.get(val, val),
                command=lambda v=val: self._pick_model(v))
        cur = self._model_var.get()
        if cur not in values:
            # 选中的本地模型没了（后端被关/地址换了）→ 退回云端默认
            cur = A.MODEL_OPTIONS[0]
            self._model_var.set(cur)
        self._paint_route_hint()

    def _pick_model(self, value):
        """在下拉框里选中某个模型（含本地模型）。"""
        self._model_var.set(value)
        self._paint_route_hint()
        self._svc.configure(model=value)
        self._rebuild_local_list()
        self._render_status(self._svc.status())

    def _set_default_model(self, mid):
        """把某个本地模型设为默认模型（本地模型清单里的「设为默认」）。"""
        self._model_var.set(mid)
        self._svc.configure(model=mid)
        self._rebuild_local_list()
        self._render_status(self._svc.status())
        self.app.toast("success", f"默认模型已设为 {mid}")

    def _paint_route_hint(self):
        """显示当前默认模型会走哪条链路，避免用户误以为本地模型在跑云端。"""
        lbl = getattr(self, "_route_lbl", None)
        if lbl is None or not W.alive(lbl):
            return
        t = self.theme
        model = self._model_var.get()
        if self._svc.is_local_model(model):
            kind, name = LB.split_model(model)
            text = (f"→ 本地后端（{LB.KIND_LABELS.get(kind, kind)} · {name}）"
                    f"，不消耗 IDE 积分")
            tone = "green"
        else:
            text = "→ 账号池（云端 IDE 积分，需已选账号）"
            tone = "slate"
        try:
            fg, _bg = t.tone(tone)
        except Exception:  # noqa: BLE001
            fg = t.fg_dim
        try:
            lbl.configure(text=text, fg=fg)
        except tk.TclError:
            pass

    # ---------------- 账号池选择 ----------------

    def _build_pool(self, parent):
        t = self.theme
        card = W.Card(parent, t, padding=14)
        card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        b = card.body
        head = tk.Frame(b, bg=t.bg_soft)
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text="◍ 账号池选择", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(side="left")
        W.button(head, t, "刷新", command=self._reload, variant="ghost",
                 small=True, icon="⟳").pack(side="right")

        tools = tk.Frame(b, bg=t.bg_soft)
        tools.pack(fill="x", pady=(0, 6))
        W.button(tools, t, "全选", command=lambda: self._select_all(True),
                 variant="ghost", small=True).pack(side="left")
        W.button(tools, t, "清空", command=lambda: self._select_all(False),
                 variant="ghost", small=True).pack(side="left", padx=(4, 0))
        self._pool_count_lbl = tk.Label(tools, text="已选 0 / 0", font=t.f_tiny,
                                        fg=t.fg_dim, bg=t.bg_soft)
        self._pool_count_lbl.pack(side="right")

        holder = tk.Frame(b, bg=t.bg_soft, height=190)
        holder.pack(fill="both", expand=True)
        holder.pack_propagate(False)
        self._pool_list = W.ScrollFrame(holder, t, bg=t.bg_soft)
        self._pool_list.pack(fill="both", expand=True)
        self._pool_rows = {}

        W.button(b, t, "保存账号池", command=self._save_pool,
                 variant="outline", small=True).pack(fill="x", pady=(8, 0))

    def _rebuild_pool_list(self):
        t = self.theme
        inner = self._pool_list.inner
        for w in inner.winfo_children():
            w.destroy()
        self._pool_rows = {}
        accs = self._accounts()
        if not accs:
            W.EmptyState(inner, t, icon="◍", title="暂无账号",
                         hint="请先在「账号管理」中添加账号").pack(
                fill="both", expand=True, padx=4, pady=4)
            self._pool_count_lbl.configure(text="已选 0 / 0")
            return
        for a in accs:
            uid = a["uid"]
            var = tk.BooleanVar(value=uid in self._enabled)
            row = tk.Frame(inner, bg=t.bg_soft)
            row.pack(fill="x", pady=1)
            cb = tk.Checkbutton(row, text="", variable=var,
                                command=lambda u=uid, v=var: self._toggle(u, v),
                                bg=t.bg_soft, activebackground=t.bg_soft,
                                highlightthickness=0, bd=0, cursor="hand2")
            cb.pack(side="left")
            txt = tk.Frame(row, bg=t.bg_soft)
            txt.pack(side="left", fill="x", expand=True)
            # 账号名与 uid 都可能很长（uid 形如 workbuddy:<36位UUID>），
            # 必须自动折行，否则会冲出这一列的右边缘被裁掉。
            W.auto_wrap(tk.Label(txt, text=a["name"], font=t.f_small, fg=t.fg,
                                 bg=t.bg_soft, anchor="w",
                                 justify="left")).pack(fill="x")
            W.auto_wrap(tk.Label(txt, text=f"{uid}   ·   {a.get('platform','')}",
                                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                                 anchor="w",
                                 justify="left")).pack(fill="x")
            self._pool_rows[uid] = var
        self._sync_pool_count()

    def _accounts(self):
        """取账号列表。

        优先用 App 的账号缓存（后台扫描、秒回），避免每次进这一页都
        重新扫 IDE 日志 + 请求接口（TraeCN 那步要几百毫秒）。
        缓存还没就绪时退回服务层的即时收集。
        """
        try:
            cached = self.app.accounts() if self.app is not None else None
            if cached:
                out = []
                for platform, rows in cached.items():
                    for a in rows or []:
                        out.append({"uid": a["key"], "name": a["display"],
                                    "platform": platform})
                if out:
                    return out
        except Exception:  # noqa: BLE001
            pass
        try:
            return self._svc._collect()
        except Exception:  # noqa: BLE001
            return []

    # ---------------- 本地模型 ----------------

    def _build_local(self, parent):
        t = self.theme
        card = W.Card(parent, t, padding=14)
        card.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        b = card.body
        head = tk.Frame(b, bg=t.bg_soft)
        head.pack(fill="x", pady=(0, 4))
        tk.Label(head, text="⌂ 本地模型", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(side="left")
        W.button(head, t, "全部探测", command=self._probe_all_local,
                 variant="ghost", small=True, icon="⟳").pack(side="right")
        tk.Label(b, text="算力在本机，不消耗 IDE 积分；模型名以 local/ 开头",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                 anchor="w").pack(fill="x", pady=(0, 8))

        for kind in ("ollama", "llamacpp"):
            self._build_local_row(b, kind)

        # 本地模型清单（探测结果）
        tk.Label(b, text="可用模型 ID（填入客户端）", font=t.f_tiny,
                 fg=t.fg_dim, bg=t.bg_soft, anchor="w").pack(
            fill="x", pady=(10, 2))
        holder = tk.Frame(b, bg=t.bg_soft, height=150)
        holder.pack(fill="both", expand=True)
        holder.pack_propagate(False)
        self._local_list = W.ScrollFrame(holder, t, bg=t.bg_soft)
        self._local_list.pack(fill="both", expand=True)
        # 首绘：没探测过时显示「尚未探测」空状态，同时把「默认模型」
        # 下拉框的本地候选补齐（本地模型可能已在别处探测过）
        self._rebuild_local_list()

        btns = tk.Frame(b, bg=t.bg_soft)
        btns.pack(fill="x", pady=(8, 0))
        W.button(btns, t, "保存", command=self._save_local,
                 variant="outline", small=True).pack(side="left")
        W.button(btns, t, "复制模型 ID", command=self._copy_local_models,
                 variant="ghost", small=True).pack(side="left", padx=(6, 0))

    def _build_local_row(self, parent, kind):
        """单个后端的一行配置：开关 + 地址 + 状态。"""
        t = self.theme
        row = self._svc.local.get_row(kind) or {}
        wrap = tk.Frame(parent, bg=t.bg_soft)
        wrap.pack(fill="x", pady=(0, 8))

        top = tk.Frame(wrap, bg=t.bg_soft)
        top.pack(fill="x")
        tk.Label(top, text=LB.KIND_LABELS.get(kind, kind), font=t.f_small,
                 fg=t.fg, bg=t.bg_soft, anchor="w").pack(side="left")

        state_lbl = tk.Label(top, text="—", font=t.f_tiny, fg=t.fg_dim,
                             bg=t.bg_soft, anchor="e")
        state_lbl.pack(side="right")

        sw = W.Switch(top, t, value=bool(row.get("enabled")),
                      command=lambda v, k=kind: self._toggle_local(k, v))
        sw.pack(side="right", padx=(0, 8))

        e = tk.Entry(wrap, font=t.f_small, bg=t.bg_input, fg=t.fg,
                     relief="flat", highlightthickness=1,
                     highlightbackground=t.border, highlightcolor=t.accent)
        e.pack(fill="x", ipady=3, pady=(4, 0))
        e.insert(0, row.get("base_url") or LB.DEFAULT_URLS.get(kind, ""))
        e.bind("<FocusOut>", lambda _e, k=kind: self._on_url_changed(k))
        e.bind("<Return>", lambda _e, k=kind: self._on_url_changed(k))

        self._local_widgets[kind] = {"switch": sw, "url": e,
                                     "state": state_lbl, "models": []}
        self._paint_local_state(kind, row.get("ok"), row.get("detail"),
                                row.get("models") or [])

    def _paint_local_state(self, kind, ok, detail, models):
        """刷新某一行的状态文案与颜色。"""
        t = self.theme
        w = self._local_widgets.get(kind)
        if not w:
            return
        row = self._svc.local.get_row(kind) or {}
        enabled = bool(row.get("enabled"))
        if not enabled:
            text, tone = "未启用", "slate"
        elif ok:
            text, tone = (f"已连接 · {len(models)} 个模型" if models
                          else "已连接 · 无模型"), "green"
        else:
            text, tone = "连接失败", "red"
        try:
            fg, _bg = t.tone(tone)
            w["state"].configure(text=text, fg=fg)
        except Exception:  # noqa: BLE001
            try:
                w["state"].configure(text=text)
            except tk.TclError:
                pass
        if enabled and not ok and detail:
            w["url"].configure(highlightbackground=t.red_fg)
        else:
            try:
                w["url"].configure(highlightbackground=t.border)
            except tk.TclError:
                pass

    def _toggle_local(self, kind, value):
        self._svc.set_local(kind, enabled=value)
        if value:
            # 打开就立刻探测，让用户看到能不能连上
            self._probe_local(kind)
        else:
            self._paint_local_state(kind, False, "未启用", [])
            self._rebuild_local_list()
        if self._svc.running:
            self.app.toast("info", "需重启 API 服务以应用变更")

    def _on_url_changed(self, kind):
        """地址输入框失焦：保存并重新探测。"""
        w = self._local_widgets.get(kind)
        if not w:
            return
        url = w["url"].get().strip()
        cur = (self._svc.local.get_row(kind) or {}).get("base_url")
        if url == cur:
            return
        self._svc.set_local(kind, base_url=url)
        if (self._svc.local.get_row(kind) or {}).get("enabled"):
            self._probe_local(kind)

    def _probe_local(self, kind):
        w = self._local_widgets.get(kind)
        if w:
            try:
                w["state"].configure(text="探测中…")
            except tk.TclError:
                pass

        def done(k, ok, detail, models):
            # 后台线程回调 → 必须回主线程改控件
            self.app.ui_call(lambda: self._after_probe(k, ok, detail, models))

        self._svc.probe_local(kind, on_done=done)

    def _probe_all_local(self):
        for kind in self._local_widgets:
            self._probe_local(kind)

    def _after_probe(self, kind, ok, detail, models):
        w = self._local_widgets.get(kind)
        if w:
            w["models"] = list(models or [])
        self._paint_local_state(kind, ok, detail, models)
        self._rebuild_local_list()
        # 状态卡上的「本地模型」总数
        self._render_status(self._svc.status())

    def _rebuild_local_list(self):
        t = self.theme
        inner = self._local_list.inner
        for ch in inner.winfo_children():
            ch.destroy()
        # 本地模型清单变了，顺便把「默认模型」下拉框的候选同步过去
        self._refresh_model_menu()
        ids = self._svc.local_model_ids()
        cur = self._model_var.get()
        if not ids:
            # 区分「还没探测」与「探测了但没模型」，提示更有用
            probed = any((self._svc.local.get_row(k) or {}).get("checked_at")
                         for k in self._local_widgets)
            if probed:
                title, hint = ("暂无可用本地模型",
                               "后端已连接但没有模型，先 `ollama pull <模型>` 再探测")
            else:
                title, hint = ("尚未探测",
                               "点击右上「全部探测」获取本地模型列表")
            W.EmptyState(inner, t, icon="⌂", title=title,
                         hint=hint).pack(fill="both", expand=True,
                                         padx=4, pady=4)
            return
        for mid in ids:
            # 两行式：上一行模型名（可换行），下一行操作按钮。
            # 不并排的原因：模型 ID（如 local/ollama/moondream:latest）
            # 本身很长，右边再挂两个按钮会把右栏撑出卡片外。
            row = tk.Frame(inner, bg=t.bg_soft)
            row.pack(fill="x", pady=(1, 3))
            tk.Label(row, text=mid, font=t.f_mono, fg=t.fg, bg=t.bg_soft,
                     anchor="w", justify="left").pack(fill="x")
            acts = tk.Frame(row, bg=t.bg_soft)
            acts.pack(fill="x")
            if mid == cur:
                W.Badge(acts, t, "默认模型", tone="green").pack(side="left")
            else:
                W.button(acts, t, "设为默认",
                         command=lambda m=mid: self._set_default_model(m),
                         variant="outline", small=True).pack(side="left")
            W.button(acts, t, "复制", command=lambda m=mid: self._copy_text(m),
                     variant="ghost", small=True).pack(side="left", padx=(6, 0))

    def _save_local(self):
        for kind, w in self._local_widgets.items():
            self._svc.set_local(kind, base_url=w["url"].get().strip())
        # 一并把当前默认模型落盘（可能是本地模型，也可能是云端模型）
        self._svc.configure(model=self._model_var.get())
        self._refresh_model_menu()
        self.app.toast("success", "本地模型配置已保存")
        if self._svc.running:
            self.app.toast("info", "需重启 API 服务以应用变更")

    def _copy_local_models(self):
        ids = self._svc.local_model_ids()
        if not ids:
            self.app.toast("warn", "还没有可用的本地模型")
            return
        self._copy_text("\n".join(ids))

    def _copy_text(self, text):
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.app.toast("success", "已复制到剪贴板")
        except tk.TclError:
            self.app.toast("error", "复制失败")

    # ---------------- 云端转发 ----------------

    def _build_cloud(self, parent):
        """云端转发卡：告诉用户「账号池能不能真的转发」，并支持实测一次。"""
        t = self.theme
        card = W.Card(parent, t, padding=14)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self._cloud_card = card
        b = card.body

        head = tk.Frame(b, bg=t.bg_soft)
        head.pack(fill="x", pady=(0, 4))
        tk.Label(head, text="☁ 云端转发", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(side="left")
        self._cloud_badge = W.Badge(head, t, "探测中", tone="slate")
        self._cloud_badge.pack(side="right")

        tk.Label(b, text="把本机 Trae CN 的登录态转成上游 llm_utils_chat 请求，"
                         "消耗 IDE 积分（product_id 208）",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w",
                 justify="left", wraplength=400).pack(fill="x", pady=(0, 8))

        # 就绪度明细（可转发账号数 / 总数 / 云端模型数）
        self._cloud_info = tk.Label(b, text="", font=t.f_tiny, fg=t.fg_muted,
                                    bg=t.bg_soft, anchor="w", justify="left",
                                    wraplength=400)
        self._cloud_info.pack(fill="x", pady=(0, 8))

        # 实测输出区（只读、无边框，避免撑破卡片宽度）
        self._cloud_out = tk.Text(b, width=1, height=6, font=t.f_mono,
                                  bg=t.bg_input, fg=t.fg_muted, relief="flat",
                                  highlightthickness=1,
                                  highlightbackground=t.border, wrap="word")
        self._cloud_out.pack(fill="x")
        self._cloud_out.configure(state="disabled")

        btns = tk.Frame(b, bg=t.bg_soft)
        btns.pack(fill="x", pady=(8, 0))
        W.button(btns, t, "测试转发", command=self._test_cloud,
                 variant="primary", small=True, icon="▶").pack(side="left")
        W.button(btns, t, "刷新状态",
                 command=lambda: self._refresh_ai_status(fresh=True),
                 variant="ghost", small=True, icon="⟳").pack(side="left",
                                                            padx=(6, 0))
        W.button(btns, t, "查看池明细", command=self._show_pool_detail,
                 variant="ghost", small=True).pack(side="left", padx=(6, 0))

    def _paint_cloud(self, st, ai=None):
        """刷新云端转发卡（由 _render_status 调用）。"""
        t = self.theme
        badge = getattr(self, "_cloud_badge", None)
        info = getattr(self, "_cloud_info", None)
        if badge is None or not W.alive(badge):
            return
        fwd = int(st.get("pool_forwardable") or 0)
        total = int(st.get("pool_count") or 0)
        cloud_ready = bool(st.get("cloud_ready"))
        ai = ai if ai is not None else self._last_ai_status()
        prefs_local = bool((ai or {}).get("prefer_local", True))
        local_ready = bool((ai or {}).get("local_ready"))

        if cloud_ready:
            text, tone = "就绪", "green"
        elif total == 0:
            text, tone = "无账号", "amber"
        else:
            text, tone = "不可转发", "red"
        try:
            badge.set(text, tone=tone)
        except Exception:  # noqa: BLE001
            pass

        lines = [f"可转发账号：{fwd} / {total}（需要 Trae CN 登录态）",
                 f"云端模型：{'可用' if cloud_ready else '不可用'}"
                 f"　·　上游接口：llm_utils_chat"]
        if local_ready and prefs_local:
            lines.append("当前偏好：本地优先 → 云端仅作兜底")
        elif local_ready:
            lines.append("当前偏好：已关闭本地优先 → AI 调用走云端")
        else:
            lines.append("当前偏好：没有可用本地后端 → AI 调用走云端")
        try:
            info.configure(text="\n".join(lines))
        except tk.TclError:
            pass

    def _test_cloud(self):
        """实测一次转发：走真实的账号池 → 上游，展示实际去向与返回片段。"""
        if getattr(self, "_cloud_busy", False):
            self.app.toast("info", "上一次测试还在进行中")
            return
        self._cloud_busy = True
        self._cloud_write("== 正在测试云端转发（会消耗少量 IDE 积分）==\n")
        self.app.toast("info", "正在测试云端转发…")

        def work():
            try:
                from ..services import ai_dispatch as AD
                res = AD.dispatcher().ask("只回复两个字：可用",
                                          force_cloud=True, timeout=60.0)
                self.app.ui_call(lambda: self._cloud_test_done(res.as_dict()))
            except Exception as e:  # noqa: BLE001
                self.app.ui_call(lambda err=e: self._cloud_test_done(
                    {"ok": False, "error": repr(err)}))

        threading.Thread(target=work, daemon=True, name="api-cloud-test").start()

    def _cloud_test_done(self, res):
        self._cloud_busy = False
        if res.get("ok"):
            usage = res.get("usage") or {}
            tok = usage.get("total_tokens")
            self._cloud_write(
                "✔ 转发成功\n"
                f"  走向：{res.get('route')}　目标：{res.get('target')}\n"
                f"  耗时：{res.get('elapsed')}s"
                + (f"　tokens：{tok}" if tok else "") + "\n"
                f"  返回：{(res.get('text') or '')[:120]}\n")
            self.app.toast("success", "云端转发正常")
        else:
            self._cloud_write(
                "✘ 转发失败\n"
                f"  原因：{res.get('error') or '未知'}\n"
                "  提示：确认 Trae CN 已登录（池内账号需带登录态）。\n")
            self.app.toast("error", "云端转发失败，详见卡片输出")
        self._refresh_ai_status()

    def _cloud_write(self, text):
        w = getattr(self, "_cloud_out", None)
        if w is None or not W.alive(w):
            return
        try:
            w.configure(state="normal")
            w.delete("1.0", "end")
            w.insert("1.0", text)
            w.configure(state="disabled")
        except tk.TclError:
            pass

    def _show_pool_detail(self):
        """把账号池逐账号状态弹成模态框（表格放不下完整原因）。"""
        t = self.theme
        st = self._svc.status()
        rows = st.get("pool") or []
        m = W.Modal(self.app, t, "账号池明细", width=560, height=420)
        body = m.content
        holder = None

        if not rows:
            tk.Label(body, text="账号池为空：请先在左中栏选择账号。",
                     font=t.f_small, fg=t.fg_dim, bg=t.bg_soft,
                     anchor="w").pack(fill="x", pady=14)
        else:
            # 每个账号占 3~4 行、约 60px，而弹窗固定 420px 高（可用区约 338px）。
            # 实测 **7 个账号就溢出 +123px**，14 个溢出 +561px；Modal 自身不带
            # 滚动容器 —— 多出来的账号既看不到、也滚不动（滚轮在弹窗上没有落点）。
            # 所以这里必须自己套一个滚动容器。
            # 内容装得下时底色必须和弹窗一致（`m.content` 是 `bg_soft`），
            # 否则下方会露出一整块比弹窗深一档的异色区域。
            holder = W.ScrollFrame(body, t, bg=t.bg_soft)
            holder.pack(fill="both", expand=True)
            body = holder.inner
        for r in rows:
            st_key = r.get("state") or ("disabled" if r.get("disabled")
                                        else "cooling" if r.get("cooling")
                                        else "available")
            cn, tone = POOL_STATE_CN.get(st_key, (st_key, "slate"))
            box = tk.Frame(body, bg=t.bg_soft)
            box.pack(fill="x", pady=4)
            top = tk.Frame(box, bg=t.bg_soft)
            top.pack(fill="x")
            tk.Label(top, text=r.get("name") or "-", font=t.f_small, fg=t.fg,
                     bg=t.bg_soft, anchor="w").pack(side="left")
            W.Badge(top, t, cn, tone=tone).pack(side="left", padx=(6, 0))
            W.Badge(top, t, "可转发" if r.get("can_forward") else "不可转发",
                    tone="blue" if r.get("can_forward") else "slate").pack(
                side="left", padx=(6, 0))
            cr = r.get("credits")
            tk.Label(top, text=("积分 —" if cr is None else f"积分 {cr:.0f}"),
                     font=t.f_tiny, fg=t.fg_muted, bg=t.bg_soft).pack(
                side="right")
            tk.Label(box, text=f"{r.get('uid')}　·　{r.get('platform')}",
                     font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                     anchor="w").pack(fill="x")
            reason = r.get("cooldown_reason") or ""
            if reason:
                tk.Label(box, text=f"原因：{reason}", font=t.f_tiny,
                         fg=t.fg_muted, bg=t.bg_soft, anchor="w",
                         wraplength=480, justify="left").pack(fill="x")

        if holder is not None:
            # 行是循环里加进去的，<Configure> 去抖可能还没跑完 → 强制重算一次
            # scrollregion，否则刚打开时"滚不动"，要等下次尺寸变化才生效。
            holder.resync()
        W.button(m.footer, t, "关闭", command=m.close,
                 variant="outline").pack(side="right")
        self._modal = m           # 防 GC
        m.show()

    # ---------------- AI 诊断（本地优先）----------------

    def _build_diag(self, parent):
        """AI 诊断卡：规则先行 + 本地模型补充，动作需用户点确认才执行。"""
        t = self.theme
        card = W.Card(parent, t, padding=14)
        card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self._diag_card = card
        b = card.body

        head = tk.Frame(b, bg=t.bg_soft)
        head.pack(fill="x", pady=(0, 4))
        tk.Label(head, text="✚ AI 诊断（本地优先）", font=t.f_card_title,
                 fg=t.fg, bg=t.bg_soft, anchor="w").pack(side="left")
        self._ai_badge = W.Badge(head, t, "探测中", tone="slate")
        self._ai_badge.pack(side="right")

        tk.Label(b, text="签到出问题时，先由本地模型分析原因并给出建议动作；"
                         "动作必须你点「执行」才会真的跑",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w",
                 justify="left", wraplength=400).pack(fill="x", pady=(0, 6))

        # 本地优先开关 + 当前就绪度
        row = tk.Frame(b, bg=t.bg_soft)
        row.pack(fill="x", pady=(0, 4))
        tk.Label(row, text="AI 调用本地优先", font=t.f_small, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(side="left")
        self._prefer_sw = W.Switch(row, t, value=bool(self._svc.prefer_local),
                                   command=self._toggle_prefer_local)
        self._prefer_sw.pack(side="right")
        self._ai_ready_lbl = tk.Label(b, text="", font=t.f_tiny, fg=t.fg_dim,
                                      bg=t.bg_soft, anchor="w", justify="left",
                                      wraplength=400)
        self._ai_ready_lbl.pack(fill="x", pady=(0, 8))

        btns = tk.Frame(b, bg=t.bg_soft)
        btns.pack(fill="x", pady=(0, 6))
        self._diag_btn = W.button(btns, t, "诊断上次签到",
                                  command=self._run_diagnose,
                                  variant="primary", small=True, icon="✚")
        self._diag_btn.pack(side="left")
        W.button(btns, t, "用上一次结果重诊", command=self._run_diagnose,
                 variant="ghost", small=True).pack(side="left", padx=(6, 0))

        # 诊断结果容器
        self._diag_holder = tk.Frame(b, bg=t.bg_soft)
        self._diag_holder.pack(fill="both", expand=True)
        self._render_diag(None)

    def _toggle_prefer_local(self, value):
        self._svc.set_ai_prefs(prefer_local=bool(value))
        try:
            from ..services import ai_dispatch as AD
            AD.dispatcher().invalidate_probe()
        except Exception:  # noqa: BLE001
            pass
        self.app.toast("success",
                       "已开启本地优先：AI 调用默认走本地模型"
                       if value else "已关闭本地优先：AI 调用走云端账号池")
        # 刚 invalidate 过探测缓存 → 强制重探（`fresh=True` 仍然不阻塞：
        # 它只把占位值放进缓存并踢后台线程）
        self._refresh_ai_status(fresh=True)

    def _last_ai_status(self, fresh=False):
        """AI 调度状态（本地是否就绪）。★ **只读缓存，绝不在这里同步探测**。

        为什么（2026-09-22）：`ai_status()` 内部会去连本地后端
        （`local_backends.probe()` → `GET /api/tags`，`CONNECT_TIMEOUT` 2 秒）。
        它原先被渲染路径同步调用（`_render_status → _paint_cloud → 这里`），
        于是**每次进 API 服务页都要等 2 秒**（`perf_ui` 实测 avg 1.93s）。
        现在：第一次调用返回「正在探测…」占位并踢一个后台探测；探测完成后
        回主线程局部刷新。用户点「刷新状态」时传 `fresh=True` 强制重探。
        """
        cached = getattr(self, "_ai_status_cache", None)
        if cached is None or fresh:
            cached = {"local_ready": False,
                      "local_detail": "正在探测本地模型…",
                      "prefer_local": bool(getattr(self._svc, "prefer_local", True)),
                      "local_model": "", "_pending": True}
            self._ai_status_cache = cached
            self._kick_ai_probe()
        return cached

    def _kick_ai_probe(self):
        """后台探测本地后端可用性（单飞，避免连点叠很多个线程）。"""
        if getattr(self, "_ai_probing", False):
            return
        self._ai_probing = True

        def work():
            try:
                st = self._svc.ai_status()
            except Exception:  # noqa: BLE001
                st = {"local_ready": False, "local_detail": "调度器未就绪",
                      "prefer_local": bool(getattr(self._svc, "prefer_local", True))}

            def done():
                self._ai_probing = False
                self._ai_status_cache = st
                if W.alive(self):
                    self._refresh_ai_status()

            self.app.ui_call(done)

        threading.Thread(target=work, daemon=True, name="ai-status").start()

    def _refresh_ai_status(self, fresh=False):
        """刷新 AI 调度状态（本地是否就绪），同步开关与徽标。

        ★ 默认**不**重新探测（走缓存）；只有用户显式点「刷新状态」才 `fresh=True`。
        """
        ai = self._last_ai_status(fresh=fresh)
        t = self.theme
        badge = getattr(self, "_ai_badge", None)
        ready_lbl = getattr(self, "_ai_ready_lbl", None)
        sw = getattr(self, "_prefer_sw", None)

        if sw is not None and W.alive(sw):
            try:
                sw.set(bool(ai.get("prefer_local", True)))
            except Exception:  # noqa: BLE001
                pass
        if badge is not None and W.alive(badge):
            if ai.get("local_ready"):
                badge.set("本地就绪", tone="green")
            else:
                badge.set("本地未就绪", tone="slate")
        if ready_lbl is not None and W.alive(ready_lbl):
            if ai.get("local_ready"):
                txt = (f"✔ 本地模型可用：{ai.get('local_detail') or '已连接'}"
                       + (f"　默认：{ai.get('local_model')}"
                          if ai.get("local_model") else ""))
            else:
                txt = (f"本地模型不可用（{ai.get('local_detail') or '未启用'}），"
                       "诊断会自动回落到云端账号池")
            try:
                fg, _bg = t.tone("green" if ai.get("local_ready") else "amber")
            except Exception:  # noqa: BLE001
                fg = t.fg_dim
            try:
                ready_lbl.configure(text=txt, fg=fg)
            except tk.TclError:
                pass
        # 云端转发卡也带一份 AI 偏好信息
        try:
            self._paint_cloud(self._svc.status(), ai)
        except Exception:  # noqa: BLE001
            pass

    def _run_diagnose(self):
        if self._diag_busy:
            self.app.toast("info", "诊断正在进行中…")
            return
        results, logs = self._collect_checkin_evidence()
        if not results and not logs:
            self.app.toast("warn", "还没有签到记录，请先在「自动签到」页跑一次")
        self._diag_busy = True
        self._render_diag({"loading": True})

        def work():
            try:
                out = DG.diagnose(results=results, logs=logs,
                                  root=str(C.ROOT), use_ai=True,
                                  timeout=120.0)
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "summary": "诊断失败：%r" % e,
                       "causes": [], "suggestions": [], "actions": [],
                       "ai_used": False}
            self.app.ui_call(lambda res=out: self._diag_done(res))

        threading.Thread(target=work, daemon=True, name="api-diagnose").start()

    def _collect_checkin_evidence(self):
        """汇总「上次签到」的证据：签到页的结果 + 页面日志。

        优先拿签到页内存里的真实结果（含账号级状态），页面实例不在时
        退回 logs/history.jsonl 的近几条，让诊断在别的页也能用。
        """
        results, logs = [], []
        page = None
        try:
            page = getattr(self.app, "page", None)
            if page is not None and getattr(page, "view", None) != "checkin":
                page = None
        except Exception:  # noqa: BLE001
            page = None
        # 签到页实例可能不在当前视图，退而求其次读落盘记录
        if page is not None:
            results = list(getattr(page, "_results", []) or [])
            try:
                logs = page.log.get("1.0", "end").splitlines()
            except Exception:  # noqa: BLE001
                logs = []
        if not results:
            results = self._history_results(limit=40)
        return results, logs

    @staticmethod
    def _history_results(limit=40):
        """从 logs/history.jsonl 读最近若干条签到记录当作诊断证据。"""
        import json
        import os
        path = os.path.join(str(C.ROOT), "logs", "history.jsonl")
        if not os.path.exists(path):
            return []
        rows = []
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        rows.append(obj)
        except OSError:
            return []
        return rows[-limit:]

    def _diag_done(self, res):
        self._diag_busy = False
        self._diag_result = res
        import time as _t
        self._diag_stamp = _t.strftime("%H:%M:%S")
        self._render_diag(res)
        ai = res.get("ai") or {}
        if ai.get("ok"):
            self.app.toast("success",
                           f"诊断完成（{ai.get('describe') or 'AI 已分析'}）")
        elif res.get("ai", None):
            self.app.toast("warn", "AI 不可用，已给出规则诊断结论")
        else:
            self.app.toast("info", "诊断完成（规则结论）")
        self._refresh_ai_status()

    def _render_diag(self, res):
        t = self.theme
        box = self._diag_holder
        for w in box.winfo_children():
            w.destroy()
        if res is None:
            W.EmptyState(box, t, icon="✚", title="尚未诊断",
                         hint="点「诊断上次签到」开始分析").pack(
                fill="both", expand=True, padx=4, pady=6)
            return

        if res.get("loading"):
            W.EmptyState(box, t, icon="⏳", title="正在诊断…",
                         hint="本地模型优先；不可用时自动回落云端").pack(
                fill="both", expand=True, padx=4, pady=6)
            return

        # 走向标记（如实告诉用户这次分析用的是什么）
        ai = res.get("ai") or {}
        if ai.get("ok"):
            route = ai.get("route")
            where = "本地模型" if route == "local" else "云端账号池"
            meta = f"{where} · {ai.get('target') or ''}"
            tone = "green" if route == "local" else "blue"
        else:
            meta = "规则诊断（无 AI）"
            if ai.get("error"):
                meta += f" · AI 失败：{str(ai.get('error'))[:40]}"
            tone = "slate"
        top = tk.Frame(box, bg=t.bg_soft)
        top.pack(fill="x", pady=(0, 4))
        W.Badge(top, t, meta[:60], tone=tone).pack(side="left")
        if self._diag_stamp:
            tk.Label(top, text=self._diag_stamp, font=t.f_tiny, fg=t.fg_dim,
                     bg=t.bg_soft).pack(side="right")

        # 结论
        summary = res.get("summary") or "（无结论）"
        tk.Label(box, text="结论", font=t.f_small, fg=t.fg_muted, bg=t.bg_soft,
                 anchor="w").pack(fill="x", pady=(4, 0))
        tk.Label(box, text=summary, font=t.f_small, fg=t.fg, bg=t.bg_soft,
                 anchor="w", justify="left", wraplength=400).pack(
            fill="x", pady=(2, 6))

        # 可能原因
        causes = res.get("causes") or []
        if causes:
            tk.Label(box, text="可能原因", font=t.f_small, fg=t.fg_muted,
                     bg=t.bg_soft, anchor="w").pack(fill="x")
            for i, c in enumerate(causes[:5], 1):
                tk.Label(box, text=f"{i}. {c}", font=t.f_tiny, fg=t.fg_muted,
                         bg=t.bg_soft, anchor="w", justify="left",
                         wraplength=396).pack(fill="x", pady=(1, 0))

        # 建议动作
        sugg = res.get("suggestions") or []
        if sugg:
            tk.Label(box, text="建议动作", font=t.f_small, fg=t.fg_muted,
                     bg=t.bg_soft, anchor="w").pack(fill="x", pady=(6, 0))
            for i, s in enumerate(sugg[:5], 1):
                tk.Label(box, text=f"{i}. {s}", font=t.f_tiny, fg=t.fg_muted,
                         bg=t.bg_soft, anchor="w", justify="left",
                         wraplength=396).pack(fill="x", pady=(1, 0))

        # 可执行按钮（白名单动作，必须用户点击）
        actions = res.get("actions") or []
        if actions:
            tk.Label(box, text="可一键执行（需你确认）", font=t.f_small,
                     fg=t.fg_muted, bg=t.bg_soft, anchor="w").pack(
                fill="x", pady=(8, 2))
            wrap = tk.Frame(box, bg=t.bg_soft)
            wrap.pack(fill="x")
            for a in actions:
                key = a.get("key")
                label = a.get("label") or key
                risky = bool(a.get("risky"))
                W.button(wrap, t, ("⚠ " if risky else "") + label,
                         command=lambda k=key: self._run_action(k),
                         variant="danger" if risky else "outline",
                         small=True).pack(side="left", padx=(0, 4), pady=2)

    def _run_action(self, key):
        """执行诊断给出的动作。高风险动作要模态确认；动作本身绝不自动跑。"""
        desc = DG.describe_action(key)
        if DG.is_risky(key):
            t = self.theme
            m = W.Modal(self.app, t, "⚠ 高风险操作确认", width=480, height=250)
            tk.Label(m.content, text="该动作会改动本机设置或数据：", font=t.f_small,
                     fg=t.fg, bg=t.bg_soft, anchor="w").pack(fill="x")
            tk.Label(m.content, text=desc, font=t.f_tiny, fg=t.fg_muted,
                     bg=t.bg_soft, anchor="w", justify="left",
                     wraplength=430).pack(fill="x", pady=(4, 8))
            tk.Label(m.content, text="确定要继续吗？", font=t.f_small, fg=t.fg,
                     bg=t.bg_soft, anchor="w").pack(fill="x")

            def on_yes():
                m.close()
                self._do_action(key)

            W.button(m.footer, t, "取消", command=m.close,
                     variant="outline").pack(side="right", padx=(6, 0))
            W.button(m.footer, t, "确认执行", command=on_yes,
                     variant="danger").pack(side="right")
            self._modal = m           # 防 GC
            m.show()
            return
        self._do_action(key)

    def _do_action(self, key):
        """真正落地执行某个白名单动作。全部是项目已实现的安全操作。"""
        try:
            detail = self._dispatch_action(key)
        except Exception as e:  # noqa: BLE001
            detail = "执行失败：%r" % e
            self.app.toast("error", detail)
            return
        if detail:
            self.app.toast("info", detail)

    def _dispatch_action(self, key):
        """把动作 key 映射到项目既有能力上（不新造功能）。"""
        if key == "rescan_tokens":
            self._reload()
            return "已重新扫描本机 IDE 令牌"
        if key == "rescan_accounts":
            self.app.refresh_accounts()
            self._reload()
            return "正在后台重新发现账号…"
        if key == "open_client":
            return self._open_client()
        if key == "retry_checkin":
            return self._retry_checkin()
        if key == "open_logs":
            import os
            from ..services import device_reset as DR
            p = os.path.join(str(C.ROOT), "logs")
            DR.open_path(p)
            return "已打开日志目录"
        if key == "check_network":
            return self._check_network()
        if key == "enable_local_model":
            self._probe_all_local()
            self._refresh_ai_status()
            return "正在探测本地模型…"
        if key == "clear_state":
            out = DG.clear_today_state(str(C.ROOT))
            return out.get("detail") or "已处理"
        if key == "reset_device":
            return self._reset_device()
        return "未知动作：%s" % key

    def _open_client(self):
        import os
        import subprocess
        cands = [
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Trae CN\Trae CN.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Trae\Trae.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\Trae CN\Trae CN.exe"),
        ]
        for p in cands:
            if p and os.path.exists(p):
                try:
                    subprocess.Popen([p])  # noqa: S603
                    return "已启动 Trae 客户端"
                except OSError as e:
                    return "启动失败：%s" % e
        return "未找到 Trae 客户端安装路径，请手动打开"

    def _retry_checkin(self):
        """触发一次「强制执行」签到（等价签到页的强制模式）。"""
        try:
            self.app.goto(self.app.module, "checkin")
        except Exception:  # noqa: BLE001
            pass
        try:
            page = self.app.page
            if hasattr(page, "start_checkin"):
                try:
                    page._force.set(True)
                except Exception:  # noqa: BLE001
                    pass
                page.start_checkin(skip_done=False)
                return "已发起重试签到（强制执行）"
        except Exception as e:  # noqa: BLE001
            return "发起签到失败：%r" % e
        return "已切到「自动签到」页，请手动点开始"

    def _check_network(self):
        out = DG.check_networks()
        lines = [f"{r['host']}: {'✔' if r['ok'] else '✘'} "
                 f"{r['dns']} / {r['tcp']}" for r in out.get("rows") or []]
        self._cloud_write("== 网络连通性 ==\n" + "\n".join(lines) + "\n")
        if out.get("ok"):
            return f"网络正常（{out.get('reachable')}/{out.get('total')} 可达）"
        return (f"有 {out.get('total', 0) - out.get('reachable', 0)} 个域名不可达，"
                "详见云端转发卡输出")

    def _reset_device(self):
        """重置 6 层设备指纹（复用 settings 页同一实现，自动备份）。

        这是本页唯一会改动本机的动作，所以只在用户点过「确认执行」后才会
        走到这里；重置结果同步写进诊断卡上方的输出区，便于核对。
        """
        from ..services import device_reset as DR
        try:
            ok, lines, bdir = DR.reset(backup=True)
        except TypeError:
            # 兼容签名差异：不带 backup 关键字时退回默认参数
            ok, lines, bdir = DR.reset()
        except Exception as e:  # noqa: BLE001
            return "重置失败：%r" % e
        tail = "".join(lines[-12:]) if lines else ""
        self._cloud_write("== 设备标识重置 ==\n" + tail
                          + f"\n备份目录：{bdir}\n")
        if ok:
            return ("设备标识已重置（已备份到 %s），请重启 Trae 客户端并重新登录"
                    % (bdir or "backups 目录"))
        return "重置未完全成功，详见云端转发卡输出"

    # ---------------- 执行池动作 ----------------
        if var.get():
            self._enabled.add(uid)
        else:
            self._enabled.discard(uid)
        self._sync_pool_count()

    def _select_all(self, on):
        uids = list(self._pool_rows.keys())
        self._enabled = set(uids) if on else set()
        for uid, var in self._pool_rows.items():
            var.set(uid in self._enabled)
        self._sync_pool_count()

    def _sync_pool_count(self):
        self._pool_count_lbl.configure(
            text=f"已选 {len(self._enabled)} / {len(self._pool_rows)}")

    # ---------------- 动作 ----------------

    def _on_toggle(self):
        if self._svc.running:
            self._svc.stop()
            self.app.toast("info", "API 服务已停止")
        else:
            if not self._enabled:
                self.app.toast("warn", "请先选择至少一个账号")
                return
            try:
                port = int(self._port_entry.get().strip() or A.DEFAULT_PORT)
            except ValueError:
                self.app.toast("warn", "端口必须是数字")
                return
            self._save_cfg(silent=True)
            self._svc.set_pool(self._enabled)
            try:
                st = self._svc.start()
            except OSError as e:
                self.app.toast("error", f"启动失败（端口被占用？）：{e}")
                return
            self.app.toast("success",
                           f"API 服务已启动（端口 {st['port']}，"
                           f"池内 {st['pool_count']} 个账号）")
        self._render_status(self._svc.status())

    def _save_cfg(self, silent=False):
        try:
            port = int(self._port_entry.get().strip() or A.DEFAULT_PORT)
        except ValueError:
            if not silent:
                self.app.toast("warn", "端口必须是数字")
            return
        self._svc.configure(port=port,
                            api_key=self._key_entry.get().strip(),
                            model=self._model_var.get())
        self._paint_route_hint()
        if not silent:
            model = self._model_var.get()
            where = "本地后端" if self._svc.is_local_model(model) else "账号池"
            self.app.toast("success", f"配置已保存（默认模型走{where}）")

    def _save_pool(self):
        self._svc.set_pool(self._enabled)
        self.app.toast("success", "账号池已更新")
        if self._svc.running:
            self.app.toast("info", "需重启 API 服务以应用变更")

    def _reload(self):
        self._svc.reload_accounts()
        self._enabled = set(self._svc.pool_uids())
        self._rebuild_pool_list()
        self._render_status(self._svc.status())
        self.app.toast("success", "账号列表已刷新")

    def _copy_example(self):
        port = self._port_entry.get().strip() or str(A.DEFAULT_PORT)
        model = self._model_var.get()
        key = self._key_entry.get().strip()
        masked = A.mask_api_key(key) if key else "（留空则不鉴权）"
        if self._svc.is_local_model(model):
            route = "本地后端（Ollama / llama.cpp），不消耗积分"
        else:
            route = "账号池（云端 IDE 积分）"
        example = (f"# 默认模型走向：{route}\n"
                   f"接口地址: http://127.0.0.1:{port}/v1\n"
                   f"API Key:  {masked}\n"
                   f"模型 ID:  {model}\n\n"
                   f"curl -X POST http://127.0.0.1:{port}/v1/chat/completions \\\n"
                   f'  -H "Content-Type: application/json" \\\n'
                   f'  -H "Authorization: Bearer {masked}" \\\n'
                   f"  -d '{{\"model\": \"{model}\", "
                   f"\"messages\": [{{\"role\": \"user\", "
                   f"\"content\": \"你好\"}}]}}'")
        try:
            self.clipboard_clear()
            self.clipboard_append(example)
            self.app.toast("success", "配置示例已复制到剪贴板")
        except tk.TclError:
            self.app.toast("error", "复制失败")

    # ---------------- 渲染 ----------------

    def _render_status(self, st):
        t = self.theme
        running = st.get("running")
        # 卡片
        self._cards["state"].set("运行中" if running else "已停止",
                                  hint=(f"127.0.0.1:{st.get('port')}"
                                        if running else "未启动"),
                                  tone="green" if running else "slate")
        self._cards["reqs"].set(str(st.get("total_requests", 0)),
                                hint="累计处理的 API 调用")
        au = st.get("active_uid")
        self._cards["active"].set((au[:12] + "…" if au and len(au) > 12
                                   else (au or "—")),
                                  hint="当前正在处理请求" if au else "空闲")
        self._cards["pool"].set(str(st.get("pool_count", 0)),
                                hint="已选入轮转池的账号数")
        # 本地模型：只统计「已启用」的后端上报的模型数
        local_rows = st.get("local") or []
        enabled_rows = [r for r in local_rows if r.get("enabled")]
        n_local = len(st.get("local_models") or [])
        if not enabled_rows:
            hint, tone = "未启用本地后端", "slate"
        elif any(r.get("ok") for r in enabled_rows):
            hint, tone = "Ollama / llama.cpp 已连接", "green"
        else:
            hint, tone = "已启用但连不上后端", "amber"
        self._cards["local"].set(str(n_local), hint=hint, tone=tone)

        # 动作按钮
        self._act_btn.configure(
            text="■ 停止服务" if running else "▶ 启动服务")
        self._act_btn.configure(
            command=self._on_toggle)

        # 错误条
        err = st.get("last_error")
        if err:
            self._err_lbl.configure(text="✕ " + str(err))
            self.err_bar.pack(fill="x", pady=(12, 0), after=self.stats_box)
        else:
            self.err_bar.pack_forget()

        # 使用示例
        port = st.get("port") or A.DEFAULT_PORT
        key = self._key_entry.get().strip()
        masked = A.mask_api_key(key) if key else "（留空则不鉴权）"
        model = self._model_var.get()
        local_ids = st.get("local_models") or []
        if self._svc.is_local_model(model):
            kind, name = LB.split_model(model)
            route = (f"→ 本地后端：{LB.KIND_LABELS.get(kind, kind)}"
                     f"（{name}）· 不耗积分")
        else:
            route = "→ 账号池：云端 IDE 积分"
        n_other = len([m for m in local_ids if m != model])
        local_line = (f"\n其它本地模型：{n_other} 个（见右侧清单）"
                      if n_other else "")
        self._usage.configure(
            text=f"接口地址：http://127.0.0.1:{port}/v1\n"
                 f"API Key： {masked}\n"
                 f"模型 ID： {model}\n"
                 f"{route}{local_line}\n"
                 f"GET  http://127.0.0.1:{port}/v1/models\n"
                 f"GET  http://127.0.0.1:{port}/health")
        self._paint_route_hint()

        # 池实时状态
        rows = st.get("pool") or []
        for w in self.pool_body.winfo_children():
            w.destroy()
        if running and rows:
            self.pool_card.pack(fill="x", pady=(12, 0))
            head = tk.Frame(self.pool_body, bg=t.bg_soft)
            head.pack(fill="x", pady=(0, 6))
            tk.Label(head, text="✓ 池实时状态", font=t.f_card_title, fg=t.fg,
                     bg=t.bg_soft, anchor="w").pack(side="left")
            tk.Label(head, text=f"可转发 {st.get('pool_forwardable', 0)} / "
                                f"{len(rows)}",
                     font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft).pack(
                side="left", padx=(8, 0))
            W.button(head, t, "清除冷却", command=self._clear_cd,
                     variant="ghost", small=True, icon="⌫").pack(side="right")
            W.button(head, t, "账号池明细", command=self._show_pool_detail,
                     variant="ghost", small=True).pack(side="right",
                                                        padx=(0, 6))

            cols = [("账号", 180), ("UID", 150), ("积分", 70),
                    ("状态", 80), ("转发", 60), ("错误", 60),
                    ("冷却原因", 190)]
            hdr = tk.Frame(self.pool_body, bg=t.bg_soft)
            hdr.pack(fill="x")
            for name, w_ in cols:
                tk.Label(hdr, text=name, font=t.f_tiny, fg=t.fg_dim,
                         bg=t.bg_soft, width=max(6, w_ // 9),
                         anchor="w").pack(side="left")
            for r in rows:
                row = tk.Frame(self.pool_body, bg=t.bg_soft)
                row.pack(fill="x", pady=1)
                state, tone = self._pool_state(r)
                vals = [
                    (r["name"], t.fg, 20),
                    (r["uid"][:14] + "…", t.fg_dim, 16),
                    ("—" if r.get("credits") is None
                     else f"{r['credits']:.0f}", t.fg_muted, 8),
                ]
                for text, fg, w_ in vals:
                    tk.Label(row, text=text, font=t.f_tiny, fg=fg,
                             bg=t.bg_soft, width=w_, anchor="w").pack(side="left")
                bd = W.Badge(row, t, state, tone=tone)
                bd.pack(side="left", padx=(2, 6))
                # 「转发」列：能不能真的走上游（需要登录态）
                W.Badge(row, t, "是" if r.get("can_forward") else "否",
                        tone="blue" if r.get("can_forward") else "slate").pack(
                    side="left", padx=(0, 6))
                tk.Label(row, text=str(r.get("err_count", 0)), font=t.f_tiny,
                         fg=t.fg_muted, bg=t.bg_soft, width=6,
                         anchor="w").pack(side="left")
                tk.Label(row, text=(r.get("cooldown_reason") or "—")[:24],
                         font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                         anchor="w").pack(side="left", fill="x", expand=True)
        else:
            self.pool_card.pack_forget()

        # 云端转发卡（含 AI 偏好）
        self._paint_cloud(st)

        # 运行中开启轮询
        self._stop_poll()
        if running:
            self._poll_id = self.after(POLL_MS, self._tick)

    @staticmethod
    def _pool_state(r):
        """把账号池的 row 映射成 (中文状态, tone)。

        新 schema 直接给 `state`（available/no_credential/cooling/expired/
        no_credits/disabled）；老 row 只有 disabled/cooling 两个布尔，这里
        做兼容回退，保证两种数据都能正确渲染。
        """
        key = r.get("state")
        if not key:
            if r.get("disabled"):
                key = "disabled"
            elif r.get("cooling"):
                key = "cooling"
            else:
                key = "available"
        return POOL_STATE_CN.get(key, (str(key), "slate"))

    def _tick(self):
        try:
            self._render_status(self._svc.status())
        except Exception:  # noqa: BLE001
            pass

    def _stop_poll(self):
        if self._poll_id:
            try:
                self.after_cancel(self._poll_id)
            except Exception:  # noqa: BLE001
                pass
            self._poll_id = None

    def _clear_cd(self):
        n = self._svc.clear_cooldowns()
        if n:
            self.app.toast("success", f"已清除 {n} 个账号的冷却状态")
        else:
            self.app.toast("info", "当前无冷却中的账号")
        self._render_status(self._svc.status())

    # ---------------- 生命周期 ----------------

    def on_show(self):
        # 用持久化的配置回填端口 / Key / 默认模型（下次启动保持一致）
        try:
            self._port_entry.delete(0, "end")
            self._port_entry.insert(0, str(self._svc.port))
            self._key_entry.delete(0, "end")
            self._key_entry.insert(0, self._svc.api_key)
        except tk.TclError:
            pass
        self._model_var.set(self._svc.model)
        self._refresh_model_menu()
        self._rebuild_pool_list()
        self._rebuild_local_list()
        self._render_status(self._svc.status())
        # 进页面时刷新一次 AI 调度状态（探测本地后端 + 同步本地优先开关）
        self._refresh_ai_status()

    def on_hide(self):
        self._stop_poll()
