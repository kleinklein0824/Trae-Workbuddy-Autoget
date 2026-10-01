#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统日志页：查看签到日志文件与签到流水，支持平台/级别筛选与自动刷新。"""
import os
import time
import tkinter as tk

from .. import config as C
from ..ui import widgets as W
from .base import BasePage

PLATFORM_FILTERS = [("全部", None), ("TraeWork", "traework"),
                    ("TraeCN", "traecn"), ("WorkBuddy", "workbuddy")]


class LogsPage(BasePage):
    title = "系统日志"

    def build(self):
        t = self.theme
        self.set_title("系统日志", "签到流水与运行日志（与命令行共用同一批文件）")
        self._platform = None
        self._auto = tk.BooleanVar(value=False)
        self._files = []
        # ★ 「内容没变就别重建」（2026-09-22）：本页每次 reload 要重建约 30 行
        #   ×5 个控件，再把日志文件尾部 600 行灌进 Text —— 实测 0.7~0.9 s。
        #   进页面时内容通常一点没变，白重建。用 (mtime_ns, size, 筛选, 主题)
        #   当签名，签名一致直接跳过。
        self._hist_sig = None
        self._file_sig = None

        # ---- 工具栏 ----
        bar = W.Card(self.body, t, padding=10)
        bar.pack(fill="x")
        left = tk.Frame(bar.body, bg=t.bg_soft)
        left.pack(side="left")
        tk.Label(left, text="平台", font=t.f_small, fg=t.fg_muted,
                 bg=t.bg_soft).pack(side="left", padx=(0, 6))
        self._pf_btns = {}
        for label, value in PLATFORM_FILTERS:
            b = W.button(left, t, label, command=lambda v=value: self._set_platform(v),
                         variant="primary" if value is None else "outline", small=True)
            b.pack(side="left", padx=2)
            self._pf_btns[value] = b

        right = tk.Frame(bar.body, bg=t.bg_soft)
        right.pack(side="right")
        self._chk_auto = tk.Checkbutton(
            right, text="每 5 秒自动刷新", variable=self._auto,
            command=self._toggle_auto, font=t.f_small, fg=t.fg_muted,
            bg=t.bg_soft, activebackground=t.bg_soft, selectcolor=t.bg_soft,
            highlightthickness=0, bd=0, cursor="hand2")
        self._chk_auto.pack(side="left", padx=(0, 8))
        W.button(right, t, "刷新", command=self.reload, variant="outline",
                 small=True, icon="⟳").pack(side="left")
        W.button(right, t, "打开 logs 目录", command=self._open_dir,
                 variant="ghost", small=True).pack(side="left", padx=(6, 0))

        # ---- 流水 ----
        W.section_title(self.body, t, "签到流水",
                        "logs/history.jsonl").pack(fill="x", pady=(16, 6))
        self.hist_box = tk.Frame(self.body, bg=t.bg)
        self.hist_box.pack(fill="x")

        # ---- 日志文件 ----
        W.section_title(self.body, t, "运行日志",
                        "logs/checkin_YYYY-MM-DD.log").pack(fill="x", pady=(18, 6))
        sel = tk.Frame(self.body, bg=t.bg)
        sel.pack(fill="x")
        self._file_var = tk.StringVar()
        self._file_menu = tk.OptionMenu(sel, self._file_var, "（无日志文件）",
                                        command=lambda _v: self._load_file())
        self._file_menu.configure(font=t.f_small, bg=t.bg_soft, fg=t.fg,
                                  activebackground=t.bg_hover, relief="flat",
                                  highlightthickness=1, highlightbackground=t.border,
                                  bd=0, width=34, anchor="w")
        self._file_menu["menu"].configure(font=t.f_small, bg=t.bg_soft, fg=t.fg)
        self._file_menu.pack(side="left")
        self._auto_scroll = tk.BooleanVar(value=True)
        tk.Checkbutton(sel, text="自动滚到底部", variable=self._auto_scroll,
                       font=t.f_small, fg=t.fg_muted, bg=t.bg,
                       activebackground=t.bg, selectcolor=t.bg_soft,
                       highlightthickness=0, bd=0, cursor="hand2").pack(
            side="left", padx=(10, 0))

        log_card = W.Card(self.body, t, padding=8)
        log_card.pack(fill="both", expand=True, pady=(6, 0))
        # width=1 覆盖 tk.Text 默认 80 字符宽，避免撑宽卡片
        self.text = tk.Text(log_card.body, width=1, height=16, font=t.f_mono,
                            bg=t.bg_soft, fg=t.fg_muted, relief="flat",
                            wrap="none", insertbackground=t.fg)
        vs = tk.Scrollbar(log_card.body, orient="vertical", command=self.text.yview,
                          relief="flat", bd=0, width=10, troughcolor=t.bg_soft,
                          bg=t.bg_muted)
        hs = tk.Scrollbar(log_card.body, orient="horizontal", command=self.text.xview,
                          relief="flat", bd=0, width=10, troughcolor=t.bg_soft,
                          bg=t.bg_muted)
        self.text.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.configure(state="disabled")

        self._tick_id = None
        self.on_show()

    # ---------------- 平台筛选 ----------------

    def _set_platform(self, value):
        self._platform = value
        t = self.theme
        for key, btn in self._pf_btns.items():
            btn.configure(bg=t.accent if key == value else t.bg_soft,
                          fg=t.accent_fg if key == value else t.fg_muted)
        self._render_history()

    def _toggle_auto(self):
        if self._auto.get():
            self._schedule_tick()
        elif self._tick_id:
            try:
                self.after_cancel(self._tick_id)
            except Exception:  # noqa: BLE001
                pass
            self._tick_id = None

    def _schedule_tick(self):
        if self._tick_id:
            try:
                self.after_cancel(self._tick_id)
            except Exception:  # noqa: BLE001
                pass
        self._tick_id = self.after(5000, self._auto_tick)

    def _auto_tick(self):
        self._tick_id = None
        if not self._auto.get():
            return
        self._render_history()
        self._load_file(preserve=True)
        self._schedule_tick()

    # ---------------- 渲染 ----------------

    def on_show(self):
        self.reload()
        if self._auto.get():
            self._schedule_tick()

    def on_hide(self):
        if self._tick_id:
            try:
                self.after_cancel(self._tick_id)
            except Exception:  # noqa: BLE001
                pass
            self._tick_id = None

    def reload(self):
        self._render_history()
        self._refresh_files()
        self._load_file()

    def _render_history(self, force=False):
        # ★ 内容签名没变 → 直接跳过重建（省下约 150 个控件的创建 + Tk 布局）
        sig = (self._platform, bool(self.theme.dark))
        try:
            st = os.stat(C.HISTORY_PATH)
            sig += (st.st_mtime_ns, st.st_size)
        except OSError:
            sig += (None, None)
        if not force and sig == self._hist_sig:
            return
        self._hist_sig = sig
        for w in self.hist_box.winfo_children():
            w.destroy()
        t = self.theme
        rows = C.load_history()
        if self._platform:
            rows = [r for r in rows if r.get("platform") == self._platform]
        rows = rows[-30:]
        rows.reverse()
        if not rows:
            W.EmptyState(self.hist_box, t, "☰", "暂无签到流水",
                         "执行签到后，记录会写入 logs/history.jsonl。").pack(fill="x")
            return
        card = W.Card(self.hist_box, t, padding=0)
        card.pack(fill="x")
        box = card.body
        for i, r in enumerate(rows):
            if i:
                W.hline(box, t).pack(fill="x")
            row = tk.Frame(box, bg=t.bg_soft)
            row.pack(fill="x", padx=14, pady=7)
            tk.Label(row, text=str(r.get("ts") or "").replace("T", " ")[:19],
                     font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, width=19,
                     anchor="w").pack(side="left")
            plat = C.PLATFORM_CN.get(r.get("platform"), str(r.get("platform")))
            tk.Label(row, text=plat, font=t.f_tiny, fg=t.fg_muted, bg=t.bg_soft,
                     width=10, anchor="w").pack(side="left")
            tk.Label(row, text=str(r.get("account") or "-"), font=t.f_small,
                     fg=t.fg, bg=t.bg_soft, width=14, anchor="w").pack(side="left")
            st = r.get("status")
            W.Badge(row, t, C.STATUS_CN.get(st, str(st)),
                    C.STATUS_TONE.get(st, "slate")).pack(side="right")
            # ★ 必须套 auto_wrap：tk.Label 默认 wraplength=0 = **不换行**，
            #   详情里一句长中文（例如「未能确认签到结果：签到明细已渲染（9 条）
            #   但没有今日签到条目」）会按整行渲染、冲出卡片右边缘被静默裁掉。
            #   `fill="x", expand=True` 只给了空间，并不会让文字换行。
            det = W.auto_wrap(tk.Label(row, text=str(r.get("detail") or "")[:90],
                                       font=t.f_tiny, fg=t.fg_muted, bg=t.bg_soft,
                                       anchor="w", justify="left"))
            det.pack(side="left", padx=(10, 0), fill="x", expand=True)

    def _refresh_files(self):
        files = []
        try:
            if os.path.isdir(C.LOG_DIR):
                for name in os.listdir(C.LOG_DIR):
                    if name.startswith("checkin_") and name.endswith(".log"):
                        files.append(name)
        except OSError:
            pass
        files.sort(reverse=True)
        self._files = files
        menu = self._file_menu["menu"]
        menu.delete(0, "end")
        if not files:
            menu.add_command(label="（无日志文件）",
                             command=lambda: self._file_var.set("（无日志文件）"))
            self._file_var.set("（无日志文件）")
            return
        for name in files:
            menu.add_command(label=name, command=lambda n=name: self._pick_file(n))
        current = self._file_var.get()
        if current not in files:
            self._file_var.set(files[0])

    def _pick_file(self, name):
        self._file_var.set(name)
        self._load_file()

    def _load_file(self, preserve=False, force=False):
        name = self._file_var.get()
        t = self.theme
        if not name or name.startswith("（"):
            self._set_text("（暂无日志文件，执行一次签到后会生成）")
            return
        path = os.path.join(C.LOG_DIR, name)
        try:
            st = os.stat(path)
        except OSError as e:
            self._set_text(f"读取失败：{e!r}")
            return
        # ★ 文件没变过（mtime+size 一致）→ Text 里已经是这份内容，别重灌
        sig = (path, st.st_mtime_ns, st.st_size)
        if not force and sig == self._file_sig:
            return
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError as e:
            self._set_text(f"读取失败：{e!r}")
            return
        self._file_sig = sig
        lines = content.splitlines()
        tail = "\n".join(lines[-600:])
        if preserve and self.text.get("1.0", "end").strip().endswith(
                tail.strip()[-200:] if len(tail.strip()) > 200 else tail.strip()):
            return
        self._set_text(tail or "（文件为空）")

    def _set_text(self, content):
        # 尽量保持滚动位置稳定，避免自动刷新时反复跳动
        at_bottom = self._auto_scroll.get()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.configure(state="disabled")
        if at_bottom:
            self.text.see("end")

    def _open_dir(self):
        try:
            os.startfile(C.LOG_DIR)  # noqa: S606
        except Exception as e:  # noqa: BLE001
            self.app.toast("error", f"打开目录失败：{e!r}")
