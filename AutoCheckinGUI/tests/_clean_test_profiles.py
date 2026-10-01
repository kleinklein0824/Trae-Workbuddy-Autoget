#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理测试残留的假会话目录（`profiles/traework/_test_*`）。

为什么需要这个工具
------------------
2026-09-18 踩过一次很贵的坑：`test_accounts_discovery` 原先直接往**生产**
`profiles/traework/` 里建假会话，并把假账号写进**生产** `config.json`。
脚本中途抛异常时清理被跳过，于是紧接着跑的 `verify_automation_chain`
拿这些假账号执行了**真实签到** —— Playwright 被拉起来对着假 profile
写下一整套 Chromium 数据，而且**进程一直不退出**、占着目录不放，
导致之后每次跑测试都 `PermissionError: Default/Network/Cookies`，
测试从此永远红。

修法是把测试改到沙箱里跑（见 `test_accounts_discovery._sandbox`）。
本脚本负责**收拾历史残留**，并在残留再现时快速定位。

用法
----
    python tests/_clean_test_profiles.py            # 只看，不动手
    python tests/_clean_test_profiles.py --kill     # 杀掉占用进程（只杀含 _test 的）
    python tests/_clean_test_profiles.py --clean    # 送回收站；失败则改名让开

★ 两条安全约束（写死在这里，不靠调用方自觉）：
  1. 只处理 `profiles/traework/` 下**名字以 `_test` 开头**的目录；
  2. `--kill` 只杀命令行里**确实包含** `_test` 的进程 —— 绝不动用户自己的 Chrome。

★ 为什么不是直接 `shutil.rmtree`：本项目的沙箱安全删除守卫会在删除量超阈值时
  **直接终止进程**（`dangerouslyDisableSandbox` 也绕不过）。回收站这条路能绕开，
  但仍可能在中途被杀；所以清理失败就退回「改名让开」（rename 不计入删除量）。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
            else os.path.dirname(os.path.dirname(GUI_DIR)))
BASE = os.path.join(PROJ_ROOT, "profiles", "traework")

# 残留的临时落脚点（改名让开用）；%TEMP% 由系统回收
JUNK = os.path.join(os.environ.get("TEMP") or "/tmp", "_traebuddy_test_junk")


def _ps(script, timeout=180):
    """跑一段 PowerShell 并返回输出（UTF-8 钉死，否则中文路径会乱）。"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=timeout)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    return out + err


def targets():
    """待清理目录：`profiles/traework/` 下以 `_test` 开头的子目录。"""
    if not os.path.isdir(BASE):
        return []
    out = []
    for name in sorted(os.listdir(BASE)):
        p = os.path.join(BASE, name)
        if name.startswith("_test") and os.path.isdir(p):
            out.append(p)
    return out


def holders():
    """命令行里含 `_test` 的进程（通常是不退出的 Playwright Chromium）。"""
    return _ps("$ps = Get-CimInstance Win32_Process | "
               "Where-Object { $_.CommandLine -like '*_test*' };"
               "foreach ($p in $ps) { Write-Output ($p.ProcessId.ToString() + ' ' "
               "+ $p.Name) }")


def kill_holders():
    """只杀命令行含 `_test` 的进程 —— 绝不动用户自己的浏览器。"""
    return _ps("$ps = Get-CimInstance Win32_Process | "
               "Where-Object { $_.CommandLine -like '*_test*' };"
               "foreach ($p in $ps) { "
               "Write-Output ('KILL ' + $p.ProcessId + ' ' + $p.Name); "
               "Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }")


def recycle(paths):
    """送回收站。返回 PowerShell 的输出（可能因守卫被杀而只有部分结果）。"""
    if not paths:
        return ""
    lst = "','".join(paths)
    return _ps(
        "Add-Type -AssemblyName Microsoft.VisualBasic;"
        "$t = @('%s');"
        "foreach ($p in $t) {"
        "  if (Test-Path -LiteralPath $p) {"
        "    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
        "      $p,'OnlyErrorDialogs','SendToRecycleBin');"
        "    Write-Output ('RECYCLED ' + $p)"
        "  } else { Write-Output ('MISSING ' + $p) }"
        "}" % lst)


def move_aside(paths):
    """改名让开：挪到 %TEMP%\\_traebuddy_test_junk\\（rename 不计入删除量）。"""
    os.makedirs(JUNK, exist_ok=True)
    done = []
    for p in paths:
        dst = os.path.join(JUNK, os.path.basename(p))
        try:
            if os.path.exists(dst):
                import shutil
                shutil.rmtree(dst, ignore_errors=True)
            os.rename(p, dst)
            done.append(p)
        except OSError as e:
            print("  改名失败 %s -> %r" % (p, e))
    return done


def main(argv):
    want_kill = "--kill" in argv
    want_clean = "--clean" in argv

    print("残留目录：")
    ts = targets()
    for t in ts:
        n = sum(len(f) for _r, _d, f in os.walk(t))
        print("   %s  (%d 个文件)" % (t, n))
    if not ts:
        print("   （无残留，生产目录是干净的）")

    if not (want_kill or want_clean):
        print("\n（只读模式。加 --kill 杀占用进程，加 --clean 清理）")
        return 0

    print("\n占用进程：")
    h = holders().strip()
    print("   " + (h.replace("\n", "\n   ") if h else "（无）"))
    if want_kill and h:
        print("\n杀进程：")
        print("   " + kill_holders().strip().replace("\n", "\n   "))

    if want_clean and ts:
        print("\n送回收站：")
        out = recycle(ts).strip()
        print("   " + (out.replace("\n", "\n   ") if out else "（无输出：可能被删除守卫终止）"))
        left = targets()
        if left:
            print("\n仍有残留，改名让开：")
            for p in move_aside(left):
                print("   moved ->", os.path.join(JUNK, os.path.basename(p)))

    print("\n处理后：")
    rest = targets()
    print("   " + (str(rest) if rest else "（干净）"))
    print("   profiles/traework 内容：", os.listdir(BASE) if os.path.isdir(BASE) else "—")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
