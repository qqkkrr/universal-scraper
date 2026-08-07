#!/usr/bin/env python3
"""内置中间件：日志 / 请求计数。"""
from __future__ import annotations

from ..protocols import BaseMiddleware, Request, Response, ParseContext


class LogMiddleware(BaseMiddleware):
    name = "log"

    def __init__(self, config, task_vars):
        self.logger = None

    def on_request(self, req: Request, ctx: ParseContext):
        self._log(f"→ {req.method} {req.url} (depth={req.depth})")
        return req

    def on_response(self, resp: Response, ctx: ParseContext):
        self._log(f"← {resp.status} {resp.url} ({len(resp.body)}B)")
        return resp

    def on_data(self, item, ctx):
        self._log(f"  item: {str(item)[:80]}")
        return item

    def _log(self, msg):
        from ..core import log
        log(msg)


class CaptchaMiddleware(BaseMiddleware):
    """HTTP 场景验证码处理：检测响应是否为验证码 → 存图 → antibot 求解 → 答案写入 ctx.vars。"""

    name = "captcha"

    def __init__(self, config, task_vars):
        super().__init__(config, task_vars)
        self.detect = config.get("detect", "captcha|verify|验证码")
        self.out_dir = None

    def on_response(self, resp: Response, ctx: ParseContext):
        import re
        from pathlib import Path
        from ..antibot import solve_captcha_file
        text = resp.text[:2000] if resp.text else ""
        if re.search(self.detect, text, re.I) or b"image" in resp.body[:64]:
            out = Path(ctx.vars.get("_captcha_dir", "/tmp/universal_scraper_captcha"))
            out.mkdir(parents=True, exist_ok=True)
            fp = out / f"captcha_{abs(hash(resp.url)) % 100000}.png"
            fp.write_bytes(resp.body)
            res = solve_captcha_file(str(fp), ctx.config.get("anti_bot", {}))
            if res.get("answer"):
                ctx.vars["captcha_answer"] = res["answer"]
                ctx.vars["captcha_image"] = str(fp)
        return resp


class NotifyMiddleware(BaseMiddleware):
    """Webhook 通知：on_error 立即通知；on_data 攒批（batch_size）或任务结束通知。

    配置: middleware: [{"on": "data|error", "action": "webhook", "url": "https://...",
                        "batch_size": 50, "headers": {"X-Key": "..."}}]
    """
    name = "webhook"

    def __init__(self, config, task_vars):
        super().__init__(config, task_vars)
        self.url = config.get("url")
        self.batch_size = int(config.get("batch_size", 50))
        self.headers = config.get("headers", {})
        self._batch = []

    def _post(self, payload: dict) -> None:
        if not self.url:
            return
        import json as _json
        import urllib.request
        req = urllib.request.Request(self.url, data=_json.dumps(payload, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json", **self.headers})
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            from ..log import log
            log(f"webhook 发送失败: {e}", "WARN")

    def on_error(self, req, error, ctx):
        self._post({"event": "error", "url": req.url, "error": str(error)[:300]})

    def on_data(self, item, ctx):
        self._batch.append(item)
        if len(self._batch) >= self.batch_size:
            self._post({"event": "batch", "count": len(self._batch), "items": self._batch})
            self._batch = []
        return item

    def flush(self):
        if self._batch:
            self._post({"event": "batch", "count": len(self._batch), "items": self._batch})
            self._batch = []
