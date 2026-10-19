# -*- coding: utf-8 -*-
"""batch2200 实测反馈修复回归（v1.13.1）：无翻页参数不启用 template / 单对象 GraphQL。"""
import json

import pytest

from universal_scraper.capture_gen import one_config
from universal_scraper.config import validate


def _gen(tmp_path, items):
    from universal_scraper.capture_gen import generate
    f = tmp_path / "capture_all.json"
    f.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return generate(f, out=str(tmp_path / "gen"), log=lambda *a: None)


def test_no_page_param_means_no_template_pagination(tmp_path):
    """P0 修复：URL/请求体都没有翻页参数时，绝不能生成 template+max_pages:5
    （曾把同一页重复抓 5 遍：most_traded 7×5=35 全重）。"""
    items = [{"url": "https://api.deutsche-boerse.com/v1/most_traded",
              "method": "GET",
              "request_headers": {"accept": "application/json"},
              "json": [{"symbol": "SAP", "price": 210.5}, {"symbol": "SIE", "price": 160.1}]}]
    r = _gen(tmp_path, items)
    cfg = r["configs"][0]
    assert cfg["pagination"]["strategy"] == "none"      # 不再是 template
    cfg_src = json.dumps(cfg["source"], ensure_ascii=False)
    assert "{{page}}" not in cfg_src                    # _hint 文案除外，配置本体无占位符


def test_single_object_response_marks_single_record(tmp_path):
    """单对象 GraphQL 响应（TMX getQuote 类）→ single_record 模式。"""
    items = [{"url": "https://app-money.tmx.com/graphql",
              "method": "POST",
              "post_data": '{"query":"{getQuoteBySymbol(...)}"}',
              "request_content_type": "application/json",
              "request_headers": {"content-type": "application/json", "locale": "en"},
              "json": {"data": {"getQuoteBySymbol": {"price": 36513.80, "change": -119.32}}}}]
    src = one_config(items[0])
    assert src.get("single_record") is True
    r = _gen(tmp_path, items)
    cfg = r["configs"][0]
    assert cfg["pagination"]["strategy"] == "none"      # 单对象不配翻页
    assert cfg["source"].get("single_record") is True


def test_single_record_config_passes_validate():
    """validate 与 runtime 一致：single_record + 无 records_path 合法。"""
    cfg = {"name": "t",
           "source": {"type": "http_json", "method": "POST",
                      "url": "https://x.example/graphql", "single_record": True,
                      "json_body": {"query": "{}"}},
           "pagination": {"strategy": "none"}}
    validate(cfg)  # 不抛 ConfigError 即通过


def test_template_without_records_path_still_rejected():
    """多页翻页缺 records_path 仍拦截（原 B站 战例契约保留）。"""
    cfg = {"name": "t",
           "source": {"type": "http_json", "url": "https://api.x.example/",
                      "json_body": {"pageIdx": "{{page}}"}},
           "pagination": {"strategy": "template", "max_pages": 5}}
    with pytest.raises(Exception):
        validate(cfg)
