# -*- coding: utf-8 -*-
"""http_json 主抓取路径的运行时回归（pyflakes 抓获的 NameError 曾逃过全部测试）。

教训：测试套件没有覆盖 HttpFetcher.fetch_list 的 http_json 分支真实执行——
一行 `src.get(...)`（应为 self.source）的 NameError 逃过了 274 个测试。
本文件用本地 HTTP 服务器真实执行该路径，任何回归立刻爆。
"""
import http.server
import json
import threading

import pytest


class _JsonAPI(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.dumps({"data": {"list": [{"id": 1, "v": 10}], "total": 1}},
                          ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def api_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _JsonAPI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/api"
    srv.shutdown()


def _fetch(http_json_cfg, pagination):
    from universal_scraper.fetchers import HttpFetcher
    f = HttpFetcher(http_json_cfg, {}, {})
    return f.fetch_list(pagination)


def test_http_json_runtime_single_record(api_server):
    """single_record 模式：整个响应体作为一条记录（分支曾引用未定义变量崩溃）。"""
    cfg = {"type": "http_json", "method": "POST",
           "url": api_server, "single_record": True}
    rows = _fetch(cfg, {"strategy": "none", "max_pages": 1})
    assert len(rows) == 1
    assert rows[0]["data"]["list"][0]["id"] == 1


def test_http_json_runtime_records_path(api_server):
    """records_path 模式：常规数组抽取。"""
    cfg = {"type": "http_json", "method": "POST",
           "url": api_server, "single_record": False}
    rows = _fetch(cfg, {"strategy": "none", "records_path": "data.list", "max_pages": 1})
    assert len(rows) == 1
    assert rows[0]["id"] == 1


def test_http_json_runtime_no_src_leak():
    """源码层防回归：http_json 分支不得引用未定义的变量名。"""
    import inspect
    from universal_scraper.fetchers import HttpFetcher
    for method in (HttpFetcher.fetch_list, HttpFetcher._fetch_single_page):
        src = inspect.getsource(method)
        assert "src.get(" not in src, "fetchers 里残留未定义变量 src（曾致主路径 NameError）"
    src = inspect.getsource(HttpFetcher._fetch_single_page)
    assert 'self.source.get("single_record")' in src
