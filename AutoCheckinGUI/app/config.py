#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""与既有自动化脚本共用的配置与数据访问层。

GUI 不另建配置体系：Root 指向本项目根目录（即 checkin.py 所在目录），
读写同一个 config.json / logs/state.json / logs/history.jsonl / 签到记录.csv，
因此 GUI 与计划任务产生的签到记录天然一致、互不冲突。
"""
import json
import os
import sys

# 根目录定位：
#   脚本模式 —— GUI 目录整体放在项目内，向上找到 checkin.py 为准；
#   exe 模式 —— 优先复用已有的项目数据目录，找不到才在 exe 旁新建。
#
# 为什么 exe 模式要「优先复用」：
#   打包后 checkin.py 被打进 _internal（不摆在 exe 旁边），所以早期版本
#   的 `exe 旁有没有 checkin.py` 判定永远为假，ROOT 直接落到 exe 所在目录。
#   用户双击 exe 后看到的是一个**空配置的新程序**：状态栏「0 个账号」、
#   账号列表「暂无账号」，跟自己在项目里跑出来的数据完全脱节。
#   因此这里按「已存在的数据 > exe 旁 > 用户目录」的顺序找，
#   保证 GUI 与命令行版共用同一份 config.json / logs。
_HOME_ENV = "TRAEBUDDY_HOME"


def _has_data(base):
    """该目录是否像一份已有的项目数据（有 config.json 或 logs/）。"""
    if not base or not os.path.isdir(base):
        return False
    return (os.path.isfile(os.path.join(base, "config.json"))
            or os.path.isdir(os.path.join(base, "logs")))


def _has_project(base):
    """该目录是否是完整的项目根（含 checkin.py）。"""
    return bool(base) and os.path.isfile(os.path.join(base, "checkin.py"))


def _frozen_root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return None


_HERE = os.path.dirname(os.path.abspath(__file__))
# 桌面版整理后的结构：桌面版/AutoCheckinGUI/app 与
# 桌面版/核心脚本 分开；优先使用同一桌面版目录内的核心脚本和数据，
# 只有旧布局/源码模式找不到时才回退到项目根。
_CORE_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "核心脚本"))
_CANDIDATES = [
    _CORE_DIR,
    os.path.abspath(os.path.join(_HERE, "..", "..")),
    os.path.abspath(os.path.join(_HERE, "..", "..", "..")),
]


def _search_candidates():
    """按优先级列出「可能已有数据」的候选目录。"""
    out = []

    def add(p):
        if p:
            p = os.path.abspath(p)
            if p not in out:
                out.append(p)

    # 1) 显式指定（用户可用环境变量把 GUI 指到自己的项目目录）
    add(os.environ.get(_HOME_ENV))
    # 2) 整理后的桌面版核心目录：与 GUI 同属一个版本，不认领脚本版数据。
    add(_CORE_DIR)
    # 3) exe 自身目录（便携版把数据放在 exe 旁，这是最直觉的位置）
    add(_frozen_root())
    # 3) 常见项目位置：用户把 exe 拷到项目里、或项目就在桌面
    fr = _frozen_root()
    if fr:
        cur = fr
        for _ in range(4):          # exe 可能被放在 dist/ 等子目录里
            cur = os.path.dirname(cur)
            if cur and cur != os.path.dirname(cur):
                add(cur)
    add(os.path.abspath(os.path.join(_HERE, "..", "..")))
    # ★ 这里**不要**写死 `~/Desktop/<某具体项目名>` 之类的路径（与 runtime.py 同口径）：
    #   写死会让「放在任意位置的便携版」意外认领某台机器上的目录，行为难以预测。
    #   项目根的定位已由「exe 上级目录」与「app/ 上级目录」通用覆盖。
    # 4) 用户级默认位置（首次运行时在这初始化）
    add(os.path.join(os.path.expanduser("~"), ".traebuddy"))
    return out


def _locate_root():
    """定位数据根目录。

    整理后的冻结桌面版先认同版本目录中的 ``核心脚本``，再走通用
    runtime.data_root()；这样它不会因为项目根仍保留脚本版兼容入口，
    误把桌面版数据写回脚本版。

    直接复用 `runtime.data_root()` —— **命令行版（checkin.py）与 GUI 必须用
    同一套判定**。历史上这里各写一套，导致 GUI 认项目根、计划任务认 exe 旁，
    用户的设置与签到结果被写进两个目录，表现为「设置不生效 / 自动签到好像
    没跑 / 弹窗也没有」。统一之后从根上避免分家。

    优先级（详见 runtime.data_root 文档）：
      1. 环境变量 TRAEBUDDY_HOME
      2. 已有完整项目（含 checkin.py）+ 数据的目录 —— GUI 与脚本共用
      3. exe 旁边已有数据
      4. exe 旁边
      5. 用户级 ~/.traebuddy
    """
    try:
        # 冻结版 exe 位于：桌面版/AutoCheckinGUI/dist/<程序>/。
        # 先检查同一桌面版目录下的核心脚本，避免认领项目根脚本版。
        if getattr(sys, "frozen", False):
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            cur = exe_dir
            for _ in range(6):
                if os.path.basename(cur).lower() == "autocheckingui":
                    frozen_core = os.path.join(os.path.dirname(cur), "核心脚本")
                    if os.path.isfile(os.path.join(frozen_core, "checkin.py")):
                        return frozen_core
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
        # runtime.py 在项目根（源码模式）或打包进 _internal（exe 模式）。
        # 源码模式下 GUI 的 sys.path[0] 是 AutoCheckinGUI/，够不到项目根，
        # 所以先把候选目录挂上 sys.path 再导入。
        for _base in reversed(_search_candidates()):
            if _base and os.path.isdir(_base) and _base not in sys.path:
                sys.path.insert(0, _base)
        import runtime as _rt
        _root = _rt.data_root(hint=_HERE)
        if _root:
            return _root
    except Exception:  # noqa: BLE001  runtime 缺失时退回本地判定
        pass
    for base in _search_candidates():
        if _has_project(base) and _has_data(base):
            return base
    for base in _search_candidates():
        if _has_project(base):
            return base
    fr = _frozen_root()
    if fr and _has_data(fr):
        return fr
    if fr:
        if os.access(fr, os.W_OK):
            return fr
        return os.path.join(os.path.expanduser("~"), ".traebuddy")
    for base in _CANDIDATES:
        if os.path.exists(os.path.join(base, "checkin.py")):
            return base
    cur = _HERE
    for _ in range(5):
        cur = os.path.dirname(cur)
        if os.path.exists(os.path.join(cur, "checkin.py")):
            return cur
    return _CANDIDATES[0]


ROOT = _locate_root()
sys.path.insert(0, ROOT)

CONFIG_PATH = os.path.join(ROOT, "config.json")
LOG_DIR = os.path.join(ROOT, "logs")
STATE_PATH = os.path.join(LOG_DIR, "state.json")
HISTORY_PATH = os.path.join(LOG_DIR, "history.jsonl")
RECORD_PATH = os.path.join(LOG_DIR, "签到记录.csv")
SHOTS_DIR = os.path.join(LOG_DIR, "screenshots")
WB_TOKENS = os.path.join(ROOT, "tokens_workbuddy.json")
TC_TOKENS = os.path.join(LOG_DIR, "tokens_traecn.json")
TW_PROFILES = os.path.join(ROOT, "profiles", "traework")

TASK_NAME = "AutoDailyCheckin"

DEFAULT_CONFIG = {
    "traework": {"enabled": True, "accounts": []},
    "workbuddy": {"enabled": True, "auto_detect_current": True, "use_saved_tokens": True},
    "traecn": {"enabled": True},
    "schedule": {
        "daily_times": ["09:00", "21:00"],
        "checkin_time": "09:00",
        "run_at_logon": True,
        "logon_delay_seconds": 10,
        "network_wait_minutes": 720,
        # 自动补跑（重试）：过了设定时间后每 retry_interval_minutes 分钟触发一次，
        # 持续 retry_window_minutes 分钟。门控幂等 —— 已签完则秒退、不开浏览器。
        # ★ 这三个键原先只在 checkin.default_config 里，本文件漏了：GUI 首次生成
        #   config.json 时不会写它们，设置页也就读不到（会退回硬编码默认值）。
        "retry_enabled": True,
        "retry_interval_minutes": 30,
        "retry_window_minutes": 360,
    },
    # 账号凭据自动重新捕获（2026-09-18 新增）。
    # 背景：三平台凭据寿命差两个数量级（TraeCN 8 小时 / TraeWork 会话 5 天 /
    #       WorkBuddy 60 天），用户没法凭直觉判断「要不要重新捕获一下」。
    # ★ 注意：**每次签到执行时本来就会重新捕获一次**（这是正确性所必需的，
    #   不受本段开关影响）。本段只控制「程序开着的时候，后台多久去看一眼」，
    #   目的是让概览页 / 账号页显示的「有效 / 即将过期」保持准确。
    "credentials": {
        "auto_refresh": True,
        # 允许 0 ~ 168 小时（见 services.credential_refresh.MIN/MAX_INTERVAL_HOURS）；
        # 设置页提供 1/6/12/24 的快捷档位（QUICK_CHOICES），也可以手填任意整数。
        # 0 = 关闭后台刷新（仍会在每次签到前自动捕获）。
        "refresh_interval_hours": 6,
        # ★ 新增（2026-09-21）：**源文件一变就立刻重新捕获**，独立于上面的间隔。
        #   对应「重新打开 Trae CN IDE / WorkBuddy 拿到新令牌」这类事件 ——
        #   否则间隔设成 24 小时时，中途重开软件也要等 24 小时界面才更新。
        #   受 `credential_refresh.SOURCE_WATCH_MIN_GAP_S`（10 分钟）冷却，
        #   避免「IDE 一直在写日志」退化成每分钟刷一次。
        "refresh_on_source_change": True,
    },
    "options": {
        "headless": True,
        # 签到记录 CSV 的自定义导出位置（2026-09-29 新增）。
        # 主记录始终写在数据根 logs/签到记录.csv（各页面与命令行都读它），
        # 这里只是**额外同步一份副本**到用户自选位置（桌面 / 网盘目录等）。
        #   record_export_enabled : 是否启用
        #   record_export_path    : 目录，或以 .csv 结尾的完整文件路径
        "record_export_enabled": False,
        "record_export_path": "",
        # 补跑轮次的弹窗策略（见 checkin._maybe_notify）：
        #   True  = 仅关键节点弹窗（第 1 次执行 / 全部签完 / 补跑窗口最后一轮仍失败）
        #   False = 每轮都弹
        # 手动执行永远弹，不受此开关影响。默认 True —— 开了补跑以后，若某平台
        # 一整天都失败，每轮都弹会变成 12 次刷屏，那反而没人看弹窗了。
        "retry_toast_quiet": True,
    },
}


# ---------------- 配置 ----------------

def is_first_run():
    """数据目录是否还没初始化过（既无 config.json 也无 logs/）。"""
    return not _has_data(ROOT)


def ensure_data_dir():
    """首次运行时把数据目录铺好，让程序开箱可用。

    做三件事（都是幂等的，非首次运行不会有任何改动）：
      1. 写一份默认 config.json —— 否则界面读不到配置，账号列表永远是空的；
      2. 建 logs/ 目录 —— 签到记录、状态、截图都往里写；
      3. 建 profiles/traework/ 目录 —— Playwright 会话目录的父目录。

    返回 (是否本次新建, 根目录路径)，供 UI 提示用户数据位置。
    """
    created = False
    try:
        os.makedirs(ROOT, exist_ok=True)
        if not os.path.isfile(CONFIG_PATH):
            save_config(json.loads(json.dumps(DEFAULT_CONFIG)))
            created = True
        for d in (LOG_DIR, TW_PROFILES):
            os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return created, ROOT


def load_config():
    """读取配置；缺失或损坏时回退默认值，保证 GUI 永远能起来。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("config root is not an object")
    except (OSError, ValueError):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    # 补齐缺失的段落，避免各处 .get(...) 链式取值 KeyError
    for key, val in DEFAULT_CONFIG.items():
        if key not in cfg or not isinstance(cfg[key], dict):
            cfg[key] = json.loads(json.dumps(val))
        elif isinstance(val, dict):
            for k2, v2 in val.items():
                cfg[key].setdefault(k2, json.loads(json.dumps(v2)))
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------- 签到状态与历史 ----------------

