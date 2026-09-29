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
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# 常见翻页参数名（探测用）
PAGE_KEYS = {"page", "pageno", "pagenum", "currentpage", "current", "pageindex",
             "pageidx", "pageIndex", "pagenumber"}

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
    for k in ("data", "list", "rows", "items", "records", "result", "resultList",
              "datas", "content", "page"):
        if k in obj:
            v = obj[k]
            # 收官十二轮（审查，实测）：空数组曾返回 "" → one_config 误判
            # single_record=True → 引擎把整响应当一条记录（翻到底/被过滤的查询
            # 常捕获到空页）。空数组路径仍返回该键（空 ≠ 单对象响应）
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return k
            if isinstance(v, dict):
                sub = _guess_records_path(v, depth + 1)
                if sub:
                    return f"{k}.{sub}" if sub != "." else k
    # 兜底：任何含 dict 列表的键（含空数组）
    for k, v in obj.items():
        if isinstance(v, list) and (not v or isinstance(v[0], dict)):
            return k
    return ""


def _page_param_holders(params: Dict[str, Any], _prefix: str = "") -> Dict[str, Any]:
    """把翻页参数替换成 {{page}} 模板占位（递归——收官十二轮审查：嵌套体
    {"query":{"pageNum":1}} 曾原样保留数字页码，配置 strategy=none 静默只抓第 1 页）。"""
    out = {}
    for k, v in params.items():
        if isinstance(v, dict):
            _sub = _page_param_holders(v)
            if _sub != v:
                out[k] = _sub
            else:
                out[k] = v
        elif k.lower() in PAGE_KEYS and str(v).lstrip("-").rstrip("0").rstrip(".").isdigit() or \
                (k.lower() in PAGE_KEYS and isinstance(v, (int, float))):
            out[k] = "{{page}}"
        else:
            out[k] = v
    return out


def _dig(obj: Any, dotted: str) -> Any:
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _sample_records(json_obj: Any, records_path: str) -> list:
    """从捕获响应里取样本记录（链式检测用）。"""
    if not isinstance(json_obj, (dict, list)):
        return []
    v = _dig(json_obj, records_path) if records_path else None
    if isinstance(v, list):
        return [r for r in v if isinstance(r, dict)][:20]
    if isinstance(json_obj, list):
        return [r for r in json_obj if isinstance(r, dict)][:20]
    if isinstance(json_obj, dict):
        for k in ("records", "items", "list", "results", "data", "rows"):
            v = json_obj.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return [r for r in v if isinstance(r, dict)][:20]
    return []


