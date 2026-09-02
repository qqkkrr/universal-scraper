#!/usr/bin/env python3
"""一键抓取（对标 Firecrawl CLI）：URL → 干净文本 / Markdown / 结构化 JSON。

用法（CLI）:
  python3 -m universal_scraper.cli fetch <url> [--browser] [--selector "h1"] [--article] [--table] [--json] [--out file.md] [--proxy http://...]

也可作库调用:
  from universal_scraper.quick import fetch_url
  result = fetch_url("https://example.com", browser=True, selector=".content")
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def fetch_url(url: str, browser: bool = False, selector: Optional[str] = None,
              article: bool = False, table: bool = False, proxy: Optional[str] = None,
              actions: Optional[List[dict]] = None, js: Optional[str] = None,
              wait_selector: Optional[str] = None, stealth: bool = True,
              remove_overlays: bool = True, timeout: float = 60,
              links: bool = False, links_allow: Optional[str] = None,
              links_deny: Optional[str] = None,
              screenshot: Optional[str] = None,
              cookie: Optional[str] = None,
              headers: Optional[Dict[str, str]] = None,
              cdp: Optional[str] = None) -> Dict[str, Any]:
    """抓取一个 URL，返回 {url, status, text, markdown?, selector?, article?, tables?, links?}。
    links=True 时额外提取页面所有外链（对标 Firecrawl scrape links）。
    - browser=False: 走 HTTP（curl_cffi→requests→urllib 自动选后端）
    - browser=True : 走浏览器桥（JS 渲染 / SPA / 需要动作链的页面）
    """
    result: Dict[str, Any] = {"url": url, "status": 0, "text": "", "error": ""}
    if screenshot and not browser:
        browser = True  # 截图需要浏览器渲染
    if not browser:
        from .core import make_http_client
        _anti = {"min_interval": 0.2, "timeout": timeout,
                 "http_backend": "auto", "proxy": proxy}
        _hdrs = dict(headers or {})
        if cookie:
            _hdrs["Cookie"] = cookie
        if _hdrs:
            _anti["headers"] = _hdrs
        client = make_http_client(_anti)
        res = client.get(url)
        result["status"] = res.get("status", 0)
        result["url"] = res.get("url") or url
        if not res.get("ok"):
            result["error"] = res.get("text", "请求失败")[:300]
            return result
        result["text"] = res.get("text", "")
    else:
        from .modules.fetchers import BrowserFetcher
        from .protocols import Request
        cfg: Dict[str, Any] = {"type": "browser", "pool": True, "scroll_count": 0,
                               "stealth": stealth, "remove_overlays": remove_overlays}
        if screenshot:
            actions = list(actions or []) + [{"type": "screenshot", "path": screenshot}]
        if actions:
            cfg["actions"] = actions
        if js:
            cfg["js_pre"] = js
        if wait_selector:
            cfg["wait_selector"] = wait_selector
        if cdp:
            cfg["cdp"] = cdp  # 附加调试 Chrome：侦察与正式采集同通道、过 Cloudflare
        anti = {"session_dir": "/tmp/us_fetch_session", "min_interval": 0.1,
                "http_backend": "auto"}
        if proxy:
            anti["proxy"] = proxy
        fetcher = BrowserFetcher(cfg, {}, anti)
        try:
            resp = fetcher.fetch(Request(url=url))
        finally:
            fetcher.close()
        result["status"] = resp.status
        result["url"] = resp.url or url
        result["text"] = resp.text
        if not resp.text:
            result["error"] = "浏览器渲染后无内容"

    text = result["text"]
    if selector:
        from .selectors import css_text
        result["selector"] = css_text(text, selector, limit=2_000_000)
    if article:
        from .extractors import extract_article
        result["article"] = extract_article(text)
    if table:
        from .extractors import extract_tables
        result["tables"] = extract_tables(text)
    if not (selector or article or table):
        from .extractors import html_to_markdown
        result["markdown"] = html_to_markdown(text) or text[:200_000]
    if links:
        from .queue import extract_links
        result["links"] = extract_links(text, url, links_allow, links_deny)
    if screenshot:
        result["screenshot"] = screenshot if Path(screenshot).exists() else None
    return result


def save_result(result: Dict[str, Any], out: Optional[str] = None,
                as_json: bool = False) -> Path:
    """把结果写到文件（默认 .md；--json 则 .json）。返回路径。"""
    if out:
        fp = Path(out)
    else:
        import hashlib
        ext = ".json" if as_json else ".md"
        h = hashlib.md5(str(result.get("url", "")).encode(), usedforsecurity=False).hexdigest()[:12]
        fp = Path(f"outputs/fetch_{h}{ext}")
    fp.parent.mkdir(parents=True, exist_ok=True)
    if as_json:
        fp.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    else:
        content = result.get("markdown") or result.get("article") or result.get("selector") or result.get("text", "")
        fp.write_text(content, encoding="utf-8")
    return fp


def crawl_url(url: str, depth: int = 2, max_pages: int = 100, allow: Optional[str] = None,
              deny: Optional[str] = None, browser: bool = False, proxy: Optional[str] = None,
              concurrency: int = 4, out: Optional[str] = None,
              respect_robots: bool = False, sitemap: Optional[str] = None,
              same_domain: bool = False) -> Dict[str, Any]:
    """从 URL 递归爬站（对标 Firecrawl crawl CLI）：
    自动生成临时任务包 → v3 引擎递归抓取 → 导出 outputs/<out>.json/csv/xlsx。
    - allow/deny: 正则过滤链接（只爬 /docs/ 等）
    - browser=True: JS 渲染页面
    返回 {name, total, fetched, errors, base_name, files}
    """
    import hashlib
    import json as _json
    import re
    import shutil
    from pathlib import Path
    from urllib.parse import urlparse

    h = hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:10]
    work = Path("outputs/.crawl_tmp")
    tdir = work / f"crawl_{h}"
    shutil.rmtree(tdir, ignore_errors=True)
    (tdir / "modules").mkdir(parents=True, exist_ok=True)
    host = re.sub(r"[^A-Za-z0-9_-]", "_", urlparse(url).netloc or "site")
    base = out or f"crawl_{host}"
    cfg = {
        "name": f"crawl_{h}",
        "start_urls": [url],
        "queue": {"max_depth": depth, "max_requests": max_pages, "max_concurrency": concurrency},
        "source": {"type": "browser", "pool": True, "stealth": True, "remove_overlays": True}
        if browser else {"type": "http"},
        "rules": [{"match": "regex", "pattern": ".*", "parser": "page"}],
        "parsers": {"page": {
            "type": "html",
            "fields": {"title": {"css": "title", "limit": 300},
                       "text": {"css": "body", "limit": 100000}},
            "extract_links": {"allow": allow, "deny": deny, "same_domain": same_domain},
        }},
        "pipelines": [{"type": "filter", "field": "text", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": f"crawl_{h}"},
        "output": {"dir": "outputs", "base_name": base},
        "anti_bot": {"min_interval": 0.2, "max_retries": 2, "respect_robots": respect_robots},
    }
    if sitemap:
        cfg["source"]["sitemap"] = sitemap  # 对标 Crawlee SitemapRequestLoader：sitemap 作为种子
    if proxy:
        cfg["anti_bot"]["proxy"] = proxy
    (tdir / "config.json").write_text(_json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    from .engine_v3 import run_task
    try:
        # 页数上限由 queue.max_requests 控制（--max 是页数不是条目数）
        result = run_task(tdir)
    finally:
        shutil.rmtree(tdir, ignore_errors=True)
        # 临时 jsonl 也清理（异常路径不残留）
        try:
            (Path("outputs/items") / f"crawl_{h}.jsonl").unlink(missing_ok=True)
        except Exception:
            pass
    result["base_name"] = base
    # 只报真实存在的导出文件，防止"导出失败还报成功"误导
    files = {}
    for ext in ("json", "csv", "xlsx"):
        fp = Path("outputs") / f"{base}.{ext}"
        if fp.exists():
            files[ext] = str(fp)
    result["files"] = files
    return result
