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
    """兼容旧接口：流式产出 JSONL 对象（captcha/error 抛异常）。
    R27 修复：timeout 此前被静默丢弃（_run 无时限）——现以 deadlineMs 传入桥内，
    支持的桥按它自限；调用方显式传过的 deadlineMs 不覆盖。"""
    params = dict(args)
    params.setdefault("deadlineMs", str(int(timeout * 1000)))
    src = {"type": "browser_script", "bridge": str(bridge_script), "bridge_params": params}
    import tempfile, shutil as _sh
    captcha_dir = tempfile.mkdtemp(prefix="us_captcha_")  # 0700 私有随机目录，替代可预测的 /tmp 固定路径
    # OCR R131（H）：mkdtemp 目录曾任何路径都不清理——每次 run_bridge 泄漏一个
    # 含验证码图片的临时目录。生成器被关闭/异常时 finally 兜底回收
    try:
        fetcher = BrowserScriptFetcher(src, {"captcha_dir": captcha_dir}, {}, Path.cwd())
        for obj in fetcher._run(bridge_script, params):
            if obj.get("type") == "captcha":
                raise CaptchaError(obj.get("message") or "验证码")
            if obj.get("type") == "error":
                raise BrowserBridgeError(obj.get("message") or "桥错误")
            yield obj
    finally:
        _sh.rmtree(captcha_dir, ignore_errors=True)


def crawl_ggzy_list(bridge_script: Path, keyword: str, begin: str, end: str, stage: str,
                    max_pages: int = 200, settle: int = 1200,
                    deadline_ms: int = 360000) -> List[Dict[str, Any]]:
    """兼容旧接口：返回全部记录（不自动解验证码，触发则抛 CaptchaError）。
    deadline_ms：桥侧整体时限（耐心重试但绝不无限拖，默认 360000ms=6 分钟/阶段；
    OCR R131（L）：docstring 曾写 8 分钟与默认值不符）。"""
    records: List[Dict[str, Any]] = []
    for obj in run_bridge(bridge_script, {"keyword": keyword, "begin": begin, "end": end,
                                          "stage": stage, "maxpages": str(max_pages),
                                          "settle": str(settle), "deadlineMs": str(deadline_ms)}):
        if obj.get("type") == "page":
            records.extend(obj.get("records") or [])
    return records
