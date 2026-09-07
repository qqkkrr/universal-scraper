#!/usr/bin/env python3
"""v3 插件化引擎：任务生命周期 = 调度 → 取数 → 路由 → 解析 → 流水线 → 存储。

设计（对标 Scrapy Engine + Crawlee Router/RequestQueue/Autoscaling）：
  - RequestQueue 统一调度（去重/域名限速/深度预算）
  - Rules 路由：URL → parser（Scrapy rules 风格）
  - Parser 产出 items + 新请求 → 递归爬取
  - 自适应并发：按平均响应时间动态调 worker 数
  - 插件全可替换：任务包 modules/ 覆盖任意模块
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from .log import Logger, logger
from .selectors import jpath, apply_extractor


_map_record_warned = set()   # 已告警过的坏模板 (字段名, template)——每个只 WARN 一次


def map_record(raw: dict, fields: dict) -> dict:
    """按 record.fields 把原始记录映射为目标字段（from/path/constant/template）。"""
    out = {}
    for name, spec in (fields or {}).items():
        if isinstance(spec, str):
            out[name] = jpath(raw, spec, None)
        elif isinstance(spec, dict):
            if "from" in spec:
                out[name] = jpath(raw, spec["from"], spec.get("default"))
            elif "path" in spec:
                out[name] = jpath(raw, spec["path"], spec.get("default"))
            elif "constant" in spec:
                out[name] = spec["constant"]
            elif "template" in spec:
                try:
                    out[name] = spec["template"].format(**{k: (v if v is not None else "") for k, v in raw.items()})
                except Exception as e:
                    # 模板渲染失败不能静默置空（配置错误会被伪装成"数据缺失"）；
                    # 每个坏模板只告警一次（每条记录都会走这里，重复告警会刷屏）
                    _key = (name, spec["template"])
                    if _key not in _map_record_warned:
                        _map_record_warned.add(_key)
                        logger.warn(f"模板渲染失败，字段置空（字段={name}，template={spec['template']!r}）：{e}")
                    out[name] = ""
            elif "concat" in spec:
                out[name] = "".join(str(jpath(raw, pth, "")) for pth in spec["concat"])
            else:
                out[name] = jpath(raw, spec.get("path", ""), None)
    return out
from .queue import RequestQueue
from .protocols import ParseContext, Request, RateLimitedError
from .task import Task


def resolve_tpl(value: Any, vars: Dict[str, str]) -> Any:
    """把配置里的 {{var}} 与 {var} 模板替换成任务变量（AI 常写单花括号）。"""
    if isinstance(value, str):
        return re.sub(r"\{\{?(\w+)\}?\}", lambda m: str(vars.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: resolve_tpl(v, vars) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_tpl(v, vars) for v in value]
    return value


_bad_pattern_warned = set()   # 已告警过的坏正则 pattern——每个只 WARN 一次


def _match_rule(rule: Dict[str, Any], url: str) -> bool:
    m = rule.get("match", "regex")
    pat = rule.get("pattern") or "/"  # AI 可能写 null/空：None in url 会 TypeError
    if m == "contains":
        return pat in url
    if m == "regex":
        try:
            return re.search(pat, url) is not None
        except re.error as e:
            # 坏正则永远 False（路由静默失效）→ 至少让配置错误可诊断（每个 pattern 只告警一次）
            if pat not in _bad_pattern_warned:
                _bad_pattern_warned.add(pat)
                logger.warn(f"坏正则，规则恒不命中（match=regex, pattern={pat!r}）：{e}")
            return False
    if m == "startswith":
        return url.startswith(pat)
    return False


class EngineV3:
    def __init__(self, task: Task, overrides: Optional[Dict[str, str]] = None,
                 limit: Optional[int] = None, resume: bool = False,
                 dry_run: bool = False, log_file: Optional[Path] = None,
                 start_url: Optional[str] = None, log_cb=None):
        self.task = task
        self.config = task.config
        self.vars = dict(task.config.get("vars", {}))
        if overrides:
            self.vars.update(overrides)
        self.start_url = start_url
        self.limit = int(limit) if limit is not None else None  # 防御：网页/API 可能传字符串
        self.dry_run = dry_run
        anti = dict(self.config.get("anti_bot", {}))
        # 引擎级代理校验（最终防线）：非法/占位符代理一律忽略，防止请求层报错
        if anti.get("proxy"):
            import re as _re
            _pm = _re.match(r"^https?://([^/@:]+(:[^/@:]+)?@)?([^/:]+):(\d+)$", str(anti["proxy"]))
            if not _pm or _pm.group(3) in ("host", "localhost", "example.com", "proxy"):
                self._bad_proxy = anti.pop("proxy", None)
            else:
                self._bad_proxy = None
        else:
            self._bad_proxy = None
        out_dir = Path(self.config.get("output", {}).get("dir", "outputs"))
        out_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir = out_dir
        anti["session_dir"] = str(out_dir / ".session")
        anti["captcha_dir"] = str(out_dir / ".captcha")
        anti["_block_stats"] = {}
        (out_dir / ".session").mkdir(parents=True, exist_ok=True)
        (out_dir / ".captcha").mkdir(parents=True, exist_ok=True)
        self.logger = Logger(log_file=log_file or (out_dir / f".run_{task.name}.log"))
        self._log_cb = log_cb
        anti["_log_cb"] = log_cb  # 供 fetcher（浏览器交互提示）回传 WebUI 进度
        # 🍪 自动会话：任务启动自动获取目标域名 cookie（默认 temp：用完即删）
        #   cookie_mode: temp(默认，任务结束删除) / persist(长期复用) / off(不用)
        self._cookie_domain = ""
        self._cookie_acquired: Dict[str, Any] = {}
        try:
            _su0 = (self.config.get("start_urls") or [""])[0]
            _domain = ""
            if "//" in str(_su0):
                from .cookies import _norm_domain
                _domain = _norm_domain(str(_su0).split("//")[-1].split("/")[0])
            if _domain:
                _cm = (anti.get("cookie_mode") or "temp").lower()
                anti["cookie_domain"] = _domain
                self._cookie_domain = _domain
                from .cookies import acquire_for_task
                _acq = acquire_for_task(_domain, port=int(anti.get("cookie_port") or 9222),
                                        mode=_cm, log=log_cb)
                self._cookie_acquired = _acq
                if _acq.get("source") in ("reused", "imported"):
                    if log_cb:
                        _n = _acq.get("count", 0)
                        if _acq.get("source") == "reused":
                            log_cb(f"🍪 已复用 {_domain} 的会话（{_n} 条 cookie，自动注入）")
                        else:
                            log_cb(f"🍪 已从调试 Chrome 自动获取 {_domain} 会话（{_n} 条，任务结束自动删除）")
                    # 存档会话由 HttpFetcher 播种进会话 jar（域名限定+按名合并），
                    # 不再静态注入 source.headers——静态头会永久压死服务端下发的
                    # 新 Cookie，长任务中途换令牌时反而掉登录。
        except Exception:
            pass
        self.anti = anti

        # 插件装配
        self.fetcher_cls = task.get_fetcher_cls()
        self.parser_cls_map = task.get_parsers()
        self.pipeline_cls = task.get_pipeline_cls()
        self.storage_cls = task.get_storage_cls()
        self.mw_cls = task.get_middleware_cls()
        _src_cfg = resolve_tpl(dict(self.config.get("source", {})), self.vars)
        _src_cfg["_task_dir"] = str(task.root)
        self.fetcher = self.fetcher_cls(_src_cfg, self.vars, anti)
        self.ctx = ParseContext(task, self.config, self.vars)

        # 存储
        store_cfg = dict(self.config.get("storage", {}))
        _sd = store_cfg.get("dir", "items")
        store_cfg["dir"] = str(_sd) if Path(_sd).is_absolute() else str(out_dir / _sd)
        self.storage_name = store_cfg.get("name", self.task.name)
        store_cfg["name"] = self.storage_name  # sqlite/multi 后端需要名字
        self.storage = self.storage_cls(store_cfg, self.vars)

        # 流水线
        self.pipeline = self.pipeline_cls(self.config.get("pipelines", []), self.vars)

        # 中间件：配置声明的内置中间件 + 任务自定义 middleware.py
        self.middlewares = []
        seen_actions = set()
        for spec in self.config.get("middleware", []):
            action = spec.get("action")
            cls = self._builtin_middleware(action)
            if cls and action not in seen_actions:
                seen_actions.add(action)
                self.middlewares.append(cls(spec, self.vars))
        if self.mw_cls:
            self.middlewares.append(self.mw_cls(self.config.get("middleware", {}), self.vars))

        # 队列
        queue_cfg = self.config.get("queue", {})
        self.queue = RequestQueue()
        self.max_depth = int(queue_cfg.get("max_depth", 10))
        self.max_requests = int(queue_cfg.get("max_requests", 10000))
        self.min_interval = float(anti.get("min_interval", 1.0))
        self.max_concurrency = int(queue_cfg.get("max_concurrency", 4))
        self.rules = self.config.get("rules", [])

        self.stats = {"fetched": 0, "items": 0, "errors": 0, "skipped": 0}
        self.resume = resume
        self.state_file = out_dir / f".state_{task.name}.json"
        self.pending_file = out_dir / f".pending_{task.name}.json"
        self._latencies: deque = deque(maxlen=20)
        self._lock = threading.Lock()
        self._stop = False
        self._all_items: List[Dict[str, Any]] = []
        # 内存 spool：大任务不把全部条目驻留内存（默认 5 万条后自动切磁盘读）
        out_cfg = self.config.get("output", {}) or {}
        self._spool_threshold = int(out_cfg.get("spool_threshold", 50000))
        self._spooling = False
        self._last_page_saved = False
        self._last_page_2_saved = False
        self._spool_start = 0
        self._spool_path: Optional[Path] = None
        try:
            # store_cfg["dir"] 已在上方被引擎绝对化（out_dir 已拼接），直接用即可
            self._spool_path = Path(str(store_cfg.get("dir", "items"))) / f"{self.storage_name}.jsonl"
        except Exception:
            self._spool_path = None
        # 失败重试队列：{url: attempts}
        self._retries: Dict[str, int] = {}
        self._pre_filter_items: list = []  # 管道过滤前快照（宽松回退用）
        self.max_retries = int(anti.get("max_retries", 2))
        self._active = 0
        # robots.txt 尊重（对标 Crawlee respectRobotsTxtFile / Scrapy）
        self.robots = None
        if anti.get("respect_robots"):
            from .robots import RobotsTxt
            self.robots = RobotsTxt(user_agent=anti.get("robots_ua", "universal-scraper/1.0"))
            self.logger.info("robots.txt 尊重已开启（Disallow 跳过 + Crawl-delay 限速）")

        # 增量去重（跨运行，对标 Crawlee RequestQueue seen + v2 SeenStore）
        inc = self.config.get("incremental", {}) or {}
        self._seen_store = None
        if inc.get("enabled"):
            from .storage import SeenStore
            self._seen_store = SeenStore(out_dir / f".seen_{task.name}.txt")
            self._inc_key = inc.get("key", "id")
            mode = "内容哈希(跨运行去重)" if self._inc_key == "content_hash" else f"key={self._inc_key}"
            self.logger.info(f"增量去重已开启（已见 {len(self._seen_store)} 条，{mode}）")

    def _notify(self, msg: str, level: str = "INFO") -> None:
        """统一进度通道：只回传回调（WebUI job.messages）。
        注意：调用方需自行写日志（logger.info/warn），避免同一条消息在日志文件出现两次。"""
        if self._log_cb:
            try:
                self._log_cb(msg)
            except Exception:
                pass

    @staticmethod
    def _builtin_middleware(action):
        from .modules.middleware import LogMiddleware, CaptchaMiddleware, NotifyMiddleware
        return {"log": LogMiddleware, "webhook": NotifyMiddleware, "captcha": CaptchaMiddleware}.get(action)

    # ---- 路由 ----
    def route_parser(self, url: str) -> str:
        for rule in self.rules:
            if _match_rule(rule, url):
                return rule.get("parser", "default")
        return "default"

    def _follow_allowed(self, url: str) -> bool:
        """Scrapy Rule.follow 语义：命中规则且 follow=false 的链接不入队。"""
        for rule in self.rules:
            if _match_rule(rule, url):
                return rule.get("follow", True) is not False
        return True

    def _robots_allowed(self, url: str) -> bool:
        if self.robots is None:
            return True
        return self.robots.allowed(url)

    def get_parser(self, name: str):
        cls = self.parser_cls_map.get(name)
        if cls is None:
            cls = self.parser_cls_map["default"]
        pcfg = self.config.get("parsers", {}).get(name, {})
        return cls(pcfg, self.vars)

    # ---- 单请求处理 ----
    def _handle(self, req: Request) -> None:
        if self._stop or (self.limit and self.stats["items"] >= self.limit):
            return
        t0 = time.time()
        try:
            for mw in self.middlewares:
                req = mw.on_request(req, self.ctx) or req
            resp = self.fetcher.fetch(req)
            # 浏览器渲染空页面（status=0 且无内容）：进入错误统计+重试路径，
            # 不把"渲染失败"混入成功抓取（此前伪造 200 导致 0 条被当成正常）
            if getattr(resp, "status", 0) == 0 and not (getattr(resp, "text", "") or "").strip() \
                    and not (getattr(resp, "body", b"") or b"").strip(b"\n\r\t "):
                raise RuntimeError(f"浏览器渲染空页面: {req.url}")
            for mw in self.middlewares:
                resp = mw.on_response(resp, self.ctx) or resp
            # 路由
            pname = self.route_parser(resp.url or req.url)
            parser = self.get_parser(pname)
            result = parser.parse(resp, self.ctx)
            # 🏆 精配兜底：AI 解析器 0 条或全是空壳（字段无值）但命中注册表站点
            # （期刊/门户/API 等）→ 用精配解析器。空壳判断防 AI 的 json 解析器
            # 没写 records_path 时把顶层对象当记录、生成一堆空字段"成功"条目跳过兜底。
            _META = ("_url", "_parser", "_ts", "_id")
            _real_items = [it for it in (result.items or []) if any(
                str(v or "").strip() for k, v in it.items() if k not in _META)]
            if not _real_items:
                try:
                    from .sites import parse_site_html
                    _rows = parse_site_html(resp.url or req.url, resp.text or "")
                    if _rows:
                        for _r in _rows:
                            _r.setdefault("_url", resp.url or req.url)
                            _r.setdefault("_parser", f"site:{_r.get('_site', '')}")
                        result.items = _rows
                except Exception:
                    pass
            # 真实内容页：保存渲染后的页面（供 LLM 兜底/自修复选择器，登录/JS 页必须用渲染结果）
            # 修复：不只存第一页——首页存 last_page.html，最近一个详情页存 last_page_2.html（轮换），
            # 自修复能看到"列表+详情"两种页面证据，避免只有首页结构
            if len(resp.text or "") > 2000:
                try:
                    _root = Path(self.task.root)
                    if not self._last_page_saved:
                        (_root / "last_page.html").write_text(resp.text, encoding="utf-8")
                        self._last_page_saved = True
                    elif self._last_page_2_saved:
                        # 已有一个详情页：轮换（保留较新的）
                        _other = _root / "last_page_2.html"
                        if _other.exists() and len(_other.read_text(encoding="utf-8", errors="replace")) < len(resp.text or ""):
                            _other.write_text(resp.text, encoding="utf-8")
                    else:
                        (_root / "last_page_2.html").write_text(resp.text, encoding="utf-8")
                        self._last_page_2_saved = True
                except Exception:
                    pass
            # 入队新请求（递归）：follow=false 规则 + robots.txt 过滤
            for new_req in result.requests:
                if not new_req.depth:
                    new_req.depth = req.depth + 1
                if not self._follow_allowed(new_req.url):
                    self.logger.info(f"跳过（follow=false）: {new_req.url}")
                    continue
                if not self._robots_allowed(new_req.url):
                    self.logger.info(f"跳过（robots.txt）: {new_req.url}")
                    continue
                self.queue.enqueue(new_req, self.min_interval, self.max_depth)
            # 流水线 + 存储
            for item in result.items:
                item.setdefault("_url", resp.url or req.url)
                item.setdefault("_parser", pname)
                for mw in self.middlewares:
                    item = mw.on_data(item, self.ctx)
                    if item is None:
                        break  # 中间件丢弃该 item
                if item is None:
                    continue
                if len(self._pre_filter_items) < 500:
                    self._pre_filter_items.append(item)
                item = self.pipeline.process(item)
                k = ""  # 必须初始化：未启用增量去重时 _seen_store 为 None，下面的 if k 分支不会执行
                if item is not None and self._seen_store is not None:
                    from .storage import record_key
                    k = record_key(item, self._inc_key)
                    if k and self._seen_store.is_seen(k):
                        continue
                    # 注意：mark 延迟到 storage.write 成功后——磁盘满/写入异常时
                    # 不能标记已见（否则重试后 is_seen 命中→数据永久丢失）
                if item is not None:
                    # storage.write 挪到锁外：sqlite/multi 每 item 提交不应阻塞所有 worker
                    try:
                        self.storage.write(item)
                    except OSError as e:
                        # 磁盘满等 IO 错误：致命（重试后 is_seen 会吞数据）
                        self.logger.error(f"存储写入失败（可能磁盘满）: {e}")
                        raise
                    if k and self._seen_store is not None:
                        self._seen_store.mark(k)  # 写成功才标记已见
                    with self._lock:
                        self.stats["items"] += 1
                        if len(self._all_items) < self._spool_threshold:
                            self._all_items.append(item)
                        elif not self._spooling:
                            self._spooling = True
                            self.logger.info(f"条目超过 {self._spool_threshold}，已切换为磁盘 spool（内存不再累计）")
            # 重试成功：按该请求的失败次数冲销错误（stats.errors 只算最终失败）
            with self._lock:
                _fails = self._retries.get(req.key(), 0)
                if _fails:
                    self.stats["errors"] = max(0, self.stats["errors"] - _fails)
                    self.logger.info(f"重试成功（冲销 {_fails} 错误）: {req.url}")
                if req.key() in self._retries:
                    del self._retries[req.key()]  # 清理，防长任务内存累计
        except Exception as e:
            from .protocols import PermanentFetchError
            if isinstance(e, PermanentFetchError):
                # 永久性 HTTP 错误（404/410…）：跳过不计错、不重试（重试无意义，还会拖慢任务）
                with self._lock:
                    self.stats["skipped"] += 1
                self.logger.warn(f"跳过（{e.status} 永久失败）: {req.url}")
                return
            with self._lock:
                self.stats["errors"] += 1
                attempts = self._retries.get(req.key(), 0) + 1
                self._retries[req.key()] = attempts
            for mw in self.middlewares:
                try:
                    mw.on_error(req, e, self.ctx)
                except Exception:
                    pass
            # 队列内重试（Crawlee 风格：带退避/Retry-After 延迟重新入队，由 worker 池并行补跑）
            self._schedule_retry(req, attempts, e)
            self.logger.warn(f"请求失败 {req.url}: {type(e).__name__}: {str(e)[:120]}")
        finally:
            with self._lock:
                self.stats["fetched"] += 1
                self._latencies.append(time.time() - t0)

    def _schedule_retry(self, req: Request, attempts: int, error: Exception) -> None:
        """把失败请求延迟重新入队（Crawlee 风格）。
        - 429 Retry-After：按服务端要求等待
        - 其它：指数退避 2^attempts（封顶 300s）
        """
        if attempts > self.max_retries:
            self.logger.warn(f"放弃重试 {req.url}（已达 {self.max_retries} 次）")
            return
        delay = min(2 ** attempts, 300)
        if isinstance(error, RateLimitedError) and error.retry_after:
            delay = min(max(float(error.retry_after), 0.5), 600)
        self.queue.enqueue_retry(req, retry_at=time.time() + delay)
        self.logger.info(f"延迟重试 {attempts}/{self.max_retries}: {req.url}（{delay:.0f}s 后）")

    # ---- 主循环 ----
    def run(self) -> Dict[str, Any]:
        # 任务运行锁：同名任务并发直接报错（先锁再跑，finally 必释放）
        _lock = _acquire_run_lock(Path(self.task.root))
        try:
            return self._run_locked()
        finally:
            try:
                _lock.unlink(missing_ok=True)
            except Exception:
                pass
            # 🍪 任务结束：temp 模式自动删除本次获取的 cookie（用完即删，不留隐私）
            if self._cookie_domain and self._cookie_acquired:
                try:
                    from .cookies import release_temp
                    if release_temp(self._cookie_domain, self._cookie_acquired):
                        if self._log_cb:
                            self._log_cb(f"🍪 任务结束，已自动删除临时会话（{self._cookie_domain}）")
                    elif (self._cookie_acquired.get("mode") == "temp"
                          and self._cookie_acquired.get("source") == "imported"):
                        # 该删而没删成：隐私删除失败必须可感知（登录态残留本地存档）
                        _msg = (f"⚠️ 临时会话 cookie 删除失败（{self._cookie_domain}），"
                                f"本次导入的登录态可能残留在本地存档，请手动检查 outputs/.cookies/")
                        self.logger.warn(_msg)
                        if self._log_cb:
                            self._log_cb(_msg)
                except Exception:
                    pass

    def _run_locked(self) -> Dict[str, Any]:
        # 桥/一次性取数：不走队列，直接 fetch_all → 流水线 → 存储
        if hasattr(self.fetcher, "fetch_all"):
            return self._run_fetch_all()

        # 断点续跑：先注入历史已抓 URL，再入队种子（避免种子重复抓）
        if self.resume and self.state_file.exists():
            try:
                state = json.loads(self.state_file.read_text(encoding="utf-8"))
                for u in state.get("urls", []):
                    self.queue.mark_seen(u)
                self.logger.info(f"断点续跑：已跳过 {state.get('urls', []) and len(state['urls'])} 个历史 URL")
            except Exception as e:
                self.logger.warn(f"断点状态加载失败: {e}")

        # --url 覆盖入口（快速重定向同一任务到新 URL）；{{var}} 模板解析
        seeds = [resolve_tpl(self.start_url, self.vars)] if self.start_url else [
            resolve_tpl(u, self.vars) for u in self.config.get("start_urls", [])]
        sm_url = None if self.start_url else self.config.get("source", {}).get("sitemap")
        if sm_url:
            from .core import make_http_client
            from .engine import fetch_sitemap_urls
            try:
                sm = make_http_client({"min_interval": 0.5, "timeout": 20, "http_backend": "auto"})
                seeds = fetch_sitemap_urls(sm, sm_url) + seeds
                self.logger.info(f"sitemap 种子: {len(seeds)} 个 URL")
            except Exception as e:
                self.logger.warn(f"sitemap 展开失败: {e}")

        # 种子：start_urls + 上次未完成队列（中断恢复）
        seeds = seeds or list(self.config.get("start_urls", []))
        if self.resume and self.pending_file.exists():
            try:
                seeds.extend(json.loads(self.pending_file.read_text(encoding="utf-8")))
                self.logger.info(f"断点续跑：恢复 {len(seeds) - len(self.config.get('start_urls', []))} 个未完成 URL")
            except Exception:
                pass
        for u in seeds:
            if not self._robots_allowed(u):
                self.logger.info(f"跳过种子（robots.txt）: {u}")
                continue
            self.queue.enqueue(Request(url=u, depth=0), self.min_interval, self.max_depth)
            if self.robots is not None:
                from urllib.parse import urlparse
                dom = urlparse(u).netloc
                cd = self.robots.crawl_delay(u)
                if cd:
                    self.queue.set_domain_interval(dom, max(self.min_interval, cd))
                    self.logger.info(f"robots Crawl-delay {dom}: {cd:.1f}s")
        if self.dry_run:
            self.logger.info(f"[dry-run] 队列种子 {len(self.queue)}，插件就绪: "
                             f"fetcher={self.fetcher_cls.__name__} parsers={list(self.parser_cls_map)}")
            return {"name": self.task.name, "dry_run": True}

        try:
            (Path(self.task.root) / ".stop").unlink(missing_ok=True)
        except Exception:
            pass
        if self._spool_path is not None and self._spool_path.exists():
            try:
                self._spool_start = self._spool_path.stat().st_size
            except Exception:
                self._spool_start = 0
        self.storage.open(self.storage_name)
        _start_msg = f"任务启动: {self.task.name} | 队列 {len(self.queue)} | 规则 {len(self.rules)}"
        self.logger.info(_start_msg)
        self._notify(_start_msg)
        try:
            self._run_pool()
        except KeyboardInterrupt:
            # 优雅中断（Ctrl+C）：保存未完成队列 + 尽力导出已抓数据，然后继续抛出
            self.logger.warn("收到中断，保存检查点并尽力导出已抓数据...")
            try:
                self._save_pending()
                try:
                    self.storage.close()   # 先 flush jsonl，spool 才能读到全部
                except Exception as e:
                    self.logger.warn(f"中断时存储关闭失败: {e}")
                self._finalize()
                if self._seen_store is not None:
                    try:
                        self._seen_store.flush()
                    except Exception:
                        pass
                self._save_state()
            except Exception as e:
                self.logger.warn(f"中断保存失败: {e}")
            raise
        finally:
            self.storage.close()
            self._save_pending()
        # 宽松回退：非日期过滤把全部数据滤成 0 时，恢复过滤前数据并警告（有数据总比 0 好）
        if self.stats["items"] == 0 and getattr(self, "_pre_filter_items", None):
            _pf = self._pre_filter_items
            _pcfg = self.config.get("pipelines") or []
            _has_strict = any(isinstance(p, dict) and p.get("type") == "filter"
                              and p.get("op") in ("contains", "non_empty", "not_contains", "regex")
                              for p in _pcfg)
            _has_between = any(isinstance(p, dict) and p.get("op") == "between" for p in _pcfg)
            if _has_strict and not _has_between and _pf:
                self.logger.warn(f"⚠️ 管道过滤把 {len(_pf)} 条全部滤成 0（多为 contains/non_empty 字段与真实数据不匹配），已放宽保留（最多 {self.limit or 50} 条）。如需精确过滤请改配置或加 detail 过滤")
                _pf = list(_pf)[: int(self.limit or 50)]
                self._all_items = _pf
                self.stats["items"] = len(_pf)
        for mw in self.middlewares:
            if hasattr(mw, "flush"):
                try:
                    mw.flush()
                except Exception:
                    pass
        # 增量去重批量写：任务结束必须 flush，否则 <flush_every 条的小任务已见记录不落盘
        if self._seen_store is not None:
            try:
                self._seen_store.flush()
            except Exception:
                pass
        if hasattr(self.fetcher, "close"):
            try:
                self.fetcher.close()
            except Exception:
                pass
        self._run_details()
        self._finalize()
        self._save_state()
        _done_msg = f"完成: 抓取 {self.stats['fetched']} | 条目 {self.stats['items']} | 错误 {self.stats['errors']}"
        if self.stats.get("skipped"):
            _done_msg += f" | 跳过 {self.stats['skipped']}"
        self.logger.info(_done_msg)
        self._notify(_done_msg)
        _drops = dict(getattr(self.pipeline, "dropped", {}) or {})
        _skips = dict(getattr(self.pipeline, "skipped", {}) or {})
        if _drops or _skips:
            _pd = "；".join(f"{k}={v}" for k, v in list(_drops.items()) + list(_skips.items()))
            _pm = f"流水线统计: {_pd}"
            self.logger.info(_pm)
            self._notify(_pm)
        blocks = dict(self.anti.get("_block_stats") or {})
        if blocks:
            from .antibot import block_summary
            _bs = f"反爬拦截统计: {block_summary(blocks)}"
            self.logger.info(_bs)
            self._notify(_bs)
        return {"name": self.task.name, "total": self.stats["items"],
                "fetched": self.stats["fetched"], "errors": self.stats["errors"],
                "skipped": self.stats.get("skipped", 0),
                "block_stats": blocks,
                "pipeline_drops": _drops, "pipeline_skips": _skips}

    def _run_pool(self) -> None:
        """长驻 worker 池：固定 max_concurrency 线程，持续 pop→handle（比每批建池更快）。"""
        workers = max(1, self.max_concurrency)
        threads = []
        for _ in range(workers):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

    def _run_fetch_all(self) -> Dict[str, Any]:
        """桥/一次性取数：fetch_all 拿原始记录 → 字段映射 → 流水线 → 存储。"""
        if self.dry_run:
            self.logger.info(f"[dry-run] 桥取数就绪: {self.fetcher_cls.__name__}")
            return {"name": self.task.name, "dry_run": True}
        raw = self.fetcher.fetch_all()
        self.logger.info(f"桥返回原始记录: {len(raw)}")
        fields = self.config.get("record", {}).get("fields", {})
        rows = [map_record(r, fields) for r in raw] if fields else list(raw)
        for r in rows:
            r["_parser"] = "bridge"
        kept = []
        marks = []          # batch2400 审查修复：mark 延迟到 storage.write 成功后
        for item in rows:
            item = self.pipeline.process(item)
            if item is None:
                continue
            if self._seen_store is not None:
                from .storage import record_key
                k = record_key(item, self._inc_key)
                if k and self._seen_store.is_seen(k):
                    continue
                if k:
                    marks.append(k)
            kept.append(item)
        self._all_items = kept
        self.stats["items"] = len(kept)
        self.storage.open(self.storage_name)
        for item in kept:
            self.storage.write(item)
        self.storage.close()
        # 写盘全部成功后才标记已见（写失败 → 下次重跑不会被去重吞掉）
        if self._seen_store is not None:
            from .storage import record_key
            fields = self.config.get("record", {}).get("fields", {})
            for item in kept:
                k = record_key(item, self._inc_key)
                if k:
                    self._seen_store.mark(k)
        self._finalize()
        try:
            urls = sorted({str(r.get("_url", r.get("url", ""))) for r in kept if r.get("_url") or r.get("url")})
            self.state_file.write_text(json.dumps({"urls": urls, "items": len(urls)}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        self.logger.info(f"完成: 条目 {len(kept)} | 错误 0")
        return {"name": self.task.name, "total": len(kept), "fetched": len(raw), "errors": 0}

    def _save_pending(self) -> None:
        """周期保存未完成队列（SIGKILL 也只丢最近 N 条）。"""
        try:
            with self._lock:
                pending = self.queue.snapshot_urls()  # 持锁快照，防与 worker pop 竞态
                # 原子替换：多 worker 并发时不写坏断点文件（损坏即静默丢未完成队列）
                _tmp = self.pending_file.with_suffix(".json.tmp")
                _tmp.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
                import os as _os
                _os.replace(_tmp, self.pending_file)
        except Exception:
            pass

    def _save_state(self) -> None:
        """保存已抓 URL 集合（合并历史，供 --resume 跳过）。"""
        try:
            old_urls = set()
            if self.state_file.exists():
                try:
                    old_urls = set(json.loads(self.state_file.read_text(encoding="utf-8")).get("urls", []))
                except Exception:
                    old_urls = set()
            urls = sorted(old_urls | {str(r.get("_url", "")) for r in self._all_items if r.get("_url")})
            self.state_file.write_text(json.dumps({"urls": urls, "items": len(urls)}, ensure_ascii=False),
                                       encoding="utf-8")
        except Exception:
            pass

    def _worker_loop(self) -> None:
        stop_flag = Path(self.task.root) / ".stop"
        while not self._stop:
            # WebUI 一键停止：任务目录出现 .stop 文件即优雅退出（保存检查点）
            if stop_flag.exists():
                self._stop = True
                self.logger.warn("收到停止信号（.stop），正在保存检查点并退出...")
                break
            if self.limit and self.stats["items"] >= self.limit:
                break
            if self.stats["fetched"] >= self.max_requests:
                break
            req = self.queue.pop()
            if req is None:
                # 有延迟重试：睡到最早重试时间再继续
                retry_at = self.queue.min_retry_at()
                if retry_at is not None:
                    wait = min(max(retry_at - time.time(), 0.05), 5)
                    time.sleep(wait)
                    continue
                # 队列空：等其它 worker 可能产出的新请求；全部空闲且队列空才退出
                with self._lock:
                    idle = (len(self.queue) == 0 and self._active == 0)
                if idle:
                    break
                time.sleep(0.3)
                continue
            with self._lock:
                self._active += 1
            try:
                self._handle(req)
            except KeyboardInterrupt:
                # 停止信号：浏览器桥主动抛 KeyboardInterrupt 中断本轮，属预期行为，不打堆栈
                self.logger.warn("任务停止信号已生效，本轮请求中断")
            finally:
                with self._lock:
                    self._active -= 1
                if self.stats["fetched"] % 25 == 0:
                    _p = f"进度: 抓取 {self.stats['fetched']} | 条目 {self.stats['items']} | 队列 {len(self.queue)}"
                    self.logger.info(_p)
                    self._notify(_p)
                if self.stats["fetched"] % 10 == 0:
                    self._save_pending()

    def _run_details(self) -> None:
        """详情补抓（列表+详情合并，v2 detail 配置在 v3 原生实现）：
        列表页字段缺失（发布日期/正文/价格等）时，按 url_field 逐个抓详情页，
        extract 提取字段合并回列表记录，再按 detail.filters 过滤（如日期区间）。
        复用当前 fetcher（保留登录态/会话）。"""
        detail = self.config.get("detail") or {}
        if not detail.get("enabled"):
            return
        from .protocols import Request
        rows = list(self._all_items)
        if self._spooling:
            try:
                store_cfg = self.config.get("storage", {}) or {}
                _sd = store_cfg.get("dir", "items")
                _dir = Path(_sd) if Path(str(_sd)).is_absolute() else self.out_dir / str(_sd)
                _sp = _dir / f"{self.storage_name}.jsonl"
                if _sp.exists():
                    rows = [json.loads(l) for l in
                            _sp.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
                    self._spool_total_hint = len(rows)  # 供写回时防子集覆盖全量
            except Exception as e:
                self.logger.warn(f"详情：spool 读取失败（{e}），退回内存数据")
        if not rows:
            return
        url_field = detail.get("url_field", "url")
        extract = detail.get("extract", []) or []
        max_pages = int(detail.get("max_pages", 0)) or len(rows)
        concurrency = max(1, int(detail.get("concurrency", 2)))
        interval = float(detail.get("interval", 0.5))
        # 交互浏览器（login/verify/headless=false）一次只能一个窗口：详情优先用浏览器池
        # （复用已保存登录态 session.json，可并发），池失败再回退交互串行，避免多进程抢
        # 同一 profile 互相把对方浏览器关掉（Boss直聘 75 详情页实测只成功 3 页的根因）
        pool_fetcher = None
        try:
            from .modules.fetchers import BrowserFetcher
            _src0 = self.config.get("source") or {}
            _interactive = bool((_src0.get("login") or {}).get("enabled")
                                or (_src0.get("verify") or {}).get("enabled")
                                or _src0.get("headless") is False)
            if isinstance(self.fetcher, BrowserFetcher) and _interactive:
                _src2 = dict(_src0)
                _src2.pop("login", None)
                _src2.pop("verify", None)
                _src2["pool"] = True
                _src2["headless"] = True
                _src2["scroll_count"] = int(_src2.get("scroll_count", 0))
                _src2["scroll_wait_ms"] = int(_src2.get("scroll_wait_ms", 2000))
                pool_fetcher = BrowserFetcher(_src2, self.vars, self.anti)
                self.logger.info(f"详情：交互模式改用浏览器池并发（{concurrency}），失败页自动回退交互串行")
        except Exception as e:
            self.logger.warn(f"详情：浏览器池初始化失败（{e}），退回原取数器")
        todo, seen = [], set()
        _base = (rows[0].get("_url") or (self.config.get("start_urls") or [""])[0] or "") if rows else ""
        for r in rows:
            u = str(r.get(url_field) or "").strip()
            # 🛡️ 先验伪链接（在 prefix 之前）：javascript:/mailto:/#/data: 等直接跳过，
            # 否则会被 prefix 拼成 "https://hostjavascript:;" 变成合法 https 伪装
            if not u or u.startswith(("javascript:", "mailto:", "tel:", "data:", "#", "about:")):
                continue
            for tr in detail.get("url_transform", []) or []:
                if "replace" in tr:
                    u = u.replace(tr["replace"][0], tr["replace"][1])
                elif "prefix" in tr:
                    # 🛡️ 绝对链接不拼前缀：防止 https://host + https://host/... 双域名
                    if not u.startswith(("http://", "https://")):
                        u = tr["prefix"] + u
                elif "suffix" in tr:
                    u = u + tr["suffix"]
            # 🛡️ 无效/伪链接防线：javascript:; / # / mailto 等必须跳过（二次校验，
            # 防止 replace 变换把伪链接拼进 https 里），相对路径按列表页 URL 补全
            u = (u or "").strip()
            if not u or "javascript:" in u or u.startswith(("mailto:", "tel:", "data:", "#", "about:")):
                continue
            if not u.startswith(("http://", "https://")):
                # urljoin 支持无斜杠相对路径（detail/x.html、show.php?id=1 等），
                # 只要有 _base 就能正确补全——此前丢弃这些链接导致详情字段静默缺失
                if _base.startswith(("http://", "https://")):
                    from urllib.parse import urljoin
                    u = urljoin(_base, u)
                else:
                    continue
            if u in seen:
                continue
            seen.add(u)
            todo.append((u, r))
            if len(todo) >= max_pages:
                break
        if not todo:
            if pool_fetcher is not None:
                try:
                    pool_fetcher.close()
                except Exception:
                    pass
            return
        self.logger.info(f"详情：共 {len(rows)} 条，待抓 {len(todo)}（并发 {concurrency}，字段缺失自动补全）")
        self._notify(f"📄 详情补抓：{len(todo)} 个详情页（字段缺失自动补全）")

        import concurrent.futures as _cf
        ok = 0

        def _work(u, r):
            try:
                try:
                    if pool_fetcher is not None:
                        resp = pool_fetcher.fetch(Request(url=u))
                    else:
                        resp = self.fetcher.fetch(Request(url=u))
                except Exception:
                    # 池失败（详情页触发验证/登录墙等）→ 回退原交互取数器保底
                    resp = self.fetcher.fetch(Request(url=u))
                html = resp.text or ""
                for spec in extract:
                    _n = spec.get("name", "detail")
                    _v = ""
                    try:
                        _v = apply_extractor(spec, html, html, None)
                    except Exception:
                        _v = ""
                    # 兜底：AI 选择器漏了也能按字段名猜（publish_time→.time/.date 等）
                    if not str(_v or "").strip():
                        try:
                            from lxml import html as _lh
                            _doc = _lh.fromstring(html)
                            from .modules.parsers import ConfigParser
                            _v = ConfigParser._guess_field(_doc, _n)
                        except Exception:
                            _v = ""
                    r[_n] = _v
                r["detail_status"] = str(resp.status)
                return True
            except Exception as e:
                r["detail_status"] = f"ERR:{type(e).__name__}"
                self.logger.warn(f"详情失败 {u}: {type(e).__name__}: {str(e)[:100]}")
                return False

        def _stop_pending():
            try:
                return self._stop or (Path(self.task.root) / ".stop").exists()
            except Exception:
                return False

        with _cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = [ex.submit(_work, u, r) for u, r in todo]
            for i, f in enumerate(_cf.as_completed(futs), 1):
                if _stop_pending():
                    # 停止/超时：取消剩余详情（避免孤儿线程继续抓几十页）
                    for _f in futs:
                        _f.cancel()
                    self.logger.warn("收到停止信号，详情补抓提前结束")
                    break
                try:
                    if f.result():
                        ok += 1
                except Exception:
                    pass
                if i % 10 == 0 or i == len(futs):
                    self.logger.info(f"详情进度: {i}/{len(todo)}")
                if interval:
                    time.sleep(interval / concurrency)

        # 详情后过滤（如按发布日期区间）：对合并后的记录再跑 detail.filters
        filters = detail.get("filters") or []
        if filters:
            from .modules.pipelines import Pipeline
            fp = Pipeline(filters, self.vars)
            before = len(rows)
            kept = []
            for r in rows:
                out = fp.process(r)
                if out is not None:
                    kept.append(out)
            rows = kept
            self.logger.info(f"详情过滤：{before} -> {len(kept)} 条（{url_field} 详情合并后按条件保留）")

        # 写回内存 / storage jsonl（导出、spool、auto 的 sample 都读最新合并结果）
        self._all_items = rows
        try:
            store_cfg = self.config.get("storage", {}) or {}
            if store_cfg.get("type", "jsonl") in ("jsonl", "multi"):
                # spooling 且本次 rows 来自内存子集（spool 读回失败退回）时，
                # 禁止用子集覆盖全量磁盘文件——那会销毁已落盘的记录
                if self._spooling and len(rows) < getattr(self, "_spool_total_hint", 0):
                    self.logger.warn(f"详情：spool 读取不完整（{len(rows)} < 磁盘全量），"
                                     "跳过 storage 写回以防覆盖")
                elif self._spooling:
                    # 覆写后文件内容已含全量合并结果：重置 spool_start 防止
                    # _finalize 按旧偏移读回时错位丢行
                    self._spool_start = 0
                else:
                    _sd = store_cfg.get("dir", "items")
                    _dir = Path(_sd) if Path(str(_sd)).is_absolute() else self.out_dir / str(_sd)
                    _sp = _dir / f"{self.storage_name}.jsonl"
                    _sp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                                   encoding="utf-8")
        except Exception as e:
            self.logger.warn(f"详情：storage 写回失败（{e}）")
        with self._lock:
            self.stats["items"] = len(rows)
        # 🛡️ 用完必关：残留浏览器池会占会话锁/资源，导致下一个任务卡死（曾实测 17:05 的 pool 残留到 18:30）
        if pool_fetcher is not None:
            try:
                pool_fetcher.close()
            except Exception:
                pass
        self.logger.info(f"详情完成：成功 {ok}/{len(todo)}，记录 {len(rows)} 条")
        self._notify(f"✅ 详情补抓完成：{ok}/{len(todo)}，字段已合并")

    def _finalize(self) -> None:
        """把 JSONL/CSV 汇总导出为标准 json/csv/xlsx（与 v2 一致）。resume 时合并历史。"""
        from .core import export_rows
        rows = self._all_items
        if self._spooling:
            # 从 jsonl 全量读回（内存不驻留，导出时才读）
            try:
                store_cfg = self.config.get("storage", {}) or {}
                _sd = store_cfg.get("dir", "items")
                _dir = Path(_sd) if Path(str(_sd)).is_absolute() else self.out_dir / str(_sd)
                _sp = _dir / f"{self.storage_name}.jsonl"
                if _sp.exists():
                    loaded = []
                    data = _sp.read_bytes()
                    tail = data[self._spool_start:]
                    for line in tail.decode("utf-8", "ignore").splitlines():
                        try:
                            loaded.append(json.loads(line))
                        except Exception:
                            continue
                    rows = loaded
                    self.logger.info(f"spool：从 {_sp} 读回本次 {len(loaded)} 条用于导出")
                else:
                    self.logger.warn("spool 已开启但找不到 jsonl（storage 非 jsonl 时 spool 不生效，回退内存数据）")
            except Exception as e:
                self.logger.warn(f"spool 读取失败，退回内存数据: {e}")
        base = self.config.get("output", {}).get("base_name", self.task.name)
        # resume：合并之前已导出的记录（按 _url 去重），保证输出完整
        prev_json = self.out_dir / f"{base}.json"
        if self.resume and prev_json.exists():
            try:
                old = json.loads(prev_json.read_text(encoding="utf-8"))
                if not isinstance(old, list):
                    raise ValueError("历史导出不是 JSON 数组")
                seen = {r.get("_url") for r in rows if r.get("_url")}
                for r in old:
                    if r.get("_url") and r["_url"] not in seen:
                        rows.append(r)
                self.logger.info(f"导出合并历史 {len(old)} 条 -> 共 {len(rows)} 条")
            except Exception as e:
                # 历史导出读不出来（损坏/格式变）时绝不能只用本次记录覆盖旧导出：
                # 改写 <base>.new.*，旧导出原样保留
                self.logger.warn(f"resume 合并失败：读 {prev_json.name} 异常（{e}），"
                                 f"本次导出改写为 {base}.new.*（不覆盖旧导出）")
                base = f"{base}.new"
        if rows:
            paths = export_rows(rows, self.out_dir, base)
            self.logger.info("导出: " + ", ".join(f"{k}={v.name}" for k, v in paths.items()))


def _acquire_run_lock(task_dir: Path) -> Optional[Path]:
    """对任务目录加运行锁（O_EXCL + PID）：同名任务并发时第二个直接报错，防文件互踩。
    进程崩溃后锁文件残留：读 PID 判断进程是否存活，死了就接管。"""
    import os
    lock = Path(task_dir) / ".running.lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return lock
    except FileExistsError:
        try:
            pid = int((lock.read_text(encoding="utf-8") or "").strip())
            os.kill(pid, 0)  # 存活则抛 PermissionError/成功
            raise RuntimeError(f"任务目录正在被另一个任务使用（{lock}，pid={pid}）")
        except (ProcessLookupError, ValueError):
            try:
                lock.unlink()
            except Exception:
                pass
            return _acquire_run_lock(task_dir)
        except PermissionError:
            raise RuntimeError(f"任务目录正在被另一个任务使用（{lock}）")
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError(f"任务目录正在被另一个任务使用（{lock}）")


def run_task(task_path: Path, overrides=None, limit=None, resume=False,
             dry_run=False, log_file=None, start_url=None, log_cb=None) -> Dict[str, Any]:
    task = Task(task_path)
    return EngineV3(task, overrides=overrides, limit=limit, resume=resume,
                    dry_run=dry_run, log_file=log_file, start_url=start_url,
                    log_cb=log_cb).run()
