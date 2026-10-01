#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""左侧边栏：顶部分段控制器切换模块（TraeWork / TraeCN / WorkBuddy），
中部模块导航，底部全局导航（日志 / 设置 / 关于）。

分段控制器与导航项的视觉语言对齐参考项目：选中项为实心深色块 + 反色文字。
"""
import tkinter as tk

from ..theme import Theme
from .widgets import alive as _alive
from .widgets import round_rect

# 侧栏宽度：三段控制器需要放下 TraeWork / TraeCN / WorkBuddy
SIDEBAR_W = 232

MODULES = [
    ("traework", "TraeWork"),
    ("traecn", "TraeCN"),
    ("workbuddy", "WorkBuddy"),
]

# 每个模块的导航项：(view_key, 显示名, 图标字符)
NAV = {
    "traework": [
        ("dashboard", "概览", "▤"),
        ("checkin", "一键签到", "▶"),
        ("accounts", "账号管理", "◍"),
        ("credits", "积分看板", "✦"),
    ],
    "traecn": [
        ("dashboard", "概览", "▤"),
        ("checkin", "一键签到", "▶"),
        ("accounts", "账号管理", "◍"),
        ("credits", "积分看板", "✦"),
    ],
    "workbuddy": [
        ("dashboard", "概览", "▤"),
        ("checkin", "一键签到", "▶"),
        ("accounts", "账号管理", "◍"),
        ("credits", "积分看板", "✦"),
    ],
}

GLOBAL_NAV = [
    ("api-service", "API 服务", "⇄"),
    ("logs", "系统日志", "☰"),
    ("settings", "系统设置", "⚙"),
]


class Segmented(tk.Canvas):
    """胶囊分段控制器：外层圆角轨道 + 滑动实心药丸。

    绘制完全由显式调用驱动（init / set_active / refresh / 尺寸变化），
    并且每次绘制前都校验控件存活，避免页面重建期间 Tk 抛
    TclError: invalid command name。

    尺寸一律存 `_ch` / `_cw` —— `_w` / `_h` 是 Tkinter 内部属性
    （`_w` 保存控件路径名），占用会导致 `must be name of window` 崩溃。
    """

    def __init__(self, master, theme, items, command=None, active=0, height=32):
        self.items = items
        self.theme = theme
        self.command = command
        self.active = active
        self._ch = height
        self._pad = 3
        self._seg = 1.0
        super().__init__(master, height=height, bg=theme.bg_sidebar,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Button-1>", self._click)
        self.bind("<Configure>", self._on_configure)
        # 首次绘制延后到几何布局完成之后
        self.after_idle(self._draw)

    def _on_configure(self, event):
        # 只在宽度实际变化时重绘，减少无谓刷新
        if abs(getattr(self, "_last_w", -1) - event.width) > 0:
            self._last_w = event.width
            self._draw()

    def _bounds(self):
        w = self.winfo_width()
        n = max(1, len(self.items))
        pad = self._pad
        seg = max(1.0, (w - pad * 2) / n)
        return w, pad, seg

    def _draw(self):
        if not _alive(self):
            return
        t = self.theme
        try:
            self.delete("all")
            w = self.winfo_width()
            if w < 10:
                return
            w, pad, seg = self._bounds()
            self._cw, self._pad, self._seg = w, pad, seg
            # 轨道
            round_rect(self, 0, 0, w, self._ch, self._ch / 2,
                       fill=t.segment_bg, outline="")
            # 滑块
            x0 = pad + seg * self.active
            round_rect(self, x0, pad, x0 + seg, self._ch - pad,
                       (self._ch - pad * 2) / 2, fill=t.segment_pill, outline="")
            # 文字
            for i, item in enumerate(self.items):
                label = item[1] if isinstance(item, (tuple, list)) else str(item)
                cx = pad + seg * (i + 0.5)
                fg = t.segment_active_fg if i == self.active else t.fg_muted
                self.create_text(cx, self._ch / 2, text=label, fill=fg,
                                 font=(t.font_family, 9, "bold"))
        except tk.TclError:
            # 控件已在重建过程中销毁，忽略这次绘制
            pass

    def _click(self, e):
        if not _alive(self):
            return
        w, pad, seg = self._bounds()
        idx = int((e.x - pad) // max(0.001, seg))
        idx = max(0, min(len(self.items) - 1, idx))
        if idx != self.active:
            self.active = idx
            self._draw()
            if self.command:
                key = self.items[idx]
                self.command(key[0] if isinstance(key, (tuple, list)) else key)

    def set_active(self, key):
        for i, item in enumerate(self.items):
            k = item[0] if isinstance(item, (tuple, list)) else item
            if k == key:
                self.active = i
                self._draw()
                return

    def refresh(self, theme):
        if not _alive(self):
            return
        self.theme = theme
        try:
            self.configure(bg=theme.bg_sidebar)
        except tk.TclError:
            return
        self._draw()


class NavItem(tk.Frame):
    """单条导航项：悬停高亮 + 选中实心块。

    性能：每个 NavItem 有 Frame + Label 两个控件，悬停要同时改两处底色。
    点击时不要重建这些控件（`_build_nav` 只在切换模块时才调），
    切换视图只走 `set_active`，这样点击是零分配的。
    """

    def __init__(self, master, theme, text, icon, command, active=False):
        super().__init__(master, bg=theme.bg_sidebar, cursor="hand2")
        self.theme = theme
        self.command = command
        self.active = active
        self._lbl = tk.Label(self, text=f"  {icon}   {text}", anchor="w",
                             font=theme.f_body, padx=10, pady=7,
                             bg=theme.bg_sidebar, fg=theme.fg_muted)
        self._lbl.pack(fill="x")
        for w in (self, self._lbl):
            w.bind("<Button-1>", lambda e: self.command())
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", lambda e: self._hover(False))
        self._paint()

    def _hover(self, on):
        if self.active:
            return
        t = self.theme
        c = t.bg_hover if on else t.bg_sidebar
        # 只有真的变化才 configure（Tk 的 configure 即使同值也有开销）
        if getattr(self, "_cur_bg", None) == c:
            return
        self._cur_bg = c
        self.configure(bg=c)
        self._lbl.configure(bg=c)

    def set_active(self, active):
        if active == self.active:
            return                       # 选中态没变 → 不重绘
        self.active = active
        self._paint()

    def _paint(self):
        if not _alive(self):
            return
        t = self.theme
        if self.active:
            bg, fg = t.sidebar_active_bg, t.sidebar_active_fg
        else:
            bg, fg = t.bg_sidebar, t.fg_muted
        self._cur_bg = bg
        try:
            self.configure(bg=bg)
            self._lbl.configure(bg=bg, fg=fg)
        except tk.TclError:
            pass

    def refresh(self, theme):
        if not _alive(self):
            return
        self.theme = theme
        try:
            self._lbl.configure(font=theme.f_body)
            self._paint()
        except tk.TclError:
            pass


class Sidebar(tk.Frame):
    """完整侧边栏。"""

    def __init__(self, master, theme, on_nav, on_module):
        super().__init__(master, bg=theme.bg_sidebar, width=SIDEBAR_W)
        self.pack_propagate(False)
        self.theme = theme
        self.on_nav = on_nav
        self.on_module = on_module
        self.module = "traework"
        self.view = "dashboard"
        self.app = None

        # 分段控制器
        wrap = tk.Frame(self, bg=theme.bg_sidebar)
        wrap.pack(fill="x", padx=10, pady=(12, 6))
        self.seg = Segmented(wrap, theme, MODULES, command=self._switch_module,
                             height=32)
        self.seg.pack(fill="x")

        # 模块导航
        self.nav_box = tk.Frame(self, bg=theme.bg_sidebar)
        self.nav_box.pack(fill="x", padx=8, pady=(4, 0))
        self._items = {}

        # 底部：作者入口 + 全局导航
        self.bottom = tk.Frame(self, bg=theme.bg_sidebar)
        self.bottom.pack(side="bottom", fill="x", padx=8, pady=(0, 10))
        tk.Frame(self.bottom, bg=theme.border, height=1).pack(fill="x", pady=(0, 8))
        self._global = {}
        for key, label, icon in GLOBAL_NAV:
            it = NavItem(self.bottom, theme, label, icon,
                         lambda k=key: self._nav(k), active=(key == self.view))
            it.pack(fill="x")
            self._global[key] = it

        # 作者卡片入口（放在全局导航下方，与参考项目一致）
        self.author_box = tk.Frame(self.bottom, bg=theme.bg_sidebar)
        self.author_box.pack(fill="x", pady=(6, 0))
        tk.Frame(self.author_box, bg=theme.border, height=1).pack(
            fill="x", pady=(0, 4))

        self._build_nav()

    def attach_author(self, author_widget):
        """挂载作者入口控件（由 App 在合适时机注入，避免循环依赖）。"""
        self._author = author_widget

    def open_author(self):
        """点击作者入口：交给 App 弹卡片。"""
        if self.app is not None and hasattr(self.app, "show_author"):
            self.app.show_author()

    def _build_nav(self):
        for w in self.nav_box.winfo_children():
            w.destroy()
        self._items = {}
        for key, label, icon in NAV.get(self.module, []):
            it = NavItem(self.nav_box, self.theme, label, icon,
                         lambda k=key: self._nav(k), active=(key == self.view))
            it.pack(fill="x")
            self._items[key] = it

    def _switch_module(self, module):
        if module == self.module:
            return
        self.module = module
        # 切模块时默认回到概览
        self.view = "dashboard"
        self._build_nav()
        self._sync_global()
        self.on_module(module)

    def _nav(self, key):
        self.view = key
        # 全局页与模块页互斥显示选中态
        for k, it in self._items.items():
            it.set_active(k == key)
        self._sync_global()
        self.on_nav(self.module, key)

    def _sync_global(self):
        is_global = self.view in dict((k, 1) for k, _, _ in GLOBAL_NAV)
        for k, it in self._global.items():
            it.set_active(is_global and k == self.view)
        if is_global:
            for it in self._items.values():
                it.set_active(False)

    def select(self, module, view):
        """从外部同步选中状态（如顶栏跳转到设置页）。"""
        changed = module != self.module
        self.module = module
        self.view = view
        if changed:
            self.seg.set_active(module)
            self._build_nav()
        for k, it in self._items.items():
            it.set_active(k == view)
        self._sync_global()

    def refresh(self, theme):
        if not _alive(self):
            return
        self.theme = theme
        try:
            self.configure(bg=theme.bg_sidebar)
            self.seg.refresh(theme)
            for w in (self.nav_box, self.bottom):
                w.configure(bg=theme.bg_sidebar)
            for it in list(self._items.values()) + list(self._global.values()):
                it.refresh(theme)
        except tk.TclError:
            pass
