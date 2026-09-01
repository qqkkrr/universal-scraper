#!/usr/bin/env python3
"""HTML/JSON 结构摘要（对标 heldernoid/scrapping 的 generate-config 思路）。

给 LLM 生成爬虫配置前，先抓一次入口页，把页面**结构**（标签/class/id 统计、
候选列表行、链接模式、JSON 字段样例）压缩成一段小摘要，而不是把整页源码塞给
LLM——省 token、首轮配置更准、选择器命中率更高。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from .log import Logger

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _fetch(url: str, timeout: Optional[int] = None) -> Optional[Dict[str, Any]]:
    import os as _os
    timeout = timeout or int(_os.environ.get("US_PROBE_TIMEOUT", "12"))
    """轻量探测抓取：优先 HTTP，失败/JS 页面时交给调用方决定。"""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        # 显式直连（绕过 Clash 等系统代理，避免探测被代理挂起/干扰）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(5 * 1024 * 1024)  # 探测只看结构，最多 5MB
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "json" in ctype:
                try:
                    return {"kind": "json", "data": json.loads(raw.decode("utf-8", "replace"))}
                except Exception:
                    return {"kind": "json", "data": raw.decode("utf-8", "replace")[:2000]}
            from .core import smart_decode
            enc = r.headers.get_content_charset() or "utf-8"
            return {"kind": "html", "text": smart_decode(raw, {"content-type": enc})}
    except Exception as e:
        return {"kind": "error", "error": f"{type(e).__name__}: {e}"}


def summarize_html(html: str, url: str, max_classes: int = 25, max_ids: int = 15) -> str:
    """从 HTML 提炼：标签统计、高频 class/id、候选列表行、详情链接模式。"""
    tags = Counter(re.findall(r"<([a-zA-Z][\w-]*)\b", html))
    top_tags = [t for t, c in tags.most_common(20) if c >= 3]

    classes = Counter(re.findall(r'class="([^"]+)"', html))
    flat = Counter()
    for v in classes:
        for c in v.split():
            flat[c] += classes[v]
    top_classes = [(c, n) for c, n in flat.most_common(max_classes) if n >= 3]

    ids = Counter(re.findall(r'id="([^"]+)"', html))
    top_ids = [(i, n) for i, n in ids.most_common(max_ids) if n >= 2]

    # 候选列表行：出现 >=3 次的“重复块”标签（div/li/tr/article）
    row_candidates: List[str] = []
    for cls, n in top_classes:
        if n >= 3:
            row_candidates.append(f".{cls}")
    for tag in ("li", "tr", "article", "div", "tr.row"):
        if tags.get(tag, 0) >= 3 and tag not in ("div",):
            row_candidates.append(tag)
    if tags.get("tr", 0) >= 3:
        row_candidates.append("table tr")
    if tags.get("li", 0) >= 5:
        row_candidates.append("ul li")

    # 详情链接模式：提取指向其它页面的相对链接（去掉静态资源）
    hrefs = re.findall(r'href="([^"#]+)"', html)
    detail_links = []
    seen_href = set()
    for h in hrefs:
        if h.startswith(("javascript:", "mailto:", "tel:")):
            continue
        if re.search(r"\.(css|js|png|jpe?g|gif|svg|ico|woff2?)(\?|$)", h, re.I):
            continue
        if h not in seen_href:
            seen_href.add(h)
            detail_links.append(h)
        if len(detail_links) >= 8:
            break

    # 翻页/分页链接：优先找 rel=next、Next/下一页/» 锚文本、page-\d+/page=\d+ 模式
    next_links: List[str] = []
    for m in re.finditer(r'<a[^>]+href="([^"#]+)"[^>]*>([^<]{0,40})</a>', html, re.I | re.S):
        h, txt = m.group(1).strip(), re.sub(r"\s+", " ", m.group(2)).strip()
        if h.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        anchor_txt = m.group(0) + " " + txt
        if re.search(r"rel=.?next|下一页|下页|next|»|›", anchor_txt, re.I) \
                or re.search(r"(page[-_]?\d+|/page/\d+|\?page=|\?p=|offset=|start=)", h, re.I):
            full = urljoin(url, h)
            next_links.append(f"{full}  [{txt[:20]}]" if txt else full)
    next_links = list(dict.fromkeys(next_links))[:6]

    lines = [
        f"页面: {url}",
        f"大小: {len(html)} 字符",
        f"高频标签: {', '.join(top_tags[:12])}",
    ]
    if next_links:
        lines.append("翻页链接(务必据此写 extract_links.allow): " + " | ".join(next_links))
    if top_classes:
        lines.append("高频 class: " + ", ".join(f".{c}({n})" for c, n in top_classes[:12]))
    if top_ids:
        lines.append("高频 id: " + ", ".join(f"#{i}({n})" for i, n in top_ids[:8]))
    if row_candidates:
        lines.append("候选列表行(重复元素): " + " | ".join(list(dict.fromkeys(row_candidates))[:12]))
    if detail_links:
        lines.append("链接样例: " + " | ".join(detail_links[:6]))
    return "\n".join(lines)


def summarize_json(data: Any, depth: int = 0) -> str:
    """JSON 结构摘要：类型 + 首条记录的字段路径/类型 + 样例值。"""
    if isinstance(data, list):
        if not data:
            return "JSON: 空数组"
        head = data[0]
        if isinstance(head, dict):
            fields = []
            for k, v in list(head.items())[:20]:
                vt = type(v).__name__
                sample = str(v)[:40] if v not in (None, "") else ""
                fields.append(f"{k}({vt}){('=' + sample) if sample else ''}")
            return f"JSON: 数组，共 {len(data)} 条；首条字段: " + ", ".join(fields)
        return f"JSON: 数组，共 {len(data)} 条；元素类型 {type(head).__name__}"
    if isinstance(data, dict):
        fields = []
        for k, v in list(data.items())[:20]:
            vt = type(v).__name__
            if isinstance(v, list):
                vt = f"list[{len(v)}]"
            sample = str(v)[:40] if v not in (None, "") else ""
            fields.append(f"{k}({vt}){('=' + sample) if sample else ''}")
        return "JSON: 对象；字段: " + ", ".join(fields)
    return f"JSON: {type(data).__name__} = {str(data)[:120]}"


def build_structure_summary(url: str, logger: Optional[Logger] = None) -> Optional[str]:
    """入口 URL → 结构摘要（失败返回 None，不阻塞后续流程）。"""
    try:
        res = _fetch(url)
        if res["kind"] == "json":
            return summarize_json(res.get("data"))
        if res["kind"] == "html":
            return summarize_html(res.get("text", ""), url)
        if logger:
            logger.warn(f"结构探测失败: {res.get('error')}")
        return None
    except Exception as e:
        if logger:
            logger.warn(f"结构探测异常: {e}")
        return None


def extract_urls(text: str) -> List[str]:
    """从任务描述里找出 http(s) 链接。"""
    return list(dict.fromkeys(re.findall(r"https?://[^\s'\"，。；、()【】]+", text or "")))


if __name__ == "__main__":
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else "https://quotes.toscrape.com/"
    s = build_structure_summary(u)
    print(s or "NO SUMMARY")


# ---------------------------------------------------------------------------
# 带 CSS 选择器标注的 DOM 摘要（对标 scrapedown：HTML→Markdown+选择器路径）
# ---------------------------------------------------------------------------

_LAYOUT_CLASS = re.compile(
    r"^(clear|clearfix|container|wrapper|content|main|header|footer|nav|menu|icon|btn|button|"
    r"arrow|arrow-down|arrow-up|hot|top|left|right|center|fl|fr|hidden|active|hover|selected|"
    r"current|empty|none|more|less|tip|mask|bg|img|pic|thumb|scroll|loader|loading|pagination|page-?list)$",
    re.I)


def _css_path(el) -> str:
    """给 lxml 元素生成稳定的 CSS 路径（含 id/class/nth-child）。"""
    parts = []
    node = el
    while node is not None:
        if not isinstance(node.tag, str) or node.tag in ("html", "body"):
            break
        tag = node.tag
        seg = tag
        if node.get("id"):
            seg = f"{tag}#{node.get('id')}"
        elif node.get("class"):
            cls = ".".join(c for c in (node.get("class") or "").split() if c)
            if cls:
                seg = f"{tag}.{cls}"
        # nth-child 消歧
        parent = node.getparent()
        if parent is not None:
            same = [c for c in parent if c.tag == tag]
            if len(same) > 1:
                idx = same.index(node) + 1
                seg += f":nth-child({idx})"
        parts.append(seg)
        node = parent
    parts.reverse()
    return " > ".join(parts)


def _rel_path(row, el) -> str:
    """行内相对 CSS 路径（相对 row）。"""
    parts = []
    node = el
    while node is not None and node is not row:
        if not isinstance(node.tag, str):
            break
        tag = node.tag
        seg = tag
        if node.get("class"):
            cls = ".".join(c for c in (node.get("class") or "").split() if c)
            if cls:
                seg = f"{tag}.{cls}"
        parent = node.getparent()
        if parent is not None:
            same = [c for c in parent if c.tag == tag]
            if len(same) > 1:
                seg += f":nth-child({same.index(node) + 1})"
        parts.append(seg)
        node = parent
    parts.reverse()
    return " > ".join(parts) if parts else tag


def _row_field_hints(row, url: str, max_fields: int = 12) -> List[str]:
    """行内字段样例：相对选择器 => 文本/href/src（省 token 的 scrapedown 输出）。"""
    out: List[str] = []
    for el in row.iterdescendants():
        if not isinstance(el.tag, str):
            continue
        if el.tag in ("script", "style", "noscript"):
            continue
        cls = " ".join((el.get("class") or "").split()).lower()
        if cls and all(_LAYOUT_CLASS.fullmatch(c) for c in cls.split()):
            continue
        txt = re.sub(r"\s+", " ", (el.text_content() or "")).strip()
        href = el.get("href") if el.tag == "a" else ""
        src = el.get("src") if el.tag == "img" else ""
        if not txt and not href and not src:
            continue
        rel = _rel_path(row, el)
        if rel in [o.split(" => ")[0] for o in out]:
            continue
        if len(out) >= max_fields:
            break
        if href:
            if href.startswith(("javascript:", "mailto:", "tel:")):
                continue
            abs_h = urljoin(url, href) if href.startswith(("/", "http")) else href
            out.append(f"{rel} => {txt[:40] if txt else '(无文本)'} [href={abs_h[:90]}]")
        elif src:
            out.append(f"{rel} => {txt[:40] if txt else '(图片)'} [img={src[:70]}]")
        elif txt:
            out.append(f"{rel} => {txt[:40]}")
    return out


def annotate_dom(html: str, url: str = "", max_chars: int = 7000,
                 top_rows: int = 5, min_count: int = 3) -> str:
    """HTML → 带 CSS 选择器标注的 DOM 摘要（scrapedown 思路）。

    找出页面里重复出现的「列表行候选」，给每行的完整 CSS 路径 + 前 2 个实例的
    字段相对选择器与真实内容，喂给 LLM 即可一次修对 row_css/fields。
    """
    if not html or len(html) < 100:
        return ""
    try:
        from lxml import html as lh
        doc = lh.fromstring(html)
        doc.make_links_absolute(url or "https://example.invalid")
    except Exception:
        return ""
    for tag in ("script", "style", "noscript", "svg", "iframe", "form"):
        for el in doc.xpath(f"//{tag}"):
            el.drop_tree()

    from collections import Counter, defaultdict
    counts: Counter = Counter()
    samples: Dict[Any, List[Any]] = defaultdict(list)
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        cls = (el.get("class") or "").strip()
        if not cls:
            continue
        key = (el.tag, tuple(sorted(cls.split())))
        counts[key] += 1
        if len(samples[key]) < 6:
            samples[key].append(el)

    # 候选行打分：出现次数多、含链接、深度适中优先
    cands = []
    for (tag, classes), n in counts.items():
        if n < min_count or tag not in ("li", "tr", "article", "div", "dl", "ul", "tbody"):
            continue
        els = samples.get((tag, classes), [])
        if not els:
            continue
        has_link = any(any(c.tag == "a" for c in e.iterdescendants()) for e in els[:3])
        # 行文本总长（过滤掉大容器）
        try:
            tlen = len(re.sub(r"\s+", "", els[0].text_content() or ""))
        except Exception:
            tlen = 99999
        if tlen > 600:
            continue
        # 深度：离 body 越远越像行
        depth = 0
        p = els[0].getparent()
        while p is not None and p.tag not in ("html", "body"):
            depth += 1
            p = p.getparent()
        score = (n * 2 + (8 if has_link else 0) + depth, n)
        cands.append((score, (tag, classes), n, els, has_link))
    cands.sort(key=lambda x: x[0], reverse=True)
    cands = cands[:top_rows]

    lines = [f"页面: {url or '(渲染页)'}", "【重复块候选（据此写 row_css）】"]
    for _, (tag, classes), n, els, has_link in cands:
        cls_str = ".".join(classes)
        full = _css_path(els[0])
        lines.append(f"\n• {tag}.{cls_str}  出现 {n} 次{'（含链接）' if has_link else ''}")
        lines.append(f"  完整CSS: {full}")
        for i, el in enumerate(els[:2], 1):
            fields = _row_field_hints(el, url)
            lines.append(f"  实例{i} ({tag}.{cls_str}:nth-child({i})):")
            for f in fields[:10]:
                lines.append(f"    {f}")
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n...(截断)"
    return out
