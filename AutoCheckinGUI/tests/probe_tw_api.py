#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：TraeWork 的「签到 / 积分」走哪个接口（能不能像 TraeCN 一样直连）。

动机
----
TraeWork 的签到判定目前**完全依赖页面渲染**（头像菜单 → 积分面板 →
展开「每日签到」明细 → 找「到期日 == 今天+31」的条目）。这条链路有两个
天生弱点：
  ① 明细懒加载 + 异步刷新，条目可能比页面晚到（2026-09-18 09:00 实测：
     明细已渲染 9 条却没有今日条目，09:30 重试才看到）；
  ② 页面一改版，文本判据全废。
而 TraeCN 是**直连接口**判定（`checkin_credits/status`），稳定得多。

本脚本只**记录请求 URL**（不读响应体 —— 在 response 钩子里同步读 body
会把页面渲染卡死，实测会让登录态被误读成 out），把页面加载过程中的全部
非静态请求 dump 出来，找找有没有 status / claim 这一类接口。

跑法（在 AutoCheckinGUI 目录下执行）：
    python tests/probe_tw_api.py
产物：logs/screenshots/explore/tw_api_report.txt
"""
import io
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_GUI_DIR = os.path.dirname(_HERE)
if _GUI_DIR not in sys.path:
    sys.path.insert(0, _GUI_DIR)

import app.config  # noqa: E402
import tw_checkin as T  # noqa: E402
import checkin as C  # noqa: E402

OUT = os.path.join(C.LOG_DIR, "screenshots", "explore")
os.makedirs(OUT, exist_ok=True)

SKIP_RE = re.compile(
    r"\.(js|css|png|jpe?g|gif|svg|woff2?|ttf|ico|webp|map|json)(\?|$)", re.I)
INTEREST_RE = re.compile(
    r"checkin|check_in|credit|point|reward|benefit|daily|score|integral|sign",
    re.I)

SEEN = {}      # "METHOD url" -> count
PAGES = {}     # page url -> [resources]


def _short(u, n=200):
    return u if len(u) <= n else u[:n] + "…"


def main():
    cfg = C.load_config()
    accs = cfg.get("traework", {}).get("accounts", [])
    if not accs:
        print("config.json 里没有 traework 账号")
        return 1
    acc = accs[0]
    print("账号 =", acc.get("name"), " profile =", T._profile_dir(cfg, acc))

    from playwright.sync_api import sync_playwright

    def log(msg):
        print(msg, flush=True)

    def hook(page, tag):
        def on_request(req):
            u = req.url
            if SKIP_RE.search(u) or u.startswith("data:") or u.startswith("blob:"):
                return
            k = f"{req.method} {u.split('#')[0]}"
            SEEN[k] = SEEN.get(k, 0) + 1
            if INTEREST_RE.search(u):
                print(f"  >> [{tag}] {req.method} {_short(u)}")
        page.on("request", on_request)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            T._profile_dir(cfg, acc), headless=True, locale="zh-CN",
            viewport={"width": 1280, "height": 800}, args=T.LAUNCH_ARGS)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            hook(page, "home")
            print("\n=== 打开主页 work.trae.cn ===")
            page.goto(T.BASE_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(7000)
            st = T._wait_login_state(page, timeout_s=20)
            print("登录态 =", st, " URL =", page.url)
            if st != "in":
                print("（登录态非 in，仍继续抓请求）")

            # 主页 performance 资源（含 XHR/fetch）
            try:
                res = page.evaluate(
                    "() => performance.getEntriesByType('resource')"
                    ".map(r => r.initiatorType + ' ' + r.name)")
                PAGES[page.url] = res
                print(f"主页资源条目 {len(res)} 个，疑似接口：")
                for r in res:
                    if INTEREST_RE.search(r) or "api" in r.lower():
                        print("   ", _short(r, 170))
            except Exception as e:  # noqa: BLE001
                print("取 performance 失败:", repr(e))

            print("\n=== 打开积分面板 ===")
            dash = T._open_points_dashboard(ctx, page, log, "Klein")
            if dash is None:
                print("积分面板打不开")
            else:
                hook(dash, "dash")
                try:
                    dash.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception as e:  # noqa: BLE001
                    print("reload 失败:", repr(e))
                dash.wait_for_timeout(8000)
                print("面板 URL =", dash.url)
                try:
                    res2 = dash.evaluate(
                        "() => performance.getEntriesByType('resource')"
                        ".map(r => r.initiatorType + ' ' + r.name)")
                    PAGES[dash.url] = res2
                    print(f"面板资源条目 {len(res2)} 个，疑似接口：")
                    for r in res2:
                        if INTEREST_RE.search(r) or "api" in r.lower():
                            print("   ", _short(r, 170))
                except Exception as e:  # noqa: BLE001
                    print("取面板 performance 失败:", repr(e))
                # 展开明细（可能触发状态接口）
                try:
                    if dash.evaluate(T._JS_MARK_SIGN_ROW):
                        dash.click("[data-cb-signrow]", timeout=4000)
                        print("已点击展开「每日签到」")
                        dash.wait_for_timeout(7000)
                except Exception as e:  # noqa: BLE001
                    print("展开失败:", repr(e))
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass

    path = os.path.join(OUT, "tw_api_report.txt")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write("===== 全部非静态请求（去重计数）=====\n")
        for k in sorted(SEEN):
            f.write(f"{SEEN[k]:4d}  {k}\n")
        for pu, res in PAGES.items():
            f.write(f"\n===== {pu} 的 performance 资源 =====\n")
            for r in res:
                f.write(r + "\n")
    print(f"\n报告已写入：{path}")
    print(f"共记录 {len(SEEN)} 个非静态请求")
    return 0


if __name__ == "__main__":
    sys.exit(main())