def _detect_chains(samples: list, log=print) -> list:
    """api_chain 脚手架（归档功能请求）：检测"列表端点 ↔ 参数化详情端点"链。

    证据两级：①值匹配——详情请求的参数值（?id=12345）出现在列表样本里 →
    高置信；②键名匹配（detailId ↔ detail_id）→ 提示级。GET 形态的详情给出
    可直接试跑的 scaffold（pipeline.template 生成 url 字段 + detail.url_field）；
    POST 体详情暂只报告关系（引擎 detail 走 URL 取数，体参数需自定义 fetcher）。"""
    from urllib.parse import parse_qsl
    chains = []
    lists = [(cfg, rows) for cfg, rows in samples if rows]
    singles = [(cfg, rows) for cfg, rows in samples if not rows]
    for b_cfg, _rows_b in singles:
        b_src = b_cfg.get("source") or {}
        # 详情端参数候选：URL query 键值 + json_body 浅层标量键值
        params: Dict[str, Any] = {}
        try:
            params.update({k: v for k, v in parse_qsl(urlsplit(b_src.get("url", "")).query)})
        except Exception:
            pass
        jb = b_src.get("json_body")
        if isinstance(jb, dict):
            params.update({k: v for k, v in jb.items() if isinstance(v, (str, int, float))})
        if not params:
            continue
        for a_cfg, a_rows in lists:
            a_fields = set()
            for r in a_rows[:5]:
                a_fields.update(r.keys())
            best = None
            for k, v in params.items():
                if not k or k in ("page", "pageSize", "pageNo", "pageNum", "limit",
                                  "size", "currentPage", "pageIndex", "token", "_t"):
                    continue  # 翻页/令牌参数不构成链
                v_s = str(v)
                if not v_s or v_s in ("0", "1", "true", "false", "null", "undefined"):
                    continue
                for f in a_fields:
                    hit = None
                    for r in a_rows[:10]:
                        if f in r and v_s and str(r.get(f)) == v_s:
                            hit = ("value", f, v_s, k)
                            break
                    if hit is None:
                        _norm = lambda s: s.lower().replace("_", "").replace("-", "")
                        if _norm(k) == _norm(f):
                            hit = ("name", f, v_s, k)
                    if hit:
                        if best is None:
                            best = hit
                        else:
                            _nm = lambda s: str(s).lower().replace("_", "").replace("-", "")
                            _better = (hit[0] == "value" and best[0] != "value")
                            if hit[0] == best[0] and not _better:
                                # 同级命中（如同值多参数 ?a=12&id=12 都等于样本 id）：
                                # 参数名与字段名一致者优先——id 参数才是链变量
                                if _nm(hit[3]) == _nm(hit[1]) and _nm(best[3]) != _nm(best[1]):
                                    _better = True
                            if _better:
                                best = hit
            if best is None:
                continue
            # 收官十二轮（审查，实测）：hit 元组曾不带参数名——下方 re.sub 用的是
            # 循环泄漏的**最后一个**参数名 k。多参数详情 URL（?id=12&sig=1234）拿
            # "sig" 找 "sig=12" 永不命中 → 脚手架丢失；末位参数值恰等于命中值时
            # （?id=7&x=7）生成 ?id=7&x={id}——变量错挂，每行详情都是 id=7 的内容
            kind, field, val, pk = best
            b_url = b_src.get("url", "")
            post_only = bool(b_src.get("json_body") or b_src.get("body"))
            # 审查 M2：曾按 f"={val}" 裸替换——"?a=12&id=12" 会替换错参数、
            # "?id=1234&x=12" 会截断别的值。按参数名锚定（?k= 或 &k= 起头）
            _tmpl_url = ""
            if not post_only and val:
                _tmpl_url = re.sub(rf"([?&]{re.escape(pk)}=){re.escape(val)}(?=&|$)",
                                   rf"\g<1>{{{field}}}", b_url, count=1)
            if _tmpl_url and _tmpl_url != b_url:
                b_url = _tmpl_url
                scaffold = {
                    "name": f"{a_cfg.get('name','list')}_{b_cfg.get('name','detail')}_chain",
                    "source": a_cfg.get("source"),
                    "pagination": a_cfg.get("pagination"),
                    # pipeline.template 契约：str.format 单花括号占位（pagination 的
                    # {{page}} 是另一套引擎侧替换，勿混淆）
                    "pipeline": [{"type": "template", "field": "url", "tmpl": b_url}],
                    "detail": {"enabled": True, "url_field": "url"},
                    "_hint": "api_chain 脚手架：先 --limit 2 验证列表；"
                             "detail.extract 按详情响应补（契约见 SKILL.md「详情页」行）",
                }
            else:
                scaffold = None
            chains.append({
                "list": a_cfg.get("name"), "detail": b_cfg.get("name"),
                "confidence": kind, "param": "（见 evidence）", "field": field,
                "evidence": (f"详情参数值 {val!r} 命中列表样本字段 {field!r}" if kind == "value"
                             else f"参数键名对应 {field!r}（未验证值，置信较低）"),
                "scaffold": scaffold,
            })
            break  # 一个详情端只挂一个最像的列表
    if chains:
        n_ok = sum(1 for c in chains if c["scaffold"])
        log(f"🔗 检测到 {len(chains)} 条 列表→详情 链（{n_ok} 条已生成可试跑脚手架，"
            "POST 体详情需自定义 fetcher）")
    return chains


