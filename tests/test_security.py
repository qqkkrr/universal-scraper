# -*- coding: utf-8 -*-
"""安全回归：/reports/ 路径穿越封堵、/api/report 文件名净化（真实 HTTP 服务，仅 localhost）。"""
import json, threading, urllib.request, urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
import pytest

import universal_scraper.webui as webui

ROOT = Path(webui.ROOT)
REPORTS = ROOT / "outputs" / "reports"

@pytest.fixture(scope="module")
def srv():
    # 测试服务器与真实服务共用模块级状态；通过临时文件/空 JOBS 隔离，
    # 避免任何 HTTP 测试污染用户 jobs_history.json、settings.json 或 schedules.json。
    import tempfile
    _tmp = tempfile.TemporaryDirectory()
    _orig = (webui.HISTORY_FILE, webui.JOBS, webui.SETTINGS_FILE, webui.SCHEDULES_FILE)
    webui.HISTORY_FILE = Path(_tmp.name) / "jobs_history.json"
    webui.JOBS = {}
    webui.SETTINGS_FILE = Path(_tmp.name) / "settings.json"
    webui.SCHEDULES_FILE = Path(_tmp.name) / "schedules.json"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        webui.HISTORY_FILE, webui.JOBS, webui.SETTINGS_FILE, webui.SCHEDULES_FILE = _orig
        _tmp.cleanup()

def _get(srv, path):
    try:
        with urllib.request.urlopen(srv + path, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

def _post(srv, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(srv + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")

def test_reports_path_traversal_blocked(srv):
    code, body = _get(srv, "/reports/../../universal_scraper/webui.py")
    assert code == 403, f"应 403 拦截越权读，实际 {code}: {body[:60]}"

def test_reports_encoded_traversal_blocked(srv):
    code, body = _get(srv, "/reports/%2e%2e/../../universal_scraper/webui.py")
    assert code in (403, 404), f"越权读应被拦，实际 {code}"

def test_reports_missing_404(srv):
    code, body = _get(srv, "/reports/no_such_report.html")
    assert code == 404

def test_report_name_sanitized(srv):
    code, d = _post(srv, "/api/report", {"name": "../../pwn_test", "content": "a,b\n1,2\n"})
    assert code == 200 and d.get("ok"), d
    # 不得写出 reports 目录；且文件名被净化（无 ..）
    escaped = ROOT / "pwn_test.csv"
    assert not escaped.exists(), "路径穿越写入未封堵！"
    assert (REPORTS / "pwn_test.csv").exists(), "净化后的文件应落在 reports 目录内"
    (REPORTS / "pwn_test.csv").unlink(missing_ok=True)
    (REPORTS / "pwn_test.html").unlink(missing_ok=True)

def test_report_empty_content_rejected(srv):
    code, d = _post(srv, "/api/report", {"name": "x", "content": ""})
    assert not d.get("ok")


def test_auto_start_config_without_taskdir_safe(srv):
    """config 给了但 task_dir 缺失：必须安全推导到 tasks/ 内，不得写到服务 CWD/项目根。"""
    import hashlib, time, shutil
    desc = "测试安全推导 task_dir 的位置"
    h = hashlib.md5(desc.encode()).hexdigest()[:10]
    name = f"auto_{h}"
    cfg = {"name": name, "start_urls": ["http://127.0.0.1:9/x"], "source": {"type": "http"},
           "rules": [{"match": "contains", "pattern": "x", "parser": "default"}],
           "parsers": {"default": {"type": "html", "row_css": ".a", "fields": {"t": {"css": ".t::text"}}}}}
    code, d = _post(srv, "/api/auto/start", {"description": desc, "config": cfg, "name": name})
    assert code == 200 and d.get("job"), d
    jid = d["job"]
    td = ROOT / "tasks" / name
    ok = False
    for _ in range(30):
        if (td / "config.json").exists():
            ok = True
            break
        time.sleep(0.5)
    assert ok, "config 应写入 tasks/<name>/config.json"
    # 原 bug：无 task_dir 时 run_with_config 写到 CWD（项目根）——必须不存在
    assert not (ROOT / "config.json").exists(), "config 不得写到项目根（CWD）"
    # 停掉后台任务并等待线程彻底结束；避免清理任务目录时后台线程仍写 .running.lock
    try:
        _post(srv, "/api/job/stop", {"job": jid})
    except Exception:
        pass
    for _ in range(50):
        try:
            _, state = _get(srv, "/api/job?job=" + jid)
            state = json.loads(state)
            if state.get("status") != "running":
                break
        except Exception:
            break
        time.sleep(0.2)
    shutil.rmtree(td, ignore_errors=True)


def test_csrf_foreign_origin_rejected(srv):
    """跨站 POST 必须被拒绝（CSRF 防护）：恶意网页不能改 AI 配置/触发任务。"""
    import urllib.request
    req = urllib.request.Request(srv + "/api/settings",
                                 data=b'{"model":"evil"}',
                                 headers={"Content-Type": "application/json",
                                          "Origin": "https://evil.example.com"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            assert False, "跨站 POST 应被拒绝"
    except urllib.error.HTTPError as e:
        assert e.code == 403, f"应 403，实际 {e.code}"


def test_same_origin_post_allowed(srv):
    """同源 POST 放行（WebUI 正常操作）。
    注意：测试服务器与真实服务共用 configs/settings.json——必须快照并在结束后恢复，
    否则会污染线上 AI 配置。"""
    import shutil
    st_file = ROOT / "configs" / "settings.json"
    backup = st_file.read_bytes() if st_file.exists() else None
    try:
        code, d = _post(srv, "/api/settings", {"model": "test-model-xyz"})
        assert code == 200 and d.get("ok"), d
        # 确实生效（设置已写入）
        code2, d2 = _get(srv, "/api/settings")
        assert code2 == 200 and json.loads(d2).get("model") == "test-model-xyz", d2
    finally:
        if backup is not None:
            st_file.write_bytes(backup)
        else:
            st_file.unlink(missing_ok=True)


def test_none_header_filtered():
    """curl_cffi 对 None header 抛 TypeError → _sanitize_headers 必须过滤 None 值。"""
    from universal_scraper.core import _sanitize_headers
    h = _sanitize_headers({"Accept": "*/*", "X-None": None, "X-Caller": None, "X-Ok": "1", "UA": "t"})
    assert h.get("X-Ok") == "1" and h.get("UA") == "t"
    assert "X-None" not in h and "X-Caller" not in h
    assert all(v is not None for v in h.values()), "仍有 None header"
    assert _sanitize_headers(None) == {}
