#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键签到页按钮：显示状态 + 功能行为的回归测试。

重点回归（曾经真实存在的 bug）：
  「开始签到」按钮的 command 写成了 `lambda: self.start_checkin()`，
  而签名默认 skip_done=True → 用户取消勾选「跳过今日已签」后一点按钮，
  复选框被**静默改回勾选**，于是已签账号依旧不签，用户以为按钮没反应。

不启动真实签到：把 runner 换成桩对象，只验证「点了会发生什么」。

用法：python tests/test_checkin_buttons.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
sys.path.insert(0, GUI)
sys.path.insert(0, os.path.dirname(GUI))

import tkinter as tk  # noqa: E402

from app.application import App  # noqa: E402
from app.pages import checkin as CP  # noqa: E402

ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label}   {extra}")


def pump(app, n=8, dt=0.08):
    for _ in range(n):
        app.update()
        time.sleep(dt)


class FakeRunner:
    """只记录被怎么调用，不真跑签到。"""

    calls = []

    def __init__(self, platforms=None, force=False, use_gate=True, accounts=None,
                 wait_lock_s=0.0):
        FakeRunner.calls.append({
            "platforms": platforms, "force": force,
            "use_gate": use_gate, "accounts": accounts,
            # ★ 2026-09-24 新增：手动优先执行会传一个正数（见
            #   runner.MANUAL_LOCK_WAIT_S）。桩必须跟着真实签名走，
            #   否则「生产改了参数、桩没跟」会直接 TypeError。
            "wait_lock_s": wait_lock_s,
        })
        self.running = True
        self.events = None

    def start(self):
        return self

    def cancel(self):
        self.running = False


app = App()
app.geometry("1240x860+40+20")
pump(app, 14)
app.goto("traework", "checkin")
pump(app, 16)
page = app.page
check("页面类型是 CheckinPage", type(page).__name__ == "CheckinPage",
      type(page).__name__)

print("\n== A. 构件存在性与初始状态 ==")
check("有「开始签到」按钮", hasattr(page, "_btn_run"))
check("有「停止」按钮", hasattr(page, "_btn_cancel"))
check("有「跳过今日已签」复选框", hasattr(page, "_chk_skip"))
check("有「强制执行」复选框", hasattr(page, "_chk_force"))
check("默认勾选「跳过今日已签」", page._skip_done.get() is True,
      page._skip_done.get())
check("默认不勾「强制执行」", page._force.get() is False, page._force.get())
check("停止按钮初始不可见", not page._btn_cancel.winfo_manager())
check("开始按钮初始可见", bool(page._btn_run.winfo_manager()))

n_acc = len(page.accounts)
print(f"  （本页账号数 = {n_acc}）")
check("勾选框数量与账号数一致", len(page._sel) == n_acc,
      f"{len(page._sel)} vs {n_acc}")

print("\n== B. 全选 / 全不选 ==")
# 注意：本机账号可能「今日已签」，此时「跳过今日已签」生效 → 全选后目标数本就该是 0。
# 所以这里用「强制执行」（= 忽略今日记录）拿到一个确定的非零基线，再去比。
page._skip_done.set(True)
page._force.set(False)
page._select_all(True)
pump(app, 3)
n_skip = len(page._targets())

page._force.set(True)          # 强制模式：skip 自动失效
page._on_force_toggle()
page._select_all(True)
pump(app, 3)
n_force = len(page._targets())

check("强制模式下全选 = 全部账号", n_force == n_acc and n_force > 0,
      f"n_force={n_force} n_acc={n_acc}")
check("跳过模式下目标数不超过强制模式", n_skip <= n_force,
      f"n_skip={n_skip} n_force={n_force}")
check("全选后「开始签到」可用",
      str(page._btn_run.cget("state")) == "normal",
      page._btn_run.cget("state"))
check("目标标签同步显示",
      f"{n_force} 个账号" in page._target_lbl.cget("text"),
      page._target_lbl.cget("text"))

page._select_all(False)
pump(app, 3)
check("全不选后目标数 == 0", len(page._targets()) == 0)
check("全不选后「开始签到」被禁用",
      str(page._btn_run.cget("state")) == "disabled",
      page._btn_run.cget("state"))
check("目标标签同步显示 0", "0 个账号" in page._target_lbl.cget("text"),
      page._target_lbl.cget("text"))
page._select_all(True)
pump(app, 3)
check("重新全选后「开始签到」恢复可用",
      str(page._btn_run.cget("state")) == "normal",
      page._btn_run.cget("state"))

print("\n== C. 「跳过今日已签」与「强制执行」互斥 ==")
page._skip_done.set(True)
page._force.set(True)
page._on_skip_toggle()
check("勾「跳过」自动取消「强制」", page._force.get() is False,
      page._force.get())
page._force.set(True)
page._skip_done.set(True)
page._on_force_toggle()
check("勾「强制」自动取消「跳过」", page._skip_done.get() is False,
      page._skip_done.get())

