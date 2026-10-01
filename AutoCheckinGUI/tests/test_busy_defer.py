#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归：`busy`（待复核）口径 —— 9074 限流不能报成「TraeWork 失败」。

背景（2026-09-19 真实可见事故）
------------------------------
当天自动化弹窗报 `[traework] Klein: fail`，标题「每日签到：1/4 成功，1 失败」，
用户以为白签了。但 2 分钟后的补跑轮复核到
`skip — 今日已签到（接口确认，+150积分）`：**积分其实早就到账了**。

两个叠加根因，本文件把它们固化成断言：

1. **`9074`（服务端「当前参与用户太多，请稍后再试」）被当成硬失败。**
   `tc_checkin.checkin_by_token` 的兜底 `return "fail"` 把它吸走。它是**限流**，
   服务端往往已受理 —— 报 fail 纯属误导。
   → 现在归成 `busy`：不计成功（补跑会继续），但文案是琥珀色「待复核」。

2. **弹窗归因顺序错了：`"登录"` 抢在 `"过期"/"令牌"` 前面。**
   `_TOKEN_REVOKED` 的建议文案里含「…登录即可自动换到新令牌」，
   于是**令牌吊销**被显示成「失败：需重新登录」，真因被遮蔽。
   → 现在先判令牌/过期，再判登录。

3. 顺带修掉一个**既存自相矛盾**：`_note()` 有 busy 分支，但弹窗 `rows` 的
   `state` 是二值化的（`fail if not in (ok, skip)`）→ busy 被画成**红灯**，
   而 `toast.html` 里根本没有 `.led.warn`（加了也白加）。
   → 现在 `state` 三态化 + `toast.html` 补 `.led.warn`。

★ 本测试**全程沙箱**：`checkin` 的 LOG_DIR / STATE_PATH / HISTORY_PATH /
  RECORD_PATH / RETRY_STATE_PATH 全部改道临时目录，`_spawn_toast` 与 `log`
  全部 stub，末尾再对生产文件做 `(mtime_ns, size)` 指纹比对。

★ 每节都带**正对照**：只断言"新逻辑对"是不够的 —— 还得断言"旧逻辑会错"，
  否则哪天有人改回去，测试照样绿。

跑法：python tests/test_busy_defer.py
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
if GUI_DIR not in sys.path:
    sys.path.insert(0, GUI_DIR)

import app.config as C  # noqa: E402  定位项目根并挂上 sys.path
import checkin  # noqa: E402
import tc_checkin as TC  # noqa: E402

ok = 0
fail = 0
skip = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {name}")
    else:
        fail += 1
        print(f"  FAIL {name}" + (f"  → {extra}" if extra else ""))


def section(t):
    print()
    print(f"== {t} ==")


# ---------------------------------------------------------------------------
# 0. 沙箱 + 生产文件指纹
# ---------------------------------------------------------------------------
print("== 0. 沙箱准备与生产文件指纹 ==")
REAL_LOG_DIR = C.LOG_DIR
PROD_FILES = [os.path.join(REAL_LOG_DIR, n) for n in (
    "state.json", "history.jsonl", "签到记录.csv", "retry_state.json",
    "tokens_traecn.json")]


def _fp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


_BEFORE = {p: _fp(p) for p in PROD_FILES}

TMP = tempfile.mkdtemp(prefix="tb_busy_test_")
_SANDBOX = {}
for _k, _v in (("LOG_DIR", TMP),
               ("STATE_PATH", os.path.join(TMP, "state.json")),
               ("HISTORY_PATH", os.path.join(TMP, "history.jsonl")),
               ("RECORD_PATH", os.path.join(TMP, "签到记录.csv")),
               ("RETRY_STATE_PATH", os.path.join(TMP, "retry_state.json"))):
    _SANDBOX[_k] = getattr(checkin, _k)
    setattr(checkin, _k, _v)
_ORIG_SPAWN = checkin._spawn_toast
_ORIG_LOG = checkin.log
checkin.log = lambda *a, **k: None
print(f"   沙箱 logdir : {TMP}")
print(f"   真实 logdir : {REAL_LOG_DIR}")

# ---------------------------------------------------------------------------
# 1. 根因 A：9074 必须归成 busy
# ---------------------------------------------------------------------------
section("1. 9074 限流 → busy（不是 fail）")


def _patch_post(status_body, claim_body, status_st="ok", claim_st="ok"):
    def _p(path, token, log, did=None, retries=2):
        if path == "status":
            return status_st, status_body
        return claim_st, claim_body
    return _p


_ORIG_POST = TC._post
_ORIG_DELAY = getattr(TC, "_BUSY_RECHECK_DELAY_S", None)
# ★ 限流后「就地复核」会 sleep 十几秒 —— 测试里必须归零，否则每个用例白等。
TC._BUSY_RECHECK_DELAY_S = 0
_NOT_CHECKED_IN = {"code": 0, "enable": True, "checked_in": False, "credits": 150}

