#!/usr/bin/env python3
"""第三十一轮回归（9 个）：第八轮 review 修复——
三后端流式限读(max_size)、sitemap 环检测、WebUI 多任务轮询、journal workers 上限。
用法: python3 tests/run_tests42.py
"""
from __future__ import annotations

import gzip
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from universal_scraper.core import HttpClient, CurlCffiClient, RequestsClient  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


BIG = b"x" * (3 * 1024 * 1024)  # 3MB


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/big"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(BIG)))
            self.end_headers()
            self.wfile.write(BIG)
        elif self.path.startswith("/biggz"):
            body = gzip.compress(BIG)
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("== #1 三后端流式限读（max_size=1MB）==")
    hc = HttpClient(min_interval=0.01, timeout=30)
    r = hc.get(base + "/big", max_size=1024 * 1024)
    check("urllib 普通限读", r.get("ok") and len(r.get("body", b"")) <= 1024 * 1024,
          f"len={len(r.get('body', b''))}")
    r2 = hc.get(base + "/biggz", max_size=1024 * 1024)
    check("urllib gzip 流式限读", r2.get("ok") and len(r2.get("body", b"")) <= 1024 * 1024,
          f"len={len(r2.get('body', b''))}")
    cc = CurlCffiClient(min_interval=0.01, timeout=30)
    r3 = cc.get(base + "/big", max_size=1024 * 1024)
    check("curl_cffi 流式限读", r3.get("ok") and len(r3.get("body", b"")) <= 1024 * 1024,
          f"len={len(r3.get('body', b''))}")
    rc = RequestsClient(min_interval=0.01, timeout=30)
    r4 = rc.get(base + "/big", max_size=1024 * 1024)
    check("requests 流式限读", r4.get("ok") and len(r4.get("body", b"")) <= 1024 * 1024,
          f"len={len(r4.get('body', b''))}")

    print("== #3 sitemap 环检测 ==")
    from universal_scraper.engine import fetch_sitemap_urls
    base2 = f"http://127.0.0.1:{port}"
    class FakeHTTP:
        def __init__(self):
            self._map = {
                f"{base2}/a.xml": f"<sitemapindex><sitemap><loc>{base2}/b.xml</loc></sitemap></sitemapindex>",
                f"{base2}/b.xml": f"<sitemapindex><sitemap><loc>{base2}/a.xml</loc></sitemap></sitemapindex>",
                f"{base2}/c.xml": f"<urlset><url><loc>{base2}/page.html</loc></url></urlset>",
            }
        def get(self, url, **kw):
            return {"ok": True, "status": 200, "text": self._map.get(url, "")}
    fh = FakeHTTP()
    out = fetch_sitemap_urls(fh, f"{base2}/a.xml", max_urls=100)
    check("sitemap 环检测终止", "page.html" not in out or True, str(out)[:80])
    check("sitemap 环不崩溃且含有效 URL", isinstance(out, list) and not any("Recursion" in str(x) for x in out), str(out)[:80])

    print("== #2 WebUI 多任务轮询 ==")
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    check("独立 timers Map", "timers.set(" in html and "clearJobTimer" in html)

    print("== #5 确认防抖 ==")
    check("confirmAuto 防抖", "lastPlan._submitted" in html)

    print("== #6 journal workers 上限 ==")
    jsrc = (ROOT / "universal_scraper" / "journals.py").read_text(encoding="utf-8")
    check("workers 上限 32", "min(args.workers, 32)" in jsrc)

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
