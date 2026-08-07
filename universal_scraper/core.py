#!/usr/bin/env python3
"""万能爬虫框架核心（zero-dependency，纯标准库）。

设计理念：几乎所有"爬虫场景"都可以拆成 4 件事 ——
  1) 取数据（HTTP 静态页 / JSON API / 需浏览器渲染的页面）
  2) 反爬应对（限速、重试、UA 轮换、代理、浏览器指纹）
  3) 遍历（分页 / 列表→详情）
  4) 导出（CSV / JSON / Excel）

本模块提供这些基础能力，具体站点只需写一个很小的"适配器"。
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import zlib
import io
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

# ---------------------------------------------------------------- 日志

def log(msg: str, level: str = "INFO") -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> "NoReturn":
    log(msg, "ERROR")
    raise SystemExit(1)


# ---------------------------------------------------------------- HTTP 客户端

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

UA_POOL = [
    DEFAULT_UA,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


def _decode_body(raw: bytes, headers: Optional[Dict[str, str]] = None) -> str:
    """按 Content-Type charset / BOM / <meta charset> 解码（GBK/GB2312 等中文站）。"""
    enc = "utf-8"
    ct = (headers or {}).get("content-type", "")
    m = re.search(r"charset=([\w-]+)", ct, re.I)
    if m:
        enc = m.group(1)
    elif raw[:3] == b"\xef\xbb\xbf":
        enc = "utf-8-sig"
    else:
        head = raw[:2048].decode("utf-8", "ignore").lower()
        m2 = re.search(r"charset=[\"']?([\w-]+)", head)
        if m2:
            enc = m2.group(1)
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


@dataclass
class HttpClient:
    """通用 HTTP 客户端：重试 + 退避 + 限速 + UA 轮换 + 代理 + 缓存。"""

    min_interval: float = 1.0          # 每次请求最小间隔（秒），防限流
    max_retries: int = 3
    backoff_base: float = 2.0          # 指数退避基数
    timeout: float = 20
    rotate_ua: bool = True
    proxy: Optional[str] = None        # 例如 http://127.0.0.1:7890
    use_system_proxy: bool = False     # False = 直连（绕过 macOS 系统代理/Clash）
    cache_dir: Optional[Path] = None   # 设置后按 URL+body 缓存响应
    extra_headers: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._last_ts = 0.0
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- 限速 --
    def _throttle(self) -> None:
        elapsed = time.time() - self._last_ts
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_ts = time.time()

    def _headers(self, extra: Optional[Dict[str, str]]) -> Dict[str, str]:
        h = {
            "User-Agent": random.choice(UA_POOL) if self.rotate_ua else DEFAULT_UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        h.update(self.extra_headers)
        if extra:
            h.update(extra)
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return h

    def _opener(self) -> urllib.request.OpenerDirector:
        return self._opener_for(None)

    def _opener_for(self, proxy: Optional[str]) -> urllib.request.OpenerDirector:
        handlers: List[Any] = []
        p = proxy if proxy is not None else self.proxy
        if p:
            handlers.append(urllib.request.ProxyHandler({
                "http": p, "https": p}))
        elif not self.use_system_proxy:
            # 显式禁用代理：urllib 默认会读 macOS 系统代理（Clash），
            # 经代理做 HTTPS 时 TLS 握手可能无限挂起
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    def _cache_key(self, url: str, body: bytes, method: str) -> str:
        raw = f"{method}|{url}|{body.decode('utf-8', 'replace')}"
        return hashlib.md5(raw.encode()).hexdigest() + ".json"

    def request(
        self,
        url: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        use_cache: bool = False,
        allow_html_404: bool = False,
        proxy: Optional[str] = None,
    ) -> Dict[str, Any]:
        """返回 {'ok':bool, 'status':int, 'body':bytes, 'json':dict|None, 'text':str, 'headers':dict}。
        proxy 传入时本请求走该代理（覆盖实例级 proxy，None 表示用实例配置）。"""
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        # 非 ASCII URL（中文参数等）自动百分号编码，urllib 才能请求
        try:
            url.encode("ascii")
        except UnicodeEncodeError:
            from urllib.parse import urlsplit, urlunsplit, quote
            _p = urlsplit(url)
            url = urlunsplit((_p.scheme, _p.netloc,
                              quote(_p.path, safe="/%"),
                              quote(_p.query, safe="=&%+?/"),
                              _p.fragment))
        body_bytes = b""
        if data is not None:
            body_bytes = urllib.parse.urlencode(data).encode()
        elif json_data is not None:
            body_bytes = json.dumps(json_data, ensure_ascii=False).encode()
            headers = dict(headers or {})
            headers["Content-Type"] = "application/json"

        if use_cache and self.cache_dir and method == "GET" and not body_bytes:
            cf = self.cache_dir / self._cache_key(url, b"", method)
            if cf.exists():
                try:
                    return json.loads(cf.read_text(encoding="utf-8"))
                except Exception:
                    pass

        result: Optional[Dict[str, Any]] = None
        last_err = ""
        _old_dt = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.timeout)  # 兜底：TLS 握手也受超时约束
        try:
            result = self._request_once(url, body_bytes, method, headers, use_cache,
                                        allow_html_404=allow_html_404, proxy=proxy)
        finally:
            socket.setdefaulttimeout(_old_dt)
        return result

    def _request_once(self, url, body_bytes, method, headers, use_cache,
                      allow_html_404=False, proxy=None):
        last_err = ""
        last_headers: Dict[str, str] = {}
        last_status = 0
        result: Optional[Dict[str, Any]] = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            req = urllib.request.Request(
                url, data=body_bytes or None,
                headers=self._headers(headers), method=method,
            )
            try:
                opener = self._opener_for(proxy)
                with opener.open(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    enc = resp.headers.get("Content-Encoding", "")
                    if enc.lower() == "gzip":
                        try:
                            raw = gzip.decompress(raw)
                        except Exception:
                            pass
                    elif enc.lower() == "deflate":
                        try:
                            raw = zlib.decompress(raw)
                        except Exception:
                            pass
                    status = getattr(resp, "status", 200)
                    ctype = resp.headers.get("Content-Type", "")
                    parsed: Any = None
                    if "json" in ctype or raw[:1] in (b"{", b"["):
                        try:
                            parsed = json.loads(raw)
                        except Exception:
                            parsed = None
                    result = {
                        "ok": True,
                        "status": status,
                        "body": raw,
                        "text": _decode_body(raw, {k.lower(): v for k, v in resp.headers.items()}),
                        "json": parsed,
                        "url": resp.geturl() or url,
                        "headers": {k.lower(): v for k, v in resp.headers.items()},
                    }
                    break
            except urllib.error.HTTPError as e:
                raw = e.read()
                status = e.code
                # WAF/反爬常返回 404 的"假页面"，allow_html_404 表示接受这种 HTML 响应
                if allow_html_404 and status == 404:
                    result = {
                        "ok": True, "status": status, "body": raw,
                        "text": _decode_body(raw, {k.lower(): v for k, v in e.headers.items()} if e.headers else {}),
                        "json": None, "url": url,
                        "headers": {k.lower(): v for k, v in e.headers.items()} if e.headers else {},
                    }
                    break
                if status in (429, 403, 500, 502, 503, 504):
                    last_err = f"HTTP {status}"
                    last_status = status
                    last_headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
                    if status == 429 and last_headers.get("retry-after"):
                        # 429 + Retry-After：不内部退避，立即交还引擎按服务端要求调度（避免耗掉窗口）
                        result = {"ok": False, "status": status, "body": raw,
                                  "text": _decode_body(raw, last_headers),
                                  "json": None, "url": url, "headers": last_headers}
                        break
                    wait = self.backoff_base ** attempt + random.uniform(0, 1)
                    log(f"  请求失败 {status}，{wait:.1f}s 后重试（{attempt}/{self.max_retries}）", "WARN")
                    time.sleep(wait)
                    continue
                last_err = f"HTTP {status}: {_decode_body(raw, {k.lower(): v for k, v in e.headers.items()} if e.headers else {})[:200]}"
                result = {"ok": False, "status": status, "body": raw,
                          "text": _decode_body(raw, {k.lower(): v for k, v in e.headers.items()} if e.headers else {}),
                          "json": None, "url": url,
                          "headers": {k.lower(): v for k, v in e.headers.items()} if e.headers else {}}
                break
            except Exception as e:  # 网络/超时
                last_err = str(e)
                wait = self.backoff_base ** attempt
                log(f"  网络异常: {e}，{wait:.1f}s 后重试（{attempt}/{self.max_retries}）", "WARN")
                time.sleep(wait)

        if result is None:
            return {"ok": False, "status": last_status, "body": b"", "text": last_err, "json": None, "url": url,
                    "headers": last_headers}
        if use_cache and result["ok"] and self.cache_dir and method == "GET" and not body_bytes:
            (self.cache_dir / self._cache_key(url, b"", method)).write_text(
                json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
        return result

    def get(self, url: str, **kw) -> Dict[str, Any]:
        return self.request(url, "GET", **kw)

    def post(self, url: str, **kw) -> Dict[str, Any]:
        return self.request(url, "POST", **kw)


# ---------------------------------------------------------------- HTML 解析（轻量，不依赖 bs4）

def html_find(html: str, tag: str, attrs: Optional[Dict[str, str]] = None, many: bool = False):
    """极简 HTML 元素查找（够用即可）。attrs 匹配属性子串。"""
    pattern = re.compile(r"<%s\b[^>]*>.*?</%s>|<%s\b[^>]*/>" % (tag, tag, tag), re.S)
    out = []
    for m in pattern.finditer(html):
        block = m.group(0)
        if attrs:
            if not all(f'{k}="' in block or f"{k}='" in block for k in attrs):
                continue
            if any(v and v not in block for v in attrs.values()):
                continue
        out.append(block)
    return out if many else (out[0] if out else None)


def html_text(block: str) -> str:
    """去掉标签、压缩空白。"""
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", "", block, flags=re.S)
    txt = re.sub(r"<[^>]+>", "", txt)
    return re.sub(r"\s+", " ", txt).strip()


# ---------------------------------------------------------------- 导出

def export_rows(rows: List[Dict[str, Any]], out_dir: Path, base_name: str) -> Dict[str, Path]:
    """同时导出 CSV + JSON + Excel（有 openpyxl 时）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, Path] = {}

    # JSON
    jp = out_dir / f"{base_name}.json"
    jp.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    paths["json"] = jp

    # CSV（字段并集，防超长）
    all_keys: List[str] = []
    for r in rows:
        for k in r:
            if k not in all_keys:
                all_keys.append(k)
    cp = out_dir / f"{base_name}.csv"
    with open(cp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (str(v) if v is not None else "") for k, v in r.items()})
    paths["csv"] = cp

    # Excel
    try:
        import openpyxl
        from openpyxl.styles import Font
        xp = out_dir / f"{base_name}.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = base_name[:28]
        ws.append(all_keys)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append([r.get(k, "") for k in all_keys])
        for col in ws.columns:
            letter = col[0].column_letter
            ws.column_dimensions[letter].width = min(max(len(str(c.value or "")) for c in col[:100]) + 2, 60)
        wb.save(xp)
        paths["xlsx"] = xp
    except ImportError:
        log("openpyxl 未安装，跳过 Excel 导出", "WARN")

    return paths


