#!/usr/bin/env python3
"""📉 配额账本（Quota Ledger）——《科研管理》1369 篇战役 2026-09 核心教训代码化。

战场原型：官网每篇文章每天有请求次数上限（POST/GET 均扣，被拒不退款），
守候进程每 8 分钟"温和试探"一次 = 把回血窗口无限重置（自伤）。
本模块把四条血泪教训变成可复用原语：

1. 冷却账本：每个 (维度, 资源) 记最后触碰时间戳；窗口未滚出绝不二次请求。
2. 切片分工：按 IP 计配额 × 顺序队列 = 灾难（队首轰击）；
   worker i ← 第 i 段不相交工作集。
3. 边际余量：每 worker 上限 = 观察墙值 × 0.75（撞墙瞬间会烧掉正在过手的资源）。
4. 失败分类路由：dead(换IP不重试资源) / quota(当日拉黑) / global(全队静默) /
   disk(暂停且不烧代理) —— 不同失败对应完全不同的处置。

用法:
  from universal_scraper.quota_ledger import QuotaLedger, assign_chunks, margin_cap
  led = QuotaLedger("~/.universal-scraper/quota_ledger.json")
  led.touch("article:22393", dim="resource")
  if led.in_cooldown("article:22393", dim="resource"): ...  # 绝不再请求
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Sequence

# 各维度默认冷却窗口（秒）；可按站点实测覆盖
DEFAULT_WINDOWS = {
    "resource": 86400,   # 按资源计费：单资源回补窗口（战例 24h）
    "ip": 86400,         # 每 IP 日配额
    "site": 86400,       # 全局日预算
    "account": 86400,    # 账号级限额
}

# 失败分类 → 处置动作（战例路由表）
FAILURE_ACTIONS = {
    "dead":    "rotate_worker_no_retry",   # 死代理/超时：换 worker，绝不动资源账本
    "quota":   "blacklist_until_window",   # 配额被拒：该资源/IP 记账并冷却
    "global":  "all_silence_wait",         # 全局熔断：全队静默等窗口
    "disk":    "pause_without_burn",       # 磁盘/本地故障：暂停，不烧任何代理
}


class QuotaLedger:
    """原子持久化的多维冷却账本。"""

    def __init__(self, path: str | Path, windows: Dict[str, int] | None = None):
        self.path = Path(path).expanduser()
        self.windows = {**DEFAULT_WINDOWS, **(windows or {})}
        # 结构: {dim: {key: last_touch_ts}}
        self.data: Dict[str, Dict[str, int]] = {}
        self._load()

    # ---------- 持久化（原子：写坏任何时刻不损旧账）
    def _load(self):
        if not self.path.exists():
            return
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            try:
                self.path.rename(self.path.with_suffix(".corrupt"))
            except Exception:
                pass
            self.data = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        tmp.replace(self.path)

    # ---------- 账本操作
    def window(self, dim: str) -> int:
        return self.windows.get(dim, self.windows.get("resource", 86400))

    def touch(self, key: str, dim: str = "resource"):
        """记账一次触碰（注意：被拒的请求同样要 touch——这是本战例最贵的教训）。"""
        self.data.setdefault(dim, {})[key] = int(time.time())
        self.save()

    def last(self, key: str, dim: str = "resource") -> int:
        return self.data.get(dim, {}).get(key, 0)

    def in_cooldown(self, key: str, dim: str = "resource") -> bool:
        return (time.time() - self.last(key, dim)) < self.window(dim)

    def remaining(self, key: str, dim: str = "resource") -> int:
        """冷却剩余秒数（0 = 可请求）。"""
        r = self.window(dim) - (time.time() - self.last(key, dim))
        return max(0, int(r))

    def filter_ready(self, keys: Sequence[str], dim: str = "resource") -> List[str]:
        """从工作清单里筛出账本允许请求的子集。"""
        return [k for k in keys if not self.in_cooldown(k, dim)]


# ---------- 切片分工（战训 #2）
def assign_chunks(items: Sequence, chunk_size: int) -> List[List]:
    """把工作清单切成互不相交的段：worker i 处理第 i 段。

    战例：修复前 30 个代理全从队首开始（0 篇/天）；切片后 1100 篇/小时级。
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正")
    return [list(items[i:i + chunk_size]) for i in range(0, len(items), chunk_size)]


def worker_chunk(items: Sequence, worker_index: int, chunk_size: int) -> List:
    """worker i ← 第 i 段（越界回绕重试前段；配合账本跳过冷却中的资源）。"""
    chunks = assign_chunks(items, chunk_size)
    if not chunks:
        return []
    return chunks[worker_index % len(chunks)]


# ---------- 边际余量（战训 #3）
def margin_cap(observed_wall: int, ratio: float = 0.75) -> int:
    """每 worker 配额上限 = 观察墙值 × ratio。

    战例：每 IP 实际墙 ~20，设 18 时每个代理撞墙瞬间烧掉 1~2 篇文章额度
    （71 代理 × ~2 = 140 篇牺牲品）。留 25% 余量后近零损耗。
    """
    if observed_wall <= 0:
        raise ValueError("observed_wall 必须为正")
    if not 0.1 <= ratio <= 1.0:
        raise ValueError("ratio 取 0.1~1.0")
    return max(1, int(observed_wall * ratio))


# ---------- 失败分类路由（战训 #4）
def route_failure(kind: str) -> str:
    """失败类型 → 处置动作。未知类型按 quota 保守处理。"""
    return FAILURE_ACTIONS.get(kind, FAILURE_ACTIONS["quota"])
