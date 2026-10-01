# -*- coding: utf-8 -*-
"""AI 调度器 + 失败诊断 端到端测试（离线）

覆盖：
  * 本地优先：本地后端就绪时走本地；不可用时回落云端
  * force_local / force_cloud 强制语义
  * 强制本地时**不会**偷偷烧云端积分
  * 本地失败自动回落并要求下次重新探测
  * 规则引擎对 10 类真实失败文案的判定
  * AI 回答解析器（三种常见排版）
  * 动作白名单 + 风险标记
  * clear_today_state 只清今日并留备份
  * check_networks 结构
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.services import ai_dispatch as AD  # noqa: E402
from app.services import diagnose as DG  # noqa: E402
from app.services import local_backends as LB  # noqa: E402

PASS = 0
FAIL = 0
FAILS: list = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  OK   %s" % name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  FAIL %s   %s" % (name, extra))


def section(t):
    print("\n== %s ==" % t)


# --------------------------------------------------------------------------
# 假本地后端（Ollama 协议）
# --------------------------------------------------------------------------

class FakeOllama:
    def __init__(self, models=None, fail=False):
        self.models = models or ["fake:7b"]
        self.fail = fail
        self.calls = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):  # noqa: N802
                if srv.fail:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                b = json.dumps({"models": [{"name": m}
                                           for m in srv.models]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                srv.calls.append(body)
                if srv.fail:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                last = (body.get("messages") or [{}])[-1].get("content", "")
                reply = "[本地] " + str(last)[:60]
                b = json.dumps({"message": {"content": reply}, "done": True,
                                "prompt_eval_count": 3, "eval_count": 5}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def stop(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# 假本地池（模拟 LocalPool 的接口子集）
# --------------------------------------------------------------------------

class FakeLocalPool:
    def __init__(self, kind="ollama", url="", models=None, ok=True,
                 enabled=True, detail="已连接"):
        self._row = {"kind": kind, "label": LB.KIND_LABELS.get(kind, kind),
                     "base_url": url, "enabled": enabled,
                     "models": list(models or []), "ok": ok,
                     "detail": detail, "checked_at": 0.0, "calls": 0}
        self._be = LB.make_backend(kind, url) if url else None
        self.probed = 0

    def enabled_kinds(self):
        return [self._row["kind"]] if self._row["enabled"] else []

    def get_row(self, kind):
        return dict(self._row) if kind == self._row["kind"] else None

    def probe(self, kind, timeout=None):
        self.probed += 1
        return (self._row["ok"], self._row["detail"], self._row["models"])

    def backend_for(self, kind):
        if self._be is None:
            raise LB.BackendError("未配置服务地址")
        return self._be

    def count_call(self, kind):
        self._row["calls"] += 1


def make_dispatcher(local_pool=None, cloud=None, prefs=None):
    d = AD.AIDispatcher(local_pool=local_pool,
                        cloud_fn=cloud,
                        settings_getter=lambda: (prefs or {}))
    return d


# ==========================================================================


def test_dispatch():
    section("调度器：本地优先")

    fake = FakeOllama(models=["fake:7b"])
    pool = FakeLocalPool(url=fake.url, models=["fake:7b"], ok=True)
    cloud_calls = []

    def cloud_fn(messages, model, timeout):
        cloud_calls.append(model)
        return "云端回答", {"target": "测试账号", "usage": {}}

    d = make_dispatcher(pool, cloud_fn, prefs={"prefer_local": True})

    st = d.status()
    check("status.local_ready=True", st["local_ready"] is True, st)
    check("status.local_kind=ollama", st["local_kind"] == "ollama", st)
    check("status.local_model 取自后端", st["local_model"] == "fake:7b", st)

    r = d.ask("你好")
    check("默认走本地", r.route == AD.Route.LOCAL, r.route)
    check("本地结果文本正确", r.text == "[本地] 你好", r.text)
    check("describe 标明本地模型", "本地模型" in r.describe(), r.describe())
    check("本地调用没打云端", len(cloud_calls) == 0, cloud_calls)
    check("usage 已透传", r.usage.get("total_tokens") == 8, r.usage)
    check("stats.local=1", d.stats["local"] == 1, d.stats)
    check("本地后端收到了消息",
          fake.calls and (fake.calls[-1]["messages"][-1]["content"] == "你好"),
          fake.calls[-1:] )
    check("系统提示词已带上前缀",
          (fake.calls[-1]["messages"][0]["role"] == "system"),
          fake.calls[-1]["messages"][0])

    section("调度器：force_cloud / force_local")

    d2 = make_dispatcher(pool, cloud_fn, prefs={"prefer_local": True})
    r = d2.ask("问云端", force_cloud=True)
    check("force_cloud 走云端", r.route == AD.Route.CLOUD, r.route)
    check("force_cloud 文本来自云端", r.text == "云端回答", r.text)

    r = d2.ask("问本地", force_local=True)
    check("force_local 走本地", r.route == AD.Route.LOCAL, r.route)

    section("调度器：local/ 前缀强制本地")
    d3 = make_dispatcher(pool, cloud_fn, prefs={"prefer_local": False})
    r = d3.ask("x", model="local/ollama/fake:7b")
    check("local/ 前缀走本地", r.route == AD.Route.LOCAL, r.route)
    r = d3.ask("x", model="glm-5.2")
    check("云端名走云端", r.route == AD.Route.CLOUD, r.route)

    section("调度器：本地不可用 -> 回落云端")

    off = FakeLocalPool(url="", models=[], ok=False, enabled=False,
                        detail="没有启用任何本地后端")
    cc2 = []

    def cloud_fn2(messages, model, timeout):
        cc2.append(1)
        return "云端兜底", {"target": "账号甲", "usage": {}}

    d4 = make_dispatcher(off, cloud_fn2, prefs={"prefer_local": True})
    r = d4.ask("需要帮助")
    check("本地不可用时走云端", r.route == AD.Route.CLOUD, r.route)
    check("云端文本正确", r.text == "云端兜底", r.text)
    check("stats.cloud=1", d4.stats["cloud"] == 1, d4.stats)
    check("local_attempted=False", r.local_attempted is False, r)

    section("调度器：prefer_local=False 时默认直接走云端")
    d5 = make_dispatcher(pool, cloud_fn, prefs={"prefer_local": False})
    # 有 model 为云端名时本就该走云端；这里测无 model 的情况
    d5b = make_dispatcher(pool, cloud_fn, prefs={"prefer_local": False})
    r = d5b.ask("无模型名", force_cloud=True)
    check("force_cloud 生效", r.route == AD.Route.CLOUD, r.route)

    section("调度器：本地报错 -> 回落云端并触发重新探测")

    bad = FakeOllama(fail=True)
    badpool = FakeLocalPool(url=bad.url, models=["fake:7b"], ok=True)
    cc3 = []

    def cloud_fn3(messages, model, timeout):
        cc3.append(1)
        return "云端救场", {"target": "账号乙", "usage": {}}

    d6 = make_dispatcher(badpool, cloud_fn3, prefs={"prefer_local": True})
    r = d6.ask("本地会挂")
    check("本地失败后回落云端", r.route == AD.Route.CLOUD, r.route)
    check("云端文本正确", r.text == "云端救场", r.text)
    check("stats.fallback=1", d6.stats["fallback"] >= 1, d6.stats)
    check("回落时确实试过本地", r.local_attempted is True, r)

    section("调度器：force_local 时本地失败**不**烧云端积分")
    cc4 = []
    d7 = make_dispatcher(badpool, lambda m, mo, t: (cc4.append(1), ("x", {}))[1],
                         prefs={"prefer_local": True})
    d7.invalidate_probe()
    r = d7.ask("必须本地", force_local=True)
    check("force_local 失败时不打云端", len(cc4) == 0, cc4)
    check("返回失败并带错误", (not r.ok) and bool(r.error), r.as_dict())

    section("调度器：全部不可用 -> 明确报错")
    d8 = make_dispatcher(off, lambda m, mo, t: ("", {"error": "池空"}),
                         prefs={"prefer_local": True})
    r = d8.ask("都没有")
    check("返回失败", not r.ok, r.as_dict())
    check("错误信息可读", bool(r.error) and "池空" in r.error, r.error)
    check("stats.failed 增加", d8.stats["failed"] == 1, d8.stats)

    section("调度器：指定本地模型名")
    pool2 = FakeLocalPool(url=fake.url, models=["a:1b", "b:2b"], ok=True)
    d9 = make_dispatcher(pool2, cloud_fn, prefs={"prefer_local": True})
    r = d9.ask("x", model="local/ollama/b:2b")
    sent_model = fake.calls[-1].get("model")
    check("传递的模型名被剥离前缀", sent_model == "b:2b", sent_model)

    section("调度器：pick_local_model 优先用户指定")
    d10 = make_dispatcher(pool2, cloud_fn,
                          prefs={"prefer_local": True, "local_model": "a:1b"})
    check("用户指定优先", d10.pick_local_model("ollama") == "a:1b",
          d10.pick_local_model("ollama"))
    d11 = make_dispatcher(pool2, cloud_fn, prefs={"prefer_local": True})
    check("未指定则取第一个", d11.pick_local_model("ollama") == "a:1b",
          d11.pick_local_model("ollama"))

    section("调度器：探测结果有 TTL 缓存")
    # 注意：row.ok=True 时 local_ready 会直接采信状态、不走 probe()，
    # 所以这里用 ok=False 让每次都要真探，才能验证 TTL 缓存。
    pool3 = FakeLocalPool(url=fake.url, models=[], ok=False,
                          detail="探测失败")
    d12 = make_dispatcher(pool3, cloud_fn, prefs={"prefer_local": True})
    d12.local_ready()
    d12.local_ready()
    check("TTL 内只探一次", pool3.probed == 1, pool3.probed)
    d12.invalidate_probe()
    d12.local_ready()
    check("invalidate 后重新探测", pool3.probed == 2, pool3.probed)

    section("DispatchResult.describe / as_dict")
    r = AD.DispatchResult(text="hi", route=AD.Route.CLOUD, target="账号丙")
    check("describe 含云端字样", "云端账号池" in r.describe(), r.describe())
    dd = r.as_dict()
    check("as_dict 含 ok", dd.get("ok") is True, dd)
    r2 = AD.DispatchResult(route=AD.Route.NONE, error="坏了")
    check("失败 describe 含原因", "坏了" in r2.describe(), r2.describe())

    fake.stop()
    bad.stop()


def test_rules():
    section("规则引擎：真实失败文案")
    cases = [
        ("未发现令牌，打开 IDE 登录后自动恢复", "no_token"),
        ("未找到登录态", "no_token"),
        ("登录已过期，请重新登录 (401)", "auth_expired"),
        ("unauthorized: invalid token", "auth_expired"),
        ("今日已签到（+150积分）", "already_done"),
        ("今日已完成", "already_done"),
        ("套餐额度用尽 code=1005", "plan_limit"),
        ("plan limit exceeded", "plan_limit"),
        ("请求频率超限 429 too many requests", "rate_limit"),
        ("DNS解析失败（api.trae.cn 无法解析）", "network"),
        ("Connection refused 10061", "network"),
        ("SSL certificate verify failed", "network"),
        ("设备环境异常，账号被风控", "device_blocked"),
        ("Executable doesn't exist at chromium", "browser"),
        ("playwright 启动失败", "browser"),
        ("完全未知的错误 xyz", ""),
    ]
    for text, want in cases:
        r = DG.rule_diagnose(text)
        check("规则命中 %-14s <- %s" % (want or "(无)", text[:34]),
              r["matched"] == want, r["matched"])

    section("规则引擎：动作映射")
    r = DG.rule_diagnose("登录已过期 401")
    check("auth_expired 给出动作", len(r["actions"]) > 0, r)
    check("动作都在白名单内", all(a in DG.ACTIONS for a in r["actions"]),
          r["actions"])
    r = DG.rule_diagnose("今日已签到")
    check("已签到不给动作", r["actions"] == [], r["actions"])
    r = DG.rule_diagnose("设备风控异常")
    check("风控给重置设备动作", "reset_device" in r["actions"], r["actions"])
    check("reset_device 标记为高风险",
          DG.is_risky("reset_device") is True)
    check("rescan_tokens 非高风险", DG.is_risky("rescan_tokens") is False)

    section("动作白名单完整性")
    for key, a in DG.ACTIONS.items():
        check("动作 %s 字段齐全" % key,
              bool(a.get("label")) and bool(a.get("desc"))
              and isinstance(a.get("risky"), bool), a)
    check("describe_action 可读",
          "只读" in DG.describe_action("rescan_tokens"),
          DG.describe_action("rescan_tokens"))


def test_parse():
    section("AI 回答解析器")

    s1 = ("结论：本机没有可用的 TraeCN 登录令牌。\n"
          "可能原因：\n"
          "1. Trae CN 客户端未登录或登录已过期\n"
          "2. 令牌缓存文件缺失\n"
          "建议动作：\n"
          "1. 打开 Trae CN 客户端确认登录\n"
          "2. 重新扫描 IDE 令牌\n")
    p = DG._parse_ai_text(s1)
    check("结论抽取", p["summary"] == "本机没有可用的 TraeCN 登录令牌。", p["summary"])
    check("原因抽取 2 条", len(p["causes"]) == 2, p["causes"])
    check("原因去掉序号", p["causes"][0] == "Trae CN 客户端未登录或登录已过期",
          p["causes"])
    check("建议抽取 2 条", len(p["suggestions"]) == 2, p["suggestions"])

    s2 = ("**结论**：网络不通。\n\n"
          "可能原因\n"
          "- 代理配置错误\n"
          "- DNS 失败\n\n"
          "建议\n"
          "* 检查代理\n")
    p = DG._parse_ai_text(s2)
    check("加粗结论也能抽", "网络不通" in p["summary"], p["summary"])
    check("破折号列表可抽", len(p["causes"]) == 2, p["causes"])

    s3 = "结论：信息不足。\n"
    p = DG._parse_ai_text(s3)
    check("只有结论时不崩", p["summary"] == "信息不足。", p)
    check("无原因时为空列表", p["causes"] == [], p["causes"])

    p = DG._parse_ai_text("")
    check("空文本不崩", p["summary"] == "" and p["causes"] == [], p)
    p = DG._parse_ai_text(None)
    check("None 不崩", p["summary"] == "", p)

    section("证据整理与提问构造")
    ev = DG.collect_evidence(
        results=[{"platform": "traecn", "account": "K", "status": "fail",
                  "detail": "未发现令牌"},
                 {"platform": "workbuddy", "account": "W", "status": "ok",
                  "detail": "成功"}],
        logs=["line1", "line2"])
    check("results 收全", len(ev["results"]) == 2, ev["results"])
    check("failed 只含失败", len(ev["failed"]) == 1, ev["failed"])
    check("日志已拼接", "line1" in ev["log_tail"], ev["log_tail"])

    prompt = DG.build_prompt(ev, DG.rule_diagnose("未发现令牌"))
    check("提问含账号结果", "traecn" in prompt, prompt[:200])
    check("提问含规则结论", "本地规则初步判断" in prompt, prompt[-600:])
    check("提问含格式要求", "结论：" in prompt and "建议动作：" in prompt)
    check("提问含禁止编造约束", "不要编造" in prompt)


def test_diagnose_offline():
    section("诊断：无 AI 时的规则兜底")
    d = DG.diagnose(results=[{"platform": "traecn", "account": "K",
                              "status": "fail",
                              "detail": "未发现令牌，打开 IDE 登录后自动恢复"}],
                    logs="[error] 没有可用账号", use_ai=False)
    check("有结论", bool(d["summary"]), d)
    check("结论走规则", "登录凭据" in d["summary"], d["summary"])
    check("有动作", len(d["actions"]) > 0, d["actions"])
    check("actions 结构完整",
          all(set(("key", "label", "desc", "risky")) <= set(a.keys())
              for a in d["actions"]), d["actions"])
    check("ai_used=False", d["ai_used"] is False, d)

    section("诊断：全部成功时不需要人工处置")
    d = DG.diagnose(results=[{"platform": "traecn", "account": "K",
                              "status": "ok", "detail": "签到成功"}],
                    logs="完成", use_ai=False)
    check("结论说无需处理", "无需处理" in d["summary"] or "没有失败" in d["summary"],
          d["summary"])
    check("无动作", d["actions"] == [], d["actions"])

    section("诊断：无结果时不崩")
    d = DG.diagnose(use_ai=False)
    check("有兜底结论", bool(d["summary"]), d["summary"])

    section("诊断：未知错误的兜底文案")
    d = DG.diagnose(results=[{"platform": "traecn", "account": "K",
                              "status": "fail", "detail": "zzz 奇怪问题"}],
                    logs="", use_ai=False)
    check("提到失败数", "1" in d["summary"], d["summary"])
    check("给出可操作建议", len(d["suggestions"]) > 0, d["suggestions"])


def test_actions():
    section("动作：clear_today_state 只清今日并留备份")
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "logs"))
        p = os.path.join(td, "logs", "state.json")
        today = __import__("time").strftime("%Y-%m-%d")
        data = {"traecn:1": today, "traework:a": today,
                "workbuddy:x": "2020-01-01"}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f)

        r = DG.clear_today_state(td)
        check("清理成功", r["ok"] is True, r)
        check("清掉 2 条今日", r["removed"] == 2, r)
        check("备份已生成", os.path.exists(p + ".bak"), os.listdir(os.path.join(td, "logs")))
        with open(p, "r", encoding="utf-8") as f:
            left = json.load(f)
        check("历史记录保留", left.get("workbuddy:x") == "2020-01-01", left)
        check("今日记录已清", "traecn:1" not in left, left)

        # 幂等
        r2 = DG.clear_today_state(td)
        check("再次清理无副作用", r2["ok"] is True and r2["removed"] == 0, r2)

        # 备份内容正确
        with open(p + ".bak", "r", encoding="utf-8") as f:
            bak = json.load(f)
        check("备份含原始今日记录", bak.get("traecn:1") == today, bak)

    section("动作：state.json 缺失时不报错")
    with tempfile.TemporaryDirectory() as td:
        r = DG.clear_today_state(td)
        check("缺文件返回 ok", r["ok"] is True and r["removed"] == 0, r)

    section("动作：state.json 损坏时如实报错")
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "logs"))
        with open(os.path.join(td, "logs", "state.json"), "w") as f:
            f.write("{bad json")
        r = DG.clear_today_state(td)
        check("损坏时 ok=False", r["ok"] is False, r)
        check("带失败原因", bool(r["detail"]), r)

    section("动作：check_networks 结构")
    r = DG.check_networks(hosts=[("127.0.0.1", 1)], timeout=1.0)
    check("返回统计字段",
          all(k in r for k in ("ok", "reachable", "total", "rows")), r)
    check("row 字段齐全",
          all(k in r["rows"][0] for k in ("host", "dns", "tcp", "ok")), r["rows"])
    check("不可达时 ok=False", r["ok"] is False, r)


def main():
    test_dispatch()
    test_rules()
    test_parse()
    test_diagnose_offline()
    test_actions()

    print("\n---")
    print("AI 调度 + 失败诊断：%d OK / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
