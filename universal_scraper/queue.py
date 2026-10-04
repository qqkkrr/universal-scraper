#!/usr/bin/env python3
"""RequestQueue：去重队列 + 域名限速 + 深度预算（Scrapy Scheduler + Crawlee RequestQueue）。"""
from __future__ import annotations

import collections
import html as _html_mod
import re
import threading
import time
from typing import Any, Deque, Dict, Optional, Set  # 审查三轮：Any 曾漏导入（mark_seen 注解）
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
        """持锁拷贝当前队列待处理项（供检查点保存；直接迭代 deque 会与 pop 竞态）。

        审查修复（P1，R14）：返回结构化条目 [{"url","method","params"}] 而非裸
        URL——参数分页（同 URL 不同 meta.params）曾把 method/params 丢掉，
        resume 重放成错误的裸 GET，且与 state 里的裸 URL 幻键碰撞静默丢页。
        OCR R6 终审：body 曾一并丢失——Request.key() 含 body（protocols.py），
        POST 带体请求 resume 重放成无体请求（抓错数据 + 去重键漂移）。
        快照补 body 字段；恢复端需同步读取（engine_v3 重放处）。"""
        with self._lock:
            return [{"url": r.url, "method": r.method,
                     "params": (r.meta.get("params") or None),
                     "body": (r.body or None)}
                    for r in self._q]

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
                # 审查十六轮（L）：曾静默 return False——调用方无法区分"重复"
                # 与"队列容量满"，容量满后所有新任务悄悄丢队列。限频大声告警
                self.stats["skipped_capacity"] = self.stats.get("skipped_capacity", 0) + 1
                if self.stats["skipped_capacity"] == 1 or self.stats["skipped_capacity"] % 100 == 0:
                    import sys as _sys
                    print(f"⛔ 队列容量已满（_max_seen={self._max_seen}）——"
                          f"新任务无法入队（已拒绝 {self.stats['skipped_capacity']} 次）",
                          file=_sys.stderr)
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
                elapsed = now - last
                if elapsed < 0:
                    # OCR R131（H）：time.time() 非单调——NTP 回拨令 elapsed 恒负、
                    # 该域被永久冻结。回拨视为间隔已满（宁可放行不冻死采集）
                    elapsed = interval
                if elapsed >= interval:
                    self._domain_last[dom] = now
                    self.stats["dequeued"] += 1
                    return req
                self._q.append(req)
            return None

    def enqueue_retry(self, req: Request, retry_at: Optional[float] = None) -> bool:
        """重试入队：允许已 seen 的 URL 再次进入（Crawlee 风格），可指定延迟时间。

        OCR R131（H）：加病态重试上限——持久 429 风暴下 retry 曾无限堆积
        （不经过 enqueue 的 _max_seen 内存闸），队列无界膨胀。100 远超引擎
        正常 max_retries（默认 3），只拦真正的失控循环。"""
        with self._lock:
            if int(req.meta.get("retry", 0)) >= 100:
                return False  # 病态重试循环：放弃该请求（引擎自有 max_retries 兜底）
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
        with self._lock:  # OCR R131：len 与 pop/enqueue 并发读 deque，统一持锁
            return len(self._q)

    @property
    def seen_count(self) -> int:
        with self._lock:  # OCR R131：与 mark_seen 并发读写 set，统一持锁
            return len(self._seen)

    def mark_seen(self, url: str, method: str = "GET",
                  params: Any = None, body: Any = None) -> bool:
        """把 URL 标记为已见（断点续跑时注入历史）。

        OCR R131（H）：key 曾硬编码 GET——非 GET 历史请求注入后幻键永不命中，
        resume 时 POST 重复入队。method 与 Request.key() 同口径。
        审查二轮（H）：params 段曾缺失——分页请求 resume 注入键与新请求
        key()（含 params）永不匹配。序列化直接复用 protocols._key_extra，
        两端共用同一实现防再漂移。
        审查四轮（H）：key() 已含 body——mark_seen 补 body 形参同口径。"""
        from .protocols import _key_extra
        with self._lock:
            k = f"{method.upper()}|{url}{_key_extra(params)}{_key_extra(body)}"
            if k in self._seen:
                return False
            self._seen.add(k)
            return True

    def mark_key(self, k: str) -> bool:
        """注入一个与 Request.key() 完全同构的现成键（resume 保存端存过完整键）。"""
        with self._lock:
            if k in self._seen:
                return False
            self._seen.add(k)
            return True

    def set_domain_interval(self, domain: str, interval: float) -> None:
        with self._lock:
            self._domain_min_interval[domain] = interval


def extract_links(html: str, base_url: str, allow: Optional[str] = None,
                  deny: Optional[str] = None, same_domain: bool = False) -> list:
    """从 HTML 提取候选链接，按 allow/deny 正则过滤，补全为绝对 URL。
    same_domain=True 时只保留与 base_url 相同 hostname 的链接（Crawlee same-hostname 策略）。"""
    from urllib.parse import urljoin, urlparse
    links = set()
    # 忽略 <script>/<style> 内容里的 href（JS 模板字符串误匹配）
    _clean = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    # 收官十二轮（审查 L，实测）：只认带引号 href——HTML5 无引号属性（minify 后的
    # 现代站点大量如此，如 example.com 的 <a href=https://…>）全部漏检，
    # quick.fetch_url(links=True)/MCP scrape(links) 静默返回空。引号改可选
    # 审查八轮（L）：href 前加 (?<![\w:-])——data-href（前缀 -）与 xlink:href
    # （前缀 :）等属性名的尾段曾从中段起配（跟踪像素/预加载 URL 混进链接集
    # 烧预算+污染去重键）
    for m in re.finditer(r'(?<![\w:-])href=\s*(?:"([^"]+)"|\'([^\']+)\'|([^"\'\s>]+))', _clean, re.I):
        u = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if not u or u.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        # 审查二轮（M）：HTML 实体曾不解码——href="a?x=1&amp;y=2" 入键后与
        # 真实请求 URL（&）口径不符，resume 去重失效。
        # r4 自查：html.unescape 曾被同名参数遮蔽（AttributeError）——别名导入
        u = _html_mod.unescape(u)
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
                # 审查修复（P2，R6）：坏正则在回退循环曾直接 pass——与路径循环
                # 不对称，坏 allow 会静默滤光全部链接。对称回退为字面量子串
                if pat in u:
                    return True
        return False

    if allow:
        links = {u for u in links if _match(allow, u)}
    if deny:
        links = {u for u in links if not _match(deny, u)}
    return sorted(links)
