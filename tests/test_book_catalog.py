# -*- coding: utf-8 -*-
"""图书目录模块离线测试：解析器 + build_catalog 合并/诊断/导出。"""
from universal_scraper.book_catalog import (
    _looks_like_image,
    build_catalog,
    norm_isbn,
    parse_dangdang_search,
    parse_douban_book_buylinks,
    parse_douban_book_detail,
    parse_douban_book_search,
)

DETAIL_HTML = """
<html><body>
<div id="wrapper"><h1>Test Book</h1></div>
<div id="info">
  <span>作者: Author A</span><br>
  <span>译者: Translator B</span><br>
  <span class="pl">出版社:</span><a href="#">Pub</a><br>
  <span class="pl">出版年:</span> 2020-01-01<br>
  <span class="pl">ISBN:</span> 978-7-5086-4833-0<br>
  <span class="pl">页数:</span> 123<br>
  <span class="pl">装帧:</span> 平装<br>
  <span class="pl">定价:</span> 59.00元<br>
</div>
<div id="link-report"><span class="intro">A useful introduction.</span></div>
<div id="mainpic"><img src="//img.example.com/cover.jpg"></div>
<strong property="v:average">8.8</strong>
<span property="v:votes">100</span>
</body></html>
"""

BUY_HTML = """
<html><body>
<a href="https://book.douban.com/link2/?vendor=jingdong&amp;price=1234&amp;url=https%3A%2F%2Fjdc%2Fx" target="_blank">京东</a>
<a href="https://book.douban.com/link2/?vendor=dangdang&amp;price=9876&amp;url=https%3A%2F%2Fdd%2Fx" target="_blank">当当</a>
<a href="https://book.douban.com/link2/?vendor=bookschina&amp;price=1000&amp;url=x">中图</a>
</body></html>
"""

SEARCH_HTML = """
<html><body>
<script>
window.__DATA__ = {"total":1,"items":[
 {"id":123,"title":"Test Book","abstract":"[美] A / Pub / 2020 / 59.00","cover_url":"https://img.example.com/s.jpg",
  "rating":{"value":8.8,"count":100},"url":"https://book.douban.com/subject/123/"}
]};
</script>
</body></html>
"""

DANGDANG_HTML = """
<html><body>
<ul class="bigimg">
  <li>
    <a href="//product.dangdang.com/101.html">商品</a>
    <h6 class="title">Test Book 9787508648330</h6>
    <strong class="price">¥59.00</strong>
    <p>定价：¥68.00</p>
    <a href="//shop.dangdang.com/1">示例书店</a>
  </li>
</ul>
</body></html>
"""

DANGDANG_EMPTY = """
<html><body><div>没有找到商品</div></body></html>
"""


def _fake_fetch(html_by_kind):
    def fetch(url):
        if "subject_search" in url:
            return SEARCH_HTML
        if "/buylinks" in url:
            return BUY_HTML
        if "search.dangdang.com" in url:
            return DANGDANG_HTML
        if "/subject/" in url:
            return DETAIL_HTML
        return ""
    return fetch


def test_norm_isbn():
    assert norm_isbn("978-7-5086-4833-0") == "9787508648330"
    assert norm_isbn(" 9780061766084 ") == "9780061766084"


def test_parse_douban_detail():
    rows = parse_douban_book_detail(DETAIL_HTML, "https://book.douban.com/subject/123/")
    assert len(rows) == 1
    r = rows[0]
    assert r["isbn"] == "9787508648330"
    assert r["douban_title"] == "Test Book"
    assert r["publisher"] == "Pub"
    assert r["douban_rating"] == "8.8"
    assert r["douban_rating_count"] == "100"
    assert r["douban_cover_url"] == "https://img.example.com/cover.jpg"


def test_parse_douban_buylinks():
    r = parse_douban_book_buylinks(BUY_HTML, "https://book.douban.com/subject/123/buylinks")[0]
    assert r["jd_price"] == "12.34"
    assert r["jd_union_url"].startswith("https://jdc/")
    assert r["dangdang_price"] == "98.76"
    assert r["other_vendors"] == "bookschina"
    assert r["buylinks_status"] == "OK"


def test_parse_douban_search_and_dangdang():
    assert parse_douban_book_search(SEARCH_HTML)[0]["douban_subject_id"] == "123"
    dd = parse_dangdang_search(DANGDANG_HTML, "https://search.dangdang.com/?key=9787508648330")[0]
    assert dd["dangdang_price"] == "59.00"
    assert dd["dangdang_link"] == "https://product.dangdang.com/101.html"
    assert dd["dangdang_seller"] == "示例书店"
    empty = parse_dangdang_search(DANGDANG_EMPTY, "https://search.dangdang.com/?key=x")[0]
    assert empty["dangdang_status"] == "NO_RESULT"


