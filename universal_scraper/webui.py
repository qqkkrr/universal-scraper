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
  GET  /api/verify?file=xxx.json -> 对 outputs/xxx.json 复核（路径限制在 outputs 内）
"""
from __future__ import annotations

import json
import os
import re
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
AUTH_TOKEN = ""

# 任务历史持久化：重启不丢，可复盘（用户说"跑过"的任务必须能回看）
HISTORY_FILE = ROOT / "jobs_history.json"
CODE_DIRS = (ROOT / "universal_scraper", ROOT / "scripts", ROOT / "webui")
_CODE_FP_START = ""


def _code_fingerprint() -> str:
    import hashlib
    h = hashlib.md5()
    try:
        for d in CODE_DIRS:
            for p in sorted(d.glob("*")) if d.exists() else []:
                if p.suffix in (".py", ".cjs", ".js", ".html", ".css"):
                    st = p.stat()
                    h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size};".encode())
    except Exception:
        pass
    return h.hexdigest()[:16]


def _persist_jobs():
    try:
        slim = {jid: {"id": j.get("id"), "kind": j.get("kind"), "title": j.get("title"),
                      "description": j.get("description", ""), "status": j.get("status"),
                      "summary": j.get("summary"), "error": j.get("error"),
                      "created": j.get("created"), "messages": (j.get("messages") or [])[-30:]}
                for jid, j in JOBS.items()}
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(slim, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tmp.replace(HISTORY_FILE)
    except Exception:
        pass


def _load_jobs():
    try:
        if HISTORY_FILE.exists():
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            for jid, j in data.items():
                j.setdefault("messages", [])
                if j.get("status") == "running":
                    j["status"] = "interrupted"
                    j["messages"] = (j.get("messages") or []) + ["⚠️ 服务重启，任务中断（历史记录已保留）"]
                JOBS[jid] = j
    except Exception:
        pass


# --------------------------------------------------------------------------
# 后台任务
# --------------------------------------------------------------------------

def _new_job(kind: str, title: str, description: str = "") -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "title": title,
        "description": description,
        "task_dir": "",
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
    _persist_jobs()
    return job


def _job_log(job, msg: str):
    with JOBS_LOCK:
        job["messages"].append(msg)
        # 消息上限：长任务只保留最近 1000 条，防止 /api/job 轮询全量复制越来越慢
        if len(job["messages"]) > 1000:
            job["messages"] = job["messages"][-1000:]


def _job_done(job, result, summary=None, verify=None):
    with JOBS_LOCK:
        job["status"] = "done"
        job["result"] = result
        job["summary"] = summary
        job["verify"] = verify
    _persist_jobs()


def _job_error(job, err: str):
    with JOBS_LOCK:
        job["status"] = "error"
        job["error"] = str(err)
        job["messages"].append(f"❌ 失败：{err}")
    _persist_jobs()
    # 自动诊断失败类型并附加解决方案（WebUI 会展示给使用者）
    try:
        from .solutions import attach_solution
        attach_solution(job, str(err))
        _persist_jobs()
    except Exception:
        pass


def run_journal_job(job: dict, site: str, since: int, out: str, workers: int, with_meta: bool):
    try:
        from .journals import run as journal_run
        summary = journal_run(site, since_year=since, out_dir=out or None,
                              workers=workers, with_meta=with_meta,
                              log=lambda m: _job_log(job, m))
        if summary.get("error"):
            _job_error(job, summary["error"])
            return
        _job_done(job, {"total": summary.get("pdf_ok", 0), "fetched": summary.get("articles_total", 0),
                        "errors": summary.get("pdf_fail", 0),
                        "files": {"pdf_dir": summary.get("pdf_dir", ""), "csv": summary.get("csv", ""),
                                  "index": summary.get("index", "")}},
                  f"🎉 {summary.get('journal','')} 完成：{summary.get('pdf_ok')}/{summary.get('articles_total')} 篇 PDF（{summary.get('total_mb')} MB），输出 {summary.get('out_dir','')}")
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            _job_error(job, "任务已被手动停止（KeyboardInterrupt）")
        else:
            _job_error(job, f"{type(e).__name__}: {e}")


def _job_heartbeat(job: dict, key: str = "配置生成中"):
    """AI 生成配置/任务执行期间的心跳：避免页面长时间无输出让用户以为卡死。"""
    t0 = time.time()
    last_n = len(job.get("messages") or [])
    while True:
        time.sleep(25)
        with JOBS_LOCK:
            if job.get("status") not in ("running", ""):
                return
            n = len(job.get("messages") or [])
        if n == last_n:
            _job_log(job, f"⏳ 仍在{key}（已等待 {int(time.time() - t0)} 秒）…")
            last_n = n + 1
        else:
            last_n = n
            t0 = time.time()


def run_auto_job(job: dict, desc: str, limit, rounds, timeout, proxy="", cookie="",
                  config=None, name="", task_dir=""):
    job["task_dir"] = task_dir or ""
    threading.Thread(target=_job_heartbeat, args=(job, "AI 生成配置/执行中"), daemon=True).start()
    try:
        if config:
            from .auto import run_with_config
            out = run_with_config(config, name, task_dir, description=desc,
                                  limit=limit, rounds=rounds, round_timeout=timeout,
                                  log_cb=lambda m: _job_log(job, m))
        else:
            from .auto import auto_task
            out = auto_task(desc, limit=limit, rounds=rounds,
                            round_timeout=timeout, log_cb=lambda m: _job_log(job, m),
                            proxy=proxy or None, cookie=cookie or None)
        # ⚠️ 0 条失败也要给引导卡片（之前只有异常才显示）——"跑不出数据"同样需要方案
        _sum = str(out.get("summary") or "")
        if "0 条" in _sum or "0 条" in _sum.replace("任务结束：", ""):
            try:
                from .solutions import attach_solution
                attach_solution(job, _sum + " " + " ".join((job.get("messages") or [])[-5:]))
            except Exception:
                pass
        _job_done(job, out.get("result"), out.get("summary"), out.get("verify"))
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            _job_error(job, "任务已被手动停止（KeyboardInterrupt）")
        else:
            _job_error(job, f"{type(e).__name__}: {e}")


def run_precise_job(job: dict, desc: str, url: str, config: dict):
    """一键自动精配：探测站点 -> LLM 生成方案 -> 注册 -> 试跑验证。"""
    try:
        _job_log(job, f"🎯 自动精配开始：{url}")
        from .precise_auto import generate_precise
        r = generate_precise(desc, url, config or None, limit=8,
                             log=lambda m: _job_log(job, m))
        if r.get("ok"):
            files = r.get("files") or {}
            summary = (f"✅ 精配生成成功（{r.get('kind')}）：{r.get('detail')}，"
                       f"导出 {list(files.values())}；现在可直接重跑原任务")
            _job_done(job, r, summary, verify=r.get("rows") or None)
        else:
            _job_error(job, f"精配试跑未成功：{r.get('error')}（配置已保存，可重跑任务或换入口再试）")
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            _job_error(job, "任务已被手动停止（KeyboardInterrupt）")
        else:
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
        # 命中高频精配站点 → 直接用精配（如工信部APP通报/ggzy/巨潮等），不再走通用猜测
        try:
            from .sites import match_site, run_site
            _site = match_site(url)
            if _site:
                _job_log(job, f"🏆 命中精配[{_site}]，走精配解析（不空转通用引擎）...")
                r = run_site(url, cookie=cookie or "", proxy=proxy or None,
                             limit=int(limit or 20))
                if r.get("error"):
                    _job_error(job, r["error"])
                    return
                rows = r.get("rows") or []
                _job_log(job, f"✅ 精配[{_site}]完成：{r['total']} 条")
                summary = f"✅ 任务结束：精配[{_site}] {r['total']} 条，导出 {list(r['files'].values())}"
                _job_done(job, {"total": r["total"], "fetched": len(rows), "errors": 0,
                                "files": r["files"]}, summary, _auto_verify(rows, None))
                return
        except Exception as e:
            _job_log(job, f"⚠️ 精配检查失败，改走通用流程：{type(e).__name__}: {e}")
        # PDF 直链 / 页面含 PDF 附件 → 通用 PDF 解析（未精配站也能直接出表格）
        if mode in ("auto", "table", "article"):
            try:
                from .pdf_table import is_attachment_url, attachment_to_rows, extract_pdf_links
                import requests as _req
                _pdfs: list = []
                if is_attachment_url(url):
                    _pdfs = [url]
                else:
                    _rr = _req.get(url, timeout=25, verify=False,
                                   headers={"User-Agent": "Mozilla/5.0"})
                    _pdfs = extract_pdf_links(_rr.text or "", url)
                if _pdfs:
                    _job_log(job, f"📄 检测到 {len(_pdfs)} 个 PDF/附件，走通用附件解析…")
                    rows_all: list = []
                    _pdf_errors: list = []
                    for _pu in _pdfs[:5]:
                        res = attachment_to_rows(_pu)
                        if res.get("kind") == "table":
                            rows_all.extend(res.get("rows") or [])
                        elif res.get("kind") == "text":
                            _t = (res.get("text") or "").strip()
                            if _t:
                                rows_all.append({"_pdf": _pu, "content": _t[:20000]})
                        elif res.get("error"):
                            _pdf_errors.append(f"{_pu}: {res['error'][:120]}")
                    if _pdf_errors and not rows_all:
                        _job_log(job, "⚠️ " + "；".join(_pdf_errors[:3]))
                    if rows_all:
                        import hashlib as _hl
                        _host = re.sub(r"[^0-9A-Za-z_-]", "_", urllib.parse.urlparse(_pdfs[0]).netloc)
                        _suffix = _hl.md5("|".join(_pdfs[:5]).encode()).hexdigest()[:8]
                        base = f"pdf_{_host}_{_suffix}"
                        # 导出时去掉内部元数据列（_pdf/_page/_table）
                        _meta = ("_pdf", "_page", "_table")
                        fp = ROOT / "outputs" / f"{base}.json"
                        fp.write_text(json.dumps(rows_all, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                        try:
                            import csv
                            keys = [k for k in rows_all[0] if k not in _meta]
                            with open(ROOT / "outputs" / f"{base}.csv", "w", newline="", encoding="utf-8-sig") as f:
                                w = csv.DictWriter(f, fieldnames=keys)
                                w.writeheader()
                                w.writerows([{k: v for k, v in r.items() if k not in _meta} for r in rows_all])
                        except Exception:
                            pass
                        try:
                            from openpyxl import Workbook
                            wb = Workbook(); ws = wb.active
                            keys = [k for k in rows_all[0] if k not in _meta]
                            ws.append(keys)
                            for r in rows_all:
                                ws.append([r.get(k, "") for k in keys])
                            wb.save(ROOT / "outputs" / f"{base}.xlsx")
                        except Exception:
                            pass
                        files = {"json": f"outputs/{base}.json", "csv": f"outputs/{base}.csv", "xlsx": f"outputs/{base}.xlsx"}
                        summary = f"✅ 任务结束：附件解析 {len(rows_all)} 条，导出 {list(files.values())}"
                        _job_done(job, {"total": len(rows_all), "fetched": len(_pdfs), "errors": 0, "files": files},
                                  summary, _auto_verify(rows_all, None))
                        return
            except Exception as e:
                _job_log(job, f"⚠️ 通用 PDF 解析失败，改走普通网页：{type(e).__name__}: {e}")
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
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            _job_error(job, "任务已被手动停止（KeyboardInterrupt）")
        else:
            _job_error(job, f"{type(e).__name__}: {e}")


def run_batch_job(job: dict, urls: list, mode: str = "auto", browser: bool = False):
    """批量网址抓取：逐个 URL 抓取（精配优先，普通网页兜底），合并导出。"""
    try:
        _job_log(job, f"📚 批量抓取开始：{len(urls)} 个网址")
        rows_all = []
        errs = 0
        for i, u in enumerate(urls, 1):
            try:
                from .sites import match_site, run_site
                _site = match_site(u)
                if _site:
                    r = run_site(u, limit=0)
                    if r.get("rows"):
                        rows_all.extend(r["rows"])
                        _job_log(job, f"[{i}/{len(urls)}] ✅ {_site}：{len(r['rows'])} 条")
                    else:
                        errs += 1
                        _job_log(job, f"[{i}/{len(urls)}] ⚠️ {_site} 0 条（{r.get('error','')[:80]}）")
                else:
                    from .quick import fetch_url
                    fr = fetch_url(u, browser=browser, timeout=60, article=True)
                    text = fr.get("article") or fr.get("markdown") or fr.get("text") or ""
                    if text.strip():
                        rows_all.append({"_url": u, "content": text[:20000]})
                        _job_log(job, f"[{i}/{len(urls)}] ✅ 网页 {len(text)} 字符")
                    else:
                        errs += 1
                        _job_log(job, f"[{i}/{len(urls)}] ⚠️ 无内容（{fr.get('error','')[:80]}）")
            except Exception as e:
                errs += 1
                _job_log(job, f"[{i}/{len(urls)}] ❌ {type(e).__name__}: {str(e)[:100]}")
        # 导出
        import time as _t
        base = f"batch_{int(_t.time())}"
        fp = ROOT / "outputs" / f"{base}.json"
        fp.write_text(json.dumps(rows_all, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        try:
            import csv as _csv
            keys = []
            for r in rows_all:
                for k in r:
                    if k not in keys:
                        keys.append(k)
            with open(ROOT / "outputs" / f"{base}.csv", "w", newline="", encoding="utf-8-sig") as f:
                w = _csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                w.writerows([{k: r.get(k, "") for k in keys} for r in rows_all])
        except Exception:
            pass
        try:
            from openpyxl import Workbook
            wb = Workbook(); ws = wb.active
            keys = list(rows_all[0].keys()) if rows_all else ["_url"]
            ws.append(keys)
            for r in rows_all:
                ws.append([r.get(k, "") for k in keys])
            wb.save(ROOT / "outputs" / f"{base}.xlsx")
        except Exception:
            pass
        files = {"json": f"outputs/{base}.json", "csv": f"outputs/{base}.csv", "xlsx": f"outputs/{base}.xlsx"}
        summary = f"✅ 批量完成：{len(urls)} 个网址，成功 {len(urls)-errs}，共 {len(rows_all)} 条，导出 {list(files.values())}"
        _job_done(job, {"total": len(rows_all), "fetched": len(urls), "errors": errs, "files": files}, summary,
                  _auto_verify(rows_all, None))
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            _job_error(job, "任务已被手动停止（KeyboardInterrupt）")
        else:
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

    MAX_BODY = 5 * 1024 * 1024

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if n > self.MAX_BODY:
            raise ValueError("请求体过大（>5MB），已拒绝")
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _auth_ok(self, headers) -> bool:
        if not AUTH_TOKEN:
            return True
        return (headers.get("X-Auth-Token") or "") == AUTH_TOKEN

    def do_GET(self):
        # 页面/静态资源不鉴权（否则用户连输入令牌的页面都打不开）；仅 /api/* 需要
        if self.path.startswith("/api/") and not self._auth_ok(self.headers):
            self._send(403, "Forbidden: 需要 X-Auth-Token")
            return
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
            elif u.path == "/api/cookies":
                try:
                    from .cookies import list_saved
                    self._json({"ok": True, "sessions": list_saved()})
                except Exception as e:
                    self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
            elif u.path == "/api/code_changed":
                self._json({"changed": _code_fingerprint() != _CODE_FP_START,
                            "current": _code_fingerprint(), "start": _CODE_FP_START})
            elif u.path == "/api/ip":
                from .net import detect_ip
                self._json(detect_ip())
            elif u.path == "/api/verify":
                name = q.get("file", [""])[0]
                # 路径穿越防护：只允许 outputs 目录内（resolve 后校验前缀，拒绝绝对路径/..）
                try:
                    fp = (ROOT / "outputs" / name).resolve()
                    fp.relative_to((ROOT / "outputs").resolve())
                except Exception:
                    self._json({"error": "非法文件路径（仅允许 outputs 目录内）"})
                    return
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
        if not self._auth_ok(self.headers):
            self._send(403, "Forbidden: 需要 X-Auth-Token")
            return
        u = urllib.parse.urlparse(self.path)
        try:
            body = self._read_body()
            if u.path == "/api/auto/plan":
                desc = str(body.get("description", "")).strip()
                if not desc:
                    self._json({"error": "请先描述任务"})
                    return
                from .auto import plan_task
                plan = plan_task(desc,
                                 limit=body.get("limit") or None,
                                 proxy=body.get("proxy", "") or None,
                                 cookie=body.get("cookie", "") or None,
                                 log_cb=lambda m: None)
                # 推荐精配提示：AI 不确定入口 / 数据形态特殊（榜单/图片/PDF/强反爬）
                try:
                    _cfg = plan.get("config") or {}
                    _hint = None
                    if (_cfg.get("intent") or {}).get("entry_unknown"):
                        _hint = "AI 不确定入口；点下方按钮可一键自动精配（自动探测站点并生成专用解析器）"
                    else:
                        _kw = re.findall(r"榜单|排行|排行榜|品牌价值|图片|图表|截图|PDF|附件|扫描件", desc)
                        _hard = any(d in desc for d in
                                    ("dianping", "taobao", "tmall", "douyin", "kuaishou", "zhihu",
                                     "weibo", "xiaohongshu", "zhipin", "boss直聘", "大众点评"))
                        if _kw or _hard:
                            _hint = "检测到可能需要精配的数据形态（榜单/图片/附件/强反爬），可一键自动精配"
                    if _hint:
                        plan["precise_hint"] = {"needed": True, "reason": _hint,
                                                "url": ((_cfg.get("start_urls") or [""])[0])}
                except Exception:
                    pass
                self._json(plan)
            if u.path == "/api/auto/start":
                desc = str(body.get("description", "")).strip()
                if not desc:
                    self._json({"error": "请先描述任务"})
                    return
                config = body.get("config")
                name = str(body.get("name", "") or "").strip()
                task_dir = str(body.get("task_dir", "") or "").strip()
                if config and name and task_dir:
                    # 安全：task_dir 只允许 tasks/ 下（防路径穿越写任意文件，分享模式=远程RCE风险）
                    import re as _re
                    if not _re.fullmatch(r"[A-Za-z0-9_\-]+", name):
                        self._json({"error": "非法任务名（仅允许字母数字_-）"})
                        return
                    _td = Path(task_dir)
                    if not _td.is_absolute():
                        _td = ROOT / _td
                    try:
                        _td.resolve().relative_to((ROOT / "tasks").resolve())
                    except Exception:
                        self._json({"error": "非法任务目录（仅允许 tasks/ 内）"})
                        return
                    desc = body.get("description", "") or desc
                job = _new_job("auto", desc[:60], description=desc)
                limit = body.get("limit") or None
                rounds = int(body.get("rounds") or 2)
                timeout = int(body.get("timeout") or 0) or None
                proxy = body.get("proxy", "")
                cookie = body.get("cookie", "")
                threading.Thread(target=run_auto_job,
                                 args=(job, desc, limit, rounds, timeout, proxy, cookie,
                                       config, name, task_dir),
                                 daemon=True).start()
                self._json({"job": job["id"]})
            elif u.path == "/api/cookies/import":
                port = int(body.get("port") or 9222)
                try:
                    from .cookies import import_from_cdp
                    r = import_from_cdp(port=port, log=lambda m: _job_log(_new_job("cookies", "导入会话"), m) if False else None)
                    self._json(r)
                except Exception as e:
                    self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
            elif u.path == "/api/cookies/delete":
                domain = str(body.get("domain", "")).strip()
                try:
                    from .cookies import delete
                    self._json({"ok": delete(domain), "domain": domain})
                except Exception as e:
                    self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
            elif u.path == "/api/chrome/start":
                # 启动调试 Chrome 并打开目标 URL（一键登录/过盾入口）
                url = str(body.get("url", "")).strip()
                port = int(body.get("port") or 9222)
                try:
                    import subprocess as _sp
                    _chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                    _profile = os.path.expanduser(f"~/.codex/cdp_profile_{port}")
                    os.makedirs(_profile, exist_ok=True)
                    # 端口被占 → 直接打开新标签
                    _occupied = False
                    try:
                        import urllib.request as _ur
                        with _ur.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as _r:
                            _occupied = True
                    except Exception:
                        _occupied = False
                    if _occupied:
                        _sp.Popen(["open", "-a", "Google Chrome", url or "https://www.baidu.com"])
                    else:
                        _cmd = [_chrome, f"--remote-debugging-port={port}",
                                f"--user-data-dir={_profile}", "--no-first-run",
                                "--no-default-browser-check", url or "about:blank"]
                        _sp.Popen(_cmd, start_new_session=True,
                                  stdout=open(os.devnull, "w"), stderr=_sp.STDOUT)
                    self._json({"ok": True, "message": f"调试 Chrome 已启动（端口 {port}）并打开目标站，请登录/过验证后回到工具重跑任务",
                                "port": port, "url": url})
                except Exception as e:
                    self._json({"ok": False, "error": f"启动失败：{type(e).__name__}: {e}"})
            elif u.path == "/api/restart":
                # 代码已更新时一键重启服务（同参数拉起新进程后退出当前进程）
                try:
                    import subprocess as _sp, sys as _sys
                    _cmd = [_sys.executable, "-m", "universal_scraper.cli", "webui"]
                    if host != "127.0.0.1":
                        _cmd += ["--host", str(host)]
                    _cmd += ["--port", str(port)]
                    if AUTH_TOKEN:
                        _cmd += ["--token", AUTH_TOKEN]
                    _sp.Popen(_cmd, cwd=str(ROOT), start_new_session=True,
                              stdout=open(ROOT / "outputs" / "webui_restart.log", "a"),
                              stderr=_sp.STDOUT)
                    self._json({"ok": True, "message": "重启中，3 秒后自动恢复…"})
                    threading.Timer(0.5, os._exit, args=(0,)).start()
                except Exception as e:
                    self._json({"ok": False, "message": f"重启失败：{type(e).__name__}: {e}"})
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

            elif u.path == "/api/job/stop":
                jid = str(body.get("job", "")).strip()
                with JOBS_LOCK:
                    job = JOBS.get(jid)
                    td = job.get("task_dir", "") if job else ""
                    desc = job.get("description", "") if job else ""
                td = td or ""
                # 无 task_dir（auto_task 直跑路径）：用描述哈希定位 tasks/auto_<md5[:10]>
                if not td and desc:
                    try:
                        import hashlib as _hl
                        from pathlib import Path as _P2
                        _n = f"auto_{_hl.md5(desc.encode()).hexdigest()[:10]}"
                        _cand = ROOT / "tasks" / _n
                        if _cand.exists():
                            td = str(_cand)
                    except Exception:
                        pass
                if not td:
                    self._json({"error": "该任务不支持停止（无任务目录）"})
                    return
                try:
                    from pathlib import Path as _P
                    _P(td).mkdir(parents=True, exist_ok=True)
                    (_P(td) / ".stop").write_text("1", encoding="utf-8")
                    if job:
                        with JOBS_LOCK:
                            job["messages"].append("⏹ 已发送停止信号，正在保存检查点并退出...")
                    # 兜底：直接终止工具浏览器子进程（单任务模式安全），防止桥卡验证不退出
                    import subprocess as _sp
                    for _pat in ("browser_generic.cjs", "browser_pool.cjs", "browser_single.cjs"):
                        try:
                            _sp.run(["pkill", "-f", _pat], capture_output=True, timeout=5)
                        except Exception:
                            pass
                    self._json({"ok": True, "message": "已发送停止信号，并已终止浏览器进程"})
                except Exception as e:
                    self._json({"error": f"{type(e).__name__}: {e}"})
            elif u.path == "/api/journal/start":
                site = str(body.get("site", "sytyxb")).strip() or "sytyxb"
                since = int(body.get("since") or 2024)
                out = str(body.get("out", "")).strip()
                workers = max(1, min(int(body.get("workers") or 6), 32))
                with_meta = bool(body.get("with_meta", True))
                job = _new_job("journal", f"期刊下载：{site}（{since} 起）")
                threading.Thread(target=run_journal_job,
                                 args=(job, site, since, out, workers, with_meta),
                                 daemon=True).start()
                self._json({"job": job["id"]})
            elif u.path == "/api/paste/batch":
                urls = body.get("urls") or []
                if isinstance(urls, str):
                    urls = [x.strip() for x in urls.replace("\n", "\n").splitlines() if x.strip()]
                urls = [u.strip() for u in urls if str(u).strip().startswith("http")]
                if not urls:
                    self._json({"error": "请提供至少一个 http/https 网址"})
                    return
                job = _new_job("batch", f"批量抓取 {len(urls)} 个网址")
                threading.Thread(target=run_batch_job,
                                 args=(job, urls, str(body.get("mode", "auto")), bool(body.get("browser"))),
                                 daemon=True).start()
                self._json({"job": job["id"]})
            elif u.path == "/api/precise/start":
                desc = str(body.get("description", "")).strip()
                url = str(body.get("url", "")).strip()
                if not url.startswith(("http://", "https://")):
                    self._json({"error": "请提供 http/https 开头的入口 URL"})
                    return
                if not desc:
                    self._json({"error": "一键精配需要先描述任务（抓什么、要哪些字段），否则 AI 无法判断数据相关性和字段。请回到「一句话任务」输入任务描述后重新点一键精配。"})
                    return
                job = _new_job("precise", f"一键精配：{url[:50]}", description=desc)
                threading.Thread(target=run_precise_job,
                                 args=(job, desc, url, body.get("config") or {}),
                                 daemon=True).start()
                self._json({"job": job["id"]})
            elif u.path == "/api/paste/start":
                url = str(body.get("url", "")).strip()
                if not url.startswith(("http://", "https://")):
                    self._json({"error": "请填写 http/https 开头的完整网址"})
                    return
                job = _new_job("paste", url[:60], description=url)
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
          share: bool = False, token: str = "") -> int:
    global PY, AUTH_TOKEN
    import sys, secrets
    global _CODE_FP_START
    _load_jobs()
    _CODE_FP_START = _code_fingerprint()
    PY = sys.executable
    AUTH_TOKEN = token or os.environ.get("US_WEBUI_TOKEN", "")
    print("🕷️ 万能爬虫工具 · 可视化版 v3", flush=True)
    if share:
        host = "0.0.0.0"
        if not AUTH_TOKEN:
            AUTH_TOKEN = secrets.token_hex(8)
            os.environ["US_WEBUI_TOKEN"] = AUTH_TOKEN
        print("   ⚠️  分享模式已开启访问令牌（所有 /api/* 需带 X-Auth-Token）", flush=True)
        print(f"   🔑 访问令牌: {AUTH_TOKEN}", flush=True)
        print("   📡 同一网络（WiFi）的人可用下面地址访问（页面需输入令牌）", flush=True)
        for u in _lan_urls(port):
            print(f"      {u}", flush=True)
    elif AUTH_TOKEN:
        print(f"   🔑 访问令牌: {AUTH_TOKEN}（所有 /api/* 需带 X-Auth-Token）", flush=True)
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"❌ 启动失败: {e}", flush=True)
        print("   可能端口被占用。换端口：python3 -m universal_scraper.cli webui --port 8643", flush=True)
        return 1
    if host == "0.0.0.0":
        _lan = _lan_urls(port)
        url = _lan[0] if _lan else f"http://127.0.0.1:{port}"
    else:
        url = f"http://{host}:{port}"
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
    ap.add_argument("--token", default="", help="访问令牌（share 模式建议设置；也可用 US_WEBUI_TOKEN）")
    a = ap.parse_args()
    serve(a.port, a.host, share=a.share, token=a.token)
