#!/usr/bin/env python3
"""v4 统一计划层回归：归一化、路由、与 legacy 执行结果一致。"""
from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from universal_scraper import plan as P
from universal_scraper import v4


def _serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _stop(srv):
    srv.shutdown()
    srv.server_close()


def _config(url, out_dir, **extra):
    cfg = {
        "name": "v4t",
        "source": {"type": "http_html", "url": url, "row_css": "div.i",
                   "fields": {"t": {"css": "span.t"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"t": {"from": "t"}}},
        "output": {"dir": str(out_dir), "base_name": "q", "formats": ["json"]},
        "anti_bot": {"min_interval": 0, "max_retries": 1, "http_backend": "curl_cffi"},
    }
    cfg.update(extra)
    return cfg


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><body><div class='i'><span class='t'>A</span></div></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_from_config_normalizes_and_isolates():
    cfg = _config("https://example.com/list", "/tmp/x")
    cfg["source"]["fields"]["t"]["css"] = "span.orig"
    plan = P.from_config(cfg, base_dir=Path("/tmp"))
    assert plan.schema_version == P.SCHEMA_VERSION
    assert plan.origin == "config" and plan.profile == "linear"
    assert plan.seeds == ["https://example.com/list"]
    assert plan.extraction["record_fields"] == {"t": {"from": "t"}}
    assert plan.base_dir == "/tmp"
    # 深拷贝：改配置不得污染计划
    cfg["source"]["fields"]["t"]["css"] = "changed"
    assert plan.raw["source"]["fields"]["t"]["css"] == "span.orig"


def test_from_task_normalizes(tmp_path):
    task = tmp_path / "t"
    task.mkdir()
    cfg = {"name": "task1", "start_urls": ["https://a.example/"],
           "rules": [{"match": "regex", "pattern": ".*", "parser": "default"}],
           "parsers": {"default": {"type": "html", "row_css": "div"}},
           "storage": {"type": "jsonl", "name": "q"},
           "queue": {"max_depth": 3},
           "pipelines": [{"type": "dedup", "key": "u"}]}
    (task / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    plan = P.from_task(task)
    assert plan.origin == "task" and plan.profile == "queue"
    assert plan.seeds == ["https://a.example/"]
    assert plan.rules and plan.parsers and plan.storage
    assert plan.task_dir == str(task)
    assert "queue" in plan.features() and "rules" in plan.features()


def test_plan_dump_is_json(tmp_path):
    plan = P.from_config(_config("https://e.example/", "/tmp/x"))
    p = P.dump(plan, tmp_path / "plan.json")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["schema_version"] == P.SCHEMA_VERSION
    assert data["origin"] == "config"
    assert data["features"][0] == "config"


def test_run_plan_rejects_bad_profile():
    plan = P.from_config(_config("https://e.example/", "/tmp/x"))
    plan.profile = "bogus"
    with pytest.raises(v4.PlanError):
        v4.run_plan(plan)


def test_v4_and_legacy_config_same_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")
    srv, port = _serve(_Handler)
    url = f"http://127.0.0.1:{port}/"
    try:
        cfg_a = _config(url, tmp_path / "a")
        cfg_b = copy.deepcopy(cfg_a)
        cfg_b["output"]["dir"] = str(tmp_path / "b")
        from universal_scraper.engine import _run_config_impl
        _run_config_impl(cfg_a)
        v4.run_config(cfg_b)
        a = json.loads((tmp_path / "a" / "q.json").read_text(encoding="utf-8"))
        b = json.loads((tmp_path / "b" / "q.json").read_text(encoding="utf-8"))
        assert a == b == [{"t": "A", "source_name": "v4t"}]
    finally:
        _stop(srv)


def test_v4_task_plan_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")
    srv, port = _serve(_Handler)
    task = tmp_path / "task"
    task.mkdir()
    cfg = {"name": "v4task", "start_urls": [f"http://127.0.0.1:{port}/"],
           "rules": [{"match": "regex", "pattern": ".*", "parser": "list", "follow": False}],
           "parsers": {"list": {"type": "html", "row_css": "div.i",
                                "fields": {"t": {"css": "span.t"}}}},
           "pipelines": [], "storage": {"type": "jsonl", "name": "q"},
           "output": {"dir": str(tmp_path / "out"), "base_name": "q"},
           "anti_bot": {"min_interval": 0, "max_retries": 1, "http_backend": "curl_cffi"}}
    (task / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    try:
        res = v4.run_task(task)
        assert res.get("total") == 1 and not res.get("nodata")
        rows = json.loads((tmp_path / "out" / "q.json").read_text(encoding="utf-8"))
        assert rows[0]["t"] == "A"
    finally:
        _stop(srv)
