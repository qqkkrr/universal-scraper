#!/usr/bin/env python3
"""中间件/钩子：请求前、响应后、数据产出、错误时 四个时机。
配置示例: "middleware": [{"on": "data", "action": "log"}] 或 Python 可调用路径。"""
from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, List, Optional


class MiddlewareChain:
    def __init__(self, specs: Optional[List[Dict[str, Any]]] = None, logger=None):
        self.logger = logger
        self._handlers: Dict[str, List[Callable]] = {"request": [], "response": [], "data": [], "error": []}
        for spec in (specs or []):
            self._register(spec)

    def _register(self, spec: Dict[str, Any]) -> None:
        on = spec.get("on")
        if on not in self._handlers:
            raise ValueError(f"未知中间件时机: {on}（可选 request/response/data/error）")
        action = spec.get("action")
        if action == "log":
            self._handlers[on].append(self._mk_log(on, spec.get("message", "")))
        elif action and ":" in action:  # module:function
            mod, fn = action.split(":", 1)
            try:
                func = getattr(importlib.import_module(mod), fn)
                self._handlers[on].append(func)
            except Exception as e:
                raise ValueError(f"中间件导入失败 {action}: {e}")
        elif callable(action):
            self._handlers[on].append(action)
        else:
            raise ValueError(f"未知中间件 action: {action}")

    def _mk_log(self, on: str, message: str):
        def handler(ctx: Dict[str, Any]) -> None:
            if self.logger:
                self.logger.info(f"[middleware:{on}] {message or ctx.get('url') or ctx.get('title') or ''}")
        return handler

    def run(self, on: str, ctx: Dict[str, Any]) -> None:
        for h in self._handlers.get(on, []):
            try:
                h(ctx)
            except Exception as e:
                if self.logger:
                    self.logger.warn(f"中间件 {on} 出错: {e}")