try:
    # ① 真实报文：9074 当前参与用户太多，请稍后再试
    TC._post = _patch_post(_NOT_CHECKED_IN,
                           {"code": 9074, "message": "当前参与用户太多，请稍后再试"})
    st, d = TC.checkin_by_token("t", "did", print)
    check("① 9074 → busy", st == "busy", f"st={st} d={d}")
    check("① 文案点明「限流」", "限流" in d, d)
    check("① 文案带原始 code", "9074" in d, d)
    check("① 文案说明会复核", "复核" in d, d)

    # ② 正对照：9074 不属于任何旧特判（1 / 1001 / 9004）→ 旧代码必然落进兜底 fail
    _OLD_SPECIAL = (1, 1001, 9004)
    check("② 正对照：9074 不在任何旧特判里 → 旧代码必然报 fail",
          9074 not in _OLD_SPECIAL and st != "fail", f"new={st}")

    # ③ 文案式限流（code 不是 9074）
    TC._post = _patch_post(_NOT_CHECKED_IN,
                           {"code": 500, "message": "Rate limit exceeded, try again later"})
    st, d = TC.checkin_by_token("t", "did", print)
    check("③ 文案含 rate limit → busy", st == "busy", f"st={st} d={d}")

    # ④ HTTP 429 走 ("net", "HTTP 429") 回程
    TC._post = _patch_post(_NOT_CHECKED_IN, "HTTP 429", claim_st="net")
    st, d = TC.checkin_by_token("t", "did", print)
    check("④ HTTP 429 → busy（不是「网络异常」）", st == "busy", f"st={st} d={d}")

    # ⑤ 负对照：真正的网络故障仍必须是 fail
    TC._post = _patch_post(_NOT_CHECKED_IN, "<urlopen error timed out>", claim_st="net")
    st, d = TC.checkin_by_token("t", "did", print)
    check("⑤ 负对照：真网络故障仍是 fail", st == "fail" and "网络" in d, f"st={st} d={d}")

    # ⑥ 负对照：未知 code 仍是 fail（别把兜底也改成 busy）
    TC._post = _patch_post(_NOT_CHECKED_IN, {"code": 5000, "message": "unknown"})
    st, d = TC.checkin_by_token("t", "did", print)
    check("⑥ 负对照：未知 code 仍是 fail", st == "fail", f"st={st} d={d}")

    # ⑦ 负对照：claim 的 1001（今日已完成幂等）不能被吸进 busy
    TC._post = _patch_post(_NOT_CHECKED_IN, {"code": 1001, "message": ""})
    st, d = TC.checkin_by_token("t", "did", print)
    check("⑦ 负对照：claim 1001 仍是 skip", st == "skip", f"st={st} d={d}")

    # ⑧ 负对照：9004 设备标识仍是 fail
    TC._post = _patch_post(_NOT_CHECKED_IN, {"code": 9004, "message": ""})
    st, d = TC.checkin_by_token("t", "did", print)
    check("⑧ 负对照：9004 仍是 fail", st == "fail", f"st={st} d={d}")

    # ⑨ ★★ 限流后**就地复核**：status 已变成「已签」→ 直接报成功，不再报待复核
    #    这是 2026-09-20 那轮「首轮报失败、5 分钟后补跑才成功」的正面解法。
    _calls = {"status": 0}

    def _post_recheck(path, token, log, did=None, retries=2):
        if path == "status":
            _calls["status"] += 1
            if _calls["status"] == 1:
                return "ok", _NOT_CHECKED_IN
            return "ok", {"code": 0, "enable": True, "checked_in": True, "credits": 150}
        return "ok", {"code": 9074, "message": "当前参与用户太多，请稍后再试"}

    TC._post = _post_recheck
    st, d = TC.checkin_by_token("t", "did", print)
    check("⑨ 限流后复核到已签 → skip", st == "skip", f"st={st} d={d}")
    check("⑨ 复核确实发生（status 查了 2 次）", _calls["status"] == 2,
          str(_calls["status"]))
    check("⑨ 文案带复核确认的积分", "+150" in d, d)
    check("⑨ 文案说明首次被限流（便于事后定性）", "限流" in d, d)
    check("⑨ 正对照：旧代码这里必然报 fail", st != "fail", st)
    # ★ `tw_checkin.run` 是靠 `detail.startswith("今日已签到")` 决定写 state 的，
    #   前缀一改就会退化成「每轮补跑都重开浏览器」（多花 ~30s）。
    check("⑨ detail 以「今日已签到」开头（保住 run 的写 state 判据）",
          d.startswith("今日已签到"), d)

    # ⑩ 复核仍拿不到结论 → 才是 busy（别把不确定也说成成功）
    _calls2 = {"status": 0}

    def _post_still_unsure(path, token, log, did=None, retries=2):
        if path == "status":
            _calls2["status"] += 1
            return "ok", _NOT_CHECKED_IN
        return "ok", {"code": 9074, "message": "当前参与用户太多，请稍后再试"}

    TC._post = _post_still_unsure
    st, d = TC.checkin_by_token("t", "did", print)
    check("⑩ 复核后仍不确定 → busy（不冒充成功）", st == "busy", f"st={st} d={d}")
    check("⑩ 复核也做了（status 查了 2 次）", _calls2["status"] == 2,
          str(_calls2["status"]))
    check("⑩ 正对照：复核到已签的那次必须与这次结论不同",
          st != "skip", st)

    # ⑪ 负对照：复核时令牌又被吊销 → 不能报成功
    #    ⚠️ 桩必须**有状态**：首次 status 要正常（否则在 `_auth_failed` 那关
    #       就直接返回 expired，根本走不到 claim，测的就不是这条路径了）。
    _calls3 = {"status": 0}

    def _post_revoked(path, token, log, did=None, retries=2):
        if path == "status":
            _calls3["status"] += 1
            if _calls3["status"] == 1:
                return "ok", _NOT_CHECKED_IN
            return "ok", {"code": 1001, "message": "not able to authenticate you"}
        return "ok", {"code": 9074, "message": "当前参与用户太多，请稍后再试"}

    TC._post = _post_revoked
    st, d = TC.checkin_by_token("t", "did", print)
    check("⑪ 负对照：复核时令牌已吊销 → 仍是 busy（不报成功）",
          st == "busy", f"st={st} d={d}")
    check("⑪ 复核确实走到了（status 查了 2 次）", _calls3["status"] == 2,
          str(_calls3["status"]))

    # ⑨ status 侧 1001（已吊销）仍必须是 expired，不能被 busy 抢走
    TC._post = _patch_post({"code": 1001,
                            "message": "We're sorry, but we are not able to authenticate you."},
                           _NOT_CHECKED_IN)
    st, d = TC.checkin_by_token("t", "did", print)
    check("⑨ 负对照：status 1001 仍是 expired", st == "expired", f"st={st} d={d}")
