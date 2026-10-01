#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量跑回归脚本，抽出每个脚本的结论行，打印一张总表。

为什么需要它
-----------
每个回归脚本的结论格式不完全一样（`%d OK / %d FAIL`、`结果：...`、`合计问题：N`），
而且 **`audit_pages.py` 根本不打印到 stdout** —— 它把结果写进
`tests/audit_result.txt`。手工一条条跑再肉眼比对既慢又容易看漏，
"全都修好了吗"这种问题需要一个**一次性、可复现的总表**。

⚠️ 必须设 `PYTHONIOENCODING=utf-8`：中文 Windows 上 Python 捕获管道时按
locale（GBK）编码，不设会 `UnicodeDecodeError`（本项目在 PowerShell 输出上
踩过同款，见 `MEMORY.md` 6.7）。

用法：
    python tests/_run_suite.py [脚本名 ...]      # 不给就跑默认全集
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

# 默认全集：本轮改动涉及的面 + 主门禁
DEFAULT = [
    "audit_pages",
    "audit_buttons",
    "test_page_cache",
    "test_credits_dashboard",
    "test_manual_priority",
    "test_modal_fit",
    "probe_all_modals",
    "verify_api_modals",
    "test_wheel_scroll",
    "test_layout_clip",
    "probe_cred_card",
    "probe_shot_util",
    "probe_retry_card",
    "verify_toast_popup",
    "test_api_page_ui",
    "test_checkin_buttons",
    "test_tw_sign_detect",
    "test_tw_api_first",
    "test_busy_defer",
    "test_scheduler_refresh",
    "test_config_robust",
    "test_accounts_discovery",
    "test_wb_encrypted_name",
    "test_wb_archive_recover",
    "test_client_launcher",
    "test_account_bundle",
    "test_envcheck",
    "test_credential_refresh",
    "verify_task_retry",
    "verify_task_health_ui",
    "test_task_target",
    "test_task_health",
    "test_data_root_unified",
    "test_record_export_path",
    "probe_author_card",
    "verify_automation_chain",
]

# 结论行的特征：含 "OK /" 或 "合计问题" 或 "RESULT:" 或弹窗审计的收尾句，
# 或中文结论行（如 test_accounts_discovery 的「合计 74 项：通过 74，失败 0」）。
# ★ 漏掉中文那种写法时，脚本会显示成「未找到结论行」而被判成"有问题" ——
#   明明全绿却报红，等于给套件加了一个恒定的假警报。
SUMMARY = re.compile(
    r"(OK\s*/\s*\d+\s*FAIL|合计问题|RESULT:|FATAL|全部弹窗|有问题弹窗"
    r"|合计\s*\d+\s*项：通过\s*\d+，失败\s*\d+"
    r"|通过\s*\d+\s*/\s*失败\s*\d+)")


def summary_of(name, out):
    """从输出里取结论行；`audit_pages` 走结果文件。"""
    if name == "audit_pages":
        path = os.path.join(HERE, "audit_result.txt")
        if not os.path.exists(path):
            return "（无 audit_result.txt）"
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        ok = sum(1 for ln in lines if ln.startswith("OK "))
        bad = sum(1 for ln in lines if ln.startswith("FAIL"))
        warn = sum(1 for ln in lines if ln.startswith("WARN"))
        # 有 FATAL 时结果文件末尾会带 traceback
        if "FATAL" in "\n".join(lines[-40:]):
            return f"{ok} OK / {bad} FAIL / {warn} WARN  ← 有 FATAL"
        return f"{ok} OK / {bad} FAIL / {warn} WARN"
    hits = [ln.strip() for ln in out.splitlines() if SUMMARY.search(ln)]
    return hits[-1] if hits else "（未找到结论行）"


# `SUITE_VERBOSE=1` → 有问题脚本额外打印输出尾部（诊断用，见下方注释）
_VERBOSE = bool(os.environ.get("SUITE_VERBOSE"))


