#!/usr/bin/env python3
"""内置取数器：HTTP（连接池） / 浏览器 / 桥。"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional

from ..cookies import _norm_domain
from ..protocols import BaseFetcher, Request, Response
from ..session import SessionPool
from ..antibot import detect_block
from ..core import update_cookie_jar, jar_cookie_header, log


class _ProfileLocks:
    """按路径串行化的进程内锁（审查 P1：并发 worker 同 profile 交互抓取
    会互删 SingletonLock、在对方活着的 profile 上再拉浏览器）。"""

    def __init__(self):
        import threading as _th
        self._reg = _th.Lock()
        self._locks: dict = {}

    def for_path(self, p: Path):
        key = str(p)
        with self._reg:
            # OCR R131 二轮（CRITICAL 复核）：曾做"超 1024 淘汰未持有项"——但
            # 「试探成功→del→release」与另一线程"已持旧引用、尚未 acquire"之间存在
            # 互斥破坏窗口（旧锁被逐后新锁实例并行进临界区）。Lock 对象单枚仅
            # 数十字节，永不淘汰的内存代价可忽略——互斥正确性优先，不再淘汰
            if key not in self._locks:
                import threading as _th
                self._locks[key] = _th.Lock()
            return self._locks[key]


_INTERACTIVE_PROFILE_LOCKS = _ProfileLocks()


def _spawn_bridge(cmd: List[str], env: Dict[str, str]):
    """启动浏览器桥 + 后台排空 stderr（防管道写满死锁）。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", env=env)
    errbuf: List[str] = []
    if proc.stderr:
        def _drain():
            try:
                for ln in proc.stderr:
                    errbuf.append(ln)
            except Exception:
                pass
        threading.Thread(target=_drain, daemon=True).start()
    return proc, errbuf


def _wait_bridge(proc, errbuf, timeout: int = 1800):
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            rc = proc.wait(timeout=10)
        except Exception:
            rc = -9
    return rc, "".join(errbuf)


def parse_cookie_header(s: str):
    """'a=1; b=2' -> [('a','1'), ('b','2')]（无等号/空片段忽略）。"""
    pairs = []
    for part in (s or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        if k.strip():
            pairs.append((k.strip(), v.strip()))
    return pairs


def seed_archive_into_jar(jar, archive_domain: str, pairs) -> None:
    """把已存档登录态按名播种进 CookieJar：
    - 域限定为 .<archive_domain>（自身+子域才携带，绝不发给第三方域）
    - 之后交给 RFC6265 自然合并：服务端 Set-Cookie 同名覆盖、异名共存，
      长任务中途不会因首个 Set-Cookie 而把登录态整串顶掉。"""
    from http.cookiejar import Cookie
    dom = (archive_domain or "").lower().lstrip(".")
    if not dom or not pairs:
        return
    for item in pairs:
        name, value = item[0], item[1]
        secure = bool(item[2]) if len(item) > 2 else False
        jar.set_cookie(Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain="." + dom, domain_specified=True, domain_initial_dot=True,
            path="/", path_specified=True, secure=secure, expires=None,
            discard=True, comment=None, comment_url=None, rest={}, rfc2109=False))


def _reconcile_host_only_overrides(jar, archive_domain: str, names, req_host: str = "") -> None:
    """服务端以 Host-Only 形态（无 Domain 属性）重发同名 Cookie 时，其 jar 键
    与播种的点域版本不同、不会互相覆盖——RFC6265§5.4 会把两个同名都发出去、
    多数服务端取第一个（陈旧的播种值）。此时删掉播种版，让服务端新版成为唯一权威。
    req_host：当前请求主机——Host-Only 可能落在 www./子域形态的主机上，同样算权威。"""
    dom = (archive_domain or "").lower().lstrip(".")
    req_host = (req_host or "").lower()
    if not dom or not names:
        return
    names = set(names)
    try:
        host_only = {}   # 服务端权威版（Host-Only：域==归档主域，或==当前请求主机）
        dotted = {}      # 我们播种的旧版（点域）
        for cookie in list(jar):
            if cookie.name not in names:
                continue
            raw = cookie.domain.lower()
            dd = raw.lstrip(".")
            is_dotted = raw.startswith(".")
            if is_dotted and dd == dom:
                dotted[cookie.name] = cookie
            elif not is_dotted and (dd == dom or (req_host and dd == req_host)):
                # 只认归档主域/当前请求主机上的 Host-Only；祖先域 Host-Only 不会
                # 随本请求发送，不能当权威（否则会误删仍会被发出的播种版）
                host_only[cookie.name] = cookie
        # 仅当存在 Host-Only 权威版时才删点域播种版；没有则播种版继续生效
        for name, ck in dotted.items():
            if name in host_only:
                try:
                    jar.clear(ck.domain, ck.path, ck.name)
                except KeyError:
                    pass
    except Exception:
        pass


def _merge_url_params(url: str, params: Optional[dict]) -> str:
    """把 params 合并进 URL 查询串（同名键整组替换、其余保序）。

    收官十五轮（core 深审 M2/M4）：原 dict(parse_qsl(...)) 实现会把 URL 里合法的
    重复参数折成最后一个（?tag=a&tag=b → tag=b）；浏览器/桥路径则完全不合并
    meta.params（json_paged/offset 翻页在浏览器模式静默重复抓第 1 页）。两处统一到
    本函数：HttpFetcher._build_url 与 BrowserFetcher.fetch 共用。
    """
    if not params:
        return url
    parts = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    _repl = {str(k): str(v) for k, v in params.items()}
    out_pairs = [(k, v) for k, v in pairs if k not in _repl]
    out_pairs.extend(_repl.items())
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                   urllib.parse.urlencode(out_pairs), parts.fragment))


