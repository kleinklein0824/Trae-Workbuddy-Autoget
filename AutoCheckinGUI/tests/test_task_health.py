#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证任务健康诊断：从未运行 / 运行失败 / 正常 三种状态各自的呈现。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.pages.settings import SettingsPage  # noqa: E402

ok = 0
fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label}  {extra}")


print("== _result_text 结果码翻译 ==")
check("0 -> 成功", "成功" in SettingsPage._result_text(0))
check("267011 -> 从未运行过", "从未运行过" in SettingsPage._result_text(267011))
check("'267011' 字符串也能翻译", "从未运行过" in SettingsPage._result_text("267011"))
check("267009 -> 正在运行", "正在运行" in SettingsPage._result_text(267009))
check("2147942402 -> 找不到程序", "找不到程序" in SettingsPage._result_text(2147942402))
check("未知码带十六进制", "0x" in SettingsPage._result_text(999999) or
      "999999" in SettingsPage._result_text(999999))
check("None -> '-'", SettingsPage._result_text(None) == "-")
check("'' -> '-'", SettingsPage._result_text("") == "-")

print()
print("== _task_health 健康判定 ==")
page = SettingsPage.__new__(SettingsPage)   # 不初始化 Tk，只测纯逻辑

h, txt = page._task_health({"lastRun": "11/30/1999 00:00:00", "result": 267011})
check("1999 从未运行 -> 不健康", h is False)
check("  说明里点明从未运行", "从未成功运行过" in txt or "从未" in txt)

h, txt = page._task_health({"lastRun": "09/16/2026 01:00:00", "result": 2147942402})
check("结果码非 0 -> 不健康", h is False)
check("  说明里带失败码含义", "找不到程序" in txt)

h, txt = page._task_health({"lastRun": "09/16/2026 01:00:00", "result": 0})
check("结果码 0 -> 健康", h is True)
check("  健康时无告警文本", txt == "")

h, txt = page._task_health({"lastRun": "09/16/2026 01:00:00", "result": 267009})
check("正在运行 -> 健康（不误报失败）", h is True)

h, txt = page._task_health({"lastRun": "", "result": ""})
check("空值 -> 不健康（当成从未运行）", h is False)

print()
print(f"结果：{ok} OK / {fail} FAIL")
sys.exit(0 if fail == 0 else 1)
