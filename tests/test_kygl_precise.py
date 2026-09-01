# -*- coding: utf-8 -*-
"""《科研管理》期刊精配：URL 路由 + magtech 期次页解析（离线 fixture）。"""
from universal_scraper.sites import match_kygl, _kygl_parse

FIXTURE = '''<html><body>
<li id="art23574">
<div class="j-title-1">
  <a href="https://www.kygl.net.cn/CN/10.19571/j.cnki.1000-2995.2026.01.001">人工智能赋能的本质认识:数据、知识与系统的三重整合</a>
</div>
<div class="j-author">潘教峰, 王楚扬, 吴静</div>
<div class="j-volumn-doi">
  <span class="j-volumn">科研管理. 2026, 47(1): 1-10.</span>
  <a class="j-doi" href="https://doi.org/10.19571/j.cnki.1000-2995.2026.01.001">https://doi.org/10.19571/j.cnki.1000-2995.2026.01.001</a>
</div>
</li>
</body></html>'''


def test_match_kygl():
    assert match_kygl("https://www.kygl.net.cn/CN/Y2026/V47/I1")
    assert match_kygl("https://www.kygl.net.cn/CN/10.19571/j.cnki.1000-2995.2026.01.001")
    assert not match_kygl("https://example.com/Y2026")


def test_parse_kygl_issue():
    rows = _kygl_parse(FIXTURE, "https://www.kygl.net.cn/CN/Y2026/V47/I1")
    assert len(rows) == 1
    r = rows[0]
    assert r["title"].startswith("人工智能赋能")
    assert r["doi"].endswith("10.19571/j.cnki.1000-2995.2026.01.001")
    assert r["year"] == "2026"