print("\n== D. 【关键回归】按钮不覆盖用户的复选框选择 ==")
# 用户主动取消「跳过今日已签」并勾上「强制执行」，且确保有可选目标
page._skip_done.set(False)
page._force.set(True)
page._select_all(True)
pump(app, 3)
before_skip = page._skip_done.get()
before_force = page._force.get()
n_targets = len(page._targets())
check("前置条件：有可选目标", n_targets > 0, n_targets)

CP.R.CheckinRunner = FakeRunner
FakeRunner.calls.clear()
try:
    page.start_checkin()          # 等价于点击「开始签到」按钮
    pump(app, 3)
except Exception as e:  # noqa: BLE001
    print("   (调用异常)", repr(e))

check("用户取消勾选后，点按钮不会把它改回勾选",
      page._skip_done.get() == before_skip,
      f"点前={before_skip} 点后={page._skip_done.get()}")
check("「强制执行」状态也不被篡改",
      page._force.get() == before_force,
      f"点前={before_force} 点后={page._force.get()}")
check("runner 被真的启动了", len(FakeRunner.calls) == 1,
      len(FakeRunner.calls))
if FakeRunner.calls:
    c = FakeRunner.calls[0]
    check("runner 收到 force=True（与复选框一致）", c["force"] is True, c)
    check("runner 收到的是本平台", c["platforms"] == ["traework"], c)
    check("runner 收到全部已选账号",
          c["accounts"] and len(c["accounts"]["traework"]) == n_targets, c)

print("\n== E. 运行中按钮切换 ==")
check("运行中隐藏「开始签到」", not page._btn_run.winfo_manager())
check("运行中显示「停止」", bool(page._btn_cancel.winfo_manager()))
page._cancel()
pump(app, 3)
check("点「停止」后 runner.running 变 False",
      page.runner is None or page.runner.running is False)
page._finish()
pump(app, 3)
check("结束后「开始签到」重新出现", bool(page._btn_run.winfo_manager()))
check("结束后「停止」隐藏", not page._btn_cancel.winfo_manager())
check("结束后「开始签到」可点", str(page._btn_run.cget("state")) == "normal",
      page._btn_run.cget("state"))

print("\n== F. 程序化调用仍然生效（不能改坏调用方）==")
# 每次调用前把桩 runner 置为已停止，否则会被
# `if runner.running: 早退` 拦住（那是正确行为，不是 bug）
def _reset_runner():
    if page.runner is not None:
        page.runner.running = False
    page.busy = False


_reset_runner()
page._skip_done.set(False)
FakeRunner.calls.clear()
page.start_checkin(skip_done=True)
pump(app, 3)
check("显式传 skip_done=True 会覆盖复选框", page._skip_done.get() is True)
# ★ 2026-09-24：手动入口必须带上「优先执行」的等待参数
#   （TraeWork 的 profile 被自动化那一轮占用时先等它让出，而不是记 busy 跳过）
from app.services import runner as _R                                     # noqa: E402
check("★ 手动签到把 MANUAL_LOCK_WAIT_S 传给了 runner",
      FakeRunner.calls and FakeRunner.calls[-1]["wait_lock_s"] == _R.MANUAL_LOCK_WAIT_S,
      FakeRunner.calls[-1] if FakeRunner.calls else None)
page._finish()
pump(app, 3)

_reset_runner()
page._skip_done.set(True)
page.start_checkin(skip_done=False)
pump(app, 3)
check("显式传 skip_done=False 会覆盖复选框", page._skip_done.get() is False)
page._finish()
pump(app, 3)

print("\n== G. 已在运行时拒绝重复启动 ==")
_reset_runner()
page._skip_done.set(False)
page._force.set(True)
page._select_all(True)
pump(app, 3)
FakeRunner.calls.clear()
page.start_checkin()
pump(app, 3)
n_first = len(FakeRunner.calls)
page.busy = False               # 假装界面空闲，但 runner 仍在跑
page.start_checkin()
pump(app, 3)
check("runner 仍在运行时不会重复启动", len(FakeRunner.calls) == n_first == 1,
      f"第一次={n_first} 累计={len(FakeRunner.calls)}")
page._finish()
pump(app, 3)

print("\n== H. 顶栏「一键签到」= 全部平台（2.4.9 新增）==")
# 历史行为：顶栏按钮只切到当前模块并以 platforms=[self.module] 运行，
# 也就是说在 TraeWork 页点「一键签到」，TraeCN / WorkBuddy 的账号一个都不签。
# 这与按钮名字给人的预期不符，2.4.9 起改成真·全局。
from app import config as C  # noqa: E402

_reset_runner()
page._scope_var.set("current")
page._load_accounts()
pump(app, 4)
n_cur = len(page.accounts)
check("范围选择器默认是「仅当前平台」", page.scope_all is False)
check("「仅当前平台」只列出本平台账号",
      all(a["platform"] == "traework" for a in page.accounts),
      {a["platform"] for a in page.accounts})

