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
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .core import log, die, smart_decode, update_cookie_jar, jar_cookie_header
from .antibot import solve_captcha_file, detect_block
from .session import SessionPool
from .selectors import jpath, css_text, xpath_text

from .runtime import resolve_node, resolve_node_path
NODE = os.environ.get("UNIVERSAL_SCRAPER_NODE", resolve_node())
NODE_PATH = os.environ.get("UNIVERSAL_SCRAPER_NODE_PATH", resolve_node_path())


def _dump_debug_page(source_url: str, text: str, page: int) -> None:
    """提取 0 条时强制落盘现场——独立 config 运行没有 _task_dir，也要有据可查。"""
    try:
        import time as _t
        from urllib.parse import urlparse as _up
        _dir = Path(os.environ.get("UNIVERSAL_SCRAPER_DEBUG_DIR")
                    or "/tmp/universal_scraper_debug")
        _dir.mkdir(parents=True, exist_ok=True)
        _host = (_up(source_url).netloc or "page").replace(":", "_")
        _p = _dir / f"{_host}_p{page}_{int(_t.time())}.html"
        _p.write_text(text, encoding="utf-8")
        log(f"  ⚠️ 本页提取 0 条——现场已存 {_p}（打开核对选择器/内嵌JSON变量名/风控页）")
    except Exception:
        pass


