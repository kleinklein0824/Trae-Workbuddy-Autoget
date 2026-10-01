#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性工具：杀掉命令行里带 `_test_ck` 的残留 Playwright Chromium。

★ 只杀命令行**确实包含** `_test_ck` 的进程 —— 绝不动用户自己的 Chrome。
（`verify_automation_chain` 曾对着测试残留的假账号跑真实签到，
拉起的浏览器会一直占着 profiles/traework/_test_ck_*，导致后续测试 PermissionError。）
"""
import subprocess
import sys

PS = (
    "$ps = Get-CimInstance Win32_Process | "
    "Where-Object { $_.CommandLine -like '*_test_ck*' };"
    "foreach ($p in $ps) { Write-Output ('KILL ' + $p.ProcessId + ' ' + $p.Name); "
    "Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue };"
    "Write-Output ('TOTAL ' + @($ps).Count)"
)

r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS],
                   capture_output=True, timeout=120)
sys.stdout.write((r.stdout or b"").decode("utf-8", "replace"))
sys.stdout.write((r.stderr or b"").decode("utf-8", "replace"))
print("rc=", r.returncode)
