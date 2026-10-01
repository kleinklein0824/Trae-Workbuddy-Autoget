#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：读出 exe 的版本资源（右键属性 → 详细信息 里的那几个字段）。

为什么单独查：`assets/version_info.txt` 的 StringTable 键必须是 **8 位十六进制**
（前 4 位语言 ID、后 4 位代码页）。写成 6 位（`'080404'`）时 Windows 找不到
匹配的字符串表 → FileVersion / ProductVersion / ProductName **全是空的**
（2.4.7 之前一直如此）。这个探针就是防它回归。
"""
import ctypes
import sys
from ctypes import wintypes

KEY = r"\StringFileInfo\080404B0"      # zh-CN + Unicode(1200)


def main(path):
    size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
    if not size:
        print("FAIL 读不到版本资源（文件不存在或没有 VS_VERSIONINFO）")
        return 1
    buf = ctypes.create_string_buffer(size)
    ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf)

    def q(sub):
        v, ln = ctypes.c_void_p(), wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
                buf, sub, ctypes.byref(v), ctypes.byref(ln)):
            return None
        return ctypes.wstring_at(v, ln.value).rstrip("\x00")

    bad = 0
    for name in ("FileVersion", "ProductVersion", "ProductName", "CompanyName"):
        val = q(KEY + "\\" + name)
        ok = bool(val)
        bad += 0 if ok else 1
        print("  %s   %-15s = %s" % ("OK  " if ok else "FAIL", name, val))

    # FixedFileInfo 的 filevers
    # ★ 索引是 2/3 不是 0/1：VS_FIXEDFILEINFO 开头是 dwSignature(0xFEEF04BD)
    #   与 dwStrucVersion，之后才是 dwFileVersionMS / dwFileVersionLS。
    #   按 0/1 取会打印出 `65263.1213.x.y`（0xFEEF 就是签名的低字）。
    v, ln = ctypes.c_void_p(), wintypes.UINT()
    if ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(v),
                                            ctypes.byref(ln)):
        dw = ctypes.cast(v, ctypes.POINTER(wintypes.DWORD))
        ms, ls = dw[2], dw[3]
        print("  OK   filevers        = %d.%d.%d.%d"
              % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF))

    print("\n结果：%s" % ("全部字段非空" if not bad else "有 %d 个字段为空" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else
                  r"dist\TraeBuddy签到助手\TraeBuddy签到助手.exe"))
