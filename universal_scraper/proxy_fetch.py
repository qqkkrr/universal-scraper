#!/usr/bin/env python3
"""🔄 免费代理池构建 v2（《科研管理》1369 篇战役 2026-09 实测重构）。

战训沉淀（详见 references/case-kygl.md 与 R21 配方）：
1. 校验必须打目标站：通用靶（example.com/baidu）可用率与目标站可用率无关
   （实测 37.5% 过百度 / 0% 过目标站）。默认校验 = 经代理 GET 目标站真实重页面
   + 内容标记断言。
2. 校验分两层：连通层（页面 GET）≠ 能力层（受门禁的接口）；
   能力层只用"处女资源"一次性定性，绝不拿任务本身的资源试错。
3. 代理三态：fresh（未试）/ dead（连不上，可复活）/ burned（当日配额烧尽，
   拉黑到次日）；状态原子持久化，重启不丢。
4. 代理只换网络身份；客户端指纹（curl_cffi impersonate=chrome）仍然必须，
   两者是乘法关系。
5. 免费代理半衰期约 40 分钟，池子是"边用边补"的流水，不是一次性资产。

用法:
  python3 -m universal_scraper.cli proxy --refresh                       # 通用连通校验
  python3 -m universal_scraper.cli proxy --refresh \\
      --target-url https://example.com/heavy-page --marker 某站标记       # 目标站校验
  python3 -m universal_scraper.cli proxy --status                        # 三态统计
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import ipaddress
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

# ---------------------------------------------------------------- 安全边界
# 仅 https 且主机在白名单内（防 SSRF）；代理地址拒绝私网/环回/保留段。
SOURCE_HOSTS = {
    "raw.githubusercontent.com",
    "api.proxyscrape.com",
    "proxylist.geonode.com",
}

SOURCES = [
    # 2026-09 实测可用（geonode 常空但保留；单一来源不可依赖）
    ("monosans-http", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"),
    ("monosans-https", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/https.txt"),
    ("proxifly", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"),
    ("mmpx12", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt"),
    ("roosterkid", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt"),
    ("proxyscrape", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=8000&country=all&ssl=all&anonymity=all"),
    ("geonode", "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc"),
]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
TEST_URL = "https://example.com"          # 轻量连通测试（仅通用模式）
# 目标站校验默认超时：免费代理慢，12s 实测过滤率与体感一致
TARGET_TIMEOUT = 12


def _guard_url(url: str) -> str:
    """源抓取 URL 白名单校验：仅 https + 固定主机。"""
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in SOURCE_HOSTS:
        raise ValueError(f"代理源不在白名单: {url}")
    return url


def proxy_addr_safe(px: str) -> bool:
    """代理地址安全校验：拒绝 私网/环回/链路本地/保留段。"""
    host = px
    for prefix in ("http://", "https://", "socks5://", "socks4://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    host = host.rsplit("/", 1)[0].split("@")[-1].rsplit(":", 1)[0]
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
    except ValueError:
        return False


# ---------------------------------------------------------------- 抓取
def _get(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(_guard_url(url), headers={"User-Agent": UA, "Accept": "*/*"})
    # 显式直连（绕过 Clash 等系统代理，避免代理干扰导致 000）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return r.read(300000).decode("utf-8", "ignore")


def _parse(text: str) -> List[str]:
    """从文本/JSON 里提取 http://ip:port（保持 v1 兼容）。"""
    out: List[str] = []
    try:
        data = json.loads(text)
        items = data if isinstance(data, list) else (data.get("data") or [])
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                ip = it.get("ip") or it.get("address") or ""
                port = str(it.get("port") or "")
                if ip and port:
                    out.append(f"http://{ip}:{port}")
            if out:
                return out
    except Exception:
        pass
    for m in re.finditer(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})", text):
        out.append(f"http://{m.group(1)}:{m.group(2)}")
    return list(dict.fromkeys(out))


def fetch_all(timeout: int = 12, log=print) -> List[str]:
    proxies: List[str] = []
    for name, url in SOURCES:
        try:
            t = _get(url, timeout=timeout)
            ps = [p for p in _parse(t) if proxy_addr_safe(p)]
            log(f"  {name}: +{len(ps)}")
            proxies.extend(ps)
        except Exception as e:
            log(f"  {name}: 失败 {type(e).__name__}")
    return list(dict.fromkeys(proxies))


