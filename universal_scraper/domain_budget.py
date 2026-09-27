#!/usr/bin/env python3
"""🚧 域名礼貌预算/封锁台账（batch1401 战训：chinamoney 421、szse 断连是 24h 级封禁，
playbook 只有抽象原则没有工程化载体）。基于 quota_ledger 的持久化结构，
按域名记录 [最后事件时间, 冷却小时数, 备注]——小时数随台账持久（审查修复：
v1.12.1 前 hours 只改运行时窗口、check/list 恒按 24h 计算，48h 封禁被谎报成已解封）。

用法:
  python3 -m universal_scraper.cli budget --mark chinamoney.com --hours 24 --note "421 限流"
  python3 -m universal_scraper.cli budget --check chinamoney.com     # 冷却中? 剩余秒?
  python3 -m universal_scraper.cli budget --list                     # 全部台账
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict

from .quota_ledger import QuotaLedger

# batch2400 GLM 战训：支持环境变量覆盖台账路径——A/B 测试/多代理模式不共享封锁状态
DEFAULT_FILE = os.environ.get(
    "UNIVERSAL_SCRAPER_LEDGER",
    str(Path.home() / ".universal_scraper" / "domain_budget.json")
)


def _norm_domain(d: str) -> str:
    """域名归一化：小写 + 去 www. 前缀。审查修复：www.chinamoney.com 和
    chinamoney.com 曾被当成两个域名，封锁台账互通失效。"""
    d = (d or "").strip().lower()
    if d.startswith("www."):
        d = d[4:]
    return d

DEFAULT_HOURS = 24.0


def ledger(path: str | Path | None = None) -> QuotaLedger:
    return QuotaLedger(path or DEFAULT_FILE, windows={"domain": 86400})


def _meta(led: QuotaLedger) -> Dict[str, Dict]:
    return led.data.setdefault("budget_meta", {})


def mark(domain: str, hours: float = DEFAULT_HOURS, note: str = "",
         path: str | Path | None = None) -> Dict:
    """登记一次封锁/超预算事件。hours 随台账持久：check/list 按
    [最后事件 + hours] 计算，绝不回退到硬编码窗口。"""
    led = ledger(path)
    # OCR R131（H）：touch 内部曾先 save 一次（只含新 ts），meta 改完再 save
    # 一次——两写非原子，中间崩溃 = 封锁登记了新 ts 却丢了 hours（check 回退
    # DEFAULT_HOURS，冷却时长静默漂移）。改：内存同改后一次落盘
    led.data.setdefault("domain", {})[f"domain:{_norm_domain(domain)}"] = int(time.time())
    _meta(led)[_norm_domain(domain)] = {"hours": float(hours), "note": note[:200]}
    led.save()
    until = time.time() + hours * 3600
    return {"domain": domain, "cooldown_hours": hours,
            "until": time.strftime("%m-%d %H:%M", time.localtime(until))}


def check(domain: str, path: str | Path | None = None) -> Dict:
    led = ledger(path)
    # 审查修复：key 必须与 mark() 同用 _norm_domain——曾用原始域名查账，
    # www.example.com 永远查不到 example.com 的登记，幂等跳过失效、重复续期
    key = f"domain:{_norm_domain(domain)}"
    meta = _meta(led).get(_norm_domain(domain), {})
    hours = meta.get("hours", DEFAULT_HOURS)
    last_ts = led.last(key, dim="domain")
    remaining = int(last_ts + hours * 3600 - time.time())
    return {"domain": domain, "in_cooldown": remaining > 0,
            "remaining_sec": max(0, remaining),
            "cooldown_hours": hours, "note": meta.get("note", "")}


def listing(path: str | Path | None = None) -> Dict:
    led = ledger(path)
    meta = _meta(led)
    out: Dict[str, Dict] = {}
    for key, ts in led.data.get("domain", {}).items():
        d = key.replace("domain:", "", 1)
        m = meta.get(d, {})
        hours = m.get("hours", DEFAULT_HOURS)
        remaining = int(ts + hours * 3600 - time.time())
        out[d] = {"in_cooldown": remaining > 0,
                  "remaining_sec": max(0, remaining),
                  "cooldown_hours": hours,
                  "note": m.get("note", ""),
                  "last_event": time.strftime("%m-%d %H:%M", time.localtime(ts))}
    return out


# ---------------------------------------------------------------- 放行窗口探针
# gsxt 战训沉淀（2026-09）：行为评分型限流的配额形态是"封 10-25 分钟一档"，
# 唯一有效采集形态 = 低频探针抓放行窗口 → 窗口内榨干便宜动作。此前该形态
# 只存在于各任务现场手写的 probe.sh——泛化为 budget 的标准原语。

def probe_window(url: str, expect: str = "", every: float = 60.0, max_rounds: int = 0,
                 timeout: float = 15.0, log=None, max_seconds: float = 0.0) -> Dict[str, Any]:
    """低频探针：每 every 秒 GET 一次 url，判 OPEN / BLOCKED / EMPTY_200 / NET_ERROR。

    - OPEN：命中 expect 标记（站名/特定文案），或（未给标记时）200 且正文
      有实际内容且无拦截指纹
    - BLOCKED：detect_block 命中硬指纹（412/429/风控页…）
    - EMPTY_200：HTTP 200 但既无标记也无内容——**软封锁嫌疑**（行为评分型
      封锁最贵的一课：200 ≠ 放行，gsxt「200 空页」实测即封锁态）
    - NET_ERROR：网络层失败（出口变化/断网，别误判为封禁）

    找到首个 OPEN 即返回（exit 语义交 CLI）；max_rounds=0 时一直探到 OPEN。
    max_seconds>0 加总时长保险丝。审查三轮（C）：两个守卫都带真值——默认
    max_rounds=0 且 max_seconds=0 时双保险丝全关，永久封锁的站会无限探针
    挂死调用方。现强制无条件兜底保险丝（默认 6 小时），到点以当前状态返回。
    单轮网络/解码异常按 NET_ERROR 记账继续探——不炸出整个探针（否则累积的
    rounds_log 诊断全丢）。返回 {"state","rounds","elapsed_sec","detail","rounds_log"}。"""
    import time as _t
    from urllib.parse import urlsplit as _split
    from .core import make_http_client, smart_decode
    from .antibot import detect_block

    if _split(url or "").scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 探针 URL: {url!r}")

    def _lg(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    # 审查三轮实测修正：client 自带 max_retries=3 曾让单轮拖 20s+（保险丝只在
    # 轮间检查，粒度失真）。探针语义=快速失败——单轮 1 次尝试，粒度=timeout
    client = make_http_client({"min_interval": 0.0, "timeout": timeout,
                               "max_retries": 1,
                               "http_backend": "auto", "autothrottle": False})
    rounds_log: list = []
    t0 = _t.time()
    n = 0
    # 审查三轮（C）：无条件兜底保险丝——调用方显式给 max_seconds 用其值，
    # 否则 6 小时硬上限（探针每轮至少一个 timeout，挂死编排器是最坏后果）
    _fuse = t0 + (max_seconds if max_seconds and max_seconds > 0 else 6 * 3600.0)
    try:
        while _t.time() < _fuse:
            n += 1
            try:
                res = client.get(url)
                status = int(res.get("status", 0) or 0)
                text = smart_decode(res.get("body") or b"") if res.get("body") else (res.get("text") or "")
                bd = detect_block(status, text, res.get("headers"), url)
            except Exception as e:
                # 异常轮 = NET_ERROR：status 0 走既有分支，hint 带异常类型供诊断
                res = {"status": 0, "hint": f"{type(e).__name__}: {e}"}
                status = 0
                text = ""
                bd = {"kind": "exception"}
            hard_kinds = ("captcha", "anti_bot", "banned", "waf", "cloudflare",
                          "verify", "rate_limit", "session_flagged")
            _content_healthy = (200 <= status < 300 and len(text.strip()) >= 4096)
            if bd["kind"] == "login" and not _content_healthy:
                # 登录墙 ≠ 软封锁（审查 P2）：处方完全不同——登录即可见。
                # 且仅当内容不健康时才判 LOGIN（审查 P1：健康首页导航带 /login
                # 链接会把 login 判型带出来，绝不能让用户去"登录"一个能读的站）
                st = "LOGIN"
            elif 200 <= status < 300 and bd["kind"] not in hard_kinds:
                if expect:
                    st = "OPEN" if expect in text else "NO_MATCH"
                else:
                    st = "OPEN" if len(text.strip()) >= 4096 else "EMPTY_200"
            elif status == 0:
                st = "NET_ERROR"
            else:
                st = "BLOCKED"
            row = {"round": n, "state": st, "status": status, "kind": bd["kind"],
                   "bytes": len(text or ""), "at": _t.strftime("%H:%M:%S")}
            if st == "NET_ERROR" and res.get("hint"):
                row["hint"] = res.get("hint")   # 客户端已给出的方向性提示，透传
            rounds_log.append(row)
            _lg(f"🔍 探针第{n}轮: {st}（HTTP {status}，{row['bytes']}B，{row['at']}）")
            if st == "OPEN":
                return {"state": st, "rounds": n, "elapsed_sec": round(_t.time() - t0, 1),
                        "detail": ("expect 标记命中" if expect else "200 有内容"),
                        "rounds_log": rounds_log}
            if max_rounds and n >= max_rounds:
                _hint = {"LOGIN": "需要登录后才能看到内容——先走登录态再探",
                         "EMPTY_200": "200 空页=软封锁嫌疑，等窗口或换通道",
                         "NET_ERROR": "网络层失败——查出口/系统代理（scripts/doctor.py）"}.get(st, "")
                return {"state": st, "rounds": n, "elapsed_sec": round(_t.time() - t0, 1),
                        "detail": (f"{max_rounds} 轮未探到放行窗口（最后: {st}）"
                                   + (f"；{_hint}" if _hint else "")),
                        "rounds_log": rounds_log}
            if max_seconds and (_t.time() - t0) >= max_seconds:
                # 总时长保险丝：调用方设了就一定返回（精度 ±1 个 every 周期）
                return {"state": st, "rounds": n, "elapsed_sec": round(_t.time() - t0, 1),
                        "detail": f"max_seconds={max_seconds}s 保险丝触发（最后: {st}）",
                        "rounds_log": rounds_log}
            _t.sleep(max(1.0, every))
        # 审查四轮（C）：兜底保险丝从 while 条件退出时曾落到隐式 return None——
        # 调用方按状态字典解包必崩。以最后一轮状态返回
        _last = rounds_log[-1]["state"] if rounds_log else "NET_ERROR"
        return {"state": _last, "rounds": n, "elapsed_sec": round(_t.time() - t0, 1),
                "detail": f"总时长保险丝触发（{_t.strftime('%H:%M:%S')}，最后: {_last}）——"
                          "未探到放行窗口",
                "rounds_log": rounds_log}
    finally:
        # client 持连接池（requests.Session/HTTPAdapter 或 curl_cffi 句柄），必须关闭；
        # CurlCffiClient 无 close，getattr 守卫
        _close = getattr(client, "close", None)
        if callable(_close):
            try:
                _close()
            except Exception:
                pass
