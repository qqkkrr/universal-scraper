# -*- coding: utf-8 -*-
"""MCP books 工具离线测试：注册、真实调用（假网络）、0 结果/坏 spec 绝不假成功。"""
import json

import pytest

import universal_scraper.book_catalog as book_catalog
import universal_scraper.mcp_server as mcp

DETAIL_HTML = """
<html><body>
<div id="wrapper"><h1>Test Book</h1></div>
<div id="info">
  <span>作者: Author A</span><br>
  <span class="pl">出版社:</span><a href="#">Pub</a><br>
  <span class="pl">出版年:</span> 2020-01-01<br>
  <span class="pl">ISBN:</span> 9787508648330<br>
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
</body></html>
"""

SEARCH_HTML = """
<html><body>
<script>window.__DATA__ = {"total":1,"items":[
 {"id":123,"title":"Test Book","abstract":"A / Pub / 9787508648330 / 2020",
  "cover_url":"https://img.example.com/s.jpg","rating":{"value":8.8,"count":100},
  "url":"https://book.douban.com/subject/123/"}
]};</script>
</body></html>
"""

DANGDANG_HTML = """
<html><body><ul class="bigimg"><li>
<a href="//product.dangdang.com/101.html">商品</a>
<h6 class="title">Test Book 9787508648330</h6>
<strong class="price">¥59.00</strong>
</li></ul></body></html>
"""

EMPTY_HTML = "<html><body></body></html>"
EMPTY_SEARCH = 'window.__DATA__ = {"items":[],"total":0};'

SPEC_OK = {"name": "测试书单", "books": [
    {"title": "Test Book", "isbn": "9787508648330", "douban_subject_id": "123"},
]}


def _fake_fetch_factory(subject_html=DETAIL_HTML, buylinks_html=BUY_HTML,
                        search_html=SEARCH_HTML, dangdang_html=DANGDANG_HTML):
    def fetch(url):
        if "subject_search" in url:
            return search_html
        if "/buylinks" in url:
            return buylinks_html
        if "search.dangdang.com" in url:
            return dangdang_html
        if "/subject/" in url:
            return subject_html
        return ""
    return fetch


@pytest.fixture(autouse=True)
def offline_book_net(monkeypatch):
    monkeypatch.setattr(book_catalog, "_default_fetch_html", _fake_fetch_factory())
    monkeypatch.setattr(book_catalog, "_default_download_cover", lambda url, path: False)
    monkeypatch.setattr(book_catalog.time, "sleep", lambda s: None)


def test_books_tool_registered():
    names = {t["name"] for t in mcp.TOOLS}
    assert {"books", "scrape", "auto", "crawl", "extract", "check"} <= names
    assert mcp.TOOL_IMPLS["books"] is mcp.tool_books


def test_books_call_happy_path(tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(SPEC_OK, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "cat"
    r = mcp.tool_books({"spec_path": str(spec_path), "out": str(out),
                        "download_covers": False, "interval": 0})
    assert r["status"] == "OK"
    assert r["total"] == 1
    assert r["sample"][0]["isbn"] == "9787508648330"
    assert r["sample"][0]["jd_price"] == "12.34"
    assert r["sample"][0]["douban_rating"] == "8.8"
    assert (out / "booklist.csv").exists()
    assert (out / "booklist.md").exists()
    assert (out / "books.json").exists()
    # MCP 报文层：isError 必须为 False，content JSON 与工具结果一致
    msg = mcp.handle_message({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                              "params": {"name": "books",
                                         "arguments": {"spec_path": str(spec_path),
                                                       "out": str(out / "again"),
                                                       "download_covers": False,
                                                       "interval": 0}}})
    payload = json.loads(msg["result"]["content"][0]["text"])
    assert payload["total"] == 1
    assert msg["result"]["isError"] is False


def test_books_bad_spec_is_error_not_success():
    r = mcp.tool_books({"spec": {"books": [{"title": "无 ISBN"}]}})
    assert "error" in r
    assert "ISBN" in r["error"]
    msg = mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "books",
                                         "arguments": {"spec": {"books": [{"title": "无 ISBN"}]}}}})
    assert msg["result"]["isError"] is True


