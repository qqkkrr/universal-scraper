"""R33 验证电池：JSONL 行切分纪律（`splitlines()` 会把整条记录切碎 ✗）。

**缺陷**：`str.splitlines()` 按**全部 Unicode 行边界**切分，包括 `U+2028`(LINE SEP) /
`U+2029`(PARA SEP) —— 而抓到的正文里就有它们（实测某交付 JSONL 里 16 个 U+2028，
`splitlines()` 把 29,539 行切成 29,556 行 ✗）。被切碎的行 `json.loads` 整条失败 →
**静默丢记录**：引擎 spool 读取失败会退回内存数据（还有子集覆盖全量的风险 ✗✗）、
日志台账丢条目、verify 审计工具把整份产物判成"解析失败" ✗。

**纪律**：JSONL 一律 `.split("\n")` ✓（`tests/static_checks.py` 有 AST/文本级检查 ✓）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from universal_scraper import health as H          # noqa: E402
from universal_scraper import verify as V          # noqa: E402

U2028 = chr(0x2028)
U2029 = chr(0x2029)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "STATE_DIR", tmp_path / "health")
    monkeypatch.setattr(H, "LEDGER_PATH", tmp_path / "health" / "ledger.jsonl")
    monkeypatch.setattr(H, "STATE_PATH", tmp_path / "health" / "state.json")
    yield


def test_splitlines_would_break_this_record():
    """先自证：`splitlines()` 确实会切碎含 U+2028 的一行 JSONL（用例本身有意义 ✓）。"""
    row = {"source": "s", "count": 3, "note": f"前置{U2028}后置"}
    line = json.dumps(row, ensure_ascii=False)
    assert len(line.split("\n")) == 1                     # 按 "\n" 切：仍是 1 行 ✓
    assert len(line.splitlines()) == 2                    # splitlines()：被切成 2 ✗✗
    with pytest.raises(json.JSONDecodeError):
        json.loads(line.splitlines()[0])                  # 碎片无法解析 ✗


def test_health_ledger_survives_u2028_in_note(tmp_path):
    """★ 回归：账本 note 含 U+2028 时，记录**必须仍被读到**（旧实现整条丢 ✗）。"""
    led = H.LEDGER_PATH
    r = H.record("src", count=2293, note=f"含行分隔符{U2028}的备注", ledger_path=led)
    assert r["ok"] is True
    b = H.baseline("src", ledger_path=led)
    assert b["last_count"] == 2293, "含 U+2028 的记录被丢了 ✗"
    assert b["rows_seen"] == 1
    assert H.check("src", 2293, ledger_path=led, state_path=H.STATE_PATH)["verdict"] == "ok"


def test_health_ledger_survives_u2029_and_multiple_rows(tmp_path):
    led = H.LEDGER_PATH
    for i, note in enumerate((f"a{U2029}b", "plain", f"c{U2028}d"), 1):
        assert H.record("src", count=100 * i, note=note, ledger_path=led)["ok"]
    b = H.baseline("src", ledger_path=led)
    assert b["rows_seen"] == 3 and b["last_count"] == 300


def test_verify_reads_jsonl_with_u2028(tmp_path):
    """★ 回归：verify 的 JSONL 兼容路径不得因 U+2028 把整份产物判成解析失败 ✗。"""
    p = tmp_path / "out.jsonl"
    rows = [{"url": f"https://example.com/{i}", "title": f"标题{U2028}副标题", "n": i}
            for i in range(1, 4)]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")
    res = V.verify_file(str(p))
    assert res.get("ok") is not False, f"含 U+2028 的 JSONL 被判解析失败 ✗：{res.get('error')}"
    assert (res.get("total") or res.get("records") or 0) == 3, res
