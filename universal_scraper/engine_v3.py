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
from .core import MaxRequestsExceeded, request_budget, safe_fname  # 请求预算硬闸（BaseException，worker 线程也要认得）；
# request_budget 必须模块级——曾分支局部导入，成功路径（分支未执行）UnboundLocalError 必崩
from .protocols import BlockDetectedError  # 封禁终态：worker 重试网必须放行（见 _handle 守卫）


_map_record_warned = set()   # 已告警过的坏模板 (字段名, template)——每个只 WARN 一次

# 进程级任务串行锁（审查 P2）：请求预算是进程全局，同进程并发 run() 会互抢预算
_ENGINE_RUN_LOCK = threading.Lock()


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
        self.logger = Logger(log_file=log_file or (out_dir / f".run_{safe_fname(task.name)}.log"))
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
                if _cm == "off":
                    # R91 修复（P2）：cookie_mode=off 曾仍设 cookie_domain——fetcher
                    # 只看该键播种存档 cookie，"off = 不用 cookie"形同虚设
                    if log_cb:
                        log_cb("🍪 cookie_mode=off：本次任务不注入任何登录态 cookie")
                else:
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
                    # 死会话/过期登录态的告警由 acquire_for_task 内部发出（它在恢复
                    # 尝试失败后才喊，且覆盖所有调用方），此处不重复播报。
                    # 审查修复（P1）：无存档且自动导入失败时错误只藏在 dict 里，且
                    # 告警曾以 log_cb 存在为前提——CLI 四条路径都不传 log_cb，等于
                    # 永远静默。现在无条件写日志（stderr + 运行日志），log_cb 只是加发
                    if _acq.get("source") == "none" and _acq.get("error"):
                        _m = f"⚠️ 会话自动获取失败，本次未登录: {_acq['error']}"
                        self.logger.warn(_m)
                        if log_cb:
                            log_cb(_m)
        except Exception as e:
            # 审查修复（P2）：曾 except Exception: pass——outputs/ 不可写等
            # 环境故障会让整个会话子系统静默失效，任务以未登录状态裸跑且无任何输出
            try:
                self.logger.warn(f"cookie 会话获取异常（本次将以未登录状态请求）: "
                                 f"{type(e).__name__}: {e}")
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
        _sd = (store_cfg.get("dir") or "items")
        store_cfg["dir"] = str(_sd) if Path(_sd).is_absolute() else str(out_dir / _sd)
        self.storage_name = safe_fname(store_cfg.get("name", self.task.name))  # R91：净化防路径逃逸
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
            elif not cls and not self.mw_cls:
                # R94 修复（P2）：未知 action 曾静默丢弃——webhook 通知类中间件
                # 配错字（webhok）后"该报的警没报"。无自定义 middleware.py 兜底时必须喊
                self.logger.warn(f"⚠️ 未知中间件 action '{action}'（无任务 modules/middleware.py 兜底）——该声明不生效")
        if self.mw_cls:
            self.middlewares.append(self.mw_cls(self.config.get("middleware", {}), self.vars))

        # 队列
        queue_cfg = self.config.get("queue", {})
        self.queue = RequestQueue()
        # R101 新能力：分布式去重/共享前沿（queue.backend=redis 时启用）。
        # 仅做跨进程共享的"已见 + 待抓 URL"，本地统计/限速不受影响；
        # 未配置或连接失败时 self.dist = None（连接失败在 get_backend 内显式报错）
        self.dist = None
        try:
            from .queue_backend import get_backend
            # R105 修复（P1）：默认命名空间按任务名派生——曾恒为 "us"，两个不同
            # 任务共用同一 Redis 时互偷对方 URL（跨解析器取数 → 垃圾数据+预算烧穿）。
            # 显式 namespace 仍作同任务多机的共享口径
            _default_ns = f"us:{safe_fname(self.task.name)}"
            self.dist = get_backend(queue_cfg, default_ns=_default_ns)
            if self.dist is not None:
                self.logger.info(f"🕸️ 分布式去重已启用（namespace={queue_cfg.get('namespace') or _default_ns}）")
        except Exception as e:
            if queue_cfg.get("backend") == "redis":
                # 显式配置了 redis 才报错——未配置的静默为 None 是正常路径
                self.logger.warn(f"⚠️ 分布式队列初始化失败，回落本地队列: {e}")
        self.max_depth = int(queue_cfg.get("max_depth", 10))
        self.max_requests = int(queue_cfg.get("max_requests", 10000))
        self.min_interval = float(anti.get("min_interval", 1.0))
        self.max_concurrency = int(queue_cfg.get("max_concurrency", 4))
        self.rules = self.config.get("rules", [])

        self.stats = {"fetched": 0, "items": 0, "errors": 0, "skipped": 0}
        self._budget_tripped = False  # worker 线程触发硬闸的旗标（主线程统一 re-raise）
        self.resume = resume
        self.state_file = out_dir / f".state_{safe_fname(task.name)}.json"
        self.pending_file = out_dir / f".pending_{safe_fname(task.name)}.json"
        self._latencies: deque = deque(maxlen=20)
        # R101 新能力：run 时序指标（对标 Crawlee Platform run 统计）——
        # 每页完成追加一个采样点，落 <out>/.metrics.json 供 webui /api/metrics 画图。
        # 收官十三轮（审查 L）：曾用 list 无界累积（落盘只裁 5000）——max_requests=0
        # 的不限任务跑百万页时内存线性涨（实测约 190B/页）。deque(maxlen) 同口径封顶
        import collections as _collections
        self._metrics_t0 = time.time()
        self._metrics = _collections.deque(
            [{"t": 0, "fetched": 0, "items": 0, "errors": 0, "qps": 0.0}], maxlen=5000)
        self._metrics_last_flush = 0.0
        # 收官十三轮（审查 M）：本run已完成的请求键（method|url|params|body），
        # 供 _save_state / --resume 与 Request.key() 同口径对齐
        self._done_req_keys: set = set()
        # 本轮产生的 _url 集合（详情+spool 回写后按归属过滤导出用，见 _finalize）
        self._run_urls: set = set()
        self._spool_rewritten = False
        self._lock = threading.Lock()
        self._stop = False
        self._all_items: List[Dict[str, Any]] = []
        # 内存 spool：大任务不把全部条目驻留内存（默认 5 万条后自动切磁盘读）
        out_cfg = self.config.get("output", {}) or {}
        self._spool_threshold = int(out_cfg.get("spool_threshold", 50000))
        # 收官十三轮（审查 M2，实测）：spool 模式的回退导出依赖 <out>/items/<name>.jsonl
        # ——storage 用 sqlite/csv/自定义后端时不产生该文件，一旦条目超过阈值，
        # _all_items 只剩前 threshold 条，json/csv/xlsx **导出被静默截断**（100 万条
        # 任务只导出 5 万条）而 run() 报全量 total。非 jsonl 后端禁用 spool 截断
        _stype = str(store_cfg.get("type") or "jsonl").lower()
        if _stype != "jsonl" and self._spool_threshold < 10 ** 12:
            self.logger.warn("storage 非 jsonl 后端——spool 截断会导致导出不全，"
                             "已禁用截断（内存按 delta 全量保存）；超大任务建议改 jsonl 后端")
            self._spool_threshold = 10 ** 12
        self._spooling = False
        self._last_page_saved = False
        self._last_page_2_saved = False
        self._spool_start = 0
        self._spool_path: Optional[Path] = None
        try:
            # store_cfg["dir"] 已在上方被引擎绝对化（out_dir 已拼接），直接用即可
            self._spool_path = Path(str((store_cfg.get("dir") or "items"))) / f"{self.storage_name}.jsonl"
        except Exception:
            self._spool_path = None
        # 失败重试队列：{url: attempts}
        self._retries: Dict[str, int] = {}
        self._pre_filter_items: list = []  # 管道过滤前快照（宽松回退用）
        self.max_retries = int(anti.get("max_retries", 2))
        self._active = 0
        # 饱和度自动刹车（Scrapy CLOSESPIDER_PAGECOUNT_NO_ITEM 心思 + Crawl4AI
        # "够了就停"的简化版）：连续 N 页 0 新增条目 → 自动停（尾页/翻页失效/
        # 软封锁都不该烧到 max_requests 上限）
        self._no_new_streak = 0
        self._no_new_limit = int((self.config.get("queue", {}) or {}).get("no_new_stop", 12))
        self._dedupe_skip_since_new = 0   # 距上次新增以来的增量去重命中数（刹车信息用）
        self._blocked_tripped = False     # 自定义取数器抛 BlockDetectedError 的旗标
        self._checkpoint_warned = False   # 检查点写盘失败只 WARN 一次（防刷屏）
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
            self._seen_store = SeenStore(out_dir / f".seen_{safe_fname(task.name)}.txt")
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
            # 审查修复 P1：已 pop 的请求不能凭空消失——用 enqueue_retry 回队
            # （enqueue 会被 _seen 去重拒绝），否则 --resume 时该 URL 永久丢失
            with self._lock:
                self.queue.enqueue_retry(req, req.meta.get("retry_at"))
            return
        t0 = time.time()
        try:
            for mw in self.middlewares:
                req = mw.on_request(req, self.ctx) or req
            # 插件钩子（R4 修复）：process_request——文档承诺自动生效但从未接线。
            # 返回 None = 丢弃该请求（跳过抓取不计错）
            try:
                from .plugins import apply_request_hooks
                # 审查八轮（MEDIUM）：插件契约声明 req 含 headers 且可改，
                # 但这里曾硬传 "headers": {} 且只在 url 变化时重建——插件注入的
                # Authorization/UA/代理头**永远不生效**（抓取照常"成功"但没带该头）。
                # 现在传真实 headers/body/method，四个字段任一变化都重建 Request。
                _preq = apply_request_hooks({"url": req.url, "method": req.method,
                                             "headers": dict(req.headers or {}),
                                             "body": req.body})
                if _preq is None:
                    self.logger.info(f"插件丢弃请求: {req.url}")
                    return
                _new_headers = _preq.get("headers") if isinstance(_preq.get("headers"), dict) else None
                if (_preq.get("url") and _preq["url"] != req.url
                        or _preq.get("method", req.method) != req.method
                        or _new_headers is not None and _new_headers != (req.headers or {})
                        or _preq.get("body", req.body) != req.body):
                    req = Request(url=_preq.get("url") or req.url,
                                  parser=req.parser,
                                  method=(_preq.get("method") or req.method),
                                  headers=_new_headers if _new_headers is not None else req.headers,
                                  body=_preq.get("body", req.body), depth=req.depth, meta=req.meta)
            except ImportError:
                pass
            resp = self.fetcher.fetch(req)
            # 浏览器渲染空页面（status=0 且无内容）：进入错误统计+重试路径，
            # 不把"渲染失败"混入成功抓取（此前伪造 200 导致 0 条被当成正常）
            if getattr(resp, "status", 0) == 0 and not (getattr(resp, "text", "") or "").strip() \
                    and not (getattr(resp, "body", b"") or b"").strip(b"\n\r\t "):
                raise RuntimeError(f"浏览器渲染空页面: {req.url}")
            for mw in self.middlewares:
                resp = mw.on_response(resp, self.ctx) or resp
            # 插件钩子（R4 修复）：process_response——文档承诺自动生效但从未接线
            # 审查八轮（MEDIUM）：此前只有 text 变化才重建，且 headers 恒传 {}——
            # 插件改 status/body 被静默丢弃（契约说可改）。现在 status/text/body 任一
            # 变化都重建（headers 字段名保留以兼容既有插件读写）。
            try:
                from .plugins import apply_response_hooks
                from .protocols import Response
                _presp = apply_response_hooks({"status": resp.status, "text": resp.text or "",
                                              "headers": {}, "body": resp.body})
                _chg = (_presp.get("status", resp.status) != resp.status
                        or (_presp.get("text") is not None and _presp["text"] != (resp.text or ""))
                        or (_presp.get("body") is not None and _presp["body"] != resp.body))
                if _chg:
                    resp = Response(request=req, status=_presp.get("status", resp.status),
                                   body=_presp.get("body", resp.body),
                                   text=(_presp["text"] if _presp.get("text") is not None else (resp.text or "")),
                                   json=None, url=resp.url)
            except ImportError:
                pass
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
                except Exception as e:
                    # 审查修复（P1）：兜底解析器崩溃曾被静默吞掉——随后 nodata
                    # 提示会误诊成"软封锁"，把用户引去查网络而不是查解析器
                    self.logger.warn(f"精配兜底解析失败（{req.url}）: {type(e).__name__}: {e}")
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
            # 入队新请求（递归）：follow=false 规则 + robots.txt 过滤。
            # R101：分布式模式下新 URL 推入共享前沿，本机 pop 空时从 Redis 拉取
            for new_req in result.requests:
                if not new_req.depth:
                    new_req.depth = req.depth + 1
                if not self._follow_allowed(new_req.url):
                    self.logger.info(f"跳过（follow=false）: {new_req.url}")
                    continue
                if not self._robots_allowed(new_req.url):
                    self.logger.info(f"跳过（robots.txt）: {new_req.url}")
                    continue
                if self.dist is not None:
                    # R101 修复（P2）：dist 模式也必须执行 max_depth（曾绕过）
                    if new_req.depth and new_req.depth > self.max_depth:
                        continue
                    try:
                        # R101 修复（P1）：seen 口径对齐 Request.key()（method|url|params）
                        # ——裸 URL 口径曾把分页 next 请求误判已见，翻页止步第 1 页
                        _dkey = new_req.key()
                        if self.dist.is_seen(self.storage_name, _dkey):
                            self.logger.info(f"跳过（分布式已见）: {new_req.url}")
                            continue
                        # R101 修复（P1）：完整序列化请求（method/params/page）——
                        # 曾只存 url/depth，分页请求弹回后退化成第 1 页死循环。
                        # 收官十三轮（审查 M）：body 也曾漏序列化——Request.key 含
                        # body（queue.snapshot_urls 已补），dist 路径丢了 body，
                        # worker 还原出 body=None 的 POST（实际发无体请求且计成功）
                        self.dist.push({"url": new_req.url, "depth": new_req.depth,
                                        "method": new_req.method,
                                        "params": new_req.meta.get("params"),
                                        "page": new_req.meta.get("page"),
                                        "body": new_req.body})
                        continue  # 共享队列持有该 URL——本机 pop 空时会取回
                    except Exception as e:
                        # Redis 抖动不丢数据：退回本地入队
                        self.logger.warn(f"分布式入队失败，退回本地: {e}")
                self.queue.enqueue(new_req, self.min_interval, self.max_depth)
            # 流水线 + 存储
            new_items = 0
            for item in result.items:
                item.setdefault("_url", resp.url or req.url)
                item.setdefault("_parser", pname)
                for mw in self.middlewares:
                    # R43 修复：on_data 曾无 per-middleware 守卫——webhook 挂掉
                    # 抛的异常会被当请求失败重试，数据被毁、exit 0 翻成 exit 3
                    # （on_error/flush 均有同款守卫，唯这里漏）
                    try:
                        item = mw.on_data(item, self.ctx)
                    except Exception as e:
                        self.logger.warn(f"中间件 on_data 执行失败"
                                         f"（{type(mw).__name__}）: {type(e).__name__}: {e}")
                        continue
                    if item is None:
                        break  # 中间件丢弃该 item
                if item is None:
                    continue
                if len(self._pre_filter_items) < 500:
                    self._pre_filter_items.append(item)
                try:
                    item = self.pipeline.process(item)
                except Exception as e:
                    # R129 修复（P1）：对齐 _run_fetch_all 的 R94——队列路径单条
                    # 管线异常曾穿透成整页失败：该页剩余 items 全部弃置不落盘、
                    # 整页按 max_retries 重试（预算白烧）；管线确定性坏时整页
                    # 条目永久丢失。降级为跳过该条并限频告警
                    self._pipe_item_warned = getattr(self, "_pipe_item_warned", 0) + 1
                    if self._pipe_item_warned <= 3:
                        self.logger.warn(f"⚠️ 管线处理失败，跳过该条（{type(e).__name__}: {e}）")
                    continue
                # 插件钩子（P0-B）：用户自定义条目处理（plugins/process_item）
                if item is not None:
                    from .plugins import apply_item_hooks
                    item = apply_item_hooks(item)
                k = ""  # 必须初始化：未启用增量去重时 _seen_store 为 None，下面的 if k 分支不会执行
                _reserved = False
                if item is not None and self._seen_store is not None:
                    from .storage import record_key
                    k = record_key(item, self._inc_key)
                    # 审查八轮（MEDIUM）：列表键各项全缺失时 record_key 返回 "|"
                    # （非空真值）——不同记录被判重复，只留第一条、其余静默丢弃。
                    # 兄弟实现（modules/pipelines.py:209、engine.py:135）都有
                    # "任一键为空即保留不判重"的守卫，此处缺；补齐（宁重不漏）。
                    _kvals = [item.get(kk) for kk in (self._inc_key if isinstance(self._inc_key, list) else [self._inc_key])]
                    if any(v in (None, "") for v in _kvals):
                        k = ""
                    # 两段式去重（R6 审查 P1）：reserve 原子"查询+占位"——旧
                    # is_seen/mark 分离曾让 4 个 worker 并发把同一条目各写一份
                    if k and not self._seen_store.reserve(k):
                        with self._lock:
                            self._dedupe_skip_since_new += 1
                        continue
                    _reserved = bool(k)
                    # 注意：commit 延迟到 storage.write 成功后——磁盘满/写入异常时
                    # 回滚占位（否则重试后 reserve 命中→数据永久丢失）
                if item is not None:
                    # 饱和度判定只认"真实条目"（全空壳 item 是选择器没匹配上的
                    # 副产物，不该让刹车失效——gsxt 200 空壳页每页都产出空壳）
                    # OCR R131（C）：生成器变量曾用 k 遮蔽上方 record_key 的 k——
                    # commit(k) 提交的是条目最后一个字段名，去重占位永不释放
                    if any(str(v or "").strip() for _fk, v in item.items()
                           if _fk not in ("_url", "_parser", "_ts", "_id")):
                        new_items += 1
                    # storage.write 挪到锁外：sqlite/multi 每 item 提交不应阻塞所有 worker。
                    # OSError（磁盘满）是终态：自己接住并停机——若 re-raise 会被下方
                    # except Exception 当普通失败重试（审查实证：满盘后继续烧预算）
                    try:
                        self.storage.write(item)
                    except OSError as e:
                        if _reserved and self._seen_store is not None:
                            self._seen_store.rollback(k)
                        self.logger.error(f"⛔ 存储写入失败（磁盘满/IO 错误）——任务硬停机"
                                          f"防数据丢失: {e}")
                        self._stop = True
                        return
                    except Exception as e:
                        # 审查修复（P2）：sqlite 满盘抛 OperationalError（非 OSError）
                        # 曾被当普通请求失败重试——每条记录烧一次预算直到队列耗尽
                        import sqlite3 as _sq
                        if isinstance(e, _sq.Error):
                            if _reserved and self._seen_store is not None:
                                self._seen_store.rollback(k)
                            self.logger.error(f"⛔ 存储写入失败（sqlite/磁盘满）——任务硬停机"
                                              f"防数据丢失: {e}")
                            self._stop = True
                            return
                        if _reserved and self._seen_store is not None:
                            self._seen_store.rollback(k)
                        raise
                    if k and self._seen_store is not None:
                        self._seen_store.commit(k)  # 写成功才把占位转正为已见
                    with self._lock:
                        self.stats["items"] += 1
                        # R78 修正（P2）：limit 曾只在 worker 顶部无锁检查——C 个
                        # worker 可在任一计数前全部通过检查，--limit 超发 C-1 页；
                        # 计数后同锁内复查并停机
                        if self.limit and self.stats["items"] >= self.limit and not self._stop:
                            self._stop = True
                            self.logger.info(f"已达 limit={self.limit}，停止取数")
                        if len(self._all_items) < self._spool_threshold:
                            self._all_items.append(item)
                        elif not self._spooling:
                            self._spooling = True
                            self.logger.info(f"条目超过 {self._spool_threshold}，已切换为磁盘 spool（内存不再累计）")
                        # 收官十三轮（审查 M）：登记本轮 _url（spool 回写后过滤历史行用）。
                        # 收官十五轮修复：此处**已在 self._lock 内**（上方 stats/limit
                        # 块），再套一层 `with self._lock` 会自死锁（threading.Lock
                        # 不可重入）——每个条目写入即挂死整个任务
                        _ru = str(item.get("_url") or "")
                        if _ru:
                            self._run_urls.add(_ru)
            # R102：整页处理成功后统一标记分布式已见（一次，页级口径）——
            # 曾挂在 per-item/增量键上的两种写法分别漏"0 条幸存页"和默认配置
            # 收官十三轮（审查 M，实测）：本地 resume 也按**请求键**记账。
            # state 曾存响应 URL（_url）→ 带 params/重定向的请求键永不命中，
            # --resume 变全量重抓（日志却打"已跳过 N 个历史 URL"）；dist 路径
            # 早已用 req.key()，本地路径漏改
            with self._lock:
                self._done_req_keys.add(req.key())
            if self.dist is not None:
                try:
                    self.dist.mark(self.storage_name, req.key())
                    # R116 at-least-once：页成功 → ack 释放 processing 暂存；
                    # 不 ack（崩溃/异常）则 visibility_timeout 后自动重投
                    _dm = req.meta.get("dist_member")
                    if _dm:
                        self.dist.ack(_dm)
                except Exception:
                    pass
            # 重试成功：按该请求的失败次数冲销错误（stats.errors 只算最终失败）；
            # 饱和度计数必须在锁内更新（审查 P2：4 worker 并发读改写曾可能误停）
            with self._lock:
                if new_items > 0:
                    self._no_new_streak = 0
                    self._dedupe_skip_since_new = 0
                else:
                    self._no_new_streak += 1
            with self._lock:
                _fails = self._retries.get(req.key(), 0)
                if _fails:
                    self.stats["errors"] = max(0, self.stats["errors"] - _fails)
                    self.logger.info(f"重试成功（冲销 {_fails} 错误）: {req.url}")
                if req.key() in self._retries:
                    del self._retries[req.key()]  # 清理，防长任务内存累计
        except BlockDetectedError:
            # 审查保险丝：封禁判定是终态——绝不能落入下方 except Exception 被
            # 当普通失败重试（重试封禁站 = 二次事故）。当前 v3 取数器抛
            # RateLimitedError；此守卫防未来接入 v2 取数器时复燃
            raise
        except Exception as e:
            from .protocols import PermanentFetchError
            if isinstance(e, PermanentFetchError):
                # 永久性 HTTP 错误（404/410…）：跳过不计错、不重试（重试无意义，还会拖慢任务）
                with self._lock:
                    self.stats["skipped"] += 1
                # R117 修复（P3）：永久失败 ack 分布式暂存——URL 不会每个周期重投
                if self.dist is not None:
                    try:
                        _dm = req.meta.get("dist_member")
                        if _dm:
                            self.dist.ack(_dm)
                    except Exception:
                        pass
                self.logger.warn(f"跳过（{e.status} 永久失败）: {req.url}")
                return
            with self._lock:
                self.stats["errors"] += 1
                attempts = self._retries.get(req.key(), 0) + 1
                self._retries[req.key()] = attempts
            for mw in self.middlewares:
                try:
                    mw.on_error(req, e, self.ctx)
                except Exception as _mwe:
                    # 审查修复（P2）：用户配置的告警中间件（webhook）挂了必须可感知，
                    # 否则"该报的警没报 + 告警通道坏了也不知道"双重静默
                    self.logger.warn(f"中间件 on_error 执行失败"
                                     f"（{type(mw).__name__}）: {type(_mwe).__name__}: {_mwe}")
            # 队列内重试（Crawlee 风格：带退避/Retry-After 延迟重新入队，由 worker 池并行补跑）
            self._schedule_retry(req, attempts, e)
            self.logger.warn(f"请求失败 {req.url}: {type(e).__name__}: {str(e)[:120]}"
                             + (f" | {getattr(e, 'detail', '')}" if getattr(e, "detail", "") else ""))
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
            # R117 修复（P3）：终态放弃时 ack 分布式暂存——否则毒 URL 每个周期重投
            if self.dist is not None:
                try:
                    _dm = req.meta.get("dist_member")
                    if _dm:
                        self.dist.ack(_dm)
                except Exception:
                    pass
            # 审查修复 P2：记账回归——errors 曾按尝试次数累计（重试 2 次=errors+2），
            # 该 URL 最终只失败 1 次；放弃后清 _retries 键防长任务内存累计
            with self._lock:
                _n = self._retries.pop(req.key(), 0)
                self.stats["errors"] = max(0, self.stats["errors"] - max(0, _n - 1))
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
        # 请求预算是进程级全局（core._HTTP_REQUEST_STATS）：同进程并发跑两个
        # 任务会互相清零/抢额（WebUI 多 job 实证场景）——审查 P2 修复：同一
        # 进程内任务串行化。CLI 单任务无影响；礼貌限速本就是全局资源。
        # R10 复查修正：锁获取到 try 之间不能有可抛代码——曾让 int() 配置
        # 错误把锁永久挂在死线程上，进程内后续所有任务死锁
        _ENGINE_RUN_LOCK.acquire()
        try:
            # 任务级请求预算（NBS 考核战训：请求计数曾只读不写；硬闸 BaseException
            # 可穿透客户端 except-Exception 重试网，在此边界转译为正常返回）
            from .core import set_request_budget, request_budget, MaxRequestsExceeded
            set_request_budget(int(self.anti.get("max_requests") or 0))
            self.MaxRequestsExceeded = MaxRequestsExceeded  # worker 线程 except 用
            return self._run_locked()
        except MaxRequestsExceeded as e:
            # _run_locked 自身的 finally 已 flush/close 存储——这里补状态持久化与导出
            self.logger.error(f"🛑 {e}")
            try:
                # 审查修复 P1：曾漏 flush seen + _save_state——resume 会把
                # 已抓的全部重抓一遍（重复入库+重复烧预算）
                if self._seen_store is not None:
                    try:
                        self._seen_store.flush()
                    except Exception:
                        pass
                self._save_state()
            except Exception as e:
                # 审查修复（P1）：预算中断路径的保存失败曾静默——resume 会把
                # 已抓数据全部重抓而用户毫不知情
                self.logger.error(f"预算中断的检查点保存失败（resume 将重抓）: "
                                  f"{type(e).__name__}: {e}")
            try:
                self.fetcher.close()
            except Exception:
                pass
            try:
                self._finalize()
            except Exception as _fe:
                # 审查修复 P1：导出失败曾静默——"已按检查点停"不能暗示数据已安全
                self.logger.error(f"预算中断导出失败（部分数据可能未落盘）: "
                                  f"{type(_fe).__name__}: {_fe}")
            return {"name": self.task.name, "total": self.stats["items"],
                    "fetched": self.stats["fetched"], "errors": self.stats["errors"],
                    "stopped": "max_requests", "budget": request_budget()}
        finally:
            try:
                _lock.unlink(missing_ok=True)
            except Exception:
                pass
            finally:
                _ENGINE_RUN_LOCK.release()
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
                _n_key = _n_legacy = 0
                for u in state.get("urls", []):
                    # 收官十三轮（审查 M）：新格式存的是请求键（含 "|"，与
                    # Request.key() 同口径）；旧格式是裸响应 URL——两条路都支持
                    if "|" in str(u):
                        self.queue.mark_key(str(u))
                        _n_key += 1
                    else:
                        self.queue.mark_seen(str(u))
                        _n_legacy += 1
                self.logger.info(f"断点续跑：已跳过 {_n_key + _n_legacy} 个历史 URL"
                                 f"（请求键 {_n_key} / 旧格式 {_n_legacy}）")
            except Exception as e:
                self.logger.warn(f"断点状态加载失败: {e}")

        # --url 覆盖入口（快速重定向同一任务到新 URL）；{{var}} 模板解析。
        # 审查三轮（M）："start_urls": null 曾 TypeError（.get 默认值不覆盖显式 null）
        seeds = [resolve_tpl(self.start_url, self.vars)] if self.start_url else [
            resolve_tpl(u, self.vars) for u in (self.config.get("start_urls") or [])]
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
        # 审查八轮（MEDIUM）：`.get("start_urls", [])` 不覆盖**显式 null**——配置写
        # "start_urls": null 时 list(None) 直接 TypeError（sitemap 种子场景可达）。
        _su = self.config.get("start_urls") or []
        seeds = seeds or list(_su)
        if self.resume and self.pending_file.exists():
            try:
                seeds.extend(json.loads(self.pending_file.read_text(encoding="utf-8")))
                self.logger.info(f"断点续跑：恢复 {len(seeds) - len(_su)} 个未完成 URL")
            except Exception as e:
                # 审查修复（P2）：损坏的 pending 曾静默丢弃——用户 --resume 想恢复
                # 500 个 URL，实际只跑了种子，还毫无提示（.state 分支有告警，此处不对称）
                self.logger.warn(f"断点 pending 队列加载失败（已忽略，仅从种子续跑）: {e}")
        for s in seeds:
            # 审查修复（P1，R14）：pending 快照现在是结构化条目（含 method/
            # params）——重放时还原成带参请求，否则参数分页页被重放成裸 GET
            if isinstance(s, dict):
                u = s.get("url", "")
                if not u:
                    continue
                req = Request(url=u, method=s.get("method", "GET"), depth=0)
                if s.get("params"):
                    req.meta["params"] = s["params"]
                # 快照对称恢复 body（Request.key() 含 body，漏读会键漂移+丢体）
                if s.get("body"):
                    req.body = s["body"]
            else:
                u, req = s, Request(url=s, depth=0)
            if not self._robots_allowed(u):
                self.logger.info(f"跳过种子（robots.txt）: {u}")
                continue
            self.queue.enqueue(req, self.min_interval, self.max_depth)
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
            # 审查修复 P0：worker 线程触发的硬闸在主线程统一 re-raise，
            # 让 run() 的 except 分支导出部分数据并打上 stopped 标记（exit 4 契约）
            if getattr(self, "_budget_tripped", False):
                # request_budget 用模块级导入——这里的分支局部导入曾让
                # _run_locked 成功路径 UnboundLocalError（全量回归都测不到的必崩）
                raise MaxRequestsExceeded(
                    f"请求预算 {request_budget()['limit']} 已用尽（worker 汇报）")
            if getattr(self, "_blocked_tripped", False):
                # 封禁终态：主线程统一 re-raise → CLI exit 5 + blocked.json 证据
                raise BlockDetectedError("取数器报告封禁终态（worker 汇报）——已停止采集")
        except KeyboardInterrupt:
            # 优雅中断（Ctrl+C）：保存未完成队列 + 尽力导出已抓数据，然后继续抛出
            self.logger.warn("收到中断，保存检查点并尽力导出已抓数据...")
            # R33 修复：先置停机旗——否则 Daemon worker 还在跑时下方就关
            # fetcher/storage，后续写入打在已关句柄上变成错误风暴 + 重试雪崩
            self._stop = True
            self._drain_workers()  # R129 修复（P1）：等 in-flight handle 落盘再关句柄
            try:
                # 审查修复 P1：中断路径曾漏关 fetcher——浏览器池/会话锁残留，
                # 下一个同 profile 任务卡死（代码 1012 行注释自己写过这个坑）
                if hasattr(self.fetcher, "close"):
                    try:
                        self.fetcher.close()
                    except Exception:
                        pass
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
        except BlockDetectedError:
            # 审查修复（P1）：封禁终态曾跳过部分导出——阻断前已抓的数据也该落盘
            # （CLI 层随后 exit 5 + blocked.json）
            self.logger.warn("封禁终态：保存检查点并尽力导出已抓数据...")
            try:
                self._drain_workers()  # R129 修复（P1）：同中断路径——先等 in-flight 落盘
                if hasattr(self.fetcher, "close"):
                    try:
                        self.fetcher.close()
                    except Exception:
                        pass
                self._save_pending()
                try:
                    self.storage.close()
                except Exception as e:
                    self.logger.warn(f"封禁停机时存储关闭失败: {e}")
                self._finalize()
                if self._seen_store is not None:
                    try:
                        self._seen_store.flush()
                    except Exception:
                        pass
                self._save_state()
            except Exception as e:
                self.logger.warn(f"封禁停机保存失败: {e}")
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
                except Exception as e:
                    # 审查修复（P2）：批量通知等 flush 失败曾全静默——用户配置的
                    # 告警通道坏了必须可感知
                    self.logger.warn(f"中间件 flush 执行失败"
                                     f"（{type(mw).__name__}）: {type(e).__name__}: {e}")
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
        # 审查八轮（HIGH）：_run_details 曾裸调用且位于 try/finally 之外——详情阶段
        # 任一异常（坏 detail.filters 步骤、Pipeline 构造失败…）会穿透到 CLI，
        # 使 _finalize/_save_state 全部跳过：已抓到的列表数据不导出，输出目录只剩
        # items/（无 json/csv/xlsx、无 state）。改为"尽力收尾 + 如实上抛"。
        try:
            self._run_details()
        except BaseException as _de:
            self.logger.error(f"⚠️ 详情阶段异常（{type(_de).__name__}: {_de}）"
                              "——先尽力导出已抓数据，再上抛")
            try:
                self._finalize()
            except Exception as _fe:
                self.logger.error(f"⚠️ 异常后的兜底导出也失败: {type(_fe).__name__}: {_fe}")
            try:
                self._save_state()
            except Exception:
                pass
            raise
        self._finalize()
        self._save_state()
        self._flush_metrics()  # R101：收尾补最后一个采样点（短任务也有完整时序）
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
        # 代理池战况（深度改进①）：直连回退次数必须让用户看见——裸奔要可审计
        _pp = getattr(self.fetcher, "proxy_pool", None)
        if _pp is not None and getattr(_pp, "size", 0):
            _psum = f"代理池: {_pp.summary()}"
            self.logger.info(_psum)
            self._notify(_psum)
        elif self.anti.get("proxies") and _pp is None:
            # 审查修复（P2）：bridge/scrapling/自定义取数器不消费代理池——
            # 配置了代理却全程直连，必须明说而不是静默忽略
            _pw = (f"⚠️ anti_bot.proxies 配置了 {len(self.anti['proxies'])} 个代理，"
                   f"但当前取数器（{self.fetcher_cls.__name__}）不支持代理池——本次全程直连")
            self.logger.warn(_pw)
            self._notify(_pw)
        # NBS 考核战训：0 条必须留证据 + 标记（此前不落盘不报错，exit 0 假成功，
        # 排查只能靠 FileNotFoundError）
        if self.stats["items"] == 0 and not getattr(self, "dry_run", False):
            _nodata_written = False
            try:
                from .core import request_budget as _rq
                _ev = {"nodata": True, "name": self.task.name,
                       "total": 0, "fetched": self.stats["fetched"],
                       "errors": self.stats["errors"],
                       "source_url": (self.config.get("source") or {}).get("url", ""),
                       "budget": _rq(), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                # gsxt 战训（2026-09）：软封锁嫌疑诊断——有请求、无拦截指纹、却 0 条，
                # 最常见形态是"HTTP 200 空壳"（行为评分封锁）。没有 block_stats 时
                # 必须把这条最贵的诊断线索写进证据，别让排查从零开始
                _hints: list = []
                if self.stats["fetched"] > 0 and not blocks and self.stats["errors"] == 0:
                    _hints.append("HTTP 200 空壳/软封锁嫌疑：请求均 200 且无拦截指纹但 0 条"
                                  "——行为评分型封锁常见形态。处方: `budget --probe <入口URL> "
                                  "--expect <站名标记>` 探放行窗口；或换真浏览器通道（见 playbook 第三章）")
                    # Tier3 结构空壳实证（Crawl4AI 哲学）：last_page.html 能判形就给铁证
                    try:
                        _lp = Path(self.task.root) / "last_page.html"
                        if _lp.exists():
                            from .antibot import looks_like_empty_shell
                            if looks_like_empty_shell(_lp.read_text(encoding="utf-8", errors="replace")[:50000]):
                                _hints.append("已实锤: last_page.html 结构完整性命中（渲染/正文为空壳）"
                                              "——SPA 站处方: jsrecon→capture2config 接口路线")
                    except Exception:
                        pass
                if _hints:
                    _ev["hints"] = _hints
                (Path(self.out_dir) / "nodata.json").write_text(
                    json.dumps(_ev, ensure_ascii=False, indent=1), encoding="utf-8")
                _nodata_written = True
                self.logger.warn("⚠️ 0 条记录——已写 nodata.json 留证（0 结果不假成功），"
                                 "请按铁律 3 出诊断说明")
                for _h in _hints:
                    self.logger.warn(f"💡 诊断提示: {_h}")
            except Exception as e:
                # 审查修复 P1：写盘失败曾静默——结论须可溯源到日志
                self.logger.error(f"nodata.json 写盘失败（{type(e).__name__}: {e}）——"
                                  f"0 条结论请直接引用本日志行作证据")
            self._nodata_written = _nodata_written
        return {"name": self.task.name, "total": self.stats["items"],
                "fetched": self.stats["fetched"], "errors": self.stats["errors"],
                "skipped": self.stats.get("skipped", 0),
                "nodata": self.stats["items"] == 0,
                "budget": request_budget(),
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
        self._worker_threads = threads  # 停机路径 _drain_workers 需要
        for t in threads:
            t.join()

    def _drain_workers(self, timeout: float = 30.0) -> None:
        """R129 修复（P1）：停机路径先等 in-flight worker 退出，再关 fetcher/
        storage——_stop 旗只在下一轮循环顶生效，正在 _handle 里的 worker 对已关
        句柄的写入会被 storages 的 f=None 守卫静默丢弃，且这些已 pop 的 URL 既不
        在 pending 快照也不在 state urls，resume 出现永久缺口。"""
        for t in getattr(self, "_worker_threads", None) or []:
            t.join(timeout=timeout)

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
        _pipe_warned = 0
        for item in rows:
            # R94 修复（P2）：管线步骤异常曾炸掉整个 fetch_all 任务（抓完不落盘）
            try:
                item = self.pipeline.process(item)
            except Exception as e:
                _pipe_warned += 1
                if _pipe_warned <= 3:
                    self.logger.warn(f"⚠️ 管线处理失败，跳过该条（{type(e).__name__}: {e}）")
                continue
            if item is None:
                continue
            if self._seen_store is not None:
                from .storage import record_key
                k = record_key(item, self._inc_key)
                if k and self._seen_store.is_seen(k):
                    continue
                # OCR R131（L）：原 marks 列表只 append 从不消费（真 mark 在写盘
                # 成功后的循环）——删除遗留死代码
            kept.append(item)
        self._all_items = kept
        self.stats["items"] = len(kept)
        self.storage.open(self.storage_name)
        # OCR 终审（P1）：storage.write 裸调用——磁盘满/IO 错误时已抓数据全部丢失
        # （对齐队列路径 534-556 的守卫：OSError 硬停机 + finalize 导出已写部分）
        _write_err = None
        try:
            for _i, item in enumerate(kept):
                try:
                    self.storage.write(item)
                except OSError as e:
                    self.logger.error(f"💾 磁盘写入失败（OSError），已写 {_i} 条——硬停机保全已写数据: {e}")
                    _write_err = e
                    break
                except Exception as e:
                    # 审查八轮（HIGH）：sqlite 满盘/DB 锁定抛的是 OperationalError
                    # （不是 OSError）——桥路径曾把它归进"单条跳过继续"，于是每条都
                    # 失败仍跑完并返回 total=len(kept)/errors=0（全部未落盘却报成功，
                    # 实测 3 条只落 1 条仍报成功）。与队列路径同口径：sqlite 类故障
                    # 一律硬停机 + 抛出止损。
                    import sqlite3 as _sq
                    if isinstance(e, _sq.Error):
                        self.logger.error(f"⛔ 存储写入失败（sqlite/磁盘满），已写 {_i} 条"
                                          f"——硬停机保全已写数据: {e}")
                        _write_err = e
                        break
                    self.logger.error(f"⚠️ 单条写盘异常（跳过继续）: {type(e).__name__}: {e}")
                    continue
        finally:
            self.storage.close()
        if _write_err is not None:
            # 磁盘满等确定性故障：finalize 尽力导出 + re-raise 让上层止损
            self._finalize()
            raise _write_err
        # 写盘全部成功后才标记已见（写失败 → 下次重跑不会被去重吞掉）
        if self._seen_store is not None:
            from .storage import record_key
            # OCR R131（L）：原 fields 赋值从不被读取——删除死赋值
            for item in kept:
                k = record_key(item, self._inc_key)
                if k:
                    self._seen_store.mark(k)
            # 审查修复（P2）：桥路径从不 flush seen——缓冲（200 条）内的已见
            # 记录随进程蒸发，增量任务每次全量重抓
            try:
                self._seen_store.flush()
            except Exception as e:
                self.logger.warn(f"增量去重 flush 失败（bridge 路径）: {e}")
        # 0 条必须留证据（与队列路径同一契约：bridge 返回 dict 无 nodata 键时
        # CLI 的 exit-3 门恒假——0 条假成功曾在桥路径复活）
        if not kept:
            try:
                from .core import request_budget as _rq2
                _ev = {"nodata": True, "name": self.task.name,
                       "total": 0, "fetched": len(raw), "errors": 0,
                       "source_url": (self.config.get("source") or {}).get("url", ""),
                       "budget": _rq2(), "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                (self.out_dir / "nodata.json").write_text(
                    json.dumps(_ev, ensure_ascii=False, indent=1), encoding="utf-8")
                self.logger.warn("⚠️ 0 条记录——已写 nodata.json 留证（0 结果不假成功），"
                                 "请按铁律 3 出诊断说明")
            except Exception as e:
                self.logger.error(f"nodata.json 写盘失败（{type(e).__name__}: {e}）")
        self._finalize()
        try:
            urls = sorted({str(r.get("_url", r.get("url", ""))) for r in kept if r.get("_url") or r.get("url")})
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            # R33 修复：state 写盘原子化——兄弟实现（_save_pending/Checkpoint）都用
            # tmp+replace，进程死在写中间会留下损坏 JSON，resume 静默丢全部状态
            _tmp = self.state_file.with_suffix(".json.tmp")
            _tmp.write_text(json.dumps({"urls": urls, "items": len(urls)}, ensure_ascii=False), encoding="utf-8")
            import os as _os
            _os.replace(_tmp, self.state_file)
        except Exception:
            pass
        self.logger.info(f"完成: 条目 {len(kept)} | 错误 0")
        # R33 修复：桥路径 0 条曾缺 nodata 键——CLI 的 exit-3 门恒假（0 条假成功
        # 在桥路径复活）。与队列路径同一契约
        return {"name": self.task.name, "total": len(kept), "fetched": len(raw), "errors": 0,
                "nodata": not kept}

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
        except Exception as e:
            # 审查修复（P1）：检查点写盘失败曾全静默——outputs/ 满盘后 resume
            # 把已抓数据全部重抓而用户毫不知情。限频告警（每任务一次）
            if not self._checkpoint_warned:
                self._checkpoint_warned = True
                self.logger.warn(f"断点检查点保存失败（后续不再重复提示）: "
                                 f"{type(e).__name__}: {e}——--resume 将无法恢复本轮队列")

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
            # 收官十三轮（审查 M）：并入本轮已完成的**请求键**——resume 侧据此
            # 精确还原（mark_key），带 params/body 的请求才不会再抓一遍
            with self._lock:
                _keys = set(self._done_req_keys)
            try:
                _keys |= {str(r.get("_req_key", "")) for r in self._all_items if r.get("_req_key")}
            except Exception:
                pass
            urls = sorted(set(urls) | {k for k in _keys if k})
            # spool 模式下 _all_items 只有内存子集——已落盘的记录 URL 必须从
            # jsonl 补齐（审查 P2：否则 resume 对 3 万条已抓记录一无所知）
            if self._spooling and self._spool_path is not None and self._spool_path.exists():
                try:
                    with open(self._spool_path, "r", encoding="utf-8") as _f:
                        for _line in _f:
                            try:
                                _u = json.loads(_line).get("_url", "")
                            except Exception:
                                continue
                            if _u:
                                urls.append(str(_u))
                except OSError as e:
                    self.logger.warn(f"spool 读取失败（resume 去重不全）: {e}")
            urls = sorted(set(urls))
            # R106 修复（P2）：write_text 非原子——进程中途被杀留截断 JSON，
            # --resume 加载失败后退化为全量重爬。tmp+replace（与 _save_pending 同款）
            import os as _os
            _tmp = self.state_file.with_suffix(".json.tmp")
            _tmp.write_text(json.dumps({"urls": urls, "items": len(urls)}, ensure_ascii=False),
                            encoding="utf-8")
            _os.replace(_tmp, self.state_file)
        except Exception as e:
            # 审查修复（P1）：state 写盘失败曾全静默——resume 重复抓取无任何信号
            if not self._checkpoint_warned:
                self._checkpoint_warned = True
                self.logger.warn(f"已抓 URL 状态保存失败（后续不再重复提示）: "
                                 f"{type(e).__name__}: {e}——--resume 将重抓本轮全部页面")

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
            # 审查修复 P2：max_requests=0 应是"不限"（core 语义），曾 break 成空跑
            if self.max_requests and self.stats["fetched"] >= self.max_requests:
                break
            # 审查修复 P2：先占位再 pop——曾 pop 后才 _active += 1，字节码窗口内
            # 兄弟 worker 可能误判 idle 提前退出（并发退化）
            with self._lock:
                self._active += 1
            # OCR R131（H）：pop/dist 段任何异常曾带着 _active 泄漏——计数永不
            # 归零，兄弟 worker 误判"还有人在干"永不退出（任务挂死）
            req = None
            try:
                req = self.queue.pop()
                if req is None and self.dist is not None:
                    # R101：本地前沿空 → 从共享队列取回（分布式模式入队的新 URL 在那）
                    try:
                        _d = self.dist.pop(timeout=0.2)
                        if _d and _d.get("url"):
                            # R101 修复（P1）配套：还原完整请求（method/params/page），
                            # 否则分页请求弹回后退化为第 1 页死循环
                            _meta: Dict[str, Any] = {}
                            if _d.get("params") is not None:
                                _meta["params"] = _d["params"]
                            if _d.get("page") is not None:
                                _meta["page"] = _d["page"]
                            # R116 at-least-once：随行原文入 meta——页成功后 ack 释放；
                            # 不 ack 则 visibility_timeout 后自动重投
                            _meta["dist_member"] = _d.get("dist_member")
                            # 收官十三轮（审查 M）：body 回填——缺了它 POST 请求
                            # 还原成无体请求（Request.key 含 body，两边口径必须一致）
                            req = Request(url=_d["url"], depth=int(_d.get("depth") or 1),
                                          method=str(_d.get("method") or "GET"),
                                          body=_d.get("body"),
                                          meta=_meta)
                    except Exception:
                        pass  # Redis 抖动：按本地空处理，走既有 idle 逻辑
            except BaseException:
                with self._lock:
                    self._active -= 1
                raise
            if req is None:
                with self._lock:
                    self._active -= 1
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
            try:
                self._handle(req)
                # 饱和度自动刹车：连续 N 页 0 新增 → 停（防翻页失效烧穿预算）
                with self._lock:
                    _streak = self._no_new_streak
                if self._no_new_limit and _streak >= self._no_new_limit \
                        and not self._stop:
                    self._stop = True
                    with self._lock:
                        _dd = self._dedupe_skip_since_new
                    _m = (f"🛑 连续 {_streak} 页 0 新增条目——"
                          f"疑似到尾页/翻页失效/被软封锁，自动停止"
                          f"（queue.no_new_stop 可调，0=关闭）")
                    if _dd:
                        _m += f"。注意：其间有 {_dd} 条为增量去重命中（已见过的记录），并非封锁"
                    self.logger.warn(_m)
                    self._notify(_m)
            except KeyboardInterrupt:
                # 停止信号：浏览器桥主动抛 KeyboardInterrupt 中断本轮，属预期行为，不打堆栈
                self.logger.warn("任务停止信号已生效，本轮请求中断")
            except MaxRequestsExceeded:
                # 审查修复 P0：worker 线程里硬闸曾静默杀线程——run() 收不到信号，
                # --task 路径退化成 exit 0/假 nodata。置停机旗标，主线程统一 re-raise
                self._budget_tripped = True
                self._stop = True
                self.logger.error("🛑 请求预算硬闸触发（worker 线程），停止全部取数")
            except BlockDetectedError as _bde:
                # 审查修复（P1）：BlockDetectedError 是终态契约——曾直接杀死 worker
                # 线程：其余 worker 继续爬封禁站、任务以 exit 0 假成功收场
                self._blocked_tripped = True
                self._stop = True
                self.logger.error(f"⛔ 封禁终态触发，停止全部取数: {_bde}")
                self._notify(f"⛔ 检测到封禁终态，任务已停止: {_bde}")
            finally:
                with self._lock:
                    self._active -= 1
                if self.stats["fetched"] % 25 == 0:
                    _p = f"进度: 抓取 {self.stats['fetched']} | 条目 {self.stats['items']} | 队列 {len(self.queue)}"
                    self.logger.info(_p)
                    self._notify(_p)
                # R101：时序采样点（每页完成记一次）+ 节流落盘（≥5s 一次）
                with self._lock:
                    self._metrics.append({
                        "t": round(time.time() - self._metrics_t0, 1),
                        "fetched": self.stats["fetched"],
                        "items": self.stats["items"],
                        "errors": self.stats["errors"],
                        "qps": round(self.stats["fetched"] / max(0.1, time.time() - self._metrics_t0), 2)})
                    if self._metrics_last_flush == 0 or time.time() - self._metrics_last_flush >= 5:
                        self._metrics_last_flush = time.time()
                        self._flush_metrics()
                if self.stats["fetched"] % 10 == 0:
                    self._save_pending()

    def _flush_metrics(self) -> None:
        """时序指标落 <out>/.metrics.json（失败静默——指标绝不拖垮抓取）。"""
        try:
            p = self.out_dir / ".metrics.json"
            # deque 不支持切片（曾写 [-5000:]）——maxlen 已封顶，直接整体序列化
            p.write_text(json.dumps(list(self._metrics), ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

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
        _own_urls = {str(r.get("_url") or r.get("url") or "") for r in rows}  # R33：本轮行键
        spool_read_ok = False   # 审查修复（P1）：读失败必须显式标记——否则守卫把
        if self._spooling:      # 子集当"全量"，tmp+replace 会销毁盘上的多出的记录
            try:
                store_cfg = self.config.get("storage", {}) or {}
                _sd = (store_cfg.get("dir") or "items")
                _dir = Path(_sd) if Path(str(_sd)).is_absolute() else self.out_dir / str(_sd)
                _sp = _dir / f"{self.storage_name}.jsonl"
                if _sp.exists():
                    rows = [json.loads(l) for l in
                            _sp.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
                    self._spool_total_hint = len(rows)  # 供写回时防子集覆盖全量
                    spool_read_ok = True
            except Exception as e:
                self.logger.warn(f"详情：spool 读取失败（{e}），退回内存数据")
        self._spool_read_ok = spool_read_ok
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
                # R88：兼容文档写法 {"type": "prefix", "value": "..."} 与简写 {"prefix": "..."}
                if "type" in tr and "value" in tr:
                    tr = {tr["type"]: tr["value"]}
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
            # R90 修复（P1）：详情请求曾绕过 robots——种子与跟进入队都有闸，
            # 详情（通常是量最大的一类请求）却没有。opt-in 时同闸
            if not self._robots_allowed(u):
                self.logger.info(f"⛔ robots 禁止详情抓取，跳过: {u}")
                continue
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
                    # R33 修复：详情页空壳/验证码时提取为空——此前无条件 r[_n]=_v
                    # 会把列表页已有的非空值（如 publish_time）覆盖成 ""（字段级
                    # 数据丢失且 detail_status 还显示 200）。契约是"缺失才补全"
                    if str(_v or "").strip() or not str(r.get(_n) or "").strip():
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

        ex = _cf.ThreadPoolExecutor(max_workers=concurrency)
        try:
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
        except BaseException:
            # 硬闸(BaseException)穿透：取消排队详情，避免 with 退出排队假死
            ex.shutdown(wait=False, cancel_futures=True)
            # R33 修复：BaseException 路径曾漏关 pool_fetcher——浏览器池残留
            # （与正常路径 1325 的"用完必关"同一事故类）
            if pool_fetcher is not None:
                try:
                    pool_fetcher.close()
                except Exception:
                    pass
            raise
        ex.shutdown(wait=True)

        # 详情后过滤/后处理（如按发布日期区间、从详情文本派生新列）：对合并后的
        # 记录再跑一次完整管道。收官十五轮：新增 post_pipeline 键（与 v2 跨引擎
        # 统一；filters 保留为历史同义键——两者都走 Pipeline，任意步骤类型可用）
        filters = detail.get("post_pipeline") or detail.get("filters") or []
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
                # spooling 且读回失败（rows 是内存子集）时禁止覆盖磁盘全量——
                # 审查修复（P1）：旧守卫比较 hint（读失败时=0）形同虚设，
                # 一条坏 jsonl 行就能让 3 万条已落盘记录被 5 万条内存子集覆盖
                if self._spooling and not getattr(self, "_spool_read_ok", False):
                    self.logger.warn("详情：spool 读回失败，跳过 storage 写回以防"
                                     "子集覆盖全量（详情字段仅内存/导出可见）")
                elif self._spooling:
                    # 审查修复 P0：详情合并结果从未写回 spool——_finalize 按旧
                    # jsonl 读回，detail 字段与 detail.filters 全部静默丢失。
                    # 现在原子写回全量合并结果（tmp+rename 防半写截断），并重置
                    # spool_start 防止按旧偏移读回错位
                    _sp = self._spool_path
                    if _sp is None:
                        _sd = (store_cfg.get("dir") or "items")
                        _sp = (Path(_sd) if Path(str(_sd)).is_absolute()
                               else self.out_dir / str(_sd)) / f"{self.storage_name}.jsonl"
                    _tmp = _sp.with_suffix(".jsonl.tmp_detail")
                    _tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False, default=str)
                                              for r in rows) + "\n", encoding="utf-8")
                    _tmp.replace(_sp)
                    self._spool_start = 0
                    # 标记"整文件已被回写"——_finalize 需按本轮归属过滤历史行
                    self._spool_rewritten = True
                    self.logger.info(f"详情：合并结果已写回 spool（{len(rows)} 条）")
                else:
                    _sd = (store_cfg.get("dir") or "items")
                    _dir = Path(_sd) if Path(str(_sd)).is_absolute() else self.out_dir / str(_sd)
                    _sp = _dir / f"{self.storage_name}.jsonl"
                    # R33 修复（P1）：非 spool 路径跨运行合并——jsonl 是追加式跨运行
                    # 历史，旧代码用本次内存行整文件截断写回，--resume 场景把历史
                    # 记录清掉；且非原子写、缺 default=str。现读旧文件按 _url 合并
                    # （本次富化版本优先）后 tmp+replace 原子落盘
                    _cur_keys = {str(r.get("_url") or r.get("url") or "") for r in rows}
                    _merged = []
                    if _sp.exists():
                        for _line in _sp.read_text(encoding="utf-8", errors="ignore").splitlines():
                            if not _line.strip():
                                continue
                            try:
                                _old = json.loads(_line)
                            except Exception:
                                continue
                            if str(_old.get("_url") or _old.get("url") or "") in _cur_keys:
                                continue  # 本次已有富化版本，替换旧版
                            _merged.append(_old)
                    _merged.extend(rows)
                    _tmp = _sp.with_suffix(".jsonl.tmp_detail")
                    _tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False, default=str)
                                              for r in _merged) + "\n", encoding="utf-8")
                    _tmp.replace(_sp)
        except Exception as e:
            self.logger.warn(f"详情：storage 写回失败（{e}）")
        try:
            with self._lock:
                # R33 修复：spool 读回可能含历史运行记录——统计只计本次运行的行，
                # 防止 run 摘要 total 被 --resume 场景的历史行虚增
                if self._spooling:
                    self.stats["items"] = sum(1 for r in rows
                                              if str(r.get("_url") or r.get("url") or "") in _own_urls)
                else:
                    self.stats["items"] = len(rows)
        except Exception as e:
            # OCR R131（M）：统计段曾裸奔——它抛异常会跳过下方"用完必关"，
            # 浏览器池残留锁死下一个任务（统计损坏不该升级成资源泄漏）
            self.logger.warn(f"详情：统计更新失败（{type(e).__name__}: {e}）")
            if self._spooling:
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
                _sd = (store_cfg.get("dir") or "items")
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
                    # 收官十三轮（审查 M1，实测）：详情+spool 路径会把**整个追加式
                    # jsonl（含上一轮历史行）**读回、回写并把 _spool_start 重置为 0
                    # ——导出混入历史行（本轮 10 条却导出 20 条）。回写过后按
                    # "本轮产生的 _url"过滤，与偏移无关地保证只导出本轮数据
                    if getattr(self, "_spool_rewritten", False) and self._run_urls:
                        _before = len(loaded)
                        loaded = [r for r in loaded
                                  if str(r.get("_url") or r.get("url") or "") in self._run_urls]
                        if _before != len(loaded):
                            self.logger.info(f"spool：回写后按本轮归属过滤历史行 "
                                             f"{_before} -> {len(loaded)}")
                    rows = loaded
                    self.logger.info(f"spool：从 {_sp} 读回本次 {len(loaded)} 条用于导出")
                else:
                    self.logger.warn("spool 已开启但找不到 jsonl（storage 非 jsonl 时 spool 不生效，回退内存数据）")
            except Exception as e:
                self.logger.warn(f"spool 读取失败，退回内存数据: {e}")
        # 收官四轮（审查 M）：--limit=N 只停取数不断当前页——末页剩余条目照写
        # 全量导出（N=5 出 10 条、verify"声明 vs 文件"必 fail）。到达 limit 后
        # 在导出前截到 limit（用户意图=最多 N 条，不是"从第 N 条处开始的页全收"）
        if self.limit and len(rows) > self.limit:
            self.logger.info(f"limit={self.limit} 截取导出（捕获 {len(rows)} 条 → {self.limit} 条）")
            rows = rows[:self.limit]
        base = self.config.get("output", {}).get("base_name", safe_fname(self.task.name))
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


