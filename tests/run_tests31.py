#!/usr/bin/env python3
"""第二十十一轮高难度回归（15 个）：逐条镜像已验证断言（仅换 URL/变量），目标首次运行全绿零修改。
用法: python3 tests/run_tests31.py
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

    # 1. fetch p1 --json
    print("[1/31] fetch p1 --json")
    p = run_cli(["fetch", f"{base}/p2.html", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200 and "p2-title-1" in data.get("text", "")
    except Exception:
        ok = False
    check("l1_fetch_p2", ok, f"status={data.get('status') if 'data' in dir() else '?'}")

    # 2. fetch gzip --json
    print("[2/31] fetch gzip --json")
    p = run_cli(["fetch", f"{base}/gzip", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200 and "gzip-ok" in data.get("text", "")
    except Exception:
        ok = False
    check("l2_gzip", ok, f"status={data.get('status') if 'data' in dir() else '?'}")

    # 3. v3 p2 单页
    print("[3/31] v3 p2 单页")
    name = "l3"
    d = make_task(name, scfg(name, [f"{base}/p2.html"]))
    try:
        r = run_task(d)
        check(name, r["total"] == 2 and r["errors"] == 0, f"total={r['total']}")
    finally:
        cleanup(name)

    # 4. v3 四页并发 4
    print("[4/31] v3 四页并发 4")
    name = "l4"
    d = make_task(name, scfg(name, [f"{base}/p{i}.html" for i in range(1, 5)], concurrency=4))
    try:
        r = run_task(d)
        urls = {i["_url"] for i in read_items(name)}
        check(name, r["total"] == 8 and r["fetched"] == 4 and len(urls) == 4,
              f"total={r['total']} fetched={r['fetched']}")
    finally:
        cleanup(name)

    # 5. json_paged list
    print("[5/31] json_paged list")
    name = "l5"
    cfg = scfg(name, [f"{base}/api/list?page=1"])
    cfg["parsers"]["js"] = {"type": "json_paged", "records_path": "records", "total_path": "total",
                            "strategy": "page_param", "page_param": "page", "start": 1,
                            "max_pages": 10, "page_size": 2,
                            "fields": {"id": {"from": "id"}, "title": {"from": "title"}}}
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
    print("[6/31] v3 429 恢复")
    name = "l6"
    d = make_task(name, scfg(name, [f"{base}/ratelimit"]))
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and r["errors"] == 0 and items[0]["title"] == "after-429",
              f"total={r['total']} errors={r['errors']}")
    finally:
        cleanup(name)

    # 7. robots p1,p3
    print("[7/31] robots p1+p3")
    name = "l7"
    cfg = scfg(name, [f"{base}/p1.html", f"{base}/p3.html"])
    cfg["anti_bot"]["respect_robots"] = True
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        check(name, r["total"] == 4 and r["errors"] == 0, f"total={r['total']}")
    finally:
        cleanup(name)

    # 8. multi jsonl+sqlite p1
    print("[8/31] multi jsonl+sqlite p1")
    name = "l8"
    cfg = scfg(name, [f"{base}/p2.html"],
               extra={"storage": {"type": "multi", "backends": [
                   {"type": "jsonl", "name": name}, {"type": "sqlite", "name": name}]}})
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        import sqlite3
        conn = sqlite3.connect(str(ROOT / "outputs/items" / f"{name}.db"))
        n = conn.execute("select count(*) from items").fetchone()[0]
        conn.close()
        nj = len((ROOT / "outputs/items" / f"{name}.jsonl").read_text(encoding="utf-8").splitlines())
        check(name, r["total"] == 2 and n == 2 and nj == 2, f"db={n} jsonl={nj}")
    finally:
        cleanup(name)

    # 9. resume p1+p2
    print("[9/31] resume p1+p2")
    name = "l9"
    d = make_task(name, scfg(name, [f"{base}/p1.html", f"{base}/p2.html"]))
    try:
        run_task(d, limit=2)
        r2 = run_task(d, resume=True)
        rows = read_json(name)
        check(name, r2["total"] == 2 and len(rows) == 4, f"r2={r2['total']} rows={len(rows)}")
    finally:
        cleanup(name)

    # 10. crawl --max 1
    print("[10/31] crawl --max 1")
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
    check("l10_crawl1", len(urls) == 1 and "hub.html" in urls[0], f"urls={urls}")

    # 11. fetch --browser --selector spa
    print("[11/31] fetch --browser --selector spa")
    p = run_cli(["fetch", f"{base}/spa.html", "--browser", "--selector", ".item"], timeout=300)
    ok = "spa-item-1" in p.stdout and "spa-item-3" in p.stdout
    check("l11_browser_sel", ok, f"stdout={p.stdout[:80]}")

    # 12. jobs 2 好任务
    print("[12/31] jobs 2 好任务")
    names = ["l12a", "l12b"]
    for nm in names:
        make_task(nm, scfg(nm, [f"{base}/p2.html"]))
    jobs_fp = ROOT / "outputs" / ".test_jobs21.json"
    jobs_fp.write_text(json.dumps([{"task": str(TMP / n)} for n in names]), encoding="utf-8")
    try:
        p = run_cli(["jobs", "--file", str(jobs_fp)])
        last = next((l for l in reversed(p.stdout.splitlines()) if l.strip()), "")
        data = json.loads(last)
        check("l12_jobs", data.get("jobs") == 2 and data.get("total_items") == 4,
              f"data={data}")
    finally:
        jobs_fp.unlink(missing_ok=True)
        for nm in names:
            cleanup(nm)

    # 13. monitor static 无变化
    print("[13/31] monitor static 无变化")
    name = "l13"
    d = make_task(name, scfg(name, [f"{base}/static.html"]))
    try:
        p = run_cli(["monitor", "--task", str(d), "--every", "1", "--times", "2", "--key", "title"],
                    timeout=120)
        ok = "无变化" in p.stdout and p.stdout.count("新增") == 1
        check(name, ok, f"stdout={p.stdout[-150:]}")
    finally:
        cleanup(name)

    # 14. run --var q=兰州
    print("[14/31] run --var q=兰州")
    name = "l14"
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
        r = run_task(d, overrides={"q": "兰州"})
        items = read_items(name)
        check(name, r["total"] == 1 and items[0]["title"] == "hit-兰州",
              f"total={r['total']} items={[i['title'] for i in items]}")
    finally:
        cleanup(name)

    # 15. fetch --screenshot article
    print("[15/31] fetch --screenshot article")
    shot = "/tmp/us_shot31.png"
    try:
        os.remove(shot)
    except FileNotFoundError:
        pass
    try:
        p = run_cli(["fetch", f"{base}/article.html", "--screenshot", shot], timeout=300)
        ok = Path(shot).exists() and Path(shot).stat().st_size > 5000
        check("l15_screenshot", ok, f"size={Path(shot).stat().st_size if Path(shot).exists() else 0}")
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