finally:
    TC._post = _ORIG_POST
    if _ORIG_DELAY is not None:
        TC._BUSY_RECHECK_DELAY_S = _ORIG_DELAY

# ---------------------------------------------------------------------------
# 1.5 根因 A2：tw_checkin 的页面兜底会把 busy 丢掉 → 又变回 fail
# ---------------------------------------------------------------------------
section("1.5 tw_checkin 的最终结论：busy 不能被页面兜底覆盖成 fail")

import tw_checkin as TW  # noqa: E402

# ① 接口说过 busy、页面也确认不了 → 必须是 busy（待复核），不是 fail
st, d = TW._final_verdict(("busy", "领取请求被服务端限流（code=9074）"), "明细已渲染但没有今日条目")
check("① 有 busy → 结论是 busy", st == "busy", f"st={st} d={d}")
check("① 文案说明「下一轮会自动复核」", "自动复核" in d, d)
check("① 文案点明原因是服务端限流", "限流" in d, d)
check("① 正对照：旧代码（无视 busy）这里必然报 fail", st != "fail", st)

# ② 没有 busy 时仍必须报 fail（别把所有不确定都说成待复核）
st2, d2 = TW._final_verdict(None, "明细已渲染但没有今日条目")
check("② 无 busy → 仍是 fail", st2 == "fail", f"st={st2} d={d2}")
check("② 负对照：两种结论必须不同", st != st2, f"{st} vs {st2}")
check("② fail 文案保留「两条判据都没能确认」", "两条判据" in d2, d2)

# ③ 接口原因只保留**最新**一条：不能把「旧令牌被吊销」当成最终结论
_reasons = []
TW._note_api_reason(_reasons, ("expired", "令牌已被服务端吊销（code=1001）"))
check("③ api0 的过期原因先被记下", len(_reasons) == 1 and "吊销" in _reasons[0],
      str(_reasons))
TW._note_api_reason(_reasons, ("busy", "领取请求被服务端限流（code=9074）"))
check("③ 后一次的接口原因**顶掉**了旧原因", len(_reasons) == 1, str(_reasons))
check("③ 留下的是最新的限流原因", "限流" in _reasons[0], str(_reasons))
check("③ 正对照：旧写法（append）会留下两条、把早已解决的原因当结论",
      "吊销" not in _reasons[0], str(_reasons))
