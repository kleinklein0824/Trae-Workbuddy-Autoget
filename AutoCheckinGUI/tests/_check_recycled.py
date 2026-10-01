#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确认那 9 个脚本到底进没进回收站（读 `$I*` 元数据里的原始路径）。

`SHFileOperationW` 返回了 rc=2（看着像失败），但文件确实从 tests/ 消失了。
必须查清楚是「进了回收站」还是「被永久删了」——本项目约定清理走回收站。
回收站元数据文件 `$I<6位ID>` 里，偏移 24 起是 UTF-16LE 的原始完整路径。
"""
import glob
import os
import struct
import sys

WANT = [
    "_diag_dpi_grab.py", "_diag_intro_label.py", "_diag_intro_wrap.py",
    "_diag_zoom_shot.py", "_diag_intro_isolated.py", "_diag_pixel_edge.py",
    "_diag_dpi_scale.py", "_diag_physical.py", "_diag_shot_sizes.py",
]


def read_orig_path(meta):
    """读 `$I*` 里的原始路径。

    ★ 格式有两版，路径偏移**不一样**（第一版按 24 读，读出 'd'/'\\x7f' 等垃圾）：
        v1: 0x00 version(8) | 0x08 size(8) | 0x10 time(8) | 0x18 path(UTF-16LE)
        v2: 0x00 version(8) | 0x08 size(8) | 0x10 time(8) | 0x18 namelen(4) | 0x1C path
    所以按版本取偏移，再用「盘符 + 反斜杠」定位真实起点，避免踩坑。
    """
    import re
    with open(meta, "rb") as f:
        data = f.read()
    if len(data) < 28:
        return None
    ver = struct.unpack_from("<Q", data, 0)[0]
    off = 28 if ver == 2 else 24
    s = data[off:].decode("utf-16-le", "replace")
    m = re.search(r"[A-Za-z]:\\", s)
    if not m:
        return None
    return s[m.start():].split("\x00")[0]


def main():
    root = "C:\\$Recycle.Bin"
    found = {}
    if not os.path.isdir(root):
        print("!! 找不到 %s" % root)
        return 1
    total_meta = 0
    samples = []
    for sid in os.listdir(root):
        d = os.path.join(root, sid)
        if not os.path.isdir(d):
            continue
        for meta in glob.glob(os.path.join(d, "$I*")):
            total_meta += 1
            p = read_orig_path(meta)
            if len(samples) < 8:
                samples.append(p)
            if not p:
                continue
            base = os.path.basename(p.replace("\\", "/"))
            if base in WANT:
                found[base] = p

    # ★ 先证明读取器是活的：回收站里总共有多少条、能读出几条原始路径
    print("回收站元数据文件共 %d 个；读取器抽样结果：" % total_meta)
    readable = sum(1 for s in samples if s)
    for s in samples:
        print("   %r" % (s,))
    print("抽样可读 %d / %d" % (readable, len(samples)))
    if total_meta == 0:
        print("→ 回收站是空的（说明本机确实把文件永久删掉了，读取器无从验证）")
    elif readable == 0:
        print("→ !! 读取器可能坏了（有元数据却一条都读不出）→ 上面结论不可信")

    print("\n目标文件在回收站里找到 %d / %d 个：" % (len(found), len(WANT)))
    for n in WANT:
        if n in found:
            print("  OK   %s" % n)
            print("        原路径 %s" % found[n])
        else:
            print("  缺失 %s" % n)
    return 0 if len(found) == len(WANT) else 1


if __name__ == "__main__":
    sys.exit(main())
