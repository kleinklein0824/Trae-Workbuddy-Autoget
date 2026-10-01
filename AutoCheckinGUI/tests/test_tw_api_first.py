#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TraeWork / TraeCN「接口优先判定」与「令牌新鲜度」的回归测试（2.4.9 新增）。

背景（2026-09-18 定因）
----------------------
用户反馈「每次自动化签到都是 traework cn 出现问题」。查 logs/history.jsonl：
  * traework 累计 26 次 fail，绝大多数是「未找到签到入口且无法确认已签到」
    或「签到明细已渲染（9 条）但没有今日签到条目」；
  * traecn 当天的失败是
    `状态查询异常：{'checked_in': False, 'code': 1001, ... 'not able to authenticate you'}`。

真正的原因有两个，**都不是"页面坏了"**：
  ① TraeWork 与 TraeCN 是**同一个 Trae 账号、同一个签到活动**（uid 相同，
     同一个 api.trae.cn/trae/api/v2/ug/checkin_credits）。执行顺序是
     traework → traecn，于是 traework 先去查一个**还没被领取**的活动，
     明细里当然没有今日条目 → 判 fail；十几秒后 traecn 才真正领取成功。
     也就是说 traework 的失败是**自己造成的假失败**。
  ② traecn 用的缓存令牌已被服务端吊销（JWT 里 exp 还剩 7 天，但服务端
     返回 code=1001 认证失败）。旧代码把它归入「状态查询异常」这种笼统失败，
     既不给自救指引，也不会去用别处**更新鲜**的令牌。

修复：
  * `tw_checkin` 增加**接口优先**路径：读页面 localStorage 的
    `Cloud-IDE-Token` → 直连 checkin_credits status/claim；页面文本判据降级为兜底。
  * `tc_checkin` 把 status 的 code=1001 识别为「令牌被吊销」(expired)；
    令牌选择改为按**发现时间**优先；失败时换另一枚候选令牌重试。
  * `tw_checkin` 把服务端认可的令牌回写共享缓存，让 traecn 也能用上。

本测试**不联网、不开浏览器**：全部用桩替换 `_post` / 页面对象。
用法：python tests/test_tw_api_first.py
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.dirname(HERE)
sys.path.insert(0, GUI)
sys.path.insert(0, os.path.dirname(GUI))

import app.config  # noqa: E402,F401  定位项目根并挂 sys.path
import tc_checkin as TC  # noqa: E402
import tw_checkin as TW  # noqa: E402

ok = fail = 0


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label}   {extra}")


TMP = tempfile.mkdtemp(prefix="tb_twapi_")

# ---------------------------------------------------------------------------
# ★ 生产数据沙箱（2026-09-18 补的回归）
#
# 本测试会调 `record_token` / `checkin_by_token(record=True)`，它们**按设计**
# 会写 `TC.TOKENS_PATH`。最初只在 B / C 两节里把路径改道，D 节开头又还原成
# 真实路径 —— 于是 D 节把一枚 uid=42 的假令牌写进了
# `logs/tokens_traecn.json`。
#
# 后果不是"脏了个缓存文件"这么轻：`collect_accounts` 会把缓存里每个 uid 都
# 当成一个**真实账号**，于是 `_gate_skip` 永远算得出"有账号今日未完成" →
# **计划任务的幂等门控彻底失效**（verify_automation_chain 里那两条门控断言
# 就是这么红的，且红得莫名其妙）。
#
# 所以：整份脚本从头到尾都把 TOKENS_PATH 指向临时目录，末尾再统一还原；
# 并且用「指纹比对」守住生产文件 —— 只比对，绝不删除（沙箱安全删除红线）。
# ---------------------------------------------------------------------------
_REAL_TOKENS_PATH = TC.TOKENS_PATH
TC.TOKENS_PATH = os.path.join(TMP, "tokens_traecn.json")
_SANDBOX_TOKENS_PATH = TC.TOKENS_PATH
_LOG_DIR = os.path.dirname(_REAL_TOKENS_PATH)


def _fingerprint(paths):
    """生产文件的 (mtime_ns, size) 指纹。用指纹而不是删除来验证"没被改动"。"""
    out = {}
    for p in paths:
        try:
            st = os.stat(p)
            out[p] = (st.st_mtime_ns, st.st_size)
        except OSError:
            out[p] = None
    return out


