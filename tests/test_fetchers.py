# -*- coding: utf-8 -*-
"""ScraplingFetcher 未安装降级 + HttpFetcher 本地抓取。"""
import sys
import pytest
from universal_scraper.protocols import Request
from universal_scraper.modules.fetchers import ScraplingFetcher

def _hide_scrapling(monkeypatch):
    saved = {k: v for k, v in sys.modules.items() if k == "scrapling" or k.startswith("scrapling.")}
    for k in list(sys.modules):
        if k == "scrapling" or k.startswith("scrapling."):
            del sys.modules[k]
    monkeypatch.setitem(sys.modules, "scrapling", None)

def test_scrapling_missing_raises_clear_error(monkeypatch):
    _hide_scrapling(monkeypatch)
    f = ScraplingFetcher({"type": "scrapling"}, {}, {})
    with pytest.raises(Exception) as ei:
        f.fetch(Request(url="https://example.com/"))
    msg = str(ei.value)
    assert "scrapling" in msg and "安装" in msg

def test_scrapling_type_in_whitelist():
    from universal_scraper.config import V3_SOURCE_TYPES
    assert "scrapling" in V3_SOURCE_TYPES


def test_http_fetcher_local_server():
    """核心 HttpFetcher 离线真测：本地 HTTP 服务器返回 HTML，能取回正文。"""
    import threading, http.server, functools
    from universal_scraper.modules.fetchers import HttpFetcher

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<html><body><div class='item'>local ok</div></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        f = HttpFetcher({"type": "http"}, {}, {})
        r = f.fetch(Request(url=f"http://127.0.0.1:{port}/list"))
        assert r.status == 200
        assert "local ok" in r.text
    finally:
        httpd.shutdown()
