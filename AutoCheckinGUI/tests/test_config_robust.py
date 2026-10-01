#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checkin.load_config 健壮性测试。

核心场景：便携版 exe 首次运行时项目目录里还没有 config.json，
过去 `load_config()` 直接抛 FileNotFoundError，用户点「保存时间」
会看到 Python traceback。修复后应返回出厂默认值并补写一份。

导入顺序很关键：`checkin.CONFIG_PATH` 在 import 时按 `runtime.app_dir()`
求值，所以要先把 `checkin.ROOT` / `CONFIG_PATH` 指到临时目录再测。
"""
import os
import sys
import json
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)                 # desktop/AutoCheckinGUI
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
            else os.path.dirname(os.path.dirname(GUI_DIR)))   # 项目根（checkin.py 所在）
for _p in (PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import checkin  # noqa: E402

assert os.path.basename(os.path.dirname(checkin.__file__)) != "tests", checkin.__file__

OK = FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {extra}")


_orig_cfg = checkin.CONFIG_PATH

print("== 1. 文件缺失：返回默认值 + 补写磁盘 ==")
with tempfile.TemporaryDirectory() as td:
    checkin.CONFIG_PATH = os.path.join(td, "sub", "config.json")
    cfg = checkin.load_config()
    check("不抛异常", isinstance(cfg, dict))
    check("含 schedule", "schedule" in cfg)
    check("daily_times 是列表", isinstance(cfg["schedule"]["daily_times"], list))
    check("默认基准时间存在", bool(cfg["schedule"].get("checkin_time")))
    check("文件被补写到磁盘", os.path.exists(checkin.CONFIG_PATH))
    if os.path.exists(checkin.CONFIG_PATH):
        with open(checkin.CONFIG_PATH, encoding="utf-8") as f:
            written = json.load(f)
        check("补写内容可解析", isinstance(written, dict))
        check("补写内容与返回值一致",
              written.get("schedule") == cfg.get("schedule"))

print()
print("== 2. 文件损坏（非法 JSON）：也走默认值 ==")
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "config.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{ this is not json ][")
    checkin.CONFIG_PATH = p
    cfg = checkin.load_config()
    check("损坏文件不抛异常", isinstance(cfg, dict))
    check("回落到默认配置", "schedule" in cfg and "traework" in cfg)
    with open(p, encoding="utf-8") as f:
        check("损坏文件已被修复为合法 JSON", isinstance(json.load(f), dict))

print()
print("== 3. 空文件 ==")
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "config.json")
    open(p, "w").close()
    checkin.CONFIG_PATH = p
    cfg = checkin.load_config()
    check("空文件不抛异常", isinstance(cfg, dict))

print()
print("== 4. 正常文件：原样返回，不被默认值覆盖 ==")
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "config.json")
    custom = {"schedule": {"daily_times": ["07:15", "22:45"],
                           "checkin_time": "07:15"},
              "traework": {"enabled": False}, "my_marker": 12345}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(custom, f, ensure_ascii=False)
    checkin.CONFIG_PATH = p
    cfg = checkin.load_config()
    check("自定义时间被保留", cfg["schedule"]["daily_times"] == ["07:15", "22:45"])
    check("自定义额外字段被保留", cfg.get("my_marker") == 12345)
    check("未被默认配置污染", "traecn" not in cfg)

print()
print("== 5. 顶层不是 dict（如 JSON 数组）也要兜底 ==")
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "config.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
    checkin.CONFIG_PATH = p
    cfg = checkin.load_config()
    check("数组内容被替换为默认 dict", isinstance(cfg, dict) and "schedule" in cfg)

checkin.CONFIG_PATH = _orig_cfg

print()
print(f"结果：{OK} OK / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
