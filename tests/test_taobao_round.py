"""第七轮反馈回归（盒马战例）：record.fields 类型契约 / RGV587 会话标记感知。"""
import pytest

from universal_scraper.config import ConfigError, validate
from universal_scraper.engine import map_record
from universal_scraper.antibot import detect_block


def test_map_record_with_list_fields_keeps_raw():
    """盒马战例：record.fields 写成 list 曾直接 AttributeError 裸栈。"""
    raw = {"标题": "酱鸭", "价格": 39.9}
    out = map_record(raw, ["标题"])  # type: ignore[arg-type]
    assert out == raw, "list 型 fields 应跳过映射并保留原始字段"


def test_validate_rejects_list_record_fields():
    cfg = {"name": "t",
           "source": {"type": "http_html", "url": "https://x.example/", "row_css": ".i"},
           "pagination": {"strategy": "none"},
           "record": {"fields": ["标题"]}}
    with pytest.raises(ConfigError):
        validate(cfg)


def test_rgv587_session_flagged():
    """淘宝系会话标记：RGV587 页 / mtop ret=TIMEOUT:: 必须被识别为 session_flagged。"""
    d1 = detect_block(200, "<html>亲，访问受限 RGV587_ERROR</html>", {}, "https://x.example/")
    assert d1["kind"] == "session_flagged"
    d2 = detect_block(200, '{"ret":["TIMEOUT::接口超时"]}', {}, "https://x.example/")
    assert d2["kind"] == "session_flagged"


def test_normal_bilibili_json_not_flagged():
    d = detect_block(200, '{"code":0,"data":{"list":[{"title":"正常"}]}}', {},
                     "https://api.bilibili.com/x")
    assert d["kind"] == "none"
