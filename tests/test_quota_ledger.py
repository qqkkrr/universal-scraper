# -*- coding: utf-8 -*-
"""quota_ledger 离线测试：冷却账本 / 切片分工 / 边际余量 / 失败路由（科研管理战训代码化）。"""
from pathlib import Path

import pytest

from universal_scraper.quota_ledger import (
    QuotaLedger, assign_chunks, worker_chunk, margin_cap, route_failure,
)


def test_touch_and_cooldown(tmp_path):
    led = QuotaLedger(tmp_path / "l.json", windows={"resource": 60})
    led.touch("article:22393")
    assert led.in_cooldown("article:22393")
    assert 0 < led.remaining("article:22393") <= 60
    # 未触碰的资源不受影响
    assert not led.in_cooldown("article:99999")
    # 冷却后可再请求（模拟时间前进）
    led.data["resource"]["article:22393"] -= 120
    assert not led.in_cooldown("article:22393")


def test_ledger_survives_restart_and_corruption(tmp_path):
    f = tmp_path / "l.json"
    led = QuotaLedger(f)
    led.touch("ip:1.2.3.4", dim="ip")
    led2 = QuotaLedger(f)
    assert led2.in_cooldown("ip:1.2.3.4", dim="ip")
    # 写坏文件 → 自动重建不崩溃
    f.write_text("{broken", encoding="utf-8")
    led3 = QuotaLedger(f)
    assert led3.data == {}


def test_filter_ready(tmp_path):
    led = QuotaLedger(tmp_path / "l.json", windows={"resource": 60})
    led.touch("a")
    ready = led.filter_ready(["a", "b", "c"])
    assert ready == ["b", "c"]


def test_assign_chunks_disjoint_cover():
    items = list(range(10))
    chunks = assign_chunks(items, 4)
    assert [x for c in chunks for x in c] == items        # 全覆盖不丢
    assert len(chunks) == 3
    # worker 取段互不相交（队首轰击修复的核心性质）
    assert worker_chunk(items, 0, 4) == [0, 1, 2, 3]
    assert worker_chunk(items, 1, 4) == [4, 5, 6, 7]
    assert worker_chunk(items, 2, 4) == [8, 9]
    # 越界回绕
    assert worker_chunk(items, 3, 4) == [0, 1, 2, 3]


def test_margin_cap():
    assert margin_cap(20, 0.75) == 15      # 战例数值：墙 20 → 上限 15
    assert margin_cap(16) == 12
    with pytest.raises(ValueError):
        margin_cap(0)
    with pytest.raises(ValueError):
        margin_cap(20, 1.5)


def test_route_failure():
    assert route_failure("dead") == "rotate_worker_no_retry"
    assert route_failure("quota") == "blacklist_until_window"
    assert route_failure("global") == "all_silence_wait"
    assert route_failure("disk") == "pause_without_burn"
    assert route_failure("???") == "blacklist_until_window"   # 未知按配额保守处理


def test_atomic_write_no_tmp(tmp_path):
    led = QuotaLedger(tmp_path / "l.json")
    led.touch("x")
    assert not list(tmp_path.glob("*.tmp"))
