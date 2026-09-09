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
    # 脏数据防线：跳过非 dict 行（jsonl 可能混入字符串/None），不因一行脏数据让复核崩溃
    rows = [r for r in (rows or []) if isinstance(r, dict)]
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

    # 2. 字段完整率（字段取前 200 行的并集；关键字段判 fail，稀疏字段仅提示）
    #    关键字段：标题/名称/链接/网址/日期/编号/文号/价格等——这些缺失=数据不可用
    #    稀疏字段：备注/说明/摘要/标签/工种等——天然允许部分为空，不误报"复核失败"
    _KEY_FIELDS = ("title", "name", "link", "url", "date", "time", "编号", "文号",
                   "标题", "名称", "链接", "网址", "日期", "时间", "价格", "金额",
                   "id", "code", "代码")
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
            is_key = any(k in f for k in _KEY_FIELDS)
            report["checks"].append({
                "name": f"字段完整率 · {f}",
                "pass": rate >= 0.9 if is_key else True,
                "value": f"{rate:.0%}（{n_ok}/{len(rows)}）"
                         + ("" if is_key else "（非关键字段，仅提示）"),
            })

    # 3. 去重率（主键优先级：url/link/id/shopId > 标题+链接复合 > 标题）
    #    修复：仅用 title 做主键会把"同标题不同内容"误判重复，必须复合链接/URL
    key = None
    for cand in ("url", "link", "id", "shopId", "链接", "网址"):
        if rows and _norm(rows[0].get(cand)):
            key = cand
            break
    if not key and rows and (_norm(rows[0].get("title")) or _norm(rows[0].get("标题"))):
        key = "title+link"  # 复合主键（兼容中英文）
    if not key and rows and (_norm(rows[0].get("name")) or _norm(rows[0].get("名称"))):
        key = "name+link"
    if key:
        seen = set()
        dups = 0
        empty = 0
        for r in rows:
            if key == "title+link":
                v = _norm(r.get("title") or r.get("标题")) + "|" + _norm(r.get("link") or r.get("url") or r.get("链接") or r.get("网址"))
            elif key == "name+link":
                v = _norm(r.get("name") or r.get("名称")) + "|" + _norm(r.get("link") or r.get("url") or r.get("链接") or r.get("网址"))
            else:
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
                # 判定分级：4xx/5xx（确定性死链）=失败；超时/连接错误(0)=警告（网络隔离/反爬，不等于数据错）；
                #           可达但标题不匹配=警告（JS渲染/PDF/动态标题常见）
                good = reachable
                warn = (st == 0) or (not good) or (reachable and match is False)
                if good and not warn:
                    ok += 1
                checked.append({"url": u[:80], "reachable": reachable,
                                "match": match, "pass": good, "warn": warn})
            except Exception as e:
                checked.append({"url": u[:80], "reachable": False,
                                "match": None, "pass": False, "err": str(e)[:60]})
        if total:
            report["checks"].append({
                "name": "抽样重抓对比",
                "pass": ok == total,
                "value": f"{ok}/{total} 可达" + ("（部分内容未匹配=警告）" if any(c.get("warn") for c in checked) else ""),
                "detail": checked,
            })

    report["ok"] = all(c.get("pass", True) for c in report["checks"])
    report["cost_ms"] = int((time.time() - t0) * 1000)
    return report


def verify_file(path: str, network: bool = False, data_key: str = "") -> Dict[str, Any]:
    from pathlib import Path
    fp = Path(path)
    if not fp.exists():
        return {"ok": False, "error": f"文件不存在: {path}"}
    try:
        data = json.loads(fp.read_text(encoding="utf-8-sig", errors="replace"))
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"JSON 解析失败（可能被截断）: {e}"}
    # 智联战例：支持 dict 包装（{"data": [...]} 等）——data_key 显式指定或自动探测常用键
    if isinstance(data, dict):
        if data_key:
            data = data.get(data_key, data)
        else:
            for k in ("data", "list", "rows", "items"):
                if isinstance(data.get(k), list):
                    data = data[k]
                    break
    rows = data if isinstance(data, list) else [data]
    return verify_rows(rows, None, sample_n=3, network=network)


