#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号「自动发现 → 纳入 → 列表刷新」全链路测试。

对应本次修复的两个问题：
  1) 手动/自动添加后列表不更新（刷新请求被静默丢弃）
  2) TraeWork 只能手填名字，不能像 WorkBuddy 那样捕获本机已有账号

测试用真实 Tk 根窗口构造 AccountsPage，但不进入 mainloop；
所有异步刷新由测试自己驱动（调用 ui_call 队列），保证确定性。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
import traceback
from contextlib import contextmanager

GUI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

from app import config as C            # noqa: E402
from app.theme import Theme            # noqa: E402

OK, FAIL, SKIP = [], [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(f"{'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra else ""))


def skip(name, why=""):
    """前置条件不满足 → 记 SKIP（**不算失败**）。

    ★ 项目约定：环境相关的前置条件不满足时报 SKIP 并**说清原因**，绝不报 FAIL。
      反例（2026-09-25 实测）：本机 TraeCN 令牌过期 → `collect_accounts` 返回 0，
      于是「TraeCN 能发现令牌」报 FAIL —— 但那**正是正确行为**
      （没有有效令牌当然发现不到），红得毫无信息量，还容易被当成"程序坏了"。
    """
    SKIP.append(name)
    print(f"SKIP {name}" + (f"   {why}" if why else ""))


@contextmanager
def _sandbox():
    """把「TraeWork 会话目录」与「配置文件」改道到临时目录。

    ★ 为什么必须（2026-09-18 踩，代价很大）：原先这些测试直接往**生产**
      ``profiles/traework/`` 里建假会话，并把假账号写进**生产** ``config.json``。
      脚本一旦中途抛异常，``finally`` 里的清理就跳过 → 紧接着跑的
      ``verify_automation_chain`` 会拿这些假账号去执行**真实签到** →
      Chromium 被拉起来对着假 profile 写一堆浏览器文件
      （``Crashpad`` / ``Local State`` / ``Default/Network/Cookies``）→
      **下一次跑测试就 PermissionError**（库被浏览器占用）→ 测试从此**永远红**。

      这是「测试污染生产数据」的第二起（第一起是 `test_tw_api_first` 往真实令牌
      缓存写了一条假 uid）。判据：``profiles/traework/_test_*`` 里出现
      ``.checkin.lock`` 或 ``Crashpad``，就是它。

    ``C.TW_PROFILES`` / ``C.CONFIG_PATH`` 都是模块级变量，而
    ``_discover_traework`` / ``traework_profile_dir`` 在**调用时**读全局，
    所以这里直接改属性即可生效。
    """
    real_prof, real_cfg = C.TW_PROFILES, C.CONFIG_PATH
    tmp = tempfile.mkdtemp(prefix="acctdisc_")
    prof = os.path.join(tmp, "traework")
    os.makedirs(prof, exist_ok=True)
    C.TW_PROFILES = prof
    C.CONFIG_PATH = os.path.join(tmp, "config.json")
    with open(C.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"traework": {"enabled": True, "accounts": []}, "traecn": {},
                   "workbuddy": {}, "schedule": {}, "options": {}}, f,
                  ensure_ascii=False, indent=2)
    try:
        yield tmp
    finally:
        C.TW_PROFILES = real_prof
        C.CONFIG_PATH = real_cfg
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 1. 刷新不丢请求
def test_refresh_not_dropped():
    """刷新进行中再请求一次，结束时必须补跑，而不是丢掉。"""
    print("\n=== 1. 刷新请求不会被静默丢弃 ===")
    calls = {"n": 0}
    orig = C.all_accounts
    gate = {"open": False}

    def slow(cfg=None):
        calls["n"] += 1
        # 第一轮卡住，模拟慢扫描
        guard = 0
        while not gate["open"] and guard < 200:
            time.sleep(0.01)
            guard += 1
        return orig(cfg)

    C.all_accounts = slow
    C._CACHE["data"] = None
    C._CACHE["ts"] = 0

    class FakeApp:
        def __init__(self):
            self._acct_refreshing = False
            self._acct_pending = False
            self._acct_waiters = []
            self.called = 0

        def refresh_accounts(self, on_done=None):
            # 与 App.refresh_accounts 同一套逻辑（这里是纯逻辑复刻）
            if self._acct_refreshing:
                self._acct_pending = True
                if on_done is not None:
                    self._acct_waiters.append(on_done)
                return
            self._acct_refreshing = True
            self.called += 1
            waiters = list(self._acct_waiters) + ([on_done] if on_done else [])
            self._acct_waiters = []

            def done(data):
                self._acct_refreshing = False
                for fn in waiters:
                    fn()
                if self._acct_pending:
                    self._acct_pending = False
                    self.refresh_accounts()

            C.refresh_accounts_async(on_done=done)

    try:
        app = FakeApp()
        app.refresh_accounts()            # 第 1 轮（页面加载）
        time.sleep(0.2)
        app.refresh_accounts()            # 第 2 轮（用户操作，应被记为待补）
        check("第二轮被记为待补而不是丢弃", app._acct_pending is True)
        gate["open"] = True
        # 等补跑完成
        for _ in range(100):
            time.sleep(0.05)
            if not app._acct_refreshing and not app._acct_pending:
                break
        check("最终补跑了刷新", app.called >= 2, f"实际发起 {app.called} 次")
        check("补跑后缓存已填充", C.peek_accounts() is not None)
    finally:
        C.all_accounts = orig


# ---------------------------------------------------------------- 2. 发现能力
def test_discovery():
    print("\n=== 2. 本机账号自动发现 ===")
    d = C.discover_accounts()
    check("返回三个平台的键", set(d.keys()) == {"traework", "workbuddy", "traecn"},
          str(list(d.keys())))

    tw = d["traework"]
    check("TraeWork 能发现账号来源", len(tw) >= 1, f"{len(tw)} 个")
    for item in tw:
        check("TraeWork 发现项含字段 platform/id/display/logged_in",
              all(k in item for k in ("platform", "id", "display", "logged_in")))
        check("TraeWork 发现项 platform 正确", item["platform"] == "traework")
        check("TraeWork 发现项标注来源 source", item.get("source") in ("client", "profile"),
              str(item.get("source")))
    # 会话来源必须带 profile（纳入操作依赖它）
    for item in [x for x in tw if x.get("source") == "profile"]:
        check("会话来源含字段 profile", "profile" in item)
    # 本机客户端来源必须带 uid（用于说明「登录的是谁」）
    for item in [x for x in tw if x.get("source") == "client"]:
        check("客户端来源含 uid", bool(item.get("uid")))
        check("客户端来源标记已登录", item.get("logged_in") is True)

    wb = d["workbuddy"]
    check("WorkBuddy 能发现账号", len(wb) >= 1, f"{len(wb)} 个")
    if wb:
        check("WorkBuddy 发现项含 uid", bool(wb[0].get("uid")))
        check("WorkBuddy 区分当前登录与快照",
              any("当前登录" in (f.get("detail") or "") for f in wb)
              or any("快照" in (f.get("detail") or "") for f in wb),
              str([f.get("detail") for f in wb])[:120])

    tc = d["traecn"]
    if tc:
        check("TraeCN 能发现令牌", True, f"{len(tc)} 个")
    else:
        skip("TraeCN 能发现令牌",
             "本机当前没有**有效**的 TraeCN 令牌（IDE 日志里的 JWT 已过期）"
             "—— 打开一次 Trae CN IDE 登录即可恢复。这是环境状态，不是缺陷")


# ---------------------------------------------------------------- 3. 纳入
def _purge_test_accounts():
    """把测试残留的 _test* 账号从 config 里清掉。

    必须前前后后都清：如果上一次测试因为异常没走到 finally，
    残留会污染下一次运行（表现为「扫描到未纳入的会话」失败）。
    """
    CONFIG = C.CONFIG_PATH
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return
    accs = cfg.get("traework", {}).get("accounts", [])
    keep = [a for a in accs
            if not str(a.get("profile") or a.get("name") or "").startswith("_test")]
    if len(keep) != len(accs):
        cfg["traework"]["accounts"] = keep
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"  （清理残留测试账号 {len(accs) - len(keep)} 个）")


