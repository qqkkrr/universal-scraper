#!/usr/bin/env python3
"""第十三轮高难度回归（15 个）：组合稳定性场景，断言程序化。
用法: python3 tests/run_tests13.py
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


def make_task(name: str, cfg: dict, modules: dict = None) -> Path:
    d = TMP / name
    (d / "modules").mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    for fn, content in (modules or {}).items():
        (d / "modules" / fn).write_text(content, encoding="utf-8")
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

    # 1. fetch --json --links（无链接页 → links=[] 不崩）
    print("[1/15] fetch --json --links 无链接页")
    p = run_cli(["fetch", f"{base}/table.html", "--json", "--links"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200 and data.get("links") == []
    except Exception:
        ok = False
    check("v1_json_links", ok, f"stdout={p.stdout[:100]}")

    # 2. jsonl+csv 双后端二次运行追加
    print("[2/15] jsonl+csv 双后端二次运行追加")
    name = "v2"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "incremental": {"enabled": True, "key": "title"},
        "storage": {"type": "multi", "backends": [{"type": "jsonl", "name": name},
                                                  {"type": "csv", "name": name}]},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        run_task(d)
        r2 = run_task(d)
        jsonl = ROOT / "outputs/items" / f"{name}.jsonl"
        csv = ROOT / "outputs/items" / f"{name}.csv"
        ok = r2["total"] == 0 and len(jsonl.read_text(encoding="utf-8").splitlines()) == 2 \
             and len(csv.read_text(encoding="utf-8-sig").splitlines()) == 3
        check(name, ok, f"r2={r2['total']} jsonl=2 csv=3")
    finally:
        cleanup(name)

    # 3. crawl --robots --same-domain 组合（hub2：robots 禁 p2）
    print("[3/15] crawl --robots --same-domain 组合")
    p = run_cli(["crawl", f"{base}/hub2.html", "--robots", "--same-domain", "--max", "10"], timeout=300)
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
    ok = len(urls) == 3 and not any("p2.html" in u for u in urls)
    check("v3_robots_samedomain", ok, f"urls={sorted(urls)}")

    # 4. v2 detail + url_transform + download 组合
    print("[4/15] v2 detail + url_transform + download")
    name = "v4"
    cfg = {
        "name": name,
        "source": {"type": "http_html", "url": f"{base}/list.html",
                   "row_css": ".item",
                   "fields": {"title": {"css": "span.t"}, "url": {"css": "a.u", "attr": "href"},
                              "file": {"css": "a.f", "attr": "href"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}, "url": {"from": "url"}, "file": {"from": "file"}}},
        "pipeline": [],
        "detail": {"enabled": True, "url_field": "url", "concurrency": 2,
                   "url_transform": [{"replace": ["/detail/", "/detail/"]}],
                   "extract": [{"name": "body", "type": "css_text", "selector": "h1"}]},
        "download": {"enabled": True, "url_field": "file", "dir": "files", "concurrency": 2},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp)])
        rows = read_json(name)
        files_dir = ROOT / "outputs" / "files"
        nfiles = len(list(files_dir.glob("*"))) if files_dir.exists() else 0
        ok = len(rows) == 2 and all(r.get("body") == "详情内容-1" for r in rows) and nfiles >= 1
        check(name, ok, f"rows={len(rows)} files={nfiles}")
    finally:
        cleanup(name)

    # 5. 自定义 middleware on_request 返回 None（语义：不拦截，保持请求）
    print("[5/15] middleware on_request None 语义")
    name = "v5"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "middleware": [],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    mod = '''from universal_scraper.protocols import BaseMiddleware
class Middleware(BaseMiddleware):
    def on_request(self, req, ctx):
        return None  # None = 不改动，请求继续
'''
    d = make_task(name, cfg, {"middleware.py": mod})
    try:
        r = run_task(d)
        check(name, r["total"] == 2, f"total={r['total']}")
    finally:
        cleanup(name)

    # 6. fetch --screenshot 自动切浏览器
    print("[6/15] fetch --screenshot 自动切浏览器")
    shot = "/tmp/us_shot13.png"
    try:
        os.remove(shot)
    except FileNotFoundError:
        pass
    try:
        p = run_cli(["fetch", f"{base}/article.html", "--screenshot", shot], timeout=300)
        ok = Path(shot).exists() and Path(shot).stat().st_size > 5000
        check("v6_auto_browser", ok, f"size={Path(shot).stat().st_size if Path(shot).exists() else 0}")
    finally:
        try:
            os.remove(shot)
        except FileNotFoundError:
            pass

    # 7. jobs 3 任务（1 坏 + 2 好）
    print("[7/15] jobs 3 任务容错")
    names = ["v7a", "v7b"]
    for nm in names:
        cfg = {
            "name": nm, "start_urls": [f"{base}/p1.html"],
            "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
            "source": {"type": "http"},
            "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
            "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
            "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
            "storage": {"type": "jsonl", "name": nm},
            "output": {"dir": "outputs", "base_name": nm},
            "anti_bot": {"min_interval": 0.0, "max_retries": 1},
        }
        make_task(nm, cfg)
    jobs_fp = ROOT / "outputs" / ".test_jobs13.json"
    jobs_fp.write_text(json.dumps([
        {"task": str(TMP / "missing")},
        {"task": str(TMP / "v7a")}, {"task": str(TMP / "v7b")}]), encoding="utf-8")
    try:
        p = run_cli(["jobs", "--file", str(jobs_fp)])
        last = next((l for l in reversed(p.stdout.splitlines()) if l.strip()), "")
        data = json.loads(last)
        ok = data.get("jobs") == 3 and data.get("total_items") == 4 and "失败跳过" in p.stderr
        check("v7_jobs3", ok, f"data={data}")
    finally:
        jobs_fp.unlink(missing_ok=True)
        for nm in names:
            cleanup(nm)

    # 8. monitor --diff-fields 无变化
    print("[8/15] monitor --diff-fields 无变化")
    name = "v8"
    cfg = {
        "name": name, "start_urls": [f"{base}/static.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/static", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item",
                           "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        p = run_cli(["monitor", "--task", str(d), "--every", "1", "--times", "2",
                     "--key", "title", "--diff-fields", "title"], timeout=120)
        ok = "无变化" in p.stdout
        check(name, ok, f"stdout={p.stdout[-200:]}")
    finally:
        cleanup(name)

    # 9. 浏览器池并发 + 会话保持
    print("[9/15] 浏览器池 4 线程并发 + 会话保持")
    from universal_scraper.modules.fetchers import BrowserFetcher
    from universal_scraper.protocols import Request
    f = BrowserFetcher({"type": "browser", "pool": True, "wait_selector": ".item"},
                       {}, {"session_dir": "/tmp/us_test_session13", "min_interval": 0.02})
    results = {}
    try:
        def work(i, url):
            results[i] = f.fetch(Request(url=url))
        ts = [threading.Thread(target=work, args=(i, f"{base}/p{i}.html")) for i in range(1, 5)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        f.fetch(Request(url=f"{base}/setcookie.html"))
        ok = all(len(results[i].text) > 0 for i in (1, 2, 3, 4)) \
             and "need-ok" in f.fetch(Request(url=f"{base}/needcookie.html")).text
        check("v9_pool_concurrent_session", ok,
              f"lens={[len(results[i].text) for i in (1,2,3,4)]}")
    finally:
        f.close()

    # 10. v2 iterate + detail 组合
    print("[10/15] v2 iterate + detail 组合")
    name = "v10"
    cfg = {
        "name": name, "vars": {"u": "默认"},
        "iterate": {"var": "u", "values": [f"{base}/list.html", f"{base}/list.html"]},
        "source": {"type": "http_html", "url": "{{u}}",
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
        run_cli(["run", "--config", str(fp)])
        rows = read_json(name + "_合并")
        ok = len(rows) == 4 and all(r.get("body") == "详情内容-1" for r in rows)
        check(name, ok, f"rows={len(rows)}")
    finally:
        cleanup(name)

    # 11. run --task --url 覆盖
    print("[11/15] run --task --url 覆盖入口")
    name = "v11"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
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
        data = run_task(d, start_url=f"{base}/p2.html")
        items = read_items(name)
        ok = data.get("total") == 2 and all("p2-title" in i["title"] for i in items)
        check(name, ok, f"total={data.get('total')} titles={[i['title'] for i in items]}")
    finally:
        cleanup(name)

    # 12. 16 worker + multi 三后端
    print("[12/15] 16 worker + multi 三后端")
    name = "v12"
    cfg = {
        "name": name, "start_urls": [f"{base}/p{i}.html" for i in range(1, 6)],
        "queue": {"max_depth": 1, "max_requests": 20, "max_concurrency": 16},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item",
                           "fields": {"title": {"css": "span.t"}, "date": {"css": "span.p"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "multi", "backends": [
            {"type": "jsonl", "name": name}, {"type": "csv", "name": name},
            {"type": "sqlite", "name": name}]},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        import sqlite3
        conn = sqlite3.connect(str(ROOT / "outputs/items" / f"{name}.db"))
        db = conn.execute("select count(*) from items").fetchone()[0]
        conn.close()
        csv = ROOT / "outputs/items" / f"{name}.csv"
        csv_lines = len(csv.read_text(encoding="utf-8-sig").splitlines()) if csv.exists() else 0
        ok = r["total"] == 10 and db == 10 and csv_lines == 11
        check(name, ok, f"total={r['total']} db={db} csv={csv_lines}")
    finally:
        cleanup(name)

    # 13. fetch --links 中文 URL
    print("[13/15] fetch --links 中文 URL")
    p = run_cli(["fetch", f"{base}/search?q=数据中心", "--links", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200 and data.get("links") == []
    except Exception:
        ok = False
    check("v13_zh_links", ok, f"stdout={p.stdout[:100]}")

    # 14. scaffold task 默认模板 validate
    print("[14/15] scaffold task 默认模板 validate")
    outdir = V2 / "scaf14"
    try:
        p1 = run_cli(["scaffold", "--type", "task", "--name", "scaf14", "--out", str(outdir)])
        d = outdir if outdir.suffix == "" else outdir.parent / outdir.stem
        ok1 = (d / "config.json").exists()
        ok2 = False
        if ok1:
            from universal_scraper.config import validate_task
            cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))
            try:
                validate_task(cfg)
                ok2 = True
            except Exception:
                ok2 = False
        check("v14_scaffold_default", ok1 and ok2, f"created={ok1} valid={ok2}")
    finally:
        import shutil
        shutil.rmtree(outdir if outdir.suffix == "" else outdir.parent / outdir.stem, ignore_errors=True)

    # 15. crawl --max 2 --same-domain
    print("[15/15] crawl --max 2 --same-domain")
    p = run_cli(["crawl", f"{base}/hub.html", "--max", "2", "--same-domain", "--allow", r"/p\d\.html"], timeout=300)
    files = sorted(glob.glob(str(ROOT / "outputs" / "crawl_*.json")))
    urls = []
    if files:
        urls = [r["_url"] for r in json.loads(Path(files[-1]).read_text(encoding="utf-8"))]
    for f_ in glob.glob(str(ROOT / "outputs" / "crawl_127_0_0_1_*")):
        try:
            os.remove(f_)
        except FileNotFoundError:
            pass
    ok = len(urls) == 2 and all(base in u for u in urls)
    check("v15_crawl_max2", ok, f"urls={sorted(urls)}")

    print(f"\n===== 结果: {len(PASS)}/15 通过 =====")
    if FAIL:
        print("失败:", FAIL)
        srv.shutdown()
        return 1
    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
