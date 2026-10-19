# -*- coding: utf-8 -*-
"""batch1600 反馈落地测试：capture POST体透传 / capture2config 生成器 / pdf 安全边界 / 预算自动记账。"""
import json

import pytest

from universal_scraper.capture_gen import one_config, generate, _guess_records_path


# ---------------- P0-1: Python capture 透传 ----------------
def test_records_from_capture_all_passes_post_fields(tmp_path):
    from universal_scraper.fetchers import BrowserFetcher
    cap = [{"url": "https://api.x.com/notice", "method": "POST",
            "post_data": '{"pageNo":1,"pageSize":20}', "request_content_type": "application/json",
            "json": {"data": {"list": [{"a": 1}]}}},
           {"url": "https://api.x.com/notice", "method": "POST",
            "post_data": '{"pageNo":1,"pageSize":20}', "json": {"data": {"list": [{"a": 1}]}}},
           {"url": "https://api.x.com/notice", "method": "POST",
            "post_data": '{"pageNo":2,"pageSize":20}', "json": {"data": {"list": [{"a": 2}]}}}]
    (tmp_path / "capture_all.json").write_text(json.dumps(cap), encoding="utf-8")
    f = BrowserFetcher({"type": "browser", "url": "https://api.x.com/"}, {}, {}, tmp_path)
    recs = f._records_from_capture_all(tmp_path)
    assert len(recs) == 2                      # 同 URL 不同请求体不误并（batch1600 去重键修复）
    r0 = recs[0]
    assert r0["_method"] == "POST"
    assert "pageNo" in r0["_post_data"]
    assert "json" in r0["_request_content_type"]


# ---------------- P0-2: capture2config ----------------
def test_one_config_post_json_with_pagination():
    item = {"url": "https://api.x.com/gateway/search", "method": "POST",
            "post_data": '{"pageNo":1,"pageSize":20,"kw":"钢"}',
            "request_content_type": "application/json",
            "json": {"data": {"list": [{"id": 1}]}}}
    src = one_config(item, referer="https://x.com/list")
    assert src["method"] == "POST"
    assert src["json_body"]["pageNo"] == "{{page}}"     # 翻页参数自动模板化
    assert src["json_body"]["pageSize"] == 20
    assert src["headers"]["Referer"] == "https://x.com/list"
    assert "json" in src["headers"]["Content-Type"]


def test_one_config_get_url_pagination():
    item = {"url": "https://x.com/api/list?page=3&size=10", "method": "GET", "json": [{}]}
    src = one_config(item)
    assert "page={{page}}" in src["url"]


def test_one_config_skips_noise():
    assert one_config({"url": "https://x.com/app.js", "method": "GET", "json": {}}) is None
    assert one_config({"url": "https://x.com/logo.png", "json": {}}) is None


def test_guess_records_path():
    assert _guess_records_path({"data": {"list": [{"a": 1}]}}) == "data.list"
    assert _guess_records_path({"rows": [{"a": 1}]}) == "rows"
    assert _guess_records_path({"code": 0}) == ""


def test_generate_end_to_end(tmp_path):
    cap = [{"url": "https://api.x.com/notice", "method": "POST",
            "post_data": '{"currentPage":1}', "request_content_type": "application/json",
            "json": {"data": {"list": [{"a": 1}]}}},
           {"url": "https://x.com/static/app.js", "json": {}}]
    f = tmp_path / "capture_all.json"
    f.write_text(json.dumps(cap), encoding="utf-8")
    r = generate(f, out=str(tmp_path / "gen"), log=lambda *a: None)
    assert r["count"] == 1
    cfg = r["configs"][0]
    assert cfg["pagination"]["records_path"] == "data.list"
    assert (tmp_path / "gen" / "gen_configs.json").exists()  # 目录容错


# ---------------- P1-2: pdf_attach 安全边界 ----------------
def test_pdf_guard_rejects_private_and_scheme():
    from universal_scraper.pdf_attach import _guard_url
    with pytest.raises(ValueError):
        _guard_url("ftp://x.com/a.pdf")
    with pytest.raises(ValueError):
        _guard_url("/relative/path.pdf")
    with pytest.raises(ValueError):
        _guard_url("http://127.0.0.1/a.pdf")


def test_pdf_extract_tables_requires_valid_pdf(tmp_path):
    from universal_scraper.pdf_attach import extract_tables
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not-a-pdf")
    with pytest.raises(ValueError):
        extract_tables(bad)
    with pytest.raises(FileNotFoundError):
        extract_tables(tmp_path / "nope.pdf")


# ---------------- P1-1: 预算自动记账 ----------------
def test_budget_auto_mark_idempotent(tmp_path, monkeypatch):
    import universal_scraper.core as core_mod
    from universal_scraper import domain_budget as db
    monkeypatch.setattr(db, "DEFAULT_FILE", str(tmp_path / "b.json"))
    core_mod._budget_auto_mark("https://chinamoney.com/x", 421)
    assert db.check("chinamoney.com")["in_cooldown"] is True
    mtime1 = (tmp_path / "b.json").stat().st_mtime
    core_mod._budget_auto_mark("https://chinamoney.com/y", 403)  # 已冷却：不再写盘
    assert (tmp_path / "b.json").stat().st_mtime == mtime1
