#!/usr/bin/env python3
"""第三十六轮回归（4 个）：Review 18 修复——精配空壳行防线。
用法: python3 tests/run_tests47.py
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
    from universal_scraper.sites import parse_baike, run_site

    print("== parse_baike 错误码 ==")
    check("errno=2 返回 []", parse_baike('{"errno":2}', "http://x/") == [])
    check("正常卡返回行", len(parse_baike('{"title":"T","desc":"D","lemmaUrl":"http://x/","card":[]}', "http://x/")) == 1)
    check("无字段返回 []", parse_baike('{"foo":1}', "http://x/") == [])

    print("== run_site 空壳行防线 ==")
    r = run_site("https://baike.baidu.com/item/%E4%BA%AC%E4%B8%9C", limit=3)
    # 接口可能真失效或真返回；空壳行必须被拦截
    check("不返回全空字段成功行", not (r.get("total", 0) > 0 and all(not str(v or "").strip() for k, v in r.get("rows", [{}])[0].items() if k != "_site")),
          f"total={r.get('total')} err={r.get('error','')[:60]}")
    check("失败时有明确错误", r.get("total", 0) > 0 or ("error" in r and r["error"]), str(r)[:120])

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
