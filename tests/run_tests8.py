#!/usr/bin/env python3
"""第八轮高难度回归（15 个，聚焦 v3 内部）：
插件协议全覆盖（fetcher/parser/pipeline/storage/middleware）、HttpFetcher 缓存、
域名限速实测、robots Crawl-delay 实测、循环链接防护、--limit 软上限、
sitemap 404 空种子、损坏 state/pending 恢复、webhook on_error、桥错误协议。
用法: python3 tests/run_tests8.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from mock_sites import Handler  # noqa: E402
from universal_scraper.engine_v3 import run_task  # noqa: E402
from universal_scraper.config import validate_task  # noqa: E402

PY = sys.executable
TMP = ROOT / "outputs" / ".test_tmp"
PASS, FAIL = [], []


def make_task(name: str, cfg: dict, modules: dict = None) -> Path:
    d = TMP / name
    (d / "modules").mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    for fn, content in (modules or {}).items():
        (d / "modules" / fn).write_text(content, encoding="utf-8")
    return d


def cleanup(name: str):
    import glob, shutil
    for pat in (f"outputs/{name}*", f"outputs/items/{name}*",
                f"outputs/.state_{name}.json", f"outputs/.pending_{name}.json",
                f"outputs/.run_{name}.log", f"outputs/.seen_{name}.txt",
                f"outputs/.checkpoint_{name}_*.json", f"outputs/.snapshot_{name}.json"):
        for f in glob.glob(str(ROOT / pat)):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
    shutil.rmtree(TMP / name, ignore_errors=True)


def read_items(name: str) -> list:
    fp = ROOT / "outputs" / "items" / f"{name}.jsonl"
    if not fp.exists():
        return []
    return [json.loads(x) for x in fp.read_text(encoding="utf-8").splitlines() if x.strip()]


def read_json(name: str) -> list:
    fp = ROOT / "outputs" / f"{name}.json"
    return json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  ❌ {name}  {detail}")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    print(f"mock server: {base}\n")

    # 1. 自定义 fetcher
    print("[1/15] 插件：自定义 fetcher.py")
    name = "q1"
    cfg = {
        "name": name, "start_urls": ["http://x/fake"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "custom"},
        "rules": [{"match": "contains", "pattern": "/fake", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"t": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    mod = '''from universal_scraper.protocols import BaseFetcher, Response, Request
class Fetcher(BaseFetcher):
    name = "custom"
    def fetch(self, req):
        return Response(request=req, status=200,
                        text="<html><body><div class='item'><span class='t'>plug-fetcher</span></div></body></html>")
'''
    d = make_task(name, cfg, {"fetcher.py": mod})
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and items[0]["t"] == "plug-fetcher", f"items={items}")
    finally:
        cleanup(name)

    # 2. 自定义多 parser
    print("[2/15] 插件：自定义 parser.py 多类路由")
    name = "q2"
    cfg = {
        "name": name, "start_urls": [f"{base}/dual.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/dual", "parser": "a"},
                  {"match": "regex", "pattern": "/du", "parser": "b"}],
        "parsers": {"a": {}, "b": {}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    mod = '''import re
from universal_scraper.protocols import BaseParser, ParseResult
class AParser(BaseParser):
    name = "a"
    def parse(self, resp, ctx):
        m = re.search(r"class='t'>(.*?)<", resp.text)
        return ParseResult(items=[{"who": "A", "val": m.group(1) if m else ""}])
class BParser(BaseParser):
    name = "b"
    def parse(self, resp, ctx):
        m = re.search(r"class='s'>(.*?)<", resp.text)
        return ParseResult(items=[{"who": "B", "val": m.group(1) if m else ""}])
'''
    d = make_task(name, cfg, {"parser.py": mod})
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and items[0]["who"] == "A" and items[0]["val"] == "first-parser",
              f"items={items}")
    finally:
        cleanup(name)

    # 3. 自定义 pipeline
    print("[3/15] 插件：自定义 pipeline.py")
    name = "q3"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    mod = '''from universal_scraper.protocols import BasePipeline
class Pipeline(BasePipeline):
    def process(self, item):
        if "title" in item:
            item["title"] = item["title"].upper()
        return item
'''
    d = make_task(name, cfg, {"pipeline.py": mod})
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 2 and all(i["title"].startswith("P1-TITLE") for i in items),
              f"items={[i['title'] for i in items]}")
    finally:
        cleanup(name)

    # 4. 自定义 storage
    print("[4/15] 插件：自定义 storage.py")
    name = "q4"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [], "storage": {"type": "custom", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    mod = '''import json, os
from universal_scraper.protocols import BaseStorage
class Storage(BaseStorage):
    name = "custom"
    def __init__(self, config, task_vars):
        self.dir = config.get("dir", "outputs")
        self.name = config.get("name", "items")
    def open(self, name):
        os.makedirs(self.dir, exist_ok=True)
        self.f = open(os.path.join(self.dir, f"{name}.custom"), "w", encoding="utf-8")
    def write(self, item):
        self.f.write(json.dumps(item, ensure_ascii=False) + "\\n")
    def close(self):
        self.f.close()
'''
    d = make_task(name, cfg, {"storage.py": mod})
    try:
        r = run_task(d)
        fp = ROOT / "outputs" / "items" / f"{name}.custom"
        ok = fp.exists() and len(fp.read_text(encoding="utf-8").splitlines()) == 2
        check(name, r["total"] == 2 and ok, f"total={r['total']} custom_file={ok}")
    finally:
        cleanup(name)
        try:
            os.remove(ROOT / "outputs" / "items" / f"{name}.custom")
        except FileNotFoundError:
            pass

    # 5. 自定义 middleware 注入请求头
    print("[5/15] 插件：自定义 middleware.py 注入请求头")
    name = "q5"
    cfg = {
        "name": name, "start_urls": [f"{base}/hdrcheck"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/hdrcheck", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"t": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}],
        "middleware": [],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    mod = '''from universal_scraper.protocols import BaseMiddleware
class Middleware(BaseMiddleware):
    def on_request(self, req, ctx):
        req.headers = dict(req.headers or {})
        req.headers["X-Plug"] = "1"
        return req
'''
    d = make_task(name, cfg, {"middleware.py": mod})
    try:
        # 补 /hdrcheck 路由
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and items and items[0]["t"] == "hdr-ok",
              f"total={r['total']} items={items}")
    finally:
        cleanup(name)

    # 6. HttpFetcher 缓存
    print("[6/15] HttpFetcher source.cache 同 URL 命中缓存")
    from universal_scraper.modules.fetchers import HttpFetcher
    from universal_scraper.protocols import Request
    f = HttpFetcher({"type": "http", "cache": True}, {},
                    {"min_interval": 0.0, "max_retries": 1, "http_backend": "auto"})
    r1 = f.fetch(Request(url=f"{base}/counter.html"))
    r2 = f.fetch(Request(url=f"{base}/counter.html"))
    ok = "cnt-1" in r1.text and "cnt-1" in r2.text  # 第二次命中缓存，计数仍为 1
    check("q6_cache", ok, f"r1={r1.text[:30]} r2={r2.text[:30]}")

    # 7. 域名限速实测
    print("[7/15] 域名限速跨 worker 实测（min_interval=0.5）")
    name = "q7"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html", f"{base}/p2.html"],
        "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 4},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.5, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        t0 = time.time()
        r = run_task(d)
        dt = time.time() - t0
        check(name, r["total"] == 4 and dt >= 0.45, f"total={r['total']} dt={dt:.2f}s")
    finally:
        cleanup(name)

    # 8. robots Crawl-delay 实测
    print("[8/15] robots Crawl-delay 实测（≥1s）")
    name = "q8"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html", f"{base}/p3.html"],
        "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 4},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1, "respect_robots": True},
    }
    d = make_task(name, cfg)
    try:
        t0 = time.time()
        r = run_task(d)
        dt = time.time() - t0
        check(name, r["total"] == 4 and dt >= 0.9, f"total={r['total']} dt={dt:.2f}s")
    finally:
        cleanup(name)

    # 9. 循环链接防护
    print("[9/15] 循环链接防护（cycle1 ↔ cycle2）")
    name = "q9"
    cfg = {
        "name": name, "start_urls": [f"{base}/cycle1.html"],
        "queue": {"max_depth": 5, "max_requests": 50, "max_concurrency": 2},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/cycle", "parser": "page"}],
        "parsers": {"page": {"type": "html", "row_css": ".item", "fields": {"t": {"css": "span.t"}},
                             "extract_links": {"allow": "/cycle"}}},
        "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        ts = sorted(i["t"] for i in items)
        check(name, r["fetched"] == 2 and ts == ["c1", "c2"],
              f"fetched={r['fetched']} ts={ts}")
    finally:
        cleanup(name)

    # 10. --limit 页面粒度软上限
    print("[10/15] --limit 页面粒度软上限（limit=3 → 4 条不挂）")
    name = "q10"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html", f"{base}/p2.html", f"{base}/p3.html"],
        "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d, limit=3)
        # limit 是页面粒度软上限：p1 两条后检查 2>=3 否 → p2 → 4 条后停（不挂、无错）
        check(name, r["total"] == 4 and r["errors"] == 0, f"total={r['total']} errors={r['errors']}")
    finally:
        cleanup(name)

    # 11. sitemap 404 + 空种子
    print("[11/15] sitemap 404 + 空种子正常完成")
    name = "q11"
    cfg = {
        "name": name, "start_urls": [],
        "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http", "sitemap": f"{base}/nonexistent.xml"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"t": {"css": "span.t"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        check(name, r["total"] == 0 and r["errors"] == 0, f"total={r['total']} errors={r['errors']}")
    finally:
        cleanup(name)

    # 12. 损坏 state/pending 文件恢复
    print("[12/15] 损坏 state/pending 文件 → resume 不崩")
    name = "q12"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html", f"{base}/p2.html"],
        "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        run_task(d)
        (ROOT / "outputs" / f".state_{name}.json").write_text("not-json{{{", encoding="utf-8")
        (ROOT / "outputs" / f".pending_{name}.json").write_text("broken[[", encoding="utf-8")
        r = run_task(d, resume=True)
        # 损坏状态文件 → 不崩、回退全量重抓（p1+p2 = 4 条）
        check(name, r["total"] == 4 and r["errors"] == 0, f"total={r['total']} errors={r['errors']}")
    finally:
        cleanup(name)

    # 13. webhook on_error 捕获失败
    print("[13/15] webhook on_error 捕获失败请求")
    captured = []
    class CapHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            captured.append(json.loads(self.rfile.read(n).decode()))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        def log_message(self, *a):
            pass
    cap = ThreadingHTTPServer(("127.0.0.1", 0), CapHandler)
    threading.Thread(target=cap.serve_forever, daemon=True).start()
    name = "q13"
    cfg = {
        "name": name, "start_urls": ["http://127.0.0.1:1/x"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/x", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"t": {"css": "span.t"}}}},
        "pipelines": [],
        "middleware": [{"on": "error", "action": "webhook",
                        "url": f"http://127.0.0.1:{cap.server_address[1]}/err"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1, "timeout": 2},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        events = [c for c in captured if c.get("event") == "error"]
        check(name, r["errors"] >= 1 and len(events) >= 1, f"errors={r['errors']} posts={len(events)}")
    finally:
        cleanup(name)
        cap.shutdown()

    # 14. 桥 error 协议
    print("[14/15] 桥 error 协议 → 任务抛 RuntimeError")
    name = "q14"
    cfg = {
        "name": name, "start_urls": [],
        "source": {"type": "bridge", "bridge": "bad.cjs", "bridge_params": {}},
        "record": {"fields": {}}, "pipelines": [],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0},
    }
    bad = 'console.log(JSON.stringify({type:"error", message:"boom"}));'
    d = make_task(name, cfg, {"bad.cjs": bad})
    try:
        try:
            run_task(d)
            check("q14_bridge_error", False, "未抛错")
        except RuntimeError as e:
            check("q14_bridge_error", "boom" in str(e), f"err={e}")
    finally:
        cleanup(name)

    # 15. 桥非零退出码
    print("[15/15] 桥非零退出码 → 任务抛 RuntimeError")
    name = "q15"
    cfg = {
        "name": name, "start_urls": [],
        "source": {"type": "bridge", "bridge": "bad2.cjs", "bridge_params": {}},
        "record": {"fields": {}}, "pipelines": [],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0},
    }
    bad2 = "process.exit(3);"
    d = make_task(name, cfg, {"bad2.cjs": bad2})
    try:
        try:
            run_task(d)
            check("q15_bridge_exit", False, "未抛错")
        except RuntimeError as e:
            check("q15_bridge_exit", "3" in str(e), f"err={e}")
    finally:
        cleanup(name)

    print(f"\n===== 结果: {len(PASS)}/15 通过 =====")
    if FAIL:
        print("失败:", FAIL)
        srv.shutdown()
        return 1
    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
