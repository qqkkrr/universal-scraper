"""内嵌 JSON 提取器回归：window.X / module const / 字典下钻 / 尾逗号 / 未命中。"""
from universal_scraper.selectors import extract_embedded_json_rows

HTML_WINDOW = """<html><body>
<script>
var page = 3;
window.article_list = [
  {"title": "茅台神话破灭?", "read": 1024, "comment": 33},
  {"title": "稳住,能赢", "read": 2048, "comment": 55,},
];
window.other = 1;
</script>
</body></html>"""

HTML_MODULE = """<script type="module">
const guba_list = [{"t": "a"}, {"t": "b"}];
</script>"""

HTML_DICT = """<script>window.g = {"list": [{"x": 1}], "total": 9};</script>"""


def test_window_var_with_trailing_comma():
    rows = extract_embedded_json_rows(HTML_WINDOW, "window.article_list")
    assert len(rows) == 2
    assert rows[0]["title"] == "茅台神话破灭?"
    assert rows[1]["read"] == 2048


def test_module_scope_const():
    rows = extract_embedded_json_rows(HTML_MODULE, "guba_list")
    assert [r["t"] for r in rows] == ["a", "b"]


def test_dict_path_drill():
    rows = extract_embedded_json_rows(HTML_DICT, {"var": "window.g", "path": "list"})
    assert rows == [{"x": 1}]
    assert extract_embedded_json_rows(HTML_DICT, {"var": "window.g", "path": "total"}) == []


def test_not_found_returns_empty():
    assert extract_embedded_json_rows(HTML_WINDOW, "window.nothing") == []
    assert extract_embedded_json_rows(HTML_WINDOW, "") == []
    assert extract_embedded_json_rows(HTML_WINDOW, None) == []


def test_single_quote_js_not_supported_returns_empty():
    # 单引号/无引号键等非 JSON 兼容字面量不支持，按 0 条走诊断不假成功
    html = "<script>window.s = [{'a': 1}];</script>"
    assert extract_embedded_json_rows(html, "window.s") == []


def test_equality_and_arrow_not_mismatched():
    # `X == 1` / `X => {...}` 不应被误当赋值截取
    html = "<script>if (window.k == 2) {} window.k == [{\"z\": 1}];</script>"
    assert extract_embedded_json_rows(html, "window.k") == []
