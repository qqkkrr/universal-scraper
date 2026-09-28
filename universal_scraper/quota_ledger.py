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
            data = json.loads(self.path.read_text(encoding="utf-8"))
            # 边界复现：合法 JSON 但结构错（顶层 list/string、dim 值非 dict）曾绕过
            # 损坏隔离直接崩在 touch/read——必须在载入时就走隔离+重建路径
            if not isinstance(data, dict):
                raise ValueError(f"顶层应为 dict，实际 {type(data).__name__}")
            bad = [k for k, v in data.items()
                   if k != "notes" and not isinstance(v, dict)]
            if bad:
                raise ValueError(f"维度值非 dict: {bad[:3]}")
            # OCR R131（H）：内层时间戳类型未校验——损坏值（字符串/None/布尔）
            # 进来后 touch/in_cooldown 的算术比较直接 TypeError。载入期剔除
            for _dim, _entries in data.items():
                if not isinstance(_entries, dict):
                    continue
                if _dim in ("action_budgets", "budget_meta", "notes"):
                    # 收官十轮（审查，实测）：`domain` 曾在此豁免名单里，但它在本
                    # 仓库就是时间戳维度（domain_budget.mark 写 int(time.time())）——
                    # 文件里该键为字符串（手编/旧版/外部工具）时既不隔离也不剔除，
                    # in_cooldown/check 的算术直接 TypeError，正是本校验要消灭的形态
                    continue  # 结构化维度不按时间戳校验
                for _k in list(_entries.keys()):
                    if isinstance(_entries[_k], bool) or \
                            not isinstance(_entries[_k], (int, float)):
                        del _entries[_k]
            self.data = data
        except Exception as e:
            # 审查修复：静默清零冷却账本 = 重新武装"自伤"行为（被拒不退款是本模块
            # 存在的理由）。出声 + 时间戳隔离，绝不覆盖前一份证据。
            # OCR R131（M）：rename 失败曾被吞——证据没隔离成功 data 已清零，
            # 下次 save 覆盖原始损坏文件。uuid 后缀防同秒碰撞，失败则复制兜底
            import sys, uuid as _uuid
            print(f"⚠️ 配额账本损坏（{type(e).__name__}: {str(e)[:60]}），隔离后从零重建: {self.path}",
                  file=sys.stderr)
            _dest = self.path.with_suffix(f".corrupt.{int(time.time())}.{_uuid.uuid4().hex[:6]}")
            try:
                self.path.rename(_dest)
            except Exception:
                try:
                    import shutil as _sh
                    _sh.copy2(self.path, _dest)
                except Exception as _ce:
                    print(f"⚠️ 损坏证据隔离彻底失败（{_ce}）——原文将在下次 save 被覆盖",
                          file=sys.stderr)
            self.data = {}
        # 注意：load-modify-save 无跨进程锁——按单进程假设设计（agent 串行驱动）。
        # 多进程并发写会互相覆盖（last-writer-wins），需要时外层自加 flock。

    def save(self):
        # R31 审查修复（P2）：save 时顺带清理过期条目——touch 型键（逐文章/
        # 逐代理）曾单调累积，账本无上限增长且每次 touch 全量重写越来越慢。
        # 只清理纯时间戳条目（int/float），保留带结构的 dim（domain/action_budgets 等）
        # OCR R131（H）：清理扫描与全量落盘曾每次 touch 都做（1369+ 键时 O(n)
        # ×每请求）。清理降频到 60s 一次；落盘去抖 2s（进程内 data 始终最新，
        # 崩溃最多丢 2s 内的 touch 精度）
        import time as _t
        now = _t.time()
        if now - getattr(self, "_last_cleanup", 0.0) > 60.0:
            self._last_cleanup = now
            for dim in list(self.data.keys()):
                if not isinstance(self.data[dim], dict):
                    continue
                if dim in ("domain", "action_budgets", "budget_meta"):
                    continue  # 结构化/非纯时间戳维度不清理
                # OCR R131（M）：未知维度曾按 resource 窗口兜底清理——自定义维度
                # 的有效期被错误套用。未显式配置窗口的维度不清理（保守保留）
                if dim not in self.windows:
                    continue
                for k in list(self.data[dim].keys()):
                    v = self.data[dim][k]
                    if isinstance(v, (int, float)) and now - v > self.window(dim) * 2:
                        del self.data[dim][k]
                if not self.data[dim]:
                    del self.data[dim]
        # 注：落盘保持每次 save 都写（CLI 短进程 mark 后即退出去抖会丢账）；
        # O(n) 清理扫描已降频，json 重写本身为毫秒级
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # R90 修复（P2）：固定 .json.tmp 曾让并发双进程写同一临时文件互相踩踏，
        # replace 后台账成垃圾被隔离重置（冷却状态静默清空）——改唯一临时名
        import tempfile as _tf, os as _os
        _fd, _tmpname = _tf.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            try:
                _f = _os.fdopen(_fd, "w", encoding="utf-8")
            except Exception:
                _os.close(_fd)  # fdopen 失败时 fd 仍归我们管，不关则泄漏
                raise
            with _f:
                _f.write(json.dumps(self.data))
            _os.replace(_tmpname, self.path)
        except Exception:
            try:
                _os.unlink(_tmpname)
            except OSError:
                pass
            raise

    # ---------- 账本操作
    def window(self, dim: str) -> int:
        return self.windows.get(dim, self.windows.get("resource", 86400))

    def touch(self, key: str, dim: str = "resource"):
        """记账一次触碰（注意：被拒的请求同样要 touch——这是本战例最贵的教训）。"""
        self.data.setdefault(dim, {})[key] = int(time.time())
        try:
            self.save()
        except Exception as _e:
            # 审查二轮（H）：save 失败曾把异常抛进抓取请求路径（记账炸掉抓取）。
            # 内存 data 保持最新，下次 save 成功即收敛——降级为大声 WARN
            import sys as _sys
            print(f"⚠️ 配额台账落盘失败（{type(_e).__name__}: {_e}）——内存已记账，"
                  "将随下次成功落盘收敛", file=_sys.stderr)

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
