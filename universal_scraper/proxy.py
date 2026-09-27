#!/usr/bin/env python3
"""代理轮换（对标 scrapy-rotating-proxies / Crawlee ProxyConfiguration）：
- round_robin / random 两种轮换
- 失败代理进入冷却（sticky 惩罚），连续失败翻倍冷却，成功恢复
- 请求前可拿"下一个可用代理"，失败后标记
"""
from __future__ import annotations

import itertools
import random
import sys
import threading
import time
from typing import List, Optional

# 全池冷却回退直连的大声告警：限频防刷屏（每 5 分钟最多一次），
# 但每次发生都计数（任务结束汇报 total）。模块级限频状态多池共享，
# check+set 必须持锁（否则同瞬间多池并发会重复刷屏）。
_DIRECT_WARN = {"at": 0.0}
_DIRECT_WARN_INTERVAL = 300.0
_DIRECT_WARN_LOCK = threading.Lock()


def _warn_direct_fallback(alive: int, size: int, warn=None) -> None:
    now = time.time()
    with _DIRECT_WARN_LOCK:
        if now - _DIRECT_WARN["at"] < _DIRECT_WARN_INTERVAL:
            return
        _DIRECT_WARN["at"] = now
    msg = (f"⚠️ 代理池全部冷却中（{alive}/{size} 可用），本次请求将【直连】——"
           f"真实出口 IP 会暴露给目标站，可能被风控关联封禁。"
           f"可选处方: 等冷却恢复 / `cli proxy` 刷新代理池 / 显式停止任务")
    if callable(warn):
        try:
            warn(msg)
            return
        except Exception:
            pass  # 回调通道失败回落 stderr（WebUI/MCP 之外的兜底）
    print(f"[WARN] {msg}", file=sys.stderr, flush=True)


class ProxyPool:
    def __init__(self, proxies: Optional[List[str]], mode: str = "round_robin",
                 cooldown: float = 60.0, max_fail_streak: int = 3,
                 fail_multiplier: float = 2.0, max_cooldown: float = 3600.0,
                 warn=None):
        self.proxies = list(proxies or [])
        self.mode = mode
        self.cooldown = cooldown
        self.max_fail_streak = max_fail_streak
        self.fail_multiplier = fail_multiplier
        self.max_cooldown = max_cooldown
        self.direct_fallbacks = 0           # 全池冷却时回退直连的累计次数（任务结束汇报）
        self._warn = warn                   # 可选告警回调（如 engine 的 _log_cb，WebUI 可见）
        self._iter = itertools.cycle(self.proxies) if self.proxies else None
        self._dead_until: dict = {}          # proxy -> 冷却截止时间
        self._fail_streak: dict = {}         # proxy -> 连续失败次数
        self._last_fail: Optional[str] = None
        self._lock = threading.RLock()        # 多 worker 并发调用（SessionPool 回调）

    def next(self) -> Optional[str]:
        """取下一个可用代理；全部冷却中返回 None（调用方直连）。线程安全。

        深度改进①（2026-09）：直连回退绝不静默——大声告警（限频）+ 计数。
        此前静默直连 = 用户以为在走代理其实裸奔，真实 IP 暴露给目标站。"""
        # OCR R131（M）：直连告警曾在持池锁时执行——warn 回调若做 I/O（WebUI
        # 日志）会阻塞全部 worker 取代理。锁内只记数，锁外告警
        _warn_out = False
        with self._lock:
            if not self.proxies:
                return None
            now = time.time()
            if self.mode == "random":
                # R128 修复：洗牌后遍历确保每个存活代理都被尝试到
                candidates = list(self.proxies)
                random.shuffle(candidates)
                for p in candidates:
                    if now >= self._dead_until.get(p, 0):
                        return p
                self.direct_fallbacks += 1
                _warn_out = True
            else:
                for _ in range(len(self.proxies)):
                    p = next(self._iter) if self._iter else None
                    if p is None:
                        return None
                    if now >= self._dead_until.get(p, 0):
                        return p
                self.direct_fallbacks += 1
                _warn_out = True
        if _warn_out:
            _warn_direct_fallback(self.alive_count, len(self.proxies), self._warn)
        # OCR R131（M）：到达此处必然 _warn_out=True（两个分支要么 return p 要么置
        # True）——原尾部的第二个 return None 不可达，删除
        return None

    def mark_fail(self, proxy: Optional[str]) -> None:
        if not proxy:
            return
        with self._lock:
            self._last_fail = proxy
            streak = self._fail_streak.get(proxy, 0) + 1
            self._fail_streak[proxy] = streak
            # OCR R131（M）：max_fail_streak 形参曾存而不用——接入指数上限
            # （超过阈值后冷却不再按倍数增长，封顶 max_cooldown 前先线性缓涨）
            _exp = min(max(0, streak - 1), self.max_fail_streak)
            cd = self.cooldown * (self.fail_multiplier ** _exp)
            self._dead_until[proxy] = time.time() + min(cd, self.max_cooldown)

    def mark_ok(self, proxy: Optional[str]) -> None:
        if not proxy:
            return
        with self._lock:
            self._fail_streak[proxy] = 0
            self._dead_until.pop(proxy, None)

    def reset(self) -> None:
        with self._lock:
            self._dead_until.clear()
            self._fail_streak.clear()

    @property
    def size(self) -> int:
        return len(self.proxies)

    @property
    def alive_count(self) -> int:
        now = time.time()
        with self._lock:
            return sum(1 for p in self.proxies if now >= self._dead_until.get(p, 0))

    def summary(self) -> str:
        with self._lock:
            s = f"{self.alive_count}/{self.size} 可用" + (f"，最近失败 {self._last_fail}" if self._last_fail else "")
            if self.direct_fallbacks:
                s += f"，直连回退 {self.direct_fallbacks} 次"
            return s
