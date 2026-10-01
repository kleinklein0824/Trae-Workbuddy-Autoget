#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐页遍历并收集所有 UI 报错（含 Tkinter 回调内的异常）。

不依赖 stdout 管道：结果写入文件，异常通过 report_callback_exception 捕获。
用法：python tests/audit_pages.py
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

OUT = os.path.join(HERE, "audit_result.txt")

PAGES = [
    ("traework", "dashboard"), ("traework", "checkin"),
    ("traework", "accounts"), ("traework", "credits"),
    ("traecn", "dashboard"), ("traecn", "checkin"),
    ("traecn", "accounts"), ("traecn", "credits"),
    ("workbuddy", "dashboard"), ("workbuddy", "checkin"),
    ("workbuddy", "accounts"), ("workbuddy", "credits"),
    ("traework", "logs"), ("traework", "settings"),
    ("traework", "api-service"),
]

LOG = []
_TRACE = os.path.join(HERE, "_trace.txt")
_TR = open(_TRACE, "w", encoding="utf-8", buffering=1)


def p(msg):
    LOG.append(msg)
    _TR.write(msg.splitlines()[0] + "\n")


def _page_error_text(app):
    """检查 page_host 里是否渲染了「页面加载失败」的错误块（真实 bug 的迹象）。"""
    try:
        for w in app.page_host.winfo_children():
            for sub in w.winfo_children():
                try:
                    txt = sub.cget("text")
                except Exception:  # noqa: BLE001
                    continue
                if txt and "页面加载失败" in str(txt):
                    return str(txt)[:200]
    except Exception:  # noqa: BLE001
        pass
    return None


def main():
    from app.application import App

    app = App()
    # 捕获 Tkinter 回调内的异常（这类异常不会冒泡到 _show 的 try）
    cb_errors = []

    def report(exc, val, tb):
        cb_errors.append("".join(traceback.format_exception(exc, val, tb)))

    app.report_callback_exception = report
    app.update()

    for module, view in PAGES:
        key = f"{module}/{view}"
        cb_errors.clear()
        try:
            if module != app.module:
                app._switch_module(module)
                app.update()
            app.goto(module, view)
            for _ in range(8):
                app.update()
            err_text = _page_error_text(app)
            if err_text:
                p(f"FAIL {key}: 页面渲染出错 -> {err_text}")
            elif cb_errors:
                p(f"WARN {key}: 回调异常 {len(cb_errors)} 处")
                for e in cb_errors[:2]:
                    p("     " + e.strip().replace("\n", "\n     "))
            else:
                p(f"OK   {key}")
        except Exception:
            p(f"FAIL {key}\n     " + traceback.format_exc().replace("\n", "\n     "))

    # 主题来回切换
    for dark in (True, False, True):
        cb_errors.clear()
        try:
            app.set_theme(dark)
            for _ in range(5):
                app.update()
            if cb_errors:
                p(f"WARN theme(dark={dark}): 回调异常 {len(cb_errors)} 处")
                for e in cb_errors[:2]:
                    p("     " + e.strip().replace("\n", "\n     "))
            else:
                p(f"OK   theme(dark={dark})")
        except Exception:
            p(f"FAIL theme(dark={dark})\n     "
              + traceback.format_exc().replace("\n", "\n     "))

    # 深色主题下再遍历一遍页面
    app.set_theme(True)
    app.update()
    for module, view in PAGES:
        key = f"dark {module}/{view}"
        cb_errors.clear()
        try:
            if module != app.module:
                app._switch_module(module)
                app.update()
            app.goto(module, view)
            for _ in range(8):
                app.update()
            err_text = _page_error_text(app)
            if err_text:
                p(f"FAIL {key}: 页面渲染出错 -> {err_text}")
            elif cb_errors:
                p(f"WARN {key}: 回调异常 {len(cb_errors)} 处")
                for e in cb_errors[:2]:
                    p("     " + e.strip().replace("\n", "\n     "))
            else:
                p(f"OK   {key}")
        except Exception:
            p(f"FAIL {key}\n     " + traceback.format_exc().replace("\n", "\n     "))

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
