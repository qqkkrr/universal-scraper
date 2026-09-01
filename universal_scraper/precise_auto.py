#!/usr/bin/env python3
"""🤖 一键自动精配生成器。

用户跑任务失败 / AI 不确定入口时，WebUI 点「一键自动精配」：
  1) 探测入口页（HTTP → 内容太少自动换浏览器渲染）
  2) LLM 分析页面结构 → 输出精配方案（engine 配置 / 图片榜单运行器）
  3) 保存 configs/auto_precise_<host>.json + 注册到精配注册表
  4) 试跑验证（小 limit），返回行数与导出文件
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "configs"
TASKS_DIR = ROOT / "tasks"


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------
def _fetch_sample(url: str, browser: bool = False, log: Optional[Callable[[str], None]] = None):
    from .quick import fetch_url
    if log:
        log(f"🔍 正在探测入口页结构: {url}")
    r = fetch_url(url, timeout=60, browser=browser)
    if r.get("error"):
        if not browser:
            if log:
                log(f"⚠️ HTTP 直连失败（{r['error']}），改浏览器渲染…")
            return _fetch_sample(url, browser=True, log=log)
        raise RuntimeError(f"探测失败: {r['error']}")
    html = r.get("html") or r.get("article") or r.get("text") or ""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html or "", flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 300 and not browser:
        if log:
            log(f"⚠️ HTTP 只拿到 {len(text)} 字符（疑似 JS 壳），改浏览器渲染…")
        return _fetch_sample(url, browser=True, log=log)
    return {"html": html, "text": text, "url": r.get("final_url") or url, "browser": browser}


def _html_summary(html: str, max_len: int = 9000) -> str:
    """给 LLM 的页面摘要：标题 + 表格行 + 链接列表 + 图片。"""
    out = []
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    if m:
        out.append("标题: " + m.group(1).strip())
    links = []
    seen = set()
    for mm in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.S):
        href, txt = mm.group(1), re.sub(r"<[^>]+>", "", mm.group(2)).strip()
        if txt and href not in seen and not href.startswith(("javascript", "#", "mailto")):
            seen.add(href)
            links.append(f"{txt[:40]} -> {href[:90]}")
    if links:
        out.append("链接(前40条):\n" + "\n".join(links[:40]))
    tables = re.findall(r"<table.*?</table>", html, re.S)
    for i, tb in enumerate(tables[:3]):
        txt = re.sub(r"<[^>]+>", " | ", tb)
        txt = re.sub(r"\|+", "|", txt).strip("| ")
        out.append(f"表格{i}: " + txt[:800])
    imgs = [mm.group(1) for mm in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html)
            if "logo" not in mm.group(1) and "icon" not in mm.group(1)]
    if imgs:
        out.append("图片(前10):\n" + "\n".join(imgs[:10]))
    body = re.sub(r"<[^>]+>", " ", html)
    body = re.sub(r"\s+", " ", body).strip()
    if body:
        out.append("正文(前2000): " + body[:2000])
    return "\n\n".join(out)[:max_len]


# ---------------------------------------------------------------------------
# LLM 生成方案
# ---------------------------------------------------------------------------
_SCHEMA_PROMPT = """你是爬虫架构师。根据任务与页面样本，输出一份可直接执行的爬虫配置 JSON。
字段含义（严格按此 schema）:
{
  "name": "任务名(字母数字下划线)",
  "host": "主域名，如 cta.org.cn",
  "data_form": "html_list | html_table | json_api | image_ranking | pdf_attachment | login_required",
  "entry": "最佳入口URL（列表/搜索页）",
  "source": {"type": "http 或 browser", "headless": true},
  "rules": [{"match": "contains", "pattern": "URL片段", "parser": "parser名"}],
  "parsers": { "parser名": {"type": "html", "row_css": "行选择器",
      "fields": {"字段": {"css": "选择器"}}, "extract_links": {"allow": "正则", "same_domain": true}}},
  "pipelines": [{"type": "dedup", "key": "字段"}],
  "detail": {"enabled": false},
  "storage": {"type": "jsonl", "name": "任务名"},
  "output": {"dir": "outputs", "base_name": "任务名"}
}
规则:
- 如果数据就在当前页面(表格/JSON/列表)：entry=当前URL，source 按页面是否为 JS 壳选 browser/http。
- 如果是列表+详情：列表 parser 提取 title/link/date，detail.enabled=true，detail.url_field=link，detail.extract 里用 {"name":"字段","type":"css_text","selector":"..."}。
- 如果数据是图片榜单：data_form=image_ranking，entry=含榜单图片的页面URL，parsers 里给出该页图片 img 的 src 特征(如 dometa.net 的 png)，字段名给预期的榜单列。
- 如果页面需要登录/验证码：data_form=login_required，并说明原因。
- 如果数据是 PDF 附件：data_form=pdf_attachment，说明附件链接特征。
只输出 JSON，不要解释。"""


def _ask_scheme(description: str, sample: Dict[str, Any], log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    from .llm import LLMClient
    llm = LLMClient()
    if log:
        log("🤖 AI 正在分析页面结构并生成精配方案…")
    prompt = (
        f"任务描述: {description}\n\n"
        f"入口URL: {sample['url']}（{'浏览器渲染' if sample.get('browser') else 'HTTP直连'}）\n\n"
        f"页面样本:\n{_html_summary(sample.get('html') or '', 9000)}\n\n"
        f"{_SCHEMA_PROMPT}"
    )
    raw = llm.chat([
        {"role": "system", "content": "你是专业的爬虫配置生成器，只输出合法 JSON。"},
        {"role": "user", "content": prompt},
    ])
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            return json.loads(m.group(0))
        raise ValueError(f"精配方案解析失败: {raw[:300]}")


# ---------------------------------------------------------------------------
# 保存 + 注册 + 试跑
# ---------------------------------------------------------------------------
def _sanitize_host(host: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", host).strip("_") or "site"


def _normalize_engine_cfg(scheme: Dict[str, Any]) -> Dict[str, Any]:
    """把 LLM 方案规范成引擎可执行配置（entry→start_urls、补默认段）。"""
    cfg = dict(scheme or {})
    if not cfg.get("start_urls") and cfg.get("entry"):
        cfg["start_urls"] = [cfg["entry"]]
    cfg.pop("entry", None)
    cfg.pop("host", None)
    cfg.pop("data_form", None)
    name = re.sub(r"[^0-9A-Za-z_]", "_", str(cfg.get("name") or "auto_precise")).strip("_") or "auto_precise"
    cfg["name"] = name
    # rules/parsers 兜底：LLM 常漏生成，补一个能跑的默认路由（页面主体行）
    if not cfg.get("rules") or not cfg.get("parsers"):
        cfg.setdefault("rules", [{"match": "contains", "pattern": "/", "parser": "default"}])
        cfg.setdefault("parsers", {})
        if "default" not in cfg["parsers"]:
            cfg["parsers"]["default"] = {
                "type": "html",
                "row_css": "table tr:not(:first-child), li, .item, .list li, ul li",
                "fields": {
                    "title": {"css": "a::text"},
                    "link": {"css": "a::attr(href)"}
                }
            }
    cfg.setdefault("queue", {"max_depth": 2, "max_requests": 100, "max_concurrency": 2})
    cfg.setdefault("storage", {"type": "jsonl", "name": name})
    cfg.setdefault("output", {"dir": "outputs", "base_name": name})
    cfg.setdefault("anti_bot", {"min_interval": 0.5, "max_retries": 2})
    src = cfg.setdefault("source", {})
    if src.get("type") == "browser":
        src.setdefault("headless", True)
    return cfg


def _register_engine_config(cfg: Dict[str, Any], host: str, log: Optional[Callable[[str], None]] = None) -> Path:
    """把 engine 配置写成任务包 tasks/auto_precise_<host>/config.json 并注册。"""
    name = f"auto_precise_{_sanitize_host(host)}"
    task_dir = TASKS_DIR / name
    task_dir.mkdir(parents=True, exist_ok=True)
    cfg.setdefault("name", name)
    (task_dir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {"host": host, "kind": "engine", "name": name, "task_dir": str(task_dir)}
    (CONFIG_DIR / f"auto_precise_{_sanitize_host(host)}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    # 注册到运行时注册表（sites.py 的配置型精配）
    from . import sites as _sites
    _sites.register_config_precise(host, task_dir, kind="engine")
    if log:
        log(f"✅ 精配已保存: configs/auto_precise_{_sanitize_host(host)}.json（任务包 tasks/{name}）")
    return task_dir


def _run_engine_probe(task_dir: Path, limit: int, log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """用生成的配置试跑一轮，返回 {rows, files, error}。"""
    from .engine_v3 import run_task
    if log:
        log(f"▶️ 第 1 轮试跑（limit={limit}）…")
    try:
        res = run_task(task_dir, limit=limit, log_cb=(log or (lambda m: None)))
    except Exception as e:
        return {"rows": [], "files": {}, "error": f"{type(e).__name__}: {e}"}
    out_name = (res or {}).get("name") or task_dir.name
    rows = []
    fp = ROOT / "outputs" / f"{out_name}.json"
    if fp.exists():
        try:
            rows = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            rows = []
    files = {}
    for ext in ("json", "csv", "xlsx"):
        p = ROOT / "outputs" / f"{out_name}.{ext}"
        if p.exists():
            files[ext] = f"outputs/{out_name}.{ext}"
    return {"rows": rows, "files": files, "error": ""}


def _build_image_ranking_runner(cfg: Dict[str, Any], host: str, log: Optional[Callable[[str], None]] = None) -> Path:
    """图片榜单：保存为 run 型精配（下载图片 → 千问视觉 OCR → 行）。"""
    from . import sites as _sites
    name = f"auto_precise_{_sanitize_host(host)}"
    meta = {"host": host, "kind": "image_ranking", "name": name, "entry": cfg.get("entry", ""),
            "img_src_hint": (cfg.get("parsers") or {}).get("img_hint", "")}
    (CONFIG_DIR / f"auto_precise_{_sanitize_host(host)}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    _sites._register_image_precise(meta)
    if log:
        log(f"✅ 图片榜单精配已注册: {name}（entry={meta['entry']}）")
    return CONFIG_DIR / f"auto_precise_{_sanitize_host(host)}.json"


def _image_ranking_run(meta: Dict[str, Any], url: str, limit: int = 20,
                       log: Optional[Callable[[str], None]] = None) -> list:
    """图片榜单运行器：浏览器渲染 entry → 收集目标图片 → 下载 → OCR → 行。"""
    import requests
    from .llm import LLMClient
    entry = meta.get("entry") or url
    hint = meta.get("img_src_hint") or ""
    rows = []
    # 1) 抓 entry + extra_pages（浏览器渲染，图片懒加载），收集榜单图
    pages = [entry] + list(meta.get("extra_pages") or [])
    imgs = []
    pat = re.compile(hint) if hint else None
    for pg in pages:
        try:
            sample = _fetch_sample(pg, browser=True, log=log)
            html = sample.get("html") or ""
            for mm in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html):
                src = mm.group(1)
                if re.search(r"(?:logo|icon|qrcode|wechat)", src, re.I):
                    continue
                if pat and pat.search(src):
                    imgs.append(src)
                elif not pat and re.search(r"(?:dometa|attach|upload|filemanager|content|cont)", src, re.I):
                    imgs.append(src)
        except Exception as e:
            if log:
                log(f"⚠️ 页面抓取失败 {pg}: {e}")
    if not imgs:
        raise RuntimeError("图片榜单页未找到榜单图片（页面结构可能变化）")
    imgs = list(dict.fromkeys(imgs))[:limit]
    llm = LLMClient()
    out_dir = ROOT / "outputs"
    out_dir.mkdir(exist_ok=True)
    for i, src in enumerate(imgs, 1):
        if not src.startswith("http"):
            src = "https://" + entry.split("//")[1].split("/")[0] + src
        if log:
            log(f"🖼️ 正在 OCR 第 {i}/{len(imgs)} 张榜单图…")
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            r = requests.get(src, timeout=40, verify=False,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data_url = "data:image/" + (src.rsplit(".", 1)[-1] if "." in src else "png") + ";base64," + \
                       __import__("base64").b64encode(r.content).decode()
            prompt = ("这是排行榜/榜单表格图片。请逐行OCR表格内容，把每一行输出为JSON对象，"
                      "字段名用中文（如：排名、品牌、价值、公司、行业等，按表头原样）。"
                      "只输出JSON数组，数字务必准确，不要解释。")
            text = llm.vision(prompt, data_url)
            m = re.search(r"\[.*\]", text, re.S)
            arr = json.loads(m.group(0)) if m else []
            for item in arr:
                if isinstance(item, dict):
                    item.setdefault("_image", src)
                    item.setdefault("_page", entry)
                    rows.append(item)
        except Exception as e:
            if log:
                log(f"⚠️ 第 {i} 张图 OCR 失败: {type(e).__name__}: {e}")
    if not rows:
        raise RuntimeError("图片 OCR 0 行（图片可能不是表格，或视觉模型未识别）")
    # 噪声过滤：分析页的“三年总价值”会被误读成榜单行（价值>10000 且无证券简称/排名连续）
    def _num(v):
        try:
            return float(str(v).replace(",", "").replace("，", ""))
        except Exception:
            return None
    _clean = []
    for r in rows:
        v = _num(r.get("价值") or r.get("商标品牌价值（亿元）") or r.get("品牌价值") or r.get("value"))
        if v is not None and v > 10000 and "证券简称" not in r and "商标品牌价值（亿元）" not in r:
            continue
        _clean.append(r)
    rows = _clean
    # 导出
    base = f"auto_precise_{_sanitize_host(meta.get('host', ''))}"
    fp = out_dir / f"{base}.json"
    fp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        import csv
        with open(out_dir / f"{base}.csv", "w", newline="", encoding="utf-8-sig") as f:
            keys = list(rows[0].keys())
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
    except Exception:
        pass
    return rows


# ---------------------------------------------------------------------------
# 高级探测：HTTP 快探 → 浏览器 detect → 若 WAF/壳则种 Cookie 重试
# ---------------------------------------------------------------------------
def _probe_advanced(url: str, log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """返回 {url, seed, detect, http_status, waf}。"""
    from urllib.parse import urlparse
    from .tactics import run_bridge_jsonl
    bridge = ROOT / "scripts" / "tactic_bridge.cjs"
    host = urlparse(url).netloc
    seed = "https://" + host + "/"

    # 第 0 层：PDF 直链（跳过 HTML 探测，直接下载确认）
    from .pdf_table import is_pdf_url, download_pdf
    if is_pdf_url(url):
        if log:
            log("📄 识别到 PDF 直链，下载探测…")
        try:
            fp = download_pdf(url, timeout=40)
            size = fp.stat().st_size
            fp.unlink(missing_ok=True)
            d = {"httpStatus": 200, "textLen": size, "title": "",
                 "pdfLinks": [url], "is_pdf": True, "bigImages": [], "tables": [],
                 "hasVue": False, "loginText": False, "linkCount": 0,
                 "_url": url, "_http": True}
            if log:
                log(f"✅ PDF 直链可用（{size} 字节）")
            return {"url": url, "seed": seed, "http_status": 200, "waf": False, "detect": d}
        except Exception as e:
            if log:
                log(f"⚠️ PDF 下载失败（{type(e).__name__}: {e}），改浏览器探测…")

    # 第 1 层：HTTP 快探
    if log:
        log(f"🔍 HTTP 快探: {url}")
    try:
        import requests
        r = requests.get(url, timeout=20, verify=False,
                          headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"})
        text = re.sub(r"<[^>]+>", " ", r.text or "")
        text = re.sub(r"\s+", " ", text).strip()
        if r.status_code == 200 and len(text) >= 300:
            html = r.text or ""
            imgs = [mm.group(1) for mm in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html)
                    if not re.search(r"logo|icon|qrcode|wechat|avatar", mm.group(1), re.I)]
            pdfs = [mm.group(1) for mm in re.finditer(r'(?:href|src|fileurl)=["\']([^"\']*?\.(?:pdf|docx?|xlsx?)[^"\']*)["\']', html, re.I)]
            tables = [re.sub(r"<[^>]+>", " | ", tb)[:200] for tb in re.findall(r"<table.*?</table>", html, re.S)[:3]]
            d = {"httpStatus": 200, "textLen": len(text), "textHead": text[:600],
                 "title": "", "hasVue": bool(re.search(r"id=\"app\"|__vue__|new Vue", html)),
                 "loginText": bool(re.search(r"登录|注册|验证码|滑块|安全验证", text[:800])),
                 "pdfLinks": list(dict.fromkeys(pdfs))[:20],
                 "bigImages": list(dict.fromkeys(imgs))[:20],
                 "tables": tables, "linkCount": len(re.findall(r"<a\s", html)),
                 "_url": url, "_http": True}
            if log:
                log(f"✅ HTTP 直连可用（{len(text)} 字符 | 大图={len(d['bigImages'])} | PDF={len(d['pdfLinks'])} | Vue={d['hasVue']} | 表格={len(d['tables'])}）")
            return {"url": url, "seed": seed, "http_status": 200, "waf": False, "detect": d}
        if log:
            log(f"⚠️ HTTP {r.status_code} / 内容 {len(text)} 字符（疑似 WAF 或 JS 壳），改浏览器探测…")
    except Exception as e:
        if log:
            log(f"⚠️ HTTP 失败（{type(e).__name__}），改浏览器探测…")

    # 第 2 层：浏览器 detect（不种 Cookie）
    if log:
        log(f"🔍 浏览器探测（无 Cookie）: {url}")
    objs = run_bridge_jsonl(bridge, {"mode": "detect", "target": url, "settle": "1200"}, timeout=180)
    det = next((o.get("data") or {}) for o in objs if o.get("type") == "detect") or {}
    status = det.get("httpStatus")
    text_len = int(det.get("textLen") or 0)
    # 修复：status 非 2xx（含 None/0=连接失败）一律视为不可用，否则连不上的地址被误判"可用"
    ok_status = status in (200, 201, 206)
    waf = (not ok_status) or (ok_status and text_len < 300)
    if not waf:
        det["_url"] = url
        if log:
            log(f"✅ 浏览器直连可用（status={status}, {text_len} 字符）")
        return {"url": url, "seed": seed, "http_status": status, "waf": False, "detect": det}

    # 第 3 层：种 Cookie 重试（WAF 场景）
    if log:
        log(f"⚠️ WAF/壳（status={status}, {text_len} 字符），先访问首页种 Cookie 再重试…")
    objs = run_bridge_jsonl(bridge, {"mode": "detect", "seed": seed, "target": url, "settle": "1200"}, timeout=180)
    det2 = next((o.get("data") or {}) for o in objs if o.get("type") == "detect") or {}
    det2["_url"] = url
    det2["_waf_seed"] = seed
    if log:
        log(f"✅ 种 Cookie 后: status={det2.get('httpStatus')}, {det2.get('textLen')} 字符")
    return {"url": url, "seed": seed, "http_status": det2.get("httpStatus"), "waf": True, "detect": det2}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def generate_precise(description: str, url: str, config: Optional[Dict[str, Any]] = None,
                     limit: int = 8, log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """一键自动精配。返回 {ok, host, kind, name, rows, files, error, detail}"""
    def _lg(m):
        if log:
            log(m)
    from urllib.parse import urlparse
    host = urlparse(url if "//" in url else "https://" + url).netloc.lower()
    _lg(f"🎯 目标站点: {host}")

    # 阶段 1：多轮探测（HTTP → 浏览器 → 种 Cookie）+ 入口候选自动探测
    probe = _entry_candidates(description, url, log=_lg)
    detect = probe.get("detect") or {}
    _lg(f"🧠 探测结果: status={probe.get('http_status')} | "
        f"text={detect.get('textLen')}字符 | Vue={detect.get('hasVue')} | "
        f"PDF={len(detect.get('pdfLinks') or [])} | 大图={len(detect.get('bigImages') or [])} | "
        f"表格={len(detect.get('tables') or [])}")

    # 阶段 2：战术决策（规则优先，LLM 兜底）
    from .tactics import decide_tactic, sanitize_host
    decision = decide_tactic(description, detect, log=_lg)
    tactic = decision.get("tactic")
    _lg(f"🧩 选定战术: {tactic}（{decision.get('reason')}）")

    if tactic == "login_required":
        _lg("⚠️ 站点需要登录/验证码，自动精配只能生成入口配置，请登录后重跑。")
        return {"ok": False, "host": host, "kind": tactic, "name": f"auto_precise_{sanitize_host(host)}",
                "rows": [], "files": {}, "error": "需要登录/验证码", "detail": "请人工登录后重跑任务"}

    # 阶段 3：生成战术参数，先试跑、通过后才落盘注册（防毒配置：失败配置绝不能进注册表）
    params = dict(decision.get("params") or {})
    params.setdefault("host", host)
    params.setdefault("entry", params.get("entry") or url)
    params.setdefault("seed", probe.get("seed") or "")
    if tactic == "cookie_click":
        # url_template 需要知道详情页 URL 模式：从任务描述或链接提取，兜底用页面里第一个跳转 URL 去 id
        params.setdefault("url_template", "")
    meta = {"host": host, "kind": f"tactic:{tactic}", "name": f"auto_precise_{sanitize_host(host)}",
            "entry": params.get("entry"), "tactic": tactic, "params": params,
            "description": description}
    meta_path = CONFIG_DIR / f"auto_precise_{sanitize_host(host)}.json"
    _wrote_meta = False

    try:
        if tactic == "html_engine":
            # 常规页面走 engine 任务包；_tactic_engine_probe 内部只会注册可执行的 engine 配置。
            rows, files, detail = _tactic_engine_probe(meta, url, limit, _lg)
            if not rows:
                raise RuntimeError("engine 试跑 0 条")
        else:
            runner = {"cookie_click": _tactic_cookie_click_probe,
                      "pdf_attach": _tactic_pdf_probe,
                      "image_ocr": _tactic_image_probe}[tactic]
            rows, files, detail = _repair_probe(meta, url, limit, runner, _lg)
            # 试跑成功后保存/注册；失败则保持无配置，下次自动精配重试。
            from . import sites as _sites
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            _wrote_meta = True
            _sites._register_tactic_precise(meta)
            _lg(f"✅ 精配已保存: {meta_path.name}（战术={tactic}）")
        _lg(f"✅ 试跑成功：{len(rows)} 条")
        # rows 返回完整数据（自动精配要拿全量；之前只给 3 行样例会让质量闸门误判"数量不足"）
        return {"ok": True, "host": host, "kind": f"tactic:{tactic}", "name": meta["name"],
                "rows": rows, "sample": rows[:3], "files": files, "error": "", "detail": detail}
    except Exception as e:
        # 只清理本次失败尝试写入的元信息；不删除此前已存在的成功配置。
        if _wrote_meta:
            try:
                meta_path.unlink(missing_ok=True)
            except Exception:
                pass
        _lg(f"❌ 试跑失败：{type(e).__name__}: {e}")
        return {"ok": False, "host": host, "kind": f"tactic:{tactic}", "name": meta["name"],
                "rows": [], "files": {}, "error": str(e),
                "detail": f"战术[{tactic}]试跑失败，未保存/已清理配置，下次可重试"}


def _export_files(base: str) -> Dict[str, str]:
    out = {}
    for ext in ("json", "csv", "xlsx"):
        p = ROOT / "outputs" / f"{base}.{ext}"
        if p.exists():
            out[ext] = f"outputs/{base}.{ext}"
    return out


# ---------------------------------------------------------------------------
# 强化（借鉴 anansi/dig2crawl）：试跑失败自动修复循环 + 入口候选探测
# ---------------------------------------------------------------------------
def _llm_repair(meta: Dict[str, Any], err: str,
                log: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
    """LLM 分析失败原因，返回修正 {entry?, params?}。带上真实页面 DOM 样本（anansi 自愈思路）。"""
    from .llm import LLMClient
    tactic = meta.get("tactic") or ""
    desc = meta.get("description") or ""
    params = meta.get("params") or {}
    # 抓一次页面样本（尽量拿真实 DOM 结构给 LLM 分析）
    sample_html = ""
    try:
        from .quick import fetch_url
        r = fetch_url(params.get("entry") or "", timeout=40,
                      browser=(tactic in ("cookie_click", "image_ocr", "pdf_attach")))
        sample_html = _html_summary(r.get("html") or r.get("article") or r.get("text") or "", 4000)
    except Exception:
        pass
    prompt = (
        f"爬虫战术 [{tactic}] 试跑失败，请给出修复方案。\n"
        f"任务: {desc}\n"
        f"入口: {params.get('entry', '')}\n"
        f"错误: {err}\n"
        f"页面样本（真实 DOM 结构，据此修正选择器）:\n{sample_html[:3500]}\n\n"
        f"只输出 JSON：{{\"entry\": \"修正后的入口URL(若无变化用原值)\"," 
        f" \"params\": {{\"item_css\": \"若点击战术选择器不对，给新选择器\"," 
        f" \"img_src_hint\": \"图片战术的图片URL正则\", \"url_template\": \"详情页URL模板，id用{id}占位\"}}}}"
    )
    try:
        llm = LLMClient()
        raw = llm.chat([
            {"role": "system", "content": "你是爬虫修复专家，只输出合法 JSON。"},
            {"role": "user", "content": prompt},
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        if log:
            log(f"⚠️ LLM 修复建议失败: {e}")
        return None


_STOPWORDS = {"抓取", "所有", "全部", "每个", "每年", "每日", "每天", "最新", "链接", "信息",
               "数据", "列表", "详情", "内容", "相关", "以及", "和", "的", "为", "请"}


def _extract_kws(desc: str):
    """从任务描述提取核心关键词：2-4 字滑动窗口，剔除含停用词的碎片。"""
    text = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", desc or "")
    kws = []
    for n in (4, 3, 2):
        for i in range(len(text) - n + 1):
            w = text[i:i + n]
            if any(stop in w for stop in _STOPWORDS):
                continue
            if w not in kws:
                kws.append(w)
    return kws


def _rows_match_task(description: str, rows) -> bool:
    """防假成功：抽样行文本里必须出现任务描述的核心关键词。"""
    kws = _extract_kws(description or "")
    if not kws:
        return True  # 无法判断时不拦
    joined = " ".join(str(v) for r in (rows or [])[:5] for v in r.values())
    # 按长度分层匹配：4 字核心词优先，2 字词兜底（如“招生”“志愿”）
    for n in (4, 3, 2):
        for w in kws:
            if len(w) == n and w in joined:
                return True
    return False


def _repair_probe(meta: Dict[str, Any], url: str, limit: int, runner, log=None):
    """试跑 + 失败自动修复（最多 2 轮）。成功但数据与任务不相关也视为失败（防假成功）。"""
    desc = meta.get("description") or ""

    def _run_once(m, u):
        rows, files, detail = runner(m, u, limit, log)
        if not rows:
            raise RuntimeError("试跑 0 条")
        if not _rows_match_task(desc, rows):
            raise RuntimeError("试跑有行但与任务内容不相关（可能抓到导航/无关列表），需要换入口或修正选择器")
        return rows, files, detail

    try:
        return _run_once(meta, url)
    except Exception as e1:
        if log:
            log(f"⚠️ 第 1 轮试跑未通过：{str(e1)[:200]}，AI 正在修复…")
        fixed = _llm_repair(meta, str(e1), log)
        if not fixed:
            raise
        meta2 = dict(meta)
        params2 = dict(meta.get("params") or {})
        params2.update(fixed.get("params") or {})
        meta2["params"] = params2
        entry2 = fixed.get("entry") or params2.get("entry") or url
        try:
            return _run_once(meta2, entry2)
        except Exception as e2:
            if log:
                log(f"⚠️ 第 2 轮仍失败：{str(e2)[:200]}")
            raise RuntimeError(f"试跑未通过（已尝试自动修复）：{e2}")


def _entry_candidates(description: str, url: str, log=None) -> Dict[str, Any]:
    """入口探测：原入口不可用（404/WAF/0条）时，LLM 生成候选入口逐个探测（dig2crawl discover 思路）。"""
    probe = _probe_advanced(url, log=log)
    d = probe.get("detect") or {}
    ok = probe.get("http_status") == 200 and (int(d.get("textLen") or 0) >= 300 or d.get("bigImages") or d.get("pdfLinks"))
    if ok:
        return probe
    # 确定性兜底①：域名根（原路径 404/改版时，根首页几乎总是活的，且不依赖 LLM 猜）
    try:
        from urllib.parse import urlparse
        _parsed = urlparse(url if "//" in url else "https://" + url)
        _host = _parsed.netloc
        for _root in (f"https://{_host}/", f"http://{_host}/"):
            if _root.rstrip("/") == url.rstrip("/"):
                continue
            if log:
                log(f"🔍 尝试域名根入口: {_root}")
            _pr = _probe_advanced(_root, log=log)
            _dd = _pr.get("detect") or {}
            if _pr.get("http_status") == 200 and (int(_dd.get("textLen") or 0) >= 300
                                                  or _dd.get("bigImages") or _dd.get("pdfLinks")):
                if log:
                    log(f"✅ 域名根入口可用: {_root}")
                return _pr
    except Exception:
        pass
    if log:
        log("🔄 原入口不可用，AI 正在推断候选入口…")
    from .llm import LLMClient
    try:
        llm = LLMClient()
        raw = llm.chat([
            {"role": "system", "content": "你是爬虫入口侦探。只输出 JSON 数组，3 个候选 URL。"},
            {"role": "user", "content": (
                f"任务: {description}\n当前入口: {url} 返回 {probe.get('http_status')}（{str(d.get('textHead'))[:120]}）。\n"
                f"请给出该任务在官方站点最可能的 3 个入口 URL（列表/查询页，按可能性排序）。"
            )},
        ])
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        try:
            cands = json.loads(raw)
        except Exception:
            # LLM 常夹带解释文字：提取第一个 JSON 数组再解析（二次兜底）
            _m = re.search(r"\[[\s\S]*?\]", raw)
            if not _m:
                raise
            cands = json.loads(_m.group(0))
        if isinstance(cands, dict):
            cands = cands.get("urls") or cands.get("candidates") or []
    except Exception as e:
        if log:
            log(f"⚠️ 候选入口生成失败: {e}")
        return probe
    for cand in cands[:3]:
        if not isinstance(cand, str) or not cand.startswith("http"):
            continue
        if log:
            log(f"🔍 尝试候选入口: {cand}")
        p2 = _probe_advanced(cand, log=log)
        d2 = p2.get("detect") or {}
        if p2.get("http_status") == 200 and (int(d2.get("textLen") or 0) >= 300 or d2.get("bigImages") or d2.get("pdfLinks")):
            if log:
                log(f"✅ 候选入口可用: {cand}")
            return p2
    return probe


# ---------------------------------------------------------------------------
# 战术试跑辅助
# ---------------------------------------------------------------------------
def _tactic_cookie_click_probe(meta: Dict[str, Any], url: str, limit: int,
                               log: Optional[Callable[[str], None]] = None):
    """cookie_click 试跑：跑点击，拿条目 + 推断详情链接模板。"""
    from .tactics import cookie_click_run
    params = dict(meta.get("params") or {})
    params.setdefault("host", meta.get("host"))
    rows = cookie_click_run(params, url or params.get("entry", ""), limit=limit, log=log)
    # 从跳转 URL 推断 url_template（去 id 参数）
    tmpl = params.get("url_template") or ""
    if not tmpl and rows:
        u = rows[0].get("跳转URL") or ""
        m = re.search(r"(.*?)(?:--(?:schId|infoId|orgId|itemId|pid|fid)|[?&/](?:id|Id))[-=]?[0-9A-Za-z_-]{1,40}(.*)$", u)
        if m:
            tmpl = m.group(1) + "{id}" + m.group(2)  # 占位符，cookie_click_run 会替换
    for r in rows:
        r.pop("_site", None)
    base = f"auto_precise_{_sanitize_host(meta.get('host',''))}"
    fp = ROOT / "outputs" / f"{base}.json"
    fp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows, _export_files(base), f"SPA 点击 {len(rows)} 条（已注册）"


def _tactic_pdf_probe(meta: Dict[str, Any], url: str, limit: int,
                      log: Optional[Callable[[str], None]] = None):
    from .tactics import pdf_attach_run
    params = dict(meta.get("params") or {})
    params.setdefault("host", meta.get("host"))
    rows = pdf_attach_run(params, url or params.get("entry", ""), limit=limit, log=log)
    base = f"auto_precise_{_sanitize_host(meta.get('host',''))}"
    fp = ROOT / "outputs" / f"{base}.json"
    fp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows, _export_files(base), f"PDF 附件解析 {len(rows)} 行"


def _tactic_image_probe(meta: Dict[str, Any], url: str, limit: int,
                        log: Optional[Callable[[str], None]] = None):
    from .tactics import image_ocr_run
    params = dict(meta.get("params") or {})
    params.setdefault("host", meta.get("host"))
    params.setdefault("name", meta.get("name"))
    rows = image_ocr_run(params, url or params.get("entry", ""), limit=limit, log=log)
    base = f"auto_precise_{_sanitize_host(meta.get('host',''))}"
    return rows, _export_files(base), f"图片榜单 OCR {len(rows)} 行"


def _llm_direct_extract(meta: Dict[str, Any], url: str, limit: int,
                         log: Optional[Callable[[str], None]] = None):
    """html_engine 兜底：CSS 选择器失败时，让 LLM 直接从页面文本抽取字段（anansi 自愈思想）。"""
    from .llm import LLMClient
    if log:
        log("🧠 CSS 解析未命中，改用 LLM 直接从页面文本抽取（成功即收）…")
    sample = _fetch_sample(url or (meta.get("params") or {}).get("entry", ""), log=log)
    text = sample.get("text") or ""
    desc = meta.get("description") or ""
    if not desc:
        raise RuntimeError("缺少任务描述，无法 LLM 直抽")
    llm = LLMClient()
    prompt = (
        f"任务: {desc}\n"
        f"页面文本（可能混入导航/菜单噪音，请只提取与任务相关的记录）:\n"
        f"{text[:8000]}\n\n"
        f"请提取页面中所有与任务相关的记录，输出 JSON 数组（每行一个对象，字段用中文命名，"
        f"按任务需要如：标题、日期、链接、标准号、分数、人数等）。"
        f"若页面没有相关数据输出 []。只输出 JSON，不要解释。"
    )
    raw = llm.chat([
        {"role": "system", "content": "你是数据抽取引擎，只输出合法 JSON 数组。"},
        {"role": "user", "content": prompt},
    ])
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        arr = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", raw, re.S)
        arr = json.loads(m.group(0)) if m else []
    rows = [r for r in arr if isinstance(r, dict)]
    if not rows:
        raise RuntimeError("LLM 直抽 0 行（页面可能无相关数据）")
    if not _rows_match_task(desc, rows):
        raise RuntimeError("LLM 直抽结果与任务不相关")
    base = f"auto_precise_{_sanitize_host(meta.get('host',''))}"
    fp = ROOT / "outputs" / f"{base}.json"
    fp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        import csv
        with open(ROOT / "outputs" / f"{base}.csv", "w", newline="", encoding="utf-8-sig") as f:
            keys = list(rows[0].keys())
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
    except Exception:
        pass
    if log:
        log(f"✅ LLM 直抽成功：{len(rows)} 行")
    return rows, _export_files(base), f"LLM 直抽 {len(rows)} 行"


def _tactic_engine_probe(meta: Dict[str, Any], url: str, limit: int,
                         log: Optional[Callable[[str], None]] = None):
    """html_engine 战术：用 LLM 生成 engine 配置并试跑（带任务描述）。"""
    if log:
        log("🤖 AI 正在生成 engine 配置…")
    sample = _fetch_sample(url, log=log)
    scheme = _ask_scheme(meta.get("description") or "", sample, log=log)
    scheme = _normalize_engine_cfg(scheme)
    host = meta.get("host", "")
    task_dir = _register_engine_config(scheme, host, log=log)
    probe = _run_engine_probe(task_dir, limit, log=log)
    if probe.get("error") or not probe.get("rows"):
        # CSS 兜底失败 → LLM 直抽（成功即收，不空转重跑）
        return _llm_direct_extract(meta, url or (meta.get("params") or {}).get("entry", ""), limit, log)
    return probe["rows"], probe.get("files", {}), f"engine 试跑 {len(probe['rows'])} 条"