def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_history(limit=None):
    """读取 history.jsonl，返回按时间升序的记录列表。"""
    rows = []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows[-limit:] if limit else rows


PLATFORM_CN = {"traework": "TraeWork", "workbuddy": "WorkBuddy", "traecn": "TraeCN"}
STATUS_CN = {"ok": "成功", "skip": "已签跳过", "fail": "失败",
             # ★ busy 有两种来源（浏览器 profile 被占用 / 服务端限流 9074），
             #   共同点是「本轮没确认签到、下一轮补跑会自动复核」。
             #   旧文案「已跳过(占用)」只描述了其中一种，对限流场景是错的。
             "busy": "待复核",
             "expired": "令牌过期", "error": "执行异常"}
STATUS_TONE = {"ok": "green", "skip": "blue", "fail": "red",
               "busy": "amber",
               "expired": "amber", "error": "red"}


def as_bool(v, default=True):
    """把配置里的值稳妥地转成 bool。

    ★ 为什么不能直接 `bool(v)`：`config.json` 是**用户可以手编的文件**。
      一旦写成 `"headless": "false"` 这种字符串形式，`bool("false")` 是 **True**
      —— 「关掉开关」反而变成「打开」，而且**不会有任何报错**。
      同理 `0`/`"0"`/`"no"`/`"off"`/`""` 都该算 False。
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


def count_status(results):
    """把签到结果按「成功/跳过」「待复核」「失败」三档计数。

    ★ `busy`（服务端限流 9074 / 浏览器 profile 被占用）**不是失败** ——
      它只是本轮没能确认，下一轮补跑会自动复核。旧代码把它算进「失败」，
      于是界面/日志会报「TraeWork 失败」，而实际积分可能早已到账
      （2026-09-19 实测：报失败后 2 分钟的补跑即确认 +150）。
    """
    ok = sum(1 for r in results if r.get("status") in ("ok", "skip"))
    busy = sum(1 for r in results if r.get("status") == "busy")
    return ok, busy, len(results) - ok - busy


def today_str():
    import datetime
    return datetime.date.today().isoformat()


# ---------------- 签到记录自定义导出 ----------------
# 与核心脚本 checkin.py 的 `record_export_target` / `sync_record_export`
# 保持**完全一致**的取值口径：
#   * 以 .csv 结尾 → 完整文件路径；否则视为目录，其下生成 签到记录.csv
#   * 未启用 / 未填写 → None（保持原有行为，不做任何额外写入）
# 主记录 RECORD_PATH 始终保留，自定义位置只多一份副本 —— 这样即使目标盘
# 被拔出、路径无权限，也只影响副本，不会破坏签到与其它页面。

def record_export_target(cfg=None):
    """自定义导出目标文件的绝对路径；未启用返回 None。"""
    cfg = cfg or load_config()
    opts = cfg.get("options") or {}
    if not as_bool(opts.get("record_export_enabled"), False):
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


def export_record_now(cfg=None):
    """立即把主记录同步一份到自定义位置。返回 `(ok, 目标路径或错误原因)`。"""
    target = record_export_target(cfg)
    if not target:
        return False, "未启用自定义导出位置"
    try:
        if not os.path.isfile(RECORD_PATH):
            return False, "还没有签到记录（先执行一次签到）"
        if os.path.abspath(target) == os.path.abspath(RECORD_PATH):
            return False, "导出位置不能就是主记录本身，请换一个文件夹"
        d = os.path.dirname(target)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(RECORD_PATH, "rb") as src:
            data = src.read()
        with open(target, "wb") as dst:
            dst.write(data)
        return True, target
    except OSError as e:
        return False, str(e)


def save_record_export(enabled, path):
    """保存「自定义导出位置」设置到 config.json。返回 (ok, 说明)。"""
    try:
        cfg = load_config()
        cfg.setdefault("options", {})
        cfg["options"]["record_export_enabled"] = bool(enabled)
        cfg["options"]["record_export_path"] = str(path or "").strip()
        save_config(cfg)
        return True, "已保存"
    except Exception as e:  # noqa: BLE001
        return False, repr(e)


# ---------------- 账号枚举 ----------------

# Chromium 存放 Cookies 的位置**随版本变过**：
#   旧版  Default/Cookies
#   新版  Default/Network/Cookies      ← 本机实测（TraeWork 的 Playwright 会话）
# 只查旧路径会把「已经登录」误判成「尚未登录」：
# 本机 profiles/traework/trae1 的 Cookies 就在 Network/ 下（32KB、登录后持续更新），
# 而 Default/Cookies 根本不存在。这类误判不会报错，只会让界面显示错状态。
_COOKIE_RELS = (("Default", "Network", "Cookies"), ("Default", "Cookies"))


def traework_profile_dir(profile):
    """TraeWork 会话目录的**唯一**构造入口。

    原先 `os.path.join(TW_PROFILES, profile)` 在本文件里散了两处；
    收敛到这里，避免哪天改了目录结构漏掉一处（那类漏改只会表现为
    「某个入口读不到会话」，不报错）。
    """
    if not profile:
        return None
    return os.path.join(TW_PROFILES, str(profile))


def traework_cookie_files(profile_dir):
    """该会话目录下所有可能存放 Cookies 的路径（新版优先）。"""
    return [os.path.join(profile_dir, *rel) for rel in _COOKIE_RELS]


def traework_has_session(profile_dir):
    """会话目录里是否存在**非空**的 Cookies。返回 ``(bool, 命中的路径)``。

    空文件不算登录（Playwright 建目录时会先落一个 0 字节的库文件）。
    """
    for p in traework_cookie_files(profile_dir):
        try:
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return True, p
        except OSError:
            continue
    return False, None


def list_traework_accounts(cfg=None):
    """TraeWork 账号：来自 config + 独立 profile 目录里的昵称缓存。"""
    cfg = cfg or load_config()
    out = []
    for a in cfg.get("traework", {}).get("accounts", []):
        name = a.get("name") or a.get("profile") or "default"
        profile = a.get("profile") or name
        pdir = traework_profile_dir(profile)
        nick = ""
        try:
            p = os.path.join(pdir, "nickname.txt")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    nick = (f.read() or "").strip()
        except OSError:
            pass
        has_profile = os.path.isdir(pdir)
        has_session, _ck = traework_has_session(pdir)
        out.append({
            "id": name,
            "name": name,
            "profile": profile,
            "nickname": nick,
            "display": nick or name,
            "label": a.get("label") or "",
            "has_profile": has_profile,
            # ★ 新增：区分「目录存在」与「真的登录过」。
            #   只看 has_profile 会把「建了空目录、从没登录」也显示成「已登录」。
            "has_session": has_session,
            "logged_in": has_session or bool(nick),
            "platform": "traework",
            "key": f"traework:{name}",
        })
    return out


def list_workbuddy_accounts(cfg=None):
    """WorkBuddy 账号：当前客户端登录态 + 已 capture 的令牌快照。"""
    cfg = cfg or load_config()
    out = {}
    try:
        import wb_checkin
        wb_cfg = cfg.get("workbuddy", {})
        if wb_cfg.get("auto_detect_current", True):
            cur = wb_checkin._read_current_auth()
            if cur:
                cur["_source"] = "current"
                out[cur["uid"]] = cur
        if wb_cfg.get("use_saved_tokens", True):
            for uid, item in wb_checkin._load_saved().items():
                if uid in out:
                    continue
                out[uid] = {
                    "uid": uid,
                    "nickname": wb_checkin.safe_text(
                        item.get("nickname")),
                    "phone": wb_checkin.safe_text(item.get("phone")),
                    "token": item.get("token") or "",
                    "domain": item.get("domain") or "",
                    "expires_at": item.get("expires_at") or 0,
                    "enterprise_id": "",
                    "captured_at": item.get("captured_at") or "",
                    "_source": "captured",
                }
    except Exception:  # noqa: BLE001
        pass
    out_list = []
    for uid, acc in out.items():
        # ★ 昵称可能是**加密信封 dict**（WorkBuddy 改了本地存储格式）——
        #   统一走 wb_checkin.safe_text 规范化，绝不把 dict 漏到界面
        #   （否则账号页 `display[:1]` 会抛 KeyError 导致整页打不开）。
        try:
            nick = wb_checkin.safe_text(acc.get("nickname"))
        except Exception:  # noqa: BLE001
            nick = acc.get("nickname") if isinstance(
                acc.get("nickname"), str) else ""
        out_list.append({
            "id": uid,
            "uid": uid,
            "name": nick or uid[:8] + "****",
            "nickname": nick,
            "phone": wb_checkin.safe_text(acc.get("phone")),
            "token": acc.get("token") or "",
            "expires_at": acc.get("expires_at") or 0,
            "source": acc.get("_source") or "captured",
            "captured_at": acc.get("captured_at") or "",
            "platform": "workbuddy",
            "key": f"workbuddy:{uid}",
            "display": nick or (uid[:8] + "****"),
        })
    out_list.sort(key=lambda x: (x["source"] != "current", x["display"]))
    return out_list


def list_traecn_accounts(cfg=None):
    """TraeCN 账号：扫描 IDE 日志得到的 JWT（含缓存回退）。"""
    try:
        import tc_checkin
        accs = tc_checkin.collect_accounts(cfg, lambda *_: None)
    except Exception:  # noqa: BLE001
        accs = []
    out = []
    for a in accs:
        out.append({
            "id": a["uid"],
            "uid": a["uid"],
            "name": a.get("name") or f"TraeCN-{a['uid'][-4:]}",
            "display": a.get("name") or f"TraeCN-{a['uid'][-4:]}",
            "exp": a.get("exp") or 0,
            "token": a.get("token") or "",
            "has_did": bool(a.get("did")),
            "platform": "traecn",
            "key": f"traecn:{a['uid']}",
        })
    return out


def all_accounts(cfg=None):
    cfg = cfg or load_config()
    return {
        "traework": list_traework_accounts(cfg),
        "workbuddy": list_workbuddy_accounts(cfg),
        "traecn": list_traecn_accounts(cfg),
    }


# ---------------- 本机账号自动发现 ----------------
# 目的：像 WorkBuddy 那样「直接捕获本机已登录的账号」，而不是让用户手填。
# 三个平台各自的登录态落点不同：
#   traework : profiles/traework/<profile>/（Chromium 会话目录）+ nickname.txt
#   workbuddy: 桌面端 auth 文件（未捕获的新账号会出现在这里）
#   traecn   : IDE 日志里的 JWT（本身即自动发现，这里只标出「尚未纳入」的）
# 只做「读 + 列」，不写任何东西；真正的导入由 UI 侧逐条确认后调用。


def _discover_traework(cfg):
    """扫描 TraeWork 账号来源。

    两路来源，缺一不可：
    1) **本机 Trae 系客户端**（Trae CN IDE / TraeWork CN 桌面端）
       —— 读客户端日志里的 ScreenName + UserID，确认「本机软件登录的是谁」。
       这些账号与 TraeCN 板块共用 api.trae.cn 账号体系，签到由 traecn 通道完成，
       因此这里只做「识别 + 提示」，不重复纳入 traework 列表。
    2) **profiles/traework 下的 Playwright 会话目录**
       —— 网页版 work.trae.cn 的独立登录态；判定依据是 Cookies 非空
       或存在 nickname.txt。这类才是需要「纳入」的对象。

    ★ Cookies 的位置随 Chromium 版本变过（``Default/Network/Cookies`` /
      ``Default/Cookies``），必须两个都查 —— 详见 ``traework_has_session``。
      只查旧路径会把「已登录」误判成「未登录」。

    缺 Cookies 的空目录视为「建了但没登录」，单独标出来提示用户去登录。
    """
    found = []
    known = {a.get("profile") or a.get("name")
             for a in cfg.get("traework", {}).get("accounts", [])}

    # ---- 来源 1：本机 Trae 系客户端（只读识别） ----
    for item in _discover_local_clients():
        item["in_config"] = True   # 客户端账号由 traecn 通道签到，无需纳入 traework
        found.append(item)

    # ---- 来源 2：Playwright 会话目录 ----
    base = TW_PROFILES
    if not os.path.isdir(base):
        return found
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return found
    for profile in entries:
        pdir = os.path.join(base, profile)
        if not os.path.isdir(pdir):
            continue
        nick = ""
        try:
            np = os.path.join(pdir, "nickname.txt")
            if os.path.exists(np):
                with open(np, encoding="utf-8") as f:
                    nick = (f.read() or "").strip()
        except OSError:
            pass
        # ★ 两个位置都要查（Default/Network/Cookies 是新版 Chromium 的位置）
        has_cookies, _ck = traework_has_session(pdir)
        logged_in = has_cookies or bool(nick)
        found.append({
            "platform": "traework",
            "source": "profile",
            "id": profile,
            "name": profile,
            "display": nick or profile,
            "profile": profile,
            "nickname": nick,
            "has_session": has_cookies,
            "logged_in": logged_in,
            "in_config": profile in known,
            "detail": ("网页会话已登录" if logged_in else "会话目录为空，尚未登录")
                      + ("（已在账号列表）" if profile in known else "（未加入账号列表）"),
        })
    return found


def _discover_local_clients():
    """识别本机 Trae 系客户端（IDE / 桌面端）当前登录的账号。

    TraeWork CN 桌面端（TRAE SOLO CN）与 Trae CN IDE 共用 api.trae.cn 账号体系，
    但前者的日志中 token 已脱敏，拿不到可签到令牌 —— 所以这里只做「识别」，
    让用户在界面上能看到「本机这个软件登录的是哪个账号」，
    至于签到，由 traecn 通道（同样读这些客户端日志取令牌）负责。

    纯只读：不写任何文件、不发网络请求。
    """
    found = []
    try:
        import tc_checkin
        identities = tc_checkin.scan_client_identity()
    except Exception:  # noqa: BLE001
        return found
    for uid, info in identities.items():
        client = info.get("client") or "Trae 客户端"
        kind = info.get("kind") or ""
        name = info.get("name") or uid[-4:]
        has_token = bool(info.get("has_token"))
        # ★ 令牌**真正**来自哪个客户端（`token_client` 由 scan_client_identity 归因）。
        #   原先只看 has_token，而 has_token 是按 uid 跨客户端「或」出来的 →
        #   会把 IDE 的令牌算到桌面端头上，界面上出现「TRAE SOLO CN（桌面端）·
        #   签到由 TraeCN 通道生效」这种不实描述（实测该桌面端 0 枚明文令牌）。
        tok_client = info.get("token_client") or ""
        if has_token:
            extra = " · 签到由 TraeCN 通道生效"
            if tok_client and tok_client != client:
                extra += "（令牌来自 %s）" % tok_client
        else:
            extra = " · 该客户端日志中无可用令牌（需 Trae CN IDE 提供）"
        found.append({
            "platform": "traework",
            "source": "client",
            "id": f"client:{uid}",
            "name": name,
            "display": name,
            "uid": uid,
            "client": client,
            "logged_in": True,
            "in_config": True,
            "detail": f"{client}（{kind}）已登录 · 账号 …{uid[-6:]}" + extra,
        })
    return found


def _discover_workbuddy(cfg):
    """本机 WorkBuddy 登录态：区分「当前桌面端登录」与「已保存快照」。"""
    found = []
    try:
        import wb_checkin
    except Exception:  # noqa: BLE001
        return found
    saved = {}
    try:
        saved = wb_checkin._load_saved()
    except Exception:  # noqa: BLE001
        pass
    cur = None
    try:
        cur = wb_checkin._read_current_auth()
    except Exception:  # noqa: BLE001
        cur = None
    if cur:
        uid = cur["uid"]
        # ★ 昵称/手机号可能是**加密信封 dict**（WorkBuddy 改了本地存储格式）——
        #   必须规范化，否则 dict 会被直接渲染成 `display[:1]` → KeyError
        #   → 整页「页面加载失败」（2026-09-24 实测）。
        # ★ 明文优先；加密信封时从历史快照反查真名（2026-09-25）
        _nick = wb_checkin.readable_name(uid, cur.get("nickname"))
        found.append({
            "platform": "workbuddy",
            "id": uid,
            "name": _nick or uid[:8],
            "display": _nick or (uid[:8] + "****"),
            "uid": uid,
            "phone": wb_checkin.safe_text(cur.get("phone")),
            "logged_in": True,
            "in_config": uid in saved,
            "detail": "本机桌面端当前登录" + (
                "（已保存快照）" if uid in saved else "（尚未保存）"),
        })
    for uid, item in saved.items():
        if cur and uid == cur["uid"]:
            continue
        _nick = wb_checkin.readable_name(uid, item.get("nickname"))
        found.append({
            "platform": "workbuddy",
            "id": uid,
            "name": _nick or uid[:8],
            "display": _nick or (uid[:8] + "****"),
            "uid": uid,
            "phone": wb_checkin.safe_text(item.get("phone")),
            "logged_in": True,
            "in_config": True,
            "detail": f"已保存的令牌快照（{item.get('captured_at') or '时间未知'}）",
        })
    return found


def _discover_traecn(cfg):
    """TraeCN：能扫到但当前不在账号列表里的（列表本身就是扫描结果，主要做提示）。"""
    found = []
    try:
        import tc_checkin
        accs = tc_checkin.collect_accounts(cfg, lambda *_: None)
    except Exception:  # noqa: BLE001
        return found
    for a in accs:
        name = a.get("name") or f"TraeCN-{a['uid'][-4:]}"
        found.append({
            "platform": "traecn",
            "id": a["uid"],
            "name": name,
            "display": name,
            "uid": a["uid"],
            "logged_in": True,
            "in_config": True,   # TraeCN 自动出现在列表，无需导入
            "detail": "IDE 日志中的有效令牌（自动生效）",
        })
    return found


_DISCOVERERS = {
    "traework": _discover_traework,
    "workbuddy": _discover_workbuddy,
    "traecn": _discover_traecn,
}


def discover_accounts(platform=None):
    """扫描本机可发现的账号（只读）。platform=None 时返回三个平台的字典。"""
    cfg = load_config()
    if platform:
        fn = _DISCOVERERS.get(platform)
        return fn(cfg) if fn else []
    return {p: fn(cfg) for p, fn in _DISCOVERERS.items()}


def adopt_traework(profile, name=None):
    """把发现到的 TraeWork 会话目录纳入 config.json 的账号列表。

    这正是「不需要手填」的关键：profile 目录已经在磁盘上了，
    此时只要把它的名字登记进配置即可，登录态是现成的。
    """
    profile = (profile or "").strip()
    if not profile:
        return False, "缺少会话标识"
    pdir = traework_profile_dir(profile)
    if not os.path.isdir(pdir):
        return False, f"会话目录不存在：{profile}"
    cfg = load_config()
    accs = cfg.setdefault("traework", {}).setdefault("accounts", [])
    if any((a.get("name") or a.get("profile")) == profile for a in accs):
        return False, f"账号 {profile} 已在列表中"
    label = (name or "").strip() or profile
    accs.append({"name": label, "profile": profile})
    save_config(cfg)
    refresh_accounts_async()
    return True, f"已纳入账号 {label}"


# ---------------- 账号缓存 ----------------
# TraeCN 的账号枚举会扫描 IDE 日志并调用接口反查昵称，首次约 10~15 秒；
# TraeWork 要读磁盘。若每次切页都同步重算会明显卡顿，因此做短时缓存，
# 由后台线程刷新，UI 始终读缓存立即返回。

_CACHE = {"data": None, "ts": 0.0}
_CACHE_TTL = 60.0        # 缓存有效期（秒）
_CACHE_LOCK = None


def _lock():
    global _CACHE_LOCK
    if _CACHE_LOCK is None:
        import threading
        _CACHE_LOCK = threading.Lock()
    return _CACHE_LOCK


def accounts_cached(cfg=None, max_age=None):
    """读缓存的账号快照；过期或为空时同步计算一次。

    UI 线程请优先用 peek_accounts()，把 refresh_accounts_async() 放在后台。
    """
    import time as _t
    ttl = _CACHE_TTL if max_age is None else max_age
    with _lock():
        fresh = (_CACHE["data"] is not None
                 and (_t.time() - _CACHE["ts"]) < ttl)
        if fresh:
            return _CACHE["data"]
    data = all_accounts(cfg)
    with _lock():
        _CACHE["data"] = data
        _CACHE["ts"] = _t.time()
    return data


def peek_accounts():
    """只读缓存，绝不触发计算；无缓存时返回 None。"""
    with _lock():
        return _CACHE["data"]


def refresh_accounts_async(on_done=None, force=True):
    """后台线程刷新账号缓存，完成后回调 on_done(data)（在主线程由调用方调度）。"""
    import threading

    def work():
        try:
            data = all_accounts()
            import time as _t
            with _lock():
                _CACHE["data"] = data
                _CACHE["ts"] = _t.time()
            if on_done:
                on_done(data)
        except Exception:  # noqa: BLE001
            pass

    th = threading.Thread(target=work, daemon=True, name="acct-refresh")
    th.start()
    return th
