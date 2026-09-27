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
                if i == len(tokens) - 1:
                    # 审查修复（P1，R7）：尾通配 `list.*` 曾把每个元素递归成 default
                    # ——data.items.* 全部静默变 None。尾星直接返回元素列表
                    return cur
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
        doc = None
        try:
            doc = _lxml_html.fromstring(html)
            return doc.cssselect(real)
        except Exception:
            pass  # fromstring/cssselect 失败时 doc 可能为 None——下方守卫
        if doc is not None:
            xp = _css_to_xpath(real)
            if xp:
                try:
                    return doc.xpath(xp)
                except Exception:
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
        # 审查三轮（H）：退化路径返回原始 HTML 字符串——曾直接 tostring 崩
        if isinstance(el, str):
            out.append(el)
            continue
        s = _lxml_html.tostring(el, encoding="unicode") if HAS_LXML else str(el)
        if s:
            out.append(s)
    text = " ".join(out)
    return text[:limit] if limit and len(text) > limit else text


# ---------------------------------------------------------------- 正则

_TIMED_OUT_PATTERNS: set = set()   # 已知危险模式（lint 拒绝过一次的直接跳过）
_REGEX_TEXT_CAP = 64 * 1024        # 用户正则参与的文本上限（截断防超长输入放大）
_MAXREP = 0                        # sre_parse.MAXREPEAT（regex_is_dangerous 首调时填充）


# R47 F4 精度：已知 CATEGORY 映射具体字符集——让 (\d+|color) 这类首集
# 实际不相交的组合不被误判；未知类别仍记 "*"（保守相交）
_CAT_SETS = {
    "CATEGORY_DIGIT": "0123456789",
    "CATEGORY_WORD": ("abcdefghijklmnopqrstuvwxyz"
                      "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"),
    "CATEGORY_SPACE": " \t\n\r\f\v",
}


def _first_of_seq(ops, out: set, ic: bool) -> bool:
    """R47 重写：收集序列首字符集（遇第一个必消费元素即止，不再把整个分支
    的字母表都算进首集——旧写法让 (ab|ba)+ 被 R3 误杀）；返回序列是否可空。
    ic=True 时字符统一小写化（IGNORECASE 下 A 与 a 首集相交）。
    LITERAL 在 sre_parse 里是码点 int，统一转 chr 字符串便于集合运算。"""
    for op, av in ops:
        o = str(op)
        if o in ("MAX_REPEAT", "MIN_REPEAT"):
            lo, _hi, inner = av
            sub: set = set()
            _first_of_seq(inner, sub, ic)  # OCR R131（L）：返回值曾存而不用（可空性由 lo==0 判定）
            out |= {t if (t == "*" or not ic) else str(t).lower() for t in sub}
            if lo == 0:
                continue  # 可空重复：后续元素同样属于首集
            return False
        elif o == "SUBPATTERN":
            if not _first_of_seq(av[-1], out, ic):
                return False
        elif o == "BRANCH":
            any_nullable = False
            for b in av[1]:
                bs: set = set()
                if _first_of_seq(b, bs, ic):
                    any_nullable = True
                out |= {t if (t == "*" or not ic) else str(t).lower() for t in bs}
            if not any_nullable:
                return False
        elif o in ("ASSERT", "ASSERT_NOT"):
            continue  # 零宽断言不消费字符
        elif o == "IN":
            for sub in av:
                so = str(sub[0])
                if so == "LITERAL":
                    ch = chr(sub[1]) if isinstance(sub[1], int) else str(sub[1])
                    out.add(ch.lower() if ic else ch)
                elif so in _CAT_SETS:
                    cs = _CAT_SETS[so]
                    out |= {c.lower() if ic else c for c in cs}
                else:  # 未知字符类：视为未知通配
                    out.add("*")
            return False
        elif o == "ANY":
            out.add("*")
            return False
        elif o == "LITERAL":
            ch = chr(av) if isinstance(av, int) else str(av)
            out.add(ch.lower() if ic else ch)
            return False
        else:
            out.add("*")
            return False
    return True


def _elements(ops, ic: bool) -> list:
    """序列展开成元素 [(首集, 可空)]；SUBPATTERN 透明展开一层。"""
    elems = []
    for op, av in ops:
        if str(op) == "SUBPATTERN":
            elems.extend(_elements(av[-1], ic))
        else:
            s: set = set()
            nullable = _first_of_seq([(op, av)], s, ic)
            elems.append((s, nullable))
    return elems


