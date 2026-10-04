#!/usr/bin/env python3
"""通用内容提取（trafilatura/readability 方向，纯 lxml 实现）：
正文抽取 / 表格抽取 / HTML→Markdown。"""
from __future__ import annotations

import re
import threading
from urllib.parse import urljoin

from typing import Any, Dict, List, Optional


def _doc(html_text: str):
    try:
        from lxml import html
        return html.fromstring(html_text)
    except Exception:
        return None


def extract_article(html_text: str, min_par_len: int = 40) -> str:
    """正文抽取：按段落文本密度打分，取最密集的容器（readability/trafilatura 思路的轻量版）。"""
    doc = _doc(html_text)
    if doc is None:
        return re.sub(r"<[^>]+>", " ", html_text)
    # 移除 script/style/nav/aside/footer
    # 审查八轮（HIGH）：**不再整块删除 <form>**——ASP.NET WebForms（<form id="form1"
    # runat="server">）与大量政务老站的正文整块包在 form 里，drop_tree 会把正文
    # 100% 丢光（实测正文全空）。表单控件（input/button/label）本身几乎不产出文本，
    # 保留 form 带来的噪音极小，而丢正文是不可逆损失。
    for tag in ("script", "style", "nav", "aside", "footer"):
        for el in doc.xpath(f"//{tag}"):
            el.drop_tree()
    best = None
    best_score = 0
    for el in doc.xpath("//body//*"):
        if el.tag in ("p", "div", "article", "section", "td"):
            text = (el.text_content() or "").strip()
            if len(text) < min_par_len:
                continue
            # 得分 = 文本长度 * 段落密度（只统计直接子段落）
            paras = el.xpath(".//p")
            score = len(text) + 2 * len(paras) * 50
            if score > best_score:
                best_score = score
                best = el
    if best is None:
        return re.sub(r"\s+", " ", (doc.text_content() or "")).strip()
    return re.sub(r"\s+", " ", best.text_content()).strip()


def _merge_header_rows(trs) -> List[str]:
    """多行表头折成逐列标签（审查八轮，MEDIUM）。

    传进来的是一串 `<tr>` 元素（数据行之前的连续全 <th> 行）。rowspan/colspan
    布局下"行内第 i 个单元格 ≠ 第 i 列"——只按位置合并会把跨列组标签塞错列
    （实测 header [名称, 价格] + [原价, 现价] 与 3 列数据行错位）。这里按
    colspan/rowspan 真实跨度建网格，再逐列把各层标签用空格连起来：
    上例得 [名称, 价格 原价, 价格 现价]（3 列，与数据行对齐）。
    同列重复标签加数字后缀（原先重复 th 会互相覆盖 → 丢列）。
    """
    grid: Dict[Any, str] = {}
    occupied: set = set()
    for ri, tr in enumerate(trs):
        ci = 0
        for c in tr.xpath("./th | ./td"):
            while (ri, ci) in occupied:
                ci += 1
            txt = re.sub(r"\s+", " ", (c.text_content() or "")).strip()
            try:
                cs = max(1, int(c.get("colspan") or 1))
            except Exception:
                cs = 1
            try:
                rs = max(1, int(c.get("rowspan") or 1))
            except Exception:
                rs = 1
            for dr in range(rs):
                for dc in range(cs):
                    grid[(ri + dr, ci + dc)] = txt
                    if dr or dc:
                        occupied.add((ri + dr, ci + dc))
            ci += cs
    ncol = max((c for _, c in grid), default=-1) + 1
    labels: List[str] = []
    for ci in range(ncol):
        parts: List[str] = []
        for ri in range(len(trs)):
            v = grid.get((ri, ci), "")
            if v and v not in parts:
                parts.append(v)
        labels.append(" ".join(parts))
    out: List[str] = []
    seen: Dict[str, int] = {}
    used: set = set()
    for lb in labels:
        # 收官十二轮（审查 M 配套）：空标签曾算好 "col" 兜底却 append 原值 ""——
        # CsvStorage 恢复表头时过滤空列名 → 追加行左移错列。这里真正落 "col"
        key = lb or "col"
        # 收官十二轮（审查 M，实测）：后缀只与"已出现过的标签"去重，不检查是否与
        # 后面的真实列名相撞——["价格","价格","价格2"] 生成 价格/价格2/价格2 三个
        # 键，dict 建行时后者覆盖前者（第 2 列的值整列消失）。落名前对照全集
        cand = key
        n = seen.get(key, 0)
        while cand in used:
            n += 1
            cand = f"{key}{n}" if n > 1 else f"{key}2"
        seen[key] = n
        used.add(cand)
        out.append(cand)
    return out


