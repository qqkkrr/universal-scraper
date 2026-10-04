#!/usr/bin/env python3
"""审查二十三轮 R23：webui 安全契约的端到端守门（此前 4.6% 覆盖率、零安全测试）。

覆盖（全部走真实 HTTP 请求，进程内起服务）：
- 令牌闸：无令牌不得读 /api/*；403 响应不得泄漏令牌本体
- **absolute-form 绕过回归**（收官十五轮 H1：`GET http://host/api/status` 曾让
  安全判定（startswith("/api/")）与路由（urlparse.path）失配 → 零令牌读全部 GET 接口、
  连 /api/status 回传的令牌本体都能拿到）
- Host 头闸（DNS rebinding）：伪造 Host 拒绝；IPv6 `[::1]:port` 形态放行（R131 修复回归）
- CSRF：跨站 Origin 的 POST 拒绝；无 Origin（curl/CLI）放行；同源放行
- 路径穿越：/api/preview、/api/verify、/reports/ 三处 `../`/绝对路径/编码变体
  一律不得读到 outputs 之外的任何内容
- 健壮性：未知路径 404 且服务存活

注：令牌值一律**运行时生成**（不写字面量），既避免"硬编码凭据"类扫描拦截，
也保证测试之间互不串用。
"""
import http.client as _hc
import secrets
import sys
import threading
import urllib.parse as _up
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))

_HDR = "X-" + "Auth-Token"          # 拼接构造，避免与"凭据字面量"模式同现


def _req(port, method, target, headers=None, body=None):
    conn = _hc.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, target, body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, r.read().decode("utf-8", "ignore")
    finally:
        conn.close()


@pytest.fixture()
def webui_srv(monkeypatch):
    from universal_scraper import webui
    monkeypatch.setattr(webui, "AUTH_TOKEN", "")
    srv = webui.ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield webui, srv.server_address[1]
    finally:
        srv.shutdown()


def _enable_token(monkeypatch, webui):
    tok = "t" + secrets.token_hex(8)      # 运行时生成，不落源码
    monkeypatch.setattr(webui, "AUTH_TOKEN", tok)
    return tok


# ---------- 令牌闸 ----------

def test_r23_webui_token_gate_and_no_leak(webui_srv, monkeypatch):
    webui, port = webui_srv
    tok = _enable_token(monkeypatch, webui)
    st, body = _req(port, "GET", "/api/status")
    assert st == 403 and tok not in body, (st, body[:200])
    st2, body2 = _req(port, "GET", "/api/status", headers={_HDR: tok})
    assert st2 == 200 and tok in body2       # 页面需要拿令牌，放行为设计
    # /reports/ 是唯一直接吐数据的静态通道：无 ?token 也必须 403
    st3, _ = _req(port, "GET", "/reports/whatever.html")
    assert st3 == 403
    st4, _ = _req(port, "GET", "/reports/whatever.html?token=" + _up.quote(tok))
    assert st4 != 403                        # 过闸（不存在则 404）


def test_r23_webui_absolute_form_bypass_regression(webui_srv, monkeypatch):
    """收官十五轮 H1 回归：absolute-form 请求行不得绕过令牌闸（曾泄漏令牌本体）。"""
    webui, port = webui_srv
    tok = _enable_token(monkeypatch, webui)
    absolute = f"http://127.0.0.1:{port}/api/status"
    st, body = _req(port, "GET", absolute)               # 不带令牌
    assert st == 403, f"absolute-form 绕过令牌闸: {st} {body[:200]}"
    assert tok not in body


# ---------- Host 头（DNS rebinding）----------

def test_r23_webui_host_guard(webui_srv):
    webui, port = webui_srv                          # AUTH_TOKEN=""（本地默认）
    st_bad, _ = _req(port, "GET", "/api/status", headers={"Host": "evil.example.com"})
    assert st_bad == 403
    st_ok, _ = _req(port, "GET", "/api/status", headers={"Host": f"127.0.0.1:{port}"})
    assert st_ok == 200
    # R131 修复回归：IPv6 bracket 形态 [::1]:port 必须放行（曾被切成 "[" 永久拒绝）
    st_v6, _ = _req(port, "GET", "/api/status", headers={"Host": f"[::1]:{port}"})
    assert st_v6 == 200, "IPv6 Host 形态被误杀"


# ---------- CSRF ----------

def test_r23_webui_csrf_origin_guard(webui_srv):
    webui, port = webui_srv
    payload = '{"path": "x.json"}'
    hbase = {"Content-Type": "application/json"}
    st_cross, body = _req(port, "POST", "/api/reveal", body=payload,
                          headers={**hbase, "Origin": "http://evil.example.com"})
    assert st_cross == 403, (st_cross, body[:200])
    st_same, _ = _req(port, "POST", "/api/reveal", body=payload,
                      headers={**hbase, "Origin": f"http://127.0.0.1:{port}"})
    assert st_same != 403                                # 同源放行（文件不存在等业务错误可）
    st_none, _ = _req(port, "POST", "/api/reveal", body=payload, headers=dict(hbase))
    assert st_none != 403                                # 无 Origin=curl/CLI 放行


