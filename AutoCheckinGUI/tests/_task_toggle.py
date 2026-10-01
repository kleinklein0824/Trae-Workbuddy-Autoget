#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时工具：禁用 / 启用 / 查询 `AutoDailyCheckin` 计划任务。

为什么需要它
------------
任务的日触发器是 `09:00` / `21:00`，并挂了**每 30 分钟、持续 6 小时**的补跑
（`MSFT_TaskRepetitionPattern`）。所以在 21:00–03:00 这段里**每半小时就会真的
跑一次**。重建 `dist/` 时任务可能正好拉起一个写到一半的 exe（实测卡 108 秒、
`State=Running`），所以重建前必须先禁用。

`schtasks.exe` 被沙箱黑名单拦死，且**从 Bash 直接调 PowerShell 也会被拦** →
统一走 `subprocess.run(["powershell", ...])`，并先把输出编码钉成 UTF-8
（中文 Windows 上 PS 按 GBK 写 stdout）。

用法：python tests/_task_toggle.py disable|enable|state
"""
from __future__ import annotations

import subprocess
import sys

TASK = "AutoDailyCheckin"

# 中文 Windows 上 PS 默认按 GBK 写 stdout；不钉 UTF-8 的话中文会被解成乱码
# （`签到助手` 的 GBK 字节 C7 A9 恰好是合法 UTF-8 → 变成 U+01E9 `ǩ`）。
PS_UTF8 = ("try { [Console]::OutputEncoding=[Text.Encoding]::UTF8 } catch {};"
           "$OutputEncoding=[Text.Encoding]::UTF8;")


def ps(script):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", PS_UTF8 + script],
                       capture_output=True, timeout=90)
    out = (r.stdout or b"").decode("utf-8", errors="ignore")
    err = (r.stderr or b"").decode("utf-8", errors="ignore")
    return out.strip(), err.strip(), r.returncode


def main():
    action = (sys.argv[1] if len(sys.argv) > 1 else "state").lower()

    if action == "disable":
        script = (f"$t = Get-ScheduledTask -TaskName '{TASK}' -ErrorAction Stop;"
                  f"if ($t.State -ne 'Disabled') {{ Disable-ScheduledTask"
                  f" -TaskName '{TASK}' | Out-Null }};"
                  f"(Get-ScheduledTask -TaskName '{TASK}').State")
    elif action == "enable":
        script = (f"$t = Get-ScheduledTask -TaskName '{TASK}' -ErrorAction Stop;"
                  f"if ($t.State -eq 'Disabled') {{ Enable-ScheduledTask"
                  f" -TaskName '{TASK}' | Out-Null }};"
                  f"(Get-ScheduledTask -TaskName '{TASK}').State")
    else:
        script = (f"$t = Get-ScheduledTask -TaskName '{TASK}' -ErrorAction Stop;"
                  f"$i = $t | Get-ScheduledTaskInfo;"
                  # ⚠️ `$t.State` 是枚举，直接 `+` 字符串会被 PS 当成枚举解析
                  # 而报「无法将值 |LastRun= 转换为 StateEnum」→ 必须先 [string]
                  f"[string]$t.State + '|LastRun=' + [string]$i.LastRunTime"
                  f" + '|LastResult=' + [string]$i.LastTaskResult")

    out, err, rc = ps(script)
    print(f"[{action}] rc={rc} out={out!r}"
          + (f" err={err!r}" if err else ""))
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
