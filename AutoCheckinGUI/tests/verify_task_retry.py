#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证计划任务的「过期补跑窗口」是否真的落到了触发器上。

用户诉求：过了设定时间要能自动重签，别因断网 / 开机晚而错过。
实现方式：给每个「每日定时」触发器加 Repetition（每 N 分钟重试，持续 M 小时），
门控保证幂等（全部完成则秒退）。

这个脚本会读真实计划任务，属于集成验证（需要任务已注册）。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
            else os.path.dirname(os.path.dirname(GUI_DIR)))
for _p in (PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import checkin  # noqa: E402

OK = FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {extra}")


PS = r"""
$t = Get-ScheduledTask -TaskName 'AutoDailyCheckin' -ErrorAction SilentlyContinue
if (-not $t) { Write-Output 'NOT_INSTALLED'; exit }
$i = $t | Get-ScheduledTaskInfo
Write-Output ('LAST=' + $i.LastRunTime)
Write-Output ('RESULT=' + $i.LastTaskResult)
Write-Output ('NEXT=' + $i.NextRunTime)
$n = 0
foreach ($tr in $t.Triggers) {
  $n++
  $cn = $tr.CimClass.CimClassName
  $iv = ''
  $du = ''
  if ($tr.Repetition) { $iv = [string]$tr.Repetition.Interval; $du = [string]$tr.Repetition.Duration }
  Write-Output ("TRIG[$n]=" + $cn + "|enabled=" + $tr.Enabled + "|start=" + $tr.StartBoundary + "|iv=" + $iv + "|du=" + $du + "|user=" + $tr.UserId)
}
$xml = Export-ScheduledTask -TaskName 'AutoDailyCheckin'
$d = [xml]$xml
foreach ($c in $d.Task.Triggers.ChildNodes) {
  $iv = ''
  $du = ''
  if ($c.Repetition) { $iv = $c.Repetition.Interval; $du = $c.Repetition.Duration }
  Write-Output ("XML=" + $c.Name + "|start=" + $c.StartBoundary + "|iv=" + $iv + "|du=" + $du)
}
$p = $d.Task.Actions.Exec
Write-Output ('EXEC=' + $d.Task.Actions.Exec.Command)
Write-Output ('ARGS=' + $d.Task.Actions.Exec.Arguments)
Write-Output ('WORKDIR=' + $d.Task.Actions.Exec.WorkingDirectory)
"""

r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS],
                   capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=150)
out = r.stdout or ""
print("---- 任务实况 ----")
print(out.strip())
print("------------------")
print()

cfg = checkin.load_config()
sched = cfg.get("schedule", {})
want_on = bool(sched.get("retry_enabled", True))
want_iv = int(sched.get("retry_interval_minutes", 30))
want_win = int(sched.get("retry_window_minutes", 360))
print(f"配置：retry_enabled={want_on}  间隔={want_iv}分  窗口={want_win}分")
print()

if "NOT_INSTALLED" in out:
    check("计划任务已注册", False, "任务不存在")
