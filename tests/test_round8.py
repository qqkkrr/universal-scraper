"""第八轮反馈回归（智联战例）：{{page}} 替换顺序 / none 策略替换 / verify dict 包装。"""
import json
from pathlib import Path

import pytest

from universal_scraper.config import ConfigError, validate
from universal_scraper.fetchers import _apply_page_tokens, _apply_page_tokens_value
from universal_scraper.verify import verify_file


def test_double_brace_replaced_correctly():
    """智联战例：{{page}} 曾被 {page} 半替换成 '{2}' 直接发给服务器。"""
    body = {"pageIdx": "{{page}}", "pageSize": 20}
    out = _apply_page_tokens_value(body, page=3, offset=40)
    assert out == {"pageIdx": "3", "pageSize": 20}


def test_single_brace_still_works():
    assert _apply_page_tokens("a={page}&o={offset}", page=2, offset=20) == "a=2&o=20"


def test_url_and_body_both():
    assert _apply_page_tokens("https://x/list?page={{page}}", 7, 0) == "https://x/list?page=7"
    assert _apply_page_tokens_value({"k": "{{offset}}"}, 3, 60) == {"k": "60"}


def test_none_strategy_config_validates():
    cfg = {"name": "t",
           "source": {"type": "http_json", "method": "POST", "url": "https://api.x.example/",
                      "json_body": {"pageIdx": "{{page}}", "pageSize": 3000}},
           "pagination": {"strategy": "none", "records_path": "data.list"}}
    assert validate(cfg) == cfg


def test_verify_unwraps_dict(tmp_path: Path):
    p = tmp_path / "out.json"
    p.write_text(json.dumps({"code": 0, "data": [{"标题": "a", "阅读": 1},
                                                 {"标题": "b", "阅读": 2}]}, ensure_ascii=False),
                 encoding="utf-8")
    rep = verify_file(str(p))
    assert rep.get("total") == 2
    rep2 = verify_file(str(p), data_key="data")
    assert rep2.get("total") == 2


def test_validate_still_rejects_list_record_fields():
    cfg = {"name": "t",
           "source": {"type": "http_html", "url": "https://x.example/", "row_css": ".i"},
           "pagination": {"strategy": "none"},
           "record": {"fields": ["标题"]}}
    with pytest.raises(ConfigError):
        validate(cfg)
