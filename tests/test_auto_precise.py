# -*- coding: utf-8 -*-
"""自动精配助手 _try_auto_precise：mock generate_precise，离线确定性测试。"""
import universal_scraper.auto as auto
import universal_scraper.precise_auto as pa

CFG = {"start_urls": ["https://example.com/list"], "source": {"type": "http"}}


def _mock_ok(**kw):
    def f(description, url, config, limit, log):
        return {"ok": True, "host": "example.com", "name": "auto_precise_example_com",
                "rows": [{"title": "a"}, {"title": "b"}, {"title": "c"}],
                "files": {"json": "outputs/x.json"}, **kw}
    return f


def test_auto_precise_success(monkeypatch):
    logs = []
    monkeypatch.setattr(pa, "generate_precise", _mock_ok())
    out = auto._try_auto_precise("抓取列表", CFG, 10, logs.append)
    assert out is not None and out.get("done")
    assert out["result"]["total"] == 3
    assert "自动精配" in out["summary"]
    assert any("自动生成精配" in m for m in logs)


def test_auto_precise_failure_returns_none(monkeypatch):
    monkeypatch.setattr(pa, "generate_precise", lambda *a, **k: {"ok": False, "error": "探测失败"})
    out = auto._try_auto_precise("抓取列表", CFG, 10, lambda m: None)
    assert out is None


def test_auto_precise_exception_returns_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("LLM 挂了")
    monkeypatch.setattr(pa, "generate_precise", boom)
    out = auto._try_auto_precise("抓取列表", CFG, 10, lambda m: None)
    assert out is None


def test_auto_precise_no_url_returns_none():
    out = auto._try_auto_precise("x", {"start_urls": []}, 10, lambda m: None)
    assert out is None
