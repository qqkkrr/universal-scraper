#!/usr/bin/env python3
"""插件协议（v3 框架核心抽象）。

任务适配 = 只改/写对应模块：
  Fetcher    —— 怎么拿数据（HTTP / 浏览器 / 自定义协议）
  Parser     —— 怎么解析（HTML→items、JSON→items、自定义）
  Pipeline   —— 拿到 item 后怎么处理（清洗/去重/入库）
  Storage    —— 存到哪里（JSONL/CSV/XLSX/自定义）
  Middleware —— 请求前后钩子（限速/代理/日志/自定义）
  CaptchaSolver —— 验证码求解策略

内置模块见 universal_scraper/modules/，任务包可覆盖任一模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------- 数据对象

def _stable(obj: Any, _depth: int = 0) -> Any:
    """递归归一键值：set/frozenset 排序、dict 按键排序、深嵌套截断。

    收官九轮（审查）：`str(set)` 的顺序随 PYTHONHASHSEED 每进程变化（实测三次
    运行三个不同键）——跨进程 resume 时旧键永不命中，已抓页被重复抓取。"""
    if _depth > 10:                      # 循环引用/超深结构：截断为 repr，停止递归
        return repr(obj)
    if isinstance(obj, dict):
        return {str(k): _stable(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted((_stable(v, _depth + 1) for v in obj), key=repr)
    if isinstance(obj, (list, tuple)):
        return [_stable(v, _depth + 1) for v in obj]
    return obj


def _key_extra(params: Any) -> str:
    """params 的键后缀（排序序列化）。独立成函数：queue.mark_seen 与 Request.key
    必须共用同一实现，否则 resume 注入键与新请求键口径漂移（审查二轮 H）。"""
    if not params:
        return ""
    try:
        import json as _json
        return "|" + _json.dumps(_stable(params), sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "|" + str(_stable(params))


@dataclass
class Request:
    url: str
    parser: str = "default"          # 路由到哪个 parser
    depth: int = 0
    method: str = "GET"
    headers: Optional[Dict[str, str]] = None
    body: Optional[Dict[str, Any]] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        # 分页请求（meta.params）与普通请求区分，避免同 URL 下一页被去重
        extra = _key_extra(self.meta.get("params"))
        # OCR R131 二轮（H）：body 曾不参与键——POST 同 URL 不同体（翻页体/
        # 查询体）被误判重复直接丢弃（数据丢失级）。排序保证同体不同序同键
        if self.body:
            extra += _key_extra(self.body)
        # 审查五轮（LOW）：method 统一大写——mark_seen 侧已 .upper()，此处不同
        # 大写会让小写 method 的 resume 注入键永不命中（两端必须真正同构）
        return f"{self.method.upper()}|{self.url}{extra}"


@dataclass
class Response:
    request: Request
    status: int = 0
    body: bytes = b""
    text: str = ""
    json: Any = None
    url: str = ""
    # 收官十五轮（core 深审 H2）：响应是否被 max_size 截断。客户端层截断后
    # 调用方必须能看见（此前 fetcher 只比 len(body) > max_size——客户端已截，
    # 判据恒假、半截 JSON 被当完整数据）
    truncated: bool = False


@dataclass
class ParseResult:
    items: List[Dict[str, Any]] = field(default_factory=list)
    requests: List[Request] = field(default_factory=list)


class ParseContext:
    """parser 运行时的上下文：配置、任务变量、统计。"""
    def __init__(self, task: Any, config: Dict[str, Any], vars: Dict[str, str]):
        self.task = task
        self.config = config
        self.vars = vars


# ---------------------------------------------------------------- 插件基类

class BaseFetcher:
    """取数器：Request -> Response。"""
    name = "base"

    def __init__(self, config: Dict[str, Any], task_vars: Dict[str, str], anti: Dict[str, Any]):  # noqa: D107
        self.config = config
        self.vars = task_vars
        self.anti = anti

    def fetch(self, req: Request) -> Response:
        raise NotImplementedError


class BaseParser:
    """解析器：Response -> ParseResult(items, requests)。"""
    name = "default"

    def __init__(self, config: Dict[str, Any], task_vars: Dict[str, str]):  # noqa: D107
        self.config = config
        self.vars = task_vars

    def parse(self, resp: Response, ctx: ParseContext) -> ParseResult:
        raise NotImplementedError


class BasePipeline:
    """数据流水线：item 逐个处理，返回 item 或 None（丢弃）。"""
    name = "base"

    def __init__(self, config: Dict[str, Any], task_vars: Dict[str, str]):  # noqa: D107
        self.config = config
        self.vars = task_vars

    def process(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raise NotImplementedError


class BaseStorage:
    """存储后端：open/write/close。"""
    name = "base"

    def open(self, name: str) -> None:  # noqa: D102
        raise NotImplementedError

    def write(self, item: Dict[str, Any]) -> None:  # noqa: D102
        raise NotImplementedError

    def close(self) -> None:  # noqa: D102
        raise NotImplementedError


class BaseMiddleware:
    """中间件：on_request / on_response / on_data / on_error。"""
    name = "base"

    def __init__(self, config: Optional[Dict[str, Any]] = None, task_vars: Optional[Dict[str, str]] = None):
        self.config = config or {}
        self.vars = task_vars or {}

    def on_request(self, req: Request, ctx: ParseContext) -> Optional[Request]:
        return req

    def on_response(self, resp: Response, ctx: ParseContext) -> Optional[Response]:
        return resp

    def on_data(self, item: Dict[str, Any], ctx: ParseContext) -> Optional[Dict[str, Any]]:
        return item

    def on_error(self, req: Request, error: Exception, ctx: ParseContext) -> None:
        pass


class BaseCaptchaSolver:
    """验证码求解器。"""
    name = "base"

    def solve(self, image_file: str, **kw) -> Optional[str]:
        raise NotImplementedError


class PermanentFetchError(RuntimeError):
    """永久性 HTTP 错误（404/410 等客户端错误）：重试无意义，引擎应跳过而非计错。"""

    def __init__(self, url: str = "", status: int = 404, detail: str = ""):
        self.url = url
        self.status = int(status)
        self.detail = detail
        _msg = f"永久错误 {status}: {url}"
        if detail:
            _msg += f" | {detail}"
        super().__init__(_msg)


class RateLimitedError(RuntimeError):
    """服务端限流（429 等）。retry_after: 服务端要求等待的秒数（可 0）。"""

    def __init__(self, url: str = "", retry_after: float = 0.0, status: int = 429, detail: str = ""):
        self.url = url
        self.retry_after = float(retry_after or 0)
        self.status = status
        self.detail = detail
        _msg = f"限流 {status}: {url} (retry_after={self.retry_after}s)"
        if detail:
            _msg += f" | {detail}"
        super().__init__(_msg)


class BlockDetectedError(RuntimeError):
    """判定已被站点封禁/风控拦截（200 状态伪装的封禁页也算）。

    裁判文书网战训（2026-09 DeepSeek 考核）：93 字符封禁页被判成
    "页面太短→已下架"，继续抓了 1.3 万条全是封禁提示页。此类命中必须
    硬停机——宁可不写，也不能把封禁页写进数据。与 RateLimitedError
    （限流可冷却重试）语义不同：本异常 = 立即停止采集并落证据。"""

    def __init__(self, url: str = "", kind: str = "", detail: str = ""):
        self.url = url
        self.kind = kind or "blocked"
        self.detail = detail
        super().__init__(f"检测到封禁/风控拦截[{self.kind}]: {url} | {detail}")