def test_build_catalog_writes_three_files(tmp_path):
    spec = {"books": [{
        "group": "g", "title": "Test Book", "isbn": "9787508648330",
        "douban_subject_id": "123", "language": "中文", "edition": "1"
    }]}
    out = tmp_path / "out"
    res = build_catalog(spec, out, fetch_html=_fake_fetch(None),
                        download_covers=False, min_interval=0)
    assert res["status"] == "OK"
    assert res["total"] == 1
    assert res["rows"][0]["isbn"] == "9787508648330"
    assert res["rows"][0]["jd_price"] == "12.34"
    assert res["rows"][0]["dangdang_price"] == "59.00"
    # 缺失率：目录内容存在时不应超过 100%，且必然小于 20%（少量 N/A）
    assert float(res["coverage"]["required_missing_rate"].rstrip("%")) < 20
    assert (out / "booklist.csv").exists()
    assert (out / "booklist.md").exists()
    assert (out / "crawl_log.md").exists()


def test_build_catalog_no_subject_id_uses_search(tmp_path):
    spec = {"books": [{"title": "Test Book", "isbn": "9787508648330"}]}
    res = build_catalog(spec, tmp_path / "out2", fetch_html=_fake_fetch(None),
                        download_covers=False, min_interval=0)
    assert res["rows"][0]["douban_subject_url"].endswith("/subject/123/")


def test_build_catalog_zero_result_is_no_data_not_fake_success(tmp_path):
    def bad(url):
        if "search.dangdang.com" in url:
            return DANGDANG_EMPTY
        return ""
    spec = {"books": [{"title": "Missing", "isbn": "9787508648330",
                       "douban_subject_id": "999"}]}
    res = build_catalog(spec, tmp_path / "out3", fetch_html=bad,
                        download_covers=False, min_interval=0)
    assert res["rows"][0]["status"] == "NO_DATA"
    assert "失败" in res["rows"][0]["diagnostics"] or "豆瓣书目字段为空" in res["rows"][0]["diagnostics"]
    assert len(res["files"]) == 4


def test_build_catalog_dedup_by_isbn_spec_bad_primary(tmp_path):
    spec = {"books": [
        {"title": "A", "isbn": "9787508648330", "douban_subject_id": "1"},
        {"title": "B", "isbn": "9787508648330", "douban_subject_id": "2"},
    ]}
    res = build_catalog(spec, tmp_path / "out4", fetch_html=_fake_fetch(None),
                        download_covers=False, min_interval=0)
    assert res["total"] == 1
    assert res["coverage"]["duplicates"] == 1
    assert any(d["status"] == "SKIPPED_DUPLICATE" for d in res["diagnostics"])

    bad = build_catalog({"books": [{"title": "No isbn"}]}, tmp_path / "out5",
                        fetch_html=_fake_fetch(None), download_covers=False, min_interval=0)
    assert bad["status"] == "INVALID_SPEC"
    assert "ISBN" in bad["error"]


BUY_NO_JD = """
<html><body>
<a href="https://book.douban.com/link2/?vendor=dangdang&amp;price=9876&amp;url=https%3A%2F%2Fdd%2Fx">当当</a>
</body></html>
"""


def _fake_no_jd(url):
    if "/buylinks" in url:
        return BUY_NO_JD
    if "search.dangdang.com" in url:
        return DANGDANG_EMPTY
    if "/subject/" in url:
        return DETAIL_HTML
    return ""


def test_login_wall_and_field_diagnostics(tmp_path):
    spec = {"books": [{"title": "Test Book", "isbn": "9787508648330",
                       "douban_subject_id": "123"}]}
    res = build_catalog(spec, tmp_path / "login", fetch_html=_fake_no_jd,
                        download_covers=False, min_interval=0)
    row = res["rows"][0]
    assert row["status"] == "PARTIAL"
    assert "LOGIN_WALL" in row["diagnostics"]
    assert any(d["code"] == "LOGIN_WALL" and d["field"] == "jd_price" for d in row["diagnostic_fields"])
    assert any(d["code"] == "NO_RESULT" for d in row["diagnostic_fields"])
    assert any(d["code"] == "LOGIN_WALL" for d in res["diagnostics"][0]["field_diagnostics"])


def test_cover_failure_degrades_file_but_keeps_url(tmp_path):
    spec = {"books": [{"title": "Test Book", "isbn": "9787508648330",
                       "douban_subject_id": "123"}]}
    res = build_catalog(spec, tmp_path / "cover", fetch_html=_fake_fetch(None),
                        download_cover=lambda *a: False, download_covers=True,
                        min_interval=0)
    row = res["rows"][0]
    assert row["cover_file"] == "N/A"
    assert row["douban_cover_url"].startswith("https://")
    assert "COVER_DOWNLOAD_FAILED" in row["diagnostics"]


