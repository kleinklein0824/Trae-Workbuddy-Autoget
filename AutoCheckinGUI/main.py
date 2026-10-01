#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""程序入口：python main.py

GUI 版本入口。直接双击 `启动GUI.vbs`（无控制台）或运行本文件即可。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CORE = os.path.abspath(os.path.join(_HERE, "..", "核心脚本"))
for _path in (_CORE, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def main():
    # 高 DPI：避免界面模糊，同时保证坐标按物理像素计算
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:  # noqa: BLE001
                pass
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    try:
        from app.application import main as run_app
    except ImportError as e:
        print(f"导入失败：{e!r}")
        print("请确认已安装依赖：pip install -r requirements.txt")
        input("按回车退出…")
        return 1

    # 依赖自检：签到模块需要 requests / playwright，缺失时给出明确指引
    missing = []
    for mod, pkg in (("requests", "requests"), ("playwright", "playwright")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print("缺少依赖：" + ", ".join(missing))
        print("请在项目根目录执行：pip install -r requirements.txt")
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(
                "缺少依赖",
                "以下依赖尚未安装，GUI 无法调用签到模块：\n\n  "
                + "\n  ".join(missing)
                + "\n\n请在项目根目录执行：\n  pip install -r requirements.txt")
            r.destroy()
        except Exception:  # noqa: BLE001
            input("按回车退出…")
        return 1

    try:
        run_app()
    except Exception:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        print(tb)
        try:
            log = os.path.join(_HERE, "startup_error.log")
            with open(log, "a", encoding="utf-8") as f:
                import datetime
                f.write(f"\n===== {datetime.datetime.now()} =====\n{tb}")
            print(f"\n错误已写入 {log}")
        except OSError:
            pass
        input("按回车退出…")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
