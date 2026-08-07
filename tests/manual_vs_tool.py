#!/usr/bin/env python3
"""工具 vs 手动浏览 对照测试（可复用）。

对每个真实任务：
  1) 手动基线：直接抓取页面 + 已知可靠选择器提取（相当于"人眼浏览并抄录"）
  2) 工具：走 auto_task 一句话自动抓取
  3) 对照：首条内容是否一致、条数对比、字段覆盖

用法: python3 tests/manual_vs_tool.py [--only quotes|books|hn] [--limit N]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from universal_scraper.structure import _fetch
from universal_scraper.selectors import css_elements, css_text, css_attr, _lxml_html_tostring


def manual_quotes(url="https://quotes.toscrape.com/"):
    """手动基线：逐条读取首页 10 条名言（人眼能看到的内容）。"""
    res = _fetch(url)
    html = res["text"]
    out = []
    for el in css_elements(html, ".quote"):
        s = el if isinstance(el, str) else _lxml_html_tostring(el)
        text = css_text(s, ".text", 0)
        author = css_text(s, ".author", 0)
        tags = css_text(s, ".tags .tag", 0, joiner=",")
        if text:
            out.append({"text": text, "author": author, "tags": tags})
    return out


def manual_books(url="https://books.toscrape.com/"):
    res = _fetch(url)
    html = res["text"]
    out = []
    for el in css_elements(html, "article.product_pod"):
        s = el if isinstance(el, str) else _lxml_html_tostring(el)
        title = css_text(s, "h3 a", 0) or css_attr(s, "h3 a", "title")
        price = css_text(s, ".price_color", 0)
        rating = re.search(r"star-rating ([\w-]+)", css_text(s, ".star-rating", 0) or "").group(1) if re.search(r"star-rating ([\w-]+)", css_text(s, ".star-rating", 0) or "") else ""
        stock = css_text(s, ".availability", 0)
        if title:
            out.append({"title": title, "price": price, "rating": rating, "stock": stock})
    return out


def manual_hn(url="https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=20"):
    """手动基线：直接读 Algolia JSON（人眼在 HN 页面能看到的内容）。"""
    res = _fetch(url)
    data = res["data"]
    out = []
    for hit in data.get("hits", []):
        if hit.get("title"):
            out.append({
                "title": hit.get("title"),
                "url": hit.get("url") or "https://news.ycombinator.com/item?id=" + str(hit.get("objectID")),
                "points": hit.get("points"),
                "comments": hit.get("num_comments"),
            })
    return out


TASKS = {
    "quotes": {
        "desc": "抓取 https://quotes.toscrape.com/ 首页的名言、作者和标签，翻 2 页",
        "manual": manual_quotes,
        "match_fields": ["text", "author"],
        "limit": 30,
    },
    "books": {
        "desc": "抓取 https://books.toscrape.com/ 首页所有书籍的标题、价格、评分和库存，翻 2 页",
        "manual": manual_books,
        "match_fields": ["title", "price"],
        "limit": 30,
    },
    "hn": {
        "desc": "抓取 https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=20 的 HN 首页新闻标题、链接、评分和评论数，翻 2 页",
        "manual": manual_hn,
        "match_fields": ["title", "points"],
        "limit": 30,
    },
}


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def main():
    args = sys.argv[1:]
    only = None
    limit = None
    if "--only" in args:
        only = args[args.index("--only") + 1]
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])

    from universal_scraper.auto import auto_task
    report = {}
    for name, spec in TASKS.items():
        if only and name != only:
            continue
        print(f"\n== {name} ==")
        try:
            manual = spec["manual"]()
        except Exception as e:
            print(f"  ❌ 手动基线失败: {e}")
            report[name] = {"error": f"manual: {e}"}
            continue
        print(f"  手动基线: {len(manual)} 条")
        lim = limit or spec.get("limit")
        try:
            out = auto_task(spec["desc"], limit=lim, log_cb=lambda m: print("  " + m))
        except Exception as e:
            print(f"  ❌ 工具失败: {e}")
            report[name] = {"error": f"tool: {e}", "manual_total": len(manual)}
            continue
        # 完整结果从导出文件读（sample 只抽 5 条，对照精度不够）
        items = list(out.get("sample") or [])
        exp = ROOT / "outputs" / f"{out.get('name')}.json"
        if exp.exists():
            try:
                full = json.loads(exp.read_text(encoding="utf-8"))
                if isinstance(full, list) and full:
                    items = full
            except Exception:
                pass
        tool = [it for it in items if any(k not in ("_url", "_parser", "_ts", "_id") for k in it)]
        print(f"  工具: {out.get('result', {}).get('total')} 条 (有效 {len(tool)})")

        # 对照：首条工具结果 vs 首条手动结果
        first_match = False
        if tool and manual:
            mf = spec["match_fields"]
            for mf_ in mf:
                if norm(tool[0].get(mf_)) and norm(tool[0].get(mf_)) == norm(manual[0].get(mf_)):
                    first_match = True
                    break
        # 重叠率：工具首 N 条中有多少能对手动基线
        overlap = 0
        for it in tool[:len(manual)]:
            for mf_ in spec["match_fields"]:
                v = norm(it.get(mf_))
                if v and any(v == norm(m.get(mf_)) for m in manual):
                    overlap += 1
                    break
        overlap_ratio = round(overlap / max(len(manual), 1), 3)
        print(f"  首条匹配: {first_match} | 重叠率: {overlap}/{len(manual)} = {overlap_ratio}")
        report[name] = {
            "tool_total": out.get("result", {}).get("total"),
            "manual_total": len(manual),
            "first_match": first_match,
            "overlap_ratio": overlap_ratio,
            "tool_first": tool[0] if tool else None,
            "manual_first": manual[0] if manual else None,
            "errors": out.get("result", {}).get("errors"),
        }

    outfile = ROOT / "outputs" / "manual_vs_tool.json"
    outfile.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n对照结果已写入 {outfile}")


if __name__ == "__main__":
    main()
