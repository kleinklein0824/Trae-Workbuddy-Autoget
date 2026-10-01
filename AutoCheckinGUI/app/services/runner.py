#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""签到执行器：在后台线程里调用既有签到模块，把进度与日志实时回传 UI。

设计要点：
  * 复用 checkin.run_all / tw_checkin / tc_checkin / wb_checkin，不重写任何
    签到逻辑，保证 GUI 与计划任务行为完全一致。
  * 每个平台/账号单独执行，逐个通过回调上报，UI 可以边跑边显示进度。
  * 全部耗时操作都在工作线程，UI 线程只通过 queue 取消息，不阻塞。
"""
import datetime
import queue
import threading
import time

from .. import config as C

# ★ 手动点击（各板块「开始签到」/ 顶栏与托盘「一键签到（全部平台）」）默认
#   愿意为「另一个实例正占着浏览器」等多久（秒）。见 CheckinRunner.wait_lock_s。
#   为什么需要：TraeWork 用浏览器 profile，同一 profile 只允许一个实例。
#   自动化那一轮正在跑时，手动点击原先会**立刻**被判成 busy（跳过），
#   用户只能干等下一轮自动补跑 —— 也就是「手动点了却要等」的滞后感。
#   给一个有限等待，手动点击就能**优先执行**：等对方让出浏览器后立刻签到。
#   ★ 只等不抢：并发用同一个 user-data-dir 会丢登录态甚至损坏 profile。
MANUAL_LOCK_WAIT_S = 120.0


class CheckinRunner:
    """一次签到任务的执行上下文。

    事件通过 self.events（queue.Queue）抛出，类型：
      {"type":"log",     "text": str}                      普通日志
      {"type":"start",   "platform": str, "account": str}  某账号开始
      {"type":"result",  "platform": str, "account": str,
       "status": str, "detail": str}                       某账号完成
      {"type":"platform_done", "platform": str}            某平台结束
      {"type":"done",    "results": [...]}                 全部结束
      {"type":"error",   "text": str}                       致命错误
    """

    def __init__(self, platforms=None, force=False, use_gate=False, accounts=None,
                 wait_lock_s=0.0):
        self.platforms = list(platforms) if platforms else ["traework", "traecn", "workbuddy"]
        self.force = force
        self.use_gate = use_gate          # True 时走门控（等价计划任务运行）
        self.accounts = accounts or {}    # {platform: [id, ...]} 仅签到选中的账号
        # > 0 表示这是**手动优先**触发：TraeWork 的 profile 被别的实例占着时
        # 先等它释放（最多这么多秒）再签到，而不是立刻记 busy 跳过。
        self.wait_lock_s = float(wait_lock_s or 0.0)
        self.events = queue.Queue()
        self._thread = None
        self.cancelled = threading.Event()

    @property
    def manual(self):
        """是否「手动优先」触发（用于把结果文案说清楚）。"""
        return self.wait_lock_s > 0

    # ---------- 对外接口 ----------

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="checkin")
        self._thread.start()
        return self

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def cancel(self):
        self.cancelled.set()

    def _emit(self, **kw):
        self.events.put(kw)

    def _log(self, text):
        self._emit(type="log", text=str(text))

    # ---------- 执行主体 ----------

    def _run(self):
        t0 = time.time()
        try:
            results = []
            for pid in self.platforms:
                if self.cancelled.is_set():
                    self._log(f"[{pid}] 已取消")
                    break
                self._log(f"===== 开始处理 {pid} =====")
                got = self._run_platform(pid)
                results += got
                self._emit(type="platform_done", platform=pid)
            elapsed = time.time() - t0
            ok, busy, fail = C.count_status(results)
            # ★ busy（服务端限流 / profile 被占用）不能算「失败」：它下一轮补跑
            #   会自动复核，报失败会误导（2026-09-19 实测）。
            self._log(f"全部完成：成功/跳过 {ok}，待复核 {busy}，失败 {fail}，"
                      f"耗时 {elapsed:.1f}s")
            if self.manual:
                # 手动优先执行：明确告诉用户「已经即时跑过了、不用等自动补跑」
                if busy or fail:
                    self._log(f"手动优先执行已完成：本次补签未完成 {busy + fail} 项"
                              f"（待复核 {busy} / 失败 {fail}）—— 可再点一次立即重试；"
                              f"自动补跑仍会按计划兜底")
                else:
                    self._log("手动优先执行已完成，全部账号均已签好，无需等待自动补跑")
            self._emit(type="done", results=results)
        except Exception as e:  # noqa: BLE001
            import traceback
            self._log("执行异常：\n" + traceback.format_exc())
            self._emit(type="error", text=repr(e))

    def _run_platform(self, pid):
        """执行单个平台；异常一律收敛为该平台的失败结果，不影响其他平台。"""
        ts = lambda: datetime.datetime.now().isoformat(timespec="seconds")  # noqa: E731
        try:
            import checkin as core
            cfg = core.load_config()
            state = core.load_state()
            today = datetime.date.today().isoformat()

            # 让既有模块的 log() 把内容转发到 UI
            core.log = self._log

            if self.use_gate and not self.force:
                reason = core._gate_skip(cfg, state, today)
                if reason:
                    self._log(f"[{pid}] 门控跳过：{reason}")
                    return []

            # ---------------- TraeWork ----------------
            if pid == "traework":
                import tw_checkin
                tw_cfg = cfg.get("traework", {})
                if not tw_cfg.get("enabled", True):
                    self._log("[traework] 配置中已禁用，跳过")
                    return []
                all_acc = tw_cfg.get("accounts", [])
                picked = self.accounts.get("traework")
                if picked is not None:
                    all_acc = [a for a in all_acc
                               if (a.get("name") or a.get("profile") or "default") in picked]
                if not all_acc:
                    self._log("[traework] 没有选中的账号")
                    return []
                out = []
                for acc in all_acc:
                    name = acc.get("name") or acc.get("profile") or "default"
                    key = f"traework:{name}"
                    display = tw_checkin._display_name(cfg, acc)
                    if self.cancelled.is_set():
                        break
                    if not self.force and state.get(key) == today:
                        self._emit(type="result", platform=pid, account=display,
                                    status="skip", detail="今日已完成")
                        out.append(dict(ts=ts(), platform=pid, account=display,
                                        status="skip", detail="今日已完成"))
                        continue
                    self._emit(type="start", platform=pid, account=display)
                    file_log = self._make_file_logger("traework")
                    if self.manual:
                        self._log(f"[traework] {display}: 手动优先执行"
                                  f"（若浏览器正被别的实例占用，最多等 "
                                  f"{int(self.wait_lock_s)}s 再签到）")
                    try:
                        from playwright.sync_api import sync_playwright
                        with sync_playwright() as p:
                            status, detail = tw_checkin._run_account(
                                p, cfg, acc, file_log,
                                __import__("os").path.join(
                                    __import__("os").path.dirname(core.__file__),
                                    "logs", "screenshots", today),
                                today, lock_wait_s=self.wait_lock_s)
                    except Exception as e:  # noqa: BLE001
                        status, detail = "error", f"异常: {e!r}"
                    file_log(f"[traework] {display}: {detail}")
                    # 与 checkin 同一口径：接口确认「今日已签到」也写日期记录，
                    # 否则每轮补跑都要重开浏览器复核，白多花 ~30s。
                    if status == "ok" or (status == "skip"
                                          and detail.startswith("今日已签到")):
                        state[key] = today
                    out.append(dict(ts=ts(), platform=pid, account=display,
                                    status=status, detail=detail))
                    self._emit(type="result", platform=pid, account=display,
                               status=status, detail=detail)
                core.save_state(state)
                core._append_records(out)
                return out

            # ---------------- TraeCN ----------------
            if pid == "traecn":
                import tc_checkin
                if not cfg.get("traecn", {}).get("enabled", True):
                    self._log("[traecn] 配置中已禁用，跳过")
                    return []
                accs = tc_checkin.collect_accounts(cfg, self._log)
                picked = self.accounts.get("traecn")
                if picked is not None:
                    accs = [a for a in accs if a["uid"] in picked]
                if not accs:
                    self._log("[traecn] 没有可用账号")
                    return [dict(ts=ts(), platform=pid, account="TraeCN", status="fail",
                                 detail="未发现令牌，打开 IDE 登录后自动恢复")]
                out = []
                for acc in accs:
                    if self.cancelled.is_set():
                        break
                    name = acc["name"]
                    key = f"traecn:{acc['uid']}"
                    if not self.force and state.get(key) == today:
                        self._emit(type="result", platform=pid, account=name,
                                    status="skip", detail="今日已完成")
                        out.append(dict(ts=ts(), platform=pid, account=name,
                                        status="skip", detail="今日已完成"))
                        continue
                    self._emit(type="start", platform=pid, account=name)
                    # 用 checkin_account 而不是 _checkin_one：前者在「当前令牌
                    # 被服务端吊销」时会自动换另一枚候选令牌重试（见 tc_checkin）。
                    status, detail = tc_checkin.checkin_account(acc, self._log)
                    if status == "ok" or (status == "skip" and detail.startswith("今日已签到")):
                        state[key] = today
                    self._emit(type="result", platform=pid, account=name,
                               status=status, detail=detail)
                    out.append(dict(ts=ts(), platform=pid, account=name,
                                    status=status, detail=detail))
                core.save_state(state)
                core._append_records(out)
                return out

            # ---------------- WorkBuddy ----------------
            if pid == "workbuddy":
                import wb_checkin
                if not cfg.get("workbuddy", {}).get("enabled", True):
                    self._log("[workbuddy] 配置中已禁用，跳过")
                    return []
                accs = wb_checkin._collect_accounts(cfg, self._log)
                picked = self.accounts.get("workbuddy")
                if picked is not None:
                    accs = [a for a in accs if a["uid"] in picked]
                if not accs:
                    self._log("[workbuddy] 没有可用账号")
                    return [dict(ts=ts(), platform=pid, account="-", status="fail",
                                 detail="未找到登录态")]
                out = []
                for acc in accs:
                    if self.cancelled.is_set():
                        break
                    name = acc.get("nickname") or acc["uid"][:8] + "****"
                    key = f"workbuddy:{acc['uid']}"
                    if not self.force and state.get(key) == today:
                        detail = "今日已完成" + wb_checkin._expiry_warning(acc)
                        self._emit(type="result", platform=pid, account=name,
                                    status="skip", detail=detail)
                        out.append(dict(ts=ts(), platform=pid, account=name,
                                        status="skip", detail=detail))
                        continue
                    self._emit(type="start", platform=pid, account=name)
                    status, detail = wb_checkin._checkin_one(acc, self._log)
                    if status == "ok":
                        state[key] = today
                        detail += wb_checkin._expiry_warning(acc)
                    self._emit(type="result", platform=pid, account=name,
                               status=status, detail=detail)
                    out.append(dict(ts=ts(), platform=pid, account=name,
                                    status=status, detail=detail))
                core.save_state(state)
                core._append_records(out)
                return out
        except Exception as e:  # noqa: BLE001
            import traceback
            self._log(f"[{pid}] 平台执行异常：\n" + traceback.format_exc())
            return [dict(ts=ts(), platform=pid, account="-", status="error",
                         detail=repr(e))]
        return []

    def _make_file_logger(self, pid):
        """TraeWork 需要把日志同时写进 logs 目录（保留原有落盘行为）。"""
        def _f(msg):
            self._log(msg)
            try:
                import os
                logdir = __import__("os").path.join(
                    __import__("os").path.dirname(
                        __import__("checkin").__file__), "logs")
                os.makedirs(logdir, exist_ok=True)
                line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
                with open(os.path.join(logdir, f"checkin_{datetime.date.today()}.log"),
                          "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
        return _f


def run_full(platforms=None, force=False, use_gate=False, accounts=None,
             wait_lock_s=0.0):
    """便捷入口：返回一个已启动的 CheckinRunner。

    `wait_lock_s` 见 `CheckinRunner.wait_lock_s`（手动优先执行时传
    `MANUAL_LOCK_WAIT_S`）。
    """
    return CheckinRunner(platforms=platforms, force=force,
                         use_gate=use_gate, accounts=accounts,
                         wait_lock_s=wait_lock_s).start()
