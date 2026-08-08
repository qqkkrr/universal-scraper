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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from .log import Logger
from .selectors import jpath, apply_extractor


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
                except Exception:
                    out[name] = ""
            elif "concat" in spec:
                out[name] = "".join(str(jpath(raw, pth, "")) for pth in spec["concat"])
            else:
                out[name] = jpath(raw, spec.get("path", ""), None)
    return out
from .queue import RequestQueue
from .protocols import ParseContext, Request, Response, RateLimitedError
from .task import Task
from .config import ConfigError


def resolve_tpl(value: Any, vars: Dict[str, str]) -> Any:
    """把配置里的 {{var}} 模板替换成任务变量（与 v2 _resolve_template 对齐）。"""
    if isinstance(value, str):
        return re.sub(r"\{\{(\w+)\}\}", lambda m: str(vars.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: resolve_tpl(v, vars) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_tpl(v, vars) for v in value]
    return value


def _match_rule(rule: Dict[str, Any], url: str) -> bool:
    m = rule.get("match", "regex")
    pat = rule.get("pattern", "")
    if m == "contains":
        return pat in url
    if m == "regex":
        try:
            return re.search(pat, url) is not None
        except re.error:
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
        self._spool_start = 0
        self._spool_path: Optional[Path] = None
        try:
            # store_cfg["dir"] 已在上方被引擎绝对化（out_dir 已拼接），直接用即可
            self._spool_path = Path(str(store_cfg.get("dir", "items"))) / f"{self.storage_name}.jsonl"
        except Exception:
            self._spool_path = None
        # 失败重试队列：{url: attempts}
        self._retries: Dict[str, int] = {}
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
            for mw in self.middlewares:
                resp = mw.on_response(resp, self.ctx) or resp
            # 路由
            pname = self.route_parser(resp.url or req.url)
            parser = self.get_parser(pname)
            result = parser.parse(resp, self.ctx)
            # 真实内容页：保存渲染后的页面（供 LLM 兜底/自修复选择器，登录/JS 页必须用渲染结果）
            if len(resp.text or "") > 2000 and not self._last_page_saved:
                try:
                    (Path(self.task.root) / "last_page.html").write_text(resp.text, encoding="utf-8")
                    self._last_page_saved = True
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
                item = self.pipeline.process(item)
                if item is not None and self._seen_store is not None:
                    from .storage import record_key
                    k = record_key(item, self._inc_key)
                    if k and self._seen_store.is_seen(k):
                        continue
                    if k:
                        self._seen_store.mark(k)
                if item is not None:
                    # storage.write 挪到锁外：sqlite/multi 每 item 提交不应阻塞所有 worker
                    self.storage.write(item)
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
        for item in rows:
            item = self.pipeline.process(item)
            if item is not None and self._seen_store is not None:
                from .storage import record_key
                k = record_key(item, self._inc_key)
                if k and self._seen_store.is_seen(k):
                    continue
                if k:
                    self._seen_store.mark(k)
            if item is not None:
                kept.append(item)
        self._all_items = kept
        self.stats["items"] = len(kept)
        self.storage.open(self.storage_name)
        for item in kept:
            self.storage.write(item)
        self.storage.close()
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
                pending = [r.url for r in self.queue._q]
            self.pending_file.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
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
            except Exception as e:
                self.logger.warn(f"详情：spool 读取失败（{e}），退回内存数据")
        if not rows:
            return
        url_field = detail.get("url_field", "url")
        extract = detail.get("extract", []) or []
        max_pages = int(detail.get("max_pages", 0)) or len(rows)
        concurrency = max(1, int(detail.get("concurrency", 2)))
        interval = float(detail.get("interval", 0.5))
        todo, seen = [], set()
        for r in rows:
            u = str(r.get(url_field) or "").strip()
            if not u:
                continue
            for tr in detail.get("url_transform", []) or []:
                if "replace" in tr:
                    u = u.replace(tr["replace"][0], tr["replace"][1])
                elif "prefix" in tr:
                    u = tr["prefix"] + u
                elif "suffix" in tr:
                    u = u + tr["suffix"]
            if not u or u in seen:
                continue
            seen.add(u)
            todo.append((u, r))
            if len(todo) >= max_pages:
                break
        if not todo:
            return
        self.logger.info(f"详情：共 {len(rows)} 条，待抓 {len(todo)}（并发 {concurrency}，字段缺失自动补全）")
        self._notify(f"📄 详情补抓：{len(todo)} 个详情页（字段缺失自动补全）")

        import concurrent.futures as _cf
        ok = 0

        def _work(u, r):
            try:
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

        with _cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = [ex.submit(_work, u, r) for u, r in todo]
            for i, f in enumerate(_cf.as_completed(futs), 1):
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
                _sd = store_cfg.get("dir", "items")
                _dir = Path(_sd) if Path(str(_sd)).is_absolute() else self.out_dir / str(_sd)
                _sp = _dir / f"{self.storage_name}.jsonl"
                _sp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                               encoding="utf-8")
        except Exception as e:
            self.logger.warn(f"详情：storage 写回失败（{e}）")
        with self._lock:
            self.stats["items"] = len(rows)
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
                if isinstance(old, list):
                    seen = {r.get("_url") for r in rows if r.get("_url")}
                    for r in old:
                        if r.get("_url") and r["_url"] not in seen:
                            rows.append(r)
                    self.logger.info(f"导出合并历史 {len(old)} 条 -> 共 {len(rows)} 条")
            except Exception:
                pass
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
