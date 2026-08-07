#!/usr/bin/env python3
"""🤖 自动层：一句话任务 → AI 自动生成配置 → 自动运行 → 失败自修复。

原理：
  1. 用大模型（默认千问 Qwen）把用户的中文任务转成 v3 任务包 config.json
  2. 引擎自动运行
  3. 若 0 条或报错：把日志喂回大模型，自动修正配置再跑（最多 rounds 轮）

用法:
  CLI:  python3 -m universal_scraper.cli auto "抓取 https://quotes.toscrape.com 的名言、作者和标签，翻 2 页"
  API:  POST /api/auto  {"description": "..."}
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent

SYSTEM_PROMPT = """你是万能爬虫工具的"任务配置生成器"。根据用户的中文任务，输出**一个完整的 v3 任务包 config.json**（严格 JSON，不要任何解释、不要 markdown 代码块）。

v3 任务包 config.json 结构（字段含义）：
{
  "name": "任务名(英文小写)",
  "vars": {"可选的变量": "值"},
  "start_urls": ["入口网址(必须 http/https)"],
  "queue": {"max_depth": 3, "max_requests": 100, "max_concurrency": 2},
  "source": {
    "type": "http",              // http=普通请求；browser=浏览器渲染(JS页面/需要登录后DOM)
    "query": {"参数": "值"},       // 可选固定 URL 参数
    "headers": {"可选": "自定义请求头"},
    "actions": [{"type":"click","selector":"#more"}]   // 仅 browser：点击/输入/滚动等
  },
  "rules": [{"match":"contains","pattern":"/list","parser":"list"}],
  "parsers": {
    "list": {
      "type": "html",            // html=网页；json=JSON接口；json_paged=JSON分页；table=表格；article=正文
      "row_css": "列表行选择器",   // html 列表必填
      "fields": {"字段名": {"css":"选择器"} 或 {"from":"JSON路径"}},
      "extract_links": {"allow":"下一页/详情链接正则", "same_domain":true}   // 需要翻页时必填
    }
  },
  "pipelines": [
    {"type":"filter","field":"字段","op":"non_empty"},
    {"type":"filter","field":"时间戳字段","op":"between","min":<unix秒>,"max":<unix秒>},  // 用户提到日期范围时用
    {"type":"dedup","key":"主键"}
  ],
  "storage": {"type":"jsonl","name":"任务名"},
  "output": {"dir":"outputs","base_name":"任务名"},
  "anti_bot": {"min_interval":0.5,"max_retries":2}
}

