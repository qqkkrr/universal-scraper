#!/usr/bin/env python3
"""配置驱动的爬虫执行引擎（v2）。

取数(HTTP/浏览器/桥) → 字段映射 → 流水线(过滤/去重/清洗)
→ 详情展开(并发/断点) → 文件下载 → 导出
支持：中间件钩子、断点续跑(--resume)、增量去重、sitemap、代理轮换、优雅中断。
"""
from __future__ import annotations

import re
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core import export_rows, log, die
from .selectors import jpath, apply_extractor, regex_extract_all
from .fetchers import HttpFetcher, BrowserScriptFetcher, BrowserFetcher, _resolve_template
from .log import Logger
from .config import validate as validate_config
from .middleware import MiddlewareChain
from .proxy import ProxyPool
from .storage import Checkpoint, SeenStore, record_key

FETCHERS = {
    "http_json": HttpFetcher,
    "http_html": HttpFetcher,
    "browser_script": BrowserScriptFetcher,
    "browser": BrowserFetcher,
}


def map_record(raw: Dict[str, Any], fields: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not isinstance(fields, dict):
        # 盒马战例：record.fields 写成 list，validate 之外这里必须兜住——丢映射不丢数据
        if fields:
            log(f"⚠️ record.fields 应为 dict（当前 {type(fields).__name__}），"
                "已跳过映射、保留原始字段——请修正配置")
        return dict(raw)
    for name, spec in fields.items():
        if isinstance(spec, str):
            out[name] = jpath(raw, spec, None)
        elif isinstance(spec, dict):
            if "from" in spec:
                out[name] = jpath(raw, spec["from"], spec.get("default"))
            elif "path" in spec:
                out[name] = jpath(raw, spec["path"], spec.get("default"))
            elif "constant" in spec:
                out[name] = spec["constant"]
            elif "template" in spec:
                try:
                    out[name] = spec["template"].format(**{k: (v if v is not None else "") for k, v in raw.items()})
                except Exception:
                    out[name] = ""
            elif "concat" in spec:
                out[name] = "".join(str(jpath(raw, p, "")) for p in spec["concat"])
            else:
                out[name] = jpath(raw, spec.get("path", ""), None)
        else:
            out[name] = spec
    return out


def run_pipeline(rows: List[Dict[str, Any]], pipeline: List[Dict[str, Any]], log_prefix: str = "") -> List[Dict[str, Any]]:
    import re as _re
    for step in pipeline or []:
        st = step.get("type")
        if st == "filter":
            field, op, value = step["field"], step.get("op", "contains"), step.get("value")
            before = len(rows)
            # AI 配置防御：value 缺失/为 None 时按空串处理，避免 `None in str` 直接炸掉整单
            value = "" if value is None else str(value)
            if op == "contains":
                rows = [r for r in rows if value in str(r.get(field) or "")]
            elif op == "eq":
                rows = [r for r in rows if str(r.get(field)) == str(value)]
            elif op == "regex":
                pat = _re.compile(step.get("pattern", ""))
                rows = [r for r in rows if pat.search(str(r.get(field) or ""))]
            elif op == "not_contains":
                rows = [r for r in rows if value not in str(r.get(field) or "")]
            elif op == "between":
                try:
                    lo = float(step.get("min", float("-inf")))
                    hi = float(step.get("max", float("inf")))
                    rows = [r for r in rows if lo <= float(str(r.get(field)).replace(",", "")) < hi]
                except (ValueError, TypeError):
                    rows = []
            log(f"{log_prefix}filter[{field} {op} {value}]: {before} -> {len(rows)}")
        elif st == "dedup":
            key = step.get("key", "id")
            before = len(rows)
            seen = set(); uniq = []
            for r in rows:
                k = tuple(str(r.get(kk)) for kk in (key if isinstance(key, list) else [key]))
                if any(kk in ("None", "") for kk in k):
                    uniq.append(r)
                    continue
                if k not in seen:
                    seen.add(k); uniq.append(r)
            rows = uniq
            log(f"{log_prefix}dedup[{key}]: {before} -> {len(rows)}")
        elif st == "rename":
            for r in rows:
                for old, new in step.get("mapping", {}).items():
                    if old in r:
                        r[new] = r[old]; del r[old]
        elif st == "cast":
            field, ctype = step["field"], step.get("to", "str")
            for r in rows:
                v = r.get(field)
                try:
                    if ctype == "int" and v not in (None, ""):
                        r[field] = int(float(str(v).replace(",", "")))
                    elif ctype == "float" and v not in (None, ""):
                        r[field] = float(str(v).replace(",", ""))
                    elif ctype == "str":
                        r[field] = str(v) if v is not None else ""
                except (ValueError, TypeError):
                    pass
        elif st == "add":
            for r in rows:
                r[step["field"]] = step.get("value")
        elif st == "transform":
            # 字段变换：op=unix_to_datetime（秒/毫秒时间戳自适应）| upper | lower
            field, op = step["field"], step.get("op", "unix_to_datetime")
            fmt = step.get("fmt", "%Y-%m-%d %H:%M:%S")
            changed = 0
            for r in rows:
                v = r.get(field)
                if v in (None, ""):
                    continue
                try:
                    if op == "unix_to_datetime":
                        ts = float(str(v).strip())
                        if ts > 9_999_999_999:  # 毫秒时间戳
                            ts /= 1000.0
                        r[field] = time.strftime(fmt, time.localtime(ts))
                        changed += 1
                    elif op == "upper":
                        r[field] = str(v).upper(); changed += 1
                    elif op == "lower":
                        r[field] = str(v).lower(); changed += 1
                except (ValueError, TypeError, OSError):
                    pass
            log(f"{log_prefix}transform[{field} {op}]: {changed} 条已变换")
        elif st == "template":
            # 用已有字段拼新字段：tmpl 里 {字段名} 占位，如 "https://www.bilibili.com/video/{bvid}"
            name, tmpl = step["field"], step.get("tmpl", "")
            for r in rows:
                out = tmpl
                for k, v in r.items():
                    if "{" + str(k) + "}" in out:
                        out = out.replace("{" + str(k) + "}", str(v))
                r[name] = out
        else:
            # 未知/未实现类型必须可见（parse_date/split/download 等属 v3 任务包执行器），
            # 静默跳过会让用户以为生效了——零结果时排查不到原因
            log(f"{log_prefix}⚠️ pipeline 类型 '{st}' 在 run --config 执行器中未实现，已跳过")
    return rows


def _tmpl_value(v: Any, row: Dict[str, Any]) -> Any:
    """{字段名} 用 row 的值递归插值（str/dict/list 通吃）——detail POST body 用。"""
    if isinstance(v, str):
        out = v
        for k, val in row.items():
            if "{" + str(k) + "}" in out:
                out = out.replace("{" + str(k) + "}", str(val))
        return out
    if isinstance(v, dict):
        return {k: _tmpl_value(x, row) for k, x in v.items()}
    if isinstance(v, list):
        return [_tmpl_value(x, row) for x in v]
    return v


def fetch_detail_row(http, row: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    url = row.get(detail.get("url_field", "url")) or ""
    if not url:
        return row
    for tr in detail.get("url_transform", []):
        if "replace" in tr:
            old, new = tr["replace"][0], tr["replace"][1]
            url = url.replace(old, new)
        elif "prefix" in tr:
            url = tr["prefix"] + url
        elif "suffix" in tr:
            url = url + tr["suffix"]
    # 详情支持 POST（小米有品战例：评分/规格在 POST 网关里，body 用 {字段} 从列表行插值）
    method = str(detail.get("method", "GET")).upper()
    if method == "POST" and hasattr(http, "post"):
        body = None
        if detail.get("json_body") is not None:
            body = _tmpl_value(detail["json_body"], row)
        resp = http.post(url, json_data=body)
    else:
        resp = http.get(url, allow_html_404=detail.get("allow_html_404", True))
    html = resp.get("text", "")
    row[detail.get("url_field", "url") + "_final"] = url
    row["detail_status"] = str(resp.get("status"))
    # 详情返回 JSON 时 extract 走 type:json（jpath 点路径，含 [name=xx] 过滤）
    ctx_obj = None
    if detail.get("type") == "http_json":
        import json as _json
        try:
            ctx_obj = _json.loads(html)
        except Exception:
            ctx_obj = None
    for spec in detail.get("extract", []):
        row[spec["name"]] = apply_extractor(spec, html, html, ctx_obj)
    return row


def fetch_details(rows, detail, anti, checkpoint: Optional[Checkpoint] = None, logger: Optional[Logger] = None) -> List:
    if not detail.get("enabled"):
        return rows
    concurrency = int(detail.get("concurrency", 1))
    interval = float(detail.get("interval", 0.5))
    timeout = float(anti.get("timeout", 15))
    backend = anti.get("http_backend", "requests")
    from .core import RequestsClient, HttpClient
    if backend == "requests":
        try:
            http = RequestsClient(min_interval=interval, timeout=timeout,
                                  rotate_ua=anti.get("rotate_ua", True),
                                  proxy=anti.get("proxy"), cookies=anti.get("cookies"))
        except Exception:
            http = HttpClient(min_interval=interval, timeout=timeout,
                              use_system_proxy=anti.get("use_system_proxy", False))
    else:
        http = HttpClient(min_interval=interval, timeout=timeout,
                          use_system_proxy=anti.get("use_system_proxy", False))
    todo = [r for r in rows if not (r.get("detail_body") or "").strip()]
    done = len(rows) - len(todo)
    (logger or Logger()).info(f"详情：共 {len(rows)}，已有 {done}，待抓 {len(todo)}（并发 {concurrency}）")
    if not todo:
        return rows

    def _work(r):
        fetch_detail_row(http, r, detail)
        return r

    if concurrency <= 1:
        for i, r in enumerate(todo, 1):
            try:
                _work(r)
            except Exception as e:
                r["detail_status"] = f"ERR:{type(e).__name__}"
            if checkpoint and i % detail.get("checkpoint_every", 20) == 0:
                checkpoint.save(rows, done + i, len(rows))
            time.sleep(interval)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {ex.submit(_work, r): r for r in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                r = futs[fut]
                try:
                    fut.result()
                except Exception as e:
                    r["detail_status"] = f"ERR:{type(e).__name__}"
                if checkpoint and i % detail.get("checkpoint_every", 20) == 0:
                    checkpoint.save(rows, done + i, len(rows))
                time.sleep(interval / concurrency)
    return rows


def fetch_sitemap_urls(http, url: str, max_urls: int = 500,
                     _visited: Optional[set] = None, _depth: int = 0) -> List[str]:
    """抓 sitemap.xml（或 robots.txt 里指出的 sitemap），返回 <loc> URL 列表。
    带 visited 环检测 + 深度上限，防循环 sitemap index 导致 RecursionError。"""
    if _depth > 5:
        return []
    visited = set() if _visited is None else _visited
    if url in visited:
        return []
    visited.add(url)
    urls: List[str] = []
    if url.rstrip("/").endswith("robots.txt"):
        res = http.get(url)
        for m in regex_extract_all(res.get("text", ""), r"Sitemap:\s*(\S+)", 1):
            urls.extend(fetch_sitemap_urls(http, m, max_urls, visited, _depth + 1))
        return urls[:max_urls]
    res = http.get(url)
    html = res.get("text", "")
    # 普通 sitemap 或 sitemap index
    for m in regex_extract_all(html, r"<loc>\s*([^<]+?)\s*</loc>", 1):
        u = m.strip()
        if u.endswith(".xml") or "sitemap" in u.lower():
            urls.extend(fetch_sitemap_urls(http, u, max_urls, visited, _depth + 1))
        else:
            urls.append(u)
        if len(urls) >= max_urls:
            break
    return urls[:max_urls]


def download_files(rows, dl_cfg, anti, out_dir: Path, logger: Optional[Logger] = None) -> int:
    """按配置下载文件（附件/PDF 等）。"""
    if not dl_cfg or not dl_cfg.get("enabled"):
        return 0
    url_field = dl_cfg.get("url_field", "url")
    dest_dir = out_dir / dl_cfg.get("dir", "files")
    dest_dir.mkdir(parents=True, exist_ok=True)
    size_limit = int(dl_cfg.get("size_limit", 50 * 1024 * 1024))
    concurrency = int(dl_cfg.get("concurrency", 4))
    backend = anti.get("http_backend", "requests")
    from .core import RequestsClient, HttpClient
    http = RequestsClient(min_interval=dl_cfg.get("interval", 0.3), timeout=60,
                          rotate_ua=anti.get("rotate_ua", True)) if backend == "requests" else \
        HttpClient(min_interval=dl_cfg.get("interval", 0.3), timeout=60)
    done = 0

    def _dl(r) -> int:
        url = r.get(url_field) or ""
        if not url:
            return 0
        try:
            resp = http.get(url, max_size=size_limit + 1)  # +1 才能检出"超限被截断"
            if not resp.get("ok"):
                return 0
            body = resp.get("body", b"")
            if len(body) > size_limit:
                return 0
            name = str(r.get("id") or r.get("title") or url.split("/")[-1] or "file")
            ext = Path(url.split("?")[0]).suffix or ".bin"
            safe = "".join(c for c in name if c.isalnum() or c in "_-.")[:80] or "file"
            fp = dest_dir / f"{safe}{ext}"
            fp.write_bytes(body)
            r["downloaded_file"] = str(fp)
            return 1
        except Exception:
            return 0

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for fut in as_completed([ex.submit(_dl, r) for r in rows]):
            done += fut.result()
    (logger or Logger()).info(f"文件下载完成: {done}/{len(rows)} -> {dest_dir}")
    return done


def _merge_url(url: str, base: str) -> str:
    if not url:
        return ""
    if url.startswith("http"):
        return url
    return base.rstrip("/") + ("/" + url.lstrip("/") if not url.startswith("/") else url)


class GracefulExit:
    """捕获 Ctrl+C，优雅保存检查点后退出。"""
    def __init__(self, checkpoint: Optional[Checkpoint]):
        self.checkpoint = checkpoint
        self.stop = False
        signal.signal(signal.SIGINT, self._handler)

    def _handler(self, *a):
        self.stop = True
        print("\n[Ctrl+C] 正在保存检查点并退出...", file=__import__("sys").stderr)

    def save(self, rows):
        if self.checkpoint:
            try:
                self.checkpoint.save(rows, len(rows), len(rows))
            except Exception:
                pass


def run_config(config: Dict[str, Any], overrides: Optional[Dict[str, str]] = None,
               base_dir: Optional[Path] = None, resume: bool = False,
               limit: Optional[int] = None, log_file: Optional[Path] = None,
               dry_run: bool = False) -> Dict[str, Any]:
    config = validate_config(config)
    name = config.get("name", "task")
    vars = dict(config.get("vars", {}))
    if overrides:
        vars.update(overrides)
    anti = dict(config.get("anti_bot", {}))
    output = config.get("output", {})
    out_dir = Path(output.get("dir", "outputs"))
    out_dir.mkdir(parents=True, exist_ok=True)
    cap_dir = out_dir / ".captcha"
    cap_dir.mkdir(parents=True, exist_ok=True)
    anti["captcha_dir"] = str(cap_dir)
    session_dir = out_dir / ".session"
    session_dir.mkdir(parents=True, exist_ok=True)
    anti["session_dir"] = str(session_dir)
    anti["session_name"] = anti.get("session_name") or name  # 可用配置指定，实现多任务共享登录态

    logger = Logger(log_file=log_file or (out_dir / f".run_{name}.log"))
    middleware = MiddlewareChain(config.get("middleware"), logger=logger)
    proxy_pool = ProxyPool(anti.get("proxies"), anti.get("proxy_mode", "round_robin"))
    if anti.get("proxy"):
        proxy_pool = ProxyPool([anti["proxy"]])

    # 增量去重
    inc = config.get("incremental", {})
    seen = None
    if inc.get("enabled"):
        seen = SeenStore(out_dir / f".seen_{name}.txt")
        logger.info(f"增量模式已开启（已见 {len(seen)} 条）")

    iterate = config.get("iterate")
    iterations: List[Dict[str, Any]] = [{}]
    if iterate:
        var_name = iterate["var"]
        iterations = [{var_name: v} for v in iterate["values"]]
        logger.info(f"迭代 {var_name}: {iterate['values']}")

    all_rows: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {}
    graceful = GracefulExit(None)

    for it in iterations:
        ivars = {**vars, **it}
        it_name = it.get("stage") or (list(it.values())[0] if it else "default")
        # 迭代名用于文件名/检查点：URL 等特殊字符做安全化
        it_name = re.sub(r"[^\w\u4e00-\u9fff-]", "_", str(it_name))
        logger.info(f"===== 迭代: {it or '(默认)'} =====")
        source = _resolve_template(config.get("source", {}), ivars)
        pagination = _resolve_template(config.get("pagination", {}), ivars)
        # 相对详情/下载链接的基准：默认取 source.url 的 origin（http(s)://host[:port]）
        if not source.get("base_url") and source.get("url"):
            from urllib.parse import urlsplit
            _pu = urlsplit(source["url"])
            if _pu.scheme and _pu.netloc:
                source["base_url"] = f"{_pu.scheme}://{_pu.netloc}"
        ftype = source.get("type")
        fetcher_cls = FETCHERS.get(ftype)
        if fetcher_cls is None:
            die(f"未知 source.type: {ftype}")

        # 断点续跑：加载该迭代已有结果
        cp_path = out_dir / f".checkpoint_{name}_{it_name}.json"
        checkpoint = Checkpoint(cp_path)
        graceful.checkpoint = checkpoint
        prev_rows = checkpoint.load_rows() if resume else []
        if resume and prev_rows:
            logger.info(f"断点续跑：检查点已有 {len(prev_rows)} 条")

        # sitemap 种子
        if source.get("sitemap"):
            from .core import make_http_client
            sm = make_http_client({"min_interval": 1.0, "timeout": 20, "http_backend": "auto"})
            urls = fetch_sitemap_urls(sm, source["sitemap"])
            logger.info(f"sitemap 种子: {len(urls)} 个 URL")
            source["sitemap_urls"] = urls
            if ftype == "http_html":
                source["url"] = urls[0] if urls else source["url"]

        anti_iter = dict(anti)
        # 现场持久化：capture_all.json/last_page.html 写进任务输出目录（猎聘战例——
        # v2 独立配置此前不设 _task_dir，捕获文件随临时目录焚毁）
        anti_iter["_task_dir"] = str(out_dir)
        if proxy_pool.size:
            anti_iter["_proxy_pool"] = proxy_pool
            if isinstance(fetcher_cls, HttpFetcher.__class__):
                pass
        cache_dir = out_dir / ".cache" if output.get("cache") else None
        fetcher = fetcher_cls(source, anti_iter, ivars, base_dir)
        if hasattr(fetcher, "http") and cache_dir:
            fetcher.http.cache_dir = cache_dir

        if dry_run:
            logger.info(f"[dry-run] 取数器 {ftype} 就绪: {source.get('url', source.get('bridge'))}")
            continue

        rows_raw = fetcher.fetch_list(pagination)
        logger.info(f"原始记录: {len(rows_raw)}")

        rows = [map_record(r, config.get("record", {}).get("fields", {})) for r in rows_raw]
        if config.get("record", {}).get("keep_raw"):
            for raw, mapped in zip(rows_raw, rows):
                for k, v in raw.items():
                    if k not in mapped:
                        mapped[f"raw_{k}"] = v
        for r in rows:
            for k, v in it.items():
                r[f"iter_{k}"] = v
            if iterate and iterate.get("labels"):
                lab = iterate["labels"].get(str(it.get("stage", it.get(var_name))))
                if lab:
                    r["iter_label"] = lab
            r["url"] = _merge_url(r.get("url") or "", source.get("base_url", ""))
            # 详情/下载的 URL 字段也补全为绝对地址（相对路径场景）
            _dlf = (config.get("download", {}) or {}).get("url_field")
            _dtf = (config.get("detail", {}) or {}).get("url_field")
            for _f in {_dlf, _dtf} - {None, "url"}:
                if r.get(_f):
                    r[_f] = _merge_url(str(r[_f]), source.get("base_url", ""))
            r["source_name"] = name

        # 中间件：数据产出
        for r in rows:
            middleware.run("data", {"record": r, "title": r.get("title"), "url": r.get("url")})

        rows = run_pipeline(rows, _resolve_template(config.get("pipeline", []), ivars), log_prefix=f"[{name}] ")

        # 增量去重
        if seen is not None:
            before = len(rows)
            kept = []
            for r in rows:
                key = record_key(r, inc.get("key", "id"))
                if key and not seen.is_seen(key):
                    seen.mark(key)
                    kept.append(r)
            rows = kept
            logger.info(f"增量去重: {before} -> {len(rows)}（跳过已见）")

        if limit:
            rows = rows[:limit]
            logger.info(f"--limit {limit}: 截断到 {len(rows)} 条")

        # 详情
        detail = config.get("detail", {})
        if detail.get("enabled"):
            # 断点续跑：从检查点合并已抓详情，避免重复请求
            if resume and checkpoint:
                old_rows = checkpoint.load_rows()
                if old_rows:
                    keyf = detail.get("resume_key", detail.get("url_field", "id"))
                    old_by_key = {record_key(o, keyf): o for o in old_rows}
                    merged = 0
                    for r in rows:
                        k = record_key(r, keyf)
                        o = old_by_key.get(k)
                        if o and (o.get("detail_body") or "").strip():
                            for kk, vv in o.items():
                                if kk not in r or not r.get(kk):
                                    r[kk] = vv
                            merged += 1
                    logger.info(f"断点续跑：从检查点合并 {merged} 条详情")
            rows = fetch_details(rows, detail, anti_iter, checkpoint=checkpoint, logger=logger)

        if rows:
            all_rows.extend(rows)
        elif resume and prev_rows:
            all_rows.extend(prev_rows)
            logger.info("本次未抓到新记录，沿用检查点数据")

        base = _resolve_template(output.get("base_name", name), ivars)
        if len(iterations) > 1 and it:
            lab = iterate.get("labels", {}).get(str(it.get(var_name))) if iterate else None
            base = f"{base}_{lab or it_name}"
        if rows and not dry_run:
            paths = export_rows(rows, out_dir, base,
                                formats=output.get("formats") or ["json", "csv", "xlsx"])
            summary[base] = len(rows)
            logger.info(f"{base} 导出: {len(rows)} 条 -> " + ", ".join(f"{k}={v.name}" for k, v in paths.items()))

        if graceful.stop:
            break

    # 文件下载
    if all_rows:
        download_files(all_rows, config.get("download"), anti, out_dir, logger=logger)

    # 合并导出
    if all_rows and not dry_run:
        base = _resolve_template(output.get("base_name", name), vars)
        paths = export_rows(all_rows, out_dir, base + "_合并" if len(iterations) > 1 else base,
                            formats=output.get("formats") or ["json", "csv", "xlsx"])
        summary["_merged"] = len(all_rows)
        logger.info("合并导出: " + ", ".join(f"{k}={v.name}" for k, v in paths.items()))
        logger.info(f"总计 {len(all_rows)} 条")
    if dry_run:
        logger.info("[dry-run] 校验通过，未发起真实请求")
    logger.summary()
    return {"name": name, "total": len(all_rows), "summary": summary}
