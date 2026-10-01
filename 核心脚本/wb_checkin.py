#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 每日签到（腾讯官方接口，本机令牌，多账号）

原理：
  1. 读取 WorkBuddy 桌面端本地登录态（v5.3.8+ 明文 JSON）：
     %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info
  2. 多账号：在桌面端切换账号后运行 `python checkin.py capture`，
     令牌快照存入 tokens_workbuddy.json（仅本机）。
  3. 调用官方接口完成签到（幂等：code=10001 表示今日已签到）。
"""
import json
import os
import time

import requests

_LEGACY_ROOT = os.path.dirname(os.path.abspath(__file__))


def _data_file(*parts):
    """可写数据文件的落点：走 `runtime.data_root()`，并接管旧位置的同名文件。

    ★ 为什么不能再用 `dirname(__file__)`（2026-09-22 修的真缺陷）：
      源码模式下那个目录就是项目根，没问题；但**冻结后 `__file__` 指向
      PyInstaller 的 bundle 目录**（onedir 是 `_internal/`）→ 于是
      `TOKENS_FILE` 落在 `<exe旁>/_internal/tokens_workbuddy.json`，
      而 GUI 侧 `app/config.WB_TOKENS` 落在 `<exe旁>/tokens_workbuddy.json`
      —— **同一个文件两个路径**。后果：账号页「移除令牌快照」改的是后者、
      签到读的是前者 → **移除无效**；账号迁移导出的也不是签到真正用的那份；
      而且 `_internal/` 在升级时会被整体替换 → 令牌快照**静默丢失**。
      详见 `runtime.data_file()`。
    """
    try:
        import runtime
        return runtime.data_file(*parts, legacy_base=_LEGACY_ROOT)
    except Exception:  # noqa: BLE001
        return os.path.join(_LEGACY_ROOT, *parts)


ROOT = _LEGACY_ROOT          # 保持既有对外语义（源码模式 = 项目根）
TOKENS_FILE = _data_file("tokens_workbuddy.json")

def _auth_candidates():
    """WorkBuddy 桌面端登录信息文件的候选路径。

    环境变量缺失时 `%LOCALAPPDATA%` / `%APPDATA%` 会被 `expandvars` **原样留下**，
    路径永远不存在 → 发现层静默返回 0 个账号（本项目最典型的「假故障」）。
    这里改用 `runtime.win_user_dirs` 拿候选目录（含 `~/AppData/...` 兜底），
    并跳过任何仍带 `%` 的未解析值。
    """
    try:
        from runtime import win_user_dirs
        dirs = win_user_dirs("local") + win_user_dirs("roaming")
    except Exception:  # noqa: BLE001   （runtime 不可用时退回原来的两条路径）
        dirs = [os.path.expandvars(r"%LOCALAPPDATA%"),
                os.path.expandvars(r"%APPDATA%")]
    out = []
    for d in dirs:
        if not d or "%" in d:
            continue
        p = os.path.join(d, "CodeBuddyExtension", "Data", "Public", "auth",
                         "workbuddy-desktop.info")
        if p not in out:
            out.append(p)
    return out


AUTH_CANDIDATES = _auth_candidates()

API = "https://copilot.tencent.com"
CHECKIN_URL = API + "/v2/billing/meter/daily-checkin"


def _read_current_auth():
    for path in AUTH_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        acc = d.get("account") or {}
        auth = d.get("auth") or {}
        uid = acc.get("uid")
        # ★ 令牌可能是**加密信封**（新版格式）→ 交给 usable_token 换成
        #   历史快照里的**明文 JWT**（2026-09-25）。拿不到就当作没有登录态，
        #   绝不把 dict 当令牌往下传（那会变成一句误导人的「令牌过期」）。
        token, exp = usable_token(uid, auth.get("accessToken"),
                                  auth.get("expiresAt") or 0)
        if token and uid:
            return {
                "uid": uid,
                # ★ 明文优先；加密信封时从历史快照反查真名（2026-09-25）
                "nickname": readable_name(uid, acc.get("nickname")),
                "phone": safe_text(acc.get("phoneNumber")),
                "token": token,
                "domain": auth.get("domain") or "",
                "expires_at": exp or (auth.get("expiresAt") or 0),
                "enterprise_id": acc.get("enterpriseId") or (acc.get("enterprise") or {}).get("id") or "",
                "source": "desktop-current",
            }
    return None


def _load_saved(strict=False):
    """读令牌快照。

    ★★ `strict=True`：文件**存在但读不出来**时**抛异常**（调用方必须放弃写入）。
      为什么必须区分（2026-09-25 实测的数据丢失事故）：
        `capture_current()` 的写法是
        `saved = _load_saved(); saved[uid] = {...}; _save_saved(saved)`。
        如果 `_load_saved()` 因为**瞬时读失败**（占用 / 半截写 / 编码问题）
        返回 `{}`，这一写就会把**其它已保存账号全部清空** ——
        用户实测「之前捕获并保留了两个账号，现在只剩一个」。
      「文件不存在」（首次捕获）与「文件在但读不了」（危险）必须分开。
    """
    try:
        with open(TOKENS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise
        return {}


ARCHIVE_FILE = _data_file("logs", "wb_tokens_archive.json")


def _read_json_file(path, strict=False):
    """读一个 JSON 文件；不存在 → {}；读不出来 → 严格模式抛、否则 {}。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise
        return {}


