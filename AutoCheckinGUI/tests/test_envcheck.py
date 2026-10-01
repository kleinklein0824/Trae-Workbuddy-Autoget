#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境自检回归测试（``app/services/envcheck.py``）。

背景
----
用户要求「彻底检查 exe 所有功能是否可用落地」。本程序的故障绝大多数是
**静默失效**（路径没解析出来 → 0 个账号；浏览器没装 → 一句看不懂的报错；
客户端装在 D 盘 → 永远找不到），所以自检的价值全在两点：

  1. **每条检查都必须独立** —— 一条炸了不能吞掉整份报告，
     否则用户看到的是「自检失败」，真正的原因被盖住了。
  2. **报 FAIL/WARN 必须给 hint** —— 只说「不可用」不说「怎么办」等于没报。

这两条是本测试的重点，其余是结构与一致性检查。

★ 本测试只读：唯一的写操作是自检自己在数据目录建/删一个临时文件，
  这里额外断言它没有留下残留。
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

from app import config as C                    # noqa: E402
from app.services import envcheck as EC        # noqa: E402

OK = FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {extra}")


VALID = {EC.OK, EC.WARN, EC.FAIL, EC.SKIP}

# ============================================================ 1. 单项检查
print("== 1. 每条检查独立可调用、返回结构合法 ==")
for fn in EC._CHECKS:
    name = getattr(fn, "__name__", "<lambda>")
    try:
        it = fn()
        raised = None
    except Exception as e:  # noqa: BLE001
        it, raised = None, e
    check(f"{name} 不抛异常", raised is None, repr(raised))
    if it is None:
        continue
    check(f"{name} 含 key/label/status/detail/hint",
          all(k in it for k in ("key", "label", "status", "detail", "hint")),
          it)
    check(f"{name} status 合法", it["status"] in VALID, it["status"])
    check(f"{name} label 非空", bool(it["label"]))
    # ★ 报 FAIL/WARN 必须给 hint —— 只说「不可用」不说「怎么办」等于没报
    if it["status"] in (EC.FAIL, EC.WARN):
        check(f"{name} 报 {it['status']} 时给出了修复建议",
              bool(it["hint"]), f"label={it['label']} detail={it['detail']}")
    if it["status"] == EC.OK:
        check(f"{name} OK 时带 detail 说明现状", bool(it["detail"]), it)

# ============================================================ 2. run()
print()
print("== 2. run() 整体行为 ==")
items = EC.run()
check("返回列表", isinstance(items, list))
check("条目数与检查项数一致", len(items) == len(EC._CHECKS),
      f"{len(items)} vs {len(EC._CHECKS)}")
check("每条 status 都合法", all(i["status"] in VALID for i in items),
      [i["status"] for i in items])
check("key 无重复", len({i["key"] for i in items}) == len(items),
      [i["key"] for i in items])

counts = EC.summary(items)
check("summary 各项之和 == 总数", sum(counts.values()) == len(items), counts)
check("summary 覆盖四种状态",
      set(counts) == {EC.OK, EC.WARN, EC.FAIL, EC.SKIP}, counts)

verdict, tone = EC.overall(items)
check("overall 返回文案与语义色", bool(verdict) and tone in
      ("green", "amber", "red"), (verdict, tone))
# 语义一致性：有 FAIL 必须 red；无 FAIL 有 WARN 必须 amber；全过 green
if counts[EC.FAIL]:
    check("有 FAIL → 结论是 red", tone == "red", (verdict, tone, counts))
elif counts[EC.WARN]:
    check("无 FAIL 有 WARN → 结论是 amber", tone == "amber", (verdict, tone))
else:
    check("全过 → 结论是 green", tone == "green", (verdict, tone))

text = EC.format_text(items)
check("文本报告含标题", "环境自检报告" in text)
check("文本报告含全部检查项标题",
      all(i["label"] in text for i in items),
      [i["label"] for i in items if i["label"] not in text])
check("文本报告含结论行", "结论：" in text)
check("文本报告用 [ OK ]/[FAIL] 标记",
      "[ OK ]" in text or "[FAIL]" in text or "[WARN]" in text)

# ============================================================ 3. 故障隔离
print()
print("== 3. 单条检查出错不能吞掉整份报告（★核心不变式）==")


def _boom():
    raise RuntimeError("模拟一条检查炸了")


_saved = EC._CHECKS
try:
    EC._CHECKS = (_boom, EC._check_mode, _boom)
    items2 = EC.run()
    check("坏检查不影响整轮跑完", len(items2) == 3, len(items2))
    check("坏检查被记成 FAIL 而不是抛出去",
          items2[0]["status"] == EC.FAIL and items2[2]["status"] == EC.FAIL,
          [i["status"] for i in items2])
    check("坏检查的 FAIL 里带上了异常信息",
          "模拟一条检查炸了" in str(items2[0]["detail"]),
          items2[0]["detail"])
    check("同一轮里好的检查仍然正常出结果",
          items2[1]["status"] in VALID and bool(items2[1]["label"]),
          items2[1])

    # 正对照：全好的时候不能有任何 FAIL（否则上面的断言可能只是恒真）
    EC._CHECKS = (EC._check_mode, EC._check_writable)
    items3 = EC.run()
    check("【正对照】全是好检查时不出现 FAIL",
          all(i["status"] != EC.FAIL for i in items3),
          [(i["label"], i["status"], i["detail"]) for i in items3])

    # progress 回调异常也不能影响结果
    def _bad_progress(*a):
        raise RuntimeError("progress 也炸")

    items4 = EC.run(progress=_bad_progress)
    check("progress 回调抛异常不影响自检", len(items4) == 2, len(items4))
finally:
    EC._CHECKS = _saved

# ============================================================ 4. 只读保证
print()
print("== 4. 自检不得留下残留文件 ==")
_leftover = [n for n in os.listdir(C.ROOT) if n.startswith(".wtest_")]
check("数据目录没有 .wtest_ 残留", not _leftover, _leftover)
_leftover_logs = ([n for n in os.listdir(C.LOG_DIR) if n.startswith(".wtest_")]
                  if os.path.isdir(C.LOG_DIR) else [])
check("logs 目录没有 .wtest_ 残留", not _leftover_logs, _leftover_logs)

# 可写性检查本身要能正确报告
_it = EC._check_writable()
check("数据目录可写检查返回 OK（本机确实可写）", _it["status"] == EC.OK,
      _it)

# ============================================================ 5. 关键链路确实被覆盖
print()
print("== 5. 关键链路都在检查清单里 ==")
_keys = {i["key"] for i in items}
for _k, _why in (("mode", "运行模式/数据目录"),
                 ("writable", "数据目录可写"),
                 ("userdirs", "★ 环境变量缺失（本项目最经典的假故障）"),
                 ("config", "配置可读"),
                 ("browser", "★ Playwright 浏览器（TraeWork 签到依赖）"),
                 ("client_traecn", "Trae CN 客户端"),
                 ("client_traework", "TraeWork 桌面端"),
                 ("client_workbuddy", "WorkBuddy 桌面端"),
                 ("src_traecn", "TraeCN 令牌来源"),
                 ("src_traework", "TraeWork 网页会话"),
                 ("src_workbuddy", "WorkBuddy 登录态文件"),
                 ("tokens", "令牌有效期"),
                 ("task", "计划任务"),
                 ("net_api.trae.cn", "网络：api.trae.cn"),
                 ("net_copilot.tencent.com", "网络：copilot.tencent.com")):
    check(f"覆盖 {_why}", _k in _keys, sorted(_keys))

print()
print(f"结果：{OK} OK / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
