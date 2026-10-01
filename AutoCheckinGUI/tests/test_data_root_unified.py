#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据根目录一致性回归测试。

**这是本项目最贵的坑之一**：GUI 与命令行版（计划任务）各自解析数据目录，
一旦不一致就会出现：
  - 用户在 GUI 改设置 → 写进项目根 config.json
  - 计划任务跑 exe → 读 exe 旁的旧 config.json（设置「不生效」）
  - 签到结果写进 exe 旁 logs/ → GUI 里看不到（「自动签到好像没跑」）
  - traework 账号配置对不上 → 签到失败
  - 弹窗也走 exe 旁那套 → 用户看不到

所以这里必须断言：**源码模式与冻结模式都解析到同一个目录**。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
            else os.path.dirname(os.path.dirname(GUI_DIR)))   # 项目根（checkin.py 所在）
for _p in (PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime as RT                      # noqa: E402
from app import config as C               # noqa: E402
import checkin                            # noqa: E402

OK = FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {extra}")


print("== 1. 源码模式：GUI 与命令行解析到同一目录 ==")
check("checkin.ROOT 存在", bool(checkin.ROOT))
check("app.config.ROOT 存在", bool(C.ROOT))
check("两者相同", os.path.normcase(checkin.ROOT) == os.path.normcase(C.ROOT),
      f"\n       checkin={checkin.ROOT}\n       app    ={C.ROOT}")
check("都指向项目根", os.path.normcase(C.ROOT) == os.path.normcase(PROJ_ROOT),
      f"\n       ROOT   ={C.ROOT}\n       期望   ={PROJ_ROOT}")
check("项目根下确有 checkin.py", os.path.isfile(os.path.join(C.ROOT, "checkin.py")))
check("项目根下确有 config.json", os.path.isfile(os.path.join(C.ROOT, "config.json")))
check("runtime.data_root() 与之一致",
      os.path.normcase(RT.data_root()) == os.path.normcase(C.ROOT))

print()
print("== 2. 冻结模式（模拟便携 exe 位于 dist/ 子目录）==")
_dist_exe = os.path.join(GUI_DIR, "dist", "TraeBuddy签到助手", "TraeBuddy签到助手.exe")
_dist_dir = os.path.dirname(_dist_exe)
print(f"   模拟 exe = {_dist_exe}")
print(f"   （该目录当前{'存在' if os.path.isdir(_dist_dir) else '不存在'}）")

_orig_frozen = getattr(sys, "frozen", None)
_orig_exe = sys.executable
try:
    sys.frozen = True
    sys.executable = _dist_exe
    frozen_root = RT.data_root()
    check("冻结模式下仍解析到项目根（而非 exe 旁）",
          os.path.normcase(frozen_root) == os.path.normcase(PROJ_ROOT),
          f"\n       得到 ={frozen_root}\n       期望 ={PROJ_ROOT}")
    check("冻结模式 app_dir() 是 exe 所在目录",
          os.path.normcase(RT.app_dir()) == os.path.normcase(_dist_dir),
          RT.app_dir())
    check("冻结模式 GUI 侧也用同一目录",
          os.path.normcase(RT.data_root(hint=os.path.join(GUI_DIR, "app")))
          == os.path.normcase(PROJ_ROOT))
    cands = RT.search_candidates(hint=os.path.join(GUI_DIR, "app"))
    check("候选里包含项目根", any(os.path.normcase(c) == os.path.normcase(PROJ_ROOT)
                                for c in cands))
    check("候选里包含 exe 自身目录",
          any(os.path.normcase(c) == os.path.normcase(_dist_dir) for c in cands))
finally:
    if _orig_frozen is None:
        try:
            del sys.frozen
        except AttributeError:
            pass
    else:
        sys.frozen = _orig_frozen
    sys.executable = _orig_exe

print()
print("== 3. TRAEBUDDY_HOME 优先级最高 ==")
import tempfile  # noqa: E402
with tempfile.TemporaryDirectory() as td:
    _orig_home = os.environ.get("TRAEBUDDY_HOME")
    try:
        os.environ["TRAEBUDDY_HOME"] = td
        # 该目录没数据没项目 → 按规则会落到别的分支；
        # 但要确认它排在最前（search_candidates 第 1 位）
        check("TRAEBUDDY_HOME 是第一个候选",
              os.path.normcase(RT.search_candidates()[0]) == os.path.normcase(td),
              RT.search_candidates()[0])
        # 造一份「项目 + 数据」，应被选中
        with open(os.path.join(td, "checkin.py"), "w", encoding="utf-8") as f:
            f.write("# stub\n")
        os.makedirs(os.path.join(td, "logs"), exist_ok=True)
        check("指定目录含项目+数据时被采纳",
              os.path.normcase(RT.data_root()) == os.path.normcase(td),
              RT.data_root())
    finally:
        if _orig_home is None:
            os.environ.pop("TRAEBUDDY_HOME", None)
        else:
            os.environ["TRAEBUDDY_HOME"] = _orig_home

print()
print("== 4. 空目录兜底（便携版新解压）==")
with tempfile.TemporaryDirectory() as td:
    _orig_frozen2 = getattr(sys, "frozen", None)
    _orig_exe2 = sys.executable
    _orig_home2 = os.environ.get("TRAEBUDDY_HOME")
    try:
        os.environ.pop("TRAEBUDDY_HOME", None)
        sys.frozen = True
        sys.executable = os.path.join(td, "TraeBuddy签到助手.exe")
        # 空目录：没有任何项目/数据 → 应落到 exe 旁（可写）
        got = RT.data_root(hint=td)
        check("空目录时落到 exe 旁（可写）",
              os.path.normcase(got) == os.path.normcase(td),
              f"得到 {got}")
    finally:
        if _orig_frozen2 is None:
            try:
                del sys.frozen
            except AttributeError:
                pass
        else:
            sys.frozen = _orig_frozen2
        sys.executable = _orig_exe2
        if _orig_home2 is not None:
            os.environ["TRAEBUDDY_HOME"] = _orig_home2

print()
print("== 5. Windows 用户目录兜底（%APPDATA% 缺失也不能静默 0 个账号）==")
# 背景：账号发现整条链路挂在 %APPDATA% / %LOCALAPPDATA% 上。而
# `os.path.expandvars` 在变量**不存在**时会把 `%APPDATA%` **原样留下**，
# 拼出来的路径永远不存在 → 发现层**静默返回 0 个账号**（界面只说
# "没有可用的账号"，日志里一句报错都没有）——本项目最典型的「假故障」形态。
import tc_checkin as TC                  # noqa: E402

_dirs = RT.win_user_dirs("roaming")
check("win_user_dirs('roaming') 返回非空列表",
      isinstance(_dirs, list) and len(_dirs) >= 1, _dirs)
check("每一项都是绝对路径且不含未解析的 %",
      all(os.path.isabs(d) and "%" not in d for d in _dirs), _dirs)

# ---- 正对照：把 APPDATA 抹掉，仍必须给出可用的兜底目录 ----
_env_bak = {k: os.environ.get(k) for k in ("APPDATA", "LOCALAPPDATA")}
try:
    os.environ.pop("APPDATA", None)
    os.environ.pop("LOCALAPPDATA", None)
    _r = RT.win_user_dirs("roaming")
    _l = RT.win_user_dirs("local")
    _home = os.path.expanduser("~")
    check("【正对照】APPDATA 缺失时仍回退到 ~/AppData/Roaming",
          any(os.path.normcase(p) == os.path.normcase(
              os.path.join(_home, "AppData", "Roaming")) for p in _r), _r)
    check("【正对照】LOCALAPPDATA 缺失时仍回退到 ~/AppData/Local",
          any(os.path.normcase(p) == os.path.normcase(
              os.path.join(_home, "AppData", "Local")) for p in _l), _l)
    check("【正对照】tc_checkin._appdata_roots() 不为空",
          bool(TC._appdata_roots()), TC._appdata_roots())
    import wb_checkin as WB2              # noqa: E402
    _ac = WB2._auth_candidates()
    check("【正对照】WorkBuddy 候选路径非空且无未解析的 %",
          bool(_ac) and all("%" not in p for p in _ac), _ac)
    check("【正对照】WorkBuddy 候选含 ~/AppData/Local 那条",
          any(os.path.normcase(p).startswith(
              os.path.normcase(os.path.join(_home, "AppData", "Local")))
              for p in _ac), _ac)

    # ---- 负对照：旧写法在这种环境下给出的就是「永远不存在」的路径 ----
    _old = os.path.expandvars(r"%APPDATA%\Trae CN\logs")
    check("【负对照】旧写法（expandvars）会留下未解析的 % → 路径永远不存在",
          "%" in _old, _old)
finally:
    for _k, _v in _env_bak.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

# ---- 去重：APPDATA 与兜底目录指向同一处时不能出现重复项（否则会重复扫日志）----
_home2 = os.path.expanduser("~")
_orig_appdata = os.environ.get("APPDATA")
try:
    os.environ["APPDATA"] = os.path.join(_home2, "AppData", "Roaming")
    _dd = RT.win_user_dirs("roaming")
    check("APPDATA 与兜底目录重合时去重（只留一份）",
          len(_dd) == 1, _dd)
    check("client_log_roots 不返回重复路径",
          len({os.path.normcase(r[0]) for r in TC.client_log_roots()})
          == len(TC.client_log_roots()),
          [r[0] for r in TC.client_log_roots()])
finally:
    if _orig_appdata is None:
        os.environ.pop("APPDATA", None)
    else:
        os.environ["APPDATA"] = _orig_appdata

print()
print("== 4. ★ 冻结模式：令牌/设备码文件不能落到 PyInstaller 的 bundle 里 ==")
#   背景（2026-09-22 修的真缺陷）：`wb_checkin` / `tc_checkin` 原先用
#   `dirname(__file__)` 当根。**源码模式没问题**（那目录就是项目根），
#   但**冻结后 `__file__` 指向 bundle**（onedir 是 `_internal/`）→
#     · `wb_checkin.TOKENS_FILE` = `<exe旁>/_internal/tokens_workbuddy.json`
#     · `tc_checkin.TOKENS_PATH` = `<exe旁>/_internal/logs/tokens_traecn.json`
#   而 GUI 侧 `C.WB_TOKENS` / `C.LOG_DIR` 用的是 `data_root()`（exe 旁）——
#   **同一个文件两个路径**。后果：账号页「移除令牌快照」改的是后者、
#   签到读的是前者 → **移除无效**；账号迁移导出的也不是签到真正用的那份；
#   而且 `_internal/` 升级时被整体替换 → 令牌快照与设备码**静默丢失**。
#
#   ★ 这个缺陷**在源码模式测不出来**（两边本来就一致），必须**模拟冻结**：
#     把三个模块复制进一个临时「bundle」目录、从那里 import，
#     并令 `sys._MEIPASS` 指向它 —— 这样 `dirname(__file__)` 就是 bundle，
#     与真实冻结场景同构。
import shutil                                                    # noqa: E402
import subprocess                                                # noqa: E402
import tempfile                                                  # noqa: E402

_BUNDLE = tempfile.mkdtemp(prefix="tb_frozen_bundle_")
_HOME = tempfile.mkdtemp(prefix="tb_frozen_home_")
for _f in ("runtime.py", "wb_checkin.py", "tc_checkin.py"):
    shutil.copy2(os.path.join(PROJ_ROOT, _f), os.path.join(_BUNDLE, _f))
# TRAEBUDDY_HOME 指向「含项目+数据」的目录 → data_root() 稳定落在它上面
open(os.path.join(_HOME, "checkin.py"), "w", encoding="utf-8").write("# stub\n")
open(os.path.join(_HOME, "config.json"), "w", encoding="utf-8").write("{}")

_FROZEN_CODE = (
    "import json, os, sys;"
    "BUNDLE = %r; HOME = %r; GUI = %r;" % (_BUNDLE, _HOME, GUI_DIR) +
    "sys.frozen = True;"                      # ★ 模拟 PyInstaller 冻结
    "sys._MEIPASS = BUNDLE;"
    "os.environ['TRAEBUDDY_HOME'] = HOME;"
    "sys.path.insert(0, GUI);"
    "sys.path.insert(0, BUNDLE);"
    "import runtime, wb_checkin, tc_checkin;"
    "from app import config as C;"
    "print(json.dumps({"
    " 'mei': runtime.bundle_dir(),"
    " 'root': runtime.data_root(),"
    " 'wb_file_dir': os.path.dirname(os.path.abspath(wb_checkin.__file__)),"
    " 'old_formula': os.path.join(os.path.dirname(os.path.abspath("
    "     wb_checkin.__file__)), 'tokens_workbuddy.json'),"
    " 'wb': wb_checkin.TOKENS_FILE,"
    " 'tc': tc_checkin.TOKENS_PATH,"
    " 'dev': tc_checkin.DEVICE_PATH,"
    " 'cfg_wb': C.WB_TOKENS,"
    " 'log': C.LOG_DIR,"
    "}))"
)
_r = subprocess.run([sys.executable, "-c", _FROZEN_CODE], cwd=_BUNDLE,
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=120,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
if _r.returncode != 0 or not (_r.stdout or "").strip():
    check("冻结模式子进程跑通", False, f"rc={_r.returncode} "
          f"out={(_r.stdout or '')[:200]!r} err={(_r.stderr or '')[:300]}")
else:
    import json as _json
    _d = _json.loads(_r.stdout.strip().splitlines()[-1])
    check("【前置】模拟确实生效：__file__ 在 bundle 里、_MEIPASS == bundle",
          os.path.normcase(_d["wb_file_dir"]) == os.path.normcase(_d["mei"]),
          f"{_d['wb_file_dir']} vs {_d['mei']}")
    check("data_root() 落在 TRAEBUDDY_HOME",
          os.path.normcase(_d["root"]) == os.path.normcase(_HOME), _d["root"])
    check("★ WorkBuddy 令牌快照**不在** bundle 里（升级不会丢）",
          not os.path.normcase(_d["wb"]).startswith(
              os.path.normcase(_d["mei"]) + os.sep), _d["wb"])
    check("★ WorkBuddy 令牌快照落在 data_root() 下",
          os.path.normcase(_d["wb"]) == os.path.normcase(
              os.path.join(_HOME, "tokens_workbuddy.json")), _d["wb"])
    check("★ 与 GUI 侧 C.WB_TOKENS **同一个文件**（不再分家）",
          os.path.normcase(_d["wb"]) == os.path.normcase(_d["cfg_wb"]),
          f"{_d['wb']} vs {_d['cfg_wb']}")
    check("★ TraeCN 令牌缓存在 C.LOG_DIR 下",
          os.path.normcase(os.path.dirname(_d["tc"])) == os.path.normcase(_d["log"]),
          _d["tc"])
    check("★ TraeCN 设备码在 C.LOG_DIR 下（升级不会重生成）",
          os.path.normcase(os.path.dirname(_d["dev"])) == os.path.normcase(_d["log"]),
          _d["dev"])
    check("【正对照】旧写法（dirname(__file__)）确实会落到 bundle 里"
          "（证明上面那几条不是恒真）",
          os.path.normcase(_d["old_formula"]).startswith(
              os.path.normcase(_d["mei"]) + os.sep), _d["old_formula"])

# ---- 无损迁移：新位置没有、旧位置有时，旧文件要被接管过来 ----
_LEG = tempfile.mkdtemp(prefix="tb_legacy_")
with open(os.path.join(_LEG, "tokens_workbuddy.json"), "w", encoding="utf-8") as f:
    f.write('{"uid_demo": {"token": "T"}}')
_orig_home = os.environ.get("TRAEBUDDY_HOME")
try:
    os.environ["TRAEBUDDY_HOME"] = _HOME
    _got = RT.data_file("tokens_workbuddy.json", legacy_base=_LEG)
    _ok_moved = (os.path.normcase(_got) == os.path.normcase(
        os.path.join(_HOME, "tokens_workbuddy.json"))
        and os.path.isfile(_got)
        and not os.path.exists(os.path.join(_LEG, "tokens_workbuddy.json")))
    check("★ 旧位置的文件被无损接管到 data_root()（搬过去，不是丢）", _ok_moved, _got)
    # 正对照：新位置已有文件时，不能覆盖它
    with open(os.path.join(_LEG, "tokens_workbuddy.json"), "w", encoding="utf-8") as f:
        f.write('{"newer": 1}')
    _got2 = RT.data_file("tokens_workbuddy.json", legacy_base=_LEG)
    with open(_got2, encoding="utf-8") as f:
        _body = f.read()
    check("【正对照】新位置已有内容时不被旧文件覆盖",
          "uid_demo" in _body, _body)
finally:
    if _orig_home is None:
        os.environ.pop("TRAEBUDDY_HOME", None)
    else:
        os.environ["TRAEBUDDY_HOME"] = _orig_home

print()
print(f"结果：{OK} OK / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
