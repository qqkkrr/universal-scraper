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

import fcntl
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

# running 超过该秒数未更新 → 视为代理崩溃，任务自动回 pending（batch2400）
STALE_RUNNING_SEC = 1800


def _as_ts(v) -> float:
    """running_ts 容错取值（batch2200 战训："yesterday" 字符串曾让调度器每次
    next() 都 TypeError 崩溃；JS Date.now() 毫秒曾让 stale 回收永不生效）。
    非法/缺失 → 0（最旧，优先回收）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f > 1e12:        # 毫秒时间戳 → 秒
        f /= 1000.0
    return f


def _prio(it) -> float:
    """priority 容错取值：None/非数字/NaN/inf → 缺省 100（"high" 字符串曾
    让 next() 裸栈并永久卡死整个队列）。"""
    try:
        f = float(it.get("priority", 100))
        if math.isnan(f) or math.isinf(f):
            raise ValueError
        return f
    except (TypeError, ValueError):
        return 100.0


class BatchQueue:
    """队列文件格式: [{"id", "text", "status", "attempts", "result", ...}]"""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        if not self.path.exists():
            raise FileNotFoundError(f"队列文件不存在: {self.path}（格式见模块 docstring）")
        try:
            self.items = json.loads(self.path.read_text(encoding="utf-8-sig", errors="replace"))
        except json.JSONDecodeError as e:
            raise ValueError(f"队列文件不是合法 JSON（可能被截断）: {self.path} ({e})") from e
        if not isinstance(self.items, list):
            raise ValueError(f"队列文件顶层必须是 list，实际 {type(self.items).__name__}")

    def _save(self):
        # 边界复现修复：固定 .json.tmp 在并发/双实例下互踩；改 mkstemp（不可预测名）
        # OCR R131（M）：序列化失败曾泄漏 .tmp 文件——finally 兜底清理
        fd, tmpname = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self.items, ensure_ascii=False, indent=1))
            os.replace(tmpname, self.path)
        except BaseException:
            try:
                os.unlink(tmpname)
            except OSError:
                pass
            raise

    def _locked_mutate(self, fn):
        """审查修复 P1：所有读-改-写统一走 flock + 锁内重读——claim 是锁的，
        mark/next 的回收却曾裸写陈旧内存视图：把别人刚标 done 的项冲回 pending
        （双跑）或把活 running 回收。fn(items) 内做变更并 _save，返回其结果。
        可重入：claim→next 会二次进入，同进程已持锁时直接复用（E2E 曾死锁于此）。"""
        if getattr(self, "_in_locked", False):
            return fn(self.items)
        lock_path = self.path.with_suffix(".lock")
        with open(lock_path, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                # R19 修复：与 __init__ 同样的 errors="replace"——手编队列文件里的
                # 非法字节曾让构造成功而所有 mutating 命令在锁内重读时崩溃
                self.items = json.loads(self.path.read_text(encoding="utf-8-sig", errors="replace"))
                self._in_locked = True
                try:
                    return fn(self.items)
                finally:
                    self._in_locked = False
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def next(self) -> Optional[Dict]:
        """下一个 pending。batch1700：priority 小者优先（缺省/坏值=100）；
        富元数据原样保留。batch2400（多代理）：过期 running 自动回收
        （agent 崩溃不丢任务）；非 dict 元素跳过。"""
        now = time.time()

        def _recycle(items):
            changed = False
            for it in items:
                if not isinstance(it, dict):
                    continue
                if it.get("status") == "running" and now - _as_ts(it.get("running_ts", 0)) > STALE_RUNNING_SEC:
                    it["status"] = "pending"
                    changed = True
            if changed:
                self._save()
            return changed

        self._locked_mutate(_recycle)
        # R37 修复：status() 把缺 status 的项计为 pending，next() 却要求字面量
        # "pending"——无 status 队列文件在汇总里显示 pending=N 但永远取不到（陷阱环）
        pend = [it for it in self.items
                if isinstance(it, dict) and it.get("status", "pending") == "pending"]
        if not pend:
            return None
        if any("priority" in it for it in pend):
            return min(pend, key=_prio)
        return pend[0]

    def claim(self) -> Optional[Dict]:
        """多代理模式：原子领取（文件锁 + 锁内重读最新账本 → 标 running）。
        崩溃的任务由 next() 的 stale 回收自动归还。
        锁文件 = 队列文件改 .lock 后缀（Path API，无字符串拼接）。"""
        def _claim(items):
            it = self.next()
            if not it:
                return None
            it["status"] = "running"
            it["running_ts"] = int(time.time())
            # 审查修复 P1：claim 曾不增 attempts——多代理模式重试上限治理失效
            it["attempts"] = int(it.get("attempts", 0)) + 1
            self._save()
            return it

        return self._locked_mutate(_claim)

    def touch(self, item_id) -> Dict:
        """续租 running 心跳（审查 P2，R6）：长任务（>30min）曾无任何续期手段，
        被 next() 的 stale 回收误判为崩溃、双代理重复执行。仅 running 态可续。"""
        import time as _time

        def _touch(items):
            for it in items:
                if str(it.get("id")) == str(item_id):
                    if it.get("status") != "running":
                        raise ValueError(f"touch 仅用于 running 态（当前 {it.get('status')}）")
                    it["running_ts"] = _time.time()
                    self._save()
                    return it
            raise KeyError(f"队列中无此 id: {item_id}")

        return self._locked_mutate(_touch)

    def mark(self, item_id, status: str, result: str = "") -> Dict:
        """状态机：done/failed/blocked/nodata/pending/retry/running。

        nodata（batch1700 战训）：与 failed 严格区分——数据本身不存在于公开渠道
        （已用 WebSearch 交叉验证过），不是爬取失败，不值得修工具重试。
        retry：failed/blocked/nodata/running → pending 重置（attempts 保留累计）。
        多代理（batch2400）：claim 产生的 running 由终态覆盖；崩溃的任务由
        next() 的 stale 回收归还。"""
        # 审查修复 P1：running 态只能由 claim 设置——mark("running") 曾剥离
        # running_ts（_as_ts(0) → 立即被 next() 回收）
        terminal = ("done", "failed", "blocked", "nodata", "pending", "retry")
        if status not in terminal:
            raise ValueError(f"非法状态: {status}（可用: {terminal}；running 只能由 claim 设置）")

        def _mark(items):
            for it in items:
                if not it.get("id"):
                    continue  # 收官九轮：缺 id 项 str()→"None" 曾被 mark("None") 误匹配
                if str(it.get("id")) == str(item_id):
                    was_pending = it.get("status") == "pending"
                    if status == "retry":
                        if it.get("status") not in ("failed", "blocked", "nodata", "running"):
                            raise ValueError(f"retry 仅用于 failed/blocked/nodata/running，"
                                             f"当前 {it.get('status')}")
                        it["status"] = "pending"
                    else:
                        it["status"] = status
                        # 仅从 pending 出发才计一次 attempt（claim 已计，终态不再重复）
                        if status != "pending" and was_pending:
                            it["attempts"] = int(it.get("attempts", 0)) + 1
                    # result 永远如实覆盖（空即空）——failed 项挂旧成功文案会误导续跑
                    it["result"] = result[:500]
                    it.pop("running_ts", None)   # 离开 running 态即清理
                    self._save()
                    return it
            raise KeyError(f"队列中无此 id: {item_id}")

        return self._locked_mutate(_mark)

    def status(self) -> Dict[str, int]:
        from collections import Counter
        # 与 next/_recycle/mark 同口径：跳过手改队列混入的非 dict 项
        c = Counter(it.get("status", "pending") for it in self.items
                    if isinstance(it, dict))
        # OCR R131（L）：total 曾算非 dict 垃圾项——与分桶同口径只数 dict 项
        _n_items = sum(1 for it in self.items if isinstance(it, dict))
        return {"total": _n_items, "done": c.get("done", 0),
                "failed": c.get("failed", 0), "blocked": c.get("blocked", 0),
                "nodata": c.get("nodata", 0),
                "running": c.get("running", 0), "pending": c.get("pending", 0)}
