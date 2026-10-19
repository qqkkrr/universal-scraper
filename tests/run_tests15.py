#!/usr/bin/env python3
"""第十五轮高难度回归（15 个）：全新确定性场景，目标首次运行全绿零修改。
用法: python3 tests/run_tests15.py
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

    def scfg(name, urls, concurrency=1, extra=None):
        cfg = {
            "name": name, "start_urls": urls,
            "queue": {"max_depth": 1, "max_requests": 20, "max_concurrency": concurrency},
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

    # 1. fetch p2 --json
    print("[1/15] fetch p2 --json")
    p = run_cli(["fetch", f"{base}/p2.html", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200 and "p2-title-1" in data.get("text", "")
    except Exception:
        ok = False
    check("x1_fetch_p2", ok, f"status={data.get('status') if 'data' in dir() else '?'}")

    # 2. fetch gzip --json
    print("[2/15] fetch gzip --json")
    p = run_cli(["fetch", f"{base}/gzip", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200 and "gzip-ok" in data.get("text", "")
    except Exception:
        ok = False
    check("x2_fetch_gzip", ok, f"status={data.get('status') if 'data' in dir() else '?'}")

    # 3. fetch gbk --json
    print("[3/15] fetch gbk --json")
    p = run_cli(["fetch", f"{base}/gbk.html", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = "中文标题" in data.get("text", "")
    except Exception:
        ok = False
    check("x3_fetch_gbk", ok, f"text={data.get('text','')[:30] if 'data' in dir() else '?'}")

    # 4. v3 双字段解析
    print("[4/15] v3 双字段解析")
    name = "x4"
    cfg = scfg(name, [f"{base}/p1.html"])
    cfg["parsers"]["js"]["fields"] = {"title": {"css": "span.t"}, "date": {"css": "span.p"}}
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        ok = r["total"] == 2 and all(i.get("date") for i in items)
        check(name, ok, f"total={r['total']} dates={[i.get('date') for i in items]}")
    finally:
        cleanup(name)

    # 5. v3 三页并发去重
    print("[5/15] v3 三页并发去重")
    name = "x5"
    d = make_task(name, scfg(name, [f"{base}/p{i}.html" for i in range(1, 4)], concurrency=4))
    try:
        r = run_task(d)
        urls = {i["_url"] for i in read_items(name)}
        check(name, r["total"] == 6 and r["fetched"] == 3 and len(urls) == 3,
              f"total={r['total']} fetched={r['fetched']}")
    finally:
        cleanup(name)

    # 6. json_paged next_url
    print("[6/15] json_paged next_url")
    name = "x6"
    cfg = scfg(name, [f"{base}/api/next?page=1"])
    cfg["parsers"]["js"] = {"type": "json_paged", "records_path": "records", "strategy": "next_url",
                            "next_path": "next", "start": 1, "max_pages": 10,
                            "fields": {"id": {"from": "id"}}}
    cfg["pipelines"] = []
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 4 and {i["id"] for i in items} == {1, 2, 3, 4},
              f"total={r['total']} fetched={r['fetched']}")
    finally:
        cleanup(name)

    # 7. 429 恢复
    print("[7/15] 429 恢复 errors=0")
    name = "x7"
    d = make_task(name, scfg(name, [f"{base}/ratelimit"]))
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and r["errors"] == 0 and items[0]["title"] == "after-429",
              f"total={r['total']} errors={r['errors']}")
    finally:
        cleanup(name)

    # 8. robots 跳过 p2
    print("[8/15] robots 跳过 p2")
    name = "x8"
    cfg = scfg(name, [f"{base}/p1.html", f"{base}/p2.html", f"{base}/p3.html"])
    cfg["anti_bot"]["respect_robots"] = True
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        ok = r["total"] == 4 and not any("p2" in i["_url"] for i in items)
        check(name, ok, f"total={r['total']} fetched={r['fetched']}")
    finally:
        cleanup(name)

    # 9. multi 三后端单页
    print("[9/15] multi 三后端单页")
    name = "x9"
    cfg = scfg(name, [f"{base}/p1.html"],
               extra={"storage": {"type": "multi", "backends": [
                   {"type": "jsonl", "name": name}, {"type": "csv", "name": name},
                   {"type": "sqlite", "name": name}]}})
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        import sqlite3
        conn = sqlite3.connect(str(ROOT / "outputs/items" / f"{name}.db"))
        n = conn.execute("select count(*) from items").fetchone()[0]
        conn.close()
        csv = ROOT / "outputs/items" / f"{name}.csv"
        nj = len((ROOT / "outputs/items" / f"{name}.jsonl").read_text(encoding="utf-8").splitlines())
        nc = len(csv.read_text(encoding="utf-8-sig").splitlines()) if csv.exists() else 0
        check(name, r["total"] == 2 and n == 2 and nj == 2 and nc == 3,
              f"db={n} jsonl={nj} csv={nc}")
    finally:
        cleanup(name)

    # 10. resume 幂等
    print("[10/15] resume 幂等")
    name = "x10"
    d = make_task(name, scfg(name, [f"{base}/p1.html", f"{base}/p2.html", f"{base}/p3.html"]))
    try:
        run_task(d, limit=2)
        r2 = run_task(d, resume=True)
        r3 = run_task(d, resume=True)
        rows = read_json(name)
        check(name, r2["total"] == 4 and r3["total"] == 0 and len(rows) == 6,
              f"r2={r2['total']} r3={r3['total']} rows={len(rows)}")
    finally:
        cleanup(name)

    # 11. crawl --max 1
    print("[11/15] crawl --max 1")
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
    check("x11_crawl1", len(urls) == 1 and "hub.html" in urls[0], f"urls={urls}")

    # 12. fetch --browser --links spa
    print("[12/15] fetch --browser --links spa")
    p = run_cli(["fetch", f"{base}/spa.html", "--browser", "--links"], timeout=300)
    ok = "3 个外链" in p.stdout
    check("x12_spa_links", ok, f"stdout={p.stdout[-100:]}")

    # 13. jobs 3 好任务
    print("[13/15] jobs 3 好任务")
    names = ["x13a", "x13b", "x13c"]
    for nm in names:
        make_task(nm, scfg(nm, [f"{base}/p1.html"]))
    jobs_fp = ROOT / "outputs" / ".test_jobs15.json"
    jobs_fp.write_text(json.dumps([{"task": str(TMP / n)} for n in names]), encoding="utf-8")
    try:
        p = run_cli(["jobs", "--file", str(jobs_fp)])
        last = next((l for l in reversed(p.stdout.splitlines()) if l.strip()), "")
        data = json.loads(last)
        check("x13_jobs3", data.get("jobs") == 3 and data.get("total_items") == 6,
              f"data={data}")
    finally:
        jobs_fp.unlink(missing_ok=True)
        for nm in names:
            cleanup(nm)

    # 14. monitor 消失
    print("[14/15] monitor 消失")
    name = "x14"
    d = make_task(name, scfg(name, [f"{base}/shrink.html"]))
    try:
        p = run_cli(["monitor", "--task", str(d), "--every", "1", "--times", "2", "--key", "title"],
                    timeout=120)
        ok = "消失 1" in p.stdout
        check(name, ok, f"stdout={p.stdout[-200:]}")
    finally:
        cleanup(name)

    # 15. run --var 模板
    print("[15/15] run --var 模板")
    name = "x15"
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
        r = run_task(d, overrides={"q": "北京"})
        items = read_items(name)
        check(name, r["total"] == 1 and items[0]["title"] == "hit-北京",
              f"total={r['total']} items={items}")
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
