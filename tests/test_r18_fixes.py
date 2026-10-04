#!/usr/bin/env python3
"""审查十八轮（R18）修复的回归测试。

覆盖：
- config：不可哈希标量（list/dict）穿 set 判定族（9 处）→ ConfigError 不裸崩
- sites：R50 剩余项清账——weibo cookie 透传 / aqi PM2.5=0 保留 / zhihu 非问题类
  不断链 / unpaywall email 可配置 / 重复定义清零且 _EXRATE_BASE 保留
"""
import ast
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


def test_config_unhashable_scalars_raise_configerror():
    from universal_scraper.config import ConfigError, validate, validate_task
    base_v = {"name": "t", "source": {"type": "http_html", "url": "http://x/"}}
    # validate 族：不可哈希标量曾 TypeError 裸崩（`x not in SET`）
    for cfg in ({**base_v, "pagination": {"strategy": ["a"]}},
                {**base_v, "pipeline": [{"type": ["a"]}]},
                {**base_v, "detail": {"extract": [{"type": {"a": 1}}]}},
                {**base_v, "anti_bot": {"captcha": {"strategy": ["a"]}}}):
        with pytest.raises(ConfigError):
            validate(cfg)
    base_t = {"name": "t", "start_urls": ["http://x/"],
              "parsers": {"default": {"type": "html", "fields": {"t": "h1"}}},
              "rules": [{"match": "contains", "pattern": "/", "parser": "default"}]}
    for cfg in ({**base_t, "source": {"type": "http",
                                      "actions": [{"type": ["a"], "selector": ".x"}]}},
                {**base_t, "rules": [{"match": "contains", "pattern": "/",
                                      "parser": ["a"]}]},
                {**base_t, "source": {"type": "http"}, "pipelines": [{"type": ["a"]}]},
                {**base_t, "source": {"type": "http"},
                 "parsers": {"default": {"type": ["llm"], "fields": {}}}},
                {**base_t, "source": {"type": "http"}, "storage": {"type": ["a"]}}):
        with pytest.raises(ConfigError):
            validate_task(cfg)
    # 正常配置回归
    validate({**base_v, "pagination": {"strategy": "none"}})
    validate_task({**base_t, "source": {"type": "http"}})


def test_sites_r50_leftovers():
    from universal_scraper import sites as S
    src = Path(S.__file__).read_text(encoding="utf-8")
    # 重复定义清零（R50 批次复制粘贴产物）
    tree = ast.parse(src)
    defs = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    for f in ("match_netease_music", "match_douban_movie", "match_exchange_rate"):
        assert defs.count(f) == 1, f
    assert src.count('_EXRATE_BASE = "https://open.er-api.com') == 1   # 唯一赋值保留
    assert "_line_field_text" not in src.split("# 审查十八轮（L）：_line_field_text 曾在此")[1][:200]
    # weibo cookie 透传
    import inspect
    assert "cookie=cookie" in inspect.getsource(S._weibo_hot_run)
    # aqi PM2.5=0 保留（显式判 None）
    aqi_src = inspect.getsource(S._aqi_run)
    assert "_pm25 = d.get" in aqi_src and "is not None" in aqi_src
    # zhihu 非问题类不断链
    zh_src = inspect.getsource(S._zhihu_hot_run)
    assert '_ttype == "question"' in zh_src
    # unpaywall email 可配置
    up_src = inspect.getsource(S._unpaywall_run)
    assert "UNPAYWALL_EMAIL" in up_src


def test_sites_matchers_still_callable():
    from universal_scraper import sites as S
    # 注册表引用的 matcher 删除重复定义后仍可用
    assert S.SITES["exchange_rate"]["match"]("https://open.er-api.com/v6/latest/USD") is True
    assert S.SITES["netease_music"]["match"]("https://music.163.com/x") is True
    assert S.SITES["douban_movie"]["match"]("https://movie.douban.com/x") is True
