#!/usr/bin/env python3
"""端到端 CLI 验证矩阵（离线、可重复；收官十五轮新增）。

起一个本地 HTTP 站点模拟真实抓取场景（静态列表/详情/JSON 分页/gzip/GBK/SSR/壳页/
重定向/404/慢响应），逐条跑真实 CLI 命令，断言退出码与关键输出。

用法: python3 tests/e2e_matrix.py            （约 1-2 分钟）
      python3 tests/e2e_matrix.py --only fetch
"""
import gzip
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
PORT = 0
BASE = ""

LIST_TMPL = """<html><head><title>测试列表</title></head><body>
<ul class="items">
  <li class="item"><b>条目一</b><a href="{base}/detail/1">详情</a></li>
  <li class="item"><b>条目二</b><a href="{base}/detail/2">详情</a></li>
  <li class="item"><b>条目三</b><a href="{base}/detail/3">详情</a></li>
</ul></body></html>"""


def list_html(host: str) -> str:
    return LIST_TMPL.format(base=f"http://{host}")

DETAIL_HTML_TMPL = """<html><head><title>详情{k}</title></head><body>
<div class="title">条目{k}</div>
<div class="price">Lowest Listing Price€ {p}.{p2} (incl. VAT)</div>
<div class="pub">2026-09-0{k}</div></body></html>"""

JSON_ROWS = [{"id": i, "name": f"行{i}", "score": i * 10} for i in range(1, 26)]

SSR_HTML = ("<html><body><h1>Listing</h1><p>Loading…</p><script>window.__DATA__ = "
            + json.dumps({"rows": [{"k": i, "v": f"value-{i}"} for i in range(1500)]})
            + "</script></body></html>")

SHELL_HTML = ("<html><body><div id='app'></div>"
              "<script src='/a.js'></script><script src='/b.js'></script>"
              "<script src='/c.js'></script></body></html>")

GBK_HTML = "<html><body><p>中文编码测试：简体中文内容用于验证 smart_decode</p></body></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):  # noqa: C901
        p = self.path.split("?")[0]
        host = self.headers.get("Host") or f"127.0.0.1:{PORT}"
        if p in ("/", "/list", "/static"):
            self._send(200, list_html(host).encode())
        elif p.startswith("/detail/"):
            k = p.rsplit("/", 1)[-1]
            try:
                ki = int(k)
            except ValueError:
                ki = 1
            self._send(200, DETAIL_HTML_TMPL.format(k=ki, p=1000 + ki, p2=52).encode())
        elif p == "/json":
            qs = dict(x.split("=", 1) for x in (self.path.split("?", 1) + [""])[1].split("&") if "=" in x)
            page = int(qs.get("page", "1") or 1)
            data = {"code": 0, "data": {"total": len(JSON_ROWS),
                                        "rows": JSON_ROWS[(page - 1) * 10: page * 10]}}
            self._send(200, json.dumps(data).encode(), "application/json")
        elif p == "/gzip":
            body = gzip.compress(list_html(host).encode())
            self._send(200, body, "text/html; charset=utf-8", {"Content-Encoding": "gzip"})
        elif p == "/gbk":
            self._send(200, GBK_HTML.encode("gbk"), "text/html; charset=gbk")
        elif p == "/ssr":
            self._send(200, SSR_HTML.encode())
        elif p == "/shell":
            self._send(200, SHELL_HTML.encode())
        elif p == "/redirect":
            self._send(302, b"", extra={"Location": "/list"})
        elif p == "/slow":
            time.sleep(3)
            self._send(200, b"<html><body>slow ok</body></html>")
        elif p == "/blocked":
            self._send(403, b"<html><title>Access Denied</title></html>")
        else:
            self._send(404, b"<html><body>not found</body></html>")

    def log_message(self, *a):
        pass


def start_server():
    global PORT, BASE
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    PORT = srv.server_address[1]
    BASE = f"http://127.0.0.1:{PORT}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  ✅ " if cond else "  ❌ ") + name + (f" | {detail}" if detail and not cond else ""))


def cli(*args, cwd=None, timeout=120, env_extra=None):
    env = {**os.environ, "US_ALLOW_PRIVATE": "1", "PYTHONPATH": str(ROOT)}
    env.update(env_extra or {})
    return subprocess.run([PY, "-m", "universal_scraper.cli", *args],
                          capture_output=True, text=True, cwd=str(cwd or ROOT),
                          timeout=timeout, env=env)


