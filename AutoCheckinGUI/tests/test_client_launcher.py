#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端启动器回归测试（``app/services/client_launcher.py``）。

背景
----
用户要求：账号管理板块「扫码 / 捕获 / 手动添加」都要能**自动跳转对应软件
引导用户登录**，并且要**换设备 / 新装 exe 也能用**。

三个平台是三套登录机制（TraeCN 读 IDE 日志令牌、TraeWork 走 Playwright 网页
会话、WorkBuddy 读桌面端登录态文件），所以「程序内扫码」不可能实现 ——
能做到的是「找到并拉起对应客户端 + 引导 + 自动捕获」。本测试锁住这段能力。

本测试**绝不真的启动任何软件**：``launch()`` 的进程创建被抽成 ``_spawn()``，
这里注入替身来断言调用与否。

★ 设计原则：断言要**跨机器成立**。
  本机把 Trae CN 装在 ``D:\\Trae CN``、WorkBuddy 装在 ``D:\\WorkBuddy``，
  但别人的机器不是。所以：
    · 「具体路径」类断言 → 只在探测到该客户端时才断言（条件断言）
    · 「不变式」类断言 → 无条件断言（任何一层返回的 exe 都必须在白名单里）
    · 负对照 → 无条件断言（WorkBuddyAI.exe 必须永远被拒）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
            else os.path.dirname(os.path.dirname(GUI_DIR)))
