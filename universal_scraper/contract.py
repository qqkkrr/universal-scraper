"""配置契约注册表 —— 配置语言的单一事实来源。

背景（四轮实战反馈的根因）：validate 词表、v2 执行器词表、v3 执行器词表、
文档记载各写各的，导致"文档教的写法必崩 / 已有能力没文档 / validator 放行但
执行器跳过"三类契约 bug 反复发作。本文件是唯一声明处：

- config.ALL_PIPELINE_TYPES 从 PIPELINE_STEPS 派生（不得再手抄）
- tests/test_contract_docs.py 断言：执行器实际处理的类型 == 注册表 v2 声明；
  文档提及的类型 ⊆ 注册表；注册表条目 ⊆ 文档
- 新增步骤/字段格式的正确姿势：先在这里声明，再实现，文档自动被测试约束

executor 含义：v2 = run --config（engine.run_pipeline）；v3 = 任务包
（modules/pipelines.BasePipeline）；both = 两边都有实现。
"""
from __future__ import annotations

from typing import Dict

PIPELINE_STEPS: Dict[str, Dict[str, str]] = {
    # ---- v2/v3 双实现 ----
    "filter":        {"executor": "both", "params": "field, op(contains|eq|regex|not_contains|between), value|pattern|min/max"},
    "dedup":         {"executor": "both", "params": "key(str|list)"},
    "rename":        {"executor": "both", "params": "mapping{旧列:新列}"},
    "cast":          {"executor": "both", "params": "field, to(int|float|str)"},
    "add":           {"executor": "both", "params": "field, value"},
    # ---- 仅 v2（engine.run_pipeline）----
    "transform":     {"executor": "v2", "params": "field, op(unix_to_datetime|upper|lower), fmt"},
    "template":      {"executor": "v2", "params": "field, tmpl({字段}插值)"},
    "regex_extract": {"executor": "v2", "params": "field, pattern, group, to"},
    # ---- 仅 v3（modules/pipelines.BasePipeline）----
    "dedup_content": {"executor": "v3", "params": "field"},
    "validate":      {"executor": "v3", "params": "field, rule"},
    "default":       {"executor": "v3", "params": "field, value"},
    "split":         {"executor": "v3", "params": "field, sep"},
    "download":      {"executor": "v3", "params": "field, dir"},
    "parse_date":    {"executor": "v3", "params": "field, fmt"},
}

V2_ONLY_STEPS = {k for k, v in PIPELINE_STEPS.items() if v["executor"] == "v2"}
V3_ONLY_STEPS = {k for k, v in PIPELINE_STEPS.items() if v["executor"] == "v3"}
ALL_STEP_TYPES = set(PIPELINE_STEPS)


def v2_step_types() -> set:
    """run --config 执行器（engine.run_pipeline）支持的全部类型。"""
    return {k for k, v in PIPELINE_STEPS.items() if v["executor"] in ("both", "v2")}


# 曾出过格式契约 bug 的字段：接受格式在此声明（消费端 normalizer 已实现）。
# tests/test_contract_docs.py 会断言这里的每条都在文档中有对应说明。
FIELD_FORMATS: Dict[str, Dict[str, str]] = {
    "anti_bot.cookies": {
        "accepts": "dict|cookie_string",
        "normalizer": "core._norm_cookies",
        "doc": "Cookie 直抓。dict 或 \"k=v; k2=v2\" 串均可（cookies 命令导出的串可直接粘贴）",
    },
    "source.capture": {
        "accepts": "bool(=全捕获)|list(=声明式捕获)",
        "doc": "true 会被翻译为桥的 capture_all；绝不能把布尔当数组传给桥",
    },
    "source.embedded_json": {
        "accepts": "str(var引用)|dict{var,path}",
        "doc": "页面内嵌 JSON 提取：\"window.article_list\" 或 {\"var\": ..., \"path\": ...}",
    },
    "pagination.start": {
        "accepts": "int(起始页号)",
        "doc": "定向分页/历史回溯；HTTP 直接生效，browser 配深链 URL 使用",
    },
    "pagination.records_path": {
        "accepts": "str(点路径)",
        "doc": "http_json 必填——缺了会静默 0 条（validate 现已强制）",
    },
}
