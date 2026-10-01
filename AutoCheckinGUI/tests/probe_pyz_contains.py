#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证明改动真的进了 exe：从 PYZ 里取出代码对象核对符号与签名。

grep 对 exe 没用 —— PYZ 是 zlib 压缩的，字符串搜不到。
正解：`PyInstaller.archive.readers.ZlibArchiveReader` → `extract('模块名')`
→ `marshal.loads` → 递归 `co_consts` 收集 `co_names` / `co_varnames`。

比"符号存在"更强的一步：**连参数签名一起核对**
（`co_varnames[:co_argcount]`），避免"名字撞上了但实现没换"。

用法：python tests/probe_pyz_contains.py <exe> [模块:符号 ...]

三种 spec 的适用范围（别用错，用错会得到**看起来像 bug 的假 FAIL**）：
  mod:sym         只匹配 `co_names` 与**函数名** ——
                  **字符串字面量匹配不到**！要查字面量请用下面的 func@sym 形式。
  mod:func@sym    func（含嵌套 lambda）内部是否引用 sym；
                  匹配 co_names（精确）、字符串常量（精确**或子串**）、
                  以及元组常量里的字符串（关键字参数名 `f(x=1)` 就存在元组常量里）。
                  ★ 子串是刻意放开的：`f"已弹窗通知：{x}"` 的常量是 `'已弹窗通知：'`，
                    带全角冒号 —— 要求用户背下 f-string 的片段边界不合理。
  ⚠️ 函数的**形参名**在 `co_varnames` 里，既不在 co_names 也不在 co_consts ——
     所以 `mod:start_checkin@scope`（scope 是它自己的形参）必然 FAIL，
     这不是"没接上"。要查接线请挑函数体里真正引用到的名字
     （如 `@scope_all`、`@PLATFORMS`）。