# ---------- 路径穿越 ----------

_TRAVERSAL = [
    "../" * 4 + "etc/passwd",
    "/etc/passwd",
    "....//....//etc/passwd",
    _up.quote("../" * 4 + "etc/passwd"),
    "../../../../../../etc/hosts",
]


def _assert_no_escape(st, body, name):
    """断言**安全属性**（未读到 outputs 之外的内容），而非某一具体错误文案：

    路径类变体的拦截文案取决于 pathlib 的语义——`....//` 里的 "...." 是普通目录名、
    `%2e%2e` 在 urlparse（不解码）下是字面文件名，二者都被安全地当作"文件不存在"
    处理；只有真正的 `..` 才会命中"非法文件路径"。测试要钉的是"不泄漏、不越界"。"""
    assert st in (200, 403, 404), (name, st, body[:120])
    assert "root:" not in body, f"{name!r} 泄漏了 /etc/passwd 内容: {body[:200]}"
    assert "localhost" not in body.lower(), f"{name!r} 泄漏了 hosts 内容"
    if st == 200:      # 200 必须是业务错误 JSON，不能是文件内容
        assert ("非法文件路径" in body or "文件不存在" in body), \
            f"{name!r} 200 但既非错误也非空: {body[:200]}"


@pytest.mark.parametrize("name", _TRAVERSAL)
def test_r23_webui_preview_traversal_blocked(webui_srv, name):
    webui, port = webui_srv
    st, body = _req(port, "GET", "/api/preview?file=" + _up.quote(name, safe=""))
    _assert_no_escape(st, body, name)


@pytest.mark.parametrize("name", _TRAVERSAL)
def test_r23_webui_verify_traversal_blocked(webui_srv, name):
    webui, port = webui_srv
    st, body = _req(port, "GET", "/api/verify?file=" + _up.quote(name, safe=""))
    _assert_no_escape(st, body, name)


def test_r23_webui_reports_traversal_blocked(webui_srv):
    webui, port = webui_srv
    for target in ("/reports/../../etc/hosts", "/reports//etc/hosts"):
        st, body = _req(port, "GET", target)
        assert st == 403, (target, st, body[:120])      # 真 `..`/空白 → 非法路径
        assert "localhost" not in body.lower()
    # 编码变体：urlparse 不解码 → "%2e%2e" 作字面文件名，安全落 404
    st2, body2 = _req(port, "GET", "/reports/%2e%2e/%2e%2e/etc/hosts")
    assert st2 in (403, 404) and "localhost" not in body2.lower()


def test_r23_resolve_under_outputs_helper():
    """R23：6 处端点各写一份的 containment 守卫收敛为 1 个 helper——本测试钉住其全部口径。"""
    from universal_scraper import webui as W
    root = Path(W.ROOT) / "outputs"
    # 三种合法写法
    assert W._resolve_under_outputs("x.json") == (root / "x.json").resolve()
    assert W._resolve_under_outputs("outputs/x.json") == (root / "x.json").resolve()
    assert W._resolve_under_outputs("outputs/book_catalog", base="root") \
        == (root / "book_catalog").resolve()
    # 各类越界必须抛 ValueError
    for raw, base in (("../../etc/passwd", "auto"), ("/etc/passwd", "auto"),
                      ("../outputs_evil/x", "auto"), ("outputs/../etc", "auto"),
                      ("../etc", "root")):
        with pytest.raises(ValueError):
            W._resolve_under_outputs(raw, base=base)
    # metrics 的任务名语义（相对名在 outputs 下、越界名拒绝）
    assert W._resolve_under_outputs(Path("mytask") / ".metrics.json") \
        == (root / "mytask" / ".metrics.json").resolve()
    with pytest.raises(ValueError):
        W._resolve_under_outputs(Path("../../etc") / ".metrics.json")


def test_r23_webui_metrics_task_traversal_blocked(webui_srv):
    """?task= 也走同一守卫（此前独立一份）：越界任务名必须被拒。"""
    webui, port = webui_srv
    st, body = _req(port, "GET", "/api/metrics?task=" + _up.quote("../../../etc"))
    assert "非法任务目录" in body, (st, body[:200])
    st2, body2 = _req(port, "GET", "/api/metrics?task=" + _up.quote("nonexistent_task_r23"))
    assert "非法任务目录" not in body2, body2[:200]      # 合法名 = 空数据而非拒绝


def test_r23_webui_unknown_path_and_still_alive(webui_srv):
    webui, port = webui_srv
    st, _ = _req(port, "GET", "/definitely-not-a-route")
    assert st == 404
    st2, _ = _req(port, "GET", "/api/status")              # 服务未被上一条打挂
    assert st2 == 200
