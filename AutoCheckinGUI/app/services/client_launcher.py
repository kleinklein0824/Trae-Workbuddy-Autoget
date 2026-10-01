#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端启动器 —— 「自动跳转对应软件引导用户登录」的落地实现。

为什么需要它
------------
三个平台用的是**三套完全不同的登录机制**，签到脚本只能「读结果」，不能替用户登录：

  · TraeCN    —— 令牌写在 ``%APPDATA%\\Trae CN\\logs`` 的 IDE 日志里（明文 JWT）
  · TraeWork  —— Playwright 网页会话（``profiles/traework/<profile>``），
                 必须在真实浏览器里登录一次；桌面端（TRAE SOLO CN）与它同属
                 api.trae.cn 账号体系，可作身份旁证
  · WorkBuddy —— 桌面端登录态文件
                 ``%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info``

结论：本项目里**不可能凭空实现「软件内扫码」**——三个平台都没有给第三方程序用的
扫码接口，二维码只存在于它们各自的客户端里。真正能做到、也确实有用的是：

    **找到并拉起对应客户端 → 明确告诉用户「在这个窗口里登录」→ 回来点扫描/捕获**

这正是本模块负责的第一段。

不写死安装路径（换设备 / 换安装位置也要能找到）
------------------------------------------------
查找顺序（先便宜后昂贵，命中即停）：

  ① 显式候选路径（环境变量展开；缺失时用 ``runtime.win_user_dirs`` 兜底）
  ② 各固定磁盘根目录下的常见相对路径（实测本机装在 ``D:\\Trae CN``，
     任何「只查 %LOCALAPPDATA%\\Programs」的写法都会全落空）
  ③ 注册表 App Paths（``HKLM/HKCU ...\\App Paths\\<exe>``）—— 最权威、几乎零成本
  ④ 注册表 Uninstall 键 —— ★ ``DisplayIcon`` 才是可靠字段，``InstallLocation`` 常为 None
  ⑤ 开始菜单 ``.lnk``（PowerShell 解析，带超时；失败只是少一层，不影响可用性）
  ⑥ PATH（``shutil.which``）

★ 已踩过的坑：本机同时装着 ``WorkBuddy.exe``（签到目标）与 ``WorkBuddyAI.exe``
  （WorkBuddy AI 编辑器，**另一个账号**），两者登录态文件分别是
  ``workbuddy-desktop.info`` / ``workbuddy-desktop-ai.info``。
  因此每个平台除了「显示名」匹配，还额外用**可执行文件名白名单**做二次校验，
  避免把用户引导到错误的软件里去。

