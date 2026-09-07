# -*- coding: utf-8 -*-
"""双代理审查修复的回归测试（v1.12.2）：hours持久化/原始查询串/jsrecon门控/
stale result/PoolState mkdir/capture_all 崩溃出声。"""
import json

import pytest

from universal_scraper.capture_gen import one_config, generate


def test_budget_custom_hours_persists(tmp_path, monkeypatch):
    """P0-4：mark(hours=2) 后 check 必须按 2h 算（曾恒按 24h 谎报）。"""
    from universal_scraper import domain_budget as db
    monkeypatch.setattr(db, "DEFAULT_FILE", str(tmp_path / "b.json"))
    db.mark("x.com", hours=2)
    c = db.check("x.com")
    assert c["in_cooldown"] is True
    assert 7100 < c["remaining_sec"] <= 7200          # ~2h，不是 24h
    assert c["cooldown_hours"] == 2
    # 跨实例（重启）仍按 2h
    c2 = db.check("x.com", path=tmp_path / "b.json")
    assert 7100 < c2["remaining_sec"] <= 7200


def test_capture_gen_get_keeps_percent_encoding():
    """P1-3：GET 模板化不得把 %E5%85%AC 解码成裸中文再拼回。"""
    item = {"url": "https://x.com/api/list?page=2&kw=%E5%85%AC%E5%8F%B8&size=10",
            "method": "GET", "json": [{"a": 1}]}
    src = one_config(item)
    assert "page={{page}}" in src["url"]
    assert "%E5%85%AC%E5%8F%B8" in src["url"]          # 编码原样保留
    assert "公司" not in src["url"]


def test_jsrecon_gates_on_ok(monkeypatch):
    """P0-3：失败响应（text 装着错误信息）必须报 error，不能'成功形态 0 候选'。"""
    import universal_scraper.core as core_mod
    class Blocked:
        def get(self, url):
            return {"ok": False, "status": 403, "text": "HTTP 403: blocked by WAF", "body": b""}
    monkeypatch.setattr(core_mod, "make_http_client", lambda cfg: Blocked())
    from universal_scraper.quick import js_recon
    r = js_recon("https://example.com/list.html")
    assert "error" in r and "403" in r["error"]


def test_batch_stale_result_overwritten(tmp_path):
    """P1-5：done 后 fail 且不带 result，旧成功文案必须清空。"""
    from universal_scraper.batch import BatchQueue
    f = tmp_path / "q.json"
    f.write_text(json.dumps([{"id": 1, "text": "x", "status": "pending"}], ensure_ascii=False), encoding="utf-8")
    q = BatchQueue(f)
    q.mark(1, "done", "ok=v1")
    q.mark(1, "failed", "")                            # retry 流程里常见的空 result
    assert json.loads(f.read_text(encoding="utf-8"))[0]["result"] == ""


def test_batch_mark_from_pending_counts_once(tmp_path):
    """终态重复 mark 不虚增 attempts（仅 pending→终态计一次）。"""
    from universal_scraper.batch import BatchQueue
    f = tmp_path / "q.json"
    f.write_text(json.dumps([{"id": 1, "text": "x", "status": "pending"}], ensure_ascii=False), encoding="utf-8")
    q = BatchQueue(f)
    q.mark(1, "done", "a")
    q.mark(1, "done", "b")                             # 重复标记
    assert q.items[0]["attempts"] == 1


def test_pool_state_save_creates_parent(tmp_path):
    """P1-1：首跑新目录 PoolState.save 不崩。"""
    from universal_scraper.proxy_fetch import PoolState
    st = PoolState(tmp_path / "deep" / "new" / "pool.json")
    st.mark("http://1.1.1.1:1", "alive")               # 不抛 FileNotFoundError
    assert (tmp_path / "deep" / "new" / "pool.json").exists()


def test_capture_all_corrupt_logs_not_silent(tmp_path, caplog):
    """P0-2：capture_all.json 损坏要出 WARN，不是静默 0 条。"""
    from universal_scraper.fetchers import BrowserFetcher
    (tmp_path / "capture_all.json").write_text("{truncated", encoding="utf-8")
    f = BrowserFetcher({"type": "browser", "url": "https://x.com/"}, {}, {}, tmp_path)
    recs = f._records_from_capture_all(tmp_path)
    assert recs == []
    # 输出里必须能看到解析失败提示（log 走 stderr）
    import io, contextlib
    buf_out, buf_err = io.StringIO(), io.StringIO()
    (tmp_path / "capture_all.json").write_text("{truncated", encoding="utf-8")
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        f._records_from_capture_all(tmp_path)
    assert "解析失败" in (buf_out.getvalue() + buf_err.getvalue())


def test_capture_generate_friendly_errors(tmp_path):
    """P2-3：损坏文件/错误结构返回 error 字典而非裸栈。"""
    bad = tmp_path / "bad.json"
    bad.write_text("{broken", encoding="utf-8")
    r = generate(bad, log=lambda *a: None)
    assert "error" in r and "解析失败" in r["error"]
    weird = tmp_path / "weird.json"
    weird.write_text('{"requests": []}', encoding="utf-8")
    r2 = generate(weird, log=lambda *a: None)
    assert r2["count"] == 0                            # dict 无 items → 0 份（有 log 提示）


def test_batch_rejects_non_list_queue(tmp_path):
    f = tmp_path / "q.json"
    f.write_text('{"not": "a list"}', encoding="utf-8")
    from universal_scraper.batch import BatchQueue
    with pytest.raises(ValueError):
        BatchQueue(f)
