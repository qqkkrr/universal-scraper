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
import threading
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
  "detail": {                       // 列表页缺字段时必配（如发布日期/正文/价格在详情页）
    "enabled": true,
    "url_field": "link",            // 列表记录里详情 URL 的字段名
    "url_transform": [{"prefix": "https://www.zhipin.com"}],  // 相对路径补全域名（必须写对）
    "extract": [{"name": "publish_time", "type": "css_text", "selector": ".job-banner .time::text"}],
    "filters": [                    // 详情合并后再过滤（如按发布日期）
      {"type": "parse_date", "field": "publish_time", "out": "ts"},
      {"type": "filter", "field": "ts", "op": "between", "min": <unix秒>, "max": <unix秒>}
    ],
    "concurrency": 2,
    "interval": 0.5
  },
  "storage": {"type":"jsonl","name":"任务名"},
  "output": {"dir":"outputs","base_name":"任务名"},
  "anti_bot": {"min_interval":0.5,"max_retries":2}
}

规则：
- 日期范围：把用户说的"2025年1月1日到1月3日"转成 **北京时间** 的 Unix 秒，用 between 过滤（min=2025-01-01 00:00 CST，max=2025-01-04 00:00 CST）。
- 翻页：HTML 用 extract_links，allow 建议写**锚定路径正则**（如 "^/page/\\d+/$"），引擎会按 URL 路径匹配，防止误吃 /tag/xxx/page/1/ 这类同构 URL；JSON 分页用 type=json_paged（records_path/strategy=page_param/page_param/page_size/max_pages/fields）。
- 选择器要根据网站常见结构推断（.item/.list/table tr 等），宁可宽一点；字段 CSS 可直接写 ".text" 或 ".text::text"，两种都支持。
- **字段提取规范（必须遵守）**：标题/名称优先选行内第一个带语义的节点（span/heading/首个链接），**禁止**直接用裸 `a::text` 或裸 `a::attr(href)`——列表行常有多个链接（详情+附件），裸 a 会把它们拼成 "d\nf"、"/a /b"。取链接时用首个详情链接：`a:first-of-type::text`、`a[href*='/detail']::text`；href 用 `.u::attr(href)` 或 `a:first-of-type::attr(href)`。只有用户明确要"所有链接"时才用 multiple。
- **列表缺字段用 detail（必须遵守）**：如果用户要的字段（发布日期/时间/正文/价格/评分等）在列表页没有、只在详情页有（如招聘/电商/资讯站的发布时间、商品详情），必须配 `detail`：`url_field` 用列表记录里存详情 URL 的字段（如 link），`url_transform.prefix` 把相对路径补成完整域名，`extract` 用 css_text/css_attr 抓详情页字段，`filters` 做合并后过滤（日期类先用 parse_date 转 unix 秒再 between）。引擎会自动逐个抓详情页并合并回列表记录。
- 需要登录的网站（大众点评/小红书/微博/淘宝/京东/知乎等）：source.type 用 browser，并加 "headless": false（弹出真实浏览器供人工登录）与 "login":{"enabled":true,"url":"入口页","wait_selector":"登录成功后页面上才会出现的元素选择器（如 .user-info、.avatar、用户名节点）"}。工具会在首次运行时弹出浏览器让用户登录一次，自动保存登录态，之后自动复用。
- **京东 URL 硬知识（必须遵守）**：京东店铺页真实格式是 https://mall.jd.com/index-<店铺数字ID>.html；商品页是 https://item.jd.com/<sku数字ID>.html；**绝对不要**把店铺名猜成 "<店铺名>sp.jd.com/list.html"（那是假地址，会 404）。用户只给店铺名没给链接时，start_urls 可以先用京东搜索或直接用已知商品链接，并在任务说明里注明"需先找到店铺/商品真实 URL"。京东搜索页(www.jd.com)、商品页、评论接口(club.jd.com)全都被强风控：公开 HTTP 接口已失效，必须 source.type=browser + 真实扫码登录。京东登录硬校验："login":{"enabled":true,"url":"https://www.jd.com/","wait_selector":".nickname","require_cookie":"pt_key|pt_pin"}——工具会检查登录 cookie 是否真的出现，没有 pt_key/pt_pin 就不会放行，避免"假登录通过"。
- 验证码/整页验证（大众点评/美团等会跳到验证中心）：source.type=browser 并加 "headless": false 与
  "verify":{"enabled":true,"markers":["verify.meituan.com","验证中心","spiderindefence","安全验证","滑动验证","访问过于频繁"],
  "success_selector":"<列表行选择器，如 div[data-shop-id]>","max_wait_ms":600000}；
  工具会弹出真实浏览器，用户手动过滑块/点选后自动继续并保存会话，之后复用。
- 大众点评/美团这类：过完滑块**还会强制登录**（扫码/账号）。所以 verify 和 login 两个都要配：
  "verify":{...上面...} + "login":{"enabled":true,"url":"https://www.dianping.com/","wait_selector":"#J-userinfo a, .user-info a, a[href*='/member/'], .nav-user"}；
  用户在弹窗里：先过滑块，再扫码/账号登录；工具自动继续并保存会话。
