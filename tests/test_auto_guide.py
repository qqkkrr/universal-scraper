# -*- coding: utf-8 -*-
"""human_guide / diagnose_failure 规则兜底（monkeypatch LLM，离线可跑）。"""
import universal_scraper.auto as auto

def _no_llm(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("测试禁用 LLM")
    monkeypatch.setattr(auto, "_llm_chat", boom)

def test_human_guide_login_hint(monkeypatch):
    _no_llm(monkeypatch)
    cfg = {"start_urls": ["https://x.com/"],
           "source": {"type": "browser", "login": {"enabled": True}}}
    g = auto.human_guide("抓取登录后才能看的内容", cfg)
    assert "登录" in g["你需要准备"]
    assert g["一句话说明"]

def test_human_guide_public_page(monkeypatch):
    _no_llm(monkeypatch)
    cfg = {"start_urls": ["https://example.com/"], "source": {"type": "http"}}
    g = auto.human_guide("抓取标题", cfg)
    assert "什么都不用准备" in g["你需要准备"]

def test_diagnose_failure_fallback(monkeypatch):
    _no_llm(monkeypatch)
    d = auto.diagnose_failure("抓取商品", {"start_urls": ["https://x.com/"]}, "第 1 轮 0 条")
    assert d["可能原因"] and d["解决办法"] and d["建议下一步"]
