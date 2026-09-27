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
    "filter":        {"executor": "both", "params": "field, op(contains|eq|regex|not_contains|between|non_empty), value|pattern|min/max"},
    "dedup":         {"executor": "both", "params": "key(str|list)"},
    "rename":        {"executor": "both", "params": "mapping{旧列:新列}"},
    "cast":          {"executor": "both", "params": "field, to(int|float|str)"},
    "add":           {"executor": "both", "params": "field, value"},
    # ---- 仅 v2（engine.run_pipeline）----
    "transform":     {"executor": "v2", "params": "field, op(unix_to_datetime|upper|lower), fmt"},
    # R113 对齐：template v2/v3 双实现（v3 于 R109 修复 tmpl 键漂移后支持）
    "template":      {"executor": "both", "params": "field, tmpl({字段}插值)"},
    "regex_extract": {"executor": "v2", "params": "field, pattern, group, to"},
    # ---- 仅 v3（modules/pipelines.BasePipeline）----
    "dedup_content": {"executor": "v3", "params": "field"},
    "validate":      {"executor": "v3", "params": "field, rule"},
    "default":       {"executor": "v3", "params": "field, value"},
    "split":         {"executor": "v3", "params": "field, sep"},
    "download":      {"executor": "v3", "params": "field, dir"},
    # R113 对齐：v3 实际消费 field/out/now（相对时间中文解析，无 fmt 自定义格式）
    "parse_date":    {"executor": "v3", "params": "field, out, now"},
}

# OCR R131（M/L）：V2_ONLY_STEPS/ALL_STEP_TYPES/v2_step_types 无任何消费方——删除；
# V3_ONLY_STEPS 保留（config.py 校验消费）。PIPELINE_STEPS 冻结为只读防运行期
# 篡改导致派生集过期
import types as _types
PIPELINE_STEPS = _types.MappingProxyType(PIPELINE_STEPS)  # type: ignore[assignment]
V3_ONLY_STEPS = {k for k, v in PIPELINE_STEPS.items() if v["executor"] == "v3"}


# 曾出过格式契约 bug 的字段：接受格式在此声明（消费端 normalizer 已实现）。
# OCR R131（M）：只有 anti_bot.cookies 带 normalizer 键——其余条目补空串占位，
# 消费方 entry["normalizer"] 直取不再 KeyError（schema 一致）。
# OCR R131 终审（M）：内层 dict 曾可变、外层无冻结——与 PIPELINE_STEPS 同一
# 口径收口为只读视图；本表是声明式契约文档（配置作者/LLM 对照用），运行期
# 不消费，校验工具未来可直接 import 此只读视图。
FIELD_FORMATS = _types.MappingProxyType({
    "anti_bot.cookies": _types.MappingProxyType({
        "accepts": "dict|cookie_string",
        "normalizer": "core._norm_cookies",
        "doc": "Cookie 直抓。dict 或 \"k=v; k2=v2\" 串均可（cookies 命令导出的串可直接粘贴）",
    }),
    "source.capture": _types.MappingProxyType({
        "accepts": "bool(=全捕获)|list(=声明式捕获)",
        "normalizer": "",
        "doc": "true 会被翻译为桥的 capture_all；绝不能把布尔当数组传给桥",
    }),
    "source.embedded_json": _types.MappingProxyType({
        "accepts": "str(var引用)|dict{var,path}",
        "normalizer": "",
        "doc": "页面内嵌 JSON 提取：\"window.article_list\" 或 {\"var\": ..., \"path\": ...}",
    }),
    "pagination.start": _types.MappingProxyType({
        "accepts": "int(起始页号)",
        "normalizer": "",
        "doc": "定向分页/历史回溯；HTTP 直接生效，browser 配深链 URL 使用",
    }),
    "pagination.records_path": _types.MappingProxyType({
        "accepts": "str(点路径)",
        "normalizer": "",
        "doc": "推荐显式声明；缺省时自动识别常见键(records/items/list/results/data)与根数组，"
               "未命中会 WARN。多页翻页(template/page_param)强烈建议声明",
    }),
})
