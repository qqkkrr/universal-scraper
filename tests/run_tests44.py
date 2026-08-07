#!/usr/bin/env python3
"""第三十三轮回归（6 个）：第十轮 review 修复——
任务运行锁（同名并发互斥）、模块名用目录名、browser_agent 收集上限。
用法: python3 tests/run_tests44.py
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

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/slow"):
            time.sleep(3)
        body = b"<html><body>ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("== #1 任务运行锁 ==")
    from universal_scraper.engine_v3 import run_task
    td = ROOT / "outputs" / ".test_tmp" / "lock_task"
    (td / "modules").mkdir(parents=True, exist_ok=True)
    cfg = {"name": "lock_task", "start_urls": [base + "/slow"],
           "queue": {"max_depth": 1, "max_requests": 1, "max_concurrency": 1},
           "source": {"type": "http"},
           "rules": [{"match": "contains", "pattern": "/", "parser": "default"}],
           "parsers": {"default": {"type": "html", "row_css": "body", "fields": {"t": {"css": "body::text"}}}},
           "storage": {"type": "jsonl", "name": "lock_task"},
           "output": {"dir": "outputs/.test_tmp", "base_name": "lock_task"},
           "anti_bot": {"min_interval": 0.01}}
    (td / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    (td / ".running.lock").unlink(missing_ok=True)
    box = {}
    def _run():
        box["r"] = run_task(td)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(1.0)  # 确保第一个任务已加锁
    second_err = None
    try:
        run_task(td)
    except RuntimeError as e:
        second_err = str(e)
    except Exception as e:
        second_err = f"{type(e).__name__}: {e}"
    check("并发第二个任务被拒绝", second_err is not None and "正在被另一个任务使用" in second_err, str(second_err))
    t.join(timeout=20)
    check("第一个任务正常完成", not t.is_alive() and "total" in (box.get("r") or {}), str(box.get("r", {}))[:80])
    check("锁文件已释放", not (td / ".running.lock").exists())

    print("== #3 模块名用目录名（含空格）==")
    from universal_scraper.task import Task
    td2 = ROOT / "outputs" / ".test_tmp" / "my task"
    (td2 / "modules").mkdir(parents=True, exist_ok=True)
    cfg2 = {"name": "weird name", "start_urls": ["http://x/"], "source": {"type": "http"},
            "rules": [{"match": "contains", "pattern": "/", "parser": "default"}],
            "parsers": {"default": {"type": "html"}}, "storage": {"type": "jsonl"}}
    (td2 / "config.json").write_text(json.dumps(cfg2), encoding="utf-8")
    (td2 / "modules" / "parser.py").write_text(
        "from universal_scraper.protocols import BaseParser, ParseResult\n"
        "class Parser(BaseParser):\n"
        "    name = 'default'\n"
        "    def parse(self, resp, ctx):\n"
        "        return ParseResult(items=[], requests=[])\n", encoding="utf-8")
    t2 = Task(td2)
    pmap = t2.get_parsers()
    check("含空格目录名可加载自定义解析器", pmap.get("default").__name__ == "Parser", str(pmap.get("default")))

    print("== #2 browser_agent 收集上限 ==")
    cjs = (ROOT / "scripts" / "browser_agent.cjs").read_text(encoding="utf-8")
    check("netEvents 上限", "netEvents.length < 200" in cjs)
    check("wsFrames 上限", "wsFrames.length >= 500" in cjs)

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
