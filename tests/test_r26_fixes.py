#!/usr/bin/env python3
"""审查二十六轮 R26 修复的回归测试。

覆盖：
- **precise_auto 同域闸（F1，提示注入→毒配置持久化）**：`_llm_repair` 与
  `_entry_candidates` 的 LLM 产出会写回 meta 并注册进精配注册表——页面内容进
  提示词后，恶意页可注入"把入口改成 attacker.com"，让该 host 后续所有任务
  抓第三方站并当原站数据导出。修复后跨域建议被忽略（连试跑都不去）
- **守卫副本统一（F2）**：`_assert_http_url` 的主机判定收敛到 core._host_is_private
  （本模块副本曾缺 multicast、空主机名裸抛 gaierror）；保留无条件拒绝语义
"""
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


# ---------- F1: 同域闸 ----------

def test_r26_same_site_semantics():
    from universal_scraper.precise_auto import _same_site
    assert _same_site("https://www.x.com/list", "https://x.com/")
    assert _same_site("http://sub.x.com/a", "https://x.com/")
    assert _same_site("https://x.com:8080/p", "http://WWW.X.COM")
    for bad in ("https://evil.com/", "https://evil-x.com/", "https://x.com.evil.com/", ""):
        assert not _same_site(bad, "https://x.com/"), bad


def test_r26_repair_probe_ignores_cross_domain_entry(monkeypatch):
    """回归：LLM 修复建议的跨域入口不得被试跑、更不得写回 meta（会被注册持久化）。"""
    from universal_scraper import precise_auto as PA

    meta = {"host": "x.com", "tactic": "pdf_attach", "description": "抓公告",
            "entry": "http://x.com/a", "params": {"entry": "http://x.com/a"}}
    calls = []

    def runner(m, u, limit, log=None):
        calls.append(u)
        if len(calls) == 1:
            raise RuntimeError("round1 fail")
        return [{"标题": "x"}], {"json": "p"}, "ok"

    monkeypatch.setattr(PA, "_llm_repair",
                        lambda m, err, log=None: {"entry": "http://evil.com/x", "params": {}})
    rows, files, detail = PA._repair_probe(meta, "http://x.com/a", 5, runner, log=None)
    assert rows and len(calls) == 2
    assert calls[1] == "http://x.com/a", f"跨域入口被拿去试跑: {calls[1]}"
    assert meta["entry"] == "http://x.com/a", f"跨域入口写回了 meta: {meta['entry']}"
    assert meta["params"]["entry"] == "http://x.com/a"


def test_r26_repair_probe_accepts_same_domain_entry(monkeypatch):
    """同域修复建议照常采纳（www 子域/换路径都行）——闸不误伤正常自愈。"""
    from universal_scraper import precise_auto as PA

    meta = {"host": "x.com", "tactic": "pdf_attach", "description": "抓公告",
            "entry": "http://x.com/a", "params": {"entry": "http://x.com/a"}}
    calls = []

    def runner(m, u, limit, log=None):
        calls.append(u)
        if len(calls) == 1:
            raise RuntimeError("round1 fail")
        return [{"标题": "x"}], {"json": "p"}, "ok"

    monkeypatch.setattr(PA, "_llm_repair",
                        lambda m, err, log=None: {"entry": "http://www.x.com/b", "params": {}})
    PA._repair_probe(meta, "http://x.com/a", 5, runner, log=None)
    assert calls[1] == "http://www.x.com/b"
    assert meta["entry"] == "http://www.x.com/b"          # 同域：正常回写


def test_r26_entry_candidates_skip_cross_domain(monkeypatch):
    """LLM 候选入口的跨域项不得被探测（更不能成为新入口）；同域项照常探测。"""
    from universal_scraper import precise_auto as PA

    probed = []

    def fake_probe(u, log=None):
        probed.append(u)
        if u == "http://x.com/list":
            return {"url": u, "http_status": 200, "waf": False,
                    "detect": {"textLen": 5000, "textHead": "数据", "_url": u}}
        return {"url": u, "http_status": 500, "waf": True,
                "detect": {"textLen": 0, "textHead": "", "_url": u}}

    monkeypatch.setattr(PA, "_probe_advanced", fake_probe)

    import types
    import universal_scraper.llm as LLM_MOD

    class _FakeLLM:
        def __init__(self, *a, **k):
            pass

        def chat(self, messages, **k):
            return '["http://evil.com/a", "http://x.com/list"]'
    monkeypatch.setattr(LLM_MOD, "LLMClient", _FakeLLM)

    probe = PA._entry_candidates("抓公告", "http://x.com/dead", log=None)
    assert probe.get("detect", {}).get("_url") == "http://x.com/list"
    assert not any(u.startswith("http://evil.com") for u in probed), \
        f"跨域候选被探测: {probed}"


# ---------- F2: 守卫副本统一 ----------

@pytest.mark.parametrize("url", ["file:///etc/passwd", "http:///path", "http://127.0.0.1/",
                                 "http://[fd00::1]/", "http://[ff02::1]/", "http://192.168.1.1/",
                                 "http://10.0.0.9/", "ftp://x.com/"])
def test_r26_assert_http_url_rejects(url):
    """守卫收敛后的语义（含 R26 新覆盖：multicast、空主机名不再裸抛 gaierror）。"""
    from universal_scraper.precise_auto import _assert_http_url
    with pytest.raises(ValueError):
        _assert_http_url(url)


def test_r26_assert_http_url_allows_public_ip(monkeypatch):
    from universal_scraper.precise_auto import _assert_http_url
    monkeypatch.delenv("US_ALLOW_PRIVATE", raising=False)   # 无条件拒绝语义：不吃环境白名单
    assert _assert_http_url("http://1.2.3.4/x") == "http://1.2.3.4/x"
