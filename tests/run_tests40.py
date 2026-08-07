#!/usr/bin/env python3
"""第二十九轮回归（6 个）：第六轮 review 修复——
浏览器池空闲超时（活跃渲染不退出）、Checkpoint 有界保存、storage 写锁外。
用法: python3 tests/run_tests40.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from universal_scraper.storage import Checkpoint  # noqa: E402
from universal_scraper.protocols import Request  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/slow"):
            time.sleep(2)  # 渲染耗时 2s，大于测试用空闲超时 1.5s
            body = b"slow done ok"
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

    print("== #1 浏览器池：活跃渲染不被空闲超时杀死 ==")
    from universal_scraper.modules.fetchers import BrowserFetcher
    old_idle = os.environ.get("US_POOL_IDLE_MS")
    os.environ["US_POOL_IDLE_MS"] = "1500"  # 1.5s 空闲退出；慢页 2s > 1.5s
    try:
        import tempfile
        sess = Path(tempfile.mkdtemp())
        bf = BrowserFetcher({"type": "browser", "pool": True, "url": base + "/slow"},
                            {}, {"session_dir": str(sess)})
        t0 = time.time()
        try:
            resp = bf.fetch(Request(url=base + "/slow"))
            ok = resp.status == 200 and "slow done" in resp.text
            dt = time.time() - t0
        except Exception as e:
            ok = False
            dt = time.time() - t0
            resp = None
            check("活跃渲染未中断", False, f"{type(e).__name__}: {e}")
        check("2s 慢页在 1.5s 空闲阈值下仍成功", ok and resp is not None and resp.text.strip(),
              f"dt={dt:.1f}s text={resp.text[:30] if resp else ''}")
        # 空闲后池退出 → 第二次请求自愈重启仍成功
        time.sleep(3)
        try:
            resp2 = bf.fetch(Request(url=base + "/slow"))
            check("池退出后自愈重启", resp2.status == 200 and "slow done" in resp2.text, resp2.text[:30])
        except Exception as e:
            check("池退出后自愈重启", False, f"{type(e).__name__}: {e}")
        bf.close()
    finally:
        if old_idle is None:
            os.environ.pop("US_POOL_IDLE_MS", None)
        else:
            os.environ["US_POOL_IDLE_MS"] = old_idle

    print("== #4 Checkpoint 有界保存 ==")
    cp_path = ROOT / "outputs" / ".test_tmp" / "cp_bounded.json"
    cp = Checkpoint(cp_path)
    rows = [{"id": i, "v": "x" * 20} for i in range(3000)]
    t0 = time.time()
    cp.save(rows, 3000, 3000)
    dt = time.time() - t0
    check("只保留最近 2000 条", len(cp.data.get("rows", [])) == 2000, str(len(cp.data.get("rows", []))))
    check("标记截断", cp.data.get("rows_truncated") is True)
    check("保存耗时可控", dt < 5, f"dt={dt:.2f}s")

    print("== #2 storage 写锁外（代码级）==")
    import universal_scraper.engine_v3 as ev3
    src = ev3.__file__ and Path(ev3.__file__).read_text(encoding="utf-8")
    i = src.find("self.storage.write(item)")
    prev = src[:i].rstrip("\n").splitlines()[-1].strip()
    check("storage.write 不在 with self._lock 内", prev != "with self._lock:", f"prev={prev}")

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
