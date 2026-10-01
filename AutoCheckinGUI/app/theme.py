#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主题令牌：对齐 Trae-workbuddyAssistant 的 slate/zinc 配色体系。

参考项目用 Tailwind 的 slate(亮色底) / zinc(暗色底) 双轨配色，这里把
实际用到的色值收拢成一份 Python 令牌表，供 Tkinter 组件统一取用。
切换主题时只需替换 Theme 实例并重建界面。
"""

_FONT_CACHE = {}


def _pick_font(candidates):
    """从候选字体里挑第一个系统可用的，全都没有就返回首选项。

    直接硬编码字体名的问题：不同机器装的中文字体不一致，写死会在
    缺字体的机器上回退到点阵宋体，小字号下非常糊。这里做一次真实探测，
    结果缓存，避免每次建 Theme 都创建 Tk 环境。
    """
    key = tuple(candidates)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    chosen = candidates[0] if candidates else "TkDefaultFont"
    try:
        import tkinter as tk
        from tkinter import font as tkfont
        # 已存在 Tk 根时直接查；否则临时建一个再销毁
        try:
            root = tk._default_root  # noqa: SLF001
        except Exception:  # noqa: BLE001
            root = None
        tmp = None
        if root is None:
            tmp = tk.Tk()
            tmp.withdraw()
            root = tmp
        try:
            available = {f.lower() for f in tkfont.families(root)}
            for name in candidates:
                if name.lower() in available:
                    chosen = name
                    break
        finally:
            if tmp is not None:
                try:
                    tmp.destroy()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    _FONT_CACHE[key] = chosen
    return chosen


class Theme:
    """单套主题的所有色值与字体参数。"""

    def __init__(self, dark: bool):
        self.dark = dark
        if dark:
            # ---- 暗色：zinc-950 底 + zinc-100 文字 ----
            self.bg = "#09090b"            # zinc-950 窗口底
            self.bg_soft = "#18181b"        # zinc-900 卡片
            self.bg_sidebar = "#09090b"     # 侧栏与窗口底同色
            self.bg_titlebar = "#09090b"
            self.bg_topbar = "#09090b"
            self.bg_hover = "#27272a"       # zinc-800 悬停
            self.bg_input = "#18181b"
            self.bg_elev = "#1f1f23"        # 浮层/模态
            self.bg_muted = "#27272a"       # 次级填充（进度条轨道等）

            self.border = "#27272a"         # zinc-800
            self.border_soft = "#3f3f46"    # zinc-700
            self.divider = "#1f1f23"

            self.fg = "#f4f4f5"             # zinc-100 主文字
            self.fg_muted = "#c4c4cb"       # 次文字（原 #a1a1aa，提亮以增清晰度）
            self.fg_dim = "#8e8e99"         # 弱文字（原 #71717a，保证在近黑底上可读）
            self.fg_invert = "#18181b"      # 反色文字（用于亮色块上）

            self.accent = "#fafafa"         # 主按钮底（暗色下为亮色块）
            self.accent_fg = "#18181b"
            self.accent_hover = "#e4e4e7"

            self.sidebar_active_bg = "#f4f4f5"
            self.sidebar_active_fg = "#18181b"
            self.segment_bg = "#18181b"
            self.segment_pill = "#f4f4f5"
            self.segment_active_fg = "#18181b"
            self.grid = "#3f3f46"
        else:
            # ---- 亮色：slate-100 底 + slate-800 文字 ----
            self.bg = "#f1f5f9"             # slate-100 窗口底
            self.bg_soft = "#ffffff"
            self.bg_sidebar = "#ffffff"
            self.bg_titlebar = "#f8fafc"    # slate-50
            self.bg_topbar = "#ffffff"
            self.bg_hover = "#f1f5f9"       # slate-100
            self.bg_input = "#ffffff"
            self.bg_elev = "#ffffff"
            self.bg_muted = "#e2e8f0"       # slate-200

            self.border = "#e2e8f0"         # slate-200
            self.border_soft = "#cbd5e1"    # slate-300
            self.divider = "#f1f5f9"

            self.fg = "#1e293b"             # slate-800
            self.fg_muted = "#475569"       # slate-600（原 slate-500，加深以增清晰度）
            self.fg_dim = "#64748b"         # slate-500（原 slate-400，白底上更易读）
            self.fg_invert = "#ffffff"

            self.accent = "#18181b"         # zinc-900 主按钮底
            self.accent_fg = "#ffffff"
            self.accent_hover = "#27272a"

            self.sidebar_active_bg = "#18181b"
            self.sidebar_active_fg = "#ffffff"
            self.segment_bg = "#f1f5f9"
            self.segment_pill = "#18181b"
            self.segment_active_fg = "#ffffff"
            self.grid = "#e2e8f0"

        # ---- 语义色（成功/警告/危险/信息，双主题各自适配） ----
        if dark:
            self.green_fg = "#6ee7b7"       # emerald-300
            self.green_bg = "#064e3b"       # emerald-500/15 近似
            self.amber_fg = "#fcd34d"       # amber-300
            self.amber_bg = "#4a3208"
            self.red_fg = "#fda4af"         # rose-300
            self.red_bg = "#4c0519"
            self.blue_fg = "#7dd3fc"        # sky-300
            self.blue_bg = "#082f49"
            self.violet_fg = "#c4b5fd"      # violet-300
            self.violet_bg = "#2e1065"
            self.slate_fg = "#d4d4d8"       # zinc-300
            self.slate_bg = "#27272a"
        else:
            self.green_fg = "#047857"       # emerald-700
            self.green_bg = "#d1fae5"       # emerald-100
            self.amber_fg = "#b45309"       # amber-700
            self.amber_bg = "#fef3c7"
            self.red_fg = "#be123c"         # rose-700
            self.red_bg = "#ffe4e6"
            self.blue_fg = "#0369a1"        # sky-700
            self.blue_bg = "#e0f2fe"
            self.violet_fg = "#6d28d9"      # violet-700
            self.violet_bg = "#ede9fe"
            self.slate_fg = "#475569"       # slate-600
            self.slate_bg = "#f1f5f9"

        # 令牌过期告警的强调色
        self.warn = self.amber_fg
        self.danger = self.red_fg
        self.ok = self.green_fg

        # ---- 字体：中文优先微软雅黑，回退 Segoe UI / 微软雅黑 ----
        # 「Microsoft YaHei UI」是 Win8+ 的界面版字形，比普通雅黑在小字号下
        # 更清晰；若系统缺失则由 Tk 自动回退，故这里只需给首选名。
        self.font_family = _pick_font([
            "Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
            "Segoe UI", "Tahoma",
        ])
        self.font_mono = _pick_font([
            "Consolas", "Cascadia Mono", "Courier New",
        ])
        self.f_title = (self.font_family, 15, "bold")
        self.f_subtitle = (self.font_family, 11)
        self.f_card_title = (self.font_family, 10, "bold")
        self.f_body = (self.font_family, 10)
        self.f_small = (self.font_family, 9)
        self.f_tiny = (self.font_family, 9)      # 原 8：8pt 中文笔画会糊，提到 9
        self.f_btn = (self.font_family, 10)
        self.f_btn_sm = (self.font_family, 9)
        self.f_stat = (self.font_family, 20, "bold")
        self.f_logo = (self.font_family, 10, "bold")
        self.f_mono = (self.font_mono, 9)

    def tone(self, name):
        """返回 (前景, 背景) 语义色对，name 取 slate/brand/green/amber/red/blue/violet。"""
        table = {
            "slate": (self.slate_fg, self.slate_bg),
            "brand": (self.fg, self.bg_muted),
            "green": (self.green_fg, self.green_bg),
            "amber": (self.amber_fg, self.amber_bg),
            "red": (self.red_fg, self.red_bg),
            "blue": (self.blue_fg, self.blue_bg),
            "violet": (self.violet_fg, self.violet_bg),
        }
        return table.get(name, table["slate"])

    def accent_bar(self, name):
        """统计卡顶部 2px 强调条颜色。"""
        return {
            "slate": "#94a3b8", "brand": "#71717a", "green": "#10b981",
            "amber": "#f59e0b", "red": "#f43f5e", "blue": "#0ea5e9",
            "violet": "#8b5cf6",
        }.get(name, "#94a3b8")

    def value_fg(self, name):
        """统计卡数值颜色。"""
        return {
            "slate": self.fg, "brand": self.fg, "green": self.green_fg,
            "amber": self.amber_fg, "red": self.red_fg, "blue": self.blue_fg,
            "violet": self.violet_fg,
        }.get(name, self.fg)
