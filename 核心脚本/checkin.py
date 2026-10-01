#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日签到自动化：TraeWork + WorkBuddy（多账号 / 开机自启 / 定时执行）

命令：
  python checkin.py run [--only workbuddy|traework] [--force]   立即执行一轮签到
  python checkin.py login traework [账号名]                      TraeWork 首次登录（每个账号一次）
  python checkin.py capture                                      保存 WorkBuddy 当前登录账号令牌（多账号）
  python checkin.py install                                      注册计划任务（每日定时 + 开机自启）
  python checkin.py uninstall                                    移除计划任务
  python checkin.py status                                       查看任务与最近签到状态
"""
import argparse
import csv
import datetime
import json
import os
import re
import subprocess
import sys

try:
    import runtime as _rt
except ImportError:  # 极端情况下 runtime.py 缺失，退回旧行为
    _rt = None

# 根目录：必须与 GUI（app/config.py）用同一套判定，否则会出现
# 「GUI 改的设置计划任务不认、签到结果 GUI 看不到」的分家问题。
# 统一交给 runtime.data_root()（含 TRAEBUDDY_HOME / 项目根 / exe 旁 的优先级）。
ROOT = (_rt.data_root() if _rt else os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# exe 模式下把 Playwright 浏览器目录指回系统安装位置（包内那份是空的，
# 否则 TraeWork 签到会报 "Executable doesn't exist ... chrome-headless-shell.exe"）。
# 必须在任何 import playwright 之前执行 —— tw_checkin 是懒加载的，这里够早。
if _rt:
    try:
        _rt.ensure_playwright_browsers()
    except Exception:  # noqa: BLE001
        pass

CONFIG_PATH = os.path.join(ROOT, "config.json")
LOG_DIR = os.path.join(ROOT, "logs")
STATE_PATH = os.path.join(LOG_DIR, "state.json")
HISTORY_PATH = os.path.join(LOG_DIR, "history.jsonl")
RECORD_PATH = os.path.join(LOG_DIR, "签到记录.csv")
# 签到记录「自定义导出位置」（2026-09-29 新增，见 sync_record_export）：
# 用户可以把 CSV 额外同步一份到自选位置（桌面 / 网盘目录 / 共享盘等）。
# ★ 主记录**仍然固定**写在上面的 RECORD_PATH —— 它是 history.jsonl 的重建产物，
#   各页面、命令行 `status`、计划任务都按这个路径读。把主文件挪走会让它们
#   读不到（表现为「记录空了」），所以自定义位置只是一份**同步副本**，不改主流程。
NOTIFY_PS1 = os.path.join(ROOT, "notify.ps1")
# 补跑（重试）轮次的记账文件。
# ★ 为什么**不**塞进 state.json：那份文件是「今日是否已签」的**判重唯一依据**，
#   里面每个键都必须是 `平台:账号`。混进 `_retry` 这种非账号键会让判重语义变脏
#   （`cmd_status` 会把它当成一个账号打印出来）。单独一个文件干净且零风险。
RETRY_STATE_PATH = os.path.join(LOG_DIR, "retry_state.json")
TASK_NAME = "AutoDailyCheckin"

SILENT = False

_STATUS_CN = {"ok": "成功", "skip": "已签跳过", "fail": "失败",
              # ★ busy 有两种来源（浏览器 profile 被占用 / 服务端限流 9074），
              #   共同点是「本轮没确认签到、下一轮补跑会自动复核」。
              #   旧文案「已跳过(占用)」只描述了其中一种，对限流场景是错的。
              "busy": "待复核",
              "expired": "失败(令牌过期)", "error": "执行异常"}


def _append_records(results):
    """重建人类可读的「签到记录.csv」（Excel 可直接打开）。

    以 history.jsonl 为唯一数据源全量重建（含本轮，调用点在其落盘之后），
    天然去重且幂等；任何一轮重跑/补写都不会产生重复行。
    """
    if not results:
        return
    try:
        rows = []
        with open(HISTORY_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                ts = str(e.get("ts", ""))
                rows.append([ts[:10] or "?", ts[11:19] or "?",
                             {"traework": "TraeWork", "workbuddy": "WorkBuddy",
                              "traecn": "TraeCN"}.get(e.get("platform"), str(e.get("platform"))),
                             str(e.get("account", "?")),
                             _STATUS_CN.get(e.get("status"), str(e.get("status"))),
                             str(e.get("detail", "")).replace(",", "，")])
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(RECORD_PATH, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["日期", "时间", "平台", "账号", "结果", "详情"])
            w.writerows(rows)
        # 主记录写好后，顺手同步一份到用户自定义位置（附加功能）。
        # ★ 放在 try 内但 sync_record_export 自己兜异常 —— 导出失败绝不影响签到。
        sync_record_export()
    except Exception:  # noqa: BLE001
        pass


def _as_bool(v, default=False):
    """配置开关稳妥转 bool（与 app/config.as_bool 同口径）。

    config.json 用户可手编：`"record_export_enabled": "false"` 时
    `bool("false")` 是 True —— 关掉会变成打开，所以必须显式判定。
    """
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("", "0", "false", "no", "off", "none", "null"):
            return False
        if s in ("1", "true", "yes", "on"):
            return True
        return default
    if isinstance(v, (int, float)):
        return bool(v)
    return default


def record_export_target(cfg=None):
    """自定义导出目标的**文件**绝对路径；未启用或未配置时返回 None。

    取值规则（与 GUI 设置页口径一致）：
      * 以 `.csv` 结尾 → 当作完整文件名（可自定义文件名）
      * 否则          → 当作目录，在其下生成 `签到记录.csv`
    """
    try:
        cfg = cfg if isinstance(cfg, dict) else load_config()
    except Exception:  # noqa: BLE001
        return None
    opts = cfg.get("options") if isinstance(cfg.get("options"), dict) else {}
    if not _as_bool(opts.get("record_export_enabled"), False):
        return None
    raw = str(opts.get("record_export_path") or "").strip()
    if not raw:
        return None
    try:
        p = os.path.abspath(os.path.expanduser(raw))
    except Exception:  # noqa: BLE001
        return None
    if p.lower().endswith(".csv"):
        return p
    return os.path.join(p, os.path.basename(RECORD_PATH))


def sync_record_export(cfg=None):
    """把主记录同步一份到用户自定义位置。

    ★ 只复制、不改主记录；任何失败都只记日志并返回 (False, 原因)，
      绝不抛异常 —— 导出是附加功能，不能拖累签到本身。

    返回 `(是否成功, 目标路径 或 错误信息)`；未启用时返回 `(False, "")`。
    """
    target = record_export_target(cfg)
    if not target:
        return False, ""
    try:
        if not os.path.isfile(RECORD_PATH):
            return False, "主记录尚不存在（还没有签到过）"
        if os.path.abspath(target) == os.path.abspath(RECORD_PATH):
            # 自己复制自己没有意义，也让用户误以为「导出成功」但位置没变
            return False, "导出位置不能就是主记录本身，请换一个文件夹"
        d = os.path.dirname(target)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(RECORD_PATH, "rb") as src:
            data = src.read()
        with open(target, "wb") as dst:
            dst.write(data)
        return True, target
    except Exception as e:  # noqa: BLE001  权限不足 / 路径是目录 / 盘符不存在等
        try:
            log("签到记录自定义导出失败：%r（目标 %s）" % (e, target))
        except Exception:  # noqa: BLE001
            pass
        return False, str(e)


def log(msg):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    if not SILENT:
        print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, f"checkin_{datetime.datetime.now():%Y-%m-%d}.log"),
                  "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def default_config():
    """出厂默认配置（便携版 exe 首次运行时 config.json 还不存在时用它兜底）。

    没有这个兜底时，`settime` / `install` 之类命令会在便携版里直接抛
    FileNotFoundError 崩掉 —— 用户刚解压出来还没签到过，就点「保存时间」
    会看到一个 Python traceback，体验很差。
    """
    return {
        "traework": {"enabled": True, "accounts": [{"name": "default",
                                                    "profile": "default"}]},
        "traecn": {"enabled": True},
        "workbuddy": {"enabled": True, "auto_detect_current": True,
                      "use_saved_tokens": True},
        "schedule": {
            "daily_times": ["09:00"],
            "checkin_time": "09:00",
            "run_at_logon": True,
            "logon_delay_seconds": 10,
            "network_wait_minutes": 720,
            # 补跑（重试）：过了设定时间后每 retry_interval_minutes 重试一次，
            # 持续 retry_window_minutes。门控幂等，已完成则秒退。
            "retry_enabled": True,
            "retry_interval_minutes": 30,
            "retry_window_minutes": 360,
        },
        # 账号凭据自动重新捕获（与 GUI 的 app/config.DEFAULT_CONFIG 保持一致）。
        # ★ 这个键只对 GUI 的后台刷新有意义 —— CLI/计划任务每次执行本来就会
        #   重新捕获一次（collect_accounts / _collect_accounts），不受它影响。
        #   写在这里是为了「GUI 生成的 config 与 CLI 兜底的 config 形状一致」。
        "credentials": {
            "auto_refresh": True,
            "refresh_interval_hours": 6,
            # ★ 源文件一变就立刻重新捕获（独立于上面的间隔）—— 对应
            #   「重新打开 Trae CN IDE / WorkBuddy 拿到新令牌」这类事件。
            #   见 desktop/AutoCheckinGUI/app/services/credential_refresh.py。
            "refresh_on_source_change": True,
        },
        "options": {
            "headless": True,
            # 补跑轮次的弹窗策略：True = 只在关键节点弹（第 1 次执行 / 全部签完 /
            # 补跑窗口最后一轮仍失败），避免某平台一整天坏着时每半小时弹一次；
            # False = 每轮都弹。手动执行永远弹，不受此开关影响。
            "retry_toast_quiet": True,
        },
    }


def load_config():
    """读 config.json；缺失或损坏时返回默认配置（并补写一份到磁盘）。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    cfg = default_config()
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return cfg


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _spawn_toast(payload):
    """弹签到结果卡片悬浮窗。

    脚本模式 → 起一个 python 进程跑 toast_gui.py；
    exe 模式  → exe 自己以 `--toast <payload>` 再启动一个实例来画卡片
               （toast_gui.py 已打进 exe，入口做了分发）。
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if _rt and _rt.is_frozen():
        cmd = [sys.executable, "--toast", payload]
    else:
        cmd = [sys.executable, os.path.join(ROOT, "toast_gui.py"), payload]
    subprocess.Popen(cmd, creationflags=flags, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def save_state(state):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _ps_toast(title, body):
    """最终兜底：系统原生 Toast。"""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", NOTIFY_PS1, "-Title", title, "-Body", body],
                         creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


def load_retry_state():
    """读补跑记账（当日第几次执行、关键节点是否已弹过窗）。"""
    try:
        with open(RETRY_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_retry_state(st):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(RETRY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _bump_retry_attempt(today):
    """把「今天自动执行到第几次」加一，跨天自动清零。返回更新后的记账 dict。

    只统计**自动执行**（计划任务 / 开机触发，即 SILENT=True）—— 手动点「一键签到」
    不算一轮，否则用户在界面上点几次就把计数带偏了。
    """
    st = load_retry_state()
    if st.get("date") != today:
        st = {"date": today, "attempts": 0,
              "notified_first_fail": False, "notified_last": False}
    st["attempts"] = int(st.get("attempts") or 0) + 1
    save_retry_state(st)
    return st


def _retry_window_end(cfg):
    """今天补跑窗口的结束时刻（最后一个签到时间点 + 补跑时长）；没开补跑返回 None。"""
    sched = cfg.get("schedule", {})
    if not sched.get("retry_enabled", True):
        return None
    try:
        win = int(sched.get("retry_window_minutes", 360))
    except (TypeError, ValueError):
        return None
    times = [t for t in (sched.get("daily_times") or []) if t] or \
        [sched.get("checkin_time") or "09:00"]
    end = None
    for t in times:
        try:
            h, m = [int(x) for x in str(t).strip().split(":")]
        except (TypeError, ValueError):
            continue
        d = (datetime.datetime.combine(datetime.date.today(),
                                       datetime.time(h, m))
             + datetime.timedelta(minutes=win))
        if end is None or d > end:
            end = d
    return end


def _is_last_attempt(cfg, now=None):
    """当前是否已是今天补跑窗口内的**最后一次**触发。

    判据：再等一个重试间隔就越过窗口结束时刻 → 下一轮不会再来（或只会擦边），
    所以这一轮如果还失败，就该明确告诉用户「得你自己处理了」。
    """
    end = _retry_window_end(cfg)
    if end is None:
        return False
    try:
        iv = int(cfg.get("schedule", {}).get("retry_interval_minutes", 30))
    except (TypeError, ValueError):
        iv = 30
    now = now or datetime.datetime.now()
    return now + datetime.timedelta(minutes=iv) > end


def _maybe_notify(results):
    """决定这轮**要不要弹窗**、以及弹窗里怎么描述这一轮。

    这是「自动补跑」与「弹窗」同步的**唯一入口**（`_record_results` 只调它）。
    三条规则：
      1) `options.toast_notify` 关掉 → 一律不弹（沿用原有开关）。
      2) 手动执行（`SILENT=False`）→ 永远弹，行为与改动前完全一致，不计入补跑轮次。
      3) 自动执行 → 记一轮。若开了 `options.retry_toast_quiet`（默认开），
         只在**关键节点**弹，避免"某平台一整天坏着 → 每 30 分钟弹一次"刷屏：
           · 当天第 1 次执行：必弹（无论成败，让你知道它跑过了）
           · 全部签完的那一轮：必弹（好消息）
           · 补跑窗口最后一轮且仍有失败：必弹（需要你手动处理）
         其余中间轮次静默 —— 弹窗标题里已经写了「将自动重试」，用户不会失去信息。
        注意 `busy`（服务端限流 / profile 被占用）算「未完成」但**不算失败**：
        它照样会走补跑，只是文案显示「待复核」而不是红灯。
    """
    if not results:
        return
    cfg = load_config()
    if not cfg.get("options", {}).get("toast_notify", True):
        return

    all_done = all(r.get("status") in ("ok", "skip") for r in results)

    if not SILENT:
        log(f"已弹窗通知：{_notify_summary(results, attempt=1, last=False) or '签到结果'}"
            f"（手动执行）")
        return

    today = datetime.date.today().isoformat()
    st = _bump_retry_attempt(today)
    attempt = st["attempts"]
    last = _is_last_attempt(cfg)

    if cfg.get("options", {}).get("retry_toast_quiet", True):
        quiet_ok = all_done or attempt == 1 or last
        if not quiet_ok:
            log(f"补跑第 {attempt} 次仍有未完成项，按「仅关键节点弹窗」策略静默"
                f"（下次触发仍会自动重试）")
            return
        # 同一关键节点只弹一次（防同一分钟内被重复触发两次任务时连弹两下）
        if not all_done and attempt == 1:
            if st.get("notified_first_fail"):
                return
            st["notified_first_fail"] = True
            save_retry_state(st)
        if not all_done and last:
            if st.get("notified_last"):
                return
            st["notified_last"] = True
            save_retry_state(st)

    # ★ 把「这一轮到底弹没弹」写进日志：2026-09-19 用户"走神"没看到第二次弹窗，
    #   事后只能靠 notify_last.html 的 mtime 反推（它只保留最后一次）。
    #   有这行就能直接 grep「已弹窗通知」核对整天的弹窗流水。
    log(f"已弹窗通知：{_notify_summary(results, attempt=attempt, last=last) or '签到结果'}")


def _notify_summary(results, attempt=1, last=False):
    """每轮结束后弹「液态金属琉璃」纯卡片悬浮窗（原生透明窗口自绘，无边框）。

    回退链：自绘卡片 → 系统 Toast；任何异常绝不影响签到主流程。
    每行 note 显示真实状态：成功带积分/连续天数，失败带简短原因。

    `attempt` = 今天第几次**自动**执行（1 = 首次；>1 = 补跑轮次），
    `last`    = 是否已是补跑窗口内的最后一次触发。
    两者只影响文案（标题 / 时间行），不改变弹不弹 —— 弹不弹由 `_maybe_notify` 决定。

    返回弹窗标题（供调用方写日志；失败/未弹时返回 None）。
    """
    try:
        plat = {"workbuddy": "WB", "traework": "TW", "traecn": "TC"}
        ok = [r for r in results if r["status"] in ("ok", "skip")]
        # ★ busy（服务端限流 9074 / 浏览器 profile 被占用）既不是成功也不是失败：
        #   本轮确实没确认签成，但下一轮补跑会自动复核。报成「失败」会**误导**
        #   —— 2026-09-19 实测：弹窗报「TraeWork 失败」，2 分钟后补跑就确认
        #   积分早已到账，用户白担心一场。
        warn = [r for r in results if r["status"] == "busy"]
        fail = [r for r in results if r["status"] not in ("ok", "skip", "busy")]
        # 标题：补跑成功时单独说「补跑完成」—— 用户能一眼看出"第一次失败的那次
        # 已经被后面某轮补上了"，而不是又看到一次含糊的"全部成功"。
        if not fail and not warn:
            title = ("每日签到：补跑完成" if attempt > 1 else "每日签到：全部成功")
        elif not fail:
            title = f"每日签到：{len(ok)}/{len(results)} 完成，{len(warn)} 待复核"
        else:
            title = (f"每日签到：{len(ok)}/{len(results)} 成功，{len(fail)} 失败"
                     + (f"，{len(warn)} 待复核" if warn else ""))

        def _note(r):
            d = str(r["detail"])
            low = d.lower()
            if "⚠" in d:  # 令牌过期预警优先展示
                return "⚠" + d.split("；⚠", 1)[1]
            st = r["status"]
            if st == "skip":
                return "已签到"
            if st == "busy":
                # ★ 绝不能落进上面的 skip 分支显示成「已签到」：本轮**没有**签到，
                #   只是还没确认（下一轮补跑会复核）。
                if "占用" in d or "profile" in low:
                    return "待复核：浏览器被占用"
                if "限流" in d or "9074" in d or "稍后" in d:
                    return "待复核：服务端限流"
                return "待复核"
            if st == "ok":
                m = re.search(r"领取\s*([\d,]+)\s*积分", d)
                if m:
                    n = re.search(r"已连续签到\s*(\d+)\s*天", d)
                    return f"签到成功 +{m.group(1)}" + (f"·连{n.group(1)}天" if n else "")
                return "已签到" if ("已签到" in d or "幂等" in d) else "签到成功"
            # fail / expired / error：简短失败原因
            if "网络" in d or "timeout" in low or "connection" in low or "ssl" in low:
                return "失败：网络异常"
            # ★ 顺序不能反：令牌吊销的建议文案里**含「登录」二字**
            #   （"…登录即可自动换到新令牌"），先判「登录」会把真因（令牌过期）
            #   遮蔽成「需重新登录」—— 2026-09-19 实测就撞上了这个假归因。
            if "过期" in d or "expired" in low or "令牌" in d:
                return "失败：令牌过期"
            if "登录" in d:
                return "失败：需重新登录"
            if "9004" in d or "设备" in d:
                return "失败：设备校验"
            if "未找到签到入口" in d or "页面" in d:
                return "失败：页面变化"
            return "失败"

        rows = []
        for r in results:
            st = r["status"]
            # 三态灯：成功/跳过=蓝绿、busy=琥珀（待复核）、其余=红。
            state = ("skip" if st in ("ok", "skip")
                     else ("warn" if st == "busy" else "fail"))
            rows.append(dict(state=state,
                             tag=plat.get(r["platform"], r["platform"][:2]).upper(),
                             name=str(r["account"]),
                             note=_note(r)))
        for r in fail[:2]:
            rows.append(dict(state="fail", tag="!!", name="失败详情",
                             note=str(r["detail"])[:60]))
        # 时间行（10px 小字，横向容量比标题大得多）承载「第几次 / 还会不会再来」，
        # 标题保持短 —— 420px 卡片里长标题会折行、卡片高度跟着变。
        when = datetime.datetime.now().strftime("%H:%M")
        if not SILENT:
            when += " 手动执行"
        elif attempt <= 1:
            when += " 自动执行"
        else:
            when += f" 自动补跑 第{attempt}次"
            if fail:
                if not load_config().get("schedule", {}).get("retry_enabled", True):
                    when += " · 重试已关闭"
                elif last:
                    when += " · 重试已结束，请手动处理"
                else:
                    when += " · 仍将自动重试"
            elif warn:
                # 只剩「待复核」时不能说「请手动处理」—— 它下一轮自己会复核掉。
                when += " · 重试窗口已结束" if last else " · 仍将自动重试"
        payload = json.dumps(dict(title=title, rows=rows, time=when),
                             ensure_ascii=False).replace("<", "\\u003c")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _spawn_toast(payload)
        return title
    except Exception:  # noqa: BLE001
        try:
            _ps_toast("每日签到", "执行完成（详情见 logs）")
        except Exception:  # noqa: BLE001
            pass


def _gate_skip(cfg, state, today, now=None):
    """触发门控：对比「当日签到基准时间」与日期记录，决定是否真正执行。

    返回跳过原因（str）或 None（需要执行）。规则：
      1) 所有账号今日（state 日期记录）均已完成 → 静默退出（避免重复弹窗）
      2) 当前时间未到当日基准时间（默认 09:00）→ 静默退出，
         到点后由当日定时触发执行；错过时点开机后自动补跑
      3) 其余情况（9:00 后开机/跨天开机/有账号未完成）→ 正常执行一轮
    """
    now = now or datetime.datetime.now()
    due = False
    tw = cfg.get("traework", {})
    if tw.get("enabled", True):
        for a in tw.get("accounts", []):
            k = f"traework:{a.get('name') or a.get('profile') or 'default'}"
            if state.get(k) != today:
                due = True
                break
    if not due:
        try:
            import tc_checkin
            if cfg.get("traecn", {}).get("enabled", True):
                for acc in tc_checkin.collect_accounts(cfg, print):
                    if state.get(f"traecn:{acc['uid']}") != today:
                        due = True
                        break
        except Exception:  # noqa: BLE001
            due = True
    if not due and cfg.get("workbuddy", {}).get("enabled", True):
        try:
            import wb_checkin
            for acc in wb_checkin._collect_accounts(cfg, print):
                if state.get(f"workbuddy:{acc['uid']}") != today:
                    due = True
                    break
        except Exception:  # noqa: BLE001
            due = True  # 判定异常时宁可多跑一轮
    if not due:
        return "今日全部账号已完成（日期记录比对）"
    sched = cfg.get("schedule", {})
    ref = sched.get("checkin_time") or (sched.get("daily_times") or ["09:00"])[0]
    try:
        h, m = [int(x) for x in str(ref).strip().split(":")]
    except Exception:  # noqa: BLE001
        h, m = 9, 0
    if (now.hour, now.minute) < (h, m):
        return f"未到当日签到基准时间 {h:02d}:{m:02d}"
    return None


def _record_results(results, state):
    """统一落盘（state + history + CSV）并弹通知；正常轮次与快速失败共用。"""
    save_state(state)
    try:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            for entry in results:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    _append_records(results)

    ok = sum(1 for r in results if r["status"] in ("ok", "skip"))
    busy = sum(1 for r in results if r["status"] == "busy")
    fail = len(results) - ok - busy
    # ★ busy 单独一档：它不是失败，只是本轮没确认（下一轮补跑会复核）。
    log(f"本轮完成：成功/跳过 {ok}，待复核 {busy}，失败 {fail}")
    for r in results:
        log(f"  - [{r['platform']}] {r['account']}: {r['status']} — {r['detail']}")
    # ★ 走 _maybe_notify 而不是直接 _notify_summary：补跑轮次要按
    #   options.retry_toast_quiet 决定弹不弹（中间失败轮次静默，别刷屏），
    #   并把「第几次 / 会不会再来」写进弹窗文案。toast_notify 总开关也在它里面判。
    _maybe_notify(results)


def run_all(only=None, force=False):
    cfg = load_config()
    state = load_state()
    today = datetime.date.today().isoformat()

    # 门控：手动 --force / --only 不受限制，计划任务与开机触发的完整轮次受门控约束
    if only is None and not force:
        reason = _gate_skip(cfg, state, today)
        if reason:
            log(f"本轮静默跳过：{reason}，不启动浏览器")
            if not SILENT:  # 手动执行也弹窗反馈，避免“点了没反应”的困惑
                _notify_gated(cfg, state, today, reason)
            return []

    results = []

    # 网络嗅探锚点：开机/定时触发常发生在联网完成之前，无网会让浏览器把
    # 「页面没加载」误判成「登录失效」并白等 7 分钟。这里持续嗅探直到联网
    # （默认上限 720 分钟 = 当天内联网必签，可配 schedule.network_wait_minutes，
    # 0 = 无限等待），联网后复查门控避免与手动签到重复。手动 --force 立即试跑。
    if not force:
        import net_util
        _sched = load_config().get("schedule", {})
        wait_min = _sched.get("network_wait_minutes", 720)
        # 开了过期补跑时，单次等网不能超过补跑间隔 —— 否则一个进程卡着等 12 小时，
        # 而任务设置是 MultipleInstances=IgnoreNew，后续几次补跑会被直接忽略，
        # 「过了时间自动重签」就形同虚设。缩短等待让每轮都快速退出、交给下一轮。
        if _sched.get("retry_enabled", True):
            try:
                _ri = int(_sched.get("retry_interval_minutes", 30))
                if isinstance(wait_min, (int, float)) and wait_min > _ri:
                    wait_min = _ri
            except (TypeError, ValueError):
                pass
        max_wait = None if (isinstance(wait_min, (int, float)) and wait_min <= 0) else wait_min * 60
        if not net_util.wait_network(max_wait_s=max_wait, interval_s=30, log=log, log_every=10):
            log("等待超时仍离线，本轮退出；联网后由网络恢复触发器/下次定时自动补跑")
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            for pid in ("traework", "traecn", "workbuddy"):
                if only in (None, pid):
                    results.append(dict(ts=ts, platform=pid, account="-",
                                        status="fail", detail="网络不可用（恢复后自动补跑）"))
            _record_results(results, state)
            return results
        # 等网期间用户可能已手动完成签到：联网后复查门控，幂等保护
        state = load_state()
        reason = _gate_skip(cfg, state, today)
        if reason and only is None:
            log(f"等待网络期间已全部完成（{reason}），本轮静默结束")
            return []

    # TraeWork 需要浏览器流程、耗时最长，放最前避免开机竞速时被手动签到抢先。
    #
    # ★ 2026-09-18 补注：TraeWork 与 TraeCN 其实是**同一个 Trae 账号、同一个
    #   签到活动**（uid 相同、都在 api.trae.cn 的 checkin_credits）。以前
    #   traework 排在前面先"查"这个活动，而真正"领"的是十几秒后的 traecn ——
    #   于是 traework 每天都看到"明细里没有今日条目"并判失败（history 里
    #   26 次 fail 的主因）。现在 tw_checkin 自己也走接口（status → 需要就
    #   claim），排在前面反而**先把活动领掉**，traecn 随后查到"已签"直接跳过。
    #   顺序保持不变，问题在源头解决。
    if only in (None, "traework"):
        try:
            import tw_checkin
            results += tw_checkin.run(log, state, cfg, today, force)
        except Exception as e:  # noqa: BLE001
            log(f"[traework] 模块执行异常: {e!r}")
            results.append(dict(ts=datetime.datetime.now().isoformat(timespec="seconds"),
                                platform="traework", account="-", status="error", detail=repr(e)))

    # Trae CN（IDE）本地令牌接口签到，轻量快速，放浏览器流程之后
    if only in (None, "traecn"):
        try:
            import tc_checkin
            results += tc_checkin.run(log, state, cfg, today, force)
        except Exception as e:  # noqa: BLE001
            log(f"[traecn] 模块执行异常: {e!r}")
            results.append(dict(ts=datetime.datetime.now().isoformat(timespec="seconds"),
                                platform="traecn", account="-", status="error", detail=repr(e)))

    if only in (None, "workbuddy"):
        try:
            import wb_checkin
            results += wb_checkin.run(log, state, cfg, today, force)
        except Exception as e:  # noqa: BLE001
            log(f"[workbuddy] 模块执行异常: {e!r}")
            results.append(dict(ts=datetime.datetime.now().isoformat(timespec="seconds"),
                                platform="workbuddy", account="-", status="error", detail=repr(e)))

    _record_results(results, state)
    return results


# ---------------- 计划任务（开机自启 + 每日定时） ----------------

def _pythonw():
    """后台静默运行所用的解释器（exe 模式下即 exe 自身）。"""
    if _rt:
        return _rt.pythonw()
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def _current_user():
    """取当前登录用户的 `DOMAIN\\User` 形式，供计划任务的 Principal 使用。

    三个坑（都踩过，务必保留这里的取法）：
    1. **不要把 `"$env:USERDOMAIN\\$env:USERNAME"` 写进 Python 的 f-string** ——
       反斜杠在 f-string 里被当转义处理后会变成双反斜杠，PowerShell 解析出歧义
       字符串，`Register-ScheduledTask` 会静默降级成裸用户名。
    2. **`%USERDOMAIN%` 在本地账户下等于计算机名（如 `KLEIN`），不是账户域** ——
       用它拼出来的是 `KLEIN\\25715`，与真实账户域 `Klein\\25715` 大小写不符，
       Windows 解析失败后同样退化成裸用户名。
    3. 权威来源是 `Win32_ComputerSystem.UserName`（形如 `Klein\\25715`），
       其次 `whoami`，最后才退回计算机名拼接。
    """
    # 1) 权威来源：Win32_ComputerSystem.UserName
    try:
        import subprocess as _sp
        flags = getattr(_sp, "CREATE_NO_WINDOW", 0)
        # ⚠️ 必须先把 PowerShell 的输出编码钉成 UTF-8：中文 Windows 默认按 GBK 写
        # stdout，而这里按 utf-8 解码 + errors="ignore" —— 账户名里只要有非 ASCII
        # 字符（中文用户名很常见）就会被静默吃掉，拼出 `KLEIN\` 这种残缺标识，
        # 任务注册"成功"却永远不会触发。ASCII 账户名看不出来，所以极易漏测。
        r = _sp.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
             "try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {};"
             "(Get-CimInstance Win32_ComputerSystem).UserName"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=20, creationflags=flags)
        name = (r.stdout or "").strip().splitlines()[-1].strip() if r.stdout else ""
        if name and "\\" in name:
            return name
    except Exception:  # noqa: BLE001
        pass

    # 2) 次选：whoami
    try:
        import subprocess as _sp
        flags = getattr(_sp, "CREATE_NO_WINDOW", 0)
        r = _sp.run(["whoami"], capture_output=True, text=True,
                    encoding="utf-8", errors="ignore", timeout=15,
                    creationflags=flags)
        name = (r.stdout or "").strip()
        if name and "\\" in name and "$" not in name:
            return name
    except Exception:  # noqa: BLE001
        pass

    # 3) 兜底：计算机名（不是 USERDOMAIN）+ 用户名
    name = (os.environ.get("USERNAME") or "").strip()
    if not name:
        try:
            import getpass
            name = getpass.getuser()
        except Exception:  # noqa: BLE001
            pass
    host = (os.environ.get("COMPUTERNAME")
            or os.environ.get("USERDOMAIN") or "").strip()
    return f"{host}\\{name}" if host and name else name


def _iso_dur_cn(s):
    """把 ISO8601 时长（PT30M / PT6H）转成中文，便于日志阅读。

    注意：Windows 会把 `PT360M` 规范化成 `PT6H`，所以**不要**拿字符串直接
    跟配置值比对，必须按数值判断（这里统一转成分钟）。
    """
    s = (s or "").strip().upper()
    if not s.startswith("PT"):
        return s or "-"
    m = re.fullmatch(r"(?:(\d+)H)?(?:(\d+)M)?", s[2:])
    if not m:
        return s
    h, mi = int(m.group(1) or 0), int(m.group(2) or 0)
    parts = []
    if h:
        parts.append(f"{h} 小时")
    if mi:
        parts.append(f"{mi} 分钟")
    return "".join(parts) or s


def _iso_dur_minutes(s):
    """ISO8601 时长 → 分钟（解析失败返回 None）。"""
    s = (s or "").strip().upper()
    if not s.startswith("PT"):
        return None
    m = re.fullmatch(r"(?:(\d+)H)?(?:(\d+)M)?", s[2:])
    if not m:
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def cmd_install(cfg):
    times = [t for t in cfg.get("schedule", {}).get("daily_times", ["09:00"])
             if isinstance(t, str) and ":" in t]
    if not times:
        times = ["09:00"]
    logon = bool(cfg.get("schedule", {}).get("run_at_logon", True))
    delay_s = int(cfg.get("schedule", {}).get("logon_delay_seconds", 60))

    # exe 模式：计划任务直接指向 exe 自身，参数里带 --run-checkin 让入口分发；
    # 脚本模式：沿用 pythonw + checkin.py 的旧方式。
    if _rt and _rt.is_frozen():
        exe = sys.executable.replace("'", "''")
        pyw = exe
        script_arg = "--run-checkin run --silent"
    else:
        pyw = _pythonw().replace("'", "''")
        script_arg = f'"{os.path.join(ROOT, "checkin.py").replace(chr(39), chr(39)*2)}" run --silent'
    root = ROOT.replace("'", "''")
    times_ps = ",".join(f"'{t}'" for t in times)
    delay = f"PT{delay_s}S"
    user = _current_user().replace("'", "''")

    # ---- 补跑窗口（用户诉求：过了设定时间要能自动重签，别因断网/开机晚而错过）----
    # 做法：给每个「每日定时」触发器加 Repetition —— 从设定时间起每 N 分钟触发一次，
    # 持续 M 小时。**门控保证幂等**：全部账号已完成时直接秒退（不开浏览器），
    # 所以重复触发几乎零成本；只有真没签上时才会真正重跑。
    sched = cfg.get("schedule", {})
    retry_on = bool(sched.get("retry_enabled", True))
    try:
        retry_min = max(1, int(sched.get("retry_interval_minutes", 30)))
    except (TypeError, ValueError):
        retry_min = 30
    try:
        retry_win = max(retry_min, int(sched.get("retry_window_minutes", 360)))
    except (TypeError, ValueError):
        retry_win = 360

    rep_block = ""
    if retry_on:
        rep_block = (
            "\n  $repClass = Get-CimClass -ClassName MSFT_TaskRepetitionPattern"
            " -Namespace Root/Microsoft/Windows/TaskScheduler"
            "\n  $rep = New-CimInstance -CimClass $repClass -ClientOnly"
            f"\n  $rep.Interval = 'PT{retry_min}M'"
            f"\n  $rep.Duration = 'PT{retry_win}M'"
            "\n  $tr.Repetition = $rep"
        )

    ps = f"""
