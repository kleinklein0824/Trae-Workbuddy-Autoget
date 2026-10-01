#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""积分看板的本地数据回归：不访问网络、不创建 Tk 窗口。"""
import datetime as dt
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
sys.path.insert(0, GUI)

from app import config as C  # noqa: E402
from app.pages.credits import CreditsPage  # noqa: E402

OK = FAIL = 0


def check(label, condition, extra=""):
    global OK, FAIL
    if condition:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def main():
    print("== 奖励积分解析 ==")
    check("领取文案", CreditsPage._reward("签到成功，领取 150 积分") == 150)
    check("加号文案", CreditsPage._reward("奖励 +100 积分") == 100)
    check("无奖励不误报", CreditsPage._reward("今日已签到") == 0)

    print("\n== 近 7 日本地流水统计 ==")
    today = dt.date.today()
    old = C.load_history
    try:
        C.load_history = lambda limit=None: [
            {"platform": "traecn", "ts": today.isoformat() + "T09:00:00",
             "status": "ok", "detail": "签到成功，领取 150 积分"},
            {"platform": "traecn", "ts": today.isoformat() + "T09:01:00",
             "status": "busy", "detail": "待复核"},
            {"platform": "traecn", "ts": today.isoformat() + "T09:02:00",
             "status": "fail", "detail": "网络异常"},
            {"platform": "workbuddy", "ts": today.isoformat() + "T09:03:00",
             "status": "ok", "detail": "领取 999 积分"},
            {"platform": "traecn",
             "ts": (today - dt.timedelta(days=8)).isoformat() + "T09:00:00",
             "status": "ok", "detail": "领取 999 积分"},
        ]
        page = CreditsPage.__new__(CreditsPage)
        page.module = "traecn"
        stats = page._history_stats()
    finally:
        C.load_history = old
    check("按平台过滤", stats["attempts"] == 3, repr(stats))
    check("busy 单独计数", stats["busy"] == 1 and stats["fail"] == 1, repr(stats))
    check("奖励合计", stats["reward"] == 150, repr(stats))
    check("时间窗口过滤", all(page._row_date(r) >= today - dt.timedelta(days=6)
                              for r in stats["rows"]), repr(stats))

    print(f"\n积分看板回归：{OK} OK / {FAIL} FAIL")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
