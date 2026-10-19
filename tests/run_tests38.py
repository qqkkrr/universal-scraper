#!/usr/bin/env python3
"""第二十七轮回归（6 个）：第四轮 review 修复——
auto 超时真停止(.stop)、human_solve 非 tty 不阻塞、core.paginate 死代码删除。
用法: python3 tests/run_tests38.py
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
            body = b"slow done"
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

    print("== #2 human_solve 非 tty 不阻塞 ==")
    from universal_scraper.antibot import solve_captcha_file
    import tempfile
    img = Path(tempfile.mkdtemp()) / "cap.png"
    img.write_bytes(b"x")
    class _FakeStdin:
        def isatty(self):
            return False
    old_stdin = sys.stdin
    sys.stdin = _FakeStdin()
    try:
        res = solve_captcha_file(str(img), {"captcha": {"strategy": "human"}})
    finally:
        sys.stdin = old_stdin
    check("human 非 tty 返回错误", "非交互" in (res.get("error") or ""), str(res))
    check("human 非 tty 不阻塞", True)  # 能返回即证明没卡 input()

    print("== #1 auto 超时真停止 ==")
    from universal_scraper.auto import run_with_config
    td = ROOT / "outputs" / ".test_tmp" / "timeout_task"
    (td / "modules").mkdir(parents=True, exist_ok=True)
    cfg = {
        "name": "timeout_task", "start_urls": [base + "/slow"],
        "queue": {"max_depth": 1, "max_requests": 1, "max_concurrency": 1},
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "default"}],
        "parsers": {"default": {"type": "html", "row_css": "body", "fields": {"t": {"css": "body::text"}}}},
        "storage": {"type": "jsonl", "name": "timeout_task"},
        "output": {"dir": "outputs/.test_tmp", "base_name": "timeout_task"},
        "anti_bot": {"min_interval": 0.01, "max_retries": 1},
    }
    (td / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    try:
        (td / ".stop").unlink()
    except Exception:
        pass
    t0 = time.time()
    out = run_with_config(cfg, "timeout_task", str(td), description="test",
                          rounds=1, round_timeout=1)
    dt = time.time() - t0
    err = (out.get("result") or {}).get("error") or ""
    check("超时被识别", "超时" in err, str(err)[:80])
    check("已写入 .stop 停止信号", (td / ".stop").exists())
    check("超时后能返回（不无限等）", dt < 40, f"dt={dt:.1f}s")

    print("== #3 core.paginate 死代码已删 ==")
    import universal_scraper.core as core_mod
    check("paginate 已移除", not hasattr(core_mod, "paginate"))
    check("core 仍可导入", callable(getattr(core_mod, "make_http_client", None)))

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
