#!/usr/bin/env python3
"""内置解析器：声明式（JSON路径/CSS/XPath/正则）。"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from ..protocols import BaseParser, ParseResult, Response, ParseContext, Request
from ..selectors import jpath, apply_extractor
try:
    from lxml.html import HtmlElement as _lxml_el
except Exception:  # pragma: no cover
    _lxml_el = None
from ..queue import extract_links


class ConfigParser(BaseParser):
    """声明式解析器：parsers.<name> 配置驱动。

    支持:
      - html 列表: row_css/row_xpath + fields{css|xpath|attr}
      - json 列表: records_path + fields{from|path}
      - 链接提取: extract_links{allow, deny} → 生成新请求（递归）
    """
    name = "config"

    def parse(self, resp: Response, ctx: ParseContext) -> ParseResult:
        cfg = self.config or {}
        result = ParseResult()
        mode = cfg.get("type", "html")

        if mode == "json" or (resp.json is not None and not cfg.get("force_html")):
            obj = resp.json
            rows = jpath(obj, cfg.get("records_path", ""), []) if cfg.get("records_path") else obj
            if not isinstance(rows, list):
                rows = [rows]
            for row in rows:
                if isinstance(row, dict):
                    result.items.append(self._map(row, cfg.get("fields", {})))
        else:
            html = resp.text
            if cfg.get("row_css") or cfg.get("row_xpath"):
                rows = self._html_rows(html, cfg)
                for row in rows:
                    result.items.append(row)
            elif cfg.get("fields"):
                result.items.append(self._map_html(html, cfg.get("fields", {})))
            else:
                result.items.append({"text": re.sub(r"<[^>]+>", " ", html)[:2000]})

        # 递归：提取链接生成新请求（same_domain = Crawlee same-hostname 策略）
        lk = cfg.get("extract_links")
        if lk and resp.text:
            base = resp.url or (resp.request.url if resp.request else "")
            for u in extract_links(resp.text, base, lk.get("allow"), lk.get("deny"),
                                   bool(lk.get("same_domain", False))):
                result.requests.append(Request(url=u, depth=(resp.request.depth + 1) if resp.request else 1))
        return result

    def _map(self, row: Dict[str, Any], fields: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name, spec in (fields or {}).items():
            if isinstance(spec, str):
                out[name] = jpath(row, spec, None)
            elif isinstance(spec, dict):
                if "from" in spec:
                    out[name] = jpath(row, spec["from"], spec.get("default"))
                elif "path" in spec:
                    out[name] = jpath(row, spec["path"], spec.get("default"))
                elif "constant" in spec:
                    out[name] = spec["constant"]
                else:
                    out[name] = jpath(row, spec.get("path", ""), None)
        return out

    @staticmethod
    def _css_value(el_html: str, fspec: Dict[str, Any], limit: int = 0) -> str:
        """取字段值：优先显式 attr；兼容 css 里的 ::attr(name) 写法（如 .p1 a::attr(href)）。
        默认按单值字段处理：多元素拼接明显是"同一行多个链接/标签"时自动只留第一个，
        避免 title 变成 'd\nf'、href 变成 '/a /b'。字段显式 multiple/join 时保留全部。"""
        from ..selectors import css_attr, css_text, xpath_text, regex_extract
        multi = bool(fspec.get("multiple") or fspec.get("join"))
        css = fspec.get("css") or ""
        if fspec.get("attr"):
            v = css_attr(el_html, css, fspec["attr"], fspec.get("limit", limit))
            return v if multi else ConfigParser._smart_single(v, "attr")
        if "::attr(" in css:
            import re as _re
            m = _re.search(r"::attr\(([^)]*)\)", css)
            attr = m.group(1).strip().strip("'\"") if m else "href"
            v = css_attr(el_html, css, attr, fspec.get("limit", limit))
            return v if multi else ConfigParser._smart_single(v, "attr")
        if fspec.get("xpath"):
            v = xpath_text(el_html, fspec["xpath"], fspec.get("limit", limit))
            return v if multi else ConfigParser._smart_single(v, "text")
        if css:
            v = css_text(el_html, css, fspec.get("limit", limit))
            return v if multi else ConfigParser._smart_single(v, "text")
        if fspec.get("regex"):
            return regex_extract(el_html, fspec["regex"], fspec.get("group", 0))
        return ""

    @staticmethod
    def _smart_single(v: str, kind: str = "text") -> str:
        """多元素拼接结果去噪（仅在字段未声明 multiple/join 时启用）：
        - attr：多个 URL/路径拼成 '/a /b' → 取第一个像 URL 的
        - text：多个短链接文本拼成 'd\nf' → 取第一个；描述类多段落（长/含标点）保留全文
        """
        if not v:
            return v
        if kind == "attr" and " " in v:
            toks = [t for t in v.split() if t]
            url_like = [t for t in toks if re.match(r"^(https?://|/|#|mailto:|tel:|\./|\.\./)", t)]
            if len(url_like) >= 2 or (len(toks) > 1 and len(url_like) == len(toks)):
                return url_like[0]
            return v
        if kind == "text" and "\n" in v:
            parts = [re.sub(r"\s+", " ", x).strip() for x in v.split("\n") if x.strip()]
            if len(parts) >= 2 and all(len(x) <= 80 and not re.search(r"[。！？；;，]", x)
                                       and " " not in x for x in parts):
                return parts[0]
            return v
        return v


    def _html_rows(self, html: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        if cfg.get("row_xpath"):
            from ..selectors import xpath_elements, _lxml_html_tostring
            els = xpath_elements(html, cfg["row_xpath"])
        else:
            from ..selectors import css_elements, _lxml_html_tostring
            els = css_elements(html, cfg.get("row_css") or "body")
        self._self_healing = False
        if not els and cfg.get("row_css"):
            # 自愈兜底：配置的选择器没匹配到（AI 猜错 class 等），
            # 自动找页面里重复最多的 li/div/tr（真实列表行）做通用提取，避免"能抓却0条"
            els = self._self_heal_rows(html)
            self._self_healing = bool(els)
        out = []
        for el in els:
            el_html = el if isinstance(el, str) else _lxml_html_tostring(el)
            row = {}
            for name, fspec in (cfg.get("fields", {}) or {}).items():
                if isinstance(fspec, dict):
                    v = self._css_value(el_html, fspec)
                    # 自愈模式：配置选择器没匹配时按字段名猜（仅当该字段全部为空才猜，避免覆盖）
                    if not v and isinstance(el, _lxml_el) and self._self_healing:
                        v = self._guess_field(el, name)
                    row[name] = v
                else:
                    row[name] = self._string_field(el_html, fspec, name)
            out.append(row)
        return out

    @staticmethod
    def _string_field(el_html: str, spec: str, name: str = "") -> str:
        """字符串形式字段选择器（AI 常用）：'sel' / 'sel::text' / 'sel::attr(href)' / xpath://...
        修复：之前字符串 spec 直接把整行 HTML 塞进字段（title 变 <tr>...</tr>）。"""
        from ..selectors import css_attr, css_text, xpath_text
        s = str(spec or "").strip()
        if not s:
            return ""
        if s.startswith("xpath:") or s.startswith("//"):
            x = s[6:] if s.startswith("xpath:") else s
            return ConfigParser._smart_single(xpath_text(el_html, x), "text")
        m = re.search(r"::attr\(([^)]*)\)\s*$", s)
        if m:
            attr = (m.group(1).strip().strip("'\"") or "href")
            sel = s[:m.start()].strip()
            return ConfigParser._smart_single(css_attr(el_html, sel, attr), "attr")
        sel = re.sub(r"::text\s*$", "", s).strip()
        if not sel:
            return ""
        return ConfigParser._smart_single(css_text(el_html, sel), "text")

    # 字段名 → 常见 class 关键词（自愈时按字段名猜选择器，让猜错 row_css 也能拿到字段值）
    _FIELD_HINTS = {
        "title": ["job-name", "job_title", "position", "post", "subject", "heading", "title", "name"],
        "name": ["job-name", "job_title", "position", "post", "subject", "heading", "title", "name"],
        "salary": ["salary", "job-salary", "pay", "wage", "money", "price", "compensation"],
        "price": ["salary", "job-salary", "pay", "wage", "money", "price", "compensation"],
        "company": ["company-name", "boss-name", "corp-name", "shop-name", "merchant", "brand",
                  "company", "corp", "enterprise"],
        "location": ["company-location", "job-area", "job-location", "location", "area",
                  "address", "city", "region", "district", "place"],
        "area": ["company-location", "job-area", "job-location", "location", "area",
                 "address", "city", "region", "district", "place"],
        "address": ["company-location", "job-area", "job-location", "location", "area",
                    "address", "city", "region", "district", "place"],
        "tags": ["tag-list", "tag", "label", "skill", "keyword", "badge"],
        "tag": ["tag-list", "tag", "label", "skill", "keyword", "badge"],
        "date": ["date", "time", "pub", "update", "create", "release", "deadline", "publish"],
        "author": ["author", "user", "people", "publisher", "boss-name", "username"],
        "desc": ["desc", "description", "intro", "summary", "content", "detail", "text"],
        "text": ["desc", "description", "intro", "summary", "content", "detail", "text"],
        "link": ["job-name", "title", "name", "detail"],
        "url": ["job-name", "title", "name", "detail"],
    }

    @staticmethod
    def _guess_field(el, name: str) -> str:
        """字段名驱动的选择器猜测：在元素(行/整页)里按常见 class 关键词找字段值。
        供自愈行和 detail 详情提取共用（AI 选择器漏了也能兜底）。"""
        key = re.sub(r"[^a-z]", "", (name or "").lower())
        if key in ("link", "url", "href"):
            a = el.cssselect("a[href]")
            return a[0].get("href", "").strip() if a else ""
        # 字段名模糊归并：publish_time/publishtime/updated_at → date 类
        if key not in ConfigParser._FIELD_HINTS:
            if any(w in key for w in ("time", "date", "publish", "pub", "update", "release", "created")):
                key = "date"
            elif any(w in key for w in ("price", "salary", "pay", "wage", "money")):
                key = "salary"
            elif any(w in key for w in ("company", "corp", "boss", "shop", "brand")):
                key = "company"
            elif any(w in key for w in ("location", "area", "addr", "city", "region")):
                key = "location"
            elif any(w in key for w in ("title", "name", "job", "position")):
                key = "title"
            elif any(w in key for w in ("desc", "content", "detail", "text", "intro")):
                key = "desc"
            elif any(w in key for w in ("tag", "label", "skill")):
                key = "tags"
            elif any(w in key for w in ("author", "user", "people")):
                key = "author"
        hints = ConfigParser._FIELD_HINTS.get(key) or []
        if key == "date":
            # 1) meta 标签（SEO 标准：发布时间常藏在 meta property/name/itemprop 里）
            for node in el.iter():
                if node.tag == "meta":
                    attrs = " ".join(str(node.get(a) or "") for a in ("property", "name", "itemprop"))
                    if any(w in attrs.lower() for w in ("date", "time")):
                        c = (node.get("content") or "").strip()
                        if c:
                            return c
            # 2) JSON-LD（upDate/datePublished/dateModified 等）
            try:
                import json as _json
                for scr in el.cssselect("script[type='application/ld+json']") or []:
                    data = _json.loads(scr.text or "{}")
                    if isinstance(data, dict):
                        for k in ("upDate", "datePublished", "dateModified", "dateCreated", "pubDate"):
                            v = data.get(k)
                            if isinstance(v, dict):
                                v = v.get("@value")
                            if v:
                                return str(v)
            except Exception:
                pass
            # 3) 可见文本"发布时间/更新时间：2026-08-07"
            txt = re.sub(r"\s+", " ", (el.text_content() or ""))
            m = re.search(r"(?:发布|更新|上线)时间[：:]\s*([0-9]{4}[-/年][0-9]{1,2}[-/月][0-9]{1,2}日?)", txt)
            if m:
                return m.group(1)
        # 精确类名（带连字符的完整类名）优先，泛词（company/tag 等）最后，避免 company 误命中 company-location
        precise = [h for h in hints if "-" in h]
        generic = [h for h in hints if "-" not in h]
        for kw in precise + generic:
            for node in el.iter():
                cls = (node.get("class") or "").lower().replace("_", "-")
                if kw not in cls:
                    continue
                if node.tag in ("ul", "ol") and node.cssselect("li"):
                    parts = [re.sub(r"\s+", " ", (li.text_content() or "").strip())
                             for li in node.cssselect("li")]
                    parts = [x for x in parts if x]
                    if parts:
                        return "、".join(parts[:6])
                if node.tag in ("a", "span", "div", "li", "p", "h1", "h2", "h3", "h4",
                                "strong", "em", "td", "dt", "dd", "b", "i"):
                    t = re.sub(r"\s+", " ", (node.text_content() or "").strip())
                    if t:
                        return t
        return ""

    def _self_heal_rows(self, html: str) -> List[Any]:
        """通用兜底：统计 li/div/tr 里出现最多的 class，取该重复块作为列表行。"""
        from collections import Counter
        try:
            from lxml import html as _lh
            doc = _lh.fromstring(html)
        except Exception:
            return []
        counts = Counter()
        for tag in ("li", "div", "tr"):
            for el in doc.cssselect(tag):
                cls = (el.get("class") or "").strip()
                if cls:
                    counts[(tag, cls)] += 1
        best = None
        best_n = 0
        for (tag, cls), n in counts.items():
            if n >= 3 and n > best_n:
                # 该重复块里至少要有链接，才像列表行
                try:
                    if doc.cssselect(f"{tag}.{cls} a[href]"):
                        best, best_n = (tag, cls), n
                except Exception:
                    pass
        if best is None:
            return []
        tag, cls = best
        els = doc.cssselect(f"{tag}.{cls}")
        # 去重（嵌套重复块只取最外层）
        out = []
        for el in els:
            txt = (el.text_content() or "").strip()
            a = el.cssselect("a[href]")
            link = a[0].get("href", "") if a else ""
            title = (a[0].text_content() or "").strip() if a else (txt[:80])
            if not title and not link:
                continue
            if any(el is not o and el in o.iter() for o in out):
                continue
            out.append(el)
        return out[:200]

    def _map_html(self, html: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        for name, spec in fields.items():
            if isinstance(spec, dict):
                if spec.get("type") == "json":
                    out[name] = apply_extractor(spec, html, html, None)
                else:
                    out[name] = self._css_value(html, spec)
            else:
                out[name] = apply_extractor(spec, html, html, None)
        return out


class LLMParser(BaseParser):
    """LLM 智能解析：任意 HTML/文本 → 结构化 JSON（ScrapeGraphAI/Firecrawl 方向）。
    配置: parsers.<name>: {"type": "llm", "schema": {"标题": "...", "价格": "..."}, "instruction": "..."}
    """
    name = "llm"

    def parse(self, resp: Response, ctx: ParseContext) -> ParseResult:
        from ..llm import LLMClient
        schema = self.config.get("schema", {})
        text = resp.text
        # 优先给纯文本（去掉标签）
        from ..extractors import html_to_markdown
        if "<" in text[:500]:
            text = html_to_markdown(text)[:8000] or re.sub(r"<[^>]+>", " ", resp.text)[:8000]
        client = LLMClient()
        data = client.extract_json(text, schema, self.config.get("instruction", ""))
        return ParseResult(items=[data if isinstance(data, dict) else {"data": data}], requests=[])


class ArticleParser(BaseParser):
    """正文抽取解析器：任意新闻/文章页 → {title, body}（trafilatura/readability 方向）。"""
    name = "article"

    def parse(self, resp: Response, ctx: ParseContext) -> ParseResult:
        from ..extractors import extract_article
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.S)
        if m:
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        body = extract_article(resp.text)
        return ParseResult(items=[{"title": title, "body": body}], requests=[])


class TableParser(BaseParser):
    """表格解析器：把页面所有表格抽成行（pandas.read_html 风格，lxml 实现）。"""
    name = "table"

    def parse(self, resp: Response, ctx: ParseContext) -> ParseResult:
        from ..extractors import extract_tables
        items = []
        for rows in extract_tables(resp.text):
            for row in rows:
                row["_url"] = resp.url
                items.append(row)
        return ParseResult(items=items, requests=[])


class JsonPagedParser(BaseParser):
    """JSON API 声明式分页解析器（对标 Crawlee PaginatedList / v2 page_param+offset）。
    配置 parsers.<name>:
      {"type": "json_paged", "records_path": "data.records", "fields": {...},
       "strategy": "page_param"|"offset"|"next_url",
       "page_param": "page", "start": 1, "max_pages": 10, "page_size": 20,
       "total_path": "data.total", "offset_param": "offset", "next_path": "data.next"}

    由 HttpFetcher 负责把 meta.params 拼进 URL；本解析器产出 items + 下一页 Request。
    """
    name = "json_paged"

    def parse(self, resp: Response, ctx: ParseContext) -> ParseResult:
        cfg = self.config or {}
        data = resp.json
        if not isinstance(data, dict):
            return ParseResult(items=[])
        rows = jpath(data, cfg.get("records_path", ""), [])
        if not isinstance(rows, list):
            rows = [rows] if rows is not None else []
        items = [self._map(r, cfg.get("fields", {})) for r in rows if isinstance(r, dict)]
        result = ParseResult(items=items)

        strategy = cfg.get("strategy", "page_param")
        max_pages = int(cfg.get("max_pages", 1))
        page = int((resp.request.meta or {}).get("page", 0)) or int(cfg.get("start", 1))
        if not (resp.request.meta or {}).get("page"):
            # 从 URL 查询参数推断起始页（--url 覆盖入口时无需 meta.page）
            import urllib.parse as _up
            qs = _up.parse_qs(_up.urlsplit(resp.request.url).query)
            pp = cfg.get("page_param") or cfg.get("offset_param")
            if pp and pp in qs:
                try:
                    val = int(qs[pp][0])
                except (ValueError, TypeError):
                    val = None
                if val is not None:
                    if cfg.get("strategy") == "offset" and cfg.get("page_size"):
                        page = val // int(cfg["page_size"]) + 1
                    else:
                        page = val
        if page >= max_pages:
            return result
        total = jpath(data, cfg.get("total_path", ""), None) if cfg.get("total_path") else None
        page_size = int(cfg.get("page_size", len(rows) or 1))
        total_n = None
        if total is not None:
            try:
                # 兼容 "1,000" / "1000" / 1000 等格式；纯非数字视为"未知"→继续翻页（由 max_pages 兜底）
                _digits = re.sub(r"[^\d]", "", str(total))
                total_n = int(_digits) if _digits else None
            except (TypeError, ValueError):
                total_n = None

        # 还有下一页？
        if total_n is not None:
            if page * page_size >= total_n:
                return result
        elif not rows:
            return result

        params = dict((resp.request.meta or {}).get("params", {}))
        new_req = None
        if strategy == "page_param":
            params[cfg.get("page_param", "page")] = page + 1
            new_req = Request(url=resp.request.url, depth=resp.request.depth,
                              meta={**(resp.request.meta or {}), "params": params, "page": page + 1})
        elif strategy == "offset":
            params[cfg.get("offset_param", "offset")] = page * page_size
            new_req = Request(url=resp.request.url, depth=resp.request.depth,
                              meta={**(resp.request.meta or {}), "params": params, "page": page + 1})
        elif strategy == "next_url":
            nxt = jpath(data, cfg.get("next_path", "next"), None)
            if nxt:
                from urllib.parse import urljoin
                base = resp.url or (resp.request.url if resp.request else "")
                new_req = Request(url=urljoin(base, str(nxt)), depth=resp.request.depth,
                                  meta={**(resp.request.meta or {}), "page": page + 1})
        if new_req is not None:
            result.requests.append(new_req)
        return result

    def _map(self, row: dict, fields: dict) -> dict:
        from ..engine_v3 import map_record
        return map_record(row, fields or {})
