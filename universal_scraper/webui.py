#!/usr/bin/env python3
"""可视化 Web 界面（本地版，零第三方依赖，Python 标准库实现）。

启动: python3 -m universal_scraper.cli webui [--port 8642]
打开: http://127.0.0.1:8642

API:
  GET  /api/tasks           列出任务包
  GET  /api/task?path=...   任务配置
  GET  /api/results         输出文件列表
  GET  /api/result?name=... 结果 JSON
  GET  /api/fetch?url=...   一键抓取（browser/article/table/selector）
  POST /api/run            运行任务 {task, limit, vars}
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "webui" / "index.html"
PY = sys.executable


def list_tasks():
    out = []
    for base in ("tasks", "real_tasks"):
        d = ROOT / base
        if not d.exists():
            continue
        for t in sorted(d.iterdir()):
            cfg_f = t / "config.json"
            if not cfg_f.exists():
                continue
            try:
                cfg = json.loads(cfg_f.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({
                "name": cfg.get("name", t.name),
                "path": str(t.relative_to(ROOT)),
                "type": (cfg.get("source") or {}).get("type", "http"),
                "start_urls": cfg.get("start_urls", []),
            })
    # v2 配置（configs/*.json）
    cd = ROOT / "configs"
    if cd.exists():
        for f in sorted(cd.glob("*.json")):
            try:
                cfg = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({
                "name": cfg.get("name", f.stem),
                "path": str(f.relative_to(ROOT)),
                "type": "v2-" + str((cfg.get("source") or {}).get("type", "config")),
                "start_urls": [str((cfg.get("source") or {}).get("url", ""))],
            })
    return out


def read_log(name: str) -> str:
    fp = ROOT / "outputs" / f".run_{name}.log"
    if fp.exists():
        return fp.read_text(encoding="utf-8", errors="replace")[-4000:]
    return ""


def run_task_cli(task: str, limit: str = "", vars_str: str = "") -> dict:
    if task.endswith(".json"):
        cmd = [PY, "-m", "universal_scraper.cli", "run", "--config", str(ROOT / task)]
    else:
        cmd = [PY, "-m", "universal_scraper.cli", "run", "--task", str(ROOT / task)]
    if limit:
        cmd += ["--limit", str(int(limit))]
    for kv in vars_str.split(","):
        kv = kv.strip()
        if "=" in kv:
            cmd += ["--var", kv]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=600)
        data = {}
        try:
            # CLI 结果是多行缩进 JSON，从第一个 { 开始解析
            _idx = p.stdout.find("{")
            data = json.loads(p.stdout[_idx:]) if _idx >= 0 else {}
        except Exception:
            data = {"total": 0, "fetched": 0, "errors": -1}
        data["log"] = (p.stderr or "")[-3000:]
        if p.returncode != 0 and not data.get("total"):
            data["error"] = (p.stderr or p.stdout)[-500:]
        # 数据预览
        name = Path(task).name
        jl = ROOT / "outputs" / "items" / f"{name}.jsonl"
        sample = []
        if jl.exists():
            for line in jl.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    try:
                        sample.append(json.loads(line))
                    except Exception:
                        pass
                if len(sample) >= 5:
                    break
        data["sample"] = sample
        return data
    except subprocess.TimeoutExpired:
        return {"total": 0, "fetched": 0, "errors": -1, "error": "任务运行超时（>600 秒）"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/" or u.path == "/index.html":
                if INDEX.exists():
                    self._send(200, INDEX.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                else:
                    self._send(404, "webui/index.html 不存在")
            elif u.path == "/api/tasks":
                self._json(list_tasks())
            elif u.path == "/api/task":
                path = q.get("path", [""])[0]
                cfg_f = (ROOT / path / "config.json") if not path.endswith(".json") else (ROOT / path)
                if cfg_f.exists():
                    self._json({"cfg": json.loads(cfg_f.read_text(encoding="utf-8"))})
                else:
                    self._json({"error": "任务不存在: " + path})
            elif u.path == "/api/results":
                out = ROOT / "outputs"
                names = sorted(f.name for f in out.glob("*.json") if f.name != "final_test_results.txt")
                self._json(names)
            elif u.path == "/api/result":
                name = q.get("name", [""])[0]
                fp = ROOT / "outputs" / f"{name}.json"
                if not fp.exists():
                    self._json({"error": f"结果文件不存在: outputs/{name}.json"})
                    return
                rows = json.loads(fp.read_text(encoding="utf-8"))
                self._json({"rows": rows if isinstance(rows, list) else [rows]})
            elif u.path == "/api/fetch":
                from .quick import fetch_url
                url = q.get("url", [""])[0]
                kw = {}
                if q.get("browser"):
                    kw["browser"] = True
                if q.get("article"):
                    kw["article"] = True
                if q.get("table"):
                    kw["table"] = True
                if q.get("selector"):
                    kw["selector"] = q["selector"][0]
                if q.get("links"):
                    kw["links"] = True
                try:
                    self._json(fetch_url(url, timeout=90, **kw))
                except Exception as e:
                    self._json({"error": str(e), "status": 0})
            else:
                self._send(404, "not found")
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            if u.path == "/api/auto":
                desc = body.get("description", "")
                if not desc.strip():
                    self._json({"error": "请先描述任务"})
                    return
                from .auto import auto_task
                msgs = []
                out = auto_task(desc, limit=body.get("limit") or None, log_cb=msgs.append)
                out["messages"] = msgs
                self._json(out)
            if u.path == "/api/run":
                task = body.get("task", "")
                ok = (ROOT / task / "config.json").exists() if not task.endswith(".json") else (ROOT / task).exists()
                if not task or not ok:
                    self._json({"error": "任务不存在: " + task})
                    return
                self._json(run_task_cli(task, body.get("limit", ""), body.get("vars", "")))
            else:
                self._send(404, "not found")
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"})

    def log_message(self, *a):
        pass


def serve(port: int = 8642, host: str = "127.0.0.1", auto_open: bool = True) -> int:
    print("🕷️ 万能爬虫工具 · 可视化版", flush=True)
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"❌ 启动失败: {e}", flush=True)
        print("   可能端口被占用。解决办法：换一个端口，例如:", flush=True)
        print("   python3 -m universal_scraper.cli webui --port 8643", flush=True)
        return 1
    url = f"http://{host}:{port}"
    print(f"✅ 服务已启动: {url}", flush=True)
    print("   按 Ctrl+C 停止", flush=True)
    if auto_open:
        import threading as _t
        import webbrowser
        _t.Timer(0.6, lambda: webbrowser.open(url)).start()
        print("   🖥️ 已尝试自动打开浏览器……", flush=True)
    else:
        print(f"   请手动打开浏览器访问: {url}", flush=True)
    print("   💡 如果浏览器打不开：", flush=True)
    print("      1) 系统代理（Clash 等）请把 127.0.0.1 加入直连/绕过", flush=True)
    print("      2) 换端口重试: --port 8643", flush=True)
    print("      3) 换个浏览器（Chrome/Edge/Safari）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        srv.shutdown()
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8642)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    serve(a.port, a.host)
