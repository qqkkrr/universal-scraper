#!/usr/bin/env python3
"""curl → 任务配置（审查二十轮 R20，第三梯队采纳项）。

场景：用户/论坛里最常给的"可复现证据"是浏览器复制出来的 curl 命令——
直接吃进 curl 省掉"手抄 URL/头/请求体"的错漏（本插件绝大多数的 Header/
Referer/签名参数配错都源于手抄）。

设计纪律：
- **只解析、不执行**：命令字符串绝不进 shell（shlex 分词 + 白名单键）；
- **说实话**：不支持的能力（multipart -F、上传 -T、代理链等）进 warnings，
  不静默丢弃；凭据类（-u/Authorization/Cookie）单独提醒"配置里含凭据勿外发"；
- 产物是"起点草案"：配合 `run --config --dry-run` 与 `--limit 2` 小样验证。
"""
from __future__ import annotations

import base64
import json
import shlex
from typing import Any, Dict, List, Tuple

# 带值的长/短选项（--opt value / --opt=value 两形态）
_VALUE_FLAGS = {
    "-X": "method", "--request": "method",
    "-H": "header", "--header": "header",
    "-b": "cookie", "--cookie": "cookie",
    "-d": "data", "--data": "data", "--data-raw": "data", "--data-binary": "data",
    "--data-urlencode": "data_urlencode",
    "-u": "user", "--user": "user",
    "-e": "referer", "--referer": "referer",
    "-A": "user_agent", "--user-agent": "user_agent",
}
# 无值开关
_BOOL_FLAGS = {"-k": "insecure", "--insecure": "insecure",
               "-L": "follow", "--location": "follow",
               "--compressed": "compressed", "-s": "silent", "--silent": "silent",
               "-i": "show_headers", "--include": "show_headers"}
# 明确不支持（进 warnings，不静默）
_UNSUPPORTED = {"-F": "multipart 表单（-F/--form）", "--form": "multipart 表单（-F/--form）",
                "-T": "文件上传（-T/--upload-file）", "--upload-file": "文件上传（-T/--upload-file）",
                "--proxy": "curl 代理参数（请改用 anti_bot.proxy）",
                "-x": "curl 代理参数（请改用 anti_bot.proxy）",
                "--config": "curl 配置文件（-K）", "-K": "curl 配置文件（-K）",
                "-o": "输出到文件（-o；本工具只生成配置）", "--output": "输出到文件（-o）"}
_CRED_HINT = "配置含凭据（Cookie/Basic Auth）——切勿提交到公开仓库或外发给他人"


def parse_curl(cmd: str) -> Tuple[Dict[str, Any], List[str]]:
    """把 curl 命令解析成 {method, url, headers, cookies, data, ...}。

    返回 (parsed, warnings)。解析失败抛 ValueError（调用方给明确报错）。"""
    warnings: List[str] = []
    if not cmd or not cmd.strip():
        raise ValueError("curl 命令为空")
    text = cmd.strip()
    # 允许粘贴多行（浏览器复制常带反斜杠续行）
    text = text.replace("\\\n", " ").replace("\n", " ")
    try:
        toks = shlex.split(text)
    except ValueError as e:
        raise ValueError(f"命令分词失败（引号未闭合？）: {e}")
    if toks and toks[0] in ("curl", "$curl", "curl.exe"):
        toks = toks[1:]
    parsed: Dict[str, Any] = {"method": "", "url": "", "headers": {},
                              "cookies": "", "data": []}
    i = 0
    while i < len(toks):
        t = toks[i]
        key, val = t, None
        if t.startswith("--") and "=" in t:
            key, val = t.split("=", 1)
        if key in _UNSUPPORTED:
            warnings.append(f"不支持 {_UNSUPPORTED[key]}——已在配置中省略，请手工补齐")
            i += 1 if val is not None else 2
            continue
        if key in _BOOL_FLAGS:
            parsed[_BOOL_FLAGS[key]] = True
            i += 1
            continue
        if key in _VALUE_FLAGS:
            if val is None:
                if i + 1 >= len(toks):
                    raise ValueError(f"{key} 缺少取值")
                val = toks[i + 1]
                i += 2
            else:
                i += 1
            kind = _VALUE_FLAGS[key]
            if kind == "header":
                if ":" in val:
                    k, v = val.split(":", 1)
                    parsed["headers"][k.strip()] = v.strip()
                else:
                    warnings.append(f"丢弃非法头（无冒号）: {val[:40]!r}")
            elif kind == "cookie":
                parsed["cookies"] = (parsed["cookies"] + "; " + val).strip("; ")
            elif kind in ("data", "data_urlencode"):
                parsed["data"].append(val)
            else:
                parsed[kind] = val
            continue
        if t.startswith("-"):
            warnings.append(f"忽略未知选项 {t}")
            i += 1
            continue
        if not parsed["url"]:
            parsed["url"] = t
        else:
            warnings.append(f"忽略多余位置参数 {t[:40]!r}")
        i += 1
    if not parsed["url"]:
        raise ValueError("命令里找不到 URL")
    if not parsed["url"].startswith(("http://", "https://")):
        raise ValueError(f"URL 形态不对（需 http/https）: {parsed['url'][:60]}")
    return parsed, warnings


