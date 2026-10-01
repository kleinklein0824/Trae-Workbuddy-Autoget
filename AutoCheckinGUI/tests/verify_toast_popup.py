#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""右下角弹窗（签到结果卡片）真的会出现吗？—— 端到端回归。

为什么不能只断言「生成了 notify_card.png」
----------------------------------------
PNG 生成成功只说明无头浏览器渲染那一步通了。用户看到的是**一个窗口**：
它得真的可见、无边框、贴在屏幕右下角、过一会儿自己消失。中间任何一环断掉
（`toast.html` 没打进包、PIL 没打进包、透明色键不支持、坐标算到屏幕外、
`--toast` 分发没接上），PNG 都照样是好的，而用户什么也看不见。

所以这条测试直接枚举**目标进程的顶层窗口**，按几何与层属性断言：
  * 出现了可见顶层窗口，尺寸 ≈ 卡片宽 423
  * 无标题栏（`overrideredirect(True)` → 没有 WS_CAPTION）
  * 位置贴屏幕右下角（右边距 ≈16、下边距 ≈60）
  * 分层窗口的 alpha 真的被拉起来了（不是 alpha=0 的"隐形窗口"）
  * 同时 `logs/notify_card.png` 存在且含透明像素

覆盖两条真实路径（`checkin._spawn_toast` 会二选一）：
  ① 脚本模式：`python toast_gui.py <payload>`
  ② 冻结模式：`TraeBuddy签到助手.exe --toast <payload>`（计划任务跑 exe 时走这条）

