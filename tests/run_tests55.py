#!/usr/bin/env python3
"""第四十四轮回归（v3 详情补抓 detail）：
列表页缺发布日期 → detail 配置自动逐个抓详情页、合并字段、按日期过滤。
用法: python3 tests/run_tests55.py
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        p = self.path
        if p.startswith("/list"):
            body = """<html><body>
<li class="job"><a class="job-name" href="/job_detail/1.html">岗1</a><span class="job-salary">10K</span></li>
<li class="job"><a class="job-name" href="/job_detail/2.html">岗2</a><span class="job-salary">20K</span></li>
<li class="job"><a class="job-name" href="/job_detail/3.html">岗3</a><span class="job-salary">30K</span></li>
</body></html>""".encode()
        elif p.startswith("/job_detail/1.html"):
            body = "<html><body><div class='time'>2026-08-06 10:00 发布</div></body></html>".encode()
        elif p.startswith("/job_detail/2.html"):
            body = "<html><body><div class='time'>2026-08-05 10:00 发布</div></body></html>".encode()
        elif p.startswith("/job_detail/3.html"):
            body = "<html><body><div class='time'>2026-08-01 10:00 发布</div></body></html>".encode()
        else:
            body = b"not found"
        self.send_response(200 if body != b"not found" else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


def main():
    import shutil
    from universal_scraper.engine_v3 import run_task

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    base = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    name = "t55_detail"
    cfg = {
        "name": name, "start_urls": [f"{base}/list"],
        "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/list", "parser": "list"}],
        "parsers": {"list": {"type": "html", "row_css": ".job",
            "fields": {"title": {"css": ".job-name::text"}, "salary": {"css": ".job-salary::text"},
                       "link": {"css": "a[href*='/job_detail/']::attr(href)"}}}},
        "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
        "detail": {"enabled": True, "url_field": "link",
                   "url_transform": [{"prefix": base}],
                   "extract": [{"name": "publish_time", "type": "css_text", "selector": ".time::text"}],
                   "filters": [{"type": "parse_date", "field": "publish_time", "out": "ts"},
                               {"type": "filter", "field": "ts", "op": "between",
                                "min": 1785945600, "max": 1786032000}],  # 2026-08-06 当天(CST)
                   "concurrency": 2, "interval": 0.02},
        "storage": {"type": "jsonl", "name": name},
        "output": {"dir": "outputs", "base_name": name},
        "anti_bot": {"min_interval": 0.02, "max_retries": 1},
    }
    d = ROOT / "outputs" / ".test_tmp" / name
    (d / "modules").mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        r = run_task(d)
        fp = ROOT / "outputs" / f"{name}.json"
        rows = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else []
        print("  result:", r)
        print("  导出:", [{k: v for k, v in x.items() if not k.startswith("_")} for x in rows])
        check("详情字段合并", len(rows) == 1 and rows[0].get("publish_time", "").startswith("2026-08-06"),
              str(rows[:2])[:200])
        check("日期过滤生效", all("2026-08-06" in str(x.get("publish_time") or "") for x in rows),
              str(rows)[:200])
        check("detail_status 记录", all(str(x.get("detail_status")) == "200" for x in rows),
              str([x.get("detail_status") for x in rows]))
        # 清理
        for f in (ROOT/"outputs"/f"{name}.json", ROOT/"outputs"/f"{name}.csv",
                  ROOT/"outputs"/f"{name}.xlsx", ROOT/"outputs"/"items"/f"{name}.jsonl"):
            try:
                f.unlink()
            except Exception:
                pass
    finally:
        shutil.rmtree(d, ignore_errors=True)
        srv.shutdown()

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, dd in FAIL:
            print("  ❌", n, dd)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
