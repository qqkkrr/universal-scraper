#!/usr/bin/env python3
"""审查八轮（R9）修复的回归测试——全部 hermetic（零网络/零真实台账）。

覆盖：
- ssr_state._replace_ctor 字符串感知（C1）
- engine resume 合并无键行（C2，逻辑段复现）
- action_budget null 穿透 / limit 告警 / limit=0 忙循环
- adaptive 只读目录 save 不炸 + 域值类型校验
- captcha_ocr _prompt_targets 不吞目标字
- engine→browser_pw 渲染配置透传（逻辑段）
- proxy_fetch CF 特征不误杀 / PoolState 归一化 / refresh 合并 alive（逻辑段）
- monitor _is_index 收窄
- report._num 时间区间
- solutions 状态码边界
- queue extract_links 属性前缀
"""
import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path


SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


# ---------- ssr_state（C1）----------

def test_ssr_ctor_string_aware():
    from universal_scraper.ssr_state import extract_state
    s = '<script>window.__S__={"tip":"use new Set(items) here","n":1}</script>'
    assert extract_state(s, "__S__") == {"tip": "use new Set(items) here", "n": 1}
    # 正常 ctor 剥除不回归
    s2 = '<script>window.__S__={"tags":new Set(["x","y"])}</script>'
    assert extract_state(s2, "__S__") == {"tags": ["x", "y"]}
    # 混合场景
    s3 = '<script>window.__S__={"tip":"new Set(a)","tags":new Set(["p"])}</script>'
    assert extract_state(s3, "__S__") == {"tip": "new Set(a)", "tags": ["p"]}


# ---------- engine resume 合并（C2，逻辑段复现）----------

def _merge_rows(old_rows, rows, detail):
    from universal_scraper.storage import record_key
    keyf = detail.get("resume_key") or detail.get("url_field") or "url"
    old_by_key = {}
    for o in old_rows:
        _ok = record_key(o, keyf)
        if _ok:
            old_by_key[_ok] = o
    merged = 0
    for r in rows:
        k = record_key(r, keyf)
        if not k:
            continue
        o = old_by_key.get(k)
        if o and (o.get("detail_body") or "").strip():
            for kk, vv in o.items():
                if kk not in r or not r.get(kk):
                    r[kk] = vv
            merged += 1
    return merged


def test_engine_resume_keyless_rows_not_merged():
    old = [{"url": "http://x/1", "detail_body": "PAGE-A-BODY"},
           {"url": "http://x/2", "detail_body": "PAGE-B-BODY"}]
    new = [{"url": "http://x/1"}, {"url": "http://x/2"}]
    assert _merge_rows(old, new, {}) == 2
    # 默认键=url 时各归各（不串行到同一条）
    assert new[0]["detail_body"] == "PAGE-A-BODY"
    assert new[1]["detail_body"] == "PAGE-B-BODY"
    # 无键行跳过合并（修前坍缩进 "" 桶共享最后一条旧详情）
    new2 = [{"title": "no-key-1"}, {"title": "no-key-2"}]
    assert _merge_rows(old, new2, {}) == 0
    assert "detail_body" not in new2[0]
    # resume_key=null 走 or 链回退 url_field
    assert _merge_rows(old[:1], [{"url": "http://x/1"}], {"resume_key": None}) == 1
    # 变异测试暴露（R18）：上方 _merge_rows 是逻辑段复现（复制品）——真实
    # engine.py 被改坏时本测试不报警。补源级结构断言双保险：resume 合并段
    # 必须同时有"构建旧键桶跳过空键"与"查找侧跳过空键"两个守卫
    import inspect
    from universal_scraper import engine as E
    src = inspect.getsource(E)
    assert 'keyf = detail.get("resume_key") or detail.get("url_field") or "url"' in src
    _seg = src[src.index('keyf = detail.get("resume_key")'):]
    _seg = _seg[:_seg.index("logger.info")]
    assert "_ok = record_key(o, keyf)" in _seg and "if _ok:" in _seg   # 构建侧守卫
    assert "if not k:" in _seg                                         # 查找侧守卫


# ---------- action_budget ----------

