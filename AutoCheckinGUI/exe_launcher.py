#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包版统一入口（PyInstaller 使用）。

一个 exe 承担三种角色，靠首参区分：
  （无参数）                    启动图形界面——给用户双击用
  --run-checkin <子命令...>     执行签到（计划任务调用，等价 python checkin.py ...）
  --toast <json>                弹签到结果卡片（签到流程内部调用）
  --selftest                    不开窗口，打印打包完整性自检（排障用）
  --envcheck                    不开窗口，逐项检查本机功能可用性
                                （数据目录 / 依赖 / 浏览器 / 客户端 / 令牌 /
                                 计划任务 / 网络；有 FAIL 时退出码为 1）
  --creds                       不开窗口，打印三平台凭据寿命与当前剩余时间
  --refresh-creds               立刻重新捕获一次凭据（会写盘），再打印状态

之所以要这样一个入口，是因为打包后 `sys.executable` 指向 exe 自身，
原代码里 `sys.executable + "checkin.py"` 的写法会失效。这里把三条路径
收敛到一个可执行文件，计划任务只需指向 exe 即可。

注意：
  * 必须最先设置 DPI 感知，否则 Tk 界面在高分屏上模糊。
  * `--run-checkin` / `--toast` 分支下不要 import tkinter，避免拖慢
    计划任务的启动（签到本身不需要 GUI）。
