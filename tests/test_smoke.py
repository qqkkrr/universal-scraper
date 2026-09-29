#!/usr/bin/env python3
"""pytest 入口（快速冒烟；重活请跑 tests/ 下的独立 CI 脚本，见 pytest.ini 说明）。

覆盖几处"曾经静默出错"的核心不变式，秒级可跑：
- BOM 剥离（JSON/CSV 任务曾静默 0 条）
- max_size 截断标记贯通（曾判据恒假）
- pipelines 入口归一（畸形步骤曾崩整轮）
- 配置校验对类型完全错的输入只抛 ConfigError（曾裸 AttributeError/TypeError）
- 详情后处理键（detail.post_pipeline）在契约里
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_bom_stripped():
    from universal_scraper.core import smart_decode
    assert smart_decode(b"\xef\xbb\xbf{\"a\":1}") == '{"a":1}'
    assert smart_decode("中文".encode("gbk"), {"content-type": "text/html; charset=gbk"}) == "中文"


def test_pipeline_sanitizes_malformed_steps():
    from universal_scraper.modules.pipelines import Pipeline
    p = Pipeline([None, {"type": ["filter"]}, {"type": "cast", "field": {"a": 1}},
                  {"type": "add", "field": "x", "value": 1}], {})
    out = p.process({"t": 1})
    assert out is not None and out.get("x") == 1     # 畸形步骤跳过、行保留
    assert p.skipped                                # 且计入 skipped（不静默）


def test_config_error_not_bare_exception():
    from universal_scraper.config import ConfigError, validate_task
    for cfg in ({"name": "t", "source": "not-a-dict"},
                {"name": "t", "source": {"type": {"a": 1}}},
                {"name": "t", "source": {"type": ["x"]}}):
        with pytest.raises(ConfigError):
            validate_task(dict(cfg), has_custom_fetcher=False,
                          has_custom_parser=False, has_custom_storage=False)


def test_detail_post_pipeline_contract():
    from universal_scraper.config import validate_task
    cfg = {"name": "t", "type": "scrape", "start_urls": ["https://x.test/l"],
           "source": {"type": "http"}, "storage": {"type": "jsonl"},
           "parsers": {"l": {"type": "html", "row_css": "li", "fields": {"t": {"css": "b"}}}},
           "rules": [{"match": "contains", "pattern": "/", "parser": "l"}],
           "detail": {"enabled": True, "url_field": "url",
                      "post_pipeline": [{"type": "regex_extract", "field": "raw",
                                         "pattern": r"(\d+)", "to": "n"}]}}
    validate_task(cfg, has_custom_fetcher=False, has_custom_parser=False,
                  has_custom_storage=False)          # 不抛即通过


def test_response_has_truncated_flag():
    from universal_scraper.protocols import Request, Response
    r = Response(request=Request(url="http://x/1"))
    assert r.truncated is False
    assert Response(request=Request(url="http://x/1"), truncated=True).truncated is True
