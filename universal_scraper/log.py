#!/usr/bin/env python3
"""统一日志：控制台 + 文件，带进度统计。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Optional


class Logger:
    def __init__(self, log_file: Optional[Path] = None, verbose: bool = True):
        self.log_file = Path(log_file) if log_file else None
        self.verbose = verbose
        self._start = time.time()
        self._counts = {"records": 0, "requests": 0, "errors": 0}
        self._lock = threading.Lock()  # R44 修复：worker 线程并发 error() 计数曾互相覆盖

    def _emit(self, msg: str, level: str = "INFO") -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        if self.verbose:
            print(line, file=sys.stderr, flush=True)
        if self.log_file:
            # 审查修复（P2，R14）：日志文件写失败曾直接抛——engine 的 except/
            # finally 路径里的日志调用会让 OSError 顶掉原始错误并杀死 worker。
            # 日志器绝不能抛：失败降级 stderr 一次性告警
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                if not getattr(self, "_file_warned", False):
                    self._file_warned = True
                    print(f"⚠️ 日志文件写入失败（后续降级仅 stderr）: {e}",
                          file=sys.stderr, flush=True)

    def info(self, msg: str) -> None:
        self._emit(msg, "INFO")

    def warn(self, msg: str) -> None:
        self._emit(msg, "WARN")

    def error(self, msg: str) -> None:
        self._emit(msg, "ERROR")
        with self._lock:
            self._counts["errors"] += 1

    def tick(self, kind: str = "records", n: int = 1) -> None:
        with self._lock:
            self._counts[kind] = self._counts.get(kind, 0) + n

    def progress(self, done: int, total: int, what: str = "records") -> None:
        pct = (done / total * 100) if total else 0
        el = time.time() - self._start
        rate = (done / el) if el > 0 else 0
        self.info(f"{what}: {done}/{total} ({pct:.1f}%) | 用时 {el:.0f}s | 速率 {rate:.1f}/s")

    def summary(self) -> None:
        el = time.time() - self._start
        # NBS 考核战训修复：requests 计数器曾只读不写（恒打印"请求 0"）——
        # 真值在 core 的全局计数器（含重试，按真实 HTTP 尝试计）
        try:
            from .core import request_budget
            req_n = request_budget()["used"]
        except ImportError:
            # OCR R131（M）：裸 Exception 曾连 core 计数器自身的 bug 也吞掉
            req_n = self._counts.get("requests", 0)
        self.info(f"汇总: 记录 {self._counts['records']} | 请求 {req_n} | "
                  f"错误 {self._counts['errors']} | 总用时 {el:.0f}s")


logger = Logger()