def test_adopt():
    print("\n=== 3. 把发现的会话纳入账号列表 ===")
    # ★ 全程沙箱：假会话建在临时目录，配置也写在临时目录（见 _sandbox 的说明）
    with _sandbox():
        _adopt_body()


def _adopt_body():
    CONFIG = C.CONFIG_PATH
    fake = os.path.join(C.TW_PROFILES, "_test_adopt")
    # 造一个「已登录」的会话目录
    os.makedirs(os.path.join(fake, "Default"), exist_ok=True)
    with open(os.path.join(fake, "Default", "Cookies"), "wb") as f:
        f.write(b"x" * 128)
    with open(os.path.join(fake, "nickname.txt"), "w", encoding="utf-8") as f:
        f.write("测试账号甲")

    # 未纳入前应被标记出来
    found = C.discover_accounts("traework")
    hit = next((f for f in found if f["id"] == "_test_adopt"), None)
    check("扫描到未纳入的会话", hit is not None and hit["in_config"] is False,
          json.dumps(hit, ensure_ascii=False) if hit else "未找到")
    check("识别为已登录", bool(hit and hit.get("logged_in")))
    check("显示名取 nickname.txt", bool(hit and hit["display"] == "测试账号甲"),
          hit["display"] if hit else "")

    # 纳入
    ok, msg = C.adopt_traework("_test_adopt")
    check("纳入成功", ok, msg)
    accs = json.load(open(CONFIG, encoding="utf-8"))["traework"]["accounts"]
    check("config 里出现该账号",
          any(a.get("profile") == "_test_adopt" for a in accs),
          json.dumps(accs, ensure_ascii=False))

    # 重复纳入应被拒
    ok2, msg2 = C.adopt_traework("_test_adopt")
    check("重复纳入被拒绝", not ok2, msg2)

    # 纳入后列表应能读到
    listed = C.list_traework_accounts()
    got = next((a for a in listed if a["profile"] == "_test_adopt"), None)
    check("账号列表能读到它", got is not None)
    check("展示名是真实昵称", bool(got and got["display"] == "测试账号甲"),
          got["display"] if got else "")
    check("标记为已有会话", bool(got and got.get("has_profile")))


