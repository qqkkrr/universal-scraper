#!/usr/bin/env python3
"""100/500 任务 · 执行级测试器（真跑，不只看配置）。

用法:
  python3 tests/stress100/run_exec_tests.py --ids 56,98,58,57 --limit 5 --timeout 75
    --reuse     复用已落盘配置（省 LLM，可反复修引擎后重跑）
    --headless  强制浏览器无头（批量测试不弹窗）
输出: tests/stress100/results_exec.json (增量)
"""
import argparse, json, os, sys, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from universal_scraper.auto import auto_task

BASE = Path(__file__).resolve().parent
RESULT_FILE = BASE / "results_exec.json"


def load_tasks(path=None):
    p = Path(path) if path else (BASE / "tasks.json")
    return {t["id"]: t["desc"] for t in json.load(open(p, encoding="utf-8"))}


def run_one(tid, desc, limit, timeout, rounds=1):
    t0 = time.time()
    try:
        r = auto_task(desc, limit=limit, rounds=rounds, round_timeout=timeout,
                      agent_fallback=False, log_cb=lambda m: None)
    except Exception as e:
        return {"id": tid, "ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}",
                "secs": round(time.time()-t0, 1), "desc": desc[:80], "kind": "exception"}
    res = r.get("result") or {}
    sample = r.get("sample") or []
    cfg = r.get("config") or {}
    src = cfg.get("source") or {}
    # 结果分类
    if res.get("total"):
        kind = "ok"
    elif (src.get("verify") or {}).get("enabled") or (src.get("login") or {}).get("enabled"):
        kind = "blocked_login"   # 需要人工登录/验证，批量环境无法完成（不是工具 bug）
    elif str(res.get("error") or ""):
        kind = "error"
    else:
        kind = "empty"
    return {
        "id": tid, "ok": bool(res.get("total")), "total": res.get("total", 0),
        "fetched": res.get("fetched", 0), "kind": kind,
        "error": str(res.get("error") or "")[:300], "sample_n": len(sample),
        "source": src.get("type", ""), "first": (sample[0] if sample else {}),
        "secs": round(time.time()-t0, 1), "desc": desc[:80],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True)
    ap.add_argument("--tasks", default="", help="任务文件路径（默认 tasks.json）")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=75)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--force", action="store_true", help="覆盖已有结果重跑")
    args = ap.parse_args()
    if args.reuse:
        os.environ["US_REUSE_CONFIG"] = "1"
    if args.headless:
        os.environ["US_HEADLESS"] = "1"
    TASKS = load_tasks(args.tasks)
    ids = []
    for _part in args.ids.split(","):
        if "-" in _part:
            _a, _b = _part.split("-", 1)
            ids += list(range(int(_a), int(_b) + 1))
        else:
            ids.append(int(_part))
    results = json.load(open(RESULT_FILE, encoding="utf-8")) if RESULT_FILE.exists() else []
    done = {r["id"] for r in results} if not args.force else set()
    todo = [i for i in ids if i not in done and i in TASKS]
    print(f"执行测试 {len(todo)} 个任务（limit={args.limit}, timeout={args.timeout}s, "
          f"workers={args.workers}, reuse={bool(args.reuse)}, headless={bool(args.headless)}）")

    def worker(ts):
        for tid in ts:
            rec = run_one(tid, TASKS[tid], args.limit, args.timeout, args.rounds)
            results[:] = [x for x in results if x["id"] != tid]
            results.append(rec)
            mark = {"ok": "✅", "blocked_login": "🔒", "error": "❌", "empty": "⚠️", "exception": "💥"}.get(rec["kind"], "?")
            print(f"{mark} #{tid:>3} {rec['kind']:<13} total={rec.get('total')} {rec.get('secs')}s | {rec.get('desc','')[:44]} | err:{str(rec.get('error',''))[:50]}")
            json.dump(results, open(RESULT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    batches = [todo[i::args.workers] for i in range(args.workers)]
    threads = [threading.Thread(target=worker, args=(b,), daemon=True) for b in batches if b]
    for th in threads: th.start()
    for th in threads: th.join()
    from collections import Counter
    c = Counter(x.get("kind") for x in results if x["id"] in ids)
    print(f"\n完成 {len(todo)}：{dict(c)}")


if __name__ == "__main__":
    main()