def main(argv):
    names = argv or DEFAULT
    rows = []
    for nm in names:
        script = os.path.join(HERE, nm + ".py")
        if not os.path.exists(script):
            rows.append((nm, "（脚本不存在）", 0.0, 127))
            continue
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        # ★ `%APPDATA%` / `%LOCALAPPDATA%` 必须可用，否则会**成片地红**且红得莫名其妙：
        #   * `requests` 装在本机**用户 site-packages**
        #     （`%APPDATA%\Python\Python313\site-packages`）→ 变量缺失时
        #     `import requests` 失败 → `import wb_checkin` 失败 →
        #     `_gate_skip` 静默退化成"需要执行" → 门控断言假红；
        #   * 账号发现读 `%APPDATA%\Trae CN\logs`，缺失时**静默 0 个账号**。
        #   从 Git Bash 里跑时环境里往往没有这两个变量（实测 APPDATA=None），
        #   所以这里统一补齐（已存在则原样保留，不覆盖用户环境）。
        #   ⚠️ 别把 APPDATA 指到**不存在**的沙箱目录（曾经写成
        #   `C:/Klein/AppData/Roaming`，那个目录并不存在 → 反而把上面两件事全弄坏）。
        home = os.path.expanduser("~")
        if not env.get("APPDATA"):
            env["APPDATA"] = os.path.join(home, "AppData", "Roaming")
        if not env.get("LOCALAPPDATA"):
            env["LOCALAPPDATA"] = os.path.join(home, "AppData", "Local")
        t0 = time.time()
        try:
            r = subprocess.run([PY, "-u", script], cwd=ROOT, env=env,
                               capture_output=True, timeout=900)
            out = (r.stdout + r.stderr).decode("utf-8", "replace")
            rc = r.returncode
        except subprocess.TimeoutExpired:
            out, rc = "（超时 900s）", 124
        dt = time.time() - t0
        rows.append((nm, summary_of(nm, out), dt, rc))
        print("  %-26s %s" % (nm, rows[-1][1]), flush=True)
        # ★ 诊断用：`SUITE_VERBOSE=1` 时把**有问题脚本**的输出尾部打出来。
        #   为什么需要：套件只留结论行，而「只在这里偶发红」的脚本
        #   （跨脚本交互 / 环境依赖）光看结论行根本查不出是哪一条断言。
        #   默认关闭，不影响正常输出格式。
        if _VERBOSE and ("FAIL" in rows[-1][1] or "未找到结论行" in rows[-1][1]):
            print("    ── %s 输出尾部 ──" % nm, flush=True)
            for ln in out.strip().splitlines()[-40:]:
                print("    " + ln, flush=True)

    print("\n" + "=" * 74)
    print("%-26s %-30s %7s" % ("脚本", "结论", "耗时"))
    print("-" * 74)
    bad = 0
    for nm, s, dt, rc in rows:
        # ★ 用一个布尔累加，别每条规则都 `bad += 1` ——
        #   同一条结论同时命中两条规则时会重复计数（"有问题脚本数" 就虚高了）。
        flag = ""
        is_bad = False
        if ("FAIL" in s and not re.search(r"\b0 FAIL", s)) or "FATAL" in s:
            is_bad = True
        if "WARN" in s and not re.search(r"0 WARN", s):
            is_bad = True
        # 弹窗审计的收尾句是「有问题弹窗 N 个」，不含 FAIL 字样，得单独认
        if "有问题弹窗" in s:
            is_bad = True
        # 中文结论行里的失败数（「合计 N 项：通过 N，失败 N」）
        m = re.search(r"失败\s*(\d+)", s)
        if m and int(m.group(1)) > 0:
            is_bad = True
        if "未找到结论行" in s or "不存在" in s or "超时" in s:
            is_bad = True
        if is_bad:
            flag = "  <<<"
            bad += 1
        print("%-26s %-30s %6.1fs%s" % (nm, s, dt, flag))
    print("=" * 74)
    print("有问题脚本数：%d" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
