"""cookies 串/dict 契约 + pipeline transform/template 回归（GLM B站实战反馈）。"""
from universal_scraper.core import _norm_cookies, HttpClient
from universal_scraper.engine import run_pipeline


def test_norm_cookies_accepts_string():
    c = _norm_cookies("buvid=abc123; SESSID = x/y ; badpair")
    assert c["buvid"] == "abc123"
    assert c["SESSID"] == "x/y"
    assert "badpair" not in c


def test_norm_cookies_accepts_dict_and_empty():
    assert _norm_cookies({"a": 1}) == {"a": "1"}
    assert _norm_cookies("") == {}
    assert _norm_cookies(None) == {}


def test_http_client_accepts_cookie_string():
    cli = HttpClient(cookies="buvid=abc; sid=9")
    assert cli.cookies["buvid"] == "abc"
    assert cli.cookies["sid"] == "9"


def test_transform_unix_to_datetime():
    rows = [{"pubdate": 1756684800}, {"pubdate": "1756684800000"}, {"pubdate": ""}]
    out = run_pipeline(rows, [{"type": "transform", "field": "pubdate",
                               "op": "unix_to_datetime", "fmt": "%Y-%m-%d"}])
    assert out[0]["pubdate"].startswith("2025-09-01")
    assert out[1]["pubdate"].startswith("2025-09-01")  # 毫秒自适应
    assert out[2]["pubdate"] == ""  # 空值不炸


def test_template_builds_url():
    rows = [{"BV号": "BV1xx"}, {"BV号": "BV2yy"}]
    out = run_pipeline(rows, [{"type": "template", "field": "链接",
                               "tmpl": "https://www.bilibili.com/video/{BV号}"}])
    assert out[0]["链接"] == "https://www.bilibili.com/video/BV1xx"
    assert out[1]["链接"] == "https://www.bilibili.com/video/BV2yy"
