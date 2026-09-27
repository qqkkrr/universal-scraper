#!/usr/bin/env python3
"""住宅/商业代理 API adapter（R116 新能力，对标 Crawlee 代理集成）：

把商业代理厂商的「提取 API」响应统一解析成代理池可用的 URL 列表。
支持三种常见响应形态（format=auto 时自动识别）：
  - "text"      : 纯文本，每行一条 `host:port` 或 `protocol://host:port`
  - "json_list" : JSON 数组，元素为字符串或 {"ip":..,"port":..[, "protocol"..]} 对象
  - "json_data" : {"data": {"proxy_list": [...]}} 等带包裹层的 JSON（自动探测列表键）

出站安全口径：默认拒绝 localhost/私网/保留地址的 API（防配置注入打内网）；
商业代理提取 API 都是公网 https，正常使用不受影响。代理条目本身不做
ip 段过滤（代理出口可以是任何地址）。

用法（engine 配置）：
    "anti_bot": {"proxy_api": {"url": "https://vendor/api?num=20&format=json",
                               "format": "auto", "protocol": "http"}}
engine 启动时拉取一次并并入代理池；拉取失败 WARN 后继续（池可为空走直连）。
"""
from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit


def _is_public_http_url(url: str) -> bool:
    """API 地址必须是公网 http/https（拒绝 localhost/私网/保留段/非 http 协议）。
    R116 修复（P2）：整型/十六进制/八进制 IPv4 字面量（2130706433、0x7f000001、
    0177.0.0.1）与 localhost 曾绕过判定——getaddrinfo 会把它们解析成 127.0.0.1。"""
    try:
        sp = urlsplit(url or "")
    except Exception:
        return False
    if (sp.scheme or "").lower() not in ("http", "https") or not sp.hostname:
        return False
    host = sp.hostname.lower()
    # R117 修复（P2）：尾点域名（"127.0.0.1."/"2130706433."）getaddrinfo 照样
    # 解析成回环/内网——剥掉尾点后走同一套检查
    while host.endswith("."):
        host = host[:-1]
    if host == "localhost" or host.endswith(".localhost"):
        return False
    # R116 修复（P2）：数字/缩写形态全部按回环风险拒绝——
    # 整型任意长度（2130706433）、0x 十六进制、点分但段数≠4（127.1 → 127.0.0.1）、
    # 前导零八进制（0177.0.0.1）、纯 "0"
    if re.fullmatch(r"\d+", host):
        return False
    if re.search(r"0x[0-9a-f]{2,}", host):
        return False
    if "." in host:
        parts = host.split(".")
        if all(p.isdigit() for p in parts) and len(parts) != 4:
            return False
        if any(len(p) > 1 and p[0] == "0" for p in parts if p.isdigit()):
            return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 普通域名：交由 DNS/请求层，不做预判
    return not (ip.is_private or ip.is_loopback or ip.is_reserved
                or ip.is_link_local or ip.is_multicast)


_ENTRY_RE = None  # 懒编译

def _normalize(entry: str, protocol: str) -> Optional[str]:
    """host:port / scheme://host:port → protocol://host:port；明显非代理行（垃圾
    文本、HTML 残片）返回 None。R116：text 形态曾不加校验把任意行当代理。"""
    global _ENTRY_RE
    entry = (entry or "").strip()
    if not entry:
        return None
    if "://" in entry:
        sp = entry.split("://", 1)
        if len(sp) == 2 and sp[0] in ("http", "https", "socks5"):
            host_port = sp[1].rsplit("/", 1)[0]
            # OCR R131（M）：端口 >65535 曾照单全收——非法地址进轮换池白烧尝试
            if ":" in host_port:
                try:
                    _p = int(host_port.rsplit(":", 1)[1])
                    if not (1 <= _p <= 65535):
                        return None
                except ValueError:
                    return None
            return entry
        return None
    import re as _re
    if _ENTRY_RE is None:
        _ENTRY_RE = _re.compile(r"^[A-Za-z0-9._-]+:\d{2,5}$")
    if not _ENTRY_RE.match(entry):
        return None
    # OCR R131（M）：同 scheme 形态——裸 host:port 的端口也要过范围闸
    try:
        if not (1 <= int(entry.rsplit(":", 1)[1]) <= 65535):
            return None
    except ValueError:
        return None
    return f"{protocol}://{entry}"


