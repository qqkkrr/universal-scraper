# -*- coding: utf-8 -*-
"""真实跑批：走 WebUI /api/auto/start（用户实际流程），并发 N，记录结果 JSONL。
用法: python3 tests/run_100_batch.py [--only public] [--workers 3] [--task 1160]
"""
import json, sys, time, argparse, urllib.request, threading, queue
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tasks_1101_1200 import PUBLIC, LOGIN_OR_ANTIBOT

BASE = "http://127.0.0.1:8642"
OUT = Path(__file__).resolve().parent / "batch_1101_1200_results.jsonl"
LOCK = threading.Lock()

def api(path, method="GET", payload=None, timeout=300):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def run_one(task):
    num, desc, min_rows = task
    rec = {"num": num, "desc": desc[:80], "status": "pending", "total": None,
           "summary": "", "error": "", "t_sec": 0}
    t0 = time.time()
    try:
        j = api("/api/auto/start", "POST", {"description": desc, "limit": 20, "rounds": _ROUNDS, "timeout": 200}, timeout=60)
        jid = j.get("job")
        if not jid:
            rec["status"] = "no_job"; rec["error"] = str(j)[:200]
            return rec
        while time.time() - t0 < _TIMEOUT:
            d = api("/api/job?job=" + jid, timeout=30)
            st = d.get("status")
            if st in ("done", "error"):
                res = d.get("result") or {}
                rec["status"] = "done" if st == "done" else "error"
                rec["total"] = (res or {}).get("total")
                rec["summary"] = (d.get("summary") or "")[:200]
                rec["error"] = (d.get("error") or "")[:200]
                rec["solution"] = (d.get("solution") or {}).get("title") if d.get("solution") else ""
                break
            time.sleep(5)
        else:
            rec["status"] = "timeout"
            # 超时则停止，避免残留
            try: api("/api/job/stop", "POST", {"job": jid}, timeout=15)
            except Exception: pass
    except Exception as e:
        rec["status"] = "exception"; rec["error"] = f"{type(e).__name__}: {e}"[:200]
    rec["t_sec"] = round(time.time() - t0, 1)
    with LOCK:
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[{num}] {rec['status']} total={rec['total']} {rec['t_sec']}s {desc[:40]} | {(rec['summary'] or rec['error'])[:60]}", flush=True)
    return rec

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--task", type=int, default=None, help="只跑指定编号")
    ap.add_argument("--probe", action="store_true", help="对需登录任务做快速探测分类")
    ap.add_argument("--timeout", type=int, default=600, help="单个任务总上限秒")
    ap.add_argument("--rounds", type=int, default=1)
    a = ap.parse_args()
    tasks = PUBLIC if not a.probe else LOGIN_OR_ANTIBOT
    if a.task:
        tasks = [t for t in tasks if t[0] == a.task]
    global _TIMEOUT, _ROUNDS
    _TIMEOUT = a.timeout
    _ROUNDS = a.rounds
    print(f"待跑 {len(tasks)} 个任务，并发 {a.workers}，单任务上限 {a.timeout}s")
    if OUT.exists(): OUT.unlink()
    q = queue.Queue()
    for t in tasks: q.put(t)
    results = []
    def worker():
        while True:
            try: t = q.get_nowait()
            except queue.Empty: return
            results.append(run_one(t))
    ths = [threading.Thread(target=worker) for _ in range(a.workers)]
    for th in ths: th.start()
    for th in ths: th.join()
    ok = [r for r in results if r["status"] == "done" and (r["total"] or 0) > 0]
    print(f"\n完成: {len(ok)}/{len(results)} 成功；结果已存 {OUT}")

if __name__ == "__main__":
    main()
