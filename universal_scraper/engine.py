#!/usr/bin/env python3
"""配置驱动的爬虫执行引擎（v2）。

取数(HTTP/浏览器/桥) → 字段映射 → 流水线(过滤/去重/清洗)
→ 详情展开(并发/断点) → 文件下载 → 导出
支持：中间件钩子、断点续跑(--resume)、增量去重、sitemap、代理轮换、优雅中断。
"""
from __future__ import annotations

import os
import re
import signal
import sys
import time
import json  # recon 路径写 recon_records.json 用（曾漏 import，跑到即 NameError）
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core import export_rows, log, die, safe_fname, MaxRequestsExceeded
from .protocols import BlockDetectedError
from .selectors import jpath, apply_extractor, regex_extract_all
from .fetchers import HttpFetcher, BrowserScriptFetcher, BrowserFetcher, _resolve_template
from .log import Logger
from .config import validate as validate_config, ConfigError
from .middleware import MiddlewareChain
from .proxy import ProxyPool
from .storage import Checkpoint, SeenStore, record_key

_REQ_THREAD_WARNED = False


class _MwClient:
    """把 MiddlewareChain 的 request/response/error 时机接到 HTTP 客户端上（审查八轮）。

    为什么用适配器：v2 的取数分散在 fetchers（列表）/ engine（详情、下载）两处，
    逐个改签名既容易漏点位、又要动多条链。包一层客户端后**任何**经它发出的请求
    都会经过钩子；适配器双向透明（读写属性都转发给被包客户端），fetcher 既有的
    `self.client.max_retries = 1` / `getattr(self.client, "_at")` 等用法不受影响。

    契约（对齐 middleware.MiddlewareChain.run）：钩子原地修改 ctx dict——
      request : {"url","method","headers","body"}   ← 可改（改后生效）
      response: {"url","status","ok","text","body","json","headers"} ← 可改（写回结果）
      error   : {"url","method","error"}（传输异常）或 {"url","status","text"}（HTTP 失败）
    钩子内部异常由 MiddlewareChain 自己捕获（不影响主流程）。
    """

    def __init__(self, client, chain, logger=None):
        object.__setattr__(self, "_c", client)
        object.__setattr__(self, "_mw", chain)
        object.__setattr__(self, "_log", logger)

    # 双向透明：读转发给被包客户端；写也落到它身上（除了本适配器自己的三个槽位）
    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_c"), name)

    def __setattr__(self, name, value):
        if name in ("_c", "_mw", "_log"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_c"), name, value)

    def request(self, url: str, method: str = "GET", **kw):
        mw = object.__getattribute__(self, "_mw")
        ctx = {"url": url, "method": method,
               "headers": dict(kw.get("headers") or {}),
               "body": kw.get("data") if kw.get("data") is not None else kw.get("json_data")}
        if mw is not None:
            mw.run("request", ctx)
        url = ctx.get("url") or url
        method = ctx.get("method") or method
        if ctx.get("headers"):
            kw["headers"] = ctx["headers"]
        if ctx.get("body") is not None:
            if "json_data" in kw and kw.get("json_data") is not None:
                kw["json_data"] = ctx["body"]
            else:
                kw["data"] = ctx["body"]
        try:
            r = object.__getattribute__(self, "_c").request(url, method, **kw)
        except Exception as e:
            if mw is not None:
                mw.run("error", {"url": url, "method": method, "error": e})
            raise
        if mw is not None:
            if isinstance(r, dict):
                rctx = {"url": r.get("url") or url, "status": r.get("status"), "ok": r.get("ok"),
                        "text": r.get("text") or "", "body": r.get("body"),
                        "json": r.get("json"), "headers": r.get("headers") or {}}
                mw.run("response", rctx)
                for k in ("status", "ok", "text", "body", "json", "headers"):
                    if k in rctx:
                        r[k] = rctx[k]
                if not r.get("ok"):
                    mw.run("error", {"url": r.get("url") or url, "status": r.get("status"),
                                     "text": (r.get("text") or "")[:200]})
            else:
                mw.run("response", {"url": url, "status": getattr(r, "status", 0)})
        return r

    def get(self, url: str, **kw):
        return self.request(url, "GET", **kw)

    def post(self, url: str, **kw):
        return self.request(url, "POST", **kw)



def _warn_requests_threading(http, workers: int) -> None:
    """requests 后端 + 多线程共享同一 Session 的告警（审查八轮，MEDIUM）。

    requests.Session 非线程安全（连接池/内部状态），而详情/下载走 ThreadPoolExecutor
    共享同一个 client；curl_cffi 后端每次请求走模块级 request（独立会话）、urllib 后端
    每次新建 opener，故只有 requests 后端有此问题。默认 auto 走 curl_cffi 时无此问题，
    但用户显式 http_backend=requests + 并发>1 时症状是"偶发响应串数据"，极难排查——
    这里一次性大声告警（不改行为，避免擅自串行化拖慢用户任务）。
    """
    global _REQ_THREAD_WARNED
    try:
        if workers and int(workers) > 1 and getattr(http, "_backend_name", "") == "requests" \
                and not _REQ_THREAD_WARNED:
            _REQ_THREAD_WARNED = True
            log("⚠️ http_backend=requests + 并发>1：多线程共享同一 requests.Session 并非"
                "线程安全用法（可能偶发响应串数据）。建议用 auto（默认 curl_cffi，每请求"
                "独立会话）或把并发设为 1", "WARN")
    except Exception:
        pass


FETCHERS = {
    "http_json": HttpFetcher,
    "http_html": HttpFetcher,
    "browser_script": BrowserScriptFetcher,
    "browser": BrowserFetcher,
}


def _row_keys(rows: List[Dict[str, Any]], rk) -> List[str]:
    """按增量键取每行的去重 key（与行一一对应；无键行返回 ""）。

    R22：去重循环与 limit 截断后的"待标记"必须用**同一份口径**——
    此前截断按数量裁 `_pending_keys[:len(rows)]`，行里含无键行时会把从未导出的
    行的 key 一并标记（实测：limit=2 冒烟后全量跑，从未出现在任何导出文件里的
    记录被永久跳过 = 静默丢数据）。"""
    parts = rk if isinstance(rk, list) else [rk]
    out: List[str] = []
    for r in rows:
        if any(r.get(p) in (None, "") for p in parts):
            out.append("")
            continue
        out.append(record_key(r, rk) or "")
    return out


def map_record(raw: Dict[str, Any], fields: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not isinstance(fields, dict):
        # 盒马战例：record.fields 写成 list，validate 之外这里必须兜住——丢映射不丢数据
        if fields:
            log(f"⚠️ record.fields 应为 dict（当前 {type(fields).__name__}），"
                "已跳过映射、保留原始字段——请修正配置")
        return dict(raw)
    for name, spec in fields.items():
        _strip = False
        if isinstance(spec, str):
            out[name] = jpath(raw, spec, None)
        elif isinstance(spec, dict):
            _strip = bool(spec.get("strip_tags"))
            if "from" in spec:
                out[name] = jpath(raw, spec["from"], spec.get("default"))
            elif "path" in spec:
                out[name] = jpath(raw, spec["path"], spec.get("default"))
            elif "constant" in spec:
                out[name] = spec["constant"]
            elif "template" in spec:
                try:
                    out[name] = spec["template"].format(**{k: (v if v is not None else "") for k, v in raw.items()})
                except Exception as _tpl_e:
                    # OCR R131（M）：模板变量缺失/格式错曾静默空串——排错无门
                    import sys as _sys
                    print(f"⚠️ 字段模板渲染失败 {name}: {type(_tpl_e).__name__}: {str(_tpl_e)[:80]}",
                          file=_sys.stderr)
                    out[name] = ""
            elif "concat" in spec:
                out[name] = "".join(str(jpath(raw, p, "")) for p in spec["concat"])
            else:
                out[name] = jpath(raw, spec.get("path", ""), None)
        else:
            out[name] = spec
        # T1 实测（2026-09-27）：strip_tags 字段级选项——搜索类 API 高亮标签
        # （gov.cn 的 <em>、通用搜索的 <b>/<mark>）是常见污染，取值后统一后处理
        if _strip and isinstance(out.get(name), str):
            import re as _re
            out[name] = _re.sub(r"<[^>]+>", "", out[name])
    return out


def run_pipeline(rows: List[Dict[str, Any]], pipeline: List[Dict[str, Any]], log_prefix: str = "") -> List[Dict[str, Any]]:
    _re = re  # OCR R131（L）：局部 re 导入曾遮蔽模块级——统一用模块级引用
    for step in pipeline or []:
        st = step.get("type")
        if st == "filter":
            field, op, value = step.get("field"), step.get("op", "contains"), step.get("value")
            if not field:
                log(f"{log_prefix}filter 缺 field——步骤跳过", "WARN")
                continue
            before = len(rows)
            # AI 配置防御：value 缺失/为 None 时按空串处理，避免 `None in str` 直接炸掉整单
            value = "" if value is None else str(value)
            if op == "contains":
                rows = [r for r in rows if value in str(r.get(field) or "")]
            elif op == "eq":
                rows = [r for r in rows if str(r.get(field)) == str(value)]
            elif op == "regex":
                # R94 修复（P1）：非法正则曾 re.error 炸掉整单（抓完不导出）
                try:
                    pat = _re.compile(str(step.get("pattern") or step.get("value") or ""))
                except (_re.error, TypeError, ValueError) as e:
                    log(f"{log_prefix}filter regex 编译失败（步骤跳过）: {e}", "WARN")
                    continue
                rows = [r for r in rows if pat.search(str(r.get(field) or ""))]
            elif op == "non_empty":
                rows = [r for r in rows if str(r.get(field) or "").strip()]
            elif op == "not_contains":
                rows = [r for r in rows if value not in str(r.get(field) or "")]
            elif op == "between":
                # batch2400 审查修复：一条脏值（"N/A"等）曾把全部行清空——逐行容错。
                # R95 修复（P2）：配置里的 min/max 本身非数值时也曾 ValueError 炸整单
                try:
                    lo = float(step.get("min", float("-inf")))
                    hi = float(step.get("max", float("inf")))
                except (TypeError, ValueError) as e:
                    log(f"{log_prefix}between min/max 非数值（步骤跳过）: {e}", "WARN")
                    continue
                kept = []
                for r in rows:
                    try:
                        if lo <= float(str(r.get(field)).replace(",", "")) < hi:
                            kept.append(r)
                    except (ValueError, TypeError):
                        continue
                rows = kept
            log(f"{log_prefix}filter[{field} {op} {value}]: {before} -> {len(rows)}")
        elif st == "dedup":
            key = step.get("key", "id")
            before = len(rows)
            seen = set(); uniq = []
            for r in rows:
                k = tuple(str(r.get(kk) or "") for kk in (key if isinstance(key, list) else [key]))
                # OCR R131（M）：字面量 "None" 曾被当缺失跳过去重（对齐 pipelines
                # 的同款修复）——None→"" 已由 or "" 处理
                if any(kk == "" for kk in k):
                    uniq.append(r)
                    continue
                if k not in seen:
                    seen.add(k); uniq.append(r)
            rows = uniq
            log(f"{log_prefix}dedup[{key}]: {before} -> {len(rows)}")
        elif st == "rename":
            for r in rows:
                for old, new in (step.get("mapping") or {}).items():
                    if old in r:
                        r[new] = r[old]; del r[old]
        elif st == "cast":
            field, ctype = step.get("field"), step.get("to", "str")
            if not field:
                log(f"{log_prefix}cast 缺 field——步骤跳过", "WARN")
                continue
            for r in rows:
                v = r.get(field)
                try:
                    if ctype == "int" and v not in (None, ""):
                        r[field] = int(float(str(v).replace(",", "")))
                    elif ctype == "float" and v not in (None, ""):
                        r[field] = float(str(v).replace(",", ""))
                    elif ctype == "str":
                        r[field] = str(v) if v is not None else ""
                except (ValueError, TypeError, OverflowError):
                    # OCR R131（属性测试抓获）：超大整数经 float 转 int 抛 OverflowError
                    pass
        elif st == "add":
            _af = step.get("field")
            if not _af:
                log(f"{log_prefix}add 缺 field——步骤跳过", "WARN")
                continue
            for r in rows:
                r[_af] = step.get("value")
        elif st == "transform":
            # 字段变换：op=unix_to_datetime（秒/毫秒时间戳自适应）| upper | lower
            field, op = step.get("field"), step.get("op", "unix_to_datetime")
            if not field:
                log(f"{log_prefix}transform 缺 field——步骤跳过", "WARN")
                continue
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
        elif st == "regex_extract":
            # 正则提取变换（闲鱼战例）：从既有字段按 capture group 派生新字段
            field = step.get("field")
            if not field:
                log(f"{log_prefix}regex_extract 缺 field——步骤跳过", "WARN")
                continue
            try:
                pat = _re.compile(str(step.get("pattern", "")))
            except (_re.error, TypeError, ValueError) as e:
                log(f"{log_prefix}regex_extract 编译失败（步骤跳过）: {e}", "WARN")
                continue
            try:
                grp = int(step.get("group", 1))
            except (TypeError, ValueError):
                log(f"{log_prefix}regex_extract group 非整数（按 1 处理）", "WARN")
                grp = 1
            to = step.get("to") or (field + "_提取")
            n_hit = 0
            for r in rows:
                m = pat.search(str(r.get(field) or ""))
                if m:
                    try:
                        r[to] = m.group(grp)
                        n_hit += 1
                    except (IndexError, _re.error):
                        pass
            log(f"{log_prefix}regex_extract[{field}->{to}]: 命中 {n_hit}/{len(rows)}")
        elif st == "template":
            # 用已有字段拼新字段：tmpl 里 {字段名} 占位，如 "https://www.bilibili.com/video/{bvid}"
            name, tmpl = step.get("field"), step.get("tmpl", "")
            if not name or not isinstance(tmpl, str):
                log(f"{log_prefix}template 缺 field 或 tmpl 非字符串——步骤跳过", "WARN")
                continue
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
    _uf = detail.get("url_field", "url")
    # R88：优先用 run_config 补全的绝对地址（<url_field>_final）——原始字段
    # 保持原样（裸 ID 字段曾连 POST body 插值一起被改写成 URL）
    url = row.get(_uf + "_final") or row.get(_uf) or ""
    if not url:
        return row
    for tr in detail.get("url_transform", []):
        # R88：兼容文档写法 {"type": "prefix", "value": "..."} 与简写 {"prefix": "..."}
        if "type" in tr and "value" in tr:
            tr = {tr["type"]: tr["value"]}
        if "replace" in tr:
            old, new = tr["replace"][0], tr["replace"][1]
            url = url.replace(old, new)
        elif "prefix" in tr:
            # 🛡️ 绝对链接不拼前缀（v3 同款）：防 https://host + https://host 双域名
            if not url.startswith(("http://", "https://")):
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
    # 裁判文书网战训（审查 P0 残留）：详情页的 200 封禁页曾按正常页抽取入库
    # （detail_status="200"、exit 0）——命中即跳过抽取，绝不把封禁页写进数据
    try:
        from .antibot import detect_block as _db
        _bd = _db(resp.get("status", 0), html, resp.get("headers"), url)
        if _bd["kind"] not in ("none", "login", "http_error"):
            row["detail_status"] = f"blocked:{_bd['kind']}"
            row["detail_block_detail"] = _bd["detail"][:120]
            return row
    except Exception as _dbe:
        # 审查修复 P0：裸 pass 曾让守卫失效时封禁页照常入库（detail_status="200"）
        # ——判型失败按"疑似封禁"处理（宁可不写），并大声记录
        row["detail_status"] = "blockcheck_error"
        row["detail_block_detail"] = f"{type(_dbe).__name__}: {_dbe}"[:120]
        return row
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
    # R118 修复（P1）：resume 哨兵字段——fetch_details 的 todo 过滤和检查点
    # 合并门都以 detail_body 非空判定"已完成"，但此前全库无任何写入点，
    # --resume 的详情跳过是静默 no-op。截断 500 字符做正文预览（防检查点膨胀）。
    row["detail_body"] = html[:500]
    return row


def fetch_details(rows, detail, anti, checkpoint: Optional[Checkpoint] = None, logger: Optional[Logger] = None,
                  source: Optional[Dict[str, Any]] = None, middleware=None) -> List:
    if not detail.get("enabled"):
        return rows
    # OCR R6：extract spec 缺 "name" 曾通过 validate、逐行抽取时才 KeyError——
    # browser 后端整个崩、HTTP 路径每行 ERR:KeyError（同 R94"抓完整单才炸"）。
    # 开抓前先报清楚。
    for i, spec in enumerate(detail.get("extract", []) or []):
        if isinstance(spec, dict) and not spec.get("name"):
            raise ConfigError(f"detail.extract[{i}]", "extract 步骤缺少 name（抽取值要写入的列名）",
                              '例如: {"type": "css", "name": "价格", "selector": ".price"}')
    # 深测二轮（2026-09-27）易用性防御：enabled 且 extract 为空 = 每行详情页照抓
    # 但不抽取任何字段（行上只有 detail_status/detail_body）——十有八九是配置写错
    # （fields/selector 等自造键不是 v1 detail 的 schema，正确键是 extract 数组）。
    # 白抓会烧请求预算，必须开抓前 WARN。
    if not detail.get("extract"):
        print("⚠️ detail.enabled=true 但 extract 为空——详情页将被抓取但不抽取任何字段；"
              "若非有意（仅取 detail_body 哨兵），请配置 detail.extract 数组"
              "（如 [{\"name\":\"正文\",\"type\":\"css_text\",\"selector\":\"div.content\"}]）",
              file=sys.stderr)
    concurrency = int(detail.get("concurrency", 1))
    interval = float(detail.get("interval", 0.5))
    timeout = float(anti.get("timeout", 15))
    # 收官十三轮（审查 L）：checkpoint_every=0（"不落检查点"的自然写法，config
    # 不拦）曾让 `i % 0` 抛 ZeroDivisionError，整个任务在导出前崩掉
    _ck_every = int(detail.get("checkpoint_every") or 20)
    todo = [r for r in rows if not (r.get("detail_body") or "").strip()]
    # R90 修复（P2）：opt-in respect_robots 时详情请求也曾绕过 robots——
    # 与 fetch_list 同闸
    _robots = None
    if anti.get("respect_robots"):
        try:
            from .robots import RobotsTxt
            _robots = RobotsTxt(user_agent="universal-scraper/1.0")
        except Exception:
            _robots = None
    if _robots is not None and todo:
        _uf = detail.get("url_field", "url")

        def _robot_ok(r):
            u = r.get(_uf + "_final") or r.get(_uf) or ""
            try:
                return bool(u) and _robots.allowed(u)
            except Exception:
                return True
        _skip = [r for r in todo if not _robot_ok(r)]
        if _skip:
            for r in _skip:
                r["detail_status"] = "robots_disallowed"
            # OCR R131（L）：todo 全集已在 _skip 判定中算过一次 _robot_ok——
            # 二次全量重算改为集合差（同结果，一半开销）
            _skip_ids = {id(r) for r in _skip}
            todo = [r for r in todo if id(r) not in _skip_ids]
            (logger or Logger()).warn(f"⛔ robots 禁止 {len(_skip)} 条详情 URL，已跳过")
    done = len(rows) - len(todo)
    (logger or Logger()).info(f"详情：共 {len(rows)}，已有 {done}，待抓 {len(todo)}（并发 {concurrency}）")
    if not todo:
        return rows

    # 详情 browser 后端（闲鱼战例）：登录态+JS 站的详情页 HTTP 全是空壳——
    # 单次浏览器实例顺序导航全部 URL，一次连接抓完再统一抽取。
    # R101：backend 为 auto/playwright 且 playwright-python 可用时走进程内
    # 渲染（无 JSONL 桥协议层）；否则回落 node 桥（CDP 附加备用通道）
    if str(detail.get("backend", "")).lower() == "browser":
        url_field = detail.get("url_field", "url")
        urls = [r.get(url_field + "_final") or r.get(url_field) or "" for r in todo]
        _backend_mode = str(detail.get("browser_backend", "auto")).lower()
        pages = None
        _pw = None  # R102 修复（P1）：未初始化曾致 playwright 缺失/node 模式下 UnboundLocalError
        if _backend_mode in ("auto", "playwright"):
            try:
                from .browser_pw import PWBrowserFetcher, playwright_available
                if playwright_available():
                    # 审查八轮（H）：source 曾只传 cdp——SKILL.md 承诺的
                    # wait_until/dom_stable（browser_pw._render 从 source 读）
                    # 与 headless 在 playwright 后端静默不生效：SPA 详情页按
                    # domcontentloaded+固定 1200ms 渲染，接口拖尾没等到 → 空字段
                    # 仍 detail_status=200 入库。node 桥回落路径反而透传了
                    # detail.headless（两条后端行为不一致）。渲染键从 detail→
                    # source 逐级取（显式 null 视同未配）
                    _src = {"cdp": detail.get("cdp") or (source or {}).get("cdp")}
                    for _k in ("wait_until", "dom_stable", "headless"):
                        _v = detail.get(_k)
                        if _v is None:
                            _v = (source or {}).get(_k)
                        if _v is not None:
                            _src[_k] = _v
                    _pw = PWBrowserFetcher(_src, anti, {}, Path("."))
                    # R101 修复（P2）：probe-launch 前置——chromium 二进制缺失时
                    # launch 失败必须在此暴露并回落 node 桥，而不是逐页静默
                    # browser_miss（曾违背 auto"能启动就用"契约）
                    _pw.ensure()
            except Exception as _pw_err:
                (logger or Logger()).warn(f"playwright 初始化失败，回落 node 桥: {_pw_err}")
                _pw = None
        if _pw is not None:
            try:
                # R116：并发渲染——分片数跟 detail.concurrency（上限 3，浏览器实例重）
                _workers = max(1, min(3, concurrency))
                pages = _pw.fetch_pages([u for u in urls if u],
                                        workers=_workers)  # ensure 已通过，此处幂等
            finally:
                # R113 修复（P3）：fetch_pages 中途异常曾跳过 close——浏览器进程泄漏
                _pw.close()
        if pages is None:
            from .fetchers import BrowserFetcher
            bsrc = {"type": "browser",
                    "url": urls[0] if urls else "about:blank",
                    "cdp": detail.get("cdp") or (source or {}).get("cdp"),
                    "headless": detail.get("headless", False)}
            bf = BrowserFetcher(bsrc, anti, {}, Path("."))
            pages = bf.fetch_pages([u for u in urls if u])
        got = 0
        for r in todo:
            url = r.get(url_field + "_final") or r.get(url_field) or ""
            html = pages.get(url, "")
            r[url_field + "_final"] = url
            # R88：与 HTTP 详情同款封禁门——渲染出的封禁页绝不抽取入库
            if html:
                try:
                    from .antibot import detect_block as _db
                    _bd = _db(200, html, {}, url)
                    if _bd["kind"] not in ("none", "login", "http_error"):
                        r["detail_status"] = f"blocked:{_bd['kind']}"
                        r["detail_block_detail"] = _bd["detail"][:120]
                        continue
                except Exception as _dbe:
                    # R128 修复（OCR）：裸 pass 曾让封禁页照常入库（同 HTTP 路径 R88 口径）
                    r["detail_status"] = "blockcheck_error"
                    r["detail_block_detail"] = f"{type(_dbe).__name__}: {_dbe}"[:120]
                    continue
            r["detail_status"] = "200" if html else "browser_miss"
            ctx_obj = None
            if detail.get("type") == "http_json":
                import json as _json
                try:
                    ctx_obj = _json.loads(html)
                except Exception:
                    ctx_obj = None
            for spec in detail.get("extract", []):
                r[spec["name"]] = apply_extractor(spec, html, html, ctx_obj)
            if html:
                got += 1
                r["detail_body"] = html[:500]  # R118 哨兵（同 HTTP 路径）
        (logger or Logger()).info(f"详情(browser)：批抓 {got}/{len(todo)} 页成功")
        # OCR R6：browser 后端此前从不落检查点（HTTP 路径都落）——中断后
        # --resume 全部重抓。尾部与 HTTP 路径同口径落一次盘。
        if checkpoint and todo:
            checkpoint.save(rows, done + len(todo), len(rows))
        return rows

    # batch2400 审查修复：详情/下载与列表共用同一 HTTP 栈（make_http_client 透传
    # anti 的 curl_cffi 指纹/verify/代理），否则"列表成功详情 403"且难排查
    from .core import make_http_client
    http = make_http_client({**anti, "min_interval": interval, "timeout": timeout})
    _warn_requests_threading(http, concurrency)   # 审查八轮：requests 会话跨线程共享告警
    if middleware is not None:
        http = _MwClient(http, middleware, logger)   # request/response/error 时机接线

    def _work(r):
        fetch_detail_row(http, r, detail)
        return r

    if concurrency <= 1:
        for i, r in enumerate(todo, 1):
            try:
                _work(r)
            except Exception as e:
                r["detail_status"] = f"ERR:{type(e).__name__}"
            if checkpoint and _ck_every > 0 and i % _ck_every == 0:
                checkpoint.save(rows, done + i, len(rows))
            time.sleep(interval)
    else:
        # 审查修复 P1：硬闸(BaseException)穿透时，with 退出会等全部排队 future
        # 各睡完 min_interval（千行任务=十几分钟假死）——中止路径须取消排队
        ex = ThreadPoolExecutor(max_workers=concurrency)
        try:
            futs = {ex.submit(_work, r): r for r in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                r = futs[fut]
                try:
                    fut.result()
                except Exception as e:
                    r["detail_status"] = f"ERR:{type(e).__name__}"
                if checkpoint and _ck_every > 0 and i % _ck_every == 0:
                    checkpoint.save(rows, done + i, len(rows))
                time.sleep(interval / concurrency)
        except BaseException:
            ex.shutdown(wait=False, cancel_futures=True)
            raise
        ex.shutdown(wait=True)
    # R118 修复（P3）：尾部不足 checkpoint_every 的行落盘（resume 少重抓）
    if checkpoint and todo:
        checkpoint.save(rows, done + len(todo), len(rows))
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
        try:
            res = http.get(url)
        except Exception as e:
            log(f"sitemap robots.txt 拉取失败 {url}: {type(e).__name__}: {e}", "WARN")
            return []
        for m in regex_extract_all(res.get("text", ""), r"Sitemap:\s*(\S+)", 1):
            urls.extend(fetch_sitemap_urls(http, m, max_urls, visited, _depth + 1))
        return urls[:max_urls]
    try:
        res = http.get(url)
    except Exception as e:
        log(f"sitemap 拉取失败 {url}: {type(e).__name__}: {e}", "WARN")
        return []
    html = res.get("text", "")
    # 普通 sitemap 或 sitemap index（cap=16MB：内部固定模式无 ReDoS 风险，
    # 不吃用户模式的 64KB 截断——大 sitemap 曾被截丢后段 <loc>）
    for m in regex_extract_all(html, r"<loc>\s*([^<]+?)\s*</loc>", 1, cap=16 * 1024 * 1024):
        u = m.strip()
        if u.endswith(".xml") or "sitemap" in u.lower():
            urls.extend(fetch_sitemap_urls(http, u, max_urls, visited, _depth + 1))
        else:
            urls.append(u)
        if len(urls) >= max_urls:
            break
    return urls[:max_urls]


def download_files(rows, dl_cfg, anti, out_dir: Path, logger: Optional[Logger] = None,
                   middleware=None) -> int:
    """按配置下载文件（附件/PDF 等）。"""
    if not dl_cfg or not dl_cfg.get("enabled"):
        return 0
    url_field = dl_cfg.get("url_field", "url")
    # R90 修复（P2）：opt-in respect_robots 时下载请求也曾绕过 robots
    if anti.get("respect_robots"):
        try:
            from .robots import RobotsTxt as _RT
            _rt = _RT(user_agent="universal-scraper/1.0")
            _kept = []
            for r in rows:
                _u = r.get(url_field + "_final") or r.get(url_field) or ""
                try:
                    if _u and not _rt.allowed(_u):
                        continue
                except Exception:
                    pass
                _kept.append(r)
            if len(_kept) != len(rows):
                (logger or Logger()).warn(f"⛔ robots 禁止 {len(rows) - len(_kept)} 条下载 URL，已跳过")
            rows = _kept
        except Exception:
            pass
    dest_dir = out_dir / dl_cfg.get("dir", "files")
    dest_dir.mkdir(parents=True, exist_ok=True)
    size_limit = int(dl_cfg.get("size_limit", 50 * 1024 * 1024))
    concurrency = int(dl_cfg.get("concurrency", 4))
    # batch2400 审查修复：与列表/detail 共用同一 HTTP 栈（make_http_client），
    # 避免列表走 TLS 伪装、下载掉到无伪装栈的指纹分裂
    from .core import make_http_client
    http = make_http_client({**anti, "min_interval": dl_cfg.get("interval", 0.3), "timeout": 60})
    if middleware is not None:
        http = _MwClient(http, middleware, logger)   # request/response/error 时机接线
    done = 0
    import threading as _th
    _name_lock = _th.Lock()

    def _dl(r) -> int:
        # R88：优先读 run_config 补全的绝对地址（<url_field>_final）
        url = r.get(url_field + "_final") or r.get(url_field) or ""
        if not url:
            return 0
        try:
            resp = http.get(url, max_size=size_limit + 1)  # +1 才能检出"超限被截断"
            if not resp.get("ok"):
                # R118 修复（P3）：HTTP 失败也写 download_error（与异常路径同口径）
                r["download_error"] = f"HTTP {resp.get('status', 0)}"
                return 0
            body = resp.get("body", b"")
            if len(body) > size_limit:
                r["download_error"] = f"超限（>{size_limit // 1024 // 1024}MB）"
                return 0
            name = str(r.get("id") or r.get("title") or url.split("/")[-1] or "file")
            ext = Path(url.split("?")[0]).suffix or ".bin"
            safe = "".join(c for c in name if c.isalnum() or c in "_-.")[:80] or "file"
            # 审查修复 P2：同名曾静默互覆（两个 report.pdf 只剩一个）。
            # R78 修正：write_bytes 也须在锁内——曾只锁选择不锁写入，两 worker
            # 可同时选中同一路径再互覆
            with _name_lock:
                fp = dest_dir / f"{safe}{ext}"
                n = 2
                while fp.exists():
                    fp = dest_dir / f"{safe}_{n}{ext}"
                    n += 1
                fp.write_bytes(body)
            r["downloaded_file"] = str(fp)
            return 1
        except Exception as e:
            # 审查修复 P2：失败曾只 return 0——0 下载与"没文件"不可区分
            r["download_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            return 0

    ex = ThreadPoolExecutor(max_workers=concurrency)
    try:
        for fut in as_completed([ex.submit(_dl, r) for r in rows]):
            done += fut.result()
    except BaseException:
        # 硬闸/中断穿透：取消排队下载（同 fetch_details 的假死修复）
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=True)
    (logger or Logger()).info(f"文件下载完成: {done}/{len(rows)} -> {dest_dir}")
    return done


def _merge_url(url: str, base: str) -> str:
    """相对链接 → 绝对地址。实战（books.toscrape detail 全 404）：曾用裸字符串
    拼接——"../../../x" 相对页路径与 "//cdn/x" 协议相对都会拼坏。改标准
    urljoin（浏览器语义）：/ 开头以 host 为根、"../" 正确回溯、协议相对补全。"""
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    if not base:
        return url
    from urllib.parse import urljoin
    return urljoin(base, url)


class GracefulExit:
    """捕获 Ctrl+C，优雅保存检查点后退出。
    R118 修复（P2）：连按两次 Ctrl+C 恢复默认硬中断——此前 handler 只置旗标
    且被永久挂上，长详情阶段按多少次都要等跑完，用户只能强杀终端。"""
    def __init__(self, checkpoint: Optional[Checkpoint]):
        self.checkpoint = checkpoint
        self.stop = False
        self._sigcount = 0
        signal.signal(signal.SIGINT, self._handler)

    def _handler(self, *a):
        self._sigcount += 1
        if self._sigcount >= 2:
            # 第二次 Ctrl+C：恢复默认硬中断（立即退出，不再等阶段收尾）
            signal.signal(signal.SIGINT, signal.default_int_handler)
            print("\n[Ctrl+C ×2] 立即硬退出（检查点已按节奏落盘）",
                  file=__import__("sys").stderr)
            raise KeyboardInterrupt
        self.stop = True
        print("\n[Ctrl+C] 正在保存检查点并退出...（再按一次立即硬退出）",
              file=__import__("sys").stderr)



def run_config(config: Dict[str, Any], overrides: Optional[Dict[str, str]] = None,
               base_dir: Optional[Path] = None, resume: bool = False,
               limit: Optional[int] = None, log_file: Optional[Path] = None,
               dry_run: bool = False, max_requests: Optional[int] = None) -> Dict[str, Any]:
    config = validate_config(config)
    name = config.get("name", "task")
    vars = dict(config.get("vars", {}))
    if overrides:
        vars.update(overrides)
    anti = dict(config.get("anti_bot", {}))
    # 任务级请求预算（NBS 考核战训："总请求数 ≤N"硬约束必须可审计、可硬闸——
    # 此前请求计数只读不写，汇总恒打印"请求 0"）
    from .core import set_request_budget
    set_request_budget(int(max_requests or anti.get("max_requests") or 0))
    output = config.get("output", {})
    # 版权中心战例：~/Desktop 写法曾把波浪号当字面路径，在 CWD 下建出名为 '~' 的目录
    out_dir = Path(os.path.expandvars(os.path.expanduser(str(output.get("dir", "outputs")))))
    out_dir.mkdir(parents=True, exist_ok=True)
    # 审查修复（P2，R5）：v1 曾无运行锁——cron 重叠 + 手动重跑会互踩同一
    # out_dir（导出文件写花、checkpoint/seen 损坏）。复用 v3 的 fail-fast 锁
    from .engine_v3 import _acquire_run_lock
    _run_lock = _acquire_run_lock(out_dir)
    # 审查二轮（H）：unlink 曾只在函数尾部正常路径——中途异常锁残留。整段
    # try/finally 缩进风险大，用 atexit 兜底（正常路径 954 仍先行删除；残留场景
    # 下次运行另有 PID 判死接管双保险）
    import atexit as _atexit

    def _cleanup_lock():
        """收官十三轮（审查 M，实测）：atexit 回调绑定整个进程寿命——长驻进程
        （webui/mcp）退出时会删掉**此刻另一位持有者（另一个进程）的活锁**，
        随后第三个进程可同时进入同一 out_dir（导出/checkpoint/seen 互踩）。
        删前确认锁文件里的 PID 仍是自己"""
        try:
            if _run_lock.exists() and _run_lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
                _run_lock.unlink(missing_ok=True)
        except OSError:
            pass

    _atexit.register(_cleanup_lock)
    cap_dir = out_dir / ".captcha"
    cap_dir.mkdir(parents=True, exist_ok=True)
    anti["captcha_dir"] = str(cap_dir)
    session_dir = out_dir / ".session"
    session_dir.mkdir(parents=True, exist_ok=True)
    anti["session_dir"] = str(session_dir)
    anti["session_name"] = safe_fname(anti.get("session_name") or name)  # 可用配置指定，实现多任务共享登录态（R91：净化防路径逃逸）

    logger = Logger(log_file=log_file or (out_dir / f".run_{safe_fname(name)}.log"))
    middleware = MiddlewareChain(config.get("middleware"), logger=logger)
    # R116：代理 API adapter（住宅/商业提取 API → 并入池；静态代理保留在前）
    try:
        from .proxy_api import merge_api_proxies
        merge_api_proxies(anti, logger)
    except Exception as _e:
        # 审查二轮（M）：裸 pass 曾吞掉代理 API 配置错误（Key 写错/URL 错）——
        # 任务静默按"无代理"跑完才被发现。大声记录后继续（代理缺失不阻塞任务）
        logger.warn(f"代理 API 合并失败（按无代理 API 运行）: {type(_e).__name__}: {str(_e)[:120]}")
    proxy_pool = ProxyPool(anti.get("proxies"), anti.get("proxy_mode", "round_robin"))
    if anti.get("proxy"):
        proxy_pool = ProxyPool([anti["proxy"]])

    # 增量去重
    inc = config.get("incremental", {})
    seen = None
    if inc.get("enabled"):
        seen = SeenStore(out_dir / f".seen_{safe_fname(name)}.txt")
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
        # 相对详情/下载链接的基准：实战（books.toscrape）曾只取 origin（host 根）
        # ——相对页路径链接（../../../x）拼出 404。改用完整列表页 URL，
        # urljoin 按浏览器语义解析（对 / 开头链接与 host 根基准结果一致）
        if not source.get("base_url") and source.get("url"):
            from urllib.parse import urlsplit
            _pu = urlsplit(source["url"])
            if _pu.scheme and _pu.netloc:
                source["base_url"] = source["url"]
        ftype = source.get("type")
        fetcher_cls = FETCHERS.get(ftype)
        if fetcher_cls is None:
            die(f"未知 source.type: {ftype}")

        # 断点续跑：加载该迭代已有结果
        cp_path = out_dir / f".checkpoint_{safe_fname(name)}_{safe_fname(it_name)}.json"
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
            # OCR R131（M）：恒真的 isinstance + 空 pass 死代码（HttpFetcher.__class__
            # 是 type，任何类都 isinstance 成立）——删除
        cache_dir = out_dir / ".cache" if output.get("cache") else None
        fetcher = fetcher_cls(source, anti_iter, ivars, base_dir)
        # 审查八轮（功能接线）：middleware 的 request/response/error 时机——包一层
        # 客户端即可覆盖列表/分页请求。两个 v2 取数器的客户端属性名不同
        # （fetchers.HttpFetcher 用 .http，modules 版用 .client），逐个探测。
        if middleware is not None:
            _cl = getattr(fetcher, "http", None) or getattr(fetcher, "client", None)
            if _cl is not None:
                _mw_c = _MwClient(_cl, middleware, logger)
                if hasattr(fetcher, "http"):
                    fetcher.http = _mw_c
                else:
                    fetcher.client = _mw_c
        if hasattr(fetcher, "http") and cache_dir:
            fetcher.http.cache_dir = cache_dir      # 适配器双向透明：写会落到被包客户端

        if dry_run:
            logger.info(f"[dry-run] 取数器 {ftype} 就绪: {source.get('url', source.get('bridge'))}")
            continue

        try:
            rows_raw = fetcher.fetch_list(pagination)
        except (KeyboardInterrupt, MaxRequestsExceeded, BlockDetectedError):
            raise
        except Exception as e:
            # 审查修复（P1，R5）：单迭代失败曾中止全部剩余迭代——多阶段配置里
            # 一个源被 WAF 拦，后两个源的已采数据也跟着丢失出口。降级为跳过本迭代。
            # R118 修复（P2）：resume 模式下失败迭代的检查点行曾随 continue 丢失——
            # 合并导出只写成功迭代，主产物静默回退。改为沿用检查点行参与合并导出
            if resume and prev_rows:
                all_rows.extend(prev_rows)
                logger.warn(f"迭代 {it_name} 抓取失败——沿用检查点 {len(prev_rows)} 条参与合并导出")
            logger.error(f"迭代 {it_name} 抓取失败（跳过该迭代，继续其余）: "
                         f"{type(e).__name__}: {str(e)[:140]}")
            continue
        logger.info(f"原始记录: {len(rows_raw)}")
        if source.get("recon"):
            # 纯侦察模式（版权中心战例）：只要网络日志/现场证据，不做记录抽取与导出
            ev = [p for p in out_dir.iterdir() if p.name in
                  ("capture_all.json", "last_page.html")] if out_dir.exists() else []
            logger.info(f"[recon] 纯侦察模式：跳过抽取/导出。证据文件: "
                        f"{[str(p) for p in ev] or '（无捕获——检查 capture 配置）'}")
            if rows_raw:
                (out_dir / "recon_records.json").write_text(
                    json.dumps(rows_raw, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
                logger.info(f"[recon] 原始记录 {len(rows_raw)} 条已存 {out_dir / 'recon_records.json'}")
            return {"recon": True, "records": len(rows_raw), "evidence": [str(p) for p in ev]}

        # 智联战例：record.fields 未声明曾静默丢字段——自动按 source 字段映射（保留原始列名）
        # batch1800 战训修复：此前只看第一行的键——首行是 GET 时，后续 POST 记录的
        # _post_data/_method 在映射时被静默丢弃（capture 主路径的 POST 体修复被这层吃掉）。
        # batch2200 再修：前 20 行仍不够——混合批次里 GET 响应常占满前 20 行，
        # POST 行在第 50 行才出现时其请求体照样丢。改为全行键并集 + `_` 前缀
        # 元数据键（_post_data/_method 等）无条件保留（capture 元数据永不被映射丢弃）。
        rec_fields = (config.get("record", {}) or {}).get("fields")
        if not rec_fields and rows_raw and isinstance(rows_raw[0], dict):
            _keys: list = []
            for _r in rows_raw:
                if isinstance(_r, dict):
                    for _k in _r:
                        if _k not in _keys:
                            _keys.append(_k)
            rec_fields = {k: {"from": k} for k in _keys}
            logger.info(f"record.fields 未声明——已按 {len(rows_raw)} 行键并集"
                        f"自动映射 {len(rec_fields)} 列（需改名请在 record.fields 声明）")
        rows = [map_record(r, rec_fields) for r in rows_raw]
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
            # OCR R131（实战反馈）：行内无 url 字段时曾无条件写 r["url"]=""——
            # 恒空列被 verify 判"死列：抽取链路断裂"。仅在有值时补全绝对地址
            _u = _merge_url(r.get("url") or "", source.get("base_url", ""))
            if _u:
                r["url"] = _u
            # 详情/下载的 URL 字段也补全为绝对地址（相对路径场景）
            _dlf = (config.get("download", {}) or {}).get("url_field")
            _dtf = (config.get("detail", {}) or {}).get("url_field")
            for _f in {_dlf, _dtf} - {None, "url"}:
                if r.get(_f):
                    # R88：原字段不再原地改写（裸 ID 字段曾连 POST body 插值一起
                    # 被污染成 URL）——绝对地址写 <字段>_final，消费方优先读它
                    r[_f + "_final"] = _merge_url(str(r[_f]), source.get("base_url", ""))
            r["source_name"] = name

        # 中间件：数据产出
        for r in rows:
            middleware.run("data", {"record": r, "title": r.get("title"), "url": r.get("url")})

        rows = run_pipeline(rows, _resolve_template(config.get("pipeline", []), ivars), log_prefix=f"[{name}] ")

        # 增量去重
        _pending_keys = []  # 无增量/seen 为 None 时保持空列表（导出后标记点引用）
        if seen is not None:
            before = len(rows)
            kept = []
            _pending_keys = []  # 审查修复 P1：延后到导出成功再标记（镜像 v3 契约）
            _rk = inc.get("key", "id")
            for r, key in zip(rows, _row_keys(rows, _rk)):
                # 收官十三轮（审查 H，实测）：去重键为空曾整条静默丢弃（不入 kept
                # 也不告警，日志还报"跳过已见"）；复合键各项全缺时 record_key 返回
                # 真值 "|"，本轮全保留但导出后 mark("|") 落盘——**下一轮这批记录
                # 全被当已见丢弃**。与 engine_v3 同口径：键不完整 = 无键，照常保留、
                # 不判重也不标记（_row_keys 已把无键行映射为 ""）
                if not key:
                    kept.append(r)
                    continue
                if not seen.is_seen(key):
                    _pending_keys.append(key)
                    kept.append(r)
            rows = kept
            logger.info(f"增量去重: {before} -> {len(rows)}（跳过已见）")
            # 审查修复（P2，R6）：跨迭代同记录曾重复进合并导出——本迭代导出
            # 成功后立即标记已见，下一迭代 is_seen 才能拦住。
            # R10 复查修正：标记必须在 export_rows 成功【之后】（曾放 dedup
            # 后立刻标——limit 截断/详情/导出崩溃会让这些行被 resume 永久跳过）

        if limit:
            rows = rows[:limit]
            # R11 审查修复（P1）：截断曾不裁 keys——被截掉的行已标已见却从未导出。
            # R22 修复（数据丢失级，实测复现）：按数量裁仍不对——行里含无键行时
            # `_pending_keys[:len(rows)]` 裁的是"前 N 个 key"而非"前 N 行的 key"，
            # 会把从未导出的行的 key 一并标记（limit=2 冒烟 → 全量跑时该记录被
            # 永久跳过）。改为按截断后的实际行重算（与去重循环同一 helper）。
            if seen is not None:
                _pending_keys = [k for k in
                                 _row_keys(rows, inc.get("key", "id")) if k]
            logger.info(f"--limit {limit}: 截断到 {len(rows)} 条")

        # 详情
        detail = config.get("detail", {})
        if detail.get("enabled"):
            # 断点续跑：从检查点合并已抓详情，避免重复请求
            if resume and checkpoint:
                old_rows = checkpoint.load_rows()
                if old_rows:
                    # 审查八轮（C2）：keyf 默认曾为 "id"，与取数侧 url_field 默认
                    # "url" 不一致——列表行通常无 id 字段，新旧行键全为 ""。而
                    # record_key 契约明确 "" = 无键（不丢弃也不误合并），唯一
                    # 没守这个契约的就是这里：old_by_key 坍缩成 {"": 最后一条
                    # 旧记录}，新行全部合并进同一条旧详情且跳过重抓（静默串行）。
                    # 修法：or 链对齐 url_field（get 的默认值不挡显式 null）+
                    # 构建与查找两侧都跳过无键行
                    keyf = detail.get("resume_key") or detail.get("url_field") or "url"
                    old_by_key = {}
                    for o in old_rows:
                        _ok = record_key(o, keyf)
                        if _ok:
                            old_by_key[_ok] = o
                    merged = 0
                    for r in rows:
                        k = record_key(r, keyf)
                        if not k:
                            continue
                        o = old_by_key.get(k)
                        if o and (o.get("detail_body") or "").strip():
                            for kk, vv in o.items():
                                if kk not in r or not r.get(kk):
                                    r[kk] = vv
                            merged += 1
                    logger.info(f"断点续跑：从检查点合并 {merged} 条详情")
            rows = fetch_details(rows, detail, anti_iter, checkpoint=checkpoint, logger=logger,
                                 source=source, middleware=middleware)
            # 收官十五轮（用户复盘）：v2 的 pipeline 跑在详情**之前**（见上方 993 行），
            # detail.extract 产出的字段完全够不到清洗——用户实测 regex_extract 派生列
            # 恒 None 且无报错。新增**详情后处理阶段**：detail.post_pipeline（跨引擎
            # 统一键）或 detail.filters（v3 已有同义键）在详情合并后跑一次完整管道。
            # 后置跑的是"过滤/派生"，行数变化会如实反映到导出与 summary
            _post_steps = detail.get("post_pipeline") or detail.get("filters") or []
            if _post_steps:
                _before_post = len(rows)
                rows = run_pipeline(rows, _resolve_template(_post_steps, ivars),
                                    log_prefix=f"[{name}·post] ")
                logger.info(f"详情后处理（{len(_post_steps)} 步）: {_before_post} -> {len(rows)} 条")

        if rows:
            all_rows.extend(rows)
        elif resume and prev_rows:
            all_rows.extend(prev_rows)
            logger.info("本次未抓到新记录，沿用检查点数据")

        base = _resolve_template(output.get("base_name", safe_fname(name)), ivars)  # R93：净化防路径逃逸
        if len(iterations) > 1 and it:
            lab = iterate.get("labels", {}).get(str(it.get(var_name))) if iterate else None
            base = f"{base}_{lab or it_name}"
        if rows and not dry_run:
            paths = export_rows(rows, out_dir, base,
                                formats=output.get("formats") or ["json", "csv", "xlsx"])
            summary[base] = len(rows)
            logger.info(f"{base} 导出: {len(rows)} 条 -> " + ", ".join(f"{k}={v.name}" for k, v in paths.items()))
            # 导出成功后立即标记已见（下一迭代 is_seen 拦住跨迭代重复；
            # 必须在导出成功之后——R10 复查修正）
            if seen is not None and _pending_keys and not dry_run:
                for _k in _pending_keys:
                    seen.mark(_k)

        if graceful.stop:
            break

    # 文件下载
    if all_rows:
        download_files(all_rows, config.get("download"), anti, out_dir, logger=logger,
                       middleware=middleware)

    # 合并导出
    if all_rows and not dry_run:
        base = _resolve_template(output.get("base_name", safe_fname(name)), vars)
        paths = export_rows(all_rows, out_dir, base + "_合并" if len(iterations) > 1 else base,
                            formats=output.get("formats") or ["json", "csv", "xlsx"])
        summary["_merged"] = len(all_rows)
        logger.info("合并导出: " + ", ".join(f"{k}={v.name}" for k, v in paths.items()))
        logger.info(f"总计 {len(all_rows)} 条")
    # 审查修复 P1：导出成功后才标记已见（曾先标后导——导出崩溃=这些行被永久跳过）
    if seen is not None:
        try:
            seen.flush()  # flush_every 不足额时此前一次都不落盘
        except Exception as _flush_err:
            # R128 修复（OCR）：静默吞曾致 resume 状态丢失不可感知
            logger.error(f"seen.flush() 失败（增量去重状态可能未持久化）: "
                         f"{type(_flush_err).__name__}: {_flush_err}")
    if dry_run:
        logger.info("[dry-run] 校验通过，未发起真实请求")
    from .core import request_budget
    _rq = request_budget()
    logger.info("请求统计: {used}{limit}{rem}".format(
        used=_rq["used"],
        limit=f"/{_rq['limit']}" if _rq["limit"] else "",
        rem=f"（剩 {_rq['remaining']}）" if _rq["limit"] else "") + " | "
        + f"记录 {len(all_rows)}")
    # 代理池战况（深度改进①）：v1 路径此前从不汇报 direct_fallbacks——
    # 全池冷却期间静默直连，用户完全无感
    if proxy_pool.size:
        logger.info(f"代理池: {proxy_pool.summary()}")
    # NBS 考核战训：0 条必须留证据（此前不落盘不报错、exit 0 假成功）
    _nodata = (len(all_rows) == 0 and not dry_run)
    _nodata_written = False
    if _nodata:
        try:
            (out_dir / "nodata.json").write_text(json.dumps(
                {"nodata": True, "name": name, "total": 0,
                 "source_url": (config.get("source") or {}).get("url", ""),
                 "budget": _rq, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                ensure_ascii=False, indent=1), encoding="utf-8")
            _nodata_written = True
            logger.warn("⚠️ 0 条记录——已写 nodata.json 留证（0 结果不假成功），请按铁律 3 出诊断说明")
        except Exception as e:
            # 审查修复 P1：写盘失败曾静默——stderr 声称有证据而磁盘上没有
            logger.error(f"nodata.json 写盘失败（{type(e).__name__}: {e}）——"
                         f"0 条结论请直接引用本日志行作证据")
    # R41 审查修复：Logger.tick("records") 全仓零调用点——汇总恒打印"记录 0"，
    # 与上方"请求统计 ... | 记录 N"及导出文件自相矛盾。按最终导出集补一次 tick
    # （循环外单次计入总数，迭代模式多迭代共享本 logger 也不会重复计数）
    logger.tick("records", len(all_rows))
    logger.summary()
    try:
        _run_lock.unlink(missing_ok=True)
    except Exception:
        pass
    return {"name": name, "total": len(all_rows), "summary": summary,
            "nodata": _nodata, "nodata_evidence_written": _nodata_written,
            "budget": _rq}