def _extract_from_json(obj: Any, protocol: str) -> List[str]:
    """从任意 JSON 结构递归收集 host:port / 代理 URL 字符串。"""
    out: List[str] = []
    if isinstance(obj, str):
        s = obj.strip()
        if s and (":" in s) and (" " not in s):
            n = _normalize(s, protocol)
            if n:
                out.append(n)
    elif isinstance(obj, dict):
        ip = obj.get("ip") or obj.get("host") or obj.get("server")
        port = obj.get("port")
        proto = (obj.get("protocol") or protocol).lower()
        if ip and port:
            n = _normalize(f"{proto}://{ip}:{port}", proto)
            if n:
                out.append(n)
        for v in obj.values():
            if isinstance(v, (list, dict)):
                out.extend(_extract_from_json(v, protocol))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_extract_from_json(v, protocol))
    return out


def fetch_proxies(api_url: str, format: str = "auto", protocol: str = "http",
                  timeout: int = 20, allow_private_api: bool = False) -> List[str]:
    """调提取 API → 去重 → 返回 ["http://h:p", ...] 形式的代理列表。

    format: "auto" | "text" | "json_list" | "json_data"
    allow_private_api: API 部署在本机/内网网关时显式开启（默认拒绝私网）。"""
    if not _is_public_http_url(api_url):
        if allow_private_api and (api_url or "").lower().startswith(("http://", "https://")):
            pass  # 显式豁免：本机网关场景
        else:
            raise ValueError(f"代理 API 地址非法或指向私网（如需内网 API 加 "
                             f"allow_private_api=True）: {api_url!r}")
    from .core import make_http_client
    client = make_http_client({"min_interval": 0, "timeout": timeout,
                               "max_retries": 1, "rotate_ua": False})
    res = client.get(api_url)
    if not res.get("ok"):
        raise RuntimeError(f"代理 API 请求失败: HTTP {res.get('status', 0)} "
                           f"{(res.get('text') or '')[:120]}")
    body = res.get("text") or ""

    fmt = (format or "auto").lower()
    proxies: List[str] = []
    if fmt in ("auto", "json_list", "json_data"):
        try:
            obj = json.loads(body)
        except Exception:
            obj = None
        if obj is not None:
            proxies = _extract_from_json(obj, protocol)
            if fmt == "json_data" and not proxies:
                # json_data 形态：递归找不到就把叶子字符串再试一遍
                proxies = [p for p in (_normalize(x, protocol) for x in
                                       [l.strip() for l in body.splitlines()]) if p]
    if not proxies and fmt in ("auto", "text"):
        for line in body.splitlines():
            n = _normalize(line, protocol)
            if n and "://" in n:
                proxies.append(n)

    # 去重保序 + 清洗明显非代理行（HTML 残片等）
    seen: set = set()
    out: List[str] = []
    for px in proxies:
        if px and "://" in px and px not in seen:
            seen.add(px)
            out.append(px)
    return out


_API_CACHE: Dict[str, Any] = {}   # url -> (ts, proxies)；TTL 300s 防每次实例化都打计费 API
_API_CACHE_TTL = 300.0
# OCR R131（H）：缓存 check-then-act 加锁——多线程并发 miss 曾同打计费 API
import threading as _th
_API_CACHE_LOCK = _th.Lock()


def merge_api_proxies(anti: Dict[str, Any], log=None) -> None:
    """engine/fetcher 装配口：anti.proxy_api = {"url", "format", "protocol",
    "allow_private_api"} → 拉取并并入 anti.proxies（静态代理保留在前）。
    拉取失败 WARN 不中断（池可空走直连）。带 300s 进程内缓存。"""
    import time
    api_cfg = anti.get("proxy_api")
    if not isinstance(api_cfg, dict) or not api_cfg.get("url"):
        return
    url = api_cfg.get("url", "")
    try:
        cache_key = (f"{url}|{api_cfg.get('format', 'auto')}"
                     f"|{api_cfg.get('protocol', 'http')}"
                     f"|{bool(api_cfg.get('allow_private_api'))}")
        with _API_CACHE_LOCK:
            cached = _API_CACHE.get(cache_key)
            fetched = None
            if cached and time.time() - cached[0] < _API_CACHE_TTL:
                fetched = cached[1]
            else:
                fetched = fetch_proxies(url, format=api_cfg.get("format", "auto"),
                                        protocol=api_cfg.get("protocol", "http"),
                                        allow_private_api=bool(api_cfg.get("allow_private_api")))
                _API_CACHE[cache_key] = (time.time(), fetched)
        static = anti.get("proxies") or []
        anti["proxies"] = list(dict.fromkeys(list(static) + fetched))
        if log:
            log(f"🌐 代理 API 注入 {len(fetched)} 条（合计 {len(anti['proxies'])}）")
    except Exception as e:
        if log:
            try:
                log(f"⚠️ 代理 API 拉取失败（继续用已有代理/直连）: "
                    f"{type(e).__name__}: {str(e)[:140]}", "WARN")
            except Exception:
                pass