def extract_tables(html_text: str) -> List[List[Dict[str, str]]]:
    """抽取所有表格为 [{表头: 单元格}, ...]。"""
    doc = _doc(html_text)
    if doc is None:
        return []
    out = []
    for tbl in doc.xpath("//table"):
        rows = []
        header = []
        _hdr_pending: List[Any] = []   # 数据行出现前的连续全 th 行（多行表头，存 tr 元素）
        # OCR R131（H）：.//tr/.//th 曾取到嵌套子表（descendant 轴）——外层表的
        # 行被内层表污染、子表行被误判表头。child 轴只取直接属性行（tbody 两种布局）
        # OCR R131 二轮（H）：thead 布局（<table><thead><tr>）的表头行曾整组漏掉
        # 收官六轮（审查）：补 ./tfoot/tr——tfoot 合计行曾全丢；header 只认首个
        # 全 th 行（后续 th 汇总行曾覆盖表头使键名错位）
        for tr in tbl.xpath("./thead/tr | ./tbody/tr | ./tfoot/tr | ./tr"):
            cells = [re.sub(r"\s+", " ", (c.text_content() or "")).strip() for c in tr.xpath("./th | ./td")]
            if not cells:
                continue
            # 全 <th> 行才算表头：<th scope="row"> 数据行（每行首列 th）曾把
            # 表头反复覆盖、整表 0 数据行（审查七轮 N28）
            # 收官六轮：not header 替代 header is None——初始值是 [] 不是 None
            # 审查八轮：**首个数据行之前**的连续全 th 行都收作表头（多行表头合并）；
            # 数据行之后的全 th 行（如 tfoot 合计行）仍按数据行处理。
            if tr.xpath("./th") and not tr.xpath("./td") and not rows and not header:
                _hdr_pending.append(tr)
                continue
            if _hdr_pending and not header:
                header = _merge_header_rows(_hdr_pending)
            if header:
                row = {header[i] if i < len(header) else f"col{i}": cells[i] if i < len(cells) else "" for i in range(max(len(header), len(cells)))}
            else:
                row = {f"col{i}": v for i, v in enumerate(cells)}
            rows.append(row)
        # 收官十二轮（审查 H，实测）：整表全 th（键值表 / 全列用 th 渲染的数据表）
        # 曾永远产生不了数据行 → rows 为空 → 整表从结果里静默消失。无数据行时
        # 无法判定哪行是表头，全部按数据行输出（colN 键）——不臆测表头也不丢内容
        if not rows and _hdr_pending:
            for tr in _hdr_pending:
                cells = [re.sub(r"\s+", " ", (c.text_content() or "")).strip()
                         for c in tr.xpath("./th | ./td")]
                rows.append({f"col{i}": v for i, v in enumerate(cells)})
        if rows:
            out.append(rows)
    return out


_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "table",
               "pre", "blockquote", "hr", "div", "section", "article", "main",
               "header", "footer", "figure", "details", "summary", "form", "center"}


