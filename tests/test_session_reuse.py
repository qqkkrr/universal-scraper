# -*- coding: utf-8 -*-
"""会话自动复用与编码修复的回归测试（全离线）。

覆盖：
- HTTP 路线自动携带已存档登录态（登录一次以后全自动）
- LLM 浏览器代理自动附着调试 Chrome（复用用户真实会话）
- 解决方案卡的「导入会话」第二步闭环
- 无 charset 响应头的 UTF-8 页面不再被解码成乱码（百度安全验证曾被误判为页面结构变化）
"""
import universal_scraper.agent as agent_mod
import universal_scraper.auto as auto_mod
from universal_scraper.core import _decode_body
from universal_scraper.modules.fetchers import (
    HttpFetcher,
    parse_cookie_header,
    seed_archive_into_jar,
)


# ---------- 编码修复回归 ----------

def test_decode_utf8_page_without_charset_header():
    """无 charset 的 Content-Type + UTF-8 字节：必须正确解出中文，不得乱码。"""
    raw = '<html><title>百度安全验证</title></html>'.encode("utf-8")
    s = _decode_body(raw, {"content-type": "text/html"})
    assert "百度安全验证" in s
    assert "å" not in s, "出现 latin-1 乱码特征"


# ---------- HTTP 会话自动复用 ----------

def test_parse_cookie_header():
    assert parse_cookie_header("a=1; b=2") == [("a", "1"), ("b", "2")]
    assert parse_cookie_header("a=1=x; ; c") == [("a", "1=x")]
    assert parse_cookie_header("") == []


def _jar_cookies_for(jar, url):
    from universal_scraper.core import jar_cookie_header
    return jar_cookie_header(jar, url)


def test_seed_archive_into_jar_merges_and_scopes_domain():
    """播种后：同域请求携带存档；第三方域不带（防登录态外发）；
    服务端 Set-Cookie 同名覆盖、异名共存（长任务不掉登录）。"""
    from http.cookiejar import CookieJar

    jar = CookieJar()
    seed_archive_into_jar(jar, "xueshu.baidu.com",
                          parse_cookie_header("sid=abc; uid=7"))
    assert "sid=abc" in _jar_cookies_for(jar, "https://xueshu.baidu.com/s?wd=x")
    assert "uid=7" in _jar_cookies_for(jar, "https://xueshu.baidu.com/s?wd=x")
    # 第三方域：绝不携带
    assert _jar_cookies_for(jar, "https://evil.example.com/") == ""
    # 子域允许（同主域入口页/静态域常见）
    assert "sid=abc" in _jar_cookies_for(jar, "https://a.xueshu.baidu.com/x")
    # 服务端下发同名新令牌 → 覆盖；异名（如 BAIDUID）→ 共存
    seed_archive_into_jar(jar, "xueshu.baidu.com", [("sid", "NEW")])
    out = _jar_cookies_for(jar, "https://xueshu.baidu.com/s")
    assert "sid=NEW" in out and "uid=7" in out


def test_http_fetcher_seeds_archive_cookie(monkeypatch):
    """anti.cookie_domain 有已存档会话时，HttpFetcher 解析成 pairs 备播。"""
    import universal_scraper.cookies as cookies_mod
    monkeypatch.setattr(cookies_mod, "cookie_header", lambda d: "sid=abc; uid=7")
    monkeypatch.setattr(cookies_mod, "load_cookies", lambda d: [
        # 主域与子域同名：播种时归档主域条目必须最后写入而胜出（R5-1）
        {"name": "sid", "value": "abc", "domain": ".xueshu.baidu.com", "secure": False},
        {"name": "sid", "value": "sub", "domain": ".pan.xueshu.baidu.com", "secure": False},
        {"name": "uid", "value": "7", "domain": "xueshu.baidu.com", "secure": True},
        {"name": "evil", "value": "x", "domain": ".third-party.com", "secure": False},
    ])
    logs = []
    f = HttpFetcher({"type": "http"}, {}, {"cookie_domain": "xueshu.baidu.com",
                                           "_log_cb": logs.append})
    assert f._archive_pairs == [("sid", "sub", False), ("sid", "abc", False),
                                ("uid", "7", True)], \
        "第三方域条目必须被过滤；主域条目必须排最后（后写胜出）；secure 必须保留"
    assert f._archive_domain == "xueshu.baidu.com"


