#!/usr/bin/env python3
"""robots.txt 尊重（对标 Crawlee respectRobotsTxtFile / Scrapy robots.txt middleware）：
- 按域名懒加载并缓存 robots.txt（stdlib urllib.robotparser）
- 遵守 Disallow / Allow；解析 Crawl-delay 供限速使用
- 获取失败默认放行（保守但不过度阻塞）
"""
from __future__ import annotations

import threading
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from typing import Dict, Optional

from .core import make_http_client


class RobotsTxt:
    def __init__(self, user_agent: str = "universal-scraper/1.0", timeout: int = 10):
        self.user_agent = user_agent
        self.timeout = timeout
        self._parsers: Dict[str, Optional[RobotFileParser]] = {}
        self._lock = threading.Lock()

    def _fetch_parser(self, domain: str, scheme: str) -> Optional[RobotFileParser]:
        parser = RobotFileParser()
        try:
            client = make_http_client({"min_interval": 0.0, "max_retries": 1,
                                       "timeout": self.timeout, "http_backend": "auto"})
            res = client.get(f"{scheme}://{domain}/robots.txt")
            if not res.get("ok") or res.get("status", 0) not in (200,):
                return None
            parser.parse((res.get("text") or "").splitlines())
            return parser
        except Exception:
            return None

    def parser_for(self, url: str) -> Optional[RobotFileParser]:
        p = urlparse(url)
        domain = (p.netloc or "").lower()
        if not domain:
            return None
        with self._lock:
            if domain in self._parsers:
                return self._parsers[domain]
        parser = self._fetch_parser(domain, p.scheme or "https")
        with self._lock:
            self._parsers[domain] = parser
        return parser

    def allowed(self, url: str) -> bool:
        """URL 是否允许抓取（获取/解析失败默认放行）。"""
        try:
            parser = self.parser_for(url)
            if parser is None:
                return True
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def crawl_delay(self, url: str) -> float:
        """该域名 robots.txt 的 Crawl-delay（秒）；无则 0。"""
        try:
            parser = self.parser_for(url)
            if parser is None:
                return 0.0
            delay = parser.crawl_delay(self.user_agent)
            return float(delay or 0.0)
        except Exception:
            return 0.0
