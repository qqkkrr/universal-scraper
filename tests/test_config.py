# -*- coding: utf-8 -*-
"""v3 任务包配置校验：source.type 白名单 / scrapling 放行 / 未知类型 / rules 必填。"""
import pytest
from universal_scraper.config import validate_task, ConfigError

def _cfg(**over):
    cfg = {
        "name": "t",
        "start_urls": ["https://example.com/"],
        "source": {"type": "http"},
        "rules": [{"match": "contains", "pattern": "example.com", "parser": "default"}],
        "parsers": {"default": {"type": "html", "row_css": ".item", "fields": {"title": {"css": "a::text"}}}},
    }
    cfg.update(over)
    return cfg

def test_scrapling_type_allowed():
    c = _cfg(**{"source": {"type": "scrapling"}})
    out = validate_task(c)
    assert out["source"]["type"] == "scrapling"

def test_unknown_type_rejected():
    c = _cfg(**{"source": {"type": "foobar"}})
    with pytest.raises(ConfigError):
        validate_task(c)

def test_rules_required():
    c = _cfg()
    del c["rules"]
    with pytest.raises(ConfigError):
        validate_task(c)

def test_http_default_when_missing():
    c = _cfg()
    del c["source"]["type"]
    # 校验器对缺失 type 用运行时默认 http（不写回、不报错）
    out = validate_task(c)
    assert out["source"].get("type", "http") == "http"
