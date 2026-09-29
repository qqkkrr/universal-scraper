#!/usr/bin/env python3
"""HTTP 会话池（对标 Crawlee SessionPool / Scrapy CookieJar）：
- 按域名维护一组「会话」：每个会话 = Cookie 罐 + UA + 代理
- 封禁/429/异常 → 会话错误计数 +1，连续出错自动换新会话（新 UA + 下一个代理）
- 成功 → 恢复计数；同一会话 Cookie 连续复用（登录态不丢）
"""
from __future__ import annotations

import random
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

UA_POOL = [
    # 全部 Chrome 系：与默认 TLS 指纹 impersonate="chrome" 保持一致（避免 UA/TLS 错配）
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


class HttpSession:
    __slots__ = ("domain", "ua", "proxy", "cookies", "errors", "last_used", "id", "jar",
                 "archive_seeded", "jlock")

    def __init__(self, domain: str, ua: str, proxy: Optional[str]):
        self.domain = domain
        self.ua = ua
        self.proxy = proxy
        self.cookies: Dict[str, str] = {}
        from http.cookiejar import CookieJar
        self.jar = CookieJar()
        # 审查修复（P2，R14）：CookieJar 非线程安全——同域 worker 并发
        # 读头/写罐会 RuntimeError（被吞后带着空 Cookie 头发请求）
        import threading as _th
        self.jlock = _th.Lock()
        self.errors = 0
        # OCR R131 终审（H）：last_used 曾初始化 0.0——新会话成为全局淘汰首选
        # （刚创建就可能被踢）。改为创建时刻，保证新会话有完整的生存窗口
        self.last_used = time.time()
        self.id = random.randint(100000, 999999)
        self.archive_seeded = False  # 已存档登录态是否播种过（轮换出的新会话为 False）


class SessionPool:
    """按域名管理会话；rotate_on_errors 次失败后强制换新会话。"""

    def __init__(self, proxies: Optional[List[str]] = None, ua_pool: Optional[List[str]] = None,
                 max_per_domain: int = 3, rotate_on_errors: int = 2, proxy_mode: str = "round_robin",
                 max_total: int = 500):
        self.proxies = list(proxies or [])
        self.ua_pool = list(ua_pool or UA_POOL)
        self.max_per_domain = max(1, int(max_per_domain))  # OCR R131：0 曾使 min() 空序列崩溃
        self.rotate_on_errors = max(1, int(rotate_on_errors))  # OCR R131：0 曾令所有会话永久不可用
        self.proxy_mode = proxy_mode
        self.max_total = max(1, max_total)  # 全局会话上限：防 1 万域名 = 3 万会话驻留内存
        self._pool: Dict[str, List[HttpSession]] = {}
        self._idx: Dict[str, int] = {}
        self._proxy_counter = 0
        self._lock = threading.RLock()  # 保护 _pool/_idx/_proxy_counter/stats；_next_proxy 的 round_robin 分支也会短暂取锁
        self.stats = {"created": 0, "rotated": 0, "blocked": 0}
        # 可选外部代理池回调（ProxyPool.next / mark_fail），打通冷却与失败惩罚
        self._proxy_source = None
        self._proxy_ok_cb = None
        self._proxy_fail_cb = None

    def _next_proxy(self) -> Optional[str]:
        if self._proxy_source is not None:
            return self._proxy_source()
        if not self.proxies:
            return None
        if self.proxy_mode == "random":
            return random.choice(self.proxies)
        # round_robin：计数器轮换（修复同秒新建会话拿同一代理）
        with self._lock:
            i = self._proxy_counter % len(self.proxies)
            self._proxy_counter += 1
            return self.proxies[i]

    def _pick_available(self, dom: str) -> Optional[HttpSession]:
        """域内轮换找一个错误未超阈值的会话（须持 self._lock 调用）。"""
        lst = self._pool.get(dom)
        if not lst:
            return None
        idx = self._idx.get(dom, 0)
        for _ in range(len(lst)):
            s = lst[idx % len(lst)]
            idx += 1
            if s.errors < self.rotate_on_errors:
                self._idx[dom] = idx
                s.last_used = time.time()
                return s
        return None

    def _evict_if_needed(self, dom: str) -> None:
        """新建会话前的回收：域内最久未用 + 全局最久未用（须持 self._lock 调用）。
        审查修复（MEDIUM）：rotate 曾缺全局 max_total 上限——封禁风暴下高频轮换
        可在多域名场景绕过内存保护。"""
        lst = self._pool.setdefault(dom, [])
        if len(lst) >= self.max_per_domain:
            # 重置最旧会话（Crawlee 思路：池满时回收最久未用的）
            old = min(lst, key=lambda x: x.last_used)
            lst.remove(old)
            self.stats["rotated"] += 1
        # 全局上限：超限时回收整个池里最久未用的会话（防 1 万域名内存爆炸）
        total = sum(len(v) for v in self._pool.values())
        if total >= self.max_total:
            _oldest = None
            _oldest_at = float("inf")
            for _lst in self._pool.values():
                for _s in _lst:
                    if _s.last_used < _oldest_at:
                        _oldest_at = _s.last_used
                        _oldest = _s
            if _oldest is not None:
                for _lst in self._pool.values():
                    if _oldest in _lst:
                        _lst.remove(_oldest)
                        self.stats["rotated"] += 1
                        break

    def acquire(self, url: str) -> HttpSession:
        """取一个会话：域内轮换；全被污染则新建（新 UA/代理）。"""
        dom = urlparse(url).netloc.lower()
        with self._lock:
            s = self._pick_available(dom)
            if s is not None:
                return s
        # 新建路径：代理解析放锁外——外部 _proxy_source（如 HTTP 代理供应商 API）
        # 可能阻塞或回调本池，持池锁执行会卡住所有 acquire/report/rotate 线程
        s = HttpSession(dom, random.choice(self.ua_pool), self._next_proxy())
        with self._lock:
            # 锁外解析期间他线程可能已补入可用会话：优先复用（新会话弃置，零成本）
            reused = self._pick_available(dom)
            if reused is not None:
                return reused
            self._evict_if_needed(dom)
            lst = self._pool.setdefault(dom, [])
            lst.append(s)
            self._idx[dom] = len(lst) - 1
            self.stats["created"] += 1
            return s

    def report(self, session: HttpSession, ok: bool, blocked: bool = False) -> None:
        """上报结果：ok=True 清零错误；blocked=True 立即污染并换新。
        同时把代理成败反馈给外部 ProxyPool（打通冷却/失败惩罚）。"""
        with self._lock:
            if ok:
                session.errors = 0
                if self._proxy_ok_cb is not None and session.proxy:
                    try:
                        self._proxy_ok_cb(session.proxy)
                    except Exception as e:
                        # OCR R131（M）：回调炸曾静默——外部 ProxyPool 联动失灵无从排查
                        import sys as _sys
                        print(f"⚠️ proxy_ok 回调异常（{type(e).__name__}: {e}）", file=_sys.stderr)
            else:
                session.errors += 1
                if self._proxy_fail_cb is not None and session.proxy:
                    try:
                        self._proxy_fail_cb(session.proxy)
                    except Exception as e:
                        import sys as _sys
                        print(f"⚠️ proxy_fail 回调异常（{type(e).__name__}: {e}）", file=_sys.stderr)
                if blocked:
                    session.errors = self.rotate_on_errors  # 立即触发轮换
                    self.stats["blocked"] += 1

    def rotate(self, url: str) -> HttpSession:
        """强制为 URL 所在域换新会话（如连续封禁）。"""
        dom = urlparse(url).netloc.lower()
        # 会话构造（含 _next_proxy 外部回调）放锁外，理由同 acquire
        s = HttpSession(dom, random.choice(self.ua_pool), self._next_proxy())
        with self._lock:
            self._evict_if_needed(dom)
            lst = self._pool.setdefault(dom, [])
            lst.append(s)
            self._idx[dom] = len(lst) - 1
            self.stats["created"] += 1
            self.stats["rotated"] += 1
            return s

    @property
    def size(self) -> int:
        """配置的代理数量（不含外部 _proxy_source）。"""
        return len(self.proxies)

    def summary(self) -> str:
        with self._lock:
            total = sum(len(v) for v in self._pool.values())
            return f"会话 {total}（新建 {self.stats['created']}，轮换 {self.stats['rotated']}，封禁 {self.stats['blocked']}）"
