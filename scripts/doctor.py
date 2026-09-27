#!/usr/bin/env python3
"""🩺 技能环境体检：Python 依赖 / Node / 浏览器引擎 / 桥接脚本。
用法: python3 "${SKILL_DIR}/scripts/doctor.py"
全绿即可开工。修复交给 setup.sh 或按提示逐条处理。
"""
from __future__ import annotations

import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from universal_scraper.doctor import check_browsers, check_deps, check_node  # noqa: E402

BRIDGES = ["browser_generic.cjs", "browser_single.cjs", "browser_pool.cjs",
           "browser_common.cjs", "agent_browser.cjs", "browser_agent.cjs"]


def check_bridges() -> list:
    missing = [b for b in BRIDGES if not (SKILL_DIR / "scripts" / b).exists()]
    return [{"item": "浏览器桥接脚本", "ok": not missing,
             "hint": "" if not missing else f"缺失: {', '.join(missing)}"}]


def check_cli() -> list:
    try:
        import universal_scraper.cli  # noqa: F401
        return [{"item": "CLI 可加载", "ok": True, "hint": ""}]
    except Exception as e:
        return [{"item": "CLI 可加载", "ok": False, "hint": str(e)[:120]}]


def check_cdp() -> list:
    """9222 调试 Chrome 活性：已开着就提示复用（跨任务共享登录态），没开不算失败。"""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=2) as r:
            if r.status == 200:
                return [{"item": "调试 Chrome (9222)", "ok": True, "hint": "运行中——配置 cdp 可直接复用"}]
    except Exception:
        pass
    return [{"item": "调试 Chrome (9222)", "ok": True,
             "hint": "未运行（需要登录态/L3 时跑 open-debug-chrome.sh）"}]


def check_netlink() -> list:
    """网络链路体检（科研管理战役 2026-09 战训）：
    1) 真实出口 IP（换网络后确认）
    2) 系统代理劫持（Clash 等开着时"直连"其实是代理节点出口——烧错配额/换IP无效的元凶）
    3) 电源模式（无人值守长跑必须接电，电池下 caffeinate 无效）
    全部为"提示级"：不阻塞任务，只在有风险时给警告。"""
    from universal_scraper.net import detect_ip, detect_system_proxy, power_source
    out = []
    d = detect_ip()
    out.append({"item": f"出口 IP（{d.get('ip','?')} {d.get('city','')}）",
                "ok": not d.get("error"),
                "hint": "" if not d.get("error") else str(d.get("error"))[:80]})
    sp = detect_system_proxy()
    if sp.get("enabled") or sp.get("processes"):
        out.append({"item": f"系统代理已开启（{', '.join(sp['sources']) or '本机进程'}）",
                    "ok": True,  # 提示级：不算失败
                    "hint": "直连请求可能被劫持！配额诊断前先确认真实出口，必要时关系统代理"})
    else:
        out.append({"item": "系统代理未检出（直连出口可信）", "ok": True, "hint": ""})
    pw = power_source()
    if pw.get("source") == "BATT":
        out.append({"item": "电源：电池模式", "ok": True,
                    "hint": "无人值守长跑请接电源（电池下 caffeinate 防睡眠无效）"})
    # batch1600 战训：显示当前处于封锁期的域名（HTTP 客户端 403/421/52x 自动记账）
    try:
        from universal_scraper.domain_budget import listing
        blocked = {d: v for d, v in listing().items() if v.get("in_cooldown")}
        if blocked:
            items = ", ".join(f"{d}(剩{v['remaining_sec']//3600}h)" for d, v in
                              sorted(blocked.items(), key=lambda kv: -kv[1]["remaining_sec"])[:5])
            out.append({"item": f"封锁期域名 {len(blocked)} 个", "ok": True,
                        "hint": f"{items} —— 这些站先冷却，别硬刚（budget --list 看详情）"})
    except Exception:
        pass
    return out


def main() -> int:
    groups = [
        ("Python 依赖", check_deps()),
        ("Node 与浏览器引擎", check_node() + check_browsers()),
        ("技能完整性", check_bridges() + check_cli() + check_cdp()),
        ("网络链路（战训新增）", check_netlink()),
    ]
    total = ok_n = 0
    print("🩺 万能爬虫技能 · 环境体检")
    for title, checks in groups:
        print(f"\n【{title}】")
        for c in checks:
            total += 1
            ok_n += 1 if c["ok"] else 0
            mark = "✅" if c["ok"] else "❌"
            print(f"  {mark} {c['item']}" + (f"  → {c['hint']}" if c["hint"] else ""))
    print()
    if ok_n == total:
        print("🎉 全部就绪，可以直接开始采集任务。")
        return 0
    print(f"🔧 {total - ok_n} 项待修复。优先跑: bash \""
          f"{SKILL_DIR}/scripts/setup.sh\"；剩余按上面 → 提示逐条处理。")
    # 浏览器引擎缺失不阻塞 HTTP 直抓，返回 0 让向导自行判断
    # 审查修复（H）：固定容差 ±2 曾掩盖任意两项失败（含 python 依赖/技能完整性）。
    # 收紧：仅"Node 与浏览器引擎"组允许有失败（HTTP 直抓不受影响），其余组
    # 任一失败都如实返回 1
    for title, checks in groups:
        if title != "Node 与浏览器引擎" and any(not c["ok"] for c in checks):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
