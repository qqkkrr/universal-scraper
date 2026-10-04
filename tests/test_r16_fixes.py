#!/usr/bin/env python3
"""审查十六轮（R16）修复的回归测试——全部 hermetic。

覆盖：
- engine_v3：详情批抓的中间件 on_request/on_response 接线（源级）
- auto：WAF 升级保留 verify / 变量扫描覆盖 actions+url_transform（整包 JSON）
- verify：sparse 核心字段判定前缀/全等口径（video 不再因含 id 判核心）
- queue：_max_seen 容量满可观测（skipped_capacity 计数）
- mcp：initialize 协议版本协商（新客户端回退/旧客户端尊重/非法形态回退）
       + notification 不回包（R15 回归）
"""
import inspect
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


def test_engine_v3_detail_middleware_wired():
    from universal_scraper import engine_v3 as E
    src = inspect.getsource(E.EngineV3._run_details)
    ws = src[src.index("def _work"):]
    assert "mw.on_request(_req, self.ctx)" in ws
    assert "mw.on_response(resp, self.ctx)" in ws
    assert "详情 on_request 失败" in ws       # per-middleware 守卫
    # 复杂插件契约不接（记录待修，避免契约重建引入新问题）
    assert "apply_request_hooks" not in ws


def test_auto_waf_upgrade_keeps_verify():
    from universal_scraper.auto import _validate_and_fix, _force_browser_waf
    cfg = {"name": "t", "start_urls": ["https://x.com/"],
           "source": {"type": "http", "url": "https://x.com/",
                      "verify": {"enabled": True, "markers": ["滑块"]}},
           "parsers": {"default": {"type": "html", "fields": {"t": "h1"}}},
           "rules": [{"match": "contains", "pattern": "/", "parser": "default"}]}
    c2 = _force_browser_waf(_validate_and_fix(cfg))
    # 修前 verify 被 pop——无头池反复撞滑块而日志让用户去"弹窗"（弹窗没开）
    assert c2["source"].get("verify", {}).get("enabled") is True


def test_auto_needs_input_covers_actions_and_transform():
    from universal_scraper.auto import _validate_and_fix
    cfg = {"name": "t2", "start_urls": ["https://x.com/"],
           "source": {"type": "browser",
                      "actions": [{"type": "type", "selector": ".q", "text": "{city}"}]},
           "detail": {"enabled": True,
                      "url_transform": [{"type": "prefix",
                                         "prefix": "https://{city}.x.com/"}]},
           "parsers": {"default": {"type": "html", "fields": {"t": "h1"}}},
           "rules": [{"match": "contains", "pattern": "/", "parser": "default"}]}
    c = _validate_and_fix(cfg)
    # 修前只扫 start_urls+query，actions/text 与 url_transform 的 {city} 漏检
    assert "city" in (c.get("_needs_input") or [])


def test_verify_core_field_prefix_match():
    from universal_scraper.verify import verify_rows
    rows = [{"title": f"T{i}", "video": ""} for i in range(5)]
    sm = (verify_rows(rows, declared=5) or {}).get("sparse_matrix") or {}
    # 修前 "video" 因含 "id" 被判核心 → core_rate=0.5 失真
    assert sm.get("core_rate") == 1.0
    assert sm.get("sparse_fields") == 1
    # 正常核心字段回归
    rows2 = [{"title": f"T{i}", "url": f"http://x/{i}"} for i in range(5)]
    sm2 = (verify_rows(rows2, declared=5) or {}).get("sparse_matrix") or {}
    assert sm2.get("core_rate") == 1.0


def test_queue_capacity_observable():
    from collections import deque
    from universal_scraper.queue import RequestQueue
    from universal_scraper.protocols import Request
    q = RequestQueue.__new__(RequestQueue)
    q._seen = set()
    q._max_seen = 2
    q.stats = {"skipped_dup": 0, "enqueued": 0}
    q._domain_min_interval = {}
    q.min_interval = 0.0
    q._lock = __import__("threading").Lock()
    q._q = deque()
    for i in range(3):
        q.enqueue(Request(url=f"http://a/{i}"))
    # 容量满：第 3 个不同 key 被拒且计数可见（修前静默 False 与"重复"无法区分）
    assert q.stats.get("skipped_capacity", 0) >= 1


def test_mcp_protocol_negotiation():
    from universal_scraper.mcp_server import handle_message
    r = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"}})
    assert r["result"]["protocolVersion"] == "2024-11-05"   # 新客户端回服务端支持的
    r2 = handle_message({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                         "params": {"protocolVersion": "2020-01-01"}})
    assert r2["result"]["protocolVersion"] == "2020-01-01"  # 旧客户端尊重
    r3 = handle_message({"jsonrpc": "2.0", "id": 3, "method": "initialize",
                         "params": {"protocolVersion": "9999-01-01"}})
    assert r3["result"]["protocolVersion"] == "2024-11-05"  # 超支持回退
    r4 = handle_message({"jsonrpc": "2.0", "id": 4, "method": "initialize",
                         "params": {"protocolVersion": 123}})
    assert r4["result"]["protocolVersion"] == "2024-11-05"  # 非法形态回退
    # R15 回归：notification 不回包
    assert handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
