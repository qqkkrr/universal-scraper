"""导出管道与 capture 诊断回归（猎聘战例：formats 被无视/嵌套 dict 崩 xlsx/覆盖配置文件）。"""
import json
from pathlib import Path

from universal_scraper.config import collect_warnings
from universal_scraper.core import export_rows

CFG_LIKE = '{"name": "probe_s5", "source": {"type": "http_html", "url": "https://x"}}'


def test_formats_filter(tmp_path: Path):
    rows = [{"a": 1, "b": {"nested": True}}]
    paths = export_rows(rows, tmp_path, "only_json", formats=["json"])
    assert paths["json"].exists() and "csv" not in paths and "xlsx" not in paths
    assert not (tmp_path / "only_json.csv").exists()


def test_nested_dict_does_not_crash_csv_or_xlsx(tmp_path: Path):
    rows = [{"标题": "x", "stat": {"view": 10846966, "coin": 507006}}]
    paths = export_rows(rows, tmp_path, "nested", formats=["json", "csv", "xlsx"])
    assert paths["csv"].exists() and paths["xlsx"].exists()
    text = paths["csv"].read_text(encoding="utf-8-sig")
    assert "view" in text  # 嵌套 dict 安全序列化而非崩溃


def test_config_file_not_overwritten(tmp_path: Path):
    cfg_path = tmp_path / "probe_s5.json"
    cfg_path.write_text(CFG_LIKE, encoding="utf-8")
    rows = [{"标题": "职位A", "薪资": "30-60万"}]
    export_rows(rows, tmp_path, "probe_s5", formats=["json"])
    assert cfg_path.read_text(encoding="utf-8") == CFG_LIKE, "配置文件被导出覆盖！"
    assert (tmp_path / "probe_s5_data.json").exists()


def test_capture_missing_records_path_warns():
    cfg = {"name": "t",
           "source": {"type": "browser", "url": "https://x.example/",
                      "capture": [{"name": "jobs", "url_pattern": "pc-search-job"}],
                      "record_from": "capture"},
           "pagination": {"strategy": "none"},
           "record": {"fields": {"a": {"from": "a"}}}}
    warns = collect_warnings(cfg)
    assert any("records_path" in w for w in warns), "capture 缺 records_path 必须告警"
    cfg["source"]["capture"][0]["records_path"] = "data.data.jobCardList"
    assert not [w for w in collect_warnings(cfg) if "records_path" in w]
