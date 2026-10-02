#!/usr/bin/env python3
"""📚 图书目录采集模块：豆瓣详情/搜索/在哪儿买 + 当当搜索 + 文档导出。

设计目标：
- 解析器纯函数，可离线测试；
- build_catalog 统一合并，按 ISBN 去重；
- 每个数据源失败都写入 diagnostics，绝不把 0 条伪装成成功；
- 不下载正文/PDF，只下载公开封面；
- 所有字段缺失写 N/A。
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urljoin, urlparse

OUTPUT_FIELDS = [
    "no", "book_group", "book_title", "author", "translator", "language", "edition",
    "isbn", "publisher", "publish_year", "page_count", "binding", "list_price",
    "douban_rating", "douban_rating_count", "douban_intro", "douban_cover_url",
    "cover_file", "douban_subject_url", "jd_price", "jd_click_link", "jd_union_url",
    "dangdang_price", "dangdang_title", "dangdang_link", "dangdang_union_url",
    "dangdang_source_link", "status", "diagnostics",
]

# 用户验收口径：书目核心字段必须缺省率 <20%；电商价格/本地封面是可选数据源，允许 N/A。
# 豆瓣 link2 供应商 → 输出字段（price/union_url/source_link）
BUYLINK_VENDOR_FIELDS = {
    "jingdong": ("jd_price", "jd_union_url", "jd_source_link"),
    "dangdang": ("dangdang_price", "dangdang_union_url", "dangdang_source_link"),
}

CORE_REQUIRED_FIELDS = [
    "no", "book_group", "book_title", "author", "isbn", "publisher",
    "publish_year", "page_count", "binding", "list_price",
    "douban_rating", "douban_rating_count", "douban_intro",
    "douban_cover_url", "douban_subject_url",
]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def norm_isbn(value: Any) -> str:
    s = re.sub(r"[^0-9Xx]", "", str(value or ""))
    return s.upper()


def isbn_checksum_ok(value: Any) -> bool:
    """ISBN-10 / ISBN-13 校验；不合规返回 False。"""
    isbn = norm_isbn(value)
    if not isbn:
        return False
    if len(isbn) == 10:
        total = 0
        for i, ch in enumerate(isbn):
            digit = 10 if ch == "X" else (int(ch) if ch.isdigit() else -1)
            if digit < 0:
                return False
            total += digit * (10 - i)
        return total % 11 == 0
    if len(isbn) == 13:
        if not isbn.isdigit():
            return False
        total = sum((1 if i % 2 == 0 else 3) * int(ch) for i, ch in enumerate(isbn[:12]))
        return (10 - total % 10) % 10 == int(isbn[12])
    return False


def _text(el: Any) -> str:
    if el is None:
        return ""
    # 审查十三轮（M）：text_content() 曾把内联 <script>/<style> 源码混进字段值
    # （douban_intro 实测混入 "var junk=1;"，与 selectors.py 的剔除口径对齐——
    # R10 实证 #main 曾有 795/1012 字符是 JS）。剥后代 script/style + 自身即
    # script/style 时返回空（drop_tree 会改文档树——提取场景一次性消费，无害）
    try:
        if str(getattr(el, "tag", "")) in ("script", "style"):
            return ""
        for _bad in el.cssselect("script, style"):
            _bad.drop_tree()
    except Exception:
        pass
    return clean_text(el.text_content())


def _find_info(doc: Any, label: str) -> str:
    """解析豆瓣 #info，兼容 pl 标签与非 pl 标签。"""
    for sp in doc.cssselect("#info span"):
        classes = sp.get("class") or ""
        text = clean_text(sp.text_content())
        if "pl" not in classes.split() and re.search(re.escape(label) + r"\s*[:：]", text):
            return clean_text(text.split(label, 1)[-1].lstrip(":： "))
    # 收官十二轮（审查 L）：label 曾用无边界子串匹配——"出品出版社:" 会顶替
    # "出版社:"（先出现者胜）。pl 标签文本即字段名，要求 label 锚定在开头
    for sp in doc.cssselect("#info span.pl"):
        if re.match(re.escape(label) + r"\s*[:：]", clean_text(sp.text_content())):
            parts = [sp.tail or ""]
            node = sp.getnext()
            while node is not None:
                if node.tag == "br":
                    break
                if node.tag == "span" and "pl" in (node.get("class") or ""):
                    break
                parts.append(_text(node))
                parts.append(node.tail or "")
                node = node.getnext()
            return clean_text(" ".join(parts))
    # 收官十二轮（审查 M）：第三分支曾在 clean_text（已折叠换行）上用 [^\n]+
    # 取值——永远吃到段尾，出版年/页数/定价/装帧互相污染。改为逐物理行匹配
    # （#info 无 span.pl 的页面变体是本分支唯一场景）
    # 审查十三轮（M）：re.search 曾无锚定——"副标题: 作者: 一个写作者的自述"
    # 行里 label="作者" 从中段命中，顶替真正的"作者: 余华"行。label 必须
    # 锚定行首（re.match）。注意 text_content() 不产换行（<br> 是空文本）——
    # 整段连成一行时行首锚定会全部落空，先从序列化 XML 把 <br> 还原成物理行
    _info_el = doc.cssselect("#info")
    if not _info_el:
        return ""
    _info_raw = ""
    try:
        from lxml import html as _lhtml
        _raw_xml = _lhtml.tostring(_info_el[0], encoding="unicode")
        _raw_xml = re.sub(r"(?i)<br\s*/?>", "\n", _raw_xml)
        _info_raw = re.sub(r"<[^>]+>", "", _raw_xml)
    except Exception:
        _info_raw = _info_el[0].text_content() if _info_el else ""
    for _line in _info_raw.splitlines():
        m = re.match(r"\s*" + re.escape(label) + r"\s*[:：]\s*(.+)", _line)
        if m:
            return clean_text(m.group(1))
    return ""


