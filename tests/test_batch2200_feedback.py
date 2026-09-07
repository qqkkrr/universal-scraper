# -*- coding: utf-8 -*-
"""batch2200（第四批 200 项）反馈回归：连续网络失败升级提示 / GET 原始查询串 / 迟到 POST 行。"""
from universal_scraper.core import _NET_FAIL_STREAK, _note_net_result


def test_net_streak_escalates_once_then_resets():
    """连续 3 次失败升级提示一次；成功清零。"""
    st = _NET_FAIL_STREAK
    saved = dict(st)
    try:
        st.update(count=0, warned_at=0)
        for _ in range(2):
            _note_net_result(False)
        assert st["count"] == 2 and st["warned_at"] == 0   # 未到阈值
        _note_net_result(False)
        assert st["count"] == 3 and st["warned_at"] > 0    # 触发升级提示一次
        t = st["warned_at"]
        _note_net_result(False)                            # 第 4 次：不再刷提示（30 分钟窗口）
        assert st["warned_at"] == t
        _note_net_result(True)                             # 成功清零
        assert st["count"] == 0
    finally:
        st.update(saved)


def test_engine_map_covers_late_post_rows():
    """batch2200：POST 行在第 26 行（旧版首 20 行窗口外）时请求体不再丢。"""
    from universal_scraper.engine import map_record
    rows = [{"_api_url": f"https://x.com/{i}", "data": {}} for i in range(25)]
    rows.append({"_api_url": "https://x.com/post", "data": {},
                 "_method": "POST", "_post_data": '{"p":1}'})
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    fields = {k: {"from": k} for k in keys}
    mapped = map_record(rows[-1], fields)
    assert mapped["_post_data"] == '{"p":1}'


def test_capture_gen_get_keeps_encoding_still_green():
    """回归锚：v1.12.2 的原始编码保留不回退。"""
    item = {"url": "https://x.com/api/list?page=2&kw=%E5%85%AC%E5%8F%B8&size=10",
            "method": "GET", "json": [{"a": 1}]}
    src = one_config(item)
    assert "page={{page}}" in src["url"] and "%E5%85%AC%E5%8F%B8" in src["url"]


from universal_scraper.capture_gen import one_config  # noqa: E402  (供上方用例)
