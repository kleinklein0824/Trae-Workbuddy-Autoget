#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：能不能从 TraeWork 的登录态里拿到 JWT，直接调 checkin_credits 接口。

背景
----
probe_tw_api.py 发现 TraeWork 的积分面板（www.trae.cn/dashboard#usage）
调用的接口主机是 **api.trae.cn** —— 与 TraeCN 完全同一个域名，且出现了
`cloudide/api/v3/common/GetUserToken`。

TraeCN 的签到是直连 `api.trae.cn/trae/api/v2/ug/checkin_credits/status|claim`
（`Authorization: Cloud-IDE-JWT <JWT>`）判定的，稳定可靠。
如果 TraeWork 的页面里也能拿到同款 JWT，那 TraeWork 的判定就可以从
「展开明细找今天+31」这种脆弱的文本判据，升级成**接口直连**。

本脚本验证三步：
  ① 页面里能不能拿到 JWT（GetUserToken / localStorage / sessionStorage）；
  ② 拿到的 JWT 调 checkin_credits/status 返回什么；
  ③ 与页面文本判据（_dashboard_already_checked）是否一致。

跑法（在 AutoCheckinGUI 目录下执行）：
    python tests/probe_tw_token.py
"""
import json
import os
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_GUI_DIR = os.path.dirname(_HERE)
if _GUI_DIR not in sys.path:
    sys.path.insert(0, _GUI_DIR)

import app.config  # noqa: E402
import tw_checkin as T  # noqa: E402
import checkin as C  # noqa: E402

JWT_RE = __import__("re").compile(r"eyJhbGciOi[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

_JS_DUMP_STORAGE = r"""
() => {
  const out = {ls: {}, ss: {}};
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      const v = localStorage.getItem(k) || '';
      out.ls[k] = v.length > 4000 ? v.slice(0, 4000) : v;
    }
  } catch (e) {}
  try {
    for (let i = 0; i < sessionStorage.length; i++) {
      const k = sessionStorage.key(i);
      const v = sessionStorage.getItem(k) || '';
      out.ss[k] = v.length > 4000 ? v.slice(0, 4000) : v;
    }
  } catch (e) {}
  return out;
}
"""

_JS_GET_TOKEN = r"""
async () => {
  try {
    const r = await fetch('https://api.trae.cn/cloudide/api/v3/common/GetUserToken',
                          {method: 'POST', credentials: 'include',
                           headers: {'Content-Type': 'application/json'},
                           body: JSON.stringify({ReqSource: 'IDE'})});
    return {status: r.status, text: (await r.text()).slice(0, 4000)};
  } catch (e) {
    return {status: -1, text: String(e)};
  }
}
"""


def probe_status(token, did=None):
    url = "https://api.trae.cn/trae/api/v2/ug/checkin_credits/status"
    h = {"Content-Type": "application/json",
         "Authorization": f"Cloud-IDE-JWT {token}",
         "x-app-version": "3.3.99"}
    if did:
        h["x-device-id"] = did
    req = urllib.request.Request(url, data=json.dumps({"req_source": 1}).encode(),
                                method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        return -1, repr(e)


def main():
    cfg = C.load_config()
    accs = cfg.get("traework", {}).get("accounts", [])
    if not accs:
        print("config.json 里没有 traework 账号")
        return 1
    acc = accs[0]
    from datetime import date
    today = date.today().isoformat()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            T._profile_dir(cfg, acc), headless=True, locale="zh-CN",
            viewport={"width": 1280, "height": 800}, args=T.LAUNCH_ARGS)
        tokens = []
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(T.BASE_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(6000)
            print("主页登录态 =", T._wait_login_state(page, timeout_s=20))

            dash = T._open_points_dashboard(ctx, page, lambda m: print(m), "Klein")
            if dash is None:
                print("积分面板打不开")
                return 1
            dash.wait_for_timeout(9000)
            print("面板 URL =", dash.url)

            # ① 页面文本判据（现有实现）
            text = T._page_text(dash)
            print("\n[① 文本判据] _dashboard_already_checked =",
                  T._dashboard_already_checked(text, today))

            # ② GetUserToken
            print("\n[② GetUserToken]")
            res = dash.evaluate(_JS_GET_TOKEN)
            print("  status =", res.get("status"))
            print("  body   =", str(res.get("text"))[:600])

            # ③ storage 里的 JWT
            print("\n[③ storage 扫描]")
            dump = dash.evaluate(_JS_DUMP_STORAGE)
            for scope in ("ls", "ss"):
                for k, v in (dump.get(scope) or {}).items():
                    hit = JWT_RE.search(str(v))
                    flag = "  <<< 含 JWT" if hit else ""
                    print(f"  [{scope}] {k}  len={len(str(v))}{flag}")
                    if hit:
                        tokens.append(hit.group(0))
            if not tokens:
                # 退一步：整个 storage 大 blob 里搜
                blob = json.dumps(dump)
                tokens = JWT_RE.findall(blob)
                print("  兜底全局搜索 JWT 数量 =", len(tokens))
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass

    # ④ 用拿到的 token 直连接口
    print(f"\n[④ 接口直连] 候选 token {len(tokens)} 个")
    did = None
    try:
        import tc_checkin
        did = tc_checkin._device_id(lambda m: None)
        print("  设备 ID =", did)
    except Exception as e:  # noqa: BLE001
        print("  取设备 ID 失败:", repr(e))
    for i, tok in enumerate(tokens[:3]):
        st, body = probe_status(tok, did)
        print(f"  token[{i}] status={st} body={body[:400]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