# ---------------------------------------------------------------- 通用分页遍历

def paginate(
    fetch_page: Callable[[int], Dict[str, Any]],
    page_size_key: str = "pageSize",
    total_key: str = "total",
    records_key: str = "records",
    max_pages: int = 1000,
    start_page: int = 1,
    on_page: Optional[Callable[[int, int, int], None]] = None,
) -> List[Dict[str, Any]]:
    """通用分页遍历：fetch_page(n) 返回 {'total':..., 'records':[...]}。"""
    all_records: List[Dict[str, Any]] = []
    page = start_page
    total = None
    while page <= max_pages:
        res = fetch_page(page)
        recs = res.get(records_key) or []
        total = res.get(total_key, total)
        if on_page:
            on_page(page, len(recs), total or 0)
        all_records.extend(recs)
        if not recs or total is None or page >= total / max(res.get(page_size_key, len(recs) or 1), 1):
            break
        page += 1
    return all_records


class RequestsClient:
    """基于 requests 的高性能客户端：连接池复用、gzip、会话 Cookie、限速、重试。"""

    def __init__(self, min_interval: float = 1.0, max_retries: int = 3,
                 timeout: float = 20, rotate_ua: bool = True,
                 proxy: Optional[str] = None, cookies: Optional[Dict[str, str]] = None,
                 extra_headers: Optional[Dict[str, str]] = None,
                 backoff_base: float = 2.0):
        import requests
        self.requests = requests
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.rotate_ua = rotate_ua
        self.proxy = proxy
        self.backoff_base = backoff_base
        self.extra_headers = dict(extra_headers or {})
        self._last_ts = 0.0
        self.session = requests.Session()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        if cookies:
            self.session.cookies.update(cookies)
        # 连接池
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_ts
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_ts = time.time()

    def request(self, url: str, method: str = "GET", params=None, data=None,
                json_data=None, headers=None, use_cache: bool = False,
                allow_html_404: bool = False, proxy: Optional[str] = None) -> Dict[str, Any]:
        h = {"Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
             "Accept-Encoding": "gzip, deflate"}
        h.update(self.extra_headers)
        if headers:
            h.update(headers)
        if self.rotate_ua:
            h["User-Agent"] = random.choice(UA_POOL)
        elif "User-Agent" not in h:
            h["User-Agent"] = DEFAULT_UA

        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                kw = {"params": params, "data": data, "json": json_data,
                      "headers": h, "timeout": self.timeout, "allow_redirects": True}
                if proxy:
                    kw["proxies"] = {"http": proxy, "https": proxy}
                resp = self.session.request(method.upper(), url, **kw)
                raw = resp.content
                if resp.status_code in (429, 403, 500, 502, 503, 504):
                    wait = self.backoff_base ** attempt + random.uniform(0, 1)
                    log(f"  请求失败 {resp.status_code}，{wait:.1f}s 后重试（{attempt}/{self.max_retries}）", "WARN")
                    time.sleep(wait)
                    continue
                parsed = None
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype or raw[:1] in (b"{", b"["):
                    try:
                        parsed = resp.json()
                    except Exception:
                        parsed = None
                return {"ok": True, "status": resp.status_code, "body": raw,
                        "text": resp.text if resp.text else _decode_body(raw, {k.lower(): v for k, v in resp.headers.items()}),
                        "json": parsed, "url": resp.url,
                        "headers": {k.lower(): v for k, v in resp.headers.items()}}
            except Exception as e:
                wait = self.backoff_base ** attempt
                log(f"  网络异常: {e}，{wait:.1f}s 后重试（{attempt}/{self.max_retries}）", "WARN")
                time.sleep(wait)
        return {"ok": False, "status": 0, "body": b"", "text": "requests 请求失败", "json": None, "url": url,
                "headers": {}}

    def get(self, url: str, **kw) -> Dict[str, Any]:
        return self.request(url, "GET", **kw)

    def post(self, url: str, **kw) -> Dict[str, Any]:
        return self.request(url, "POST", **kw)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 高级 HTTP 后端（可选依赖）

