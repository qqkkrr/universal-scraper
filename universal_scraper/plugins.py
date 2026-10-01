#!/usr/bin/env python3
"""插件钩子架构（R25 对标 Scrapy 中间件/Crawlee 请求提供者）：

用户在 `plugins/` 目录下放 `*.py` 文件，定义以下任意函数即自动生效：
    def process_request(req: dict) -> dict | None
        修改/拦截请求（req 是 dict: {"url","method","headers","body"}）。
        返回 None 丢弃该请求。
    def process_response(resp: dict) -> dict
        修改响应（resp 是 dict: {"status","text","headers","body"}）。
    def process_item(item: dict) -> dict | None
        修改/过滤条目（返回 None 丢弃）。

插件加载规则：启动时扫描 `plugins/` 目录下所有 `*.py`，按文件名排序加载。
单个插件抛异常不影响其他插件和主流程（错误打印 stderr 后跳过该插件）。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "plugins"

_LOADED = False
_LOADED_DIRS: set = set()  # OCR R131（M）：记录已载目录——后到 extra_dir 显式告警
_PLUGINS: List[Dict[str, Callable]] = []
_LOAD_LOCK = threading.Lock()  # R4 复查修复：多 worker 并发首触发曾双加载——
                                # 插件重复 N 次执行整个任务（非幂等插件数据损坏）


def _load_plugins(extra_dir: Optional[Path] = None) -> List[Dict[str, Callable]]:
    """扫描并加载所有插件模块（幂等：只在首次调用时加载，线程安全）。"""
    global _LOADED
    if _LOADED:
        # OCR R131（M）：首载后 extra_dir 曾被静默无视——调用方以为插件生效了。
        # 目录未载入过时显式告警（加载器按首次调用定目录，属既有缓存语义）
        if extra_dir is not None:
            _ex = str(extra_dir.resolve())
            if _ex != str(PLUGIN_DIR.resolve()) and _ex not in _LOADED_DIRS:
                print(f"⚠️ 插件已按首次调用目录加载，extra_dir={_ex} 未生效"
                      f"（已载: {sorted(_LOADED_DIRS)}）", file=sys.stderr)
        return _PLUGINS
    with _LOAD_LOCK:
        if _LOADED:  # 双检：等锁期间别的线程已完成加载
            return _PLUGINS
        dirs = [PLUGIN_DIR]
        if extra_dir and extra_dir.resolve() != PLUGIN_DIR.resolve():
            dirs.append(extra_dir.resolve())
        for d in dirs:
            _LOADED_DIRS.add(str(d))
            if not d.is_dir():
                continue
            for fp in sorted(d.glob("*.py")):
                if fp.name.startswith("_"):
                    continue
                try:
                    # OCR R131（M）：spec 名只含 stem——两目录同名插件曾在 sys.modules
                    # 互覆（后载的顶掉先载的）。目录名拼进模块名保证唯一
                    # 审查八轮（L）：目录 tag 曾只拼 basename——技能自带 ROOT/plugins
                    # 与用户 extra_dir 同名（…/plugins）时两目录同名文件仍互覆。
                    # 全路径进 tag
                    _dtag = re.sub(r"\W", "_", str(d))
                    spec = importlib.util.spec_from_file_location(f"plugin_{_dtag}_{fp.stem}", fp)
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = mod
                    spec.loader.exec_module(mod)
                    hooks = {}
                    for fn_name in ("process_request", "process_response", "process_item"):
                        fn = getattr(mod, fn_name, None)
                        if callable(fn):
                            hooks[fn_name] = fn
                    if hooks:
                        hooks["_source"] = fp.name
                        _PLUGINS.append(hooks)
                except Exception as e:
                    # OCR R131（M）：曾只打一行摘要——插件内语法/依赖错误无处可查
                    import traceback as _tb
                    print(f"⚠️ 插件加载失败 {fp.name}: {type(e).__name__}: {e}", file=sys.stderr)
                    _tb.print_exc(file=sys.stderr)
        _LOADED = True
        return _PLUGINS


def apply_request_hooks(req: Dict[str, Any], extra_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """对所有插件的 process_request 依次执行。返回 None = 请求被丢弃。"""
    for p in _load_plugins(extra_dir):
        fn = p.get("process_request")
        if fn is None:
            continue
        try:
            req = fn(req)
            if req is None:
                return None
        except Exception as e:
            print(f"⚠️ 插件 {p.get('_source', '?')} process_request 异常: {e}", file=sys.stderr)
    return req


def apply_response_hooks(resp: Dict[str, Any], extra_dir: Optional[Path] = None) -> Dict[str, Any]:
    for p in _load_plugins(extra_dir):
        fn = p.get("process_response")
        if fn is None:
            continue
        try:
            _result = fn(resp)
            if _result is None:
                continue  # 插件返回 None 表示跳过（不丢 resp）
            resp = _result
        except Exception as e:
            print(f"⚠️ 插件 {p.get('_source', '?')} process_response 异常: {e}", file=sys.stderr)
    return resp


def apply_item_hooks(item: Dict[str, Any], extra_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    for p in _load_plugins(extra_dir):
        fn = p.get("process_item")
        if fn is None:
            continue
        try:
            item = fn(item)
            if item is None:
                return None
        except Exception as e:
            print(f"⚠️ 插件 {p.get('_source', '?')} process_item 异常: {e}", file=sys.stderr)
    return item
