"""第六轮反馈回归：jpath 按名过滤 / detail POST+JSON 网关 / template 翻页策略。"""
import json

from universal_scraper.selectors import jpath
from universal_scraper.engine import fetch_detail_row
from universal_scraper.config import validate


def test_jpath_name_filter():
    obj = {"spec": [{"name": "主材", "value": "PP"}, {"name": "尺寸", "value": "20寸"}]}
    assert jpath(obj, "spec[name=主材].value") == "PP"
    assert jpath(obj, "spec[name=尺寸].value") == "20寸"
    assert jpath(obj, "spec[name=不存在].value") is None


def test_jpath_index_still_works():
    obj = {"list": [{"a": 1}, {"a": 2}]}
    assert jpath(obj, "list[0].a") == 1
    assert jpath(obj, "list[*].a") == [1, 2]


def test_detail_post_json_gateway():
    captured = {}

    class FakeHttp:
        def post(self, url, json_data=None, **kw):
            captured["url"] = url
            captured["body"] = json_data
            return {"status": 200,
                    "text": json.dumps({"data": {"score": 4.8,
                                                 "spec": [{"name": "主材", "value": "PP"}]}})}

    detail = {"url_field": "商品ID", "method": "POST",
              "json_body": {"itemId": "{商品ID}"}, "type": "http_json",
              "extract": [{"name": "评分", "type": "json", "path": "data.score"},
                          {"name": "主材", "type": "json", "path": "data.spec[name=主材].value"}]}
    row = {"商品ID": "123"}
    out = fetch_detail_row(FakeHttp(), row, detail)
    assert captured["body"] == {"itemId": "123"}, "json_body 模板未按行插值"
    assert out["评分"] == 4.8
    assert out["主材"] == "PP"
    assert out["detail_status"] == "200"


def test_detail_get_backward_compat():
    class FakeHttp:
        def get(self, url, allow_html_404=True, **kw):
            return {"status": 200, "text": "<html><h1>正文</h1></html>"}

    detail = {"url_field": "url", "extract": [{"name": "标题", "type": "xpath_text", "xpath": "//h1"}]}
    out = fetch_detail_row(FakeHttp(), {"url": "https://x.example/1"}, detail)
    assert "正文" in str(out.get("标题"))


def test_template_strategy_validates():
    cfg = {"name": "t",
           "source": {"type": "http_json", "method": "POST", "url": "https://api.x.example/gateway",
                      "json_body": {"pageIdx": "{{page}}", "pageSize": 20}},
           "pagination": {"strategy": "template", "max_pages": 5, "records_path": "data.list"}}
    assert validate(cfg) == cfg
