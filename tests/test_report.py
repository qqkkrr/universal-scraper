# -*- coding: utf-8 -*-
"""report 模块单元测试"""
import sys, csv, io
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from universal_scraper.report import _num, _median, _read_csv, _numeric_cols, generate

def test_num_chinese_units():
    assert _num("120000") == 120000.0
    assert _num("12万") == 120000.0          # 关键：中文万
    assert _num("广深合计29.4万") == 294000.0
    assert _num("1.55万") == 15500.0
    assert _num("300000") == 300000.0
    assert _num("0") == 0.0
    assert _num("未公布") is None
    assert _num("") is None


def test_num_range():
    assert _num("4-5万") == 45000.0
    assert _num("42000-50000") == 46000.0
    assert _num("4-5") == 4.5
    assert _num("12万") == 120000.0

def test_median():
    assert _median([1, 2, 3]) == 2
    assert _median([1, 2, 3, 4]) == 2.5
    assert _median([5]) == 5

def test_read_csv_gbk():
    p = Path("/tmp/test_gbk.csv")
    p.write_bytes("城市,机架\n上海,12万\n北京,30万\n".encode("gbk"))
    rows = _read_csv(str(p))
    assert len(rows) == 2 and rows[0]["城市"] == "上海"

def test_numeric_cols_skip_year():
    rows = [
        {"城市": "A", "机架": "1万", "年份": "2020"},
        {"城市": "B", "机架": "2万", "年份": "2021"},
        {"城市": "C", "机架": "3万", "年份": "2022"},
    ]
    cols = _numeric_cols(rows)
    assert "机架" in cols and "年份" not in cols

def test_generate_report():
    p = Path("/tmp/test_report.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["城市", "机架", "组别"])
        w.writerow(["A", "12万", "处理组"])
        w.writerow(["B", "30万", "对照组"])
        w.writerow(["C", "5万", "对照组"])
    r = generate(str(p), group_col="组别", out="/tmp/test_report.html")
    assert r["rows"] == 3 and r["groups"] == 2
    html = Path("/tmp/test_report.html").read_text(encoding="utf-8")
    assert "120000" in html or "12万" in html
    print("HTML 含机架数值:", "120000" in html)


def test_xss_escape():
    """恶意 CSV 内容不得注入 HTML"""
    p = Path("/tmp/test_xss.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["<script>alert(1)</script>", "机架", "组别"])
        w.writerow(["<img src=x onerror=alert(2)>", "1万", "处理组"])
        w.writerow(["正常", "2万", "<b>对照组</b>"])
        w.writerow(["正常2", "3万", "对照组"])
    r = generate(str(p), group_col="组别", out="/tmp/test_xss.html")
    html = Path("/tmp/test_xss.html").read_text(encoding="utf-8")
    # 转义后不应出现原始 <script> 标签
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;对照组&lt;/b&gt;" in html
    print("XSS 测试通过：恶意内容已被转义")

def test_empty_numeric():
    """无非数值数据时不崩溃"""
    p = Path("/tmp/test_empty.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["城市", "备注"])
        w.writerow(["A", "无"])
        w.writerow(["B", "无"])
    r = generate(str(p), out="/tmp/test_empty.html")
    assert r["numeric"] == 0
    print("空数值测试通过")

if __name__ == "__main__":
    for fn in [test_num_chinese_units, test_num_range, test_median, test_read_csv_gbk, test_numeric_cols_skip_year, test_generate_report, test_xss_escape, test_empty_numeric]:
        fn()
        print(f"✅ {fn.__name__}")
    print("全部测试通过")
