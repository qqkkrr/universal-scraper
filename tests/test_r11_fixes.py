#!/usr/bin/env python3
"""审查十一轮（R11）修复的回归测试——全部 hermetic。

覆盖：
- fetchers：capture_all 白名单 / allow_html_404 / truncated / 重复页 / xpath / records_path
- cookies：域规范化 / 编码损坏隔离 / health 结构防御 / 父域合并 / temp 全删
- diagnose：拦截词前置 / quick 403 回退
- domain_budget：hours 校验 / meta 防御 / listing 归一
- capture_gen：链残留告警 / records_path 猜测
- agent：raw_decode / 登录判定收窄 / 示例 vs 真数据
- pdf_attach：表头碰撞循环改名
"""
import inspect
import json
import sys
import tempfile
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


# ---------------- fetchers ----------------

def test_fetchers_capture_all_whitelist():
    from universal_scraper import fetchers as F
    bf = F.BrowserFetcher.__new__(F.BrowserFetcher)
    bf.source = {"url": "u", "capture_all": True}
    assert bf._build_spec()["capture_all"] is True          # 修前 False
    bf.source = {"url": "u", "capture": True}
    assert bf._build_spec()["capture_all"] is True
    bf.source = {"url": "u", "capture": [{"name": "x"}]}
    spec = bf._build_spec()
    assert spec["capture_all"] is False and spec["capture"] == [{"name": "x"}]
    # modules 同款
    from universal_scraper.modules import fetchers as MF
    assert 'self.config.get("capture_all") is True' in inspect.getsource(MF)


def test_fetchers_guards_source_level():
    from universal_scraper import fetchers as F
    assert "allow_html_404=True" in inspect.getsource(F.HttpFetcher._request)
    sp = inspect.getsource(F.HttpFetcher._fetch_single_page)
    assert "truncated" in sp and "传输层失败" in sp and "_prev_page_sig" in sp
    nx = inspect.getsource(F.HttpFetcher._next_url)
    assert 'sel.removeprefix("xpath:")' in nx               # 修前 lstrip 啃首字符
    rc = inspect.getsource(F.BrowserFetcher._records_from_capture)
    assert "未命中任何值" in rc
    bl = inspect.getsource(F.BrowserFetcher.fetch_list)
    assert "已收 {len(records)} 条" in bl


# ---------------- cookies ----------------

def test_cookies_norm_domain():
    from universal_scraper.cookies import _norm_domain
    assert _norm_domain("example.com:8443") == "example.com"
    assert _norm_domain("user:pw@example.com") == "example.com"
    assert _norm_domain("example.com.") == "example.com"
    assert _norm_domain("https://www.example.com:8443/p") == "example.com"
    assert _norm_domain("weibo.com") == "weibo.com"


def test_cookies_roundtrip_with_port(monkeypatch, tmp_path):
    import universal_scraper.cookies as CK
    monkeypatch.setattr(CK, "COOKIE_DIR", tmp_path)
    assert CK.save_cookies("example.com:8443", [
        {"name": "SESSDATA", "value": "tok", "domain": ".example.com", "expires": 0}]) is True
    assert [c["name"] for c in CK.load_cookies("example.com")] == ["SESSDATA"]


def test_cookies_corrupt_encoding_isolated(monkeypatch, tmp_path):
    import universal_scraper.cookies as CK
    monkeypatch.setattr(CK, "COOKIE_DIR", tmp_path)
    (tmp_path / "corruptonly.test.json").write_bytes(b'{"cookies": ["\xff\xfe\x00bad"]}')
    assert CK.load_cookies("corruptonly.test") == []
    assert any(x.name.endswith(".corrupt") for x in tmp_path.iterdir())


def test_cookies_health_struct_defense(monkeypatch, tmp_path):
    import universal_scraper.cookies as CK
    monkeypatch.setattr(CK, "COOKIE_DIR", tmp_path)
    (tmp_path / "s.test.json").write_text(json.dumps({"cookies": None}), encoding="utf-8")
    assert CK.session_health("s.test").get("corrupt") is True   # 修前 TypeError


def test_cookies_parent_merge_all_levels(monkeypatch, tmp_path):
    import universal_scraper.cookies as CK
    monkeypatch.setattr(CK, "COOKIE_DIR", tmp_path)
    (tmp_path / "example.com.json").write_text(json.dumps({"cookies": [
        {"name": "SESSDATA", "value": "t", "domain": ".example.com", "expires": 0}]}), encoding="utf-8")
    (tmp_path / "b.example.com.json").write_text(json.dumps({"cookies": [
        {"name": "cf_clearance", "value": "c", "domain": ".b.example.com", "expires": 0}]}), encoding="utf-8")
    got = CK.load_cookies("a.b.example.com")
    assert sorted(c["name"] for c in got) == ["SESSDATA", "cf_clearance"]


def test_cookies_temp_release_all_imported(monkeypatch, tmp_path):
    import universal_scraper.cookies as CK
    monkeypatch.setattr(CK, "COOKIE_DIR", tmp_path)
    for d in ("s1.com", "s2.com"):
        (tmp_path / f"{d}.json").write_text('{"cookies":[]}', encoding="utf-8")
    CK.release_temp("s1.com", {"mode": "temp", "source": "imported",
                               "imported_domains": ["s1.com", "s2.com"]})
    assert not (tmp_path / "s1.com.json").exists() and not (tmp_path / "s2.com.json").exists()


# ---------------- diagnose ----------------

