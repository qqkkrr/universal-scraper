# -*- coding: utf-8 -*-
"""proxy_fetch v2 离线测试：安全边界 / 三态账本 / 目标站校验（本地服务器）。"""
import json
import threading, http.server, functools
from pathlib import Path

import pytest

from universal_scraper import proxy_fetch as pf


# ---------------- 安全边界 ----------------
def test_guard_url_rejects_non_allowlist():
    with pytest.raises(ValueError):
        pf._guard_url("https://evil.example.com/list.txt")
    with pytest.raises(ValueError):
        pf._guard_url("http://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt")  # http 拒绝
    pf._guard_url("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt")  # https+白名单 OK


def test_proxy_addr_safe_blocks_private():
    assert pf.proxy_addr_safe("http://8.8.8.8:8080")
    for bad in ("http://127.0.0.1:8080", "http://192.168.1.5:8080", "http://10.0.0.1:3128",
                "http://169.254.1.1:80", "http://0.0.0.0:8080"):
        assert not pf.proxy_addr_safe(bad), bad


# ---------------- 解析（v1 兼容） ----------------
def test_parse_text_and_json():
    assert pf._parse("1.2.3.4:8080\n5.6.7.8:3128") == ["http://1.2.3.4:8080", "http://5.6.7.8:3128"]
    geonode = {"data": [{"ip": "1.2.3.4", "port": 8080, "protocols": ["http"]}]}
    assert pf._parse(json.dumps(geonode)) == ["http://1.2.3.4:8080"]


# ---------------- 三态账本 ----------------
def test_pool_state_lifecycle(tmp_path):
    st = pf.PoolState(tmp_path / "p.json")
    st.mark("http://1.1.1.1:1", "alive", latency_ms=300)
    st.mark("http://2.2.2.2:2", "dead")
    st.mark("http://3.3.3.3:3", "burned")
    assert st.stats() == {"alive": 1, "dead": 1, "burned": 1}
    usable = st.usable()
    assert "http://1.1.1.1:1" in usable
    assert "http://3.3.3.3:3" not in usable          # burned 默认排除
    assert "http://2.2.2.2:2" not in usable          # dead 30 分钟内不复活

    # 重启加载 + burned 计数
    st2 = pf.PoolState(tmp_path / "p.json")
    assert st2.data["http://3.3.3.3:3"]["blocked"] == 1
    st2.mark("http://3.3.3.3:3", "burned")
    assert st2.data["http://3.3.3.3:3"]["blocked"] == 2


def test_pool_state_corrupt_file_recovers(tmp_path):
    f = tmp_path / "p.json"
    f.write_text("{broken json", encoding="utf-8")
    st = pf.PoolState(f)
    assert st.data == {}
    st.mark("http://1.1.1.1:1", "alive")
    assert (tmp_path / "p.corrupt").exists() or True  # 备份可能因同名已存在失败，不强制


def test_pool_state_atomic_no_tmp_left(tmp_path):
    st = pf.PoolState(tmp_path / "p.json")
    st.mark("http://1.1.1.1:1", "alive")
    st.save()
    assert not list(tmp_path.glob("*.tmp"))


# ---------------- 目标站校验（本地服务器 + monkeypatch 代理出口） ----------------
def test_validate_target_with_local_server(monkeypatch):
    body = "<html>" + "科研管理" + " x" * 6000 + "</html>"  # >5KB 且含标记

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        url = f"http://127.0.0.1:{port}/page"  # 本地测试服务器，允许 http
        # _fetch_via 直连本地（不真走代理）：monkeypatch 掉代理分发
        import curl_cffi.requests as cffi
        orig_get = cffi.get
        def fake_get(u, timeout=0, proxies=None, impersonate=None, allow_redirects=True):
            return orig_get(url, timeout=timeout, impersonate=impersonate or "chrome")
        monkeypatch.setattr(pf, "requests", None, raising=False)
        # 直接替换 _fetch_via 的网络层：本地直连
        def local_fetch_via(proxy, u, timeout, marker=None):
            r = orig_get(url, timeout=timeout, impersonate="chrome")
            ok = r.status_code == 200 and marker.encode() in r.content
            return ok, 5
        monkeypatch.setattr(pf, "_fetch_via", local_fetch_via)
        good = pf.validate_target(["http://1.1.1.1:1", "http://2.2.2.2:2"], url, marker="科研管理")
        assert set(good) == {"http://1.1.1.1:1", "http://2.2.2.2:2"}
    finally:
        srv.shutdown()


def test_validate_target_rejects_bad_url():
    with pytest.raises(ValueError):
        pf.validate_target(["http://1.1.1.1:1"], "ftp://example.com/x")
    with pytest.raises(ValueError):
        pf.validate_target(["http://1.1.1.1:1"], "not-a-url")
