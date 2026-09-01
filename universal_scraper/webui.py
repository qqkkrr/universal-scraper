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
  GET  /api/books/example -> 示例书籍清单 spec（examples/books.spec.json）
  POST /api/books/start  {spec(json字符串或.json路径), out, covers, interval} -> {job}
"""
from __future__ import annotations

import json
import os
from typing import Dict
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
SETTINGS_FILE = ROOT / "configs" / "settings.json"
_LLM_ENV_KEYS = {
    "model": "LLM_MODEL",
    "base_url": "OPENAI_BASE_URL",
    "api_key": "OPENAI_API_KEY",
    "vision_model": "VISION_MODEL",
    "vision_base_url": "VISION_BASE_URL",
    "vision_api_key": "VISION_API_KEY",
}


def _load_settings() -> dict:
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_settings(st: dict) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _apply_settings_to_env(st: dict) -> None:
    """把持久化的 AI 配置写进当前进程 env（LLMClient 每次读 env，立即生效）。"""
    for key, env in _LLM_ENV_KEYS.items():
        v = (st.get(key) or "").strip()
        if v:
            os.environ[env] = v


def _mask_key(k: str) -> str:
    if not k:
        return ""
    return k[:6] + "••••" + k[-4:] if len(k) > 12 else "••••"


CODE_DIRS = (ROOT / "universal_scraper", ROOT / "scripts", ROOT / "webui")
_CODE_FP_START = ""


def _code_fingerprint() -> str:
    import hashlib
    h = hashlib.md5(usedforsecurity=False)  # 仅指纹用途，非密码学
    try:
        for d in CODE_DIRS:
            for p in sorted(d.glob("*")) if d.exists() else []:
                if p.suffix in (".py", ".cjs", ".js", ".html", ".css"):
                    st = p.stat()
                    h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size};".encode())
    except Exception:
        pass
    return h.hexdigest()[:16]


SCHEDULES_FILE = ROOT / "configs" / "schedules.json"
SCHED_LOCK = threading.Lock()


def _load_schedules() -> list:
    try:
        if SCHEDULES_FILE.exists():
            return json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_schedules(scheds: list) -> None:
    try:
        SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCHEDULES_FILE.write_text(json.dumps(scheds, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _scheduler_loop() -> None:
    """后台调度线程：每分钟检查定时任务，到点自动跑（复用已学配置，全自动）。"""
    while True:
        time.sleep(60)
        try:
            now = time.time()
            with SCHED_LOCK:
                scheds = _load_schedules()
            for sc in scheds:
                if not sc.get("enabled", True):
                    continue
                if float(sc.get("next_run") or 0) <= now:
                    sc["next_run"] = now + float(sc.get("interval_hours") or 24) * 3600
                    sc["last_run"] = now
                    _save_schedules(scheds)  # 仍持 SCHED_LOCK（save 内部无锁）
                    desc = sc.get("description", "")
                    if desc:
                        job = _new_job("auto", "⏰ 定时任务：" + desc[:40], description=desc)
                        threading.Thread(target=run_auto_job,
                                         args=(job, desc, None, 2, None, "", ""),
                                         daemon=True).start()
        except Exception:
            pass


def _persist_jobs():
    try:
        slim = {jid: {"id": j.get("id"), "kind": j.get("kind"), "title": j.get("title"),
                      "description": j.get("description", ""), "status": j.get("status"),
                      "summary": j.get("summary"), "error": j.get("error"), "url": j.get("url", ""),
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
            # 淘汰最旧的已完成/失败任务，绝不淘汰运行中的任务（否则不可轮询/停止）
            done_keys = [k for k, j in JOBS.items() if j.get("status") != "running"]
            evict_n = len(JOBS) - JOBS_MAX
            for k in done_keys[:evict_n]:
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
    # 0 条/无正文也是「未完成」，必须附上确定性解决方案，不能只靠前端猜。
    try:
        _result = result or {}
        _summary = str(summary or "")
        _has_zero = (isinstance(_result, dict) and "total" in _result
                     and not _result.get("total")) or bool(
            re.search(r"(?<![0-9])0\s*条|页面无正文|无正文|没有匹配|未找到", _summary, re.I))
        if _has_zero:
            from .solutions import attach_solution
            attach_solution(job, _summary + " " + " ".join((job.get("messages") or [])[-5:]))
            _persist_jobs()
    except Exception:
        pass


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
            _wait = int(time.time() - t0)
            _hint = ""
            if _wait > 180:
                _hint = "｜⚠️ 长时间无进展，疑似反爬/验证/登录拦截——可点停止，按失败卡片或「🚀 打开调试Chrome」操作后重跑"
            _job_log(job, f"⏳ 仍在{key}（已等待 {_wait} 秒）{_hint}")
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
        # 记录 task_dir（供 AI 诊断读取 config）：直跑路径也能从 name 推导
        try:
            _td = str(out.get("task_dir") or "")
            if not _td:
                _n = str(out.get("name") or "")
                if _n:
                    _td = str(ROOT / "tasks" / _n)
            if _td:
                job["task_dir"] = _td
        except Exception:
            pass
        # 通用引擎成功时 files 只在顶层（result 里没有）——合并进 result，
        # 前端的「数据预览 / 复制路径 / 打开文件夹」都依赖 result.files
        result = dict(out.get("result") or {})
        if out.get("files"):
            result.setdefault("files", out["files"])
        _job_done(job, result, out.get("summary"), out.get("verify"))
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
            total = len(r.get("rows") or [])
            summary = (f"✅ 精配生成成功（{r.get('kind')}）：{r.get('detail')}，"
                       f"导出 {list(files.values())}；现在可直接重跑原任务")
            result = dict(r)
            result["total"] = total
            result["files"] = files
            _job_done(job, result, summary, verify=r.get("rows") or None)
        else:
            _job_error(job, f"精配试跑未成功：{r.get('error')}（未保存/已清理失败配置，可重新自动精配或换入口再试）")
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            _job_error(job, "任务已被手动停止（KeyboardInterrupt）")
        else:
            _job_error(job, f"{type(e).__name__}: {e}")


def _export_rows(rows: list, base: str) -> Dict[str, str]:
    """把通用 rows 导出为 outputs/<base>.{json,csv,xlsx}，失败不抛给任务线程。"""
    files = {}
    try:
        out_dir = ROOT / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        fp = out_dir / f"{base}.json"
        fp.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        files["json"] = f"outputs/{base}.json"
        keys = [k for k in rows[0].keys() if k != "_url"]
        with open(out_dir / f"{base}.csv", "w", newline="", encoding="utf-8-sig") as f:
            import csv as _csv
            w = _csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows([{k: v for k, v in r.items() if k != "_url"} for r in rows])
        files["csv"] = f"outputs/{base}.csv"
        try:
            from openpyxl import Workbook
            wb = Workbook(); ws = wb.active
            ws.append(keys)
            for r in rows:
                ws.append([r.get(k, "") for k in keys])
            wb.save(out_dir / f"{base}.xlsx")
            files["xlsx"] = f"outputs/{base}.xlsx"
        except Exception:
            pass
    except Exception:
        pass
    return files


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
        # 普通 URL 只请求一次：先 fetch_url，再用返回的原始 HTML 判断 PDF/附件，避免重复 GET。
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

        # PDF 直链 / 页面含 PDF 附件 → 通用 PDF 解析（未精配站也能直接出表格）
        if mode in ("auto", "table", "article"):
            try:
                from .pdf_table import is_attachment_url, attachment_to_rows, extract_pdf_links
                _pdfs: list = []
                if is_attachment_url(url):
                    _pdfs = [url]
                else:
                    _pdfs = extract_pdf_links(r.get("text") or "", url)
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
                        _suffix = _hl.md5("|".join(_pdfs[:5]).encode(), usedforsecurity=False).hexdigest()[:8]
                        base = f"pdf_{_host}_{_suffix}"
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
        status = r.get("status") or 0
        # 正文/自动模式：绝不把原始 HTML 当“正文”。explicit article 为空时用 markdown/HTML→Markdown 兜底。
        text = r.get("article") or r.get("markdown") or ""
        if not text.strip():
            from .extractors import html_to_markdown
            text = html_to_markdown(r.get("text") or "", base_url=url)
        links = r.get("links") or []
        tables = r.get("tables") or []
        import hashlib as _hash
        base = f"paste_{_hash.md5(url.encode(), usedforsecurity=False).hexdigest()[:10]}"

        if mode == "links":
            rows = [{"link": str(u), "_url": url} for u in links]
            files = _export_rows(rows, base) if rows else {}
            summary = (f"✅ 任务结束：收集 {len(rows)} 个链接（HTTP {status}）"
                       if rows else "⚠️ 任务结束：未发现链接（可能需浏览器渲染或页面无外链）")
            _job_done(job, {"total": len(rows), "fetched": 1, "errors": 0,
                            "status": status, "len": len(text), "files": files},
                      summary, _auto_verify(rows, None))
            return

        if mode == "table":
            rows = [row for table_rows in tables for row in table_rows]
            files = _export_rows(rows, base) if rows else {}
            summary = (f"✅ 任务结束：提取表格 {len(rows)} 行（HTTP {status}）"
                       if rows else "⚠️ 任务结束：未发现表格（可能需浏览器渲染或页面结构变化）")
            _job_done(job, {"total": len(rows), "fetched": 1, "errors": 0,
                            "status": status, "len": len(text), "files": files},
                      summary, _auto_verify(rows, None))
            return

        rows = [{"_url": url, "content": text[:20000]}] if text.strip() else []
        files = _export_rows(rows, base) if rows else {}
        _job_log(job, f"✅ 抓取完成：状态 {status}，内容 {len(text)} 字符")
        if text.strip():
            summary = f"✅ 任务结束：抓取成功（HTTP {status}，正文 {len(text)} 字符）"
        else:
            summary = "⚠️ 任务结束：页面无正文（可能需浏览器渲染或需要登录）"
        _job_done(job, {"total": len(rows), "fetched": 1, "errors": 0,
                        "status": status, "len": len(text), "files": files},
                  summary, _auto_verify(rows, None))
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            _job_error(job, "任务已被手动停止（KeyboardInterrupt）")
        else:
            _job_error(job, f"{type(e).__name__}: {e}")


def run_books_job(job: dict, spec_src: str, out_dir: str, download_covers: bool, interval: float):
    """图书目录采集：豆瓣详情+比价。0 条/部分失败必须带诊断与行动方案，不假成功。"""
    try:
        text = (spec_src or "").strip()
        if not text:
            _job_error(job, "请先粘贴书籍清单 spec JSON（可在「📚 图书目录」点「填入示例」快速开始）")
            return
        spec = None
        # 也接受本地 .json 文件路径：先按输入路径找，找不到再按项目根重试
        if text.endswith(".json"):
            p = Path(text).expanduser()
            if not p.is_absolute() and not p.exists():
                p2 = ROOT / p
                if p2.exists():
                    p = p2
            try:
                spec = json.loads(p.read_text(encoding="utf-8"))
                _job_log(job, f"📖 已从文件读取 spec：{p}")
            except Exception as e:
                _job_error(job, f"spec 文件读取/解析失败：{type(e).__name__}: {e}"
                                f"（文件：{p}；可改为直接粘贴 JSON 内容）")
                return
        if spec is None:
            try:
                spec = json.loads(text)
            except json.JSONDecodeError as e:
                _job_error(job, f"spec 不是合法 JSON：{e}；请对照示例修改后重试"
                                f"（需要 {{\"books\":[{{\"title\":\"书名\",\"isbn\":\"合法ISBN\"}}]}}）")
                return
        from .book_catalog import build_catalog
        _job_log(job, "📚 开始采集图书目录（豆瓣+京东/当当比价，只采书目不下载正文）...")
        try:
            iv = float(interval)
        except (TypeError, ValueError):
            iv = 1.0
        if iv != iv:  # NaN 必须在钳制前拦下
            iv = 1.0
        stop_flag = job.get("cancel_event")
        if not isinstance(stop_flag, threading.Event):
            stop_flag = threading.Event()
            job["cancel_event"] = stop_flag
        res = build_catalog(spec, out_dir or "outputs/book_catalog",
                            download_covers=download_covers,
                            min_interval=min(max(iv, 0.0), 10.0),
                            log=lambda m: _job_log(job, f"📚 {m}"),
                            should_stop=stop_flag.is_set)
        rows = res.get("rows") or []
        status = res.get("status") or ""
        files = res.get("files") or {}
        bad_rows = [r for r in rows if r.get("status") != "OK"]
        dup_skipped = int((res.get("coverage") or {}).get("duplicates") or 0)
        if rows and dup_skipped:
            # 重复 ISBN 被跳过必须可见，不能静默少行
            _job_log(job, f"🔁 {dup_skipped} 个重复 ISBN 条目已按主键去重跳过（同一版本只保留一行）")
        result_out = {
            "total": len(rows),
            "fetched": len(rows),
            "errors": len(bad_rows),
            "status": status,
            "files": {k: v for k, v in files.items()},
            "coverage": res.get("coverage"),
            "diagnostics": [
                {"isbn": d.get("isbn"), "row_status": d.get("status"), "diagnostics": d.get("diagnostics")}
                for d in (res.get("diagnostics") or []) if d.get("status") != "OK"
            ][:50],
        }
        if status == "INVALID_SPEC":
            _job_error(job, f'图书目录采集失败——这不是成功：{res.get("error") or "spec 不合法"}'
                            f'。正确结构：{{"books":[{{"title":"书名","isbn":"合法 ISBN-10/13"}}]}}')
            return
        if not rows:
            if isinstance(stop_flag, threading.Event) and stop_flag.is_set():
                _job_error(job, "⏹ 任务在采到任何书目之前就被手动停止：0 行结果（导出文件为空表）。"
                                "这不是成功；如需继续请重新提交任务")
                return
            dup = int((res.get("coverage") or {}).get("duplicates") or 0)
            reason = (f"{dup} 个条目全是重复 ISBN（主键去重后无剩余），请检查书单是否重复列了同一版本"
                      if dup else "spec.books 为空")
            _job_error(job, f"图书目录采集失败——0 条结果（{reason}）。这不是成功，请修正书单后重跑")
            return
        if all(r.get("status") == "NO_DATA" for r in rows):
            _job_error(job,
                       f"图书目录采集失败——0 条有效结果（共 {len(rows)} 行，豆瓣/京东/当当均未命中）。"
                       f"逐项诊断与行动方案见 {files.get('log', 'crawl_log.md')}；"
                       "常见处理：核对 ISBN、补充 douban_subject_id、调大请求间隔防限流后重跑")
            return
        # 用户中途停止：如实说明只含部分结果，绝不冒充完整书单
        if isinstance(stop_flag, threading.Event) and stop_flag.is_set() and res.get("stopped"):
            result_out["stopped"] = True
            head = f"⏹ 任务已被手动停止：{len(rows)} 本已完成并照常导出，书单中剩余书目未抓取"
            tail = (f"，其中 {len(bad_rows)} 本有字段缺失/来源拦截（诊断见 "
                    f"{files.get('log', 'crawl_log.md')}）" if bad_rows else "")
            _job_done(job, result_out, head + tail, verify=_auto_verify(rows, None))
            return
        if bad_rows:
            codes = {}
            for r in bad_rows:
                # 结构化字段级诊断统计次数（×N=出现的诊断处数），不解析人类可读文本
                for d in (r.get("diagnostic_fields") or []):
                    c = str(d.get("code") or "").strip()
                    if c and c != "N/A":
                        codes[c] = codes.get(c, 0) + 1
            code_str = "/".join(f"{c}×{n}" for c, n in sorted(codes.items())) or "详见日志"
            summary = (f"⚠️ 任务结束：{len(rows)} 本书完成，其中 {len(bad_rows)} 本未全字段成功"
                       f"（来源提示：{code_str}）。"
                       f"每本书的逐项诊断与行动方案见 {files.get('log', 'crawl_log.md')}，全部行已导出")
            _job_done(job, result_out, summary, verify=_auto_verify(rows, None))
            return
        coverage = res.get("coverage") or {}
        summary = (f"✅ 任务结束：{len(rows)} 本书目录采集成功（必填缺失率 "
                   f"{coverage.get('required_missing_rate', 'N/A')}），导出 {list(files.values())}")
        _job_done(job, result_out, summary, verify=_auto_verify(rows, None))
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            _job_error(job, "任务已被手动停止（KeyboardInterrupt）")
        else:
            _job_error(job, f"{type(e).__name__}: {e}")
    finally:
        # cancel_event 是 threading.Event，不可 JSON 序列化，不能进持久化历史
        job.pop("cancel_event", None)


def run_batch_job(job: dict, urls: list, mode: str = "auto", browser: bool = False):
    """批量网址抓取：逐个 URL 抓取（精配优先，普通网页兜底），合并导出。"""
    try:
        _job_log(job, f"📚 批量抓取开始：{len(urls)} 个网址（并发 4）")
        rows_all = []
        errs = 0

        def _grab(u):
            try:
                from .sites import match_site, run_site
                _site = match_site(u)
                if _site:
                    r = run_site(u, limit=0)
                    if r.get("rows"):
                        return {"ok": True, "rows": r["rows"], "site": _site, "note": f"{len(r['rows'])} 条"}
                    return {"ok": False, "error": (r.get('error') or '0 条')[:100], "site": _site}
                from .quick import fetch_url
                fr = fetch_url(u, browser=browser, timeout=60, article=True)
                text = fr.get("article") or fr.get("markdown") or fr.get("text") or ""
                if text.strip():
                    return {"ok": True, "rows": [{"_url": u, "content": text[:20000]}],
                            "note": f"{len(text)} 字符"}
                return {"ok": False, "error": (fr.get('error') or '无内容')[:100]}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:90]}"}

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_grab, u): u for u in urls}
            for fut in as_completed(futs):
                u = futs[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    r = {"ok": False, "error": str(e)[:100]}
                if r.get("ok"):
                    rows_all.extend(r["rows"])
                    _job_log(job, f"✅ {u[:60]}：{r.get('note','')}")
                else:
                    errs += 1
                    _job_log(job, f"⚠️ {u[:60]}：{r.get('error','')[:80]}")
        _job_log(job, f"📚 批量完成：成功 {len(urls)-errs}/{len(urls)}，共 {len(rows_all)} 条")
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
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 客户端提前断开（关页面/切网络）：静默忽略，不打印吓人的 traceback
            try:
                self.close_connection = True
            except Exception:
                pass

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

    def _origin_ok(self) -> bool:
        """CSRF 防护：POST 必须同源。
        无 Origin（curl/CLI/非浏览器）放行；浏览器跨站 POST 必带 Origin，host 不一致则拒绝。"""
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        try:
            from urllib.parse import urlparse
            return urlparse(origin).netloc == host
        except Exception:
            return False

    def do_GET(self):
        # 页面/静态资源不鉴权（否则用户连输入令牌的页面都打不开）；仅 /api/* 需要
        if self.path.startswith("/api/") and not self._auth_ok(self.headers):
            self._send(403, "Forbidden: 需要 X-Auth-Token")
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path.startswith("/reports/"):
                # 路径穿越防护：只允许 outputs/reports 目录内（resolve 后校验前缀，拒绝绝对路径/..）
                try:
                    _rp_root = (ROOT / "outputs" / "reports").resolve()
                    fp = (_rp_root / u.path[len("/reports/"):]).resolve()
                    fp.relative_to(_rp_root)
                except Exception:
                    self._send(403, "非法路径")
                    return
                if fp.is_file() and fp.exists():
                    data = fp.read_bytes()
                    ctype = "text/html; charset=utf-8" if fp.suffix == ".html" else "text/plain"
                    self._send(200, data, ctype)
                else:
                    self._send(404, "报告不存在")
                return
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
                # cancel_event 是线程对象，不能出现在 API 响应里
                snap.pop("cancel_event", None)
                self._json(snap)
            elif u.path == "/api/jobs":
                with JOBS_LOCK:
                    lst = [{"id": j["id"], "kind": j["kind"], "title": j["title"],
                            "status": j["status"], "summary": j["summary"],
                            "url": j.get("url", ""), "description": j.get("description", ""),
                            "created": j["created"]} for j in reversed(list(JOBS.values()))][:20]
                self._json(lst)
            elif u.path == "/api/schedule/list":
                self._json({"ok": True, "schedules": _load_schedules()})
            elif u.path == "/api/books/example":
                fp = ROOT / "examples" / "books.spec.json"
                if fp.exists():
                    self._json({"ok": True, "spec": fp.read_text(encoding="utf-8")})
                else:
                    self._json({"error": "示例文件不存在：examples/books.spec.json"})
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
            elif u.path == "/api/settings":
                st = dict(_load_settings())
                out = {k: (v if k != "api_key" and k != "vision_api_key" else _mask_key(v))
                       for k, v in st.items()}
                out["_masked"] = {"api_key": bool(st.get("api_key")), "vision_api_key": bool(st.get("vision_api_key"))}
                self._json(out)
            elif u.path == "/api/status":
                _st_host = os.environ.get("US_WEBUI_HOST", "127.0.0.1")
                _st_lan = _lan_urls(int(os.environ.get("US_WEBUI_PORT", "8642"))) if _st_host == "0.0.0.0" else []
                self._json({
                    "share": bool(AUTH_TOKEN),
                    "token": AUTH_TOKEN or "",
                    "port": int(os.environ.get("US_WEBUI_PORT", "8642")),
                    "host": _st_host,
                    "lan_url": _st_lan[0] if _st_lan else "",
                    "llm_model": os.environ.get("LLM_MODEL", "qwen3.7-plus"),
                    "llm_base_url": os.environ.get("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                })
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
            elif u.path == "/api/preview":
                # 小白友好：任务完成后直接在页面里看到抓到的数据长什么样（不用会找文件/开 Excel）
                name = q.get("file", [""])[0]
                try:
                    rows_n = max(1, min(int(q.get("rows", ["20"])[0] or 20), 100))
                except ValueError:
                    rows_n = 20
                try:
                    fp = Path(name).expanduser()
                    if not fp.is_absolute():
                        # 兼容两种相对写法："outputs/x.json"（工具根基准）与裸 "x.json"（outputs 基准）
                        fp = (ROOT / fp) if fp.parts[:1] == ("outputs",) else (ROOT / "outputs" / fp)
                    fp = fp.resolve()
                    fp.relative_to((ROOT / "outputs").resolve())
                except Exception:
                    self._json({"error": "非法文件路径（仅允许 outputs 目录内）"})
                    return
                if not fp.exists():
                    self._json({"error": f"文件不存在: {name}"})
                    return
                # 大文件护栏：预览是轻量功能（只展示 20 行），超大导出直接引导用文件
                # 本身查看，避免整文件解析把内存放大数倍 + 占住请求线程
                try:
                    if fp.stat().st_size > 20 * 1024 * 1024:
                        self._json({"error": "文件太大（>20MB），预览只支持小文件；请点「📂 打开所在文件夹」用 Excel/WPS 查看完整数据"})
                        return
                except OSError as e:
                    self._json({"error": f"读取文件信息失败: {e}"})
                    return
                if fp.suffix.lower() == ".json":
                    try:
                        data = json.loads(fp.read_text(encoding="utf-8"))
                    except Exception as e:
                        self._json({"error": f"JSON 解析失败: {e}"})
                        return
                    if isinstance(data, dict) and isinstance(data.get("rows"), list):
                        rows, total = data["rows"], data.get("total", len(data["rows"]))
                    elif isinstance(data, list):
                        rows, total = data, len(data)
                    else:
                        rows, total = [data], 1
                elif fp.suffix.lower() == ".csv":
                    import csv as _csv
                    try:
                        with fp.open(newline="", encoding="utf-8-sig") as f:
                            rows = list(_csv.DictReader(f))
                        total = len(rows)
                    except Exception as e:
                        self._json({"error": f"CSV 解析失败: {e}"})
                        return
                else:
                    self._json({"error": "预览仅支持 .json / .csv 导出文件"})
                    return
                dict_rows = [r for r in rows if isinstance(r, dict)]
                if not dict_rows:
                    # 空文件不是错误：如实返回 0 条（配合 0 条诊断，不假成功也不吓人）
                    self._json({"ok": True, "total": total, "columns": [], "rows": [],
                                "empty": True})
                    return
                all_cols: list = []
                for r in dict_rows[:500]:  # 列扫描只采样前 500 行，足够覆盖取列
                    for k in r:
                        if k not in all_cols:
                            all_cols.append(k)
                # 内部字段（_url/_site 等）对小白是噪音：优先隐藏；全都是内部字段才原样显示
                user_cols = [c for c in all_cols if not str(c).startswith("_")]
                cols = (user_cols or all_cols)[:12]

                def _cell(v):
                    s = "" if v is None else str(v)
                    return s[:120] + ("…" if len(s) > 120 else "")

                self._json({
                    "ok": True, "total": total,
                    "columns": cols,
                    "rows": [{k: _cell(r.get(k)) for k in cols} for r in dict_rows[:rows_n]],
                    "truncated_cols": max(0, len(user_cols or all_cols) - len(cols)),
                })
            else:
                self._send(404, "not found")
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not self._auth_ok(self.headers):
            self._send(403, "Forbidden: 需要 X-Auth-Token")
            return
        if not self._origin_ok():
            self._send(403, "跨域请求被拒绝（CSRF 防护）")
            return
        u = urllib.parse.urlparse(self.path)
        try:
            body = self._read_body()
            if u.path == "/api/reveal":
                # 小白友好：一键在系统文件管理器里定位导出文件（不需要会用终端/找目录）
                raw = str(body.get("path", "") or "")
                try:
                    p = Path(raw).expanduser()
                    if not p.is_absolute():
                        p = (ROOT / p) if p.parts[:1] == ("outputs",) else (ROOT / "outputs" / p)
                    p = p.resolve()
                    p.relative_to((ROOT / "outputs").resolve())
                except Exception:
                    self._json({"ok": False, "error": "非法路径（仅允许 outputs 目录内）"})
                    return
                if not p.exists():
                    self._json({"ok": False, "error": f"文件不存在: {raw}"})
                    return
                if AUTH_TOKEN:
                    self._json({"ok": False,
                                "error": "分享模式下此按钮会在服务器那台电脑上打开文件夹，已禁用；"
                                         "请让使用者在自己电脑上查看下载的导出文件"})
                    return
                import subprocess as _sp
                import sys as _sys
                try:
                    if _sys.platform == "darwin":
                        _sp.run(["open", "-R", str(p)] if p.is_file() else ["open", str(p)],
                                check=False)
                    elif _sys.platform.startswith("win"):
                        # explorer 的 /select,<路径> 必须是单个参数，拆开会被忽略
                        _sp.run(["explorer", f"/select,{p}"] if p.is_file()
                                else ["explorer", str(p)], check=False)
                    else:
                        _sp.run(["xdg-open", str(p.parent if p.is_file() else p)], check=False)
                    self._json({"ok": True,
                                "message": "已在系统的文件管理器中定位该文件"})
                except Exception as e:
                    self._json({"ok": False, "error": f"打开失败: {type(e).__name__}: {e}"})
                return
            if u.path == "/api/report":
                content = str(body.get("content", "") or "").strip()
                if len(content) > 5_000_000:
                    self._json({"error": "CSV 太大（>5MB）"})
                    return
                if not content:
                    self._json({"error": "请粘贴 CSV 内容"})
                    return
                name = str(body.get("name", "") or "report").strip() or "report"
                # 路径穿越防护：只允许英文/数字/下划线/连字符（文件名不得含 / 和 ..）
                name = re.sub(r"[^A-Za-z0-9_-]", "", name)[:80] or "report"
                group = str(body.get("group", "") or "").strip() or None
                rdir = ROOT / "outputs" / "reports"
                rdir.mkdir(parents=True, exist_ok=True)
                csv_path = rdir / f"{name}.csv"
                csv_path.write_text(content, encoding="utf-8-sig")
                from .report import generate as _report_gen
                try:
                    r = _report_gen(str(csv_path), group_col=group, out=str(rdir / f"{name}.html"))
                    self._json({"ok": True, "report": f"/reports/{name}.html", "stats": r})
                except Exception as e:
                    self._json({"error": f"生成失败: {e}"})
                return
            if u.path == "/api/settings":
                if body.get("reset"):
                    # 恢复默认：清空持久化配置 + 从 env 移除（回落 QWEN_API_KEY 等默认）
                    _save_settings({})
                    for _env in _LLM_ENV_KEYS.values():
                        os.environ.pop(_env, None)
                    self._json({"ok": True, "message": "已恢复默认（清空保存的 AI 配置）"})
                    return
                allowed = {k: str(body.get(k) or "").strip() for k in _LLM_ENV_KEYS}
                _save_settings(allowed)
                _apply_settings_to_env(allowed)
                self._json({"ok": True, "message": "AI 配置已保存并生效"})
                return
            if u.path == "/api/settings/test":
                model = str(body.get("model") or os.environ.get("LLM_MODEL", "qwen3.7-plus")).strip()
                base_url = str(body.get("base_url") or os.environ.get("OPENAI_BASE_URL", "")).strip()
                api_key = str(body.get("api_key") or os.environ.get("OPENAI_API_KEY", "")).strip()
                try:
                    from .llm import LLMClient
                    c = LLMClient(model=model or None, base_url=base_url or None, api_key=api_key or None)
                    r = c.chat([{"role": "user", "content": "只回复：OK"}], temperature=0.1)
                    self._json({"ok": True, "message": f"✅ 连通成功：{model} 返回「{str(r)[:30]}」"})
                except Exception as e:
                    self._json({"ok": False, "message": f"❌ 连通失败：{type(e).__name__}: {str(e)[:120]}"})
                return
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
                # AI 人话指南（小白友好）
                try:
                    from .auto import human_guide
                    plan["guide"] = human_guide(desc, plan.get("config") or {})
                except Exception:
                    plan["guide"] = {}
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
                return
            if u.path == "/api/auto/diagnose":
                task_dir = str(body.get("task_dir", "") or "").strip()
                desc = str(body.get("description", "") or "").strip()
                log_text = str(body.get("log", "") or "")[:4000]
                cfg = {}
                candidates = []
                if task_dir:
                    # 只允许 tasks/ 目录内的 config（防分享模式读任意路径配置）
                    try:
                        _tasks_root = (ROOT / "tasks").resolve()
                        _p = (Path(task_dir) if Path(task_dir).is_absolute() else ROOT / task_dir).resolve()
                        _p.relative_to(_tasks_root)
                        candidates.append(_p)
                    except Exception:
                        pass
                # 直跑路径兜底：tasks/auto_<md5(desc)[:10]>
                if desc and not task_dir:
                    import hashlib as _hl
                    candidates.append((ROOT / "tasks" / f"auto_{_hl.md5(desc.encode(), usedforsecurity=False).hexdigest()[:10]}").resolve())
                for _p in candidates:
                    try:
                        _cfg_f = _p / "config.json"
                        if _cfg_f.exists():
                            cfg = json.loads(_cfg_f.read_text(encoding="utf-8"))
                            break
                    except Exception:
                        pass
                from .auto import diagnose_failure
                try:
                    d = diagnose_failure(desc, cfg, log_text)
                    self._json({"ok": True, **d})
                except Exception as e:
                    self._json({"error": f"诊断失败: {e}"})
                return
            if u.path == "/api/auto/start":
                desc = str(body.get("description", "")).strip()
                if not desc:
                    self._json({"error": "请先描述任务"})
                    return
                config = body.get("config")
                name = str(body.get("name", "") or "").strip()
                task_dir = str(body.get("task_dir", "") or "").strip()
                if config:
                    # 安全：task_dir 只允许 tasks/ 下（防路径穿越写任意文件，分享模式=远程RCE风险）
                    import re as _re
                    import hashlib as _hl
                    if not name or not _re.fullmatch(r"[A-Za-z0-9_\-]+", name):
                        # 缺 name / 非法 name：用描述哈希安全推导（绝不落到 CWD/根目录）
                        name = f"auto_{_hl.md5(desc.encode(), usedforsecurity=False).hexdigest()[:10]}"
                    if not task_dir:
                        # 缺 task_dir：安全推导到 tasks/auto_<md5>，防止 run_with_config 写到 CWD
                        task_dir = str((ROOT / "tasks" / name).resolve())
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
            elif u.path == "/api/schedule/add":
                desc = str(body.get("description", "")).strip()
                interval = float(body.get("interval_hours") or 0)
                if not desc:
                    self._json({"error": "请提供任务描述"})
                    return
                if interval <= 0:
                    interval = 24
                with SCHED_LOCK:
                    scheds = _load_schedules()
                    scheds.append({"id": uuid.uuid4().hex[:8], "description": desc,
                                   "interval_hours": interval, "next_run": time.time() + interval * 3600,
                                   "last_run": None, "enabled": True, "created": time.time()})
                    _save_schedules(scheds)
                self._json({"ok": True, "message": f"已设为每 {interval:.0f} 小时自动跑一次"})
            elif u.path == "/api/schedule/delete":
                sid = str(body.get("id", "")).strip()
                with SCHED_LOCK:
                    _save_schedules([x for x in _load_schedules() if x.get("id") != sid])
                self._json({"ok": True})
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
                        with _ur.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
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
                    # host/port 是 serve() 的形参，这里读它启动时写入的 env（/api/status 同款）
                    _host = os.environ.get("US_WEBUI_HOST", "127.0.0.1")
                    _port = os.environ.get("US_WEBUI_PORT", "8642")
                    if _host != "127.0.0.1":
                        _cmd += ["--host", str(_host)]
                    _cmd += ["--port", str(_port)]
                    if os.environ.get("US_WEBUI_SHARE") == "1":
                        _cmd += ["--share"]  # 分享模式重启后保持局域网可访问
                    if AUTH_TOKEN:
                        _cmd += ["--token", AUTH_TOKEN]
                    _sp.Popen(_cmd, cwd=str(ROOT), start_new_session=True,
                              stdout=open(ROOT / "outputs" / "webui_restart.log", "a"),
                              stderr=_sp.STDOUT)
                    self._json({"ok": True, "message": "重启中，3 秒后自动恢复…"})
                    threading.Timer(0.5, os._exit, args=(0,)).start()
                except Exception as e:
                    self._json({"ok": False, "message": f"重启失败：{type(e).__name__}: {e}"})
                return
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
                    from .core import assert_http_url
                    try:
                        assert_http_url(url or "https://www.baidu.com/")
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
                    cancel_ev = job.get("cancel_event") if job else None
                # 图书目录等内存协作停止：置位 cancel flag，当前书采完后保存结果退出。
                # 不要求未 set：重复点击保持幂等，返回同一语义的成功而不是误导性错误。
                if isinstance(cancel_ev, threading.Event):
                    first = not cancel_ev.is_set()
                    cancel_ev.set()
                    if job and first:
                        with JOBS_LOCK:
                            job["messages"].append("⏹ 已发送停止信号：当前这本书抓完后将保存已有结果并退出...")
                    self._json({"ok": True,
                                "message": ("已发送停止信号（图书任务会在当前书目完成后保存结果并退出）"
                                            if first else "停止信号已发送，正在等当前书目完成后保存退出")})
                    return
                td = td or ""
                # 无 task_dir（auto_task 直跑路径）：用描述哈希定位 tasks/auto_<md5[:10]>
                if not td and desc:
                    try:
                        import hashlib as _hl
                        _n = f"auto_{_hl.md5(desc.encode(), usedforsecurity=False).hexdigest()[:10]}"
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
                    self._json({"ok": True, "message": "已发送停止信号"})
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
            elif u.path == "/api/books/start":
                spec_src = str(body.get("spec", "") or "").strip()
                if not spec_src:
                    self._json({"error": "请先粘贴书籍清单 spec JSON，或填 .json 文件路径（可点「填入示例」）"})
                    return
                # 安全：输出目录限定在 outputs/ 内（与 /api/verify 同口径；防分享模式远程投递文件到源码/config 目录）
                try:
                    _od = Path(str(body.get("out", "") or "").strip()
                               or "outputs/book_catalog").expanduser()
                    if not _od.is_absolute():
                        _od = ROOT / _od
                    _od = _od.resolve()
                    _od.relative_to((ROOT / "outputs").resolve())
                except Exception:
                    self._json({"error": "非法输出目录（仅允许 outputs 目录内，如 outputs/book_catalog）"})
                    return
                out_dir = str(_od)
                covers = bool(body.get("covers", True))
                try:
                    iv = float(body.get("interval", 1.0))
                except (TypeError, ValueError):
                    iv = 1.0
                if iv != iv:  # NaN 必须拦下，否则 max/min 会把它折成 0 秒节流
                    iv = 1.0
                # 注意不用 `or 1.0`：显式传 0（离线/快速模式）必须保留
                interval = min(max(iv, 0.0), 10.0)
                job = _new_job("books", f"📚 图书目录采集（{out_dir}）")
                threading.Thread(target=run_books_job,
                                 args=(job, spec_src, out_dir, covers, interval),
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
                job["url"] = url  # 重跑需要完整 URL（title 仅截断 50 字符）
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
    # ⚙️ 启动时加载持久化 AI 配置（用户上次在界面里配置的模型/接口/Key 自动生效）
    try:
        _apply_settings_to_env(_load_settings())
    except Exception:
        pass
    os.environ["US_WEBUI_PORT"] = str(port)
    os.environ["US_WEBUI_HOST"] = host
    _load_jobs()
    threading.Thread(target=_scheduler_loop, daemon=True).start()  # ⏰ 定时任务调度
    _CODE_FP_START = _code_fingerprint()
    PY = sys.executable
    AUTH_TOKEN = token or os.environ.get("US_WEBUI_TOKEN", "")
    print("🕷️ 万能爬虫工具 · 可视化版 v3", flush=True)
    if share:
        host = "0.0.0.0"
        # 同步 env：/api/restart 重建进程时要带上同样的绑定与 --share
        os.environ["US_WEBUI_HOST"] = "0.0.0.0"
        os.environ["US_WEBUI_SHARE"] = "1"
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
