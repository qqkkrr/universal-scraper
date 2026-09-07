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
from pathlib import Path
from typing import Dict, List, Optional


class BatchQueue:
    """队列文件格式: [{id, text, status: pending|done|failed|blocked, attempts, result}]"""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        if not self.path.exists():
            raise FileNotFoundError(f"队列文件不存在: {self.path}（格式见模块 docstring）")
        self.items: List[Dict] = json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def next(self) -> Optional[Dict]:
        """下一个 pending。batch1700 战训：priority 字段小的优先（缺省=文件顺序）；
        富元数据（result/attempts/自定义字段）原样保留，agent 不必再自建状态文件。"""
        pend = [it for it in self.items if it.get("status") == "pending"]
        if not pend:
            return None
        has_prio = any("priority" in it for it in pend)
        if has_prio:
            return min(pend, key=lambda it: (float(it.get("priority", 100)),))
        return pend[0]

    def mark(self, item_id, status: str, result: str = "") -> Dict:
        """状态机：done/failed/blocked/nodata/pending/retry。

        nodata（batch1700 战训）：与 failed 严格区分——数据本身不存在于公开渠道
        （已用 WebSearch 交叉验证过），不是爬取失败，不值得修工具重试。
        retry：failed/blocked → pending 重置（attempts 保留累计）。"""
        terminal = ("done", "failed", "blocked", "nodata", "pending", "retry")
        if status not in terminal:
            raise ValueError(f"非法状态: {status}（可用: {terminal}）")
        for it in self.items:
            if str(it.get("id")) == str(item_id):
                if status == "retry":
                    if it.get("status") not in ("failed", "blocked", "nodata"):
                        raise ValueError(f"retry 仅用于 failed/blocked/nodata，当前 {it.get('status')}")
                    it["status"] = "pending"
                else:
                    it["status"] = status
                    it["attempts"] = int(it.get("attempts", 0)) + (1 if status != "pending" else 0)
                it["result"] = result[:500] or it.get("result", "")
                self._save()
                return it
        raise KeyError(f"队列中无此 id: {item_id}")

    def status(self) -> Dict[str, int]:
        from collections import Counter
        c = Counter(it.get("status", "pending") for it in self.items)
        return {"total": len(self.items), "done": c.get("done", 0),
                "failed": c.get("failed", 0), "blocked": c.get("blocked", 0),
                "nodata": c.get("nodata", 0), "pending": c.get("pending", 0)}
