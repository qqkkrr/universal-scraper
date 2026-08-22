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
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()

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
    # 停掉后台任务并清理
    try:
        _post(srv, "/api/job/stop", {"job": jid})
    except Exception:
        pass
    shutil.rmtree(td, ignore_errors=True)
