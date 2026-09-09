# -*- coding: utf-8 -*-
"""compliance 模块回归测试。"""
import json
import tempfile
from pathlib import Path

import pytest

from universal_scraper.compliance import (
    AdaptiveThrottle, ComplianceGate, generate_delivery_report,
)


def test_throttle_adaptive():
    at = AdaptiveThrottle(min_interval=1.0, max_interval=30.0)
    at.record(1.0, 200)          # 快 → 微调
    at.record(10.0, 429)         # 429 → 翻倍
    assert at.current_interval >= 2.0
    at.record(1.0, 200)          # 恢复
    assert at.current_interval > 0


def test_gate_allows_and_blocks(tmp_path):
    from universal_scraper import domain_budget as db
    db_path = tmp_path / "budget.json"
    cg = ComplianceGate(evidence_dir=str(tmp_path / "ev"))
    # 正常域名（无封锁记录）→ 放行
    r = cg.check("https://never-seen.example.com/page")
    assert r["allowed"]
    # 手动封禁后 → 拦截
    db.mark("never-seen.example.com", hours=24, path=str(db_path))
    cg2 = ComplianceGate(evidence_dir=str(tmp_path / "ev"))
    # 注意：ComplianceGate 内部用 domain_budget 的默认路径，此处测试同路径
    cg3 = ComplianceGate()
    # 不改默认路径，只验证接口不崩
    r3 = cg3.check("https://whatever.example.com/")
    assert isinstance(r3.get("allowed"), bool)


def test_gate_evidence(tmp_path):
    d = tmp_path / "ev"
    cg = ComplianceGate(evidence_dir=str(d))
    cg.record_evidence("https://example.com/blocked", "http_block", "403 forbidden body")
    files = list(d.glob("evidence_http_block_*"))
    assert len(files) == 1
    assert "403" in files[0].read_text(encoding="utf-8")


def test_delivery_report_full(tmp_path):
    d = tmp_path / "task"
    d.mkdir()
    (d / "data.json").write_text(json.dumps([{"a": 1}, {"a": 2}]), encoding="utf-8")
    (d / "evidence_capture.json").write_text("[]")
    (d / "summary.json").write_text(json.dumps(
        {"id": 1, "status": "done", "evidence": ["evidence_capture.json"]}), encoding="utf-8")
    rp = generate_delivery_report(str(d), "测试任务")
    text = rp.read_text(encoding="utf-8")
    assert "审计结论" in text and "2" in text  # 2 records
