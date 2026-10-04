#!/usr/bin/env python3
"""覆盖率度量（R20，报告工具——不是门禁）。

定位：R18/R19 抓到的 bug 多数躺在"审过但没测过"的路径上——覆盖率的作用是
把"哪些模块根本没被测过"变成可见事实，而不是追求数字。

用法:
    python3 tests/coverage_report.py            # 跑 pytest + 打印总览与最低覆盖模块
    python3 tests/coverage_report.py --json     # 额外输出 coverage.json

依赖 coverage（pip install coverage）。未安装时**明确说明并退出 0**——
不得静默跳过（静默跳过 = 假装测过，本仓库最忌讳的假成功）。
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP_N = 12


def _has_coverage() -> bool:
    try:
        import coverage  # noqa: F401
        return True
    except ImportError:
        return False


def main(argv) -> int:
    if not _has_coverage():
        print("⚠️ 未安装 coverage —— 覆盖率报告跳过（pip install coverage 后可用）")
        print("   注意：跳过 ≠ 通过；本工具是报告而非门禁，pytest/CI 自检才是门禁。")
        return 0
    want_json = "--json" in argv
    data_file = ROOT / ".coverage"
    cmd = [sys.executable, "-m", "coverage", "run", "--data-file", str(data_file),
           "-m", "pytest", "tests/", "-q", "--no-header", "-p", "no:cacheprovider"]
    print("== 运行 pytest（coverage run）==")
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
    print(f"  pytest: {tail[0][:120]}（退出码 {r.returncode}）")
    if r.returncode not in (0, 1):     # 1 = 有用例失败；其它 = pytest 自身崩
        print(r.stderr[-600:])
        return r.returncode
    rep = subprocess.run([sys.executable, "-m", "coverage", "json", "--data-file", str(data_file),
                          "-o", str(ROOT / "coverage.json"), "--pretty-print"],
                         cwd=str(ROOT), capture_output=True, text=True)
    if rep.returncode != 0:
        print("coverage json 生成失败:", rep.stderr[-400:])
        return 1
    data = json.loads((ROOT / "coverage.json").read_text(encoding="utf-8"))
    files = [(p, v.get("summary", {}).get("percent_covered", 0.0), v.get("summary", {}).get("num_statements", 0))
             for p, v in data.get("files", {}).items()]
    files.sort(key=lambda x: x[1])
    total = data.get("totals", {}).get("percent_covered", 0.0)
    print(f"== 总覆盖率 {total:.1f}%（语句 {data.get('totals', {}).get('num_statements', 0)}）==")
    print(f"== 覆盖率最低的 {TOP_N} 个模块（找'没测过的路径'，不是找数字）==")
    for p, pct, stmts in files[:TOP_N]:
        rel = str(Path(p).relative_to(ROOT)) if str(p).startswith(str(ROOT)) else p
        print(f"  {pct:6.1f}%  {stmts:5d} 语句  {rel}")
    if not want_json:
        try:
            (ROOT / "coverage.json").unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