def test_http_fetcher_no_archive_no_crash(monkeypatch):
    import universal_scraper.cookies as cookies_mod
    monkeypatch.setattr(cookies_mod, "cookie_header", lambda d: "")
    f = HttpFetcher({"type": "http"}, {}, {"cookie_domain": "example.com"})
    assert f._archive_pairs == []


# ---------- 浏览器代理附着调试 Chrome ----------

def test_debug_chrome_alive(monkeypatch):
    import urllib.request

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen_ok(url, timeout):
        assert "127.0.0.1:9222" in url, "只允许探测固定本机调试端点"
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen_ok)
    assert agent_mod.debug_chrome_alive() is True

    def fake_urlopen_err(url, timeout):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen_err)
    assert agent_mod.debug_chrome_alive() is False


def test_resolve_cdp_prefers_explicit_then_debug_chrome(monkeypatch):
    assert auto_mod.resolve_cdp_for_agent({"cdp": "http://1.2.3.4:9222"}) == "http://1.2.3.4:9222"
    monkeypatch.setattr(agent_mod, "debug_chrome_alive", lambda: True)
    assert auto_mod.resolve_cdp_for_agent({}) == "http://127.0.0.1:9222"
    monkeypatch.setattr(agent_mod, "debug_chrome_alive", lambda: False)
    assert auto_mod.resolve_cdp_for_agent({}) == ""


def test_auto_fallback_call_site_uses_resolve_cdp():
    """0 条回退到 LLM 浏览器代理的调用点必须使用 resolve_cdp_for_agent（源码断言，
    决策逻辑已由 test_resolve_cdp_prefers_explicit_then_debug_chrome 锁定）。"""
    from pathlib import Path
    src = Path(auto_mod.__file__).read_text(encoding="utf-8")
    assert "resolve_cdp_for_agent(_src_now)" in src
    assert src.count("_cdp0 = resolve_cdp_for_agent(_src_now)") == 1


# ---------- 第 2 轮修复回归 ----------

def test_browser_fetcher_persists_archive_storage_state(tmp_path, monkeypatch):
    """【回归 P1】BrowserFetcher 必须能把已存档登录态写成浏览器桥的 storageState。
    曾因 `from .cookies import`（应为 ..cookies）+ json 未定义被 except 静默吞掉，
    浏览器路线的会话复用整体失效。"""
    import universal_scraper.cookies as cookies_mod
    from universal_scraper.modules.fetchers import BrowserFetcher
    monkeypatch.setattr(cookies_mod, "load_storage_state",
                        lambda d: {"cookies": [{"name": "sid", "value": "abc",
                                                "domain": ".xueshu.baidu.com"}],
                                   "origins": []})
    sd = tmp_path / ".session"
    BrowserFetcher({"type": "browser"}, {},
                   {"cookie_domain": "xueshu.baidu.com", "session_dir": str(sd)})
    assert (sd / "session.json").exists(), "storageState 未写入——浏览器会话复用是死代码"


def test_browser_fetcher_saveback_gated_by_domain():
    """池渲染后的 Cookie 回存必须有 cookie_domain 门（第三方域不进存档）。"""
    from pathlib import Path
    src = Path(fetchers_src_path()).read_text(encoding="utf-8")
    assert "from ..cookies import save_cookies" in src, "save_cookies 相对导入曾写错层级"
    assert "_d = self.cookie_domain or" in src


def fetchers_src_path():
    import universal_scraper.modules.fetchers as fm
    return fm.__file__


def test_new_session_is_reseeded_after_rotation():
    """【回归 P2】轮换出的新会话必须重新播种（archive_seeded 标记在对象上，
    免疫 GC 后 id 复用导致的漏播）。"""
    from universal_scraper.session import HttpSession
    s1 = HttpSession("xueshu.baidu.com", "UA", None)
    assert s1.archive_seeded is False
    s1.archive_seeded = True
    s2 = HttpSession("xueshu.baidu.com", "UA", None)  # 轮换新会话
    assert s2.archive_seeded is False, "新会话必须被视为未播种"


