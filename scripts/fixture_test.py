#!/usr/bin/env python3
"""站点模板 fixture 测试（R101 新能力：贡献流程的一部分）。

为 parse 型精配站点提供离线夹具测试——贡献新站点模板时：
  1. 在 fixtures/sites/<站点名>/ 放三个文件：
     - meta.json   : {"site": "注册名", "url": "命中该站的示例 URL"}
     - page.html   : 真实页面快照（脱敏后）
     - expect.json : {"min_rows": 1, "must_have_fields": ["标题", "链接"],
                      "field_contains": {"标题": "关键词"}}
  2. 跑: python3 scripts/fixture_test.py [站点名]
  3. 全部通过才能提交（CI 同口径）。

run 型站点（带 run 函数打网络的）暂不进夹具体系——请优先贡献 parse 型。
"""
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE_ROOT = ROOT / "fixtures" / "sites"


def run_one(fixture_dir: Path) -> list:
    problems = []
    meta = json.loads((fixture_dir / "meta.json").read_text(encoding="utf-8"))
    expect = json.loads((fixture_dir / "expect.json").read_text(encoding="utf-8"))
    page = (fixture_dir / "page.html").read_text(encoding="utf-8")
    site = meta.get("site", fixture_dir.name)
    url = meta.get("url", "")
    from universal_scraper.sites import SITES
    reg = SITES.get(site)
    if reg is None:
        return [f"站点 {site} 未注册"]
    parse = reg.get("parse")
    if parse is None:
        return [f"站点 {site} 是 run 型（打网络），暂不支持夹具测试——请提供 parse 函数"]
    rows = parse(page, url) or []
    min_rows = int(expect.get("min_rows", 1))
    if len(rows) < min_rows:
        problems.append(f"行数 {len(rows)} < 期望 {min_rows}")
    for f in expect.get("must_have_fields", []):
        if rows and f not in rows[0] and not any(r.get(f) for r in rows):
            problems.append(f"缺字段: {f}")
    for f, sub in (expect.get("field_contains") or {}).items():
        got = " ".join(str(r.get(f, "")) for r in rows)
        if sub not in got:
            problems.append(f"字段 {f} 不含期望子串: {sub!r}")
    return problems


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    if not FIXTURE_ROOT.exists():
        print(f"无夹具目录: {FIXTURE_ROOT}")
        return 0
    total = passed = 0
    failures = []
    for d in sorted(FIXTURE_ROOT.iterdir()):
        if not d.is_dir() or not (d / "meta.json").exists():
            continue
        if only and d.name != only:
            continue
        total += 1
        try:
            problems = run_one(d)
        except Exception as e:
            problems = [f"{type(e).__name__}: {e}"]
        if problems:
            failures.append((d.name, problems))
            print(f"❌ {d.name}: " + "; ".join(problems))
        else:
            passed += 1
            print(f"✅ {d.name}")
    print(f"\n夹具测试: {passed}/{total} 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
