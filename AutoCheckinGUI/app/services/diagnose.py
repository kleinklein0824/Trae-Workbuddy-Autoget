# -*- coding: utf-8 -*-
"""签到失败诊断与一键修复。

工作方式（用户确认过的口径）
--------------------------
1. 把这次签到的**真实证据**（每个账号的状态/详情 + 运行日志尾部）整理成
   结构化材料，交给本地模型优先的 AI 调度器分析。
2. AI 给出：**结论**、**可能原因**、**建议动作**。
3. 建议动作里凡是本项目能安全执行的，附上 `action` 标识；UI 显示「执行」
   按钮，**必须用户点确认后才真的执行**。
4. 本项目**绝不静默修改系统**。

设计要点
--------
* **规则先行**：先用本地规则（不依赖 AI）判断出高置信度结论与候选动作。
  这样即使没有可用模型，用户也能拿到准确的处置建议，而不是一片空白。
* **AI 补充**：把规则结论 + 原始证据一起给模型，让它给更细的解释
  （比如「令牌过期」要提醒重新登录 IDE）。
* **动作白名单**：只有白名单里的动作能被执行，且都是本项目已实现、
  可回滚或只读的操作。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# 可执行动作白名单
#
#   key       : 动作标识（UI 通过它回调 app 层执行）
#   label     : 按钮文案
#   desc      : 说明（告诉用户到底会做什么）
#   risky     : 是否会改动本机/磁盘（高风险动作要二次确认）
# --------------------------------------------------------------------------

ACTIONS: Dict[str, Dict[str, Any]] = {
    "rescan_tokens": {
        "label": "重新扫描 IDE 令牌",
        "desc": "重新读取本机 Trae 客户端的登录令牌（只读，不改任何文件）",
        "risky": False,
    },
    "open_client": {
        "label": "打开 Trae 客户端",
        "desc": "启动 Trae CN / TRAE SOLO CN，便于你确认登录状态",
        "risky": False,
    },
    "retry_checkin": {
        "label": "重试签到",
        "desc": "用「强制执行」重跑一次签到（忽略今日已完成状态）",
        "risky": False,
    },
    "reset_device": {
        "label": "重置设备标识",
        "desc": "重置 6 层设备指纹（machineid / telemetry / 注册表等），"
                "执行前会自动备份到 backups 目录",
        "risky": True,
    },
    "clear_state": {
        "label": "清除今日签到状态",
        "desc": "清掉 logs/state.json 里今日的记录，让签到重新执行一遍",
        "risky": True,
    },
    "open_logs": {
        "label": "打开日志目录",
        "desc": "打开 logs 目录，便于查看完整运行日志与截图",
        "risky": False,
    },
    "rescan_accounts": {
        "label": "重新发现账号",
        "desc": "重扫三个平台的本机登录态并刷新账号列表（只读）",
        "risky": False,
    },
    "check_network": {
        "label": "检测网络连通性",
        "desc": "测试签到接口与上游域名能否正常解析和访问",
        "risky": False,
    },
    "enable_local_model": {
        "label": "检查本地模型",
        "desc": "探测 Ollama / llama.cpp 是否在线，并列出可用模型",
        "risky": False,
    },
}


# --------------------------------------------------------------------------
# 规则引擎
# --------------------------------------------------------------------------

#: (正则, 结论, 原因列表, 动作列表) —— 按顺序匹配，先命中先返回
_RULES: List[Dict[str, Any]] = [
    {
        "name": "no_token",
        "pattern": r"未发现令牌|未找到登录态|没有可用账号|未发现.*令牌",
        "summary": "本机没有可用的登录凭据，签到无法进行。",
        "causes": [
            "客户端未登录，或登录已过期",
            "令牌加密存储后日志里不再出现明文 JWT",
            "客户端换了数据目录，扫描路径没覆盖到",
        ],
        "actions": ["open_client", "rescan_tokens", "rescan_accounts"],
    },
    {
        "name": "auth_expired",
        "pattern": r"401|403|登录过期|token.*(失效|过期)|unauthorized|invalid.*token",
        "summary": "登录状态已失效，服务端拒绝了请求。",
        "causes": [
            "令牌过期（Trae 令牌有效期较短，需客户端保活）",
            "账号在别处退出了登录",
            "本地缓存的令牌比真实登录态旧",
        ],
        "actions": ["open_client", "rescan_tokens", "retry_checkin"],
    },
    {
        "name": "already_done",
        "pattern": r"今日已签到|今日已完成|已领取",
        "summary": "今天已经签到过了，属正常状态，无需处理。",
        "causes": ["今天已经成功签到", "计划任务已经跑过一轮"],
        "actions": [],
    },
    {
        "name": "plan_limit",
        "pattern": r"1005|plan.*limit|套餐|额度.*(用尽|耗尽)|超出套餐",
        "summary": "账号套餐额度受限，服务端拒绝发放。",
        "causes": [
            "该账号今日/本周期额度已用完",
            "账号套餐等级不支持该签到",
        ],
        "actions": ["rescan_accounts"],
    },
    {
        "name": "rate_limit",
        "pattern": r"429|too many|频率|限流|请稍后",
        "summary": "请求被限流，需要稍后重试。",
        "causes": ["短时间请求过多", "同一账号并发登录"],
        "actions": ["retry_checkin"],
    },
    {
        "name": "network",
        "pattern": r"DNS|解析失败|超时|timed out|timeout|无法连接|Connection|"
                   r"10061|refused|SSL|证书|网络",
        "summary": "网络连接异常，请求没能到达服务端。",
        "causes": [
            "本机网络不通或代理配置异常",
            "DNS 解析失败",
            "代理端口未启动（本机代理软件退出后残留配置）",
            "目标域名临时不可达",
        ],
        "actions": ["check_network", "retry_checkin"],
    },
    {
        "name": "device_blocked",
        "pattern": r"设备|device.*(封禁|限制|异常)|风控|risk|异常环境",
        "summary": "设备指纹被风控识别，账号被判定为异常环境。",
        "causes": [
            "同一设备上多账号操作触发风控",
            "设备标识被服务端标记",
        ],
        "actions": ["reset_device", "retry_checkin"],
    },
    {
        "name": "browser",
        "pattern": r"playwright|浏览器|browser|chromium|Executable doesn't exist|"
                   r"driver|headless|Target closed|net::ERR",
        "summary": "浏览器自动化（TraeWork 通道）执行失败。",
        "causes": [
            "Playwright 浏览器内核未安装或损坏",
            "本机缺少必要的运行库",
            "无头模式被目标站点拦截",
        ],
        "actions": ["retry_checkin", "open_logs"],
    },
    {
        "name": "timeout",
        "pattern": r"脚本.*超时|执行超时|长时间无响应|hung",
        "summary": "签到流程卡住超时。",
        "causes": ["页面元素改版导致 selector 失效", "网络慢导致等待超时"],
        "actions": ["retry_checkin", "open_logs"],
    },
]


def rule_diagnose(text: str) -> Dict[str, Any]:
    """不依赖 AI 的规则诊断。返回 {matched, summary, causes, actions}。"""
    low = text or ""
    for rule in _RULES:
        m = re.search(rule["pattern"], low, re.IGNORECASE)
        if m:
            return {
                "matched": rule["name"],
                "summary": rule["summary"],
                "causes": list(rule["causes"]),
                "actions": [a for a in rule["actions"] if a in ACTIONS],
                "evidence": m.group(0)[:120],
            }
    return {"matched": "", "summary": "", "causes": [], "actions": [],
            "evidence": ""}


# --------------------------------------------------------------------------
# 证据收集
# --------------------------------------------------------------------------

def _tail(path: str, max_lines: int = 60) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except OSError:
        return ""


def collect_evidence(results=None, logs=None, root=None) -> Dict[str, Any]:
    """整理诊断材料。

    results : CheckinRunner 的 results 列表（每个账号一条）
    logs    : UI 里的运行日志文本（字符串或行列表）
    root    : 项目根（找 logs 目录用）
    """
    ev: Dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "results": [],
        "failed": [],
        "log_tail": "",
        "log_file": "",
    }

    for r in (results or []):
        if not isinstance(r, dict):
            continue
        row = {
            "platform": r.get("platform") or "",
            "account": r.get("account") or "",
            "status": r.get("status") or "",
            "detail": (r.get("detail") or "")[:400],
        }
        ev["results"].append(row)
        if row["status"] in ("fail", "error"):
            ev["failed"].append(row)

    # 运行日志尾部
    if logs:
        if isinstance(logs, (list, tuple)):
            text = "\n".join(str(x) for x in logs)
        else:
            text = str(logs)
        ev["log_tail"] = text[-6000:]

    # 落盘日志（UI 日志可能被截断，补一份文件尾部）
    if root:
        p = os.path.join(root, "logs",
                         "checkin_%s.log" % time.strftime("%Y-%m-%d"))
        if os.path.exists(p):
            ev["log_file"] = _tail(p, 80)

    return ev


def build_prompt(ev: Dict[str, Any], rule: Dict[str, Any]) -> str:
    """把证据组织成给模型的提问。"""
    lines: List[str] = []
    lines.append("下面是本次「Trae / WorkBuddy 每日签到」的执行结果，请帮我诊断。\n")

    lines.append("## 各账号执行结果")
    if ev.get("results"):
        for r in ev["results"]:
            lines.append("- [%s] %s：%s —— %s"
                         % (r.get("platform"), r.get("account"),
                            r.get("status"), r.get("detail")))
    else:
        lines.append("（这次没有拿到任何账号级结果）")
    lines.append("")

    if rule.get("summary"):
        lines.append("## 本地规则初步判断（供参考，可推翻）")
        lines.append("结论：%s" % rule["summary"])
        if rule.get("evidence"):
            lines.append("命中特征：%s" % rule["evidence"])
        lines.append("")

    if ev.get("log_tail"):
        lines.append("## 运行日志（尾部）")
        lines.append("```")
        lines.append(ev["log_tail"][-3500:])
        lines.append("```")
        lines.append("")

    if ev.get("log_file"):
        lines.append("## 落盘日志（尾部）")
        lines.append("```")
        lines.append(ev["log_file"][-2500:])
        lines.append("```")
        lines.append("")

    lines.append("## 请按以下格式回答（务必简洁，不要复述日志）")
    lines.append("结论：<一句话说明最可能的原因>")
    lines.append("可能原因：")
    lines.append("1. ...")
    lines.append("2. ...")
    lines.append("建议动作：")
    lines.append("1. ...")
    lines.append("2. ...")
    lines.append("")
    lines.append("注意：只依据上面的材料推断，**不要编造**不存在的接口、日志或错误码；"
                 "材料不足以判断时，请直接说「信息不足」，并指出还需要什么信息。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# AI 诊断
# --------------------------------------------------------------------------

_SYSTEM = (
    "你是一名自动化运维助手，专门诊断「Trae / WorkBuddy 每日签到」脚本的故障。"
    "要求：用简体中文；结论先行；只依据用户提供的材料推断，不要编造接口名或日志；"
    "材料不足时明确说「信息不足」；不要输出与诊断无关的客套话。"
)


def _parse_ai_text(text: str) -> Dict[str, Any]:
    """从模型回答里抽出结论 / 原因 / 动作三段。

    模型的排版很不固定 —— 可能用 `## ` 标题、`**加粗**`、也可能
    「可能原因」后面不跟冒号。所以这里先把标题行归一化再切段，
    而不是写一个严格的正则。
    """
    out = {"summary": "", "causes": [], "suggestions": [], "raw": text}
    if not text:
        return out

    lines = text.splitlines()

    # 归一化标题行：去掉 markdown 记号与序号，便于识别
    def _norm(line: str) -> str:
        s = line.strip()
        s = re.sub(r"^#{1,6}\s*", "", s)              # ## 标题
        # **结论**：xxx / **结论** xxx -> 结论：xxx / 结论 xxx
        s = re.sub(r"^\*\*(.+?)\*\*\s*", r"\1 ", s)
        s = s.strip()
        s = re.sub(r"^\d+[.、)]\s*", "", s)
        return s

    # 结论：找「结论」开头的行，取冒号后内容
    for line in lines:
        n = _norm(line)
        if re.match(r"^结论\s*[：:]", n):
            v = re.split(r"[：:]", n, 1)[-1].strip()
            if v:
                out["summary"] = v[:200]
                break

    HEADS = {
        "可能原因": "causes", "故障原因": "causes", "原因": "causes",
        "建议动作": "suggestions", "处理建议": "suggestions",
        "建议": "suggestions", "下一步": "suggestions",
        "结论": None, "说明": None, "分析": None,
    }

    cur = None
    for line in lines:
        n = _norm(line)
        # 是否是标题行？（归一化后以某个标题词开头，且整行不超过 12 字）
        head = None
        for key in HEADS:
            if n.startswith(key) and len(n) <= len(key) + 12:
                head = key
                break
        if head is not None:
            cur = HEADS[head]
            # 标题同行可能还有内容
            rest = re.split(r"[：:]", n, 1)[-1].strip() if "：" in n or ":" in n else ""
            rest = rest.replace(head, "", 1).strip() if rest == head else rest
            if cur and rest and rest != head:
                out[cur].append(rest[:200])
            continue
        if cur and n:
            s = re.sub(r"^[-*•]\s*", "", n)
            s = re.sub(r"^\d+[.、)]\s*", "", s).strip()
            if s:
                out[cur].append(s[:200])

    out["causes"] = out["causes"][:6]
    out["suggestions"] = out["suggestions"][:6]
    return out


def diagnose(results=None, logs=None, root=None, use_ai=True,
             timeout=120.0) -> Dict[str, Any]:
    """完整诊断：规则 + AI。

    返回：
      {
        "ok": bool,
        "summary": str,            # 结论
        "causes": [str, ...],
        "suggestions": [str, ...],
        "actions": [{key,label,desc,risky}, ...],   # 可一键执行的动作
        "rule": {...},             # 规则结果
        "ai": {...} or None,       # AI 结果（含 route/target/model）
        "evidence": {...},
      }
    """
    ev = collect_evidence(results=results, logs=logs, root=root)
    # 规则判断用的文本 = 所有失败详情 + 日志
    blob_parts = []
    for r in ev.get("failed") or ev.get("results") or []:
        blob_parts.append("%s %s %s" % (r.get("platform"), r.get("account"),
                                        r.get("detail")))
    blob_parts.append(ev.get("log_tail") or "")
    blob_parts.append(ev.get("log_file") or "")
    blob = "\n".join(blob_parts)

    rule = rule_diagnose(blob)
    out: Dict[str, Any] = {
        "ok": True,
        "summary": rule.get("summary") or "",
        "causes": list(rule.get("causes") or []),
        "suggestions": [],
        "actions": [dict(key=k, **ACTIONS[k]) for k in rule.get("actions") or []],
        "rule": rule,
        "ai": None,
        "evidence": ev,
        "ai_used": False,
    }

    if not use_ai:
        if not out["summary"]:
            out["summary"] = _fallback_summary(ev)
        out["suggestions"] = _fallback_suggestions(ev, rule)
        return out

    # ---- AI 补充 ----
    try:
        from . import ai_dispatch as AD
    except Exception:  # noqa: BLE001
        out["summary"] = out["summary"] or _fallback_summary(ev)
        out["suggestions"] = _fallback_suggestions(ev, rule)
        return out

    if not ev.get("failed") and not ev.get("log_tail"):
        # 没有任何失败证据 -> 不必打扰模型
        out["summary"] = out["summary"] or _fallback_summary(ev)
        out["suggestions"] = _fallback_suggestions(ev, rule)
        return out

    res = AD.dispatcher().ask(build_prompt(ev, rule), system=_SYSTEM,
                              timeout=timeout)
    out["ai"] = res.as_dict()
    out["ai_used"] = res.ok
    if res.ok:
        parsed = _parse_ai_text(res.text)
        # AI 结论优先；AI 没给就沿用规则结论
        if parsed["summary"]:
            out["summary"] = parsed["summary"]
        if parsed["causes"]:
            out["causes"] = parsed["causes"]
        out["suggestions"] = parsed["suggestions"]
        out["ai_text"] = parsed["raw"]
    else:
        out["ai_error"] = res.error

    if not out["summary"]:
        out["summary"] = _fallback_summary(ev)
    if not out["suggestions"]:
        out["suggestions"] = _fallback_suggestions(ev, rule)
    return out


def _fallback_summary(ev: Dict[str, Any]) -> str:
    failed = ev.get("failed") or []
    if not failed:
        if ev.get("results"):
            return "本次签到没有失败项，无需处理。"
        return "本次没有拿到执行结果，可能是还没跑过签到。"
    plats = sorted({r.get("platform") or "?" for r in failed})
    return ("有 %d 个账号签到失败（%s），但暂未匹配到已知的故障特征，"
            "建议先看日志详情。" % (len(failed), "、".join(plats)))


def _fallback_suggestions(ev: Dict[str, Any], rule: Dict[str, Any]) -> List[str]:
    if not (ev.get("failed") or []):
        return ["无需处理。若想重新签到，可用「强制执行」再跑一次。"]
    out = []
    if rule.get("summary"):
        out.append("按上述结论处理；关键详情见「运行日志」。")
    out.append("先点「重新扫描 IDE 令牌」确认本机登录态是最新的。")
    out.append("再点「重试签到」复跑一次；若仍失败，请查看 logs 目录里的完整日志。")
    if any((r.get("detail") or "").lower().find("browser") >= 0
           or "playwright" in (r.get("detail") or "").lower()
           for r in ev.get("failed") or []):
        out.append("浏览器通道失败时，可在设置页检查 Playwright 依赖是否完整。")
    return out[:5]


# --------------------------------------------------------------------------
# 动作执行（仅供 app 层按 key 调用；这里只做「安全」的实现）
# --------------------------------------------------------------------------

def describe_action(key: str) -> str:
    a = ACTIONS.get(key)
    return a["desc"] if a else ""


def is_risky(key: str) -> bool:
    a = ACTIONS.get(key)
    return bool(a and a.get("risky"))


def clear_today_state(root: str, platforms=None) -> Dict[str, Any]:
    """清除 logs/state.json 里**今日**的记录，返回清理摘要。

    这是「clear_state」动作的实现：只删今日的键，不动历史，
    并且先备份原文件。
    """
    path = os.path.join(root, "logs", "state.json")
    if not os.path.exists(path):
        return {"ok": True, "removed": 0, "detail": "没有 state.json，无需清理"}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return {"ok": False, "removed": 0, "detail": "读取失败：%s" % e}
    if not isinstance(data, dict):
        return {"ok": False, "removed": 0, "detail": "state.json 格式异常"}

    today = time.strftime("%Y-%m-%d")
    removed = [k for k, v in data.items() if v == today]
    if not removed:
        return {"ok": True, "removed": 0, "detail": "今日没有需要清理的记录"}

    # 先备份
    try:
        bak = path + ".bak"
        with open(bak, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        bak = ""

    for k in removed:
        data.pop(k, None)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        return {"ok": False, "removed": 0, "detail": "写入失败：%s" % e}

    return {"ok": True, "removed": len(removed),
            "detail": "已清除 %d 条今日记录%s"
                      % (len(removed), ("（备份：%s）" % os.path.basename(bak))
                         if bak else "")}


def check_networks(hosts=None, timeout=6.0) -> Dict[str, Any]:
    """检测关键域名的 DNS 与连通性（只读，不发业务请求）。"""
    import socket
    targets = hosts or [
        ("api.trae.cn", 443),
        ("trae-api-cn.mchost.guru", 443),
        ("copilot.tencent.com", 443),
    ]
    rows = []
    for host, port in targets:
        row = {"host": host, "port": port, "dns": "", "tcp": "", "ok": False}
        try:
            ip = socket.gethostbyname(host)
            row["dns"] = ip
        except OSError as e:
            row["dns"] = "解析失败: %s" % e
            rows.append(row)
            continue
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            row["tcp"] = "已连通"
            row["ok"] = True
        except OSError as e:
            row["tcp"] = "连接失败: %s" % e
        rows.append(row)
    ok = sum(1 for r in rows if r["ok"])
    return {"ok": ok == len(rows), "reachable": ok, "total": len(rows),
            "rows": rows}


__all__ = ["ACTIONS", "build_prompt", "check_networks", "clear_today_state",
           "collect_evidence", "describe_action", "diagnose", "is_risky",
           "rule_diagnose"]
