#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TraeWork 每日签到（work.trae.cn，浏览器自动化，多账号持久化 profile）

多账号原理：每个账号一个独立的 Chromium 持久化用户目录 profiles/traework/<profile>，
各账号登录态互不覆盖。首次使用运行 `python checkin.py login traework [账号名]`
在弹出的浏览器里手动登录一次，之后脚本自动复用会话签到。

签到流程：头像菜单 -> 积分余额按钮 -> 新标签页「用量管理」-> 点击签到 /
依据「签到奖励」到期日（今天+31 天）判定今日已签。
积分页位于 cloud.trae.cn，网络偶发不可达，故钩住 window.open 捕获真实
URL，弹出页加载失败时用该 URL 直连重试。
"""
import os
import re
import time
from datetime import date as _date, datetime as _datetime

import net_util

BASE_URL = "https://work.trae.cn/"
LOGIN_HINT_RE = re.compile(r"login|signin|passport|oauth|sso|auth", re.I)

# 可点击文本：优先“签到”，其次“领取”
SIGN_TEXT_RE = re.compile(r"^(每日|立即|去)?签到$|^签到(领积分|领福利|有礼)$|^领取(今日|每日)?(礼包|积分|奖励)?$")
DONE_TEXT_RE = re.compile(r"已签到|今日已(签|领)|明天(再来|见)|今日积分已领取")
SUCCESS_TEXT_RE = re.compile(r"签到成功|已签到|获得\d+积分|成功领取")
TEXT_LIMIT = 16
# 积分余额徽标：纯整数（带千分位，排除 29.9 这类价格）
POINTS_NUM_RE = re.compile(r"^\d{1,3}(,\d{3})*$")

# 头像候选（左下角）：第 k 小的可文本元素，逐个尝试直到菜单打开
_JS_MENU_CANDIDATES = r"""
(k) => {
  const vh = window.innerHeight;
  const cands = [];
  for (const el of document.querySelectorAll('span, div, button, a')) {
    if (el.children.length > 3) continue;
    const t = (el.innerText || '').trim();
    if (!t || t.length > 24) continue;
    if (/^\d/.test(t)) continue;                        // 跳过数字（积分徽标）
    if (/下载|桌面|退出|升级|^免费$|^lite$|^pro/i.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.y < vh * 0.85 || r.x > 200 || r.width < 16 || r.height < 10) continue;
    cands.push({ el, area: r.width * r.height });
  }
  cands.sort((a, b) => a.area - b.area);
  if (k >= cands.length) return false;
  cands[k].el.setAttribute('data-cb-mark', 'avatar');
  return true;
}
"""
_JS_MENU_OPEN = r"""
() => {
  const els = document.querySelectorAll('button, div, span, a');
  for (const el of els) {
    if (/退出登录|管理账户|报告问题/.test(el.innerText || '')) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) return true;
    }
  }
  return false;
}
"""
# 钩住 window.open，记录即将弹出的 URL（弹出页加载失败时用于直连重试）
_JS_HOOK_OPEN = r"""
() => {
  window.__cbPopUrl = null;
  const orig = window.open;
  window.open = function (url, name, specs) {
    try { window.__cbPopUrl = url; } catch (e) {}
    return orig ? orig.call(window, url, name, specs) : null;
  };
  return true;
}
"""
_JS_POP_URL = "() => window.__cbPopUrl || null"

# ---------------------------------------------------------------- 接口直连（接口优先）
# 为什么加这条路（2026-09-18 定因）
# --------------------------------
# TraeWork 的签到判定原本**完全依赖页面文本**：展开「每日签到」明细，找
# 「到期日 == 今天+31」的条目。这条链路的两个天生弱点在 09-18 当天都暴露了：
#   ① 明细懒加载 + 异步刷新，条目比页面晚到。09:00 实测：明细已渲染 9 条却
#      **没有今日条目** → 判 fail；09:30 重试同一条码就成功了 —— 签到本身
#      早就成了，只是没被"看见"（history 里 traework 累计 26 次 fail 的主因）；
#   ② 它与 TraeCN 其实是**同一个 Trae 账号、同一个签到活动**（uid 完全相同，
#      都是 api.trae.cn 的 checkin_credits），而 TraeCN 走的是接口直连。
# 实测进一步发现：TraeWork 页面（www.trae.cn）的 localStorage 里就有同款
# `Cloud-IDE-Token`（Cloud-IDE-JWT），直接拿它调
#   POST api.trae.cn/trae/api/v2/ug/checkin_credits/status
# 返回 {"checked_in":true,"code":0,"credits":150,"extra_credits":50,"enable":true}
# —— 判定一次到位：不用等明细渲染、不怕页面改版，还能把「今日已签」和
# 「确实没签成」**明确区分开**（旧文本判据只能给出含糊的"没找到条目"）。
# 因此这里把**接口路径提到最前**，页面文本判据保留为兜底。
TOKEN_RE = re.compile(r"^ey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")

_JS_LOCAL_TOKEN = r"""
() => {
  const keys = ['Cloud-IDE-Token', 'cloud-ide-token', 'CloudIDEToken'];
  for (const k of keys) {
    try {
      const v = localStorage.getItem(k);
      if (v && /^ey[A-Za-z0-9_-]+\./.test(v)) return v;
    } catch (e) {}
  }
  return null;
}
"""

_JS_LOCAL_DID = r"""
() => {
  try { return localStorage.getItem('SOLO_DEVICE_ID') || null; }
  catch (e) { return null; }
}
"""

# localStorage 里没有令牌时的备用：直接问服务端要一枚（同源、带凭据）
_JS_FETCH_TOKEN = r"""
async () => {
  try {
    const r = await fetch('https://api.trae.cn/cloudide/api/v3/common/GetUserToken',
                          {method: 'POST', credentials: 'include',
                           headers: {'Content-Type': 'application/json'},
                           body: JSON.stringify({ReqSource: 'IDE'})});
    const j = await r.json();
    return ((j.Result || {}).Token) || null;
  } catch (e) { return null; }
}
"""

# 轻量令牌页：只要浏览器能加载这个源，就能读到 localStorage 里的令牌。
# 用它替代「头像菜单 → 积分按钮 → 新标签页」这条脆弱链路。
TOKEN_PAGE_URL = "https://www.trae.cn/"

# 签到类按钮：**只收集**页面上的可见短文本并打标记（点击用 locator，自动滚动到可见区域）
#
# ⚠️ 正则**不能嵌进 JS**。旧写法把 `SIGN_TEXT_RE.pattern` 直接插进
# `if ((%s).test(t))`，生成的是
#     if ((^(每日|立即|去)?签到$|^签到(领积分|领福利|有礼)$|...).test(t))
# —— **非法 JavaScript**（`^` 不能这样当表达式用），`page.evaluate` 抛
# `SyntaxError: Unexpected token '^'`，于是**永远返回 0 个候选**：
# 「点击签到按钮」这条兜底路径**从来没生效过**（2026-09-17 排查才暴露）。
# 现在 JS 只收集文本，正则过滤放回 Python —— 正则只有一处定义、可离线单测，
# 也不可能再产生 JS 语法错误。
_JS_MARK_SIGNS = r"""
() => {
  const out = [];
  const seen = new Set();
  let i = 0;
  for (const el of document.querySelectorAll('button, a, [role="button"], span, div, p, li')) {
    if (el.children.length > 2) continue;
    const t = (el.innerText || '').trim();
    if (!t || t.length > %d || seen.has(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.pointerEvents === 'none') continue;
    el.setAttribute('data-cb-sign', 's' + i);
    out.push({ text: t, mark: 's' + i });
    i++; seen.add(t);
  }
  return out;
}
""" % TEXT_LIMIT

# 「每日签到」这一行是**展开/收起明细的开关**，不是"签到按钮"。
# 必须排除在 `_try_click_sign` 之外：修好上面的 JS 之后实测它就命中了
# `SIGN_TEXT_RE`，若两处都去点它 —— 一处展开、一处收起 —— 会互相打架，
# 表现为「刚展开又被折回去，明细永远看不到」。
SIGN_TOGGLE_TEXT = "每日签到"


def _profile_dir(cfg, acc):
    root = os.path.dirname(os.path.abspath(__file__))
    profiles_root = os.path.join(root, "profiles", "traework")
    return os.path.join(profiles_root, acc.get("profile") or acc.get("name") or "default")


# ---------------------------------------------------------------- profile 互斥锁
# 为什么必须加锁（2026-09-17 实测事故）
# ------------------------------------
# 计划任务 12:13:09 触发时，手工跑的诊断脚本正在用**同一个** Chromium profile。
# 第二个实例拿不到登录态，于是判定为"会话未登录" → **弹出有头浏览器等 7 分钟
# 手动登录** → 超时失败。用户完全不知道发生了什么，只看到一条失败记录。
# Chromium 的 `user-data-dir` 本来就是**独占**的：并发使用不仅会丢会话，
# 还可能损坏 profile。所以同一 profile 只允许一个实例使用。
_LOCK_NAME = ".checkin.lock"
# 超过这个时长仍未释放，视为残留锁（正常一轮最多几分钟）
_LOCK_STALE_S = 30 * 60
# 「等锁」时的轮询间隔（见 _acquire_profile_lock 的 wait_s）
_LOCK_POLL_S = 0.5
# PROCESS_QUERY_LIMITED_INFORMATION；STILL_ACTIVE
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _pid_alive(pid):
    """进程是否还活着。判不出来时**保守返回 True**（宁可跳过也不要并发）。"""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == _STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return True


def _acquire_profile_lock(profile, log, name, wait_s=0.0):
    """给账号 profile 加互斥锁。成功返回锁文件路径，被占用返回 None。

    `wait_s` > 0 时：锁被**活进程**持有时**轮询等待它释放**（最多 wait_s 秒）再放弃。
    这是给「手动优先签到」用的（2026-09-24）：
      自动化那一轮正在用同一个 profile 时，手动点击原先会**立刻**被判成 `busy`
      （跳过），用户只能干等下一轮自动补跑 —— 也就是「手动点了却要等」。
      等待让手动点击**优先执行**：等对方让出浏览器，然后马上签到。
    ★ **绝不抢占**：并发使用同一个 `user-data-dir` 会丢登录态甚至损坏 profile，
      所以只等不抢；等不到就照旧返回 None（记 `busy`，由补跑兜底）。
    ★ 默认 `wait_s=0` = 与改动前完全一致，计划任务/CLI 路径不受影响。
    """
    os.makedirs(profile, exist_ok=True)
    path = os.path.join(profile, _LOCK_NAME)
    deadline = time.time() + max(0.0, float(wait_s or 0))
    waited = False
    last_told = 0.0
    cleaned = False
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("%d %s\n" % (os.getpid(),
                                     time.strftime("%Y-%m-%d %H:%M:%S")))
            if waited:
                log(f"[traework] {name}: 另一个实例已让出浏览器，现在开始签到")
            return path
        except FileExistsError:
            # 已有锁：判断是**残留**还是真的在用
            pid, age = None, None
            try:
                with open(path, encoding="utf-8") as f:
                    pid = int((f.read().split() or ["0"])[0])
                age = time.time() - os.path.getmtime(path)
            except Exception:  # noqa: BLE001
                pass
            stale = (age is not None and age > _LOCK_STALE_S)
            dead = (pid is not None and not _pid_alive(pid))
            if not cleaned and (stale or dead):
                cleaned = True        # 只清理一次，避免与别人反复抢建形成死循环
                log(f"[traework] {name}: 清理残留的 profile 锁"
                    f"（pid={pid} 已退出={dead} 超龄={stale}）")
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            if time.time() < deadline:
                waited = True
                now = time.time()
                if now - last_told >= 5.0:      # 每 5 秒报一次进度，别刷屏
                    last_told = now
                    log(f"[traework] {name}: 该账号的浏览器正被另一个实例使用"
                        f"（pid={pid}），等待它释放（还剩约 {int(deadline - now)}s）…")
                time.sleep(_LOCK_POLL_S)
                continue
            log(f"[traework] {name}: 该账号的浏览器 profile 正被另一个实例使用"
                f"（pid={pid}）" + ("，等待超时" if waited else "")
                + "，本轮跳过以免并发丢登录态")
            return None
        except Exception as e:  # noqa: BLE001
            # 加锁本身失败不该阻断签到（例如目录只读），记一笔继续
            log(f"[traework] {name}: profile 锁不可用({e!r})，继续执行")
            return path


def _release_profile_lock(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--disable-gpu"]


def _page_text(page):
    try:
        return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:  # noqa: BLE001
        return ""


# 左下角用户栏文本（头像旁的账号真实昵称；排除套餐徽标/菜单项）
_JS_NICK = r"""
() => {
  const vh = window.innerHeight;
  const bad = /^(免费|lite|pro|pro\+|ultra|专业版|标准版|团队版|企业版|退出|退出登录|下载|桌面端|报告问题|管理账户)$/;
  let best = null;
  for (const el of document.querySelectorAll('span, div, button, a, p')) {
    if (el.children.length > 2) continue;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 24 || bad.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.y < vh * 0.85 || r.x > 160 || r.width < 16 || r.height < 10) continue;
    if (!best || r.width * r.height < best.w * best.h) best = { t, w: r.width, h: r.height };
  }
  return best ? best.t : null;
}
"""


def _nick_file(cfg, acc):
    return os.path.join(_profile_dir(cfg, acc), "nickname.txt")


def _save_nick(cfg, acc, nick):
    try:
        with open(_nick_file(cfg, acc), "w", encoding="utf-8") as f:
            f.write(nick)
    except OSError:
        pass


def _cached_nick(cfg, acc):
    try:
        with open(_nick_file(cfg, acc), encoding="utf-8") as f:
            return (f.read() or "").strip()
    except OSError:
        return ""


def _display_name(cfg, acc):
    """展示名：优先账号真实昵称（缓存），回退配置标签名。"""
    return _cached_nick(cfg, acc) or (acc.get("name") or acc.get("profile") or "default")


# 未登录标志：页面上存在独立的「登录」按钮（登录后由头像替代）
_JS_HAS_LOGIN_BTN = r"""
() => {
  const re = /^\s*登录\s*[→>]?\s*$/;
  const els = document.querySelectorAll('button, a, [role="button"], span, div, p');
  for (const el of els) {
    if (el.children.length > 1) continue;
    const t = (el.innerText || '').trim();
    if (t && t.length <= 6 && re.test(t)) return true;
  }
  return false;
}
"""


def _login_state(page):
    """判定登录态：'in' 已登录 / 'out' 未登录 / 'unknown' 页面未渲染完成无法判定。

    关键：SPA 渲染完成前页面上没有任何「登录」按钮，不能据此判定已登录，
    必须以“页面已渲染出足量文本”作为判定前提。
    """
    url = page.url or ""
    host = url.split("/")[2] if "://" in url and len(url.split("/")) > 2 else ""
    if "work.trae.cn" not in host:
        return "out"
    if LOGIN_HINT_RE.search(url.split("work.trae.cn")[-1]):
        return "out"
    try:
        body = (page.evaluate("() => document.body ? document.body.innerText : ''") or "").strip()
        if len(body) < 30:
            return "unknown"
        if page.evaluate(_JS_HAS_LOGIN_BTN):
            return "out"
        return "in"
    except Exception:  # noqa: BLE001
        return "unknown"


def _wait_login_state(page, timeout_s):
    """轮询等待出现确定登录态，返回 'in' / 'out'。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = _login_state(page)
        if st == "in":
            return "in"
        if st == "unknown":
            try:
                page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
            continue
        return "out"
    return "out"


