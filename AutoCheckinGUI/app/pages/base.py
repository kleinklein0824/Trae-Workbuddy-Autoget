#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""页面基类：统一页面标题区、可滚动内容区与背景色管理。"""
import tkinter as tk

from ..ui.widgets import ScrollFrame, button


class BasePage(tk.Frame):
    """所有页面的父类。

    子类在 self.build() 里往 self.body 中填内容即可，标题与滚动容器由基类提供。
    """

    title = ""
    desc = ""
    busy = False

    def __init__(self, master, theme, app, module="traework"):
        super().__init__(master, bg=theme.bg)
        self.theme = theme
        self.app = app
        self.module = module
        self._actions = []

        head = tk.Frame(self, bg=theme.bg)
        head.pack(fill="x", padx=20, pady=(16, 8))
        left = tk.Frame(head, bg=theme.bg)
        left.pack(side="left", fill="x", expand=True)
        self._title_lbl = tk.Label(left, text=self.title, font=theme.f_title,
                                   fg=theme.fg, bg=theme.bg, anchor="w")
        self._title_lbl.pack(anchor="w")
        self._desc_lbl = tk.Label(left, text=self.desc, font=theme.f_small,
                                  fg=theme.fg_dim, bg=theme.bg, anchor="w")
        self._desc_lbl.pack(anchor="w")
        self.action_box = tk.Frame(head, bg=theme.bg)
        self.action_box.pack(side="right")

        self.scroll = ScrollFrame(self, theme)
        self.scroll.pack(fill="both", expand=True, padx=(20, 12), pady=(0, 14))
        self.body = self.scroll.inner
        self.body.configure(bg=theme.bg)

        self.build()

    def build(self):
        pass

    def add_action(self, text, command, variant="outline", icon=""):
        b = button(self.action_box, self.theme, text, command=command,
                   variant=variant, icon=icon, small=True)
        b.pack(side="right", padx=(6, 0))
        self._actions.append(b)
        return b

    def set_title(self, title, desc=None):
        self._title_lbl.configure(text=title)
        if desc is not None:
            self._desc_lbl.configure(text=desc)

    def on_show(self):
        pass

    def on_hide(self):
        pass

    def on_geometry(self):
        """窗口尺寸变化（最大化/还原/拖拽缩放）后被调用。

        无边框窗口走 Win32 的 ShowWindow 最大化时，Tk 收到的 Configure
        事件时序不可靠，滚动容器的 canvas 宽度与 scrollregion 可能仍按
        旧尺寸计算，表现为「最大化后有些板块看不见」。
        这里强制重算一次滚动区域与所有卡片的尺寸。
        """
        try:
            self.scroll.resync()
        except Exception:  # noqa: BLE001
            pass

    def refresh(self):
        """主题切换后由外部调用；默认整页重建交给 App.apply_theme。"""
        pass