def _inline_md(el) -> str:
    """渲染元素为一行内联 markdown（a/strong/em/code/img 等）。"""
    tag = el.tag if isinstance(el.tag, str) else ""
    if tag in ("script", "style"):
        return ""
    if tag == "br":
        return "\n"
    if tag == "img":
        alt = (el.get("alt") or "").strip()
        src = el.get("src") or el.get("data-src") or ""
        if src and _get_base():
            src = urljoin(_get_base(), src)
        return f"![{alt}]({src})" if src else alt
    if tag == "a":
        href = el.get("href") or ""
        if href and _get_base() and not href.startswith(("http://", "https://", "mailto:", "tel:", "javascript:", "#")):
            href = urljoin(_get_base(), href)
        txt = _inline_md_children(el).strip()
        return f"[{txt}]({href})" if href and txt else txt
    if tag in ("strong", "b"):
        return "**" + _inline_md_children(el).strip() + "**"
    if tag in ("em", "i"):
        return "*" + _inline_md_children(el).strip() + "*"
    if tag == "code":
        # R118 修复：el.text 只取直接文本节点——含子元素时用 text_content
        # OCR R131（M）：内容含反引号时曾提前终止 code span。
        # 审查三轮（H）：反斜杠转义在 CommonMark 无效——改双反引号包裹
        # （首尾同时是 ` 时按规范补空格分隔）
        code = (el.text_content() or "").strip()
        if "`" in code:
            pad = " " if (code.startswith("`") or code.endswith("`")) else ""
            return f"``{pad}{code}{pad}``"
        return f"`{code}`" if code else ""
    if tag == "del":
        return "~~" + _inline_md_children(el).strip() + "~~"
    return _inline_md_children(el)


def _inline_md_children(el) -> str:
    out = [el.text or ""]
    for child in el:
        out.append(_inline_md(child))
        out.append(child.tail or "")
    return "".join(out)


def _inline_text(el) -> str:
    return re.sub(r"\s+", " ", _inline_md_children(el)).strip()


def _li_inline_text(li) -> str:
    """li 的内联文本（排除嵌套 ul/ol 的污染）。"""
    parts = [li.text or ""]
    for child in li:
        if (child.tag if isinstance(child.tag, str) else "") in ("ul", "ol"):
            continue
        parts.append(_inline_md(child))
        parts.append(child.tail or "")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _list_blocks(el, tag: str) -> list:
    lines = []
    idx = 0
    for li in el.xpath("./li"):
        idx += 1
        marker = f"{idx}. " if tag == "ol" else "- "
        lines.append(marker + _li_inline_text(li))
        for sub in li:
            st = sub.tag if isinstance(sub.tag, str) else ""
            if st in ("ul", "ol"):
                for nested in _list_blocks(sub, st):
                    lines.append("    " + nested)
    return lines


def _escape_cell(v: str) -> str:
    v = v.replace("|", "\\|").replace("\n", " ").strip()
    return v


def _table_md(el) -> list:
    lines = []
    header = None
    _hp: List[Any] = []   # 多行表头暂存（rowspan/colspan，存 tr 元素）
    rows = []
    # 收官六轮（审查）：seen 全行去重曾静默丢弃合法重复数据行（N/A|N/A 出现两次
    # 第二行消失）——与 extract_tables 行为不一致，移除去重（markdown 展示层去重
    # 是信息丢失）；同时补 ./tfoot/tr、去掉 [:n] 截断（超出表头宽度的单元格曾丢失）
    for tr in el.xpath("./thead/tr | ./tbody/tr | ./tfoot/tr | ./tr"):  # 审查二轮：thead 同修
        cells = [re.sub(r"\s+", " ", (c.text_content() or "").strip()) for c in tr.xpath("./th|./td")]
        if not cells:
            continue
        # 审查八轮：数据行之前的连续全 th 行都收作表头（rowspan/colspan 多行表头
        # 曾把第二行当数据 → 表名与数据错位），与 extract_tables 同口径合并。
        if tr.xpath("./th") and not tr.xpath("./td") and header is None and not rows:
            _hp.append(tr)
            continue
        if _hp and header is None:
            header = _merge_header_rows(_hp)
        rows.append(cells)
    if not rows and header is None:
        return []
    # 收官十二轮（审查 M，实测）：表头/分隔行曾按 len(header) 写死——数据行更宽时
    # GFM 以分隔行定列数，多出的单元格被渲染器丢弃（markdown_it/markdown 实测
    # "额外"列消失）。先算最大列数 n，表头行与分隔行都补到 n 列
    _n = max([len(header)] if header is not None else []) if header is not None else 0
    _n = max([_n] + [len(r) for r in rows]) if (rows or header is not None) else 0
    # <caption> 表题曾整段丢弃——另起一行输出
    _cap = el.xpath("./caption")
    if _cap:
        _ct = _inline_text(_cap[0]).strip()
        if _ct:
            lines.append(_ct)
    if header is not None:
        _h = header + [""] * (_n - len(header))
        lines.append("| " + " | ".join(_escape_cell(c) for c in _h) + " |")
        lines.append("| " + " | ".join(["---"] * _n) + " |")
    elif rows:
        # 审查八轮（MEDIUM）：无 <th> 表头（td 当表头 / thead 里用 td）的表此前只输出
        # 数据行、缺 GFM 分隔行 → 渲染器当成普通段落，表格语义整块丢失。补一个空表头
        # + 分隔行（不丢任何数据行，与 pandas.to_markdown 对无表头表的口径一致）。
        lines.append("| " + " | ".join([""] * _n) + " |")
        lines.append("| " + " | ".join(["---"] * _n) + " |")
    # 列宽取最大（表头/数据行取宽），不做 [:n] 截断——超出表头宽度的数据单元格保留
    n = _n
    for r in rows:
        cells = r + [""] * (n - len(r))
        lines.append("| " + " | ".join(_escape_cell(c) for c in cells) + " |")
    lines.append("")
    return lines


