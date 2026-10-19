#!/usr/bin/env python3
"""第七轮高难度回归（15 个）：浏览器表单/下拉动作、source.query 静态参数、regex_all/attr、
v2 链接翻页、桥任务包、meta 编码、多表格、CLI 输出文件、配置校验/脚手架、多字段增量、
monitor diff-fields、限流+去重组合。
用法: python3 tests/run_tests7.py
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

    # 1. 浏览器：type + press Enter 提交搜索
    print("[1/15] 浏览器动作：type + press Enter 提交表单")
    name = "z1"
    cfg = {
        "name": name, "start_urls": [f"{base}/form.html"],
        "queue": {"max_depth": 2, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "browser", "pool": True,
                   "actions": [{"type": "type", "selector": "#q", "text": "数据中心"},
                               {"type": "press", "key": "Enter", "ms": 800}]},
        "rules": [{"match": "contains", "pattern": "/form", "parser": "js"}],
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
        check(name, r["total"] == 1 and items and items[0]["t"] == "form-数据中心",
              f"items={items}")
    finally:
        cleanup(name)

    # 2. 浏览器：select 下拉 + wait_for_selector
    print("[2/15] 浏览器动作：select + wait_for_selector")
    name = "z2"
    cfg = {
        "name": name, "start_urls": [f"{base}/select.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "browser", "pool": True,
                   "actions": [{"type": "select", "selector": "#s", "value": "2"},
                               {"type": "wait_for_selector", "selector": ".item", "timeout": 5000}]},
        "rules": [{"match": "contains", "pattern": "/select", "parser": "js"}],
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
        check(name, r["total"] == 1 and items and items[0]["t"] == "sel-2", f"items={items}")
    finally:
        cleanup(name)

    # 3. v3 source.query 静态参数
    print("[3/15] v3 source.query 静态参数 + 分页")
    name = "z3"
    cfg = {
        "name": name, "start_urls": [f"{base}/api/list2?page=1"],
        "queue": {"max_depth": 5, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http", "query": {"type": "7", "static": "yes"}},
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
        urls = [i["_url"] for i in items]
        check(name, r["total"] == 4 and all(i["type"] == "7" for i in items)
              and any("static=yes" in u for u in urls), f"urls={urls}")
    finally:
        cleanup(name)

    # 4. 解析器字段：regex_all + attr 多元素
    print("[4/15] 解析器 regex_all + css attr 多元素")
    name = "z4"
    cfg = {
        "name": name, "start_urls": [f"{base}/list.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/list", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item",
                           "fields": {"title": {"css": "span.t"},
                                      "urls": {"css": "a", "attr": "href"},
                                      "ids": {"regex": r"href='/(\w+)/", "group": 1, "regex_all": True}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r = run_task(d)
        items = read_items(name)
        ok = r["total"] == 2 and all("detail/1.html" in i["urls"] for i in items)
        check(name, ok, f"items={items}")
    finally:
        cleanup(name)

    # 5. v2 http_html 链接翻页
    print("[5/15] v2 http_html 链接翻页（next_selector）")
    name = "z5"
    cfg = {
        "name": name,
        "source": {"type": "http_html", "url": f"{base}/htmlp1.html",
                   "row_css": ".item", "fields": {"title": {"css": "span.t"}}},
        "pagination": {"strategy": "none", "max_pages": 3, "next_selector": "a.next"},
        "record": {"fields": {"title": {"from": "title"}}},
        "pipeline": [], "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": name, "formats": ["json"]},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1, "http_backend": "auto"},
    }
    fp = write_v2(name, cfg)
    try:
        run_cli(["run", "--config", str(fp)])
        rows = read_json(name)
        check(name, len(rows) == 4 and {r["title"] for r in rows} == {"hp-1", "hp-2", "pg-3", "pg-4"},
              f"rows={len(rows)} titles={sorted(r['title'] for r in rows)}")
    finally:
        cleanup(name)

    # 6. 桥任务包端到端
    print("[6/15] 桥任务包（demo_bridge）端到端")
    data = run_task(ROOT / "tasks" / "demo_bridge")
    items = read_items("demo_bridge")
    ok = data.get("total") == 2 and any("桥数据-数据中心-甲" in i["title"] for i in items)
    check("z6_bridge", ok, f"total={data.get('total')} items={[i['title'] for i in items]}")
    cleanup("demo_bridge")

    # 7. <meta charset=gbk> 无 header 编码探测
    print("[7/15] <meta charset> 编码探测（无 header）")
    name = "z7"
    cfg = {
        "name": name, "start_urls": [f"{base}/gbkmeta.html"],
        "queue": {"max_depth": 1, "max_requests": 5, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/gbkmeta", "parser": "js"}],
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
        check(name, r["total"] == 1 and items[0]["t"] == "元标签中文", f"items={items}")
    finally:
        cleanup(name)

    # 8. 多表格提取
    print("[8/15] fetch --table 多表格")
    p = run_cli(["fetch", f"{base}/multitable.html", "--table", "--json"])
    try:
        data = json.loads(p.stdout)
        ok = len(data.get("tables", [])) == 2 and data["tables"][0][0].get("A") == "a1"
    except Exception:
        ok = False
    check("z8_multitable", ok, f"tables={len(data.get('tables', [])) if 'data' in dir() else '?'}")

    # 9. fetch --out 保存文件
    print("[9/15] fetch --out 保存 Markdown 文件")
    out = ROOT / "outputs" / "fetch_test.md"
    try:
        os.remove(out)
    except FileNotFoundError:
        pass
    try:
        p = run_cli(["fetch", f"{base}/p1.html", "--out", str(out)])
        ok = out.exists() and "page 1" in out.read_text(encoding="utf-8")
        check("z9_fetch_out", ok, f"exists={out.exists()}")
    finally:
        try:
            os.remove(out)
        except FileNotFoundError:
            pass

    # 10. crawl --out 自定义文件名
    print("[10/15] crawl --out 自定义文件名")
    out = ROOT / "outputs" / "my_crawl_test"
    for f in (Path(str(out) + ".json"), Path(str(out) + ".csv"), Path(str(out) + ".xlsx")):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass
    try:
        p = run_cli(["crawl", f"{base}/hub.html", "--max", "10", "--out", str(out)])
        ok = Path(str(out) + ".json").exists()
        check("z10_crawl_out", ok, f"exists={ok}")
    finally:
        for f in (Path(str(out) + ".json"), Path(str(out) + ".csv"), Path(str(out) + ".xlsx")):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass

    # 11. v2 validate 拦截坏配置
    print("[11/15] v2 validate 拦截坏配置")
    bad = V2 / "bad.json"
    V2.mkdir(parents=True, exist_ok=True)
    bad.write_text(json.dumps({"name": "bad", "source": {"type": "http_unknown", "url": "http://a"},
                               "pagination": {"strategy": "none"}, "record": {}, "pipeline": [],
                               "detail": {"enabled": False},
                               "output": {"dir": "outputs", "base_name": "bad"},
                               "anti_bot": {}}), encoding="utf-8")
    p = run_cli(["validate", "--config", str(bad)])
    ok = p.returncode == 1 and "未知取数类型" in p.stderr
    check("z11_v2_validate", ok, f"rc={p.returncode} stderr={p.stderr[:80]}")
    try:
        os.remove(bad)
    except FileNotFoundError:
        pass

    # 12. scaffold task → validate 通过
    print("[12/15] scaffold task → validate 通过")
    outdir = V2 / "scaf"
    p1 = run_cli(["scaffold", "--type", "task", "--name", "scaf_demo", "--out", str(outdir)])
    d = outdir if outdir.suffix == "" else outdir.parent / outdir.stem
    ok1 = (d / "config.json").exists()
    ok2 = False
    if ok1:
        cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))
        cfg["start_urls"] = ["http://127.0.0.1:1/x"]
        (d / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        from universal_scraper.config import validate_task
        try:
            validate_task(cfg)
            ok2 = True
        except Exception:
            ok2 = False
    check("z12_scaffold", ok1 and ok2, f"created={ok1} valid={ok2}")
    import shutil
    shutil.rmtree(d, ignore_errors=True)

    # 13. incremental 多字段 key
    print("[13/15] incremental 多字段 key（id+title）")
    name = "z13"
    cfg = {
        "name": name, "start_urls": [f"{base}/p1.html", f"{base}/p1.html"],
        "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/p", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item",
                           "fields": {"title": {"css": "span.t"}, "date": {"css": "span.p"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "incremental": {"enabled": True, "key": ["title", "date"]},
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r1 = run_task(d)
        r2 = run_task(d)
        check(name, r1["total"] == 2 and r2["total"] == 0, f"r1={r1['total']} r2={r2['total']}")
    finally:
        cleanup(name)

    # 14. monitor --diff-fields 指定字段
    print("[14/15] monitor --diff-fields 指定字段变化")
    name = "z14"
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
        p = run_cli(["monitor", "--task", str(d), "--every", "1", "--times", "2",
                     "--key", "title", "--diff-fields", "title"], timeout=120)
        check(name, "新增 1" in p.stdout, f"stdout={p.stdout[-200:]}")
    finally:
        cleanup(name)

    # 15. 限流重试 + 增量去重组合
    print("[15/15] 限流重试 + 增量去重组合（不重复）")
    name = "z15"
    cfg = {
        "name": name, "start_urls": [f"{base}/ratelimit"],
        "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/ratelimit", "parser": "js"}],
        "parsers": {"js": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "span.t"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "incremental": {"enabled": True, "key": "title"},
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = make_task(name, cfg)
    try:
        r1 = run_task(d)
        r2 = run_task(d)
        rows = read_json(name)
        check(name, r1["total"] == 1 and r2["total"] == 0 and len(rows) == 1,
              f"r1={r1['total']} r2={r2['total']} rows={len(rows)}")
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
