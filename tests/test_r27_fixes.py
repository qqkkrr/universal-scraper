#!/usr/bin/env python3
"""审查二十七轮 R27 修复的回归测试。

覆盖：
- quick.py 两份守卫副本（fetch_url / js_recon）补 is_multicast——与 R26
  precise_auto 同族缺口（ff02:: 系组播地址可触达本地网络服务）。
  两入口在白名单未开时必须拒绝组播地址；白名单开启时放行（既有语义）。
"""
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))

_MCAST = "http://[ff02::1]:8080/x"


def test_r27_fetch_url_rejects_multicast(monkeypatch):
    from universal_scraper.quick import fetch_url
    monkeypatch.delenv("US_ALLOW_PRIVATE", raising=False)
    r = fetch_url(_MCAST, timeout=3)
    assert r.get("status") == 0 and "拒绝私有/保留地址" in (r.get("error") or ""), r
    assert "ff02::1" in (r.get("error") or "")


def test_r27_js_recon_rejects_multicast(monkeypatch):
    from universal_scraper.quick import js_recon
    monkeypatch.delenv("US_ALLOW_PRIVATE", raising=False)
    r = js_recon(_MCAST)
    assert "拒绝私有/保留地址" in (r.get("error") or ""), r
    # 白名单语义保留：开启后不再走守卫拒绝（进入取数层报网络/robots 错误均可）
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")
    r2 = js_recon(_MCAST)
    assert "拒绝私有/保留地址" not in (r2.get("error") or ""), r2


def test_r27_quick_guards_source_level():
    """源级不变量：quick.py 两份守卫都必须包含 multicast（防未来副本再漂移）。"""
    src = (SKILL / "universal_scraper" / "quick.py").read_text(encoding="utf-8")
    import re as _re
    # 条件可能跨行——空白归一化后整体匹配
    flat = _re.sub(r"\s+", " ", src)
    guards = _re.findall(r"is_private or _?ip\.is_loopback or _?ip\.is_reserved"
                         r" or _?ip\.is_link_local(?: or _?ip\.is_multicast)?", flat)
    assert len(guards) == 2, f"quick.py 守卫数量变化（{len(guards)}）——请同步更新本测试"
    for g in guards:
        assert "is_multicast" in g, f"守卫缺 multicast: {g}"