def _regex_sub_conflicts(sub, ic: bool) -> bool:
    """无界重复体的灾难形态判定（近似，宁缺勿滥——误杀合法模式会丢数据）：
    R1 嵌套无界重复（(a+)+）；R2 重复体整体可空（(a?)+ / (a*)+）；R2b 体含
    可空分支（sre 重写 (a|aa)+ → a(?:|a)+）；R3 体分支首集相交（(a|ab)+）；
    F2 序列内可空元素与其后元素首集相交（(a?\\w)+）。
    F1：零宽断言体（ASSERT）同样进入递归。ic = IGNORECASE 生效中。"""
    def _nullable(ops) -> bool:
        """子式能否匹配空串（序列全部元素可空）。"""
        for op, av in ops:
            o = str(op)
            if o in ("MAX_REPEAT", "MIN_REPEAT"):
                lo, _hi, inner = av
                if lo > 0 and not _nullable(inner):
                    return False
            elif o == "SUBPATTERN":
                if not _nullable(av[-1]):
                    return False
            elif o == "BRANCH":
                if not any(_nullable(b) for b in av[1]):
                    return False
            elif o in ("ASSERT", "ASSERT_NOT"):
                continue  # 零宽断言不消费
            else:
                # LITERAL / IN / ANY 等实质消费字符
                return False
        return True

    def _sets_hit(s1: set, s2: set) -> bool:
        """通配 "*"（字符类/ANY）与任意具体字符视为相交。"""
        if "*" in s1 or "*" in s2:
            return True
        return bool(s1 & s2)

    def _seq_overlap(ops) -> bool:
        elems = _elements(ops, ic)
        for i, (s1, n1) in enumerate(elems):
            if not n1:
                continue  # 只关心可空元素（多为 lo==0 重复/可空分支）
            rest: set = set()
            for s2, _n2 in elems[i + 1:]:
                rest |= s2
            if _sets_hit(s1, rest):
                return True
        return False

    def _branch_overlap(ops) -> bool:
        for op, av in ops:
            o = str(op)
            if o == "BRANCH":
                sets = []
                for b in av[1]:
                    s2: set = set()
                    _first_of_seq(b, s2, ic)
                    sets.append(s2)
                for i in range(len(sets)):
                    for j in range(i + 1, len(sets)):
                        # R61 修正：首集相交即致命——(a|a)+$ 实测 2^n 指数爆炸，
                        # 等长定长分支并不能豁免。sre 把 (a|a) 常数折叠成
                        # a(?:|)（两支均为空），故"两支首集皆为空集（均可空）"
                        # 同样算相交。首集不相交的分支（(ab|ba)+、(\d+|color)）
                        # 在 _sets_hit 已放行，不受影响
                        si, sj = sets[i], sets[j]
                        if (not si and not sj) or _sets_hit(si, sj):
                            return True
            elif o == "SUBPATTERN":
                if _branch_overlap(av[-1]):
                    return True
        return False

    def _has_nullable_branch(ops) -> bool:
        """重复体含"可空分支"且另有非空分支（R47 修正）——纯空分支组（全部
        可空）每迭代恒消费 0 字符、无长度变异，不构成回溯；混合才有变长歧义。
        sre 会把 (a|aa)+ 重写成 a(?:|a)+——空分支 + 非空分支正是变长体痕迹。"""
        for op, av in ops:
            o = str(op)
            if o == "BRANCH":
                nb = [_nullable(b) for b in av[1]]
                if any(nb) and any(not x for x in nb):
                    return True
                for b in av[1]:
                    if _has_nullable_branch(b):
                        return True
            elif o == "SUBPATTERN":
                if _has_nullable_branch(av[-1]):
                    return True
            elif o in ("MAX_REPEAT", "MIN_REPEAT"):
                if _has_nullable_branch(av[2]):
                    return True
        return False

    def _single_variable_repeat(ops) -> bool:
        """R4（审查八轮新增）：重复体是"单个变长重复元素"——(a{1,3})+ / ([a-z]{2,5})+。
        每次迭代可消费 1..n 个同一字符集字符，切分方式数随长度指数增长（实测
        (a{1,3})+$ 对 24 字符 0.27s、每 +2 字符涨约 3.3 倍）。只在"整体仅此一个
        消费元素"时判：定长（lo==hi，如 (a{3})+）安全；首集不重叠的多元素体
        （如 (ab{1,3})+，每次迭代起点由 'a' 唯一确定）同样安全，不误杀。"""
        consuming = [(op, av) for op, av in ops
                     if str(op) not in ("ASSERT", "ASSERT_NOT")]
        if len(consuming) != 1:
            return False
        op, av = consuming[0]
        o = str(op)
        if o in ("MAX_REPEAT", "MIN_REPEAT"):
            lo, hi, _inner = av
            return hi > lo
        if o == "SUBPATTERN":
            return _single_variable_repeat(av[-1])
        return False

    def _walk(ops, in_unb: bool) -> bool:
        for op, av in ops:
            o = str(op)
            if o in ("MAX_REPEAT", "MIN_REPEAT"):
                _lo, hi, inner = av
                unb = (hi == _MAXREP)
                if unb:
                    if in_unb:
                        return True           # R1：(a+)+ / ((a+)+)
                    if _nullable(inner):
                        return True           # R2：(a?)+ / (a*)+ / ()+
                    if _branch_overlap(inner):
                        return True           # R3：(a|ab)+
                    if _has_nullable_branch(inner):
                        return True           # R2b：(a|aa)+（sre 重写为 a(?:|a)+）
                    if _seq_overlap(inner):
                        return True           # F2：(a?\\w)+ 可空元素邻接相交
                    if _single_variable_repeat(inner):
                        return True           # R4：(a{1,3})+ 有界变长重复套无界重复
                if _walk(inner, in_unb or unb):
                    return True
            elif o == "SUBPATTERN":
                if _walk(av[-1], in_unb):
                    return True
            elif o in ("ASSERT", "ASSERT_NOT"):
                # F1 新增：零宽断言体同样可藏嵌套无界回溯（(?=(a+)+b)）
                if _walk(av[-1], in_unb):
                    return True
            elif o == "BRANCH":
                for b in av[1]:
                    if _walk(b, in_unb):
                        return True
        return False

    return _walk(sub, False)