TW._note_api_reason(_reasons, None)          # 取不到令牌 → 不该改动
check("③ api 为 None 时不动 reasons", len(_reasons) == 1, str(_reasons))

# ④ 接线检查：_do_checkin 里必须真的用上这两个 helper
_src = open(os.path.join(checkin.ROOT, "tw_checkin.py"), encoding="utf-8").read()
check("④ _do_checkin 末尾走 _final_verdict", "return _final_verdict(busy, why)" in _src)
check("④ busy 有被接住（不是只认 ok/skip）",
      'api2[0] == "busy"' in _src and 'api[0] == "busy"' in _src)
check("④ 每次接口判定都会刷新原因（顶掉旧原因）",
      # ⚠️ 别用 count 匹配前缀 —— 函数**定义**行 `def _note_api_reason(reasons, api):`
      #    也会被算进去。只断言两个**调用点**都存在。
      "_note_api_reason(reasons, api)\n" in _src
      and "_note_api_reason(reasons, api2)" in _src)

# ---------------------------------------------------------------------------
# 2. 根因 B：_note() 的归因顺序
# ---------------------------------------------------------------------------
section("2. 弹窗归因：令牌吊销不能被说成「需重新登录」")

_CAP = {}


def _fake_spawn(payload):
    _CAP["payload"] = payload


checkin._spawn_toast = _fake_spawn


def _notes(results):
    """跑一次 _notify_summary，返回 {账号: note} 与整份载荷。"""
    _CAP.clear()
    title = checkin._notify_summary(results, attempt=1, last=False)
    pl = json.loads(_CAP["payload"])
    return {r["name"]: r["note"] for r in pl["rows"]}, pl, title


