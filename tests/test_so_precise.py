# -*- coding: utf-8 -*-
"""Stack Overflow 精配：描述路由命中、标签抽取、EPOCH 时间、API JSON 解析（离线 fixture）。"""
import json
from universal_scraper.sites import match_site_by_description, seed_url_for, match_stackoverflow, parse_stackoverflow

SO_JSON = json.dumps({"items": [
    {"title": "How to scrape with python?", "link": "https://so.com/q/1",
     "score": 5, "answer_count": 2, "tags": ["python", "scraping"], "creation_date": 1787000000},
    {"title": "Low score question", "link": "https://so.com/q/2",
     "score": -3, "answer_count": 0, "tags": ["python"], "creation_date": 1787000001},
]})


def test_desc_routing_hits_stackoverflow():
    desc = "Stack Overflow“Questions”标签“python”下24小时新问题，抓取高分问题"
    assert match_site_by_description(desc) == "stackoverflow"


def test_seed_has_tag_and_epoch():
    desc = "Stack Overflow 标签 java 的新问题"
    seed = seed_url_for(desc)
    assert "tagged=java" in seed
    assert "fromdate=" in seed
    # EPOCH 秒：纯数字
    import re
    m = re.search(r"fromdate=(\d+)", seed)
    assert m and len(m.group(1)) == 10, "EPOCH 应为 10 位 Unix 秒"


def test_seed_without_tag_drops_tagged():
    desc = "stackoverflow 最近的新问题"
    seed = seed_url_for(desc)
    assert "tagged=" not in seed, "无标签时应移除 tagged 参数"


def test_match():
    assert match_stackoverflow("https://api.stackexchange.com/2.3/questions?tagged=python")
    assert match_stackoverflow("https://stackoverflow.com/questions")


def test_parse_sorted_by_score():
    rows = parse_stackoverflow(SO_JSON, "https://api.stackexchange.com/2.3/questions")
    assert len(rows) == 2
    assert rows[0]["title"].startswith("How to scrape")  # score 5 在前
    assert rows[0]["tags"] == "python,scraping"
    assert rows[1]["score"] == -3


def test_entry_candidates_root_fallback(monkeypatch):
    """死路径 → 自动回退域名根（不依赖 LLM 猜）。"""
    import universal_scraper.precise_auto as pa
    calls = []

    def fake_probe(url, log=None):
        calls.append(url)
        if "open.163.com/course" in url:
            return {"http_status": 404, "url": url, "detect": {"textLen": 100, "textHead": "404"}}
        if url.rstrip("/") == "https://open.163.com":
            return {"http_status": 200, "url": url, "detect": {"textLen": 4000, "textHead": "网易公开课"}}
        return {"http_status": 404, "url": url, "detect": {"textLen": 0}}

    monkeypatch.setattr(pa, "_probe_advanced", fake_probe)
    r = pa._entry_candidates("抓取网易公开课", "https://open.163.com/course/", log=lambda m: None)
    assert r.get("http_status") == 200
    assert r.get("url", "").rstrip("/") == "https://open.163.com"
    assert any("open.163.com/" in c for c in calls), "应尝试域名根"


def test_html_engine_tactic_is_not_run_registered(monkeypatch):
    """回归：tactic:html_engine 不是 run 型精配，绝不能注册成 run_site（曾报未支持的战术）。"""
    import universal_scraper.sites as sites
    host = "tactic-guard-test.invalid"
    name = "auto_precise_" + host.replace(".", "_").replace(":", "_")
    meta = {"host": host, "kind": "tactic:html_engine", "name": name,
            "tactic": "html_engine", "params": {}, "entry": "https://x/"}
    sites._register_tactic_precise(meta)
    assert name not in sites.SITES


def test_generate_precise_html_engine_failure_cleans_config(monkeypatch, tmp_path):
    """防毒配置：试跑失败时不得留下 tactic:html_engine 文件。"""
    import universal_scraper.precise_auto as pa
    host = "clean-test.invalid"
    url = f"https://{host}/list"
    monkeypatch.setattr(pa, "_entry_candidates", lambda *a, **k: {
        "http_status": 200, "url": url, "detect": {"textLen": 800, "textHead": "列表"}})
    import universal_scraper.tactics as tactics
    monkeypatch.setattr(tactics, "decide_tactic", lambda *a, **k: {
        "tactic": "html_engine", "params": {"entry": url}, "reason": "unit test"})
    def boom(*a, **k):
        raise RuntimeError("unit test 试跑失败")
    monkeypatch.setattr(pa, "_tactic_engine_probe", boom)
    out = pa.generate_precise("抓取测试列表", url, limit=3, log=lambda m: None)
    assert not out.get("ok")
    cfg = pa.CONFIG_DIR / "auto_precise_clean-test.invalid.json"
    assert not cfg.exists(), "试跑失败后不应留存可误注册的战术配置"


def test_run_precise_job_success_has_total(monkeypatch):
    """一键精配成功后，job.result 必须带 total，WebUI 才不会显示「0 条」。"""
    import universal_scraper.webui as webui
    monkeypatch.setattr(webui, "_persist_jobs", lambda: None)
    monkeypatch.setattr(webui, "_export_rows", lambda rows, base: {})
    import universal_scraper.precise_auto as pa
    monkeypatch.setattr(pa, "generate_precise", lambda *a, **k: {
        "ok": True, "kind": "tactic:html_engine", "detail": "engine 试跑 2 条",
        "rows": [{"标题": "a"}, {"标题": "b"}], "files": {}})
    job = {"id": "precise-test", "kind": "precise", "status": "running",
           "result": None, "summary": None, "messages": [], "task_dir": ""}
    webui.run_precise_job(job, "抓测试", "https://example.com/", {})
    assert job["status"] == "done"
    assert job["result"]["total"] == 2
    assert "solution" not in job
