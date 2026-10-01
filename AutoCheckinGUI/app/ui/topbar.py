#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""顶栏状态条：展示当前模块的关键状态徽标 + 主题切换 + 快捷操作。

对齐参考项目 TopBar 的思路：徽标随模块切换（TraeWork/TraeCN 展示令牌与
计划任务状态，WorkBuddy 展示账号与今日签到进度），右侧固定放主题切换与
主操作按钮。
"""
import tkinter as tk

from .widgets import Badge, button


class TopBar(tk.Frame):
    def __init__(self, master, theme, app):
        super().__init__(master, bg=theme.bg_topbar, height=48)
        self.pack_propagate(False)
        self.theme = theme
        self.app = app

        self.left = tk.Frame(self, bg=theme.bg_topbar)
        self.left.pack(side="left", fill="y", padx=(14, 0))
        self.right = tk.Frame(self, bg=theme.bg_topbar)
        self.right.pack(side="right", fill="y", padx=(0, 14))

        self.badges = {}
        self._btn_theme = button(self.right, theme, "", command=app.toggle_theme,
                                 variant="ghost", small=True)
        self._btn_theme.pack(side="right", padx=(6, 0))
        # 文案里写明范围：2.4.8 及以前它其实只签**当前模块**的账号，
        # 名字却叫「一键签到」，用户会以为它覆盖了所有平台。
        # 2.4.9 起真的覆盖三个平台的全部账号，所以把范围写进按钮里。
        self._btn_main = button(self.right, theme, "一键签到（全部平台）",
                                command=app.quick_checkin, variant="primary",
                                icon="▶", small=True)
        self._btn_main.pack(side="right")

    def set_badges(self, specs):
        """specs: [(key, text, tone, icon)]，按顺序重建徽标。

        ★ 内容没变就不重建（2026-09-22）：`App._refresh_topbar()` 在**每次切页**
          之后都会调这里，而它每次都 destroy + 新建 4~5 个 Badge（每个 Badge
          内部还有若干控件 + 一次字体测量）。签名一致直接返回 —— 切页时顶栏
          几乎总是一模一样。
        """
        try:
            sig = (bool(self.theme.dark), tuple(tuple(s) for s in specs))
        except Exception:  # noqa: BLE001
            sig = None
        if sig is not None and sig == getattr(self, "_badge_sig", None):
            return
        self._badge_sig = sig
        for w in self.left.winfo_children():
            w.destroy()
        self.badges = {}
        for key, text, tone, icon in specs:
            b = Badge(self.left, self.theme, text, tone, icon)
            b.pack(side="left", padx=(0, 6), pady=12)
            self.badges[key] = b

    def refresh(self, theme):
        self.theme = theme
        self.configure(bg=theme.bg_topbar)
        for w in (self.left, self.right):
            w.configure(bg=theme.bg_topbar)
        self._btn_theme.configure(text="☀" if theme.dark else "☾",
                                  fg=theme.fg_muted, bg=theme.bg_topbar,
                                  activebackground=theme.bg_hover,
                                  activeforeground=theme.fg)
        for b in self.badges.values():
            b.refresh()
