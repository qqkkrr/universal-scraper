# -*- coding: utf-8 -*-
"""已学配置清理公共函数 + GitHub Topics 精配（离线 fixture）。"""
import json
from pathlib import Path

import universal_scraper.auto as auto


def test_drop_learned_matching(tmp_path, monkeypatch):
    monkeypatch.setattr(auto, "LEARNED_DIR", tmp_path)
    (tmp_path / "a.com.json").write_text(json.dumps(
        {"config": {"start_urls": ["https://a.com/list"]}}), encoding="utf-8")
    (tmp_path / "b.com.json").write_text(json.dumps(
        {"config": {"start_urls": ["https://b.com/"]}}), encoding="utf-8")
    logs = []
    ok = auto._drop_learned_by_start_url("https://a.com/list", reason="意图不符", log=logs.append)
    assert ok is True
    assert not (tmp_path / "a.com.json").exists()
    assert (tmp_path / "b.com.json").exists()
    assert any("已删除" in m for m in logs)


def test_drop_learned_no_match_keeps_all(tmp_path, monkeypatch):
    monkeypatch.setattr(auto, "LEARNED_DIR", tmp_path)
    (tmp_path / "c.com.json").write_text(json.dumps(
        {"config": {"start_urls": ["https://c.com/"]}}), encoding="utf-8")
    ok = auto._drop_learned_by_start_url("https://other.com/")
    assert ok is False
    assert (tmp_path / "c.com.json").exists()


GH_TOPICS_HTML = """
<html><body>
<article class="border">
  <h3><a href="/firecrawl/firecrawl">firecrawl</a><span> / </span><a href="/firecrawl/firecrawl">firecrawl</a></h3>
  <p>The context API to search, scrape, and interact with the web at scale.</p>
  <a class="tooltipped btn-sm btn" aria-label="firecrawl/firecrawl stargazers">171k</a>
  <span itemprop="programmingLanguage">TypeScript</span>
</article>
<article class="border">
  <h3><a href="/D4Vinci/Cr3dOv3r">D4Vinci</a><span> / </span><a href="/D4Vinci/Cr3dOv3r">Cr3dOv3r</a></h3>
  <p>An adaptive web scraping framework</p>
  <span class="btn">Star 75.8k</span>
  <span itemprop="programmingLanguage">Python</span>
</article>
</body></html>
"""


def test_parse_github_topics():
    from universal_scraper.sites import match_github_topics, parse_github_topics
    assert match_github_topics("https://github.com/topics/web-scraping")
    rows = parse_github_topics(GH_TOPICS_HTML, "https://github.com/topics/web-scraping")
    assert len(rows) == 2
    assert rows[0]["repo"] == "firecrawl/firecrawl"
    assert rows[0]["stars"] == "171k"
    assert rows[0]["language"] == "TypeScript"
    # 无 aria-label 的卡片走正则兜底
    assert rows[1]["stars"] == "75.8k"
    assert rows[1]["repo"] == "D4Vinci/Cr3dOv3r"
