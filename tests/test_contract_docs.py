"""配置契约测试——"配置契约"类的 CI 防线。

四轮实战反馈的病根：validate 词表 / v2 执行器 / v3 执行器 / 文档记载 四套词表各自漂移。
本文件断言它们只能从 contract.py 单一来源派生，且文档示例真实可执行。
"""
import inspect
import json
import re
from pathlib import Path

import pytest

from universal_scraper import contract, engine
from universal_scraper.config import ALL_PIPELINE_TYPES, ConfigError, validate, collect_warnings

DOCS = Path(__file__).resolve().parent.parent / "docs" / "skill-references"


def test_config_vocab_derived_from_registry():
    """validate 词表必须等于注册表词表（杜绝手抄漂移）。"""
    assert ALL_PIPELINE_TYPES == contract.ALL_STEP_TYPES


def test_v2_executor_handles_exactly_declared_types():
    """执行器实际处理的类型集合 == 注册表 v2 声明（源码级断言）。"""
    src = inspect.getsource(engine.run_pipeline)
    handled = set(re.findall(r'st == "(\w+)"', src))
    assert handled == contract.v2_step_types(), (
        f"执行器与注册表漂移: 多实现={sorted(handled - contract.v2_step_types())}, "
        f"少实现={sorted(contract.v2_step_types() - handled)}")


def _doc_blocks():
    if not DOCS.exists():
        return []
    out = []
    for f in sorted(DOCS.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"```json\n(.*?)```", text, re.S):
            out.append((f.name, m.group(1)))
    return out


def test_every_complete_doc_config_passes_validate():
    """文档里的完整配置示例必须能通过 validate——文档烂了测试就红。"""
    blocks = _doc_blocks()
    if not blocks:
        pytest.skip("技能文档未同步到 docs/skill-references/")
    n = 0
    for fname, block in blocks:
        b = block.strip()
        # 只测完整配置：带 name + source + url 的对象；片段（数组/缺 url）走词表覆盖测试
        if not (b.startswith("{") and '"name"' in b and '"source"' in b and '"url"' in b):
            continue
        try:
            cfg = json.loads(b)
        except json.JSONDecodeError:
            continue
        if not isinstance(cfg, dict) or not isinstance(cfg.get("source"), dict):
            continue
        validate(cfg)  # 有问题直接抛 ConfigError 让测试红
        n += 1
    assert n >= 2, f"可验证的完整配置示例过少: {n}"


def test_doc_pipeline_types_in_registry():
    """文档 JSON 里出现的每个 pipeline 步骤类型都必须在注册表中。"""
    blocks = _doc_blocks()
    if not blocks:
        pytest.skip("技能文档未同步")
    known = sorted(contract.ALL_STEP_TYPES)
    for fname, block in blocks:
        b = block.strip()
        if not b[:1] in ("[", "{"):
            continue
        try:
            obj = json.loads(re.sub(r",\s*([}\]])", r"\1", b))
        except json.JSONDecodeError:
            continue
        items = obj if isinstance(obj, list) else [obj]
        for step in items:
            if isinstance(step, dict) and step.get("type") in known:
                assert step["type"] in contract.ALL_STEP_TYPES


def test_registry_steps_documented():
    """注册表每个步骤类型都必须在文档中有说明（治'能力存在但没文档'）。"""
    if not DOCS.exists():
        pytest.skip("技能文档未同步")
    text = "\n".join(f.read_text(encoding="utf-8") for f in DOCS.glob("*.md"))
    for t in contract.ALL_STEP_TYPES:
        assert f'"{t}"' in text or f"`{t}`" in text or t in text, f"步骤 {t} 未在文档中说明"


def test_field_formats_documented():
    """出过格式 bug 的字段必须在文档中有对应说明。"""
    if not DOCS.exists():
        pytest.skip("技能文档未同步")
    text = "\n".join(f.read_text(encoding="utf-8") for f in DOCS.glob("*.md"))
    for field in contract.FIELD_FORMATS:
        base = field.split(".")[-1]
        assert base in text, f"字段 {field} 未在文档中说明"


def test_semantic_rules_fire():
    """跨字段语义规则：微博双层丢弃 / B站 records_path / PH cloudflare 提示。"""
    cfg = {"name": "t", "source": {"type": "http_html", "url": "https://x.example/",
                                   "row_css": ".item"},
           "pagination": {"strategy": "none"}}
    warns = collect_warnings(cfg)
    assert any("record.fields" in w for w in warns), "双层字段空映射必须告警"

    bad = {"name": "t", "source": {"type": "http_json", "url": "https://api.x.example/"},
           "pagination": {"strategy": "none"}}
    with pytest.raises(ConfigError):
        validate(bad), "http_json 缺 records_path 必须报错"

    ok = {"name": "t", "source": {"type": "http_json", "url": "https://api.x.example/"},
          "pagination": {"strategy": "none", "records_path": "data.list"}}
    assert validate(ok) == ok
    assert collect_warnings(ok) == []

    br = {"name": "t", "source": {"type": "browser", "url": "https://x.example/"},
          "pagination": {"strategy": "none"}}
    assert any("cdp" in w or "R16" in w for w in collect_warnings(br)), "headless browser 应提示 R16"

    cfg_v3 = {"name": "t", "source": {"type": "http_html", "url": "https://x.example/",
                                      "row_css": ".i"},
              "record": {"fields": {"a": {"from": "a"}}},
              "pagination": {"strategy": "none"},
              "pipeline": [{"type": "parse_date", "field": "d"}]}
    assert any("仅 v3" in w for w in collect_warnings(cfg_v3)), "v3-only 类型必须告警"
