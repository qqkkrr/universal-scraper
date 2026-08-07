#!/usr/bin/env python3
"""第二十四轮回归（12 个）：Code Review 收尾——
v2/v3 配置校验统一（#9）、pipelines/parsers/storage 校验、us doctor 自检。
用法: python3 tests/run_tests35.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from universal_scraper.config import (  # noqa: E402
    validate, validate_task, ConfigError, ALL_PIPELINE_TYPES, ALL_PARSER_TYPES,
    ALL_STORAGE_TYPES, V3_ACTION_TYPES, V3_SOURCE_TYPES,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


def expect_error(fn, name, needle=""):
    try:
        fn()
        check(name, False, "未抛 ConfigError")
    except ConfigError as e:
        check(name, (not needle) or needle in str(e), str(e)[:120])
    except Exception as e:
        check(name, False, f"{type(e).__name__}: {e}")


def main():
    print("== v2/v3 常量统一（review #9）==")
    check("v2 接受 download 流水线", "download" in ALL_PIPELINE_TYPES)
    check("v2 接受 template/split/default/validate", {"template", "split", "default", "validate"} <= ALL_PIPELINE_TYPES)
    check("v3 动作类型与 v2 一致", V3_ACTION_TYPES == {"click", "type", "write", "fill", "press", "select", "wait",
                                                      "wait_time", "wait_for_selector", "waitfor", "scroll",
                                                      "exec", "js", "execute_javascript", "screenshot", "noop"})
    check("解析器类型集合", ALL_PARSER_TYPES == {"html", "json", "llm", "article", "table", "json_paged"})
    check("存储类型集合", ALL_STORAGE_TYPES == {"jsonl", "csv", "sqlite", "multi"})

    print("== v2 validate 兼容新流水线 ==")
    v2_ok = validate({
        "name": "t", "source": {"type": "http_html", "url": "http://x/"},
        "pagination": {"strategy": "none"},
        "pipeline": [{"type": "download", "field": "pdf", "dir": "dl"},
                     {"type": "template", "field": "full", "template": "{a}-{b}"}],
        "anti_bot": {"captcha": {"strategy": "auto"}},
    })
    check("v2 download/template 校验通过", v2_ok["name"] == "t")

    print("== v3 validate_task pipelines/parsers/storage ==")
    base = {
        "name": "t3",
        "start_urls": ["http://x/"],
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "/", "parser": "default"}],
        "parsers": {"default": {"type": "html"}},
        "storage": {"type": "jsonl"},
    }
    ok3 = validate_task(dict(base, pipelines=[{"type": "download", "field": "pdf"}]))
    check("v3 download 流水线通过", "pipelines" in ok3 or ok3["name"] == "t3")
    expect_error(lambda: validate_task(dict(base, pipelines=[{"type": "download"}])),
                 "v3 download 缺 field 报错", "field")
    expect_error(lambda: validate_task(dict(base, pipelines=[{"type": "no_such_pipe"}])),
                 "v3 未知流水线报错", "未知流水线类型")
    expect_error(lambda: validate_task(dict(base, parsers={"default": {"type": "no_parser"}})),
                 "v3 未知解析器报错", "未知解析器类型")
    expect_error(lambda: validate_task(dict(base, storage={"type": "oracle"})),
                 "v3 未知存储报错", "未知存储类型")
    expect_error(lambda: validate_task(dict(base, storage={"type": "multi"})),
                 "v3 multi 缺 backends 报错", "backends")

    print("== us doctor 自检 ==")
    from universal_scraper.doctor import run as doctor_run
    r = doctor_run()
    check("doctor 返回结构", r.get("total", 0) >= 10 and r.get("ok", 0) > 0, str(r)[:100])
    check("doctor 含依赖/Node/浏览器", any("python" in c["item"] for c in r["checks"])
          and any("Node" in c["item"] for c in r["checks"])
          and any("Chrome" in c["item"] for c in r["checks"]),
          str([c["item"] for c in r["checks"]])[:120])

    print("== 回归：合法 v3 任务仍通过 ==")
    ok4 = validate_task(dict(base, pipelines=[{"type": "filter", "field": "a", "op": "non_empty"}],
                             parsers={"default": {"type": "json_paged", "records_path": "data.rows"}}))
    check("合法 v3 任务通过", ok4["name"] == "t3")

    print()
    print(f"===== 结果: {len(PASS)}/{len(PASS)+len(FAIL)} 通过 =====")
    if FAIL:
        for n, d in FAIL:
            print("  ❌", n, d)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
