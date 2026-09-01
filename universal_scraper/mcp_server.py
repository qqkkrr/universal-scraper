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
import sys
from typing import Any, Dict, List, Optional

VERSION = "2.2.0"
SERVER_NAME = "universal-scraper"

# --------------------------------------------------------------------------
# 工具定义（MCP tools/list 返回）
# --------------------------------------------------------------------------

def _param(name: str, desc: str, required: bool = True, default: Any = None,
           ptype: str = "string") -> Dict[str, Any]:
    d = {"name": name, "description": desc, "required": required, "type": ptype}
    if default is not None or not required:
        d["default"] = default
    return d


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
        "name": "extract",
        "description": "把 HTML 源码转成干净 Markdown/正文/表格，用于省 token 地阅读网页内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "html": {"type": "string", "description": "HTML 源码"},
                "mode": {"type": "string", "description": "markdown（默认）/ article / table"},
                "base_url": {"type": "string", "description": "用于把相对链接转绝对（可选）"},
                "max_chars": {"type": "integer", "description": "输出上限字符（默认 50000，防爆 token）"},
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
            out[k] = v[:50]
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
        return {"error": result["error"]}
    # 只返回有用的字段，避免把整页 text 也带回来（省 token）
    keys = ["url", "status", "markdown", "article", "selector", "tables", "links"]
    out = {k: result[k] for k in keys if k in result}
    if "markdown" in out:
        out["markdown"] = out["markdown"][:50000]
    return out


def tool_auto(args: Dict[str, Any]) -> Dict[str, Any]:
    from .auto import auto_task
    desc = str(args.get("description", "")).strip()
    if not desc:
        return {"error": "description 不能为空"}
    out = auto_task(desc,
                    limit=args.get("limit") or None,
                    rounds=int(args.get("rounds") or 2))
    return {
        "name": out.get("name"),
        "result": out.get("result"),
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
        depth=int(args.get("depth") or 2),
        max_pages=int(args.get("max_pages") or 100),
        allow=args.get("allow") or None,
        deny=args.get("deny") or None,
        browser=bool(args.get("browser", False)),
        same_domain=bool(args.get("same_domain", False)),
    )
    if result.get("error"):
        return {"error": result["error"]}
    return _cap(result, 5000)


def tool_extract(args: Dict[str, Any]) -> Dict[str, Any]:
    from .extractors import extract_article, extract_tables, html_to_markdown
    html = str(args.get("html", ""))
    mode = str(args.get("mode") or "markdown").lower()
    base_url = args.get("base_url") or None
    max_chars = int(args.get("max_chars") or 50000)
    try:
        if mode == "article":
            txt = extract_article(html)
        elif mode == "table":
            return {"tables": extract_tables(html)[:20]}
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
    if not p.exists():
        cand = Path(__file__).resolve().parent.parent / sp
        if cand.exists():
            p = cand
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
    result = build_catalog(spec, str(args.get("out") or "outputs/book_catalog"),
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
            except Exception:
                pass
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


def _notify(method: str, params: Any = None) -> Dict[str, Any]:
    m = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        m["params"] = params
    return m


def handle_message(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理一条 JSON-RPC 消息；notification 返回 None（不回包）。"""
    method = msg.get("method")
    params = msg.get("params") or {}
    mid = msg.get("id")

    if method == "initialize":
        return _result(mid, {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": VERSION},
            "instructions": (
                "万能爬虫引擎 MCP：可用 scrape/auto/crawl/extract/books/check 六个工具。"
                "auto 是最强入口——给一句话任务即可全自动爬取；"
                "books 按 ISBN 清单采集图书目录（豆瓣+比价，不下载正文，0 条会返回诊断与行动方案）。"
            ),
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOL_IMPLS:
            return _error(mid, -32602, f"未知工具: {name}", {"available": list(TOOL_IMPLS)})
        try:
            out = TOOL_IMPLS[name](args)
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
    if mid is None:
        return None
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
            resp = handle_message(msg)
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
            resp = handle_message(msg)
        except Exception as e:
            resp = _error(msg.get("id"), -32603, f"内部错误: {type(e).__name__}: {e}")
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False, flush=True))


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="universal-scraper-mcp",
                                 description="万能爬虫 MCP Server（stdio JSON-RPC）")
    ap.add_argument("--once", action="store_true", help="读一次输入即退出（自测）")
    args = ap.parse_args()
    return serve_stdio(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
