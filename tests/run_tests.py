#!/usr/bin/env python3
"""高难度回归套件：10 个贴近实战的场景，全自动跑，输出 PASS/FAIL。
覆盖：浏览器动作链/弹窗/stealth、三种 JSON 分页、429 Retry-After、robots+follow、
增量去重+多后端存储、断点续跑、浏览器池自愈、CLI fetch 截图/外链、并发去重限速。
用法: python3 tests/run_tests.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from mock_sites import Handler  # noqa: E402
from universal_scraper.engine_v3 import run_task  # noqa: E402

PY = sys.executable
TMP = ROOT / "outputs" / ".test_tmp"


def make_task(name: str, cfg: dict) -> Path:
    d = TMP / name
    (d / "modules").mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def cleanup(name: str):
    import glob
    for pat in (f"outputs/{name}.*", f"outputs/items/{name}.*",
                f"outputs/.state_{name}.json", f"outputs/.pending_{name}.json",
                f"outputs/.run_{name}.log", f"outputs/.seen_{name}.txt"):
        for f in glob.glob(str(ROOT / pat)):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
    for f in glob.glob(str(TMP / name)):
        import shutil
        shutil.rmtree(f, ignore_errors=True)


def read_json(name: str) -> list:
    fp = ROOT / "outputs" / f"{name}.json"
    return json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else []


def read_items(name: str) -> list:
    fp = ROOT / "outputs" / "items" / f"{name}.jsonl"
    if not fp.exists():
        return []
    return [json.loads(x) for x in fp.read_text(encoding="utf-8").splitlines() if x.strip()]


def base_cfg(name: str, url: str, parser_type: str = "html", extra=None) -> dict:
    cfg = {
        "name": name,
        "start_urls": [url],
        "queue": {"max_depth": 1, "max_requests": 30, "max_concurrency": 2},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "js"}],
        "parsers": {"js": {"type": parser_type, "row_css": ".item",
                           "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.05, "max_retries": 1},
    }
    if extra:
        cfg.update(extra)
    return cfg


PASS = []
FAIL = []


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

    # ---- 1. 浏览器动作链 + 弹窗清理 + stealth（池）----
    print("[1/10] 浏览器：点击加载更多 + cookie 弹窗清理 + stealth")
    name = "t1_actions"
    cfg = base_cfg(name, f"{base}/actions.html")
    cfg["source"] = {"type": "browser", "pool": True, "stealth": True, "remove_overlays": True,
                     "actions": [{"type": "click", "selector": "#more", "ms": 500},
                                 {"type": "wait", "ms": 500}]}
    cfg["queue"]["max_concurrency"] = 1
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 6 and any(i["title"] == "loaded-3" for i in items),
              f"total={r['total']} titles={[i['title'] for i in items]}")
    finally:
        cleanup(name)

    # ---- 2. JSON 分页 offset 策略 ----
    print("[2/10] JSON 分页 offset 策略")
    name = "t2_offset"
    cfg = base_cfg(name, f"{base}/api/offset?offset=0")
    cfg["parsers"]["js"] = {"type": "json_paged", "records_path": "records", "total_path": "total",
                            "strategy": "offset", "offset_param": "offset", "start": 1,
                            "max_pages": 10, "page_size": 2,
                            "fields": {"id": {"from": "id"}, "title": {"from": "title"}}}
    cfg["pipelines"] = []
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 5 and len({i["id"] for i in items}) == 5,
              f"total={r['total']} ids={sorted(i['id'] for i in items)}")
    finally:
        cleanup(name)

    # ---- 3. JSON 分页 next_url 策略 ----
    print("[3/10] JSON 分页 next_url 策略")
    name = "t3_next"
    cfg = base_cfg(name, f"{base}/api/next?page=1")
    cfg["parsers"]["js"] = {"type": "json_paged", "records_path": "records", "strategy": "next_url",
                            "next_path": "next", "start": 1, "max_pages": 10,
                            "fields": {"id": {"from": "id"}, "title": {"from": "title"}}}
    cfg["pipelines"] = []
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 4 and len({i["id"] for i in items}) == 4,
              f"total={r['total']} fetched={r['fetched']} ids={sorted(i['id'] for i in items)}")
    finally:
        cleanup(name)

    # ---- 4. 429 Retry-After 队列重试 ----
    print("[4/10] 429 + Retry-After 延迟重试")
    name = "t4_ratelimit"
    cfg = base_cfg(name, f"{base}/ratelimit")
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and r["errors"] == 0 and items[0]["title"] == "after-429",
              f"total={r['total']} errors={r['errors']} items={items}")
    finally:
        cleanup(name)

    # ---- 5. robots + follow 组合 ----
    print("[5/10] robots.txt Disallow + follow=false 组合")
    name = "t5_robots_follow"
    cfg = {
        "name": name, "start_urls": [f"{base}/hub2.html"],
        "queue": {"max_depth": 2, "max_requests": 10, "max_concurrency": 2},
        "source": {"type": "http"},
        "rules": [
            {"match": "contains", "pattern": "/hub2", "parser": "list", "follow": True},
            {"match": "contains", "pattern": "/p1", "parser": "js", "follow": True},
            {"match": "contains", "pattern": "/p2", "parser": "js", "follow": True},
            {"match": "contains", "pattern": "/p3", "parser": "js", "follow": False},
        ],
        "parsers": {"list": {"type": "html", "fields": {"page": {"css": "h1"}},
                             "extract_links": {"allow": r"/p\d\.html"}},
                    "js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "page", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.05, "max_retries": 1, "respect_robots": True},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        urls = [i.get("_url", "") for i in read_items(name)]
        check(name, r["fetched"] == 2 and not any("p2" in u for u in urls) and not any("p3" in u for u in urls),
              f"fetched={r['fetched']} urls={urls}")
    finally:
        cleanup(name)

    # ---- 6. 增量去重 + 多后端存储 ----
    print("[6/10] 增量去重 + 多后端存储（jsonl+sqlite）")
    name = "t6_inc_multi"
    cfg = base_cfg(name, f"{base}/p1.html")
    cfg["start_urls"] = [f"{base}/p1.html", f"{base}/p2.html"]
    cfg["storage"] = {"type": "multi", "backends": [{"type": "jsonl", "name": name},
                                                    {"type": "sqlite", "name": name}]}
    cfg["incremental"] = {"enabled": True, "key": "title"}
    d = make_task(name, cfg)
    try:
        r1 = run_task(d)
        r2 = run_task(d)
        import sqlite3
        conn = sqlite3.connect(str(ROOT / "outputs/items" / f"{name}.db"))
        rows = conn.execute("select count(*) from items").fetchone()[0]
        conn.close()
        check(name, r1["total"] == 4 and r2["total"] == 0 and rows == 4,
              f"run1={r1['total']} run2={r2['total']} sqlite={rows}")
    finally:
        cleanup(name)

    # ---- 7. 断点续跑 ----
    print("[7/10] 断点续跑（limit 截断后 --resume 合并）")
    name = "t7_resume"
    cfg = base_cfg(name, f"{base}/p1.html")
    cfg["start_urls"] = [f"{base}/p1.html", f"{base}/p2.html", f"{base}/p3.html"]
    cfg["queue"]["max_requests"] = 10
    d = make_task(name, cfg)
    try:
        r1 = run_task(d, limit=2)
        r2 = run_task(d, resume=True)
        rows = read_json(name)
        titles = {x["title"] for x in rows}
        check(name, r1["total"] == 2 and len(rows) == 6 and len(titles) == 6,
              f"r1={r1['total']} final_rows={len(rows)} titles={len(titles)}")
    finally:
        cleanup(name)

    # ---- 8. 浏览器池自愈 ----
    print("[8/10] 浏览器池自愈（kill 后自动重启）")
    from universal_scraper.modules.fetchers import BrowserFetcher
    from universal_scraper.protocols import Request
    f = BrowserFetcher({"type": "browser", "pool": True, "wait_selector": ".item"},
                       {}, {"session_dir": "/tmp/us_test_session", "min_interval": 0.05})
    try:
        r1 = f.fetch(Request(url=f"{base}/p1.html"))
        pid1 = f._pool.pid
        os.kill(pid1, signal.SIGKILL)
        time.sleep(1.2)
        r2 = f.fetch(Request(url=f"{base}/p2.html"))
        ok = len(r1.text) > 0 and len(r2.text) > 0 and f._pool is not None and f._pool.pid != pid1
        check(name, ok, f"pid1={pid1} pid2={f._pool.pid if f._pool else None}")
    finally:
        f.close()

    # ---- 9. CLI fetch 截图 + 外链 ----
    print("[9/10] CLI fetch：--screenshot + --links")
    shot = "/tmp/us_test_shot.png"
    for f in ([shot] if Path(shot).exists() else []):
        os.remove(f)
    p1 = subprocess.run([PY, "-m", "universal_scraper.cli", "fetch", f"{base}/actions.html",
                         "--screenshot", shot], capture_output=True, text=True, cwd=ROOT)
    p2 = subprocess.run([PY, "-m", "universal_scraper.cli", "fetch", f"{base}/hub.html", "--links"],
                        capture_output=True, text=True, cwd=ROOT)
    ok_shot = Path(shot).exists() and Path(shot).stat().st_size > 5000
    ok_links = "6 个外链" in p2.stdout and "p1.html" in p2.stdout
    check(name, ok_shot and ok_links, f"shot={ok_shot} links={ok_links}")
    try:
        os.remove(shot)
    except FileNotFoundError:
        pass

    # ---- 10. 并发去重 + 限速 ----
    print("[10/10] 8 worker 并发：重复种子去重 + 同域限速")
    name = "t10_concurrency"
    cfg = base_cfg(name, f"{base}/p1.html")
    cfg["start_urls"] = [f"{base}/p{i}.html" for i in range(1, 6)] * 3
    cfg["queue"] = {"max_depth": 1, "max_requests": 20, "max_concurrency": 8}
    cfg["anti_bot"]["min_interval"] = 0.02
    d = make_task(name, cfg)
    try:
        t0 = time.time()
        r = run_task(d)
        dt = time.time() - t0
        items = read_items(name)
        urls = [i["_url"] for i in items]
        check(name, r["fetched"] == 5 and r["total"] == 10 and len(set(urls)) == 5,
              f"fetched={r['fetched']} items={r['total']} unique_urls={len(set(urls))} dt={dt:.1f}s")
    finally:
        cleanup(name)

    print(f"\n===== 结果: {len(PASS)}/10 通过 =====")
    if FAIL:
        print("失败:", FAIL)
        srv.shutdown()
        return 1
    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
