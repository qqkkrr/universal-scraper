# -*- coding: utf-8 -*-
"""us report：爬取 CSV → 自动可视化报告（概览/分布/分组对比/缺失统计）
用法: us report data.csv [--group 城市] [--out report.html]
"""
from __future__ import annotations
import csv, re, io, html as _h
from pathlib import Path

def _num(v: object) -> float | None:
    """解析数值；支持中文单位（万/亿）、区间值（'4-5万'→45000）、后缀说明。"""
    if v is None: return None
    s = str(v).strip().replace(",", "")
    m = re.search(r'([+-]?\d+(?:\.\d+)?)\s*-\s*([+-]?\d+(?:\.\d+)?)\s*(万|亿)?', s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        unit = m.group(3) or ""
        scale = 10000 if unit == "万" else (100000000 if unit == "亿" else 1)
        return (lo + hi) / 2 * scale
    m = re.search(r'([+-]?\d+(?:\.\d+)?)\s*(万|亿)?', s)
    if not m: return None
    n = float(m.group(1))
    unit = m.group(2) or ""
    if unit == "万": n *= 10000
    elif unit == "亿": n *= 100000000
    return n

def _median(nums):
    """中位数（偶数取中间两值平均）"""
    s = sorted(nums)
    n = len(s)
    if n % 2 == 1:
        return s[n//2]
    return (s[n//2-1] + s[n//2]) / 2

def _read_csv(path: str) -> list[dict]:
    """读取 CSV（自动检测 utf-8/gbk 编码）"""
    if not Path(path).exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        raise ValueError(f"无法识别文件编码: {path}")
    return list(csv.DictReader(io.StringIO(text)))

def _numeric_cols(rows: list[dict]) -> list[str]:
    """识别数值列（跳过年份/日期列，要求≥3个有效值）"""
    if not rows: return []
    out = []
    SKIP_WORDS = ("年份", "year", "date", "时间", "日期")
    for col in rows[0]:
        if any(k in col.lower() for k in SKIP_WORDS):
            continue
        vals = [_num(r.get(col)) for r in rows if r.get(col) not in (None, "")]
        nums = [v for v in vals if v is not None]
        if nums and len(nums) >= max(3, len(rows) * 0.3):
            out.append(col)
    return out

def _missing(rows, col):
    """缺失值计数"""
    return sum(1 for r in rows if not str(r.get(col, "")).strip())

def generate(path: str, group_col: str | None = None, out: str = "report.html") -> dict:
    """读取 CSV 生成可视化 HTML 报告，返回统计摘要"""
    rows = _read_csv(path)
    if not rows:
        raise ValueError("CSV 无数据")
    n = len(rows)
    cols = list(rows[0].keys())
    num_cols = _numeric_cols(rows)
    stats = []
    for c in num_cols:
        vals = [_num(r.get(c)) for r in rows]
        nums = [v for v in vals if v is not None]
        stats.append({
            "col": c, "n": len(nums), "min": min(nums), "max": max(nums),
            "mean": sum(nums)/len(nums), "median": _median(nums),
            "missing": _missing(rows, c),
        })
    # 分组统计
    group_stats = []
    if group_col and group_col in cols:
        by = {}
        for r in rows:
            k = str(r.get(group_col, "")).strip() or "(空)"
            by.setdefault(k, []).append(r)
        for k, sub in sorted(by.items(), key=lambda x: -len(x[1])):
            row = {"group": k, "n": len(sub)}
            for c in num_cols:
                nums = [_num(x.get(c)) for x in sub]
                nums = [v for v in nums if v is not None]
                if nums:
                    row[f"{c}_mean"] = round(sum(nums)/len(nums), 2)
                    row[f"{c}_sum"] = round(sum(nums), 2)
            group_stats.append(row)

    # 生成 HTML
    parts = [f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>爬取数据报告</title>
<style>
body{{font-family:-apple-system,'PingFang SC',sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#1a1a2e}}
h1{{font-size:22px;border-bottom:2px solid #4361ee;padding-bottom:8px}}
h2{{font-size:17px;margin-top:28px;color:#4361ee}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}}
th,td{{border:1px solid #ddd;padding:6px 8px;text-align:left}}
th{{background:#f0f4ff}}
.badge{{display:inline-block;background:#4361ee;color:#fff;border-radius:12px;padding:2px 10px;font-size:12px;margin:2px}}
.missing{{color:#e63946}}
svg text{{font-size:11px}}
</style></head><body>
<h1>📊 爬取数据报告</h1>
<div><span class="badge">{n} 行</span><span class="badge">{len(cols)} 列</span><span class="badge">{len(num_cols)} 数值列</span></div>
<h2>1. 数据概览</h2>
<table><tr><th>列</th><th>非空</th><th>缺失</th><th>样例</th></tr>"""]
    for c in cols:
        nonempty = n - _missing(rows, c)
        sample = str(rows[0].get(c, ""))[:40] if rows else ""
        miss = _missing(rows, c)
        parts.append(f"<tr><td>{_h.escape(c)}</td><td>{nonempty}</td><td class='missing'>{miss}</td><td>{_h.escape(sample)}</td></tr>")
    parts.append("</table>")

    if stats:
        parts.append("<h2>2. 数值列统计</h2><table><tr><th>列</th><th>样本</th><th>最小</th><th>最大</th><th>均值</th><th>中位数</th><th>缺失</th></tr>")
        for s in stats:
            parts.append(f"<tr><td>{_h.escape(s['col'])}</td><td>{s['n']}</td><td>{s['min']:.0f}</td><td>{s['max']:.0f}</td><td>{s['mean']:.1f}</td><td>{s['median']:.0f}</td><td>{s['missing']}</td></tr>")
        parts.append("</table>")

    if group_stats:
        parts.append(f"<h2>3. 按「{_h.escape(group_col)}」分组</h2>")
        keys = [k for k in group_stats[0] if k not in ("group", "n")]
        parts.append("<table><tr><th>分组</th><th>样本</th>" + "".join(f"<th>{_h.escape(k)}</th>" for k in keys) + "</tr>")
        for g in group_stats:
            parts.append(f"<tr><td>{_h.escape(str(g['group']))}</td><td>{g['n']}</td>" + "".join(f"<td>{g.get(k,'')}</td>" for k in keys) + "</tr>")
        parts.append("</table>")

    parts.append("<h2>4. 数值分布（HTML 条形图）</h2>")
    for s in stats[:3]:
        vals = [_num(r.get(s["col"])) for r in rows]
        nums = sorted(v for v in vals if v is not None)
        if len(nums) < 2: continue
        # 简单分箱
        lo, hi = nums[0], nums[-1]
        if hi - lo < 1e-9: hi = lo + 1
        bins = 10
        width = (hi - lo) / bins
        counts = [0]*bins
        for v in nums:
            idx = min(bins-1, int((v-lo)/width))
            counts[idx] += 1
        maxc = max(counts) or 1
        parts.append(f"<h3>{_h.escape(s['col'])}</h3><svg width='900' height='{40+len(counts)*22}'>")
        for i, c in enumerate(counts):
            bar_h = max(3, int(c/maxc*160))
            parts.append(f"<rect x='50' y='{i*22+20+160-bar_h}' width='40' height='{bar_h}' fill='#4361ee'/>")
            parts.append(f"<text x='95' y='{i*22+34+160-bar_h}'>{c}</text>")
            parts.append(f"<text x='5' y='{i*22+34}'>{lo+i*width:.0f}</text>")
        parts.append("</svg>")
    parts.append("</body></html>")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(parts), encoding="utf-8")
    return {"rows": n, "cols": len(cols), "numeric": len(num_cols), "groups": len(group_stats), "report": str(Path(out).resolve())}

def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="us report", description="爬取 CSV → 自动可视化报告")
    ap.add_argument("csv", help="输入 CSV 文件")
    ap.add_argument("--group", default=None, help="分组列名（可选）")
    ap.add_argument("--out", default="report.html", help="输出 HTML 报告路径")
    a = ap.parse_args(argv)
    r = generate(a.csv, a.group, a.out)
    print(f"✅ 报告已生成：{r['report']}（{r['rows']}行 / {r['cols']}列 / {r['numeric']}数值列 / {r['groups']}组）")
    return 0
