#!/usr/bin/env python3
"""内置流水线：过滤/去重/清洗/常量。"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Optional

from ..protocols import BasePipeline

_META_KEYS = ("_url", "_parser", "_ts", "_id")


def _parse_zh_datetime(text: str, base) -> Optional[Any]:
    """把常见中文相对/绝对时间文本转成北京时间 datetime（解析失败返回 None）。

    支持：刚刚 / 现在；今天/今日/昨天/昨日/前天/大前天 [+HH:MM]；
    N分钟前/N小时前/N天前/N秒前；2026-8-7 11:08 / 2026年8月7日 11:08。
    多个时间（多楼层拼接的“发表于…”）逐行解析，取第一个成功（楼主发布时间优先）。
    """
    import datetime as _dt
    if not text or not str(text).strip():
        return None
    tz8 = _dt.timezone(_dt.timedelta(hours=8))
    lines = [ln.strip() for ln in str(text).replace("\r", "").split("\n") if ln.strip()]
    for line in lines:
        # 去掉常见前缀词（发表于/发布于/创建于/最后发表/发帖/时间/日期 等）
        v = re.sub(r"^(发表于|发布于|创建于|最后发表|发帖时间|时间|日期|更新于|编辑于|来自)[:：]?\s*", "", line)
        if not v:
            continue
        # 1) 刚刚 / 现在
        if re.fullmatch(r"(刚刚|现在|刚刚发布|just now)", v, re.I):
            return base
        # 2) 绝对日期 2026-8-7 11:08 / 2026年8月7日11:08 / 2026-08-07
        m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})[日]?(?:[ T]+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?", v)
        if m:
            try:
                return _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                    int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
                                    tzinfo=tz8)
            except Exception:
                pass
        # 3) 今天/今日 [+HH:MM]
        m = re.match(r"^(今天|今日)[\s:：]*(\d{1,2}):(\d{1,2})", v)
        if m:
            try:
                return base.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
            except Exception:
                pass
        if re.fullmatch(r"(今天|今日)", v):
            return base.replace(hour=0, minute=0, second=0, microsecond=0)
        # 4) 昨天/昨日 [+HH:MM]
        m = re.match(r"^(昨天|昨日)[\s:：]*(\d{1,2}):(\d{1,2})", v)
        if m:
            try:
                d0 = (base - _dt.timedelta(days=1)).replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
                return d0
            except Exception:
                pass
        if re.fullmatch(r"(昨天|昨日)", v):
            return (base - _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        # 5) 前天/大前天 [+HH:MM]
        for i, w in enumerate(("前天", "大前天"), 2):
            m = re.match(rf"^{w}[\s:：]*(\d{{1,2}}):(\d{{1,2}})", v)
            if m:
                try:
                    return (base - _dt.timedelta(days=i)).replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
                except Exception:
                    pass
            if re.fullmatch(w, v):
                return (base - _dt.timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        # 6) N天前 / N小时前 / N分钟前 / N秒前
        m = re.search(r"(\d+)\s*(天|小时|分钟|秒)前", v)
        if m:
            n = int(m.group(1))
            unit = m.group(2)
            if unit == "天":
                return base - _dt.timedelta(days=n)
            if unit == "小时":
                return base - _dt.timedelta(hours=n)
            if unit == "分钟":
                return base - _dt.timedelta(minutes=n)
            if unit == "秒":
                return base - _dt.timedelta(seconds=n)
        # 7) HH:MM（无日期修饰：视为今天）
        m = re.match(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$", v)
        if m:
            try:
                return base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3) or 0), microsecond=0)
            except Exception:
                pass
    return None



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
        self.dropped = {}
        self.skipped = {}

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
                if op == "regex":
                    # 兼容 AI 生成写法：value 或 pattern 都认（历史上 AI 常写 value）
                    _pat = step.get("pattern", "") or step.get("value", "")
                    if _pat:
                        try:
                            if not re.search(_pat, val):
                                return None
                        except re.error:
                            # 非法正则（AI 常生成）：跳过该过滤并保留行，不崩溃也不误丢数据
                            self.skipped[f"filter:{field}:非法正则"] = self.skipped.get(f"filter:{field}:非法正则", 0) + 1
                            continue
                if op == "between":
                    raw = item.get(field)
                    if raw in (None, ""):
                        self.skipped[f"filter:{field}:日期缺失"] = self.skipped.get(f"filter:{field}:日期缺失", 0) + 1
                        continue
                    try:
                        v = float(str(raw).replace(",", ""))
                        lo = float(step.get("min", float("-inf")))
                        hi = float(step.get("max", float("inf")))
                        if not (lo <= v < hi):
                            self.dropped[f"filter:{field}:区间外"] = self.dropped.get(f"filter:{field}:区间外", 0) + 1
                            return None
                    except (ValueError, TypeError):
                        self.skipped[f"filter:{field}:无法解析"] = self.skipped.get(f"filter:{field}:无法解析", 0) + 1
                        continue
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
                _prev = self.vars.get("_seen")
                if not isinstance(_prev, set):
                    _prev = set()  # vars 同键可能被其他步骤写为 str 等类型，重置为 set
                    self.vars["_seen"] = _prev
                _prev.add(k)
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
            elif t == "parse_date":
                # 把日期文本（2026-08-06 / 今天 / 昨天 / 前天 / N天前 / N小时前 / N分钟前 /
                # 「发表于 昨天 22:47」这类带前缀/多楼层拼接的相对时间）转 Unix 秒（北京时间）
                import datetime as _dt
                import time as _t
                field = step.get("field", "date")
                out_f = step.get("out", "timestamp")
                v = str(item.get(field) or "").strip()
                now = step.get("now")
                try:
                    if not now:
                        now = _t.time()
                    base = _dt.datetime.fromtimestamp(float(now), tz=_dt.timezone(_dt.timedelta(hours=8)))
                    d = _parse_zh_datetime(v, base)
                except Exception:
                    d = None
                if d is not None:
                    item[out_f] = int(d.timestamp())
                # 解析失败/无日期：不写字段（保持缺失，让 between 跳过而不是全丢）
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
                        fname = _h.md5(u.encode(), usedforsecurity=False).hexdigest()[:16] + ext
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
