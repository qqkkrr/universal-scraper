#!/usr/bin/env python3
"""RequestQueue：去重队列 + 域名限速 + 深度预算（Scrapy Scheduler + Crawlee RequestQueue）。"""
from __future__ import annotations

import collections
import re
import threading
import time
from typing import Deque, Dict, Optional, Set
from urllib.parse import urlparse

from .protocols import Request


class RequestQueue:
    def __init__(self, max_seen: int = 2_000_000):
        self._q: Deque[Request] = collections.deque()
        self._seen: Set[str] = set()
        self._max_seen = max_seen
        self._domain_last: Dict[str, float] = {}
        self._domain_min_interval: Dict[str, float] = {}
        self.stats = {"enqueued": 0, "dequeued": 0, "skipped_dup": 0, "skipped_depth": 0}
        self._lock = threading.Lock()  # 多 worker 并发安全

    def snapshot_urls(self):
        """持锁拷贝当前队列 URL（供检查点保存；直接迭代 deque 会与 pop 竞态）。"""
        with self._lock:
            return [r.url for r in self._q]

    def enqueue(self, req: Request, min_interval: float = 1.0, max_depth: int = 10) -> bool:
        """入队；去重；超深度丢弃。返回是否真的入队。"""
        with self._lock:
            if req.depth > max_depth:
                self.stats["skipped_depth"] += 1
                return False
            k = req.key()
            if k in self._seen:
                self.stats["skipped_dup"] += 1
                return False
            if len(self._seen) >= self._max_seen:
                return False
            self._seen.add(k)
            dom = urlparse(req.url).netloc
            self._domain_min_interval.setdefault(dom, min_interval)
            self._q.append(req)
            self.stats["enqueued"] += 1
            return True

    def pop(self) -> Optional[Request]:
        """出队；遵守域名最小间隔 + 重试时间（不到时间就跳过到队尾）。线程安全。"""
        with self._lock:
            now = time.time()
            for _ in range(len(self._q)):
                req = self._q.popleft()
                retry_at = req.meta.get("retry_at")
                if retry_at is not None and now < retry_at:
                    self._q.append(req)  # 重试未到时间，跳回队尾
                    continue
                dom = urlparse(req.url).netloc
                interval = self._domain_min_interval.get(dom, 1.0)
                last = self._domain_last.get(dom, 0.0)
                if now - last >= interval:
                    self._domain_last[dom] = now
                    self.stats["dequeued"] += 1
                    return req
                self._q.append(req)
            return None

    def enqueue_retry(self, req: Request, retry_at: Optional[float] = None) -> bool:
        """重试入队：允许已 seen 的 URL 再次进入（Crawlee 风格），可指定延迟时间。"""
        with self._lock:
            req.meta["retry_at"] = retry_at if retry_at is not None else time.time()
            req.meta["retry"] = int(req.meta.get("retry", 0)) + 1
            self._q.append(req)
            self.stats["enqueued"] += 1
            return True

    def min_retry_at(self) -> Optional[float]:
        """队列里最早的重试时间（没有待重试返回 None）。"""
        with self._lock:
            ts = [r.meta.get("retry_at") for r in self._q if r.meta.get("retry_at")]
            return min(ts) if ts else None

    def __len__(self) -> int:
        return len(self._q)

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    def mark_seen(self, url: str) -> bool:
        """把 URL 标记为已见（断点续跑时注入历史）。"""
        with self._lock:
            k = f"GET|{url}"
            if k in self._seen:
                return False
            self._seen.add(k)
            return True

    def set_domain_interval(self, domain: str, interval: float) -> None:
        self._domain_min_interval[domain] = interval


def extract_links(html: str, base_url: str, allow: Optional[str] = None,
                  deny: Optional[str] = None, same_domain: bool = False) -> list:
    """从 HTML 提取候选链接，按 allow/deny 正则过滤，补全为绝对 URL。
    same_domain=True 时只保留与 base_url 相同 hostname 的链接（Crawlee same-hostname 策略）。"""
    from urllib.parse import urljoin, urlparse
    links = set()
    # 忽略 <script>/<style> 内容里的 href（JS 模板字符串误匹配）
    _clean = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    for m in re.finditer(r'href=["\']([^"\']+)["\']', _clean, re.I):
        u = m.group(1).strip()
        if not u or u.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        u = urljoin(base_url, u)
        if u.startswith(("http://", "https://")):
            # URL 规范化：去掉 #fragment（同页不同锚点不重复抓）
            try:
                _p = urlparse(u)
                u = _p._replace(fragment="").geturl()
            except Exception:
                pass
            links.add(u)
    if same_domain:
        host = urlparse(base_url).netloc.lower()
        links = {u for u in links if urlparse(u).netloc.lower() == host}
    # allow/deny 支持字符串或列表；按 URL 路径+查询串匹配（Scrapy LinkExtractor 风格，
    # 锚定模式 ^/page/\d+/$ 才能避免误吃 /tag/xxx/page/1/ 这类同构 URL）
    def _paths(u):
        try:
            parsed = urlparse(u)
            return parsed.path + (("?" + parsed.query) if parsed.query else "")
        except Exception:
            return u

    def _match(patterns, u):
        pats = patterns if isinstance(patterns, list) else [patterns]
        path = _paths(u)
        for pat in pats:
            try:
                if re.search(pat, path):
                    return True
            except re.error:
                if pat in path:
                    return True
        # 兼容旧写法：路径没匹配上时，回退匹配完整 URL（老配置写 ^https://... 仍可用）
        for pat in pats:
            try:
                if re.search(pat, u):
                    return True
            except re.error:
                pass
        return False

    if allow:
        links = {u for u in links if _match(allow, u)}
    if deny:
        links = {u for u in links if not _match(deny, u)}
    return sorted(links)
