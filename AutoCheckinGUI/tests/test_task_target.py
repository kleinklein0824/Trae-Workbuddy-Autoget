#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计划任务「指向比对」与「PowerShell 输出编码」回归。

这两条都是**静默错**，不写断言就发现不了：

1. **编码**：中文 Windows 上 PowerShell 按 GBK 写 stdout，若按 utf-8 解码，
   `签到助手` 会变成 `ǩ`（`\\xc7\\xa9` 恰好是合法 UTF-8 序列，连异常都不抛）。
   症状：设置页把任务路径显示成乱码。断言方式 —— **路径必须真实存在**，
   乱码路径不可能存在，所以这一条能稳定抓到它。

2. **脚本模式的指向比对**：任务注册成 `pythonw.exe + "checkin.py" run --silent`
   时，`Execute` 是解释器、真入口在 `Arguments` 里。只比 `Execute` 的话
   源码模式**恒报「▲ 需检查」**（假警报）。这里覆盖三种形态：
   脚本模式（应一致）/ exe 模式指向别处（应不一致）/ exe 模式指向自己（应一致）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
sys.path.insert(0, GUI)

import app.config as C  # noqa: E402
import runtime  # noqa: E402

ROOT = runtime.data_root()
sys.path.insert(0, ROOT)

from app.services import scheduler as S  # noqa: E402

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


print("== A. 参数解析 _script_from_args ==")
check("带引号", S._script_from_args(r'"C:\a b\checkin.py" run --silent')
      == r"C:\a b\checkin.py")
check("不带引号", S._script_from_args(r"C:\a\checkin.py run --silent")
      == r"C:\a\checkin.py")
check("空串 -> 空", S._script_from_args("") == "")
check("None -> 空", S._script_from_args(None) == "")
check("只有选项 -> 取第一段", S._script_from_args("--run-checkin run") == "--run-checkin")

print()
print("== B. 解释器识别 _is_python_interp ==")
for name in ("python.exe", "pythonw.exe", "py.exe", "pyw.exe", "PYTHONW.EXE"):
    check(f"{name} 是解释器", S._is_python_interp(rf"C:\Py\{name}"))
check("应用 exe 不是解释器",
      not S._is_python_interp(r"C:\App\TraeBuddy签到助手.exe"))
check("空值不是解释器", not S._is_python_interp(""))

print()
print("== C. 目录树判定 _under ==")
check("同路径算在内", S._under(ROOT, ROOT))
check("子目录算在内", S._under(os.path.join(ROOT, "logs", "a.txt"), ROOT))
check("树外不算", not S._under(r"C:\Windows\notepad.exe", ROOT))
check("前缀相同但不是子目录不算",
      not S._under(ROOT + "2\\x.py", ROOT))
check("空值不算", not S._under("", ROOT) and not S._under(ROOT, ""))

print()
print("== D. 指向比对 task_target_mismatch ==")
mine = S.current_program()
check("current_program 非空", bool(mine), repr(mine))
check("current_program 指向 checkin.py", mine.lower().endswith("checkin.py"), mine)

# D1 脚本模式：解释器 + 脚本 = 本项目 -> 一致
info = {"exe": r"C:\Program Files\Python313\pythonw.exe",
        "args": f'"{mine}" run --silent'}
mis, tgt, cur = S.task_target_mismatch(info)
check("脚本模式（指向本项目）判为一致", mis is False, f"{mis} {tgt}")
check("  返回的『任务指向』是脚本而非 pythonw", tgt.lower().endswith("checkin.py"), tgt)

# D2 便携版 exe（同在数据根目录树内）-> 也算同一套配置，不该报警
#    这是最容易误报的组合：任务指 dist 里的便携版，GUI 跑的是源码。
#    两者都会把 runtime.data_root() 解析到项目根，读写同一份 config/logs。
portable = os.path.join(GUI, "dist", "TraeBuddy签到助手", "TraeBuddy签到助手.exe")
mis, tgt, cur = S.task_target_mismatch(
    {"exe": portable, "args": "--run-checkin run --silent"})
check("便携版（同数据根树下）判为一致（不误报）", mis is False, f"{mis} {tgt}")

# D3 数据根树**外**的另一个副本 -> 必须报（这才是真故障）
outside = r"C:\Users\someone\Desktop\AnotherCopy\TraeBuddy签到助手.exe"
mis, tgt, cur = S.task_target_mismatch(
    {"exe": outside, "args": "--run-checkin run --silent"})
check("树外副本判为不一致（真故障要报）", mis is True)
check("  返回的『任务指向』就是那个副本", tgt == outside, tgt)

# D4 exe 模式指向自己 -> 一致
mis, _, _ = S.task_target_mismatch({"exe": mine, "args": "--run-checkin run --silent"})
check("exe 模式指向自己判为一致", mis is False)

# D5 反方向：GUI 是便携 exe、任务指向源码 -> 也应一致
#    （冻结模式下 current_program() 返回 exe，这里用打桩模拟）
_real_cp = S.current_program
try:
    S.current_program = lambda: portable
    mis, tgt, cur = S.task_target_mismatch(
        {"exe": r"C:\Program Files\Python313\pythonw.exe",
         "args": f'"{mine}" run --silent'})
    check("便携 exe 当 GUI + 任务指向源码 -> 一致", mis is False, f"{mis} {tgt}")
finally:
    S.current_program = _real_cp

# D6 信息缺失时不误报
check("exe 为空 -> 不报不一致", S.task_target_mismatch({"exe": "", "args": ""})[0] is False)

print()
print("== E. PowerShell 输出编码（真机） ==")
out, err = S._powershell("Write-Output 'TraeBuddy签到助手'")
check("中文原样返回", "签到助手" in out, repr(out[:80]))
check("没有替换字符 U+FFFD", "\ufffd" not in out)
check("没有丢字（长度为 15）", out.strip() == "TraeBuddy签到助手", repr(out.strip()))

info = S.task_info(force=True)
if not info.get("installed"):
    print("  SKIP 计划任务未安装，跳过真实路径断言")
else:
    exe = str(info.get("exe") or "")
    args = str(info.get("args") or "")
    target = S._script_from_args(args) if S._is_python_interp(exe) else exe
    print(f"  任务 exe  = {exe}")
    print(f"  任务 args = {args}")
    print(f"  还原入口  = {target}")
    check("还原出的入口真实存在（乱码路径不可能存在）",
          os.path.exists(target), target)
    check("任务路径里没有替换字符", "\ufffd" not in exe + args)
    if "签到助手" in target:
        check("路径中的中文解码正确（含『签到助手』）", "签到助手" in target, target)

print()
print(f"结果：{ok} OK / {fail} FAIL")
sys.exit(0 if fail == 0 else 1)