try:
    # ① 真实事故文案：令牌吊销（_TOKEN_REVOKED 里含「登录」二字）
    revoked = ("令牌已被服务端吊销；打开一次 Trae CN IDE 或 TraeWork 页面"
               "（work.trae.cn）登录即可自动换到新令牌（code=1001：not able to authenticate）")
    notes, pl, _t = _notes([
        dict(platform="traework", account="Klein", status="expired", detail=revoked)])
    check("① 令牌吊销 → 「失败：令牌过期」", notes["Klein"] == "失败：令牌过期",
          notes["Klein"])
    check("① 正对照：旧顺序会误判成「需重新登录」",
          notes["Klein"] != "失败：需重新登录", notes["Klein"])

    # ② 正对照：真正的「要登录」还得能认出来（别把这条分支删成死代码）
    notes, pl, _t = _notes([
        dict(platform="traework", account="Klein", status="fail",
             detail="未找到签到入口，页面提示请先登录后再签到")])
    check("② 真的需要登录 → 「失败：需重新登录」",
          notes["Klein"] == "失败：需重新登录", notes["Klein"])

    # ③ busy 的两种来源分别有具体文案，且**绝不能**显示成「已签到」
    notes, pl, _t = _notes([
        dict(platform="traework", account="A", status="busy",
             detail="领取请求被服务端限流（code=9074：当前参与用户太多，请稍后再试）；"
                    "本轮视为未完成，稍后补跑会自动复核（服务端很可能已受理）"),
        dict(platform="traework", account="B", status="busy",
             detail="该账号的浏览器 profile 正被另一个实例使用，本轮跳过")])
    check("③ 限流 busy → 「待复核：服务端限流」", notes["A"] == "待复核：服务端限流", notes["A"])
    check("③ 占用 busy → 「待复核：浏览器被占用」",
          notes["B"] == "待复核：浏览器被占用", notes["B"])
    check("③ busy 绝不能显示成「已签到」",
          all("已签到" not in v for v in notes.values()), str(notes))

    # ④ 正对照：`skip` 仍然要显示「已签到」（别把 busy 的修法误伤 skip）
    notes, pl, _t = _notes([
        dict(platform="traework", account="A", status="skip", detail="今日已完成")])
    check("④ 正对照：skip 仍显示「已签到」", notes["A"] == "已签到", notes["A"])

    # -----------------------------------------------------------------
    # 3. 三态灯 + 标题口径
    # -----------------------------------------------------------------
    section("3. 三态灯（warn）与标题口径")

    notes, pl, title = _notes([
        dict(platform="traework", account="Klein", status="busy",
             detail="领取请求被服务端限流（code=9074：当前参与用户太多，请稍后再试）；"
                    "本轮视为未完成，稍后补跑会自动复核"),
        dict(platform="traecn", account="Klein", status="ok", detail="签到成功，领取 150 积分"),
        dict(platform="workbuddy", account="Klein", status="skip", detail="今日已完成"),
        dict(platform="workbuddy", account="KK", status="ok", detail="签到成功，领取 100 积分")])
    states = {r["name"] + r["tag"]: r["state"] for r in pl["rows"]}
    check("③ 全部只有 busy 时标题不说「失败」", "失败" not in title, title)
    check("③ 标题写「待复核」", "待复核" in title, title)
    check("③ 标题写完成数 3/4", "3/4" in title, title)
    check("③ busy 行 state=warn（不是 fail）",
          states["KleinTW"] == "warn", str(states))
    check("③ 正对照：busy 若用旧口径会渲染成 fail",
          ("fail" if "busy" not in ("ok", "skip") else "skip") == "fail")

    # ④ busy + 真失败 并存：两个计数都要出现，且不能互相吞掉
    notes, pl, title = _notes([
        dict(platform="traework", account="A", status="busy", detail="领取请求被服务端限流"),
        dict(platform="traework", account="B", status="fail", detail="网络异常：连接超时"),
        dict(platform="traecn", account="C", status="ok", detail="签到成功，领取 150 积分")])
    check("④ 标题同时含「失败」与「待复核」",
          "失败" in title and "待复核" in title, title)
    st_by_name = {r["name"]: r["state"] for r in pl["rows"]}
    check("④ busy 行 warn / fail 行 fail",
          st_by_name["A"] == "warn" and st_by_name["B"] == "fail", str(st_by_name))
    check("④ 失败详情补行只统计**真失败**（1 条）",
          sum(1 for r in pl["rows"] if r["tag"] == "!!") == 1,
          str([r for r in pl["rows"] if r["tag"] == "!!"]))

    # ⑤ 全绿时标题保持原样（别把正常路径改坏）
    notes, pl, title = _notes([
        dict(platform="traework", account="A", status="skip", detail="今日已完成")])
    check("⑤ 全绿 → 「全部成功」", title == "每日签到：全部成功", title)
    _CAP.clear()
    t2 = checkin._notify_summary(
        [dict(platform="traework", account="A", status="ok", detail="签到成功")],
        attempt=2, last=False)
    check("⑤ 补跑轮全绿 → 「补跑完成」", t2 == "每日签到：补跑完成", str(t2))

    # ⑥ _notify_summary 必须把标题返回给调用方（供「已弹窗通知」日志用）
    check("⑥ _notify_summary 返回标题（不再是 None）", isinstance(t2, str) and t2, str(t2))

    # -----------------------------------------------------------------
    # 4. 弹窗模板必须真的有琥珀灯
    # -----------------------------------------------------------------
    section("4. toast.html 的 .led.warn（没有它 → warn 行是个没有背景的空 div）")

    html = open(os.path.join(checkin.ROOT, "toast.html"), encoding="utf-8").read()
    m = re.search(r"\.led\.warn\s*\{([^}]*)\}", html)
    check("④ toast.html 定义了 .led.warn", m is not None)
    if m:
        body = m.group(1)
        bg = re.search(r"background:\s*(#[0-9a-fA-F]{6})", body)
        check("④ .led.warn 有 background 颜色", bg is not None, body.strip())
        if bg:
            col = bg.group(1).lower()
            check("④ 琥珀色不是红/蓝（别复制错）",
                  col not in ("#ff6b6b", "#7dd3fc", "#3ddc84"), col)
            r_, g_, b_ = (int(col[i:i + 2], 16) for i in (1, 3, 5))
            check("④ 颜色确实偏琥珀（R>G>B 且够亮）",
                  r_ > g_ > b_ and r_ > 200, f"{col} rgb=({r_},{g_},{b_})")
    # 正对照：模板里 fail 的红色必须还在（别把 .led.fail 误删）
    check("④ 正对照：.led.fail 仍在", ".led.fail" in html)

    # -----------------------------------------------------------------
    # 5. GUI 侧口径（app/config.count_status）
    # -----------------------------------------------------------------
    section("5. GUI 侧：count_status 把 busy 单独一档")
    got = C.count_status([{"status": "ok"}, {"status": "skip"},
                          {"status": "busy"}, {"status": "fail"}])
    check("⑤ (ok, busy, fail) = (2, 1, 1)", got == (2, 1, 1), str(got))
    check("⑤ 正对照：旧口径会把 busy 算进 fail",
          (len([1 for r in [{"status": "busy"}] if r["status"] not in ("ok", "skip")])
           == 1))
    check("⑤ GUI STATUS_CN 有 busy 且叫「待复核」",
          C.STATUS_CN.get("busy") == "待复核", str(C.STATUS_CN.get("busy")))
    check("⑤ CLI _STATUS_CN 有 busy 且叫「待复核」",
          checkin._STATUS_CN.get("busy") == "待复核",
          str(checkin._STATUS_CN.get("busy")))
    check("⑤ 两侧口径一致（防漂移）",
          C.STATUS_CN.get("busy") == checkin._STATUS_CN.get("busy"))
    check("⑤ busy 仍配琥珀色 tone", C.STATUS_TONE.get("busy") == "amber",
          str(C.STATUS_TONE.get("busy")))

    # -----------------------------------------------------------------
    # 5.5 弹窗时长（写在两处，改一处等于没改）+「弹过没」可事后核对
    # -----------------------------------------------------------------
    section("5.5 弹窗时长与「弹过没」可核对")

    m_html = re.search(r"setTimeout\(doClose,\s*(\d+)\)", html)
    host_src = open(os.path.join(checkin.ROOT, "toast_gui.py"), encoding="utf-8").read()
    m_host = re.search(r"root\.after\((\d+),\s*close\)", host_src)
    check("⑤ toast.html 有自动关闭定时器", m_html is not None)
    check("⑤ toast_gui.py 有自动关闭定时器", m_host is not None)
    if m_html and m_host:
        _a, _b = int(m_html.group(1)), int(m_host.group(1))
        # 卡片是「无头浏览器渲染 HTML → 截图 → Tk 画出来」：HTML 里那个 setTimeout
        # 在这种渲染模式下其实**不生效**（`pywebview.api` 根本不存在，异常被吞），
        # 真正决定用户看到多久的是宿主 `root.after`。两处必须一致，否则改了白改。
        check("⑤ 两处时长一致（改一处等于没改）", _a == _b, f"html={_a} host={_b}")
        check("⑤ 时长 >= 10s（6.5s 用户走神就看不到）", _a >= 10000, str(_a))

    _logs = []
    _saved = (checkin.log, checkin.load_config, checkin.SILENT,
              checkin._is_last_attempt)
    checkin.log = lambda m: _logs.append(str(m))
    checkin.load_config = lambda: {
        "options": {"toast_notify": True, "retry_toast_quiet": True},
        "schedule": {"retry_enabled": True}}
    checkin.SILENT = True
    checkin._is_last_attempt = lambda cfg, now=None: False
    try:
        checkin.save_retry_state({"date": datetime.date.today().isoformat(),
                                  "attempts": 0, "notified_first_fail": False,
                                  "notified_last": False})
        checkin._maybe_notify([dict(platform="traework", account="A", status="ok",
                                    detail="签到成功，领取 150 积分")])
    finally:
        (checkin.log, checkin.load_config, checkin.SILENT,
         checkin._is_last_attempt) = _saved
    check("⑤ 弹窗后写了「已弹窗通知」日志（可事后 grep 核对）",
          any("已弹窗通知" in m for m in _logs), str(_logs))
    check("⑤ 日志里带上了标题", any("全部成功" in m for m in _logs), str(_logs))
    check("⑤ 正对照：静默分支也要写明「静默」"
          "（否则「没弹」和「没跑」分不清）",
          "静默" in open(os.path.join(checkin.ROOT, "checkin.py"),
                         encoding="utf-8").read())