_PROD_FILES = [os.path.join(_LOG_DIR, n) for n in
               ("tokens_traecn.json", "state.json", "retry_state.json")]
_PROD_BEFORE = _fingerprint(_PROD_FILES)

# 一枚**结构真实**的 JWT：payload = {"data":{"id":"42"},"exp":1789735739}
# （`_token_payload` 会从 data.id 取 uid、从 exp 取过期时间；
#   用假串测会静默走不到 record 分支，等于白测）
JWT = ("eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
       "eyJkYXRhIjp7ImlkIjoiNDIifSwiZXhwIjoxNzg5NzM1NzM5fQ.sig")


def _mk_post(status_body=None, claim_body=None):
    """造一个 `_post` 桩。"""
    calls = []

    def _post(path, token, log, did=None, retries=2):
        calls.append({"path": path, "token": token, "did": did})
        body = status_body if path == "status" else claim_body
        if isinstance(body, tuple):        # ("net", "...") 这类分类直通
            return body
        return "ok", body

    return _post, calls


# ============================================================ A. code=1001 的语义
print("\n== A. status 的 code=1001 = 令牌被吊销（不是「今天没签成」）==")
_revoked = {"checked_in": False, "code": 1001, "enable": False,
            "message": "We're sorry, but we are not able to authenticate you."}
_real_post = TC._post
try:
    TC._post, _ = _mk_post(_revoked)
    st, detail = TC.checkin_by_token("tok", None, lambda m: None)
    check("status code=1001 → expired（可自救，不是 fail）", st == "expired", st)
    check("文案给出自救路径（打开 IDE / TraeWork 页面）",
          "Trae CN IDE" in detail and "TraeWork" in detail, detail)
    check("文案不再是笼统的「状态查询异常」", "状态查询异常" not in detail, detail)
    check("文案保留服务端原话便于定性",
          "authenticate" in detail, detail)

    # 正对照：claim 的 1001 是「今日已完成」的幂等返回，语义完全不同
    TC._post, _ = _mk_post({"checked_in": False, "code": 0, "enable": True},
                           {"code": 1001, "message": "already checked in"})
    st2, d2 = TC.checkin_by_token("tok", None, lambda m: None)
    check("【正对照】claim code=1001 仍是 skip（幂等）", st2 == "skip", st2)
    check("【正对照】幂等文案正确", "今日已完成" in d2, d2)
    check("_auth_failed(by_code=False) 不把 claim 的 1001 当认证失败",
          TC._auth_failed({"code": 1001, "message": "already checked in"},
                          by_code=False) is False)
    check("_auth_failed(by_code=True) 把 status 的 1001 当认证失败",
          TC._auth_failed({"code": 1001}) is True)

    # 正常路径不能被改坏
    TC._post, _ = _mk_post({"checked_in": True, "code": 0, "credits": 150,
                            "enable": True})
    st3, d3 = TC.checkin_by_token("tok", None, lambda m: None, warn="；W")
    check("已签到 → skip 且带上到期提醒", st3 == "skip" and d3.endswith("；W"), d3)

    TC._post, _ = _mk_post({"checked_in": False, "code": 0, "enable": True},
                           {"code": 0, "credits": 150, "extra_credits": 50})
    st4, d4 = TC.checkin_by_token("tok", None, lambda m: None)
    check("未签到 → 自动 claim → ok", st4 == "ok", st4)
    check("claim 成功文案含积分与额外积分",
          "150" in d4 and "50" in d4, d4)

    TC._post, _ = _mk_post({"checked_in": False, "code": 0, "enable": False})
    st5, d5 = TC.checkin_by_token("tok", None, lambda m: None)
    check("活动未开启 → skip（不算失败）", st5 == "skip", st5)

    TC._post, _ = _mk_post({"checked_in": False, "code": 0, "enable": True},
                           {"code": 9004})
    st6, d6 = TC.checkin_by_token("tok", None, lambda m: None)
    check("claim 9004 → fail 且提示打开 IDE", st6 == "fail" and "9004" in d6, d6)
finally:
    TC._post = _real_post


