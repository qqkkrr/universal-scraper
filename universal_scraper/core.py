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

from typing import NoReturn

import csv
import gzip
import base64
import hashlib
import threading
import zlib
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- 常量
CACHE_DEFAULT_TTL = 86400.0      # HTTP 响应缓存默认有效期（秒，24h）
CACHE_MAX_FILES = 2000           # 缓存文件上限（超限删最旧）
DEFAULT_MAX_BODY = 20 * 1024 * 1024  # 默认响应体上限（20MB，流式限读）


def _norm_cookies(val: Any) -> Dict[str, str]:
    """cookies 容忍 dict 或 "k=v; k2=v2" 串（cookies 命令导出的即串）。
    文档曾按串教用户填写、实现却按 dict 消费导致必崩——两侧在此统一。"""
    if not val:
        return {}
    if isinstance(val, dict):
        return {str(k): str(v) for k, v in val.items()}
    if isinstance(val, str):
        out: Dict[str, str] = {}
        for part in val.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, _, v = part.partition("=")
            if k.strip():
                out[k.strip()] = v.strip()
        return out
    return {}

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
    """智能解码（对标 charset_normalizer / trafilatura 编码探测）：
    按 BOM / Content-Type / <meta charset> 判断；声明编码解码质量差或为
    latin-1 等"万能可解码"编码时，自动在 utf-8/gb18030/gbk/big5 等候选里
    选"替换字符最少"的解码（修复 GBK 中文站乱码 / 错标 charset 的站）。"""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw  # 已解码的字符串直接返回（防调用方误传 str 崩溃）
    if raw.isascii():
        return raw.decode("ascii")  # 纯 ASCII 快路径：任何编码下结果相同（JSON/API 大头）
    enc = "utf-8"
    explicit = False
    ct = (headers or {}).get("content-type", "") or (headers or {}).get("Content-Type", "")
    m = re.search(r"charset=([\w-]+)", ct, re.I)
    if m:
        enc = m.group(1); explicit = True
    elif raw[:3] == b"\xef\xbb\xbf":
        enc = "utf-8-sig"
    else:
        head = raw[:2048].decode("utf-8", "ignore").lower()
        m2 = re.search(r"charset=[\"']?([\w-]+)", head)
        if m2:
            enc = m2.group(1); explicit = True

    def _score(e):
        try:
            s2 = raw.decode(e, "replace")
            return s2.count("\ufffd"), s2
        except LookupError:
            return 10 ** 9, None

    # 候选打分：替换字符最少者胜（顺序决定同分时优先：utf-8 → gb18030 → gbk …）
    best_err, best_s = 10 ** 9, None
    for cand in ("utf-8", "gb18030", "gbk", "big5", "shift_jis", "latin-1"):
        e, s2 = _score(cand)
        if s2 is not None and e < best_err:
            best_err, best_s = e, s2
    # charset_normalizer 仅作为"额外候选"参与打分（不作为权威，防误判）
    try:
        from charset_normalizer import from_bytes
        _c = from_bytes(raw[:65536]).best() if raw else None  # 只采样探测，控制 CPU 开销
        if _c is not None and _c.encoding:
            e, s2 = _score(_c.encoding)
            if s2 is not None and e < best_err:
                best_err, best_s = e, s2
    except Exception as _e:
        log(f"  charset_normalizer 探测失败: {_e}", "DEBUG")

    single_byte = (enc.lower() in ("latin-1", "latin1", "ascii", "iso-8859-1", "windows-1252", "cp1252")
                   or enc.lower().startswith("iso-8859") or enc.lower().startswith("windows-125")
                   or enc.lower().startswith("cp125"))
    if not explicit:
        return best_s if best_s is not None else raw.decode("utf-8", "replace")
    if single_byte:
        # latin-1 等"万能解码"：若候选能零错误解出（如实际是 UTF-8/GBK），优先用候选
        if best_err == 0 and best_s is not None:
            return best_s
        return raw.decode(enc, "replace")
    try:
        return raw.decode(enc, "strict")
    except (UnicodeDecodeError, LookupError):
        pass
    decl_err, _ = _score(enc)
    if best_s is not None and best_err < max(1, decl_err // 2):
        return best_s
    return raw.decode(enc, "replace")


def assert_http_url(url: str) -> str:
    """出站 URL scheme 校验：仅允许 http/https（防 file:/ftp: 等伪协议读取本地资源）。
    合法返回原 URL，否则抛 ValueError。"""
    scheme = (urllib.parse.urlsplit(url or "").scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 协议，已拒绝: {str(url)[:60]!r}")
    return url


def smart_decode(raw: bytes, headers: Optional[Dict[str, str]] = None) -> str:
    """公开别名：智能解码（BOM/声明/候选打分）。"""
    return _decode_body(raw, headers)


# ---------------------------------------------------------------- Cookie 工具
# 多后端（urllib/curl_cffi/requests）会把多个 Set-Cookie 合并成一个逗号串，
# Expires 日期里也带逗号——直接喂 http.cookiejar 会解析错，这里先按"新 cookie 名=值"
# 特征切分，再把每条单独喂给 CookieJar（RFC 6265 语义）。

def split_set_cookie(value: str) -> List[str]:
    """把可能逗号合并的 Set-Cookie 串切成单条。"""
    if not value:
        return []
    parts: List[str] = []
    cur = ""
    for seg in re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', value):
        seg = seg.strip()
        if not seg:
            continue
        # 新 cookie：以 "名字=值" 开头（名字不含 =;,\s）
        if cur and not re.match(r"^[^=;,\s]+=", seg):
            cur += ", " + seg          # Expires 日期延续
        else:
            if cur:
                parts.append(cur)
            cur = seg
    if cur:
        parts.append(cur)
    return parts


def set_cookie_strings(headers: Optional[Dict[str, Any]] = None,
                       raw_headers: Any = None) -> List[str]:
    """从响应里取所有 Set-Cookie 字符串（优先 raw 的多值接口）。"""
    out: List[str] = []
    if raw_headers is not None:
        try:
            if hasattr(raw_headers, "get_all"):
                out = list(raw_headers.get_all("Set-Cookie") or [])
            elif hasattr(raw_headers, "getlist"):
                out = list(raw_headers.getlist("Set-Cookie") or [])
        except Exception:
            out = []
    if not out:
        v = (headers or {}).get("set-cookie", "") or (headers or {}).get("Set-Cookie", "")
        out = split_set_cookie(str(v))
    # raw 多值里可能仍有个别被合并，逐条再切一次（幂等）
    final: List[str] = []
    for o in out:
        final.extend(split_set_cookie(o))
    return final


def update_cookie_jar(jar, url: str, headers: Optional[Dict[str, Any]] = None,
                      raw_headers: Any = None) -> None:
    """把响应的 Set-Cookie 写入 CookieJar。"""
    if jar is None:
        return
    try:
        from http.cookiejar import CookieJar
        from http.client import HTTPMessage
        import urllib.request as _ur
        if not isinstance(jar, CookieJar):
            return
        req = _ur.Request(url or "http://localhost/")
        for sc in set_cookie_strings(headers, raw_headers):
            if not sc:
                continue
            msg = HTTPMessage()
            msg.add_header("Set-Cookie", sc)
            class _Resp:
                def __init__(self, m):
                    self._m = m
                def info(self):
                    return self._m
                def geturl(self):
                    return url or "http://localhost/"
            try:
                jar.extract_cookies(_Resp(msg), req)
            except Exception:
                continue
    except Exception:
        pass


def jar_cookie_header(jar, url: str) -> str:
    """从 CookieJar 生成 Cookie 请求头（无 cookie 返回 ""）。"""
    if jar is None:
        return ""
    try:
        import urllib.request as _ur
        req = _ur.Request(url or "http://localhost/")
        jar.add_cookie_header(req)
        return req.get_header("Cookie") or ""
    except Exception:
        return ""


def fetch_bytes(url: str, headers: Optional[Dict[str, str]] = None, proxy: Optional[str] = None,
               timeout: int = 60) -> Optional[bytes]:
    """下载原始字节（curl_cffi TLS 伪装优先，回退 urllib+gzip）。失败返回 None，
    失败原因记录在 fetch_bytes.last_error（供调用方诊断，不再双重静默吞错）。"""
    # 与 HttpClient 同一 SSRF 面：urllib 回退 opener 含 FileHandler，file:// 会读本地文件
    if urllib.parse.urlsplit(url or "").scheme not in ("http", "https"):
        fetch_bytes.last_error = f"拒绝非 http/https 协议: {url!r}"
        return None
    fetch_bytes.last_error = ""
    hdrs = {"User-Agent": random.choice(UA_POOL), "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        hdrs.update(headers)
    try:
        import curl_cffi.requests as cffi
        kw = {"headers": hdrs, "timeout": timeout, "impersonate": "chrome"}
        if proxy:
            kw["proxies"] = {"http": proxy, "https": proxy}
        r = cffi.get(url, **kw)
        if r.status_code < 400:
            return r.content or None
        fetch_bytes.last_error = f"curl_cffi: HTTP {r.status_code}"
        return None
    except Exception as e:
        fetch_bytes.last_error = f"curl_cffi: {type(e).__name__}: {e}"
    try:
        req = urllib.request.Request(url, headers=hdrs)
        opener = urllib.request.build_opener()
        if proxy:
            opener.add_handler(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            enc = resp.headers.get("Content-Encoding", "").lower()
            if enc == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            return raw or None
    except Exception as e:
        fetch_bytes.last_error += f" | urllib: {type(e).__name__}: {e}"
        return None


fetch_bytes.last_error = ""  # 每次调用重置；失败时记录最后一次错误（诊断用）


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
        self.cookies = _norm_cookies(self.cookies)
        self._last_ts = 0.0
        self._throttle_lock = threading.Lock()
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- 限速 --
    def _throttle(self) -> None:
        # 多 worker 并发调用：必须加锁，否则限速形同虚设（可 2 倍突发）
        with self._throttle_lock:
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
        return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest() + ".json"

    def _cache_encode(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """缓存编码：body bytes → base64（json 无法直接存 bytes）；raw_headers 不落盘。"""
        out = dict(result)
        out.pop("raw_headers", None)
        b = out.get("body")
        if isinstance(b, bytes):
            out["body"] = "b64:" + base64.b64encode(b).decode("ascii")
        return out

    def _cache_decode(self, cached: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(cached)
        b = out.get("body")
        if isinstance(b, str) and b.startswith("b64:"):
            out["body"] = base64.b64decode(b[4:])
        return out

    def _cache_valid(self, path: Path, ttl: float = CACHE_DEFAULT_TTL) -> bool:
        try:
            return time.time() - path.stat().st_mtime <= ttl
        except Exception:
            return False

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
        max_size: Optional[int] = None,
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
            if cf.exists() and self._cache_valid(cf):
                try:
                    return self._cache_decode(json.loads(cf.read_text(encoding="utf-8")))
                except Exception:
                    pass

        result: Optional[Dict[str, Any]] = None
        # 注意：不要用 socket.setdefaulttimeout 改进程级全局超时——多线程 worker
        # 会互相覆盖（竞态）。urllib opener.open(timeout=...) 已覆盖连接/TLS/读取。
        try:
            result = self._request_once(url, body_bytes, method, headers, use_cache,
                                        allow_html_404=allow_html_404, proxy=proxy,
                                        max_size=max_size)
        finally:
            pass
        return result

    def _request_once(self, url, body_bytes, method, headers, use_cache,
                      allow_html_404=False, proxy=None, max_size=None):
        # SSRF 面：urllib 默认 opener 含 FileHandler，file:// 会直接读本地文件。
        # HttpClient 只做 HTTP 抓取，非 http/https 协议一律拒绝（不加私网 IP 过滤：
        # 爬虫需要访问任意公网站点，且本地回环测试依赖 127.0.0.1）。
        if urllib.parse.urlsplit(url or "").scheme not in ("http", "https"):
            raise ValueError(f"HttpClient 仅支持 http/https 协议，已拒绝: {url!r}")
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
                    enc = resp.headers.get("Content-Encoding", "").lower()
                    limit = (max_size + 1) if max_size else None
                    if enc == "gzip":
                        # 流式解压限读：gzip 炸弹也能被 max_size 截住
                        try:
                            gz = gzip.GzipFile(fileobj=resp)
                            raw = gz.read(limit) if limit else resp.read()
                        except Exception:
                            raw = resp.read(limit) if limit else resp.read()
                    elif enc == "deflate":
                        raw = resp.read()
                        try:
                            raw = zlib.decompress(raw)
                        except Exception:
                            pass
                        if limit:
                            raw = raw[:limit]
                    else:
                        raw = resp.read(limit) if limit else resp.read()
                    if max_size and len(raw) > max_size:
                        raw = raw[:max_size]
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
                        "raw_headers": resp.headers,
                    }
                    break
            except urllib.error.HTTPError as e:
                raw = e.read((max_size + 1) if max_size else None) if max_size else e.read()
                if max_size and len(raw) > max_size:
                    raw = raw[:max_size]
                # urllib 不自动解压 gzip：HTTPError 路径的 raw 仍是压缩字节，不解码全乱码
                if raw[:2] == b"\x1f\x8b":
                    import gzip as _gz
                    try:
                        raw = _gz.decompress(raw)[:max_size] if max_size else _gz.decompress(raw)
                    except Exception:
                        pass
                status = e.code
                # WAF/反爬常返回 404 的"假页面"，allow_html_404 表示接受这种 HTML 响应
                if allow_html_404 and status == 404:
                    result = {
                        "ok": True, "status": status, "body": raw,
                        "text": _decode_body(raw, {k.lower(): v for k, v in e.headers.items()} if e.headers else {}),
                        "json": None, "url": url,
                        "headers": {k.lower(): v for k, v in e.headers.items()} if e.headers else {},
                        "raw_headers": e.headers if e.headers else None,
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
            _cf = self.cache_dir / self._cache_key(url, b"", method)
            _cf.write_text(json.dumps(self._cache_encode(result), ensure_ascii=False), encoding="utf-8")
            # 简单淘汰：超过上限时删最旧的（防无限增长）
            try:
                _files = sorted(self.cache_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
                if len(_files) > CACHE_MAX_FILES:
                    for _old in _files[: len(_files) - CACHE_MAX_FILES]:
                        _old.unlink(missing_ok=True)
            except Exception:
                pass
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

class RequestsClient:
    """基于 requests 的高性能客户端：连接池复用、gzip、会话 Cookie、限速、重试。"""

    def __init__(self, min_interval: float = 1.0, max_retries: int = 3,
                 timeout: float = 20, rotate_ua: bool = True,
                 proxy: Optional[str] = None, cookies: Optional[Dict[str, str]] = None,
                 extra_headers: Optional[Dict[str, str]] = None,
                 backoff_base: float = 2.0, verify: bool = False):
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
        self.session.verify = verify
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        if cookies:
            self.session.cookies.update(_norm_cookies(cookies))
        self._throttle_lock = threading.Lock()
        # 连接池
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _throttle(self) -> None:
        # 多 worker 并发调用：必须加锁，否则限速形同虚设（可 2 倍突发）
        with self._throttle_lock:
            elapsed = time.time() - self._last_ts
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_ts = time.time()

    def request(self, url: str, method: str = "GET", params=None, data=None,
                json_data=None, headers=None, use_cache: bool = False,
                allow_html_404: bool = False, proxy: Optional[str] = None,
                max_size: Optional[int] = None) -> Dict[str, Any]:
        h = {"Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
             "Accept-Encoding": "gzip, deflate"}
        h.update(self.extra_headers)
        if headers:
            h.update(headers)
        # 调用方/会话已给 UA 就不随机（三后端一致；登录会话不能被随机 UA 踢掉）
        if "User-Agent" not in h and self.rotate_ua:
            h["User-Agent"] = random.choice(UA_POOL)
        elif "User-Agent" not in h:
            h["User-Agent"] = DEFAULT_UA

        last_err = "requests 请求失败"
        last_status = 0
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                kw = {"params": params, "data": data, "json": json_data,
                      "headers": h, "timeout": self.timeout, "allow_redirects": True}
                if proxy:
                    kw["proxies"] = {"http": proxy, "https": proxy}
                resp = self.session.request(method.upper(), url, **kw)
                if resp.status_code == 429:
                    _t = smart_decode(resp.content, {k.lower(): v for k, v in resp.headers.items()})
                    return {"ok": False, "status": 429, "body": resp.content, "text": _t,
                            "json": None, "url": resp.url,
                            "headers": {k.lower(): v for k, v in resp.headers.items()},
                            "raw_headers": getattr(resp, "raw", None) and getattr(resp.raw, "headers", None)}
                if resp.status_code in (403, 500, 502, 503, 504):
                    last_status = resp.status_code
                    last_err = f"HTTP {resp.status_code}"
                    wait = self.backoff_base ** attempt + random.uniform(0, 1)
                    log(f"  请求失败 {resp.status_code}，{wait:.1f}s 后重试（{attempt}/{self.max_retries}）", "WARN")
                    time.sleep(wait)
                    continue
                if max_size:
                    chunks = []
                    total = 0
                    for chunk in resp.iter_content(chunk_size=65536):
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > max_size:
                            break
                    raw = b"".join(chunks)[:max_size]
                else:
                    raw = resp.content
                if resp.status_code >= 400 and not (allow_html_404 and resp.status_code == 404):
                    _t = smart_decode(raw, {k.lower(): v for k, v in resp.headers.items()})
                    return {"ok": False, "status": resp.status_code, "body": raw, "text": _t,
                            "json": None, "url": resp.url,
                            "headers": {k.lower(): v for k, v in resp.headers.items()},
                            "raw_headers": getattr(resp, "raw", None) and getattr(resp.raw, "headers", None)}
                parsed = None
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype or raw[:1] in (b"{", b"["):
                    try:
                        parsed = json.loads(raw.decode("utf-8", "ignore"))
                    except Exception:
                        parsed = None
                # 无 charset 头时 resp.text 按 ISO-8859-1 解码 → 中文全乱码
                # （"百度安全验证"曾被误判为页面结构变化）。一律走 smart_decode 从 raw 解。
                _text = (smart_decode(raw, {k.lower(): v for k, v in resp.headers.items()})
                         if raw else (resp.text or ""))
                return {"ok": True, "status": resp.status_code, "body": raw,
                        "text": _text, "json": parsed, "url": resp.url,
                        "headers": {k.lower(): v for k, v in resp.headers.items()},
                        "raw_headers": getattr(resp, "raw", None) and getattr(resp.raw, "headers", None)}
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                wait = self.backoff_base ** attempt
                log(f"  网络异常: {e}，{wait:.1f}s 后重试（{attempt}/{self.max_retries}）", "WARN")
                time.sleep(wait)
        return {"ok": False, "status": last_status, "body": b"", "text": last_err, "json": None, "url": url,
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

def _sanitize_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """🛡️ curl_cffi 对 None 值 header 会抛 TypeError（'in <string>' requires string）。
    统一过滤 None 值并字符串化，避免配置里出现 None header 导致整请求崩溃。"""
    if not headers:
        return {}
    return {k: str(v) for k, v in headers.items() if v is not None}


class CurlCffiClient:
    """curl_cffi 后端：伪装浏览器 TLS/JA3/HTTP2 指纹（curl-impersonate）。
    未安装 curl_cffi 时导入即抛 ImportError，调用方回退 requests/urllib。
    接口与 HttpClient 对齐：request/get/post。
    """

    def __init__(self, min_interval: float = 1.0, max_retries: int = 3,
                 timeout: float = 20, rotate_ua: bool = True,
                 proxy: Optional[str] = None, cookies: Optional[Dict[str, str]] = None,
                 extra_headers: Optional[Dict[str, str]] = None,
                 impersonate: str = "chrome", verify: bool = False):
        __import__("curl_cffi.requests")  # 可用性探测：未安装则抛 ImportError
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.rotate_ua = rotate_ua
        self.proxy = proxy
        self.impersonate = impersonate
        self.verify = verify
        self.impersonate_pool = ["chrome", "safari17_0", "firefox133", "edge101"]  # impersonate="auto" 时轮换
        self._throttle_lock = threading.Lock()
        self.extra_headers = dict(extra_headers or {})
        self.cookies = _norm_cookies(cookies)
        self._last_ts = 0.0

    def _throttle(self) -> None:
        # 多 worker 并发调用：必须加锁，否则限速形同虚设（可 2 倍突发）
        with self._throttle_lock:
            elapsed = time.time() - self._last_ts
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_ts = time.time()

    def request(self, url: str, method: str = "GET", params=None, data=None,
                json_data=None, headers=None, use_cache: bool = False,
                allow_html_404: bool = False, proxy: Optional[str] = None,
                max_size: Optional[int] = None) -> Dict[str, Any]:
        import curl_cffi.requests as cffi
        h = {"Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        h.update(self.extra_headers)
        if headers:
            h.update(headers)
        h = _sanitize_headers(h)
        if not self.rotate_ua and "User-Agent" not in h:
            h["User-Agent"] = DEFAULT_UA
        proxy_use = proxy if proxy is not None else self.proxy
        imp = self.impersonate
        if imp == "auto":
            # 调用方给了 UA（会话 UA）→ 指纹跟随 UA，避免 "Safari UA + Chrome TLS" 错配
            _ua = (h.get("User-Agent") or "").lower()
            if "firefox" in _ua:
                imp = "firefox133"
            elif "edg/" in _ua:
                imp = "edge101"
            elif "safari" in _ua and "chrome" not in _ua:
                imp = "safari17_0"
            else:
                imp = random.choice(self.impersonate_pool)
        kw = dict(impersonate=imp, timeout=self.timeout, headers=h,
                  allow_redirects=True, verify=self.verify)
        if max_size:
            kw["stream"] = True  # iter_content 需要流式模式
        if proxy_use:
            kw["proxies"] = {"http": proxy_use, "https": proxy_use}
        if self.cookies:
            kw["cookies"] = self.cookies
        last_err = ""
        last_status = 0
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                resp = cffi.request(method.upper(), url, params=params,
                                    data=data, json=json_data, **kw)
                if max_size:
                    # 流式限读：读满 max_size+1 即停，避免超大响应占满内存
                    chunks = []
                    total = 0
                    for chunk in resp.iter_content(chunk_size=65536):
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > max_size:
                            break
                    raw = b"".join(chunks)[:max_size]
                else:
                    raw = resp.content
                if resp.status_code == 429:
                    # 429 不内部重试：立即交回，让引擎按 Retry-After 调度（保留状态码）
                    _t = smart_decode(raw, {k.lower(): v for k, v in resp.headers.items()})
                    return {"ok": False, "status": 429, "body": raw, "text": _t,
                            "json": None, "url": str(resp.url),
                            "headers": {k.lower(): v for k, v in resp.headers.items()},
                            "raw_headers": resp.headers}
                if resp.status_code in (403, 500, 502, 503, 504):
                    last_err = f"HTTP {resp.status_code}"
                    last_status = resp.status_code
                    wait = self.backoff_base ** attempt + random.uniform(0, 1)
                    log(f"  curl_cffi 请求失败 {resp.status_code}，{wait:.1f}s 后重试", "WARN")
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400 and not (allow_html_404 and resp.status_code == 404):
                    # 非 2xx/3xx 视为失败（404 仅在 allow_html_404 时接受）——与 urllib 后端一致
                    _t = smart_decode(raw, {k.lower(): v for k, v in resp.headers.items()})
                    return {"ok": False, "status": resp.status_code, "body": raw, "text": _t,
                            "json": None, "url": str(resp.url),
                            "headers": {k.lower(): v for k, v in resp.headers.items()},
                            "raw_headers": resp.headers}
                parsed = None
                # 一律从 raw 解析 JSON：stream 模式下 resp.json() 读不到已消费的流
                if "json" in resp.headers.get("Content-Type", "") or raw[:1] in (b"{", b"["):
                    try:
                        parsed = json.loads(raw.decode("utf-8", "ignore"))
                    except Exception:
                        parsed = None
                # 无 charset 头时 resp.text 按 ISO-8859-1 解码 → 中文全乱码
                # （"百度安全验证"曾被误判为页面结构变化）。一律走 smart_decode 从 raw 解。
                _text = (smart_decode(raw, {k.lower(): v for k, v in resp.headers.items()})
                         if raw else (resp.text or ""))
                return {"ok": True, "status": resp.status_code, "body": raw,
                        "text": _text,
                        "json": parsed,
                        "url": str(resp.url), "headers": {k.lower(): v for k, v in resp.headers.items()},
                        "raw_headers": resp.headers}
            except Exception as e:
                last_err = str(e)
                wait = self.backoff_base ** attempt
                log(f"  curl_cffi 网络异常: {e}，{wait:.1f}s 后重试", "WARN")
                time.sleep(wait)
        return {"ok": False, "status": last_status, "body": b"", "text": last_err,
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
        verify=bool(anti.get("verify", False)),
    )
    common.update(kw)
    if backend in ("auto", "curl_cffi"):
        try:
            return CurlCffiClient(impersonate=anti.get("impersonate", "chrome"), **common)
        except Exception as _e:
            # 降级必须可诊断（TLS 指纹伪装静默失效曾导致被反爬拦截却查不到原因）
            log(f"⚠️ HTTP 后端降级: curl_cffi 不可用({_e})，改用 requests", "WARN")
    if backend in ("auto", "curl_cffi", "requests"):
        try:
            __import__("requests")  # 可用性探测：未安装则抛 ImportError
            return RequestsClient(**common)
        except Exception as _e:
            log(f"⚠️ HTTP 后端降级: requests 不可用({_e})，改用 urllib", "WARN")
    return HttpClient(min_interval=common["min_interval"], max_retries=common["max_retries"],
                      timeout=common["timeout"], rotate_ua=common["rotate_ua"],
                      proxy=common["proxy"], cookies=common["cookies"] or {},
                      extra_headers=common["extra_headers"],
                      use_system_proxy=bool(anti.get("use_system_proxy", False)))
