#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量 Toast 通知：右下角堆叠淡出，不打断操作。"""
import tkinter as tk


class Toaster:
    """在窗口右下角堆叠显示短提示。type: success / warn / error / info。"""

    def __init__(self, root, theme):
        self.root = root
        self.theme = theme
        self._items = []

    def push(self, kind, text, ms=3200):
        t = self.theme
        tone = {"success": "green", "warn": "amber",
                "error": "red", "info": "blue"}.get(kind, "slate")
        fg, bg = t.tone(tone)
        icon = {"success": "✓", "warn": "!", "error": "✕", "info": "i"}.get(kind, "·")

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.97)
        except Exception:  # noqa: BLE001
            pass
        frame = tk.Frame(win, bg=t.bg_elev, highlightthickness=1,
                         highlightbackground=fg)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=icon, font=(t.font_family, 11, "bold"), fg=fg,
                 bg=t.bg_elev, width=3).pack(side="left", padx=(8, 0), pady=10)
        tk.Label(frame, text=text, font=t.f_small, fg=t.fg, bg=t.bg_elev,
                 wraplength=300, justify="left", anchor="w").pack(
            side="left", padx=(0, 14), pady=10, fill="x", expand=True)

        self._items.append(win)
        self._reposition()
        win.after(ms, lambda: self._dismiss(win))

    def _reposition(self):
        self.root.update_idletasks()
        rx = self.root.winfo_rootx() + self.root.winfo_width() - 24
        ry = self.root.winfo_rooty() + self.root.winfo_height() - 24
        for win in self._items:
            try:
                win.update_idletasks()
                w, h = win.winfo_width(), win.winfo_height()
                ry -= h + 8
                win.geometry(f"+{max(0, rx - w)}+{max(0, ry)}")
            except Exception:  # noqa: BLE001
                pass

    def _dismiss(self, win):
        self._items = [w for w in self._items if w is not win]
        try:
            for i in range(6):
                win.attributes("-alpha", 0.97 * (1 - i / 6))
                win.update()
        except Exception:  # noqa: BLE001
            pass
        try:
            win.destroy()
        except Exception:  # noqa: BLE001
            pass
        self._reposition()

    def retheme(self, theme):
        self.theme = theme
