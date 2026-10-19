# -*- coding: utf-8 -*-
"""自动精配助手 _try_auto_precise：mock generate_precise，离线确定性测试。"""
import universal_scraper.auto as auto
import universal_scraper.precise_auto as pa

CFG = {"start_urls": ["https://example.com/list"], "source": {"type": "http"}}


def _mock_ok(**kw):
    def f(description, url, config, limit, log):
        return {"ok": True, "host": "example.com", "name": "auto_precise_example_com",
                "rows": [{"title": "a"}, {"title": "b"}, {"title": "c"}],
                "files": {"json": "outputs/x.json"}, **kw}
    return f


def test_auto_precise_success(monkeypatch):
    # 质量闸门（意图/关键字段/数量）走真实 LLM 会拖慢测试 → mock 掉闸门检查
    monkeypatch.setattr(auto, "_intent_check", lambda *a, **k: "")
    monkeypatch.setattr(auto, "_missing_key_field", lambda *a, **k: "")
    logs = []
    monkeypatch.setattr(pa, "generate_precise", _mock_ok())
    out = auto._try_auto_precise("抓取列表", CFG, 10, logs.append)
    assert out is not None and out.get("done")
    assert out["result"]["total"] == 3
    assert "自动精配" in out["summary"]
    assert any("自动生成精配" in m for m in logs)


def test_auto_precise_failure_returns_none(monkeypatch):
    monkeypatch.setattr(pa, "generate_precise", lambda *a, **k: {"ok": False, "error": "探测失败"})
    out = auto._try_auto_precise("抓取列表", CFG, 10, lambda m: None)
    assert out is None


def test_auto_precise_exception_returns_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("LLM 挂了")
    monkeypatch.setattr(pa, "generate_precise", boom)
    out = auto._try_auto_precise("抓取列表", CFG, 10, lambda m: None)
    assert out is None


def test_auto_precise_volume_gate_rejects(monkeypatch):
    """描述要"前 20 条"但自动精配只出 3 条 → 不采纳（质量闸门）。"""
    monkeypatch.setattr(auto, "_intent_check", lambda *a, **k: "")
    monkeypatch.setattr(auto, "_missing_key_field", lambda *a, **k: "")
    logs = []
    monkeypatch.setattr(pa, "generate_precise", _mock_ok())
    out = auto._try_auto_precise("抓取列表前 20 条", CFG, 10, logs.append)
    assert out is None
    assert any("数量不足" in m for m in logs)


def test_auto_precise_no_url_returns_none():
    out = auto._try_auto_precise("x", {"start_urls": []}, 10, lambda m: None)
    assert out is None


def test_run_with_config_initializes_learned():
    """回归：run_with_config 曾未初始化 _learned → 确认计划命中精配时 NameError 崩溃。"""
    import ast
    from pathlib import Path
    src = (Path(auto.__file__).parent / "auto.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    rwc = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "run_with_config")
    body_src = src.splitlines()[rwc.lineno-1: rwc.end_lineno]
    # 顶层必须有 `_learned = None`（而不是只在 if 分支里）
    has_init = any(line.strip() == "_learned = None" for line in body_src)
    assert has_init, "run_with_config 缺少 _learned 初始化（会导致确认流程崩溃）"
    # run_with_config 没有 cookie/proxy 形参 → hook 不得引用它们（曾 NameError）
    assert "cookie=cookie" not in "\n".join(body_src), "run_with_config hook 引用了不存在的 cookie 形参"
    assert "proxy=proxy" not in "\n".join(body_src), "run_with_config hook 引用了不存在的 proxy 形参"
