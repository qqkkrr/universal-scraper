#!/usr/bin/env python3
"""最终严苛测试（15 项）：7 个真实站点任务 + 8 个高强度/极端场景。
输出结果写入 outputs/final_test_results.txt。
用法: python3 tests/run_final.py
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
REPORT = []


def log(msg):
    print(msg, flush=True)
    REPORT.append(msg)


def make_task(name: str, cfg: dict, modules: dict = None) -> Path:
    d = TMP / name
    (d / "modules").mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    for fn, content in (modules or {}).items():
        (d / "modules" / fn).write_text(content, encoding="utf-8")
    return d


def write_v2(name: str, cfg: dict) -> Path:
    d = ROOT / "outputs" / ".test_tmp_v2"
    d.mkdir(parents=True, exist_ok=True)
    fp = d / f"{name}.json"
    fp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


def cleanup(name: str):
    import glob, shutil
    for pat in (f"outputs/{name}*", f"outputs/items/{name}*",
                f"outputs/.state_{name}.json", f"outputs/.pending_{name}.json",
                f"outputs/.run_{name}.log", f"outputs/.seen_{name}.txt",
                f"outputs/.snapshot_{name}.json"):
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


def read_items(name: str) -> list:
    fp = ROOT / "outputs" / "items" / f"{name}.jsonl"
    if not fp.exists():
        return []
    return [json.loads(x) for x in fp.read_text(encoding="utf-8").splitlines() if x.strip()]


def read_json(name: str) -> list:
    fp = ROOT / "outputs" / f"{name}.json"
    return json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else []


def run_cli(args, timeout=180):
    return subprocess.run([PY, "-m", "universal_scraper.cli"] + args,
                          capture_output=True, text=True, cwd=ROOT, timeout=timeout)


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        log(f"  ✅ {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        log(f"  ❌ {name}  {detail}")


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


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    log(f"=== 万能爬虫工具 最终严苛测试 ===")
    log(f"mock 服务器: {base}\n")

    # ---- 真实站点 7 项 ----
    # R1 books 翻页
    log("[1/15] 真实：books.toscrape.com 翻页（HTTP）")
    name = "fin_books"
    cfg = {
        "name": name, "start_urls": ["https://books.toscrape.com/"],
        "queue": {"max_depth": 3, "max_requests": 12, "max_concurrency": 2},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": "article.product_pod",
                           "fields": {"title": {"css": "h3 a", "attr": "title"},
                                      "price": {"css": "p.price_color"}},
                           "extract_links": {"allow": r"/catalogue/page-\d+\.html", "same_domain": True}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.3, "max_retries": 2},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["errors"] == 0 and len(items) >= 20 and "A Light in the Attic" in items[0]["title"],
              f"items={len(items)} errors={r['errors']}")
    finally:
        cleanup(name)

    # R2 quotes 翻页
    log("[2/15] 真实：quotes.toscrape.com 翻页（HTTP）")
    name = "fin_quotes"
    cfg = {
        "name": name, "start_urls": ["https://quotes.toscrape.com/"],
        "queue": {"max_depth": 3, "max_requests": 12, "max_concurrency": 2},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".quote",
                           "fields": {"title": {"css": "span.text", "limit": 300},
                                      "author": {"css": "small.author"}},
                           "extract_links": {"allow": r"/page/\d+", "same_domain": True}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.3, "max_retries": 2},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["errors"] == 0 and len(items) >= 20,
              f"items={len(items)} errors={r['errors']}")
    finally:
        cleanup(name)

    # R3 jsonplaceholder
    log("[3/15] 真实：jsonplaceholder.typicode.com/posts（JSON）")
    name = "fin_posts"
    d = make_task(name, scfg(name, ["https://jsonplaceholder.typicode.com/posts"],
                             extra={"parsers": {"js": {"type": "json",
                                                       "fields": {"title": {"from": "title"}}}},
                                    "pipelines": []}))
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["errors"] == 0 and len(items) == 100, f"items={len(items)}")
    finally:
        cleanup(name)

    # R4 GitHub search 分页
    log("[4/15] 真实：GitHub 搜索 API 分页（JSON）")
    name = "fin_gh"
    cfg = {
        "name": name, "start_urls": ["https://api.github.com/search/repositories?q=scraper&per_page=5&page=1"],
        "queue": {"max_depth": 3, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "js"}],
        "parsers": {"js": {"type": "json_paged", "records_path": "items", "total_path": "total_count",
                           "strategy": "page_param", "page_param": "page", "start": 1,
                           "max_pages": 2, "page_size": 5,
                           "fields": {"title": {"from": "full_name"}, "stars": {"from": "stargazers_count"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.3, "max_retries": 2,
                     "headers": {"Accept": "application/vnd.github+json"}},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["errors"] == 0 and len(items) == 10, f"items={len(items)} errors={r['errors']}")
    finally:
        cleanup(name)

    # R5 HN Algolia 分页
    log("[5/15] 真实：Hacker News Algolia 搜索（JSON 分页）")
    name = "fin_hn"
    cfg = {
        "name": name,
        "start_urls": ["https://hn.algolia.com/api/v1/search?query=python&hitsPerPage=10&page=0"],
        "queue": {"max_depth": 3, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "js"}],
        "parsers": {"js": {"type": "json_paged", "records_path": "hits", "total_path": "nbHits",
                           "strategy": "page_param", "page_param": "page", "start": 0,
                           "max_pages": 2, "page_size": 10,
                           "fields": {"title": {"from": "title"}, "points": {"from": "points"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.3, "max_retries": 2},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["errors"] == 0 and len(items) >= 10, f"items={len(items)}")
    finally:
        cleanup(name)

    # R6 gov.cn 浏览器渲染
    log("[6/15] 真实：国务院政策页（浏览器 JS 渲染）")
    name = "fin_gov"
    cfg = {
        "name": name, "start_urls": ["https://www.gov.cn/zhengce/"],
        "queue": {"max_depth": 1, "max_requests": 6, "max_concurrency": 1},
        "source": {"type": "browser", "pool": True, "stealth": True, "wait_selector": "li"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": "li.poster-item",
                           "fields": {"title": {"css": ".picTitle a"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.5, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["errors"] == 0 and len(items) >= 1 and "图表" in items[0]["title"],
              f"items={len(items)} errors={r['errors']}")
    finally:
        cleanup(name)

    # R7 真实下载（books 封面图，v2 download）
    log("[7/15] 真实：books.toscrape.com 封面图下载（v2 download）")
    name = "fin_dl"
    cfg = {
        "name": name,
        "source": {"type": "http_html", "url": "https://books.toscrape.com/",
                   "row_css": "article.product_pod",
                   "fields": {"title": {"css": "h3 a", "attr": "title"},
                              "img": {"css": "img", "attr": "src"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}, "img": {"from": "img"}}},
        "pipeline": [], "detail": {"enabled": False},
        "download": {"enabled": True, "url_field": "img", "dir": "files", "concurrency": 3},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.2, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp), "--limit", "3"])
        files = list((ROOT / "outputs" / "files").glob("*")) if (ROOT / "outputs" / "files").exists() else []
        ok = len(files) >= 3 and all(f.stat().st_size > 1000 for f in files)
        check(name, ok, f"files={len(files)}")
    finally:
        cleanup(name)

    # ---- 高强度/极端 8 项（mock）----
    # M8 16 worker 高并发去重
    log("[8/15] 压力：16 worker × 20 重复种子去重")
    name = "fin_stress"
    d = make_task(name, scfg(name, [f"{base}/p{i}.html" for i in range(1, 6)] * 4, concurrency=16))
    try:
        r = run_task(d)
        urls = {i["_url"] for i in read_items(name)}
        check(name, r["fetched"] == 5 and r["total"] == 10 and len(urls) == 5,
              f"fetched={r['fetched']} items={r['total']}")
    finally:
        cleanup(name)

    # M9 SIGTERM 中断恢复
    log("[9/15] 极端：SIGTERM 优雅中断 → resume 恢复")
    name = "fin_sig"
    cfg = {
        "name": name, "start_urls": [f"{base}/slow/{i}" for i in range(10)],
        "queue": {"max_depth": 1, "max_requests": 50, "max_concurrency": 1},
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
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
        pending = ROOT / "outputs" / f".pending_{name}.json"
        saved = pending.exists() and json.loads(pending.read_text(encoding="utf-8")) != []
        run_task(d, resume=True)
        final_urls = [x["_url"] for x in read_json(name)]
        check(name, saved and len({u.split("/slow/")[1] for u in final_urls}) == 10,
              f"saved={saved} final={len(final_urls)}")
    finally:
        cleanup(name)

    # M10 429 HTTP-date
    log("[10/15] 协议：429 HTTP-date Retry-After 解析")
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
    check("fin_429_date", ra is not None and 0 < ra <= 5 and "after-date" in r2.text,
          f"retry_after={ra}")

    # M11 插件五件套
    log("[11/15] 模块化：插件五件套同包")
    name = "fin_plug"
    cfg = {
        "name": name, "start_urls": ["http://x/fake"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "custom"},
        "rules": [{"match": "contains", "pattern": "/fake", "parser": "p"}],
        "parsers": {"p": {}}, "pipelines": [],
        "storage": {"type": "custom", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0},
    }
    modules = {
        "fetcher.py": "from universal_scraper.protocols import BaseFetcher, Response\nclass Fetcher(BaseFetcher):\n    name='custom'\n    def fetch(self, req):\n        return Response(request=req, status=200, text=\"<span class='t'>kit</span>\")\n",
        "parser.py": "from universal_scraper.protocols import BaseParser, ParseResult\nclass Parser(BaseParser):\n    name='p'\n    def parse(self, resp, ctx):\n        return ParseResult(items=[{'v': 'raw'}])\n",
        "pipeline.py": "from universal_scraper.protocols import BasePipeline\nclass Pipeline(BasePipeline):\n    def process(self, item):\n        item['v'] = item['v'].upper() + '-OK'\n        return item\n",
        "storage.py": "import json, os\nfrom universal_scraper.protocols import BaseStorage\nclass Storage(BaseStorage):\n    name='custom'\n    def __init__(self, config, task_vars):\n        self.dir=config.get('dir','outputs'); self.name=config.get('name','items')\n    def open(self, name):\n        os.makedirs(self.dir, exist_ok=True); self.f=open(os.path.join(self.dir, name+'.custom'),'w',encoding='utf-8')\n    def write(self, item):\n        self.f.write(json.dumps(item, ensure_ascii=False)+'\\n')\n    def close(self):\n        self.f.close()\n",
        "middleware.py": "from universal_scraper.protocols import BaseMiddleware\nclass Middleware(BaseMiddleware):\n    def on_data(self, item, ctx):\n        return None if item.get('v') == 'RAW' else item\n",
    }
    d = make_task(name, cfg, modules)
    try:
        r = run_task(d)
        fp = ROOT / "outputs/items" / f"{name}.custom"
        content = fp.read_text(encoding="utf-8") if fp.exists() else ""
        check(name, r["total"] == 1 and "RAW-OK" in content, f"content={content[:40]}")
    finally:
        cleanup(name)
        try:
            os.remove(ROOT / "outputs/items" / f"{name}.custom")
        except FileNotFoundError:
            pass

    # M12 multi 三后端 + incremental
    log("[12/15] 存储：multi 三后端 + incremental")
    name = "fin_multi"
    cfg = scfg(name, [f"{base}/p1.html"],
               extra={"incremental": {"enabled": True, "key": "title"},
                      "storage": {"type": "multi", "backends": [
                          {"type": "jsonl", "name": name}, {"type": "csv", "name": name},
                          {"type": "sqlite", "name": name}]}})
    d = make_task(name, cfg)
    try:
        r1 = run_task(d)
        r2 = run_task(d)
        import sqlite3
        conn = sqlite3.connect(str(ROOT / "outputs/items" / f"{name}.db"))
        n = conn.execute("select count(*) from items").fetchone()[0]
        conn.close()
        csv = ROOT / "outputs/items" / f"{name}.csv"
        nc = len(csv.read_text(encoding="utf-8-sig").splitlines()) if csv.exists() else 0
        check(name, r1["total"] == 2 and r2["total"] == 0 and n == 2 and nc == 3,
              f"r2={r2['total']} db={n} csv={nc}")
    finally:
        cleanup(name)

    # M13 robots + follow + same_domain
    log("[13/15] 合规：robots + follow + same_domain 组合")
    name = "fin_robot"
    cfg = {
        "name": name, "start_urls": [f"{base}/hub2.html"],
        "queue": {"max_depth": 2, "max_requests": 10, "max_concurrency": 2},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/hub2", "parser": "list", "follow": True},
                  {"match": "contains", "pattern": "/p", "parser": "js", "follow": True}],
        "parsers": {"list": {"type": "html", "fields": {"h": {"css": "h1"}},
                             "extract_links": {"allow": r"/p\d\.html", "same_domain": True}},
                    "js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1, "respect_robots": True},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        urls = [i["_url"] for i in read_items(name)]
        check(name, r["fetched"] == 3 and not any("p2.html" in u for u in urls),
              f"fetched={r['fetched']} urls={sorted(set(urls))}")
    finally:
        cleanup(name)

    # M14 编码：GBK meta + gzip
    log("[14/15] 编码：GBK <meta> 无 header + gzip")
    name = "fin_enc"
    cfg = scfg(name, [f"{base}/gbkmeta.html"])
    cfg["parsers"]["js"]["fields"] = {"title": {"css": "span.t"}}
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        ok1 = r["total"] == 1 and items[0]["title"] == "元标签中文"
        name2 = "fin_gz"
        cfg2 = scfg(name2, [f"{base}/gzip"])
        d2 = make_task(name2, cfg2)
        r2 = run_task(d2)
        ok2 = r2["total"] == 1 and read_items(name2)[0]["title"] == "gzip-ok"
        cleanup(name2)
        check(name, ok1 and ok2, f"gbk={ok1} gzip={ok2}")
    finally:
        cleanup(name)

    # M15 CSV 跨运行追加 + monitor 三向 diff
    log("[15/15] 数据：CSV 跨运行追加 + monitor 三向 diff")
    name = "fin_csv"
    cfg = scfg(name, [f"{base}/p1.html"],
               extra={"incremental": {"enabled": True, "key": "title"},
                      "storage": {"type": "csv", "name": name}})
    d = make_task(name, cfg)
    ok_csv = False
    try:
        run_task(d)
        r2 = run_task(d)
        csv = ROOT / "outputs/items" / f"{name}.csv"
        n = len(csv.read_text(encoding="utf-8-sig").splitlines()) if csv.exists() else 0
        ok_csv = r2["total"] == 0 and n == 3
    finally:
        cleanup(name)
    # monitor 三向：新增（static 首次）、消失（shrink）、变更（change）
    ok_mon = True
    for nm, url, expect in (("fin_mn_add", "/static.html", "新增 2"),
                            ("fin_mn_del", "/shrink.html", "消失 1"),
                            ("fin_mn_chg", "/change.html", "变更 1")):
        cfg = scfg(nm, [f"{base}{url}"])
        if nm == "fin_mn_chg":
            cfg["parsers"]["js"]["fields"] = {"title": {"css": "span.t"}, "val": {"css": "span.p"}}
        d = make_task(nm, cfg)
        try:
            p = run_cli(["monitor", "--task", str(d), "--every", "1", "--times", "2",
                         "--key", "title"] + (["--diff-fields", "val"] if nm == "fin_mn_chg" else []),
                        timeout=120)
            if expect not in p.stdout:
                ok_mon = False
        finally:
            cleanup(nm)
    check("fin_csv_monitor", ok_csv and ok_mon, f"csv_append={ok_csv} monitor_3way={ok_mon}")

    log(f"\n===== 最终严苛测试结果: {len(PASS)}/15 通过 =====")
    if FAIL:
        log("失败: " + ", ".join(FAIL))
        srv.shutdown()
        return 1
    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
