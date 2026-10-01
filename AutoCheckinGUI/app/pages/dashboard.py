#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""概览页：统计卡 + 今日账号状态列表 + 最近记录。

对齐参考项目 Dashboard 的结构：顶部四张统计卡（账号总数 / 今日已签 /
待处理 / 令牌告警），下方是账号签到状态明细与最近签到流水。
"""
import os
import time
import tkinter as tk

from .. import config as C
from ..services import credits as CR
from ..services import scheduler
from ..ui import widgets as W
from .base import BasePage

MODULE_CN = {"traework": "TraeWork", "traecn": "TraeCN", "workbuddy": "WorkBuddy"}


class DashboardPage(BasePage):
    title = "概览"
    desc = "多账号签到工作台 · 一眼掌握状态与快捷入口"

    def build(self):
        self.add_action("刷新", self.reload, variant="outline", icon="⟳")
        self.add_action("立即签到", self.go_checkin, variant="primary", icon="▶")

        t = self.theme
        self.set_title(f"{MODULE_CN.get(self.module, '')} 概览",
                       "多账号签到工作台 · 一眼掌握状态与快捷入口")

        # ---- 配置导航（未完成初始化时显示；全部完成后自动隐藏）----
        self.guide_box = tk.Frame(self.body, bg=t.bg)
        self._guide = None

        # ---- 统计卡 ----
        grid = tk.Frame(self.body, bg=t.bg)
        grid.pack(fill="x")
        for i in range(4):
            grid.grid_columnconfigure(i, weight=1, uniform="stat")
        self.cards = {}
        specs = [("total", "账号总数", "brand"), ("done", "今日已签", "green"),
                 ("pending", "待处理", "amber"), ("warn", "令牌告警", "red")]
        for i, (key, label, tone) in enumerate(specs):
            c = W.StatCard(grid, t, label, "-", "", tone)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 5, 0))
            self.cards[key] = c

        # ---- 计划任务提示条 ----
        self.task_box = tk.Frame(self.body, bg=t.bg)
        self.task_box.pack(fill="x", pady=(12, 0))

        # ---- 账号状态 ----
        W.section_title(self.body, t, "账号签到状态",
                        "同一份 state.json，与计划任务共享").pack(
            fill="x", pady=(16, 6))
        self.acc_box = tk.Frame(self.body, bg=t.bg)
        self.acc_box.pack(fill="x")

        # ---- 最近记录 ----
        W.section_title(self.body, t, "最近签到记录",
                        "来自 logs/history.jsonl").pack(fill="x", pady=(18, 6))
        self.hist_box = tk.Frame(self.body, bg=t.bg)
        self.hist_box.pack(fill="x")

        self.on_show()

    def on_show(self):
        self.reload()

    def on_accounts_updated(self):
        """账号缓存后台刷新完成后由 App 调用。"""
        self.reload()

    def on_task_updated(self):
        """计划任务状态后台刷新完成后由 App 调用。

        任务状态是异步取的（避免同步 PowerShell 卡住渲染），
        拿到结果后只重画任务卡片与引导，不整页 reload。
        """
        try:
            self._render_task_box()
            self._render_guide()
        except Exception:  # noqa: BLE001
            pass

    def go_checkin(self):
        self.app._show(self.module, "checkin")

    # ---------------- 数据加载 ----------------

    def reload(self):
        try:
            cfg = C.load_config()
            state = C.load_state()
            today = C.today_str()
            # 读缓存，不阻塞；无缓存时 App 会在后台刷新后回调 on_accounts_updated
            accs = self.app.accounts().get(self.module, [])
            total = len(accs)
            done = sum(1 for a in accs if state.get(a["key"]) == today)
            pending = total - done
            warned = 0
            for a in accs:
                exp = a.get("expires_at") or a.get("exp") or 0
                if exp:
                    ms = exp if exp > 1e12 else exp * 1000
                    if 0 < (ms - time.time() * 1000) < 7 * 86400_000:
                        warned += 1

            self.cards["total"].set(total, f"平台：{MODULE_CN.get(self.module, '')}")
            self.cards["done"].set(f"{done}/{total}" if total else "0/0",
                                   "全部完成" if total and done == total else "尚未开始",
                                   "green" if total and done == total else "slate")
            self.cards["pending"].set(pending,
                                      "今日待签到" if pending else "无需处理",
                                      "amber" if pending else "slate")
            self.cards["warn"].set(warned, "7 天内临期" if warned else "令牌正常",
                                   "red" if warned else "slate")

            self._render_task_box()
            self._render_guide()
            self._render_accounts(accs, state, today)
            self._render_history()
        except Exception as e:  # noqa: BLE001
            self.app.toast("error", f"读取概览数据失败：{e!r}")

    def _render_guide(self):
        """未完成初始化时展示配置导航；已全部完成则不占用版面。

        性能：先问一遍各步骤是否都完成了，**全完成就直接不构造组件**。
        原实现是先把整个 SetupGuide 建出来再判断，等于每次渲染概览都白建
        一整套控件（十几个 Label/Badge/Button），这是切回概览页时的隐性开销。
        """
        for w in self.guide_box.winfo_children():
            w.destroy()
        self._guide = None
        try:
            from ..ui.setup_guide import SetupGuide, all_steps_done
            if all_steps_done():
                self.guide_box.pack_forget()      # 全部完成，彻底不占位
                return
            g = SetupGuide(self.guide_box, self.theme, self.app)
            g.pack(fill="x", pady=(12, 0))
            self._guide = g
            self.guide_box.pack(fill="x")
        except Exception:  # noqa: BLE001
            pass

    def _render_task_box(self):
        for w in self.task_box.winfo_children():
            w.destroy()
        t = self.theme
        # 读缓存（App 启动时已异步预热），避免这里同步起 PowerShell（约 1s）
        info = scheduler.cached_task_info() or {}
        cfg = C.load_config()
        times = cfg.get("schedule", {}).get("daily_times", [])
        card = W.Card(self.task_box, t, accent="blue", padding=12)
        card.pack(fill="x")
        b = card.body
        row = tk.Frame(b, bg=t.bg_soft)
        row.pack(fill="x")
        if info.get("installed"):
            dot, tone = "●", "green"
            text = f"计划任务已启用 · 每日 {', '.join(times) or '未设置'}"
            sub = f"上次运行 {info.get('lastRun') or '-'} · 结果码 {info.get('result') or '-'}"
            if cfg.get("schedule", {}).get("run_at_logon", True):
                sub += " · 开机自启"
        else:
            dot, tone = "○", "slate"
            text = "计划任务未安装"
            sub = "安装后可在指定时间自动签到，并在开机后自动补跑"
        fg, _ = t.tone(tone)
        tk.Label(row, text=dot, font=t.f_small, fg=fg, bg=t.bg_soft).pack(side="left")
        tk.Label(row, text=text, font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft).pack(side="left", padx=(6, 0))
        tk.Label(row, text=sub, font=t.f_tiny, fg=t.fg_dim,
                 bg=t.bg_soft).pack(side="left", padx=(12, 0))
        W.button(row, t, "去设置", command=lambda: self.app.goto(self.module, "settings"),
                 variant="outline", small=True).pack(side="right")

    def _render_accounts(self, accs, state, today):
        # ★ 输入没变就不重建（2026-09-22）：这一块会 destroy + 新建约 96 个控件，
        #   而 Tk 的每次 destroy/create 都是一次 Tcl 往返（实测本页 341 ms，
        #   其中 `destroy` 一项就占 66 ms）。进页面时数据通常一模一样，白重建。
        #   签名里带上 `_token_note(a)`，保证「剩余 X 天」这类文案仍然会更新。
        try:
            sig = (self.module, bool(self.theme.dark), today,
                   tuple((a.get("key"), a.get("display"), a.get("phone"),
                          a.get("profile"), a.get("uid"),
                          bool(state.get(a.get("key")) == today),
                          self._token_note(a)) for a in accs))
        except Exception:  # noqa: BLE001
            sig = None
        if sig is not None and sig == getattr(self, "_acc_sig", None):
            return
        self._acc_sig = sig
        for w in self.acc_box.winfo_children():
            w.destroy()
        t = self.theme
        if not accs:
            loading = C.peek_accounts() is None
            W.EmptyState(self.acc_box, t, "◍",
                         "正在读取账号…" if loading else
                         f"还没有 {MODULE_CN.get(self.module, '')} 账号",
                         "账号信息会在后台读取完成后自动出现。"
                         if loading else
                         "请到「账号管理」导入或添加账号后再发起签到。").pack(fill="x")
            return
        card = W.Card(self.acc_box, t, padding=0)
        card.pack(fill="x")
        box = card.body
        for i, a in enumerate(accs):
            if i:
                W.hline(box, t).pack(fill="x")
            row = tk.Frame(box, bg=t.bg_soft)
            row.pack(fill="x", padx=14, pady=9)
            done = state.get(a["key"]) == today
            tk.Label(row, text="◍" if done else "○", font=t.f_small,
                     fg=t.green_fg if done else t.fg_dim,
                     bg=t.bg_soft).pack(side="left")
            name_box = tk.Frame(row, bg=t.bg_soft)
            name_box.pack(side="left", padx=(8, 0), fill="x", expand=True)
            tk.Label(name_box, text=a["display"], font=t.f_body, fg=t.fg,
                     bg=t.bg_soft, anchor="w").pack(anchor="w")
            sub = a.get("phone") or a.get("uid") or a.get("profile") or ""
            if a["platform"] == "workbuddy":
                sub = a.get("phone") or a["uid"][:12] + "…"
            elif a["platform"] == "traework":
                sub = a.get("profile") or ""
            elif a["platform"] == "traecn":
                sub = f"uid …{a['uid'][-6:]}"
            if sub:
                tk.Label(name_box, text=sub, font=t.f_tiny, fg=t.fg_dim,
                         bg=t.bg_soft, anchor="w").pack(anchor="w")

            # 令牌信息 / 状态徽标
            extra = self._token_note(a)
            if extra:
                txt, tone = extra
                W.Badge(row, t, txt, tone).pack(side="right", padx=(6, 0))
            W.Badge(row, t, "今日已签" if done else "待签到",
                    "green" if done else "slate").pack(side="right")

    def _token_note(self, a):
        """返回 (文案, tone) 或 None。"""
        if a["platform"] == "workbuddy":
            exp = a.get("expires_at") or 0
            if not exp:
                return ("无需令牌", "slate")
            days = (exp - time.time() * 1000) / 86400000
            if days < 0:
                return ("令牌已过期", "red")
            if days <= 7:
                return (f"令牌 {max(1, round(days))} 天到期", "amber")
            return (f"令牌 {round(days)} 天有效", "green")
        if a["platform"] == "traecn":
            exp = a.get("exp") or 0
            if not exp:
                return ("无令牌", "red")
            days = (exp - time.time()) / 86400
            if days < 0:
                return ("令牌已过期", "red")
            if days <= 7:
                return (f"令牌 {max(1, round(days))} 天到期", "amber")
            return ("令牌有效", "green")
        if a["platform"] == "traework":
            return ("会话已保存" if a.get("has_profile") else "未登录", 
                    "green" if a.get("has_profile") else "amber")
        return None

    def _render_history(self):
        # ★ 同上：流水文件没变就不重建（12 行 × 4 控件）
        sig = (self.module, bool(self.theme.dark))
        try:
            st = os.stat(C.HISTORY_PATH)
            sig += (st.st_mtime_ns, st.st_size)
        except OSError:
            sig += (None, None)
        if sig == getattr(self, "_hist_sig", None):
            return
        self._hist_sig = sig
        for w in self.hist_box.winfo_children():
            w.destroy()
        t = self.theme
        rows = [r for r in C.load_history(limit=120)
                if r.get("platform") == self.module][-12:]
        rows.reverse()
        if not rows:
            W.EmptyState(self.hist_box, t, "☰", "暂无签到记录",
                         "执行一次签到后，流水会显示在这里，同时写入 logs/签到记录.csv。").pack(fill="x")
            return
        card = W.Card(self.hist_box, t, padding=0)
        card.pack(fill="x")
        box = card.body
        for i, r in enumerate(rows):
            if i:
                W.hline(box, t).pack(fill="x")
            row = tk.Frame(box, bg=t.bg_soft)
            row.pack(fill="x", padx=14, pady=7)
            ts = str(r.get("ts") or "")[5:16].replace("T", " ")
            tk.Label(row, text=ts, font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                     width=13, anchor="w").pack(side="left")
            tk.Label(row, text=str(r.get("account") or "-"), font=t.f_small,
                     fg=t.fg, bg=t.bg_soft, width=14, anchor="w").pack(side="left")
            st = r.get("status")
            W.Badge(row, t, C.STATUS_CN.get(st, str(st)),
                    C.STATUS_TONE.get(st, "slate")).pack(side="right")
            detail = str(r.get("detail") or "")
            if len(detail) > 52:
                detail = detail[:51] + "…"
            tk.Label(row, text=detail, font=t.f_tiny, fg=t.fg_muted, bg=t.bg_soft,
                     anchor="w").pack(side="left", padx=(10, 0), fill="x", expand=True)