# ---------------------------------------------------------------- 4. Cookies 位置
def test_cookie_locations():
    """★ 回归：Chromium 的 Cookies 位置随版本变过，两个位置都必须认。

    实测本机 profiles/traework/trae1 的 Cookies 在 ``Default/Network/Cookies``
    （32KB、登录后持续更新），而 ``Default/Cookies`` 根本不存在。
    旧代码只查后者 → 已登录的会话被判成「尚未登录」。

    这里四种组合都要覆盖，并且带**负对照**（空文件 / 没有文件必须判成未登录），
    否则「两个位置都认」这个改动可能把「未登录」也误判成「已登录」。
    """
    print("\n=== 4. Cookies 位置兼容（Default/Network/Cookies 与 Default/Cookies）===")
    # ★ 全程沙箱：绝不往生产的 profiles/traework/ 里建假会话（见 _sandbox 的说明）
    with _sandbox():
        _cookie_body()


def _cookie_body():
    def make(name, rels):
        """建一个假会话目录；rels = [(相对路径, 字节数), ...]"""
        pdir = os.path.join(C.TW_PROFILES, name)
        os.makedirs(pdir, exist_ok=True)
        for rel, size in rels:
            fp = os.path.join(pdir, rel)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "wb") as f:
                f.write(b"x" * size)
        return pdir

    # ① 只在新版位置放非空 Cookies，且**没有** nickname.txt
    net_dir = make("_test_ck_net",
                   [(os.path.join("Default", "Network", "Cookies"), 128)])
    # ② 只在旧版位置放非空 Cookies
    leg_dir = make("_test_ck_legacy",
                   [(os.path.join("Default", "Cookies"), 128)])
    # ③ 新版位置存在但**是空文件**（Playwright 建目录时会先落一个空库）
    emp_dir = make("_test_ck_empty",
                   [(os.path.join("Default", "Network", "Cookies"), 0)])
    # ④ 目录存在但完全没有 Cookies
    non_dir = make("_test_ck_none", [])

    # 单元级：直接问判据函数
    hit, path = C.traework_has_session(net_dir)
    check("【正对照】只认新版位置时能判定已登录", hit is True, path)
    check("命中路径确实指向 Network/Cookies",
          bool(path and "Network" in path), path)
    hit2, path2 = C.traework_has_session(leg_dir)
    check("【正对照】旧版位置仍然认（向后兼容）", hit2 is True, path2)
    hit3, _ = C.traework_has_session(emp_dir)
    check("【负对照】空 Cookies 不算登录", hit3 is False)
    hit4, _ = C.traework_has_session(non_dir)
    check("【负对照】没有 Cookies 不算登录", hit4 is False)
    check("traework_cookie_files 同时列出两个位置",
          len(C.traework_cookie_files(net_dir)) == 2)

    # 集成级：通过 discover_accounts 看最终判定
    found = {f["id"]: f for f in C.discover_accounts("traework")}
    net = found.get("_test_ck_net")
    check("【正对照】discover 把新版位置的会话判为已登录",
          bool(net and net["logged_in"] and net["has_session"]),
          json.dumps(net, ensure_ascii=False) if net else "未找到")
    leg = found.get("_test_ck_legacy")
    check("【正对照】discover 把旧版位置的会话判为已登录",
          bool(leg and leg["logged_in"] and leg["has_session"]))
    emp = found.get("_test_ck_empty")
    check("【负对照】discover 不把空 Cookies 判为已登录",
          bool(emp and not emp["logged_in"] and not emp["has_session"]))
    non = found.get("_test_ck_none")
    check("【负对照】discover 不把无 Cookies 判为已登录",
          bool(non and not non["logged_in"] and not non["has_session"]))

    # list_traework_accounts：has_session / logged_in 字段必须存在
    # （账号页的「已登录/未登录」徽标依赖它，此前只看目录是否存在）
    cfg = C.load_config()
    accs = cfg.setdefault("traework", {}).setdefault("accounts", [])
    for _p in ("_test_ck_net", "_test_ck_none"):
        if not any(a.get("profile") == _p for a in accs):
            accs.append({"name": _p, "profile": _p})
    C.save_config(cfg)
    listed = {a["profile"]: a for a in C.list_traework_accounts()}
    ln = listed.get("_test_ck_net") or {}
    lo = listed.get("_test_ck_none") or {}
    check("list_traework_accounts 提供 has_session 字段",
          "has_session" in ln and "logged_in" in ln)
    check("【正对照】已登录会话 logged_in=True",
          ln.get("logged_in") is True, ln)
    check("【负对照】空目录 logged_in=False（不再只看目录存在）",
          lo.get("logged_in") is False, lo)
    # 沙箱里的目录/配置由 _sandbox 的 finally 统一回收，这里不需要手工清理


