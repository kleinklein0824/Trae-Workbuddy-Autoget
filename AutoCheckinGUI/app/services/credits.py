#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""积分 / 额度查询服务。

各平台口径（均沿用参考项目 Trae-workbuddyAssistant 已验证的接口）：
  * WorkBuddy：POST /v2/billing/meter/get-user-resource（旧接口，兼容性最好）
  * TraeCN   ：POST api.trae.cn 的 ug 相关额度接口（Cloud-IDE-JWT 鉴权）
  * TraeWork ：浏览器会话内的积分页（成本高，GUI 中按需触发）

查询结果统一为 []{platform, id, name, ok, totalRemaining, error}，便于看板复用。
"""
import json
import time
import urllib.error
import urllib.request

from .. import config as C

WB_API = "https://copilot.tencent.com"
WB_RESOURCE = WB_API + "/v2/billing/meter/get-user-resource"
WB_USER_RESOURCE_ALT = "/v2/billing/meter/get-user-resource-summary"


def _pick_num(obj, keys):
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            v = obj[k]
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                try:
                    return float(v.strip())
                except ValueError:
                    continue
    return None


def _deep_total(obj, keys, depth=0):
    """在嵌套结构里找第一个匹配的数值字段（原项目解析器同样是宽容扫描）。"""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        got = _pick_num(obj, keys)
        if got is not None:
            return got
        for v in obj.values():
            got = _deep_total(v, keys, depth + 1)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _deep_total(v, keys, depth + 1)
            if got is not None:
                return got
    return None


def _deep_sum(obj, key_re, depth=0):
    """递归把所有 key 名匹配的数值加总（Accounts 明细累加用）。"""
    if depth > 6:
        return []
    vals = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if key_re.lower() in k.lower() and isinstance(v, (int, float)):
                vals.append(float(v))
            else:
                vals += _deep_sum(v, key_re, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            vals += _deep_sum(v, key_re, depth + 1)
    return vals


# ---------------- WorkBuddy ----------------

def _wb_headers(acc):
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {acc['token']}",
        "X-User-Id": acc["uid"],
    }
    if acc.get("domain"):
        h["X-Domain"] = acc["domain"]
    if acc.get("enterprise_id"):
        h["X-Enterprise-Id"] = acc["enterprise_id"]
        h["X-Tenant-Id"] = acc["enterprise_id"]
    return h


def _wb_remaining(acc):
    """查询单个 WorkBuddy 账号的剩余额度。返回 (ok, value, error)。"""
    body = json.dumps({"ProductCode": "p_tcaca", "productCode": "p_tcaca"}).encode()
    req = urllib.request.Request(WB_RESOURCE, data=body, method="POST",
                                 headers=_wb_headers(acc))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, None, "令牌过期，请重新 capture"
        return False, None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, None, f"网络异常: {type(e).__name__}"

    if data.get("code") not in (0, None):
        # code 非 0 时仍尝试解析 data，解析不到再报错
        pass
    # 优先取明确的总剩余字段
    total = _deep_total(data, ["totalRemaining", "TotalRemaining", "total_remaining",
                               "remainAmount", "Remaining", "remaining"])
    if total is None:
        # 回退：把所有 *Remain*/Balance 明细累加
        vals = _deep_sum(data, "remain") + _deep_sum(data, "balance")
        total = sum(vals) if vals else None
    if total is None:
        total = _deep_total(data, ["quota", "Quota", "amount", "Amount"])
    if total is None:
        msg = data.get("msg") or data.get("message") or "接口未返回额度字段"
        return False, None, str(msg)[:80]
    return True, total, ""


def query_workbuddy(cfg=None):
    """查询所有 WorkBuddy 账号剩余额度。"""
    cfg = cfg or C.load_config()
    accounts = (C.peek_accounts() or {}).get("workbuddy")
    if accounts is None:
        accounts = C.list_workbuddy_accounts(cfg)
    out = []
    for acc in accounts:
        ok, val, err = _wb_remaining({
            "uid": acc["uid"], "token": acc["token"],
        })
        out.append({
            "platform": "workbuddy", "id": acc["uid"], "name": acc["display"],
            "ok": ok, "totalRemaining": val, "error": err,
        })
    return out


# ---------------- TraeCN ----------------

TC_CREDITS = "https://api.trae.cn/trae/api/v2/ug/credits/status"
TC_APIV3 = "https://api.trae.cn/cloudide/api/v3/trae/GetUserInfo"


def query_traecn(cfg=None):
    cfg = cfg or C.load_config()
    accounts = (C.peek_accounts() or {}).get("traecn")
    if accounts is None:
        accounts = C.list_traecn_accounts(cfg)
    out = []
    for acc in accounts:
        ok, val, err = _tc_remaining(acc)
        out.append({
            "platform": "traecn", "id": acc["uid"], "name": acc["display"],
            "ok": ok, "totalRemaining": val, "error": err,
        })
    return out


def _tc_remaining(acc):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Cloud-IDE-JWT {acc['token']}",
        "x-app-version": "3.3.99",
    }
    if acc.get("has_did"):
        headers["x-device-id"] = "0"
    for path in (TC_CREDITS, "/trae/api/v2/ug/checkin_credits/status"):
        req = urllib.request.Request(
            f"https://api.trae.cn{path}", data=json.dumps({"req_source": 1}).encode(),
            method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return False, None, "令牌失效，请打开一次 Trae CN IDE"
            continue
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        total = _deep_total(data, ["remaining_credits", "remainingCredits",
                                   "total_remaining", "totalRemaining",
                                   "credits", "balance", "total"])
        if total is not None:
            return True, total, ""
    return False, None, "接口未返回额度（该平台可能未开放查询）"


def query_all(cfg=None):
    """并发查询三平台（TraeWork 走无接口，返回提示项）。"""
    res = []
    for fn in (query_workbuddy, query_traecn):
        try:
            res += fn(cfg)
        except Exception as e:  # noqa: BLE001
            res.append({"platform": "?", "id": "-", "name": "-", "ok": False,
                        "totalRemaining": None, "error": repr(e)})
    return res


def format_num(v):
    """数字格式化：千分位，整数不带小数点。"""
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f - round(f)) < 1e-9:
        return f"{int(round(f)):,}"
    return f"{f:,.1f}"
