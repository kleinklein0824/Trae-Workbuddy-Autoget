#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自定义标题栏 + 无边框窗口支持。

对齐参考项目的 TitleBar：左侧 Logo 方块 + 应用名，右侧窗口按钮，
关闭按钮悬停变危险色。

「两层窗口框架」的完整根因（实测确认）
--------------------------------------
只调用 `overrideredirect(True)` 是不完整的，实测窗口样式为：

    STYLE   = 0x96CC0008
              -> WS_POPUP + WS_CAPTION + WS_THICKFRAME + WS_SYSMENU
                 + WS_BORDER + WS_DLGFRAME
    EXSTYLE = 0x00000180
              -> WS_EX_TOOLWINDOW + WS_EX_WINDOWEDGE
    非客户区 = 16 x 39        ← 左右各 8px 边线 + 31px 标题区

三件事同时出问题：

1. **两层外框**：`WS_CAPTION` 仍在 → 系统依旧绘制「边线 + 标题区」，
   与我们自绘的 TitleBar 叠成两层。必须去掉 CAPTION/BORDER/DLGFRAME。
2. **任务栏没有图标**：`overrideredirect` 会让 Tk 把窗口标成
   `WS_EX_TOOLWINDOW`，而 Windows 对 TOOLWINDOW 是**明确不进任务栏**的。
   必须清掉 TOOLWINDOW 并加上 `WS_EX_APPWINDOW`。
3. **最大化后板块被裁**：残留的 16x39 非客户区让窗口矩形与客户区不一致，
   Tk 按客户区排版，于是右侧/底部内容被挤到可视区外。

因此正确的做法是：
  overrideredirect(True) → 重写 STYLE/EXSTYLE → 通知 DWM 重算非客户区
  → 显式设置图标（任务栏与 Alt-Tab 都用它）。
