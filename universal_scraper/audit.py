#!/usr/bin/env python3
"""交付审计电池（四轮深检的引擎化，2026-09-26）。

把 2026-09-25 科研级审计验证过的检查固化为一条命令，采集完成时的内置出口检查：
  panel   面板 xlsx 全量电池：唯一键/窗口/freq 三公式/年度-标题对齐/文本源覆盖/抽验重算/文档-数据一致
  urls    来源 URL 全量在线可达性 + 根路径引用检测
  verbatim 文本逐字回源（条款库类：clause_text ↔ 源文件全量比对）

审计认识论（四轮换来的，检查项按 维度×字段 矩阵推进，勿删）：
- "freq 公式自洽"不能证明跨提取管线一致——单管线复算才是硬验
- "逐字回源通过"≠无重复伪影（页眉也是原文）——须查文档内唯一性
- 数据正确≠引用正确——URL 可达与引用-声明对齐是独立维度
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import List

_ROOT_URL = re.compile(r"^[hH][tT][tT][pP][sS]?://[^/?#]+[/?#]*$")


def _norm(s) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def _safe_int(v, default):
    """残缺单元格/浮点串安全转 int：失败返回 default（审计器输入恰是可能残缺的交付物）。"""
    try:
        return int(float(str(v)))
    except (TypeError, ValueError):
        return default


def _safe_float(v, default):
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return default


def audit_panel(xlsx: str, texts_dir: str, universe_csv: str = "",
                year_from: int = 2010, year_to: int = 2024,
                sample: int = 12, keywords: dict = None) -> List[str]:
    """面板 xlsx 全量审计。返回问题清单（空=通过）。

    keywords 传入研究词典时，抽验从"仅验 total_chars"升级为全字段关键词重算
    （count 列 × freq 公式 × 档案文本三方一致——科研复现性的硬验）。"""
    from openpyxl import load_workbook
    issues: List[str] = []
    try:
        wb = load_workbook(xlsx, read_only=True)
    except Exception as e:
        return [f"xlsx 读取失败: {type(e).__name__}: {e}"]
    if not wb.sheetnames:
        return ["xlsx 无任何工作表"]
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return ["首个工作表为空（无表头行）"]
    h = list(rows[0])
    H = {c: i for i, c in enumerate(h)}
    t = rows[1:]
    if not t:
        return ["首个工作表无数据行"]
    if not any("stkcd" in str(c or "") for c in h):
        return [f"表头缺 stkcd 列: {h[:6]}"]
    if "stkcd" not in H:
        return [f"stkcd 仅以变体形式存在（如带空格），无精确列名: {[c for c in h if 'stkcd' in str(c)][:3]}"]
    if "year" not in H:
        return [f"表头缺 year 列: {h[:8]}"]

    # 1) 唯一键
    keys = [(r[H["stkcd"]], r[H["year"]]) for r in t]
    if len(keys) != len(set(keys)):
        dup = [k for k, n in Counter(keys).items() if n > 1]
        issues.append(f"重复键 {len(dup)} 组: {dup[:5]}")

    # 2) 代码规范 + 年份窗（残缺单元格转 issue，不让审计器自身崩溃）
    bad_code = [r[H["stkcd"]] for r in t if not re.match(r"^\d{6}$", str(r[H["stkcd"]] or ""))]
    if bad_code:
        issues.append(f"非6位代码 {len(bad_code)}: {bad_code[:5]}")
    bad_year = []
    for r in t:
        y = _safe_int(r[H["year"]], None)
        if y is None or not (year_from <= y <= year_to):
            bad_year.append(r[H["year"]])
    if bad_year:
        issues.append(f"年份越界/非法 {len(bad_year)}: {bad_year[:5]}")

    # 3) 清单窗口（提供 universe 时；年份残缺的行归入违例而非崩溃）
    if universe_csv:
        import csv
        win = {}
        try:
            universe_rows = list(csv.DictReader(open(universe_csv, encoding="utf-8-sig")))
        except Exception as e:
            universe_rows = []
            issues.append(f"清单 CSV 读取失败（窗口检查跳过）: {type(e).__name__}: {e}")
        for r in universe_rows:
            code = "".join(ch for ch in str(r.get("stkcd", "")) if ch.isdigit()).zfill(6)
            win[code] = (_safe_int(r.get("first_year"), year_from), _safe_int(r.get("last_year"), year_to))
        viol = []
        for r in t:
            code6 = str(r[H["stkcd"]] or "").zfill(6)
            if code6 not in win:
                viol.append((r[H["stkcd"]], r[H["year"]]))
                continue
            y = _safe_int(r[H["year"]], None)
            if y is None or not (win[code6][0] <= y <= win[code6][1]):
                viol.append((r[H["stkcd"]], r[H["year"]]))
        if viol:
            issues.append(f"面板窗口违例 {len(viol)}: {viol[:5]}")

    # 4) freq 公式（自动发现 组名：*_kw_count 列）
    groups = sorted({str(c)[: -len("_kw_count")] for c in h if str(c or "").endswith("_kw_count")})
    bad_freq = 0
    freq_ready = "total_chars" in H
    if not freq_ready:
        issues.append(f"表头缺 total_chars 列（无法做 freq/真实行检查）: {h[:8]}")
        real = []
    else:
        real = [r for r in t if r[H["total_chars"]]]
    # 配套列齐全 + 数值可算，任一不满足计入违例而非让审计器崩溃
    for g in groups:
        if f"{g}_kw_count" not in H or f"{g}_kw_freq" not in H:
            issues.append(f"组 {g} 的 count/freq 列不配套（缺 {f'{g}_kw_count' if f'{g}_kw_count' not in H else f'{g}_kw_freq'}）")
            groups = [x for x in groups if x != g]
    for r in real:
        tc = _safe_int(r[H["total_chars"]], None)
        if tc is None or tc <= 0:
            bad_freq += 1
            continue
        for g in groups:
            cnt = _safe_int(r[H[f"{g}_kw_count"]], None)
            frq = _safe_float(r[H[f"{g}_kw_freq"]], None)
            if cnt is None or frq is None or round(cnt * 2000 / tc, 3) != frq:
                bad_freq += 1
    if bad_freq:
        issues.append(f"freq 公式违例 {bad_freq} 处（组: {groups}）")

    # 5) 年度↔标题对齐（year 残缺的行计入错位而非崩溃——title 常规即匹配年度报告正则）
    mis = 0
    for r in t:
        m = re.search(r"(\d{4})\s*年年度报告", str(r[H.get("title", 0)] or ""))
        if m and _safe_int(r[H["year"]], -1) != int(m.group(1)):
            mis += 1
    if mis:
        issues.append(f"year↔title 错位 {mis} 行")

    # 6) 文本源覆盖 + 抽验全字段重算
    if keywords is not None:
        kw_bad = (not isinstance(keywords, dict)
                  or not all(hasattr(v, "__iter__") and not isinstance(v, (str, bytes))
                             and all(isinstance(w, str) for w in v)
                             for v in keywords.values()))
        if kw_bad:
            issues.append(f"keywords 参数须为 {{组: [词,...]}}（词须为字符串），实为 "
                          f"{type(keywords).__name__}（全字段重算降级为仅验 total_chars）")
            keywords = None
    if texts_dir:
        td = Path(texts_dir)
        have = {m.group(0) for m in (re.match(r"\d{6}_\d{4}", f.name) for f in td.glob("*.txt.gz")) if m}
        uncovered = [k for k in ((str(r[H['stkcd']]), str(r[H['year']])) for r in real)
                     if f"{k[0]}_{k[1]}" not in have]
        flagged = sum(1 for r in real if "不可复现" in str(r[H.get("备注", -1)] or ""))
        if len(uncovered) > flagged:
            issues.append(f"文本源缺失 {len(uncovered)} > 已标注不可复现 {flagged}（有未标注的缺口）")
        random.seed(7)
        samp = random.sample(real, min(sample, len(real)))
        okl = 0
        n_have = 0
        for r in samp:
            y = _safe_int(r[H["year"]], None)
            if y is None:
                continue  # 年份残缺行已在"年份越界/非法"中计 issue
            p = td / f"{r[H['stkcd']] if isinstance(r[H['stkcd']], str) else str(r[H['stkcd']]).zfill(6)}_{y}.txt.gz"
            if not p.exists():
                continue
            n_have += 1
            try:
                text = gzip.open(p, "rt", encoding="utf-8").read()
            except Exception as e:
                issues.append(f"文本档案损坏 {p.name}: {type(e).__name__}: {e}")
                continue
            good = len(text) == r[H["total_chars"]]
            if keywords:
                for g, words in keywords.items():
                    if g in groups and f"{g}_kw_count" in H:
                        good = good and sum(text.count(w) for w in words) == r[H[f"{g}_kw_count"]]
            okl += bool(good)
        mode = "全字段关键词重算" if keywords else "仅验 total_chars（--keywords 可升级为全字段）"
        log(f"抽验重算 {okl}/{n_have}（{mode}）")
        if okl < n_have:
            # 核心检查必须是门禁不是日志——否则单管线复算失败也绿灯（假阴性）
            issues.append(f"抽验重算未命中 {n_have - okl}/{n_have}（{mode}）")

        # 6b) 繁体披露版全量扫描（T3 实测：A+H 双语繁体年报让简体词典计数失真，
        # 600029_2016 合规词 0 次即此因）——发现未标注的繁体行必须报 issue
        if texts_dir:
            td2 = Path(texts_dir)
            n_trad = n_unflagged = 0
            for r in real:
                y2 = _safe_int(r[H["year"]], None)
                if y2 is None:
                    continue
                p2 = td2 / f"{r[H['stkcd']] if isinstance(r[H['stkcd']], str) else str(r[H['stkcd']]).zfill(6)}_{y2}.txt.gz"
                if not p2.exists():
                    continue
                try:
                    head = gzip.open(p2, "rt", encoding="utf-8").read(20000)
                except Exception:
                    continue
                fan = sum(head.count(c) for c in "國關於這時總運網發後來區員會務東車語")
                jian = sum(head.count(c) for c in "国关于这时总运网发后来区员会务东车语")
                if fan > jian * 2 and fan > 10:
                    n_trad += 1
                    if "繁体" not in str(r[H.get("备注", -1)] or ""):
                        n_unflagged += 1
            if n_trad:
                log(f"繁体披露版 {n_trad} 行（未标注 {n_unflagged}）")
                if n_unflagged:
                    issues.append(f"繁体披露版未标注 {n_unflagged}/{n_trad}——简体词典计数失真行必须在备注声明")

    # 7) 文档-数据一致（说明页里的数字 vs 实际行数）
    for sn in ("说明", "notes"):
        if sn not in wb.sheetnames:
            continue
        declared = " ".join(str(c and c.value or "") for row in wb[sn].iter_rows() for c in row)
        import re as _re
        m = _re.search(r"(\d{1,3}(?:,\d{3})+|\d{3,})\s*行", declared)
        if m:
            claimed = int(m.group(1).replace(",", ""))
            if claimed != len(t) and claimed != len(real):
                issues.append(f"说明页声明 {claimed} 行 vs 实际主表 {len(t)}/真实 {len(real)} 不一致")
    return issues


def audit_urls(xlsx: str, sheet: str = None, url_col: str = "source_url",
               note_col: str = None, timeout: int = 30) -> List[str]:
    """来源 URL 可达性 + 根路径引用检测（视为引用的 URL 必须是深链）。

    note_col 未指定时自动尝试 source_org/备注 列：含「待补」的行豁免（显式承认的缺口）。"""
    import requests
    from openpyxl import load_workbook
    issues: List[str] = []
    try:
        wb = load_workbook(xlsx, read_only=True)
    except Exception as e:
        return [f"xlsx 读取失败: {type(e).__name__}: {e}"]
    if not wb.sheetnames:
        return ["xlsx 无任何工作表"]
    if sheet is not None and sheet not in wb.sheetnames:
        return [f"工作表 {sheet!r} 不存在: 实有 {wb.sheetnames[:5]}"]
    ws = wb[sheet] if sheet else wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return ["首个工作表为空（无表头行）"]
    h = list(rows[0])
    H = {c: i for i, c in enumerate(h)}
    if url_col not in H:
        return [f"缺 {url_col} 列"]
    if note_col is None:
        note_col = next((c for c in ("source_org", "备注", "note", "notes") if c in H), None)
    S = requests.Session()
    S.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
    bad, roots, ok = [], [], 0
    for r in rows[1:]:
        u = str(r[H[url_col]] or "").strip()
        flagged = note_col and note_col in H and "待补" in str(r[H[note_col]] or "")
        if not u:
            if not flagged:  # 已声明待补的空引用豁免（与根路径同规则）
                issues.append(f"空 URL 行: {r[0]}")
            continue
        if _ROOT_URL.match(u):
            if not flagged:
                roots.append((r[0], u))
            continue
        if flagged:
            continue
        # 重试+429 退避：瞬时抖动/限流不误报（第二判才计入不可达）
        code = None
        for attempt in range(2):
            try:
                resp = S.get(u, timeout=timeout, stream=True,
                             headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                    "Chrome/126.0.0.0 Safari/537.36"})
                code = resp.status_code
                resp.close()
                if code == 429 and attempt == 0:
                    time.sleep(3)
                    continue
                break
            except Exception as e:
                if attempt == 0:
                    time.sleep(3)
                    continue
                code = type(e).__name__
                break
        if isinstance(code, int) and code < 400:
            ok += 1
        else:
            bad.append((r[0], code, u[:70]))
        time.sleep(0.7)
    if roots:
        issues.append(f"根路径引用未标注待补 {len(roots)}: {[x[1] for x in roots[:4]]}")
    if bad:
        issues.append(f"URL 不可达 {len(bad)}: {bad[:6]}")
    log(f"URL 审计: 可达 {ok} / 不可达 {len(bad)} / 根路径 {len(roots)}")
    return issues


def audit_verbatim(json_path: str, sources: dict, text_field: str = "clause_text",
                   doc_field: str = "doc_id", unique: bool = True) -> List[str]:
    """逐字回源 + 文档内唯一性（条款库类）。sources: {doc_id: 文件路径}。"""
    import pymupdf
    issues: List[str] = []
    if not isinstance(sources, dict):
        return [f"sources 必须是 {{doc_id: 路径}} 映射，实为 {type(sources).__name__}"]
    try:
        data = json.load(open(json_path, encoding="utf-8"))
    except Exception as e:
        return [f"条款 JSON 读取/解析失败: {type(e).__name__}: {e}"]
    if isinstance(data, dict) and "clauses" in data:
        cls = data["clauses"]
    elif isinstance(data, list):
        cls = data
    else:
        return [f"JSON 结构不识别（既非列表也无 clauses 键）: 顶层键 {list(data)[:5] if isinstance(data, dict) else type(data).__name__}"]
    if not isinstance(cls, list) or not all(isinstance(c, dict) for c in cls):
        return [f"clauses 不是 dict 列表: {type(cls).__name__}"
                + (f"（首元素 {type(cls[0]).__name__}）" if isinstance(cls, list) and cls else "")]
    if not cls:
        return ["clauses 为空列表（无可审计条款）"]
    # 字段守卫查全量而非仅首条——后续条款缺键曾在迭代处裸 KeyError
    bad_doc = next((i for i, c in enumerate(cls) if doc_field not in c), None)
    if bad_doc is not None:
        return [f"条款[{bad_doc}] 缺 {doc_field} 字段: 实有键 {list(cls[bad_doc])[:6]}"]
    bad_txt = next((i for i, c in enumerate(cls) if text_field not in c), None)
    if bad_txt is not None:
        return [f"条款[{bad_txt}] 缺 {text_field} 字段: 实有键 {list(cls[bad_txt])[:6]}"]
    texts = {}
    for k, p in sources.items():
        try:
            if str(p).endswith(".pdf"):
                doc = pymupdf.open(p)
                texts[k] = _norm("".join(pg.get_text() for pg in doc))
                doc.close()
            else:
                texts[k] = _norm(open(p, encoding="utf-8").read())
        except Exception as e:
            issues.append(f"源文件读取失败 {k}={p}: {type(e).__name__}: {e}")
    missing_src = {c[doc_field] for c in cls if c[doc_field] not in texts}
    if missing_src:
        issues.append(f"条款引用的 doc_id 无对应源文件 {len(missing_src)} 个: {sorted(missing_src)[:5]}")
        return issues
    miss = sum(1 for c in cls if _norm(c[text_field]) not in texts[c[doc_field]])
    if miss:
        issues.append(f"逐字回源未中 {miss}/{len(cls)}")
    if unique:
        dupn = len(cls) - len({(c[doc_field], _norm(c[text_field])) for c in cls})
        if dupn:
            issues.append(f"文档内重复条款 {dupn}（页眉/页脚伪影嫌疑）")
    log(f"逐字回源: {len(cls) - miss}/{len(cls)} 命中 | 文档内重复 {dupn if unique else '-'}")
    return issues


def log(m: str) -> None:
    print(f"[audit] {m}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="audit", description="交付审计电池（出口检查）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("panel", help="面板 xlsx 全量电池")
    p1.add_argument("--xlsx", required=True)
    p1.add_argument("--texts", help="文本档案目录（txt.gz）")
    p1.add_argument("--keywords", help="研究词典 JSON {组:[词]}——抽验升级为全字段关键词重算")
    p1.add_argument("--universe")
    p1.add_argument("--year-from", type=int, default=2010)
    p1.add_argument("--year-to", type=int, default=2024)
    p2 = sub.add_parser("urls", help="来源 URL 可达性")
    p2.add_argument("--xlsx", required=True)
    p2.add_argument("--sheet")
    p2.add_argument("--url-col", default="source_url")
    p2.add_argument("--note-col")
    p3 = sub.add_parser("verbatim", help="逐字回源（JSON 条款库）")
    p3.add_argument("--json", required=True)
    p3.add_argument("--sources", required=True, help="doc_id=路径 的 JSON 映射文件")
    p3.add_argument("--no-unique", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "panel":
        kw = None
        if getattr(args, "keywords", None):
            try:
                kw = json.load(open(args.keywords, encoding="utf-8"))
            except Exception as e:
                print(f"✗ 词典文件读取失败: {e}")
                return 1
        issues = audit_panel(args.xlsx, args.texts, args.universe, args.year_from, args.year_to,
                             keywords=kw)
    elif args.cmd == "urls":
        issues = audit_urls(args.xlsx, args.sheet, args.url_col, args.note_col)
    else:
        try:
            srcs = json.load(open(args.sources, encoding="utf-8"))
        except Exception as e:
            print(f"✗ sources 映射文件读取失败: {e}")
            return 1
        issues = audit_verbatim(args.json, srcs, unique=not args.no_unique)
    if issues:
        print("\n".join("✗ " + i for i in issues))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
