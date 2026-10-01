#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实验：用**生产的时序**展开「每日签到」，看点击是不是空操作。

假设
----
`_do_checkin` 里打开积分面板后只隔 ~3 秒就去点「每日签到」展开（生产日志实测
12:07:47 开面板 → 12:07:50 点击）。而「每日签到」这一行的**文本**在明细数据
到达之前就已经存在，于是可能：
  * 点击落在尚未绑定处理器的行上 → **空操作**，面板始终折叠，
    后续 10 秒轮询当然等不到明细 → 误报"未找到签到入口"；
  * 或者点开了，但明细要 >10 秒才渲染 → 同样是等得不够。

这个脚本把「点击后每秒的文本长度 / 明细条目数」打出来，直接区分：
  - 若点击后条目数**始终为 0** → 点击是空操作（要点校验 + 重点）
  - 若条目数在 N 秒后出现且 N > 10 → 只是等待窗口太短

跑法：python tests/probe_tw_expand.py [等待秒数，默认 30]（在 desktop/AutoCheckinGUI 下执行）
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

WAIT_S = int(sys.argv[1]) if len(sys.argv) > 1 else 30


def entries(text):
    return len(re.findall(r"签到奖励", text))


def main():
    cfg = C.load_config()
    accs = cfg.get("traework", {}).get("accounts", [])
    acc = accs[0]
    today = _date.today().isoformat()

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
            if T._wait_login_state(page, timeout_s=15) != "in":
                print("未登录")
                return 1

            t0 = time.time()
            dash = T._open_points_dashboard(ctx, page, log, "Klein")
            if dash is None:
                print("面板打不开")
                return 1
            print("  面板已打开（t=%.1fs）" % (time.time() - t0))

            # ---- 复刻生产时序：等「签到」立刻命中就停（通常 0 次 sleep）----
            sleeps = 0
            for _ in range(15):
                tx = T._page_text(dash)
                if "签到" in tx:
                    break
                time.sleep(1)
                sleeps += 1
            print("  等「签到」用了 %d 次 sleep" % sleeps)

            # 生产里这一步会跑；本实验只报告它会不会点东西
            try:
                cands = dash.evaluate(T._JS_MARK_SIGNS) or []
            except Exception as e:  # noqa: BLE001
                cands = []
                print("  _JS_MARK_SIGNS 异常:", repr(e))
            print("  签到类候选按钮数 =", len(cands),
                  [c["text"][:10] for c in cands[:5]])

            # ---- 立刻点展开（与生产一致）----
            marked = bool(dash.evaluate(T._JS_MARK_SIGN_ROW))
            print("  标记到「每日签到」行 =", marked)
            t_click = time.time() - t0
            clicked = False
            if marked:
                try:
                    dash.click("[data-cb-signrow]", timeout=4000)
                    clicked = True
                except Exception as e:  # noqa: BLE001
                    print("  点击失败:", repr(e))
            print("  点击展开 at t=%.1fs  clicked=%s" % (t_click, clicked))

            # ---- 每秒采样 ----
            print("\n  t(s)  文本长度  明细条目  已判今日已签")
            first_hit = None
            first_entry = None
            for i in range(1, WAIT_S + 1):
                time.sleep(1)
                tx = T._page_text(dash)
                n = entries(tx)
                hit = T._dashboard_already_checked(tx, today)
                if n and first_entry is None:
                    first_entry = i
                if hit and first_hit is None:
                    first_hit = i
                # 只在有变化时打印，避免刷屏
                if i <= 6 or n != 0 and (first_entry is None or i - first_entry < 3) \
                        or hit or i == WAIT_S:
                    print("  %4d  %8d  %8d  %s" % (i, len(tx), n, hit))
            dash.screenshot(path=os.path.join(OUT, "tw_expand_prod_timing.png"),
                            full_page=True)
            with io.open(os.path.join(OUT, "tw_expand_prod_timing.txt"), "w",
                         encoding="utf-8") as f:
                f.write(T._page_text(dash))

            print("\n  === 结论 ===")
            print("  明细首次出现于第 %s 秒" % first_entry)
            print("  今日已签首次命中于第 %s 秒" % first_hit)
            if first_entry is None:
                print("  → 点击后明细**始终没出现**：点击很可能是空操作（行未就绪）")
            elif first_hit is None:
                print("  → 明细出现了但没有今天+31的条目：今天确实没签成")
            else:
                print("  → 明细出现于第 %s 秒（生产只等 10s）" % first_entry)
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
