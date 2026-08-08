#!/usr/bin/env python3
"""Node 运行时定位（唯一来源）。

优先级：环境变量 → sys.executable 同级目录（codex runtime 布局）→ PATH → 兜底本机缓存。
避免把 /Users/<user>/.cache/... 硬编码散落在各模块（分享给其他人用时会直接坏掉）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# 兜底：仅当上面都找不到时使用本机已知的 codex runtime 路径
_FALLBACK_NODE = "/Users/kairanqin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
_FALLBACK_MODS = "/Users/kairanqin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"


def resolve_node() -> str:
    env = os.environ.get("UNIVERSAL_SCRAPER_NODE", "").strip()
    if env and Path(env).exists():
        return env
    exe = Path(sys.executable).resolve()
    # codex runtime 常见布局：<...>/dependencies/node/bin/node
    for cand in (exe.parent / "node",
                 exe.parent.parent / "bin" / "node",
                 exe.parent.parent / "node" / "bin" / "node",
                 exe.parent.parent.parent / "node" / "bin" / "node",
                 exe.parent.parent.parent.parent / "node" / "bin" / "node"):
        if cand.exists():
            return str(cand)
    w = shutil.which("node")
    if w:
        return w
    return _FALLBACK_NODE if Path(_FALLBACK_NODE).exists() else "node"


def resolve_node_path() -> str:
    env = os.environ.get("UNIVERSAL_SCRAPER_NODE_PATH", "").strip()
    if env and Path(env).exists():
        return env
    exe = Path(sys.executable).resolve()
    for cand in (exe.parent / "node_modules", exe.parent.parent / "node_modules",
                 exe.parent.parent / "node" / "node_modules"):
        if cand.exists():
            return str(cand)
    try:
        out = subprocess.run(
            [resolve_node(), "-e",
             "console.log(require.resolve('playwright/package.json'))"],
            capture_output=True, text=True, timeout=10)
        p = out.stdout.strip()
        if p:
            # require.resolve 返回 .../node_modules/playwright/package.json
            # NODE_PATH 需要的是 node_modules 目录（父级）
            pkg = Path(p)
            if pkg.name == "package.json" and pkg.parent.parent.name == "node_modules":
                return str(pkg.parent.parent)
            if pkg.exists() and pkg.name == "playwright":
                return str(pkg.parent)
    except Exception:
        pass
    return _FALLBACK_MODS if Path(_FALLBACK_MODS).exists() else ""
