
#!/usr/bin/env python3
"""高德实战反馈回归：punish 大壳判型 / 419 / 截图落盘 / capture 原始体。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from universal_scraper.diagnose import classify_block


def test_punish_large_shell_detected():
    html = ('<html><script src="https://g.alicdn.com/awsc.js"></script>'
            '<div class="punish-component">x</div>' + "a" * 84000 + "</html>")
    r = classify_block(200, html)
    assert r["is_block"] is True
    assert r["type"] == "aliyun_punish"
    assert r["burns_budget"] is False


def test_419_session_expired_classified():
    r = classify_block(419, "")
    assert r["is_block"] is True
    assert r["type"] == "session_expired"


def test_screenshot_relative_path_resolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class FakeFetcher:
        def __init__(self, cfg, *args, **kw):
            self.cfg = cfg

        def fetch(self, req):
            for a in self.cfg.get("actions") or []:
                if a.get("type") == "screenshot":
                    Path(a["path"]).write_bytes(b"png")
            return SimpleNamespace(status=200, text="<html><body>ok</body></html>",
                                   url=str(req.url))

        def close(self):
            pass

    import universal_scraper.modules.fetchers as mf
    monkeypatch.setattr(mf, "BrowserFetcher", FakeFetcher)
    from universal_scraper.quick import fetch_url
    r = fetch_url("http://example.com/", browser=True, screenshot="shot.png")
    assert r.get("error") == ""
    assert r["screenshot"] and Path(r["screenshot"]).is_absolute()
    assert Path(r["screenshot"]).exists()


def test_capture_keeps_non_json_xhr_body():
    src = Path("scripts/browser_generic.cjs").read_text(encoding="utf-8")
    assert 'resourceType()' in src
    assert 'raw: t.slice(0, 12000)' in src
    assert 'content_type: ctAll' in src