def _md_blocks(el) -> list:
    """把元素递归渲染成 markdown 块。"""
    lines = []
    tag = el.tag if isinstance(el.tag, str) else ""
    if tag in ("script", "style", "nav", "aside", "head"):
        return []
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        lines.append("#" * int(tag[1]) + " " + _inline_text(el))
    elif tag == "p":
        txt = _inline_text(el)
        if txt:
            lines.append(txt)
    elif tag in ("ul", "ol"):
        lines.extend(_list_blocks(el, tag))
    elif tag == "table":
        lines.extend(_table_md(el))
    elif tag == "pre":
        lang = ""
        code_els = el.xpath(".//code")
        if code_els:
            cls = code_els[0].get("class") or ""
            m = re.search(r"(?:language-|lang-)([\w+-]+)", cls)
            if m:
                lang = m.group(1)
        code = (el.text_content() or "").strip("\n")
        lines.append("```" + lang)
        lines.append(code)
        lines.append("```")
    elif tag == "blockquote":
        inner = []
        for child in el:
            inner.extend(_md_blocks(child))
        if not inner:
            inner = [re.sub(r"\s+", " ", (el.text_content() or "")).strip()]
        for line in inner:
            lines.append("> " + line)
    elif tag == "hr":
        lines.append("---")
    else:
        # 块级容器：先处理元素自身文本，再递归子块
        buf = el.text or ""
        for child in el:
            if (child.tag if isinstance(child.tag, str) else "") in _BLOCK_TAGS:
                if buf.strip():
                    lines.append(re.sub(r"\s+", " ", buf).strip())
                    buf = ""
                lines.extend(_md_blocks(child))
                # OCR R131（M）：块级子元素之后的尾随文本（child.tail）曾整段丢失，
                # 作为下一段缓冲的开头续上
                buf = child.tail or ""
            else:
                buf += _inline_md(child) + (child.tail or "")
        if buf.strip():
            lines.append(re.sub(r"\s+", " ", buf).strip())
    return lines


# base_url 用线程本地存储（WebUI 多线程并发调用 html_to_markdown 不会串）
_base_state = threading.local()


def _get_base() -> Optional[str]:
    return getattr(_base_state, "url", None)


def _set_base_url(url: Optional[str]):
    _base_state.url = url


