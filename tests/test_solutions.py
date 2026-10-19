# -*- coding: utf-8 -*-
"""失败分类：登录/验证码/0条/404/「10条不算0条」回归。"""
from universal_scraper.solutions import classify_failure

def test_login():
    assert classify_failure("需要登录才能查看") == "login_required"

def test_captcha():
    assert classify_failure("遇到滑块验证码") == "captcha"

def test_zero_rows_selector():
    assert classify_failure("解析 0 条，选择器未命中") == "selector_failed"

def test_404():
    assert classify_failure("HTTP 404 not found") == "entry_invalid"

def test_ten_rows_is_not_zero_rows_regression():
    """关键回归：'成功 10 条' 不能被当成 '0 条'。"""
    assert classify_failure("成功 10 条") != "selector_failed"

def test_403_blocked():
    assert classify_failure("403 Forbidden") == "ip_blocked"


def test_job_done_zero_rows_attaches_solution(monkeypatch):
    """0 条/无正文的任务结束也必须带确定性方案（核心承诺：不假成功）。"""
    import universal_scraper.webui as webui
    monkeypatch.setattr(webui, "_persist_jobs", lambda: None)
    job = {"status": "running", "result": None, "summary": None,
           "messages": ["⚠️ 任务结束：0 条。原因诊断：选择器未匹配到内容"], "task_dir": ""}
    webui._job_done(job, {"total": 0, "fetched": 0, "errors": 0},
                    "⚠️ 任务结束：0 条。原因诊断：选择器未匹配到内容")
    assert job.get("solution", {}).get("type") == "selector_failed"
    assert job.get("solution", {}).get("steps")


def test_job_done_nonzero_rows_does_not_attach_false_solution(monkeypatch):
    """回归：成功 10 条绝不能因 summary 含“0”/“条”被当成 0 条。"""
    import universal_scraper.webui as webui
    monkeypatch.setattr(webui, "_persist_jobs", lambda: None)
    job = {"status": "running", "result": None, "summary": None,
           "messages": ["✅ 任务结束：成功 10 条"], "task_dir": ""}
    webui._job_done(job, {"total": 10, "fetched": 1, "errors": 0},
                    "✅ 任务结束：成功 10 条")
    assert "solution" not in job


def test_no_content_classified():
    assert classify_failure("页面无正文（可能需浏览器渲染）") == "no_content"
    assert classify_failure("未发现表格") == "no_content"


def test_job_done_no_content_gets_specific_solution(monkeypatch):
    """空页面不能因为文案里带「需要登录」而误判成登录墙。"""
    import universal_scraper.webui as webui
    monkeypatch.setattr(webui, "_persist_jobs", lambda: None)
    job = {"status": "running", "result": None, "summary": None,
           "messages": ["⚠️ 任务结束：页面无正文（可能需浏览器渲染或需要登录）"], "task_dir": ""}
    webui._job_done(job, {"total": 0, "fetched": 1, "errors": 0},
                    "⚠️ 任务结束：页面无正文（可能需浏览器渲染或需要登录）")
    assert job.get("solution", {}).get("type") == "no_content"
    assert "浏览器渲染" in " ".join(job.get("solution", {}).get("steps", []))


def test_job_done_no_total_precise_success_is_not_zero(monkeypatch):
    """精配成功结果只有 rows/ok、没有 total 时，不得被当成 0 条并附失败方案。"""
    import universal_scraper.webui as webui
    monkeypatch.setattr(webui, "_persist_jobs", lambda: None)
    job = {"status": "running", "result": None, "summary": None,
           "messages": ["✅ 精配生成成功"], "task_dir": ""}
    webui._job_done(job, {"ok": True, "rows": [{"标题": "a"}, {"标题": "b"}], "files": {}},
                    "✅ 精配生成成功")
    assert "solution" not in job
