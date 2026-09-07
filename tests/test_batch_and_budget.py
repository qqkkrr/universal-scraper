# -*- coding: utf-8 -*-
"""batch 队列 runner + domain_budget 封锁台账 离线测试（batch1401 战训代码化）。"""
import json

import pytest

from universal_scraper.batch import BatchQueue
from universal_scraper import domain_budget as db


# ---------------- BatchQueue ----------------
@pytest.fixture
def queue_file(tmp_path):
    f = tmp_path / "tasks.json"
    f.write_text(json.dumps([
        {"id": 1401, "text": "任务一", "status": "done", "attempts": 1, "result": "ok"},
        {"id": 1402, "text": "任务二", "status": "pending", "attempts": 0, "result": ""},
        {"id": 1403, "text": "任务三", "status": "pending", "attempts": 0, "result": ""},
    ], ensure_ascii=False), encoding="utf-8")
    return f


def test_next_returns_first_pending(queue_file):
    q = BatchQueue(queue_file)
    it = q.next()
    assert it["id"] == 1402


def test_mark_and_resume_across_restart(queue_file):
    q = BatchQueue(queue_file)
    q.mark(1402, "done", "usd=7.1023 ✓")
    q2 = BatchQueue(queue_file)  # 模拟崩溃后重启：断点续跑
    assert q2.next()["id"] == 1403
    st = q2.status()
    assert st == {"total": 3, "done": 2, "failed": 0, "blocked": 0, "pending": 1}


def test_all_done_signals_finished(queue_file):
    q = BatchQueue(queue_file)
    q.mark(1402, "done")
    q.mark(1403, "failed", "登录墙")
    assert q.next() is None
    assert q.status()["pending"] == 0


def test_mark_validates_status_and_id(queue_file):
    q = BatchQueue(queue_file)
    with pytest.raises(ValueError):
        q.mark(1402, "bogus")
    with pytest.raises(KeyError):
        q.mark(9999, "done")


def test_atomic_no_tmp_left(queue_file):
    q = BatchQueue(queue_file)
    q.mark(1402, "done")
    assert not list(queue_file.parent.glob("*.tmp"))


def test_missing_queue_file():
    with pytest.raises(FileNotFoundError):
        BatchQueue("/nonexistent/queue.json")


# ---------------- domain_budget ----------------
def test_budget_mark_check_cycle(tmp_path, monkeypatch):
    f = tmp_path / "b.json"
    monkeypatch.setattr(db, "DEFAULT_FILE", str(f))
    r = db.mark("chinamoney.com", hours=24, note="421 限流")
    assert "cooldown_hours" in r
    c = db.check("chinamoney.com")
    assert c["in_cooldown"] is True
    assert c["remaining_sec"] > 23 * 3600
    assert "421" in c["note"]
    # 无记录域名不受影响
    assert db.check("example.com")["in_cooldown"] is False
    # 台账跨实例持久
    c2 = db.check("chinamoney.com", path=f)
    assert c2["in_cooldown"] is True


def test_budget_listing(tmp_path, monkeypatch):
    f = tmp_path / "b.json"
    monkeypatch.setattr(db, "DEFAULT_FILE", str(f))
    db.mark("szse.cn", hours=24, note="断连")
    rows = db.listing()
    assert "szse.cn" in rows
    assert rows["szse.cn"]["in_cooldown"] is True
