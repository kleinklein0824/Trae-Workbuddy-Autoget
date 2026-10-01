#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断脚本：绕过 _show 的 try/except，直接构造页面以拿到完整 traceback。"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PAGES = [
    ("traework", "checkin"), ("traework", "accounts"), ("traework", "logs"),
    ("traework", "settings"), ("traecn", "accounts"), ("workbuddy", "checkin"),
]

OUT = os.path.join(HERE, "diag_result.txt")
LOG = []


def main():
    from app.application import App
    from app import config as C

    app = App()
    app.update()
    # 预热账号缓存，避免后台线程干扰
    try:
        C.accounts_cached(force=True)
    except Exception:
        pass
    app.update()

    for module, view in PAGES:
        key = f"{module}/{view}"
        LOG.append(f"\n{'='*70}\n### {key}\n{'='*70}")
        # 清空 page_host
        for w in app.page_host.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass
        app.module, app.view = module, view
        factory = app._page_factory(view)
        try:
            page = factory(app.page_host, app.theme, app, module)
        except Exception:
            LOG.append("构造期异常:\n" + traceback.format_exc())
            continue
        LOG.append("构造 OK")
        try:
            page.pack(fill="both", expand=True)
        except Exception:
            LOG.append("pack 异常:\n" + traceback.format_exc())
        try:
            if hasattr(page, "on_show"):
                page.on_show()
            LOG.append("on_show OK")
        except Exception:
            LOG.append("on_show 异常:\n" + traceback.format_exc())
        # 跑几轮事件循环，让 after_idle / Configure 生效
        try:
            for _ in range(10):
                app.update()
            LOG.append("update OK")
        except Exception:
            LOG.append("update 异常:\n" + traceback.format_exc())
        try:
            page.destroy()
        except Exception:
            pass

    try:
        app.destroy()
    except Exception:
        pass

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(LOG))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(LOG) + "\n\nFATAL:\n" + traceback.format_exc())
        sys.exit(1)
