#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资源路径解析：图标、图片等静态文件。

冻结成 exe 之后，静态资源被 PyInstaller 解包到 `sys._MEIPASS`，
不能再按源码目录的相对路径去找。这里统一收口。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))          # app/ui/
_GUI_DIR = os.path.dirname(os.path.dirname(_HERE))          # 项目(或 exe)根


def _candidates(*parts):
    """返回按优先级排列的候选路径：源码目录 → 解包目录 → exe 同级。"""
    rel = os.path.join(*parts)
    out = [os.path.join(_GUI_DIR, rel)]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(os.path.join(meipass, rel))
    if getattr(sys, "frozen", False):
        out.append(os.path.join(os.path.dirname(sys.executable), rel))
    return out


def _first_existing(*parts):
    for p in _candidates(*parts):
        if os.path.exists(p):
            return p
    return None


def app_icon_ico():
    """主图标 .ico 的绝对路径（用于任务栏 / 快捷方式，找不到返回 None）。"""
    return _first_existing("assets", "app.ico")


def app_icon_png():
    """主图标 .png 的绝对路径（用于 Tk 的 iconphoto）。"""
    return _first_existing("assets", "app.png")


def author_reward_png():
    """奖励作者收款码（卡片显示尺寸，PNG）的绝对路径。"""
    return _first_existing("assets", "reward_qr.png")


def author_reward_full_png():
    """奖励作者收款码原图（高分辨率，供点击放大）的绝对路径。"""
    return _first_existing("assets", "reward_qr_full.png")


def exists(*parts):
    """判断某个资源是否存在（供调用方做降级判断）。"""
    return _first_existing(*parts) is not None
