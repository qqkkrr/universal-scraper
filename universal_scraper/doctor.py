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
        ok = _py_ok(m)  # OCR R131（M）：importlib 探测曾每模块跑两次
        out.append({"item": f"python 依赖 {m}", "ok": ok,
                    "hint": "python3 -m pip install " + m if not ok else ""})
    for m in OPTIONAL_PY:
        ok = _py_ok(m)
        # 审查八轮（MEDIUM）：可选依赖与硬依赖共用 ok 计数与退出码——缺 pandas 也
        # exit 1，把 doctor 当预检门禁的编排器/agent 会被恒假失败挡住。
        out.append({"item": f"python 可选 {m}", "ok": ok,
                    "hint": "" if ok else "可选（缺省时自动降级）", "optional": True})
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
    # OCR R131（M）：探测路径仅 macOS——非 darwin 平台逐项报"未找到"全是误报，
    # 显式降级为平台提示而非三行红叉
    import sys as _sys
    if _sys.platform != "darwin":
        return [{"item": "浏览器安装", "ok": True,
                 "hint": f"当前平台 {_sys.platform} 的浏览器体检仅支持 macOS 路径——"
                         "请用 `npx playwright install chromium` 自行确认"}]
    cands = [("用户 Chrome", Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))]
    # OCR R131（M）：Playwright 构建号（如 1208）曾硬编码——升级后体检恒报
    # "未找到"。动态枚举 ms-playwright 下全部已装构建（新→旧），逐个探测两种布局
    import glob as _glob
    _pw = Path(home) / "Library/Caches/ms-playwright"
    _builds = (sorted(_glob.glob(str(_pw / "chromium_headless_shell-*")), reverse=True)
               + sorted(_glob.glob(str(_pw / "chromium-*")), reverse=True))
    if not _builds:
        # 审查二轮（M）：曾把中文提示塞进 Path 当候选路径——输出怪异。独立提示项
        return [{"item": "Playwright Chromium", "ok": False,
                 "hint": "未安装任何构建——运行 npx playwright install chromium"}]
    for bd in _builds:
        b = Path(bd)
        layouts = [
            b / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell",
            b / "chrome-mac-arm64" / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing",
        ]
        found = next((p for p in layouts if p.exists()), None)
        cands.append((b.name, found or layouts[0]))
    return [{"item": n, "ok": p.exists(), "hint": "" if p.exists() else f"未找到: {p}"} for n, p in cands]


def check_port(port: int = 8642) -> List[Dict[str, str]]:
    try:
        # OCR R7：socket() 本身可能抛 OSError（fd 耗尽等）——其余 check_* 都优雅降级，
        # 唯独这里曾让整个 doctor 崩掉。创建一并纳入 try，with 保证句柄释放
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            return [{"item": f"端口 {port}", "ok": True, "hint": "空闲"}]
    except OSError:
        return [{"item": f"端口 {port}", "ok": False, "hint": "被占用（可能 WebUI 已在运行）"}]


def check_repo() -> List[Dict[str, str]]:
    out = []
    git = Path(ROOT) / ".git"
    out.append({"item": "git 仓库", "ok": git.exists(), "hint": "" if git.exists() else "未初始化"})
    if git.exists():
        try:
            r = subprocess.run(["git", "-C", str(ROOT), "status", "--short"], capture_output=True, text=True, timeout=15)
            dirty = len(r.stdout.strip().splitlines())
            # 审查八轮（LOW）：技能仓库常态有未提交改动（本地沉淀/输出），工作区脏
            # 不该让 doctor 整体判失败 → 标 optional（仍如实显示，不计入退出码）
            out.append({"item": "工作区", "ok": dirty == 0,
                        "hint": f"{dirty} 个未提交改动（不影响使用）" if dirty else "干净",
                        "optional": True})
        except Exception as e:
            out.append({"item": "工作区", "ok": False, "hint": str(e), "optional": True})
    return out


def check_outputs() -> List[Dict[str, str]]:
    d = ROOT / "outputs"
    try:
        d.mkdir(parents=True, exist_ok=True)
        # 审查修复：固定探测名在并发 doctor 双跑下互踩（write 撞 unlink 曾误报
        # "目录不可写"）——拼 PID 保证各进程各测各的
        probe = d / f".doctor_probe_{os.getpid()}"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
        return [{"item": "outputs 目录", "ok": True, "hint": str(d)}]
    except Exception as e:
        return [{"item": "outputs 目录", "ok": False, "hint": str(e)}]


def check_network() -> List[Dict[str, str]]:
    """网络链路组（信息性）：代理/出口状态。ok=True 表示"可诊断"而非"无代理"——
    代理开启是状态不是错误，hint 里给出精确措辞（商标网战训：进程在跑但未启用
    曾被误报成"系统代理已开启"）。"""
    out: List[Dict[str, str]] = []
    try:
        from .net import detect_system_proxy
        sp = detect_system_proxy()
        if sp.get("enabled"):
            out.append({"item": "网络出口（系统代理）", "ok": True,
                        "hint": f"⚠️ 代理已启用（{', '.join(sp.get('sources') or [])}）——"
                                f"直连请求可能被劫持，先 cli ip 确认真实出口"})
        elif sp.get("processes"):
            out.append({"item": "网络出口（代理进程）", "ok": True,
                        "hint": f"有代理进程（{', '.join(sp['processes'])}）但系统代理未启用——"
                                f"当前大概率未被劫持"})
        else:
            out.append({"item": "网络出口", "ok": True, "hint": "直连（无代理接管）"})
    except Exception as e:
        out.append({"item": "网络出口", "ok": True, "hint": f"检测失败（不影响使用）: {e}"})
    return out


def run() -> Dict[str, object]:
    checks = (check_deps() + check_node() + check_browsers() +
              check_port() + check_repo() + check_outputs() + check_network())
    ok_n = sum(1 for c in checks if c["ok"])
    return {"total": len(checks), "ok": ok_n, "checks": checks}


def main() -> int:
    r = run()
    print(f"🩺 万能爬虫工具自检：{r['ok']}/{r['total']} 项通过")
    for c in r["checks"]:
        if c["ok"]:
            mark = "✅"
        else:
            mark = "○" if c.get("optional") else "❌"
        print(f"  {mark} {c['item']}" + (f"  → {c['hint']}" if c["hint"] else ""))
    # 审查八轮（MEDIUM）：可选依赖/工作区脏曾是硬失败 → 恒 exit 1（本机技能仓库
    # 常态脏、pandas 常缺）。退出码只反映"硬依赖/环境不可用"，可选项仅提示。
    hard = [c for c in r["checks"] if not c["ok"] and not c.get("optional")]
    if not hard:
        if r["ok"] != r["total"]:
            print("（○ = 可选依赖/工作区提示，不影响使用）")
        print("🎉 核心就绪，可正常使用。")
        return 0
    print("\n修复提示见每项 → 提示。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
