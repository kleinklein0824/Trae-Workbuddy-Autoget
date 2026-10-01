#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「手动签到优先执行」回归（2026-09-24）。

用户诉求原话
------------
「优化增强 exe 里的各板块手动点击签到和一键签到（全部平台）的按键模块的
 功能优先强制性，可以避免有些平台自动化签到时出错了要等自动化脚本补跑
 补签的滞后性」

查清的真实机制
--------------
只有 **TraeWork** 走浏览器，而 Chromium 的 `user-data-dir` 是**独占**的，
所以 `tw_checkin` 给每个 profile 加了 `.checkin.lock`。
`_acquire_profile_lock()` 原先在「锁被**活进程**持有」时**立刻放弃**（返回 None），
`_run_account` 于是把该账号记成 `busy`（跳过）—— 计划任务那一轮正在跑的时候，
用户手动点「一键签到」得到的是一堆 `busy`，**一个都没真签**，
只能干等下一轮自动补跑。这就是「滞后性」。

本轮的修法
----------
1. `tw_checkin._acquire_profile_lock(..., wait_s=)`：`wait_s > 0` 时**轮询等待**
   对方释放（最多 wait_s 秒）再放弃。★ **只等不抢** —— 并发用同一个
   user-data-dir 会丢登录态甚至损坏 profile。
2. `CheckinRunner(wait_lock_s=)` + `runner.MANUAL_LOCK_WAIT_S = 120`：
   手动入口（各板块「开始签到」/ 顶栏与托盘「一键签到（全部平台）」）
   都传 120；**计划任务 / CLI 不传 → 0，行为与改动前完全一致**。
3. 消除静默无反应：`start_checkin` 在 `busy` 时原先**静默 return**，
   用户点了毫无反馈；现在给明确提示。`App.quick_checkin` 同样先判再提示。

测试内容
--------
  1. 默认（wait_s=0）：活进程持锁 → **立刻**返回 None（与改动前一致）
  2. ★ wait_s>0：锁在等待期内被释放 → **必须拿到锁**（核心新行为）
  3. wait_s>0：锁一直不放 → 超时返回 None，日志里有「等待超时」
  4. 残留锁（pid 已死）→ 立刻清理并拿到，不受 wait 影响
  5. 接线：`_run_account(lock_wait_s=)` 真的把它传给了 `_acquire_profile_lock`
     （用日志与耗时断言，不是看源码字符串）
  6. ★ 默认值没被动过：`CheckinRunner()` 的 `wait_lock_s == 0`、`manual is False`
  7. GUI：`start_checkin` 在 busy 时**不再静默**；`quick_checkin` 同样先提示

★ 关键断言都带**正对照**（例如 2 的「不等待就拿不到」、3 的「等待期够长就能拿到」），
  避免断言恒真。