# ---------------- 通用任务目录审计（batch1800~2200 战训：verify 此前只认自家
# config 产出的数据；异构批次（API/PDF/浏览器捕获混合）需要"任意任务目录"校验） ----------------
_EVIDENCE_PREFIX = ("evidence_", "capture_", "recon_")
_NON_DATA = {"report.md", "summary.json", "last_page.html", "recon_records.json"}


def verify_dir(path: str, log=print) -> Dict[str, Any]:
    """任意任务目录的通用审计（不要求由本工具 config 产出）：
    - 数据文件（*.json/*.csv，排除证据/快照命名）逐个：记录数、字段完整率
    - 证据清单：evidence_* / capture_* / summary.json 存在性
    - summary.json 存在时核对其中 evidence 引用的文件是否落盘
    返回 {files:[...], evidence:{...}, verdict}。verdict ∈ ok|partial|empty|no_data_files"""
    import csv as _csv
    root = Path(path).expanduser()
    if not root.exists() or not root.is_dir():
        return {"ok": False, "error": f"目录不存在: {path}"}
    files_out = []
    total_records = 0
    data_files = []
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        if f.name.startswith(_EVIDENCE_PREFIX) or f.name in _NON_DATA or f.suffix == ".tmp":
            continue
        if f.suffix.lower() in (".json", ".csv"):
            data_files.append(f)
    if not data_files:
        log("⚠️ 未发现数据文件（*.json/*.csv）")
    for f in data_files:
        entry: Dict[str, Any] = {"file": f.name}
        try:
            if f.suffix.lower() == ".json":
                vr = verify_file(str(f))
                if vr.get("error"):
                    raise ValueError(vr["error"])
                n = vr.get("total") or vr.get("records") or 0
                entry["records"] = int(n) if isinstance(n, (int, float)) else 0
                fields = vr.get("fields") or {}
                if fields:
                    rates = [v.get("rate", 0) if isinstance(v, dict) else v for v in fields.values()]
                    entry["field_complete_rate"] = round(sum(rates) / len(rates), 3)
                total_records += entry["records"]
            else:  # csv
                with f.open(encoding="utf-8-sig", newline="") as fh:
                    rows = list(_csv.DictReader(fh))
                entry["records"] = len(rows)
                total_records += len(rows)
                if rows:
                    filled = sum(1 for r in rows for v in r.values() if str(v or "").strip())
                    cells = sum(len(r) for r in rows) or 1
                    entry["field_complete_rate"] = round(filled / cells, 3)
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {str(e)[:80]}"
        files_out.append(entry)
    # 证据清单
    evidence = {
        "summary_json": (root / "summary.json").exists(),
        "report_md": (root / "report.md").exists(),
        "evidence_files": sorted(p.name for p in root.iterdir()
                                 if p.is_file() and p.name.startswith("evidence_")),
        "capture_files": sorted(p.name for p in root.iterdir()
                                if p.is_file() and p.name.startswith("capture_")),
    }
    # summary.json 引用的证据文件存在性
    summary = root / "summary.json"
    missing_ref = []
    if summary.exists():
        try:
            refs = json.loads(summary.read_text(encoding="utf-8")).get("evidence") or []
            missing_ref = [r for r in refs if not (root / str(r)).exists()]
        except Exception:
            pass
    has_data = any(f.get("records", 0) > 0 for f in files_out)
    if not data_files:
        verdict = "no_data_files"
    elif has_data and not missing_ref:
        verdict = "ok"
    elif has_data:
        verdict = "partial"
    else:
        verdict = "empty"
    result = {"dir": str(root), "files": files_out, "total_records": total_records,
              "evidence": evidence, "missing_evidence_refs": missing_ref,
              "verdict": verdict}
    log(f"🧾 目录审计 {root.name}: verdict={verdict}, 数据文件 {len(files_out)}, "
        f"记录 {total_records}, 证据 evidence_{len(evidence['evidence_files'])} 个")
    return result


if __name__ == "__main__":
    import sys
    sys.exit(0)
