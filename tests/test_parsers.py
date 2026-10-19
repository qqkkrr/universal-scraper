# -*- coding: utf-8 -*-
"""ConfigParser：HTML row_css+fields、JSON records_path+path、字段映射。"""
from universal_scraper.protocols import Request, Response, ParseContext
from universal_scraper.modules.parsers import ConfigParser

HTML = """
<html><body>
  <div class="item"><a class="t">Alpha</a><span class="p">10</span></div>
  <div class="item"><a class="t">Beta</a><span class="p">20</span></div>
</body></html>
"""

def _resp(text="", json=None):
    return Response(request=Request(url="https://example.com/"), status=200,
                    body=text.encode("utf-8"), text=text, json=json, url="https://example.com/")

def _ctx():
    class DummyTask:
        name = "t"
        root = "/tmp"
    return ParseContext(DummyTask(), {"parsers": {}}, {})

def test_html_rows():
    cfg = {"type": "html", "row_css": ".item",
           "fields": {"title": {"css": ".t::text"}, "price": {"css": ".p::text"}}}
    out = ConfigParser(cfg, {}).parse(_resp(text=HTML), _ctx())
    items = out.items
    assert len(items) == 2
    assert items[0]["title"] == "Alpha" and items[0]["price"] == "10"
    assert items[1]["title"] == "Beta"

def test_html_no_rows_when_selector_misses():
    cfg = {"type": "html", "row_css": ".nope",
           "fields": {"title": {"css": ".t::text"}}}
    out = ConfigParser(cfg, {}).parse(_resp(text=HTML), _ctx())
    assert out.items == []

def test_json_records_path():
    cfg = {"type": "json", "records_path": "data.list",
           "fields": {"name": {"path": "name"}, "n": {"path": "n"}}}
    j = {"data": {"list": [{"name": "A", "n": 1}, {"name": "B", "n": 2}]}}
    out = ConfigParser(cfg, {}).parse(_resp(json=j), _ctx())
    assert len(out.items) == 2 and out.items[0]["name"] == "A"
