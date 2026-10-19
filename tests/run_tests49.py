#!/usr/bin/env python3
"""第三十八轮回归（3 个）：Review 31 修复——GitHub 精配修正为 api.github.com/search。
用法: python3 tests/run_tests49.py
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
    from universal_scraper.sites import match_github, parse_github, run_site, SITES

    print("== GitHub 精配修正 ==")
    check("匹配 api.github.com/search", match_github("https://api.github.com/search/repositories?q=x"))
    check("不匹配 HTML 搜索页", not match_github("https://github.com/search?q=x"))
    rows = parse_github('{"items":[{"full_name":"a/b","html_url":"http://x/","description":"d","stargazers_count":1}]}', "u")
    check("JSON 解析出仓库", len(rows) == 1 and rows[0]["repo"] == "a/b", str(rows))
    r = run_site("https://api.github.com/search/repositories?q=requests&per_page=2", limit=2)
    check("run_site 实测返回真实仓库", r.get("total", 0) > 0 and any("requests" in str(x.get("repo","")).lower() for x in r.get("rows", [])),
          f"total={r.get('total')} err={r.get('error','')[:60]}")

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
