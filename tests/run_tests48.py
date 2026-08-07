#!/usr/bin/env python3
"""第三十七轮回归（2 个）：Review 23 修复——WebUI 启动回归（os 导入丢失的 P1）。
用法: python3 tests/run_tests48.py
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    print("== WebUI 启动回归 ==")
    import universal_scraper.webui as w
    check("webui 模块导入", callable(getattr(w, "serve", None)))

    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "universal_scraper.cli", "webui",
                             "--no-open", "--port", str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=str(ROOT))
    ok = False
    err = ""
    for _ in range(40):
        if proc.poll() is not None:
            err = proc.stdout.read() if proc.stdout else ""
            break
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                ok = r.status == 200
                break
        except Exception:
            time.sleep(0.5)
    check("WebUI 能启动并响应 /", ok, err[-300:] if err else "")
    if proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=5)

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
