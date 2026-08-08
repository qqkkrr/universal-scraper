#!/usr/bin/env python3
"""100 任务 · 执行级测试器：跑 auto_task（小限额+短超时，不弹浏览器）。
用法: python3 tests/stress100/run_exec_tests.py --ids 56,98,58,57,61,34,37 --timeout 75 --limit 5
输出: tests/stress100/results_exec.json
"""
import argparse, json, sys, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from universal_scraper.auto import auto_task

BASE = Path(__file__).resolve().parent
TASKS = {t["id"]: t["desc"] for t in json.load(open(BASE / "tasks.json", encoding="utf-8"))}
RESULT_FILE = BASE / "results_exec.json"


def run_one(tid, desc, limit, timeout):
    t0 = time.time()
    try:
        r = auto_task(desc, limit=limit, rounds=1, round_timeout=timeout,
                      agent_fallback=False, log_cb=lambda m: None)
    except Exception as e:
        return {"id": tid, "ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                "secs": round(time.time()-t0, 1), "desc": desc[:80]}
    res = r.get("result") or {}
    sample = r.get("sample") or []
    return {
        "id": tid, "ok": bool(res.get("total")), "total": res.get("total", 0),
        "fetched": res.get("fetched", 0), "error": str(res.get("error") or "")[:200],
        "sample_n": len(sample),
        "first": (sample[0] if sample else {}),
        "secs": round(time.time()-t0, 1), "desc": desc[:80],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=75)
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()
    ids = [int(x) for x in args.ids.split(",")]
    results = json.load(open(RESULT_FILE, encoding="utf-8")) if RESULT_FILE.exists() else []
    done = {r["id"] for r in results}
    todo = [i for i in ids if i not in done and i in TASKS]
    print(f"执行测试 {len(todo)} 个任务（limit={args.limit}, timeout={args.timeout}s, workers={args.workers}）")

    def worker(ts):
        for tid in ts:
            rec = run_one(tid, TASKS[tid], args.limit, args.timeout)
            results.append(rec)
            mark = "✅" if rec["ok"] else "❌"
            print(f"{mark} #{tid:>3} total={rec.get('total')} {rec.get('secs')}s | {rec.get('desc','')[:50]} | err:{str(rec.get('error',''))[:60]}")
            json.dump(results, open(RESULT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    batches = [todo[i::args.workers] for i in range(args.workers)]
    threads = [threading.Thread(target=worker, args=(b,), daemon=True) for b in batches if b]
    for th in threads: th.start()
    for th in threads: th.join()
    ok = sum(1 for r in results if r.get("id") in ids and r.get("ok"))
    print(f"\n完成 {len(ids)} 个：成功 {ok}")


if __name__ == "__main__":
    main()