# ---------------------------------------------------------------- 校验
def _fetch_via(proxy: str, url: str, timeout: int, marker: Optional[str] = None) -> Tuple[bool, int]:
    """经代理 GET url；marker 给定时断言响应含标记内容。返回 (ok, latency_ms)。
    审查修复：curl_cffi 缺失不再逐代理吞 ImportError（表现为沉默的"0 可用"）——
    先探测一次，缺失则降级 requests 并大声提示。"""
    import curl_cffi.requests as cffi
    p = {"http": proxy, "https": proxy}
    t0 = time.time()
    r = cffi.get(url, timeout=timeout, proxies=p, impersonate="chrome",
                 allow_redirects=False)
    ok = r.status_code == 200
    if ok and marker:
        ok = marker.encode("utf-8") in r.content or marker in r.text
    if ok and not marker:
        # 无标记时至少要求是真实页面而非拦截页（>5KB 或 JSON）
        ok = len(r.content) > 5120 or r.content[:1] in (b"{", b"[")
    return ok, int((time.time() - t0) * 1000)


def _fetch_via_requests(proxy: str, url: str, timeout: int, marker: Optional[str] = None) -> Tuple[bool, int]:
    """curl_cffi 不可用时的降级通道（requests 指纹，对 TLS 门禁站可用率会偏低，但可用性判断诚实）。"""
    import requests as _rq
    p = {"http": proxy, "https": proxy}
    t0 = time.time()
    r = _rq.get(url, timeout=timeout, proxies=p, allow_redirects=False,
                headers={"User-Agent": UA})
    ok = r.status_code == 200
    if ok and marker:
        ok = marker in r.text or marker.encode("utf-8") in r.content
    if ok and not marker:
        ok = len(r.content) > 5120 or r.content[:1] in (b"{", b"[")
    return ok, int((time.time() - t0) * 1000)


def _test_proxy(proxy: str) -> Tuple[str, bool]:
    """v1 通用连通校验（保持兼容）。"""
    try:
        ok, _ = _fetch_via(proxy, TEST_URL, 6)
        return proxy, ok
    except Exception:
        return proxy, False


def validate(proxies: List[str], workers: int = 30, log=print) -> List[str]:
    """v1 通用校验（example.com 连通）。"""
    ok: List[str] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for p, good in ex.map(_test_proxy, proxies):
            if good:
                ok.append(p)
    return ok


