#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境自检 —— 「这台机器上，程序的所有功能能不能用」。

为什么需要
----------
本程序的「故障」绝大多数**不是崩溃，而是静默失效**：

  · ``%APPDATA%`` 没解析出来 → 账号发现返回 0 个，界面一片空白却毫无报错
  · Playwright 浏览器没装 → TraeWork 签到报一句看不懂的 Executable doesn't exist
  · 客户端装在 D 盘 → 只查 ``%LOCALAPPDATA%\\Programs`` 的写法永远找不到
  · 计划任务注册了但账户标识不对 → 永不触发，界面还显示「已安装」
  · 令牌被服务端吊销 → 缓存里 exp 还剩 7 天，实际调不通

这些在「换设备 / 新装 exe」时最容易一起撞上。本模块把每条链路单独探一遍，
给出 ``OK / WARN / FAIL / SKIP`` 与**可执行的修复建议**，
让用户（和排障的人）一眼看到缺哪一环。

设计原则
--------
* 每条检查**独立**：一条失败绝不影响其余检查（否则一个异常吞掉整份报告）。
* 检查项**只读**：唯一的写操作是「在数据目录建一个临时文件再删掉」以验证可写性。
* 结果里必须带 ``hint``：只报 FAIL 不告诉用户怎么办，等于没报。
* 网络检查用 TCP 连接（不用 HTTP）：够判断「通不通」，且不依赖 requests。
"""
import os
import socket
import tempfile
import time

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"


def _item(key, label, status, detail="", hint=""):
    return {"key": key, "label": label, "status": status,
            "detail": detail, "hint": hint}


# ---------------------------------------------------------------- 单项检查

def _check_mode():
    try:
        import runtime
        frozen = runtime.is_frozen()
        mode = "打包版（exe）" if frozen else "源码模式"
        root = runtime.data_root()
        return _item("mode", "运行模式与数据目录", OK,
                     "%s · 数据目录 %s" % (mode, root))
    except Exception as e:  # noqa: BLE001
        return _item("mode", "运行模式与数据目录", FAIL, repr(e),
                     "runtime 模块不可用，安装包可能不完整，建议重新安装")


def _check_writable():
    try:
        from .. import config as C
        root = C.ROOT
    except Exception as e:  # noqa: BLE001
        return _item("writable", "数据目录可写", FAIL, repr(e))
    if not os.path.isdir(root):
        return _item("writable", "数据目录可写", FAIL, "目录不存在：%s" % root,
                     "请检查程序是否有权限创建该目录")
    try:
        # delete=True 让上下文管理器负责清理，不留垃圾
        with tempfile.NamedTemporaryFile(dir=root, prefix=".wtest_",
                                         delete=True):
            pass
        return _item("writable", "数据目录可写", OK, root)
    except OSError as e:
        return _item("writable", "数据目录可写", FAIL, "%s（%s）" % (e, root),
                     "把程序换到有写权限的目录，或以管理员身份运行一次")


def _check_user_dirs():
    """★ 本项目最经典的「假故障」源头：环境变量缺失 → 路径带 % → 永远不存在。"""
    try:
        from . import client_launcher as CL
        env = CL._env_map()
        missing = [k for k in ("LOCALAPPDATA", "APPDATA") if not env.get(k)]
        bad = [v for v in env.values() if v and "%" in v]
        if bad:
            return _item("userdirs", "Windows 用户目录", FAIL,
                         "存在未展开的路径：%s" % bad,
                         "环境变量异常，请重启系统或从「开始菜单」正常启动本程序")
        if missing:
            return _item("userdirs", "Windows 用户目录", WARN,
                         "环境变量缺失：%s（已用 ~/AppData 兜底）"
                         % "、".join(missing),
                         "兜底路径可用，但建议从正常登录的桌面会话启动")
        return _item("userdirs", "Windows 用户目录", OK,
                     "APPDATA / LOCALAPPDATA 均可用")
    except Exception as e:  # noqa: BLE001
        return _item("userdirs", "Windows 用户目录", FAIL, repr(e))


def _check_config():
    try:
        from .. import config as C
        cfg = C.load_config()
        tw = len((cfg.get("traework") or {}).get("accounts") or [])
        sched = cfg.get("schedule") or {}
        return _item("config", "配置文件可读", OK,
                     "config.json 正常 · TraeWork 账号 %d 个 · 签到时间 %s"
                     % (tw, "、".join(sched.get("daily_times") or []) or "未设置"))
    except Exception as e:  # noqa: BLE001
        return _item("config", "配置文件可读", FAIL, repr(e),
                     "删除数据目录下的 config.json 让程序重建（会丢失账号配置）")


def _check_dep(name, label, hint):
    try:
        __import__(name)
        return _item("dep_" + name, label, OK, "已安装")
    except Exception as e:  # noqa: BLE001
        return _item("dep_" + name, label, FAIL, repr(e), hint)


def _browsers_dir():
    """Playwright 浏览器目录（与 runtime.ensure_playwright_browsers 同一口径）。"""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and os.path.isdir(env):
        return env
    local = os.environ.get("LOCALAPPDATA") or ""
    if not local:
        try:
            from runtime import win_user_dirs
            d = win_user_dirs("local")
            local = d[0] if d else ""
        except Exception:  # noqa: BLE001
            local = ""
    if local:
        p = os.path.join(local, "ms-playwright")
        if os.path.isdir(p):
            return p
    return None


def _check_browser():
    """TraeWork 签到依赖 Playwright 的 Chromium；缺了只会报一句看不懂的错。"""
    try:
        import playwright  # noqa: F401
    except Exception:  # noqa: BLE001
        return _item("browser", "Playwright 浏览器", SKIP,
                     "playwright 未安装，跳过",
                     "TraeWork 签到需要它；请重新安装完整版程序")
    d = _browsers_dir()
    if not d:
        return _item("browser", "Playwright 浏览器", FAIL,
                     "未找到浏览器目录（%LOCALAPPDATA%\\ms-playwright）",
                     "运行：python -m playwright install chromium")
    try:
        names = os.listdir(d)
    except OSError as e:
        return _item("browser", "Playwright 浏览器", FAIL, repr(e))
    chrome = [n for n in names if n.startswith(("chromium", "chrome"))]
    if not chrome:
        return _item("browser", "Playwright 浏览器", FAIL,
                     "目录存在但没有 chromium：%s" % d,
                     "运行：python -m playwright install chromium")
    return _item("browser", "Playwright 浏览器", OK,
                 "%s（%d 个组件）" % (d, len(chrome)))


def _check_client(platform, label):
    """客户端是否装在本机（换设备后最容易缺的就是它）。"""
    try:
        from . import client_launcher as CL
        info = CL.diagnose(platform)
    except Exception as e:  # noqa: BLE001
        return _item("client_" + platform, label, FAIL, repr(e))
    if info["found"]:
        return _item("client_" + platform, label, OK,
                     "%s · %s" % (info["path"], info["source"]))
    return _item("client_" + platform, label, WARN, info["detail"],
                 "到「账号管理」点「打开客户端并登录」；"
                 "若确实没装，可用网页版或直接在客户端登录后回来扫描")


def _check_source_traecn():
    try:
        import tc_checkin
        roots = tc_checkin.client_log_roots()
    except Exception as e:  # noqa: BLE001
        return _item("src_traecn", "TraeCN 令牌来源（客户端日志）", FAIL, repr(e))
    if not roots:
        return _item("src_traecn", "TraeCN 令牌来源（客户端日志）", WARN,
                     "未发现任何 Trae 客户端日志目录",
                     "打开一次 Trae CN IDE 或 TraeWork CN 桌面端并登录，"
                     "令牌会自动写入日志")
    try:
        found = tc_checkin._scan_ide_logs(lambda m: None)
    except Exception as e:  # noqa: BLE001
        return _item("src_traecn", "TraeCN 令牌来源（客户端日志）", FAIL, repr(e))
    if found:
        return _item("src_traecn", "TraeCN 令牌来源（客户端日志）", OK,
                     "发现 %d 个账号令牌 · 日志目录 %d 个"
                     % (len(found), len(roots)))
    return _item("src_traecn", "TraeCN 令牌来源（客户端日志）", WARN,
                 "日志目录存在但没有有效令牌",
                 "在客户端里登录一次（令牌有有效期，过期后需重新登录）")


def _check_source_traework():
    try:
        from .. import config as C
        base = C.TW_PROFILES
    except Exception as e:  # noqa: BLE001
        return _item("src_traework", "TraeWork 网页会话", FAIL, repr(e))
    if not os.path.isdir(base):
        return _item("src_traework", "TraeWork 网页会话", WARN,
                     "会话目录不存在：%s" % base,
                     "到「账号管理」点「登录」打开浏览器登录一次即可创建")
    try:
        profiles = [d for d in os.listdir(base)
                    if os.path.isdir(os.path.join(base, d))]
    except OSError as e:
        return _item("src_traework", "TraeWork 网页会话", FAIL, repr(e))
    if not profiles:
        return _item("src_traework", "TraeWork 网页会话", WARN, "还没有任何会话",
                     "到「账号管理」点「登录」完成一次网页版登录")
    logged = 0
    for p in profiles:
        # ★ 必须用 config 里的同一个判据（两个 Cookies 位置都查）——
        #   这里曾按 Default/Cookies 单独判一次，结果把已登录的会话报成
        #   「0 个已登录」的假警告。判据只留一份，别再抄第二遍。
        try:
            hit, _ck = C.traework_has_session(os.path.join(base, p))
        except Exception:  # noqa: BLE001
            hit = False
        if hit:
            logged += 1
    status = OK if logged else WARN
    return _item("src_traework", "TraeWork 网页会话", status,
                 "%d 个会话，其中 %d 个已登录" % (len(profiles), logged),
                 "" if logged else "会话目录是空的，点「登录」完成一次登录")


def _check_source_workbuddy():
    try:
        import wb_checkin
        cands = wb_checkin.AUTH_CANDIDATES
    except Exception as e:  # noqa: BLE001
        return _item("src_workbuddy", "WorkBuddy 登录态文件", FAIL, repr(e))
    hit = [p for p in cands if os.path.isfile(p)]
    if hit:
        try:
            cur = wb_checkin._read_current_auth()
        except Exception:  # noqa: BLE001
            cur = None
        if cur:
            return _item("src_workbuddy", "WorkBuddy 登录态文件", OK,
                         "已登录：%s" % (cur.get("nickname")
                                       or cur["uid"][:8]))
        return _item("src_workbuddy", "WorkBuddy 登录态文件", WARN,
                     "文件存在但没有可用的 accessToken",
                     "在 WorkBuddy 桌面端重新登录一次")
    return _item("src_workbuddy", "WorkBuddy 登录态文件", WARN,
                 "未找到 %s" % (cands[0] if cands else "auth 文件"),
                 "到「账号管理」点「打开 WorkBuddy 并登录」登录一次")


def _check_tokens():
    """令牌到期情况 —— 顺手把「还有几天」告诉用户。"""
    now = time.time()
    soon, total = [], 0
    try:
        import tc_checkin
        for uid, val in (tc_checkin._scan_ide_logs(lambda m: None) or {}).items():
            exp = val[1] if isinstance(val, (tuple, list)) and len(val) > 1 else 0
            total += 1
            if exp and (exp - now) < 3 * 86400:
                soon.append("TraeCN …%s" % str(uid)[-4:])
    except Exception:  # noqa: BLE001
        pass
    try:
        import wb_checkin
        for uid, it in (wb_checkin._load_saved() or {}).items():
            total += 1
            exp = (it.get("expires_at") or 0) / 1000.0
            if exp and (exp - now) < 3 * 86400:
                soon.append("WorkBuddy …%s" % str(uid)[:8])
    except Exception:  # noqa: BLE001
        pass
    if total == 0:
        return _item("tokens", "令牌有效期", WARN, "本机没有可用令牌",
                     "先在各平台完成一次登录")
    if soon:
        return _item("tokens", "令牌有效期", WARN,
                     "%d 个令牌 3 天内到期：%s" % (len(soon), "、".join(soon[:4])),
                     "打开一次对应客户端即可自动续期")
    return _item("tokens", "令牌有效期", OK, "%d 个令牌，均未临近到期" % total)


def _check_task():
    """计划任务：注册了 ≠ 会触发（本项目踩过「注册成功但永不触发」）。"""
    try:
        from . import scheduler
        info = scheduler.task_info()
    except Exception as e:  # noqa: BLE001
        return _item("task", "Windows 计划任务", SKIP, repr(e))
    if not isinstance(info, dict):
        return _item("task", "Windows 计划任务", SKIP, "未取到任务信息")
    state = info.get("state") or info.get("status") or ""
    installed = info.get("installed")
    if installed is False or not state:
        return _item("task", "Windows 计划任务", WARN, "尚未安装每日自动签到任务",
                     "到「系统设置」点「安装/更新计划任务」")
    if str(state).lower() in ("ready", "running"):
        return _item("task", "Windows 计划任务", OK,
                     "状态 %s · 下次运行 %s"
                     % (state, info.get("next_run") or "未知"))
    return _item("task", "Windows 计划任务", WARN,
                 "状态 %s（不是 Ready，可能不会自动触发）" % state,
                 "到「系统设置」重新安装一次计划任务")


def _net(host, port=443, timeout=4.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as e:
        return False, str(e)


def _check_net(host, label, hint):
    ok, err = _net(host)
    if ok:
        return _item("net_" + host, label, OK, "%s:443 可连接" % host)
    return _item("net_" + host, label, FAIL, "%s:443 连不上（%s）" % (host, err),
                 hint)


# ---------------------------------------------------------------- 主入口

# 检查项顺序 = 报告顺序：从「程序自身」到「外部依赖」再到「网络」
_CHECKS = (
    _check_mode,
    _check_writable,
    _check_user_dirs,
    _check_config,
    lambda: _check_dep("requests", "依赖：requests", "重新安装完整版程序"),
    lambda: _check_dep("playwright", "依赖：playwright",
                       "重新安装完整版程序"),
    _check_browser,
    lambda: _check_client("traecn", "Trae CN IDE 客户端"),
    lambda: _check_client("traework", "TraeWork CN 桌面端"),
    lambda: _check_client("workbuddy", "WorkBuddy 桌面端"),
    _check_source_traecn,
    _check_source_traework,
    _check_source_workbuddy,
    _check_tokens,
    _check_task,
    lambda: _check_net("api.trae.cn", "网络：api.trae.cn",
                       "TraeCN/TraeWork 签到需要访问该域名，"
                       "请检查网络或代理设置"),
    lambda: _check_net("copilot.tencent.com", "网络：copilot.tencent.com",
                       "WorkBuddy 签到需要访问该域名，请检查网络或代理设置"),
)


def run(progress=None):
    """跑一遍自检。返回 ``[{key,label,status,detail,hint}, ...]``。

    ★ 每条独立 try/except：一条炸了不能吞掉整份报告
      （否则用户看到的是「自检失败」，而真正的原因被盖住了）。
    """
    out = []
    total = len(_CHECKS)
    for i, fn in enumerate(_CHECKS):
        if progress:
            try:
                progress(i, total)
            except Exception:  # noqa: BLE001
                pass
        try:
            out.append(fn())
        except Exception as e:  # noqa: BLE001
            out.append(_item(getattr(fn, "__name__", "check"),
                             "未命名检查", FAIL, repr(e)))
    if progress:
        try:
            progress(total, total)
        except Exception:  # noqa: BLE001
            pass
    return out


def summary(items):
    """统计各状态数量。"""
    counts = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    return counts


def overall(items):
    """一句话结论 + 语义色。"""
    c = summary(items)
    if c[FAIL]:
        return ("发现 %d 项不可用，%d 项需注意" % (c[FAIL], c[WARN]), "red")
    if c[WARN]:
        return ("全部可用，但有 %d 项需要注意" % c[WARN], "amber")
    return ("全部通过（%d 项）" % (c[OK] + c[SKIP]), "green")


def format_text(items):
    """纯文本报告（用于「复制报告」与命令行 ``--envcheck``）。"""
    mark = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}
    lines = ["TraeBuddy 签到助手 · 环境自检报告",
             "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "=" * 62]
    for it in items:
        lines.append("%s %s" % (mark.get(it["status"], "[????]"), it["label"]))
        if it["detail"]:
            lines.append("        " + str(it["detail"]).replace("\n", "\n        "))
        if it["hint"] and it["status"] in (FAIL, WARN):
            lines.append("        → " + str(it["hint"]).replace("\n", "\n        "))
    verdict, _tone = overall(items)
    lines += ["=" * 62, "结论：" + verdict]
    return "\n".join(lines)
