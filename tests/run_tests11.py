#!/usr/bin/env python3
"""第十一轮高难度回归（15 个）：v2 全链路、插件四件套同包、多后端三写、
组合 CLI、递归深度、会话保持、稳定性（monitor×3 / dry-run 组合 / 管道纯度）。
用法: python3 tests/run_tests11.py
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
from universal_scraper.config import validate_task  # noqa: E402

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

    # 1. v2 全链路：list + detail + download + resume
    print("[1/15] v2 全链路：list+detail+download+resume")
    name = "t11a"
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
                   "extract": [{"name": "body", "type": "css_text", "selector": "h1"}]},
        "download": {"enabled": True, "url_field": "file", "dir": "files", "concurrency": 2},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp), "--limit", "1"])
        run_cli(["run", "--config", str(fp), "--resume"])
        rows = read_json(name)
        files = list((ROOT / "outputs" / "files").glob("*")) if (ROOT / "outputs" / "files").exists() else []
        ok = len(rows) == 2 and all(r.get("body") == "详情内容-1" for r in rows) and len(files) >= 1
        check(name, ok, f"rows={len(rows)} files={len(files)}")
    finally:
        cleanup(name)

    # 2. v2 offset + iterate 组合
    print("[2/15] v2 offset + iterate 组合")
    name = "t11b"
    cfg = {
        "name": name, "vars": {"p": "0"},
        "iterate": {"var": "p", "values": ["0", "2"]},
        "source": {"type": "http_json", "url": f"{base}/api/offset?offset={{{{p}}}}"},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"id": {"from": "id"}}},
        "pipeline": [], "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp)])
        rows = read_json(name + "_合并")
        ids = {r["id"] for r in rows}
        check(name, len(rows) == 4 and ids == {1, 2, 3, 4}, f"rows={len(rows)} ids={sorted(ids)}")
    finally:
        cleanup(name)

    # 3. 插件四件套同包
    print("[3/15] 插件四件套同包（fetcher+parser+pipeline+storage）")
    name = "t11c"
    cfg = {
        "name": name, "start_urls": ["http://x/fake"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "custom"},
        "rules": [{"match": "contains", "pattern": "/fake", "parser": "p"}],
        "parsers": {"p": {}},
        "pipelines": [],
        "storage": {"type": "custom", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0},
    }
    modules = {
        "fetcher.py": '''from universal_scraper.protocols import BaseFetcher, Response
class Fetcher(BaseFetcher):
    name = "custom"
    def fetch(self, req):
        return Response(request=req, status=200,
                        text="<html><body><span class='t'>kit</span></body></html>")
''',
        "parser.py": '''from universal_scraper.protocols import BaseParser, ParseResult
class Parser(BaseParser):
    name = "p"
    def parse(self, resp, ctx):
        return ParseResult(items=[{"v": "raw"}])
''',
        "pipeline.py": '''from universal_scraper.protocols import BasePipeline
class Pipeline(BasePipeline):
    def process(self, item):
        item["v"] = item["v"].upper() + "-OK"
        return item
''',
        "storage.py": '''import json, os
from universal_scraper.protocols import BaseStorage
class Storage(BaseStorage):
    name = "custom"
    def __init__(self, config, task_vars):
        self.dir = config.get("dir", "outputs"); self.name = config.get("name", "items")
    def open(self, name):
        os.makedirs(self.dir, exist_ok=True)
        self.f = open(os.path.join(self.dir, f"{name}.custom"), "w", encoding="utf-8")
    def write(self, item):
        self.f.write(json.dumps(item, ensure_ascii=False) + "\\n")
    def close(self):
        self.f.close()
''',
    }
    d = make_task(name, cfg, modules)
    try:
        r = run_task(d)
        fp = ROOT / "outputs" / "items" / f"{name}.custom"
        content = fp.read_text(encoding="utf-8") if fp.exists() else ""
        ok = r["total"] == 1 and "RAW-OK" in content
        check(name, ok, f"total={r['total']} content={content[:60]}")
    finally:
        cleanup(name)
        try:
            os.remove(ROOT / "outputs" / "items" / f"{name}.custom")
        except FileNotFoundError:
            pass

    # 4. multi 三后端 + incremental
    print("[4/15] multi 三后端（jsonl+csv+sqlite）+ incremental")
    name = "t11d"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html", f"{base}/p2.html"],
        "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 4},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item",
                           "fields": {"title": {"css": "span.t"}, "date": {"css": "span.p"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "incremental": {"enabled": True, "key": "title"},
        "storage": {"type": "multi", "backends": [
            {"type": "jsonl", "name": name}, {"type": "csv", "name": name},
            {"type": "sqlite", "name": name}]},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r1 = run_task(d)
        r2 = run_task(d)
        import sqlite3
        conn = sqlite3.connect(str(ROOT / "outputs/items" / f"{name}.db"))
        db_rows = conn.execute("select count(*) from items").fetchone()[0]
        conn.close()
        csv_fp = ROOT / "outputs/items" / f"{name}.csv"
        csv_lines = len(csv_fp.read_text(encoding="utf-8-sig").splitlines()) if csv_fp.exists() else 0
        ok = r1["total"] == 4 and r2["total"] == 0 and db_rows == 4 and csv_lines == 5
        check(name, ok, f"r1={r1['total']} r2={r2['total']} db={db_rows} csv={csv_lines}")
    finally:
        cleanup(name)

    # 5. fetch --selector 中文内容
    print("[5/15] fetch --selector 中文内容")
    p = run_cli(["fetch", f"{base}/search?q=数据中心", "--selector", ".item"])
    ok = p.returncode == 0  # JSON 页 selector 无匹配 → 不崩、rc 0
    p2 = run_cli(["fetch", f"{base}/article.html", "--selector", "h1"])
    ok2 = "标题一" in p2.stdout
    check("t11e_fetch_zh_sel", ok and ok2, f"rc={p.returncode} article_sel={'标题一' in p2.stdout}")

    # 6. scaffold http_json → validate
    print("[6/15] scaffold http_json → validate 通过")
    out = V2 / "scaf_json.json"
    try:
        p = run_cli(["scaffold", "--type", "http_json", "--name", "scaf_json", "--out", str(out)])
        ok1 = out.exists()
        p2 = run_cli(["validate", "--config", str(out)])
        ok2 = p2.returncode == 0 and "配置有效" in p2.stdout
        check("t11f_scaffold", ok1 and ok2, f"created={ok1} valid={ok2}")
    finally:
        try:
            os.remove(out)
        except FileNotFoundError:
            pass

    # 7. list 命令
    print("[7/15] list 命令列出配置")
    p = run_cli(["list"])
    ok = p.returncode == 0 and "ggzy_datacenter_2025-01.json" in p.stdout
    check("t11g_list", ok, f"stdout={p.stdout[:100]}")

    # 8. json_paged + source.query + cookies 三合一
    print("[8/15] json_paged + source.query + cookies 三合一")
    name = "t11h"
    cfg = {
        "name": name, "start_urls": [f"{base}/api/list2?page=1"],
        "queue": {"max_depth": 5, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http", "query": {"type": "8", "x": "y"}},
        "rules": [{"match": "contains", "pattern": "/api/list2", "parser": "js"}],
        "parsers": {"js": {"type": "json_paged", "records_path": "records", "total_path": "total",
                           "strategy": "page_param", "page_param": "page", "start": 1,
                           "max_pages": 10, "page_size": 2,
                           "fields": {"id": {"from": "id"}, "type": {"from": "type"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1, "cookies": {"us_cookie": "1"}},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        ok = r["total"] == 4 and all(i["type"] == "8" for i in items)
        check(name, ok, f"total={r['total']} types={ {i['type'] for i in items} }")
    finally:
        cleanup(name)

    # 9. crawl --allow + --same-domain + --max 组合
    print("[9/15] crawl --allow + --same-domain + --max 组合")
    p = run_cli(["crawl", f"{base}/hub.html", "--allow", r"/p[145]\.html", "--same-domain",
                 "--max", "5"], timeout=300)
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
    ok = len(urls) == 4 and all("p2.html" not in u and "p3.html" not in u for u in urls) \
         and all(base in u for u in urls)
    check("t11i_crawl_combo", ok, f"urls={sorted(urls)}")

    # 10. 三级递归 max_depth=3 全抓
    print("[10/15] 三级递归 max_depth=3 全抓（5 页）")
    name = "t11j"
    cfg = {
        "name": name, "start_urls": [f"{base}/site/level1.html"],
        "queue": {"max_depth": 3, "max_requests": 20, "max_concurrency": 3},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/site", "parser": "page"}],
        "parsers": {"page": {"type": "html", "row_css": ".item",
                             "fields": {"t": {"css": "span.t"}},
                             "extract_links": {"allow": "/site/"}}},
        "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        ts = sorted(i["t"] for i in read_items(name))
        check(name, r["fetched"] == 4 and ts == ["L2", "L2", "L3"], f"fetched={r['fetched']} ts={ts}")
    finally:
        cleanup(name)

    # 11. fetch --json 管道纯度
    print("[11/15] fetch --json 管道纯度（stdout 仅一个 JSON）")
    p = run_cli(["fetch", f"{base}/p1.html", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = isinstance(data, dict) and data.get("status") == 200
    except Exception:
        ok = False
    check("t11k_json_pure", ok, f"stdout_len={len(p.stdout)}")

    # 12. v2 sitemap + limit
    print("[12/15] v2 sitemap + limit 组合")
    name = "t11l"
    cfg = {
        "name": name,
        "source": {"type": "http_html", "url": f"{base}/p1.html", "sitemap": f"{base}/sitemap.xml",
                   "row_css": ".item", "fields": {"title": {"css": "span.t"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}}},
        "pipeline": [], "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp), "--limit", "4"])
        rows = read_json(name)
        check(name, len(rows) == 4, f"rows={len(rows)}")
    finally:
        cleanup(name)

    # 13. monitor --times 3 无变化稳定
    print("[13/15] monitor --times 3 无变化稳定")
    name = "t11m"
    cfg = {
        "name": name, "start_urls": [f"{base}/static.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/static", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        p = run_cli(["monitor", "--task", str(d), "--every", "1", "--times", "3", "--key", "title"],
                    timeout=180)
        ok = "第 3 次抓取" in p.stdout and p.stdout.count("无变化") >= 2
        check(name, ok, f"stdout={p.stdout[-250:]}")
    finally:
        cleanup(name)

    # 14. 浏览器池 5 页会话保持
    print("[14/15] 浏览器池 5 页会话保持")
    from universal_scraper.modules.fetchers import BrowserFetcher
    from universal_scraper.protocols import Request
    f = BrowserFetcher({"type": "browser", "pool": True, "wait_selector": ".item"},
                       {}, {"session_dir": "/tmp/us_test_session11", "min_interval": 0.02})
    try:
        f.fetch(Request(url=f"{base}/setcookie.html"))
        ok = all("need-ok" in f.fetch(Request(url=f"{base}/needcookie.html")).text for _ in range(4))
        check("t11n_session5", ok, f"ok={ok}")
    finally:
        f.close()

    # 15. dry-run 全参数组合
    print("[15/15] run --dry-run 全参数组合不报错")
    name = "t11o"
    cfg = {
        "name": name, "vars": {"q": "默认"},
        "start_urls": [f"{base}/search?q={{{{q}}}}"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/search", "parser": "js"}],
        "parsers": {"js": {"type": "json", "records_path": "records", "fields": {"title": {"from": "title"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        p = run_cli(["run", "--task", str(d), "--dry-run", "--var", "q=测试", "--limit", "1"])
        ok = p.returncode == 0 and "dry_run" in p.stdout
        check("t11o_dryrun_combo", ok, f"rc={p.returncode} stdout={p.stdout[-120:]}")
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