"""
import sys
import tkinter as tk

from .widgets import IconButton

IS_WIN = sys.platform.startswith("win")

# ---- Win32 常量 ----
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9
SW_SHOW = 5

GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_CAPTION = 0x00C00000       # WS_BORDER | WS_DLGFRAME
WS_BORDER = 0x00800000
WS_DLGFRAME = 0x00400000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000

WS_EX_APPWINDOW = 0x00040000   # 强制进任务栏
WS_EX_TOOLWINDOW = 0x00000080  # 不进任务栏（必须清掉）
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_STATICEDGE = 0x00020000
WS_EX_DLGMODALFRAME = 0x00000001

SWP_FRAMECHANGED = 0x0020
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

# DWM：让窗口不做非客户区渲染（彻底去掉系统边线阴影框）
DWMWA_NCRENDERING_POLICY = 2
DWMNCRP_DISABLED = 1

# 允许用鼠标拖动窗口边缘改变大小的感应宽度
RESIZE_MARGIN = 6


def _hwnd(widget):
    """取窗口句柄（Windows）。

    注意：`winfo_id()` 返回的可能是**子窗口**句柄，对顶层窗口要用
    `frame()` 的结果（十六进制字符串）才是真正的 HWND。
    两者混淆会导致 SetWindowLongPtr 改到错误的窗口上——表现为
    「样式改了但没生效」。
    """
    try:
        widget.update_idletasks()
        fr = widget.frame()
        if isinstance(fr, str):
            try:
                return int(fr, 16)
            except ValueError:
                pass
        return int(widget.winfo_id())
    except Exception:  # noqa: BLE001
        return None


class TitleBar(tk.Frame):
    """自绘标题栏：Logo + 应用名 + 窗口按钮 + 拖拽。"""

    def __init__(self, master, theme, on_close=None, title="Trae & Buddy 签到助手"):
        super().__init__(master, bg=theme.bg_titlebar, height=40)
        self.pack_propagate(False)
        self.theme = theme
        self.win = master
        self._drag = (0, 0)
        self._max_btn = None

        left = tk.Frame(self, bg=theme.bg_titlebar)
        left.pack(side="left", fill="y", padx=(12, 0))

        # Logo 方块（Canvas 画圆角，避免 Label 的直角显得廉价）
        self._logo = tk.Canvas(left, width=28, height=28, bg=theme.bg_titlebar,
                               highlightthickness=0, bd=0)
        self._logo.pack(side="left", pady=6)
        self._draw_logo()
        self._logo_text = self._logo.create_text(14, 14, text="TB", fill="#ffffff",
                                                font=(theme.font_family, 9, "bold"))

        self._name = tk.Label(left, text=title, font=(theme.font_family, 10, "bold"),
                              fg=theme.fg, bg=theme.bg_titlebar)
        self._name.pack(side="left", padx=(9, 0))

        right = tk.Frame(self, bg=theme.bg_titlebar)
        right.pack(side="right", fill="y")
        self._btn_min = IconButton(right, theme, "—", command=self.minimize)
        self._btn_min.pack(side="left", fill="y")
        self._max_btn = IconButton(right, theme, "▢", command=self.toggle_max)
        self._max_btn.pack(side="left", fill="y")
        self._btn_close = IconButton(right, theme, "✕", command=on_close,
                                     hover_bg=theme.red_fg, hover_fg="#ffffff")
        self._btn_close.pack(side="left", fill="y")

        # 拖拽：整个标题栏（按钮自身会消费事件）
        for w in (self, left, self._name, self._logo):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<Double-Button-1>", lambda e: self.toggle_max())
            w.bind("<ButtonRelease-1>", self._drag_end)

    # ---------------- 绘制 ----------------

    def _draw_logo(self):
        """圆角深色 Logo 底 + 高光，避免直角方块的廉价感。"""
        from .widgets import round_rect
        try:
            self._logo.delete("logo")
            dark = self.theme.dark
            base = "#fafafa" if dark else "#18181b"
            round_rect(self._logo, 0, 0, 28, 28, 7, fill=base, outline="",
                       tags="logo")
            self._logo.tag_lower("logo")
        except Exception:  # noqa: BLE001
            try:
                self._logo.delete("all")
                self._logo.configure(bg="#18181b")
            except Exception:  # noqa: BLE001
                pass

    # ---------------- 窗口控制 ----------------

    def minimize(self):
        """最小化。overrideredirect 下 iconify() 失效，改用 Win32 API。"""
        w = self.win
        if IS_WIN:
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(_hwnd(w), SW_MINIMIZE)
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            w.iconify()
        except Exception:  # noqa: BLE001
            pass

    def toggle_max(self):
        self.restore() if getattr(self.win, "_is_max", False) else self.maximize()

    def maximize(self):
        """最大化到工作区（避开任务栏），并在之后强制重排布局。"""
        w = self.win
        if getattr(w, "_is_max", False):
            return
        # 记录还原信息，供 restore 使用
        w._restore_geo = (w.winfo_x(), w.winfo_y(), w.winfo_width(), w.winfo_height())
        done = False
        if IS_WIN:
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(_hwnd(w), SW_MAXIMIZE)
                done = True
            except Exception:  # noqa: BLE001
                done = False
        if not done:
            # 非 Windows 兜底：手动铺满工作区
            try:
                sw = w.winfo_screenwidth()
                sh = w.winfo_screenheight()
                w.geometry(f"{sw}x{sh-48}+0+0")
            except Exception:  # noqa: BLE001
                pass
        w._is_max = True
        self._sync_max_icon()
        self._after_geometry_change()

    def restore(self):
        w = self.win
        if IS_WIN:
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(_hwnd(w), SW_RESTORE)
            except Exception:  # noqa: BLE001
                pass
        w._is_max = False
        geo = getattr(w, "_restore_geo", None)
        if geo:
            x, y, cw, ch = geo
            try:
                # 还原后几何可能被系统改过，主动写回
                w.geometry(f"{cw}x{ch}+{x}+{y}")
            except Exception:  # noqa: BLE001
                pass
        self._sync_max_icon()
        self._after_geometry_change()

    def _after_geometry_change(self):
        """几何变化后通知全局重排。

        最大化时 Tk 收到的 Configure 事件时序与 Win32 不同步：
        页面此刻已经按旧尺寸算好了卡片高度与 scrollregion，
        结果就是「右侧/底部有板块看不见」。这里在下一帧强制
        重新计算一次布局（页面侧通过 on_geometry 钩子响应）。
        """
        w = self.win

        def fix():
            try:
                w.update_idletasks()
            except Exception:  # noqa: BLE001
                return
            page = getattr(w, "page", None)
            if page is not None and hasattr(page, "on_geometry"):
                try:
                    page.on_geometry()
                except Exception:  # noqa: BLE001
                    pass

        for delay in (0, 60, 180):
            try:
                w.after(delay, fix)
            except Exception:  # noqa: BLE001
                break

    def _sync_max_icon(self):
        try:
            if self._max_btn is not None:
                self._max_btn.configure(text="❐" if getattr(self.win, "_is_max", False)
                                        else "▢")
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 拖拽 ----------------

    def _drag_start(self, e):
        if getattr(self.win, "_is_max", False):
            # 最大化状态下拖动 → 先还原（与原生窗口一致的手感）
            self._pending_restore = True
            self._drag = (0, 0)
            return
        self._pending_restore = False
        self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _drag_move(self, e):
        if getattr(self, "_pending_restore", False):
            return
        dx, dy = self._drag
        if dx == 0 and dy == 0:
            return
        try:
            self.win.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")
        except Exception:  # noqa: BLE001
            pass

    def _drag_end(self, _e=None):
        self._pending_restore = False

    def refresh(self, theme):
        self.theme = theme
        try:
            self.configure(bg=theme.bg_titlebar)
            self._name.configure(bg=theme.bg_titlebar, fg=theme.fg)
            self._logo.configure(bg=theme.bg_titlebar)
            self._draw_logo()
            # 深色主题下 Logo 底变浅，字要转深色才看得清
            self._logo.itemconfigure(self._logo_text,
                                     fill="#18181b" if theme.dark else "#ffffff")
            for w in self.winfo_children():
                try:
                    w.configure(bg=theme.bg_titlebar)
                except Exception:  # noqa: BLE001
                    continue
            for b in (self._btn_min, self._max_btn, self._btn_close):
                if b is not None and hasattr(b, "refresh"):
                    b.refresh(theme)
        except Exception:  # noqa: BLE001
            pass


def _winapi():
    """统一的 Win32 句柄获取 + 原型声明（避免每处重复写）。"""
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    u.GetWindowLongPtrW.restype = ctypes.c_void_p
    u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetWindowLongPtrW.restype = ctypes.c_void_p
    u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    return u, ctypes, wintypes


def set_window_icon(win):
    """显式设置窗口图标：任务栏、Alt-Tab、标题栏缩略图都用它。

    `overrideredirect` 之后 Tk 不再自动把图标关联到扩展样式，
    必须用 WM_SETICON 手动下发大小两个图标，否则任务栏是空白图标。
    """
    if not IS_WIN:
        return
    from .resources import app_icon_ico
    path = app_icon_ico()
    if not path:
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = _hwnd(win)
        if not hwnd:
            return
        # LR_LOADFROMFILE | LR_DEFAULTSIZE
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        hicon = u.LoadImageW(None, str(path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        if not hicon:
            return
        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        u.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)
        u.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon)
        win._hicon = hicon          # 持引用，避免被回收
    except Exception:  # noqa: BLE001
        pass


def make_frameless(win):
    """把窗口变成真正的无边框，同时保留边缘缩放与任务栏图标。

    实测数据（同一窗口，逐步施加）：

        | 状态                        | STYLE      | EXSTYLE    | 非客户区 |
        |-----------------------------|------------|------------|----------|
        | Tk 默认                     | 0x16CF0008 | 0x00000100 | 16 x 39  |
        | 仅 overrideredirect(True)   | 0x00000000 | 0x00000000 | 0 x 0    |
        | 再补 WS_THICKFRAME           | 0x00040000 | 0x00040100 | 14 x 14  |

    结论：**只要加回 WS_THICKFRAME，系统就会重新生出 14x14 的缩放边**，
    这正是「两层外框」压不下去的原因——我们既想去掉边框，又想保留缩放，
    而这两件事在 Win32 下是冲突的。

    因此这里采取「不要 WS_THICKFRAME」的策略：
      * 保持 overrideredirect 带来的 nc=0（彻底没有系统边框）；
      * 边缘缩放完全交给 enable_edge_resize() 的 WM_NCHITTEST 处理
        （返回 HT* 后系统会自行进入缩放循环，不需要 THICKFRAME）；
      * 补 WS_EX_APPWINDOW，让窗口出现在任务栏。
    """
    try:
        win.overrideredirect(True)
    except Exception:  # noqa: BLE001
        return
    if not IS_WIN:
        return
    try:
        u, ctypes, _wt = _winapi()
        hwnd = _hwnd(win)
        if not hwnd:
            return

        # STYLE 保持 0（POPUP 风格，无任何系统边框/标题/菜单）
        style = u.GetWindowLongPtrW(hwnd, GWL_STYLE) or 0
        style &= ~(WS_CAPTION | WS_BORDER | WS_DLGFRAME | WS_THICKFRAME)
        u.SetWindowLongPtrW(hwnd, GWL_STYLE, style)

        # EXSTYLE：清掉不进任务栏的 TOOLWINDOW，加上 APPWINDOW
        ex = u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) or 0
        ex &= ~(WS_EX_TOOLWINDOW | WS_EX_CLIENTEDGE
                | WS_EX_STATICEDGE | WS_EX_DLGMODALFRAME)
        ex |= WS_EX_APPWINDOW
        u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)

        u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                       SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE
                       | SWP_NOZORDER | SWP_NOACTIVATE)

        # 让 Tk 重新接管窗口（overrideredirect 后 Tk 会停止响应某些消息，
        # 这里把窗口映射一次，确保样式生效且能收到鼠标事件）
        try:
            win.deiconify()
        except Exception:  # noqa: BLE001
            pass

        set_window_icon(win)
    except Exception:  # noqa: BLE001
        pass


def enable_edge_resize(win, margin=RESIZE_MARGIN):
    """接管窗口过程：补回边缘缩放，并锁死「没有非客户区」。

    为什么需要它（而不是用 WS_THICKFRAME）
    --------------------------------------
    实测发现只要给窗口加回 `WS_THICKFRAME`，系统就会重新生成 14x14 的
    缩放边框，非客户区不再为 0，于是又出现「两层外框」。
    所以现在完全不加 THICKFRAME，缩放改为在这里处理 `WM_NCHITTEST`：
    鼠标落在边缘时返回 HTLEFT/HTRIGHT/... 系统会自动进入原生缩放循环。

    同时处理两个配套消息：
      * `WM_NCCALCSIZE`：强制客户区 = 整窗（继续保证 nc=0，
        即使将来有人把 THICKFRAME 加回来也不会冒边）。
      * `WM_GETMINMAXINFO`：最大化时不用系统默认的全屏，
        而是限制到工作区（避开任务栏），否则内容会被任务栏挡住。

    踩坑记录（不要在 WNDPROC 里做的事）：
      * 参数一律用 ctypes 整数类型，不要用 wintypes.HWND/BOOL 这类会做
        隐式转换的类型，否则回传时 int 溢出 → ctypes.ArgumentError，
        进而被当成「回调异常被忽略」刷屏。
      * 不要在回调里做重活，只做纯整数运算 + 调用原始过程。
    """
    if not IS_WIN:
        return None
    # 已经被接管过就不要重复装，否则回调链会越套越深
    if getattr(win, "_resize_proc", None) is not None:
        return win._resize_proc
    try:
        from ctypes import wintypes

        u, ctypes, _wt = _winapi()
        hwnd = _hwnd(win)
        if not hwnd:
            return None

        GWLP_WNDPROC = -4
        WM_NCHITTEST = 0x0084
        WM_NCCALCSIZE = 0x0083
        WM_GETMINMAXINFO = 0x0024

        HTLEFT, HTRIGHT = 10, 11
        HTTOP, HTTOPLEFT, HTTOPRIGHT = 12, 13, 14
        HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17

        u.CallWindowProcW.restype = ctypes.c_void_p
        u.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                      ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                            ctypes.c_void_p, ctypes.c_uint]

        # 回调签名：LRESULT(HWND, UINT msg, WPARAM, LPARAM)
        LRESULT = ctypes.c_void_p if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                                     ctypes.c_void_p, ctypes.c_void_p)

        class MINMAXINFO(ctypes.Structure):
            _fields_ = [("ptReserved", wintypes.POINT),
                        ("ptMaxSize", wintypes.POINT),
                        ("ptMaxPosition", wintypes.POINT),
                        ("ptMinTrackSize", wintypes.POINT),
                        ("ptMaxTrackSize", wintypes.POINT)]

        old_proc = u.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)
        if not old_proc:
            return None

        state = {"old": ctypes.c_void_p(old_proc), "proc": None, "margin": int(margin)}
        rect = wintypes.RECT()
        wa = wintypes.RECT()          # 工作区缓存

        def _handler(h, msg, wparam, lparam):
            try:
                if msg == WM_NCCALCSIZE:
                    # wparam != 0 时表示要重算客户区，直接返回 0 = 客户区占满整窗
                    if wparam:
                        return 0
                elif msg == WM_NCHITTEST:
                    lp = lparam or 0
                    x = ctypes.c_short(lp & 0xFFFF).value
                    y = ctypes.c_short((lp >> 16) & 0xFFFF).value
                    if u.GetWindowRect(h, ctypes.byref(rect)):
                        m = state["margin"]
                        on_l = (x - rect.left) < m
                        on_r = (rect.right - x) < m
                        on_t = (y - rect.top) < m
                        on_b = (rect.bottom - y) < m
                        if on_t and on_l:
                            return HTTOPLEFT
                        if on_t and on_r:
                            return HTTOPRIGHT
                        if on_b and on_l:
                            return HTBOTTOMLEFT
                        if on_b and on_r:
                            return HTBOTTOMRIGHT
                        if on_l:
                            return HTLEFT
                        if on_r:
                            return HTRIGHT
                        if on_t:
                            return HTTOP
                        if on_b:
                            return HTBOTTOM
                elif msg == WM_GETMINMAXINFO:
                    # 最大化限制到工作区（避开任务栏）
                    if lparam and u.SystemParametersInfoW(0x0030, 0,
                                                          ctypes.byref(wa), 0):
                        mmi = ctypes.cast(lparam,
                                          ctypes.POINTER(MINMAXINFO)).contents
                        w = wa.right - wa.left
                        hgt = wa.bottom - wa.top
                        mmi.ptMaxSize.x = w
                        mmi.ptMaxSize.y = hgt
                        mmi.ptMaxPosition.x = wa.left
                        mmi.ptMaxPosition.y = wa.top
                        mmi.ptMaxTrackSize.x = w
                        mmi.ptMaxTrackSize.y = hgt
                        return 0
            except Exception:  # noqa: BLE001
                pass
            return u.CallWindowProcW(state["old"], h, msg, wparam, lparam)

        proc = WNDPROC(_handler)
        state["proc"] = proc                      # 必须持引用，否则被 GC 回收会崩
        u.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, proc)
        win._resize_proc = proc                   # 挂到窗口上防止回收
        win._resize_old = state                   # 保留旧过程
        return proc
    except Exception:  # noqa: BLE001
        return None