def html_to_markdown(html_text: str, base_url: Optional[str] = None,
                     max_chars: int = 0) -> str:
    """HTML→Markdown（对标 crawl4ai/Firecrawl/Scrapling：
    标题/嵌套列表/GFM 表格/代码块/引用/图片/行内格式/相对链接转绝对）。
    - base_url: 把相对链接/图片转成绝对地址
    - max_chars: >0 时截断输出并加提示（省 token）
    """
    doc = _doc(html_text)
    if doc is None:
        return re.sub(r"<[^>]+>", " ", html_text)
    body = doc.body if doc.body is not None else doc
    # 审查八轮（HIGH）：同 extract_article——不再删 <form> 子树（ASP.NET/政务站
    # 正文包在 form 里，删了 markdown 直接为空；表单控件本身几乎无文本）。
    for tag in ("script", "style", "nav", "aside", "footer"):
        for el in body.xpath(f"//{tag}"):
            el.drop_tree()
    # OCR R6：先存旧值、finally 恢复——无条件置 None 曾在嵌套/重入调用时
    # 把外层调用的 base_url 清掉（外层相对链接静默不再转绝对）
    _prev_base = _get_base()
    _set_base_url(base_url)
    try:
        blocks = _md_blocks(body)
    finally:
        _set_base_url(_prev_base)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(blocks)).strip()
    if max_chars > 0 and len(out) > max_chars:
        # 审查四轮（M）：截断后 f-string 才求值——len(out) 报的是截断后长度
        _total = len(out)
        out = out[:max_chars] + f"\n...(截断，共 {_total} 字符)"
    return out


