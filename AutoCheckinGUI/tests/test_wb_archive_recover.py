#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy「历史账号备份 / 一键找回」回归（2026-09-25 新增）。

为什么要有这个功能
------------------
当天出过一次**真实的数据丢失**：`capture_current()` 是「读旧 → 加一条 →
整体覆盖」，一次瞬时读失败就把用户**已捕获的第二个账号**抹掉了
（表现为「之前捕获并保留了两个账号，现在只剩一个」）。
`_load_saved(strict=True)` 已经堵住**新发生**的路径，但**已经被抹掉的数据
没法凭空回来** —— 所以再加一层：**归档**。

设计
----
* `archive_saved(saved)`：把一份快照**并入归档**，按 uid 取并集，
  同一 uid 保留「打分更高」的那条（有令牌 > 到期更晚 > 捕获时间更晚），
  并记 `first_seen` / `last_seen`。**只增不减**。
* `_save_saved()` 每次写快照前先并归档 → 任何一次捕获都会留下备份。
* `recoverable_accounts()` / `recover_accounts(uids)`：把归档里
  快照中**缺失**的账号合并回去。
* GUI：WorkBuddy 账号页的「⟲ 找回历史账号（N）」按钮 + 找回弹窗。

测试内容
--------
  1. 归档：并集、只增不减、同 uid 取优、first_seen 保留最早
  2. ★ 核心：快照丢账号 → 可找回 → 找回后快照恢复
  3. ★ 归档**只增不减**：快照被清空也不影响归档
  4. 找回的边界：已有的不重复加；只找回指定的；空归档不报错
  5. 快照损坏时 `recover_accounts` **拒绝写入**（绝不清库）
  6. GUI：按钮文案带可找回数量；弹窗能正常弹出（无异常）
  7. 测试隔离：全程沙箱 + 生产文件指纹比对

★ 关键断言都带**负对照/正对照**。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
            else os.path.dirname(os.path.dirname(GUI_DIR)))