else:
    check("计划任务已注册", True)
    lines = [ln for ln in out.splitlines() if ln.startswith("TRIG[")]
    xmls = [ln for ln in out.splitlines() if ln.startswith("XML=")]
    dailies = [ln for ln in lines if "DailyTrigger" in ln]
    check("有 2 个每日触发器", len(dailies) == 2, f"实际 {len(dailies)}")
    check("有 1 个登录触发器", any("LogonTrigger" in ln for ln in lines))
    check("有 1 个事件触发器（网络恢复）", any("EventTrigger" in ln for ln in lines))

    # Repetition 只应落在「每日定时」触发器上（登录/事件触发器不该有）
    daily_xml, other_xml = [], []
    for ln in xmls:
        if "|iv=" not in ln:
            continue
        (daily_xml if "CalendarTrigger" in ln else other_xml).append(ln)

    def _dur(ln, key):
        part = ln.split(f"|{key}=", 1)[1]
        return part.split("|", 1)[0].strip()

    if want_on:
        ivs = [_dur(ln, "iv") for ln in daily_xml]
        dus = [_dur(ln, "du") for ln in daily_xml]
        check("每个每日触发器都带补跑间隔", bool(ivs) and all(ivs for iv in ivs),
              f"实际 {ivs}")
        mins = [checkin._iso_dur_minutes(v) for v in ivs]
        wins = [checkin._iso_dur_minutes(v) for v in dus]
        check("补跑间隔 = 配置值", bool(mins) and all(m == want_iv for m in mins),
              f"实际 {mins} 期望 {want_iv}")
        check("补跑窗口 = 配置值", bool(wins) and all(w == want_win for w in wins),
              f"实际 {wins} 期望 {want_win}")
        check("登录/事件触发器不带补跑（符合预期）",
              all(not _dur(ln, "iv") for ln in other_xml),
              f"实际 {[_dur(ln, 'iv') for ln in other_xml]}")
    else:
        all_ivs = [_dur(ln, "iv") for ln in daily_xml + other_xml]
        check("未开启补跑时不应有 Repetition", all(not v for v in all_ivs),
              f"实际 {all_ivs}")

    # 门控幂等：全部完成后必须秒退（这是"每 30 分钟重试"零成本的前提）
    today = __import__("datetime").date.today().isoformat()
    st = checkin.load_state()
    gate_cfg = json.loads(json.dumps(cfg))
    # 构造"全部已完成"的 state
    done_state = {}
    for a in gate_cfg.get("traework", {}).get("accounts", []):
        done_state[f"traework:{a.get('name') or a.get('profile') or 'default'}"] = today
    try:
        import tc_checkin
        if gate_cfg.get("traecn", {}).get("enabled", True):
            for acc in tc_checkin.collect_accounts(gate_cfg, lambda *a: None):
                done_state[f"traecn:{acc['uid']}"] = today
    except Exception as e:  # noqa: BLE001
        print("   (traecn 枚举跳过:", e, ")")
    try:
        import wb_checkin
        for acc in wb_checkin._collect_accounts(gate_cfg, lambda *a: None):
            done_state[f"workbuddy:{acc['uid']}"] = today
    except Exception as e:  # noqa: BLE001
        print("   (workbuddy 枚举跳过:", e, ")")
    import datetime as _dt
    reason = checkin._gate_skip(gate_cfg, done_state, today,
                                now=_dt.datetime(2026, 9, 16, 10, 30))
    check("全部完成时门控跳过（幂等，补跑不会重复开浏览器）",
          bool(reason), f"reason={reason!r}")
    # 未完成时应放行
    reason2 = checkin._gate_skip(gate_cfg, {}, today,
                                 now=_dt.datetime(2026, 9, 16, 10, 30))
    check("未完成时门控放行", reason2 is None, f"reason={reason2!r}")
    # 过了基准时间就该跑（用户诉求核心）
    reason3 = checkin._gate_skip(gate_cfg, {}, today,
                                 now=_dt.datetime(2026, 9, 16, 9, 0))
    check("刚到设定时间即放行", reason3 is None, f"reason={reason3!r}")
    # 未到基准时间不放行
    reason4 = checkin._gate_skip(gate_cfg, {}, today,
                                 now=_dt.datetime(2026, 9, 16, 7, 30))
    check("未到设定时间不放行", bool(reason4), f"reason={reason4!r}")

# ---------------------------------------------------------------------------
# 自动补跑设置（2026-09-18 新增）：用户诉求「过了设定时间要能自动补签，
# 直到真的签完为止，签完即停」，并要在设置页里可调。
# ---------------------------------------------------------------------------
print()
print("== 自动补跑设置（cmd_setretry / 设置页）==")
import contextlib  # noqa: E402
import io  # noqa: E402


def _now_sched():
    return checkin.load_config().get("schedule", {})


def _run_setretry(iv, win, enabled=True):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = checkin.cmd_setretry(iv, win, enabled)
    return rc, buf.getvalue()


def _triple():
    s = _now_sched()
    return (s.get("retry_enabled", True),
            s.get("retry_interval_minutes", 30),
            s.get("retry_window_minutes", 360))


_orig = _triple()
try:
    # ① 越界必须被拒，且**不能**改动配置
    rc, _ = _run_setretry(0, 360)
    check("① 间隔 0 被拒", rc == 1, f"rc={rc}")
    rc, _ = _run_setretry(400, 360)
    check("① 间隔 400 被拒", rc == 1, f"rc={rc}")
    rc, _ = _run_setretry(30, 10)
    check("① 窗口小于间隔被拒（否则只可能重试一次）", rc == 1, f"rc={rc}")
    check("① 被拒时配置原封不动", _triple() == _orig, f"now={_triple()}")

    # ② 正常写入 → 回读一致。**故意用与当前不同的值**，否则"写进去了"证明不了。
    rc, out = _run_setretry(15, 180)
    check("② 设定 15/180 成功", rc == 0, f"rc={rc} {out.strip()[:120]}")
    s = _now_sched()
    check("② 回读间隔 = 15", s.get("retry_interval_minutes") == 15,
          f"实际 {s.get('retry_interval_minutes')}")
    check("② 回读窗口 = 180", s.get("retry_window_minutes") == 180,
          f"实际 {s.get('retry_window_minutes')}")

    # ③ 关闭开关也要能落盘（UI 上是个 Switch）
    rc, out = _run_setretry(30, 360, enabled=False)
    check("③ 关闭自动补跑成功", rc == 0, f"rc={rc}")
    check("③ 回读 retry_enabled = False", _now_sched().get("retry_enabled") is False,
          f"实际 {_now_sched().get('retry_enabled')}")