★ 每次用都要带一条**负对照**（肯定不存在的符号），确认工具真的会报 FAIL。
"""
from __future__ import annotations

import marshal
import os
import sys
import types

from PyInstaller.archive.readers import ZlibArchiveReader


def walk(code, seen=None):
    """递归遍历代码对象，产出所有 (names, varnames, argcount, name)。"""
    if seen is None:
        seen = set()
    if id(code) in seen:
        return
    seen.add(id(code))
    if isinstance(code, types.CodeType):
        yield (set(code.co_names), tuple(code.co_varnames),
               code.co_argcount, code.co_name)
    for c in getattr(code, "co_consts", ()) or ():
        if isinstance(c, types.CodeType):
            yield from walk(c, seen)
        elif isinstance(c, (tuple, list)):
            for x in c:
                if isinstance(x, types.CodeType):
                    yield from walk(x, seen)


def _reader_for(exe):
    """拿到能读 PYZ 的 reader。

    ⚠️ onedir 的 exe **本身是 CArchive（PKG），不是 PYZ** —— 直接
    `ZlibArchiveReader(exe)` 会抛 `ArchiveReadError: PYZ magic pattern mismatch!`。
    PYZ 是**嵌在 PKG 里**的，必须先开 CArchive 再 `open_embedded_archive()`。
    """
    from PyInstaller.archive.readers import CArchiveReader

    # 1) 直接给的就是一个独立 .pyz
    try:
        return ZlibArchiveReader(exe)
    except Exception:  # noqa: BLE001
        pass
    # 2) 从 PKG 里取出内嵌的 PYZ
    ca = CArchiveReader(exe)
    for name in ca.toc:
        if str(name).lower().endswith(".pyz"):
            return ca.open_embedded_archive(name)
    raise SystemExit(f"在 {exe} 里没找到内嵌的 PYZ（toc 项：{list(ca.toc)[:10]}）")


def load_module(exe, modname):
    reader = _reader_for(exe)
    try:
        data = reader.extract(modname)
    except KeyError:
        return None
    # ⚠️ `extract()` 有时直接返回代码对象，有时返回字节 → 两种都要兜住
    if isinstance(data, types.CodeType):
        return data
    if isinstance(data, (bytes, bytearray)):
        return marshal.loads(bytes(data))
    return None


def find_func(code, name):
    """在代码树里找名为 name 的函数代码对象（含嵌套）。"""
    for _names, _varnames, _argcount, cname in walk(code):
        pass
    # walk 不返回对象本身，这里单独再走一遍
    stack = [code]
    seen = set()
    while stack:
        c = stack.pop()
        if id(c) in seen:
            continue
        seen.add(id(c))
        if isinstance(c, types.CodeType) and c.co_name == name:
            return c
        for k in getattr(c, "co_consts", ()) or ():
            if isinstance(k, types.CodeType):
                stack.append(k)
            elif isinstance(k, (tuple, list)):
                stack.extend(x for x in k if isinstance(x, types.CodeType))
    return None


def _strings_in(const):
    """把常量里的**所有**字符串挖出来（含元组/列表/集合里的）。

    ★ 为什么需要：**关键字参数名不在 co_names 里**。
    `page.start_checkin(skip_done=True, scope="all")` 编译后，
    参数名 `'skip_done'`/`'scope'` 是作为**元组常量**
    `('skip_done', 'scope')` 存在 `co_consts` 里的。
    第一版只认 `co_consts` 里**裸字符串**，于是这类「按关键字传参」的接线
    被误报成「改了但没接上」—— 探针自己撒谎，比不查还糟。
    """
    if isinstance(const, str):
        yield const
    elif isinstance(const, (tuple, list, set, frozenset)):
        for x in const:
            yield from _strings_in(x)


def refs_in(code, func, callee):
    """`func` 内部是否引用了 `callee`。返回 `True` / `False` / `None`（函数不存在）。

    四条判据，命中其一即算接上：
      ① `callee` 出现在 co_names —— 真·名字引用，例如 `scheduler.set_retry(...)`；
      ② `callee` **恰好等于**某个字符串常量 —— 配置键 / dict 键走的就是这条路
         （`cfg.get("retry_toast_quiet")` 在字节码里只是个常量，co_names 里看不到）；
      ③ `callee` 出现在**元组常量**里 —— 关键字参数名走的是这条路
         （见 `_strings_in`）；
      ④ ★ `callee` 是某个字符串常量的**子串** —— f-string 的字面量片段走这条路。
    ★ ② 是后补的：第一版只看 co_names，于是所有「按键名读配置」的接线都被误报成
      「改了但没接上」，把探针的结论带偏。③ 同样是后补的。
    ★★ ④ 是本轮补的，原因很实在：`log(f"已弹窗通知：{title}")` 编译后常量是
      `'已弹窗通知：'`（**带全角冒号**），用 `callee='已弹窗通知'` 去**精确相等**
      匹配必然落空 —— 于是「日志已经加上了」被误报成「改了但没接上」。
      f-string 会把字面量与插值切碎，要求用户背下片段边界是不合理的。
      ⚠️ 只对**字符串常量**放开子串；`co_names` 仍然**精确**匹配 ——
      否则 `_is_busy` 会撞上 `_is_busy_x` 之类的近似名，那才是真的在撒谎。
    """
    fc = find_func(code, func)
    if fc is None:
        return None
    stack, seen = [fc], set()
    while stack:
        c = stack.pop()
        if id(c) in seen:
            continue
        seen.add(id(c))
        if callee in (getattr(c, "co_names", ()) or ()):
            return True
        for k in (getattr(c, "co_consts", ()) or ()):
            for s in _strings_in(k):
                if s == callee or callee in s:
                    return True
            if isinstance(k, types.CodeType):
                stack.append(k)
            elif isinstance(k, (tuple, list)):
                stack.extend(x for x in k if isinstance(x, types.CodeType))
    return False


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    exe = sys.argv[1]
    if not os.path.exists(exe):
        print(f"找不到 exe：{exe}")
        return 2

    print(f"exe: {os.path.basename(exe)}  ({os.path.getsize(exe)/1048576:.1f} MB)")
    rc = 0
    for spec in sys.argv[2:]:
        # 支持三种写法：
        #   mod:sym            模块里有这个符号
        #   mod:func@sym       函数 func 里**引用**了 sym（比"符号存在"强得多）
        mod, _, rest = spec.partition(":")
        # ⚠️ 只有出现 '@' 才是 `func@callee`；否则 `rest` 就是符号本身。
        # （第一版直接 partition("@") 会把 `mod:sym` 误当成 `func=sym, callee=""`，
        #  于是所有不带 @ 的 spec 全部 FAIL。）
        if "@" in rest:
            func, _, sym = rest.partition("@")
        else:
            func, sym = "", rest
        code = load_module(exe, mod)
        if code is None:
            print(f"  FAIL {spec}  模块不在 PYZ 里")
            rc = 1
            continue

        if func:
            got = refs_in(code, func, sym)
            if got is None:
                print(f"  FAIL {spec}  模块里没有函数 {func}")
                rc = 1
            elif got:
                print(f"  OK   {spec}  （{func} 内部确实引用了 {sym}）")
            else:
                print(f"  FAIL {spec}  {func} 里没有引用 {sym}（改了但没接上）")
                rc = 1
            continue

        found_names = set()
        found_sig = None
        for names, varnames, argcount, cname in walk(code):
            found_names |= names
            if cname == sym:
                found_sig = varnames[:argcount]
        if sym in found_names or found_sig is not None:
            extra = f"  签名={found_sig}" if found_sig else ""
            print(f"  OK   {spec}{extra}")
        else:
            print(f"  FAIL {spec}  模块在，但找不到该符号")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
