#!/usr/bin/env python3
"""第六轮高难度回归（15 个）：cookie/会话持久化、gzip/GBK 编码、重定向、规则优先级、
分页参数保留、v2 offset、增量+断点组合、16 并发压力、jobs var、schedule、组合 CLI、
HTTP-date 限流、--log-file。
用法: python3 tests/run_tests6.py
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
                f"outputs/.checkpoint_{name}_*.json"):
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

    # 1. v3 anti_bot.cookies
    print("[1/15] v3 anti_bot.cookies 请求 Cookie")
    name = "y1"
    cfg = {
        "name": name, "start_urls": [f"{base}/cookies"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/cookies", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"t": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "cookies": {"us_cookie": "1"}},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and items[0]["t"] == "cookie-ok", f"items={items}")
    finally:
        cleanup(name)

    # 2. HTTP 302 重定向
    print("[2/15] HTTP 302 重定向")
    name = "y2"
    cfg = {
        "name": name, "start_urls": [f"{base}/redirect"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 2 and all("p1-title" in i["title"] for i in items),
              f"items={[i['title'] for i in items]}")
    finally:
        cleanup(name)

    # 3. gzip 响应解码
    print("[3/15] gzip 响应解码")
    name = "y3"
    cfg = {
        "name": name, "start_urls": [f"{base}/gzip"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/gzip", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"t": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and items[0]["t"] == "gzip-ok", f"items={items}")
    finally:
        cleanup(name)

    # 4. GBK 编码解码
    print("[4/15] GBK 中文编码解码")
    name = "y4"
    cfg = {
        "name": name, "start_urls": [f"{base}/gbk.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/gbk", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"t": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and items[0]["t"] == "中文标题",
              f"items={items}")
    finally:
        cleanup(name)

    # 5. 浏览器池会话持久化（setcookie → needcookie 同池跨页）
    print("[5/15] 浏览器池会话持久化（登录态跨页）")
    from universal_scraper.modules.fetchers import BrowserFetcher
    from universal_scraper.protocols import Request
    f = BrowserFetcher({"type": "browser", "pool": True, "wait_selector": ".item"},
                       {}, {"session_dir": "/tmp/us_test_session6", "min_interval": 0.02})
    try:
        r1 = f.fetch(Request(url=f"{base}/setcookie.html"))
        r2 = f.fetch(Request(url=f"{base}/needcookie.html"))
        ok = len(r1.text) > 0 and "need-ok" in r2.text and r2.status == 200
        check("y5_session", ok, f"need_status={r2.status} has_need_ok={'need-ok' in r2.text}")
    finally:
        f.close()

    # 6. 规则优先级（先匹配先赢）
    print("[6/15] 规则优先级（先匹配先赢）")
    name = "y6"
    cfg = {
        "name": name, "start_urls": [f"{base}/dual.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [
            {"match": "contains", "pattern": "/dual", "parser": "first"},
            {"match": "regex", "pattern": "/du", "parser": "second"},
        ],
        "parsers": {"first": {"type": "html", "fields": {"t": {"css": "span.t"}}},
                    "second": {"type": "html", "fields": {"s": {"css": "span.s"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and items[0].get("t") == "first-parser" and "s" not in items[0],
              f"items={items}")
    finally:
        cleanup(name)

    # 7. json_paged 保留起始查询参数
    print("[7/15] json_paged 保留起始查询参数（type=1&page=2）")
    name = "y7"
    cfg = {
        "name": name, "start_urls": [f"{base}/api/list2?type=1&page=1"],
        "queue": {"max_depth": 5, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/api/list2", "parser": "js"}],
        "parsers": {"js": {"type": "json_paged", "records_path": "records", "total_path": "total",
                           "strategy": "page_param", "page_param": "page", "start": 1,
                           "max_pages": 10, "page_size": 2,
                           "fields": {"id": {"from": "id"}, "type": {"from": "type"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        types = {i["type"] for i in items}
        urls = [i["_url"] for i in items]
        check(name, r["total"] == 4 and types == {"1"} and any("page=2" in u for u in urls),
              f"total={r['total']} types={types} urls={urls}")
    finally:
        cleanup(name)

    # 8. v2 offset 分页 + limit_param
    print("[8/15] v2 offset 分页 + limit_param")
    name = "y8"
    cfg = {
        "name": name,
        "source": {"type": "http_json", "url": f"{base}/api/offset"},
        "pagination": {"strategy": "offset", "offset_param": "offset", "limit_param": "limit",
                       "limit": 2, "start": 1, "max_pages": 10,
                       "records_path": "records", "total_path": "total"},
        "record": {"fields": {"id": {"from": "id"}}},
        "pipeline": [], "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp)])
        rows = read_json(name)
        check(name, len(rows) == 5 and {r["id"] for r in rows} == {1, 2, 3, 4, 5},
              f"rows={len(rows)} ids={sorted(r['id'] for r in rows)}")
    finally:
        cleanup(name)

    # 9. incremental + resume 组合
    print("[9/15] incremental + resume 组合（不重不漏）")
    name = "y9"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html", f"{base}/p2.html", f"{base}/p3.html"],
        "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 2},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "incremental": {"enabled": True, "key": "title"},
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r1 = run_task(d, limit=2)
        r2 = run_task(d, resume=True)
        rows = read_json(name)
        titles = {x["title"] for x in rows}
        check(name, r1["total"] == 2 and r2["total"] == 4 and len(titles) == 6,
              f"r1={r1['total']} r2={r2['total']} unique={len(titles)}")
    finally:
        cleanup(name)

    # 10. 16 worker 高并发压力
    print("[10/15] 16 worker 高并发压力（20 重复种子去重）")
    name = "y10"
    cfg = {
        "name": name, "start_urls": [f"{base}/p{i}.html" for i in range(1, 6)] * 4,
        "queue": {"max_depth": 1, "max_requests": 30, "max_concurrency": 16},
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
        t0 = time.time()
        r = run_task(d)
        dt = time.time() - t0
        urls = {i["_url"] for i in read_items(name)}
        check(name, r["fetched"] == 5 and r["total"] == 10 and len(urls) == 5 and dt < 20,
              f"fetched={r['fetched']} items={r['total']} dt={dt:.1f}s")
    finally:
        cleanup(name)

    # 11. jobs var 覆盖
    print("[11/15] jobs 编排带 var 覆盖")
    name = "y11"
    cfg = {
        "name": name, "vars": {"q": "默认"},
        "start_urls": [f"{base}/search?q={{{{q}}}}"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/search", "parser": "js"}],
        "parsers": {"js": {"type": "json", "records_path": "records", "fields": {"title": {"from": "title"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    jobs_fp = ROOT / "outputs" / ".test_jobs6.json"
    jobs_fp.write_text(json.dumps([{"task": str(d), "var": {"q": "数据中心"}}]), encoding="utf-8")
    try:
        p = run_cli(["jobs", "--file", str(jobs_fp)])
        data = json.loads(p.stdout.strip().splitlines()[-1])
        items = read_items(name)
        check(name, data.get("total_items") == 1 and items and items[0]["title"] == "hit-数据中心",
              f"total={data.get('total_items')} items={items}")
    finally:
        jobs_fp.unlink(missing_ok=True)
        cleanup(name)

    # 12. schedule 定时运行 2 次
    print("[12/15] schedule 定时运行 2 次")
    name = "y12"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        p = run_cli(["schedule", "--task", str(d), "--every", "1", "--times", "2"], timeout=120)
        check(name, "第 2 次运行" in p.stdout, f"stdout={p.stdout[-200:]}")
    finally:
        cleanup(name)

    # 13. fetch 组合（screenshot + links + selector）
    print("[13/15] fetch 组合：--screenshot + --links + --selector")
    shot = "/tmp/us_shot6.png"
    try:
        os.remove(shot)
    except FileNotFoundError:
        pass
    p = run_cli(["fetch", f"{base}/list.html", "--screenshot", shot,
                 "--links", "--selector", ".item"], timeout=300)
    ok_shot = Path(shot).exists() and Path(shot).stat().st_size > 5000
    ok_links = "2 个外链" in p.stdout and "detail/1.html" in p.stdout
    ok_sel = "dl-1" in p.stdout
    try:
        os.remove(shot)
    except FileNotFoundError:
        pass
    check("y13_fetch_combo", ok_shot and ok_links and ok_sel, f"shot={ok_shot} links={ok_links} sel={ok_sel}")

    # 14. HTTP-date Retry-After
    print("[14/15] HTTP-date Retry-After 解析")
    from universal_scraper.modules.fetchers import HttpFetcher
    from universal_scraper.protocols import Request, RateLimitedError
    f = HttpFetcher({"type": "http", "headers": {"X-N": "0"}}, {},
                    {"min_interval": 0.0, "max_retries": 1, "http_backend": "auto"})
    ra = None
    try:
        f.fetch(Request(url=f"{base}/ratelimit-date"))
    except RateLimitedError as e:
        ra = e.retry_after
    f2 = HttpFetcher({"type": "http", "headers": {"X-N": "1"}}, {},
                     {"min_interval": 0.0, "max_retries": 1, "http_backend": "auto"})
    r2 = f2.fetch(Request(url=f"{base}/ratelimit-date"))
    check("y14_retry_after_date", ra is not None and 0 < ra <= 5 and "after-date" in r2.text,
          f"retry_after={ra} second_ok={'after-date' in r2.text}")

    # 15. --log-file 落盘
    print("[15/15] run --log-file 落盘")
    name = "y15"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    logf = "/tmp/us_log6.log"
    try:
        os.remove(logf)
    except FileNotFoundError:
        pass
    try:
        p = run_cli(["run", "--task", str(d), "--log-file", logf])
        ok = Path(logf).exists() and "任务启动" in Path(logf).read_text(encoding="utf-8")
        check("y15_logfile", ok, f"exists={Path(logf).exists()}")
    finally:
        try:
            os.remove(logf)
        except FileNotFoundError:
            pass
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
