#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号凭据的「寿命」说明 + 可配置的自动重新捕获。

为什么需要
----------
三个平台的凭据寿命**差了两个数量级**，用户没法凭直觉判断「要不要去重新捕获一下」。
2026-09-18 在本机真实数据上实测（解码 JWT 的 ``iat`` / ``exp``、读 Chromium
Cookies 的 ``expires_utc``）：

============  ===============  ============  ============================
平台           捕获方式          实测寿命       续期方式
============  ===============  ============  ============================
TraeCN        桌面端（IDE 日志） **8 小时**     打开一次 Trae CN IDE
TraeWork CN   本地浏览器会话     会话 5 天 /    长期票据有效时自动续期
                                 长期票据 ~1 年
WorkBuddy     桌面端（auth 文件）**60 天**      重新登录一次桌面端
============  ===============  ============  ============================

★ 为什么只有 TraeWork 是「浏览器会话」，不能全走桌面端？
------------------------------------------------------
**不是实现偷懒，是产品本身不给。** 2026-09-18 实测（`tests/_probe_tw_cred.py` /
`_probe_tw_has_token.py` / `_probe_desktop_token.py`）：

  · **Trae CN（IDE）** 日志里有**明文 JWT** —— 本机扫到 39 枚未过期令牌
    （``uid=3956523050480058``）。→ 桌面端可捕获。
  · **WorkBuddy** 桌面端把令牌落在本地 auth 文件里。→ 桌面端可捕获。
  · **TRAE SOLO CN（TraeWork CN 桌面端）** 日志里的令牌**是脱敏的**：
    ``"token":"******"``，明文 JWT 命中 **0 枚**；再扫它的磁盘
    （Local Storage / Session Storage / IndexedDB / solo-lite* / User /
    Network 等共 **8007 个文件、300 MB**）也**没有任何明文 JWT**。
    → 桌面端**拿不到令牌**。而 TraeWork 的每日签到入口只存在于
    ``work.trae.cn`` 网页产品里，所以只能复用本机浏览器 profile 的会话。
    ★ 这一步**同样是自动的**：读的是本地磁盘上的 Cookies 库，不需要你去开网页。

  ★ 另外：TraeWork 与 TraeCN **共用同一个签到接口**（``api.trae.cn`` 的
    checkin_credits）。TraeWork 那条路只是「另一个拿到同一个令牌的地方」——
    ``tw_checkin._try_api_checkin`` 取到页面令牌后会 ``record=True`` 回写共享
    缓存，供 TraeCN 通道使用。所以「TraeWork 走浏览器」并不吃亏。

★ 最关键的一条事实：**每次签到执行时都会重新捕获一次**，不需要用户手动管。
  ``tc_checkin.collect_accounts()`` 每次都会重扫 IDE 日志并把最新的令牌合并落盘；
  ``wb_checkin._collect_accounts()`` 每次都会重读桌面端 auth 文件。
  所以「令牌还有 2 小时过期」并不等于「签到会失败」—— 只要那之前 IDE 写过新令牌，
  下一轮签到就会自动用上新的。

那本模块补什么？两件事：
  1. **把「寿命 / 上次捕获 / 还剩多久」摆到界面上**，用户不用猜、不用来问；
  2. 提供一个**可配置的后台刷新**（默认 6 小时），程序开着的时候也让缓存保持新鲜，
     这样概览页 / 账号页显示的「有效 / 即将过期」才是准的。

⚠️ 不要用本模块的刷新去**替代**签到前的那次捕获：后台刷新只是「多看一眼」，
   签到前的捕获才是「保证用的是最新令牌」。两者不能互相替代。