def _spawn_bridge(cmd: List[str], env: Optional[Dict[str, str]] = None):
    """启动浏览器桥子进程 + 后台排空 stderr（防止管道写满死锁）。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8",
                            env=env if env is not None else dict(os.environ))
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


def _wait_bridge(proc: subprocess.Popen, errbuf: List[str], timeout: int = 1800):
    """等待桥退出；超时强杀（防僵尸）。返回 (rc, stderr_text)。"""
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


def _apply_page_tokens(text: str, page: int, offset: int) -> str:
    """翻页 token 替换：{{page}}/{{offset}}（文档写法）与 {page}/{offset}（简写）。
    必须先替换双括号——先替换单括号会把 {{page}} 半替换成 '{2}'（智联战例）。"""
    out = text.replace("{{offset}}", str(offset)).replace("{{page}}", str(page))
    return out.replace("{offset}", str(offset)).replace("{page}", str(page))


def _apply_page_tokens_value(v: Any, page: int, offset: int) -> Any:
    if isinstance(v, str):
        return _apply_page_tokens(v, page, offset)
    if isinstance(v, (dict, list)):
        try:
            return json.loads(_apply_page_tokens(json.dumps(v, ensure_ascii=False), page, offset))
        except Exception:
            return v
    return v


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
        # 会话池：取当前域会话（含 Cookie jar/UA/代理），封禁自动轮换
        sess = self.sessions.acquire(url)
        self._cur_session = sess
        hdrs = dict(s.get("headers") or {})
        hdrs.setdefault("User-Agent", sess.ua)
        ck = jar_cookie_header(sess.jar, url)
        if ck:
            hdrs.setdefault("Cookie", ck)
        kw = dict(params=query or None, headers=hdrs, proxy=sess.proxy,
                  max_size=int(s.get("max_size", 20 * 1024 * 1024)))
        try:
            if method.upper() == "POST":
                res = self.http.post(url, data=s.get("body"), json_data=s.get("json_body"), **kw)
            else:
                res = self.http.get(url, **kw)
        except Exception:
            self.sessions.report(sess, ok=False, blocked=True)
            raise
        # 封禁识别（Crawlee block-detection 思路）：200 但被风控的页面也会被识破
        ok = bool(res.get("ok"))
        bd = detect_block(res.get("status", 0), res.get("text", ""), res.get("headers"), url)
        blocked = bd["kind"] != "none" and bd["kind"] != "login"
        self.sessions.report(sess, ok=ok, blocked=blocked)
        # 会话 Cookie 续存（真 cookie jar：多 Set-Cookie + Expires 逗号正确处理）
        if ok:
            try:
                update_cookie_jar(sess.jar, res.get("url") or url,
                                  res.get("headers"), res.get("raw_headers"))
                sess.cookies = {c.name: c.value for c in sess.jar}
            except Exception:
                pass
        if blocked:
            log(f"  检测到反爬拦截[{bd['kind']}] {bd['detail']}（{url[:100]}）", "WARN")
            if self.anti.get("_block_stats") is not None:
                self.anti["_block_stats"][bd["kind"]] = self.anti["_block_stats"].get(bd["kind"], 0) + 1
            if bd["kind"] in ("cloudflare", "verify", "captcha", "rate_limit", "anti_bot",
                              "session_flagged", "waf", "429", "403"):
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
        # none/template 都做一次 token 替换（none：page=start、offset=0——"单个大 size 请求"即可行）
        do_tokens = (strat == "template") or strat == "none"
        if do_tokens:
            _tmpl_orig = {k: s.get(k) for k in ("url", "body", "json_body")}

        while page <= max_pages:
            params: Dict[str, Any] = {}
            if do_tokens:
                off = (page - 1) * limit
                for k, v0 in _tmpl_orig.items():
                    s[k] = _apply_page_tokens_value(v0, page, off)
            elif strat == "page_param":
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
                    # batch2400 美团战训：风控页/拦截页伪装 200 → 自动识别
                    _body = resp.get("text", "") or resp.get("body", b"").decode("utf-8", "ignore")
                    if len(_body) < 500 and any(
                        kw in _body for kw in ("验证", "captcha", "blocked", "forbidden", "登录")
                    ):
                        log(f"  ⛔ 疑似风控拦截（{len(_body)}B，含验证/拦截特征），"
                            "已停止——请换 IP 或冷却后再试（budget --list 查台账）", "ERROR")
                        break
                    log("  返回不是 JSON，停止", "ERROR")
                    break
                rp = pagination.get("records_path")
                # batch2200：单对象响应模式（GraphQL getQuote 类）——整个响应体作为一条记录
                if self.source.get("single_record"):
                    recs = [obj] if obj is not None else []
                elif rp:
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
                if isinstance(recs, list) and not recs and text:
                    log(f"  ⚠️ 记录数组为空/未命中——响应体前200字节: {text[:200]!r}"
                        f"（检查 pagination.records_path / 响应里的风控码）")
                # 终止条件
                if strat == "none":
                    break
                if total is not None:
                    # batch2400 审查修复：total 为 "1,024"/"12.0" 字符串曾 ValueError
                    # 且丢失本轮已抓记录
                    try:
                        _t = int(float(str(total).replace(",", "").strip()))
                    except (ValueError, TypeError):
                        _t = None
                    if _t is not None and (not recs or len(records) >= _t):
                        break
                elif not recs:
                    break
                page += 1
                # batch2400 美团战训：max_pages>1 但 page 始终为 1 → 翻页未生效
                if page == 2 and len(records) == 0:
                    log("  ⚠️ 翻页疑似未生效（page=2 仍 0 条），请检查 pagination 配置", "WARN")
                    break
            else:  # http_html
                if self.source.get("embedded_json"):
                    from .selectors import extract_embedded_json_rows
                    rows = extract_embedded_json_rows(text, self.source["embedded_json"])
                else:
                    rows = self._extract_html_rows(text)
                records.extend(rows)
                log(f"  page {page}: +{len(rows)}（累计 {len(records)}）")
                if not rows and text:
                    self._dump_debug_page(text, page)
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
        from .selectors import _lxml_html_tostring  # 函数内延迟导入（lxml 可选依赖）
        for el in rows:
            el_html = el if isinstance(el, str) else _lxml_html_tostring(el)
            row = {}
            for name, fspec in fields.items():
                if isinstance(fspec, str):
                    row[name] = el_html  # 简单模式：整行文本
                    continue
                if isinstance(fspec.get("subs"), dict):
                    # 结构化子字段（闲鱼战例）：价格 span 与"X人想要"无缝拼接不可逆——
                    # 在行内分别取子选择器，产出 dict（导出时安全序列化）
                    from .selectors import css_text as _ct
                    row[name] = {sub: _ct(el_html, sub_sel, 0).strip()
                                 for sub, sub_sel in fspec["subs"].items()}
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
        proc, errbuf = _spawn_bridge(cmd, env)
        assert proc.stdout is not None
        _completed = False
        try:
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
            rc, err = _wait_bridge(proc, errbuf)
            if rc != 0 and not err:
                die(f"浏览器桥退出码 {rc}")
            _completed = True
        finally:
            # 生成器被提前终止（消费端异常/die()）时回收桥子进程，防孤儿 node/Playwright
            if not _completed and proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=10)
                except Exception:
                    pass


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

    def fetch_pages(self, urls: List[str]) -> Dict[str, str]:
        """批量详情抓取（详情 browser 后端）：单次桥进程 = 单 CDP 连接顺序导航
        全部 URL，返回 {url: html}。闲鱼战例——登录态+JS 站的详情页 HTTP 全是空壳。"""
        import tempfile
        if not urls:
            return {}
        spec = self._build_spec(urls=urls)
        with tempfile.TemporaryDirectory(prefix="us_detail_") as tmp:
            spec_file = Path(tmp) / "spec.json"
            out_dir = Path(tmp) / "pages"
            spec_file.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            session_dir = Path(self.anti.get("session_dir") or "/tmp/universal_scraper_session")
            session_dir.mkdir(parents=True, exist_ok=True)
            storage_state = str(session_dir / f"{self.anti.get('session_name', 'session')}.json")
            cmd = [NODE, str(self.bridge), "--spec", str(spec_file), "--out", str(out_dir),
                   "--maxPages", str(len(urls)), "--settle", "1000",
                   "--captchaDir", "/tmp/universal_scraper_captcha",
                   "--storageState", storage_state,
                   "--scrollCount", "0", "--scrollWait", "1000",
                   "--loginTimeout", "600000",
                   "--headless", "0" if self.source.get("headless") is False else "1"]
            if self.source.get("cdp"):
                cmd += ["--cdp", str(self.source["cdp"])]
            env = dict(os.environ)
            env["NODE_PATH"] = NODE_PATH
            proc, errbuf = _spawn_bridge(cmd, env)
            assert proc.stdout is not None
            pages: Dict[str, str] = {}
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "detail_page" and obj.get("file") and not obj.get("error"):
                    try:
                        pages[obj["url"]] = Path(obj["file"]).read_text(encoding="utf-8", errors="replace")
                        log(f"  详情 {int(obj.get('index', 0)) + 1}/{len(urls)}: {obj.get('bytes', 0)}B")
                    except Exception:
                        pass
                elif obj.get("type") == "error":
                    proc.terminate()
                    die(f"详情批抓桥错误: {obj.get('message')}")
            rc, err = _wait_bridge(proc, errbuf)
            if rc != 0 and not pages:
                die(f"详情批抓桥退出码 {rc}: {err[-300:]}")
            return pages

    def _build_spec(self, urls: Any = None) -> Dict[str, Any]:
        s = self.source
        spec = {
            "url": s["url"],
            "js_pre": s.get("js_pre"),
            "wait": s.get("wait"),
            "actions": s.get("actions"),  # 闲鱼战例：v1.9 起透传给桥执行（此前静默丢弃）
            "pagination": s.get("pagination", {"type": "none"}),
            "captcha": s.get("captcha"),
            "slider": s.get("slider"),
            # capture 契约：布尔 true=全捕获（翻译成桥的 capture_all）；列表=声明式捕获。
            # 绝不能把布尔原样传给 spec.capture——桥会迭代它导致 TypeError 崩溃。
            "capture": (s.get("capture") if isinstance(s.get("capture"), list) else None),
            "capture_all": s.get("capture") is True,
            "login": s.get("login"),
            "verify": s.get("verify"),
        }
        if urls:
            spec["urls"] = urls
            spec["detail_wait_ms"] = s.get("detail_wait_ms", 1200)
        return spec

    def fetch_list(self, pagination: Dict[str, Any]) -> List[Dict[str, Any]]:
        import tempfile

        spec = self._build_spec()
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
                   "--startPage", str(int(pagination.get("start", 1))),
                   "--captchaDir", str(cap_dir),
                   "--storageState", storage_state,
                   "--scrollCount", str(self.source.get("scroll_count", 0)),
                   "--scrollWait", str(self.source.get("scroll_wait_ms", 2000)),
                   "--loginTimeout", str(self.source.get("login_timeout_ms", 600000)),
                   "--headless", "0" if self.source.get("headless") is False else "1"]
            if self.source.get("cdp"):
                cmd += ["--cdp", str(self.source["cdp"])]
            _td = self.source.get("_task_dir") or self.anti.get("_task_dir") or ""
            if _td:
                cmd += ["--stopFile", str(Path(_td) / ".stop")]
            env = dict(os.environ); env["NODE_PATH"] = NODE_PATH
            pp = self.anti.get("_proxy_pool")
            if pp is not None and pp.size:
                cmd += ["--proxy", pp.next() or ""]
            elif self.anti.get("proxy"):
                cmd += ["--proxy", self.anti["proxy"]]
            proc, errbuf = _spawn_bridge(cmd, env)
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
                    if self.source.get("embedded_json"):
                        from .selectors import extract_embedded_json_rows
                        rows = extract_embedded_json_rows(html, self.source["embedded_json"])
                    else:
                        rows = self._extract(html)
                    records.extend(rows)
                    log(f"  page {obj.get('page')}: +{len(rows)}（累计 {len(records)}）")
                    if not rows and html:
                        self._dump_debug_page(html, int(obj.get("page") or 1))
                    # 渲染页落盘到任务目录：供失败轮内 LLM 直接抽取/选择器精修用
                    try:
                        if _td:
                            _lp = Path(_td) / "last_page.html"
                            if not _lp.exists() or len(html) > _lp.stat().st_size:
                                _lp.write_text(html, encoding="utf-8")
                    except Exception:
                        pass
                elif t == "capture_file":
                    log(f"  捕获接口 {obj.get('name')}: {obj.get('count')} 个响应 -> {obj.get('file')}")
                    # 接口 JSON 落盘到任务目录：SPA 加密接口的数据喂 LLM 结构化抽取
                    try:
                        if _td and obj.get("file"):
                            _cf = Path(obj["file"])
                            if _cf.exists():
                                (Path(_td) / "capture_all.json").write_text(
                                    _cf.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                    except Exception:
                        pass
                elif t == "login":
                    log(f"[登录] {obj.get('message')}（请在浏览器窗口完成登录，最多等 {self.source.get('login_timeout_ms',600000)//1000}s）", "WARN")
                elif t == "login_ok":
                    log(f"[登录成功] 会话已保存: {obj.get('storageState')}", "WARN")
                elif t == "error":
                    proc.terminate(); die(f"浏览器错误: {obj.get('message')}")
            rc, err = _wait_bridge(proc, errbuf)
            if rc != 0:
                # 报错保留首行（真正的异常类型常在头部）+ 尾部，避免截断导致误诊
                _lines = [l for l in err.strip().splitlines() if l.strip()]
                _head = _lines[0][:220] if _lines else ""
                die(f"浏览器桥退出码 {rc}: 首行[{_head}] 尾部[{err[-300:]}]")
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
            from .selectors import _lxml_html_tostring
            el_html = el if isinstance(el, str) else (_lxml_html_tostring(el))
            row = {}
            for name, fspec in (s.get("fields", {}) or {}).items():
                if isinstance(fspec, str):
                    row[name] = css_text(el_html, fspec, 0)
                    continue
                if isinstance(fspec.get("subs"), dict):
                    # 结构化子字段（闲鱼战例）：行内分别取子选择器，防无缝拼接不可逆
                    row[name] = {sub: css_text(el_html, sub_sel, 0).strip()
                                 for sub, sub_sel in fspec["subs"].items()}
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
        每条记录 = {_api_url, data}，交给 LLM/解析器事后挑字段。
        batch1600 战训（P0）：桥侧早已落盘 method/post_data（改写 http_json 配置的
        关键参数），Python 侧此前转记录时丢弃——现已透传 _method/_post_data/
        _request_content_type，去重键同步纳入请求体（同 URL 不同体的 POST 不再误并）。"""
        f = out_dir / "capture_all.json"
        if not f.exists():
            return []
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            # 审查修复：解析失败绝不能静默当 0 条（整轮登录/验证码/配额都花了，
            # 却与"本来就没捕获到"无法区分——agent 会误判成 nodata）
            log(f"  ⚠️ capture_all.json 解析失败（{type(e).__name__}: {str(e)[:80]}），按 0 条处理，"
                "请检查文件是否被截断", "WARN")
            return []
        if not isinstance(data, list):
            log(f"  ⚠️ capture_all.json 结构异常（顶层 {type(data).__name__}，预期 list），按 0 条处理", "WARN")
            return []
        recs = []
        for item in data:
            if not isinstance(item, dict):
                continue
            rec = {"_api_url": item.get("url", ""), "data": item.get("json")}
            if item.get("method"):
                rec["_method"] = item["method"]
            if item.get("post_data"):
                rec["_post_data"] = item["post_data"]
            if item.get("request_content_type"):
                rec["_request_content_type"] = item["request_content_type"]
            recs.append(rec)
        # 去重（同一接口多次响应；POST 同 URL 不同请求体视为不同记录）
        seen = set()
        out = []
        for r in recs:
            k = (str(r.get("_api_url", "")) + ":" + str(r.get("_method", "GET")) + ":"
                 + str(r.get("_post_data", ""))[:200] + ":"
                 + json.dumps(r.get("data"), ensure_ascii=False)[:200])
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
                    if rows is not None:
                        # 猎聘战例：records_path 拼错时把整个响应体当一条记录、导出全空——必须可见
                        log(f"  ⚠️ capture[{cap.get('name')}] 未命中 records_path='{rp}'"
                            f"（响应顶层键: {list(obj)[:8] if isinstance(obj, dict) else type(obj).__name__}），"
                            "已按整条响应记录")
                    rows = [rows]
                for row in rows:
                    if isinstance(row, dict):
                        recs.append(row)
        log(f"捕获记录: {len(recs)} 条")
        return recs

