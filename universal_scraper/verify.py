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

from .core import safe_fname

META = {"_url", "_parser", "_ts", "_id", "_key"}


_NO_DATA_VALUES = {"--", "—", "-", "N/A", "n/a", "无", "暂无", "暂缺", "null", "None", ""}


def _norm(v: Any) -> str:
    # 审查修复：`v or ""` 会把合法数值 0/0.0 当空——全 0 数值列曾被误判"死列"
    return "" if v is None else str(v).strip()


def _is_no_data(v: Any) -> bool:
    """OCR R131 反馈 #3：`--`/`—`/`N/A` 等占位符是"来源确认无此项"而非"抽取失败"——
    不应拉低字段完整率。单列一个判定函数，与 _norm（是否非空）正交。"""
    return _norm(v) in _NO_DATA_VALUES


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
        # OCR R131（L）：裸 or 短路让 (declared is not None or cfg) 与三元组合
        # 出现优先级歧义——显式括号固化意图（cfg 有 output.base_name 优先）
        base = ((cfg.get("output") or {}).get("base_name") or cfg.get("name")) if cfg else None
        fp = Path("outputs") / f"{safe_fname(base)}.json" if base else None
        if fp and fp.exists():
            try:
                on_disk = json.loads(fp.read_text(encoding="utf-8"))
                n_disk = len(on_disk) if isinstance(on_disk, list) else 1
                if declared is not None:
                    expect = declared
                    same = n_disk == expect
                    report["checks"].append({
                        "name": "数量校验（声明 vs 文件）",
                        "pass": same,
                        "value": f"声明 {expect} vs 文件 {n_disk}",
                    })
                else:
                    # OCR R131（M）：未声明数量时曾拿文件数自比自证（恒 pass，
                    # 报告却写成"声明 N"）——如实标注口径
                    report["checks"].append({
                        "name": "数量校验（声明 vs 文件）",
                        "pass": True,
                        "value": f"未声明数量（文件实有 {n_disk} 条，仅记录）",
                    })
            except Exception as e:
                # OCR R131（H）：数量校验读文件失败曾被静默吞掉——检查项直接消失，
                # 报告 verdict 照常出 ok。必须显式落一条 fail
                report["checks"].append({
                    "name": "数量校验（声明 vs 文件）",
                    "pass": False,
                    "value": f"导出文件存在但无法读取/解析（{type(e).__name__}: {str(e)[:80]}）",
                })

    # 2. 字段完整率（字段取前 200 行的并集；关键字段判 fail，稀疏字段仅提示）
    #    关键字段：标题/名称/链接/网址/日期/编号/文号/价格/数值等——这些缺失=数据不可用
    #    稀疏字段：备注/说明/摘要/标签/工种等——天然允许部分为空，不误报"复核失败"
    _KEY_FIELDS = ("title", "name", "link", "url", "date", "time", "编号", "文号",
                   "标题", "名称", "链接", "网址", "日期", "时间", "价格", "金额",
                   "数值", "数量", "指数", "id", "code", "代码", "value")
    if rows:
        fields = []
        for r in rows[:200]:
            for k in r:
                # 审查修复 P1：跳过空键——CSV 多余列会被 DictReader 塞进 None 键，
                # `k in f` 对 None 抛 TypeError 让整个复核崩掉
                if k and k not in META and k not in fields:
                    fields.append(k)
        # 审查修复（P2，R7）：全空 dict 行曾零检查通过（all([])==True）——
        # 损坏导出被当 pass 恰是 verify 的存在意义，必须显式 fail
        if not fields:
            report["checks"].append({
                "name": "字段完整率",
                "pass": False,
                "value": f"所有 {len(rows)} 行均无业务字段（导出损坏/选择器全空）",
            })
        fields = fields[:12]
        for f in fields:
            n_ok = sum(1 for r in rows if _norm(r.get(f)))
            # OCR R131 反馈 #3：`--`/`N/A` 等"来源确认无此项"占位符不拉低完整率。
            # n_real = 剔除占位符后的真实有效率
            n_real = sum(1 for r in rows if _norm(r.get(f)) and not _is_no_data(r.get(f)))
            n_nodata = n_ok - n_real
            rate = n_ok / len(rows)
            real_rate = n_real / len(rows)
            is_key = any(k in f for k in _KEY_FIELDS)
            dead_col = (rate == 0 and len(rows) >= 3)
            passed = (rate >= 0.9) if is_key else (not dead_col)
            report["checks"].append({
                "name": f"字段完整率 · {f}",
                "pass": passed,
                "rate": round(rate, 3),
                "real_rate": round(real_rate, 3) if n_nodata else None,
                "value": f"{rate:.0%}（{n_ok}/{len(rows)}）"
                         + (f"（其中 {n_nodata} 条为 --/N/A 占位）" if n_nodata else "")
                         + ("（死列：抽取链路断裂）" if dead_col else "")
                         + ("" if is_key or dead_col else "（非关键字段，仅提示）"),
            })

    # 2.5 稀疏矩阵双口径（R102 汽车之家战报）：宽表大量选装列本来就该为空，
    # 单一完整率读数会误导。补报核心字段完整率（非稀疏字段）供交付判断
    if report["checks"] and rows:
        all_rates = [c["rate"] for c in report["checks"]
                     if c.get("name", "").startswith("字段完整率") and isinstance(c.get("rate"), (int, float))]
        if all_rates:
            overall = sum(all_rates) / len(all_rates)
            core_rates = [c["rate"] for c in report["checks"]
                          if c.get("name", "").startswith("字段完整率")
                          and any(k in c.get("name", "") for k in _KEY_FIELDS)]
            core_rate = sum(core_rates) / len(core_rates) if core_rates else overall
            sparse_n = len(all_rates) - len(core_rates)
            report["sparse_matrix"] = {
                "overall_rate": round(overall, 3),
                "core_rate": round(core_rate, 3),
                "sparse_fields": sparse_n,
                "total_fields": len(all_rates),
                "note": ("稀疏矩阵：大量选装/备注列为正常空值——交付判断请看核心字段完整率"
                         if sparse_n > len(all_rates) // 2 else ""),
            }

    # 2.7 缺口归因（OCR R131 反馈 #6：区分"来源确认无数据"和"疑似抽取失败"）
    #     来源确认无 = 值为 --/N/A/无 等占位符（来源方明确标注了"没有"）
    #     疑似抽取失败 = 值为空（None/""），可能是选择器漏了
    if rows and fields:
        _gaps = {"source_confirmed": [], "possible_extraction": []}
        for f in fields:
            total = len(rows)
            n_filled = sum(1 for r in rows if _norm(r.get(f)))
            n_nodata = sum(1 for r in rows if _norm(r.get(f)) and _is_no_data(r.get(f)))
            n_true_empty = total - n_filled  # _norm 为空（None/""）
            gap_n = n_nodata + n_true_empty
            if gap_n == 0 or total < 3:
                continue
            _is_key = any(k in f for k in _KEY_FIELDS)
            entry = {"field": f, "gap": gap_n, "total": total,
                     "gap_rate": round(gap_n / total, 3), "key": _is_key}
            if n_nodata >= n_true_empty:
                _gaps["source_confirmed"].append(entry)
            else:
                _gaps["possible_extraction"].append(entry)
        if _gaps["source_confirmed"] or _gaps["possible_extraction"]:
            report["gap_attribution"] = {
                "source_confirmed_absent": _gaps["source_confirmed"],
                "possible_extraction_gap": _gaps["possible_extraction"],
                "note": ("source_confirmed = 来源方以 --/N/A 等标注'此项无数据'（非抽取失败）；"
                         "possible_extraction = 字段真空值（需检查选择器是否漏了）")}

    # 3. 去重率（主键优先级：url/link/id/shopId > 标题+链接复合 > 标题）
    #    修复：仅用 title 做主键会把"同标题不同内容"误判重复，必须复合链接/URL
    #    OCR R131（H）：选键曾只看 rows[0]——首行恰好缺 url 时整批退化为
    #    复合键甚至跳过去重。扫前 50 行取"多数行都有值"的候选键
    _sample = rows[:50]
    key = None
    for cand in ("url", "link", "id", "shopId", "链接", "网址"):
        if _sample and sum(1 for r in _sample if _norm(r.get(cand))) >= max(1, len(_sample) // 2):
            key = cand
            break
    # 审查二轮（M）：复合键探测曾也只看 rows[0]——首行缺 title 时整批跳过
    # 去重。与上方候选键同口径：多数行有值才启用
    _has = lambda f: bool(_sample) and sum(
        1 for r in _sample if _norm(r.get(f))) >= max(1, len(_sample) // 2)
    if not key and (_has("title") or _has("标题")):
        key = "title+link"  # 复合主键（兼容中英文）
    if not key and (_has("name") or _has("名称")):
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
            # OCR R131（M）：复合键两段全空曾得 "|"（非空串）——同类行互相判重
            if not v or not v.replace("|", ""):
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
                # 审查修复（P1，R7）：警告级结果（超时/标题不匹配）曾把整体判成
                # FAIL——inline 注释自己说"不等于数据错"，计数口径与之矛盾
                hard = 400 <= st < 600
                if good and not warn:
                    ok += 1
                checked.append({"url": u[:80], "reachable": reachable,
                                "match": match, "pass": good, "warn": warn,
                                "hard_fail": hard})
            except Exception as e:
                checked.append({"url": u[:80], "reachable": False,
                                "match": None, "pass": False, "err": str(e)[:60],
                                "hard_fail": False})
        if total:
            _hard = sum(1 for c in checked if c.get("hard_fail"))
            report["checks"].append({
                "name": "抽样重抓对比",
                "pass": _hard == 0,
                "value": f"{ok}/{total} 可达" + ("（部分内容未匹配=警告）" if any(c.get("warn") for c in checked) else "")
                         + (f"｜{_hard} 条确定性死链" if _hard else ""),
                "detail": checked,
            })

    report["ok"] = all(c.get("pass", True) for c in report["checks"])
    report["cost_ms"] = int((time.time() - t0) * 1000)
    return report


def semantic_check(data: Any, expect: str) -> Dict[str, Any]:
    """语义校验（NBS 夜测战训：要"产量"给成"价格"也判了成功——完整率查不出语义错位）。

    expect 语法（逗号分隔词元，三种前缀）：
      `词`   = 任一命中即可（多个正词 OR，宽容口径）；
      `+词`  = 必须命中（多个 + 词 AND，严格口径——双指标任务防"丢一半"）；
      `!词`  = 必须不出现。
    例："消费价格,出厂价格" → 两词命中其一日 OK（但两指标任务应用 + 前缀！）
        "+消费价格,+出厂价格,!预测" → 两词都必须在，且不得出现"预测"。
    data 可为 list[dict]/dict/str——统一序列化成可搜索文本。
    返回含 semantic_ok / matched / required_missed / excluded_hit / expect，
    matched_any 标注 OR 组命中比例（n/m）。
    """
    if isinstance(data, str):
        text = data
    else:
        try:
            if isinstance(data, list):
                # 审查修复 P2：剥离 META 元数据键再序列化——_url 里恰含期望词时
                # 曾让 AND 组假通过/排除词假失败（语义校验只看业务字段）。
                # 审查修复（P2，R7）：非 dict 元素曾整行丢弃→cleaned=[] 假失败
                cleaned = [{k: v for k, v in row.items() if k not in META}
                           if isinstance(row, dict) else row
                           for row in data]
                text = json.dumps(cleaned, ensure_ascii=False, default=str)
            else:
                # 审查修复 P1-6：单记录 dict 同样剥 META（_url 含期望词曾假通过）
                text = json.dumps({k: v for k, v in data.items() if k not in META}
                                  if isinstance(data, dict) else data,
                                  ensure_ascii=False, default=str)
        except Exception:
            text = str(data)
    must_any, must_all, must_not = [], [], []
    for token in (expect or "").replace("，", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if token.startswith("!"):
            w = token[1:].strip()
            if w:
                must_not.append(w)
        elif token.startswith("+"):
            w = token[1:].strip()
            if w:
                must_all.append(w)
            # 审查修复 P1：裸 "+" 曾静默 no-op（词元为空不入任何组）且不触发告警
            else:
                must_all.append("\x00EMPTY")
        else:
            must_any.append(token)
    matched = [w for w in must_any if w and w in text]
    has_empty_required = "\x00EMPTY" in must_all
    required_missed = [w for w in must_all if w != "\x00EMPTY" and w not in text]
    if has_empty_required:
        required_missed.append("<空词元>")
    excluded_hit = [w for w in must_not if w and w in text]
    tokens_parsed = len(must_any) + len([w for w in must_all if w != "\x00EMPTY"]) + len(must_not)
    ok_any = bool(matched) if must_any else True
    ok_all = not required_missed
    ok_not = not excluded_hit
    semantic_ok = ok_any and ok_all and ok_not
    out = {
        "semantic_ok": semantic_ok,
        "matched": matched,
        "matched_any": f"{len(matched)}/{len(must_any)}" if must_any else "",
        "required_missed": required_missed,
        "excluded_hit": excluded_hit,
        "expect": expect,
    }
    if (expect or "").strip() and tokens_parsed == 0:
        out["expect_warning"] = "期望串非空但解析出 0 个有效词元（检查是否全为分隔符/裸前缀）"
    if must_any and must_all:
        out["note"] = "混用了任一词(OR)与必含词(AND)：任一词组不满足不计入失败，必含词缺一即败"
    return out


def verify_file(path: str, network: bool = False, data_key: str = "",
                expect: str = "", require: str = "",
                expected_count: Optional[int] = None) -> Dict[str, Any]:
    from pathlib import Path
    fp = Path(path)
    if not fp.exists():
        return {"ok": False, "error": f"文件不存在: {path}"}
    if fp.suffix.lower() == ".csv":
        # 审查修复 P2：JSON 存成 .csv 后缀的文件曾假通过（逗号少的 JSON 解析成
        # 垃圾表头 100% 填充）——首字符嗅探直接拦下
        head = fp.read_text(encoding="utf-8-sig", errors="replace").lstrip()[:1]
        if head in ("{", "["):
            return {"ok": False,
                    "error": "文件内容像 JSON 却用了 .csv 后缀——请改扩展名后重试"}
        # 国家数据考核反馈：考核命令形如 verify --file xxx.csv——此前只吃 JSON，
        # CSV 直接报"JSON 解析失败"。自动按 CSV 读成行记录。
        import csv as _csv
        try:
            with fp.open(encoding="utf-8-sig", newline="") as f:
                data = [dict(r) for r in _csv.DictReader(f)]
        except Exception as e:
            return {"ok": False, "error": f"CSV 解析失败: {e}"}
        if not data:
            return {"ok": False, "error": "CSV 无数据行"}
    else:
        try:
            data = json.loads(fp.read_text(encoding="utf-8-sig", errors="replace"))
        except json.JSONDecodeError:
            # 裁判文书网战训（UI 驱动采集没有配置产物，verify 此前无从下手）：
            # 兼容 JSONL——逐行 JSON 的流式采集产物
            try:
                lines = [ln for ln in
                         fp.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                         if ln.strip()]
                data = [json.loads(ln) for ln in lines]
            except json.JSONDecodeError as e2:
                return {"ok": False, "error": f"JSON/JSONL 均解析失败（可能被截断）: {e2}"}
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
    rep = verify_rows(rows, None, sample_n=3, network=network)
    # 对账型检查（实战反馈六#1，知乎 762 回复只采到 317 的教训）：字段完整率
    # 检测不出"源站声明 N 条、实采 M<N 条"的对账缺口。expected_count 由调用方
    # 传源站计数器（common_counts/total/child_comment_count）
    if expected_count is not None:
        actual = len([r for r in rows if isinstance(r, dict)])
        gap = expected_count - actual
        rep["checks"].append({
            "name": "对账（源站声明 vs 实采）",
            "pass": gap <= 0,
            "value": (f"声明 {expected_count} 条 vs 实采 {actual} 条"
                      + (f"，缺口 {gap} 条（{gap / max(1, expected_count):.0%}）——"
                         "检查翻页是否到底/展开是否齐全/是否被限流截断" if gap > 0 else "")),
        })
        rep["ok"] = bool(rep.get("ok")) and gap <= 0
    # 必需字段清单（裁判文书网战训）：显式点名"这份数据没有这些列就不算成品"，
    # 完整率 ≥90% 才过——比自动关键字段猜测更严格、更明确
    if require:
        req_fields = [w.strip() for w in require.replace("，", ",").split(",") if w.strip()]
        missing_checks = []
        for f in req_fields:
            n_ok = sum(1 for r in rows if isinstance(r, dict) and _norm(r.get(f)))
            rate = (n_ok / len(rows)) if rows else 0.0
            missing_checks.append({
                "name": f"必需字段 · {f}",
                "pass": rate >= 0.9,
                "value": f"{rate:.0%}（{n_ok}/{len(rows)}）",
            })
        rep["require_fields"] = missing_checks
        rep["ok"] = bool(rep.get("ok")) and all(c["pass"] for c in missing_checks)
    if expect:
        # 词元分流（审查修复 P1-3）：`列名!词`/`列名+词` 走列级断言；
        # 其余词元拼接后交给 semantic_check 全文匹配。此前列级词元原样流入
        # 全文匹配（整个 "列名!词" 串不可能出现在数据里）→ 列级用法必然假失败。
        tokens = [t.strip() for t in expect.replace("，", ",").split(",") if t.strip()]
        cols = set()
        for r in rows[:200]:
            if isinstance(r, dict):
                cols.update(k for k in r.keys() if k)
        col_specs, residual_tokens = [], []
        for t in tokens:
            claimed = False
            for sym in ("!", "+"):
                if sym in t:
                    col, _, word = t.partition(sym)
                    col, word = col.strip(), word.strip()
                    if col in cols and word:
                        col_specs.append({"col": col, "sym": sym, "word": word})
                        claimed = True
                    break
            if not claimed:
                residual_tokens.append(t)
        col_checks = evaluate_column_assertions(rows, col_specs)
        if col_checks:
            rep["column_assertions"] = col_checks
            rep["ok"] = bool(rep.get("ok")) and all(c["pass"] for c in col_checks)
        residual = ",".join(residual_tokens)
        if residual:
            sem = semantic_check(data, residual)
            rep["semantic"] = sem
            rep["ok"] = bool(rep.get("ok")) and sem["semantic_ok"]
    return rep


def evaluate_column_assertions(rows: List[Dict[str, Any]],
                               specs: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """执行列级断言。失败时定位前 3 个违规行（行号 1 起，含列值摘录）。"""
    clean = [(i, r) for i, r in enumerate(rows, 1) if isinstance(r, dict)]
    out: List[Dict[str, Any]] = []
    for spec in specs:
        col, sym, word = spec["col"], spec["sym"], spec["word"]
        if sym == "!":
            bad = [(i, r.get(col)) for i, r in clean if word in _norm(r.get(col))]
            desc = f"列「{col}」不得出现「{word}」"
        else:
            bad = [(i, r) for i, r in clean if word not in _norm(r.get(col))]
            desc = f"列「{col}」必须含「{word}」"
        passed = not bad
        out.append({
            "name": f"列级断言 · {desc}",
            "pass": passed,
            "value": (f"通过（{len(clean)} 行）" if passed
                      else f"{len(bad)} 行违规，首 3 处: " + "; ".join(
                          f"第{i}行[{col}={_norm(v)[:24]}]" for i, v in bad[:3])),
        })
    return out


# ---------------- 通用任务目录审计（batch1800~2200 战训：verify 此前只认自家
# config 产出的数据；异构批次（API/PDF/浏览器捕获混合）需要"任意任务目录"校验） ----------------
_EVIDENCE_PREFIX = ("evidence_", "capture_", "recon_")
# nodata.json/blocked.json：engine 零记录或被反爬拦截时写入的证据 dict，
# <out>.quality.json：PDF 表格质量元数据——计入都会复活"空爬假绿"
_NON_DATA = {"report.md", "summary.json", "last_page.html", "recon_records.json",
             "nodata.json", "blocked.json"}


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
        # R25 修复：引擎检查点 .state_*.json / .pending_*.json 与数据同目录且
        # rglob 会匹配点文件——曾被当数据记录审计（pending 队列长度=记录数），
        # 空爬+非空 pending 队列拿到假 verdict=ok / exit 0
        if f.name.startswith("."):
            continue
        if f.name.startswith(_EVIDENCE_PREFIX) or f.name in _NON_DATA \
                or f.suffix == ".tmp" or f.name.endswith(".quality.json"):
            continue
        if f.suffix.lower() in (".json", ".csv", ".jsonl"):
            data_files.append(f)
    if not data_files:
        log("⚠️ 未发现数据文件（*.json/*.jsonl/*.csv）")
    for f in data_files:
        entry: Dict[str, Any] = {"file": f.name}
        try:
            if f.suffix.lower() == ".json":
                vr = verify_file(str(f))
                if vr.get("error"):
                    raise ValueError(vr["error"])
                # OCR R131 反馈 #3：非列表 JSON（配置/单对象/证据文件）曾被当"1 条
                # 记录"计——产出 "1 条、完整率 0.75" 之类噪音。识别后标注、不计 records
                try:
                    _raw = json.loads(f.read_text(encoding="utf-8-sig"))
                    _is_list = isinstance(_raw, list)
                except Exception:
                    _is_list = True  # 解析失败走 verify_file 的正常错误路径
                n = vr.get("total") or vr.get("records") or 0
                if _is_list:
                    entry["records"] = int(n) if isinstance(n, (int, float)) else 0
                    total_records += entry["records"]
                else:
                    entry["records"] = 0
                    entry["note"] = "非列表 JSON（单对象/配置/证据）——不计入记录数"
                # R25 修复：字段完整率从 checks 里取名带"字段完整率 ·"的检查项
                # （verify_rows 在检查项上带数值 rate；旧的 "fields" 键从不存在）
                rates = [c["rate"] for c in vr.get("checks", [])
                         if c.get("name", "").startswith("字段完整率") and isinstance(c.get("rate"), (int, float))]
                if rates:
                    entry["field_complete_rate"] = round(sum(rates) / len(rates), 3)
                # OCR 终审（P1）：列表 JSON 的 records 已在 _is_list 分支累加（L526）——
                # 此处不重复累加（非列表 JSON records=0 加零无害但逻辑冗余）
            elif f.suffix.lower() == ".jsonl":
                # R102 战报落地：JSONL 自定义流水线产物也可审计（此前只认自家格式）
                n = 0
                filled = 0
                cells = 0
                bad_lines = 0  # 审查二轮（M）：坏行曾静默 continue——审计报告无从知晓
                with f.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            bad_lines += 1
                            continue
                        if not isinstance(r, dict):
                            bad_lines += 1
                            continue
                        n += 1
                        # OCR R131（H）：`v or ""` 把合法 0/0.0 当空——数值列完整率被低估
                        filled += sum(1 for v in r.values()
                                      if str("" if v is None else v).strip())
                        cells += len(r) or 1
                if bad_lines:
                    entry.setdefault("issues", []).append(
                        f"JSONL 有 {bad_lines} 行损坏/非对象（已跳过）——写入进程可能中断过")
                entry["records"] = n
                total_records += n
                if cells:
                    entry["field_complete_rate"] = round(filled / cells, 3)
            else:  # csv
                with f.open(encoding="utf-8-sig", newline="") as fh:
                    rows = list(_csv.DictReader(fh))
                entry["records"] = len(rows)
                total_records += len(rows)
                if rows:
                    filled = sum(1 for r in rows for v in r.values()
                                 if str("" if v is None else v).strip())
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
        except Exception as e:
            # OCR R131（H）：summary.json 损坏曾被静默当"无引用"→ verdict=ok。
            # 损坏 ≠ 通过：证据引用无法核实，按 partial 降级并说明原因
            missing_ref = [f"<summary.json 解析失败: {type(e).__name__}>"]
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
