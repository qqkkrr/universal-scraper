#!/usr/bin/env python3
"""内置存储：JSONL（快）/ CSV / XLSX / 控制台。"""
from __future__ import annotations

import csv
import json
import sys
import threading
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
        # engine_v3 多 worker 并发写：write/flush 与 close 必须串行（_count += 1 也非原子）
        self._wlock = threading.Lock()

    def write(self, item: Dict[str, Any]) -> None:
        with self._wlock:
            if self.f:
                self.f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
                self._count += 1
                if self._count % 50 == 0:  # 定期落盘，崩溃少丢数据
                    self.f.flush()

    def close(self) -> None:
        with self._wlock:
            if self.f:
                self.f.close()
                self.f = None  # 置 None：关停竞态下迟到的 write 静默跳过，而非对已关闭句柄抛 ValueError


class CsvStorage(JsonLinesStorage):
    name = "csv"

    def __init__(self, config, task_vars):
        super().__init__(config, task_vars)  # _wlock 由父类初始化（CsvStorage 复用同一把锁）
        self.writer = None
        self.fields: List[str] = []

    def open(self, name: str) -> None:
        self.name = name
        path = self.dir / f"{name}.csv"
        existed = path.exists() and path.stat().st_size > 0
        self._existed_at_open = existed  # append 判据（tell() 在追加模式恒为 0，不可用）
        # 审查修复 P0：追加已有文件用 utf-8（utf-8-sig 在追加模式会在文件中部再写 BOM）
        self.f = open(path, "a", newline="", encoding="utf-8" if existed else "utf-8-sig")
        self.writer = None
        self.fields = []
        if existed:
            # 已有旧表头：恢复字段，避免重复写表头（跨运行追加，与 jsonl 一致）。
            # 审查修复 P0：append 句柄不可读，seek+readline 必抛 UnsupportedOperation
            # 被裸 except 吞掉 → fields 恒空 → 每次追加都在文件中部写一行带 BOM 的
            # 幽灵表头，且列序漂移。改用独立只读句柄恢复表头。
            # OCR R131 终审（M）：恢复失败曾静默 self.fields=[]——追加旧文件时
            # 列序漂移 + 幽灵表头。改为大声告警（至少用户知道追加数据列可能错位）
            try:
                with open(path, "r", encoding="utf-8-sig", newline="") as rf:
                    header = next(csv.reader(rf), [])
                self.fields = [h for h in header if h]
            except Exception as e:
                self.fields = []
                print(f"⚠️ CsvStorage: 已有文件表头恢复失败（{e}）——追加数据的列序"
                      f"可能与原文件不一致，建议检查 {path}", file=sys.stderr)

    def write(self, item: Dict[str, Any]) -> None:
        with self._wlock:
            if self.f is None:
                return  # OCR R131（H）：close 后迟到 write 曾对 None 建 DictWriter→AttributeError
            self._write_locked(item)

    def _write_locked(self, item: Dict[str, Any]) -> None:
        grew = False
        for k in item:
            if k not in self.fields:
                self.fields.append(k)
                grew = True
        if self.writer is None:
            # 新文件才写表头（追加已有文件时跳过——用 open 时的 existed 快照）
            self.writer = csv.DictWriter(self.f, fieldnames=list(self.fields), extrasaction="ignore")
            if not self._existed_at_open:
                self.writer.writeheader()
        elif grew:
            # 追加模式中途出现新列：扩列即可，禁止再写表头（否则 CSV 中间多一行表头，文件损坏）。
            # 审查修复：DictWriter 曾按引用持有旧 fields，扩列分支实为死代码
            # OCR R6 终审：曾先 close 再 open——open 失败（权限/EMFILE 等）时
            # self.f 残留已关闭句柄（真值），write 的 None 守卫拦不住，
            # 存储永久坏死。先开新句柄成功再关旧的，失败时旧句柄/旧 writer 仍一致可用
            _new_f = open(self.dir / f"{self.name}.csv", "a", newline="", encoding="utf-8")
            self.f.close()
            self.f = _new_f
            self.writer = csv.DictWriter(self.f, fieldnames=list(self.fields), extrasaction="ignore")
        row = {}
        for k, v in item.items():
            # CSV 公式注入（审查 P1）：抓取文本以 =+-@ 开头时 Excel 会当公式执行
            s = "" if v is None else str(v)
            if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
                s = "'" + s
            row[k] = s
        self.writer.writerow(row)


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
            # OCR R6 终审：setdefault 曾把 config.get("dir") 的 None 显式写入——
            # 后端 config.get("dir", "outputs") 拿到 None → Path(None) TypeError。
            # 按 MultiStorage 自身 docstring 示例（storage 配置可不带 dir）即崩
            if config.get("dir") is not None:
                bcfg.setdefault("dir", config.get("dir"))
            bcfg.setdefault("name", config.get("name") or "items")
            cls = cls_map.get(bcfg.get("type", "jsonl"), JsonLinesStorage)
            self.backends.append(cls(bcfg, task_vars))

    def open(self, name: str) -> None:
        for b in self.backends:
            b.open(name)

    def write(self, item) -> None:
        for b in self.backends:
            # OCR R131（M）：单后端异常曾中断循环——其余后端（可能含主存储）丢数据
            try:
                b.write(item)
            except Exception as e:
                print(f"⚠️ MultiStorage 后端 {type(b).__name__} 写入失败: {e}", file=sys.stderr)

    def close(self) -> None:
        for b in self.backends:
            # OCR R131（M）：单个后端 close 异常曾中断链——其余后端永不关闭（句柄泄漏）
            # OCR R6 终审：曾完全静默——后端最终 flush/commit 失败（磁盘满/SQLite 锁）
            # 即静默丢数据，与 write() 的告警口径不对称。保留继续关其余后端的语义
            try:
                b.close()
            except Exception as e:
                print(f"⚠️ MultiStorage 后端 {type(b).__name__} 关闭失败（最终落盘可能不完整）: {e}",
                      file=sys.stderr)


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
            if self.conn is None:
                return  # OCR R6 终审：close 后迟到的 write 曾对 None 调 execute→AttributeError
            self.conn.execute(
                "INSERT INTO items (url, data, ts) VALUES (?, ?, ?)",
                (str(item.get("_url", "")), _json.dumps(item, ensure_ascii=False, default=str),
                 datetime.datetime.now().isoformat(timespec="seconds")),
            )
            # OCR R131（M）：逐行 commit 曾让 sqlite 写入慢一个数量级（每行一次
            # fsync）。对齐 jsonl 的 50 条落盘节奏；崩溃最多丢 50 条（同 jsonl）
            self._dirty = getattr(self, "_dirty", 0) + 1
            if self._dirty >= 50:
                self.conn.commit()
                self._dirty = 0

    def close(self) -> None:
        # 审查修复 P1：幂等——Ctrl+C 路径 close 一次、finally 再 close 一次，
        # 二次 close 的 ProgrammingError 曾顶掉 KeyboardInterrupt、丢检查点
        with self._lock:
            if self.conn is None:
                return
            self.conn.commit()
            self.conn.close()
            self.conn = None