- **大众点评列表页如被风控需要住宅代理**：只有用户明确提供了真实代理时才在 anti_bot.proxy 里填写；**不要写 "host:port" 之类的示例占位符**（会导致请求报错）。用户没给代理就不配置 proxy。
- 图片验证码：anti_bot 加 "captcha":{"strategy":"auto"}（自动识别，失败则人工兜底）。
- **CDP 直连真实浏览器（强风控站的王炸）**：如果用户说"已登录/用我自己的浏览器"，source.type 用 browser 并加 "cdp":"http://127.0.0.1:9222"（用户需先开 Chrome：退出 Chrome 后执行 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222 并登录目标站）。CDP 模式附着真实浏览器=真实指纹+真实登录态，京东/知乎/微博/小红书/抖音这类站最稳；不要配 login（用户浏览器已登录）。
- Cloudflare/Turnstile 挑战：工具会自动尝试点击"我不是机器人"复选框并等待 cf_clearance，仍失败才转人工；无需额外配置。
- **强风控 SPA（小红书/抖音/知乎/微博等，数据全靠加密接口）**：source.type 用 browser + 登录/CDP，并加 "record_from":"capture_all" 与 "capture_all":true——工具会**自动捕获页面上所有 JSON 接口响应**（不用预先知道接口名），任务结束后记录在 {_api_url, data}，再由 LLM/解析器挑字段。比硬逆向签名省事得多。
- 如果用户已提供登录 Cookie：source.type 用 http，source.headers 加 "Cookie": "<用户提供的Cookie>"，
  并加 rules/parsers 解析 SSR 页面（如大众点评搜索页 .shop-list li），不要用 browser（Cookie 直抓更快更稳）。
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
    # LLM 可能一次性生成几十个入口：只保留前 10 个（防队列爆炸）
    if isinstance(cfg.get("start_urls"), list) and len(cfg["start_urls"]) > 10:
        cfg["start_urls"] = cfg["start_urls"][:10]
    cfg.setdefault("queue", {"max_depth": 3, "max_requests": 100, "max_concurrency": 2})
    cfg.setdefault("source", {"type": "http"})
    cfg.setdefault("storage", {"type": "jsonl", "name": cfg["name"]})
    cfg.setdefault("output", {"dir": "outputs", "base_name": cfg["name"]})
    cfg.setdefault("anti_bot", {"min_interval": 0.5, "max_retries": 2})
    cfg.setdefault("pipelines", [])
    if not cfg.get("rules"):
        cfg["rules"] = [{"match": "contains", "pattern": "/", "parser": "default"}]
    # 浏览器任务默认滚动（懒加载站不滚动=0条）：未显式配置时给 4 次
    if (cfg.get("source") or {}).get("type") == "browser":
        _src = cfg["source"]
        _src.setdefault("scroll_count", 4)
        _src.setdefault("scroll_wait_ms", 800)
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


_PROBE_LOCK = threading.Lock()  # probe 缓存读写锁（多任务并发写同一缓存文件）


_AUTH_HINTS = ("验证码", "滑动验证", "安全验证", "人机验证", "访问过于频繁",
              "验证中心", "spiderindefence", "verify.meituan.com",
              "请登录", "登录后", "请输入手机号", "微信扫码登录", "app 扫码登录")


def dom_class_stats(html: str, top: int = 12, min_n: int = 3) -> list:
    """统计页面里出现次数最多的 class（真实 DOM 高频类名），喂给 LLM 写对选择器。"""
    from collections import Counter
    try:
        from lxml import html as _lh
        doc = _lh.fromstring(html)
    except Exception:
        return []
    cnt = Counter()
    for el in doc.iter():
        cls = (el.get("class") or "").strip()
        if cls:
            for c in cls.split():
                cnt[c] += 1
    return [f"{c}×{n}" for c, n in cnt.most_common(top) if n >= min_n]


def _class_stats_text(html: str) -> str:
    st = dom_class_stats(html)
    if st:
        return "\n页面实际高频 class（写选择器时直接用这些）：" + ", ".join(st)
    return ""


def _probe_summary(url: str, max_chars: int = 2500) -> str:
    """探测入口页结构摘要（8s 短超时 + 磁盘缓存 1 小时，大幅提速重复任务）。"""
    import os as _os
    import time as _t
    import urllib.request
    now = _t.time()
    cache_file = ROOT / "outputs" / ".probe_cache.json"
    data = {}
    try:
        with _PROBE_LOCK:
            if cache_file.exists():
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if data.get(url) and now - data[url][1] < 3600:
                    return data[url][0]
    except Exception:
        data = {}
    summary = ""
    timeout = int(_os.environ.get("US_PROBE_TIMEOUT", "8"))
    try:
        from .structure import build_structure_summary
        summary = build_structure_summary(url) or ""
    except Exception:
        summary = ""
    if not summary:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            # 显式直连（绕过 Clash 系统代理，避免探测被挂起）
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            raw = opener.open(req, timeout=timeout).read(300000).decode("utf-8", "ignore")
            from .extractors import html_to_markdown, extract_links_markdown
            md = html_to_markdown(raw, base_url=url, max_chars=max_chars)
            links = extract_links_markdown(raw, base_url=url, max_links=30)
            summary = md
            if links:
                summary += "\n\n页面链接：\n" + "\n".join(links)
            _cst = _class_stats_text(raw.decode("utf-8", "ignore"))
            if _cst:
                summary += _cst
        except Exception:
            summary = ""
    summary = (summary or "").strip()[:max_chars]
    if summary:
        try:
            with _PROBE_LOCK:
                data[url] = [summary, now]
                if len(data) > 200:
                    data = dict(list(data.items())[-100:])
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return summary


