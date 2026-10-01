#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：TraeWork 签到判定的三条硬化措施（离线，不需要浏览器）。

背景（2026-09-17 真实故障）
--------------------------
当天自动化弹窗报了 `[traework] Klein: fail — 未找到签到入口且无法确认已签到`，
但**签到其实成功了** —— 展开明细里有一条「2026/10/18 12:08 到期」，
正是今天+31 天、时间也对得上当轮的运行时刻。

排查中发现两个真问题，本文件把它们固化成断言：

1. **`_JS_MARK_SIGNS` 是非法 JavaScript。** 旧写法把 `SIGN_TEXT_RE.pattern`
   裸插进 `if ((%s).test(t))`，生成 `if ((^(每日|...)?签到$|...).test(t))` ——
   `^` 不能这样当表达式用，`page.evaluate` 抛
   `SyntaxError: Unexpected token '^'` → **永远返回 0 个候选**，
   「点击签到按钮」这条兜底路径**从来没生效过**。
   → 现在 JS 只收集文本，正则过滤在 Python 侧；并用 `node --check` 离线验证。

2. **展开明细时"点一次就傻等 10 秒"。** 明细是懒加载的，而「每日签到」这一行的
   文本在数据到达前就已存在 → 点在还没绑定处理器的行上 = **空操作**，
   面板始终折叠，等多久都等不到。同一天重试又能成功就是这个原因。
   → 现在"点了校验、没展开就重点"，并且无论成败都留**展开后**的截图。

3. 兜底旁证：明细实在渲染不出来时，看折叠行的「共 N 笔」是否 +1
   （实测签到成功 8 笔 → 9 笔）。**只在明细没渲染时才采信** ——
   明细渲染出来了就以它为准，避免"看不见就当没发生"。

跑法：python tests/test_tw_sign_detect.py
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date as _date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import app.config  # noqa: E402  定位项目根并挂上 sys.path
import checkin  # noqa: E402
import tw_checkin as T  # noqa: E402

ROOT = checkin.ROOT
FIXTURE = os.path.join(HERE, "fixtures", "traework_dash_expanded.txt")

PASS = 0
FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  OK   %s" % name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  FAIL %s   %s" % (name, extra))


def section(t):
    print("\n== %s ==" % t)


# ---------------------------------------------------------------- 假面板
class FakeDash:
    """可控的假积分面板，模拟「折叠 → 点击展开 → 明细渲染」的懒加载过程。

    `expand_after_clicks=N` 表示**前 N 次点击是空操作**（模拟"行还没绑定处理器
    就被点了"），第 N+1 次才真的展开 —— 这正是 2026-09-13/14/15/17 失败的原因，
    也是本文件最重要的回归点。
    """

    def __init__(self, collapsed, expanded, expand_after_clicks=1,
                 never_expand=False):
        self.collapsed = collapsed
        self.expanded = expanded
        self.expand_after = expand_after_clicks
        self.never_expand = never_expand
        self.clicks = 0
        self.shots = []

    def evaluate(self, js, *a):
        if "document.body" in js:          # _page_text 的调用
            if self.never_expand or self.clicks < self.expand_after:
                return self.collapsed
            return self.expanded
        return True                        # _JS_MARK_SIGN_ROW 标记成功

    def click(self, sel, timeout=None):
        self.clicks += 1

    def screenshot(self, path=None, full_page=False):
        self.shots.append(path)

    def close(self):
        pass


def load_fixture():
    with io.open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def collapsed_text(count, total=None):
    """构造折叠态文本（只有「共 N 笔」，没有各笔日期）。"""
    total = total if total is not None else count * 150
    return ("用量管理\n总可用积分\n5,425\n奖励积分\n"
            "老用户福利\n2026/09/29 15:22 到期\n1,574.87 / 2,000\n"
            "每月登录赠送\n2026/09/30 23:59 到期\n500 / 500\n"
            f"每日签到\n通用积分 · 共 {count} 笔\n{total:,} / {total:,}\n")


