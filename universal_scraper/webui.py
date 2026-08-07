#!/usr/bin/env python3
"""可视化 Web 界面 v3（零第三方依赖，Python 标准库实现）。

设计：只保留两个入口 ——
  1. 🤖 一句话任务（auto）：描述任务 → 后台线程跑 → 前端轮询实时进度 → 完成即出结果+复核
  2. 📋 贴网页爬虫（paste）：贴 URL → 单页/整站 → 实时进度 → 结果+复核

启动:
  python3 -m universal_scraper.cli webui            # 本机使用
  python3 -m universal_scraper.cli webui --share    # 分享给同一网络的人

API:
  POST /api/auto/start   {description, limit, rounds, timeout}  -> {job}
  POST /api/paste/start  {url, mode, browser, depth, max_pages, limit} -> {job}
  GET  /api/job?job=..   -> {status, messages, result, summary, verify}
  GET  /api/jobs         -> 最近任务列表
  GET  /api/verify?file=.. -> 对 outputs/<file>.json 复核
"""
from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "webui" / "index.html"
PY = None  # 延迟到 serve 里注入 sys.executable

JOBS: dict = {}
JOBS_LOCK = threading.Lock()
JOBS_MAX = 50


# --------------------------------------------------------------------------
# 后台任务
# --------------------------------------------------------------------------

def _new_job(kind: str, title: str) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "title": title,
        "status": "running",
        "messages": ["▶️ 任务已创建，开始执行..."],
        "result": None,
        "summary": None,
        "verify": None,
        "error": None,
        "created": time.time(),
    }
    with JOBS_LOCK:
        JOBS[job["id"]] = job
        if len(JOBS) > JOBS_MAX:
            for k in list(JOBS)[: len(JOBS) - JOBS_MAX]:
                JOBS.pop(k, None)
    return job


def _job_log(job, msg: str):
    with JOBS_LOCK:
        job["messages"].append(msg)


def _job_done(job, result, summary=None, verify=None):
    with JOBS_LOCK:
        job["status"] = "done"
        job["result"] = result
        job["summary"] = summary
        job["verify"] = verify


def _job_error(job, err: str):
    with JOBS_LOCK:
        job["status"] = "error"
        job["error"] = str(err)
        job["messages"].append(f"❌ 失败：{err}")


def run_auto_job(job: dict, desc: str, limit, rounds, timeout, proxy="", cookie=""):
    try:
        from .auto import auto_task
        out = auto_task(desc, limit=limit, rounds=rounds,
                        round_timeout=timeout, log_cb=lambda m: _job_log(job, m),
                        proxy=proxy or None, cookie=cookie or None)
        _job_done(job, out.get("result"), out.get("summary"), out.get("verify"))
    except Exception as e:
        _job_error(job, f"{type(e).__name__}: {e}")