# ============================================================ B. 令牌新鲜度
print("\n== B. 令牌新鲜度：按「发现时间」优先，而不是「exp 更长」==")
check("发现时间更新的令牌胜出（即使 exp 更短）",
      TC._better_token({"exp": 100, "saved_at": 200},
                       {"exp": 99999, "saved_at": 100}) is True)
check("发现时间相同才比 exp",
      TC._better_token({"exp": 100, "saved_at": 100},
                       {"exp": 99999, "saved_at": 100}) is False)
check("旧版缓存（无 saved_at）视为最旧",
      TC._entry_freshness({"exp": 1}) == 0.0)
check("cur 为空时任何候选都可接受",
      TC._better_token({"exp": 1}, None) is True)

_real_scan = TC._scan_ide_logs
_real_load = TC._load_cache
_real_save = TC._save_cache
_real_ident = TC.scan_client_identity
_real_fetch = TC._fetch_username
_real_tokpath = TC.TOKENS_PATH   # 已是上面的沙箱路径（整份脚本都改道）
try:
    TC.TOKENS_PATH = os.path.join(TMP, "tokens_traecn.json")
    TC._scan_ide_logs = lambda log=None: {
        "42": ("IDE_TOK", 9999999999, 100.0)}          # 发现早、exp 长
    # ★ `exp` 必须**相对当前时间**算。这里原本硬编码 1789735739
    #   （= 2026-09-18 20:48:59），到 2026-09-19 就变成**已过期**，
    #   而 `collect_accounts` 会跳过 `exp <= now` 的令牌 → `accs` 变空
    #   → 下面两条断言**假红**（2026-09-19 实测 59 OK / 2 FAIL）。
    #   本用例要的是「exp 比 IDE 那枚短」，不是「快过期」，所以给 30 天足够。
    _soon_exp = int(time.time()) + 30 * 86400
    TC._load_cache = lambda: {
        "42": {"token": "TW_TOK", "exp": _soon_exp, "saved_at": 500.0,
               "name": "Klein"}}                       # 发现晚、exp 短
    TC._save_cache = lambda c: None
    TC.scan_client_identity = lambda *a, **k: {}
    TC._fetch_username = lambda tok: None
    accs = TC.collect_accounts({}, lambda *a: None)
    check("collect_accounts 选的是「更新鲜」的令牌", len(accs) == 1 and
          accs[0]["token"] == "TW_TOK",
          accs[0]["token"] if accs else None)
    check("更新鲜的令牌保留了缓存里的昵称",
          accs and accs[0]["name"] == "Klein",
          accs[0]["name"] if accs else None)

    # 反过来：IDE 日志更新鲜时应当胜出
    TC._scan_ide_logs = lambda log=None: {"42": ("IDE_TOK", 9999999999, 900.0)}
    accs2 = TC.collect_accounts({}, lambda *a: None)
    check("【正对照】IDE 日志更新鲜时选 IDE 令牌",
          accs2 and accs2[0]["token"] == "IDE_TOK",
          accs2[0]["token"] if accs2 else None)
finally:
    TC._scan_ide_logs = _real_scan
    TC._load_cache = _real_load
    TC._save_cache = _real_save
    TC.scan_client_identity = _real_ident
    TC._fetch_username = _real_fetch
    TC.TOKENS_PATH = _real_tokpath