finally:
    checkin._spawn_toast = _ORIG_SPAWN
    checkin.log = _ORIG_LOG

# ---------------------------------------------------------------------------
# 7. ★★ 端到端复现 2026-09-20 首轮时间线：首轮不再误报「失败」
# ---------------------------------------------------------------------------
section("7 端到端：2026-09-20 首轮（api0 过期 → api1/api2 限流 → 明细无今日条目）")

# 事故原貌（logs/checkin_2026-09-20.log）：
#   13:33:50 api0 → expired「令牌已被服务端吊销（code=1001）」
#   13:33:55 api1 → busy「9074 限流」   ← 此刻页面刷新出了**新**令牌，旧令牌已作废
#   13:34:13 api2 → busy「9074 限流」
#   13:34:25 签到明细已渲染（11 条）但没有今日签到条目
#   13:34:37 本轮完成：成功/跳过 3，待复核 0，失败 1     ← 旧代码在这里**误报**
# 现在把这条时间线**灌进真实的 `_do_checkin`**，看它吐什么结论。
# ★ 只打桩「外部世界」（浏览器/接口/截图），判据合并逻辑一律跑真代码。
_ORIG_TW = {_n: getattr(TW, _n) for _n in
            ("_shot", "_page_text", "_api_via_token_page", "_try_click_sign",
             "_open_points_dashboard", "_try_api_checkin", "_expand_sign_detail",
             "_sign_progress")}
_ORIG_SLEEP = TW.time.sleep

# 明细文本：11 条，最新到期日是 10-20（= 09-19 签的）——
# **不含今天+31（= 10-21）**，即「今天那条确实还没落账」（截图 Klein_dash2_nohit.png 同款）。
_DASH = ("每日签到\n共 11 笔\n"
         "2026-09-09 签到奖励 +150 2026-10-10\n"
         "2026-09-19 签到奖励 +150 2026-10-20\n积分明细")