def _acquire_run_lock(task_dir: Path, _attempt: int = 0) -> Optional[Path]:
    """对任务目录加运行锁（O_EXCL + PID）：同名任务并发时第二个直接报错，防文件互踩。
    进程崩溃后锁文件残留：读 PID 判断进程是否存活，死了就接管。

    审查修复（P2）：① 拿到锁后写 PID 前存在空窗，第二个进程读到空文件会
    误判"死锁"并删掉活锁——接管前必须二次确认（重读非空 + PID 仍死）；
    ② 跨用户残留锁 unlink 失败曾静默递归 → RecursionError，现改为有界
    重试后抛出本意 RuntimeError。"""
    import os
    import time as _time
    lock = Path(task_dir) / ".running.lock"
    if _attempt >= 3:
        raise RuntimeError(f"任务目录运行锁竞争超限（{lock}）——请手动清理后重试")
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        # 二次确认锁仍在手上（防止刚才的空窗里被别的进程误删重抢）
        try:
            _owner = lock.read_text(encoding="utf-8").strip()
        except Exception:
            _owner = ""
        try:
            _owner_pid = int(_owner) if _owner else os.getpid()
        except ValueError:
            _owner_pid = os.getpid()
        if _owner_pid != os.getpid():
            return _acquire_run_lock(task_dir, _attempt + 1)  # 锁被别人接管，走竞争路径
        return lock
    except FileExistsError:
        try:
            _txt = (lock.read_text(encoding="utf-8") or "").strip()
        except Exception:
            _txt = ""
        if not _txt:
            # 锁文件为空 = 持有者还在写 PID 的空窗：短暂等待重试，绝不删活锁
            _time.sleep(0.2)
            return _acquire_run_lock(task_dir, _attempt + 1)
        try:
            pid = int(_txt)
            os.kill(pid, 0)  # 存活则抛 PermissionError/成功
            raise RuntimeError(f"任务目录正在被另一个任务使用（{lock}，pid={pid}）")
        except (ProcessLookupError, ValueError):
            try:
                lock.unlink()
            except PermissionError:
                raise RuntimeError(f"任务目录运行锁无法接管（权限不足，{lock}，pid={_txt}）"
                                   "——请手动清理或换任务目录")
            except OSError:
                pass
            return _acquire_run_lock(task_dir, _attempt + 1)
        except PermissionError:
            raise RuntimeError(f"任务目录正在被另一个任务使用（{lock}）")
        except RuntimeError:
            raise


def run_task(task_path: Path, overrides=None, limit=None, resume=False,
             dry_run=False, log_file=None, start_url=None, log_cb=None,
             max_requests: Optional[int] = None) -> Dict[str, Any]:
    task = Task(task_path)
    eng = EngineV3(task, overrides=overrides, limit=limit, resume=resume,
                   dry_run=dry_run, log_file=log_file, start_url=start_url,
                   log_cb=log_cb)
    if max_requests is not None:
        # 审查修复 P1：--task 路径此前静默忽略 --max-requests（help 承诺 exit 4）
        eng.anti["max_requests"] = int(max_requests)
    return eng.run()
