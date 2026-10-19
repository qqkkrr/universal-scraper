#!/usr/bin/env python3
"""v4 统一运行入口。

职责：
  - 接收 CrawlPlan（或 --config/--task 路径）；
  - 按 plan.profile 选择成熟执行后端（linear/queue）；
  - 保持 v2/v3 的全部行为与退出码契约不变。

这是绞杀式合并的“统一入口层”：前端、计划、版本号统一为 v4；
执行器暂以两个 profile 共存，后续可把 linear 编译到 queue 内核而不动本 API。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from . import plan as plan_mod
from .plan import CrawlPlan

ENGINE_VERSION = "v4"


class PlanError(ValueError):
    """计划类型/字段不合法。"""


def _check(plan: CrawlPlan) -> None:
    if not isinstance(plan, CrawlPlan):
        raise PlanError("v4.run 需要 CrawlPlan")
    if plan.schema_version != plan_mod.SCHEMA_VERSION:
        raise PlanError(f"不支持的 plan schema_version={plan.schema_version}")
    if plan.profile not in ("linear", "queue"):
        raise PlanError(f"未知 plan.profile={plan.profile!r}")
    if plan.origin == "config" and plan.profile != "linear":
        raise PlanError("origin=config 必须是 linear profile")
    if plan.origin == "task" and plan.profile != "queue":
        raise PlanError("origin=task 必须是 queue profile")


def run_plan(plan: CrawlPlan, **kwargs) -> Dict[str, Any]:
    """执行计划。kwargs 与对应执行后端签名一致。"""
    _check(plan)
    if plan.profile == "queue":
        from .engine_v3 import run_task
        _root_kw = kwargs.pop("task_dir", None)
        root = plan.task_dir or _root_kw
        if not root:
            raise PlanError("queue profile 缺少 task_dir")
        return run_task(Path(root), **kwargs)
    # 必须走 engine.run_config 包装器：它持有 v2/v3 共用的进程级运行锁，
    # 直接调 _run_config_impl 会绕过锁（WebUI 并发时预算互清零的老问题复发）。
    from .engine import run_config as _run_config
    base_dir = kwargs.get("base_dir")
    if base_dir is None and plan.base_dir:
        kwargs["base_dir"] = Path(plan.base_dir)
    return _run_config(plan.raw, **kwargs)


def run_config(config: Dict[str, Any], base_dir: Optional[Path] = None, **kwargs) -> Dict[str, Any]:
    """v4 的 --config 入口：先归一为 plan，再执行。"""
    return run_plan(plan_mod.from_config(config, base_dir=base_dir), **kwargs)


def run_task(task_dir: Path, **kwargs) -> Dict[str, Any]:
    """v4 的 --task 入口：先归一为 plan，再执行。"""
    return run_plan(plan_mod.from_task(Path(task_dir)), **kwargs)


def run(path: Path, **kwargs) -> Dict[str, Any]:
    """按路径自动识别（目录=任务包，文件=配置）并执行。"""
    return run_plan(plan_mod.load(Path(path)), **kwargs)