_TW_CALLS = {"api": []}


def _tw_api0(ctx, log, name, tag):
    _TW_CALLS["api"].append(tag)
    return "expired", "令牌已被服务端吊销（code=1001：not able to authenticate）"


def _tw_api_busy(pages, log, name, tag):
    _TW_CALLS["api"].append(tag)
    return "busy", "领取请求被服务端限流（code=9074：当前参与用户太多，请稍后再试）"


TW._shot = lambda *a, **k: None
TW._page_text = lambda page: page if isinstance(page, str) else ""
TW._api_via_token_page = _tw_api0
TW._try_click_sign = lambda *a, **k: None
TW._open_points_dashboard = lambda ctx, page, log, name: _DASH
TW._try_api_checkin = _tw_api_busy
TW._sign_progress = lambda t: 11
TW._expand_sign_detail = lambda *a, **k: (
    "fail", "签到明细已渲染（11 条）但没有今日签到条目")
TW.time.sleep = lambda *a, **k: None       # 别让 attempt 1 的 5s 真等

try:
    _st7, _detail7 = TW._do_checkin(None, "PAGE", lambda m: None,
                                    "Klein", TMP, datetime.date(2026, 9, 20))
finally:
    for _n, _v in _ORIG_TW.items():
        setattr(TW, _n, _v)
    TW.time.sleep = _ORIG_SLEEP

check("⑦ 首轮结论是 busy（待复核）", _st7 == "busy", f"st={_st7} d={_detail7}")
check("⑦ ★ 正对照：旧代码这条路径必然报 fail", _st7 != "fail", _st7)
check("⑦ 文案里带「限流」", "限流" in _detail7, _detail7)
check("⑦ ★ 过时的「吊销」被顶掉，不再被当成结论", "吊销" not in _detail7, _detail7)
check("⑦ 三个接口判定都试过了", _TW_CALLS["api"] == ["api0", "api1", "api2"],
      str(_TW_CALLS["api"]))

# 把这条结论灌进弹窗，检查**用户看到的那句话**（用户投诉的正是这句话）
checkin._spawn_toast = _fake_spawn
_rs7 = [
    dict(platform="traecn", account="Klein", status="ok",
         detail="签到成功，领取 150 积分，已连续签到 12 天"),
    dict(platform="workbuddy", account="Klein", status="ok", detail="今日已签到（幂等）"),
    dict(platform="traecn", account="Klein2", status="ok", detail="签到成功，领取 150 积分"),
    dict(platform="traework", account="Klein", status=_st7, detail=_detail7),
]
try:
    _n7, _pl7, _title7 = _notes(_rs7)
    check("⑦ 弹窗标题说「待复核」", "待复核" in _title7, _title7)
    check("⑦ ★ 标题里**不再出现「失败」**（用户看到的原话）", "失败" not in _title7, _title7)
    check("⑦ 该行是琥珀灯 warn，不是红灯 fail",
          _pl7["rows"][3]["state"] == "warn", _pl7["rows"][3]["state"])
    check("⑦ 该行文案是「待复核：服务端限流」",
          _pl7["rows"][3]["note"] == "待复核：服务端限流", _pl7["rows"][3]["note"])
    check("⑦ 没有多出「失败详情」行", len(_pl7["rows"]) == 4, str(len(_pl7["rows"])))

    # ★ 正对照：真失败时**必须还能说「失败」** —— 否则就是"只会报好消息"，
    #   判据等于失效。这一条守住上面那几条不变成无意义的断言。
    _n7b, _pl7b, _title7b = _notes(_rs7[:3] + [
        dict(platform="traework", account="Klein", status="fail",
             detail="未找到签到入口")])
    check("⑦ 正对照：真失败时标题照样出现「失败」", "失败" in _title7b, _title7b)
    check("⑦ 正对照：真失败时该行是红灯", _pl7b["rows"][3]["state"] == "fail",
          _pl7b["rows"][3]["state"])
finally:
    checkin._spawn_toast = _ORIG_SPAWN

# ---------------------------------------------------------------------------
# 8. ★★ 承重不变量：busy **不能**被记成「今日已完成」，否则补跑会直接跳过
# ---------------------------------------------------------------------------
section("8 busy 不写 state —— 否则补跑直接跳过，用户永远等不到复核")

# 为什么这是**整条链路的承重墙**：
#   `_gate_skip()` 判「今日是否还需要跑」靠的就是 `state[key] != today`。
#   要是 busy 也写了 state，下一轮补跑会认为「已完成」→ 秒退、不开浏览器 →
#   那次限流**永远不会被复核**，用户看到的就是永久的「待复核」。
#   2026-09-20 那天 13:38 的补跑之所以能复核成功，正是因为 busy 没写 state。
_today8 = datetime.date.today().isoformat()
_cfg8 = {"traework": {"enabled": False}, "workbuddy": {"enabled": False},
         "traecn": {"enabled": True, "accounts": []},
         "schedule": {"checkin_time": "09:00", "retry_enabled": True}}
