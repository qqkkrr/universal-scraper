#!/usr/bin/env python3
"""第五轮高难度回归（15 个）：v2 断点/迭代/sitemap/POST/--url、代理池冷却回退、
fetch --table、crawl --browser、分页 off-by-one、递归并发、dedup、dry-run 零输出、
fetch 404 退出码、SIGINT 优雅中断恢复、双任务并行。
用法: python3 tests/run_tests5.py
"""
from __future__ import annotations

import json
import os
import signal
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


def make_task(name: str, cfg: dict) -> Path:
    d = TMP / name
    (d / "modules").mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
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


def read_json(name: str) -> list:
    fp = ROOT / "outputs" / f"{name}.json"
    return json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else []


def read_items(name: str) -> list:
    fp = ROOT / "outputs" / "items" / f"{name}.jsonl"
    if not fp.exists():
        return []
    return [json.loads(x) for x in fp.read_text(encoding="utf-8").splitlines() if x.strip()]


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

    # 1. v2 断点续跑（detail 检查点）
    print("[1/15] v2 断点续跑（detail 检查点合并）")
    name = "x1"
    cfg = {
        "name": name,
        "source": {"type": "http_html", "url": f"{base}/list.html",
                   "row_css": ".item",
                   "fields": {"title": {"css": "span.t"}, "url": {"css": "a.u", "attr": "href"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}, "url": {"from": "url"}}},
        "pipeline": [],
        "detail": {"enabled": True, "url_field": "url", "concurrency": 2,
                   "extract": [{"name": "body", "type": "css_text", "selector": "h1"}]},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp), "--limit", "1"])
        run_cli(["run", "--config", str(fp), "--resume"])
        rows = read_json(name)
        check(name, len(rows) == 2 and all(r.get("body") == "详情内容-1" for r in rows),
              f"rows={len(rows)} bodies={[r.get('body') for r in rows]}")
    finally:
        cleanup(name)

    # 2. v2 iterate 多迭代 + 合并导出
    print("[2/15] v2 iterate 多迭代 + 合并导出")
    name = "x2"
    cfg = {
        "name": name, "vars": {"q": "默认"},
        "iterate": {"var": "q", "values": ["a", "b"]},
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
        rows = read_json(name + "_合并")
        check(name, len(rows) == 2 and {r["title"] for r in rows} == {"hit-a", "hit-b"},
              f"rows={len(rows)} titles={[r['title'] for r in rows]}")
    finally:
        cleanup(name)

    # 3. v2 sitemap 种子
    print("[3/15] v2 sitemap 种子")
    name = "x3"
    cfg = {
        "name": name,
        "source": {"type": "http_html", "url": f"{base}/p1.html", "sitemap": f"{base}/sitemap.xml",
                   "row_css": ".item", "fields": {"title": {"css": "span.t"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}}},
        "pipeline": [], "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp)])
        rows = read_json(name)
        check(name, len(rows) == 6, f"rows={len(rows)}")
    finally:
        cleanup(name)

    # 4. v2 POST JSON body
    print("[4/15] v2 http_json POST json_body")
    name = "x4"
    cfg = {
        "name": name,
        "source": {"type": "http_json", "method": "POST", "url": f"{base}/api/post",
                   "json_body": {"x": 1}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}}},
        "pipeline": [], "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp)])
        rows = read_json(name)
        check(name, len(rows) == 1 and rows[0]["title"] == "post-ok", f"rows={rows}")
    finally:
        cleanup(name)

    # 5. v2 --url 覆盖
    print("[5/15] v2 run --config --url 覆盖入口")
    name = "x5"
    cfg = {
        "name": name,
        "source": {"type": "http_html", "url": f"{base}/p1.html",
                   "row_css": ".item", "fields": {"title": {"css": "span.t"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}}},
        "pipeline": [], "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp), "--url", f"{base}/p2.html"])
        rows = read_json(name)
        check(name, len(rows) == 2 and all("p2-title" in r["title"] for r in rows),
              f"rows={[r['title'] for r in rows]}")
    finally:
        cleanup(name)

    # 6. 代理池冷却回退（死代理 → 直连成功）
    print("[6/15] 代理池冷却回退")
    from universal_scraper.modules.fetchers import HttpFetcher
    from universal_scraper.protocols import Request
    f = HttpFetcher({"type": "http"}, {}, {"min_interval": 0.0, "max_retries": 1,
                                           "http_backend": "auto",
                                           "proxies": ["http://127.0.0.1:1"],
                                           "proxy_mode": "round_robin"})
    calls = []
    class FakeClient:
        def request(self, url, *a, **kw):
            calls.append(kw.get("proxy"))
            if kw.get("proxy"):
                raise RuntimeError("proxy dead")
            return {"ok": True, "status": 200, "body": b"<html><body>ok</body></html>",
                    "text": "<html><body>ok</body></html>", "json": None, "url": url, "headers": {}}
    f.client = FakeClient()
    first_ok = True
    try:
        f.fetch(Request(url=f"{base}/p1.html"))
    except Exception:
        first_ok = False
    r2 = f.fetch(Request(url=f"{base}/p1.html"))
    check("x6_proxy_fallback", (not first_ok) and r2.status == 200
          and calls[0] == "http://127.0.0.1:1" and calls[1] is None,
          f"first_ok={first_ok} calls={calls}")

    # 7. fetch --table --json
    print("[7/15] fetch --table --json")
    p = run_cli(["fetch", f"{base}/table.html", "--table", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("tables") and data["tables"][0][0].get("Name") == "Alice"
    except Exception:
        ok = False
    check("x7_fetch_table", ok, f"stdout={p.stdout[:100]}")

    # 8. crawl --browser（SPA JS 渲染递归）
    print("[8/15] crawl --browser（SPA 渲染 + 链接递归）")
    p = run_cli(["crawl", f"{base}/spa.html", "--browser", "--max", "10", "--allow", r"/p\d\.html"], timeout=300)
    rows = read_json("crawl_127_0_0_1_" + str(srv.server_address[1]).replace(".", "_"))
    # 用 glob 更稳
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
    check("x8_crawl_browser", len(urls) >= 3 and any("spa" in u for u in urls),
          f"urls={urls}")

    # 9. json_paged 总数精确 off-by-one
    print("[9/15] json_paged 总数精确（total=3, size=2）")
    name = "x9"
    cfg = {
        "name": name, "start_urls": [f"{base}/api/odd?page=1"],
        "queue": {"max_depth": 5, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/api/odd", "parser": "js"}],
        "parsers": {"js": {"type": "json_paged", "records_path": "records", "total_path": "total",
                           "strategy": "page_param", "page_param": "page", "start": 1,
                           "max_pages": 10, "page_size": 2,
                           "fields": {"id": {"from": "id"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 3 and r["fetched"] == 2 and {i["id"] for i in items} == {1, 2, 3},
              f"total={r['total']} fetched={r['fetched']} ids={sorted(i['id'] for i in items)}")
    finally:
        cleanup(name)

    # 10. 递归并发（4 worker，depth1，无重复）
    print("[10/15] 递归并发 4 worker 无重复")
    name = "x10"
    cfg = {
        "name": name, "start_urls": [f"{base}/site/level1.html"],
        "queue": {"max_depth": 1, "max_requests": 20, "max_concurrency": 4},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/site", "parser": "page"}],
        "parsers": {"page": {"type": "html", "row_css": ".item",
                             "fields": {"t": {"css": "span.t"}},
                             "extract_links": {"allow": "/site/"}}},
        "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.01, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        urls = [i["_url"] for i in items]
        check(name, r["fetched"] == 3 and len(set(urls)) == len(urls),
              f"fetched={r['fetched']} urls={urls}")
    finally:
        cleanup(name)

    # 11. pipeline dedup
    print("[11/15] pipeline dedup 去重")
    name = "x11"
    cfg = {
        "name": name, "start_urls": [f"{base}/dup.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/dup", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "dedup", "key": "title"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        check(name, r["total"] == 1, f"total={r['total']}")
    finally:
        cleanup(name)

    # 12. dry-run 零输出
    print("[12/15] --dry-run 不产生任何输出")
    name = "x12"
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
    d = make_task(name, cfg)
    try:
        r = run_task(d, dry_run=True)
        no_items = not (ROOT / "outputs/items" / f"{name}.jsonl").exists()
        no_export = not (ROOT / "outputs" / f"{name}.json").exists()
        check(name, r.get("dry_run") and no_items and no_export, f"dry={r.get('dry_run')}")
    finally:
        cleanup(name)

    # 13. fetch 404 → 退出码 1
    print("[13/15] fetch 404 → 报错退出码 1")
    p = run_cli(["fetch", f"{base}/not_exist.html"])
    check("x13_fetch_404", p.returncode == 1 and "❌" in p.stderr, f"rc={p.returncode} stderr={p.stderr[:80]}")

    # 14. SIGINT 优雅中断 → resume 恢复
    print("[14/15] SIGINT 优雅中断 → resume 恢复")
    name = "x14"
    urls = [f"{base}/slow/{i}" for i in range(10)]
    cfg = {
        "name": name, "start_urls": urls,
        "queue": {"max_depth": 1, "max_requests": 50, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/slow", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        proc = subprocess.Popen([PY, "-m", "universal_scraper.cli", "run", "--task", str(d)],
                                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2.2)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=15)
        # 优雅退出：退出码 130 + pending 非空（并发1时队列必有剩余）
        rc = proc.returncode
        pending = ROOT / "outputs" / f".pending_{name}.json"
        saved = pending.exists() and json.loads(pending.read_text(encoding="utf-8")) != []
        r = run_task(d, resume=True)
        final_urls = [x["_url"] for x in read_json(name)]
        check(name, rc == 130 and saved and len({u.split("/slow/")[1] for u in final_urls}) == 10,
              f"rc={rc} saved={saved} final={len(final_urls)}")
    finally:
        cleanup(name)

    # 15. 双任务并行
    print("[15/15] 两个任务线程并行互不干扰")
    res = {}
    def work(nm, url):
        cfg = {
            "name": nm, "start_urls": [url],
            "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
            "source": {"type": "http"},
            "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
            "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
            "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
            "storage": {"type": "jsonl", "name": nm},
            "output": {"dir": "outputs", "base_name": nm},
            "anti_bot": {"min_interval": 0.01, "max_retries": 1},
        }
        d = make_task(nm, cfg)
        res[nm] = run_task(d)
    t1 = threading.Thread(target=work, args=("x15a", f"{base}/p1.html"))
    t2 = threading.Thread(target=work, args=("x15b", f"{base}/p2.html"))
    t1.start(); t2.start(); t1.join(); t2.join()
    ok = res["x15a"]["total"] == 2 and res["x15b"]["total"] == 2 and res["x15a"]["errors"] == 0 and res["x15b"]["errors"] == 0
    check("x15_parallel", ok, f"a={res['x15a']} b={res['x15b']}")
    cleanup("x15a")
    cleanup("x15b")

    print(f"\n===== 结果: {len(PASS)}/15 通过 =====")
    if FAIL:
        print("失败:", FAIL)
        srv.shutdown()
        return 1
    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
