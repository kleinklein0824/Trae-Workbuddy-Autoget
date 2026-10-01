#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号凭据寿命 / 自动重新捕获 回归测试（``app/services/credential_refresh.py``）。

背景
----
用户问：「令牌有效期是多久？多久重新捕获账户一次？能不能调整自定义设置？」
三个平台的凭据寿命差两个数量级（TraeCN 8 小时 / TraeWork 会话 5 天 /
WorkBuddy 60 天），用户没法凭直觉判断，所以要把寿命和剩余时间摆到界面上，
并提供一个可配置的后台刷新间隔。

★ 本测试**全程沙箱**：
  · ``C.CONFIG_PATH`` / ``C.LOG_DIR`` / ``C.WB_TOKENS`` 改道到临时目录；
  · ``wb_checkin.TOKENS_FILE`` / ``wb_checkin.AUTH_CANDIDATES`` 也改道；
  · 末尾对**生产文件做指纹比对**（(st_mtime_ns, st_size)），只比对不删除。
  —— 本项目已有先例：测试往真实令牌缓存写了一条假 uid，导致幂等门控被击穿。

覆盖的**真实缺陷**（都是本次改动时发现的）：
  1. `import tc_checkin` 依赖 `app.config` 先把项目根挂上 `sys.path`；
     本模块若先 import 它，会 `ModuleNotFoundError` 而被 `except: pass` 吞掉
     → 界面静默显示「本机没有凭据」。→ 第 1 节用**子进程**复现并锁定。
  2. `wb_checkin.capture_current()` 原先**无条件覆盖**已保存令牌，而
     `_read_current_auth()` 不校验 `exp` —— 桌面端登录态一过期，
     一次（现在是**自动**的）刷新就会把好令牌换成过期令牌。→ 第 5 节。
  3. TraeWork 的到期时间可以直接从 Chromium 的 Cookies 库读出来，
     但路径随版本变过（Network/ 下），且库可能被浏览器占用。→ 第 4 节。
  4. `_traework_expiry()` 原先取「**所有** Cookie 里最早到期的那个」，
     实测真实 profile 里最早的是 `_uetsid`（Bing 统计 Cookie，**0.8 天**）——
     界面于是天天报「TraeWork 即将过期」，而真凭据（X-Cloudide-Session）
     还有 5 天。→ 第 6c/6d 节改成「只认鉴权 Cookie」并有负对照。
  5. 文案：TraeWork 原写作「（网页会话）/ 浏览器会话 Cookie」，而 `renew`
     又写「打开一次**桌面端**并登录即可续期」—— 自相矛盾，且实测桌面端
     （TRAE SOLO CN）日志令牌全脱敏、磁盘也无明文令牌，根本续不了期。
     → 第 1 节文案回归 + `LIFESPANS` 新增 `capture` / `why`。
  6. 间隔的**静默不一致**：`settings._save_cred` 允许 0~168，而 `cfg_section`
     要求「必须正好命中 `INTERVAL_CHOICES`」→ 填 48 会「保存成功」，
     读回来却被改成默认 6，而输入框仍显示 48。→ 第 3 节改成范围校验。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
            else os.path.dirname(os.path.dirname(GUI_DIR)))
