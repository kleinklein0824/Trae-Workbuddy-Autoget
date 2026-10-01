#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计划任务服务回归测试：刷新守卫语义 + 缓存失效 + 结果码判定。

覆盖本次修复的核心 bug：`refresh_task_info_async` 的 `_refreshing` 布尔守卫
会把刷新中的第二次请求静默丢弃，导致保存签到时间后界面仍显示旧时间
（用户感知为「保存不了」）。这里用真实线程验证「请求不丢帧」。

全程不依赖真实计划任务（不调 PowerShell）——用 monkeypatch 替换底层查询函数。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import scheduler as S  # noqa: E402

OK = FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {extra}")


# ---------------- 用假查询替换真实 PowerShell ----------------
_orig_query = S._query_task_info
_calls = {"n": 0}
_lock = threading.Lock()


def fake_query():
    with _lock:
        _calls["n"] += 1
    time.sleep(0.35)          # 模拟 ~0.35s 的真实查询耗时
    return {"installed": True, "state": "Ready", "times": "09:00,21:00",
            "triggers": "MSFT_TaskDailyTrigger", "lastRun": "x", "result": 0,
            "nextRun": "y", "seq": _calls["n"]}


S._query_task_info = fake_query


def reset():
    with S._lock:
        S._cache["data"] = None
        S._cache["at"] = 0.0
        S._refreshing = False
        S._pending = False
    with _lock:
        _calls["n"] = 0


print("== 1. 刷新中请求不再被丢弃（本次修复的核心）==")
reset()
r1 = S.refresh_task_info_async()
r2 = S.refresh_task_info_async()      # 立刻第二次（模拟 _times_done 撞上 on_show 刷新）
r3 = S.refresh_task_info_async()
check("第一次返回 True（启动刷新）", r1 is True, r1)
check("第二次返回 True（不再被静默丢弃）", r2 is True, r2)
check("第三次返回 True", r3 is True, r3)
time.sleep(1.0)
check("重复请求只起 1 个批次但会补跑（查询 >=2 次）", _calls["n"] >= 2,
      f"实际查询 {_calls['n']} 次")
check("最终缓存已填充", S.cached_task_info() is not None)
with S._lock:
    check("_refreshing 已归位（不会卡死后续刷新）", S._refreshing is False)
    check("_pending 已清空", S._pending is False)

print()
print("== 2. 回调只触发一次（避免界面重复重画）==")
reset()
got = []
S.refresh_task_info_async(on_done=lambda d: got.append(d))
S.refresh_task_info_async(on_done=lambda d: got.append(d))
time.sleep(1.0)
check("外部回调只收到 1 次", len(got) == 1, f"收到 {len(got)} 次")

print()
print("== 3. 单次刷新：结果会写进缓存 ==")
reset()
done = []
S.refresh_task_info_async(on_done=lambda d: done.append(d))
time.sleep(0.8)
check("回调收到结果", len(done) == 1)
check("缓存与回调一致", S.cached_task_info() == done[0])
check("缓存内容正确", (S.cached_task_info() or {}).get("times") == "09:00,21:00")

print()
print("== 4. invalidate_task_cache 在刷新中会登记补跑 ==")
reset()
S.refresh_task_info_async()
time.sleep(0.05)
S.invalidate_task_cache()
check("失效后缓存立即为 None", S.cached_task_info() is None)
with S._lock:
    check("刷新中失效 -> 登记了 _pending", S._pending is True)
time.sleep(1.2)
check("补跑完成后缓存又被填回（不会一直空）", S.cached_task_info() is not None)

print()
print("== 5. task_info 缓存 TTL 行为 ==")
reset()
t1 = S.task_info()
n1 = _calls["n"]
t2 = S.task_info()                 # 命中缓存，不应再查询
check("5s 内命中缓存不重复查询", _calls["n"] == n1, f"{n1} -> {_calls['n']}")
check("两次返回同一对象", t1 is t2)
t3 = S.task_info(force=True)       # 强制查询
check("force=True 会重新查询", _calls["n"] > n1)
check("force 结果更新了缓存", S.cached_task_info() is t3)

print()
print("== 6. task_installed 走缓存 ==")
reset()
check("无缓存时 task_installed 为 False", S.task_installed() is False)
S.task_info()
check("有缓存后为 True", S.task_installed() is True)

# 还原
S._query_task_info = _orig_query
reset()

print()
print(f"结果：{OK} OK / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