$ErrorActionPreference = 'Stop'
$times = @({times_ps})
$trigs = @()
foreach ($t in $times) {{
  $tr = New-ScheduledTaskTrigger -Daily -At $t{rep_block}
  $trigs += $tr
}}
"""
    if logon:
        # 用户标识在 Python 侧解析好，这里只做纯字符串注入（不要用 $env: 拼接）
        ps += f"""
$u = '{user}'
if ($u -and $u.Contains('\\')) {{
  $l = New-ScheduledTaskTrigger -AtLogOn -User $u
  $l.Delay = '{delay}'
  $trigs += $l
}} else {{
  Write-Host ("WARN_LOGON_USER_INVALID=" + $u)
}}
"""
    ps += f"""
$action = New-ScheduledTaskAction -Execute '{pyw}' -Argument '{script_arg}' -WorkingDirectory '{root}'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
# 网络恢复事件触发器：无网时段错过签到后，联网瞬间自动补跑（门控保证幂等，已签到则静默跳过）
$netSub = @'
<QueryList><Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational"><Select Path="Microsoft-Windows-NetworkProfile/Operational">*[System[EventID=10000]]</Select></Query></QueryList>
'@
$c = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$netTrig = New-CimInstance -CimClass $c -ClientOnly
$netTrig.Enabled = $true
$netTrig.Subscription = $netSub
$trigs += $netTrig
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $action -Trigger $trigs -Settings $settings -RunLevel Limited -Description 'TraeWork + TraeCN + WorkBuddy 多账号每日签到' -Force | Out-Null
Write-Host 'TASK_REGISTERED'
# ---- 注册后自检：把 Principal 与触发器真实落盘结果回报给调用方 ----
# 过去这里没有校验，用户名拼错时会「静默注册成功但永不触发」，
# 用户只能看到界面上写着"已安装"，实际一次都没跑过。
$v = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if ($v) {{
  Write-Host ('VERIFY_PRINCIPAL=' + $v.Principal.UserId)
  Write-Host ('VERIFY_LOGONTYPE=' + $v.Principal.LogonType)
  $logonTrig = @($v.Triggers | Where-Object {{ $_.CimClass.CimClassName -like '*LogonTrigger*' }})
  Write-Host ('VERIFY_LOGON_COUNT=' + $logonTrig.Count)
  if ($logonTrig.Count -gt 0) {{
    Write-Host ('VERIFY_LOGON_USER=' + $logonTrig[0].UserId)
  }}
  $dailyTrig = @($v.Triggers | Where-Object {{ $_.CimClass.CimClassName -like '*DailyTrigger*' }})
  Write-Host ('VERIFY_DAILY_COUNT=' + $dailyTrig.Count)
  if ($dailyTrig.Count -gt 0 -and $dailyTrig[0].Repetition) {{
    Write-Host ('VERIFY_RETRY_INTERVAL=' + $dailyTrig[0].Repetition.Interval)
    Write-Host ('VERIFY_RETRY_DURATION=' + $dailyTrig[0].Repetition.Duration)
  }}
}}
"""
    ps_path = os.path.join(LOG_DIR, "install_task.ps1")
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(ps_path, "w", encoding="utf-8-sig") as f:
        f.write(ps)
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_path],
        capture_output=True, text=True, encoding="utf-8", errors="ignore",
        timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    if "TASK_REGISTERED" not in out:
        log(f"注册计划任务失败：\n{out.strip()}")
        return False

    # 解析自检结果。注意「主体名」判定口径：
    # Principal.UserId 在交互式登录任务里本来就会被系统渲染成裸用户名
    # （如 `25715`），这是正常的显示形态，**不能据此判失败**。
    # 真正要守住的是「登录触发器有没有绑定到具体账户」——
    # 绑错了才会出现「任务已安装但永不触发」（LastTaskResult=0x41303）。
    ver = {}
    for line in (r.stdout or "").splitlines():
        if line.startswith("VERIFY_") and "=" in line:
            k, v = line.split("=", 1)
            ver[k.strip()] = v.strip()
    warn = ""
    logon_user = ver.get("VERIFY_LOGON_USER") or ""
    if logon and ver.get("VERIFY_LOGON_COUNT") == "0":
        warn = "（警告：登录触发器未注册成功，开机补跑不会生效）"
        log(f"计划任务登录触发器未注册成功")
    elif logon and logon_user and "\\" not in logon_user:
        warn = f"（警告：登录触发器绑定到 {logon_user}，缺少域名前缀）"
        log(f"计划任务登录触发器绑定异常：{logon_user}")
    if "WARN_LOGON_USER_INVALID" in out:
        warn = warn or "（警告：未取到有效的 DOMAIN\\User，已跳过登录触发器）"
        log("登录触发器跳过：未取到有效的 DOMAIN\\User")

    # 补跑窗口落地校验：期望有 Repetition，且时长与配置一致
    retry_note = ""
    if retry_on:
        iv = ver.get("VERIFY_RETRY_INTERVAL") or ""
        du = ver.get("VERIFY_RETRY_DURATION") or ""
        iv_min = _iso_dur_minutes(iv)
        du_min = _iso_dur_minutes(du)
        if not iv:
            warn = warn or "（警告：过期补跑未生效，错过时间点后不会自动重签）"
            log("补跑窗口未写入触发器（Repetition 缺失）")
        else:
            retry_note = (f"；过期补跑：每 {_iso_dur_cn(iv)} 重试一次，"
                          f"持续 {_iso_dur_cn(du)}")
            if iv_min is not None and iv_min != retry_min:
                log(f"补跑间隔与配置不一致：触发器 {iv}（{iv_min} 分）"
                    f" vs 配置 {retry_min} 分")
            if du_min is not None and du_min != retry_win:
                log(f"补跑时长与配置不一致：触发器 {du}（{du_min} 分）"
                    f" vs 配置 {retry_win} 分")

    log(f"计划任务已注册：每日 {', '.join(times)} 执行"
        + (f" + 用户登录后 {delay_s}s 自动执行（错过的时间点开机会自动补跑）" if logon else "")
        + (f" 触发器用户={logon_user}" if logon_user else "")
        + retry_note
        + warn)
    return True


def _notify_gated(cfg, state, today, reason):
    """手动执行被门控跳过时，弹窗给可见反馈（自动任务保持完全静默）。"""
    try:
        rows = []
        tw = cfg.get("traework", {})
        if tw.get("enabled", True):
            for a in tw.get("accounts", []):
                nm = str(a.get("name") or a.get("profile") or "default")
                done = state.get(f"traework:{nm}") == today
                rows.append(dict(state="skip", tag="TW", name=nm,
                                 note="已签到" if done else "待执行"))
        if cfg.get("workbuddy", {}).get("enabled", True):
            try:
                import wb_checkin
                for acc in wb_checkin._collect_accounts(cfg, print):
                    nm = str(acc.get("nickname") or acc["uid"][:8])
                    done = state.get(f"workbuddy:{acc['uid']}") == today
                    rows.append(dict(state="skip", tag="WB", name=nm,
                                     note="已签到" if done else "待执行"))
            except Exception:  # noqa: BLE001
                pass
        if cfg.get("traecn", {}).get("enabled", True):
            try:
                import tc_checkin
                for acc in tc_checkin.collect_accounts(cfg, print):
                    done = state.get(f"traecn:{acc['uid']}") == today
                    rows.append(dict(state="skip", tag="TC", name=acc["name"],
                                     note="已签到" if done else "待执行"))
            except Exception:  # noqa: BLE001
                pass
        title = ("每日签到：今日已完成" if "已完成" in reason
                 else f"每日签到：{reason.split('（')[0]}")
        payload = json.dumps(dict(title=title, rows=rows,
                                  time=datetime.datetime.now().strftime("%H:%M") + " 手动执行"),
                             ensure_ascii=False).replace("<", "\\u003c")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _spawn_toast(payload)
        # ★ 与 `_maybe_notify` 一致：弹了就往日志里留一行，便于事后核对
        #   「整天到底弹过几次、每次是什么」（手动被门控跳过这条也要留痕）。
        log(f"已弹窗通知：{title}（手动执行 · 门控跳过）")
    except Exception:  # noqa: BLE001
        pass


def cmd_settime(times):
    """设定每日自动执行时间（HH:MM，1~4 个）并重新注册计划任务。"""
    if not times:
        print("用法: python checkin.py settime 08:30 20:00")
        return 1
    norm = []
    for t in times:
        try:
            h, m = t.strip().split(":")
            h, m = int(h), int(m)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
        except Exception:  # noqa: BLE001
            print(f"✗ 时间格式无效：{t}（应为 HH:MM，例如 08:30）")
            return 1
        norm.append(f"{h:02d}:{m:02d}")
    if len(set(norm)) != len(norm):
        print("✗ 存在重复的时间点")
        return 1
    cfg = load_config()
    sched = cfg.setdefault("schedule", {})
    sched["daily_times"] = norm
    sched["checkin_time"] = norm[0]  # 首个时间作为当日签到基准时间（门控用）
    # 写盘失败必须明确报错并返回非零，否则界面会弹「已生效」但配置其实没落地。
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"✗ 配置文件写入失败：{e!r}")
        print(f"  路径：{CONFIG_PATH}")
        return 1
    # 回读校验：确认真的落盘（防磁盘只读 / 被安全软件拦截等静默失败）
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            back = json.load(f).get("schedule", {}).get("daily_times")
        if back != norm:
            print(f"✗ 配置写入后回读不一致（写入 {norm}，读到 {back}）")
            return 1
    except (OSError, ValueError) as e:
        print(f"✗ 配置回读失败：{e!r}")
        return 1
    print(f"✓ 每日执行时间已设定为：{', '.join(norm)}")
    print(f"✓ 当日签到基准时间（门控）：{norm[0]}")
    print("✓ 正在重新注册计划任务…")
    ok = cmd_install(cfg)
    if not ok:
        print("✗ 计划任务注册失败，时间已保存但自动签到未生效，请重试")
        return 1
    return 0


def cmd_setretry(interval_min, window_min, enabled=True):
    """设定「自动补跑（重试）」参数并重新注册计划任务。

    补跑 = 过了设定时间后**每隔 interval_min 分钟再触发一次**，持续 window_min 分钟。
    幂等由 `_gate_skip()` 保证：全部账号已完成时秒退（不开浏览器），
    所以「签完即停」是天然成立的 —— 重复触发几乎零成本。

    为什么下限是 1 分钟而不是"几秒"：Windows 计划任务 Repetition 的分辨率就是分钟，
    `PT1M` 已是最细粒度；而且单次签到要开浏览器、耗时约 30s，秒级重试没有意义。
    """
    try:
        iv = int(interval_min)
        win = int(window_min)
    except (TypeError, ValueError):
        print("✗ 重试间隔 / 补跑时长必须是整数（分钟）")
        return 1
    if not (1 <= iv <= 360):
        print(f"✗ 重试间隔需在 1~360 分钟之间（当前 {iv}）")
        return 1
    if not (1 <= win <= 1440):
        print(f"✗ 补跑时长需在 1~1440 分钟之间（当前 {win}）")
        return 1
    # 窗口短于间隔 = 只可能触发一次，那不是"补跑"，直接拒掉比让用户困惑好
    if win < iv:
        print(f"✗ 补跑时长不能小于重试间隔（{win} < {iv} 分钟），否则只会重试一次")
        return 1
    cfg = load_config()
    sched = cfg.setdefault("schedule", {})
    sched["retry_enabled"] = bool(enabled)
    sched["retry_interval_minutes"] = iv
    sched["retry_window_minutes"] = win
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"✗ 配置文件写入失败：{e!r}")
        print(f"  路径：{CONFIG_PATH}")
        return 1
    # 回读校验（同 cmd_settime：防磁盘只读 / 被安全软件拦截等静默失败）
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            back = json.load(f).get("schedule", {})
        if (back.get("retry_interval_minutes") != iv
                or back.get("retry_window_minutes") != win
                or bool(back.get("retry_enabled")) != bool(enabled)):
            print(f"✗ 配置写入后回读不一致（期望 {iv}/{win}/{enabled}，"
                  f"读到 {back.get('retry_interval_minutes')}/"
                  f"{back.get('retry_window_minutes')}/"
                  f"{back.get('retry_enabled')}）")
            return 1
    except (OSError, ValueError) as e:
        print(f"✗ 配置回读失败：{e!r}")
        return 1
    if enabled:
        print(f"✓ 自动补跑已开启：每 {iv} 分钟重试一次，持续 {win} 分钟"
              f"（最多约 {win // iv} 次）；全部签完立即停止")
    else:
        print("✓ 自动补跑已关闭（仅按设定时间各执行一次）")
    print("✓ 正在重新注册计划任务…")
    if not cmd_install(cfg):
        print("✗ 计划任务注册失败，设置已保存但未生效，请重试")
        return 1
    return 0


def cmd_uninstall():
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false; Write-Host 'UNINSTALLED'"],
        capture_output=True, text=True, timeout=60)
    if "UNINSTALLED" in (r.stdout or ""):
        log("计划任务已移除")
        return True
    log(f"移除计划任务失败（可能本来就不存在）：{(r.stderr or '').strip()}")
    return False


def cmd_status():
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue;"
         "if ($t) { $i = $t | Get-ScheduledTaskInfo;"
         "Write-Host ('state=' + $t.State);"
         "Write-Host ('lastRun=' + $i.LastRunTime + ' result=' + $i.LastTaskResult);"
         "Write-Host ('nextRun=' + $i.NextRunTime) } else { Write-Host 'state=NOT_INSTALLED' }"],
        capture_output=True, text=True, timeout=60)
    print("== 计划任务 ==")
    print((r.stdout or "").strip() or (r.stderr or "").strip())

    print("\n== 今日签到状态（state.json） ==")
    today = datetime.date.today().isoformat()
    state = load_state()
    if not state:
        print("（空）")
    for k, v in state.items():
        print(f"  {k}: {v}{' ✓今日已完成' if v == today else ''}")

    print("\n== 最近 20 条签到记录 ==")
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            lines = f.readlines()[-20:]
        for line in lines:
            e = json.loads(line)
            print(f"  {e['ts']} [{e['platform']}] {e['account']}: {e['status']} — {e['detail']}")
    except OSError:
        print("（暂无记录）")

    print("\n== 签到记录文档 ==")
    print(f"  {RECORD_PATH}")
    print("  （每日自动追加：日期/时间/平台/账号/成功失败/详情，Excel 可直接打开）")


# ---------------- 首次登录 ----------------

def cmd_login_traework(name=None):
    cfg = load_config()
    accounts = cfg.get("traework", {}).get("accounts", [])
    if not accounts:
        print("config.json 中没有 TraeWork 账号，请先在 accounts 数组里添加。")
        return
    acc = None
    if name:
        acc = next((a for a in accounts if a.get("name") == name or a.get("profile") == name), None)
        if acc is None:
            print(f"未找到账号 {name}，现有：{[a.get('name') for a in accounts]}")
            return
    else:
        acc = accounts[0]
        print(f"未指定账号名，默认使用第一个：{acc.get('name')}")
    import tw_checkin
    tw_checkin.interactive_login(cfg, acc, log)


def cmd_capture():
    import wb_checkin
    wb_checkin.capture_current(log)


def main():
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    parser = argparse.ArgumentParser(description="TraeWork + WorkBuddy 每日签到")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="立即执行一轮签到")
    p_run.add_argument("--only", choices=["workbuddy", "traework", "traecn"], default=None)
    p_run.add_argument("--force", action="store_true", help="忽略今日已成功记录，强制重跑")
    p_run.add_argument("--silent", action="store_true", help="仅写日志，不打印控制台（计划任务用）")

    p_login = sub.add_parser("login", help="首次登录引导")
    p_login.add_argument("platform", choices=["traework", "workbuddy"])
    p_login.add_argument("name", nargs="?", default=None)

    sub.add_parser("capture", help="保存 WorkBuddy 当前登录账号令牌（多账号）")
    sub.add_parser("install", help="注册计划任务")
    p_set = sub.add_parser("settime", help="设定每日自动执行时间并重注册任务")
    p_set.add_argument("times", nargs="+", help="HH:MM 格式，1~4 个，如 08:30 20:00")
    p_retry = sub.add_parser("setretry", help="设定自动补跑（重试）参数并重注册任务")
    p_retry.add_argument("interval", type=int, help="重试间隔（分钟，1~360）")
    p_retry.add_argument("window", type=int, help="补跑持续时长（分钟，1~1440）")
    p_retry.add_argument("--off", action="store_true", help="关闭自动补跑")
    sub.add_parser("uninstall", help="移除计划任务")
    sub.add_parser("status", help="查看状态")

    args = parser.parse_args()

    global SILENT
    SILENT = getattr(args, "silent", False)

    if args.cmd == "run":
        run_all(args.only, args.force)
    elif args.cmd == "login":
        if args.platform == "traework":
            cmd_login_traework(args.name)
        else:
            print("1. 打开 WorkBuddy 桌面端并登录账号 A")
            print("2. 运行: python checkin.py capture   （保存账号 A）")
            print("3. 在 WorkBuddy 里切换到账号 B，再次运行 capture")
            print("4. 如需所有账号：重复步骤 3")
            print("提示：当前正在登录的账号无需 capture，签到时会自动读取。")
    elif args.cmd == "capture":
        cmd_capture()
    elif args.cmd == "install":
        cmd_install(load_config())
    elif args.cmd == "settime":
        sys.exit(cmd_settime(args.times) or 0)
    elif args.cmd == "setretry":
        sys.exit(cmd_setretry(args.interval, args.window,
                              enabled=not args.off) or 0)
    elif args.cmd == "uninstall":
        cmd_uninstall()
    elif args.cmd == "status":
        cmd_status()


def dispatch_frozen(argv=None):
    """exe 模式统一入口分发。

    exe 只有一个可执行文件，GUI / 签到 / 结果卡片都从它启动，
    靠首参区分：
      --run-checkin <子命令...>  转发给 checkin 的命令行
      --toast <json>             画签到结果卡片
      （无参数）                 启动 GUI
    返回退出码；返回 None 表示「不处理，交给 GUI」。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return None

    if argv[0] == "--toast":
        payload = argv[1] if len(argv) > 1 else "{}"
        try:
            import toast_gui
        except ImportError:
            return 1
        # toast_gui 从命令行读 payload
        sys.argv = ["toast_gui.py", payload]
        try:
            toast_gui.main()
        except Exception:  # noqa: BLE001
            return 1
        return 0

    if argv[0] == "--run-checkin":
        sys.argv = ["checkin.py"] + argv[1:]
        try:
            main()
        except SystemExit as e:
            return e.code or 0
        return 0

    return None


if __name__ == "__main__":
    main()
