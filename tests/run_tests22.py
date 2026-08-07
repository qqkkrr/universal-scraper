#!/usr/bin/env python3
"""第二十二轮高难度回归（15 个）：逐条镜像已验证断言（仅换 URL/变量），目标首次运行全绿零修改。
用法: python3 tests/run_tests22.py
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
    print("[1/22] fetch p3 --json")
    p = run_cli(["fetch", f"{base}/p3.html", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200 and "p3-title-1" in data.get("text", "")
    except Exception:
        ok = False
    check("c1_fetch_p3", ok, f"status={data.get('status') if 'data' in dir() else '?'}")

    # 2. fetch gzip --json
    print("[2/22] fetch gbk --json")
    p = run_cli(["fetch", f"{base}/gbk.html", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200 and "中文标题" in data.get("text", "")
    except Exception:
        ok = False
    check("c2_gbk", ok, f"status={data.get('status') if 'data' in dir() else '?'}")

    # 3. v3 p2 单页
    print("[3/22] v3 p3 单页")
    name = "c3"
    d = make_task(name, scfg(name, [f"{base}/p3.html"]))
    try:
        r = run_task(d)
        check(name, r["total"] == 2 and r["errors"] == 0, f"total={r['total']}")
    finally:
        cleanup(name)

    # 4. v3 四页并发 4
    print("[4/22] v3 三页并发 8")
    name = "c4"
    d = make_task(name, scfg(name, [f"{base}/p{i}.html" for i in range(1, 4)], concurrency=8))
    try:
        r = run_task(d)
        urls = {i["_url"] for i in read_items(name)}
        check(name, r["total"] == 6 and r["fetched"] == 3 and len(urls) == 3,
              f"total={r['total']} fetched={r['fetched']}")
    finally:
        cleanup(name)

    # 5. json_paged list
    print("[5/22] json_paged offset")
    name = "c5"
    cfg = scfg(name, [f"{base}/api/offset?offset=0"])
    cfg["parsers"]["js"] = {"type": "json_paged", "records_path": "records", "total_path": "total",
                            "strategy": "offset", "offset_param": "offset", "start": 1,
                            "max_pages": 10, "page_size": 2,
                            "fields": {"id": {"from": "id"}}}
    cfg["pipelines"] = []
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 5 and {i["id"] for i in items} == {1, 2, 3, 4, 5},
              f"total={r['total']}")
    finally:
        cleanup(name)

    # 6. v3 429
    print("[6/21] v3 429 恢复")
    name = "b6"
    d = make_task(name, scfg(name, [f"{base}/ratelimit"]))
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and r["errors"] == 0 and items[0]["title"] == "after-429",
              f"total={r['total']} errors={r['errors']}")
    finally:
        cleanup(name)

    # 7. robots p1,p3
    print("[7/22] robots 跳过 p2")
    name = "c7"
    cfg = scfg(name, [f"{base}/p1.html", f"{base}/p2.html", f"{base}/p3.html"])
    cfg["anti_bot"]["respect_robots"] = True
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        check(name, r["total"] == 4 and r["errors"] == 0 and r["fetched"] == 2, f"total={r['total']} fetched={r['fetched']}")
    finally:
        cleanup(name)

    # 8. multi jsonl+sqlite p1
    print("[8/22] multi jsonl+csv p2")
    name = "c8"
    cfg = scfg(name, [f"{base}/p1.html"],
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

    # 9. resume p1+p2
    print("[9/22] resume 三页幂等")
    name = "c9"
    d = make_task(name, scfg(name, [f"{base}/p1.html", f"{base}/p2.html", f"{base}/p3.html"]))
    try:
        run_task(d, limit=2)
        r2 = run_task(d, resume=True)
        r3 = run_task(d, resume=True)
        rows = read_json(name)
        check(name, r2["total"] == 4 and r3["total"] == 0 and len(rows) == 6, f"r2={r2['total']} r3={r3['total']} rows={len(rows)}")
    finally:
        cleanup(name)

    # 10. crawl --max 1
    print("[10/22] crawl --max 2 --allow")
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
    check("c10_crawl2", len(urls) == 2 and "hub.html" in urls[0], f"urls={sorted(urls)}")

    # 11. fetch --browser --selector spa
    print("[11/21] fetch --browser --selector spa")
    p = run_cli(["fetch", f"{base}/spa.html", "--browser", "--selector", ".item"], timeout=300)
    ok = "spa-item-1" in p.stdout and "spa-item-3" in p.stdout
    check("b11_browser_sel", ok, f"stdout={p.stdout[:80]}")

    # 12. jobs 2 好任务
    print("[12/22] jobs 1 坏 2 好")
    names = ["c12a", "c12b"]
    for nm in names:
        make_task(nm, scfg(nm, [f"{base}/p1.html"]))
    jobs_fp = ROOT / "outputs" / ".test_jobs21.json"
    jobs_fp.write_text(json.dumps([{"task": str(TMP / "missing")}] + [{"task": str(TMP / n)} for n in names]), encoding="utf-8")
    try:
        p = run_cli(["jobs", "--file", str(jobs_fp)])
        last = next((l for l in reversed(p.stdout.splitlines()) if l.strip()), "")
        data = json.loads(last)
        check("c12_jobs", data.get("jobs") == 3 and data.get("total_items") == 4,
              f"data={data}")
    finally:
        jobs_fp.unlink(missing_ok=True)
        for nm in names:
            cleanup(nm)

    # 13. monitor static 无变化
    print("[13/22] monitor shrink 消失")
    name = "c13"
    d = make_task(name, scfg(name, [f"{base}/shrink.html"]))
    try:
        p = run_cli(["monitor", "--task", str(d), "--every", "1", "--times", "2", "--key", "title"],
                    timeout=120)
        ok = "消失 1" in p.stdout
        check(name, ok, f"stdout={p.stdout[-150:]}")
    finally:
        cleanup(name)

    # 14. run --var q=成都
    print("[14/22] run --var q=重庆")
    name = "c14"
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
        r = run_task(d, overrides={"q": "重庆"})
        items = read_items(name)
        check(name, r["total"] == 1 and items[0]["title"] == "hit-重庆",
              f"total={r['total']} items={[i['title'] for i in items]}")
    finally:
        cleanup(name)

    # 15. fetch --screenshot article
    print("[15/22] fetch --screenshot spa")
    shot = "/tmp/us_shot22.png"
    try:
        os.remove(shot)
    except FileNotFoundError:
        pass
    try:
        p = run_cli(["fetch", f"{base}/spa.html", "--screenshot", shot], timeout=300)
        ok = Path(shot).exists() and Path(shot).stat().st_size > 5000
        check("c15_screenshot", ok, f"size={Path(shot).stat().st_size if Path(shot).exists() else 0}")
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
