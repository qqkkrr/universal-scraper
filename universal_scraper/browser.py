#!/usr/bin/env python3
"""【兼容层】旧版浏览器桥 API。新代码请用配置驱动的 engine + fetchers.BrowserScriptFetcher。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List

from .fetchers import BrowserScriptFetcher


class CaptchaError(RuntimeError):
    pass


class BrowserBridgeError(RuntimeError):
    pass


def run_bridge(bridge_script: Path, args: Dict[str, str], timeout: int = 900) -> Iterator[Dict[str, Any]]:
    """兼容旧接口：流式产出 JSONL 对象（captcha/error 抛异常）。"""
    src = {"type": "browser_script", "bridge": str(bridge_script), "bridge_params": dict(args)}
    fetcher = BrowserScriptFetcher(src, {"captcha_dir": "/tmp/universal_scraper_captcha"}, {}, Path.cwd())
    for obj in fetcher._run(bridge_script, dict(args)):
        if obj.get("type") == "captcha":
            raise CaptchaError(obj.get("message", "验证码"))
        if obj.get("type") == "error":
            raise BrowserBridgeError(obj.get("message", "桥错误"))
        yield obj


def crawl_ggzy_list(bridge_script: Path, keyword: str, begin: str, end: str, stage: str,
                    max_pages: int = 200, settle: int = 1200,
                    deadline_ms: int = 360000) -> List[Dict[str, Any]]:
    """兼容旧接口：返回全部记录（不自动解验证码，触发则抛 CaptchaError）。
    deadline_ms：桥侧整体时限（耐心重试但绝不无限拖，默认 8 分钟/阶段）。"""
    records: List[Dict[str, Any]] = []
    for obj in run_bridge(bridge_script, {"keyword": keyword, "begin": begin, "end": end,
                                          "stage": stage, "maxpages": str(max_pages),
                                          "settle": str(settle), "deadlineMs": str(deadline_ms)}):
        if obj.get("type") == "page":
            records.extend(obj.get("records") or [])
    return records