def test_action_budget_null_and_zero(tmp_path, monkeypatch):
    led = tmp_path / "ab.json"
    led.write_text(json.dumps({
        "action_budgets": {"k_null": {"limit": None, "window": None, "events": None}}
    }), encoding="utf-8")
    monkeypatch.setattr("universal_scraper.action_budget.DEFAULT_FILE", str(led))
    from universal_scraper import action_budget as ab
    ab._WARNED_MISMATCH.clear()
    # 显式 null 回退调用参数（修前 int(None) 崩）
    st = ab.state("k_null", limit=7, window=1800)
    assert st["limit"] == 7 and st["window_sec"] == 1800
    r = ab.acquire("k_null", limit=7, window=1800)
    assert r["allowed"] is True
    # limit=0 拒绝且 reset_in=window（修前 0.0 忙循环）
    r0 = ab.acquire("k_zero", limit=0, window=3600)
    assert r0["allowed"] is False and r0["reset_in_sec"] == 3600.0
    # limit 不一致告警
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        ab.acquire("k_m", limit=5, window=3600)
        r2 = ab.acquire("k_m", limit=99, window=3600)
    assert r2["limit"] == 5 and "被忽略" in err.getvalue()


# ---------- adaptive ----------

def test_adaptive_readonly_save_and_type_check(tmp_path):
    from universal_scraper import adaptive as ad
    ro = tmp_path / "configs"
    ro.mkdir()
    (ro / "_adaptive_strategies.json").write_text("{}", encoding="utf-8")
    ad.STRATEGY_FILE = ro / "_adaptive_strategies.json"
    ad._SAVE_WARNED = False
    os.chmod(ro, 0o555)
    try:
        ad.save_strategy("https://example.com/page", "http")  # 修前 PermissionError
    finally:
        os.chmod(ro, 0o755)
    # 域值非 dict 载入期剔除
    f2 = tmp_path / "s2.json"
    f2.write_text(json.dumps({"example.com": "browser_headless",
                              "ok.com": {"best_strategy": "http"}}), encoding="utf-8")
    ad.STRATEGY_FILE = f2
    ad._SAVE_WARNED = False
    assert ad.get_cached_strategy("https://ok.com/x") == "http"
    assert ad.get_cached_strategy("https://example.com/x") is None
    ad.save_strategy("https://example.com/p2", "curl_cffi")
    d = json.loads(f2.read_text(encoding="utf-8"))
    assert d["example.com"]["best_strategy"] == "curl_cffi"
    assert d["ok.com"]["best_strategy"] == "http"


# ---------- captcha_ocr ----------

def test_prompt_targets_keeps_target_chars():
    from universal_scraper.captcha_ocr import _prompt_targets
    assert _prompt_targets("依次点击：点 击 试") == ["点", "击", "试"]      # 修前 ['试']
    assert _prompt_targets("依次点击下面的字 点 击") == list("下面的字点击")  # 修前真目标全丢
    # 回归：正常形态
    assert _prompt_targets("依次点击：国 家 税") == ["国", "家", "税"]
    assert _prompt_targets("请点击 国 家 税") == ["国", "家", "税"]
    assert _prompt_targets("请依次点击：【国】【家】【税】") == ["国", "家", "税"]


# ---------- engine→browser_pw 透传（逻辑段）----------

def test_engine_pw_source_passthrough():
    def build_src(detail, source):
        _src = {"cdp": detail.get("cdp") or (source or {}).get("cdp")}
        for _k in ("wait_until", "dom_stable", "headless"):
            _v = detail.get(_k)
            if _v is None:
                _v = (source or {}).get(_k)
            if _v is not None:
                _src[_k] = _v
        return _src

    assert build_src({"wait_until": "networkidle"}, {}).get("wait_until") == "networkidle"
    assert build_src({"headless": False}, {}).get("headless") is False
    assert build_src({}, {"dom_stable": True}).get("dom_stable") is True
    assert "wait_until" not in build_src({}, {})
    assert "headless" not in build_src({"headless": None}, {})


def test_browser_pw_reads_source():
    from universal_scraper.browser_pw import PWBrowserFetcher
    f = PWBrowserFetcher({"wait_until": "networkidle", "headless": False}, {}, {}, SKILL)
    assert f.source.get("wait_until") == "networkidle"
    assert bool(f.source.get("headless", True)) is False
    f2 = PWBrowserFetcher({}, {}, {}, SKILL)
    assert bool(f2.source.get("headless", True)) is True  # 未配默认无头（保持现状）


# ---------- proxy_fetch ----------

