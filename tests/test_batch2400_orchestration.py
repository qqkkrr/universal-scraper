# -*- coding: utf-8 -*-
"""batch2400（第五批 600 项终版）反馈回归：verify --dir / batch claim / guide 生成器。"""
import json

import pytest

from universal_scraper.batch import BatchQueue
from universal_scraper.verify import verify_dir
from universal_scraper import agent_guide


# ---------------- verify --dir 通用目录审计 ----------------
def _make_task_dir(tmp_path, *, with_summary=True, records=True, evidence=True):
    d = tmp_path / "task_2061"
    d.mkdir(parents=True)
    data = [{"orgName": f"机构{i}", "addr": "某市"} for i in range(76)] if records else []
    (d / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (d / "evidence_capture.json").write_text("[]", encoding="utf-8")
    (d / "report.md").write_text("# 报告", encoding="utf-8")
    if with_summary:
        (d / "summary.json").write_text(json.dumps(
            {"id": 2061, "status": "done", "evidence": ["evidence_capture.json"]},
            ensure_ascii=False), encoding="utf-8")
    return d


def test_verify_dir_ok(tmp_path):
    d = _make_task_dir(tmp_path)
    r = verify_dir(str(d), log=lambda *a: None)
    assert r["verdict"] == "ok"
    assert r["total_records"] == 76
    assert r["evidence"]["summary_json"] and r["evidence"]["report_md"]
    assert r["evidence"]["evidence_files"] == ["evidence_capture.json"]


def test_verify_dir_missing_summary_ref_partial(tmp_path):
    d = _make_task_dir(tmp_path, with_summary=False)
    (d / "summary.json").write_text(json.dumps(
        {"id": 2061, "status": "done", "evidence": ["evidence_not_exist.html"]},
        ensure_ascii=False), encoding="utf-8")
    r = verify_dir(str(d), log=lambda *a: None)
    assert r["verdict"] == "partial"                 # 有数据但证据引用缺失
    assert "evidence_not_exist.html" in r["missing_evidence_refs"]


def test_verify_dir_empty_and_no_data(tmp_path):
    d = _make_task_dir(tmp_path, records=False)
    r = verify_dir(str(d), log=lambda *a: None)
    assert r["verdict"] == "empty"
    empty = tmp_path / "task_empty"
    empty.mkdir()
    assert verify_dir(str(empty), log=lambda *a: None)["verdict"] == "no_data_files"


def test_verify_dir_csv_support(tmp_path):
    d = tmp_path / "task_csv"
    d.mkdir()
    (d / "data.csv").write_text("name,price\n甲,10\n乙,20\n", encoding="utf-8")
    r = verify_dir(str(d), log=lambda *a: None)
    assert r["verdict"] == "ok" and r["total_records"] == 2


def test_verify_dir_rejects_missing(tmp_path):
    r = verify_dir(str(tmp_path / "nope"), log=lambda *a: None)
    assert r.get("ok") is False


# ---------------- batch claim / stale 回收 / running 状态 ----------------
def test_batch_claim_marks_running(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps([{"id": 1, "text": "x", "status": "pending"}], ensure_ascii=False), encoding="utf-8")
    q = BatchQueue(f)
    it = q.claim()
    assert it["status"] == "running" and "running_ts" in it
    assert q.next() is None                          # running 不参与派发
    assert q.status()["running"] == 1


def test_batch_stale_running_recycled(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps([{"id": 1, "text": "x", "status": "running",
                              "running_ts": 1}], ensure_ascii=False), encoding="utf-8")  # ts=1970 → 过期
    q = BatchQueue(f)
    it = q.next()                                    # 触发 stale 回收
    assert it["id"] == 1 and it["status"] == "pending"


def test_batch_mark_over_running_terminal(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps([{"id": 1, "text": "x", "status": "running", "running_ts": 9}], ensure_ascii=False), encoding="utf-8")
    q = BatchQueue(f)
    q.mark(1, "done", "ok")
    it = json.loads(f.read_text(encoding="utf-8"))[0]
    assert it["status"] == "done" and "running_ts" not in it


def test_batch_retry_accepts_running(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps([{"id": 1, "text": "x", "status": "running", "running_ts": 9}], ensure_ascii=False), encoding="utf-8")
    q = BatchQueue(f)
    q.mark(1, "retry")
    assert q.next()["status"] == "pending"


# ---------------- guide 生成器 ----------------
def test_agent_guide_emits(tmp_path):
    from universal_scraper.agent_guide import emit
    p = emit(tmp_path / "AGENT_GUIDE.md")
    text = p.read_text(encoding="utf-8")
    for kw in ("铁律", "claim", "evidence_", "并发", "nodata", "verify --dir"):
        assert kw in text, f"规范缺关键词: {kw}"
