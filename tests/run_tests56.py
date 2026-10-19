#!/usr/bin/env python3
"""第四十五轮回归（auto 关键字段缺失 → 自修复加 detail）：
任务要求"发布日期"，第1轮AI配置没有日期字段/没有detail → 判定关键字段缺失 →
自修复 → 第2轮AI配置带detail → 详情补抓+日期过滤 → 成功。
用法: python3 tests/run_tests56.py
"""
from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []

LIST_HTML = """<html><body>
<li class="job"><a class="job-name" href="/job_detail/1.html">岗1</a><span class="job-salary">10K</span></li>
<li class="job"><a class="job-name" href="/job_detail/2.html">岗2</a><span class="job-salary">20K</span></li>
<li class="job"><a class="job-name" href="/job_detail/3.html">岗3</a><span class="job-salary">30K</span></li>
</body></html>"""
DETAILS = {"/job_detail/1.html": "2026-08-06 10:00 发布",
           "/job_detail/2.html": "2026-08-06 09:00 发布",
           "/job_detail/3.html": "2026-08-05 09:00 发布"}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        p = self.path
        if p.startswith("/list"):
            body, code = LIST_HTML.encode(), 200
        elif p in DETAILS:
            body = f"<html><body><div class='time'>{DETAILS[p]}</div></body></html>".encode()
            code = 200
        else:
            body, code = b"not found", 404
        self.send_response(code)
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
    import universal_scraper.auto as auto
    orig_chat = auto._llm_chat
    call = {"n": 0}

    def fake_chat(messages, temperature=0.1):
        prompt = " ".join(str(m.get("content", "")) for m in messages)
        m = re.search(r"https?://127\.0\.0\.1:\d+/list", prompt)
        url = m.group(0)
        call["n"] += 1
        if call["n"] == 1:
            # 第1轮：列表配置，无日期字段、无 detail（AI 犯错）
            cfg = {"name": "x", "start_urls": [url],
                   "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 1},
                   "source": {"type": "http"},
                   "rules": [{"match": "contains", "pattern": "/list", "parser": "list"}],
                   "parsers": {"list": {"type": "html", "row_css": ".job",
                       "fields": {"title": {"css": ".job-name::text"},
                                  "salary": {"css": ".job-salary::text"},
                                  "link": {"css": "a[href*='/job_detail/']::attr(href)"}}}},
                   "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
                   "storage": {"type": "jsonl", "name": "x"},
                   "output": {"dir": "outputs", "base_name": "x"},
                   "anti_bot": {"min_interval": 0.02, "max_retries": 1}}
        else:
            # 第2轮：自修复后加 detail（列表+详情+日期过滤）
            cfg = {"name": "x", "start_urls": [url],
                   "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 1},
                   "source": {"type": "http"},
                   "rules": [{"match": "contains", "pattern": "/list", "parser": "list"}],
                   "parsers": {"list": {"type": "html", "row_css": ".job",
                       "fields": {"title": {"css": ".job-name::text"},
                                  "salary": {"css": ".job-salary::text"},
                                  "link": {"css": "a[href*='/job_detail/']::attr(href)"}}}},
                   "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
                   "detail": {"enabled": True, "url_field": "link",
                              "url_transform": [{"prefix": url.rsplit("/", 1)[0]}],
                              "extract": [{"name": "publish_time", "type": "css_text", "selector": ".time::text"}],
                              "filters": [{"type": "parse_date", "field": "publish_time", "out": "ts"},
                                          {"type": "filter", "field": "ts", "op": "between",
                                           "min": 1785945600, "max": 1786032000}],
                              "concurrency": 2, "interval": 0.02},
                   "storage": {"type": "jsonl", "name": "x"},
                   "output": {"dir": "outputs", "base_name": "x"},
                   "anti_bot": {"min_interval": 0.02, "max_retries": 1}}
        return json.dumps(cfg, ensure_ascii=False)

    auto._llm_chat = fake_chat
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        base = f"http://127.0.0.1:{srv.server_port}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        desc = f"抓取 {base}/list 上发布日期为2026年8月6日的岗位，需要发布时间字段"
        logs = []
        r = auto.auto_task(desc, rounds=2, round_timeout=60,
                           log_cb=lambda m: logs.append(str(m)))
        joined = "\n".join(logs)
        print("  关键日志:", [l for l in logs if "关键字段" in l or "详情" in l or "成功" in l][-6:])
        check("触发关键字段缺失自修复", "关键字段" in joined and "详情" in joined, joined[-500:])
        check("两轮调用LLM", call["n"] >= 2, f"calls={call['n']}")
        check("最终成功且有日期", r["result"].get("total", 0) == 2
              and all(str(it.get("publish_time") or "").startswith("2026-08-06") for it in r["sample"]),
              f"total={r['result'].get('total')} sample={r['sample'][:2]}")
        check("导出文件含日期", (ROOT / "outputs" / f"{r['name']}.json").exists())
        srv.shutdown()
    finally:
        auto._llm_chat = orig_chat

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, dd in FAIL:
            print("  ❌", n, dd)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