# ---------------------------------------------------------------- 5. 页面渲染
def test_page_render():
    print("\n=== 5. 账号页真实渲染（三个平台 × 亮/暗）===")
    import app.pages.accounts as A

    class FakeApp(tk.Tk):
        def __init__(self):
            super().__init__()
            self.withdraw()

        def ui_call(self, fn):
            # 测试里直接同步执行，避免依赖 mainloop
            try:
                fn()
            except Exception:  # noqa: BLE001
                traceback.print_exc()

        def toast(self, *a, **k):
            pass

        def accounts(self):
            return C.accounts_cached(max_age=0)

        def refresh_accounts(self, on_done=None):
            C.refresh_accounts_async()
            if on_done:
                on_done()

        def _refresh_topbar(self):
            pass

    root = FakeApp()
    try:
        for module in ("traework", "workbuddy", "traecn"):
            for dark in (False, True):
                page = A.AccountsPage(root, Theme(dark=dark), root, module=module)
                page.pack(fill="both", expand=True)
                root.update_idletasks()
                root.update()
                # 手动触发一次扫描渲染（不等待线程）
                page._found = C.discover_accounts(module)
                page._render_discover()
                root.update_idletasks()
                root.update()
                kids = len(page.discover_box.winfo_children())
                tag = f"{'dark' if dark else 'light'} {module}"
                if kids >= 1:
                    check(f"{tag} 发现区已渲染", True, f"{kids} 个卡片")
                elif not page._found:
                    # 本机这个平台**没有可发现的账号**（如 TraeCN 令牌过期）
                    # → 发现区本来就该是空的，这是**正确行为**，不是缺陷
                    skip(f"{tag} 发现区已渲染",
                         f"本机没有可发现的 {module} 账号（发现结果为空）")
                else:
                    check(f"{tag} 发现区已渲染", False, f"{kids} 个卡片")
                page.destroy()
    finally:
        root.destroy()


