#!/usr/bin/env python3
"""任务包（Task Bundle）：标准化任务目录 + 模块加载。

任务包结构:
  tasks/<task_name>/
  ├── config.json        # 声明式：start_urls / rules / parsers / pipelines / storage / anti_bot
  └── modules/           # 可选：覆盖/新增模块（只改需要的）
      ├── fetcher.py     # class Fetcher(BaseFetcher)
      ├── parser.py      # class Parser(BaseParser) 或 class XxxParser(BaseParser)（多 parser）
      ├── pipeline.py    # class Pipeline(BasePipeline)
      ├── storage.py     # class Storage(BaseStorage)
      └── middleware.py  # class Middleware(BaseMiddleware)

适配新任务 = scaffold 生成任务包 → 主要改 parser.py 或 config.json 的 parsers 声明。
"""
from __future__ import annotations

import importlib.util
import re
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from .config import validate_task as validate
from .protocols import (BaseFetcher, BaseParser, BasePipeline, BaseStorage, BaseMiddleware)


class Task:
    def __init__(self, root: Path):
        self.root = root
        self.config_path = root / "config.json"
        self.modules_dir = root / "modules"
        if not self.config_path.exists():
            raise FileNotFoundError(f"任务包缺少 config.json: {root}")
        self.config = validate(json.loads(self.config_path.read_text(encoding="utf-8")),
                                has_custom_fetcher=(self.modules_dir / "fetcher.py").exists(),
                                has_custom_storage=(self.modules_dir / "storage.py").exists(),
                                has_custom_parser=(self.modules_dir / "parser.py").exists())
        self.name = self.config.get("name", root.name)
        self.modules: Dict[str, Any] = {}

    # ---- 模块加载（按文件缓存：同一任务内每个自定义模块只 exec 一次，
    #      避免 get_parsers/get_custom_parser_classes 重复加载导致副作用双跑）----
    def _load_module_file(self, filename: str) -> Optional[Type]:
        if filename in self.modules:
            return self.modules[filename]
        fp = self.modules_dir / filename
        if not fp.exists():
            self.modules[filename] = None
            return None
        _base = re.sub(r"\W", "_", self.root.name)  # 用目录名（不用 config.name），防非法字符/同名并发
        spec = importlib.util.spec_from_file_location(f"task_{_base}_{filename[:-3]}", fp)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        self.modules[filename] = mod
        return mod

    def _find_class(self, mod, base, default_name: str):
        if mod is None:
            return None
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if isinstance(obj, type) and issubclass(obj, base) and obj is not base:
                return obj
        return None

    def get_fetcher_cls(self) -> Type[BaseFetcher]:
        mod = self._load_module_file("fetcher.py")
        cls = self._find_class(mod, BaseFetcher, "Fetcher")
        if cls:
            return cls
        from .modules.fetchers import HttpFetcher, BridgeFetcher, BrowserFetcher, ScraplingFetcher
        m = {"http": HttpFetcher, "bridge": BridgeFetcher, "browser": BrowserFetcher,
             "scrapling": ScraplingFetcher}
        return m.get(self.config.get("source", {}).get("type", "http"), HttpFetcher)

    def get_parsers(self) -> Dict[str, Type[BaseParser]]:
        """返回 {parser_name: class}。自定义 parser.py 优先，其次内置 ConfigParser。"""
        parsers: Dict[str, Type[BaseParser]] = {}
        mod = self._load_module_file("parser.py")
        if mod:
            for attr in dir(mod):
                obj = getattr(mod, attr)
                if isinstance(obj, type) and issubclass(obj, BaseParser) and obj is not BaseParser:
                    name = getattr(obj, "name", None) or "default"
                    parsers[name] = obj
        from .modules.parsers import ConfigParser, LLMParser, ArticleParser, TableParser, JsonPagedParser
        # 内置解析器按 name 注册（llm/article/table/json_paged）
        builtin = {}
        for cls in (LLMParser, ArticleParser, TableParser, JsonPagedParser):
            builtin[cls.name] = cls
        for pname, pcfg in (self.config.get("parsers", {}) or {}).items():
            t = pcfg.get("type") if isinstance(pcfg, dict) else None
            if t in builtin:
                parsers.setdefault(pname, builtin[t])
            else:
                parsers.setdefault(pname, ConfigParser)
        parsers.setdefault("default", ConfigParser)
        return parsers

    def get_pipeline_cls(self) -> Type[BasePipeline]:
        mod = self._load_module_file("pipeline.py")
        cls = self._find_class(mod, BasePipeline, "Pipeline")
        if cls:
            return cls
        from .modules.pipelines import Pipeline
        return Pipeline

    def get_storage_cls(self) -> Type[BaseStorage]:
        mod = self._load_module_file("storage.py")
        cls = self._find_class(mod, BaseStorage, "Storage")
        if cls:
            return cls
        from .modules.storages import JsonLinesStorage, CsvStorage, SqliteStorage, MultiStorage
        return {"jsonl": JsonLinesStorage, "csv": CsvStorage, "sqlite": SqliteStorage,
                "multi": MultiStorage}.get(
            self.config.get("storage", {}).get("type", "jsonl"), JsonLinesStorage)

    def get_middleware_cls(self):
        mod = self._load_module_file("middleware.py")
        return self._find_class(mod, BaseMiddleware, "Middleware")

    def get_custom_parser_classes(self) -> List[Type[BaseParser]]:
        """返回自定义 parser 类（供 router 注册）。"""
        mod = self._load_module_file("parser.py")
        out = []
        if mod:
            for attr in dir(mod):
                obj = getattr(mod, attr)
                if isinstance(obj, type) and issubclass(obj, BaseParser) and obj is not BaseParser:
                    out.append(obj)
        return out
