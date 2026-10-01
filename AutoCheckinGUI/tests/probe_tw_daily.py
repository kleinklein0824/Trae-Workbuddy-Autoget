#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：TraeWork「每日签到」明细到底长什么样（不依赖截图肉眼看）。

背景
----
2026-09-13 / 14 / 15 / 17 都出现过同一条失败：

    [traework] Klein: fail — 未找到签到入口且无法确认已签到（入口可能未开放或页面变化）

而同一天重试又能成功（详情「今日已签到（签到明细确认已领取）」）。
说明这是**判定环节**的问题，不是真的没签到。但现场截图只拍到**展开之前**
的状态（`_do_checkin` 里 `_shot(dash, ...)` 在点击展开之前），
看不到明细里的到期日期 —— 于是无法定性。

这个脚本把「展开后」的完整文本抓下来，并逐条列出 `签到奖励` 后面那个
日期与「今天+31」的差值，从而区分三种可能：
  A. 明细里**有**今天+31 的条目 → 判定逻辑/时序有问题（该修判定）
  B. 明细里**没有**、最新一条是今天+30（即昨天签的） → 今天确实没签成
  C. 明细根本没渲染出来 → 时序问题（该加强等待）

跑法：python tests/probe_tw_daily.py（在 desktop/AutoCheckinGUI 下执行）
"""
import io
import os
import re
import sys
import time
from datetime import date as _date

# 本文件在 desktop/AutoCheckinGUI/tests/ 下：先把 GUI 目录挂上 sys.path，
# 再由 app.config 定位项目根（与 test_tw_sign_detect.py 同一套引导）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_GUI_DIR = os.path.dirname(_HERE)
if _GUI_DIR not in sys.path:
    sys.path.insert(0, _GUI_DIR)

import app.config  # noqa: E402  定位项目根并挂上 sys.path
import tw_checkin as T  # noqa: E402
import checkin as C  # noqa: E402

# 截图落到项目根的 logs/screenshots/explore
OUT = os.path.join(C.LOG_DIR, "screenshots", "explore")
os.makedirs(OUT, exist_ok=True)


def dump_dates(tag, text):
    """列出所有 `签到奖励` 后面第一个日期，以及与今天的差值。"""
    today = _date.today()
    rows = []
    for m in re.finditer(r"签到奖励", text):
        tail = text[m.end():m.end() + 60]
        dm = T.DATE_TEXT_RE.search(tail)
        if dm:
            d = T._parse_cn_date(dm.group(0))
            if d:
                rows.append((d, (d - today).days, tail.strip()[:46]))
    print(f"  [{tag}] 找到 {len(rows)} 条「签到奖励」条目：")
    for d, delta, tail in rows:
        mark = ""
        if delta == 31:
            mark = "   <<< 今天+31（判定为今日已签）"
        elif delta == 30:
            mark = "   <<< 今天+30（= 昨天签的）"
        print(f"      {d}  (今天{delta:+d}天){mark}")
        print(f"         {tail}")
    if not rows:
        print("      （没有任何「签到奖励」条目 —— 明细没渲染，或文案变了）")
    return rows


def main():
    cfg = C.load_config()
    accs = cfg.get("traework", {}).get("accounts", [])
    if not accs:
        print("config.json 里没有 traework 账号")
        return 1
    acc = accs[0]
    today = _date.today().isoformat()
    print("今天 =", today, " 账号 =", acc.get("name"))

    from playwright.sync_api import sync_playwright

    def log(msg):
        print(msg, flush=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            T._profile_dir(cfg, acc), headless=True, locale="zh-CN",
            viewport={"width": 1280, "height": 800}, args=T.LAUNCH_ARGS)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(T.BASE_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            st = T._wait_login_state(page, timeout_s=15)
            print("登录态 =", st)
            if st != "in":
                print("未登录 → 无法继续诊断")
                return 1

            # ---- 主页文本（路径 0 的判据）----
            home = T._page_text(page)
            print("\n[主页] DONE_TEXT_RE 命中 =",
                  bool(T.DONE_TEXT_RE.search(home)))
            page.screenshot(path=os.path.join(OUT, "tw_home.png"))

            # ---- 打开积分面板（第 1 次，模拟真实流程）----
            print("\n=== 第 1 次打开积分面板 ===")
            dash = T._open_points_dashboard(ctx, page, log, "Klein")
            if dash is None:
                print("积分面板打不开（网络/入口问题）")
                return 1
            # 等「签到」区块渲染，最长 15s（与 _do_checkin 一致）
            text1 = ""
            for i in range(15):
                text1 = T._page_text(dash)
                if "签到" in text1:
                    break
                time.sleep(1)
            dash.screenshot(path=os.path.join(OUT, "tw_dash_collapsed.png"),
                            full_page=True)
            with io.open(os.path.join(OUT, "tw_dash_collapsed.txt"), "w",
                         encoding="utf-8") as f:
                f.write(text1)
            print("  折叠态文本长度 =", len(text1),
                  " 含「每日签到」 =", "每日签到" in text1)
            print("  折叠态已判为今日已签 =",
                  T._dashboard_already_checked(text1, today))
            dump_dates("折叠态", text1)

            # ---- 展开「每日签到」----
            print("\n=== 展开「每日签到」 ===")
            marked = False
            try:
                marked = bool(dash.evaluate(T._JS_MARK_SIGN_ROW))
            except Exception as e:  # noqa: BLE001
                print("  标记行失败:", repr(e))
            print("  标记到「每日签到」行 =", marked)
            if marked:
                try:
                    dash.click("[data-cb-signrow]", timeout=4000)
                    print("  已点击展开")
                except Exception as e:  # noqa: BLE001
                    print("  点击失败:", repr(e))
                # 与 _do_checkin 一致：最多等 10s
                hit_at = None
                text2 = ""
                for i in range(10):
                    time.sleep(1)
                    text2 = T._page_text(dash)
                    if T._dashboard_already_checked(text2, today):
                        hit_at = i + 1
                        break
                dash.screenshot(path=os.path.join(OUT, "tw_dash_expanded.png"),
                                full_page=True)
                with io.open(os.path.join(OUT, "tw_dash_expanded.txt"), "w",
                             encoding="utf-8") as f:
                    f.write(text2)
                print("  展开态文本长度 =", len(text2),
                      "（折叠态 %d）" % len(text1))
                print("  展开态已判为今日已签 =",
                      T._dashboard_already_checked(text2, today),
                      "命中于第 %s 秒" % hit_at)
                dump_dates("展开态", text2)
                # 有没有可点击的签到/领取入口
                print("\n  [展开态] 文本里是否出现「签到」类可点击词：")
                for kw in ("立即签到", "去签到", "每日签到", "签到领",
                           "领取", "已签到", "已领取", "明天再来"):
                    if kw in text2:
                        print("     命中:", kw)
            try:
                dash.close()
            except Exception:  # noqa: BLE001
                pass

            # ---- 第 2 次打开（模拟重试）----
            print("\n=== 第 2 次打开积分面板（重试）===")
            time.sleep(5)
            dash2 = T._open_points_dashboard(ctx, page, log, "Klein")
            if dash2 is not None:
                text3 = ""
                for i in range(15):
                    text3 = T._page_text(dash2)
                    if "签到" in text3:
                        break
                    time.sleep(1)
                print("  第2次折叠态已判为今日已签 =",
                      T._dashboard_already_checked(text3, today))
                dump_dates("第2次折叠态", text3)
                if "每日签到" in text3 and dash2.evaluate(T._JS_MARK_SIGN_ROW):
                    dash2.click("[data-cb-signrow]", timeout=4000)
                    for i in range(10):
                        time.sleep(1)
                        text3 = T._page_text(dash2)
                        if T._dashboard_already_checked(text3, today):
                            print("  第2次展开后命中（第 %d 秒）" % (i + 1))
                            break
                    dash2.screenshot(path=os.path.join(OUT, "tw_dash2_expanded.png"),
                                     full_page=True)
                    dump_dates("第2次展开态", text3)
                try:
                    dash2.close()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass

    print("\n产物目录:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
