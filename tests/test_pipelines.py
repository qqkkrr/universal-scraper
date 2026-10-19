# -*- coding: utf-8 -*-
"""管道：filter(non_empty/contains/between) + dedup。"""
from universal_scraper.modules.pipelines import Pipeline

def _pipe(steps):
    return Pipeline(steps, {})

def test_filter_non_empty_drops_blank():
    p = _pipe([{"type": "filter", "field": "title", "op": "non_empty"}])
    assert p.process({"title": "x"}) is not None
    assert p.process({"title": "  "}) is None
    assert p.process({}) is None

def test_filter_contains():
    p = _pipe([{"type": "filter", "field": "cat", "op": "contains", "value": "美食"}])
    assert p.process({"cat": "美食测评"}) is not None
    assert p.process({"cat": "旅游攻略"}) is None

def test_filter_between_drops_outside_keeps_inside():
    p = _pipe([{"type": "filter", "field": "price", "op": "between", "min": 10, "max": 100}])
    assert p.process({"price": "50"}) is not None
    assert p.process({"price": "200"}) is None
    # 无法解析的数值：跳过不崩溃、不误删
    assert p.process({"price": "约50"}) is not None

def test_dedup_content_hash():
    p = _pipe([{"type": "dedup", "key": "content_hash", "fields": ["title"]}])
    assert p.process({"title": "a"}) is not None
    assert p.process({"title": "a"}) is None
    assert p.process({"title": "b"}) is not None
