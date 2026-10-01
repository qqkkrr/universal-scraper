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
        # 审查八轮（L）：urlopen 曾走环境代理（http_proxy 开着时 9222 探测被发往
        # 代理）——与本文件自己警告的"代理劫持"场景自相矛盾。环回显式直连
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:9222/json/version", timeout=2) as r:
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
    # 网易云战训（2026-09）：曾把"有代理进程"与"系统代理已启用"合并报成"系统代理已开启"
    # ——Clash 类进程常驻但未接管系统代理时是假警报。三分支 + 本机代理软化（与
    # universal_scraper/doctor.py check_network 同口径）
    _hp = str(sp.get("http_proxy") or "")
    _port = sp.get("port") or 0
    _loopback = _hp.startswith("127.") or _hp in ("localhost", "::1")
    if sp.get("enabled") and _loopback:
        out.append({"item": f"系统代理为本机代理（{_hp}{':' + str(_port) if _port else ''}）",
                    "ok": True,  # 提示级：本机工具/透明代理，非劫持
                    "hint": "疑似本地工具/透明代理——cli ip 出口正常即可忽略本提示"})
    elif sp.get("enabled"):
        out.append({"item": f"系统代理已开启（{', '.join(sp['sources']) or _hp}）",
                    "ok": True,  # 提示级：不算失败
                    "hint": "直连请求可能被劫持！配额诊断前先确认真实出口（cli ip）。"
                            "别默认关代理——目标站依赖它时关掉直接连不上；只想给本次请求锁直连用 "
                            "no_proxy 即可；改系统代理属用户环境变更，由用户决定"})
    elif sp.get("processes"):
        out.append({"item": f"有代理进程（{', '.join(sp['processes'])}）但系统代理未启用",
                    "ok": True,
                    "hint": "当前大概率未被劫持（进程在跑 ≠ 代理接管；诊断异常时再 cli ip 查真实出口）"})
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
    total = ok_n = soft_n = 0
    print("🩺 万能爬虫技能 · 环境体检")
    for title, checks in groups:
        print(f"\n【{title}】")
        for c in checks:
            total += 1
            if c["ok"]:
                ok_n += 1
                print(f"  ✅ {c['item']}" + (f"  → {c['hint']}" if c["hint"] else ""))
            elif c.get("optional"):
                # 审查八轮（H）：可选依赖（缺省自动降级）曾与硬依赖共用 ❌ 和
                # 退出码——全新机器 setup.sh 刚装完必选包，doctor 却因缺可选
                # 包 exit 1，set -e 的 setup.sh 把成功安装判为失败（cli doctor
                # 第八轮已修此口径，本脚本漏同步）
                soft_n += 1
                print(f"  ⚠️ {c['item']}（可选，缺省时自动降级）" + (f"  → {c['hint']}" if c["hint"] else ""))
            else:
                print(f"  ❌ {c['item']}" + (f"  → {c['hint']}" if c["hint"] else ""))
    print()
    if ok_n == total:
        print("🎉 全部就绪，可以直接开始采集任务。")
        return 0
    _soft = f"（另有 {soft_n} 项可选依赖未装，不影响核心功能）" if soft_n else ""
    print(f"🔧 {total - ok_n - soft_n} 项待修复{_soft}。优先跑: bash \""
          f"{SKILL_DIR}/scripts/setup.sh\"；剩余按上面 → 提示逐条处理。")
    # 浏览器引擎缺失不阻塞 HTTP 直抓，返回 0 让向导自行判断
    # 审查修复（H）：固定容差 ±2 曾掩盖任意两项失败（含 python 依赖/技能完整性）。
    # 收紧：仅"Node 与浏览器引擎"组允许有失败（HTTP 直抓不受影响），其余组
    # 任一失败都如实返回 1；optional 失败不计入退出码
    for title, checks in groups:
        if title != "Node 与浏览器引擎" and any(not c["ok"] and not c.get("optional") for c in checks):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