finally:
    # ④ 恢复原值 —— 验证脚本不能给用户留下副作用（中断也要恢复）
    _run_setretry(_orig[1], _orig[2], _orig[0])

check("④ 已恢复原设置", _triple() == _orig, f"now={_triple()} 期望 {_orig}")

# ⑤ 设置页确实有这块 UI；config 默认值也要齐全（缺键 UI 会读不到 → 退回硬编码）
_settings = io.open(os.path.join(GUI_DIR, "app", "pages", "settings.py"),
                    encoding="utf-8").read()
check("⑤ 设置页有「自动补跑」卡片", "自动补跑" in _settings)
check("⑤ 设置页有保存入口 _save_retry", "def _save_retry" in _settings)
check("⑤ 保存走 scheduler.set_retry（统一重注册一次）",
      "scheduler.set_retry(" in _settings)
check("⑤ 说明文字用了 auto_wrap（长文案不换行会被裁掉）",
      "_retry_hint" in _settings and "W.auto_wrap" in _settings)
import app.config as _C  # noqa: E402
_dc = _C.DEFAULT_CONFIG.get("schedule", {})
for _k in ("retry_enabled", "retry_interval_minutes", "retry_window_minutes"):
    check(f"⑤ DEFAULT_CONFIG 含 {_k}", _k in _dc, f"实际键 {sorted(_dc)}")

# ---------------------------------------------------------------------------
# 弹窗与自动补跑「同步搭配」（2026-09-18 追加）
#   诉求：上面的补跑设置要同步搭配自动化弹窗 ——
#     ① 弹窗要能看出「这是第几次自动补跑 / 还会不会再来 / 这次是补跑补上的」
#     ② 补跑不能把弹窗变成刷屏 → options.retry_toast_quiet 只在关键节点弹
#   全部用桩替换 _notify_summary / _spawn_toast，绝不真弹窗、不碰真实数据文件。
# ---------------------------------------------------------------------------
print()
print("== 补跑弹窗策略（_maybe_notify / _notify_summary）==")
import tempfile  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="tb_retry_notify_")
_PATHS = ("RETRY_STATE_PATH", "STATE_PATH", "HISTORY_PATH", "RECORD_PATH", "LOG_DIR")
_orig_paths = {k: getattr(checkin, k) for k in _PATHS}
_orig_load_config = checkin.load_config
_orig_notify = checkin._notify_summary
_orig_spawn = checkin._spawn_toast
_orig_is_last = checkin._is_last_attempt
_orig_silent = checkin.SILENT
_orig_maybe = checkin._maybe_notify

for _k in _PATHS:
    setattr(checkin, _k, os.path.join(_TMP, _k.lower() + ".tmp"))
checkin.LOG_DIR = _TMP
checkin.STATE_PATH = os.path.join(_TMP, "state.json")
checkin.HISTORY_PATH = os.path.join(_TMP, "history.jsonl")
checkin.RECORD_PATH = os.path.join(_TMP, "rec.csv")
checkin.RETRY_STATE_PATH = os.path.join(_TMP, "retry_state.json")

_CALLS = []      # _notify_summary 收到的 (attempt, last, statuses)
_PAYLOADS = []   # _spawn_toast 收到的 payload 字符串
_LAST = {"v": False}


def _cfg(quiet=True, retry_on=True, iv=30, win=360, toast=True,
         times=("09:00", "21:00")):
    return {
        "schedule": {"daily_times": list(times), "checkin_time": times[0],
                     "retry_enabled": retry_on,
                     "retry_interval_minutes": iv, "retry_window_minutes": win},
        "options": {"toast_notify": toast, "retry_toast_quiet": quiet},
    }


def _res(*statuses):
    return [dict(ts="2026-09-18T09:00:00", platform="traework",
                 account="default", status=s, detail="测试") for s in statuses]


