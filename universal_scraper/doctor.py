#!/usr/bin/env python3
"""🩺 万能爬虫工具自检（us doctor）：依赖 / Node 桥 / 浏览器 / 端口 / 仓库 / 输出目录。
用法: python3 -m universal_scraper.cli doctor
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
from .runtime import resolve_node, resolve_node_path
NODE = os.environ.get("UNIVERSAL_SCRAPER_NODE", resolve_node())
NODE_PATH = os.environ.get("UNIVERSAL_SCRAPER_NODE_PATH", resolve_node_path())

REQUIRED_PY = ["lxml", "curl_cffi", "charset_normalizer", "openpyxl"]
OPTIONAL_PY = ["pandas", "requests", "ddddocr", "cv2", "rapidocr_onnxruntime"]


def _py_ok(name: str) -> bool:
    """导入探测：静默重定向 stdout/stderr——可选依赖（如 anaconda 的 pandas/numpy
    兼容性警告）的 Traceback 不许刷屏淹没真实结论。"""
    import contextlib
    import io
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            __import__(name)
        return True
    except Exception:
        return False


def check_deps() -> List[Dict[str, str]]:
    out = []
    for m in REQUIRED_PY:
        out.append({"item": f"python 依赖 {m}", "ok": _py_ok(m),
                    "hint": "python3 -m pip install " + m if not _py_ok(m) else ""})
    for m in OPTIONAL_PY:
        ok = _py_ok(m)
        out.append({"item": f"python 可选 {m}", "ok": ok,
                    "hint": "" if ok else "可选（缺省时自动降级）"})
    return out


def check_node() -> List[Dict[str, str]]:
    out = []
    node_ok = Path(NODE).exists()
    out.append({"item": "Node 运行时", "ok": node_ok,
                "hint": f"未找到: {NODE}" if not node_ok else ""})
    if node_ok:
        try:
            r = subprocess.run([NODE, "-v"], capture_output=True, text=True, timeout=15)
            out.append({"item": "Node 版本", "ok": r.returncode == 0, "hint": r.stdout.strip()[:40]})
        except Exception as e:
            out.append({"item": "Node 版本", "ok": False, "hint": str(e)})
    for pkg, imp in (("patchright", "patchright"), ("playwright", "playwright")):
        p = Path(NODE_PATH) / imp
        local = ROOT / "node_modules" / imp
        ok = p.exists() or local.exists()
        out.append({"item": f"npm 包 {pkg}", "ok": ok,
                    "hint": "" if ok else f"未找到: {p}（npm install 或配置 UNIVERSAL_SCRAPER_NODE_PATH）"})
    return out


def check_browsers() -> List[Dict[str, str]]:
    home = Path.home()
    cands = [
        ("用户 Chrome", Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
        ("headless shell", Path(home) / "Library/Caches/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell"),
        ("Chrome for Testing", Path(home) / "Library/Caches/ms-playwright/chromium-1208/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
    ]
    return [{"item": n, "ok": p.exists(), "hint": "" if p.exists() else f"未找到: {p}"} for n, p in cands]


def check_port(port: int = 8642) -> List[Dict[str, str]]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return [{"item": f"端口 {port}", "ok": True, "hint": "空闲"}]
    except OSError:
        return [{"item": f"端口 {port}", "ok": False, "hint": "被占用（可能 WebUI 已在运行）"}]
    finally:
        s.close()


def check_repo() -> List[Dict[str, str]]:
    out = []
    git = Path(ROOT) / ".git"
    out.append({"item": "git 仓库", "ok": git.exists(), "hint": "" if git.exists() else "未初始化"})
    if git.exists():
        try:
            r = subprocess.run(["git", "-C", str(ROOT), "status", "--short"], capture_output=True, text=True, timeout=15)
            dirty = len(r.stdout.strip().splitlines())
            out.append({"item": "工作区", "ok": dirty == 0, "hint": f"{dirty} 个未提交改动" if dirty else "干净"})
        except Exception as e:
            out.append({"item": "工作区", "ok": False, "hint": str(e)})
    return out


def check_outputs() -> List[Dict[str, str]]:
    d = ROOT / "outputs"
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".doctor_probe"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
        return [{"item": "outputs 目录", "ok": True, "hint": str(d)}]
    except Exception as e:
        return [{"item": "outputs 目录", "ok": False, "hint": str(e)}]


def run() -> Dict[str, object]:
    checks = (check_deps() + check_node() + check_browsers() +
              check_port() + check_repo() + check_outputs())
    ok_n = sum(1 for c in checks if c["ok"])
    return {"total": len(checks), "ok": ok_n, "checks": checks}


def main() -> int:
    r = run()
    print(f"🩺 万能爬虫工具自检：{r['ok']}/{r['total']} 项通过")
    for c in r["checks"]:
        mark = "✅" if c["ok"] else "❌"
        print(f"  {mark} {c['item']}" + (f"  → {c['hint']}" if c["hint"] else ""))
    if r["ok"] == r["total"]:
        print("🎉 全部就绪，可正常使用。")
        return 0
    print("\n修复提示见每项 → 提示。")
    return 1 if r["total"] - r["ok"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
