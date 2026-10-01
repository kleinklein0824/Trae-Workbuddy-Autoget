#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 昵称 / 手机号「加密信封」回归（2026-09-24）。

真实故障
--------
WorkBuddy 桌面端改了本地存储格式：`nickname` 与 `phone` 不再是明文，
而是一个**加密信封** dict：
```python
{"$wbEncrypted": 1, "envelope": "eyJzdWl0ZSI6MSwia2V5SWQiOiI5MTI3ZGVhMWI0NDAyMGE3Ii..."}
```
「捕获当前账号」把它原样存进了 `tokens_workbuddy.json`，界面又把它当字符串用
（`a["display"][:1]`、`" · ".join(parts)`），于是：

* 账号管理页 **整页打不开**：`KeyError(slice(None, 1, None))`
  —— ★ 报的是 KeyError 不是 TypeError，因为 **Python 3.12+ 起 slice 可哈希**，
  dict 被切片就成了「查一个不存在的 key」，很容易看懵；
* 明细行 `" · ".join(parts)` → `TypeError: expected str instance, dict found`。

修法（数据层兜底 + 界面再防一道）
--------------------------------
* `wb_checkin.safe_text(v)`：非空字符串原样（strip），其它一律返回 `""`；
  调用方原本就有「退回 uid 前缀 / 手机号」的兜底，于是界面显示
  `83992452****` 这种可读标识，**绝不把 dict 漏到界面**。
  ★ 不去解那个信封：那是 WorkBuddy 的私有格式，硬猜只会引入更难查的错。
* `app/config.discover_accounts` 的 nickname / phone 都走 `safe_text`。
* `accounts._render_row` / `_detail` 再判一次类型（以后任何非字符串都不崩页）。

本测试守住的**核心不变量**：
  **`discover_accounts()` 返回的每个账号，`display` / `name` / `phone` 必须是 `str`。**
