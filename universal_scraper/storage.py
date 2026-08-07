#!/usr/bin/env python3
"""持久化：断点续跑 + 增量去重（seen 集合跨任务保存）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


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

    def save(self, rows: List[Dict[str, Any]], done: int, total: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data.update({"done": done, "total": total, "rows": rows})
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, default=str), encoding="utf-8")

    def load_rows(self) -> List[Dict[str, Any]]:
        return self.data.get("rows", [])


class SeenStore:
    """增量去重：跨任务保存已见 key（基于 key 的哈希集合，避免内存爆炸）。"""

    def __init__(self, path: Path, flush_every: int = 200):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._flush_every = max(1, flush_every)
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
        return key in self._seen

    def mark(self, key: str) -> None:
        if key not in self._seen:
            self._seen.add(key)
            self._pending.append(key)
            # 批量 flush：避免 10 万条 = 10 万次文件 open/write
            if len(self._pending) >= self._flush_every:
                self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("\n".join(self._pending) + "\n")
        except Exception:
            pass
        self._pending = []

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
        parts.append(str(record.get(k) or ""))
    return "|".join(parts)
