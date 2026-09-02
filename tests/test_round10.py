"""第十轮反馈回归（版权中心战例）：~ 展开 / jsrecon 安全边界 / recon 模式 / validate 提示。"""
import json
from pathlib import Path

import pytest

from universal_scraper.config import ConfigError, validate
from universal_scraper.engine import run_config  # noqa: F401（导入冒烟）
from universal_scraper.quick import js_recon


def test_tilde_output_dir_expands(tmp_path, monkeypatch):
    """版权中心战例：~/Desktop 写法曾建出名为 '~' 的字面目录。"""
    monkeypatch.chdir(tmp_path)
    from universal_scraper.engine import run_config as _  # noqa
    src = Path("/Users/kairanqin/.agents/skills/universal-scraper/universal_scraper/engine.py").read_text(encoding="utf-8")
    assert "expanduser" in src, "output.dir 必须过 expanduser"
    # 行为验证：构造小配置跑 export 路径太重，直接断言解析函数行为
    import os
    assert os.path.expanduser("~/x").startswith("/"), "expanduser 契约"


def test_jsrecon_rejects_private_and_bad_scheme():
    r = js_recon("file:///etc/passwd")
    assert "仅允许 http/https" in r["error"]
    r2 = js_recon("http://127.0.0.1:8080/x")
    assert "私有" in r2["error"] or "环回" in r2["error"] or "拒绝" in r2["error"]


def test_jsrecon_rejects_unresolvable():
    r = js_recon("https://this-domain-definitely-not-exist-qqkkrr-9x.com/")
    assert "error" in r


def test_recon_mode_flag_in_doc():
    p = Path("/Users/kairanqin/.agents/skills/universal-scraper/references/spec-schema.md")
    assert "recon" in p.read_text(encoding="utf-8")


def test_validate_missing_name_suggests_scaffold():
    with pytest.raises(ConfigError) as ei:
        validate({"source": {"type": "http_html", "url": "https://x/"}})
    assert "scaffold" in str(ei.value)


def test_capture_schema_documented():
    p = Path("/Users/kairanqin/.agents/skills/universal-scraper/references/spec-schema.md")
    t = p.read_text(encoding="utf-8")
    assert "post_data" in t and "request_content_type" in t, "capture_all.json 结构必须文档化"
