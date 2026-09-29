#!/usr/bin/env python3
"""跨引擎一致性套件（收官十五轮新增）。

用户的 KLEKT 复盘暴露了"两引擎各缺一半"的断层（v2 有 regex_extract 无详情后处理入口，
v3 有入口无 regex_extract）。本套件对**同一批语义**在两个引擎上各跑一遍，断言结果一致。

实现说明：直接**进程内**调用 CLI 的 main()（sys.argv 注入 + stdout 捕获），
不起子进程——更快、且避免"动态 argv 被静态扫描误判为命令注入"的噪音。

用法: python3 tests/cross_engine.py    （约 30 秒）
"""
import contextlib
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("US_ALLOW_PRIVATE", "1")
RESULTS = []

PAGE_TMPL = """<html><body>
<div class="row"><b>条目A</b><span class="n">12</span><span class="t">2026-09-01 10:00</span>
  <span class="c">abc</span><a href="{base}/detail/1">d</a></div>
<div class="row"><b>条目B</b><span class="n">7</span><span class="t">2026-09-02 11:30</span>
  <span class="c">def</span><a href="{base}/detail/2">d</a></div>
<div class="row"><b>条目C</b><span class="n">30</span><span class="t">2026-09-03 09:15</span>
  <span class="c">ghi</span><a href="{base}/detail/3">d</a></div>
<div class="row"><b>条目A</b><span class="n">12</span><span class="t">2026-09-01 10:00</span>
  <span class="c">abc</span><a href="{base}/detail/1">d</a></div>
</body></html>"""

DETAIL = """<html><body><div class="price">Lowest Listing Price€ 104.52 (incl. VAT)</div></body></html>"""

# 引擎**内部行元数据**白名单（比对时排除；均为 v2 侧的历史实现细节）：
# - source_name：v2 run_config 给每行附任务名（合并导出溯源用）
# - detail_body：v2 的断点续传哨兵（缓存详情原文，resume 判"已有详情"）
# - url_final  ：v2 记录重定向后的最终 URL
# v3 用等价但不同名的机制（seen/state），故不出现。**业务字段必须一致**。
V2_META_KEYS = {"source_name", "detail_body", "url_final"}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        host = self.headers.get("Host") or "127.0.0.1"
        if self.path.startswith("/detail"):
            body = DETAIL.encode()
        else:
            body = PAGE_TMPL.format(base=f"http://{host}").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  ✅ " if cond else "  ❌ ") + name + (f" | {detail}" if detail and not cond else ""))


def run_cli(args) -> tuple:
    """进程内跑 CLI（返回 (rc, stdout+stderr)）。"""
    from universal_scraper import cli as cli_mod
    buf = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["universal-scraper", *args]
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                rc = cli_mod.main()
            except SystemExit as e:
                rc = int(e.code or 0)
    finally:
        sys.argv = old_argv
    return (rc if isinstance(rc, int) else 0), buf.getvalue()


PIPELINE = [
    {"type": "filter", "field": "n", "op": "between", "min": 10},
    {"type": "dedup", "key": "标题"},
    {"type": "cast", "field": "n", "to": "int"},
    {"type": "add", "field": "来源", "value": "x"},
    {"type": "rename", "mapping": {"t": "时间"}},
    {"type": "template", "field": "标签", "tmpl": "{标题}-{n}"},
    {"type": "regex_extract", "field": "时间", "to": "日期",
     "pattern": r"^(\d{4}-\d{2}-\d{2})", "group": 1},
    {"type": "transform", "field": "code", "op": "upper"},
]


def build_v2(base: str, tmp: Path) -> Path:
    cfg = tmp / "v2.json"
    cfg.write_text(json.dumps({
        "name": "xeng_v2",
        "start_urls": [f"{base}/list"],
        "source": {"type": "http_html", "url": f"{base}/list", "row_css": "div.row",
                   "fields": {"标题": {"css": "b"}, "n": {"css": "span.n"},
                              "t": {"css": "span.t"},
                              "code": {"css": "span.c"},
                              "url": {"css": "a", "attr": "href"}}},
        "pipeline": PIPELINE,
        "detail": {"enabled": True, "url_field": "url", "interval": 0.0,
                   "extract": [{"name": "raw", "type": "xpath_text",
                                "xpath": "//div[@class='price']"}],
                   "post_pipeline": [{"type": "regex_extract", "field": "raw", "to": "eur",
                                      "pattern": r"€\s*([\d.]+)", "group": 1}]},
        "output": {"dir": str(tmp / "out_v2"), "formats": ["json"]},
        "anti_bot": {"min_interval": 0.0},
    }, ensure_ascii=False), encoding="utf-8")
    return cfg


