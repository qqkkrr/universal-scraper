#!/usr/bin/env python3
"""第十四轮高难度回归（15 个）：确定性场景（基于已加固能力，断言极简）。
用法: python3 tests/run_tests14.py
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

    def simple_cfg(name, urls, parser_extra=None, storage=None, extra=None):
        cfg = {
            "name": name, "start_urls": urls,
            "queue": {"max_depth": 1, "max_requests": 20, "max_concurrency": 2},
            "source": {"type": "http"},
            "rules": [{"match": "contains", "pattern": "/", "parser": "js"}],
            "parsers": {"js": {"type": "html", "row_css": ".item",
                               "fields": {"title": {"css": "span.t"}}}},
            "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
            "storage": storage or {"type": "jsonl", "name": name},
            "output": {"dir": "outputs", "base_name": name},
            "anti_bot": {"min_interval": 0.0, "max_retries": 1},
        }
        if parser_extra:
            cfg["parsers"]["js"].update(parser_extra)
        if extra:
            cfg.update(extra)
        return cfg

    # 1. fetch --json p1
    print("[1/15] fetch --json p1")
    p = run_cli(["fetch", f"{base}/p1.html", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200 and "p1-title-1" in data.get("text", "")
    except Exception:
        ok = False
    check("w1_fetch_json", ok, f"status={data.get('status') if 'data' in dir() else '?'}")

    # 2. fetch --table
    print("[2/15] fetch --table")
    p = run_cli(["fetch", f"{base}/table.html", "--table", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = len(data.get("tables", [])) == 1 and data["tables"][0][0].get("Name") == "Alice"
    except Exception:
        ok = False
    check("w2_fetch_table", ok, f"tables={len(data.get('tables', [])) if 'data' in dir() else '?'}")

    # 3. v3 单页 2 条
    print("[3/15] v3 单页 2 条")
    name = "w3"
    d = make_task(name, simple_cfg(name, [f"{base}/p1.html"]))
    try:
        r = run_task(d)
        check(name, r["total"] == 2 and r["errors"] == 0, f"total={r['total']}")
    finally:
        cleanup(name)

    # 4. v3 两页 4 条
    print("[4/15] v3 两页 4 条")
    name = "w4"
    d = make_task(name, simple_cfg(name, [f"{base}/p1.html", f"{base}/p2.html"]))
    try:
        r = run_task(d)
        check(name, r["total"] == 4 and r["fetched"] == 2, f"total={r['total']} fetched={r['fetched']}")
    finally:
        cleanup(name)

    # 5. json_paged page_param
    print("[5/15] json_paged page_param")
    name = "w5"
    cfg = simple_cfg(name, [f"{base}/api/list?page=1"],
                     parser_extra={"type": "json_paged", "records_path": "records", "total_path": "total",
                                   "strategy": "page_param", "page_param": "page", "start": 1,
                                   "max_pages": 10, "page_size": 2,
                                   "fields": {"id": {"from": "id"}, "title": {"from": "title"}}})
    cfg["pipelines"] = []
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 4 and {i["id"] for i in items} == {1, 2, 3, 4},
              f"total={r['total']} ids={sorted(i['id'] for i in items)}")
    finally:
        cleanup(name)

    # 6. json_paged offset
    print("[6/15] json_paged offset")
    name = "w6"
    cfg = simple_cfg(name, [f"{base}/api/offset?offset=0"],
                     parser_extra={"type": "json_paged", "records_path": "records", "total_path": "total",
                                   "strategy": "offset", "offset_param": "offset", "start": 1,
                                   "max_pages": 10, "page_size": 2,
                                   "fields": {"id": {"from": "id"}}})
    cfg["pipelines"] = []
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 5 and {i["id"] for i in items} == {1, 2, 3, 4, 5},
              f"total={r['total']}")
    finally:
        cleanup(name)

    # 7. incremental 二次归零
    print("[7/15] incremental 二次归零")
    name = "w7"
    cfg = simple_cfg(name, [f"{base}/p1.html"])
    cfg["incremental"] = {"enabled": True, "key": "title"}
    d = make_task(name, cfg)
    try:
        r1 = run_task(d)
        r2 = run_task(d)
        check(name, r1["total"] == 2 and r2["total"] == 0, f"r1={r1['total']} r2={r2['total']}")
    finally:
        cleanup(name)

    # 8. multi jsonl+sqlite
    print("[8/15] multi jsonl+sqlite")
    name = "w8"
    cfg = simple_cfg(name, [f"{base}/p1.html"],
                     storage={"type": "multi", "backends": [{"type": "jsonl", "name": name},
                                                            {"type": "sqlite", "name": name}]})
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        import sqlite3
        conn = sqlite3.connect(str(ROOT / "outputs/items" / f"{name}.db"))
        n = conn.execute("select count(*) from items").fetchone()[0]
        conn.close()
        jsonl = ROOT / "outputs/items" / f"{name}.jsonl"
        nj = len(jsonl.read_text(encoding="utf-8").splitlines())
        check(name, r["total"] == 2 and n == 2 and nj == 2, f"db={n} jsonl={nj}")
    finally:
        cleanup(name)

    # 9. resume 合并
    print("[9/15] resume 合并")
    name = "w9"
    cfg = simple_cfg(name, [f"{base}/p1.html", f"{base}/p2.html", f"{base}/p3.html"])
    cfg["queue"] = {"max_depth": 1, "max_requests": 20, "max_concurrency": 1}
    d = make_task(name, cfg)
    try:
        r1 = run_task(d, limit=2)
        r2 = run_task(d, resume=True)
        rows = read_json(name)
        check(name, r1["total"] == 2 and r2["total"] == 4 and len(rows) == 6,
              f"r1={r1['total']} r2={r2['total']} rows={len(rows)}")
    finally:
        cleanup(name)

    # 10. crawl --max 3 --allow /p\d
    print("[10/15] crawl --max 3 --allow /p\\d")
    p = run_cli(["crawl", f"{base}/hub.html", "--max", "3", "--allow", r"/p\d\.html"], timeout=300)
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
    ok = len(urls) == 3 and "hub.html" in urls[0]
    check("w10_crawl_max3", ok, f"urls={sorted(urls)}")

    # 11. fetch --browser --selector spa
    print("[11/15] fetch --browser --selector spa")
    p = run_cli(["fetch", f"{base}/spa.html", "--browser", "--selector", ".item"], timeout=300)
    ok = "spa-item-1" in p.stdout and "spa-item-3" in p.stdout
    check("w11_browser_sel", ok, f"stdout={p.stdout[:80]}")

    # 12. run --task --url
    print("[12/15] run --task --url")
    name = "w12"
    d = make_task(name, simple_cfg(name, [f"{base}/p1.html"]))
    try:
        r = run_task(d, start_url=f"{base}/p2.html")
        items = read_items(name)
        check(name, r["total"] == 2 and all("p2-title" in i["title"] for i in items),
              f"total={r['total']} titles={[i['title'] for i in items]}")
    finally:
        cleanup(name)

    # 13. jobs 2 好任务
    print("[13/15] jobs 2 好任务")
    names = ["w13a", "w13b"]
    for nm in names:
        make_task(nm, simple_cfg(nm, [f"{base}/p1.html"]))
    jobs_fp = ROOT / "outputs" / ".test_jobs14.json"
    jobs_fp.write_text(json.dumps([{"task": str(TMP / "w13a")}, {"task": str(TMP / "w13b")}]), encoding="utf-8")
    try:
        p = run_cli(["jobs", "--file", str(jobs_fp)])
        last = next((l for l in reversed(p.stdout.splitlines()) if l.strip()), "")
        data = json.loads(last)
        check("w13_jobs2", data.get("jobs") == 2 and data.get("total_items") == 4,
              f"data={data}")
    finally:
        jobs_fp.unlink(missing_ok=True)
        for nm in names:
            cleanup(nm)

    # 14. monitor 静态无变化
    print("[14/15] monitor 静态无变化")
    name = "w14"
    d = make_task(name, simple_cfg(name, [f"{base}/static.html"]))
    try:
        p = run_cli(["monitor", "--task", str(d), "--every", "1", "--times", "2", "--key", "title"],
                    timeout=120)
        ok = "无变化" in p.stdout and p.stdout.count("新增") == 1
        check(name, ok, f"stdout={p.stdout[-150:]}")
    finally:
        cleanup(name)

    # 15. fetch --screenshot article
    print("[15/15] fetch --screenshot article")
    shot = "/tmp/us_shot14.png"
    try:
        os.remove(shot)
    except FileNotFoundError:
        pass
    try:
        p = run_cli(["fetch", f"{base}/article.html", "--screenshot", shot], timeout=300)
        ok = Path(shot).exists() and Path(shot).stat().st_size > 5000
        check("w15_screenshot", ok, f"size={Path(shot).stat().st_size if Path(shot).exists() else 0}")
    finally:
        try:
            os.remove(shot)
        except FileNotFoundError:
            pass

    print(f"\n===== 结果: {len(PASS)}/15 通过 =====")
    if FAIL:
        print("失败:", FAIL)
        srv.shutdown()
        return 1
    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
