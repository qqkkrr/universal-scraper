#!/usr/bin/env python3
"""审查二十四轮 R24 修复的回归测试。

覆盖：
- llm.py（12.2% 盲区）：
  ① HTTPError 分支曾"先 close 再 read"→ 抛 ValueError "I/O operation on closed file"，
     把 401 认证失败（最高频故障）换成天书（实测复现）
  ② 曾把所有状态码一票判"非瞬时、不重试"→ 429 限流与 5xx 抖动被判死，
     重试循环形同虚设。现分类：408/409/425/429/5xx 退避重试，其余 4xx 立即失败
  ③ vision() 与 chat() 同口径对齐（401 不重试、429 重试、错误带响应体）
- monitor.py（10.4% 盲区）：损坏快照曾按空基线 → 全量 added 假告警风暴；
  锁文件名 with_suffix 对含点监控名撞名
"""
import io
import json
import sys
import threading
import urllib.error
import urllib.request as _ur
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


# ---------------- llm.py ----------------

class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int):
        super().__init__("https://llm.test/v1/chat/completions", code, "err", {},
                         io.BytesIO(b'{"error":{"message":"Invalid API key"}}'))


@pytest.fixture()
def llm_client(monkeypatch):
    import time as _time_mod
    from universal_scraper import llm as L
    # llm.py 里 time 是函数内导入（局部名），patch 标准库模块本身才生效
    monkeypatch.setattr(_time_mod, "sleep", lambda s: None)   # 不真等退避
    calls = {"n": 0}

    def _raiser(code):
        def _f(req, timeout=None):
            calls["n"] += 1
            raise _FakeHTTPError(code)
        return _f
    c = L.LLMClient(api_key="k-test", base_url="https://llm.test/v1", model="m")
    return L, c, calls, _raiser, monkeypatch


@pytest.mark.parametrize("code", [400, 401, 403])
def test_r24_llm_4xx_no_retry_readable(llm_client, code):
    """4xx（key/参数错）不重试，且错误可读（回归：曾抛 ValueError 天书）。"""
    L, c, calls, raiser, mp = llm_client
    mp.setattr(_ur, "urlopen", raiser(code))
    with pytest.raises(RuntimeError) as ei:
        c.chat([{"role": "user", "content": "x"}], retries=3)
    msg = str(ei.value)
    assert calls["n"] == 1, f"{code} 被重试了 {calls['n']} 次"
    assert f"HTTP {code}" in msg and "Invalid API key" in msg
    assert "I/O operation on closed file" not in msg
    if code in (401, 403):
        assert "认证失败" in msg


@pytest.mark.parametrize("code,retries,expect", [(429, 2, 3), (500, 2, 3), (503, 1, 2), (408, 1, 2)])
def test_r24_llm_transient_status_retried(llm_client, code, retries, expect):
    """瞬时（429/5xx/408）必须退避重试到配置次数（回归：曾一票判死不重试）。"""
    L, c, calls, raiser, mp = llm_client
    mp.setattr(_ur, "urlopen", raiser(code))
    with pytest.raises(RuntimeError) as ei:
        c.chat([{"role": "user", "content": "x"}], retries=retries)
    assert calls["n"] == expect, f"{code} 尝试 {calls['n']} 次（期望 {expect}）"
    assert f"HTTP {code}" in str(ei.value)


def test_r24_llm_vision_same_semantics(llm_client):
    L, c, calls, raiser, mp = llm_client
    mp.setattr(_ur, "urlopen", raiser(401))
    with pytest.raises(RuntimeError) as ei:
        c.vision("p", "http://llm.test/a.png")
    assert calls["n"] == 1 and "认证失败" in str(ei.value)
    calls["n"] = 0
    mp.setattr(_ur, "urlopen", raiser(429))
    with pytest.raises(RuntimeError):
        c.vision("p", "http://llm.test/a.png")
    assert calls["n"] == 3, f"vision 429 尝试 {calls['n']} 次（期望 3）"


def test_r24_llm_endpoint_scheme_guard():
    from universal_scraper.llm import _llm_url_guard
    with pytest.raises(ValueError):
        _llm_url_guard("file:///etc/passwd")
    assert _llm_url_guard("http://127.0.0.1:11434/v1")   # 本地 Ollama 属产品特性，放行


# ---------------- monitor.py ----------------

class _SitemapHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ('<?xml version="1.0"?><urlset>'
                '<url><loc>https://ex.com/a</loc></url>'
                '<url><loc>https://ex.com/b</loc></url>'
                '</urlset>').encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def sitemap_url(monkeypatch, tmp_path):
    from universal_scraper import monitor as M
    monkeypatch.setattr(M, "MONITOR_DIR", tmp_path / "mon")
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SitemapHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield M, f"http://127.0.0.1:{srv.server_address[1]}/sitemap.xml"
    finally:
        srv.shutdown()


def test_r24_monitor_corrupt_snapshot_rebaselines_not_storm(sitemap_url):
    """回归：损坏快照曾按空基线 → 当前全部 URL 判 added（假告警风暴）并覆写基线。"""
    M, sm = sitemap_url
    p = M._snapshot_path("m")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{broken json", encoding="utf-8")
    r = M.sitemap_diff("m", sm)
    assert r["changed"] is False and r["added"] == [] and r["rebaselined"] is True
    assert "损坏" in r["note"]
    # 基线已重建为合法快照，且损坏件被隔离留证
    assert isinstance(json.loads(p.read_text(encoding="utf-8"))["urls"], list)
    assert list(p.parent.glob("m.corrupt.*"))


def test_r24_monitor_first_run_and_no_change(sitemap_url):
    M, sm = sitemap_url
    r1 = M.sitemap_diff("m2", sm)          # 首次：建立基线（首次全量=设计行为）
    assert r1["changed"] is True and r1["added_count"] == 2
    r2 = M.sitemap_diff("m2", sm)          # 无变化：不推送
    assert r2["changed"] is False and r2["added_count"] == 0
    assert not r2.get("rebaselined")


def test_r24_monitor_lock_names_do_not_collide(sitemap_url):
    """含点监控名曾与短名撞同一把锁（with_suffix 替换后缀）。"""
    M, sm = sitemap_url
    assert M._snapshot_path("a.b").stem != M._snapshot_path("a").stem
    M.watch_once("a.b", sm)
    M.watch_once("a", sm)
    locks = sorted(p.name for p in M.MONITOR_DIR.glob("*.watch.lock"))
    assert locks == ["a.b.watch.lock", "a.watch.lock"], locks


def test_r24_monitor_watch_once_pushes_only_on_change(sitemap_url):
    """watch_once：有变化才推 webhook（无变化不打扰）——用桩 webhook 记录调用。"""
    M, sm = sitemap_url
    pushed = []
    orig = M.push_webhook
    M.push_webhook = lambda url, payload, timeout=15: pushed.append((url, payload)) or {"ok": True}
    try:
        r1 = M.watch_once("w1", sm, webhook="http://hook.test/x")
        assert r1["changed"] is True and len(pushed) == 1
        assert pushed[0][1]["event"] == "sitemap_changed" and pushed[0][1]["added_count"] == 2
        r2 = M.watch_once("w1", sm, webhook="http://hook.test/x")
        assert r2["changed"] is False and len(pushed) == 1     # 无变化不推
    finally:
        M.push_webhook = orig