def _subject_id_from_url(url: str) -> str:
    m = re.search(r"/subject/(\d+)", url or "")
    return m.group(1) if m else ""


def _abs_url(href: str, base: str = "") -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return urljoin(base or "https://book.douban.com", href)
    if href.startswith(("http://", "https://")):
        return href
    return urljoin(base or "https://book.douban.com", href)


def _cents_to_yuan(value: Any) -> str:
    """豆瓣 link2 price 是整数分；只用于该来源，避免把元/分单位混用。"""
    v = clean_text(value)
    if not v:
        return ""
    if v.isdigit():
        return f"{int(v) / 100:.2f}"
    m = re.search(r"\d+(?:\.\d+)?", v)
    return m.group(0) if m else v


def _parse_doc(html: str):
    from lxml import html as lh
    try:
        return lh.fromstring(html or "")
    except Exception:
        return None


def _first_text(doc: Any, css: str) -> str:
    els = doc.cssselect(css) if doc is not None else []
    return _text(els[0]) if els else ""


def _first_attr(doc: Any, css: str, attr: str, default: str = "") -> str:
    els = doc.cssselect(css) if doc is not None else []
    return (els[0].get(attr) or default) if els else ""


def _extract_global_json(html: str, marker: str) -> Dict[str, Any]:
    """用 JSONDecoder.raw_decode 从 window.__DATA__ = {...}; 中安全取完整对象。"""
    pos = html.find(marker)
    if pos < 0:
        return {}
    pos = html.find("=", pos + len(marker))
    if pos < 0:
        return {}
    pos += 1
    while pos < len(html) and html[pos] in " \t\r\n":
        pos += 1
    try:
        obj, _end = json.JSONDecoder().raw_decode(html[pos:])
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


# ---------------------------------------------------------------------------
# 豆瓣读书：详情页
# ---------------------------------------------------------------------------

def parse_douban_book_detail(html: str, url: str = "") -> List[Dict[str, Any]]:
    """解析豆瓣读书 subject 详情页。返回单行列表；页面异常返回 []。"""
    doc = _parse_doc(html)
    if doc is None:
        return []
    if not doc.cssselect("#info") and not doc.cssselect("#wrapper h1"):
        return []
    info = _text(doc.cssselect("#info")[0]) if doc.cssselect("#info") else ""
    isbn = norm_isbn(_find_info(doc, "ISBN"))
    if not isbn:
        m = re.search(r"ISBN[:：]?\s*([0-9Xx\-]{8,20})", info)
        if m:
            isbn = norm_isbn(m.group(1))
    row = {
        "douban_subject_id": _subject_id_from_url(url),
        "douban_title": _first_text(doc, "#wrapper h1"),
        "author": _find_info(doc, "作者"),
        "translator": _find_info(doc, "译者"),
        "publisher": _find_info(doc, "出版社"),
        "publish_year": _find_info(doc, "出版年"),
        "page_count": _find_info(doc, "页数"),
        "binding": _find_info(doc, "装帧"),
        "list_price": _find_info(doc, "定价"),
        "isbn": isbn,
        "douban_rating": _first_text(doc, 'strong[property="v:average"]'),
        "douban_rating_count": _first_text(doc, 'span[property="v:votes"]'),
        "douban_intro": _first_text(doc, "#link-report .intro, #link-report span[property='v:summary']"),
        "douban_cover_url": _abs_url(_first_attr(doc, "#mainpic img", "src"), url),
        "douban_subject_url": url or "",
    }
    return [row]


# ---------------------------------------------------------------------------
# 豆瓣读书：搜索页
# ---------------------------------------------------------------------------

def parse_douban_book_search(html: str, url: str = "") -> List[Dict[str, Any]]:
    """解析豆瓣 subject_search 的 window.__DATA__ JSON；兼容 rate limit 诊断行。"""
    if not html:
        return []
    data = _extract_global_json(html, "window.__DATA__")
    if data:
        error_info = data.get("error_info") or ""
        if error_info:
            return [{
                "douban_subject_id": "",
                "douban_title": "",
                "douban_search_status": "RATE_LIMITED",
                "douban_search_error": error_info,
            }]
        out = []
        for item in data.get("items") or []:
            rating = item.get("rating") or {}
            out.append({
                "douban_subject_id": str(item.get("id") or ""),
                "douban_title": item.get("title") or "",
                "douban_search_abstract": (item.get("abstract") or item.get("abstract_2") or ""),
                "douban_cover_url": _abs_url(item.get("cover_url") or "", url),
                "douban_rating": rating.get("value") or "",
                "douban_rating_count": rating.get("count") or "",
                "douban_subject_url": item.get("url") or "",
                "douban_search_status": "OK",
                "douban_search_error": "",
            })
        return out
    # 旧版搜索页（SSR .result）
    doc = _parse_doc(html)
    if doc is None:
        return []
    out = []
    for div in doc.cssselect(".result")[:50]:
        a = div.cssselect("h2 a, .title a")
        if not a:
            continue
        link = a[0].get("href") or ""
        rating = _text(div.cssselect(".rating_nums")[0]) if div.cssselect(".rating_nums") else ""
        out.append({
            "douban_subject_id": _subject_id_from_url(link),
            "douban_title": _text(a[0]),
            "douban_search_abstract": _text(div.cssselect(".abstract")[0]) if div.cssselect(".abstract") else "",
            "douban_cover_url": "",
            "douban_rating": rating,
            "douban_rating_count": "",
            "douban_subject_url": _abs_url(link, url),
            "douban_search_status": "OK",
            "douban_search_error": "",
        })
    return out