"""
import os
import sys
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
# ★★ 绝不让测试**写**用户的真实快照（2026-09-25 修）。
#   为什么必须拦：`wb_checkin.capture_current()` 内部是
#       saved = _load_saved()      # ← 测试如果给这里打过桩，就是**测试假数据**
#       saved[cur["uid"]] = {...}
#       _save_saved(saved)         # ← 一写就把假数据混进真实文件
#   实测后果：真实 `tokens_workbuddy.json` 里多出一个 uid 含 `demo00000001`
#   的**假账号**，界面上多出一行名字不对的账号。
#   这里把 `_save_saved` 换成「只记录、不落盘」：
#     · **写**被拦死 → 不可能污染真实文件；
#     · **读**（`_load_saved`）保持真实 → 账号数不变，不影响其它断言。
_REAL_SAVE_SAVED = WB._save_saved
_SAVED_WRITES = []


def _record_only(saved):
    """替代 `_save_saved`：记录本次「本来要写什么」，不碰磁盘。"""
    _SAVED_WRITES.append(dict(saved or {}))


WB._save_saved = _record_only

# ★★ 生产文件指纹：测试**全程**不得改动用户的真实快照（2026-09-25 加）。
#   这条守卫正是能提前抓住上一轮那次污染的东西：
#   当时测试给 `_load_saved` 打了桩，`capture_current` 顺手把**假数据**
#   写进了真实 `tokens_workbuddy.json`（多出两个不存在的账号，
#   界面上就是「捕获的账号 ID 名称显示不对」）。
#   做法沿用项目约定：**开头记指纹、末尾比对**，不删文件。
_REAL_SNAP = WB.TOKENS_FILE


def _snap_fp():
    try:
        st = os.stat(_REAL_SNAP)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


_FP_BEFORE = _snap_fp()
ENVELOPE = {"$wbEncrypted": 1, "envelope": "eyJzdWl0ZSI6MSwia2V5SWQiOiJkZW1vIn0="}


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {extra}")


def main():
    print("== 1. safe_text：非字符串一律返回空串 ==")
    check("加密信封 dict → ''", WB.safe_text(ENVELOPE) == "", WB.safe_text(ENVELOPE))
    check("None → ''", WB.safe_text(None) == "")
    check("数字 → ''（不是 '123'）", WB.safe_text(123) == "", WB.safe_text(123))
    check("列表 → ''", WB.safe_text(["a"]) == "")
    check("普通字符串原样（并 strip）", WB.safe_text("  Klein  ") == "Klein",
          WB.safe_text("  Klein  "))
    check("空字符串 → ''", WB.safe_text("") == "")

    print()
    print("== 2. ★ 核心不变量：发现结果里不许出现非字符串字段 ==")
    for plat in ("traework", "traecn", "workbuddy"):
        try:
            accs = C.discover_accounts(plat)
        except Exception as e:  # noqa: BLE001
            check(f"{plat} 发现不抛异常", False, repr(e))
            continue
        bad = [(a.get("id"), k, type(a.get(k)).__name__)
               for a in accs for k in ("display", "name", "phone")
               if k in a and not isinstance(a.get(k), str)]
        check(f"★ {plat}：{len(accs)} 个账号的 display/name/phone 全是 str",
              not bad, bad[:4])

    print()
    print("== 3. ★ 合成「快照里是加密信封」→ 仍然全是字符串，且退回 uid 前缀 ==")
    _real_saved = WB._load_saved
    _uid = "83992452-51e0-4c1f-9a2b-demo00000001"
    fake = {
        _uid: {"nickname": ENVELOPE, "phone": ENVELOPE, "token": "T",
               "domain": "", "expires_at": 0, "captured_at": "2026-09-24 00:00:00"},
        "6f2fe543-1111-2222-3333-444455556666": {
            "nickname": "Klein", "phone": "15219062371", "token": "T2",
            "domain": "", "expires_at": 0, "captured_at": "2026-09-24 00:00:00"},
    }
    # 关掉「实时登录态」干扰：只测快照分支
    #   （`discover_accounts(platform)` 的第一个参数是 platform，
    #    不能传 cfg —— 所以直接让 `_read_current_auth` 返回 None 来隔离）
    _real_auto = WB._read_current_auth
    try:
        WB._load_saved = lambda: dict(fake)
        WB._read_current_auth = lambda: None
        accs = C.discover_accounts("workbuddy")
        by_uid = {a["uid"]: a for a in accs}
        check("加密昵称账号被发现", _uid in by_uid, list(by_uid))
        if _uid in by_uid:
            a = by_uid[_uid]
            check("★ display 是字符串（不是 dict）", isinstance(a["display"], str),
                  type(a["display"]).__name__)
            check("★ display 退回 uid 前缀", a["display"] == "83992452****",
                  repr(a["display"]))
            check("★ phone 是空串（不是 dict）", a["phone"] == "", repr(a["phone"]))
            check("★ name 也是字符串", isinstance(a["name"], str), repr(a["name"]))
        ok_acc = by_uid.get("6f2fe543-1111-2222-3333-444455556666")
        check("正常明文昵称不受影响", ok_acc and ok_acc["display"] == "Klein",
              ok_acc and ok_acc["display"])
    finally:
        WB._load_saved = _real_saved
        WB._read_current_auth = _real_auto

    print()
    print("== 4. 【负对照】旧写法确实会把 dict 漏出来（证明上面那几条不是恒真）==")
    # 旧代码就是 `item.get("nickname") or ""` —— 直接照抄一遍
    _old = fake[_uid].get("nickname") or ""
    check("【负对照】旧写法拿到的是 dict",
          isinstance(_old, dict), type(_old).__name__)
    check("【负对照】而 dict[:1] 会抛 KeyError（不是 TypeError）",
          _raises_keyerror(_old), "见 _raises_keyerror 说明")

    print()
    print("== 5. ★ GUI：快照含加密信封时，账号页仍能构造出来 ==")
    try:
        WB._load_saved = lambda: dict(fake)
        from app.application import App
        app = App()
        app.update()
        for _ in range(16):
            app.update()
            time.sleep(0.02)
        app._switch_module("workbuddy")
        app.update()
        app.goto("workbuddy", "accounts")
        for _ in range(10):
            app.update()
        page = app.page
        check("★ workbuddy 账号页构造成功（不再「页面加载失败」）",
              type(page).__name__ == "AccountsPage", type(page).__name__)
        # 页面上不该出现「页面加载失败」
        bad_txt = ""
        for w in app.page_host.winfo_children():
            for sub in w.winfo_children():
                try:
                    t = str(sub.cget("text"))
                except Exception:  # noqa: BLE001
                    continue
                if "页面加载失败" in t:
                    bad_txt = t[:120]
        check("★ 没有渲染出「页面加载失败」错误块", not bad_txt, bad_txt)
        try:
            app.destroy()
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("workbuddy 账号页构造不抛异常", False, repr(e))
    finally:
        WB._load_saved = _real_saved

    print()
    print("== 6. ★ 加密昵称 → 从历史快照反查明文真名（2026-09-25 加）==")
    #   背景：新版把 nickname 加密了（本地解不开），界面上只能显示 uid 前缀
    #   （`6f2fe543****` / `83992452****`）→ 用户报「账号名显示不对」。
    #   但同一个 auth 目录里**保留着历史快照**，那些旧格式是**明文**的。
    _hist = WB._name_history()
    check("能扫到历史明文映射（本机 auth 目录里有旧快照）",
          isinstance(_hist, dict) and len(_hist) >= 1, f"{len(_hist)} 条")
    _uid_demo = "6f2fe543-fe5a-42d1-b702-794cddf28f62"
    _nm = _hist.get(_uid_demo)
    check("★ 已知账号能反查到明文昵称", bool(_nm), f"{_uid_demo} → {_nm!r}")
    check("★ readable_name：加密信封 → 历史明文（不是 uid 前缀）",
          WB.readable_name(_uid_demo, ENVELOPE) == (_nm or ""),
          repr(WB.readable_name(_uid_demo, ENVELOPE)))
    check("★ readable_name：明文昵称优先于历史",
          WB.readable_name(_uid_demo, " 显式名字 ") == "显式名字",
          WB.readable_name(_uid_demo, " 显式名字 "))
    # 【负对照】没有历史记录的 uid 必须返回空串（调用方才会退 uid 前缀），
    #   证明上面那条不是「什么都返回真名」。
    check("【负对照】历史里没有的 uid → 空串（退回 uid 前缀）",
          WB.readable_name("__no_such_uid__", ENVELOPE) == "",
          repr(WB.readable_name("__no_such_uid__", ENVELOPE)))
    # 端到端：发现结果里不该再出现纯 uid 前缀（有历史可查的那些）
    _wb = C.discover_accounts("workbuddy")
    _prefix_only = [a["display"] for a in _wb
                    if a["display"].endswith("****")
                    and a["uid"] in _hist]
    check("★ 发现结果里，有历史可查的账号不再显示成 uid 前缀",
          not _prefix_only, _prefix_only)

    print()
    print("== 7. ★★ 令牌也是加密信封 → 必须换历史明文 JWT（2026-09-25 加）==")
    #   用户报「workbuddy 手动签到失败」的真因：新版把 `auth.accessToken`
    #   也加密成了 `{"$wbEncrypted":1,...}`，塞进 `Bearer <dict>` 必然 401，
    #   再被误报成「令牌过期」。而历史快照里的令牌是**明文 JWT 且仍在有效期**。
    check("is_jwt：明文 JWT → True", WB.is_jwt("eyJh.eyJi.c") is True)
    check("is_jwt：加密信封 → False", WB.is_jwt(ENVELOPE) is False)
    check("is_jwt：None / 数字 → False",
          WB.is_jwt(None) is False and WB.is_jwt(123) is False)

    _th = WB._token_history()
    check("能扫到历史明文令牌", isinstance(_th, dict) and len(_th) >= 1,
          f"{len(_th)} 个 uid")
    _uid2 = "83992452-51ec-4353-9399-98591a64f25a"
    _tok2, _exp2 = WB.usable_token(_uid2, ENVELOPE)
    check("★ 加密信封 → 换成历史明文 JWT", WB.is_jwt(_tok2),
          f"{_tok2[:16]}… len={len(_tok2)}")
    check("★ 换出来的令牌带到期时间", bool(_exp2),
          time.strftime("%Y-%m-%d", time.localtime(_exp2 / 1000)) if _exp2 else 0)
    check("★ 明文 JWT 原样返回（不被历史覆盖）",
          WB.usable_token(_uid2, "eyJx.eyJy.z")[0] == "eyJx.eyJy.z")
    # 【负对照】历史里没有的 uid → 拿不到令牌（调用方才会报「需要登录」），
    #   证明上面那条不是「随便给个 JWT」。
    check("【负对照】历史里没有的 uid → 空令牌",
          WB.usable_token("__no_such_uid__", ENVELOPE) == ("", 0),
          WB.usable_token("__no_such_uid__", ENVELOPE))

    # 核心不变量：采集出来的账号，令牌**必须**是可用的明文 JWT
    _cfg = C.load_config()
    _accs = WB._collect_accounts(_cfg, lambda *_: None)
    check("采集到 WorkBuddy 账号", len(_accs) >= 1, len(_accs))
    _bad_tok = [(a.get("uid", "")[:8], type(a.get("token")).__name__)
                for a in _accs if not WB.is_jwt(a.get("token"))]
    check("★ 每个账号的 token 都是明文 JWT（不会是 dict）", not _bad_tok, _bad_tok)
    check("★ 每个账号都有可读昵称（不是加密信封）",
          all(isinstance(a.get("nickname"), str) for a in _accs),
          [(a.get("uid", "")[:8], type(a.get("nickname")).__name__) for a in _accs])
    # `_headers` 必须拒绝非 JWT，而不是把 dict 发出去（否则 401 被误报成「令牌过期」）
    try:
        WB._headers({"uid": "u", "token": ENVELOPE})
        _hdr_ok = False
    except ValueError:
        _hdr_ok = True
    except Exception:  # noqa: BLE001
        _hdr_ok = False
    check("★ _headers 对加密令牌直接报错（不发出无效请求）", _hdr_ok)

    print()
    print("== 8. ★★ 读失败绝不许清空快照（数据丢失事故，2026-09-25 加）==")
    #   事故：`capture_current()` 是「读旧 → 加一条 → 整体覆盖」。
    #   一旦 `_load_saved()` 因**瞬时读失败**返回 {}，覆盖就会把**其它已保存账号
    #   全部清空** —— 用户实测「之前捕获并保留了两个账号，现在只剩一个」。
    import tempfile as _tf
    _bak_file = WB.TOKENS_FILE
    try:
        _d = _tf.mkdtemp(prefix="tb_wb_wipe_")
        WB.TOKENS_FILE = os.path.join(_d, "tokens_workbuddy.json")
        # ① 文件不存在（首次捕获）→ 允许当空
        check("文件不存在 → 当空（首次捕获）", WB._load_saved() == {})
        # ② 文件损坏 → 非严格读返回 {}，**严格读必须抛**
        with open(WB.TOKENS_FILE, "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 JSON")
        check("【负对照】非严格读：损坏文件 → {}", WB._load_saved() == {})
        try:
            WB._load_saved(strict=True)
            _strict_raised = False
        except Exception:  # noqa: BLE001
            _strict_raised = True
        check("★ 严格读：损坏文件 → 抛异常（调用方必须放弃写入）", _strict_raised)
        # ③ 关键：capture_current 在这种情况必须**拒绝写入**，文件保持原样
        _fp_before = (os.path.getmtime(WB.TOKENS_FILE),
                      os.path.getsize(WB.TOKENS_FILE))
        _r = WB.capture_current(lambda *_: None)
        _fp_after = (os.path.getmtime(WB.TOKENS_FILE),
                     os.path.getsize(WB.TOKENS_FILE))
        check("★ 快照损坏时 capture_current 不写入（不返回 True）", _r is False, _r)
        check("★ 快照文件**一个字节都没动**", _fp_before == _fp_after,
              f"{_fp_before} → {_fp_after}")
    finally:
        WB.TOKENS_FILE = _bak_file

    print()
    print("== 9. ★ 生产文件指纹：测试全程不得改动真实快照 ==")
    _fp_after = _snap_fp()
    check("★ 真实 tokens_workbuddy.json 未被测试改动（指纹一致）",
          _fp_after == _FP_BEFORE,
          f"{_FP_BEFORE} → {_fp_after}（{_REAL_SNAP}）")
    check("★ 落盘函数已被替换成「只记录不落盘」",
          WB._save_saved is _record_only, WB._save_saved)
    # 【正对照】直接调一次落盘函数：必须「只被记录、文件纹丝不动」，
    #   证明拦截真的在起作用（否则上面那条可能只是"恰好没写过"）。
    _n_before = len(_SAVED_WRITES)
    WB._save_saved({"__probe__": 1})
    check("【正对照】直接调 _save_saved 也不写盘（拦截生效）",
          len(_SAVED_WRITES) == _n_before + 1 and _snap_fp() == _FP_BEFORE,
          f"记录 {len(_SAVED_WRITES)} 次，指纹 {_snap_fp()}")

    print()
    print(f"结果：{OK} OK / {FAIL} FAIL")
    return 0 if FAIL == 0 else 1


def _raises_keyerror(value):
    """`value[:1]` 是否抛 KeyError（Python 3.12+ 起 slice 可哈希，dict 被切片报 KeyError）。"""
    try:
        value[:1]
        return False
    except KeyError:
        return True
    except TypeError:
        return False        # 3.11 及以前：报 TypeError，也算「旧写法会炸」


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        # 恢复被测试替换掉的落盘函数（绝不污染用户的真实快照）
        WB._save_saved = _REAL_SAVE_SAVED
