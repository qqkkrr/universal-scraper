# -*- coding: utf-8 -*-
"""auto 任务结果语义：result 必须带 files，前端预览/复制路径/打开文件夹才有数据源。"""
import universal_scraper.webui as webui


def test_run_auto_job_merges_files_into_result(monkeypatch):
    """通用引擎成功时 files 只在顶层——run_auto_job 必须合并进 result（P0 回归）。"""
    import universal_scraper.auto as auto_mod
    captured = {}
    monkeypatch.setattr(webui, "_persist_jobs", lambda: None)
    monkeypatch.setattr(webui, "_job_done",
                        lambda j, result, summary=None, verify=None: captured.update(
                            {"result": result}))
    monkeypatch.setattr(webui, "_job_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(auto_mod, "auto_task",
                        lambda desc, **kw: {"name": "t", "result": {"total": 2, "fetched": 1,
                                                                   "errors": 0},
                                            "summary": "ok", "verify": None,
                                            "files": {"json": "outputs/t.json",
                                                      "csv": "outputs/t.csv"},
                                            "task_dir": "", "log": ""})
    job = {"kind": "auto", "messages": [], "task_dir": ""}
    webui.run_auto_job(job, "抓取示例", None, 2, None)
    r = captured.get("result") or {}
    assert r.get("total") == 2
    assert r.get("files", {}).get("json") == "outputs/t.json", \
        "result.files 缺失会让页面预览/复制路径/打开文件夹全部不可用"


def test_run_auto_job_keeps_result_files_when_engine_sets(monkeypatch):
    """精配等路径 result 自带 files 时不得被覆盖。"""
    import universal_scraper.auto as auto_mod
    captured = {}
    monkeypatch.setattr(webui, "_persist_jobs", lambda: None)
    monkeypatch.setattr(webui, "_job_done",
                        lambda j, result, summary=None, verify=None: captured.update(
                            {"result": result}))
    monkeypatch.setattr(auto_mod, "auto_task",
                        lambda desc, **kw: {"result": {"total": 1,
                                                       "files": {"json": "outputs/own.json"}},
                                            "summary": "ok",
                                            "files": {"json": "outputs/top.json"}})
    job = {"kind": "auto", "messages": [], "task_dir": ""}
    webui.run_auto_job(job, "x", None, 2, None)
    assert captured["result"]["files"]["json"] == "outputs/own.json"
