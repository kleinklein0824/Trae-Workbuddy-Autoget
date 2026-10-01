#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号包导出 / 导入 —— 换设备、新装 exe 的「一键搬家」。

为什么不能只拷 config.json
--------------------------
三个平台的登录态分散在四处，缺任何一份「搬家」都会残废：

  · ``config.json``               —— 账号列表 + 签到时间 + 选项
  · ``logs/tokens_traecn.json``   —— TraeCN 令牌缓存（uid → JWT）
  · ``tokens_workbuddy.json``     —— WorkBuddy 令牌快照（uid → JWT）
  · ``logs/traecn_device.json``   —— TraeCN 的设备标识 ``did``

少了令牌，新设备就得重新登录一遍；少了 ``did``，TraeCN 的接口直接调不通
（本机没有 IDE 日志时根本产不出 did）。所以搬家必须把这四份一起打包。

两种导出模式（★ 含凭据的文件是**敏感文件**）
--------------------------------------------
  · **完整迁移**：含上面四份 → 新设备导入即可用，无需重新登录。
    文件里是**明文 JWT**，等同于账号登录态：别外传、别传公开网盘。
  · **仅配置**  ：只含 ``config.json`` → 不含任何凭据。
    新设备导入后用账号页的「打开客户端并登录」重新登录即可。

导入策略（**幂等**，可重复导入）
--------------------------------
  · 账号列表（``traework.accounts``）**取并集** —— 导入不会丢掉本机已有账号。
  · 令牌按 uid 取并集；同一 uid 保留**更新的那一份**
    （traecn 比 ``saved_at``、workbuddy 比 ``captured_at``），
    与项目里既有的「按发现时间优先」口径一致（只比 exp 会让失效令牌压过新令牌）。
  · 其余设置项（签到时间 / 选项 / 开关）**以导入的为准** ——
    用户导出的目的就是把这些带到新设备。
  · 设备标识 ``did`` **只在本机缺失时**才填入，绝不覆盖本机已有的值。
  · 导入前先把本机现状备份到 ``logs/bundle_backup_<时间戳>/``，可回退。
"""
import datetime
import json
import os
import shutil

BUNDLE_FORMAT = "traebuddy-account-bundle"
BUNDLE_VERSION = 1

WARNING_TEXT = ("本文件包含账号登录凭据（明文令牌），等同于登录态；"
                "请勿公开分享或上传到公开网盘。")


def _C():
    from .. import config as C
    return C


def _read_json(path):
    """读 JSON；不存在或损坏都返回 None（调用方据此区分「空」与「坏」）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 路径

def paths():
    """本机参与搬家的四份数据文件（含是否存在）。"""
    C = _C()
    items = [
        ("config", "账号与设置", C.CONFIG_PATH),
        ("tokens_traecn", "TraeCN 令牌缓存",
         os.path.join(C.LOG_DIR, "tokens_traecn.json")),
        ("tokens_workbuddy", "WorkBuddy 令牌快照", C.WB_TOKENS),
        ("traecn_device", "TraeCN 设备标识",
         os.path.join(C.LOG_DIR, "traecn_device.json")),
    ]
    return [{"key": k, "label": lbl, "path": p, "exists": os.path.isfile(p)}
            for k, lbl, p in items]


def default_path(with_credentials=True):
    """建议的导出文件名（优先放桌面，便于用户找到）。"""
    home = os.path.expanduser("~")
    desktop = os.path.join(home, "Desktop")
    base = desktop if os.path.isdir(desktop) else _C().ROOT
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    kind = "完整" if with_credentials else "仅配置"
    return os.path.join(base, f"TraeBuddy账号包_{kind}_{stamp}.json")


# ---------------------------------------------------------------- 导出

