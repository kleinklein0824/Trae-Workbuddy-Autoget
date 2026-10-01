#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统设置页：平台开关、签到时间、开机自启、浏览器模式、计划任务管理。

所有开关直接写回根目录 config.json，与命令行/计划任务共用同一份配置。
"""
import os
import re
import threading
import tkinter as tk

from .. import config as C
from ..services import account_bundle as AB
from ..services import credential_refresh as CR
from ..services import envcheck
from ..services import scheduler
from ..ui import widgets as W
from .base import BasePage

# 自检状态 → 主题语义色
_ENV_TONE = {"OK": "green", "WARN": "amber", "FAIL": "red", "SKIP": "slate"}


def _dur_short(td):
    """timedelta → 「N 小时 M 分钟」这种短文案（给「还剩多久」用）。"""
    try:
        s = int(max(0, td.total_seconds()))
    except Exception:  # noqa: BLE001
        return "—"
    h, r = divmod(s, 3600)
    m = r // 60
    if h and m:
        return "%d 小时 %d 分钟" % (h, m)
    if h:
        return "%d 小时" % h
    return "%d 分钟" % max(1, m)


def _minutes_to_hours_text(minutes):
    """分钟 → 界面用的「小时」文本（整小时不带小数，90 分钟显示 1.5）。"""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        m = 360
    if m % 60 == 0:
        return str(m // 60)
    return ("%.2f" % (m / 60.0)).rstrip("0").rstrip(".")


def _ago_text(ts):
    """时间戳 → 「刚刚 / N 分钟前 / N 小时前 / N 天前」；None → 「从未」。"""
    if not ts:
        return "从未"
    import time as _t
    d = _t.time() - float(ts)
    if d < 60:            # 含「时间戳在未来」的情况（时钟回拨），一并按「刚刚」
        return "刚刚"
    if d < 3600:
        return "%d 分钟前" % round(d / 60)
    if d < 86400:
        return "%.1f 小时前" % (d / 3600)
    return "%.1f 天前" % (d / 86400)


class SettingsPage(BasePage):
    title = "系统设置"

    def build(self):
        t = self.theme
        self.set_title("系统设置", "配置会写入项目根目录的 config.json，与计划任务同步生效")
        self.busy = False
        # 凭据状态是**异步**读的（见 _refresh_cred_info）：这两个状态位跟着一起建
        self._cred_reading = False
        self._cred_info_text = ""

        # ---- 平台开关 ----
        self.card_platform = W.Card(self.body, t, padding=14)
        self.card_platform.pack(fill="x")
        self._platform_vars = {}
        body = self.card_platform.body
        tk.Label(body, text="平台开关", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(fill="x")
        tk.Label(body, text="关闭后该平台不参与任何签到（含计划任务）",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                 anchor="w").pack(fill="x", pady=(2, 8))
        for key, label in (("traework", "TraeWork"), ("traecn", "TraeCN"),
                           ("workbuddy", "WorkBuddy")):
            self._switch_row(body, label, key, "enabled")

        # ---- 签到时间 ----
        self.card_time = W.Card(self.body, t, padding=14)
        self.card_time.pack(fill="x", pady=(12, 0))
        b = self.card_time.body
        tk.Label(b, text="每日签到时间", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(fill="x")
        tk.Label(b, text="最多 4 个时间点（HH:MM，逗号分隔）；第一个为当日基准时间，"
                         "用于判定是否补跑",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w").pack(
            fill="x", pady=(2, 8))
        row = tk.Frame(b, bg=t.bg_soft)
        row.pack(fill="x")
        self._time_entry = tk.Entry(row, font=t.f_body, bg=t.bg_input, fg=t.fg,
                                    relief="flat", highlightthickness=1,
                                    highlightbackground=t.border,
                                    highlightcolor=t.accent, width=28)
        self._time_entry.pack(side="left", ipady=4)
        W.button(row, t, "保存并重注册任务", command=self._save_times,
                 variant="primary", small=True).pack(side="left", padx=(8, 0))
        self._time_hint = tk.Label(b, text="", font=t.f_tiny, fg=t.fg_dim,
                                   bg=t.bg_soft, anchor="w")
        self._time_hint.pack(fill="x", pady=(6, 0))

        # ---- 自动化执行链 · 弹窗联动（2026-09-22 新增）----
        # 为什么单列一张卡：签到时间、自动补跑、弹窗策略原先散在**三张**卡片里
        # （「每日签到时间」/「自动补跑（重试）」/「运行选项」），用户要在页面里
        # 上下翻才能拼出「到底什么时候跑、失败了怎么办、什么时候会弹窗」。
        # 这张卡把整条链**按执行顺序**串起来，并把两个弹窗开关收到这里
        # （它们本来就是这条链的最后一环）。
        # 状态行是**纯读**（config + logs/retry_state.json + 计划任务缓存），
        # 不做任何探测，所以放在渲染路径上是安全的。
        self.card_chain = W.Card(self.body, t, padding=14)
        self.card_chain.pack(fill="x", pady=(12, 0))
        cb = self.card_chain.body
        tk.Label(cb, text="自动化执行链 · 弹窗联动", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(fill="x")
        tk.Label(cb, text="把「什么时候跑 → 失败了怎么补 → 什么时候弹窗」串在一起，"
                         "不用来回翻卡片",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w",
                 justify="left").pack(fill="x", pady=(2, 8))
        self._chain_line = W.auto_wrap(
            tk.Label(cb, text="", font=t.f_small, fg=t.fg_muted, bg=t.bg_soft,
                     anchor="w", justify="left"))
        self._chain_line.pack(fill="x")
        self._chain_state = W.auto_wrap(
            tk.Label(cb, text="", font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                     anchor="w", justify="left"))
        self._chain_state.pack(fill="x", pady=(6, 0))
        W.hline(cb, t).pack(fill="x", pady=(10, 8))
        # 弹窗总开关（原在「运行选项」里）
        self._switch_row(cb, "触发签到时弹出通知卡片", "options", "toast_notify",
                         hint="沿用项目原有的液态金属琉璃弹窗；关掉后完全静默",
                         on_change=self._refresh_chain)
        # 补跑弹窗策略（原在「自动补跑」卡片里）—— 改完要让上面的链条文案跟着变，
        # 也要让「自动补跑」卡片里那段说明文字跟着变。
        self._retry_toast_sw = self._switch_row(
            cb, "仅在关键节点弹窗（推荐）", "options", "retry_toast_quiet",
            hint="只在第 1 次执行 / 全部签完 / 补跑最后一轮仍失败时弹窗",
            on_change=self._on_retry_toast_change)

        # ---- 自动补跑（重试）----
        # 机制：给每个「每日定时」触发器挂 Repetition —— 从设定时间起每 N 分钟触发一次，
        # 持续 M 分钟。**幂等由门控（`_gate_skip`）保证**：全部账号已完成时秒退、
        # 根本不开浏览器，所以「执行一次后就暂停」是天然成立的，重复触发几乎零成本。
        # 开机补跑由「运行选项 → 开机后自动执行一次」+ 任务的 StartWhenAvailable 负责
        # （开机晚了错过设定时间，Windows 会尽快补跑那一次）。
        self.card_retry = W.Card(self.body, t, padding=14)
        self.card_retry.pack(fill="x", pady=(12, 0))
        rb = self.card_retry.body
        tk.Label(rb, text="自动补跑（重试）", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(fill="x")
        tk.Label(rb, text="过了设定时间后自动反复重试，直到真的签完为止；"
                         "全部签完立即停止，不会空跑",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w",
                 justify="left").pack(fill="x", pady=(2, 8))

        sched = C.load_config().get("schedule", {})
        self._retry_on = tk.BooleanVar(value=bool(sched.get("retry_enabled", True)))
        row = tk.Frame(rb, bg=t.bg_soft)
        row.pack(fill="x", pady=3)
        tk.Label(row, text="开启自动补跑", font=t.f_small, fg=t.fg_muted,
                 bg=t.bg_soft, anchor="w", width=22).pack(side="left")
        W.Switch(row, t, value=self._retry_on.get(),
                 command=lambda v: self._retry_on.set(bool(v))).pack(side="left")

        self._retry_iv = self._num_field(
            rb, "重试间隔", sched.get("retry_interval_minutes", 30), "分钟")
        self._retry_win = self._num_field(
            rb, "补跑持续", _minutes_to_hours_text(sched.get("retry_window_minutes", 360)),
            "小时")
        # 弹窗策略开关已**移到上方「自动化执行链 · 弹窗联动」卡片**（2026-09-22）：
        # 它和「触发签到时弹出通知卡片」是同一条链的最后一环，放一起才看得懂。
        # 这里只留说明文字（`_retry_hint`），内容里仍然包含弹窗策略。
        W.button(rb, t, "保存并重注册任务", command=self._save_retry,
                 variant="primary", small=True).pack(anchor="w", pady=(8, 0))
        # 说明文字可能很长，必须 auto_wrap —— 否则 tk.Label 不换行会冲出卡片被裁掉
        self._retry_hint = W.auto_wrap(
            tk.Label(rb, text="", font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                     anchor="w", justify="left", wraplength=880))
        self._retry_hint.pack(fill="x", pady=(6, 0))

        # ---- 运行选项 ----
        self.card_opt = W.Card(self.body, t, padding=14)
        self.card_opt.pack(fill="x", pady=(12, 0))
        ob = self.card_opt.body
        tk.Label(ob, text="运行选项", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(fill="x", pady=(0, 8))
        self._switch_row(ob, "无头浏览器（后台运行，不显示窗口）", "options", "headless",
                         hint="关闭后会弹出浏览器窗口，便于排查签到失败")
        self._switch_row(ob, "开机后自动执行一次", "schedule", "run_at_logon",
                         hint="登录系统后延迟一段时间自动补跑，错过的时点不会漏")
        # 「触发签到时弹出通知卡片」已**移到「自动化执行链 · 弹窗联动」卡片**
        # （2026-09-22）：弹窗属于那条链的最后一环，和补跑策略放一起才成体系。

        # ---- 计划任务 ----
        self.card_task = W.Card(self.body, t, padding=14)
        self.card_task.pack(fill="x", pady=(12, 0))
        tb = self.card_task.body
        head = tk.Frame(tb, bg=t.bg_soft)
        head.pack(fill="x")
        tk.Label(head, text="Windows 计划任务", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(side="left")
        self._task_status = tk.Label(head, text="", font=t.f_tiny, fg=t.fg_dim,
                                     bg=t.bg_soft, anchor="w")
        self._task_status.pack(side="left", padx=(10, 0))
        # 详情里会出现很长的绝对路径（任务指向 / 当前程序）与整句说明，
        # 不设 wraplength 的话 tk.Label **不会自动换行**，长行会直接冲出
        # 卡片右边缘被裁掉 —— 用户看到的就只是"显示不全"。
        self._task_detail = W.auto_wrap(
            tk.Label(tb, text="", font=t.f_tiny, fg=t.fg_dim,
                     bg=t.bg_soft, anchor="w", justify="left",
                     wraplength=880))
        self._task_detail.pack(fill="x", pady=(6, 0))
        btns = tk.Frame(tb, bg=t.bg_soft)
        btns.pack(fill="x", pady=(10, 0))
        self._btn_install = W.button(btns, t, "安装 / 更新任务",
                                     command=self._install_task, variant="primary",
                                     small=True)
        self._btn_install.pack(side="left")
        W.button(btns, t, "立即执行一次", command=self._run_task,
                 variant="outline", small=True).pack(side="left", padx=(6, 0))
        self._btn_uninstall = W.button(btns, t, "移除任务", command=self._uninstall_task,
                                       variant="danger", small=True)
        self._btn_uninstall.pack(side="left", padx=(6, 0))

        # ---- 设备标识（6 层重置 / 重置设备码）----
        self._build_device_section(t)

        # ---- 账号凭据自动刷新（寿命说明 + 后台重新捕获间隔）----
        self._build_credentials(t)

        # ---- 数据维护（签到记录 / 配置 / 日志）----
        self._build_maintenance(t)

        # ---- 签到记录导出位置（自定义 CSV 存放位置）----
        self._build_record_export(t)

        # ---- 账号迁移（换设备 / 新装 exe）----
        self._build_migration(t)

        # ---- 外观 ----
        self.card_ui = W.Card(self.body, t, padding=14)
        self.card_ui.pack(fill="x", pady=(12, 0))
        ub = self.card_ui.body
        tk.Label(ub, text="外观", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(fill="x", pady=(0, 8))
        self._theme_var = tk.BooleanVar(value=self.app.dark)
        row = tk.Frame(ub, bg=t.bg_soft)
        row.pack(fill="x", pady=3)
        tk.Label(row, text="深色主题", font=t.f_small, fg=t.fg_muted,
                 bg=t.bg_soft, width=26, anchor="w").pack(side="left")
        W.Switch(row, t, command=self._toggle_theme, value=self.app.dark).pack(side="left")

        # ---- 关于 ----
        self.card_about = W.Card(self.body, t, padding=14)
        self.card_about.pack(fill="x", pady=(12, 0))
        ab = self.card_about.body
        head_ab = tk.Frame(ab, bg=t.bg_soft)
        head_ab.pack(fill="x", pady=(0, 8))
        tk.Label(head_ab, text="关于", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(side="left")
        import runtime as _rt
        mode = "打包版（exe）" if _rt.is_frozen() else "源码模式"
        W.Badge(head_ab, t, mode, "blue").pack(side="right")
        W.button(head_ab, t, "打开数据目录", command=self._open_data_dir,
                 variant="outline", small=True).pack(side="right", padx=(0, 8))

        for k, v in (("数据目录", C.ROOT),
                     ("配置文件", C.CONFIG_PATH),
                     ("签到记录", C.RECORD_PATH),
                     ("界面参考", "Trae-workbuddyAssistant（视觉与信息架构）")):
            r = tk.Frame(ab, bg=t.bg_soft)
            r.pack(fill="x", pady=2)
            tk.Label(r, text=k, font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                     width=12, anchor="w").pack(side="left")
            tk.Label(r, text=str(v), font=t.f_tiny, fg=t.fg_muted, bg=t.bg_soft,
                     anchor="w", wraplength=700, justify="left").pack(
                side="left", fill="x", expand=True)

        tk.Label(ab, text="账号、签到记录、浏览器会话都保存在「数据目录」下，"
                          "整目录备份即可迁移到另一台电脑。\n"
                          "只想搬账号的话，用下面的「账号迁移」导出账号包更省事"
                          "（含登录态，新设备导入后无需重新登录）。",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w",
                 justify="left", wraplength=880).pack(fill="x", pady=(6, 0))

        self.on_show()

    def _open_data_dir(self):
        """在资源管理器里打开数据目录（跨平台兜底，失败给 toast）。"""
        try:
            import subprocess
            import sys as _sys
            if _sys.platform == "win32":
                os.startfile(C.ROOT)  # noqa: S606
            elif _sys.platform == "darwin":
                subprocess.Popen(["open", C.ROOT])
            else:
                subprocess.Popen(["xdg-open", C.ROOT])
        except Exception as e:  # noqa: BLE001
            self.app.toast("error", f"无法打开目录：{e!r}")

    # ---------------- 设备标识 ----------------

    def _build_device_section(self, t):
        """6 层设备标识重置 + 重置设备码（对齐参考项目 Settings）。"""
        self.card_dev = W.Card(self.body, t, padding=14)
        self.card_dev.pack(fill="x", pady=(12, 0))
        b = self.card_dev.body

        head = tk.Frame(b, bg=t.bg_soft)
        head.pack(fill="x")
        tk.Label(head, text="设备标识重置", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(side="left")
        self._dev_count = tk.Label(head, text="", font=t.f_tiny, fg=t.fg_dim,
                                   bg=t.bg_soft)
        self._dev_count.pack(side="right")

        tk.Label(b, text="用于账号隔离与防关联。建议先完全关闭 Trae 客户端再执行；"
                         "每次执行都会自动生成带时间戳的备份。",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w",
                 justify="left", wraplength=760).pack(fill="x", pady=(2, 8))

        # 6 层明细
        self._dev_layers = tk.Frame(b, bg=t.bg_soft)
        self._dev_layers.pack(fill="x")

        # 选项
        self._opt_machineguid = tk.BooleanVar(value=False)
        self._opt_credentials = tk.BooleanVar(value=False)
        opt1 = tk.Frame(b, bg=t.bg_soft)
        opt1.pack(fill="x", pady=(8, 0))
        tk.Checkbutton(opt1, text="同时轮换注册表 MachineGuid（需管理员权限）",
                       variable=self._opt_machineguid, font=t.f_small,
                       fg=t.fg_muted, bg=t.bg_soft, activebackground=t.bg_soft,
                       selectcolor=t.bg_input, anchor="w",
                       highlightthickness=0, bd=0).pack(anchor="w")
        tk.Checkbutton(opt1, text="清除登录凭据键（强制登出所有账号）",
                       variable=self._opt_credentials, font=t.f_small,
                       fg=t.fg_muted, bg=t.bg_soft, activebackground=t.bg_soft,
                       selectcolor=t.bg_input, anchor="w",
                       highlightthickness=0, bd=0).pack(anchor="w")

        # 动作
        btns = tk.Frame(b, bg=t.bg_soft)
        btns.pack(fill="x", pady=(10, 0))
        W.button(btns, t, "执行 6 层重置", command=self._confirm_reset_all,
                 variant="primary", small=True, icon="⚿").pack(side="left")
        W.button(btns, t, "重置设备码", command=self._confirm_reset_code,
                 variant="outline", small=True, icon="⚿").pack(side="left",
                                                               padx=(6, 0))
        W.button(btns, t, "打开备份目录", command=self._open_backups,
                 variant="ghost", small=True, icon="📁").pack(side="left",
                                                              padx=(6, 0))

        # 进度输出
        self._dev_log = tk.Text(b, width=1, height=6, font=t.f_mono,
                                bg=t.bg_input, fg=t.fg_muted, relief="flat",
                                highlightthickness=1,
                                highlightbackground=t.border, wrap="word")
        self._dev_log.pack(fill="x", pady=(8, 0))
        self._dev_log.insert("1.0", "尚未执行。点击上方按钮开始（可先查看下方扫描结果）。")
        self._dev_log.configure(state="disabled")

    def _refresh_device(self):
        t = self.theme
        for w in self._dev_layers.winfo_children():
            w.destroy()
        try:
            from ..services import device_reset as D
            layers = D.scan()
        except Exception as e:  # noqa: BLE001
            tk.Label(self._dev_layers, text=f"扫描失败：{e!r}", font=t.f_tiny,
                     fg=t.red_fg, bg=t.bg_soft, anchor="w").pack(fill="x")
            return
        total = sum(len(L["found"]) for L in layers if not L["optional"])
        self._dev_count.configure(text=f"已发现 {total} 个可重置目标")
        marks = "①②③④⑤⑥"
        for i, L in enumerate(layers):
            row = tk.Frame(self._dev_layers, bg=t.bg_soft)
            row.pack(fill="x", pady=1)
            n = len(L["found"])
            tone = "green" if n else "slate"
            fg, _ = t.tone(tone)
            tk.Label(row, text=marks[i] if i < len(marks) else "·",
                     font=t.f_small, fg=fg, bg=t.bg_soft, width=2).pack(side="left")
            tk.Label(row, text=L["name"], font=t.f_small, fg=t.fg_muted,
                     bg=t.bg_soft, anchor="w").pack(side="left")
            if L["optional"]:
                W.Badge(row, t, "需管理员", tone="amber").pack(side="right")
            else:
                W.Badge(row, t, f"{n} 项", tone=tone).pack(side="right")

    def _dev_write(self, text):
        try:
            self._dev_log.configure(state="normal")
            self._dev_log.delete("1.0", "end")
            self._dev_log.insert("1.0", text)
            self._dev_log.configure(state="disabled")
        except tk.TclError:
            pass

    def _dev_append(self, text):
        try:
            self._dev_log.configure(state="normal")
            self._dev_log.insert("end", text + "\n")
            self._dev_log.see("end")
            self._dev_log.configure(state="disabled")
        except tk.TclError:
            pass

    def _confirm_reset_all(self):
        levels = ["machineid", "telemetry", "aha", "tinystorage", "webview"]
        if self._opt_machineguid.get():
            levels.append("machineguid")
        self._ask_confirm(
            "确认执行 6 层设备标识重置",
            "将重置以下全部设备标识层：\n\n"
            "  ① machineid\n  ② storage.json telemetry\n"
            "  ③ storage.json aha.device\n  ④ TinyStorage / Local Storage\n"
            "  ⑤ 注册表 MachineGuid" + ("（已勾选）" if self._opt_machineguid.get()
                                        else "（未勾选，将跳过）") + "\n"
            "  ⑥ webview / Network 追踪数据\n\n"
            "建议先关闭 Trae 客户端再执行。每个文件都会自动备份。",
            lambda: self._do_reset(levels, clear_creds=False))

    def _confirm_reset_code(self):
        self._ask_confirm(
            "确认重置设备码",
            "将对本机全部 Trae 系列目录执行：\n\n"
            "  · 轮换 machineid 与 storage.json telemetry 标识\n"
            "  · 清除 storage.json 全部登录凭据键（强制登出所有账号）\n"
            "  · 每个文件自动生成带时间戳的备份\n\n"
            "仅操作用户数据文件，无需管理员权限。\n"
            "请先完全关闭 Trae 再执行；执行后需重新登录账号。",
            lambda: self._do_reset(["machineid", "telemetry"], clear_creds=True))

    def _ask_confirm(self, title, body, on_ok):
        t = self.theme
        dlg = W.Modal(self, t, title, width=520, height=340)
        txt = tk.Text(dlg.content, width=1, height=12, font=t.f_small,
                      bg=t.bg_input, fg=t.fg, relief="flat",
                      highlightthickness=1, highlightbackground=t.border,
                      wrap="word")
        txt.insert("1.0", body)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        W.button(dlg.footer, t, "取消", command=dlg.close,
                 variant="outline", small=True).pack(side="right")
        W.button(dlg.footer, t, "确认重置",
                 command=lambda: (dlg.close(), on_ok()),
                 variant="primary", small=True).pack(side="right", padx=(0, 6))
        dlg.show()

    def _do_reset(self, levels, clear_creds):
        if self.busy:
            self.app.toast("warn", "已有操作正在进行中")
            return
        self.busy = True
        self._dev_write("正在执行…\n")
        self._dev_append("（完成后会在此显示逐层结果）")

        def progress(msg):
            self.app.ui_call(lambda m=msg: self._dev_append(m))

        def work():
            try:
                from ..services import device_reset as D
                ok, lines, bdir = D.reset(
                    levels=levels, clear_credentials=clear_creds,
                    rotate_machineguid=("machineguid" in levels),
                    progress=progress)
                self.app.ui_call(lambda: self._dev_done(ok, lines, bdir))
            except Exception as e:  # noqa: BLE001
                self.app.ui_call(lambda: self._dev_done(False, [repr(e)], None))

        threading.Thread(target=work, daemon=True).start()

    def _dev_done(self, ok, lines, bdir):
        self.busy = False
        self._dev_write("\n".join(lines))
        if ok:
            self.app.toast("success", "设备标识重置完成")
        else:
            self.app.toast("error", "重置未完成，详见进度输出")
        self._refresh_device()

    def _open_backups(self):
        try:
            from ..services import device_reset as D
            root = D._backup_root()
            os.makedirs(root, exist_ok=True)
            if D.open_path(root):
                self.app.toast("info", "已在资源管理器中打开备份目录")
            else:
                self.app.toast("warn", f"备份目录：{root}")
        except Exception as e:  # noqa: BLE001
            self.app.toast("error", f"打开失败：{e!r}")

    # ---------------- 数据维护 ----------------

    def _build_maintenance(self, t):
        """签到记录 / 配置 / 日志的快捷访问与清理。"""
        card = W.Card(self.body, t, padding=14)
        card.pack(fill="x", pady=(12, 0))
        b = card.body
        tk.Label(b, text="数据与维护", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(fill="x")
        tk.Label(b, text="签到记录、配置与日志都与命令行/计划任务共用同一份文件",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                 anchor="w").pack(fill="x", pady=(2, 8))

        row = tk.Frame(b, bg=t.bg_soft)
        row.pack(fill="x")
        W.button(row, t, "打开记录文件夹",
                 command=lambda: self._open(scheduler.open_records_folder),
                 variant="outline", small=True, icon="📁").pack(side="left")
        W.button(row, t, "打开签到记录 CSV",
                 command=lambda: self._open(scheduler.open_record_csv),
                 variant="outline", small=True, icon="▤").pack(side="left",
                                                              padx=(6, 0))
        W.button(row, t, "打开 config.json",
                 command=lambda: self._open(scheduler.open_config_file),
                 variant="outline", small=True, icon="⚙").pack(side="left",
                                                              padx=(6, 0))

        row1b = tk.Frame(b, bg=t.bg_soft)
        row1b.pack(fill="x", pady=(8, 0))
        W.button(row1b, t, "运行环境自检", command=self._open_envcheck,
                 variant="primary", small=True).pack(side="left")
        _eh = tk.Label(row1b, text="逐项检查数据目录 / 依赖 / 浏览器 / 客户端 / "
                                   "令牌 / 计划任务 / 网络是否可用",
                       font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w",
                       justify="left")
        _eh.pack(side="left", padx=(8, 0), fill="x", expand=True)
        W.auto_wrap(_eh)

        tk.Frame(b, bg=t.border, height=1).pack(fill="x", pady=(10, 8))

        row2 = tk.Frame(b, bg=t.bg_soft)
        row2.pack(fill="x")
        self._keep_days = tk.StringVar(value="30")
        tk.Label(row2, text="清理截图：保留最近", font=t.f_small, fg=t.fg_muted,
                 bg=t.bg_soft).pack(side="left")
        tk.Entry(row2, textvariable=self._keep_days, font=t.f_small, width=5,
                 bg=t.bg_input, fg=t.fg, relief="flat", highlightthickness=1,
                 highlightbackground=t.border, justify="center").pack(
            side="left", padx=4)
        tk.Label(row2, text="天", font=t.f_small, fg=t.fg_muted,
                 bg=t.bg_soft).pack(side="left")
        W.button(row2, t, "清理", command=self._purge, variant="outline",
                 small=True).pack(side="left", padx=(8, 0))
        W.button(row2, t, "清空历史记录", command=self._clear_history,
                 variant="ghost", small=True).pack(side="left", padx=(6, 0))
        self._maint_hint = tk.Label(b, text="", font=t.f_tiny, fg=t.fg_dim,
                                    bg=t.bg_soft, anchor="w", justify="left",
                                    wraplength=740)
        self._maint_hint.pack(fill="x", pady=(6, 0))

    # ---------------- 环境自检 ----------------

    def _open_envcheck(self):
        """逐项检查「这台机器上所有功能能不能用」，并给出修复建议。"""
        t = self.theme
        dlg = W.Modal(self.app, t, "环境自检", width=790, height=560)
        head = tk.Frame(dlg.content, bg=t.bg_soft)
        head.pack(fill="x")
        self._env_verdict = tk.Label(head, text="正在自检…", font=t.f_body,
                                     fg=t.fg, bg=t.bg_soft, anchor="w")
        self._env_verdict.pack(side="left", fill="x", expand=True)
        W.auto_wrap(self._env_verdict)

        # ★ 检查项数量无上界 → 必须用可滚动容器；
        #   而且循环后要显式 resync()，否则 scrollregion 还是旧高度（滚不到底）。
        holder = W.ScrollFrame(dlg.content, t)
        holder.pack(fill="both", expand=True, pady=(10, 0))
        self._env_holder = holder
        self._env_inner = holder.inner
        self._env_dlg = dlg
        self._env_items = []
        self._env_running = False

        W.button(dlg.footer, t, "关闭", command=dlg.close,
                 variant="outline", small=True).pack(side="right")
        W.button(dlg.footer, t, "重新检测", command=self._run_envcheck,
                 variant="outline", small=True).pack(side="right", padx=(0, 6))
        W.button(dlg.footer, t, "复制报告", command=self._copy_envcheck,
                 variant="primary", small=True).pack(side="right", padx=(0, 6))
        dlg.show()
        self._run_envcheck()

    def _run_envcheck(self):
        if getattr(self, "_env_running", False):
            return
        self._env_running = True
        try:
            self._env_verdict.configure(text="正在自检…（约需数秒）", fg=self.theme.fg)
        except tk.TclError:
            return
        for w in self._env_inner.winfo_children():
            w.destroy()

        def work():
            try:
                items = envcheck.run()
            except Exception as e:  # noqa: BLE001
                items = [{"key": "fatal", "label": "自检本身出错",
                          "status": "FAIL", "detail": repr(e),
                          "hint": "请把这条信息反馈给开发者"}]
            self.app.ui_call(lambda: self._env_done(items))

        threading.Thread(target=work, daemon=True, name="envcheck").start()

    def _env_done(self, items):
        self._env_running = False
        self._env_items = items
        try:
            if not self._env_dlg.winfo_exists():
                return
        except tk.TclError:
            return
        t = self.theme
        verdict, tone = envcheck.overall(items)
        c = envcheck.summary(items)
        try:
            self._env_verdict.configure(
                text="%s    （通过 %d · 注意 %d · 不可用 %d）"
                     % (verdict, c["OK"] + c["SKIP"], c["WARN"], c["FAIL"]),
                fg=t.tone(tone)[0])
        except tk.TclError:
            return

        for w in self._env_inner.winfo_children():
            w.destroy()
        for it in items:
            row = tk.Frame(self._env_inner, bg=t.bg_soft)
            row.pack(fill="x", pady=3)
            fg, bg = t.tone(_ENV_TONE.get(it["status"], "slate"))
            tk.Label(row, text=it["status"], font=t.f_tiny, fg=fg, bg=bg,
                     width=5).pack(side="left", padx=(0, 8), anchor="n")
            body = tk.Frame(row, bg=t.bg_soft)
            body.pack(side="left", fill="x", expand=True)
            for text, font, color in (
                    (it["label"], t.f_small, t.fg),
                    (str(it["detail"] or ""), t.f_tiny, t.fg_dim),
                    ("→ " + str(it["hint"]) if (it["hint"] and it["status"]
                                                in ("FAIL", "WARN")) else "",
                     t.f_tiny, t.tone("amber")[0])):
                if not text:
                    continue
                lbl = tk.Label(body, text=text, font=font, fg=color,
                               bg=t.bg_soft, anchor="w", justify="left")
                lbl.pack(fill="x")
                W.auto_wrap(lbl)
        try:
            self._env_holder.resync()
        except Exception:  # noqa: BLE001
            pass

    def _copy_envcheck(self):
        if not getattr(self, "_env_items", None):
            self.app.toast("warn", "还没有自检结果")
            return
        try:
            self.app.clipboard_clear()
            self.app.clipboard_append(envcheck.format_text(self._env_items))
            self.app.toast("success", "自检报告已复制到剪贴板")
        except tk.TclError as e:
            self.app.toast("error", "复制失败：%r" % (e,))

    def _open(self, fn):
        ok, msg = fn()
        self.app.toast("success" if ok else "error",
                       "已打开" if ok else f"打开失败：{msg}")

    # ---------------- 账号迁移（换设备 / 新装 exe） ----------------

    # ---------------- 账号凭据自动刷新 ----------------

    def _build_credentials(self, t):
        """凭据寿命说明 + 可配置的后台重新捕获。

        为什么单开一张卡：三个平台的凭据寿命差两个数量级（TraeCN 8 小时 /
        TraeWork 会话 5 天 / WorkBuddy 60 天），用户没法凭直觉判断「要不要去
        重新捕获一下」。把寿命、当前剩余时间、上次刷新时间一起摆出来，
        用户就不用猜了。详见 services/credential_refresh.py。
        """
        card = W.Card(self.body, t, padding=14)
        card.pack(fill="x", pady=(12, 0))
        b = card.body
        tk.Label(b, text="账号凭据自动刷新", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(fill="x")
        intro = tk.Label(
            b, text="三个平台的登录凭据寿命差别很大。程序在每次签到前都会自动"
                    "重新捕获一次（这是保证签到用的是最新凭据，不受下面设置影响）。"
                    "这里的设置只管「程序开着的时候，后台多久去看一眼」，"
                    "让界面上的有效期显示保持准确 —— "
                    "除了按间隔刷新，重新打开 Trae CN IDE / WorkBuddy 时也会"
                    "立刻刷新（见下面的开关）。",
            font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w",
            justify="left")
        intro.pack(fill="x", pady=(2, 8))
        W.auto_wrap(intro)

        # ★ 总览：直接回答「为什么只有 TraeWork 不是桌面端捕获」
        #   （用户的原始疑问就是这一条，与其让他猜，不如写在最前面）
        summ = tk.Label(b, text=CR.CAPTURE_SUMMARY, font=t.f_tiny,
                        fg=t.fg_dim, bg=t.bg_soft, anchor="w", justify="left")
        summ.pack(fill="x", pady=(0, 8))
        W.auto_wrap(summ)

        # 寿命说明（实测值，来自 credential_refresh.LIFESPANS）
        # ★ 每个平台一块：第一行是「寿命 + 捕获方式」（用户最关心的两个数），
        #   下面缩进写来源 / 为什么 / 续期。原来是一行塞四段、还只写「浏览器
        #   会话 Cookie」，用户读完只记住了「怎么只有它不一样」。
        for row in CR.lifespan_rows():
            blk = tk.Frame(b, bg=t.bg_soft)
            blk.pack(fill="x", pady=(0, 6))
            head = tk.Label(
                blk, text="· %s：%s　·　捕获方式：%s"
                          % (row["label"], row["life_text"], row["capture"]),
                font=t.f_tiny, fg=t.fg_muted, bg=t.bg_soft,
                anchor="w", justify="left")
            head.pack(fill="x")
            W.auto_wrap(head)
            for key, prefix in (("where", "来源"), ("why", "为什么"),
                                ("renew", "续期")):
                if not row.get(key):
                    continue
                sub = tk.Label(blk, text="%s：%s" % (prefix, row[key]),
                               font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                               anchor="w", justify="left")
                sub.pack(fill="x", padx=(14, 0))
                W.auto_wrap(sub)

        # 开关：即时落盘（和「运行选项」里的开关同类，不需要重注册任务）
        self._cred_sw = self._switch_row(
            b, "程序运行时后台自动重新捕获", "credentials", "auto_refresh",
            hint="关闭后仍会在每次签到前自动捕获，只是界面上的有效期可能滞后",
            on_change=self._refresh_cred_info)

        # 间隔：单独一行 + 快捷档位按钮（数值需要校验，故不逐键落盘）
        sec = CR.cfg_section()
        row = tk.Frame(b, bg=t.bg_soft)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text="刷新间隔", font=t.f_small, fg=t.fg_muted,
                 bg=t.bg_soft, anchor="w", width=22).pack(side="left")
        self._cred_hours = tk.StringVar(
            value=str(sec["refresh_interval_hours"]))
        tk.Entry(row, textvariable=self._cred_hours, font=t.f_small,
                 bg=t.bg_input, fg=t.fg, relief="flat", highlightthickness=1,
                 highlightbackground=t.border, highlightcolor=t.accent,
                 width=6, justify="center").pack(side="left", ipady=3)
        tk.Label(row, text="小时", font=t.f_small, fg=t.fg_dim, bg=t.bg_soft,
                 anchor="w").pack(side="left", padx=(6, 12))
        for h in CR.QUICK_CHOICES:
            W.button(row, t, "%d 小时" % h,
                     command=lambda v=h: self._cred_hours.set(str(v)),
                     variant="outline", small=True).pack(side="left", padx=(0, 4))
        W.button(row, t, "保存", command=self._save_cred,
                 variant="primary", small=True).pack(side="left", padx=(6, 0))

        # ★ 新增（2026-09-21）：源变化触发 —— 用户原话
        #   「确保重新打开对应软件自动获取令牌更新」。
        #   为什么必须单列一个开关而不是并进间隔：这两件事的**语义**不同 ——
        #   间隔是「周期性兜底」，源变化是「事件驱动」。把间隔设成 24 小时
        #   完全合理（省事），但「重开 IDE 拿到新令牌也要等 24 小时」不合理。
        self._cred_watch_sw = self._switch_row(
            b, "检测到软件重新打开时立即刷新", "credentials",
            "refresh_on_source_change",
            hint="Trae CN IDE / WorkBuddy 一打开就会写入新令牌；这里一发现凭据"
                 "源文件变化就立刻重新捕获，不用等上面的间隔"
                 "（最短 %d 分钟一次，避免频繁读写）"
                 % max(1, CR.SOURCE_WATCH_MIN_GAP_S // 60),
            on_change=self._refresh_cred_info)

        # 当前状态 + 立即刷新
        self._cred_info = tk.Label(b, text="", font=t.f_tiny, fg=t.fg_muted,
                                   bg=t.bg_soft, anchor="w", justify="left")
        self._cred_info.pack(fill="x", pady=(8, 0))
        W.auto_wrap(self._cred_info)

        row2 = tk.Frame(b, bg=t.bg_soft)
        row2.pack(fill="x", pady=(6, 0))
        W.button(row2, t, "立即重新捕获", command=self._refresh_creds_now,
                 variant="outline", small=True).pack(side="left")
        self._cred_result = tk.Label(row2, text="", font=t.f_tiny,
                                     fg=t.fg_dim, bg=t.bg_soft, anchor="w",
                                     justify="left")
        self._cred_result.pack(side="left", padx=(8, 0), fill="x", expand=True)
        W.auto_wrap(self._cred_result)
        self._refresh_cred_info()

    def _refresh_cred_info(self, _items=None, _head=None, _tail=None):
        """把「上次刷新 / 下次何时刷 / 每个账号还剩多久」摆到界面上。

        ★ `CR.entries()` 内部会跑 `tc_checkin._scan_ide_logs()` —— 实测 **0.39 s**
          （要读 IDE 日志内容跑正则找 JWT）。它原先被 `build()` 与 `on_show()`
          **同步**调用（本页实测卡 0.4~0.9 s，是「进设置页要等一下」的主因）。
          现在改成：先给占位/沿用上次文本，后台线程取一次再回填 —— 与
          `accounts` 页探测客户端、`credits` 页查额度同一套路。
        """
        if _items is None:
            # 只在**还没有任何内容**时显示占位，避免每次切换开关都闪一下
            if not getattr(self, "_cred_info_text", ""):
                try:
                    self._cred_info.configure(text="正在读取凭据状态…")
                except tk.TclError:
                    pass
            if getattr(self, "_cred_reading", False):
                return                      # 单飞：上一轮还没回来就别再起一个
            self._cred_reading = True

            def work():
                try:
                    items = CR.entries()
                    head = "上次后台刷新：%s" % _ago_text(CR.last_refresh())
                    tail = CR.next_due_text()
                except Exception as e:  # noqa: BLE001
                    items, head, tail = [], "状态读取失败：%r" % (e,), ""
                self.app.ui_call(
                    lambda: self._refresh_cred_info(items, head, tail))

            threading.Thread(target=work, daemon=True,
                             name="cred-info").start()
            return

        self._cred_reading = False
        items, head, tail = _items, _head or "", _tail or ""
        parts = []
        for e in items:
            if e.get("platform") == "traework" and not e.get("logged_in", True):
                parts.append("%s：未登录" % e["account"])
            elif e.get("exp"):
                parts.append("%s：剩余 %s%s"
                             % (e["account"], e["left_text"],
                                "（即将过期）" if e.get("soon") else ""))
        txt = head + " · " + tail
        if parts:
            txt += "\n当前凭据：" + "；".join(parts)
        else:
            txt += "\n当前凭据：（尚未捕获到任何账号，请到「账号管理」登录）"
        self._cred_info_text = txt
        try:
            self._cred_info.configure(text=txt)
        except tk.TclError:
            pass

    def _save_cred(self):
        """校验并落盘刷新间隔（0 = 关闭后台刷新）。

        ★ 校验范围用 `CR.MIN/MAX_INTERVAL_HOURS`，**和 `cfg_section` 同一套常量**：
          两边不一致时会出现「保存成功但读回来被改成别的值」的静默不一致
          （见 credential_refresh.py 里 MIN_INTERVAL_HOURS 的注释）。
          存完再把输入框回写成「真正生效的值」，界面永不显示未生效的数字。
        """
        raw = (self._cred_hours.get() or "").strip()
        try:
            hours = int(float(raw))
        except (TypeError, ValueError):
            self.app.toast("error", "刷新间隔要填数字（小时）")
            return
        if not (CR.MIN_INTERVAL_HOURS <= hours <= CR.MAX_INTERVAL_HOURS):
            self.app.toast("error", "刷新间隔请在 %d ~ %d 小时之间（0 = 关闭）"
                           % (CR.MIN_INTERVAL_HOURS, CR.MAX_INTERVAL_HOURS))
            return
        try:
            cfg = C.load_config()
            cfg.setdefault("credentials", {})["refresh_interval_hours"] = hours
            cfg["credentials"]["auto_refresh"] = bool(hours > 0)
            C.save_config(cfg)
        except Exception as e:  # noqa: BLE001
            self.app.toast("error", "保存失败：%r" % (e,))
            return
        # 回读一次，确认落盘的就是即将生效的那个值（读不到就不改输入框）
        try:
            effective = CR.cfg_section()["refresh_interval_hours"]
            self._cred_hours.set(str(effective))
        except Exception:  # noqa: BLE001
            effective = hours
        self.app.toast("success", "已保存：每 %d 小时自动刷新" % effective
                       if effective > 0 else "已关闭后台自动刷新")
        self._refresh_cred_info()

    def _refresh_creds_now(self):
        """手动触发一次重新捕获（后台线程，不阻塞界面）。"""
        if self.busy:
            self.app.toast("warn", "已有操作正在进行中")
            return
        self.busy = True
        try:
            self._cred_result.configure(text="正在重新捕获…")
        except tk.TclError:
            pass

        def work():
            try:
                ok, detail, _stats = CR.refresh()
            except Exception as e:  # noqa: BLE001
                ok, detail = False, repr(e)
            self.app.ui_call(lambda: self._cred_done(ok, detail))

        threading.Thread(target=work, daemon=True, name="cred-now").start()

    def _cred_done(self, ok, detail):
        self.busy = False
        try:
            self._cred_result.configure(text=detail)
        except tk.TclError:
            pass
        self.app.toast("success" if ok else "error", detail)
        # 捕获可能带来新账号 → 刷新全局账号缓存（账号页/概览页跟着更新）
        try:
            self.app.refresh_accounts(on_done=self._refresh_cred_info)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_cred_info()

    def _build_migration(self, t):
        """导出 / 导入账号包 —— 换设备、新装 exe 的「一键搬家」。"""
        card = W.Card(self.body, t, padding=14)
        card.pack(fill="x", pady=(12, 0))
        b = card.body
        tk.Label(b, text="账号迁移（换设备 / 新装 exe）", font=t.f_card_title,
                 fg=t.fg, bg=t.bg_soft, anchor="w").pack(fill="x")
        intro = tk.Label(
            b, text="把账号列表、登录态与签到设置打包成一个文件，"
                    "在新设备上导入即可继续用（无需重新登录）。",
            font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w",
            justify="left")
        intro.pack(fill="x", pady=(2, 6))
        W.auto_wrap(intro)

        self._mig_info = tk.Label(b, text="", font=t.f_tiny, fg=t.fg_muted,
                                  bg=t.bg_soft, anchor="w", justify="left")
        self._mig_info.pack(fill="x")
        W.auto_wrap(self._mig_info)

        row = tk.Frame(b, bg=t.bg_soft)
        row.pack(fill="x", pady=(8, 0))
        W.button(row, t, "导出完整账号包", command=lambda: self._export(True),
                 variant="primary", small=True).pack(side="left")
        W.button(row, t, "导出仅配置", command=lambda: self._export(False),
                 variant="outline", small=True).pack(side="left", padx=(6, 0))
        W.button(row, t, "导入账号包…", command=self._import_pick,
                 variant="outline", small=True).pack(side="left", padx=(6, 0))

        warn = tk.Label(
            b, text="⚠「完整账号包」含明文登录凭据（等同于登录态），"
                    "请只在本人设备之间传递，勿公开分享或上传公开网盘。",
            font=t.f_tiny, fg=t.tone("amber")[0], bg=t.bg_soft, anchor="w",
            justify="left")
        warn.pack(fill="x", pady=(6, 0))
        W.auto_wrap(warn)

        self._mig_result = tk.Label(b, text="", font=t.f_tiny, fg=t.fg_dim,
                                    bg=t.bg_soft, anchor="w", justify="left")
        self._mig_result.pack(fill="x", pady=(6, 0))
        W.auto_wrap(self._mig_result)
        self._refresh_mig_info()

    def _refresh_mig_info(self):
        try:
            _payload, s = AB.collect(with_credentials=True)
            if s["total_accounts"] == 0:
                txt = "本机还没有可迁移的账号（请先在「账号管理」里完成登录 / 捕获）"
            else:
                txt = ("本机现有：TraeWork %d · TraeCN %d · WorkBuddy %d"
                       % (s["traework"], s["traecn"], s["workbuddy"]))
                if s["has_device_id"]:
                    txt += " · 设备标识已就绪"
        except Exception as e:  # noqa: BLE001
            txt = "统计失败：%r" % (e,)
        try:
            self._mig_info.configure(text=txt)
        except tk.TclError:
            pass

    def _set_mig_result(self, text):
        try:
            self._mig_result.configure(text=text)
        except tk.TclError:
            pass

    def _export(self, with_credentials):
        path = AB.default_path(with_credentials)
        ok, detail, real = AB.export_bundle(path,
                                           with_credentials=with_credentials)
        self._set_mig_result(detail)
        self.app.toast("success" if ok else "error",
                       "账号包已导出" if ok else "导出失败")
        if ok:
            self._show_text_modal("导出完成", detail, path=real)
        self._refresh_mig_info()

    def _import_pick(self):
        """选择账号包文件 → 校验 → 展示预览 → 确认后导入。"""
        from tkinter import filedialog
        try:
            path = filedialog.askopenfilename(
                parent=self.app, title="选择 TraeBuddy 账号包",
                initialdir=os.path.dirname(AB.default_path(True)),
                filetypes=[("TraeBuddy 账号包", "*.json"),
                           ("所有文件", "*.*")])
        except Exception as e:  # noqa: BLE001
            self.app.toast("error", "无法打开文件选择框：%r" % (e,))
            return
        if not path:
            return
        ok, detail, info = AB.inspect_bundle(path)
        if not ok:
            self._set_mig_result(detail)
            self.app.toast("error", detail)
            return
        self._confirm_import(path, info)

    def _confirm_import(self, path, info):
        """导入前把「会发生什么」摆清楚，避免用户糊里糊涂覆盖本机数据。"""
        lines = [
            "账号包来源：%s（%s）" % (info["hostname"], info["source_root"]),
            "导出时间  ：%s" % info["exported_at"],
            "",
            "包含内容：",
            "  · TraeWork 账号 %d 个" % info["traework"],
            "  · TraeCN 令牌 %d 个" % info["traecn"],
            "  · WorkBuddy 令牌 %d 个" % info["workbuddy"],
            "  · 登录凭据：%s" % ("含（明文）" if info["with_credentials"] else "不含"),
            "  · 设备标识：%s" % ("含" if info["has_device_id"] else "不含"),
            "  · 签到时间等设置：%s" % ("含" if info["has_schedule"] else "不含"),
            "",
            "导入规则：",
            "  · 账号列表取**并集** —— 不会丢掉本机已有的账号",
            "  · 同一账号的令牌保留**更新的那一份**",
            "  · 签到时间 / 选项等设置**以账号包为准**",
            "  · 设备标识只在本机缺失时填入，绝不覆盖本机已有的值",
            "",
            "导入前会自动把本机现有数据备份到 logs/bundle_backup_<时间戳>/，可回退。",
        ]
        if info["with_credentials"]:
            lines += ["", "⚠ 该包含明文登录凭据，请确认来源可信。"]
        t = self.theme
        dlg = W.Modal(self, t, "确认导入账号包", width=600, height=430)
        txt = tk.Text(dlg.content, width=1, height=16, font=t.f_small,
                      bg=t.bg_input, fg=t.fg, relief="flat",
                      highlightthickness=1, highlightbackground=t.border,
                      wrap="word")
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        W.button(dlg.footer, t, "取消", command=dlg.close,
                 variant="outline", small=True).pack(side="right")
        W.button(dlg.footer, t, "确认导入",
                 command=lambda: (dlg.close(), self._do_import(path)),
                 variant="primary", small=True).pack(side="right", padx=(0, 6))
        dlg.show()

    def _do_import(self, path):
        ok, detail, _stats = AB.import_bundle(path)
        self._set_mig_result(detail)
        self.app.toast("success" if ok else "error",
                       "导入完成" if ok else "导入失败")
        if ok:
            # 账号已变更 —— 必须强制刷新缓存，否则列表还是导入前的旧快照
            self.app.refresh_accounts(on_done=self._refresh_mig_info)
            self.app._refresh_topbar()
            self._show_text_modal("导入完成", detail)
        self._refresh_mig_info()

    def _show_text_modal(self, title, body, path=None):
        """等宽文本框弹窗（内容长度可变 → 用 Text，不做定尺寸裁切）。"""
        t = self.theme
        dlg = W.Modal(self, t, title, width=600, height=330)
        txt = tk.Text(dlg.content, width=1, height=11, font=t.f_small,
                      bg=t.bg_input, fg=t.fg, relief="flat",
                      highlightthickness=1, highlightbackground=t.border,
                      wrap="word")
        txt.insert("1.0", body)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        if path:
            W.button(dlg.footer, t, "打开所在文件夹",
                     command=lambda: self._open_dir_of(path),
                     variant="outline", small=True).pack(side="left")
        W.button(dlg.footer, t, "关闭", command=dlg.close,
                 variant="outline", small=True).pack(side="right")
        dlg.show()

    def _open_dir_of(self, path):
        try:
            folder = os.path.dirname(path) or "."
            if os.path.isdir(folder):
                os.startfile(folder)  # noqa: S606
                return
        except Exception:  # noqa: BLE001
            pass
        self.app.toast("warn", "无法打开所在文件夹，请手动前往：%s"
                       % os.path.dirname(path))

    def _purge(self):
        try:
            days = int(self._keep_days.get().strip() or 30)
        except ValueError:
            self.app.toast("warn", "保留天数必须是数字")
            return
        ok, msg = scheduler.purge_records(keep_days=days)
        self._maint_hint.configure(text=msg)
        self.app.toast("success" if ok else "error", msg)

    def _clear_history(self):
        self._ask_confirm(
            "确认清空签到历史记录",
            "将清空 logs/history.jsonl（最近签到流水）。\n\n"
            "注意：\n"
            "  · 不会影响 logs/签到记录.csv（人类可读记录会保留）\n"
            "  · 不会影响 state.json（今日已签状态不受影响）\n"
            "  · 操作不可撤销，建议先备份 logs 目录",
            lambda: self._do_clear_history())

    def _do_clear_history(self):
        ok, msg = scheduler.purge_records(keep_days=0, clear_history=True)
        self._maint_hint.configure(text=msg)
        self.app.toast("success" if ok else "error", msg)

    # ---------------- 签到记录导出位置 ----------------

    def _build_record_export(self, t):
        """「签到记录.csv」的自定义存放位置。

        为什么是**额外一份副本**而不是把主文件挪走：
        主记录是 `logs/history.jsonl` 的重建产物，概览页 / 流水页 / 命令行
        `status` 都按数据根那个路径读它。一旦改成只写自定义位置，拔掉 U 盘、
        网盘没同步、路径没权限时，界面就变成「记录空了」，属于把附加功能
        变成了核心链路的单点故障。因此这里只做**同步副本**，主记录照旧。
        """
        card = W.Card(self.body, t, padding=14)
        card.pack(fill="x", pady=(12, 0))
        b = card.body
        tk.Label(b, text="签到记录导出位置", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(fill="x")
        tk.Label(b, text="默认只存在数据目录。开启后每次签到会**额外同步一份**"
                         "到你指定的位置（桌面、网盘同步盘、共享盘都行），"
                         "原来那份仍然保留",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w",
                 justify="left").pack(fill="x", pady=(2, 8))

        opts = C.load_config().get("options") or {}
        self._exp_on = tk.BooleanVar(
            value=C.as_bool(opts.get("record_export_enabled"), False))
        row = tk.Frame(b, bg=t.bg_soft)
        row.pack(fill="x", pady=3)
        tk.Label(row, text="启用自定义导出位置", font=t.f_small, fg=t.fg_muted,
                 bg=t.bg_soft, anchor="w").pack(side="left")
        W.Switch(row, t, value=self._exp_on.get(),
                 command=lambda v: self._exp_on.set(bool(v))).pack(side="right")

        prow = tk.Frame(b, bg=t.bg_soft)
        prow.pack(fill="x", pady=(6, 0))
        self._exp_path = tk.Entry(prow, font=t.f_body, bg=t.bg_input, fg=t.fg,
                                  relief="flat", highlightthickness=1,
                                  highlightbackground=t.border,
                                  highlightcolor=t.accent)
        self._exp_path.pack(side="left", fill="x", expand=True, ipady=4)
        self._exp_path.insert(0, str(opts.get("record_export_path") or ""))
        W.button(prow, t, "选文件夹", command=self._exp_pick_dir,
                 variant="outline", small=True, icon="📁").pack(
            side="left", padx=(8, 0))
        W.button(prow, t, "指定文件名", command=self._exp_pick_file,
                 variant="outline", small=True, icon="▤").pack(
            side="left", padx=(6, 0))

        brow = tk.Frame(b, bg=t.bg_soft)
        brow.pack(fill="x", pady=(8, 0))
        W.button(brow, t, "保存设置", command=self._exp_save,
                 variant="primary", small=True).pack(side="left")
        W.button(brow, t, "立即导出一份", command=self._exp_export,
                 variant="outline", small=True).pack(side="left", padx=(6, 0))
        W.button(brow, t, "打开所在文件夹", command=self._exp_open_dir,
                 variant="ghost", small=True).pack(side="left", padx=(6, 0))
        W.button(brow, t, "恢复默认位置", command=self._exp_reset,
                 variant="ghost", small=True).pack(side="left", padx=(6, 0))

        self._exp_hint = W.auto_wrap(
            tk.Label(b, text="", font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                     anchor="w", justify="left", wraplength=880))
        self._exp_hint.pack(fill="x", pady=(6, 0))
        self._refresh_export_info()

    def _exp_cfg_from_input(self):
        """用界面当前输入拼一份配置（用于「立即导出」这类不必先保存的操作）。"""
        cfg = C.load_config()
        cfg.setdefault("options", {})
        cfg["options"]["record_export_enabled"] = bool(self._exp_on.get())
        cfg["options"]["record_export_path"] = self._exp_path.get().strip()
        return cfg

    def _exp_set_path(self, path):
        self._exp_path.delete(0, "end")
        self._exp_path.insert(0, path)
        self._exp_on.set(True)

    def _exp_pick_dir(self):
        """选文件夹 → 该文件夹下生成「签到记录.csv」。"""
        from tkinter import filedialog
        try:
            path = filedialog.askdirectory(
                parent=self.app, title="选择签到记录的导出文件夹")
        except Exception as e:  # noqa: BLE001
            self.app.toast("error", "无法打开文件夹选择框：%r" % (e,))
            return
        if path:
            self._exp_set_path(path)

    def _exp_pick_file(self):
        """直接指定完整文件名（可自定义文件名）。"""
        from tkinter import filedialog
        try:
            path = filedialog.asksaveasfilename(
                parent=self.app, title="选择签到记录的保存位置",
                defaultextension=".csv", initialfile="签到记录.csv",
                filetypes=[("CSV 表格", "*.csv"), ("所有文件", "*.*")])
        except Exception as e:  # noqa: BLE001
            self.app.toast("error", "无法打开保存框：%r" % (e,))
            return
        if path:
            self._exp_set_path(path)

    def _exp_save(self):
        ok, msg = C.save_record_export(self._exp_on.get(),
                                       self._exp_path.get().strip())
        if not ok:
            self.app.toast("error", "保存失败：%s" % msg)
            return
        self.app.toast("success", "导出设置已保存")
        self.app._refresh_topbar()
        self._refresh_export_info()

    def _exp_export(self):
        """按界面当前输入立即导出一份（未保存也能试）。"""
        cfg = self._exp_cfg_from_input()
        target = C.record_export_target(cfg)
        if not target:
            self.app.toast("warn", "请先开启开关并填写导出位置")
            return
        ok, info = C.export_record_now(cfg)
        if ok:
            self.app.toast("success", "已导出到：%s" % info)
        else:
            self.app.toast("error", "导出失败：%s" % info)
        self._refresh_export_info()

    def _exp_open_dir(self):
        target = C.record_export_target() or C.RECORD_PATH
        if not os.path.isfile(target):
            self.app.toast("warn", "该文件还没生成：%s" % target)
            return
        self._open_dir_of(target)

    def _exp_reset(self):
        """关掉自定义位置，回到「只存数据目录」的默认行为。"""
        self._exp_on.set(False)
        self._exp_path.delete(0, "end")
        ok, msg = C.save_record_export(False, "")
        self.app.toast("success" if ok else "error",
                       "已恢复默认位置" if ok else "保存失败：%s" % msg)
        self._refresh_export_info()

    def _refresh_export_info(self):
        """刷新说明行：主记录状态 + 自定义副本状态。

        ★ 只 stat 两个文件（微秒级），不读内容、不扫目录 —— 这个方法会在
          切页（on_show）时被调用，不能在这里做重活。
        """
        if not hasattr(self, "_exp_hint"):
            return
        lines = []
        try:
            st = os.stat(C.RECORD_PATH)
            lines.append("主记录（始终存在）：%s · %.1f KB · %s"
                         % (C.RECORD_PATH, st.st_size / 1024.0,
                            _ago_text(st.st_mtime) + "更新"))
        except OSError:
            lines.append("主记录：%s（尚未生成）" % C.RECORD_PATH)
        target = C.record_export_target()
        if target:
            if os.path.isfile(target):
                try:
                    st = os.stat(target)
                    lines.append("自定义副本：%s · %.1f KB · %s"
                                 % (target, st.st_size / 1024.0,
                                    _ago_text(st.st_mtime) + "更新"))
                except OSError:
                    lines.append("自定义副本：%s（读取失败）" % target)
            else:
                lines.append("自定义副本：%s —— 尚未生成，"
                             "点「立即导出一份」或等下一次签到自动同步" % target)
        else:
            lines.append("自定义副本：未启用（只保存在数据目录）")
        self._exp_hint.configure(text="\n".join(lines))

    # ---------------- 开关辅助 ----------------

    def _switch_row(self, master, label, section, key, hint="", on_change=None):
        """一行「文字 + 开关」，切换即落盘。

        `on_change`（可选）：保存成功后的回调 —— 给「改了这个开关要顺带刷新别的
        文案」的场景用（例如补跑的弹窗策略开关，改完要让下面的说明文字跟着变）。
        """
        t = self.theme
        cfg = C.load_config()
        # ★ 必须走 `C.as_bool`，不能写 `bool(...)`：config.json 是用户可以手编的，
        #   一旦写成 `"false"` 这种字符串，`bool("false")` 是 True ——
        #   界面上开关会显示成「开」，用户以为自己开了，其实是关的（反之亦然）。
        value = C.as_bool(cfg.get(section, {}).get(key, True), True)
        row = tk.Frame(master, bg=t.bg_soft)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, font=t.f_small, fg=t.fg_muted, bg=t.bg_soft,
                 anchor="w").pack(side="left")

        def _on(v):
            if self._save_switch(section, key, v) and on_change:
                on_change()

        sw = W.Switch(row, t, value=value, command=_on)
        sw.pack(side="right")
        if hint:
            tk.Label(master, text="　" + hint, font=t.f_tiny, fg=t.fg_dim,
                     bg=t.bg_soft, anchor="w").pack(fill="x")
        return sw

    def _save_switch(self, section, key, value):
        """落盘一个布尔开关；返回是否成功（调用方据此决定要不要刷关联文案）。"""
        try:
            cfg = C.load_config()
            cfg.setdefault(section, {})[key] = bool(value)
            C.save_config(cfg)
            self.app.toast("success", "设置已保存")
            self.app._refresh_topbar()
            return True
        except Exception as e:  # noqa: BLE001
            self.app.toast("error", f"保存失败：{e!r}")
            return False

    def _toggle_theme(self, value):
        self.app.set_theme(value, persist=True)

    # ---------------- 签到时间 ----------------

    def on_show(self):
        cfg = C.load_config()
        times = cfg.get("schedule", {}).get("daily_times", [])
        self._time_entry.delete(0, "end")
        self._time_entry.insert(0, ", ".join(times))
        self._load_retry()
        self._refresh_chain()
        self._refresh_task()
        self._refresh_device()
        self._refresh_mig_info()
        self._refresh_cred_info()
        self._refresh_export_info()

    def _save_times(self):
        if self.busy:
            self.app.toast("warn", "已有操作正在进行中")
            return
        raw = self._time_entry.get().strip()
        parts = [p.strip() for p in re.split(r"[,，;；\s]+", raw) if p.strip()]
        if not parts:
            self.app.toast("warn", "请至少填写一个时间点，例如 09:00")
            return
        if len(parts) > 4:
            self.app.toast("warn", "最多 4 个时间点")
            return
        for p in parts:
            if not re.fullmatch(r"\d{1,2}:\d{2}", p):
                self.app.toast("warn", f"时间格式无效：{p}（应为 HH:MM）")
                return
        self.busy = True
        self._time_hint.configure(text="正在写入配置并重新注册计划任务…")

        def work():
            ok, msg = scheduler.set_times(parts)
            # 保存成功后**立刻**拿一次权威状态（force=True 走慢路径，约 1s）。
            # 不能只依赖异步刷新：异步回调有 1s 延迟且会被并发刷新合并，
            # 界面会在「刚点完保存」的那一瞬间读到旧缓存，
            # 表现成「保存不了 / 时间没变」。这里在后台线程里同步取准，
            # 连同结果一起交回主线程渲染，保证点完就是新值。
            info = None
            if ok:
                try:
                    info = scheduler.task_info(force=True)
                except Exception:  # noqa: BLE001
                    info = None
            self.app.ui_call(lambda: self._times_done(ok, msg, info))

        threading.Thread(target=work, daemon=True).start()

    def _times_done(self, ok, msg, info=None):
        self.busy = False
        if ok:
            # 回读配置确认真落盘（防止"提示成功但其实没写入"的假成功）
            try:
                saved = C.load_config().get("schedule", {}).get("daily_times", [])
            except Exception:  # noqa: BLE001
                saved = []
            if saved:
                self._time_hint.configure(
                    text=f"已生效：每日 {'、'.join(saved)} 自动执行"
                         + ("（计划任务已重注册）" if scheduler.task_installed() else ""))
            else:
                self._time_hint.configure(text="已生效：" + (msg.splitlines()[-1]
                                                          if msg else ""))
            tail = (msg or "").splitlines()
            # 注册自检带出的告警（如登录触发器未绑定）要显式提示，别静默
            warns = [ln for ln in tail if "警告" in ln or "异常" in ln]
            if warns:
                self.app.toast("warn", warns[-1].split("）")[0][-70:] + "）")
            else:
                self.app.toast("success", "签到时间已更新，计划任务已重注册")
        else:
            self.app.toast("error", f"保存失败：{msg}")
            self._time_hint.configure(text="保存失败：" + str(msg)[:120])
        if info:
            # 直接拿权威数据渲染，不等异步刷新
            self._paint_task(info)
        else:
            self._refresh_task()
        self._refresh_chain()              # 执行链里的时间/窗口要跟着变

    # ---------------- 自动补跑（重试） ----------------

    def _num_field(self, master, label, value, suffix, width=6):
        """一行「标签 + 数字输入 + 单位」，返回 StringVar（由调用方统一校验保存）。

        单独拆出来是因为这些值必须**一起**提交（一次 set_retry 只重注册一次任务），
        不像 `_switch_row` 那样逐项即时落盘。
        """
        t = self.theme
        row = tk.Frame(master, bg=t.bg_soft)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, font=t.f_small, fg=t.fg_muted, bg=t.bg_soft,
                 anchor="w", width=22).pack(side="left")
        var = tk.StringVar(value=str(value))
        tk.Entry(row, textvariable=var, font=t.f_small, bg=t.bg_input, fg=t.fg,
                 relief="flat", highlightthickness=1,
                 highlightbackground=t.border, highlightcolor=t.accent,
                 width=width, justify="center").pack(side="left", ipady=3)
        tk.Label(row, text=suffix, font=t.f_small, fg=t.fg_dim, bg=t.bg_soft,
                 anchor="w").pack(side="left", padx=(6, 0))
        return var

    def _retry_summary(self):
        """把当前配置翻译成人话，让用户一眼看懂实际会发生什么。"""
        sched = C.load_config().get("schedule", {})
        if not bool(sched.get("retry_enabled", True)):
            return ("当前：已关闭 —— 只在设定的时间点各执行一次；"
                    "单次失败或断网错过都不会自动重试。")
        iv = int(sched.get("retry_interval_minutes", 30) or 30)
        win = int(sched.get("retry_window_minutes", 360) or 360)
        times = [t for t in (sched.get("daily_times") or ["09:00"]) if t]
        hours, mins = divmod(win, 60)
        span = (f"{hours} 小时" if not mins else
                (f"{hours} 小时 {mins} 分钟" if hours else f"{mins} 分钟"))
        # 门控是「任一账号未完成就放行」，所以某平台**持续**失败时，每次重试都会真的
        # 跑一轮（开浏览器）。间隔越短这个代价越明显 —— 明确写出来，
        # 别让用户以为调小只有好处。
        quiet = bool(C.load_config().get("options", {}).get("retry_toast_quiet", True))
        toast = ("弹窗：仅关键节点（第 1 次执行 / 全部签完 / 最后一轮仍失败）；"
                 if quiet else "弹窗：每轮都弹；")
        warn = ("　⚠ 间隔较短：若某平台持续失败，重试会相应变频繁"
                + ("（弹窗已按关键节点过滤，不会刷屏）。" if quiet
                   else "，弹窗也会跟着变频繁。")
                if iv <= 5 else "")
        return (f"当前：{'、'.join(times)} 起每 {iv} 分钟自动重试一次，持续 {span}"
                f"（最多约 {max(1, win // iv)} 次）；全部账号签完立即停止；"
                f"开机后也会补跑一次。{toast}{warn}")

    # ---------------- 自动化执行链（签到 × 补跑 × 弹窗）----------------

    def _on_retry_toast_change(self):
        """补跑弹窗策略改了 → 链条文案 + 补跑卡片里的说明文字都要跟着变。

        ⚠️ **不要**调 `_load_retry()`：那会把用户刚敲进输入框、还没点保存的
           间隔/时长覆盖掉（这是本项目踩过的坑）。
        """
        self._refresh_chain()
        try:
            self._retry_hint.configure(text=self._retry_summary())
        except tk.TclError:
            pass

    def _refresh_chain(self):
        """刷新「自动化执行链」的两行状态。

        纯读本地小文件（config.json），**不做任何探测、不起任何进程** ——
        所以放在渲染路径上是安全的（对比：`scheduler.task_info()` ≈1.1s，
        绝不能同步调）。
        """
        try:
            cfg = C.load_config()
            sched = cfg.get("schedule", {})
            opts = cfg.get("options", {})
            times = [t for t in (sched.get("daily_times") or []) if t] \
                or [sched.get("checkin_time") or "09:00"]
            retry_on = C.as_bool(sched.get("retry_enabled", True), True)
            try:
                iv = int(sched.get("retry_interval_minutes", 5) or 5)
            except (TypeError, ValueError):
                iv = 5
            try:
                win = int(sched.get("retry_window_minutes", 60) or 60)
            except (TypeError, ValueError):
                win = 60
            quiet = C.as_bool(opts.get("retry_toast_quiet", True), True)
            toast = C.as_bool(opts.get("toast_notify", True), True)

            steps = ["每天 %s 自动签到" % "、".join(times)]
            if retry_on:
                steps.append("失败后每 %d 分钟自动补签（持续 %s，最多 %d 次）"
                             % (iv, _minutes_to_hours_text(win) + " 小时",
                                max(1, win // iv)))
            else:
                steps.append("失败不补签")
            steps.append("全部签完立即停止")
            if not toast:
                steps.append("不弹窗（完全静默）")
            elif quiet:
                steps.append("仅在关键节点弹窗")
            else:
                steps.append("每轮都弹窗")
            self._chain_line.configure(text="  →  ".join(steps))
            self._chain_state.configure(text=self._chain_state_text(cfg))
        except Exception as e:  # noqa: BLE001
            try:
                self._chain_line.configure(text="执行链状态读取失败：%r" % (e,))
                self._chain_state.configure(text="")
            except tk.TclError:
                pass

    def _chain_state_text(self, cfg):
        """今天执行到第几次 / 补跑窗口到几点 / 下次执行什么时候。"""
        import datetime
        bits = []
        try:
            import checkin
        except Exception:  # noqa: BLE001
            return ""
        now = datetime.datetime.now()
        today = datetime.date.today().isoformat()
        try:
            st = checkin.load_retry_state()
        except Exception:  # noqa: BLE001
            st = {}
        if st.get("date") == today and int(st.get("attempts") or 0) > 0:
            bits.append("今天已自动执行 %d 次" % int(st["attempts"]))
        else:
            bits.append("今天还没有自动执行记录")
        try:
            end = checkin._retry_window_end(cfg)
        except Exception:  # noqa: BLE001
            end = None
        if end is not None:
            if now < end:
                bits.append("补跑窗口到 %s（还剩 %s）"
                            % (end.strftime("%H:%M"), _dur_short(end - now)))
            else:
                bits.append("今天的补跑窗口已于 %s 结束" % end.strftime("%H:%M"))
        sched = cfg.get("schedule", {})
        times = [t for t in (sched.get("daily_times") or []) if t] \
            or [sched.get("checkin_time") or "09:00"]
        nxt = None
        for t in times:
            try:
                h, m = [int(x) for x in str(t).strip().split(":")]
            except (TypeError, ValueError):
                continue
            cand = datetime.datetime.combine(datetime.date.today(),
                                             datetime.time(h, m))
            if cand > now:
                nxt = cand
                break
        if nxt is not None:
            bits.append("下次执行 %s（还有 %s）"
                        % (nxt.strftime("%H:%M"), _dur_short(nxt - now)))
        else:
            bits.append("今天的时点都已过（明天 %s 继续）" % times[0])
        return "　·　".join(bits)

    def _load_retry(self):
        sched = C.load_config().get("schedule", {})
        self._retry_on.set(bool(sched.get("retry_enabled", True)))
        self._retry_iv.set(str(sched.get("retry_interval_minutes", 30)))
        self._retry_win.set(
            _minutes_to_hours_text(sched.get("retry_window_minutes", 360)))
        self._retry_hint.configure(text=self._retry_summary())

    def _save_retry(self):
        if self.busy:
            self.app.toast("warn", "已有操作正在进行中")
            return
        try:
            iv = int(str(self._retry_iv.get()).strip())
        except ValueError:
            self.app.toast("warn", "重试间隔需为整数（分钟）")
            return
        if not (1 <= iv <= 360):
            self.app.toast("warn", "重试间隔需在 1~360 分钟之间")
            return
        try:
            hrs = float(str(self._retry_win.get()).strip())
        except ValueError:
            self.app.toast("warn", "补跑持续需为数字（小时）")
            return
        if not (0.5 <= hrs <= 24):
            self.app.toast("warn", "补跑持续需在 0.5~24 小时之间")
            return
        win = int(round(hrs * 60))
        # 窗口短于间隔 = 只可能触发一次，那就不叫"补跑"了 —— 当场拦下并说清原因
        if win < iv:
            self.app.toast("warn", f"补跑持续（{win} 分钟）不能小于重试间隔"
                                   f"（{iv} 分钟），否则只会重试一次")
            return
        enabled = bool(self._retry_on.get())
        self.busy = True
        self._retry_hint.configure(text="正在写入配置并重新注册计划任务…")

        def work():
            ok, msg = scheduler.set_retry(iv, win, enabled)
            info = None
            if ok:
                try:
                    info = scheduler.task_info(force=True)
                except Exception:  # noqa: BLE001
                    info = None
            self.app.ui_call(lambda: self._retry_done(ok, msg, info))

        threading.Thread(target=work, daemon=True).start()

    def _retry_done(self, ok, msg, info=None):
        self.busy = False
        if ok:
            # 回读配置确认真落盘（防"提示成功但其实没写进去"的假成功）
            self._load_retry()
            self._refresh_chain()          # 执行链文案要跟着变
            self.app.toast("success", "自动补跑已更新，计划任务已重注册")
        else:
            self._retry_hint.configure(text="保存失败：" + str(msg)[:160])
            self.app.toast("error", f"保存失败：{msg}")
        if info:
            # 直接拿权威数据渲染，不等异步刷新
            self._paint_task(info)
        else:
            self._refresh_task()

    # ---------------- 计划任务 ----------------

    def _refresh_task(self):
        # 读缓存渲染（快），同时后台刷新（准）；完成后 App 会回调 on_task_updated
        info = scheduler.cached_task_info()
        self.app.refresh_task()
        if info is None:
            # 缓存还没预热完：先给"读取中"，避免误报"未安装"
            t = self.theme
            self._task_status.configure(text="◌ 读取中…", fg=t.fg_dim)
            self._task_detail.configure(text="正在查询计划任务状态…")
            self._btn_uninstall.configure(state="disabled")
            return
        self._paint_task(info)

    @staticmethod
    def _result_text(code):
        """把计划任务结果码翻译成人话（这些码直接决定用户能不能看出自动化失效）。

        注意 0 是**成功**码，不能用 `code or ""` 那种写法 —— 整数 0 是 falsy，
        会被误判成"空值"，把成功显示成 '-'。
        """
        if code is None:
            return "-"
        s = str(code).strip()
        if s == "":
            return "-"
        try:
            n = int(s)
        except ValueError:
            return s
        m = {
            0: "成功",
            267009: "正在运行",
            267011: "从未运行过",       # 0x41303
            267010: "被禁用",
            267012: "已终止",
            267014: "已终止",
            2147942402: "找不到程序",   # 0x80070002
            2147943712: "缺少权限",
            3221225781: "缺少依赖",
        }
        if n in m:
            return f"{n}（{m[n]}）"
        return f"{n}（0x{n & 0xFFFFFFFF:X}）"

    def _task_health(self, info):
        """判断自动签到是否真的健康，返回 (是否健康, 说明文本)。

        这是本轮修复的重点：过去界面只显示「● 已安装」，
        任务其实一次都没跑过（LastTaskResult=0x41303）也照样绿着，
        用户根本无从判断自动化到底有没有生效。
        """
        last = str(info.get("lastRun") or "")
        raw_res = info.get("result")
        # 显式区分「没有结果码」与「结果码为 0（成功）」
        if raw_res is None or str(raw_res).strip() == "":
            code = None
        else:
            try:
                code = int(str(raw_res).strip())
            except ValueError:
                code = None
        # 「从没出现过有效时间」或「系统明确回 0x41303」都算从未运行
        never = ("1999" in last) or (not last.strip()) or (code == 267011) or (code is None)
        if never:
            return False, ("⚠ 从未成功运行过：任务已注册但还没有成功触发过一次。"
                           "若已过设定的时间点仍未执行，可点「立即执行一次」验证，"
                           "或点「安装 / 更新任务」重新注册。")
        if code != 0 and code != 267009:
            return False, (f"⚠ 上次运行失败（结果码 {self._result_text(code)}）。"
                           f"可点「立即执行一次」复现，并到「日志」页查看失败原因。")
        return True, ""

    def _paint_task(self, info):
        """按给定的任务状态渲染状态区（info 为权威数据或缓存数据）。"""
        t = self.theme
        if not info.get("installed"):
            self._task_status.configure(text="○ 未安装", fg=t.fg_dim)
            self._task_detail.configure(
                text="未安装时不会自动签到。点击「安装 / 更新任务」后，"
                     "将按上面的时间点自动执行。\n"
                     "安装后包含三类触发器：每日定时 + 开机自启（延迟补跑）"
                     "+ 网络恢复补跑（错过联网后自动补签）。")
            self._btn_uninstall.configure(state="disabled")
            return

        healthy, health = self._task_health(info)
        # 计划任务指向的可能是「另一个副本」（比如曾经用过的便携版 exe）。
        # 那种情况下任务照跑，但读写的是另一份 config/logs ——
        # 用户在界面里改的设置永远不生效，是极难自查的故障，必须显式告警。
        mismatched, task_exe, my_prog = (False, "", "")
        try:
            mismatched, task_exe, my_prog = scheduler.task_target_mismatch(info)
        except Exception:  # noqa: BLE001
            pass
        if mismatched:
            healthy = False
            # 先给结论，再给证据，最后给动作 —— 长路径单独成行，配合
            # wraplength 折行后仍然读得清。
            health = ("⚠ 任务指向的不是当前程序，界面里的设置写不进任务实际用的那份配置。\n"
                      f"　　任务 → {task_exe}\n"
                      f"　　当前 → {my_prog or '（未识别）'}\n"
                      "点「安装 / 更新任务」即可重新指向当前程序。")
        self._task_status.configure(text="● 已安装" if healthy else "▲ 需检查",
                                    fg=t.green_fg if healthy else t.amber_fg)
        times = info.get("times") or ("、".join(
            C.load_config().get("schedule", {}).get("daily_times", [])) or "-")
        # 触发器类型中文化，特别标出「网络恢复自动补跑」
        trig = str(info.get("triggers") or "")
        tr_map = []
        if "Daily" in trig:
            tr_map.append("每日定时")
        if "Logon" in trig:
            tr_map.append("开机自启")
        if "Event" in trig:
            tr_map.append("网络恢复补跑")
        tr_txt = " + ".join(tr_map) or "—"
        txt = (f"状态 {info.get('state') or '-'} · 每日 {times}\n"
               f"触发器 {tr_txt}\n"
               f"上次运行 {info.get('lastRun') or '-'}"
               f"（{self._result_text(info.get('result'))}）\n"
               f"下次运行 {info.get('nextRun') or '-'}")
        if health:
            txt += "\n" + health
        self._task_detail.configure(text=txt)
        self._btn_uninstall.configure(state="normal")

    def on_task_updated(self):
        """任务状态后台刷新完成后重画状态区。

        直接用刷新回来的权威数据渲染，避免再去读一次缓存
        （那会在缓存被 invalidate 的窗口期读到 None，白闪一下「读取中」）。
        """
        try:
            info = scheduler.cached_task_info()
            if info is None:
                self._refresh_task()
            else:
                self._paint_task(info)
        except Exception:  # noqa: BLE001
            pass

    def _install_task(self):
        self._task_action(lambda: scheduler.install(C.load_config()),
                          "计划任务已安装/更新")

    def _uninstall_task(self):
        self._task_action(scheduler.uninstall, "计划任务已移除")

    def _run_task(self):
        self._task_action(scheduler.run_task_now, "已触发计划任务执行")

    def _task_action(self, fn, done_msg):
        if self.busy:
            self.app.toast("warn", "已有操作正在进行中")
            return
        self.busy = True
        self._btn_install.configure(state="disabled")

        def work():
            try:
                ok, msg = fn()
            except Exception as e:  # noqa: BLE001
                ok, msg = False, repr(e)
            # 安装/卸载/立即执行后同样强制取一次权威状态，
            # 避免界面停在旧缓存上（「点了没反应」的观感就来自这里）
            info = None
            try:
                info = scheduler.task_info(force=True)
            except Exception:  # noqa: BLE001
                info = None
            self.app.ui_call(lambda: self._task_done(ok, msg, done_msg, info))

        threading.Thread(target=work, daemon=True).start()

    def _task_done(self, ok, msg, done_msg, info=None):
        self.busy = False
        self._btn_install.configure(state="normal")
        self.app.toast("success" if ok else "error", done_msg if ok else str(msg))
        if info is not None:
            self._paint_task(info)
        else:
            self._refresh_task()
        self.app._refresh_topbar()

    def on_hide(self):
        pass