def test_books_all_sources_fail_reports_failure_not_success(tmp_path, monkeypatch):
    """豆瓣详情/在哪儿买/当当全部拿不到数据：必须输出 error + 行动方案，不得返回成功语义。"""
    monkeypatch.setattr(book_catalog, "_default_fetch_html", _fake_fetch_factory(
        subject_html=EMPTY_HTML, buylinks_html=EMPTY_HTML,
        search_html=EMPTY_SEARCH, dangdang_html=EMPTY_HTML))
    out = tmp_path / "fail"
    r = mcp.tool_books({"spec": SPEC_OK, "out": str(out), "download_covers": False, "interval": 0})
    assert "error" in r, "全源失败必须按失败报告，不能伪装成功"
    assert "诊断" in r["error"]
    msg = mcp.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": "books",
                                         "arguments": {"spec": SPEC_OK, "out": str(out / "again"),
                                                       "download_covers": False, "interval": 0}}})
    assert msg["result"]["isError"] is True


def test_books_partial_run_carries_warning_and_diagnostics(tmp_path, monkeypatch):
    """当当搜索 0 条（其余正常）：不算失败但也不得静默——要有 warning 与逐行诊断。"""
    monkeypatch.setattr(book_catalog, "_default_fetch_html", _fake_fetch_factory(
        dangdang_html="<html><body>没有找到商品</body></html>"))
    r = mcp.tool_books({"spec": SPEC_OK, "out": str(tmp_path / "p"), "interval": 0})
    assert r["status"] == "PARTIAL"
    assert "warning" in r and "未取全字段" in r["warning"]
    assert r["problem_rows"], "PARTIAL 行必须带逐行诊断"
    # RPC 层：PARTIAL 是“成功但有警告”，不得被标成工具错误（诚实性规则的另一半）
    msg = mcp.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "books",
                                         "arguments": {"spec": SPEC_OK,
                                                       "out": str(tmp_path / "p2"), "interval": 0}}})
    assert msg["result"]["isError"] is False
    assert "warning" in json.loads(msg["result"]["content"][0]["text"])


def test_books_interval_zero_is_respected(tmp_path, monkeypatch):
    """显式 interval:0 不得被吞成默认值；非法/超界值钳制到 [0,10]。"""
    seen = {}

    def fake_build(spec, out_dir, *, download_covers=True, min_interval=1.0):
        seen["interval"] = min_interval
        return {"status": "INVALID_SPEC", "error": "stop-here", "rows": [], "files": {}}

    # tool_books 在调用时才 from .book_catalog import build_catalog，patch 来源模块即生效
    monkeypatch.setattr(book_catalog, "build_catalog", fake_build)
    r = mcp.tool_books({"spec": SPEC_OK, "out": str(tmp_path / "i"), "interval": 0})
    assert r["error"] == "stop-here"
    assert seen["interval"] == 0.0, "显式 0 必须原样传递"
    mcp.tool_books({"spec": SPEC_OK, "out": str(tmp_path / "i"), "interval": 600})
    assert seen["interval"] == 10.0, "上限必须钳制到 10 秒"


def test_books_interval_nan_falls_back_to_default(monkeypatch):
    """NaN 不得穿过钳制变成 0 秒节流，必须回落默认值。"""
    assert mcp._clamp_interval(float("nan"), 1.0) == 1.0
    assert mcp._clamp_interval("not-a-number") == 1.0
    assert mcp._clamp_interval(None) == 1.0
    assert mcp._clamp_interval(3.5) == 3.5


def test_books_duplicate_isbn_skip_is_visible(tmp_path):
    """书单里列重复 ISBN：主键去重必须显式上报（duplicates_skipped），不能静默少行。"""
    spec = {"name": "含重复", "books": [
        {"title": "Test Book", "isbn": "9787508648330", "douban_subject_id": "123"},
        {"title": "Test Book 副本", "isbn": "9787508648330"},
    ]}
    r = mcp.tool_books({"spec": spec, "out": str(tmp_path / "dup"), "interval": 0})
    assert r["total"] == 1
    assert r.get("duplicates_skipped") == 1
    assert "去重" in r.get("notice", "")


def test_books_bad_inline_spec_shape_gives_precise_error():
    """内联 spec 结构不对时，必须透传 build_catalog 的精确诊断而不是“没提供 spec”。"""
    r = mcp.tool_books({"spec": {"books": []}})
    assert "error" in r and "不能为空" in r["error"]
    r2 = mcp.tool_books({"spec": {"note": "没有 books"}})
    assert "error" in r2 and "books" in r2["error"]


def test_books_spec_path_missing_is_actionable_error():
    r = mcp.tool_books({"spec_path": "/no/such/file.json"})
    assert "error" in r and "不存在" in r["error"]
