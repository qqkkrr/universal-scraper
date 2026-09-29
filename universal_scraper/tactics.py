#!/usr/bin/env python3
"""🎯 战术库：一键自动精配的“武器库”。

每个战术 = 一类数据形态的标准解法。探测阶段识别站点特征后，
决策器选一个战术，生成战术参数（meta），注册为 run 型精配并试跑。

战术清单：
  html_engine   常规 HTML/JSON 列表+详情（复用通用引擎配置）
  cookie_click  先种 Cookie 过 WAF，再在 SPA/Vue 列表里点击项捕获 id（阳光高考）
  pdf_attach    详情/页面含 PDF 附件 → 下载 → 文本抽取 → LLM 结构化（工信部）
  image_ocr     页面含榜单大图 → 下载 → 视觉 OCR → 行（中华商标协会）
  login_required 登录墙/验证码 → 提示人工
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "configs"


def sanitize_host(host: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "_", host or "").strip("_") or "site"


def run_bridge_jsonl(bridge: Path, params: Dict[str, Any], timeout: int = 300) -> List[Dict[str, Any]]:
    """跑桥，返回全部 JSONL 对象。"""
    from .browser import run_bridge, BrowserBridgeError
    objs = []
    try:
        for obj in run_bridge(bridge, {k: str(v) for k, v in params.items()}, timeout=timeout):
            objs.append(obj)
    except BrowserBridgeError as e:
        raise RuntimeError(f"战术桥失败：{e}") from e  # OCR R131（M）：补链保住原始栈
    return objs


# ---------------------------------------------------------------------------
# 战术 1：cookie_click —— 种 Cookie 过 WAF + SPA 点击捕获 id
# ---------------------------------------------------------------------------
def cookie_click_run(meta: Dict[str, Any], url: str, limit: int = 200,
                     log: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
    bridge = ROOT / "scripts" / "tactic_bridge.cjs"
    # R128 修复：host 为 None/空时曾产生 "https:///" 无效 URL
    seed = meta.get("seed") or ""
    if not seed:
        _h = (meta.get("host") or "").split("//")[-1]
        seed = f"https://{_h}/" if _h else ""
    item_css = meta.get("item_css") or ".sch-item"
    # run_site 默认 limit=20 视为“未指定”：点击列表尽量全量（500）
    # 审查修复：int(limit or 0) 曾对非数字串直接 ValueError——对齐下方
    # pdf_attach_run 的容错口径（给了正数就用之，未给/非法按未指定回退 500）
    try:
        _lim = int(limit)
    except (TypeError, ValueError):
        _lim = 0
    _max = 500 if _lim <= 20 else _lim
    # OCR R131（H）：seed/target 双空时曾照样起 600s 浏览器桥——注定 0 条还
    # 烧一次冷启动（对齐 pdf_attach_run 的 R128 入口校验）
    _target = url or meta.get("entry", "")
    if not seed and not _target:
        raise ValueError("cookie_click 缺少 seed/host 与 target/url——无法定位页面，拒绝起桥")
    objs = run_bridge_jsonl(bridge, {
        "mode": "click_ids", "seed": seed, "target": _target,
        "item_css": item_css, "max": str(_max), "settle": "800",
    }, timeout=600)
    items = [o for o in objs if o.get("type") == "item"]
    if not items:
        raise RuntimeError("SPA 点击 0 条（可能没有可点击项或页面结构变化）")
    if log:  # OCR R131（M）：log 形参曾声明不用——补过程可见性
        log(f"🖱️ SPA 点击捕获 {len(items)} 项")
    tmpl = meta.get("url_template") or ""
    rows = []
    for it in items:
        row = {"文本": it.get("text", ""), "id": it.get("id", ""), "跳转URL": it.get("url", "")}
        if tmpl and it.get("id"):
            row["详情链接"] = tmpl.replace("{id}", str(it.get("id")))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 战术 2：pdf_attach —— PDF 附件下载 + 文本抽取 + LLM 结构化
# ---------------------------------------------------------------------------
def pdf_attach_run(meta: Dict[str, Any], url: str, limit: int = 20,
                   log: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
    """PDF 战术：PDF 直链 / 页面 PDF 附件 → 通用解析（表格直出，文本才 LLM）。"""
    from .pdf_table import attachment_to_rows, extract_pdf_links, is_attachment_url
    from .llm import LLMClient
    entry = url or meta.get("entry", "") or ""
    if not entry:
        return []  # R128 修复：无 URL 时直接返回空（下游会报 0 条）
    fields = meta.get("fields") or []
    proxy = meta.get("proxy") or None
    # 1) 候选附件：直链（pdf/xlsx/docx）> HTTP 快扫 HTML > 浏览器兜底
    pdfs: List[str] = []
    if is_attachment_url(entry):
        pdfs = [entry]
    if not pdfs:
        # R20 修复：快扫改走 sites.fetch_html（协议闸 + 超时内建）——此前裸
        # requests 直连且关了证书校验，探测结果会喂给后续附件下载，未校验
        # 通道的 MITM 内容会污染整条战术链
        from .sites import fetch_html as _fetch_html
        res = _fetch_html(entry, timeout=25)
        if res.get("ok"):
            pdfs = extract_pdf_links(res.get("html", ""), entry)
    if not pdfs:
        bridge = ROOT / "scripts" / "tactic_bridge.cjs"
        seed = meta.get("seed") or ""
        try:
            objs = run_bridge_jsonl(bridge, {
                "mode": "pdfs", "seed": seed, "target": entry, "settle": "800",
            }, timeout=300)
            pdfs = [o.get("url", "") for o in objs if o.get("type") == "pdf" and o.get("url")]
        except Exception as e:
            if log:
                log(f"⚠️ 浏览器找附件失败：{e}")
    if not pdfs:
        raise RuntimeError("页面未找到 PDF/附件（可能需要登录或页面结构变化）")
    # 审查二轮（M）：原 `20 if limit<=20 else limit` 把显式 limit=10 放大成 20。
    # 给了正数就用之（含 <20），未给/非法回退 20
    try:
        _max = int(limit)
        if _max <= 0:
            raise ValueError
    except (TypeError, ValueError):
        _max = 20
    rows: List[Dict[str, Any]] = []
    llm = LLMClient()
    for i, pu in enumerate(pdfs[:_max], 1):
        if not pu.startswith("http"):
            # R20 修复：相对 URL 用 urljoin 解析——旧拼接 "//".join(...) 会产出
            # "https:////host"（无 host），requests 直接 InvalidURL，浏览器桥
            # pdfs 模式返回的 iframe 原始 src 全部被静默跳过
            from urllib.parse import urljoin as _urljoin
            pu = _urljoin(entry, pu)
        try:
            res = attachment_to_rows(pu, proxy=proxy, fields=fields or None)
            if res.get("kind") == "table":
                rows.extend(res.get("rows") or [])
                if log:
                    log(f"✅ 附件表格直出 {len(res.get('rows') or [])} 行（{pu}）")
                continue
            if res.get("kind") == "text":
                txt = (res.get("text") or "").strip()
                if len(txt) < 30:
                    continue
                # 文本型 → LLM 结构化（表格型不需要烧 LLM）
                schema = {f: "提取内容" for f in (fields or ["内容"])}
                try:
                    parsed = llm.extract_json(
                        f"这是附件文本（可能含表格），请按每一条记录输出一个对象。附件URL: {pu}\n\n文本:\n{txt[:6000]}",
                        schema, instruction=f"从以下文本中提取{len(schema)}个字段的列表，输出 JSON 数组。")
                    arr = parsed if isinstance(parsed, list) else [parsed]
                    for item in arr:
                        if isinstance(item, dict):
                            item.setdefault("_pdf", pu)
                            rows.append(item)
                except Exception as e:
                    if log:
                        log(f"⚠️ 第{i}个附件文本结构化失败: {e}")
                    rows.append({**{f: "" for f in (fields or ["内容"])}, "_pdf": pu, "_raw": txt[:500]})
                continue
            # OCR R131（L）：未知 kind 曾打出 "⚠️ None"——带 kind 便于诊断
            if log:
                log(f"⚠️ 附件返回未处理类型 kind={res.get('kind')}: {res.get('error') or '(无错误信息)'}")
        except Exception as e:
            if log:
                log(f"⚠️ 第{i}个附件处理失败: {type(e).__name__}: {e}")
    if not rows:
        raise RuntimeError("PDF 解析 0 行")
    return rows


# ---------------------------------------------------------------------------
# 战术 3：image_ocr —— 榜单大图 OCR（复用 precise_auto 的实现）
# ---------------------------------------------------------------------------
def image_ocr_run(meta: Dict[str, Any], url: str, limit: int = 20,
                  log: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
    from .precise_auto import _image_ranking_run
    m = dict(meta)
    m.setdefault("name", f"auto_precise_{sanitize_host(meta.get('host',''))}")
    return _image_ranking_run(m, url or meta.get("entry", ""), limit=limit, log=log)


# ---------------------------------------------------------------------------
# 战术注册表（决策器用）
# ---------------------------------------------------------------------------
TACTIC_INFO = {
    "html_engine": {"desc": "常规 HTML/JSON 列表+详情", "needs": ["start_urls"]},
    "cookie_click": {"desc": "WAF + SPA 点击捕获 id", "needs": ["seed", "entry", "item_css", "url_template"]},
    "pdf_attach": {"desc": "PDF 附件下载+LLM结构化", "needs": ["entry", "fields"]},
    "image_ocr": {"desc": "榜单大图 OCR", "needs": ["entry", "img_src_hint"]},
    "login_required": {"desc": "登录墙/验证码", "needs": []},
}


def decide_tactic(desc: str, detect: Dict[str, Any]) -> Dict[str, Any]:
    """规则优先 + LLM 兜底，返回 {tactic, params}。
    OCR R131（M）：log 参数曾收下但函数体从不使用——删死参数（调用方同步）。"""
    from .pdf_table import is_pdf_url
    d = detect or {}
    text_len = int(d.get("textLen") or 0)
    has_vue = bool(d.get("hasVue"))
    pdfs = d.get("pdfLinks") or []
    imgs = d.get("bigImages") or []
    tables = d.get("tables") or []

    # 1) 登录墙：文本极少 + 登录关键词
    if text_len < 200 and d.get("loginText"):
        return {"tactic": "login_required", "params": {}, "reason": "页面像登录墙/验证码拦截"}
    # 2) PDF 附件 / PDF 直链
    is_direct = is_pdf_url(d.get("_url") or "")
    if pdfs or is_direct:
        return {"tactic": "pdf_attach",
                "params": {"entry": d.get("_url", ""), "direct_pdf": is_direct},
                "reason": "PDF 直链" if is_direct else f"检测到 {len(pdfs)} 个 PDF/附件"}
    # 3) 图片榜单：任务提到榜单/排名/价值 且页面有大图
    if imgs and re.search(r"榜单|排行|排名|价值|排行榜|Top|top\d*", desc):
        return {"tactic": "image_ocr", "params": {"entry": d.get("_url", "")},
                "reason": f"任务像榜单，检测到 {len(imgs)} 张大图"}
    # 4) Vue SPA + 有列表文本：点击捕获
    if has_vue and text_len > 300:
        return {"tactic": "cookie_click", "params": {"entry": d.get("_url", ""), "item_css": ".sch-item"},
                "reason": "Vue/SPA 列表页，用点击捕获 id"}
    # 5) HTML 表格
    if tables:
        return {"tactic": "html_engine", "params": {"entry": d.get("_url", "")},
                "reason": f"页面含 {len(tables)} 个 HTML 表格"}
    # 6) 兜底：常规列表
    return {"tactic": "html_engine", "params": {"entry": d.get("_url", "")},
            "reason": "常规 HTML 页面，走通用引擎"}