def test_no_duplicate_children():
    """反复 reload() 不应堆叠控件。

    Tkinter 不会自动回收旧控件：如果某个按钮挂在 body 上而 reload 只清了
    hint_box / list_box，每刷新一次就会多出一个按钮（实测「手动新增」曾重复）。
    """
    print("\n=== 5. 反复刷新不产生重复控件 ===")
    import app.pages.accounts as A

    class FakeApp(tk.Tk):
        def __init__(self):
            super().__init__()
            self.withdraw()

        def ui_call(self, fn):
            fn()

        def toast(self, *a, **k):
            pass

        def accounts(self):
            return C.accounts_cached(max_age=0)

        def refresh_accounts(self, on_done=None):
            C.refresh_accounts_async()
            if on_done:
                on_done()

        def _refresh_topbar(self):
            pass

    root = FakeApp()
    try:
        page = A.AccountsPage(root, Theme(dark=False), root, module="traework")
        page.pack(fill="both", expand=True)
        root.update()

        def count_buttons():
            n = 0

            def walk(w):
                nonlocal n
                try:
                    if w.winfo_class() == "Canvas" and hasattr(w, "master"):
                        pass
                except Exception:  # noqa: BLE001
                    return
                for c in w.winfo_children():
                    walk(c)
            # 直接数 body 的直接子容器数量（这才是会堆叠的地方）
            return len(page.body.winfo_children())

        n1 = count_buttons()
        for _ in range(4):
            page.reload()
            root.update()
        n2 = count_buttons()
        check("body 子控件数量稳定", n1 == n2, f"刷新前 {n1}，刷新 4 次后 {n2}")

        # 再确认「手动新增」按钮只有一个（注意只数 Button，
        # 旁边的说明 Label 也含「手动新增」四个字，不能一起算进去）
        found = []

        def walk(w):
            for c in w.winfo_children():
                try:
                    if c.winfo_class() == "Button" and "手动新增" in str(c.cget("text")):
                        found.append(c)
                except Exception:  # noqa: BLE001
                    pass
                walk(c)
        walk(page)
        check("「手动新增」按钮只有 1 个", len(found) == 1, f"实际 {len(found)} 个")
        page.destroy()
    finally:
        root.destroy()