规则：
- 日期范围：把用户说的"2025年1月1日到1月3日"转成 **北京时间** 的 Unix 秒，用 between 过滤（min=2025-01-01 00:00 CST，max=2025-01-04 00:00 CST）。
- 翻页：HTML 用 extract_links，allow 建议写**锚定路径正则**（如 "^/page/\\d+/$"），引擎会按 URL 路径匹配，防止误吃 /tag/xxx/page/1/ 这类同构 URL；JSON 分页用 type=json_paged（records_path/strategy=page_param/page_param/page_size/max_pages/fields）。
- 选择器要根据网站常见结构推断（.item/.list/table tr 等），宁可宽一点；字段 CSS 可直接写 ".text" 或 ".text::text"，两种都支持。
- 需要登录/验证码的网站：source.type 用 browser，并在 anti_bot 里加 "captcha":{"strategy":"auto"}；如果用户没提供登录方式，仍输出配置并在 config 里保留说明字段 "requires_login": true。
- 只输出 JSON 对象本身。"""


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("AI 未返回 JSON: " + raw[:200])
    return json.loads(m.group(0))


def _llm_chat(messages: List[Dict[str, str]]) -> str:
    from .llm import LLMClient
    client = LLMClient()
    return client.chat(messages, temperature=0.2)


def _validate_and_fix(cfg: dict) -> dict:
    """规范化 AI 输出：把不认识的写法映射为引擎支持的写法，再校验。"""
    from .config import validate_task
    cfg.setdefault("name", "auto_task")
    cfg.setdefault("start_urls", [])
    cfg.setdefault("queue", {"max_depth": 3, "max_requests": 100, "max_concurrency": 2})
    cfg.setdefault("source", {"type": "http"})
    cfg.setdefault("storage", {"type": "jsonl", "name": cfg["name"]})
    cfg.setdefault("output", {"dir": "outputs", "base_name": cfg["name"]})
    cfg.setdefault("anti_bot", {"min_interval": 0.5, "max_retries": 2})
    cfg.setdefault("pipelines", [])
    if not cfg.get("rules"):
        cfg["rules"] = [{"match": "contains", "pattern": "/", "parser": "default"}]
    cfg.setdefault("parsers", {})
    # source.type 规范化
    st = cfg["source"].get("type", "http")
    if st not in ("http", "browser", "bridge"):
        cfg["source"]["type"] = "http"
    # storage.type 规范化
    st2 = cfg["storage"].get("type", "jsonl")
    if st2 not in ("jsonl", "csv", "sqlite", "multi"):
        cfg["storage"]["type"] = "jsonl"
    # 规则规范化：match 只支持 regex/contains/startswith
    for r in cfg["rules"]:
        if r.get("match") not in ("regex", "contains", "startswith"):
            r["match"] = "contains"
        if not r.get("pattern"):
            r["pattern"] = "/"
        if not r.get("parser"):
            r["parser"] = "default"
    # 分页/列表 allow 自动锚定：非锚定的 page 类正则改为 ^/...$（按路径匹配），
    # 避免 /page/\\d+/ 把 /tag/xxx/page/1/ 这类同构 URL 全部入队导致队列爆炸
    for pcfg in cfg.get("parsers", {}).values():
        if not isinstance(pcfg, dict):
            continue
        lk = pcfg.get("extract_links") or {}
        allow = lk.get("allow")
        if isinstance(allow, str) and re.search(r"(page|p=)", allow, re.I) \
                and re.search(r"\\d|\[0-9\]", allow):
            allow = allow.strip()
            if not allow.startswith("^"):
                # 非锚定 → 锚定为路径模式（^/...），防止 /tag/xxx/page/1/ 同构 URL 入队
                if allow.startswith("/"):
                    lk["allow"] = f"^{allow.rstrip('/')}/?$"
                else:
                    lk["allow"] = f"^/{allow.lstrip('/')}"
            elif not allow.startswith(("^/", "^http", "^https", r"^\w+://")):
                # 已锚定但缺路径开头 /（如 ^catalogue/page-\\d+.html$ → ^/catalogue/...）
                lk["allow"] = "^/" + allow[1:]
            pcfg["extract_links"] = lk
    # 默认 parser
    cfg["parsers"].setdefault("default", {"type": "html", "fields": {}})
    # records_path 规范化：JSONPath 的 $ 根 / $.a.b → 引擎支持的 a.b / 空（顶层数组自动识别）
    for pcfg in cfg["parsers"].values():
        if not isinstance(pcfg, dict):
            continue
        rp = pcfg.get("records_path")
        if isinstance(rp, str):
            rp = rp.strip()
            if rp in ("$", "$[]", "$."):
                pcfg["records_path"] = ""
            elif rp.startswith("$."):
                pcfg["records_path"] = rp[2:]
            elif rp.startswith("$["):
                pcfg["records_path"] = ""
    # start_urls 过滤非 http(s)
    cfg["start_urls"] = [u for u in cfg.get("start_urls", []) if str(u).startswith(("http://", "https://"))]
    if not cfg["start_urls"]:
        raise ValueError("AI 未给出有效入口网址")

    # 路由修正：rules 若指向空解析器，自动改指"最有内容"的解析器（避免抓到一堆空壳）
    def parser_richness(pc):
        if not isinstance(pc, dict):
            return 0
        return len(pc.get("fields") or {}) + (1 if pc.get("row_css") or pc.get("row_xpath") else 0) \
               + (1 if pc.get("type") in ("json", "json_paged", "table", "article") else 0)

    non_empty = [n for n, pc in cfg.get("parsers", {}).items() if parser_richness(pc) > 0]
    if non_empty:
        best = max(non_empty, key=lambda n: parser_richness(cfg["parsers"][n]))
        for r in cfg["rules"]:
            pn = r.get("parser") or "default"
            pc = cfg.get("parsers", {}).get(pn, {})
            if parser_richness(pc) == 0 and pn in ("default", "main", "list") and best:
                r["parser"] = best
        # 默认解析器为空且有内容解析器时：让默认解析器也指向 best，未匹配 URL 也能解析
        dp = cfg.get("parsers", {}).get("default", {})
        if parser_richness(dp) == 0 and best:
            cfg["parsers"]["default"] = dict(cfg["parsers"][best])  # 保留 extract_links，未匹配 URL 也能翻页

    # 兜底路由：若入口 URL 匹配不到任何规则，追加 catch-all（放在最后，特定规则优先）
    if non_empty:
        def _rule_hits(r, u):
            m = r.get("match", "contains")
            pat = r.get("pattern", "/")
            if m == "regex":
                try:
                    return re.search(pat, u) is not None
                except re.error:
                    return False
            if m == "startswith":
                return u.startswith(pat)
            return pat in u

        if not any(_rule_hits(r, u) for r in cfg["rules"] for u in cfg["start_urls"]):
            if not any(r.get("pattern", "/") == "/" and r.get("match") == "contains" for r in cfg["rules"]):
                cfg["rules"].append({"match": "contains", "pattern": "/", "parser": best})

    validate_task(cfg)
    return cfg


def _page_context(url: str, max_chars: int = 2500) -> str:
    """抓入口页 → 结构摘要/Markdown 摘要（对标 scrape-mcp/cortex-scout：给 LLM 的是省 token 的文本，
    不是整页 HTML）。失败返回空字符串。"""
    try:
        from .structure import build_structure_summary
        s = build_structure_summary(url)
        if s:
            return s[:max_chars]
    except Exception:
        pass
    try:
        from .quick import fetch_url
        r = fetch_url(url)
        if r.get("error"):
            return ""
        md = r.get("markdown") or r.get("article") or r.get("text") or ""
        if not md:
            return ""
        if len(md) > max_chars:
            md = md[:max_chars] + f"\n...(截断，共 {len(md)} 字符)"
        return f"入口页 Markdown 摘要（来自真实抓取）：\n{md}"
    except Exception:
        return ""


def auto_task(description: str, limit: Optional[int] = None, rounds: int = 2,
              log_cb=None) -> Dict[str, Any]:
    """执行一次自动任务。返回 {config, result, log, sample, files}。"""
    if limit is not None:
        limit = int(limit) or None

    def log(msg):
        if log_cb:
            log_cb(msg)

    h = hashlib.md5(description.encode()).hexdigest()[:10]
    name = f"auto_{h}"
    task_dir = ROOT / "tasks" / name

    # 第 1 轮：生成配置（先做入口页结构探测，把结构摘要喂给 AI，而非整页源码）
    from .structure import build_structure_summary, extract_urls
    summary = None
    urls = extract_urls(description)
    if urls:
        log(f"🔍 正在探测入口页结构: {urls[0]}")
        summary = build_structure_summary(urls[0])
        if summary:
            log("✅ 结构摘要已生成（省 token、选择器更准）")
        else:
            summary = _page_context(urls[0])
            if summary:
                log("✅ 已抓取入口页 Markdown 摘要（省 token、选择器更准）")
    prompt = f"任务：{description}\n请输出完整 config.json。"
    if summary:
        prompt += f"\n\n入口页结构摘要（来自真实抓取，据此推断 row_css/fields/extract_links 会更准）：\n{summary}"
    if re.search(r"翻|分页|page|更多页", description, re.I):
        prompt += "\n\n【硬性要求】任务明确要求翻页：HTML 解析器必须带 extract_links（allow 写锚定正则，如 ^/page/\\d+/$），JSON 必须用 json_paged，否则配置不合格。"
    prompt += "\n\n只输出完整 config.json。"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    log("🤖 AI 正在理解任务并生成配置...")
    cfg = _extract_json(_llm_chat(messages))
    cfg = _validate_and_fix(cfg)
    cfg["name"] = name
    cfg["storage"]["name"] = name
    cfg["output"]["base_name"] = name
    if limit:
        cfg.setdefault("queue", {})["max_requests"] = int(limit) + 5
    log(f"✅ 配置已生成（source={cfg.get('source', {}).get('type')}）")

    last_result = {}
    last_log = ""
    for round_i in range(1, rounds + 1):
        # 写任务包
        (task_dir / "modules").mkdir(parents=True, exist_ok=True)
        (task_dir / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

        # 运行
        log(f"▶️ 第 {round_i} 轮运行...")
        from .engine_v3 import run_task
        from .log import Logger
        log_file = ROOT / "outputs" / f".run_{name}.log"
        try:
            result = run_task(task_dir, limit=limit, log_file=log_file)
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
        except Exception as e:
            result = {"total": 0, "fetched": 0, "errors": -1, "error": str(e)}
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else str(e)

        items_path = ROOT / "outputs" / "items" / f"{name}.jsonl"
        sample = []
        if items_path.exists():
            for line in items_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    try:
                        sample.append(json.loads(line))
                    except Exception:
                        pass
                if len(sample) >= 5:
                    break

        # 成功判定：必须有"真实字段"（至少一个非 _url/_parser/_ts 的字段非空），
        # 防止 AI 生成空 fields 导致 total>0 但全是空壳
        META = ("_url", "_parser", "_ts", "_id")
        real = [it for it in sample if any(
            str(it.get(k) or "").strip() for k in it if k not in META)]
        if result.get("total", 0) > 0 and real:
            log(f"✅ 第 {round_i} 轮成功：{result.get('total')} 条（抽样 {len(real)} 条有真实字段）")
            break
        if result.get("total", 0) > 0 and not real:
            log(f"⚠️ 第 {round_i} 轮 total={result.get('total')} 但全是空壳字段，视为失败，进入自修复...")

        if round_i < rounds:
            log(f"⚠️ 第 {round_i} 轮 0 条/报错，AI 正在自修复...")
            fix_messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"任务：{description}\n"
                    f"上次配置：{json.dumps(cfg, ensure_ascii=False)}\n"
                    f"运行结果：{json.dumps(result, ensure_ascii=False)}\n"
                    f"{_page_context(cfg.get('start_urls', [''])[0]) if cfg.get('start_urls') else ''}\n"
                    f"运行日志（末尾）：\n{last_log[-2000:]}\n\n"
                    f"请修正配置（选择器/网址/解析方式等），只输出修正后的完整 config.json。"
                )},
            ]
            try:
                cfg = _extract_json(_llm_chat(fix_messages))
                cfg = _validate_and_fix(cfg)
                cfg["name"] = name
                cfg["storage"]["name"] = name
                cfg["output"]["base_name"] = name
            except Exception as e:
                log(f"❌ 自修复生成失败: {e}")
                break

    # 导出文件清单
    files = {}
    for ext in ("json", "csv", "xlsx"):
        fp = ROOT / "outputs" / f"{name}.{ext}"
        if fp.exists():
            files[ext] = str(fp.relative_to(ROOT))
    return {
        "name": name,
        "config": cfg,
        "result": last_result,
        "log": last_log[-3000:],
        "sample": sample,
        "files": files,
    }


def run_auto_cli(description: str, limit: Optional[int] = None) -> Dict[str, Any]:
    lines = []

    def log(msg):
        lines.append(msg)
        print(msg, flush=True)

    try:
        out = auto_task(description, limit=limit, log_cb=log)
        out["messages"] = lines
        return out
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "messages": lines}
