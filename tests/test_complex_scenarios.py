
#!/usr/bin/env python3
"""10 个复杂场景的集成回归（全本地、无外网）。

覆盖：v4/legacy 线性分页+详情、预算 resume、v3 队列任务、笛卡尔积、
verify 多格式、编码/表格、snapshot、code800 熔断、合规分级、详情阶段预算。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


class H(BaseHTTPRequestHandler):
    hits = {}

    def send(self, code, data, ctype="text/html; charset=utf-8"):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        H.hits[p] = H.hits.get(p, 0) + 1
        if p in ("/list", "/list5"):
            page = int((q.get("page") or ["1"])[0])
            last = 3 if p == "/list" else 5
            rows = []
            if 1 <= page <= last:
                for n in range((page - 1) * 2 + 1, (page - 1) * 2 + 3):
                    rows.append("<div class='i'><span class='id'>" + str(n) +
                                "</span><a href='/detail/" + str(n) + "'>T" + str(n) + "</a></div>")
            self.send(200, "<html><body>" + "".join(rows) + "</body></html>")
        elif p.startswith("/detail/"):
            n = p.rsplit("/", 1)[-1]
            self.send(200, "<html><body><h1>D" + n +
                      "</h1><div class='body'>B" + n + "</div></body></html>")
        elif p == "/v3/index":
            self.send(200, "<html><body><h1>IDX</h1>"
                      "<a href='/v3/detail/1'>one</a><a href='/v3/detail/2'>two</a></body></html>")
        elif p.startswith("/v3/detail/"):
            n = p.rsplit("/", 1)[-1]
            self.send(200, "<html><body><div class='body'>V" + n + "</div></body></html>")
        elif p == "/cart":
            kw = (q.get("kw") or ["x"])[0]
            yr = (q.get("year") or ["0"])[0]
            row = "<div class='i'><span class='t'>" + kw + "-" + yr + "</span></div>"
            self.send(200, "<html><body>" + row + row + "</body></html>")
        elif p == "/gbk":
            self.send(200, "<html><body><h1>中文测试</h1></body></html>".encode("gbk"),
                      "text/html; charset=gbk")
        elif p == "/snap/ok":
            self.send(200, "<html><body><h1>快照标题</h1><p>正文内容</p></body></html>")
        elif p == "/snap/bad":
            self.send(500, "boom")
        elif p == "/code800/list":
            rows = "".join("<div class='i'><a href='/code800/detail/" + str(n) +
                           "'>D" + str(n) + "</a></div>" for n in range(1, 13))
            self.send(200, "<html><body>" + rows + "</body></html>")
        elif p.startswith("/code800/detail/"):
            self.send(200, '{"code":800,"msg":"busy"}', "application/json; charset=utf-8")
        else:
            self.send(404, "not found")

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def base_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:" + str(srv.server_address[1])
    srv.shutdown()
    srv.server_close()


def cli(args, cwd):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["US_ALLOW_PRIVATE"] = "1"
    env["NO_COLOR"] = "1"
    return subprocess.run([sys.executable, "-m", "universal_scraper.cli"] + args,
                          cwd=str(cwd), env=env, text=True, capture_output=True, timeout=120)


def cfg(out, url, name, pages=4):
    return {
        "name": name,
        "source": {"type": "http_html", "url": url, "row_css": "div.i",
                   "fields": {"id": {"css": "span.id"}, "title": {"css": "a"},
                              "url": {"css": "a", "attr": "href"}}},
        "pagination": {"strategy": "page_param", "page_param": "page", "start": 1,
                       "max_pages": pages},
        "record": {"fields": {"id": {"from": "id"}, "title": {"from": "title"},
                              "url": {"from": "url"}}},
        "output": {"dir": str(out), "base_name": name, "formats": ["json", "csv"]},
        "anti_bot": {"min_interval": 0, "max_retries": 1, "http_backend": "curl_cffi"},
    }


def write(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_rows(out, name):
    p = Path(out)
    for fn in (name + ".json", name + ".jsonl", "items/" + name + ".jsonl"):
        f = p / fn
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        rows = ([json.loads(x) for x in text.splitlines() if x.strip()]
                if fn.endswith(".jsonl") else json.loads(text))
        if rows:
            return rows
    return []


def test_complex_01_v4_linear_detail_plan(tmp_path, base_url):
    out = tmp_path / "t1"; out.mkdir()
    c = cfg(out, base_url + "/list", "q1")
    c["detail"] = {"enabled": True, "url_field": "url",
                   "extract": [{"name": "body", "type": "css_text", "selector": ".body"}],
                   "concurrency": 2, "interval": 0}
    c["record"]["fields"]["body"] = {"from": "body"}
    p = out / "c.json"; write(p, c)
    plan = out / "plan.json"
    r = cli(["run", "--config", str(p), "--plan-out", str(plan)], out)
    rows = read_rows(out, "q1")
    assert r.returncode == 0, r.stderr
    assert len(rows) == 6 and all(x.get("body") for x in rows)
    assert json.loads(plan.read_text(encoding="utf-8"))["profile"] == "linear"
    # legacy 同 id 集
    out2 = tmp_path / "t1l"; out2.mkdir()
    c2 = json.loads(p.read_text(encoding="utf-8")); c2["output"]["dir"] = str(out2)
    p2 = out2 / "c.json"; write(p2, c2)
    r2 = cli(["run", "--config", str(p2), "--engine", "legacy"], out2)
    rows2 = read_rows(out2, "q1")
    assert r2.returncode == 0
    assert {x["id"] for x in rows} == {x["id"] for x in rows2}
    assert cli(["verify", "--file", str(out / "q1.json")], out).returncode == 0


def test_complex_02_budget_resume(tmp_path, base_url):
    out = tmp_path / "t2"; out.mkdir()
    p = out / "c.json"; write(p, cfg(out, base_url + "/list5", "q2", pages=5))
    r = cli(["run", "--config", str(p), "--max-requests", "3"], out)
    assert r.returncode == 4
    assert any(out.glob("q2*.json"))
    r2 = cli(["run", "--config", str(p), "--resume", "--max-requests", "100"], out)
    rows = read_rows(out, "q2")
    assert r2.returncode == 0 and len(rows) == 10
    assert len({x["id"] for x in rows}) == 10


def test_complex_03_v3_task_resume(tmp_path, base_url):
    task = tmp_path / "task"; task.mkdir()
    out = tmp_path / "out"; out.mkdir()
    c = {
        "name": "t3", "start_urls": [base_url + "/v3/index"],
        "queue": {"max_depth": 2, "max_requests": 20, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/v3/index", "parser": "index"},
                  {"match": "contains", "pattern": "/v3/detail", "parser": "detail"}],
        "parsers": {"index": {"type": "html", "fields": {"t": {"css": "h1"}},
                              "extract_links": {"allow": "/v3/detail/\\d+", "same_domain": True}},
                    "detail": {"type": "html", "fields": {"body": {"css": ".body"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": "t3"},
        "output": {"dir": str(out), "base_name": "t3"},
        "anti_bot": {"min_interval": 0, "max_retries": 1, "http_backend": "curl_cffi"},
    }
    (task / "config.json").write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    r = cli(["run", "--task", str(task), "--engine", "v4"], tmp_path)
    assert r.returncode == 0 and len(read_rows(out, "t3")) == 3
    # 预算中断 + resume
    out2 = tmp_path / "out2"; out2.mkdir()
    task2 = tmp_path / "task2"; task2.mkdir()
    c2 = json.loads(json.dumps(c)); c2["name"] = "t3b"; c2["output"]["dir"] = str(out2)
    (task2 / "config.json").write_text(json.dumps(c2, ensure_ascii=False), encoding="utf-8")
    r1 = cli(["run", "--task", str(task2), "--max-requests", "2"], tmp_path)
    r2 = cli(["run", "--task", str(task2), "--resume", "--max-requests", "20"], tmp_path)
    rows = read_rows(out2, "t3")
    assert r1.returncode == 4 and r2.returncode == 0 and len(rows) == 3


def test_complex_04_cartesian_url_dedup(tmp_path, base_url):
    out = tmp_path / "t4"; out.mkdir()
    c = {
        "name": "q4",
        "iterate": {"vars": {"kw": ["智算", "算力"], "year": [2024, 2025]}},
        "source": {"type": "http_html", "url": base_url + "/cart?kw={{kw}}&year={{year}}",
                   "row_css": "div.i", "fields": {"t": {"css": "span.t"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"t": {"from": "t"}}},
        "pipeline": [{"type": "dedup", "key": "t"}],
        "output": {"dir": str(out), "base_name": "q4", "formats": ["json"]},
        "anti_bot": {"min_interval": 0, "max_retries": 1, "http_backend": "curl_cffi"},
    }
    p = out / "c.json"; write(p, c)
    r = cli(["run", "--config", str(p)], out)
    rows = read_rows(out, "q4_合并")
    assert r.returncode == 0 and len(rows) == 4
    assert {x["t"] for x in rows} == {"智算-2024", "智算-2025", "算力-2024", "算力-2025"}
    assert "去重后" in r.stderr


def test_complex_05_verify_formats(tmp_path):
    csv = tmp_path / "v.csv"
    csv.write_text("id,name,city\n1,A,\n2,B,\n3,C,\n", encoding="utf-8")
    assert cli(["verify", "--file", str(csv), "--expect-empty", "city",
                "--expect-count", "3"], tmp_path).returncode == 0
    assert "源站本就不提供" in cli(["verify", "--file", str(csv)], tmp_path).stdout
    j = tmp_path / "v.json"
    j.write_text(json.dumps({"products": [{"id": 1}, {"id": 2}],
                             "sellers": [{"sid": "a"}]}), encoding="utf-8")
    assert cli(["verify", "--file", str(j)], tmp_path).returncode == 1
    assert cli(["verify", "--file", str(j), "--data-key", "products"], tmp_path).returncode == 0
    jl = tmp_path / "v.jsonl"
    jl.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    assert cli(["verify", "--file", str(jl)], tmp_path).returncode == 0


def test_complex_06_encoding_table(tmp_path, base_url):
    from universal_scraper.core import _decode_body
    from universal_scraper.extractors import extract_tables
    assert _decode_body("中文测试".encode("gbk"),
                        {"content-type": "text/html; charset=gbk"}) == "中文测试"
    tables = extract_tables("<table><tr><td rowspan='2'>A</td><td>B</td></tr>"
                            "<tr><td>C</td></tr></table>")
    assert [list(r.values()) for t in tables for r in t] == [["A", "B"], ["A", "C"]]
    r = cli(["fetch", base_url + "/gbk", "--json"], tmp_path)
    assert r.returncode == 0 and "中文测试" in (r.stdout + r.stderr)


def test_complex_07_snapshot_resume(tmp_path, base_url):
    out = tmp_path / "snap"; out.mkdir()
    r = cli(["snapshot", base_url + "/snap/ok", base_url + "/snap/bad",
             "--out", str(out), "--interval", "0"], tmp_path)
    assert r.returncode == 0
    meta = json.loads((out / "page_0001.json").read_text(encoding="utf-8"))
    md = (out / "page_0001.md").read_text(encoding="utf-8")
    assert meta["status"] == 200 and "<h1>" not in md and "快照标题" in md
    assert oct((out / "page_0001.md").stat().st_mode & 0o777) == "0o600"
    before = H.hits.get("/snap/ok", 0)
    assert cli(["snapshot", base_url + "/snap/ok", "--out", str(out),
                "--interval", "0", "--resume"], tmp_path).returncode == 0
    assert H.hits.get("/snap/ok", 0) == before


def test_complex_08_detail_block_breaker(tmp_path, base_url):
    out = tmp_path / "t8"; out.mkdir()
    c = cfg(out, base_url + "/code800/list", "q8", pages=1)
    c["pagination"] = {"strategy": "none"}
    c["detail"] = {"enabled": True, "url_field": "url",
                   "extract": [{"name": "body", "type": "json", "path": "data"}],
                   "concurrency": 4, "interval": 0, "block_codes": [800],
                   "block_backoff": 0.01, "max_consecutive_blocks": 3}
    p = out / "c.json"; write(p, c)
    r = cli(["run", "--config", str(p)], out)
    blocked = out / "blocked.json"
    assert r.returncode == 5 and blocked.exists()
    assert json.loads(blocked.read_text(encoding="utf-8"))["status"] == 800


def test_complex_09_compliance_levels(tmp_path):
    g = tmp_path / "g.json"; write(g, {"name": "g", "source": {"type": "http_html",
                                                                "url": "https://e/"}})
    a = tmp_path / "a.json"; write(a, {"name": "a", "source": {"type": "http_html",
                                                                "url": "https://e/",
                                                                "fields": {"用户ID": {"css": "b"}}}})
    r = tmp_path / "r.json"; write(r, {"name": "r", "source": {"type": "http_html",
                                                                "url": "https://e/"},
                                       "note": "x-sign 绕过风控"})
    assert cli(["compliance", "--config", str(g), "--purpose", "p", "--scale", "1"], tmp_path).returncode == 0
    assert cli(["compliance", "--config", str(a), "--purpose", "p", "--scale", "1"], tmp_path).returncode == 0
    assert cli(["compliance", "--config", str(r), "--purpose", "p", "--scale", "1"], tmp_path).returncode == 2
    assert cli(["compliance", "--config", str(a), "--purpose", "p", "--scale", "1",
                "--strict"], tmp_path).returncode == 1


def test_complex_10_detail_budget_resume(tmp_path, base_url):
    out = tmp_path / "t10"; out.mkdir()
    c = cfg(out, base_url + "/list", "q10", pages=3)
    c["detail"] = {"enabled": True, "url_field": "url",
                   "extract": [{"name": "body", "type": "css_text", "selector": ".body"}],
                   "concurrency": 1, "interval": 0}
    c["record"]["fields"]["body"] = {"from": "body"}
    p = out / "c.json"; write(p, c)
    r1 = cli(["run", "--config", str(p), "--max-requests", "4"], out)
    r2 = cli(["run", "--config", str(p), "--resume", "--max-requests", "100"], out)
    rows = read_rows(out, "q10")
    assert r1.returncode == 4
    assert r2.returncode == 0 and len(rows) == 6 and all(x.get("body") for x in rows)
