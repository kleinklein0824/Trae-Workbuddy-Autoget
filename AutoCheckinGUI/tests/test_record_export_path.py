#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""签到记录 CSV 自定义导出位置 —— 回归测试。

覆盖三件事：
  1. 取值口径：目录 / 完整文件名 / 未启用 三种情况（核心脚本与 GUI 必须一致）；
  2. 真的会同步一份副本，且**主记录不受影响**；
  3. 导出失败（路径不可写）时只返回失败原因，绝不抛异常 —— 导出是附加功能，
     不能让签到挂掉。

用 TRAEBUDDY_HOME 指到临时目录，不碰真实数据。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.abspath(os.path.join(HERE, ".."))
CORE_DIR = os.path.abspath(os.path.join(GUI_DIR, "..", "核心脚本"))

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + ((" — " + str(extra)) if extra else ""))


def write_cfg(tmp, enabled, path):
    import json
    cfg = {
        "traework": {"enabled": True, "accounts": []},
        "traecn": {"enabled": True},
        "workbuddy": {"enabled": True},
        "schedule": {"daily_times": ["09:00"]},
        "options": {"headless": True,
                    "record_export_enabled": enabled,
                    "record_export_path": path},
    }
    with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)


def fresh_home():
    tmp = tempfile.mkdtemp(prefix="tb_export_")
    os.makedirs(os.path.join(tmp, "logs"), exist_ok=True)
    # runtime.data_root() 认「含 checkin.py + 有数据」的目录为项目根，
    # 没有这个占位文件的话 TRAEBUDDY_HOME 会被跳过，测试会写到真实数据目录。
    with open(os.path.join(tmp, "checkin.py"), "w", encoding="utf-8") as f:
        f.write("# 测试占位：让 runtime 把本目录认作项目根\n")
    return tmp


def main():
    print("== 1. 核心脚本 checkin.py ==")
    # TRAEBUDDY_HOME 必须在 import 之前设置：checkin 的 ROOT 是模块级算好的
    tmp = fresh_home()
    write_cfg(tmp, False, "")
    os.environ["TRAEBUDDY_HOME"] = tmp
    for p in (CORE_DIR, GUI_DIR):
        if p not in sys.path:
            sys.path.insert(0, p)
    for m in ("checkin", "app", "app.config"):
        sys.modules.pop(m, None)

    import checkin as core
    check("数据根指向临时目录", os.path.abspath(core.ROOT) == os.path.abspath(tmp),
          core.ROOT)

    # 主记录先造一份
    with open(core.RECORD_PATH, "w", encoding="utf-8-sig") as f:
        f.write("日期,时间,平台,账号,结果,详情\n2026-09-29,12:00,TraeCN,Klein,成功,测试\n")

    check("未启用时目标为 None", core.record_export_target() is None)
    ok, info = core.sync_record_export()
    check("未启用时同步是空操作", (ok, info) == (False, ""), (ok, info))

    # ---- 目录形式 ----
    out_dir = os.path.join(tmp, "out")
    write_cfg(tmp, True, out_dir)
    tgt = core.record_export_target()
    check("目录形式 → 其下 签到记录.csv",
          tgt == os.path.join(out_dir, "签到记录.csv"), tgt)
    ok, info = core.sync_record_export()
    check("目录形式同步成功", ok and info == tgt, (ok, info))
    check("副本内容一致",
          os.path.isfile(tgt) and
          open(tgt, "rb").read() == open(core.RECORD_PATH, "rb").read())

    # ---- 完整文件名形式 ----
    out_file = os.path.join(tmp, "我的签到.csv")
    write_cfg(tmp, True, out_file)
    tgt2 = core.record_export_target()
    check(".csv 结尾 → 当作文件名", tgt2 == out_file, tgt2)
    ok, _ = core.sync_record_export()
    check("文件名形式同步成功", ok and os.path.isfile(out_file))

    # ---- 字符串 "false" 不能当成启用 ----
    write_cfg(tmp, "false", out_dir)
    check('字符串 "false" 视为关闭', core.record_export_target() is None)

    # ---- 目标就是主记录本身：应拒绝，不能自己复制自己 ----
    write_cfg(tmp, True, os.path.join(tmp, "logs"))
    ok, info = core.sync_record_export()
    check("目标=主记录时拒绝", (not ok) and "主记录本身" in info, (ok, info))

    # ---- 失败要收敛，不能抛（不存在的盘符）----
    write_cfg(tmp, True, "Z:\\不存在的盘\\out")
    try:
        ok, info = core.sync_record_export()
        check("不可写路径不抛异常", ok is False and bool(info), "ok=%s info=%s" % (ok, info))
    except Exception as e:  # noqa: BLE001
        check("不可写路径不抛异常", False, repr(e))
    check("失败后主记录仍完好", os.path.isfile(core.RECORD_PATH))

    print()
    print("== 2. GUI 侧 app/config.py（口径必须与核心脚本一致）==")
    tmp2 = fresh_home()
    write_cfg(tmp2, False, "")
    os.environ["TRAEBUDDY_HOME"] = tmp2
    for m in ("app", "app.config", "runtime"):
        sys.modules.pop(m, None)
    from app import config as C
    check("GUI 数据根指向临时目录",
          os.path.abspath(C.ROOT) == os.path.abspath(tmp2), C.ROOT)
    with open(C.RECORD_PATH, "w", encoding="utf-8-sig") as f:
        f.write("日期,时间\n2026-09-29,12:00\n")

    check("GUI 未启用 → None", C.record_export_target() is None)
    d2 = os.path.join(tmp2, "exported")
    write_cfg(tmp2, True, d2)
    check("GUI 目录形式一致",
          C.record_export_target() == os.path.join(d2, "签到记录.csv"),
          C.record_export_target())
    ok, info = C.export_record_now()
    check("GUI 立即导出成功", ok and os.path.isfile(info), (ok, info))
    check("主记录仍在数据根", os.path.isfile(C.RECORD_PATH))

    write_cfg(tmp2, "no", d2)
    check('GUI 字符串 "no" 视为关闭', C.record_export_target() is None)

    ok, msg = C.save_record_export(True, os.path.join(tmp2, "x.csv"))
    cfg2 = C.load_config()
    check("save_record_export 落盘",
          ok and cfg2["options"]["record_export_enabled"] is True
          and cfg2["options"]["record_export_path"].endswith("x.csv"), msg)

    print()
    for t in (tmp, tmp2):
        shutil.rmtree(t, ignore_errors=True)
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
