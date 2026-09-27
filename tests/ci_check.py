#!/usr/bin/env python3
"""CI 自检（R101 新能力：仓库内自包含，不依赖外部回归目录）。

覆盖：全模块编译/导入、ReDoS lint 真阳/假阳、
v3 pipeline template 三态、fixture 夹具测试（进程内调用）。
用法: python3 tests/ci_check.py   （仓库根运行）
"""
import importlib.util
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAIL = []


def check(name, cond, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


print("== 编译/导入 ==")
py_files = sorted(ROOT.rglob("*.py"))
bad = []
for f in py_files:
    if "__pycache__" in str(f) or "node_modules" in str(f):
        continue
    try:
        py_compile.compile(str(f), doraise=True)
    except Exception as e:
        bad.append(f"{f.name}: {e}")
check(f"py_compile {len(py_files)} 文件", not bad, str(bad[:3]))

import universal_scraper  # noqa: F401
check("import universal_scraper", True)

print("== 新功能边界（2026-09-21 批次回归） ==")
from universal_scraper.capture_gen import _detect_chains, _sample_records, _dig  # noqa: E402

_ch = _detect_chains(
    [({"name": "list", "source": {"url": "http://x/api/list"}, "pagination": {}}, [{"id": 5}]),
     ({"name": "detail", "source": {"url": "http://x/api/detail",
                                    "json_body": {"id": 5, "page": 1}}}, [])],
    log=lambda *_: None)
check("api_chain: POST 体详情报告链但不出 scaffold",
      len(_ch) == 1 and _ch[0]["scaffold"] is None and _ch[0]["confidence"] == "value")
_ch2 = _detect_chains(
    [({"name": "l", "source": {"url": "http://x/l"}, "pagination": {}}, [{"detail_id": 7}]),
     ({"name": "d", "source": {"url": "http://x/d?detailId=9"}, "pagination": {}}, [])],
    log=lambda *_: None)
check("api_chain: 值不符时键名回退仍出可跑 scaffold",
      len(_ch2) == 1 and _ch2[0]["confidence"] == "name" and _ch2[0]["scaffold"] is not None)
_ch3 = _detect_chains(
    [({"name": "l", "source": {"url": "http://x/l"}, "pagination": {}}, [{"page": 1}]),
     ({"name": "d", "source": {"url": "http://x/d?page=1"}, "pagination": {}}, [])],
    log=lambda *_: None)
check("api_chain: 翻页参数不构链", _ch3 == [])
# 审查 M2 回归：值替换必须锚定参数名——同值多参数/前缀碰撞两形态
_ch4 = _detect_chains(
    [({"name": "l", "source": {"url": "http://x/l"}, "pagination": {}}, [{"id": 12}]),
     ({"name": "d", "source": {"url": "http://x/d?a=12&id=12"}, "pagination": {}}, [])],
    log=lambda *_: None)
check("api_chain: 同值多参数只动锚定参数",
      _ch4 and _ch4[0]["scaffold"]["pipeline"][0]["tmpl"] == "http://x/d?a=12&id={id}")
_ch5 = _detect_chains(
    [({"name": "l", "source": {"url": "http://x/l"}, "pagination": {}}, [{"x": 12}]),
     ({"name": "d", "source": {"url": "http://x/d?id=1234&x=12"}, "pagination": {}}, [])],
    log=lambda *_: None)
check("api_chain: 值前缀碰撞不损坏 URL",
      _ch5 and _ch5[0]["scaffold"]["pipeline"][0]["tmpl"] == "http://x/d?id=1234&x={x}")
check("链检测: _dig 数组下标/缺路径", _dig([10, 20], "1") == 20 and _dig({"a": {}}, "a.b.c") is None)
check("链检测: _sample_records 兜底键",
      _sample_records({"items": [{"b": 2}]}, "") == [{"b": 2}]
      and _sample_records({"a": 1}, "") == [])

from universal_scraper.report import _num  # noqa: E402
check("report: 前置单位区间", _num("1万-2万") == 15000 and _num("4-5万") == 45000
      and _num("3-5亿") == 400000000)
check("report: 日期/年月拒绝", _num("2026-09-15") is None and _num("2026-09") is None)

from universal_scraper.config import collect_warnings  # noqa: E402
_ws = collect_warnings({"": "junk", "name": "t", "source": {"type": "http", "url": "https://x"}})
check("config: 空字符串键出警告不崩", any("未知顶层键" in w for w in _ws))

from universal_scraper.engine import _merge_url  # noqa: E402
check("engine: _merge_url 相对页路径回溯",
      _merge_url("../../../x_1/index.html",
                 "https://b.com/catalogue/category/books/travel_2/index.html")
      == "https://b.com/catalogue/x_1/index.html")
check("engine: _merge_url 根相对/协议相对/绝对",
      _merge_url("/a/b", "https://b.com/dir/l.html") == "https://b.com/a/b"
      and _merge_url("//cdn.com/x.js", "https://b.com/") == "https://cdn.com/x.js"
      and _merge_url("https://a.com", "https://b.com/") == "https://a.com"
      and _merge_url("", "https://b.com/") == "")

from universal_scraper.diagnose import classify_block  # noqa: E402
_cb = classify_block(403, "forbidden", {"server": "nginx"})
check("diagnose: 403 判型为阻断", bool(_cb.get("is_block")))

print("== ReDoS lint ==")
from universal_scraper.selectors import regex_is_dangerous

for p in (r"(a|a)+$", r"(a+)+$", r"(\d|)+$"):
    check(f"TP {p}", regex_is_dangerous(p))
for p in (r"\d+", r"(ab|ba)+", r"(?:a|b)+", r"(\d+|color)"):
    check(f"FP {p}", not regex_is_dangerous(p))

print("== v3 pipeline template 三态 ==")
from universal_scraper.modules.pipelines import Pipeline

p = Pipeline([{"type": "template", "field": "u", "tmpl": None, "default": "D"}], {})
check("tmpl:null → default", p.process({"id": "x"})["u"] == "D")
p2 = Pipeline([{"type": "template", "field": "u", "tmpl": "https://x/{id}"}], {})
check("tmpl 文档键", p2.process({"id": "1"})["u"] == "https://x/1")

print("== 夹具（进程内调用） ==")
spec = importlib.util.spec_from_file_location(
    "us_fixture_test", ROOT / "scripts" / "fixture_test.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # 只加载定义（__main__ 块不执行）
try:
    fx_rc = mod.main()
except SystemExit as e:
    fx_rc = e.code or 0
check("fixture_test exit 0", fx_rc in (0, None), f"rc={fx_rc}")

print()
print("✅ CI 自检全部通过" if not FAIL else f"❌ 失败 {len(FAIL)} 项: {FAIL}")
sys.exit(1 if FAIL else 0)
