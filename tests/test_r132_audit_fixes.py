#!/usr/bin/env python3
"""v1.32.2 审计修复回归：编码/日期/表格/反爬/缓存/预算/resume/配置校验。"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from universal_scraper import core as C
from universal_scraper.config import ConfigError, validate
from universal_scraper.extractors import extract_tables


def _serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _stop(srv):
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------- 编码

def test_smart_decode_short_gbk_not_big5():
    raw = "中文测试".encode("gbk")
    assert "中文测试" in C.smart_decode(raw, {})


def test_smart_decode_short_big5():
    raw = "繁體中文測試".encode("big5")
    assert "繁體中文測試" in C.smart_decode(raw, {})


def test_smart_decode_shift_jis_short():
    raw = "日本語のテストです。".encode("shift_jis")
    assert "日本語" in C.smart_decode(raw, {})


# ---------------------------------------------------------------- 日期

def test_parse_relative_time_chinese_absolute():
    assert C.parse_relative_time("2024年1月2日") == "2024-01-02"
    assert C.parse_relative_time("2024年1月2日 10:30") == "2024-01-02 10:30"
    assert C.parse_relative_time("2024/1/2") == "2024-01-02"
    # 相对时间仍然正确
    assert C.parse_relative_time("3天前").startswith("20")


# ---------------------------------------------------------------- 表格

def test_extract_tables_rowspan_data_rows():
    html = ('''<table><tr><th>省份</th><th>城市</th><th>值</th></tr>
    <tr><td rowspan="2">A</td><td>x</td><td>1</td></tr>
    <tr><td>y</td><td>2</td></tr></table>''')
    rows = extract_tables(html)[0]
    assert rows[1] == {"省份": "A", "城市": "y", "值": "2"}


def test_extract_tables_colspan_data_rows():
    html = ('''<table><tr><th>省</th><th>市</th><th>值</th></tr>
    <tr><td colspan="2">合计</td><td>9</td></tr></table>''')
    rows = extract_tables(html)[0]
    assert rows[0] == {"省": "合计", "市": "", "值": "9"}


def test_extract_tables_nested_not_bleeding():
    html = ('''<table><tr><th>外</th></tr><tr><td>X<table><tr><th>内</th></tr>
    <tr><td>Y</td></tr></table></td></tr></table>''')
    tables = extract_tables(html)
    assert tables[0][0]["外"] == "X"
    assert tables[1][0]["内"] == "Y"


# ---------------------------------------------------------------- 反爬判型

def test_captcha_phrase_not_blocking_long_page():
    from universal_scraper.antibot import detect_block
    body = ("<html><body><form>请输入验证码<input name='c'></form>"
            + "<p>正常文章内容</p>" * 3000 + "</body></html>")
    assert detect_block(200, body, {}, "https://example.com/x")["kind"] == "none"


def test_captcha_phrase_still_blocks_short_challenge():
    from universal_scraper.antibot import detect_block
    body = "<html><body>访问验证，请输入验证码后继续</body></html>"
    assert detect_block(200, body, {}, "https://example.com/x")["kind"] == "captcha"


# ---------------------------------------------------------------- 配置校验

def _base_cfg(**over):
    cfg = {
        "name": "t",
        "source": {"type": "http_html", "url": "https://example.com/",
                   "row_css": "div.i", "fields": {"t": {"css": "span"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"t": {"from": "t"}}},
        "output": {"dir": "outputs", "base_name": "q", "formats": ["json"]},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    for path, val in over.items():
        node = cfg
        parts = path.split(".")
        for p in parts[:-1]:
            node = node[p]
        node[parts[-1]] = val
    return cfg


@pytest.mark.parametrize("patch", [
    {"anti_bot.timeout": None},
    {"pagination.max_pages": "abc"},
    {"source.headers": [1, 2]},
    {"source.fields": {"t": {"cs": "span"}}},
])
def test_validate_rejects_bad_nested_config(patch):
    with pytest.raises(ConfigError):
        validate(_base_cfg(**patch))


# ---------------------------------------------------------------- 缓存 / replay

def test_requests_backend_cache_then_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")
    counter = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            counter["n"] += 1
            body = b'{"data": [1, 2]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv, port = _serve(H)
    url = f"http://127.0.0.1:{port}/x"
    try:
        c = C.make_http_client({"min_interval": 0, "http_backend": "requests",
                                "cache_dir": str(tmp_path), "cache": True})
        assert c.get(url).get("ok") is True
        assert counter["n"] == 1
    finally:
        _stop(srv)
    rep = C.make_http_client({"min_interval": 0, "http_backend": "requests",
                              "cache_dir": str(tmp_path), "replay": True})
    r = rep.get(url)
    assert r.get("ok") is True and r.get("json") == {"data": [1, 2]}
    assert counter["n"] == 1


# ---------------------------------------------------------------- v2 预算 / resume

def _paged_server(pages=3):
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            n = int(self.path.rstrip("/").split("/p")[-1])
            rows = "".join(f'<div class="i"><span class="t">P{n}-{i}</span></div>'
                           for i in range(2))
            nxt = f'<a class="next" href="/p{n + 1}">n</a>' if n < pages else ""
            body = f"<html><body>{rows}{nxt}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return _serve(H), hits


def _v2_cfg(port, out_dir):
    return {
        "name": "t2",
        "source": {"type": "http_html", "url": f"http://127.0.0.1:{port}/p1",
                   "row_css": "div.i", "fields": {"t": {"css": "span.t"}}},
        "pagination": {"next_selector": "a.next", "max_pages": 5},
        "record": {"fields": {"t": {"from": "t"}}},
        "output": {"dir": str(out_dir), "base_name": "q", "formats": ["json"]},
        "anti_bot": {"min_interval": 0, "max_retries": 1, "http_backend": "curl_cffi"},
    }


def test_v2_resume_completed_iteration_skips_list(tmp_path, monkeypatch):
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")
    (srv, hits), seen = _paged_server(2)
    cfg = _v2_cfg(seen[0] if False else srv.server_address[1], tmp_path)
    from universal_scraper.engine import run_config
    try:
        run_config(cfg)
        assert len(seen) >= 2
        seen.clear()
        run_config(cfg, resume=True)
        assert seen == [], "已完成迭代的 --resume 不应重抓列表页"
    finally:
        _stop(srv)


def test_v2_budget_exports_partial_and_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")
    (srv, hits), seen = _paged_server(3)
    cfg = _v2_cfg(srv.server_address[1], tmp_path)
    from universal_scraper.engine import run_config
    try:
        with pytest.raises(C.MaxRequestsExceeded):
            run_config(cfg, max_requests=1)
        data = json.loads((tmp_path / "q.json").read_text(encoding="utf-8"))
        assert len(data) == 2
        assert list(tmp_path.glob(".checkpoint_*.json")), "预算中断必须落检查点"
    finally:
        _stop(srv)


# ---------------------------------------------------------------- v3 预算 requeue

def test_v3_budget_requeues_unfinished_url(tmp_path, monkeypatch):
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")
    (srv, hits), seen = _paged_server(3)
    task = tmp_path / "task"
    task.mkdir()
    cfg = {
        "name": "t3", "start_urls": [f"http://127.0.0.1:{srv.server_address[1]}/p1"],
        "queue": {"max_depth": 8, "max_requests": 10, "max_concurrency": 1},
        "rules": [{"match": "regex", "pattern": ".*", "parser": "list", "follow": True}],
        "parsers": {"list": {"type": "html", "row_css": "div.i",
                             "fields": {"t": {"css": "span.t"}},
                             "extract_links": {"allow": r"/p\d+", "deny": None}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": "q"},
        "output": {"dir": str(tmp_path / "out"), "base_name": "q"},
        "anti_bot": {"min_interval": 0, "max_retries": 1, "http_backend": "curl_cffi"},
    }
    (task / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    from universal_scraper.engine_v3 import run_task
    try:
        res = run_task(task, max_requests=2)
        assert res.get("stopped") == "max_requests"
        pending = json.loads((tmp_path / "out" / ".pending_t3.json").read_text(encoding="utf-8"))
        assert any("p3" in str(x.get("url", "")) for x in pending), pending
        seen.clear()
        run_task(task, resume=True)
        assert any("/p3" in h for h in seen), "resume 必须继续抓被硬闸打断的 p3"
    finally:
        _stop(srv)


# ---------------------------------------------------------------- 导出原子性

def test_export_rows_atomic_and_valid(tmp_path):
    C.export_rows([{"a": 1}, {"a": 2}], tmp_path, "q", formats=["json", "csv"])
    data = json.loads((tmp_path / "q.json").read_text(encoding="utf-8"))
    assert data == [{"a": 1}, {"a": 2}]
    assert not list(tmp_path.glob("*.tmp")), "导出后不应残留临时文件"