def one_config(item: Dict[str, Any], referer: str = "") -> Optional[Dict[str, Any]]:
    """单条捕获 → 一份 http_json source 配置草案；无意义响应返回 None。"""
    url = item.get("url", "")
    if not isinstance(url, str) or not url or NOISE_URL_PAT.search(url):
        return None
    if item.get("json") is None and not item.get("raw"):
        return None
    method = str(item.get("method") or "GET").upper()
    src: Dict[str, Any] = {"type": "http_json", "method": method, "url": url}
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    # POST 体：json 可解析 → json_body（dict 深拷贝并模板化翻页参数）；
    # 表单/其他 → body 字符串（翻页数字做文本级模板替换）
    pd = item.get("post_data") or ""
    if not isinstance(pd, str):        # 边界复现：post_data=123 曾 TypeError 毁掉整批
        pd = json.dumps(pd, ensure_ascii=False) if pd is not None else ""
    req_ct = str(item.get("request_content_type") or "")
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
                # JSON 解析失败（拼接/非常规转义/截断）：按文本做键级翻页模板
                # 审查修复：覆盖 JSON 风格（"page":1 / 'page':1）与 JS 字面量（page:3）
                b = pd
                for key in ("pageNo", "currentPage", "pageIndex", "pageNum", "page"):
                    b = re.sub(rf'([?&]{key}=)\d+', r"\g<1>{{page}}", b)
                    b = re.sub(rf"(^|&){key}=\d+", r"\g<1>" + key + "={{page}}", b)
                    b = re.sub(rf'(["\']{key}["\']\s*:\s*)\d+', r'\g<1>"{{page}}"', b)
                src["body"] = b
                headers["Content-Type"] = req_ct or "application/x-www-form-urlencoded"
        else:
            b = pd
            for key in ("pageNo", "currentPage", "pageIndex", "pageNum", "page"):
                b = re.sub(rf"([?&]{key}=)\d+", r"\g<1>{{page}}", b)
                b = re.sub(rf"(^|&){key}=\d+", r"\g<1>" + key + "={{page}}", b)
            src["body"] = b
            if req_ct:
                headers["Content-Type"] = req_ct
    # GET：查询串翻页参数模板化。审查修复：不走 parse_qsl 重建（会把 %E5%85%AC
    # 解码成裸中文再拼回，带关键词过滤的分页接口查询串被静默破坏）——
    # 直接在原始查询串上做正则替换。
    if method == "GET":
        for key in ("pageNo", "currentPage", "pageIndex", "pageNum", "page"):
            new_url = re.sub(rf"([?&]{key}=)\d+", r"\g<1>{{page}}", url)
            if new_url != url:
                src["url"] = new_url
                break
    src["headers"] = headers
    # batch2200：响应为单对象（GraphQL getQuote 类）→ 标记 single_record，
    # 引擎把整个响应体作为一条记录（records_path 的数组模型不适用）。
    # 判据：dict 响应里找不到"记录数组"路径（复用 _guess_records_path）。
    _j = item.get("json")
    if isinstance(_j, dict) and not _guess_records_path(_j):
        src["single_record"] = True
    # batch1800 战训（根本建议#2/#3）：认证类请求头随配置携带——重放接口常缺的就是它
    _AUTH_KEYS = ("cookie", "authorization", "x-requested-with", "x-csrf-token", "token")
    rh = item.get("request_headers") or {}
    carried = {k: v for k, v in rh.items()
               if k.lower() in _AUTH_KEYS and isinstance(v, str) and v}
    if carried:
        src["headers"].update(carried)
        src["_auth_hint"] = "已携带捕获时的认证头（Cookie/Token 会过期，失效重抓一次捕获或换 cookies 命令导出）"
    return src