def _font_obfuscation_hint(sample) -> str:
    """检测字体反爬：字段值含私有区字符(\ue000-\uf8ff)说明数字/文字被自定义字体混淆。"""
    if not sample:
        return ""
    hits = []
    for it in sample:
        for k, v in (it or {}).items():
            if k.startswith("_"):
                continue
            if any("\ue000" <= ch <= "\uf8ff" for ch in str(v or "")):
                if k not in hits:
                    hits.append(k)
    if hits:
        return "｜⚠️ 字段 " + "、".join(hits) + " 疑似字体反爬加密（数字/文字被自定义字体混淆，需解码字体映射才能还原）"
    return ""


def _rendered_class_hint(task_dir) -> str:
    """自修复时把引擎保存的渲染页高频 class 喂给 LLM（登录/JS 页裸抓拿不到真实 DOM）。"""
    try:
        lp = Path(task_dir) / "last_page.html"
        if lp.exists() and lp.stat().st_size > 2000:
            return _class_stats_text(lp.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        pass
    return ""


_KEY_DATE_WORDS = ("日期", "时间", "发布", "更新", "date", "time", "publish", "上线", "创建")


def _missing_key_field(description: str, sample: list) -> str:
    """检测用户任务里的关键字段是否缺失（如日期/时间类）：缺失时返回字段说明，用于触发自修复。"""
    if not description or not sample:
        return ""
    low = description.lower()
    if not any(w in low for w in _KEY_DATE_WORDS):
        return ""
    time_fields = [k for k in set(k for it in sample for k in it)
                   if any(w in k.lower() for w in ("date", "time", "publish", "pub", "时间", "日期", "更新"))]
    if not time_fields:
        return "发布日期/时间"
    if all(not str(it.get(k) or "").strip() for it in sample for k in time_fields):
        return "、".join(sorted(time_fields)[:3])
    return ""


def _diagnose_failure(urls, result, log) -> str:
    """失败原因诊断（说人话、快）：404 / 登录验证码 / 字体反爬 / 选择器不匹配。"""
    import urllib.request
    reasons = []
    if result.get("errors", 0) > 0:
        reasons.append(f"请求错误 {result['errors']} 次")
    if result.get("error"):
        reasons.append(str(result["error"])[:120])
    for u in (urls or [])[:1]:
        if not str(u).startswith("http"):
            continue
        st, raw = 0, ""
        try:
            req = urllib.request.Request(str(u), headers={"User-Agent": "Mozilla/5.0"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                resp = opener.open(req, timeout=8)
                st = resp.status or 0
                raw = resp.read(6000).decode("utf-8", "ignore")
            except urllib.error.HTTPError as e:
                st = e.code or 0
        except Exception:
            pass
        if st == 404:
            reasons.append("入口网址不存在（HTTP 404，网站可能改版或网址是 AI 猜的）")
        elif st >= 400:
            reasons.append(f"入口网址被拦截（HTTP {st}，多为反爬拒绝或需要登录）")
        head = raw[:3000]
        hit = next((k for k in _AUTH_HINTS if k in head), None)
        if hit:
            reasons.append(
                f"页面出现「{hit}」→ 网站要求登录/人工验证（滑块/点选）。"
                "解决：重跑时工具会弹出浏览器，你手动登录/过验证一次，会话会自动保存并复用（无需写代码）")
        if "@font-face" in raw or "woff" in raw.lower():
            reasons.append("页面疑似使用字体反爬（数字/文字被自定义字体混淆，需解码字体映射）")
    if not reasons:
        reasons.append("选择器未匹配到内容（可能页面结构变化，或需要浏览器渲染/登录）")
    return "；".join(dict.fromkeys(reasons))


def _page_context(url: str, max_chars: int = 2500) -> str:
    """抓入口页 → 结构摘要/Markdown 摘要（省 token、带缓存，对标 scrape-mcp/cortex-scout）。"""
    s = _probe_summary(url, max_chars=max_chars)
    if s:
        return f"入口页结构摘要（来自真实抓取）：\n{s}"
    return ""


def _llm_fallback_extract(description: str, cfg: dict, log,
                         rendered_html: Optional[str] = None,
                         rendered_url: str = "") -> dict:
    """CSS/规则解析失败时，用 LLM 直接从页面 Markdown 抽取与任务匹配的条目（ScrapeGraphAI 路线）。
    rendered_html：引擎在浏览器抓取后保存的"渲染完成"页面（登录/JS 页必须用它，裸 HTML 会拿到验证页）。"""
    import hashlib
    urls = cfg.get("start_urls") or []
    if not urls:
        return {"items": [], "total": 0, "files": {}}
    url = urls[0]
    raw = ""
    if rendered_html:
        raw = rendered_html
    else:
        import urllib.request
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=8).read(200000).decode("utf-8", "ignore")
        except Exception:
            return {"items": [], "total": 0, "files": {}}
    head = raw[:3000]
    if any(k in head for k in _AUTH_HINTS):
        log("⛔ 页面被登录/验证码拦截，LLM 兜底跳过（先解决登录）")
        return {"items": [], "total": 0, "files": {}}
    from .extractors import html_to_markdown
    md = html_to_markdown(raw, base_url=url, max_chars=8000)
    if len(md.strip()) < 80:
        return {"items": [], "total": 0, "files": {}}
    log(f"📄 页面有内容（{len(md)} 字符），正在让 LLM 直接抽取...")
    prompt = (
        f"任务：{description}\n"
        f"以下是抓取的页面内容（Markdown）。请提取与任务相关的所有条目，输出 JSON 数组，"
        f"每个对象用贴合任务的中文字段名（如 标题/链接/价格/作者/时间/正文 等），链接保持原始 URL。"
        f"如果页面上没有任何与任务相关的条目，输出 []。只输出 JSON 数组。\n\n内容：\n{md}"
    )
    try:
        raw_out = _llm_chat([
            {"role": "system", "content": "你是数据抽取引擎，只输出合法 JSON 数组。"},
            {"role": "user", "content": prompt},
        ])
        raw_out = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_out.strip())
        try:
            arr = json.loads(raw_out)
        except json.JSONDecodeError:
            m = re.search(r"\[.*\]", raw_out, re.S)
            arr = json.loads(m.group(0)) if m else []
        if not isinstance(arr, list):
            arr = []
    except Exception as e:
        log(f"❌ LLM 兜底抽取失败: {e}")
        return {"items": [], "total": 0, "files": {}}
    items = [it for it in arr if isinstance(it, dict)
             and any(str(v or "").strip() for v in it.values())]
    if not items:
        return {"items": [], "total": 0, "files": {}}
    for it in items:
        it.setdefault("_url", url)
        it.setdefault("_parser", "llm_fallback")
    name = f"auto_{hashlib.md5(description.encode()).hexdigest()[:10]}"
    items_dir = ROOT / "outputs" / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    (items_dir / f"{name}.jsonl").write_text(
        "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items), encoding="utf-8")
    fp = ROOT / "outputs" / f"{name}.json"
    fp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✅ LLM 兜底抽取成功：{len(items)} 条")
    return {"items": items, "total": len(items), "files": {"json": f"outputs/{name}.json"}}


def _valid_proxy(p: Optional[str]) -> bool:
    """代理格式校验：http(s)://[user:pass@]ip:port，host 不能是占位符，port 必须是数字。"""
    if not p:
        return False
    import re as _re
    m = _re.match(r"^(https?)://([^/@:]+(:[^/@:]+)?@)?([^/:]+):(\d+)$", p)
    if not m:
        return False
    host = m.group(4)
    if host in ("host", "localhost", "example.com", "proxy"):
        return False
    return True


def _build_config(description: str, proxy: Optional[str] = None,
                    cookie: Optional[str] = None, log=None) -> tuple:
    """生成任务配置（探测 + LLM + 校验 + 注入），返回 (cfg, name, task_dir)。不运行。"""
    if log is None:
        log = lambda m: None
    h = hashlib.md5(description.encode()).hexdigest()[:10]
    name = f"auto_{h}"
    task_dir = ROOT / "tasks" / name
    (task_dir / "modules").mkdir(parents=True, exist_ok=True)

    # 生成配置（先做入口页结构探测，把结构摘要喂给 AI）
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
    if proxy:
        if _valid_proxy(proxy):
            cfg.setdefault("anti_bot", {})["proxy"] = proxy
            log(f"🛰️ 已注入代理：{proxy}")
        else:
            log(f"⚠️ 代理格式无效已忽略：{proxy}（正确格式 http://user:pass@ip:port，host:port 是占位符不能直接用）")
    if cookie:
        cfg.setdefault("source", {})["type"] = "http"
        cfg["source"].setdefault("headers", {})["Cookie"] = cookie
        cfg.get("source", {}).pop("login", None)
        cfg.get("source", {}).pop("verify", None)
        cfg.pop("login", None); cfg.pop("verify", None)
        log("🍪 已注入登录 Cookie（HTTP 直抓模式，跳过验证/登录弹窗）")
    _ab = cfg.get("anti_bot") or {}
    if _ab.get("proxy") and not _valid_proxy(_ab["proxy"]):
        _ab.pop("proxy", None)
        log("⚠️ 已清理配置中的非法代理占位符（host:port 不能直接用）")
    _src = cfg.get("source", {}) or {}
    _verify = _src.get("verify") or {}
    _login = _src.get("login") or {}
    if _verify.get("enabled") or _login.get("enabled"):
        wait_s = int(_verify.get("max_wait_ms", 300000)) // 1000
        log(f"⚠️ 网站需要人工验证/登录：即将弹出真实浏览器，请在弹出的窗口中完成滑块/点选/登录"
            f"（最长等待 {wait_s} 秒），完成后自动继续并保存会话")
    log(f"✅ 配置已生成（source={cfg.get('source', {}).get('type')}）")
    return cfg, name, task_dir


def describe_route(cfg: dict) -> Dict[str, str]:
    """生成技术路线说明（给用户确认用）。"""
    from .sites import match_site
    su = (cfg.get("start_urls") or [""])[0]
    src = cfg.get("source", {}) or {}
    parts = []
    site = match_site(su) if su else None
    if site:
        parts.append(f"🏆 命中精配站点[{site}]：用专用解析器，字段干净")
    st = src.get("type", "http")
    cookie = bool((src.get("headers") or {}).get("Cookie"))
    if st == "http":
        if cookie:
            parts.append("🍪 Cookie 直抓（HTTP+登录态，无弹窗/无验证）")
        elif (cfg.get("anti_bot") or {}).get("proxy"):
            parts.append("🛰️ 代理直抓（HTTP+代理）")
        else:
            parts.append("🌐 HTTP 直连（公开页面）")
    elif st == "browser":
        if (src.get("verify") or {}).get("enabled"):
            parts.append("🖥️ 浏览器 + 人工验证（会弹窗，需拖滑块/点选）")
        if (src.get("login") or {}).get("enabled"):
            parts.append("🔑 浏览器 + 登录（会弹窗，需扫码/账号）")
        if not (src.get("verify") or {}).get("enabled") and not (src.get("login") or {}).get("enabled"):
            parts.append("🖥️ 浏览器渲染（JS 页面，无人工步骤）")
    elif st == "bridge":
        parts.append("🌉 桥取数（Node 桥过 WAF）")
    fields = []
    for pcfg in (cfg.get("parsers") or {}).values():
        if isinstance(pcfg, dict):
            fields.extend(list((pcfg.get("fields") or {}).keys()))
    fields = list(dict.fromkeys(fields))[:8]
    q = cfg.get("queue", {})
    summary_parts = [f"入口: {su[:70]}"]
    if fields:
        summary_parts.append("字段: " + "、".join(fields))
    summary_parts.append(f"上限: {q.get('max_requests', '默认')} 请求 / 深度 {q.get('max_depth', '默认')}")
    for pl in (cfg.get("pipelines") or [])[:3]:
        if pl.get("type") == "filter" and pl.get("op") == "between":
            import datetime
            try:
                t0 = datetime.datetime.fromtimestamp(int(pl["min"])).strftime("%Y-%m-%d")
                t1 = datetime.datetime.fromtimestamp(int(pl["max"])).strftime("%Y-%m-%d")
                summary_parts.append(f"日期过滤: {t0} ~ {t1}")
            except Exception:
                pass
    return {"route": "；".join(parts) if parts else "通用 AI 流程",
            "summary": "；".join(summary_parts)}


def plan_task(description: str, limit: Optional[int] = None, proxy: Optional[str] = None,
              cookie: Optional[str] = None, log_cb=None) -> Dict[str, Any]:
    """🔍 生成「执行计划」但不运行：任务理解 + 技术路线 + 完整配置。"""
    lines = []
    def log(msg):
        lines.append(msg)
        if log_cb:
            log_cb(msg)
    try:
        cfg, name, task_dir = _build_config(description, proxy=proxy, cookie=cookie, log=log)
        plan = describe_route(cfg)
        return {"ok": True, "name": name, "task_dir": str(task_dir),
                "config": cfg, "route": plan["route"], "summary": plan["summary"],
                "messages": lines, "description": description}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "messages": lines}


def auto_task(description: str, limit: Optional[int] = None, rounds: int = 2,
              log_cb=None, round_timeout: Optional[int] = None,
              proxy: Optional[str] = None,
              cookie: Optional[str] = None) -> Dict[str, Any]:
    """执行一次自动任务。返回 {config, result, log, sample, files}。"""
    if limit is not None:
        limit = int(limit) or None

    def log(msg):
        if log_cb:
            log_cb(msg)

    cfg, name, task_dir = _build_config(description, proxy=proxy, cookie=cookie, log=log)
    # 统一命名：storage/output 一律用 auto 任务名（否则引擎写 items/xxx.jsonl 与
    # auto 读 sample 的 items/auto_xxx.jsonl 不一致，复核/抽样会被旧数据污染）
    cfg["name"] = name
    cfg.setdefault("storage", {})["name"] = name
    cfg.setdefault("output", {})["base_name"] = name

    last_result = {}
    last_log = ""
    sample: List[Dict[str, Any]] = []
    for round_i in range(1, rounds + 1):
        # 写任务包
        (task_dir / "modules").mkdir(parents=True, exist_ok=True)
        (task_dir / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        # 每轮运行前清空旧 items（同名任务多次跑会 append 累积，污染抽样/复核）
        _items_dir = ROOT / "outputs" / "items"
        _items_dir.mkdir(parents=True, exist_ok=True)
        try:
            (_items_dir / f"{name}.jsonl").unlink(missing_ok=True)
        except Exception:
            pass

        # 运行（带超时保护：单轮最多 round_timeout 秒，超时强制终止并停止重试，绝不无限转圈）
        import os as _os
        import threading as _th
        if round_timeout is None:
            round_timeout = int(_os.environ.get("US_AUTO_ROUND_TIMEOUT", "240"))
        _src0 = cfg.get("source", {}) or {}
        if (_src0.get("verify") or {}).get("enabled") or (_src0.get("login") or {}).get("enabled"):
            round_timeout = max(round_timeout, 600)  # 人工验证要等人，放宽到 10 分钟
        log(f"▶️ 第 {round_i} 轮运行（超时上限 {round_timeout}s）...")
        from .engine_v3 import run_task
        from .log import Logger
        log_file = ROOT / "outputs" / f".run_{name}.log"
        _box = {}
        def _run_round():
            _box["r"] = run_task(task_dir, limit=limit, log_file=log_file, log_cb=log_cb)
        _t = _th.Thread(target=_run_round, daemon=True)
        _t.start()
        _t.join(timeout=round_timeout)
        if _t.is_alive():
            log(f"⏱️ 本轮超过 {round_timeout}s 未结束，正在发送停止信号并回收后台线程...")
            try:
                (Path(task_dir) / ".stop").write_text("1", encoding="utf-8")
            except Exception:
                pass
            _t.join(timeout=30)
            if _t.is_alive():
                log("⚠️ 后台线程 30s 后仍未退出（可能卡在子进程），本轮结果作废，不再重试")
            result = {"total": 0, "fetched": 0, "errors": -2, "error": f"运行超时（>{round_timeout}s）"}
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
            break
        try:
            result = _box.get("r") or {}   # 线程内 run_task 抛错时兜底，避免二次崩溃
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
        except Exception as e:
            result = {"total": 0, "fetched": 0, "errors": -1, "error": str(e)}
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else str(e)

        items_path = ROOT / "outputs" / "items" / f"{name}.jsonl"
        sample = []
        if items_path.exists():
            # 从文件末尾读（最新写入在末尾），最多回看 500 行取最新 5 条
            _lines = items_path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
            for line in reversed(_lines):
                if line.strip():
                    try:
                        sample.append(json.loads(line))
                    except Exception:
                        pass
                if len(sample) >= 5:
                    break
            sample.reverse()

        # 成功判定：必须有"真实字段"（至少一个非 _url/_parser/_ts 的字段非空），
        # 防止 AI 生成空 fields 导致 total>0 但全是空壳
        META = ("_url", "_parser", "_ts", "_id")
        real = [it for it in sample if any(
            str(it.get(k) or "").strip() for k in it if k not in META)]
        _miss = _missing_key_field(description, sample)
        if result.get("total", 0) > 0 and real and not _miss:
            log(f"✅ 第 {round_i} 轮成功：{result.get('total')} 条（抽样 {len(real)} 条有真实字段）")
            break
        if _miss:
            log(f"⚠️ 第 {round_i} 轮 total={result.get('total')} 但用户关键字段【{_miss}】为空，视为失败，进入自修复（需要详情页补抓）...")
        elif result.get("total", 0) > 0 and not real:
            log(f"⚠️ 第 {round_i} 轮 total={result.get('total')} 但全是空壳字段，视为失败，进入自修复...")

        if round_i < rounds:
            log(f"⚠️ 第 {round_i} 轮 0 条/报错，AI 正在自修复...")
            _blocks = (result or {}).get("block_stats") or {}
            _bhint = ""
            if _blocks:
                from .antibot import block_summary
                _bhint = (f"\n反爬拦截统计: {block_summary(_blocks)}。"
                          f"若含 cloudflare/verify/captcha/429/403：必须换路线——"
                          f"①source.type 改 browser + login/verify 弹窗人工过验证；"
                          f"②或建议用户用 CDP 直连已登录浏览器(加 \"cdp\":\"http://127.0.0.1:9222\")；"
                          f"③有真实代理时用 anti_bot.proxy。不要继续用 http 硬刚。")
            fix_messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"任务：{description}\n"
                    f"上次配置：{json.dumps(cfg, ensure_ascii=False)}\n"
                    f"运行结果：{json.dumps(result, ensure_ascii=False)}\n"
                    f"{_bhint}\n"
                    f"{_page_context(cfg.get('start_urls', [''])[0]) if cfg.get('start_urls') else ''}\n"
                    f"{_rendered_class_hint(task_dir)}\n"
                    f"运行日志（末尾）：\n{last_log[-2000:]}\n\n"
                    f"注意：若日志提示【用户关键字段缺失】，说明列表页没有该字段，"
                    f"必须在 config 里加 detail 配置（url_field 指向列表记录中的详情 URL 字段，"
                    f"url_transform.prefix 补全域名，extract 抓详情页字段，filters 做日期过滤）。\n"
                    f"请修正配置（选择器/网址/解析方式/是否升级浏览器/加 detail 等），只输出修正后的完整 config.json。"
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
    # 最终总结：说人话，不再让用户以为卡死
    META = ("_url", "_parser", "_ts", "_id")
    total = (last_result or {}).get("total", 0)
    real = [it for it in sample if any(str(it.get(k) or "").strip() for k in it if k not in META)]

    # 常规解析未命中但页面有内容 → LLM 直接抽取兜底（扩能力边界）
    if not (total > 0 and real):
        log("🧠 常规解析未命中，尝试 LLM 直接抽取（兜底）...")
        _rendered = ""
        _rurl = ""
        try:
            _lp = Path(task_dir) / "last_page.html"
            if _lp.exists() and _lp.stat().st_size > 2000:
                _rendered = _lp.read_text(encoding="utf-8", errors="replace")
                _rurl = (cfg.get("start_urls") or [""])[0]
        except Exception:
            pass
        fb = _llm_fallback_extract(description, cfg, log,
                                   rendered_html=_rendered or None, rendered_url=_rurl)
        if fb.get("items"):
            sample = fb["items"][:5]
            files = fb["files"]
            last_result = dict(last_result or {})
            last_result["total"] = fb["total"]
            last_result["llm_fallback"] = True
            total = fb["total"]
            real = sample
            log("✅ LLM 兜底成功，任务视为完成")

    # 🏆 精配解析器覆盖（高频网站注册表）：检测到命中即用精配解析器，字段干净
    try:
        su = (cfg.get("start_urls") or [""])[0]
        from .sites import match_site, run_site
        _site = match_site(su)
        if _site:
            _ch = ((cfg.get("source") or {}).get("headers") or {}).get("Cookie") or ""
            _px = (cfg.get("anti_bot") or {}).get("proxy") or ""
            _dr = run_site(su, cookie=_ch, proxy=_px or None, limit=int(limit or 20))
            if _dr.get("rows"):
                sample = _dr["rows"][:5]
                files = _dr["files"]
                total = _dr["total"]
                real = sample
                log(f"🏆 精配解析[{_site}]覆盖：{total} 条（字段干净）")
    except Exception as _e:
        log(f"⚠️ 精配解析未启用：{_e}")

    # 自动复核（字段完整率/去重/数量，不联网，秒级完成）
    verify = None
    try:
        from .verify import verify_rows
        verify = verify_rows(sample, cfg, sample_n=0, network=False,
                             declared=total or len(sample))
    except Exception:
        verify = None

    if total > 0 and real:
        vtxt = ""
        if verify and verify.get("checks"):
            bad = [c["name"] for c in verify["checks"] if not c.get("pass", True)]
            vtxt = ("｜复核 ✅ 通过" if not bad else "｜复核 ⚠️ " + "；".join(bad))
        summary = f"✅ 任务结束：成功 {total} 条（抽样 {len(real)} 条有真实字段）{vtxt}{_font_obfuscation_hint(real)}，导出 {list(files)}"
    else:
        reason = _diagnose_failure(cfg.get("start_urls"), last_result, log)
        summary = f"⚠️ 任务结束：0 条。原因诊断：{reason}"
        log(summary)
    return {
        "name": name,
        "config": cfg,
        "result": last_result,
        "log": last_log[-3000:],
        "sample": sample,
        "files": files,
        "summary": summary,
        "verify": verify,
        "done": True,
    }


def run_with_config(config: dict, name: str, task_dir, description: str = "",
                   limit: Optional[int] = None, rounds: int = 2,
                   round_timeout: Optional[int] = None,
                   log_cb=None) -> Dict[str, Any]:
    """确认后的执行：用已确认的 config 直接运行（跳过 AI 生成）。"""
    import json as _json
    from pathlib import Path as _P
    if limit is not None:
        limit = int(limit) or None

    def log(msg):
        if log_cb:
            log_cb(msg)

    td = _P(task_dir)
    (td / "modules").mkdir(parents=True, exist_ok=True)
    # 统一命名：storage/output 与运行器 name 一致（避免 items 文件名错位污染抽样/复核）
    config["name"] = name
    config.setdefault("storage", {})["name"] = name
    config.setdefault("output", {})["base_name"] = name
    (td / "config.json").write_text(_json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    _items_dir = ROOT / "outputs" / "items"
    _items_dir.mkdir(parents=True, exist_ok=True)
    try:
        (_items_dir / f"{name}.jsonl").unlink(missing_ok=True)
    except Exception:
        pass

    last_result = {}
    last_log = ""
    sample: List[Dict[str, Any]] = []
    import os as _os
    import threading as _th
    if round_timeout is None:
        round_timeout = int(_os.environ.get("US_AUTO_ROUND_TIMEOUT", "240"))
    _src0 = config.get("source", {}) or {}
    if (_src0.get("verify") or {}).get("enabled") or (_src0.get("login") or {}).get("enabled"):
        round_timeout = max(round_timeout, 600)
    for round_i in range(1, rounds + 1):
        log(f"▶️ 第 {round_i} 轮运行（超时上限 {round_timeout}s）...")
        from .engine_v3 import run_task
        log_file = ROOT / "outputs" / f".run_{name}.log"
        _box = {}
        def _run_round():
            _box["r"] = run_task(td, limit=limit, log_file=log_file, log_cb=log_cb)
        _t = _th.Thread(target=_run_round, daemon=True)
        _t.start()
        _t.join(timeout=round_timeout)
        if _t.is_alive():
            log(f"⏱️ 本轮超过 {round_timeout}s 未结束，正在发送停止信号并回收后台线程...")
            try:
                (Path(task_dir) / ".stop").write_text("1", encoding="utf-8")
            except Exception:
                pass
            _t.join(timeout=30)
            if _t.is_alive():
                log("⚠️ 后台线程 30s 后仍未退出（可能卡在子进程），本轮结果作废，不再重试")
            result = {"total": 0, "fetched": 0, "errors": -2, "error": f"运行超时（>{round_timeout}s）"}
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
            break
        try:
            result = _box.get("r") or {}   # 线程内 run_task 抛错时兜底，避免二次崩溃
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
        except Exception as e:
            result = {"total": 0, "fetched": 0, "errors": -1, "error": str(e)}
            last_result = result
            last_log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else str(e)
        items_path = ROOT / "outputs" / "items" / f"{name}.jsonl"
        sample = []
        if items_path.exists():
            # 从文件末尾读（最新写入在末尾），最多回看 500 行取最新 5 条
            _lines = items_path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
            for line in reversed(_lines):
                if line.strip():
                    try:
                        sample.append(_json.loads(line))
                    except Exception:
                        pass
                    if len(sample) >= 5:
                        break
            sample.reverse()
        META = ("_url", "_parser", "_ts", "_id")
        real = [it for it in sample if any(str(it.get(k) or "").strip() for k in it if k not in META)]
        _miss = _missing_key_field(description, sample)
        if result.get("total", 0) > 0 and real and not _miss:
            log(f"✅ 第 {round_i} 轮成功：{result.get('total')} 条（抽样 {len(real)} 条有真实字段）")
            break
        if _miss:
            log(f"⚠️ 第 {round_i} 轮 total={result.get('total')} 但用户关键字段【{_miss}】为空，视为失败，进入自修复（需要详情页补抓）...")
        elif result.get("total", 0) > 0 and not real:
            log(f"⚠️ 第 {round_i} 轮 total={result.get('total')} 但全是空壳字段，视为失败，进入自修复...")
        if round_i < rounds:
            log(f"⚠️ 第 {round_i} 轮 0 条/报错，AI 正在自修复...")
            fix_messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"任务：{description}\n"
                    f"上次配置：{_json.dumps(config, ensure_ascii=False)}\n"
                    f"运行结果：{_json.dumps(result, ensure_ascii=False)}\n"
                    f"{_page_context((config.get('start_urls') or [''])[0]) if config.get('start_urls') else ''}\n"
                    f"运行日志（末尾）：\n{last_log[-2000:]}\n\n"
                    f"{_rendered_class_hint(task_dir)}\n"
                    f"注意：若日志提示【用户关键字段缺失】，说明列表页没有该字段，"
                    f"必须在 config 里加 detail 配置（url_field 指向列表记录中的详情 URL 字段，"
                    f"url_transform.prefix 补全域名，extract 抓详情页字段，filters 做日期过滤）。\n"
                    f"请修正配置（选择器/网址/解析方式/加 detail 等），只输出修正后的完整 config.json。"
                )},
            ]
            try:
                config = _extract_json(_llm_chat(fix_messages))
                config = _validate_and_fix(config)
                config["name"] = name
                config["storage"]["name"] = name
                config["output"]["base_name"] = name
                (td / "config.json").write_text(_json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                log(f"❌ 自修复生成失败: {e}")
                break

    files = {}
    for ext in ("json", "csv", "xlsx"):
        fp = ROOT / "outputs" / f"{name}.{ext}"
        if fp.exists():
            files[ext] = str(fp.relative_to(ROOT))
    META = ("_url", "_parser", "_ts", "_id")
    total = (last_result or {}).get("total", 0)
    real = [it for it in sample if any(str(it.get(k) or "").strip() for k in it if k not in META)]

    try:
        su = (config.get("start_urls") or [""])[0]
        from .sites import match_site, run_site
        _site = match_site(su)
        if _site:
            _ch = ((config.get("source") or {}).get("headers") or {}).get("Cookie") or ""
            _px = (config.get("anti_bot") or {}).get("proxy") or ""
            _dr = run_site(su, cookie=_ch, proxy=_px or None, limit=int(limit or 20))
            if _dr.get("rows"):
                sample = _dr["rows"][:5]
                files = _dr["files"]
                total = _dr["total"]
                real = sample
                log(f"🏆 精配解析[{_site}]覆盖：{total} 条（字段干净）")
    except Exception as _e:
        log(f"⚠️ 精配解析未启用：{_e}")

    if not (total > 0 and real):
        log("🧠 常规解析未命中，尝试 LLM 直接抽取（兜底）...")
        _rendered = ""
        _rurl = ""
        try:
            _lp = Path(task_dir) / "last_page.html"
            if _lp.exists() and _lp.stat().st_size > 2000:
                _rendered = _lp.read_text(encoding="utf-8", errors="replace")
                _rurl = (config.get("start_urls") or [""])[0]
        except Exception:
            pass
        fb = _llm_fallback_extract(description, config, log,
                                   rendered_html=_rendered or None, rendered_url=_rurl)
        if fb.get("items"):
            sample = fb["items"][:5]
            files = fb["files"]
            last_result = dict(last_result or {})
            last_result["total"] = fb["total"]
            last_result["llm_fallback"] = True
            total = fb["total"]
            real = sample
            log("✅ LLM 兜底成功，任务视为完成")

    verify = None
    try:
        from .verify import verify_rows
        verify = verify_rows(sample, config, sample_n=0, network=False,
                             declared=total or len(sample))
    except Exception:
        verify = None

    if total > 0 and real:
        vtxt = ""
        if verify and verify.get("checks"):
            bad = [c["name"] for c in verify["checks"] if not c.get("pass", True)]
            vtxt = ("｜复核 ✅ 通过" if not bad else "｜复核 ⚠️ " + "；".join(bad))
        summary = f"✅ 任务结束：成功 {total} 条（抽样 {len(real)} 条有真实字段）{vtxt}{_font_obfuscation_hint(real)}，导出 {list(files)}"
    else:
        reason = _diagnose_failure(config.get("start_urls"), last_result, log)
        summary = f"⚠️ 任务结束：0 条。原因诊断：{reason}"
        log(summary)
    return {
        "name": name,
        "config": config,
        "result": last_result,
        "log": last_log[-3000:],
        "sample": sample,
        "files": files,
        "summary": summary,
        "verify": verify,
        "done": True,
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