class CurlCffiClient:
    """curl_cffi 后端：伪装浏览器 TLS/JA3/HTTP2 指纹（curl-impersonate）。
    未安装 curl_cffi 时导入即抛 ImportError，调用方回退 requests/urllib。
    接口与 HttpClient 对齐：request/get/post。
    """

    def __init__(self, min_interval: float = 1.0, max_retries: int = 3,
                 timeout: float = 20, rotate_ua: bool = True,
                 proxy: Optional[str] = None, cookies: Optional[Dict[str, str]] = None,
                 extra_headers: Optional[Dict[str, str]] = None,
                 impersonate: str = "chrome"):
        import curl_cffi.requests  # noqa: F401  # 未安装则抛 ImportError
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.rotate_ua = rotate_ua
        self.proxy = proxy
        self.impersonate = impersonate
        self.extra_headers = dict(extra_headers or {})
        self.cookies = dict(cookies or {})
        self._last_ts = 0.0

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_ts
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_ts = time.time()

    def request(self, url: str, method: str = "GET", params=None, data=None,
                json_data=None, headers=None, use_cache: bool = False,
                allow_html_404: bool = False, proxy: Optional[str] = None) -> Dict[str, Any]:
        import curl_cffi.requests as cffi
        h = {"Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        h.update(self.extra_headers)
        if headers:
            h.update(headers)
        if not self.rotate_ua and "User-Agent" not in h:
            h["User-Agent"] = DEFAULT_UA
        proxy_use = proxy if proxy is not None else self.proxy
        kw = dict(impersonate=self.impersonate, timeout=self.timeout, headers=h,
                  allow_redirects=True, verify=True)
        if proxy_use:
            kw["proxies"] = {"http": proxy_use, "https": proxy_use}
        if self.cookies:
            kw["cookies"] = self.cookies
        last_err = ""
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = cffi.request(method.upper(), url, params=params,
                                    data=data, json=json_data, **kw)
                raw = resp.content
                if resp.status_code in (429, 403, 500, 502, 503, 504):
                    wait = self.backoff_base ** attempt + random.uniform(0, 1)
                    log(f"  curl_cffi 请求失败 {resp.status_code}，{wait:.1f}s 后重试", "WARN")
                    time.sleep(wait)
                    continue
                parsed = None
                if "json" in resp.headers.get("Content-Type", "") or raw[:1] in (b"{", b"["):
                    try:
                        parsed = resp.json()
                    except Exception:
                        parsed = None
                return {"ok": True, "status": resp.status_code, "body": raw,
                        "text": resp.text if resp.text else _decode_body(raw, {k.lower(): v for k, v in resp.headers.items()}),
                        "json": parsed,
                        "url": str(resp.url), "headers": {k.lower(): v for k, v in resp.headers.items()}}
            except Exception as e:
                last_err = str(e)
                wait = self.backoff_base ** attempt
                log(f"  curl_cffi 网络异常: {e}，{wait:.1f}s 后重试", "WARN")
                time.sleep(wait)
        return {"ok": False, "status": 0, "body": b"", "text": last_err,
                "json": None, "url": url, "headers": {}}

    @property
    def backoff_base(self) -> float:
        return 2.0

    def get(self, url: str, **kw) -> Dict[str, Any]:
        return self.request(url, "GET", **kw)

    def post(self, url: str, **kw) -> Dict[str, Any]:
        return self.request(url, "POST", **kw)


def make_http_client(anti: Dict[str, Any], **kw) -> Any:
    """按 anti.http_backend 选择 HTTP 客户端：curl_cffi（伪装指纹）→ requests → urllib。
    anti 可含: http_backend("auto"/"curl_cffi"/"requests"/"urllib"), impersonate,
               min_interval, max_retries, timeout, rotate_ua, proxy, cookies, headers。
    """
    backend = str(anti.get("http_backend", "auto")).lower()
    common = dict(
        min_interval=float(anti.get("min_interval", 1.0)),
        max_retries=int(anti.get("max_retries", 3)),
        timeout=float(anti.get("timeout", 20)),
        rotate_ua=bool(anti.get("rotate_ua", True)),
        proxy=anti.get("proxy"),
        cookies=anti.get("cookies") or {},
        extra_headers=anti.get("headers") or {},
    )
    common.update(kw)
    if backend in ("auto", "curl_cffi"):
        try:
            return CurlCffiClient(impersonate=anti.get("impersonate", "chrome"), **common)
        except Exception:
            if backend == "curl_cffi":
                log("curl_cffi 未安装，回退 requests", "WARN")
    if backend in ("auto", "curl_cffi", "requests"):
        try:
            import requests  # noqa: F401
            return RequestsClient(**common)
        except Exception:
            if backend == "requests":
                log("requests 未安装，回退 urllib", "WARN")
    return HttpClient(min_interval=common["min_interval"], max_retries=common["max_retries"],
                      timeout=common["timeout"], rotate_ua=common["rotate_ua"],
                      proxy=common["proxy"], cookies=common["cookies"] or {},
                      extra_headers=common["extra_headers"],
                      use_system_proxy=bool(anti.get("use_system_proxy", False)))
