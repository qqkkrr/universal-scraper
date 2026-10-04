#!/usr/bin/env python3
"""审查二十二轮 R22 修复的回归测试。

覆盖：
- **v2 引擎 --limit + 增量去重静默丢数据**（实测复现：limit=2 冒烟后全量跑，
  从未出现在任何导出文件里的记录被标已见 → 永久跳过）
- `_row_keys` 口径统一（无键行 "" 占位、复合键、与行一一对应）
- incremental.key 被 record.fields 改名后的"增量静默失效"告警（四态）
- CLI 死参数 / 配方引用两项新静态检查（检查本体可跑且全绿，防退化）
"""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))

_RECORDS = [{"title": "无ID行"}, {"id": "r2", "title": "第二行"},
            {"id": "r3", "title": "第三行"}, {"id": "r4", "title": "第四行"}]


@pytest.fixture()
def stub_json_server(monkeypatch):
    monkeypatch.setenv("US_ALLOW_PRIVATE", "1")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"data": {"list": _RECORDS}}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/api"
    finally:
        srv.shutdown()


def _cfg(url: str, out: Path) -> dict:
    return {
        "name": "r22t",
        "source": {"type": "http_json", "url": url},
        "pagination": {"strategy": "none", "records_path": "data.list"},
        "record": {"fields": {"标题": {"from": "title"}, "id": {"from": "id"}}},
        "incremental": {"enabled": True, "key": "id"},
        "storage": {"type": "jsonl"},
        "output": {"dir": str(out), "base_name": "r22"},
        "anti_bot": {"min_interval": 0.0},
    }


def test_r22_limit_incremental_no_silent_data_loss(stub_json_server, tmp_path):
    """回归（数据丢失级）：limit 冒烟只允许标记**实际导出过**的行。

    修前：_pending_keys[:len(rows)] 按数量裁——行含无键行时把从未导出的 r3
    也标成已见，全量跑时 r3 被永久跳过（导出只剩 无ID行+第四行）。"""
    from universal_scraper.engine import run_config
    out = tmp_path / "out"
    cfg = _cfg(stub_json_server, out)

    run_config(dict(cfg), limit=2)                       # 冒烟：导出 keyless + r2
    seen = (out / ".seen_r22t.txt").read_text(encoding="utf-8").split()
    assert seen == ["r2"], f"只应标记实际导出的 r2，实际 {seen}"

    run_config(dict(cfg), limit=None)                    # 全量
    titles = [r.get("标题") for r in json.loads((out / "r22.json").read_text(encoding="utf-8"))]
    assert "第三行" in titles, f"从未导出过的行被误标已见而丢失: {titles}"
    assert "第四行" in titles, titles
    # r2 在冒烟轮已实际导出，增量语义下全量轮不再重复——这是契约而非丢失
    assert "第二行" not in titles


def test_r22_row_keys_alignment():
    from universal_scraper.engine import _row_keys
    rows = [{"id": "a"}, {"title": "无键"}, {"id": "b", "x": 1}]
    assert _row_keys(rows, "id") == ["a", "", "b"]
    # 复合键：任一缺失 = 无键（""），全缺时不产出真值 "|"（曾在 v2 触发过跨轮误判）
    assert _row_keys([{"a": "1", "b": "2"}, {"a": "1"}], ["a", "b"]) == ["1|2", ""]
    # content_hash 特例不抛
    assert len(_row_keys([{"t": "x"}], "content_hash")) == 1


def test_r22_incremental_key_mapping_warning():
    from universal_scraper.config import collect_warnings
    base = {"name": "t",
            "source": {"type": "http_json", "url": "http://x/", "records_path": "d.l"},
            "record": {"fields": {"标题": {"from": "title"}, "ID": {"from": "id"}}},
            "incremental": {"enabled": True, "key": "id"}}
    w = [x for x in collect_warnings(dict(base)) if "incremental.key" in x]
    assert w and "静默失效" in w[0]
    ok = dict(base, incremental={"enabled": True, "key": "ID"})
    assert not [x for x in collect_warnings(ok) if "incremental.key" in x]
    ok2 = dict(base, incremental={"enabled": True, "key": "content_hash"})
    assert not [x for x in collect_warnings(ok2) if "incremental.key" in x]
    ok3 = dict(base, incremental={"enabled": True, "key": "dedup_key"},
               pipeline=[{"type": "template", "field": "dedup_key", "tmpl": "{id}"}])
    assert not [x for x in collect_warnings(ok3) if "incremental.key" in x]


def test_r22_static_checks_present_and_clean(capsys):
    """两项新检查（CLI 死参数 / 配方引用）必须存在且当前全绿。"""
    spec = importlib.util.spec_from_file_location("r22_static_checks",
                                                  SKILL / "tests" / "static_checks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0, out[-600:]
    assert "失败 0 项" in out, out[-300:]
    src = (SKILL / "tests" / "static_checks.py").read_text(encoding="utf-8")
    assert "check_cli_flags_wired" in src and "check_recipe_refs_resolve" in src
