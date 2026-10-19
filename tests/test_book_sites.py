# -*- coding: utf-8 -*-
"""精配注册表：书籍类 URL 必须路由到专门解析器，避免被通用豆瓣覆盖。"""
from universal_scraper.sites import match_site, parse_site_html

DETAIL = """
<html><body><div id="wrapper"><h1>X</h1></div><div id="info">
<span class="pl">ISBN:</span> 9787111111111<br></div>
<div id="mainpic"><img src="https://img.example/x.jpg"></div></body></html>
"""

BUY = """
<html><body><a href="https://book.douban.com/link2/?vendor=jingdong&amp;price=1000&amp;url=x">JD</a></body></html>
"""


def test_match_book_urls():
    assert match_site("https://book.douban.com/subject/1/") == "douban_book_detail"
    assert match_site("https://book.douban.com/subject/1/buylinks") == "douban_book_buylinks"
    assert match_site("https://book.douban.com/subject_search?cat=1001&search_text=x") == "douban_book_search"
    assert match_site("https://search.dangdang.com/?key=978") == "dangdang_search"


def test_parse_site_html_dispatch_details():
    rows = parse_site_html("https://book.douban.com/subject/1/", DETAIL)
    assert rows and rows[0]["isbn"] == "9787111111111"
    assert rows[0]["_site"] == "douban_book_detail"


def test_parse_site_html_dispatch_buylinks():
    rows = parse_site_html("https://book.douban.com/subject/1/buylinks", BUY)
    assert rows and rows[0]["jd_price"] == "10.00"
    assert rows[0]["_site"] == "douban_book_buylinks"


SEARCH_JSON = 'window.__DATA__ = {"items":[{"id":1,"title":"Book","abstract":"[A / Pub / 9787508648330 / 2020]","url":"https://book.douban.com/subject/1/"}],"error_info":""};'
DANGDANG = '<html><body><ul class="bigimg"><li><a href="//product.dangdang.com/1.html">b</a><h6 class="title">Book</h6><strong class="price">¥1.00</strong></li></ul></body></html>'


def test_parse_site_html_dispatch_search_and_dangdang():
    rows = parse_site_html("https://book.douban.com/subject_search?cat=1001&search_text=x", SEARCH_JSON)
    assert rows and rows[0]["douban_subject_id"] == "1"
    assert rows[0]["_site"] == "douban_book_search"

    rows = parse_site_html("https://search.dangdang.com/?key=9787508648330", DANGDANG)
    assert rows and rows[0]["dangdang_link"] == "https://product.dangdang.com/1.html"
    assert rows[0]["_site"] == "dangdang_search"
