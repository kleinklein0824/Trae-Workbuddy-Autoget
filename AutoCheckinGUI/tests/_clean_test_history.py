#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性：清掉 history.jsonl / 签到记录.csv 里测试残留的 `_test*` 账号记录。

背景：`test_accounts_discovery` 曾把假会话写进**生产** config，
计划任务于是对着假账号跑了真实签到（17:01 / 17:08 两条 fail）。
测试已改成沙箱，这两条历史记录属于遗留噪音，会误导用户看签到记录。

做法（安全、可回滚）：
  1. 先备份 `history.jsonl` 与 `签到记录.csv`（`.testjunk.bak`）；
  2. 过滤 `history.jsonl` 中 account 以 `_test` 开头的行；
  3. 调 `checkin._append_records([True])` **复用真实重建逻辑**刷新 CSV
     （该函数只把 `results` 当作「要不要干活」的开关，内容全部从
       history.jsonl 重建，所以传个非空列表不会多写一行）。
"""
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)

import checkin  # noqa: E402

HIST = checkin.HISTORY_PATH
CSV = checkin.RECORD_PATH

print("history:", HIST)
print("csv    :", CSV)

for p in (HIST, CSV):
    if os.path.isfile(p):
        bak = p + ".testjunk.bak"
        shutil.copy2(p, bak)
        print("  已备份 ->", bak)

# 过滤
kept, dropped = [], []
try:
    with open(HIST, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                e = json.loads(s)
            except ValueError:
                kept.append(s)
                continue
            if str(e.get("account", "")).startswith("_test"):
                dropped.append(s)
            else:
                kept.append(s)
except OSError as e:
    print("读 history 失败：", e)
    sys.exit(1)

print("\n将删除 %d 行：" % len(dropped))
for d in dropped:
    print("   ", d[:140])

with open(HIST, "w", encoding="utf-8") as f:
    for s in kept:
        f.write(s + "\n")
print("已写回 history.jsonl（%d 行）" % len(kept))

# 复用真实重建逻辑刷新 CSV（results 只当开关用）
checkin._append_records([True])
print("已重建 签到记录.csv")

# 复核
bad = 0
with open(CSV, encoding="utf-8-sig") as f:
    for ln in f:
        if "_test" in ln:
            bad += 1
print("复核：CSV 里剩余 _test 行数 =", bad)
