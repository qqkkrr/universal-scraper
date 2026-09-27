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
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from .config import validate_task as validate
from .protocols import (BaseFetcher, BaseParser, BasePipeline, BaseStorage, BaseMiddleware)


class Task:
    def __init__(self, root: Path):
        self.root = root
        self.config_path = root / "config.json"
        self.modules_dir = root / "modules"
        self._mod_lock = threading.Lock()  # OCR R131（M）：模块加载 check-then-act 加载器互斥
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
        with self._mod_lock:  # OCR R131（M）：并发 get_parsers 曾对同一文件双 exec（副作用双跑）
            return self._load_module_file_locked(filename)

    def _load_module_file_locked(self, filename: str) -> Optional[Type]:
        if filename in self.modules:
            return self.modules[filename]
        fp = self.modules_dir / filename
        if not fp.exists():
            self.modules[filename] = None
            return None
        _base = re.sub(r"\W", "_", self.root.name)  # 用目录名（不用 config.name），防非法字符/同名并发
        # OCR R131（M）：仅目录名仍可能跨父目录撞名（不同路径同名任务在
        # sys.modules 互覆）——拼根路径短哈希保证进程内唯一
        import hashlib as _hl
        _tag = _hl.md5(str(self.root.resolve()).encode(), usedforsecurity=False).hexdigest()[:6]
        spec = importlib.util.spec_from_file_location(f"task_{_base}_{_tag}_{filename[:-3]}", fp)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            # 审查 L4：exec 失败曾残留 sys.modules 半成品且不缓存失败键——
            # 下次调用重复 exec（用户模块副作用重跑）。清掉并记 None
            sys.modules.pop(spec.name, None)
            self.modules[filename] = None
            raise
        self.modules[filename] = mod
        return mod

    def _find_class(self, mod, base, default_name: str):
        if mod is None:
            return None
        # R21 审查修复（P1）：优先返回【本模块定义】的类——用户按文档惯例写
        # class Fetcher(BaseFetcher) 并复用导入内置类（如 BrowserFetcher）时，
        # dir() 字母序曾让内置类抢在自定义类之前被选中，任务静默用错取数器
        own = getattr(mod, "__name__", "")
        candidates = [getattr(mod, a) for a in dir(mod)]
        defined = [o for o in candidates
                   if isinstance(o, type) and issubclass(o, base) and o is not base
                   and getattr(o, "__module__", "") == own]
        if defined:
            defined.sort(key=lambda c: c.__name__)
            preferred = [c for c in defined if c.__name__ == default_name]
            return (preferred or defined)[0]
        imported = [o for o in candidates
                    if isinstance(o, type) and issubclass(o, base) and o is not base]
        if imported:
            imported.sort(key=lambda c: c.__name__)
            return imported[0]
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
            own = getattr(mod, "__name__", "")
            for attr in dir(mod):
                obj = getattr(mod, attr)
                # R21 审查修复（P1）：只注册本模块定义的 parser（导入的内置类
                # 曾按字母序抢占注册表）
                if (isinstance(obj, type) and issubclass(obj, BaseParser)
                        and obj is not BaseParser
                        and getattr(obj, "__module__", "") == own):
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
            own = getattr(mod, "__name__", "")
            for attr in dir(mod):
                obj = getattr(mod, attr)
                # OCR R131（H）：对齐 get_parsers 的 R21 修复——曾把 import 进来的
                # 内置 parser 也注册进 router（与注册表冲突/抢名）
                if (isinstance(obj, type) and issubclass(obj, BaseParser)
                        and obj is not BaseParser
                        and getattr(obj, "__module__", "") == own):
                    out.append(obj)
        return out
