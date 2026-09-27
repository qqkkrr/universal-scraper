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
            except Exception as e:
                # OCR R6 终审：损坏曾完全静默——用户以为在 resume，实际从头重跑
                # 且毫无线索（与 SeenStore 的告警口径对齐）
                self.data = {}
                import sys
                print(f"⚠️ 检查点文件读取失败（{type(e).__name__}: {e}），按无检查点处理"
                      f"——resume 将从头开始: {path}", file=sys.stderr)

    MAX_ROWS = 2000

    def save(self, rows: List[Dict[str, Any]], done: int, total: int) -> None:
        """断点保存：只保留最近 MAX_ROWS 条（大任务不再每 20 条全量序列化 O(n²)）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        kept = rows[-self.MAX_ROWS:] if len(rows) > self.MAX_ROWS else rows
        self.data.update({"done": done, "total": total, "rows": kept,
                          "rows_truncated": len(rows) > self.MAX_ROWS})
        # 原子写：断电/崩溃不损坏检查点（损坏后 resume 静默丢全部历史行）
        _tmp = self.path.with_suffix(".json.tmp")
        # R129 修复（P1）：并发详情路径中 worker 线程仍会向 row dict 补键，
        # json.dumps 迭代中字典被改大小曾抛 RuntimeError 并炸掉整个任务
        # （抓完的数据全部不导出）。序列化放有界重试内——短窗竞态几次必收敛；
        # 仍失败则降级为告警并跳过本次检查点（旧行为=任务崩溃，严格更差）
        import sys, time
        payload = None
        for _attempt in range(5):
            try:
                payload = json.dumps(self.data, ensure_ascii=False, default=str)
                break
            except (TypeError, ValueError) as e:
                # OCR R6 终审：确定性序列化错误（循环引用/值 __str__ 抛错）重试无意义——
                # 曾穿透到调用方炸掉整个任务（与下方注释的降级目标相悖）
                print(f"⚠️ 检查点序列化失败（{type(e).__name__}: {e}），本次检查点已跳过"
                      "（已抓数据不受影响）", file=sys.stderr)
                return
            except RuntimeError:
                if _attempt == 4:
                    print("⚠️ 检查点序列化与 worker 写入持续冲突，本次检查点已跳过"
                          "（已抓数据不受影响，稍后重试会再保存）", file=sys.stderr)
                    return
                time.sleep(0.05)
        _tmp.write_text(payload, encoding="utf-8")
        import os as _os
        _os.replace(_tmp, self.path)

    def load_rows(self) -> List[Dict[str, Any]]:
        rows = self.data.get("rows", [])
        if self.data.get("rows_truncated"):
            # 审查修复 P2：MAX_ROWS 截断曾静默——resume 回退导出会悄悄缺行
            import sys
            print(f"⚠️ 检查点超过 MAX_ROWS({self.MAX_ROWS}) 已截断至 {len(rows)} 条"
                  "——据此 resume 导出的数据不完整", file=sys.stderr)
        return rows


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
        self._inflight: set = set()  # R6 审查修复：reserve/commit 两段式去重
        if self.path.exists():
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        self._seen.add(line)
            except Exception as e:
                # OCR R131（M）：损坏曾静默清空去重状态——跨运行重复抓取无任何线索
                import sys as _sys
                print(f"⚠️ 去重存档读取失败（{type(e).__name__}），本轮从空集开始: {self.path}",
                      file=_sys.stderr)
        # R6 审查修复（P2）：SIGKILL 前缓冲的已见标记会丢——注册退出兜底
        # （SIGKILL 本身不可救，属 at-least-once 设计窗口，见类 docstring）
        import atexit
        atexit.register(self.flush)

    def is_seen(self, key: str) -> bool:
        with self._lock:
            return key in self._seen or key in self._inflight

    def reserve(self, key: str) -> bool:
        """两段式去重第一步（审查 P1）：原子"查询+占位"。返回 True=占位成功
        （调用方应写存储，写成功后 commit(key)，写失败 rollback(key)）；
        False=已见过或另一 worker 正在写同 key（本次跳过，防并发重复落盘）。"""
        with self._lock:
            if key in self._seen or key in self._inflight:
                return False
            self._inflight.add(key)
            return True

    def commit(self, key: str) -> None:
        """写存储成功后把占位转正为已见（等价于旧 mark，但配对 reserve）。"""
        self.mark(key)
        with self._lock:
            self._inflight.discard(key)

    def rollback(self, key: str) -> None:
        """写存储失败：释放占位（下次重试仍可写）。"""
        with self._lock:
            self._inflight.discard(key)

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
        except Exception as e:
            # OCR R131（M）：写失败曾静默——用户以为增量去重在积累，实际一直在丢
            import sys as _sys
            print(f"⚠️ 去重存档写入失败（{type(e).__name__}），缓冲保留待重试: {self.path}",
                  file=_sys.stderr)  # 保 _pending 供下次重试

    def __len__(self) -> int:
        return len(self._seen)


def record_key(record: Dict[str, Any], keys) -> str:
    if keys == "content_hash":
        try:
            from .modules.pipelines import content_hash
            return content_hash(record, None)
        except Exception:
            # OCR R131（H）：曾返回 ""——调用方把空键记录整条丢弃（既不保留也
            # 不去重）。降级为本地确定性哈希（同记录稳定同键），绝不返回空
            try:
                import hashlib
                blob = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
                return "fb:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
            except Exception:
                # 双重兜底（json.dumps + default=str 仍失败：仅循环引用等极端态）。
                # 返回 ""=调用方按"无键"处理该条（不丢弃也不误去重）；固定哨兵键
                # 反而会让所有极端记录互相误判重复
                return ""
    parts = []
    for k in (keys if isinstance(keys, list) else [keys]):
        # 换行/回车会破坏 JSONL 行对齐（跨重启去重失效），在源头清洗。
        # 审查修复（P2，R6）：`v or ""` 曾把 0/False 折叠成空串——首行 id=0
        # 与缺 id 的记录同键，静默互吞
        v = record.get(k)
        parts.append("" if v is None else str(v).replace("\n", " ").replace("\r", " "))
    return "|".join(parts)
