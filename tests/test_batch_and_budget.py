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
    assert st == {"total": 3, "done": 2, "failed": 0, "blocked": 0, "nodata": 0,
                  "running": 0, "pending": 1}


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


# ---------------- batch1700 新增：优先级 / nodata / retry ----------------
def test_batch_priority_ordering(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps([
        {"id": 1, "text": "低优先", "status": "pending", "priority": 10},
        {"id": 2, "text": "高优先", "status": "pending", "priority": 1},
        {"id": 3, "text": "无优先", "status": "pending"},
    ], ensure_ascii=False), encoding="utf-8")
    q = BatchQueue(f)
    assert q.next()["id"] == 2          # priority 小者优先
    q.mark(2, "done")
    assert q.next()["id"] == 1          # 剩余中有 priority 的仍优先


def test_batch_nodata_distinct_from_failed(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps([{"id": 1419, "text": "x", "status": "pending"}], ensure_ascii=False), encoding="utf-8")
    q = BatchQueue(f)
    q.mark(1419, "nodata", "当日无披露,WebSearch已交叉验证")
    st = q.status()
    assert st["nodata"] == 1 and st["failed"] == 0   # 严格区分
    assert q.next() is None                            # 终态不回流


def test_batch_retry_resets_terminal_state(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps([{"id": 7, "text": "x", "status": "pending"}], ensure_ascii=False), encoding="utf-8")
    q = BatchQueue(f)
    q.mark(7, "failed", "登录墙")
    assert q.next() is None
    q.mark(7, "retry", "用户已登录,重试")
    assert q.next()["id"] == 7                       # 回到队列
    assert q.next()["attempts"] == 1                 # attempts 保留累计
    q.mark(7, "done")
    with pytest.raises(ValueError):                  # retry 不能用于 done
        q.mark(7, "retry")


def test_batch_preserves_rich_metadata(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps([{"id": 9, "text": "x", "status": "pending",
                              "custom_field": "保留我", "notes": ["a"]}], ensure_ascii=False), encoding="utf-8")
    q = BatchQueue(f)
    q.mark(9, "done", "ok")
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data[0]["custom_field"] == "保留我" and data[0]["notes"] == ["a"]
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
    assert st == {"total": 3, "done": 2, "failed": 0, "blocked": 0, "nodata": 0,
                  "running": 0, "pending": 1}


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


# ---------------- batch1700 新增：优先级 / nodata / retry ----------------
def test_batch_priority_ordering(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps([
        {"id": 1, "text": "低优先", "status": "pending", "priority": 10},
        {"id": 2, "text": "高优先", "status": "pending", "priority": 1},
        {"id": 3, "text": "无优先", "status": "pending"},
    ], ensure_ascii=False), encoding="utf-8")
    q = BatchQueue(f)
    assert q.next()["id"] == 2          # priority 小者优先
    q.mark(2, "done")
    assert q.next()["id"] == 1          # 剩余中有 priority 的仍优先


def test_batch_nodata_distinct_from_failed(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps([{"id": 1419, "text": "x", "status": "pending"}], ensure_ascii=False), encoding="utf-8")
    q = BatchQueue(f)
    q.mark(1419, "nodata", "当日无披露,WebSearch已交叉验证")
    st = q.status()
    assert st["nodata"] == 1 and st["failed"] == 0   # 严格区分
    assert q.next() is None                            # 终态不回流


def test_batch_retry_resets_terminal_state(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps([{"id": 7, "text": "x", "status": "pending"}], ensure_ascii=False), encoding="utf-8")
    q = BatchQueue(f)
    q.mark(7, "failed", "登录墙")
    assert q.next() is None
    q.mark(7, "retry", "用户已登录,重试")
    assert q.next()["id"] == 7                       # 回到队列
    assert q.next()["attempts"] == 1                 # attempts 保留累计
    q.mark(7, "done")
    with pytest.raises(ValueError):                  # retry 不能用于 done
        q.mark(7, "retry")


def test_batch_preserves_rich_metadata(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps([{"id": 9, "text": "x", "status": "pending",
                              "custom_field": "保留我", "notes": ["a"]}], ensure_ascii=False), encoding="utf-8")
    q = BatchQueue(f)
    q.mark(9, "done", "ok")
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data[0]["custom_field"] == "保留我" and data[0]["notes"] == ["a"]


# ---------------- batch2200 审查修复回归（容错取值 / 锁内重读） ----------------
def test_as_ts_coerces_string_and_ms():
    from universal_scraper.batch import _as_ts
    assert _as_ts("1796000000") == 1796000000.0      # 数字字符串
    assert _as_ts(None) == 0.0                        # 缺失 → 最旧
    assert _as_ts("yesterday") == 0.0                 # 垃圾 → 最旧
    assert _as_ts(1_800_000_000_000) == 1_800_000_000.0   # 毫秒 → 秒（1.8e12ms = 1.8e9s）


def test_prio_coerces_dirty_values():
    from universal_scraper.batch import _prio
    assert _prio({"priority": "high"}) == 100.0       # 垃圾字符串 → 缺省
    assert _prio({"priority": None}) == 100.0
    assert _prio({"priority": -1}) == -1.0
    assert _prio({}) == 100.0


def test_batch_claim_uses_lock_and_rereads(tmp_path, monkeypatch):
    """claim 锁内重读：文件被外部更新后，claim 拿到的是最新账本的下一项。"""
    f = tmp_path / "q.json"
    f.write_text(json.dumps([
        {"id": 1, "text": "a", "status": "done", "attempts": 1},
        {"id": 2, "text": "b", "status": "pending"},
    ], ensure_ascii=False), encoding="utf-8")
    from universal_scraper.batch import BatchQueue
    q = BatchQueue(f)
    # 模拟：另一个进程在 claim 前把 id=2 标 done
    data = json.loads(f.read_text(encoding="utf-8"))
    for it in data:
        if it["id"] == 2:
            it["status"] = "done"
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    it = q.claim()
    assert it is None                                 # 锁内重读发现无 pending


def test_batch_next_recycles_stale_running(tmp_path):
    import time as _t
    f = tmp_path / "q.json"
    stale_ms = int((_t.time() - 7200) * 1000)         # 2 小时前（毫秒）
    f.write_text(json.dumps([{"id": 1, "text": "x", "status": "running",
                              "running_ts": stale_ms}], ensure_ascii=False), encoding="utf-8")
    from universal_scraper.batch import BatchQueue
    q = BatchQueue(f)
    it = q.next()                                     # 毫秒时间戳曾被解析错导致回收永不生效
    assert it["id"] == 1 and it["status"] == "pending"