# ============================================================ C. 令牌回写
print("\n== C. record_token：把页面里取到的可用令牌回写共享缓存 ==")
try:
    TC.TOKENS_PATH = os.path.join(TMP, "tokens_traecn.json")
    with io.open(TC.TOKENS_PATH, "w", encoding="utf-8") as f:
        json.dump({"42": {"token": "OLD", "exp": 1, "name": "Klein"}}, f)
    ent = TC.record_token("42", "NEWTOK", 1789735739, name=None,
                          log=lambda m: None)
    saved = json.load(io.open(TC.TOKENS_PATH, encoding="utf-8"))
    check("令牌已写入缓存", saved["42"]["token"] == "NEWTOK", saved)
    check("带上了发现时间 saved_at", float(saved["42"]["saved_at"]) > 0, saved)
    check("name=None 时保留原有昵称", saved["42"]["name"] == "Klein", saved)
    check("record_token 返回写入后的条目", ent and ent["token"] == "NEWTOK", ent)
    check("uid 为空时安全返回 None", TC.record_token("", "x", 1) is None)
    check("token 为空时安全返回 None", TC.record_token("42", "", 1) is None)

    # checkin_by_token(record=True) 只在服务端认可时才写缓存
    with io.open(TC.TOKENS_PATH, "w", encoding="utf-8") as f:
        json.dump({"42": {"token": "OLD", "exp": 1, "name": "K"}}, f)
    _real_post2 = TC._post
    try:
        TC._post, _ = _mk_post({"checked_in": True, "code": 0, "credits": 1,
                                "enable": True})
        TC.checkin_by_token(JWT, None, lambda m: None, record=True)
        got = json.load(io.open(TC.TOKENS_PATH, encoding="utf-8"))
        check("服务端认可 → 回写缓存", got["42"]["token"] == JWT, got)
    finally:
        TC._post = _real_post2

    with io.open(TC.TOKENS_PATH, "w", encoding="utf-8") as f:
        json.dump({"42": {"token": "OLD", "exp": 1, "name": "K"}}, f)
    _real_post3 = TC._post
    try:
        TC._post, _ = _mk_post(_revoked)
        TC.checkin_by_token(JWT, None, lambda m: None, record=True)
        got2 = json.load(io.open(TC.TOKENS_PATH, encoding="utf-8"))
        check("【正对照】令牌被吊销时**不**污染缓存",
              got2["42"]["token"] == "OLD", got2)
    finally:
        TC._post = _real_post3
finally:
    TC.TOKENS_PATH = _real_tokpath


# ============================================================ D. 换令牌补救
print("\n== D. checkin_account：当前令牌被拒就换另一枚候选再试 ==")
_real_post = TC._post
_real_cands = TC._token_candidates
_real_load = TC._load_cache
try:
    TC._load_cache = lambda: {}
    TC._token_candidates = lambda uid, log=None: [
        {"token": "DEAD", "exp": 2, "saved_at": 200.0},
        {"token": "ALIVE", "exp": 1, "saved_at": 100.0}]
    seen = []

    def _post(path, token, log, did=None, retries=2):
        seen.append(token)
        if token == "DEAD":
            return "ok", _revoked
        return "ok", {"checked_in": True, "code": 0, "credits": 150,
                      "enable": True}

    TC._post = _post
    st, d = TC.checkin_account({"uid": "42", "token": "DEAD", "name": "Klein",
                                "did": None}, lambda m: None)
    check("被拒后换到可用令牌 → skip", st == "skip", st)
    check("确实依次试了 DEAD → ALIVE", seen == ["DEAD", "ALIVE"], seen)

    # 负对照：两枚都不可用 → 仍报 expired
    seen.clear()

    def _post2(path, token, log, did=None, retries=2):
        seen.append(token)
        return "ok", _revoked

    TC._post = _post2
    st2, d2 = TC.checkin_account({"uid": "42", "token": "DEAD", "name": "K",
                                  "did": None}, lambda m: None)
    check("【正对照】候选都不可用 → expired", st2 == "expired", st2)
    check("【正对照】不会无限重试（每枚只试一次）",
          len(seen) <= 3, seen)
finally:
    TC._post = _real_post
    TC._token_candidates = _real_cands
    TC._load_cache = _real_load


# ============================================================ E. 页面取令牌
print("\n== E. tw_checkin 从页面取令牌 / 设备标识 ==")


class FakePage:
    def __init__(self, store=None, boom=False):
        self.store = dict(store or {})
        self.boom = boom
        self.shots = []

    def evaluate(self, js, *args):
        if self.boom:
            raise RuntimeError("evaluate boom")
        if js == TW._JS_LOCAL_TOKEN:
            return self.store.get("Cloud-IDE-Token")
        if js == TW._JS_LOCAL_DID:
            return self.store.get("SOLO_DEVICE_ID")
        if js == TW._JS_FETCH_TOKEN:
            return self.store.get("_fetched")
        if "innerText" in str(js):
            return self.store.get("_text", "")
        return None

    def screenshot(self, **kw):
        self.shots.append(kw.get("path"))

    def close(self):
        pass