def collect(with_credentials=True):
    """收集要打包的内容。返回 ``(payload, summary)``。"""
    C = _C()
    payload = {"config": _read_json(C.CONFIG_PATH) or {}}
    summary = {}

    cfg = payload["config"]
    summary["traework"] = len((cfg.get("traework") or {}).get("accounts") or [])
    summary["schedule"] = bool(cfg.get("schedule"))

    if with_credentials:
        tc = _read_json(os.path.join(C.LOG_DIR, "tokens_traecn.json")) or {}
        wb = _read_json(C.WB_TOKENS) or {}
        dev = _read_json(os.path.join(C.LOG_DIR, "traecn_device.json")) or {}
        payload["tokens_traecn"] = tc
        payload["tokens_workbuddy"] = wb
        payload["traecn_device"] = dev
        summary["traecn"] = len(tc)
        summary["workbuddy"] = len(wb)
        summary["has_device_id"] = bool(dev)
    else:
        summary["traecn"] = 0
        summary["workbuddy"] = 0
        summary["has_device_id"] = False

    # 账号总数（按各平台账号表口径）
    summary["total_accounts"] = (summary["traework"] + summary["traecn"]
                                 + summary["workbuddy"])
    return payload, summary


def _hostname():
    """本机名（用于在导入预览里告诉用户「这份包从哪台机器来的」）。

    ★ 不能只读 ``COMPUTERNAME``：精简环境 / 某些启动方式下它是空的
      （本项目已多次踩到「环境变量缺失 → 静默退化成未知」）。
    """
    name = os.environ.get("COMPUTERNAME")
    if name:
        return name
    try:
        import socket
        return socket.gethostname() or ""
    except Exception:  # noqa: BLE001
        return ""


def export_bundle(path=None, with_credentials=True):
    """导出账号包。返回 ``(ok, detail, path)``。"""
    C = _C()
    payload, summary = collect(with_credentials)
    if summary["total_accounts"] == 0:
        return False, ("本机没有可导出的账号（账号列表为空）。"
                       "请先在账号管理里完成登录/捕获。"), None

    doc = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "with_credentials": bool(with_credentials),
        "exported_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_root": C.ROOT,
        "hostname": _hostname(),
        "summary": summary,
        "payload": payload,
    }
    if with_credentials:
        doc["_warning"] = WARNING_TEXT

    path = path or default_path(with_credentials)
    try:
        _write_json(path, doc)
    except OSError as e:
        return False, "写入失败：%s" % e, None

    detail = ("已导出 %d 个账号（TraeWork %d / TraeCN %d / WorkBuddy %d）到\n%s"
              % (summary["total_accounts"], summary["traework"],
                 summary["traecn"], summary["workbuddy"], path))
    if with_credentials:
        detail += "\n\n⚠ 该文件含明文登录凭据，请妥善保管、勿公开分享。"
    return True, detail, path


# ---------------------------------------------------------------- 校验

def inspect_bundle(path):
    """只读校验账号包，返回 ``(ok, detail, info)``。**不修改任何本机数据**。"""
    doc = _read_json(path)
    if doc is None:
        return False, "无法读取该文件（不是有效的 JSON）", None
    if doc.get("format") != BUNDLE_FORMAT:
        return False, ("这不是本程序导出的账号包（format=%r）"
                       % doc.get("format")), None
    ver = doc.get("version")
    if not isinstance(ver, int) or ver < 1:
        return False, "账号包版本号无效：%r" % ver, None
    if ver > BUNDLE_VERSION:
        return False, ("账号包版本 %d 比本程序支持的 %d 更新，"
                       "请先升级本程序" % (ver, BUNDLE_VERSION)), None
    payload = doc.get("payload")
    if not isinstance(payload, dict):
        return False, "账号包内容为空或损坏（缺少 payload）", None

    with_cred = bool(doc.get("with_credentials"))
    tc = payload.get("tokens_traecn") or {}
    wb = payload.get("tokens_workbuddy") or {}
    cfg = payload.get("config") or {}
    tw = (cfg.get("traework") or {}).get("accounts") or []

    info = {
        "with_credentials": with_cred,
        "exported_at": doc.get("exported_at") or "未知",
        "source_root": doc.get("source_root") or "未知",
        "hostname": doc.get("hostname") or "未知",
        "traework": len(tw),
        "traecn": len(tc) if with_cred else 0,
        "workbuddy": len(wb) if with_cred else 0,
        "has_device_id": bool(payload.get("traecn_device")) if with_cred else False,
        "has_schedule": bool(cfg.get("schedule")),
    }
    info["total"] = info["traework"] + info["traecn"] + info["workbuddy"]
    if info["total"] == 0 and not info["has_schedule"]:
        return False, "账号包里没有任何账号或设置", info
    return True, "账号包有效", info