本模块只读 + 启动进程，不改任何文件、不碰任何凭据。
"""
import functools
import os
import re
import shutil
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")

# ---------------------------------------------------------------- 平台描述

# ★ 每个平台都要「显示名匹配」+「可执行文件名白名单」双重校验。
#   exe_names 是最后一道闸：哪怕注册表显示名匹配错了，拉起来的也不会是别的软件。
SPECS = {
    "traecn": {
        "label": "Trae CN IDE",
        "exe_names": ("trae cn.exe", "trae.exe"),
        "proc_names": ("trae cn.exe", "trae.exe"),
        # 注册表 Uninstall 里的 DisplayName（实测为 "TraeCode CN (User)"）
        "name_re": re.compile(r"(trae\s*code\s*cn|^trae\s*cn\b|trae\s*cn\s*ide)",
                              re.I),
        "name_exclude_re": re.compile(r"traework|solo|workbuddy", re.I),
        "explicit": (
            r"%LOCALAPPDATA%\Programs\Trae CN\Trae CN.exe",
            r"%LOCALAPPDATA%\Programs\Trae\Trae.exe",
            r"%PROGRAMFILES%\Trae CN\Trae CN.exe",
            r"%PROGRAMFILES(X86)%\Trae CN\Trae CN.exe",
            r"%LOCALAPPDATA%\Trae CN\Trae CN.exe",
        ),
        # 磁盘根目录下的常见相对路径（覆盖 D:\Trae CN 这类自定义安装位置）
        "drive_rel": (
            r"Trae CN\Trae CN.exe",
            r"TraeCode CN\Trae CN.exe",
            r"Program Files\Trae CN\Trae CN.exe",
        ),
        "web": None,
        "guide": ("在 Trae CN 里用要签到的账号登录（手机号 / 微信扫码都行）。\n"
                  "登录完成后回到本软件，点「重新扫描 IDE 令牌」即可自动识别。"),
    },
    "traework": {
        "label": "TraeWork CN 桌面端",
        "exe_names": ("trae solo cn.exe", "traework cn.exe", "trae work.exe"),
        "proc_names": ("trae solo cn.exe", "traework cn.exe", "trae work.exe"),
        "name_re": re.compile(r"(traework\s*cn|trae\s*solo\s*cn|trae\s*work)", re.I),
        "name_exclude_re": re.compile(r"trae\s*code|traecn", re.I),
        "explicit": (
            r"%LOCALAPPDATA%\Programs\TRAE SOLO CN\TRAE SOLO CN.exe",
            r"%LOCALAPPDATA%\Programs\TraeWork CN\TraeWork CN.exe",
            r"%LOCALAPPDATA%\Programs\Trae Work\Trae Work.exe",
            r"%PROGRAMFILES%\TRAE SOLO CN\TRAE SOLO CN.exe",
        ),
        "drive_rel": (
            r"TRAE SOLO CN\TRAE SOLO CN.exe",
            r"TraeWork CN\TraeWork CN.exe",
            r"Trae Work\Trae Work.exe",
        ),
        # TraeWork 的签到走网页会话；这里给一个「用默认浏览器打开」的辅助入口
        "web": "https://www.trae.cn/",
        "guide": ("TraeWork 的签到使用**网页会话**，所以推荐直接点「登录」：\n"
                  "  会打开一个真实浏览器窗口，在窗口里登录 work.trae.cn 即可，\n"
                  "  登录态会被本软件保存下来，之后每天自动签到。\n"
                  "也可以先点「打开桌面端」在 TraeWork CN 里登录，再回来点「登录」。"),
    },
    "workbuddy": {
        "label": "WorkBuddy 桌面端",
        # ★ 白名单里**故意不含** workbuddyai.exe —— 那是另一个产品、另一个账号
        "exe_names": ("workbuddy.exe",),
        "proc_names": ("workbuddy.exe",),
        "name_re": re.compile(r"^workbuddy\b(?!\s*ai)", re.I),
        "name_exclude_re": re.compile(r"\bai\b|codebuddy", re.I),
        "explicit": (
            r"%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe",
            r"%PROGRAMFILES%\WorkBuddy\WorkBuddy.exe",
            r"%LOCALAPPDATA%\WorkBuddy\WorkBuddy.exe",
            r"%PROGRAMFILES%\Tencent\WorkBuddy\WorkBuddy.exe",
        ),
        "drive_rel": (
            r"WorkBuddy\WorkBuddy.exe",
            r"Tencent\WorkBuddy\WorkBuddy.exe",
        ),
        "web": "https://www.workbuddy.cn/",
        "guide": ("在 WorkBuddy 桌面端里登录要签到的账号。\n"
                  "登录完成后回到本软件，点「捕获当前账号」即可保存登录态。"),
    },
}

PLATFORMS = ("traework", "traecn", "workbuddy")

_VAR_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


def spec(platform):
    """取平台描述；未知平台返回 None。"""
    return SPECS.get(platform)


# ---------------------------------------------------------------- 路径展开

def _user_dir(kind):
    """取 ``roaming`` / ``local`` 用户目录，多路兜底。

    顺序：环境变量 → ``runtime.win_user_dirs`` → 从 ``USERPROFILE`` 拼 ``AppData``。

    ★ 为什么第三路必须**自带**：``runtime`` 是项目根下的模块，一旦它没在
      ``sys.path`` 上（打包路径变化、单独跑测试脚本等），`import runtime`
      会失败；如果兜底逻辑整个挂在它身上，就会**静默失效** ——
      于是路径里留下未展开的 ``%``、永远找不到客户端，而没有任何报错。
      这正是本项目反复出现的「假故障」形态，所以兜底不依赖任何可选依赖。
    """
    env = "APPDATA" if kind == "roaming" else "LOCALAPPDATA"
    sub = "Roaming" if kind == "roaming" else "Local"
    cands = []
    v = os.environ.get(env)
    if v:
        cands.append(v)
    try:
        from runtime import win_user_dirs
        cands.extend(win_user_dirs(kind) or [])
    except Exception:  # noqa: BLE001  runtime 不可用不该影响兜底
        pass
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if home and home != "~":
        cands.append(os.path.join(home, "AppData", sub))
    for c in cands:
        if c and "%" not in c:
            return c
    return None


@functools.lru_cache(maxsize=1)
def _env_map():
    """``%VAR%`` → 真实路径。

    ★ 为什么不能直接用 ``os.path.expandvars``：环境变量缺失时它会把
      ``%APPDATA%`` **原样留下**，于是拼出来的路径永远不存在 ——
      本项目最典型的「假故障」（界面 0 个账号、客户端「未找到」，却毫无报错）。
    """
    out = {}

    def put(key, value):
        if value:
            out[key] = value

    put("LOCALAPPDATA", _user_dir("local"))
    put("APPDATA", _user_dir("roaming"))
    put("USERPROFILE", os.environ.get("USERPROFILE")
        or os.path.expanduser("~"))
    put("SYSTEMDRIVE", os.environ.get("SystemDrive") or "C:")
    put("SYSTEMROOT", os.environ.get("SystemRoot") or r"C:\Windows")
    put("PROGRAMFILES", os.environ.get("ProgramFiles")
        or os.environ.get("PROGRAMFILES") or r"C:\Program Files")
    put("PROGRAMFILES(X86)", os.environ.get("ProgramFiles(x86)")
        or os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)")
    return out


def _expand(path):
    """展开 ``%VAR%``；**任何变量缺失都返回 None**（绝不返回带 % 的残留路径）。"""
    env = _env_map()

    def repl(m):
        val = env.get(m.group(1).upper())
        return val if val else "\x00"

    out = _VAR_RE.sub(repl, path)
    if "\x00" in out or "%" in out:
        return None
    return out


def _isfile(path):
    if not path:
        return False
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def _norm(path):
    return os.path.normcase(os.path.normpath(path)) if path else ""


def _exe_ok(platform, path):
    """最后一道闸：可执行文件名必须在白名单里。

    ★ 本机同时装着 ``WorkBuddy.exe``（签到目标）与 ``WorkBuddyAI.exe``
      （WorkBuddy AI，另一个账号）。没有这道闸就会把用户引导到错的软件。
    """
    sp = SPECS.get(platform) or {}
    if not path:
        return False
    base = os.path.basename(path).lower()
    return base in sp.get("exe_names", ())


# ---------------------------------------------------------------- 各层查找

def _fixed_drives():
    """本机固定磁盘根目录（C:\\、D:\\ …）。"""
    if not IS_WINDOWS:
        return []
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        out = []
        for i in range(26):
            if mask & (1 << i):
                root = "%s:\\" % chr(ord("A") + i)
                if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 3:  # DRIVE_FIXED
                    out.append(root)
        return out
    except Exception:  # noqa: BLE001
        return []


_UNINSTALL_KEYS = (
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM"),
    (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM32"),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU"),
)


def _hive(name):
    import winreg
    return {"HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKLM32": winreg.HKEY_LOCAL_MACHINE,
            "HKCU": winreg.HKEY_CURRENT_USER}[name]


def _reg_values(key):
    """读一个注册表键的全部值（失败返回 {}）。"""
    import winreg
    out = {}
    try:
        count = winreg.QueryInfoKey(key)[1]
    except OSError:
        return out
    for j in range(count):
        try:
            name, val, _ = winreg.EnumValue(key, j)
        except OSError:
            continue
        out[name] = val
    return out


def _icon_to_exe(raw):
    """``"D:\\App\\App.exe,0"`` → ``D:\\App\\App.exe``。"""
    if not raw:
        return None
    p = str(raw).strip().strip('"')
    if p.lower().endswith(",0"):
        p = p[:-2]
    p = p.strip().strip('"')
    return p or None


def _tier_app_paths(platform):
    """③ 注册表 App Paths —— 系统里登记「这个 exe 在哪」的权威位置。"""
    if not IS_WINDOWS:
        return []
    import winreg
    out = []
    roots = ((winreg.HKEY_LOCAL_MACHINE, "HKLM"),
             (winreg.HKEY_CURRENT_USER, "HKCU"))
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive, tag in roots:
        for exe in SPECS[platform]["exe_names"]:
            try:
                key = winreg.OpenKey(hive, sub + "\\" + exe)
            except OSError:
                continue
            try:
                val = winreg.QueryValueEx(key, "")[0]
            except OSError:
                val = None
            p = _icon_to_exe(val)
            if _isfile(p):
                out.append((p, "App Paths(%s)" % tag,
                            "注册表 App Paths 登记"))
    return out


def _tier_uninstall(platform):
    """④ 注册表 Uninstall 键。

    ★ ``InstallLocation`` 实测经常是 None（本机 WorkBuddy 就是），
      真正可靠的是 ``DisplayIcon``（形如 ``D:\\WorkBuddy\\WorkBuddy.exe,0``）。
    """
    if not IS_WINDOWS:
        return []
    import winreg
    sp = SPECS[platform]
    out = []
    for path, tag in _UNINSTALL_KEYS:
        try:
            base = winreg.OpenKey(_hive(tag), path)
        except OSError:
            continue
        try:
            n = winreg.QueryInfoKey(base)[0]
        except OSError:
            continue
        for i in range(n):
            try:
                sub = winreg.EnumKey(base, i)
                key = winreg.OpenKey(base, sub)
            except OSError:
                continue
            vals = _reg_values(key)
            name = str(vals.get("DisplayName") or "")
            if not name or not sp["name_re"].search(name):
                continue
            if sp["name_exclude_re"].search(name):
                continue
            cand = _icon_to_exe(vals.get("DisplayIcon"))
            if not _isfile(cand):
                loc = vals.get("InstallLocation")
                if loc and os.path.isdir(str(loc)):
                    for exe in sp["exe_names"]:
                        p = os.path.join(str(loc), os.path.basename(exe))
                        if _isfile(p):
                            cand = p
                            break
            if _isfile(cand):
                out.append((cand, "Uninstall(%s)" % tag,
                            "注册表卸载项「%s」" % name))
    return out


_PS_UTF8 = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "$OutputEncoding=[Text.Encoding]::UTF8;")

# ★ 目录由 Python 侧算好再传进去，**不依赖 PowerShell 自己的 $env:APPDATA**。
#   子进程的环境不保证与父进程一致（本项目在计划任务场景就踩过：
#   PowerShell 侧读不到变量 → 中文路径比对恒不相等 → 恒报「需检查」）。
_PS_SHORTCUTS = r"""
$ErrorActionPreference='SilentlyContinue'
$sh = New-Object -ComObject WScript.Shell
foreach ($d in @(__DIRS__)) {
  if (-not (Test-Path $d)) { continue }
  Get-ChildItem -Path $d -Recurse -Filter *.lnk -ErrorAction SilentlyContinue |
    ForEach-Object {
      $t = $sh.CreateShortcut($_.FullName).TargetPath
      if ($t) { Write-Output ("$($_.Name)`t$t") }
    }
}
"""


def _start_menu_dirs():
    """两个开始菜单根（用户级 / 全局级），只返回真实存在的。"""
    out = []
    roaming = _env_map().get("APPDATA")
    if roaming:
        out.append(os.path.join(roaming, "Microsoft", "Windows",
                                "Start Menu", "Programs"))
    pd = os.environ.get("ProgramData") or r"C:\ProgramData"
    out.append(os.path.join(pd, "Microsoft", "Windows", "Start Menu",
                            "Programs"))
    seen, uniq = set(), []
    for d in out:
        if os.path.isdir(d) and _norm(d) not in seen:
            seen.add(_norm(d))
            uniq.append(d)
    return uniq


@functools.lru_cache(maxsize=1)
def _start_menu_links():
    """⑤ 开始菜单快捷方式 → ``{lnk 名: 目标 exe}``。

    用 PowerShell 解析 ``.lnk``（纯 Python 解析要处理 LinkTargetIDList，
    太脆弱）。**带超时**，失败就当作「这一层没有结果」——不影响其余层可用。
    ★ 必须先把 PowerShell 的输出编码钉成 UTF-8，否则中文路径会乱码。
    """
    if not IS_WINDOWS:
        return {}
    dirs = _start_menu_dirs()
    if not dirs:
        return {}
    literal = ", ".join("'%s'" % d.replace("'", "''") for d in dirs)
    script = _PS_UTF8 + _PS_SHORTCUTS.replace("__DIRS__", literal)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=25,
            encoding="utf-8", errors="replace", creationflags=flags)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for line in (r.stdout or "").splitlines():
        if "\t" not in line:
            continue
        name, target = line.split("\t", 1)
        name, target = name.strip(), target.strip()
        if target and _isfile(target):
            out[name] = target
    return out


def _tier_start_menu(platform):
    sp = SPECS[platform]
    out = []
    for name, target in _start_menu_links().items():
        if not _exe_ok(platform, target):
            continue
        label = os.path.splitext(name)[0]
        if sp["name_re"].search(label) and not sp["name_exclude_re"].search(label):
            out.append((target, "开始菜单", "快捷方式「%s」" % label))
    return out


def _tier_path(platform):
    out = []
    for exe in SPECS[platform]["exe_names"]:
        p = shutil.which(exe)
        if _isfile(p):
            out.append((p, "PATH", "系统 PATH 中的 %s" % os.path.basename(p)))
    return out


# ---------------------------------------------------------------- 对外接口

def candidates(platform):
    """按查找顺序返回全部候选（含不存在的），供界面排障展示。

    每项：``{"path", "source", "detail", "exists"}``
    """
    sp = SPECS.get(platform)
    if not sp:
        return []
    items = []

    # ① 显式候选
    for raw in sp["explicit"]:
        p = _expand(raw)
        items.append({"path": p or raw, "source": "显式路径",
                      "detail": "预设安装位置", "exists": _isfile(p)})

    # ② 各磁盘根目录常见相对路径
    for drive in _fixed_drives():
        for rel in sp["drive_rel"]:
            p = os.path.join(drive, rel)
            items.append({"path": p, "source": "磁盘扫描",
                          "detail": "%s 下的常见安装位置" % drive,
                          "exists": _isfile(p)})

    # ③④ 注册表
    for p, source, detail in _tier_app_paths(platform) + _tier_uninstall(platform):
        items.append({"path": p, "source": source, "detail": detail,
                      "exists": True})

    # ⑤ 开始菜单
    for p, source, detail in _tier_start_menu(platform):
        items.append({"path": p, "source": source, "detail": detail,
                      "exists": True})

    # ⑥ PATH
    for p, source, detail in _tier_path(platform):
        items.append({"path": p, "source": source, "detail": detail,
                      "exists": True})

    # 去重（保序，先出现的层优先）
    seen, uniq = set(), []
    for it in items:
        key = _norm(it["path"])
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq


def find(platform):
    """定位客户端。返回 ``{"found", "path", "source", "detail", "tried"}``。

    ``found`` 为 True 时 ``path`` 一定通过可执行文件名白名单校验。
    """
    sp = SPECS.get(platform)
    if not sp:
        return {"platform": platform, "found": False, "path": None,
                "source": None, "detail": "未知平台：%s" % platform,
                "tried": []}
    cands = candidates(platform)
    tried = []
    for it in cands:
        if not it["exists"]:
            continue
        tried.append(it)
        if _exe_ok(platform, it["path"]):
            return {"platform": platform, "found": True, "path": it["path"],
                    "source": it["source"], "detail": it["detail"],
                    "tried": tried}
    # 找到了文件但都不在白名单里 —— 必须说清楚，否则用户以为「没装」
    bad = [it for it in tried if not _exe_ok(platform, it["path"])]
    detail = "未找到 %s 的安装位置" % sp["label"]
    if bad:
        detail += ("（发现 %d 个同名但非目标的程序，已忽略：%s）"
                   % (len(bad),
                      "、".join(os.path.basename(b["path"]) for b in bad[:3])))
    return {"platform": platform, "found": False, "path": None, "source": None,
            "detail": detail, "tried": tried}


def running_pids(platform):
    """当前正在运行的客户端进程 pid 列表（Toolhelp32，零进程开销）。"""
    names = set(SPECS.get(platform, {}).get("proc_names", ()))
    if not names or not IS_WINDOWS:
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        k32 = ctypes.windll.kernel32
        h = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if h == -1:
            return []
        out = []
        try:
            e = PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(h, ctypes.byref(e))
            while ok:
                if (e.szExeFile or "").lower() in names:
                    out.append(int(e.th32ProcessID))
                ok = k32.Process32NextW(h, ctypes.byref(e))
        finally:
            k32.CloseHandle(h)
        return out
    except Exception:  # noqa: BLE001
        return []


def is_running(platform):
    return bool(running_pids(platform))


def focus(platform):
    """尽力把客户端窗口切到前台。

    为什么需要：客户端常驻托盘时，「再启动一次」可能什么都不发生，
    用户看到的就是「点了按钮没反应」。这里直接把它的顶层窗口拉出来。
    纯 best-effort，失败不影响主流程。
    """
    pids = set(running_pids(platform))
    if not pids or not IS_WINDOWS:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and u32.IsWindowVisible(hwnd):
                if u32.GetWindowTextLengthW(hwnd) > 0:
                    found.append(hwnd)
            return True

        u32.EnumWindows(_cb, 0)
        if not found:
            return False
        hwnd = found[0]
        u32.ShowWindow(hwnd, 9)        # SW_RESTORE
        u32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001
        return False


def _spawn(path):
    """真正把进程拉起来。返回 ``(ok, err)``。

    单独抽出来是为了让回归测试能注入替身 —— **测试绝不能真的弹出用户的软件**。
    """
    try:
        # ★ 用 startfile（ShellExecute）：能正确处理「按用户安装」的
        #   提升/工作目录，比 Popen 更贴近用户双击的行为。
        os.startfile(path)  # noqa: S606
        return True, None
    except Exception:  # noqa: BLE001  startfile 不可用时退回 Popen
        try:
            subprocess.Popen([path], cwd=os.path.dirname(path), close_fds=True)
            return True, None
        except OSError as e:
            return False, str(e)


def launch(platform):
    """启动（或唤起）客户端。返回 ``(ok, detail, path)``。

    找不到安装位置时**不报错**，而是返回可执行的替代建议 ——
    因为「用户没装桌面端」是合法状态，不该当成故障。
    """
    sp = SPECS.get(platform)
    if not sp:
        return False, "未知平台：%s" % platform, None

    was_running = is_running(platform)
    info = find(platform)
    if not info["found"]:
        tip = "请手动打开 %s 并登录" % sp["label"]
        if sp.get("web"):
            tip += "，或点「打开网页版」用浏览器登录"
        return False, "%s。%s" % (info["detail"], tip), None

    path = info["path"]

    if was_running:
        # 已在运行：先试唤起窗口（托盘/被遮挡时最有用），再兜底重启一次
        # （Electron / VSCode 系都是单实例，再启动一次通常会聚焦已有窗口）。
        if focus(platform):
            return True, "已把 %s 切到前台，请在该窗口里登录" % sp["label"], path
        ok, _err = _spawn(path)
        if ok:
            return True, "%s 已在运行，请切到该窗口登录" % sp["label"], path
        return False, "%s 已在运行，但无法唤起窗口，请手动切到该窗口" % sp["label"], path

    ok, err = _spawn(path)
    if ok:
        return True, "已启动 %s，请在该窗口里登录" % sp["label"], path
    return False, "启动失败：%s" % err, path


def open_web(platform):
    """用默认浏览器打开该平台的网页版（辅助入口，非签到必需）。"""
    sp = SPECS.get(platform) or {}
    url = sp.get("web")
    if not url:
        return False, "该平台没有网页版入口"
    try:
        os.startfile(url)  # noqa: S606
        return True, "已用默认浏览器打开 %s" % url
    except Exception as e:  # noqa: BLE001
        return False, "打开失败：%s" % e


def guide_text(platform):
    """给用户看的登录引导文案。"""
    return (SPECS.get(platform) or {}).get("guide") or ""


def diagnose(platform):
    """一个平台的环境体检结果，供「环境自检」面板与排障使用。"""
    sp = SPECS.get(platform) or {}
    info = find(platform)
    pids = running_pids(platform)
    hints = []
    if not info["found"]:
        hints.append("未找到 %s：可先点「打开网页版」，或手动启动后回来重试"
                     % sp.get("label", platform))
    if info["found"] and not pids:
        hints.append("已定位到客户端但当前未运行，点「打开客户端」即可拉起")
    return {
        "platform": platform,
        "label": sp.get("label") or platform,
        "found": info["found"],
        "path": info["path"],
        "source": info["source"],
        "detail": info["detail"],
        "running": bool(pids),
        "pids": pids,
        "guide": guide_text(platform),
        "hints": hints,
    }
