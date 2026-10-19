#!/usr/bin/env python3
"""第四轮高难度回归：v2 引擎（JSON 分页/详情展开/浏览器翻页/文件下载）+
健壮性（max_size 截断/连接拒绝不挂起/浏览器池并发/SIGKILL 中断恢复）+ monitor/中文 URL。
用法: python3 tests/run_tests4.py
"""
from __future__ import annotations

import json
import os
import signal
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


def write_v2(name: str, cfg: dict) -> Path:
    d = ROOT / "outputs" / ".test_tmp_v2"
    d.mkdir(parents=True, exist_ok=True)
    fp = d / f"{name}.json"
    fp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


def cleanup(name: str):
    import glob, shutil
    for pat in (f"outputs/{name}.*", f"outputs/items/{name}.*",
                f"outputs/.state_{name}.json", f"outputs/.pending_{name}.json",
                f"outputs/.run_{name}.log", f"outputs/.seen_{name}.txt",
                f"outputs/.checkpoint_{name}_*.json", f"outputs/.snapshot_{name}.json"):
        for f in glob.glob(str(ROOT / pat)):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
    shutil.rmtree(TMP / name, ignore_errors=True)
    for f in glob.glob(str(ROOT / "outputs/.test_tmp_v2" / f"{name}.json")):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass
    shutil.rmtree(ROOT / "outputs" / "files", ignore_errors=True)


