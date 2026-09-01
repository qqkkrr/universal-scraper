# -*- coding: utf-8 -*-
"""WebUI 图书目录任务语义测试：OK/PARTIAL/全失败/坏 spec 都要如实上报，不假成功。

夹具自包含（与 tests/test_mcp_books.py 平行），避免测试模块间互相依赖。
"""
import json
import sys
import types

import pytest

import universal_scraper.book_catalog as book_catalog
import universal_scraper.webui as webui

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
    monkeypatch.setattr(webui, "_persist_jobs", lambda: None)
    monkeypatch.setattr(webui, "_auto_verify", lambda rows, cfg: {"ok": True, "checks": []})
    # 解决方案附加依赖 solutions 匹配表，与本测试无关；打桩避免真实调用
    sol = types.ModuleType("fake_solutions")
    sol.attach_solution = lambda job, text="": None
    monkeypatch.setitem(sys.modules, "universal_scraper.solutions", sol)


def _capture(job, monkeypatch):
    cap = {}
    monkeypatch.setattr(webui, "_job_done",
                        lambda j, result, summary=None, verify=None: cap.update(
                            {"done": result, "summary": summary}))
    monkeypatch.setattr(webui, "_job_error",
                        lambda j, err: cap.update({"error": err}))
    return cap


def test_books_job_ok(tmp_path, monkeypatch):
    job = {"kind": "books", "messages": [], "task_dir": ""}
    cap = _capture(job, monkeypatch)
    webui.run_books_job(job, json.dumps(SPEC_OK), str(tmp_path / "ok"), False, 0)
    assert "error" not in cap
    assert cap["done"]["total"] == 1 and cap["done"]["errors"] == 0
    assert cap["summary"].startswith("✅") and "1 本书" in cap["summary"]
    assert (tmp_path / "ok" / "booklist.csv").exists()


def test_books_job_partial_reports_warning_with_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(book_catalog, "_default_fetch_html", _fake_fetch_factory(
        dangdang_html="<html><body>没有找到商品</body></html>"))
    job = {"kind": "books", "messages": [], "task_dir": ""}
    cap = _capture(job, monkeypatch)
    webui.run_books_job(job, json.dumps(SPEC_OK), str(tmp_path / "p"), False, 0)
    assert "error" not in cap
    assert cap["done"]["errors"] == 1
    assert cap["summary"].startswith("⚠️")
    assert "行动方案" in cap["summary"] and "crawl_log.md" in cap["summary"]


def test_books_job_all_sources_fail_is_error_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(book_catalog, "_default_fetch_html", _fake_fetch_factory(
        subject_html=EMPTY_HTML, buylinks_html=EMPTY_HTML,
        search_html=EMPTY_SEARCH, dangdang_html=EMPTY_HTML))
    job = {"kind": "books", "messages": [], "task_dir": ""}
    cap = _capture(job, monkeypatch)
    webui.run_books_job(job, json.dumps(SPEC_OK), str(tmp_path / "f"), False, 0)
    assert "error" in cap and "0 条有效结果" in cap["error"]
    assert "ISBN" in cap["error"], "失败必须给行动方案"
    assert "done" not in cap


def test_books_job_invalid_spec_is_error(tmp_path, monkeypatch):
    job = {"kind": "books", "messages": [], "task_dir": ""}
    cap = _capture(job, monkeypatch)
    webui.run_books_job(job, '{"books": "不是数组"}', str(tmp_path / "bad"), False, 0)
    assert "error" in cap and "正确结构" in cap["error"]
    assert "done" not in cap


def test_books_job_broken_json_gives_actionable_error(tmp_path, monkeypatch):
    job = {"kind": "books", "messages": [], "task_dir": ""}
    cap = _capture(job, monkeypatch)
    webui.run_books_job(job, "{books:", str(tmp_path / "x"), False, 0)
    assert "error" in cap and "JSON" in cap["error"] and "示例" in cap["error"]


def test_books_job_accepts_spec_file_path(tmp_path, monkeypatch):
    sp = tmp_path / "my.spec.json"
    sp.write_text(json.dumps(SPEC_OK), encoding="utf-8")
    job = {"kind": "books", "messages": [], "task_dir": ""}
    cap = _capture(job, monkeypatch)
    webui.run_books_job(job, str(sp), str(tmp_path / "out2"), False, 0)
    assert "error" not in cap
    assert any("已从文件读取 spec" in m for m in job["messages"])
    assert cap["done"]["total"] == 1


def test_books_job_duplicate_isbn_skip_logged(tmp_path, monkeypatch):
    spec = {"name": "含重复", "books": [
        {"title": "Test Book", "isbn": "9787508648330", "douban_subject_id": "123"},
        {"title": "Test Book 副本", "isbn": "9787508648330"},
    ]}
    job = {"kind": "books", "messages": [], "task_dir": ""}
    cap = _capture(job, monkeypatch)
    webui.run_books_job(job, json.dumps(spec), str(tmp_path / "dup"), False, 0)
    assert "error" not in cap and cap["done"]["total"] == 1
    assert any("去重跳过" in m for m in job["messages"])


def test_books_fixture_sanity():
    """夹具自检：页面常量必须非空（防误改共享夹具）。"""
    assert DETAIL_HTML and BUY_HTML and DANGDANG_HTML and EMPTY_HTML


SPEC_TWO = {"name": "两本书", "books": [
    {"title": "Test Book", "isbn": "9787508648330", "douban_subject_id": "123"},
    {"title": "Another Book", "isbn": "9787563394180", "douban_subject_id": "4230237"},
]}


def test_books_job_manual_stop_midway_saves_partial_and_is_honest(tmp_path, monkeypatch):
    """用户中途停止：已完成的书照常导出，摘要必须写明「已手动停止+剩余未抓」，不冒充完整结果。"""
    import threading
    ev = threading.Event()
    base = _fake_fetch_factory()
    calls = {"n": 0}

    def fetch_with_cancel(url):
        calls["n"] += 1
        if calls["n"] >= 3:
            # 第一本书的最后一抓完成即置位 → 第二本书在循环开头被拦截
            ev.set()
        return base(url)

    monkeypatch.setattr(book_catalog, "_default_fetch_html", fetch_with_cancel)
    job = {"kind": "books", "messages": [], "task_dir": "", "cancel_event": ev}
    cap = _capture(job, monkeypatch)
    webui.run_books_job(job, json.dumps(SPEC_TWO), str(tmp_path / "stop"), False, 0)
    done = cap.get("done")
    assert done is not None and done.get("stopped") is True
    assert done["total"] == 1
    assert cap["summary"].startswith("⏹") and "手动停止" in cap["summary"]
    assert "剩余书目未抓取" in cap["summary"]


def test_books_job_stop_before_any_book_reports_honestly(tmp_path, monkeypatch):
    import threading
    job = {"kind": "books", "messages": [], "task_dir": ""}
    ev = threading.Event()
    ev.set()  # 一本书都还没采就点了停止
    job["cancel_event"] = ev
    cap = _capture(job, monkeypatch)
    webui.run_books_job(job, json.dumps(SPEC_OK), str(tmp_path / "stop0"), False, 0)
    assert "error" in cap
    assert "手动停止" in cap["error"], "停止导致的 0 行不得误归因为书单为空"
    assert cap.get("done") is None
