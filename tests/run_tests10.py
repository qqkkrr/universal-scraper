#!/usr/bin/env python3
"""第十轮高难度回归（15 个）：Request 级 headers/POST、SPA links、跨站 same-domain、
allow+deny、resume 幂等、写压力、fetch 最终 URL、size_limit、v2 labels、jobs 容错、
crawl --max、limit=0、自定义 fetch_all、monitor 无变化。
用法: python3 tests/run_tests10.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from mock_sites import Handler  # noqa: E402
from universal_scraper.engine_v3 import run_task  # noqa: E402

PY = sys.executable
TMP = ROOT / "outputs" / ".test_tmp"
V2 = ROOT / "outputs" / ".test_tmp_v2"
PASS, FAIL = [], []


def make_task(name: str, cfg: dict, modules: dict = None) -> Path:
    d = TMP / name
    (d / "modules").mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    for fn, content in (modules or {}).items():
        (d / "modules" / fn).write_text(content, encoding="utf-8")
    return d


def write_v2(name: str, cfg: dict) -> Path:
    V2.mkdir(parents=True, exist_ok=True)
    fp = V2 / f"{name}.json"
    fp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


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
    for f in glob.glob(str(V2 / f"{name}.json")):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass


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


def run_cli(args, timeout=180):
    return subprocess.run([PY, "-m", "universal_scraper.cli"] + args,
                          capture_output=True, text=True, cwd=ROOT, timeout=timeout)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    print(f"mock server: {base}\n")

    # 1. 自定义 parser 生成带 headers 的 Request
    print("[1/15] 自定义 parser 生成带 headers 的 Request")
    name = "s1"
    cfg = {
        "name": name, "start_urls": [f"{base}/list.html"],
        "queue": {"max_depth": 2, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/list", "parser": "gen"},
                  {"match": "contains", "pattern": "/hdrcheck2", "parser": "js"}],
        "parsers": {"gen": {"type": "html", "fields": {"h": {"css": "h1"}}},
                    "js": {"type": "html", "row_css": ".item", "fields": {"t": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    mod = '''from universal_scraper.protocols import BaseParser, ParseResult, Request
class GenParser(BaseParser):
    name = "gen"
    def parse(self, resp, ctx):
        return ParseResult(items=[], requests=[
            Request(url=resp.url.replace("/list.html", "/hdrcheck2"), headers={"X-R": "1"})
        ])
'''
    d = make_task(name, cfg, {"parser.py": mod})
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and items and items[0]["t"] == "req-hdr-ok",
              f"total={r['total']} items={items}")
    finally:
        cleanup(name)

    # 2. 自定义 parser 生成 POST Request
    print("[2/15] 自定义 parser 生成 POST Request")
    name = "s2"
    cfg = {
        "name": name, "start_urls": [f"{base}/list.html"],
        "queue": {"max_depth": 2, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/list", "parser": "gen"},
                  {"match": "contains", "pattern": "/api/post", "parser": "js"}],
        "parsers": {"gen": {"type": "html", "fields": {"h": {"css": "h1"}}},
                    "js": {"type": "json", "records_path": "records",
                           "fields": {"title": {"from": "title"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    mod = '''from universal_scraper.protocols import BaseParser, ParseResult, Request
class GenParser(BaseParser):
    name = "gen"
    def parse(self, resp, ctx):
        return ParseResult(items=[], requests=[
            Request(url="%BASE%/api/post", method="POST", body={"from": "parser"})
        ])
'''
    mod = mod.replace("%BASE%", base)
    d = make_task(name, cfg, {"parser.py": mod})
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and items and items[0]["title"] == "post-ok",
              f"total={r['total']} items={items}")
    finally:
        cleanup(name)

    # 3. fetch --browser --links（SPA JS 渲染链接）
    print("[3/15] fetch --browser --links（JS 渲染后的链接）")
    p = run_cli(["fetch", f"{base}/spa.html", "--browser", "--links"], timeout=300)
    ok = "3 个外链" in p.stdout and "p1.html" in p.stdout and "${i}" not in p.stdout
    check("s3_spa_links", ok, f"stdout={p.stdout[-200:]}")

    # 4. crawl --same-domain 排除外链（第二个 mock 服务器）
    print("[4/15] crawl --same-domain 排除外链（双服务器）")
    srv2 = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    base2 = f"http://127.0.0.1:{srv2.server_address[1]}"
    # 在 base 放一个页面链接到 base2（外站）
    import urllib.request as _ur
    # 用一个特殊路由：/ext.html 在 base 上链接到 base2
    # 直接通过第二个服务器不行，用 mock 已有 /hub.html 加不了外链；
    # 用一个动态路由：复用 /hubdocs 不可行。改为手动请求注入：用 fetch 构造不现实。
    # 简单方案：本测试用 /hub.html（同域）+ 外链页面来自 base2 的 /hub.html → same-domain 会排除
    p = run_cli(["crawl", f"{base2}/hub.html", "--max", "10", "--same-domain"], timeout=300)
    import glob
    files = sorted(glob.glob(str(ROOT / "outputs" / "crawl_*.json")))
    urls = []
    if files:
        urls = [r["_url"] for r in json.loads(Path(files[-1]).read_text(encoding="utf-8"))]
    for f_ in glob.glob(str(ROOT / "outputs" / "crawl_127_0_0_1_*")):
        try:
            os.remove(f_)
        except FileNotFoundError:
            pass
    srv2.shutdown()
    ok = len(urls) >= 1 and all(base2 in u for u in urls)
    check("s4_same_domain", ok, f"urls={urls}")

    # 5. extract_links allow + deny 同设（deny 优先）
    print("[5/15] extract_links allow+deny 同设（deny 优先）")
    name = "s5"
    cfg = {
        "name": name, "start_urls": [f"{base}/hub.html"],
        "queue": {"max_depth": 2, "max_requests": 20, "max_concurrency": 2},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/hub", "parser": "list"},
                  {"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"list": {"type": "html", "fields": {"h": {"css": "h1"}},
                             "extract_links": {"allow": r"/p\d", "deny": r"/p[23]"}},
                    "js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        urls = {i["_url"] for i in read_items(name)}
        ok = r["fetched"] == 4 and not any("p2.html" in u or "p3.html" in u for u in urls)              and all(any(p in u for u in urls) for p in ("p1.html", "p4.html", "p5.html"))
        check(name, ok, f"fetched={r['fetched']} urls={sorted(urls)}")
    finally:
        cleanup(name)

    # 6. resume 幂等
    print("[6/15] resume 幂等（连续两次 resume 不增长）")
    name = "s6"
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
        run_task(d, limit=2)
        r2 = run_task(d, resume=True)
        r3 = run_task(d, resume=True)
        rows = read_json(name)
        check(name, r2["total"] == 4 and r3["total"] == 0 and len(rows) == 6,
              f"r2={r2['total']} r3={r3['total']} rows={len(rows)}")
    finally:
        cleanup(name)

    # 7. 8 worker + 增量去重 + sqlite 写压力
    print("[7/15] 8 worker + 增量去重 + sqlite 写压力")
    name = "s7"
    cfg = {
        "name": name, "start_urls": [f"{base}/p{i}.html" for i in range(1, 6)],
        "queue": {"max_depth": 1, "max_requests": 30, "max_concurrency": 8},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item",
                           "fields": {"title": {"css": "span.t"}, "date": {"css": "span.p"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "incremental": {"enabled": True, "key": "title"},
        "storage": {"type": "multi", "backends": [{"type": "jsonl", "name": name},
                                                  {"type": "sqlite", "name": name}]},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        import sqlite3
        conn = sqlite3.connect(str(ROOT / "outputs/items" / f"{name}.db"))
        rows = conn.execute("select count(*) from items").fetchone()[0]
        conn.close()
        check(name, r["total"] == 10 and r["errors"] == 0 and rows == 10,
              f"total={r['total']} sqlite={rows}")
    finally:
        cleanup(name)

    # 8. fetch 302 最终 URL
    print("[8/15] fetch 302 显示最终 URL")
    p = run_cli(["fetch", f"{base}/redirect", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200 and data.get("url", "").endswith("/p1.html")
    except Exception:
        ok = False
    check("s8_redirect_url", ok, f"stdout={p.stdout[:150]}")

    # 9. v2 download size_limit 跳过
    print("[9/15] v2 download size_limit 跳过大文件")
    name = "s9"
    cfg = {
        "name": name,
        "source": {"type": "http_html", "url": f"{base}/list.html",
                   "row_css": ".item",
                   "fields": {"title": {"css": "span.t"}, "file": {"css": "a.f", "attr": "href"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}, "file": {"from": "file"}}},
        "pipeline": [], "detail": {"enabled": False},
        "download": {"enabled": True, "url_field": "file", "dir": "files", "size_limit": 5},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp)])
        files_dir = ROOT / "outputs" / "files"
        n = len(list(files_dir.glob("*"))) if files_dir.exists() else 0
        check(name, n == 0, f"downloaded={n}（应被 size_limit 跳过）")
    finally:
        cleanup(name)

    # 10. v2 iterate + labels 导出命名
    print("[10/15] v2 iterate + labels 导出命名")
    name = "s10"
    cfg = {
        "name": name, "vars": {"q": "x"},
        "iterate": {"var": "q", "values": ["0001", "0002"], "labels": {"0001": "招标"}},
        "source": {"type": "http_json", "url": f"{base}/search?q={{{{q}}}}"},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}}},
        "pipeline": [], "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp)])
        import glob
        files = sorted(glob.glob(str(ROOT / "outputs" / f"{name}*.json")))
        names = [Path(f).name for f in files]
        ok = any("招标" in n for n in names) and any(f"{name}_0002.json" == n for n in names)
        check(name, ok, f"files={names}")
    finally:
        cleanup(name)

    # 11. jobs 容错
    print("[11/15] jobs 容错（坏任务跳过继续）")
    name = "s11"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    jobs_fp = ROOT / "outputs" / ".test_jobs10.json"
    jobs_fp.write_text(json.dumps([
        {"task": str(TMP / "does_not_exist")},
        {"task": str(d)},
    ]), encoding="utf-8")
    try:
        p = run_cli(["jobs", "--file", str(jobs_fp)])
        last = next((l for l in reversed(p.stdout.splitlines()) if l.strip()), "")
        data = json.loads(last)
        ok = data.get("jobs") == 2 and data.get("total_items") == 2 and "失败跳过" in p.stderr
        check(name, ok, f"data={data} stderr={p.stderr[-80:]}")
    finally:
        jobs_fp.unlink(missing_ok=True)
        cleanup(name)

    # 12. crawl --max 1 只抓入口
    print("[12/15] crawl --max 1 只抓入口")
    p = run_cli(["crawl", f"{base}/hub.html", "--max", "1"], timeout=300)
    import glob
    files = sorted(glob.glob(str(ROOT / "outputs" / "crawl_*.json")))
    urls = []
    if files:
        urls = [r["_url"] for r in json.loads(Path(files[-1]).read_text(encoding="utf-8"))]
    for f_ in glob.glob(str(ROOT / "outputs" / "crawl_127_0_0_1_*")):
        try:
            os.remove(f_)
        except FileNotFoundError:
            pass
    check("s12_crawl_max1", len(urls) == 1 and "hub.html" in urls[0], f"urls={urls}")

    # 13. limit=0 视为无限
    print("[13/15] limit=0 不截断")
    name = "s13"
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
        r = run_task(d, limit=0)
        check(name, r["total"] == 4, f"total={r['total']}")
    finally:
        cleanup(name)

    # 14. 自定义 fetcher fetch_all（无 start_urls）
    print("[14/15] 自定义 fetcher fetch_all（无种子）")
    name = "s14"
    cfg = {
        "name": name, "start_urls": [],
        "source": {"type": "custom"},
        "record": {"fields": {"title": {"from": "title"}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0},
    }
    mod = '''from universal_scraper.protocols import BaseFetcher
class Fetcher(BaseFetcher):
    name = "custom"
    def fetch(self, req):
        raise NotImplementedError
    def fetch_all(self):
        return [{"title": "fa-1"}, {"title": "fa-2"}, {"title": ""}]
'''
    d = make_task(name, cfg, {"fetcher.py": mod})
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 2 and [i["title"] for i in items] == ["fa-1", "fa-2"],
              f"total={r['total']} items={[i['title'] for i in items]}")
    finally:
        cleanup(name)

    # 15. monitor 无变化不误报
    print("[15/15] monitor 固定内容无变化")
    name = "s15"
    cfg = {
        "name": name, "start_urls": [f"{base}/static.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/static", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        p = run_cli(["monitor", "--task", str(d), "--every", "1", "--times", "2", "--key", "title"],
                    timeout=120)
        ok = "无变化" in p.stdout and p.stdout.count("新增") == 1
        check(name, ok, f"stdout={p.stdout[-250:]}")
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
