#!/usr/bin/env python3
"""🤖 MCP Server（Model Context Protocol，对标 silkworm-mcp / scrape-mcp / cortex-scout）。

让 Claude / Cursor / Codex / 任何支持 MCP 的 AI 客户端，把"万能爬虫引擎"当作原生工具调用：
  - scrape   一键抓取 URL → Markdown/正文/表格/链接（Firecrawl fetch 风格）
  - auto     一句话任务 → AI 自动生成配置并运行（万能爬虫最强入口）
  - crawl    从 URL 递归爬站（Firecrawl crawl 风格）
  - extract  HTML 文本 → 干净 Markdown / 正文 / 表格（省 token）
  - books    图书目录采集：ISBN → 豆瓣评分/出版社 + 京东当当比价（只采书目，不下载正文）
  - check    引擎体检：版本、能力矩阵、战绩、LLM 是否可用

传输：stdio + 换行分隔 JSON-RPC 2.0（MCP 标准），零第三方依赖，可直接被
Claude Desktop / Cursor / Continue / Codex 等通过 stdio 配置接入。

用法:
  python3 -m universal_scraper.mcp_server            # 启动 stdio MCP
  python3 -m universal_scraper.cli mcp               # 等价
  echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | \
    python3 -m universal_scraper.mcp_server --once   # 自测
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

VERSION = "2.2.0"
SERVER_NAME = "universal-scraper"

# --------------------------------------------------------------------------
# 工具定义（MCP tools/list 返回）
# --------------------------------------------------------------------------
# OCR R131（L）：_param 构造器与 _notify 均无调用方（schema 手写字面量）——已删


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "scrape",
        "description": "一键抓取一个 URL，返回干净文本/Markdown/正文/表格/链接。适合：读网页内容、取正文、找表格、收集链接。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标网址（http/https）"},
                "browser": {"type": "boolean", "description": "是否用浏览器渲染 JS 页面（默认 false）"},
                "selector": {"type": "string", "description": "CSS 选择器，只返回匹配内容（如 h1、.content）"},
                "article": {"type": "boolean", "description": "只提取正文（默认 false）"},
                "table": {"type": "boolean", "description": "提取页面里的表格为 JSON（默认 false）"},
                "links": {"type": "boolean", "description": "同时返回页面里的链接（默认 false）"},
                "proxy": {"type": "string", "description": "代理地址（可选）"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "auto",
        "description": "用一句话描述爬虫任务，AI 自动生成配置、自动运行、失败自修复，返回结构化结果。适合：不会写选择器/配置的非技术用户。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "中文/英文任务描述，如：抓取 https://quotes.toscrape.com/ 的名言、作者和标签，翻 2 页"},
                "limit": {"type": "integer", "description": "最多抓取条数（可选）"},
                "rounds": {"type": "integer", "description": "自动重试轮数（默认 2，失败会自修复配置）"},
            },
            "required": ["description"],
        },
    },
    {
        "name": "crawl",
        "description": "从入口 URL 递归爬取整站/子目录，返回每页标题+正文+链接。适合：整站抓取、文档站镜像。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "入口网址"},
                "depth": {"type": "integer", "description": "递归深度（默认 2）"},
                "max_pages": {"type": "integer", "description": "最多抓取页数（默认 100）"},
                "allow": {"type": "string", "description": "只爬匹配该正则的链接（如 ^/docs/）"},
                "deny": {"type": "string", "description": "跳过匹配该正则的链接"},
                "browser": {"type": "boolean", "description": "是否浏览器渲染（默认 false）"},
                "same_domain": {"type": "boolean", "description": "只爬同域名（默认 false）"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "bulk_scrape",
        "description": "批量抓取多个 URL（一次调用替代 N 次往返；逐 URL 隔离失败——"
                       "单个 URL 出错不影响其余）。默认最多 10 个、上限 30（防爆）。"
                       "每个 URL 有间隔（默认 1s）保持礼貌；返回每条的 url/status/摘要。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"},
                         "description": "URL 列表（最多 30 个）"},
                "article": {"type": "boolean", "description": "正文抽取（默认 false）"},
                "selector": {"type": "string", "description": "只取匹配元素的文本（可选）"},
                "max": {"type": "integer", "description": "最多处理几个（默认 10，上限 30）"},
                "interval": {"type": "number", "description": "每个 URL 间隔秒数（默认 1.0）"},
                "max_chars": {"type": "integer", "description": "每条摘要上限字符（默认 8000）"},
            },
            "required": ["urls"],
        },
    },
    {
        "name": "screenshot",
        "description": "对页面截图（浏览器渲染）并保存为图片文件，返回文件路径。"
                       "用于人工核对渲染结果/排障；图片本身不内联返回。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "目标网址（http/https）"},
                "path": {"type": "string",
                         "description": "保存路径（可选；默认 outputs/mcp_shots/<时间戳>.png）"},
                "wait_selector": {"type": "string", "description": "等该元素出现后再截图（可选）"},
                "full_page": {"type": "boolean", "description": "整页截图（默认 false=视口）"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "extract",
        "description": "把 HTML 源码转成干净 Markdown/正文/表格，用于省 token 地阅读网页内容。"
                       "默认先净化（剥离隐藏元素/注释/零宽字符等提示注入载体）再转换；"
                       "main_only=true 时额外收窄到主内容容器（更适合阅读文章页）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "html": {"type": "string", "description": "HTML 源码"},
                "mode": {"type": "string", "description": "markdown（默认）/ article / table"},
                "base_url": {"type": "string", "description": "用于把相对链接转绝对（可选）"},
                "max_chars": {"type": "integer", "description": "输出上限字符（默认 50000，防爆 token）"},
                "main_only": {"type": "boolean", "description": "只取主内容容器（默认 false；正文页建议 true）"},
                "raw": {"type": "boolean", "description": "跳过净化（默认 false；仅在确需原始 HTML 时用）"},
            },
            "required": ["html"],
        },
    },
    {
        "name": "books",
        "description": (
            " 图书目录采集：按 ISBN 清单抓取豆瓣评分/作者/出版社、京东+当当聚合比价，"
            "导出 booklist.csv / booklist.md / books.json / crawl_log.md。"
            "只采集书目数据与公开封面，不下载电子书/PDF，不绕过登录或付费墙；"
            "0 条/部分失败会返回逐项诊断与行动方案（status=NO_DATA/PARTIAL），不会伪装成功。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "object", "description": '内联 spec：{"name":"我的书单","books":[{"title":"书名","isbn":"9787563394180"}]}'},
                "spec_path": {"type": "string", "description": "spec JSON 文件路径（与 spec 二选一；相对路径按本仓库根解析，如 examples/books.spec.json）"},
                "out": {"type": "string", "description": "输出目录（默认 outputs/book_catalog；相对路径基于服务启动目录）"},
                "download_covers": {"type": "boolean", "description": "是否下载公开封面（默认 true）"},
                "interval": {"type": "number", "description": "每个 HTTP 请求间隔秒数（默认 1.0，防限流）"},
            },
        },
    },
    {
        "name": "check",
        "description": "引擎体检：返回版本、已通过测试（100+100+27）、能力矩阵、LLM 可用性、目录结构。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

# --------------------------------------------------------------------------
# 工具实现（薄封装，复用 quick / auto / extractors / engine_v3）
# --------------------------------------------------------------------------

def _cap(obj: Dict[str, Any], limit: int) -> Dict[str, Any]:
    """大字段截断，防止 MCP 返回超限。"""
    out = dict(obj)
    for k, v in list(out.items()):
        if isinstance(v, str) and len(v) > limit:
            out[k] = v[:limit] + f"...(截断，共 {len(v)} 字符)"
        elif isinstance(v, list) and len(v) > 50:
            # 审查二轮（M）：列表截断曾无提示——调用方误以为拿全了
            out[k] = v[:50] + [f"...(截断，共 {len(v)} 项)"]
    return out


def tool_scrape(args: Dict[str, Any]) -> Dict[str, Any]:
    from .quick import fetch_url
    url = str(args.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return {"error": "url 必须是 http/https 开头的完整网址"}
    result = fetch_url(
        url,
        browser=bool(args.get("browser", False)),
        selector=args.get("selector") or None,
        article=bool(args.get("article", False)),
        table=bool(args.get("table", False)),
        proxy=args.get("proxy") or None,
        links=bool(args.get("links", False)),
    )
    if result.get("error"):
        # 审查修复 P1：降级信号在错误路径同样要带出去——残血后端恰恰常以失败示形
        out = {"error": result["error"]}
        if result.get("degraded_backend"):
            out["backend"] = result.get("backend")
            out["degraded_backend"] = True
        return out
    # 只返回有用的字段，避免把整页 text 也带回来（省 token）
    # NBS 夜测战训：backend/degraded_backend 必须随结果走——静默降级 urllib 曾废掉一整晚
    keys = ["url", "status", "markdown", "article", "selector", "tables", "links",
            "backend", "degraded_backend"]
    out = {k: result[k] for k in keys if k in result}
    if "markdown" in out:
        # R27 审查修复（P2）：截断曾无任何标记——长页被当作完整内容消费
        _md = out["markdown"]
        if len(_md) > 50000:
            out["markdown"] = _md[:50000] + f"\n\n...(已截断，原始 {len(_md)} 字符)"
    return out


_BULK_MAX = 30


def tool_bulk_scrape(args: Dict[str, Any]) -> Dict[str, Any]:
    """批量抓取（R20）：一次调用多个 URL，逐 URL 隔离失败 + 服务端硬上限。

    设计取舍（对标 Scrapling bulk_get，但守我们的纪律）：
    - 上限 30 硬钳（agent 一次塞 1000 个 URL 会把本机打成 DDoS 工具）；
    - 逐条 try/except：单条失败进 results[i].error，不影响其余（不整体失败）；
    - 顺序 + 间隔（默认 1s）：批量很容易变成对单站的并发压测，礼貌优先。"""
    import time as _t
    from .quick import fetch_url
    urls = args.get("urls")
    if not isinstance(urls, list) or not urls:
        return {"error": "urls 必须是 URL 数组（非空）"}
    try:
        _max = int(args.get("max") or 10)
    except (TypeError, ValueError):
        _max = 10
    _max = max(1, min(_max, _BULK_MAX))
    _truncated = len(urls) > _max
    try:
        _interval = max(0.0, float(args.get("interval", 1.0)))
    except (TypeError, ValueError):
        _interval = 1.0
    try:
        _mc = max(200, int(args.get("max_chars") or 8000))
    except (TypeError, ValueError):
        _mc = 8000
    results: List[Dict[str, Any]] = []
    for i, u in enumerate(urls[:_max]):
        u = str(u or "").strip()
        if i:
            _t.sleep(_interval)
        if not u.startswith(("http://", "https://")):
            results.append({"url": u, "ok": False, "error": "url 必须是 http/https 开头"})
            continue
        try:
            r = fetch_url(u, article=bool(args.get("article", False)),
                          selector=args.get("selector") or None)
        except Exception as e:
            results.append({"url": u, "ok": False,
                            "error": f"{type(e).__name__}: {str(e)[:160]}"})
            continue
        if r.get("error"):
            results.append({"url": u, "ok": False, "error": r["error"]})
            continue
        body = r.get("article") or r.get("selector") or r.get("markdown") or r.get("text") or ""
        item = {"url": r.get("url") or u, "ok": True, "status": r.get("status")}
        if r.get("backend"):
            item["backend"] = r["backend"]
        if r.get("degraded_backend"):
            item["degraded_backend"] = True
        item["text"] = body[:_mc] + (f"\n...(截断，共 {len(body)} 字符)" if len(body) > _mc else "")
        results.append(item)
    out: Dict[str, Any] = {"count": len(results), "results": results}
    if _truncated:
        out["truncated"] = True
        out["note"] = f"URL 超过上限 {_max} 个，只处理了前 {_max} 个（分批调用）"
    return out


def tool_screenshot(args: Dict[str, Any]) -> Dict[str, Any]:
    """页面截图（R20）：浏览器渲染后落盘图片，返回路径（不内联二进制）。"""
    from datetime import datetime
    from pathlib import Path as _P

    from .quick import fetch_url
    url = str(args.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return {"error": "url 必须是 http/https 开头的完整网址"}
    p = args.get("path")
    if p:
        out = _P(str(p)).expanduser()
        if out.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            return {"error": "path 扩展名须为 .png/.jpg/.jpeg/.webp"}
    else:
        out = _P(__file__).resolve().parent.parent / "outputs" / "mcp_shots" / \
            f"shot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"error": f"无法创建目录 {out.parent}: {e}"}
    try:
        r = fetch_url(url, browser=True, screenshot=str(out),
                      wait_selector=args.get("wait_selector") or None)
    except Exception as e:
        return {"error": f"截图失败: {type(e).__name__}: {str(e)[:160]}"}
    got = r.get("screenshot") or (str(out) if out.exists() else None)
    if not got:
        return {"error": r.get("error") or "截图未产出（浏览器不可用或页面加载失败）"}
    return {"url": url, "path": str(got), "bytes": out.stat().st_size if out.exists() else None}


def tool_auto(args: Dict[str, Any]) -> Dict[str, Any]:
    from .auto import auto_task
    desc = str(args.get("description", "")).strip()
    if not desc:
        return {"error": "description 不能为空"}
    # 审查八轮（MEDIUM）：rounds 此前无任何钳制——0/负数静默跑 0 轮（返回空结果），
    # 10^6 时每轮上限 240s（auto.py 的 round_timeout）可长期占用工具调用。
    _rounds = int(args.get("rounds") if args.get("rounds") is not None else 2)
    _rounds = max(1, min(_rounds, 10))
    out = auto_task(desc,
                    limit=args.get("limit") or None,
                    rounds=_rounds)
    return {
        "name": out.get("name"),
        "result": out.get("result"),
        # 审查十一轮（M）：质量门否决/验证结果曾丢失——客户端无法区分"部分
        # 成功"与干净成功（结构化信号只剩 log_tail 的自然语言）。显式传递
        "quality_gate_failed": out.get("quality_gate_failed"),
        "verify": out.get("verify"),
        "summary": out.get("summary"),
        "sample": out.get("sample", [])[:10],
        "files": out.get("files"),
        "log_tail": (out.get("log") or "")[-1500:],
    }


def tool_crawl(args: Dict[str, Any]) -> Dict[str, Any]:
    from .quick import crawl_url
    url = str(args.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return {"error": "url 必须是 http/https 开头的完整网址"}
    result = crawl_url(
        url,
        depth=int(args.get("depth") if args.get("depth") is not None else 2),
        max_pages=int(args.get("max_pages") if args.get("max_pages") is not None else 100),
        allow=args.get("allow") or None,
        deny=args.get("deny") or None,
        browser=bool(args.get("browser", False)),
        same_domain=bool(args.get("same_domain", False)),
    )
    if result.get("error"):
        return {"error": result["error"]}
    return _cap(result, 5000)


def tool_extract(args: Dict[str, Any]) -> Dict[str, Any]:
    from .extractors import (ai_safe_markdown, extract_article, extract_tables,
                             html_to_markdown, strip_hidden_content)
    html = str(args.get("html", ""))
    mode = str(args.get("mode") or "markdown").lower()
    base_url = args.get("base_url") or None
    max_chars = int(args.get("max_chars") if args.get("max_chars") is not None else 50000)
    # 审查二十轮（R20，安全）：MCP 输出直接喂给 agent——默认净化掉隐藏元素/
    # 注释/零宽字符（提示注入载体）；raw=true 才放行原始内容
    if not args.get("raw"):
        html = strip_hidden_content(html)
    try:
        if mode == "article":
            txt = extract_article(html)
        elif mode == "table":
            # 审查二轮（H）：曾提前 return 绕过 max_chars——大表格打爆 MCP 响应
            _all_tables = extract_tables(html)
            tbls = _all_tables[:20]
            payload = json.dumps(tbls, ensure_ascii=False, default=str)
            if len(payload) > max_chars:
                tbls = tbls[:5]
                payload = json.dumps(tbls, ensure_ascii=False, default=str)
            if len(payload) > max_chars:
                return {"tables": [], "text": f"(共 {len(_all_tables)} 张表，响应超限已省略——请缩小页面范围)", "mode": "table"}
            return {"tables": tbls, "mode": "table"}
        elif args.get("main_only"):
            # 已净化过——此处只收窄（main_only 缺省 false，保持旧行为兼容）
            txt = ai_safe_markdown(html, base_url=base_url, main_only=True)
        else:
            txt = html_to_markdown(html, base_url=base_url)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if len(txt) > max_chars:
        txt = txt[:max_chars] + f"\n...(截断，共 {len(txt)} 字符)"
    return {"mode": mode, "text": txt}


def _load_books_spec(args: Dict[str, Any]):
    """spec 支持内联对象或本地 JSON 文件路径；返回 (spec_dict, error)。"""
    spec = args.get("spec")
    if isinstance(spec, dict):
        # 结构是否合法交给 build_catalog 统一判定（其 INVALID_SPEC 诊断精确到条目）
        return spec, ""
    sp = str(args.get("spec_path") or "").strip()
    if not sp:
        return None, ('请提供 spec（内联 {"books":[…]} 对象）或 spec_path（.json 文件路径）')
    from pathlib import Path
    p = Path(sp).expanduser()
    # OCR R131（H）：spec_path 曾可读任意绝对路径/../../ 逃逸——MCP 客户端可
    # 传 /etc/passwd 类路径探测文件系统。限定在插件根内（绝对路径仅放行插件
    # 根内部；相对路径解析后同样收归插件根）
    _ROOT = Path(__file__).resolve().parent.parent
    if p.is_absolute():
        try:
            p.relative_to(_ROOT)
        except ValueError:
            return None, f"spec_path 仅允许插件目录内路径（收到绝对路径越界）: {sp}"
    else:
        p = _ROOT / sp
    try:
        p = p.resolve()
        p.relative_to(_ROOT)
    except ValueError:
        return None, f"spec_path 解析后越出插件目录: {sp}"
    if not p.exists():
        return None, f"spec 文件不存在: {sp}（也可改用内联 spec 对象）"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, f"spec 读取/解析失败：{type(e).__name__}: {e}"
    return data, ""


def _clamp_interval(value: Any, default: float = 1.0) -> float:
    """请求间隔钳制 [0,10] 秒；显式 0 允许（仅离线/测试用），NaN 等非法值回落默认。"""
    try:
        iv = float(value)
    except (TypeError, ValueError):
        iv = default
    if iv != iv:  # NaN：比较恒 False，必须在钳制前拦下，否则会变成 0 秒节流
        iv = default
    return min(max(iv, 0.0), 10.0)


# MCP 返回里每本书保留的字段（省 token；全量看 books.json / booklist.csv）
BOOK_SAMPLE_FIELDS = ("no", "isbn", "book_title", "author", "publisher", "publish_year",
                      "douban_rating", "douban_rating_count",
                      "jd_price", "jd_click_link", "dangdang_price", "dangdang_link", "status")


def tool_books(args: Dict[str, Any]) -> Dict[str, Any]:
    from .book_catalog import build_catalog
    spec, err = _load_books_spec(args)
    if err:
        return {"error": err,
                "hint": 'spec 结构：{"name":"书单名","books":[{"title":"书名","isbn":"合法 ISBN-10/13"}]}'}
    # 输出目录限制在 outputs/ 内（审查 P2，R8：与 webui 同一 containment 口径，
    # 防提示注入把文件写到项目外任意路径）
    from pathlib import Path as _P
    _out = _P(str(args.get("out") or "outputs/book_catalog")).expanduser().resolve()
    _out_root = (_P(".") / "outputs").resolve()
    try:
        _out.relative_to(_out_root)
    except ValueError:
        return {"error": f"out 仅允许 outputs/ 内（当前解析为 {_out}）"}
    result = build_catalog(spec, str(_out),
                           download_covers=bool(args.get("download_covers", True)),
                           min_interval=_clamp_interval(args.get("interval"), 1.0))
    status = str(result.get("status") or "")
    rows = result.get("rows") or []
    out: Dict[str, Any] = {
        "status": status,
        "total": len(rows),
        "coverage": result.get("coverage"),
        "files": result.get("files"),
        "sample": [{f: r.get(f) for f in BOOK_SAMPLE_FIELDS} for r in rows[:10]],
        "diagnostics": [
            {"isbn": d.get("isbn"), "status": d.get("status"), "diagnostics": d.get("diagnostics")}
            for d in (result.get("diagnostics") or [])[:20]
        ],
    }
    if status == "INVALID_SPEC":
        out["error"] = result.get("error") or "spec 不合法"
        out["hint"] = 'spec 结构：{"name":"书单名","books":[{"title":"书名","isbn":"合法 ISBN-10/13"}]}'
        return out
    bad_rows = [r for r in rows if r.get("status") != "OK"]
    # 真实可达场景：书单里列了重复 ISBN 时跳过必须可见，不能静默少行
    dup_skipped = int((result.get("coverage") or {}).get("duplicates") or 0)
    if rows and dup_skipped:
        out["duplicates_skipped"] = dup_skipped
        out["notice"] = (f"另有 {dup_skipped} 个重复 ISBN 条目被主键去重跳过"
                         "（同一版本只保留一行）")
    if not rows:
        # 防御层：当前 build_catalog 去重逻辑下“零行且全为重复”构造不出（首条必采），留作回归护栏
        dup = int((result.get("coverage") or {}).get("duplicates") or 0)
        if dup:
            out["error"] = (f"图书采集 0 条结果：spec 里所有条目都是重复 ISBN（{dup} 个重复被主键去重跳过）"
                            "——这不是成功。行动：检查书单是否重复列了同一版本，补充不同 ISBN 后重跑。")
        else:
            out["error"] = ("图书采集 0 条结果——这不是成功。请检查 spec.books 是否为空、"
                            "ISBN 是否有效，然后重试。")
        return out
    bad_n = {s: sum(1 for r in bad_rows if r.get("status") == s) for s in {r.get("status") for r in bad_rows}}
    if all(r.get("status") == "NO_DATA" for r in rows):
        out["error"] = (f"图书采集 {len(rows)} 行但全部无可采数据（豆瓣/京东/当当均未命中）——这不是成功。"
                        "逐项诊断见上方 diagnostics 与 crawl_log.md；"
                        "常见处理：核对 ISBN、补充 douban_subject_id、调大 interval 防限流后重跑。")
        return out
    if not all(r.get("status") == "OK" for r in rows):
        # 部分失败：不伪装成功，给出诊断指针与行动方案
        bad_str = "、".join(f"{k}×{v}" for k, v in sorted(bad_n.items()))
        out["warning"] = (
            f"部分书目未取全字段：{bad_str}。逐项诊断与行动方案见 "
            f"{result['files'].get('log') if result.get('files') else 'crawl_log.md'}；"
            "常见处理：补充 douban_subject_id、降低限流（调大 interval）、稍后重跑。")
    out["problem_rows"] = [{"isbn": r.get("isbn"), "title": r.get("book_title"),
                            "row_status": r.get("status"),
                            "diagnostics": r.get("diagnostics")} for r in bad_rows[:20]]
    return out


def tool_check(_args: Dict[str, Any]) -> Dict[str, Any]:
    from .llm import _get_key
    key = bool(_get_key())
    # 战绩：读取挑战结果（存在则统计）
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    stats = {"gen1": None, "gen2": None, "learnspider": None}
    for key_name, rel in (("gen1", "challenges/results.json"),
                          ("gen2", "challenges2/results.json"),
                          ("learnspider", "challenges/learnspider_results.json")):
        fp = root / rel
        if fp.exists():
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    vals = data.values()
                    passed = sum(1 for v in vals
                                 if isinstance(v, dict) and v.get("status") in (None, "pass"))
                    total = len(vals)
                    stats[key_name] = f"{passed}/{total}"
            except Exception as e:
                # OCR R131（M）：挑战得分统计失败曾静默——得分缺失时无从排查
                import sys as _sys
                print(f"⚠️ 挑战得分统计失败（{key_name}: {type(e).__name__}: {str(e)[:60]}）",
                      file=_sys.stderr)
    return {
        "server": SERVER_NAME,
        "version": VERSION,
        "llm_configured": key,
        "challenge_scores": stats,
        "capabilities": [
            "http_json/http_html/browser/browser_script 四类取数",
            "AI 一句话任务 → 配置 → 自动运行 → 失败自修复",
            "反爬四级：限流/UA指纹/验证码(自动+人工)/浏览器Stealth",
            "断点续跑 / 增量去重 / 代理池 / 并发 / sitemap/robots",
            "输出 JSON/CSV/XLSX，支持附件下载",
            "MCP 原生接入 AI 客户端",
        ],
        "usage_examples": [
            {"tool": "scrape", "args": {"url": "https://example.com", "article": True}},
            {"tool": "auto", "args": {"description": "抓取 https://quotes.toscrape.com/ 的名言和作者，翻 2 页"}},
        ],
    }

TOOL_IMPLS = {
    "scrape": tool_scrape,
    "bulk_scrape": tool_bulk_scrape,
    "screenshot": tool_screenshot,
    "auto": tool_auto,
    "crawl": tool_crawl,
    "extract": tool_extract,
    "books": tool_books,
    "check": tool_check,
}

# --------------------------------------------------------------------------
# JSON-RPC 2.0 分发（MCP stdio：每行一个 JSON 消息）
# --------------------------------------------------------------------------

def _result(id_: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": e}


def _mcp_token_ok(params: Dict[str, Any]) -> bool:
    """可选工具级令牌闸（R20）：US_MCP_TOKEN 未设 → 放行（stdio 默认本地信任）。

    设了令牌就必须匹配（常量时间比较）：适用于 stdio 会话被转发/共享
    （ssh 隧道、agent 编排平台把多会话接到同一进程）的场景。令牌从
    params._token 或 params.token 读，**不记录进日志**。"""
    want = os.environ.get("US_MCP_TOKEN") or ""
    if not want:
        return True
    import hmac
    got = str((params or {}).get("_token") or (params or {}).get("token") or "")
    return bool(got) and hmac.compare_digest(got, want)


def handle_message(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理一条 JSON-RPC 消息；notification 返回 None（不回包）。"""
    method = msg.get("method")
    params = msg.get("params") or {}
    mid = msg.get("id")
    # 审查十三轮（L）：JSON-RPC 通知（无 id）不得回包——此前 notifications/
    # initialized 之外的 notification（如 notifications/cancelled）曾收到
    # {"id": null, ...} 的错误回包，违反协议"不得回复 notification"
    if mid is None:
        return None

    if method == "initialize":
        # 审查十六轮（L）：回显服务端**实际支持**的协议版本——原样回显客户端
        # 版本（甚至 "9999-01-01"/数字 123）会让客户端误以为协商成功
        _pv = str(params.get("protocolVersion") or "2024-11-05")
        if re.match(r"^\d{4}-\d{2}-\d{2}$", _pv) and _pv <= "2024-11-05":
            _supported = _pv      # 旧客户端：尊重其版本
        else:
            _supported = "2024-11-05"  # 非法形态/超出支持的版本 → 服务端实际支持的
        return _result(mid, {
            "protocolVersion": _supported,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": VERSION},
            "instructions": (
                "万能爬虫引擎 MCP：scrape/bulk_scrape/screenshot/auto/crawl/extract/books/check。"
                "auto 是最强入口——给一句话任务即可全自动爬取；bulk_scrape 一次抓多个 URL"
                "（逐条隔离失败）；screenshot 落盘页面截图供人工核对；"
                "books 按 ISBN 清单采集图书目录（豆瓣+比价，不下载正文，0 条会返回诊断与行动方案）。"
                "服务端设了 US_MCP_TOKEN 时，tools/call 需带 params._token。"
            ),
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": TOOLS})
    if method == "tools/call":
        # R20：可选令牌闸（US_MCP_TOKEN；未设置=不启用）
        if not _mcp_token_ok(params):
            return _error(mid, -32001, "未授权：缺少或错误的 _token"
                                        "（服务端已通过 US_MCP_TOKEN 启用令牌校验）")
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOL_IMPLS:
            return _error(mid, -32602, f"未知工具: {name}", {"available": list(TOOL_IMPLS)})
        try:
            out = TOOL_IMPLS[name](args)
        except (TypeError, ValueError) as e:
            # 收官十二轮（审查 L）：客户端参数类型错（depth="abc" 等）曾是 -32603
            # "内部错误"——按 JSON-RPC 口径应是 -32602 Invalid params，否则被当
            # 服务端 bug 而非调用方错误
            return _error(mid, -32602, f"{name} 参数非法: {type(e).__name__}: {e}")
        except Exception as e:
            return _error(mid, -32603, f"{name} 执行失败: {type(e).__name__}: {e}")
        text = json.dumps(out, ensure_ascii=False, default=str)
        return _result(mid, {
            "content": [{"type": "text", "text": text}],
            "isError": bool(out.get("error")),
        })
    if method == "resources/list":
        return _result(mid, {"resources": []})
    if method == "resources/templates/list":
        return _result(mid, {"resourceTemplates": []})
    if method == "prompts/list":
        return _result(mid, {"prompts": []})
    return _error(mid, -32601, f"未知方法: {method}")


def serve_stdio(once: bool = False) -> int:
    """从 stdin 逐行读取 JSON-RPC，处理并写回 stdout。"""
    if once:
        # 自测模式：读入全部输入，处理每行，输出结果后退出
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            # R27 审查修复（P2）：非对象输入（batch 数组/标量）曾 AttributeError
            if not isinstance(msg, dict):
                continue
            # OCR R131（M）：once 模式 handle_message 曾裸调——一条消息内部异常
            # 直接炸掉自测进程；对齐常驻模式的 JSON-RPC 错误响应
            try:
                resp = handle_message(msg)
            except Exception as e:
                resp = _error(msg.get("id"), -32603, f"内部错误: {type(e).__name__}: {e}")
            if resp is not None:
                print(json.dumps(resp, ensure_ascii=False), flush=True)
        return 0

    # 常驻模式：每次只回一条消息（MCP 客户端是请求-响应模型）
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            # R16 审查修复（P0）：print 括号错位（flush 传给了 json.dumps）——
            # 持久模式首个响应即 TypeError，MCP 集成完全不可用（自测路径掩盖）
            # R16 审查修复（P2）：JSON-RPC batch 数组曾 AttributeError
            if not isinstance(msg, dict):
                continue
            resp = handle_message(msg)
        except Exception as e:
            _id = msg.get("id") if isinstance(msg, dict) else None
            resp = _error(_id, -32603, f"内部错误: {type(e).__name__}: {e}")
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), flush=True)
    return 0  # 审查修复：曾隐式返回 None 违反 -> int 契约（与 --once 分支对齐）


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="universal-scraper-mcp",
                                 description="万能爬虫 MCP Server（stdio JSON-RPC）")
    ap.add_argument("--once", action="store_true", help="读一次输入即退出（自测）")
    args = ap.parse_args()
    return serve_stdio(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
