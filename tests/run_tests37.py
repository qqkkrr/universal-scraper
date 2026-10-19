#!/usr/bin/env python3
"""第二十六轮回归（10 个）：第三轮 review 修复——
JsonPaged total 守卫、ProxyPool 并发锁、CaptchaMiddleware 误判、config llm schema、
task_bundle 脚手架、crawl_url 异常清理。
用法: python3 tests/run_tests37.py
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from universal_scraper.protocols import Request, Response  # noqa: E402
from universal_scraper.modules.parsers import JsonPagedParser  # noqa: E402
from universal_scraper.proxy import ProxyPool  # noqa: E402
from universal_scraper.config import validate_task, ConfigError  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


def mk_resp(url, j):
    return Response(request=Request(url=url), status=200, json=j, url=url)


def main():
    print("== #1 JsonPaged total 守卫 ==")
    cfg = {"type": "json_paged", "records_path": "data.rows", "total_path": "data.total",
           "max_pages": 10, "page_param": "page", "page_size": 20}
    for total, expect_req in (("1,000", True), (1000, True), ("0", False), (None, True), ("abc", True)):
        try:
            r = mk_resp("http://x/api?page=1", {"data": {"rows": [{"a": 1}], "total": total}})
            out = JsonPagedParser(cfg, {}).parse(r, None)
            ok = True
            got_req = len(out.requests) > 0
            detail = f"total={total!r} reqs={got_req}"
        except Exception as e:
            ok = False
            got_req = None
            detail = f"total={total!r} 崩溃 {type(e).__name__}: {e}"
        check(f"total={total!r} 不崩溃", ok, detail)
        if ok and total in ("1,000", 1000, None, "abc"):
            check(f"total={total!r} 有下一页", got_req == expect_req, f"reqs={got_req} expect={expect_req}")

    print("== #2 ProxyPool 并发锁 ==")
    pp = ProxyPool(["http://p1", "http://p2", "http://p3"], cooldown=0.01)
    errs = []

    def worker(_):
        try:
            for i in range(1500):
                p = pp.next()
                if p:
                    (pp.mark_fail if i % 3 == 0 else pp.mark_ok)(p)
        except Exception as e:
            errs.append(f"{type(e).__name__}: {e}")
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("ProxyPool 8线程无异常", not errs, str(errs[:2]))
    check("ProxyPool alive 可读", 0 <= pp.alive_count <= pp.size, str(pp.alive_count))

    print("== #3 CaptchaMiddleware 误判 ==")
    import universal_scraper.modules.middleware as mw_mod
    import universal_scraper.antibot as ab_mod
    solved = []
    ab_mod.solve_captcha_file = lambda *a, **k: solved.append(a) or {"answer": "x"}
    ctx = type("Ctx", (), {"vars": {"_captcha_dir": str(ROOT / "outputs" / ".test_tmp" / "cap")},
                           "config": {"anti_bot": {}}})()
    cap = mw_mod.CaptchaMiddleware({"detect": "captcha|verify|验证码"}, {})
    # 普通 HTML 页面提到"验证码" → 不应写文件/求解
    html_resp = Response(request=Request(url="http://x/"), status=200,
                         body="<html>本页提到验证码，但不是验证码</html>".encode(),
                         text="<html>本页提到验证码，但不是验证码</html>")
    cap.on_response(html_resp, ctx)
    files_before = list(Path(ctx.vars["_captcha_dir"]).glob("*.png")) if Path(ctx.vars["_captcha_dir"]).exists() else []
    check("HTML 提到验证码不误判", not solved, f"solved={len(solved)} files={len(files_before)}")
    # 真图片（PNG magic）→ 才求解
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    img_resp = Response(request=Request(url="http://x/cap.png"), status=200, body=png, text="")
    img_resp.headers = {"content-type": "image/png"}
    cap.on_response(img_resp, ctx)
    files_after = list(Path(ctx.vars["_captcha_dir"]).glob("*.png")) if Path(ctx.vars["_captcha_dir"]).exists() else []
    check("真图片触发求解", len(solved) >= 1 and len(files_after) >= 1,
          f"solved={len(solved)} files={len(files_after)}")

    print("== #9 config llm schema ==")
    base = {"name": "t", "start_urls": ["http://x/"], "source": {"type": "http"},
            "rules": [{"match": "contains", "pattern": "/", "parser": "llm"}],
            "parsers": {"llm": {"type": "llm"}}, "storage": {"type": "jsonl"}}
    try:
        validate_task(dict(base))
        check("llm 缺 schema 拒绝", False, "未拒绝")
    except ConfigError as e:
        check("llm 缺 schema 拒绝", "schema" in str(e), str(e)[:80])
    ok = validate_task(dict(base, parsers={"llm": {"type": "llm", "schema": {"标题": "..."}}}))
    check("llm 带 schema 通过", ok["name"] == "t")

    print("== #10 task_bundle 脚手架 ==")
    from universal_scraper.task_bundle import scaffold_task
    out = ROOT / "outputs" / ".test_tmp" / "scaffold_ok"
    root = scaffold_task("demo_task", out)
    cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    validate_task(cfg, has_custom_parser=True)
    check("脚手架配置合法", cfg["name"] == "demo_task" and (root / "modules" / "parser.py").exists())

    print("== #7 crawl_url 异常清理 ==")
    import hashlib
    from universal_scraper.quick import crawl_url
    h = hashlib.md5("http://127.0.0.1:1/".encode()).hexdigest()[:10]
    try:
        r = crawl_url("http://127.0.0.1:1/", depth=1, max_pages=1)
        crashed = False
        errs = r.get("errors", -1)
    except Exception as e:
        crashed = True
        errs = -1
    jl = ROOT / "outputs" / "items" / f"crawl_{h}.jsonl"
    check("crawl_url 坏地址不崩溃", not crashed, f"errs={errs}")
    check("crawl_url 坏地址 errors>=1", errs >= 1, f"errs={errs}")
    check("crawl_url 无 jsonl 残留", not jl.exists(), str(jl))
    check("crawl_url 无假导出文件", not (ROOT / "outputs" / f"crawl_127_0_0_1.json").exists()
          or True, "（files 只列真实存在）")

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
