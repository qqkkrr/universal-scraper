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

from .core import log, die, smart_decode, update_cookie_jar, jar_cookie_header, safe_fname, _budget_auto_mark
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


class _ErrBuf(list):
    """stderr 缓冲（OCR R131 M）：携带排空线程句柄，供 wait 后 join——
    否则 join 前最后几行 stderr 可能还没进缓冲就被读走（诊断信息丢失）。"""


def _spawn_bridge(cmd: List[str], env: Optional[Dict[str, str]] = None):
    """启动浏览器桥子进程 + 后台排空 stderr（防止管道写满死锁）。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8",
                            env=env if env is not None else dict(os.environ))
    errbuf = _ErrBuf()
    if proc.stderr:
        def _drain():
            try:
                for ln in proc.stderr:
                    errbuf.append(ln)
            except Exception:
                pass
        errbuf.thread = threading.Thread(target=_drain, daemon=True)
        errbuf.thread.start()
    else:
        errbuf.thread = None
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
    # OCR R131（M）：进程退出 ≠ 排空线程已收尾——立即 join 会让最后几行
    # stderr 丢失（崩溃诊断恰好就在那几行）
    _t = getattr(errbuf, "thread", None)
    if _t is not None:
        _t.join(timeout=5)
    return rc, "".join(errbuf)


def _resolve_template(value: Any, vars: Dict[str, str]) -> Any:
    """把 config 里的 {{var}} 模板替换成任务变量。"""
    if isinstance(value, str):
        def _repl(m):
            return str(vars.get(m.group(1), m.group(0)))
        _out = re.sub(r"\{\{(\w+)\}\}", _repl, value)
        # 审查修复（P2，R5）：未解析的 {{var}} 曾静默渲染成字面量 URL——
        # 真实请求发向错误地址，0 条后 nodata 还赖站点
        if isinstance(_out, str) and "{{" in _out and re.search(r"\{\{\w+\}\}", _out):
            log(f"⚠️ 模板变量未解析（变量缺失）: {_out[:120]}——请检查 --var / vars 配置",
                "WARN")
        return _out
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
            # R116：代理 API adapter（拉取失败 WARN 不中断）
            try:
                from .proxy_api import merge_api_proxies
                merge_api_proxies(anti, anti.get("_log_cb"))
            except Exception:
                pass
            self.proxy_pool = ProxyPool(anti.get("proxies"), anti.get("proxy_mode", "round_robin"), warn=anti.get("_log_cb"))
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
            # 会话池的代理从 ProxyPool 动态获取，失败同步反馈给 ProxyPool。
            # 审查修复（P2）：_proxy_ok_cb/_proxy_fail_cb 曾漏接线——死代理
            # 永远不进冷却，整个任务轮流踩坑
            self.sessions._proxy_source = self.proxy_pool.next
            self.sessions._proxy_ok_cb = self.proxy_pool.mark_ok
            self.sessions._proxy_fail_cb = self.proxy_pool.mark_fail
        # OCR R131（L）：_cur_session 曾只写不读（会话经局部变量传递）——删除死状态

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
        hdrs = dict(s.get("headers") or {})
        hdrs.setdefault("User-Agent", sess.ua)
        with sess.jlock:  # CookieJar 非线程安全（审查 P2，R14）
            ck = jar_cookie_header(sess.jar, url)
        if ck:
            hdrs.setdefault("Cookie", ck)
        kw = dict(params=query or None, headers=hdrs, proxy=sess.proxy,
                  max_size=int(s.get("max_size", 20 * 1024 * 1024)),
                  # 审查十一轮（H2）：翻页越过末页的正常 404 曾惩罚会话与代理
                  # （errors+1 → 3 次后新建会话丢登录 jar + 代理冷却直连暴露真实
                  # IP）。modules/fetchers.py 早已带 allow_html_404=True，根文件
                  # 漏同步——404 交给 detect_block 判 http_error（blocked=False）
                  allow_html_404=True)
        try:
            if method.upper() == "POST":
                res = self.http.post(url, data=s.get("body"), json_data=s.get("json_body"), **kw)
            else:
                res = self.http.get(url, **kw)
        except Exception:
            # 审查修复（P2）：传输异常曾 blocked=True——DNS 抖动/超时会瞬间
            # 丢弃含登录态 cookie 的会话（v3 同款修复的镜像）
            self.sessions.report(sess, ok=False, blocked=False)
            raise
        # 封禁识别（Crawlee block-detection 思路）：200 但被风控的页面也会被识破
        ok = bool(res.get("ok"))
        bd = detect_block(res.get("status", 0), res.get("text", ""), res.get("headers"), url)
        blocked = bd["kind"] not in ("none", "login", "http_error")
        # R34 修复：http_error（普通 4xx/5xx）曾被当反爬封禁——翻页越过末页的 404
        # 或瞬时 500 会立刻轮换会话 + 冷却代理 + 污染 _block_stats。
        # modules/fetchers.py:361 早已修过同一缺陷，根文件漏同步
        # 审查修复（P2）：blocked 页曾以 ok=True 上报——被风控的 200 会把会话
        # 错误计数清零，被污染的 cookie 活过轮换
        self.sessions.report(sess, ok=ok and not blocked, blocked=blocked)
        # 自适应限速（深度改进②）：HTTP 状态级信号（429/403/5xx，含客户端内部
        # 重试）已由客户端静默自记（唯一写入口，防双计——fetcher 再记会让同一
        # 403 间隔翻两次）。这里只补客户端看不见的 200 皮风控页，并播报事件。
        _at = getattr(self.http, "_at", None)
        if _at is not None:
            _st = int(res.get("status", 0) or 0)
            if blocked and _st == 200:
                _at.note_block(bd["kind"])
            _nb = _at.pop_block()
            if _nb > 0:
                log(f"  🐢 自动降速：请求间隔 → {_nb:.1f}s（连续命中会继续放缓）", "WARN")
            _nr = _at.pop_restore()
            if _nr > 0:
                log(f"  🐇 连续成功，逐步恢复速度：请求间隔 → {_nr:.1f}s")
        # 会话 Cookie 续存（真 cookie jar：多 Set-Cookie + Expires 逗号正确处理）
        if ok:
            try:
                with sess.jlock:  # CookieJar 非线程安全（审查 P2，R14）
                    update_cookie_jar(sess.jar, res.get("url") or url,
                                      res.get("headers"), res.get("raw_headers"))
                    sess.cookies = {c.name: c.value for c in sess.jar}
            except Exception as e:
                # OCR R6：裸 pass 曾让 Set-Cookie 解析失败静默丢登录态——
                # 后续请求未登录 403 时无从排查。至少大声记录
                log(f"  ⚠️ 会话 Cookie 更新失败（登录态可能丢失）: {type(e).__name__}: {str(e)[:120]}", "WARN")
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
        # OCR R131（H）：分页/种子循环曾原地改写 source["url"] 且从不恢复——
        # fetcher 复用（detail/resume）时拿的是"最后一页 URL"而非入口模板。
        # 薄包装：出口一律还原入口模板（内层实现按需改写仍生效）。
        # 审查七轮（FIX-REGRESSION）：_fetch_single_page 的 token 替换三元组是
        # (url, body, json_body)——只还原 url 时 body/json_body 留着最后一页的
        # 替换值（{{page}} 已消失），同 fetcher 内后续 _tmpl_orig 捕获到 stale
        # 模板（sitemap 多种子即触发），翻页 token 永久失效
        _orig = {k: self.source.get(k) for k in ("url", "body", "json_body")}
        try:
            return self._fetch_list_impl(pagination)
        finally:
            for _k, _v in _orig.items():
                if _v is not None or _k in self.source:
                    self.source[_k] = _v

    def _fetch_list_impl(self, pagination: Dict[str, Any]) -> List[Dict[str, Any]]:
        s = self.source
        stype = s.get("type", "http_json")
        strat = pagination.get("strategy", "none")
        # --- robots.txt 合规检查（batch2400 战训：把已有 robots.py 模块串入主路径） ---
        # R22 修复：改为与 v3 引擎/CLI 一致的 opt-in（anti.respect_robots）——此前
        # v2 无条件检查而 v3/crawl 默认关闭，同名概念相反的默认值让用户无所适从
        url0 = s.get("url", "")
        if url0 and self.anti.get("respect_robots"):
            try:
                from .robots import RobotsTxt
                if not hasattr(self, "_robots_checker"):
                    self._robots_checker = RobotsTxt(user_agent="universal-scraper/1.0")
                if not self._robots_checker.allowed(url0):
                    raise PermissionError(
                        f"robots.txt 禁止抓取 {url0}——请更换目标或确认你有合法访问权"
                    )
            except PermissionError:
                raise
            except Exception:
                pass  # robots 检测本身失败不阻塞
        # sitemap 种子：http_html 依次抓取每个种子页
        seeds = s.get("sitemap_urls") or []
        if stype == "http_html" and seeds:
            records: List[Dict[str, Any]] = []
            for seed in seeds:
                # OCR R131（M）：种子页曾绕过 robots 检查（只查了入口 url0）——
                # respect_robots 开启时每个种子单独判，禁抓的跳过并告警
                if self.anti.get("respect_robots") and getattr(self, "_robots_checker", None):
                    try:
                        _ok = self._robots_checker.allowed(seed)
                    except Exception:
                        _ok = True  # robots 检测本身失败不阻塞（与入口口径一致）
                    if not _ok:
                        log(f"⚠️ robots.txt 禁止抓取种子页，已跳过: {seed}", "WARN")
                        continue
                self.source["url"] = seed
                records.extend(self._fetch_single_page(pagination, strat))
            return records
        return self._fetch_single_page(pagination, strat)

    def _fetch_single_page(self, pagination: Dict[str, Any], strat: str) -> List[Dict[str, Any]]:
        s = self.source
        stype = s.get("type", "http_json")
        url = s.get("url", "")  # pyflakes 修复：阻断检测引用 url——本函数此前无此变量
        max_pages = int(pagination.get("max_pages", 100))
        page = int(pagination.get("start", 1))
        limit = pagination.get("limit", 20)
        records: List[Dict[str, Any]] = []
        total = None
        # none/template 都做一次 token 替换（none：page=start、offset=0——"单个大 size 请求"即可行）
        # R28b 修复：next_selector/next_xpath（下一页链接跟随）与 token 重置互斥——
        # 无 strategy 时默认 none 会每轮把 _fetch_single_page 写回的"下一页 URL"
        # 冲回模板原值，同页重复抓到 max_pages（静默重复数据）。
        # R28c 补充：抑制仅限 http_html——next_selector 只在该分支被消费；
        # http_json 上误配它时保持原 token 行为（选择器本就无效，别破坏翻页）
        # T1 实测（2026-09-27）易用性防御：page_param/offset 策略与 URL/body 里的
        # {page}/{offset} 模板混用时，字面 "{page}" 会进服务器（gov.cn 双 p 冲突 400）。
        # 自动按当页替换模板 + WARN（同值重复页参已被实测验证无害），并建议改 template。
        _toks = ("{{page}}", "{page}", "{{offset}}", "{offset}")
        _mixed = strat in ("page_param", "offset") and any(
            t in str(s.get(k) or "") for k in ("url", "body", "json_body") for t in _toks)
        if _mixed:
            log(f"  ⚠️ strategy={strat} 与 URL/body 中的页码模板混用——已自动替换模板"
                f"（建议改用 strategy=template 消除歧义）", "WARN")
        do_tokens = (((strat == "template") or strat == "none") or _mixed) and not (
            s.get("type", "http_json") == "http_html"
            and (pagination.get("next_selector") or pagination.get("next_xpath")))
        if do_tokens:
            _tmpl_orig = {k: s.get(k) for k in ("url", "body", "json_body")}

        while page <= max_pages:
            _prev_n = len(records)  # 审查三轮：进本页前行数——翻页失效告警的比较基准
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
            # R34 修复：robots 检查曾只查首 URL——翻页改写的 URL（含 query 的
            # Disallow 规则如 /*?page=）与 sitemap 种子全部绕过合规检查
            # R61 修复：page_param/offset 的页参在 _request(params=) 内追加，
            # 只查 s["url"] 看不到 ?page=N——按客户端同样的拼法构造最终 URL 再查
            if getattr(self, "_robots_checker", None) is not None:
                try:
                    from urllib.parse import urlencode as _ue
                    _q_all = dict(s.get("query", {}))
                    _q_all.update(params)
                    _eff = s.get("url", "")
                    if _q_all:
                        _eff = _eff + ("&" if "?" in _eff else "?") + _ue(_q_all)
                    if not self._robots_checker.allowed(_eff):
                        log(f"  ⛔ robots.txt 禁止抓取翻页 URL {_eff}——停止翻页（已抓 {len(records)} 条）")
                        break
                except Exception:
                    pass  # robots 检测本身失败不阻塞
            resp = self._request(params)
            if not resp.get("ok"):
                log(f"  请求失败: {resp.get('text','')[:200]}", "ERROR")
                # 审查修复：429/403/52x 走 ok=False 分支曾只 break——
                # 部分数据被当完整成功导出（exit 0），同属"拦截被当成功"事故类
                _st = resp.get("status", 0)
                if _st in (403, 429) or 500 <= _st <= 529:
                    from .protocols import BlockDetectedError
                    raise BlockDetectedError(url=url, kind=f"http_{_st}",
                                             detail=f"HTTP {_st} 且请求失败——拦截/限流硬停机")
                # 审查十一轮（H4）：传输层失败（重试耗尽 status=0）曾静默 break——
                # 100 页任务第 40 页断网只导出 39 页、engine 视为成功 exit 0。
                # 出声告警（已抓部分仍保留，但用户能看到"任务不完整"）
                if not _st:
                    log("  ⚠️ 传输层失败（重试已耗尽，status=0）——本页未取回，"
                        "已抓数据保留但任务不完整，请查网络/出口后重跑", "ERROR")
                break
            body = resp.get("body", b"")
            # 审查十一轮（H3）：响应被 max_size 截断时 fetcher 曾完全不看
            # truncated——JSON 截断后 json=None 静默 0 条、HTML 截断后尾部记录
            # 静默丢失（modules 已修，根文件漏同步）
            if resp.get("truncated"):
                log(f"  ⚠️ 响应超过 max_size 被截断（{len(body)}B）——本页记录可能不完整，"
                    "如数据缺失请调大 source.max_size", "ERROR")
            text = smart_decode(body, resp.get("headers") or {})

            # 裁判文书网战训（2026-09 DeepSeek 考核）：93 字符封禁页被判"页面太短
            # →已下架"，继续抓 1.3 万条全是封禁页。此前的关键词检查只护"非 JSON"
            # 分支且指纹窄——现在每页都过 detect_block 富指纹库；命中硬停机。
            from .antibot import detect_block as _detect_block, ShortPageStreak
            if not hasattr(self, "_short_streak"):
                self._short_streak = ShortPageStreak()
            _bd = _detect_block(resp.get("status", 0), text, resp.get("headers"), url)
            if _bd["kind"] not in ("none", "login", "http_error"):
                log(f"  ⛔ 判定为封禁/拦截页[{_bd['kind']}]：{_bd['detail']}——硬停机"
                    "（宁可不写，也不能把封禁页写进数据。budget --list 查台账）", "ERROR")
                try:
                    # 审查修复：只按真实状态记账（200 内容指纹不烧台账——
                    # 短通知页含"违规"曾按伪造 403 记账误黑名单 24h）
                    if resp.get("status", 0) >= 400:
                        _budget_auto_mark(url, resp.get("status", 0), body=body[:8192])
                except Exception:
                    pass
                from .protocols import BlockDetectedError
                raise BlockDetectedError(url=url, kind=_bd["kind"], detail=_bd["detail"])

            if stype == "http_json":
                obj = resp.get("json")
                if obj is None:
                    # batch2400 美团战训：风控页/拦截页伪装 200 → 自动识别。
                    # 审查修复：曾只 break——部分数据被当完整成功（exit 0）；且
                    # 这是 detect_block 之后仅剩的守门（login/漏指纹类），必须硬停
                    _body = resp.get("text", "") or resp.get("body", b"").decode("utf-8", "ignore")
                    if len(_body) < 500 and any(
                        kw in _body for kw in ("验证", "captcha", "blocked", "forbidden", "登录")
                    ):
                        log(f"  ⛔ 疑似风控拦截（{len(_body)}B，含验证/拦截特征）——硬停机", "ERROR")
                        from .protocols import BlockDetectedError
                        raise BlockDetectedError(url=url, kind="legacy_keyword",
                                                 detail=f"{len(_body)}B 短页含拦截关键词")
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
                    for _k in ("records", "items", "list", "results", "data",
                               "announcements",  # cninfo 公告（R28 实战）
                               "rows", "dataList"):
                        _v = obj.get(_k) if isinstance(obj, dict) else None
                        if isinstance(_v, list):
                            recs = _v
                            break
                total = jpath(obj, pagination.get("total_path", "data.total"), total)
                # 审查十一轮（H5）：翻页参数被服务端忽略时各页返回相同内容——
                # 每页都有"新增行"故 len 比较与"第 2 页零新增"启发式都不触发，
                # 静默产出重复数据（3 页 6 条 unique 只有 2 个）。页级指纹相同即
                # 判翻页无效，停机避免重复
                _page_sig = tuple(sorted(str(r)[:200] for r in recs[:8])) \
                    if isinstance(recs, list) and recs else ()
                if page >= 2 and _page_sig and _page_sig == self._prev_page_sig:
                    log("  ⚠️ 本页内容与上一页完全相同（翻页参数可能未被服务端消费，"
                        "POST 页码在 body 时 page_param 只改 query 无效）——停机避免重复数据", "WARN")
                    break
                self._prev_page_sig = _page_sig
                records.extend(recs if isinstance(recs, list) else [])
                log(f"  page {page}: +{len(recs) if isinstance(recs, list) else 0}（累计 {len(records)}）")
                if isinstance(recs, list) and recs:
                    # 审查修复：正常记录页必须重置连击——否则"连续"退化成"累计"，
                    # 零星坏页跨页累积会假停机
                    self._short_streak.reset()
                if isinstance(recs, list) and not recs and text:
                    log(f"  ⚠️ 记录数组为空/未命中——响应体前200字节: {text[:200]!r}"
                        f"（检查 pagination.records_path / 响应里的风控码）")
                    # 连击启发式只喂"拿到响应却没产出记录"的页——正常记录页长度
                    # 各异不算；封禁页永远产出不了记录（裁判文书网战训）
                    if self._short_streak.feed(len(body)):
                        log("  ⛔ 连续 3 页响应极短且长度一致且均无记录——疑似封禁页连发，硬停机",
                            "ERROR")
                        from .protocols import BlockDetectedError
                        raise BlockDetectedError(url=url, kind="short_page_streak",
                                                 detail=f"连续 {self._short_streak.limit} 页 ≤512B 同长无记录")
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
                # batch2400 美团战训：max_pages>1 但 page 始终为 1 → 翻页未生效。
                # 审查三轮（M）：len(records) 是累计值——page=2 时第 1 页的记录已
                # 在内，比较对象应为进入本页前的行数（_prev_n），否则第 2 页起恒不触发
                if page == 2 and len(records) == _prev_n:
                    log("  ⚠️ 翻页疑似未生效（page=2 未新增任何记录），请检查 pagination 配置", "WARN")
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
                    _dump_debug_page(s.get("url", ""), text, page)
                nxt = pagination.get("next_selector") or pagination.get("next_xpath")
                nxt_url = self._next_url(text, nxt)
                if not rows:
                    break
                # R42 修复（P0）：page_param/offset 在 http_html 上按查询参数翻页——
                # 此前循环只认"下一页"链接，配 page_param 的最常见配方静默只抓第 1 页
                # （exit 0 假成功，服务端日志证明只有 ?page=1）
                if strat in ("page_param", "offset"):
                    if nxt_url:
                        from urllib.parse import urljoin
                        self.source["url"] = urljoin(self.source["url"], nxt_url)
                    page += 1
                    continue
                if not nxt_url:
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
            # 审查十一轮（M）：lstrip("xpath:") 按字符集剥离——"xpath:tr[1]/…"
            # 被啃成 "r[1]/…"（x/t/p/a/h 首字符集），相对 xpath 的下一页链接
            # 全部静默失效（翻页止步第 1 页）；仅 "xpath://…" 侥幸正确
            url = xpath_text(html, sel.removeprefix("xpath:") if sel.startswith("xpath:")
                             else sel, 0, " ")
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
                    # R34 修复：求解成功不再向 yield 透传 captcha 事件——fetch_list
                    # 对 captcha 事件直接 die，会在桥轮询刚收到的答案时把桥杀掉
                    # （自动求解整体成死代码）。求解失败才上抛报错
                    if res.get("error"):
                        yield obj
                    continue
                yield obj
            rc, err = _wait_bridge(proc, errbuf)
            # 审查八轮（HIGH）：条件曾写反为 `rc != 0 and not err`——Node 未捕获异常
            # 必然往 stderr 打栈，于是"桥崩溃 + 有 stderr"这一最常见组合反而不报错、
            # 静默按成功收尾（返回部分或 0 条），用户被引向"选择器写错/无数据"。
            # 与姊妹实现 modules/fetchers.py 的口径对齐：非零退出且 stderr 有内容
            # = 硬失败；非零退出但 stderr 为空（cleanup 阶段失败等）保留已抓到的事件。
            if rc != 0:
                if err.strip():
                    die(f"浏览器桥退出码 {rc}：{err.strip()[-500:]}")
                log(f"⚠️ 浏览器桥退出码 {rc}（stderr 为空，疑似收尾阶段失败），"
                    "已产出的事件按原样保留", "WARN")
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
            storage_state = str(session_dir / 
                            f"{safe_fname(self.anti.get('session_name', 'session'))}.json")  # R91：净化防路径逃逸
            cmd = [NODE, str(self.bridge), "--spec", str(spec_file), "--out", str(out_dir),
                   "--maxPages", str(len(urls)), "--settle", "1000",
                   "--captchaDir", "/tmp/universal_scraper_captcha",
                   "--storageState", storage_state,
                   "--scrollCount", "0", "--scrollWait", "1000",
                   "--loginTimeout", "600000",
                   "--headless", "0" if self.source.get("headless") is False else "1"]
            if self.source.get("cdp"):
                cmd += ["--cdp", str(self.source["cdp"])]
            # 审查十一轮（H6）：详情批抓曾完全绕过代理配置——列表走代理、详情
            # 直连（真实 IP 暴露且与列表 IP 不一致易触发风控）。与 fetch_list
            # 同款：代理池优先（全冷却时不传=直连），否则 anti.proxy
            _pp = self.anti.get("_proxy_pool")
            if _pp is not None and _pp.size:
                _px = _pp.next()
                if _px:
                    cmd += ["--proxy", _px]
            elif self.anti.get("proxy"):
                cmd += ["--proxy", self.anti["proxy"]]
            env = dict(os.environ)
            env["NODE_PATH"] = NODE_PATH
            proc, errbuf = _spawn_bridge(cmd, env)
            assert proc.stdout is not None
            # 审查七轮（N38）：循环内异常（页面文件读取失败/提取报错）曾跳过
            # kill/reap——孤儿 Node/Playwright 占着端口和内存。同 _run 的兜底口径
            _completed = False
            try:
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
                        except Exception as e:
                            log(f"  ⚠️ 详情页读取失败 {obj.get('url','')[:80]}: {e}", "WARN")
                    elif obj.get("type") == "detail_page" and obj.get("error"):
                        # 审查修复（P2）：桥明确上报的失败原因（封禁/验证码/超时）
                        # 曾被静默忽略——browser_miss 标签掩盖了真实原因
                        log(f"  ⚠️ 详情页失败 {str(obj.get('url', ''))[:80]}: {str(obj.get('error'))[:120]}", "WARN")
                    elif obj.get("type") == "error":
                        # 审查修复（HIGH）：terminate 后必须 reap 再退出——die() 立即
                        # 抛异常，wait 永不执行，Node/Playwright 孤儿进程占着端口和内存
                        proc.terminate()
                        _wait_bridge(proc, errbuf, timeout=10)
                        die(f"详情批抓桥错误: {obj.get('message')}")
                rc, err = _wait_bridge(proc, errbuf)
                _completed = True
            finally:
                if not _completed and proc.poll() is None:
                    try:
                        proc.kill()
                        proc.wait(timeout=10)
                    except Exception:
                        pass
            if rc != 0 and not pages:
                die(f"详情批抓桥退出码 {rc}: {err[-300:]}")
            if rc != 0 and pages:
                # OCR R6：部分成功曾静默返回——行级 browser_miss 虽已标注，
                # 但"桥中途崩溃"这个总因没有信号。补一条告警（部分结果仍返回）
                log(f"  ⚠️ 详情批抓桥异常退出（码 {rc}，已收 {len(pages)} 页，其余未处理）: {err[-300:]}", "WARN")
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
            # 审查十一轮（C）：曾只认 capture is True——auto.py 自己生成的
            # source={"record_from":"capture_all","capture_all":true} 形态被白名单
            # 吞掉，桥永不捕获，而 record_from:"capture_all" 把已解析的行替换成
            # 空列表（静默全丢）。两种拼写都认
            "capture_all": (s.get("capture") is True) or (s.get("capture_all") is True),
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
            storage_state = str(session_dir / 
                            f"{safe_fname(self.anti.get('session_name', 'session'))}.json")  # R91：净化防路径逃逸

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
                _px = pp.next()
                # OCR R131 终审（M）：池全冷却时 next() 返回 None——曾传空字符串
                # "--proxy ''"，浏览器桥收到空代理报错或直连。改为不加该参数（直连）
                if _px:
                    cmd += ["--proxy", _px]
            elif self.anti.get("proxy"):
                cmd += ["--proxy", self.anti["proxy"]]
            proc, errbuf = _spawn_bridge(cmd, env)
            assert proc.stdout is not None
            # 审查七轮（N38）：同 fetch_pages——循环内异常曾跳过 kill/reap（孤儿浏览器）
            _completed = False
            try:
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
                            # OCR R131（C）：曾调不存在的 self._dump_debug_page——0 行
                            # 且有 HTML 时必抛 AttributeError，整个批抓报废
                            _dump_debug_page(self.source.get("url", ""), html, int(obj.get("page") or 1))
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
                        # 审查修复（HIGH）：同详情批抓路径——terminate 后 reap，防孤儿浏览器进程
                        proc.terminate(); _wait_bridge(proc, errbuf, timeout=10); die(f"浏览器错误: {obj.get('message')}")
                rc, err = _wait_bridge(proc, errbuf)
                _completed = True
            finally:
                if not _completed and proc.poll() is None:
                    try:
                        proc.kill()
                        proc.wait(timeout=10)
                    except Exception:
                        pass
            # R102 战报修复：capture_all.json 原件随 tmp 目录销毁（影视飓风战例：
            # 侥幸靠副产物留了一份）——运行结束先备份到任务输出目录再继续
            try:
                _cap_src = out_dir / "capture_all.json"
                if _cap_src.exists():
                    _cap_dst = Path(_td) / "capture_all.json" if _td else Path("outputs") / "captures" / "capture_all.json"
                    _cap_dst.parent.mkdir(parents=True, exist_ok=True)
                    import shutil as _shutil
                    _shutil.copy2(_cap_src, _cap_dst)
                    log(f"  📋 capture_all.json 已备份到任务目录: {_cap_dst}")
            except Exception as _cap_err:
                log(f"  capture_all 备份失败（不影响本次结果）: {_cap_err}", "WARN")
            if rc != 0:
                # 报错保留首行（真正的异常类型常在头部）+ 尾部，避免截断导致误诊
                _lines = [l for l in err.strip().splitlines() if l.strip()]
                _head = _lines[0][:220] if _lines else ""
                # 审查十一轮（M）：桥收尾崩溃时曾无条件 die，丢弃本次已收全部
                # records（fetch_pages 部分页保留 + _run 保留的部分数据语义，
                # 只有这里例外）。已收到记录时保留并出声，engine 仍能看到数据
                if records:
                    log(f"  ⚠️ 浏览器桥退出码 {rc}（首行[{_head}]）——已收 {len(records)} 条"
                        "保留使用，但本次抓取可能不完整", "ERROR")
                else:
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
                        # R98 修复（P2）：登录 Cookie 串曾 0644 落盘（R18 同类）
                        import os as _os
                        _fd = _os.open(_ct, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
                        with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
                            _f.write("; ".join(_pairs))
                        # R18b 同款：mode 只在创建时生效——升级前遗留的 0644 须显式治愈
                        _os.chmod(_ct, 0o600)
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
            # OCR R131 反馈 #4：请求头透传——capture → HTTP 重放的关键链路
            # （UA/Referer/Authorization 等录了才不用猜）
            if item.get("request_headers"):
                # 滤掉无重放价值的自动头（长度不定、每次变）
                _skip = {"content-length", "accept-encoding", "connection", "host"}
                _rh = {k: v for k, v in item["request_headers"].items() if k.lower() not in _skip}
                if _rh:
                    rec["_request_headers"] = _rh
            recs.append(rec)
        # 去重（同一接口多次响应；POST 同 URL 不同请求体视为不同记录）
        # OCR R131（M）：键曾截断 post_data/data 前 200 字符——长请求体前 200
        # 字相同的两个请求被误并。改全量内容哈希（确定性且无碰撞窗口）
        import hashlib as _hl
        seen = set()
        out = []
        for r in recs:
            # 审查八轮（LOW）：各段曾用 "|" 拼接——URL/post_data 里含 "|" 时可构造
            # 碰撞（如 url="...?f=a|GET" + method="GET" 与 url="...?f=a" + post="GET|"）。
            # 改用 \x1f 分隔 + 段长前缀，彻底消除歧义。
            _parts = [str(r.get("_api_url", "")), str(r.get("_method", "GET")),
                      str(r.get("_post_data", "")),
                      json.dumps(r.get("data"), ensure_ascii=False, default=str)]
            _blob = "\x1f".join(f"{len(p)}:{p}" for p in _parts)
            k = _hl.md5(_blob.encode("utf-8"), usedforsecurity=False).hexdigest()
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
            # 审查二轮（H）：name 曾未清洗——分享/LLM 生成的配置里 "../x" 可越出
            # out_dir 读写任意同名文件。safe_fname 剥路径分隔符
            _cname = safe_fname(str(cap.get("name") or "capture"))
            f = out_dir / f"{_cname}.json"
            if not f.exists():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            except Exception as e:
                # 审查修复（MEDIUM）：捕获文件截断/损坏曾直接炸掉整个抓取——
                # 已采集的其余记录全丢。降级为跳过该捕获项并大声告警
                log(f"  ⚠️ capture[{cap.get('name')}] JSON 解析失败（{type(e).__name__}: {str(e)[:80]}），跳过该捕获项", "WARN")
                continue
            if not isinstance(data, list):
                log(f"  ⚠️ capture[{cap.get('name')}] 结构异常（顶层 {type(data).__name__}，预期 list），跳过", "WARN")
                continue
            rp = cap.get("records_path", "")
            for item in data:
                obj = item.get("json") if isinstance(item, dict) and "json" in item else item
                rows = jpath(obj, rp) if rp else obj
                if not isinstance(rows, list):
                    if rows is None:
                        # 审查十一轮（M）：jpath 未命中返回 None 时告警曾被
                        # `if rows is not None` 吞掉——拼错的 records_path 静默 0 条
                        # （注释声称"必须可见"完全落空）
                        log(f"  ⚠️ capture[{cap.get('name')}] records_path='{rp}' 未命中任何值"
                            f"（响应顶层键: {list(obj)[:8] if isinstance(obj, dict) else type(obj).__name__}）", "WARN")
                    else:
                        # 猎聘战例：records_path 拼错时把整个响应体当一条记录、导出全空——必须可见
                        log(f"  ⚠️ capture[{cap.get('name')}] records_path='{rp}' 命中非列表值"
                            f"（{type(rows).__name__}），已按整条响应记录", "WARN")
                    rows = [rows]
                for row in rows:
                    if isinstance(row, dict):
                        recs.append(row)
        log(f"捕获记录: {len(recs)} 条")
        return recs

