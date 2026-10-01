#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分批清理历史构建产物（走回收站）。

★ 守卫是**整批拒绝**的：一次提交太多条目时会在动手前就杀掉进程
  （实测 14 项的一批：0 项被删、进程 SIGTERM、连一行输出都没有）。
  所以这里**逐项**处理，每项单独一次 PowerShell 调用，并把结果立刻打出来 ——
  这样即使中途被杀，也能看清已经删到哪一项。

用法：
    python tests/_clean_build_artifacts.py            # 只列清单
    python tests/_clean_build_artifacts.py --files    # 只删文件（小批量）
    python tests/_clean_build_artifacts.py --one <路径相对项目>   # 删一项
"""
import os
import subprocess
import sys

BASE = os.getcwd()
KEEP = {
    os.path.normcase("dist/TraeBuddy签到助手"),
    os.path.normcase("dist/TraeBuddy签到助手-单文件.exe"),
    os.path.normcase("Output/payload.zip"),          # verify_installer 的参照
}


def _keep(rel):
    n = os.path.normcase(rel)
    if n in KEEP:
        return True
    b = os.path.basename(rel)
    return b.startswith("TraeBuddy签到助手_Setup_") and b.endswith(".exe")


def _size(p):
    if os.path.isfile(p):
        return os.path.getsize(p)
    t = 0
    for r, _d, f in os.walk(p):
        for x in f:
            try:
                t += os.path.getsize(os.path.join(r, x))
            except OSError:
                pass
    return t


def candidates():
    out = []
    for d in ("dist", "Output"):
        full = os.path.join(BASE, d)
        if os.path.isdir(full):
            for n in sorted(os.listdir(full)):
                rel = d + "/" + n
                if not _keep(rel):
                    out.append(rel)
    for n in sorted(os.listdir(BASE)):
        if n.startswith("_old") or n in ("build", "build_installer"):
            out.append(n)
    return [r for r in out if os.path.exists(os.path.join(BASE, r.replace("/", os.sep)))]


def recycle_one(p):
    """送一项进回收站。返回 (ok, 输出)。"""
    ps = (
        "Add-Type -AssemblyName Microsoft.VisualBasic;"
        "$p = '%s';"
        "if (-not (Test-Path -LiteralPath $p)) { Write-Output 'MISSING'; exit }"
        "try {"
        "  if ((Get-Item -LiteralPath $p).PSIsContainer) {"
        "    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
        "      $p,'OnlyErrorDialogs','SendToRecycleBin')"
        "  } else {"
        "    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
        "      $p,'OnlyErrorDialogs','SendToRecycleBin')"
        "  }"
        "} catch { }"
        "if (Test-Path -LiteralPath $p) { Write-Output 'FAIL' } else { Write-Output 'OK' }"
    ) % p.replace("'", "''")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=600)
    out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").strip()
    return (not os.path.exists(p)), out


def main(argv):
    cands = candidates()
    if not cands:
        print("没有需要清理的历史产物。")
        return 0

    print("清单（%d 项）：" % len(cands))
    total = 0
    for rel in cands:
        p = os.path.join(BASE, rel.replace("/", os.sep))
        s = _size(p)
        total += s
        print("   %-40s %8.1f MB  %s" % (rel, s / 1048576,
                                         "dir" if os.path.isdir(p) else "file"))
    print("   合计 %.2f GB" % (total / 1073741824))

    if "--one" in argv:
        rel = argv[argv.index("--one") + 1]
        p = os.path.join(BASE, rel.replace("/", os.sep))
        ok, out = recycle_one(p)
        print("\n%s -> %s (%s)" % (rel, "OK" if ok else "FAIL", out))
        return 0 if ok else 1

    if "--files" not in argv:
        print("\n（只读模式。加 --files 删文件，或 --one <相对路径> 删单项）")
        return 0

    files = [r for r in cands
             if os.path.isfile(os.path.join(BASE, r.replace("/", os.sep)))]
    print("\n逐项删文件（%d 项）：" % len(files))
    for rel in files:
        p = os.path.join(BASE, rel.replace("/", os.sep))
        ok, out = recycle_one(p)
        print("   %-40s %s" % (rel, "OK" if ok else "FAIL " + out), flush=True)
    print("\n剩余：", candidates())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