"""
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
            else os.path.dirname(os.path.dirname(GUI_DIR)))
for _p in (PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tw_checkin as T                                              # noqa: E402
import wb_checkin as WB                                             # noqa: E402
from app.services import runner as R                                # noqa: E402

OK = FAIL = 0


# ============================================================ 测试隔离
# ★★ 绝不让测试**写**用户的真实快照（2026-09-25 修）。
#   为什么必须拦：`wb_checkin.capture_current()` 内部是
#       saved = _load_saved()      # ← 测试如果给这里打过桩，就是**测试假数据**
#       saved[cur["uid"]] = {...}
#       _save_saved(saved)         # ← 一写就把假数据混进真实文件
#   实测后果：真实 `tokens_workbuddy.json` 里多出一个 uid 含 `demo00000001`
#   的**假账号**，界面上多出一行名字不对的账号。
#   这里把 `_save_saved` 换成「只记录、不落盘」：
#     · **写**被拦死 → 不可能污染真实文件；
#     · **读**（`_load_saved`）保持真实 → 账号数不变，不影响其它断言。
_REAL_SAVE_SAVED = WB._save_saved
_SAVED_WRITES = []


def _record_only(saved):
    """替代 `_save_saved`：记录本次「本来要写什么」，不碰磁盘。"""
    _SAVED_WRITES.append(dict(saved or {}))


WB._save_saved = _record_only


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {extra}")


def _live_pid():
    """拿一个**已退出**进程的 pid（用于制造「残留锁」）。"""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _write_lock(profile, pid):
    os.makedirs(profile, exist_ok=True)
    with open(os.path.join(profile, T._LOCK_NAME), "w", encoding="utf-8") as f:
        f.write("%d 2026-09-24 00:00:00\n" % pid)


def _lockfile(profile):
    return os.path.join(profile, T._LOCK_NAME)


def _consts_text(code):
    """**已废弃的本地实现，保留作反面教材**（见下面 section 6 的说明）。

    第一版我在这里自己写了一份「把 co_consts 里的字符串挖出来」，
    只认裸字符串 → 于是 `f(x=1)` 的关键字参数名（Python 3.11+ 打包在
    **元组常量**里）被判成「没接上」，探针又撒了一次谎。
    项目里 `tests/probe_pyz_contains.py` 的 `_strings_in` / `refs_in`
    **早就处理了这种情况** —— 正确做法是**复用它**，而不是另写一份。
    本函数不再被断言使用，只留在这里提醒后来者。
    """
    out = set()
    for c in (code.co_consts or ()):
        if isinstance(c, str):
            out.add(c)
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="tb_prio_")

    print("== 1. 默认 wait_s=0：活进程持锁 → 立刻放弃（与改动前一致）==")
    p1 = os.path.join(tmp, "p1")
    _write_lock(p1, os.getpid())            # 本进程活着 → 锁「真被占着」
    logs = []
    t0 = time.time()
    got = T._acquire_profile_lock(p1, logs.append, "acc", wait_s=0)
    dt = time.time() - t0
    check("返回 None（判定为被占用）", got is None, got)
    check("★ 没有等待（< 0.3s）", dt < 0.3, f"{dt:.2f}s")
    check("日志说明了原因", any("正被另一个实例使用" in s for s in logs), logs)

    print()
    print("== 2. ★ wait_s>0：锁在等待期内被释放 → 必须拿到锁 ==")
    p2 = os.path.join(tmp, "p2")
    _write_lock(p2, os.getpid())

    def _release_later():
        time.sleep(0.8)
        try:
            os.remove(_lockfile(p2))
        except OSError:
            pass

    threading.Thread(target=_release_later, daemon=True).start()
    logs2 = []
    t0 = time.time()
    got2 = T._acquire_profile_lock(p2, logs2.append, "acc", wait_s=10)
    dt2 = time.time() - t0
    check("★ 等到了锁（返回锁文件路径）", got2 == _lockfile(p2), got2)
    check("★ 确实等待过（0.5s < 耗时 < 5s）", 0.5 < dt2 < 5.0, f"{dt2:.2f}s")
    check("等待期间有进度日志", any("等待它释放" in s for s in logs2), logs2)
    check("拿到后有「开始签到」提示", any("开始签到" in s for s in logs2), logs2)
    # 【正对照】不等待时同一个场景**拿不到**（证明上面那条不是恒真）
    p2b = os.path.join(tmp, "p2b")
    _write_lock(p2b, os.getpid())
    check("【正对照】同一场景 wait_s=0 拿不到锁",
          T._acquire_profile_lock(p2b, lambda s: None, "acc", wait_s=0) is None)

    print()
    print("== 3. wait_s>0 但锁一直不放 → 超时返回 None（不会无限等）==")
    p3 = os.path.join(tmp, "p3")
    _write_lock(p3, os.getpid())
    logs3 = []
    t0 = time.time()
    got3 = T._acquire_profile_lock(p3, logs3.append, "acc", wait_s=1.5)
    dt3 = time.time() - t0
    check("超时后返回 None", got3 is None, got3)
    check("★ 等待时长受 wait_s 约束（1.5s ≤ 耗时 < 5s）", 1.5 <= dt3 < 5.0,
          f"{dt3:.2f}s")
    check("日志里写明「等待超时」",
          any("等待超时" in s for s in logs3), logs3)

    print()
    print("== 4. 残留锁（持锁进程已退出）→ 立刻清理并拿到 ==")
    p4 = os.path.join(tmp, "p4")
    _write_lock(p4, _live_pid())            # 一个已经退出的 pid
    logs4 = []
    t0 = time.time()
    got4 = T._acquire_profile_lock(p4, logs4.append, "acc", wait_s=0)
    dt4 = time.time() - t0
    check("拿到锁", got4 == _lockfile(p4), got4)
    check("没有白等（< 0.3s）", dt4 < 0.3, f"{dt4:.2f}s")
    check("日志说明清理了残留锁", any("清理残留的 profile 锁" in s for s in logs4),
          logs4)

    print()
    print("== 5. 接线：_run_account 把 lock_wait_s 传给了 _acquire_profile_lock ==")
    #    直接跑 _run_account（p=None）：锁拿不到时它在用 p **之前**就返回 busy，
    #    所以可以安全地用「日志 + 耗时」判定等待是否真的发生。
    p5 = os.path.join(tmp, "p5")
    _write_lock(p5, os.getpid())
    cfg = {"traework": {"accounts": []}, "options": {"headless": True}}
    acc = {"name": "demo", "profile": "p5"}
    orig_profile_dir = T._profile_dir
    T._profile_dir = lambda c, a: p5          # 把 profile 指到我们造的锁上
    try:
        lg_a = []
        t0 = time.time()
        st_a, det_a = T._run_account(None, cfg, acc, lg_a.append, tmp,
                                     "2026-09-24", lock_wait_s=0)
        dt_a = time.time() - t0
        check("lock_wait_s=0 → 立刻 busy（现状不变）",
              st_a == "busy" and dt_a < 0.5, f"{st_a} {dt_a:.2f}s")
        lg_b = []
        t0 = time.time()
        st_b, det_b = T._run_account(None, cfg, acc, lg_b.append, tmp,
                                     "2026-09-24", lock_wait_s=1.2)
        dt_b = time.time() - t0
        check("★ lock_wait_s=1.2 → 真的等了（耗时 ≥ 1.2s）",
              dt_b >= 1.2, f"{dt_b:.2f}s")
        check("★ 等到超时仍 busy（没有并发抢锁）", st_b == "busy", st_b)
        check("★ 日志里出现等待进度（说明参数确实传到了锁函数）",
              any("等待它释放" in s or "等待超时" in s for s in lg_b), lg_b)
        check("【正对照】lock_wait_s=0 的日志里**没有**等待字样",
              not any("等待" in s for s in lg_a), lg_a)
    finally:
        T._profile_dir = orig_profile_dir

    print()
    print("== 6. ★ 默认值没被动过（计划任务 / CLI 行为必须与改动前一致）==")
    r0 = R.CheckinRunner()
    check("CheckinRunner() 的 wait_lock_s == 0", r0.wait_lock_s == 0.0,
          r0.wait_lock_s)
    check("CheckinRunner() 的 manual is False", r0.manual is False)
    check("run_full 支持 wait_lock_s 参数",
          "wait_lock_s" in R.run_full.__code__.co_varnames)
    check("★ MANUAL_LOCK_WAIT_S 是正数（手动入口才有等待）",
          R.MANUAL_LOCK_WAIT_S > 0, R.MANUAL_LOCK_WAIT_S)
    # 字节码级接线断言：**复用项目探针的判据**（`probe_pyz_contains.refs_in`），
    # ★ 不要自己再写一份「挖 co_consts 里的字符串」——
    #   第一版我就是那么写的，只认裸字符串，于是 `f(x=1)` 的关键字参数名
    #   （Python 3.11+ 打包在**元组常量**里）被判成「没接上」，探针又撒了一次谎。
    #   项目探针的 `_strings_in` 早就处理了元组常量 —— 复用它才是正解。
    from probe_pyz_contains import refs_in
    rp = R.CheckinRunner._run_platform
    check("★ _run_platform 确实把 wait_lock_s 作为 lock_wait_s 传下去"
          "（复用项目探针的判据）",
          refs_in(rp.__code__, "_run_platform", "lock_wait_s") is True)
    check("★ _run_platform 读取了 self.wait_lock_s",
          "wait_lock_s" in (rp.__code__.co_names or ()), rp.__code__.co_names)
    check("【负对照】编造的名字必须判为「没接上」（证明上面那条不是恒真）",
          refs_in(rp.__code__, "_run_platform", "__no_such_kwarg__") is False)

    print()
    print("== 7. GUI：手动点击不再「静默无反应」==")
    from app.application import App
    from app import config as _C
    app = App()
    app.update()
    for _ in range(20):
        app.update()
        time.sleep(0.02)
    # ★ 必须**等账号缓存就绪**再断言（2026-09-25 修）。
    #   账号是后台线程扫的；没读完时 `quick_checkin` 会走
    #   「正在读取账号，读完后会自动开始签到…」那条分支（也是正确行为），
    #   于是「busy 时给提示」这条会**偶发红**。固定次数的 update 循环
    #   等不到就等不到 —— 改成显式轮询，测试才是确定的。
    _deadline = time.time() + 15
    while _C.peek_accounts() is None and time.time() < _deadline:
        app.update()
        time.sleep(0.05)
    app._switch_module("traework")
    app.update()
    app.goto("traework", "checkin")
    for _ in range(8):
        app.update()
    page = app.page

    toasts = []
    app.toast = lambda kind, text, ms=3200: toasts.append((kind, text))

    page.busy = True
    page.runner = None
    page.start_checkin()
    check("★ busy 时给出明确提示（原先静默 return）",
          any("执行中" in t for _, t in toasts), toasts)
    check("busy 时没有启动新任务",
          page.runner is None or not page.runner.running)

    # 一键签到（顶栏 / 托盘）同样先判再提示
    toasts.clear()
    page.busy = True
    app.quick_checkin()
    for _ in range(4):
        app.update()
    check("★ 一键签到在 busy 时也给出明确提示",
          any("执行中" in t for _, t in toasts), toasts)

    # 正对照：busy 关掉后，提示就不该再出现
    toasts.clear()
    page.busy = False
    app.quick_checkin()
    for _ in range(4):
        app.update()
    check("【正对照】busy=False 时不会出现「执行中」提示",
          not any("执行中" in t for _, t in toasts), toasts)
    # 收尾：别把 runner 留着
    try:
        if page.runner is not None:
            page.runner.cancel()
    except Exception:  # noqa: BLE001
        pass
    try:
        app.destroy()
    except Exception:  # noqa: BLE001
        pass

    print()
    print(f"结果：{OK} OK / {FAIL} FAIL")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        # 恢复被测试替换掉的落盘函数（绝不污染用户的真实快照）
        WB._save_saved = _REAL_SAVE_SAVED
