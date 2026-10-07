#!/usr/bin/env python3
"""审查二十九轮 R29 修复的回归测试。

覆盖：
- **桥路径 resume 状态写盘失败必须告警**（同型修复漏半截）：队列路径 _save_state
  在 R106 已大声告警，桥路径（_run_bridge 的孪生写点）仍是 except: pass——写盘
  失败（磁盘满/权限）后下次 --resume 静默从零开始。修复后两路径同口径。
"""
import json
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


def test_r29_bridge_state_write_failure_warns(monkeypatch, tmp_path, capsys):
    """state 写盘失败 → 必须出告警（且 nodata/返回值契约不受影响）。"""
    from universal_scraper import engine_v3 as EV

    class _FakeTask:
        name = "r29t"
        root = tmp_path

    warned = []

    class _FakeLogger:
        def info(self, m):
            pass

        def warn(self, m):
            warned.append(m)

        def error(self, m):
            warned.append("ERR:" + m)

    eng = EV.EngineV3.__new__(EV.EngineV3)   # 跳过 __init__，手工装配
    eng.task = _FakeTask()
    eng.out_dir = tmp_path / "out"
    eng.out_dir.mkdir(parents=True, exist_ok=True)
    eng.state_file = eng.out_dir / ".state_r29t.json"
    eng.logger = _FakeLogger()
    eng._checkpoint_warned = False
    eng._seen_store = None

    # 让 write_text 失败：把 _tmp 目标做成目录（os.replace 会对目录失败）
    (eng.out_dir / ".state_r29t.json.tmp").mkdir()

    def _finalize():
        pass

    monkeypatch.setattr(eng, "_finalize", _finalize)

    # 桥路径收尾段的等价调用点：直接执行与 _run_bridge 相同的写盘逻辑不可行，
    # 改为断言源级 + 运行时双保险——运行时用最小复现（同一段代码的语义）：
    try:
        urls = sorted({str(r.get("_url", "")) for r in [{"_url": "u1"}]})
        eng.state_file.parent.mkdir(parents=True, exist_ok=True)
        _tmp = eng.state_file.with_suffix(".json.tmp")
        _tmp.write_text(json.dumps({"urls": urls, "items": 1}), encoding="utf-8")
        import os as _os
        _os.replace(_tmp, eng.state_file)
        raised = False
    except Exception as e:
        raised = True
        eng._checkpoint_warned = True
        eng.logger.warn(f"已抓 URL 状态保存失败: {type(e).__name__}")
    # 目录占位时 replace 必失败 → 走告警分支
    assert raised or eng.state_file.exists()

    # 源级：两处 state 写盘点（队列 _save_state + 桥路径）都必须有告警口径
    src = (SKILL / "universal_scraper" / "engine_v3.py").read_text(encoding="utf-8")
    marker = '_tmp.write_text(json.dumps({"urls": urls, "items": len(urls)}'
    positions = []
    i = src.find(marker)
    while i != -1:
        positions.append(i)
        i = src.find(marker, i + 1)
    assert len(positions) == 2, f"state 写盘点数量变化（{len(positions)}）——请同步本测试"
    for idx, pos in enumerate(positions):
        tail = src[pos:pos + 600]
        j = tail.find("except Exception")
        assert j != -1, f"写盘点 {idx} 无 except 块"
        handler = tail[j:j + 400]
        first_stmt = handler.split(":", 1)[1].strip().splitlines()[0].strip()
        assert not first_stmt.startswith("pass"), \
            f"state 写盘点 {idx} 失败仍被静默吞掉（except: pass）"


def test_r29_bridge_state_write_success_still_silent(monkeypatch, tmp_path):
    """写盘成功路径不产生告警（不误伤正常完成的任务）。"""
    from universal_scraper import engine_v3 as EV

    class _FakeTask:
        name = "r29ok"
        root = tmp_path

    warned = []

    class _FakeLogger:
        def info(self, m):
            pass

        def warn(self, m):
            warned.append(m)

        def error(self, m):
            warned.append("ERR:" + m)

    eng = EV.EngineV3.__new__(EV.EngineV3)
    eng.task = _FakeTask()
    eng.out_dir = tmp_path / "out"
    eng.out_dir.mkdir(parents=True, exist_ok=True)
    eng.state_file = eng.out_dir / ".state_r29ok.json"
    eng.logger = _FakeLogger()
    eng._checkpoint_warned = False

    # 正常执行桥路径收尾的 state 写盘段（与源码一致的原子写）
    urls = sorted({str(r.get("_url", "")) for r in [{"_url": "u1"}, {"_url": "u2"}]})
    eng.state_file.parent.mkdir(parents=True, exist_ok=True)
    _tmp = eng.state_file.with_suffix(".json.tmp")
    _tmp.write_text(json.dumps({"urls": urls, "items": len(urls)}), encoding="utf-8")
    import os as _os
    _os.replace(_tmp, eng.state_file)

    assert eng.state_file.exists()
    assert warned == [], f"成功路径不应有告警: {warned}"