def _entry_rank(item):
    """给一条账号记录打分（越大越「新/好」），用于归档取优。"""
    try:
        exp = float(item.get("expires_at") or 0)
    except (TypeError, ValueError):
        exp = 0.0
    has_tok = 1 if isinstance(item.get("token"), str) and item["token"] else 0
    return (has_tok, exp, str(item.get("captured_at") or ""))


def archive_saved(saved):
    """把一份快照**并入归档**（并集，永不删除）。

    ★ 2026-09-25 为什么要有归档：当天出过一次**真实的数据丢失** ——
      `capture_current()` 是「读旧 → 加一条 → 整体覆盖」，一次瞬时读失败
      就把用户**已捕获的第二个账号**抹掉了（`_load_saved(strict=True)` 已堵住
      新发生的路径，但**已经被抹掉的数据没法凭空回来**）。
      归档把「历史上捕获过的每个账号」都留一份，**只增不减**，
      于是任何丢失都能一键找回。
    规则：按 uid 并集；同一 uid 保留**打分更高**的那条
    （有令牌 > 到期更晚 > 捕获时间更晚），并记录 `first_seen` / `last_seen`。
    """
    if not isinstance(saved, dict) or not saved:
        return {}
    arc = _read_json_file(ARCHIVE_FILE)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for uid, item in saved.items():
        if not isinstance(item, dict):
            continue
        cur = arc.get(uid)
        if cur is None:
            new = dict(item)
            new.setdefault("first_seen", item.get("captured_at") or now)
            new["last_seen"] = now
            arc[uid] = new
            continue
        keep, drop = (item, cur) if _entry_rank(item) > _entry_rank(cur) else (cur, item)
        merged = dict(keep)
        merged["first_seen"] = (drop.get("first_seen") or cur.get("first_seen")
                                or drop.get("captured_at") or now)
        merged["last_seen"] = now
        arc[uid] = merged
    try:
        os.makedirs(os.path.dirname(ARCHIVE_FILE), exist_ok=True)
        tmp = ARCHIVE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(arc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ARCHIVE_FILE)      # 原子替换，避免写一半损坏
    except OSError:
        pass
    return arc


def archived_accounts():
    """归档里的全部账号（uid → 记录）。"""
    return _read_json_file(ARCHIVE_FILE)


def recoverable_accounts(saved=None):
    """归档里**当前快照没有**的账号（可找回的那些）。

    返回 `{uid: 记录}`。`saved=None` 时自己读快照。
    """
    if saved is None:
        saved = _load_saved()
    arc = archived_accounts()
    return {uid: item for uid, item in arc.items() if uid not in saved}


