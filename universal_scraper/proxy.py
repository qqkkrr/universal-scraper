#!/usr/bin/env python3
"""代理轮换（对标 scrapy-rotating-proxies / Crawlee ProxyConfiguration）：
- round_robin / random 两种轮换
- 失败代理进入冷却（sticky 惩罚），连续失败翻倍冷却，成功恢复
- 请求前可拿"下一个可用代理"，失败后标记
"""
from __future__ import annotations

import itertools
import random
import threading
import time
from typing import List, Optional


class ProxyPool:
    def __init__(self, proxies: Optional[List[str]], mode: str = "round_robin",
                 cooldown: float = 60.0, max_fail_streak: int = 3,
                 fail_multiplier: float = 2.0, max_cooldown: float = 3600.0):
        self.proxies = list(proxies or [])
        self.mode = mode
        self.cooldown = cooldown
        self.max_fail_streak = max_fail_streak
        self.fail_multiplier = fail_multiplier
        self.max_cooldown = max_cooldown
        self._iter = itertools.cycle(self.proxies) if self.proxies else None
        self._dead_until: dict = {}          # proxy -> 冷却截止时间
        self._fail_streak: dict = {}         # proxy -> 连续失败次数
        self._last_fail: Optional[str] = None
        self._lock = threading.RLock()        # 多 worker 并发调用（SessionPool 回调）

    def next(self) -> Optional[str]:
        """取下一个可用代理；全部冷却中返回 None（调用方直连）。线程安全。"""
        with self._lock:
            if not self.proxies:
                return None
            now = time.time()
            for _ in range(len(self.proxies)):
                if self.mode == "random":
                    p = random.choice(self.proxies)
                else:
                    p = next(self._iter) if self._iter else None
                if p is None:
                    return None
                if now >= self._dead_until.get(p, 0):
                    return p
            return None

    def mark_fail(self, proxy: Optional[str]) -> None:
        if not proxy:
            return
        with self._lock:
            self._last_fail = proxy
            streak = self._fail_streak.get(proxy, 0) + 1
            self._fail_streak[proxy] = streak
            cd = self.cooldown * (self.fail_multiplier ** max(0, streak - 1))
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
            return f"{self.alive_count}/{self.size} 可用" + (f"，最近失败 {self._last_fail}" if self._last_fail else "")
