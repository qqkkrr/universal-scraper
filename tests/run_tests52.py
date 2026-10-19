#!/usr/bin/env python3
"""第四十一轮回归（4 个）：懒加载/日期处理修复——
门卫阈值 800、parse_date 流水线。
用法: python3 tests/run_tests52.py
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
    print("== 门卫阈值 ==")
    cjs = (ROOT / "scripts" / "browser_generic.cjs").read_text(encoding="utf-8")
    check("阈值 800", "txtLen > 800" in cjs)

    print("== parse_date 流水线 ==")
    from universal_scraper.modules.pipelines import Pipeline
    now = 1785945600  # 2026-08-06 00:00 CST
    pipe = Pipeline([{"type": "parse_date", "field": "publish_date", "out": "ts", "now": now}], {})
    cases = {"2026-08-06": 1785945600, "今天": 1785945600, "昨天": 1785859200,
             "1天前": 1785859200, "3小时前": 1785934800, "未知文案": None}
    for v, want in cases.items():
        got = pipe.process({"publish_date": v}).get("ts")
        check(f"parse_date {v}", got == want, f"got={got} want={want}")

    print("== parse_date 注册 ==")
    import universal_scraper.config as cfg_mod
    check("parse_date 在流水线白名单", "parse_date" in cfg_mod.ALL_PIPELINE_TYPES)

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