def run_paste_job(job: dict, url: str, mode: str, browser: bool, depth: int,
                  max_pages: int, limit: int, proxy: str = "", cookie: str = ""):
    try:
        _job_log(job, f"🌐 正在抓取 {url}")
        # 大众点评搜索页：Cookie 直抓 SSR 解析（绕开验证码/csec），无需浏览器弹窗
        if "dianping.com/search" in url and cookie:
            try:
                import re as _re
                m = _re.search(r"/search/keyword/(\d+)/", url)
                city = int(m.group(1)) if m else 2
                m2 = _re.search(r"/0_(.+)", url)
                kw = urllib.parse.unquote(m2.group(1)) if m2 else "美食"
                from .dianping import run as dp_run
                _job_log(job, f"🌶️ 检测到大众点评搜索页：Cookie 直抓「{kw}」城市 {city}...")
                r = dp_run(kw, city=city, cookie=cookie, limit=int(limit or 10), proxy=proxy or None)
                if r.get("error"):
                    _job_error(job, r["error"])
                    return
                summary = f"✅ 任务结束：大众点评「{kw}」抓取 {r['total']} 家，导出 {list(r['files'].values())}"
                _job_done(job, {"total": r["total"], "fetched": 1, "errors": 0, "files": r["files"]},
                          summary, _auto_verify(r["rows"], None))
                return
            except Exception as e:
                _job_error(job, f"大众点评直抓失败：{type(e).__name__}: {e}")
                return
        if mode == "crawl":
            from .quick import crawl_url
            _job_log(job, f"🔄 整站爬取模式：深度 {depth}，最多 {max_pages} 页")
            result = crawl_url(url, depth=depth, max_pages=max_pages,
                               browser=browser, proxy=proxy or None, concurrency=3)
            rows = []
            fp = ROOT / "outputs" / f"{result.get('base_name')}.json"
            if fp.exists():
                rows = json.loads(fp.read_text(encoding="utf-8"))
            summary = f"✅ 任务结束：{len(rows)} 条，抓取 {result.get('fetched')} 页"
            _job_done(job, {"total": len(rows), "fetched": result.get("fetched"),
                            "errors": result.get("errors"), "files": result.get("files")},
                      summary, _auto_verify(rows, None))
            return
        from .quick import fetch_url
        kw = {"browser": browser, "proxy": proxy or None, "cookie": cookie or None}
        if mode == "article":
            kw["article"] = True
        elif mode == "table":
            kw["table"] = True
        elif mode == "links":
            kw["links"] = True
        else:
            kw["article"] = True  # 自动：优先正文
        _job_log(job, "⏳ 抓取中（普通网页模式，若内容为空可改用浏览器渲染）...")
        r = fetch_url(url, timeout=90, **kw)
        if r.get("error"):
            _job_error(job, r["error"])
            return
        text = r.get("article") or r.get("markdown") or r.get("text") or ""
        rows = [{"_url": url, "content": text[:20000]}] if text else []
        _job_log(job, f"✅ 抓取完成：状态 {r.get('status')}，内容 {len(text)} 字符")
        summary = f"✅ 任务结束：抓取成功（HTTP {r.get('status')}，正文 {len(text)} 字符）"
        if not text.strip():
            summary = "⚠️ 任务结束：页面无正文（可能需浏览器渲染或需要登录）"
        _job_done(job, {"status": r.get("status"), "len": len(text)}, summary,
                  _auto_verify(rows, None))
    except Exception as e:
        _job_error(job, f"{type(e).__name__}: {e}")


