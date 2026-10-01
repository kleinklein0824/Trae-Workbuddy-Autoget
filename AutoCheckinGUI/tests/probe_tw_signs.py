#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测：按「修复后」的写法，签到类候选按钮在真实页面上到底有哪些。

为什么要先探测再改
------------------
`_JS_MARK_SIGNS` 现在因为把 Python 正则裸插进 JS 而**语法报错**，
永远返回 0 个候选 → 「点击签到按钮」这条兜底路径**从未生效**。
修好之后它会真的开始点东西，所以**必须先看清它会点到什么**，
避免"修好一个 bug，引入一个乱点"。

修复后的写法：JS 只负责**收集可见文本**（不做正则），
正则过滤放回 Python（`SIGN_TEXT_RE`）—— 这样正则只有一处定义、可离线单测，
也不可能再产生 JS 语法错误。

跑法：python tests/probe_tw_signs.py（在 desktop/AutoCheckinGUI 下执行）
"""
import os
import re
import sys
import time

# 本文件在 desktop/AutoCheckinGUI/tests/ 下：先把 GUI 目录挂上 sys.path，
# 再由 app.config 定位项目根（与 test_tw_sign_detect.py 同一套引导）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_GUI_DIR = os.path.dirname(_HERE)
if _GUI_DIR not in sys.path:
    sys.path.insert(0, _GUI_DIR)

import app.config  # noqa: E402  定位项目根并挂上 sys.path
import tw_checkin as T  # noqa: E402
import checkin as C  # noqa: E402

# 修复后的 JS：不嵌正则，只收集候选文本 + 打标记
_JS_COLLECT = r"""
() => {
  const out = [];
  const seen = new Set();
  let i = 0;
  for (const el of document.querySelectorAll('button, a, [role="button"], span, div, p, li')) {
    if (el.children.length > 2) continue;
    const t = (el.innerText || '').trim();
    if (!t || t.length > %d || seen.has(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.pointerEvents === 'none') continue;
    el.setAttribute('data-cb-sign', 's' + i);
    out.push({ text: t, mark: 's' + i });
    i++; seen.add(t);
  }
  return out;
}
""" % T.TEXT_LIMIT


def main():
    cfg = C.load_config()
    acc = cfg.get("traework", {}).get("accounts", [])[0]

    from playwright.sync_api import sync_playwright

    def log(m):
        print(m, flush=True)

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

            def report(tag, pg):
                try:
                    allc = pg.evaluate(_JS_COLLECT) or []
                except Exception as e:  # noqa: BLE001
                    print("  [%s] 收集失败: %r" % (tag, e))
                    return
                hit = [c for c in allc if T.SIGN_TEXT_RE.fullmatch(c["text"])]
                print("  [%s] 页面可见短文本 %d 个，其中**命中签到正则**的 %d 个："
                      % (tag, len(allc), len(hit)))
                for c in hit:
                    print("       %-14r  mark=%s" % (c["text"], c["mark"]))
                if not hit:
                    print("       （无）")
                # 顺带看看差点命中的（含「签到」/「领取」但不匹配）
                near = [c["text"] for c in allc
                        if ("签到" in c["text"] or "领取" in c["text"])
                        and not T.SIGN_TEXT_RE.fullmatch(c["text"])]
                if near:
                    print("       含关键词但**不**命中的（仅供参考）:",
                          [t[:16] for t in near[:12]])

            print("\n=== 主页（路径 1）===")
            report("home", page)

            print("\n=== 积分面板（路径 2/3）===")
            dash = T._open_points_dashboard(ctx, page, log, "Klein")
            if dash is None:
                print("面板打不开")
                return 1
            for _ in range(15):
                if "签到" in T._page_text(dash):
                    break
                time.sleep(1)
            report("dash-折叠", dash)
            # 展开后再看一次
            if dash.evaluate(T._JS_MARK_SIGN_ROW):
                dash.click("[data-cb-signrow]", timeout=4000)
                time.sleep(3)
                report("dash-展开", dash)
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
