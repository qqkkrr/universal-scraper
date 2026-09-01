#!/usr/bin/env python3
"""持久化：断点续跑 + 增量去重（seen 集合跨任务保存）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


class Checkpoint:
    """记录任务进度，支持 --resume 跳过已完成部分。"""

    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, Any] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    MAX_ROWS = 2000

    def save(self, rows: List[Dict[str, Any]], done: int, total: int) -> None:
        """断点保存：只保留最近 MAX_ROWS 条（大任务不再每 20 条全量序列化 O(n²)）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        kept = rows[-self.MAX_ROWS:] if len(rows) > self.MAX_ROWS else rows
        self.data.update({"done": done, "total": total, "rows": kept,
                          "rows_truncated": len(rows) > self.MAX_ROWS})
        # 原子写：断电/崩溃不损坏检查点（损坏后 resume 静默丢全部历史行）
        _tmp = self.path.with_suffix(".json.tmp")
        _tmp.write_text(json.dumps(self.data, ensure_ascii=False, default=str), encoding="utf-8")
        import os as _os
        _os.replace(_tmp, self.path)

    def load_rows(self) -> List[Dict[str, Any]]:
        return self.data.get("rows", [])


class SeenStore:
    """增量去重：跨任务保存已见 key（基于 key 的哈希集合，避免内存爆炸）。"""

    def __init__(self, path: Path, flush_every: int = 200):
        import threading
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._flush_every = max(1, flush_every)
        self._lock = threading.Lock()  # 多 worker 并发 mark/is_seen
        self._pending: list = []
        self._seen: set = set()
        if self.path.exists():
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        self._seen.add(line)
            except Exception:
                pass

    def is_seen(self, key: str) -> bool:
        with self._lock:
            return key in self._seen

    def mark(self, key: str) -> None:
        with self._lock:
            if key not in self._seen:
                self._seen.add(key)
                self._pending.append(key)
                # 批量 flush：避免 10 万条 = 10 万次文件 open/write
                # 注意：已持有 _lock，必须调 _flush_locked（flush() 会再次加锁→死锁）
                if len(self._pending) >= self._flush_every:
                    self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("\n".join(self._pending) + "\n")
            self._pending = []  # 写成功才清空；失败保留待下次 flush（防增量去重跨运行失效）
        except Exception:
            pass  # 保 _pending 供下次重试

    def __len__(self) -> int:
        return len(self._seen)


def record_key(record: Dict[str, Any], keys) -> str:
    if keys == "content_hash":
        try:
            from .modules.pipelines import content_hash
            return content_hash(record, None)
        except Exception:
            return ""
    parts = []
    for k in (keys if isinstance(keys, list) else [keys]):
        # 换行/回车会破坏 JSONL 行对齐（跨重启去重失效），在源头清洗
        parts.append(str(record.get(k) or "").replace("\n", " ").replace("\r", " "))
    return "|".join(parts)