check("TOKEN_RE 认得出真实格式的 JWT", bool(TW.TOKEN_RE.match(JWT)))
check("TOKEN_RE 不会把普通串当令牌",
      not TW.TOKEN_RE.match("not-a-token"))
check("localStorage 里有令牌 → 直接取到",
      TW._page_token(FakePage({"Cloud-IDE-Token": JWT})) == JWT)
check("localStorage 里没有 → 回退问 GetUserToken",
      TW._page_token(FakePage({"_fetched": JWT})) == JWT)
check("两处都没有 → None",
      TW._page_token(FakePage({})) is None)
check("页面 evaluate 抛异常时安全返回 None",
      TW._page_token(FakePage(boom=True)) is None)
check("设备标识取自 SOLO_DEVICE_ID",
      TW._page_device_id([FakePage({"SOLO_DEVICE_ID": "1788951886854732763"})])
      == "1788951886854732763")
check("非数字设备标识被拒绝（不传脏值给服务端）",
      TW._page_device_id([FakePage({"SOLO_DEVICE_ID": "abc"})]) != "abc")


# ============================================================ F. 接口判定的降级
print("\n== F. _try_api_checkin：不可用时必须返回 None 交给页面兜底 ==")
_real_post = TC._post
try:
    TC._post, _ = _mk_post({"checked_in": True, "code": 0, "credits": 150,
                            "enable": True})
    r = TW._try_api_checkin([FakePage({})], lambda m: None, "Klein", "t")
    check("页面里没有令牌 → None（降级）", r is None, r)

    r2 = TW._try_api_checkin([FakePage({"Cloud-IDE-Token": JWT})],
                             lambda m: None, "Klein", "t")
    check("有令牌 → 返回接口结论", r2 and r2[0] == "skip", r2)

    # 接口抛异常也不能炸，要降级
    _real2 = TC.checkin_by_token
    try:
        def _boom(*a, **k):
            raise RuntimeError("api down")
        TC.checkin_by_token = _boom
        r3 = TW._try_api_checkin([FakePage({"Cloud-IDE-Token": JWT})],
                                 lambda m: None, "Klein", "t")
        check("接口调用异常 → None（降级，不炸）", r3 is None, r3)
    finally:
        TC.checkin_by_token = _real2
finally:
    TC._post = _real_post


# ============================================================ G. 接口优先的顺序
print("\n== G. _do_checkin：接口路径**最先**跑，失败才回落到页面判据 ==")
_real_api = TW._api_via_token_page
_real_click = TW._try_click_sign
_real_dash = TW._open_points_dashboard
try:
    TW._api_via_token_page = lambda ctx, log, name, tag: (
        "skip", "今日已签到（接口确认，+150积分）")

    def _no_click(*a, **k):
        raise AssertionError("接口已成功，不该再走页面点击路径")

    TW._try_click_sign = _no_click
    TW._open_points_dashboard = _no_click
    _pg = FakePage({})
    st, d = TW._do_checkin(None, _pg, lambda m: None, "Klein",
                           TMP, "2026-09-18")
    check("接口说已签 → 直接返回 ok/skip，不再碰页面",
          st == "skip" and "接口确认" in d, (st, d))
    check("接口成功时留下了截图（接口判定的证据）",
          any(p and "api_ok" in str(p) for p in _pg.shots), _pg.shots)

    # 正对照：接口不可用时必须回到页面路径
    TW._api_via_token_page = lambda ctx, log, name, tag: None
    TW._try_click_sign = lambda page, log, name, shots, tag: "done"
    st2, d2 = TW._do_checkin(None, FakePage({}), lambda m: None, "Klein",
                             TMP, "2026-09-18")
    check("【正对照】接口不可用 → 页面路径仍然生效",
          st2 == "ok" and "页面" in d2, (st2, d2))
finally:
    TW._api_via_token_page = _real_api
    TW._try_click_sign = _real_click
    TW._open_points_dashboard = _real_dash


# ============================================================ H. run 写 state
print("\n== H. tw_checkin.run：接口确认已签也要写 state（否则每轮都重开浏览器）==")


class _FakePW:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __getattr__(self, k):
        return self


import playwright.sync_api as _pwapi  # noqa: E402

