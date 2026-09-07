#!/usr/bin/env python3
"""🚧 域名礼貌预算/封锁台账（batch1401 战训：chinamoney 421、szse 断连是 24h 级封禁，
playbook 只有抽象原则没有工程化载体）。复用 quota_ledger 的冷却账本，dim 固定 "domain"。

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


def ledger(path: str | Path | None = None) -> QuotaLedger:
    return QuotaLedger(path or DEFAULT_FILE, windows={"domain": 86400})


def mark(domain: str, hours: float = 24.0, note: str = "",
         path: str | Path | None = None) -> Dict:
    """登记一次封锁/超预算事件（touch + 备注）。同域 24h 内重复 mark 只刷新时间戳。"""
    led = ledger(path)
    led.windows["domain"] = int(hours * 3600)
    led.touch(f"domain:{domain}", dim="domain")
    # 备注挂在 data 里（账本结构允许附加字段）
    led.data.setdefault("notes", {})[f"domain:{domain}"] = note[:200]
    led.save()
    return {"domain": domain, "cooldown_hours": hours,
            "until": time.strftime("%m-%d %H:%M", time.localtime(time.time() + hours * 3600))}


def check(domain: str, path: str | Path | None = None) -> Dict:
    led = ledger(path)
    key = f"domain:{domain}"
    cool = led.in_cooldown(key, dim="domain")
    return {"domain": domain, "in_cooldown": cool,
            "remaining_sec": led.remaining(key, dim="domain"),
            "note": led.data.get("notes", {}).get(key, "")}


def listing(path: str | Path | None = None) -> Dict:
    led = ledger(path)
    out = {}
    for key, ts in led.data.get("domain", {}).items():
        d = key.replace("domain:", "", 1)
        out[d] = {"in_cooldown": led.in_cooldown(key, dim="domain"),
                  "remaining_sec": led.remaining(key, dim="domain"),
                  "note": led.data.get("notes", {}).get(key, ""),
                  "last_event": time.strftime("%m-%d %H:%M", time.localtime(ts))}
    return out
