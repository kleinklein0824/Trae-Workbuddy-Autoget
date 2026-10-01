#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号管理页：自动发现本机账号（首选）+ 手动补充。

三个平台都以 WorkBuddy 那套「捕获本机登录态」为默认路径：
* TraeWork：扫描 profiles/traework 下的会话目录，一键纳管（无需手填名字）
* WorkBuddy：读取桌面端当前登录态 + 已保存快照，一键保存/刷新
* TraeCN  ：IDE 日志自动出现，页面只做展示与令牌查看

手动「新增账号」入口保留，作为 TraeWork 需要提前建空会话时的补充手段。
"""
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk

from .. import config as C
from ..services import client_launcher as CL
from ..ui import widgets as W
from .base import BasePage

MODULE_CN = {"traework": "TraeWork", "traecn": "TraeCN", "workbuddy": "WorkBuddy"}


class AccountsPage(BasePage):
    title = "账号管理"

    def build(self):
        t = self.theme
        self.set_title(f"{MODULE_CN.get(self.module, '')} 账号管理", self._desc_text())
        self.busy = False
        self._found = []          # 自动发现结果（后台填充）
        self._client_info = None  # 客户端定位结果（后台填充）
        # 「打开客户端 → 登录 → 自动捕获」轮询状态。
        # gen 是代次：只有代次相同的那一轮才允许回填 UI，
        # 否则「连点两次」或「切页再回来」会让旧线程覆盖新状态。
        self._watch = {"gen": 0, "stop": True, "base": set(), "start": 0.0}
        self._opening = False

        # 主操作按钮：三个平台都以「发现/捕获本机账号」为首选
        if self.module == "traework":
            self.add_action("扫描本机会话", self._scan_local, variant="primary", icon="⟳")
        elif self.module == "workbuddy":
            self.add_action("捕获当前账号", self._capture_wb, variant="primary", icon="＋")
            # ★ 找回历史账号（2026-09-25 新增）：所有**捕获过**的账号都会自动进
            #   归档（只增不减），丢失时可从这里一键找回。按钮上带可找回数量。
            try:
                import wb_checkin as _WB
                _rec = len(_WB.recoverable_accounts())
            except Exception:  # noqa: BLE001
                _rec = 0
            self._recover_btn = self.add_action(
                "找回历史账号" + (f"（{_rec}）" if _rec else ""),
                self._recover_wb, variant="outline", icon="⟲")
        elif self.module == "traecn":
            self.add_action("重新扫描 IDE 令牌", self._rescan_tc, variant="primary", icon="⟳")
        self.add_action("刷新", self.reload, variant="outline", icon="⟳")
        # 「打开客户端」——「自动跳转对应软件引导登录」的入口
        self.add_action(self._open_label(), self._open_client,
                        variant="outline", icon="↗")

        # 登录引导卡排在最前：这是用户最需要先看到的一步
        self.guide_box = tk.Frame(self.body, bg=t.bg)
        self.guide_box.pack(fill="x")
        self.hint_box = tk.Frame(self.body, bg=t.bg)
        self.hint_box.pack(fill="x", pady=(12, 0))
        self.discover_box = tk.Frame(self.body, bg=t.bg)
        self.discover_box.pack(fill="x", pady=(12, 0))
        self.list_box = tk.Frame(self.body, bg=t.bg)
        self.list_box.pack(fill="x", pady=(12, 0))
        # 手动新增入口单独用一个容器，reload() 里一并清空，
        # 否则每刷新一次就会多堆一个按钮（Tkinter 不会自动回收）。
        self.add_box = tk.Frame(self.body, bg=t.bg)
        self.add_box.pack(fill="x", pady=(10, 0))

        self.on_show()

    def _desc_text(self):
        return {
            "traework": "识别本机 Trae 客户端（IDE / 桌面端）登录的账号，并纳管网页版会话",
            "workbuddy": "捕获桌面端当前登录态为令牌快照，支持多账号切换后逐个捕获",
            "traecn": "令牌自动从 Trae 客户端（IDE / 桌面端）日志中读取，打开一次软件即可续期",
        }.get(self.module, "")

    def on_show(self):
        self.reload()
        self._render_guide()
        # 客户端定位要查注册表 / 开始菜单（首次约 1~2 秒）——
        # **绝不能放在 UI 渲染路径上**（本项目已有先例：每次起 PowerShell ≈1.1s
        # 直接拖垮页面）。这里先渲染占位卡，再后台探测回填。
        self.after(120, self._probe_client)
        self.after(140, self._scan_local_quiet)

    def on_accounts_updated(self):
        """账号缓存后台刷新完成后重建列表。"""
        self.reload()

    # ---------------- 客户端登录引导 ----------------

    _OPEN_LABEL = {
        "traework": "打开桌面端",
        "traecn": "打开 Trae CN",
        "workbuddy": "打开 WorkBuddy",
    }

    def _open_label(self):
        return self._OPEN_LABEL.get(self.module, "打开客户端")

    def _probe_client(self):
        """后台探测客户端安装位置（注册表 / 开始菜单 / 磁盘，可能耗时）。"""
        def work():
            try:
                info = CL.diagnose(self.module)
            except Exception as e:  # noqa: BLE001
                info = {"found": False, "path": None, "source": None,
                        "detail": repr(e), "running": False, "pids": [],
                        "label": "", "guide": "", "hints": []}
            self.app.ui_call(lambda: self._client_done(info))

        threading.Thread(target=work, daemon=True, name="acct-client").start()

    def _client_done(self, info):
        self._client_info = info
        self._render_guide()

    def _render_guide(self):
        """渲染「登录引导」卡：客户端在哪 + 怎么登录 + 一键打开。"""
        for w in self.guide_box.winfo_children():
            w.destroy()
        t = self.theme
        info = getattr(self, "_client_info", None)

        card = W.Card(self.guide_box, t, accent="blue", padding=12)
        card.pack(fill="x")

        head = tk.Frame(card.body, bg=t.bg_soft)
        head.pack(fill="x")
        tk.Label(head, text="◈ 登录引导", font=t.f_body, fg=t.fg,
                 bg=t.bg_soft, anchor="w").pack(side="left")
        if info is None:
            W.Badge(head, t, "正在检测…", "slate").pack(side="left", padx=(8, 0))
        elif info.get("running"):
            W.Badge(head, t, "客户端运行中", "green").pack(side="left", padx=(8, 0))
        elif info.get("found"):
            W.Badge(head, t, "已定位客户端", "green").pack(side="left", padx=(8, 0))
        else:
            W.Badge(head, t, "未找到客户端", "amber").pack(side="left", padx=(8, 0))

        # 路径 / 状态行。★ 长路径必须 auto_wrap：tk.Label 默认 wraplength=0
        # 不换行，超出部分会被卡片右边缘**静默裁掉**。
        if info is not None:
            text = (info.get("path") if info.get("found")
                    else info.get("detail")) or ""
            if text:
                sub = tk.Label(card.body, text=text, font=t.f_tiny,
                               fg=t.fg_dim, bg=t.bg_soft, anchor="w",
                               justify="left")
                sub.pack(fill="x", pady=(4, 0))
                W.auto_wrap(sub)

        guide = tk.Label(card.body, text=CL.guide_text(self.module),
                         font=t.f_small, fg=t.fg_muted, bg=t.bg_soft,
                         anchor="w", justify="left")
        guide.pack(fill="x", pady=(6, 0))
        W.auto_wrap(guide)

        btns = tk.Frame(card.body, bg=t.bg_soft)
        btns.pack(fill="x", pady=(8, 0))
        watching = self._watching()
        W.button(btns, t, f"{self._open_label()}并登录",
                 command=self._open_client, variant="primary",
                 small=True).pack(side="left")
        if self.module == "traework":
            W.button(btns, t, "打开网页版", command=self._open_web,
                     variant="outline", small=True).pack(side="left", padx=(6, 0))
        if watching:
            W.button(btns, t, "停止等待", command=self._stop_watch,
                     variant="ghost", small=True).pack(side="left", padx=(6, 0))
        elif info is not None:
            W.button(btns, t, "重新检测", command=self._probe_client,
                     variant="ghost", small=True).pack(side="left", padx=(6, 0))

        # 等待登录的进度（静态文案，不做每秒重绘：本项目的 UI 红线里
        # 明确禁止在高频路径上重建控件，否则会闪、会卡）
        if watching:
            w = self._watch
            deadline = time.strftime("%H:%M:%S",
                                     time.localtime(w["start"] + self.WATCH_TIMEOUT))
            lbl = tk.Label(card.body,
                           text=f"⏳ 正在等待登录…（已等待 "
                                f"{int(time.time() - w['start'])} 秒，"
                                f"最多等到 {deadline}）\n"
                                f"检测到新登录会**自动保存**，不需要手动点按钮。",
                           font=t.f_tiny, fg=t.tone("blue")[0], bg=t.bg_soft,
                           anchor="w", justify="left")
            lbl.pack(fill="x", pady=(6, 0))
            W.auto_wrap(lbl)
            if w.get("note"):
                nlbl = tk.Label(card.body, text="· " + w["note"],
                                font=t.f_tiny, fg=t.tone("amber")[0],
                                bg=t.bg_soft, anchor="w", justify="left")
                nlbl.pack(fill="x", pady=(4, 0))
                W.auto_wrap(nlbl)

        for hint in (info or {}).get("hints") or []:
            lbl = tk.Label(card.body, text="· " + hint, font=t.f_tiny,
                           fg=t.tone("amber")[0], bg=t.bg_soft, anchor="w",
                           justify="left")
            lbl.pack(fill="x", pady=(4, 0))
            W.auto_wrap(lbl)

    def _watching(self):
        w = getattr(self, "_watch", None) or {}
        return bool(w.get("gen")) and not w.get("stop")

    def _stop_watch(self):
        self._watch["stop"] = True
        self._render_guide()

    def _open_client(self):
        """拉起对应客户端 → 引导登录 → **自动捕获**（闭环）。

        为什么要做成闭环：用户点完「打开客户端」后，注意力全在客户端窗口上，
        再让他回来手动点「捕获/扫描」这一步最容易漏 —— 漏了就表现为
        「账号管理里还是空的」。所以打开成功后自动开始轮询登录态。
        """
        if self._opening:
            return
        self._opening = True

        def work():
            try:
                ok, msg, _path = CL.launch(self.module)
            except Exception as e:  # noqa: BLE001
                ok, msg = False, repr(e)
            self.app.ui_call(lambda: self._opened(ok, msg))

        threading.Thread(target=work, daemon=True, name="acct-open").start()

    def _opened(self, ok, msg):
        self._opening = False
        self.app.toast("success" if ok else "error", msg)
        self._probe_client()
        if ok:
            self._start_watch()          # 只有真的打开了才进入等待闭环
        else:
            self._render_guide()

    def _open_web(self):
        ok, msg = CL.open_web(self.module)
        self.app.toast("success" if ok else "error", msg)

    # ---------------- 「登录 → 自动捕获」轮询 ----------------

    WATCH_TIMEOUT = 300     # 最多等 5 分钟（人工登录需要时间）
    WATCH_INTERVAL = 4      # 轮询间隔（秒）

    def _baseline(self):
        """当前「已保存 / 已识别」的账号 key 集合，作为「新登录」的判据基线。"""
        try:
            if self.module == "workbuddy":
                import wb_checkin
                return set(wb_checkin._load_saved().keys())
            if self.module == "traecn":
                import tc_checkin
                return set(tc_checkin._load_cache().keys())
            if self.module == "traework":
                return {a.get("profile") or a.get("name")
                        for a in C.load_config().get("traework", {})
                        .get("accounts", [])}
        except Exception:  # noqa: BLE001  基线取不到就当作空集（宁可多捕获一次）
            pass
        return set()

    def _probe_new_login(self):
        """检测基线之外是否出现了新登录。返回 ``(items, 说明)``。

        全部走**本地**数据源，绝不发网络请求 —— 这里每 4 秒跑一次，
        若调用会查昵称的 ``collect_accounts``（约 10~15 秒 + 网络），
        轮询本身就会变成负担。
        """
        base = set(self._watch.get("base") or ())
        try:
            if self.module == "workbuddy":
                import wb_checkin
                cur = wb_checkin._read_current_auth()
                if cur and cur["uid"] not in base:
                    return [cur], (cur.get("nickname") or cur["uid"][:8])
                return [], ""
            if self.module == "traecn":
                import tc_checkin
                fresh = tc_checkin._scan_ide_logs(lambda m: None)
                new = {u: v for u, v in fresh.items() if u not in base}
                if new:
                    return list(new.items()), f"{len(new)} 个新令牌"
                return [], ""
            if self.module == "traework":
                found = C.discover_accounts("traework")
                new = [f for f in found
                       if f.get("source") != "client" and f.get("logged_in")
                       and (f.get("profile") or f.get("id")) not in base]
                if new:
                    return new, "、".join(
                        (f.get("display") or "") for f in new[:2])
                return [], ""
        except Exception:  # noqa: BLE001  单次探测失败不该终止整个等待
            return [], ""
        return [], ""

    def _already_known_hint(self):
        """当前登录**已经在列表里**时给出提示（否则用户会白等 5 分钟）。"""
        try:
            if self.module == "workbuddy":
                import wb_checkin
                cur = wb_checkin._read_current_auth()
                if cur and cur["uid"] in wb_checkin._load_saved():
                    return ("本机当前登录的「%s」已在账号列表中；"
                            "若要新增其他账号，请在客户端里切换登录，"
                            "程序会自动捕获新账号。"
                            % (cur.get("nickname") or cur["uid"][:8]))
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _start_watch(self):
        """开始轮询「是否出现新登录」。"""
        self._watch = {
            "gen": int(self._watch.get("gen", 0)) + 1,
            "base": self._baseline(),
            "start": time.time(),
            "stop": False,
            "note": self._already_known_hint(),
        }
        gen = self._watch["gen"]
        self._render_guide()

        def work():
            while True:
                time.sleep(self.WATCH_INTERVAL)
                w = self._watch
                if w.get("stop") or w.get("gen") != gen:
                    return
                if time.time() - w["start"] > self.WATCH_TIMEOUT:
                    self.app.ui_call(lambda g=gen: self._watch_timeout(g))
                    return
                items, desc = self._probe_new_login()
                if items:
                    self.app.ui_call(
                        lambda i=items, d=desc, g=gen: self._watch_found(g, i, d))
                    return

        threading.Thread(target=work, daemon=True, name="acct-watch").start()

    def _watch_found(self, gen, items, desc):
        if self._watch.get("gen") != gen:
            return
        # ★ 不能因为 busy 就把这次捕获**丢掉**：`_run_bg` 在 busy 时只弹一句
        #   「已有操作正在进行中」然后 return，于是自动捕获被静默吞掉，
        #   界面停在旧列表上 —— 这正是本项目踩过的「单飞守卫吞请求」。
        #   改成稍后重试（ui_call 保证我们已经在主线程，after 是安全的）。
        if self.busy:
            self.after(800, lambda g=gen, i=items, d=desc:
                       self._watch_found(g, i, d))
            return
        self._watch["stop"] = True
        self._render_guide()
        self._run_bg(lambda: self._adopt_items(items),
                     done_msg=f"已自动捕获：{desc}" if desc else None,
                     err_prefix="自动保存失败")

    def _watch_timeout(self, gen):
        if self._watch.get("gen") != gen:
            return
        self._watch["stop"] = True
        self._render_guide()
        self.app.toast("info", "等待登录超时（%d 分钟）。登录完成后点「刷新」"
                               "或重新点「%s并登录」即可。"
                       % (self.WATCH_TIMEOUT // 60, self._open_label()))

    def _adopt_items(self, items):
        """把检测到的新登录落盘。返回 ``(ok, msg)``。

        ★ 复用与手动按钮**完全相同的落盘函数**（capture_current /
          record_token / adopt_traework），不另造一套 ——
          否则自动路径与手动路径会慢慢分叉，出现「手动能存、自动存不上」。
        """
        if self.module == "workbuddy":
            import wb_checkin
            if not wb_checkin.capture_current(lambda m: None):
                return False, "未读取到 WorkBuddy 登录态"
            nick = ""
            if items:
                nick = items[0].get("nickname") or items[0].get("uid", "")[:8]
            return True, f"已保存账号「{nick}」的令牌快照"
        if self.module == "traecn":
            import tc_checkin
            n = 0
            for uid, val in items:
                try:
                    token, exp = val[0], val[1]
                except (TypeError, IndexError, KeyError):
                    continue
                if tc_checkin.record_token(uid, token, exp, None, lambda m: None):
                    n += 1
            if not n:
                return False, "检测到新登录，但令牌写入失败"
            return True, f"已保存 {n} 个 TraeCN 令牌"
        if self.module == "traework":
            ok_n, fails = 0, []
            for f in items:
                ok, msg = C.adopt_traework(f.get("profile") or f.get("id"),
                                           f.get("display"))
                if ok:
                    ok_n += 1
                else:
                    fails.append(msg)
            if not ok_n:
                return False, (fails[0] if fails else "没有可纳入的账号")
            return True, f"已纳入 {ok_n} 个网页版会话"
        return False, "未知平台"

    # ---------------- 自动发现 ----------------

    def _scan_local_quiet(self):
        """进入页面时静默扫一次（不弹提示），结果填入发现区。"""
        self._scan_local(silent=True)

    def _scan_local(self, silent=False):
        """后台扫描本机账号来源，完成后重建「发现」区。"""
        if getattr(self, "_scanning", False):
            return
        self._scanning = True

        def work():
            try:
                found = C.discover_accounts(self.module)
            except Exception as e:  # noqa: BLE001
                found = []
                self.app.ui_call(lambda: self.app.toast("error", f"扫描失败：{e!r}"))
            self.app.ui_call(lambda: self._scan_done(found, silent))

        threading.Thread(target=work, daemon=True, name="acct-scan").start()

    def _scan_done(self, found, silent):
        self._scanning = False
        self._found = found or []
        self._render_discover()
        if not silent:
            n_new = sum(1 for f in self._found
                        if not f.get("in_config") and f.get("logged_in"))
            if not self._found:
                self.app.toast("info", "未在本机发现可用的账号来源")
            elif n_new:
                self.app.toast("success", f"发现 {n_new} 个尚未纳入的账号")
            else:
                self.app.toast("info", f"发现 {len(self._found)} 个账号，均已在列表中")

    def _render_discover(self):
        for w in self.discover_box.winfo_children():
            w.destroy()
        t = self.theme
        found = self._found
        if not found:
            return

        # 两类来源分开：本机客户端（只读识别）在前，Playwright 会话（可纳入）在后
        clients = [f for f in found if f.get("source") == "client"]
        sessions = [f for f in found if f.get("source") != "client"]
        pending = [f for f in sessions
                   if f.get("logged_in") and not f.get("in_config")]

        card = W.Card(self.discover_box, t, accent="green", padding=12)
        card.pack(fill="x")

        head = tk.Frame(card.body, bg=t.bg_soft)
        head.pack(fill="x")
        tk.Label(head, text="◈ 本机发现的账号", font=t.f_body,
                 fg=t.fg, bg=t.bg_soft, anchor="w").pack(side="left")
        if sessions:
            W.Badge(head, t, f"待纳入 {len(pending)}" if pending else "已全部纳入",
                    "amber" if pending else "green").pack(side="left", padx=(8, 0))
        else:
            W.Badge(head, t, f"识别到 {len(clients)} 个本机登录",
                    "green").pack(side="left", padx=(8, 0))

        if self.module == "traework" and pending:
            W.button(head, t, "全部纳入", command=self._adopt_all,
                     variant="primary", small=True).pack(side="right")

        rows = clients + sessions
        for i, f in enumerate(rows):
            row = tk.Frame(card.body, bg=t.bg_soft)
            row.pack(fill="x", pady=(8 if i == 0 else 4, 0))
            dot = "●" if f.get("logged_in") else "○"
            tone = "green" if f.get("logged_in") else "slate"
            tk.Label(row, text=dot, font=t.f_small, fg=t.tone(tone)[0],
                     bg=t.bg_soft).pack(side="left", padx=(0, 6))
            info = tk.Frame(row, bg=t.bg_soft)
            info.pack(side="left", fill="x", expand=True)
            tk.Label(info, text=f.get("display") or f.get("id"),
                     font=(t.font_family, 10, "bold"), fg=t.fg,
                     bg=t.bg_soft, anchor="w").pack(anchor="w")
            tk.Label(info, text=f.get("detail") or "", font=t.f_tiny,
                     fg=t.fg_dim, bg=t.bg_soft, anchor="w").pack(anchor="w")

            if f.get("source") == "client":
                # 客户端账号由 TraeCN 通道签到，此处只做识别展示，无需纳入
                W.Badge(row, t, "已识别", "green").pack(side="right")
            elif f.get("in_config"):
                W.Badge(row, t, "已纳入", "slate").pack(side="right")
            elif f.get("logged_in"):
                if self.module == "traework":
                    W.button(row, t, "纳入", command=lambda x=f: self._adopt_one(x),
                             variant="primary", small=True).pack(side="right")
                elif self.module == "workbuddy":
                    W.button(row, t, "保存快照", command=self._capture_wb,
                             variant="primary", small=True).pack(side="right")
            else:
                W.Badge(row, t, "未登录", "amber").pack(side="right")

        # 提示：客户端识别项与网页会话是两条通道，避免用户误以为要重复纳入
        if clients:
            tk.Label(card.body,
                     text="上方带「已识别」的是本机 Trae 客户端（IDE / 桌面端）登录的账号，"
                          "签到由 TraeCN 通道完成，无需纳入；"
                          "下方才是网页版会话，需要点「纳入」。",
                     font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w",
                     justify="left", wraplength=880).pack(fill="x", pady=(8, 0))

    def _adopt_one(self, item):
        def job():
            return C.adopt_traework(item.get("profile") or item.get("id"),
                                    item.get("display"))
        self._run_bg(job, err_prefix="纳入失败")

    def _adopt_all(self):
        todo = [f for f in self._found
                if f.get("logged_in") and not f.get("in_config")]

        def job():
            ok_n, fails = 0, []
            for f in todo:
                ok, msg = C.adopt_traework(f.get("profile") or f.get("id"),
                                           f.get("display"))
                if ok:
                    ok_n += 1
                else:
                    fails.append(msg)
            if ok_n == 0:
                return False, fails[0] if fails else "没有可纳入的账号"
            return True, f"已纳入 {ok_n} 个账号"

        self._run_bg(job, err_prefix="批量纳入失败")

    def reload(self):
        # ★ 输入没变就不重建（2026-09-22）：本页每次 reload 要 destroy + 新建
        #   上百个控件（提示卡 4 段长文案 + 每个账号一行 6~8 个控件 + 手动新增区），
        #   实测缓存命中后仍要 255 ms。进页面时数据通常一模一样。
        #   签名里带上 `_status(a)`，保证「令牌临期」这类徽标仍然会更新；
        #   另外这一改还有个附带好处：手动新增表单里**没提交的输入不会被清掉**。
        accs = self.app.accounts().get(self.module, [])
        try:
            sig = (self.module, bool(self.theme.dark),
                   # ★ 「账号缓存还在读」也是一个渲染输入（空列表会渲染成
                   #   「正在读取账号…」），必须进签名，否则读完以后会被跳过。
                   C.peek_accounts() is None,
                   tuple((a.get("key"), a.get("display"), a.get("uid"),
                          a.get("profile"), a.get("phone"),
                          bool(a.get("logged_in", a.get("has_profile"))),
                          self._status(a)) for a in accs))
        except Exception:  # noqa: BLE001
            sig = None
        if sig is not None and sig == getattr(self, "_list_sig", None):
            return
        self._list_sig = sig
        for box in (self.hint_box, self.list_box, self.add_box):
            for w in box.winfo_children():
                w.destroy()
        t = self.theme

        # ---- 操作提示卡 ----
        card = W.Card(self.hint_box, t, accent="blue", padding=12)
        card.pack(fill="x")
        tips = {
            "traework": [
                "第一步：点上方「登录引导」里的「打开桌面端并登录」（或「打开网页版」），"
                "在打开的软件里完成登录。",
                "点击「扫描本机会话」会同时识别本机 Trae 客户端（Trae CN IDE / TraeWork CN 桌面端）"
                "已登录的账号，以及在网页版登录过的会话。",
                "带「已识别」的客户端账号签到由 TraeCN 通道完成，无需纳入；"
                "带「纳入」的是网页版会话，点一下即可加入列表。",
                "列表中的账号点「登录」可单独打开浏览器窗口；未登录时先登录一次即可。",
            ],
            "workbuddy": [
                "第一步：点上方「登录引导」里的「打开 WorkBuddy 并登录」，"
                "在桌面端完成登录（程序会自动定位安装位置，换设备也不用改配置）。",
                "在 WorkBuddy 桌面端登录账号后，点击「捕获当前账号」保存其令牌。",
                "需要多个账号时：在桌面端切换登录，再次点击捕获即可逐个累积。",
                "★ 每个捕获过的账号都会自动备份（只增不减）；万一丢了，点上方「找回历史账号」即可一键找回。",
            ],
            "traecn": [
                "第一步：点上方「登录引导」里的「打开 Trae CN 并登录」，"
                "在 IDE 里完成登录；打开一次软件令牌即自动续期。",
                "令牌来源为 Trae 系客户端（Trae CN IDE / TraeWork CN 桌面端）的运行日志，无需手动填写。",
                "若列表为空或提示过期，打开一次 Trae 客户端并登录后重新扫描即可。",
            ],
        }.get(self.module, [])
        for i, tip in enumerate(tips):
            tk.Label(card.body, text="• " + tip, font=t.f_small, fg=t.fg_muted,
                     bg=t.bg_soft, anchor="w", justify="left",
                     wraplength=880).pack(fill="x", pady=(0 if i == 0 else 3, 0))

        # ---- 账号列表 ----
        if not accs:
            loading = C.peek_accounts() is None
            W.EmptyState(self.list_box, t, "◍",
                         "正在读取账号…" if loading else "暂无账号",
                         "账号信息会在后台读取完成后自动出现。"
                         if loading else self._empty_hint()).pack(fill="x")
        else:
            lcard = W.Card(self.list_box, t, padding=0)
            lcard.pack(fill="x")
            box = lcard.body
            for i, a in enumerate(accs):
                if i:
                    W.hline(box, t).pack(fill="x")
                self._render_row(box, a)

        # ---- 手动新增入口（仅 TraeWork；自动扫描是主路径，这里作为补充）----
        if self.module == "traework":
            W.button(self.add_box, t, "＋ 手动新增", command=self._add_traework,
                     variant="outline", small=True).pack(side="left")
            tk.Label(self.add_box,
                     text="一般不需要手填：先点上方「扫描本机会话」。"
                          "仅在需要预留一个空会话时才手动新增。",
                     font=t.f_tiny, fg=t.fg_dim, bg=t.bg).pack(side="left", padx=(10, 0))

    def _empty_hint(self):
        return {
            "traework": "先点上方「登录引导」里的「打开桌面端并登录」，"
                        "登录后回来点「扫描本机会话」自动找出账号，无需手填。",
            "workbuddy": "先点上方「登录引导」里的「打开 WorkBuddy 并登录」，"
                         "登录后回来点「捕获当前账号」。",
            "traecn": "先点上方「登录引导」里的「打开 Trae CN 并登录」，"
                      "登录后回来点「重新扫描 IDE 令牌」。",
        }.get(self.module, "")

    def _render_row(self, box, a):
        t = self.theme
        row = tk.Frame(box, bg=t.bg_soft)
        row.pack(fill="x", padx=14, pady=10)

        # ★ 头像首字母：**必须**先确认是字符串再切片（2026-09-24）。
        #   WorkBuddy 改了本地存储格式后，昵称可能是**加密信封 dict**
        #   （`{'$wbEncrypted': 1, 'envelope': '...'}`）；`dict[:1]` 在
        #   Python 3.12+ 会抛 `KeyError(slice(None, 1, None))`（slice 变可哈希了，
        #   所以不是 TypeError，很容易看懵）→ **整页「页面加载失败」**。
        #   数据层已统一兜底（`wb_checkin.safe_text`），这里再加一道，
        #   保证以后**任何**非字符串昵称都不会把页面搞崩。
        _disp = a.get("display")
        if not isinstance(_disp, str):
            _disp = ""
        avatar = tk.Label(row, text=_disp[:1].upper() or "?",
                          font=(t.font_family, 12, "bold"), fg=t.accent_fg,
                          bg=t.accent, width=2, height=1)
        avatar.pack(side="left", padx=(0, 10))

        info = tk.Frame(row, bg=t.bg_soft)
        info.pack(side="left", fill="x", expand=True)
        tk.Label(info, text=a["display"], font=(t.font_family, 10, "bold"),
                 fg=t.fg, bg=t.bg_soft, anchor="w").pack(anchor="w")
        tk.Label(info, text=self._detail(a), font=t.f_tiny, fg=t.fg_dim,
                 bg=t.bg_soft, anchor="w").pack(anchor="w")

        # 状态徽标
        note = self._status(a)
        if note:
            txt, tone = note
            W.Badge(row, t, txt, tone).pack(side="right", padx=(6, 0))

        # 操作按钮
        if self.module == "traework":
            # 未登录时「登录」用强调色（主操作），已登录时降级为描边。
            # ★ 判据同 _status：logged_in 而非 has_profile。
            _logged = a.get("logged_in", a.get("has_profile"))
            W.button(row, t, "登录", command=lambda x=a: self._login_one(x),
                     variant="outline" if _logged else "primary",
                     small=True).pack(side="right")
            W.button(row, t, "移除", command=lambda x=a: self._remove_traework(x),
                     variant="ghost", small=True).pack(side="right", padx=(0, 4))
        elif self.module == "workbuddy":
            if a.get("source") != "current":
                W.button(row, t, "移除", command=lambda x=a: self._remove_wb(x),
                         variant="ghost", small=True).pack(side="right", padx=(0, 4))
            else:
                W.button(row, t, "重新捕获", command=self._capture_wb,
                         variant="outline", small=True).pack(side="right")
        elif self.module == "traecn":
            W.button(row, t, "查看令牌", command=lambda x=a: self._show_token(x),
                     variant="ghost", small=True).pack(side="right")

    def _detail(self, a):
        if a["platform"] == "workbuddy":
            parts = []
            # ★ phone 也可能是**加密信封 dict**（与 nickname 一起被加密）——
            #   不判类型直接 append，`" · ".join(parts)` 会抛
            #   `TypeError: sequence item 0: expected str instance, dict found`
            #   → 同样让整页打不开。数据层已兜底，这里再防一道。
            _phone = a.get("phone")
            if isinstance(_phone, str) and _phone:
                parts.append(_phone)
            parts.append(f"uid {a['uid'][:8]}…")
            parts.append("本机当前登录" if a.get("source") == "current" else
                         f"于 {a.get('captured_at') or '未知时间'} 捕获")
            return " · ".join(parts)
        if a["platform"] == "traework":
            _logged = a.get("logged_in", a.get("has_profile"))
            return (f"会话目录 profiles/traework/{a.get('profile')} · "
                    + ("已登录" if _logged else "尚未登录"))
        if a["platform"] == "traecn":
            return f"uid {a['uid']} · 设备标识 {'已就绪' if a.get('has_did') else '缺失'}"
        return ""

    def _status(self, a):
        if a["platform"] == "workbuddy" and a.get("expires_at"):
            days = (a["expires_at"] - time.time() * 1000) / 86400000
            if days < 0:
                return ("令牌已过期", "red")
            if days <= 7:
                return (f"{max(1, round(days))} 天到期", "amber")
            return ("有效", "green")
        if a["platform"] == "traecn" and a.get("exp"):
            days = (a["exp"] - time.time()) / 86400
            if days < 0:
                return ("令牌已过期", "red")
            if days <= 7:
                return (f"{max(1, round(days))} 天到期", "amber")
            return ("有效", "green")
        if a["platform"] == "traework":
            # ★ 用 logged_in（Cookies 非空或已有昵称），而不是 has_profile。
            #   只看「目录存在」会把「建了空目录、从没登录过」显示成「已登录」。
            logged = a.get("logged_in", a.get("has_profile"))
            return ("已登录", "green") if logged else ("未登录", "amber")
        return None

    # ---------------- 后台任务封装 ----------------

    def _run_bg(self, fn, done_msg=None, err_prefix="操作失败"):
        """把耗时操作放线程执行，完成后回主线程刷新。"""
        if self.busy:
            self.app.toast("warn", "已有操作正在进行中")
            return
        self.busy = True

        def work():
            try:
                ok, msg = fn()
            except Exception as e:  # noqa: BLE001
                ok, msg = False, repr(e)
            self.app.ui_call(lambda: self._bg_done(ok, msg, done_msg, err_prefix))

        threading.Thread(target=work, daemon=True).start()

    def _bg_done(self, ok, msg, done_msg, err_prefix):
        self.busy = False
        if ok:
            self.app.toast("success", done_msg or msg or "操作完成")
        else:
            self.app.toast("error", f"{err_prefix}：{msg}")
        # 账号已变更，强制刷新缓存后再重建列表
        self.app.refresh_accounts(on_done=self.reload)
        self.app._refresh_topbar()

    # ---------------- TraeWork ----------------

    def _add_traework(self):
        t = self.theme
        m = W.Modal(self.app, t, "手动新增 TraeWork 账号", width=470, height=270)
        tk.Label(m.content, text="账号名（用于区分多个账号，仅本机可见）",
                 font=t.f_small, fg=t.fg_muted, bg=t.bg_soft, anchor="w").pack(fill="x")
        entry = tk.Entry(m.content, font=t.f_body, bg=t.bg_input, fg=t.fg,
                         relief="flat", highlightthickness=1,
                         highlightbackground=t.border, highlightcolor=t.accent)
        entry.pack(fill="x", pady=(4, 0), ipady=5)
        entry.insert(0, f"trae{len(self.app.accounts().get('traework', [])) + 1}")
        tk.Label(m.content,
                 text="提示：如果本机已经登录过 TraeWork，直接点「扫描本机会话」"
                      "即可自动纳入，不用手填。",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w",
                 justify="left", wraplength=420).pack(fill="x", pady=(8, 0))

        def submit():
            name = entry.get().strip()
            if not name:
                self.app.toast("warn", "账号名不能为空")
                return
            cfg = C.load_config()
            accs = cfg.setdefault("traework", {}).setdefault("accounts", [])
            if any((a.get("name") or a.get("profile")) == name for a in accs):
                self.app.toast("warn", f"账号 {name} 已存在")
                return
            accs.append({"name": name, "profile": name})
            C.save_config(cfg)
            m.close()
            self.app.toast("success", f"已新增账号 {name}，请点击「登录」完成首次登录")
            # 改的是 config，本地缓存还是旧的 —— 必须强制刷新，
            # 否则 reload() 读到的仍是旧快照，看起来就是「点了没反应」。
            self.app.refresh_accounts(on_done=self.reload)

        W.button(m.footer, t, "取消", command=m.close, variant="outline").pack(
            side="right", padx=(6, 0))
        W.button(m.footer, t, "添加", command=submit, variant="primary").pack(side="right")
        m.show()
        entry.focus_set()

    def _remove_traework(self, acc):
        t = self.theme
        m = W.Modal(self.app, t, "移除账号", width=430, height=200)
        tk.Label(m.content,
                 text=f"确认移除 TraeWork 账号「{acc['display']}」？\n"
                      f"会话目录 profiles/traework/{acc.get('profile')} 将一并删除，"
                      f"下次使用需重新登录。",
                 font=t.f_small, fg=t.fg_muted, bg=t.bg_soft, justify="left",
                 anchor="w", wraplength=380).pack(fill="x")

        def do():
            cfg = C.load_config()
            accs = cfg.get("traework", {}).get("accounts", [])
            cfg["traework"]["accounts"] = [
                a for a in accs
                if (a.get("name") or a.get("profile")) != acc["id"]]
            C.save_config(cfg)
            # 清理独立会话目录（仅限本项目 profiles 下的该账号目录）
            prof = os.path.join(C.TW_PROFILES, acc.get("profile") or acc["id"])
            if os.path.isdir(prof) and os.path.abspath(prof).startswith(
                    os.path.abspath(C.TW_PROFILES)):
                import shutil
                shutil.rmtree(prof, ignore_errors=True)
            m.close()
            self.app.toast("success", f"已移除账号 {acc['display']}")
            # 同步清理 state 中的日期记录
            st = C.load_state()
            if acc["key"] in st:
                st.pop(acc["key"], None)
                try:
                    with open(C.STATE_PATH, "w", encoding="utf-8") as f:
                        json.dump(st, f, ensure_ascii=False, indent=2)
                except OSError:
                    pass
            self.app.refresh_accounts(on_done=self.reload)

        W.button(m.footer, t, "取消", command=m.close, variant="outline").pack(
            side="right", padx=(6, 0))
        W.button(m.footer, t, "确认移除", command=do, variant="danger").pack(side="right")
        # 文案里含用户可控的展示名与 profile 名，折行会顶高内容 →
        # 见 `Modal.fit_height()` 的注释（定尺寸弹窗 + 变长内容 = 静默裁掉）
        m.fit_height()
        m.show()

    def _login_traework(self):
        """批量入口：为第一个尚未登录的账号打开浏览器（保留兼容）。"""
        accs = self.app.accounts().get("traework", [])
        if not accs:
            self.app.toast("warn", "请先扫描本机会话或手动新增一个账号")
            return
        target = next((a for a in accs if not a.get("has_profile")), accs[0])
        self._login_one(target)

    def _login_one(self, acc):
        def job():
            import checkin
            cfg = checkin.load_config()
            target = next((a for a in cfg.get("traework", {}).get("accounts", [])
                           if (a.get("name") or a.get("profile")) == acc["id"]), None)
            if target is None:
                return False, "配置中未找到该账号"
            import tw_checkin
            tw_checkin.interactive_login(cfg, target, lambda m: None)
            return True, f"账号 {acc['display']} 登录流程已结束"

        self.app.toast("info", "已启动登录窗口，请在浏览器中完成登录（最长等待 10 分钟）")
        self._run_bg(job, done_msg="登录流程已结束", err_prefix="登录失败")

    # ---------------- WorkBuddy ----------------

    def _recover_wb(self):
        """「找回历史账号」：把备份归档里**丢失的**账号一键合并回快照。

        ★ 2026-09-25 新增。为什么值得有：
          当天出过一次真实的数据丢失（`capture_current` 读失败时把整个快照
          覆盖成单账号），用户「之前捕获并保留的两个账号」少了一个。
          归档把**历史上捕获过的每个账号**都留一份、**只增不减**，
          于是误删 / 清空 / 换设备都能一键找回。
        """
        t = self.theme
        try:
            import wb_checkin
            arc = wb_checkin.archived_accounts()
            saved = wb_checkin._load_saved()
        except Exception as e:  # noqa: BLE001
            self.app.toast("warn", f"读取备份失败：{e!r}")
            return
        lost = [u for u in arc if u not in saved]
        # 可找回的排前面，其余按首次捕获时间
        items = sorted(arc.items(),
                       key=lambda kv: (kv[0] not in lost,
                                       str(kv[1].get("first_seen") or "")))
        h = min(560, 250 + 40 * max(1, min(len(items), 7)))
        m = W.Modal(self.app, t, "历史账号备份（找回）", width=580, height=h)
        tk.Label(m.content,
                 text="所有**捕获过**的账号都会自动备份在这里，**只增不减**。"
                      "快照里丢失的可以勾选后一键找回；已在快照里的标记为「已在」。",
                 font=t.f_small, fg=t.fg_muted, bg=t.bg_soft, anchor="w",
                 justify="left", wraplength=520).pack(fill="x", pady=(0, 8))

        if not items:
            tk.Label(m.content,
                     text="备份里还没有账号。\n点上方「捕获当前账号」捕获一次，"
                          "之后每个捕获过的账号都会自动备份到这里。",
                     font=t.f_small, fg=t.fg_dim, bg=t.bg_soft, anchor="w",
                     justify="left").pack(fill="x", pady=10)
            W.button(m.footer, t, "关闭", command=m.close,
                     variant="outline").pack(side="right")
            m.show()
            return

        box = tk.Frame(m.content, bg=t.bg_soft)
        box.pack(fill="both", expand=True)
        vars_ = {}
        for uid, item in items:
            row = tk.Frame(box, bg=t.bg_soft)
            row.pack(fill="x", pady=2)
            var = tk.BooleanVar(value=(uid in lost))
            if uid in lost:
                vars_[uid] = var
                tk.Checkbutton(row, variable=var, bg=t.bg_soft,
                               activebackground=t.bg_soft, selectcolor=t.bg_input,
                               highlightthickness=0, bd=0).pack(side="left")
            else:
                tk.Label(row, text="  ", bg=t.bg_soft).pack(side="left")
            name = item.get("nickname") or (uid[:8] + "****")
            tk.Label(row, text=str(name), font=t.f_small, fg=t.fg,
                     bg=t.bg_soft, width=12, anchor="w").pack(side="left")
            exp = item.get("expires_at") or 0
            exp_txt = (time.strftime("%Y-%m-%d", time.localtime(exp / 1000))
                       if exp else "未知")
            detail = f"uid {uid[:8]}… · 令牌到期 {exp_txt} · 首次 {item.get('first_seen') or '未知'}"
            tk.Label(row, text=detail, font=t.f_tiny, fg=t.fg_dim,
                     bg=t.bg_soft, anchor="w").pack(side="left", padx=(6, 0))
            if uid not in lost:
                W.Badge(row, t, "已在", "slate").pack(side="right")
            else:
                W.Badge(row, t, "可找回", "amber").pack(side="right")

        tk.Label(m.content,
                 text=(f"可找回 {len(lost)} 个" if lost
                       else "备份里的账号目前都在快照里，没有需要找回的"),
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft, anchor="w").pack(
            fill="x", pady=(8, 0))

        def do():
            picked = [u for u, v in vars_.items() if v.get()]
            if not picked:
                self.app.toast("warn", "没有勾选要找回的账号")
                return
            n, total, err = wb_checkin.recover_accounts(
                picked, lambda m: None)   # 结果用 toast 呈现，不再另写日志
            if err:
                self.app.toast("warn", err)
                return
            m.close()
            self.app.toast("success", f"已找回 {n} 个账号，现共 {total} 个")
            self.reload()

        W.button(m.footer, t, "关闭", command=m.close,
                 variant="outline").pack(side="right", padx=(6, 0))
        if lost:
            W.button(m.footer, t, f"找回选中（{len(lost)}）", command=do,
                     variant="primary").pack(side="right")
        m.show()

    def _capture_wb(self):
        def job():
            import wb_checkin
            before = set(wb_checkin._load_saved().keys())
            ok = wb_checkin.capture_current(lambda m: None)
            after = wb_checkin._load_saved()
            new = set(after.keys()) - before
            if not ok:
                return False, "未读取到本机 WorkBuddy 登录态，请确认桌面端已登录"
            if new:
                uid = list(new)[0]
                return True, f"已捕获新账号「{after[uid].get('nickname') or uid[:8]}」"
            return True, "该账号此前已捕获，令牌已刷新"

        self._run_bg(job, err_prefix="捕获失败")

    def _remove_wb(self, acc):
        t = self.theme
        m = W.Modal(self.app, t, "移除令牌快照", width=430, height=190)
        tk.Label(m.content,
                 text=f"确认移除 WorkBuddy 账号「{acc['display']}」的令牌快照？\n"
                      f"移除后该账号不再参与签到，可随时重新捕获。",
                 font=t.f_small, fg=t.fg_muted, bg=t.bg_soft, justify="left",
                 anchor="w", wraplength=380).pack(fill="x")

        def do():
            try:
                with open(C.WB_TOKENS, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            data.pop(acc["uid"], None)
            with open(C.WB_TOKENS, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            st = C.load_state()
            st.pop(acc["key"], None)
            try:
                with open(C.STATE_PATH, "w", encoding="utf-8") as f:
                    json.dump(st, f, ensure_ascii=False, indent=2)
            except OSError:
                pass
            m.close()
            self.app.toast("success", f"已移除账号 {acc['display']}")
            # 令牌快照文件已改，缓存必须失效后重取，否则列表不会更新
            self.app.refresh_accounts(on_done=self.reload)

        W.button(m.footer, t, "取消", command=m.close, variant="outline").pack(
            side="right", padx=(6, 0))
        W.button(m.footer, t, "确认移除", command=do, variant="danger").pack(side="right")
        # 文案里含用户可控的展示名，折行会顶高内容 → 见 `Modal.fit_height()` 注释
        m.fit_height()
        m.show()

    # ---------------- TraeCN ----------------

    def _rescan_tc(self):
        def job():
            import tc_checkin
            fresh = tc_checkin._scan_ide_logs(lambda m: None)
            if not fresh:
                return False, ("未在 Trae 客户端日志中发现有效令牌，"
                               "请打开一次 Trae CN IDE 或 TraeWork CN 桌面端并登录")
            return True, f"已发现 {len(fresh)} 个账号令牌"

        self._run_bg(job, err_prefix="扫描失败")

    def _show_token(self, acc):
        t = self.theme
        m = W.Modal(self.app, t, "TraeCN 令牌信息", width=560, height=290)
        tok = acc.get("token") or ""
        masked = (tok[:24] + "…" + tok[-12:]) if len(tok) > 40 else tok
        rows = [
            ("账号", acc["display"]),
            ("uid", acc["uid"]),
            ("设备标识", "已就绪" if acc.get("has_did") else "缺失（请打开一次 IDE）"),
            ("剩余有效期", self._exp_text(acc.get("exp") or 0)),
            ("令牌（脱敏）", masked),
        ]
        for k, v in rows:
            r = tk.Frame(m.content, bg=t.bg_soft)
            r.pack(fill="x", pady=3)
            tk.Label(r, text=k, font=t.f_small, fg=t.fg_dim, bg=t.bg_soft,
                     width=12, anchor="w").pack(side="left")
            tk.Label(r, text=str(v), font=t.f_mono if "令牌" in k or k == "uid"
                     else t.f_small, fg=t.fg, bg=t.bg_soft, anchor="w",
                     wraplength=380, justify="left").pack(side="left", fill="x",
                                                          expand=True)
        tk.Label(m.content,
                 text="令牌来自 IDE 日志，属于本机敏感凭据，请勿外传。",
                 font=t.f_tiny, fg=t.fg_dim, bg=t.bg_soft).pack(anchor="w", pady=(10, 0))
        W.button(m.footer, t, "关闭", command=m.close, variant="outline").pack(side="right")
        # 5 行里 `账号` / `uid` / `令牌` 都是变长值，而弹窗固定 290px 高、
        # 内容可用区只有约 208px（现实值就占了 178px，余量不到两行）。
        # 一旦某个值折行把内容顶过 208px，多出来的行会**静默消失**（既看不到
        # 也滚不动）。这里按内容自然高度**只增不减**，装得下就完全不动。
        m.fit_height()
        m.show()

    def _exp_text(self, exp):
        if not exp:
            return "未知"
        left = (exp - time.time()) / 86400
        if left < 0:
            return "已过期"
        return f"约 {left:.1f} 天"

    def on_hide(self):
        # 离开页面就停止轮询：否则线程会一直空转到超时，还会在用户
        # 已经切到别的页面时弹出「已自动捕获」的提示。
        w = getattr(self, "_watch", None)
        if w:
            w["stop"] = True