# ---------------------------------------------------------------------------
# 豆瓣读书：在哪儿买
# ---------------------------------------------------------------------------

def parse_douban_book_buylinks(html: str, url: str = "") -> List[Dict[str, Any]]:
    """解析豆瓣在哪儿买：京东/当当价格与联盟链接。无商家也返回诊断行。"""
    doc = _parse_doc(html)
    if doc is None:
        return []
    row = {
        "douban_subject_id": _subject_id_from_url(url),
        "jd_price": "",
        "jd_union_url": "",
        "jd_source_link": "",
        "dangdang_price": "",
        "dangdang_union_url": "",
        "dangdang_source_link": "",
        "other_vendors": "",
        "buylinks_status": "OK",
    }
    seen: set = set()
    others: List[str] = []
    for a in doc.cssselect("a[href*='book.douban.com/link2/']"):
        href = a.get("href") or ""
        if href in seen:
            continue
        seen.add(href)
        q = parse_qs(urlparse(href).query)
        vendor = (q.get("vendor") or [""])[0]
        price = (q.get("price") or [""])[0]
        embedded = (q.get("url") or [""])[0]
        fields = BUYLINK_VENDOR_FIELDS.get(vendor)
        # 收官十二轮（审查，实测）：命中"补缺"条件后曾整组覆写 3 字段——第二个
        # 无价链接会把首个链接已抓到的价格清空（jd_price 4500 → ""），且
        # 状态被误翻成 NO_VENDOR（诊断谎称"未列出商家"，实际有）。逐字段按
        # "当前为空才填"补齐
        if fields:
            _changed = False
            for f, v in zip(fields, (_cents_to_yuan(price), embedded, href)):
                if not row[f] and v:
                    row[f] = v
                    _changed = True
            if not _changed:
                pass  # 该商家三字段已齐，无需处理
        elif vendor not in BUYLINK_VENDOR_FIELDS and vendor:
            others.append(vendor)
    row["other_vendors"] = "/".join(dict.fromkeys(others))
    if not row["jd_price"] and not row["dangdang_price"]:
        row["buylinks_status"] = "NO_VENDOR"
    return [row]


# ---------------------------------------------------------------------------
# 当当搜索
# ---------------------------------------------------------------------------

def parse_dangdang_search(html: str, url: str = "") -> List[Dict[str, Any]]:
    """解析当当搜索页 ISBN 查询结果。首个商品作为实时价快照；无结果返回诊断行。"""
    doc = _parse_doc(html)
    if doc is None:
        return []
    cards = doc.cssselect("ul.bigimg li") or doc.cssselect("li.line1")
    # 优先含 ISBN 的卡片；搜索页不显示 ISBN 时回退首个商品
    q = parse_qs(urlparse(url or "").query)
    isbn = norm_isbn((q.get("key") or [""])[0])
    # ISBN 须为独立数字串（两侧非数字）——裸子串会命中更长数字串（价格/数量拼接）误选卡片
    exact = [c for c in cards
             if isbn and re.search(r"(?<!\d)" + re.escape(isbn) + r"(?!\d)",
                                   clean_text(c.text_content()))]
    use_card = exact[0] if exact else (cards[0] if cards else None)
    # 收官十二轮（审查，实测）：页面有卡但无一含该 ISBN = 当当返回**模糊匹配**
    # （多为无关商品）——旧逻辑 `for card in (exact or cards)` 直接取首卡记
    # OK 状态，把别家的价格/链接静默写到本书且零诊断。模糊命中不写价格，
    # 状态记 UNMATCHED 留诊断
    _unmatched = use_card is not None and not exact
    if use_card is not None:
        card = use_card
        links = card.cssselect("a[href*='product.dangdang.com'], a[href*='product.dangdang.com.cn']")
        if not links:
            links = card.cssselect("a[href]")
        if not links:
            return [{
                "dangdang_title": "", "dangdang_price": "", "dangdang_list_price": "",
                "dangdang_link": "", "dangdang_seller": "",
                "dangdang_status": "NO_RESULT",
            }]
        href = links[0].get("href") or ""
        href = _abs_url(href, "https://product.dangdang.com/")
        title = _first_attr(card, "h6.title, .name, a[title]", "title") or \
            _first_text(card, "h6.title, .name, a[title]")
        text = clean_text(card.text_content())
        # 收官十二轮（审查 L）：售价优先取 span.price_n（标准 DOM 的售价节点），
        # 首个 ¥ 兜底——定价/电子价排在售价前时曾把定价当售价
        price_n = _first_text(card, "span.price_n")
        price_m = (re.search(r"¥?\s*(\d+(?:\.\d+)?)", price_n) if price_n
                   else None) or re.search(r"¥\s*(\d+(?:\.\d+)?)", text)
        list_m = re.search(r"定价[：:]?\s*¥\s*(\d+(?:\.\d+)?)", text)
        return [{
            # 审查十二轮（H5）：模糊命中（UNMATCHED）时曾只清价格——书名/链接仍是
            # 别的商品且该状态无任何消费方，整行以"本书数据"交付他人书名与购买
            # 链接（端到端实测）。UNMATCHED 一律清空商品字段，只留状态诊断
            "dangdang_title": "" if _unmatched else title,
            "dangdang_price": "" if _unmatched else (price_m.group(1) if price_m else ""),
            "dangdang_list_price": "" if _unmatched else (list_m.group(1) if list_m else ""),
            "dangdang_link": "" if _unmatched else href,
            "dangdang_seller": "" if _unmatched else (
                _text(card.cssselect("a[href*='shop.dangdang.com']")[0])
                if card.cssselect("a[href*='shop.dangdang.com']") else ""),
            "dangdang_status": "UNMATCHED" if _unmatched else "OK",
        }]
    return [{
        "dangdang_title": "",
        "dangdang_price": "",
        "dangdang_list_price": "",
        "dangdang_link": "",
        "dangdang_seller": "",
        "dangdang_status": "NO_RESULT",
    }]


