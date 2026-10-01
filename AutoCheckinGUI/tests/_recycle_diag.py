#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本轮的一次性诊断脚本送进**回收站**（不是永久删除）。

为什么不用 `Remove-Item`：本项目约定「清理一律走回收站」，永久删除不可逆。
为什么不用 `Add-Type`：被 PowerShell 安全策略拦死
（"Add-Type compiles and loads .NET code at runtime"）。
所以走 Win32 `SHFileOperationW` + `FOF_ALLOWUNDO`（= 回收站）——
纯 ctypes，无需编译任何东西。
"""
import ctypes
import os
import sys
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))

TARGETS = [
    "_diag_dpi_grab.py",
    "_diag_intro_label.py",
    "_diag_intro_wrap.py",
    "_diag_zoom_shot.py",
    "_diag_intro_isolated.py",
    "_diag_pixel_edge.py",
    "_diag_dpi_scale.py",
    "_diag_physical.py",
    "_diag_shot_sizes.py",
]

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040          # ★ 送回收站
FOF_NOERRORUI = 0x0400


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def recycle(path):
    """返回 SHFileOperationW 的错误码（0 = 成功）。"""
    # pFrom 必须是**双 null 结尾**的多字符串
    src = path + "\0\0"
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = src
    op.pTo = None
    op.fFlags = (FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO
                 | FOF_NOERRORUI)
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))


def main():
    ok = bad = 0
    for n in TARGETS:
        p = os.path.join(HERE, n)
        if not os.path.exists(p):
            print("  skip  %s（不存在）" % n)
            continue
        rc = recycle(p)
        gone = not os.path.exists(p)
        if rc == 0 and gone:
            ok += 1
            print("  OK    已送回收站  %s" % n)
        else:
            bad += 1
            print("  FAIL  %s  rc=%d gone=%s" % (n, rc, gone))
    print("\n回收站 %d 个，失败 %d 个" % (ok, bad))

    print("\n--- 保留的截图/探针相关文件 ---")
    for n in sorted(os.listdir(HERE)):
        if (n.startswith("_shot") or n.startswith("probe_shot")
                or n.startswith("_diag_") or n.startswith("shoot")):
            print("   %s" % n)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