class HttpFetcher(BaseFetcher):
    """HTTP 取数器：curl_cffi（TLS 指纹伪装）→ requests → urllib 自动选后端；
    支持代理池轮换（anti._proxy_pool）与 429 Retry-After 限流重试。"""
    name = "http"

    def __init__(self, config, task_vars, anti):
        super().__init__(config, task_vars, anti)
        self._cache: dict = {} if config.get("cache") else None
        # OCR R131（M）：多 worker 并发读写共享缓存——check-then-act 与淘汰
        # 曾无锁（iter 期间写入可抛 RuntimeError）
        import threading as _th
        self._cache_lock = _th.Lock()
        # 🍪 自动会话（HTTP 路线）：目标域名有已存档登录态（用户从调试 Chrome
        #    导入过/任务存过）→ 播种进会话 jar（域名限定+按名合并），
        #    "登录一次以后全自动"
        self._archive_domain = str(anti.get("cookie_domain") or "")
        self._archive_pairs = []
        self._archive_logged = False
        if self._archive_domain:
            try:
                # 结构化读取：保留 secure 标记（防 Secure 令牌被明文重放到 http://），
                # 且按 cookie 自身域过滤（与归档域不同树的第三方会话不播种）
                from ..cookies import load_cookies
                _dom = self._archive_domain.lstrip(".")

                def _related(d):
                    d = str(d or "").lower().lstrip(".")
                    return bool(d) and (d == _dom or _dom.endswith("." + d)
                                        or d.endswith("." + _dom))

                rows = [c for c in (load_cookies(self._archive_domain) or [])
                        if c.get("name") and _related(c.get("domain"))]
                # 同名多作用域去重：归档主域条目排最后（播种时后写者胜出），
                # 保证主域权威初值胜出且不受存档文件行序影响
                rows.sort(key=lambda c: 0 if str(c.get("domain", "")).lower().lstrip(".")
                          in (_dom, "www." + _dom) else 1, reverse=True)
                # rows 为空（无同树条目）时不得回退到未过滤的 cookie_header——
                # 那会把刚被判定为不同树的第三方会话又播种回目标域
                self._archive_pairs = [(str(c["name"]), str(c["value"]),
                                        bool(c.get("secure"))) for c in rows]
            except Exception as _e:
                self._archive_pairs = []
                log(f"⚠️ 已存档登录态装载失败（domain={self._archive_domain}）：{_e}，本次将以未登录状态请求", "WARN")
        from ..core import make_http_client
        self.client = make_http_client(anti)
        # 基准速率快照（诊断面保留）：自适应状态机（AdaptiveThrottle）已在客户端
        # 创建时固化基准，这里同步一份到 anti 供外部工具/脚本读取
        self.anti.setdefault("base_min_interval",
                             float(getattr(self.client, "min_interval", 1.0) or 1.0))
        self.proxy_pool = anti.get("_proxy_pool")
        if self.proxy_pool is None and anti.get("proxies"):
            from ..proxy import ProxyPool
            self.proxy_pool = ProxyPool(anti.get("proxies"), anti.get("proxy_mode", "round_robin"), warn=anti.get("_log_cb"))
        self._last_proxy = None
        # HTTP 会话池（Crawlee SessionPool 思路）：按域名 Cookie/UA/代理，封禁自动换会话
        sp_proxies = list(anti.get("proxies") or [])
        if not sp_proxies and anti.get("proxy"):
            sp_proxies = [anti["proxy"]]
        self.sessions = SessionPool(proxies=sp_proxies or None,
                                    max_per_domain=int(anti.get("session_pool_max", 3)),
                                    rotate_on_errors=int(anti.get("session_rotate_on_errors", 2)),
                                    proxy_mode=anti.get("proxy_mode", "round_robin"))
        if self.proxy_pool is not None:
            self.sessions._proxy_source = self.proxy_pool.next
            self.sessions._proxy_ok_cb = self.proxy_pool.mark_ok
            self.sessions._proxy_fail_cb = self.proxy_pool.mark_fail
        self._cur_session = None
        # 死代理快速失败：代理模式下客户端不再同一代理内部退避 3 次，
        # 第一次失败就抛回引擎 → 新会话自动换下一个代理（比死等 2+4+8s 快得多）
        if self.sessions.size > 0 or self.proxy_pool is not None:
            try:
                if getattr(self.client, "max_retries", 3) > 1:
                    self.client.max_retries = 1
            except Exception:
                pass

    def _autothrottle(self, kind: str) -> None:
        """拦截自适应降速（对标 Scrapy AUTOTHROTTLE）——状态已下沉 core 客户端
        （AdaptiveThrottle，三后端共用：间隔翻倍、floor 起步、cap 封顶）。
        调用契约：只传客户端看不见的内容级信号（status==200 的风控皮），
        HTTP 状态级由客户端自记（否则双计）。这里只做播报与 anti 簿记。"""
        try:
            _at = getattr(self.client, "_at", None)
            if _at is None:
                return
            _at.note_block(kind)     # 写入事件（内部已置 last_block）
            _nxt = _at.pop_block()   # 锁内原子读清，防多 worker 播报竞态
            if _nxt <= 0:
                return
            self.anti["min_interval"] = _nxt
            msg = f"🐢 检测到拦截[{kind}]，自动降速：请求间隔 → {_nxt:.1f}s（连续命中会继续放缓）"
            _cb = self.anti.get("_log_cb")
            if _cb:
                _cb(msg)
            else:
                log("  " + msg, "WARN")
        except Exception as e:
            log(f"autothrottle 播报异常（限速状态不受影响）: {type(e).__name__}: {e}", "DEBUG")

    def _announce_throttle(self) -> None:
        """播报客户端静默记下的自适应事件（HTTP 状态级拦截），读后清零。"""
        try:
            _at = getattr(self.client, "_at", None)
            if _at is None:
                return
            _nb = _at.pop_block()
            if _nb > 0:
                self.anti["min_interval"] = _nb
                _cb = self.anti.get("_log_cb")
                _msg = f"🐢 自动降速：请求间隔 → {_nb:.1f}s（连续命中会继续放缓）"
                if _cb:
                    _cb(_msg)
                else:
                    log("  " + _msg, "WARN")
        except Exception as e:
            log(f"autothrottle 播报异常（限速状态不受影响）: {type(e).__name__}: {e}", "DEBUG")

    def _maybe_speedup(self) -> None:
        """播报恢复事件：note_ok 连击由客户端自记，恢复发生时 last_restore
        带出新间隔，这里播报并清零（不重复计数）。"""
        try:
            _at = getattr(self.client, "_at", None)
            if _at is None:
                return
            _nxt = _at.pop_restore()
            if _nxt > 0:
                self.anti["min_interval"] = _nxt
                msg = f"🐇 连续成功，逐步恢复速度：请求间隔 → {_nxt:.1f}s"
                _cb = self.anti.get("_log_cb")
                if _cb:
                    _cb(msg)
                else:
                    log("  " + msg)
        except Exception as e:
            log(f"autothrottle 播报异常（限速状态不受影响）: {type(e).__name__}: {e}", "DEBUG")

    def _pick_proxy(self) -> str:
        """从代理池取一个可用代理；没有则直连（None）。"""
        if self.proxy_pool is None:
            return self.anti.get("proxy") or None
        p = self.proxy_pool.next()
        self._last_proxy = p
        return p

    def _report_proxy(self, ok: bool) -> None:
        if self.proxy_pool is not None and self._last_proxy:
            if ok:
                self.proxy_pool.mark_ok(self._last_proxy)
            else:
                self.proxy_pool.mark_fail(self._last_proxy)
            self._last_proxy = None

    def _sign_headers(self, req: Request) -> dict:
        """请求头：source.headers 静态头 + 签名注入钩子（source.sign_hook = "module:function"，
        函数签名 fn(headers, url, body) -> headers，用于 x-s / x-t 等签名头）。"""
        headers = dict(req.headers or {})
        headers.update(self.config.get("headers", {}) or {})
        hook = self.config.get("sign_hook")
        if not hook:
            return headers
        mod_name, fn_name = hook.split(":", 1)
        import importlib
        try:
            fn = getattr(importlib.import_module(mod_name), fn_name)
        except Exception as e:
            raise RuntimeError(f"签名钩子导入失败 {hook}: {e}")
        return fn(headers, req.url, req.body) or headers

    def fetch(self, req: Request) -> Response:
        url = self._build_url(req)   # 先合并 query/meta.params——缓存键必须用
        if self._cache is not None and req.method == "GET":   # 最终 URL（审查 P1：
            with self._cache_lock:                            # 翻页参数在 meta 里，
                hit = self._cache.get(url)                    # 旧键曾让每页都返回第 1 页）
            if hit is not None:
                return hit
        # 会话池：取当前域会话（含 Cookie jar/UA/代理），封禁自动轮换
        sess = self.sessions.acquire(url)
        self._cur_session = sess
        if self._archive_pairs and not sess.archive_seeded:
            with sess.jlock:  # R6 修复：播种也须持锁（CookieJar 非线程安全）
                # R16 修复：检查也移入锁内（真双检）——检查在锁外时，两个并发 worker
                # 都可能在对方播种前通过判断，旧归档值二次 set_cookie 顶掉服务端
                # 刚下发的新 Cookie，恰好毁掉登录态
                if not sess.archive_seeded:
                    seed_archive_into_jar(sess.jar, self._archive_domain, self._archive_pairs)
                    sess.archive_seeded = True  # 锁内置位：关掉双播种窗口
            if not self._archive_logged:
                self._archive_logged = True
                _cb = self.anti.get("_log_cb")
                if _cb:
                    _cb(f"🍪 已把 {self._archive_domain} 的已保存登录态注入会话"
                        "（服务端下发的新 Cookie 会自动合并，不会顶掉登录态）")
        headers = self._sign_headers(req)
        headers.setdefault("User-Agent", sess.ua)
        if self._archive_pairs:
            from urllib.parse import urlparse as _up
            with sess.jlock:  # R6 修复：reconcile 迭代+clear 须持锁
                # 审查八轮（CRITICAL）：_archive_pairs 是 (name, value, secure) 三元组
                # （见本文件 181-182 行与 seed_archive_into_jar 的 len(item) > 2 判定），
                # 此处曾按两元组解包——任何命中已存档登录态的域每次 fetch 必抛
                # ValueError，被上游吞成"请求失败"→ 重试 3 次 → nodata（用户被误导
                # 去查反爬/网络）。按索引取名字，兼容二元组历史形态。
                _ap_names = [p[0] if isinstance(p, (tuple, list)) and p else str(p)
                             for p in self._archive_pairs]
                _reconcile_host_only_overrides(
                    sess.jar, self._archive_domain,
                    _ap_names,
                    req_host=(_up(url).hostname or "").lower())
        with sess.jlock:  # CookieJar 非线程安全（审查 P2，R14）
            ck = jar_cookie_header(sess.jar, url)
        if ck:
            headers.setdefault("Cookie", ck)
        try:
            res = self.client.request(url, method=req.method, data=req.body,
                                      headers=headers, allow_html_404=True, proxy=sess.proxy,
                                      max_size=int(self.config.get("max_size", 20 * 1024 * 1024)))
        except Exception:
            # 客户端抛异常（网络层/自定义客户端）：继续抛出。
            # 审查修复 P1：曾 blocked=True——DNS 抖动/超时曾瞬间丢弃含登录态
            # cookie 的会话并冷却好代理，登录任务从此一路撞登录墙
            self.sessions.report(sess, ok=False, blocked=False)
            raise
        # 响应 Set-Cookie → 会话 Cookie jar（真 cookie 持久化）
        try:
            with sess.jlock:  # CookieJar 非线程安全（审查 P2，R14）
                update_cookie_jar(sess.jar, res.get("url") or url,
                                  res.get("headers"), res.get("raw_headers"))
        except Exception as e:
            log(f"  Cookie jar 更新失败（{type(e).__name__}: {e}）", "DEBUG")
        # 封禁识别：HTTP 200 但被风控的页面也能识破
        ok = bool(res.get("ok"))
        bd = detect_block(res.get("status", 0), res.get("text", ""), res.get("headers"), url)
        # login 登录墙是"预期状态"不是会话污染：只提示不轮换（否则需要登录的 API 每次换会话丢 cookie）
        # http_error（4xx/5xx 通用错误）也不是"反爬拦截"：交给下方按 4xx 永久跳过 / 5xx 重试处理
        blocked = bd["kind"] not in ("none", "login", "http_error")
        self.sessions.report(sess, ok=ok and not blocked, blocked=blocked)
        if blocked:
            if self.anti.get("_block_stats") is not None:
                self.anti["_block_stats"][bd["kind"]] = self.anti["_block_stats"].get(bd["kind"], 0) + 1
            # 只补客户端看不见的内容级信号：429/403/5xx 已由客户端 note_block，
            # 这里再记就双计（同一信号间隔翻两次）。200 皮风控页客户端只能当成功。
            if res.get("status", 0) == 200:
                self._autothrottle(bd["kind"])
            else:
                self._announce_throttle()
            from ..protocols import RateLimitedError
            status = res.get("status", 0)
            # 429 必须带上服务端 Retry-After（秒或 HTTP-date），引擎才能按它精确调度
            ra = self._retry_after(res.get("headers") or {}) if status == 429 else 0.0
            raise RateLimitedError(req.url, retry_after=ra, status=status or 403,
                                   detail=f"反爬拦截[{bd['kind']}] {bd['detail']}")
        # 请求失败：抛错交给引擎队列重试（客户端内部重试耗尽后不再静默吞掉）
        if not ok:
            self._announce_throttle()
            status = res.get("status", 0)
            from ..protocols import RateLimitedError, PermanentFetchError
            if status == 429:
                ra = self._retry_after(res.get("headers") or {})
                raise RateLimitedError(req.url, retry_after=ra, status=429)
            # 4xx 客户端错误（除 403 反爬/408 超时/429 限流）是永久性失败：重试无意义，引擎跳过不计错
            # 审查修复（P2）：408 超时是暂态却曾被永久跳过——与注释自相矛盾
            if 400 <= status < 500 and status != 408:
                raise PermanentFetchError(req.url, status=status,
                                          detail=str(res.get("text", ""))[:200])
            raise RuntimeError(f"HTTP {status}: {str(res.get('text', ''))[:200]}")
        body = res.get("body", b"")
        text = res.get("text", "")
        # 响应大小限制：防内存爆（max_size 字节，默认 20MB）
        max_size = int(self.config.get("max_size", 20 * 1024 * 1024))
        # 收官十五轮（core 深审 H2，实测）：客户端层（三后端）已按 max_size 截断并
        # 打 truncated 标记；而这里曾只比 `len(body) > max_size`——恒假（body 已截），
        # "截断曾静默"的 P1 修复实为死代码，半截 JSON 被当完整数据。改为读标记 + 兜底长度判
        _truncated = bool(res.get("truncated")) or (max_size and len(body) > max_size)
        if _truncated:
            body = body[:max_size]
            text = text[:max_size]
            # 审查修复（P1）：截断曾静默——JSON 任务会误诊"返回不是 JSON"，
            # HTML 任务尾部记录无声丢失
            log(f"⚠️ 响应超过 max_size 已截断（{req.url}）——大响应请调大 max_size", "WARN")
        resp = Response(request=req, status=res.get("status", 0),
                        body=body, text=text,
                        json=res.get("json"), url=res.get("url", req.url),
                        truncated=_truncated)
        self._announce_throttle()   # 延迟感知降速事件也在成功路径上播报（否则静默爬升到 8s）
        self._maybe_speedup()
        if self._cache is not None and req.method == "GET" and ok:
            # 内存缓存上限（防长任务内存爆炸）：超 2000 条丢最旧
            with self._cache_lock:
                if len(self._cache) > 2000:
                    try:
                        self._cache.pop(next(iter(self._cache)))
                    except Exception as e:
                        log(f"  内存缓存淘汰失败（{type(e).__name__}）", "DEBUG")
                self._cache[url] = resp  # 与查找同键：最终 URL（含合并后的分页参数）
        return resp

    def _build_url(self, req: Request) -> str:
        """合并 source.query 静态参数 + 请求 meta.params（分页等）到 URL。"""
        params = dict(self.config.get("query", {}) or {})
        params.update(req.meta.get("params", {}) or {})
        return _merge_url_params(req.url, params)

    @staticmethod
    def _retry_after(headers: dict) -> float:
        """解析 Retry-After（秒 或 HTTP-date）。"""
        import email.utils
        v = (headers or {}).get("retry-after", "")
        if not v:
            return 0.0
        v = str(v).strip()
        try:
            return max(0.0, float(v))
        except ValueError:
            try:
                dt = email.utils.parsedate_to_datetime(v)
                import time as _t
                return max(0.0, (dt.timestamp() - _t.time()))
            except Exception:
                return 0.0


