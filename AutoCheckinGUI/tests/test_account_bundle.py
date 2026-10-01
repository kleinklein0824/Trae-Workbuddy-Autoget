#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号包导出 / 导入回归测试（``app/services/account_bundle.py``）。

背景
----
用户要求「方便用户在不同设备或新安装 exe 去安心使用程序所有功能」。
三个平台的登录态分散在四份文件里（config.json / tokens_traecn.json /
tokens_workbuddy.json / traecn_device.json），只拷 config.json 是不够的。

★ 本测试**全程沙箱**：把 ``C.CONFIG_PATH`` / ``C.LOG_DIR`` / ``C.WB_TOKENS``
  以及 ``tc_checkin.TOKENS_PATH`` 全部改道到临时目录，
  并对**生产文件做指纹比对**（(st_mtime_ns, st_size)），
  确保测试没有污染真实账号数据。
  —— 本项目已有先例：测试往真实令牌缓存里写了一条假 uid，
     导致「账号永远签不完」、幂等门控被彻底击穿。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
            else os.path.dirname(os.path.dirname(GUI_DIR)))
for _p in (PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import config as C                      # noqa: E402
from app.services import account_bundle as AB    # noqa: E402
import tc_checkin as TC                          # noqa: E402

OK = FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {extra}")


# ============================================================ 沙箱
TMP = tempfile.mkdtemp(prefix="bundle_test_")
_REAL = {
    "CONFIG_PATH": C.CONFIG_PATH,
    "LOG_DIR": C.LOG_DIR,
    "WB_TOKENS": C.WB_TOKENS,
    "TC_TOKENS": TC.TOKENS_PATH,
}


def _fingerprint(paths):
    out = {}
    for p in paths:
        try:
            st = os.stat(p)
            out[p] = (st.st_mtime_ns, st.st_size)
        except OSError:
            out[p] = None
    return out


_PROD_FILES = [_REAL["CONFIG_PATH"], _REAL["WB_TOKENS"], _REAL["TC_TOKENS"],
               os.path.join(_REAL["LOG_DIR"], "tokens_traecn.json"),
               os.path.join(_REAL["LOG_DIR"], "traecn_device.json"),
               os.path.join(_REAL["LOG_DIR"], "state.json")]
_PROD_BEFORE = _fingerprint(_PROD_FILES)

C.CONFIG_PATH = os.path.join(TMP, "config.json")
C.LOG_DIR = os.path.join(TMP, "logs")
C.WB_TOKENS = os.path.join(TMP, "tokens_workbuddy.json")
TC.TOKENS_PATH = os.path.join(C.LOG_DIR, "tokens_traecn.json")
os.makedirs(C.LOG_DIR, exist_ok=True)

print("== 0. 沙箱 ==")
print(f"   临时目录 : {TMP}")
print(f"   沙箱 config : {C.CONFIG_PATH}")
print(f"   沙箱 logdir : {C.LOG_DIR}")
check("沙箱与生产路径不同",
      os.path.normcase(C.CONFIG_PATH) != os.path.normcase(_REAL["CONFIG_PATH"]))


def _seed(cfg=None, tc=None, wb=None, dev=None):
    """铺一份本机数据（每次调用先清空沙箱）。"""
    for name in ("config.json", "tokens_workbuddy.json",
                 "tokens_traecn.json", "traecn_device.json"):
        p = os.path.join(TMP, name)
        if os.path.exists(p):
            os.remove(p)
        p2 = os.path.join(C.LOG_DIR, name)
        if os.path.exists(p2):
            os.remove(p2)
    with open(C.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg if cfg is not None else {}, f, ensure_ascii=False)
    if tc is not None:
        with open(os.path.join(C.LOG_DIR, "tokens_traecn.json"), "w",
                  encoding="utf-8") as f:
            json.dump(tc, f, ensure_ascii=False)
    if wb is not None:
        with open(C.WB_TOKENS, "w", encoding="utf-8") as f:
            json.dump(wb, f, ensure_ascii=False)
    if dev is not None:
        with open(os.path.join(C.LOG_DIR, "traecn_device.json"), "w",
                  encoding="utf-8") as f:
            json.dump(dev, f, ensure_ascii=False)


def _rd(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


CFG_FULL = {
    "traework": {"enabled": True, "accounts": [
        {"name": "tw-a", "profile": "tw-a"},
        {"name": "tw-b", "profile": "tw-b"},
    ]},
    "workbuddy": {"enabled": True},
    "traecn": {"enabled": True},
    "schedule": {"checkin_time": "09:00", "retry_enabled": True},
    "options": {"headless": True},
}
TC_FULL = {"3956523050480058": {"token": "TOK_A", "exp": 9999999999,
                                "saved_at": 2000, "name": "Klein"}}
WB_FULL = {"6f2fe543": {"token": "TOK_W", "nickname": "Klein",
                        "captured_at": "2026-09-18 10:00:00"}}
DEV_FULL = {"did": "2443595114919243"}

# ============================================================ 1. 导出
print()
print("== 1. 导出（完整迁移 / 仅配置）==")
_seed(CFG_FULL, TC_FULL, WB_FULL, DEV_FULL)
_full_path = os.path.join(TMP, "full.json")
ok, detail, path = AB.export_bundle(_full_path, with_credentials=True)
check("完整导出成功", ok is True, detail)
check("返回了文件路径", path == _full_path, path)
check("文件确实生成", os.path.isfile(_full_path))
_doc = _rd(_full_path) or {}
check("format 正确", _doc.get("format") == AB.BUNDLE_FORMAT, _doc.get("format"))
check("version 正确", _doc.get("version") == AB.BUNDLE_VERSION)
check("标记为含凭据", _doc.get("with_credentials") is True)
check("含敏感文件警示字段", bool(_doc.get("_warning")))
_pl = _doc.get("payload") or {}
for _k in ("config", "tokens_traecn", "tokens_workbuddy", "traecn_device"):
    check(f"payload 含 {_k}", _k in _pl)
check("summary 账号数正确（2+1+1=4）",
      (_doc.get("summary") or {}).get("total_accounts") == 4,
      _doc.get("summary"))
check("summary 分平台计数正确",
      (_doc.get("summary") or {}).get("traework") == 2
      and (_doc.get("summary") or {}).get("traecn") == 1
      and (_doc.get("summary") or {}).get("workbuddy") == 1,
      _doc.get("summary"))
check("完整模式含令牌原文", "TOK_A" in json.dumps(_pl, ensure_ascii=False))

_cfg_path = os.path.join(TMP, "cfgonly.json")
ok, detail, _ = AB.export_bundle(_cfg_path, with_credentials=False)
check("仅配置导出成功", ok is True, detail)
_doc2 = _rd(_cfg_path) or {}
_pl2 = _doc2.get("payload") or {}
check("仅配置模式不含 tokens_traecn", "tokens_traecn" not in _pl2)
check("仅配置模式不含 tokens_workbuddy", "tokens_workbuddy" not in _pl2)
check("仅配置模式不含 traecn_device", "traecn_device" not in _pl2)
check("【负对照】仅配置模式文件里搜不到任何令牌原文",
      "TOK_A" not in json.dumps(_pl2, ensure_ascii=False)
      and "TOK_W" not in json.dumps(_pl2, ensure_ascii=False))
check("仅配置模式不含敏感警示（本来就没有凭据）",
      not _doc2.get("_warning"))
check("仅配置模式仍带账号与设置",
      len(((_pl2.get("config") or {}).get("traework") or {})
          .get("accounts") or []) == 2)

# 负对照：没有账号时必须拒绝导出，且不能留下空文件
_seed({"traework": {"accounts": []}})
_empty_path = os.path.join(TMP, "empty.json")
ok, detail, path = AB.export_bundle(_empty_path, with_credentials=True)
check("【负对照】没有账号时导出失败（不生成空包骗人）", ok is False, detail)
check("【负对照】失败时未创建文件", not os.path.isfile(_empty_path))
check("【负对照】失败文案给出可执行建议",
      "账号" in detail and "登录" in detail, detail)

# ============================================================ 2. 校验
print()
print("== 2. 校验 inspect_bundle（只读，不改本机）==")
_seed(CFG_FULL, TC_FULL, WB_FULL, DEV_FULL)
AB.export_bundle(_full_path, with_credentials=True)

_bad = os.path.join(TMP, "bad.json")
with open(_bad, "w", encoding="utf-8") as f:
    f.write("{ not json ")
check("坏 JSON 被拒", AB.inspect_bundle(_bad)[0] is False)

_wrong = os.path.join(TMP, "wrong.json")
with open(_wrong, "w", encoding="utf-8") as f:
    json.dump({"format": "something-else", "version": 1}, f)
check("【负对照】非本程序的包被拒（format 不符）",
      AB.inspect_bundle(_wrong)[0] is False)

_future = os.path.join(TMP, "future.json")
with open(_future, "w", encoding="utf-8") as f:
    json.dump({"format": AB.BUNDLE_FORMAT, "version": AB.BUNDLE_VERSION + 5,
               "payload": {}}, f)
_ok, _msg, _ = AB.inspect_bundle(_future)
check("【负对照】未来版本被拒且提示升级", _ok is False and "升级" in _msg, _msg)

_nopay = os.path.join(TMP, "nopay.json")
with open(_nopay, "w", encoding="utf-8") as f:
    json.dump({"format": AB.BUNDLE_FORMAT, "version": 1}, f)
check("【负对照】缺 payload 被拒", AB.inspect_bundle(_nopay)[0] is False)

check("不存在的文件被拒", AB.inspect_bundle(os.path.join(TMP, "nope.json"))[0]
      is False)

_ok, _msg, _info = AB.inspect_bundle(_full_path)
check("【正对照】正常包校验通过", _ok is True, _msg)
check("info 里 traework 数为 2", _info["traework"] == 2, _info)
check("info 里 traecn 数为 1", _info["traecn"] == 1, _info)
check("info 里 workbuddy 数为 1", _info["workbuddy"] == 1, _info)
check("info 标记含设备标识", _info["has_device_id"] is True, _info)
check("info 记录导出时间", bool(_info["exported_at"]), _info)

# ============================================================ 3. 导入
print()
print("== 3. 导入：并集 + 幂等 ==")
# 本机已有 1 个账号（与包里的 tw-a 重名，另有一个本机独有 tw-local）
_seed({"traework": {"enabled": True, "accounts": [
    {"name": "tw-a", "profile": "tw-a"},
    {"name": "tw-local", "profile": "tw-local"},
]}}, {}, {}, None)
ok, detail, stats = AB.import_bundle(_full_path)
check("导入成功", ok is True, detail)
_cfg = _rd(C.CONFIG_PATH) or {}
_names = [a.get("name") for a in
          (_cfg.get("traework") or {}).get("accounts") or []]
check("本机独有的 tw-local 未被丢掉", "tw-local" in _names, _names)
check("包里的 tw-a 未重复", _names.count("tw-a") == 1, _names)
check("包里的 tw-b 被并入", "tw-b" in _names, _names)
check("并集后共 3 个账号", len(_names) == 3, _names)
check("stats 报告新增 1 个", stats["traework_added"] == 1, stats)
check("设置项以导入为准（schedule 已带入）",
      (_cfg.get("schedule") or {}).get("checkin_time") == "09:00",
      _cfg.get("schedule"))

# 幂等：再导一次不应产生重复
ok2, detail2, stats2 = AB.import_bundle(_full_path)
_cfg2 = _rd(C.CONFIG_PATH) or {}
_names2 = [a.get("name") for a in
           (_cfg2.get("traework") or {}).get("accounts") or []]
check("重复导入仍成功", ok2 is True, detail2)
check("重复导入不新增账号", stats2["traework_added"] == 0, stats2)
check("重复导入后账号数仍为 3（幂等）", len(_names2) == 3, _names2)
check("重复导入文案说明「本机已有」", "已有" in detail2, detail2)

# ============================================================ 4. 令牌合并
print()
print("== 4. 令牌合并：同一 uid 保留更新的那一份 ==")
# 本机 A 的 saved_at=1000，包里 A 的 saved_at=2000 → 应取包里的
_seed({"traework": {"accounts": []}},
      {"A": {"token": "OLD", "exp": 1, "saved_at": 1000}},
      {}, None)
AB.import_bundle(_full_path)     # 包里是 uid 3956523050480058，先看新增
_tc = _rd(os.path.join(C.LOG_DIR, "tokens_traecn.json")) or {}
check("本机原有 uid 保留", "A" in _tc, list(_tc))
check("包里的 uid 被并入", "3956523050480058" in _tc, list(_tc))

# 直接构造同 uid 新旧对照
_seed({"traework": {"accounts": []}},
      {"X": {"token": "LOCAL_OLD", "exp": 1, "saved_at": 1000}},
      {}, None)
_pkg = os.path.join(TMP, "merge.json")
_src = _rd(_full_path)
_src["payload"]["tokens_traecn"] = {"X": {"token": "IMPORT_NEW", "exp": 1,
                                          "saved_at": 2000}}
with open(_pkg, "w", encoding="utf-8") as f:
    json.dump(_src, f, ensure_ascii=False)
AB.import_bundle(_pkg)
_tc = _rd(os.path.join(C.LOG_DIR, "tokens_traecn.json")) or {}
check("【正对照】包里 saved_at 更新 → 采用包里的令牌",
      _tc.get("X", {}).get("token") == "IMPORT_NEW", _tc.get("X"))

# 反向：包里的更旧 → 保留本机的
_seed({"traework": {"accounts": []}},
      {"X": {"token": "LOCAL_NEW", "exp": 1, "saved_at": 5000}},
      {}, None)
_src["payload"]["tokens_traecn"] = {"X": {"token": "IMPORT_OLD", "exp": 1,
                                          "saved_at": 100}}
with open(_pkg, "w", encoding="utf-8") as f:
    json.dump(_src, f, ensure_ascii=False)
AB.import_bundle(_pkg)
_tc = _rd(os.path.join(C.LOG_DIR, "tokens_traecn.json")) or {}
check("【负对照】包里 saved_at 更旧 → 保留本机令牌",
      _tc.get("X", {}).get("token") == "LOCAL_NEW", _tc.get("X"))

# WorkBuddy 令牌同样按 captured_at 合并
_seed({"traework": {"accounts": []}}, {},
      {"W": {"token": "WB_LOCAL", "captured_at": "2026-01-01 00:00:00"}}, None)
_src2 = _rd(_full_path)
_src2["payload"]["tokens_workbuddy"] = {
    "W": {"token": "WB_IMPORT", "captured_at": "2026-09-18 10:00:00"}}
with open(_pkg, "w", encoding="utf-8") as f:
    json.dump(_src2, f, ensure_ascii=False)
AB.import_bundle(_pkg)
_wb = _rd(C.WB_TOKENS) or {}
check("WorkBuddy 令牌按 captured_at 取更新的一份",
      _wb.get("W", {}).get("token") == "WB_IMPORT", _wb.get("W"))

# ============================================================ 5. 设备标识
print()
print("== 5. 设备标识 did：只在本机缺失时填入 ==")
_seed({"traework": {"accounts": []}}, {}, {}, None)
AB.import_bundle(_full_path)
_dev = _rd(os.path.join(C.LOG_DIR, "traecn_device.json")) or {}
check("【正对照】本机缺失 did → 从包里补上",
      _dev.get("did") == "2443595114919243", _dev)

_seed({"traework": {"accounts": []}}, {}, {}, {"did": "LOCAL_DID_999"})
AB.import_bundle(_full_path)
_dev = _rd(os.path.join(C.LOG_DIR, "traecn_device.json")) or {}
check("【负对照】本机已有 did → 绝不被覆盖",
      _dev.get("did") == "LOCAL_DID_999", _dev)

# 仅配置包不得写入任何凭据文件。
# ★ 这里必须传 None（=「不要创建这个文件」）；传 {} 会**建出空文件**，
#   于是断言 `not isfile(...)` 检的是「种子文件在不在」，而不是「导入写没写」——
#   这种「负对照自身失效」是假绿的典型来源。
_seed({"traework": {"accounts": []}}, None, None, None)
check("前置条件：沙箱里确实没有任何凭据文件",
      not os.path.isfile(os.path.join(C.LOG_DIR, "tokens_traecn.json"))
      and not os.path.isfile(C.WB_TOKENS)
      and not os.path.isfile(os.path.join(C.LOG_DIR, "traecn_device.json")))
AB.import_bundle(_cfg_path)
check("【负对照】仅配置包不写 traecn_device",
      not os.path.isfile(os.path.join(C.LOG_DIR, "traecn_device.json")))
check("【负对照】仅配置包不写 workbuddy 令牌",
      not os.path.isfile(C.WB_TOKENS))
check("【负对照】仅配置包不写 traecn 令牌",
      not os.path.isfile(os.path.join(C.LOG_DIR, "tokens_traecn.json")))
# 正对照：同一套前置条件下，完整包**必须**写出凭据文件
# （否则上面的负对照可能只是因为「导入压根不写文件」而通过）
_seed({"traework": {"accounts": []}}, None, None, None)
AB.import_bundle(_full_path)
check("【正对照】完整包会写出 workbuddy 令牌", os.path.isfile(C.WB_TOKENS))
check("【正对照】完整包会写出 traecn 令牌",
      os.path.isfile(os.path.join(C.LOG_DIR, "tokens_traecn.json")))

# ============================================================ 6. 备份
print()
print("== 6. 导入前自动备份 ==")
_seed(CFG_FULL, TC_FULL, WB_FULL, DEV_FULL)
_ok, _d, _st = AB.import_bundle(_full_path)
check("返回了备份目录", bool(_st.get("backup")), _st)
check("备份目录存在", os.path.isdir(_st.get("backup") or ""), _st.get("backup"))
check("备份里含 config.json",
      os.path.isfile(os.path.join(_st.get("backup") or "", "config.json")))
check("备份里含 traecn 令牌",
      os.path.isfile(os.path.join(_st.get("backup") or "",
                                  "tokens_traecn.json")))
check("导入文案里写明备份位置",
      "备份" in _d and (_st.get("backup") or "") in _d, _d)

# ============================================================ 7. 生产数据
print()
print("== 7. 生产数据未被污染（指纹比对）==")
C.CONFIG_PATH = _REAL["CONFIG_PATH"]
C.LOG_DIR = _REAL["LOG_DIR"]
C.WB_TOKENS = _REAL["WB_TOKENS"]
TC.TOKENS_PATH = _REAL["TC_TOKENS"]
_PROD_AFTER = _fingerprint(_PROD_FILES)
for _p in _PROD_FILES:
    check(f"未改动 {os.path.basename(_p)}",
          _PROD_AFTER[_p] == _PROD_BEFORE[_p],
          f"\n       before={_PROD_BEFORE[_p]}\n       after ={_PROD_AFTER[_p]}")
check("测试确实改道到了沙箱（生产路径 ≠ 沙箱路径）",
      os.path.normcase(C.CONFIG_PATH) != os.path.normcase(
          os.path.join(TMP, "config.json")))

# 清理沙箱
shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"结果：{OK} OK / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
