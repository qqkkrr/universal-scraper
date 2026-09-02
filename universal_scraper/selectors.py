#!/usr/bin/env python3
"""选择器/提取器：JSON 路径、CSS、正则。

- jpath: 轻量 JSONPath（支持 a.b.c、a[0].b、*.x）
- CSS: 用 lxml.cssselect（lxml 可用时），否则退化为正则
- regex: 正则提取
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

try:
    from lxml import html as _lxml_html
    # 可用性探测：cssselect 是 lxml 的可选扩展（缺失则 HAS_LXML=False 走正则回退）
    _CSSSelector = __import__("lxml.cssselect", fromlist=["CSSSelector"]).CSSSelector
    HAS_LXML = True
except Exception:  # pragma: no cover
    HAS_LXML = False


# ---------------------------------------------------------------- JSON 路径

def jpath(obj: Any, path: str, default: Any = None) -> Any:
    """按点分路径取值，支持下标 a[0] 与通配 a[*].b。"""
    if path is None:
        return default
    path = str(path).strip()
    if not path:
        return default
    # 切分 a.b[0].c -> ['a','b','0','c']（[] 内为下标）
    tokens: List[str] = []
    for part in path.split("."):
        if not part:
            continue
        m = re.match(r"^([^\[]*)((?:\[[^\]]*\])*)$", part)
        name = m.group(1) if m else part
        if name:
            tokens.append(name)
        if m:
            for idx in re.findall(r"\[([^\]]*)\]", m.group(2)):
                tokens.append(idx)
    cur: Any = obj
    for i, tok in enumerate(tokens):
        if tok == "*":
            if isinstance(cur, list):
                # 用剩余 token 递归（修复多通配 a.*.b.*.c 时 index() 永远取第一个 * 的 bug）
                out = [jpath(x, ".".join(tokens[i + 1:]), default) for x in cur]
                return out
            return default
        if isinstance(cur, dict):
            cur = cur.get(tok, default)
        elif isinstance(cur, list):
            if "=" in tok and not tok.lstrip("-").isdigit():
                # 过滤语法 spec[name=主材]：在 dict 列表里按 key=value 取第一个命中
                # （小米有品战例：规格 [{"name":"主材","value":"PP"},...] 的按名取值）
                k, _, v = tok.partition("=")
                matches = [x for x in cur
                           if isinstance(x, dict) and str(x.get(k)) == v]
                if not matches:
                    return default
                cur = matches[0]
                continue
            try:
                cur = cur[int(tok)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur


def jpath_first(obj: Any, path: str, default: Any = None) -> Any:
    v = jpath(obj, path, default)
    if isinstance(v, list):
        return v[0] if v else default
    return v


# ---------------------------------------------------------------- CSS

def _css_to_xpath(selector: str) -> str:
    """简单 CSS 选择器 → XPath 兜底（cssselect 缺失时可用）：tag.class / tag#id / .class / #id / a[attr=val]"""
    sel = selector.strip()
    m = re.match(r"^([a-zA-Z][\w-]*)?([.#])([\w-]+)$", sel)
    if m:
        tag, kind, name = m.group(1) or "*", m.group(2), m.group(3)
        if kind == ".":
            return f"//{tag}[contains(concat(' ', normalize-space(@class), ' '), ' {name} ')]"
        return f"//{tag}[@id='{name}']"
    m = re.match(r"^([a-zA-Z][\w-]*)?\[([\w-]+)=['\"]?([^'\"]+)['\"]?\]$", sel)
    if m:
        return f"//{m.group(1) or '*'}[@{m.group(2)}='{m.group(3)}']"
    return ""


def _strip_pseudo(selector: str):
    """剥离 cssselect 不支持的伪元素：.text::text / a::attr(href) → .text / a。
    返回 (真实选择器, 伪元素名, 伪元素参数)；无伪元素时返回 (原样, None, None)。"""
    sel = (selector or "").strip()
    m = re.search(r"::(text|attr)\(([^)]*)\)$", sel)
    if m:
        return sel[:m.start()].strip(), m.group(1), m.group(2).strip().strip("'\"")
    m = re.search(r"::(text)$", sel)
    if m:
        return sel[:m.start()].strip(), "text", None
    return sel, None, None