def extract_links_markdown(html_text: str, base_url: Optional[str] = None,
                           max_links: int = 200) -> List[str]:
    """抽取页面里的链接为 Markdown 行（[文字](url)），供 LLM 快速理解导航结构（省 token）。"""
    doc = _doc(html_text)
    if doc is None:
        return []
    out: List[str] = []
    seen = set()
    for a in doc.xpath("//a[@href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        if base_url and not href.startswith(("http://", "https://")):
            href = urljoin(base_url, href)
        _full_txt = re.sub(r"\s+", " ", (a.text_content() or "")).strip()
        # OCR R131（M）：截断后的文本做去重键曾把"前 80 字相同的不同链接"误并——
        # 去重按 (全文本, href)，展示才截断
        if (_full_txt, href) in seen:
            continue
        seen.add((_full_txt, href))
        txt = _full_txt[:80]
        line = f"[{txt}]({href})" if txt else f"[{href}]({href})"
        out.append(line)
        if len(out) >= max_links:
            break
    return out


# ---------------------------------------------------------------- 表单自动发现
# R25 对标 MechanicalSoup：自动解析页面表单结构，小白不用"审查元素猜选择器"

def discover_forms(html_text: str, base_url: str = "") -> List[Dict[str, Any]]:
    """解析 HTML 中所有 <form> 并返回结构化定义（字段名/类型/提交地址/方法）。

    返回 [{"action": "...", "method": "POST", "fields": [
        {"name": "user", "type": "text", "label": "用户名", "required": True}, ...
    ]}]"""
    from lxml import html as _lh
    out: List[Dict[str, Any]] = []
    try:
        doc = _lh.fromstring(html_text)
    except Exception:
        return out
    for form_el in doc.iter("form"):
        action = form_el.get("action") or ""
        if action and base_url:
            action = urljoin(base_url, action)
        method = (form_el.get("method") or "GET").upper()
        fields: List[Dict[str, Any]] = []
        for inp in form_el.iter("input", "select", "textarea"):
            tag = inp.tag.lower() if isinstance(inp.tag, str) else ""
            # 收官十二轮（审查 L）：无 type 的 <input> 是合法 HTML（默认 text）——
            # 曾返回不存在的类型名 "input"；docstring 也承诺 "text"
            it = inp.get("type") or ("text" if tag == "input" else tag)
            nm = inp.get("name") or inp.get("id") or ""
            if not nm:
                continue
            f: Dict[str, Any] = {
                "name": nm, "type": it,
                "required": inp.get("required") is not None,
            }
            label = inp.get("placeholder") or inp.get("aria-label") or ""
            if label:
                f["label"] = label
            if tag == "select":
                # 收官十二轮（审查 L）：`value=""` 是合法空值（"请选择"占位项）——
                # 曾回退成可见文本，value/label 混在一起。仅 value 缺失（None）才用文本
                opts = []
                for o in inp.iter("option"):
                    _v = o.get("value")
                    if _v is None:
                        _v = (o.text_content() or "").strip()
                    opts.append(_v)
                f["options"] = opts[:20]
            elif it in ("hidden",):
                f["value"] = inp.get("value", "")
            fields.append(f)
        out.append({
            "action": action,
            "method": method,
            "id": form_el.get("id", ""),
            "fields": fields,
        })
    return out


# ---------------------------------------------------------------------------
# AI 消费前的净化与收窄（审查二十轮 R20）
#
# 背景：页面内容进 LLM/Agent 之前必须当作**不可信数据**——隐藏元素、注释、
# 零宽字符都是提示注入（prompt injection）的常见载体。本组函数只服务
# "页面内容 → LLM/Agent"链路；用户数据抽取路径（html_to_markdown /
# extract_article / extract_tables 等）行为不变，避免动到已交付数据。
#
# 对标 Scrapling 的 _sanitize_for_ai / _strip_noise_tags，并修正其一个已知误伤：
# 它用 contains(@style,"opacity:0") 判隐藏，会把 opacity:0.5 的可见元素也删掉——
# 这里按属性值边界解析，0.5 / 10px 不受影响；并补上它未覆盖的 HTML5 hidden 属性。
# ---------------------------------------------------------------------------

# 注意 height/width 需要前置边界（^ 或 ;），否则 "line-height:0" 会被误判
_HIDE_STYLE_RES = tuple(re.compile(p) for p in (
    r"display\s*:\s*none",
    r"visibility\s*:\s*(?:hidden|collapse)",
    r"opacity\s*:\s*0(?:\.0*)?\s*(?:!important)?\s*(?:;|$)",
    r"font-size\s*:\s*0(?:\.0*)?\s*(?:px|em|rem|%)?\s*(?:!important)?\s*(?:;|$)",
    r"(?:^|;)\s*(?:max-)?height\s*:\s*0(?:\.0*)?\s*(?:px|em|rem|%)?\s*(?:!important)?\s*(?:;|$)",
    r"(?:^|;)\s*(?:max-)?width\s*:\s*0(?:\.0*)?\s*(?:px|em|rem|%)?\s*(?:!important)?\s*(?:;|$)",
))
# 零宽字符（U+200B-200F 零宽空格/连接符/方向标记、U+202A-202E 双向控制、
# U+2060-2064 词连接/不可见运算符、U+FEFF BOM）+ C0/C1 控制字符
_ZWC_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_CTRL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_AI_NOISE_TAGS = ("script", "style", "noscript", "template", "svg", "iframe")


def scrub_text(text: str) -> str:
    """剥零宽/控制字符（注入常用载体，可见正文语义不受影响）。"""
    if not text:
        return text
    return _CTRL_RE.sub("", _ZWC_RE.sub("", text))


def _is_hidden_element(el) -> bool:
    style = (el.get("style") or "").lower()
    if style and any(rx.search(style) for rx in _HIDE_STYLE_RES):
        return True
    if (el.get("aria-hidden") or "").strip().lower() == "true":
        return True
    if el.get("hidden") is not None:      # HTML5 hidden 属性（Scrapling 未覆盖）
        return True
    return False


def strip_hidden_content(html_text: str) -> str:
    """移除不可见内容并剥零宽字符，返回净化后的 HTML。

    处理：CSS 隐藏（display:none / visibility:hidden|collapse / opacity:0 /
    font-size:0 / height:0 / width:0）、aria-hidden="true"、hidden 属性、
    <template>/<script>/<style>/<noscript>/<svg>/<iframe>、HTML 注释
    （注入常藏 <!-- 指令 -->）、零宽与控制字符。
    lxml 不可用时退回正则级（注释 + 零宽字符）。"""
    src = html_text or ""
    doc = _doc(src)
    if doc is None:
        return scrub_text(re.sub(r"<!--.*?-->", "", src, flags=re.S))
    # 注释节点
    for node in doc.xpath("//comment()"):
        parent = node.getparent()
        if parent is not None:
            try:
                parent.remove(node)
            except Exception:
                pass
    for el in list(doc.iter()):
        if not isinstance(el.tag, str):
            continue
        try:
            if el.tag in _AI_NOISE_TAGS or _is_hidden_element(el):
                el.drop_tree()
        except Exception:
            pass
    for el in doc.iter():
        if isinstance(el.tag, str):
            if el.text:
                el.text = scrub_text(el.text)
            if el.tail:
                el.tail = scrub_text(el.tail)
    try:
        from lxml import etree as _etree
        return _etree.tostring(doc, encoding="unicode", method="html")
    except Exception:
        return scrub_text(re.sub(r"<!--.*?-->", "", src, flags=re.S))


_MAIN_SEMANTIC_XPATHS = (
    ".//main", ".//article", ".//*[@role='main']", ".//*[@role=\"main\"]",
    ".//*[contains(@class,'article-content') or contains(@class,'article_content')]",
    ".//*[contains(@class,'post-content') or contains(@class,'entry-content')]",
    ".//*[contains(@class,'main-content') or contains(@class,'main_content')]",
)


def main_content_html(html_text: str, min_chars: int = 300) -> str:
    """收窄到主内容容器（语义容器 → 文本密度打分 → 回退全文），返回 HTML 字符串。

    拿不准时宁可不收窄（回退全文/body）——把正文收没了比多送点 token 更糟。"""
    doc = _doc(html_text)
    if doc is None:
        return html_text
    body = doc.body if doc.body is not None else doc
    total = len((body.text_content() or "").strip())

    def _ser(el) -> str:
        try:
            from lxml import etree as _etree
            return _etree.tostring(el, encoding="unicode", method="html")
        except Exception:
            return html_text

    # 1) 语义容器（main/article/[role=main]/常见内容 class）取文本最长者
    best_el, best_len = None, 0
    for xp in _MAIN_SEMANTIC_XPATHS:
        try:
            for el in body.xpath(xp):
                n = len((el.text_content() or "").strip())
                if n > best_len:
                    best_el, best_len = el, n
        except Exception:
            continue
    # 语义容器成立条件：绝对长度够，或"短但主导全页"（很多真页正文只有两三百字，
    # 硬卡 300 会一律回退全文，收窄形同虚设——但主导性门槛必须保住）
    if best_el is not None and (best_len >= min_chars
                                or (best_len >= 120 and total and best_len >= 0.5 * total)):
        return _ser(best_el)

    # 2) 文本密度打分（extract_article 同款口径：长度 + 2×直接子段落数×50）
    for el in body.iter():
        if not isinstance(el.tag, str) or el.tag not in ("p", "div", "article", "section", "td"):
            continue
        text = (el.text_content() or "").strip()
        if len(text) < 40:
            continue
        try:
            paras = len(el.xpath(".//p"))
        except Exception:
            paras = 0
        score = len(text) + 2 * paras * 50
        if score > best_len and len(text) >= min_chars:
            best_el, best_len = el, score
    # 密度最优块须占到全页文本的足够比例，否则视为"内容分散"→ 不收窄
    if best_el is not None and best_len and total and len((best_el.text_content() or "").strip()) >= max(min_chars, int(total * 0.25)):
        return _ser(best_el)
    return _ser(body)


def ai_safe_markdown(html_text: str, base_url: Optional[str] = None,
                     max_chars: int = 0, main_only: bool = True) -> str:
    """AI/Agent 消费专用 Markdown：先净化、再收窄、后转换。

    顺序不可颠倒——先收窄的话，隐藏的垃圾文本会参与密度评分，把真正文挤出
    主容器选择（等于被注入内容牵着走）。"""
    cleaned = strip_hidden_content(html_text)
    narrowed = main_content_html(cleaned) if main_only else cleaned
    return html_to_markdown(narrowed, base_url=base_url, max_chars=max_chars)
