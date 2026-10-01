#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""应用主窗口：装配标题栏 / 侧栏 / 顶栏 / 页面路由 / 主题切换。

页面以「模块 + 视图」二维定位：模块取 traework / traecn / workbuddy，
视图取 dashboard / checkin / accounts / credits，另有全局视图 logs / settings。
"""
import queue
import tkinter as tk

from . import config as C
from .services import scheduler
from .ui.sidebar import Sidebar
from .ui.titlebar import TitleBar
from .ui.toast import Toaster
from .ui.topbar import TopBar
from .theme import Theme

MODULE_CN = {"traework": "TraeWork", "traecn": "TraeCN", "workbuddy": "WorkBuddy"}


def _alive(widget):
    """控件是否仍然存在（页面缓存用；销毁过的残骸必须丢弃）。"""
    try:
        return bool(widget.winfo_exists())
    except Exception:  # noqa: BLE001
        return False


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(C.APP_TITLE if hasattr(C, "APP_TITLE") else "Trae & Buddy 签到助手")
        self.geometry("1180x760")
        self.minsize(1040, 640)
        self.configure(bg="#f1f5f9")
        self._set_tk_icon()

        # 数据目录初始化必须最先做：exe 便携版首次运行时该目录还没有
        # config.json，若不先铺好，后面读到的就是内存里的默认值，
        # 界面表现为「0 个账号 / 暂无账号」，与用户实际配置脱节。
        self._first_run, self._data_root = C.ensure_data_dir()

        # 主题（默认跟随已保存偏好：亮色）
        self.dark = bool(C.load_config().get("options", {}).get("dark_theme", False))
        self.theme = Theme(self.dark)

        # 当前定位
        self.module = "traework"
        self.view = "dashboard"

        self._build()

        # 去掉系统原生标题栏，只保留自绘 TitleBar（否则会出现两层框架）
        # 必须放在 _build 之后：此时窗口已创建，Win32 句柄才有效。
        self._apply_frameless()
        # 居中显示
        self._center()

        self.toaster = Toaster(self, self.theme)
        self._net_ok = True           # 乐观初值，随后由后台探测校正
        self._cred_busy = False       # 凭据后台刷新的单飞标志
        self._build_tray()
        self.after(120, self._warm_task_cache)
        self.after(200, self._refresh_topbar)
        self.after(400, lambda: self._first_render())
        self.after(900, self._probe_net)
        self.after(1200, self.refresh_accounts)   # 启动即后台预热账号缓存
        self.after(1600, self._cred_tick)         # 凭据自动重新捕获（可配置间隔）

    # ---------------- 计划任务状态 ----------------

    def _warm_task_cache(self):
        """启动时后台预热计划任务状态缓存。

        `task_info()` 要起 PowerShell（约 1s），绝不能在渲染路径上同步调用，
        否则每次切页都会卡住 1 秒以上（一次 dashboard 渲染会读 7 次）。
        这里先异步取一次，之后页面直接读缓存，秒开。
        """
        try:
            scheduler.refresh_task_info_async(
                on_done=lambda _d: self.ui_call(self._after_task_refresh))
        except Exception:  # noqa: BLE001
            pass
        # 依赖探测（import playwright 约 400ms）也一起预热到后台
        import threading
        threading.Thread(target=self._warm_deps, daemon=True,
                         name="deps-probe").start()

    @staticmethod
    def _warm_deps():
        try:
            from .ui.setup_guide import _deps_ok
            _deps_ok()
        except Exception:  # noqa: BLE001
            pass

    def _after_task_refresh(self):
        """任务状态刷新完成 → 更新顶栏与当前页。"""
        try:
            self._refresh_topbar()
        except Exception:  # noqa: BLE001
            pass
        page = self.page
        if page is not None and hasattr(page, "on_task_updated"):
            try:
                page.on_task_updated()
            except Exception:  # noqa: BLE001
                pass

    def refresh_task(self):
        """强制刷新任务状态（用户主动点击时用）。"""
        def done(_d):
            self.ui_call(self._after_task_refresh)

        scheduler.refresh_task_info_async(on_done=done)

    # ---------------- 系统托盘 ----------------

    def _build_tray(self):
        """常驻系统托盘（通知区域）图标。

        `overrideredirect` 窗口虽然靠 WS_EX_APPWINDOW 进了任务栏，
        但用户仍可能希望「关掉窗口但保持后台待命」，托盘图标承担这个角色。
        托盘不可用（非 Windows / 无 shell）时静默降级，不影响主窗口。
        """
        self.tray = None
        try:
            from .services.tray import TrayIcon
            from .ui.resources import app_icon_ico
            self.tray = TrayIcon(
                tooltip="Trae & Buddy 签到助手",
                icon_path=app_icon_ico(),
                on_click=self._tray_show,
                menu=[
                    ("显示主窗口", self._tray_show),
                    ("立即签到（全部平台）", self._tray_checkin),
                    None,
                    ("退出", self._tray_quit),
                ],
            )
            if not self.tray.start():
                self.tray = None
        except Exception:  # noqa: BLE001
            self.tray = None

    def _tray_show(self):
        """从托盘恢复并前置主窗口。"""
        def do():
            try:
                import ctypes
                from .ui.titlebar import _hwnd
                h = _hwnd(self)
                if h:
                    # 还原 + 置前（overrideredirect 下 Tk 的 deiconify 不够）
                    ctypes.windll.user32.ShowWindow(h, 9)      # SW_RESTORE
                    ctypes.windll.user32.SetForegroundWindow(h)
                else:
                    self.deiconify()
                    self.lift()
            except Exception:  # noqa: BLE001
                try:
                    self.deiconify()
                    self.lift()
                except Exception:  # noqa: BLE001
                    pass

        self.ui_call(do)

    def _tray_checkin(self):
        self.ui_call(self.quick_checkin)

    def _tray_quit(self):
        self.ui_call(self.destroy)

    def _set_tk_icon(self):
        """设置 Tk 层图标（任务栏 / Alt-Tab 的兜底）。

        Tk 的 iconphoto 只在窗口映射前设置才生效，所以放在 __init__ 最前面。
        随后 make_frameless() 还会用 WM_SETICON 再设一次（更可靠）。
        """
        try:
            from .ui.resources import app_icon_png
            path = app_icon_png()
            if not path:
                return
            self._icon_img = tk.PhotoImage(file=path)
            self.iconphoto(True, self._icon_img)
        except Exception:  # noqa: BLE001
            pass

    def _apply_frameless(self):
        """隐藏原生标题栏（解决「两层窗口框架」），并保留边缘缩放。"""
        try:
            from .ui.titlebar import enable_edge_resize, make_frameless
            make_frameless(self)
            # 边框拉伸需要窗口已映射，延后挂载更稳
            self.after(120, lambda: enable_edge_resize(self))
        except Exception:  # noqa: BLE001
            pass

    def _center(self):
        try:
            self.update_idletasks()
            w, h = 1180, 760
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 3)
            self.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 后台探测 ----------------

    def ui_call(self, fn):
        """把回调安全地调度回 UI 线程（线程安全，可在任意后台线程调用）。

        注意：**不能在后台线程里直接调用 `self.after()`**。
        Tkinter 的 `after` 最终会执行 `tk.createcommand`，而 Tcl 解释器只允许
        主线程访问，跨线程调用会抛
        `RuntimeError: main thread is not in main loop`（异常还会被静默吞掉，
        表现为界面永远停在「正在查询…」）。
        因此这里改用线程安全队列：后台线程只入队，主线程用定时器轮询出队。
        """
        if getattr(self, "_closing", False):
            return
        self._uiq.put(fn)

    def _drain_uiq(self):
        """主线程定时器：把后台线程投递的回调逐个执行。"""
        if getattr(self, "_closing", False):
            return
        for _ in range(64):
            try:
                fn = self._uiq.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._uiq_id = self.after(40, self._drain_uiq)
        except Exception:  # noqa: BLE001
            self._uiq_id = None

    # ---------------- 凭据自动重新捕获 ----------------

    def _cred_tick(self):
        """主线程定时器：每分钟问一次「到点该重新捕获了吗」。

        ★ 判据是「距上次刷新是否超过用户设的间隔」，不是「进程活了多久」——
          否则每次开程序都会立刻刷一次，用户设的 24 小时形同虚设。
        ★ 真正的捕获在后台线程里做（要读日志/注册表，虽然快但绝不放在 UI 路径上）。
        ★ 用 `_cred_busy` 单飞，避免上一轮还没跑完又叠一轮。
        ★★ **测试进程里直接不刷**（2026-09-25 加）：刷新会**写用户的真实
          `tokens_workbuddy.json` / `logs/`**，而 32 个测试脚本都会构造 `App()`。
          实测事故：测试给 `wb_checkin._load_saved` 打了桩，`capture_current()`
          顺手把测试夹具写进真实快照 → 界面上凭空多出两个「已捕获的账号」，
          ID 与名称都不对（用户报的「有些账号捕获的 ID 名称显示不对」）。
          测试要刷新就**自己显式调** `CR.refresh()`，别让构造 App 顺带写盘。
        """
        try:
            import runtime as _RT
            if _RT.in_test_harness():
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            from .services import credential_refresh as CR
        except Exception:  # noqa: BLE001
            return
        import threading
        try:
            if not getattr(self, "_closing", False) and not self._cred_busy \
                    and CR.due():
                self._cred_busy = True

                def work():
                    try:
                        ok, detail, _stats = CR.refresh()
                    except Exception as e:  # noqa: BLE001
                        ok, detail = False, repr(e)
                    self.ui_call(lambda: self._cred_tick_done(ok, detail))

                threading.Thread(target=work, daemon=True,
                                 name="cred-refresh").start()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.after(CR.TICK_SECONDS * 1000, self._cred_tick)
        except Exception:  # noqa: BLE001
            pass

    def _cred_tick_done(self, ok, detail):
        """后台捕获完成：只在**真有新增/更新**时提示，否则静默（别刷屏）。"""
        self._cred_busy = False
        try:
            if ok and detail and "无新增" not in detail:
                self.toast("success", "已自动重新捕获凭据：" + detail)
                self.refresh_accounts()
        except Exception:  # noqa: BLE001
            pass

    def _probe_net(self):
        """后台探测网络，结果回主线程更新顶栏徽标（避免 UI 卡在探测上）。"""
        import threading

        def work():
            ok = scheduler.network_ok(timeout_s=3)
            self.ui_call(lambda: self._net_result(ok))

        threading.Thread(target=work, daemon=True, name="net-probe").start()

    def _net_result(self, ok):
        self._net_ok = bool(ok)
        self._refresh_topbar()

    # ---------------- 装配 ----------------

    def _build(self):
        t = self.theme
        self.titlebar = TitleBar(self, t, on_close=self._on_close)
        self.titlebar.pack(fill="x", side="top")

        body = tk.Frame(self, bg=t.bg)
        body.pack(fill="both", expand=True)

        self.sidebar = Sidebar(body, t, on_nav=self._show, on_module=self._switch_module)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.app = self
        # 作者入口（信息与参考项目同源，含完整性自检）
        try:
            from .ui.author_card import AuthorButton
            self._author_btn = AuthorButton(self.sidebar.author_box, t, self,
                                            command=self.show_author)
            self._author_btn.pack(fill="x")
            self.sidebar.attach_author(self._author_btn)
        except Exception:  # noqa: BLE001
            self._author_btn = None

        main = tk.Frame(body, bg=t.bg)
        main.pack(side="left", fill="both", expand=True)
        self.topbar = TopBar(main, t, self)
        self.topbar.pack(fill="x", side="top")

        self.page_host = tk.Frame(main, bg=t.bg)
        self.page_host.pack(fill="both", expand=True)

        self.page = None
        # ★ 页面缓存（2026-09-22）：key=(module, view) → 页面实例。
        #   见 `_show()` 的说明：复用页面把「切页」从 226~1939ms 降到只跑 on_show()。
        #   主题切换时由 `apply_theme()` 整体失效（旧配色的控件不能留）。
        self._page_cache = {}
        self._acct_refreshing = False
        # 刷新期间迟到的请求会被记为「待补刷新」，结束后立刻再跑一轮（见 refresh_accounts）
        self._acct_pending = False
        self._acct_waiters = []
        self._closing = False
        self._resize_id = None
        # 后台线程 → UI 线程的回调队列（见 ui_call 说明）
        self._uiq = queue.Queue()
        self._uiq_id = self.after(40, self._drain_uiq)
        # 窗口缩放后重排（无边框窗口的 Configure 时序不可靠，需去抖兜底）
        self.bind("<Configure>", self._on_resize)

    # ---------------- 账号缓存 ----------------

    def accounts(self):
        """供页面读取的账号快照：有缓存立即返回，无缓存先给空结构并后台刷新。"""
        data = C.peek_accounts()
        if data is None:
            self.refresh_accounts()
            return {"traework": [], "workbuddy": [], "traecn": []}
        return data

    def refresh_accounts(self, on_done=None):
        """后台刷新账号（扫描 IDE 日志/磁盘），避免阻塞 UI 线程。

        单飞（single-flight）只用来防止并发重算，**不能吞掉后来的请求**：
        页面加载时那次刷新可能要跑 3~15 秒，用户若在这期间新增/导入了账号，
        旧写法直接 return，列表就永远停在旧数据 —— 表现就是「点了没反应」。
        因此这里把迟到的请求记为**一次待补刷新**，等当前这轮结束后立即再跑一遍，
        并把所有等待中的回调都挂到最后一轮上。
        """
        if self._acct_refreshing:
            # 记下「结束后必须再刷一次」，并把回调并进待办
            self._acct_pending = True
            if on_done is not None:
                self._acct_waiters.append(on_done)
            return

        self._acct_refreshing = True
        waiters = list(self._acct_waiters) + ([on_done] if on_done else [])
        self._acct_waiters = []

        def done(data):
            self._acct_refreshing = False
            self.ui_call(lambda: self._accounts_ready(data, waiters))
            if self._acct_pending:
                # 有人在我们刷新期间又请求过，立刻补一轮（保证拿到最新数据）
                self._acct_pending = False
                self.refresh_accounts()

        C.refresh_accounts_async(on_done=done)

    def _accounts_ready(self, data, on_done):
        try:
            self._refresh_topbar()
            page = self.page
            if page is not None and hasattr(page, "on_accounts_updated"):
                page.on_accounts_updated()
            for fn in (on_done or []):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
            if callable(on_done):
                on_done()
        except Exception:  # noqa: BLE001
            pass

    def _first_render(self):
        self._show("traework", "dashboard")

    # ---------------- 路由 ----------------

    def _switch_module(self, module):
        self.module = module
        self._show(module, "dashboard")

    def show_author(self):
        """弹出作者信息卡片。"""
        try:
            from .ui.author_card import show_author_modal
            show_author_modal(self, self.theme)
        except Exception as e:  # noqa: BLE001
            self.toast("error", f"无法打开作者卡片：{e!r}")

    def _show(self, module, view):
        """切到 (module, view)。★ 页面**复用**而不是每次重建（2026-09-22）。

        改前：每次切换都 `destroy()` 旧页 + `factory()` 新建 → 实测单页
        226~1939 ms（见 `tests/perf_ui.py`）。这是「点一下要等一会儿」的主因，
        而且**第二次进同一页一样慢**（没有缓存）。大头有三块：
          · settings：`_refresh_cred_info()` 同步跑 `tc_checkin._scan_ide_logs()` 约 0.4 s；
          · api-service：`_last_ai_status()` 同步探测本地后端（urllib，超时 2 s）；
          · 每页 150~250 个控件的创建 + Tk 几何计算。
        改后：同一 (模块, 视图) 只建一次，再切回来只跑 `on_show()`（刷新数据）。

        ★ 主题切换必须整体失效（见 `apply_theme`）：复用旧页会让新配色不生效。
        ★ 页面生命周期保持不变：切走时仍然调 `on_hide()`（各页在里面停定时器 /
          轮询），只是不再 `destroy()`。
        """
        self.module = module
        self.view = view
        if view in ("logs", "settings", "api-service"):
            self.sidebar.select(module, view)

        key = (module, view)
        page = self._page_cache.get(key)
        if page is not None and not _alive(page):
            # 缓存里的是已被销毁的残骸（例如主题切换时被清掉）→ 丢弃重建
            self._page_cache.pop(key, None)
            page = None

        # 旧页「隐藏」而不是销毁：先让它收尾（停定时器），再从布局里摘掉
        if self.page is not None and self.page is not page:
            try:
                if hasattr(self.page, "on_hide"):
                    self.page.on_hide()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.page.pack_forget()
            except Exception:  # noqa: BLE001
                pass

        if page is None:
            factory = self._page_factory(view)
            try:
                page = factory(self.page_host, self.theme, self, module)
            except Exception as e:  # noqa: BLE001
                import traceback
                tb = traceback.format_exc()
                box = tk.Frame(self.page_host, bg=self.theme.bg)
                box.pack(fill="both", expand=True, padx=20, pady=20)
                tk.Label(box, text=f"页面加载失败：{e!r}",
                         font=self.theme.f_card_title, fg=self.theme.red_fg,
                         bg=self.theme.bg, anchor="w").pack(fill="x")
                txt = tk.Text(box, width=1, font=self.theme.f_mono,
                              bg=self.theme.bg_soft, fg=self.theme.fg, relief="flat")
                txt.insert("1.0", tb)
                txt.pack(fill="both", expand=True, pady=6)
                self.page = box
                self._refresh_topbar()
                return
            self._page_cache[key] = page
            try:
                page.pack(fill="both", expand=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                if hasattr(page, "on_show"):
                    page.on_show()
            except Exception:  # noqa: BLE001
                pass
        else:
            # 命中缓存：重新挂回布局（此前被 pack_forget 摘掉），再刷新数据
            try:
                page.pack(fill="both", expand=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                if hasattr(page, "on_show"):
                    page.on_show()
            except Exception:  # noqa: BLE001
                pass

        self.page = page
        self._refresh_topbar()

    def drop_page_cache(self, destroy=False):
        """让页面缓存整体失效（主题切换 / 需要强制重建时调用）。

        `destroy=True` 时连控件一起销毁（换主题必须这样，否则旧配色的
        控件会留在内存里且再也用不上）。
        """
        cache = getattr(self, "_page_cache", None)
        if not cache:
            return
        for p in list(cache.values()):
            if destroy:
                try:
                    p.destroy()
                except Exception:  # noqa: BLE001
                    pass
        cache.clear()

    def _page_factory(self, view):
        from .pages import (AccountsPage, ApiServicePage, CheckinPage,
                            CreditsPage, DashboardPage, LogsPage, SettingsPage)
        table = {
            "dashboard": DashboardPage,
            "checkin": CheckinPage,
            "accounts": AccountsPage,
            "credits": CreditsPage,
            "api-service": ApiServicePage,
            "logs": LogsPage,
            "settings": SettingsPage,
        }
        return table.get(view, DashboardPage)

    # ---------------- 顶栏状态 ----------------

    def _refresh_topbar(self):
        try:
            cfg = C.load_config()
            state = C.load_state()
            today = C.today_str()
            module = self.module
            specs = []

            if module in ("traework", "traecn", "workbuddy"):
                # 只读缓存，无缓存时不阻塞（账号数稍后由 on_accounts_updated 补齐）
                all_acc = C.peek_accounts()
                accs = (all_acc or {}).get(module, []) if all_acc else []
                done = sum(1 for a in accs if state.get(a["key"]) == today)
                total = len(accs)
                label = MODULE_CN.get(module, module)
                if all_acc is None:
                    specs.append(("accounts", f"{label} 账号读取中…", "slate", "◍"))
                    specs.append(("today", "今日状态读取中…", "slate", "◷"))
                else:
                    specs.append(("accounts", f"{label} {total} 个账号",
                                  "green" if total else "slate", "◍"))
                    specs.append(("today", f"今日已签 {done}/{total}",
                                  "green" if total and done == total else
                                  ("blue" if total else "slate"), "◷"))
                # 令牌临期提醒
                warned = 0
                import time as _t
                for a in accs:
                    exp = a.get("expires_at") or a.get("exp") or 0
                    if exp:
                        ms = exp if exp > 1e12 else exp * 1000
                        if 0 < (ms - _t.time() * 1000) < 7 * 86400_000:
                            warned += 1
                if warned:
                    specs.append(("warn", f"{warned} 个令牌临期", "amber", "⚠"))

            # 计划任务与网络状态（所有模块通用）
            # 只读缓存：没有缓存时显示"读取中"，后台刷新完会再回来更新，
            # 绝不在这里同步调 PowerShell（那是 1s 级阻塞，切页会卡死）。
            info = scheduler.cached_task_info()
            if info is None:
                specs.append(("task", "计划任务读取中…", "slate", "◌"))
            elif info.get("installed"):
                specs.append(("task", "计划任务已启用", "green", "✓"))
            else:
                specs.append(("task", "计划任务未安装", "slate", "○"))
            specs.append(("net", "网络正常" if self._net_ok else "网络离线",
                          "green" if self._net_ok else "red", "⇅"))

            self.topbar.set_badges(specs)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 交互 ----------------

    def toast(self, kind, text, ms=3200):
        try:
            self.toaster.push(kind, text, ms)
        except Exception:  # noqa: BLE001
            pass

    def toggle_theme(self):
        self.dark = not self.dark
        self.set_theme(self.dark, persist=True)

    def set_theme(self, dark, persist=False):
        self.dark = bool(dark)
        if persist:
            try:
                cfg = C.load_config()
                cfg.setdefault("options", {})["dark_theme"] = self.dark
                C.save_config(cfg)
            except Exception:  # noqa: BLE001
                pass
        self.apply_theme()

    def apply_theme(self):
        """重建主题：换令牌表并把当前页整体重绘。

        各步骤独立容错：任一组件在重建窗口期内已销毁都不应中断换肤。
        """
        self.theme = Theme(self.dark)
        t = self.theme
        self.configure(bg=t.bg)
        for part, fn in (("sidebar", self.sidebar.refresh),
                         ("titlebar", self.titlebar.refresh),
                         ("topbar", self.topbar.refresh)):
            try:
                fn(t)
            except Exception:  # noqa: BLE001
                pass
        try:
            if getattr(self, "_author_btn", None) is not None:
                self._author_btn.refresh(t)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.toaster.retheme(t)
        except Exception:  # noqa: BLE001
            pass
        for w in self.page_host.winfo_children():
            try:
                w.destroy()
            except Exception:  # noqa: BLE001
                pass
        # ★ 缓存必须整体作废：下面那轮 destroy 已经把（可能被 pack_forget 的）
        #   缓存页也销毁了，留着引用只会拿到残骸；而且旧配色的控件绝不能再复用。
        self.drop_page_cache(destroy=False)
        self.page = None
        # 按当前位置重建页面，保证新主题完整生效
        self._show(self.module, self.view)

    def quick_checkin(self, _retried=False):
        """顶栏 / 托盘的「一键签到」：**三个平台、全部账号**，跳过今日已签。

        历史行为（2.4.8 及以前）：只切到「当前模块」的签到页并以
        `platforms=[self.module]` 运行 —— 也就是说，用户在 TraeWork 页点
        「一键签到」，TraeCN / WorkBuddy 的账号**一个都不会被签到**。
        这与按钮名字给人的预期完全不符，所以 2.4.9 起改成真·全局：
        切到签到页 → 范围置为「全部平台」→ 执行（每个平台各自跳过今日已签）。
        界面上的范围选择器会同步显示为「全部平台」，用户看得到发生了什么。
        """
        # 账号缓存还没就绪时，先等它读完再自动重来一次（只重试一次，避免打转）
        if C.peek_accounts() is None:
            if not _retried:
                self.toast("info", "正在读取账号，读完后会自动开始签到…")
                self.refresh_accounts(
                    on_done=lambda: self.after(150, lambda: self.quick_checkin(True)))
            else:
                self.toast("warn", "账号仍在读取中，请稍后再点一次")
            return
        self._show(self.module, "checkin")
        page = self.page
        if hasattr(page, "start_checkin"):
            # ★ 先明确告知「已有任务在跑」（2026-09-24）：原先这里把请求直接丢给
            #   `start_checkin`，而它在 busy 时是**静默 return** 的 ——
            #   顶栏/托盘点了「一键签到」却毫无反应，像是坏了。
            if getattr(page, "busy", False):
                self.toast("info", "本次签到还在执行中，等它跑完即可；"
                                   "要中止请在签到页点「停止」")
                return
            self.after(120, lambda: page.start_checkin(skip_done=True, scope="all"))

    def goto(self, module, view):
        self.sidebar.select(module, view)
        self._show(module, view)

    def _on_close(self):
        cfg = C.load_config()
        running = self.page is not None and getattr(self.page, "busy", False)
        msg = "确认退出签到助手？"
        if running:
            msg = "当前有签到任务正在执行，退出将中断任务。\n确认退出？"
        from .ui.widgets import Modal, button
        m = Modal(self, self.theme, "退出应用", width=420, height=170)
        tk.Label(m.content, text=msg, font=self.theme.f_small, fg=self.theme.fg_muted,
                 bg=self.theme.bg_soft, justify="left", anchor="w",
                 wraplength=360).pack(fill="x")
        button(m.footer, self.theme, "取消", command=m.close, variant="outline").pack(
            side="right", padx=(6, 0))
        button(m.footer, self.theme, "确认退出", command=self.destroy,
               variant="danger").pack(side="right")
        # 「有任务正在执行」时 `msg` 是两行（多一个 \n），自然高度 175 > 弹窗
        # 固定高度 170 → footer 只分到 34px（需要 39），「取消 / 确认退出」的
        # 下沿被切掉 5px。定尺寸弹窗 + 变长文案的老问题，按内容自然高度
        # **只增不减**地长高即可；空闲态自然高度 158 < 170，完全不动。
        m.fit_height()
        m.show()

    def destroy(self):
        # 标记关闭中，后续后台线程回调一律丢弃，避免 invalid command name
        self._closing = True
        # 托盘图标必须显式移除，否则退出后通知区域会残留一个死图标
        try:
            if getattr(self, "tray", None) is not None:
                self.tray.stop()
                self.tray = None
        except Exception:  # noqa: BLE001
            pass
        for attr in ("_uiq_id", "_resize_id"):
            tid = getattr(self, attr, None)
            if tid:
                try:
                    self.after_cancel(tid)
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, attr, None)
        try:
            page = self.page
            if page is not None and hasattr(page, "on_hide"):
                page.on_hide()
        except Exception:  # noqa: BLE001
            pass
        super().destroy()

    # ---------------- 尺寸变化 ----------------

    def _on_resize(self, event=None):
        """窗口尺寸变化 → 通知当前页重排（去抖）。

        这是「最大化后有些板块看不见」的另一半修法：
        TitleBar 在最大化后主动触发，这里同时兜住用户手动拖拽缩放的场景。
        """
        if getattr(self, "_closing", False):
            return
        # 只关心顶层窗口自己的 Configure
        if event is not None and getattr(event, "widget", None) is not self:
            return
        if getattr(self, "_resize_id", None) is not None:
            return
        try:
            self._resize_id = self.after(80, self._do_resize)
        except Exception:  # noqa: BLE001
            pass

    def _do_resize(self):
        self._resize_id = None
        if getattr(self, "_closing", False):
            return
        page = self.page
        if page is not None and hasattr(page, "on_geometry"):
            try:
                page.on_geometry()
            except Exception:  # noqa: BLE001
                pass


def main():
    app = App()
    app.mainloop()