"""
import os
import sys

# 保证能 import 到同目录（脚本模式）或已打进包里的模块
_HERE = os.path.dirname(os.path.abspath(__file__))
_CORE = os.path.abspath(os.path.join(_HERE, "..", "核心脚本"))
for _path in (_CORE, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)
# 打包后追加 _MEIPASS，确保 checkin/tw_checkin 等模块可被找到
_MEI = getattr(sys, "_MEIPASS", None)
if _MEI and _MEI not in sys.path:
    sys.path.insert(0, _MEI)


def _set_dpi():
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _reconfigure():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def _selftest():
    """打印打包完整性自检结果，返回退出码（0 = 全过）。

    存在意义：用户反馈「某个功能点了没反应」时，不必让他装 Python，
    直接 `程序.exe --selftest` 就能看出是模块没打进去还是运行期出错。
    """
    import importlib
    print(f"frozen      = {getattr(sys, 'frozen', False)}")
    print(f"_MEIPASS    = {getattr(sys, '_MEIPASS', '(无)')}")
    print(f"executable  = {sys.executable}")

    # 数据目录定位（最容易出问题的一环：
    # 早期版本 exe 旁没有 checkin.py，ROOT 会落到 exe 所在空目录，
    # 表现为界面「0 个账号 / 暂无账号」，与用户已有配置脱节）
    try:
        from app import config as _C
        print(f"\n数据目录    = {_C.ROOT}")
        print(f"  配置文件  = {_C.CONFIG_PATH}  "
              f"{'（存在）' if os.path.isfile(_C.CONFIG_PATH) else '（不存在）'}")
        cfg = _C.load_config()
        n_tw = len(cfg.get("traework", {}).get("accounts", []))
        print(f"  已配置账号 = TraeWork {n_tw} 个"
              f"（另有 WorkBuddy / TraeCN 走自动发现）")
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL  数据目录定位 -> {e!r}")

    mods = [
        ("app.application", "图形界面入口"),
        ("app.services.api_server", "API 服务"),
        ("app.services.local_backends", "本地模型（Ollama / llama.cpp）"),
        ("app.services.solo_upstream", "云端上游转发（llm_utils_chat）"),
        ("app.services.ai_dispatch", "AI 调度（本地优先 + 回落云端）"),
        ("app.services.diagnose", "签到失败诊断与一键修复"),
        ("app.services.scheduler", "计划任务"),
        ("app.services.device_reset", "设备标识重置"),
        ("app.services.client_launcher", "客户端定位与启动（登录引导）"),
        ("app.services.account_bundle", "账号包导出 / 导入（换设备迁移）"),
        ("app.services.envcheck", "环境自检"),
        ("app.services.credential_refresh", "凭据寿命与自动重新捕获"),
        ("app.pages.api_service", "API 服务页面"),
        ("checkin", "命令行签到编排"),
        ("runtime", "路径自调用"),
        ("requests", "HTTP 客户端"),
        ("playwright", "浏览器自动化"),
    ]
    bad = 0
    for name, desc in mods:
        try:
            importlib.import_module(name)
            print(f"  OK    {name:34s} {desc}")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"  FAIL  {name:34s} {desc}  -> {e!r}")

    # 本地后端的实际可用性（不需要真的起服务）
    try:
        from app.services import local_backends as LB
        print(f"\n本地后端注册表：{sorted(LB.BACKENDS)}")
        for kind in sorted(LB.BACKENDS):
            be = LB.make_backend(kind)
            print(f"  {kind:10s} 默认地址 {be.base_url}")
        print(f"路由前缀：{LB.LOCAL_PREFIX!r}（"
              f"'local/ollama/xxx' -> {LB.split_model('local/ollama/xxx')}）")
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"  FAIL  本地后端注册表 -> {e!r}")

    # 账号发现：确认本机客户端扫描链路在冻结版下可用
    # （用户反馈过「TraeWork 板块识别不到本机软件里的账号」，这里直接给出结论）
    try:
        import tc_checkin
        roots = tc_checkin.client_log_roots()
        print(f"\n本机 Trae 客户端日志根：{len(roots)} 个")
        for path, client, kind in roots:
            print(f"  - {client}（{kind}）{path}")
        ids = tc_checkin.scan_client_identity()
        if ids:
            for uid, info in ids.items():
                print(f"  已登录：{info.get('name') or uid[-6:]}"
                      f"（{info.get('client')}，uid=…{uid[-6:]}"
                      f"，令牌{'有' if info.get('has_token') else '无'}）")
        else:
            print("  （未识别到本机客户端登录态，属正常：未装或未登录）")
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"  FAIL  账号发现链路 -> {e!r}")

    # 云端上游转发：确认协议常量与凭据装载链路在冻结版下可用
    try:
        from app.services import solo_upstream as SU
        print(f"\n云端上游转发：{SU.AGENT_HOST}{SU.EP_LLM_CHAT}")
        print(f"  模型清单 {len(SU.model_ids())} 个，"
              f"默认 {SU.DEFAULT_MODEL}，"
              f"请求头 {len(SU.build_upstream_headers('jwt', 'dev', 'mach'))} 个")
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"  FAIL  云端上游转发 -> {e!r}")

    # AI 调度：确认本地优先链路与诊断模块在冻结版下可用
    try:
        from app.services import ai_dispatch as AD
        from app.services import diagnose as DG
        st = AD.dispatcher().status()
        print(f"\nAI 调度（本地优先）：本地{'就绪' if st['local_ready'] else '未就绪'}"
              f"（{st['local_detail']}）")
        print(f"  偏好 prefer_local={st['prefer_local']}，"
              f"云端可用={st['cloud_available']}")
        risky = [k for k in DG.ACTIONS if DG.is_risky(k)]
        print(f"  诊断动作白名单 {len(DG.ACTIONS)} 个（其中高风险 "
              f"{len(risky)} 个需二次确认）：{', '.join(sorted(DG.ACTIONS))}")
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"  FAIL  AI 调度 -> {e!r}")

    # API 服务页新增卡片：云端转发 + AI 诊断（确认 UI 文案常量与状态映射可用）
    try:
        from app.pages.api_service import POOL_STATE_CN
        print(f"\nAPI 服务页增强：账号池状态 {len(POOL_STATE_CN)} 种"
              f"（{', '.join(sorted(POOL_STATE_CN))}）")
        print("  新增卡片：云端转发（实测转发/查看池明细）+ "
              "AI 诊断（本地优先，动作需确认）")
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"  FAIL  API 服务页增强 -> {e!r}")

    # 默认模型候选：「接口配置 × 本地模型」联动
    # 关键点是下拉框要能同时列出云端与本地模型，且本地模型能被识别为本地路由。
    try:
        from app.services import api_server as AS_
        svc = AS_.service()
        choices = svc.model_choices()
        n_cloud = sum(1 for _v, _l, g in choices if g == "cloud")
        n_local = sum(1 for _v, _l, g in choices if g == "local")
        print(f"\n默认模型候选：{len(choices)} 个"
              f"（云端 {n_cloud}，本地 {n_local}）")
        print(f"  当前默认模型：{svc.model}")
        print(f"  本地判定：'local/ollama/x' -> "
              f"{svc.is_local_model('local/ollama/x')}，"
              f"'glm-5.2' -> {svc.is_local_model('glm-5.2')}")
        print(f"  设置文件：{svc._settings_path()}（"
              f"{'有' if __import__('os').path.exists(svc._settings_path()) else '无'}"
              f"）")
        st = svc.status()
        print(f"  账号池：{st.get('pool_count')} 个账号，"
              f"其中可转发 {st.get('pool_forwardable')} 个"
              f"（云端转发{'就绪' if st.get('cloud_ready') else '未就绪'}）")
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"  FAIL  默认模型候选 -> {e!r}")

    print(f"\n结果：{'全部通过' if not bad else f'{bad} 项失败'}")
    return 0 if not bad else 1


def _envcheck():
    """`--envcheck`：逐项检查本机所有功能是否可用（排障 / 换设备落地检查）。

    与 `--selftest` 的分工：
      · `--selftest` 只看**打包完整性**（模块有没有打进去）
      · `--envcheck` 看**运行环境**（数据目录 / 依赖 / 浏览器 / 客户端 /
        令牌 / 计划任务 / 网络）
    换了一台电脑、或用户说「某些功能用不了」时，先跑这个。
    """
    try:
        from app.services import envcheck as EC
    except ImportError as e:
        sys.stderr.write(f"内部模块缺失：{e!r}\n")
        return 3
    items = EC.run()
    print(EC.format_text(items))
    counts = EC.summary(items)
    # 有 FAIL 才返回非零：WARN 是「能用但要注意」，不该让脚本判成失败
    return 1 if counts.get("FAIL") else 0


def _creds():
    """`--creds`：打印三平台凭据寿命 + 当前剩余时间（只读）。

    为什么需要：三个平台的凭据寿命差两个数量级（TraeCN 8 小时 /
    TraeWork 会话 5 天 / WorkBuddy 60 天），用户最容易问的就是
    「这个令牌能用多久、要不要重新捕获」。这里一次说清楚。
    """
    try:
        from app.services import credential_refresh as CR
    except ImportError as e:
        sys.stderr.write(f"内部模块缺失：{e!r}\n")
        return 3
    print(CR.format_text())
    return 0


def _refresh_creds():
    """`--refresh-creds`：立刻重新捕获一次凭据（会写盘）。

    与 GUI 里「立即重新捕获」按钮走的是**同一个函数**，
    复用的是签到路径上的 `collect_accounts` / `capture_current`。
    """
    try:
        from app.services import credential_refresh as CR
    except ImportError as e:
        sys.stderr.write(f"内部模块缺失：{e!r}\n")
        return 3
    ok, detail, _stats = CR.refresh(log=lambda m: None)
    print(CR.format_text())
    print("\n结果：%s" % detail)
    return 0 if ok else 1


def _cli_mode():
    """判断是否走命令行分支；返回退出码，None 表示继续起 GUI。"""
    argv = sys.argv[1:]
    if not argv:
        return None
    if argv[0] == "--selftest":
        return _selftest()
    if argv[0] == "--envcheck":
        return _envcheck()
    if argv[0] == "--creds":
        return _creds()
    if argv[0] == "--refresh-creds":
        return _refresh_creds()
    # 只要出现我们认识的分发参数就走 CLI，避免把未知参数误当 GUI 启动
    if argv[0] in ("--run-checkin", "--toast"):
        try:
            import checkin
        except ImportError as e:
            sys.stderr.write(f"内部模块缺失：{e!r}\n")
            return 3
        code = checkin.dispatch_frozen(argv)
        return 0 if code is None else code
    # 兼容裸子命令：`程序.exe run --silent` / `status` / `install` 等
    known = {"run", "login", "capture", "install", "settime",
             "uninstall", "status"}
    if argv[0] in known:
        try:
            import checkin
        except ImportError as e:
            sys.stderr.write(f"内部模块缺失：{e!r}\n")
            return 3
        code = checkin.dispatch_frozen(["--run-checkin", *argv])
        return 0 if code is None else code
    return None


def main():
    _set_dpi()
    _reconfigure()

    code = _cli_mode()
    if code is not None:
        return code

    # ---- 图形界面 ----
    try:
        from app.application import main as run_app
    except ImportError as e:
        sys.stderr.write(f"导入失败：{e!r}\n")
        _report("导入失败", str(e))
        return 1

    missing = []
    for mod, pkg in (("requests", "requests"), ("playwright", "playwright")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        _report("缺少依赖", "以下依赖缺失，签到功能不可用：\n\n  "
                + "\n  ".join(missing))
        return 1

    try:
        run_app()
    except Exception:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        try:
            log = os.path.join(os.path.expanduser("~"), ".trae_buddy_error.log")
            with open(log, "a", encoding="utf-8") as f:
                import datetime
                f.write(f"\n===== {datetime.datetime.now()} =====\n{tb}")
        except OSError:
            log = "(无法写入日志)"
        _report("启动失败", f"{tb}\n\n已记录到：{log}")
        return 1
    return 0


def _report(title, msg):
    """出错时的可见反馈：优先 GUI 弹窗，失败则写 stderr。"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(title, msg)
        r.destroy()
    except Exception:  # noqa: BLE001
        sys.stderr.write(f"[{title}] {msg}\n")


if __name__ == "__main__":
    sys.exit(main() or 0)
