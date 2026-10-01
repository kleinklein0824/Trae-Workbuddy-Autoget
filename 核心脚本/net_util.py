#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网络可用性探测（供各签到模块共用）。"""
import time
import urllib.request

_PROBES = ["https://www.baidu.com/generate_204", "https://api.trae.cn/", "https://work.trae.cn/"]


def network_ok(timeout_s=5):
    """快速探测当前是否可上网（任一目标可达即视为在线）。"""
    for url in _PROBES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout_s):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def wait_network(max_wait_s=None, interval_s=30, log=print, log_every=10):
    """嗅探等待网络就绪（嗅探锚点：每 interval_s 秒探测一次）。

    max_wait_s=None 表示一直等到联网为止；否则超时返回 False。
    返回 True=在线，False=超时仍离线。等待期间每 log_every 次嗅探打一条
    心跳日志，避免刷屏又能确认进程存活。
    """
    n = 0
    if max_wait_s is None:
        deadline = None
    else:
        deadline = time.time() + max_wait_s
    while True:
        if network_ok():
            if n:
                log(f"[net] 网络已恢复（本次共嗅探 {n} 次）")
            return True
        n += 1
        if log_every and n % log_every == 0:
            left = "无上限" if deadline is None else f"{max(0, int(deadline - time.time()))//60} 分钟"
            log(f"[net] 仍离线，持续嗅探中（剩余等待上限：{left}）")
        if deadline is not None and time.time() >= deadline:
            return False
        time.sleep(interval_s)
