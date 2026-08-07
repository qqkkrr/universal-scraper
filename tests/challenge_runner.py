#!/usr/bin/env python3
"""100 挑战自动测试框架：进程内起靶场服务器，逐挑战用工具攻克，比对期望值。

用法:
  python3 tests/challenge_runner.py            # 跑全部已实现挑战
  python3 tests/challenge_runner.py 1 3 12     # 只跑指定编号
  python3 tests/challenge_runner.py --only-fail
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from challenges.server import make_server, CHALLENGES, HOST, PORT  # noqa: E402
import challenges.solutions as S  # noqa: E402


def start_server():
    srv = make_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    return srv


def run(only=None, only_fail=False, verbose=False):
    srv = start_server()
    results = {}
    try:
        ids = sorted(CHALLENGES.keys())
        if only:
            ids = [int(x) for x in only]
        passed = failed = 0
        for n in ids:
            if n not in CHALLENGES:
                print(f"#{n}: 未实现")
                continue
            expected = CHALLENGES[n]["expected"]
            solver = S.SOLUTIONS.get(n)
            if solver is None:
                print(f"#{n:>3} {CHALLENGES[n]['title']:<28} ⏭ 无解法")
                results[n] = {"status": "no_solution", "expected": expected}
                continue
            t0 = time.time()
            try:
                answer = solver()
                ok = bool(answer) and str(answer).strip() == str(expected).strip()
            except Exception as e:
                answer = f"ERR:{type(e).__name__}:{e}"
                ok = False
            dt = time.time() - t0
            status = "✅" if ok else "❌"
            if ok:
                passed += 1
            else:
                failed += 1
            results[n] = {"status": "pass" if ok else "fail", "expected": expected,
                          "answer": str(answer)[:200], "time": round(dt, 1)}
            if verbose or not ok or only:
                print(f"#{n:>3} {CHALLENGES[n]['title']:<28} {status} {dt:5.1f}s  期望={expected} 得到={str(answer)[:80]}")
        print(f"\n===== 结果: {passed}/{passed + failed} 通过 =====")
        if failed and not verbose:
            print("失败项:")
            for n in ids:
                if n in results and results[n]["status"] == "fail":
                    print(f"  #{n} {CHALLENGES[n]['title']}: 期望={results[n]['expected']} 得到={results[n]['answer'][:100]}")
    finally:
        srv.shutdown()
    out = ROOT / "challenges" / "results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告: {out}")
    return passed, failed


if __name__ == "__main__":
    args = sys.argv[1:]
    only = None
    only_fail = "--only-fail" in args
    nums = [a for a in args if a.isdigit()]
    if nums:
        only = nums
    run(only=only, only_fail=only_fail, verbose=True)