# ---------------------------------------------------------------------------
# 构建目录
# ---------------------------------------------------------------------------

def _default_fetch_html(url: str) -> str:
    from .sites import fetch_html
    res = fetch_html(url)
    if not res.get("ok"):
        raise RuntimeError(f"HTTP {res.get('status')}: {res.get('error') or ''}")
    return res.get("html") or ""


_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)  # webp 走下方 RIFF????WEBP 双重校验（OCR R131：裸 RIFF 误纳 wav/avi）


def _looks_like_image(path: Path) -> bool:
    try:
        head = path.read_bytes()[:16]
    except Exception:
        return False
    return any(head.startswith(magic) for magic, _name in _IMAGE_MAGIC) or (
        # OCR R131（M）：裸 RIFF 前缀匹配一切 RIFF 容器（wav/avi 也命中）——
        # webp 必须 RIFF????WEBP 双重校验
        head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    )


def _default_download_cover(url: str, path: Path) -> bool:
    if path.exists() and path.stat().st_size > 100 and _looks_like_image(path):
        return True
    # OCR R131（H）：封面 URL 来自抓取页面——file:// 等协议经 curl 可读本地文件
    # 并落进封面目录。仅放行 http/https（与全局 SSRF 口径一致）
    from urllib.parse import urlsplit as _us
    if _us(url or "").scheme not in ("http", "https"):
        return False
    # OCR R7（M）：curl 超时/--max-filesize 中断曾把半截文件直接写在 path 上——
    # 魔数在前 16 字节，截断 JPEG 照样通过下轮缓存检查且永不重下。改写临时文件，
    # 完整下载才原子替换，失败即清理
    import os as _os
    tmp = path.with_name(f"{path.name}.{_os.getpid()}.tmp")
    p = subprocess.run(
        ["curl", "-sS", "-L", "-A", "Mozilla/5.0", "-e", "https://book.douban.com/",
         # 审查二轮（H）：-L 跟随曾不限协议（http→file/ftp 可读写本地）；
         # --max-filesize 防异常大响应撑爆磁盘
         "--proto", "=http,https", "--max-filesize", "52428800",
         "--max-time", "30", "-o", str(tmp), url],
        capture_output=True, text=True,
    )
    ok = (p.returncode == 0 and tmp.exists() and tmp.stat().st_size > 100
          and _looks_like_image(tmp))
    if ok:
        _os.replace(tmp, path)
        return True
    try:
        tmp.unlink()
    except OSError:
        pass
    return False


def _search_candidate(items: List[Dict[str, Any]], isbn: str, title: str) -> Optional[Dict[str, Any]]:
    if not items:
        return None
    for it in items:
        abstract = it.get("douban_search_abstract") or ""
        if isbn:
            tokens = re.split(r"[\s,/|；;]+", abstract)
            if any(norm_isbn(t) == isbn for t in tokens if len(norm_isbn(t)) >= 10):
                return it
    if title:
        for it in items:
            if title.lower() in (it.get("douban_title") or "").lower():
                return it
    return items[0]


def _normalize_spec(spec: Any) -> Tuple[Dict[str, Any], str]:
    if not isinstance(spec, dict):
        return {}, "spec 必须是 JSON 对象"
    books = spec.get("books")
    if not isinstance(books, list):
        return {}, "spec.books 必须是数组"
    if not books:
        return {}, "spec.books 不能为空"
    for i, b in enumerate(books, 1):
        if not isinstance(b, dict) or not isbn_checksum_ok(b.get("isbn")):
            return {}, f"spec.books[{i-1}] 缺少合法 ISBN-10/13（目录以 ISBN 为主键）"
    return spec, ""


def _to_na(value: Any) -> str:
    s = clean_text(value)
    return s if s else "N/A"


def _fetch_text(fetcher: Callable[[str], str], url: str, interval: float) -> str:
    interval = max(0.0, float(interval or 0))
    if interval > 0:
        time.sleep(interval)
    return fetcher(url)


def _diagnostic_text(entries: List[Dict[str, str]]) -> str:
    if not entries:
        return "N/A"
    parts = []
    for e in entries:
        item = f"{e['code']}: {e['message']}"
        if e.get("action"):
            item += f"；行动：{e['action']}"
        parts.append(item)
    return " | ".join(parts)


