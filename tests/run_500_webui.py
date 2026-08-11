#!/usr/bin/env python3
"""500 任务批量真跑（与 WebUI 完全同入口）：POST /api/auto/start → 轮询 /api/job。
用法:
  python3 tests/run_500_webui.py 601 610        # 跑 601-610
  python3 tests/run_500_webui.py --all          # 跑全部
  python3 tests/run_500_webui.py --resume       # 跳过已完成的
"""
import json, sys, time, urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8642"
TASKS = json.loads(Path("tests/tasks_601_1100.json").read_text(encoding="utf-8"))
RESULT = Path("tests/run500_results.json")
PER_TASK_TIMEOUT = 240   # 单任务最长 4 分钟

def api(path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE+path, data=data,
                                 headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def run_one(task):
    try:
        jid = api("/api/auto/start", {"description": task["desc"]})["job"]
    except Exception as e:
        return {"id": task["id"], "status": "start_fail", "summary": str(e)[:200]}
    t0 = time.time()
    while time.time() - t0 < PER_TASK_TIMEOUT:
        try:
            d = api(f"/api/job?job={jid}")
        except Exception:
            time.sleep(2); continue
        st = d.get("status")
        if st in ("done", "error"):
            summary = d.get("summary") or ""
            sol = d.get("solution") or {}
            if "成功" in summary and "0 条" not in summary:
                status = "SUCCESS"
            elif st == "error":
                status = "ERROR"
            else:
                status = "FAIL_0"
            return {"id": task["id"], "status": status,
                    "summary": summary[:200], "solution": sol.get("title", ""),
                    "action": bool(sol.get("action"))}
        # 检测"需人工验证/登录"→ 提前标记（不傻等满超时）
        msgs = " ".join(str(x) for x in (d.get("messages") or [])[-6:])
        if any(k in msgs for k in ("请在弹出的浏览器", "需要人工验证", "完成滑块", "请在弹出的窗口", "verify_required", "需要登录", "人工验证/登录")):
            return {"id": task["id"], "status": "NEEDS_HUMAN",
                    "summary": "需人工验证/登录（工具已给方案）", "solution": "", "action": True}
        time.sleep(3)
    return {"id": task["id"], "status": "TIMEOUT", "summary": "超时"}

def main():
    args = sys.argv[1:]
    results = {}
    if RESULT.exists():
        try: results = json.loads(RESULT.read_text(encoding="utf-8"))
        except Exception: results = {}
    if "--all" in args:
        todo = TASKS
    elif "--resume" in args:
        todo = [t for t in TASKS if str(t["id"]) not in results]
    else:
        lo = int(args[0]) if len(args) > 0 else 601
        hi = int(args[1]) if len(args) > 1 else lo
        todo = [t for t in TASKS if lo <= t["id"] <= hi and str(t["id"]) not in results]
    print(f"本轮待跑: {len(todo)} 个任务（并发 2）")
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(run_one, t): t for t in todo}
        done_i = 0
        for fut in as_completed(futs):
            done_i += 1
            t = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"id": t["id"], "status": "RUN_ERR", "summary": str(e)[:120]}
            results[str(t["id"])] = r
            RESULT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[{done_i}/{len(todo)}] #{t['id']} {r['status']} | {t['desc'][:38]} | {r['summary'][:60]}", flush=True)
    # 汇总
    from collections import Counter
    c = Counter(r.get("status") for r in results.values())
    print("\n=== 汇总 ===")
    for k, v in c.most_common():
        print(f"  {k}: {v}")
    print(f"总计: {len(results)}")

if __name__ == "__main__":
    main()
