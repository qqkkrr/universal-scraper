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

# OCR R131（H）：硬编码单机用户路径违背本模块 docstring 自己写的约定（分享即坏）。
# 兜底改为按标准缓存布局展开 ~/<user>，任何用户机器上都能命中同类布局
_CACHE_BASE = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node"
_FALLBACK_NODE = str(_CACHE_BASE / "bin" / "node")
_FALLBACK_MODS = str(_CACHE_BASE / "node_modules")


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
    # OCR R131（M）：全链路失败时有 stderr 诊断提示。审查三轮（H）：原注释
    # 称"返回空串"与实际 return _fb or "node" 矛盾——现状：裸 "node" 作最后
    # 兜底（子进程 command not found 可诊断，上方 WARN 已说明原因），注释据实
    _fb = _FALLBACK_NODE if Path(_FALLBACK_NODE).exists() else ""
    if not _fb:
        import sys as _sys
        print("⚠️ 未找到 Node 运行时：请安装 node 或设置 UNIVERSAL_SCRAPER_NODE",
              file=_sys.stderr)
    return _fb or "node"


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
        # 审查修复（反馈 #3）：require.resolve 从 cwd 搜索模块——从任务目录跑
        # fetch --browser 时找不到 playwright（skill 根目录的 node_modules 不在
        # 搜索链上）。改：显式传 cwd = 技能根目录（本文件 parent.parent）
        _skill_root = str(Path(__file__).resolve().parent.parent)
        out = subprocess.run(
            [resolve_node(), "-e",
             "console.log(require.resolve('playwright/package.json'))"],
            capture_output=True, text=True, timeout=10,
            cwd=_skill_root)
        # OCR R131（M）：returncode 未检查——node 报错时 stdout 空本就不取，
        # 但非零退出伴 stderr 时应留痕（否则"为什么找不到 playwright"无从排查）
        if out.returncode != 0 and out.stderr.strip():
            import sys as _sys
            print(f"⚠️ node 定位 playwright 失败: {out.stderr.strip()[:120]}", file=_sys.stderr)
        p = out.stdout.strip()
        if p:
            # require.resolve 返回 .../node_modules/playwright/package.json
            # NODE_PATH 需要的是 node_modules 目录（父级）
            pkg = Path(p)
            if pkg.name == "package.json" and pkg.parent.parent.name == "node_modules":
                return str(pkg.parent.parent)
            if pkg.exists() and pkg.name == "playwright":
                return str(pkg.parent)
    except Exception as e:
        # 审查修复：静默吞掉（超时/node 不在 PATH 等）曾让"为什么定位不到
        # playwright"无从排查——降级路径保留，但失败必须留痕
        import sys as _sys
        print(f"⚠️ 定位 playwright 模块目录异常（{type(e).__name__}: {e}）"
              "——回退到缓存兜底", file=_sys.stderr)
    return _FALLBACK_MODS if Path(_FALLBACK_MODS).exists() else ""
