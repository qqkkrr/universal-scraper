#!/usr/bin/env python3
"""React/Vue SSR 站的 window.__INITIAL_STATE__ 提取器（实战反馈五#5 入库）。

来源：小红书 xhs_damo_task 的 extract_state.py——水合脚本里 `undefined`/`NaN`/
`new Set(X)`/`new Map(X)` 会让 json.loads 直接失败，且 `</script>` 截断让正则
拿不到完整对象。本模块的解法（括号配平扫描 + JS 字面量修复）对所有把状态挂在
window 全局上的 SSR 站通用，变量名可参数化。

用法:
    from universal_scraper.ssr_state import extract_state
    st = extract_state(html, var="__INITIAL_STATE__")
"""
from __future__ import annotations

import json
from typing import Any, Optional


def _find_marker_outside_str(blob: str, marker: str, start: int) -> int:
    """在双引号字符串区段之外查找 marker；返回位置或 -1。
    审查八轮（C1）：blob.find 曾不感知字符串——字符串值内含 "new Set(items)"
    字样（教程/文档类站点正文）会被整体剥成 "items"（静默数据污染，与
    _repair_js_literals 六轮 M1 同型）。"""
    n = len(blob)
    i = start
    while i < n:
        ch = blob[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if blob[j] == "\\":
                    j += 2
                    continue
                if blob[j] == '"':
                    break
                j += 1
            i = min(j + 1, n)
            continue
        if blob.startswith(marker, i):
            return i
        i += 1
    return -1


def _replace_ctor(blob: str, name: str) -> str:
    """把 new Set(X)/new Map(X) 替换为 X（X 为字面量），供 json.loads 使用。
    审查六轮（M2a）：嵌套同类 ctor（new Set(new Set(x))）一轮剥不完——
    整串反复剥直到无 marker（每轮剥最外层，层数=N 轮）。"""
    marker = "new " + name + "("
    while True:
        k = _find_marker_outside_str(blob, marker, 0)
        if k < 0:
            return blob
        # 配平 marker 后的括号（字符串感知）
        depth = 1
        q = k + len(marker)
        in_str = False
        esc = False
        while q < len(blob) and depth > 0:
            ch = blob[q]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            q += 1
        inner = blob[k + len(marker): q - 1]
        blob = blob[:k] + inner + blob[q:]


def _repair_js_literals(blob: str) -> str:
    """SSR 水合脚本里的非 JSON 字面量 → JSON 等价物。
    审查六轮（M1）：re.sub 曾不感知字符串——正文里独立词 "Infinity War"/"NaN"
    被改写成 null（静默数据污染）。改单遍扫描：跳过双引号字符串区段再替换。"""
    out = []
    i = 0
    n = len(blob)
    while i < n:
        ch = blob[i]
        if ch == '"':
            # 字符串区段原样保留（含转义）
            j = i + 1
            while j < n:
                if blob[j] == "\\":
                    j += 2
                    continue
                if blob[j] == '"':
                    break
                j += 1
            out.append(blob[i: min(j + 1, n)])
            i = j + 1
            continue
        for word in ("undefined", "NaN", "Infinity", "-Infinity"):
            if blob.startswith(word, i):
                before = blob[i - 1] if i > 0 else ""
                after = blob[i + len(word)] if i + len(word) < n else ""
                if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                    out.append("null")
                    i += len(word)
                    break
        else:
            out.append(ch)
            i += 1
    blob = "".join(out)
    blob = _replace_ctor(blob, "Set")
    blob = _replace_ctor(blob, "Map")
    return blob


def extract_state(html: str, var: str = "__INITIAL_STATE__") -> Optional[Any]:
    """从 SSR HTML 提取 `window.<var>={...}` 的完整 JSON。

    括号配平扫描（字符串感知），不受 `</script>` 截断影响。解析失败返回 None
    （调用方按"无状态"处理，勿当空 dict 混淆）。
    """
    key = "window." + var + "="
    i = html.find(key)
    if i < 0:
        return None
    i += len(key)
    depth = 0
    in_str = False
    esc = False
    for p in range(i, len(html)):
        ch = html[p]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = html[i: p + 1]
                # 审查六轮（M2b）：残留 JS 字面量曾让 json.loads 裸抛——违反
                # "解析失败返回 None"契约，xhs.py 调用方未捕获会炸整轮采集
                try:
                    return json.loads(_repair_js_literals(blob))
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def project(state: Any, dotted: str, default: Any = None) -> Any:
    """点路径取值（"note.noteDetailMap.<id>.note"），任何一段缺失返回 default。"""
    cur = state
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur
