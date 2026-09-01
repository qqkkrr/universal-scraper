#!/usr/bin/env python3
"""🔄 免费代理池自动构建（对标 DsTansice/aggregator、proxyscrape 思路）：
从多个公开源抓免费代理 → 并发验证（HTTP 连通 + 出口可用）→ 写入 proxies.txt。
注意：免费代理仅供低风险站点/应急轮换，强风控站请用付费住宅代理。
用法:
  python3 -m universal_scraper.proxy_fetch --refresh --out outputs/proxies.txt
  python3 -m universal_scraper.cli proxy --refresh
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import List, Tuple

SOURCES = [
    # 仅保留实测可用源（proxyscrape/openproxy 已失效，HTTP 000）
    ("geonode", "https://proxylist.geonode.com/api/proxy-list?limit=150&page=1&sort_by=lastChecked&sort_type=desc"),
]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
TEST_URL = "https://example.com"          # 轻量连通测试
TEST_URL_CN = "http://www.baidu.com"      # 国内可达性测试（可选）


def _get(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    # 显式直连（绕过 Clash 等系统代理，避免代理干扰导致 000）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return r.read(300000).decode("utf-8", "ignore")


def _parse(text: str) -> List[str]:
    """从文本/JSON 里提取 ip:port。"""
    out = []
    # JSON（geonode 等）
    try:
        data = json.loads(text)
        items = data if isinstance(data, list) else (data.get("data") or [])
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                ip = it.get("ip") or it.get("address") or ""
                port = str(it.get("port") or it.get("port") or "")
                if ip and port:
                    protos = it.get("protocols") or ["http"]
                    scheme = "http://" if any("http" in str(x).lower() for x in protos) else "socks5://"
                    out.append(f"{scheme}{ip}:{port}")
            if out:
                return out
    except Exception:
        pass
    # 纯文本行 ip:port → 默认 http 代理
    for m in re.finditer(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})", text):
        out.append(f"http://{m.group(1)}:{m.group(2)}")
    return out


def fetch_all(timeout: int = 12, log=print) -> List[str]:
    proxies: List[str] = []
    for name, url in SOURCES:
        try:
            t = _get(url, timeout=timeout)
            ps = _parse(t)
            log(f"  {name}: +{len(ps)}")
            proxies.extend(ps)
        except Exception as e:
            log(f"  {name}: 失败 {type(e).__name__}")
    # 去重
    return list(dict.fromkeys(proxies))


def _test_proxy(proxy: str) -> Tuple[str, bool]:
    p = {"http": proxy, "https": proxy}
    if not proxy.startswith(("http://", "socks5://", "socks4://")):
        proxy = "http://" + proxy
        p = {"http": proxy, "https": proxy}
    try:
        import curl_cffi.requests as cffi
        r = cffi.get(TEST_URL, timeout=6, proxies=p, impersonate="chrome")
        return proxy, r.status_code < 400
    except Exception:
        pass
    try:
        import requests  # type: ignore
        r = requests.get(TEST_URL, timeout=6, proxies=p)
        return proxy, r.status_code < 400
    except Exception:
        pass
    return proxy, False


def validate(proxies: List[str], workers: int = 30, log=print) -> List[str]:
    ok: List[str] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_test_proxy, p): p for p in proxies}
        done = 0
        for f in cf.as_completed(futs):
            done += 1
            p, good = f.result()
            if good:
                ok.append(p)
            if done % 50 == 0:
                log(f"  验证 {done}/{len(proxies)}，可用 {len(ok)}")
    return ok


def refresh(out: str = "outputs/proxies.txt", workers: int = 30,
            min_ok: int = 5, log=print) -> dict:
    log("🔄 抓取免费代理（geonode，其余公开源已失效移除）...")
    all_p = fetch_all(log=log)
    log(f"共抓取 {len(all_p)} 个（去重后），开始验证连通性...")
    ok = validate(all_p, workers=workers, log=log)
    if len(ok) < min_ok:
        log(f"⚠️ 可用代理仅 {len(ok)} 个（阈值 {min_ok}），仍会写入供应急使用")
    fp = Path(out).expanduser()
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("\n".join(ok) + ("\n" if ok else ""), encoding="utf-8")
    log(f"✅ 可用代理 {len(ok)} 个 → {fp}")
    return {"total": len(all_p), "ok": len(ok), "file": str(fp)}


def main() -> int:
    ap = argparse.ArgumentParser(description="🔄 免费代理池自动构建")
    ap.add_argument("--refresh", action="store_true", help="抓取+验证+写入")
    ap.add_argument("--out", default="outputs/proxies.txt")
    ap.add_argument("--workers", type=int, default=30)
    args = ap.parse_args()
    r = refresh(out=args.out, workers=args.workers)
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
