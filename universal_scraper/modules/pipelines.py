#!/usr/bin/env python3
"""内置流水线：过滤/去重/清洗/常量。"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Optional

from ..protocols import BasePipeline

_META_KEYS = ("_url", "_parser", "_ts", "_id")


def content_hash(item: Dict[str, Any], fields: Optional[list] = None) -> str:
    """对条目算 SHA-256 内容指纹（对标 browsertrix-crawler-deduplication 内容哈希去重）。
    fields 指定时只对这几个字段；否则对所有非元字段。"""
    if fields:
        payload = {k: item.get(k) for k in fields}
    else:
        payload = {k: v for k, v in item.items() if k not in _META_KEYS}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Pipeline(BasePipeline):
    name = "pipeline"

    def __init__(self, config, task_vars):
        super().__init__(config, task_vars)
        self.steps = config or []

    def process(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for step in self.steps:
            t = step.get("type")
            if t == "filter":
                field, op, value = step["field"], step.get("op", "contains"), step.get("value")
                val = str(item.get(field) or "")
                if op == "non_empty" and not val.strip():
                    return None
                if op == "contains" and value not in val:
                    return None
                if op == "not_contains" and value and value in val:
                    return None
                if op == "eq" and val != str(value):
                    return None
                if op == "regex" and not re.search(step.get("pattern", ""), val):
                    return None
                if op == "between":
                    try:
                        v = float(str(item.get(field)).replace(",", ""))
                        lo = float(step.get("min", float("-inf")))
                        hi = float(step.get("max", float("inf")))
                        if not (lo <= v < hi):
                            return None
                    except (ValueError, TypeError):
                        return None
            elif t == "dedup":
                key = step.get("key", "id")
                if key == "content_hash":
                    k = content_hash(item, step.get("fields"))
                    if k in self.vars.get("_seen_hash", set()):
                        return None
                    self.vars.setdefault("_seen_hash", set()).add(k)
                    continue
                keys = key if isinstance(key, list) else [key]
                k = tuple(str(item.get(kk) or "") for kk in keys)
                if any(x in ("None", "") for x in k):
                    continue
                if k in self.vars.get("_seen", set()):
                    return None
                self.vars.setdefault("_seen", set()).add(k)
            elif t == "dedup_content":
                # 便捷别名：{"type":"dedup_content","fields":["title","body"]}
                k = content_hash(item, step.get("fields"))
                if k in self.vars.get("_seen_hash", set()):
                    return None
                self.vars.setdefault("_seen_hash", set()).add(k)
            elif t == "cast":
                field, ctype = step["field"], step.get("to", "str")
                v = item.get(field)
                try:
                    if ctype == "int" and v not in (None, ""):
                        item[field] = int(float(str(v).replace(",", "")))
                    elif ctype == "float" and v not in (None, ""):
                        item[field] = float(str(v).replace(",", ""))
                    elif ctype == "str":
                        item[field] = str(v) if v is not None else ""
                except (ValueError, TypeError):
                    pass
            elif t == "add":
                item[step["field"]] = step.get("value")
            elif t == "validate":
                field = step["field"]
                val = item.get(field)
                if step.get("required") and val in (None, ""):
                    return None
                as_type = step.get("as")
                if as_type and val not in (None, ""):
                    try:
                        if as_type == "int":
                            int(float(str(val).replace(",", "")))
                        elif as_type == "float":
                            float(str(val).replace(",", ""))
                        elif as_type == "str":
                            str(val)
                    except (ValueError, TypeError):
                        return None
                if step.get("pattern") and val not in (None, ""):
                    if not re.search(step["pattern"], str(val)):
                        return None
            elif t == "rename":
                for old, new in step.get("mapping", {}).items():
                    if old in item:
                        item[new] = item[old]
                        del item[old]
            elif t == "default":
                fld = step["field"]
                if fld not in item or item.get(fld) in (None, ""):
                    item[fld] = step.get("value")
            elif t == "template":
                try:
                    item[step["field"]] = step["template"].format(
                        **{k: (v if v is not None else "") for k, v in item.items()})
                except Exception:
                    item[step["field"]] = step.get("default", "")
            elif t == "split":
                sep = step.get("sep", ",")
                item[step["field"]] = [x.strip() for x in str(item.get(step["field"]) or "").split(sep) if x.strip()]
            elif t == "download":
                # 通用文件下载（PDF/图片/附件）：从 item 字段取 URL 下载到 dir，写回本地路径
                field = step.get("field", "url")
                out_field = step.get("out_field", "local_file")
                out_dir = step.get("dir", "downloads")
                u = str(item.get(field) or "").strip()
                if not u.startswith(("http://", "https://")):
                    item[out_field] = ""
                    continue
                try:
                    from pathlib import Path as _P
                    from ..core import fetch_bytes
                    _d = _P(out_dir)
                    _d.mkdir(parents=True, exist_ok=True)
                    ext = step.get("ext", "")
                    if not ext:
                        from urllib.parse import urlparse as _up
                        ext = _P(_up(u).path).suffix[:8] or ".bin"
                    fname = step.get("name_template", "").format(**{k: str(v)[:60] for k, v in item.items()}) if step.get("name_template") else ""
                    if not fname:
                        import hashlib as _h
                        fname = _h.md5(u.encode()).hexdigest()[:16] + ext
                    fname = re.sub(r'[\\/:*?"<>|\r\n]+', "_", fname)
                    fp = _d / fname
                    min_size = int(step.get("min_size", 20))
                    if not fp.exists() or fp.stat().st_size < min_size:
                        raw = fetch_bytes(u, headers=step.get("headers"), proxy=step.get("proxy"), timeout=int(step.get("timeout", 60)))
                        if raw:
                            fp.write_bytes(raw)
                    if fp.exists() and fp.stat().st_size >= min_size:
                        item[out_field] = str(fp)
                    else:
                        item[out_field] = ""
                except Exception:
                    item[out_field] = ""
        return item
