# -*- coding: utf-8 -*-
"""强化轮回归测试（全离线）：
- 入口 URL 变体生成与预检兜底（对标 Crawlee URL normalization）
- 拦截自适应降速 / 成功逐步恢复（对标 Scrapy AUTOTHROTTLE）
- 0 条认证墙时自动从调试 Chrome 导入会话（登录一次以后全自动的最后一环）
"""
import universal_scraper.auto as auto_mod
import universal_scraper.modules.fetchers as fetchers_mod
from universal_scraper.auto import _maybe_import_session_on_auth_wall, url_variants
from universal_scraper.modules.fetchers import HttpFetcher


# ---------- S1: URL 变体 ----------

def test_url_variants_order_and_dedup():
    vs = url_variants("https://www.example.com/list?a=1&utm_source=x#frag")
    assert vs[0] == "https://www.example.com/list?a=1", "首项=原始（去 fragment）"
    assert "utm_source" not in vs[0]
    assert "http://www.example.com/list?a=1" in vs, "协议互换"
    assert "https://example.com/list?a=1" in vs, "去 www"
    assert "https://m.example.com/list?a=1" in vs, "www 站点的移动版"
    assert len(vs) == len(set(vs)), "变体必须去重"
    assert len(vs) <= 6


def test_url_variants_bare_domain_and_ignores_non_http():
    vs = url_variants("http://example.gov.cn/zcwj/")
    assert "https://example.gov.cn/zcwj/" in vs
    assert "http://www.example.gov.cn/zcwj/" in vs, "裸域要补 www 变体"
    assert url_variants("ftp://x/") == ["ftp://x/"], "非 http(s) 原样返回"
    assert url_variants("") == []


def test_preflight_rescue_falls_back_to_variants(monkeypatch):
    """预检全失效且候选救援无果时，必须尝试 URL 变体并替换可用入口。"""
    import universal_scraper.sites as sites_mod

    calls = []

    def fake_fetch(url, timeout=15, allow_html_404=False, **kw):
        calls.append(url)
        if url.startswith("https://"):
            raise TimeoutError("down")
        return {"ok": True, "status": 200, "html": "x" * 300}

    monkeypatch.setattr(sites_mod, "fetch_html", fake_fetch)

    import universal_scraper.precise_auto as pa
    monkeypatch.setattr(pa, "_entry_candidates",
                        lambda desc, u, log=None: {"url": ""})

    cfg = {"start_urls": ["https://www.example.com/list"]}
    out = auto_mod._preflight_and_rescue(cfg, "测试", log=None)
    assert out["start_urls"][0].startswith("http://"), \
        f"应替换为可达的 http 变体: {out['start_urls']}"
    # 探测顺序锁定：原始入口 → 协议变体（首个可达即停，不空转）
    assert calls == ["https://www.example.com/list", "http://www.example.com/list"]


def test_preflight_variants_cover_mobile_when_trailing_slash(monkeypatch):
    """带尾斜杠的入口也必须探测到 m. 移动版变体（曾因切片被静默丢弃）。"""
    import universal_scraper.sites as sites_mod
    import universal_scraper.precise_auto as pa
    seen = []

    def fake_fetch(url, timeout=15, allow_html_404=False, **kw):
        seen.append(url)
        if "m.example.com" in url:
            return {"ok": True, "status": 200, "html": "m" * 300}
        raise TimeoutError("down")

    monkeypatch.setattr(sites_mod, "fetch_html", fake_fetch)
    monkeypatch.setattr(pa, "_entry_candidates", lambda d, u, log=None: {"url": ""})
    out = auto_mod._preflight_and_rescue({"start_urls": ["https://www.example.com/a/"]},
                                         "测试", log=None)
    assert out["start_urls"][0] == "https://m.example.com/a/", seen
    assert len(seen) <= 5, "原始 + 最多 4 个变体"


def test_preflight_skips_variants_for_post_entry(monkeypatch):
    """POST 型 API 入口不做 GET 变体替换（探测必 405，防误换）。"""
    import universal_scraper.sites as sites_mod
    import universal_scraper.precise_auto as pa
    seen = []

    def fake_fetch(url, timeout=15, allow_html_404=False, **kw):
        seen.append(url)
        raise TimeoutError("down")

    monkeypatch.setattr(sites_mod, "fetch_html", fake_fetch)
    monkeypatch.setattr(pa, "_entry_candidates", lambda d, u, log=None: {"url": ""})
    cfg = {"start_urls": ["https://api.example.com/v1/list"],
           "source": {"type": "http_json", "method": "POST"}}
    auto_mod._preflight_and_rescue(cfg, "测试", log=None)
    assert seen == ["https://api.example.com/v1/list"], \
        f"POST 入口不应做变体探测: {seen}"


# ---------- S2: Autothrottle ----------

class _FakeClient:
    def __init__(self):
        self.min_interval = 0.5


def _fetcher_with_fake_client():
    f = HttpFetcher.__new__(HttpFetcher)
    f.client = _FakeClient()
    f.anti = {"min_interval": 0.5}
    return f


def test_autothrottle_doubles_and_caps():
    f = _fetcher_with_fake_client()
    f._autothrottle("verify")
    assert f.client.min_interval == 2.0
    f._autothrottle("rate_limit")
    assert f.client.min_interval == 4.0
    f._autothrottle("rate_limit")
    assert f.client.min_interval == 8.0
    f._autothrottle("rate_limit")
    assert f.client.min_interval == 8.0, "封顶 8s"
    assert f.anti["min_interval"] == 8.0, "anti 必须同步（引擎/日志可见）"


