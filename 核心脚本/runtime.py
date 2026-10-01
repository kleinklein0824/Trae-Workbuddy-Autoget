#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行时环境抽象：让同一套代码既能以「脚本 + Python 解释器」运行，
也能以「PyInstaller 打包的 exe」运行。

为什么需要这个模块
------------------
打包后的 `sys.executable` 指向 exe 本身，而不是 python.exe。原代码里
`subprocess.Popen([sys.executable, "checkin.py", "run"])` 在 exe 模式下
会变成「用 exe 去执行 checkin.py」——直接失效，计划任务也随之失灵。

因此统一通过本模块取「用什么命令跑自己」，并统一管理数据目录：
  * 脚本模式：命令 = [pythonw.exe, <项目根>/checkin.py]
  * exe  模式：命令 = [<exe 路径>]（exe 内部再按参数分发到 checkin.main）

数据目录（config.json / logs / profiles / tokens）始终放在「程序根目录」，
exe 模式下取 exe 所在目录，保证用户可直接看到并备份这些文件。
"""
import os
import sys

# Windows 下打包后不希望弹黑框
_CREATE_NO_WINDOW = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)


def is_frozen():
    """是否运行在 PyInstaller 打出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_dir():
    """程序根目录：脚本模式=本文件所在目录；exe 模式=exe 所在目录。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# ---------------- Windows 用户目录（环境变量缺失时的兜底） ----------------
def win_user_dirs(kind):
    """返回某个 Windows 用户目录的候选列表（按优先级、去重保序）。

    kind: "roaming"（对应 %APPDATA%）或 "local"（对应 %LOCALAPPDATA%）。

    【为什么需要兜底】账号发现整条链路都挂在这两个环境变量上：
      * `%APPDATA%\\Trae CN\\logs`             —— Trae CN IDE 日志（明文 JWT 的主要来源）
      * `%APPDATA%\\TRAE SOLO CN\\logs`        —— TraeWork CN 桌面端日志（身份识别）
      * `%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info`
                                               —— WorkBuddy 桌面端登录态

    而 `os.path.expandvars()` 在变量**不存在**时会把 `%APPDATA%` **原样留下**，
    拼出来的路径永远不存在 → 发现层**静默返回 0 个账号**：界面只说
    "没有可用的账号"，日志里也没有任何报错。这是本项目最典型的
    「假故障」形态（对比数据目录错位的指纹：界面 0 个账号、发现区却有内容）。
    环境变量异常在真实场景里是有的：受限账户/服务账户下跑计划任务、
    从某些精简的 shell 或自启脚本里拉起程序等。

    因此这里补上 `~/AppData/Roaming|Local` 作为兜底候选。正常用户环境下
    环境变量都在，兜底只是保险，**不改变原有优先级**。
    """
    env = "APPDATA" if kind == "roaming" else "LOCALAPPDATA"
    sub = "Roaming" if kind == "roaming" else "Local"
    cands = []
    val = os.environ.get(env)
    if val:
        cands.append(val)
    home = os.path.expanduser("~")
    if home and home != "~":
        cands.append(os.path.join(home, "AppData", sub))
    seen, uniq = set(), []
    for p in cands:
        k = os.path.normcase(os.path.normpath(p))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


# ---------------- 数据根目录（GUI 与命令行必须用同一套判定） ----------------
# 【为什么必须统一】曾经 GUI 走 app/config.py 的智能搜索（能找到项目根），
# 而 checkin.py 直接用 app_dir()（= exe 所在目录）。两者不一致时会出现：
#   用户在 GUI 改设置 → 写进项目根 config.json；
#   计划任务跑 exe → 读 exe 旁的 config.json（旧值）；
#   签到结果写进 exe 旁 logs/ → GUI 里什么都看不到。
# 表现就是「设置不生效、自动签到好像没跑、弹窗也没有」。
# 现在两边都调 data_root()，从根上杜绝分家。

_HOME_ENV = "TRAEBUDDY_HOME"


def _has_data(base):
    """该目录是否像一份已有的项目数据（有 config.json 或 logs/）。"""
    if not base or not os.path.isdir(base):
        return False
    return (os.path.isfile(os.path.join(base, "config.json"))
            or os.path.isdir(os.path.join(base, "logs")))


def _has_project(base):
    """该目录是否是完整的项目根（含 checkin.py）。"""
    return bool(base) and os.path.isfile(os.path.join(base, "checkin.py"))


def _ancestors(start, levels=5):
    """逐级上溯的父目录列表（用于「exe 被放在 dist/ 子目录里」这类情况）。"""
    out = []
    cur = os.path.abspath(start)
    for _ in range(levels):
        nxt = os.path.dirname(cur)
        if not nxt or nxt == cur:
            break
        cur = nxt
        out.append(cur)
    return out


def search_candidates(hint=None):
    """按优先级列出「可能已有数据」的候选目录。"""
    out = []

    def add(p):
        if p:
            p = os.path.abspath(p)
            if p not in out:
                out.append(p)

    # 1) 显式指定（用户可用环境变量把程序指到自己的项目目录）
    add(os.environ.get(_HOME_ENV))
    # 2) exe 自身目录 + 逐级上溯（便携版数据放 exe 旁；也兼容 exe 在 dist/ 里的布局）
    if is_frozen():
        fr = os.path.dirname(os.path.abspath(sys.executable))
        add(fr)
        ancestors = _ancestors(fr)
        # 整理后的桌面版结构：桌面版/核心脚本 与
        # 桌面版/AutoCheckinGUI/dist/<程序> 为同一版本，
        # 必须优先认领旁边的核心脚本数据，不能回退到项目根脚本版。
        for p in ancestors:
            add(p)
            if os.path.basename(p).lower() == "autocheckingui":
                add(os.path.join(os.path.dirname(p), "核心脚本"))
    # 3) 本文件所在目录 + 逐级上溯（脚本模式即项目根）
    add(app_dir())
    for p in _ancestors(app_dir()):
        add(p)
    # 4) 调用方提示目录（GUI 传 app/ 所在目录，便于源码模式定位项目根）
    #    只在**源码模式**下采纳：冻结后 app/ 在解包目录里，这个 hint 没有意义，
    #    反而会让「被放到别处的便携 exe」意外认领源目录旁边的项目数据。
    #    exe 场景下的定位已由上面的「exe 目录 + 逐级上溯」完整覆盖。
    if hint and not is_frozen():
        add(hint)
        for p in _ancestors(hint):
            add(p)
    # 5) 用户级兜底（首次运行时在这初始化）
    # 注意：这里**不要**写死 `~/Desktop/Auto get` 之类具体路径 —— 那会让
    # 「放在任意位置的便携 exe」意外认领某台机器上的项目目录，行为难以预测。
    # 项目根的定位已由「exe 上级目录」与「app_dir() 上级目录」通用覆盖。
    add(os.path.join(os.path.expanduser("~"), ".traebuddy"))
    return out


def data_root(hint=None):
    """定位数据根目录（config.json / logs / profiles / tokens 的落点）。

    优先级：
      1. 环境变量 TRAEBUDDY_HOME 指定的目录（最强，用户显式指定）
      2. 已有完整项目（含 checkin.py）+ 数据的目录 —— GUI 与计划任务共用
      3. exe 旁边已有数据（便携版用户自己放的数据）
      4. exe 旁边（新装时的初始化位置）
      5. 用户级 ~/.traebuddy（exe 放在只读位置时的兜底）
    """
    cands = search_candidates(hint)
    for base in cands:
        if _has_project(base) and _has_data(base):
            return base
    for base in cands:
        if _has_project(base):
            return base
    if is_frozen():
        fr = os.path.dirname(os.path.abspath(sys.executable))
        if _has_data(fr):
            return fr
        # exe 旁可写就用 exe 旁，否则退回用户目录（安装到 Program Files 的场合）
        if os.access(fr, os.W_OK):
            return fr
        return os.path.join(os.path.expanduser("~"), ".traebuddy")
    for base in cands:
        if os.path.exists(os.path.join(base, "checkin.py")):
            return base
    cur = app_dir()
    for _ in range(5):
        cur = os.path.dirname(cur)
        if os.path.exists(os.path.join(cur, "checkin.py")):
            return cur
    return cands[0] if cands else app_dir()


def bundle_dir():
    """只读资源目录：exe 模式下指向 PyInstaller 解包目录。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", app_dir())
    return app_dir()


