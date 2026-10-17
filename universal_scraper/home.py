#!/usr/bin/env python3
"""运行态目录的唯一真源（v1.32.1 收尾）。

历史遗留：两套命名并存——
  ~/.universal_scraper/  → budget / chain / health / monitor
  ~/.universal-scraper/  → http_cache / daemon_profile / xhs_profile / quota_ledger
排障时容易找错地方，也容易让"封锁台账"和"动作台账"落在不同目录。
现在统一到 ~/.universal-scraper/（旧目录已迁移，原位置留兼容软链）。

环境变量 UNIVERSAL_SCRAPER_HOME 可整体重定向（A/B 测试 / 多代理隔离）。
"""
from __future__ import annotations

import os
from pathlib import Path

HOME_DIR = Path(
    os.environ.get("UNIVERSAL_SCRAPER_HOME")
    or str(Path.home() / ".universal-scraper")
).expanduser()


def state_path(*parts: str) -> Path:
    """运行态文件/子目录的绝对路径（不主动建目录，与旧常量同语义）。"""
    return HOME_DIR.joinpath(*parts)
