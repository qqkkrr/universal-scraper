#!/usr/bin/env python3
"""v4 统一计划层（CrawlPlan）。

设计目标（绞杀式合并第一/二阶段）：
  - 所有入口（--config / --task / 未来 --plan）先归一为 CrawlPlan；
  - v4 只暴露一个 run 入口，执行后端按 plan.profile 选择：
      * profile="linear" —— 复用成熟的 v2 线性执行器（配置语义零变化）
      * profile="queue"  —— 复用成熟的 v3 队列执行器（任务包语义零变化）
  - 后续阶段可在不改前端/IR 的前提下，把 linear 逐步编译到 queue 内核；
    在此之前绝不重写执行器，避免引入行为回归。

本模块只做“纯数据归一 + 编译”，不发起任何网络/文件写入。
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 4


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, list) else []


@dataclass
class CrawlPlan:
    """一次采集任务的归一化描述（v4 IR）。"""

    name: str
    origin: str                     # config | task
    profile: str                    # linear | queue
    schema_version: int = SCHEMA_VERSION
    seeds: List[str] = field(default_factory=list)
    source: Dict[str, Any] = field(default_factory=dict)
    extraction: Dict[str, Any] = field(default_factory=dict)
    pagination: Dict[str, Any] = field(default_factory=dict)
    detail: Dict[str, Any] = field(default_factory=dict)
    pipeline: List[Any] = field(default_factory=list)
    output: Dict[str, Any] = field(default_factory=dict)
    run: Dict[str, Any] = field(default_factory=dict)
    storage: Dict[str, Any] = field(default_factory=dict)
    rules: List[Any] = field(default_factory=list)
    parsers: Dict[str, Any] = field(default_factory=dict)
    base_dir: Optional[str] = None
    task_dir: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def features(self) -> List[str]:
        """供诊断/测试的能力标签（不参与执行）。"""
        feats: List[str] = []
        if self.origin == "config":
            feats.append("config")
            if self.pagination:
                feats.append("pagination")
            if self.run.get("iterate"):
                feats.append("iterate")
            if self.detail.get("enabled"):
                feats.append("detail")
            if self.source.get("sitemap"):
                feats.append("sitemap")
            if self.output.get("cache"):
                feats.append("cache")
        else:
            feats.append("task")
            if self.rules:
                feats.append("rules")
            if self.run.get("queue"):
                feats.append("queue")
            if self.storage:
                feats.append("storage")
            if self.detail.get("enabled"):
                feats.append("detail")
        return feats

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "origin": self.origin,
            "profile": self.profile,
            "features": self.features(),
            "seeds": list(self.seeds),
            "source": copy.deepcopy(self.source),
            "extraction": copy.deepcopy(self.extraction),
            "pagination": copy.deepcopy(self.pagination),
            "detail": copy.deepcopy(self.detail),
            "pipeline": copy.deepcopy(self.pipeline),
            "output": copy.deepcopy(self.output),
            "run": copy.deepcopy(self.run),
            "storage": copy.deepcopy(self.storage),
            "rules": copy.deepcopy(self.rules),
            "parsers": copy.deepcopy(self.parsers),
            "base_dir": self.base_dir,
            "task_dir": self.task_dir,
            "raw": copy.deepcopy(self.raw),
        }


def from_config(config: Dict[str, Any], base_dir: Optional[Path] = None) -> CrawlPlan:
    """v2 配置 -> CrawlPlan（profile=linear）。

    只做归一化，不改语义：raw 保留完整配置，linear 执行器仍按原字段消费。
    """
    if not isinstance(config, dict):
        raise TypeError("config 必须是 dict")
    cfg = copy.deepcopy(config)
    src = _as_dict(cfg.get("source"))
    pag = _as_dict(cfg.get("pagination"))
    rec = _as_dict(cfg.get("record"))
    detail = _as_dict(cfg.get("detail"))
    output = _as_dict(cfg.get("output"))
    anti = _as_dict(cfg.get("anti_bot"))
    iterate = cfg.get("iterate") if isinstance(cfg.get("iterate"), dict) else None
    seeds: List[str] = []
    if isinstance(src.get("url"), str) and src["url"].strip():
        seeds.append(src["url"].strip())
    if isinstance(src.get("sitemap_urls"), list):
        seeds.extend(str(u) for u in src["sitemap_urls"] if u)
    extraction = {
        "type": src.get("type"),
        "row_css": src.get("row_css"),
        "row_xpath": src.get("row_xpath"),
        "fields": _as_dict(src.get("fields")),
        "record_fields": _as_dict(rec.get("fields")),
    }
    return CrawlPlan(
        name=str(cfg.get("name") or "task"),
        origin="config",
        profile="linear",
        seeds=seeds,
        source=src,
        extraction=extraction,
        pagination=pag,
        detail=detail,
        pipeline=_as_list(cfg.get("pipeline")),
        output=output,
        run={"anti_bot": anti, "iterate": iterate,
             "resume": False, "max_requests": anti.get("max_requests")},
        storage={},
        base_dir=str(base_dir) if base_dir is not None else None,
        task_dir=None,
        raw=cfg,
    )


def from_task(task_dir: Path) -> CrawlPlan:
    """v3 任务包 -> CrawlPlan（profile=queue）。"""
    root = Path(task_dir)
    cfg_path = root / "config.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"任务包缺少 config.json: {root}")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError(f"任务包 config.json 顶层必须是对象: {cfg_path}")
    src = _as_dict(cfg.get("source"))
    queue = _as_dict(cfg.get("queue"))
    detail = _as_dict(cfg.get("detail"))
    return CrawlPlan(
        name=str(cfg.get("name") or root.name),
        origin="task",
        profile="queue",
        seeds=[str(u) for u in _as_list(cfg.get("start_urls")) if u],
        source=src,
        extraction={"rules": _as_list(cfg.get("rules")),
                    "parsers": _as_dict(cfg.get("parsers"))},
        pagination={},
        detail=detail,
        pipeline=_as_list(cfg.get("pipelines")),
        output=_as_dict(cfg.get("output")),
        run={"anti_bot": _as_dict(cfg.get("anti_bot")), "queue": queue},
        storage=_as_dict(cfg.get("storage")),
        rules=_as_list(cfg.get("rules")),
        parsers=_as_dict(cfg.get("parsers")),
        task_dir=str(root),
        raw=cfg,
    )


def load(path: Path) -> CrawlPlan:
    """按路径形态加载：目录=任务包，文件=v2 配置。"""
    p = Path(path)
    if p.is_dir():
        return from_task(p)
    if p.is_file():
        cfg = json.loads(p.read_text(encoding="utf-8"))
        return from_config(cfg, base_dir=p.resolve().parent)
    raise FileNotFoundError(f"计划来源不存在: {p}")


def dump(plan: CrawlPlan, path: Path) -> Path:
    """把计划写成可审阅 JSON（原子写）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    tmp.replace(p)
    return p