def _collect_book_sources(
    book: Dict[str, Any],
    initial_subject_id: str,
    fetch: Callable[[str], str],
    min_interval: float,
) -> Dict[str, Any]:
    """抓取一本书的豆瓣搜索/详情/在哪儿买 + 当当搜索，返回字段级诊断。"""
    diag_entries: List[Dict[str, str]] = []

    def add_diag(source: str, code: str, message: str, field: str = "", action: str = "") -> None:
        diag_entries.append({
            "source": source, "code": code, "field": field,
            "message": message, "action": action,
        })

    subject_id = initial_subject_id
    detail_row: Dict[str, Any] = {}
    buylinks_row: Dict[str, Any] = {}
    dangdang_row: Dict[str, Any] = {}
    search_row: Dict[str, Any] = {}

    # 1) 没有 subject_id 时按 ISBN/书名搜索
    if not subject_id:
        query = norm_isbn(book.get("isbn")) or (book.get("title") or "")
        search_url = "https://book.douban.com/subject_search?cat=1001&search_text=" + quote(query)
        try:
            html = _fetch_text(fetch, search_url, min_interval)
            candidates = parse_douban_book_search(html, search_url)
            search_row = next((x for x in candidates if x.get("douban_search_status") == "RATE_LIMITED"), {})
            if search_row:
                add_diag("DOUBAN_SEARCH", "RATE_LIMITED",
                         f"豆瓣搜索限流：{search_row.get('douban_search_error')}",
                         field="douban_subject_url", action="等待数分钟重试，或直接在 spec 填写 douban_subject_id")
            else:
                candidate = _search_candidate(candidates, query, book.get("title") or "")
                if candidate:
                    subject_id = candidate.get("douban_subject_id") or ""
                    search_row = candidate
                else:
                    add_diag("DOUBAN_SEARCH", "NO_RESULT",
                             "豆瓣搜索 0 条（书名/ISBN 未命中或搜索页有限流）",
                             field="douban_subject_url", action="检查 ISBN/书名；到豆瓣先确认 subject_id 再填 spec")
        except Exception as e:
            add_diag("DOUBAN_SEARCH", "FETCH_ERROR",
                     f"豆瓣搜索失败：{type(e).__name__}: {e}",
                     field="douban_subject_url", action="稍后重试或直接填写豆瓣 subject_id")

    # 2) 豆瓣详情
    if subject_id:
        detail_url = f"https://book.douban.com/subject/{subject_id}/"
        try:
            html = _fetch_text(fetch, detail_url, min_interval)
            details = parse_douban_book_detail(html, detail_url)
            detail_row = details[0] if details else {}
            if not detail_row:
                add_diag("DOUBAN_DETAIL", "NO_DATA",
                         "豆瓣详情解析 0 行（页面结构变化或需要登录）",
                         field="douban_rating", action="确认 subject_id 是否存在；如被限流稍后重试")
        except Exception as e:
            add_diag("DOUBAN_DETAIL", "FETCH_ERROR",
                     f"豆瓣详情失败：{type(e).__name__}: {e}",
                     field="douban_rating", action="稍后重试")

    # 3) 豆瓣在哪儿买
    if subject_id:
        buy_url = f"https://book.douban.com/subject/{subject_id}/buylinks"
        try:
            html = _fetch_text(fetch, buy_url, min_interval)
            buys = parse_douban_book_buylinks(html, buy_url)
            buylinks_row = buys[0] if buys else {}
            if not buylinks_row:
                add_diag("BUYLINKS", "NO_DATA",
                         "豆瓣在哪儿买解析 0 行（页面为空或结构变化）", field="jd_price",
                         action="稍后重试，或手动打开在哪儿买页确认")
            elif buylinks_row.get("buylinks_status") == "NO_VENDOR":
                add_diag("BUYLINKS", "NO_VENDOR",
                         "豆瓣在哪儿买未列出京东/当当商家", field="jd_price",
                         action="换用中图网/出版社/海外渠道，或手动打开豆瓣在哪儿买页确认")
        except Exception as e:
            add_diag("BUYLINKS", "FETCH_ERROR",
                     f"豆瓣在哪儿买失败：{type(e).__name__}: {e}",
                     field="jd_price", action="稍后重试")

    # 4) 京东聚合价：不抓登录墙，只使用豆瓣公开聚合数据
    jd_price = buylinks_row.get("jd_price") or ""
    jd_click_link = buylinks_row.get("jd_source_link") or ""
    jd_union_url = buylinks_row.get("jd_union_url") or ""
    if not jd_price and not jd_click_link and not jd_union_url:
        add_diag("JD", "LOGIN_WALL",
                 "京东无公开聚合价/链接（搜索页或商品页可能触发登录墙，或商品未在售）",
                 field="jd_price",
                 action="使用豆瓣跳转/中图网/海外渠道；本工具不提交账号、不破解签名")

    # 5) 当当搜索
    dd_url = "https://search.dangdang.com/?key=" + quote(norm_isbn(book.get("isbn")))
    try:
        html = _fetch_text(fetch, dd_url, min_interval)
        dds = parse_dangdang_search(html, dd_url)
        dangdang_row = dds[0] if dds else {}
        if not dangdang_row:
            add_diag("DANGDANG", "NO_RESULT",
                     "当当搜索页解析 0 行（无结果或页面为空/结构变化）",
                     field="dangdang_price", action="用书名/ISBN 再搜，或改用京东/中图/海外渠道")
        elif dangdang_row.get("dangdang_status") == "NO_RESULT":
            add_diag("DANGDANG", "NO_RESULT",
                     "当当 ISBN 搜索 0 条（无国内在售或该 ISBN 未收录）",
                     field="dangdang_price", action="用书名/ISBN 再搜，或改用京东/中图/海外渠道")
    except Exception as e:
        add_diag("DANGDANG", "FETCH_ERROR",
                 f"当当失败：{type(e).__name__}: {e}",
                 field="dangdang_price", action="稍后重试")

    return {
        "subject_id": subject_id,
        "detail_row": detail_row,
        "buylinks_row": buylinks_row,
        "dangdang_row": dangdang_row,
        "search_row": search_row,
        "diag_entries": diag_entries,
    }


def _merge_book_row(book: Dict[str, Any], isbn: str, sources: Dict[str, Any], row_no: int) -> Dict[str, Any]:
    """把取数结果合并成一行输出，并补齐字段级诊断。"""
    diag_entries = list(sources["diag_entries"])
    detail_row = sources["detail_row"]
    search_row = sources["search_row"]
    buylinks_row = sources["buylinks_row"]
    dangdang_row = sources["dangdang_row"]
    subject_id = sources["subject_id"]

    detail_isbn = detail_row.get("isbn") or ""
    if detail_isbn and detail_isbn != isbn:
        diag_entries.append({
            "source": "ISBN", "code": "MISMATCH", "field": "isbn",
            "message": f"豆瓣条目 ISBN={detail_isbn} 与主键 {isbn} 不一致",
            "action": "核对 subject_id；spec 的 ISBN 仍作为主键",
        })

    detail_url = detail_row.get("douban_subject_url") or (
        f"https://book.douban.com/subject/{subject_id}/" if subject_id else "")
    cover_url = detail_row.get("douban_cover_url") or search_row.get("douban_cover_url") or ""
    rating = detail_row.get("douban_rating") or search_row.get("douban_rating") or ""
    rating_count = detail_row.get("douban_rating_count") or search_row.get("douban_rating_count") or ""
    intro = detail_row.get("douban_intro") or search_row.get("douban_search_abstract") or ""
    has_bibliographic_data = any([rating, intro, detail_row.get("publisher"), detail_row.get("author")])
    if not has_bibliographic_data:
        diag_entries.append({
            "source": "DOUBAN", "code": "NO_DATA", "field": "douban_rating",
            "message": "豆瓣书目字段为空（需检查 subject_id 是否对应正确版本）",
            "action": "核对豆瓣条目；如果确认有数据，检查页面结构是否变化",
        })

    jd_price = buylinks_row.get("jd_price") or ""
    jd_click_link = buylinks_row.get("jd_source_link") or ""
    jd_union_url = buylinks_row.get("jd_union_url") or ""
    dd_price = dangdang_row.get("dangdang_price") or ""
    dd_link = dangdang_row.get("dangdang_link") or ""
    has_retail_data = any([jd_price, jd_click_link, jd_union_url, dd_price, dd_link])

    problem_codes = {"NO_RESULT", "NO_VENDOR", "LOGIN_WALL", "FETCH_ERROR",
                     "NO_DATA", "RATE_LIMITED", "MISMATCH", "COVER_DOWNLOAD_FAILED"}
    # 审查八轮（MEDIUM）：JD 的 LOGIN_WALL 是**设计内**结果（本工具不抓京东登录墙，
    # 只用豆瓣公开聚合数据；模块自述"电商价格/封面是可选数据源，允许 N/A"）。把它
    # 计入 problem_codes 会让每本数据齐全的书都恒判 PARTIAL（质量信号失去区分度，
    # webui 报"N 本未全字段成功（LOGIN_WALL×N）"误导用户）。只豁免这一组合，
    # JD 的其它问题码（限流/抓取失败）照旧计入。
    has_problem = any(
        diag["code"] in problem_codes
        and not (diag.get("source") == "JD" and diag["code"] == "LOGIN_WALL")
        for diag in diag_entries)
    if has_bibliographic_data and has_retail_data and not has_problem:
        status = "OK"
    elif has_bibliographic_data or has_retail_data:
        status = "PARTIAL"
    else:
        status = "NO_DATA"

    row = {
        "no": row_no,
        "book_group": _to_na(book.get("group") or book.get("title") or ""),
        "book_title": _to_na(book.get("title") or detail_row.get("douban_title") or search_row.get("douban_title") or ""),
        "author": _to_na(detail_row.get("author") or ""),
        "translator": _to_na(detail_row.get("translator") or ""),
        "language": _to_na(book.get("language") or ""),
        "edition": _to_na(book.get("edition") or ""),
        "isbn": isbn,
        "publisher": _to_na(detail_row.get("publisher") or ""),
        "publish_year": _to_na(detail_row.get("publish_year") or ""),
        "page_count": _to_na(detail_row.get("page_count") or ""),
        "binding": _to_na(detail_row.get("binding") or ""),
        "list_price": _to_na(detail_row.get("list_price") or ""),
        "douban_rating": _to_na(rating),
        "douban_rating_count": _to_na(rating_count),
        "douban_intro": _to_na(intro),
        "douban_cover_url": _to_na(cover_url),
        "cover_file": "N/A",
        "douban_subject_url": _to_na(detail_url),
        "jd_price": _to_na(jd_price),
        "jd_click_link": _to_na(jd_click_link),
        "jd_union_url": _to_na(jd_union_url),
        "dangdang_price": _to_na(dd_price),
        "dangdang_title": _to_na(dangdang_row.get("dangdang_title") or ""),
        "dangdang_link": _to_na(dd_link),
        "dangdang_union_url": _to_na(buylinks_row.get("dangdang_union_url") or ""),
        "dangdang_source_link": _to_na(buylinks_row.get("dangdang_source_link") or ""),
        "status": status,
        "diagnostics": _diagnostic_text(diag_entries),
        "diagnostic_fields": diag_entries,
    }
    return row


def build_catalog(
    spec: Any,
    out_dir: Any = "outputs/book_catalog",
    *,
    fetch_html: Optional[Callable[[str], str]] = None,
    download_cover: Optional[Callable[[str, Path], bool]] = None,
    download_covers: bool = True,
    min_interval: float = 1.0,
    log: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """按 spec 抓取并生成 booklist.csv / booklist.md / crawl_log.md / books.json。

    每个数据源失败会记录结构化 field_diagnostics：{source, code, field, message, action}。
    零结果/登录墙/下载失败会体现在诊断和 status，不把 0 条伪装为成功。
    should_stop 在每本书开始前被查询：返回 True 时停止抓取，已抓到的行仍会正常导出。
    """
    spec, err = _normalize_spec(spec)
    if err:
        return {"status": "INVALID_SPEC", "error": err, "rows": [], "files": {},
                "diagnostics": [], "coverage": {}}
    fetch = fetch_html or _default_fetch_html
    cover_dl = download_cover or _default_download_cover
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cover_dir = out / "covers"
    cover_dir.mkdir(parents=True, exist_ok=True)

    rows: List[Dict[str, Any]] = []
    seen: set = set()
    diagnostics_all: List[Dict[str, Any]] = []
    duplicates = 0
    stopped = False

    for idx, book in enumerate((spec.get("books") or []), 1):
        if should_stop is not None and should_stop():
            stopped = True
            if log:
                log(f"⏹ 已按要求停止：剩余 {len(spec.get('books') or []) - idx + 1} 本书未抓取，"
                    f"已完成的 {len(rows)} 行照常导出")
            break
        if log:
            log(f"[{idx}] {book.get('title') or book.get('isbn')}")
        isbn = norm_isbn(book.get("isbn"))
        if isbn in seen:
            duplicates += 1
            diagnostics_all.append({
                "isbn": isbn,
                "status": "SKIPPED_DUPLICATE",
                "diagnostics": "SKIPPED_DUPLICATE: 同一 ISBN 已存在，跳过",
                "field_diagnostics": [{
                    "source": "SPEC", "code": "SKIPPED_DUPLICATE", "field": "isbn",
                    "message": "同一 ISBN 已存在，跳过", "action": "检查 spec 是否重复列了同一版本",
                }],
            })
            continue
        seen.add(isbn)

        sources = _collect_book_sources(
            book, str(book.get("douban_subject_id") or "").strip(),
            fetch, min_interval)
        row = _merge_book_row(book, isbn, sources, len(rows) + 1)

        # 封面失败降级：本地文件 N/A，在线 URL 保留供人工访问
        cover_url = row["douban_cover_url"]
        if download_covers and cover_url and cover_url != "N/A":
            cover_path = cover_dir / f"{isbn}.jpg"
            try:
                if cover_dl(cover_url, cover_path):
                    # 收官十二轮（审查 L）：下载成功后按魔数改扩展名——
                    # png/webp 内容挂 .jpg 时下游按扩展名处理会错
                    _head = b""
                    try:
                        _head = cover_path.read_bytes()[:4]
                    except OSError:
                        pass
                    _ext = {b"\x89PNG": ".png", b"GIF8": ".gif",
                            b"RIFF": ".webp"}.get(_head, ".jpg")
                    if _head == b"RIFF":
                        try:
                            _head12 = cover_path.read_bytes()[8:12]
                            if _head12 != b"WEBP":
                                _ext = ".jpg"
                        except OSError:
                            pass
                    if _ext != ".jpg":
                        try:
                            cover_path.rename(cover_dir / f"{isbn}{_ext}")
                        except OSError:
                            _ext = ".jpg"
                    row["cover_file"] = f"covers/{isbn}{_ext}"
                else:
                    row["diagnostic_fields"].append({
                        "source": "COVER", "code": "COVER_DOWNLOAD_FAILED", "field": "cover_file",
                        "message": "封面下载失败，本地 cover_file 为 N/A；在线 URL 保留",
                        "action": "手工打开在线封面 URL，或稍后重跑",
                    })
                    row["diagnostics"] = _diagnostic_text(row["diagnostic_fields"])
            except Exception as e:
                row["diagnostic_fields"].append({
                    "source": "COVER", "code": "COVER_DOWNLOAD_FAILED", "field": "cover_file",
                    "message": f"封面下载异常：{type(e).__name__}: {e}",
                    "action": "手工下载在线封面 URL",
                })
                row["diagnostics"] = _diagnostic_text(row["diagnostic_fields"])
        if any(d["code"] == "COVER_DOWNLOAD_FAILED" for d in row["diagnostic_fields"]):
            row["status"] = "PARTIAL" if row["status"] == "OK" else row["status"]
        rows.append(row)
        diagnostics_all.append({
            "isbn": isbn, "status": row["status"], "diagnostics": row["diagnostics"],
            "field_diagnostics": row["diagnostic_fields"],
        })

    total_cells = len(rows) * len(OUTPUT_FIELDS)
    required_cells = len(rows) * len(CORE_REQUIRED_FIELDS)
    missing = sum(1 for r in rows for f in OUTPUT_FIELDS if str(r.get(f, "")).strip() in ("", "N/A"))
    required_missing = sum(1 for r in rows for f in CORE_REQUIRED_FIELDS
                           if str(r.get(f, "")).strip() in ("", "N/A"))
    files = _write_outputs(out, rows, {
        "total_cells": total_cells,
        "missing": missing,
        "missing_rate": f"{missing / total_cells * 100:.2f}%" if total_cells else "N/A",
        "required_cells": required_cells,
        "required_missing": required_missing,
        "required_missing_rate": f"{required_missing / required_cells * 100:.2f}%" if required_cells else "N/A",
        "duplicates": duplicates,
    }, diagnostics_all)

    status = "OK" if rows and all(r["status"] == "OK" for r in rows) else ("NO_DATA" if not rows else "PARTIAL")
    return {
        "status": status,
        "total": len(rows),
        "stopped": stopped,
        "rows": rows,
        "diagnostics": diagnostics_all,
        "coverage": {
            "total_cells": total_cells,
            "all_missing": missing,
            "all_missing_rate": f"{missing / total_cells * 100:.2f}%" if total_cells else "N/A",
            "required_missing": required_missing,
            "required_missing_rate": f"{required_missing / required_cells * 100:.2f}%" if required_cells else "N/A",
            "duplicates": duplicates,
        },
        "files": files,
    }


def _write_outputs(
    out: Path,
    rows: List[Dict[str, Any]],
    coverage: Dict[str, Any],
    diagnostics_all: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, str]:
    csv_path = out / "booklist.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    json_path = out / "books.json"
    json_path.write_text(json.dumps({
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "total": len(rows),
        "coverage": coverage,
        "diagnostics": diagnostics_all or [],
        "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = [
        "# 图书目录采集结果",
        "",
        f"- 生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- ISBN 行数：{len(rows)}（按 ISBN 主键去重）",
        f"- 必填字段缺失率：{coverage.get('required_missing_rate', 'N/A')}",
        "",
        "| 序号 | 书名 | 出版社 | 出版年 | 评分 | 评价人数 | 京东价 | 当当价 | 状态 |",
        "| ---: | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    def _cell(v) -> str:
        # OCR R131（M）：书名/出版社含 "|" 或换行时曾撕裂 Markdown 表格结构
        return str(v).replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    for r in rows:
        md_lines.append(
            f"| {_cell(r['no'])} | {_cell(r['book_title'])} | {_cell(r['publisher'])} | {_cell(r['publish_year'])} | "
            f"{_cell(r['douban_rating'])} | {_cell(r['douban_rating_count'])} | {_cell(r['jd_price'])} | "
            f"{_cell(r['dangdang_price'])} | {_cell(r['status'])} |"
        )
    md_lines += [
        "",
        "## 正版获取说明",
        "",
        "本工具只采集书目、评分、公开封面与价格，不下载整本电子书/PDF，不绕过版权、付费墙或登录限制。",
        "需要正文请使用图书馆、出版社官方渠道、已授权数据库或正版购买。",
        "",
    ]
    (out / "booklist.md").write_text("\n".join(md_lines), encoding="utf-8")

    log_lines = [
        "# 图书目录抓取日志",
        "",
        f"- 生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- 行数：{len(rows)}；重复跳过：{coverage.get('duplicates', 0)}",
        f"- 必填字段缺失率：{coverage.get('required_missing_rate', 'N/A')}",
        f"- 全字段 N/A 率：{coverage.get('missing_rate', 'N/A')}",
        "",
        "## 逐书诊断",
        "",
        "| ISBN | 状态 | 诊断 |",
        "| --- | --- | --- |",
    ]
    for r in rows:
        # OCR R131（M）：诊断文本里的 | 破坏 Markdown 表格列对齐——转义
        _diag = str(r.get("diagnostics", "")).replace("|", "\\|")
        log_lines.append(f"| {r['isbn']} | {r['status']} | {_diag} |")
    log_lines += [
        "",
        "## 字段级诊断",
        "",
    ]
    for r in rows:
        fields = r.get("diagnostic_fields") or []
        if not fields:
            log_lines.append(f"- {r['isbn']}：无")
            continue
        log_lines.append(f"- {r['isbn']}：")
        for d in fields:
            action = f"；行动：{d.get('action')}" if d.get("action") else ""
            log_lines.append(f"  - `{d['code']}` | 字段 `{d.get('field')}` | {d.get('message')}{action}")
        log_lines.append("")
    log_lines += [
        "## 合规与限制",
        "",
        "- 京东搜索页可能触发登录墙；工具只采用豆瓣“在哪儿买”的公开聚合价和联盟跳转，不获取或提交登录态。",
        "- 当当价格来自当前搜索结果页首个商品；价格可能由第三方/二手商户展示，会变化。",
        "- 未下载任何正文/PDF。",
        "",
    ]
    (out / "crawl_log.md").write_text("\n".join(log_lines), encoding="utf-8")
    return {
        "csv": str(csv_path),
        "md": str(out / "booklist.md"),
        "log": str(out / "crawl_log.md"),
        "json": str(json_path),
    }
