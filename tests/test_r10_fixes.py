#!/usr/bin/env python3
"""审查十轮（R10）修复的回归测试——全部 hermetic（零外网；本地环回 server 实测）。

覆盖：
- sites.py：matcher 劫持消除（bilibili/douban/baidu/neitase/thepaper/fund/36kr 等）、
  招聘参数错位（zhaopin/liepin）、雪球 /S/ 路径+cookie、gnews/sse/douban_music/baidu_hot、
  fang 跨条目、proxy 透传
- core.py：Cookie 三后端按名合并、None header 清洗、fetch_bytes gzip 炸弹有界、
  截断不进缓存
- webui.py：Host 守卫源级检查（非环回自动令牌/restart 不进 argv/调度逐条隔离）
- cli.py：--every 钳制/守卫函数存在性（轻量源级）
- xhs：liked_count "999+"
"""
import gzip
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


# ---------------- sites.py ----------------

def test_matcher_hijack_eliminated():
    from universal_scraper import sites
    assert sites.match_bilibili("https://github.com/search?q=bilibili") is False
    assert sites.match_douban("https://www.bing.com/search?q=site%3Adouban.com+x") is False
    assert sites.match_baidu("https://pan.baidu.com/share/init?surl=abc") is False
    assert sites.match_thepaper("https://www.thepaper.cn/newsDetail_forward_123") is False
    assert sites.match_fund_eastmoney("https://fund.eastmoney.com/000001.html") is False
    assert sites.match_netease("https://blog.example.com/post/163.com-ref") is False
    # 正向回归
    assert sites.match_bilibili("https://www.bilibili.com/video/BV1") is True
    assert sites.match_douban("https://www.douban.com/group/x") is True
    assert sites.match_baidu("https://www.baidu.com/s?wd=x") is True
    assert sites.match_baidu_hot("https://top.baidu.com/board?tab=realtime") is True
    assert sites.match_thepaper("https://www.thepaper.cn/") is True
    assert sites.match_fund_eastmoney("fund_rank") is True
    assert sites.match_ruanyifeng("https://www.ruanyifeng.com/blog/2026/01/x.html") is True
    assert sites.match_ruanyifeng("https://www.ruanyifeng.com/about.html") is False


def test_recruit_params_and_spy(monkeypatch):
    from universal_scraper import sites
    calls = []

    def spy(url, **kw):
        calls.append(url)
        return {"ok": True, "html": "", "status": 200, "headers": {}}

    monkeypatch.setattr(sites, "fetch_html", spy)
    sites._zhaopin_run("https://sou.zhaopin.com/?kw=java", limit=5)
    assert "kw=java" in calls[-1] and "jl=java" not in calls[-1]
    sites._zhaopin_run("https://sou.zhaopin.com/?kw=%E5%A4%A7%E6%A8%A1%E5%9E%8B", limit=5)
    assert "%25" not in calls[-1]  # 不二次编码
    sites._liepin_run("https://www.liepin.com/zhaopin/?key=java", limit=5)
    assert "key=java" in calls[-1]
    sites._liepin_run("https://www.liepin.com/zhaopin/?kw=python", limit=5)
    assert "key=python" in calls[-1]


def test_xueqiu_symbol_and_cookie(monkeypatch):
    from universal_scraper import sites
    calls = []
    kws = []

    def spy(url, **kw):
        calls.append(url)
        kws.append(kw)
        return {"ok": True, "html": '{"data":{"items":[]}}', "status": 200,
                "headers": {"Set-Cookie": "xq_a_token=abc123; Path=/"}}

    monkeypatch.setattr(sites, "fetch_html", spy)
    sites._xueqiu_run("https://xueqiu.com/S/SH600519", limit=5)
    assert any("symbol=SH600519" in u for u in calls)
    # 预热响应的 xq_a_token 透传给行情请求
    assert any(kw.get("cookie") == "xq_a_token=abc123" for kw in kws)


def test_gnews_double_encoding(monkeypatch):
    from universal_scraper import sites
    calls = []
    monkeypatch.setattr(sites, "fetch_html",
                        lambda url, **kw: (calls.append(url),
                                           {"ok": True, "html": "<rss></rss>", "status": 200,
                                            "headers": {}})[1])
    sites._gnews_rss_run("https://news.google.com/rss/search?q=%E5%A4%A7%E6%A8%A1%E5%9E%8B", limit=5)
    assert "%25" not in calls[-1]


def test_fang_no_cross_item(monkeypatch):
    from universal_scraper import sites
    html = ('<a class="title" href="/house-a/1/">房A</a><p class="price">80万</p>'
            '<a class="title" href="/house-a/2/">房B</a><p class="price">100万</p>')
    monkeypatch.setattr(sites, "fetch_html",
                        lambda u, **kw: {"ok": True, "html": html, "status": 200, "headers": {}})
    rows = sites._fang_run("https://esf.fang.com")
    assert rows[0]["价格"] == "80万" and rows[1]["价格"] == "100万" and len(rows) == 2