for _p in (PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import wb_checkin as WB                                              # noqa: E402
from app import config as C                                          # noqa: E402

OK = FAIL = 0

# ============================================================ 测试隔离
# ★ 绝不写用户的真实快照与归档（沿用 test_wb_encrypted_name 的做法）：
#   **写**全部改道临时目录；**读**保持真实（不影响其它断言）。
_TMP = tempfile.mkdtemp(prefix="tb_archive_")
_REAL = {
    "TOKENS_FILE": WB.TOKENS_FILE,
    "ARCHIVE_FILE": WB.ARCHIVE_FILE,
    "save": WB._save_saved,
}
WB.TOKENS_FILE = os.path.join(_TMP, "tokens_workbuddy.json")
WB.ARCHIVE_FILE = os.path.join(_TMP, "logs", "wb_tokens_archive.json")


def _fp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


_FP_REAL_SNAP = _fp(_REAL["TOKENS_FILE"])
_FP_REAL_ARC = _fp(_REAL["ARCHIVE_FILE"])


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {extra}")


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    A = "aaaa1111-0000-0000-0000-000000000001"
    B = "bbbb2222-0000-0000-0000-000000000002"
    C_ = "cccc3333-0000-0000-0000-000000000003"

    print("== 1. 归档：并集 / 只增不减 / 同 uid 取优 ==")
    _write(WB.TOKENS_FILE, {A: {"nickname": "A", "token": "TA",
                                "expires_at": 1000, "captured_at": "2026-09-01 10:00:00"}})
    WB.archive_saved(_read(WB.TOKENS_FILE))
    arc = WB.archived_accounts()
    check("归档拿到 1 条", set(arc) == {A}, list(arc))
    check("记了 first_seen", bool(arc[A].get("first_seen")), arc[A])

    # 再并入一条新的 → 应该是并集（旧的不丢）
    WB.archive_saved({B: {"nickname": "B", "token": "TB",
                          "expires_at": 2000, "captured_at": "2026-09-02 10:00:00"}})
    arc = WB.archived_accounts()
    check("★ 并集：旧的 A 没丢", {A, B} <= set(arc), list(arc))

    # 同一 uid 再来一条「更好」的（到期更晚）→ 取优，但 first_seen 保留最早的
    WB.archive_saved({A: {"nickname": "A2", "token": "TA2",
                          "expires_at": 9999, "captured_at": "2026-09-03 10:00:00"}})
    arc = WB.archived_accounts()
    check("★ 同 uid 取优（换成到期更晚的那条）",
          arc[A].get("nickname") == "A2" and arc[A].get("expires_at") == 9999, arc[A])
    check("★ first_seen 保留最早的那次",
          arc[A].get("first_seen") == "2026-09-01 10:00:00", arc[A].get("first_seen"))
    # 【负对照】「更差」的一条不能把好的覆盖掉
    WB.archive_saved({A: {"nickname": "A3", "token": "",
                          "expires_at": 1, "captured_at": "2026-09-04 10:00:00"}})
    check("【负对照】更差的一条不覆盖更好的",
          WB.archived_accounts()[A].get("nickname") == "A2",
          WB.archived_accounts()[A])

    print()
    print("== 2. ★ 核心：快照丢账号 → 可找回 → 找回后恢复 ==")
    _write(WB.TOKENS_FILE, {A: {"nickname": "A2", "token": "TA2",
                                "expires_at": 9999, "captured_at": "2026-09-03 10:00:00"},
                            B: {"nickname": "B", "token": "TB",
                                "expires_at": 2000, "captured_at": "2026-09-02 10:00:00"}})
    # 模拟「丢了一个账号」（等价于那次读失败被整体覆盖）
    _write(WB.TOKENS_FILE, {A: _read(WB.TOKENS_FILE)[A]})
    check("模拟丢失后快照只剩 1 个", len(WB._load_saved()) == 1, list(WB._load_saved()))
    check("★ 可找回列表里出现了 B", list(WB.recoverable_accounts()) == [B],
          list(WB.recoverable_accounts()))
    n, total, err = WB.recover_accounts(None, lambda *_: None)
    check("★ 找回 1 个、快照回到 2 个", n == 1 and total == 2 and not err,
          (n, total, err))
    check("★ B 真的回来了（昵称/令牌都在）",
          _read(WB.TOKENS_FILE).get(B, {}).get("nickname") == "B"
          and _read(WB.TOKENS_FILE).get(B, {}).get("token") == "TB",
          _read(WB.TOKENS_FILE).get(B))
    check("★ 找回后没有可找回的了", not WB.recoverable_accounts(),
          list(WB.recoverable_accounts()))

    print()
    print("== 3. ★ 归档只增不减：快照被清空也不影响 ==")
    _write(WB.TOKENS_FILE, {})          # 极端：快照被清空
    check("快照空了", WB._load_saved() == {})
    arc = WB.archived_accounts()
    check("★ 归档里 A/B 仍在", {A, B} <= set(arc), list(arc))
    n2, total2, _ = WB.recover_accounts(None, lambda *_: None)
    check("★ 从空快照里一次找回全部", n2 == 2 and total2 == 2, (n2, total2))

    print()
    print("== 4. 找回的边界 ==")
    # 已有的不重复加
    n3, total3, _ = WB.recover_accounts(None, lambda *_: None)
    check("已存在的不会被重复加入（返回 0）", n3 == 0 and total3 == 2, (n3, total3))
    # 只找回指定的
    _write(WB.TOKENS_FILE, {A: _read(WB.TOKENS_FILE)[A]})
    n4, total4, _ = WB.recover_accounts([C_], lambda *_: None)   # 归档里没有 C
    check("指定归档里没有的 uid → 不写入", n4 == 0 and total4 == 1, (n4, total4))
    n5, total5, _ = WB.recover_accounts([B], lambda *_: None)
    check("只找回指定的 B", n5 == 1 and total5 == 2, (n5, total5))

    print()
    print("== 5. ★ 快照损坏时拒绝写入（绝不清库）==")
    with open(WB.TOKENS_FILE, "w", encoding="utf-8") as f:
        f.write("{ 坏掉的 JSON")
    _fp_before = _fp(WB.TOKENS_FILE)
    n6, _t6, err6 = WB.recover_accounts(None, lambda *_: None)
    check("★ 损坏时返回失败原因而不是硬写", n6 == 0 and bool(err6), (n6, err6))
    check("★ 快照文件一个字节都没动", _fp(WB.TOKENS_FILE) == _fp_before,
          f"{_fp_before} → {_fp(WB.TOKENS_FILE)}")

    print()
    print("== 6. GUI：按钮带数量 + 弹窗能弹出 ==")
    # 恢复到「丢了一个」的状态，好让按钮显示数量
    _write(WB.TOKENS_FILE, {A: _read(WB.ARCHIVE_FILE)[A]})
    try:
        from app.application import App
        app = App()
        app.update()
        for _ in range(18):
            app.update()
            time.sleep(0.02)
        app._switch_module("workbuddy")
        app.update()
        app.goto("workbuddy", "accounts")
        for _ in range(14):
            app.update()
            time.sleep(0.02)
        page = app.page
        check("★ 账号页有「找回历史账号」按钮", hasattr(page, "_recover_btn"))
        if hasattr(page, "_recover_btn"):
            _txt = page._recover_btn.cget("text")
            check("★ 按钮文案带可找回数量", "找回历史账号" in _txt and "（" in _txt,
                  _txt)
        _err = None
        try:
            page._recover_wb()
            for _ in range(10):
                app.update()
                time.sleep(0.03)
        except Exception as e:  # noqa: BLE001
            _err = repr(e)
        check("★ 找回弹窗能正常弹出（不抛异常）", _err is None, _err)
        try:
            app.destroy()
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("GUI 流程不抛异常", False, repr(e))

    print()
    print("== 7. ★ 生产文件指纹：测试全程不得改动真实快照/归档 ==")
    check("★ 真实 tokens_workbuddy.json 未被改动",
          _fp(_REAL["TOKENS_FILE"]) == _FP_REAL_SNAP,
          f"{_FP_REAL_SNAP} → {_fp(_REAL['TOKENS_FILE'])}")
    check("★ 真实归档文件未被改动",
          _fp(_REAL["ARCHIVE_FILE"]) == _FP_REAL_ARC,
          f"{_FP_REAL_ARC} → {_fp(_REAL['ARCHIVE_FILE'])}")
    check("★ 落盘路径确实被改道到沙箱",
          WB.TOKENS_FILE.startswith(_TMP) and WB.ARCHIVE_FILE.startswith(_TMP),
          (WB.TOKENS_FILE, WB.ARCHIVE_FILE))

    print()
    print(f"结果：{OK} OK / {FAIL} FAIL")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        WB.TOKENS_FILE = _REAL["TOKENS_FILE"]
        WB.ARCHIVE_FILE = _REAL["ARCHIVE_FILE"]
        WB._save_saved = _REAL["save"]
