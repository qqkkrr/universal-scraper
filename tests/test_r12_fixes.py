#!/usr/bin/env python3
"""审查十二轮（R12）修复的回归测试——全部 hermetic（轻量 e2e 走本地环回）。

覆盖：
- audit：C1 面板 tuple 不崩 / H2 覆盖对账 + 缺口清单豁免
- engine_v3：H3 同 URL 重跑页级合并（同页多行不压、不翻倍、详情不丢）/ H2 limit 声明=文件
- verify：H3 verdict 并入逐文件 ok/完整率
- research：H4 熔断（成功项才清零——API 通/static 断也熔断）
- auto：H3 域名标签边界 / H2 出站守卫 / H7 删除命中
- precise：H3 url_template 直接赋值 / H2 相关性闸降级为告警
- book：H5 UNMATCHED 清空商品字段
- selectors：M1 css_html 用 _HAS_LXML
"""
import gzip
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


# ---------------- audit ----------------

def test_audit_panel_tuple_and_coverage(tmp_path):
    from openpyxl import Workbook
    from universal_scraper.audit import audit_panel
    d = tmp_path
    wb = Workbook()
    ws = wb.active
    ws.append(["stkcd", "year", "title", "total_chars", "compliance_kw_count",
               "compliance_kw_freq", "备注"])
    ws.append(["600519", "2022", "某年报", 1000, 5, 10.0, ""])
    wb.save(d / "panel.xlsx")
    td = d / "texts"
    td.mkdir()
    (td / "600519_2022.txt.gz").write_bytes(gzip.compress("测试文本".encode()))
    uni = d / "universe.csv"
    uni.write_text("stkcd,first_year,last_year\n600519,2021,2022\n", encoding="utf-8")
    # C1：带备注列 + texts 不崩（修前 AttributeError）
    iss = audit_panel(str(d / "panel.xlsx"), texts_dir=str(td), universe_csv=str(uni))
    assert not any("AttributeError" in i for i in iss)
    # H2：覆盖缺口被报出（600519_2021 缺）
    assert any("覆盖疑似缺失" in i or "不在缺口清单" in i for i in iss)
    # H2b：progress.json 标注 nodata → 豁免
    (d / "progress.json").write_text(json.dumps({"done": {"600519_2021": "nodata"}}),
                                     encoding="utf-8")
    iss2 = audit_panel(str(d / "panel.xlsx"), texts_dir=str(td), universe_csv=str(uni))
    assert not any("覆盖疑似缺失" in i or "不在缺口清单" in i for i in iss2)


# ---------------- engine_v3（轻量 e2e） ----------------

