#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：`scan_client_identity.has_token` 与 `_scan_ide_logs` 的口径差异。

现象：`scan_client_identity()` 对 TRAE SOLO CN 报 has_token=True，
      但该目录里 token 明明是 `"token":"******"`（全脱敏）。
本探针把该目录里所有 JWT_RE 命中的串逐个解码，看它们**能不能真的用来签到**
（要求载荷里有 uid 且有未过期的 exp）。
"""
import json
import os
import pathlib
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))


def main():
    from app import config as _C  # noqa: F401  ★ 先把项目根挂上 sys.path
    import tc_checkin as T

    for root, client, kind in T.client_log_roots():
        print("\n=== %s（%s）%s" % (client, kind, root))
        p = pathlib.Path(root)
        sessions = sorted((d for d in p.iterdir() if d.is_dir()),
                          key=lambda x: x.stat().st_mtime, reverse=True)[:8]
        total = 0
        usable = 0
        for sd in sessions:
            for f in sd.rglob("*.log"):
                try:
                    if f.stat().st_size > 80_000_000:
                        continue
                    blob = f.read_bytes()
                except OSError:
                    continue
                for m in T.JWT_RE.finditer(blob):
                    tok = m.group(0).decode("ascii", "ignore")
                    info = T._token_payload(tok)
                    total += 1
                    if not info:
                        continue
                    left = (info["exp"] - time.time()) / 3600.0
                    if left > 0:
                        usable += 1
                        print("   [可用] uid=%s 剩 %.1f 小时  %s…"
                              % (info["uid"], left, tok[:28]))
                    else:
                        print("   [已过期] uid=%s  %s…" % (info["uid"], tok[:28]))
        print("   JWT 形态命中 %d 个，其中**可解析且未过期** %d 个" % (total, usable))

    print("\n=== _scan_ide_logs 实际拿到什么 ===")
    got = T._scan_ide_logs(lambda m: None) or {}
    if not got:
        print("   （0 个）")
    for uid, v in got.items():
        print("   %s -> exp 剩 %.1f 小时" % (uid, (v[1] - time.time()) / 3600.0))

    print("\n=== 各客户端单独跑 _scan_ide_logs（分目录归因） ===")
    for root, client, kind in T.client_log_roots():
        best = {}
        p = pathlib.Path(root)
        sessions = sorted((d for d in p.iterdir() if d.is_dir()),
                          key=lambda x: x.stat().st_mtime, reverse=True)[:8]
        for sd in sessions:
            for f in sd.rglob("*.log"):
                try:
                    if f.stat().st_size > 80_000_000:
                        continue
                    mtime = f.stat().st_mtime
                    blob = f.read_bytes()
                except OSError:
                    continue
                for m in T.JWT_RE.finditer(blob):
                    tok = m.group(0).decode("ascii", "ignore")
                    info = T._token_payload(tok)
                    if not info or info["exp"] < time.time():
                        continue
                    saved_at = mtime + m.start() * 1e-9
                    cur = best.get(info["uid"])
                    if not cur or (saved_at, info["exp"]) > (cur[2], cur[1]):
                        best[info["uid"]] = (tok, info["exp"], saved_at)
        print("   %-16s → %d 个可用令牌 %s"
              % (client, len(best), list(best.keys())))


if __name__ == "__main__":
    main()
