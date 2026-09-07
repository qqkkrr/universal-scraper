#!/usr/bin/env python3
"""🚧 域名礼貌预算/封锁台账（batch1401 战训：chinamoney 421、szse 断连是 24h 级封禁，
playbook 只有抽象原则没有工程化载体）。基于 quota_ledger 的持久化结构，
按域名记录 [最后事件时间, 冷却小时数, 备注]——小时数随台账持久（审查修复：
v1.12.1 前 hours 只改运行时窗口、check/list 恒按 24h 计算，48h 封禁被谎报成已解封）。

用法:
  python3 -m universal_scraper.cli budget --mark chinamoney.com --hours 24 --note "421 限流"
  python3 -m universal_scraper.cli budget --check chinamoney.com     # 冷却中? 剩余秒?
  python3 -m universal_scraper.cli budget --list                     # 全部台账
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict

from .quota_ledger import QuotaLedger

DEFAULT_FILE = "~/.universal_scraper/domain_budget.json"
DEFAULT_HOURS = 24.0


def ledger(path: str | Path | None = None) -> QuotaLedger:
    return QuotaLedger(path or DEFAULT_FILE, windows={"domain": 86400})


def _meta(led: QuotaLedger) -> Dict[str, Dict]:
    return led.data.setdefault("budget_meta", {})


def mark(domain: str, hours: float = DEFAULT_HOURS, note: str = "",
         path: str | Path | None = None) -> Dict:
    """登记一次封锁/超预算事件。hours 随台账持久：check/list 按
    [最后事件 + hours] 计算，绝不回退到硬编码窗口。"""
    led = ledger(path)
    led.touch(f"domain:{domain}", dim="domain")
    _meta(led)[domain] = {"hours": float(hours), "note": note[:200]}
    led.save()
    until = time.time() + hours * 3600
    return {"domain": domain, "cooldown_hours": hours,
            "until": time.strftime("%m-%d %H:%M", time.localtime(until))}


def check(domain: str, path: str | Path | None = None) -> Dict:
    led = ledger(path)
    key = f"domain:{domain}"
    meta = _meta(led).get(domain, {})
    hours = meta.get("hours", DEFAULT_HOURS)
    last_ts = led.last(key, dim="domain")
    remaining = int(last_ts + hours * 3600 - time.time())
    return {"domain": domain, "in_cooldown": remaining > 0,
            "remaining_sec": max(0, remaining),
            "cooldown_hours": hours, "note": meta.get("note", "")}


def listing(path: str | Path | None = None) -> Dict:
    led = ledger(path)
    meta = _meta(led)
    out: Dict[str, Dict] = {}
    for key, ts in led.data.get("domain", {}).items():
        d = key.replace("domain:", "", 1)
        m = meta.get(d, {})
        hours = m.get("hours", DEFAULT_HOURS)
        remaining = int(ts + hours * 3600 - time.time())
        out[d] = {"in_cooldown": remaining > 0,
                  "remaining_sec": max(0, remaining),
                  "cooldown_hours": hours,
                  "note": m.get("note", ""),
                  "last_event": time.strftime("%m-%d %H:%M", time.localtime(ts))}
    return out
