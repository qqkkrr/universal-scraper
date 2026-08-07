#!/usr/bin/env python3
"""第二十二轮回归（15 个）：第二十二轮强化新增能力——智能编码 / 封禁识别 / 会话池 /
curl_cffi 精配抓取 / 文件下载管线 / cookies CLI / 期刊解析。
用法: python3 tests/run_tests33.py
"""
from __future__ import annotations

import gzip
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from universal_scraper.core import smart_decode, fetch_bytes  # noqa: E402
from universal_scraper.antibot import detect_block  # noqa: E402
from universal_scraper.session import SessionPool  # noqa: E402
from universal_scraper.modules.pipelines import Pipeline  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/gbk":
            body = "中文标题：沈阳体育学院学报测试".encode("gbk")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=gbk")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/gbk_noct":
            body = "没有声明编码的中文测试".encode("gb18030")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/blocked":
            body = "<html>访问过于频繁，请稍后再试 安全验证</html>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/slow":
            import time as _t
            _t.sleep(10)
            body = b"slow done"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/file.pdf":
            body = b"%PDF-1.4 fake pdf content 1234567890"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/gz.json":
            raw = json.dumps({"msg": "中文gzip响应"}).encode("utf-8")
            body = gzip.compress(raw)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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

    print("== 智能编码 ==")
    check("smart_decode GBK 声明", smart_decode("中文测试".encode("gbk"), {"content-type": "text/html; charset=gbk"}) == "中文测试")
    check("smart_decode GBK 无声明", smart_decode("中文测试".encode("gb18030"), {}) == "中文测试")
    check("smart_decode 错标 latin1", "中文" in smart_decode("中文正常".encode("utf-8"), {"content-type": "text/plain; charset=latin-1"}))

    print("== 封禁识别 ==")
    b1 = detect_block(200, "Just a moment... Enable JavaScript and cookies to continue", {"cf-ray": "x"})
    check("detect_block cloudflare", b1["kind"] == "cloudflare", str(b1))
    b2 = detect_block(200, "正在前往验证中心，请完成安全验证 滑动验证", {})
    check("detect_block verify", b2["kind"] == "verify", str(b2))
    b3 = detect_block(200, "今日天气晴朗，适合运动", {})
    check("detect_block none", b3["kind"] == "none", str(b3))
    b4 = detect_block(429, "", {})
    check("detect_block 429", b4["kind"] == "429", str(b4))

    print("== 会话池 ==")
    sp = SessionPool(proxies=None, rotate_on_errors=2)
    s1 = sp.acquire(base + "/")
    sp.report(s1, ok=False, blocked=True)  # 封禁 → 立即污染
    s2 = sp.acquire(base + "/")
    check("session rotate on block", s1 is not s2)
    sp.report(s2, ok=True)
    s3 = sp.acquire(base + "/")
    check("session reuse after ok", s3 is s2, "应复用成功会话")
    s2.cookies["a"] = "1"
    s4 = sp.acquire(base + "/")
    check("session cookie persist", s4 is s2 and s4.cookies.get("a") == "1")

    print("== 精配 fetch_html (curl_cffi + GBK) ==")
    from universal_scraper.sites import fetch_html
    r = fetch_html(base + "/gbk", timeout=10)
    check("fetch_html gbk status", r.get("ok") and r.get("status") == 200, str(r))
    check("fetch_html gbk text", "沈阳体育学院学报" in r.get("html", ""), r.get("html", "")[:50])
    r2 = fetch_html(base + "/gbk_noct", timeout=10)
    check("fetch_html gbk 无声明", "中文测试" in r2.get("html", ""), r2.get("html", "")[:50])

    print("== fetch_bytes gzip ==")
    raw = fetch_bytes(base + "/gz.json", timeout=10)
    check("fetch_bytes gzip", raw is not None and b"gzip" in raw, str(raw[:60]))

    print("== 文件下载管线 ==")
    pipe = Pipeline([{"type": "download", "field": "pdf", "dir": str(ROOT / "outputs" / ".test_tmp" / "dl"),
                      "name_template": "paper-{id}.pdf"}], {})
    item = {"id": 7, "pdf": base + "/file.pdf"}
    out = pipe.process(item)
    check("download pipeline local_file", out is not None and "paper-7.pdf" in str(out.get("local_file", "")), str(out))
    if out and out.get("local_file"):
        check("download pipeline content", Path(out["local_file"]).read_bytes()[:4] == b"%PDF")

    print("== 封禁集成（HttpFetcher 抛 RateLimitedError）==")
    from universal_scraper.fetchers import HttpFetcher
    from universal_scraper.protocols import RateLimitedError
    f = HttpFetcher({"type": "http_html", "url": base + "/blocked"}, {"min_interval": 0.01, "max_retries": 1}, {})
    try:
        f.fetch_list({"strategy": "none", "max_pages": 1})
        check("fetcher block raise", False, "未抛异常")
    except RateLimitedError as e:
        check("fetcher block raise", "反爬拦截" in str(e), str(e))
    except Exception as e:
        check("fetcher block raise", False, f"{type(e).__name__}: {e}")

    print("== cookies CLI ==")
    import subprocess, os
    sf = ROOT / "outputs" / ".test_tmp" / "sess.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"cookies": [
        {"name": "pt_key", "value": "AAA", "domain": ".jd.com"},
        {"name": "pt_pin", "value": "user", "domain": ".jd.com"},
        {"name": "other", "value": "x", "domain": ".baidu.com"},
    ]}), encoding="utf-8")
    cp = subprocess.run([sys.executable, "-m", "universal_scraper.cli", "cookies",
                         "--session", str(sf), "--domain", "jd.com"],
                        capture_output=True, text=True, cwd=str(ROOT))
    check("cookies cli jd", cp.returncode == 0 and "pt_key=AAA" in cp.stdout and "other=x" not in cp.stdout,
          cp.stdout[:100] + cp.stderr[:100])

    print("== capture_all 接口捕获 ==")
    from universal_scraper.fetchers import BrowserFetcher
    ca_dir = ROOT / "outputs" / ".test_tmp" / "ca"
    ca_dir.mkdir(parents=True, exist_ok=True)
    (ca_dir / "capture_all.json").write_text(json.dumps([
        {"url": "https://x.com/api/list", "json": {"items": [{"id": 1}]}},
        {"url": "https://x.com/api/list", "json": {"items": [{"id": 1}]}},
        {"url": "https://x.com/api/detail", "json": {"id": 2}},
    ]), encoding="utf-8")
    f2 = BrowserFetcher({"type": "browser", "url": "http://127.0.0.1:1/"},
                       {"min_interval": 0.01}, {}, ROOT)
    recs = f2._records_from_capture_all(ca_dir)
    check("capture_all records", len(recs) == 2, str(len(recs)))
    check("capture_all dedup", recs[0]["_api_url"].endswith("/api/list") and recs[1]["_api_url"].endswith("/api/detail"),
          str([r["_api_url"] for r in recs]))

    print("== 停止信号（WebUI 一键停止）==")
    import time, threading as _th
    from universal_scraper.engine_v3 import run_task as _rt
    st_dir = ROOT / "outputs" / ".test_tmp" / "stop_task"
    (st_dir / "modules").mkdir(parents=True, exist_ok=True)
    (st_dir / "config.json").write_text(json.dumps({
        "name": "stop_task",
        "start_urls": [base + "/slow"],
        "queue": {"max_depth": 1, "max_requests": 1, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "default"}],
        "parsers": {"default": {"type": "html", "row_css": "body",
                                "fields": {"t": {"css": "body::text"}}}},
        "storage": {"type": "jsonl", "name": "stop_task"},
        "output": {"dir": "outputs/.test_tmp", "base_name": "stop_task"},
        "anti_bot": {"min_interval": 0.1, "max_retries": 1, "timeout": 30},
    }), encoding="utf-8")
    # 给 mock server 加 /slow（10 秒响应）
    import tests.run_tests33 as _self
    box = {}
    def _slow_runner():
        box["r"] = _rt(st_dir)
    t = _th.Thread(target=_slow_runner, daemon=True)
    t.start()
    time.sleep(1.5)
    (st_dir / ".stop").write_text("1", encoding="utf-8")
    t.join(timeout=15)
    check("stop signal exits", not t.is_alive(), "任务未退出")
    r = box.get("r") or {}
    check("stop signal result", "total" in r, str(r)[:120])

    print("== 免费代理解析 ==")
    from universal_scraper.proxy_fetch import _parse
    pj = json.dumps({"data": [{"ip": "1.2.3.4", "port": "8080", "protocols": ["http"]},
                              {"ip": "5.6.7.8", "port": "3128", "protocols": ["https"]}]})
    ps = _parse(pj)
    check("proxy parse json", "http://1.2.3.4:8080" in ps and "http://5.6.7.8:3128" in ps, str(ps))
    check("proxy parse text", "http://9.9.9.9:80" in _parse("9.9.9.9:80"), str(_parse("9.9.9.9:80")))

    print("== 期刊期次解析 ==")
    from universal_scraper.journals import parse_issue_html
    sample = '''
    <li id="art9534" class="noselectrow">
      <div class="article-l article-w">
        <div class="biaoti-con"><div class="j-title-1 biaoti">
          <a href="https://stxb.magtech.com.cn/CN/10.12163/j.ssu.20231081">我国冰雪体育强国建设</a>
        </div></div>
        <div class="j-author">刘征</div>
        <div class="j-volumn-doi"><span class="j-volumn">2024,43(1): 1-8.</span>
          doi: <a class="j-doi" href="https://doi.org/10.12163/j.ssu.20231081">10.12163/j.ssu.20231081</a></div>
      </div>
    </li>'''
    arts = parse_issue_html(sample, {"year": "2024", "vol": "43", "issue": "1"})
    check("journal parse count", len(arts) == 1, str(len(arts)))
    if arts:
        a = arts[0]
        check("journal parse fields", a["id"] == "9534" and "冰雪" in a["title"] and a["authors"] == "刘征"
              and a["doi"] == "10.12163/j.ssu.20231081", str(a))

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
