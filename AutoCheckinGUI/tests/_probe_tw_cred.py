#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：TraeWork 会话 Cookie 的真实构成 + 桌面端有没有可用令牌。

回答两个问题：
  1. `_traework_expiry` 取「最早到期的那个 Cookie」是否合理？
     —— 看看真实 profile 里到底有哪些 Cookie、分别多久到期。
  2. `TRAE SOLO CN` 桌面端日志里 token 是否真的脱敏？
     —— 决定「能不能全走桌面端捕获」。

★ 全程只读：不写任何文件、不改配置、不启动浏览器。
"""
import os
import re
import sqlite3
import sys
import time

# 脱敏令牌的样子：token/authorization 后面跟着一串星号（可能带前缀）
_MASKED_RE = re.compile(rb'"[A-Za-z_]*(?:[Tt]oken|Authorization)[A-Za-z_]*"\s*:\s*"([^"]{1,80})"')

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # desktop/AutoCheckinGUI
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))  # 项目根


def main():
    from app import config as C

    print("=== 1. TraeWork profile 的 Cookie 明细 ===")
    accs = C.list_traework_accounts() or []
    print("账号数：%d" % len(accs))
    now = time.time()
    for a in accs:
        pdir = C.traework_profile_dir(a.get("profile"))
        print("\n-- %s（logged_in=%s）\n   %s"
              % (a.get("name") or a.get("profile"), a.get("logged_in"), pdir))
        for f in C.traework_cookie_files(pdir):
            if not os.path.exists(f):
                print("   [缺] %s" % f)
                continue
            uri = "file:%s?mode=ro&immutable=1" % f.replace("\\", "/")
            try:
                con = sqlite3.connect(uri, uri=True, timeout=1.0)
                rows = con.execute(
                    "select name, host_key, expires_utc from cookies").fetchall()
                con.close()
            except Exception as e:  # noqa: BLE001
                print("   [读失败] %s -> %r" % (f, e))
                continue
            print("   [%s] %d 条" % (os.path.basename(f), len(rows)))
            items = []
            for name, host, exp in rows:
                ts = (exp / 1000000.0 - 11644473600) if exp else 0
                items.append((ts, name, host))
            items.sort()
            for ts, name, host in items:
                if ts <= 0:
                    left = "会话级（关闭即失效）"
                elif ts <= now:
                    left = "已过期"
                else:
                    left = "%.1f 天" % ((ts - now) / 86400)
                print("      %-34s %-22s %s" % (name, host, left))

    print("\n=== 2. Trae 系客户端日志根 ===")
    import tc_checkin
    for root, client, kind in tc_checkin.client_log_roots():
        print("   %-16s %-8s %s" % (client, kind, root))

    print("\n=== 3. 桌面端日志里的 token 是否脱敏 ===")
    for root, client, kind in tc_checkin.client_log_roots():
        if client == "Trae CN":
            continue
        hits = {"jwt": 0, "masked": 0, "samples": []}
        import pathlib
        p = pathlib.Path(root)
        sessions = sorted((d for d in p.iterdir() if d.is_dir()),
                          key=lambda x: x.stat().st_mtime, reverse=True)[:3]
        for sd in sessions:
            for f in list(sd.rglob("*.log"))[:6]:
                try:
                    blob = f.read_bytes()
                except OSError:
                    continue
                hits["jwt"] += len(tc_checkin.JWT_RE.findall(blob))
                for m in _MASKED_RE.finditer(blob):
                    hits["masked"] += 1
                    if len(hits["samples"]) < 4:
                        hits["samples"].append(m.group(0)[:110])
        print("   %s：明文 JWT %d 个 / 脱敏 token %d 处"
              % (client, hits["jwt"], hits["masked"]))
        for s in hits["samples"]:
            print("        %s" % s)

    print("\n=== 4. scan_client_identity（谁能提供令牌） ===")
    for uid, v in (tc_checkin.scan_client_identity() or {}).items():
        print("   %s -> %s" % (uid, v))


if __name__ == "__main__":
    main()