def build_config(cmd: str, name: str = "from_curl", prefer_html: bool = False) -> Dict[str, Any]:
    """curl 命令 → v2 任务配置草案（source/anti_bot/pagination + _hint 提示）。

    返回 {"config": {...}, "warnings": [...]}；warnings 含凭据提醒与不支持项。"""
    parsed, warnings = parse_curl(cmd)
    headers: Dict[str, str] = dict(parsed["headers"])
    if parsed.get("user_agent") and not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = parsed["user_agent"]
    if parsed.get("referer") and not any(k.lower() == "referer" for k in headers):
        headers["Referer"] = parsed["referer"]
    if parsed.get("cookies"):
        headers["Cookie"] = parsed["cookies"]
    if parsed.get("user"):
        # curl -u user:pass → Basic Auth 头（配置里含凭据，必须提醒）
        _tok = base64.b64encode(parsed["user"].encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {_tok}"
        warnings.append(_CRED_HINT)
    if parsed.get("cookies"):
        warnings.append(_CRED_HINT)

    method = (parsed.get("method") or ("POST" if parsed.get("data") else "GET")).upper()
    body_raw = "&".join(parsed.get("data") or [])
    source: Dict[str, Any] = {"type": "http_html" if prefer_html else "http_json",
                              "url": parsed["url"], "method": method}
    if headers:
        source["headers"] = headers
    if body_raw:
        _is_json = False
        try:
            obj = json.loads(body_raw)
            _is_json = isinstance(obj, (dict, list))
        except Exception:
            _is_json = False
        if _is_json:
            source["json_body"] = obj
        else:
            source["body"] = body_raw
    if parsed.get("follow"):
        pass  # 默认跟随重定向；显式 -L 与默认一致，无需配置
    if parsed.get("compressed"):
        pass  # 客户端默认处理 gzip/deflate

    anti: Dict[str, Any] = {"min_interval": 0.5, "max_retries": 2}
    if parsed.get("insecure"):
        anti["verify"] = False
        warnings.append("-k/--insecure：已设置 anti_bot.verify=false（仅对证书损坏的自建站使用）")

    cfg: Dict[str, Any] = {
        "name": name,
        "source": source,
        "pagination": {"strategy": "none"},
        "anti_bot": anti,
        "storage": {"type": "jsonl"},
        "output": {"dir": "outputs", "base_name": name},
    }
    if prefer_html:
        cfg["source"]["row_css"] = ""      # 需人工/AI 填：列表行选择器
        cfg["source"]["fields"] = {}
        warnings.append("http_html 模板：row_css/fields 需补全（可跑 auto 或 jsrecon 辅助定位）")
    else:
        cfg["pagination"]["records_path"] = ""
        warnings.append("http_json 模板：records_path 需指向记录数组（如 data.list）；"
                        "单对象响应改 source.single_record=true")
    hint = ["curl 草案：先 `run --config <file> --dry-run` 校验，再 `--limit 2` 小样验证字段",
            "页面带签名/时间戳参数时，curl 里的固定值可能很快失效——按报错调整"]
    cfg["_hint"] = hint
    return {"config": cfg, "warnings": warnings}