page._scope_var.set("all")
page._on_scope_change()
pump(app, 6)
pids = {a["platform"] for a in page.accounts}
check("切「全部平台」后账号数不少于单平台", len(page.accounts) >= n_cur,
      f"{len(page.accounts)} vs {n_cur}")
check("切「全部平台」后仍含本平台账号", "traework" in pids, pids)
check("「全部平台」的账号按平台分组渲染（有分组标题）",
      len(pids) >= 1)
check("目标标签显示平台数",
      "个平台" in page._target_lbl.cget("text") or not page.accounts,
      page._target_lbl.cget("text"))

# 直接验证 start_checkin(scope="all") 传给 runner 的内容
page._force.set(True)
page._on_force_toggle()
page._select_all(True)
pump(app, 4)
FakeRunner.calls.clear()
page.start_checkin(skip_done=True, scope="all")
pump(app, 4)
check("scope=all 时 runner 收到三个平台",
      bool(FakeRunner.calls) and
      FakeRunner.calls[0]["platforms"] == ["traework", "traecn", "workbuddy"],
      FakeRunner.calls[0] if FakeRunner.calls else None)
if FakeRunner.calls:
    acc = FakeRunner.calls[0]["accounts"] or {}
    check("scope=all 时 accounts 按平台分组（不再只有本平台）",
          set(acc) <= {"traework", "traecn", "workbuddy"} and len(acc) >= 1,
          list(acc))
    check("scope=all 时本平台账号仍在其中", "traework" in acc, list(acc))
page._finish()
pump(app, 4)

# 再验证 App.quick_checkin（顶栏 / 托盘按钮的实际入口）
_reset_runner()
page._scope_var.set("current")
page._load_accounts()
pump(app, 4)
FakeRunner.calls.clear()

# 先测「今日全部已签」这一真实存在的状态：必须**不启动** runner，
# 并给出明确提示（否则用户点了没反应会以为按钮坏了）。
#
# ★ 必须**显式**关掉「强制重签」（2026-09-24）。
#   页面现在是**复用**的（`App._page_cache`），上面 `page._force.set(True)`
#   会保留下来；而 `_targets_by_platform()` 里「强制执行」的优先级**高于**
#   「跳过今日已签」→ 即使今日全部已签也照样排进目标 → 下面的断言必然失败。
#   ★ 改前这条能过，是因为每次切页都**销毁重建**页面、复选框被重置回默认 False
#     —— 它当时是「靠销毁重建才成立」的，并没有真的在测「全部已签」。
#     这类**依赖重建副作用的断言**在页面缓存上线后会集体失效，
#     必须逐个改成「显式设置前置状态」。
page._force.set(False)
page._on_force_toggle()
pump(app, 3)

_toasts = []
_real_toast = app.toast
app.toast = lambda kind, text, ms=3200: _toasts.append((kind, text))
_real_load_state = C.load_state
try:
    _st = dict(_real_load_state())
    print('DBG1 force=', page._force.get(), 'skip=', page._skip_done.get(),
          'accounts=', [(a['key'], _st.get(a['key'])) for a in page.accounts],
          'today=', C.today_str(), 'sel=', len(page._sel),
          'n=', sum(len(v) for v in page._targets_by_platform().values()))
    C.load_state = lambda *a, **k: _st
    app.quick_checkin()
    pump(app, 30)
    page_done = app.page
    check("全部已签时 quick_checkin 不启动 runner",
          len(FakeRunner.calls) == 0, len(FakeRunner.calls))
    check("全部已签时给出「无需签到」提示",
          any(k == "warn" for k, _ in _toasts), _toasts)

    # 再把「今日判重表」清空 → 每个账号都算未签，验证真的一键跑全平台
    FakeRunner.calls.clear()
    _toasts.clear()
    C.load_state = lambda *a, **k: {}
    app.quick_checkin()
    pump(app, 30)
    page2 = app.page
    check("quick_checkin 落在签到页", type(page2).__name__ == "CheckinPage",
          type(page2).__name__)
    check("quick_checkin 把范围置为「全部平台」",
          getattr(page2, "scope_all", None) is True, page2.scope_all)
    check("quick_checkin 强制跳过今日已签",
          page2._skip_done.get() is True, page2._skip_done.get())
    check("quick_checkin 真的启动了 runner", len(FakeRunner.calls) == 1,
          len(FakeRunner.calls))
    if FakeRunner.calls:
        check("quick_checkin 的 runner 覆盖三个平台",
              FakeRunner.calls[0]["platforms"] == ["traework", "traecn", "workbuddy"],
              FakeRunner.calls[0]["platforms"])
        check("quick_checkin 的 runner 拿到多平台账号",
              len(FakeRunner.calls[0]["accounts"] or {}) >= 1,
              FakeRunner.calls[0]["accounts"])
    page2._finish()
    pump(app, 4)
finally:
    C.load_state = _real_load_state
    app.toast = _real_toast

app.destroy()
print()
print(f"结果：{ok} OK / {fail} FAIL")
sys.exit(0 if fail == 0 else 1)
