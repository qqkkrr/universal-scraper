#!/usr/bin/env python3
"""内置中间件：日志 / 请求计数。"""
from __future__ import annotations

from ..protocols import BaseMiddleware, Request, Response, ParseContext


class LogMiddleware(BaseMiddleware):
    name = "log"

    def __init__(self, config, task_vars):
        super().__init__(config, task_vars)  # OCR R131：曾漏 super——实例缺 config/vars 属性
        # OCR R131（L）：self.logger 死属性已删（无任何读取方）

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
        import hashlib
        import re
        from pathlib import Path
        from ..antibot import solve_captcha_file
        body = resp.body or b""
        text = resp.text[:2000] if resp.text else ""
        ctype = (resp.headers.get("content-type") or "") if hasattr(resp, "headers") else ""
        # 只有"确实是图片"才当验证码解（防止普通页面提到"验证码"三字就被整页误存）。
        # 审查三轮（M）：RIFF 容器含 WAV/AVI——只有 offset 8 为 WEBP 才算图
        _riff_webp = body[:4] == b"RIFF" and body[8:12] == b"WEBP"
        is_image = (body[:8].startswith(b"\x89PNG") or body[:3] == b"\xff\xd8\xff"
                    or body[:4] == b"GIF8" or _riff_webp or "image" in str(ctype).lower())
        if not is_image:
            if re.search(self.detect, text, re.I):
                from ..core import log as _log
                _log(f"  页面含验证码字样但非图片响应，跳过自动求解（url={resp.url[:80]}）", "DEBUG")
            return resp
        # OCR R131（M）：验证码输出目录曾可用 ctx.vars 注入任意路径——任务变量
        # 属可配置输入，收归任务目录下的固定子目录（防越权写）
        _base = Path(ctx.vars.get("_task_dir") or ".")
        out = Path(_base) / ".captcha"
        out.mkdir(parents=True, exist_ok=True)
        fp = out / f"captcha_{hashlib.md5(str(resp.url).encode(), usedforsecurity=False).hexdigest()[:12]}.png"
        fp.write_bytes(body)
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
        # OCR R131（M）：batch_size 配了非数字（"50"/空串）曾在中间件构造期裸抛
        # ValueError 打断整个任务装配——回退默认值并 WARN
        try:
            self.batch_size = max(1, int(config.get("batch_size", 50)))
        except (TypeError, ValueError):
            self.batch_size = 50
            from ..core import log
            log(f"webhook batch_size 配置非法（{config.get('batch_size')!r}），回退 50", "WARN")
        self.headers = config.get("headers", {})
        self._batch = []

    def _post(self, payload: dict) -> None:
        if not self.url:
            return
        import json as _json
        import socket
        import urllib.error
        import urllib.parse
        import urllib.request
        # R43 修复：webhook URL 校验——仅 http/https；host 解析结果拒绝私网/
        # 保留地址（防配置误填打内网/云元数据）；环回放行（本机 n8n 等自动化
        # 是 webhook 的合法主要场景）
        sp = urllib.parse.urlsplit(str(self.url))
        if sp.scheme not in ("http", "https") or not sp.hostname:
            from ..core import log
            log(f"webhook URL 非法（仅允许 http/https 且带域名）: {self.url}", "WARN")
            return
        try:
            for info in socket.getaddrinfo(sp.hostname, None):
                import ipaddress
                ip = ipaddress.ip_address(info[4][0])
                if (ip.is_private or ip.is_reserved or ip.is_link_local) and not ip.is_loopback:
                    from ..core import log
                    log(f"webhook URL 指向私网/保留地址，已拒绝: {sp.hostname} -> {ip}", "WARN")
                    return
        except ValueError:
            pass  # 主机名非 IP 形态（正常域名），交给 DNS 解析
        except socket.gaierror:
            # R43b 加固：域名解析失败曾穿透到 urlopen 每条重试——WARN 一次即止
            from ..core import log
            log(f"webhook 域名解析失败: {sp.hostname}", "WARN")
            return
        req = urllib.request.Request(self.url, data=_json.dumps(payload, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json", **self.headers})
        # R129 修复（P1）：urlopen 曾自动跟随 30x 且对目标零复查——检查通过的一跳
        # 之后，一次重定向即可打到云元数据/内网（同策略类：环回放行、私网拒绝）
        import ipaddress as _ipa
        class _WebhookRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                sp2 = urllib.parse.urlsplit(newurl or "")
                if sp2.scheme not in ("http", "https") or not sp2.hostname:
                    raise urllib.error.HTTPError(req.full_url, code,
                                                 f"拒绝重定向到非 http/https: {newurl!r}", headers, fp)
                try:
                    for info in socket.getaddrinfo(sp2.hostname, None):
                        ip = _ipa.ip_address(info[4][0])
                        if (ip.is_private or ip.is_reserved or ip.is_link_local) and not ip.is_loopback:
                            raise urllib.error.HTTPError(req.full_url, code,
                                                         f"重定向指向私网/保留地址: {ip}", headers, fp)
                except socket.gaierror:
                    raise urllib.error.HTTPError(req.full_url, code,
                                                 f"重定向目标解析失败: {sp2.hostname}", headers, fp)
                return super().redirect_request(req, fp, code, msg, headers, newurl)
        try:
            # OCR R131 终审（M）：open() 返回的 response 曾不关闭——webhook 高频
            # 发送时 socket fd 泄漏。with 上下文确保关闭
            with urllib.request.build_opener(_WebhookRedirectHandler()).open(req, timeout=10) as _resp:
                _resp.read()  # 读完关闭，连接归还池
        except Exception as e:
            # R43 修复：曾 `from ..log import log`——log() 在 core.py 不在 log.py，
            # 这个 ImportError 让设计好的失败 WARN 永远打不出来
            from ..core import log
            log(f"webhook 发送失败: {e}", "WARN")

    def on_error(self, req, error, ctx):
        # 限频：同一时刻 5s 内只发一次（避免重试风暴刷爆 webhook）
        import time as _t
        now = _t.time()
        if now - getattr(self, "_last_err_ts", 0) < 5:
            return
        self._last_err_ts = now
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
