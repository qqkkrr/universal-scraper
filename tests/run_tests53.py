#!/usr/bin/env python3
"""第四十二轮回归（7 个）：万能化强化——
选择器自愈、浏览器默认滚动、流水线丢弃/跳过统计、LLM 兜底用渲染页。
用法: python3 tests/run_tests53.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


def main():
    print("== 选择器自愈（AI 猜错 class 也能出数据）==")
    from universal_scraper.modules.parsers import ConfigParser
    from universal_scraper.protocols import Response, Request
    html = ("<html><body>" +
            "".join(f'<li class="job-card-box"><a class="job-name" href="/job_detail/{i}.html">产品经理{i}</a>'
                    f'<span class="salary">10-{i}K</span><a class="boss-info">公司{i}</a></li>' for i in range(5)) +
            "</body></html>")
    resp = Response(request=Request(url="https://x/list"), status=200, text=html, url="https://x/list")
    p = ConfigParser({"type": "html", "row_css": ".wrong-selector",
                      "fields": {"title": {"css": "a.job-name::text"}}}, {})
    r = p.parse(resp, None)
    check("错误选择器自愈出数据", len(r.items) >= 5, str(len(r.items)))
    check("自愈字段有值", len(r.items) > 0 and r.items[0].get("title") == "产品经理0", str(r.items[0])[:80])

    print("== 浏览器默认滚动 ==")
    from universal_scraper.auto import _validate_and_fix
    cfg = {"name": "t", "start_urls": ["http://x/"], "source": {"type": "browser"},
           "storage": {"type": "jsonl"}, "output": {"dir": "outputs", "base_name": "t"}}
    out = _validate_and_fix(dict(cfg))
    check("browser 默认 scroll_count=4", out["source"].get("scroll_count") == 4, str(out["source"]))

    print("== 流水线丢弃/跳过统计 ==")
    from universal_scraper.modules.pipelines import Pipeline
    pipe = Pipeline([{"type": "parse_date", "field": "pt", "out": "ts", "now": 1785945600},
                     {"type": "filter", "field": "ts", "op": "between",
                      "min": 1785945600, "max": 1786032000}], {})
    keep = pipe.process({"pt": "2026-08-06"})
    drop = pipe.process({"pt": "昨天"})
    skip = pipe.process({"pt": ""})
    check("8/6 保留", keep is not None and keep.get("ts") == 1785945600)
    check("昨天丢弃", drop is None)
    check("缺日期保留(不静默全丢)", skip is not None)
    check("丢弃/跳过统计", pipe.dropped.get("filter:ts:区间外") == 1
          and pipe.skipped.get("filter:ts:日期缺失") == 1, str(pipe.dropped) + str(pipe.skipped))

    print("== LLM 兜底用渲染页 ==")
    import universal_scraper.auto as auto_mod
    rendered = "<html><body><h1>产品经理</h1>" + "".join(f"<p>岗位描述{i}：负责产品规划、需求分析、跨部门协作，薪资 10-{i}K，公司{i}，要求本科以上学历，工作地点北京，五险一金齐全，双休。</p>" for i in range(5)) + "</body></html>"
    auto_mod._llm_chat = lambda messages, temperature=0.1: json.dumps([{"标题": "产品经理", "公司": "某公司"}], ensure_ascii=False)
    fb = auto_mod._llm_fallback_extract("抓产品经理岗位", {"start_urls": ["http://x/"]}, lambda m: None,
                                        rendered_html=rendered, rendered_url="http://x/")
    check("渲染页兜底出条目", fb.get("total", 0) >= 1 and fb.get("items", [{}])[0].get("标题") == "产品经理",
          str(fb.get("items"))[:80])

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