# ---------------------------------------------------------------- 6. 本机 Trae 客户端识别
def test_local_clients():
    """TraeWork 板块要能识别本机 Trae 系客户端（IDE / 桌面端）登录的账号。

    背景：用户反馈「TraeWork 板块没有识别到 TraeWork CN 软件里的账号」。
    根因是发现逻辑此前只扫 profiles/traework 的 Playwright 会话，
    完全不读本机客户端（TRAE SOLO CN / Trae CN）的登录态。
    """
    print("\n=== 6. 本机 Trae 客户端识别 ===")
    import tc_checkin as T

    roots = T.client_log_roots()
    check("能枚举本机 Trae 客户端日志根", isinstance(roots, list), f"{len(roots)} 个")
    check("每个根是 (路径, 客户端名, 类型) 三元组",
          all(len(r) == 3 for r in roots), str(roots)[:120])
    for r in roots:
        check(f"日志根存在：{r[1]}", os.path.isdir(r[0]))

    ids = T.scan_client_identity()
    check("scan_client_identity 返回字典", isinstance(ids, dict))
    for uid, info in ids.items():
        check("客户端身份含 uid（18 位左右）", uid.isdigit() and len(uid) >= 10, uid)
        check("客户端身份标注 client", bool(info.get("client")), str(info.get("client")))
        check("客户端身份含 has_token 布尔", isinstance(info.get("has_token"), bool))

    # 与发现层打通：客户端身份应出现在 traework 发现结果里
    tw = C.discover_accounts("traework")
    clients = [f for f in tw if f.get("source") == "client"]
    if ids:
        check("客户端账号出现在 TraeWork 发现结果中",
              len(clients) >= 1, f"{len(clients)} 个")
        for f in clients:
            check("客户端发现项标记 in_config（无需纳入）", f.get("in_config") is True)
            check("客户端发现项 detail 说明签到通道",
                  "TraeCN" in (f.get("detail") or ""), str(f.get("detail"))[:80])

    # 幂等：连续扫描结果应一致（纯只读，不产生副作用）
    ids2 = T.scan_client_identity()
    check("重复扫描结果一致", set(ids2.keys()) == set(ids.keys()))


