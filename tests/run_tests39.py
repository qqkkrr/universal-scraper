#!/usr/bin/env python3
"""第二十八轮回归（9 个）：第五轮 review 修复——
三后端 UA 一致性、TLS 指纹与 UA 匹配、WebUI 消息上限、编码采样探测。
用法: python3 tests/run_tests39.py
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from universal_scraper.session import UA_POOL  # noqa: E402
from universal_scraper.core import smart_decode  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"ua": self.headers.get("User-Agent", "")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("== #1 三后端 UA 一致性 ==")
    # requests 后端：调用方 UA 必须被尊重（不再被 rotate_ua 覆盖）
    from universal_scraper.core import RequestsClient
    rc = RequestsClient(min_interval=0.01, rotate_ua=True)
    r = rc.get(base + "/ua", headers={"User-Agent": "my-fixed-ua-1"})
    echoed = (r.get("json") or {}).get("ua", "")
    check("requests 后端尊重调用方 UA", echoed == "my-fixed-ua-1", echoed)

    # curl_cffi 后端：调用方 UA 尊重 + auto 指纹按 UA 推导不报错
    from universal_scraper.core import CurlCffiClient
    cc = CurlCffiClient(min_interval=0.01, rotate_ua=True, impersonate="auto")
    r2 = cc.get(base + "/ua", headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/133.0"})
    echoed2 = (r2.get("json") or {}).get("ua", "")
    check("curl_cffi 后端尊重调用方 UA", echoed2.startswith("Mozilla") and "Firefox" in echoed2, echoed2)

    print("== #2 会话 UA 池全 Chrome（匹配默认 TLS 指纹）==")
    check("UA_POOL 全 Chrome 系", all("Chrome" in ua for ua in UA_POOL), str(UA_POOL))

    print("== #2 sites.fetch_html auto 指纹不塞 Safari UA ==")
    from universal_scraper.sites import fetch_html, UA as SITES_UA
    r3 = fetch_html(base + "/ua", timeout=10)
    echoed3 = (r3.get("html") and json.loads(r3["html"]).get("ua", "")) or ""
    check("fetch_html auto 不强制 Safari UA", echoed3 and echoed3 != SITES_UA, echoed3[:80])

    print("== #3 WebUI 消息上限 ==")
    from universal_scraper.webui import _new_job, _job_log
    job = _new_job("test", "cap")
    for i in range(1005):
        _job_log(job, f"msg-{i}")
    check("消息保留最近 1000 条", len(job["messages"]) == 1000 and job["messages"][-1] == "msg-1004",
          f"len={len(job['messages'])} last={job['messages'][-1]}")

    print("== #4 编码采样探测仍正确 ==")
    check("GBK 解码仍正确", smart_decode("中文测试".encode("gbk"), {}) == "中文测试")
    check("UTF-8 解码仍正确", smart_decode("中文".encode("utf-8"), {}) == "中文")
    check("声明编码仍优先", smart_decode("中文".encode("gbk"), {"content-type": "text/html; charset=gbk"}) == "中文")

    print("== #5 smart_decode 大响应不崩（采样路径）==")
    big = ("中文" * 40000).encode("utf-8")  # 240KB 无声明
    out = smart_decode(big, {})
    check("240KB 无声明解码", "中文" in out and len(out) >= 80000, f"len={len(out)}")

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
