#!/usr/bin/env python3
"""稀缺动作预算（gsxt 战训沉淀，2026-09）：按"动作类型"记账的滑动窗口配额。

背景：gsxt 的配额单位不是"请求数"，而是"每小时 N 次搜索、且**失败也扣额度**"。
请求级预算（--max-requests）把搜索和详情当成同价动作，会误导采集节奏——
正确姿势：贵动作（搜索/提交/发码）单独记账，便宜动作（详情/翻页）在窗口内
尽量多收。本模块实现带时间衰减的命名预算：

    from .action_budget import acquire
    r = acquire("gsxt-search", limit=5, window=3600, cost=1)
    if not r["allowed"]:
        sleep(r["reset_in"])   # 或转做便宜动作

失败也扣：调用方在动作"发起"时 acquire（无论成败不退额）；窗口是滑动的，
只数最近 window 秒内的 events。台账持久化到 ~/.universal_scraper/action_budget.json
（可用环境变量 UNIVERSAL_SCRAPER_ACTION_BUDGET 覆盖），跨运行生效。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from .quota_ledger import QuotaLedger

DEFAULT_FILE = os.environ.get(
    "UNIVERSAL_SCRAPER_ACTION_BUDGET",
    str(Path.home() / ".universal_scraper" / "action_budget.json")
)
_LOCK = threading.Lock()


def _ledger(path: str | Path | None = None) -> QuotaLedger:
    return QuotaLedger(path or DEFAULT_FILE, windows={"action": 86400})


def _budgets(led: QuotaLedger) -> Dict[str, Any]:
    return led.data.setdefault("action_budgets", {})


def _prune(events: List[float], window: float, now: float) -> List[float]:
    """滑动窗口：只保留最近 window 秒内的事件。"""
    cut = now - window
    return [t for t in events if t > cut]


def state(name: str, limit: int, window: float, path: str | Path | None = None) -> Dict[str, Any]:
    """查当前预算状态（不扣额）：used/limit/remaining/reset_in。
    审查修复（P2）：优先使用 acquire 时持久化的 limit/window——查询时的
    参数若与扣额时不同，remaining/reset_in 会算出与真实执行相悖的数字。
    OCR R131（H）：与 acquire 同抢 flock——此前只持进程内锁，另一进程
    acquire 写到一半时的读取是撕裂快照。"""
    _p = Path(os.path.expanduser(str(path or DEFAULT_FILE))).resolve()  # 审查三轮：与 acquire 同口径展开
    _p.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    with open(f"{_p}.lock", "a") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_SH)  # 读共享锁：与 acquire 的写锁互斥
        led = _ledger(_p)
        with _LOCK:
            b = _budgets(led).get(name) or {}
            limit = int(b.get("limit", limit))
            window = float(b.get("window", window))
            now = time.time()
            events = _prune([float(t) for t in (b.get("events") or [])], window, now)
            used = len(events)
            reset_in = 0.0
            # OCR R131（M）：limit<=0 且 events 空时 events[0] 必 IndexError
            # 审查修复：时钟回拨（NTP 步进）后插入序≠时间序，events[0] 未必最早
            # 出窗——用 min(events) 取真实最早出窗时刻
            if events and used >= limit:
                reset_in = round(max(0.0, min(events) + window - now), 1)
            return {"name": name, "used": used, "limit": int(limit),
                    "remaining": max(0, int(limit) - used),
                    "window_sec": float(window), "reset_in_sec": reset_in}


def acquire(name: str, limit: int = 5, window: float = 3600.0, cost: int = 1,
            path: str | Path | None = None) -> Dict[str, Any]:
    """尝试为一次贵动作扣额。允许 → events 落账返回 allowed=True；
    超额 → allowed=False + reset_in（秒，最早一批事件出窗时间）。
    语义提醒：动作发起即扣额，**失败不退**（gsxt 战训：失败搜索同样烧窗口）。

    跨进程安全（审查 P2 修复）：读→判→写在 <file>.lock 的 flock 内完成——
    多进程/多子代理并发扣额不再 last-writer-wins 超发。"""
    # 审查三轮（H）：env 覆盖值含 "~" 或相对段时未展开——数据与锁文件建成
    # 字面 "~/" 目录（版权中心战训同款）。统一 expanduser+resolve，两处同源
    _p = Path(os.path.expanduser(str(path or DEFAULT_FILE))).resolve()
    _p.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    with open(f"{_p}.lock", "a") as lf:  # OCR R131（M）：'w' 曾先截断后抢锁
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)  # 进程间互斥；锁内必须重新加载
        led = _ledger(_p)                        # 拿到锁后再 load（读最新账）
        with _LOCK:
            budgets = _budgets(led)
            b = budgets.get(name) or {"limit": int(limit), "window": float(window),
                                      "events": []}
            # OCR R131（H）：以持久化的 limit/window 为准（与 state 同口径）——
            # 曾按调用参数记账，查询与执行两套数字互相矛盾
            limit = int(b.get("limit", limit))
            window = float(b.get("window", window))
            now = time.time()
            events = _prune([float(t) for t in (b.get("events") or [])], float(window), now)
            used = len(events)
            # OCR R131（M）：cost=0 曾被 max(1,·) 抬成 1（免费探测被扣额）——
            # 0 = 不占额度的纯查询动作
            _c = max(0, int(cost))
            allowed = used + _c <= int(limit)
            if allowed and _c:
                events.extend([now] * _c)
            budgets[name] = {"limit": int(limit), "window": float(window), "events": events}
            led.save()
    used_after = len(budgets[name]["events"]) if allowed else used
    reset_in = 0.0
    if not allowed:
        # 时钟回拨后 events[0] 未必最早出窗——同 state() 口径取 min(events)
        reset_in = round(max(0.0, min(events) + float(window) - now), 1) if events else 0.0
    return {"name": name, "allowed": allowed, "used": used_after, "limit": int(limit),
            "remaining": max(0, int(limit) - used_after),
            "window_sec": float(window), "reset_in_sec": reset_in,
            "at": time.strftime("%Y-%m-%d %H:%M:%S")}