def _shot(page, shots_dir, name):
    try:
        os.makedirs(shots_dir, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|]', "_", name)  # 展示名可能含昵称，去掉文件非法字符
        page.screenshot(path=os.path.join(shots_dir, safe), full_page=False)
    except Exception:  # noqa: BLE001
        pass


def _wait_login_interactive(page, account_name, timeout_s=420, log=print):
    log(f"[traework] 请在弹出的浏览器窗口中完成账号「{account_name}」的登录（最长等待 {timeout_s // 60} 分钟）…")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        # 只认确定态：unknown（未渲染完）继续等，out 也继续等（用户可能正在登录页与主页间跳转）
        if _wait_login_state(page, timeout_s=8) == "in":
            return True
        time.sleep(2)
    return False


def _try_click_sign(page, log, name, shots_dir, tag, max_clicks=3):
    """在指定页面上扫描并点击签到类按钮，返回 'ok' / 'done' / None。"""
    try:
        cands = page.evaluate(_JS_MARK_SIGNS) or []
    except Exception:  # noqa: BLE001
        return None
    # 正则过滤在 Python 侧做（理由见 `_JS_MARK_SIGNS` 注释）；
    # 并排除「每日签到」这个**展开开关**，避免和明细展开逻辑互相点来点去。
    cands = [c for c in cands
             if c.get("text") and SIGN_TEXT_RE.fullmatch(c["text"])
             and c["text"] != SIGN_TOGGLE_TEXT]
    cands.sort(key=lambda c: 0 if "签到" in c["text"] else 1)
    for attempt, c in enumerate(cands[:max_clicks]):
        log(f"[traework] {name}: 点击「{c['text'][:12]}」({tag} attempt {attempt + 1})")
        try:
            # locator 点击会自动滚动到可见区域
            page.click(f"[data-cb-sign='{c['mark']}']", timeout=6000)
        except Exception as e:  # noqa: BLE001
            log(f"[traework] {name}: 点击失败: {e!r}")
            continue
        time.sleep(2.5)
        text = _page_text(page)
        if SUCCESS_TEXT_RE.search(text):
            _shot(page, shots_dir, f"{name}_{tag}_success.png")
            return "ok"
        if DONE_TEXT_RE.search(text):
            return "done"
    return None


