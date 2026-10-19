#!/usr/bin/env python3
"""第三十四轮回归（6 个）：第十一轮 review 修复——
SessionPool 全局上限、LLM start_urls 上限、proxy_fetch 死源清理、core 命名常量。
用法: python3 tests/run_tests45.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


def main():
    print("== SessionPool 全局上限 ==")
    from universal_scraper.session import SessionPool
    sp = SessionPool(proxies=None, max_per_domain=3, max_total=6)
    for i in range(20):
        sp.acquire(f"http://domain-{i % 10}.com/")
    total = sum(len(v) for v in sp._pool.values())
    check("全局会话 ≤ 上限", total <= 6, f"total={total}")

    print("== LLM start_urls 上限 ==")
    from universal_scraper.auto import _validate_and_fix
    cfg = {"name": "t", "start_urls": [f"http://x/{i}" for i in range(25)],
           "source": {"type": "http"}, "storage": {"type": "jsonl"}, "output": {"dir": "outputs", "base_name": "t"}}
    out = _validate_and_fix(dict(cfg))
    check("start_urls 截到 10", len(out.get("start_urls", [])) == 10, str(len(out.get("start_urls", []))))

    print("== proxy_fetch 死源清理 ==")
    from universal_scraper.proxy_fetch import SOURCES
    names = [n for n, _ in SOURCES]
    check("仅保留可用源", names == ["geonode"], str(names))

    print("== core 命名常量 ==")
    import universal_scraper.core as c
    check("CACHE_DEFAULT_TTL 常量", getattr(c, "CACHE_DEFAULT_TTL", 0) == 86400.0)
    check("CACHE_MAX_FILES 常量", getattr(c, "CACHE_MAX_FILES", 0) == 2000)

    print("== SessionPool 全局上限回收不崩溃 ==")
    sp2 = SessionPool(proxies=None, max_per_domain=3, max_total=2)
    s1 = sp2.acquire("http://a.com/")
    s2 = sp2.acquire("http://b.com/")
    s3 = sp2.acquire("http://c.com/")  # 应回收最旧
    check("超限回收正常", s3 is not None and s3.domain == "c.com", s3.domain)

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
