#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性探针：找出命令行里带 _test_ck / profiles\\traework 的进程。

用途：清理测试残留前，先确认没有进程占着那些目录
（否则删除会 PermissionError）。
"""
import subprocess
import sys

PS = (
    "Get-CimInstance Win32_Process | "
    "Where-Object { $_.CommandLine -like '*_test_ck*' -or "
    "$_.CommandLine -like '*traework*' } | "
    "Select-Object ProcessId,Name,CommandLine | Format-List"
)

r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS],
                   capture_output=True, timeout=90)
sys.stdout.write((r.stdout or b"").decode("utf-8", "replace"))
sys.stdout.write((r.stderr or b"").decode("utf-8", "replace"))
print("rc=", r.returncode)
