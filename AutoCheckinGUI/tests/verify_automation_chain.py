#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无人值守执行链路回归：计划任务能不能按对的身份、对的时间真的把签到跑起来，
跑完有没有右下角弹窗。

为什么必须读任务 **XML** 而不是 CIM 属性
--------------------------------------
`Get-ScheduledTask` 暴露的 CIM 属性有几个是可空的：`Repetition.Enabled` 读出来
是空的、`Settings.MultipleInstancesPolicy` 也常读成空字符串。照着这些读，会把
「补跑其实开着 / 并发策略其实是 IgnoreNew」误判成「没配」。`Export-ScheduledTask`
给的是任务计划程序真正落库的 XML，是唯一权威来源，所以这里全部按 XML 断言。

覆盖：
  0. 数据根统一（源码 / GUI 的 logs / 冻结 exe 必须落在同一个根）
  1. 注册质量（身份、触发器、补跑窗口、并发策略、动作参数、指向）
  2. 结果码翻译（`0` 是成功码且 falsy，绝不能显示成 "-"）
  3. 幂等门控（全部完成 → 静默跳过不启动浏览器；未到点 → 不执行）
  4. 真的触发一次计划任务，断言结果码与日志
  5. 冻结 exe 跑一轮**真实**签到，断言右下角真的弹出卡片窗口

