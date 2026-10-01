#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：Trae 桌面端（TRAE SOLO CN）磁盘上有没有**明文可用令牌**。

决定「TraeWork 能不能也走桌面端捕获」。

★ 为什么不能全量扫：该目录有 7 万多个文件，全量读取会被 I/O 拖死
  （第一次这么写，进程直接被杀）。所以只扫**最可能存令牌**的位置：
  Chromium 的 Local/Session Storage(leveldb)、顶层 *.json、solo-lite* 等，
  并设总量上限。
★ 只读：不写、不改、不删。
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
# 只走这些子目录（Chromium 存登录态的地方）
SUBDIRS = ("Local Storage", "Session Storage", "solo-lite",
           "solo-lite-default-workspace", "aha", "ahanet", "User", "Network",
           "WebStorage", "ModularData", "IndexedDB")
SKIP = {"Cache", "Code Cache", "GPUCache", "Crashpad", "DawnCache",
        "DawnGraphiteCache", "DawnWebGPUCache", "ShaderCache",
        "Service Worker", "logs", "Backups"}
MAX_BYTES = 300 * 1024 * 1024      # 总读取上限
MAX_FILE = 8 * 1024 * 1024


def decode(tok):
    import base64
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        d = json.loads(base64.urlsafe_b64decode(p))
        inner = d.get("data", d)
        return str(inner.get("id") or ""), int(d.get("exp") or 0)
    except Exception:  # noqa: BLE001
        return "", 0


def main():
    from app import config as _C  # noqa: F401
    import tc_checkin as T

    now = time.time()
    for root, client, kind in T.client_log_roots():
        if client == "Trae CN":
            continue
        base = os.path.dirname(root)
        print("=== %s（%s）  根 %s" % (client, kind, base))
        if not os.path.isdir(base):
            print("   （不存在）")
            continue

        targets = []
        for sub in SUBDIRS:
            d = os.path.join(base, sub)
            if os.path.isdir(d):
                targets.append(d)
        for fn in os.listdir(base):
            p = os.path.join(base, fn)
            if os.path.isfile(p):
                targets.append(p)

        read_bytes = 0
        nfiles = 0
        hits = []
        for t in targets:
            walk = ([(t, [], [])] if os.path.isfile(t) else os.walk(t))
            for dp, dn, fn in walk:
                dn[:] = [d for d in dn if d not in SKIP]
                for f in fn:
                    if read_bytes > MAX_BYTES:
                        break
                    p = os.path.join(dp, f)
                    try:
                        s = os.path.getsize(p)
                        if s > MAX_FILE:
                            continue
                        blob = open(p, "rb").read()
                    except OSError:
                        continue
                    read_bytes += len(blob)
                    nfiles += 1
                    for m in JWT_RE.finditer(blob):
                        hits.append((os.path.relpath(p, base),
                                     *decode(m.group(0).decode("ascii", "ignore"))))

        print("   扫了 %d 个文件（%.0f MB），命中 JWT %d 处"
              % (nfiles, read_bytes / 1048576.0, len(hits)))
        for rel, uid, exp in hits[:15]:
            if exp:
                left = (exp - now) / 3600.0
                print("      %-60s uid=%s 剩 %s"
                      % (rel[:60], uid or "?", "%.1f 小时" % left if left > 0 else "已过期"))
            else:
                print("      %-60s （无 uid/exp，非签到令牌）" % rel[:60])
        if not hits:
            print("      → 该客户端磁盘上**没有任何明文 JWT**")


if __name__ == "__main__":
    main()