def build_v3(base: str, tmp: Path) -> Path:
    tdir = tmp / "task_v3"
    (tdir / "modules").mkdir(parents=True, exist_ok=True)
    (tdir / "config.json").write_text(json.dumps({
        "name": "xeng_v3", "type": "scrape", "start_urls": [f"{base}/list"],
        "source": {"type": "http"}, "storage": {"type": "jsonl"},
        "parsers": {"list": {"type": "html", "row_css": "div.row",
                             "fields": {"标题": {"css": "b"}, "n": {"css": "span.n"},
                                        "t": {"css": "span.t"},
                              "code": {"css": "span.c"},
                                        "url": {"css": "a::attr(href)"}}}},
        "rules": [{"match": "contains", "pattern": "/", "parser": "list"}],
        "pipelines": PIPELINE,
        "detail": {"enabled": True, "url_field": "url", "interval": 0.0,
                   "extract": [{"name": "raw", "type": "xpath_text",
                                "xpath": "//div[@class='price']"}],
                   "post_pipeline": [{"type": "regex_extract", "field": "raw", "to": "eur",
                                      "pattern": r"€\s*([\d.]+)", "group": 1}]},
        "output": {"dir": str(tmp / "out_v3"), "formats": ["json"]},
        "anti_bot": {"min_interval": 0.0},
    }, ensure_ascii=False), encoding="utf-8")
    return tdir


def norm(rows):
    """归一化对比：只比业务字段（去内部 _ 前缀键），值转字符串。"""
    if not rows:
        return []
    return [{k: str(v) for k, v in sorted(r.items())
             if not str(k).startswith("_") and k not in V2_META_KEYS}
            for r in rows]


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="us_xeng_"))
    try:
        cfg = build_v2(base, tmp)
        tdir = build_v3(base, tmp)
        rc2, log2 = run_cli(["run", "--config", str(cfg)])
        rc3, log3 = run_cli(["run", "--task", str(tdir)])
        f2, f3 = tmp / "out_v2" / "xeng_v2.json", tmp / "out_v3" / "xeng_v3.json"
        rows2 = json.loads(f2.read_text(encoding="utf-8")) if f2.exists() else None
        rows3 = json.loads(f3.read_text(encoding="utf-8")) if f3.exists() else None
        check("v2 运行成功", rc2 == 0 and rows2 is not None, f"rc={rc2} log={log2[-200:]!r}")
        check("v3 运行成功", rc3 == 0 and rows3 is not None, f"rc={rc3} log={log3[-200:]!r}")
        n2, n3 = norm(rows2), norm(rows3)
        check("行数一致（filter+dedup 后）", len(n2) == len(n3) == 2,
              f"v2={len(n2)} v3={len(n3)}")
        check("业务字段全一致", n2 == n3, f"\n      v2={n2[:1]}\n      v3={n3[:1]}")
        for key, want in (("日期", "2026-09-01"), ("来源", "x"), ("eur", "104.52")):
            v2v = n2[0].get(key) if n2 else None
            v3v = n3[0].get(key) if n3 else None
            check(f"派生列 {key} 一致", v2v == v3v == want,
                  f"v2={v2v!r} v3={v3v!r} 期望={want!r}")
        check("transform upper 生效（两引擎一致）",
              bool(n2) and bool(n3) and n2[0].get("code") == n3[0].get("code") == "ABC",
              f"v2={(n2 or [{}])[0].get('code')!r} v3={(n3 or [{}])[0].get('code')!r}")
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n跨引擎一致性 {len(RESULTS)} 项，失败 {len(bad)}" + (f": {bad}" if bad else "  ★ 全部通过"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
