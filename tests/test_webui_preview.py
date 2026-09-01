# -*- coding: utf-8 -*-
"""/api/preview 与 /api/reveal 的行为与安全回归（真实 HTTP 服务，仅 localhost）。"""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import universal_scraper.webui as webui

ROOT = Path(webui.ROOT)
OUT = ROOT / "outputs"


@pytest.fixture(scope="module")
def srv():
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


def _get_json(srv, path):
    try:
        with urllib.request.urlopen(srv + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _post_json(srv, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(srv + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


@pytest.fixture(scope="module")
def sample_files():
    OUT.mkdir(parents=True, exist_ok=True)
    j = OUT / "_preview_test_rows.json"
    j.write_text(json.dumps([
        {"title": "书A", "_url": "https://x/1", "price": "12.5"},
        {"title": "书B", "_url": "https://x/2", "price": "20"},
        {"title": "书C", "_url": "https://x/3", "price": None},
    ], ensure_ascii=False), encoding="utf-8")
    wrapped = OUT / "_preview_test_books.json"
    wrapped.write_text(json.dumps({
        "total": 2, "rows": [{"isbn": "9787508648330", "book_title": "T"}],
        "coverage": {}, "diagnostics": []}, ensure_ascii=False), encoding="utf-8")
    empty = OUT / "_preview_test_empty.json"
    empty.write_text("[]", encoding="utf-8")
    c = OUT / "_preview_test.csv"
    c.write_text("名称,数量\n甲,1\n乙,2\n", encoding="utf-8-sig")
    yield
    for f in (j, wrapped, empty, c):
        f.unlink(missing_ok=True)


def test_preview_json_rows_and_internal_cols_hidden(srv, sample_files):
    code, d = _get_json(srv, "/api/preview?file=" + urllib.parse.quote("_preview_test_rows.json"))
    assert code == 200 and d["ok"]
    assert d["total"] == 3
    assert d["columns"] == ["title", "price"], "内部字段 _url 必须默认隐藏"
    assert d["rows"][0]["price"] == "12.5"
    assert d["rows"][2]["price"] == ""  # None → 空串而非 "None"


def test_preview_wrapped_books_json(srv, sample_files):
    code, d = _get_json(srv, "/api/preview?file=" + urllib.parse.quote("_preview_test_books.json"))
    assert code == 200 and d["ok"]
    assert d["total"] == 2 and d["rows"][0]["isbn"] == "9787508648330"


def test_preview_csv(srv, sample_files):
    code, d = _get_json(srv, "/api/preview?file=" + urllib.parse.quote("_preview_test.csv"))
    assert code == 200 and d["ok"]
    assert d["columns"] == ["名称", "数量"] and d["total"] == 2


def test_preview_empty_file_reports_zero_not_error(srv, sample_files):
    code, d = _get_json(srv, "/api/preview?file=" + urllib.parse.quote("_preview_test_empty.json"))
    assert code == 200 and d["ok"] and d["empty"] is True
    assert d["total"] == 0 and d["rows"] == []


def test_preview_rows_limit_clamped(srv, sample_files):
    code, d = _get_json(srv, "/api/preview?file=_preview_test_rows.json&rows=99999")
    assert code == 200 and len(d["rows"]) <= 3
    code, d = _get_json(srv, "/api/preview?file=_preview_test_rows.json&rows=abc")
    assert code == 200, "非法 rows 参数应回落默认而不是 500"


def test_preview_path_traversal_blocked(srv):
    code, d = _get_json(srv, "/api/preview?file=" + urllib.parse.quote("../../universal_scraper/webui.py"))
    assert "error" in d, "越权读必须被拒绝"


def test_preview_missing_and_unsupported(srv):
    code, d = _get_json(srv, "/api/preview?file=no_such_preview_file.json")
    assert "error" in d
    OUT.mkdir(parents=True, exist_ok=True)
    x = OUT / "_preview_test.txt"
    x.write_text("x", encoding="utf-8")
    try:
        code, d = _get_json(srv, "/api/preview?file=_preview_test.txt")
        assert "error" in d, "非 json/csv 必须明确报不支持"
    finally:
        x.unlink(missing_ok=True)


def test_reveal_opens_outputs_file(srv, sample_files, monkeypatch):
    calls = []
    import subprocess as _sp
    # Handler 里局部 import subprocess 拿到的是同一个真模块：monkeypatch 模块属性即可，
    # 用 pytest 自带 fixture 保证测试结束自动还原，不污染其他线程
    monkeypatch.setattr(_sp, "run", lambda cmd, check=False: calls.append(cmd))
    monkeypatch.setattr(webui, "AUTH_TOKEN", "")
    code, d = _post_json(srv, "/api/reveal", {"path": "outputs/_preview_test_rows.json"})
    assert code == 200 and d["ok"], d
    assert calls and calls[0][:2] == ["open", "-R"], f"macOS 应在访达中定位文件: {calls}"
    assert str(OUT / "_preview_test_rows.json") in calls[0][2]


def test_reveal_rejects_traversal_and_missing(srv):
    code, d = _post_json(srv, "/api/reveal", {"path": "../../universal_scraper/webui.py"})
    assert not d.get("ok"), "越权路径必须拒绝"
    code, d = _post_json(srv, "/api/reveal", {"path": "outputs/no_such_file.json"})
    assert not d.get("ok")


def test_reveal_disabled_in_share_mode(srv, sample_files, monkeypatch):
    import subprocess as _sp
    calls = []
    monkeypatch.setattr(_sp, "run", lambda cmd, check=False: calls.append(cmd))
    monkeypatch.setattr(webui, "AUTH_TOKEN", "sometoken")
    data = json.dumps({"path": "outputs/_preview_test_rows.json"}).encode("utf-8")
    req = urllib.request.Request(srv + "/api/reveal", data=data,
                                 headers={"Content-Type": "application/json",
                                          "X-Auth-Token": "sometoken"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read().decode("utf-8"))
    assert not d.get("ok"), "分享模式必须禁用（会操作服务器电脑的界面）"
    assert not calls


def test_reveal_single_response_no_404_tail(srv, sample_files, monkeypatch):
    """回归：reveal 处理完必须 return，同一连接不能追加第二个 404 响应。"""
    import http.client
    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", lambda cmd, check=False: None)
    monkeypatch.setattr(webui, "AUTH_TOKEN", "")
    url = urllib.parse.urlparse(srv)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    try:
        conn.request("POST", "/api/reveal",
                     body=json.dumps({"path": "outputs/_preview_test_rows.json"}),
                     headers={"Content-Type": "application/json"})
        r1 = conn.getresponse()
        body1 = r1.read().decode("utf-8")
        assert r1.status == 200, body1
        # 同一连接再发一个请求：若 reveal 曾落穿写坏 socket，这里会挂/读出脏数据
        conn.request("GET", "/api/jobs")
        r2 = conn.getresponse()
        body2 = r2.read().decode("utf-8")
        assert r2.status == 200 and body2.startswith("["), f"连接被写坏: {body2[:80]}"
    finally:
        conn.close()


def test_restart_uses_env_host_port(srv, monkeypatch):
    """/api/restart 曾因引用未定义的 host/port 抛 NameError（重启功能全灭）。
    现在：命令从 env 取参数，且 stub 掉 os._exit / Popen 后可离线验证。"""
    import subprocess as _sp
    import universal_scraper.webui as w
    cmds = []

    class _FakePopen:
        def __init__(self, cmd, **kw):
            cmds.append(cmd)

    monkeypatch.setattr(_sp, "Popen", _FakePopen)
    monkeypatch.setattr(w.os, "_exit", lambda code=0: None)  # 防真退出测试进程
    monkeypatch.setenv("US_WEBUI_HOST", "0.0.0.0")
    monkeypatch.setenv("US_WEBUI_PORT", "9999")
    code, d = _post_json(srv, "/api/restart", {})
    assert d.get("ok") is True, d
    assert cmds, "重启命令未发出"
    assert cmds[0][-1] == "9999" and "--host" in cmds[0] and "0.0.0.0" in cmds[0], cmds[0]


def test_restart_keeps_share_flag(srv, monkeypatch):
    """share 模式重启必须带 --share，否则局域网使用者全部断连。"""
    import subprocess as _sp
    import universal_scraper.webui as w
    cmds = []

    class _FakePopen:
        def __init__(self, cmd, **kw):
            cmds.append(cmd)

    monkeypatch.setattr(_sp, "Popen", _FakePopen)
    monkeypatch.setattr(w.os, "_exit", lambda code=0: None)
    monkeypatch.setenv("US_WEBUI_HOST", "0.0.0.0")
    monkeypatch.setenv("US_WEBUI_PORT", "8642")
    monkeypatch.setenv("US_WEBUI_SHARE", "1")
    monkeypatch.setattr(w, "AUTH_TOKEN", "t")
    # share 模式所有 /api/* 需带令牌
    data = json.dumps({}).encode("utf-8")
    req = urllib.request.Request(srv + "/api/restart", data=data,
                                 headers={"Content-Type": "application/json",
                                          "X-Auth-Token": "t"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read().decode("utf-8"))
    assert d.get("ok") is True, d
    assert "--share" in cmds[0] and "--token" in cmds[0], cmds[0]
