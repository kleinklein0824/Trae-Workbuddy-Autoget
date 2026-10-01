# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：Trae & Buddy 签到助手。

产出两份：
  * dist/TraeBuddy签到助手/        —— 目录版（推荐，启动快、体积可控）
  * dist/TraeBuddy签到助手.exe     —— 单文件版（便于分发，但每次启动要解包）

打包要点
--------
1. 把项目根目录的签到模块（checkin/tw_checkin/tc_checkin/wb_checkin/
   net_util/toast_gui/runtime）作为 hiddenimport 打进来，因为 exe 模式下
   它们不在同一目录，PyInstaller 静态分析看不到。
2. `requests` 装在 user site-packages（%APPDATA%\\Python\\...），
   pathex 里要显式加上，否则打包时找不到。
3. playwright 只做「运行时检测」：浏览器内核（Chromium）体积巨大且需
   单独安装，不打进包；缺内核时 GUI 会给出明确提示。
4. 数据文件（config.json / logs / profiles / tokens_*.json）**不**打进包，
   运行时在 exe 同级目录读写，用户可直接备份与迁移。
"""
import os
import sys

# ---- 路径解析 ----
# SPECPATH 在部分 PyInstaller 版本下指向 spec 所在目录，部分版本下
# 会被解析成调用时的 CWD，因此这里以「本 spec 文件自身位置」为准最稳。
def _spec_dir():
    import os as _os
    sp = globals().get("SPECPATH")
    if sp and _os.path.exists(_os.path.join(sp, "build_exe.spec")):
        return _os.path.abspath(sp)
    if sp and _os.path.isdir(sp):
        # SPECPATH 指到了 desktop/ 等上层目录时，按文件名回找
        cand = _os.path.join(sp, "AutoCheckinGUI")
        if _os.path.exists(_os.path.join(cand, "build_exe.spec")):
            return cand
    # 兜底：相对 CWD 推断
    for base in (_os.getcwd(), _os.path.join(_os.getcwd(), "desktop", "AutoCheckinGUI")):
        if _os.path.exists(_os.path.join(base, "build_exe.spec")):
            return base
    return _os.path.abspath(sp or _os.getcwd())


SPEC_DIR = _spec_dir()                              # 桌面版/AutoCheckinGUI
GUI_DIR = SPEC_DIR
# 桌面版把签到核心与 GUI 分开放在同一个版本目录中。
# 不再依赖项目根的散落副本，避免整理目录后构建链路找错入口。
CORE_DIR = os.path.abspath(os.path.join(GUI_DIR, "..", "核心脚本"))
PROJ_ROOT = CORE_DIR

APP_NAME = "TraeBuddy签到助手"
ICON = os.path.join(SPEC_DIR, "assets", "app.ico")
VERSION_FILE = os.path.join(SPEC_DIR, "assets", "version_info.txt")
_version = VERSION_FILE if os.path.exists(VERSION_FILE) else None

# user site-packages（requests 所在）
_USER_SP = os.path.join(
    os.environ.get("APPDATA", ""), "Python", "Python313", "site-packages")

pathex = [GUI_DIR, PROJ_ROOT]
if os.path.isdir(_USER_SP):
    pathex.append(_USER_SP)

# 项目根的签到模块（exe 里以模块方式 import）
CORE_MODULES = [
    "checkin",
    "tw_checkin",
    "tc_checkin",
    "wb_checkin",
    "net_util",
    "toast_gui",
    "runtime",
]

hiddenimports = list(CORE_MODULES) + [
    "app", "app.application", "app.config", "app.theme",
    "app.ui", "app.ui.titlebar", "app.ui.sidebar", "app.ui.topbar",
    "app.ui.widgets", "app.ui.toast", "app.ui.resources",
    "app.ui.author_card", "app.ui.setup_guide",
    "app.pages", "app.pages.base", "app.pages.dashboard",
    "app.pages.checkin", "app.pages.accounts", "app.pages.credits",
    "app.pages.logs", "app.pages.settings", "app.pages.api_service",
    "app.services", "app.services.runner", "app.services.credits",
    "app.services.scheduler", "app.services.api_server",
    "app.services.local_backends", "app.services.solo_upstream",
    "app.services.ai_dispatch", "app.services.diagnose",
    "app.services.device_reset", "app.services.tray",
    "app.services.client_launcher", "app.services.account_bundle",
    "app.services.envcheck", "app.services.credential_refresh",
    "requests", "urllib3", "charset_normalizer", "idna",
    "certifi", "playwright",
]

# 数据文件：把签到脚本以 .py 形式也放进包内，便于被 import 与自检
datas = []
for m in CORE_MODULES:
    src = os.path.join(PROJ_ROOT, f"{m}.py")
    if os.path.exists(src):
        datas.append((src, "."))
# notify.ps1 是原生 Toast 兜底
_notify = os.path.join(PROJ_ROOT, "notify.ps1")
if os.path.exists(_notify):
    datas.append((_notify, "."))
# toast.html 是「液态金属琉璃」结果卡片的模板。
# 【必须打进去】漏了它 exe 模式下卡片渲染会直接失败，用户看不到任何弹窗 ——
# toast_gui.py 会从 bundle_dir() 找这个文件。
_toast_html = os.path.join(PROJ_ROOT, "toast.html")
if os.path.exists(_toast_html):
    datas.append((_toast_html, "."))

# 收集 app 包内的静态资源（目前只有 py，占位以便后续加图标/字体）
for root, _dirs, files in os.walk(os.path.join(GUI_DIR, "app")):
    for fn in files:
        if fn.endswith((".png", ".ico", ".ttf", ".ttc", ".json")):
            full = os.path.join(root, fn)
            rel = os.path.relpath(root, GUI_DIR)
            datas.append((full, rel))

# ★★ assets/ 下的资源**必须**打进去（2026-09-22 修图标；2026-10-01 扩到全部）
#   上面那轮 `os.walk` 只扫 `app/`，而资源实际在 **GUI_DIR/assets/** ——
#   于是 exe 里 `ui/resources.py` 的 `app_icon_ico()` / `app_icon_png()`
#   全部返回 None：
#     · 窗口 / 任务栏图标靠 `titlebar.set_window_icon()` 用 WM_SETICON 下发，
#       拿不到 .ico → **空白图标**；
#     · 系统托盘图标同样没有图标（`_build_tray` 传的是 `app_icon_ico()`）；
#     · Tk 的 `iconphoto`（Alt-Tab 兜底）也失效。
#   源码运行时按源码目录能找到，所以这个缺陷**只在 exe 上显形**。
#   现在改为**把 assets/ 下所有资源整目录打进包**（含奖励作者收款码
#   reward_qr.png / reward_qr_full.png），避免以后新增资源又漏打。
#   注意目标目录要写成 `assets`：`resources._candidates()` 找的是
#   `<base>/assets/<name>`（onedir 的 base 是 `_internal/`）。
_ASSETS_DIR = os.path.join(SPEC_DIR, "assets")
if os.path.isdir(_ASSETS_DIR):
    for _fn in sorted(os.listdir(_ASSETS_DIR)):
        if _fn.lower().endswith((".png", ".ico", ".ttf", ".ttc", ".json")):
            datas.append((os.path.join(_ASSETS_DIR, _fn), "assets"))

excludes = [
    "matplotlib", "numpy", "pandas", "scipy", "PIL.ImageQt",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "pytest", "setuptools", "pip", "wheel",
    "notebook", "IPython", "jupyter",
]

block_cipher = None

a = Analysis(
    [os.path.join(GUI_DIR, "exe_launcher.py")],
    pathex=pathex,
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_icon = ICON if os.path.exists(ICON) else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
    version=_version,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

# ---- 单文件版 ----
exe_onefile = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=f"{APP_NAME}-单文件",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    icon=_icon,
    version=_version,
)