for _p in (PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.services import client_launcher as CL   # noqa: E402

OK = FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {extra}")


# ============================================================ 1. 平台描述
print("== 1. 平台描述完整性 ==")
check("PLATFORMS 覆盖三个平台",
      set(CL.PLATFORMS) == {"traework", "traecn", "workbuddy"},
      CL.PLATFORMS)
for _p in CL.PLATFORMS:
    _sp = CL.spec(_p)
    check(f"{_p}: spec 存在", bool(_sp))
    check(f"{_p}: 有 exe_names 白名单", bool(_sp.get("exe_names")))
    check(f"{_p}: 有 label", bool(_sp.get("label")))
    check(f"{_p}: 有登录引导文案", bool(CL.guide_text(_p)))
    check(f"{_p}: 有显式候选路径", bool(_sp.get("explicit")))
check("未知平台 spec 返回 None", CL.spec("nope") is None)
check("未知平台 find 不抛异常且 found=False",
      CL.find("nope")["found"] is False)

# ============================================================ 2. 白名单闸门
print()
print("== 2. 可执行文件名白名单（★最关键的一道闸）==")
# 负对照：这些都必须被拒
_NEG = [
    ("workbuddy", r"D:\WorkBuddyAI\WorkBuddyAI.exe",
     "WorkBuddy AI 是另一个产品、另一个账号，绝不能拉错"),
    ("workbuddy", r"D:\WorkBuddy\Uninstall WorkBuddy.exe", "卸载程序"),
    ("workbuddy", r"D:\WorkBuddy\WorkBuddy.exe.bak", "备份文件"),
    ("traecn", r"C:\Users\someone\AppData\Local\Programs\TRAE SOLO CN\TRAE SOLO CN.exe",
     "TraeWork 桌面端不能被当成 Trae CN IDE"),
    ("traework", r"D:\Trae CN\Trae CN.exe", "Trae CN IDE 不能被当成 TraeWork"),
    ("traecn", r"", "空路径"),
    ("traecn", None, "None"),
]
for _plat, _path, _why in _NEG:
    check(f"【负对照】{_plat} 拒绝 {os.path.basename(_path or '') or '空值'}（{_why}）",
          CL._exe_ok(_plat, _path) is False, _path)

# 正对照：这些必须通过
_POS = [
    ("workbuddy", r"D:\WorkBuddy\WorkBuddy.exe"),
    ("traecn", r"D:\Trae CN\Trae CN.exe"),
    ("traecn", r"C:\Program Files\Trae CN\Trae CN.exe"),
    ("traework",
     r"C:\Users\someone\AppData\Local\Programs\TRAE SOLO CN\TRAE SOLO CN.exe"),
]
for _plat, _path in _POS:
    check(f"【正对照】{_plat} 接受 {os.path.basename(_path)}",
          CL._exe_ok(_plat, _path) is True, _path)

# 白名单大小写不敏感（Windows 路径大小写不保证）
check("白名单大小写不敏感",
      CL._exe_ok("workbuddy", r"D:\WorkBuddy\WORKBUDDY.EXE") is True)

# ★ 跨产品串扰：三个平台的 exe 名集合必须两两不相交，
#   否则「拉对软件」这件事就无法保证。
_names = {p: set(CL.spec(p)["exe_names"]) for p in CL.PLATFORMS}
for _a, _b in (("traecn", "traework"), ("traecn", "workbuddy"),
               ("traework", "workbuddy")):
    check(f"{_a} 与 {_b} 的 exe 名不重叠",
          not (_names[_a] & _names[_b]), _names[_a] & _names[_b])

# ============================================================ 3. 路径展开
print()
print("== 3. 路径展开与用户目录兜底 ==")
check("_expand 展开 %LOCALAPPDATA% 无残留 %",
      "%" not in (CL._expand(r"%LOCALAPPDATA%\a\b.exe") or "%"),
      CL._expand(r"%LOCALAPPDATA%\a\b.exe"))
check("_expand 展开 %APPDATA% 无残留 %",
      "%" not in (CL._expand(r"%APPDATA%\a\b.exe") or "%"),
      CL._expand(r"%APPDATA%\a\b.exe"))
# 负对照：旧写法 expandvars 在变量缺失时会留下 %
_old = os.path.expandvars(r"%__NO_SUCH_VAR__%\a.exe")
check("【负对照】os.path.expandvars 会留下未解析的 %（说明为何不能用它）",
      "%" in _old, _old)
check("【正对照】_expand 对未知变量返回 None（而不是带 % 的假路径）",
      CL._expand(r"%__NO_SUCH_VAR__%\a.exe") is None,
      CL._expand(r"%__NO_SUCH_VAR__%\a.exe"))

# 环境变量被抹掉时仍要能解析出真实目录（计划任务 / 精简环境下会发生）
_saved_env = {k: os.environ.get(k) for k in ("APPDATA", "LOCALAPPDATA")}
try:
    for _k in _saved_env:
        os.environ.pop(_k, None)
    CL._env_map.cache_clear()
    _ld = CL._user_dir("local")
    _rd = CL._user_dir("roaming")
    check("【正对照】APPDATA/LOCALAPPDATA 缺失时 local 仍可解析",
          bool(_ld) and "%" not in _ld, _ld)
    check("【正对照】APPDATA/LOCALAPPDATA 缺失时 roaming 仍可解析",
          bool(_rd) and "%" not in _rd, _rd)
    check("兜底目录确实存在",
          os.path.isdir(_ld or "") and os.path.isdir(_rd or ""),
          f"{_ld} / {_rd}")
    check("兜底到 ~/AppData 下",
          (_ld or "").lower().endswith(os.path.join("appdata", "local")),
          _ld)
    # 兜底之后显式候选路径也要能展开（不能是 None）
    _exp = CL._expand(r"%LOCALAPPDATA%\Programs\X\X.exe")
    check("【正对照】缺失环境下显式候选路径仍能展开",
          bool(_exp) and "%" not in _exp, _exp)
finally:
    for _k, _v in _saved_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v
    CL._env_map.cache_clear()

# ============================================================ 4. 查找各层
print()
print("== 4. 六层查找（先便宜后昂贵）==")
check("_fixed_drives 至少返回一个盘符", len(CL._fixed_drives()) >= 1,
      CL._fixed_drives())

_layers = {
    "① 显式路径": lambda p: CL.candidates(p),
    "② 磁盘扫描": lambda p: CL.candidates(p),
    "③ App Paths": lambda p: CL._tier_app_paths(p),
    "④ 注册表卸载项": lambda p: CL._tier_uninstall(p),
    "⑤ 开始菜单": lambda p: CL._tier_start_menu(p),
    "⑥ PATH": lambda p: CL._tier_path(p),
}


def _as_items(res, source_filter=None):
    """把各层返回值统一成 ``[(path, exists), ...]``。"""
    out = []
    for it in res:
        if isinstance(it, tuple):
            out.append((it[0], True))
        elif source_filter is None or it.get("source") == source_filter:
            out.append((it["path"], it["exists"]))
    return out


for _name, _fn in _layers.items():
    _sf = {"① 显式路径": "显式路径", "② 磁盘扫描": "磁盘扫描"}.get(_name)
    for _p in CL.PLATFORMS:
        try:
            _res = _fn(_p)
            _ok = isinstance(_res, list)
        except Exception as e:                      # noqa: BLE001
            _res, _ok = repr(e), False
        check(f"{_name} / {_p} 可调用且返回列表", _ok, _res)
        # ★ 不变式（跨机器成立）：任何一层返回的**已存在**路径都必须在白名单里
        if _ok:
            for _path, _exists in _as_items(_res, _sf):
                if _exists:
                    check(f"{_name} / {_p} 返回的 {os.path.basename(_path)} "
                          f"在白名单内", CL._exe_ok(_p, _path), _path)

# 注册表层：本机实测能靠 DisplayIcon 命中（InstallLocation 为 None）
_reg_hits = sum(len(CL._tier_uninstall(p)) for p in CL.PLATFORMS)
print(f"   （信息）注册表 Uninstall 层命中 {_reg_hits} 条")
check("注册表层返回项都带非空说明",
      all(d for p in CL.PLATFORMS
          for _x, _s, d in CL._tier_uninstall(p)),
      [CL._tier_uninstall(p) for p in CL.PLATFORMS])

# ============================================================ 5. find 不变式
print()
print("== 5. find() 结构与不变式 ==")
for _p in CL.PLATFORMS:
    _info = CL.find(_p)
    for _k in ("platform", "found", "path", "source", "detail", "tried"):
        check(f"{_p}: find() 含键 {_k}", _k in _info)
    check(f"{_p}: tried 是列表", isinstance(_info["tried"], list))
    check(f"{_p}: detail 非空", bool(_info["detail"]), _info["detail"])
    if _info["found"]:
        check(f"{_p}: 找到的路径是真实文件",
              os.path.isfile(_info["path"]), _info["path"])
        check(f"{_p}: 找到的路径在白名单内",
              CL._exe_ok(_p, _info["path"]), _info["path"])
        check(f"{_p}: 给出了来源层", bool(_info["source"]), _info["source"])
        # ★ 不变式：find() 必须返回「第一个已存在且合法」的候选
        _first = next((c["path"] for c in CL.candidates(_p)
                       if c["exists"] and CL._exe_ok(_p, c["path"])), None)
        check(f"{_p}: find() == 首个合法候选（层序未被破坏）",
              os.path.normcase(_info["path"]) == os.path.normcase(_first or ""),
              f"\n       find ={_info['path']}\n       first={_first}")
    else:
        check(f"{_p}: 未找到时 path 为 None", _info["path"] is None)

# 候选去重
for _p in CL.PLATFORMS:
    _keys = [os.path.normcase(c["path"]) for c in CL.candidates(_p)]
    check(f"{_p}: candidates 无重复路径", len(_keys) == len(set(_keys)))

# ============================================================ 6. launch 注入
print()
print("== 6. launch() 行为（注入替身，绝不真启动软件）==")
_spawn_calls = []
_fake_path = r"D:\Fake\WorkBuddy.exe"
_orig = (CL.find, CL.is_running, CL.focus, CL._spawn)
try:
    # 6a 未找到 → 不启动，返回可执行建议
    CL.find = lambda p: {"platform": p, "found": False, "path": None,
                         "source": None, "detail": "未找到安装位置",
                         "tried": []}
    CL.is_running = lambda p: False
    CL._spawn = lambda path: (_spawn_calls.append(path), (True, None))[1]
    _ok, _msg, _path = CL.launch("workbuddy")
    check("【未找到】返回 False", _ok is False)
    check("【未找到】不尝试启动进程", not _spawn_calls, _spawn_calls)
    check("【未找到】文案给出替代做法（手动打开 / 网页版）",
          "手动打开" in _msg and "网页版" in _msg, _msg)
    check("【未找到】path 为 None", _path is None)

    # 6b 找到且未运行 → 启动
    _spawn_calls.clear()
    CL.find = lambda p: {"platform": p, "found": True, "path": _fake_path,
                         "source": "测试注入", "detail": "注入",
                         "tried": []}
    _ok, _msg, _path = CL.launch("workbuddy")
    check("【已找到·未运行】返回 True", _ok is True)
    check("【已找到·未运行】确实调用了启动", _spawn_calls == [_fake_path],
          _spawn_calls)
    check("【已找到·未运行】文案提示「在该窗口里登录」",
          "在该窗口里登录" in _msg, _msg)
    check("【已找到·未运行】回传路径", _path == _fake_path)

    # 6c 找到且已在运行、唤起成功 → 不重复启动
    _spawn_calls.clear()
    CL.is_running = lambda p: True
    CL.focus = lambda p: True
    _ok, _msg, _path = CL.launch("workbuddy")
    check("【已运行·唤起成功】返回 True", _ok is True)
    check("【已运行·唤起成功】不重复启动进程", not _spawn_calls, _spawn_calls)
    check("【已运行·唤起成功】文案说明已切到前台",
          "切到前台" in _msg, _msg)

    # 6d 已运行但唤起失败 → 兜底重启一次
    _spawn_calls.clear()
    CL.focus = lambda p: False
    _ok, _msg, _path = CL.launch("workbuddy")
    check("【已运行·唤起失败】兜底启动一次", _spawn_calls == [_fake_path],
          _spawn_calls)
    check("【已运行·唤起失败】文案提示切到该窗口", "切到该窗口" in _msg, _msg)

    # 6e 启动本身失败 → 必须报失败，不能假装成功
    _spawn_calls.clear()
    CL.is_running = lambda p: False
    CL._spawn = lambda path: (False, "模拟失败")
    _ok, _msg, _path = CL.launch("workbuddy")
    check("【启动失败】返回 False（不谎报成功）", _ok is False, _msg)
    check("【启动失败】文案含原因", "模拟失败" in _msg, _msg)

    # 6f 未知平台
    check("【未知平台】launch 返回 False", CL.launch("nope")[0] is False)
finally:
    CL.find, CL.is_running, CL.focus, CL._spawn = _orig

# ============================================================ 7. 诊断
print()
print("== 7. diagnose() 环境体检 ==")
for _p in CL.PLATFORMS:
    _d = CL.diagnose(_p)
    for _k in ("platform", "label", "found", "path", "source", "detail",
               "running", "pids", "guide", "hints"):
        check(f"{_p}: diagnose 含键 {_k}", _k in _d)
    check(f"{_p}: running 是布尔", isinstance(_d["running"], bool))
    check(f"{_p}: pids 是列表", isinstance(_d["pids"], list))
    check(f"{_p}: 有引导文案", bool(_d["guide"]))
    check(f"{_p}: 未运行时给出「可拉起」提示",
          _d["found"] is False or _d["running"] or bool(_d["hints"]),
          _d["hints"])
check("未知平台 diagnose 不抛异常", isinstance(CL.diagnose("nope"), dict))

# ============================================================ 8. 只读保证
print()
print("== 8. 本模块必须只读（不写任何文件、不碰凭据）==")
_src = open(os.path.join(GUI_DIR, "app", "services", "client_launcher.py"),
            encoding="utf-8").read()
for _bad, _why in (("shutil.rmtree", "不得删除目录"),
                   ("os.remove", "不得删除文件"),
                   ("open(", "不得打开/写入文件")):
    if _bad == "open(":
        # 允许 open 出现在注释里，但代码里不应有写模式
        check(f"源码不含写文件模式（{_why}）",
              '"w"' not in _src and "'w'" not in _src and '"wb"' not in _src,
              "发现写模式")
    else:
        check(f"源码不含 {_bad}（{_why}）", _bad not in _src)

print()
print(f"结果：{OK} OK / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
