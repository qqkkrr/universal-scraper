#!/usr/bin/env python3
"""第九轮高难度回归（15 个）：中间件 on_data/on_response、解析器异常重试恢复、
SIGTERM 优雅中断、monitor 消失检测、v2 --var/url_transform、大 JSON、池大小、
schedule --resume、fetch 组合（browser article / gzip / fullPage 截图）、crawl 404 容错、v2 --log-file。
用法: python3 tests/run_tests9.py
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

    # 1. 自定义 parser 前 2 次抛异常 → 重试恢复
    print("[1/15] 自定义 parser 异常 → 重试恢复（errors 归零）")
    name = "r1"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "flaky"}],
        "parsers": {"flaky": {}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 3},
    }
    mod = '''import re
from universal_scraper.protocols import BaseParser, ParseResult
_calls = {"n": 0}
class FlakyParser(BaseParser):
    name = "flaky"
    def parse(self, resp, ctx):
        _calls["n"] += 1
        if _calls["n"] <= 2:
            raise RuntimeError("parse boom")
        return ParseResult(items=[{"t": "recovered"}])
'''
    d = make_task(name, cfg, {"parser.py": mod})
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and r["errors"] == 0 and items[0]["t"] == "recovered",
              f"total={r['total']} errors={r['errors']} items={items}")
    finally:
        cleanup(name)

    # 2. 自定义 middleware on_data 丢弃
    print("[2/15] 自定义 middleware on_data 丢弃 item")
    name = "r2"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [], "middleware": [],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    mod = '''from universal_scraper.protocols import BaseMiddleware
class Middleware(BaseMiddleware):
    def on_data(self, item, ctx):
        return None if item.get("title", "").endswith("2") else item
'''
    d = make_task(name, cfg, {"middleware.py": mod})
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 1 and items and items[0]["title"] == "p1-title-1",
              f"total={r['total']} items={[i['title'] for i in items]}")
    finally:
        cleanup(name)

    # 3. 自定义 middleware on_response 改写
    print("[3/15] 自定义 middleware on_response 改写响应")
    name = "r3"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [], "middleware": [],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    mod = '''from universal_scraper.protocols import BaseMiddleware
class Middleware(BaseMiddleware):
    def on_response(self, resp, ctx):
        resp.text = resp.text.replace("p1-title", "REWRITTEN")
        resp.body = resp.text.encode("utf-8")
        return resp
'''
    d = make_task(name, cfg, {"middleware.py": mod})
    try:
        r = run_task(d)
        items = read_items(name)
        check(name, r["total"] == 2 and all(i["title"].startswith("REWRITTEN") for i in items),
              f"items={[i['title'] for i in items]}")
    finally:
        cleanup(name)

    # 4. SIGTERM 优雅中断
    print("[4/15] SIGTERM 优雅中断 → pending 落盘 + resume")
    name = "r4"
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
        r = run_task(d, resume=True)
        final_urls = [x["_url"] for x in read_json(name)]
        check(name, saved and len({u.split("/slow/")[1] for u in final_urls}) == 10,
              f"saved={saved} final={len(final_urls)}")
    finally:
        cleanup(name)

    # 5. monitor 消失检测
    print("[5/15] monitor 内容消失检测")
    name = "r5"
    cfg = {
        "name": name, "start_urls": [f"{base}/shrink.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/shrink", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        p = run_cli(["monitor", "--task", str(d), "--every", "1", "--times", "2", "--key", "title"],
                    timeout=120)
        check(name, "消失 1" in p.stdout, f"stdout={p.stdout[-200:]}")
    finally:
        cleanup(name)

    # 6. v2 run --var
    print("[6/15] v2 run --var 覆盖")
    name = "r6"
    cfg = {
        "name": name, "vars": {"q": "默认"},
        "source": {"type": "http_json", "url": f"{base}/search?q={{{{q}}}}"},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}}},
        "pipeline": [], "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp), "--var", "q=北京"])
        rows = read_json(name)
        check(name, len(rows) == 1 and rows[0]["title"] == "hit-北京", f"rows={rows}")
    finally:
        cleanup(name)

    # 7. v2 detail url_transform prefix
    print("[7/15] v2 detail url_transform（prefix 补全）")
    name = "r7"
    cfg = {
        "name": name,
        "source": {"type": "http_html", "url": f"{base}/list.html",
                   "row_css": ".item",
                   "fields": {"title": {"css": "span.t"}, "url": {"css": "a.u", "attr": "href"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}, "url": {"from": "url"}}},
        "pipeline": [],
        "detail": {"enabled": True, "url_field": "url", "concurrency": 2,
                   "url_transform": [{"replace": ["/detail/", "/abs/"]}],
                   "extract": [{"name": "body", "type": "css_text", "selector": "h1"}]},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        # /abs/1.html 不存在 → detail_status 非 200，验证 url_transform 生效
        run_cli(["run", "--config", str(fp)])
        rows = read_json(name)
        ok = len(rows) == 2 and all("/abs/1.html" in r.get("url_final", "") for r in rows)
        check(name, ok, f"rows={[r.get('url_final') for r in rows]}")
    finally:
        cleanup(name)

    # 8. 大 JSON 2000 条
    print("[8/15] 大 JSON 2000 条解析")
    name = "r8"
    cfg = {
        "name": name, "start_urls": [f"{base}/api/big"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/api/big", "parser": "js"}],
        "parsers": {"js": {"type": "json", "records_path": "records",
                           "fields": {"id": {"from": "id"}, "title": {"from": "title"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.0, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        t0 = time.time()
        r = run_task(d)
        dt = time.time() - t0
        check(name, r["total"] == 2000 and dt < 15, f"total={r['total']} dt={dt:.1f}s")
    finally:
        cleanup(name)

    # 9. 浏览器池 US_POOL_SIZE=1 并发串行
    print("[9/15] 浏览器池 US_POOL_SIZE=1（并发请求串行仍成功）")
    from universal_scraper.modules.fetchers import BrowserFetcher
    from universal_scraper.protocols import Request
    os.environ["US_POOL_SIZE"] = "1"
    f = BrowserFetcher({"type": "browser", "pool": True, "wait_selector": ".item"},
                       {}, {"session_dir": "/tmp/us_test_session9", "min_interval": 0.02})
    results = {}
    try:
        def work(i):
            results[i] = f.fetch(Request(url=f"{base}/p{i}.html"))
        ts = [threading.Thread(target=work, args=(i,)) for i in (1, 2, 3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        ok = all(len(results[i].text) > 0 for i in (1, 2, 3))
        check(name, ok, f"lens={[len(results[i].text) for i in (1,2,3)]}")
    finally:
        os.environ.pop("US_POOL_SIZE", None)
        f.close()

    # 10. schedule --resume
    print("[10/15] schedule --resume 二次运行跳过")
    name = "r10"
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
        p = run_cli(["schedule", "--task", str(d), "--every", "1", "--times", "2", "--resume"], timeout=120)
        # 第二次 resume 会跳过已抓 URL → jsonl 只追加 0 条，导出仍 2 条
        check(name, "第 2 次运行" in p.stdout and len(read_items(name)) == 2,
              f"stdout={p.stdout[-120:]} items={len(read_items(name))}")
    finally:
        cleanup(name)

    # 11. fetch --browser --article
    print("[11/15] fetch --browser --article（JS 页正文）")
    p = run_cli(["fetch", f"{base}/article.html", "--browser", "--article"], timeout=300)
    ok = "标题一" in p.stdout and "正文第一段" in p.stdout
    check("r11_fetch_browser_article", ok, f"stdout={p.stdout[:120]}")

    # 12. fetch HTTP gzip
    print("[12/15] fetch HTTP gzip 页")
    p = run_cli(["fetch", f"{base}/gzip"])
    check("r12_fetch_gzip", "gzip-ok" in p.stdout, f"stdout={p.stdout[:100]}")

    # 13. 截图 fullPage
    print("[13/15] 截图 fullPage（动作链）")
    name = "r13"
    shot = ROOT / "outputs" / "r13_full.png"
    try:
        os.remove(shot)
    except FileNotFoundError:
        pass
    cfg = {
        "name": name, "start_urls": [f"{base}/article.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "browser", "pool": True,
                   "actions": [{"type": "screenshot", "path": str(shot), "fullPage": True}]},
        "rules": [{"match": "contains", "pattern": "/article", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"t": {"css": "span.t"}}}},
        "pipelines": [], "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        ok = shot.exists() and shot.stat().st_size > 5000
        check(name, r["errors"] == 0 and ok, f"errors={r['errors']} shot_size={shot.stat().st_size if shot.exists() else 0}")
    finally:
        cleanup(name)
        try:
            os.remove(shot)
        except FileNotFoundError:
            pass

    # 14. crawl 404 链接容错
    print("[14/15] crawl 404 链接不报错")
    p = run_cli(["crawl", f"{base}/hub.html", "--max", "20"], timeout=300)
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
    ok = "错误 0" in p.stdout and all("out.html" not in u for u in urls)
    check("r14_crawl_404", ok, f"urls={urls}")

    # 15. v2 run --log-file
    print("[15/15] v2 run --log-file 落盘")
    name = "r15"
    cfg = {
        "name": name,
        "source": {"type": "http_html", "url": f"{base}/p1.html",
                   "row_css": ".item", "fields": {"title": {"css": "span.t"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}}},
        "pipeline": [], "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    logf = "/tmp/us_log9.log"
    try:
        os.remove(logf)
    except FileNotFoundError:
        pass
    try:
        run_cli(["run", "--config", str(fp), "--log-file", logf])
        ok = Path(logf).exists() and "原始记录" in Path(logf).read_text(encoding="utf-8")
        check("r15_v2_logfile", ok, f"exists={Path(logf).exists()}")
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
