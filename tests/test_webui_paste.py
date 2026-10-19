# -*- coding: utf-8 -*-
"""贴网页模式的真实结果语义测试：links/table/article 都要按“条数”报告，3 个模式不得互相串线。"""
import universal_scraper.webui as webui
import universal_scraper.quick as quick


def _capture_done(monkeypatch, job):
    done = {}
    monkeypatch.setattr(webui, "_job_done",
                        lambda j, result, summary=None, verify=None: done.update(
                            {"result": result, "summary": summary, "verify": verify}))
    monkeypatch.setattr(webui, "_export_rows", lambda rows, base: {"json": f"outputs/{base}.json"})
    monkeypatch.setattr(webui, "_persist_jobs", lambda: None)
    monkeypatch.setattr(webui, "_auto_verify", lambda rows, cfg: None)
    return done


def test_paste_links_mode_uses_links_not_article(monkeypatch):
    job = {"kind": "paste", "messages": [], "task_dir": ""}

    def fake_fetch(url, **kw):
        return {"url": url, "status": 200, "text": "<html>正文</html>",
                "links": ["https://a.example/1", "https://a.example/2"]}

    monkeypatch.setattr(quick, "fetch_url", fake_fetch)
    done = _capture_done(monkeypatch, job)
    webui.run_paste_job(job, "https://a.example", "links", False, 2, 100, None)
    assert done["result"]["total"] == 2
    assert "2 个链接" in done["summary"]


def test_paste_table_mode_uses_tables(monkeypatch):
    job = {"kind": "paste", "messages": [], "task_dir": ""}

    def fake_fetch(url, **kw):
        return {"url": url, "status": 200, "text": "<table></table>",
                "tables": [[{"名称": "甲", "数量": "1"}, {"名称": "乙", "数量": "2"}]]}

    monkeypatch.setattr(quick, "fetch_url", fake_fetch)
    done = _capture_done(monkeypatch, job)
    webui.run_paste_job(job, "https://a.example/table", "table", False, 2, 100, None)
    assert done["result"]["total"] == 2
    assert "2 行" in done["summary"]


def test_paste_article_mode_reports_one_row(monkeypatch):
    job = {"kind": "paste", "messages": [], "task_dir": ""}

    def fake_fetch(url, **kw):
        return {"url": url, "status": 200, "text": "有效正文内容"}

    monkeypatch.setattr(quick, "fetch_url", fake_fetch)
    done = _capture_done(monkeypatch, job)
    webui.run_paste_job(job, "https://a.example/article", "auto", False, 2, 100, None)
    assert done["result"]["total"] == 1
    assert "抓取成功" in done["summary"]


def test_paste_empty_raw_html_is_not_article_content(monkeypatch):
    """回归：空白页面即使 HTTP 返回原始 HTML，也不能被当成“正文成功”。"""
    job = {"kind": "paste", "messages": [], "task_dir": ""}

    def fake_fetch(url, **kw):
        return {"url": url, "status": 200, "text": "<html><head></head><body></body></html>"}

    monkeypatch.setattr(quick, "fetch_url", fake_fetch)
    done = _capture_done(monkeypatch, job)
    webui.run_paste_job(job, "https://a.example/empty", "auto", False, 2, 100, None)
    assert done["result"]["total"] == 0
    assert "页面无正文" in done["summary"]