def generate(capture_file: str | Path, referer: str = "",
             out: Optional[str] = None, log=print) -> Dict[str, Any]:
    fp = Path(capture_file).expanduser()
    try:
        data = json.loads(fp.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        return {"error": f"捕获文件解析失败（{type(e).__name__}: {str(e)[:80]}）: {fp}"}
    if isinstance(data, dict):
        data = data.get("items") or []
        if not data:
            log("⚠️ 捕获文件是 dict 但无 items 键——若是声明式捕获请直接传 <name>.json 的数组内容")
    if not isinstance(data, list):
        return {"error": f"捕获文件顶层应为 list，实际 {type(data).__name__}"}
    configs = []
    samples: list = []  # [(cfg, 样本记录)]——api_chain 链式检测原料
    seen = set()
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        try:
            src = one_config(item, referer=referer)
        except Exception as e:
            log(f"⚠️ 捕获记录[{idx}] 转换失败已跳过: {type(e).__name__}: {str(e)[:60]}")
            continue
        if not src:
            continue
        # OCR R131（M）：`|` 分隔符曾碰撞——URL/body 中合法出现 `|` 时两个不同
        # 请求生成同 key 被误去重。改用 length-prefix 编码消除歧义
        _m = src.get("method", "GET")
        _b = json.dumps(src.get("json_body") or src.get("body", ""), ensure_ascii=False, sort_keys=True)
        key = f"{len(src['url'])}:{src['url']}|{len(_m)}:{_m}|{len(_b)}:{_b}"
        if key in seen:
            continue
        seen.add(key)
        rp = _guess_records_path(item.get("json"))
        # batch2200 审查修复：仅当确实模板化了 {{page}} 才启用 template 翻页——
        # 无翻页参数的端点配 max_pages:5 会把同一页重复抓 5 遍（most_traded 7×5=35 全重）。
        paginated = "{{page}}" in json.dumps(src.get("json_body") or "") or \
            "{{page}}" in (src.get("body") or "") or "{{page}}" in src["url"]
        is_single = bool(src.get("single_record"))
        if is_single:
            pag = {"strategy": "none"}
        elif paginated:
            pag = {"strategy": "template", "max_pages": 5, "records_path": rp}
        else:
            pag = {"strategy": "none", "records_path": rp} if rp else {"strategy": "none"}
        cfg = {"name": urlsplit(src["url"]).path.rsplit("/", 1)[-1][:40] or "api",
               "source": src,
               "pagination": pag,
               "_hint": ("先 run --limit 2 小样：单对象响应加 source.single_record=true；"
                         "需要翻页时在 url/body 里把页码改为 {{page}} 并配 max_pages")}
        configs.append(cfg)
        samples.append((cfg, _sample_records(item.get("json"), rp)))
    # api_chain 链式检测（归档功能请求）：列表端点 ↔ 参数化详情端点
    chains = _detect_chains(samples, log=log) if len(samples) > 1 else []
    result = {"capture_file": str(fp), "count": len(configs), "configs": configs}
    if chains:
        result["chains"] = chains
    log(f"⚡ 生成 {len(configs)} 份 http_json 配置草案（含方法/请求体/翻页模板，先小样再全量）")
    if out:
        p = Path(out).expanduser()
        if p.is_dir() or not p.suffix:  # 目录（或无后缀路径）→ 目录模式
            p = p / "gen_configs.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        # R37 修复：单配置时直接写配置对象本体——此前写 {capture_file,configs}
        # 信封，run --config/validate 加载报"缺少必填字段 name"，命令自己提示
        # 的"先 --limit 2 小样验证"根本走不通。
        # 审查二轮（M）：单配置裸写曾把 chains 一并丢掉——有链式检测结果时
        # 仍走信封（要单配置时用户自取 configs[0]）
        _write_envelope = len(configs) != 1 or bool(result.get("chains"))
        p.write_text(json.dumps(configs[0] if not _write_envelope else result,
                                ensure_ascii=False, indent=1), encoding="utf-8")
        result["saved"] = str(p)
    return result
