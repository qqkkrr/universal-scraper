#!/usr/bin/env python3
"""🗂️ 批量任务队列 runner（batch1401 战训：100 项实测任务靠 agent 手搓 tasks.json）。

agent 的批处理状态机：队列文件 + 断点续跑 + 逐项状态。执行本体仍由 agent
（读任务文本→用 universal-scraper 干活），本模块只管"取下一项/记账/汇报"，
这样崩溃可续、跨会话可续。

用法:
  python3 -m universal_scraper.cli batch --queue tasks.json next          # 取下一 pending（打印全文+序号）
  python3 -m universal_scraper.cli batch --queue tasks.json done 1401 --result "usd=7.1023 ✓"
  python3 -m universal_scraper.cli batch --queue tasks.json fail 1415 --result "登录墙,blocked"
  python3 -m universal_scraper.cli batch --queue tasks.json status        # done/failed/pending 汇总
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
        """下一个 pending（按文件顺序）；全空返回 None。"""
        for it in self.items:
            if it.get("status") == "pending":
                return it
        return None

    def mark(self, item_id, status: str, result: str = "") -> Dict:
        if status not in ("done", "failed", "blocked", "pending"):
            raise ValueError(f"非法状态: {status}")
        for it in self.items:
            if str(it.get("id")) == str(item_id):
                it["status"] = status
                it["attempts"] = int(it.get("attempts", 0)) + (1 if status != "pending" else 0)
                it["result"] = result[:500]
                self._save()
                return it
        raise KeyError(f"队列中无此 id: {item_id}")

    def status(self) -> Dict[str, int]:
        from collections import Counter
        c = Counter(it.get("status", "pending") for it in self.items)
        return {"total": len(self.items), "done": c.get("done", 0),
                "failed": c.get("failed", 0), "blocked": c.get("blocked", 0),
                "pending": c.get("pending", 0)}
