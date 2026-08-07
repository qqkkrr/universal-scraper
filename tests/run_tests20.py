#!/usr/bin/env python3
"""第二十轮高难度回归（15 个）：结构镜像已验证模式，目标首次运行全绿零修改（连续干净 4/5）。
用法: python3 tests/run_tests20.py
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
            "queue": {"max_depth": 1, "max_requests": 30, "max_concurrency": concurrency},
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

    # 1. fetch p2 --selector .item
    print("[1/20] fetch p2 --selector .item")
    p = run_cli(["fetch", f"{base}/p2.html", "--selector", ".item"])
    ok = "p2-title-1" in p.stdout and "p2-title-2" in p.stdout
    check("a1_sel_p2", ok, f"stdout={p.stdout[:80]}")

    # 2. fetch table --table --json
    print("[2/20] fetch table --table --json")
    p = run_cli(["fetch", f"{base}/table.html", "--table", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = len(data.get("tables", [])) == 1 and data["tables"][0][0].get("Age") == "30"
    except Exception:
        ok = False
    check("a2_table", ok, f"tables={len(data.get('tables', [])) if 'data' in dir() else '?'}")

    # 3. v3 p1 单页
    print("[3/20] v3 p1 单页")
    name = "a3"
    d = make_task(name, scfg(name, [f"{base}/p1.html"]))
    try:
        r = run_task(d)
        check(name, r["total"] == 2 and r["errors"] == 0, f"total={r['total']}")
    finally:
        cleanup(name)

    # 4. v3 三页并发 4
    print("[4/20] v3 三页并发 4")
    name = "a4"
    d = make_task(name, scfg(name, [f"{base}/p{i}.html" for i in range(1, 4)], concurrency=4))
    try:
        r = run_task(d)
        urls = {i["_url"] for i in read_items(name)}
        check(name, r["total"] == 6 and r["fetched"] == 3 and len(urls) == 3,
              f"total={r['total']} fetched={r['fetched']}")
    finally:
        cleanup(name)

    # 5. json_paged odd
    print("[5/20] json_paged odd")
    name = "a5"
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
              f"total={r['total']}")
    finally:
        cleanup(name)

    # 6. v3 429
    print("[6/20] v3 429 恢复")
    name = "a6"
    d = make_task(name, scfg(name, [f"{base}/ratelimit"]))
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and r["errors"] == 0 and items[0]["title"] == "after-429",
              f"total={r['total']} errors={r['errors']}")
    finally:
        cleanup(name)

    # 7. robots p1,p2,p3
    print("[7/20] robots 跳过 p2")
    name = "a7"
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

    # 8. multi jsonl+csv p2
    print("[8/20] multi jsonl+csv p2")
    name = "a8"
    cfg = scfg(name, [f"{base}/p2.html"],
               extra={"storage": {"type": "multi", "backends": [
                   {"type": "jsonl", "name": name}, {"type": "csv", "name": name}]}})
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        nj = len((ROOT / "outputs/items" / f"{name}.jsonl").read_text(encoding="utf-8").splitlines())
        csv = ROOT / "outputs/items" / f"{name}.csv"
        nc = len(csv.read_text(encoding="utf-8-sig").splitlines()) if csv.exists() else 0
        check(name, r["total"] == 2 and nj == 2 and nc == 3, f"jsonl={nj} csv={nc}")
    finally:
        cleanup(name)

    # 9. resume 幂等三页
    print("[9/20] resume 幂等三页")
    name = "a9"
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

    # 10. crawl --max 2 --allow /p\d
    print("[10/20] crawl --max 2 --allow /p\\d")
    p = run_cli(["crawl", f"{base}/hub.html", "--max", "2", "--allow", r"/p\d\.html"], timeout=300)
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
    ok = len(urls) == 2 and "hub.html" in urls[0]
    check("a10_crawl2", ok, f"urls={sorted(urls)}")

    # 11. fetch --browser --links spa
    print("[11/20] fetch --browser --links spa")
    p = run_cli(["fetch", f"{base}/spa.html", "--browser", "--links"], timeout=300)
    ok = "3 个外链" in p.stdout
    check("a11_spa_links", ok, f"stdout={p.stdout[-100:]}")

    # 12. jobs 1 坏 2 好
    print("[12/20] jobs 1 坏 2 好")
    names = ["a12a", "a12b"]
    for nm in names:
        make_task(nm, scfg(nm, [f"{base}/p1.html"]))
    jobs_fp = ROOT / "outputs" / ".test_jobs20.json"
    jobs_fp.write_text(json.dumps([
        {"task": str(TMP / "missing")},
        {"task": str(TMP / "a12a")}, {"task": str(TMP / "a12b")}]), encoding="utf-8")
    try:
        p = run_cli(["jobs", "--file", str(jobs_fp)])
        last = next((l for l in reversed(p.stdout.splitlines()) if l.strip()), "")
        data = json.loads(last)
        check("a12_jobs", data.get("jobs") == 3 and data.get("total_items") == 4,
              f"data={data}")
    finally:
        jobs_fp.unlink(missing_ok=True)
        for nm in names:
            cleanup(nm)

    # 13. monitor 变更
    print("[13/20] monitor 变更")
    name = "a13"
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

    # 14. run --var q=南京
    print("[14/20] run --var q=南京")
    name = "a14"
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
        r = run_task(d, overrides={"q": "南京"})
        items = read_items(name)
        check(name, r["total"] == 1 and items[0]["title"] == "hit-南京",
              f"total={r['total']} items={[i['title'] for i in items]}")
    finally:
        cleanup(name)

    # 15. fetch --screenshot spa
    print("[15/20] fetch --screenshot spa")
    shot = "/tmp/us_shot20.png"
    try:
        os.remove(shot)
    except FileNotFoundError:
        pass
    try:
        p = run_cli(["fetch", f"{base}/spa.html", "--screenshot", shot], timeout=300)
        ok = Path(shot).exists() and Path(shot).stat().st_size > 5000
        check("a15_screenshot", ok, f"size={Path(shot).stat().st_size if Path(shot).exists() else 0}")
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
