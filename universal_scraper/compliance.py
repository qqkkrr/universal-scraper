#!/usr/bin/env python3
"""🛡️ 合规工具箱（R22 审查纠偏：本模块当前未被主执行链接入——真实生效的合规
在各执行器内：engine_v3 的 anti.respect_robots、fetchers 的 robots 检查、
core 的域名封锁台账。此处的 AdaptiveThrottle 以 core.py 内的同名实现为准。
保留本模块作为独立可复用的工具集；接线前请勿把本文档当作"已自动生效"的依据）。

三个能力：
1. AdaptiveThrottle —— 动态限速（Scrapy AutoThrottle 思路；生效版本在 core.py）
2. ComplianceGate —— 独立可调用的合规检查（robots.txt + 域名封锁台账 + 证据留存）
3. DeliveryReport —— 交付审计报告生成（数据量/字段完整率/来源/证据）
"""
from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


# ============================================================
# 1. AdaptiveThrottle（Scrapy AutoThrottle 思路）
# ============================================================
class AdaptiveThrottle:
    """根据服务器响应时间自适应调节请求间隔。

    规则：
    - 初始间隔 = min_interval 配置值
    - 响应快（< target_latency）→ 间隔逐步缩小到 min_interval
    - 响应慢（> target_latency * 2）→ 间隔逐步放大到 max_interval
    - 429/403 → 间隔立即翻倍
    - 线程安全

    用法:
        at = AdaptiveThrottle(min_interval=1.0, max_interval=30.0)
        at.wait()             # 在每次请求前调用（内部 sleep + 更新间隔）
        at.record(latency_s)  # 在每次响应后调用
    """

    def __init__(self, min_interval: float = 1.0, max_interval: float = 30.0,
                 target_latency: float = 3.0, adjustment_factor: float = 0.5):
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.target_latency = target_latency
        self.adjustment_factor = adjustment_factor
        self._current = min_interval
        self._lock = threading.Lock()

    @property
    def current_interval(self) -> float:
        with self._lock:  # OCR R131（L）：与 wait/record 统一持锁读
            return self._current

    def wait(self):
        with self._lock:
            w = self._current
        if w > 0:
            time.sleep(w)

    def record(self, latency_sec: float, status: int = 200):
        """记录一次响应的延迟和状态码，自适应调整下次间隔。"""
        with self._lock:
            if status in (429, 403):
                # 被限流/拒绝：立即翻倍
                self._current = min(self._current * 2, self.max_interval)
                return
            # 正常响应：根据延迟调整
            if latency_sec < self.target_latency:
                # 服务器响应快 → 可以略微加快
                self._current = max(self.min_interval, self._current * (1 - self.adjustment_factor * 0.1))
            else:
                # 服务器响应慢 → 稍微放慢
                self._current = min(self.max_interval, self._current * (1 + self.adjustment_factor * 0.2))