_real_sync = _pwapi.sync_playwright
_real_run_acct = TW._run_account
try:
    _pwapi.sync_playwright = lambda: _FakePW()
    cfg = {"traework": {"accounts": [{"name": "trae1"}]}}

    TW._run_account = lambda *a, **k: ("skip", "今日已签到（接口确认，+150积分）")
    state = {}
    res = TW.run(lambda m: None, state, cfg, "2026-09-18")
    check("skip(接口确认已签) → 写入 state",
          state.get("traework:trae1") == "2026-09-18", state)
    check("结果行状态为 skip", res and res[0]["status"] == "skip", res)

    TW._run_account = lambda *a, **k: ("skip", "签到活动未开启（enable=false）")
    state2 = {}
    TW.run(lambda m: None, state2, cfg, "2026-09-18")
    check("【正对照】活动未开启不该写 state",
          "traework:trae1" not in state2, state2)

    TW._run_account = lambda *a, **k: ("ok", "签到成功（接口确认）")
    state3 = {}
    TW.run(lambda m: None, state3, cfg, "2026-09-18")
    check("ok 仍然写 state", state3.get("traework:trae1") == "2026-09-18",
          state3)
finally:
    _pwapi.sync_playwright = _real_sync
    TW._run_account = _real_run_acct


# ============================================================ I. JS 语法
print("\n== I. 新增 JS 片段必须语法合法（且不含 Python 正则插值）==")


def _find_node():
    for cand in (r"C:\Program Files\nodejs\node.exe",
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs\node.exe")):
        if os.path.exists(cand):
            return cand
    for p in (os.environ.get("PATH") or "").split(os.pathsep):
        c = os.path.join(p, "node.exe")
        if os.path.exists(c):
            return c
    return None


node = _find_node()
SNIPPETS = [("_JS_LOCAL_TOKEN", TW._JS_LOCAL_TOKEN),
            ("_JS_LOCAL_DID", TW._JS_LOCAL_DID),
            ("_JS_FETCH_TOKEN", TW._JS_FETCH_TOKEN)]
for nm, src in SNIPPETS:
    check(f"{nm} 里没有 Python 正则插值残留（% 或 SIGN_TEXT_RE）",
          "%" not in src and "SIGN_TEXT_RE" not in src, src[:60])
if node:
    for nm, src in SNIPPETS:
        path = os.path.join(TMP, f"{nm}.js")
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("const _ = " + src + ";\n")
        rc = subprocess.run([node, "--check", path], capture_output=True).returncode
        check(f"{nm} 通过 `node --check`", rc == 0, f"rc={rc}")
    # 正对照：故意造一个含正则插值的 JS，必须报错（否则校验是死的）
    bad = os.path.join(TMP, "bad.js")
    with io.open(bad, "w", encoding="utf-8") as f:
        f.write("const _ = () => { if ((^(每日|立即)?签到$).test('x')) {} };\n")
    rc_bad = subprocess.run([node, "--check", bad],
                            capture_output=True).returncode
    check("【正对照】含正则插值的 JS 必须被 node 判为语法错误",
          rc_bad != 0, f"rc={rc_bad}")
else:
    print("  SKIP 未找到 node，跳过 JS 语法校验")

# ============================================================ J. 生产数据未被污染
print("\n== J. 生产数据未被污染（回归：曾往真实令牌缓存写入假 uid=42）==")
TC.TOKENS_PATH = _REAL_TOKENS_PATH
_PROD_AFTER = _fingerprint(_PROD_FILES)
for _p in _PROD_FILES:
    _name = os.path.basename(_p)
    check(f"未改动 logs/{_name}",
          _PROD_AFTER[_p] == _PROD_BEFORE[_p],
          f"{_PROD_BEFORE[_p]} -> {_PROD_AFTER[_p]}")
check("测试期间真的改道到了沙箱（生产路径 ≠ 沙箱路径）",
      _SANDBOX_TOKENS_PATH != _REAL_TOKENS_PATH
      and os.path.dirname(_SANDBOX_TOKENS_PATH) == TMP,
      f"sandbox={_SANDBOX_TOKENS_PATH} real={_REAL_TOKENS_PATH}")

print()
print(f"结果：{ok} OK / {fail} FAIL")
sys.exit(0 if fail == 0 else 1)
