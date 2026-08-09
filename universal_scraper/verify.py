#!/usr/bin/env python3
"""🧾 复核 / 检查模块：抓完不等于抓对。

对抓取结果做 4 项独立检查，输出机器可读报告：
  1. 数量          —— 声明条数 vs 实际条数
  2. 字段完整率    —— 每个业务字段非空比例（≥90% 通过）
  3. 去重率        —— 按 url/link/id 主键查重复
  4. 抽样重抓对比  —— 抽前 N 条重新请求源网页，标题出现在正文即视为一致（可选 network=True）

用法:
  CLI:  python3 -m universal_scraper.cli verify --file outputs/xxx.json [--network]
  API:  GET /api/verify?file=xxx.json
  auto 结束后自动跑一次轻量复核（不联网），结果放 report.verify
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

META = {"_url", "_parser", "_ts", "_id", "_key"}


def _norm(v: Any) -> str:
    return str(v or "").strip()


def verify_rows(rows: List[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None,
                sample_n: int = 3, network: bool = False, timeout: int = 15,
                declared: Optional[int] = None) -> Dict[str, Any]:
    """对 rows 复核，返回 {ok, total, checks:[...], ts}。
    declared: 运行器声明的总条数（auto 传 result.total），用于与导出文件对比。"""
    t0 = time.time()
    report: Dict[str, Any] = {"ok": False, "total": len(rows), "checks": [], "ts": time.time()}
    if not rows:
        report["message"] = "0 条数据，无需复核"
        report["ok"] = False
        return report

    # 1. 数量校验（声明条数 vs 导出文件实际条数）
    if declared is not None or cfg:
        base = (cfg.get("output") or {}).get("base_name") or cfg.get("name") if cfg else None
        fp = Path("outputs") / f"{base}.json" if base else None
        if fp and fp.exists():
            try:
                on_disk = json.loads(fp.read_text(encoding="utf-8"))
                n_disk = len(on_disk) if isinstance(on_disk, list) else 1
                expect = declared if declared is not None else n_disk
                same = n_disk == expect
                report["checks"].append({
                    "name": "数量校验（声明 vs 文件）",
                    "pass": same,
                    "value": f"声明 {expect} vs 文件 {n_disk}",
                })
            except Exception:
                pass

    # 2. 字段完整率（字段取前 200 行的并集，避免只看第一行漏掉后续新字段）
    if rows:
        fields = []
        for r in rows[:200]:
            for k in r:
                if k not in META and k not in fields:
                    fields.append(k)
        fields = fields[:12]
        for f in fields:
            n_ok = sum(1 for r in rows if _norm(r.get(f)))
            rate = n_ok / len(rows)
            report["checks"].append({
                "name": f"字段完整率 · {f}",
                "pass": rate >= 0.9,
                "value": f"{rate:.0%}（{n_ok}/{len(rows)}）",
            })

    # 3. 去重率（主键：url > link > id > shopId > 第一个字段）
    key = None
    for cand in ("url", "link", "id", "shopId", "name", "title"):
        if rows and _norm(rows[0].get(cand)):
            key = cand
            break
    if key:
        seen = set()
        dups = 0
        empty = 0
        for r in rows:
            v = _norm(r.get(key))
            if not v:
                empty += 1
            elif v in seen:
                dups += 1
            else:
                seen.add(v)
        report["checks"].append({
            "name": f"去重率 · 按 {key}",
            "pass": dups == 0,
            "value": f"{dups} 条重复" + (f"，{empty} 条主键为空" if empty else ""),
        })

    # 4. 抽样重抓对比（联网）：抓到的详情链接要能在网上真正打开且内容匹配
    if network and sample_n > 0:
        # 相对链接补全：入口 start_urls[0] 的 scheme://host 作为基址
        base_url = ""
        try:
            su = (cfg or {}).get("start_urls") or []
            if su:
                from urllib.parse import urlparse
                _p = urlparse(su[0])
                base_url = f"{_p.scheme}://{_p.netloc}"
        except Exception:
            pass
        ok = total = 0
        checked = []
        for r in rows[:sample_n]:
            # 支持中英文键名（url/link/链接/网址/详情链接；title/name/标题/名称）
            u = _norm(r.get("url") or r.get("link") or r.get("链接")
                      or r.get("网址") or r.get("详情链接") or "")
            if u.startswith("/") and base_url:
                u = base_url + u
            if not u or not u.startswith(("http://", "https://")):
                continue
            total += 1
            title = _norm(r.get("title") or r.get("name") or r.get("标题") or r.get("名称") or "")
            try:
                from .quick import fetch_url
                fr = fetch_url(u, timeout=timeout, article=True)
                st = fr.get("status") or 0
                body = _norm(fr.get("article") or fr.get("markdown") or "")
                reachable = 200 <= st < 400
                if not title or not body:
                    match = None  # 无法判断
                else:
                    match = title[:12] in body
                good = reachable and match is not False
                if good:
                    ok += 1
                checked.append({"url": u[:80], "reachable": reachable,
                                "match": match, "pass": good})
            except Exception as e:
                checked.append({"url": u[:80], "reachable": False,
                                "match": None, "pass": False, "err": str(e)[:60]})
        if total:
            report["checks"].append({
                "name": "抽样重抓对比",
                "pass": ok == total,
                "value": f"{ok}/{total} 一致",
                "detail": checked,
            })

    report["ok"] = all(c.get("pass", True) for c in report["checks"])
    report["cost_ms"] = int((time.time() - t0) * 1000)
    return report


def verify_file(path: str, network: bool = False) -> Dict[str, Any]:
    from pathlib import Path
    fp = Path(path)
    if not fp.exists():
        return {"ok": False, "error": f"文件不存在: {path}"}
    data = json.loads(fp.read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else [data]
    return verify_rows(rows, None, sample_n=3, network=network)


if __name__ == "__main__":
    import sys
    sys.exit(0)