def validate_target(proxies: List[str], target_url: str, marker: Optional[str] = None,
                    timeout: int = TARGET_TIMEOUT, workers: int = 40,
                    log=print) -> Dict[str, int]:
    """目标站校验（战训核心）：经代理 GET 目标站真实页面。

    target_url 须为 http/https（含主机），推荐选一个 >50KB 的重页面；
    marker 为该页唯一文案（如站名），断言命中才视为可用。
    返回 {proxy: latency_ms}，已按延迟排序。
    """
    u = urlparse(target_url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError(f"target_url 必须是 http/https 绝对地址: {target_url}")

    def one(px: str) -> Tuple[str, Optional[int]]:
        try:
            ok, ms = _fetch_via(px, target_url, timeout, marker)
            return px, (ms if ok else None)
        except Exception:
            return px, None

    good: Dict[str, int] = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for px, ms in ex.map(one, proxies):
            if ms is not None:
                good[px] = ms
    return dict(sorted(good.items(), key=lambda kv: kv[1]))


# ---------------------------------------------------------------- 三态池
# fresh: 未试 / alive: 校验通过 / dead: 连不上（TTL 后可复活） / burned: 配额烧尽（次日复活）
class PoolState:
    """代理三态账本（原子持久化；科研管理战役实战结构）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: Dict[str, Dict] = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            # 边界复现：合法 JSON 但结构错（顶层 list/string、记录值非 dict）曾绕过
            # 损坏隔离直接崩在 mark/read——必须在载入时就走隔离+重建路径
            if not isinstance(data, dict):
                raise ValueError(f"顶层应为 dict，实际 {type(data).__name__}")
            for px, rec in data.items():
                if not isinstance(rec, dict):
                    raise ValueError(f"代理记录非 dict: {px}")
                if rec.get("state") not in ("fresh", "alive", "dead", "burned", None):
                    raise ValueError(f"未知代理状态: {px}={rec.get('state')}")
            self.data = data
        except Exception as e:
            # 损坏自动重建，但必须出声 + 带时间戳隔离（审查修复：静默清零 burned
            # 等于把烧尽代理重新放回战场；二次损坏曾覆盖前一份证据）
            import sys as _sys
            print(f"⚠️ 代理池状态损坏（{type(e).__name__}: {str(e)[:60]}），隔离后从零重建: {self.path}",
                  file=_sys.stderr)
            try:
                self.path.rename(self.path.with_suffix(f".corrupt.{int(time.time())}"))
            except Exception:
                pass
            self.data = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)  # 审查修复：首跑新目录曾直接崩
        import tempfile as _tf
        fd, tmpname = _tf.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(self.data, ensure_ascii=False, indent=1))
        os.replace(tmpname, self.path)

    def mark(self, proxy: str, state: str, latency_ms: int = -1):
        rec = self.data.setdefault(proxy, {"state": "fresh", "ok": 0, "blocked": 0,
                                           "latency_ms": -1, "ts": 0})
        rec["state"] = state
        rec["ts"] = int(time.time())
        if latency_ms >= 0:
            rec["latency_ms"] = latency_ms
        if state == "burned":
            rec["blocked"] += 1
            rec["burned_until"] = int(time.time()) + 86400
        if state == "alive":
            rec["ok"] += 1
        self.save()

    def usable(self, exclude_burned: bool = True) -> List[str]:
        now = int(time.time())
        out = []
        for px, rec in self.data.items():
            if not isinstance(rec, dict):
                continue
            st = rec.get("state")
            if st == "alive":
                out.append(px)
            elif st == "fresh":
                out.append(px)
            elif st == "dead" and now - rec.get("ts", 0) > 1800:  # 死代理 30 分钟后可复活重试
                out.append(px)
            elif st == "burned":
                # 边界复现修复：过期检查曾被嵌在 not exclude_burned 之内——
                # burned 代理到期后永不复活，可用池单调萎缩
                if now > rec.get("burned_until", 0):
                    out.append(px)
        return out

    def stats(self) -> Dict[str, int]:
        from collections import Counter
        return dict(Counter(r.get("state", "fresh") for r in self.data.values()))


# ---------------------------------------------------------------- 刷新主流程
def refresh(out: str = "outputs/proxies.txt", workers: int = 30,
            min_ok: int = 5, log=print,
            target_url: Optional[str] = None, marker: Optional[str] = None,
            state_file: Optional[str] = None, sample: int = 600) -> dict:
    """构建/刷新代理池。给 target_url+marker 时用目标站校验（战训推荐），
    否则回退 v1 通用连通校验。状态写 pool state JSON（三态）。"""
    log(f"🔄 抓取免费代理源（{len(SOURCES)} 个）...")
    all_p = fetch_all(log=log)
    import random
    random.shuffle(all_p)
    batch = all_p[:sample]
    log(f"候选 {len(all_p)}，抽样校验 {len(batch)}")

    if target_url:
        good = validate_target(batch, target_url, marker=marker, workers=workers, log=log)
    else:
        ok_list = validate(batch, workers=workers, log=log)
        good = {p: -1 for p in ok_list}
    log(f"✓ 可用 {len(good)}/{len(batch)}")

    st = PoolState(Path(state_file) if state_file else Path(out).with_suffix(".pool.json"))
    for px, ms in good.items():
        st.mark(px, "alive", latency_ms=ms)
    for px in batch:
        if px in good:
            continue
        prev = st.data.get(px, {}).get("state")
        # 审查修复：本轮验证失败且此前 alive → 降级 dead（否则 stats 永久虚高）；
        # burned 不降级（当日配额烧尽是另一回事，保留到次日）
        if prev != "burned":
            st.mark(px, "dead")
    st.save()

    if len(good) < min_ok:
        log(f"⚠️ 可用代理仅 {len(good)} 个（阈值 {min_ok}），仍会写入供应急使用")
    fp = Path(out).expanduser()
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("\n".join(good) + ("\n" if good else ""), encoding="utf-8")
    log(f"✅ 可用代理 {len(good)} 个 → {fp}（三态账本: {st.path}）")
    return {"total": len(all_p), "ok": len(good), "file": str(fp),
            "stats": st.stats(), "state_file": str(st.path)}


def main() -> int:
    ap = argparse.ArgumentParser(description="🔄 免费代理池构建 v2（目标站校验 + 三态账本）")
    ap.add_argument("--refresh", action="store_true", help="抓取+验证+写入")
    ap.add_argument("--out", default="outputs/proxies.txt")
    ap.add_argument("--workers", type=int, default=30)
    ap.add_argument("--target-url", default=None, help="目标站真实页面 URL（推荐重页面 >50KB）")
    ap.add_argument("--marker", default=None, help="目标页唯一文案标记（如站名）")
    ap.add_argument("--sample", type=int, default=600, help="每轮抽样校验数")
    args = ap.parse_args()
    r = refresh(out=args.out, workers=args.workers, target_url=args.target_url,
                marker=args.marker, sample=args.sample)
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