"""
from __future__ import annotations

import json
import os
import time

# 后台刷新的最小轮询粒度（秒）。真正「该不该刷」由配置里的间隔决定，
# 这里只是决定多久去问一次「到点了吗」—— 一分钟一次，开销可忽略。
TICK_SECONDS = 60

# 快捷档位（小时）。0 表示关闭后台刷新。
INTERVAL_CHOICES = (0, 1, 3, 6, 12, 24)
# 设置页上「一键填入」的档位（不含 0，0 用开关表达）
QUICK_CHOICES = (1, 6, 12, 24)
DEFAULT_INTERVAL_HOURS = 6

# ★ 允许手动填写的范围（小时）。**必须与设置页 `_save_cred` 的校验同一套常量**。
#   原先 `cfg_section` 用的是「必须正好等于 `INTERVAL_CHOICES` 里的某一档」，
#   而保存侧允许 0~168 —— 于是填 48 会「保存成功」，读回来却被静默改成默认 6，
#   而输入框里仍然显示 48：**界面显示的生效值 ≠ 真实生效值**。
#   这类「存了但读不到」的不一致比报错更难发现，所以改成范围校验。
MIN_INTERVAL_HOURS = 0
MAX_INTERVAL_HOURS = 168

# ★ 新增（2026-09-21）：**源文件一变就立刻重新捕获**，独立于上面的间隔。
#   默认开启 —— 这是「重新打开对应软件就自动拿到新令牌」那条需求。
DEFAULT_SOURCE_WATCH = True

# 源变化触发的**最小间隔**（秒）。
#
# ⚠️ 为什么必须有这个冷却：Trae CN IDE 开着的时候**一直在写日志**，
#    单看「mtime 变了」几乎每分钟都成立。没有冷却的话就会退化成
#    「每分钟重新捕获一次」——而 `_scan_ide_logs()` 实测要 **0.394 s**
#    （它要读日志内容找 JWT），一分钟一次纯属浪费。
#    10 分钟冷却后：真正「重开软件」那一刻的令牌最多 10 分钟内被拿到，
#    而稳态开销 = 每 10 分钟 0.4 s，可以忽略。
SOURCE_WATCH_MIN_GAP_S = 600

# 三平台凭据寿命（实测值，不是估的；见模块 docstring）
#
# 每条都带三样东西，界面上一行说清：
#   ``capture`` 捕获方式（桌面端 / 本地浏览器会话）—— 直接回答「为什么它跟别人不一样」
#   ``where``   凭据具体存在哪里（**本地**，都写着「本地读取」）
#   ``why``     为什么是这个来源（产品限制，不是实现取舍）
LIFESPANS = {
    "traecn": {
        "label": "TraeCN（IDE）",
        "capture": "桌面端",
        "where": "IDE 日志里的 JWT（本地读取）",
        "why": "Trae CN IDE 会把明文令牌写进自己的会话日志，直接读日志即可",
        "life_hours": 8.0,
        "life_text": "约 8 小时",
        "renew": "打开一次 Trae CN IDE 就会写入新令牌",
    },
    "traework": {
        # ★ 标签从「（网页会话）」改成「（网页签到）」：
        #   「网页会话」读起来像「另一种捕获方式 / 要你去开网页」，
        #   而事实上 TraeWork 与 TraeCN 走的是**同一个签到接口**，
        #   区别只在「签到入口开在网页里」。措辞改成「网页签到」后，
        #   用户看到的是「这个平台的签到功能在网页上」，而不是
        #   「这个平台的凭据要特殊处理」。
        "label": "TraeWork CN（网页签到）",
        "capture": "本地浏览器会话",
        "where": "本地会话目录里的 Cookie（自动读取）",
        "why": "签到入口只在 work.trae.cn 网页里；桌面端（TRAE SOLO CN）日志中的"
               "令牌是脱敏的（\"token\":\"******\"），磁盘上也没有明文令牌",
        # 取会话 Cookie（X-Cloudide-Session）；没有它时取鉴权 Cookie 里最早的那个
        "life_hours": 5 * 24,
        "life_text": "会话约 5 天（长期票据约 1 年，会自动续期）",
        # ★ 原文案是「打开一次桌面端并登录即可续期」—— **错的**：
        #   桌面端不会去动这个浏览器 profile。真正续期靠长期票据
        #   （d_ticket / odin_tt / sid_guard，约 1 年）在下次访问时自动换新会话；
        #   票据也失效时才需要重新登一次浏览器会话。
        "renew": "长期票据有效时会自动续期；失效后用「账号管理 → 重新登录」再登一次",
    },
    "workbuddy": {
        "label": "WorkBuddy（桌面端）",
        "capture": "桌面端",
        "where": "桌面端 auth 文件里的 JWT（本地读取）",
        "why": "WorkBuddy 桌面端把登录令牌落在本地 auth 文件里，直接读文件即可",
        "life_hours": 60 * 24,
        "life_text": "60 天",
        "renew": "重新登录一次 WorkBuddy 桌面端",
    },
}
PLATFORMS = ("traecn", "traework", "workbuddy")

# 设置页顶上的一句话总览 —— 直接回答「为什么只有 TraeWork 不一样」
CAPTURE_SUMMARY = (
    "三个平台里，TraeCN 与 WorkBuddy 本来就是「桌面端捕获」（读 IDE 日志 / "
    "读桌面端 auth 文件）；TraeWork 的每日签到入口只存在于网页产品里，"
    "而且它的桌面端日志中令牌是脱敏的、磁盘上也没有明文令牌，"
    "所以只能复用本机浏览器会话 —— 这一步同样是程序自动读本地文件，"
    "不需要你去打开网页。"
)


# 「即将过期」阈值：低于这个剩余时间就在界面上标黄
SOON_SECONDS = 24 * 3600


def _stamp_path():
    """上次刷新时间的落盘位置（放 logs/ 里，跟着数据根走）。"""
    try:
        from .. import config as C
        return os.path.join(C.LOG_DIR, "credential_refresh.json")
    except Exception:  # noqa: BLE001
        return None


def _read_stamp():
    """读整份刷新记账文件（损坏时给空 dict，绝不抛）。"""
    p = _stamp_path()
    if not p:
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def last_refresh():
    """上次成功刷新的时间戳（epoch 秒）；从未刷过返回 None。"""
    v = _read_stamp().get("last_refresh")
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


def _write_stamp(d):
    """整份写回记账文件（目录不存在就建；写不动就静默放弃，返回 bool）。

    ★ 返回值**必须**用起来：调用方要拿它决定「能不能据此判断」——
      写不动时若还当作成功，就会变成**每 60 秒刷一次**的死循环。
    """
    p = _stamp_path()
    if not p:
        return False
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def _mark_refreshed(ts=None):
    """记录「刚刷过」+ **当时的源文件指纹**。

    ★ 指纹必须和刷新时间**一起**落盘：下次判断「源变了没」要以「上次刷新的
      那一刻」为基准，否则拿不到「变没变」的参照。
    """
    _write_stamp({"last_refresh": ts or time.time(),
                  "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "fingerprint": source_fingerprint()})


def _adopt_baseline_if_missing():
    """★ 旧版记账文件里没有指纹 → 补一个基准；返回 True 表示「刚补上，应刷一次」。

    为什么必须有这段（2026-09-21 补，**这是本轮最后一个真实缺口**）
    --------------------------------------------------------------
    `sources_changed()` 要拿「上次刷新那一刻的指纹」当参照。**从旧版本升上来**时，
    记账文件里只有 `last_refresh` / `at`、**没有** `fingerprint`
    （实测生产文件就是 `{"last_refresh": ..., "at": "..."}`）→ 源变化判据
    **永远返回 False**。

    单看这一条似乎只是「要等下一次周期刷新才有基准」，但用户的间隔可以设成
    **0**（界面上就写着「仅在检测到软件重新打开 / 凭据文件变化时刷新」）：
      ① 源变化 → 没基准 → False；
      ② 周期兜底 → `hours <= 0` → 直接 False。
    ⇒ **`due()` 永远是 False，这条功能彻底失效，而且一点报错都没有。**

    ★ 这里**先把基准落盘、再**返回 True，而不是「返回 True 等 refresh 去写」：
      `refresh()` 只在 `any_ok` 时才调 `_mark_refreshed()`
      （三个平台全失败就不写）。若依赖它写，一旦全失败就会
      **每 60 秒刷一次**的死循环。先落盘则「哪怕这次刷新全失败，基准也已存在」，
      下一拍 `sources_changed()` 就有参照可比 → 天然收敛。
    """
    st = _read_stamp()
    if not st.get("last_refresh"):
        return False            # 从未刷过 → 交给周期判据（它本来就会刷）
    if st.get("fingerprint"):
        return False            # 已有基准
    cur = source_fingerprint()
    if not cur:
        return False            # 一个源文件都读不到 → 别据此判断
    st["fingerprint"] = cur
    if not _write_stamp(st):
        return False            # 写不动 → 绝不能据此刷（否则每拍都刷）
    return True


# ---------------------------------------------------------------- 源文件指纹
#
# ★ 为什么需要这个（2026-09-21 新增）
# ----------------------------------
# 原设计的判据**只有**「距上次刷新是否超过间隔」，于是有一个用户能直接
# 感受到的毛病：
#   · 把刷新间隔设成 24 小时；
#   · 中途关掉 Trae CN IDE 再打开 —— IDE 往日志里写了一枚**全新的 8 小时令牌**；
#   · 界面上却仍然显示旧令牌的剩余时间，**最长要等 24 小时才会更新**。
# 用户的原话是「确保重新打开对应软件自动获取令牌更新」—— 正是这件事。
#
# 所以补一条**独立于间隔**的触发：**凭据源文件一变就立刻重新捕获**。
#
# ⚠️ 判据用「文件 mtime + size」而**不是**「重新扫一遍日志找令牌」：
#    `_scan_ide_logs()` 实测 **0.394 s**（要读日志内容跑正则），
#    而本函数每分钟被问一次 —— 每分钟读一遍日志是不可接受的。
#    `stat()` 几个文件是微秒级，而「软件重新打开」必然改 mtime，信号足够。
# ⚠️ 会有误报（日志写了些与令牌无关的东西）→ 后果只是多做一次捕获，
#    捕获本身是幂等的（`refresh()` 只比对，无变化时只报「已是最新」）。
#    再加上 `SOURCE_WATCH_MIN_GAP_S` 冷却，稳态开销可忽略。

def _tc_log_fingerprint():
    """TraeCN / TraeWork 桌面端：各日志根下**最新几个会话目录**里 `*.log` 的最新 mtime。

    ★ 为什么只取最新 3 个会话目录：写入一定发生在**最近被写过的**会话里。
      `_scan_ide_logs()` 取前 8 个是为了「**找到**令牌」（怕新版客户端换了目录），
      而本函数只需要「**发现变化**」，最新 3 个足够 ——
      实测代价从 0.08 s 降到 0.01 s（两者都要每分钟跑一次，差 8 倍）。
    """
    out = {}
    try:
        import tc_checkin
        roots = tc_checkin.client_log_roots()
    except Exception:  # noqa: BLE001
        return out
    for root, client, _kind in roots:
        try:
            import pathlib
            p = pathlib.Path(root)
            if not p.exists():
                continue
            sessions = [d for d in p.iterdir() if d.is_dir()]
            sessions.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            newest = 0
            for sd in sessions[:3]:
                for f in sd.rglob("*.log"):
                    try:
                        newest = max(newest, f.stat().st_mtime_ns)
                    except OSError:
                        continue
            if newest:
                out["tc:" + client] = newest
        except Exception:  # noqa: BLE001
            continue
    return out


def _wb_auth_fingerprint():
    """WorkBuddy：桌面端 auth 文件的 (mtime_ns, size)。"""
    out = {}
    try:
        import wb_checkin
        for path in getattr(wb_checkin, "AUTH_CANDIDATES", ()) or ():
            try:
                st = os.stat(path)
            except OSError:
                continue
            out["wb:" + os.path.basename(path)] = [st.st_mtime_ns, st.st_size]
    except Exception:  # noqa: BLE001
        pass
    return out


def _tw_cookie_fingerprint():
    """TraeWork：每个会话目录里 Cookies 库的 (mtime_ns, size)。"""
    out = {}
    try:
        from .. import config as C
        for a in (C.list_traework_accounts() or []):
            pdir = C.traework_profile_dir(a.get("profile"))
            if not pdir:
                continue
            for f in C.traework_cookie_files(pdir):
                try:
                    st = os.stat(f)
                except OSError:
                    continue
                out["tw:%s:%s" % (a.get("name") or "default",
                                  os.path.basename(f))] = [st.st_mtime_ns, st.st_size]
    except Exception:  # noqa: BLE001
        pass
    return out


def source_fingerprint():
    """三个平台「凭据源文件」的廉价指纹。**只 stat，不读内容。**

    返回 ``{标签: 值}``；读不到的项直接不出现（**绝不猜**）。

    ★ 必须先 ``_ensure_root()``（2026-09-21 实测踩到）：
      下面三个采集函数都是 ``import tc_checkin`` / ``import wb_checkin`` ——
      这两个模块在**项目根**上，而把项目根挂上 `sys.path` 这件事是
      `app.config` 在导入时顺手做的。若本函数在 `app.config` 之前被调用，
      三个 import 全部 `ModuleNotFoundError`，而每一层都是
      ``except Exception: pass`` → **静默返回空指纹**。
      实测：不先导入 `app.config` 时只拿到 1 项（TraeWork 的 Cookie，
      因为它走 `from .. import config` 而不是顶层 import），
      导入后是 4 项（Trae CN / TRAE SOLO CN / TraeWork / WorkBuddy 全齐）。
      空指纹的后果不是报错，而是 `sources_changed()` **永远判 False** ——
      「重新打开软件就自动刷新」这条功能**无声失效**，比崩溃更难查。
    """
    _ensure_root()
    fp = {}
    fp.update(_tc_log_fingerprint())
    fp.update(_wb_auth_fingerprint())
    fp.update(_tw_cookie_fingerprint())
    return fp


def sources_changed(cfg=None, now=None):
    """凭据源文件相对「上次刷新那一刻」有没有变化。

    ★ 没有基准（从未刷过 / 旧版记账文件里没指纹）时返回 **False** ——
      不能返回 True，否则「第一次打开程序」会变成一个无条件的刷新理由，
      把「刚刷过就不该刷」这条判据冲掉。
    """
    if cfg is None:
        try:
            from .. import config as C
            cfg = C.load_config()
        except Exception:  # noqa: BLE001
            cfg = {}
    sec = cfg_section(cfg)
    if not sec["refresh_on_source_change"]:
        return False
    base = _read_stamp().get("fingerprint")
    if not isinstance(base, dict) or not base:
        return False          # 没有参照 → 交给间隔判据，别在这里刷
    cur = source_fingerprint()
    if not cur:
        return False          # 一个源文件都没读到 → 别据此判断
    return cur != base


def _watch_gap_ok(now=None):
    """距上次刷新是否已经超过源变化触发的冷却时间。"""
    last = last_refresh()
    if last is None:
        return True
    now = now if now is not None else time.time()
    return (now - last) >= SOURCE_WATCH_MIN_GAP_S


def _as_bool(v, default=True):
    """薄封装 —— 实现只有一份，在 `app.config.as_bool`（避免两处漂移）。

    ★ 为什么不能直接 `bool(v)`：JSON 里手写成字符串时，
      `bool("false")` 是 **True** —— 「关掉开关」反而变成「打开」，
      而且**不会有任何报错**。配置是用户能直接编辑的文件，必须按字符串理解。
    """
    try:
        from .. import config as C
        return C.as_bool(v, default)
    except Exception:  # noqa: BLE001
        # 极端情况（app.config 不可导入）下退回最小可用判断 ——
        # 至少不能让 "false" 变成 True。
        if isinstance(v, str):
            return v.strip().lower() not in ("", "0", "false", "no", "off",
                                             "none", "null")
        return default if v is None else bool(v)


def cfg_section(cfg=None):
    """取配置里的 credentials 段（缺失时给默认值，绝不 KeyError）。"""
    if cfg is None:
        try:
            from .. import config as C
            cfg = C.load_config()
        except Exception:  # noqa: BLE001
            cfg = {}
    sec = (cfg or {}).get("credentials")
    if not isinstance(sec, dict):
        sec = {}
    auto = _as_bool(sec.get("auto_refresh", True), True)
    try:
        hours = int(sec.get("refresh_interval_hours", DEFAULT_INTERVAL_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_INTERVAL_HOURS
    # 范围校验（而不是「必须命中档位」）—— 见上面 MIN/MAX_INTERVAL_HOURS 的说明
    if not (MIN_INTERVAL_HOURS <= hours <= MAX_INTERVAL_HOURS):
        hours = DEFAULT_INTERVAL_HOURS
    # ★ 新增（2026-09-21）：源文件一变就立刻刷新，独立于上面的间隔。
    watch = _as_bool(sec.get("refresh_on_source_change", DEFAULT_SOURCE_WATCH),
                     DEFAULT_SOURCE_WATCH)
    return {"auto_refresh": auto,
            "refresh_interval_hours": hours,
            "refresh_on_source_change": watch}


def due(cfg=None, now=None):
    """现在该不该后台刷新一次。

    两条**互相独立**的触发，命中任一即刷：

      ① **源文件变了** —— 对应「重新打开对应软件」「IDE 写出了新令牌」
         这类事件。**不受间隔限制**，但受 `SOURCE_WATCH_MIN_GAP_S` 冷却
         （否则 IDE 一直写日志会退化成每分钟刷一次）。
      ② **距上次刷新超过用户设的间隔** —— 周期性兜底。

    另有一条**一次性迁移**（①′）：记账文件来自旧版本（有刷新时间、没指纹）时，
    先补上基准并刷一次 —— 否则「间隔设 0 + 只靠源变化」会**永远不刷**。
    详见 `_adopt_baseline_if_missing()`。

    ★ 判据里**没有**「距进程启动多久」—— 否则每次开程序都会立刻刷一次，
      用户设的 24 小时形同虚设。
    ★ ① 之所以要独立于 ②：用户把间隔设成 24 小时，中途重开 IDE 拿到新令牌，
      却要等 24 小时界面才更新 —— 这正是用户报的那个毛病。
    """
    sec = cfg_section(cfg)
    if not sec["auto_refresh"]:
        return False
    now = now if now is not None else time.time()
    # ①′ 旧版记账文件缺基准 → 补上并刷一次（**必须排在 ① 之前**：没基准时 ① 必不成立）
    if sec["refresh_on_source_change"] and _adopt_baseline_if_missing():
        return True
    # ① 源变化：不受间隔限制，但受冷却限制
    if sec["refresh_on_source_change"] and _watch_gap_ok(now) \
            and sources_changed(cfg, now):
        return True
    # ② 周期兜底
    hours = sec["refresh_interval_hours"]
    if hours <= 0:
        return False
    last = last_refresh()
    if last is None:
        return True          # 从未刷过 → 该刷
    return (now - last) >= hours * 3600


def next_due_text(cfg=None, now=None):
    """给界面用的一句话：下次什么时候刷 / 为什么不会刷。"""
    sec = cfg_section(cfg)
    watch = sec["refresh_on_source_change"]
    hours = sec["refresh_interval_hours"]
    if not sec["auto_refresh"]:
        return "后台自动刷新已关闭（每次签到仍会自动重新捕获）"
    if hours <= 0:
        return ("仅在检测到软件重新打开 / 凭据文件变化时刷新"
                if watch else
                "后台自动刷新已关闭（每次签到仍会自动重新捕获）")
    last = last_refresh()
    if last is None:
        return "尚未刷新过，程序启动后会立即刷新一次"
    now = now if now is not None else time.time()
    left = hours * 3600 - (now - last)
    # ⚠️ 尾注刻意**不含**「立即刷新」四个字：`next_due_text()` 的既有断言里
    #    用「立即刷新」区分「从未刷过」那一支，尾注若也含这四个字，
    #    那条断言就会在任何分支都成立 —— 等于把判据测没了。
    tail = "（软件重新打开时会自动刷新）" if watch else ""
    if left <= 0:
        return "已到刷新时间，程序运行中会很快刷新" + tail
    return "约 %s后自动刷新%s" % (_dur_text(left), tail)


def _dur_text(sec):
    """把秒数说成人话。"""
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return "未知"
    if sec <= 0:
        return "已过期"
    if sec < 3600:
        return "%d 分钟" % max(1, round(sec / 60))
    if sec < 86400:
        h = sec / 3600
        return "%.1f 小时" % h if h < 10 else "%d 小时" % round(h)
    return "%.1f 天" % (sec / 86400)


# ---------------------------------------------------------------- 当前快照

def _ensure_root():
    """把项目根挂上 ``sys.path``。

    ★ 这一步**不是可选的**：``import tc_checkin`` / ``wb_checkin`` 依赖项目根在
      `sys.path` 上，而这件事是 `app.config` 在导入时顺手做的
      （`app/config.py` 末尾那句 `sys.path.insert(0, ROOT)`）。
      如果本模块在 `app.config` 之前就去 `import tc_checkin`，会
      `ModuleNotFoundError`，而下面每条都是 `except Exception: pass` ——
      于是**静默变成「本机没有凭据」**（本项目最典型的假故障）。
      所以进任何采集函数前先导入 `app.config`。
    """
    try:
        from .. import config as C  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _traework_expiry(profile_dir):
    """从 Chromium 的 Cookies 库里读出网页会话最早到期的时间（epoch 秒）。

    只读打开（``mode=ro&immutable=1``）：浏览器正在跑时直接连会锁库。
    读不到就返回 0 —— **绝不猜**，猜出来的数字比没有更误导。

    ★ 只认「鉴权 Cookie」（2026-09-18 修）
    -------------------------------------
    原实现是「取所有 Cookie 里最早到期的那个」。实测某个真实 profile 里有
    36 条 Cookie，最早的是 ``_uetsid``（**Bing 的统计 Cookie，0.8 天**）——
    于是界面天天报「TraeWork 剩余 0.8 天（即将过期）」，而真正管签到的
    ``X-Cloudide-Session`` 还有 5 天。统计 Cookie 的寿命跟凭据毫无关系，
    混进来只会制造假警报。现在只认 `_AUTH_COOKIE_NAMES` 里的名字。
    """
    ts, _name = _traework_expiry_detail(profile_dir)
    return ts


def _traework_expiry_detail(profile_dir):
    """同上，但额外返回**读到的是哪条 Cookie**（给界面显示来源用）。

    返回 ``(epoch 秒, Cookie 名)``；读不到时 ``(0, "")``。
    """
    if not profile_dir:
        return 0, ""
    try:
        from .. import config as C
        files = C.traework_cookie_files(profile_dir)
    except Exception:  # noqa: BLE001
        return 0, ""
    now = time.time()
    for f in files:
        if not os.path.exists(f):
            continue
        try:
            import sqlite3
            uri = "file:%s?mode=ro&immutable=1" % f.replace("\\", "/")
            con = sqlite3.connect(uri, uri=True, timeout=1.0)
            try:
                rows = con.execute(
                    "select name, host_key, expires_utc from cookies").fetchall()
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            continue
        best = 0
        best_name = ""
        for row in rows:
            name, host, exp = (list(row) + ["", "", 0])[:3]
            if not exp:
                continue
            name = str(name)
            host = str(host or "")
            if any(j in host for j in _JUNK_COOKIE_HOSTS):
                continue          # 第三方统计，不是凭据
            if name not in _AUTH_COOKIE_NAMES:
                continue          # 不在鉴权白名单里，别拿它的寿命当凭据寿命
            # Chromium 纪元：1601-01-01 起的微秒数
            ts = exp / 1000000.0 - 11644473600
            if ts <= now:
                continue
            if name == _SESSION_COOKIE_NAME:
                return ts, name    # 这是签到真正依赖的那个会话，优先
            if not best or ts < best:
                best, best_name = ts, name
        if best:
            return best, best_name
    return 0, ""


# 签到真正依赖的会话 Cookie（最高优先级）
_SESSION_COOKIE_NAME = "X-Cloudide-Session"

# 鉴权白名单：只有这些名字的 Cookie 才算「凭据寿命」。
# 实测真实 profile 里的名字（`tests/_probe_tw_cred.py` 会打印全部）。
_AUTH_COOKIE_NAMES = frozenset((
    "X-Cloudide-Session",
    "sessionid", "sessionid_ss", "sid_tt", "sid_guard",
    "sid_ucp_v1", "ssid_ucp_v1", "session_tlb_tag",
    "d_ticket", "odin_tt", "uid_tt", "uid_tt_ss",
    "passport_auth_status", "passport_auth_status_ss",
    "passport_csrf_token", "passport_mfa_token",
    "has_biz_token", "ttwid",
))

# 明确排除的第三方统计 / 广告域名：它们的 Cookie 寿命从 0.8 天到 389 天都有，
# 与凭据无关，混进来就是假警报的来源。
_JUNK_COOKIE_HOSTS = ("bing.com", "clarity.ms", "doubleclick.net",
                      "google-analytics.com", "googletagmanager.com",
                      "criteo.com", "facebook.com")


def entries():
    """列出当前所有凭据的剩余时间。

    返回 ``[{platform, account, exp, left, left_text, captured_at, source}]``，
    ``exp`` 统一成 epoch 秒（WorkBuddy 的毫秒戳在这里换算）。**只读**。
    """
    _ensure_root()
    now = time.time()
    out = []

    # TraeCN —— ★ 报「签到真正会用的那一份」，不是「日志里最新的一份」。
    #   `collect_accounts` 会按发现时间优先把日志令牌与缓存合并（`_better_token`），
    #   所以这里复用同一个比较器做一次**只读**合并 —— 否则界面会报一个
    #   与签到实际使用的不同的有效期，用户看着就矛盾。
    try:
        import tc_checkin
        fresh = tc_checkin._scan_ide_logs(lambda m: None) or {}
        try:
            cache = tc_checkin._load_cache() or {}
        except Exception:  # noqa: BLE001
            cache = {}
        merged = {}
        for uid, val in fresh.items():
            exp = val[1] if isinstance(val, (tuple, list)) and len(val) > 1 else 0
            saved = val[2] if isinstance(val, (tuple, list)) and len(val) > 2 else None
            merged[uid] = {"exp": int(exp or 0), "saved_at": saved,
                           "token": val[0] if val else ""}
        for uid, ent in cache.items():
            cand = {"exp": int(ent.get("exp") or 0),
                    "saved_at": tc_checkin._entry_freshness(ent),
                    "token": ent.get("token", "")}
            if tc_checkin._better_token(cand, merged.get(uid)):
                merged[uid] = cand
        for uid, ent in merged.items():
            exp = ent["exp"]
            if not exp:
                continue
            name = (cache.get(uid) or {}).get("name")
            out.append({
                "platform": "traecn",
                "account": name or ("TraeCN …%s" % str(uid)[-4:]),
                "exp": float(exp),
                "left": float(exp) - now,
                "captured_at": ent.get("saved_at"),
                "source": "IDE 日志 / 令牌缓存",
            })
    except Exception:  # noqa: BLE001
        pass

    # WorkBuddy —— ★ 以「**实时** auth 文件」为准，落盘快照只作兜底。
    #   原实现只读 `_load_saved()`（`capture_current` 落的快照），于是
    #   「重新登录一次 WorkBuddy」之后，界面会一直显示**旧令牌**的剩余时间，
    #   直到下一次捕获 —— 间隔设得长就是几小时。这正是用户报的
    #   「重新打开对应软件要能自动拿到新令牌」在**显示**上的表现。
    #   `_read_current_auth()` 只读文件、**不校验 exp**，所以这里自己比一下：
    #   **取到期更晚的那个**（与 TraeCN 的 `_better_token` 同一思路）。
    try:
        import wb_checkin
        rows = {u: dict(it) for u, it in (wb_checkin._load_saved() or {}).items()}
        live = None
        try:
            live = wb_checkin._read_current_auth()
        except Exception:  # noqa: BLE001
            live = None
        if live and live.get("uid"):
            uid = live["uid"]
            cur = rows.get(uid) or {}
            live_exp = live.get("expires_at") or 0
            if live_exp and live_exp >= (cur.get("expires_at") or 0):
                rows[uid] = {"uid": uid,
                             "nickname": live.get("nickname") or cur.get("nickname") or "",
                             "token": live.get("token") or cur.get("token") or "",
                             "expires_at": live_exp,
                             "captured_at": cur.get("captured_at"),
                             "_live": True}
        for uid, it in rows.items():
            exp = (it.get("expires_at") or 0) / 1000.0
            out.append({
                "platform": "workbuddy",
                "account": it.get("nickname") or ("WorkBuddy %s" % str(uid)[:8]),
                "exp": exp,
                "left": (exp - now) if exp else 0.0,
                "captured_at": it.get("captured_at"),
                # ★ 标明数据来源：用户看到「已保存快照」就知道还没刷新过，
                #   不会以为程序读错了。
                "source": ("桌面端 auth 文件（实时）" if it.get("_live")
                           else "桌面端 auth 文件（已保存快照）"),
            })
    except Exception:  # noqa: BLE001
        pass

    # TraeWork —— 浏览器会话；到期时间直接从 Cookies 库读出来
    try:
        from .. import config as C
        for a in (C.list_traework_accounts() or []):
            # ★ 键名是 `profile`（不是 `profile_dir`）；`logged_in` 由 config 侧
            #   统一判定（已修过「只看目录存不存在」那个假登录的坑），这里直接复用。
            pdir = C.traework_profile_dir(a.get("profile"))
            exp, ckname = _traework_expiry_detail(pdir)
            if exp:
                src = "本地会话 Cookie（%s）" % ckname
            elif a.get("logged_in"):
                src = "本地会话目录（未识别到鉴权 Cookie）"
            else:
                src = "本地会话目录（未登录）"
            out.append({
                "platform": "traework",
                "account": a.get("nickname") or a.get("name") or "TraeWork",
                "exp": float(exp or 0),
                "left": (float(exp) - now) if exp else 0.0,
                "captured_at": None,
                "cookie": ckname,
                "source": src,
                "logged_in": bool(a.get("logged_in")),
            })
    except Exception:  # noqa: BLE001
        pass

    for e in out:
        e["left_text"] = _dur_text(e["left"]) if e.get("exp") else "—"
        e["soon"] = bool(e.get("exp")) and 0 < e["left"] < SOON_SECONDS
        e["expired"] = bool(e.get("exp")) and e["left"] <= 0
    return out


def soonest(entries_list=None):
    """最快过期的那个凭据；没有带 exp 的返回 None。"""
    items = [e for e in (entries_list if entries_list is not None else entries())
             if e.get("exp")]
    if not items:
        return None
    return min(items, key=lambda e: e["left"])


# ---------------------------------------------------------------- 重新捕获

def refresh(log=print):
    """重新捕获三个平台的本地凭据。

    返回 ``(ok, detail, stats)``：
      * ``ok``      —— 有没有至少成功刷新一个平台（不代表「有新令牌」）；
      * ``detail``  —— 一句话结果，可直接拿去 toast；
      * ``stats``   —— ``{"traecn": {...}, "workbuddy": {...}, "traework": {...}}``

    ★ 复用的是**签到路径上同一套函数**（``collect_accounts`` / ``capture_current``），
      不另写一份 —— 否则「界面刷新」和「签到实际用的」会慢慢漂移成两套逻辑。
    """
    stats = {}
    detail_bits = []
    _ensure_root()

    # TraeCN：重扫 IDE 日志并合并落盘（collect_accounts 内部会 _save_cache）
    try:
        import tc_checkin
        before = {}
        try:
            before = dict(tc_checkin._load_cache() or {})
        except Exception:  # noqa: BLE001
            pass
        accs = tc_checkin.collect_accounts(None, lambda m: None) or []
        after = {}
        try:
            after = dict(tc_checkin._load_cache() or {})
        except Exception:  # noqa: BLE001
            pass
        new_uids = [u for u in after if u not in before]
        changed = [u for u in after
                   if u in before and after[u].get("token") != before[u].get("token")]
        stats["traecn"] = {"ok": True, "accounts": len(accs),
                           "new": len(new_uids), "updated": len(changed)}
        if new_uids or changed:
            detail_bits.append("TraeCN 新增 %d / 更新 %d" % (len(new_uids), len(changed)))
    except Exception as e:  # noqa: BLE001
        stats["traecn"] = {"ok": False, "error": repr(e)}

    # WorkBuddy：重读桌面端 auth 文件（capture_current 内含过期护栏）
    try:
        import wb_checkin
        before = dict(wb_checkin._load_saved() or {})
        cur = wb_checkin._read_current_auth()
        ok = wb_checkin.capture_current(lambda m: None)
        after = wb_checkin._load_saved() or {}
        new = [u for u in after if u not in before]
        upd = [u for u in after
               if u in before and after[u].get("token") != before[u].get("token")]
        # ★ 「本机登录态已过期 → 护栏拦下」不是失败，是**按设计保住了令牌**。
        #   不能把它算成 error，否则界面上会一直挂着一条吓人的失败提示。
        skipped = bool(cur and not ok)
        stats["workbuddy"] = {"ok": bool(ok), "accounts": len(after),
                              "new": len(new), "updated": len(upd),
                              "skipped": skipped}
        if ok and (new or upd):
            detail_bits.append("WorkBuddy 新增 %d / 更新 %d" % (len(new), len(upd)))
        elif skipped:
            detail_bits.append("WorkBuddy 本机登录态已过期，已保留原令牌")
    except Exception as e:  # noqa: BLE001
        stats["workbuddy"] = {"ok": False, "error": repr(e)}

    # TraeWork：会话在浏览器 profile 里，只探在不在，不做写操作
    try:
        from .. import config as C
        tw = C.list_traework_accounts() or []
        logged = [a for a in tw if a.get("logged_in")]
        stats["traework"] = {"ok": True, "accounts": len(tw), "logged_in": len(logged)}
        # ★ 这里**故意不报**「TraeWork 新增 0」：它没有可增量捕获的东西，
        #   每次都报一条只会让用户以为出问题了。
    except Exception as e:  # noqa: BLE001
        stats["traework"] = {"ok": False, "error": repr(e)}

    # 「护栏拦下」不算失败：我们确实去看过了，只是按设计没动它
    any_ok = any(v.get("ok") or v.get("skipped") for v in stats.values())
    if any_ok:
        _mark_refreshed()
    detail = "、".join(detail_bits) if detail_bits else "凭据已是最新（无新增）"
    if not any_ok:
        detail = "重新捕获失败，请查看日志"
    try:
        log("[credentials] 重新捕获：%s" % detail)
    except Exception:  # noqa: BLE001
        pass
    return any_ok, detail, stats


# ---------------------------------------------------------------- 展示

def lifespan_rows():
    """三平台寿命说明的**结构化**形式（设置页按行渲染用）。

    每项：``{key, label, capture, where, why, life_text, renew}``。
    设置页把 ``capture`` 单独拎出来显示，就是为了让用户一眼看到
    「两个是桌面端、一个不是」，以及**为什么**不是。
    """
    rows = []
    for p in PLATFORMS:
        s = LIFESPANS[p]
        rows.append({
            "key": p,
            "label": s["label"],
            "capture": s.get("capture", ""),
            "where": s.get("where", ""),
            "why": s.get("why", ""),
            "life_text": s.get("life_text", ""),
            "renew": s.get("renew", ""),
        })
    return rows


def lifespan_lines():
    """三平台寿命说明（纯文本，CLI / 报告用）。

    ★ 每行都带上**捕获方式**：用户最容易误解的就是「为什么只有 TraeWork
      不是桌面端」，把 capture 摆在行首附近，疑问当场就消了。
    """
    lines = []
    for p in PLATFORMS:
        s = LIFESPANS[p]
        head = "%s：%s" % (s["label"], s["life_text"])
        if s.get("capture"):
            head += " · 捕获方式：%s" % s["capture"]
        lines.append(head)
        lines.append("    来源：%s" % s.get("where", ""))
        if s.get("why"):
            lines.append("    为什么：%s" % s["why"])
        lines.append("    续期：%s" % s.get("renew", ""))
    return lines


def format_text(items=None):
    """纯文本报告（CLI / 复制用）。"""
    items = items if items is not None else entries()
    lines = ["账号凭据寿命与当前状态", "=" * 46]
    for p in PLATFORMS:
        s = LIFESPANS[p]
        cap = ("（捕获方式：%s）" % s["capture"]) if s.get("capture") else ""
        lines.append("· %s：%s%s" % (s["label"], s["life_text"], cap))
        lines.append("    来源 %s" % s.get("where", ""))
        if s.get("why"):
            lines.append("    为什么：%s" % s["why"])
        lines.append("    续期：%s" % s.get("renew", ""))
    lines.append("")
    lines.append(CAPTURE_SUMMARY)
    lines.append("")
    if not items:
        lines.append("（本机暂无可识别的凭据）")
    else:
        lines.append("当前凭据：")
        for e in items:
            flag = ""
            if e.get("expired"):
                flag = "  ⚠已过期"
            elif e.get("soon"):
                flag = "  ⚠即将过期"
            elif e.get("platform") == "traework" and not e.get("logged_in", True):
                flag = "  ⚠未登录"
            left = e["left_text"] if e.get("exp") else "—"
            lines.append("  [%s] %s：剩余 %s%s（%s）"
                         % (e["platform"], e["account"], left, flag, e["source"]))
    lines.append("")
    lines.append("注：每次签到执行时都会自动重新捕获一次，无需手动操作。")
    return "\n".join(lines)
