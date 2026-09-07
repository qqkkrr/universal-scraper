# -*- coding: utf-8 -*-
"""jsrecon v2 离线测试：噪声过滤 / baseURL 提取 / --out 目录容错（batch1401 战训）。"""
import json

from universal_scraper.quick import js_recon


class _FakeClient:
    """离线伪 HTTP 客户端：页面 + 两个 JS 包。"""
    def __init__(self):
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if url.endswith("/list.html"):
            return {"text": '<html><script src="/static/app.js"></script>'
                    '<script src="https://cdn.example.com/vendor.js"></script></html>',
                    "status": 200}
        if url.endswith("app.js"):
            return {"text": 'axios.create({baseURL:"/api/prod"});'
                    'fetch("/svc/query/notice-list?page=1");'
                    'var e=i("baseURL\\\"),E=i(\\\"x\\\"),n=[=;{}()'  # 压缩噪声
                    , "status": 200}
        return {"text": "", "status": 200}


def test_jsrecon_filters_noise_and_extracts_baseurl(monkeypatch):
    import universal_scraper.core as core_mod
    fake = _FakeClient()
    monkeypatch.setattr(core_mod, "make_http_client", lambda cfg: fake)
    r = js_recon("https://example.com/list.html", max_scripts=6)
    assert "error" not in r
    # baseURL 正确提取
    assert "/api/prod" in r["base_urls"]
    # 真实端点保留
    assert any("notice-list" in h for h in r["api_candidates"])
    # 压缩噪声被过滤：不含代码残渣特征的候选
    for h in r["api_candidates"]:
        assert "=" not in h and "{" not in h, h


def test_jsrecon_out_directory_tolerance(monkeypatch, tmp_path):
    import universal_scraper.core as core_mod
    monkeypatch.setattr(core_mod, "make_http_client", lambda cfg: _FakeClient())
    d = tmp_path / "outdir"
    d.mkdir()
    r = js_recon("https://example.com/list.html", out=str(d))  # 传目录不抛异常
    assert r["saved"].endswith("jsrecon.json")
    assert json.loads((d / "jsrecon.json").read_text(encoding="utf-8"))["url"].endswith("list.html")


def test_jsrecon_rejects_private_target():
    r = js_recon("http://127.0.0.1:8080/x")  # 环回地址拒绝（安全边界）
    assert "error" in r