# ============================================================
# 2. ComplianceGate（robots.txt + 封锁台账 + 证据留存）
# ============================================================
class ComplianceGate:
    """每次请求前的合规检查门。整合 robots.txt / domain_budget / 证据留存。"""

    def __init__(self, evidence_dir: Optional[str] = None, respect_robots: bool = True):
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.respect_robots = respect_robots
        self._robots = None
        self._budget_warned: set = set()  # 台账检查失败的域只告警一次（防每请求刷屏）
        # OCR R131（M）：多 worker 并发 check 时 _robots/_budget_warned 的
        # check-then-act 无锁——加轻量锁串行化（检查本身是纯内存操作）
        import threading as _th
        self._gate_lock = _th.Lock()
        if self.evidence_dir:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def _get_robots(self):
        if self._robots is None and self.respect_robots:
            try:
                from .robots import RobotsTxt
                self._robots = RobotsTxt()
            except Exception as e:
                self._robots = False  # 标记为"不可用"
                # 审查修复：初始化失败曾无声——robots 合规检查被永久禁用而调用方
                # 毫不知情。失败后 _robots=False 不再重试，此告警天然只打一次
                print(f"⚠️ compliance: robots.txt 初始化失败（{type(e).__name__}: {e}）"
                      f"——robots 检查已禁用", file=sys.stderr, flush=True)
        return self._robots or None

    def check(self, url: str) -> Dict[str, Any]:
        """请求前调用。返回 {"allowed": bool, "reason": str}。"""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.hostname or ""
        _crawl_delay = 0.0

        # 1) robots.txt
        # OCR R131（M）：_get_robots 的 check-then-act 同样入锁（仅首次构造走慢路径）
        with self._gate_lock:
            robots = self._get_robots()
        if robots:
            allowed = robots.allowed(url)
            if not allowed:
                return {"allowed": False, "reason": "robots.txt Disallow"}
            # 审查对标（Crawlee robots representation）：Crawl-delay 透传给调用方——
            # 采集器可据此把该域 min_interval 提到服务端要求（原解析了却无人消费）
            try:
                _cd = robots.crawl_delay(url)
            except Exception:
                _cd = 0.0
            # 审查八轮（LOW）：此处曾 `if _cd > 0: return {"allowed": True, ...}` 提前
            # 返回——robots 里带 Crawl-delay 的域会**跳过下面的域名冷却台账检查**
            # （已封禁的域照放行，与"合规门"职责矛盾）。改为只记值、继续走冷却检查，
            # 最终 allowed 结果里带上 crawl_delay。
            _crawl_delay = float(_cd or 0.0)

        # 2) 域名封锁台账
        try:
            from .domain_budget import check
            bc = check(domain)
            if bc.get("in_cooldown"):
                return {"allowed": False,
                        "reason": f"域名冷却中（剩 {bc['remaining_sec']//3600}h，{bc.get('note','')}）"}
        except Exception as e:
            # 审查修复（HIGH）：台账检查炸了不再无声放行——冷却封锁被静默禁用，
            # 用户却以为还在生效。降级为放行但大声告警（每域一次，防刷屏）；
            # 不 fail-closed：台账文件损坏不应打死整个采集
            with self._gate_lock:  # OCR R131（M）：check-then-add 串行化防双份告警
                _first = domain not in self._budget_warned
                self._budget_warned.add(domain)
            if _first:
                print(f"⚠️ compliance: 域名封锁台账检查失败（{type(e).__name__}: {e}）"
                      f"——冷却封锁临时失效，请检查台账文件", file=sys.stderr, flush=True)

        _ret: Dict[str, Any] = {"allowed": True, "reason": ""}
        if _crawl_delay > 0:
            _ret["crawl_delay"] = _crawl_delay     # 审查八轮：透传（不再提前 return）
        return _ret

    def record_evidence(self, url: str, kind: str, content: str):
        """留证据文件（kind: http_block / captcha / waf / network_fail）。"""
        if not self.evidence_dir:
            return
        # OCR R131（M）：截断到 60 字符曾让同前缀 URL 互覆证据——拼短哈希保唯一。
        # 审查三轮（H）：kind 同样清洗——防拼接路径成分越出证据目录
        import hashlib as _hl
        safe = re.sub(r"[^0-9A-Za-z._\-]+", "_", url)[:60]
        _ksafe = re.sub(r"[^0-9A-Za-z._\-]+", "_", kind or "ev")[:24]
        _tag = _hl.md5(url.encode(), usedforsecurity=False).hexdigest()[:8]
        fp = self.evidence_dir / f"evidence_{_ksafe}_{safe}.{_tag}.txt"
        try:
            fp.write_text(f"URL: {url}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                          f"类型: {kind}\n---\n{content[:5000]}", encoding="utf-8")
        except Exception as e:
            # 审查修复（MEDIUM）：证据写失败必须可见——磁盘满/权限错时合规证据
            # 已丢，调用方却以为存上了
            print(f"⚠️ compliance: 证据写入失败 {fp.name}（{type(e).__name__}: {e}）",
                  file=sys.stderr, flush=True)


# ============================================================
# 3. DeliveryReport（交付审计报告自动生成）
# ============================================================
def generate_delivery_report(task_dir: str | Path, task_name: str = "",
                             source_summary: str = "") -> Path:
    """任务完成后自动生成交付审计报告 report.md。

    审计维度：数据文件清单、记录数、字段完整率、证据文件、时间口径、来源声明。
    """
    from .verify import verify_dir
    root = Path(task_dir)
    audit = verify_dir(str(root), log=lambda *a: None)

    lines = [
        f"# 交付报告 · {task_name or root.name}",
        f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"## 审计结论: {audit.get('verdict', '?')}",
        f"- 数据文件: {len(audit.get('files', []))} 个",
        f"- 总记录数: {audit.get('total_records', 0)}",
        f"- 证据文件: {len(audit.get('evidence', {}).get('evidence_files', []))} 个",
        f"- summary.json: {'✓' if audit.get('evidence', {}).get('summary_json') else '✗'}",
        "",
    ]
    if audit.get("missing_evidence_refs"):
        lines.append(f"⚠️ 缺失证据: {audit['missing_evidence_refs']}")
    for f in audit.get("files", []):
        status = f.get("error") or f"{f.get('records', 0)} 条, 完整率 {f.get('field_complete_rate', '?')}"
        lines.append(f"  · {f.get('file', '?')}: {status}")
    lines.append("")
    lines.append("---")
    # OCR R131（M）：source_summary 形参曾声明却从不进报告——审计报告缺"来源声明"段
    if source_summary:
        lines.append(f"## 来源声明\n{source_summary}")
        lines.append("")
    lines.append("本报告由 universal-scraper 自动生成（verify_dir + delivery_report）。")

    report_path = root / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path