def in_test_harness():
    """当前进程是不是**测试脚本**（测试绝不该写用户的真实数据/凭据文件）。

    ★ 为什么必须有这个函数（2026-09-25 踩出来的事故）：
      `App.__init__` 会在 1.6 秒后触发**凭据自动刷新**，而**32 个测试脚本**
      都会构造 `App()`。于是跑一次测试就可能真的写用户的
      `tokens_workbuddy.json` / `logs/`。
      实测后果：某个测试给 `wb_checkin._load_saved` 打了桩（返回测试假数据），
      而 `capture_current()` 内部是「`saved = _load_saved()` → 加账号 →
      `_save_saved(saved)`」—— 这一写就把**测试夹具**混进了真实快照，
      界面上凭空多出两个「已捕获的账号」，ID 与名称都不对。
      （用户看到的就是「有些账号捕获的 ID 名称显示不对」。）

    判据（**保守**：拿不准就当作"不是测试"，生产行为绝不受影响）：
      1. 显式设了 `TRAEBUDDY_NO_CRED_REFRESH=1` → 是；
      2. 冻结 exe（`is_frozen()`）→ **不是**（正常生产运行）；
      3. `sys.argv[0]` 是 `.py` 且路径里含 `tests` 目录 → 是。
    """
    if os.environ.get("TRAEBUDDY_NO_CRED_REFRESH"):
        return True
    if is_frozen():
        return False
    try:
        argv0 = os.path.abspath(sys.argv[0] or "")
    except Exception:  # noqa: BLE001
        return False
    if not argv0.lower().endswith(".py"):
        return False
    return "tests" in [p.lower() for p in argv0.split(os.sep)]


