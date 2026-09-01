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
        extra = ""
        if self.meta.get("params"):
            import json as _json
            extra = "|" + _json.dumps(self.meta["params"], sort_keys=True, ensure_ascii=False)
        return f"{self.method}|{self.url}{extra}"


@dataclass
class Response:
    request: Request
    status: int = 0
    body: bytes = b""
    text: str = ""
    json: Any = None
    url: str = ""


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
