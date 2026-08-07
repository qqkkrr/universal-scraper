#!/usr/bin/env python3
"""第三十九轮回归（3 个）：前端 JS 语法守卫（Review 36 后续热修复）。
用法: python3 tests/run_tests50.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


def main():
    html = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    check("index.html 含脚本", m is not None)

    print("== 前端 JS 语法（node --check）==")
    js = m.group(1) if m else ""
    tmp = ROOT / "outputs" / ".test_tmp" / "index_script.js"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True, timeout=30)
    check("JS 语法通过", r.returncode == 0, r.stderr[:200])

    print("== 旧坏模式已消失 ==")
    check("无 stopJob('${...}') 坏模式", "stopJob('${" not in js and "stopJob(\'${" not in js)

    print("== stopJob 拼接正确 ==")
    check("stopJob 拼接", "stopJob(\\'" in js and "+esc(d.id)+" in js)

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
