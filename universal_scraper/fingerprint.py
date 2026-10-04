#!/usr/bin/env python3
"""元素指纹自适应重定位（审查二十轮 R20，对标 Scrapling 的 auto_save/adaptive）。

用途：站点改版（class 改名/加外层容器/重排）后，选择器失效但数据还在——
按"结构指纹 + 相似度"把元素找回来，而不是整单重学。

与 Scrapling 的差异是我们的护栏（它只 warning，我们上闸）：
1. **只在空结果时触发**：已有非空抽取绝不覆盖——静默错数据比缺数据更糟；
2. **验证门**：候选必须过相似度阈值且取值非空才采用，否则如实报"找回失败"；
3. **存储隔离**：指纹按任务目录落盘（原子写 + 0600），跨任务不串味。

相似度因子（加权平均 ×100）：tag / 文本 / 属性字典 / class·id·href·src 逐项 /
结构路径 / 父节点 / 兄弟标签序列——任一因子缺失只计可得项（与 Scrapling 同口径）。
"""
from __future__ import annotations

import json
import os
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional

FP_VERSION = 1
DEFAULT_THRESHOLD = 40.0          # 与 Scrapling 默认一致（百分比）
_TEXT_CAP = 200
_ATTR_CAP = 8                     # 属性只留前 N 个（防指纹膨胀）
_KEY_ATTRS = ("class", "id", "href", "src")


def _norm_text(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:_TEXT_CAP]


def _attrs_of(el) -> Dict[str, str]:
    try:
        items = sorted((el.attrib or {}).items())[:_ATTR_CAP]
    except Exception:
        return {}
    return {str(k): _norm_text(v)[:80] for k, v in items}


def _path_of(el, cap: int = 12) -> str:
    """结构路径：tag+nth-of-type 链（不含文本，改版后 class 变了仍可比）。"""
    parts: List[str] = []
    node = el
    while node is not None and len(parts) < cap:
        try:
            tag = node.tag
        except Exception:
            break
        if not isinstance(tag, str):
            break
        parent = node.getparent()
        seg = tag
        if parent is not None:
            try:
                same = [c for c in parent if getattr(c, "tag", None) == tag]
                if len(same) > 1:
                    seg = f"{tag}[{same.index(node) + 1}]"
            except Exception:
                pass
        parts.append(seg)
        node = parent
    return "/".join(reversed(parts))


def element_fingerprint(el) -> Dict[str, Any]:
    """元素 → 指纹字典（可 JSON 序列化）。"""
    parent = None
    try:
        p = el.getparent()
        if p is not None and isinstance(getattr(p, "tag", None), str):
            parent = {"tag": p.tag, "attrs": _attrs_of(p)}
    except Exception:
        parent = None
    sib_tags: List[str] = []
    try:
        p = el.getparent()
        if p is not None:
            sib_tags = [c.tag for c in p if isinstance(getattr(c, "tag", None), str)][:8]
    except Exception:
        sib_tags = []
    return {
        "v": FP_VERSION,
        "tag": getattr(el, "tag", "") if isinstance(getattr(el, "tag", None), str) else "",
        "text": _norm_text(el.text_content() if hasattr(el, "text_content") else ""),
        "attrs": _attrs_of(el),
        "path": _path_of(el),
        "parent": parent,
        "siblings": sib_tags,
    }


def _dict_diff(d1: Dict[str, str], d2: Dict[str, str]) -> float:
    if not d1 and not d2:
        return 1.0
    a = SequenceMatcher(None, tuple(sorted(d1)), tuple(sorted(d2))).ratio() * 0.5
    b = SequenceMatcher(None, tuple(sorted(d1.values())), tuple(sorted(d2.values()))).ratio() * 0.5
    return a + b


def similarity(fp: Dict[str, Any], el) -> float:
    """指纹与候选元素的相似度（0-100）。"""
    score = 0.0
    checks = 0
    data = element_fingerprint(el)

    score += 1.0 if fp.get("tag") == data["tag"] else 0.0
    checks += 1
    if fp.get("text"):
        score += SequenceMatcher(None, fp["text"], data["text"]).ratio()
        checks += 1
    score += _dict_diff(fp.get("attrs") or {}, data["attrs"])
    checks += 1
    for k in _KEY_ATTRS:
        want = (fp.get("attrs") or {}).get(k)
        if want:
            score += SequenceMatcher(None, want, data["attrs"].get(k) or "").ratio()
            checks += 1
    score += SequenceMatcher(None, fp.get("path") or "", data["path"]).ratio()
    checks += 1
    pf = fp.get("parent")
    if isinstance(pf, dict) and pf.get("tag"):
        p_now = data.get("parent") or {}
        if p_now.get("tag"):
            score += 1.0 if pf["tag"] == p_now["tag"] else 0.0
            checks += 1
            score += _dict_diff(pf.get("attrs") or {}, p_now.get("attrs") or {})
            checks += 1
    if fp.get("siblings"):
        score += SequenceMatcher(None, tuple(fp["siblings"]),
                                 tuple(data.get("siblings") or [])).ratio()
        checks += 1
    return round((score / checks) * 100, 2) if checks else 0.0


def relocate(root_el, fp: Dict[str, Any], threshold: float = DEFAULT_THRESHOLD) -> List[Any]:
    """在 root 子树里找回与指纹最相似的元素（返回并列最高分的全部候选）。

    低于阈值返回 []（调用方按"找回失败"如实处理，不得猜）。"""
    if root_el is None or not isinstance(fp, dict) or not fp.get("tag"):
        return []
    best = -1.0
    ties: List[Any] = []
    try:
        nodes = root_el.iter()
    except Exception:
        return []
    for el in nodes:
        if not isinstance(getattr(el, "tag", None), str):
            continue
        s = similarity(fp, el)
        if s > best + 1e-9:
            best, ties = s, [el]
        elif abs(s - best) <= 1e-9:
            ties.append(el)
    if best >= float(threshold):
        return ties
    return []


