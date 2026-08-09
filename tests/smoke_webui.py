#!/usr/bin/env python3
"""🩺 WebUI 等价回归测试：用与使用者完全相同的 API 入口跑任务并验证。

为什么要有它：之前"我跑过"是直接调精配函数/直连，绕过了用户必经的
「一句话任务→AI生成配置→引擎→验证」环节，导致用户实测总出问题。
本脚本 100% 走 WebUI HTTP API（/api/paste/start、/api/auto/start），
任何我声称"跑过"的任务都必须在这里留痕（tests/smoke_report.md）。

用法:
  python3 tests/smoke_webui.py                 # 跑全部场景
  python3 tests/smoke_webui.py zige eq         # 只跑指定场景
  python3 tests/smoke_webui.py --rebuild       # 强制重跑（不跳过已通过）
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8642"
REPORT = Path(__file__).resolve().parent / "smoke_report.md"
MIN_NONEMPTY_RATE = 0.5   # 字段非空率下限（防"空壳当成功"）
MIN_ROWS = 1

# 场景定义：(id, 名称, 类型, 参数, 预期行数下限, 预期关键词)
# 类型 paste = 贴网址爬虫；auto = 一句话任务（AI 全流程，最接近用户实测）
SCENARIOS = [
    {
        "id": "zige_paste",
        "name": "人社部职业资格目录·贴公告页",
        "type": "paste",
        "url": "https://www.gov.cn/zhengce/zhengceku/2021-12/03/content_5655553.htm",
        "min_rows": 90,
        "kw": ["职业资格", "准入类"],
    },
    {
        "id": "zige_auto",
        "name": "人社部职业资格目录·一句话任务（描述路由）",
        "type": "auto",
        "desc": "抓取国家职业资格目录的所有职业名称和资格类别",
        "min_rows": 90,
        "kw": ["职业资格", "准入类"],
    },
    {
        "id": "eq_auto",
        "name": "最近24小时中国地震·一句话任务（描述路由）",
        "type": "auto",
        "desc": "抓最近24小时中国地震的震级、地点、震源深度",
        "min_rows": 1,
        "kw": ["震级"],
    },
]


def api(path: str, body: dict | None = None, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_job(jid: str, timeout: float = 600) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = api(f"/api/job?job={jid}")
        if d.get("status") in ("done", "error", "interrupted"):
            return d
        time.sleep(2)
    return {"status": "timeout", "error": "等待超时"}


def check_rows(rows, min_rows, kw) -> dict:
    if not rows:
        return {"ok": False, "msg": "0 行"}
    # 字段非空率
    meta = ("_url", "_parser", "_ts", "_id", "_pdf", "_page", "_table")
    rates = []
    for r in rows[:20]:
        vals = [str(v or "").strip() for k, v in r.items() if k not in meta]
        if vals:
            rates.append(sum(1 for v in vals if v) / len(vals))
    avg_rate = sum(rates) / len(rates) if rates else 0
    text = json.dumps(rows[:20], ensure_ascii=False)
    hit = all(k in text for k in kw)
    ok = len(rows) >= min_rows and avg_rate >= MIN_NONEMPTY_RATE and hit
    return {"ok": ok, "msg": f"{len(rows)} 行 | 非空率 {avg_rate:.0%} | 关键词{'✓' if hit else '✗'} | 要求 ≥{min_rows} 行 / 非空率 ≥{MIN_NONEMPTY_RATE:.0%}"}


def locate_output(sc: dict, d: dict) -> Path:
    """定位导出 JSON：job.result.files > summary 路径 > auto 描述 hash > 最近 auto_*.json。"""
    import re as _re
    import hashlib as _hl
    root = Path(__file__).resolve().parent.parent
    files = (d.get("result") or {}).get("files") or {}
    if isinstance(files, dict):
        jp = files.get("json")
        if jp:
            p = root / jp if not str(jp).startswith("/") else Path(jp)
            if p.exists():
                return p
    summary = d.get("summary") or ""
    for m in _re.finditer(r"outputs/[^'\"\s,]+\\.json", summary):
        p = root / m.group(0)
        if p.exists():
            return p
    desc = sc.get("desc", "")
    if desc:
        h = _hl.md5(desc.encode()).hexdigest()[:10]
        p = root / "outputs" / f"auto_{h}.json"
        if p.exists():
            return p
    cand = []
    od = root / "outputs"
    if od.exists():
        for f in od.glob("auto_*.json"):
            if f.name.startswith("auto_") and "precise" not in f.name:
                try:
                    if time.time() - f.stat().st_mtime < 600:
                        cand.append(f)
                except Exception:
                    pass
    if cand:
        return max(cand, key=lambda f: f.stat().st_mtime)
    return Path("")


def run_scenario(sc: dict) -> dict:
    if sc["type"] == "paste":
        jid = api("/api/paste/start", {"url": sc["url"], "mode": "auto"})["job"]
    else:
        jid = api("/api/auto/start", {"description": sc["desc"]})["job"]
    d = wait_job(jid)
    status = d.get("status")
    if status != "done":
        return {"ok": False, "msg": f"status={status} err={str(d.get('error') or '')[:120]}",
                "summary": d.get("summary"), "job": jid}
    fp = locate_output(sc, d)
    rows = []
    if fp and fp.exists():
        try:
            rows = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            rows = []
    if not rows:
        return {"ok": False, "msg": "summary 成功但导出 JSON 缺失/为空（假成功！）",
                "summary": (d.get("summary") or "")[:200], "job": jid, "fp": str(fp)}
    chk = check_rows(rows, sc.get("min_rows", 1), sc.get("kw", []))
    return {"ok": chk["ok"], "msg": chk["msg"], "summary": (d.get("summary") or "")[:120],
            "job": jid, "rows": len(rows), "fp": str(fp)}


def main():
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    results = []
    for sc in SCENARIOS:
        if only and sc["id"] not in only:
            continue
        print(f"▶️  {sc['name']} ...", flush=True)
        t0 = time.time()
        try:
            r = run_scenario(sc)
        except Exception as e:
            r = {"ok": False, "msg": f"异常 {type(e).__name__}: {e}"}
        r["id"] = sc["id"]; r["name"] = sc["name"]; r["secs"] = round(time.time() - t0)
        results.append(r)
        print(f"   {'✅' if r['ok'] else '❌'} {r['msg']}（{r['secs']}s）", flush=True)
    # 报告
    lines = ["# 🩺 WebUI 等价回归报告", "",
             f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}｜入口：与 WebUI 完全一致的 HTTP API",
             f"> 判定标准：status=done + 导出文件存在 + 行数≥阈值 + 字段非空率≥{MIN_NONEMPTY_RATE:.0%} + 关键词命中",
             "", "| 场景 | 结果 | 详情 | 耗时 |", "|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['name']} | {'✅' if r['ok'] else '❌'} | {r['msg']} | {r['secs']}s |")
    ok_n = sum(1 for r in results if r["ok"])
    lines += ["", f"**通过 {ok_n}/{len(results)}**"]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n📄 报告已写入: {REPORT}")
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
