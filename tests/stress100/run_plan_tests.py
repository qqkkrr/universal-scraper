#!/usr/bin/env python3
"""100 任务 · plan 级测试器：只跑 AI 理解+配置生成（不执行），评估入口/字段/日期正确性。
用法:
  python3 tests/stress100/run_plan_tests.py --range 1-10            # 跑 1~10 号
  python3 tests/stress100/run_plan_tests.py --range 1-100 --timeout 90
输出: tests/stress100/results_plan.json (增量保存)
"""
import argparse, json, os, sys, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from universal_scraper.auto import plan_task

BASE = Path(__file__).resolve().parent
TASKS = json.load(open(BASE / "tasks.json", encoding="utf-8"))
EXPECTED = json.load(open(BASE / "expected.json", encoding="utf-8"))
RESULT_FILE = BASE / "results_plan.json"


def host_of(url):
    from urllib.parse import urlparse
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def score(rec):
    """0-3 分：0=无法生成 1=入口错 2=入口对但配置缺关键字段 3=入口+字段合理"""
    if not rec.get("ok"):
        return 0
    su = (rec.get("start_url") or "")
    exp = EXPECTED.get(str(rec.get("id")), "")
    if not exp:
        return 3 if su else 0
    ok_entry = any(e in host_of(su) for e in exp.split("|"))
    fields = rec.get("fields") or []
    return (1 if ok_entry else 0) + (2 if ok_entry and len(fields) >= 3 else 0)


def run_one(t):
    t0 = time.time()
    try:
        r = plan_task(t["desc"], limit=5)
    except Exception as e:
        return {"id": t["id"], "ok": False, "error": f"{type(e).__name__}: {e}", "secs": round(time.time()-t0,1)}
    cfg = r.get("config") or {}
    src = cfg.get("source") or {}
    parsers = cfg.get("parsers") or {}
    fields = []
    for p in parsers.values():
        if isinstance(p, dict):
            fields.extend(list((p.get("fields") or {}).keys()))
    return {
        "id": t["id"], "ok": bool(r.get("ok")), "error": (r.get("error") or "")[:200],
        "start_url": (cfg.get("start_urls") or [""])[0] if cfg else "",
        "source": src.get("type", ""),
        "route": (r.get("route") or ""),
        "summary": (r.get("summary") or ""),
        "intent_target": ((cfg.get("intent") or {}).get("target_type") or ""),
        "entry_unknown": bool((cfg.get("intent") or {}).get("entry_unknown")),
        "fields": fields[:10],
        "has_verify": bool(((src.get("verify") or {}).get("enabled"))),
        "has_login": bool(((src.get("login") or {}).get("enabled"))),
        "has_date_filter": any((pl or {}).get("type") == "filter" and (pl or {}).get("op") == "between"
                               for pl in (cfg.get("pipelines") or [])),
        "detail_enabled": bool((cfg.get("detail") or {}).get("enabled")),
        "secs": round(time.time()-t0, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", default="1-10")
    ap.add_argument("--timeout", type=int, default=150)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    a, b = [int(x) for x in args.range.split("-")]
    targets = [t for t in TASKS if a <= t["id"] <= b]
    results = []
    if RESULT_FILE.exists():
        results = json.load(open(RESULT_FILE, encoding="utf-8"))
    done_ids = {r["id"] for r in results}
    todo = [t for t in targets if t["id"] not in done_ids]
    print(f"共 {len(todo)} 个待测（{a}-{b}），workers={args.workers}")

    def worker(ts):
        for t in ts:
            rec = run_one(t)
            results.append(rec)
            sc = score(rec)
            mark = "✅" if sc >= 3 else ("🟡" if sc >= 2 else ("🔴" if sc >= 1 else "⛔"))
            print(f"{mark} #{t['id']:>3} 分{sc} {rec['secs']:>5}s | {rec.get('start_url','')[:55]} | {rec.get('intent_target','')[:16]} | err:{str(rec.get('error',''))[:40]}")
            json.dump(results, open(RESULT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    batches = [todo[i::args.workers] for i in range(args.workers)]
    threads = [threading.Thread(target=worker, args=(bs,), daemon=True) for bs in batches if bs]
    for th in threads: th.start()
    for th in threads: th.join()
    print(f"\n完成 {len(results)} 条。按分数统计：")
    from collections import Counter
    c = Counter(score(r) for r in results if a <= r["id"] <= b)
    for k in [3,2,1,0]:
        print(f"  分{k}: {c.get(k,0)}")


if __name__ == "__main__":
    main()
