# -*- coding: utf-8 -*-
"""Stack Overflow 精配：描述路由命中、标签抽取、EPOCH 时间、API JSON 解析（离线 fixture）。"""
import json
from universal_scraper.sites import match_site_by_description, seed_url_for, match_stackoverflow, parse_stackoverflow

SO_JSON = json.dumps({"items": [
    {"title": "How to scrape with python?", "link": "https://so.com/q/1",
     "score": 5, "answer_count": 2, "tags": ["python", "scraping"], "creation_date": 1787000000},
    {"title": "Low score question", "link": "https://so.com/q/2",
     "score": -3, "answer_count": 0, "tags": ["python"], "creation_date": 1787000001},
]})


def test_desc_routing_hits_stackoverflow():
    desc = "Stack Overflow“Questions”标签“python”下24小时新问题，抓取高分问题"
    assert match_site_by_description(desc) == "stackoverflow"


def test_seed_has_tag_and_epoch():
    desc = "Stack Overflow 标签 java 的新问题"
    seed = seed_url_for(desc)
    assert "tagged=java" in seed
    assert "fromdate=" in seed
    # EPOCH 秒：纯数字
    import re
    m = re.search(r"fromdate=(\d+)", seed)
    assert m and len(m.group(1)) == 10, "EPOCH 应为 10 位 Unix 秒"


def test_seed_without_tag_drops_tagged():
    desc = "stackoverflow 最近的新问题"
    seed = seed_url_for(desc)
    assert "tagged=" not in seed, "无标签时应移除 tagged 参数"


def test_match():
    assert match_stackoverflow("https://api.stackexchange.com/2.3/questions?tagged=python")
    assert match_stackoverflow("https://stackoverflow.com/questions")


def test_parse_sorted_by_score():
    rows = parse_stackoverflow(SO_JSON, "https://api.stackexchange.com/2.3/questions")
    assert len(rows) == 2
    assert rows[0]["title"].startswith("How to scrape")  # score 5 在前
    assert rows[0]["tags"] == "python,scraping"
    assert rows[1]["score"] == -3
