#!/usr/bin/env python3
"""第四十轮回归（2 个）：登录门卫文本截断修复（500→5000，txtLen>2000 兜底生效）。
用法: python3 tests/run_tests51.py
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
    cjs = (ROOT / "scripts" / "browser_generic.cjs").read_text(encoding="utf-8")
    print("== 登录门卫文本截断 ==")
    check("轮询取 5000 字符", "innerText.slice(0, 5000)" in cjs)
    check("不再取 500 字符", "innerText.slice(0, 500)" not in cjs)
    check("兜底阈值仍 >2000", "txtLen > 2000" in cjs)

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