def _auto_verify(rows, cfg):
    """轻量复核：字段完整率 + 去重 + 数量。返回报告 dict 或 None。"""
    if not rows:
        return {"ok": False, "message": "0 条数据，无需复核", "checks": []}
    try:
        from .verify import verify_rows
        return verify_rows(rows, cfg, sample_n=3, network=False)
    except Exception as e:
        return {"ok": False, "message": f"复核失败：{e}", "checks": []}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

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

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                if INDEX.exists():
                    self._send(200, INDEX.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                else:
                    self._send(404, "webui/index.html 不存在")
            elif u.path == "/api/job":
                jid = q.get("job", [""])[0]
                with JOBS_LOCK:
                    job = JOBS.get(jid)
                    if job is None:
                        self._json({"error": "任务不存在或已过期"})
                        return
                    snap = dict(job)
                    snap["messages"] = list(job["messages"])
                self._json(snap)
            elif u.path == "/api/jobs":
                with JOBS_LOCK:
                    lst = [{"id": j["id"], "kind": j["kind"], "title": j["title"],
                            "status": j["status"], "summary": j["summary"],
                            "created": j["created"]} for j in reversed(list(JOBS.values()))][:20]
                self._json(lst)
            elif u.path == "/api/ip":
                from .net import detect_ip
                self._json(detect_ip())
            elif u.path == "/api/verify":
                name = q.get("file", [""])[0]
                fp = ROOT / "outputs" / name if not name.startswith(("outputs/", "/")) else ROOT / name
                if not fp.exists():
                    self._json({"error": f"文件不存在: {name}"})
                    return
                from .verify import verify_rows
                rows = json.loads(fp.read_text(encoding="utf-8"))
                if not isinstance(rows, list):
                    rows = [rows]
                self._json(verify_rows(rows, None, sample_n=3, network=True))
            else:
                self._send(404, "not found")
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            body = self._read_body()
            if u.path == "/api/auto/start":
                desc = str(body.get("description", "")).strip()
                if not desc:
                    self._json({"error": "请先描述任务"})
                    return
                job = _new_job("auto", desc[:60])
                limit = body.get("limit") or None
                rounds = int(body.get("rounds") or 2)
                timeout = int(body.get("timeout") or 0) or None
                proxy = body.get("proxy", "")
                cookie = body.get("cookie", "")
                threading.Thread(target=run_auto_job, args=(job, desc, limit, rounds, timeout, proxy, cookie),
                                 daemon=True).start()
                self._json({"job": job["id"]})
            elif u.path == "/api/test-cookie":
                cookie = str(body.get("cookie", ""))
                url = str(body.get("url", ""))
                if not cookie:
                    self._json({"ok": False, "message": "请先粘贴 Cookie"})
                    return
                if "dianping.com/search" in url:
                    from .dianping import fetch_search_page, parse_search_html
                    r = fetch_search_page("美食", 2, cookie=cookie)
                    if r.get("ok") and "shop-list" in r.get("html", ""):
                        n = len(parse_search_html(r.get("html", ""), 3))
                        self._json({"ok": True, "message": f"✅ Cookie 有效！已识别 {n} 家商家，可直接抓取"})
                    elif r.get("ok") and "verify.meituan.com" in r.get("final_url", ""):
                        self._json({"ok": False, "message": "❌ Cookie 失效或被风控：请求被重定向到验证中心，请重新复制 Cookie 或换网络"})
                    else:
                        self._json({"ok": False, "message": "❌ 页面未包含商家列表（Cookie 可能失效），请重新复制"})
                else:
                    import urllib.request as _urlreq
                    try:
                        req = _urlreq.Request(url or "https://www.baidu.com/",
                                              headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie})
                        rr = _urlreq.urlopen(req, timeout=15)
                        self._json({"ok": True, "message": f"✅ Cookie 已随请求发送（HTTP {rr.status}）"})
                    except Exception as e:
                        self._json({"ok": False, "message": f"❌ 测试失败：{type(e).__name__}"})

            elif u.path == "/api/paste/start":
                url = str(body.get("url", "")).strip()
                if not url.startswith(("http://", "https://")):
                    self._json({"error": "请填写 http/https 开头的完整网址"})
                    return
                job = _new_job("paste", url[:60])
                mode = body.get("mode", "auto")
                browser = bool(body.get("browser"))
                depth = int(body.get("depth") or 2)
                max_pages = int(body.get("max_pages") or 100)
                limit = body.get("limit") or None
                proxy = body.get("proxy", "")
                cookie = body.get("cookie", "")
                threading.Thread(target=run_paste_job,
                                 args=(job, url, mode, browser, depth, max_pages, limit, proxy, cookie),
                                 daemon=True).start()
                self._json({"job": job["id"]})
            else:
                self._send(404, "not found")
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"})

    def log_message(self, *a):
        pass


def _lan_urls(port: int):
    urls = []
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if ip.startswith("127."):
                continue
            urls.append(f"http://{ip}:{port}")
    except Exception:
        pass
    try:
        out = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, timeout=3)
        ip = out.stdout.strip()
        if ip:
            urls.insert(0, f"http://{ip}:{port}")
    except Exception:
        pass
    return urls


def serve(port: int = 8642, host: str = "127.0.0.1", auto_open: bool = True,
          share: bool = False) -> int:
    global PY
    import sys
    PY = sys.executable
    print("🕷️ 万能爬虫工具 · 可视化版 v3", flush=True)
    if share:
        host = "0.0.0.0"
        print("   📡 分享模式：同一网络（WiFi）的人可用下面地址访问", flush=True)
        for u in _lan_urls(port):
            print(f"      {u}", flush=True)
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"❌ 启动失败: {e}", flush=True)
        print("   可能端口被占用。换端口：python3 -m universal_scraper.cli webui --port 8643", flush=True)
        return 1
    url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}"
    print(f"✅ 服务已启动: {url}", flush=True)
    print("   按 Ctrl+C 停止", flush=True)
    if auto_open and host == "127.0.0.1":
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        print("   🖥️ 已尝试自动打开浏览器……", flush=True)
    print("   💡 如果浏览器打不开：系统代理（Clash 等）请把 127.0.0.1 加入直连", flush=True)
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
    ap.add_argument("--share", action="store_true")
    a = ap.parse_args()
    serve(a.port, a.host, share=a.share)