def test_required_missing_excludes_optional_retail_fields(tmp_path):
    res = build_catalog({"books": [{"title": "Test Book", "isbn": "9787508648330",
                                    "douban_subject_id": "123"}]},
                        tmp_path / "required", fetch_html=_fake_no_jd,
                        download_covers=False, min_interval=0)
    assert res["coverage"]["required_missing"] == 0
    assert float(res["coverage"]["required_missing_rate"].rstrip("%")) < 20


def test_dangdang_root_relative_link_uses_dangdang_base():
    html = '<html><body><ul class="bigimg"><li><a href="/root-relative.html">x</a><h6 class="title">Book</h6><strong class="price">¥10.00</strong></li></ul></body></html>'
    row = parse_dangdang_search(html, "https://search.dangdang.com/?key=9787508648330")[0]
    assert row["dangdang_link"] == "https://product.dangdang.com/root-relative.html"


def test_search_rate_limit_and_malformed_json():
    rate = parse_douban_book_search(
        'window.__DATA__ = {"error_info":"搜索访问太频繁。","items":[]};',
        "https://book.douban.com/subject_search?cat=1001&search_text=x")
    assert rate[0]["douban_search_status"] == "RATE_LIMITED"
    # 损坏 JSON 不抛异常，返回空列表
    assert parse_douban_book_search('window.__DATA__ = {broken', "https://example.com") == []


def test_cover_fake_html_rejected(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"<html>" + b"x" * 200 + b"</html>")
    bad_good = tmp_path / "good.jpg"
    bad_good.write_bytes(b"\xff\xd8\xff" + b"x" * 200)
    assert not _looks_like_image(bad)
    assert _looks_like_image(bad_good)


def test_empty_books_invalid():
    res = build_catalog({"books": []}, "/tmp/should-not-write", fetch_html=lambda u: "",
                        download_covers=False, min_interval=0)
    assert res["status"] == "INVALID_SPEC"
    assert "不能为空" in res["error"]


def test_buylinks_href_dedup():
    html = BUY_HTML.replace("vendor=jingdong", "vendor=jingdong")
    html = html.replace("https://jdc/x", "https://jdc/x") + html[-8:]  # noop
    # 构造两次同一 href
    html = ('<a href="https://book.douban.com/link2/?vendor=jingdong&amp;price=1234&amp;url=x">JD</a>'
            '<a href="https://book.douban.com/link2/?vendor=jingdong&amp;price=1234&amp;url=x">JD</a>')
    row = parse_douban_book_buylinks(html, "https://book.douban.com/subject/123/buylinks")[0]
    assert row["jd_price"] == "12.34"


def test_books_json_contains_top_level_diagnostics(tmp_path):
    build_catalog({"books": [{"title": "Test Book", "isbn": "9787508648330",
                              "douban_subject_id": "123"}]},
                  tmp_path / "jsoncheck", fetch_html=_fake_fetch(None),
                  download_covers=False, min_interval=0)
    data = __import__("json").loads((tmp_path / "jsoncheck" / "books.json").read_text(encoding="utf-8"))
    assert "diagnostics" in data
    assert data["diagnostics"][0]["isbn"] == "9787508648330"


def test_empty_pages_never_fake_success(tmp_path):
    """抓取返回空页面时，必须产生 NO_DATA/LOGIN_WALL/NO_RESULT 诊断，绝不显示成功。"""
    def empty(url):
        return ""
    res = build_catalog({"books": [{"title": "Ghost", "isbn": "9787508648330",
                                    "douban_subject_id": "999999"}]},
                        tmp_path / "empty", fetch_html=empty,
                        download_covers=False, min_interval=0)
    row = res["rows"][0]
    assert row["status"] == "NO_DATA"
    codes = {d["code"] for d in row["diagnostic_fields"]}
    assert {"NO_DATA", "LOGIN_WALL", "NO_RESULT"} <= codes


def test_build_catalog_should_stop_between_books(tmp_path):
    """should_stop 在每本书开始前生效：停止后已完成行照常导出，绝不假完成。"""
    spec = {"name": "两本书", "books": [
        {"title": "Book A", "isbn": "9787508648330", "douban_subject_id": "123"},
        {"title": "Book B", "isbn": "9787563394180", "douban_subject_id": "4230237"},
    ]}

    def fetch(url):
        return ""

    # 第 1 次检查（书 A 前）放行，第 2 次（书 B 前）拦截
    checks = {"n": 0}

    def should_stop():
        checks["n"] += 1
        return checks["n"] > 1

    res = build_catalog(spec, str(tmp_path / "out"), fetch_html=fetch,
                        download_covers=False, min_interval=0.0, should_stop=should_stop)
    assert res["total"] == 1, "第二本必须在循环开头被拦截"
    assert res["stopped"] is True

    res2 = build_catalog(spec, str(tmp_path / "out2"), fetch_html=fetch,
                         download_covers=False, min_interval=0.0, should_stop=None)
    assert res2["total"] == 2 and res2["stopped"] is False