用法：python tests/verify_automation_chain.py
"""
import datetime
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
sys.path.insert(0, GUI_DIR)

import app.config  # noqa: E402  定位项目根并挂上 sys.path
import checkin  # noqa: E402
import runtime  # noqa: E402
import win_tree_probe as WTP  # noqa: E402

ROOT = checkin.ROOT
NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# PowerShell 的中文输出必须先把编码钉成 UTF-8，否则按 utf-8 解码会静默吃掉
# 非 ASCII 字符（任务名/路径里的中文会变乱码，判断就全错）。
PS_UTF8 = ("try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {};"
           "$OutputEncoding = [Text.Encoding]::UTF8;")

ok = 0
fail = 0
skip = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label}  {extra}")


def note(label):
    global skip
    skip += 1
    print(f"  SKIP {label}")


def ps(script, timeout=120):
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-Command", PS_UTF8 + script],
                       capture_output=True, encoding="utf-8", errors="ignore",
                       timeout=timeout, creationflags=NO_WIN)
    return r.stdout or ""


def task_xml():
    out = ps(f"Export-ScheduledTask -TaskName '{checkin.TASK_NAME}'")
    i = out.find("<?xml")
    return out[i:] if i >= 0 else ""


def txt(node, path):
    """安全取 XML 子节点文本。

    ⚠️ 不要写成 `(node.find(p) or ET.Element("x")).text` —— `Element` 没定义
    `__bool__`，但定义了 `__len__`（子节点个数），所以**没有子节点的叶子节点
    是 falsy 的**。叶子节点恰好就是我们要取文本的那些（UserId / Interval /
    Arguments…），于是 `or` 一律走兜底分支，全部读成空串 —— 断言全 FAIL，
    看起来像"任务没配"，其实是读法错了。Python 3.12+ 会为此发
    DeprecationWarning，早期版本静默出错。
    """
    if node is None:
        return ""
    e = node.find(path)
    return (e.text or "").strip() if e is not None and e.text else ""


def iso_seconds(s):
    """ISO8601 时长 → 秒（解析失败返回 None）。

    `checkin._iso_dur_minutes` 只认 H/M，而开机延迟写的是 `PT10S`（秒级），
    用它会得到 None，断言里就变成 "None * 60" 直接抛异常。这里单独补上秒。
    """
    s = (s or "").strip().upper()
    if not s.startswith("PT"):
        return None
    m = re.fullmatch(r"(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s[2:])
    if not m or not any(m.groups()):
        return None
    h, mi, sec = (int(g or 0) for g in m.groups())
    return h * 3600 + mi * 60 + sec


def task_state():
    """返回 "LastRunTime|LastTaskResult|State"（读不到返回空串）。"""
    out = ps(f"$t = Get-ScheduledTask -TaskName '{checkin.TASK_NAME}';"
             "$i = $t | Get-ScheduledTaskInfo;"
             "[string]$i.LastRunTime + '|' + [string]$i.LastTaskResult"
             " + '|' + [string]$t.State").strip().splitlines()
    return out[-1].strip() if out else ""


# 「正在运行」的各种表示：267009 = 0x41301；0xFFFFFFFF 也是调度器表示"在跑"。
RUNNING_CODES = {"267009", "4294967295"}
# 0x800710E0 = ERROR_REQUEST_REFUSED：已有实例在跑时 Start 被调度器拒绝。
REFUSED_CODE = "2147946720"


def wait_task_idle(timeout=240):
    """等任务不再是"正在运行"。返回 (是否空闲, 最后读到的状态串)。

    为什么要等：这个任务是「每日两个触发点 + 每 30 分钟补跑」，
    `MultipleInstances=IgnoreNew` —— **"已经有一个实例在跑"是很常见的真实
    状态**。这时再 `Start-ScheduledTask` 会被调度器直接拒绝
    （`0x800710E0`），日志当然也不会新增。那是**按设计拒绝，不是缺陷**。

    不等空闲就触发，会把这种情况误报成两条失败（实测踩过一次：
    结果码非 0 + 日志没新增，看起来像"任务跑不起来"，其实是它正在跑）。
    """
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = task_state()
        parts = last.split("|")
        code = parts[1].strip() if len(parts) > 1 else ""
        state = parts[2].strip() if len(parts) > 2 else ""
        if code not in RUNNING_CODES and state != "Running":
            return True, last
        time.sleep(3)
    return False, last


def today_log():
    return os.path.join(checkin.LOG_DIR,
                        f"checkin_{datetime.datetime.now():%Y-%m-%d}.log")


def tail_log(n=3):
    p = today_log()
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8", errors="ignore") as f:
        return f.read().strip().splitlines()[-n:]


def main():
    exe = os.path.join(GUI_DIR, "dist", "TraeBuddy签到助手",
                       "TraeBuddy签到助手.exe")
    # 读窗口几何前必须声明 DPI 感知，否则 125% 缩放下会把 423 宽的卡片
    # 读成 338，尺寸断言直接失败（看起来像卡片画错了，其实是读法不对）。
    WTP.set_dpi_aware()

    # ---------------- 0. 数据根统一 ----------------
    print("== 0. 数据根统一（源码 / GUI logs / 冻结 exe 同一个根）==")
    dr = os.path.abspath(runtime.data_root() or "")
    check("data_root() == checkin.ROOT",
          os.path.normcase(dr) == os.path.normcase(os.path.abspath(ROOT)),
          f"{dr} vs {ROOT}")
    gui_logs = os.path.join(GUI_DIR, "logs")
    check("GUI 的 logs 与数据根的 logs 是同一份（junction/同目录）",
          os.path.realpath(gui_logs) == os.path.realpath(checkin.LOG_DIR),
          f"{os.path.realpath(gui_logs)} vs {os.path.realpath(checkin.LOG_DIR)}")
    if os.path.exists(exe):
        r = subprocess.run([exe, "--selftest"], capture_output=True,
                           timeout=240, creationflags=NO_WIN)
        selftest_out = ""
        for enc in ("utf-8", "gbk"):
            try:
                selftest_out = (r.stdout or b"").decode(enc)
                if "数据目录" in selftest_out:
                    break
            except Exception:  # noqa: BLE001
                continue
        m = re.search(r"数据目录\s*=\s*(.+)", selftest_out)
        check("冻结 exe 自解析的数据根 = 源码根",
              bool(m) and os.path.normcase(m.group(1).strip())
              == os.path.normcase(os.path.abspath(ROOT)),
              (m.group(1).strip() if m else "未解析到「数据目录 =」"))
    else:
        note(f"便携 exe 不存在（{exe}），跳过冻结数据根校验")

    # ---------------- 1. 计划任务注册质量（XML 权威） ----------------
    print()
    print("== 1. 计划任务注册质量（读 Export-ScheduledTask 的 XML）==")
    raw = task_xml()
    if "<Task" not in raw:
        check("能导出任务 XML（任务已注册）", False, raw.strip()[:120])
        print(f"\n结果：{ok} OK / {fail} FAIL / {skip} SKIP")
        return 1
    root = ET.fromstring(raw)
    cfg = checkin.load_config()
    sched = cfg.get("schedule", {})

    logon_type = txt(root, f"{NS}Principals/{NS}Principal/{NS}LogonType")
    principal = txt(root, f"{NS}Principals/{NS}Principal/{NS}UserId")
    check("Principal 绑定了具体账户（不是空）", bool(principal), repr(principal))
    check("LogonType = InteractiveToken（跑在用户会话里，弹窗才看得见）",
          logon_type == "InteractiveToken", logon_type)

    st = root.find(f"{NS}Settings")
    check("MultipleInstancesPolicy = IgnoreNew（不并发堆积）",
          txt(st, f"{NS}MultipleInstancesPolicy") == "IgnoreNew",
          txt(st, f"{NS}MultipleInstancesPolicy"))
    check("StartWhenAvailable = true（错过的时间点开机补跑）",
          txt(st, f"{NS}StartWhenAvailable").lower() == "true",
          txt(st, f"{NS}StartWhenAvailable"))
    check("ExecutionTimeLimit = PT0S（不限时，长流程不被杀）",
          txt(st, f"{NS}ExecutionTimeLimit") == "PT0S",
          txt(st, f"{NS}ExecutionTimeLimit"))
    check("DisallowStartIfOnBatteries = false（笔记本电池下也跑）",
          txt(st, f"{NS}DisallowStartIfOnBatteries").lower() == "false",
          txt(st, f"{NS}DisallowStartIfOnBatteries"))

    cal = root.findall(f"{NS}Triggers/{NS}CalendarTrigger")
    want_times = list(sched.get("daily_times") or ["09:00"])
    got_times = []
    for c in cal:
        m = re.search(r"T(\d{2}:\d{2})", txt(c, f"{NS}StartBoundary"))
        if m:
            got_times.append(m.group(1))
    check(f"每日定时触发器 {len(want_times)} 个，时间与配置一致",
          sorted(got_times) == sorted(want_times),
          f"任务 {sorted(got_times)} vs 配置 {sorted(want_times)}")

    # 过期补跑：**必须数值比较**，Windows 会把 PT360M 规范化成 PT6H
    ivs = [txt(c, f"{NS}Repetition/{NS}Interval") for c in cal]
    dus = [txt(c, f"{NS}Repetition/{NS}Duration") for c in cal]
    want_iv = int(sched.get("retry_interval_minutes", 30))
    want_du = int(sched.get("retry_window_minutes", 360))
    got_iv = [checkin._iso_dur_minutes(v) for v in ivs]
    got_du = [checkin._iso_dur_minutes(v) for v in dus]
    check(f"每个日触发器都挂了补跑间隔（数值 {want_iv} 分钟）",
          bool(got_iv) and all(v == want_iv for v in got_iv),
          f"原始 {ivs} → {got_iv} 分钟")
    check(f"补跑持续窗口（数值 {want_du} 分钟）",
          bool(got_du) and all(v == want_du for v in got_du),
          f"原始 {dus} → {got_du} 分钟")

    lt = root.find(f"{NS}Triggers/{NS}LogonTrigger")
    check("有开机自启触发器", lt is not None)
    if lt is not None:
        u = txt(lt, f"{NS}UserId")
        want_u = checkin._current_user()
        check("开机触发器绑到当前登录账户（不能为空）", bool(u), repr(u))
        check("开机触发器账户 = 当前账户", u.lower() == want_u.lower(),
              f"任务 {u!r} vs 当前 {want_u!r}")
        delay = txt(lt, f"{NS}Delay")
        want_d = int(sched.get("logon_delay_seconds", 10))
        check(f"开机延迟 = {want_d} 秒（等桌面起来再跑）",
              iso_seconds(delay) == want_d,
              f"{delay} → {iso_seconds(delay)} 秒")

    ev = root.find(f"{NS}Triggers/{NS}EventTrigger")
    check("有网络恢复触发器（联网即补签）", ev is not None)
    if ev is not None:
        sub = txt(ev, f"{NS}Subscription")
        check("网络触发器订阅了「网络已连接」事件（EventID=10000）",
              "10000" in sub and "NetworkProfile" in sub, sub[:80])

    act = root.find(f"{NS}Actions/{NS}Exec")
    args = txt(act, f"{NS}Arguments")
    wdir = txt(act, f"{NS}WorkingDirectory")
    check("动作参数是无人值守模式（run --silent）",
          "run" in args and "--silent" in args, args)
    check("工作目录 = 项目根（相对路径才解析得对）",
          os.path.normcase(os.path.abspath(wdir))
          == os.path.normcase(os.path.abspath(ROOT)), wdir)

    # 指向：允许是同一数据根下的别的副本（源码 / dist 便携 exe 都算），
    # 只要读写的是同一份 config.json / logs 就说明设置生效。
    try:
        from app.services import scheduler as SCH
        info = SCH.task_info()
        mism, target, mine = SCH.task_target_mismatch(info)
        check("任务指向与当前程序在同一数据根（设置能生效）", not mism,
              f"任务 {target} vs 当前 {mine}")
    except Exception as e:  # noqa: BLE001
        check("任务指向一致性可判定", False, repr(e))

    # ---------------- 2. 结果码翻译 ----------------
    print()
    print("== 2. 结果码翻译（0 是成功码，且 falsy）==")
    from app.pages.settings import SettingsPage
    check("0 -> 成功（不是 '-'）", "成功" in SettingsPage._result_text(0),
          SettingsPage._result_text(0))
    check("267011 -> 从未运行过", "从未运行过" in SettingsPage._result_text(267011))
    check("267009 -> 正在运行", "正在运行" in SettingsPage._result_text(267009))
    check("2147942402 -> 找不到程序",
          "找不到程序" in SettingsPage._result_text(2147942402))
    check("空字符串 -> '-'", SettingsPage._result_text("") == "-")
    check("成功码不会被判成「从未运行」",
          "从未运行" not in SettingsPage._task_health(
              SettingsPage.__new__(SettingsPage),
              {"lastRun": "09/16/2026 19:15:08", "result": 0})[1])

    # ---------------- 3. 幂等门控 ----------------
    print()
    print("== 3. 幂等门控（不重复开浏览器、不重复弹窗）==")
    today = datetime.date.today().isoformat()

    class _AllDone(dict):
        """「每个账号今日都已完成」的映射。

        ★ 为什么不能直接用真实 `state.json`（2026-09-24 修）：
          `_gate_skip` 只看 `state.get(key) == today`，而真实 state 里有没有
          「今天」的记录**取决于那天恰好签过** —— 它是按日期变的。
          实测 2026-09-24 那天里面是 `2026-09-23` 的记录 → 门控不命中 →
          这条断言必然红，而且看起来像真故障。
        ★ 为什么用「get 永远返回今天」而不是逐个账号填 key：
          `_gate_skip` 内部的账号枚举（traework 配置 + traecn/wb 的
          `collect_accounts`）如果在这里**复制一份**，两边迟早漂移；
          直接表达「全部已完成」这个**语义**，才是稳的。
        """

        def get(self, _k, default=None):
            return today

    reason = checkin._gate_skip(cfg, _AllDone(), today,
                                now=datetime.datetime(2026, 9, 16, 12, 0))
    check("全部账号今日已完成 → 门控命中（静默退出，不开浏览器）",
          reason is not None and "已完成" in reason, repr(reason))

    # 空 state = 每个账号都没完成 → due=True，会短路掉 TraeCN 枚举，跑得快
    # ★ 这一条同时也是上面那条的【正对照】：同一份 cfg / 同一个 now，
    #   只把「都完成」换成「都没完成」，门控就**不**命中 ——
    #   证明上面判「已完成」不是恒真的。
    r2 = checkin._gate_skip(cfg, {}, today, now=datetime.datetime(2026, 9, 16, 12, 0))
    check("有账号未完成 → 需要执行（返回 None）", r2 is None, repr(r2))
    r3 = checkin._gate_skip(cfg, {}, today, now=datetime.datetime(2026, 9, 16, 8, 0))
    check("未到当日基准时间 → 跳过执行",
          r3 is not None and "基准时间" in r3, repr(r3))

    # ---------------- 4. 真的触发一次计划任务 ----------------
    print()
    print("== 4. 真的触发一次计划任务（无人值守路径）==")
    # 先等空闲实例收尾：`IgnoreNew` 下重复触发会被**按设计拒绝**，
    # 不等就会误报（见 wait_task_idle）。等不到空闲本身就是个问题 ——
    # 说明有实例卡了很久，所以这条也断言。
    # ★ 前置条件：任务必须处于**启用**状态。
    # 「停用」是**合法状态**（用户可能主动暂停自动化，或构建前临时停掉避开
    # 触发点）。此时 `Start-ScheduledTask` 会被调度器直接忽略 →
    # LastRunTime 不变、日志不增，于是下面两条断言会报出**看起来像真故障的红**。
    # 那是"判据分不清原因"的误导（和「弱判据 / 仪器失明」同一类问题），
    # 不是缺陷。所以先查 State：停用就明确 SKIP 并说清怎么恢复，绝不误报 FAIL。
    _st = task_state()
    task_disabled = _st.split("|")[-1].strip() == "Disabled"
    if task_disabled:
        note("计划任务当前是「已停用」，无法验证无人值守触发路径"
             f"（读到 {_st!r}）—— 先跑 tests/_task_toggle.py enable 再验")
    else:
        idle, st_before = wait_task_idle(timeout=240)
        check("任务没有卡在运行中（等得到空闲实例）", idle, st_before)

        before = task_state()
        print("   触发前：", before)
        n_before = len(tail_log(400))
        ps(f"Start-ScheduledTask -TaskName '{checkin.TASK_NAME}'")
        deadline = time.time() + 150
        after, res = before, None
        while time.time() < deadline:
            time.sleep(3)
            line = task_state()
            parts = line.split("|")
            if len(parts) >= 2:
                t, r = parts[0].strip(), parts[1].strip()
                after, res = line, r
                if t != before.split("|")[0].strip() and r not in ("", "267009"):
                    break
        print("   触发后：", after)
        check("触发后 LastRunTime 变化了（任务真的被拉起来）",
              after.split("|")[0].strip() != before.split("|")[0].strip(), after)
        check("结果码 = 0（成功退出）", res == "0",
              f"{res} → {SettingsPage._result_text(res)}"
              + ("（0x800710E0 = 已有实例在跑，被 IgnoreNew 拒绝）"
                 if res == REFUSED_CODE else ""))
        lines = tail_log(6)
        print("   日志尾部：")
        for ln in lines:
            print("    ", ln)
        check("日志新增了本轮记录", len(tail_log(400)) > n_before,
              f"{n_before} → {len(tail_log(400))}")
        # ★ 门控有**两种都正确**的结果（2026-09-24 修）：
        #     ① 今日全部已签 → 「静默跳过」（不开浏览器）
        #     ② 还有账号未完成 → 正常跑完一轮并落账（「本轮完成」）
        #   两者都说明「自动化链路是通的」。旧写法把 ② 判成 FAIL ——
        #   而 ② 只是「还有账号没签完」时的正确行为。实测 WorkBuddy 令牌过期时
        #   永远到不了 ①，这条就会**一直红**，看起来像门控坏了，其实是环境使然。
        #   另外 tail_log(6) 太窄：「本轮完成」是**整轮的第一行**，
        #   账号一多就会被挤出窗口 → 也会误红。放宽到 20 行。
        _tail = tail_log(20)
        _skipped = any("静默跳过" in ln for ln in _tail)
        _ran = any("本轮完成" in ln for ln in _tail)
        check("门控生效：本轮要么静默跳过、要么正常跑完一轮并落账",
              _skipped or _ran, " | ".join(_tail[-3:]))
        if not _skipped and _ran:
            note("本轮未静默跳过 —— 说明今天还有账号未签完，门控判定「需要执行」，"
                 "这是正确行为（不是缺陷）")

    # ---------------- 5. 冻结 exe 真跑一轮 + 右下角弹窗 ----------------
    print()
    print("== 5. 冻结 exe 跑一轮真实签到：右下角必须弹出卡片 ==")
    if not os.path.exists(exe):
        note(f"便携 exe 不存在（{exe}），跳过真实轮次验证")
    else:
        import verify_toast_popup as VTP
        # 只跑 TraeCN（本地令牌接口，秒级），避开 TraeWork 的浏览器长流程；
        # --force 跳过门控，让本轮真的走到 _record_results → 弹窗
        cmd = [exe, "--run-checkin", "run", "--silent",
               "--only", "traecn", "--force"]
        print("   cmd =", " ".join(cmd[1:]))
        # ★ 把这一轮在「弹窗策略」眼里变成**当天第 1 次自动执行** ——
        #   第 1 次执行属于关键节点，**无论成败都必弹**，本节断言才有确定结论。
        #   不这么做的话结论会随"今天这台机器已经自动跑过几轮"漂移：
        #   补跑轮次里「仍有失败且不是最后一轮」按设计是**静默**的
        #   （见 checkin._maybe_notify），于是当天上游一抽风
        #   （实测 TraeCN 返回 code:1001）就会误报"没弹卡片"。
        #   子进程读的是**真实** logs/retry_state.json（没有环境变量能改道），
        #   所以这里必须直接写它，并在 finally 里原样恢复。
        _rs_path = checkin.RETRY_STATE_PATH
        try:
            with open(_rs_path, encoding="utf-8") as _f:
                _rs_saved = _f.read()
        except OSError:
            _rs_saved = None
        checkin.save_retry_state({"date": datetime.date.today().isoformat(),
                                  "attempts": 0,
                                  "notified_first_fail": False,
                                  "notified_last": False})
        n_before = len(tail_log(400))
        png = os.path.join(checkin.LOG_DIR, "notify_card.png")
        # 只取指纹，**不删文件**：沙箱的安全删除守卫累计超阈值会直接终止进程
        # （退出码 1、无 traceback），表现成"跑到这里神秘崩溃"
        png_before = VTP.snapshot_file(png)
        # 先记下现有窗口：签到进程跑完就退出了，卡片是**另一个进程**弹的，
        # 只按父 PID 找会一无所获，必须全屏差分
        baseline = {r["hwnd"] for r in WTP.windows(None)}
        proc = subprocess.Popen(cmd, creationflags=NO_WIN,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            row, alpha = VTP.probe_new_window(wait=180, exclude=baseline)
            check("自动化轮次跑完弹出了右下角卡片", row is not None,
                  "未在 180s 内发现新卡片窗口")
            if row is not None:
                check("卡片尺寸正确（宽 423、无标题栏）",
                      abs(row["w"] - 423) <= 6, f"{row['w']}x{row['h']}")
                check("卡片真的不透明（alpha≥200）", alpha >= 200, str(alpha))
                check("卡片由本程序弹出（进程名对得上）",
                      "TraeBuddy" in WTP.exe_of(row["pid"])
                      or "python" in WTP.exe_of(row["pid"]).lower(),
                      WTP.exe_of(row["pid"]))
                check("同时生成了 notify_card.png",
                      os.path.exists(png) and os.path.getsize(png) > 1000
                      and VTP.file_rewritten(png, png_before))
            else:
                print("       未发现卡片；当前可见的 Tk 顶层窗口：")
                for r in WTP.windows(None):
                    if r["cls"] in ("TkTopLevel", "Tk") and r["visible"]:
                        print("        ", r)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                pass
            check("冻结 exe 退出码 = 0", proc.returncode == 0,
                  str(proc.returncode))
            lines = tail_log(6)
            check("真实轮次写进了日志（本轮完成）",
                  any("本轮完成" in ln for ln in lines),
                  " | ".join(lines[-2:]))
        finally:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            except Exception:  # noqa: BLE001
                pass
            # 恢复补跑轮次记账（不能给用户留下被测试改过的计数）
            try:
                if _rs_saved is None:
                    checkin.save_retry_state({
                        "date": datetime.date.today().isoformat(),
                        "attempts": 0, "notified_first_fail": False,
                        "notified_last": False})
                else:
                    with open(_rs_path, "w", encoding="utf-8") as _f:
                        _f.write(_rs_saved)
            except OSError:
                pass

    print()
    print(f"结果：{ok} OK / {fail} FAIL / {skip} SKIP")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
