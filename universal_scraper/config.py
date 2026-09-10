#!/usr/bin/env python3
"""任务配置：加载 + 校验（报错带路径，友好提示）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

SOURCE_TYPES = {"http_json", "http_html", "browser_script", "browser"}
# next_url 曾收录但从未实现（HTML 下一页请用 next_selector）——batch2400 战训移除，
# 避免 validate 放行一个会"同页重复抓"的死策略
PAGINATION_STRATEGIES = {"page_param", "offset", "none", "template"}
EXTRACT_TYPES = {"json", "css_text", "css_attr", "css_html", "xpath_text", "xpath_attr", "regex", "regex_all", "constant"}
CAPTCHA_STRATEGIES = {"auto", "ddddocr", "opencv_slider", "2captcha", "nopecha", "human", "config", "none", "external"}

# ---------------------------------------------------------------- 共享常量（v2/v3 一套，杜绝 API 分裂）
# 词表唯一来源：contract.PIPELINE_STEPS（validate/执行器/文档测试全部从它派生，禁止手抄）
from .contract import PIPELINE_STEPS, V3_ONLY_STEPS  # noqa: E402
ALL_PIPELINE_TYPES = set(PIPELINE_STEPS)
ALL_ACTION_TYPES = {"click", "type", "write", "fill", "press", "select", "wait",
                    "wait_time", "wait_for_selector", "waitfor", "scroll",
                    "exec", "js", "execute_javascript", "screenshot", "noop"}
ALL_PARSER_TYPES = {"html", "json", "llm", "article", "table", "json_paged"}
ALL_STORAGE_TYPES = {"jsonl", "csv", "sqlite", "multi"}
# 兼容别名（旧代码引用）
PIPELINE_TYPES = ALL_PIPELINE_TYPES
V3_ACTION_TYPES = ALL_ACTION_TYPES
V3_SOURCE_TYPES = {"http", "browser", "bridge", "scrapling"}


class ConfigError(ValueError):
    def __init__(self, path: str, msg: str, hint: str = ""):
        self.path = path
        self.hint = hint
        super().__init__(f"配置错误 [{path}]: {msg}" + (f"\n  提示: {hint}" if hint else ""))


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ConfigError("(文件)", f"配置文件不存在: {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError("(JSON)", f"JSON 解析失败: {e}")
    if not isinstance(cfg, dict):
        raise ConfigError("(根)", "配置必须是 JSON 对象")
    return validate(cfg)


def _require(cfg: Dict[str, Any], key: str, path: str, types, hint: str = "") -> Any:
    if key not in cfg:
        # 版权中心战例：新手第一次写错结构时，顺手给出 scaffold 出路
        raise ConfigError(path, f"缺少必填字段 '{key}'",
                          (hint + "；可先跑 `scaffold --type http_html` 生成模板再改").strip())
    v = cfg[key]
    if types and not isinstance(v, types):
        raise ConfigError(f"{path}.{key}", f"类型应为 {types.__name__}，实际 {type(v).__name__}",
                          hint or f"示例: {_example(key)}")
    return v


def _example(key: str) -> str:
    return {
        "name": '"my_task"',
        "source": '{"type": "http_json", "url": "https://..."}',
        "url": '"https://api.example.com/list"',
        "pagination": '{"strategy": "page_param", "page_param": "page"}',
    }.get(key, "")


def validate(cfg: Dict[str, Any]) -> Dict[str, Any]:
    _require(cfg, "name", "(根)", str, "任务名，例如 \"my_task\"")
    src = _require(cfg, "source", "(根)", dict, 'source: {"type": "...", "url": "..."}')
    stype = _require(src, "type", "source", str)
    if stype not in SOURCE_TYPES:
        raise ConfigError("source.type", f"未知取数类型 '{stype}'",
                          f"可选: {', '.join(sorted(SOURCE_TYPES))}")
    if stype in ("http_json", "http_html", "browser") and "url" not in src:
        raise ConfigError("source.url", "该 source.type 需要 url", "例如: \"https://api.example.com/list\"")
    # 类型守卫（审查修复：字段存在但类型错曾甩 AttributeError 裸栈）
    for fname, ftype, example in (("pagination", dict, '{"strategy": "none"}'),
                                  ("anti_bot", dict, '{"min_interval": 1.0}'),
                                  ("detail", dict, '{"url_field": "url"}'),
                                  ("record", dict, '{"fields": {...}}')):
        v = cfg.get(fname)
        if v is not None and not isinstance(v, dict):
            raise ConfigError(fname, f"{fname} 应为 dict，实际 {type(v).__name__}", example)
    if stype == "browser_script" and not src.get("bridge"):
        raise ConfigError("source.bridge", "browser_script 需要 bridge 脚本路径",
                          '例如: "../scripts/ggzy_bridge.cjs"')
    if stype == "http_json" and not (cfg.get("pagination", {}) or {}).get("records_path"):
        # 战例：B站接口 200 + 38 条数据，因缺 records_path 静默 page+0。
        # batch2200 审查放宽：runtime 已支持空 records_path（自动识别常见键/根数组），
        # 且单对象响应（GraphQL getQuote 类）应配 single_record=true 整响应一条记录。
        # 保留告警：既无 records_path 又无 single_record 的"多页翻页"配置仍会静默 0 条。
        pag0 = cfg.get("pagination", {}) or {}
        if not cfg.get("source", {}).get("single_record") and str(
                pag0.get("strategy", "none")) != "none":
            raise ConfigError("pagination.records_path",
                              "http_json 翻页抓取需要 records_path 指向记录数组"
                              "（单对象响应请加 source.single_record=true）",
                              '例如: {"strategy": "template", "records_path": "data.list"} 或 '
                              '{"strategy": "none", "single_record": true}')

    # batch2400 美团战训：template 翻页仅支持 http_json（json_body/url 的 {{page}} 替换）。
    # http_html 的翻页走 next_selector / page_param，与 template 互斥；
    # 配了 template 的 http_html 会在 validate 通过后"只抓第 1 页就静默结束"。
    _pag_strat = (cfg.get("pagination", {}) or {}).get("strategy", "none")
    if _pag_strat == "template" and stype == "http_html":
        raise ConfigError(
            "pagination.strategy",
            f"翻页策略 'template' 仅支持 http_json，不支持 http_html",
            "http_html 翻页请用 browser + pagination.type=click，或 page_param + page_param_name")

    pag = cfg.get("pagination", {})
    if pag is not None and not isinstance(pag, dict):
        raise ConfigError("pagination", f"pagination 应为 dict，实际 {type(pag).__name__}",
                          '例如: "pagination": {"strategy": "none"}')
    if pag:
        strat = pag.get("strategy", "none")
        if strat not in PAGINATION_STRATEGIES:
            raise ConfigError("pagination.strategy", f"未知分页策略 '{strat}'",
                              f"可选: {', '.join(sorted(PAGINATION_STRATEGIES))}")
        if strat == "page_param" and not pag.get("page_param"):
            raise ConfigError("pagination.page_param", "page_param 策略需要 page_param 字段",
                              '例如: {"strategy": "page_param", "page_param": "page"}')
        # records_path 只对 http_json 强制（html 行来自 row_css，与策略无关）——见下方按类型校验

    pipeline = cfg.get("pipeline", [])
    if isinstance(pipeline, str):
        raise ConfigError("pipeline", "pipeline 应为步骤数组",
                          '例如: "pipeline": [{"type": "dedup", "key": "url"}]')
    for i, step in enumerate(pipeline if isinstance(pipeline, list) else []):
        if not isinstance(step, dict):
            raise ConfigError(f"pipeline[{i}]", f"流水线步骤应为 dict，实际 {type(step).__name__}")
        pt = step.get("type")
        if pt not in PIPELINE_TYPES:
            raise ConfigError(f"pipeline[{i}].type", f"未知流水线类型 '{pt}'",
                              f"可选: {', '.join(sorted(PIPELINE_TYPES))}")

    detail = cfg.get("detail", {})
    if detail is not None and not isinstance(detail, dict):
        raise ConfigError("detail", f"detail 应为 dict，实际 {type(detail).__name__}")
    if isinstance(detail, dict):
        for i, spec in enumerate(detail.get("extract", []) or []):
            if not isinstance(spec, dict):
                raise ConfigError(f"detail.extract[{i}]", "extract 步骤应为 dict")
            et = spec.get("type")
            if et not in EXTRACT_TYPES:
                raise ConfigError(f"detail.extract[{i}].type", f"未知提取类型 '{et}'",
                                  f"可选: {', '.join(sorted(EXTRACT_TYPES))}")

    rec_fields = (cfg.get("record", {}) or {}).get("fields")
    if rec_fields is not None and not isinstance(rec_fields, dict):
        # 盒马战例：record.fields 写成 list 时 validate 放行、run 崩 AttributeError
        raise ConfigError("record.fields",
                          f"record.fields 应为 dict {{列名: {{from: 字段}}}}，当前为 {type(rec_fields).__name__}",
                          '例如: {"标题": {"from": "title"}}')

    ab = cfg.get("anti_bot", {})
    if ab is not None and not isinstance(ab, dict):
        raise ConfigError("anti_bot", "anti_bot 应为 dict",
                          '例如: "anti_bot": {"min_interval": 1.0}')
    ab = ab if isinstance(ab, dict) else {}
    cap = ab.get("captcha", {})
    if cap is not None and not isinstance(cap, dict):
        raise ConfigError("anti_bot.captcha", f"captcha 应为 dict，实际 {type(cap).__name__}")
    cap = cap if isinstance(cap, dict) else {}
    cs = cap.get("strategy", "auto")
    if cs not in CAPTCHA_STRATEGIES:
        raise ConfigError("anti_bot.captcha.strategy", f"未知验证码策略 '{cs}'",
                          f"可选: {', '.join(sorted(CAPTCHA_STRATEGIES))}")
    return cfg


def collect_warnings(cfg: Dict[str, Any]) -> "List[str]":
    """跨字段语义检查：不阻断运行，但必须在 validate 时可见（战例驱动，勿删）。"""
    warns: List[str] = []
    src = cfg.get("source", {}) or {}
    stype = src.get("type", "")
    rec = cfg.get("record", {}) or {}
    # 微博战例：source.fields 提取了、record.fields 空映射 → 输出被丢弃
    # （v1.8 起运行时会自动按 source 字段映射；此提示改为确认口径/改名指引）
    if stype in ("http_html", "browser") and (src.get("fields") or src.get("row_css")) \
            and not rec.get("fields"):
        warns.append("record.fields 未声明——输出列将自动等于提取字段名。"
                     "需改名/筛选请在 record.fields 声明：{\"列名\": {\"from\": \"字段名\"}}")
    if stype == "browser" and not src.get("cdp") and src.get("headless") is not False:
        warns.append("browser 为 headless 且未配 cdp——遇 Cloudflare/Turnstile 会被拦，"
                     "见配方 R16（调试 Chrome 过一次校验后附加）。")
    for i, step in enumerate(cfg.get("pipeline", [])):
        pt = step.get("type")
        if pt in V3_ONLY_STEPS:
            warns.append(f"pipeline[{i}] 类型 '{pt}' 仅 v3 任务包执行器实现，"
                         "run --config 会跳过（v2 可用: transform/template 等，见 contract.PIPELINE_STEPS）。")
    # 猎聘战例：capture 声明缺 records_path 会把整个响应体当一条记录（导出全空）
    if stype == "browser" and src.get("record_from") == "capture" and isinstance(src.get("capture"), list):
        for j, cap in enumerate(src["capture"]):
            if isinstance(cap, dict) and not cap.get("records_path"):
                warns.append(f"source.capture[{j}] 缺 records_path——会把整个响应体当一条记录"
                             "（导出全空）。请声明记录数组所在路径，如 \"data.list\"")
    return warns


def validate_task(cfg: Dict[str, Any], has_custom_fetcher: bool = False,
                 has_custom_storage: bool = False,
                 has_custom_parser: bool = False) -> Dict[str, Any]:
    """v3 任务包配置校验：source + start_urls + rules + parsers + storage。
    - 任务包自带 modules/fetcher.py 时允许任意 source.type（插件协议）
    - 仅 http/browser 需要 start_urls（桥/自定义 fetch_all 可省略）"""
    _require(cfg, "name", "(根)", str, "任务名")
    src = cfg.get("source", {}) or {}
    stype = src.get("type", "http")
    if stype not in V3_SOURCE_TYPES and not has_custom_fetcher:
        raise ConfigError("source.type", f"未知取数类型 '{stype}'",
                          f"可选: {', '.join(sorted(V3_SOURCE_TYPES))} 或提供 modules/fetcher.py 自定义")
    if src.get("sitemap") is not None and not isinstance(src.get("sitemap"), str):
        raise ConfigError("source.sitemap", "sitemap 应为 URL 字符串", '例如: "https://site.com/sitemap.xml"')
    for flag in ("stealth", "remove_overlays", "pool"):
        if flag in src and not isinstance(src[flag], bool):
            raise ConfigError(f"source.{flag}", f"{flag} 应为布尔值", "true/false")
    for i, a in enumerate(src.get("actions") or []):
        if not isinstance(a, dict) or not a.get("type"):
            raise ConfigError(f"source.actions[{i}]", "每个 action 需是含 type 的对象",
                              '{"type": "click", "selector": "#more"}')
        if a["type"] not in V3_ACTION_TYPES:
            raise ConfigError(f"source.actions[{i}].type", f"未知动作类型 '{a['type']}'",
                              f"可选: {', '.join(sorted(V3_ACTION_TYPES))}")
        if a["type"] in ("click", "type", "write", "fill", "wait_for_selector", "waitfor", "select") and not a.get("selector"):
            raise ConfigError(f"source.actions[{i}]", f"动作 {a['type']} 需要 selector")
    inc = cfg.get("incremental", {}) or {}
    if inc.get("enabled") and not inc.get("key"):
        raise ConfigError("incremental.key", "增量去重需要 key（去重主键字段）", '例如: {"enabled": true, "key": "id"}')
    is_bridge = stype == "bridge"
    needs_seeds = stype in ("http", "browser")
    if needs_seeds and not cfg.get("start_urls") and not src.get("sitemap"):
        raise ConfigError("start_urls", "任务包需要 start_urls（入口 URL 列表）",
                          '例如: ["https://site.com/list"]（桥/自定义 fetch_all 或 source.sitemap 可省略）')
    rules = cfg.get("rules", [])
    if not is_bridge and not has_custom_fetcher and not rules:
        raise ConfigError("rules", "任务包需要 rules（URL→解析器路由）",
                          '例如: [{"match": "regex", "pattern": "/detail", "parser": "detail"}]'
                          '（自定义 fetcher 可省略）')
    for i, r in enumerate(rules):
        if r.get("match") not in ("regex", "contains", "startswith"):
            raise ConfigError(f"rules[{i}].match", f"未知匹配方式 '{r.get('match')}'",
                              "可选: regex / contains / startswith")
        if not r.get("pattern") or not r.get("parser"):
            raise ConfigError(f"rules[{i}]", "规则需要 pattern 和 parser")
    for pname in {r.get("parser") for r in rules}:
        if pname not in cfg.get("parsers", {}):
            raise ConfigError(f"parsers.{pname}", f"规则引用了未定义的 parser '{pname}'",
                              "在 parsers 里声明，或写 modules/parser.py 提供同名 Parser")
    # pipelines 校验（与 modules/pipelines.py 实现对齐）
    for i, step in enumerate(cfg.get("pipelines", []) or []):
        pt = (step or {}).get("type")
        if pt not in ALL_PIPELINE_TYPES:
            raise ConfigError(f"pipelines[{i}].type", f"未知流水线类型 '{pt}'",
                              f"可选: {', '.join(sorted(ALL_PIPELINE_TYPES))}")
        if pt == "download" and not step.get("field"):
            raise ConfigError(f"pipelines[{i}]", "download 流水线需要 field（下载 URL 字段）")
    # parsers 类型校验（任务自带 modules/parser.py 时跳过）
    if not has_custom_parser:
        for pname, pcfg in (cfg.get("parsers", {}) or {}).items():
            pt = (pcfg or {}).get("type")
            if pt and pt not in ALL_PARSER_TYPES:
                raise ConfigError(f"parsers.{pname}.type", f"未知解析器类型 '{pt}'",
                                  f"可选: {', '.join(sorted(ALL_PARSER_TYPES))}")
            if pt == "llm" and not (pcfg or {}).get("schema"):
                raise ConfigError(f"parsers.{pname}.schema", "llm 解析器需要 schema（字段定义）",
                                  '例如: {"type": "llm", "schema": {"标题": "...", "价格": "..."}}')
    st = cfg.get("storage", {})
    if st.get("type", "jsonl") not in ALL_STORAGE_TYPES and not has_custom_storage:
        raise ConfigError("storage.type", f"未知存储类型 '{st.get('type')}'",
                          "可选: " + " / ".join(sorted(ALL_STORAGE_TYPES)) + " 或提供 modules/storage.py 自定义")
    if st.get("type") == "multi" and not st.get("backends"):
        raise ConfigError("storage.backends", "multi 存储需要 backends 数组",
                          '[{"type": "jsonl"}, {"type": "sqlite"}]')
    return cfg
