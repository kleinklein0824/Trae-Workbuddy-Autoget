#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统托盘图标（通知区域 / 隐藏图标窗口）。

Windows 的托盘图标走 Shell_NotifyIcon：
  * 注册一个隐藏消息窗口接收回调（WM_APP+1）；
  * NIM_ADD 添加图标，NIM_MODIFY 更新气泡/提示，NIM_DELETE 移除；
  * 右键弹出菜单用 CreatePopupMenu + TrackPopupMenu。

不依赖 pywin32，全部用 ctypes 实现。

主要坑：
  * NOTIFYICONDATA 结构体在不同 Windows 版本尺寸不同，必须显式设置
    `cbSize`，否则 Shell_NotifyIcon 直接失败且不报错。
  * 菜单宿主窗口必须 `SetForegroundWindow`，否则菜单显示后不消失
    （经典的"右键菜单点不掉"问题）。
  * 图标句柄要在整个生命周期内持有引用，否则被 GC 回收后托盘变空白。
"""
import ctypes
import os
import sys
import threading
from ctypes import wintypes

IS_WIN = sys.platform.startswith("win")

# --- Shell_NotifyIcon ---
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0x00000000, 0x00000001, 0x00000002
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1        # 自定义回调消息
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_LBUTTONDBLCLK = 0x0203

IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010

MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

WS_EX_TOOLWINDOW = 0x00000080
WS_POPUP = 0x80000000

_GWLP_WNDPROC = -4
LRESULT = ctypes.c_void_p if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long


class NOTIFYICONDATA(ctypes.Structure):
    """64 位下的 NOTIFYICONDATA（szTip 128 字符版本）。"""
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


class TrayIcon:
    """托盘图标。

    参数：
      tooltip   —— 悬停提示文字
      menu      —— [(标签, 回调), None(=分隔线), ...]
      on_click  —— 左键单击/双击回调（通常用于显示主窗口）
    """

    def __init__(self, tooltip="签到助手", menu=None, on_click=None,
                 icon_path=None):
        self.tooltip = tooltip
        self.menu = menu or []
        self.on_click = on_click
        self.icon_path = icon_path
        self._hwnd = None
        self._hicon = None
        self._nid = None
        self._proc = None
        self._thread = None
        self._ready = threading.Event()
        self._running = False

    # ---------- 生命周期 ----------

    def start(self):
        """在后台线程里创建消息窗口并添加图标。

        托盘消息窗口需要自己的消息循环，不能占用 Tk 的主循环，
        所以单独起一个线程跑 GetMessage 循环。
        """
        if not IS_WIN or self._running:
            return False
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="tray-icon")
        self._thread.start()
        self._ready.wait(timeout=4)
        return self._hwnd is not None

    def _run(self):
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32

        # 声明原型
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        u.DefWindowProcW.restype = ctypes.c_void_p
        u.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                     ctypes.c_void_p, ctypes.c_void_p]
        u.CallWindowProcW.restype = ctypes.c_void_p
        u.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                      ctypes.c_uint, ctypes.c_void_p,
                                      ctypes.c_void_p]

        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                                     ctypes.c_void_p, ctypes.c_void_p)

        def _wndproc(h, msg, wparam, lparam):
            try:
                if msg == WM_TRAYICON:
                    lo = (lparam or 0) & 0xFFFF
                    if lo in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                        if self.on_click:
                            try:
                                self.on_click()
                            except Exception:  # noqa: BLE001
                                pass
                    elif lo == WM_RBUTTONUP:
                        self._popup_menu(h)
                        return 0
            except Exception:  # noqa: BLE001
                pass
            return u.DefWindowProcW(h, msg, wparam, lparam)

        try:
            self._proc = WNDPROC(_wndproc)
            hinst = k.GetModuleHandleW(None)
            cls = "TraeBuddyTrayWnd"
            # 注册窗口类（重复注册会失败，忽略即可）
            class WNDCLASS(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", ctypes.c_void_p),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            wc = WNDCLASS()
            wc.lpfnWndProc = ctypes.cast(self._proc, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = cls
            u.RegisterClassW(ctypes.byref(wc))

            self._hwnd = u.CreateWindowExW(
                WS_EX_TOOLWINDOW, cls, "TraeBuddyTray", WS_POPUP,
                0, 0, 0, 0, None, None, hinst, None)
            if not self._hwnd:
                self._running = False
                self._ready.set()
                return

            self._add_icon()
            self._ready.set()

            # 消息循环
            msg = wintypes.MSG()
            while self._running and u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        except Exception:  # noqa: BLE001
            self._ready.set()
        finally:
            try:
                self._remove_icon()
            except Exception:  # noqa: BLE001
                pass

    def _add_icon(self):
        u = ctypes.windll.user32
        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.szTip = self.tooltip[:127]
        if self.icon_path and os.path.exists(self.icon_path):
            hicon = u.LoadImageW(None, str(self.icon_path), IMAGE_ICON,
                                 0, 0, LR_LOADFROMFILE)
            if hicon:
                self._hicon = hicon
        if not self._hicon:
            hicon = u.LoadIconW(None, ctypes.c_wchar_p(32512))  # IDI_APPLICATION
            self._hicon = hicon
        nid.hIcon = self._hicon
        self._nid = nid
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    def _remove_icon(self):
        if self._nid is not None and self._hwnd:
            ctypes.windll.shell32.Shell_NotifyIconW(
                NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None

    def stop(self):
        """移除图标（退出时调用，否则会残留一个死图标）。"""
        self._running = False
        try:
            self._remove_icon()
        except Exception:  # noqa: BLE001
            pass
        if self._hwnd:
            try:
                ctypes.windll.user32.PostMessageW(self._hwnd, 0x0012, 0, 0)  # WM_QUIT
            except Exception:  # noqa: BLE001
                pass
            self._hwnd = None

    def set_tooltip(self, text):
        self.tooltip = text
        if self._nid is None:
            return
        try:
            self._nid.szTip = text[:127]
            ctypes.windll.shell32.Shell_NotifyIconW(
                NIM_MODIFY, ctypes.byref(self._nid))
        except Exception:  # noqa: BLE001
            pass

    # ---------- 右键菜单 ----------

    def _popup_menu(self, hwnd):
        u = ctypes.windll.user32
        # CreatePopupMenu 返回 HMENU 指针，64 位下不声明 restype 会被截断成 int
        u.CreatePopupMenu.restype = wintypes.HMENU
        u.CreatePopupMenu.argtypes = []
        u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                  ctypes.c_void_p, wintypes.LPCWSTR]
        u.TrackPopupMenu.restype = ctypes.c_int
        u.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     wintypes.HWND, ctypes.c_void_p]
        u.DestroyMenu.argtypes = [wintypes.HMENU]
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        try:
            hmenu = u.CreatePopupMenu()
            ids = {}
            for i, item in enumerate(self.menu):
                if item is None:
                    u.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                    continue
                label, cb = item
                cmd = 1000 + i
                ids[cmd] = cb
                u.AppendMenuW(hmenu, MF_STRING, cmd, label)

            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            # 非前台窗口的菜单会"点不掉"，必须先抢前台
            u.SetForegroundWindow(hwnd)
            cmd = u.TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                   pt.x, pt.y, 0, hwnd, None)
            u.PostMessageW(hwnd, 0x0000, 0, 0)      # WM_NULL：修菜单残影
            u.DestroyMenu(hmenu)

            cb = ids.get(cmd)
            if cb:
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