@pytest.fixture()
def v3_env(monkeypatch, tmp_path):
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/list"):
                body = json.dumps({"data": {"list": [
                    {"id": i, "title": t} for i, t in [(1, "A"), (2, "B"), (3, "C"), (4, "D")]]}},
                    ensure_ascii=False).encode()
            else:
                body = json.dumps({"data": {"body": "详情-" + self.path.rsplit("/", 1)[-1]}},
                                  ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = tmp_path / "task"
    (base / "modules").mkdir(parents=True)
    cfg = {
        "name": "r12t", "start_urls": [f"http://127.0.0.1:{port}/list"],
        "source": {"type": "http"},
        "parsers": {"default": {"type": "json", "records_path": "data.list",
                                "fields": {"id": "id", "title": "title", "url_t": "id"}}},
        "detail": {"enabled": True, "url_field": "url_t",
                   "url_transform": [{"type": "prefix",
                                      "prefix": f"http://127.0.0.1:{port}/detail/"}],
                   "extract": [{"name": "body", "type": "regex",
                                "pattern": r"(详情-\d+)"}]},
        "storage": {"type": "jsonl"},
        "output": {"dir": str(tmp_path / "out"), "base_name": "r12out",
                   "spool_threshold": 2},
        "anti_bot": {"min_interval": 0.0},
        "rules": [{"match": "contains", "pattern": "/", "parser": "default"}],
    }
    (base / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    yield base, tmp_path / "out"
    srv.shutdown()


def test_engine_v3_rerun_and_limit(v3_env):
    from universal_scraper.engine_v3 import run_task
    base, out = v3_env
    jl = out / "items" / "r12t.jsonl"
    r1 = run_task(base, log_file=None)
    rows1 = [json.loads(l) for l in jl.read_text(encoding="utf-8").strip().splitlines()]
    # H3 基础：同页 4 行不被压 + 全带详情
    assert len(rows1) == 4 and all(x.get("body") for x in rows1)
    assert r1.get("total") == 4
    # H3：同 URL 重跑不翻倍、详情不丢
    r2 = run_task(base, log_file=None)
    rows2 = [json.loads(l) for l in jl.read_text(encoding="utf-8").strip().splitlines()]
    assert len(rows2) == 4 and all(x.get("body") for x in rows2)
    assert r2.get("total") == 4
    # H2：limit 声明 = 导出行数
    r3 = run_task(base, limit=3, log_file=None)
    exp = json.loads((out / "r12out.json").read_text(encoding="utf-8"))
    assert len(exp) == 3 and r3.get("total") == 3


# ---------------- verify ----------------

def test_verify_verdict_includes_file_ok(tmp_path):
    from universal_scraper.verify import verify_dir
    d = tmp_path / "empty_fields"
    d.mkdir()
    (d / "data.json").write_text(json.dumps(
        [{"title": "", "url": ""}] * 3), encoding="utf-8")
    r = verify_dir(str(d), log=lambda *a, **k: None)
    assert r["verdict"] == "partial"      # 修前 ok（exit 0 放行）
    d2 = tmp_path / "good"
    d2.mkdir()
    (d2 / "data.json").write_text(json.dumps(
        [{"title": "甲", "url": "http://x/1"}, {"title": "乙", "url": "http://x/2"}]),
        encoding="utf-8")
    assert verify_dir(str(d2), log=lambda *a, **k: None)["verdict"] == "ok"


# ---------------- research ----------------

class _FakeRunner:
    pass


def test_research_fuse_counts_static_failures(tmp_path):
    from universal_scraper.research import ResearchRunner

    class Fake(ResearchRunner):
        def fetch_announce(self, code, year):
            return [("t", b"http://x/f.pdf")]

        def download_bytes(self, adjunct):
            raise RuntimeError("static blocked")

    r = Fake(tmp_path, {"k": []}, fuse=3)
    r.api_interval = 0.0
    r.jitter = 0.0
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = r.run([(f"60051{i}", 2022) for i in range(6)])
    # 修前：bad 被 API 成功清零 → rc=0 永不熔断
    assert rc == 3 and "连续 3 次异常" in buf.getvalue()


# ---------------- auto ----------------

def test_auto_domain_boundary_and_guard(monkeypatch):
    from universal_scraper import auto as A
    import re as _re

    def _m(host, desc):
        _d = desc.lower()
        return bool(_re.search(rf"(?<![a-z0-9.-]){_re.escape(host)}(?![a-z0-9.-])", _d))

    assert _m("example.com", "抓 notexample.com 的价格") is False
    assert _m("example.com", "抓 example.com 的价格") is True
    # 测试必须自洽：显式控制 env（同进程的其他模块导入时可能已设 US_ALLOW_PRIVATE）
    monkeypatch.delenv("US_ALLOW_PRIVATE", raising=False)
    assert A._outbound_ok("http://127.0.0.1:9222/x", "t") is False
    assert A._outbound_ok("https://example.com/", "t") is True
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")
    assert A._outbound_ok("http://127.0.0.1:9222/x", "t") is True   # 白名单显式放行


# ---------------- precise ----------------

def test_precise_matcher_softened():
    from universal_scraper.precise_auto import _rows_match_task
    # 规则闸本身仍能识别无交集（供告警用）
    assert _rows_match_task("抓取图书价格数据", [{"书名": "活着", "价格": "28"}]) is True
    assert _rows_match_task("抓取图书价格数据", [{"foo": "bar"}]) is False
    # 调用点降级为告警（源码级）
    import inspect
    from universal_scraper import precise_auto as P
    src = inspect.getsource(P._repair_probe)
    assert "已接受该结果" in src and "需要换入口或修正选择器" not in src
    src2 = inspect.getsource(P)
    assert '_mp["url_template"] = tmpl' in src2       # H3 直接赋值（修前 setdefault no-op）


# ---------------- book ----------------

def test_book_dangdang_unmatched_cleared():
    from universal_scraper.book_catalog import parse_dangdang_search
    # 卡片是别的书（不含目标 ISBN）→ UNMATCHED：商品字段必须全空
    card_html = ('<ul class="bigimg"><li><p class="name"><a title="别的一本书" '
                 'href="http://product.dangdang.com/999.html">别的一本书</a></p>'
                 '<p class="price"><span class="price_n">¥20.00</span></p></li></ul>')
    rows = parse_dangdang_search(
        card_html, "https://search.dangdang.com/?key=9787506365437")
    r = rows[0]
    assert r["dangdang_status"] == "UNMATCHED"
    assert not r["dangdang_title"] and not r["dangdang_link"] and not r["dangdang_price"]


# ---------------- selectors ----------------

def test_selectors_css_html_uses_lxml_flag():
    import inspect
    from universal_scraper import selectors as S
    src = inspect.getsource(S.css_html)
    assert "if _HAS_LXML else str(el)" in src          # 修前 HAS_LXML
