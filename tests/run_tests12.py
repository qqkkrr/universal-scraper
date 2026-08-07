#!/usr/bin/env python3
"""第十二轮高难度回归（15 个）：稳定性组合场景（基于已加固能力，断言程序化避免手数）。
用法: python3 tests/run_tests12.py
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

    # 1. fetch --article + --json 组合
    print("[1/15] fetch --article --json")
    p = run_cli(["fetch", f"{base}/article.html", "--article", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = data.get("status") == 200 and "正文第一段" in data.get("article", "")
    except Exception:
        ok = False
    check("u1_article_json", ok, f"stdout={p.stdout[:120]}")

    # 2. fetch --proxy 参数（localhost 直连仍成功）
    print("[2/15] fetch --proxy 参数不崩")
    p = run_cli(["fetch", f"{base}/p1.html", "--proxy", "http://127.0.0.1:1"])
    # 死代理必须失败并报错（不能静默直连成功）；能干净报错即通过
    ok = p.returncode != 0 and ("❌" in p.stderr or "失败" in p.stderr or "错误" in p.stderr)
    check("u2_proxy_arg", ok, f"rc={p.returncode} stderr={p.stderr[:60]}")

    # 3. source.headers + cookies + query 三合一（/cookies 校验 cookie）
    print("[3/15] source.headers + cookies + query 三合一")
    name = "u3"
    cfg = {
        "name": name, "start_urls": [f"{base}/cookies"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http", "headers": {"X-Extra": "1"}, "query": {"t": "3"}},
        "rules": [{"match": "contains", "pattern": "/cookies", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"t": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1, "cookies": {"us_cookie": "1"}},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        ok = r["total"] == 1 and items and items[0]["t"] == "cookie-ok" and "t=3" in items[0]["_url"]
        check(name, ok, f"total={r['total']} url={items[0]['_url'] if items else None}")
    finally:
        cleanup(name)

    # 4. CSV 跨运行追加（新增能力验证）
    print("[4/15] CSV 跨运行追加不丢数据")
    name = "u4"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "incremental": {"enabled": True, "key": "title"},
        "storage": {"type": "csv", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        run_task(d)
        r2 = run_task(d)  # 第二次 0 条
        fp = ROOT / "outputs" / "items" / f"{name}.csv"
        lines = fp.read_text(encoding="utf-8-sig").splitlines() if fp.exists() else []
        ok = r2["total"] == 0 and len(lines) == 3 and lines[0].startswith("title")
        check(name, ok, f"r2={r2['total']} csv_lines={len(lines)}")
    finally:
        cleanup(name)

    # 5. LogMiddleware 不崩
    print("[5/15] LogMiddleware 中间件不崩")
    name = "u5"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "middleware": [{"on": "request", "action": "log"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        check(name, r["total"] == 2 and r["errors"] == 0, f"total={r['total']}")
    finally:
        cleanup(name)

    # 6. 自定义 fetcher 无种子 0 条不崩（队列模式容错）
    print("[6/15] 自定义 fetcher 无种子 0 条不崩")
    name = "u6"
    cfg = {
        "name": name, "start_urls": [],
        "source": {"type": "custom"},
        "rules": [], "parsers": {},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0},
    }
    mod = '''from universal_scraper.protocols import BaseFetcher, Response
class Fetcher(BaseFetcher):
    name = "custom"
    def fetch(self, req):
        return Response(request=req, status=200, text="<html></html>")
'''
    d = make_task(name, cfg, {"fetcher.py": mod})
    try:
        r = run_task(d)
        check(name, r["total"] == 0 and r["errors"] == 0, f"total={r['total']} errors={r['errors']}")
    finally:
        cleanup(name)

    # 7. crawl --deny 组合
    print("[7/15] crawl --deny 排除")
    p = run_cli(["crawl", f"{base}/hub.html", "--deny", r"/p[23]\.html", "--max", "10"], timeout=300)
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
    ok = len(urls) == 4 and not any("p2.html" in u or "p3.html" in u for u in urls)
    check("u7_crawl_deny", ok, f"urls={sorted(urls)}")

    # 8. fetch --wait 等待选择器（浏览器）
    print("[8/15] fetch --wait 等待选择器")
    p = run_cli(["fetch", f"{base}/spa.html", "--browser", "--wait", ".item",
                 "--selector", ".item"], timeout=300)
    ok = "spa-item-1" in p.stdout and "spa-item-3" in p.stdout
    check("u8_fetch_wait", ok, f"stdout={p.stdout[:100]}")

    # 9. schedule 定时 3 次稳定
    print("[9/15] schedule 定时 3 次稳定")
    name = "u9"
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
        p = run_cli(["schedule", "--task", str(d), "--every", "1", "--times", "3"], timeout=180)
        ok = "第 3 次运行" in p.stdout
        check(name, ok, f"stdout={p.stdout[-150:]}")
    finally:
        cleanup(name)

    # 10. monitor 变更检测（同 key 不同值）
    print("[10/15] monitor 变更检测（val a→b）")
    name = "u10"
    cfg = {
        "name": name, "start_urls": [f"{base}/change.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/change", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item",
                           "fields": {"title": {"css": "span.t"}, "val": {"css": "span.p"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        p = run_cli(["monitor", "--task", str(d), "--every", "1", "--times", "2",
                     "--key", "title", "--diff-fields", "val"], timeout=120)
        ok = "变更 1" in p.stdout
        check(name, ok, f"stdout={p.stdout[-250:]}")
    finally:
        cleanup(name)

    # 11. 中文 base_name 导出
    print("[11/15] 中文 base_name 导出")
    name = "u11"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": "数据_结果"},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        run_task(d)
        fp = ROOT / "outputs" / "数据_结果.json"
        rows = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else []
        ok = len(rows) == 2
        check(name, ok, f"rows={len(rows)}")
    finally:
        cleanup(name)
        for f in glob.glob(str(ROOT / "outputs" / "数据_结果*")):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
        for f in glob.glob(str(ROOT / "outputs/items" / f"{name}*")):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass

    # 12. 16 worker + 5 URL + CSV 写压力
    print("[12/15] 16 worker + CSV 写压力")
    name = "u12"
    cfg = {
        "name": name, "start_urls": [f"{base}/p{i}.html" for i in range(1, 6)],
        "queue": {"max_depth": 1, "max_requests": 20, "max_concurrency": 16},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item",
                           "fields": {"title": {"css": "span.t"}, "date": {"css": "span.p"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "csv", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        fp = ROOT / "outputs" / "items" / f"{name}.csv"
        lines = fp.read_text(encoding="utf-8-sig").splitlines() if fp.exists() else []
        ok = r["total"] == 10 and len(lines) == 11
        check(name, ok, f"total={r['total']} csv_lines={len(lines)}")
    finally:
        cleanup(name)

    # 13. 深度 + 并发 + 去重组合
    print("[13/15] 深度+并发+去重组合（cycle 页面）")
    name = "u13"
    cfg = {
        "name": name, "start_urls": [f"{base}/cycle1.html"],
        "queue": {"max_depth": 3, "max_requests": 30, "max_concurrency": 6},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/cycle", "parser": "page"}],
        "parsers": {"page": {"type": "html", "row_css": ".item", "fields": {"t": {"css": "span.t"}},
                             "extract_links": {"allow": "/cycle", "same_domain": True}}},
        "pipelines": [{"type": "filter", "field": "t", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        ts = sorted(i["t"] for i in read_items(name))
        ok = r["fetched"] == 2 and ts == ["c1", "c2"]
        check(name, ok, f"fetched={r['fetched']} ts={ts}")
    finally:
        cleanup(name)

    # 14. v2 http_json + detail（JSON 记录带 url → detail 展开）
    print("[14/15] v2 http_json + detail 展开")
    name = "u14"
    cfg = {
        "name": name,
        "source": {"type": "http_json", "url": f"{base}/search?q=数据中心"},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}, "url": {"constant": f"{base}/detail/1.html"}}},
        "pipeline": [],
        "detail": {"enabled": True, "url_field": "url", "concurrency": 1,
                   "extract": [{"name": "body", "type": "css_text", "selector": "h1"}]},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp)])
        rows = read_json(name)
        ok = len(rows) == 1 and rows[0]["body"] == "详情内容-1"
        check(name, ok, f"rows={rows}")
    finally:
        cleanup(name)

    # 15. dry-run 全参数组合（v2 config）
    print("[15/15] v2 run --dry-run 组合")
    name = "u15"
    cfg = {
        "name": name,
        "source": {"type": "http_json", "url": f"{base}/search?q=x"},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}}},
        "pipeline": [], "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        p = run_cli(["run", "--config", str(fp), "--dry-run", "--var", "q=测试", "--limit", "1"])
        ok = p.returncode == 0 and "dry-run" in p.stderr
        check(name, ok, f"rc={p.returncode} stderr={p.stderr[-150:]}")
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