def regex_is_dangerous(pattern: str, flags: int = 0) -> bool:
    """静态 lint（R24 修复；R47 对抗加固）：CPython 的 re 在 C 层回溯不释放
    GIL——事前拒绝灾难模式：嵌套无界量词、可空重复体、可空/相交分支、
    零宽断言体内的同类形态。flags 必须与实际匹配一致（R47 F3：IGNORECASE
    会让字面首集相交，parse 不折叠，lint 必须自己感知）。"""
    try:
        import sre_parse as _sre_parse
    except ImportError:  # 3.13+ 弃用迁移兜底
        import re._parser as _sre_parse
    global _MAXREP
    _MAXREP = _sre_parse.MAXREPEAT
    try:
        tree = _sre_parse.parse(pattern, flags=flags)
    except Exception:
        return False  # 非法正则交给 re.error 路径
    _ic = bool(flags & re.IGNORECASE) or bool(
        getattr(getattr(tree, "state", None), "flags", 0) & re.IGNORECASE)
    return _regex_sub_conflicts(tree, _ic)


def _regex_search_bounded(text: str, pattern: str, flags: int, cap: int = 0):
    """R24 重构：lint 拒绝灾难模式 + 文本截断——两层事前防线取代无效的线程
    超时。线性/温和模式不受影响；被拒模式进黑名单并按无匹配处理。cap=0 用
    默认 64KB；内部可信固定模式（如 sitemap 解析）可显式给大 cap。
    R47 F3：黑名单按 (pattern, flags) 键控——同一模式在不同 flags 下危险性
    不同（IGNORECASE 让字面首集相交），不能跨 flags 共享。"""
    key = (pattern, flags)
    if key in _TIMED_OUT_PATTERNS:
        return None
    if regex_is_dangerous(pattern, flags):
        # OCR R131（M）：黑名单无上界——超限清空重记（与 regex_extract_all 同口径）
        if len(_TIMED_OUT_PATTERNS) >= 4096:
            _TIMED_OUT_PATTERNS.clear()
        _TIMED_OUT_PATTERNS.add(key)
        return None
    t = text or ""
    _cap = cap or _REGEX_TEXT_CAP
    if len(t) > _cap:
        t = t[:_cap]
    try:
        return re.search(pattern, t, flags)
    except re.error:
        return None


def regex_extract(text: str, pattern: str, group: int = 0, flags: int = re.S | re.I,
                  cap: int = 0) -> str:
    if not pattern:
        return ""
    m = _regex_search_bounded(text or "", pattern, flags, cap)
    if not m:
        return ""
    try:
        return m.group(group) or ""
    except IndexError:
        return m.group(0)


def regex_extract_all(text: str, pattern: str, group: int = 0, cap: int = 0,
                      flags: int = re.S | re.I) -> List[str]:
    if not pattern:
        return []
    # R24 重构：与 regex_extract 同防线（lint + 截断）后同步 finditer——
    # 旧的"本体也放有界线程"同样受 GIL 约束不可靠
    # OCR R131（M）：flags 形参化——与 regex_extract 对齐（此前硬编码 S|I，
    # 两个 API 的同 pattern 行为不一致）
    _fl = flags
    key = (pattern, _fl)
    if key in _TIMED_OUT_PATTERNS:
        return []
    if regex_is_dangerous(pattern, _fl):
        # OCR R131（M）：黑名单集合无上界——恶意/失控输入下线性膨胀。超限时
        # 清空重记（lint 命中会重新加回，只损失部分短路）
        if len(_TIMED_OUT_PATTERNS) >= 4096:
            _TIMED_OUT_PATTERNS.clear()
        _TIMED_OUT_PATTERNS.add(key)
        return []
    t = text or ""
    _cap = cap or _REGEX_TEXT_CAP
    if len(t) > _cap:
        t = t[:_cap]
    _out: List[str] = []
    try:
        for mm in re.finditer(pattern, t, _fl):
            # R15 审查（P2）：group 越界回退 group(0)
            try:
                _out.append(mm.group(group) or "")
            except IndexError:
                _out.append(mm.group(0))
    except re.error:
        return []
    return _out


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
    if etype == "text":
        # OCR R131（M）：默认 type="text" 曾落到底部 jpath 分支——纯文本提取
        # 语义下返回的是 JSON 对象查询结果（恒 default），静默错值
        return ctx_text
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
