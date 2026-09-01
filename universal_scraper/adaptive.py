#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""万能自适应引擎：自动探测目标站特征 → 选择最优取数策略 → 自修复闭环。

升级链: HTTP直抓 → TLS指纹伪装 → 无头浏览器 → 有头浏览器(登录提示)
每次失败自动升级；成功后缓存策略到站点注册表，下次跳过试错。

用法:
  python3 -m universal_scraper.adaptive "https://target.com/list"
 或在 Python: from universal_scraper.adaptive import adaptive_fetch
"""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from curl_cffi import requests as cffi_requests

STRATEGY_FILE = Path(__file__).resolve().parent.parent / "configs" / "_adaptive_strategies.json"
ESCALATION = ["http", "curl_cffi", "browser_headless", "browser_visible"]
LABELS = {"http": "HTTP 直抓", "curl_cffi": "TLS 指纹伪装",
          "browser_headless": "无头浏览器", "browser_visible": "有头浏览器"}


def _load_strategies() -> dict:
    if STRATEGY_FILE.exists():
        try:
            return json.loads(STRATEGY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_strategies(data: dict):
    STRATEGY_FILE.parent.mkdir(parents=True, exist_ok=True)
    STRATEGY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def get_cached_strategy(url: str):
    domain = urlsplit(url).hostname or ""
    return _load_strategies().get(domain, {}).get("best_strategy")


def save_strategy(url: str, strategy: str):
    domain = urlsplit(url).hostname or ""
    if not domain:
        return
    data = _load_strategies()
    data.setdefault(domain, {})["best_strategy"] = strategy
    data.setdefault(domain, {})["updated"] = time.strftime("%Y-%m-%d %H:%M")
    _save_strategies(data)


def _detect_block(text: str, status: int) -> str:
    t = (text or "")[:5000].lower()
    if status in (403, 406, 412):
        return "captcha" if ("验证" in t or "captcha" in t) else "waf"
    if status == 429:
        return "rate_limit"
    if "百度安全验证" in t:
        return "captcha"
    if "wappass" in t or "verify.meituan" in t:
        return "login"
    # JS challenge 壳页特征：短页面 + 含跳转/挑战代码（不能仅凭短就判定——短页面可能合法）
    if len(t) < 300 and re.search(r"document\.(?:location|write)|setTimeout.*location|__js_challenge|stoken", t):
        return "js_challenge"
    return "none"


# ── 策略实现 ──

def _fetch_http(url: str, timeout: int = 20):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    resp = opener.open(req, timeout=timeout)
    return resp.status, resp.read().decode("utf-8", "ignore")


def _fetch_curl_cffi(url: str, timeout: int = 25):
    r = cffi_requests.get(url, timeout=timeout, impersonate="chrome")
    return r.status_code, r.text


def _fetch_browser(url: str, headless: bool):
    """用已有的 BrowserFetcher 类（内部安全处理子进程）。"""
    import importlib
    fm = importlib.import_module(".modules.fetchers", package="universal_scraper")
    from .protocols import Request as Req
    source = {"type": "browser", "url": url, "pool": False,
              "headless": headless, "scroll_count": 2,
              "scroll_wait_ms": 2000, "pagination": {"type": "none"}}
    fetcher = fm.BrowserFetcher(source, {}, {"session_dir": "/tmp/us_adaptive"})
    resp = fetcher.fetch(Req(url=url))
    return resp.status or 200, resp.text or ""


# ── 核心入口 ──

def adaptive_fetch(url: str, keyword: str = "", log=None) -> dict:
    """自适应取数：自动升级策略直到成功。返回 {strategy, status, html, error}。"""
    _log = log or (lambda m: print(m))

    domain = urlsplit(url).hostname or ""
    cached = get_cached_strategy(url)
    start_idx = ESCALATION.index(cached) if cached in ESCALATION else 0

    last_err, html, status = "", "", 0

    for i, strategy in enumerate(ESCALATION[start_idx:], start_idx):
        label = LABELS.get(strategy, strategy)
        _log(f"🔄 [{domain}] 策略 {i+1}/{len(ESCALATION)}: {label}…")
        try:
            if strategy == "http":
                status, html = _fetch_http(url)
            elif strategy == "curl_cffi":
                status, html = _fetch_curl_cffi(url)
            elif strategy in ("browser_headless", "browser_visible"):
                headless = strategy == "browser_headless"
                status, html = _fetch_browser(url, headless)
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
            _log(f"  ⚠️ {label} 异常: {last_err}")
            continue

        block = _detect_block(html, status)
        if block == "none" and html.strip():
            _log(f"  ✅ {label} 成功（{len(html)} 字符）")
            save_strategy(url, strategy)
            return {"strategy": strategy, "status": status, "html": html,
                    "items": [], "error": ""}

        _log(f"  ⚠️ {label} 被拦截[{block}]: {len(html)} 字符")
        last_err = f"拦截类型 {block}"

        if block == "login":
            _log("  💡 请在浏览器中登录该网站，然后点「🍪 导入会话」→「重跑」")
            save_strategy(url, "browser_visible")

    save_strategy(url, "failed")
    _log(f"❌ 所有策略均失败: {last_err}")
    return {"strategy": "failed", "status": 0, "html": "", "items": [],
            "error": f"所有策略均失败: {last_err}"}


# ── 独立运行 ──

if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    result = adaptive_fetch(url, log=print)
    print(json.dumps({"strategy": result["strategy"], "status": result["status"],
                      "html_len": len(result["html"]), "error": result["error"]},
                     ensure_ascii=False))
