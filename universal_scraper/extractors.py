#!/usr/bin/env python3
"""通用内容提取（trafilatura/readability 方向，纯 lxml 实现）：
正文抽取 / 表格抽取 / HTML→Markdown。"""
from __future__ import annotations

import re
import threading
from urllib.parse import urljoin

from typing import Dict, List, Optional


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
        for tr in tbl.xpath(".//tr"):
            cells = [re.sub(r"\s+", " ", (c.text_content() or "")).strip() for c in tr.xpath(".//th | .//td")]
            if not cells:
                continue
            if tr.xpath(".//th"):
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
        code = (el.text or "").strip()
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
    for tr in el.xpath(".//tr"):
        cells = [re.sub(r"\s+", " ", (c.text_content() or "").strip()) for c in tr.xpath("./th|./td")]
        if not cells:
            continue
        key = tuple(cells)
        if key in seen:
            continue
        seen.add(key)
        if tr.xpath("./th") and header is None:
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
    if tag in ("script", "style", "nav", "aside"):
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
    _set_base_url(base_url)
    try:
        blocks = _md_blocks(body)
    finally:
        _set_base_url(None)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(blocks)).strip()
    if max_chars > 0 and len(out) > max_chars:
        out = out[:max_chars] + f"\n...(截断，共 {len(out)} 字符)"
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
        txt = re.sub(r"\s+", " ", (a.text_content() or "")).strip()[:80]
        line = f"[{txt}]({href})" if txt else f"[{href}]({href})"
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
        if len(out) >= max_links:
            break
    return out
