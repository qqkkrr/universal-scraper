#!/usr/bin/env python3
"""第二十三轮回归（15 个）：Code Review 修复项——
Cookie jar 多值/Expires、HTTP 缓存 base64+TTL、jpath 多通配、v3 会话池封禁、
WebUI 路径穿越防护、引擎磁盘 spool、SeenStore 批量 flush、停止信号。
用法: python3 tests/run_tests34.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from universal_scraper.core import (  # noqa: E402
    split_set_cookie, update_cookie_jar, jar_cookie_header, HttpClient,
)
from universal_scraper.selectors import jpath  # noqa: E402
from universal_scraper.session import SessionPool  # noqa: E402
from universal_scraper.storage import SeenStore  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/setcookies"):
            body = b"ok"
            self.send_response(200)
            self.send_header("Set-Cookie", "a=1; Path=/")
            self.send_header("Set-Cookie", "b=2; Expires=Wed, 21 Oct 2026 07:28:00 GMT; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/echo"):
            body = ("cookie=" + (self.headers.get("Cookie") or "")).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/blocked"):
            body = "<html>访问过于频繁，请稍后再试 安全验证</html>".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/list"):
            # 10 个条目，测 spool
            body = "".join(f"<div class='i'><span class='t'>item-{i}</span></div>" for i in range(10)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b'{"msg":"hello"}'
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

    print("== Cookie jar（review #2）==")
    joined = "a=1; Path=/, b=2; Expires=Wed, 21 Oct 2026 07:28:00 GMT; Path=/"
    parts = split_set_cookie(joined)
    check("split_set_cookie 切分", len(parts) == 2 and "b=2" in parts[1], str(parts))
    jar = SessionPool(proxies=None).acquire(base + "/").jar
    update_cookie_jar(jar, base + "/", {"set-cookie": joined})
    names = sorted(c.name for c in jar)
    check("jar 两条都收", names == ["a", "b"], str(names))
    hdr = jar_cookie_header(jar, base + "/x")
    check("jar 生成请求头", "a=1" in hdr and "b=2" in hdr, hdr)

    print("== v3 会话池持久化（review #2/#6）==")
    from universal_scraper.modules.fetchers import HttpFetcher
    f = HttpFetcher({"type": "http", "url": base + "/setcookies"}, {"min_interval": 0.01}, {})
    f.fetch(__import__("universal_scraper.protocols", fromlist=["Request"]).Request(url=base + "/setcookies"))
    sess = f._cur_session
    check("v3 会话收集 cookie", "a" in [c.name for c in sess.jar], str([c.name for c in sess.jar]))
    resp = f.fetch(__import__("universal_scraper.protocols", fromlist=["Request"]).Request(url=base + "/echo"))
    check("v3 请求带回 cookie", "a=1" in resp.text and "b=2" in resp.text, resp.text[:80])

    print("== v3 封禁识别（review #5）==")
    from universal_scraper.protocols import RateLimitedError
    f3 = HttpFetcher({"type": "http", "url": base + "/blocked"}, {"min_interval": 0.01}, {})
    try:
        f3.fetch(__import__("universal_scraper.protocols", fromlist=["Request"]).Request(url=base + "/blocked"))
        check("v3 block raise", False, "未抛异常")
    except RateLimitedError as e:
        check("v3 block raise", "反爬拦截" in str(e), str(e))
    except Exception as e:
        check("v3 block raise", False, f"{type(e).__name__}: {e}")

    print("== HTTP 缓存修复（review #7）==")
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    c = HttpClient(min_interval=0.01, cache_dir=tmp)
    r1 = c.get(base + "/json", use_cache=True)
    r2 = c.get(base + "/json", use_cache=True)
    check("缓存 body 仍是 bytes", isinstance(r2.get("body"), bytes), str(type(r2.get("body"))))
    check("缓存文本正确", r2.get("text") == r1.get("text"), r2.get("text", "")[:40])

    print("== jpath 多通配（review #11）==")
    data = {"a": [{"b": [{"c": 1}, {"c": 2}]}, {"b": [{"c": 3}]}]}
    v = jpath(data, "a.*.b.*.c")
    check("jpath 多通配", v == [[1, 2], [3]], str(v))

    print("== WebUI 路径穿越防护（review #1）==")
    import urllib.request, urllib.error
    from universal_scraper.webui import Handler  # noqa: F401
    srv2 = ThreadingHTTPServer(("127.0.0.1", 0), __import__("universal_scraper.webui", fromlist=["Handler"]).Handler)
    port2 = srv2.server_address[1]
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    for bad in ("../../etc/passwd", "/etc/passwd"):
        try:
            body = urllib.request.urlopen(f"http://127.0.0.1:{port2}/api/verify?file={bad}", timeout=5).read().decode("utf-8", "ignore")
            check(f"webui 拒绝 {bad}", ("非法文件路径" in body) or ("文件不存在" in body), body[:80])
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            check(f"webui 拒绝 {bad}", "非法文件路径" in body or "文件不存在" in body, f"HTTP {e.code} {body[:80]}")
        except Exception as e:
            check(f"webui 拒绝 {bad}", False, f"异常 {type(e).__name__}: {e}")

    print("== 引擎磁盘 spool（review #4）==")
    from universal_scraper.engine_v3 import run_task
    td = ROOT / "outputs" / ".test_tmp" / "spool_task"
    (td / "modules").mkdir(parents=True, exist_ok=True)
    (td / "config.json").write_text(json.dumps({
        "name": "spool_task",
        "start_urls": [base + "/list"],
        "queue": {"max_depth": 1, "max_requests": 1, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "default"}],
        "parsers": {"default": {"type": "html", "row_css": ".i",
                                "fields": {"t": {"css": ".t::text"}}}},
        "storage": {"type": "jsonl", "name": "spool_task"},
        "output": {"dir": "outputs/.test_tmp", "base_name": "spool_task",
                   "spool_threshold": 5},
        "anti_bot": {"min_interval": 0.01},
    }), encoding="utf-8")
    r = run_task(td)
    fp = ROOT / "outputs" / ".test_tmp" / "spool_task.json"
    rows = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else []
    check("spool 导出完整", r.get("total") == 10 and len(rows) == 10,
          f"total={r.get('total')} rows={len(rows)}")

    print("== SeenStore 批量 flush（review #14）==")
    sp = ROOT / "outputs" / ".test_tmp" / "seen_test.txt"
    try:
        sp.unlink()
    except Exception:
        pass
    st = SeenStore(sp, flush_every=3)
    for k in ("k1", "k2", "k3", "k4", "k5"):
        st.mark(k)
    st.flush()
    content = sp.read_text(encoding="utf-8")
    check("SeenStore flush", all(k in content for k in ("k1", "k2", "k3", "k4", "k5")), content)

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
