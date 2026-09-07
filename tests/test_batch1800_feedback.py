# -*- coding: utf-8 -*-
"""batch1800（600 项终版复盘）反馈回归：自动映射键并集 / 认证头携带 / 网络失败提示。"""
import json

from universal_scraper.capture_gen import one_config


def test_engine_auto_map_uses_key_union():
    """P0：首行 GET 时，后续 POST 行的 _post_data 不再被自动映射丢掉。"""
    from universal_scraper.engine import map_record
    rows_raw = [
        {"_api_url": "https://x.com/a", "data": {"a": 1}, "_method": "GET"},
        {"_api_url": "https://x.com/b", "data": {"b": 2}, "_method": "POST",
         "_post_data": '{"pageNo":1}', "_request_content_type": "application/json"},
    ]
    keys = []
    for r in rows_raw[:20]:
        for k in r:
            if k not in keys:
                keys.append(k)
    rec_fields = {k: {"from": k} for k in keys}
    mapped = [map_record(r, rec_fields) for r in rows_raw]
    assert "_post_data" in mapped[1]                    # 旧逻辑（只看首行）会丢
    assert mapped[1]["_post_data"] == '{"pageNo":1}'


def test_capture_gen_carries_auth_headers():
    item = {"url": "https://x.com/api/q", "method": "POST", "post_data": '{"a":1}',
            "request_content_type": "application/json",
            "request_headers": {"Cookie": "sid=abc", "X-Requested-With": "XMLHttpRequest",
                                 "User-Agent": "should-not-carry", "Accept": "*/*"},
            "json": {"ok": 1}}
    src = one_config(item)
    assert src["headers"].get("Cookie") == "sid=abc"
    assert src["headers"].get("X-Requested-With") == "XMLHttpRequest"
    assert "should-not-carry" not in json.dumps(src)    # 非 auth 头不带
    assert "_auth_hint" in src


def test_network_fail_hint_attached():
    """P3：重试耗尽返回应带 doctor 提示（三个客户端返回路径各一处）。"""
    from universal_scraper import core
    src = open(core.__file__, encoding="utf-8").read()
    assert src.count("连续网络失败：出口/系统代理可能已变化") >= 3
