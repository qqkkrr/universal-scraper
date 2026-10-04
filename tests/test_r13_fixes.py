#!/usr/bin/env python3
"""审查十三轮（R13）修复的回归测试——全部 hermetic。

覆盖：
- selectors：ReDoS lint R5/R6（有界外层包变长/可空内层——5 个实测冻结形态）
- auto：H5 四形态（click+xpath/未知键/filter 缺 field/畸形容器）+ H6 质量门复核（源级）
- fetchers：fields 字符串简写统一为 CSS 取文本（http 侧）
- agent：goto 出站守卫
- mcp：notification 不回包
- precise：试跑导出按 CWD 口径
- book：_find_info 行首锚定 / _text 剥 script+style
"""
import inspect
import sys
from pathlib import Path


SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


# ---------------- selectors：ReDoS lint ----------------

def test_regex_lint_bounded_repeats():
    from universal_scraper.selectors import regex_is_dangerous
    # R13 TP：agent 实测冻结进程的 5 个有界形态（修前全被放过）
    for pat in [r"(\w+){5}$", r"(?:[\w.]+){3,8}$", r"(?:a{1,3}|b{1,3})+$",
                r"(a?){25}a{25}$", r"(?:[a-z]+){20}$"]:
        assert regex_is_dangerous(pat) is True, pat
    # FP 回归：已固化面 + 新防护不误杀
    for pat, want in [
        (r"\d{4}-\d{2}-\d{2}", False),
        (r"(a{3})+", False),
        (r"(ab{1,3}){3}", False),
        (r"(?:ab|ba)+", False),
        (r"(?:\d{1,4}\.?)+$", True),      # 已固化 TP 不回归
        (r"(a+)+", True), (r"(a?)+", True),
        (r"(\w{2}){5}", False),           # 定长内层不误杀
        (r"(a{3}){5}", False),
        (r"(?:\d{4}){2}", False),
        (r"([a-z]{2}){3}", False),
        (r"https?://[^\s/]+", False),
    ]:
        assert regex_is_dangerous(pat) is want, pat


# ---------------- auto：validate 兼容四形态 ----------------

def test_auto_validate_and_fix_compat():
    from universal_scraper.auto import _validate_and_fix
    base = {"name": "t", "start_urls": ["https://x.com/"],
            "parsers": {"default": {"type": "html", "fields": {"t": "h1"}}},
            "rules": [{"match": "contains", "pattern": "/", "parser": "default"}]}
    # H5a：click+xpath（修前 ConfigError"需要 selector"）
    c1 = _validate_and_fix({**base, "source": {"type": "browser",
                            "actions": [{"type": "click", "xpath": "//a.more"}]}})
    assert c1["source"]["actions"][0]["type"] == "click"
    # H5b：未知键清洗（修前 ACTION_KEY_SPEC ConfigError）
    c2 = _validate_and_fix({**base, "source": {"type": "browser",
                            "actions": [{"type": "click", "selector": ".x", "to": "y"}]}})
    assert "to" not in c2["source"]["actions"][0]
    # H5c：detail.filters 缺 field 丢弃（修前 ConfigError）
    c3 = _validate_and_fix({**base, "source": {"type": "http"},
                            "detail": {"enabled": True,
                                       "filters": [{"type": "filter", "op": "contains",
                                                    "value": "x"}]}})
    assert not (c3.get("detail") or {}).get("filters")
    # H5d：畸形容器规范化（source:"browser" 字符串 / fields 为 list / rules 为 str）
    c4 = _validate_and_fix({"name": "t4", "start_urls": ["https://x.com/"],
                            "source": "browser",
                            "parsers": {"default": {"type": "html", "fields": ["a"]}},
                            "rules": "x"})
    assert isinstance(c4["source"], dict)
    assert c4["rules"][0]["parser"] == "default"


def test_auto_llm_evidence_gate_reviewed():
    from universal_scraper import auto as A
    src = inspect.getsource(A)
    # H6：LLM 证据抽取的清空前有复核（两处调用点同款）
    assert src.count("_fb_miss = _missing_key_field") == 2
    assert src.count("不采纳，继续自修复") >= 2


# ---------------- fetchers ----------------

def test_fetchers_fields_string_unified():
    from universal_scraper.fetchers import HttpFetcher
    hf = HttpFetcher.__new__(HttpFetcher)
    hf.source = {"row_css": "li.item", "fields": {"标题": "b.t"}}
    rows = hf._extract_html_rows(
        '<ul><li class="item"><b class="t">标题A</b></li></ul>')
    # 修前：字符串简写取整行 HTML（与 browser 侧 CSS 取文本分叉）
    assert rows == [{"标题": "标题A"}]


# ---------------- agent ----------------

def test_agent_goto_guard():
    from universal_scraper import agent as AG
    src = inspect.getsource(AG)
    assert 'assert_public_url(u, context="agent goto")' in src
    assert "出站守卫拦截" in src


# ---------------- mcp ----------------

def test_mcp_notification_no_reply():
    from universal_scraper.mcp_server import handle_message
    # 无 id 的 notification 不得回包（修前返回 {"id": null, ...}）
    assert handle_message({"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None
    assert handle_message({"jsonrpc": "2.0", "method": "unknown/thing"}) is None
    # 有 id 的请求照常回包
    r = handle_message({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    assert r["id"] == 7


# ---------------- precise ----------------

def test_precise_probe_cwd_source():
    from universal_scraper import precise_auto as P
    src = inspect.getsource(P._run_engine_probe)
    # 修前 ROOT / "outputs"（与引擎 CWD 口径不一致）
    assert 'Path("outputs")' in src


# ---------------- book ----------------

def test_book_find_info_anchored_and_text_stripped():
    from lxml import html as LH
    from universal_scraper.book_catalog import _find_info, _text
    doc = LH.fromstring(
        '<div id="info">副标题: 作者: 一个写作者的自述<br/>作者: 余华<br/>'
        '<script>var junk=1;</script>出版社: 作家出版社</div>')
    # 行首锚定：作者行不被"副标题: 作者:..." 行顶替（修前取到"一个写作者的自述"）
    assert _find_info(doc, "作者") == "余华"
    # _text 剥 script/style（修前混入 "var junk=1;"）
    intro = LH.fromstring('<div class="intro">简介 <script>var junk=1;</script>正文</div>')
    assert _text(intro) == "简介 正文"
    assert _text(LH.fromstring("<script>var x=1;</script>")) == ""
