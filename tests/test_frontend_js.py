# -*- coding: utf-8 -*-
"""前端 JS 语法回归：webui/index.html 内嵌 <script> 必须可解析。"""
import subprocess, re
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "webui" / "index.html"

def test_index_js_syntax():
    s = HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>([\s\S]*?)</script>", s)
    assert m, "未找到内嵌 <script>"
    code = m.group(1)
    r = subprocess.run(["node", "-e", f"new Function({code!r}); console.log('OK')"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"JS 语法错误: {r.stderr[:300]}"
    assert "OK" in r.stdout

def test_index_contains_key_ui():
    s = HTML.read_text(encoding="utf-8")
    for key in ("a_guide", "diag_wrap", "a_examples", "第一次用？"):
        assert key in s, f"缺少 UI 元素/文案: {key}"