class ScraplingFetcher(BaseFetcher):
    """Scrapling 取数器（可选依赖，懒加载）：curl_cffi 内核 + 可选 StealthyFetcher 过 Cloudflare/WAF。

    任务配置 source: {"type": "scrapling", "stealthy": true|false, "impersonate": "chrome"}
    - 未安装 scrapling 时：抛 PermanentFetchError，引擎会自动升级到浏览器模式（不空转）
    - stealthy=true 且已装 camoufox（scrapling install）：StealthyFetcher 自动过 Cloudflare
    - stealthy 未装 camoufox：自动降级为静态 Fetcher 并给出提示
    """
    name = "scrapling"

    def __init__(self, config, task_vars, anti):
        super().__init__(config, task_vars, anti)
        self.impersonate = config.get("impersonate") or "chrome"
        self.stealthy = bool(config.get("stealthy"))

    def fetch(self, req: Request) -> Response:
        try:
            from scrapling.fetchers import Fetcher, StealthyFetcher
        except Exception as e:  # noqa: BLE001 - 未安装 scrapling 时给出明确错误
            from ..protocols import PermanentFetchError
            # 注意：PermanentFetchError 第一个参数是 url（不是 message），message 走 detail
            raise PermanentFetchError(
                url=req.url, status=0,
                detail=f"未安装 scrapling（可选反爬取数器）。安装：python3 -m pip install \"scrapling[fetchers]\"；"
                       f"或忽略此报错，工具会自动改走浏览器模式。详情：{e}") from e
        headers = dict(req.headers or {})
        headers.update(self.config.get("headers", {}) or {})
        proxies = None
        _px = self.anti.get("proxy") or None
        if _px:
            proxies = {"http": _px, "https": _px}
        kwargs = dict(impersonate=self.impersonate, timeout=30, headers=headers or None,
                      proxies=proxies, follow_redirects=True)
        # 收官十三轮（审查 L，实测）：scrapling 只发 GET——req.method/body 既不
        # 使用也不告警，自定义 parser 发 POST 时静默降级成 GET 且返回值照常被
        # 解析计成功。明确不支持（永久错误），别让调用方拿到错数据
        if str(req.method or "GET").upper() != "GET" or req.body:
            from ..protocols import PermanentFetchError
            raise PermanentFetchError(
                url=req.url, status=0,
                detail=f"scrapling 取数器仅支持 GET（收到 {req.method}"
                       f"{' 带 body' if req.body else ''}）——请改用 http 后端或浏览器模式")
        try:
            if self.stealthy:
                try:
                    # 毫秒单位超时；camoufox 未安装会抛 ImportError
                    r = StealthyFetcher.fetch(req.url, timeout=60000,
                                              solve_cloudflare=True, network_idle=True)
                except Exception as _e:  # noqa: BLE001 - 无 camoufox 自动降级静态
                    log(f"⚠️ scrapling stealthy 不可用（{_e}），降级为静态 Fetcher")
                    r = Fetcher.get(req.url, **kwargs)
            else:
                r = Fetcher.get(req.url, **kwargs)
        except Exception as e:  # noqa: BLE001
            # 收官十三轮（审查 M，实测）：曾把一切失败（连接重置/超时/DNS）包成
            # PermanentFetchError——引擎对永久错误"跳过不重试不计错误"，一次网络
            # 抖动就永久放弃该 URL，max_retries 完全失效，最终以 0 条收场并给出
            # 误导性诊断。暂态网络错误按普通异常抛出交给引擎退避重试
            _msg = f"{type(e).__name__}: {e}"
            if any(k in _msg.lower() for k in (
                    "connection", "timeout", "timed out", "reset", "refused",
                    "resolve", "unreachable", "ssl", "temporarily")):
                raise RuntimeError(f"scrapling 网络错误（可重试）: {_msg}") from e
            from ..protocols import PermanentFetchError
            raise PermanentFetchError(url=req.url, status=0,
                                      detail=f"scrapling 抓取失败（非暂态）: {_msg}") from e
        # scrapling 0.4.x 的 Response：正文在 .body(bytes)，.text 属性可能为空，优先 .body
        _body = getattr(r, "body", None)
        if not _body:
            _body = (getattr(r, "text", "") or "").encode("utf-8", "replace")
        _enc = getattr(r, "encoding", None) or "utf-8"
        _text = ""
        try:
            _html = getattr(r, "html_content", None)
            _text = str(_html) if _html is not None else _body.decode(_enc, errors="replace")
        except Exception:
            _text = _body.decode("utf-8", errors="replace")
        return Response(request=req, status=int(getattr(r, "status", 0) or 0),
                        body=_body, text=_text,
                        url=getattr(r, "url", "") or req.url)


