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
    for tag in ("script", "style", "nav", "aside", "footer", "form"):
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


def extract_tables(html_text: str) -> List[List[Dict[str, str]]]:
    """抽取所有表格为 [{表头: 单元格}, ...]。"""
    doc = _doc(html_text)
    if doc is None:
        return []
    out = []
    for tbl in doc.xpath("//table"):
        rows = []
        header = []
        # OCR R131（H）：.//tr/.//th 曾取到嵌套子表（descendant 轴）——外层表的
        # 行被内层表污染、子表行被误判表头。child 轴只取直接属性行（tbody 两种布局）
        # OCR R131 二轮（H）：thead 布局（<table><thead><tr>）的表头行曾整组漏掉
        for tr in tbl.xpath("./thead/tr | ./tbody/tr | ./tr"):
            cells = [re.sub(r"\s+", " ", (c.text_content() or "")).strip() for c in tr.xpath("./th | ./td")]
            if not cells:
                continue
            # 全 <th> 行才算表头：<th scope="row"> 数据行（每行首列 th）曾把
            # 表头反复覆盖、整表 0 数据行（审查七轮 N28）
            if tr.xpath("./th") and not tr.xpath("./td"):
                header = cells
                continue
            if header:
                row = {header[i] if i < len(header) else f"col{i}": cells[i] if i < len(cells) else "" for i in range(max(len(header), len(cells)))}
            else:
                row = {f"col{i}": v for i, v in enumerate(cells)}
            rows.append(row)
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
    rows = []
    seen = set()
    # OCR R131（H）：.//tr 曾取到嵌套子表——与 extract_tables 同口径改 child 轴
    for tr in el.xpath("./thead/tr | ./tbody/tr | ./tr"):  # 审查二轮：thead 同修
        cells = [re.sub(r"\s+", " ", (c.text_content() or "").strip()) for c in tr.xpath("./th|./td")]
        if not cells:
            continue
        key = tuple(cells)
        if key in seen:
            continue
        seen.add(key)
        if tr.xpath("./th") and not tr.xpath("./td") and header is None:
            header = cells
        else:
            rows.append(cells)
    if not rows and header is None:
        return []
    if header is not None:
        lines.append("| " + " | ".join(_escape_cell(c) for c in header) + " |")
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")
        n = len(header)
    else:
        n = max(len(r) for r in rows)
    for r in rows:
        cells = (r + [""] * (n - len(r)))[:n]
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
    for tag in ("script", "style", "nav", "aside", "footer", "form"):
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
            it = inp.get("type", tag)
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
                opts = [o.get("value") or (o.text_content() or "").strip()
                        for o in inp.iter("option") if o.get("value") is not None or o.text_content()]
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