def _wait_render(page, min_chars=60, timeout_s=15):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if len(_page_text(page).strip()) >= min_chars:
            return True
        time.sleep(1.5)
    return False


def _click_points_badge(page):
    """遍历可点击元素定位积分余额徽标（纯数字千分位），返回 (locator, 文本)。"""
    for loc in page.locator("button, [role='button'], a").all():
        try:
            t = (loc.inner_text(timeout=800) or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if POINTS_NUM_RE.match(t):
            return loc, t
    return None, None


def _open_menu(page, log, name):
    """打开左下角头像菜单：候选元素逐个尝试 + 常驻用户栏坐标兜底。"""
    if page.evaluate(_JS_MENU_OPEN):
        return True
    for k in range(4):
        if not page.evaluate(_JS_MENU_CANDIDATES, k):
            break
        try:
            page.click("[data-cb-mark='avatar']", timeout=4000)
        except Exception:  # noqa: BLE001
            continue
        time.sleep(2)
        if page.evaluate(_JS_MENU_OPEN):
            return True
    # 兜底：点击屏幕左下角常驻用户栏（头像+昵称，任何登录态都存在）
    try:
        vh = page.viewport_size.get("height", 800) or 800
        page.mouse.click(100, vh - 18)
        time.sleep(2)
        if page.evaluate(_JS_MENU_OPEN):
            return True
    except Exception:  # noqa: BLE001
        pass
    log(f"[traework] {name}: 无法打开头像菜单（未检测到菜单项）")
    return False


def _open_points_dashboard(ctx, page, log, name):
    """头像菜单 -> 积分余额按钮 -> 「用量管理」新标签页；返回目标 page 或 None。"""
    try:
        if not _open_menu(page, log, name):
            return None

        # 钩住 window.open 记录弹出 URL（弹出页加载失败时直连重试用）
        try:
            page.evaluate(_JS_HOOK_OPEN)
        except Exception:  # noqa: BLE001
            pass

        loc, t = _click_points_badge(page)
        if loc is None:
            log(f"[traework] {name}: 头像菜单中未找到积分余额按钮")
            return None
        log(f"[traework] {name}: 点击积分按钮「{t}」")

        popup = None
        try:
            with ctx.expect_event("page", timeout=15000) as ev:
                loc.click(timeout=5000)
            popup = ev.value
        except Exception as e:  # noqa: BLE001
            log(f"[traework] {name}: 点击积分按钮后未弹出新标签: {e!r}")

        pop_url = None
        try:
            pop_url = page.evaluate(_JS_POP_URL)
        except Exception:  # noqa: BLE001
            pass

        # 1) 弹出标签正常加载
        if popup is not None and not (popup.url or "").startswith("chrome-error"):
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            _wait_render(popup)
            return popup

        # 2) 弹出页加载失败（chrome-error）或未弹出：用捕获的 URL 直连重试
        if pop_url:
            log(f"[traework] {name}: 弹出页加载失败，改用直连重试: {pop_url}")
            np = ctx.new_page()
            for i in range(3):
                try:
                    np.goto(pop_url, wait_until="domcontentloaded", timeout=25000)
                    if _wait_render(np):
                        return np
                except Exception as e:  # noqa: BLE001
                    log(f"[traework] {name}: 直连尝试 {i + 1}/3 失败: {e!r}")
                    time.sleep(5)
            try:
                np.close()
            except Exception:  # noqa: BLE001
                pass

        log(f"[traework] {name}: 积分页无法打开（积分页域名网络不可达），本轮放弃，下次计划任务会重试")
        return None
    except Exception as e:  # noqa: BLE001
        log(f"[traework] {name}: 打开积分面板异常: {e!r}")
        return None


DATE_TEXT_RE = re.compile(r"\d{4}[/.\-年]\d{1,2}[/.\-月]\d{1,2}")

# 「每日签到」行（折叠时只显示「共 N 笔」，需点击展开才见各笔到期日期）
_JS_MARK_SIGN_ROW = r"""
() => {
  let best = null;
  for (const el of document.querySelectorAll('div, span, button, a, li, p')) {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 40 || !/^每日签到/.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 30 || r.height < 12) continue;
    if (!best || r.width * r.height < best.w * best.h) best = { el, w: r.width, h: r.height };
  }
  if (!best) return false;
  best.el.setAttribute('data-cb-signrow', '1');
  return true;
}
"""


def _parse_cn_date(s):
    """解析 2026/10/12、2026-10-12、2026.10.12、2026年10月12日 等格式。"""
    try:
        p = [int(x) for x in re.split(r"[/.\-年月日\s]+", s.strip()) if x]
        if len(p) >= 3:
            return _date(p[0], p[1], p[2])
    except ValueError:
        pass
    return None


def _dashboard_already_checked(dash_text, today):
    """明细中存在到期日恰好 == 今天+31 天的「签到奖励」条目 ⇒ 今天已签。

    严格取 31：昨天签的积分到期日是今天+30，容忍 30 会把昨天的积分
    误判为今日已签（已发生过的真实误判，勿再放宽）。
    2026-09 页面改版后每条明细变长（含积分类型前缀与发放时刻），
    旧的 240 字符区块截取会把最新一条（恰好是 +31 那条）截掉，
    因此改为直接以「签到奖励」为锚点，逐条取其后第一个日期判定。
    """
    if isinstance(today, str):
        today = _date.fromisoformat(today)
    for m in re.finditer(r"签到奖励", dash_text):
        tail = dash_text[m.end():m.end() + 60]
        dm = DATE_TEXT_RE.search(tail)
        if dm:
            d = _parse_cn_date(dm.group(0))
            if d is not None and (d - today).days == 31:
                return True
    return False


_SIGN_ENTRY_RE = re.compile(r"签到奖励")
_SIGN_COUNT_RE = re.compile(r"共\s*(\d+)\s*笔")
# 展开「每日签到」最多点几次。明细是**懒加载**的，点太早（行还没绑定处理器）
# 会变成空操作，所以必须"点了就校验、没展开就重点"，而不是点一次就傻等。
_MAX_EXPAND_CLICKS = 4


def _sign_entry_count(text):
    """明细里「签到奖励」的条数。**0 表示明细还没渲染出来，不等于今天没签到。**"""
    return len(_SIGN_ENTRY_RE.findall(text))


def _sign_progress(text):
    """折叠行里的「共 N 笔」（已签明细笔数）；读不到返回 None。

    这是**独立于明细渲染的旁证**：签到成功会让笔数 +1。
    实测 2026-09-17 签到时 `共 8 笔 1,200/1,200` → `共 9 笔 1,350/1,350`。
    当明细迟迟渲染不出来时，用它兜底（见 `_expand_sign_detail`）。
    """
    m = _SIGN_COUNT_RE.search(text)
    return int(m.group(1)) if m else None


def _expand_sign_detail(dash, log, name, shots_dir, tag, today,
                        count_before=None, expand_wait_s=30, verify_wait_s=8):
    """展开「每日签到」明细并核对今日是否已签，返回 (status, detail)。

    status ∈ {"ok", None}；返回 None 时 detail 是**具体原因**（便于事后定性）。

    为什么不能"点一下然后傻等 10 秒"
    ------------------------------
    明细是**懒加载**的：折叠态只有「每日签到 › 通用积分 · 共 N 笔」，各笔的
    到期日期要展开后才拉取渲染；而「每日签到」这一行的**文本在数据到达之前
    就已经存在** → 流程可能在行还没绑定点击处理器时就去点它，**点击变成空操作**，
    面板始终折叠，之后无论等多久都等不到明细。
    实测 2026-09-13 / 14 / 15 / 17 都栽在这里：同一天重试又能成功，且明细里
    确实有「今天+31」的条目 —— 说明**签到本身早就成功了，只是没被看见**。

    所以这里必须：
      ① 点完**校验真的展开了**（明细条目数 > 0），没展开就**重新点**；
      ② 无论成败都留下**展开后**的截图 —— 旧代码只在展开**之前**截图，
         事后根本无法定性（本次排查就卡在这上面）；
      ③ 明细实在出不来时，用「共 N 笔」是否 +1 做**兜底旁证**
         （见 `_sign_progress`），而不是直接判失败。

    判定的可信度是分层的（避免"看不见就当没发生"）：
      * 明细**渲染出来了** → 以它为准：有「今天+31」= 已签；没有 = **确实没签成**；
      * 明细**没渲染出来** → 才看笔数旁证：+1 = 已签；没变 = 无法确认（留待复核）。
    """
    clicked = 0
    text = ""

    # ---- 阶段 1：把明细点出来 ----
    deadline = time.time() + expand_wait_s
    while time.time() < deadline:
        text = _page_text(dash)
        if _sign_entry_count(text) > 0:
            break
        if _dashboard_already_checked(text, today):
            _shot(dash, shots_dir, f"{name}_{tag}_ok.png")
            return "ok", "今日已签到（签到明细确认已领取）"
        if clicked < _MAX_EXPAND_CLICKS:
            try:
                if dash.evaluate(_JS_MARK_SIGN_ROW):
                    dash.click("[data-cb-signrow]", timeout=4000)
                    clicked += 1
                    log(f"[traework] {name}: 展开「每日签到」明细核对（第 {clicked} 次）")
            except Exception:  # noqa: BLE001
                pass
            # 给展开动画 + 懒加载留时间，否则会把"正在加载"误判成"没展开"又点回去
            time.sleep(2.5)
        else:
            time.sleep(1.5)

    # ---- 阶段 2：明细已渲染 → 判日期（再留一点时间给迟到的那一条）----
    deadline = time.time() + verify_wait_s
    while True:
        text = _page_text(dash)
        if _dashboard_already_checked(text, today):
            _shot(dash, shots_dir, f"{name}_{tag}_ok.png")
            return "ok", "今日已签到（签到明细确认已领取）"
        if time.time() >= deadline:
            break
        time.sleep(1)

    n = _sign_entry_count(text)
    if n:
        # 明细渲染出来了 → 以它为准，没有「今天+31」就是真的没签成
        _shot(dash, shots_dir, f"{name}_{tag}_nohit.png")
        return None, f"签到明细已渲染（{n} 条）但没有今日签到条目"

    # 明细没渲染出来 → 看笔数旁证（+1 说明今天多了一条签到奖励）
    now = _sign_progress(text)
    if count_before is not None and now is not None and now > count_before:
        _shot(dash, shots_dir, f"{name}_{tag}_countup.png")
        return "ok", (f"今日已签到（明细未渲染，但已签笔数 {count_before}→{now} 增加）")

    _shot(dash, shots_dir, f"{name}_{tag}_norender.png")
    return None, (f"「每日签到」明细未渲染（已点击展开 {clicked} 次仍未出现）"
                  + (f"，笔数仍为 {now}" if now is not None else ""))


def _page_token(page):
    """从页面里取 Cloud-IDE-JWT：localStorage 优先，其次问 GetUserToken 接口。"""
    for js in (_JS_LOCAL_TOKEN, _JS_FETCH_TOKEN):
        try:
            tok = page.evaluate(js)
        except Exception:  # noqa: BLE001
            tok = None
        if tok and TOKEN_RE.match(str(tok)):
            return str(tok)
    return None


def _page_device_id(pages):
    """取设备标识（claim 需要）。页面 localStorage 优先，再退回 IDE 日志。"""
    for pg in pages:
        if pg is None:
            continue
        try:
            did = pg.evaluate(_JS_LOCAL_DID)
        except Exception:  # noqa: BLE001
            did = None
        if did and str(did).strip().isdigit():
            return str(did).strip()
    try:
        import tc_checkin
        return tc_checkin._device_id(lambda m: None)
    except Exception:  # noqa: BLE001
        return None


def _try_api_checkin(pages, log, name, tag):
    """用页面里的令牌走接口判定/领取，返回 (status, detail) 或 None。

    不可用（页面里没令牌、接口模块缺失、调用异常）时返回 None，
    由调用方回退到页面文本判据 —— **兜底路径必须保留**，否则一旦
    TraeWork 改了令牌的存放位置就会全盘失效。

    `record=True`：服务端认可这枚令牌时会把它回写进共享缓存，
    让 TraeCN 通道也能用上（见 `tc_checkin.record_token`）。
    """
    try:
        import tc_checkin
    except Exception:  # noqa: BLE001
        return None
    pages = [p for p in (pages or []) if p is not None]
    token = None
    for pg in pages:
        token = _page_token(pg)
        if token:
            break
    if not token:
        log(f"[traework] {name}: 页面里未找到可用令牌（{tag}），改用页面判据")
        return None
    did = _page_device_id(pages)
    try:
        st, detail = tc_checkin.checkin_by_token(token, did, log,
                                                 record=True, name=name)
    except Exception as e:  # noqa: BLE001
        log(f"[traework] {name}: 接口判定异常({e!r})，改用页面判据")
        return None
    log(f"[traework] {name}: 接口判定（{tag}，did={did}）→ {st}: {detail}")
    return st, detail


def _api_via_token_page(ctx, log, name, tag):
    """开一个轻量页面（www.trae.cn）取令牌走接口判定。

    为什么不复用积分面板：那条链路要先开头像菜单、再点积分按钮、再等新标签页，
    任何一步失败就没有令牌；而令牌只要求浏览器能加载一次 www.trae.cn。
    实测该源在 1 秒内就能读到 localStorage 里的令牌。
    """
    np = None
    try:
        np = ctx.new_page()
        try:
            np.goto(TOKEN_PAGE_URL, wait_until="domcontentloaded", timeout=25000)
        except Exception as e:  # noqa: BLE001
            log(f"[traework] {name}: 轻量令牌页加载异常({e!r})，继续等 localStorage")
        token = None
        for _ in range(15):
            token = _page_token(np)
            if token:
                break
            time.sleep(1)
        if not token:
            log(f"[traework] {name}: 轻量令牌页 15s 内未取到令牌（{tag}）")
            return None
        return _try_api_checkin([np], log, name, tag)
    except Exception as e:  # noqa: BLE001
        log(f"[traework] {name}: 轻量令牌页不可用({e!r})")
        return None
    finally:
        try:
            if np is not None:
                np.close()
        except Exception:  # noqa: BLE001
            pass


def _note_api_reason(reasons, api):
    """把一次接口判定的原因写进 `reasons`，并**顶掉过时的旧接口原因**。

    ★ 为什么必须顶掉：2026-09-20 实测首轮是 api0「令牌已被服务端吊销」，
      随后页面刷新出**新令牌**、api1/api2 得到「服务端限流」。若把 api0 的原因
      留在最终文案里，用户会看到一个**早已解决**的原因被当成结论 —— 完全无法定性。
      规则：接口原因只保留**最后一次**（最新事实）。
    """
    if not (api and api[1]):
        return reasons
    reasons[:] = [r for r in reasons if not r.startswith("接口判定：")]
    reasons.append(f"接口判定：{api[1]}")
    return reasons


def _final_verdict(busy, why):
    """页面判据也确认不了时的最终结论。

    ★ 接口说过 `busy`（服务端限流 9074 / HTTP 429）时必须是 **busy（待复核）**，
      不能报 `fail`：限流意味着「服务端没告诉我们结果」，而实际证据显示
      限流的那次 claim **往往已经受理**（2026-09-20：13:34 首轮报失败，
      13:38 复核即「今日已签到 +150」）。
      `busy` 同样**不计入成功**、补跑照常继续 —— 只是文案诚实了，
      用户不会以为白签一场。
    """
    if busy:
        return "busy", (f"未能确认签到结果：{why}。"
                        f"（接口返回服务端限流，本轮视为未完成，"
                        f"下一轮计划任务会自动复核）截图见 logs/screenshots")
    return "fail", (f"未能确认签到结果：{why}。"
                    f"（接口与页面两条判据都没能确认；下一轮计划任务会自动复核）"
                    f"截图见 logs/screenshots")


def _do_checkin(ctx, page, log, name, shots_dir, today):
    _shot(page, shots_dir, f"{name}_0_page.png")
    text = _page_text(page)
    if DONE_TEXT_RE.search(text):
        return "ok", "今日已签到（页面显示已签到）"

    # 路径 0（最优先）：接口判定。
    # 只要求能加载一次 www.trae.cn 拿到令牌，不依赖任何页面文本 ——
    # 这是 2026-09-18 定位到 traework「明细已渲染但没有今日条目」假失败后
    # 新增的主路径。接口说「已签」就是已签，说「没签」就顺手领了，
    # 不再有"看见了/没看见"的模糊地带。
    api = _api_via_token_page(ctx, log, name, "api0")
    if api and api[0] in ("ok", "skip"):
        _shot(page, shots_dir, f"{name}_api_ok.png")
        return api
    # ★ 接口说「限流」时**不能丢**：它意味着本轮没确认（既非成功也非失败）。
    #   页面判据也确认不了时以它为准 → 报 busy（待复核），而不是 fail。
    #   2026-09-20 实测：api1/api2 都报 busy，但旧代码只认 ok/skip，
    #   于是 busy 被页面兜底覆盖成 fail → 弹窗又说「TraeWork 失败」。
    busy = api if (api and api[0] == "busy") else None

    # 路径 1：主页直接找签到入口
    r = _try_click_sign(page, log, name, shots_dir, "home")
    if r:
        return ("ok", "签到成功（页面确认）") if r == "ok" else ("ok", "今日已签到（页面确认）")

    # 路径 2/3：头像菜单 -> 积分 dashboard（新标签页「用量管理」）。
    # 开机后机器忙、SPA 渲染慢，明细日期可能迟迟不出：整轮复核最多两遍（第二遍重开面板），
    # 避免把「数据没加载完」误判为「未找到签到入口」（2026-09-13 开机轮真实误报）。
    reasons = []
    # 路径 0 的失败原因（可能没有）。★ 后面每次接口判定都会**顶掉**它，
    # 保证最终文案里只留最新事实，不会把「旧令牌被吊销」当成结论。
    _note_api_reason(reasons, api)
    for attempt in range(2):
        if attempt:
            log(f"[traework] {name}: 面板数据可能未就绪，重开积分面板复检…")
            time.sleep(5)
        dash = _open_points_dashboard(ctx, page, log, name)
        if dash is None:
            reasons.append("积分面板打不开")
            continue
        try:
            _shot(dash, shots_dir, f"{name}_{attempt + 1}_dashboard.png")
            # ★ 面板页也在 www.trae.cn 源上，令牌同样能读到 —— 再试一次接口，
            #   这样即使路径 0 的轻量页当时取不到令牌，这里还有机会。
            api2 = _try_api_checkin([dash, page], log, name, f"api{attempt + 1}")
            if api2 and api2[0] in ("ok", "skip"):
                return api2
            if api2 and api2[0] == "busy":
                busy = api2
            _note_api_reason(reasons, api2)
            # dashboard 是重 SPA：等「签到」区块渲染出来再判定，最长 15s
            dash_text = ""
            for _ in range(15):
                dash_text = _page_text(dash)
                if "签到" in dash_text:
                    break
                time.sleep(1)

            # 路径 3a：整页文本里直接能判出「今天+31」的签到奖励
            if dash_text and _dashboard_already_checked(dash_text, today):
                return "ok", "今日已签到（积分面板显示今日签到奖励已领取）"

            # 路径 3b：展开「每日签到」明细核对。
            # **先做这个**：到期日规则是最可靠的判据（独立签到按钮 2026-09 已消失）。
            if "每日签到" in dash_text:
                # 记下展开前的「共 N 笔」，明细万一渲染不出来时可作旁证
                r, why = _expand_sign_detail(
                    dash, log, name, shots_dir, f"dash{attempt + 1}", today,
                    count_before=_sign_progress(dash_text))
                if r == "ok":
                    return r, why
                reasons.append(why)

            # 路径 3c：明细里若真有独立的签到/领取按钮，点它（兜底）
            r = _try_click_sign(dash, log, name, shots_dir, "dash")
            if r:
                return ("ok", "签到成功（页面确认）") if r == "ok" else ("ok", "今日已签到（页面确认）")
        finally:
            try:
                dash.close()
            except Exception:  # noqa: BLE001
                pass

    _shot(page, shots_dir, f"{name}_final.png")
    # 失败原因要**具体**：旧文案一律说「入口可能未开放或页面变化」，
    # 把「明细没渲染出来（时序问题，签到多半已经成功）」和「明细渲染了但没有
    # 今日条目（确实没签成）」混成一句，事后无法定性、也误导用户。
    why = "；".join(dict.fromkeys(reasons)) if reasons else "未找到签到入口"
    return _final_verdict(busy, why)


def _run_account(p, cfg, acc, log, shots_dir, today, login_wait_s=0,
                 lock_wait_s=0.0):
    """跑一个 TraeWork 账号。

    `lock_wait_s` > 0 → profile 锁被占时**等待**对方释放（手动优先签到用）；
    默认 0 = 与改动前一致（立刻放弃、记 busy）。
    """
    name = acc.get("name") or acc.get("profile") or "default"
    display = _display_name(cfg, acc)  # 展示名：真实昵称优先，登录成功后即时更新
    profile = _profile_dir(cfg, acc)
    os.makedirs(profile, exist_ok=True)
    opts = cfg.get("options", {})
    headless = bool(opts.get("headless", True))

    # ★ 同一 profile 只允许一个实例：并发会让第二个实例拿不到登录态，
    #   进而弹出有头浏览器干等 7 分钟然后失败（2026-09-17 实测）。
    lock = _acquire_profile_lock(profile, log, display, wait_s=lock_wait_s)
    if lock is None:
        # ★ 状态必须是独立的 "busy"，**不能复用 "skip"**：
        #   "skip" 在本项目里语义是「今日已完成」，汇总处按成功计、弹窗里
        #   显示「已签到」—— 复用它会把「本轮根本没签到」伪装成成功。
        #   与既有 "expired"（界面 amber、汇总不计成功）保持同一套口径。
        return "busy", ("该账号的浏览器 profile 正被另一个实例使用，本轮跳过"
                        "（并发使用会丢登录态）；下一轮计划任务会自动补跑")

    ctx = None
    try:
        ctx = p.chromium.launch_persistent_context(
            profile, headless=headless, locale="zh-CN",
            viewport={"width": 1280, "height": 800}, args=LAUNCH_ARGS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(3000)
        # 等待 SPA 渲染出确定登录态（unknown 视为未定，最长 15s）
        state_now = _wait_login_state(page, timeout_s=15)

        # 断网场景：页面根本没加载出来（about:blank），不能误判为未登录而弹登录窗
        if (state_now != "in" and page.url in ("about:blank", "chrome://new-tab-page/")
                and not net_util.network_ok(timeout_s=4)):
            return "fail", "网络不可用，页面未加载（并非登录失效）；网络恢复后自动补跑"

        if state_now != "in":
            if headless:
                ctx.close()
                log(f"[traework] {display}: 会话未登录（URL: {page.url}），改用有头浏览器等待手动登录…")
                ctx = p.chromium.launch_persistent_context(
                    profile, headless=False, locale="zh-CN",
                    viewport={"width": 1280, "height": 800}, args=LAUNCH_ARGS)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
                except Exception:  # noqa: BLE001
                    pass
            if not _wait_login_interactive(page, display, timeout_s=login_wait_s or 420, log=log):
                return "fail", "登录等待超时，请手动运行 python checkin.py login traework 完成登录"
            page.wait_for_timeout(5000)
            log(f"[traework] {display}: 登录完成，会话已保存到该账号独立 profile")

        # 抓取并缓存账号真实昵称（左下角用户栏），用于弹窗/记录展示
        try:
            nick = (page.evaluate(_JS_NICK) or "").strip()
            if nick and nick != display:
                _save_nick(cfg, acc, nick)
                log(f"[traework] {display}: 登录昵称为「{nick}」，后续展示名已同步")
                display = nick
            elif nick:
                _save_nick(cfg, acc, nick)
        except Exception:  # noqa: BLE001
            pass

        return _do_checkin(ctx, page, log, display, shots_dir, today)
    finally:
        # ctx 可能是 None（锁获取后、浏览器启动前就抛异常），必须判空再关
        try:
            if ctx is not None:
                ctx.close()
        except Exception:  # noqa: BLE001
            pass
        # ★ 锁必须在 finally 里放：任何 return（含 fail/skip）都要放锁，
        #   否则残留锁会让下一轮计划任务白等 30 分钟才被判定为超龄。
        _release_profile_lock(lock)


def run(log, state, cfg, today, force=False):
    from playwright.sync_api import sync_playwright

    tw_cfg = cfg.get("traework", {})
    accounts = tw_cfg.get("accounts", [])
    results = []
    if not accounts:
        log("[traework] config.json 中未配置账号")
        return results
    date_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "logs", "screenshots", today)

    # 无账号需要处理时不必启动浏览器引擎
    if not any(force or state.get(f"traework:{a.get('name') or a.get('profile') or 'default'}") != today
               for a in accounts):
        log("[traework] 所有账号今日已完成，跳过")
        for a in accounts:
            results.append(dict(ts=time.strftime("%Y-%m-%dT%H:%M:%S"), platform="traework",
                                account=_display_name(cfg, a), status="skip", detail="今日已完成"))
        return results

    with sync_playwright() as p:
        for acc in accounts:
            name = acc.get("name") or acc.get("profile") or "default"
            display = _display_name(cfg, acc)
            key = f"traework:{name}"
            if not force and state.get(key) == today:
                log(f"[traework] {display}: 今日已完成，跳过")
                results.append(dict(ts=time.strftime("%Y-%m-%dT%H:%M:%S"), platform="traework",
                                    account=display, status="skip", detail="今日已完成"))
                continue
            try:
                status, detail = _run_account(p, cfg, acc, log, date_dir, today)
            except Exception as e:  # noqa: BLE001
                status, detail = "fail", f"异常: {e!r}"
            log(f"[traework] {display}: {detail}")
            # 接口确认「今日已签到」同样写入日期记录：否则每轮补跑都要重开
            # 一次浏览器去复核，白白多花 ~30s（与 tc_checkin.run 同一口径）。
            if status == "ok" or (status == "skip" and detail.startswith("今日已签到")):
                state[key] = today
            results.append(dict(ts=time.strftime("%Y-%m-%dT%H:%M:%S"), platform="traework",
                                account=display, status=status, detail=detail))
    return results


def interactive_login(cfg, acc, log):
    """弹出浏览器供用户手动登录指定账号，登录态存入该账号独立 profile。"""
    from playwright.sync_api import sync_playwright
    name = acc.get("name") or acc.get("profile") or "default"
    display = _display_name(cfg, acc)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            _profile_dir(cfg, acc), headless=False, locale="zh-CN",
            viewport={"width": 1280, "height": 800}, args=LAUNCH_ARGS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:  # noqa: BLE001
            pass
        # 先等页面渲染出确定登录态（最长 20s），避免把“未渲染完”误判成已登录
        if _wait_login_state(page, timeout_s=20) == "in":
            log(f"[traework] {display}: 该账号已登录，无需重新登录")
        else:
            ok = _wait_login_interactive(page, display, timeout_s=600, log=log)
            if ok:
                page.wait_for_timeout(3000)
                log(f"[traework] {display}: 登录成功并已保存")
            else:
                log(f"[traework] {display}: 登录等待超时，可重新运行本命令再试")
        # 抓取并缓存账号真实昵称
        try:
            nick = (page.evaluate(_JS_NICK) or "").strip()
            if nick:
                _save_nick(cfg, acc, nick)
                display = nick
                log(f"[traework] {display}: 当前登录昵称已缓存")
        except Exception:  # noqa: BLE001
            pass
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