def relocate_in_html(html_text: str, fp: Dict[str, Any],
                     threshold: float = DEFAULT_THRESHOLD) -> List[Any]:
    """HTML 字符串版本：解析后找回候选元素。"""
    try:
        from lxml import html as _html
        root = _html.fromstring(html_text or "")
    except Exception:
        return []
    return relocate(root, fp, threshold)


def element_for_spec(doc, spec: Dict[str, Any]):
    """按 extract/field 规格取第一个命中元素（用于保存指纹）。失败返回 None。"""
    try:
        css = spec.get("selector") or spec.get("css") or ""
        if not css:
            return None
        if css.startswith("/") and not re.match(r"^[.#\[]", css):
            els = doc.xpath(css)
        else:
            els = doc.cssselect(css) if hasattr(doc, "cssselect") else []
            if not els:
                from .selectors import css_elements
                els = css_elements(doc, css)
        for el in els or []:
            if isinstance(getattr(el, "tag", None), str):
                return el
    except Exception:
        return None
    return None


def value_from_element(el, spec: Dict[str, Any]) -> str:
    """按 spec 类型从"命中元素自身"取值（找回路径专用）。

    为什么不能复用原选择器：找回场景里选择器正是坏掉的那个——
    存指纹时元素=值载体（element_for_spec 命中的元素），所以取值口径是
    "元素自身"：css_text/xpath_text → 自身文本；*_attr → 自身属性；css_html → 自身 HTML。
    regex/json 等无元素语义的类型不参与找回（返回 ""，如实失败）。"""
    et = str(spec.get("type") or "text")
    try:
        if et in ("css_text", "xpath_text", "xpath", "text"):
            return re.sub(r"\s+", " ", el.text_content() or "").strip()
        if et in ("css_attr", "xpath_attr"):
            return str(el.get(spec.get("attr") or "href") or "").strip()
        if et == "css_html":
            try:
                from lxml import etree as _etree
                return _etree.tostring(el, encoding="unicode", method="html")
            except Exception:
                return ""
    except Exception:
        return ""
    return ""


def rescue_field(store, key: str, html_text: str, spec: Dict[str, Any],
                 current: str = "", threshold: float = DEFAULT_THRESHOLD):
    """字段级自适应闭环（v3 详情主链路用）。返回 (value, action)：

    - current 非空 → 保存/更新指纹，action="saved"（值原样返回，绝不改动）
    - current 为空且库里有指纹 → 相似度找回 → **验证门**（元素自身取值非空）
      才采用；成功回写新指纹，action="rescued"
    - 找不到/验证不过 → 返回 (current, "none")——如实失败，不猜
    - store 为 None → (None, "disabled")

    闭环策略集中在这里（不在引擎里复制），单测直接打这条路径。"""
    if store is None:
        return None, "disabled"
    if str(current or "").strip():
        try:
            from lxml import html as _html
            el = element_for_spec(_html.fromstring(html_text or ""), spec)
            if el is not None:
                store.put_field(key, element_fingerprint(el))
                return current, "saved"
        except Exception:
            pass
        return current, "kept"
    fp = store.get_field(key)
    if not fp:
        return current, "none"
    cands = relocate_in_html(html_text, fp, threshold=threshold)
    for el in cands:
        val = value_from_element(el, spec)
        if str(val or "").strip():
            try:
                store.put_field(key, element_fingerprint(el))
            except Exception:
                pass
            return val, "rescued"
    return current, "none"


class FingerprintStore:
    """任务级指纹库：<任务目录>/.fingerprints.json（原子写 + 0600 + 线程安全）。

    结构：{"version": 1, "rows": {key: fp}, "fields": {key: fp}}"""

    _LOCK = threading.Lock()

    def __init__(self, path):
        self.path = Path(path)
        self.data: Dict[str, Any] = {"version": FP_VERSION, "rows": {}, "fields": {}}
        self._dirty = False
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("version") == FP_VERSION:
                    self.data["rows"] = raw.get("rows") or {}
                    self.data["fields"] = raw.get("fields") or {}
                # 版本不匹配：按空库处理（旧指纹语义可能不同，宁可重学）
        except Exception:
            self.data = {"version": FP_VERSION, "rows": {}, "fields": {}}

    def get_row(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            v = self.data["rows"].get(key)
        return v if isinstance(v, dict) else None

    def get_field(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            v = self.data["fields"].get(key)
        return v if isinstance(v, dict) else None

    def put_row(self, key: str, fp: Dict[str, Any]) -> None:
        with self._lock:
            self.data["rows"][str(key)] = fp
            self._dirty = True

    def put_field(self, key: str, fp: Dict[str, Any]) -> None:
        with self._lock:
            self.data["fields"][str(key)] = fp
            self._dirty = True

    def flush(self) -> bool:
        """原子落盘（无变更不写）。失败返回 False（调用方静默降级，不阻断主流程）。"""
        with self._lock:
            if not self._dirty:
                return True
            payload = json.dumps(self.data, ensure_ascii=False, default=str)
            self._dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)      # 指纹含页面文本片段，按敏感产物处理
            except OSError:
                pass
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False


def store_path_for(base_dir) -> Path:
    return Path(base_dir or ".") / ".fingerprints.json"
