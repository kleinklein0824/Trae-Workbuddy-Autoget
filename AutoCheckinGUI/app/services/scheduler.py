#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计划任务与网络状态服务：封装 Windows 计划任务的注册/查询/移除。

直接复用 checkin.py 里既有的 TaskName 与注册逻辑，保证 GUI 与命令行
操作的是同一个计划任务，不会出现两套任务互相覆盖的问题。

性能约定（非常重要）
--------------------
`task_info()` 底层要起一个 PowerShell 进程，单次约 **1.1 秒**。
它在页面渲染路径上被多处调用（顶栏徽标、新手引导、任务卡片），
一次切页可能触发 7 次，累计 8 秒以上 —— 这正是「点击很卡」的根因。

因此这里做两件事：
  1. **进程内缓存**（默认 5 秒 TTL），同一帧里的重复调用直接命中缓存；
  2. **提供后台刷新接口** `refresh_task_info_async()`，
     页面先用缓存渲染（立即出图），拿到新结果后再回调更新。

调用方凡是在渲染路径上读任务状态，都应走 `task_info()`（读缓存）
而不是直接 `_powershell`。
"""
import datetime
import json
import os
import subprocess
import sys
import threading
import time

from .. import config as C

# ---- task_info 缓存 ----
_CACHE_TTL = 5.0                  # 秒；缓存有效期
_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()
_refreshing = False
# 「刷新进行中又来了一次刷新请求」时置位；当前轮结束后立刻补跑一轮。
# 没有这个标记会丢帧：保存签到时间后触发的刷新若撞上正在进行的刷新，
# 会被直接丢弃，界面继续显示旧时间 —— 用户看到的现象就是「保存不了」。
_pending = False


# ⚠️ 必须给每条 PowerShell 命令加这个前缀，见 _powershell 的说明。
_PS_UTF8 = ("try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {};"
            "$OutputEncoding = [Text.Encoding]::UTF8;")


def _powershell(script, timeout=90):
    """执行一段 PowerShell，返回 (stdout, stderr) 文本。

    ⚠️ 编码陷阱（真实踩过）：中文 Windows 上 PowerShell 默认按**控制台代码页
    （GBK / cp936）**写 stdout，而这里原来固定 `encoding="utf-8"`。
    后果是**所有非 ASCII 字符被静默错解**——注意它连异常都不会抛：
    `签到助手` 的 GBK 字节是 `\\xc7\\xa9\\xb5\\xbd\\xd6\\xfa\\xca\\xd6`，其中
    `\\xc7\\xa9` 恰好是**合法的 UTF-8 双字节序列**（U+01E9 = `ǩ`），
    剩下的字节被 `errors="ignore"` 悄悄丢掉。于是

        ...\\dist\\TraeBuddy签到助手\\TraeBuddy签到助手.exe
        变成
        ...\\dist\\TraeBuddyǩ\\TraeBuddyǩ.exe

    两个真实故障由此而来：
      1. 设置页「Windows 计划任务」卡片把任务路径显示成乱码，用户看不懂；
      2. `task_target_mismatch()` 拿乱码串去比对 → **永远不相等**，
         于是恒报「▲ 需检查」，把正常状态说成有问题。

    正解：在脚本开头把输出编码钉成 UTF-8（实测有效），再按 utf-8 解码。
    不要改成"先试 utf-8 失败再退 gbk"——上面那个 `\\xc7\\xa9` 的例子说明
    GBK 字节可以伪装成合法 UTF-8，靠 try/except 根本发现不了。
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", _PS_UTF8 + script],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="ignore", creationflags=flags)
        return (r.stdout or ""), (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return "", repr(e)


def _query_task_info():
    """真正去查一次计划任务（慢路径，约 1s）。"""
    out, err = _powershell(
        f"$t = Get-ScheduledTask -TaskName '{C.TASK_NAME}' -ErrorAction SilentlyContinue;"
        "if ($t) {"
        "  $i = $t | Get-ScheduledTaskInfo;"
        "  $trig = ($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ',';"
        "  $at = ($t.Triggers | Where-Object { $_.StartBoundary } | ForEach-Object {"
        "     ($_.StartBoundary -split 'T')[-1].Substring(0,5) }) -join ',';"
        # 任务到底在跑哪个程序 —— 指向别的副本时会「设置不生效」，必须能看见
        "  $a = $t.Actions | Select-Object -First 1;"
        # 过期补跑（Repetition）—— 用户最关心的「过了时间会不会自动重签」
        "  $dt = @($t.Triggers | Where-Object { $_.CimClass.CimClassName -like '*DailyTrigger*' });"
        "  $iv = ''; $du = '';"
        "  if ($dt.Count -gt 0 -and $dt[0].Repetition) {"
        "    $iv = [string]$dt[0].Repetition.Interval; $du = [string]$dt[0].Repetition.Duration };"
        "  [pscustomobject]@{"
        "    installed=$true; state=[string]$t.State;"
        "    lastRun=[string]$i.LastRunTime; result=[string]$i.LastTaskResult;"
        "    nextRun=[string]$i.NextRunTime; triggers=$trig; times=$at;"
        "    exe=[string]$a.Execute; args=[string]$a.Arguments; workdir=[string]$a.WorkingDirectory;"
        "    retryInterval=$iv; retryDuration=$du"
        "  } | ConvertTo-Json -Compress"
        "} else { '{\"installed\":false}' }")
    txt = (out or "").strip()
    try:
        return json.loads(txt.splitlines()[-1]) if txt else {"installed": False}
    except ValueError:
        return {"installed": False, "error": (err or out or "").strip()[:200]}


def current_program():
    """当前程序自身的「可执行入口」路径，用于跟计划任务指向比对。

    exe 模式返回 exe 路径；脚本模式返回 checkin.py 的路径。
    """
    try:
        import runtime as _rt
        if _rt.is_frozen():
            return os.path.abspath(sys.executable)
        return os.path.abspath(os.path.join(_rt.data_root(), "checkin.py"))
    except Exception:  # noqa: BLE001
        return ""


def _is_python_interp(path):
    """任务里的 Execute 是不是 Python 解释器（= 脚本模式的注册方式）。"""
    name = os.path.basename(str(path or "")).lower()
    return name in ("python.exe", "pythonw.exe", "py.exe", "pyw.exe")


def _script_from_args(args):
    """从任务参数里取出真正的脚本路径。

    脚本模式注册出来的是
        Execute = C:\\Program Files\\Python313\\pythonw.exe
        Arguments = "C:\\...\\Auto get\\checkin.py" run --silent
    所以真入口藏在 Arguments 的第一个参数里（可能带引号）。
    """
    s = str(args or "").strip()
    if not s:
        return ""
    if s[0] in ('"', "'"):
        end = s.find(s[0], 1)
        if end > 1:
            return s[1:end]
    return s.split()[0]


def _data_root_safe():
    """当前数据根（拿不到就返回空串，绝不抛）。"""
    try:
        import runtime as _rt
        return os.path.abspath(_rt.data_root() or "")
    except Exception:  # noqa: BLE001
        return ""


def _under(path, root):
    """path 是否位于 root 目录树内（含 root 本身）。"""
    if not path or not root:
        return False
    try:
        p = os.path.normcase(os.path.abspath(path))
        r = os.path.normcase(os.path.abspath(root)).rstrip("\\/")
    except Exception:  # noqa: BLE001
        return False
    return p == r or p.startswith(r + os.sep)


def _data_root_of_program(path):
    """从某个程序路径反推出它的「数据根」（含 checkin.py 的目录）。

    为什么需要它：目录整理后，便携 exe 在 `AutoCheckinGUI/dist/<程序>/`，
    而签到脚本在**兄弟目录** `核心脚本/`（两者不存在文件树的嵌套关系）。
    但运行时 exe 会把自己的数据根解析到 `核心脚本/`，与源码脚本是**同一份
    数据**。只按"文件路径树是否嵌套"判断会把这种完全正常的组合误报成
    「任务指向不一致」，于是这里直接反推各自的数据根来比 —— 根相同即一致。

    规则：
      * 脚本（.py）：从其所在目录向上找第一个含 checkin.py 的目录；
      * exe（打包版）：从其路径向上找 `AutoCheckinGUI` 目录，取其兄弟
        `核心脚本`（整理后的桌面版结构）作为候选；仍找不到就退回到
        向上找第一个含 checkin.py 的目录。

    拿不到返回空串（调用方据此跳过这一步、不误报）。
    """
    if not path:
        return ""
    try:
        p = os.path.normcase(os.path.abspath(path))
    except Exception:  # noqa: BLE001
        return ""
    if p.endswith(".py"):
        cur = os.path.dirname(p)
    else:
        cur = p
    # 向上最多 6 级，找含 checkin.py 的目录（最通用的兜底）
    for _ in range(6):
        if os.path.isfile(os.path.join(cur, "checkin.py")):
            return os.path.normcase(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    # exe 专属：找 AutoCheckinGUI 的兄弟「核心脚本」
    cur = p
    for _ in range(7):
        base = os.path.basename(cur).lower()
        if base == "autocheckingui":
            cand = os.path.normcase(
                os.path.join(os.path.dirname(cur), "核心脚本"))
            if os.path.isfile(os.path.join(cand, "checkin.py")):
                return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


def task_target_mismatch(info=None):
    """计划任务指向的程序是否**不是**当前这份程序。

    返回 (是否不一致, 任务指向, 当前程序)。指向别的副本时，
    用户在界面上改的设置不会生效 —— 这是最容易让人困惑的故障之一。

    判据（两层，缺一不可）
    ----------------------
    1. **脚本模式要把「解释器 + 脚本」还原成一个入口再比。**
       只拿 `Execute` 去比的话，源码模式（任务 = pythonw.exe + checkin.py）
       **永远判为"不一致"**。返回给界面显示的「任务指向」也应该是 checkin.py
       而不是 pythonw.exe，否则用户照着提示也修不对。
    2. **两边都在同一个数据根目录树里，就算"同一套配置"。**
       这是判据的**本意**：用户真正关心的是"我在这儿改的设置，
       任务那边读不读得到"，而不是"是不是同一个文件"。
       本项目 `runtime.data_root()` 是向上逐级找「含 checkin.py + 数据」的目录，
       所以同一棵树里的任何副本（源码、`dist/` 里的便携 exe）都会解析到
       同一个根 —— 实测便携 exe 跑任务时确实写进了项目根的日志。
       只比"程序路径是否相同"的话，源码版 GUI + 便携版任务这种**完全正常**
       的组合也会恒报「▲ 需检查」；而假警报会让真警报失效。
    真正危险的是第 3 种情况：任务指向树**外**的另一个副本
    （例如装到 `%LOCALAPPDATA%\\Programs\\...`，或用户把便携版拖到了别处），
    那时它自解析的数据根与当前不是同一个 —— 这种必须报。
    """
    info = info if info is not None else (cached_task_info() or {})
    exe = str(info.get("exe") or "").strip()
    mine = current_program()
    if not exe or not mine:
        return False, exe, mine

    target = exe
    if _is_python_interp(exe):
        script = _script_from_args(info.get("args"))
        if script:
            target = script

    try:
        same = (os.path.normcase(os.path.abspath(target))
                == os.path.normcase(os.path.abspath(mine)))
    except Exception:  # noqa: BLE001
        same = os.path.normcase(target) == os.path.normcase(mine)

    if not same:
        # 程序不同，但只要两边解析出的「数据根」是同一个，读写的就是同一份
        # config.json / logs / state.json —— 设置是生效的，不该报警。
        # 优先用各自**反推出的数据根**比对（整理后 exe 与脚本不在同一文件树，
        # 但数据根相同，这是最准确的判据）；拿不到时再退回「都在当前数据根
        # 文件树内」的兼容判定。
        t_root = _data_root_of_program(target)
        m_root = _data_root_of_program(mine)
        if t_root and m_root and t_root == m_root:
            same = True
        else:
            root = _data_root_safe()
            if root and _under(target, root) and _under(mine, root):
                same = True

    return (not same), target, mine


def task_info(force=False):
    """查询计划任务状态（带缓存），返回 dict（未安装时 installed=False）。

    force=True 时忽略缓存直接查（用户点了「刷新」才用，会有 1s 卡顿）。
    """
    now = time.time()
    if not force:
        with _lock:
            if _cache["data"] is not None and (now - _cache["at"]) < _CACHE_TTL:
                return _cache["data"]
    data = _query_task_info()
    with _lock:
        _cache["data"] = data
        _cache["at"] = time.time()
    return data


def cached_task_info():
    """只读缓存，永不阻塞。无缓存时返回 None（供渲染路径使用）。"""
    with _lock:
        return _cache["data"]


def refresh_task_info_async(on_done=None):
    """后台刷新任务状态，完成后把结果交给 on_done（在主线程外调用）。

    返回 True 表示本次请求会被满足（已启动新线程，或已加入待补跑队列），
    False 仅出现在「连补跑请求都已登记」的极端情况，调用方可忽略。

    并发语义（重要）
    ----------------
    同一时刻只跑一个查询线程（避免多个 PowerShell 进程叠加拖慢界面），
    但**请求不会被丢弃**：刷新进行中再来请求就置 `_pending`，
    当前轮结束立刻补跑一轮，保证调用方最终一定拿到最新状态。
    """
    global _refreshing, _pending
    with _lock:
        if _refreshing:
            _pending = True
            return True
        _refreshing = True

    def work():
        global _refreshing, _pending
        # 每次查询都读一次「当前挂起的回调」，补跑轮用得到
        hook = on_done
        while True:
            try:
                data = _query_task_info()
                with _lock:
                    _cache["data"] = data
                    _cache["at"] = time.time()
                if hook:
                    try:
                        hook(data)
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                with _lock:
                    # 本轮结束：若期间有新的刷新请求，继续补跑一轮
                    if _pending:
                        _pending = False
                    else:
                        _refreshing = False
                        break
            # 走到这里说明 _pending 为真，需要再跑一轮。
            # 补跑轮不再重复回调外部 hook（调用方只关心「最终拿到最新值」，
            # 重复回调会导致界面重画两次）；但要在补跑前再确认一次是否还有请求。
            hook = None

    threading.Thread(target=work, daemon=True, name="task-info").start()
    return True


def task_installed():
    """任务是否已安装（走缓存，渲染路径安全）。"""
    return bool((cached_task_info() or {}).get("installed"))


def install(cfg=None):
    """注册/更新计划任务（内含每日定时 + 开机自启 + 网络恢复触发器）。"""
    cfg = cfg or C.load_config()
    try:
        import checkin
    except Exception as e:  # noqa: BLE001
        return False, f"无法导入 checkin 模块：{e!r}"
    try:
        ok = checkin.cmd_install(cfg)
        invalidate_task_cache()
        return bool(ok), ("计划任务已注册/更新" if ok else "注册失败，详见 logs/install_task.ps1")
    except Exception as e:  # noqa: BLE001
        invalidate_task_cache()
        return False, f"注册异常：{e!r}"


def uninstall():
    out, err = _powershell(
        f"Unregister-ScheduledTask -TaskName '{C.TASK_NAME}' -Confirm:$false;"
        "Write-Host 'UNINSTALLED'")
    invalidate_task_cache()
    if "UNINSTALLED" in out:
        return True, "计划任务已移除"
    return False, f"移除失败（可能本就不存在）：{(err or out).strip()[:200]}"


def invalidate_task_cache():
    """让任务状态缓存立即失效（安装/卸载/改时间之后必须调用）。

    否则顶栏与设置页会继续显示 5 秒前的旧状态，看起来像"点了没反应"。
    若此刻正有刷新在跑，额外登记一次补跑 —— 那一轮查到的可能还是改之前的
    旧值（查询请求早于本次改动发出），不补跑就会把旧值写回缓存。
    """
    global _pending
    with _lock:
        _cache["data"] = None
        _cache["at"] = 0.0
        if _refreshing:
            _pending = True


def run_task_now():
    """立即触发一次计划任务（等价于计划任务的那一次执行，走门控）。"""
    out, err = _powershell(
        f"Start-ScheduledTask -TaskName '{C.TASK_NAME}'; Write-Host 'STARTED'")
    if "STARTED" in out:
        return True, "已触发计划任务执行"
    return False, f"触发失败：{(err or out).strip()[:200]}"


def set_times(times):
    """设定每日执行时间并重注册（复用 checkin.cmd_settime 的校验与写盘）。"""
    try:
        import checkin
    except Exception as e:  # noqa: BLE001
        return False, f"无法导入 checkin 模块：{e!r}"
    import io
    buf = io.StringIO()
    old = sys.stdout
    try:
        sys.stdout = buf
        rc = checkin.cmd_settime(list(times))
    except Exception as e:  # noqa: BLE001
        sys.stdout = old
        return False, f"设定异常：{e!r}"
    finally:
        sys.stdout = old
    msg = buf.getvalue().strip()
    return (rc == 0), msg


def set_retry(interval_min, window_min, enabled=True):
    """设定自动补跑参数并重注册（复用 checkin.cmd_setretry 的校验与写盘）。

    与 `set_times` 同一套路：捕获 checkin 的 stdout 当返回值，避免它的 print
    直接漏到控制台（GUI 模式没有控制台，写 stdout 会炸）。
    """
    try:
        import checkin
    except Exception as e:  # noqa: BLE001
        return False, f"无法导入 checkin 模块：{e!r}"
    import io
    buf = io.StringIO()
    old = sys.stdout
    try:
        sys.stdout = buf
        rc = checkin.cmd_setretry(int(interval_min), int(window_min), bool(enabled))
    except Exception as e:  # noqa: BLE001
        sys.stdout = old
        return False, f"设定异常：{e!r}"
    finally:
        sys.stdout = old
    return (rc == 0), buf.getvalue().strip()


def network_ok(timeout_s=4):
    """网络可用性（复用项目内的 net_util 判定口径）。"""
    try:
        import net_util
        return bool(net_util.network_ok(timeout_s=timeout_s))
    except Exception:  # noqa: BLE001
        return False


def last_run_summary():
    """最近一次签到轮次的摘要（从 history 尾部按 ts 聚类）。"""
    rows = C.load_history()
    if not rows:
        return None
    last_ts = str(rows[-1].get("ts") or "")
    group = [r for r in rows if str(r.get("ts") or "") == last_ts]
    ok, busy, fail = C.count_status(group)
    return {
        "ts": last_ts,
        "total": len(group),
        "ok": ok,
        "busy": busy,
        "fail": fail,
        "rows": group,
    }


def open_records_folder():
    """在资源管理器中打开签到记录目录。"""
    try:
        os.makedirs(C.LOG_DIR, exist_ok=True)
        os.startfile(C.LOG_DIR)  # noqa: S606  (Windows-only 项目)
        return True, C.LOG_DIR
    except Exception as e:  # noqa: BLE001
        return False, repr(e)


def open_record_csv():
    """用默认程序（通常是 Excel）打开签到记录 CSV。"""
    try:
        if not os.path.exists(C.RECORD_PATH):
            return False, "签到记录文件尚未生成"
        os.startfile(C.RECORD_PATH)  # noqa: S606
        return True, C.RECORD_PATH
    except Exception as e:  # noqa: BLE001
        return False, repr(e)


def open_config_file():
    """打开 config.json（便于高级用户直接编辑）。"""
    try:
        os.startfile(C.CONFIG_PATH)  # noqa: S606
        return True, C.CONFIG_PATH
    except Exception as e:  # noqa: BLE001
        return False, repr(e)


def purge_records(keep_days=0, clear_history=False, clear_state=False):
    """维护：按保留天数清理历史记录。

    keep_days>0 时只保留最近 N 天的 screenshots 目录；
    clear_history 清空 history.jsonl；clear_state 清空 state.json（下次会重跑）。
    返回 (ok, 说明文本)。
    """
    import shutil
    notes = []
    try:
        shots = os.path.join(C.LOG_DIR, "screenshots")
        if keep_days > 0 and os.path.isdir(shots):
            cutoff = datetime.date.today() - datetime.timedelta(days=keep_days)
            removed = 0
            for name in os.listdir(shots):
                d = os.path.join(shots, name)
                if not os.path.isdir(d):
                    continue
                try:
                    day = datetime.date.fromisoformat(name)
                except ValueError:
                    continue
                if day < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            notes.append(f"已清理 {removed} 天的截图目录（保留最近 {keep_days} 天）")

        if clear_history and os.path.exists(C.HISTORY_PATH):
            with open(C.HISTORY_PATH, "w", encoding="utf-8"):
                pass
            notes.append("history.jsonl 已清空")

        if clear_state and os.path.exists(C.STATE_PATH):
            with open(C.STATE_PATH, "w", encoding="utf-8") as f:
                f.write("{}")
            notes.append("state.json 已清空（下次签到会全部重跑）")

        if not notes:
            notes.append("没有需要清理的内容")
        return True, "；".join(notes)
    except Exception as e:  # noqa: BLE001
        return False, repr(e)


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
