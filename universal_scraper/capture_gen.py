#!/usr/bin/env python3
"""⚡ capture → 可重放 http_json 配置生成器（batch1600 战训 P0：打通最后一公里）。

输入：浏览器捕获文件（capture_all.json 或声明式 <name>.json，
桥侧格式 [{url, method, post_data, request_content_type, json/raw}]）。
输出：每个"有数据的接口"一份 http_json source 配置草案——
POST 体/方法/Content-Type 全部就位，自动探测 {{page}} 翻页参数与 records_path，
先小样验证再全量的纪律不变。

用法:
  python3 -m universal_scraper.cli capture2config <capture_all.json> \
      [--referer https://原页面] [--out gen_configs.json]
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, parse_qsl

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# 常见翻页参数名（探测用）
PAGE_KEYS = {"page", "pageno", "pagenum", "currentpage", "current", "pageindex",
             "pageidx", "pageIndex", "pagenumber"}
SIZE_KEYS = {"pagesize", "pageSize", "size", "limit", "perpage", "rows"}

# 静态资源/噪声响应过滤（生成配置无意义）
NOISE_URL_PAT = re.compile(r"\.(js|css|png|jpe?g|gif|svg|woff2?|ttf|ico|map)(\?|$)", re.I)


def _guess_records_path(obj: Any, depth: int = 0) -> str:
    """在 JSON 里找"最像数据列表"的路径（点路径）。"""
    if depth > 4:
        return ""
    if isinstance(obj, list):
        return "." if depth == 0 else ""
    if not isinstance(obj, dict):
        return ""
    best = ""
    for k in ("data", "list", "rows", "items", "records", "result", "resultList",
              "datas", "content", "page", "records"):
        if k in obj:
            v = obj[k]
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return k
            if isinstance(v, dict):
                sub = _guess_records_path(v, depth + 1)
                if sub:
                    return f"{k}.{sub}" if sub != "." else k
    # 兜底：任何含 dict 列表的键
    for k, v in obj.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return k
    return ""


def _page_param_holders(params: Dict[str, Any]) -> Dict[str, Any]:
    """把翻页参数替换成 {{page}} 模板占位。"""
    out = {}
    for k, v in params.items():
        if k.lower() in PAGE_KEYS and str(v).lstrip("-").isdigit():
            out[k] = "{{page}}"
        else:
            out[k] = v
    return out


def one_config(item: Dict[str, Any], referer: str = "") -> Optional[Dict[str, Any]]:
    """单条捕获 → 一份 http_json source 配置草案；无意义响应返回 None。"""
    url = item.get("url", "")
    if not url or NOISE_URL_PAT.search(url):
        return None
    if item.get("json") is None and not item.get("raw"):
        return None
    method = (item.get("method") or "GET").upper()
    src: Dict[str, Any] = {"type": "http_json", "method": method, "url": url}
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    # POST 体：json 可解析 → json_body（dict 深拷贝并模板化翻页参数）；
    # 表单/其他 → body 字符串（翻页数字做文本级模板替换）
    pd = item.get("post_data") or ""
    req_ct = item.get("request_content_type") or ""
    if method != "GET" and pd:
        if "json" in req_ct.lower():
            try:
                body_obj = json.loads(pd)
                if isinstance(body_obj, dict):
                    src["json_body"] = _page_param_holders(body_obj)
                else:
                    src["json_body"] = body_obj
                headers["Content-Type"] = "application/json"
            except Exception:
                src["body"] = re.sub(r'(["&?])(page|pageNo|currentPage|pageIndex|pageNum)("?)=("\\?|)?\d+',
                                     lambda m: m.group(0), pd)
                headers["Content-Type"] = req_ct or "application/x-www-form-urlencoded"
        else:
            b = pd
            for key in ("pageNo", "currentPage", "pageIndex", "pageNum", "page"):
                b = re.sub(rf"([?&]{key}=)\d+", r"\g<1>{{page}}", b)
                b = re.sub(rf"(^|&){key}=\d+", r"\g<1>" + key + "={{page}}", b)
            src["body"] = b
            if req_ct:
                headers["Content-Type"] = req_ct
    # GET：url 查询串翻页参数模板化
    if method == "GET":
        sp = urlsplit(url)
        q = parse_qsl(sp.query, keep_blank_values=True)
        qd = _page_param_holders({k: v for k, v in q})
        if qd != dict(q):
            new_q = "&".join(f"{k}={v}" for k, v in qd.items())
            src["url"] = sp._replace(query=new_q).geturl()
    src["headers"] = headers
    return src


def generate(capture_file: str | Path, referer: str = "",
             out: Optional[str] = None, log=print) -> Dict[str, Any]:
    fp = Path(capture_file).expanduser()
    data = json.loads(fp.read_text(encoding="utf-8"))
    if isinstance(data, dict):  # 声明式 <name>.json 兼容
        data = data.get("items") or []
    configs = []
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        src = one_config(item, referer=referer)
        if not src:
            continue
        key = src["url"] + "|" + src.get("method", "GET") + "|" + json.dumps(
            src.get("json_body") or src.get("body", ""), ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        rp = _guess_records_path(item.get("json"))
        cfg = {"name": urlsplit(src["url"]).path.rsplit("/", 1)[-1][:40] or "api",
               "source": src,
               "pagination": {"strategy": "template", "max_pages": 5, "records_path": rp},
               "_hint": ("先 run --limit 2 小样：records_path 为空则从响应顶层键里找列表；"
                         "翻页参数未模板化时改 url/body 里的页码为 {{page}}")}
        configs.append(cfg)
    result = {"capture_file": str(fp), "count": len(configs), "configs": configs}
    log(f"⚡ 生成 {len(configs)} 份 http_json 配置草案（含方法/请求体/翻页模板，先小样再全量）")
    if out:
        p = Path(out).expanduser()
        if p.is_dir() or not p.suffix:  # 目录（或无后缀路径）→ 目录模式
            p = p / "gen_configs.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        result["saved"] = str(p)
    return result