def test_maybe_speedup_recovers_gradually():
    f = _fetcher_with_fake_client()
    f.client.min_interval = 8.0
    f.anti["min_interval"] = 8.0
    f.anti["base_min_interval"] = 0.5
    f._maybe_speedup(); f._maybe_speedup()
    assert f.client.min_interval == 8.0, "连续成功不足 3 次不恢复"
    f._maybe_speedup()
    assert f.client.min_interval == 4.25, "3 次成功后取中点逐步恢复"
    f._ok_streak = 0
    f._maybe_speedup(); f._maybe_speedup(); f._maybe_speedup()
    assert f.client.min_interval == 2.375


def test_autothrottle_wired_into_blocked_path():
    """blocked 分支必须调用 _autothrottle；成功路径必须调用 _maybe_speedup。"""
    from pathlib import Path
    src = Path(fetchers_mod.__file__).read_text(encoding="utf-8")
    assert "self._autothrottle(bd[\"kind\"])" in src
    assert "self._maybe_speedup()" in src
    # P1 修复行：__init__ 固化 base_min_interval（删除后懒初始化会让恢复永久失效）
    assert 'anti.setdefault("base_min_interval"' in src


def test_autothrottle_resets_streak_even_at_cap():
    """【回归 R2-1】已到 8s 封顶后再被拦：成功连击也必须清零，
    否则 1 次成功就能回速一半，恰在拦截最凶时防抖最弱。"""
    f = _fetcher_with_fake_client()
    f.anti["base_min_interval"] = 0.5
    for _ in range(3):
        f._autothrottle("rate_limit")  # 2→4→8（封顶）
    assert f.client.min_interval == 8.0
    f._maybe_speedup(); f._maybe_speedup()
    f._autothrottle("rate_limit")  # 封顶态再拦
    f._maybe_speedup()
    assert f.client.min_interval == 8.0, "封顶态拦截后 1 次成功不得回速"


def test_url_variants_ipv6_and_bad_port():
    """IPv6 字面量必须保括号；非法端口不崩。"""
    vs = url_variants("https://[2001:db8::1]/p")
    assert vs and vs[0] == "https://[2001:db8::1]/p", vs
    assert all("2001:db8::1]" in u for u in vs), f"IPv6 变体必须带括号: {vs}"
    assert all("www." not in u and "m.2001" not in u for u in vs), \
        f"IPv6 不做 www/m 变换（废变体）: {vs}"
    vs2 = url_variants("https://x.com:abc/l")
    assert isinstance(vs2, list) and vs2, "非法端口不得使变体生成崩溃"


def test_speedup_recovers_after_first_request_blocked():
    """【回归 P1】首个请求先被拦截：base 不得被记成已降速的值，
    连续成功后必须能恢复；拦截必须清零成功连击（P2b）。"""
    f = _fetcher_with_fake_client()
    f.anti["base_min_interval"] = 0.5  # __init__ 固化基准（此处显式模拟）
    f._autothrottle("verify")
    assert f.client.min_interval == 2.0
    for _ in range(3):
        f._maybe_speedup()
    assert f.client.min_interval < 2.0, "先拦后成：恢复必须生效（懒初始化曾使其永久失效）"

    f2 = _fetcher_with_fake_client()
    f2.anti["base_min_interval"] = 0.5
    f2._maybe_speedup()
    f2._maybe_speedup()
    f2._autothrottle("rate_limit")
    f2._maybe_speedup()
    assert f2.client.min_interval == 2.0, "拦截后 1 次成功不得回速（连击被清零）"


# ---------- S3: 0 条认证墙自动导入会话 ----------

def test_zero_auth_wall_auto_imports_when_chrome_alive(monkeypatch):
    import universal_scraper.cookies as cookies_mod
    import universal_scraper.agent as agent_mod

    state = {"has": False}

    monkeypatch.setattr(cookies_mod, "has_cookies", lambda d: state["has"])
    monkeypatch.setattr(agent_mod, "debug_chrome_alive", lambda: True)

    def fake_import(port=9222, log=None):
        state["has"] = True
        return {"ok": True}

    monkeypatch.setattr(cookies_mod, "import_from_cdp_patchright", fake_import)
    logs = []
    out = _maybe_import_session_on_auth_wall(
        {"start_urls": ["https://xueshu.baidu.com/s?wd=x"]},
        "页面出现「安全验证」→ 需要登录/人工验证", log=logs.append)
    assert "已自动导入登录会话" in out
    assert any("重跑" in m for m in logs), "必须告诉用户直接重跑即可"


def test_zero_auth_wall_skips_when_no_reason_or_archive(monkeypatch):
    import universal_scraper.cookies as cookies_mod
    import universal_scraper.agent as agent_mod
    called = {"import": 0}

    def no_import(port=9222, log=None):
        called["import"] += 1
        return {"ok": False}

    monkeypatch.setattr(cookies_mod, "import_from_cdp_patchright", no_import)
    # 非认证原因：不动
    out = _maybe_import_session_on_auth_wall(
        {"start_urls": ["https://x.com/"]}, "选择器不匹配", log=None)
    assert out == "选择器不匹配" and called["import"] == 0
    # 已有存档：不动（复用播种即可）
    monkeypatch.setattr(cookies_mod, "has_cookies", lambda d: True)
    out = _maybe_import_session_on_auth_wall(
        {"start_urls": ["https://x.com/"]}, "需要登录", log=None)
    assert called["import"] == 0
    # Chrome 不在线：不动
    monkeypatch.setattr(cookies_mod, "has_cookies", lambda d: False)
    monkeypatch.setattr(agent_mod, "debug_chrome_alive", lambda: False)
    out = _maybe_import_session_on_auth_wall(
        {"start_urls": ["https://x.com/"]}, "需要登录", log=None)
    assert called["import"] == 0 and out == "需要登录"