用法：python tests/verify_toast_popup.py
"""
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
sys.path.insert(0, GUI_DIR)

import app.config  # noqa: E402  定位项目根并挂上 sys.path
import checkin  # noqa: E402
import win_tree_probe as WTP  # noqa: E402

# ⚠️ 弹窗的脚本与模板都在**项目根**（checkin.ROOT），不在 GUI 目录里。
# 写死 `dirname(HERE)` 会得到 GUI 目录，命令里就变成一个不存在的
# toast_gui.py，子进程秒退 —— 表现成"弹窗没出现"，其实路径就错了。
ROOT = checkin.ROOT

u = ctypes.windll.user32
GWL_STYLE = -16
WS_CAPTION = 0x00C00000

# 卡片渲染宽度（与 toast_gui.CARD_W 一致）
CARD_W = 423
MARGIN_R = 16
MARGIN_B = 60

ok = 0
fail = 0
skip = 0

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {label}")
    else:
        fail += 1
        print(f"  FAIL {label}  {extra}")


def note(label):
    global skip
    skip += 1
    print(f"  SKIP {label}")


# ---------------- 与进程无关的窗口属性读取 ----------------

def snapshot_file(p):
    """文件指纹 (mtime_ns, size)；不存在返回 None。"""
    try:
        st = os.stat(p)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def file_rewritten(p, before):
    """`p` 是否在 `before` 之后被重写过。

    ⚠️ 为什么用指纹而不是"先删掉再看有没有生成"：本项目跑在带安全删除
    守卫的沙箱里，`os.remove` 累计超过阈值会抛
    `SAFE_DELETE_BULK_CONFIRM_REQUIRED` 并**直接终止进程**（退出码 1、
    没有 traceback）。加上 stdout 重定向到文件时是块缓冲，尾部输出会一起
    丢掉 —— 表现成"测试跑到一半神秘崩溃"。测试里一律不要删文件。
    """
    now = snapshot_file(p)
    return now is not None and now != before


def win_style(hwnd):
    return u.GetWindowLongW(hwnd, GWL_STYLE)


def win_alpha(hwnd):
    """分层窗口 alpha（0~255）；不是分层窗口返回 255。"""
    key = wintypes.DWORD()
    alpha = wintypes.BYTE()
    flags = wintypes.DWORD()
    try:
        r = u.GetLayeredWindowAttributes(hwnd, ctypes.byref(key),
                                         ctypes.byref(alpha),
                                         ctypes.byref(flags))
        if not r:
            return 255
        if not (flags.value & 0x00000002):      # LWA_ALPHA 没开 → 完全不透明
            return 255
        return alpha.value
    except Exception:  # noqa: BLE001
        return 255


def probe_new_window(wait=60.0, exclude=(), want_w=CARD_W, want_pids=None):
    """全屏扫一个**新出现**的卡片窗口，返回 (row, max_alpha)。

    为什么不能只盯"我们自己起的那个进程"
    ------------------------------------
    签到进程跑完一轮后，是用**另一个进程**把卡片弹出来的，自己随即退出。
    只按父 PID（或进程树）找窗口会一无所获：父进程没了，子进程被挂到
    别的父进程下，进程树断了。所以这里全屏枚举、按窗口特征认人：
    宽度≈423、无标题栏、贴右下角 —— 这三个特征凑齐的窗口不可能是别的程序。

    `exclude` 是开跑前就已经存在的 hwnd 集合（差分掉，避免误认旧窗口）。
    `want_pids` 若给定则只认这些 pid 的窗口（脚本模式同进程时更严格）。
    """
    t0 = time.time()
    best_alpha = 0
    best_row = None
    while time.time() - t0 < wait:
        time.sleep(0.3)
        rows = WTP.windows(want_pids)
        cands = [r for r in rows
                 if r["visible"] and r["hwnd"] not in exclude
                 and abs(r["w"] - want_w) <= 6 and r["h"] > 80]
        if cands:
            r = max(cands, key=lambda x: x["h"])
            best_row = r
            best_alpha = max(best_alpha, win_alpha(r["hwnd"]))
            if best_alpha >= 200:
                return r, best_alpha
    # 找到过但 alpha 一直没拉起来 → 也把那个窗口返回，方便调用方报出细节
    return best_row, best_alpha


def probe_window(proc, wait=45.0, want_w=CARD_W):
    """轮询 `proc` 及其子孙进程的顶层窗口，返回第一个符合"卡片"特征的。

    返回 (row, max_alpha) 或 (None, 0)。
    """
    t0 = time.time()
    best_alpha = 0
    while time.time() - t0 < wait:
        if proc.poll() is not None:
            break
        time.sleep(0.3)
        rows = WTP.windows(set(WTP.tree(proc.pid)))
        cands = [r for r in rows
                 if r["visible"] and abs(r["w"] - want_w) <= 6 and r["h"] > 80]
        if not cands:
            continue
        r = max(cands, key=lambda x: x["h"])
        best_alpha = max(best_alpha, win_alpha(r["hwnd"]))
        # 等淡入完成（alpha 拉起来）再返回，避免读到 alpha=0 的中间态
        if best_alpha >= 200:
            return r, best_alpha
        best_alpha = max(best_alpha, win_alpha(r["hwnd"]))
    rows = WTP.windows(set(WTP.tree(proc.pid)))
    cands = [r for r in rows
             if r["visible"] and abs(r["w"] - want_w) <= 6 and r["h"] > 80]
    if cands:
        r = max(cands, key=lambda x: x["h"])
        return r, max(best_alpha, win_alpha(r["hwnd"]))
    return None, best_alpha


def kill(proc):
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    except Exception:  # noqa: BLE001
        pass


def assert_popup(tag, cmd, want_rows):
    """起进程 → 断言右下角真的出现卡片窗口。"""
    print(f"\n-- {tag} --")
    print("   cmd =", " ".join(cmd[:2]) + (" ..." if len(cmd) > 2 else ""))
    png = os.path.join(checkin.LOG_DIR, "notify_card.png")
    err = os.path.join(checkin.LOG_DIR, "toast_err.log")
    # 只取指纹，**不删文件**（见 file_rewritten 的说明）
    png_before, err_before = snapshot_file(png), snapshot_file(err)

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, creationflags=flags,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        row, alpha = probe_window(proc)
        check(f"{tag}：出现了可见顶层窗口", row is not None,
              "未在 45s 内发现卡片窗口（渲染失败 / 分发没接上？）")
        if row is None:
            # 只报**本轮**新产生的错误，别把几个月前的老日志当成现场证据
            if file_rewritten(err, err_before):
                print("       toast_err.log（本轮新增）：")
                for ln in open(err, encoding="utf-8", errors="ignore"
                               ).read().strip().splitlines()[-4:]:
                    print("        ", ln)
            else:
                print(f"       子进程退出码 = {proc.poll()}，且本轮没有新错误")
            return
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        r = wintypes.RECT()
        u.GetWindowRect(row["hwnd"], ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        gap_r, gap_b = sw - r.right, sh - r.bottom

        check(f"{tag}：宽度 = 卡片宽 {CARD_W}（实测 {w}）", abs(w - CARD_W) <= 6)
        check(f"{tag}：高度符合行数（{want_rows} 行 → {250 + 30 * want_rows}，"
              f"实测 {h}）", abs(h - (250 + 30 * want_rows)) <= 30)
        check(f"{tag}：无标题栏（overrideredirect）",
              not (win_style(row["hwnd"]) & WS_CAPTION),
              f"style=0x{win_style(row['hwnd']):08x}")
        check(f"{tag}：贴屏幕右下角（右边距 {gap_r}≈{MARGIN_R}）",
              abs(gap_r - MARGIN_R) <= 8, f"gap_r={gap_r}")
        check(f"{tag}：贴屏幕底部（下边距 {gap_b}≈{MARGIN_B}）",
              abs(gap_b - MARGIN_B) <= 12, f"gap_b={gap_b}")
        check(f"{tag}：窗口真的不透明（alpha={alpha}）", alpha >= 200)

        check(f"{tag}：同时生成了 notify_card.png",
              os.path.exists(png) and os.path.getsize(png) > 1000
              and file_rewritten(png, png_before),
              f"{os.path.getsize(png) if os.path.exists(png) else 'missing'} B"
              f"（本轮{'已重写' if file_rewritten(png, png_before) else '未变'}）")
        if os.path.exists(png):
            try:
                from PIL import Image
                im = Image.open(png).convert("RGBA")
                a = im.getchannel("A")
                check(f"{tag}：PNG 含透明像素（抠底成功）",
                      a.getextrema()[0] == 0 and a.getextrema()[1] == 255,
                      f"alpha 范围 {a.getextrema()}")
            except Exception as e:  # noqa: BLE001
                check(f"{tag}：PNG 可解析", False, repr(e))
    finally:
        kill(proc)
        time.sleep(0.5)


def main():
    # 本进程也声明 DPI 感知：否则 GetSystemMetrics / GetWindowRect 拿到的是
    # 被虚拟化过的逻辑坐标（125% 下 1920x1080 会读成 1536x864，423 宽读成 338），
    # 右下角边距和卡片宽度断言都会莫名其妙地差一大截。
    WTP.set_dpi_aware()

    print("== 0. 前置：渲染引擎与模板 ==")
    browser = next((b for b in BROWSERS if os.path.exists(b)), None)
    check("本机有无头渲染用的浏览器（Edge/Chrome）", browser is not None)
    if browser is None:
        print("     没有浏览器 → 卡片渲染必然失败，后续跳过")
    tpl = os.path.join(ROOT, "toast.html")
    check("toast.html 模板存在", os.path.exists(tpl),
          f"{os.path.getsize(tpl) if os.path.exists(tpl) else 'missing'} B")
    try:
        import PIL  # noqa: F401
        has_pil = True
    except Exception:  # noqa: BLE001
        has_pil = False
    check("Pillow 可用（抠透明要用）", has_pil)
    check("toast_notify 默认开启（配置缺键时按 True 处理）",
          checkin.load_config().get("options", {}).get("toast_notify", True))
    if browser is None or not has_pil:
        print("\n环境缺依赖，无法端到端验证弹窗")
        print(f"结果：{ok} OK / {fail} FAIL / {skip} SKIP")
        return 1 if fail else 0

    # ---------------- 1. 捕获 _spawn_toast 真正会执行的命令 ----------------
    print()
    print("== 1. 弹窗负载与命令（捕获 checkin._spawn_toast 的真实入参）==")
    results = [
        dict(platform="traework", account="Klein", status="ok",
             detail="签到成功，领取 100 积分，已连续签到 14 天"),
        dict(platform="traecn", account="Klein", status="skip",
             detail="今日已完成"),
        dict(platform="workbuddy", account="KK", status="ok",
             detail="签到成功，领取 100 积分，已连续签到 1 天"),
        dict(platform="workbuddy", account="Klein", status="fail",
             detail="网络异常：连接超时（read timeout=30）"),
    ]
    cap = {}
    real_spawn = checkin._spawn_toast

    def fake_spawn(payload):
        cap["payload"] = payload

    checkin._spawn_toast = fake_spawn
    try:
        checkin._notify_summary(results)
    finally:
        checkin._spawn_toast = real_spawn

    check("_notify_summary 确实触发了弹窗", "payload" in cap)
    if "payload" not in cap:
        print(f"结果：{ok} OK / {fail} FAIL / {skip} SKIP")
        return 1
    pl = json.loads(cap["payload"])
    print("   title =", pl["title"])
    for r in pl["rows"]:
        print("   ", r["state"], r["tag"], r["name"], "|", r["note"])
    check("标题含成功/失败计数", "3/4" in pl["title"] or "失败" in pl["title"],
          pl["title"])
    n_fail = sum(1 for r in results if r["status"] not in ("ok", "skip"))
    check(f"行数 = 账号数 {len(results)} + 失败详情 {min(2, n_fail)}",
          len(pl["rows"]) == len(results) + min(2, n_fail), str(len(pl["rows"])))
    check("成功行带积分", any("+100" in r["note"] for r in pl["rows"]))
    check("失败行带原因", any("网络" in r["note"] for r in pl["rows"]))
    check("时间戳带执行方式", "执行" in pl["time"], pl["time"])
    check("已转义 '<'（防注入）", "<" not in cap["payload"])

    # ---------------- 2. 脚本模式：真的弹出来了吗 ----------------
    print()
    print("== 2. 脚本模式：python toast_gui.py <payload> ==")
    cmd = [sys.executable, os.path.join(ROOT, "toast_gui.py"), cap["payload"]]
    assert_popup("脚本模式", cmd, len(pl["rows"]))

    # ---------------- 3. 冻结模式：exe --toast（计划任务走这条） ----------------
    print()
    print("== 3. 冻结模式：TraeBuddy签到助手.exe --toast <payload> ==")
    exe = os.path.join(GUI_DIR, "dist", "TraeBuddy签到助手",
                       "TraeBuddy签到助手.exe")
    if not os.path.exists(exe):
        note(f"便携 exe 不存在（{exe}），跳过冻结模式验证")
    else:
        assert_popup("冻结模式", [exe, "--toast", cap["payload"]],
                     len(pl["rows"]))

    print()
    print("== 4. 自动化轮次结束会不会弹窗（stub 平台签到，不碰真实日志）==")
    # 这一节只验「跑完一轮 → 有没有把弹窗叫起来」这条接线。
    # 平台签到、写盘、写日志全部 stub 掉：既让测试确定、也避免污染
    # 真实 state.json / history.jsonl / 签到记录.csv。
    import datetime
    import tempfile

    import tc_checkin
    import tw_checkin
    import wb_checkin

    spawned = []
    saved = dict(spawn=checkin._spawn_toast, log=checkin.log,
                 save_state=checkin.save_state, append=checkin._append_records,
                 history=checkin.HISTORY_PATH, gate=checkin._gate_skip,
                 silent=checkin.SILENT, load_cfg=checkin.load_config,
                 retry_state=checkin.RETRY_STATE_PATH)
    orig_run = {m: getattr(m, "run") for m in (tw_checkin, tc_checkin,
                                               wb_checkin)}
    try:
        checkin._spawn_toast = spawned.append
        checkin.log = lambda *a, **k: None
        checkin.save_state = lambda s: None
        checkin._append_records = lambda r: None
        checkin.HISTORY_PATH = os.path.join(tempfile.gettempdir(),
                                            "_tb_toast_test_history.jsonl")
        # ★ 补跑轮次记账也必须重定向。它是 2026-09-18 新增的写盘点，
        #   漏掉的话这一节会一边往**真实** logs/retry_state.json 里加轮次，
        #   一边让下面的断言依赖"今天这台机器已经跑过几次"——
        #   实测就是这么炸的：断言读到「自动补跑 第4次」而失败。
        checkin.RETRY_STATE_PATH = os.path.join(tempfile.gettempdir(),
                                                "_tb_toast_test_retry.json")

        def mk(pid):
            def _run(log, state, cfg, today, force):
                return [dict(ts=datetime.datetime.now().isoformat(
                    timespec="seconds"), platform=pid, account="Klein",
                    status="ok",
                    detail="签到成功，领取 100 积分，已连续签到 3 天")]
            return _run

        tw_checkin.run = mk("traework")
        tc_checkin.run = mk("traecn")
        wb_checkin.run = mk("workbuddy")

        # 4.1 正常的自动轮次（计划任务用的就是 --silent）→ 必须弹窗
        # 先把补跑轮次计数**归零**（临时文件跨次运行会留着旧值），
        # 否则"第 2 轮"这条断言会随历史漂移。
        checkin.save_retry_state({
            "date": datetime.date.today().isoformat(), "attempts": 0,
            "notified_first_fail": False, "notified_last": False})
        spawned.clear()
        checkin.SILENT = True
        checkin.run_all(force=True)
        check("自动轮次（--silent）跑完会弹窗", len(spawned) == 1,
              f"{len(spawned)} 次")
        if spawned:
            p = json.loads(spawned[0])
            # 判据：必须标明"这是自动跑的"，且**绝不能**说成手动。
            # 不写死"自动执行"四个字 —— 补跑轮次的文案是「自动补跑 第N次」，
            # 那也是自动执行，写死会把正常行为判成失败。
            check("自动轮次弹窗标注为自动（而非手动）",
                  "自动" in p["time"] and "手动" not in p["time"], p["time"])
            check("自动轮次弹窗列出 3 个账号", len(p["rows"]) == 3,
                  str(len(p["rows"])))

        # 4.1b ★ 同一台机器当天的第 2 轮自动执行 → 文案必须变成补跑轮次
        #      （证明"弹窗与补跑同步"这条真的接在 run_all 上了）
        spawned.clear()
        checkin.SILENT = True
        checkin.run_all(force=True)
        if spawned:
            p = json.loads(spawned[0])
            check("第 2 轮自动执行标注为「自动补跑 第2次」",
                  "自动补跑" in p["time"] and "第2次" in p["time"], p["time"])
            check("补跑轮次标题为「补跑完成」（不是含糊的『全部成功』）",
                  "补跑完成" in p["title"], p["title"])
        else:
            check("第 2 轮自动执行也会弹窗（全成功属于关键节点）", False,
                  "没弹")

        # 4.2 被门控的轮次：静默不弹（否则每天会被重复打扰好几次）
        checkin._gate_skip = lambda cfg, state, today, now=None: \
            "今日全部账号已完成（日期记录比对）"
        spawned.clear()
        checkin.SILENT = True
        checkin.run_all()
        check("被门控的静默轮次不弹窗（不重复打扰）", len(spawned) == 0,
              f"{len(spawned)} 次")
        spawned.clear()
        checkin.SILENT = False
        checkin.run_all()
        check("被门控的手动轮次弹窗（点了要有反馈）", len(spawned) == 1,
              f"{len(spawned)} 次")
        if spawned:
            p = json.loads(spawned[0])
            check("门控弹窗标题说明今日已完成", "已完成" in p["title"], p["title"])
            check("门控弹窗标注「手动执行」", "手动执行" in p["time"], p["time"])
            check("门控弹窗列出了每个账号", len(p["rows"]) >= 1,
                  str(len(p["rows"])))

        # 4.3 用户把「弹出通知卡片」开关关掉 → 不再弹
        checkin._gate_skip = saved["gate"]
        checkin.load_config = lambda: dict(saved["load_cfg"](),
                                           options={"toast_notify": False})
        spawned.clear()
        checkin.SILENT = True
        checkin.run_all(force=True)
        check("关掉「弹出通知卡片」开关后不再弹窗", len(spawned) == 0,
              f"{len(spawned)} 次")
    finally:
        checkin._spawn_toast = saved["spawn"]
        checkin.log = saved["log"]
        checkin.save_state = saved["save_state"]
        checkin._append_records = saved["append"]
        checkin.HISTORY_PATH = saved["history"]
        checkin._gate_skip = saved["gate"]
        checkin.SILENT = saved["silent"]
        checkin.load_config = saved["load_cfg"]
        checkin.RETRY_STATE_PATH = saved["retry_state"]
        for m, fn in orig_run.items():
            m.run = fn
        # 临时历史文件故意不删：沙箱的安全删除守卫会把进程直接杀掉
        # （见 file_rewritten），而它本来就在 %TEMP% 里，留着无害。

    print()
    print(f"结果：{ok} OK / {fail} FAIL / {skip} SKIP")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