def css_elements(html: str, selector: str) -> List[Any]:
    if not selector:
        return []
    real, _pseudo, _arg = _strip_pseudo(selector)
    if not real:
        return []
    if HAS_LXML:
        try:
            doc = _lxml_html.fromstring(html)
            return doc.cssselect(real)
        except Exception:
            xp = _css_to_xpath(real)
            if xp:
                try:
                    return doc.xpath(xp)
                except Exception:
                    return []
            return []
    # 退化：简单 class/id 正则
    out = []
    for m in re.finditer(r"<[^>]+(?:class|id)=[\"']([^\"']*" + re.escape(selector.lstrip(".#")) + r"[^\"']*)[\"'][^>]*>", html):
        out.append(m.group(0))
    return out


def css_text(html: str, selector: str, limit: int = 0, joiner: str = "\n") -> str:
    parts = []
    for el in css_elements(html, selector):
        t = el.text_content() if hasattr(el, "text_content") else str(el)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            parts.append(t)
    text = joiner.join(parts)
    return text[:limit] if limit and len(text) > limit else text


def css_attr(html: str, selector: str, attr: str = "href", limit: int = 0) -> str:
    real, pseudo, arg = _strip_pseudo(selector)
    if pseudo == "attr" and arg:
        attr = arg
    out = []
    for el in css_elements(html, real):
        v = el.get(attr) if hasattr(el, "get") else ""
        if v:
            out.append(v)
    text = " ".join(out)
    return text[:limit] if limit and len(text) > limit else text


def css_html(html: str, selector: str, limit: int = 0) -> str:
    out = []
    for el in css_elements(html, selector):
        s = _lxml_html.tostring(el, encoding="unicode") if HAS_LXML else str(el)
        if s:
            out.append(s)
    text = " ".join(out)
    return text[:limit] if limit and len(text) > limit else text


# ---------------------------------------------------------------- 正则

def regex_extract(text: str, pattern: str, group: int = 0, flags: int = re.S | re.I) -> str:
    if not pattern:
        return ""
    try:
        m = re.search(pattern, text or "", flags)
    except re.error:
        return ""  # 非法正则（AI 常生成）：不崩溃，按无匹配处理
    if not m:
        return ""
    try:
        return m.group(group) or ""
    except IndexError:
        return m.group(0)


def regex_extract_all(text: str, pattern: str, group: int = 0) -> List[str]:
    if not pattern:
        return []
    try:
        return [m.group(group) or "" for m in re.finditer(pattern, text or "", re.S | re.I)]
    except re.error:
        return []


# ---------------------------------------------------------------- 统一提取入口

def apply_extractor(spec: dict, ctx_text: str, ctx_html: str, ctx_obj: Any) -> Any:
    """根据 spec 提取一个字段。ctx_text=纯文本, ctx_html=原始 HTML, ctx_obj=JSON 对象。"""
    etype = spec.get("type", "text")
    if etype == "json":
        return jpath(ctx_obj, spec.get("path", ""), spec.get("default"))
    if etype == "css_text":
        return css_text(ctx_html, spec.get("selector", ""), spec.get("limit", 0))
    if etype == "css_attr":
        return css_attr(ctx_html, spec.get("selector", ""), spec.get("attr", "href"), spec.get("limit", 0))
    if etype == "css_html":
        return css_html(ctx_html, spec.get("selector", ""), spec.get("limit", 0))
    if etype in ("xpath_text", "xpath"):
        return xpath_text(ctx_html, spec.get("xpath", ""), spec.get("limit", 0))
    if etype == "xpath_attr":
        return xpath_attr(ctx_html, spec.get("xpath", ""), spec.get("attr", "href"), spec.get("limit", 0))
    if etype == "regex":
        return regex_extract(ctx_text, spec.get("pattern", ""), spec.get("group", 0))
    if etype == "regex_all":
        return regex_extract_all(ctx_text, spec.get("pattern", ""), spec.get("group", 0))
    if etype == "constant":
        return spec.get("value")
    # 默认：从 JSON 对象取
    return jpath(ctx_obj, spec.get("path", ""), spec.get("default"))