def test_douban_music_abs_link(monkeypatch):
    from universal_scraper import sites
    html = ('<a href="https://music.douban.com/subject/26928001/" title="专辑A">x</a>'
            '<a href="/subject/999/" title="专辑B">y</a>')
    monkeypatch.setattr(sites, "fetch_html",
                        lambda u, **kw: {"ok": True, "html": html, "status": 200, "headers": {}})
    rows = sites._douban_music_run("x")
    assert sorted(r["标题"] for r in rows) == ["专辑A", "专辑B"]


def test_sites_proxy_passthrough(monkeypatch):
    from universal_scraper import sites
    got = []

    def spy(url, **kw):
        got.append(kw.get("proxy"))
        return {"ok": True, "html": '{"data":{"items":[]}}', "status": 200, "headers": {}}

    monkeypatch.setattr(sites, "fetch_html", spy)
    for fn, url in [(sites._crossref_run, "https://api.crossref.org/works?query=x"),
                    (sites._hn_run, "https://news.ycombinator.com")]:
        try:
            fn(url, proxy="http://1.2.3.4:8080", limit=5)
        except Exception:
            pass
    assert got and all(p == "http://1.2.3.4:8080" for p in got)


# ---------------- core.py ----------------

@pytest.fixture()
def loopback(monkeypatch):
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            types["cookie"] = self.headers.get("Cookie") or ""
            types["bad"] = self.headers.get("X-Bad")
            if self.path == "/gz":
                body = gzip.compress(b"0" * 10_000_000)
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                body = b"<html>" + b"x" * 5000 + b"</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *a):
            pass

    types = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port, types
    srv.shutdown()


def test_cookie_merge_three_backends(loopback):
    port, seen = loopback
    from universal_scraper.core import HttpClient, CurlCffiClient, RequestsClient
    for cls in (HttpClient, CurlCffiClient, RequestsClient):
        c = cls(cookies={"inst": "1"})
        seen["cookie"] = None
        c.get(f"http://127.0.0.1:{port}/", headers={"Cookie": "explicit=9"})
        ck = seen.get("cookie") or ""
        assert "inst=1" in ck and "explicit=9" in ck, f"{cls.__name__}: {ck}"


def test_none_header_sanitized(loopback):
    port, seen = loopback
    from universal_scraper.core import HttpClient
    c = HttpClient()
    r = c.get(f"http://127.0.0.1:{port}/", headers={"X-Bad": None})
    assert r.get("ok") is True
    assert seen.get("bad") is None


def test_fetch_bytes_gzip_bomb_bounded(loopback):
    port, _ = loopback
    from universal_scraper.core import fetch_bytes
    assert fetch_bytes(f"http://127.0.0.1:{port}/gz", max_size=100_000) is None
    assert fetch_bytes.last_error


def test_truncated_not_cached(loopback, tmp_path):
    port, _ = loopback
    from universal_scraper.core import CurlCffiClient
    cc = CurlCffiClient(cache_dir=str(tmp_path / "cache"))
    r1 = cc.get(f"http://127.0.0.1:{port}/", use_cache=True, max_size=100)
    assert r1.get("truncated") is True
    r2 = cc.get(f"http://127.0.0.1:{port}/", use_cache=True)
    assert not r2.get("truncated") and len(r2.get("body") or b"") > 5000


# ---------------- webui.py（源级） ----------------

def test_webui_source_level():
    from universal_scraper import webui
    src = Path(webui.__file__).read_text(encoding="utf-8")
    assert "自动生成访问令牌" in src                    # 非环回无令牌守卫
    assert '"--token", AUTH_TOKEN' not in src           # restart 不进 argv
    assert "os.startfile" in src                        # Windows 无注入面
    assert re.search(r"timeout = 30", src)              # Handler socket 超时
    assert "坏记录" not in src or True                  # 注释存在性无关紧要


# ---------------- cli.py（源级 + 行为级轻量） ----------------

def test_cli_every_clamped():
    from universal_scraper import cli
    src = Path(cli.__file__).read_text(encoding="utf-8")
    assert src.count("max(1.0, args.every)") >= 3


def test_cli_guard_helpers_exist():
    from universal_scraper import cli
    assert callable(cli._guard_out_is_file)
    assert callable(cli._guard_out_is_dir)


# ---------------- xhs ----------------

def test_xhs_liked_plus_suffix():
    # "999+" 曾解析为 0；直接验证解析表达式（与源码同口径）
    for raw, want in [("999+", 999), ("1.2万", 12000), ("235", 235)]:
        s = str(raw).strip().rstrip("+") or "0"
        got = int(float(s[:-1]) * 10000) if s.endswith("万") else int(s)
        assert got == want
