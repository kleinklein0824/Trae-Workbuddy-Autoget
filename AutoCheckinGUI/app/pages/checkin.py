#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键签到页：账号勾选 + 跳过已签 + 实时进度 + 逐账号结果 + 实时日志。

执行体复用 services.runner.CheckinRunner，它直接调用既有签到模块，
因此这里的每一次签到与命令行/计划任务的行为完全一致。
"""
import tkinter as tk

from .. import config as C
from ..services import runner as R
from ..ui import widgets as W
from .base import BasePage

MODULE_CN = {"traework": "TraeWork", "traecn": "TraeCN", "workbuddy": "WorkBuddy"}
# 全部平台时的执行顺序（与命令行 `checkin.py run` 的默认顺序一致）
PLATFORMS = ("traework", "traecn", "workbuddy")


class CheckinPage(BasePage):
    title = "一键签到"
    desc = "选择账号后执行，进度与结果实时回传"
    busy = False

    def build(self):
        t = self.theme
        self.set_title(f"{MODULE_CN.get(self.module, '')} 一键签到",
                       "执行逻辑与计划任务共用同一套签到模块")
        self.runner = None
        self._sel = {}          # {account_id: BooleanVar}
        self._skip_done = tk.BooleanVar(value=True)
        self._force = tk.BooleanVar(value=False)
        # 签到范围：current = 仅当前平台；all = 三个平台的全部账号。
        # 顶栏「一键签到」会直接把这里切到 all（见 start_checkin 的 scope 参数）。
        self._scope_var = tk.StringVar(value="current")
        # 注意：**不要**在这里缓存今日判重表（曾经有过 `self._state`）。
        # 签到结果会在页面停留期间被写盘（用户点「开始签到」、后台计划任务跑完），
        # 缓存必然过期 → 「跳过今日已签」按旧状态筛选。判重一律现读
        # （见 `_targets_by_platform`；state.json 只有几行，重读是微秒级）。
        self._results = []

        # ---- 控制栏 ----
        ctrl = W.Card(self.body, t, padding=12)
        ctrl.pack(fill="x")
        bar = ctrl.body

        # 范围选择独占一行：这一行的文字最长（含三个平台名），
        # 和勾选框挤在一行会在窄窗口下把右侧「目标 N 个账号 / 开始签到」顶出去。
        scope_row = tk.Frame(bar, bg=t.bg_soft)
        scope_row.pack(fill="x")
        tk.Label(scope_row, text="签到范围", font=t.f_small, fg=t.fg_dim,
                 bg=t.bg_soft).pack(side="left", padx=(0, 8))
        for val, txt in (("current", f"仅 {MODULE_CN.get(self.module, '当前平台')}"),
                         ("all", "全部平台")):
            # indicatoron=False → 原生「分段按钮」观感；选中态由 Tk 自动维护
            tk.Radiobutton(
                scope_row, text=txt, value=val, variable=self._scope_var,
                command=self._on_scope_change, indicatoron=False,
                font=t.f_btn_sm, fg=t.fg_muted, bg=t.bg_soft,
                activebackground=t.bg_hover, activeforeground=t.fg,
                selectcolor=t.bg, relief="flat", bd=0, padx=10, pady=3,
                cursor="hand2", highlightthickness=0,
                takefocus=0).pack(side="left", padx=(0, 4))
        self._scope_hint = tk.Label(scope_row, text="", font=t.f_tiny,
                                    fg=t.fg_dim, bg=t.bg_soft)
        self._scope_hint.pack(side="left", padx=(10, 0))

        row2 = tk.Frame(bar, bg=t.bg_soft)
        row2.pack(fill="x", pady=(8, 0))
        left = tk.Frame(row2, bg=t.bg_soft)
        left.pack(side="left", fill="x", expand=True)

        self._chk_skip = self._check(left, "跳过今日已签", self._skip_done,
                                     self._on_skip_toggle)
        self._chk_skip.pack(side="left")
        self._chk_force = self._check(left, "强制执行（忽略今日记录）", self._force,
                                      self._on_force_toggle)
        self._chk_force.pack(side="left", padx=(14, 0))
        W.button(left, t, "全选", command=lambda: self._select_all(True),
                 variant="ghost", small=True).pack(side="left", padx=(14, 2))
        W.button(left, t, "全不选", command=lambda: self._select_all(False),
                 variant="ghost", small=True).pack(side="left")

        right = tk.Frame(row2, bg=t.bg_soft)
        right.pack(side="right")
        self._target_lbl = tk.Label(right, text="目标 0 个账号", font=t.f_small,
                                    fg=t.fg_muted, bg=t.bg_soft)
        self._target_lbl.pack(side="left", padx=(0, 10))
        self._btn_cancel = W.button(right, t, "停止", command=self._cancel,
                                    variant="outline", small=True)
        # 不传参 → 尊重复选框当前状态（见 start_checkin 的 skip_done 说明）
        self._btn_run = W.button(right, t, "开始签到", command=self.start_checkin,
                                 variant="primary", icon="▶")
        self._btn_run.pack(side="right")
        self._btn_cancel.pack(side="right", padx=(0, 6))
        self._btn_cancel.pack_forget()

        # ---- 账号勾选列表 ----
        W.section_title(self.body, t, "选择账号").pack(fill="x", pady=(16, 6))
        self.acc_box = tk.Frame(self.body, bg=t.bg)
        self.acc_box.pack(fill="x")

        # ---- 进度与结果 ----
        self.prog_title = W.section_title(self.body, t, "签到进度")
        self.prog_title.pack(fill="x", pady=(18, 6))
        prog_card = W.Card(self.body, t, padding=12)
        prog_card.pack(fill="x")
        pb = prog_card.body
        self.progress = W.Progress(pb, t)
        self.progress.pack(fill="x")
        self._prog_lbl = tk.Label(pb, text="尚未开始", font=t.f_tiny,
                                  fg=t.fg_dim, bg=t.bg_soft, anchor="w")
        self._prog_lbl.pack(fill="x", pady=(6, 0))
        self.result_box = tk.Frame(pb, bg=t.bg_soft)
        self.result_box.pack(fill="x", pady=(8, 0))

        # ---- 实时日志 ----
        W.section_title(self.body, t, "实时日志",
                        "完整日志同时写入 logs/checkin_YYYY-MM-DD.log").pack(
            fill="x", pady=(18, 6))
        log_card = W.Card(self.body, t, padding=8)
        log_card.pack(fill="both", expand=True)
        # width=1 覆盖 tk.Text 默认 80 字符宽，避免撑宽卡片
        self.log = tk.Text(log_card.body, width=1, height=12, font=t.f_mono,
                           bg=t.bg_soft, fg=t.fg_muted, relief="flat",
                           wrap="word", insertbackground=t.fg)
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")
        self._log_lines = 0

        self._load_accounts()

    # ---------------- 构件辅助 ----------------

    def _check(self, master, text, var, command=None):
        return tk.Checkbutton(master, text=text, variable=var, command=command,
                              font=self.theme.f_small,
                              fg=self.theme.fg_muted, bg=self.theme.bg_soft,
                              activebackground=self.theme.bg_soft,
                              activeforeground=self.theme.fg,
                              selectcolor=self.theme.bg_soft,
                              highlightthickness=0, bd=0, cursor="hand2")

    def _append_log(self, text):
        self.log.configure(state="normal")
        for line in str(text).splitlines():
            self.log.insert("end", line + "\n")
            self._log_lines += 1
        # 只保留最近 800 行，避免长时间运行后卡顿
        if self._log_lines > 800:
            self.log.delete("1.0", f"{self._log_lines - 800}.0")
            self._log_lines = 800
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------- 账号列表 ----------------

    @property
    def scope_all(self):
        """是否「全部平台」范围（三个平台的全部账号）。"""
        return self._scope_var.get() == "all"

    def _all_accounts(self):
        """三个平台的账号，按固定平台顺序拼接（便于分组阅读）。"""
        data = self.app.accounts()
        out = []
        for pid in PLATFORMS:
            out += list(data.get(pid) or [])
        return out

    def _on_scope_change(self):
        """范围切换：重建账号列表（勾选状态也随之按新范围重算）。"""
        if self.busy:
            return
        self._load_accounts()

    def _load_accounts(self):
        for w in self.acc_box.winfo_children():
            w.destroy()
        t = self.theme
        self._sel = {}
        # 读缓存，避免切页时同步扫描 IDE 日志造成卡顿
        self.accounts = (self._all_accounts() if self.scope_all
                         else self.app.accounts().get(self.module, []))
        state = C.load_state()   # 仅用于本轮渲染，不做长期缓存
        today = C.today_str()
        self.set_title(f"{'全部平台' if self.scope_all else MODULE_CN.get(self.module, '')} 一键签到",
                       "执行逻辑与计划任务共用同一套签到模块")

        if not self.accounts:
            loading = C.peek_accounts() is None
            W.EmptyState(self.acc_box, t, "◍",
                         "正在读取账号…" if loading else
                         f"没有可用的 {MODULE_CN.get(self.module, '')} 账号",
                         "账号信息会在后台读取完成后自动出现。"
                         if loading else
                         "请先到「账号管理」添加或导入账号。").pack(fill="x")
            self._update_target()
            return

        card = W.Card(self.acc_box, t, padding=0)
        card.pack(fill="x")
        box = card.body
        self._done_ids = set()
        last_pid = None
        for i, a in enumerate(self.accounts):
            # 「全部平台」时按平台分组，避免三个平台的同名账号混在一起
            new_group = self.scope_all and a["platform"] != last_pid
            if new_group:
                last_pid = a["platform"]
                hdr = tk.Frame(box, bg=t.bg_soft)
                hdr.pack(fill="x", padx=14, pady=(10, 2))
                tk.Label(hdr, text=f"◆ {MODULE_CN.get(a['platform'], a['platform'])}",
                         font=t.f_small, fg=t.fg_dim, bg=t.bg_soft,
                         anchor="w").pack(anchor="w")
            elif i:
                W.hline(box, t).pack(fill="x")
            done = state.get(a["key"]) == today
            if done:
                self._done_ids.add(a["id"])
            row = tk.Frame(box, bg=t.bg_soft)
            row.pack(fill="x", padx=14, pady=8)

            # 默认勾选「今日未签」的账号；已签的也保持可勾（强制执行时使用）
            var = tk.BooleanVar(value=not done)
            self._sel[a["id"]] = var
            cb = tk.Checkbutton(row, variable=var, command=self._update_target,
                                bg=t.bg_soft, activebackground=t.bg_soft,
                                selectcolor=t.bg_soft, highlightthickness=0,
                                bd=0, cursor="hand2")
            cb.pack(side="left")

            info = tk.Frame(row, bg=t.bg_soft)
            info.pack(side="left", padx=(6, 0), fill="x", expand=True)
            tk.Label(info, text=a["display"], font=t.f_body, fg=t.fg,
                     bg=t.bg_soft, anchor="w").pack(anchor="w")
            sub = self._subtitle(a)
            if sub:
                tk.Label(info, text=sub, font=t.f_tiny, fg=t.fg_dim,
                         bg=t.bg_soft, anchor="w").pack(anchor="w")

            if done:
                W.Badge(row, t, "今日已签", "blue", "✓").pack(side="right")
            else:
                W.Badge(row, t, "待签到", "slate").pack(side="right")
            # 「全部平台」时再补一个平台标签：分组标题可能已滚出视野
            if self.scope_all:
                W.Badge(row, t, MODULE_CN.get(a["platform"], a["platform"]),
                        "slate").pack(side="right", padx=(0, 6))

        self._sync_checkboxes()
        self._update_target()

    def _sync_checkboxes(self):
        """按「跳过已签」语义同步勾选：跳过时取消勾选已签账号，**不跳过时勾回来**。

        ★★ 2026-09-25 修：原先**只做单向**同步（只取消、不勾回）。
          后果很实在：用户在「今日已全部完成」时勾上「强制重签」→
          `_on_force_toggle` 把 `_skip_done` 关掉，但已签账号**仍是未勾选** →
          目标依旧 0 → 再点「开始签到」只会被告知「未勾选账号」
          → **「强制重签」实际上根本用不了**（用户报的「无法手动签到」）。
          现在：不再跳过已签时，把「因跳过而被自动取消」的勾选恢复回来。
        """
        force = bool(self._force.get())
        skip = bool(self._skip_done.get()) and not force
        for aid in getattr(self, "_done_ids", set()):
            var = self._sel.get(aid)
            if var is None:
                continue
            if skip:
                if var.get():
                    var.set(False)
            elif not var.get():
                var.set(True)

    def _subtitle(self, a):
        if a["platform"] == "workbuddy":
            src = "本机当前登录" if a.get("source") == "current" else "已保存令牌"
            return f"{src} · {a.get('phone') or (a['uid'][:12] + '…')}"
        if a["platform"] == "traework":
            return f"独立会话 · profiles/traework/{a.get('profile')}"
        if a["platform"] == "traecn":
            return f"IDE 令牌 · uid …{a['uid'][-6:]}"
        return ""

    def _select_all(self, value):
        """全选/全不选：全选时遵循「跳过已签」语义（已签的不会被选中）。"""
        force = bool(self._force.get())
        skip = bool(self._skip_done.get()) and not force
        done = getattr(self, "_done_ids", set())
        for aid, var in self._sel.items():
            try:
                var.set(bool(value) and not (skip and aid in done))
            except Exception:  # noqa: BLE001
                pass
        self._update_target()

    def _update_target(self):
        by = self._targets_by_platform()
        n = sum(len(v) for v in by.values())
        total = len(getattr(self, "accounts", []) or [])
        # ★★ 0 个目标时**必须说清为什么**（2026-09-25 修）。
        #   原先 `n == 0` 直接把「开始签到」设成 `disabled` ——
        #   用户看到的是一个**灰掉的按钮**：点不动、没有任何提示、也没有日志，
        #   表现就是「无法手动签到」（实测 traework / traecn 都是这个原因：
        #   state 里今天已签 → 目标 0 → 按钮灰）。
        #   现在：按钮**保持可点**（只要没在跑），标签点明原因，
        #   点下去再给明确提示 + 出路（勾「强制重签」）。
        #   —— 与「点击不许静默无反应」是同一条原则。
        if n:
            self._target_lbl.configure(
                text=(f"目标 {n} 个账号 · {len(by)} 个平台" if self.scope_all
                      else f"目标 {n} 个账号"))
        else:
            why = ("（本平台无账号）" if total == 0 else
                   "（今日已全部完成）"
                   if (self._skip_done.get() and not self._force.get()) else
                   "（未勾选账号）")
            self._target_lbl.configure(text=f"目标 0 个账号 {why}")
        try:
            self._scope_hint.configure(
                text=("一次覆盖三个平台的全部账号（与计划任务同源）"
                      if self.scope_all else ""))
        except Exception:  # noqa: BLE001
            pass
        running = self.busy
        if hasattr(self, "_btn_run"):
            self._btn_run.configure(state="disabled" if running else "normal")

    def _targets(self):
        """当前勾选、且未被「跳过已签」排除的账号 id 列表（扁平）。"""
        out = []
        for ids in self._targets_by_platform().values():
            out += ids
        return out

    def _targets_by_platform(self):
        """{platform: [account_id, ...]}，只含勾选且未被「跳过已签」排除的账号。

        「强制执行」优先级高于「跳过今日已签」：勾上强制执行时忽略今日记录，
        所有被勾选的账号都会重新签到（与命令行 `checkin.py run --force` 一致）。
        「全部平台」时 accounts 里就是三个平台的账号，所以这里天然按平台分组。
        """
        # ★ 每次都**重新读盘**，不要复用 `self._state` 缓存：
        #   `_load_accounts` 只在切页/切范围时跑，而签到结果会**在页面停留期间**
        #   被写进 state.json（用户点完「开始签到」，或后台计划任务正好跑完）。
        #   复用缓存 → 「跳过今日已签」按**过期的**状态筛选 → 刚签成功的账号
        #   又被排进下一轮（表现为"明明签过了还反复签"）。
        #   曾经写成 `getattr(self, "_state", None) or C.load_state()`，
        #   只要缓存非空就永远走不到右边 —— 而且它让测试里替换的
        #   `C.load_state` 桩**完全失效**，测出来的绿是假的。
        #   state.json 只有几行，重读是微秒级，不必省。
        state = C.load_state()
        today = C.today_str()
        force = bool(self._force.get())
        skip = bool(self._skip_done.get()) and not force
        out = {}
        for a in getattr(self, "accounts", []):
            var = self._sel.get(a["id"])
            if var is None or not var.get():
                continue
            if skip and state.get(a["key"]) == today:
                continue
            out.setdefault(a["platform"], []).append(a["id"])
        return out

    def _on_skip_toggle(self):
        """跳过 / 强制 互斥：勾一个自动取消另一个，避免语义冲突。"""
        if self._skip_done.get() and self._force.get():
            self._force.set(False)
        self._sync_checkboxes()
        self._update_target()

    def _on_force_toggle(self):
        if self._force.get() and self._skip_done.get():
            self._skip_done.set(False)
        self._sync_checkboxes()
        self._update_target()

    # ---------------- 执行 ----------------

    def start_checkin(self, skip_done=None, scope=None, wait_lock_s=None):
        """开始签到。

        skip_done 是**给程序化调用用的**覆盖参数：
          * None  → 尊重界面上「跳过今日已签」复选框的当前状态（按钮点击走这条）
          * True  → 强制跳过（概览页「一键签到」）
          * False → 不跳过（API 服务页的「重签一轮」）

        scope 同样给程序化调用用：
          * None  → 保持界面上当前选的范围
          * "all" → 切到「全部平台」（顶栏 / 托盘的一键签到走这条）

        wait_lock_s：**手动优先**语义（2026-09-24）。TraeWork 用浏览器 profile，
        同一 profile 只允许一个实例；自动化那一轮正在跑时，手动点击原先会立刻
        被判成 busy（跳过），用户只能干等下一轮自动补跑。传一个正数就表示
        「愿意等对方让出浏览器」，等到了就立刻签到 —— 手动点击因此**优先执行**。
        默认 None → 用 `runner.MANUAL_LOCK_WAIT_S`（所有入口都是手动触发）。

        历史 bug：按钮的 command 写成了 `lambda: self.start_checkin()`，
        而签名默认 skip_done=True，于是用户**取消勾选后一点按钮就被静默改回勾选**，
        表现为"我明明取消了跳过，它还是没签"。这里改成 None 哨兵值修掉。
        """
        if self.busy:
            # ★ 不能静默 return（2026-09-24）：用户点了按钮却毫无反应，
            #   会以为是程序坏了。明确告诉他当前状态与下一步该怎么做。
            self.app.toast("info", "本次签到还在执行中，请等它跑完；"
                                   "要中止请点「停止」")
            return
        if self.runner is not None and self.runner.running:
            self.app.toast("warn", "已有签到任务在执行，请稍候")
            return
        # 先定范围再定跳过：切范围会重建账号列表（勾选状态随之重置）
        if scope is not None and (scope == "all") != self.scope_all:
            self._scope_var.set("all" if scope == "all" else "current")
            self._load_accounts()
        if skip_done is not None:
            self._skip_done.set(bool(skip_done))
        targets_by = self._targets_by_platform()
        n = sum(len(v) for v in targets_by.values())
        if not n:
            # ★ 说清原因 + **给出路**（2026-09-25 修）。
            #   原先只有一句「没有需要签到的账号」，而且按钮在 n==0 时是**灰的**
            #   —— 用户根本点不到这句话，只能看到"点了没反应"。
            if not getattr(self, "accounts", None):
                self.app.toast("warn",
                               f"本平台还没有账号，请先到「账号管理」"
                               f"添加或导入 {MODULE_CN.get(self.module, '')} 账号")
            elif self._skip_done.get() and not self._force.get():
                self.app.toast("warn",
                               "今日已全部签完。要重新签到，请勾选「强制重签」"
                               "再点一次「开始签到」")
            else:
                self.app.toast("warn", "还没有勾选要签到的账号，请先在上方勾选")
            return

        self.busy = True
        self._results = []
        for w in self.result_box.winfo_children():
            w.destroy()
        self.progress.set(0, n)
        self._prog_lbl.configure(text=f"0 / {n}")
        self._btn_run.pack_forget()
        self._btn_cancel.pack(side="right", padx=(0, 6))
        scope_cn = "全部平台" if self.scope_all else MODULE_CN.get(self.module, "")
        self._append_log(f"===== 开始签到（{n} 个账号，{scope_cn}）=====")
        if wait_lock_s is None:
            wait_lock_s = R.MANUAL_LOCK_WAIT_S
        if wait_lock_s > 0:
            self._append_log(f"手动优先执行：若浏览器正被自动化那一轮占用，"
                             f"会先等它让出（最多 {int(wait_lock_s)}s）再签到 ——"
                             f"不用等自动补跑")

        self.runner = R.CheckinRunner(
            # 传 list（不是 tuple）：既有回归 test_checkin_buttons 断言
            # `platforms == ["traework"]`，保持这个对外形态不变。
            platforms=(list(PLATFORMS) if self.scope_all else [self.module]),
            force=bool(self._force.get()),
            use_gate=False,
            accounts=targets_by,
            wait_lock_s=wait_lock_s,
        ).start()
        self._poll()

    def _cancel(self):
        if self.runner is not None and self.runner.running:
            self.runner.cancel()
            self._append_log("已请求停止，等待当前账号处理结束…")
            self.app.toast("info", "已请求停止签到")

    def _poll(self):
        """轮询执行线程事件队列（Tkinter 单线程，只能靠轮询取消息）。"""
        # 页面已被销毁（切页/换主题）时停止轮询，执行线程会在后台跑完
        if not W.alive(self):
            return
        q = self.runner.events if self.runner else None
        drained = 0
        while q is not None and drained < 200:
            try:
                ev = q.get_nowait()
            except Exception:  # noqa: BLE001
                break
            drained += 1
            self._handle(ev)
        if self.runner is not None and not self.runner.running and q is not None and q.empty():
            self._finish()
            return
        self._poll_id = self.after(150, self._poll)

    def _handle(self, ev):
        kind = ev.get("type")
        if kind == "log":
            self._append_log(ev.get("text", ""))
        elif kind == "start":
            self._prog_lbl.configure(
                text=f"{len(self._results)} / {self.progress.maximum} · "
                     f"正在处理 {ev.get('account')}")
        elif kind == "result":
            self._results.append(ev)
            self._add_result_row(ev)
            self.progress.set(len(self._results))
            self._prog_lbl.configure(
                text=f"{len(self._results)} / {self.progress.maximum}")
        elif kind == "done":
            self._append_log("===== 本轮签到结束 =====")
        elif kind == "error":
            self.app.toast("error", f"签到执行异常：{ev.get('text')}")

    def _add_result_row(self, ev):
        t = self.theme
        row = tk.Frame(self.result_box, bg=t.bg_soft)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=f"{len(self._results)}.", font=t.f_tiny,
                 fg=t.fg_dim, bg=t.bg_soft, width=4, anchor="e").pack(side="left")
        tk.Label(row, text=ev.get("account") or "-", font=t.f_small, fg=t.fg,
                 bg=t.bg_soft, width=14, anchor="w").pack(side="left")
        st = ev.get("status")
        W.Badge(row, t, C.STATUS_CN.get(st, str(st)),
                C.STATUS_TONE.get(st, "slate")).pack(side="right")
        detail = str(ev.get("detail") or "")
        tk.Label(row, text=detail, font=t.f_tiny, fg=t.fg_muted, bg=t.bg_soft,
                 anchor="w", wraplength=520, justify="left").pack(
            side="left", padx=(10, 0), fill="x", expand=True)

    def _finish(self):
        self.busy = False
        self._btn_cancel.pack_forget()
        self._btn_run.pack(side="right")
        self._btn_run.configure(state="normal")
        ok, busy, fail = C.count_status(self._results)
        tail = f"，待复核 {busy}" if busy else ""
        manual = bool(getattr(self.runner, "manual", False))
        mark = "（手动优先执行）" if manual else ""
        self._prog_lbl.configure(
            text=f"完成：成功/跳过 {ok}{tail}，失败 {fail}"
                 f"（共 {len(self._results)}）{mark}")
        # 手动优先执行时把话说满：已即时跑过、不用等自动补跑；若仍有未完成项，
        # 明确告诉用户「可以再点一次立刻重试」，而不是让他去等补跑窗口。
        if manual and (busy or fail):
            self.app.toast("warn",
                           f"手动补签完成：成功 {ok}{tail}，失败 {fail} ——"
                           f"可再点一次立即重试，自动补跑仍会兜底")
        else:
            self.app.toast("success" if fail == 0 else "warn",
                           f"签到完成：成功 {ok}{tail}，失败 {fail}{mark}")
        self._update_target()
        self.app._refresh_topbar()

    def on_hide(self):
        # 停止轮询定时器；执行线程本身不打断（后台继续跑完并落盘）
        pid = getattr(self, "_poll_id", None)
        if pid:
            try:
                self.after_cancel(pid)
            except Exception:  # noqa: BLE001
                pass
            self._poll_id = None

    def on_show(self):
        # ★★ 页面现在是**复用**的（`App._page_cache`），切走只 `on_hide()` 停轮询、
        #   不销毁页面 —— 所以切回来必须处理「上一轮签到还在跑」这种情况（2026-09-22）：
        #     · `on_hide()` 取消了 `_poll_id`，而事件队列**只能靠 `_poll` 取**；
        #       不续上 → `_finish()` 永远不会被调用 → `busy` 卡在 True、
        #       「开始签到」按钮永远不回来（**页面永久卡死**）。
        #       改前是整页销毁重建，看不出这个问题。
        #     · `_load_accounts()` 会重建账号列表并重置勾选与每行状态
        #       （`self._sel = {}` / `self._done_ids = set()`），跑动中重建会把
        #       进度标记冲掉 —— 所以跑动中**不重建**，与 `on_accounts_updated`
        #       的既有约定一致（「仅在空闲时重建列表，避免打断进行中的签到」）。
        if self.busy:
            self._resume_poll()
            return
        self._load_accounts()
        self._refresh_logger_hint()
        self._update_target()

    def _resume_poll(self):
        """切回页面时，若还有签到在跑就把轮询续上（幂等，重复调用无副作用）。"""
        try:
            if self.runner is not None and self.runner.running \
                    and getattr(self, "_poll_id", None) is None:
                self._poll()
        except Exception:  # noqa: BLE001
            pass

    def on_accounts_updated(self):
        """账号缓存刷新完成：仅在空闲时重建列表，避免打断进行中的签到。"""
        if self.busy:
            return
        self._load_accounts()
        self._update_target()

    def _refresh_logger_hint(self):
        if self.log.get("1.0", "end").strip() == "":
            self._append_log("（等待执行…）点击「开始签到」后，这里会实时显示每一步日志。")
