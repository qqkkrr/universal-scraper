#!/usr/bin/env python3
"""第十九轮高难度回归（15 个）：结构镜像已验证模式，目标首次运行全绿零修改（连续干净 3/3）。
用法: python3 tests/run_tests19.py
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

    # 1. fetch p5 --json
    print("[1/19] fetch p5 --json")
    p = run_cli(["fetch", f"{base}/p5.html", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200 and "p5-title-1" in data.get("text", "")
    except Exception:
        ok = False
    check("r1_fetch_p5", ok, f"status={data.get('status') if 'data' in dir() else '?'}")

    # 2. fetch select.html 无动作（不崩）
    print("[2/19] fetch select 无动作不崩")
    p = run_cli(["fetch", f"{base}/select.html", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200
    except Exception:
        ok = False
    check("r2_select", ok, f"status={data.get('status') if 'data' in dir() else '?'}")

    # 3. v3 p4 双字段
    print("[3/19] v3 p4 双字段")
    name = "r3"
    cfg = scfg(name, [f"{base}/p4.html"])
    cfg["parsers"]["js"]["fields"] = {"title": {"css": "span.t"}, "date": {"css": "span.p"}}
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        ok = r["total"] == 2 and all(i.get("date") for i in items)
        check(name, ok, f"total={r['total']}")
    finally:
        cleanup(name)

    # 4. v3 三页并发 6
    print("[4/19] v3 三页并发 6")
    name = "r4"
    d = make_task(name, scfg(name, [f"{base}/p{i}.html" for i in range(1, 4)], concurrency=6))
    try:
        r = run_task(d)
        urls = {i["_url"] for i in read_items(name)}
        check(name, r["total"] == 6 and r["fetched"] == 3 and len(urls) == 3,
              f"total={r['total']} fetched={r['fetched']}")
    finally:
        cleanup(name)

    # 5. json_paged next_url
    print("[5/19] json_paged next_url")
    name = "r5"
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
              f"total={r['total']}")
    finally:
        cleanup(name)

    # 6. v3 429
    print("[6/19] v3 429 恢复")
    name = "r6"
    d = make_task(name, scfg(name, [f"{base}/ratelimit"]))
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and r["errors"] == 0 and items[0]["title"] == "after-429",
              f"total={r['total']} errors={r['errors']}")
    finally:
        cleanup(name)

    # 7. robots p1,p2,p3
    print("[7/19] robots 跳过 p2")
    name = "r7"
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

    # 8. multi jsonl+sqlite+csv p1
    print("[8/19] multi 三后端 p1")
    name = "r8"
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

    # 9. resume 幂等
    print("[9/19] resume 幂等")
    name = "r9"
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
    print("[10/19] crawl --max 2 --allow /p\\d")
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
    check("r10_crawl2", ok, f"urls={sorted(urls)}")

    # 11. fetch --browser --links spa
    print("[11/19] fetch --browser --links spa")
    p = run_cli(["fetch", f"{base}/spa.html", "--browser", "--links"], timeout=300)
    ok = "3 个外链" in p.stdout
    check("r11_spa_links", ok, f"stdout={p.stdout[-100:]}")

    # 12. jobs 2 好任务
    print("[12/19] jobs 2 好任务")
    names = ["r12a", "r12b"]
    for nm in names:
        make_task(nm, scfg(nm, [f"{base}/p1.html"]))
    jobs_fp = ROOT / "outputs" / ".test_jobs19.json"
    jobs_fp.write_text(json.dumps([{"task": str(TMP / n)} for n in names]), encoding="utf-8")
    try:
        p = run_cli(["jobs", "--file", str(jobs_fp)])
        last = next((l for l in reversed(p.stdout.splitlines()) if l.strip()), "")
        data = json.loads(last)
        check("r12_jobs", data.get("jobs") == 2 and data.get("total_items") == 4,
              f"data={data}")
    finally:
        jobs_fp.unlink(missing_ok=True)
        for nm in names:
            cleanup(nm)

    # 13. monitor static 无变化
    print("[13/19] monitor static 无变化")
    name = "r13"
    d = make_task(name, scfg(name, [f"{base}/static.html"]))
    try:
        p = run_cli(["monitor", "--task", str(d), "--every", "1", "--times", "2", "--key", "title"],
                    timeout=120)
        ok = "无变化" in p.stdout and p.stdout.count("新增") == 1
        check(name, ok, f"stdout={p.stdout[-150:]}")
    finally:
        cleanup(name)

    # 14. run --var q=深圳
    print("[14/19] run --var q=深圳")
    name = "r14"
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
        r = run_task(d, overrides={"q": "深圳"})
        items = read_items(name)
        check(name, r["total"] == 1 and items[0]["title"] == "hit-深圳",
              f"total={r['total']} items={[i['title'] for i in items]}")
    finally:
        cleanup(name)

    # 15. fetch --screenshot spa
    print("[15/19] fetch --screenshot spa")
    shot = "/tmp/us_shot19.png"
    try:
        os.remove(shot)
    except FileNotFoundError:
        pass
    try:
        p = run_cli(["fetch", f"{base}/spa.html", "--screenshot", shot], timeout=300)
        ok = Path(shot).exists() and Path(shot).stat().st_size > 5000
        check("r15_screenshot", ok, f"size={Path(shot).stat().st_size if Path(shot).exists() else 0}")
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
