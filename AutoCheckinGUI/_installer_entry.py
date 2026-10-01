#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自解压安装程序入口（由 make_installer.py 生成，请勿手工修改）。"""
import os
import shutil
import subprocess
import sys
import tkinter as tk
import zipfile
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Trae & Buddy 签到助手"
APP_SHORT = "TraeBuddy"
VERSION = "2.6.8"
EXE_NAME = "TraeBuddy签到助手.exe"
TASK_NAME = "AutoDailyCheckin"


def _payload_dir():
    """payload.zip 所在目录（PyInstaller 解包目录或脚本同级）。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return base


def _default_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Programs", APP_SHORT)


def _desktop():
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _start_menu():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs")


def _make_shortcut(link_path, target, workdir, icon):
    """用 PowerShell 建 .lnk 快捷方式（不依赖 pywin32）。"""
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{link_path}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.WorkingDirectory = '{workdir}'; "
        f"$s.IconLocation = '{icon}'; "
        "$s.Save()"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-Command", ps],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="ignore", creationflags=flags)


def _write_uninstaller(target_dir):
    """在安装目录放一份 uninstall.exe（就是本程序自身副本）。"""
    try:
        me = sys.executable
        dst = os.path.join(target_dir, "uninstall.exe")
        if os.path.abspath(me) != os.path.abspath(dst):
            shutil.copy2(me, dst)
        return dst
    except OSError:
        return None


def do_install(target, desktop_icon, auto_start):
    os.makedirs(target, exist_ok=True)
    payload = os.path.join(_payload_dir(), "payload.zip")
    if not os.path.exists(payload):
        raise FileNotFoundError("安装包损坏：缺少 payload.zip")

    with zipfile.ZipFile(payload) as z:
        z.extractall(target)

    exe = os.path.join(target, EXE_NAME)
    icon = os.path.join(target, "app.ico")
    icon_arg = icon if os.path.exists(icon) else exe

    # 开始菜单
    sm = os.path.join(_start_menu(), APP_NAME)
    os.makedirs(sm, exist_ok=True)
    _make_shortcut(os.path.join(sm, f"{APP_NAME}.lnk"), exe, target, icon_arg)
    _make_shortcut(os.path.join(sm, f"卸载 {APP_NAME}.lnk"),
                   os.path.join(target, "uninstall.exe"), target, icon_arg)

    # 桌面
    if desktop_icon:
        _make_shortcut(os.path.join(_desktop(), f"{APP_NAME}.lnk"),
                       exe, target, icon_arg)

    # 开机自启
    if auto_start:
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, APP_SHORT, 0, winreg.REG_SZ, f'"{exe}"')
            winreg.CloseKey(key)
        except OSError:
            pass

    _write_uninstaller(target)

    # 写注册表卸载项（控制面板可见）
    try:
        import winreg
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                               rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_SHORT}")
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, target)
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, icon_arg)
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                          f'"{os.path.join(target, "uninstall.exe")}" --uninstall')
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, "极泊Poles")
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(key)
    except OSError:
        pass

    return exe


def do_uninstall(target):
    """卸载：删快捷方式、注册表项、计划任务与程序文件。

    用户数据（logs / config.json / profiles / tokens_*.json）默认保留，
    避免误删签到记录；这里显式跳过它们。
    """
    keep = {"logs", "config.json", "profiles", "tokens_workbuddy.json"}
    try:
        for name in os.listdir(target):
            if name.lower() in keep:
                continue
            p = os.path.join(target, name)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass

    # 快捷方式
    for base, names in ((_desktop(), [f"{APP_NAME}.lnk"]),
                        (os.path.join(_start_menu(), APP_NAME),
                         [f"{APP_NAME}.lnk", f"卸载 {APP_NAME}.lnk"])):
        for n in names:
            try:
                os.remove(os.path.join(base, n))
            except OSError:
                pass
    try:
        os.rmdir(os.path.join(_start_menu(), APP_NAME))
    except OSError:
        pass

    # 注册表
    try:
        import winreg
        winreg.DeleteKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_SHORT}")
    except OSError:
        pass
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, APP_SHORT)
        winreg.CloseKey(key)
    except OSError:
        pass

    # 计划任务
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="ignore", creationflags=flags)


class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"安装 {APP_NAME} {VERSION}")
        self.geometry("560x460")
        self.configure(bg="#f1f5f9")
        self.resizable(False, False)
        icon = os.path.join(_payload_dir(), "app.ico")
        if os.path.exists(icon):
            try:
                self.iconbitmap(icon)
            except tk.TclError:
                pass

        tk.Label(self, text="欢迎使用", font=("Microsoft YaHei UI", 16, "bold"),
                 bg="#f1f5f9", fg="#1e293b").pack(anchor="w", padx=24, pady=(20, 0))
        tk.Label(self, text=f"{APP_NAME}  {VERSION}\n"
                            "多平台自动签到工具（TraeWork / TraeCN / WorkBuddy）",
                 font=("Microsoft YaHei UI", 10), bg="#f1f5f9", fg="#64748b",
                 justify="left").pack(anchor="w", padx=24, pady=(4, 14))

        box = tk.Frame(self, bg="#ffffff", highlightthickness=1,
                       highlightbackground="#e2e8f0")
        box.pack(fill="x", padx=24)
        tk.Label(box, text="安装位置", font=("Microsoft YaHei UI", 9, "bold"),
                 bg="#ffffff", fg="#1e293b").pack(anchor="w", padx=12, pady=(10, 4))
        row = tk.Frame(box, bg="#ffffff")
        row.pack(fill="x", padx=12, pady=(0, 12))
        self.path_var = tk.StringVar(value=_default_dir())
        tk.Entry(row, textvariable=self.path_var,
                 font=("Microsoft YaHei UI", 9), bg="#ffffff", fg="#1e293b",
                 relief="flat", highlightthickness=1,
                 highlightbackground="#cbd5e1").pack(
            side="left", fill="x", expand=True, ipady=4)

        def browse():
            d = filedialog.askdirectory(initialdir=self.path_var.get())
            if d:
                self.path_var.set(d)
        tk.Button(row, text="浏览…", command=browse,
                  font=("Microsoft YaHei UI", 9), relief="flat",
                  bg="#f1f5f9", fg="#475569", padx=10,
                  activebackground="#e2e8f0").pack(side="left", padx=(6, 0))

        self.desktop_var = tk.BooleanVar(value=True)
        self.start_var = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="  创建桌面快捷方式", variable=self.desktop_var,
                       font=("Microsoft YaHei UI", 9), bg="#f1f5f9",
                       fg="#475569", activebackground="#f1f5f9",
                       selectcolor="#ffffff", anchor="w").pack(
            anchor="w", padx=24, pady=(14, 0))
        tk.Checkbutton(self, text="  开机自动启动", variable=self.start_var,
                       font=("Microsoft YaHei UI", 9), bg="#f1f5f9",
                       fg="#475569", activebackground="#f1f5f9",
                       selectcolor="#ffffff", anchor="w").pack(anchor="w", padx=24)

        # ★ 安装后的「数据在哪 / 账号怎么带过来 / 自动签到怎么注册」必须写清楚
        #   （2026-09-22 补）：安装版的数据目录是**安装目录**（`runtime.data_root()`
        #   跟着 exe 走），与免安装版 / 源码版各自独立。用户装完看到「0 个账号」
        #   会以为「功能丢了」，其实只是数据没搬过来 —— 而「账号迁移」就是干这个的。
        #   计划任务同理：不注册就没有每日自动签到，注册入口在设置页。
        tk.Label(self,
                 text="提示：账号 / 配置 / 签到记录都存在「安装目录」下，与免安装版、"
                      "源码版各自独立；\n之前用过本工具的话，安装后用「系统设置 → "
                      "账号迁移 → 导入」把账号带过来；\n每日自动签到用「系统设置 → "
                      "Windows 计划任务 → 安装 / 更新任务」注册（指向安装目录，与源码无关）。",
                 font=("Microsoft YaHei UI", 8), bg="#f1f5f9", fg="#94a3b8",
                 justify="left", anchor="w").pack(anchor="w", padx=24, pady=(10, 0))

        self.status = tk.Label(self, text="", font=("Microsoft YaHei UI", 9),
                               bg="#f1f5f9", fg="#64748b", anchor="w")
        self.status.pack(fill="x", padx=24, pady=(10, 0))

        self.bar = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.bar.pack(fill="x", padx=24, pady=(6, 0))
        self.bar.pack_forget()

        btns = tk.Frame(self, bg="#f1f5f9")
        btns.pack(fill="x", padx=24, pady=(16, 18))
        self.go = tk.Button(btns, text="开始安装", command=self.install,
                            font=("Microsoft YaHei UI", 10, "bold"),
                            bg="#18181b", fg="#ffffff", relief="flat",
                            padx=20, pady=7, activebackground="#27272a",
                            activeforeground="#ffffff")
        self.go.pack(side="right")
        tk.Button(btns, text="取消", command=self.destroy,
                  font=("Microsoft YaHei UI", 10), bg="#ffffff", fg="#475569",
                  relief="flat", padx=16, pady=7,
                  activebackground="#f1f5f9").pack(side="right", padx=(0, 8))

    def install(self):
        target = self.path_var.get().strip()
        if not target:
            messagebox.showwarning("提示", "请先选择安装位置")
            return
        self.go.configure(state="disabled", text="安装中…")
        self.bar.pack(fill="x", padx=24, pady=(6, 0))
        self.bar["value"] = 20
        self.status.configure(text="正在释放文件…")
        self.update()

        try:
            exe = do_install(target, self.desktop_var.get(), self.start_var.get())
        except Exception as e:
            messagebox.showerror("安装失败", f"{e!r}")
            self.go.configure(state="normal", text="开始安装")
            self.bar.pack_forget()
            return

        self.bar["value"] = 100
        self.status.configure(text="安装完成")
        self.update()
        if messagebox.askyesno("安装完成",
                               f"{APP_NAME} 已安装到：\n{target}\n\n是否立即启动？"):
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.Popen([exe], cwd=target, creationflags=flags)
            except OSError:
                pass
        self.destroy()


def main():
    if "--uninstall" in sys.argv:
        target = os.path.dirname(os.path.abspath(sys.executable))
        root = tk.Tk()
        root.withdraw()
        if not messagebox.askyesno(
                "卸载确认",
                f"确定要卸载 {APP_NAME} 吗？\n\n"
                "签到记录、配置与账号文件（logs / config.json / profiles）"
                "将会保留，不会被删除。\n"
                "如需彻底清理，请在卸载后手动删除该目录。"):
            return 0
        try:
            do_uninstall(target)
            # 自身文件在运行中无法删除，交给系统延迟处理
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["cmd", "/c", "timeout", "/t", "2", ">nul", "&",
                 "rmdir", "/s", "/q", target],
                creationflags=flags)
            messagebox.showinfo("卸载完成",
                                "已卸载。用户数据保留在原安装目录。")
        except Exception as e:
            messagebox.showerror("卸载失败", f"{e!r}")
        return 0

    Wizard().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