def data_file(*parts, legacy_base=None):
    """**可写数据文件**的落点：一律放在 `data_root()` 下，并接管旧位置的文件。

    ★ 为什么必须有这个函数（2026-09-22 修的真缺陷）
    -------------------------------------------------
    `wb_checkin.py` / `tc_checkin.py` 原先用
    `os.path.dirname(os.path.abspath(__file__))` 当根。**源码模式没问题**
    （那个目录就是项目根），但**冻结后 `__file__` 指向 PyInstaller 的 bundle**
    （onedir 是 `_internal/`）→ 于是：

      · `wb_checkin.TOKENS_FILE`      = `<exe旁>/_internal/tokens_workbuddy.json`
      · `tc_checkin.TOKENS_PATH`      = `<exe旁>/_internal/logs/tokens_traecn.json`
      · `tc_checkin.DEVICE_PATH`      = `<exe旁>/_internal/logs/traecn_device.json`

    而 GUI 侧（`app/config.py`）用的是 `data_root()`：
      · `C.WB_TOKENS` = `<exe旁>/tokens_workbuddy.json`
      · `C.LOG_DIR`   = `<exe旁>/logs`

    ⇒ **同一个文件出现两个互不相干的路径**。实测后果：
      · 账号页「移除令牌快照」改的是 `C.WB_TOKENS`，而签到读的是
        `wb_checkin.TOKENS_FILE` → **移除无效**（签到仍读得到旧快照）；
      · 账号迁移（导出/导入）走 `C.WB_TOKENS` → 与签到实际用的文件**不是同一个**；
      · `<exe旁>/_internal/` 在**升级/重装时会被整体替换** → 里面的令牌快照
        与设备码**静默丢失**。

    `legacy_base` 传「旧实现的目录」（通常是 `dirname(__file__)`）：
    新位置没有、旧位置有时，把旧文件**搬过来**（搬不动就复制），保证无损迁移。
    """
    base = data_root()
    dest = os.path.join(base, *parts)
    if legacy_base:
        old = os.path.join(legacy_base, *parts)
        try:
            if os.path.normcase(old) != os.path.normcase(dest) \
                    and os.path.exists(old) and not os.path.exists(dest):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                try:
                    os.replace(old, dest)          # 同卷：原子移动
                except OSError:
                    import shutil as _sh
                    _sh.copy2(old, dest)           # 跨卷/被占用：退回复制
        except OSError:
            pass
    return dest


def pythonw():
    """返回可用于后台静默运行的 Python 解释器（脚本模式）。"""
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def self_command(*args):
    """构造「重新调用自己」的命令行列表。

    脚本模式：[pythonw.exe, checkin.py, *args]
    exe  模式：[当前exe, *args]   （exe 入口负责分发到 checkin.main）
    """
    if is_frozen():
        return [sys.executable, *[str(a) for a in args]]
    script = os.path.join(app_dir(), "checkin.py")
    return [pythonw(), script, *[str(a) for a in args]]


def subprocess_flags():
    """后台静默运行子进程所需的 creationflags。"""
    return _CREATE_NO_WINDOW if os.name == "nt" else 0


def ensure_playwright_browsers():
    """exe 模式下把 Playwright 的浏览器目录指回**系统安装位置**。

    为什么需要：PyInstaller 打包 playwright 时，包内会带一个空的
    `_internal/playwright/driver/package/.local-browsers/`，而浏览器二进制
    有几百 MB、不会被打进去。于是 exe 跑 TraeWork 签到时会直接报：

        Executable doesn't exist at ...\\.local-browsers\\
        chromium_headless_shell-1234\\chrome-headless-shell-win64\\
        chrome-headless-shell.exe

    而本机通常已经在 `%LOCALAPPDATA%\\ms-playwright` 装好了浏览器
    （源码模式跑过一次就会装）。这里在导入 playwright **之前**把
    `PLAYWRIGHT_BROWSERS_PATH` 指过去，exe 与源码就能共用同一份浏览器。

    返回最终生效的浏览器目录（None 表示未改动）。
    """
    if not is_frozen():
        return None
    bundled = os.path.join(bundle_dir(), "playwright", "driver", "package",
                           ".local-browsers")
    has_bundled = os.path.isdir(bundled) and bool(os.listdir(bundled))
    if has_bundled:
        return os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or bundled

    local = os.environ.get("LOCALAPPDATA") or ""
    sys_browsers = os.path.join(local, "ms-playwright") if local else ""
    if sys_browsers and os.path.isdir(sys_browsers):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = sys_browsers
        return sys_browsers
    # 系统里也没有：保持原样，让 playwright 报出它自己的错误（便于排障）
    return os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
