#!/usr/bin/env python3
"""robots.txt 尊重（对标 Crawlee respectRobotsTxtFile / Scrapy robots.txt middleware）：
- 按域名懒加载并缓存 robots.txt（stdlib urllib.robotparser）
- 遵守 Disallow / Allow；解析 Crawl-delay 供限速使用
- 获取失败默认放行（保守但不过度阻塞）
"""
from __future__ import annotations

import re
import threading
from urllib.parse import urlparse, urlsplit
from urllib.robotparser import RobotFileParser
from typing import Dict, Optional

from .core import make_http_client


class WildcardRobotParser(RobotFileParser):
    """R22 修复：stdlib RobotFileParser 把 * / $ 当字面字符——`Disallow: /*.pdf$`
    这类 Google 通配规则永不命中，意图封禁的路径被静默抓取（wrong-allow 方向）。
    且 3.12 起规则路径会被百分号编码（* → %2A），通配符到不了匹配层——所以
    parse 时自留原始规则行，can_fetch 在父类判"允许"后按 Google 通配语义复查
    （* → 任意串、$ → 行尾锚、最长规则胜出、Allow 可翻案、具名组优先于 * 组）。"""

    # OCR R131（L）：_WC 死代码已删（通配检测由 _rule_regex 自身处理）
    _RULE_RE_CACHE: dict = {}  # OCR R131（M）：规则正则曾每 can_fetch 每规则重编译——
                                # 大 robots（数百规则）×高频检查全部浪费在 compile
    # OCR R131 终审（H）：锁曾延迟到 __init__ 竞态初始化——并发首实例可能各建一个
    # Lock 互不互斥。改类体直接初始化（类定义期单线程执行，天然安全）
    _RULE_RE_CACHE_LOCK = threading.Lock()

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._raw_rules = []  # [(agents 元组, "DISALLOW"/"ALLOW", 原始路径)]

    @classmethod
    def _rule_regex(cls, pat: str) -> "re.Pattern[str]":
        with cls._RULE_RE_CACHE_LOCK:
            cached = cls._RULE_RE_CACHE.get(pat)
            if cached is not None:
                return cached
            rx = re.escape(pat).replace(r"\*", ".*")
            if rx.endswith(r"\$"):
                rx = rx[:-2] + "$"
            compiled = re.compile(rx)
            if len(cls._RULE_RE_CACHE) >= 2048:
                cls._RULE_RE_CACHE.clear()  # 防御性上限（正常 robots 远小于此）
            cls._RULE_RE_CACHE[pat] = compiled
            return compiled

    def parse(self, lines):
        super().parse(lines)
        agents, state = [], 0
        for line in lines:
            s = (line or "").strip()
            if not s:
                # 收官六轮（审查）：空行不断组——RFC 9309 规定组内空行忽略
                # 此前重置 agents 导致组内规则落入空元组并从仲裁表消失
                continue
            i = s.find("#")
            if i >= 0:
                s = s[:i].strip()
                if not s:
                    continue
            parts = s.split(":", 1)
            if len(parts) != 2:
                continue
            k = parts[0].strip().lower()
            v = parts[1].strip()
            if k in ("user-agent", "useragent"):
                if state == 2:
                    agents = []
                agents.append(v.lower())
                state = 1
            elif k in ("disallow", "allow") and v:
                self._raw_rules.append((tuple(agents), "DISALLOW" if k.startswith("d") else "ALLOW", v))
                state = 2

    def _wc_rules_for(self, useragent):
        """适用组的全部规则（含纯前缀规则）——最长匹配仲裁必须在同一张桌上做：
        纯前缀 Allow 要能翻案通配 Disallow（反之亦然），只看通配规则会失衡。"""
        ua = (useragent or "*").lower()
        named = [t for t in self._raw_rules if any(a != "*" and a in ua for a in t[0])]
        pool = named or [t for t in self._raw_rules if any(a == "*" for a in t[0])]
        return [(r, p) for _ags, r, p in pool]

    def can_fetch(self, useragent, url):
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        rules = self._wc_rules_for(useragent)
        if rules:
            # 有原始规则时全量自裁：stdlib 的 allowance 是"首条匹配胜"（文件顺序），
            # Google 语义是最长匹配——Allow: /search/about 应翻案先出现的 Disallow: /search
            u = urlsplit(url)
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
            best_dis = best_allow = -1
            for rule, pat in rules:
                if self._rule_regex(pat).match(path):
                    if rule == "DISALLOW":
                        best_dis = max(best_dis, len(pat))
                    else:
                        best_allow = max(best_allow, len(pat))
            if best_dis < 0:
                return True
            return best_allow >= best_dis
        return super().can_fetch(useragent, url)


class RobotsTxt:
    def __init__(self, user_agent: str = "universal-scraper/1.0", timeout: int = 10):
        self.user_agent = user_agent
        self.timeout = timeout
        self._parsers: Dict[str, Optional[RobotFileParser]] = {}
        self._lock = threading.Lock()

    def _fetch_parser(self, domain: str, scheme: str) -> Optional[RobotFileParser]:
        parser = WildcardRobotParser()  # R22：通配符语义（stdlib 会漏判 /*.pdf$ 类规则）
        try:
            client = make_http_client({"min_interval": 0.0, "max_retries": 1,
                                       "timeout": self.timeout, "http_backend": "auto"})
            res = client.get(f"{scheme}://{domain}/robots.txt")
            if not res.get("ok") or res.get("status", 0) not in (200,):
                # 审查修复（P2）：robots 获取失败（常见：WAF 连 robots 一起拦）
                # fail-open 是既定策略，但静默禁用合规检查必须有信号
                from .core import log
                log(f"⚠️ robots.txt 获取失败（HTTP {res.get('status', 0)}，{domain}）"
                    "——本次按允许处理，合规检查未生效", "WARN")
                return None
            parser.parse((res.get("text") or "").splitlines())
            return parser
        except Exception as e:
            from .core import log
            log(f"⚠️ robots.txt 获取异常（{domain}）: {type(e).__name__}: {e}"
                "——本次按允许处理，合规检查未生效", "WARN")
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
            # OCR R131（H）：双取竞态——A 线程成功取到 parser、B 线程失败拿到
            # None，B 后写曾把好结果覆盖成 None（整域判型退化为永久放行）
            if parser is None and self._parsers.get(domain) is not None:
                return self._parsers[domain]  # 他线程已拿到好结果：用它的
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