def test_host_only_setcookie_replaces_seeded_dotted_version():
    """【回归 P2】服务端 Host-Only 重发同名 Cookie 时，播种的点域陈旧版必须被
    清掉——否则两个同名都发、服务端取第一个（陈旧值），长任务静默掉登录。"""
    from http.cookiejar import Cookie, CookieJar
    from universal_scraper.modules.fetchers import _reconcile_host_only_overrides
    from universal_scraper.core import jar_cookie_header

    jar = CookieJar()
    seed_archive_into_jar(jar, "xueshu.baidu.com", parse_cookie_header("sid=OLD; uid=7"))
    # 模拟服务端 Host-Only Set-Cookie: sid=NEW
    jar.set_cookie(Cookie(version=0, name="sid", value="NEW", port=None,
                          port_specified=False, domain="xueshu.baidu.com",
                          domain_specified=False, domain_initial_dot=False,
                          path="/", path_specified=True, secure=False, expires=None,
                          discard=True, comment=None, comment_url=None, rest={},
                          rfc2109=False))
    _reconcile_host_only_overrides(jar, "xueshu.baidu.com", ["sid", "uid"])
    out = jar_cookie_header(jar, "https://xueshu.baidu.com/s")
    assert out.count("sid=") == 1, f"同名 Cookie 重复下发: {out}"
    assert "sid=NEW" in out, "服务端新版必须成为唯一权威"
    assert "uid=7" in out, "异名存档 Cookie 必须保留"


def test_reconcile_covers_www_entry_host_only():
    """【回归 P2】入口是 www./子域、服务端在该主机上以 Host-Only 续发同名 Cookie：
    播种的点域陈旧版同样要被清掉（req_host 感知）。"""
    from http.cookiejar import Cookie, CookieJar
    from universal_scraper.modules.fetchers import _reconcile_host_only_overrides
    from universal_scraper.core import jar_cookie_header

    jar = CookieJar()
    seed_archive_into_jar(jar, "xueshu.baidu.com", parse_cookie_header("sid=OLD; uid=7"))
    jar.set_cookie(Cookie(version=0, name="sid", value="NEW", port=None,
                          port_specified=False, domain="www.xueshu.baidu.com",
                          domain_specified=False, domain_initial_dot=False,
                          path="/", path_specified=True, secure=False, expires=None,
                          discard=True, comment=None, comment_url=None, rest={},
                          rfc2109=False))
    _reconcile_host_only_overrides(jar, "xueshu.baidu.com", ["sid", "uid"],
                                   req_host="www.xueshu.baidu.com")
    out = jar_cookie_header(jar, "https://www.xueshu.baidu.com/s")
    assert out.count("sid=") == 1, f"同名重复: {out}"
    assert "sid=NEW" in out and "uid=7" in out


def test_w_leading_domains_not_mangled(tmp_path, monkeypatch):
    """【回归 P1】weibo/wikipedia 等 w 开头域名曾被 lstrip 按字符集毁掉
    （weibo.com→eibo.com），导致整域会话存档静默失效/跨站撞档。
    现在必须精确剥离 www. 标签，存档可写可读。"""
    import universal_scraper.cookies as cookies_mod
    monkeypatch.setattr(cookies_mod, "_cookie_path", lambda d: tmp_path / f"{d}.json")
    ok = cookies_mod.save_cookies("weibo.com", [
        {"name": "sid", "value": "1", "domain": ".weibo.com", "secure": False}])
    assert ok, "weibo.com 会话存档必须成功（不得被 mangle 成 eibo.com 后整域丢弃）"
    rows = cookies_mod.load_cookies("weibo.com")
    assert [r["name"] for r in rows] == ["sid"]
    assert cookies_mod._norm_domain("www.wikipedia.org") == "wikipedia.org"
    assert cookies_mod._norm_domain("weibo.com") == "weibo.com"


def test_ancestor_host_only_not_treated_as_authority():
    """祖先域上的 Host-Only 同名 Cookie 不会随本请求发送，不得当权威删除播种版。"""
    from http.cookiejar import Cookie, CookieJar
    from universal_scraper.modules.fetchers import _reconcile_host_only_overrides
    from universal_scraper.core import jar_cookie_header

    jar = CookieJar()
    seed_archive_into_jar(jar, "news.example.com", parse_cookie_header("sid=OLD"))
    jar.set_cookie(Cookie(version=0, name="sid", value="SERVER", port=None,
                          port_specified=False, domain="example.com",
                          domain_specified=False, domain_initial_dot=False,
                          path="/", path_specified=True, secure=False, expires=None,
                          discard=True, comment=None, comment_url=None, rest={},
                          rfc2109=False))
    _reconcile_host_only_overrides(jar, "news.example.com", ["sid"],
                                   req_host="news.example.com")
    out = jar_cookie_header(jar, "https://news.example.com/x")
    assert "sid=OLD" in out, "祖先域 Host-Only 不得触发删除播种登录态"