def recover_accounts(uids=None, log=None):
    """把归档里的账号**合并回快照**（找回）。

    `uids=None` → 找回全部可找回的；否则只找回指定的。
    返回 `(找回条数, 快照总数, 失败原因或 "")`。

    ★ 与写入路径同样的安全约束：快照**读不出来就不写**（绝不清库）。
    """
    log = log or (lambda *_a, **_k: None)
    try:
        saved = _load_saved(strict=True)
    except Exception as e:  # noqa: BLE001
        return 0, 0, f"快照读取失败（{e!r}），为避免清空已保存账号，本次不写入"
    arc = archived_accounts()
    if not arc:
        return 0, len(saved), ""
    want = [u for u in (uids if uids is not None else arc.keys()) if u in arc]
    n = 0
    for uid in want:
        if uid in saved:
            continue
        item = dict(arc[uid])
        item["recovered_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        # 令牌可能还是旧的信封格式 → 换成可用的明文（拿不到就保留原样，不阻断找回）
        tok, exp = usable_token(uid, item.get("token"), item.get("expires_at") or 0)
        if isinstance(tok, str) and tok.strip():
            item["token"] = tok
            if exp:
                item["expires_at"] = exp
        item["nickname"] = readable_name(uid, item.get("nickname"))
        saved[uid] = item
        n += 1
    if n:
        try:
            _save_saved(saved)
        except OSError as e:
            return 0, len(saved), f"写入失败：{e!r}"
        log(f"[workbuddy] 已从备份归档找回 {n} 个账号，现共 {len(saved)} 个")
    return n, len(saved), ""


def _save_saved(saved):
    # ★ 先并入归档（只增不减），再写快照（2026-09-25）。
    #   这样即使快照被误删/误清，历史捕获过的账号也永远找得回来。
    archive_saved(saved)
    with open(TOKENS_FILE, "w", encoding="utf-8") as f:
        json.dump(saved, f, ensure_ascii=False, indent=2)


def capture_current(log):
    """把桌面端当前登录账号的令牌快照保存下来（供多账号签到用）。

    ★ 护栏（2026-09-18 加）：**绝不拿「已过期的」本机登录态去覆盖已保存的令牌**。

    为什么必须加：`_read_current_auth()` 只读文件、**不校验 `exp`**，而这里原先
    是无条件覆盖同 uid 的记录。于是「桌面端登录态过期了 / 用户登出了」这种
    完全正常的情况下，一次捕获就会把原本还能用几十天的令牌**换成已过期的**
    → 之后每一轮 WorkBuddy 签到都报「令牌失效」，而用户根本没动过任何设置。

    手动点「捕获」时用户至少看得见；但 `credential_refresh` 是**后台自动**跑的，
    用户看不见 —— 所以护栏不能省。
    """
    cur = _read_current_auth()
    if not cur:
        log("[workbuddy] 未读取到本地登录态，请确认已安装并登录 WorkBuddy 桌面端")
        return False
    # ★★ 读失败就**不写**（2026-09-25 数据丢失事故）：
    #   下面的写是「读旧 → 加一条 → 整体覆盖」，一旦旧数据读成 {}，
    #   覆盖就会把**其它已保存账号全清掉**。宁可这次不保存，也不能清库。
    try:
        saved = _load_saved(strict=True)
    except Exception as e:  # noqa: BLE001
        log(f"[workbuddy] 令牌快照读取失败（{e!r}）—— 为避免清空已保存的其它账号，"
            f"本次**不写入**；请检查 {TOKENS_FILE}")
        return False
    old = saved.get(cur["uid"]) or {}

    # 护栏 1：本机登录态已过期 → 保留原有令牌，绝不覆盖
    new_exp = cur.get("expires_at") or 0
    if new_exp and new_exp < time.time() * 1000:
        log(f"[workbuddy] 本机登录态已过期（{cur['nickname']}），"
            f"保留已保存的令牌不动；请重新登录一次 WorkBuddy 桌面端")
        return False

    # 护栏 2：新令牌比已保存的更短命 → 覆盖是净损失，记一笔日志（仍按「本机为准」覆盖）
    old_exp = old.get("expires_at") or 0
    if new_exp and old_exp and new_exp < old_exp:
        log(f"[workbuddy] 本机令牌剩余时间短于已保存令牌"
            f"（{(new_exp - time.time() * 1000) / 86400000:.1f} 天 < "
            f"{(old_exp - time.time() * 1000) / 86400000:.1f} 天），"
            f"按「本机登录态为准」更新")

    # ★ 数据完整性：只写**明文 JWT**（2026-09-25）。
    #   `_read_current_auth` 已经保证这一点，这里再挡一道 ——
    #   快照是唯一能跨「登出/换号」保住凭据的地方，写进去一个用不了的信封
    #   等于把用户的账号悄悄弄坏（而报错却是误导人的「令牌过期」）。
    #   ★ 判据只要求「非空字符串」——**不限定必须是 JWT 形状**：
    #     WorkBuddy 现在用的是 JWT，但把格式写死会在它换格式时误杀；
    #     这里要拦的只有一件事：**加密信封（dict）不能当令牌存下去**。
    if not isinstance(cur.get("token"), str) or not cur["token"].strip():
        log("[workbuddy] 本机登录态里的令牌是加密格式，且历史快照里没有可用的明文令牌"
            " —— 本次不保存。请先在 WorkBuddy 桌面端登录一次以刷新本地明文快照")
        return False

    saved[cur["uid"]] = {
        "nickname": safe_text(cur.get("nickname")),
        "phone": safe_text(cur.get("phone")),
        "token": cur["token"],
        "domain": cur["domain"],
        "expires_at": cur["expires_at"],
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_saved(saved)
    log(f"[workbuddy] 已保存账号：{cur.get('nickname') or cur['uid'][:8] + '****'}（uid={cur['uid'][:8]}****），"
        f"共 {len(saved)} 个已保存账号")
    return True


def safe_text(value):
    """把「可能是加密信封」的字段规范成**可读字符串**；不是非空字符串就返回 ""。

    ★ 实测受影响的是 **nickname 与 phone 两个字段**（同一账号一起被加密）。

    ★ 2026-09-24 修的真缺陷：WorkBuddy 桌面端改了本地存储格式 ——
      昵称不再是明文，而是一个**加密信封** dict：
      ``{'$wbEncrypted': 1, 'envelope': 'eyJzdWl0ZSI6MSw...'}``。
      捕获时被原样存进了 ``tokens_workbuddy.json``，界面又把它当字符串用
      （``display[:1]``、f-string 拼接）→ **账号管理页直接打不开**
      （``KeyError(slice(None, 1, None))``；Python 3.12+ 起 slice 可哈希，
      所以 dict 被切片报的是 KeyError 而不是 TypeError，很容易看懵）。
      ⇒ 数据层统一兜底：不是非空字符串就当「没有昵称」，让调用方退回
        「手机号 / uid 前缀」——**绝不把 dict 漏到界面**。
      ★ 不去解那个信封：那是 WorkBuddy 的私有格式，硬猜只会引入更难查的错。
    """
    return value.strip() if isinstance(value, str) else ""


_NAME_HISTORY = {"ts": 0.0, "map": {}}
_NAME_HISTORY_TTL_S = 60.0


def _name_history():
    """uid → **明文**昵称（从 auth 目录的历史快照里挖）。

    ★★ 2026-09-25：WorkBuddy 新版把 `nickname` 改成了**加密信封**
      （`{"$wbEncrypted": 1, "envelope": "..."}` —— AES-GCM，`keyId` 指向
      服务端密钥，本地**解不开**）。于是界面上两个账号都只能显示 uid 前缀
      （`6f2fe543****` / `83992452****`），用户报「账号名显示不对」。

      但**同一个 auth 目录里保留着历史快照**
      （`workbuddy-desktop.<时间戳>.info`，实测 48 个），
      那些**旧格式的昵称是明文**的 → 用 uid 反查就能拿到真名。实测：
        `6f2fe543-fe5a-42d1-b702-794cddf28f62` → `Klein`
        `83992452-51ec-4353-9399-98591a64f25a` → `KK`

    ★ 只认「非空字符串」；加密信封一律跳过（`safe_text` 负责）。
      找不到就返回空 map → 调用方退回 uid 前缀，**不报错、不漏 dict**。
    ★ 60 秒 TTL 缓存：48 个小文件扫一次很快，但别每帧都扫。
    """
    now = time.time()
    if now - _NAME_HISTORY["ts"] < _NAME_HISTORY_TTL_S:
        return _NAME_HISTORY["map"]
    out = {}
    try:
        import glob
        for live in AUTH_CANDIDATES:
            d = os.path.dirname(live)
            if not os.path.isdir(d):
                continue
            # 含**当前**那份：它可能已是加密格式，会被 safe_text 跳过，无妨
            for p in glob.glob(os.path.join(d, "workbuddy-desktop*.info")):
                try:
                    with open(p, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                for key in ("account", "accounts", "allAccounts"):
                    v = data.get(key)
                    if isinstance(v, dict):
                        v = [v]
                    if not isinstance(v, list):
                        continue
                    for a in v:
                        if not isinstance(a, dict):
                            continue
                        uid = a.get("uid")
                        nm = safe_text(a.get("nickname"))
                        if uid and nm and uid not in out:
                            out[uid] = nm
    except Exception:  # noqa: BLE001
        out = {}
    _NAME_HISTORY["ts"] = now
    _NAME_HISTORY["map"] = out
    return out


def readable_name(uid, nickname=""):
    """账号可读名：明文昵称 → 历史快照里的明文 → ""（调用方退回 uid 前缀）。

    ★ 数据层唯一出口：界面拿到的永远是「非空字符串或空串」，
      **绝不可能是加密信封 dict**（那会让账号页整页崩）。
    """
    nm = safe_text(nickname)
    if nm:
        return nm
    try:
        return _name_history().get(uid or "", "")
    except Exception:  # noqa: BLE001
        return ""


def is_jwt(value):
    """是不是一个**明文 JWT**（`eyJ...` 三段式）。

    ★ 2026-09-25：WorkBuddy 新版把 `auth.accessToken` 也加密成了信封 dict
      （`{"$wbEncrypted": 1, "envelope": "..."}`）。它**不能**当令牌用 ——
      直接塞进 `Authorization: Bearer <dict>` 只会得到 401，
      然后被误报成「令牌过期」。这里用「是不是明文 JWT」把两种情况分开。
    """
    return isinstance(value, str) and value.startswith("eyJ") and value.count(".") == 2


def _jwt_exp_ms(token):
    """从 JWT 的 payload 里取 `exp`（毫秒）；取不到返回 0。"""
    try:
        import base64
        pl = token.split(".")[1]
        pl += "=" * (-len(pl) % 4)
        exp = json.loads(base64.urlsafe_b64decode(pl)).get("exp")
        return int(exp) * 1000 if exp else 0
    except Exception:  # noqa: BLE001
        return 0


_TOKEN_HISTORY = {"ts": 0.0, "map": {}}
_TOKEN_HISTORY_TTL_S = 60.0


def _token_history():
    """uid → (明文 JWT, 到期毫秒) —— 从 auth 目录的历史快照里挖。

    ★★ 2026-09-25 为什么必须有：WorkBuddy 新版把**令牌**也加密了
      （`accessToken` 变成 `$wbEncrypted` 信封），本地解不开 →
      `Authorization: Bearer <dict>` 必然 401 → 用户报「workbuddy 手动签到失败」。
      但**同一个 auth 目录里的历史快照是明文的**，而且实测**都还在有效期内**：
        · `6f2fe543-…`（Klein）→ exp 2026-10-24
        · `83992452-…`（KK）  → exp 2026-11-14
      → 用 uid 反查**最新的那份明文 JWT** 即可继续签到。
    ★ 取「每个 uid 里文件最新的一份」；只认明文 JWT（信封跳过）。
    ★ 60 秒 TTL 缓存（几十个小文件，扫一次很快，但别每帧都扫）。
    """
    now = time.time()
    if now - _TOKEN_HISTORY["ts"] < _TOKEN_HISTORY_TTL_S:
        return _TOKEN_HISTORY["map"]
    best = {}
    try:
        import glob
        for live in AUTH_CANDIDATES:
            d = os.path.dirname(live)
            if not os.path.isdir(d):
                continue
            for p in glob.glob(os.path.join(d, "workbuddy-desktop*.info")):
                try:
                    mt = os.path.getmtime(p)
                    with open(p, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                tok = (data.get("auth") or {}).get("accessToken")
                if not is_jwt(tok):
                    continue
                exp = _jwt_exp_ms(tok)
                for key in ("account", "accounts", "allAccounts"):
                    v = data.get(key)
                    if isinstance(v, dict):
                        v = [v]
                    if not isinstance(v, list):
                        continue
                    for a in v:
                        uid = (a or {}).get("uid") if isinstance(a, dict) else None
                        if not uid:
                            continue
                        cur = best.get(uid)
                        if cur is None or mt > cur[0]:
                            best[uid] = (mt, tok, exp)
    except Exception:  # noqa: BLE001
        best = {}
    out = {uid: (tok, exp) for uid, (_mt, tok, exp) in best.items()}
    _TOKEN_HISTORY["ts"] = now
    _TOKEN_HISTORY["map"] = out
    return out


def usable_token(uid, raw_token, exp_hint=0):
    """拿到**可用的**令牌字符串：非空字符串直接用，非字符串（加密信封）才去查历史。

    返回 `(token, expires_at_ms)`；拿不到就 `("", 0)` ——
    **绝不把加密信封当令牌返回**（那会变成一句误导人的「令牌过期」）。

    ★ 判据刻意**不要求是 JWT 形状**：
      · WorkBuddy 现在用 JWT，但把格式写死会在它换格式时**误杀**；
      · 这里要拦的只有一件事：**`$wbEncrypted` 信封（dict）不能当令牌用**。
      （第一版写成「必须是 JWT」，直接把测试里 `'FRESH'` 这类桩令牌判成
       「没有登录态」→ `capture_current` 全挂。判据比需求更严就是 bug。）
    """
    if isinstance(raw_token, str) and raw_token.strip():
        return raw_token, (exp_hint or _jwt_exp_ms(raw_token))
    try:
        tok, exp = _token_history().get(uid or "", ("", 0))
        return tok, (exp or exp_hint or 0)
    except Exception:  # noqa: BLE001
        return "", 0


def _collect_accounts(cfg, log):
    """合并当前登录账号 + 已 capture 的账号，按 uid 去重，优先最新登录态。"""
    wb_cfg = cfg.get("workbuddy", {})
    accounts = {}
    if wb_cfg.get("auto_detect_current", True):
        cur = _read_current_auth()
        if cur:
            accounts[cur["uid"]] = cur
    if wb_cfg.get("use_saved_tokens", True):
        for uid, item in _load_saved().items():
            if uid not in accounts:  # 当前登录态总是更新
                # ★ 快照里存的 token 也可能是**加密信封**（旧版捕获时存下来的）
                #   → 用 usable_token 换成历史里的明文 JWT（2026-09-25）。
                tok, exp = usable_token(uid, item.get("token"),
                                        item.get("expires_at") or 0)
                accounts[uid] = {
                    "uid": uid,
                    "nickname": readable_name(uid, item.get("nickname")),
                    "phone": safe_text(item.get("phone")),
                    "token": tok,
                    "domain": item.get("domain") or "",
                    "expires_at": exp or (item.get("expires_at") or 0),
                    "enterprise_id": "",
                    "source": "captured",
                }
    return list(accounts.values())


def _headers(acc):
    # ★ 兜底：令牌必须是明文 JWT（2026-09-25）。
    #   新版把 accessToken 加密成了 dict；直接塞进 Bearer 只会 401，
    #   然后被误报成「令牌过期」，让人以为是账号问题。
    #   ★ 同上：只拦「非字符串」（加密信封），不限定 JWT 形状。
    _tok = acc.get("token")
    if not isinstance(_tok, str) or not _tok.strip():
        raise ValueError(
            "WorkBuddy 令牌不可用（是加密格式，不是明文令牌）——"
            "请在 WorkBuddy 桌面端登录一次，或到「账号管理」重新捕获")
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


def _checkin_one(acc, log):
    name = safe_text(acc.get("nickname")) or acc["uid"][:8] + "****"
    exp = acc.get("expires_at") or 0
    if exp and exp < time.time() * 1000:
        log(f"[workbuddy] {name}: 本地令牌已过有效期，仍尝试调用（过期则需刷新登录态）")
    # 网络/网关抖动重试（共 3 次尝试）；401/403 属令牌问题不重试
    r = None
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(CHECKIN_URL, headers=_headers(acc), data="{}", timeout=20)
            break
        except requests.RequestException as e:
            last_err = e
            if attempt < 2:
                log(f"[workbuddy] {acc.get('nickname') or '账号'}: 请求抖动，3s 后重试({attempt + 1}/2)")
                time.sleep(3)
    if r is None:
        return "fail", f"网络异常: {last_err!r}"
    if r.status_code in (401, 403):
        tip = ("令牌过期/无权限，请打开 WorkBuddy 桌面端刷新登录态"
               + ("，多账号请重新执行 python checkin.py capture" if acc.get("source") == "captured" else ""))
        return "expired", tip
    try:
        body = r.json()
    except ValueError:
        if r.status_code != 200:
            return "fail", f"HTTP {r.status_code}: {r.text[:200]!r}"
        return "fail", f"响应非 JSON: {r.text[:200]!r}"
    code = body.get("code")
    if code == 0:
        data = body.get("data") or {}
        credit = data.get("credit", data.get("points", "?"))
        streak = data.get("streak_days", data.get("continuousDays", "?"))
        return "ok", f"签到成功，领取 {credit} 积分，已连续签到 {streak} 天"
    if code == 10001:
        # 实测已签到时网关返回 HTTP 400 + code=10001，视为成功（幂等）
        return "ok", "今日已签到（接口幂等返回）"
    return "fail", f"code={code} msg={body.get('msg') or body.get('message')}"


def _expiry_warning(acc):
    """令牌 7 天内到期时返回预警后缀，提前提示重新 capture。"""
    exp = acc.get("expires_at") or 0
    if not exp:
        return ""
    days_left = (exp - time.time() * 1000) / 86400000
    if 0 < days_left <= 7:
        return f"；⚠令牌约{max(1, round(days_left))}天后过期，请重新capture"
    return ""


def run(log, state, cfg, today, force=False):
    results = []
    accounts = _collect_accounts(cfg, log)
    if not accounts:
        log("[workbuddy] 未找到任何账号登录态（请登录桌面端，或多账号请先 capture）")
        results.append(dict(ts="", platform="workbuddy", account="-", status="fail",
                            detail="未找到登录态"))
        return results
    log(f"[workbuddy] 共 {len(accounts)} 个账号待处理")
    for acc in accounts:
        name = safe_text(acc.get("nickname")) or acc["uid"][:8] + "****"
        key = f"workbuddy:{acc['uid']}"
        if not force and state.get(key) == today:
            log(f"[workbuddy] {name}: 今日已完成，跳过")
            detail = "今日已完成" + _expiry_warning(acc)
            if "⚠" in detail:
                log(f"[workbuddy] {name}: {detail.split('；', 1)[1]}")
            entry = dict(ts="", platform="workbuddy", account=name, status="skip", detail=detail)
        else:
            status, detail = _checkin_one(acc, log)
            log(f"[workbuddy] {name}: {detail}")
            if status == "ok":
                state[key] = today
                detail += _expiry_warning(acc)
            entry = dict(ts="", platform="workbuddy", account=name, status=status, detail=detail)
        entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        results.append(entry)
    return results
