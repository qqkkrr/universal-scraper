#!/usr/bin/env python3
"""🛡️ 合规中间件 + 交付报告生成器——把已有的 Dormant 模块（robots/middleware/antibot）
串进主执行链，让"合规"从文档变成自动行为。

三个能力：
1. AdaptiveThrottle —— 动态限速（Scrapy AutoThrottle 思路：根据服务器响应速度自适应调节）
2. ComplianceGate —— 每次请求前的合规检查（robots.txt + 域名封锁台账 + 证据留存）
3. DeliveryReport —— 任务完成后自动生成交付审计报告（数据量/字段完整率/来源/证据）
"""
from __future__ import annotations

import re
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
        return self._current

    def wait(self):
        with self._lock:
            w = self._current
        if w > 0:
            import time as _t
            _t.sleep(w)

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
        self._checked_domains: set = set()
        if self.evidence_dir:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def _get_robots(self):
        if self._robots is None and self.respect_robots:
            try:
                from .robots import RobotsTxt
                self._robots = RobotsTxt()
            except Exception:
                self._robots = False  # 标记为"不可用"
        return self._robots or None

    def check(self, url: str) -> Dict[str, Any]:
        """请求前调用。返回 {"allowed": bool, "reason": str}。"""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.hostname or ""

        # 1) robots.txt
        robots = self._get_robots()
        if robots:
            allowed = robots.allowed(url)
            if not allowed:
                return {"allowed": False, "reason": "robots.txt Disallow"}

        # 2) 域名封锁台账
        try:
            from .domain_budget import check
            bc = check(domain)
            if bc.get("in_cooldown"):
                return {"allowed": False,
                        "reason": f"域名冷却中（剩 {bc['remaining_sec']//3600}h，{bc.get('note','')}）"}
        except Exception:
            pass

        return {"allowed": True, "reason": ""}

    def record_evidence(self, url: str, kind: str, content: str):
        """留证据文件（kind: http_block / captcha / waf / network_fail）。"""
        if not self.evidence_dir:
            return
        safe = re.sub(r"[^0-9A-Za-z._\-]+", "_", url)[:60]
        fp = self.evidence_dir / f"evidence_{kind}_{safe}.txt"
        try:
            fp.write_text(f"URL: {url}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                          f"类型: {kind}\n---\n{content[:5000]}", encoding="utf-8")
        except Exception:
            pass


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
    lines.append("本报告由 universal-scraper 自动生成（verify_dir + delivery_report）。")

    report_path = root / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path

