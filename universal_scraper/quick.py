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
import re
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
        # SPA 壳判型提示（版权中心战例：3.9KB 壳完全靠人眼识别）
        if 0 < len(text) < 6144 and re.search(r'id="(?:app|root|__next)"', text):
            result["recon_hint"] = ("页面为 SPA 壳（无实质内容）——数据靠 JS/接口，"
                                    "先 jsrecon 找接口或 capture 捕获（配方 R8/R13）")
    if links:
        from .queue import extract_links
        result["links"] = extract_links(text, url, links_allow, links_deny)
    if screenshot:
        result["screenshot"] = screenshot if Path(screenshot).exists() else None
    return result


def js_recon(url: str, max_scripts: int = 6, out: Optional[str] = None) -> Dict[str, Any]:
    """接口侦察（版权中心战例）：下载页面 JS 包 → 提取候选 API 端点。
    安全边界：仅 http/https；host 解析到私网/环回/保留地址即拒绝。"""
    import ipaddress
    import socket
    from urllib.parse import urljoin, urlsplit
    sp = urlsplit(url)
    if sp.scheme not in ("http", "https"):
        return {"error": f"仅允许 http/https: {url}"}
    try:
        for info in socket.getaddrinfo(sp.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                return {"error": f"拒绝私有/保留地址: {sp.hostname} -> {ip}"}
    except socket.gaierror as e:
        return {"error": f"域名解析失败: {e}"}

    from .core import make_http_client
    client = make_http_client({"min_interval": 0.5, "timeout": 20, "http_backend": "auto"})
    res = client.get(url)
    html = res.get("text", "")
    if not html:
        return {"error": f"页面获取失败: HTTP {res.get('status')}"}
    scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
    urls = [urljoin(url, s) for s in scripts if s]
    api_pat = re.compile(
        r'(?:["\'])(/[A-Za-z0-9_\-]*/(?:api|service|gateway|rest|query|search|inquiry)[/\w\-./]*'
        r'|https?://[\w.\-]+/(?:api|gateway|service)[/\w\-./]*'
        r'|baseURL[:\s]*["\']([^"\']{4,120})["\'])')
    # batch1401 战训：webpack 压缩包会吐 "baseURL\"),E=i(\" 这类拼接噪声。
    # 过滤规则：剥离转义引号后必须是"看起来像 URL/路径"的串。
    def _clean_hit(h: str) -> str:
        h = h.replace('\\"', '').replace("\\'", "").strip()
        return h

    def _plausible(h: str) -> bool:
        h2 = _clean_hit(h)
        if len(h2) < 4:
            return False
        # 必须以 / 或协议开头，或含域名特征；拒绝残留代码符号的拼接噪声
        if not (h2.startswith(("/", "http://", "https://", "${"))):
            return False
        if re.search(r'[=;{}()<>]', h2):  # 代码残渣
            return False
        return True

    found: Dict[str, List[str]] = {}
    base_urls: List[str] = []  # axios/fetch baseURL 优先单列（改写 http_json 的锚点）
    frags: List[str] = []      # batch1600 战训：压缩包里散落的路径碎片（低置信，agent 自行拼接）
    checked = 0
    for su in urls:
        if checked >= max_scripts:
            break
        try:
            jr = client.get(su)
            body = jr.get("text", "")[:2_000_000]
            if not body:
                continue
            checked += 1
            raw_hits = api_pat.findall(body)
            hits = sorted({_clean_hit(m[0] or m[1] or "") for m in raw_hits})
            hits = [h for h in hits if h and _plausible(h)][:60]
            # axios 实例 baseURL（baseURL:"/api" 或 baseURL:"https://x"）单独收集
            for bm in re.finditer(r'baseURL\s*[:=]\s*["\']([^"\']{4,120})["\']', body):
                c = _clean_hit(bm.group(1))
                if _plausible(c):
                    base_urls.append(c)
            # 路径碎片：带 ≥2 段的引号路径（拼接产物 "/a/b"），与已确认 hits 去重
            for fm in re.finditer(r'["\'](/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-./]+){1,4})["\']', body):
                f = _clean_hit(fm.group(1))
                if _plausible(f) and f not in hits and not f.endswith((".js", ".css", ".png", ".svg")):
                    frags.append(f)
            if hits:
                found[su.rsplit("/", 1)[-1][:48] or su] = hits
        except Exception:
            continue
    all_hits = sorted({h for v in found.values() for h in v})
    result = {"url": url, "scripts_checked": checked, "api_candidates": all_hits,
              "base_urls": sorted(set(base_urls))[:20],
              "path_fragments": sorted(set(frags))[:40],
              "by_script": found, "hint": "候选端点需逐个探测验证（带 UA/Referer/cookie 预热）"}
    if out:
        import os as _os
        p = Path(_os.path.expanduser(out))
        if p.is_dir():  # batch1401 战训：--out 传目录不再抛 IsADirectoryError
            p = p / "jsrecon.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        result["saved"] = str(p)
    return result


def save_result(result: Dict[str, Any], out: Optional[str] = None,
                as_json: bool = False) -> Path:
    """把结果写到文件（默认 .md；--json 则 .json）。返回路径。"""
    if out:
        import os as _os
        fp = Path(_os.path.expanduser(out))
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