# ---------------------------------------------------------------- 导入

def _merge_accounts(local, incoming):
    """TraeWork 账号列表取并集（按 name/profile 去重）。返回 (结果, 新增数)。"""
    out = list(local or [])
    seen = {(a.get("name") or a.get("profile")) for a in out}
    added = 0
    for a in incoming or []:
        if not isinstance(a, dict):
            continue
        key = a.get("name") or a.get("profile")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(a)
        added += 1
    return out, added


def _stamp_cmp(new, old):
    """比较两个「发现时间」戳；``new`` 更新则返回 True。

    ★ 必须同时支持两种格式，否则合并会**静默失效**：
      · TraeCN  用 ``time.time()`` 的**浮点秒**（``saved_at``）
      · WorkBuddy 用 ``'YYYY-mm-dd HH:MM:SS'`` 的**字符串**（``captured_at``）

    早期写法是直接 ``float(item.get(key))`` —— 字符串戳会抛 ValueError，
    被 ``except`` 吞掉后判定为「不是更新」，
    表现就是「WorkBuddy 令牌导入了但一直不生效」，且没有任何报错。
    """
    if new is None:
        return False
    if old is None:
        return True
    try:
        return float(new) > float(old)
    except (TypeError, ValueError):
        pass
    try:
        # 字符串戳按字典序比较即可：'YYYY-mm-dd HH:MM:SS' 的字典序 == 时间序
        return str(new) > str(old)
    except Exception:  # noqa: BLE001
        return False


def _merge_tokens(local, incoming, stamp_key):
    """令牌按 uid 取并集，同一 uid 保留**更新的那一份**。

    ★ 只比 ``exp`` 是错的：服务端可以提前吊销，缓存里 exp 更长的反而不可用。
      项目里既有的口径是「按发现时间优先」，这里沿用。
    ★ 用**严格大于**：同戳视为「没有变化」，这样重复导入同一份包是幂等的
      （用 ``>=`` 会把每一轮都记成「已更新」，文案上永远显示有变动）。
    """
    out = dict(local or {})
    added = updated = 0
    for uid, item in (incoming or {}).items():
        if not isinstance(item, dict):
            continue
        old = out.get(uid)
        if old is None:
            out[uid] = item
            added += 1
            continue
        if _stamp_cmp(item.get(stamp_key), old.get(stamp_key)):
            out[uid] = item
            updated += 1
    return out, added, updated


def _backup_now():
    """把本机四份数据备份到 ``logs/bundle_backup_<时间戳>/``。返回备份目录。"""
    C = _C()
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(C.LOG_DIR, "bundle_backup_" + stamp)
    try:
        os.makedirs(dest, exist_ok=True)
        for it in paths():
            if it["exists"]:
                shutil.copy2(it["path"], os.path.join(dest, os.path.basename(
                    it["path"])))
    except OSError:
        return None
    return dest


