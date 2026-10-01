#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：能否**直接**打开 www.trae.cn 拿到 Cloud-IDE-Token（不经头像菜单）。

为什么要问：如果可以直接打开，TraeWork 的签到判定就能改成
「开一个轻量页面 → 取令牌 → 直连接口」，完全绕开
「头像菜单 → 积分按钮 → 新标签页」这条脆弱链路，也绕开明细渲染竞态。
"""
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# 签到核心与 GUI 是同级的 `核心脚本/`（源码模式下的项目根）
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "核心脚本")))
import checkin as C  # noqa: E402
import tw_checkin as T  # noqa: E402

TOKEN_RE = re.compile(r"^ey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
JS = ("() => localStorage.getItem('Cloud-IDE-Token')")
JS_DID = ("() => localStorage.getItem('SOLO_DEVICE_ID')")


def main():
    cfg = C.load_config()
    acc = cfg["traework"]["accounts"][0]
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            T._profile_dir(cfg, acc), headless=True, locale="zh-CN",
            viewport={"width": 1280, "height": 800}, args=T.LAUNCH_ARGS)
        try:
            # 先按正常流程登录一次主页（拿 SSO 会话）
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(T.BASE_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(6000)
            print("主页登录态 =", T._wait_login_state(page, timeout_s=20))

            for url in ("https://www.trae.cn/dashboard",
                        "https://www.trae.cn/",
                        "https://www.trae.cn/dashboard#usage"):
                np = ctx.new_page()
                try:
                    np.goto(url, wait_until="domcontentloaded", timeout=30000)
                    tok = None
                    for i in range(15):
                        try:
                            tok = np.evaluate(JS)
                        except Exception:
                            tok = None
                        if tok and TOKEN_RE.match(str(tok)):
                            print(f"[{url}] 第 {i + 1}s 拿到令牌 OK  "
                                  f"did={np.evaluate(JS_DID)!r}")
                            break
                        time.sleep(1)
                    if not tok:
                        print(f"[{url}] 15s 内没拿到令牌；最终 URL={np.url}")
                    else:
                        import tc_checkin
                        st, body = tc_checkin._post(
                            "status", str(tok), lambda m: None,
                            str(np.evaluate(JS_DID) or "") or None)
                        print(f"    status -> {st} {str(body)[:200]}")
                except Exception as e:  # noqa: BLE001
                    print(f"[{url}] 异常: {e!r}")
                finally:
                    try:
                        np.close()
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
