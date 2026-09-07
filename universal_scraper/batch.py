#!/usr/bin/env python3
"""🗂️ 批量任务队列 runner（batch1401 战训：100 项实测任务靠 agent 手搓 tasks.json）。

agent 的批处理状态机：队列文件 + 断点续跑 + 逐项状态。执行本体仍由 agent
（读任务文本→用 universal-scraper 干活），本模块只管"取下一项/记账/汇报"，
这样崩溃可续、跨会话可续。

用法:
  python3 -m universal_scraper.cli batch --queue tasks.json next          # 取下一 pending（priority 小者优先）
  python3 -m universal_scraper.cli batch --queue tasks.json done 1401 --result "usd=7.1023 ✓"
  python3 -m universal_scraper.cli batch --queue tasks.json fail 1415 --result "登录墙"
  python3 -m universal_scraper.cli batch --queue tasks.json nodata 1419 --result "当日无披露,WebSearch已交叉验证"
  python3 -m universal_scraper.cli batch --queue tasks.json retry 1415   # failed/blocked/nodata → 重回队列
  python3 -m universal_scraper.cli batch --queue tasks.json status        # done/failed/blocked/nodata/pending 汇总
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Optional

# running 超过该秒数未更新 → 视为代理崩溃，任务自动回 pending（batch2400）
STALE_RUNNING_SEC = 1800


class BatchQueue:
    """队列文件格式: [{id, text, status: pending|done|failed|blocked, attempts, result}]"""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        if not self.path.exists():
            raise FileNotFoundError(f"队列文件不存在: {self.path}（格式见模块 docstring）")
        self.items = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(self.items, list):
            raise ValueError(f"队列文件顶层必须是 list，实际 {type(self.items).__name__}")

    def _save(self):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def next(self) -> Optional[Dict]:
        """下一个 pending。batch1700 战训：priority 字段小的优先（缺省=文件顺序）；
        富元数据（result/attempts/自定义字段）原样保留，agent 不必再自建状态文件。
        batch2400（多代理）：过期 running 自动回收回 pending（agent 崩溃不丢任务）。"""
        now = time.time()
        changed = False
        for it in self.items:
            if it.get("status") == "running" and now - it.get("running_ts", 0) > STALE_RUNNING_SEC:
                it["status"] = "pending"
                changed = True
        if changed:
            self._save()
        pend = [it for it in self.items if it.get("status") == "pending"]
        if not pend:
            return None
        has_prio = any("priority" in it for it in pend)
        if has_prio:
            return min(pend, key=lambda it: (float(it.get("priority", 100)),))
        return pend[0]

    def claim(self) -> Optional[Dict]:
        """多代理模式：原子地"取下一个 pending → 标 running"。
        崩溃的任务由 next() 的 stale 回收（STALE_RUNNING_SEC=30 分钟）自动归还。"""
        it = self.next()
        if not it:
            return None
        import time
        it["status"] = "running"
        it["running_ts"] = int(time.time())
        self._save()
        return it

    def mark(self, item_id, status: str, result: str = "") -> Dict:
        """状态机：done/failed/blocked/nodata/pending/retry/running。

        nodata（batch1700 战训）：与 failed 严格区分——数据本身不存在于公开渠道
        （已用 WebSearch 交叉验证过），不是爬取失败，不值得修工具重试。
        retry：failed/blocked/nodata/running → pending 重置（attempts 保留累计）。
        多代理（batch2400）：claim 产生的 running 由终态覆盖；崩溃的任务由
        next() 的 stale 回收归还。"""
        terminal = ("done", "failed", "blocked", "nodata", "pending", "retry", "running")
        if status not in terminal:
            raise ValueError(f"非法状态: {status}（可用: {terminal}）")
        for it in self.items:
            if str(it.get("id")) == str(item_id):
                was_pending = it.get("status") == "pending"
                if status == "retry":
                    if it.get("status") not in ("failed", "blocked", "nodata", "running"):
                        raise ValueError(f"retry 仅用于 failed/blocked/nodata/running，"
                                         f"当前 {it.get('status')}")
                    it["status"] = "pending"
                else:
                    it["status"] = status
                    # 审查修复：仅从 pending 出发才计一次 attempt（终态重复 mark
                    # 不再虚增轮次）
                    if status != "pending" and was_pending:
                        it["attempts"] = int(it.get("attempts", 0)) + 1
                # 审查修复：result 永远如实覆盖（空即空）——failed 项挂着旧的
                # 成功文案会误导断点续跑的 agent
                it["result"] = result[:500]
                it.pop("running_ts", None)   # 离开 running 态即清理
                self._save()
                return it
        raise KeyError(f"队列中无此 id: {item_id}")

    def status(self) -> Dict[str, int]:
        from collections import Counter
        c = Counter(it.get("status", "pending") for it in self.items)
        return {"total": len(self.items), "done": c.get("done", 0),
                "failed": c.get("failed", 0), "blocked": c.get("blocked", 0),
                "nodata": c.get("nodata", 0),
                "running": c.get("running", 0), "pending": c.get("pending", 0)}