def _pop(results, cfg, silent=True):
    """跑一次 _maybe_notify；返回是否弹了窗。"""
    _CALLS.clear()
    checkin.load_config = lambda: cfg
    checkin.SILENT = silent
    checkin._maybe_notify(results)
    return bool(_CALLS)


def _attempts():
    return int(checkin.load_retry_state().get("attempts") or 0)


try:
    checkin._notify_summary = lambda results, attempt=1, last=False: _CALLS.append(
        (attempt, last, [r["status"] for r in results]))
    checkin._is_last_attempt = lambda cfg, now=None: _LAST["v"]

    # ① 总开关关掉 → 一律不弹
    check("① toast_notify=False 时不弹窗",
          not _pop(_res("ok"), _cfg(toast=False)), "仍然弹了")

    # ② 手动执行永远弹，且**不计入**补跑轮次（否则用户点几次计数就偏了）
    checkin.save_retry_state({"date": __import__("datetime").date.today().isoformat(),
                              "attempts": 0})
    check("② 手动执行弹窗", _pop(_res("ok"), _cfg(), silent=False), "没弹")
    check("② 手动执行不计入补跑轮次", _attempts() == 0, f"attempts={_attempts()}")

    # ③ 自动执行第 1 次（有失败）→ 必弹，且标为第 1 次
    _LAST["v"] = False
    check("③ 第 1 次自动执行弹窗", _pop(_res("fail"), _cfg()), "没弹")
    check("③ 第 1 次弹窗 attempt=1", _CALLS and _CALLS[-1][0] == 1, f"{_CALLS}")
    check("③ 计数 = 1", _attempts() == 1, f"attempts={_attempts()}")

    # ④ 第 2 次（仍失败、且不是最后一轮）→ 静默，这是防刷屏的核心
    check("④ 中间失败轮次静默（仅关键节点策略）",
          not _pop(_res("fail"), _cfg()), "仍然弹了")
    check("④ 静默也照样计数", _attempts() == 2, f"attempts={_attempts()}")

    # ⑤ 正对照：关掉「仅关键节点」→ 同一轮**必须**弹
    #    （否则 ④ 的"没弹"可能只是因为函数从来不弹，检查就是死的）
    check("⑤ 正对照：关闭该策略后中间轮次会弹",
          _pop(_res("fail"), _cfg(quiet=False)), "关掉策略也不弹 = ④ 无意义")

    # ⑥ 全部签完 → 必弹（好消息不能被静默掉）
    check("⑥ 全部签完必弹", _pop(_res("ok", "skip"), _cfg()), "没弹")
    check("⑥ 全成功时标为第 4 次（③④⑤各记一轮）",
          _CALLS and _CALLS[-1][0] == 4, f"{_CALLS}")

    # ⑦ 补跑窗口最后一轮仍失败 → 必弹（要提醒用户手动处理）
    _LAST["v"] = True
    check("⑦ 最后一轮仍失败必弹", _pop(_res("fail"), _cfg()), "没弹")
    check("⑦ 最后一轮标记 last=True", _CALLS and _CALLS[-1][1] is True, f"{_CALLS}")
    # 同一关键节点不重复弹（任务被重复触发时不该连弹两下）
    check("⑦ 同一关键节点不重复弹", not _pop(_res("fail"), _cfg()), "又弹了一次")

    # ⑧ 记账跨天清零
    checkin.save_retry_state({"date": "1999-01-01", "attempts": 99})
    _LAST["v"] = False
    _pop(_res("fail"), _cfg())
    check("⑧ 跨天计数清零后重新从 1 开始", _attempts() == 1, f"attempts={_attempts()}")

    # ---- 文案：真的跑 _notify_summary，只把弹窗进程换成记录器 ----
    checkin._notify_summary = _orig_notify
    checkin._spawn_toast = lambda payload: _PAYLOADS.append(payload)
    # _notify_summary 在"有失败"时会读配置判断要不要写"仍将自动重试"，
    # 这里显式钉死配置，别依赖上一次 _pop 留下的闭包。
    checkin.load_config = lambda: _cfg()

    def _say(results, attempt, last, silent=True):
        _PAYLOADS.clear()
        checkin.SILENT = silent
        checkin._notify_summary(results, attempt=attempt, last=last)
        return json.loads(_PAYLOADS[-1]) if _PAYLOADS else {}

    p = _say(_res("ok"), 1, False)
    check("⑨ 首次全成功标题 = 全部成功", p.get("title") == "每日签到：全部成功",
          f"实际 {p.get('title')!r}")
    check("⑨ 首次时间行含「自动执行」", "自动执行" in p.get("time", ""),
          f"实际 {p.get('time')!r}")

    p = _say(_res("ok", "skip"), 3, False)
    check("⑩ 补跑补上的标题 = 补跑完成", p.get("title") == "每日签到：补跑完成",
          f"实际 {p.get('title')!r}")
    check("⑩ 补跑时间行含「自动补跑 第3次」",
          "自动补跑" in p.get("time", "") and "第3次" in p.get("time", ""),
          f"实际 {p.get('time')!r}")

    p = _say(_res("fail", "ok"), 3, False)
    check("⑪ 还会再来时提示「仍将自动重试」",
          "仍将自动重试" in p.get("time", ""), f"实际 {p.get('time')!r}")
    p = _say(_res("fail"), 4, True)
    check("⑫ 最后一轮失败提示「重试已结束，请手动处理」",
          "重试已结束" in p.get("time", ""), f"实际 {p.get('time')!r}")
    # 未开启补跑时不该说"仍将自动重试"（那是假承诺）—— 用桩配置把它逼出来
    checkin.load_config = lambda: _cfg(retry_on=False)
    p = _say(_res("fail"), 2, False)
    check("⑬ 未开启补跑时时间行提示「重试已关闭」",
          "重试已关闭" in p.get("time", ""), f"实际 {p.get('time')!r}")
    checkin.load_config = _orig_load_config

    p = _say(_res("ok"), 1, False, silent=False)
    check("⑭ 手动执行时间行 = 手动执行", "手动执行" in p.get("time", ""),
          f"实际 {p.get('time')!r}")

    # ---- 窗口结束时刻 / 是否最后一轮的算术（显式 now，不依赖真实时间）----
    # ★ 必须先换回**真实** _is_last_attempt：上面为了可控把它换成了桩，
    #   忘了换回来时这两条断言会打到桩上（第一条还会"假通过"）。
    checkin._is_last_attempt = _orig_is_last
    import datetime as _dt3  # noqa: E402
    c = _cfg(iv=30, win=360, times=("09:00", "21:00"))
    end = checkin._retry_window_end(c)
    check("⑮ 窗口结束 = 最后一个时间点 + 补跑时长",
          end == _dt3.datetime.combine(__import__("datetime").date.today(),
                                       _dt3.time(21, 0)) + _dt3.timedelta(minutes=360),
          f"实际 {end}")
    check("⑮ 窗口内不算最后一轮",
          not checkin._is_last_attempt(c, now=end - _dt3.timedelta(minutes=120)),
          "误判为最后一轮")
    check("⑮ 窗口尾段算最后一轮",
          checkin._is_last_attempt(c, now=end - _dt3.timedelta(minutes=10)),
          "未识别为最后一轮")
    check("⑮ 关闭补跑时永远不算最后一轮",
          checkin._retry_window_end(_cfg(retry_on=False)) is None)

    # ---- _record_results 必须走 _maybe_notify（否则策略形同虚设）----
    _seen = []
    checkin._maybe_notify = lambda results: _seen.append(len(results))
    checkin.save_state({})
    checkin._record_results(_res("ok", "fail"), {})
    check("⑯ _record_results 经由 _maybe_notify 决定弹窗",
          _seen == [2], f"实际 {_seen}")
    checkin._maybe_notify = _orig_maybe

    # ---- 配置默认值 / 设置页 UI ----
    _dc2 = checkin.default_config().get("options", {})
    check("⑰ checkin.default_config 含 retry_toast_quiet",
          "retry_toast_quiet" in _dc2, f"实际键 {sorted(_dc2)}")
    _dc3 = _C.DEFAULT_CONFIG.get("options", {})
    check("⑰ app.config.DEFAULT_CONFIG 含 retry_toast_quiet",
          "retry_toast_quiet" in _dc3, f"实际键 {sorted(_dc3)}")
    check("⑰ 设置页有该开关且走 _switch_row 落盘",
          'retry_toast_quiet' in _settings and "_switch_row" in _settings)
    check("⑰ 开关带 on_change 回调（改完要刷新说明文字）",
          "on_change=" in _settings, "未见 on_change")
finally:
    checkin.load_config = _orig_load_config
    checkin._notify_summary = _orig_notify
    checkin._spawn_toast = _orig_spawn
    checkin._is_last_attempt = _orig_is_last
    checkin._maybe_notify = _orig_maybe
    checkin.SILENT = _orig_silent
    for _k, _v in _orig_paths.items():
        setattr(checkin, _k, _v)

print()
print(f"结果：{OK} OK / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