_ORIG_TC8 = (TC.collect_accounts, TC.checkin_account)
_ACC8 = [{"name": "Klein", "uid": "u8"}]


def _acc8(cfg, log):
    return _ACC8


try:
    TC.collect_accounts = _acc8

    def _mk(_st, _d):
        def _f(acc, log):
            return _st, _d
        return _f

    # ⑧-a busy 轮：state 必须**一点没动**
    TC.checkin_account = _mk("busy", "领取请求被服务端限流（code=9074）")
    _state8 = {}
    _res8 = TC.run(lambda m: None, _state8, _cfg8, _today8)
    check("⑧ busy 轮 state 保持为空（不被记成已完成）", _state8 == {}, str(_state8))
    check("⑧ busy 轮结果状态确实是 busy", _res8[0]["status"] == "busy", _res8[0]["status"])
    # ★ 这一步才是重点：门控必须判定「还得再跑一轮」
    _g8 = checkin._gate_skip(_cfg8, _state8, _today8,
                             now=datetime.datetime(2026, 9, 20, 12, 0))
    check("⑧ ★ busy 之后门控仍判「需要执行」→ 补跑真的会复核",
          _g8 is None, str(_g8))

    # ⑧-b 正对照：ok 轮必须写 state，且门控转为「今日全部账号已完成」
    TC.checkin_account = _mk("ok", "签到成功，领取 150 积分")
    _state8b = {}
    TC.run(lambda m: None, _state8b, _cfg8, _today8)
    check("⑧ 正对照：ok 轮写了 state",
          _state8b == {"traecn:u8": _today8}, str(_state8b))
    _g8b = checkin._gate_skip(_cfg8, _state8b, _today8,
                              now=datetime.datetime(2026, 9, 20, 12, 0))
    check("⑧ 正对照：ok 之后门控说「今日全部账号已完成」",
          _g8b is not None and "已完成" in _g8b, str(_g8b))

    # ⑧-c 边界：skip 只有在 detail 以「今日已签到」开头时才写 state
    #     （这个前缀是 `tw_checkin.run` / `tc_checkin.run` 共用的口径，
    #      改了它 → 每轮补跑都重开浏览器，白花 ~30s）
    TC.checkin_account = _mk("skip", "今日已签到（接口确认，+150积分）")
    _state8c = {}
    TC.run(lambda m: None, _state8c, _cfg8, _today8)
    check("⑧ skip +「今日已签到」前缀 → 写 state",
          _state8c == {"traecn:u8": _today8}, str(_state8c))

    TC.checkin_account = _mk("skip", "今日已完成（别的说法）")
    _state8d = {}
    TC.run(lambda m: None, _state8d, _cfg8, _today8)
    check("⑧ 正对照：skip 但前缀不对 → **不写** state（前缀是判据不是装饰）",
          _state8d == {}, str(_state8d))
finally:
    TC.collect_accounts, TC.checkin_account = _ORIG_TC8

# 另外两处写点跑不了（要浏览器），但必须**源码级**守住：
#   `tw_checkin` / `wb_checkin` 的守卫里同样不能出现 busy，
#   而且每个文件只允许**一个** `state[key] = today` ——
#   防止有人"顺手"再加一处写点把 busy 也记进去。
for _mod, _need in (("tw_checkin.py", 'status == "ok" or (status == "skip"'),
                    ("wb_checkin.py", 'status == "ok"')):
    _s8 = open(os.path.join(checkin.ROOT, _mod), encoding="utf-8").read()
    check(f"⑧ {_mod} 只有一处 state 写点（新增写点会绕过守卫）",
          _s8.count("state[key] = today") == 1,
          str(_s8.count("state[key] = today")))
    check(f"⑧ {_mod} 的写点守卫里没有 busy", _need in _s8 and "busy" not in _need,
          _need)

# ---------------------------------------------------------------------------
# 6. 生产文件指纹比对（沙箱没漏）
# ---------------------------------------------------------------------------
section("6. 生产文件指纹（沙箱没漏）")
_drift = [p for p in PROD_FILES if _fp(p) != _BEFORE[p]]
check("⑥ 生产 logs 一个都没被改动", not _drift,
      "; ".join(f"{p}" for p in _drift))
for _k, _v in _SANDBOX.items():
    setattr(checkin, _k, _v)

print()
print(f"结果：{ok} OK / {fail} FAIL / {skip} SKIP")
sys.exit(1 if fail else 0)
