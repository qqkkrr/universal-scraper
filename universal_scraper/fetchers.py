#!/usr/bin/env python3
"""取数器：把"怎么拿到数据"抽象成可配置的策略。

- HttpFetcher        : 普通 HTTP（JSON API / HTML 页面），带分页
- BrowserScriptFetcher: 调用 Node+Playwright 桥脚本（过 WAF / 驱动 Vue 等复杂页面）
- BrowserFetcher     : 通用浏览器取数（CSS 行选择器 + 翻页点击），由 config 驱动
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .core import HttpClient, log, die, smart_decode
from .antibot import solve_captcha_file, detect_block
from .session import SessionPool
from .selectors import jpath, css_text, xpath_text

NODE = os.environ.get(
    "UNIVERSAL_SCRAPER_NODE",
    "/Users/kairanqin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node",
)
NODE_PATH = os.environ.get(
    "UNIVERSAL_SCRAPER_NODE_PATH",
    "/Users/kairanqin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules",
)


def _resolve_template(value: Any, vars: Dict[str, str]) -> Any:
    """把 config 里的 {{var}} 模板替换成任务变量。"""
    if isinstance(value, str):
        def _repl(m):
            return str(vars.get(m.group(1), m.group(0)))
        return re.sub(r"\{\{(\w+)\}\}", _repl, value)
    if isinstance(value, dict):
        return {k: _resolve_template(v, vars) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_template(v, vars) for v in value]
    return value


class BaseFetcher:
    def fetch_list(self, pagination: Dict[str, Any]) -> List[Dict[str, Any]]:
        raise NotImplementedError


class HttpFetcher(BaseFetcher):
    """HTTP 取数器：支持 JSON API（records_path/total_path）与 HTML 列表（row_css + 字段提取）。"""

    def __init__(self, source: Dict[str, Any], anti: Dict[str, Any], vars: Dict[str, str], cache_dir: Optional[Path] = None):
        self.source = _resolve_template(source, vars)
        self.anti = anti
        from .core import make_http_client
        self.http = make_http_client(anti)
        if cache_dir:
            self.http.cache_dir = cache_dir
        # 代理池轮换（anti._proxy_pool 由 engine 注入，或配置里 proxies 列表）
        self.proxy_pool = anti.get("_proxy_pool")
        if self.proxy_pool is None and anti.get("proxies"):
            from .proxy import ProxyPool
            self.proxy_pool = ProxyPool(anti.get("proxies"), anti.get("proxy_mode", "round_robin"))
        self._last_proxy = None
        # HTTP 会话池（Crawlee SessionPool 思路）：按域名 Cookie/UA/代理，封禁自动换会话
        sp_proxies = list(anti.get("proxies") or [])
        if not sp_proxies and anti.get("proxies_file"):
            try:
                pf = Path(anti["proxies_file"]).expanduser()
                if pf.exists():
                    sp_proxies = [ln.strip() for ln in pf.read_text(encoding="utf-8").splitlines() if ln.strip() and ":" in ln]
                    log(f"🔄 已加载代理文件 {pf}: {len(sp_proxies)} 个")
            except Exception as e:
                log(f"⚠️ 代理文件加载失败: {e}")
        if not sp_proxies and anti.get("proxy"):
            sp_proxies = [anti["proxy"]]
        self.sessions = SessionPool(proxies=sp_proxies or None,
                                    max_per_domain=int(anti.get("session_pool_max", 3)),
                                    rotate_on_errors=int(anti.get("session_rotate_on_errors", 2)),
                                    proxy_mode=anti.get("proxy_mode", "round_robin"))
        if self.proxy_pool is not None:
            # 会话池的代理从 ProxyPool 动态获取，失败同步反馈给 ProxyPool
            self.sessions._next_proxy = self.proxy_pool.next
        self._cur_session = None

    def _pick_proxy(self):
        if self.proxy_pool is not None:
            p = self.proxy_pool.next()
            self._last_proxy = p
            return p
        return self.anti.get("proxy") or None

    def _report_proxy(self, ok: bool):
        if self.proxy_pool is not None and self._last_proxy:
            if ok:
                self.proxy_pool.mark_ok(self._last_proxy)
            else:
                self.proxy_pool.mark_fail(self._last_proxy)
            self._last_proxy = None

    def _request(self, page_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        s = self.source
        url = s["url"]
        query = dict(s.get("query", {}))
        if page_params:
            query.update(page_params)
        method = s.get("method", "GET")
        # 会话池：取当前域会话（含 Cookie/UA/代理），封禁自动轮换
        sess = self.sessions.acquire(url)
        self._cur_session = sess
        hdrs = dict(s.get("headers") or {})
        hdrs.setdefault("User-Agent", sess.ua)
        if sess.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in sess.cookies.items())
        kw = dict(params=query or None, headers=hdrs, proxy=sess.proxy)
        try:
            if method.upper() == "POST":
                res = self.http.post(url, data=s.get("body"), json_data=s.get("json_body"), **kw)
            else:
                res = self.http.get(url, **kw)
        except Exception:
            self.sessions.report(sess, ok=False, blocked=True)
            self._report_proxy(False)
            raise
        # 封禁识别（Crawlee block-detection 思路）：200 但被风控的页面也会被识破
        ok = bool(res.get("ok"))
        bd = detect_block(res.get("status", 0), res.get("text", ""), res.get("headers"), url)
        blocked = bd["kind"] != "none"
        self.sessions.report(sess, ok=ok, blocked=blocked)
        self._report_proxy(ok and not blocked)
        # 会话 Cookie 续存（Set-Cookie 由 HttpClient 收集后再这里同步）
        if ok and res.get("headers"):
            try:
                setck = res["headers"].get("set-cookie", "") or res["headers"].get("Set-Cookie", "")
                for part in setck.split(","):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        sess.cookies[k.strip()] = v.split(";")[0].strip()
            except Exception:
                pass
        if blocked:
            log(f"  检测到反爬拦截[{bd['kind']}] {bd['detail']}（{url[:100]}）", "WARN")
            if self.anti.get("_block_stats") is not None:
                self.anti["_block_stats"][bd["kind"]] = self.anti["_block_stats"].get(bd["kind"], 0) + 1
            if bd["kind"] in ("cloudflare", "verify", "captcha", "rate_limit", "anti_bot", "429", "403"):
                from .protocols import RateLimitedError
                raise RateLimitedError(url, retry_after=None, detail=f"反爬拦截[{bd['kind']}] {bd['detail']}")
        return res

    def fetch_list(self, pagination: Dict[str, Any]) -> List[Dict[str, Any]]:
        s = self.source
        stype = s.get("type", "http_json")
        strat = pagination.get("strategy", "none")
        # sitemap 种子：http_html 依次抓取每个种子页
        seeds = s.get("sitemap_urls") or []
        if stype == "http_html" and seeds:
            records: List[Dict[str, Any]] = []
            for seed in seeds:
                self.source["url"] = seed
                records.extend(self._fetch_single_page(pagination, strat))
            return records
        return self._fetch_single_page(pagination, strat)

    def _fetch_single_page(self, pagination: Dict[str, Any], strat: str) -> List[Dict[str, Any]]:
        s = self.source
        stype = s.get("type", "http_json")
        max_pages = int(pagination.get("max_pages", 100))
        page = int(pagination.get("start", 1))
        limit = pagination.get("limit", 20)
        records: List[Dict[str, Any]] = []
        total = None
        while page <= max_pages:
            params: Dict[str, Any] = {}
            if strat == "page_param":
                params[pagination["page_param"]] = page
            elif strat == "offset":
                params[pagination.get("offset_param", "offset")] = (page - 1) * limit
                if pagination.get("limit_param"):
                    params[pagination["limit_param"]] = limit
            resp = self._request(params)
            if not resp.get("ok"):
                log(f"  请求失败: {resp.get('text','')[:200]}", "ERROR")
                break
            body = resp.get("body", b"")
            text = smart_decode(body, resp.get("headers") or {})

            if stype == "http_json":
                obj = resp.get("json")
                if obj is None:
                    log("  返回不是 JSON，停止", "ERROR")
                    break
                rp = pagination.get("records_path")
                if rp:
                    recs = jpath(obj, rp, []) or []
                elif isinstance(obj, list):
                    recs = obj
                else:
                    # 自动识别常见记录键（易用性：strategy=none 时无需写 records_path）
                    recs = []
                    for _k in ("records", "items", "list", "results", "data"):
                        _v = obj.get(_k) if isinstance(obj, dict) else None
                        if isinstance(_v, list):
                            recs = _v
                            break
                total = jpath(obj, pagination.get("total_path", "data.total"), total)
                records.extend(recs if isinstance(recs, list) else [])
                log(f"  page {page}: +{len(recs) if isinstance(recs, list) else 0}（累计 {len(records)}）")
                # 终止条件
                if strat == "none":
                    break
                if total is not None:
                    fetched = page * limit if strat == "offset" else (page) * (len(recs) or 1)
                    if not recs or len(records) >= int(total):
                        break
                elif not recs:
                    break
                page += 1
            else:  # http_html
                rows = self._extract_html_rows(text)
                records.extend(rows)
                log(f"  page {page}: +{len(rows)}（累计 {len(records)}）")
                nxt = pagination.get("next_selector") or pagination.get("next_xpath")
                nxt_url = self._next_url(text, nxt)
                if not rows or not nxt_url:
                    break
                from urllib.parse import urljoin
                self.source["url"] = urljoin(self.source["url"], nxt_url)  # 相对下一页补全
                page += 1
        return records

    def _extract_html_rows(self, html: str) -> List[Dict[str, Any]]:
        s = self.source
        row_css = s.get("row_css") or s.get("row_xpath")
        fields = s.get("fields", {})
        if s.get("row_xpath"):
            from .selectors import xpath_elements
            rows = xpath_elements(html, s["row_xpath"])
        else:
            from .selectors import css_elements
            rows = css_elements(html, row_css or "tr")
        out = []
        for el in rows:
            el_html = el if isinstance(el, str) else _lxml_html_tostring(el)
            row = {}
            for name, fspec in fields.items():
                if isinstance(fspec, str):
                    row[name] = el_html  # 简单模式：整行文本
                    continue
                if fspec.get("attr"):
                    from .selectors import css_attr
                    row[name] = css_attr(el_html, fspec.get("css") or "", fspec["attr"], fspec.get("limit", 0))
                elif fspec.get("xpath"):
                    row[name] = xpath_text(el_html, fspec["xpath"], fspec.get("limit", 0))
                elif fspec.get("css"):
                    row[name] = css_text(el_html, fspec["css"], fspec.get("limit", 0))
                else:
                    row[name] = el_html
            out.append(row)
        return out

    def _next_url(self, html: str, sel: Optional[str]) -> Optional[str]:
        if not sel:
            return None
        url = None
        if sel.startswith("//") or sel.startswith("xpath:"):
            url = xpath_text(html, sel.lstrip("xpath:"), 0, " ")
        else:
            from .selectors import css_attr
            url = css_attr(html, sel, "href", 0)
        return url or None


class BrowserScriptFetcher(BaseFetcher):
    """浏览器桥取数：config 里 source.bridge 指向一个 Node 脚本，按 JSONL 协议输出记录。"""

    def __init__(self, source: Dict[str, Any], anti: Dict[str, Any], vars: Dict[str, str], base_dir: Path):
        self.source = _resolve_template(source, vars)
        self.base_dir = base_dir
        self.anti = anti

    def fetch_list(self, pagination: Dict[str, Any]) -> List[Dict[str, Any]]:
        bridge = self.base_dir / self.source["bridge"] if not Path(self.source["bridge"]).is_absolute() \
            else Path(self.source["bridge"])
        params = dict(self.source.get("bridge_params", {}))
        params.update(pagination.get("extra_params", {}))
        records: List[Dict[str, Any]] = []
        meta: Dict[str, Any] = {}
        for obj in self._run(bridge, params):
            t = obj.get("type")
            if t == "meta":
                meta = obj
                log(f"  命中 {meta.get('total')} 条 / {meta.get('pages')} 页")
            elif t == "page":
                recs = obj.get("records") or []
                records.extend(recs)
                log(f"  第 {obj.get('page')} 页: +{len(recs)}（累计 {len(records)}）")
            elif t == "captcha":
                die(f"触发验证码: {obj.get('message')}（图片: {obj.get('imageFile')}）")
            elif t == "error":
                die(f"桥错误: {obj.get('message')}")
        return records

    def _run(self, bridge: Path, params: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        # 验证码目录：桥把图存这里，我们解完把答案写 <图>.answer
        cap_dir = Path(self.anti.get("captcha_dir") or "/tmp/universal_scraper_captcha")
        cap_dir.mkdir(parents=True, exist_ok=True)
        params.setdefault("captchaDir", str(cap_dir))
        cmd = [NODE, str(bridge)]
        for k, v in params.items():
            if v is not None:
                cmd += [f"--{k}", str(v)]
        env = dict(os.environ)
        env["NODE_PATH"] = NODE_PATH
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", env=env)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "captcha" and obj.get("imageFile"):
                # 自动求解并写答案文件（ddddocr/2captcha/human）
                res = solve_captcha_file(
                    obj["imageFile"],
                    self.anti,
                    answer_file=str(obj["imageFile"]) + ".answer",
                    seq=obj.get("seq", 0),
                )
                if res.get("error"):
                    log(f"  验证码求解失败({res.get('strategy')}): {res.get('error')}", "WARN")
            yield obj
        rc = proc.wait(timeout=1800)
        err = proc.stderr.read() if proc.stderr else ""
        if rc != 0 and not err:
            die(f"浏览器桥退出码 {rc}")


class BrowserFetcher(BaseFetcher):
    """通用浏览器取数：配置驱动导航（翻页/等待/滑块/验证码），页面 HTML 交给 lxml 提取。

    source 额外字段：
      js_pre, wait{selector,timeout}, pagination{type: click|js|none, selector, js, wait_ms},
      row_css / row_xpath, fields{name:{css|xpath, attr, limit}}, captcha{...}, slider{...}
    """

    def __init__(self, source: Dict[str, Any], anti: Dict[str, Any], vars: Dict[str, str], base_dir: Path):
        self.source = _resolve_template(source, vars)
        self.anti = anti
        self.base_dir = base_dir
        self.bridge = Path(__file__).resolve().parent.parent / "scripts/browser_generic.cjs"

    def fetch_list(self, pagination: Dict[str, Any]) -> List[Dict[str, Any]]:
        import tempfile
        from .selectors import xpath_elements, css_elements, xpath_text, css_text, css_attr

        spec = {
            "url": self.source["url"],
            "js_pre": self.source.get("js_pre"),
            "wait": self.source.get("wait"),
            "pagination": self.source.get("pagination", {"type": "none"}),
            "captcha": self.source.get("captcha"),
            "slider": self.source.get("slider"),
            "capture": self.source.get("capture"),
            "login": self.source.get("login"),
            "verify": self.source.get("verify"),
        }
        max_pages = int(pagination.get("max_pages", 100))
        settle = int(pagination.get("settle_ms", 1500))
        with tempfile.TemporaryDirectory(prefix="us_browser_") as tmp:
            spec_file = Path(tmp) / "spec.json"
            out_dir = Path(tmp) / "pages"
            spec_file.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            cap_dir = Path(self.anti.get("captcha_dir") or "/tmp/universal_scraper_captcha")
            cap_dir.mkdir(parents=True, exist_ok=True)
            # 会话持久化：登录态存储到 outputs/.session/<name>.json
            session_dir = Path(self.anti.get("session_dir") or "/tmp/universal_scraper_session")
            session_dir.mkdir(parents=True, exist_ok=True)
            storage_state = str(session_dir / f"{self.anti.get('session_name', 'session')}.json")

            cmd = [NODE, str(self.bridge), "--spec", str(spec_file), "--out", str(out_dir),
                   "--maxPages", str(max_pages), "--settle", str(settle),
                   "--captchaDir", str(cap_dir),
                   "--storageState", storage_state,
                   "--scrollCount", str(self.source.get("scroll_count", 0)),
                   "--scrollWait", str(self.source.get("scroll_wait_ms", 2000)),
                   "--loginTimeout", str(self.source.get("login_timeout_ms", 600000)),
                   "--headless", "0" if self.source.get("headless") is False else "1"]
            if self.source.get("cdp"):
                cmd += ["--cdp", str(self.source["cdp"])]
            env = dict(os.environ); env["NODE_PATH"] = NODE_PATH
            pp = self.anti.get("_proxy_pool")
            if pp is not None and pp.size:
                cmd += ["--proxy", pp.next() or ""]
            elif self.anti.get("proxy"):
                cmd += ["--proxy", self.anti["proxy"]]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", env=env)
            assert proc.stdout is not None
            records: List[Dict[str, Any]] = []
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                if t == "captcha" and obj.get("imageFile"):
                    solve_captcha_file(obj["imageFile"], self.anti,
                                       answer_file=str(obj["imageFile"]) + ".answer",
                                       seq=obj.get("seq", 0))
                elif t == "page":
                    html = Path(obj["file"]).read_text(encoding="utf-8", errors="replace")
                    rows = self._extract(html)
                    records.extend(rows)
                    log(f"  page {obj.get('page')}: +{len(rows)}（累计 {len(records)}）")
                elif t == "capture_file":
                    log(f"  捕获接口 {obj.get('name')}: {obj.get('count')} 个响应 -> {obj.get('file')}")
                elif t == "login":
                    log(f"[登录] {obj.get('message')}（请在浏览器窗口完成登录，最多等 {self.source.get('login_timeout_ms',600000)//1000}s）", "WARN")
                elif t == "login_ok":
                    log(f"[登录成功] 会话已保存: {obj.get('storageState')}", "WARN")
                elif t == "error":
                    proc.terminate(); die(f"浏览器错误: {obj.get('message')}")
            rc = proc.wait(timeout=1800)
            err = proc.stderr.read() if proc.stderr else ""
            if rc != 0:
                die(f"浏览器桥退出码 {rc}: {err[-400:]}")
            # 记录来源 = 网络捕获（SPA 签名接口，如小红书评论）
            if self.source.get("record_from") == "capture":
                records = self._records_from_capture(out_dir)
            elif self.source.get("record_from") == "capture_all":
                records = self._records_from_capture_all(out_dir)
            # 登录态自动导出 Cookie 串（浏览器登录一次 → HTTP Cookie 直抓复用）
            try:
                if Path(storage_state).exists():
                    import json as _json
                    _st = _json.loads(Path(storage_state).read_text(encoding="utf-8"))
                    _cs = _st.get("cookies", []) or []
                    _pairs = [f"{c.get('name','')}={c.get('value','')}" for c in _cs if c.get("name")]
                    if _pairs:
                        _ct = Path(storage_state).with_suffix(".cookie.txt")
                        _ct.write_text("; ".join(_pairs), encoding="utf-8")
                        log(f"🍪 登录态已导出 Cookie 串: {_ct}（{len(_pairs)} 个，可用 --cookie 直抓复用）")
            except Exception:
                pass
            return records

    def _extract(self, html: str) -> List[Dict[str, Any]]:
        from .selectors import xpath_elements, css_elements, xpath_text, css_text, css_attr
        s = self.source
        if s.get("row_xpath"):
            els = xpath_elements(html, s["row_xpath"])
        else:
            els = css_elements(html, s.get("row_css") or "body")
        out = []
        for el in els:
            el_html = el if isinstance(el, str) else (_lxml_html_tostring(el))
            row = {}
            for name, fspec in (s.get("fields", {}) or {}).items():
                if isinstance(fspec, str):
                    row[name] = css_text(el_html, fspec, 0)
                    continue
                if fspec.get("xpath"):
                    row[name] = xpath_text(el_html, fspec["xpath"], fspec.get("limit", 0))
                elif fspec.get("css"):
                    if fspec.get("attr"):
                        row[name] = css_attr(el_html, fspec["css"], fspec["attr"], fspec.get("limit", 0))
                    else:
                        row[name] = css_text(el_html, fspec["css"], fspec.get("limit", 0))
                else:
                    row[name] = css_text(el_html, ".", fspec.get("limit", 0))
            out.append(row)
        return out

    def _records_from_capture_all(self, out_dir: Path) -> List[Dict[str, Any]]:
        """从 capture_all.json（浏览器自动捕获的所有 JSON 响应）生成记录。
        每条记录 = {_api_url, data}，交给 LLM/解析器事后挑字段。"""
        f = out_dir / "capture_all.json"
        if not f.exists():
            return []
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return []
        recs = []
        for item in data:
            if not isinstance(item, dict):
                continue
            recs.append({"_api_url": item.get("url", ""), "data": item.get("json")})
        # 去重（同一接口多次响应）
        seen = set()
        out = []
        for r in recs:
            k = str(r.get("_api_url", "")) + ":" + json.dumps(r.get("data"), ensure_ascii=False)[:200]
            if k in seen:
                continue
            seen.add(k)
            out.append(r)
        return out


    def _records_from_capture(self, out_dir: Path) -> List[Dict[str, Any]]:
        """从捕获的接口 JSON 提取记录。source.capture: [{name, url_pattern, records_path, fields}]"""
        from .selectors import jpath
        recs: List[Dict[str, Any]] = []
        for cap in self.source.get("capture", []):
            f = out_dir / f"{cap.get('name')}.json"
            if not f.exists():
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
            rp = cap.get("records_path", "")
            for item in data:
                obj = item.get("json") if isinstance(item, dict) and "json" in item else item
                rows = jpath(obj, rp) if rp else obj
                if not isinstance(rows, list):
                    rows = [rows]
                for row in rows:
                    if isinstance(row, dict):
                        recs.append(row)
        log(f"捕获记录: {len(recs)} 条")
        return recs



def _lxml_html_tostring(el) -> str:
    try:
        from lxml import html as _h
        return _h.tostring(el, encoding="unicode")
    except Exception:
        return str(el)