def main():
    from app.ui import widgets  # noqa: F401  仅确保 GUI 侧可导入（保持与其它测试一致）

    print("项目根 =", ROOT)
    print("夹具   =", FIXTURE, os.path.exists(FIXTURE))

    # ---------------- A. JS 语法 / 正则归属 ----------------
    section("A) `_JS_MARK_SIGNS` 必须是合法 JavaScript，且正则只在 Python 侧")
    js = T._JS_MARK_SIGNS
    check("JS 里不含 Python 正则片段（`签到$`）", "签到$" not in js)
    check("JS 里不含 Python 专有语法 `(?:`", "(?:" not in js)

    node = None
    for cand in (r"C:\Program Files\nodejs\node.exe",
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs\node.exe")):
        if os.path.exists(cand):
            node = cand
            break
    if node is None:
        # 退回 PATH 上的 node
        for p in os.environ.get("PATH", "").split(os.pathsep):
            c = os.path.join(p, "node.exe")
            if os.path.exists(c):
                node = c
                break

    def node_check(src):
        fd, path = tempfile.mkstemp(suffix=".js")
        os.close(fd)
        try:
            with io.open(path, "w", encoding="utf-8") as f:
                f.write("const f = " + src.strip() + ";\n")
            return subprocess.run([node, "--check", path],
                                  capture_output=True).returncode
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    if node:
        rc = node_check(js)
        check("生成的 JS 通过 `node --check`", rc == 0, "rc=%s" % rc)
        # 正对照：把正则塞回 JS（旧写法）必须**语法报错**，否则这个检查是死的
        old = js.replace(
            "    el.setAttribute('data-cb-sign', 's' + i);",
            "    if ((" + T.SIGN_TEXT_RE.pattern +
            ").test(t)) { el.setAttribute('data-cb-sign', 's' + i); }")
        rc_old = node_check(old)
        check("正对照：旧写法（正则嵌 JS）必须语法报错", rc_old != 0,
              "rc=%s（应为非 0；若为 0 说明检查本身没生效）" % rc_old)
    else:
        print("  SKIP 未找到 node，跳过 JS 语法校验")

    # ---------------- B. 候选过滤 ----------------
    section("B) 候选过滤：只认真正的签到按钮，排除「每日签到」开关行")

    def filt(texts):
        return [t for t in texts
                if T.SIGN_TEXT_RE.fullmatch(t) and t != T.SIGN_TOGGLE_TEXT]

    check("「每日签到」被排除（它是展开开关，不是签到按钮）",
          filt(["每日签到"]) == [])
    check("「立即签到」命中", filt(["立即签到"]) == ["立即签到"])
    check("「去签到」命中", filt(["去签到"]) == ["去签到"])
    check("裸「签到」命中", filt(["签到"]) == ["签到"])
    check("「领取」命中", filt(["领取"]) == ["领取"])
    check("「领取积分」命中", filt(["领取积分"]) == ["领取积分"])
    check("「签到奖励」**不**命中（明细条目标题，不是按钮）",
          filt(["签到奖励"]) == [])
    check("真实主页候选里没有签到类（实测 35 个可见短文本）",
          filt(["Work", "Code", "Design", "新建任务", "插件市场", "模板库",
                "自动化", "我的文件", "Klein", "免费", "5,425", "升级权益",
                "管理账户", "消息", "语言", "主题", "设置", "报告问题",
                "退出登录", "下载桌面端"]) == [])

    # ---------------- C. 折叠态解析 ----------------
    section("C) 折叠态/展开态解析（用**真实抓取**的明细文本做夹具）")
    real = load_fixture() if os.path.exists(FIXTURE) else ""
    if not real:
        check("夹具存在", False, FIXTURE)
    else:
        check("真实展开态：明细条目数 == 9", T._sign_entry_count(real) == 9,
              "实际 %d" % T._sign_entry_count(real))
        check("真实展开态：折叠行笔数 == 9", T._sign_progress(real) == 9,
              "实际 %s" % T._sign_progress(real))
        col = collapsed_text(8)
        check("折叠态：明细条目数 == 0（**不等于今天没签到**）",
              T._sign_entry_count(col) == 0)
        check("折叠态：笔数 == 8", T._sign_progress(col) == 8)
        check("笔数读不到时返回 None", T._sign_progress("毫无相关文案") is None)

    # ---------------- D. 日期判定（今天+31） ----------------
    section("D) 到期日判据：恰好「今天+31」才算今日已签")
    if real:
        dates = sorted({T._parse_cn_date(m.group(0))
                        for m in re.finditer(T.DATE_TEXT_RE, real)
                        if T._parse_cn_date(m.group(0))})
        latest = dates[-1]
        check("真实夹具里最新到期日 == 2026-10-18",
              latest == _date(2026, 10, 18), str(latest))
        check("today = 最新到期日-31 ⇒ 判为今日已签",
              T._dashboard_already_checked(real, latest - timedelta(days=31)))
        # ★ 严格性负对照：夹具里是 9 条**连续日期**的明细（10/10..10/18），
        #   所以取 today = latest-30（= 9/18）时：
        #       today+31 = 10/19（**不在**夹具里）
        #       today+30 = 10/18（在夹具里，就是"昨天签的那条"）
        #   判据必须**只认 +31**，否则会把昨天签的算成今天 —— 这是已发生过的
        #   真实误判，注释里明确写着「勿再放宽」。
        #   ⚠️ 不要用 today = latest-32 做这条负对照：那等于把 today 往前挪一天，
        #   恰好命中夹具里的另一条（10/17），得到 True 是**正确**行为。
        check("负对照：today+30 有明细、today+31 没有 ⇒ 必须判**未**签到",
              not T._dashboard_already_checked(real, latest - timedelta(days=30)))

    # ---------------- E. 展开逻辑（核心回归） ----------------
    section("E) `_expand_sign_detail`：点空之后必须重点，而不是傻等")
    if real:
        dates = sorted({T._parse_cn_date(m.group(0))
                        for m in re.finditer(T.DATE_TEXT_RE, real)
                        if T._parse_cn_date(m.group(0))})
        today = dates[-1] - timedelta(days=31)
        col8, col9 = collapsed_text(8), collapsed_text(9)
        shots = os.path.join(tempfile.gettempdir(), "_tw_shots")

        def run(expand_after=1, never=False, count_before=8, expanded=None):
            d = FakeDash(col8, expanded if expanded is not None else real,
                         expand_after_clicks=expand_after, never_expand=never)
            st, why = T._expand_sign_detail(
                d, lambda m: None, "T", shots, "tag", today.isoformat(),
                count_before=count_before, expand_wait_s=12, verify_wait_s=2)
            return st, why, d

        # ① 正常：点一次就展开，明细有今天+31
        st, why, d = run(expand_after=1)
        check("① 点一次即展开 → ok", st == "ok", str(why))
        check("① 留下了**展开后**的截图", any(d.shots), str(d.shots))

        # ② ★ 核心回归：前 3 次点击是空操作，第 4 次才展开
        #    旧逻辑"点一次就傻等"在这里必然失败
        st, why, d = run(expand_after=4)
        check("② 点击空转 3 次后仍能展开 → ok（旧逻辑必失败）", st == "ok",
              "%s / clicks=%d" % (why, d.clicks))
        check("② 确实重试了多次点击", d.clicks >= 4, "clicks=%d" % d.clicks)

        # ③ 明细渲染出来了但没有今天条目 → 不能当成功（明细是权威）
        nohit = real.replace("2026/10/18", "2026/11/18")
        st, why, d = run(expand_after=1, expanded=nohit)
        check("③ 明细已渲染但无今日条目 → 不判成功", st is None, str(st))
        check("③ 原因写明「没有今日签到条目」", "没有今日签到条目" in why, why)

        # ④ 明细始终不渲染，但笔数 +1 → 用旁证判成功
        st, why, d = run(never=True, count_before=8)
        # never_expand 时文本恒为折叠态 8 笔 → 笔数没变 → 不能判成功
        check("④ 明细不渲染且笔数没变 → 不判成功（留待复核）", st is None, str(st))
        check("④ 原因写明「明细未渲染」", "未渲染" in why, why)

        # ⑤ 明细不渲染但笔数确实 +1 → 判成功（旁证）
        d = FakeDash(col8, col9, never_expand=True)
        # 让 evaluate 在点击后返回 9 笔的折叠文本：用 expand_after=1 但 never
        # 会恒返回 collapsed；这里改用「展开后=9笔折叠文本」的变体
        d2 = FakeDash(col8, col9, expand_after_clicks=1)
        st2, why2 = T._expand_sign_detail(
            d2, lambda m: None, "T", shots, "tag", today.isoformat(),
            count_before=8, expand_wait_s=4, verify_wait_s=2)
        check("⑤ 明细未渲染但笔数 8→9 → 判 ok（旁证兜底）", st2 == "ok", str(why2))
        check("⑤ 详情说明是笔数旁证", "笔数" in why2, why2)

        # ⑥ 反例：明细已渲染（有明细条目）时**不得**用笔数旁证掩盖
        st, why, d = run(expand_after=1, expanded=nohit, count_before=8)
        check("⑥ 明细渲染了就不看笔数（明细权威）", st is None and "笔数" not in why,
              why)

    # ---------------- F. 失败文案必须能区分原因 ----------------
    section("F) 失败文案不能把「没渲染」和「确实没签」混成一句")
    src = io.open(os.path.join(ROOT, "tw_checkin.py"), encoding="utf-8").read()
    # 只看**代码**行：注释里提到旧文案是允许的（本文件就专门解释了为什么改掉它）。
    # 朴素去注释（本文件里 `#` 不出现在字符串字面量中，够用）。
    code_only = [ln.split("#", 1)[0] for ln in src.splitlines()]
    check("旧文案已从**代码**里移除（注释里可以提到它）",
          not any("入口可能未开放或页面变化" in ln for ln in code_only))
    check("新文案包含「未能确认签到结果」", "未能确认签到结果" in src)
    check("新文案会拼上具体原因（reasons）", "reasons" in src)

    # ---------------- G. profile 互斥锁 ----------------
    # 背景：2026-09-17 计划任务 12:13:09 触发时，手工诊断脚本正占用**同一个**
    # Chromium profile → 第二个实例拿不到登录态 → 判定「未登录」→ 弹出有头浏览器
    # 干等 7 分钟 → 超时失败。用户完全不知道原因，只看到一条失败。
    # Chromium 的 user-data-dir 本来就是独占的，所以同一 profile 必须互斥。
    section("G) 同一 profile 只允许一个实例（并发会丢登录态）")
    tmp = tempfile.mkdtemp(prefix="tw_lock_")   # 放 %TEMP%，不碰真实 profiles/
    prof = os.path.join(tmp, "acc")
    T._profile_dir = lambda cfg, acc: prof      # 把 profile 根改到临时目录
    lockp = os.path.join(prof, T._LOCK_NAME)
    logs = []

    def lg(m):
        logs.append(str(m))

    # ① 空目录 → 拿得到锁
    l1 = T._acquire_profile_lock(prof, lg, "Klein")
    check("① 空目录能拿到锁", bool(l1) and os.path.exists(l1), str(l1))

    # ② 持有者 pid 存活 → 第二个实例必须被挡住
    l2 = T._acquire_profile_lock(prof, lg, "Klein")
    check("② 持有者存活时拒绝并发（返回 None）", l2 is None, str(l2))
    check("② 日志说明了原因", any("正被另一个实例使用" in m for m in logs), str(logs))

    # ③ ★ 释放后必须能重新拿到 —— 这条就是「finally 里忘了放锁」的回归
    T._release_profile_lock(l1)
    check("③ 释放后锁文件已删除", not os.path.exists(lockp))
    l3 = T._acquire_profile_lock(prof, lg, "Klein")
    check("③ 释放后能重新拿到锁", l3 is not None, str(l3))
    T._release_profile_lock(l3)

    # ④ 残留锁：持有者 pid 已退出 → 自动清理并接管
    with io.open(lockp, "w", encoding="utf-8") as f:
        f.write("999999 %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    check("④ 前置：pid 999999 判为已退出", not T._pid_alive(999999))
    l4 = T._acquire_profile_lock(prof, lg, "Klein")
    check("④ 死进程的残留锁被清理并接管", l4 is not None, str(l4))
    T._release_profile_lock(l4)

    # ⑤ 超龄锁（pid 存活但超过 _LOCK_STALE_S）→ 也接管
    with io.open(lockp, "w", encoding="utf-8") as f:
        f.write("%d x\n" % os.getpid())
    old = time.time() - (T._LOCK_STALE_S + 60)
    os.utime(lockp, (old, old))
    l5 = T._acquire_profile_lock(prof, lg, "Klein")
    check("⑤ 超龄锁被清理并接管", l5 is not None, str(l5))
    T._release_profile_lock(l5)

    # ⑥ ★ 端到端：锁被占用时 `_run_account` 必须返回 "busy"，**不能是 "skip"**。
    #    "skip" 在本项目里语义是「今日已完成」→ 汇总按成功计、弹窗显示「已签到」，
    #    会把「本轮根本没签到」伪装成成功（这正是要防的那类"静默掩盖"）。
    #    传 p=None 是刻意的：锁检查发生在用 p 之前，所以不需要浏览器。
    held = T._acquire_profile_lock(prof, lg, "Klein")
    cfg = {"traework": {"accounts": [{"name": "Klein", "profile": "acc"}]}}
    acc = {"name": "Klein", "profile": "acc"}
    st, why = T._run_account(None, cfg, acc, lg, tmp, "2026-09-17")
    check("⑥ 锁被占用时返回 busy", st == "busy", str(st))
    check("⑥ **不是** skip（skip 会被当成已签到）", st != "skip", str(st))
    check("⑥ 文案说明会自动补跑", "自动补跑" in why, why)
    T._release_profile_lock(held)

    # ⑦ ★ 行为化验证「finally 一定放锁」：让浏览器启动直接抛异常。
    #    异常会穿出 `_run_account`（没有 except 兜住启动），但 finally 必须先放锁，
    #    否则这一轮留下的残留锁会让下一轮白等 30 分钟才被判定超龄。
    class BoomP:
        class chromium:
            @staticmethod
            def launch_persistent_context(*a, **k):
                raise RuntimeError("boom")

    raised = False
    try:
        T._run_account(BoomP, cfg, acc, lg, tmp, "2026-09-17")
    except Exception:  # noqa: BLE001
        raised = True
    check("⑦ 前置：浏览器启动确实抛了异常", raised)
    check("⑦ 异常路径下锁也被释放（finally 生效）", not os.path.exists(lockp))

    # ⑧ 状态口径：busy 必须**不计入成功**，否则弹窗标题会说「全部成功」
    section("G2) busy 状态的口径（不能算成功）")
    ok = [r for r in [{"status": "busy"}] if r["status"] in ("ok", "skip")]
    check("⑧ busy 不计入「成功/跳过」", ok == [], str(ok))
    import app.config as C
    check("⑧ GUI 有 busy 的中文名", "busy" in C.STATUS_CN, str(C.STATUS_CN.get("busy")))
    check("⑧ GUI 给 busy 配了非红/非绿的中性色",
          C.STATUS_TONE.get("busy") not in (None, "red", "green"),
          str(C.STATUS_TONE.get("busy")))
    check("⑧ CLI 有 busy 的中文名", "busy" in checkin._STATUS_CN,
          str(checkin._STATUS_CN.get("busy")))

    print("\n" + "=" * 66)
    print("TraeWork 签到判定：%d OK / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
