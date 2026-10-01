#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新手引导：按步骤完成初始化的清单卡片。

对齐参考项目的 SetupGuide 信息架构，但步骤内容适配本项目：
  1. 安装依赖（requests / playwright）
  2. 登录至少一个账号（TraeWork / TraeCN / WorkBuddy 任一）
  3. 完成一次签到，验证链路
  4. 设定每日签到时间
  5. 安装计划任务（实现无人值守）

每一步的「完成」判定都来自实时状态，可点击直接跳到对应页面。
"""
import os
import tkinter as tk

from .. import config as C
from ..ui import widgets as W

# 依赖探测结果缓存。
# `__import__("playwright")` 会把整个包（含 driver 相关模块）拉进来，
# 单次约 400ms —— 而概览页每次渲染都要判断一次，直接导致切页变慢。
# 依赖装没装在一个进程生命周期内不会变，缓存一次即可。
_DEPS_CACHE = {}


def _deps_ok(force=False):
    """依赖是否齐全（结果缓存，force=True 时重新探测）。"""
    if not force and "val" in _DEPS_CACHE:
        return _DEPS_CACHE["val"]
    miss = []
    for mod in ("requests", "playwright"):
        try:
            __import__(mod)
        except ImportError:
            miss.append(mod)
    val = (not miss, miss)
    _DEPS_CACHE["val"] = val
    return val


def _has_any_account():
    data = C.peek_accounts()
    if not data:
        return None          # 未知（缓存未就绪）
    return any(data.get(k) for k in ("traework", "traecn", "workbuddy"))


def _checked_today():
    state = C.load_state()
    today = C.today_str()
    return any(v == today for v in state.values())


def _times_set(cfg):
    times = cfg.get("schedule", {}).get("daily_times") or []
    return bool([t for t in times if isinstance(t, str) and ":" in t])


def _task_installed():
    """计划任务是否已安装。

    必须走缓存版本：`task_info()` 会同步起 PowerShell（约 1s），
    而引导组件在每次页面渲染时都要读它，直接调会明显卡顿。
    App 启动时已异步预热，没有缓存则先当作未安装（下一帧会纠正）。
    """
    try:
        from ..services import scheduler
        info = scheduler.cached_task_info()
        if info is None:
            # 没有缓存 → 触发一次异步刷新，并先返回"未知"
            scheduler.refresh_task_info_async()
            return None
        return bool(info.get("installed"))
    except Exception:  # noqa: BLE001
        return False


def all_steps_done():
    """快速判断 5 个初始化步骤是否都已完成（不构造任何控件）。

    概览页用它决定要不要建引导卡片：全完成时直接跳过，
    省掉一整套控件的创建开销。任一状态未知（如任务缓存未预热）
    都返回 False，宁可多显示一次引导，也不要漏掉未完成的步骤。
    """
    try:
        deps_ok, _miss = _deps_ok()
        if not deps_ok:
            return False
        has_acc = _has_any_account()
        if has_acc is not True:
            return False
        if not _checked_today():
            return False
        cfg = C.load_config()
        if not _times_set(cfg):
            return False
        if _task_installed() is not True:
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


class SetupGuide(tk.Frame):
    """配置导航卡片：步骤清单 + 完成度徽标。"""

    def __init__(self, master, theme, app):
        super().__init__(master, bg=theme.bg)
        self.theme = theme
        self.app = app
        self._build()

    # ---------------- 构建 ----------------

    def _build(self):
        t = self.theme
        for w in self.winfo_children():
            w.destroy()

        card = W.Card(self, t, padding=0)
        card.pack(fill="x")
        b = card.body

        # 头部
        head = tk.Frame(b, bg=t.bg_soft)
        head.pack(fill="x", padx=14, pady=(12, 10))
        left = tk.Frame(head, bg=t.bg_soft)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="配置导航", font=t.f_card_title, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(anchor="w")
        tk.Label(left, text="按步骤完成初始化，已完成的步骤无需重复处理",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft,
                 anchor="w").pack(anchor="w", pady=(2, 0))

        steps = self._steps()
        done = sum(1 for s in steps if s["done"] is True)
        total = len(steps)
        self._badge = W.Badge(head, t, f"{done}/{total} 已完成",
                              tone="green" if done == total else "amber")
        self._badge.pack(side="right")

        tk.Frame(b, bg=t.border, height=1).pack(fill="x")

        # 步骤列表
        for i, s in enumerate(steps):
            self._step_row(b, i, s, total)

    def _steps(self):
        cfg = C.load_config()
        deps_ok, _miss = _deps_ok()
        has_acc = _has_any_account()
        return [
            {
                "key": "deps",
                "title": "安装运行依赖",
                "desc": "requests 与 playwright 是签到模块的必需依赖。",
                "done": deps_ok,
                "action": "重新检测",
                "run": self._rebuild,
            },
            {
                "key": "account",
                "title": "登录至少一个账号",
                "desc": "TraeWork / TraeCN / WorkBuddy 任一即可开始。",
                "done": bool(has_acc) if has_acc is not None else False,
                "unknown": has_acc is None,
                "action": "去账号管理",
                "run": lambda: self.app.goto(self.app.module, "accounts"),
            },
            {
                "key": "checkin",
                "title": "完成首次签到",
                "desc": "验证「账号 → 接口 → 记录」整条链路是否跑通。",
                "done": _checked_today(),
                "action": "去签到",
                "run": lambda: self.app.goto(self.app.module, "checkin"),
            },
            {
                "key": "times",
                "title": "设定每日签到时间",
                "desc": "默认 09:00 / 21:00，可按需要调整（最多 4 个）。",
                "done": _times_set(cfg),
                "action": "去设置",
                "run": lambda: self.app.goto(self.app.module, "settings"),
            },
            {
                "key": "task",
                "title": "安装计划任务",
                "desc": "安装后无需开机守着：定时执行 + 开机补跑 + 网络恢复补跑。",
                "done": _task_installed(),
                "action": "去设置",
                "run": lambda: self.app.goto(self.app.module, "settings"),
            },
        ]

    def _step_row(self, master, idx, step, total):
        t = self.theme
        done = step["done"] is True
        unknown = step.get("unknown")

        row = tk.Frame(master, bg=t.bg_soft)
        row.pack(fill="x")
        inner = tk.Frame(row, bg=t.bg_soft)
        inner.pack(fill="x", padx=14, pady=9)

        # 序号 / 完成勾
        if done:
            mark, mk_fg, mk_bg = "✓", t.green_fg, t.green_bg
        elif unknown:
            mark, mk_fg, mk_bg = "?", t.fg_dim, t.bg_muted
        else:
            mark, mk_fg, mk_bg = str(idx + 1), t.fg_muted, t.bg_muted
        circle = tk.Canvas(inner, width=22, height=22, bg=t.bg_soft,
                           highlightthickness=0, bd=0)
        circle.pack(side="left")
        circle.create_oval(0, 0, 21, 21, fill=mk_bg, outline="")
        circle.create_text(11, 11, text=mark, fill=mk_fg,
                           font=(t.font_family, 9, "bold"))

        txt = tk.Frame(inner, bg=t.bg_soft)
        txt.pack(side="left", padx=(10, 0), fill="x", expand=True)
        title = tk.Label(txt, text=step["title"], font=t.f_small,
                         fg=t.fg if not done else t.fg_muted,
                         bg=t.bg_soft, anchor="w")
        title.pack(anchor="w")
        tk.Label(txt, text=step["desc"], font=t.f_tiny, fg=t.fg_dim,
                 bg=t.bg_soft, anchor="w", justify="left",
                 wraplength=520).pack(anchor="w")

        if done:
            W.Badge(inner, t, "已完成", tone="green").pack(side="right")
        else:
            W.button(inner, t, step["action"], command=step["run"],
                     variant="outline", small=True).pack(side="right")

        if idx < total - 1:
            tk.Frame(master, bg=t.border, height=1).pack(fill="x", padx=14)

    # ---------------- 刷新 ----------------

    def _rebuild(self):
        _deps_ok(force=True)        # 用户点了「重新检测」→ 绕过缓存重新探一遍
        self._build()
        self.app.toast("info", "已重新检测环境")

    def set_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme.bg)
        self._build()
