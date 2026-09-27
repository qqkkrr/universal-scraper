#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api_session —— 多步 API 链执行器（R27 路线图落地；两轮考核反馈收敛的 P0）。

为"接口考古"类任务提供带护栏的一等公民执行器：单会话 cookie jar、请求预算硬闸、
最小间隔节流、逐请求 JSONL 审计（时间/方法/URL/状态/字节/耗时）、挑战壳冷却、
响应证据落盘（单文件 ≤2MB）。计划须是具体化的步骤列表（变量/循环由调用方先展开）。

计划格式（plan.json）：
{
  "name": "国家数据考核",
  "max_requests": 20, "min_gap": 3.0,
  "steps": [
    {"name": "会话首页", "method": "GET",  "url": "https://data.stats.gov.cn/", "save": "home.html"},
    {"name": "指标树",   "method": "GET",  "url": "https://data.stats.gov.cn/dg/.../queryIndexTreeAsync?pid=&code=1",
     "headers": {"X-Requested-With": "XMLHttpRequest"}, "save": "tree.json", "expect_json": true},
    {"name": "数值",     "method": "POST", "url": "https://data.stats.gov.cn/dg/.../stream/esData",
     "json": {"cid": "...", "indicatorIds": ["..."], "das": [{"text": "全国", "value": "000000000000"}],
              "showType": "1", "dts": ["202508MM-202608MM"], "rootId": "..."},
     "save": "values.json", "expect_json": true}
  ]
}
CLI：python3 -m universal_scraper.cli session --plan plan.json --out 证据目录 \
        [--max-requests 20] [--min-gap 3.0]
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core import MaxRequestsExceeded, make_http_client, request_budget, set_request_budget
from .diagnose import looks_like_challenge
from .log import Logger

MAX_SAVE_BYTES = 2 * 1024 * 1024  # 单步证据落盘上限（资源约束）

# 总量风险闸门（裁判文书网战训 P0：5 万次请求打政府站 → 封号）。
# 政府司法站阈值降一个数量级：被拦的代价是账号/出口全灭，不是慢。
SCALE_GATE_DEFAULT = 2000
SCALE_GATE_GOV = 500
GOV_HOST_MARKS = (".gov.cn", ".court.", "court.gov", "zxgk", "wenshu")


