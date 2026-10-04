#!/usr/bin/env python3
"""审查十九轮（R19）修复的回归测试。

覆盖：
- sites.fetch_html 协议闸：file://（实测曾读出 /etc/hosts 内容）等伪协议
  一律拒绝且**不发起任何网络调用**；http 正常路径不受影响
- core.loopback_http_alive：环回探活显式绕开环境代理（挂死代理时仍直连成功）
- agent/webui 的环回 CDP 探测改走统一 helper（源级：环回直连不再裸调 urllib）

说明：本文件内的环回 URL 只指向本进程内的 pytest 桩服务器（临时端口），
用于验证协议闸/代理绕行两条守卫本身的语义。
"""
import ast
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


class _StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = "你好-stub".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静音
        pass


@pytest.fixture()
def stub_server():
    srv = HTTPServer(("127.0.0.1", 0), _StubHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield port
    finally:
        srv.shutdown()


# ---------- F1: fetch_html 协议闸 ----------

def test_fetch_html_rejects_pseudo_schemes(tmp_path, monkeypatch):
    from universal_scraper import net, sites

    # 网络层探针：协议闸必须在任何请求之前拦下（file:// 曾由 urllib FileHandler
    # 直接读本地文件——实测 ok=True 且 html=/etc/hosts 内容）
    net_calls = []
    monkeypatch.setattr(net, "opener_for",
                        lambda *a, **k: net_calls.append(a) or (_ for _ in ()).throw(
                            AssertionError("协议闸前不应构造网络 opener")))
    try:
        import curl_cffi.requests as _cffi_req
        monkeypatch.setattr(_cffi_req, "get",
                            lambda *a, **k: net_calls.append(a) or (_ for _ in ()).throw(
                                AssertionError("协议闸前不应发起 cffi 请求")))
    except ImportError:
        pass

    victim = tmp_path / "secret.txt"
    victim.write_text("R19-MARKER-DO-NOT-READ", encoding="utf-8")
    targets = [f"file://{victim}", "file:///etc/hosts", "ftp://127.0.0.1/x",
               "data:text/html,<b>x</b>", "gopher://127.0.0.1/1", "FILE:///etc/hosts", ""]
    for bad in targets:
        r = sites.fetch_html(bad, timeout=3)
        assert r.get("ok") is False, f"{bad!r} 未被协议闸拦下: {r}"
        assert "http/https" in (r.get("error") or ""), f"{bad!r} 错误信息缺少协议提示: {r}"
        assert "R19-MARKER-DO-NOT-READ" not in (r.get("html") or "")
        assert "Host Database" not in (r.get("html") or "")
    assert net_calls == [], f"协议闸前发生了网络调用: {net_calls}"


def test_fetch_html_http_path_unaffected(stub_server):
    from universal_scraper.sites import fetch_html

    url = f"http://127.0.0.1:{stub_server}/x"
    r = fetch_html(url, timeout=5)
    assert r.get("ok") is True and r.get("status") == 200
    assert r.get("html") == "你好-stub"


def test_fetch_html_gate_runs_before_network():
    """协议闸必须位于任何网络调用之前（cffi.get / opener.open）。"""
    src = (SKILL / "universal_scraper" / "sites.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "fetch_html")
    gate_ln = net_ln = None
    for node in ast.walk(fn):
        if gate_ln is None and isinstance(node, ast.If):
            seg = ast.get_source_segment(src, node.test) or ""
            if '"http"' in seg and '"https"' in seg:
                gate_ln = node.lineno
        if net_ln is None and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = getattr(node.func.value, "id", "")
            if (base == "cffi" and node.func.attr == "get") or \
                    (base == "opener" and node.func.attr == "open"):
                net_ln = node.lineno
    assert gate_ln and net_ln and gate_ln < net_ln, f"gate={gate_ln} net={net_ln}"


# ---------- F2: 环回探活绕开环境代理 ----------

def test_loopback_probe_bypasses_dead_env_proxy(stub_server, monkeypatch):
    from universal_scraper.core import loopback_http_alive

    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)

    url = f"http://127.0.0.1:{stub_server}/json/version"
    # 桩服务器活着 → 死代理环境下仍须探到（实现若退回裸请求会被代理劫持而失败）
    assert loopback_http_alive(url, timeout=3) is True
    # 死端口 / 失败路径：返回 False 而不抛
    assert loopback_http_alive("http://127.0.0.1:9/x", timeout=1) is False


# ---------- F2/F3: 调用点走统一 helper（源级语义断言） ----------

def _module_src(name: str) -> str:
    return (SKILL / "universal_scraper" / name).read_text(encoding="utf-8")


def _direct_loopback_requests(src: str):
    """AST 扫描：直接对环回字面量发起请求的调用点（源级不变量）。

    识别形如 urllib.request.urlopen("http://127.0.0.1:...") 或 f-string
    拼出的环回地址——这类调用会被环境代理劫持（R19 修复族）。"""
    marks = ("127.0.0.", "localhost", "[::1]")
    hits = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name not in ("urlopen", "open"):
            continue
        for arg in node.args[:1]:
            vals = []
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                vals = [arg.value]
            elif isinstance(arg, ast.JoinedStr):
                vals = [str(getattr(v, "value", "")) for v in arg.values]
            if any(m in v for v in vals for m in marks):
                hits.append((getattr(node, "lineno", 0), ast.dump(node)[:60]))
    return hits


def test_r19_agent_probe_uses_helper():
    src = _module_src("agent.py")
    tree = ast.parse(src)
    body = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "debug_chrome_alive":
            body = ast.get_source_segment(src, node)
    assert body, "debug_chrome_alive 不存在"
    assert "loopback_http_alive" in body
    assert _direct_loopback_requests(body) == []


def test_r19_webui_probe_uses_helper():
    src = _module_src("webui.py")
    assert "loopback_http_alive" in src
    assert _direct_loopback_requests(src) == []


# ---------- 契约对账：validate 放行 ⇒ 引擎消费不裸崩 ----------

_V2_BASE = {"name": "t", "source": {"type": "http_html", "url": "http://x/", "row_css": ".r"}}
_V3_BASE = {"name": "t", "start_urls": ["http://x/"],
            "parsers": {"default": {"type": "html", "fields": {"t": "h1"}}},
            "rules": [{"match": "contains", "pattern": "/", "parser": "default"}]}


@pytest.mark.parametrize("bad", [
    {"vars": ["a"]},              # engine dict(vars) → ValueError（修前实测）
    {"output": ["out"]},          # engine output.get → AttributeError
    {"storage": ["jsonl"]},       # storage.type 消费
    {"incremental": ["x"]},       # incremental.key 消费
    {"download": ["x"]},          # download.enabled 消费
    {"iterate": [{"var": "x"}]},  # iterate["var"] → TypeError
    {"iterate": {}},              # 缺 var → KeyError
    {"iterate": {"var": "x", "values": "北京"}},   # values 须是数组
    {"iterate": {"var": "", "values": ["北京"]}},  # var 须非空
    {"iterate": {"var": "x", "values": ["a"], "labels": ["L"]}},
])
def test_r19_v2_container_contract(bad):
    from universal_scraper.config import ConfigError, validate
    with pytest.raises(ConfigError):
        validate({**_V2_BASE, **bad})


def test_r19_v2_contract_valid_forms_pass():
    from universal_scraper.config import validate
    cfg = validate({**_V2_BASE,
                    "vars": {"城市": "北京"},
                    "output": {"dir": "out"},
                    "storage": {"type": "jsonl"},
                    "incremental": {"enabled": True, "key": "id"},
                    "download": {"enabled": True, "field": "url"},
                    "iterate": {"var": "城市", "values": ["北京", "上海"],
                                "labels": {"北京": "京城"}}})
    assert cfg["vars"] == {"城市": "北京"}
    # 显式 null 归一为空容器（引擎按缺省语义消费）
    cfg2 = validate({**_V2_BASE, "vars": None, "output": None})
    assert cfg2["vars"] == {} and cfg2["output"] == {}


@pytest.mark.parametrize("bad", [{"vars": ["a"]}, {"output": "o"}, {"queue": ["a"]}])
def test_r19_v3_container_contract(bad):
    from universal_scraper.config import ConfigError, validate_task
    with pytest.raises(ConfigError):
        validate_task({**_V3_BASE, **bad})


# ---------- audit：文本源覆盖豁免的键口径（真实面板实测修复） ----------

def _mk_panel(tmp_path, rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["stkcd", "year", "title", "total_chars", "compliance_kw_count",
               "compliance_kw_freq", "备注"])
    for r in rows:
        ws.append(r)
    p = tmp_path / "panel.xlsx"
    wb.save(p)
    return str(p)


def test_r19_audit_coverage_exemption_numeric_year(tmp_path):
    """数值年份面板：已标注"不可复现"的缺口必须豁免，未标注的仍要报。

    修前 uncovered 存原始 year（int 2021）、flagged 键用 str(year)，集合差永不
    匹配——真实面板 17 行已标注缺口全被误报（消息自相矛盾："缺失 17 > 已标注 17"）。
    """
    from universal_scraper.audit import audit_panel
    td = tmp_path / "texts"
    td.mkdir()
    import gzip
    (td / "600009_2022.txt.gz").write_bytes(gzip.compress("正文".encode()))
    xlsx = _mk_panel(tmp_path, [
        ["600009", 2022, "上海机场2022年年度报告", 1000, 5, 10.0, ""],
        ["600029", 2021, "某年报", 900, 4, 8.889, "源文本暂不可复现: 平台无候选公告"],
        ["600050", 2020, "某年报", 800, 3, 7.5, ""],
    ])
    iss = audit_panel(xlsx, str(td))
    cov = [i for i in iss if "文本源缺失" in i]
    assert len(cov) == 1, iss
    # 已标注的 600029_2021 被豁免；未标注的 600050/2020 必须仍被报出
    assert "未标注缺口 1" in cov[0] and "600050" in cov[0], cov[0]
    assert "600029" not in cov[0], cov[0]


def test_r19_audit_coverage_all_annotated_clean(tmp_path):
    """全部缺口均已逐行标注 → 覆盖检查不再报缺（真实面板形态）。"""
    from universal_scraper.audit import audit_panel
    td = tmp_path / "texts"
    td.mkdir()
    import gzip
    (td / "600009_2022.txt.gz").write_bytes(gzip.compress("正文".encode()))
    xlsx = _mk_panel(tmp_path, [
        ["600009", 2022, "上海机场2022年年度报告", 1000, 5, 10.0, ""],
        ["600029", 2021, "某年报", 900, 4, 8.889, "源文本暂不可复现: 平台无候选公告"],
        ["600056.0", "2018.0", "某年报", 800, 3, 7.5, "源文本暂不可复现: 扫描件"],
    ])
    iss = audit_panel(xlsx, str(td))
    assert not any("文本源缺失" in i for i in iss), iss


def test_r19_audit_sample_hits_float_tail_stkcd(tmp_path, capsys):
    """浮点尾 stkcd（pandas 导出常态）也要命中文本档案——修前抽验静默跳过。"""
    from universal_scraper.audit import audit_panel
    td = tmp_path / "texts"
    td.mkdir()
    import gzip
    body = "正文" * 500          # 1000 字符，与面板 total_chars 一致
    (td / "600009_2022.txt.gz").write_bytes(gzip.compress(body.encode()))
    xlsx = _mk_panel(tmp_path, [
        ["600009.0", "2022.0", "某2022年年度报告", len(body), 0, 0.0, ""],
    ])
    iss = audit_panel(xlsx, str(td))
    assert not any("抽验重算未命中" in i for i in iss), iss
    assert "抽验重算 1/1" in capsys.readouterr().out


def test_r19_code6_normalization():
    from universal_scraper.audit import _code6
    assert _code6("600009") == "600009"
    assert _code6("600009.0") == "600009"   # pandas 浮点尾
    assert _code6(600009) == "600009"
    assert _code6(" 600009 ") == "600009"
    assert _code6("1") == "000001"          # 短码补零（对齐 at 另一家公司的风险口径）
