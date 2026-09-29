#!/usr/bin/env python3
"""内置流水线：过滤/去重/清洗/常量。"""
from __future__ import annotations

import hashlib
import json
import re
import time
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
        # 收官十二轮（审查 L）：日期与时间之间的 `[ T]+` 要求至少一个分隔符——
        # 中文站常见的"2026年8月7日11:08"（无空格）只取到日期，时间静默归零。
        # 改 `[\s T]*`，并支持 "11时08分" 中文时间形态
        m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?\s*"
                      r"(?:[ T]*(\d{1,2})[:时点](\d{1,2})(?:[:分]?(\d{1,2}))?)?", v)
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
    fields 指定时只对这几个字段；否则对所有非元字段。

    收官十二轮（审查，实测）：fields 传字符串（配置写 "fields": "title"，或按
    contract 文档写单数 "field"）曾按**单个字符**建 payload——所有记录哈希相同，
    整批被压成 1 条（静默丢 99%）。此处归一为列表，并由调用侧接受 field/fields 两名。"""
    if isinstance(fields, str):
        fields = [fields]
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
        self._warned = set()   # R24b：跳过类告警只打印一次（防每行刷屏）
        # OCR R131（H）：dedup 的 check-then-act 与 dropped/skipped 计数曾无锁——
        # engine_v3 多 worker 并发 process 同一实例，重复条目可双双通过、计数丢失
        import threading as _th
        self._state_lock = _th.Lock()

    def _bump(self, store: dict, key: str) -> None:
        """OCR R6 终审：dropped/skipped 计数的 read-modify-write 曾无锁——
        engine_v3 多 worker 并发 process 同一实例，`d[k]=d.get(k,0)+1` 非原子，
        并发下计数丢失（与 dedup 同一把 _state_lock）。"""
        with self._state_lock:
            store[key] = store.get(key, 0) + 1

    def _warn_once(self, msg: str) -> None:
        """R24b 修复：skipped/dropped 计数此前无任何出口——危险正则被跳过对
        用户完全不可见。首个跳过发生时打一行 WARN。
        OCR R6 终审：check-then-add 曾无锁——并发下同一告警可重复打印。"""
        with self._state_lock:
            if msg in self._warned:
                return
            self._warned.add(msg)
        try:
            from ..core import log
            log(f"⚠️ {msg}", "WARN")
        except Exception:
            pass

    def process(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for step in self.steps:
            t = step.get("type")
            if t == "filter":
                field, op = step["field"], step.get("op", "contains")
                # 审查八轮（HIGH）：value 未兜底——`None not in val` 直接抛 TypeError。
                # v3 主流程（engine_v3._handle）按"跳过该条"接住 → 整批条目静默归零；
                # detail.filters 路径（_run_details）无守卫 → 抓完后崩掉导出。
                # 与 v2（engine.py:92）同口径：缺失/None 按空串处理。
                _v = step.get("value")
                value = "" if _v is None else str(_v)
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
                    # 收官十二轮（审查 L）：非字符串 pattern（AI 常写数值 2025）会让
                    # re.search 抛 TypeError 冒泡到引擎的 except → 该条被静默丢弃。
                    # 按"非法正则"同口径处理（跳过该步、保留行）
                    if _pat and not isinstance(_pat, str):
                        self._bump(self.skipped, f"filter:{field}:正则非字符串")
                        self._warn_once(f"filter 字段 {field} 的正则不是字符串（{_pat!r}），已跳过该步")
                        continue
                    if _pat:
                        # R24 修复：用户正则必须有 ReDoS 防线——与 selectors 同一
                        # lint + 截断（此前 filter 的正则完全裸奔，一个灾难模式
                        # 冻结全部 worker 且无诊断）
                        from ..selectors import regex_is_dangerous, _REGEX_TEXT_CAP
                        try:
                            if regex_is_dangerous(_pat):
                                self._bump(self.skipped, f"filter:{field}:危险正则已跳过")
                                self._warn_once(f"filter 字段 {field} 的正则被判定为灾难回溯模式，已跳过该步：{_pat[:60]}")
                                continue
                            if not re.search(_pat, val[:_REGEX_TEXT_CAP]):
                                return None
                        except re.error:
                            # 非法正则（AI 常生成）：跳过该过滤并保留行，不崩溃也不误丢数据
                            self._bump(self.skipped, f"filter:{field}:非法正则")
                            continue
                if op == "between":
                    raw = item.get(field)
                    if raw in (None, ""):
                        self._bump(self.skipped, f"filter:{field}:日期缺失")
                        continue
                    try:
                        v = float(str(raw).replace(",", ""))
                        if v != v:
                            # OCR R6：NaN 文本（"nan"/"NaN" 缺失标记）可过 float()，
                            # 但 lo<=nan 恒 False——曾被当"区间外"静默丢行。
                            # 按无法解析跳过保留行（与其余解析失败同口径）
                            raise ValueError("NaN")
                        lo = float(step.get("min", float("-inf")))
                        hi = float(step.get("max", float("inf")))
                        if not (lo <= v < hi):
                            self._bump(self.dropped, f"filter:{field}:区间外")
                            return None
                    except (ValueError, TypeError):
                        self._bump(self.skipped, f"filter:{field}:无法解析")
                        continue
            elif t == "dedup":
                key = step.get("key", "id")
                if key == "content_hash":
                    k = content_hash(item, step.get("fields") or step.get("field"))
                    with self._state_lock:
                        if k in self.vars.get("_seen_hash", set()):
                            return None
                        self.vars.setdefault("_seen_hash", set()).add(k)
                    continue
                keys = key if isinstance(key, list) else [key]
                # 收官十二轮（审查 M，实测）：`or ""` 曾把 0/0.0/False 归一成空串，
                # 整条跳过去重——数值型 key（从 0 起的序号/楼层）的重复行全放行。
                # 缺失只认 None
                k = tuple("" if item.get(kk) is None else str(item.get(kk)) for kk in keys)
                if any(x == "" for x in k):
                    continue
                with self._state_lock:
                    if k in self.vars.get("_seen", set()):
                        return None
                    _prev = self.vars.get("_seen")
                    if not isinstance(_prev, set):
                        _prev = set()  # vars 同键可能被其他步骤写为 str 等类型，重置为 set
                        self.vars["_seen"] = _prev
                    _prev.add(k)
            elif t == "dedup_content":
                # 便捷别名：{"type":"dedup_content","fields":["title","body"]}
                # 收官十二轮（审查）：contract 文档写的单数 field 曾静默忽略——
                # 退化成全字段去重（正文相同、他列不同的两条都保留）。两名都收
                k = content_hash(item, step.get("fields") or step.get("field"))
                with self._state_lock:  # OCR R131 终审：曾漏锁——并发下双条通过
                    if k in self.vars.get("_seen_hash", set()):
                        return None
                    self.vars.setdefault("_seen_hash", set()).add(k)
            elif t == "cast":
                field, ctype = step["field"], step.get("to", "str")
                v = item.get(field)
                try:
                    if ctype == "int" and v not in (None, ""):
                        # 收官十二轮（审查 M）：曾一律 int(float(...))——19 位雪花
                        # ID/订单号经 float 丢精度静默改值（…789 → …768）。
                        # 纯整数字符串直接 int，失败再回退 float 路径
                        _s = str(v).replace(",", "").strip()
                        try:
                            item[field] = int(_s)
                        except ValueError:
                            item[field] = int(float(_s))
                    elif ctype == "float" and v not in (None, ""):
                        item[field] = float(str(v).replace(",", ""))
                    elif ctype == "str":
                        item[field] = str(v) if v is not None else ""
                except (ValueError, TypeError, OverflowError):
                    # OCR R131（属性测试抓获）：超大整数（>1.8e308）经 float 转 int
                    # 抛 OverflowError——曾不在捕获列表，整条管线炸掉
                    pass
            elif t == "regex_extract":
                # 收官十五轮（用户复盘）：v3 此前未实现 regex_extract/transform——
                # 唯一能跑在**详情之后**的入口（detail.filters/post_pipeline）因此
                # 无法清洗 detail.extract 产出的字段，用户被迫外挂 Python 脚本。
                # 语义与 v2 run_pipeline 对齐（field/pattern/group/to），并接受 out 别名
                fld = step.get("field")
                if not fld:
                    self._warn_once("regex_extract 缺 field——步骤跳过")
                    self._bump(self.skipped, "regex_extract:缺field")
                    continue
                _pat = step.get("pattern", "")
                if not isinstance(_pat, str):
                    self._warn_once(f"regex_extract 的 pattern 应为字符串（{_pat!r}），步骤跳过")
                    self._bump(self.skipped, "regex_extract:pattern非字符串")
                    continue
                try:
                    grp = int(step.get("group", 1))
                except (TypeError, ValueError):
                    self._warn_once(f"regex_extract group 非整数（按 1 处理）: {step.get('group')!r}")
                    grp = 1
                to = step.get("to") or step.get("out") or f"{fld}_提取"
                val = item.get(fld)
                if val in (None, ""):
                    continue
                # 与 filter/validate 同口径：ReDoS 防线 + 文本截断
                from ..selectors import regex_is_dangerous, _REGEX_TEXT_CAP
                _txt = str(val)[:_REGEX_TEXT_CAP]
                try:
                    if regex_is_dangerous(_pat):
                        self._bump(self.skipped, f"regex_extract:{fld}:危险正则已跳过")
                        self._warn_once(f"regex_extract 字段 {fld} 的正则被判定为灾难回溯模式，"
                                        f"已跳过该步：{_pat[:60]}")
                        continue
                    m = re.search(_pat, _txt)
                except re.error:
                    self._bump(self.skipped, f"regex_extract:{fld}:非法正则")
                    self._warn_once(f"regex_extract 字段 {fld} 的正则非法，已跳过该步：{_pat[:60]}")
                    continue
                if m:
                    try:
                        item[to] = m.group(grp)
                    except (IndexError, re.error):
                        self._bump(self.skipped, f"regex_extract:{fld}:组号越界")
                        continue
            elif t == "transform":
                # 与 v2 同口径：unix_to_datetime（秒/毫秒自适应）/ upper / lower
                fld = step.get("field")
                if not fld:
                    self._warn_once("transform 缺 field——步骤跳过")
                    self._bump(self.skipped, "transform:缺field")
                    continue
                op = step.get("op", "unix_to_datetime")
                fmt = step.get("fmt", "%Y-%m-%d %H:%M:%S")
                v = item.get(fld)
                if v in (None, ""):
                    continue
                try:
                    if op == "unix_to_datetime":
                        ts = float(str(v).strip())
                        if ts > 9_999_999_999:      # 毫秒时间戳
                            ts /= 1000.0
                        item[fld] = time.strftime(fmt, time.localtime(ts))
                    elif op == "upper":
                        item[fld] = str(v).upper()
                    elif op == "lower":
                        item[fld] = str(v).lower()
                except (ValueError, TypeError, OSError, OverflowError):
                    # 单行坏值不炸整条管线（与 cast 同口径）
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
                    # R24 修复：validate 的正则同样过 lint + 截断防线；
                    # R24b 补齐：与 filter 同样的 re.error 容忍（AI 常生成非法
                    # 正则——跳过该校验保留行，不让整条管道崩掉）
                    from ..selectors import regex_is_dangerous, _REGEX_TEXT_CAP
                    try:
                        if regex_is_dangerous(step["pattern"]):
                            self._bump(self.skipped, f"validate:{step['field']}:危险正则已跳过")
                            self._warn_once(f"validate 字段 {step['field']} 的正则被判定为灾难回溯模式，已跳过该步：{step['pattern'][:60]}")
                        elif not re.search(step["pattern"], str(val)[:_REGEX_TEXT_CAP]):
                            return None
                    except re.error:
                        self._bump(self.skipped, f"validate:{step['field']}:非法正则")
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
                # R109 修复（P2）：v3 曾只认 step["template"] 而文档/contract 写
                # tmpl——KeyError 落 except 后用 default 静默覆盖目标字段
                # R114：tmpl 显式 null 曾经 template 兜底变 "" 再 str 化——
                # 键存在但非字符串（null/数字）一律落 default
                if "tmpl" in step:
                    _tmpl = step["tmpl"]
                elif "template" in step:
                    _tmpl = step["template"]
                else:
                    _tmpl = ""
                if not isinstance(_tmpl, str):
                    item[step["field"]] = step.get("default", "")
                    continue
                try:
                    item[step["field"]] = str(_tmpl).format(
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
                # 空 sep 会让 str.split("") 抛 ValueError——引擎虽按"跳过该条"降级，
                # 但 AI 生成配置写 "sep": "" 时等于整批记录全丢；回落逗号（审查七轮 N40）
                sep = step.get("sep", ",") or ","
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
                # OCR R131（H）：dir 配置曾可 ../../ 逃逸任务目录（写任意路径）。
                # 只允许相对路径且不得包含 .. 段
                from pathlib import PurePosixPath as _PP
                if out_dir.startswith(("/", "\\")) or ":" in out_dir[:3] \
                        or any(seg == ".." for seg in _PP(out_dir.replace("\\", "/")).parts):
                    self._warn_once(f"download dir 非法（仅允许任务内相对路径）: {out_dir!r}，下载跳过")
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
                        else:
                            # 收官十二轮（审查 M）：fetch_bytes 按契约"失败返回 None
                            # 并写 last_error"（404/超限/出站守卫/网络失败）走正常
                            # 返回路径——曾静默置空零告警。此处补明确诊断
                            _err = getattr(fetch_bytes, "last_error", "") or "未知原因"
                            self._warn_once(f"download 未取到内容（{_err}）: {u[:80]}")
                    if fp.exists() and fp.stat().st_size >= min_size:
                        item[out_field] = str(fp)
                    else:
                        item[out_field] = ""
                except Exception as _dl_e:
                    # OCR R131（M）：下载失败曾静默置空——磁盘满/超时对用户不可见
                    self._warn_once(f"download 失败（{type(_dl_e).__name__}: {str(_dl_e)[:80]}）: {u[:80]}")
                    item[out_field] = ""
            else:
                # 审查八轮（MEDIUM）：if/elif 链没有 else——v3 执行器未实现的步骤类型
                # （v2-only 的 regex_extract/transform 等，config 用 ALL_PIPELINE_TYPES
                # 放行）既不报错也不告警，产出静默缺列，只有 verify 事后报"死列"。
                # 这里只告警一次并计数（不抛错：既有任务包不能因一行未知步骤整体失败）。
                self._warn_once(f"流水线步骤类型 '{t}' 未被 v3 执行器实现，已跳过"
                                "（v2-only 步骤请用 run --config；详见 references/spec-schema.md）")
                self._bump(self.skipped, f"pipeline:{t}:未实现")
        return item
