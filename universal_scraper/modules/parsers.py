#!/usr/bin/env python3
"""内置解析器：声明式（JSON路径/CSS/XPath/正则）。"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from ..protocols import BaseParser, ParseResult, Response, ParseContext, Request
from ..selectors import jpath, css_text, css_attr, xpath_text, apply_extractor, regex_extract
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
        """取字段值：优先显式 attr；兼容 css 里的 ::attr(name) 写法（如 .p1 a::attr(href)）。"""
        from ..selectors import css_attr, css_text, xpath_text, regex_extract
        css = fspec.get("css") or ""
        if fspec.get("attr"):
            return css_attr(el_html, css, fspec["attr"], fspec.get("limit", limit))
        if "::attr(" in css:
            import re as _re
            m = _re.search(r"::attr\(([^)]*)\)", css)
            attr = m.group(1).strip().strip("'\"") if m else "href"
            return css_attr(el_html, css, attr, fspec.get("limit", limit))
        if fspec.get("xpath"):
            return xpath_text(el_html, fspec["xpath"], fspec.get("limit", limit))
        if css:
            return css_text(el_html, css, fspec.get("limit", limit))
        if fspec.get("regex"):
            return regex_extract(el_html, fspec["regex"], fspec.get("group", 0))
        return ""


    def _html_rows(self, html: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        if cfg.get("row_xpath"):
            from ..selectors import xpath_elements, _lxml_html_tostring
            els = xpath_elements(html, cfg["row_xpath"])
        else:
            from ..selectors import css_elements, _lxml_html_tostring
            els = css_elements(html, cfg.get("row_css") or "body")
        out = []
        for el in els:
            el_html = el if isinstance(el, str) else _lxml_html_tostring(el)
            row = {}
            for name, fspec in (cfg.get("fields", {}) or {}).items():
                if isinstance(fspec, dict):
                    row[name] = self._css_value(el_html, fspec)
                else:
                    row[name] = el_html
            out.append(row)
        return out

    def _map_html(self, html: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        from ..selectors import css_text, css_attr, xpath_text
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
            pp = cfg.get("page_param") or (cfg.get("offset_param") and None)
            if pp and pp in qs:
                try:
                    page = int(qs[pp][0])
                except (ValueError, TypeError):
                    pass
        if page >= max_pages:
            return result
        total = jpath(data, cfg.get("total_path", ""), None) if cfg.get("total_path") else None
        page_size = int(cfg.get("page_size", len(rows) or 1))

        # 还有下一页？
        if total is not None:
            if page * page_size >= int(total):
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