class BridgeFetcher(BaseFetcher):
    """桥取数器：调用 Node 桥（过 WAF / 驱动 Vue 等复杂页面），一次性返回整批记录。

    任务配置 source: {"type": "bridge", "bridge": "../scripts/ggzy_bridge.cjs",
                      "bridge_params": {"keyword": "{{keyword}}", "stage": "{{stage}}"}}
    桥协议（stdout JSONL）：{"type":"meta"|"page"|"captcha"|"error"|"done", ...}
    验证码：自动解（captchaDir 协议，见 antibot）。
    """
    name = "bridge"

    def __init__(self, config, task_vars, anti):
        super().__init__(config, task_vars, anti)
        self.bridge = config.get("bridge")
        self.params = config.get("bridge_params", {})
        self.task_dir = config.get("_task_dir")

    def fetch(self, req: Request) -> Response:
        raise NotImplementedError("桥取数器是一次性取整批（fetch_all），不走请求队列")

    def fetch_all(self) -> list:
        """调用桥，返回全部原始记录。"""
        import json as _json, re as _re
        bridge_path = Path(self.bridge)
        if not bridge_path.is_absolute():
            cands = []
            if self.task_dir:
                cands.append(Path(self.task_dir) / self.bridge)
                cands.append(Path(self.task_dir) / "modules" / self.bridge)
            cands.append(Path(__file__).resolve().parent.parent.parent / self.bridge)
            bridge_path = next((c for c in cands if c.exists()), cands[0])
        # 模板替换
        params = {}
        for k, v in (self.params or {}).items():
            if isinstance(v, str):
                v = _re.sub(r"\{\{(\w+)\}\}", lambda m: str(self.vars.get(m.group(1), m.group(0))), v)
            params[k] = v
        cap_dir = Path(self.anti.get("captcha_dir") or "/tmp/us_captcha")
        cap_dir.mkdir(parents=True, exist_ok=True)
        params.setdefault("captchaDir", str(cap_dir))
        cmd = [_NODE_BIN, str(bridge_path)]
        for k, v in params.items():
            if v is not None:
                cmd += [f"--{k}", str(v)]
        proc, errbuf = _spawn_bridge(cmd, {**os.environ, "NODE_PATH": _NODE_PATH})
        records = []
        bad_lines = 0
        assert proc.stdout is not None
        # R34 修复：读循环曾无 try/finally——KeyboardInterrupt / error 事件只
        # terminate 不等不杀，node 的 chromium 子进程残留锁死 profile
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except Exception:
                    bad_lines += 1
                    continue
                t = obj.get("type")
                if t == "page":
                    records.extend(obj.get("records") or [])
                elif t == "captcha" and obj.get("imageFile"):
                    from ..antibot import solve_captcha_file
                    solve_captcha_file(obj["imageFile"], self.anti,
                                       answer_file=str(obj["imageFile"]) + ".answer", seq=obj.get("seq", 0))
                elif t == "error":
                    proc.terminate()
                    raise RuntimeError(obj.get("message"))
        finally:
            # 必杀：与根文件 _run 的 finally-kill 同款——异常退出绝不残留桥进程
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        rc, err = _wait_bridge(proc, errbuf)
        if bad_lines:
            log(f"⚠️ 桥输出含 {bad_lines} 行无法解析，数据可能不完整")
        # R34 修复：rc!=0 曾无条件 raise——cleanup 阶段失败（如 browser.close 抛）
        # 会把已抓到的全部记录丢弃。有记录且无 errbuf 错误时如实保留
        if rc != 0:
            if records and not err:
                log(f"⚠️ 桥退出码 {rc}（cleanup 阶段失败），已保留 {len(records)} 条已抓记录")
                return records
            raise RuntimeError(f"桥退出码 {rc}: {err[-300:]}")
        return records


