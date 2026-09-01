#!/usr/bin/env python3
"""内置存储：JSONL（快）/ CSV / XLSX / 控制台。"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

from ..protocols import BaseStorage


class JsonLinesStorage(BaseStorage):
    """JSONL 追加写（默认，速度最快，防内存爆）。"""
    name = "jsonl"

    def open(self, name: str) -> None:
        self.name = name
        self.f = open(self.dir / f"{name}.jsonl", "a", encoding="utf-8")

    def __init__(self, config, task_vars):
        self.dir = Path(config.get("dir", "outputs"))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.name = None
        self.f = None
        self._count = 0

    def write(self, item: Dict[str, Any]) -> None:
        if self.f:
            self.f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
            self._count += 1
            if self._count % 50 == 0:  # 定期落盘，崩溃少丢数据
                self.f.flush()

    def close(self) -> None:
        if self.f:
            self.f.close()


class CsvStorage(JsonLinesStorage):
    name = "csv"

    def __init__(self, config, task_vars):
        super().__init__(config, task_vars)
        self.writer = None
        self.fields: List[str] = []
        import threading
        self._wlock = threading.Lock()  # engine_v3 多 worker 并发写（扩列+writerow 必须串行）

    def open(self, name: str) -> None:
        self.name = name
        path = self.dir / f"{name}.csv"
        existed = path.exists() and path.stat().st_size > 0
        self.f = open(path, "a", newline="", encoding="utf-8-sig")
        self.writer = None
        self.fields = []
        if existed:
            # 已有旧表头：恢复字段，避免重复写表头（跨运行追加，与 jsonl 一致）
            try:
                self.f.seek(0)
                header = self.f.readline().rstrip("\n").split(",")
                self.fields = [h for h in header if h]
                self.f.seek(0, 2)
            except Exception:
                self.fields = []

    def write(self, item: Dict[str, Any]) -> None:
        with self._wlock:
            self._write_locked(item)

    def _write_locked(self, item: Dict[str, Any]) -> None:
        for k in item:
            if k not in self.fields:
                self.fields.append(k)
        if self.writer is None:
            self.writer = csv.DictWriter(self.f, fieldnames=self.fields, extrasaction="ignore")
            # 新文件才写表头（追加已有文件时跳过）
            if not self.fields or (self.f.tell() == 0):
                self.writer.writeheader()
        elif set(item) - set(self.fields):
            # 追加模式中途出现新列：扩列即可，禁止再写表头（否则 CSV 中间多一行表头，文件损坏）
            self.fields = list(dict.fromkeys(self.fields + [k for k in item if k not in self.fields]))
            self.f.close()
            self.f = open(self.dir / f"{self.name}.csv", "a", newline="", encoding="utf-8-sig")
            self.writer = csv.DictWriter(self.f, fieldnames=self.fields, extrasaction="ignore")
        self.writer.writerow({k: (v if v is not None else "") for k, v in item.items()})


class MultiStorage(BaseStorage):
    """多后端存储（对标 Crawlee Dataset 多数据集）：一次任务同时写 jsonl/sqlite/csv 等。
    配置 storage: {"type": "multi", "backends": [
        {"type": "jsonl", "name": "my_jsonl"},
        {"type": "sqlite", "name": "my_sqlite"},
        {"type": "csv"}
    ]}
    """
    name = "multi"

    def __init__(self, config, task_vars):
        cls_map = {"jsonl": JsonLinesStorage, "csv": CsvStorage, "sqlite": SqliteStorage}
        self.backends = []
        for b in config.get("backends") or []:
            bcfg = dict(b)
            bcfg.setdefault("dir", config.get("dir"))
            bcfg.setdefault("name", config.get("name") or "items")
            cls = cls_map.get(bcfg.get("type", "jsonl"), JsonLinesStorage)
            self.backends.append(cls(bcfg, task_vars))

    def open(self, name: str) -> None:
        for b in self.backends:
            b.open(name)

    def write(self, item) -> None:
        for b in self.backends:
            b.write(item)

    def close(self) -> None:
        for b in self.backends:
            b.close()


class SqliteStorage(BaseStorage):
    """SQLite 存储：结构化落库，可查询/增量更新（Scrapy item pipeline 的常见后端）。"""
    name = "sqlite"

    def __init__(self, config, task_vars):
        import sqlite3, threading
        self.dir = Path(config.get("dir", "outputs"))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = str(self.dir / f"{config.get('name', 'items')}.db")
        # worker 线程写入：check_same_thread=False + 锁（引擎多 worker 并发写安全）
        self.conn = sqlite3.connect(self.db, check_same_thread=False)
        self._lock = threading.Lock()
        self.conn.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, data TEXT, ts TEXT)")
        self.conn.commit()

    def open(self, name: str) -> None:
        pass

    def write(self, item: Dict[str, Any]) -> None:
        import json as _json
        import datetime
        with self._lock:
            self.conn.execute(
                "INSERT INTO items (url, data, ts) VALUES (?, ?, ?)",
                (str(item.get("_url", "")), _json.dumps(item, ensure_ascii=False, default=str),
                 datetime.datetime.now().isoformat(timespec="seconds")),
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.commit()
            self.conn.close()
