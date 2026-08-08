#!/usr/bin/env python3
"""浏览器执行器封装：Node+Playwright 桥的 Python API。

设计（对标 agentic-stealth-browser / playwright-stealth-mcp）：
  - 真实 Chromium + 最小 stealth（去 automation 痕迹）
  - 动作序列（点击/拖拽/轨迹/滚动/JS）+ 提取规则 + 网络/WS 帧/ Cookie 收集
  - 一次调用返回结构化 JSON，供引擎/auto 层/挑战测试复用
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
from .runtime import resolve_node, resolve_node_path
NODE = os.environ.get("UNIVERSAL_SCRAPER_NODE", resolve_node())
NODE_PATH = os.environ.get("UNIVERSAL_SCRAPER_NODE_PATH", resolve_node_path())
BRIDGE = ROOT / "scripts" / "browser_agent.cjs"


class BrowserAgentError(RuntimeError):
    pass


def run_browser(url: str,
                actions: Optional[List[Dict[str, Any]]] = None,
                extract: Optional[List[Dict[str, Any]]] = None,
                wait_ms: int = 800,
                stealth: bool = True,
                network: bool = True,
                cookies: bool = True,
                html: bool = False,
                viewport: Optional[Dict[str, int]] = None,
                ua: Optional[str] = None,
                geo: Optional[Dict[str, float]] = None,
                timeout_ms: int = 60000) -> Dict[str, Any]:
    """执行一次浏览器任务。返回 {url,title,extracted,network,ws_frames,cookies,html}。"""
    cmd = [NODE, str(BRIDGE), "--url", url, "--wait-ms", str(wait_ms),
           "--stealth", "1" if stealth else "0",
           "--network", "1" if network else "0",
           "--cookies", "1" if cookies else "0",
           "--html", "1" if html else "0",
           "--timeout", str(timeout_ms)]
    if actions:
        cmd += ["--actions", json.dumps(actions, ensure_ascii=False)]
    if extract:
        cmd += ["--extract", json.dumps(extract, ensure_ascii=False)]
    if viewport:
        cmd += ["--viewport", json.dumps(viewport)]
    if ua:
        cmd += ["--ua", ua]
    if geo:
        cmd += ["--geo", json.dumps(geo)]
    env = dict(os.environ)
    env["NODE_PATH"] = NODE_PATH
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              timeout=timeout_ms // 1000 + 30, env=env)
    except subprocess.TimeoutExpired:
        raise BrowserAgentError("浏览器执行超时")
    last = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result":
            return obj
        last = obj
    err = (proc.stderr or "").strip().splitlines()
    raise BrowserAgentError(f"浏览器执行失败: {last or err[-1] if err else '无输出'}")


def solve_slider(url: str, track_sel: str = "#track", slider_sel: str = "#slider",
                 gap_sel: str = "#gap", wait_ms: int = 600) -> Dict[str, Any]:
    """通用滑块拖拽：读 DOM 上轨道/滑块/缺口几何，计算位移并带轨迹拖拽。"""
    info = run_browser(url, actions=[{"type": "wait", "ms": wait_ms}], extract=[
        {"name": "track", "js": f"""(() => {{
          const t = document.querySelector('{track_sel}');
          const s = document.querySelector('{slider_sel}');
          const g = document.querySelector('{gap_sel}');
          if (!t || !s || !g) return null;
          const tr = t.getBoundingClientRect(), sr = s.getBoundingClientRect(), gr = g.getBoundingClientRect();
          return {{tx: tr.x, ty: tr.y, sx: sr.x, sy: sr.y, gx: gr.x, gw: gr.width, sw: s.offsetWidth}};
        }})()"""},
        {"name": "title", "js": "document.title"},
        {"name": "result", "css": "#result"},
    ], wait_ms=wait_ms)
    geo = info["extracted"].get("track")
    if not geo:
        raise BrowserAgentError("未找到滑块元素")
    g = json.loads(geo) if isinstance(geo, str) else geo
    from_x = g["sx"] + g.get("sw", 40) / 2
    from_y = g["sy"] + 20
    to_x = g["gx"] + g["gw"] / 2
    # 目标位置 = 缺口中心 - 滑块中心 + 微调（保证命中）
    dx = to_x - from_x + 2
    res = run_browser(url, actions=[
        {"type": "wait", "ms": wait_ms},
        {"type": "drag", "from": {"x": from_x, "y": from_y}, "to": {"x": from_x + dx, "y": from_y}, "steps": 15},
        {"type": "wait", "ms": 500},
    ], extract=[
        {"name": "title", "js": "document.title"},
        {"name": "result", "css": "#result"},
        {"name": "body", "js": "document.body.innerText.slice(0, 500)"},
        {"name": "cookies", "js": "document.cookie"},
    ], wait_ms=500)
    return res


if __name__ == "__main__":
    import sys
    r = run_browser(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8755/c/1",
                    extract=[{"name": "token", "css": "#token"}], wait_ms=1000)
    print(json.dumps(r, ensure_ascii=False, indent=1)[:2000])
