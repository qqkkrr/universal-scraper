#!/usr/bin/env python3
"""第二轮高难度回归：分页+限流、深度控制、正文抽取、crawl allow、--var 模板、
签名钩子、空页终止、URL 规范化去重、CSV 并发动态字段、fetch --selector --json。
用法: python3 tests/run_tests2.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

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
    for pat in (f"outputs/{name}.*", f"outputs/items/{name}.*",
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

    # ---- 1. 分页 + 429 Retry-After 组合 ----
    print("[1/10] JSON 分页中第 2 页首次 429，全量恢复")
    name = "u1_paged429"
    cfg = {
        "name": name, "start_urls": [f"{base}/api/paged429?page=1"],
        "queue": {"max_depth": 5, "max_requests": 20, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/api/paged429", "parser": "js"}],
        "parsers": {"js": {"type": "json_paged", "records_path": "records", "total_path": "total",
                           "strategy": "page_param", "page_param": "page", "start": 1,
                           "max_pages": 10, "page_size": 2,
                           "fields": {"id": {"from": "id"}, "title": {"from": "title"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 6 and r["errors"] == 0 and len({i["id"] for i in items}) == 6,
              f"total={r['total']} errors={r['errors']} ids={sorted(i['id'] for i in items)}")
    finally:
        cleanup(name)

    # ---- 2. 递归深度控制 ----
    print("[2/10] 递归 max_depth=2 不越级")
    name = "u2_depth"
    cfg = {
        "name": name, "start_urls": [f"{base}/site/level1.html"],
        "queue": {"max_depth": 1, "max_requests": 20, "max_concurrency": 2},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/site", "parser": "page"}],
        "parsers": {"page": {"type": "html", "row_css": ".item",
                             "fields": {"t": {"css": "span.t"}},
                             "extract_links": {"allow": "/site/"}}},
        "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        ts = [i["t"] for i in read_items(name)]
        check(name, r["fetched"] == 3 and "L2" in ts and "L3" not in ts,
              f"fetched={r['fetched']} ts={ts}")
    finally:
        cleanup(name)

    # ---- 3. 正文抽取（剔除导航/页脚）----
    print("[3/10] fetch --article 正文抽取剔噪")
    from universal_scraper.quick import fetch_url
    res = fetch_url(f"{base}/article.html", article=True)
    art = res.get("article", "")
    check("u3_article", "标题一" in art and "正文第一段" in art and "导航垃圾" not in art and "页脚垃圾" not in art,
          f"article={art[:80]}...")

    # ---- 4. crawl --allow /docs/ ----
    print("[4/10] crawl --allow 只爬 /docs/")
    p = subprocess.run([PY, "-m", "universal_scraper.cli", "crawl", f"{base}/hubdocs.html",
                        "--max", "10", "--allow", "/docs/"], capture_output=True, text=True, cwd=ROOT)
    rows = json.loads((ROOT / "outputs" / "crawl_127_0_0_1_*.json").parent.glob("crawl_*.json").__next__().read_text(encoding="utf-8"))
    urls = [r["_url"] for r in rows]
    for f in (ROOT / "outputs").glob("crawl_127_0_0_1_*"):
        f.unlink()
    check("u4_crawl_allow", all("docs" in u for u in urls) and len(urls) >= 2,
          f"urls={urls}")

    # ---- 5. run --var 模板替换 ----
    print("[5/10] run --var 模板替换（search?q={{keyword}}）")
    name = "u5_var"
    cfg = {
        "name": name, "vars": {"keyword": "默认"},
        "start_urls": [f"{base}/search?q={{{{keyword}}}}"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/search", "parser": "js"}],
        "parsers": {"js": {"type": "json", "records_path": "records",
                           "fields": {"title": {"from": "title"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d, overrides={"keyword": "数据中心"})
        items = read_items(name)
        check(name, r["total"] == 1 and items and items[0]["title"] == "hit-数据中心",
              f"total={r['total']} items={items}")
    finally:
        cleanup(name)

    # ---- 6. 签名钩子 sign_hook ----
    print("[6/10] sign_hook 签名头注入")
    name = "u6_sign"
    cfg = {
        "name": name, "start_urls": [f"{base}/signed"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http", "sign_hook": "sign_hook_mod:add_sign"},
        "rules": [{"match": "contains", "pattern": "/signed", "parser": "js"}],
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
        check(name, r["total"] == 1 and items and items[0]["t"] == "signed-ok",
              f"total={r['total']} errors={r['errors']} items={items}")
    finally:
        cleanup(name)

    # ---- 7. 空页提前终止 ----
    print("[7/10] JSON 分页空页提前终止（无 total 无 records）")
    name = "u7_emptypage"
    cfg = {
        "name": name, "start_urls": [f"{base}/api/emptypage?page=1"],
        "queue": {"max_depth": 5, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/api/emptypage", "parser": "js"}],
        "parsers": {"js": {"type": "json_paged", "records_path": "records", "strategy": "page_param",
                           "page_param": "page", "start": 1, "max_pages": 10, "page_size": 2,
                           "fields": {"title": {"from": "title"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 2 and r["fetched"] == 2,
              f"total={r['total']} fetched={r['fetched']}")
    finally:
        cleanup(name)

    # ---- 8. URL 规范化去重（#fragment）----
    print("[8/10] URL 规范化：/p1.html#x 与 /p1.html 不重复抓")
    name = "u8_fragment"
    cfg = {
        "name": name, "start_urls": [f"{base}/hub3.html"],
        "queue": {"max_depth": 2, "max_requests": 20, "max_concurrency": 2},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/hub3", "parser": "list"},
                  {"match": "contains", "pattern": "/p1", "parser": "js"}],
        "parsers": {"list": {"type": "html", "fields": {"h": {"css": "h1"}},
                             "extract_links": {"allow": "/p1"}},
                    "js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "h", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        urls = [i["_url"] for i in read_items(name)]
        no_frag = all("#" not in u for u in urls)
        check(name, r["fetched"] == 3 and no_frag,
              f"fetched={r['fetched']} urls={urls}")
    finally:
        cleanup(name)

    # ---- 9. CSV 并发写 + 动态字段 ----
    print("[9/10] CSV 8 worker 并发写 + 动态字段扩展")
    name = "u9_csv"
    cfg = {
        "name": name,
        "start_urls": [f"{base}/p{i}.html" for i in range(1, 6)],
        "queue": {"max_depth": 1, "max_requests": 20, "max_concurrency": 8},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item",
                           "fields": {"title": {"css": "span.t"}, "date": {"css": "span.p"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"},
                      {"type": "add", "field": "extra", "value": "E"}],
        "storage": {"type": "csv", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        csv_fp = ROOT / "outputs" / "items" / f"{name}.csv"
        lines = csv_fp.read_text(encoding="utf-8-sig").splitlines() if csv_fp.exists() else []
        header_fields = set((lines[0] if lines else "").split(","))
        header_ok = {"title", "date", "extra", "_url", "_parser"} <= header_fields
        check(name, r["total"] == 10 and len(lines) == 11 and header_ok,
              f"total={r['total']} csv_lines={len(lines)} header={lines[0] if lines else None}")
    finally:
        cleanup(name)

    # ---- 10. fetch --selector --json ----
    print("[10/10] fetch --selector .item --json")
    p = subprocess.run([PY, "-m", "universal_scraper.cli", "fetch", f"{base}/p1.html",
                        "--selector", ".item", "--json"], capture_output=True, text=True, cwd=ROOT)
    try:
        data = json.loads(p.stdout)
        ok = "p1-title-1" in data.get("selector", "") and data.get("status") == 200
    except Exception:
        ok = False
    check("u10_fetch_selector_json", ok, f"stdout={p.stdout[:120]}")

    print(f"\n===== 结果: {len(PASS)}/10 通过 =====")
    if FAIL:
        print("失败:", FAIL)
        srv.shutdown()
        return 1
    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