class BrowserFetcher(BaseFetcher):
    """浏览器取数器（会话池，长驻复用）。

    - 默认用长驻浏览器池（scripts/browser_pool.cjs）：一次启动、多页复用，比每次重启快数倍
    - pool=false 时回退单页桥（scripts/browser_single.cjs）
    适用：SPA / JS 渲染 / 多页面的浏览器任务（配合 v3 parser 的 CSS/LLM 解析）。
    """
    name = "browser"

    def __init__(self, config, task_vars, anti):
        super().__init__(config, task_vars, anti)
        self.scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
        self.session_dir = Path(anti.get("session_dir") or "/tmp/us_session")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        # 🍪 自动会话：按任务域名加载已存档 cookie → 注入 storageState（浏览器复用登录态/过盾）
        self.cookie_domain = anti.get("cookie_domain") or ""
        if self.cookie_domain:
            try:
                from ..cookies import load_storage_state
                import json as _json
                _ss = load_storage_state(self.cookie_domain)
                if _ss:
                    # R98 修复（P2）：storageState（登录态全文）曾 0644 落盘
                    import os as _os
                    _sp = self.session_dir / "session.json"
                    _fd = _os.open(_sp, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
                    with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
                        _f.write(_json.dumps(_ss, ensure_ascii=False))
                    _os.chmod(_sp, 0o600)  # R18b 同款：治愈升级前遗留的 0644
            except Exception as _e:
                self._notify(f"⚠️ 已存档登录态(storageState)装载/写入失败（domain={self.cookie_domain}）：{_e}，浏览器可能以未登录状态启动")
        self._pool = None
        self._pool_enabled = config.get("pool", True)
        self._req_id = 0
        self._pending: dict = {}
        self._pending_owner: dict = {}  # rid -> 发往的池实例（OCR R131：EOF 清理按池归属）
        self._lock = threading.Lock()
        self._stdin_lock = threading.Lock()  # R128：并发 stdin 写曾损坏 JSONL 协议
        self._cond = threading.Condition(self._lock)
        # 代理：单代理走池（启动时注入）；多代理走单页桥逐请求轮换
        self.proxy_pool = anti.get("_proxy_pool")
        if self.proxy_pool is None and anti.get("proxies"):
            from ..proxy import ProxyPool
            self.proxy_pool = ProxyPool(anti.get("proxies"), anti.get("proxy_mode", "round_robin"), warn=anti.get("_log_cb"))
        if self.proxy_pool is None and anti.get("proxy"):
            from ..proxy import ProxyPool
            self.proxy_pool = ProxyPool([anti["proxy"]])
        self._single_proxy_mode = bool(self.proxy_pool is not None and self.proxy_pool.size > 1)
        self._proxy = anti.get("proxy") or (self.proxy_pool.next() if self.proxy_pool else None)
        # 任务停止信号（WebUI 一键停止 → 任务目录写 .stop）
        self._task_dir = Path(str(config.get("_task_dir") or anti.get("_task_dir") or ""))

    def _stopped(self) -> bool:
        return bool(self._task_dir) and (self._task_dir / ".stop").exists()

    def _notify(self, msg: str) -> None:
        """进度回传：WebUI job.messages（engine 通过 anti._log_cb 注入），无则打印终端。"""
        cb = self.anti.get("_log_cb")
        if cb:
            try:
                cb(msg)
                return
            except Exception:
                pass
        print(msg, file=sys.stderr, flush=True)  # 必须 stderr：MCP stdio 场景 stdout 是协议流

    # ---- 会话池 ----
    def _ensure_pool(self):
        if not self._pool_enabled:
            return None
        # 双检锁：并发首抓/自愈重启时只允许一个线程拉起池进程（否则前者进程泄漏）
        with self._lock:
            # 池进程已死 → 清掉，重新拉起（自愈）
            if self._pool is not None and self._pool.poll() is not None:
                self._pool = None
            if self._pool is not None:
                return self._pool
            import subprocess, threading
            env = {**os.environ, "NODE_PATH": _NODE_PATH}
            ss = self.session_dir / "session.json"
            if ss.exists():
                env["US_STORAGE_STATE"] = str(ss)
            if self._proxy and not self._single_proxy_mode:
                env["US_PROXY"] = self._proxy
            if self._task_dir:
                env["US_STOP_FILE"] = str(self._task_dir / ".stop")
            self._pool = subprocess.Popen(
                [_NODE_BIN, str(self.scripts_dir / "browser_pool.cjs")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", env=env,
            )
            # stderr 排空线程（审查 P1）：池崩溃原因曾进 DEVNULL，用户只看到
            # "浏览器池进程已退出"六字，无法诊断
            self._pool_stderr_tail: list = []
            def _drain_err(proc=self._pool):
                try:
                    for _line in proc.stderr:
                        self._pool_stderr_tail.append(_line.strip()[:200])
                        del self._pool_stderr_tail[:-5]  # 只留最后 5 行
                except Exception:
                    pass
            threading.Thread(target=_drain_err, daemon=True).start()
            self._reader = threading.Thread(target=self._read_pool, daemon=True)
            self._reader.start()
            return self._pool

    def _read_pool(self):
        import json as _json
        if not self._pool or not self._pool.stdout:
            return
        my_pool = self._pool
        for line in my_pool.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = _json.loads(line)
            except Exception:
                continue
            try:
                with self._cond:
                    rid = obj.get("id")
                    if rid in self._pending:
                        self._pending[rid] = obj
                        self._cond.notify_all()
                    elif obj.get("error"):
                        # 审查修复（P1）：池进程主动上报的错误（id=null 曾被
                        # 直接丢弃——启动失败的唯一线索）
                        from ..core import log as _corelog
                        _corelog(f"⚠️ 浏览器池上报错误: {str(obj.get('error'))[:160]}", "WARN")
            except Exception as e:
                # reader 线程绝不能死：一行处理失败即永久 120s 超时且不自愈
                from ..core import log as _corelog
                _corelog(f"⚠️ 池输出行处理失败({type(e).__name__}): {str(e)[:60]}", "WARN")
        # EOF：池进程退出（崩溃/被杀）→ 挂起请求立即报错，下一次 fetch 自愈重启。
        # 审查修复（P2）：错误填充不再要求 self._pool 仍是本 reader 的池——
        # 池已被替换成新进程时，旧池的死同样永远等不到应答
        with self._cond:
            # OCR R131（H）：只错误化属于本（已死）池的在途请求——旧版把新池
            # 的 pending 一并填错，重启自愈期间新请求被凭空判失败
            dead = [rid for rid, obj in self._pending.items()
                    if obj is None and self._pending_owner.get(rid) is my_pool]
            if self._pool is my_pool:
                self._pool = None
            for rid in dead:
                _tail = "；".join(self._pool_stderr_tail[-2:]) if getattr(self, "_pool_stderr_tail", None) else ""
                self._pending[rid] = {"id": rid, "html": "", "url": "",
                                      "error": "浏览器池进程已退出" + (f"（{_tail}）" if _tail else "")}
                self._pending_owner.pop(rid, None)
            self._cond.notify_all()

    def fetch(self, req: Request) -> Response:
        if self._stopped():
            raise KeyboardInterrupt("任务已停止（WebUI 停止信号）")
        # 收官十五轮（core 深审 M4，实测）：交互/单页/池三条子路径都直接用 req.url，
        # 而 json_paged/offset 翻页与断点恢复用 meta.params 表达分页（HTTP 路径会
        # 合并、这里不会）→ 浏览器模式反复抓第 1 页，Request.key 含 params 也拦不住。
        # 入口统一把 config.query + meta.params 合并进 URL，三条子路径自动继承
        _p = {**(self.config.get("query") or {}), **(req.meta.get("params") or {})}
        if _p:
            import dataclasses as _dc
            req = _dc.replace(req, url=_merge_url_params(req.url, _p))
        # 人工交互场景（登录 / 整页验证码 / headless=false 弹窗）→ browser_generic.cjs
        if (self.config.get("login") or {}).get("enabled") or \
           (self.config.get("verify") or {}).get("enabled") or \
           self.config.get("capture") or \
           self.config.get("capture_all") or \
           self.config.get("headless") is False:
            # 审查十七轮自查：capture_all 拼写曾漏——auto.py 生成的
            # {"record_from":"capture_all","capture_all":true} 形态走 pool 桥
            # （pool 无捕获代码），捕获是死键（R12 M10 残留）
            resp = self._fetch_interactive(req)
        # 多代理轮换：逐请求换代理，必须走单页桥（池是固定代理）
        elif self._single_proxy_mode:
            resp = self._fetch_single(req)
        else:
            pool = self._ensure_pool()
            resp = self._fetch_pool(pool, req) if pool is not None else self._fetch_single(req)
        # Tier3 渲染后结构完整性检测（Crawl4AI 哲学，2026-09）：渲染完仍是空壳
        # = 软封锁（200 空壳是行为评分封锁的常见形态，gsxt 战训）。HTTP 直抓的
        # SPA 壳合法（另有 jsrecon/capture 处方），所以只在浏览器路线做。
        # 可用 anti_bot.empty_shell_check=false 关闭（审后修复：iframe/canvas
        # 类合法薄页会误判，给逃生门）。
        # 审查十五轮（实战反馈猫眼，用户实测复现）：capture 模式下**不抛**空壳
        # RateLimitedError——空壳恰恰是 capture 的主场景（数据在 XHR 接口不在
        # DOM），此处在桥跑完、capture_all 已落盘后抛错，调用方（fetch --capture）
        # 收到异常走降级路径、capture 参数拿不到文件。统计照记，豁免照旧可用
        # 审查十七轮自查：豁免条件补 capture_all 拼写（auto.py 生成形态），
        # 单查 capture 会漏 record_from+capture_all 形态
        if self.anti.get("empty_shell_check", True) \
                and not (self.config.get("capture")
                         or self.config.get("capture_all")):
            try:
                _bd = detect_block(int(getattr(resp, "status", 0) or 0),
                                   getattr(resp, "text", "") or "", {}, req.url, rendered=True)
            except Exception as e:
                log(f"渲染后封禁检测异常（已跳过本轮判定）: {type(e).__name__}: {e}", "DEBUG")
                _bd = None
            if _bd is not None and _bd.get("kind") == "empty_shell":
                if self.anti.get("_block_stats") is not None:
                    self.anti["_block_stats"]["empty_shell"] = \
                        self.anti["_block_stats"].get("empty_shell", 0) + 1
                # 注意：BrowserFetcher 没有 client 级 _autothrottle（审查 P0 修复：
                # 曾调用不存在的方法让 AttributeError 抢在 RateLimitedError 之前）
                from ..protocols import RateLimitedError
                raise RateLimitedError(req.url, retry_after=None, status=200,
                                       detail=f"渲染后空壳[{_bd.get('detail', '')}]")
        return resp

    def _fetch_interactive(self, req: Request) -> Response:
        """人工交互浏览器取数：登录 / 整页验证码（大众点评/美团验证中心）/ headless=false 弹窗。
        走 scripts/browser_generic.cjs（支持 login + verify + 会话持久化 + 等真人过验证）。"""
        import json as _json, tempfile
        spec = {
            "url": req.url,
            "js_pre": self.config.get("js_pre"),
            "login": self.config.get("login"),
            "verify": self.config.get("verify"),
            "fingerprint": self.config.get("fingerprint"),
            # capture 契约：布尔 true=全捕获（翻译成桥的 capture_all）；列表=声明式捕获
            "capture": (self.config.get("capture")
                        if isinstance(self.config.get("capture"), list) else None),
            # 审查十一轮（C 同款）：capture_all 拼写也要认（auto.py 生成形态）
            "capture_all": (self.config.get("capture") is True)
            or (self.config.get("capture_all") is True),
        }
        wait_sel = self.config.get("wait_selector")
        wait_to = int(self.config.get("wait_timeout") or 30000)
        for a in (self.config.get("actions") or []):
            if isinstance(a, dict) and a.get("type") == "wait" and a.get("selector"):
                wait_sel = a["selector"]
                wait_to = int(a.get("timeout") or wait_to)
        if wait_sel:
            spec["wait"] = {"selector": wait_sel, "timeout": wait_to}
        holder: dict = {}
        with tempfile.TemporaryDirectory(prefix="us_int_") as tmp:
            spec_file = Path(tmp) / "spec.json"
            out_dir = Path(tmp) / "pages"
            out_dir.mkdir()
            spec_file.write_text(_json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            ss = self.session_dir / "session.json"
            # 按任务隔离 profile：不同任务不抢同一浏览器档案（之前全局 .browser_profile
            # 被残留进程锁住 → 新任务 launchPersistentContext 直接 browser closed）
            _task_name = self._task_dir.name if self._task_dir else "default"
            _prof_root = Path(self.scripts_dir).parent / "outputs" / ".browser_profiles"
            _profile_dir = _prof_root / _task_name
            _profile_dir.mkdir(parents=True, exist_ok=True)
            # 审查修复（P1）：并发 worker 同时走交互路径会互删 SingletonLock、
            # 在同一 profile 上拉起第二个 Chromium（profile 损坏）——按 profile
            # 路径串行化
            with _INTERACTIVE_PROFILE_LOCKS.for_path(_profile_dir):
                self._fetch_interactive_locked(req, tmp, spec, spec_file, out_dir, ss,
                                               _profile_dir, holder)
            html = holder.get("html", "")
            final_url = holder.get("url", req.url)
        # 空页面不伪造 200：status=0 走引擎错误统计路径（否则空壳被当成功，问题被掩盖）
        return Response(request=req, status=200 if html else 0, body=html.encode("utf-8"),
                        text=html, json=None, url=final_url)

    def _fetch_interactive_locked(self, req, tmp, spec, spec_file, out_dir, ss,
                                  _profile_dir, holder) -> None:
        """交互取数主体（调用方已持有 profile 锁）。结果写进 holder：html/url。"""
        import json as _json
        # 启动前清锁：残留进程留下的 SingletonLock/SingletonCookie 会让启动失败
        #（持有 profile 锁时清理是安全的——同 profile 的其他 fetch 已被串行化）
        for _lk in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (_profile_dir / _lk).unlink(missing_ok=True)
            except Exception:
                pass
        cmd = [_NODE_BIN, str(self.scripts_dir / "browser_generic.cjs"),
               "--spec", str(spec_file), "--out", str(out_dir),
               # R1 复查修复（P2-6）：headless 尊重配置——capture+headless:true
               # 的无人值守组合不再强制弹窗；仅 login/verify 等真交互场景强制可见
               "--headless", "1" if self.config.get("headless") else "0",
               "--profile", str(_profile_dir),
               "--storageState", str(ss),
               "--scrollCount", str(self.config.get("scroll_count", 0)),
               "--scrollWait", str(self.config.get("scroll_wait_ms", 2000)),
               "--loginTimeout", str(self.config.get("login_timeout_ms", 600000)),
               "--debugDir", str(Path(self.scripts_dir).parent / "outputs" / ".debug")]
        # CDP 附着：调试 Chrome(9222) 在线时附着其真实会话（复用用户登录态）
        _cdp = self.config.get("cdp") or ""
        if not _cdp:
            try:
                from ..agent import debug_chrome_alive
                if debug_chrome_alive():
                    _cdp = "http://127.0.0.1:9222"
                    (self.anti.get("_log_cb") or log)(
                        "🖥️ 检测到调试 Chrome 在线，附着真实浏览器（复用登录态）…")
            except Exception:
                pass
        if _cdp:
            cmd += ["--cdp", _cdp]
        if self._task_dir:
            cmd += ["--stopFile", str(self._task_dir / ".stop")]
        env = {**os.environ, "NODE_PATH": _NODE_PATH}
        proc, errbuf = _spawn_bridge(cmd, env)
        html = ""
        final_url = req.url
        finished = False
        bad_lines = 0
        capture_saved = False
        # R34 修复：读循环曾无 try/finally——stopped/error 路径只 terminate
        # 不等不杀，node 的 chromium 子进程残留锁住 profile，引擎重试删除
        # SingletonLock 后再启动第二个 chromium 打到活 profile 上（写坏会话）
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except Exception:
                    bad_lines += 1
                    continue
                t = obj.get("type")
                msg = obj.get("message") or ""
                if t == "done":
                    finished = True
                    self._notify(f"✅ done pages={obj.get('pages')} html_len={len(html)}")
                    break  # 收到 done 即结束（不再等 EOF——桥的 browser.close 可能挂住）
                if t in ("login", "verify_required", "login_required"):
                    self._notify(f"⚠️ {msg}")
                elif t in ("login_ok", "verify_ok"):
                    self._notify(f"✅ 会话已保存: {obj.get('storageState', '')}")
                elif t == "verify_passed":
                    self._notify(f"✅ {msg}")
                elif t == "wait_timeout":
                    # 审查修复（P2）：等待选择器超时曾静默——骨架页当 200 返回
                    self._notify(f"⚠️ 等待渲染超时（selector={obj.get('selector', '')}）"
                                 "——页面可能未渲染完成，结果可能不完整")
                elif t == "gate_info":
                    self._notify(f"🔎 门卫诊断: url={obj.get('url','')} text_len={obj.get('text_len')} text={str(obj.get('text',''))[:180]}")
                elif t == "capture_file":
                    # 审查修复（P1）：接口捕获数据曾被连同临时目录一起销毁——
                    # 现在落盘到任务目录（record_from 的消费方才能读到）
                    _cf = obj.get("file")
                    if _cf and Path(_cf).exists() and self._task_dir:
                        try:
                            _dst = Path(self._task_dir) / "capture_all.json"
                            _dst.write_bytes(Path(_cf).read_bytes())
                            capture_saved = True
                            self._notify(f"📡 接口捕获已保存: {_dst}")
                        except Exception as e:
                            self._notify(f"⚠️ 接口捕获保存失败: {e}")
                elif t == "page":
                    f = obj.get("file")
                    _exists = bool(f) and Path(f).exists()
                    _size = Path(f).stat().st_size if _exists else 0
                    self._notify(f"📄 页面 {obj.get('page')} exists={_exists} size={_size}")
                    if _exists:
                        html = Path(f).read_text(encoding="utf-8", errors="replace")
                    # OCR R131（M）：桥上报的最终 URL 曾被丢弃——重定向/跳转后
                    # Response.url 仍是请求 URL，相对链接补全全错
                    if obj.get("url"):
                        final_url = str(obj["url"])
                elif t == "stopped":
                    proc.terminate()
                    raise KeyboardInterrupt("任务已停止（浏览器桥收到停止信号）")
                elif t == "error":
                    proc.terminate()
                    raise RuntimeError(msg or "浏览器交互桥错误")
        except BaseException:
            # R34 修复：循环中途 raise（stopped/error/KeyboardInterrupt）时必杀——
            # 只 terminate 不等不杀会残留 chromium 子进程锁死 profile
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            raise
        try:
            proc.wait(timeout=30 if finished else 600)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
        if bad_lines:
            log(f"⚠️ 桥输出含 {bad_lines} 行无法解析，数据可能不完整")
        try:
            if not html:
                files = sorted(out_dir.glob("*.html"))
                if files:
                    html = files[0].read_text(encoding="utf-8", errors="replace")
            # 诊断保留：最近一次交互页面的 HTML（排查"抓了但0条"）
            try:
                debug_html = Path(self.scripts_dir).parent / "outputs" / ".debug" / "last_interactive_page.html"
                debug_html.parent.mkdir(parents=True, exist_ok=True)
                debug_html.write_text(html or "", encoding="utf-8")
            except Exception:
                pass
        finally:
            # 必杀：子进程绝不允许残留（卡验证的进程会锁住 profile，害死下一个任务）
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        if capture_saved and not self.config.get("record_from"):
            self._notify("⚠️ 捕获到接口数据但未配置 record_from——数据在 capture_all.json，"
                         "不会自动变成记录。处置: `cli capture2config` 生成可重放配置")
        holder["html"] = html
        holder["url"] = final_url

    def _fetch_pool(self, pool, req: Request) -> Response:
        import json as _json
        if pool.poll() is not None:
            pool = self._ensure_pool()
            if pool is None:
                return self._fetch_single(req)
        with self._lock:
            self._req_id += 1
            rid = self._req_id
            self._pending[rid] = None
            # OCR R131（H）：记录该请求发往哪个池实例——旧池死掉时 EOF 清理只
            # 错误化属于自己的条目，新池的在途请求不再被旧 reader 误杀
            self._pending_owner[rid] = pool
            payload = {"id": rid, "url": req.url,
                       "scroll": self.config.get("scroll_count", 0),
                       "scrollWait": self.config.get("scroll_wait_ms", 1500),
                       "actions": self.config.get("actions", []),
                       "stealth": bool(self.config.get("stealth", False)),
                       "remove_overlays": bool(self.config.get("remove_overlays", False))}
            if self.config.get("js_pre"):
                payload["js"] = self.config["js_pre"]
            if self.config.get("wait_selector"):
                payload["wait"] = self.config["wait_selector"]
            ss = self.session_dir / "session.json"
            if ss.exists():
                payload["storageState"] = str(ss)
            # 写入移出临界区：stdin 管道写满而池进程停读时，持锁阻塞会挂死所有 worker
            # （登记已在锁内完成，reader 线程按 rid 回填结果，写晚于登记是安全的）
        try:
            # R128 修复（OCR CRITICAL）：多 worker 并发写同一 stdin 管道曾损坏
            # JSONL 协议（两行 JSON 交错）——加专用写锁
            with self._stdin_lock:
                pool.stdin.write(_json.dumps(payload, ensure_ascii=False) + "\n")
                pool.stdin.flush()
        except Exception:
            # 池进程已死等写入失败：撤销登记，防 _pending 无等待者条目慢性泄漏
            self._pending.pop(rid, None)
            self._pending_owner.pop(rid, None)
            raise
        # 等待结果（带超时）
        deadline = time.time() + 120
        with self._cond:
            while self._pending.get(rid) is None:
                if self._stopped():
                    # 注意：此处已持有 self._lock（Condition 包装同一把锁，非重入），
                    # 严禁再 with self._lock 二次获取（会死锁），直接操作即可
                    self._pending.pop(rid, None)
                    self._pending_owner.pop(rid, None)
                    # OCR R131（H）：只终止自己请求发往的池实例——terminate 共享的
                    # self._pool 可能误杀他线程刚换好的新池
                    try:
                        pool.terminate()
                    except Exception:
                        pass
                    raise KeyboardInterrupt("任务已停止（WebUI 停止信号，浏览器池已终止）")
                if time.time() > deadline:
                    self._pending.pop(rid, None)
                    self._pending_owner.pop(rid, None)
                    raise TimeoutError(f"浏览器池渲染超时: {req.url}")
                self._cond.wait(1.0)
            obj = self._pending.pop(rid)
            self._pending_owner.pop(rid, None)
        if "error" in obj and obj["error"]:
            raise RuntimeError(obj["error"])
        html = obj.get("html", "")
        # 🍪 自动存档会话 cookie（含 cf_clearance/登录态），下次同域名任务自动复用
        _cks = obj.get("cookies")
        if _cks and (self.cookie_domain or req.url):
            _d = ""
            try:
                from ..cookies import save_cookies
                # 只回存任务目标域的会话：跟链到第三方域的 cookie 不进本地存档
                _d = self.cookie_domain or _norm_domain((req.url or "").split("//")[-1].split("/")[0])
                save_cookies(_d, _cks)
            except Exception as _e:
                self._notify(f"⚠️ 会话 cookie 回存失败（domain={_d or '未知'}）：{_e}，下次同域任务可能需要重新登录")
        # 空页面不伪造 200：status=0 走引擎错误统计路径（否则空壳被当成功，问题被掩盖）
        return Response(request=req, status=200 if html else 0, body=html.encode("utf-8"),
                        text=html, json=None, url=obj.get("url") or req.url)

    def _fetch_single(self, req: Request) -> Response:
        # 单页桥（pool=false 后备）
        import json as _json, subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tf:
            out_file = tf.name
        try:
            cmd = [_NODE_BIN, str(self.scripts_dir / "browser_single.cjs"), "--url", req.url, "--out", out_file,
                   "--scrollCount", str(self.config.get("scroll_count", 0)),
                   "--storageState", str(self.session_dir / "session.json")]
            if self.config.get("js_pre"):
                cmd += ["--js", self.config["js_pre"]]
            if self.config.get("wait_selector"):
                cmd += ["--wait", self.config["wait_selector"]]
            if self.config.get("actions"):
                cmd += ["--actions", _json.dumps(self.config["actions"], ensure_ascii=False)]
            if self.config.get("stealth"):
                cmd += ["--stealth", "1"]
            if self.config.get("remove_overlays"):
                cmd += ["--removeOverlays", "1"]
            if self._task_dir:
                cmd += ["--stopFile", str(self._task_dir / ".stop")]
            proxy = None
            if self._single_proxy_mode and self.proxy_pool is not None:
                proxy = self.proxy_pool.next()
            elif self._proxy and not self._single_proxy_mode:
                proxy = self._proxy
            if proxy:
                cmd += ["--proxy", proxy]
            # OCR R131 终审（H）：subprocess.run 超时曾留孤儿 Chrome——
            # TimeoutExpired 时用 Popen 杀进程树再重抛
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                      env={**os.environ, "NODE_PATH": _NODE_PATH}, timeout=180)
            except subprocess.TimeoutExpired:
                # run() 内部已 kill 直接子进程，但 node 的 chromium 孙进程不在其管辖内。
                # 审查二轮（H）：pkill 按脚本名曾全杀——并行任务的 browser_single.cjs
                # 一并陪葬。改用本任务唯一的 --out 输出路径做特征（mkdtemp 唯一）
                _out_val = next((cmd[i + 1] for i, a in enumerate(cmd)
                                 if a == "--out" and i + 1 < len(cmd)), "")
                try:
                    if _out_val:
                        subprocess.run(["pkill", "-f", _out_val], capture_output=True, timeout=5)
                except Exception:
                    pass
                raise TimeoutError(f"浏览器单页桥超时 180s（孤儿已清理）: {req.url}")
            try:
                obj = _json.loads(proc.stdout.strip().splitlines()[-1])
            except Exception:
                raise RuntimeError(f"浏览器桥输出异常: {proc.stderr[-300:]}")
            if obj.get("type") == "error":
                raise RuntimeError(obj.get("message"))
            if obj.get("type") == "stopped" or self._stopped():
                raise KeyboardInterrupt("任务已停止（WebUI 停止信号）")
            html = Path(out_file).read_text(encoding="utf-8", errors="replace")
        finally:
            # 审查修复（HIGH）：任何路径（成功/桥错误/停止信号/超时）都清理临时
            # HTML——此前每次单页抓取都在临时目录遗留一个孤儿 .html 文件
            try:
                os.unlink(out_file)
            except OSError:
                pass
        # 空页面不伪造 200：status=0 走引擎错误统计路径（否则空壳被当成功，问题被掩盖）
        return Response(request=req, status=200 if html else 0, body=html.encode("utf-8"),
                        text=html, json=None, url=obj.get("url") or req.url)

    def close(self) -> None:
        # 无论是否 stopped：自己的 pool 一律 terminate + kill，绝不留孤儿进程锁资源
        if self._pool is not None:
            try:
                if self._pool.stdin:
                    # OCR R131：close 与在途 worker 的请求写共用同一 stdin 管道，
                    # 必须同走 _stdin_lock——并发时 close 帧会插进请求 JSON 中间
                    with self._stdin_lock:
                        self._pool.stdin.write('{"type":"close"}\n')
                        self._pool.stdin.flush()
                try:
                    self._pool.wait(timeout=3)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                if self._pool.poll() is None:
                    self._pool.terminate()
            except Exception:
                pass
            try:
                if self._pool.poll() is None:
                    self._pool.kill()
            except Exception:
                pass
            self._pool = None


import time  # noqa: E402
import os as _os_mod  # noqa: E402
from ..runtime import resolve_node, resolve_node_path as _resolve_node_path

# 复审回退（pyflakes 抓获）：曾改 PEP 562 惰性 __getattr__——但模块内部函数的
# 裸名查找不走 __getattr__（它只拦截外部属性访问），8 处调用点运行时 NameError。
# 恢复急切赋值；resolve_node/path 均有不抛兜底（"node"/""），导入安全
_NODE_BIN = _os_mod.environ.get("UNIVERSAL_SCRAPER_NODE", resolve_node())
_NODE_PATH = _os_mod.environ.get("UNIVERSAL_SCRAPER_NODE_PATH", _resolve_node_path())