def main(only=None):
    def want(sec):
        return (only is None) or (only == sec)

    srv = start_server()
    tmp = Path(tempfile.mkdtemp(prefix="us_e2e_"))
    try:
        if want("fetch"):
            print("== fetch 矩阵 ==")
            r = cli("fetch", f"{BASE}/list")
            check("fetch 静态页 exit0 且有内容", r.returncode == 0 and "条目一" in r.stdout,
                  f"rc={r.returncode} out={r.stdout[:80]!r}")
            r = cli("fetch", f"{BASE}/gzip")
            check("fetch gzip 透明解压", r.returncode == 0 and "条目一" in r.stdout,
                  f"rc={r.returncode} out={r.stdout[:80]!r}")
            r = cli("fetch", f"{BASE}/gbk")
            check("fetch GBK 正确解码", r.returncode == 0 and "中文编码测试" in r.stdout,
                  f"rc={r.returncode} out={r.stdout[:120]!r}")
            r = cli("fetch", f"{BASE}/json", "--json")
            ok = False
            try:
                env = json.loads(r.stdout)
                ok = env.get("status") == 200 and "rows" in json.dumps(env)
            except Exception:
                ok = False
            check("fetch --json 出信封结构", r.returncode == 0 and ok, f"rc={r.returncode}")
            r = cli("fetch", f"{BASE}/list", "--links")
            check("fetch --links 抽到外链", "detail/1" in r.stdout, r.stdout[-120:])
            r = cli("fetch", f"{BASE}/redirect")
            check("fetch 跟随重定向", r.returncode == 0 and "条目一" in r.stdout,
                  f"rc={r.returncode}")
            r = cli("fetch", f"{BASE}/ssr")
            check("fetch SSR 识别（非壳页指路）", r.returncode == 3 and "疑似 SSR" in r.stderr,
                  f"rc={r.returncode} err={r.stderr[:100]!r}")
            r = cli("fetch", f"{BASE}/shell")
            check("fetch 壳页识别", r.returncode == 3 and "壳页" in r.stderr, f"rc={r.returncode}")
            out_f = tmp / "page.html"
            r = cli("fetch", f"{BASE}/list", "--raw", "--out", str(out_f))
            check("fetch --raw --out 落盘", out_f.exists() and "<ul" in out_f.read_text(encoding="utf-8"),
                  f"rc={r.returncode}")
            r = cli("fetch", f"{BASE}/slow", "--timeout" if False else "--json")
            check("fetch 慢响应不崩", r.returncode in (0, 1, 3), f"rc={r.returncode}")

        if want("diagnose"):
            print("== diagnose 矩阵 ==")
            r = cli("diagnose", f"{BASE}/list")
            check("diagnose 正常页 exit0", r.returncode == 0, f"rc={r.returncode} {r.stdout[:80]!r}")
            r = cli("diagnose", f"{BASE}/blocked")
            check("diagnose 403 exit2", r.returncode == 2, f"rc={r.returncode}")
            out_j = tmp / "diag.json"
            r = cli("diagnose", f"{BASE}/list", "--out", str(out_j))
            check("diagnose --out 落盘", out_j.exists() and out_j.read_text(encoding="utf-8").strip().startswith("{"),
                  f"rc={r.returncode}")

        if want("validate"):
            print("== validate 矩阵 ==")
            good = tmp / "good.json"
            good.write_text(json.dumps({
                "name": "e2e", "source": {"type": "http_html", "url": f"{BASE}/list"},
                "record": {"fields": {"标题": {"from": "b"}}},
                "pipeline": [{"type": "dedup", "key": "标题"}],
            }, ensure_ascii=False), encoding="utf-8")
            r = cli("validate", "--config", str(good))
            check("validate 合法配置 exit0", r.returncode == 0, f"rc={r.returncode} {r.stdout[:80]!r}")
            bad = tmp / "bad.json"
            bad.write_text(json.dumps({"name": "x", "source": {"type": "no_such"}}), encoding="utf-8")
            r = cli("validate", "--config", str(bad))
            check("validate 非法配置非 0", r.returncode != 0, f"rc={r.returncode}")
            warn = tmp / "warn.json"
            warn.write_text(json.dumps({
                "name": "w", "source": {"type": "http_html", "url": f"{BASE}/list"},
                "record": {"fields": {"k": {"from": "b"}}},
                "pipeline": [{"type": "regex_extract", "field": "raw", "pattern": r"(\d+)", "to": "n"}],
                "detail": {"enabled": True, "url_field": "url",
                           "extract": [{"name": "raw", "type": "xpath_text", "xpath": "//div"}]},
            }, ensure_ascii=False), encoding="utf-8")
            r = cli("validate", "--config", str(warn))
            check("validate 详情字段告警", "post_pipeline" in r.stderr and r.returncode == 0,
                  f"rc={r.returncode} err={r.stderr[:120]!r}")

        if want("run_v2"):
            print("== run --config（v2 引擎）矩阵 ==")
            cfg = tmp / "v2.json"
            cfg.write_text(json.dumps({
                "name": "e2e_v2",
                "start_urls": [f"{BASE}/list"],
                # v2 的 HTML 字段契约在 source.fields（css/attr/xpath），不是 record.fields
                "source": {"type": "http_html", "url": f"{BASE}/list", "row_css": "li.item",
                           "fields": {"标题": {"css": "b"},
                                      "url": {"css": "a", "attr": "href"}}},
                "pipeline": [{"type": "dedup", "key": "标题"}],
                "detail": {"enabled": True, "url_field": "url", "interval": 0.0,
                           "extract": [{"name": "raw", "type": "xpath_text", "xpath": "//div[@class='price']"}],
                           "post_pipeline": [{"type": "regex_extract", "field": "raw", "to": "eur",
                                              "pattern": r"€\s*([\d.]+)", "group": 1}]},
                "output": {"dir": str(tmp / "out_v2"), "formats": ["json"]},
                "anti_bot": {"min_interval": 0.0},
            }, ensure_ascii=False), encoding="utf-8")
            r = cli("run", "--config", str(cfg), timeout=180)
            jf = tmp / "out_v2" / "e2e_v2.json"
            ok = False
            if jf.exists():
                rows = json.loads(jf.read_text(encoding="utf-8"))
                ok = (isinstance(rows, list) and len(rows) == 3
                      and all(x.get("eur") for x in rows))
            check("v2 列表+详情+post_pipeline 派生列", r.returncode == 0 and ok,
                  f"rc={r.returncode} rows={jf.read_text()[:200] if jf.exists() else 'N/A'}")
            r2 = cli("run", "--config", str(cfg), "--limit", "1", timeout=180)
            jf2 = tmp / "out_v2" / "e2e_v2.json"
            rows2 = json.loads(jf2.read_text(encoding="utf-8")) if jf2.exists() else []
            check("v2 --limit 生效", r2.returncode == 0 and len(rows2) == 1,
                  f"rc={r2.returncode} n={len(rows2)}")

        if want("run_v3"):
            print("== run <task>（v3 引擎）矩阵 ==")
            tdir = tmp / "task_v3"
            (tdir / "modules").mkdir(parents=True, exist_ok=True)
            (tdir / "config.json").write_text(json.dumps({
                "name": "e2e_v3", "type": "scrape",
                "start_urls": [f"{BASE}/list"],
                "source": {"type": "http"},
                "storage": {"type": "jsonl"},
                "parsers": {"list": {"type": "html", "row_css": "li.item",
                                     "fields": {"标题": {"css": "b"}, "url": {"css": "a::attr(href)"}}}},
                "rules": [{"match": "contains", "pattern": "/", "parser": "list"}],
                "detail": {"enabled": True, "url_field": "url", "interval": 0.0,
                           "extract": [{"name": "raw", "type": "xpath_text", "xpath": "//div[@class='price']"}],
                           "post_pipeline": [{"type": "regex_extract", "field": "raw", "to": "eur",
                                              "pattern": r"€\s*([\d.]+)", "group": 1}]},
                "output": {"dir": str(tmp / "out_v3"), "formats": ["json"]},
                "anti_bot": {"min_interval": 0.0},
            }, ensure_ascii=False), encoding="utf-8")
            r = cli("run", "--task", str(tdir), timeout=180)
            jf = tmp / "out_v3" / "e2e_v3.json"
            ok = False
            if jf.exists():
                rows = json.loads(jf.read_text(encoding="utf-8"))
                ok = (isinstance(rows, list) and len(rows) == 3
                      and all(x.get("eur") for x in rows))
            check("v3 列表+详情+post_pipeline 派生列（跨引擎同义）",
                  r.returncode == 0 and ok,
                  f"rc={r.returncode} rows={(jf.read_text()[:200] if jf.exists() else 'N/A')}")
            # resume：第二轮不应重复抓取（比对请求计数不便，改验 exit0 且行数不变）
            r2 = cli("run", "--task", str(tdir), "--resume", timeout=180)
            rows2 = json.loads(jf.read_text(encoding="utf-8")) if jf.exists() else []
            check("v3 --resume 可跑且不炸", r2.returncode in (0, 3) and len(rows2) == 3,
                  f"rc={r2.returncode} n={len(rows2)}")

        if want("report"):
            print("== report / verify 矩阵 ==")
            csvp = tmp / "data.csv"
            csvp.write_text("城市,销量,电话\n北京,10,010-88886666\n上海,20,021-62478888\n",
                            encoding="utf-8")
            r = cli("report", str(csvp), "--out", str(tmp / "r.html"))
            html = (tmp / "r.html").read_text(encoding="utf-8") if (tmp / "r.html").exists() else ""
            check("report 生成 HTML", r.returncode == 0 and "<html" in html.lower(), f"rc={r.returncode}")
            check("report 电话列不进数值统计", "010-88886666" in html and "44443338" not in html,
                  "电话被当区间取中值")
            r = cli("report", str(csvp), "--group", "不存在的列", "--out", str(tmp / "r2.html"))
            check("report 分组列缺失报错", r.returncode != 0, f"rc={r.returncode}")
            jl = tmp / "rows.jsonl"
            jl.write_text("\n".join(json.dumps({"t": f"行{i}", "n": i}) for i in range(5)) + "\n",
                          encoding="utf-8")
            r = cli("verify", "--dir", str(tmp), timeout=90)
            check("verify 可跑", r.returncode in (0, 3, 4), f"rc={r.returncode}")

        if want("batch"):
            print("== batch 队列矩阵 ==")
            q = tmp / "q.json"
            # 队列文件即 JSON 数组（CLI 无 init 子命令——子命令是 next/claim/done/...）
            q.write_text(json.dumps([{"id": "a", "status": "pending"},
                                     {"id": "b", "status": "pending"}]), encoding="utf-8")
            r1 = cli("batch", "--queue", str(q), "next")
            r2 = cli("batch", "--queue", str(q), "status")
            check("batch next+status", r1.returncode == 0 and r2.returncode == 0 and '"a"' in r1.stdout,
                  f"rc={r1.returncode}/{r2.returncode} {r1.stdout[:80]!r}")

        if want("mcp"):
            print("== MCP JSON-RPC 矩阵 ==")
            reqs = "\n".join([
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "nope", "arguments": {}}}),
                json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "fetch", "arguments": {"url": f"{BASE}/list"}}}),
            ]) + "\n"
            r = subprocess.run([PY, "-m", "universal_scraper.mcp_server"], input=reqs,
                               capture_output=True, text=True, timeout=120,
                               cwd=str(ROOT), env={**os.environ, "US_ALLOW_PRIVATE": "1",
                                                   "PYTHONPATH": str(ROOT)})
            lines = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("{")]
            codes = []
            for ln in lines:
                try:
                    codes.append(json.loads(ln).get("error", {}).get("code")
                                 or json.loads(ln).get("result") is not None)
                except Exception:
                    codes.append(None)
            ok = (len(lines) >= 3
                  and any(isinstance(c, int) and c == -32602 for c in codes)
                  and any(c is True for c in codes))
            check("MCP initialize/tools/results/-32602", ok, f"lines={len(lines)} codes={codes[:5]}")

        if want("cookies"):
            print("== cookies CLI 矩阵 ==")
            cdir = tmp / "ck"
            sess = tmp / "session.json"
            sess.write_text(json.dumps({
                "cookies": [{"name": "S", "value": "v", "domain": ".example.com", "path": "/"}],
                "origins": [],
            }), encoding="utf-8")
            r = cli("cookies", "--session", str(sess), "--domain", "example.com",
                    env_extra={"US_COOKIE_DIR": str(cdir)})
            r2 = cli("cookies", "--list", env_extra={"US_COOKIE_DIR": str(cdir)})
            check("cookies 会话导入打印 + list 可运行",
                  r.returncode == 0 and "S=v" in r.stdout and r2.returncode == 0,
                  f"rc={r.returncode}/{r2.returncode} out={r.stdout[:80]!r} err={r.stderr[:80]!r}")
            # 语义说明：`cookies --session` 打印 Cookie 串（--out 可落文件）；
            # 建立域档案走 `--from-cdp`（需调试 Chrome + node，CI 不可用）
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n矩阵共 {len(RESULTS)} 项，失败 {len(bad)}" + (f": {bad}" if bad else "  ★ 全部通过"))
    return 1 if bad else 0


if __name__ == "__main__":
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    sys.exit(main(only))
