#!/usr/bin/env python3
"""第十六轮高难度回归（15 个）：全新确定性场景，目标首次运行全绿零修改。
用法: python3 tests/run_tests16.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from mock_sites import Handler  # noqa: E402
from universal_scraper.engine_v3 import run_task  # noqa: E402

PY = sys.executable
TMP = ROOT / "outputs" / ".test_tmp"
PASS, FAIL = [], []


def make_task(name: str, cfg: dict) -> Path:
    d = TMP / name
    (d / "modules").mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def cleanup(name: str):
    import glob, shutil
    for pat in (f"outputs/{name}*", f"outputs/items/{name}*",
                f"outputs/.state_{name}.json", f"outputs/.pending_{name}.json",
                f"outputs/.run_{name}.log", f"outputs/.seen_{name}.txt"):
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

    def scfg(name, urls, extra=None):
        cfg = {
            "name": name, "start_urls": urls,
            "queue": {"max_depth": 1, "max_requests": 30, "max_concurrency": 2},
            "source": {"type": "http"},
            "rules": [{"match": "contains", "pattern": "/", "parser": "js"}],
            "parsers": {"js": {"type": "html", "row_css": ".item",
                               "fields": {"title": {"css": "span.t"}}}},
            "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
            "storage": {"type": "jsonl", "name": name},
            "output": {"dir": "outputs", "base_name": name},
            "anti_bot": {"min_interval": 0.0, "max_retries": 1},
        }
        if extra:
            cfg.update(extra)
        return cfg

    # 1. fetch redirect 最终 URL
    print("[1/16] fetch redirect 最终 URL")
    p = run_cli(["fetch", f"{base}/redirect", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200 and data.get("url", "").endswith("/p1.html")
    except Exception:
        ok = False
    check("y1_redirect", ok, f"url={data.get('url') if 'data' in dir() else '?'}")

    # 2. fetch 多表
    print("[2/16] fetch 多表")
    p = run_cli(["fetch", f"{base}/multitable.html", "--table", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = len(data.get("tables", [])) == 2
    except Exception:
        ok = False
    check("y2_multitable", ok, f"tables={len(data.get('tables', [])) if 'data' in dir() else '?'}")

    # 3. v3 两页 jsonl 4 行
    print("[3/16] v3 两页 jsonl")
    name = "y3"
    d = make_task(name, scfg(name, [f"{base}/p1.html", f"{base}/p2.html"]))
    try:
        r = run_task(d)
        n = len(read_items(name))
        check(name, r["total"] == 4 and n == 4, f"total={r['total']} rows={n}")
    finally:
        cleanup(name)

    # 4. json_paged 奇数总数
    print("[4/16] json_paged 奇数总数")
    name = "y4"
    cfg = scfg(name, [f"{base}/api/odd?page=1"])
    cfg["parsers"]["js"] = {"type": "json_paged", "records_path": "records", "total_path": "total",
                            "strategy": "page_param", "page_param": "page", "start": 1,
                            "max_pages": 10, "page_size": 2, "fields": {"id": {"from": "id"}}}
    cfg["pipelines"] = []
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 3 and {i["id"] for i in items} == {1, 2, 3},
              f"total={r['total']} fetched={r['fetched']}")
    finally:
        cleanup(name)

    # 5. cycle 防护
    print("[5/16] cycle 防护")
    name = "y5"
    cfg = scfg(name, [f"{base}/cycle1.html"],
               extra={"queue": {"max_depth": 5, "max_requests": 30, "max_concurrency": 2},
                      "parsers": {"page": {"type": "html", "row_css": ".item",
                                           "fields": {"t": {"css": "span.t"}},
                                           "extract_links": {"allow": "/cycle"}}},
                      "rules": [{"match": "contains", "pattern": "/cycle", "parser": "page"}],
                      "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}]})
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        ts = sorted(i["t"] for i in read_items(name))
        check(name, r["fetched"] == 2 and ts == ["c1", "c2"], f"fetched={r['fetched']} ts={ts}")
    finally:
        cleanup(name)

    # 6. depth=1 不越级
    print("[6/16] depth=1 不越级")
    name = "y6"
    cfg = scfg(name, [f"{base}/site/level1.html"],
               extra={"queue": {"max_depth": 1, "max_requests": 20, "max_concurrency": 2},
                      "parsers": {"page": {"type": "html", "row_css": ".item",
                                           "fields": {"t": {"css": "span.t"}},
                                           "extract_links": {"allow": "/site/"}}},
                      "rules": [{"match": "contains", "pattern": "/site", "parser": "page"}],
                      "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}]})
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        ts = sorted(i["t"] for i in read_items(name))
        check(name, r["fetched"] == 3 and ts == ["L2", "L2"], f"fetched={r['fetched']} ts={ts}")
    finally:
        cleanup(name)

    # 7. source.query 静态参数
    print("[7/16] source.query 静态参数")
    name = "y7"
    cfg = scfg(name, [f"{base}/p1.html"])
    cfg["source"]["query"] = {"static": "yes"}
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        ok = r["total"] == 2 and all("static=yes" in i["_url"] for i in items)
        check(name, ok, f"urls={[i['_url'] for i in items][:1]}")
    finally:
        cleanup(name)

    # 8. v3 cookies
    print("[8/16] v3 cookies")
    name = "y8"
    cfg = scfg(name, [f"{base}/cookies"])
    cfg["anti_bot"]["cookies"] = {"us_cookie": "1"}
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and items[0]["title"] == "cookie-ok",
              f"total={r['total']}")
    finally:
        cleanup(name)

    # 9. csv 单后端
    print("[9/16] csv 单后端")
    name = "y9"
    cfg = scfg(name, [f"{base}/p1.html"],
               extra={"storage": {"type": "csv", "name": name}})
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        csv = ROOT / "outputs/items" / f"{name}.csv"
        n = len(csv.read_text(encoding="utf-8-sig").splitlines()) if csv.exists() else 0
        check(name, r["total"] == 2 and n == 3, f"total={r['total']} csv={n}")
    finally:
        cleanup(name)

    # 10. 大 JSON
    print("[10/16] 大 JSON 2000")
    name = "y10"
    cfg = scfg(name, [f"{base}/api/big"])
    cfg["parsers"]["js"] = {"type": "json", "records_path": "records",
                            "fields": {"id": {"from": "id"}}}
    cfg["pipelines"] = []
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        check(name, r["total"] == 2000, f"total={r['total']}")
    finally:
        cleanup(name)

    # 11. crawl --deny out
    print("[11/16] crawl --deny out")
    p = run_cli(["crawl", f"{base}/hub.html", "--deny", r"/out\.html", "--max", "10"], timeout=300)
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
    ok = len(urls) == 6 and not any("out.html" in u for u in urls)
    check("y11_crawl_deny", ok, f"urls={sorted(urls)}")

    # 12. fetch --browser --selector article h1
    print("[12/16] fetch --browser --selector h1")
    p = run_cli(["fetch", f"{base}/article.html", "--browser", "--selector", "h1"], timeout=300)
    ok = "标题一" in p.stdout
    check("y12_browser_h1", ok, f"stdout={p.stdout[:80]}")

    # 13. jobs 1 坏 2 好
    print("[13/16] jobs 1 坏 2 好")
    names = ["y13a", "y13b"]
    for nm in names:
        make_task(nm, scfg(nm, [f"{base}/p1.html"]))
    jobs_fp = ROOT / "outputs" / ".test_jobs16.json"
    jobs_fp.write_text(json.dumps([
        {"task": str(TMP / "missing")},
        {"task": str(TMP / "y13a")}, {"task": str(TMP / "y13b")}]), encoding="utf-8")
    try:
        p = run_cli(["jobs", "--file", str(jobs_fp)])
        last = next((l for l in reversed(p.stdout.splitlines()) if l.strip()), "")
        data = json.loads(last)
        check("y13_jobs", data.get("jobs") == 3 and data.get("total_items") == 4,
              f"data={data}")
    finally:
        jobs_fp.unlink(missing_ok=True)
        for nm in names:
            cleanup(nm)

    # 14. monitor 变更
    print("[14/16] monitor 变更")
    name = "y14"
    cfg = scfg(name, [f"{base}/change.html"])
    cfg["parsers"]["js"]["fields"] = {"title": {"css": "span.t"}, "val": {"css": "span.p"}}
    d = make_task(name, cfg)
    try:
        p = run_cli(["monitor", "--task", str(d), "--every", "1", "--times", "2",
                     "--key", "title", "--diff-fields", "val"], timeout=120)
        ok = "变更 1" in p.stdout
        check(name, ok, f"stdout={p.stdout[-200:]}")
    finally:
        cleanup(name)

    # 15. run --task --url + --var 组合（--url 优先）
    print("[15/16] run --url + --var 组合")
    name = "y15"
    cfg = {
        "name": name, "vars": {"q": "默认"},
        "start_urls": [f"{base}/search?q={{{{q}}}}"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/search", "parser": "js"}],
        "parsers": {"js": {"type": "json", "records_path": "records",
                           "fields": {"title": {"from": "title"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d, start_url=f"{base}/search?q=上海", overrides={"q": "北京"})
        items = read_items(name)
        check(name, r["total"] == 1 and items[0]["title"] == "hit-上海",
              f"total={r['total']} items={[i['title'] for i in items]}")
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
