#!/usr/bin/env python3
"""实战反馈轮 R21（2026-10 快手战役反馈）修复的回归测试。

覆盖：
- sites 显示层：手写 status 不再被"注册表 desc 推断"覆盖（快手曾被显示成
  "需登录调优" → 实战白扫 6 次码的根因链）；endpoints 端点级登录要求透出与显示
- 数据一致性：B站/天气 过时标注修正；注册表 desc 不再一律"（浏览器渲染）"
- 文档：判型表新增 protobuf/WebSocket 两行；playbook 第九章形态章；SKILL 铁律 №6
"""
import contextlib
import io
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


# ---------- 显示层：手写 status 优先 ----------

def test_r21_authored_status_wins_over_registry_inference():
    """回归：此前 17/21 站的手写 status 被"desc 含浏览器渲染 → 需登录调优"覆盖。

    最刺眼的一条：快手（R47 实测评论免登录）被显示成"🔧 浏览器模式·需登录调优"。"""
    from universal_scraper.sites import HIGH_FREQUENCY_SITES, list_sites
    authored = {s["name"]: s.get("status", "") for s in HIGH_FREQUENCY_SITES}
    shown = {x["name"]: x["status"] for x in list_sites()}
    mismatched = {n: (a, shown.get(n)) for n, a in authored.items()
                  if str(a or "").strip() and shown.get(n) != a}
    assert not mismatched, f"手写 status 被覆盖: {mismatched}"
    # 具体回归：快手列表里必须是"免登录"，绝不能出现"需登录调优"
    k = shown["快手"]
    assert "免登录" in k and "需登录调优" not in k, k


def test_r21_endpoints_schema_and_required_facts():
    """端点级登录要求：枚举合法、字段完整；快手"评论=免登录"必须在内。"""
    from universal_scraper.sites import SITE_AUTH_VALUES, HIGH_FREQUENCY_SITES
    seen = 0
    for s in HIGH_FREQUENCY_SITES:
        eps = s.get("endpoints")
        if eps is None:
            continue
        assert isinstance(eps, list) and eps, f"{s['name']}: endpoints 应为非空数组"
        for e in eps:
            assert isinstance(e, dict), f"{s['name']}: 端点项应为 dict"
            assert isinstance(e.get("name"), str) and e["name"].strip(), f"{s['name']}: 端点需 name"
            assert e.get("auth") in SITE_AUTH_VALUES, \
                f"{s['name']}/{e.get('name')}: auth={e.get('auth')!r} 不在 {SITE_AUTH_VALUES}"
            seen += 1
    assert seen >= 8, f"endpoints 标注太少（{seen} 项）——实证站应尽快回填"
    ks = {s["name"]: s for s in HIGH_FREQUENCY_SITES}["快手"]
    auths = {e["name"]: e["auth"] for e in ks["endpoints"]}
    assert auths.get("评论") == "none", auths


def test_r21_format_endpoints_labels():
    from universal_scraper.sites import format_endpoints
    assert format_endpoints({"endpoints": [{"name": "评论", "auth": "none"}]}) == "评论=免登录"
    assert format_endpoints({"endpoints": [{"name": "主页", "auth": "login"},
                                           {"name": "搜索", "auth": "cookie"}]}) \
        == "主页=需登录 / 搜索=需 Cookie"
    # 无标注 / 脏数据不炸
    assert format_endpoints({}) == ""
    assert format_endpoints({"endpoints": "bad"}) == ""
    assert format_endpoints({"endpoints": [{"auth": "none"}, {"name": "B", "auth": "nope"}]}) == "B=nope"
    # 上限 6 条
    many = [{"name": f"E{i}", "auth": "none"} for i in range(10)]
    assert format_endpoints({"endpoints": many}).count("=") == 6


def test_r21_stale_statuses_fixed():
    """B站/天气 手写"待精配"与实际能力不符（bili 命令/R34；天气公开 API）。"""
    from universal_scraper.sites import HIGH_FREQUENCY_SITES, SITES
    m = {s["name"]: s for s in HIGH_FREQUENCY_SITES}
    assert "待精配" not in m["B站"]["status"] and "已精配" in m["B站"]["status"]
    assert "待精配" not in m["天气"]["status"]
    # 注册表 desc 不再一律"（浏览器渲染）"（有手写 desc 的站以手写为准）
    assert "浏览器渲染" not in SITES["kuaishou"].get("desc", "")


def test_r21_cli_sites_output_shows_endpoints(monkeypatch):
    """端到端：CLI `sites` 输出必须体现端点级登录要求（agent 读的就是这段）。"""
    from universal_scraper import cli
    monkeypatch.setattr(sys, "argv", ["universal_scraper.cli", "sites"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main()
    out = buf.getvalue()
    assert rc == 0, out[-300:]
    assert "评论=免登录" in out, "快手端点标注未出现在 sites 输出"
    k_line = next(ln for ln in out.splitlines() if "快手" in ln)
    assert "需登录调优" not in k_line, k_line
    assert "└" in out and "用户微博=需 Cookie" in out


# ---------- 文档：形态章与纪律 ----------

def test_r21_playbook_protocol_forms_chapter():
    doc = (SKILL / "references" / "anti-block-playbook.md").read_text(encoding="utf-8")
    assert "## 九、接口形态判型" in doc
    for k in ("protobuf", "gRPC-Web", "WebSocket", "decode_raw", "framereceived"):
        assert k in doc, f"第九章缺 {k}"
    # 判型表两行（只增不删）
    assert "响应体是二进制乱码" in doc and "wss://" in doc
    # 纪律
    assert "形态先判" in doc


def test_r21_skill_rules_and_routing():
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "先查表再试错" in skill
    assert "protobuf" in skill and "第九章" in skill
    quick = (SKILL / "references" / "agent-quickref.md").read_text(encoding="utf-8")
    assert "接口看不懂形态" in quick