# ---------------------------------------------------------------- XPath（lxml 内置）

def _lxml_doc(html: str):
    try:
        return _lxml_html.fromstring(html)
    except Exception:
        return None


def xpath_elements(html: str, xpath: str) -> List[Any]:
    doc = _lxml_doc(html)
    if doc is None:
        return []
    try:
        return doc.xpath(xpath)
    except Exception:
        return []


def xpath_text(html: str, xpath: str, limit: int = 0, joiner: str = "\n") -> str:
    parts = []
    for el in xpath_elements(html, xpath):
        if isinstance(el, str):
            t = el
        else:
            t = el.text_content() if hasattr(el, "text_content") else str(el)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            parts.append(t)
    text = joiner.join(parts)
    return text[:limit] if limit and len(text) > limit else text


def xpath_attr(html: str, xpath: str, attr: str = "href", limit: int = 0) -> str:
    out = []
    for el in xpath_elements(html, xpath):
        if isinstance(el, str):
            continue
        v = el.get(attr) if hasattr(el, "get") else ""
        if v:
            out.append(v)
    text = " ".join(out)
    return text[:limit] if limit and len(text) > limit else text




def _lxml_html_tostring(el) -> str:
    """把 lxml 元素序列化为 HTML 字符串（用于在行内继续用 CSS/XPath 提取）。"""
    try:
        from lxml import html as _h
        return _h.tostring(el, encoding="unicode")
    except Exception:
        return str(el)


def extract_embedded_json_rows(html: str, spec: Any) -> List[Dict[str, Any]]:
    """从页面 <script> 内嵌 JSON 提取记录行（SSR 常见：window.X = [...]、
    module 作用域 const X = [...]、裸 X = [...] 都认）。

    spec: 变量引用字符串，如 "window.article_list" / "article_list"；
          或 {"var": "window.article_list", "path": "list"}（path 为解析后下钻的点路径，
          数组下标用数字段）。
    只解析 JSON 兼容字面量（容忍尾逗号）；单引号/无引号键等非标 JS 不支持。
    找不到变量或解析失败一律返回 []，由上层按 0 条走诊断，不假成功。
    """
    import json as _json

    if isinstance(spec, dict):
        ref = str(spec.get("var") or "")
        drill = str(spec.get("path") or "")
    else:
        ref, drill = str(spec or ""), ""
    if not ref:
        return []
    name = ref.split(".")[-1].strip()
    if not name:
        return []

    val = None
    # 三种赋值形态按序尝试；每种形态遍历全部出现位置，直到有一处成功解析
    pats = (
        rf"window\.{re.escape(name)}\s*(?<![=!<>])=(?!=)\s*",
        rf"(?:var|let|const)\s+{re.escape(name)}\s*(?<![=!<>])=(?!=)\s*",
        rf"(?<![\w$.]){re.escape(name)}\s*(?<![=!<>])=(?!=)\s*",
    )
    for pat in pats:
        for m in re.finditer(pat, html):
            val = _balanced_json(html, m.end(), _json)
            if val is not None:
                break
        if val is not None:
            break
    if val is None:
        return []

    if drill:
        for part in drill.split("."):
            if isinstance(val, dict):
                val = val.get(part)
            elif isinstance(val, list):
                try:
                    val = val[int(part)]
                except (ValueError, IndexError):
                    return []
            else:
                return []
            if val is None:
                return []
    if isinstance(val, dict):
        val = [val]
    if not isinstance(val, list):
        return []
    return [r for r in val if isinstance(r, dict)]


def _balanced_json(s: str, start: int, json_mod: Any) -> Any:
    """从 start 起跳过空白后必须出现 [ 或 {，用括号配平截出字面量并解析。
    字符串内的引号/转义/括号不影响配平。解析失败（含尾逗号容忍一次）返回 None。"""
    n = len(s)
    i = start
    while i < n and s[i] in " \t\r\n":
        i += 1
    if i >= n or s[i] not in "[{":
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, n):
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                literal = s[i:j + 1]
                try:
                    return json_mod.loads(literal)
                except Exception:
                    pass
                try:
                    return json_mod.loads(re.sub(r",\s*([}\]])", r"\1", literal))
                except Exception:
                    return None
    return None