def test_proxy_cf_hint_not_overblocking():
    from universal_scraper.proxy_fetch import _looks_like_real_page
    rocket = (b'<html><head><script src="/cdn-cgi/cloudflare-static/rocket-loader.min.js">'
              b'</script></head><body>' + b"x" * 2400 + b"</body></html>")
    assert _looks_like_real_page(rocket) is True          # 修前 False（误杀）
    assert _looks_like_real_page(b"<script>var __cf_chl_opt={};</script>") is False
    assert _looks_like_real_page(b"<html><body>captcha required</body></html>") is False


def test_pool_state_load_normalizes_records(tmp_path):
    from universal_scraper.proxy_fetch import PoolState
    f = tmp_path / "pool.json"
    f.write_text(json.dumps({
        "http://1.2.3.4:80": {"state": "fresh", "ok": 1, "ts": 0},
        "http://5.6.7.8:80": {"state": "alive", "blocked": "2", "latency_ms": "x"},
    }), encoding="utf-8")
    st = PoolState(f)
    st.mark("http://1.2.3.4:80", "burned")   # 修前 KeyError: blocked
    st.mark("http://5.6.7.8:80", "burned")   # 修前 TypeError
    assert st.data["http://1.2.3.4:80"]["blocked"] == 1
    assert st.data["http://5.6.7.8:80"]["blocked"] == 1


def test_refresh_merges_alive_proxies(tmp_path):
    from universal_scraper.proxy_fetch import PoolState
    st = PoolState(tmp_path / "pool.json")
    st.data = {"http://1.1.1.1:80": {"state": "alive"},
               "http://3.3.3.3:80": {"state": "fresh"},
               "http://4.4.4.4:80": {"state": "dead"}}
    good = {"http://9.9.9.9:80": 50}
    _alive = [px for px, rec in st.data.items()
              if isinstance(rec, dict) and rec.get("state") == "alive"]
    _lines = list(dict.fromkeys(list(good) + sorted(_alive)))
    assert _lines == ["http://9.9.9.9:80", "http://1.1.1.1:80"]  # 修前丢 1.1.1.1


# ---------- monitor ----------

def test_monitor_is_index_narrowed():
    def is_index(loc):
        _path = loc.split("?", 1)[0].lower()
        _fname = _path.rstrip("/").rsplit("/", 1)[-1]
        return bool(re.match(r"sitemap.*(\.xml(\.gz)?|\.gz)$", _fname))

    assert is_index("https://s.com/sitemap.xml")
    assert is_index("https://s.com/sitemap_index.xml")
    assert is_index("https://s.com/sitemap.xml.gz")
    # 叶子 URL 不再误判（修前恒 True → 丢数据 + 白烧请求）
    assert not is_index("https://site.com/article/sitemap-guide.html")
    assert not is_index("https://site.com/download/feed.xml")
    assert not is_index("https://site.com/data.xml?id=1")


# ---------- report ----------

def test_report_num_time_range():
    from universal_scraper.report import _num
    assert _num("12:30-14:00") is None      # 修前 22.0
    assert _num("11:30-14:30") is None
    # 回归：正常区间/单位
    assert _num("4-5万") == 45000.0
    assert _num("2000-3000") == 2500.0
    assert _num("010-88886666") is None


# ---------- solutions ----------

def test_solutions_status_code_boundaries():
    from universal_scraper.solutions import classify_failure
    # 数据量数字/页码不再撞状态码（修前 ip_blocked）
    assert classify_failure("解析到 403 条记录，已导出") == "unknown"
    assert classify_failure("共抓取 429 条数据") == "unknown"
    assert classify_failure("", ["第 403 页"]) == "unknown"
    assert classify_failure("", [], "http://x.com/a?page=429") == "unknown"
    # 真状态码语义保留
    assert classify_failure("HTTP Error 403: Forbidden") == "ip_blocked"
    assert classify_failure("HTTP Error 404: Not Found") == "entry_invalid"
    assert classify_failure("429 Too Many Requests") == "ip_blocked"
    assert classify_failure("", [], "http://x.com/forbidden") == "ip_blocked"


# ---------- queue ----------

def test_extract_links_ignores_data_href():
    from universal_scraper.queue import extract_links
    html = ('<a href="http://real.com/1">ok</a>'
            '<div data-href="http://tracker.com/pixel?x=1"></div>'
            '<use xlink:href="http://svg.com/i.svg#icon"></use>'
            '<a href=http://plain.com/x>p</a>')
    got = sorted(extract_links(html, "http://real.com/"))
    assert got == sorted(["http://real.com/1", "http://plain.com/x"])
