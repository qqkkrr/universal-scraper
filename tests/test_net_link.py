# -*- coding: utf-8 -*-
"""net.py 网络链路体检离线测试：IP 查询白名单 / 系统代理检测 / 电源解析。"""
import pytest

from universal_scraper import net


def test_guard_echo_url_allowlist():
    net._guard_echo_url("http://ip-api.com/json/?lang=zh-CN")       # 白名单 OK
    with pytest.raises(ValueError):
        net._guard_echo_url("http://evil.example.com/ip")
    with pytest.raises(ValueError):
        net._guard_echo_url("ftp://ip-api.com/x")


def test_detect_system_proxy_env(monkeypatch):
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.setattr(net.shutil, "which", lambda x: None)  # 跳过 scutil/ps
    out = net.detect_system_proxy()
    assert out["enabled"] is False
    assert not out["processes"]

    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7890")
    out2 = net.detect_system_proxy()
    assert out2["enabled"] is True
    assert any(s.startswith("env:") for s in out2["sources"])
    assert "劫持" in out2["warning"] or out2["warning"]


def test_power_source_no_pmset(monkeypatch):
    monkeypatch.setattr(net.shutil, "which", lambda x: None)
    assert net.power_source()["source"] == "unknown"
