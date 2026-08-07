#!/usr/bin/env python3
"""统一日志：控制台 + 文件，带进度统计。"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional


class Logger:
    def __init__(self, log_file: Optional[Path] = None, verbose: bool = True):
        self.log_file = Path(log_file) if log_file else None
        self.verbose = verbose
        self._start = time.time()
        self._counts = {"records": 0, "requests": 0, "errors": 0}

    def _emit(self, msg: str, level: str = "INFO") -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        if self.verbose:
            print(line, file=sys.stderr, flush=True)
        if self.log_file:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def info(self, msg: str) -> None:
        self._emit(msg, "INFO")

    def warn(self, msg: str) -> None:
        self._emit(msg, "WARN")

    def error(self, msg: str) -> None:
        self._emit(msg, "ERROR")
        self._counts["errors"] += 1

    def tick(self, kind: str = "records", n: int = 1) -> None:
        self._counts[kind] = self._counts.get(kind, 0) + n

    def progress(self, done: int, total: int, what: str = "records") -> None:
        pct = (done / total * 100) if total else 0
        el = time.time() - self._start
        rate = (done / el) if el > 0 else 0
        self.info(f"{what}: {done}/{total} ({pct:.1f}%) | 用时 {el:.0f}s | 速率 {rate:.1f}/s")

    def summary(self) -> None:
        el = time.time() - self._start
        self.info(f"汇总: 记录 {self._counts['records']} | 请求 {self._counts['requests']} | "
                  f"错误 {self._counts['errors']} | 总用时 {el:.0f}s")


logger = Logger()
