#!/usr/bin/env python3
"""Playwright-Python 取数器（R101 新能力，对标 Playwright 生态）：
进程内驱动 Chromium，取代 node 子进程桥的主路径——消灭 JSONL 桥协议层
（R57/R98 战训：协议序列化/等待语义是 bug 最高发区）。

- PWBrowserFetcher: 与 fetchers.BrowserFetcher 同接口（fetch/fetch_list 级的
  fetch_pages / fetch(Request)），引擎/详情/列表可无感切换。
- 启用口径：anti["browser_backend"] = "playwright"（默认 auto：装了
  playwright-python 且能启动就用它，否则回落 node 桥——桥仍是 CDP 附加
  登录态的备用通道，不删除）。

用法边界：本模块只做"单页渲染取 HTML"，动作链/captcha 人机协同仍走原桥。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

# R20 隐身开关：内置广告/追踪域名清单（与 scripts/browser_common.cjs 的
# AD_DOMAINS 同源同口径——两处改动必须同步，注释互指）
_AD_DOMAINS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "google-analytics.com", "googletagmanager.com", "googletagservices.com",
    "adservice.google.com", "ads.yahoo.com", "adnxs.com", "criteo.com",
    "criteo.net", "taboola.com", "outbrain.com", "pubmatic.com",
    "rubiconproject.com", "openx.net", "casalemedia.com", "smartadserver.com",
    "scorecardresearch.com", "quantserve.com", "moatads.com", "adform.net",
    "sharethrough.com", "teads.tv", "zedo.com", "sizmek.com",
)


def playwright_available() -> bool:
    """playwright-python 可导入即认为可用（浏览器二进制缺失在 launch 时暴露）。"""
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


class PWBrowserFetcher:
    """playwright-python 进程内浏览器取数器（与 BrowserFetcher 同口径）。"""

    def __init__(self, source: Dict[str, Any], anti: Dict[str, Any],
                 config: Dict[str, Any], out_root: Path):
        self.source = source
        self.anti = anti
        self.config = config
        self.out_root = out_root
        self.timeout = int(anti.get("timeout", 30)) * 1000  # playwright 用 ms
        self._lock = threading.RLock()  # 可重入：fetch_pages/fetch 持锁后 ensure/_ensure_browser 再次加锁不死锁
        self._pw = None
        self._browser = None
        self._context = None   # R20：仅在有 stealth 上下文参数（时区/语言/UA）时创建

    # ---- R20 隐身细粒度开关（anti_bot.stealth_opts）----
    def _stealth_opts(self) -> Dict[str, Any]:
        o = self.anti.get("stealth_opts")
        return o if isinstance(o, dict) else {}

    def _launch_args(self) -> List[str]:
        """stealth_opts → Chromium 启动参数（与 node 桥 parseStealthOpts 同口径）。"""
        o = self._stealth_opts()
        args: List[str] = []
        if o.get("hide_canvas") is True:
            args.append("--fingerprinting-canvas-image-data-noise")
        if o.get("allow_webgl") is False:
            args += ["--disable-webgl", "--disable-webgl-image-chromium", "--disable-webgl2"]
        if o.get("block_webrtc") is True:
            args += ["--webrtc-ip-handling-policy=disable_non_proxied_udp",
                     "--force-webrtc-ip-handling-policy"]
        if o.get("dns_over_https") is True:
            args += ["--dns-over-https-mode=secure",
                     "--dns-over-https-templates=https://cloudflare-dns.com/dns-query"]
        extra = o.get("extra_flags")
        if isinstance(extra, list):
            args += [str(a) for a in extra[:10]
                     if isinstance(a, str) and a.startswith("--") and len(a) < 200]
        return args

    def _context_kwargs(self) -> Dict[str, Any]:
        """stealth_opts → new_context 参数（时区/语言/UA）。"""
        o = self._stealth_opts()
        kw: Dict[str, Any] = {}
        if isinstance(o.get("timezone"), str) and o["timezone"]:
            kw["timezone_id"] = o["timezone"]
        if isinstance(o.get("locale"), str) and o["locale"]:
            kw["locale"] = o["locale"]
        if isinstance(o.get("user_agent"), str) and o["user_agent"]:
            kw["user_agent"] = o["user_agent"]
        return kw

    def _install_domain_blocking(self) -> None:
        """域名级阻断（广告清单 + 用户清单）：只作用于自建 context（CDP 附加不动）。"""
        o = self._stealth_opts()
        if not self._context or not (o.get("block_ads") or o.get("blocked_domains")):
            return
        doms = []
        if o.get("block_ads"):
            doms += _AD_DOMAINS
        for d in (o.get("blocked_domains") or [])[:200]:
            if isinstance(d, str) and d:
                doms.append(d.lower())
        if not doms:
            return

        def _handler(route):
            try:
                host = (urlsplit(route.request.url).hostname or "").lower()
            except Exception:
                return route.continue_()
            if any(host == d or host.endswith("." + d) for d in doms):
                return route.abort()
            return route.continue_()

        try:
            self._context.route("**/*", _handler)
        except Exception:
            pass  # 老内核不支持 route：静默降级（与桥同口径）

    # ---- 生命周期 ----
    def _ensure_browser(self, cdp: str = ""):
        with self._lock:
            if self._browser is not None:
                return
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            try:
                if cdp:
                    # CDP 附加：复用用户已登录的调试 Chrome（与桥的 --cdp 同语义）
                    self._browser = self._pw.chromium.connect_over_cdp(cdp)
                else:
                    # 审查八轮（H）：曾硬编码 headless=True——用户配 headless:false
                    # 想走有头抗检测时被静默无视（engine 现已透传该键）
                    self._browser = self._pw.chromium.launch(
                        headless=bool(self.source.get("headless", True)),
                        args=self._launch_args() or None)
                # R20：有 stealth 上下文参数时建共享 context（cookie 在会话内保持）
                _kw = self._context_kwargs()
                self._context = self._browser.new_context(**_kw) if _kw else None
                self._install_domain_blocking()
            except Exception:
                # 启动失败不能留下半初始化状态：close() 只认 _browser，
                # _pw 泄漏会让 playwright 驱动进程挂住
                self._pw.stop()
                self._pw = None
                raise

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        finally:
            self._browser = None
            self._context = None
            self._pw = None

    # ---- 取数 ----
    def ensure(self) -> None:
        """预先启动浏览器（R101：启动失败必须在此抛出——引擎据此回落 node 桥，
        而不是每页静默渲染失败、全部 browser_miss）。"""
        self._ensure_browser(cdp=str(self.source.get("cdp") or ""))

    def _render(self, url: str, wait_ms: int, js_actions: List[Dict[str, Any]]) -> str:
        self._ensure_browser(cdp=str(self.source.get("cdp") or ""))
        # R20：有共享 context 时复用（cookie 在会话内保持）
        page = (self._context or self._browser).new_page()
        try:
            # R116 智能等待：wait_until 支持 networkidle（SPA 接口拖尾）——
            # 站点长连接多时 networkidle 会超时，此时回落 domcontentloaded+固定等待
            wu = str(self.source.get("wait_until") or "domcontentloaded")
            try:
                page.goto(url, timeout=self.timeout, wait_until=wu)
            except Exception:
                if wu != "domcontentloaded":
                    # OCR R131（H）：超时后页面可能已半加载——直接重发 goto 会
                    # 在半残状态上叠导航。先回空白页复位再重试
                    try:
                        page.goto("about:blank", timeout=5000)
                    except Exception:
                        pass
                    page.goto(url, timeout=self.timeout, wait_until="domcontentloaded")
                else:
                    raise
            page.wait_for_timeout(wait_ms)
            # R116 智能等待：DOM 稳定检测（连续两次采样长度一致即视为渲染完成，
            # 最多再等 5 轮×500ms——比固定 sleep 更快也更稳）
            if self.source.get("dom_stable"):
                last = -1
                for _ in range(5):
                    cur = page.evaluate("document.documentElement.outerHTML.length")
                    if cur == last:
                        break
                    last = cur
                    page.wait_for_timeout(500)
            for act in js_actions or []:
                t = str(act.get("type", ""))
                sel = act.get("selector", "")
                if t == "click" and sel:
                    try:
                        page.locator(sel).first.click(timeout=self.timeout)
                        page.wait_for_timeout(int(act.get("wait_ms", 800)))
                    except Exception as _ck:
                        # OCR R131（M）：单步失败不弃整页（桥同口径），但必须留痕——
                        # 静默吞掉时"点击了却没生效"无从排查
                        print(f"[browser_pw] 点击动作失败 {sel}: {type(_ck).__name__}: {str(_ck)[:100]}", file=sys.stderr)
                elif t == "scroll" or t == "scroll_bottom":
                    for _ in range(int(act.get("count", 3))):
                        page.mouse.wheel(0, 20000)
                        page.wait_for_timeout(int(act.get("wait_ms", 600)))
                elif t == "wait":
                    page.wait_for_timeout(int(act.get("wait_ms", 1000)))
            return page.content()
        finally:
            page.close()

    def fetch_pages(self, urls: List[str], wait_ms: int = 1200,
                    js_actions: Optional[List[Dict[str, Any]]] = None,
                    workers: int = 1) -> Dict[str, str]:
        """批量渲染：返回 {url: html}。
        workers=1：单实例顺序导航（默认，最省资源）。
        workers>1：分片给多个独立 playwright 实例并行渲染（sync API 非线程安全，
        每分片必须持有自己的实例；实例各自启动，互不共享）。"""
        if workers > 1 and len(urls) > 1:
            return self._fetch_pages_parallel(urls, wait_ms, js_actions, workers)
        out: Dict[str, str] = {}
        with self._lock:  # sync API 非线程安全——串行化（与桥的顺序导航同口径）
            self.ensure()  # R101：启动失败早暴露，绝不吞成逐页空 HTML
            for u in urls:
                if not u:
                    continue
                try:
                    out[u] = self._render(u, wait_ms, js_actions)
                except Exception as e:
                    print(f"[browser_pw] 渲染失败 {u}: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
        return out

    def _fetch_pages_parallel(self, urls: List[str], wait_ms: int,
                              js_actions: Optional[List[Dict[str, Any]]],
                              workers: int) -> Dict[str, str]:
        """多实例分片并行：R116 新能力。每个分片独立 PWBrowserFetcher
        （独立 playwright 进程句柄，规避 sync API 线程限制）。
        单分片启动失败 → 该分片 URL 标 browser_miss（与桥口径一致），不影响其余分片。"""
        import threading as _th
        urls = [u for u in urls if u]
        shards: List[List[str]] = [[] for _ in range(min(workers, len(urls)))]
        for i, u in enumerate(urls):
            shards[i % len(shards)].append(u)
        results: Dict[str, str] = {}
        results_lock = _th.Lock()

        def _worker(shard: List[str]):
            if not shard:
                return
            sub = PWBrowserFetcher(self.source, self.anti, self.config, self.out_root)
            try:
                sub.ensure()  # 启动失败 → 整分片标 miss（引擎可选回落）
                for u in shard:
                    try:
                        _html = sub._render(u, wait_ms, js_actions)
                    except Exception as e:
                        print(f"[browser_pw] 渲染失败 {u}: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
                        continue
                    with results_lock:  # OCR R131（H）：锁曾建未用——并发写共享 dict
                        results[u] = _html
            except Exception as e:
                with results_lock:
                    for u in shard:
                        results.setdefault(u, "")
                print(f"[browser_pw] 分片启动失败: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            finally:
                sub.close()

        threads = [_th.Thread(target=_worker, args=(shard,), daemon=True)
                   for shard in shards if shard]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def fetch(self, url: str, wait_ms: int = 1200,
              js_actions: Optional[List[Dict[str, Any]]] = None) -> str:
        """单页渲染。"""
        with self._lock:
            return self._render(url, wait_ms, js_actions)
