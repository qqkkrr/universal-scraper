#!/usr/bin/env python3
"""第四十三轮回归（auto 数据污染修复）：
auto 统一 storage/output 命名、每轮清空 items、sample 读文件末尾最新数据——
同名任务重复跑时，复核/抽样不再被旧记录（旧字段/空壳）污染。
用法: python3 tests/run_tests54.py
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

HTML = """<html><body>
<li class="job-card-box"><a class="job-name" href="/job_detail/1.html">产品经理1</a><span class="job-salary">10-15K</span><ul class="tag-list"><li>经验不限</li><li>本科</li></ul><span class="company-location"> 北京·朝阳区 </span><a class="boss-info"><span class="boss-name">公司1</span></a></li>
<li class="job-card-box"><a class="job-name" href="/job_detail/2.html">产品经理2</a><span class="job-salary">20-25K</span><ul class="tag-list"><li>经验1-3年</li><li>本科</li></ul><span class="company-location"> 上海·浦东 </span><a class="boss-info"><span class="boss-name">公司2</span></a></li>
<li class="job-card-box"><a class="job-name" href="/job_detail/3.html">产品经理3</a><span class="job-salary">30-40K</span><ul class="tag-list"><li>经验3-5年</li><li>硕士</li></ul><span class="company-location"> 深圳·南山 </span><a class="boss-info"><span class="boss-name">公司3</span></a></li>
</body></html>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = HTML.encode()
        self.send_response(200)
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
    import universal_scraper.auto as auto
    orig_chat = auto._llm_chat

    def fake_chat(messages, temperature=0.1):
        prompt = " ".join(str(m.get("content", "")) for m in messages)
        m = re.search(r"https?://127\.0\.0\.1:\d+/list", prompt)
        url = m.group(0)
        cfg = {"name": "whatever_ai_name", "start_urls": [url],
               "queue": {"max_depth": 1, "max_requests": 10, "max_concurrency": 1},
               "source": {"type": "http"},
               "rules": [{"match": "contains", "pattern": "/list", "parser": "list"}],
               "parsers": {"list": {"type": "html", "row_css": ".wrong-row",
                   "fields": {"title": {"css": ".nope::text"}, "area": {"css": ".nope2::text"},
                              "company": {"css": ".nope3::text"},
                              "link": {"css": "a[href*='/job_detail/']::attr(href)"}}}},
               "pipelines": [{"type": "filter", "field": "title", "op": "non_empty"}],
               "storage": {"type": "jsonl", "name": "whatever"},
               "output": {"dir": "outputs", "base_name": "whatever"},
               "anti_bot": {"min_interval": 0.02, "max_retries": 1}}
        return json.dumps(cfg, ensure_ascii=False)

    auto._llm_chat = fake_chat
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        base = f"http://127.0.0.1:{srv.server_port}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        desc = f"抓取 {base}/list 的标题、公司、地区和链接"

        def run_once():
            r = auto.auto_task(desc, rounds=1, round_timeout=60, log_cb=lambda m: None)
            items = ROOT / "outputs" / "items" / f"{r['name']}.jsonl"
            n = len(items.read_text(encoding="utf-8").splitlines()) if items.exists() else 0
            return r, items, n

        r1, items1, n1 = run_once()
        fields1 = sorted(set(k for it in r1["sample"] for k in it if not k.startswith("_")))
        check("第1次字段猜测补全", r1["result"].get("total") == 3 and fields1 == ["area", "company", "link", "title"],
              f"total={r1['result'].get('total')} fields={fields1}")
        check("第1次 items 只有当轮数据", n1 == 3, f"n={n1}")

        # 模拟旧数据污染：append 一段旧字段空壳
        with open(items1, "a", encoding="utf-8") as f:
            for i in range(3):
                f.write(json.dumps({"title": f"旧{i}", "area": "", "company": "", "link": "/old"}) + "\n")
        polluted = len(items1.read_text(encoding="utf-8").splitlines())
        check("污染模拟成功", polluted == 6, f"n={polluted}")

        r2, items2, n2 = run_once()
        fields2 = sorted(set(k for it in r2["sample"] for k in it if not k.startswith("_")))
        check("第2次清空旧数据", n2 == 3, f"n={n2}")
        check("第2次 sample 是最新数据", r2["result"].get("total") == 3 and fields2 == ["area", "company", "link", "title"],
              f"fields={fields2}")
        check("第2次 sample 无旧空壳", all(str(it.get("area") or "").strip() for it in r2["sample"]),
              str(r2["sample"])[:160])
        v = {c["name"]: c.get("pass") for c in (r2.get("verify") or {}).get("checks", [])}
        check("第2次复核全过", all(v.values()), str(v))
        srv.shutdown()
    finally:
        auto._llm_chat = orig_chat

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
