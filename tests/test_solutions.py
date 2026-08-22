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
