#!/usr/bin/env python3
"""第三十二轮回归（7 个）：第九轮 review 修复——
自定义模块单次 exec、html_to_markdown 线程安全、WebUI api 兜底/按任务停止。
用法: python3 tests/run_tests43.py
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


def main():
    print("== #1 自定义模块只 exec 一次 ==")
    from universal_scraper.task import Task
    td = ROOT / "outputs" / ".test_tmp" / "once_load"
    (td / "modules").mkdir(parents=True, exist_ok=True)
    cfg = {"name": "once_load", "start_urls": ["http://x/"], "source": {"type": "http"},
           "rules": [{"match": "contains", "pattern": "/", "parser": "default"}],
           "parsers": {"default": {"type": "html"}}, "storage": {"type": "jsonl"}}
    (td / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    marker = ROOT / "outputs" / ".test_tmp" / "once_marker.txt"
    try:
        marker.unlink()
    except Exception:
        pass
    (td / "modules" / "parser.py").write_text(
        f"from pathlib import Path\n"
        f"Path({str(marker)!r}).open('a').write('X')\n"
        f"from universal_scraper.protocols import BaseParser, ParseResult\n"
        f"class Parser(BaseParser):\n"
        f"    name = 'default'\n"
        f"    def parse(self, resp, ctx):\n"
        f"        return ParseResult(items=[], requests=[])\n", encoding="utf-8")
    t = Task(td)
    _ = t.get_parsers()
    _ = t.get_custom_parser_classes()
    _ = t.get_parsers()
    content = marker.read_text(encoding="utf-8") if marker.exists() else ""
    check("parser.py 只 exec 一次", content.count("X") == 1, f"count={content.count('X')}")

    print("== #2 html_to_markdown 线程安全 ==")
    from universal_scraper.extractors import html_to_markdown
    html = '<html><body><a href="/path">link</a></body></html>'
    errs = []
    def worker(base, tag):
        for _ in range(60):
            out = html_to_markdown(html, base_url=base)
            if base not in out:
                errs.append((tag, out[:80]))
                return
    ts = [threading.Thread(target=worker, args=(f"http://site-{i}.com", i)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("并发 base_url 不串", not errs, str(errs[:2]))

    print("== #3/#4 WebUI 前端 ==")
    html_src = (ROOT / "webui" / "index.html").read_text(encoding="utf-8")
    check("api() 有 JSON 兜底", "响应解析失败" in html_src and "try { return await r.json(); }" in html_src)
    check("stop 按钮带 jobId", "stopJob('${esc(d.id)}')" in html_src)
    check("stopJob 接受 jobId", "async function stopJob(jobId)" in html_src)

    print("== #1 engine 无 _custom_parsers 死代码 ==")
    ev3 = (ROOT / "universal_scraper" / "engine_v3.py").read_text(encoding="utf-8")
    check("_custom_parsers 已删除", "_custom_parsers" not in ev3)

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
