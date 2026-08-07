#!/usr/bin/env python3
"""第三十轮回归（7 个）：第七轮 review 修复——
浏览器桥非法正则不再崩溃、dianping 空 HTML 防护、verify 字段并集。
用法: python3 tests/run_tests41.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"<html><body>hello</body></html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("== 浏览器桥非法正则不崩溃 ==")
    import os, tempfile
    NODE = os.environ.get("UNIVERSAL_SCRAPER_NODE",
                          "/Users/kairanqin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
    NODE_PATH = os.environ.get("UNIVERSAL_SCRAPER_NODE_PATH",
                               "/Users/kairanqin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules")
    with tempfile.TemporaryDirectory() as tmp:
        spec = {"url": base + "/", "wait": {"selector": "body", "timeout": 8000}, "scrollCount": 0,
                "capture": [{"name": "bad", "url_pattern": "(", "save": True}]}
        sp = Path(tmp) / "spec.json"
        out = Path(tmp) / "out"
        out.mkdir()
        sp.write_text(json.dumps(spec), encoding="utf-8")
        p = subprocess.run([NODE, str(ROOT / "scripts/browser_generic.cjs"),
                            "--spec", str(sp), "--out", str(out), "--headless", "1", "--scrollCount", "0"],
                           capture_output=True, text=True, timeout=60,
                           env={**os.environ, "NODE_PATH": NODE_PATH})
        check("桥 exit=0", p.returncode == 0, f"rc={p.returncode}")
        check("桥仍产出 page", '"type":"page"' in p.stdout, p.stdout[:120])
        check("stderr 无正则崩溃", "Invalid regular expression" not in p.stderr, p.stderr[:120])

    print("== requireCookie 不再裸 new RegExp ==")
    src = (ROOT / "scripts/browser_generic.cjs").read_text(encoding="utf-8")
    # 判定：任何含 new RegExp( 的行必须同时含 catch（即被 try/catch 保护）
    bad = [ln.strip() for ln in src.splitlines() if "new RegExp(" in ln and "catch" not in ln]
    check("桥内无裸 new RegExp", not bad, str(bad[:3]))

    print("== dianping 空 HTML 防护 ==")
    from universal_scraper.dianping import parse_search_html
    check("空 HTML 返回 []", parse_search_html("") == [] and parse_search_html("   ") == [])

    print("== verify 字段并集 ==")
    from universal_scraper.verify import verify_rows
    rows = [{"title": "a", "url": "http://x/1"}, {"title": "b", "url": "http://x/2", "price": "9"}]
    rep = verify_rows(rows, None, sample_n=0, network=False)
    names = [c["name"] for c in rep["checks"]]
    check("字段完整率覆盖后续新字段", any("price" in n for n in names), str(names))

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
