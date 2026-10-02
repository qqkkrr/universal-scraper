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
ALL_ACTION_TYPES = {"click", "type", "write", "fill", "type_real", "fill_real",
                    "press", "select", "wait",
                    "wait_time", "wait_for_selector", "waitfor", "wait_for_url",
                    "wait_for_navigation", "scroll",
                    "exec", "js", "execute_javascript", "screenshot", "noop"}
# 商标网战训（2026-09）：各动作的合法键名——validate 时校验，配置错误在跑之前
# 就拦住（曾发生：fill 写 value 键静默填空、screenshot 漏 path 落到 /tmp 默认值）。
# R1 复查修复：wait_ms/timeout_ms 是 spec-schema 文档与 browser_generic 桥实际
# 支持的键——曾漏收，照文档抄的配置被 validate 拒绝。
ACTION_KEY_SPEC = {
    # 审查十三轮（H5）：click 补 xpath——桥支持 xpath 定位，auto 兼容链也专门
    # 保留它（370-379），校验却报"需要 selector"并删未知键，两头互相矛盾
    "click":             {"selector", "xpath", "index", "timeout", "timeout_ms", "ms", "wait_ms", "optional"},
    "type":              {"selector", "text", "value", "index", "timeout", "timeout_ms", "wait_ms", "optional"},
    "write":             {"selector", "text", "value", "index", "timeout", "timeout_ms", "wait_ms", "optional"},
    "fill":              {"selector", "text", "value", "index", "timeout", "timeout_ms", "wait_ms", "optional"},
    "type_real":         {"selector", "text", "value", "index", "timeout", "timeout_ms", "delay_ms", "ms", "wait_ms", "optional"},
    "fill_real":         {"selector", "text", "value", "index", "timeout", "timeout_ms", "delay_ms", "ms", "wait_ms", "optional"},
    "press":             {"key", "ms", "wait_ms", "optional"},
    "select":            {"selector", "value", "label", "index", "timeout", "timeout_ms", "wait_ms", "optional"},
    "wait":              {"ms", "milliseconds", "wait_ms", "selector", "timeout", "timeout_ms", "optional"},
    "wait_time":         {"ms", "milliseconds", "wait_ms", "selector", "timeout", "timeout_ms", "optional"},
    "wait_for_selector": {"selector", "timeout", "timeout_ms", "wait_ms", "optional"},
    "waitfor":           {"selector", "timeout", "timeout_ms", "wait_ms", "optional"},
    "wait_for_url":      {"url_pattern", "urlPattern", "url", "timeout", "timeout_ms", "wait_ms", "optional"},
    "wait_for_navigation": {"url_pattern", "urlPattern", "url", "timeout", "timeout_ms", "wait_ms", "optional"},
    "scroll":            {"direction", "amount", "ms", "wait_ms", "optional"},
    "exec":              {"js", "code", "ms", "wait_ms", "optional"},
    "js":                {"js", "code", "ms", "wait_ms", "optional"},
    "execute_javascript": {"js", "code", "ms", "wait_ms", "optional"},
    "screenshot":        {"path", "fullPage", "wait_ms", "optional"},
    "noop":              set(),
}
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
        # OCR R131（M）：types 为元组时 types.__name__ 必炸 AttributeError——
        # 用类型名拼接兼容单类型与元组
        _want = "/".join(t.__name__ for t in (types if isinstance(types, tuple) else (types,)))
        raise ConfigError(f"{path}.{key}", f"类型应为 {_want}，实际 {type(v).__name__}",
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
                              '在 source 内加 "single_record": true（单对象响应，strategy 用 none）')

    # batch2400 美团战训：template 翻页仅支持 http_json（json_body/url 的 {{page}} 替换）。
    # http_html 的翻页走 next_selector / page_param，与 template 互斥；
    # 配了 template 的 http_html 会在 validate 通过后"只抓第 1 页就静默结束"。
    _pag_strat = (cfg.get("pagination", {}) or {}).get("strategy", "none")
    if _pag_strat == "template" and stype == "http_html":
        raise ConfigError(
            "pagination.strategy",
            "翻页策略 'template' 仅支持 http_json，不支持 http_html",  # pyflakes：无占位符
            "http_html 翻页请用 browser + pagination.type=click，或 page_param（查询参数翻页）/ next_selector")

    pag = cfg.get("pagination", {})
    if pag is not None and not isinstance(pag, dict):
        raise ConfigError("pagination", f"pagination 应为 dict，实际 {type(pag).__name__}",
                          '例如: "pagination": {"strategy": "none"}')
    # R43 修复：middleware 配置校验——v2 中间件仅支持 log / module:function；
    # 配 v3 专属的 webhook 曾通过 validate 但运行时裸抛 ValueError（裸栈 exit 1）
    for _i, _mw in enumerate(cfg.get("middleware") or []):
        if not isinstance(_mw, dict):
            raise ConfigError(f"middleware[{_i}]", "middleware 项应为 dict",
                              '例如: {"on": "data", "action": "log"}')
        if _mw.get("on") not in ("request", "response", "data", "error"):
            raise ConfigError(f"middleware[{_i}].on", f"未知中间件时机: {_mw.get('on')}",
                              "可选: request / response / data / error")
        _act = _mw.get("action")
        if _act != "log" and not (isinstance(_act, str) and ":" in _act):
            raise ConfigError(f"middleware[{_i}].action",
                              f"v2 中间件仅支持 log 或 module:function 路径，实际: {_act}",
                              "webhook 通知为 v3 引擎能力，请改用 v3 任务包（task 包）或去掉该中间件")
    if pag:
        strat = pag.get("strategy", "none")
        if strat not in PAGINATION_STRATEGIES:
            raise ConfigError("pagination.strategy", f"未知分页策略 '{strat}'",
                              f"可选: {', '.join(sorted(PAGINATION_STRATEGIES))}")
        if strat == "page_param" and not pag.get("page_param"):
            raise ConfigError("pagination.page_param", "page_param 策略需要 page_param 字段",
                              '例如: {"strategy": "page_param", "page_param": "page"}')
        # R81 修复（P2）：browser/browser_script 的翻页策略（click/js）不读顶层
        # pagination——只认 source 内的 pagination。误配时曾"只抓第 1 页就静默
        # 结束"（与 template+http_html 同类）。注意 settle_ms/extra_params/
        # max_pages/start 是顶层确会被转发的合法键，不得误伤
        if stype in ("browser", "browser_script") and (
                strat != "none" or any(k not in ("strategy", "max_pages", "start",
                                                 "settle_ms", "extra_params") for k in pag)):
            raise ConfigError(
                "pagination.strategy",
                "browser/browser_script 的翻页策略（click/js）不读顶层 pagination——请把翻页写进 source 内",
                '例如: {"source": {"type": "browser", ..., "pagination": {"type": "click", '
                '"selector": "a.next", "max_pages": 5}}}')
        # records_path 只对 http_json 强制（html 行来自 row_css，与策略无关）——见下方按类型校验

    pipeline = cfg.get("pipeline", [])
    if not isinstance(pipeline, list):
        # R21 审查修复（P2）：dict 形态曾静默通过验证（漏了 list 包装），流水线
        # 完全不生效、输出含重复——缺失列表类型的提示给出正确写法
        raise ConfigError("pipeline", "pipeline 应为步骤数组",
                          '例如: "pipeline": [{"type": "dedup", "key": "url"}]')
    # OCR R131（L）：上方守卫已确保 list——`if isinstance(...) else []` 是死分支
    for i, step in enumerate(pipeline):
        if not isinstance(step, dict):
            raise ConfigError(f"pipeline[{i}]", f"流水线步骤应为 dict，实际 {type(step).__name__}")
        pt = step.get("type")
        if pt not in PIPELINE_TYPES:
            raise ConfigError(f"pipeline[{i}].type", f"未知流水线类型 '{pt}'",
                              f"可选: {', '.join(sorted(PIPELINE_TYPES))}")
        # R94 修复（P1）：必填键曾只在运行时 KeyError——抓完整单才炸、数据全丢。
        # 按类型前置校验必填参数
        _need = {"filter": ("field",), "cast": ("field",), "add": ("field",),
                 "transform": ("field",), "regex_extract": ("field", "pattern"),
                 "template": ("field", "tmpl"), "rename": ("mapping",)}
        for _k in _need.get(pt, ()):
            if not step.get(_k):
                raise ConfigError(f"pipeline[{i}].{_k}", f"流水线 {pt} 步骤需要 {_k}",
                                  f'例如: {{"type": "{pt}", "{_k}": "..."}}')

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

    # 收官十五轮（用户复盘）：pipeline 跑在详情**之前**——引用了 detail.extract
    # 才产出的字段时，派生列恒空且**没有任何报错**（实测 regex_extract 派生列为
    # None）。这里在跑之前就把这个坑说出来，并指路 detail.post_pipeline
    try:
        import sys as _sys
        _rec_fields = set((rec_fields or {}).keys()) if isinstance(rec_fields, dict) else set()
        _det_names = {str(e.get("name")) for e in ((detail or {}).get("extract") or [])
                      if isinstance(e, dict) and e.get("name")} if isinstance(detail, dict) else set()
        _detail_only = _det_names - _rec_fields
        if _detail_only:
            _hit = [f"{i}:{s.get('type')}({s.get('field')})" for i, s in enumerate(pipeline)
                    if isinstance(s, dict) and str(s.get("field") or "") in _detail_only]
            if _hit:
                print("[WARN] pipeline 引用了仅在详情阶段产出的字段 "
                      f"{sorted(_detail_only)}（步骤 {_hit[:3]}）——pipeline 跑在详情**之前**，"
                      "这些步骤会静默拿到空值、派生列恒为 None。请把这类步骤搬到 "
                      'detail.post_pipeline（详情合并后执行，键与 pipeline 完全一致）',
                      file=_sys.stderr, flush=True)
    except Exception:
        pass  # 告警失败绝不影响校验主流程

    ab = cfg.get("anti_bot", {})
    if ab is not None and not isinstance(ab, dict):
        raise ConfigError("anti_bot", "anti_bot 应为 dict",
                          '例如: "anti_bot": {"min_interval": 1.0}')
    ab = ab if isinstance(ab, dict) else {}
    # 审查修复 P1：负数 max_requests 曾被静默钳成 0（=不限）——硬闸形同虚设
    _mr = ab.get("max_requests")
    if _mr is not None:
        try:
            _mr_i = int(_mr)
        except (TypeError, ValueError):
            raise ConfigError("anti_bot.max_requests", f"应为整数（0=不限），当前 {_mr!r}")
        if _mr_i < 0:
            raise ConfigError("anti_bot.max_requests", f"不能为负数（0=不限），当前 {_mr!r}")
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
    # R21 审查修复（P2）：未知顶层键（如把 pagination 拼成 pagiantion）曾静默
    # 被忽略——配置拼写错误产生"只抓第一页"式假成功
    _KNOWN_KEYS = {"name", "vars", "source", "pagination", "pipeline", "detail",
                   "record", "storage", "anti_bot", "output", "queue", "rules",
                   "parsers", "middleware", "incremental", "download", "start_urls",
                   "sitemap", "iterate", "capture", "base_dir", "description",
                   "_hint"}  # R40：capture2config 产物的操作提示键，非拼写错误
    for k in cfg:
        if k not in _KNOWN_KEYS:
            # OCR R131（M）：空字符串键 "" 曾在 k[0] 越界崩掉整个配置校验
            head = k[0] if k else ""
            warns.append(f"未知顶层键 '{k}'（拼写错误？相似键: "
                         f"{[x for x in _KNOWN_KEYS if x and x[0] == head][:3]}）——该键将被忽略")
    src = cfg.get("source", {}) or {}
    stype = src.get("type", "")
    rec = cfg.get("record", {}) or {}
    # 审查八轮（功能接线）：middleware 的四个时机现已全部有消费点——
    #   data    : engine.run_config 产出记录时
    #   request/response/error : 经 engine._MwClient 适配器包住的客户端
    #                            （列表/分页 + 详情 + 下载三条取数链）
    # （v3 任务包是另一套消费路径：内置 action 类 + 任务自带 modules/middleware.py，
    #  未知 action 由 engine_v3 的 R94 告警提示，与本处无关。）
    # 微博战例：source.fields 提取了、record.fields 空映射 → 输出被丢弃
    # （v1.8 起运行时会自动按 source 字段映射；此提示改为确认口径/改名指引）
    if stype in ("http_html", "browser") and (src.get("fields") or src.get("row_css")) \
            and not rec.get("fields"):
        warns.append("record.fields 未声明——输出列将自动等于提取字段名。"
                     "需改名/筛选请在 record.fields 声明：{\"列名\": {\"from\": \"字段名\"}}")
    if stype == "browser" and not src.get("cdp") and src.get("headless") is not False:
        warns.append("browser 为 headless 且未配 cdp——遇 Cloudflare/Turnstile 会被拦，"
                     "见配方 R16（调试 Chrome 过一次校验后附加）。")
    # 审查三轮（M）："pipeline": null 曾 TypeError（.get 默认值不覆盖显式 null）
    for i, step in enumerate(cfg.get("pipeline") or []):
        if not isinstance(step, dict):
            # 显式 null 元素（[null, {...}]）曾 AttributeError——collect_warnings
            # 会被 validate 之外独立调用（tests/ci_check.py），需自防御跳过
            continue
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
    # 收官十五轮（模糊测试）：source 非 dict（字符串/列表）曾裸 AttributeError；
    # source.type 为 dict/list 时 `in 集合` 抛 unhashable TypeError。都归一为 ConfigError
    src = cfg.get("source", {}) or {}
    if not isinstance(src, dict):
        raise ConfigError("source", f"source 应为 dict，实际 {type(src).__name__}",
                          '例如: {"type": "http", "url": "https://..."}')
    stype = src.get("type", "http")
    if not isinstance(stype, str):
        raise ConfigError("source.type", f"source.type 应为字符串，实际 {type(stype).__name__}",
                          f"可选: {', '.join(sorted(V3_SOURCE_TYPES))}")
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
        # 审查十三轮（H5）：click 支持 xpath 定位（桥同款消费）——只认 selector
        # 曾让 auto 兼容链保留的 xpath 动作在这里被 ConfigError 卡死整单
        if a["type"] == "click" and not (a.get("selector") or a.get("xpath")):
            raise ConfigError(f"source.actions[{i}]", "动作 click 需要 selector 或 xpath")
        if a["type"] in ("type", "write", "fill", "wait_for_selector", "waitfor", "select") and not a.get("selector"):
            raise ConfigError(f"source.actions[{i}]", f"动作 {a['type']} 需要 selector")
        # epub 战训（2026-09）：键名校验——错误键曾静默忽略（fill 只写 value 时
        # 桥按空串填入，失败沉在日志里无汇总提示）。跑之前就拦住。
        _allowed = ACTION_KEY_SPEC.get(a["type"])
        if _allowed is not None:
            _unknown = set(k for k in a.keys() if k not in _allowed and k != "type")
            if _unknown:
                raise ConfigError(f"source.actions[{i}]",
                                  f"动作 {a['type']} 含未知键: {sorted(_unknown)}",
                                  f"合法键: {sorted(_allowed)}")
    inc = cfg.get("incremental") or {}
    if not isinstance(inc, dict):
        raise ConfigError("incremental", f"incremental 应为 dict，实际 {type(inc).__name__}")
    if inc.get("enabled") and not inc.get("key"):
        raise ConfigError("incremental.key", "增量去重需要 key（去重主键字段）", '例如: {"enabled": true, "key": "id"}')
    # R21 审查修复（P2）：容器类型守卫——错误类型曾以裸 AttributeError 崩溃
    # 而非给出 ConfigError 提示
    for key, want in (("parsers", dict), ("rules", list), ("storage", dict),
                      ("start_urls", list), ("pipelines", list),
                      ("incremental", dict), ("download", dict),
                      ("middleware", list), ("detail", dict)):
        val = cfg.get(key)
        # 审查九轮（H）：None（显式 null）曾穿透 isinstance 检查——parsers=null 时
        # 后续 `pname not in cfg.get("parsers", {})` 得 None 引发 TypeError
        if val is None:
            cfg[key] = want()  # null → 空容器（parsers→{} rules→[] 等同缺省）
        elif not isinstance(val, want):
            raise ConfigError(key, f"{key} 应为 {want.__name__}，实际 {type(val).__name__}")
    is_bridge = stype == "bridge"
    needs_seeds = stype in ("http", "browser")
    if needs_seeds and not cfg.get("start_urls") and not src.get("sitemap"):
        raise ConfigError("start_urls", "任务包需要 start_urls（入口 URL 列表）",
                          '例如: ["https://site.com/list"]（桥/自定义 fetch_all 或 source.sitemap 可省略）')
    # OCR R131 二轮（H）：显式 null（"rules": null）曾穿透 .get 默认值——
    # rules=None 进 for 循环 TypeError、storage=None 直接 AttributeError
    rules = cfg.get("rules") or []
    if not is_bridge and not has_custom_fetcher and not rules:
        raise ConfigError("rules", "任务包需要 rules（URL→解析器路由）",
                          '例如: [{"match": "regex", "pattern": "/detail", "parser": "detail"}]'
                          '（自定义 fetcher 可省略）')
    for i, r in enumerate(rules):
        # OCR R131（H）：非 dict 项（如手写配置的字符串）曾裸 .get 崩 AttributeError
        if not isinstance(r, dict):
            raise ConfigError(f"rules[{i}]", f"规则应为 dict，实际 {type(r).__name__}",
                              '例如: {"match": "startswith", "pattern": "/detail", "parser": "detail"}')
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
        # OCR R131（H）：`(step or {})` 不防非 dict 真值（如字符串 "dedup"）
        if not isinstance(step, dict):
            raise ConfigError(f"pipelines[{i}]", f"流水线步骤应为 dict，实际 {type(step).__name__}",
                              '例如: {"type": "dedup", "key": "url"}')
        pt = step.get("type")
        if pt not in ALL_PIPELINE_TYPES:
            raise ConfigError(f"pipelines[{i}].type", f"未知流水线类型 '{pt}'",
                              f"可选: {', '.join(sorted(ALL_PIPELINE_TYPES))}")
        if pt == "download" and not step.get("field"):
            raise ConfigError(f"pipelines[{i}]", "download 流水线需要 field（下载 URL 字段）")
    # 收官十五轮（用户复盘）：pipelines 跑在详情**之前**——引用了 detail.extract
    # 才产出的字段时派生列恒空且无报错。跑之前先说清楚，并指路 post_pipeline
    try:
        import sys as _sys2
        _dc = cfg.get("detail")
        _p_fields = set()
        for _pc in (cfg.get("parsers") or {}).values():
            if isinstance(_pc, dict):
                _p_fields.update((_pc.get("fields") or {}).keys())
        _d_names = {str(e.get("name")) for e in ((_dc or {}).get("extract") or [])
                    if isinstance(e, dict) and e.get("name")} if isinstance(_dc, dict) else set()
        _d_only = _d_names - _p_fields
        if _d_only:
            _hit2 = [f"{i}:{s.get('type')}({s.get('field')})"
                     for i, s in enumerate(cfg.get("pipelines", []) or [])
                     if isinstance(s, dict) and str(s.get("field") or "") in _d_only]
            if _hit2:
                print(f"[WARN] pipelines 引用了仅在详情阶段产出的字段 {sorted(_d_only)}"
                      f"（步骤 {_hit2[:3]}）——pipelines 跑在详情**之前**，这些步骤会静默"
                      "拿到空值。请把这类步骤搬到 detail.post_pipeline（详情合并后执行）",
                      file=_sys2.stderr, flush=True)
    except Exception:
        pass  # 告警失败不影响校验主流程
    # detail.filters 校验（审查八轮，HIGH）：v3 的详情过滤此前**完全不校验**，
    # 坏步骤（如 {"type":"cast"} 缺 field、filter 缺 value）在抓完之后才崩——
    # 异常穿透 _run_locked，_finalize/_save_state 全被跳过，输出目录只剩 items/
    # （json/csv/xlsx/state 一个都没有）。与顶层 pipelines 同口径，跑之前拦住。
    _det_cfg = cfg.get("detail")
    if isinstance(_det_cfg, dict):
        # 收官十五轮：post_pipeline（详情后处理，与 filters 同义/跨引擎统一键）
        # 与 filters 同口径校验
        for _dkey in ("filters", "post_pipeline"):
            for i, step in enumerate(_det_cfg.get(_dkey, []) or []):
                _where = f"detail.{_dkey}[{i}]"
                if not isinstance(step, dict):
                    raise ConfigError(_where,
                                      f"过滤步骤应为 dict，实际 {type(step).__name__}",
                                      '例如: {"type": "filter", "field": "标题", "op": "contains", "value": "2024"}')
                pt = step.get("type")
                if pt not in ALL_PIPELINE_TYPES:
                    raise ConfigError(f"{_where}.type", f"未知流水线类型 '{pt}'",
                                      f"可选: {', '.join(sorted(ALL_PIPELINE_TYPES))}")
                if pt in ("filter", "cast", "add", "validate", "download", "split",
                          "rename", "default", "template", "transform", "regex_extract") \
                        and not step.get("field"):
                    raise ConfigError(_where, f"{pt} 步骤需要 field"
                                      "（modules/pipelines.py 用 step[\"field\"] 取值，缺则 KeyError 崩详情阶段）")
                if pt == "regex_extract" and not step.get("pattern"):
                    raise ConfigError(f"{_where}.pattern", "regex_extract 需要 pattern",
                                      '例如: {"type": "regex_extract", "field": "raw", '
                                      '"pattern": r"(\\\\d+)", "to": "数字"}')
                if pt == "filter" and step.get("op", "contains") in ("contains", "eq", "not_contains") \
                        and step.get("value") is None and not step.get("pattern"):
                    raise ConfigError(f"{_where}.value",
                                      f"filter(op={step.get('op', 'contains')}) 需要 value（或 regex 用 pattern）",
                                      '例如: {"type": "filter", "field": "标题", "op": "contains", "value": "中标"}')
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
    st = cfg.get("storage") or {}  # OCR R131 二轮（H）："storage": null 曾 AttributeError
    if st.get("type", "jsonl") not in ALL_STORAGE_TYPES and not has_custom_storage:
        raise ConfigError("storage.type", f"未知存储类型 '{st.get('type')}'",
                          "可选: " + " / ".join(sorted(ALL_STORAGE_TYPES)) + " 或提供 modules/storage.py 自定义")
    if st.get("type") == "multi" and not st.get("backends"):
        raise ConfigError("storage.backends", "multi 存储需要 backends 数组",
                          '[{"type": "jsonl"}, {"type": "sqlite"}]')
    return cfg
