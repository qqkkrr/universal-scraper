#!/usr/bin/env python3
"""v1.33.0 能力扩展回归：合规预检 / 页面快照 / verify 任意 JSON。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from universal_scraper import compliance_preflight as compliance
from universal_scraper import snapshot as snap
from universal_scraper.verify import verify_file


def test_compliance_green_for_plain_config():
    cfg = {"name": "p", "source": {"type": "http_html", "url": "https://e/",
                                    "row_css": "div", "fields": {"价格": {"css": "b"}}}}
    rep = compliance.assess(cfg, purpose="公开价格监测")
    assert rep["level"] == "green" and not rep["personal_data"]


def test_compliance_personal_data_amber_and_actions():
    cfg = {"name": "p", "source": {"type": "http_html", "url": "https://e/",
                                    "row_css": "div",
                                    "fields": {"用户ID": {"css": "a"},
                                               "昵称": {"css": "b"}}}}
    rep = compliance.assess(cfg, purpose="用户画像", scale=1500)
    assert rep["level"] == "amber" and rep["personal_data"]
    assert any("最小必要" in a for a in rep["actions"])


def test_compliance_bypass_is_red():
    cfg = {"name": "p", "source": {"type": "http_html", "url": "https://e/"},
           "note": "需要 x-sign / x-mini-wua / x5sec 与风控降权"}
    rep = compliance.assess(cfg)
    assert rep["level"] == "red" and rep["bypass_detected"]


def test_compliance_non_public_red_even_without_pii():
    cfg = {"name": "p", "source": {"type": "http_html", "url": "https://e/",
                                    "fields": {"价格": {"css": "b"}}}}
    rep = compliance.assess(cfg, public_only=False)
    assert rep["level"] == "red"


def test_compliance_sensitive_amber_when_fully_controlled():
    cfg = {"name": "p", "source": {"type": "http_html", "url": "https://e/",
                                    "fields": {"手机号": {"css": "b"}}}}
    rep = compliance.assess(cfg, authorized=True, legal_basis="同意",
                            retention_days=30, scale=10)
    assert rep["level"] == "amber" and rep["personal_data"]


def test_verify_arbitrary_json_lists(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"products": [{"id": 1}, {"id": 2}],
                             "sellers": [{"sid": "a"}]}), encoding="utf-8")
    # 多候选：必须显式指定，避免静默选错表
    rep = verify_file(str(p))
    assert rep.get("ok") is False and "data-key" in rep.get("error", "")
    rep = verify_file(str(p), data_key="products")
    assert rep.get("ok") is True and rep.get("rows") == 2
    # 嵌套点路径
    q = tmp_path / "n.json"
    q.write_text(json.dumps({"result": {"products": [{"id": 1}]}}), encoding="utf-8")
    rep = verify_file(str(q))
    assert rep.get("ok") is True and rep.get("data_key") == "result.products"


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><body><h1>Hello</h1><p>Snapshot</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_snapshot_saves_meta_html_md(tmp_path, monkeypatch):
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rep = snap.capture([f"http://127.0.0.1:{srv.server_address[1]}/"],
                           tmp_path / "snap", interval=0)
        assert rep["captured"] == 1 and rep["ok"] == 1 and not rep["errors"]
        files = {p.name for p in (tmp_path / "snap").iterdir()}
        assert {"page_0001.json", "page_0001.html", "page_0001.md"} <= files
        meta = json.loads((tmp_path / "snap" / "page_0001.json").read_text(encoding="utf-8"))
        assert meta["status"] == 200 and meta["html_file"] == "page_0001.html"
        assert "Hello" in (tmp_path / "snap" / "page_0001.md").read_text(encoding="utf-8")
    finally:
        srv.shutdown()
        srv.server_close()