def test_diagnose_block_words_hoisted():
    from universal_scraper.diagnose import classify_block
    r = classify_block(200, "<html><title>访问被拒</title><h1>访问受限</h1></html>")
    assert r["is_block"] is True and r["type"] == "waf_plain"   # 修前 ok
    r2 = classify_block(200, "<html><div id='app'></div></html>")
    assert (r2["is_block"], r2["type"]) == (False, "spa_hint")
    # 正文拦截词不误报（非 title/h1）
    r3 = classify_block(200, "<html><title>教程</title><p>访问受限请联系客服</p><div id='app'></div></html>")
    assert r3["is_block"] is False


def test_diagnose_quick_403_fallback():
    from universal_scraper import diagnose as D
    assert "403, 429, 468" in inspect.getsource(D.diagnose_quick)


# ---------------- domain_budget ----------------

def test_domain_budget_hours_validation(tmp_path):
    from universal_scraper import domain_budget as DB
    p = str(tmp_path / "db.json")
    for bad in (float("inf"), float("nan"), 0, -5):
        with pytest.raises(ValueError):
            DB.mark("x.test", hours=bad, path=p)
    DB.mark("x.test", hours=1, path=p)
    assert DB.check("x.test", path=p)["in_cooldown"] is True


def test_domain_budget_meta_defense_and_listing_norm(tmp_path):
    from universal_scraper import domain_budget as DB
    import time as _t
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"domain": {"domain:x.test": int(_t.time())},
                             "budget_meta": {"x.test": {"hours": "24", "note": "n"}},
                             "notes": {}}), encoding="utf-8")
    c = DB.check("x.test", path=str(f))          # 修前 TypeError
    assert c["in_cooldown"] is True
    g = tmp_path / "o.json"
    g.write_text(json.dumps({"domain": {"domain:www.old.test": int(_t.time())},
                             "budget_meta": {}, "notes": {}}), encoding="utf-8")
    assert "old.test" in DB.listing(path=str(g))  # 修前原始 key


# ---------------- capture_gen ----------------

def test_capture_gen_chain_residual_warning():
    from universal_scraper.capture_gen import _detect_chains
    # 未匹配参数按常量保留（既有契约）
    c = _detect_chains([({"name": "l", "source": {"url": "http://x/l"}, "pagination": {}}, [{"x": 12}]),
                        ({"name": "d", "source": {"url": "http://x/d?id=1234&x=12"}, "pagination": {}}, [])],
                       log=lambda *_: None)
    assert c[0]["scaffold"]["pipeline"][0]["tmpl"] == "http://x/d?id=1234&x={x}"
    # 残留告警进 hint（不再静默）
    c2 = _detect_chains([({"name": "l", "source": {"url": "http://x/l"}, "pagination": {}},
                          [{"id": 9, "cat": 12}]),
                         ({"name": "d", "source": {"url": "http://x/d?cat=12&id=5"}, "pagination": {}}, [])],
                        log=lambda *_: None)
    assert c2 and "每行 URL 相同" in c2[0]["scaffold"]["_hint"]


def test_capture_gen_records_path_guess():
    from universal_scraper.capture_gen import _guess_records_path
    assert _guess_records_path({"data": [], "list": [{"id": 1}]}) == "list"   # 修前 data
    assert _guess_records_path({"data": []}) == "data"                        # 空数组回归
    assert _guess_records_path({"data": {"rows": []}, "list": [{"x": 1}]}) == "list"
    assert _guess_records_path({"data": {"list": [{"id": 1}]}}) == "data.list"


# ---------------- agent ----------------

def test_agent_parse_action_nested():
    from universal_scraper.agent import _parse_action
    d = _parse_action('页面摘要：{"url":"x"}\n{"action":"extract","schema":{"a":"b"}}')
    assert d["action"] == "extract" and d["schema"] == {"a": "b"}
    assert _parse_action('{"action":"goto","url":"http://x"}')["action"] == "goto"


def test_agent_need_login_narrowed():
    from universal_scraper.agent import re_need_login
    assert re_need_login("首页 登录 免费注册 帮助") is False   # 修前 True
    assert re_need_login("请先登录后查看") is True
    assert re_need_login("验证码") is True


def test_agent_extract_prefers_rich_data(monkeypatch):
    from universal_scraper import agent as AG
    monkeypatch.setattr(AG, "_llm", lambda msgs, **kw: (
        '格式示例：[{"title":"示例值","url":"string"},{"title":"示例2","url":"s2"}]\n'
        '真实数据：[{"title":"某公司被罚120万元","url":"https://x.gov.cn/a/1"}]'))
    items = AG._extract_items("t", "page", {"title": "", "url": ""}, [], lambda m: None)
    assert len(items) == 1 and items[0]["title"].startswith("某公司被罚")


# ---------------- pdf_attach ----------------

def test_pdf_header_collision_loop(monkeypatch):
    from universal_scraper import pdf_attach as PA
    # 伪造 pdfplumber 返回碰撞表头
    class FakePage:
        def extract_tables(self):
            return [[["项目", "数量", "数量2", "数量"], ["甲", "1", "2", "3"]]]
    class FakePDF:
        pages = [FakePage()]
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    import types
    fake_mod = types.SimpleNamespace(open=lambda _p: FakePDF())
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_mod)
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tf:
        tf.write(b"%PDF-1.4 fake content for magic check\n")
        tf.flush()
        res = PA.extract_tables(tf.name)
    row = res[0]["rows"][0]
    # 四列全部保留、无覆盖（修前第 4 列吃掉第 3 列）
    assert len(row) == 4 and len(set(row.values())) == 4