def run_session(plan: Dict[str, Any], out_dir: Path, max_requests: Optional[int] = None,
                min_gap: Optional[float] = None, timeout: float = 30.0,
                log_file: Optional[Path] = None,
                risk_accepted: bool = False) -> Dict[str, Any]:
    name = str(plan.get("name", "session"))
    # 审查修复 P0-2：name 流入审计/日志文件名，白名单化（中英文/数字/_-）防路径注入
    import re as _re
    name = _re.sub(r"[^\w\u4e00-\u9fff-]", "_", name)[:60] or "session"
    steps: List[Dict[str, Any]] = plan.get("steps") or []
    if not steps:
        return {"name": name, "error": "计划无 steps"}
    # 总量风险闸门（P0-B）：步数超阈值须显式 risk_accepted 才放行
    # OCR R131（M）：标记曾对整条 URL 子串匹配——查询参数里出现 "court" 即
    # 误触政务阈值。只在 hostname 里判
    from urllib.parse import urlsplit as _us
    _gov = any(any(m in ((_us(str(s.get("url", ""))).hostname or "").lower())
                      for m in GOV_HOST_MARKS) for s in steps)
    _threshold = SCALE_GATE_GOV if _gov else SCALE_GATE_DEFAULT
    # 审查修复 P2：plan 内 "risk_accepted": "false" 字符串曾真值绕过闸门
    _plan_ok = plan.get("risk_accepted")
    _plan_ok = (_plan_ok is True) if isinstance(_plan_ok, bool) else str(_plan_ok).lower() in ("1", "true", "yes")
    if len(steps) > _threshold and not (risk_accepted or _plan_ok):
        return {"name": name, "error": (
            f"总量风险闸门：{len(steps)} 步超过"
            f"{'政府/司法站' if _gov else '默认'}阈值 {_threshold}——预估总请求 ≥该数，"
            f"被封锁/封号风险高。确认规模合理后加 --risk-accepted 或计划内 "
            f"\"risk_accepted\": true 放行；否则请缩小范围或分批执行"), "refused": True}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = Logger(log_file=log_file or (out_dir / f".session_{name}.log"))
    # 预算 + 节流与 `run --max-requests` 同源：core 计数器经 client._throttle 累计，
    # 超限抛 MaxRequestsExceeded（BaseException，穿透一切重试网）
    set_request_budget(int(max_requests or plan.get("max_requests") or 0))
    client = make_http_client({"min_interval": float(min_gap if min_gap is not None
                               else plan.get("min_gap", 2.5)), "timeout": timeout,
                               "http_backend": "auto"})
    audit_path = out_dir / f"requests_{name}.jsonl"

    def audit(rec: Dict[str, Any]) -> None:
        # 收官三轮（审查 M）：跨运行追加曾把多次运行混在一个文件里无分隔——
        # 每次运行首条写分隔标记（ts+计划名），audit 与单次运行可对账
        if not audit_path.exists() or getattr(audit, "_run_marked", False) is False:
            with audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"_run": name, "t": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "steps": len(steps)}, ensure_ascii=False) + "\n")
            audit._run_marked = True
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    results: List[Dict[str, Any]] = []
    stopped = None
    cooled = 0
    t0 = time.time()
    from .antibot import ShortPageStreak
    streak = ShortPageStreak()  # 必须在循环外实例化（审查 P2：曾每步新建，
    # 连击计数永远到不了 3，"连续同长短页硬停机"保护是死代码）
    for i, step in enumerate(steps, 1):
        sname = str(step.get("name") or f"step{i}")
        method = str(step.get("method", "GET")).upper()
        url = step.get("url") or ""
        if not url:
            results.append({"step": sname, "error": "缺 url"})
            continue
        kw: Dict[str, Any] = {"headers": step.get("headers") or {}}
        if step.get("params"):
            kw["params"] = step["params"]
        if step.get("json") is not None:
            kw["json_data"] = step["json"]
        elif step.get("data") is not None:
            kw["data"] = step["data"]

        def _attempt(note: str = "") -> Dict[str, Any]:
            t1 = time.monotonic()
            # 注意参数序：CurlCffiClient.request(url, method=...)——url 在前
            res = client.request(url, method=method, **kw)
            lat = round(time.monotonic() - t1, 2)
            audit({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "step": sname, "method": method,
                   "url": url, "status": res.get("status", 0),
                   "bytes": len(res.get("body") or b""), "elapsed": lat, "note": note})
            return res

        try:
            res = _attempt()
        except MaxRequestsExceeded:
            stopped = "max_requests"
            logger.error(f"🛑 请求预算硬闸触发于步骤「{sname}」——已停，逐次证据见 {audit_path.name}")
            break

        status = res.get("status", 0)
        text = res.get("text", "") or ""
        # 阻断检测（裁判文书网战训 P0-A）：分两类处置（审查修复 P2-2/P1-4）——
        #   可恢复挑战类（cloudflare/verify/captcha/waf）→ 冷却 60s 重试一次；
        #   封禁/拒绝类（banned/anti_bot/session_flagged/rate_limit/403/429/52x）
        #   → 无论状态码立即硬停机（此前要求 status==200，403 封禁曾继续跑并把
        #   封禁页写进证据文件）
        from .antibot import detect_block as _db
        _bd = _db(status, text, {k.lower(): str(v) for k, v in (res.get("headers") or {}).items()}, url)
        _kind = _bd["kind"]
        _challenge_kinds = ("cloudflare", "verify", "captcha", "waf")
        _hard_kinds = ("banned", "anti_bot", "session_flagged", "rate_limit",
                       "403", "429", "502", "503")
        if _kind in _hard_kinds:
            stopped = "blocked"
            logger.error(f"⛔ 步骤「{sname}」判定为封禁/拦截[{_kind}]：{_bd['detail']}"
                         "——硬停机（宁可不写，也不能把封禁页写进数据/证据）")
            break
        _json_ok = True
        if status == 200 and step.get("expect_json"):
            try:
                json.loads(text)
            except Exception:
                _json_ok = False
        # 连击启发式只喂"没按声明产出 JSON 的响应"——定长小 JSON API 是合法形态；
        # 正常步骤重置连击（审查修复：曾只喂坏页，"连续"退化成"累计"假停机）
        if _json_ok:
            streak.reset()
        elif streak.feed(len(res.get("body") or b"")):
            stopped = "blocked"
            logger.error("⛔ 连续 3 页响应极短且长度一致且均非声明 JSON——疑似封禁页连发，硬停机")
            break
        # 挑战壳冷却：可恢复挑战类 → 冷却 60s 重试一次（两次尝试都进审计）
        if _kind in _challenge_kinds or looks_like_challenge(
                status, text, str((res.get("headers") or {}).get("content-type", ""))):
            cooled += 1
            logger.warn(f"⚠️ 步骤「{sname}」命中挑战类拦截[{_kind}]，冷却 60s 后重试一次")
            time.sleep(60)
            try:
                res = _attempt("challenge-retry")
            except MaxRequestsExceeded:
                stopped = "max_requests"
                logger.error("🛑 冷却重试前预算耗尽——停止")
                break
            status = res.get("status", 0)
            text = res.get("text", "") or ""
            _bd2 = _db(status, text, {k.lower(): str(v) for k, v in (res.get("headers") or {}).items()}, url)
            if _bd2["kind"] not in ("none", "login", "http_error"):
                stopped = "blocked"
                # OCR R131（H）：blocked.json 曾引用第一次尝试的 _bd/_kind——
                # 真正触发硬停的是重试这轮，证据必须对准最新判型
                _bd, _kind = _bd2, _bd2["kind"]
                logger.error(f"⛔ 冷却重试后仍为拦截页[{_bd2['kind']}]——硬停机")
                break
            # 审查修复（P2，R5）：挑战应答曾在冷却重试前喂 streak 且成功恢复
            # 后从不重置——3 步都靠冷却恢复的会话会被误判"封禁页连发"硬停
            streak.reset()

        rec: Dict[str, Any] = {"step": sname, "status": status, "bytes": len(res.get("body") or b"")}
        if not res.get("ok") and status == 0:
            # 网络层失败：错误详情必须进审计（排查限速/代理/解析错全靠它）
            rec["error"] = str(res.get("text", ""))[:200]
        save = step.get("save")
        if save:
            # 审查修复 P0-2：save 路径禁越界——绝对路径/.. 曾可任意写文件
            fp = (out_dir / save).resolve()
            if not (str(fp).startswith(str(out_dir.resolve()) + "/") or fp.parent == out_dir.resolve()):
                rec["save_error"] = f"save 路径越界已拒绝: {save!r}"
                logger.warn(f"⚠️ 步骤「{sname}」save 路径越界已拒绝: {save!r}")
            else:
                fp.parent.mkdir(parents=True, exist_ok=True)
                body = res.get("body") or b""
                fp.write_bytes(body[:MAX_SAVE_BYTES])
                rec["saved"] = str(fp)
        if step.get("expect_json") and status == 200:
            try:
                json.loads(text)
                rec["json_ok"] = True
            except Exception:
                rec["json_ok"] = False
                rec["head"] = text[:120]
                logger.warn(f"⚠️ 步骤「{sname}」声明 expect_json 但响应非 JSON（防挑战壳假成功）")
        results.append(rec)
        logger.info(f"[{i}/{len(steps)}] {sname}: HTTP {status} {rec.get('bytes', 0)}B"
                    + (f" → {rec['saved']}" if rec.get("saved") else ""))

    budget = request_budget()
    # 审查修复 P0-1：聚合 ok——任何步骤网络失败/HTTP≥400/expect_json 为假 → 不算成功。
    # 此前全失败链 exit 0，验收门禁会把废数据当成品
    ok = (stopped is None and bool(results)
          and all(r.get("status") == 200 and r.get("json_ok", True) and not r.get("error")
                  and not r.get("save_error") for r in results))
    if stopped == "blocked" and out_dir:
        try:
            (Path(out_dir) / "blocked.json").write_text(json.dumps(
                {"blocked": True, "kind": f"session_{_kind if stopped == 'blocked' else ''}",
                 "url": url, "detail": _bd.get("detail", ""), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "steps_done": len(results)}, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            # 审查修复 P1：证据写盘曾静默——日志行就是封禁证据的兜底
            logger.error(f"blocked.json 写盘失败（{type(e).__name__}: {e}）——"
                         f"请引用上方⛔日志行作封禁证据")
    logger.info(f"会话结束: 步骤 {len(results)}/{len(steps)} | 请求 {budget['used']}"
                + (f"/{budget['limit']}" if budget["limit"] else "")
                + f" | 冷却 {cooled} | 用时 {time.time() - t0:.0f}s"
                + (" | ⚠️ 未设请求上限——无人值守跑请加 --max-requests" if not budget["limit"] else ""))
    return {"name": name, "steps_done": len(results), "total_steps": len(steps),
            "stopped": stopped, "cooled": cooled, "budget": budget, "ok": ok,
            "audit": str(audit_path), "results": results}
