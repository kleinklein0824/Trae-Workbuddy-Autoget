#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trae CN（IDE）每日签到自动化。

原理：与 wb_checkin 相同——本地令牌 + 官方接口直调。
- 签到接口：POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/status|claim
- 鉴权头：  Authorization: Cloud-IDE-JWT <JWT>
- 令牌来源：Trae CN IDE 会话日志（ai-code-completion 扩展会话内会写入当前账号
  的 JWT）；IDE 每次启动登录都会刷新，因此只需定期打开一次 IDE 即可持续续期。
  发现的新令牌会缓存到 logs/tokens_traecn.json，IDE 长期未开时回退使用缓存。
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.trae.cn/trae/api/v2/ug/checkin_credits"
USERINFO_URL = "https://api.trae.cn/cloudide/api/v3/trae/GetUserInfo"
APP_VERSION = "3.3.99"
JWT_RE = re.compile(rb"eyJhbGciOi[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def _appdata_roots():
    """`%APPDATA%` 候选目录（环境变量缺失时兜底 `~/AppData/Roaming`）。

    见 `runtime.win_user_dirs` 的说明：环境变量不存在时 `expandvars` 会把
    `%APPDATA%` 原样留下，拼出的路径永远不存在 → **静默 0 个账号**。
    """
    try:
        from runtime import win_user_dirs
        return win_user_dirs("roaming")
    except Exception:  # noqa: BLE001   （runtime 不可用时退回单一路径）
        val = os.environ.get("APPDATA")
        return [val] if val else []


LOG_ROOT = os.path.join((_appdata_roots() or [""])[0], "Trae CN", "logs")

# Trae 系客户端日志根目录（按优先级）：
#   Trae CN              —— IDE，日志里有明文 JWT（主要令牌来源）
#   TRAE SOLO CN         —— TraeWork CN 桌面端，日志里 token 已脱敏，
#                           但含 ScreenName / UserID，可用于「识别本机登录了谁」
#   两者共用 api.trae.cn 账号体系与 checkin_credits 签到接口，
#   因此同属 traecn 签到范畴，扫描时一并纳入。
_CLIENT_LOG_ROOTS = [
    ("Trae CN", "IDE"),
    ("TRAE SOLO CN", "桌面端"),
    ("TraeWork CN", "桌面端"),
]

# 桌面端日志里的用户身份（getUserInfo / updateUserInfo 的响应体）
_SCREEN_NAME_RE = re.compile(rb'"ScreenName"\s*:\s*"([^"]{1,64})"')
_USER_ID_RE = re.compile(rb'"UserID"\s*:\s*"(\d{6,24})"')
_UPDATE_UID_RE = re.compile(rb'"userId"\s*:\s*"(\d{6,24})"')
_UPDATE_NAME_RE = re.compile(rb'"username"\s*:\s*"([^"]{1,64})"')
_LEGACY_ROOT = os.path.dirname(os.path.abspath(__file__))


def _data_file(*parts):
    """可写数据文件的落点：走 `runtime.data_root()`，并接管旧位置的同名文件。

    ★ 同上（2026-09-22 修）：原先用 `dirname(__file__)`，**冻结后那是
      PyInstaller 的 bundle 目录**（onedir 是 `_internal/`）→ 令牌缓存与
      **设备码 `did`** 被写进 `_internal/`，与 GUI 侧的 `C.LOG_DIR` 不是同一处，
      且升级时随 `_internal/` 一起被替换掉（设备码静默重生成）。
    """
    try:
        import runtime
        return runtime.data_file(*parts, legacy_base=_LEGACY_ROOT)
    except Exception:  # noqa: BLE001
        return os.path.join(_LEGACY_ROOT, *parts)


TOKENS_PATH = _data_file("logs", "tokens_traecn.json")
DEVICE_PATH = _data_file("logs", "traecn_device.json")
DID_RE = re.compile(rb"[?&]did=(\d{10,20})")


def _b64d(s):
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _token_payload(token):
    """解码 JWT 载荷，返回 {uid, exp} 或 None。"""
    try:
        payload = json.loads(_b64d(token.split(".")[1]))
        d = payload.get("data", payload)
        uid = str(d.get("id") or "")
        exp = int(payload.get("exp") or 0)
        if uid and exp > 0:
            return {"uid": uid, "exp": exp}
    except Exception:  # noqa: BLE001
        pass
    return None


def _scan_ide_logs(log):
    """扫描所有 Trae 系客户端会话日志中的 JWT（最新会话优先）。

    覆盖 Trae CN IDE 与 TraeWork CN 桌面端等全部客户端目录：
    只要任一客户端里出现过有效明文 JWT，都能被采集到，
    避免用户「装了新版客户端就扫不到账号」。

    返回 {uid: (token, exp, saved_at)}。

    ★ 为什么多一个 `saved_at`（2026-09-18 新增）
    ------------------------------------------
    JWT 里的 `exp` 只是**客户端侧的过期时间**，服务端随时可以提前吊销。
    实测：缓存里 `exp` 还剩 7 天的 TraeCN 令牌已被服务端拒绝
    （status 返回 code=1001 "not able to authenticate you"），
    而同一天从 TraeWork 页面取到的新令牌可用 —— 新令牌的 `exp` 反而更早。
    因此**只比 `exp` 会让"看起来更长寿"的失效令牌压过真正可用的新令牌**，
    必须按「发现时间」优先（`saved_at` = 日志文件 mtime + 文件内偏移，
    越靠后越新），`exp` 只做同时间的次级比较。
    """
    best = {}
    roots = client_log_roots()
    if not roots:
        # 极端情况（APPDATA 缺失）下仍尝试旧路径，保持向后兼容
        roots = [(LOG_ROOT, "Trae CN", "IDE")]
    for root, client, _kind in roots:
        p = pathlib_path(root)
        if not p.exists():
            continue
        sessions = [d for d in p.iterdir() if d.is_dir()]
        sessions.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        for sd in sessions[:8]:
            for f in sd.rglob("*.log"):
                try:
                    if f.stat().st_size > 80_000_000:
                        continue
                    mtime = f.stat().st_mtime
                    text = f.read_bytes()
                except OSError:
                    continue
                for m in JWT_RE.finditer(text):
                    tok = m.group(0).decode("ascii", "ignore")
                    info = _token_payload(tok)
                    if not info or info["exp"] < time.time():
                        continue
                    # 同一文件里越靠后的出现越新 → 用偏移做次级排序键
                    saved_at = mtime + m.start() * 1e-9
                    cur = best.get(info["uid"])
                    if not cur or (saved_at, info["exp"]) > (cur[2], cur[1]):
                        best[info["uid"]] = (tok, info["exp"], saved_at)
                        if log:
                            pass
    return best


def pathlib_path(p):
    import pathlib
    return pathlib.Path(p)


def client_log_roots(appdata=None):
    """枚举本机所有 Trae 系客户端的日志根目录，返回 [(路径, 客户端名, 类型)]。

    只返回实际存在 logs 子目录的项，避免无谓探测。
    不传 `appdata` 时用 `_appdata_roots()` 的**全部候选**（含环境变量缺失时的
    兜底目录）；显式传入时只用传入的那个（测试/自检会这么用）。
    """
    bases = [appdata] if appdata is not None else _appdata_roots()
    out, seen = [], set()
    for base in bases:
        if not base:
            continue
        for name, kind in _CLIENT_LOG_ROOTS:
            root = os.path.join(base, name, "logs")
            key = os.path.normcase(os.path.normpath(root))
            if key in seen or not os.path.isdir(root):
                continue
            seen.add(key)
            out.append((root, name, kind))
    return out


def scan_client_identity(log=None, appdata=None):
    """从 Trae 系客户端日志中提取「本机登录的账号」身份（uid → 昵称）。

    为什么需要它：TraeWork CN 桌面端（TRAE SOLO CN）的日志里 **没有明文 JWT**
    （token 字段被脱敏为 ******），所以拿不到可签到的令牌；但它的
    getUserInfo / updateUserInfo 响应里带有 ScreenName 与 UserID，
    足以确认「本机这个客户端当前登录的是谁」。

    返回 {uid: {"name": str, "client": str, "kind": str, "has_token": bool,
                "token_client": str, "token_kind": str}}。
    has_token 表示该客户端日志中是否发现了可用明文 JWT（IDE 通常为 True）；
    ★ 另给 ``token_client`` / ``token_kind`` —— **真正提供令牌的那个客户端**。

    ★ 2026-09-18 修的归因 bug
    -------------------------
    原先 `result` 是按 uid 跨客户端聚合的，但 `client` / `kind` 会被**后扫到的**
    客户端无条件覆盖，而 `has_token` 却是「或」累积的。本机实测后果：

        roots 顺序是 Trae CN(IDE) → TRAE SOLO CN(桌面端)
        Trae CN 先扫到 uid=…058 且 has_jwt=True  → has_token=True
        接着 TRAE SOLO CN 扫到同一个 uid        → client 被改写成「TRAE SOLO CN」
        最终输出 {'client': 'TRAE SOLO CN', 'kind': '桌面端', 'has_token': True}

    于是界面上出现「TRAE SOLO CN（桌面端）· 签到由 TraeCN 通道生效」——
    可实测该桌面端日志里明文 JWT 命中 **0 枚**（token 字段是 ``"******"``），
    磁盘上也扫不到明文令牌。**纯属把 IDE 的功劳记到了桌面端头上**。
    现在：只有**真的扫到令牌**的客户端才认领 client/kind。
    """
    result = {}
    for root, client, kind in client_log_roots(appdata):
        try:
            sessions = sorted((d for d in pathlib_path(root).iterdir() if d.is_dir()),
                              key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        for sd in sessions[:6]:
            for f in sd.rglob("*.log"):
                try:
                    if f.stat().st_size > 40_000_000:
                        continue
                    blob = f.read_bytes()
                except OSError:
                    continue
                has_jwt = bool(JWT_RE.search(blob))
                # 一条日志里可能同时出现多次，取最后一次（最新状态）
                uids = _USER_ID_RE.findall(blob) + _UPDATE_UID_RE.findall(blob)
                names = _SCREEN_NAME_RE.findall(blob)
                # 脱敏 username 不可用，只在没有 ScreenName 时退回
                plain_names = [n for n in names if n != b"******"]
                for uid_b in uids:
                    uid = uid_b.decode("ascii", "ignore")
                    if not uid or uid == "0":
                        continue
                    name = plain_names[-1].decode("utf-8", "ignore") if plain_names else ""
                    ent = result.get(uid) or {"name": "", "client": client,
                                              "kind": kind, "has_token": False,
                                              "token_client": "", "token_kind": ""}
                    if name:
                        ent["name"] = name
                    if has_jwt:
                        # ★ 只有带令牌的客户端才认领身份标注
                        ent["has_token"] = True
                        ent["token_client"] = client
                        ent["token_kind"] = kind
                        ent["client"] = client
                        ent["kind"] = kind
                    elif not ent["has_token"]:
                        # 无令牌的客户端只在「目前还没人认领」时补上，绝不覆盖
                        ent["client"] = client
                        ent["kind"] = kind
                    result[uid] = ent
                if plain_names and not uids:
                    # 只有昵称没有 uid 的场合（少见），无法入列，忽略
                    pass
    if log and result:
        desc = "、".join(f"{v.get('name') or uid[-4:]}（{v['client']}）"
                        for uid, v in result.items())
        log(f"[traecn] 本机客户端登录态：{desc}")
    return result


def _fetch_username(token):
    """调用 IDE 同款 GetUserInfo 接口反查真实昵称（失败返回 None）。"""
    req = urllib.request.Request(
        USERINFO_URL,
        data=json.dumps({"ReqSource": "IDE", "IDEVersion": APP_VERSION}).encode(),
        method="POST",
        headers={"Content-Type": "application/json",
                 "x-cloudide-token": token,
                 "x-app-version": APP_VERSION})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8", "ignore"))
        res = body.get("Result") or {}
        return res.get("ScreenName") or res.get("NonPlainTextMobile") or None
    except Exception:  # noqa: BLE001
        return None


def _load_cache():
    try:
        with open(TOKENS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(cache):
    try:
        os.makedirs(os.path.dirname(TOKENS_PATH), exist_ok=True)
        with open(TOKENS_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _entry_freshness(ent):
    """候选令牌的「发现时间」。旧版缓存里没有这个字段 → 按 0 处理（最旧）。"""
    try:
        return float(ent.get("saved_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def _better_token(cand, cur):
    """cand 是否比 cur 更该被采用：**先比发现时间，再比 exp**。

    只比 `exp` 会让「服务端已吊销、但 exp 更长」的旧令牌压过当天新取的
    可用令牌（2026-09-18 实测的真实事故，详见 `_scan_ide_logs`）。
    """
    if cur is None:
        return True
    return ((_entry_freshness(cand), int(cand.get("exp") or 0))
            > (_entry_freshness(cur), int(cur.get("exp") or 0)))


def record_token(uid, token, exp, name=None, log=None):
    """把从**活页面**取到的令牌写进共享缓存，并盖上发现时间。

    为什么需要：TraeWork 的浏览器会话里能拿到与 TraeCN 完全同款的
    `Cloud-IDE-JWT`（同一个 api.trae.cn、同一个 uid）。把它回写到这里，
    即使本机没开 IDE、或 IDE 日志里只剩被服务端吊销的旧令牌，
    TraeCN 也能用上这个**新鲜且可用**的令牌 —— 这正是 2026-09-18
    「traecn 状态查询异常」的真实成因（缓存令牌被吊销）。
    """
    if not uid or not token:
        return None
    cache = _load_cache()
    old = cache.get(uid) or {}
    cache[uid] = {"token": token, "exp": int(exp or 0),
                  "saved_at": time.time(),
                  "name": name or old.get("name") or ""}
    _save_cache(cache)
    if log:
        log(f"[traecn] 已用 TraeWork 会话里的令牌刷新账号缓存"
            f"（uid={uid[-4:]}，有效期至 "
            f"{datetime.fromtimestamp(int(exp or 0)).strftime('%m-%d %H:%M')}）")
    return cache[uid]


def collect_accounts(cfg=None, log=print):
    """汇总可用账号：IDE 日志令牌与本地缓存取**最新发现的**一个；昵称走接口反查并缓存。

    合并规则（2026-09-18 起）：**按发现时间优先**，`exp` 只做次级比较 ——
    理由见 `_scan_ide_logs` 与 `_better_token`。
    """
    accounts = []
    fresh = _scan_ide_logs(log)
    cache = _load_cache()
    merged = {}
    for uid, (tok, exp, saved_at) in fresh.items():
        ent = {"token": tok, "exp": exp, "saved_at": saved_at}
        # 新令牌合并时保留已缓存的昵称（否则会退回 TraeCN-xxxx 显示）
        if cache.get(uid, {}).get("name"):
            ent["name"] = cache[uid]["name"]
        if _better_token(ent, merged.get(uid)):
            merged[uid] = ent
    for uid, ent in cache.items():
        back = {"token": ent.get("token", ""), "exp": int(ent.get("exp") or 0),
                "saved_at": _entry_freshness(ent)}
        if ent.get("name"):
            back["name"] = ent["name"]
        if _better_token(back, merged.get(uid)):
            merged[uid] = back
    # 反查真实昵称：已缓存的直接用，没有的调接口（失败则下次再试）
    # 另外：本机客户端日志里若已写明 ScreenName（如 TraeWork CN 桌面端），
    # 优先直接采用，省掉一次网络反查，也让「扫不到 IDE 令牌」的用户至少能看到账号名。
    try:
        identities = scan_client_identity()
    except Exception:  # noqa: BLE001
        identities = {}
    changed = merged != cache
    now = time.time()
    for uid, ent in merged.items():
        if ent["exp"] <= now or not ent["token"]:
            continue
        name = cache.get(uid, {}).get("name")
        if not name and identities.get(uid, {}).get("name"):
            name = identities[uid]["name"]
            ent["name"] = name
            changed = True
            log(f"[traecn] 从本机客户端识别到账号昵称：{name}（uid={uid[-4:]}）")
        if not name:
            name = _fetch_username(ent["token"])
            if name:
                ent["name"] = name
                changed = True
                log(f"[traecn] 已解析账号昵称：{name}（uid={uid[-4:]}）")
    if changed:
        _save_cache(merged)
    for uid, ent in sorted(merged.items()):
        if ent["exp"] > now and ent["token"]:
            accounts.append({"uid": uid, "token": ent["token"], "exp": ent["exp"],
                             "did": _device_id(log),
                             "name": ent.get("name") or f"TraeCN-{uid[-4:]}"})
    return accounts


def _device_id(log=print):
    """取稳定的设备注册 ID（claim 必需，缺失时 claim 返回 code=9004）。

    来源：IDE 启动日志 config/query URL 的 did= 参数（跨会话稳定）。
    首次从日志提取后缓存到 logs/traecn_device.json。
    """
    try:
        with open(DEVICE_PATH, encoding="utf-8") as f:
            did = json.load(f).get("device_id")
        if did:
            return did
    except Exception:  # noqa: BLE001
        pass
    did = None
    try:
        root = pathlib_path(LOG_ROOT)
        if root.exists():
            sessions = sorted((d for d in root.iterdir() if d.is_dir()),
                              key=lambda p: p.name, reverse=True)
            for sd in sessions[:6]:
                mf = sd / "main.log"
                if not mf.exists():
                    continue
                m = DID_RE.search(mf.read_bytes())
                if m:
                    did = m.group(1).decode("ascii")
                    break
    except OSError:
        pass
    if did:
        try:
            os.makedirs(os.path.dirname(DEVICE_PATH), exist_ok=True)
            with open(DEVICE_PATH, "w", encoding="utf-8") as f:
                json.dump({"device_id": did}, f)
            log(f"[traecn] 已缓存设备 ID：{did}")
        except OSError:
            pass
    return did


def _expiry_warning(acc):
    days_left = (acc.get("exp", 0) - time.time()) / 86400
    if 0 < days_left <= 7:
        return f"；⚠令牌约{max(1, round(days_left))}天后过期，打开一次 Trae CN IDE 即可续期"
    return ""


def _post(path, token, log, did=None, retries=2):
    """调用签到接口，返回 (分类, 数据/消息)。分类: ok/net/expired/protocol。

    claim 必须携带 x-device-id（IDE 同款设备头），否则返回 code=9004。
    网络/网关类抖动自动重试（共 retries+1 次尝试），401/403 令牌问题不重试。
    """
    headers = {"Content-Type": "application/json",
               "Authorization": f"Cloud-IDE-JWT {token}",
               "x-app-version": APP_VERSION}
    if did:
        headers["x-device-id"] = did
    req = urllib.request.Request(
        f"{BASE_URL}/{path}", data=json.dumps({"req_source": 1}).encode(),
        method="POST", headers=headers)
    last = "unknown"
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.loads(r.read().decode("utf-8", "ignore"))
            return ("ok", body)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return ("expired", f"HTTP {e.code}，令牌失效")
            last = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        if attempt < retries:
            log(f"[traecn] 请求抖动，{3}s 后重试({attempt + 1}/{retries})：{str(last)[:80]}")
            time.sleep(3)
    return ("net", last)


def _checkin_one(acc, log):
    """单个账号走一轮「查状态 → 需要就领取」（保留原签名，供 runner 调用）。"""
    return checkin_by_token(acc["token"], acc.get("did"), log,
                            warn=_expiry_warning(acc), name=acc.get("name"))


# status 返回 code=1001 时的服务端文案形如
#   "We're sorry, but we are not able to authenticate you. ..."
# 这是**令牌被服务端吊销**，不是「今天没签成」。旧代码把它塞进
# 「状态查询异常」这种笼统失败里 —— 用户看到只会一头雾水，而它其实有一条
# 明确的自救路径：打开一次 IDE / TraeWork 页面就能换到新令牌。
# 2026-09-18 实测：缓存里 exp 还剩 7 天的令牌就是这样被拒的。
_AUTH_FAIL_RE = re.compile(r"not able to authenticate|unauthenticated|"
                           r"token\s*(?:is\s*)?(?:invalid|expired)|登录已过期", re.I)


def _auth_failed(body, by_code=True):
    """响应是否表示「令牌不被服务端接受」。

    `by_code=True` 时把 code==1001 也算上（**仅适用于 status 接口**）；
    claim 接口的 1001 是「今日已完成」的幂等返回，语义完全不同，
    所以 claim 侧必须传 by_code=False，只看文案。
    """
    if not isinstance(body, dict):
        return False
    if by_code and body.get("code") == 1001:
        return True
    return bool(_AUTH_FAIL_RE.search(str(body.get("message") or "")))


_TOKEN_REVOKED = ("令牌已被服务端吊销；打开一次 Trae CN IDE 或 TraeWork 页面"
                  "（work.trae.cn）登录即可自动换到新令牌")

# ★ 服务端「太忙 / 稍后再试」类响应 = **待复核**，不是失败。
# 2026-09-19 实测（可见事故）：TraeWork 换到新令牌后 claim 连续两次返回
#   {'code': 9074, 'message': '当前参与用户太多，请稍后再试'}
# 旧代码把它落进兜底的 `return "fail"` → 弹窗报「TraeWork 失败」，
# 而 **2 分钟后的下一轮复核就确认 +150 积分其实早已到账**
# （服务端已受理，只是回了一个忙响应）。
# 归成 busy 之后：仍**不计入成功**（补跑会继续、不会提前收工），
# 但弹窗/CSV 显示琥珀色「待复核」而不是红灯 + 错误归因。
_BUSY_CODES = frozenset((9074, 429))
_BUSY_RE = re.compile(r"参与用户太多|请稍后再试|too\s*many|try\s*again\s*later|"
                      r"rate\s*limit|HTTP\s*429", re.I)

# claim 被限流后**就地复核**的等待秒数。
# ★ 9074 是「太忙」，语义是**「没确认」而不是「没受理」**：
#   2026-09-19 与 09-20 连续两次实测，同一枚令牌报完 9074，过一会儿再查 status
#   就已经是「今日已签到（+150积分）」—— 也就是说那次 claim 服务端其实落账了。
# ⚠️ 但**落账有延迟**：09-20 那次 13:33:55 报限流，13:34:13 再查仍是「没签」，
#   一直到 13:38 才变成「已签」（明细里今天那条也才出现）。
#   所以这次复核只是**加分项**，不能当成保证 —— 真正的兜底是：
#   ① 判成 busy（待复核）而不是 fail；② 计划任务下一轮自动复核。
#   等这一下能救回「几秒内就落账」的那部分，救不回的也有 ② 接着。
# ⚠️ 测试里必须把它改成 0，否则每个用例白等十几秒。
_BUSY_RECHECK_DELAY_S = 12


def _is_busy(body):
    """响应是否表示「服务端限流 / 太忙，请稍后再试」。

    接受两种入参：接口 JSON（看 `code` 与 `message`）或 `_post` 的错误串
    （HTTP 429 会走 `("net", "HTTP 429")` 这条回程）。

    ⚠️ 判据必须只覆盖「忙」这一类：status 的 1001（已吊销）与 claim 的 1001
    （今日已完成幂等）语义完全不同，绝不能被吸进这里。
    """
    if isinstance(body, dict):
        if body.get("code") in _BUSY_CODES:
            return True
        return bool(_BUSY_RE.search(str(body.get("message") or "")))
    return bool(_BUSY_RE.search(str(body)))


def checkin_by_token(token, did, log, warn="", record=False, name=None):
    """用一枚 Cloud-IDE-JWT 走完「查状态 → 需要就领取」，返回 (status, detail)。

    这是 **TraeCN 与 TraeWork 共用**的接口判定实现：两者同属 api.trae.cn、
    同一套账号体系与同一个签到活动（2026-09-18 实测两者 uid 完全相同）。
    status ∈ ok / skip / busy / fail / expired。

    `busy` = 本轮**没能确认**签到（服务端限流 9074 / HTTP 429，或浏览器 profile
    被占用）—— 既不是成功也不是失败，下一轮补跑会自动复核。

    `record=True` 时，只要服务端认可这枚令牌（code==0），就把它回写进共享
    缓存（见 `record_token`）—— 这样另一条通道也能用上这枚**新鲜可用**的令牌。
    """
    st, body = _post("status", token, log, did)
    if st == "expired":
        return "expired", "令牌已失效（打开一次 Trae CN IDE 或 TraeWork 页面登录即可自动续期）"
    if st == "net":
        return "fail", f"网络异常：{body}"
    if st == "ok" and _auth_failed(body):
        # ★ 必须报 expired 而不是「状态查询异常」：这是可自救的令牌问题，
        #   不是签到失败。文案里带上服务端原话，便于事后定性。
        return "expired", (f"{_TOKEN_REVOKED}"
                           f"（code={body.get('code')}："
                           f"{str(body.get('message') or '')[:60]}）")
    if st != "ok" or not isinstance(body, dict) or body.get("code") != 0:
        return "fail", f"状态查询异常：{str(body)[:80]}"
    if record:
        info = _token_payload(token) or {}
        record_token(info.get("uid"), token, info.get("exp"), name=name, log=log)
    if not body.get("enable"):
        return "skip", "签到活动未开启（enable=false）"
    if body.get("checked_in"):
        detail = f"今日已签到（接口确认，+{body.get('credits', '?')}积分）"
        return "skip", detail + warn
    # 未签到 -> 领取
    st2, body2 = _post("claim", token, log, did)
    if st2 == "expired":
        return "expired", "令牌已失效（打开一次 Trae CN IDE 或 TraeWork 页面登录即可自动续期）"
    if st2 == "net":
        if _is_busy(body2):
            # HTTP 429 也会落到这条回程（`_post` 把非 401/403 的 HTTPError
            # 统一折成 ("net", "HTTP xxx")）。
            return "busy", (f"领取请求被服务端限流（{body2}）；"
                            "本轮视为未完成，稍后补跑会自动复核")
        return "fail", f"网络异常：{body2}"
    if st2 == "ok" and isinstance(body2, dict) and body2.get("code") == 0:
        credits = body2.get("credits") or body.get("credits") or "?"
        extra = body2.get("extra_credits") or body.get("extra_credits")
        msg = f"签到成功，领取 {credits} 积分" + (f" + 额外 {extra}" if extra else "")
        return "ok", msg + warn
    if _auth_failed(body2, by_code=False):
        return "expired", f"{_TOKEN_REVOKED}（领取时被拒）"
    code = body2.get("code") if isinstance(body2, dict) else "?"
    if code in (1, 1001):
        return "skip", "今日已完成（平台幂等返回）"
    if code == 9004:
        return "fail", ("领取失败(9004)：缺少设备标识，请打开一次 Trae CN IDE 让设备重新注册"
                        if not did else "领取失败(9004)：设备标识被服务端拒绝，请打开一次 Trae CN IDE")
    if _is_busy(body2):
        # ★ 必须排在兜底 fail **之前**。9074 这类「当前参与用户太多，请稍后再试」
        #   是限流不是失败：2026-09-19 实测它报出来的 2 分钟后复核即确认积分到账。
        #   报成 fail 会让弹窗说「TraeWork 失败」，用户以为白签了。
        msg = str(body2.get("message") or "")[:40] if isinstance(body2, dict) else ""
        code = body2.get("code") if isinstance(body2, dict) else "?"
        # ★ 再进一步：**就地复核一次 status**。限流的那次 claim 很可能已经落账，
        #   等十几秒再查一下就能直接给出「今日已签到」，把一次误导性弹窗消掉
        #   （2026-09-20 实测：首轮报限流→补跑才确认，中间白弹了一次「失败」）。
        time.sleep(_BUSY_RECHECK_DELAY_S)
        st3, body3 = _post("status", token, log, did)
        if (st3 == "ok" and isinstance(body3, dict) and body3.get("code") == 0
                and body3.get("checked_in")):
            return "skip", (f"今日已签到（接口确认，+{body3.get('credits', '?')}积分）；"
                            f"注：首次领取被服务端限流（code={code}），"
                            f"复核已确认到账" + warn)
        return "busy", (f"领取请求被服务端限流（code={code}"
                        + (f"：{msg}" if msg else "")
                        + "）；本轮视为未完成，稍后补跑会自动复核"
                        + "（服务端很可能已受理）")
    return "fail", f"领取失败：{str(body2)[:80]}"


def _token_candidates(uid, log=None):
    """某个 uid 的**全部**候选令牌，按发现时间从新到旧。

    用于「当前这枚被服务端拒了 → 换另一枚再试」的补救路径（见
    `checkin_account`）。只有失败时才走，所以这里重新扫一遍日志是可接受的。
    """
    out = []
    try:
        for u, (tok, exp, saved_at) in _scan_ide_logs(None).items():
            if u == uid:
                out.append({"token": tok, "exp": exp, "saved_at": saved_at})
    except Exception:  # noqa: BLE001
        pass
    ent = _load_cache().get(uid)
    if ent and ent.get("token"):
        out.append({"token": ent["token"], "exp": int(ent.get("exp") or 0),
                    "saved_at": _entry_freshness(ent)})
    out.sort(key=lambda e: (e["saved_at"], e["exp"]), reverse=True)
    return out


def _rescue_tokens(uid, used_token, log=None):
    """除 used_token 之外、还值得一试的候选令牌（去重）。"""
    seen, out = {used_token}, []
    for c in _token_candidates(uid, log):
        if c["token"] in seen:
            continue
        seen.add(c["token"])
        out.append(c["token"])
    return out


def checkin_account(acc, log):
    """一个账号走一轮签到，**含「令牌被吊销就换一枚候选再试」的补救**。

    为什么需要：JWT 的 `exp` 与「服务端是否还认」是两码事。缓存里可能留着
    一枚看起来没过期、实际已被吊销的令牌，而日志里还有另一枚可用的。
    只试一枚就直接报 expired，会把「换个令牌就能签成」误报成失败。
    """
    status, detail = _checkin_one(acc, log)
    if status != "expired":
        return status, detail
    for alt in _rescue_tokens(acc["uid"], acc["token"], log):
        log(f"[traecn] {acc.get('name') or acc['uid']}: 当前令牌被服务端拒绝，"
            f"换用另一枚候选令牌重试…")
        st2, d2 = checkin_by_token(alt, acc.get("did"), log,
                                   warn=_expiry_warning(acc),
                                   name=acc.get("name"), record=True)
        if st2 != "expired":
            return st2, d2
    return status, detail


def run(log, state, cfg, today, force=False):
    accounts = collect_accounts(cfg, log)
    if not accounts:
        log("[traecn] 未发现可用令牌：请打开一次 Trae CN IDE 并登录，下次运行自动恢复")
        return [dict(ts=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                     platform="traecn", account="TraeCN", status="fail",
                     detail="未发现令牌，打开 IDE 登录后自动恢复")]
    log(f"[traecn] 共 {len(accounts)} 个账号待处理")
    results = []
    from datetime import datetime as dt
    for acc in accounts:
        name = acc["name"]
        key = f"traecn:{acc['uid']}"
        if not force and state.get(key) == today:
            detail = "今日已完成" + _expiry_warning(acc)
            log(f"[traecn] {name}: {detail}")
            results.append(dict(ts=dt.now().isoformat(timespec="seconds"),
                                platform="traecn", account=name, status="skip", detail=detail))
            continue
        status, detail = checkin_account(acc, log)
        log(f"[traecn] {name}: {detail}")
        # 接口确认「今日已签到」同样写入日期记录，避免后续轮次重复调接口确认
        if status == "ok" or (status == "skip" and detail.startswith("今日已签到")):
            state[key] = today
        results.append(dict(ts=dt.now().isoformat(timespec="seconds"),
                            platform="traecn", account=name, status=status, detail=detail))
    return results
