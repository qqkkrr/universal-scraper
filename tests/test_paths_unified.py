#!/usr/bin/env python3
"""v1.32.1 收尾回归：运行态目录必须统一在同一个 home 下。

历史坑：budget/chain/health/monitor 落 ~/.universal_scraper（下划线），
cache/profile/ledger 落 ~/.universal-scraper（短横线）——排障时找错地方。
"""
import os
from pathlib import Path

from universal_scraper import action_budget, chain, domain_budget, health, monitor
from universal_scraper.home import HOME_DIR, state_path


def test_all_state_dirs_share_one_home():
    """各模块的运行态落在同一个 home 下（不再两套命名）。"""
    dirs = {
        "monitor": monitor.MONITOR_DIR,
        "chain": chain.STATE_DIR,
        "health": health.STATE_DIR,
        "action_budget": Path(action_budget.DEFAULT_FILE),
    }
    # domain_budget 支持环境变量覆盖，只在未覆盖时校验
    if not os.environ.get("UNIVERSAL_SCRAPER_LEDGER"):
        dirs["domain_budget"] = Path(domain_budget.DEFAULT_FILE)
    for name, p in dirs.items():
        assert Path(p).parent == HOME_DIR, f"{name} 不在 {HOME_DIR} 下：{p}"


def test_home_name_and_composition():
    """口径为 ~/.universal-scraper（短横线），且 state_path 只做拼接。"""
    assert HOME_DIR.name == ".universal-scraper"
    assert state_path("monitor") == HOME_DIR / "monitor"
    assert state_path("a", "b.json") == HOME_DIR / "a" / "b.json"
