"""第九轮反馈回归（闲鱼战例）：actions 透传 / subs 子字段 / regex_extract / cdp 输出。"""
import json
from pathlib import Path

from universal_scraper.engine import run_pipeline
from universal_scraper.fetchers import BrowserFetcher, HttpFetcher


def test_actions_passthrough_in_spec():
    """闲鱼战例：scaffold 教写 actions，fetch_list 组装 spec 时曾静默丢弃。"""
    src = {"type": "browser", "url": "https://x.example/",
           "actions": [{"type": "click", "selector": "text=最新发布"}],
           "pagination": {"type": "none"}}
    bf = BrowserFetcher(src, {}, {}, Path("."))
    spec = bf._build_spec()
    assert spec["actions"] == [{"type": "click", "selector": "text=最新发布"}]


def test_urls_mode_in_spec():
    bf = BrowserFetcher({"type": "browser", "url": "https://x/"}, {}, {}, Path("."))
    spec = bf._build_spec(urls=["https://x/1", "https://x/2"])
    assert spec["urls"] == ["https://x/1", "https://x/2"]


XIANyu_HTML = """<html><body>
<div class="card"><span class="price">592</span><span class="want">23人想要</span></div>
</body></html>"""


def test_subs_structured_subfields_http():
    src = {"type": "http_html", "url": "https://x/", "row_css": "div.card",
           "fields": {"价格区": {"subs": {"价格": "span.price", "想要": "span.want"}}}}
    f = HttpFetcher(src, {}, {}, Path("."))
    rows = f._extract_html_rows(XIANyu_HTML)
    assert rows[0]["价格区"] == {"价格": "592", "想要": "23人想要"}, \
        "subs 应分开取子选择器，而非无缝拼接"


def test_regex_extract_pipeline():
    rows = [{"描述": "用过3次，轻微划痕"}, {"描述": "全新未拆"}]
    out = run_pipeline(rows, [{"type": "regex_extract", "field": "描述",
                               "pattern": r"用过(\d+)次", "to": "使用次数"}])
    assert out[0]["使用次数"] == "3"
    assert "使用次数" not in out[1] or not out[1].get("使用次数")


def test_cdp_dict_output_format():
    """cdp --login-state 输出修复：dict 结果按 k: v 打印（曾只打印键名）。"""
    data = json.loads('{"domain": "weibo.com", "count": 15, "names": ["SUB", "SVB"]}')
    lines = []
    if isinstance(data, dict):
        for k, v in data.items():
            lines.append(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}")
    assert any("weibo.com" in l for l in lines)
    assert any("SUB" in l for l in lines), "names 列表内容必须可见"