# ---------------------------------------------------------------- 6b. 令牌归因
def test_client_attribution():
    """★ 令牌必须归因到**真正提供令牌**的客户端，不能把 IDE 的功劳记到桌面端。

    2026-09-18 实测的真 bug：`scan_client_identity` 按 uid 跨客户端聚合，
    `has_token` 是「或」累积的，而 `client` / `kind` 会被**后扫到的**客户端
    无条件覆盖。本机 roots 顺序是 Trae CN(IDE) → TRAE SOLO CN(桌面端)，
    于是输出变成
        {'client': 'TRAE SOLO CN', 'kind': '桌面端', 'has_token': True}
    —— 界面据此显示「TRAE SOLO CN（桌面端）· 签到由 TraeCN 通道生效」，
    而实测该桌面端日志里明文 JWT 命中 **0 枚**（``"token":"******"``）、
    磁盘上（8007 个文件 / 300 MB）也扫不到明文令牌。纯属不实描述，
    也正是用户问「为什么只有 TraeWork 不是桌面端捕获」时最容易被误导的地方。
    """
    print("\n=== 6b. 客户端令牌归因（IDE vs 桌面端）===")
    import base64
    import json as _json
    import tc_checkin as T

    tmp = tempfile.mkdtemp(prefix="cliattr_")
    try:
        def _mk(appdata, client, name, uid, with_jwt):
            """造一个客户端日志目录（沙箱内，绝不碰生产 APPDATA）。"""
            d = os.path.join(appdata, client, "logs", "20260918T000000")
            os.makedirs(d, exist_ok=True)
            b64 = lambda o: base64.urlsafe_b64encode(      # noqa: E731
                _json.dumps(o).encode()).decode().rstrip("=")
            jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.%s.%s" % (
                b64({"data": {"id": uid}, "exp": int(time.time()) + 3600}),
                "c2lnbmF0dXJl")
            body = ('{"ScreenName":"%s","UserID":"%s"}' % (name, uid)).encode()
            body += b'\n{"token":"******"}\n'      # 桌面端日志里 token 是脱敏的
            if with_jwt:
                body += ('\nAuthorization: Bearer %s\n' % jwt).encode()
            with open(os.path.join(d, "main.log"), "wb") as f:
                f.write(body)

        UID = "3956523050480058"

        # --- 场景 A：IDE 有明文 JWT + 桌面端只有脱敏 token（本机真实情况）---
        ad = os.path.join(tmp, "A")
        _mk(ad, "Trae CN", "Klein", UID, with_jwt=True)
        _mk(ad, "TRAE SOLO CN", "Klein", UID, with_jwt=False)
        info = (T.scan_client_identity(appdata=ad) or {}).get(UID) or {}
        check("场景 A：识别到账号", bool(info), info)
        check("场景 A：has_token=True", info.get("has_token") is True, info)
        check("★ 场景 A：client 归因到 IDE，**不被桌面端覆盖**",
              info.get("client") == "Trae CN", info)
        check("★ 场景 A：token_client 指向真正提供令牌的客户端",
              info.get("token_client") == "Trae CN", info)
        check("场景 A：kind 为 IDE", info.get("kind") == "IDE", info)
        check("场景 A：昵称取到", info.get("name") == "Klein", info)

        # --- 场景 B：【负对照】只有桌面端（没装 IDE）→ 必须 has_token=False ---
        bd = os.path.join(tmp, "B")
        _mk(bd, "TRAE SOLO CN", "Klein", UID, with_jwt=False)
        info_b = (T.scan_client_identity(appdata=bd) or {}).get(UID) or {}
        check("【负对照】只有桌面端时 has_token=False",
              info_b.get("has_token") is False, info_b)
        check("【负对照】只有桌面端时 client 才是桌面端",
              info_b.get("client") == "TRAE SOLO CN", info_b)
        check("【负对照】只有桌面端时 token_client 为空",
              not info_b.get("token_client"), info_b)

        # --- 场景 C：第三个客户端（TraeWork CN 桌面端）同样不得覆盖归因 ---
        cd = os.path.join(tmp, "C")
        _mk(cd, "Trae CN", "Klein", UID, with_jwt=True)
        _mk(cd, "TraeWork CN", "Klein", UID, with_jwt=False)
        info_c = (T.scan_client_identity(appdata=cd) or {}).get(UID) or {}
        check("场景 C：TraeWork CN 桌面端也不覆盖归因",
              info_c.get("client") == "Trae CN"
              and info_c.get("token_client") == "Trae CN", info_c)

        # --- 与发现层打通：detail 不能谎称桌面端「签到由 TraeCN 通道生效」---
        real = T.scan_client_identity
        try:
            T.scan_client_identity = lambda *a, **k: {
                UID: {"name": "Klein", "client": "TRAE SOLO CN", "kind": "桌面端",
                      "has_token": False, "token_client": "", "token_kind": ""}}
            det = (C._discover_local_clients() or [{}])[0].get("detail") or ""
            check("桌面端（无令牌）不再谎称「签到由 TraeCN 通道生效」",
                  "签到由 TraeCN 通道生效" not in det, det)
            check("桌面端（无令牌）说明了「无可用令牌」",
                  "无可用令牌" in det, det)

            T.scan_client_identity = lambda *a, **k: {
                UID: {"name": "Klein", "client": "TRAE SOLO CN", "kind": "桌面端",
                      "has_token": True, "token_client": "Trae CN",
                      "token_kind": "IDE"}}
            det2 = (C._discover_local_clients() or [{}])[0].get("detail") or ""
            check("有令牌时仍说明签到通道，并标出令牌来源",
                  "签到由 TraeCN 通道生效" in det2 and "Trae CN" in det2, det2)
        finally:
            T.scan_client_identity = real
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 7. 数据目录定位
def test_data_root():
    """exe 便携版必须复用已有项目数据，而不是另起一份空配置。

    用户反馈：双击 exe 后状态栏显示「TraeWork 0 个账号」、账号列表「暂无账号」，
    但自己在项目里的 config.json 明明有账号。
    根因：`_locate_root()` 只认「exe 旁边有 checkin.py」，而打包后该文件
    在 _internal 里，条件永远为假 → ROOT 落到 exe 所在空目录。
    """
    print("\n=== 7. 数据目录定位（exe 便携版场景）===")
    import importlib
    import tempfile

    # --- 场景 A：项目存在 + 有数据 → 必须定位到项目根 ---
    real_root = C.ROOT
    check("脚本模式定位到项目根", C._has_project(real_root), real_root)
    check("项目根含 config.json", C._has_data(real_root), str(C._has_data(real_root)))
    check("非首次运行", C.is_first_run() is False)

    # --- 场景 B：模拟 exe 在 dist/ 下，应向上找到项目 ---
    saved_frozen = getattr(sys, "frozen", None)
    saved_exe = sys.executable
    saved_mei = getattr(sys, "_MEIPASS", None)
    try:
        sys.frozen = True
        sys.executable = os.path.join(GUI_DIR, "dist", "TraeBuddy签到助手",
                                      "TraeBuddy签到助手.exe")
        C2 = importlib.reload(C)
        check("exe 模式下仍定位到项目根（关键修复）",
              C2.ROOT == real_root, f"{C2.ROOT} != {real_root}")
        check("exe 模式下能读到已有账号",
              len(C2.load_config().get("traework", {}).get("accounts", [])) >= 1,
              str(C2.load_config().get("traework", {}).get("accounts")))
    finally:
        if saved_frozen is None:
            try:
                del sys.frozen
            except AttributeError:
                pass
        else:
            sys.frozen = saved_frozen
        sys.executable = saved_exe
        if saved_mei is not None:
            sys._MEIPASS = saved_mei
        C3 = importlib.reload(C)
        check("恢复后 ROOT 正确", C3.ROOT == real_root, C3.ROOT)

    # --- 场景 C：全新环境（无项目无数据）→ 应自动初始化 ---
    tmp = os.path.join(tempfile.gettempdir(), "_test_fresh_install")
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "app"))
        exe = os.path.join(tmp, "app", "TraeBuddy签到助手.exe")
        with open(exe, "w", encoding="utf-8") as f:
            f.write("")
        fake_home = os.path.join(tmp, "home")
        os.makedirs(fake_home)

        saved_env = {k: os.environ.get(k) for k in ("TRAEBUDDY_HOME", "USERPROFILE", "HOME")}
        os.environ.pop("TRAEBUDDY_HOME", None)
        os.environ["USERPROFILE"] = fake_home
        os.environ["HOME"] = fake_home
        try:
            sys.frozen = True
            sys.executable = exe
            C4 = importlib.reload(C)
            check("全新环境识别为首次运行", C4.is_first_run() is True,
                  f"ROOT={C4.ROOT}")
            created, root = C4.ensure_data_dir()
            check("首次运行执行了初始化", created is True)
            check("初始化后写出 config.json", os.path.isfile(C4.CONFIG_PATH))
            check("初始化后建出 logs/", os.path.isdir(C4.LOG_DIR))
            check("初始化后建出 profiles/traework/", os.path.isdir(C4.TW_PROFILES))
            check("初始化幂等（再调一次不再新建）",
                  C4.ensure_data_dir()[0] is False)
            cfg = C4.load_config()
            check("默认配置三个平台齐全",
                  all(k in cfg for k in ("traework", "workbuddy", "traecn")))
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # 还原模块状态
        try:
            del sys.frozen
        except AttributeError:
            pass
        sys.executable = saved_exe
        importlib.reload(C)
        check("清理后 ROOT 回到项目根", C.ROOT == real_root, C.ROOT)


def main():
    # 上一次异常退出可能残留测试账号，先清干净再跑，保证可重复
    _purge_test_accounts()
    for fn in (test_refresh_not_dropped, test_discovery, test_adopt,
               test_cookie_locations,
               test_page_render, test_no_duplicate_children, test_local_clients,
               test_client_attribution,
               test_data_root):
        try:
            fn()
        except Exception:  # noqa: BLE001
            FAIL.append(fn.__name__)
            print(f"FAIL {fn.__name__} 抛异常：")
            traceback.print_exc()
    _purge_test_accounts()

    print(f"\n合计 {len(OK) + len(FAIL) + len(SKIP)} 项：通过 {len(OK)}，失败 {len(FAIL)}" + (f"，跳过 {len(SKIP)}" if SKIP else ""))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
