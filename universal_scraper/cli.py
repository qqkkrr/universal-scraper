#!/usr/bin/env python3
"""万能爬虫 CLI（v2）。

命令:
  run       运行配置任务（--resume/--limit/--dry-run/--var/--log-file）
  validate  校验配置（不抓取）
  scaffold  生成新任务配置模板（快速上手）
  list      列出配置目录
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import run_config
from .config import load_config, ConfigError

SCAFFOLD_TEMPLATE = {
    "http_json": {
        "name": "my_http_json_task",
        "vars": {"keyword": "example"},
        "source": {"type": "http_json", "method": "GET", "url": "https://api.example.com/search?q={{keyword}}",
                   "headers": {"User-Agent": "universal-scraper"}},
        "pagination": {"strategy": "page_param", "page_param": "page", "start": 1, "max_pages": 10,
                       "total_path": "data.total", "records_path": "data.records"},
        "record": {"fields": {"id": {"from": "id"}, "title": {"from": "title"}, "url": {"from": "url"}}},
        "pipeline": [],
        "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": "my_task", "formats": ["json", "csv", "xlsx"]},
        "anti_bot": {"min_interval": 1.0, "max_retries": 3, "http_backend": "requests"},
    },
    "http_html": {
        "name": "my_html_task",
        "source": {"type": "http_html", "method": "GET", "url": "https://example.com/list",
                   "row_css": "tr.item",
                   "fields": {"title": {"css": "td.title"}, "url": {"css": "a", "attr": "href"}}},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}, "url": {"from": "url"}}},
        "pipeline": [],
        "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": "my_task", "formats": ["json", "csv", "xlsx"]},
        "anti_bot": {"min_interval": 1.0, "max_retries": 3, "http_backend": "requests"},
    },
    "browser": {
        "name": "my_browser_task",
        "source": {"type": "browser", "url": "https://example.com", "pool": True,
                   "stealth": True, "remove_overlays": True,
                   "wait": {"selector": "#list", "timeout": 20000},
                   "actions": [{"type": "click", "selector": "a.next", "ms": 800}],
                   "row_css": "li.item",
                   "fields": {"title": {"css": "span.t"}, "url": {"css": "a", "attr": "href"}},
                   "pagination": {"type": "click", "selector": "a.next", "wait_ms": 1500}},
        "pagination": {"strategy": "none", "max_pages": 10},
        "record": {"fields": {"title": {"from": "title"}, "url": {"from": "url"}}},
        "pipeline": [],
        "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": "my_task", "formats": ["json", "csv", "xlsx"]},
        "anti_bot": {"min_interval": 1.0, "captcha": {"strategy": "auto"}},
    },
    "browser_script": {
        "name": "my_bridge_task",
        "source": {"type": "browser_script", "bridge": "../scripts/ggzy_bridge.cjs",
                   "bridge_params": {"keyword": "{{keyword}}", "stage": "{{stage}}"}},
        "iterate": {"var": "stage", "values": ["0001"], "labels": {"0001": "招标"}},
        "vars": {"keyword": "数据中心"},
        "pagination": {"strategy": "none"},
        "record": {"fields": {"title": {"from": "title"}, "url": {"from": "url"}}},
        "pipeline": [],
        "detail": {"enabled": False},
        "output": {"dir": "outputs", "base_name": "my_task", "formats": ["json", "csv", "xlsx"]},
        "anti_bot": {"min_interval": 1.0, "captcha": {"strategy": "auto"}},
    },
}


def main() -> int:
    import signal as _signal
    def _sigterm(*_a):
        # SIGTERM（系统/容器优雅停机）→ 走 KeyboardInterrupt 保存检查点路径
        raise KeyboardInterrupt
    try:
        _signal.signal(_signal.SIGTERM, _sigterm)
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="universal-scraper", description="配置驱动的万能爬虫引擎")
    sub = ap.add_subparsers(dest="cmd", required=True)

    runp = sub.add_parser("run", help="运行配置任务或任务包")
    runp.add_argument("--config", default=None, help="v2 配置 JSON 路径")
    runp.add_argument("--task", default=None, help="v3 任务包目录（tasks/<name>）")
    runp.add_argument("--var", action="append", default=[], help="覆盖变量 k=v")
    runp.add_argument("--resume", action="store_true", help="断点续跑")
    runp.add_argument("--limit", type=int, default=None, help="只处理前 N 条（冒烟测试）")
    runp.add_argument("--dry-run", action="store_true", help="只校验不抓取")
    runp.add_argument("--url", default=None, help="覆盖入口 URL（任务包=start_urls[0]，配置=source.url）")
    runp.add_argument("--log-file", type=Path, default=None, help="日志文件路径")

    valp = sub.add_parser("validate", help="校验配置")
    valp.add_argument("--config", required=True)

    scp = sub.add_parser("scaffold", help="生成任务配置模板或任务包")
    scp.add_argument("--type", choices=list(SCAFFOLD_TEMPLATE.keys()) + ["task"], default="http_json")
    scp.add_argument("--name", default="my_task")
    scp.add_argument("--out", default="configs/new_task.json")

    lst = sub.add_parser("list", help="列出配置目录")
    lst.add_argument("--dir", default="configs")

    jp = sub.add_parser("jobs", help="顺序运行多个任务（编排）")
    jp.add_argument("--file", required=True, help="jobs.json: [{\"task\": \"tasks/a\", \"var\": {\"k\": \"v\"}}]")

    sp = sub.add_parser("schedule", help="定时重复运行一个任务")
    sp.add_argument("--task", required=True)
    sp.add_argument("--every", type=int, default=60, help="间隔秒数")
    sp.add_argument("--times", type=int, default=0, help="运行次数（0=无限）")
    sp.add_argument("--resume", action="store_true", help="每次运行断点续跑")

    mp = sub.add_parser("monitor", help="定时监控网页变化（对比快照输出差异）")
    mp.add_argument("--task", required=True)
    mp.add_argument("--every", type=int, default=300, help="轮询间隔秒数")
    mp.add_argument("--times", type=int, default=0, help="轮询次数（0=无限）")
    mp.add_argument("--key", default="url", help="对比主键字段（默认 url）")
    mp.add_argument("--diff-fields", default=None, help="对比字段，逗号分隔（默认全部）")

    fp = sub.add_parser("fetch", help="一键抓取 URL → 干净文本/Markdown/JSON（Firecrawl CLI 风格）")
    fp.add_argument("url", help="目标 URL")
    fp.add_argument("--browser", action="store_true", help="用浏览器渲染（JS/SPA/需要点击的页面）")
    fp.add_argument("--selector", default=None, help="CSS 选择器，只提取该区域文本")
    fp.add_argument("--article", action="store_true", help="自动抽取正文")
    fp.add_argument("--table", action="store_true", help="抽取表格为 JSON")
    fp.add_argument("--json", action="store_true", help="输出完整 JSON 结构")
    fp.add_argument("--out", default=None, help="输出文件路径（默认 outputs/fetch_*.md|json）")
    fp.add_argument("--proxy", default=None, help="代理，如 http://127.0.0.1:7890")
    fp.add_argument("--actions", default=None, help="动作链 JSON，如 [{\"type\":\"click\",\"selector\":\"#more\"}]")
    fp.add_argument("--wait", default=None, help="等待选择器出现（浏览器模式）")
    fp.add_argument("--links", action="store_true", help="同时提取页面所有外链（Firecrawl map 风格）")
    fp.add_argument("--screenshot", default=None, help="浏览器截全页图保存路径（Firecrawl screenshot 风格）")
    fp.add_argument("--links-allow", default=None, help="只保留匹配该正则的外链")
    fp.add_argument("--links-deny", default=None, help="排除匹配该正则的外链")
    fp.add_argument("--cdp", default=None, help="附加调试 Chrome（如 http://127.0.0.1:9222），侦察与正式跑同通道")

    cp = sub.add_parser("crawl", help="从 URL 递归爬站（Firecrawl crawl 风格）")
    cp.add_argument("url", help="入口 URL")
    cp.add_argument("--depth", type=int, default=2, help="最大递归深度（默认 2）")
    cp.add_argument("--max", type=int, default=100, help="最大抓取页数（默认 100）")
    cp.add_argument("--allow", default=None, help="只跟随后缀匹配该正则的链接，如 /docs/")
    cp.add_argument("--deny", default=None, help="跳过匹配该正则的链接")
    cp.add_argument("--browser", action="store_true", help="用浏览器渲染（JS 页面）")
    cp.add_argument("--proxy", default=None, help="代理")
    cp.add_argument("--concurrency", type=int, default=4, help="并发数（默认 4）")
    cp.add_argument("--out", default=None, help="导出文件名前缀（默认 crawl_<host>）")
    cp.add_argument("--robots", action="store_true", help="尊重 robots.txt（Disallow 跳过 + Crawl-delay）")
    cp.add_argument("--sitemap", default=None, help="用 sitemap.xml 作为种子（Crawlee SitemapRequestLoader 风格）")
    cp.add_argument("--same-domain", action="store_true", help="只跟进同 hostname 链接（Crawlee same-hostname 策略）")
    cp.add_argument("--json", action="store_true", help="输出完整 JSON 结果")

    ap_auto = sub.add_parser("auto", help="🤖 一句话任务：AI 自动生成配置并运行")
    ap_auto.add_argument("desc", nargs="+", help="任务描述，如：抓取某网站的名言、作者和标签，翻 2 页")
    ap_auto.add_argument("--limit", type=int, default=None, help="最多条数（可选）")

    ap_agent = sub.add_parser("agent", help="🕹️ LLM 浏览器代理：不写选择器，AI 看页面自己点/翻/抽（browser-use 路线）")
    ap_agent.add_argument("desc", nargs="+", help="任务描述")
    ap_agent.add_argument("--url", default="", help="入口 URL（可选，留空用当前页面）")
    ap_agent.add_argument("--max-steps", type=int, default=12, help="最大操作步数（默认 12）")
    ap_agent.add_argument("--cdp", default="", help="附着已登录 Chrome：http://127.0.0.1:9222（淘宝/登录站必用）")
    ap_agent.add_argument("--limit", type=int, default=None, help="最多条数（可选）")

    mcp_p = sub.add_parser("mcp", help="🤖 启动 MCP Server（stdio，供 Claude/Cursor/Codex 调用）")
    mcp_p.add_argument("--once", action="store_true", help="自测：读一次输入即退出")

    wp = sub.add_parser("webui", help="启动可视化 Web 界面（零依赖本地版）")
    wp.add_argument("--port", type=int, default=8642, help="端口（默认 8642）")
    wp.add_argument("--host", default="127.0.0.1", help="监听地址（默认本机）")
    wp.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    wp.add_argument("--token", default="", help="访问令牌（share 模式建议设置；也可用 US_WEBUI_TOKEN）")
    wp.add_argument("--share", action="store_true", help="分享模式：同网络的人可访问（0.0.0.0）")

    st_p = sub.add_parser("sites", help="🏆 高频网站表与精配解析器状态")
    st_p.add_argument("--run", default="", help="可选：URL 命中精配站点时直接精配抓取")
    st_p.add_argument("--cookie", default="", help="Cookie（配合 --run）")
    st_p.add_argument("--limit", type=int, default=20, help="条数上限")

    bk_p = sub.add_parser("books", help="📚 图书目录采集（豆瓣详情+京东/当当比价，不下载正文）")
    bk_p.add_argument("--spec", required=True, help="spec JSON 路径，如 examples/books.spec.json")
    bk_p.add_argument("--out", default="outputs/book_catalog", help="输出目录（默认 outputs/book_catalog）")
    bk_p.add_argument("--no-covers", action="store_true", help="不下载公开封面")
    bk_p.add_argument("--interval", type=float, default=1.0, help="每个 HTTP 请求间隔秒数（默认 1.0）")

    jp = sub.add_parser("journal", help="📚 期刊论文批量下载（magtech 系统，沈阳体育学院学报已精配）")
    jp.add_argument("--site", default="sytyxb", help="期刊站点（默认 sytyxb=沈阳体育学院学报）")
    jp.add_argument("--since", type=int, default=2024, help="起始年份（默认 2024）")
    jp.add_argument("--out", default="", help="输出目录（默认 outputs/journal_<site>）")
    jp.add_argument("--workers", type=int, default=6, help="并发数")
    jp.add_argument("--no-meta", action="store_true", help="跳过摘要/关键词拉取（更快）")
    jp.add_argument("--fulltext", action="store_true", help="科研管理模式：官方 PDF 受限时追加公开 MAG XML 全文 PDF")
    jp.add_argument("--list-only", action="store_true", help="只出论文清单（标题/作者/期次/DOI，不拉摘要不下载PDF）")
    jp.add_argument("--pdf-batch-resume", action="store_true",
                    help="断点续传批抓 PDF（登录态 CDP 模式；需清单已生成+调试 Chrome 已登录；每 IP 日配额约20篇，换 IP 重跑即可续）")
    jp.add_argument("--cdp", default="http://127.0.0.1:9222", help="CDP 调试 Chrome 地址（配 --pdf-batch-resume）")

    pr_p = sub.add_parser("proxy", help="🔄 免费代理池自动构建（抓取+验证+入库）")
    rp_p = sub.add_parser("report", help="📊 爬取CSV → 自动可视化报告（概览/统计/分组/分布图）")
    rp_p.add_argument("csv", help="输入 CSV 文件")
    rp_p.add_argument("--group", default=None, help="分组列名（可选）")
    rp_p.add_argument("--out", default="report.html", help="输出 HTML 报告路径")
    pr_p.add_argument("--refresh", action="store_true", help="抓取公开源代理并验证")
    pr_p.add_argument("--out", default="outputs/proxies.txt", help="输出文件")
    pr_p.add_argument("--workers", type=int, default=30)
    pr_p.add_argument("--target-url", default="", help="目标站真实页面URL（战训：通用靶可用率与目标站无关，必须打目标站）")
    pr_p.add_argument("--marker", default="", help="目标页唯一文案标记（如站名），断言命中才算可用")
    pr_p.add_argument("--sample", type=int, default=600, help="每轮抽样校验数")
    pr_p.add_argument("--status", action="store_true", help="查看三态账本统计（fresh/alive/dead/burned）")

    sub.add_parser("doctor", help="🩺 自检：依赖/Node/浏览器/端口/仓库/输出目录")

    llm_p = sub.add_parser("llm", help="🤖 显示/切换 AI 模型配置（主模型 + 视觉模型）")
    llm_p.add_argument("--model", default="", help="切换主模型，如 qwen-max / deepseek-chat")
    llm_p.add_argument("--vision", default="", help="切换视觉模型，如 qwen-vl-max")
    llm_p.add_argument("--base", default="", help="主模型接口，如 https://api.deepseek.com/v1")
    llm_p.add_argument("--key", default="", help="API Key（写入 ~/.zshenv 持久生效）")

    ck_p = sub.add_parser("cookies", help="🍪 浏览器登录态 → Cookie 直抓串（登录一次，HTTP 直抓复用）")
    ck_p.add_argument("--session", default="outputs/.session/session.json", help="storageState JSON 路径")
    ck_p.add_argument("--domain", default="", help="按域名过滤，如 jd.com / dianping.com / weibo.com")
    ck_p.add_argument("--out", default="", help="同时写入文件（如 /tmp/jd_cookie.txt）")
    ck_p.add_argument("--from-cdp", action="store_true",
                      help="直接从 9222 调试 Chrome 导出 Cookie（CDP 附加运行不落盘 session.json 时的正路）")

    dp_p = sub.add_parser("dianping", help="🌶️ 大众点评专用：Cookie 直抓搜索页列表（绕开验证码/csec）")
    dp_p.add_argument("--keyword", required=True, help="关键词，如 美食 / 烤肉")
    dp_p.add_argument("--city", type=int, default=2, help="城市 ID（默认 2=北京，上海=1）")
    dp_p.add_argument("--cookie", default="", help="已登录大众点评的浏览器 Cookie 整串")
    dp_p.add_argument("--cookie-file", default="", help="或从文件读取 Cookie")
    dp_p.add_argument("--limit", type=int, default=10, help="抓前 N 家（默认 10）")
    dp_p.add_argument("--proxy", default="", help="可选：住宅代理 http://user:pass@host:port")
    dp_p.add_argument("--out", default="", help="导出文件名前缀")

    sub.add_parser("ip", help="🌐 查看当前出口 IP 与运营商（换网络后确认）")

    cdp_p = sub.add_parser("cdp", help="🔗 调试 Chrome (9222) 辅助：列标签页 / 查登录态 / 导 Cookie")
    cdp_p.add_argument("--list-tabs", action="store_true", help="列出所有打开的标签页（默认）")
    cdp_p.add_argument("--login-state", default="", help="查某域名的 Cookie 数量与名称，如 taobao.com")
    cdp_p.add_argument("--out", default="", help="把该域名的 Cookie 串写入文件（配合 --login-state）")

    jr_p = sub.add_parser("jsrecon", help="🔍 接口侦察：下载页面 JS 包自动提取候选 API 端点（SPA 先于 capture 使用）")
    jr_p.add_argument("url", help="目标页面 URL")
    jr_p.add_argument("--max-scripts", type=int, default=6, help="最多分析的 JS 包数（默认 6）")
    jr_p.add_argument("--out", default=None, help="结果 JSON 保存路径（目录自动补 jsrecon.json）")

    bt_p = sub.add_parser("batch", help="🗂️ 批量任务队列：next/done/fail/nodata/retry/status（断点续跑+优先级）")
    bt_p.add_argument("--queue", required=True, help="队列 JSON 文件（[{id,text,status,attempts,result,priority?}]）")
    bt_p.add_argument("action", choices=["next", "done", "fail", "blocked", "nodata", "retry", "status"],
                      help="队列操作（nodata=数据不存在于公开渠道，区别于爬取失败）")
    bt_p.add_argument("item_id", nargs="?", default=None, help="任务 id（done/fail/blocked/nodata/retry 时必填）")
    bt_p.add_argument("--result", default="", help="核对结论/失败原因（写入台账）")

    bd_p = sub.add_parser("budget", help="🚧 域名礼貌预算/封锁台账：mark/check/list（跨运行持久）")
    bd_p.add_argument("--mark", default=None, help="登记封锁事件：域名")
    bd_p.add_argument("--hours", type=float, default=24.0, help="冷却小时数（默认 24）")
    bd_p.add_argument("--note", default="", help="备注（现象/处置）")
    bd_p.add_argument("--check", default=None, help="查询某域名是否冷却中")
    bd_p.add_argument("--list", action="store_true", help="列出全部台账")
    bd_p.add_argument("--file", default=None, help="台账文件路径（默认 ~/.universal_scraper/domain_budget.json）")

    c2p = sub.add_parser("capture2config", help="⚡ 捕获→可重放 http_json 配置（POST体/方法/翻页模板一步到位）")
    c2p.add_argument("capture", help="捕获文件：capture_all.json 或声明式 <name>.json")
    c2p.add_argument("--referer", default="", help="原页面 URL（写进配置的 Referer 头）")
    c2p.add_argument("--out", default="", help="输出 JSON（默认 <捕获文件>_configs.json）")

    pdf_p = sub.add_parser("pdf", help="📎 附件批量下载 + 表格型 PDF 结构化（pdfplumber/pypdf）")
    pdf_p.add_argument("--download", default=None, help="下载清单 JSON（[{url,name}] 或 [url]）")
    pdf_p.add_argument("--tables", default=None, help="提取某 PDF 的表格 → JSON")
    pdf_p.add_argument("--out", default="", help="输出目录/文件")
    pdf_p.add_argument("--interval", type=float, default=1.0, help="下载间隔秒（礼貌限速）")

    vp = sub.add_parser("verify", help="🧾 复核抓取结果：字段完整率/去重/抽样重抓对比")
    vp.add_argument("--file", required=True, help="结果 JSON 文件，如 outputs/xxx.json")
    vp.add_argument("--network", action="store_true", help="联网抽样重抓对比（默认只做本地检查）")
    vp.add_argument("--data-key", default="", help="JSON 为 dict 包装时取数组的键；未指定则自动探测 data/list/rows/items")

    args = ap.parse_args()

    if args.cmd == "list":
        d = Path(args.dir)
        if d.exists():
            for f in sorted(d.glob("*.json")):
                print(f.name)
        return 0

    if args.cmd == "validate":
        try:
            cfg = load_config(Path(args.config))
            print(f"✅ 配置有效: {cfg.get('name')} (source={cfg['source'].get('type')})")
            from .config import collect_warnings
            for w in collect_warnings(cfg):
                print(f"⚠️  {w}")
            return 0
        except ConfigError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1

    if args.cmd == "scaffold" and args.type == "task":
        from .task_bundle import scaffold_task
        out = scaffold_task(args.name, Path(args.out))
        print(f"✅ 任务包已生成: {out}")
        print("   下一步: 改 config.json 的 start_urls/rules/parsers，或写 modules/parser.py")
        print(f"   运行:   python3 -m universal_scraper.cli run --task {out}")
        return 0

    if args.cmd == "scaffold":
        tpl = json.loads(json.dumps(SCAFFOLD_TEMPLATE[args.type]))
        tpl["name"] = args.name
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(tpl, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"✅ 模板已生成: {out}")
        print(f"   运行: python3 -m universal_scraper.cli validate --config {out}")
        print(f"        python3 -m universal_scraper.cli run --config {out}")
        return 0

    if args.cmd == "jobs":
        from .engine_v3 import run_task
        try:
            jobs = json.loads(Path(args.file).read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"❌ 编排文件不存在: {args.file}", file=sys.stderr)
            return 1
        except json.JSONDecodeError as e:
            print(f"❌ 编排文件不是合法 JSON: {e}", file=sys.stderr)
            return 1
        if not isinstance(jobs, list):
            print("❌ 编排文件应为 JSON 数组：[{\"task\": \"tasks/a\"}, ...]", file=sys.stderr)
            return 1
        total = 0
        for i, job in enumerate(jobs, 1):
            tp = Path(job["task"])
            print(f"[jobs] {i}/{len(jobs)} {tp}", flush=True)
            try:
                r = run_task(tp, overrides=job.get("var"), limit=job.get("limit"),
                             resume=bool(job.get("resume")))
            except Exception as e:
                print(f"[jobs] {i} 失败跳过: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
                continue
            total += r.get("total", 0)
        print(json.dumps({"jobs": len(jobs), "total_items": total}, ensure_ascii=False))
        return 0

    if args.cmd == "monitor":
        from .engine_v3 import run_task
        import time
        key_field = args.key
        diff_fields = args.diff_fields.split(",") if args.diff_fields else None
        tp = Path(args.task)
        snap_path = Path("outputs") / f".snapshot_{tp.name}.json"
        old_snap = {}
        if snap_path.exists():
            try:
                import json as _json
                old_snap = _json.loads(snap_path.read_text(encoding="utf-8"))
            except Exception:
                old_snap = {}
        run_no = 0
        while args.times == 0 or run_no < args.times:
            run_no += 1
            print(f"[monitor] 第 {run_no} 次抓取 {tp}", flush=True)
            run_task(tp)
            # 读取任务输出
            cfg = json.loads((tp / "config.json").read_text(encoding="utf-8"))
            out_json = Path("outputs") / f"{cfg.get('output', {}).get('base_name', tp.name)}.json"
            rows = []
            if out_json.exists():
                rows = json.loads(out_json.read_text(encoding="utf-8"))
            snap = {str(r.get(key_field)): r for r in rows if r.get(key_field)}
            added = [k for k in snap if k not in old_snap]
            removed = [k for k in old_snap if k not in snap]
            changed = []
            for k in snap:
                if k in old_snap and snap[k] != old_snap[k]:
                    if diff_fields is None or any(snap[k].get(f) != old_snap[k].get(f) for f in diff_fields):
                        changed.append(k)
            if added or removed or changed:
                print(f"[monitor] 变化: 新增 {len(added)} | 消失 {len(removed)} | 变更 {len(changed)}", flush=True)
                for k in added[:10]:
                    print(f"  + {k}", flush=True)
                for k in removed[:10]:
                    print(f"  - {k}", flush=True)
                for k in changed[:10]:
                    print(f"  ~ {k}", flush=True)
            else:
                print(f"[monitor] 无变化（共 {len(snap)} 条）", flush=True)
            snap_path.write_text(json.dumps(snap, ensure_ascii=False, default=str), encoding="utf-8")
            old_snap = snap
            if args.times == 0 or run_no < args.times:
                print(f"[monitor] 等待 {args.every}s...", flush=True)
                time.sleep(args.every)
        return 0

    if args.cmd == "schedule":
        from .engine_v3 import run_task
        import time as _time
        run_no = 0
        while args.times == 0 or run_no < args.times:
            run_no += 1
            print(f"[schedule] 第 {run_no} 次运行 {args.task}", flush=True)
            run_task(Path(args.task), resume=args.resume)
            if args.times == 0 or run_no < args.times:
                print(f"[schedule] 等待 {args.every}s...", flush=True)
                _time.sleep(args.every)
        return 0

    if args.cmd == "auto":
        from .auto import run_auto_cli
        out = run_auto_cli(" ".join(args.desc), limit=args.limit)

    if args.cmd == "agent":
        from .agent import run_agent_cli
        out = run_agent_cli(" ".join(args.desc), url=args.url,
                            max_steps=args.max_steps, cdp=args.cdp, limit=args.limit)
        if out.get("error"):
            print(f"❌ {out['error']}", file=sys.stderr)
            return 1
        print(json.dumps({"name": out.get("name"), "result": out.get("result"),
                          "files": out.get("files")}, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "mcp":
        from .mcp_server import serve_stdio
        return serve_stdio(once=args.once)

    if args.cmd == "sites":
        from .sites import list_sites, run_site
        if args.run:
            r = run_site(args.run, cookie=args.cookie, limit=args.limit)
            if r.get("error"):
                print(f"❌ {r['error']}")
                return 1
            print(f"🏆 精配[{r.get('site')}] {r['total']} 条：")
            for row in r["rows"][:10]:
                print("  ", json.dumps({k: v for k, v in row.items() if k != "_site"}, ensure_ascii=False)[:200])
            print("  导出:", list(r["files"].values()))
            return 0
        print("🏆 高频网站表（v1）：")
        for s in list_sites():
            print(f"  {s['status']} {s['name']:<6} {s['domain']:<22} {s['desc']}  [{s['difficulty']}]")
        return 0

    if args.cmd == "books":
        from .book_catalog import build_catalog
        spec_path = Path(args.spec)
        if not spec_path.exists():
            print(f"❌ spec 文件不存在: {spec_path}", file=sys.stderr)
            return 1
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            result = build_catalog(spec, args.out, download_covers=not args.no_covers,
                                   min_interval=args.interval)
        except Exception as e:
            print(f"❌ books 执行失败: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("status") != "INVALID_SPEC" else 1

    if args.cmd == "doctor":
        from .doctor import main as doctor_main
        return doctor_main()

    if args.cmd == "llm":
        from .llm import LLMClient
        from pathlib import Path as _P
        _zs = _P.home() / ".zshenv"
        _lines = _zs.read_text().splitlines() if _zs.exists() else []
        def _upsert(k, v):
            nonlocal _lines
            _lines = [ln for ln in _lines if not ln.startswith(f"export {k}=")]
            _lines.append(f"export {k}={v!r}")
        changed = False
        if args.key:
            _upsert("QWEN_API_KEY" if "dashscope" in (args.base or "") or not args.base else "OPENAI_API_KEY", args.key)
            changed = True
        if args.model:
            _upsert("LLM_MODEL", args.model); changed = True
        if args.vision:
            _upsert("VISION_MODEL", args.vision); changed = True
        if args.base:
            _upsert("OPENAI_BASE_URL", args.base); changed = True
        if changed:
            _zs.write_text("\n".join(_lines) + "\n", encoding="utf-8")
            print("✅ 已写入 ~/.zshenv（新终端/重启 WebUI 后生效）")
        for k, v in LLMClient.describe().items():
            print(f"  {k}: {v}")
        print("\n切换示例：")
        print("  us llm --model qwen-max            # 千问最强")
        print("  us llm --model deepseek-v4-flash --base https://api.deepseek.com/v1 --key sk-xxx   # DeepSeek")
        print("  us llm --vision qwen-vl-max        # 视觉模型（看截图/验证码）")
        return 0

    if args.cmd == "proxy":
        from pathlib import Path as _PP
        if args.status:
            from .proxy_fetch import PoolState
            st = PoolState(_PP(args.out).with_suffix(".pool.json"))
            stats = st.stats()
            print(json.dumps({"pool_file": str(st.path), "stats": stats,
                              "usable_now": len(st.usable())}, ensure_ascii=False))
            return 0
        if args.refresh:
            from .proxy_fetch import refresh as _pf_refresh
            r = _pf_refresh(out=args.out, workers=args.workers,
                            target_url=args.target_url or None,
                            marker=args.marker or None, sample=args.sample)
            print(json.dumps(r, ensure_ascii=False))
            if args.target_url:
                print("（目标站校验模式：可用率即真实可用率，可直接投入任务）", file=sys.stderr)
            else:
                print("（通用连通校验：对有门禁的站点可用率会虚高，建议 --target-url + --marker）",
                      file=sys.stderr)
            return 0 if r.get("ok", 0) > 0 else 1
        # 无 --refresh：显示现有代理池（不抓取）
        p = _PP(args.out)
        if p.exists():
            lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            print(json.dumps({"ok": len(lines), "file": str(p.resolve())}))
            return 0 if lines else 1
        print(json.dumps({"ok": 0, "file": str(p), "msg": "代理池不存在，请用 --refresh 构建"}))
        return 1

    if args.cmd == "report":
        from .report import generate as _report_gen
        try:
            r = _report_gen(args.csv, args.group, args.out)
        except (FileNotFoundError, ValueError) as e:
            print(f"❌ 报告生成失败: {e}", file=sys.stderr)
            print("用法: us report <csv> [--group 列名] [--out 报告.html]", file=sys.stderr)
            return 1
        print(f"✅ 报告已生成：{r['report']}（{r['rows']}行 / {r['cols']}列 / {r['numeric']}数值列 / {r['groups']}组）")
        return 0

    if args.cmd == "cookies":
        import json as _json
        from pathlib import Path as _P
        if getattr(args, "from_cdp", False):
            # CDP 附加运行不落盘 storageState——直接从调试 Chrome 取全部 Cookie
            import os as _os
            import subprocess as _sp
            from .runtime import resolve_node, resolve_node_path
            _js = (
                'const {chromium}=require("playwright");'
                '(async()=>{const b=await chromium.connectOverCDP("http://127.0.0.1:9222");'
                'const ctx=b.contexts()[0];if(!ctx){console.error("CDP 无浏览器上下文");process.exit(1);}'
                'const cs=await ctx.cookies();console.log(JSON.stringify(cs));'
                'process.exit(0);'  # connectOverCDP 的 ws 会挂住事件循环，必须显式退出
                '})().catch(e=>{console.error(e.message);process.exit(1);})'
            )
            _env = {**_os.environ, "NODE_PATH": resolve_node_path()}
            r = _sp.run([resolve_node(), "-e", _js], capture_output=True, text=True,
                        env=_env, timeout=30)
            if r.returncode != 0:
                print(f"❌ CDP 导出失败: {r.stderr.strip()[:200]}（9222 未运行先跑 open-debug-chrome.sh）",
                      file=sys.stderr)
                return 1
            cs = _json.loads(r.stdout.strip() or "[]")
        else:
            sf = _P(args.session)
            if not sf.exists():
                print(f"❌ 会话文件不存在: {sf}", file=sys.stderr)
                return 1
            try:
                st = _json.loads(sf.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"❌ 解析失败: {e}", file=sys.stderr)
                return 1
            cs = st.get("cookies", []) or []
        dom = (args.domain or "").lower()
        if dom:
            cs = [c for c in cs if dom in str(c.get("domain", "")).lower()]
        if not cs:
            print(f"❌ 无匹配 Cookie（domain={dom or '全部'}）", file=sys.stderr)
            return 1
        pairs = []
        for c in cs:
            k, v = c.get("name", ""), c.get("value", "")
            if k:
                pairs.append(f"{k}={v}")
        out = "; ".join(pairs)
        print(out)
        if args.out:
            _P(args.out).write_text(out, encoding="utf-8")
            print(f"✅ 已写入: {args.out}", file=sys.stderr)
        print(f"（{len(pairs)} 个 cookie，域过滤={dom or '全部'}）", file=sys.stderr)
        return 0

    if args.cmd == "cdp":
        import json as _json
        import os as _os
        import subprocess as _sp
        from .runtime import resolve_node, resolve_node_path
        js = (
            'const {chromium}=require("playwright");'
            '(async()=>{const b=await chromium.connectOverCDP("http://127.0.0.1:9222");'
            'const ctx=b.contexts()[0];if(!ctx){console.error("CDP 无浏览器上下文");process.exit(1);}'
        )
        if args.login_state:
            dom = args.login_state
            js += (f'const cs=(await ctx.cookies()).filter(c=>String(c.domain).includes({json.dumps(dom)}));'
                   f'console.log(JSON.stringify({{"domain":{json.dumps(dom)},"count":cs.length,'
                   f'"names":cs.map(c=>c.name).slice(0,30)}}));')
            if args.out:
                js += (f'require("fs").writeFileSync({json.dumps(args.out)},'
                       f'cs.map(c=>c.name+"="+c.value).join("; "));'
                       f'console.error("已写入 {args.out}");')
        else:
            js += 'console.log(JSON.stringify(ctx.pages().map(p=>p.url())));'
        js += 'process.exit(0);})().catch(e=>{console.error(e.message);process.exit(1);})'
        env = {**_os.environ, "NODE_PATH": resolve_node_path()}
        r = _sp.run([resolve_node(), "-e", js], capture_output=True, text=True, env=env, timeout=30)
        out = (r.stdout or "").strip()
        if out:
            try:
                data = json.loads(out)
                # 闲鱼战例：dict 结果曾按 list 遍历只打印出键名（"空表头"）——按形态分流
                if isinstance(data, dict):
                    for k, v in data.items():
                        print(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}")
                elif isinstance(data, list):
                    for u in data:
                        print(u)
                else:
                    print(data)
            except json.JSONDecodeError:
                print(out)
        if r.stderr.strip():
            print(r.stderr.strip(), file=sys.stderr)
        return 0 if r.returncode == 0 else 1

    if args.cmd == "jsrecon":
        from .quick import js_recon
        r = js_recon(args.url, max_scripts=args.max_scripts, out=args.out)
        if r.get("error"):
            print(f"❌ {r['error']}", file=sys.stderr)
            return 1
        print(f"🔍 接口侦察 {r['url']}：分析 {r['scripts_checked']} 个 JS 包，"
              f"提取 {len(r.get('api_candidates', []))} 个候选端点")
        for ep in r.get("api_candidates", [])[:40]:
            print(f"  {ep}")
        if r.get("base_urls"):
            print(f"📍 baseURL 锚点: {', '.join(r['base_urls'][:8])}")
        if r.get("saved"):
            print(f"✅ 已保存: {r['saved']}")
        print("（候选端点需逐个探测验证：带 UA/Referer/cookie 预热，见反爬手册）")
        return 0

    if args.cmd == "batch":
        from .batch import BatchQueue
        try:
            q = BatchQueue(args.queue)
        except (FileNotFoundError, ValueError) as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
        if args.action == "next":
            it = q.next()
            if not it:
                st = q.status()
                print(json.dumps({"done": True, **st}, ensure_ascii=False))
                return 0
            print(json.dumps(it, ensure_ascii=False))
            return 0
        if args.action == "status":
            print(json.dumps(q.status(), ensure_ascii=False))
            return 0
        if not args.item_id:
            print("❌ done/fail/blocked 需要任务 id", file=sys.stderr)
            return 1
        status_map = {"done": "done", "fail": "failed", "blocked": "blocked",
                      "nodata": "nodata", "retry": "retry"}
        it = q.mark(args.item_id, status_map[args.action], args.result)
        st = q.status()
        print(json.dumps({"marked": it.get("id"), "status": it.get("status"), **st},
                         ensure_ascii=False))
        return 0

    if args.cmd == "capture2config":
        from .capture_gen import generate
        out = args.out or str(Path(args.capture).with_suffix("").resolve()) + "_configs.json"
        r = generate(args.capture, referer=args.referer, out=out)
        for c in r.get("configs", [])[:10]:
            src = c["source"]
            print(f"  {src.get('method','GET'):4s} {src['url'][:80]}  records_path={c['pagination']['records_path'] or '?'}")
        if r.get("saved"):
            print(f"✅ {r['count']} 份配置草案 → {r['saved']}（先 --limit 2 小样验证）")
        return 0 if r.get("count") else 1

    if args.cmd == "pdf":
        if args.download:
            from .pdf_attach import download_attachments
            r = download_attachments(args.download, args.out or "attachments", interval=args.interval)
            print(json.dumps({k: r[k] for k in ("total", "ok", "skipped", "failed", "dir")},
                             ensure_ascii=False))
            return 0 if not r["failed"] else 1
        if args.tables:
            from .pdf_attach import extract_tables
            try:
                tables = extract_tables(args.tables)
            except (ImportError, ValueError, FileNotFoundError) as e:
                print(f"❌ {e}", file=sys.stderr)
                return 1
            out = args.out or str(Path(args.tables).with_suffix(".tables.json").resolve())
            Path(out).write_text(json.dumps(tables, ensure_ascii=False, indent=1), encoding="utf-8")
            n = sum(len(t["rows"]) for t in tables)
            print(f"✅ {len(tables)} 页表格 / {n} 行 → {out}")
            return 0
        print("用法：--download <清单.json> --out <目录> / --tables <pdf>", file=sys.stderr)
        return 1

    if args.cmd == "budget":
        from . import domain_budget as db
        if args.mark:
            r = db.mark(args.mark, hours=args.hours, note=args.note, path=args.file)
            print(json.dumps(r, ensure_ascii=False))
            return 0
        if args.check:
            r = db.check(args.check, path=args.file)
            print(json.dumps(r, ensure_ascii=False))
            return 0 if not r["in_cooldown"] else 2
        if args.list:
            r = db.listing(path=args.file)
            if not r:
                print("（台账为空）")
                return 0
            for d, v in sorted(r.items()):
                mark = "🚫" if v["in_cooldown"] else "✅"
                print(f"  {mark} {d:32s} 剩 {v['remaining_sec']//3600}h（{v['last_event']} {v['note'][:40]}）")
            return 0
        print("用法：--mark <域名> [--hours 24 --note ...] / --check <域名> / --list", file=sys.stderr)
        return 1

    if args.cmd == "journal":
        from .journals import run as journal_run
        summary = journal_run(args.site, since_year=args.since, out_dir=args.out or None,
                              workers=args.workers, with_meta=not args.no_meta,
                              list_only=args.list_only,
                              pdf_batch_resume=args.pdf_batch_resume, cdp=args.cdp)
        if summary.get("error"):
            print(f"❌ {summary['error']}", file=sys.stderr)
            return 1
        if args.fulltext:
            import os, subprocess
            script = _P(__file__).resolve().parent.parent / "scripts" / "kygl_fulltext_download.py"
            env = os.environ.copy()
            if args.out:
                env["KYGL_OUT"] = str(_P(args.out).expanduser().resolve())
            print("🧩 追加官方 MAG XML 全文 PDF（官方 PDF 受权限限制时）...", file=sys.stderr)
            r = subprocess.run([sys.executable, str(script)], env=env, cwd=str(_P(__file__).resolve().parent.parent))
            if r.returncode != 0:
                print("❌ 全文 PDF 追加失败", file=sys.stderr)
                return r.returncode
        return 0

    if args.cmd == "dianping":
        from .dianping import run
        cookie = args.cookie
        if args.cookie_file:
            cookie = Path(args.cookie_file).read_text(encoding="utf-8").strip()
        r = run(args.keyword, city=args.city, cookie=cookie, limit=args.limit,
                proxy=args.proxy or None, out_name=args.out or None)
        if r.get("error"):
            print(f"❌ {r['error']}")
            return 1
        print(f"✅ 大众点评「{args.keyword}」前 {r['total']} 家已抓取：")
        for row in r["rows"]:
            print(f"  {row['shopName']} | 人均¥{row['avgPrice']} | {row['reviewCount']}条点评 | {row['address']} | {row['shopId']}")
        print(f"   导出: {list(r['files'].values())}")
        return 0

    if args.cmd == "ip":
        from .net import detect_ip, detect_system_proxy, power_source
        d = detect_ip()
        if d.get("error"):
            print(f"❌ {d['error']}")
            return 1
        print(f"🌐 当前出口 IP: {d.get('ip')}")
        print(f"   运营商: {d.get('isp')}")
        print(f"   地区: {d.get('city')} {d.get('region')}")
        print(f"   类型: {d.get('org')}")
        # 战训（2026-09 科研管理战役）：系统代理劫持直连 = 烧错配额/换IP无效
        sp = detect_system_proxy()
        if sp.get("enabled") or sp.get("processes"):
            print(f"⚠️  系统代理: 开启（{', '.join(sp['sources']) or '本机进程'}）"
                  f" 出口可能被劫持到 {sp.get('http_proxy') or '本机代理节点'}:{sp.get('port') or '?'}")
            print(f"   本机代理进程: {', '.join(sp['processes']) or '未检出'}")
            print(f"   {sp['warning']}")
        else:
            print("   系统代理: 未检出（直连出口即真实出口）")
        pw = power_source()
        print(f"🔋 电源: {pw.get('source')} {('- ' + pw['caffeinate_hint']) if pw.get('caffeinate_hint') else ''}")
        return 0

    if args.cmd == "verify":
        from .verify import verify_file
        rep = verify_file(args.file, network=args.network, data_key=args.data_key)
        print(f"🧾 复核报告：{rep.get('total', 0)} 条｜{'✅ 全部通过' if rep.get('ok') else '⚠️ 存在问题'}")
        for c in rep.get("checks", []):
            mark = "✅" if c.get("pass", True) else "❌"
            print(f"  {mark} {c['name']}: {c.get('value', '')}")
            for d in c.get("detail", [])[:5]:
                print(f"      - {d.get('url','')} reachable={d.get('reachable')} match={d.get('match')}")
        return 0 if rep.get("ok") else 1

    if args.cmd == "webui":
        from .webui import serve
        import os
        token = getattr(args, "token", "") or os.environ.get("US_WEBUI_TOKEN", "")
        return serve(args.port, args.host, auto_open=not args.no_open,
                     share=args.share, token=token)

    if args.cmd == "crawl":
        from .quick import crawl_url
        result = crawl_url(args.url, depth=args.depth, max_pages=args.max,
                           allow=args.allow, deny=args.deny, browser=args.browser,
                           proxy=args.proxy, concurrency=args.concurrency, out=args.out,
                           respect_robots=args.robots, sitemap=args.sitemap,
                           same_domain=args.same_domain)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"✅ 爬取完成: {result.get('total', 0)} 条 | 抓取 {result.get('fetched', 0)} 页 | 错误 {result.get('errors', 0)}")
            for k, v in result.get("files", {}).items():
                if Path(v).exists():
                    print(f"   {k}: {v}")
        return 0

    if args.cmd == "fetch":
        from .quick import fetch_url, save_result
        actions = None
        if args.actions:
            try:
                actions = json.loads(args.actions)
            except json.JSONDecodeError as e:
                print(f"❌ --actions 不是合法 JSON: {e}", file=sys.stderr)
                return 1
        result = fetch_url(args.url, browser=args.browser, selector=args.selector,
                           article=args.article, table=args.table, proxy=args.proxy,
                           actions=actions, wait_selector=args.wait, links=args.links,
                           links_allow=args.links_allow, links_deny=args.links_deny,
                           screenshot=args.screenshot, cdp=args.cdp)
        if result.get("error"):
            print(f"❌ {result['error']}", file=sys.stderr)
            return 1
        if args.json:
            # --json：stdout 只输出纯 JSON（可管道），保存提示走 stderr
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            if args.out:
                fp = save_result(result, args.out, as_json=True)
                print(f"✅ 已保存: {fp}", file=sys.stderr)
            return 0
        content = result.get("markdown") or result.get("article") or result.get("selector") or result.get("text", "")
        print(f"# {result['url']}  ({result['status']}, {len(result.get('text',''))}B)\n")
        print(content[:20000])
        if result.get("links"):
            print(f"\n--- {len(result['links'])} 个外链 ---")
            for u in result["links"][:200]:
                print(u)
        if args.out:
            fp = save_result(result, args.out, as_json=False)
            print(f"\n✅ 已保存: {fp}")
        return 0

    # run
    if args.task:
        from .engine_v3 import run_task
        tp = Path(args.task)
        if not (tp / "config.json").exists():
            print(f"任务包不存在或缺少 config.json: {tp}", file=sys.stderr)
            return 1
        overrides = {}
        for v in args.var or []:
            if "=" in v:
                k, val = v.split("=", 1)
                overrides[k] = val
        try:
            result = run_task(tp, overrides=overrides, limit=args.limit, resume=args.resume,
                              dry_run=args.dry_run, log_file=args.log_file, start_url=args.url)
        except KeyboardInterrupt:
            print("\n已中断", file=sys.stderr)
            return 130
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    cfg_path = Path(args.config)
    try:
        config = load_config(cfg_path)
    except ConfigError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    if args.url:
        config["source"]["url"] = args.url
    overrides = {}
    for v in args.var:
        if "=" in v:
            k, val = v.split("=", 1)
            overrides[k] = val
    try:
        result = run_config(config, overrides=overrides, base_dir=cfg_path.resolve().parent,
                            resume=args.resume, limit=args.limit, dry_run=args.dry_run,
                            log_file=args.log_file)
    except KeyboardInterrupt:
        print("\n已中断，检查点已保存，可用 --resume 续跑", file=sys.stderr)
        return 130
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
