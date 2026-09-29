#!/usr/bin/env python3
"""内置存储：JSONL（快）/ CSV / XLSX / 控制台。"""
from __future__ import annotations

import csv
import json
import re
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

    def _rewrite_with_new_header(self) -> None:
        """表头变更时把整个 CSV 重写为并集表头（读回已写行，原子替换后重开句柄）。

        审查八轮（MEDIUM）：CSV 追加写无法"回溯改表头"，此前只扩 DictWriter →
        新行比表头多列（pandas/Excel 直接解析失败）。整表重写是唯一能保持文件
        合法的做法（新建型任务每轮最多触发一次，成本可控）。
        """
        import os
        path = self.dir / f"{self.name}.csv"
        # 关键：回读前必须先 flush——append 句柄带缓冲时已写行还在内存里，
        # 直接读盘会把前面所有行当"不存在"丢掉（实测：重写后只剩扩列之后的行）
        try:
            self.f.flush()
        except Exception:
            pass
        rows: List[Dict[str, Any]] = []
        if path.exists():
            try:
                with open(path, "r", newline="", encoding="utf-8-sig") as fr:
                    rows = list(csv.DictReader(fr))
            except Exception:
                rows = []
        tmp = path.with_name(path.name + ".rewrite.tmp")
        # 收官十二轮（审查 M）：重写曾用 utf-8（无 BOM）——同一文件是否有 BOM
        # 取决于中途是否扩过列，Excel 打开中文乱码。与新建路径同用 utf-8-sig
        with open(tmp, "w", newline="", encoding="utf-8-sig") as ft:
            w = csv.DictWriter(ft, fieldnames=list(self.fields), extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: (v if v is not None else "") for k, v in r.items()})
        # 先关旧句柄再替换（否则 self.f 仍指向被替换掉的旧 inode，后续写入丢失）
        try:
            self.f.close()
        except Exception:
            pass
        os.replace(tmp, path)
        self.f = open(path, "a", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.f, fieldnames=list(self.fields), extrasaction="ignore")

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
                # 收官十二轮（审查 H，实测）：跨运行追加时本轮首条即带新列，曾只建
                # writer 不扩表头 → 磁盘表头 2 列、该行 3 列，整文件不可解析
                # （pandas: Expected 2 fields in line 4, saw 3）。与下文"中途扩列"
                # 同口径：整表重写为并集表头
                self._rewrite_with_new_header()
        elif grew:
            # 追加模式中途出现新列：**重写整表**（读回已写行 + 并集表头，原子替换）。
            # 审查八轮（MEDIUM）：此前只重建 DictWriter 不扩表头——新行比表头多列，
            # 标准解析器直接报错（实测 pandas.read_csv: Expected 2 fields in line 3,
            # saw 3），字段错位且无法解析。原注释"扩列即可，禁止再写表头"防的是
            # 文件中部插一行表头，但那条路同样是坏文件；这里改为真正的整表重写。
            self._rewrite_with_new_header()
        row = {}
        for k, v in item.items():
            # CSV 公式注入（审查 P1）：抓取文本以 =+-@ 开头时 Excel 会当公式执行。
            # 收官十二轮（审查 L）：`-3.5`/`+8613...` 是合法数值/电话而非公式——
            # 加前缀会让 pandas 读到字符串 "'-3.5"，数值列全变文本。纯数字形态
            # （含百分比/千分位）不做前缀
            s = "" if v is None else str(v)
            if s[:1] in ("=", "+", "-", "@", "\t", "\r") and \
                    not re.fullmatch(r"[-+]\d[\d,]*\.?\d*%?", s):
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
            # 审查八轮（LOW）：commit 抛错（磁盘满/DB 锁定）曾让下面的 close 永不执行
            # ——连接句柄滞留（self.conn 仍非 None）、后续 finally 收尾也拿不到干净状态。
            # 用 finally 保证句柄一定释放，异常照常上抛（失败不伪装成功）。
            try:
                self.conn.commit()
            finally:
                try:
                    self.conn.close()
                finally:
                    self.conn = None