def import_bundle(path, merge=True):
    """导入账号包。返回 ``(ok, detail, stats)``。

    幂等：重复导入同一份包不会产生重复账号，也不会破坏本机已有账号。
    """
    C = _C()
    ok, detail, info = inspect_bundle(path)
    if not ok:
        return False, detail, None
    doc = _read_json(path) or {}
    payload = doc.get("payload") or {}
    with_cred = bool(doc.get("with_credentials"))

    backup = _backup_now()
    stats = {"traework_added": 0, "traecn_added": 0, "workbuddy_added": 0,
             "traecn_updated": 0, "workbuddy_updated": 0,
             "device_id_filled": False, "backup": backup}

    # ---- config.json：账号取并集，设置项以导入为准 ----
    local_cfg = _read_json(C.CONFIG_PATH) or {}
    incoming_cfg = payload.get("config") or {}
    new_cfg = dict(local_cfg)
    for key, val in incoming_cfg.items():
        if key == "traework" and isinstance(val, dict):
            tw = dict(new_cfg.get("traework") or {})
            merged, added = _merge_accounts(
                (new_cfg.get("traework") or {}).get("accounts"), val.get("accounts"))
            tw.update({k: v for k, v in val.items() if k != "accounts"})
            tw["accounts"] = merged
            new_cfg["traework"] = tw
            stats["traework_added"] = added
        else:
            new_cfg[key] = val
    try:
        _write_json(C.CONFIG_PATH, new_cfg)
    except OSError as e:
        return False, "写入 config.json 失败：%s" % e, stats

    if with_cred:
        # ---- TraeCN 令牌 ----
        tc_path = os.path.join(C.LOG_DIR, "tokens_traecn.json")
        merged, added, updated = _merge_tokens(
            _read_json(tc_path) or {}, payload.get("tokens_traecn") or {},
            "saved_at")
        stats["traecn_added"], stats["traecn_updated"] = added, updated
        if added or updated:
            try:
                _write_json(tc_path, merged)
            except OSError as e:
                return False, "写入 TraeCN 令牌失败：%s" % e, stats

        # ---- WorkBuddy 令牌 ----
        merged, added, updated = _merge_tokens(
            _read_json(C.WB_TOKENS) or {}, payload.get("tokens_workbuddy") or {},
            "captured_at")
        stats["workbuddy_added"], stats["workbuddy_updated"] = added, updated
        if added or updated:
            try:
                _write_json(C.WB_TOKENS, merged)
            except OSError as e:
                return False, "写入 WorkBuddy 令牌失败：%s" % e, stats

        # ---- 设备标识：**只在本机缺失时**填入 ----
        dev_path = os.path.join(C.LOG_DIR, "traecn_device.json")
        incoming_dev = payload.get("traecn_device") or {}
        if incoming_dev and not (_read_json(dev_path) or {}):
            try:
                _write_json(dev_path, incoming_dev)
                stats["device_id_filled"] = True
            except OSError:
                pass

    # ★ 这里**不**调用 refresh_accounts_async()：账号刷新会走
    #   collect_accounts → 反查昵称（网络）并回写令牌缓存，
    #   在「导入」这个只该做文件 I/O 的服务里制造副作用。
    #   刷新交给调用方（UI 的 _bg_done 已经会强制刷新缓存）。
    parts = []
    if stats["traework_added"]:
        parts.append("TraeWork 新增 %d" % stats["traework_added"])
    if stats["traecn_added"] or stats["traecn_updated"]:
        parts.append("TraeCN %d 个（更新 %d）"
                     % (stats["traecn_added"], stats["traecn_updated"]))
    if stats["workbuddy_added"] or stats["workbuddy_updated"]:
        parts.append("WorkBuddy %d 个（更新 %d）"
                     % (stats["workbuddy_added"], stats["workbuddy_updated"]))
    if stats["device_id_filled"]:
        parts.append("已补上设备标识")
    detail = ("导入完成：" + ("、".join(parts) if parts
                              else "没有新增内容（本机已有）"))
    if backup:
        detail += "\n\n导入前的本机数据已备份到：\n" + backup
    return True, detail, stats
