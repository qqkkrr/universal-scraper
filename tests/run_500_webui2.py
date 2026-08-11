#!/usr/bin/env python3
"""500 任务批量真跑 v2：并发提交 + 全局轮询（适配长任务），超时停止。
用法: python3 tests/run_500_webui2.py 621 660   # 跑一段
"""
import json, sys, time, urllib.request, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

BASE = "http://127.0.0.1:8642"
TASKS = json.loads(Path("tests/tasks_601_1100.json").read_text(encoding="utf-8"))
RESULT = Path("tests/run500_results.json")
HARD_TIMEOUT = 600   # 10 分钟
POLL = 20

def api(path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE+path, data=data, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def start_one(task):
    try:
        return api("/api/auto/start", {"description": task["desc"]})["job"]
    except Exception as e:
        return None

def judge(task, d):
    summary = d.get("summary") or ""
    sol = d.get("solution") or {}
    if "成功" in summary and "0 条" not in summary:
        return "SUCCESS"
    if d.get("status") == "error":
        return "ERROR"
    msgs = " ".join(str(x) for x in (d.get("messages") or [])[-6:])
    if any(k in msgs for k in ("请在弹出的浏览器", "需要人工验证", "完成滑块", "verify_required",
                                "需要登录", "人工验证/登录", "请在弹出的窗口", "需要浏览器渲染/登录",
                                "被拦截", "反爬", "验证码")):
        return "NEEDS_HUMAN"
    return "FAIL_0"

def main():
    args = sys.argv[1:]
    results = {}
    if RESULT.exists():
        try: results = json.loads(RESULT.read_text(encoding="utf-8"))
        except Exception: results = {}
    if "--all" in args:
        todo = [t for t in TASKS if str(t["id"]) not in results]
    else:
        lo = int(args[0]) if len(args) > 0 else 601
        hi = int(args[1]) if len(args) > 1 else lo
        todo = [t for t in TASKS if lo <= t["id"] <= hi and str(t["id"]) not in results]
    print(f"本轮待跑: {len(todo)}（并发 3，硬超时 10 分钟）")
    running = {}   # job_id -> (task, started_at)
    idx = 0
    lock = threading.Lock()
    def submit_more():
        nonlocal idx
        while idx < len(todo) and len(running) < 3:
            t = todo[idx]; idx += 1
            jid = start_one(t)
            if jid:
                running[jid] = (t, time.time())
                print(f"▶ #{t['id']} 已提交（{t['desc'][:30]}）", flush=True)
            else:
                results[str(t["id"])] = {"id": t["id"], "status": "START_FAIL", "summary": "提交失败"}
    submit_more()
    while running:
        time.sleep(POLL)
        finished = []
        for jid, (t, t0) in list(running.items()):
            try:
                d = api(f"/api/job?job={jid}")
            except Exception:
                continue
            st = d.get("status")
            if st in ("done", "error"):
                st2 = judge(t, d)
                results[str(t["id"])] = {"id": t["id"], "status": st2,
                    "summary": (d.get("summary") or "")[:200],
                    "solution": (d.get("solution") or {}).get("title", ""),
                    "action": bool((d.get("solution") or {}).get("action"))}
                finished.append(jid)
                print(f"✓ #{t['id']} {st2} | {(d.get('summary') or '')[:70]}", flush=True)
            elif time.time() - t0 > HARD_TIMEOUT:
                try: api("/api/job/stop", {"job": jid})
                except Exception: pass
                results[str(t["id"])] = {"id": t["id"], "status": "TIMEOUT",
                    "summary": "超时(10分钟)已停止，工具未快速识别反爬"}
                finished.append(jid)
                print(f"✗ #{t['id']} TIMEOUT（已停止）", flush=True)
        for jid in finished:
            running.pop(jid, None)
        RESULT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        submit_more()
    # 汇总
    from collections import Counter
    c = Counter(r.get("status") for r in results.values())
    print("\n=== 汇总 ===")
    for k, v in c.most_common():
        print(f"  {k}: {v}")
    print(f"总计: {len(results)}")

if __name__ == "__main__":
    main()
