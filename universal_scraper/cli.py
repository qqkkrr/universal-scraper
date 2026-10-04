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
import os
import sys
import time
from pathlib import Path

from .engine import run_config
from .config import load_config, ConfigError
from .core import MaxRequestsExceeded
from .protocols import BlockDetectedError, RateLimitedError

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


def _guard_out_is_file(p, cmd: str) -> bool:
    """`--out` 语义为"文件路径"的子命令统一守卫：指向已存在目录时干净报错。

    收官十五轮（安全审计 M4）：report/rangedl/cookies/guide 曾直接 write_text /
    os.open —— 目录路径抛 IsADirectoryError 裸栈（fetch --raw 那条还会连带
    丢掉已抓到的正文，已在 fetch 分支单独修）。返回 False = 调用方应中止。
    """
    if not p:
        return True
    pp = Path(str(p)).expanduser()
    if pp.is_dir():
        _ext = ".html" if cmd == "report" else ".json"
        print(f"❌ {cmd}: --out 需要文件名，而 {pp} 是目录——例如 --out {pp}/result{_ext}",
              file=sys.stderr)
        return False
    return True


def _guard_out_is_dir(p, cmd: str) -> bool:
    """`--out` 语义为"目录路径"的子命令守卫：已存在同名普通文件时干净报错。

    审查十轮（M）：session/xhs/bili/books/research 的 mkdir(exist_ok=True) 只
    豁免目录——--out 指向已存在文件时 FileExistsError 裸栈。返回 False = 中止。
    """
    if not p:
        return True
    pp = Path(str(p)).expanduser()
    if pp.exists() and not pp.is_dir():
        print(f"❌ {cmd}: --out 需要目录路径，但已存在同名文件: {pp}",
              file=sys.stderr)
        return False
    return True


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
    runp.add_argument("--limit", type=int, default=None, help="只处理前 N 条（冒烟测试；0=不限，留空=不限）")
    runp.add_argument("--dry-run", action="store_true", help="只校验不抓取")
    runp.add_argument("--replay", action="store_true",
                      help="开发模式（R20）：只读本地响应缓存、绝不发网络——改解析规则时"
                           "不再重打目标站（先正常跑一次生成缓存；未命中结构化失败不是空数据）")
    runp.add_argument("--url", default=None, help="覆盖入口 URL（任务包=start_urls[0]，配置=source.url）")
    runp.add_argument("--log-file", type=Path, default=None, help="日志文件路径")
    runp.add_argument("--max-requests", type=int, default=None,
                      help="任务级请求预算硬闸（含重试，按真实 HTTP 尝试计；触发即停，exit 4）")

    dp2 = sub.add_parser("dryparse", help="🔍 单 URL 预检：过完整 parser 看字段抽取结果"
                                           "（不写盘、几秒定位选择器错误——Scrapy parse 同款心智）")
    dp2.add_argument("--task", required=True, help="v3 任务包目录")
    dp2.add_argument("--url", required=True, help="要预检的 URL（列表页/详情页均可）")
    dp2.add_argument("--var", action="append", default=[], help="覆盖变量 k=v")
    dp2.add_argument("--limit", type=int, default=3, help="最多打印 N 条 item（默认 3）")
    dp2.add_argument("--json", action="store_true", help="输出原始 JSON（供脚本消费）")

    sp = sub.add_parser("session", help="🔗 多步 API 链执行器：单会话+预算硬闸+节流+逐请求 JSONL 审计+挑战壳冷却+证据落盘（接口考古标配）")
    sp.add_argument("--plan", required=True, help="计划 JSON（具体化步骤列表：name/method/url/headers/params/json/save/expect_json）")
    sp.add_argument("--out", required=True, help="证据目录（响应落盘 + requests_<name>.jsonl 审计）")
    sp.add_argument("--max-requests", type=int, default=None, help="请求预算硬闸（默认取计划内 max_requests）")
    sp.add_argument("--min-gap", type=float, default=None, help="最小请求间隔秒（默认取计划内 min_gap，兜底 2.5）")
    sp.add_argument("--timeout", type=float, default=30.0, help="单请求超时秒")
    sp.add_argument("--risk-accepted", action="store_true",
                    help="确认总量风险（步数超阈值时的显式放行；政府/司法站阈值 500、默认 2000）")

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
    fp.add_argument("--raw", action="store_true",
                    help="输出原始 HTML 字节（不 markdown 化）——反爬取证用：看 @font-face、"
                         "background-position、加密脚本等原始结构。与 --json 互斥")
    fp.add_argument("--out", default=None, help="输出文件路径（默认 outputs/fetch_*.md|json）")
    fp.add_argument("--proxy", default=None, help="代理，如 http://127.0.0.1:7890")
    fp.add_argument("--actions", default=None,
                    help="动作链 JSON。支持 type: click/wait/wait_for_url/fill/press/hover/"
                         "select/scroll/scroll_bottom/mouse_move/drag/click_point/evaluate/screenshot。"
                         '示例 [{"type":"click","selector":"#more"},{"type":"fill","selector":"#q","value":"abc"}]；'
                         "选择器支持 :contains(text)（自动转 Playwright 的 :has-text）")
    fp.add_argument("--wait", default=None, help="等待选择器出现（浏览器模式）")
    fp.add_argument("--links", action="store_true", help="同时提取页面所有外链（Firecrawl map 风格）")
    fp.add_argument("--screenshot", default=None, help="浏览器截全页图保存路径（Firecrawl screenshot 风格）")
    fp.add_argument("--links-allow", default=None, help="只保留匹配该正则的外链")
    fp.add_argument("--links-deny", default=None, help="排除匹配该正则的外链")
    fp.add_argument("--cdp", default=None, help="附加调试 Chrome（如 http://127.0.0.1:9222），侦察与正式跑同通道")
    fp.add_argument("--capture", nargs="?", const="outputs/fetch_capture.json", default=None,
                    help="捕获页面全部 XHR/fetch JSON 响应并保存（自动启用浏览器模式；"
                         "裸用=outputs/fetch_capture.json，也可给保存路径）——SPA 接口侦察一步到位")

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
    mcp_p.add_argument("--token", default=None,
                       help="可选：启用到具级令牌校验（等同设 US_MCP_TOKEN；"
                            "stdio 会话被转发/共享时用，客户端须带 params._token）")

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

    # R101 沉淀：B站精配（影视飓风 UP主战役四通道战法代码化）
    bp = sub.add_parser("bili", help="📺 B站精配：视频元数据+弹幕+评论三通道（R34 战法，含 wbi 签名）")
    bp.add_argument("--video", default="", help="单个视频 BV 号（例 BV1abc，元数据+弹幕+评论全跑）")
    bp.add_argument("--mid", default="", help="UP主 UID：拉取投稿列表（需 wbi；-352 时按提示补 dm_img_*）")
    bp.add_argument("--year", type=int, default=0, help="按发布年份过滤投稿（配 --mid，例 2024）")
    bp.add_argument("--max-videos", type=int, default=30, help="--mid 模式最多处理的视频数（默认 30）")
    bp.add_argument("--limit-danmaku", type=int, default=5000, help="每个视频弹幕上限（默认 5000）")
    bp.add_argument("--limit-comments", type=int, default=500, help="每个视频评论上限（默认 500，含楼中楼）")
    bp.add_argument("--no-replies", action="store_true", help="不抓楼中楼回复")
    bp.add_argument("--dm-img-str", default="", help="真实行为指纹（-352 时用浏览器 capture 目标页取原值重放）")
    bp.add_argument("--dm-cover-img-str", default="", help="同上（capture_all.json 里的 dm_cover_img_str）")
    bp.add_argument("--dm-img-inter", default="", help="同上（capture_all.json 里的 dm_img_inter）")
    bp.add_argument("--out", default="", help="输出目录（默认 outputs/bili_<BV或UP主>）")

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
    ck_p.add_argument("--list", action="store_true",
                      help="列出已存档会话与健康状态（有效/过期条数、登录态、存档年龄）")
    ck_p.add_argument("--clear", action="store_true",
                      help="删除 --domain 指定域的会话存档（与 --domain 一起用）")

    dp_p = sub.add_parser("dianping", help="🌶️ 大众点评专用：Cookie 直抓搜索页列表（绕开验证码/csec）")
    dp_p.add_argument("--keyword", required=True, help="关键词，如 美食 / 烤肉")
    dp_p.add_argument("--city", type=int, default=2, help="城市 ID（默认 2=北京，上海=1）")
    dp_p.add_argument("--cookie", default="", help="已登录大众点评的浏览器 Cookie 整串")
    dp_p.add_argument("--cookie-file", default="", help="或从文件读取 Cookie")
    dp_p.add_argument("--limit", type=int, default=10, help="抓前 N 家（默认 10）")
    dp_p.add_argument("--proxy", default="", help="可选：住宅代理 http://user:pass@host:port")
    dp_p.add_argument("--out", default="", help="导出文件名前缀")

    sub.add_parser("ip", help="🌐 查看当前出口 IP 与运营商（换网络后确认）")

    diag_p = sub.add_parser("diagnose", help="🧬 阻断判型：实测一次请求，机器识别 瑞数/Cloudflare/WAF/JS壳/SPA 并给出处方（NMPA 战训：执行命令比读判型表可靠）")
    diag_p.add_argument("url", nargs="?", default="", help="要判型的 URL（被 412/403 拦的页面地址）；--history 时可省略")
    diag_p.add_argument("--json", action="store_true", help="输出原始 JSON（供脚本/子代理消费）")
    diag_p.add_argument("--proxy", default="", help="可选：通过指定代理判型 http://host:port")
    diag_p.add_argument("--quick", action="store_true",
                        help="🪶 轻量探针：HEAD 优先 + 8s 短超时 + 不下载全文——代理循环/批量预检用；"
                             "JS 挑战类判型不完整，疑阻断时用完整模式复核")
    diag_p.add_argument("--history", nargs="?", const="", default=None, metavar="URL关键词",
                        help="📒 查诊断台账（可带 URL 关键词过滤，省略=全部最近记录），不发起诊断")
    diag_p.add_argument("--out", default="", metavar="路径",
                        help="把原始诊断 JSON 另存到指定文件（与 --json 同内容；批量/留证用）")

    cdp_p = sub.add_parser("cdp", help="🔗 调试 Chrome (9222) 辅助：列标签页 / 查登录态 / 导 Cookie")
    cap_p = sub.add_parser("captcha", help="🧩 验证码人机协同：CDP 附加真 Chrome + 文件协议"
                                           "（文字点选 OCR 自动点 / 图标码转人工在环，gsxt 战训标配）")
    cap_p.add_argument("--dir", required=True,
                       help="会话工作目录（boot.json/cmd.json/last_result.json/status.json 交接面）")
    cap_p.add_argument("--start", action="store_true", help="启动验证码桥（默认 CDP 附加 9222 调试 Chrome）")
    cap_p.add_argument("--stop", action="store_true", help="停止验证码桥（触摸 stop 文件）")
    cap_p.add_argument("--status", action="store_true", help="查看桥状态（JSON）")
    cap_p.add_argument("--url", default="", help="启动/解题前导航到该 URL")
    cap_p.add_argument("--own", action="store_true", help="CDP 不可用时改启动自带 headful 浏览器（窗口可见仍可人工点码）")
    cap_p.add_argument("--cdp", type=int, default=9222, help="调试 Chrome 端口（默认 9222）")
    cap_p.add_argument("--solve", action="store_true",
                       help="进入解题循环：截图→文字点选 OCR 自动点→等通过；图标码/未配题面自动转人工")
    cap_p.add_argument("--prompt", default="", help="文字点选题面，如 '依次点击：国 家 税'（--solve 必需）")
    cap_p.add_argument("--captcha-selector", default="",
                       help="验证码主图选择器（OCR 截该元素；不给则整页截图）")
    cap_p.add_argument("--wait-selector", default="",
                       help="验证码通过后页面出现的特征选择器（通过判定，强烈建议给）")
    cap_p.add_argument("--max-wait", type=float, default=240.0, help="解题/人工等待上限秒（默认 240；GT4 弹窗约 90s 存活，留足余量）")
    cap_p.add_argument("--manual", action="store_true",
                       help="人工在环：打印提示后轮询等待——用户直接在调试 Chrome 窗口里点码（默认行为之一）")
    cap_p.add_argument("--out-html", default="", help="通过后落盘渲染 HTML 的路径")
    cap_p.add_argument("--out-cookies", default="", help="通过后落盘 cookies JSON 的路径")
    cdp_p.add_argument("--list-tabs", action="store_true", help="列出所有打开的标签页（默认）")
    cdp_p.add_argument("--login-state", default="", help="查某域名的 Cookie 数量与名称，如 taobao.com")
    cdp_p.add_argument("--out", default="", help="把该域名的 Cookie 串写入文件（配合 --login-state）")

    shl_p = sub.add_parser("shell", help="🐚 交互式调试：抓取 URL 后进入 Python REPL 探索 response"
                                          "（对标 Scrapy shell——试选择器/正则/JSON 路径再也不用猜）")
    shl_p.add_argument("url", help="要抓取并探索的 URL")
    shl_p.add_argument("--browser", action="store_true", help="走浏览器桥（JS 渲染 / SPA）")
    shl_p.add_argument("--cookie", default="", help="Cookie 串")
    shl_p.add_argument("--proxy", default="", help="代理")
    shl_p.add_argument("--cdp", default="", help="CDP 附加调试 Chrome")
    shl_p.add_argument("--timeout", type=float, default=60, help="请求超时秒")
    shl_p.add_argument("--actions", default="", help="JSON 格式的动作链（如 '[{\"type\":\"click\",\"selector\":\"#more\"}]'）")
    shl_p.add_argument("--js", default="", help="页面加载前注入的 JS 代码")
    shl_p.add_argument("--wait-selector", default="", help="等待选择器出现")

    jr_p = sub.add_parser("jsrecon", help="🔍 接口侦察：下载页面 JS 包自动提取候选 API 端点（SPA 先于 capture 使用）")
    jr_p.add_argument("url", help="目标页面 URL")
    jr_p.add_argument("--max-scripts", type=int, default=6, help="最多分析的 JS 包数（默认 6）")
    jr_p.add_argument("--out", default=None, help="结果 JSON 保存路径（目录自动补 jsrecon.json）")

    mp_p = sub.add_parser("map", help="🗺️ 全站 URL 快速发现（Firecrawl /map 风格）：入口链接 + sitemap.xml 并集，秒级出规划清单")
    mp_p.add_argument("url", help="入口 URL")
    mp_p.add_argument("--max-urls", type=int, default=500, help="最多返回的 URL 数（默认 500）")
    mp_p.add_argument("--sitemap", action="store_true", default=True, help="并集 sitemap.xml（默认开）")
    mp_p.add_argument("--allow", default=None, help="路径正则白名单（锚定 ^/page/\\d+/$）")
    mp_p.add_argument("--deny", default=None, help="路径正则黑名单")

    xhs_p = sub.add_parser("xhs", help="📕 小红书一等公民采集（实战反馈五收编）：话题搜索 Top-N 笔记 + 评论 + 作者主页；需调试窗口人工登录")
    xhs_p.add_argument("--keyword", required=True, help="搜索关键词（如 \"#大模型\"）")
    xhs_p.add_argument("--top", type=int, default=20, help="按点赞取前 N 篇（默认 20）")
    xhs_p.add_argument("--comments", type=int, default=50, help="每篇采集的顶层评论数（默认 50）")
    xhs_p.add_argument("--proxy", default="", help="库层代理 http://127.0.0.1:7897（300012 IP 封锁时必须）")
    xhs_p.add_argument("--out", default="outputs/xhs", help="输出目录（默认 outputs/xhs）")
    xhs_p.add_argument("--wait", type=int, default=600, help="等待人工登录+搜索的超时秒数（默认 600）")

    cad_p = sub.add_parser("capture-daemon", help="📡 常驻捕获守护：patchright 持久上下文 + 指定域 XHR 持续落 JSONL（签名型站点通用）")
    cad_p.add_argument("action", choices=["start", "stop", "status"], help="守护动作")
    cad_p.add_argument("--hosts", default="", help="捕获域白名单（逗号分隔，如 a.com,b.com；start 必填）")
    cad_p.add_argument("--capture", default="outputs/capture_daemon.jsonl", help="捕获 JSONL 路径")
    cad_p.add_argument("--profile", default="", help="持久 profile 目录（默认 ~/.universal-scraper/daemon_profile）")
    cad_p.add_argument("--proxy", default="", help="库层代理 URL")

    pf_p = sub.add_parser("pagefn", help="🧩 枚举页面非原生全局函数（加密/签名入口清单——传统 jQuery 页面打法，需调试 Chrome）")
    pf_p.add_argument("url", help="目标页面 URL（须已过挑战/登录，调试 Chrome 里能看到数据）")
    pf_p.add_argument("--limit", type=int, default=60, help="最多返回的函数数（默认 60，实现长的排前）")

    rd_p = sub.add_parser("rangedl", help="⬇️ 慢站大文件 Range 并行下载：分段并行 + 断点续传（实战反馈四#4）")
    rd_p.add_argument("url", help="文件 URL（服务器需支持 Accept-Ranges）")
    rd_p.add_argument("--out", required=True, help="输出文件路径")
    rd_p.add_argument("--segments", type=int, default=8, help="Range 分段数（默认 8）")
    rd_p.add_argument("--concurrency", type=int, default=3, help="并发下载数（默认 3）")

    bt_p = sub.add_parser("batch", help="🗂️ 批量任务队列：next/claim/touch/done/fail/nodata/retry/status（多代理+断点续跑）")
    bt_p.add_argument("--queue", required=True, help="队列 JSON 文件（[{id,text,status,attempts,result,priority?}]）")
    bt_p.add_argument("action", choices=["next", "claim", "touch", "done", "fail", "blocked", "nodata", "retry", "status"],
                      help="队列操作（claim=多代理原子领取；touch=长任务续租 running 心跳防误回收；nodata=数据不存在于公开渠道，区别于爬取失败）")
    bt_p.add_argument("item_id", nargs="?", default=None, help="任务 id（done/fail/blocked/nodata/retry 时必填）")
    bt_p.add_argument("--result", default="", help="核对结论/失败原因（写入台账）")

    bd_p = sub.add_parser("budget", help="🚧 域名礼貌预算/封锁台账：mark/check/list（跨运行持久）")
    bd_p.add_argument("--mark", default=None, help="登记封锁事件：域名")
    bd_p.add_argument("--hours", type=float, default=24.0, help="冷却小时数（默认 24）")
    bd_p.add_argument("--note", default="", help="备注（现象/处置）")
    bd_p.add_argument("--check", default=None,
                      help="查询某域名是否冷却中（退出码：0=可访问，2=冷却中）")
    bd_p.add_argument("--list", action="store_true", help="列出全部台账")
    bd_p.add_argument("--usage", action="store_true",
                      help="查看本进程任务请求额度（--max-requests 计数：已用/上限/剩余）")
    bd_p.add_argument("--probe", default=None,
                      help="放行窗口探针：URL（每 --every 秒 GET 一次，判 OPEN/BLOCKED/EMPTY_200；"
                           "gsxt 战训：200 空页=软封锁，200≠放行）")
    bd_p.add_argument("--expect", default="",
                      help="探针放行标记（站名/页面特有文案）；命中即 OPEN。不给标记则 200+正文≥4KB 判放行")
    bd_p.add_argument("--every", type=float, default=60.0, help="探针间隔秒（默认 60）")
    bd_p.add_argument("--max-rounds", type=int, default=30,
                      help="探针最大轮数（默认 30=每60s间隔约30分钟；0=不限，慎用——会卡死任务）")
    bd_p.add_argument("--timeout", type=float, default=15.0, help="探针单次请求超时秒")
    bd_p.add_argument("--action", default=None,
                      help="稀缺动作预算扣额：动作名（如 gsxt-search）。发起即扣额、失败不退"
                           "（gsxt 战训：失败搜索同样烧窗口）；退出码 0=允许 2=超额")
    bd_p.add_argument("--action-limit", type=int, default=5, help="动作预算：窗口内限额（默认 5）")
    bd_p.add_argument("--action-window", type=float, default=3600.0, help="动作预算：滑动窗口秒（默认 3600）")
    bd_p.add_argument("--action-cost", type=int, default=1, help="动作预算：本次扣额数（默认 1）")
    bd_p.add_argument("--action-info", default=None, help="查动作预算状态（不扣额）")
    bd_p.add_argument("--file", default=None, help="台账文件路径（默认 ~/.universal_scraper/domain_budget.json）")

    c2p = sub.add_parser("capture2config", help="⚡ 捕获→可重放 http_json 配置（POST体/方法/翻页模板一步到位）")
    c2p.add_argument("capture", help="捕获文件：capture_all.json 或声明式 <name>.json")
    c2p.add_argument("--referer", default="", help="原页面 URL（写进配置的 Referer 头）")
    c2p.add_argument("--out", default="", help="输出 JSON（默认 <捕获文件>_configs.json）")
    c2p.add_argument("--watch", action="store_true",
                     help="👀 监听捕获文件变化，每次落盘稳定后自动重新生成配置草案（Ctrl+C 退出）")

    cu_p = sub.add_parser("curl2config", help="🥟 curl 命令 → 任务配置草案（R20；只解析不执行，"
                                              "凭据/不支持项明确提醒）")
    # 注意 dest：不能用 args.cmd（那是子命令名本身，会被覆盖）
    cu_p.add_argument("--cmd", dest="curl_cmd", default=None,
                      help="curl 命令字符串（缺省从 stdin 读）")
    cu_p.add_argument("--name", default="from_curl", help="任务名（默认 from_curl）")
    cu_p.add_argument("--html", action="store_true",
                      help="生成 http_html 模板（row_css/fields 待补）而非 http_json")
    cu_p.add_argument("--out", default="", help="输出配置路径（默认打印到 stdout）")

    pdf_p = sub.add_parser("pdf", help="📎 附件批量下载 + 表格型 PDF 结构化（pdfplumber/pypdf）")
    pdf_p.add_argument("--download", default=None, help="下载清单 JSON（[{url,name}] 或 [url]）")
    pdf_p.add_argument("--tables", default=None, help="提取某 PDF 的表格 → JSON")
    pdf_p.add_argument("--out", default="", help="输出目录/文件")
    pdf_p.add_argument("--interval", type=float, default=1.0, help="下载间隔秒（礼貌限速）")

    gd_p = sub.add_parser("guide", help="📖 生成并行子代理执行规范 AGENT_GUIDE.md（batch2400 战训标准件）")
    gd_p.add_argument("--out", default="AGENT_GUIDE.md", help="输出路径（默认 ./AGENT_GUIDE.md）")

    rs_p = sub.add_parser("research", help="🔬 科研批量采集（五任务实战动线引擎化）：清单×年份窗×词典 → 面板+文本档案；断点续跑/防封禁自熔断/扫描件 OCR 兜底")
    rs_sub = rs_p.add_subparsers(dest="research_cmd", required=True)
    r1 = rs_sub.add_parser("run", help="批量采集（断点续跑/自熔断/文本档案/OCR 兜底）")
    r1.add_argument("--universe", required=True, help="企业清单 CSV（stkcd[,first_year,last_year]）")
    r1.add_argument("--out", required=True, help="输出目录（progress/texts/rows.jsonl）")
    r1.add_argument("--keywords", help="关键词词典 JSON {组:[词]}（缺省内置 合规/出海/收缩）")
    r1.add_argument("--year-from", type=int, default=2010)
    r1.add_argument("--year-to", type=int, default=2024)
    r1.add_argument("--fuse", type=int, default=5, help="连续 API 异常熔断阈值")
    r1.add_argument("--panel-xlsx", help="采集完成后立即生成面板 xlsx（可选）")
    r2 = rs_sub.add_parser("panel", help="rows.jsonl → 面板 xlsx（含清单外/缺口/说明）")
    r2.add_argument("--out", required=True)
    r2.add_argument("--universe", required=True)
    r2.add_argument("--keywords")
    r2.add_argument("--year-from", type=int, default=2010)
    r2.add_argument("--year-to", type=int, default=2024)
    r2.add_argument("--xlsx", required=True)

    au_p = sub.add_parser("audit", help="🧪 交付审计电池（出口检查）：panel=面板全量电池 / urls=来源可达性 / verbatim=逐字回源")
    au_sub = au_p.add_subparsers(dest="audit_cmd", required=True)
    a1 = au_sub.add_parser("panel", help="面板 xlsx：唯一键/窗口/freq 公式/年度-标题/文本源覆盖/文档一致")
    a1.add_argument("--xlsx", required=True)
    a1.add_argument("--texts", help="文本档案目录（*.txt.gz）")
    a1.add_argument("--keywords", help="研究词典 JSON {组:[词]}——抽验升级为全字段关键词重算")
    a1.add_argument("--universe")
    a1.add_argument("--year-from", type=int, default=2010)
    a1.add_argument("--year-to", type=int, default=2024)
    a2 = au_sub.add_parser("urls", help="来源 URL 在线可达 + 根路径引用检测")
    a2.add_argument("--xlsx", required=True)
    a2.add_argument("--sheet")
    a2.add_argument("--url-col", default="source_url")
    a2.add_argument("--note-col")
    a3 = au_sub.add_parser("verbatim", help="JSON 条款库逐字回源 + 文档内唯一性")
    a3.add_argument("--json", required=True)
    a3.add_argument("--sources", required=True, help="doc_id=路径 的 JSON 映射")
    a3.add_argument("--no-unique", action="store_true")

    vp = sub.add_parser("verify", help="🧾 复核抓取结果：字段完整率/去重/抽样重抓对比；或通用目录审计")
    vp.add_argument("--file", default=None, help="结果 JSON 文件，如 outputs/xxx.json")
    vp.add_argument("--dir", default=None, help="任意任务目录审计（不要求由本工具产出）：记录数/字段完整率/证据存在性")
    vp.add_argument("--network", action="store_true", help="联网抽样重抓对比（默认只做本地检查）")
    vp.add_argument("--data-key", default="", help="JSON 为 dict 包装时取数组的键；未指定则自动探测 data/list/rows/items")
    vp.add_argument("--expect", default="",
                    help="语义校验（--file，JSON/CSV 自动识别）：'词'=任一命中(OR)，'+词'=必须含(AND)，'!词'=不得出现；"
                         "列级断言：'列名!词'/'列名+词'=该列内约束（精准，防全文误杀）；"
                         "多指标任务用 + 防丢一半，如 \"+消费价格,+出厂价格,!预测\"")
    vp.add_argument("--require", default="",
                    help="必需字段清单（逗号分隔）：点名这些列完整率必须 ≥90%%，否则整体 FAIL——"
                         "UI 驱动采集产物的显式验收契约，如 \"案号,裁判日期\"")
    vp.add_argument("--expect-count", type=int, default=None,
                    help="对账型检查（实战反馈六）：源站声明的总数（如 common_counts/"
                         "回答总数）——实采 < 声明则 FAIL 并给出缺口率（防\"采到一半以为完了\"）")
    vp.add_argument("--expect-empty", default="",
                    help="声明'源站不提供/本就应为空'的字段（逗号分隔，--file 用）——"
                         "全空不判死列、不计抽取缺口，报告如实标注。网易云战训："
                         "IP 属地/回复数这类字段 0%% 是源站特性非漏抓，如 \"IP属地,回复数\"")

    args = ap.parse_args()

    if args.cmd == "dryparse":
        # 单 URL 过完整 fetch→route→parse 链，字段级打印（Scrapy parse 心智）：
        # 小白 90% 的失败是选择器/字段映射写错，预检把发现周期从"烧一轮请求"
        # 缩到 3 秒。不写盘，只读。
        import json as _json
        from pathlib import Path as _P
        from .task import Task
        from .protocols import Request, ParseContext
        from .engine_v3 import resolve_tpl, _match_rule
        from .antibot import detect_block as _db
        from .core import set_request_budget, request_budget as _rq
        _fetcher = None
        try:
            task = Task(_P(args.task))
            cfg = task.config
            # 审查十轮（M）：显式 null 曾穿透 dict(None) TypeError（报错不指向键）
            vars_ = dict(cfg.get("vars") or {})
            for kv in (args.var or []):
                _k, _, _v = kv.partition("=")
                if _k:
                    vars_[_k] = _v
            anti = dict(cfg.get("anti_bot") or {})
            anti.setdefault("min_interval", 0.05)
            set_request_budget(50)  # 预检硬闸：绝不烧成全量抓取
            src = resolve_tpl(dict(cfg.get("source", {})), vars_)
            src["_task_dir"] = str(task.root)
            fetcher = task.get_fetcher_cls()(src, vars_, anti)
            _fetcher = fetcher
            if not hasattr(fetcher, "fetch"):
                print("❌ bridge/一次性取数器没有单 URL fetch——请用 `session` 或 `run` 跑",
                      file=sys.stderr)
                return 1
            pname = "default"
            for rule in (cfg.get("rules") or []):
                if _match_rule(rule, args.url):
                    pname = rule.get("parser", "default")
                    break
            pmap = task.get_parsers()
            pcls = pmap.get(pname) or pmap.get("default")
            if pcls is None:
                print(f"❌ 任务包没有可用 parser（routed={pname}）", file=sys.stderr)
                return 1
            parser = pcls((cfg.get("parsers") or {}).get(pname, {}), vars_)
            resp = fetcher.fetch(Request(url=args.url, depth=0))
            ctx = ParseContext(task, cfg, vars_)
            result = parser.parse(resp, ctx)
            _META = ("_url", "_parser", "_ts", "_id")
            all_items = list(result.items or [])
            # 空壳过滤（AI 空壳问题）：全字段无值的 item 是选择器没匹配上的
            # 副产物——预检必须说"0 有效条目"，不能拿空壳凑数
            items = [it for it in all_items
                     if any(str(v or "").strip() for k, v in it.items() if k not in _META)]
            _bd = _db(int(getattr(resp, "status", 0) or 0),
                      (getattr(resp, "text", "") or "")[:20000], {}, args.url)
            payload = {
                "url": args.url, "routed_parser": pname,
                "status": int(getattr(resp, "status", 0) or 0),
                "text_bytes": len(getattr(resp, "text", "") or ""),
                "items": len(items), "items_raw": len(all_items),
                "new_requests": len(result.requests or []),
                "block_kind": _bd.get("kind"), "budget": _rq(),
                "sample": items[:max(1, args.limit)],
            }
            if args.json:
                print(_json.dumps(payload, ensure_ascii=False, indent=1, default=str))
            else:
                print(f"🧪 dryparse: {args.url}")
                print(f"   路由解析器: {pname} | HTTP {payload['status']} | "
                      f"正文 {payload['text_bytes']:,}B | 有效条目 {len(items)}"
                      + (f"（另有 {len(all_items) - len(items)} 条全空壳）" if len(all_items) != len(items) else "")
                      + f" | 跟进链接 {payload['new_requests']} | 预算 {payload['budget']['used']}")
                if _bd.get("kind") not in ("none",):
                    print(f"   ⚠️ 页面命中拦截指纹 [{_bd['kind']}] {_bd.get('detail', '')}"
                          f"——先跑 `cli diagnose {args.url}` 判型")
                if not items:
                    _shell_note = (f"，而原始解析出了 {len(all_items)} 条全空壳 item——"
                                   "rows_css/records_path 多半没匹配上本页" ) if all_items else ""
                    print(f"   ❌ 0 有效条目{_shell_note}。排查顺序: ① rows_css/records_path "
                          "是否匹配本页（用 `fetch <url>` 看渲染后结构）② 需要渲染的站改 "
                          "source.type=browser ③ 数据在接口里走 jsrecon→capture2config")
                for i, it in enumerate(items[:max(1, args.limit)], 1):
                    print(f"   ── item {i} ──")
                    for k, v in it.items():
                        _sv = "" if v is None else str(v)
                        mark = "⚠️ 空" if not _sv.strip() else (f"{_sv[:80]}…" if len(_sv) > 80 else _sv)
                        print(f"      {k:<16} = {mark}")
            if _bd.get("kind") not in ("none",) and not items:
                return 2
            return 0 if items else 3
        except Exception as e:
            print(f"❌ dryparse 失败: {type(e).__name__}: {e}", file=sys.stderr)
            return 2
        finally:
            # 异常路径也必须关取数器（审查 P2）：BrowserFetcher 池残留会占
            # profile 锁害死下一个任务
            if _fetcher is not None and hasattr(_fetcher, "close"):
                try:
                    _fetcher.close()
                except Exception:
                    pass

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
        # 审查十轮（M）：已存在任务包时 task_bundle 有意抛 FileExistsError
        # （拒绝覆盖）——CLI 不接曾裸栈，本该是一句话报错
        try:
            out = scaffold_task(args.name, Path(args.out))
        except FileExistsError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2
        print(f"✅ 任务包已生成: {out}")
        print("   下一步: 改 config.json 的 start_urls/rules/parsers，或写 modules/parser.py")
        print(f"   运行:   python3 -m universal_scraper.cli run --task {out}")
        return 0

    if args.cmd == "scaffold":
        tpl = json.loads(json.dumps(SCAFFOLD_TEMPLATE[args.type]))
        tpl["name"] = args.name
        out = Path(args.out)
        # 审查八轮（LOW）：--out 指向已存在目录曾甩裸 IsADirectoryError（traceback）
        if out.is_dir():
            print(f"❌ --out 是目录（需要文件路径）: {out}\n   例如: --out {out}/{args.name}.json",
                  file=sys.stderr)
            return 2
        # 审查十轮（M）：与 --type task 的"拒绝覆盖"口径对齐——普通模板曾对
        # 已存在文件无确认直接覆盖（用户编辑过的配置被静默换掉）
        if out.exists():
            print(f"❌ 目标文件已存在（拒绝覆盖）: {out}\n   如需重新生成请先删除，或换 --out 路径",
                  file=sys.stderr)
            return 2
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
        failed = []
        for i, job in enumerate(jobs, 1):
            try:
                if not isinstance(job, dict) or not job.get("task"):
                    raise ValueError("编排条目缺少 task 字段（应为 {\"task\": ...}）")
                tp = Path(job["task"])
                print(f"[jobs] {i}/{len(jobs)} {tp}", flush=True)
                r = run_task(tp, overrides=job.get("var"), limit=job.get("limit"),
                             resume=bool(job.get("resume")))
            except Exception as e:
                print(f"[jobs] {i} 失败跳过: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
                failed.append({"i": i, "task": (job.get("task") if isinstance(job, dict) else None),
                               "error": f"{type(e).__name__}: {str(e)[:120]}"})
                continue
            total += r.get("total", 0)
        # 审查修复（P1）：全部失败也 exit 0 曾让调度方误判成功
        print(json.dumps({"jobs": len(jobs), "failed": len(failed),
                          "total_items": total}, ensure_ascii=False))
        return 1 if failed else 0

    if args.cmd == "monitor":
        from .engine_v3 import run_task
        # 审查修复（P1，R77）：局部 import time 曾遮蔽模块级导入——本分支内
        # 的 blocked.json 写盘调用 time.strftime 必抛 UnboundLocalError，
        # exit 5 时证据文件永远写不出来
        key_field = args.key
        # 审查十轮（L）：逗号后带空格（"title, price"）曾不 strip——" price" 永远
        # 取不到值，该字段变更静默漏报
        diff_fields = [f.strip() for f in args.diff_fields.split(",") if f.strip()] \
            if args.diff_fields else None
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
        _failed_rounds = 0
        while args.times == 0 or run_no < args.times:
            run_no += 1
            print(f"[monitor] 第 {run_no} 次抓取 {tp}", flush=True)
            try:
                run_task(tp)
                # 读取任务输出
                cfg = json.loads((tp / "config.json").read_text(encoding="utf-8"))
                # R42b 修复：解析与引擎一致——output.dir 相对路径按 CWD 解析
                # （engine_v3:125 同款）；base_name 缺省用 config.name（引擎同款）
                _od = Path(str((cfg.get("output") or {}).get("dir", "") or "outputs"))
                out_json = _od / f"{(cfg.get('output') or {}).get('base_name', cfg.get('name', tp.name))}.json"
                rows = []
                if out_json.exists():
                    rows = json.loads(out_json.read_text(encoding="utf-8"))
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # 审查修复（P1）：监控循环曾因单轮异常整进程死亡——无人值守
                # 场景夜里一次桥超时就全瞎。失败轮跳过本轮 diff（快照不动，
                # 下轮重比），并明确告警而非假"无变化"
                _failed_rounds += 1
                print(f"[monitor] ⚠️ 本轮抓取失败（快照保持不变）: "
                      f"{type(e).__name__}: {str(e)[:140]}", file=sys.stderr, flush=True)
                if args.times == 0 or run_no < args.times:
                    time.sleep(max(1.0, args.every))  # 审查十轮（M）：0 曾热循环全速轰炸目标站
                continue
            # 审查十轮（H）：默认 key=url 对 v3 任务普遍失明——引擎导出行只有
            # _url（engine_v3 给每条 item setdefault("_url", ...)），url 字段仅
            # 在任务显式映射时存在 → 恒"无变化（共 0 条）"exit 0。默认口径下
            # 行里没有 url 时回退 _url
            _kf = key_field
            if _kf == "url" and rows and not any(r.get("url") for r in rows) \
                    and any(r.get("_url") for r in rows):
                _kf = "_url"
            snap = {str(r.get(_kf)): r for r in rows if r.get(_kf)}
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
            # R45 修复：快照写曾无 mkdir 且无兜底——output.dir≠outputs 的任务在
            # 干净 CWD 下第 1 轮就 FileNotFoundError exit 1，监控循环中止
            try:
                snap_path.parent.mkdir(parents=True, exist_ok=True)
                snap_path.write_text(json.dumps(snap, ensure_ascii=False, default=str), encoding="utf-8")
            except Exception as e:
                print(f"[monitor] ⚠️ 快照写盘失败（下轮将整体视为新增）: {e}", file=sys.stderr, flush=True)
            old_snap = snap
            if args.times == 0 or run_no < args.times:
                print(f"[monitor] 等待 {args.every}s...", flush=True)
                time.sleep(max(1.0, args.every))  # 审查十轮（M）：负数曾 ValueError 崩溃
        if _failed_rounds:
            print(f"[monitor] 结束：共 {_failed_rounds} 轮失败（见上方告警）", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "schedule":
        from .engine_v3 import run_task
        import time as _time
        run_no = 0
        _failed_rounds = 0
        while args.times == 0 or run_no < args.times:
            run_no += 1
            print(f"[schedule] 第 {run_no} 次运行 {args.task}", flush=True)
            try:
                run_task(Path(args.task), resume=args.resume)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # 审查修复（P1）：定时循环曾因单轮异常整进程死亡
                _failed_rounds += 1
                print(f"[schedule] ⚠️ 本轮运行失败: {type(e).__name__}: {str(e)[:140]}",
                      file=sys.stderr, flush=True)
            if args.times == 0 or run_no < args.times:
                print(f"[schedule] 等待 {args.every}s...", flush=True)
                _time.sleep(max(1.0, args.every))
        if _failed_rounds:
            print(f"[schedule] 结束：共 {_failed_rounds} 轮失败（见上方告警）", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "auto":
        from .auto import run_auto_cli
        out = run_auto_cli(" ".join(args.desc), limit=args.limit)
        if isinstance(out, dict) and out.get("error"):
            print(f"❌ {out['error']}", file=sys.stderr)
            return 1
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0

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
        if getattr(args, "token", None):
            # R20：可选令牌闸（不落日志；只设进程内环境变量）
            os.environ["US_MCP_TOKEN"] = str(args.token)
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
        from .sites import format_endpoints
        for s in list_sites():
            print(f"  {s['status']} {s['name']:<6} {s['domain']:<22} {s['desc']}  [{s['difficulty']}]")
            _eps = format_endpoints(s)
            if _eps:
                # R21：端点级登录要求（整站一个标签会把"评论免登录"这类关键事实盖掉）
                print(f"        └ {_eps}")
        return 0

    if args.cmd == "books":
        from .book_catalog import build_catalog
        # 审查十轮（M）：--out 指向已存在文件曾 FileExistsError 裸栈
        if not _guard_out_is_dir(getattr(args, "out", None), "books"):
            return 1
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
        # 审查十轮（L）：读曾不指定 encoding（locale 非 UTF-8 时 UnicodeDecodeError
        # 裸栈），写却是 utf-8——读写口径对齐
        _lines = _zs.read_text(encoding="utf-8", errors="replace").splitlines() \
            if _zs.exists() else []
        def _upsert(k, v):
            nonlocal _lines
            _lines = [ln for ln in _lines if not ln.startswith(f"export {k}=")]
            # 审查三轮（H）：repr() 是 Python 引号——值含 !/反斜杠时写进 zshenv
            # 会坏（! 触发历史扩展、\ 不还原）。shlex.quote 才是 POSIX 安全写法
            import shlex as _shx
            _lines.append(f"export {k}={_shx.quote(str(v))}")
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
            # 审查十轮（L）：--workers 0/负数曾 ThreadPoolExecutor ValueError 裸栈
            r = _pf_refresh(out=args.out, workers=max(1, args.workers),
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
        if not _guard_out_is_file(getattr(args, "out", ""), "report"):
            return 2
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
        import time as _time
        from pathlib import Path as _P
        from . import cookies as _ck
        if getattr(args, "list", False):
            rows = _ck.list_saved()
            if not rows:
                print("（无存档会话——在调试 Chrome 登录目标站后运行 cli cookies --from-cdp 导入）")
                return 0
            print(f"{'域':<32} {'条数':>4} {'有效':>4} {'过期':>4} 登录态 存档时间")
            for r in rows:
                if r.get("corrupt"):
                    print(f"{str(r.get('domain', '')):<32} {'—':>4} {'—':>4} {'—':>4}  —    ⚠️存档损坏，请重新导入")
                    continue
                _sa = r.get("saved_at")
                _age = f"{(_time.time() - _sa) / 86400:.1f}天前" if isinstance(_sa, (int, float)) else "?"
                _flag = " ⚠️已全部过期，请重新导入" if r.get("count") and not r.get("valid") else ""
                print(f"{str(r.get('domain', '')):<32} {r.get('count', 0):>4} {r.get('valid', 0):>4} "
                      f"{r.get('expired', 0):>4} {'✓' if r.get('has_login') else '✗':>4} {_age}{_flag}")
            return 0
        if getattr(args, "clear", False):
            _dom = (args.domain or "").strip()
            if not _dom:
                print("❌ --clear 需与 --domain 一起用（指定要删除哪个域的会话存档）", file=sys.stderr)
                return 1
            _ok = _ck.delete(_dom)
            if _ok:
                print(f"✅ 已删除: {_dom}")
                return 0
            if _ck.has_cookies(_dom):
                # delete 失败但存档还在（权限等）——delete 内部已打印 WARN
                print(f"❌ 删除失败: {_dom}（见上方 WARN）", file=sys.stderr)
                return 1
            print(f"ℹ️ 该域无存档: {_dom}")
            return 1
        if getattr(args, "from_cdp", False):
            # CDP 附加运行不落盘 storageState——直接从调试 Chrome 取全部 Cookie
            # （端口自动搜索：9222 被占时调试脚本可能自动换了端口）
            import os as _os
            import subprocess as _sp
            from .runtime import resolve_node, resolve_node_path
            _cdp_port = _ck.find_cdp_port(9222)
            if _cdp_port is None:
                print("❌ 未发现调试 Chrome（9222-9230 均未监听）——先跑 open-debug-chrome.sh 并登录目标站",
                      file=sys.stderr)
                return 1
            _js = (
                'const {chromium}=require("playwright");'
                f'(async()=>{{const b=await chromium.connectOverCDP("http://127.0.0.1:{_cdp_port}");'
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
            if not _guard_out_is_file(args.out, "cookies"):
                return 2
            # R100 修复（P2）：登录 Cookie 串曾 0644 落盘（R18 同类）
            import os as _os
            _fd = _os.open(args.out, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
            with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
                _f.write(out)
            _os.chmod(args.out, 0o600)
            print(f"✅ 已写入: {args.out}", file=sys.stderr)
        print(f"（{len(pairs)} 个 cookie，域过滤={dom or '全部'}）", file=sys.stderr)
        return 0

    if args.cmd == "cdp":
        import json as _json
        import os as _os
        import subprocess as _sp
        from .runtime import resolve_node, resolve_node_path
        from .cookies import find_cdp_port as _fcp
        _cdp_port = _fcp(9222)
        if _cdp_port is None:
            print("❌ 未发现调试 Chrome（9222-9230 均未监听）——先跑 open-debug-chrome.sh", file=sys.stderr)
            return 1
        js = (
            'const {chromium}=require("playwright");'
            f'(async()=>{{const b=await chromium.connectOverCDP("http://127.0.0.1:{_cdp_port}");'
            'const ctx=b.contexts()[0];if(!ctx){console.error("CDP 无浏览器上下文");process.exit(1);}'
        )
        if args.login_state:
            dom = args.login_state
            # 实战反馈五#6：count=0 曾语义歧义（未登录 vs 目标站没开标签页）——
            # 增列 tab_open 供 CLI 侧区分处方
            js += (f'const tabOpen=ctx.pages().some(p=>p.url().includes({json.dumps(dom)}));'
                   f'const cs=(await ctx.cookies()).filter(c=>String(c.domain).includes({json.dumps(dom)}));'
                   f'console.log(JSON.stringify({{"domain":{json.dumps(dom)},"count":cs.length,'
                   f'"tab_open":tabOpen,"names":cs.map(c=>c.name).slice(0,30)}}));')
            if args.out:
                # R100 修复（P2）：Node 侧写登录 Cookie 串曾默认 0644
                js += (f'const _fs=require("fs");'
                       f'const _p={json.dumps(args.out)};'
                       f'_fs.writeFileSync(_p, cs.map(c=>c.name+"="+c.value).join("; "), {{mode:0o600}});'
                       f'_fs.chmodSync(_p, 0o600);'
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
                    # 实战反馈五#6：count=0 时按 tab_open 区分处方（没开标签页 ≠ 未登录）
                    if data.get("count") == 0 and data.get("tab_open") is not None:
                        _rx = ("调试 Chrome 没有打开该域的标签页——先在窗口里打开目标站"
                               if not data.get("tab_open") else
                               "该域下无任何 Cookie——大概率未登录，请在窗口登录后重查")
                        print(f"count: 0\n说明: {_rx}")
                        return 0 if not data.get("tab_open") else 1
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

    if args.cmd == "captcha":
        # 验证码人机协同（gsxt 战训内置化）：桥进程归 CLI 管，协议归 CaptchaSession
        import json as _json
        import os as _os
        import subprocess as _sp
        import time as _time
        from pathlib import Path as _P
        from .captcha_session import CaptchaSession, BRIDGE as _BRIDGE, SCRIPTS_DIR as _SD
        from .runtime import resolve_node as _rn
        from .antibot import detect_block as _db
        wdir = _P(args.dir).resolve()
        s = CaptchaSession(wdir)

        def _emit(o):
            print(_json.dumps(o, ensure_ascii=False))

        def _solve_flow():
            if not s.is_running():
                print("❌ 验证码桥未运行——先加 --start 启动（或单独跑 cli captcha --start --dir）",
                      file=sys.stderr)
                return 1
            if args.url:
                s.goto(args.url, wait_ms=2000)
            if not args.solve:
                # 不解题 = 纯人工在环：提示用户在窗口里操作，轮询通过特征
                print("🙋 人工在环：请在调试 Chrome 窗口内完成验证码/登录等人工操作"
                      f"（等待上限 {args.max_wait:.0f}s）…", file=sys.stderr)
            if args.solve and args.prompt:
                try:
                    __import__("ddddocr")
                    _can_ocr = True
                except Exception:
                    print("⚠️ 未安装 ddddocr（pip install ddddocr）——文字点选自动解题不可用，"
                          "转人工在环", file=sys.stderr)
                    _can_ocr = False
            else:
                _can_ocr = False
            from .captcha_ocr import solve_text_clicks
            deadline = _time.time() + args.max_wait
            last_html_len = -1
            last_hint = 0.0
            _last_unmatched: tuple = ()
            passed = False
            _bridge_error = ""
            while _time.time() < deadline:
                # 通过判定 1：wait-selector 出现
                try:
                    if args.wait_selector and s.wait(args.wait_selector, timeout_ms=2000):
                        passed = True
                        break
                    # 尝试 OCR 自动点选（每轮先截验证码主图——元素截图，防题面误点）
                    if _can_ocr and args.captcha_selector:
                        try:
                            png = s.shot("captcha.png", selector=args.captcha_selector)
                            img = _P(png).read_bytes()
                            sol = solve_text_clicks(img, args.prompt)
                            pts = [p for p in sol.get("points") if p]
                            if pts and not sol.get("unmatched"):
                                s.click_xy(pts, delay_ms=600)
                                print(f"🤖 OCR 已点选 {len(pts)} 点（{sol['matched']}）",
                                      file=sys.stderr)
                                _last_unmatched = ()
                            elif tuple(sol.get("unmatched") or ()) != _last_unmatched:
                                _last_unmatched = tuple(sol.get("unmatched") or ())
                                print(f"⚠️ OCR 未完整匹配（命中 {sol.get('matched')} / "
                                      f"未命中 {list(_last_unmatched)}）——本轮不点击，"
                                      f"重试或转人工", file=sys.stderr)
                        except RuntimeError:
                            raise   # 桥死亡（RuntimeError 契约）→ 交外层终止解题
                        except Exception as e:
                            print(f"⚠️ OCR 解题轮失败（转重试/人工）: {e}", file=sys.stderr)
                            _can_ocr = False
                    # 通过判定 2：无 wait-selector 时用"HTML 长度跳增 + 无拦截指纹
                    # + 验证码图已消失"（审查 P2：只看增长会把 SPA 渲染误判为通过）
                    if not args.wait_selector:
                        h = s.html()
                        bd = _db(200, h, {}, args.url or "")
                        _cap_gone = True
                        if args.captcha_selector:
                            _cap_gone = not s.run_js(
                                "!!document.querySelector(%s)" % _json.dumps(args.captcha_selector))
                        if len(h) > 5000 and bd["kind"] == "none" and _cap_gone and \
                                last_html_len >= 0 and len(h) - last_html_len >= 2000:
                            passed = True
                            break
                        last_html_len = len(h)
                except RuntimeError as e:
                    # 审查修复（P2）：桥死亡（Chrome 被关/崩溃）曾裸栈崩溃——
                    # 自动化拿不到结构化失败。转成 passed=false + exit 3 契约
                    _bridge_error = f"{type(e).__name__}: {str(e)[:160]}"
                    print(f"❌ 验证码桥异常（停止解题）: {_bridge_error}", file=sys.stderr)
                    break
                # 人工在环提示（每 30s 一次）
                if _time.time() - last_hint > 30:
                    last_hint = _time.time()
                    print(f"⏳ 等待验证码通过（剩 {deadline - _time.time():.0f}s）——"
                          f"可在调试 Chrome 窗口内人工点选", file=sys.stderr)
                _time.sleep(3)
            result = {"passed": passed, "dir": str(wdir)}
            try:
                result["url"] = s.status().get("url", "")
            except Exception:
                result["url"] = ""
            if _bridge_error:
                result["bridge_error"] = _bridge_error
            try:
                html = s.html()
                if args.out_html:
                    _P(args.out_html).write_text(html, encoding="utf-8")
                    result["out_html"] = args.out_html
                if args.out_cookies:
                    # R99 修复（P2）：登录态 Cookie 全文曾 0644 落盘
                    import os as _os
                    _fd = _os.open(args.out_cookies,
                                   _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
                    with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
                        _f.write(_json.dumps(s.cookies(), ensure_ascii=False))
                    _os.chmod(args.out_cookies, 0o600)  # R18b 同款：治愈遗留 0644
                    result["out_cookies"] = args.out_cookies
                result["html_bytes"] = len(html)
            except Exception as e:
                result["dump_error"] = str(e)
            _emit(result)
            return 0 if passed else 3  # 3=未通过（证据已落盘，与 exit 5 封禁语义区分）

        if args.status:
            _emit(s.status())
            return 0
        if args.stop:
            _sr = s.stop()
            # 审查八轮（M）：stop 现在等待桥确认退出——未确认时如实上报
            # （is_running 仍 True，立即 start 会被守卫拒绝，防双开桥）
            _emit({"stopped": True, "confirmed": bool(_sr.get("confirmed")), "dir": str(wdir)})
            return 0
        if args.start:
            # 双启动守卫（审查 P2）：同一 workdir 已有活桥时再 start 会双桥抢命令
            if s.is_running():
                _emit({"started": False, "error": f"该目录已有验证码桥在运行（pid={s.status().get('pid')}）",
                       "dir": str(wdir)})
                return 1
            boot = {"cdp": args.cdp, "own": bool(args.own)}
            if args.url:
                boot["url"] = args.url
            wdir.mkdir(parents=True, exist_ok=True)
            # 收官十轮：清理可能的残留 stop（上次 stop 时桥未在运行 → 桥没机会自删
            # 该文件，新桥首轮轮询即 break 永远起不来）
            try:
                (wdir / "stop").unlink(missing_ok=True)
            except Exception:
                pass
            (wdir / "boot.json").write_text(_json.dumps(boot), encoding="utf-8")
            env = {**_os.environ, "NODE_PATH": str(_SD.parent / "node_modules")}
            _spawn_after = time.time()      # 只认此刻之后的心跳（防残留 status.json）
            proc = _sp.Popen(
                [_rn(), str(_BRIDGE), "--dir", str(wdir)],
                cwd=str(_SD.parent), env=env,
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            try:
                st = s.wait_alive(30, require_after=_spawn_after)
            except Exception as e:
                # 孤儿进程防线（审查 P1）：启动超时必须击杀残留桥，否则留下
                # 无人认领的浏览器窗口（墓碑错误路径下桥已自退，kill 无害）
                if proc.poll() is None:
                    proc.kill()
                    _killed = True
                else:
                    _killed = False
                _emit({"started": False, "error": str(e),
                       "killed_orphan": _killed})
                return 1
            _emit({"started": True, "pid": proc.pid, "mode": st.get("mode"),
                   "url": st.get("url"), "dir": str(wdir)})
            if args.solve or args.manual or args.wait_selector:
                return _solve_flow()
            return 0
        if args.url or args.solve or args.manual or args.wait_selector:
            return _solve_flow()
        print("用法：cli captcha --start --dir <目录> [--url ...] / "
              "cli captcha --solve --dir <目录> --prompt '依次点击：国 家 税' "
              "--captcha-selector '.geetest_item' --wait-selector '.result' / "
              "cli captcha --status|--stop --dir <目录>", file=sys.stderr)
        return 1

    if args.cmd == "shell":
        # 交互式调试（对标 Scrapy shell）：抓 URL → 进入 Python REPL，
        # 预注入 response + 便捷函数，小白试选择器不再需要"改配置→跑→看结果"循环
        import code as _code_mod
        import re as _re_mod
        from .quick import fetch_url
        _sh_actions = None
        if args.actions:
            try:
                _sh_actions = json.loads(args.actions)
            except json.JSONDecodeError as e:
                print(f"❌ --actions 不是合法 JSON: {e}", file=sys.stderr)
                return 1
        # 审查十轮（H 同款）：--cdp/--actions 自动启用浏览器模式（--cdp 忘加
        # --browser 时登录态通道曾静默变成匿名直连）
        r = fetch_url(args.url,
                      browser=(args.browser or bool(args.cdp) or bool(_sh_actions)),
                      cookie=args.cookie or None,
                      proxy=args.proxy or None, timeout=args.timeout,
                      js=args.js or None, wait_selector=args.wait_selector or None,
                      actions=_sh_actions, cdp=args.cdp or None)
        if r.get("error"):
            print(f"❌ 抓取失败: {r['error']}", file=sys.stderr)
            return 1
        html = r.get("text", "") or ""
        _banner = (
            f"\n🐚 万能爬虫交互式调试（{r.get('backend', '?')} | HTTP {r.get('status', '?')} | {len(html):,} 字符）\n"
            f"  常用变量:\n"
            f"    resp          = 完整 response dict\n"
            f"    html          = 页面 HTML 全文\n"
            f"    sel('css')    → CSS 选择器结果列表\n"
            f"    rex(r'正则')  → 正则匹配列表\n"
            f"    jp('a.b.*')   → JSON 路径提取\n"
            f"    links()       → 页面所有链接\n"
            f"    forms()       → 页面所有表单结构\n"
            f"  试试: sel('h1') / rex(r'价格.*?(\\d+)') / forms()\n"
        )
        print(_banner)

        def sel(pattern):
            """快速 CSS 选择器"""
            try:
                from lxml import html as _lh
                doc = _lh.fromstring(html)
                return [e.text_content().strip() if not e.attrib else
                        {**e.attrib, "text": e.text_content().strip()} for e in doc.cssselect(pattern)]
            except Exception as e:
                return [f"❌ {e}"]

        def rex(pattern, flags=_re_mod.S | _re_mod.I):
            """快速正则匹配"""
            return _re_mod.findall(pattern, html, flags)

        def jp(path):
            """JSON 路径提取"""
            from .selectors import jpath
            try:
                data = json.loads(html) if html.strip().startswith(("{", "[")) else {}
            except Exception:
                data = {}
            return jpath(data, path) if data else []

        def links():
            """页面所有链接"""
            from .extractors import extract_links_markdown
            return extract_links_markdown(html, base_url=r.get("url", ""))

        def forms():
            """页面所有表单结构（字段名/类型/提交地址）"""
            from .extractors import discover_forms
            return discover_forms(html, base_url=r.get("url", ""))

        ns = {"resp": r, "html": html, "sel": sel, "rex": rex,
              "jp": jp, "links": links, "forms": forms,
              "url": r.get("url", ""), "status": r.get("status", 0)}
        try:
            from IPython import embed as _ipy
            _ipy(user_ns=ns, colors="neutral")
        except ImportError:
            import readline  # noqa: F401
            _code_mod.interact(local=ns, banner="")
        return 0

    if args.cmd == "capture-daemon":
        # 实战反馈五#2：常驻捕获守护通用化（签名型站点在场捕获，页自算签名不逆向）
        import json as _json
        import os as _os
        import subprocess as _sp
        import time as _time
        from pathlib import Path as _Path
        from .runtime import resolve_node, resolve_node_path
        _cap = _Path(args.capture).resolve()
        _stf = _Path(str(_cap) + ".status.json")
        if args.action == "status":
            try:
                d = _json.loads(_stf.read_text(encoding="utf-8"))
            except Exception:
                print("📡 守护进程未在运行（无状态文件）")
                return 0
            _pid = d.get("pid")
            _alive = False
            if _pid:
                try:
                    _os.kill(int(_pid), 0)
                    _alive = True
                except OSError:
                    pass
            print(f"📡 {'运行中' if _alive else '已停止（状态残留）'} pid={_pid} "
                  f"已捕获={d.get('captured', 0)} 条\n  hosts={d.get('hosts')}\n  capture={d.get('capture')}")
            return 0 if _alive else 1
        if args.action == "stop":
            try:
                import signal as _sg
                d = _json.loads(_stf.read_text(encoding="utf-8"))
                _os.kill(int(d["pid"]), _sg.SIGTERM)
            except Exception as e:
                print(f"⚠️ 停止失败（{e}）——可能本就未运行", file=sys.stderr)
                return 1
            print("📡 已发送 TERM，守护进程优雅退出中")
            return 0
        # start
        if not args.hosts:
            print("❌ start 需要 --hosts（捕获域白名单，逗号分隔）", file=sys.stderr)
            return 1
        node, npath = resolve_node(), resolve_node_path()
        _cmd = [node, str(Path(__file__).resolve().parent.parent / "scripts" / "capture_daemon.cjs"),
                "--hosts", args.hosts, "--capture", str(_cap),
                "--profile", (args.profile or str(_Path.home() / ".universal-scraper" / "daemon_profile")),
                "--cdp", "9222"]
        if args.proxy:
            _cmd.extend(["--proxy", args.proxy])
        _env = {**_os.environ, "NODE_PATH": npath}
        _logf = open(_Path(str(_cap) + ".daemon.log"), "ab")
        try:
            _stf.unlink()          # 清掉上一次运行的残留状态文件（否则会被当"已启动"）
        except Exception:
            pass
        _t_start = _time.time()
        _sp.Popen(_cmd, stdout=_logf, stderr=_sp.STDOUT,
                  stdin=_sp.DEVNULL, start_new_session=True, env=_env)
        # 审查八轮（MEDIUM）：此前只判状态文件"是否存在"就打印已启动并 return 0——
        # 上一次运行残留的 status.json 会让"新进程其实没起来"（profile 被占/node 缺库/
        # CDP 9222 不可用）也报成功。改为真存活校验：pid 存活 且 状态文件在本次启动后
        # 被刷新；超时则打日志尾部并返回 1（与 xhs 分支的 running 轮询同口径）。
        for _ in range(40):
            _time.sleep(0.25)
            try:
                _d = _json.loads(_stf.read_text(encoding="utf-8"))
            except Exception:
                continue
            _pid = _d.get("pid")
            if not _pid or _d.get("stopping"):
                continue
            try:
                if _stf.stat().st_mtime < _t_start - 0.5:
                    continue          # 旧文件（未刷新）
                _os.kill(int(_pid), 0)
            except OSError:
                continue
            except Exception:
                continue
            print(f"📡 守护进程已启动（pid={_pid}），捕获 → {_cap}")
            print("   在有头窗口里人工登录/操作；停止用 capture-daemon stop")
            return 0
        print("❌ 启动超时或进程未能存活（看 capture.daemon.log）", file=sys.stderr)
        try:
            _tail = _Path(str(_cap) + ".daemon.log").read_text(encoding="utf-8", errors="ignore")[-400:]
            if _tail.strip():
                print("--- 日志尾部 ---\n" + _tail, file=sys.stderr)
        except Exception:
            pass
        return 1

    if args.cmd == "xhs":
        # 实战反馈五#1 收编：小红书一等公民采集（搜索 Top-N + 评论 + 作者主页）
        if not _guard_out_is_dir(getattr(args, "out", None), "xhs"):
            return 1
        import json as _json
        import os as _os
        import subprocess as _sp
        import time as _time
        from pathlib import Path as _Path
        from .runtime import resolve_node, resolve_node_path
        from .modules import xhs as _xhs
        out_dir = _Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        capture = out_dir / "capture.jsonl"

        # ① 出口预检（#3：请用户扫码/登录前先确认出口没被目标站封）
        eg = _xhs.check_egress(args.proxy)
        print(f"🌐 出口预检: {eg.get('egress_ip', '?')}（{eg.get('egress_note', '')}）"
              + (f" 代理={args.proxy}" if args.proxy else ""))
        print("   出口 IP 若已确认被目标站封锁，先 --proxy 换出口再继续，避免登录暴露")

        # ② 守护进程（cli 层持有 Popen；xhs.py 只构造命令——进程职责分离）
        st = _xhs.daemon_status(capture)
        if not st.get("running"):
            node, npath = resolve_node(), resolve_node_path()
            _cmd = _xhs.daemon_cmd(capture, proxy=args.proxy)
            _env = {**_os.environ, "NODE_PATH": npath}
            _logf = open(capture.with_suffix(".daemon.log"), "ab")
            _sp.Popen(_cmd, stdout=_logf, stderr=_sp.STDOUT,
                      stdin=_sp.DEVNULL, start_new_session=True, env=_env)
            for _ in range(40):
                _time.sleep(0.25)
                if _xhs.daemon_status(capture).get("running"):
                    break
            else:
                _hint = ""
                try:
                    _logtail = capture.with_suffix(".daemon.log").read_text(
                        encoding="utf-8", errors="ignore")[-500:]
                    if "现有的浏览器会话" in _logtail or "existing browser" in _logtail:
                        _hint = ("——检测到同 profile 已有浏览器实例在运行，"
                                 "请先关闭旧窗口（或 kill 旧进程）再试")
                except OSError:
                    pass
                print(f"❌ 守护进程启动超时{_hint}（看 {capture.with_suffix('.daemon.log')}）",
                      file=sys.stderr)
                return 1
        print("📕 守护进程在位。请在有头窗口完成：①登录小红书 ②搜索关键词 "
              f"\"{args.keyword}\"\n   （本命令轮询捕获，最多等 {args.wait}s）")

        # ③ 轮询搜索卡片
        node, npath = resolve_node(), resolve_node_path()
        _env = {**_os.environ, "NODE_PATH": npath}
        _deadline = _time.time() + args.wait
        cards = []
        while _time.time() < _deadline:
            cards = _xhs.parse_search_cards(capture)
            if len(cards) >= max(3, args.top // 4):
                break
            _time.sleep(3)
        if not cards:
            print("❌ 等待超时未捕获到搜索结果——确认已登录并在窗口完成搜索", file=sys.stderr)
            return 1
        cards = cards[:args.top]
        print(f"🔍 捕获 {len(cards)} 篇（按点赞取前 {args.top}）：")
        for c in cards[:10]:
            print(f"   {c['liked_count_raw']:>8} 赞  {c['title'][:50]}")

        # ④ 逐篇驱动（滚动评论到目标数）+ 解析；导航间隔 ≥3.5s（#4 节奏建议）
        note_rows, comment_rows, user_rows = [], [], []
        node_bin = node
        for idx, c in enumerate(cards, 1):
            print(f"📥 [{idx}/{len(cards)}] {c['title'][:40]} …")
            _cp_env = {**_env, "CAPTURE_PATH": str(capture)}
            # 审查六轮（H2）：驱动超时曾炸穿 handler——已采的整轮数据随异常丢失。
            # 超时/失败跳过该篇继续（capture 数据仍在，可补解析）
            try:
                _r = _sp.run([node_bin, str(Path(__file__).resolve().parent.parent / "scripts" / "xhs_collect_note.cjs"),
                              c["note_id"], c["xsec_token"], str(out_dir / f"note_{idx:02d}")],
                             capture_output=True, text=True, env=_cp_env, timeout=300)
            except _sp.TimeoutExpired:
                print("   ⚠️ 单篇驱动超时（300s）——跳过该篇继续", file=sys.stderr)
                _time.sleep(3.5)
                continue
            if _r.returncode != 0:
                print(f"   ⚠️ 驱动失败（已跳过，capture 仍保留数据）: {(_r.stderr or '')[:100]}")
            detail = _xhs.parse_note_detail(capture, c["note_id"])
            interact = detail.get("interact_info") or {}
            # 审查六轮（H3）：h264 变体可能是空数组/None——get 默认值只兜键缺失，
            # [0] 对空列表 IndexError 会炸掉整轮采集
            _h264 = (((detail.get("video") or {}).get("media") or {}).get("stream") or {}).get("h264") or []
            _video_url = (_h264[0].get("master_url", "") if _h264 else "")
            note_rows.append({
                "笔记ID": c["note_id"], "标题": c["title"],
                "正文": detail.get("desc", ""),
                "话题标签": " ".join(t.get("name", "") for t in detail.get("tag_list") or []),
                "图片": " ".join((im.get("url_default") or im.get("url", "")) for im in detail.get("image_list") or []),
                "视频": _video_url,
                "发布时间": _time.strftime("%Y-%m-%d %H:%M", _time.localtime(int(detail.get("time", 0) or 0) / 1000)) if detail.get("time") else "",
                "点赞": (interact.get("liked_count") or ""), "收藏": (interact.get("collected_count") or ""),
                "评论数": (interact.get("comment_count") or ""), "分享": (interact.get("share_count") or ""),
                "类型": c.get("type", ""), "推广标识": "是" if detail.get("is_promotion") else "",
                "作者ID": (detail.get("user") or {}).get("user_id", ""),
                "作者昵称": (detail.get("user") or {}).get("nickname", ""),
                "_url": f"https://www.xiaohongshu.com/explore/{c['note_id']}",
            })
            cms = _xhs.parse_comments(capture, c["note_id"])
            for cm in cms[:args.comments]:
                cm["笔记ID"] = c["note_id"]
                comment_rows.append(cm)
            uid = (detail.get("user") or {}).get("user_id", "")
            if uid:
                try:
                    _sp.run([node_bin, str(Path(__file__).resolve().parent.parent / "scripts" / "xhs_ctl.cjs"), "goto",
                             f"https://www.xiaohongshu.com/user/profile/{uid}", "4000"],
                            capture_output=True, text=True, env=_cp_env, timeout=90)
                except _sp.TimeoutExpired:
                    print("   ⚠️ 作者主页导航超时（跳过该主页）", file=sys.stderr)
                _time.sleep(2)
                user_rows.append({"用户ID": uid, **_xhs.parse_user_profile(capture, uid)})
            _time.sleep(3.5)  # 导航间隔（评论型任务节奏）
        print(f"📊 汇总：笔记 {len(note_rows)} | 评论 {len(comment_rows)} | 主页 {len(user_rows)}")
        from .core import export_rows
        paths = export_rows(note_rows, out_dir, "notes")
        export_rows(comment_rows, out_dir, "comments")
        export_rows(user_rows, out_dir, "users")
        print(f"📁 导出: {list(paths.values()) if isinstance(paths, dict) else paths}")
        print("🛑 采集完成——守护进程保持运行（人工窗口可继续用）；停止: capture-daemon stop 或关窗口")
        return 0

    if args.cmd == "map":
        # 实战反馈五对标 Firecrawl /map：入口页链接 + sitemap 并集，秒级出全站
        # URL 清单——先 map 再选页，避免对"有多大/长什么样"一无所知就开爬
        import re
        from urllib.parse import urlsplit as _usp
        from .core import make_http_client
        from .queue import extract_links
        _sp0 = _usp(args.url)
        if _sp0.scheme not in ("http", "https"):
            print("❌ 仅允许 http/https", file=sys.stderr)
            return 1
        # 审查八轮（HIGH）：map 走 client.get 直连且无地址校验——与 fetch/shell 的
        # 守卫口径不一致（内网/环回可被探测）。统一过出站守卫。
        try:
            from .core import assert_public_url as _apu_m
            _apu_m(args.url, context="cli map")
        except Exception as _e:
            print(f"⛔ {_e}", file=sys.stderr)
            return 1
        client = make_http_client({"min_interval": 0.3, "timeout": 20})
        urls: list = []
        res = client.get(args.url)
        if res.get("ok"):
            urls = extract_links(res.get("text", ""), args.url,
                                 allow=args.allow, deny=args.deny)
        if args.sitemap:
            try:
                from .engine import fetch_sitemap_urls
                _base = f"{_sp0.scheme}://{_sp0.netloc}"
                sm_urls = fetch_sitemap_urls(client, f"{_base}/sitemap.xml",
                                             max_urls=args.max_urls)
                # 审查六轮（L4）：sitemap URL 也过一遍 allow/deny（与 extract_links
                # 同口径 path+query）；CLI 级重复复筛曾用纯 path 口径清空合法结果。
                # 审查十轮（H）：完整 URL 口径曾让锚定写法（^/page/\d+/$）把
                # sitemap URL 静默清光——统一按 path+query 复筛
                from urllib.parse import urlsplit as _usp

                def _pq(u):
                    _p = _usp(u)
                    return (_p.path or "") + (("?" + _p.query) if _p.query else "")
                if args.allow:
                    sm_urls = [u for u in sm_urls if re.search(args.allow, _pq(u))]
                if args.deny:
                    sm_urls = [u for u in sm_urls if not re.search(args.deny, _pq(u))]
                urls.extend(u for u in sm_urls if u not in urls)
            except Exception as e:
                print(f"  (sitemap 不可用: {type(e).__name__})", file=sys.stderr)
        # 审查十轮（L）：负数曾触发 urls[:-n] 截尾语义（丢尾部 N 条而非"最多 N 条"）
        urls = urls[:max(0, args.max_urls)]
        print(f"🗺️ {args.url} → {len(urls)} 个 URL：")
        for u in urls[:100]:
            print(f"  {u}")
        if len(urls) > 100:
            print(f"  …（其余 {len(urls) - 100} 条用 --max-urls 控制）")
        return 0 if urls else 1

    if args.cmd == "pagefn":
        from .quick import pagefn_recon
        r = pagefn_recon(args.url, limit=args.limit)
        if r.get("error"):
            print(f"❌ {r['error']}", file=sys.stderr)
            return 1
        print(f"🧩 {r['url']} 非原生全局函数 {r['count']} 个（实现长的排前）：")
        for f in r["functions"]:
            print(f"  {f['name']:32s} {f['src'][:90]}")
        print(f"   {r['hint']}")
        return 0 if r["count"] else 1

    if args.cmd == "rangedl":
        from .rangedl import rangedl
        if not _guard_out_is_file(getattr(args, "out", ""), "rangedl"):
            return 2
        r = rangedl(args.url, out=args.out, segments=args.segments,
                    concurrency=args.concurrency)
        if r.get("error"):
            print(f"❌ {r['error']}", file=sys.stderr)
            return 1
        print(f"✅ {args.out}（{r['size']}B，{r['segments']} 段"
              f"{'，含续传' if r.get('resumed') else ''}）")
        return 0

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
        if args.action == "claim":
            it = q.claim()
            print(json.dumps({"claimed": bool(it), "task": it}, ensure_ascii=False))
            return 0 if it else 1
        if args.action == "touch":
            # 长任务续租（审查 P2）：>30min 的任务必须周期性 touch 防 stale 回收
            try:
                it = q.touch(args.item_id)
            except (ValueError, KeyError) as e:  # R19b：手滑 id 不该甩栈（与 mark 同标准）
                print(f"❌ {e}", file=sys.stderr)
                return 1
            print(json.dumps({"touched": True, "id": args.item_id}, ensure_ascii=False))
            return 0
        if args.action == "status":
            print(json.dumps(q.status(), ensure_ascii=False))
            return 0
        if not args.item_id:
            print("❌ done/fail/blocked/nodata/retry 需要任务 id", file=sys.stderr)
            return 1
        status_map = {"done": "done", "fail": "failed", "blocked": "blocked",
                      "nodata": "nodata", "retry": "retry"}
        try:
            it = q.mark(args.item_id, status_map[args.action], args.result)
        except (KeyError, ValueError) as e:  # 审查修复：手滑 id 不该甩栈
            print(f"❌ {e}", file=sys.stderr)
            return 1
        st = q.status()
        print(json.dumps({"marked": it.get("id"), "status": it.get("status"), **st},
                         ensure_ascii=False))
        return 0

    if args.cmd == "curl2config":
        import json as _json
        from .curl_import import build_config
        _cmd = args.curl_cmd
        if not _cmd:
            _cmd = sys.stdin.read()
        try:
            r = build_config(_cmd, name=args.name, prefer_html=bool(args.html))
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2
        for w in r["warnings"]:
            print(f"⚠️ {w}", file=sys.stderr)
        payload = _json.dumps(r["config"], ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(payload, encoding="utf-8")
            print(f"✅ 配置草案 → {args.out}（先 --dry-run 校验，再 --limit 2 小样验证）")
        else:
            print(payload)
        return 0

    if args.cmd == "capture2config":
        from .capture_gen import generate
        out = args.out or str(Path(args.capture).with_suffix("").resolve()) + "_configs.json"

        def _run_once() -> int:
            r = generate(args.capture, referer=args.referer, out=out)
            if r.get("error"):
                print(f"❌ {r['error']}", file=sys.stderr)
                return 1
            for c in r.get("configs", [])[:10]:
                src = c["source"]
                rp = c.get("pagination", {}).get("records_path") or "?"
                print(f"  {src.get('method','GET'):4s} {src['url'][:80]}  records_path={rp}")
            if r.get("saved"):
                print(f"✅ {r['count']} 份配置草案 → {r['saved']}（先 --limit 2 小样验证）")
            return 0 if r.get("count") else 1

        if not getattr(args, "watch", False):
            return _run_once()

        # 👀 watch 模式（归档功能请求）：浏览器桥持续追加 capture_all.json 时，
        # 每次落盘稳定后自动重新生成配置草案——省去手动重跑
        import time as _time
        cap = Path(args.capture)
        if not cap.exists():
            print(f"❌ 捕获文件不存在: {cap}", file=sys.stderr)
            return 1
        print(f"👀 watch 模式：监听 {cap.name} 变化（Ctrl+C 退出）…")
        # 审查 L1：桥的 unlink+rename 原子落盘瞬间 stat 会 FileNotFoundError——
        # 三处 stat 统一防护，竞态按"没变化"处理，绝不冲出常驻循环
        try:
            last_mtime = cap.stat().st_mtime
        except FileNotFoundError:
            last_mtime = 0.0
        try:
            while True:
                _time.sleep(1.5)
                try:
                    m = cap.stat().st_mtime
                except FileNotFoundError:
                    continue
                if m == last_mtime:
                    continue
                # 防抖：桥可能分片写入——1.5s 内 mtime 稳定才触发
                _time.sleep(1.5)
                try:
                    if cap.stat().st_mtime != m:
                        continue
                    last_mtime = cap.stat().st_mtime
                except FileNotFoundError:
                    continue
                print(f"\n🔄 捕获文件已更新（{_time.strftime('%H:%M:%S')}），重新生成…")
                try:
                    _run_once()
                except Exception as e:
                    print(f"⚠️ 重新生成失败: {type(e).__name__}: {e}", file=sys.stderr)
        except KeyboardInterrupt:
            # cli 全局 SIGTERM 处理器也走这里（优雅停机）——静默退出，不甩裸栈
            print("\n👋 watch 已退出", file=sys.stderr)
            return 0


    if args.cmd == "pdf":
        if args.download:
            from .pdf_attach import download_attachments
            # R32 修复：清单文件缺失/坏 JSON 曾甩裸栈（siblings 都有干净报错）
            try:
                r = download_attachments(args.download, args.out or "attachments", interval=args.interval)
            except FileNotFoundError as e:
                print(f"❌ 清单文件不存在: {e.filename}", file=sys.stderr)
                return 1
            except (json.JSONDecodeError, ValueError) as e:
                print(f"❌ 清单不是合法 JSON: {e}", file=sys.stderr)
                return 1
            print(json.dumps({k: r[k] for k in ("total", "ok", "skipped", "failed", "dir")},
                             ensure_ascii=False))
            return 0 if not r["failed"] else 1
        if args.tables:
            from .pdf_attach import extract_tables, table_quality_report
            try:
                tables = extract_tables(args.tables)
            except Exception as e:  # 审查修复：pypdf 的 PdfStreamError 等曾甩裸栈
                print(f"❌ {type(e).__name__}: {e}", file=sys.stderr)
                return 1
            out = args.out or str(Path(args.tables).with_suffix(".tables.json").resolve())
            # 审查十轮（M）：--out 指目录/父目录缺失曾裸栈——抽取出的表格 JSON
            # 没写到任何地方就崩（IsADirectoryError/FileNotFoundError）
            if not _guard_out_is_file(out, "pdf --tables"):
                return 1
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(json.dumps(tables, ensure_ascii=False, indent=1), encoding="utf-8")
            n = sum(len(t["rows"]) for t in tables)
            # 质量门（招行摘要战训：退化垃圾列曾照样报 ✅）
            q = table_quality_report(tables)
            Path(str(out) + ".quality.json").write_text(
                json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")
            if q.get("degraded"):
                print(f"⚠️ 表格质量门：疑似退化（列名回退 {q['fallback_rate']}、空单元格 {q['empty_rate']}）"
                      f"——{q['hint']}", file=sys.stderr)
                print(f"⚠️ {len(tables)} 页表格 / {n} 行 → {out}（⚠️ 质量存疑，见 {out}.quality.json）")
                return 2
            print(f"✅ {len(tables)} 页表格 / {n} 行 → {out}")
            return 0
        print("用法：--download <清单.json> --out <目录> / --tables <pdf>", file=sys.stderr)
        return 1

    if args.cmd == "session":
        from .api_session import run_session
        # 审查十轮（M）：--out 指向已存在文件曾 FileExistsError 裸栈（mkdir 只豁免目录）
        if not _guard_out_is_dir(args.out, "session"):
            return 1
        try:
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8-sig"))
        except Exception as e:
            print(f"❌ 计划读取/解析失败: {e}", file=sys.stderr)
            return 1
        result = run_session(plan, out_dir=Path(args.out), max_requests=args.max_requests,
                             min_gap=args.min_gap, timeout=args.timeout,
                             risk_accepted=args.risk_accepted)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        if result.get("stopped") == "max_requests":
            return 4
        if result.get("stopped") == "blocked":
            print("⛔ 封禁/拦截判定硬停机——已抓数据保留，请冷却/换出口后再评估", file=sys.stderr)
            return 5
        return 0 if result.get("ok") else 1

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
        if args.usage:
            from .core import request_budget
            r = request_budget()
            if r["limit"]:
                print(f"📊 本进程任务请求额度: 已用 {r['used']} / {r['limit']}（剩 {r['remaining']}）"
                      f"——计数含重试，按真实 HTTP 尝试计（run --max-requests / anti_bot.max_requests）")
            else:
                print(f"📊 本进程任务请求额度: 未设上限（已计 {r['used']} 次）")
            return 0
        if args.probe:
            # 放行窗口探针（gsxt 战训）：行为评分型限流下先探窗口再干活
            from .domain_budget import probe_window
            try:
                r = probe_window(args.probe, expect=args.expect, every=args.every,
                                 max_rounds=args.max_rounds, timeout=args.timeout,
                                 log=lambda m: print(m, file=sys.stderr, flush=True))
            except ValueError as e:
                print(f"❌ {e}", file=sys.stderr)
                return 1
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r["state"] == "OPEN" else 2
        if args.action:
            # 稀缺动作预算（gsxt 战训）：贵动作发起即扣额，失败不退
            from .action_budget import acquire
            r = acquire(args.action, limit=args.action_limit, window=args.action_window,
                        cost=args.action_cost, path=args.file)
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r["allowed"] else 2
        if args.action_info:
            from .action_budget import state as _ab_state
            r = _ab_state(args.action_info, limit=args.action_limit,
                          window=args.action_window, path=args.file)
            print(json.dumps(r, ensure_ascii=False))
            return 0
        print("用法：--mark <域名> [--hours 24 --note ...] / --check <域名> / --list / --usage / "
              "--probe <url> [--expect 标记 --every 60] / --action <名> [--action-limit 5 --action-window 3600] / "
              "--action-info <名>", file=sys.stderr)
        return 1

    if args.cmd == "bili":
        # R101 沉淀：B站四通道（元数据/弹幕/评论/UP主列表）——战法详见 recipes R34
        if not _guard_out_is_dir(getattr(args, "out", None), "bili"):
            return 1
        from pathlib import Path as _P
        from universal_scraper import bili as _bili
        from universal_scraper.core import export_rows
        if not args.video and not args.mid:
            print("用法: us bili --video BV1abc  或  us bili --mid <UID> --year 2024", file=sys.stderr)
            return 1

        def _fetch_all(bvid: str, out_dir: _P, client=None, wbi: dict = None):
            # R113：client/wbi 由调用方复用传入（--mid 循环曾每视频重建客户端，
            # 重置限速器并多一次预热请求）；单视频模式传 None 自建
            client = client or _bili._client()
            meta = _bili.video_meta(client, bvid)
            # R111 修复（P2）：--mid 模式曾固定名 meta.json 互相覆盖——按 bvid 命名
            (_P(out_dir) / f"meta_{bvid}.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            danmaku = _bili.danmaku(client, meta.get("cid"), limit=args.limit_danmaku)
            comments = _bili.comments(client, meta.get("aid"),
                                      limit=args.limit_comments,
                                      with_replies=not args.no_replies,
                                      wbi=wbi)
            return meta, danmaku, comments

        def _export(rows, name, out_dir: _P):
            if not rows:
                print(f"  ⚠️ {name}: 0 条", file=sys.stderr)
                return
            paths = export_rows(rows, _P(out_dir), name, formats=["json", "csv", "xlsx"])
            n = len(rows)
            print(f"  ✅ {name}: {n} 条 -> {list(paths.values())}", file=sys.stderr)

        out_dir = _P(args.out or f"outputs/bili_{args.video or args.mid}")
        out_dir.mkdir(parents=True, exist_ok=True)

        if args.video:
            if args.mid:
                print("ℹ️ --video 与 --mid 同时指定：按 --video 单视频模式执行（--mid 忽略）",
                      file=sys.stderr)
            print(f"📺 B站视频 {args.video}", flush=True)
            try:
                meta, danmaku, comments = _fetch_all(args.video, out_dir)
            except RuntimeError as e:
                # R111 修复（P2）：bili.py 的用户导向错误（-352/-412/-400）曾裸栈退出
                print(f"❌ {e}", file=sys.stderr)
                return 1
            print(f"   标题: {meta.get('标题', '')}\n   播放 {meta.get('播放量')} | 弹幕 {meta.get('弹幕总数')} | 评论 {meta.get('评论总数')}", flush=True)
            _export(danmaku, f"bili_{args.video}_弹幕", out_dir)
            _export(comments, f"bili_{args.video}_评论", out_dir)
            print(f"📁 输出目录: {out_dir}")
            return 0

        # --mid：UP主投稿列表 → 逐视频三通道
        client = _bili._client()
        try:
            wbi = _bili.get_wbi_keys(client)
            vids = _bili.user_videos(client, args.mid, wbi,
                                     dm_img_str=args.dm_img_str,
                                     dm_cover_img_str=args.dm_cover_img_str,
                                     dm_img_inter=args.dm_img_inter,
                                     max_pages=max(1, args.max_videos // 30 + 1),
                                     year=args.year or None)
        except RuntimeError as e:
            # R113 修复（P2）：列表阶段（-352/-412/nav 失败）曾裸栈退出
            print(f"❌ {e}", file=sys.stderr)
            return 1
        vids = vids[:args.max_videos]
        print(f"📺 UP主 {args.mid}：命中 {len(vids)} 个视频（year={args.year or '全部'}）", flush=True)
        if not vids:
            return 3
        failed = []
        for i, v in enumerate(vids, 1):
            bvid = v.get("bvid", "")
            if not bvid:
                continue
            print(f"[{i}/{len(vids)}] {v.get('标题', '')[:40]}", flush=True)
            try:
                # R113：复用 --mid 阶段的 client 与缓存 wbi（少一次预热+nav 往返）
                meta, danmaku, comments = _fetch_all(bvid, out_dir, client=client, wbi=wbi)
                _export(danmaku, f"bili_{bvid}_弹幕", out_dir)
                _export(comments, f"bili_{bvid}_评论", out_dir)
            except Exception as e:
                failed.append(bvid)
                print(f"  ❌ {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
        if failed:
            print(f"⚠️ {len(failed)} 个视频失败: {', '.join(failed[:10])}——按铁律诊断后重跑", file=sys.stderr)
            return 1
        print(f"📁 输出目录: {out_dir}")
        return 0

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
            # R42b 修复：局部 import os 曾遮蔽模块级 os——全文下载失败路径之后的
            # run 封禁处理器用到 os.path 会 UnboundLocalError（webui 同款事故）
            import subprocess
            from pathlib import Path as _P
            script = _P(__file__).resolve().parent.parent / "scripts" / "kygl_fulltext_download.py"
            # 审查修复（P2）：脚本缺失曾让成功的采集以 exit 2 收场——先检查再跑
            if not script.exists():
                print(f"❌ 全文追加脚本缺失: {script}", file=sys.stderr)
                print("   （journal 采集本身已成功；全文 PDF 追加功能在当前安装中不可用）",
                      file=sys.stderr)
                return 1
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
            from pathlib import Path as _P
            try:
                cookie = _P(args.cookie_file).read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                print(f"❌ cookie 文件不存在: {args.cookie_file}", file=sys.stderr)
                return 1
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

    if args.cmd == "diagnose":
        # 📒 诊断台账（归档功能请求）：--history 查历史判型（同一站点多轮诊断
        # 可追溯，agent 循环免重跑）
        if getattr(args, "history", None) is not None:
            _lp = Path("outputs") / "diagnose_history.jsonl"
            if not _lp.exists():
                print("📒 诊断台账为空（跑一次 diagnose 自动落账）")
                return 0
            _kw = args.history.lower()
            _rows = []
            for line in _lp.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                try:
                    _e = json.loads(line)
                except Exception:
                    continue
                if not _kw or _kw in str(_e.get("url", "")).lower():
                    _rows.append(_e)
            if not _rows:
                print(f"📒 台账无匹配「{args.history}」的记录")
                return 0
            print(f"📒 诊断台账（{_kw or '全部'}，最近 {len(_rows[-20:])} 条）：")
            for _e in _rows[-20:]:
                _b = _e.get("block") or {}
                print(f"  {_e.get('ts','?')} {_e.get('mode','?'):5s} {_e.get('status','?'):>3} "
                      f"{(_b.get('name') or _b.get('type') or '?')[:14]:14s} {_e.get('url','')[:70]}")
            return 0
        if not args.url:
            print("❌ 缺少 URL（diagnose <url>；查历史用 diagnose --history）", file=sys.stderr)
            return 1
        # 审查八轮（HIGH）：diagnose 曾无出站守卫——与 fetch/shell 的口径不一致，
        # 被诱导评测内网地址时会把请求真发出去（盲 SSRF + 状态码泄漏）。
        try:
            from .core import assert_public_url as _apu_d
            _apu_d(args.url, context="cli diagnose")
        except Exception as _e:
            print(f"⛔ {_e}", file=sys.stderr)
            return 1

        from .diagnose import format_verdict
        if getattr(args, "quick", False):
            from .diagnose import diagnose_quick as _diagnose
        else:
            from .diagnose import diagnose as _diagnose
        d = _diagnose(args.url, proxy=(args.proxy or None))
        blk = d.get("block") or {}  # 必须在 json 分支外取：审查修复，--json 路径曾 UnboundLocalError 必崩
        # 📒 落账（含失败诊断——排障时"哪次开始坏的"靠它）
        try:
            from datetime import datetime as _dt
            _lp = Path("outputs") / "diagnose_history.jsonl"
            _lp.parent.mkdir(parents=True, exist_ok=True)
            _entry = {"ts": _dt.now().strftime("%m-%d %H:%M"), "url": args.url,
                      "mode": "quick" if getattr(args, "quick", False) else "full",
                      "status": d.get("status"), "block": blk,
                      "error": d.get("error")}
            with open(_lp, "a", encoding="utf-8") as _f:
                _f.write(json.dumps(_entry, ensure_ascii=False) + "\n")
            # 防膨胀：超 2000 行截到最近 1000（tmp+replace 原子替换）
            if _lp.exists() and _lp.stat().st_size > 512 * 1024:
                _lines = _lp.read_text(encoding="utf-8", errors="ignore").splitlines()
                if len(_lines) > 2000:
                    _tmp = _lp.with_suffix(".jsonl.tmp")
                    _tmp.write_text("\n".join(_lines[-1000:]) + "\n", encoding="utf-8")
                    _tmp.replace(_lp)
        except Exception:
            pass  # 台账是锦上添花，落账失败绝不影响诊断结论
        if getattr(args, "out", ""):
            # 收官十五轮（用户复盘）：diagnose 少了 --out，与其他命令参数风格不一致
            # （用户明确点名）。与 --json 同内容，供批量/留证。
            # 收官十五轮（安全审计 L1）：写失败曾 return 1 并把**整份判型结论也吞掉**
            # （阻断类本该 exit 2 变成 exit 1）——改为只告警，结论与退出码照常
            try:
                _op = Path(args.out).expanduser()
                if _op.is_dir():
                    raise IsADirectoryError(_op)
                _op.parent.mkdir(parents=True, exist_ok=True)
                _op.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
                print(f"📄 诊断 JSON 已存: {_op}")
            except Exception as e:
                print(f"⚠️ --out 写入失败（不影响判型结论）: {type(e).__name__}: {e}"
                      f"——如需落盘请给文件名而非目录，如 --out out/diag.json", file=sys.stderr)
        if args.json:
            print(json.dumps(d, ensure_ascii=False, indent=1))
        else:
            print(format_verdict(d))
            if blk.get("type") not in (None, "ok"):
                print("   完整判型表与升级阶梯: references/anti-block-playbook.md")
        # 判型为阻断类时退出码 2（脚本可据此分支），正常/SPA 提示为 0
        if d.get("error"):
            # 审查修复（P2）：网络层失败（DNS/离线/URL 拼错）曾 exit 0——与
            # "畅通无阻"同码，脚本会拿假绿灯去爬一个不可达的站
            print("❌ 诊断请求本身失败（网络层错误，非站点封禁）", file=sys.stderr)
            return 1
        return 2 if (blk.get("is_block")) else 0

    if args.cmd == "guide":
        from .agent_guide import emit
        if not _guard_out_is_file(getattr(args, "out", ""), "guide"):
            return 2
        p_ = emit(args.out)
        print(f"📖 子代理执行规范已生成: {p_.resolve()}")
        print("   随任务分派发给每个并行子代理，并写进调度 prompt。")
        return 0

    if args.cmd == "research":
        from .research import ResearchRunner, load_keywords, load_universe, build_panel_xlsx
        from pathlib import Path as _P
        # 审查十轮（M）：--out 指向已存在文件曾 FileExistsError 裸栈
        if not _guard_out_is_dir(getattr(args, "out", None), "research"):
            return 1
        if args.research_cmd == "run":
            kw = load_keywords(args.keywords)
            pending = load_universe(args.universe, args.year_from, args.year_to)
            runner = ResearchRunner(_P(args.out), kw, fuse=args.fuse)
            rc = runner.run(pending)
            if rc == 0 and getattr(args, "panel_xlsx", None):
                st = build_panel_xlsx(_P(args.out), args.universe, kw, args.year_from, args.year_to,
                                      _P(args.panel_xlsx))
                print(f"📦 面板 xlsx: {st}")
            return rc
        kw = load_keywords(getattr(args, "keywords", None))
        st = build_panel_xlsx(_P(args.out), args.universe, kw, args.year_from, args.year_to, _P(args.xlsx))
        print(f"📦 面板 xlsx: {st}")
        return 0

    if args.cmd == "audit":
        from . import audit as _audit
        a = vars(args)
        if args.audit_cmd == "panel":
            kw = None
            if a.get("keywords"):
                try:
                    kw = json.load(open(a["keywords"], encoding="utf-8"))
                except Exception as e:
                    print(f"✗ 词典文件读取失败: {e}")
                    return 1
            issues = _audit.audit_panel(args.xlsx, args.texts, a.get("universe"),
                                        args.year_from, args.year_to, keywords=kw)
        elif args.audit_cmd == "urls":
            issues = _audit.audit_urls(args.xlsx, a.get("sheet"), args.url_col, a.get("note_col"))
        else:
            try:
                srcs = json.load(open(args.sources, encoding="utf-8"))
            except Exception as e:
                print(f"✗ sources 映射文件读取失败: {e}")
                return 1
            issues = _audit.audit_verbatim(args.json, srcs, unique=not a.get("no_unique", False))
        if issues:
            print("\n".join("✗ " + i for i in issues))
            return 1
        print("✓ 审计全部通过")
        return 0

    if args.cmd == "verify":
        from .verify import verify_dir
        if args.dir:
            rep = verify_dir(args.dir)
            if rep.get("error"):
                # 审查修复：审计不存在的目录曾打印 verdict=None 垃圾且 exit 0
                print(f"❌ {rep['error']}", file=sys.stderr)
                return 1
            verdict_emoji = {"ok": "✅", "partial": "⚠️", "empty": "❌", "no_data_files": "❌"}.get(
                rep.get("verdict"), "•")
            print(f"🧾 目录审计 {rep.get('dir')}: {verdict_emoji} verdict={rep.get('verdict')}｜"
                  f"数据文件 {len(rep.get('files', []))}｜记录 {rep.get('total_records', 0)}")
            for f in rep.get("files", []):
                if f.get("error"):
                    print(f"  ⚠️ {f['file']}: {f['error']}")
                else:
                    print(f"  · {f['file']}: {f.get('records', 0)} 条"
                          + (f"，字段完整率 {f.get('field_complete_rate')}" if f.get("field_complete_rate") is not None else ""))
            ev = rep.get("evidence", {})
            print(f"  证据: summary.json={'有' if ev.get('summary_json') else '无'}, "
                  f"report.md={'有' if ev.get('report_md') else '无'}, "
                  f"evidence_* {len(ev.get('evidence_files', []))} 个")
            if rep.get("missing_evidence_refs"):
                print(f"  ⚠️ summary 引用但缺失的证据: {rep['missing_evidence_refs']}")
            # 退出码即验收门禁：agent_guide 依赖它做自动化判定
            return 0 if rep.get("verdict") == "ok" else 1
        if not args.file:
            print("用法：verify --file <结果.json> [--network] 或 verify --dir <任务目录>", file=sys.stderr)
            return 1
        from .verify import verify_file as _vf
        rep = _vf(args.file, network=args.network, data_key=args.data_key,
                  expect=args.expect, require=getattr(args, "require", ""),
                  expected_count=getattr(args, "expect_count", None),
                  expect_empty=getattr(args, "expect_empty", ""))
        # 审查修复（P2）：文件缺失/JSON 损坏等原因曾只进 dict 不打印——
        # 用户只看到"0 条｜存在问题"却无从分辨是文件没了还是语义不匹配
        if rep.get("error"):
            print(f"❌ {rep['error']}", file=sys.stderr)
            return 1
        print(f"🧾 复核报告：{rep.get('total', 0)} 条｜{'✅ 全部通过' if rep.get('ok') else '⚠️ 存在问题'}")
        for c in rep.get("checks", []):
            mark = "✅" if c.get("pass", True) else "❌"
            print(f"  {mark} {c['name']}: {c.get('value', '')}")
            for d in c.get("detail", [])[:5]:
                print(f"      - {d.get('url','')} reachable={d.get('reachable')} match={d.get('match')}")
        for c in rep.get("require_fields", []) or []:
            print(f"  {'✅' if c['pass'] else '❌'} {c['name']}: {c['value']}")
        if rep.get("semantic"):
            sem = rep["semantic"]
            mark = "✅" if sem["semantic_ok"] else "❌"
            line = f"  {mark} 语义校验: expect={sem['expect']}"
            has_plus = any(t.strip().startswith("+") for t in sem["expect"].split(","))
            if sem.get("matched_any"):
                line += f"｜任一词命中 {sem['matched'] or '无'}（{sem['matched_any']}，仅要求其一；多指标请用 +词 必含）"
            elif sem.get("matched"):
                line += f"｜命中 {sem['matched']}"
            if has_plus:
                line += ("｜必含词全部命中" if not sem.get("required_missed")
                         else f"｜❗必含词缺失 {sem['required_missed']}")
            if sem.get("excluded_hit"):
                line += f"｜出现排除词 {sem['excluded_hit']}"
            if sem.get("expect_warning"):
                line += f"｜⚠️ {sem['expect_warning']}"
            print(line)
        for c in rep.get("column_assertions", []):
            print(f"  {'✅' if c['pass'] else '❌'} {c['name']}: {c['value']}")
        return 0 if rep.get("ok") else 1

    if args.cmd == "webui":
        from .webui import serve
        # R42 修复：局部 import os 曾遮蔽模块级 os——之后 BlockDetectedError
        # 处理器里的 os.path/expandvars 触发 UnboundLocalError，封禁证据写不出
        token = getattr(args, "token", "") or os.environ.get("US_WEBUI_TOKEN", "")
        return serve(args.port, args.host, auto_open=not args.no_open,
                     share=args.share, token=token)

    if args.cmd == "crawl":
        from .quick import crawl_url
        # R32 修复（P1）：crawl 走同一 v3 引擎却没接 --task 路径的封禁终态契约
        # ——BlockDetectedError 曾裸栈 + exit 1 + 无证据
        try:
            result = crawl_url(args.url, depth=args.depth, max_pages=args.max,
                               allow=args.allow, deny=args.deny, browser=args.browser,
                               proxy=args.proxy, concurrency=args.concurrency, out=args.out,
                               respect_robots=args.robots, sitemap=args.sitemap,
                               same_domain=args.same_domain)
        except BlockDetectedError as e:
            print(f"⛔ {e}", file=sys.stderr)
            print("   已停止采集：宁可不写，也不把封禁页写进数据。", file=sys.stderr)
            print("   处置：冷却/换出口/降频后重试；核查台账 budget --list", file=sys.stderr)
            try:
                _od = Path(args.out or "outputs")
                _od.mkdir(parents=True, exist_ok=True)
                (_od / "blocked.json").write_text(json.dumps(
                    {"blocked": True, "kind": e.kind, "url": e.url, "detail": e.detail,
                     "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                print(f"   证据已写 {_od / 'blocked.json'}", file=sys.stderr)
            except Exception as _we:
                print(f"⚠️ blocked.json 写盘失败（{type(_we).__name__}: {_we}）"
                      "——请以本条与上方 stderr 中的 kind/url/detail 为封禁证据", file=sys.stderr)
            return 5
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            _tot = result.get("total", 0)
            if _tot:
                print(f"✅ 爬取完成: {_tot} 条 | 抓取 {result.get('fetched', 0)} 页 | 错误 {result.get('errors', 0)}")
            else:
                print(f"⚠️ 爬取结束: 0 条 | 抓取 {result.get('fetched', 0)} 页 | 错误 {result.get('errors', 0)}"
                      "（0 结果不假成功，请先诊断再重试）")
            for k, v in result.get("files", {}).items():
                if Path(v).exists():
                    print(f"   {k}: {v}")
        # R32 修复：nodata 曾无条件 exit 0——CI 门禁对全失败爬取亮绿灯
        if result.get("nodata"):
            return 3
        return 0

    if args.cmd == "fetch":
        from .quick import fetch_url, save_result
        # 审查十轮（M）：--out 指向已存在目录曾抛 IsADirectoryError 裸栈
        # （save_result 5 处调用点全部无守卫；--raw 分支有内联检查，其余漏网）
        # ——JSON 已打印到 stdout 后才崩，管道消费方判失败但 stdout 有数据
        if args.out and not _guard_out_is_file(args.out, "fetch"):
            return 1
        actions = None
        if args.actions:
            try:
                actions = json.loads(args.actions)
            except json.JSONDecodeError as e:
                print(f"❌ --actions 不是合法 JSON: {e}", file=sys.stderr)
                return 1
        # 审查十轮（H）：--cdp/--actions 曾不自动启用浏览器模式——--cdp 忘加
        # --browser 时登录态通道静默变成匿名直连（受限页 exit 0 假成功）。
        # 显式 --browser 仍优先；需要浏览器的参数一律自动启用
        _browser = args.browser or bool(args.cdp) or bool(actions) \
            or args.screenshot or bool(args.capture)
        result = fetch_url(args.url, browser=_browser, selector=args.selector,
                           article=args.article, table=args.table, proxy=args.proxy,
                           actions=actions, wait_selector=args.wait, links=args.links,
                           links_allow=args.links_allow, links_deny=args.links_deny,
                           screenshot=args.screenshot, cdp=args.cdp,
                           capture=args.capture or None)
        if result.get("error"):
            print(f"❌ {result['error']}", file=sys.stderr)
            return 1
        if args.capture:
            if result.get("capture_file"):
                # 实战反馈六#3（知乎）：捕获文件存在曾即报成功——实际可能只有
                # 1 条无关请求。条数过少按铁律 3 大声告警
                _cap_lines = 0
                try:
                    with open(result["capture_file"], "rb") as _cf:
                        _cap_lines = sum(1 for _ln in _cf if _ln.strip())
                except OSError:
                    pass
                if _cap_lines < 3:
                    print(f"⚠️ 捕获仅 {_cap_lines} 条记录——目标站接口大概率未触发"
                          "（需滚动/登录/页面交互），此捕获不可用于 capture2config",
                          file=sys.stderr)
                else:
                    print(f"📡 接口捕获已保存: {result['capture_file']}（{_cap_lines} 条）", file=sys.stderr)
            else:
                print(f"⚠️ 捕获未产出: {result.get('capture_error', '未知原因')}", file=sys.stderr)
        if args.json:
            # --json：stdout 只输出纯 JSON（可管道），保存提示走 stderr
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            if args.out:
                fp = save_result(result, args.out, as_json=True)
                print(f"✅ 已保存: {fp}", file=sys.stderr)
            return 0
        if getattr(args, "raw", False) and result.get("text") is not None:
            # OCR R131 反馈 #4：fetch --raw 输出原始 HTML——反爬取证（@font-face、
            # CSS 偏移、加密脚本）需要看原始字节而非 markdown 化结果。
            # 收官十五轮（安全审计 M4）：写盘曾直接 write_text——--out 指向已存在
            # 目录时抛 IsADirectoryError 裸栈，且**抓到的正文既不落盘也不打印**。
            # 改为：写失败只告警，正文照打 stdout（侦察/取证数据永不丢）
            raw_html = result.get("html") or result.get("text", "")
            if args.out:
                _outp = Path(args.out)
                _wrote = False
                if _outp.is_dir():
                    print(f"⚠️ --out 是目录（{_outp}）——请给文件名，如 --out {_outp}/page.html；"
                          "本次改为打印到 stdout", file=sys.stderr)
                else:
                    try:
                        _outp.parent.mkdir(parents=True, exist_ok=True)
                        _outp.write_text(raw_html, encoding="utf-8")
                        print(f"✅ 原始 HTML 已保存: {_outp}（{len(raw_html)} 字符）", file=sys.stderr)
                        _wrote = True
                    except Exception as e:
                        print(f"⚠️ 原始 HTML 落盘失败（{type(e).__name__}: {e}）——改为打印到 stdout",
                              file=sys.stderr)
                if not _wrote:
                    sys.stdout.write(raw_html)
            else:
                sys.stdout.write(raw_html)
            return 0
        # 审查十轮（H）：--table 曾是孤儿参数——quick.fetch_url 抽出了 tables
        # 且 --table 时跳过 markdown 生成，但 content 回退链从不读 tables，
        # 结果把原始 HTML 当"抽取结果"打印/保存（帮助承诺的表格 JSON 不可见）
        if args.table and result.get("tables"):
            content = json.dumps(result["tables"], ensure_ascii=False, indent=1)
        else:
            content = result.get("markdown") or result.get("article") or result.get("selector") or result.get("text", "")
        # 实战反馈六#3（知乎）：200 + 壳页（markdown 0 行）曾照样 ✅——工具说谎
        # 比工具失败更误事。空/极短内容按铁律 3 大声告警并置 nodata 退出码
        # 审查八轮（MEDIUM）：200 字符硬阈值把"内容短但成功"的合法页（示例页
        # example.com 165 字符/单行公告/短 JSON）判成壳页并 exit 3——fetch 是
        # 第〇/二幕侦察入口，agent 会据此无谓升级浏览器/诊断。改为"抽取量 vs
        # 原文体量"判据：原文本身就小（<4KB 或本身就没几个 script）＝真短页照常成功；
        # 原文很大却只抽出几乎零文本＝壳页/抽取失败，仍 exit 3。
        _cl = len(content.strip())
        if _cl < 200:
            # 注意：main() 里 map 分支等处有局部 `import re`——整个函数作用域内 `re`
            # 是局部名，此处直接用会 UnboundLocalError（实测踩中）。改用别名局部导入。
            import re as _re
            _raw_html = result.get("html") or result.get("text") or ""
            _scripts = len(_re.findall(r"<script", _raw_html, _re.I))
            # 收官十五轮（用户复盘）：**先识别 SSR/内联数据页**再谈"壳页"。用户实测
            # 93KB 的 SSR 页数据全在标记里，旧逻辑一律按"壳页→上浏览器+capture"劝
            # （方向错误：SSR 根本不需要浏览器），甚至对中等大小的页打出"原文本身
            # 就是小页面"的误导提示。SSR 特征：原文大 + 脚本少 + 含内联数据标记
            _ssr_like = (len(_raw_html) >= 8000 and _scripts < 4 and bool(_re.search(
                r"__DATA__|__NEXT_DATA__|__NUXT__|__INITIAL_STATE__|__APOLLO_STATE__|"
                r"window\.__|application/(?:ld\+)?json|data-(?:page|props|state)=",
                _raw_html, _re.I)))
            _empty_extract = _cl < 60
            # 收官十五轮（e2e 矩阵实测）：`_cl < 60` 曾无条件判壳页——78B 的中文短页
            # （27 字符正文，正常抓取）被报"大概率是壳页/被拦截"并 exit 3，与早先
            # "原文本身就小＝真短页照常成功"的修复意图相悖（该意图只覆盖了 ≥4KB 分支）。
            # 原文确实很小（100B~4KB 且脚本少）时不算壳页；0B/近空响应仍算（防假成功）
            _small_origin = 0 < len(_raw_html) < 4000 and _scripts < 3
            _shellish = bool(result.get("shell_suspect")) or (
                _empty_extract and not _small_origin) or (
                len(_raw_html) >= 4000 and _scripts >= 3 and _cl < 200)
            if _ssr_like:
                print(f"ℹ️ 抽取正文偏短（{_cl} 字符）但原文 HTML 很大（{len(_raw_html)}B，"
                      f"script×{_scripts}）且含**内联数据**——疑似 SSR/内联 JSON 页："
                      "数据在 HTML 标记里，不是壳页、也不需要浏览器。下一步："
                      "① fetch --raw --out page.html 看原始 HTML 确认数据结构 "
                      "② 用 run --config 把 record.fields 指向数据容器（如 JSON 在 script 里，"
                      "可用 selectors.extract_embedded_json_rows 或 source.type=http_json）"
                      "③ 明细页字段放 detail.extract、清洗放 detail.post_pipeline",
                      file=sys.stderr)
                print(content)
                if args.out:
                    fp = save_result(result, args.out, as_json=False)
                    print(f"\n（内容已保存，供按 SSR 结构重写配置）: {fp}")
                return 3
            if _shellish:
                print(f"⛔ 内容为空或极短（{_cl} 字符；原文 {len(_raw_html)}B / script×{_scripts}）"
                      "——大概率是壳页/渲染失败/被拦截，按铁律 3 走诊断："
                      "1) fetch --browser --capture 看接口 2) cli diagnose 3) 换 L4 登录态",
                      file=sys.stderr)
                # 审查六轮（L6）：告警归告警，正文仍打 stdout（取证/人工判断不丢数据）
                print(content)
                if args.out:
                    fp = save_result(result, args.out, as_json=False)
                    print(f"\n（空内容仍已保存供取证）: {fp}")
                return 3
            print(f"ℹ️ 内容偏短（{_cl} 字符）但原文本身就是小页面——按成功返回"
                  "（若后续抽取为空再按铁律 3 诊断）", file=sys.stderr)
        _deg = " [degraded:{}⚠️非curl_cffi]".format(result["backend"]) if result.get("degraded_backend") else ""
        print(f"# {result['url']}  ({result['status']}, {len(result.get('text',''))}B){_deg}\n")
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
    if not args.task and not args.config:
        # 审查修复（P2）：曾 TypeError 裸栈（Path(None)）
        print("❌ run 需要 --config <v2配置> 或 --task <v3任务包> 之一", file=sys.stderr)
        return 1
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
                              dry_run=args.dry_run, log_file=args.log_file, start_url=args.url,
                              max_requests=getattr(args, "max_requests", None))
        except KeyboardInterrupt:
            print("\n已中断", file=sys.stderr)
            return 130
        except BlockDetectedError as e:
            # 审查修复（P1）：--task 路径曾缺封禁终态处理——裸栈 + exit 1 +
            # 无证据，而 v2 --config 路径有完整 exit 5 契约。补齐：
            print(f"⛔ {e}", file=sys.stderr)
            print("   已停止采集：宁可不写，也不把封禁页写进数据。", file=sys.stderr)
            print("   处置：冷却/换出口/降频后重试；核查台账 budget --list", file=sys.stderr)
            try:
                _cfg = json.loads((tp / "config.json").read_text(encoding="utf-8"))
                _od = Path(str((_cfg.get("output") or {}).get("dir", tp / "out")))
                if not Path(_od).is_absolute():
                    # 审查十轮（M）：引擎 output.dir 按 CWD 解析（engine_v3.py），
                    # 此处曾按任务包目录解析——数据写在 <CWD>/outputs/ 而封禁
                    # 证据写在 tasks/<pkg>/outputs/，自动化按数据目录找证据落空
                    _od = Path.cwd() / _od
                _od.mkdir(parents=True, exist_ok=True)
                (_od / "blocked.json").write_text(json.dumps(
                    {"blocked": True, "kind": e.kind, "url": e.url, "detail": e.detail,
                     "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                print(f"   证据已写 {_od / 'blocked.json'}", file=sys.stderr)
            except Exception as _we:
                print(f"⚠️ blocked.json 写盘失败（{type(_we).__name__}: {_we}）"
                      "——请以本条与上方 stderr 中的 kind/url/detail 为封禁证据", file=sys.stderr)
            return 5
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("stopped") == "max_requests":
            print("🛑 请求预算硬闸触发（--max-requests / anti_bot.max_requests），已按检查点停",
                  file=sys.stderr)
            return 4
        if result.get("nodata"):
            print("⚠️ 0 条记录：nodata.json 已写入输出目录（0 结果不假成功，请先诊断再重试）",
                  file=sys.stderr)
            return 3
        return 0
    cfg_path = Path(args.config)
    try:
        config = load_config(cfg_path)
    except ConfigError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    if args.url:
        config["source"]["url"] = args.url
    if getattr(args, "replay", False):
        # R20 开发模式：注入 anti_bot.replay（客户端只读缓存、不发网络）
        config.setdefault("anti_bot", {})["replay"] = True
        print("🔁 replay 开发模式：只读本地响应缓存、不发网络（未命中将结构化失败）",
              file=sys.stderr)
    overrides = {}
    for v in args.var:
        if "=" in v:
            k, val = v.split("=", 1)
            overrides[k] = val
    try:
        result = run_config(config, overrides=overrides, base_dir=cfg_path.resolve().parent,
                            resume=args.resume, limit=args.limit, dry_run=args.dry_run,
                            log_file=args.log_file, max_requests=getattr(args, "max_requests", None))
    except KeyboardInterrupt:
        print("\n已中断，检查点已保存，可用 --resume 续跑", file=sys.stderr)
        return 130
    except MaxRequestsExceeded as e:
        # 注意必须显式捕获：它继承 BaseException（穿透客户端重试网），except Exception 接不住
        print(f"🛑 请求预算硬闸：{e}", file=sys.stderr)
        print("   已抓数据已按迭代落盘；如需继续请调大 --max-requests", file=sys.stderr)
        return 4
    except BlockDetectedError as e:
        # 裁判文书网战训：封禁页命中必须硬停机 + 落证据（exit 5）
        print(f"⛔ {e}", file=sys.stderr)
        print("   已停止采集：宁可不写，也不把封禁页写进数据。"
              "失败迭代已抓的内存记录已丢弃（此前已完成迭代的导出文件保留）。",
              file=sys.stderr)
        print("   处置：冷却/换出口/降频后重试；核查台账 budget --list", file=sys.stderr)
        try:
            _cfg = load_config(cfg_path)  # pyflakes：局部 as _lc 导入未用——模块级已导入
            _od = Path(os.path.expandvars(os.path.expanduser(
                str((_cfg.get("output") or {}).get("dir", "outputs")))))
            _od.mkdir(parents=True, exist_ok=True)
            (_od / "blocked.json").write_text(json.dumps(
                {"blocked": True, "kind": e.kind, "url": e.url, "detail": e.detail,
                 "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=1),
                encoding="utf-8")
            print(f"   证据已写 {_od / 'blocked.json'}", file=sys.stderr)
        except Exception as _we:
            # 审查修复 P1：证据写盘失败曾静默——exit 5 却找不到 blocked.json
            print(f"⚠️ blocked.json 写盘失败（{type(_we).__name__}: {_we}）"
                  "——请以本条与上方 stderr 中的 kind/url/detail 为封禁证据", file=sys.stderr)
        return 5
    except RateLimitedError as e:
        # 审查修复 P1：9 种拦截类在 _request 先抛 RateLimitedError——曾裸栈崩
        # exit 1 无证据。统一按"站点在推回"处理：落证据 + exit 5
        print(f"⛔ 反爬拦截/限流：{e}", file=sys.stderr)
        print("   已停止采集。处置：冷却/换出口/降频后重试（详见 anti-block-playbook）。",
              file=sys.stderr)
        try:
            _cfg = load_config(cfg_path)  # pyflakes：局部 as _lc 导入未用——模块级已导入
            _od = Path(os.path.expandvars(os.path.expanduser(
                str((_cfg.get("output") or {}).get("dir", "outputs")))))
            _od.mkdir(parents=True, exist_ok=True)
            (_od / "blocked.json").write_text(json.dumps(
                {"blocked": True, "kind": "rate_limited", "url": e.url,
                 "detail": e.detail, "status": e.status,
                 "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=1),
                encoding="utf-8")
            print(f"   证据已写 {_od / 'blocked.json'}", file=sys.stderr)
        except Exception as _we:
            print(f"⚠️ blocked.json 写盘失败（{type(_we).__name__}: {_we}）"
                  "——请以 stderr 为封禁证据", file=sys.stderr)
        return 5
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("nodata"):
        print("⚠️ 0 条记录：nodata.json 已写入输出目录（0 结果不假成功，请先诊断再重试）", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
