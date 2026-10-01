#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设备标识重置：让服务端把本机识别为一台「全新设备」。

对齐参考项目 Settings 里的「6 层设备标识重置」与「重置设备码」。

6 层结构
--------
  ① machineid                     —— 客户端主设备号
  ② storage.json  telemetry.*     —— 遥测标识（machineId / devDeviceId / sqmId）
  ③ storage.json  aha.device      —— aha 设备指纹
  ④ TinyStorage / Local Storage   —— 本地存储里的设备痕迹
  ⑤ 注册表 MachineGuid            —— 系统级设备 GUID（可选，需管理员）
  ⑥ webview / Network 追踪数据    —— 会话与网络追踪目录

安全约定
--------
* 只操作用户数据目录，默认不动注册表（注册表项单独开关，且失败不影响其余层）。
* 每个被改动的文件都先生成带时间戳的备份，备份目录写明路径返回给 UI。
* 默认 dry-run 语义：先扫描出「将要改什么」，由调用方决定是否落地。
"""
import datetime
import glob
import json
import os
import shutil
import subprocess
import uuid

# Trae 系列各客户端的数据目录名（相对 %APPDATA% / %LOCALAPPDATA%）
APP_DIR_NAMES = ["Trae CN", "TRAE SOLO CN", "Trae", "Trae Work"]

# 需要清理追踪痕迹的子目录（相对每个客户端根）
TRACE_DIRS = [
    "Network",
    "Session Storage",
    "Local Storage",
    "WebStorage",
    "blob_storage",
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
]

# storage.json 里与设备标识相关的键
TELEMETRY_KEYS = ["telemetry.machineId", "telemetry.devDeviceId", "telemetry.sqmId"]


def _appdata():
    return os.environ.get("APPDATA") or os.path.expanduser("~")


def _localappdata():
    return os.environ.get("LOCALAPPDATA") or _appdata()


def client_roots():
    """返回本机存在的 Trae 系列客户端数据目录列表。"""
    roots = []
    for base in (_appdata(), _localappdata()):
        for name in APP_DIR_NAMES:
            p = os.path.join(base, name)
            if os.path.isdir(p):
                roots.append(p)
    # 去重（同一目录可能同时命中两个 base）
    seen, out = set(), []
    for p in roots:
        key = os.path.normcase(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _backup_root():
    return os.path.join(_appdata(), "TraeBuddyDeviceReset", "backups")


def _new_id():
    """生成新的设备标识（32 位十六进制，与 Electron 习惯一致）。"""
    return uuid.uuid4().hex


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _backup_file(path, backup_dir):
    """把文件复制到备份目录，返回备份路径（失败返回 None）。"""
    try:
        os.makedirs(backup_dir, exist_ok=True)
        dst = os.path.join(backup_dir, os.path.basename(path))
        i = 1
        while os.path.exists(dst):
            dst = os.path.join(backup_dir, f"{os.path.basename(path)}.{i}")
            i += 1
        shutil.copy2(path, dst)
        return dst
    except OSError:
        return None


def scan():
    """扫描本机可重置的设备标识层，返回逐层信息（不修改任何文件）。"""
    layers = []
    roots = client_roots()

    # ① machineid
    files = []
    for r in roots:
        p = os.path.join(r, "machineid")
        if os.path.exists(p):
            files.append(p)
    layers.append({
        "key": "machineid",
        "name": "machineid 客户端设备号",
        "found": files,
        "optional": False,
    })

    # ②③ storage.json（telemetry / aha.device）
    sj = []
    for r in roots:
        p = os.path.join(r, "User", "globalStorage", "storage.json")
        if os.path.exists(p):
            sj.append(p)
    layers.append({
        "key": "telemetry",
        "name": "storage.json telemetry 遥测标识",
        "found": sj,
        "optional": False,
    })
    layers.append({
        "key": "aha",
        "name": "storage.json aha.device 设备指纹",
        "found": sj,
        "optional": False,
    })

    # ④ TinyStorage / Local Storage
    tiny = []
    for r in roots:
        for pat in ("Local Storage", "SharedStorage", "blob_storage"):
            d = os.path.join(r, pat)
            if os.path.isdir(d):
                tiny.append(d)
    layers.append({
        "key": "tinystorage",
        "name": "TinyStorage / Local Storage 本地痕迹",
        "found": tiny,
        "optional": False,
    })

    # ⑤ 注册表 MachineGuid
    layers.append({
        "key": "machineguid",
        "name": "注册表 MachineGuid（系统级，需管理员）",
        "found": ["HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid"],
        "optional": True,
    })

    # ⑥ webview / 网络追踪
    traces = []
    for r in roots:
        for pat in TRACE_DIRS:
            d = os.path.join(r, pat)
            if os.path.isdir(d):
                traces.append(d)
    layers.append({
        "key": "webview",
        "name": "webview / Network 追踪数据",
        "found": traces,
        "optional": False,
    })

    return layers


def reset(levels=None, clear_credentials=False, rotate_machineguid=False,
          progress=None):
    """执行设备标识重置。

    参数
    ----
    levels             要处理的层 key 列表；None=全部
    clear_credentials  是否清除 storage.json 里的登录凭据键（强制登出）
    rotate_machineguid 是否同时轮换注册表 MachineGuid（需管理员）
    progress           进度回调 progress(str)

    返回 (ok, 日志行列表, 备份目录)
    """
    def say(msg):
        lines.append(msg)
        if progress:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001
                pass

    lines = []
    levels = set(levels or ["machineid", "telemetry", "aha",
                            "tinystorage", "machineguid", "webview"])
    roots = client_roots()
    if not roots:
        return False, ["未发现任何 Trae 系列客户端数据目录"], None

    bdir = os.path.join(_backup_root(), _stamp())
    say(f"发现 {len(roots)} 个客户端目录：")
    for r in roots:
        say(f"  · {r}")
    say(f"备份目录：{bdir}")

    # ① machineid
    if "machineid" in levels:
        say("\n① 轮换 machineid…")
        for r in roots:
            p = os.path.join(r, "machineid")
            if not os.path.exists(p):
                continue
            bak = _backup_file(p, bdir)
            try:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(_new_id())
                say(f"  ✓ {os.path.basename(r)}/machineid"
                    + (f"（备份 {os.path.basename(bak)}）" if bak else ""))
            except OSError as e:
                say(f"  ✗ {os.path.basename(r)}/machineid 失败：{e}")

    # ②③ storage.json
    if levels & {"telemetry", "aha"}:
        say("\n②③ 处理 storage.json（telemetry / aha.device）…")
        for r in roots:
            p = os.path.join(r, "User", "globalStorage", "storage.json")
            if not os.path.exists(p):
                continue
            bak = _backup_file(p, bdir)
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                say(f"  ✗ {os.path.basename(r)} storage.json 读取失败：{e}")
                continue

            changed = []
            if "telemetry" in levels:
                for k in TELEMETRY_KEYS:
                    if k in data:
                        data[k] = _new_id()
                        changed.append(k)
            if "aha" in levels:
                aha = data.get("aha.device")
                if isinstance(aha, dict):
                    for kk in list(aha.keys()):
                        if "id" in kk.lower() or "device" in kk.lower():
                            aha[kk] = _new_id()
                            changed.append(f"aha.device.{kk}")
                elif aha is not None:
                    data["aha.device"] = _new_id()
                    changed.append("aha.device")

            if clear_credentials:
                for kk in list(data.keys()):
                    low = kk.lower()
                    if any(t in low for t in ("token", "session", "auth",
                                              "credential", "account",
                                              "login", "refresh")):
                        data.pop(kk, None)
                        changed.append(f"-{kk}")

            if changed:
                try:
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    say(f"  ✓ {os.path.basename(r)} storage.json"
                        f"（{len(changed)} 项）"
                        + (f"（备份 {os.path.basename(bak)}）" if bak else ""))
                except OSError as e:
                    say(f"  ✗ {os.path.basename(r)} storage.json 写入失败：{e}")
            else:
                say(f"  · {os.path.basename(r)} storage.json 无可更新键")

    # ④ TinyStorage / Local Storage
    if "tinystorage" in levels:
        say("\n④ 清理 TinyStorage / Local Storage…")
        for r in roots:
            for pat in ("Local Storage", "SharedStorage", "blob_storage"):
                d = os.path.join(r, pat)
                if not os.path.isdir(d):
                    continue
                try:
                    _backup_dir(d, os.path.join(bdir, os.path.basename(r), pat))
                    shutil.rmtree(d, ignore_errors=True)
                    os.makedirs(d, exist_ok=True)
                    say(f"  ✓ {os.path.basename(r)}/{pat} 已清空")
                except OSError as e:
                    say(f"  ✗ {os.path.basename(r)}/{pat} 失败：{e}")

    # ⑤ 注册表 MachineGuid
    if "machineguid" in levels:
        say("\n⑤ 注册表 MachineGuid…")
        if not rotate_machineguid:
            say("  · 已跳过（未开启；该项需管理员权限）")
        else:
            ok, msg = _rotate_machineguid()
            say(("  ✓ " if ok else "  ✗ ") + msg)

    # ⑥ webview / Network
    if "webview" in levels:
        say("\n⑥ 清理 webview / Network 追踪数据…")
        for r in roots:
            for pat in TRACE_DIRS:
                d = os.path.join(r, pat)
                if not os.path.isdir(d):
                    continue
                try:
                    _backup_dir(d, os.path.join(bdir, os.path.basename(r), pat))
                    shutil.rmtree(d, ignore_errors=True)
                    os.makedirs(d, exist_ok=True)
                    say(f"  ✓ {os.path.basename(r)}/{pat} 已清空")
                except OSError as e:
                    say(f"  ✗ {os.path.basename(r)}/{pat} 失败：{e}")

    say("\n完成。请重新启动 Trae 客户端并重新登录账号。")
    return True, lines, bdir


def _backup_dir(src, dst):
    """目录级备份（失败静默，不阻断清理）。"""
    try:
        if os.path.exists(dst):
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.lock"))
    except (OSError, shutil.Error):
        pass


def _rotate_machineguid():
    """轮换 HKLM\\...\\Cryptography\\MachineGuid（需管理员）。"""
    key = r"HKLM\SOFTWARE\Microsoft\Cryptography"
    name = "MachineGuid"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        cur = subprocess.run(
            ["reg", "query", f"{key}", "/v", name],
            capture_output=True, text=True, timeout=15, creationflags=flags)
        if cur.returncode != 0:
            return False, f"读取失败：{(cur.stderr or cur.stdout).strip()[:120]}"
        new = str(uuid.uuid4())
        r = subprocess.run(
            ["reg", "add", key, "/v", name, "/t", "REG_SZ", "/d", new, "/f"],
            capture_output=True, text=True, timeout=15, creationflags=flags)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip()
            if "access" in err.lower() or "拒绝" in err:
                return False, "权限不足：请以管理员身份运行本程序后再试"
            return False, err[:140]
        return True, f"MachineGuid 已更新为新值（原值请从注册表备份恢复）"
    except Exception as e:  # noqa: BLE001
        return False, repr(e)


def find_backups():
    """列出历史备份目录（按时间倒序）。"""
    root = _backup_root()
    if not os.path.isdir(root):
        return []
    try:
        items = [os.path.join(root, d) for d in os.listdir(root)
                 if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return []
    items.sort(reverse=True)
    return items


def open_path(path):
    """在资源管理器中打开路径。"""
    try:
        os.startfile(path)  # noqa: S606  (Windows-only 项目)
        return True
    except Exception:  # noqa: BLE001
        try:
            subprocess.Popen(["explorer", os.path.normpath(path)])
            return True
        except Exception:  # noqa: BLE001
            return False
