#!/usr/bin/env python3
"""第三十五轮回归（4 个）：Review 14 修复——stdout 协议卫生。
用法: python3 tests/run_tests46.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


def main():
    print("== MCP stdout 协议卫生 ==")
    fetchers = (ROOT / "universal_scraper" / "modules" / "fetchers.py").read_text(encoding="utf-8")
    check("_notify 回退打 stderr", "print(msg, file=sys.stderr" in fetchers)
    antibot = (ROOT / "universal_scraper" / "antibot.py").read_text(encoding="utf-8")
    check("human_solve 打 stderr", 'file=_sys.stderr' in antibot)

    # 动态验证：BrowserFetcher._notify 无 log_cb 时不污染 stdout
    import io, contextlib
    from universal_scraper.modules.fetchers import BrowserFetcher
    buf = io.StringIO()
    bf = BrowserFetcher({"type": "browser", "url": "http://x/"}, {}, {})
    with contextlib.redirect_stdout(buf):
        bf._notify("hello-progress")
    check("_notify 不写 stdout", "hello-progress" not in buf.getvalue(), repr(buf.getvalue()[:50]))

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