def read_json(name: str) -> list:
    fp = ROOT / "outputs" / f"{name}.json"
    return json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else []


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


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    print(f"mock server: {base}\n")

    # 1. v2 http_json 分页
    print("[1/10] v2 http_json page_param 分页")
    name = "w1_v2_json"
    cfg = {
        "name": name,
        "source": {"type": "http_json", "url": f"{base}/api/list"},
        "pagination": {"strategy": "page_param", "page_param": "page", "start": 1,
                       "max_pages": 10, "records_path": "records", "total_path": "total"},
        "record": {"fields": {"id": {"from": "id"}, "title": {"from": "title"}}},
        "pipeline": [], "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        p = subprocess.run([PY, "-m", "universal_scraper.cli", "run", "--config", str(fp)],
                           capture_output=True, text=True, cwd=ROOT)
        rows = read_json(name)
        check(name, len(rows) == 4 and {r["id"] for r in rows} == {1, 2, 3, 4},
              f"rows={len(rows)} ids={sorted(r['id'] for r in rows)}")
    finally:
        cleanup(name)

    # 2. v2 detail 展开
    print("[2/10] v2 detail 详情展开")
    name = "w2_v2_detail"
    cfg = {
        "name": name,
        "source": {"type": "http_html", "url": f"{base}/list.html",
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
        p = subprocess.run([PY, "-m", "universal_scraper.cli", "run", "--config", str(fp)],
                           capture_output=True, text=True, cwd=ROOT)
        rows = read_json(name)
        ok = len(rows) == 2 and all(r.get("body") == "详情内容-1" for r in rows)
        check(name, ok, f"rows={len(rows)} bodies={[r.get('body') for r in rows]}")
    finally:
        cleanup(name)

    # 3. v2 browser_generic 翻页点击
    print("[3/10] v2 browser_generic 翻页点击")
    name = "w3_v2_browser"
    cfg = {
        "name": name,
        "source": {"type": "browser", "url": f"{base}/paginated.html",
                   "wait": {"selector": ".item", "timeout": 15000},
                   "row_css": ".item", "fields": {"title": {"css": "span.t"}},
                   "pagination": {"type": "click", "selector": "#next", "wait_ms": 400}},
        "pagination": {"strategy": "none", "max_pages": 3},
        "record": {"fields": {"title": {"from": "title"}}},
        "pipeline": [], "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        p = subprocess.run([PY, "-m", "universal_scraper.cli", "run", "--config", str(fp)],
                           capture_output=True, text=True, cwd=ROOT, timeout=180)
        rows = read_json(name)
        check(name, len(rows) == 4 and {r["title"] for r in rows} == {"pg-1", "pg-2", "pg-3", "pg-4"},
              f"rows={len(rows)} titles={sorted(r['title'] for r in rows)}")
    finally:
        cleanup(name)

    # 4. v2 download 文件下载
    print("[4/10] v2 download 文件下载")
    name = "w4_v2_download"
    cfg = {
        "name": name,
        "source": {"type": "http_html", "url": f"{base}/list.html",
                   "row_css": ".item",
                   "fields": {"title": {"css": "span.t"}, "file": {"css": "a.f", "attr": "href"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}, "file": {"from": "file"}}},
        "pipeline": [], "detail": {"enabled": False},
        "download": {"enabled": True, "url_field": "file", "dir": "files", "concurrency": 2},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        p = subprocess.run([PY, "-m", "universal_scraper.cli", "run", "--config", str(fp)],
                           capture_output=True, text=True, cwd=ROOT)
        files = list((ROOT / "outputs" / "files").glob("*")) if (ROOT / "outputs" / "files").exists() else []
        ok = len(files) >= 1 and files[0].read_bytes() == b"HELLO-BINARY-FILE-12345"
        check(name, ok, f"files={[f.name for f in files]}")
    finally:
        cleanup(name)

    # 5. max_size 截断
    print("[5/10] source.max_size 响应截断")
    name = "w5_maxsize"
    cfg = {
        "name": name, "start_urls": [f"{base}/article.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http", "max_size": 150},
        "rules": [{"match": "contains", "pattern": "/article", "parser": "js"}],
        "parsers": {"js": {"type": "html", "fields": {"text": {"css": "body", "limit": 0}}}},
        "pipelines": [{"type": "filter", "field": "text", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["errors"] == 0 and items and len(items[0]["text"]) <= 300,
              f"errors={r['errors']} text_len={len(items[0]['text']) if items else None}")
    finally:
        cleanup(name)

    # 6. 连接拒绝快速失败不挂起
    print("[6/10] 连接拒绝快速失败（不挂起）")
    name = "w6_refused"
    cfg = {
        "name": name, "start_urls": ["http://127.0.0.1:1/nope"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/nope", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"t": {"css": "span.t"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.01, "max_retries": 1, "timeout": 2},
    }
    d = make_task(name, cfg)
    try:
        t0 = time.time()
        r = run_task(d)
        dt = time.time() - t0
        check(name, r["errors"] >= 1 and dt < 20, f"errors={r['errors']} dt={dt:.1f}s")
    finally:
        cleanup(name)

    # 7. 浏览器池 3 并发
    print("[7/10] 浏览器池 3 并发请求")
    from universal_scraper.modules.fetchers import BrowserFetcher
    from universal_scraper.protocols import Request
    f = BrowserFetcher({"type": "browser", "pool": True, "wait_selector": ".item"},
                       {}, {"session_dir": "/tmp/us_test_session4", "min_interval": 0.02})
    results = {}
    try:
        def work(i):
            results[i] = f.fetch(Request(url=f"{base}/p{i}.html"))
        threads = [threading.Thread(target=work, args=(i,)) for i in range(1, 4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok = all(len(results[i].text) > 0 for i in (1, 2, 3)) and f._pool is not None
        check(name, ok, f"lens={[len(results[i].text) for i in (1,2,3)]}")
    finally:
        f.close()

    # 8. monitor 变化检测
    print("[8/10] monitor 快照变化检测")
    name = "w8_monitor"
    cfg = {
        "name": name, "start_urls": [f"{base}/monitor.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/monitor", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        p = subprocess.run([PY, "-m", "universal_scraper.cli", "monitor", "--task", str(d),
                            "--every", "1", "--times", "2", "--key", "title"],
                           capture_output=True, text=True, cwd=ROOT, timeout=120)
        check(name, "新增 1" in p.stdout, f"stdout={p.stdout[-300:]}")
    finally:
        cleanup(name)

    # 9. fetch 中文 URL --json
    print("[9/10] fetch 中文 URL --json")
    p = subprocess.run([PY, "-m", "universal_scraper.cli", "fetch",
                        f"{base}/search?q=数据中心", "--json"],
                       capture_output=True, text=True, cwd=ROOT)
    try:
        data = json.loads(p.stdout)
        inner = json.loads(data.get("text", "{}")) if data.get("text", "").startswith("{") else {}
        ok = data.get("status") == 200 and inner.get("records", [{}])[0].get("title") == "hit-数据中心"
    except Exception:
        ok = False
    check("w9_fetch_zh", ok, f"stdout={p.stdout[:120]}")

    # 10. SIGKILL 中断 → resume 恢复
    print("[10/10] SIGKILL 中断 → --resume 恢复（不重不漏）")
    name = "w10_kill"
    urls = [f"{base}/slow/{i}" for i in range(10)]
    cfg = {
        "name": name, "start_urls": urls,
        "queue": {"max_depth": 1, "max_requests": 50, "max_concurrency": 2},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/slow", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        proc = subprocess.Popen([PY, "-m", "universal_scraper.cli", "run", "--task", str(d)],
                                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2.2)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        pending = (ROOT / "outputs" / f".pending_{name}.json")
        killed = not pending.exists() or json.loads(pending.read_text(encoding="utf-8")) != []
        r = run_task(d, resume=True)
        rows = read_json(name)
        final_urls = [x["_url"] for x in rows]
        ok = killed and len({u.split("/slow/")[1] for u in final_urls}) == 10 and len(final_urls) == 10
        check(name, ok, f"killed_pending={killed} final={len(final_urls)} unique={len(set(final_urls))} resume_total={r['total']}")
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
