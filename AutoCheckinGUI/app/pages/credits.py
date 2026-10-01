#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实用数据看板：把「积分」页改成可用的签到健康与收益概览。

额度接口只在部分平台/账号可用，不能让整页依赖一个经常为空的接口。
本页优先展示本地必然存在的 state.json、history.jsonl、账号会话和计划任务，
额度查询结果作为账号明细的补充信息；因此即使额度接口未开放，页面也不会留大块空白。
"""
import datetime as _dt
import re
import threading
import tkinter as tk

from .. import config as C
from ..services import credits as CR
from ..services import scheduler
from ..ui import widgets as W
from .base import BasePage

MODULE_CN = {"traework": "TraeWork", "traecn": "TraeCN", "workbuddy": "WorkBuddy"}
_REWARD_RE = re.compile(
    r"(?:领取|获得|到账|奖励)\s*\+?\s*([0-9]+(?:\.[0-9]+)?)\s*积分"
    r"|\+\s*([0-9]+(?:\.[0-9]+)?)\s*积分"
)


class CreditsPage(BasePage):
    """保留原路由，内容改为「签到健康 + 近期收益 + 可用性明细」。"""

    title = "积分看板"

    def build(self):
        t = self.theme
        module_name = MODULE_CN.get(self.module, "")
        self.set_title(f"{module_name} 积分看板",
                       "签到健康、近期收益与账号可用性一页掌握")
        self.busy = False
        self.add_action("刷新看板", self.reload, variant="primary", icon="⟳")

        grid = tk.Frame(self.body, bg=t.bg)
        grid.pack(fill="x")
        for i in range(4):
            grid.grid_columnconfigure(i, weight=1, uniform="stat")
        self.cards = {}
        specs = [
            ("accounts", "账号数", "brand"),
            ("today", "今日已签", "green"),
            ("success", "近 7 日成功率", "blue"),
            ("reward", "近 7 日领取", "amber"),
        ]
        for i, (key, label, tone) in enumerate(specs):
            c = W.StatCard(grid, t, label, "-", "", tone)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 5, 0))
            self.cards[key] = c

        W.section_title(self.body, t, "签到健康", "使用本地状态与历史记录，不依赖额度接口").pack(
            fill="x", pady=(16, 6))
        self.health_box = tk.Frame(self.body, bg=t.bg)
        self.health_box.pack(fill="x")

        W.section_title(self.body, t, "近期收益与签到记录",
                        "近 7 天；积分以 history.jsonl 中的实际奖励文案为准").pack(
            fill="x", pady=(18, 6))
        self.recent_box = tk.Frame(self.body, bg=t.bg)
        self.recent_box.pack(fill="x")

        W.section_title(self.body, t, "账号可用性与额度查询",
                        "额度接口未开放时仍保留账号、会话和令牌状态").pack(
            fill="x", pady=(18, 6))
        self.list_box = tk.Frame(self.body, bg=t.bg)
        self.list_box.pack(fill="x")

        self.on_show()

    def on_show(self):
        self.reload()

    def reload(self):
        if self.busy:
            return
        t = self.theme
        self.busy = True
        for box in (self.health_box, self.recent_box, self.list_box):
            for w in box.winfo_children():
                w.destroy()
        tk.Label(self.list_box, text="正在刷新账号可用性…", font=t.f_small,
                 fg=t.fg_dim, bg=t.bg).pack(anchor="w")

        module = self.module

        def work():
            try:
                if module == "workbuddy":
                    data = CR.query_workbuddy()
                elif module == "traecn":
                    data = CR.query_traecn()
                else:
                    data = self._query_traework()
            except Exception as e:  # noqa: BLE001
                data = [{"platform": module, "id": "-", "name": "-", "ok": False,
                         "totalRemaining": None, "error": repr(e)}]

            def deliver():
                if not W.alive(self):
                    self.busy = False
                    return
                self._render(data)

            self.app.ui_call(deliver)

        threading.Thread(target=work, daemon=True,
                         name="credits-refresh").start()

    def _query_traework(self):
        """TraeWork 额度仍需浏览器会话读取；本页用本地健康数据兜底。"""
        accs = self.app.accounts().get("traework", [])
        return [{
            "platform": "traework", "id": a["id"], "name": a["display"],
            "ok": False, "unsupported": True, "totalRemaining": None,
            "error": "需在浏览器会话中查看，暂不支持自动查询",
        } for a in accs]

    # ---------------- 本地实用数据 ----------------

    @staticmethod
    def _reward(detail):
        m = _REWARD_RE.search(str(detail or ""))
        if not m:
            return 0.0
        try:
            return float(m.group(1) or m.group(2) or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _row_date(row):
        raw = str(row.get("ts") or "")
        try:
            return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            try:
                return _dt.date.fromisoformat(raw[:10])
            except (TypeError, ValueError):
                return None

    def _history_stats(self):
        today = _dt.date.today()
        start = today - _dt.timedelta(days=6)
        rows = []
        for row in C.load_history(limit=500):
            if row.get("platform") != self.module:
                continue
            d = self._row_date(row)
            if d is not None and start <= d <= today:
                rows.append(row)
        ok = sum(1 for r in rows if r.get("status") in ("ok", "skip"))
        busy = sum(1 for r in rows if r.get("status") == "busy")
        fail = sum(1 for r in rows if r.get("status") not in ("ok", "skip", "busy"))
        reward = sum(self._reward(r.get("detail")) for r in rows)
        return {
            "rows": rows,
            "attempts": len(rows),
            "ok": ok,
            "busy": busy,
            "fail": fail,
            "reward": reward,
        }

    def _next_run(self, cfg):
        times = cfg.get("schedule", {}).get("daily_times", [])
        parsed = []
        for item in times:
            try:
                parsed.append(_dt.datetime.strptime(str(item), "%H:%M").time())
            except ValueError:
                continue
        if not parsed:
            return "未设置"
        now = _dt.datetime.now()
        for tm in sorted(parsed):
            candidate = _dt.datetime.combine(now.date(), tm)
            if candidate > now:
                return candidate.strftime("今天 %H:%M")
        return (_dt.datetime.combine(now.date() + _dt.timedelta(days=1),
                                     sorted(parsed)[0]).strftime("明天 %H:%M"))

    def _account_health(self, accounts):
        now = _dt.datetime.now().timestamp()
        ready = issues = 0
        for a in accounts:
            if a.get("platform") == "traework":
                good = bool(a.get("has_session") or a.get("logged_in"))
            elif a.get("platform") == "workbuddy":
                exp = a.get("expires_at") or 0
                good = not exp or exp > now * 1000
            else:
                exp = a.get("exp") or 0
                good = bool(exp) and exp > now
            if good:
                ready += 1
            else:
                issues += 1
        return ready, issues

    def _health_row(self, parent, label, value, hint, tone):
        t = self.theme
        row = tk.Frame(parent, bg=t.bg_soft)
        row.pack(fill="x", padx=14, pady=8)
        left = tk.Frame(row, bg=t.bg_soft)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=label, font=t.f_small, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(anchor="w")
        W.auto_wrap(tk.Label(left, text=hint, font=t.f_tiny, fg=t.fg_dim,
                             bg=t.bg_soft, anchor="w", justify="left")).pack(
            anchor="w", fill="x")
        W.Badge(row, t, value, tone).pack(side="right")

    def _render_health(self, stats, accounts, done, total, cfg):
        t = self.theme
        card = W.Card(self.health_box, t, accent="blue", padding=0)
        card.pack(fill="x")
        box = card.body
        success = stats["ok"] / stats["attempts"] * 100 if stats["attempts"] else None
        ready, issues = self._account_health(accounts)
        self._health_row(
            box, "今日签到", f"{done}/{total}" if total else "0/0",
            "全部账号已完成" if total and done == total else
            ("仍有账号待处理" if total else "先到账号管理添加账号"),
            "green" if total and done == total else ("amber" if total else "slate"))
        if stats["attempts"]:
            result_hint = (f"成功/跳过 {stats['ok']} · 待复核 {stats['busy']} · "
                           f"失败 {stats['fail']}")
            result_value = f"{success:.0f}%"
            result_tone = "green" if success >= 80 else ("amber" if success >= 50 else "red")
        else:
            result_hint = "近 7 天还没有该平台的签到流水"
            result_value, result_tone = "暂无", "slate"
        self._health_row(box, "近 7 日执行质量", result_value, result_hint, result_tone)
        self._health_row(
            box, "账号可用性", f"{ready}/{len(accounts)}" if accounts else "0/0",
            ("令牌/会话均可用" if accounts and not issues else
             f"有 {issues} 个账号需要检查令牌或登录态" if issues else
             "暂无账号可检查"),
            "green" if accounts and not issues else ("amber" if issues else "slate"))
        self._health_row(box, "下一次自动签到", self._next_run(cfg),
                         "按系统设置中的每日时间执行；手动签到不受此项限制",
                         "blue" if cfg.get("schedule", {}).get("daily_times") else "slate")

    def _render_recent(self, stats):
        t = self.theme
        rows = list(reversed(stats["rows"][-8:]))
        if not rows:
            W.EmptyState(self.recent_box, t, "☰", "近 7 天暂无流水",
                         "完成一次签到后，这里会显示执行结果和实际领取的积分。").pack(fill="x")
            return
        card = W.Card(self.recent_box, t, padding=0)
        card.pack(fill="x")
        box = card.body
        head = tk.Frame(box, bg=t.bg_soft)
        head.pack(fill="x", padx=14, pady=(10, 6))
        for text, width in (("时间", 13), ("账号", 16), ("奖励", 10), ("结果", 12)):
            tk.Label(head, text=text, font=t.f_tiny, fg=t.fg_dim,
                     bg=t.bg_soft, width=width, anchor="w").pack(side="left")
        W.hline(box, t).pack(fill="x")
        for i, row_data in enumerate(rows):
            if i:
                W.hline(box, t).pack(fill="x")
            row = tk.Frame(box, bg=t.bg_soft)
            row.pack(fill="x", padx=14, pady=7)
            ts = str(row_data.get("ts") or "")
            ts = ts[5:16].replace("T", " ") if len(ts) >= 16 else (ts or "-")
            reward = self._reward(row_data.get("detail"))
            status = row_data.get("status")
            tk.Label(row, text=ts, font=t.f_tiny, fg=t.fg_dim,
                     bg=t.bg_soft, width=13, anchor="w").pack(side="left")
            tk.Label(row, text=str(row_data.get("account") or "-"), font=t.f_small,
                     fg=t.fg, bg=t.bg_soft, width=16, anchor="w").pack(side="left")
            tk.Label(row, text=(f"+{CR.format_num(reward)}" if reward else "—"),
                     font=(t.font_family, 9, "bold"),
                     fg=t.green_fg if reward else t.fg_dim,
                     bg=t.bg_soft, width=10, anchor="w").pack(side="left")
            W.Badge(row, t, C.STATUS_CN.get(status, str(status)),
                    C.STATUS_TONE.get(status, "slate")).pack(side="left")
            detail = str(row_data.get("detail") or "")
            if len(detail) > 48:
                detail = detail[:47] + "…"
            W.auto_wrap(tk.Label(row, text=detail, font=t.f_tiny, fg=t.fg_muted,
                                 bg=t.bg_soft, anchor="w", justify="left")).pack(
                side="left", padx=(10, 0), fill="x", expand=True)

    # ---------------- 额度查询补充 ----------------

    def _render(self, data):
        self.busy = False
        if not W.alive(self):
            return
        t = self.theme
        accounts = self.app.accounts().get(self.module, [])
        cfg = C.load_config()
        state = C.load_state()
        today = C.today_str()
        total_accounts = len(accounts)
        done = sum(1 for a in accounts if state.get(a.get("key")) == today)
        stats = self._history_stats()
        ok_rows = [d for d in data if d.get("ok")]
        unsup = [d for d in data if d.get("unsupported")]
        failed = [d for d in data if not d.get("ok") and not d.get("unsupported")]
        queried_total = sum(d.get("totalRemaining") or 0 for d in ok_rows)

        self.cards["accounts"].set(total_accounts, "本地账号列表")
        self.cards["today"].set(f"{done}/{total_accounts}" if total_accounts else "0/0",
                                  "全部完成" if total_accounts and done == total_accounts
                                  else "今日状态", "green" if total_accounts and done == total_accounts
                                  else "slate")
        rate = stats["ok"] / stats["attempts"] * 100 if stats["attempts"] else None
        self.cards["success"].set(f"{rate:.0f}%" if rate is not None else "暂无",
                                   f"近 7 日 {stats['attempts']} 条流水",
                                   "green" if rate is not None and rate >= 80 else
                                   ("amber" if rate is not None else "slate"))
        self.cards["reward"].set(f"+{CR.format_num(stats['reward'])}" if stats["reward"] else "暂无",
                                  "从签到流水提取实际奖励",
                                  "amber" if stats["reward"] else "slate")

        self._render_health(stats, accounts, done, total_accounts, cfg)
        self._render_recent(stats)

        card = W.Card(self.list_box, t, padding=0)
        card.pack(fill="x")
        box = card.body
        head = tk.Frame(box, bg=t.bg_soft)
        head.pack(fill="x", padx=14, pady=(10, 6))
        for text, width in (("账号", 20), ("额度查询", 14), ("可用性", 28)):
            tk.Label(head, text=text, font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                     width=width, anchor="w").pack(side="left")
        W.hline(box, t).pack(fill="x")
        if not data:
            tk.Label(box, text="暂无账号明细，请先到「账号管理」添加账号。",
                     font=t.f_small, fg=t.fg_dim, bg=t.bg_soft,
                     anchor="w").pack(fill="x", padx=14, pady=14)
        else:
            for i, d in enumerate(data):
                if i:
                    W.hline(box, t).pack(fill="x")
                row = tk.Frame(box, bg=t.bg_soft)
                row.pack(fill="x", padx=14, pady=8)
                name = d.get("name") or "-"
                tk.Label(row, text=name, font=t.f_small, fg=t.fg,
                         bg=t.bg_soft, width=20, anchor="w").pack(side="left")
                val = CR.format_num(d.get("totalRemaining")) if d.get("ok") else "—"
                tk.Label(row, text=val, font=(t.font_family, 10, "bold"),
                         fg=t.fg if d.get("ok") else t.fg_dim,
                         bg=t.bg_soft, width=14, anchor="w").pack(side="left")
                if d.get("ok"):
                    W.Badge(row, t, "接口可用", "green").pack(side="left")
                elif d.get("unsupported"):
                    W.Badge(row, t, "浏览器查看额度", "slate").pack(side="left")
                else:
                    msg = str(d.get("error") or "查询失败")[:34]
                    W.Badge(row, t, msg, "amber" if "未开放" in msg else "red").pack(side="left")

        # 在明细底部给出接口查询结果的简短说明，避免用户误以为「—」是账号失效。
        if ok_rows:
            hint = f"本次接口查询成功 {len(ok_rows)} 个账号，额度合计 {CR.format_num(queried_total)}。"
        elif unsup and not failed:
            hint = "当前平台额度需要在浏览器内查看；上方签到健康与收益数据仍然有效。"
        elif failed:
            hint = f"本次有 {len(failed)} 个账号未取到额度；请以账号可用性和最近签到结果判断是否需要处理。"
        else:
            hint = "额度接口没有返回可用数据；本页不以此项作为签到是否成功的判据。"
        W.auto_wrap(tk.Label(self.list_box, text=hint, font=t.f_tiny,
                             fg=t.fg_dim, bg=t.bg, anchor="w", justify="left")).pack(
            fill="x", pady=(6, 0))

    def on_hide(self):
        pass