for _p in (PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import config as C                              # noqa: E402
from app.services import credential_refresh as CR        # noqa: E402
import wb_checkin as WB                                  # noqa: E402

OK = FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {extra}")


# ============================================================ 沙箱
TMP = tempfile.mkdtemp(prefix="cred_test_")
_REAL = {
    "CONFIG_PATH": C.CONFIG_PATH,
    "LOG_DIR": C.LOG_DIR,
    "WB_TOKENS": C.WB_TOKENS,
    "WB_TOKENS_FILE": WB.TOKENS_FILE,
    "WB_AUTH": list(WB.AUTH_CANDIDATES or []),
}


def _fingerprint(paths):
    out = {}
    for p in paths:
        try:
            st = os.stat(p)
            out[p] = (st.st_mtime_ns, st.st_size)
        except OSError:
            out[p] = None
    return out


_PROD_FILES = [_REAL["CONFIG_PATH"], _REAL["WB_TOKENS"],
               os.path.join(_REAL["LOG_DIR"], "tokens_traecn.json"),
               os.path.join(_REAL["LOG_DIR"], "state.json"),
               os.path.join(_REAL["LOG_DIR"], "credential_refresh.json")]
_PROD_BEFORE = _fingerprint(_PROD_FILES)

C.CONFIG_PATH = os.path.join(TMP, "config.json")
C.LOG_DIR = os.path.join(TMP, "logs")
C.WB_TOKENS = os.path.join(TMP, "tokens_workbuddy.json")
os.makedirs(C.LOG_DIR, exist_ok=True)
WB.TOKENS_FILE = C.WB_TOKENS
_FAKE_AUTH = os.path.join(TMP, "workbuddy-desktop.info")
WB.AUTH_CANDIDATES = [_FAKE_AUTH]


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _write_cfg(section):
    _write_json(C.CONFIG_PATH, {"credentials": section} if section is not None else {})


def _stamp_file():
    return os.path.join(C.LOG_DIR, "credential_refresh.json")


def _write_stamp(ts):
    _write_json(_stamp_file(), {"last_refresh": ts})


def _read_stamp():
    try:
        with open(_stamp_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ============================================================ 1. 寿命表
print("== 1. 三平台寿命表 ==")
check("三个平台都在寿命表里",
      set(CR.PLATFORMS) == set(CR.LIFESPANS.keys()), list(CR.LIFESPANS))
check("TraeCN 寿命 = 8 小时（实测值）",
      abs(CR.LIFESPANS["traecn"]["life_hours"] - 8.0) < 0.01,
      CR.LIFESPANS["traecn"]["life_hours"])
check("TraeWork 寿命 = 5 天（会话 Cookie）",
      abs(CR.LIFESPANS["traework"]["life_hours"] - 5 * 24) < 0.01,
      CR.LIFESPANS["traework"]["life_hours"])
check("WorkBuddy 寿命 = 60 天（实测值）",
      abs(CR.LIFESPANS["workbuddy"]["life_hours"] - 60 * 24) < 0.01,
      CR.LIFESPANS["workbuddy"]["life_hours"])
check("每个平台都写明了续期方式",
      all(CR.LIFESPANS[p].get("renew") for p in CR.PLATFORMS))
check("每个平台都写明了捕获方式",
      all(CR.LIFESPANS[p].get("capture") for p in CR.PLATFORMS),
      {p: CR.LIFESPANS[p].get("capture") for p in CR.PLATFORMS})
check("每个平台都写明了「为什么是这个来源」",
      all(CR.LIFESPANS[p].get("why") for p in CR.PLATFORMS))

# ---- ★ 文案回归：用户问过「为什么只有 TraeWork CN 是网页会话捕获」 ----
print("  -- 文案回归（用户原始疑问）--")
check("TraeWork 标签改成「网页签到」，不再是「网页会话」",
      "网页签到" in CR.LIFESPANS["traework"]["label"]
      and "网页会话" not in CR.LIFESPANS["traework"]["label"],
      CR.LIFESPANS["traework"]["label"])
check("TraeCN 与 WorkBuddy 的捕获方式都是「桌面端」",
      CR.LIFESPANS["traecn"]["capture"] == "桌面端"
      and CR.LIFESPANS["workbuddy"]["capture"] == "桌面端",
      (CR.LIFESPANS["traecn"]["capture"], CR.LIFESPANS["workbuddy"]["capture"]))
check("TraeWork 的捕获方式明确不是「桌面端」",
      CR.LIFESPANS["traework"]["capture"] != "桌面端",
      CR.LIFESPANS["traework"]["capture"])
check("TraeWork 的「为什么」写明桌面端令牌脱敏",
      "脱敏" in CR.LIFESPANS["traework"]["why"],
      CR.LIFESPANS["traework"]["why"])
check("三个平台的来源都写明是**本地**读取（不需要用户去开网页）",
      all(("本地" in CR.LIFESPANS[p]["where"]) for p in CR.PLATFORMS),
      {p: CR.LIFESPANS[p]["where"] for p in CR.PLATFORMS})
check("【负对照】TraeWork 的续期方式不再谎称「打开桌面端」",
      "桌面端" not in CR.LIFESPANS["traework"]["renew"],
      CR.LIFESPANS["traework"]["renew"])
check("总览文案同时提到「桌面端」与 TraeWork",
      "桌面端" in CR.CAPTURE_SUMMARY and "TraeWork" in CR.CAPTURE_SUMMARY,
      CR.CAPTURE_SUMMARY)
check("总览文案说明这一步是自动的（不需要开网页）",
      "自动" in CR.CAPTURE_SUMMARY, CR.CAPTURE_SUMMARY)

_rows = CR.lifespan_rows()
check("lifespan_rows() 返回 3 条", len(_rows) == 3, len(_rows))
check("lifespan_rows() 每条都带 capture / where / why",
      all({"capture", "where", "why"} <= set(r) for r in _rows), _rows)
check("lifespan_rows() 与 LIFESPANS 一一对应",
      [r["key"] for r in _rows] == list(CR.PLATFORMS),
      [r["key"] for r in _rows])

_lines = CR.lifespan_lines()
# 每个平台 4 行：标题行 + 来源 + 为什么 + 续期
check("lifespan_lines() 每个平台 4 行（共 12 行）", len(_lines) == 12, len(_lines))
check("说明里带上了寿命文本",
      all(CR.LIFESPANS[p]["life_text"] in "".join(_lines)
          for p in CR.PLATFORMS))
check("说明里带上了捕获方式",
      all(CR.LIFESPANS[p]["capture"] in "".join(_lines)
          for p in CR.PLATFORMS))
check("说明里带上了「为什么」",
      all(CR.LIFESPANS[p]["why"] in "".join(_lines) for p in CR.PLATFORMS))
check("【负对照】不存在的平台不在寿命表里", "nope" not in CR.LIFESPANS)

# ============================================================ 2. _dur_text
print()
print("== 2. 时长文案 _dur_text ==")
check("负数 → 已过期", CR._dur_text(-5) == "已过期", CR._dur_text(-5))
check("0 → 已过期", CR._dur_text(0) == "已过期", CR._dur_text(0))
check("90 秒 → 分钟", CR._dur_text(90).endswith("分钟"), CR._dur_text(90))
check("3600*2 → 小时", CR._dur_text(7200).endswith("小时"), CR._dur_text(7200))
check("86400*3 → 天", CR._dur_text(86400 * 3).endswith("天"), CR._dur_text(86400 * 3))
check("非数字 → 未知（不抛异常）", CR._dur_text("abc") == "未知", CR._dur_text("abc"))
# 负对照：小时档绝不能被说成「天」
check("【负对照】7200 秒不报成天", not CR._dur_text(7200).endswith("天"),
      CR._dur_text(7200))
check("【负对照】3 天不报成小时", not CR._dur_text(86400 * 3).endswith("小时"),
      CR._dur_text(86400 * 3))

# ============================================================ 3. 配置容错
print()
print("== 3. cfg_section 容错 ==")
_write_cfg(None)
_s = CR.cfg_section()
check("缺 credentials 段 → 默认开启", _s["auto_refresh"] is True, _s)
check("缺 credentials 段 → 默认 6 小时",
      _s["refresh_interval_hours"] == CR.DEFAULT_INTERVAL_HOURS, _s)

_write_cfg({"auto_refresh": False, "refresh_interval_hours": 12})
_s = CR.cfg_section()
check("读到 auto_refresh=False", _s["auto_refresh"] is False, _s)
check("读到 12 小时", _s["refresh_interval_hours"] == 12, _s)

_write_cfg({"refresh_interval_hours": "abc"})
check("间隔是字符串 → 退回默认",
      CR.cfg_section()["refresh_interval_hours"] == CR.DEFAULT_INTERVAL_HOURS)
_write_cfg({"refresh_interval_hours": 999})
check("间隔超出允许范围 → 退回默认",
      CR.cfg_section()["refresh_interval_hours"] == CR.DEFAULT_INTERVAL_HOURS)
_write_cfg({"refresh_interval_hours": 0})
check("间隔 0 是合法值（表示关闭）",
      CR.cfg_section()["refresh_interval_hours"] == 0)

# ★ 回归（本次修掉的静默不一致）：保存侧允许 0~168，读侧却要求「必须命中档位」，
#   于是填 48 会「保存成功」但读回来变成 6，界面还显示 48。
print("  -- 间隔范围与保存侧一致（修掉的静默不一致）--")
_write_cfg({"refresh_interval_hours": 48})
check("★ 非档位但在允许范围内（48 小时）→ 原样保留，不被静默改成 6",
      CR.cfg_section()["refresh_interval_hours"] == 48,
      CR.cfg_section()["refresh_interval_hours"])
_write_cfg({"refresh_interval_hours": CR.MAX_INTERVAL_HOURS})
check("边界值 %d 小时允许" % CR.MAX_INTERVAL_HOURS,
      CR.cfg_section()["refresh_interval_hours"] == CR.MAX_INTERVAL_HOURS)
_write_cfg({"refresh_interval_hours": CR.MIN_INTERVAL_HOURS - 1})
check("低于下界 → 退回默认",
      CR.cfg_section()["refresh_interval_hours"] == CR.DEFAULT_INTERVAL_HOURS)
check("每个快捷档位都落在允许范围内",
      all(CR.MIN_INTERVAL_HOURS <= h <= CR.MAX_INTERVAL_HOURS
          for h in CR.QUICK_CHOICES), CR.QUICK_CHOICES)
check("默认值落在允许范围内",
      CR.MIN_INTERVAL_HOURS <= CR.DEFAULT_INTERVAL_HOURS <= CR.MAX_INTERVAL_HOURS)
check("【负对照】48 小时不在快捷档位里（所以上面那条测的是范围而非档位）",
      48 not in CR.INTERVAL_CHOICES, CR.INTERVAL_CHOICES)

# ============================================================ 4. due() 判据
print()
print("== 4. due() —— 判据是「距上次刷新」，不是「距进程启动」==")
# ★ 这一节的正/负对照是本测试的重点：
#   如果实现写成「进程起来就刷」，下面「刚刷过 → False」会失败；
#   如果实现写成恒 False，下面「很久没刷 → True」会失败。
_now = time.time()
# ★ 本节**刻意关掉源变化触发**（`refresh_on_source_change: False`）：
#   这里要单独测「周期判据」，而源变化判据（含旧版记账文件的一次性补基准迁移，
#   见第 11 节）会在别的分支上先返回 True，把这一节的判据盖住。
_write_cfg({"auto_refresh": True, "refresh_interval_hours": 6,
            "refresh_on_source_change": False})

try:
    os.remove(_stamp_file())
except OSError:
    pass
check("从未刷过 → 该刷（True）", CR.due() is True)
check("last_refresh() 无记录 → None", CR.last_refresh() is None)

_write_stamp(_now)
check("【负对照】刚刷过 → 不该刷（False）", CR.due(now=_now) is False)

_write_stamp(_now - 7 * 3600)
check("超过 6 小时间隔 → 该刷（True）", CR.due(now=_now) is True)

_write_stamp(_now - 5 * 3600)
check("【正对照】未到 6 小时 → 不该刷（False）", CR.due(now=_now) is False)

_write_cfg({"auto_refresh": False, "refresh_interval_hours": 6,
            "refresh_on_source_change": False})
_write_stamp(_now - 30 * 3600)
check("开关关闭 → 再久也不刷", CR.due(now=_now) is False)

_write_cfg({"auto_refresh": True, "refresh_interval_hours": 0,
            "refresh_on_source_change": False})
check("间隔 0 → 不刷", CR.due(now=_now) is False)

_write_cfg({"auto_refresh": True, "refresh_interval_hours": 6,
            "refresh_on_source_change": False})
_write_stamp(_now - 7 * 3600)
check("_mark_refreshed() 之后立刻不该刷",
      (CR._mark_refreshed(_now), CR.due(now=_now))[1] is False)

print()
print("  -- next_due_text --")
_write_cfg({"auto_refresh": False, "refresh_interval_hours": 6})
check("关闭时说明里写明「已关闭」",
      "已关闭" in CR.next_due_text(), CR.next_due_text())
_write_cfg({"auto_refresh": True, "refresh_interval_hours": 6})
_write_stamp(_now - 7 * 3600)
check("到点后说明里写「已到刷新时间」",
      "已到刷新时间" in CR.next_due_text(now=_now), CR.next_due_text(now=_now))
_write_stamp(_now)
check("未到点写「约 N 后自动刷新」",
      "后自动刷新" in CR.next_due_text(now=_now), CR.next_due_text(now=_now))
try:
    os.remove(_stamp_file())
except OSError:
    pass
check("从未刷过时说明里写「启动后会立即刷新」",
      "立即刷新" in CR.next_due_text(), CR.next_due_text())

# ============================================================ 5. _ensure_root
print()
print("== 5. _ensure_root()：先挂 sys.path 再 import tc_checkin ==")
# ★ 复现真实缺陷：在**只** import 本模块（不 import app.config）的子进程里
#   调 entries()。若不先挂 sys.path，tc_checkin 会 ModuleNotFoundError，
#   被 `except: pass` 吞掉 → 静默少一个平台的凭据。
_CODE = (
    "import sys, json;"
    "sys.path.insert(0, %r);" % GUI_DIR +
    "sys.path.insert(0, %r);" % PROJ_ROOT +
    "from app.services import credential_refresh as CR;"
    "print(json.dumps({'root': CR._ensure_root(),"
    " 'has_traecn': 'tc_checkin' in sys.modules or CR._ensure_root()}))"
)
_r = subprocess.run([sys.executable, "-c", _CODE], capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=90,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
check("子进程里 _ensure_root() 返回 True",
      '"root": true' in (_r.stdout or ""), (_r.stdout or "") + (_r.stderr or "")[:400])
check("子进程里能 import 到 tc_checkin（sys.path 已挂好）",
      _r.returncode == 0 and '"has_traecn": true' in (_r.stdout or ""),
      f"rc={_r.returncode} out={_r.stdout!r} err={(_r.stderr or '')[:300]}")
check("【负对照】不挂 sys.path 时 import tc_checkin 会失败（证明这条测得到）",
      subprocess.run([sys.executable, "-c",
                      "import sys; sys.path=[p for p in sys.path "
                      "if 'Auto' not in p]; import tc_checkin"],
                     capture_output=True, timeout=60).returncode != 0)

# ---- ★ 2026-09-21 追加：`source_fingerprint()` 走的是**同一批顶层 import** ----
#   `_tc_log_fingerprint()` / `_wb_auth_fingerprint()` 里都是裸 `import tc_checkin`
#   / `import wb_checkin`，而这两个模块在**项目根**上。`source_fingerprint()`
#   原先**没有** `_ensure_root()`，于是「项目根还没挂上 sys.path」时会
#   **静默返回空指纹**（实测只剩 1 项 —— 只有 TraeWork 的 Cookie，因为它走
#   `from .. import config` 而不是顶层 import）。
#   空指纹的后果**不是报错**，而是 `sources_changed()` 永远判 False →
#   「重新打开软件就自动刷新」这条功能**无声失效**。比崩溃更难查。
#   判据用 `sys.modules`（确定性，与这台机器装没装 TraeCN 无关）。
_GUARD_CODE = (
    "import sys, os, json;"
    "PROJ = %r; GUI = %r;" % (PROJ_ROOT, GUI_DIR) +
    # 只摘掉**项目根**，保留 GUI_DIR（要能 import app.services）与标准库路径
    "sys.path[:] = [p for p in sys.path "
    "               if os.path.abspath(p or '.') != os.path.abspath(PROJ)];"
    "GUI in sys.path or sys.path.insert(0, GUI);"
    "from app.services import credential_refresh as CR;"
    "fp = CR.source_fingerprint();"
    "print(json.dumps({'tc': 'tc_checkin' in sys.modules,"
    "                  'wb': 'wb_checkin' in sys.modules,"
    "                  'n': len(fp)}))"
)
_r2 = subprocess.run([sys.executable, "-c", _GUARD_CODE], cwd=TMP,
                     capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=120,
                     env={**os.environ, "PYTHONIOENCODING": "utf-8"})
check("★ source_fingerprint() 自带 _ensure_root()：项目根不在 sys.path 时也能取到 tc/wb",
      _r2.returncode == 0 and '"tc": true' in (_r2.stdout or "")
      and '"wb": true' in (_r2.stdout or ""),
      f"rc={_r2.returncode} out={(_r2.stdout or '')!r} err={(_r2.stderr or '')[:300]}")

# 【正对照】同样的环境，直接调**没有护栏**的 `_tc_log_fingerprint()` ——
#   必须静默返回空、且模块没进 sys.modules（否则上面那条断言测不到东西）。
_CTRL_CODE = (
    "import sys, os, json;"
    "PROJ = %r; GUI = %r;" % (PROJ_ROOT, GUI_DIR) +
    "sys.path[:] = [p for p in sys.path "
    "               if os.path.abspath(p or '.') != os.path.abspath(PROJ)];"
    "GUI in sys.path or sys.path.insert(0, GUI);"
    "from app.services import credential_refresh as CR;"
    "raw = CR._tc_log_fingerprint();"
    "print(json.dumps({'tc': 'tc_checkin' in sys.modules, 'n': len(raw)}))"
)
_r3 = subprocess.run([sys.executable, "-c", _CTRL_CODE], cwd=TMP,
                     capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=120,
                     env={**os.environ, "PYTHONIOENCODING": "utf-8"})
check("【正对照】没有护栏的采集函数在该环境下确实静默返回空（证明上面那条不是恒真）",
      _r3.returncode == 0 and '"tc": false' in (_r3.stdout or "")
      and '"n": 0' in (_r3.stdout or ""),
      f"rc={_r3.returncode} out={(_r3.stdout or '')!r} err={(_r3.stderr or '')[:300]}")

# 源码级兜底：函数体里必须出现 _ensure_root()（防止有人「重构掉」这一行）
import inspect  # noqa: E402
_src_fp = inspect.getsource(CR.source_fingerprint)
check("源码级：source_fingerprint() 函数体里确实调用了 _ensure_root()",
      "_ensure_root()" in _src_fp, _src_fp[:160])

# ============================================================ 6. TraeWork 到期
print()
print("== 6. _traework_expiry()：直接从 Cookies 库读到期时间 ==")
_PROF = os.path.join(TMP, "prof", "trae1")
_CK = os.path.join(_PROF, "Default", "Network", "Cookies")
os.makedirs(os.path.dirname(_CK), exist_ok=True)
_CHROME_EPOCH = 11644473600


def _mk_cookies(path, rows):
    try:
        os.remove(path)
    except OSError:
        pass
    con = sqlite3.connect(path)
    con.execute("create table cookies (host_key text, name text, "
                "expires_utc integer)")
    con.executemany("insert into cookies values (?,?,?)", rows)
    con.commit()
    con.close()


def _to_chrome(ts):
    return int((ts + _CHROME_EPOCH) * 1000000)


_now2 = time.time()

# 6a. 会话 Cookie 优先
_mk_cookies(_CK, [(".trae.cn", "d_ticket", _to_chrome(_now2 + 300 * 86400)),
                  (".trae.cn", "X-Cloudide-Session",
                   _to_chrome(_now2 + 5 * 86400))])
_exp = CR._traework_expiry(_PROF)
check("读到 X-Cloudide-Session 的到期时间（优先）",
      abs(_exp - (_now2 + 5 * 86400)) < 120, _exp)

# 6b. 没有会话 Cookie → 取**鉴权 Cookie 里**最早的那个
_mk_cookies(_CK, [(".trae.cn", "d_ticket", _to_chrome(_now2 + 300 * 86400)),
                  (".trae.cn", "sessionid", _to_chrome(_now2 + 51 * 86400))])
_exp = CR._traework_expiry(_PROF)
check("没有会话 Cookie 时取最早的**鉴权** Cookie（51 天而非 300 天）",
      abs(_exp - (_now2 + 51 * 86400)) < 120, _exp)

# 6c. ★ 回归（本次修掉的真 bug）：统计 Cookie 不得盖过会话 Cookie。
#     实测真实 profile 里 _uetsid 只剩 0.8 天，旧实现会把它当凭据 →
#     界面天天报「TraeWork 即将过期」，而真凭据还有 5 天。
_mk_cookies(_CK, [(".trae.cn", "_uetsid", _to_chrome(_now2 + 0.8 * 86400)),
                  (".trae.cn", "_clsk", _to_chrome(_now2 + 0.8 * 86400)),
                  ("www.trae.cn", "gfkadpd", _to_chrome(_now2 + 0.8 * 86400)),
                  (".trae.cn", "n_mh", _to_chrome(_now2 + 111 * 86400)),
                  (".trae.cn", "X-Cloudide-Session",
                   _to_chrome(_now2 + 5 * 86400))])
_exp = CR._traework_expiry(_PROF)
check("★ 统计 Cookie（_uetsid 0.8 天）不会盖过会话 Cookie（5 天）",
      abs(_exp - (_now2 + 5 * 86400)) < 120, _exp)
check("★ 返回值确实不是统计 Cookie 的 0.8 天",
      abs(_exp - (_now2 + 0.8 * 86400)) > 3600, _exp)

# 6d. 【负对照】只有统计 Cookie → 必须返回 0（绝不猜）
_mk_cookies(_CK, [(".trae.cn", "_uetsid", _to_chrome(_now2 + 0.8 * 86400)),
                  (".trae.cn", "_clsk", _to_chrome(_now2 + 0.8 * 86400)),
                  (".bing.com", "MUID", _to_chrome(_now2 + 388 * 86400))])
_exp = CR._traework_expiry(_PROF)
check("【负对照】只有统计 Cookie → 返回 0（不拿 0.8 天当凭据）",
      _exp == 0, _exp)

# 6e. 第三方统计域名上的同名 Cookie 也要排除
_mk_cookies(_CK, [("www.bing.com", "sessionid", _to_chrome(_now2 + 1 * 86400)),
                  (".trae.cn", "d_ticket", _to_chrome(_now2 + 300 * 86400))])
_exp = CR._traework_expiry(_PROF)
check("第三方域名上的同名 sessionid 被排除（取 trae.cn 的 d_ticket）",
      abs(_exp - (_now2 + 300 * 86400)) < 120, _exp)

# 6f. 明细接口要带出「读到的是哪条 Cookie」（界面显示来源用）
_mk_cookies(_CK, [(".trae.cn", "X-Cloudide-Session",
                   _to_chrome(_now2 + 5 * 86400))])
_det = CR._traework_expiry_detail(_PROF)
check("_traework_expiry_detail 返回 (时间, Cookie 名)",
      isinstance(_det, tuple) and len(_det) == 2
      and _det[1] == "X-Cloudide-Session", _det)
check("_traework_expiry_detail 读不到时返回 (0, '')",
      CR._traework_expiry_detail(None) == (0, ""),
      CR._traework_expiry_detail(None))

# 6g. 边界
_mk_cookies(_CK, [(".trae.cn", "dead", _to_chrome(_now2 - 86400))])
check("全是过期 Cookie → 返回 0（不返回过去的时间）",
      CR._traework_expiry(_PROF) == 0, CR._traework_expiry(_PROF))
check("会话目录不存在 → 0", CR._traework_expiry(os.path.join(TMP, "nope")) == 0)
check("profile 为空 → 0", CR._traework_expiry(None) == 0)
os.remove(_CK)
check("Cookies 库不存在 → 0（不抛异常）",
      CR._traework_expiry(_PROF) == 0)
# 负对照：随便返回 0 的实现在上面「优先会话 Cookie」那条必然失败
check("【负对照】上面的优先判定确实读到过非 0 值",
      _exp == 0 or _exp > _now2)

# ============================================================ 7. WorkBuddy 护栏
print()
print("== 7. capture_current() 的过期护栏（本次新增）==")


def _auth(uid, exp_ms, nick="Tester", token="tok"):
    return {"account": {"uid": uid, "nickname": nick, "phoneNumber": "1"},
            "auth": {"accessToken": token, "domain": "d", "expiresAt": exp_ms}}


def _saved():
    try:
        with open(C.WB_TOKENS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_MS = 1000
_now_ms = time.time() * 1000

# 7a. 本机登录态已过期 → 护栏必须拦住，不能覆盖
_write_json(C.WB_TOKENS, {"u1": {"nickname": "Old", "token": "GOOD",
                                 "expires_at": _now_ms + 50 * 86400 * _MS}})
_write_json(_FAKE_AUTH, _auth("u1", _now_ms - 86400 * _MS, token="EXPIRED"))
_ok = WB.capture_current(lambda m: None)
check("本机登录态过期 → 返回 False", _ok is False, _ok)
check("★ 已保存的好令牌**没有被覆盖**",
      _saved().get("u1", {}).get("token") == "GOOD", _saved().get("u1"))
check("到期时间也保持原样（未被缩短）",
      _saved().get("u1", {}).get("expires_at") == _now_ms + 50 * 86400 * _MS,
      _saved().get("u1"))

# 7b. 【正对照】本机登录态有效 → 必须能写进去
#     （否则上面「没被覆盖」可能只是因为「压根没写」）
_write_json(_FAKE_AUTH, _auth("u1", _now_ms + 10 * 86400 * _MS, token="FRESH"))
_ok = WB.capture_current(lambda m: None)
check("【正对照】有效登录态 → 返回 True", _ok is True, _ok)
check("【正对照】令牌已更新为 FRESH",
      _saved().get("u1", {}).get("token") == "FRESH", _saved().get("u1"))

# 7c. 新账号（uid 不在缓存里）→ 正常写入
_write_json(_FAKE_AUTH, _auth("u2", _now_ms + 10 * 86400 * _MS, nick="Second"))
WB.capture_current(lambda m: None)
check("新 uid 正常写入", "u2" in _saved(), list(_saved()))
check("写入后共 2 个账号", len(_saved()) == 2, list(_saved()))

# 7d. 本机没有登录态文件 → 不写、返回 False，且不清空已保存的
os.remove(_FAKE_AUTH)
_before_keys = set(_saved())
_ok = WB.capture_current(lambda m: None)
check("读不到本机登录态 → 返回 False", _ok is False, _ok)
check("读不到本机登录态时不清空已保存账号",
      set(_saved()) == _before_keys, _saved())

# ============================================================ 8. refresh()
print()
print("== 8. refresh() 全链路（沙箱）==")
os.remove(C.WB_TOKENS)
_write_json(_FAKE_AUTH, _auth("u1", _now_ms + 10 * 86400 * _MS, nick="Tester"))
_write_cfg({"auto_refresh": True, "refresh_interval_hours": 6})
try:
    os.remove(_stamp_file())
except OSError:
    pass
_ok, _detail, _stats = CR.refresh(log=lambda m: None)
check("返回 (bool, str, dict)", isinstance(_ok, bool)
      and isinstance(_detail, str) and isinstance(_stats, dict))
check("stats 含三个平台键",
      set(_stats) == set(CR.PLATFORMS), list(_stats))
check("每个平台都有 ok 字段",
      all("ok" in v for v in _stats.values()), _stats)
check("WorkBuddy 侧确实捕获到了 1 个账号",
      _stats["workbuddy"].get("accounts") == 1, _stats.get("workbuddy"))
check("detail 非空", bool(_detail), _detail)
check("刷新后写下了 last_refresh 时间戳",
      CR.last_refresh() is not None, CR.last_refresh())
check("刚刷完 → due() 为 False", CR.due() is False)

# 护栏被触发时：skipped=True 且整体仍算「看过了」（不该报成失败）
_write_json(C.WB_TOKENS, {"u9": {"nickname": "Keep", "token": "KEEP",
                                 "expires_at": _now_ms + 40 * 86400 * _MS}})
_write_json(_FAKE_AUTH, _auth("u9", _now_ms - 86400 * _MS, token="EXPIRED"))
_ok, _detail, _stats = CR.refresh(log=lambda m: None)
check("护栏触发时 workbuddy.skipped = True",
      _stats["workbuddy"].get("skipped") is True, _stats.get("workbuddy"))
check("护栏触发时整体仍算成功（不报成失败）", _ok is True, _ok)
check("护栏触发时保留原令牌",
      _saved().get("u9", {}).get("token") == "KEEP", _saved().get("u9"))

# ============================================================ 9. 展示
print()
print("== 9. entries() / format_text() ==")
_items = CR.entries()
check("entries() 返回 list", isinstance(_items, list), type(_items))
check("每条都带 left_text / soon / expired",
      all({"left_text", "soon", "expired"} <= set(e) for e in _items), _items)
check("TraeWork 账号被列出（沙箱里 config 为空 → 允许 0 条）",
      all(e.get("platform") in CR.PLATFORMS for e in _items), _items)
_txt = CR.format_text(_items)
check("format_text 含三平台名",
      all(CR.LIFESPANS[p]["label"] in _txt for p in CR.PLATFORMS))
check("format_text 写明「每次签到会自动重新捕获」",
      "自动重新捕获" in _txt)
check("format_text 非空且是多行", len(_txt.splitlines()) > 5)
check("format_text 带上了捕获方式总览",
      CR.CAPTURE_SUMMARY in _txt, _txt[:200])
check("【负对照】format_text 里不再出现旧措辞「浏览器会话 Cookie」",
      "浏览器会话 Cookie" not in _txt)
check("format_text 里 TraeWork 那条写的是「本地会话 Cookie」",
      "本地会话 Cookie" in _txt or "本地会话目录" in _txt, _txt)
_soon = CR.soonest(_items)
check("soonest() 在没有带 exp 的凭据时返回 None 或 dict",
      _soon is None or isinstance(_soon, dict), _soon)
# 正对照：造两条带 exp 的，必须挑剩余更短的那条
_fake = [{"exp": _now_ms / 1000 + 100, "left": 100, "platform": "traecn"},
         {"exp": _now_ms / 1000 + 5, "left": 5, "platform": "workbuddy"}]
check("【正对照】soonest 挑剩余最短的",
      CR.soonest(_fake).get("platform") == "workbuddy", CR.soonest(_fake))

# ============================================================ 11. 源变化触发
print()
print("== 11. 源变化触发（「重新打开对应软件就自动拿到新令牌」）==")
# 用户原话：「令牌更新、期限板块有问题…确保重新打开对应软件自动获取令牌更新」。
# 原设计的判据**只有**间隔 —— 间隔设成 24 小时时，中途关掉 IDE 再打开
# （日志里已经写了一枚全新的 8 小时令牌），界面最长要等 24 小时才更新。
_ORIG_FP = CR.source_fingerprint
_FAKE_FP = {"tc:Trae CN": 111}


def _set_fp(v):
    CR.source_fingerprint = lambda: dict(v)


try:
    # ---- _as_bool：配置是手编的 JSON，'false' 必须读成 False ----
    check("_as_bool('false') 是 False", CR._as_bool("false") is False,
          CR._as_bool("false"))
    check("_as_bool('true') 是 True", CR._as_bool("true") is True)
    check("_as_bool(None) 取默认值", CR._as_bool(None, True) is True)
    check("【负对照】bool('false') 是 True —— 所以不能直接用 bool()",
          bool("false") is True)

    # ---- cfg_section 读新键（含字符串容错）----
    _write_cfg(None)
    check("缺 credentials 段 → 源变化触发默认开启",
          CR.cfg_section()["refresh_on_source_change"] is True,
          CR.cfg_section())
    _write_cfg({"refresh_on_source_change": False})
    check("读到 False", CR.cfg_section()["refresh_on_source_change"] is False)
    _write_cfg({"refresh_on_source_change": "false"})
    check("★ 字符串 'false' 也读成 False（不然「关掉」会变成「打开」）",
          CR.cfg_section()["refresh_on_source_change"] is False)

    # ---- ★★ 旧版记账文件（有刷新时间、没指纹）→ 一次性补基准 + 刷一次 ----
    #   这是本轮**最后一个真实缺口**：生产文件实测就是
    #   `{"last_refresh": ..., "at": "..."}`（没有 fingerprint）。
    #   不补基准的话，「间隔设 0 + 只靠源变化」这个组合会：
    #     ① 源变化 → 没基准 → False；② 周期 → hours<=0 → False
    #   ⇒ `due()` **永远是 False**，功能彻底失效且零报错。
    _set_fp(_FAKE_FP)
    _write_cfg({"auto_refresh": True, "refresh_interval_hours": 0,
                "refresh_on_source_change": True})
    _write_stamp(_now)            # 旧版记账文件：只有 last_refresh，没有指纹
    check("★ 旧版记账文件缺基准 → 间隔 0 也必须刷一次（否则功能永远失效）",
          CR.due(now=_now) is True, CR.due(now=_now))
    _st = _read_stamp()
    check("★ 基准是「先落盘再返回 True」（不依赖 refresh 去写）",
          _st.get("fingerprint") == _FAKE_FP, _st.get("fingerprint"))
    check("★ 补基准不改动 last_refresh",
          abs(float(_st.get("last_refresh") or 0) - _now) < 1e-6,
          _st.get("last_refresh"))
    check("★ 收敛：紧接着再问一次就不再刷（不会每 60 秒刷一次）",
          CR.due(now=_now) is False, CR.due(now=_now))
    check("【正对照】补完基准后，源再变（越过冷却）照样触发",
          (_set_fp({"tc:Trae CN": 222}),
           CR.due(now=_now + CR.SOURCE_WATCH_MIN_GAP_S + 1))[1] is True)
    check("【负对照】没有 last_refresh（从未刷过）时**不**走迁移分支",
          (_write_json(_stamp_file(), {}), CR._adopt_baseline_if_missing())[1]
          is False)
    check("【负对照】没有基准时 sources_changed() 返回 False（没有参照就不猜）",
          (_write_stamp(_now), CR.sources_changed())[1] is False)
    check("【负对照】已有基准时 _adopt_baseline_if_missing() 返回 False",
          (_write_json(_stamp_file(), {"last_refresh": _now,
                                       "fingerprint": dict(_FAKE_FP)}),
           CR._adopt_baseline_if_missing())[1] is False)
    check("【负对照】一个源文件都读不到时**不**补基准（绝不猜）",
          (_set_fp({}), _write_stamp(_now),
           CR._adopt_baseline_if_missing())[2] is False)
    # 写不动时必须返回 False —— 否则会退化成「每拍都刷」的死循环
    # （用打桩而不是造「不可写目录」：Windows 上造 ACL 麻烦，而这里要测的
    #   就是「_write_stamp 返回 False 时的分支」，打桩最直接、最确定）
    _set_fp(_FAKE_FP)
    _orig_read, _orig_write = CR._read_stamp, CR._write_stamp
    CR._read_stamp = lambda: {"last_refresh": _now}     # 读得到、但没指纹
    CR._write_stamp = lambda d: False                   # 写不进去
    try:
        check("★ 记账文件写不动时 _adopt_baseline_if_missing() 必须返回 False"
              "（否则每 60 秒刷一次）",
              CR._adopt_baseline_if_missing() is False,
              CR._adopt_baseline_if_missing())
    finally:
        CR._read_stamp, CR._write_stamp = _orig_read, _orig_write
    check("【正对照】同一个输入、写能成功时就会返回 True（证明上面测的是写失败分支）",
          (_write_stamp(_now), CR._adopt_baseline_if_missing())[1] is True)

    # ---- 有基准 + 源没变 → 不触发 ----
    # 用「距上次刷新 700 秒」：刚好越过 SOURCE_WATCH_MIN_GAP_S(600) 冷却，
    # 但远没到 24 小时间隔 —— 这样测出来的 True 只可能来自源变化分支。
    _write_json(_stamp_file(), {"last_refresh": _now - 700,
                                "fingerprint": dict(_FAKE_FP)})
    check("源没变 → 不触发（间隔也没到）", CR.due(now=_now) is False,
          CR.due(now=_now))
    _set_fp({"tc:Trae CN": 222})
    check("★ 源变了 → 立刻该刷（间隔 24 小时、距上次才 700 秒）",
          CR.due(now=_now) is True)
    check("★ sources_changed() 直接判也是 True", CR.sources_changed() is True)

    # ---- 冷却：防「IDE 一直写日志 → 每分钟刷一次」----
    _write_json(_stamp_file(), {"last_refresh": _now,
                                "fingerprint": dict(_FAKE_FP)})
    check("★ 刚刷过 0 秒 → 源变了也先不刷（冷却 %d 秒）"
          % CR.SOURCE_WATCH_MIN_GAP_S, CR.due(now=_now) is False)
    check("【正对照】越过冷却后同一次源变化就会刷",
          CR.due(now=_now + CR.SOURCE_WATCH_MIN_GAP_S + 1) is True)

    # ---- 开关关掉 → 源变了也不刷 ----
    _write_cfg({"auto_refresh": True, "refresh_interval_hours": 24,
                "refresh_on_source_change": False})
    _write_json(_stamp_file(), {"last_refresh": _now - 700,
                                "fingerprint": dict(_FAKE_FP)})
    check("开关关闭 → 源变了也不刷", CR.due(now=_now) is False,
          CR.due(now=_now))
    check("开关关闭时 next_due_text 不再提「重新打开」",
          "重新打开" not in CR.next_due_text(now=_now),
          CR.next_due_text(now=_now))

    # ---- 开关打开 → 文案要告诉用户「重开软件会立刻刷」----
    _write_cfg({"auto_refresh": True, "refresh_interval_hours": 24,
                "refresh_on_source_change": True})
    check("开启时 next_due_text 写明「软件重新打开时会自动刷新」",
          "重新打开" in CR.next_due_text(now=_now), CR.next_due_text(now=_now))
    _write_cfg({"auto_refresh": True, "refresh_interval_hours": 0,
                "refresh_on_source_change": True})
    check("间隔 0 但开关开着 → 不能说「已关闭」（它还会按源变化刷）",
          "已关闭" not in CR.next_due_text(now=_now),
          CR.next_due_text(now=_now))

    # ---- _mark_refreshed() 要把指纹一起记下来 ----
    _set_fp({"tc:Trae CN": 333})
    CR._mark_refreshed(_now)
    check("★ _mark_refreshed() 记下了当时的指纹",
          (CR._read_stamp().get("fingerprint") or {}).get("tc:Trae CN") == 333,
          CR._read_stamp().get("fingerprint"))
    check("记完之后立刻判「源变了没」必须是 False",
          CR.sources_changed() is False)

    # ---- 真实指纹（用沙箱里的 auth 文件，不 stub）----
    CR.source_fingerprint = _ORIG_FP
    _write_json(_FAKE_AUTH, {"account": {"uid": "u1", "nickname": "Klein"},
                             "auth": {"accessToken": "a" * 40,
                                      "expiresAt": _now_ms + 1000}})
    _fp1 = CR.source_fingerprint()
    check("真实指纹里含 WorkBuddy auth 文件",
          any(k.startswith("wb:") for k in _fp1), sorted(_fp1))
    _write_json(_FAKE_AUTH, {"account": {"uid": "u1", "nickname": "Klein"},
                             "auth": {"accessToken": "b" * 80,
                                      "expiresAt": _now_ms + 2000}})
    _fp2 = CR.source_fingerprint()
    _k = "wb:workbuddy-desktop.info"
    check("★ 改动 auth 文件后指纹确实变了",
          _fp1.get(_k) != _fp2.get(_k), (_fp1.get(_k), _fp2.get(_k)))
    check("【负对照】没动过的源不会凭空出现在指纹里",
          not any(k.startswith("zz:") for k in _fp2), sorted(_fp2))

    # ---- WorkBuddy：**实时** auth 文件优先于落盘快照 ----
    # 原实现只读 _load_saved()，于是「重新登录一次 WorkBuddy」之后界面
    # 一直显示旧令牌的剩余时间，直到下一次捕获。
    _now_ms2 = int(time.time() * 1000)
    _60d = 60 * 86400 * 1000
    # 场景 1：快照只剩 1 秒，桌面端刚重新登录（实时 60 天）→ 必须采用实时
    _write_json(C.WB_TOKENS, {"u1": {"uid": "u1", "nickname": "Klein",
                                     "token": "old",
                                     "expires_at": _now_ms2 + 1000}})
    _write_json(_FAKE_AUTH, {"account": {"uid": "u1", "nickname": "Klein"},
                             "auth": {"accessToken": "new",
                                      "expiresAt": _now_ms2 + _60d}})
    _wb = [e for e in CR.entries() if e.get("platform") == "workbuddy"]
    check("实时 auth 比快照晚到期 → 采用实时值",
          bool(_wb) and "实时" in _wb[0]["source"], _wb)
    check("★ 剩余时间取的是实时的 60 天，不是快照的 1 秒",
          bool(_wb) and _wb[0]["left"] > 30 * 86400,
          _wb[0]["left"] if _wb else None)
    # 场景 2（正对照）：快照是 60 天，而实时登录态**已经过期** ——
    #   不能被这条已过期的实时记录把有效期带偏（与 capture_current 的护栏同一道理）。
    _write_json(C.WB_TOKENS, {"u1": {"uid": "u1", "nickname": "Klein",
                                     "token": "good",
                                     "expires_at": _now_ms2 + _60d}})
    _write_json(_FAKE_AUTH, {"account": {"uid": "u1", "nickname": "Klein"},
                             "auth": {"accessToken": "stale",
                                      "expiresAt": _now_ms2 - 1000}})
    _wb2 = [e for e in CR.entries() if e.get("platform") == "workbuddy"]
    check("【正对照】实时已过期 → 仍用快照的 60 天",
          bool(_wb2) and _wb2[0]["left"] > 30 * 86400,
          _wb2[0]["left"] if _wb2 else None)
    check("【正对照】此时来源标为「已保存快照」",
          bool(_wb2) and "快照" in _wb2[0]["source"], _wb2)
finally:
    CR.source_fingerprint = _ORIG_FP

# ============================================================ 10. 生产数据
print()
print("== 10. 生产数据未被污染（指纹比对）==")
C.CONFIG_PATH = _REAL["CONFIG_PATH"]
C.LOG_DIR = _REAL["LOG_DIR"]
C.WB_TOKENS = _REAL["WB_TOKENS"]
WB.TOKENS_FILE = _REAL["WB_TOKENS_FILE"]
WB.AUTH_CANDIDATES = _REAL["WB_AUTH"]
_PROD_AFTER = _fingerprint(_PROD_FILES)
for _p in _PROD_FILES:
    check(f"未改动 {os.path.basename(_p)}",
          _PROD_AFTER[_p] == _PROD_BEFORE[_p],
          f"\n       before={_PROD_BEFORE[_p]}\n       after ={_PROD_AFTER[_p]}")
check("测试确实改道到了沙箱（生产路径 ≠ 沙箱路径）",
      os.path.normcase(C.WB_TOKENS) != os.path.normcase(
          os.path.join(TMP, "tokens_workbuddy.json")))

shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"结果：{OK} OK / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
