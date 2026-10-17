#!/usr/bin/env python3
"""审查三十轮 R30 新增能力的回归测试。

覆盖（对应本轮新增/追加的四个面）：
- `core.fetch_json`：非 JSON / 非 ok / 顶层非对象 → 抛带体首段的 RuntimeError（不静默兜底）
- `core.run_tool`：白名单外拒绝、空参拒绝（外部转换器的唯一入口）
- `attachments`：扩展名三级定名、magic 嗅探（含 OLE/ZIP 细分）、viewer 外壳不落盘、
  镜像并集、幂等下载、出站守卫拒绝私网
- `local_ocr`：裁决规则（agree/differ/alt_wins）、不可用/空图不抛异常
- `name_list`：表头列位映射、rowspan 续行并回、序号列空时不启用"无序号续行"、
  一问题一行合并、同 APP 多来源合并、跨页续表、声明数句子级求和与两类排除句、
  完备率（源表声明列口径）、文档优先图片兜底
"""
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))


# ---------------- core：fetch_json / run_tool ----------------

class _FakeResp:
    def __init__(self, body=b"", ok=True, status=200):
        self._b, self._ok, self._s = body, ok, status

    def get(self, k, default=None):
        return {"body": self._b, "ok": self._ok, "status": self._s}.get(k, default)


class _FakeClient:
    def __init__(self, resp):
        self.resp = resp

    def get(self, url, **kw):
        return self.resp


def test_r30_fetch_json_parses_and_reports_body_on_failure():
    from universal_scraper import core
    ok = core.fetch_json(_FakeClient(_FakeResp(b'{"a": 1}')), "http://x/y")
    assert ok == {"a": 1}
    # 非 JSON：报错必须带体首段（网关限流/验证码页是常态）
    with pytest.raises(RuntimeError) as e1:
        core.fetch_json(_FakeClient(_FakeResp(b"<html>blocked</html>")), "http://x/y")
    assert "blocked" in str(e1.value) and "非 JSON" in str(e1.value)
    # ok=False 也要抛（不能把失败当空数据）
    with pytest.raises(RuntimeError):
        core.fetch_json(_FakeClient(_FakeResp(b"{}", ok=False, status=503)), "http://x/y")
    # 顶层是数组 → 抛（契约是对象）
    with pytest.raises(RuntimeError):
        core.fetch_json(_FakeClient(_FakeResp(b"[1,2]")), "http://x/y")


def test_r30_run_tool_whitelist_blocks_non_tools():
    from universal_scraper import core
    r = core.run_tool(["rm", "-rf", "/tmp/whatever"])
    assert r["ok"] is False and "白名单" in r["err"]
    assert core.run_tool([])["ok"] is False


# ---------------- attachments ----------------

def test_r30_ext_of_url_three_levels():
    from universal_scraper import attachments as AT
    assert AT.ext_of_url("https://a/b/c.docx") == ".docx"
    # ?fileUrl= 包壳（政府 CMS 常见）
    assert AT.ext_of_url("https://a/api-gateway/download?fileUrl=/cms_files/x/1.pdf") == ".pdf"
    assert AT.ext_of_url("https://a/cms_files/part/123") == ".bin"
    assert AT.ext_of_url("https://a/cms_files/part/123", default="dat") == ".dat"


def test_r30_sniff_ext_distinguishes_ole_and_ooxml():
    from universal_scraper import attachments as AT
    assert AT.sniff_ext(b"\x89PNG\r\n\x1a\n" + b"0" * 40) == ".png"
    assert AT.sniff_ext(b"%PDF-1.7") == ".pdf"
    # ZIP 容器：流名可能在尾部，必须扫全量
    assert AT.sniff_ext(b"PK\x03\x04" + b"\x00" * 4000 + b"xl/workbook.xml") == ".xlsx"
    assert AT.sniff_ext(b"PK\x03\x04" + b"\x00" * 10 + b"word/document.xml") == ".docx"
    assert AT.sniff_ext(b"\xd0\xcf\x11\xe0" + b"\x00" * 100 + b"WordDocument") == ".doc"
    assert AT.sniff_ext(b"\xd0\xcf\x11\xe0" + b"\x00" * 100 + b"Workbook") == ".xls"
    assert AT.sniff_ext(b"") == ""


def test_r30_extract_targets_skips_viewer_shell_and_unions_mirrors():
    from universal_scraper import attachments as AT
    canonical = ('<a href="/cms_files/f/attach/a.docx">x</a>'
                 '<iframe src="/viewer.html?file=/cms_files/f/attach/a.docx"></iframe>'
                 '<img src="/cms_files/f/picture/1.png"/>')
    mirror = ('<a href="/cms_files/f/attach/a.docx">x</a>'
              '<img src="/cms_files/f/picture/2.png"/>')
    a1, i1 = AT.extract_targets(canonical, "https://h")
    assert a1 == ["https://h/cms_files/f/attach/a.docx"], a1          # 外壳不重复计入
    assert i1 == ["https://h/cms_files/f/picture/1.png"]
    au, iu = AT.mirror_union([canonical, mirror], "https://h")
    assert len(au) == 1 and len(iu) == 2, (au, iu)                     # 镜像补链才拿到第 2 张


def test_r30_download_targets_idempotent_and_rejects_html(tmp_path):
    from universal_scraper import attachments as AT

    class _C:
        def __init__(self, mapping):
            self.mapping, self.hits = mapping, []

        def get(self, url, **kw):
            self.hits.append(url)
            body = self.mapping.get(url, b"")
            return {"body": body, "ok": bool(body), "status": 200 if body else 404}

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    urls = ["https://h/cms_files/f/attach/1.png", "https://h/cms_files/f/attach/2.png"]
    c = _C({urls[0]: png, urls[1]: b"<!DOCTYPE html><html>x</html>"})
    r1 = AT.download_targets(c, urls, tmp_path / "d", prefix="att")
    assert len(r1["saved"]) == 1 and Path(r1["saved"][0]).suffix == ".png"
    assert len(r1["failed"]) == 1 and "HTML" in r1["failed"][0]["error"]
    # 幂等：第二次跑只重试**失败项**（成功的跳过、不再发请求）
    c.hits.clear()
    r2 = AT.download_targets(c, urls, tmp_path / "d", prefix="att")
    assert len(r2["skipped"]) == 1 and c.hits == [urls[1]]
    # 内容嗅探校正扩展名：URL 无扩展名（.bin），内容是真 PNG
    c.mapping["https://h/cms_files/part/9"] = png
    r3 = AT.download_targets(c, ["https://h/cms_files/part/9"], tmp_path / "d2", prefix="att")
    assert Path(r3["saved"][0]).suffix == ".png"


def test_r30_download_targets_blocks_private_host(tmp_path):
    from universal_scraper import attachments as AT

    class _C:
        def get(self, url, **kw):          # 不该被调用
            raise AssertionError("私网 URL 不应发起请求")

    r = AT.download_targets(_C(), ["http://127.0.0.1/x.docx"], tmp_path / "d")
    assert r["saved"] == [] and len(r["failed"]) == 1
    assert "守卫" in r["failed"][0]["error"] or "拒绝" in r["failed"][0]["error"]


def test_r30_dedupe_by_content(tmp_path):
    from universal_scraper import attachments as AT
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"%PDF-1.7 same")
    b.write_bytes(b"%PDF-1.7 same")
    keep, dups = AT.dedupe_by_content([str(a), str(b)])
    assert keep == [str(a)] and dups == [str(b)]


# ---------------- local_ocr ----------------

def test_r30_cross_check_verdict_three_way():
    from universal_scraper import local_ocr as L
    # LLM 顺句补字（多一个"信"）→ 本地引擎赢
    assert L.cross_check_verdict("苏州云网通信信息科技有限公司",
                                 "苏州云网通信息科技有限公司") == "alt_wins"
    # 形近字分歧（不是插入）→ 保留主读，标 differ
    assert L.cross_check_verdict("杭州丁香健康管理有限公司",
                                 "杭州丁香使康管理有限公司") == "differ"
    assert L.cross_check_verdict("北京米拓世纪科技有限公司",
                                 "北京米拓世纪科技有限公司") == "agree"
    # 副读混入商店词/重复字 → 不算干净，不能翻盘
    assert L.cross_check_verdict("北京环宇万维科技有限公司",
                                 "北京环宇万维科技OPPO软件商店有限公司") == "differ"


def test_r30_local_ocr_never_raises_on_bad_input(tmp_path):
    from universal_scraper import local_ocr as L
    assert isinstance(L.available(), bool)
    assert L.ocr_image(tmp_path / "missing.png") == []
    assert L.ocr_text(tmp_path / "missing.png") == ""
    assert L.row_bands(tmp_path / "missing.png") == []


def test_r30_row_bands_on_synthetic_table(tmp_path):
    from universal_scraper import local_ocr as L
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw
    img = Image.new("L", (120, 200), 255)
    d = ImageDraw.Draw(img)
    for y in (10, 60, 110, 160):                 # 4 条横线 → 3 个行带
        d.line([(0, y), (119, y)], fill=0)
    p = tmp_path / "t.png"
    img.save(p)
    bands = L.row_bands(p, x_frac=(0.0, 0.2))
    assert len(bands) == 3, bands
    assert bands[0][1] < bands[1][0]


# ---------------- name_list ----------------

def test_r30_header_map_uses_positions_not_shapes():
    from universal_scraper import name_list as NL
    hm = NL.header_map(["序号", "应用名称", "应用开发者", "应用来源", "应用版本", "所涉问题"])
    assert hm == {"seq": 0, "app_name": 1, "company_name": 2, "store_source": 3,
                  "version": 4, "issue_types": 5}
    # "应用 类别" 前置列不影响：表头驱动按列位
    hm2 = NL.header_map(["应用类别", "序号", "应用名称", "运营者名称", "应用来源", "版本号", "所涉问题"])
    assert hm2["company_name"] == 3 and hm2["seq"] == 1


def test_r30_table_rows_rowspan_continuation_and_empty_seq_column():
    from universal_scraper import name_list as NL
    # ① rowspan 续行：前 5 列跨 3 行，每物理行只装一个问题 → 必须并回
    rows = [
        ["序号", "应用名称", "应用开发者", "版本", "版本来源", "所涉问题"],
        ["1", "QQ", "深圳市腾讯计算机系统有限公司", "8.2.0", "官网", "强制用户使用定向推送功能"],
        ["", "", "", "", "", "不给权限不让用"],
        ["", "", "", "", "", "账号注销难"],
        ["2", "QQ阅读", "上海阅文信息技术有限公司", "7.1.1", "官网", "私自收集个人信息"],
    ]
    out = NL.table_rows(rows)
    assert len(out) == 2
    assert out[0]["issue_types"] == ["强制用户使用定向推送功能", "不给权限不让用", "账号注销难"]
    # ② 序号列整列为空（Word 自动编号不落文本）→ 不得启用"无序号即续行"（曾把 41 行并成 1 行）
    rows2 = [
        ["序号", "软件名称", "企业名称", "版本", "版本来源", "所涉问题"],
        ["", "智慧树", "北京环宇万维科技有限公司", "1.0", "官网", "过度索取权限"],
        ["", "ClassIn", "北京翼鸥教育科技有限公司", "3.0", "小米应用商店", "不给权限不让用"],
    ]
    out2 = NL.table_rows(rows2)
    assert len(out2) == 2, out2


def test_r30_merge_app_rows_two_semantics():
    from universal_scraper import name_list as NL
    # ① 一问题一行（身份字段全同）
    rows = [{"seq": 1, "app_name": "A", "company_name": "C", "version": "1", "store_source": "官网",
             "issue_types": ["x"]},
            {"seq": 1, "app_name": "A", "company_name": "C", "version": "1", "store_source": "官网",
             "issue_types": ["y"]}]
    out = NL.merge_app_rows(rows)
    assert len(out) == 1 and out[0]["issue_types"] == ["x", "y"]
    # ② 同 APP 多来源/版本 → 合并且额外来源进 alt_sources（声明数按 APP 计）
    rows2 = [{"seq": 7, "app_name": "B", "company_name": "C1", "version": "1", "store_source": "官网",
              "issue_types": ["x"]},
             {"seq": 7, "app_name": "B", "company_name": "C2", "version": "2", "store_source": "App Store",
              "issue_types": ["x"]}]
    out2 = NL.merge_app_rows(rows2)
    assert len(out2) == 1 and out2[0]["alt_sources"][0]["company_name"] == "C2"


def test_r30_parse_docx_one_issue_per_row_and_cross_table_join(tmp_path):
    from universal_scraper import name_list as NL
    docx = pytest.importorskip("docx")
    d = docx.Document()
    t = d.add_table(rows=4, cols=6)
    hdr = ["序号", "应用名称", "应用开发者", "应用来源", "版本号", "所涉问题"]
    for i, h in enumerate(hdr):
        t.rows[0].cells[i].text = h
    data = [["1", "甲", "甲公司", "应用宝", "1.0", "违规收集个人信息"],
            ["1", "甲", "甲公司", "应用宝", "1.0", "过度索取权限"],
            ["2", "乙", "乙公司", "官网", "2.0", "欺骗误导强迫用户"]]
    for r, vals in enumerate(data, 1):
        for c, v in enumerate(vals):
            t.rows[r].cells[c].text = v
    # 第二张表：没有表头（跨页续表的形态），必须靠首表列映射继续解析
    t2 = d.add_table(rows=1, cols=6)
    for c, v in enumerate(["3", "丙", "丙公司", "小米应用商店", "3.0", "账号注销难"]):
        t2.rows[0].cells[c].text = v
    p = tmp_path / "a.docx"
    d.save(str(p))
    rows = NL.parse_docx(p)
    # 链只做"表→行"：一问题一行是 4 行（合并是 merge_app_rows 的职责，见下）
    assert [r["app_name"] for r in rows] == ["甲", "甲", "乙", "丙"], rows
    merged = NL.merge_app_rows(rows)
    assert [r["app_name"] for r in merged] == ["甲", "乙", "丙"]
    assert merged[0]["issue_types"] == ["违规收集个人信息", "过度索取权限"]


def test_r30_stated_count_sums_sentences_and_excludes_cumulative():
    from universal_scraper import name_list as NL
    # 多附件期：部本级 + 各省分列 → 求和
    t1 = "截至目前，尚有71款APP未完成整改（详见附件1）。各通信管理局检查发现仍有74款APP未完成整改（详见附件2-6）。上述145款APP应在7月26日前完成整改。"
    assert NL.stated_count(t1) == 145
    # 累计口径句必须排除（368 是"提出整改要求"的历史累计，84 才是本批名单）
    t2 = "对发现存在侵害用户权益行为的368款APP提出整改要求。截至目前，尚有84款APP未按要求完成整改（详见附件）。"
    assert NL.stated_count(t2) == 84
    # 下架类汇总复述句排除（5+101=106，末句"共计106款…进行下架"不得重复计）
    t3 = "尚有5款APP未按我部要求完成整改（详见附件1）。各通信管理局检查发现尚有101款APP仍未完成整改（详见附件2-8）。我部组织对上述共计106款APP进行下架。"
    assert NL.stated_count(t3) == 106
    # APP + 内嵌 SDK 两句都要计
    t4 = "尚有107款APP未完成整改。同时，检测过程中发现，13款内嵌第三方软件开发工具包（SDK）存在违规收集用户设备信息的行为（详见附件）。"
    assert NL.stated_count(t4) == 120
    assert NL.stated_count("本页没有任何计数句") is None


def test_r30_declared_fields_and_completeness_by_source_schema():
    from universal_scraper import name_list as NL
    # 下架名单的表头只有四列（无来源/问题）→ 空值不算缺失
    d = NL.declared_fields("序号 应用名称 应用开发者 应用版本")
    assert d == {"app_name", "company_name", "version"}
    d2 = NL.declared_fields("序号 软件名称 企业名称 下架版本")
    assert d2 == {"app_name", "company_name", "version"}
    recs = [{"notice_id": "n1", "source_file": "att_01.docx", "seq": 1, "app_name": "A",
             "company_name": "C", "version": "1", "store_source": "", "issue_types": [],
             "batch_no_total": 1}]
    rep = NL.audit_completeness(recs, {("n1", "att_01.docx"): {"app_name", "company_name", "version"}})
    assert rep["checked"] == 3 and rep["missing"] == 0 and rep["rate"] == 100.0
    # 键缺失 → 按五列从严
    rep2 = NL.audit_completeness(recs, {})
    assert rep2["checked"] == 5 and rep2["missing"] == 2


def test_r30_aggregate_prefers_documents_over_images(tmp_path, monkeypatch):
    """同一名单既有 doc 又有截图 → 只解析 doc（否则重复计数：实测 41 → 55）。"""
    from universal_scraper import name_list as NL
    (tmp_path / "att_01.doc").write_bytes(b"x")
    (tmp_path / "att_01.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    calls = []

    def fake_parse_ex(path, cache_dir=None, llm=None, ocr_cache=None, stats=None):
        calls.append(Path(path).suffix)
        return {"rows": [{"seq": 1, "app_name": "A", "company_name": "C", "version": "1",
                          "store_source": "官网", "issue_types": ["x"]}],
                "chain": "fake", "error": ""}

    monkeypatch.setattr(NL, "parse_attachment_ex", fake_parse_ex)
    monkeypatch.setattr(NL, "att_header_text", lambda p, c=None: "序号 应用名称 应用开发者 应用版本 所涉问题")
    rows, src = NL.aggregate([str(tmp_path / "att_01.doc"), str(tmp_path / "att_01.png")])
    assert calls == [".doc"] and len(rows) == 1 and "att_01.doc" in src


def test_r30_aggregate_marks_resurvey_list_and_dedupes_twin_uploads(tmp_path, monkeypatch):
    """① 表头含"复测"的附件打 list_kind（不计入声明数）；② 名称集合重叠 ≥90% 的
    二次上传版本只留一版（保留"名称粘连版本号"更少的那版）。"""
    from universal_scraper import name_list as NL
    (tmp_path / "att_01.docx").write_bytes(b"x")
    (tmp_path / "att_02.docx").write_bytes(b"x")
    (tmp_path / "att_03.docx").write_bytes(b"x")
    base = [{"seq": i, "app_name": f"甲{i}", "company_name": "C", "version": "1",
             "store_source": "官网", "issue_types": ["x"]} for i in range(1, 11)]

    def fake_parse_ex(path, cache_dir=None, llm=None, ocr_cache=None, stats=None):
        p = Path(path)
        if p.name == "att_01.docx":
            rows = [dict(r) for r in base]
        elif p.name == "att_02.docx":                    # 二次上传版本：1/10 行名称粘连版本号
            rows = [dict(r) for r in base]
            rows[0]["app_name"] = "甲11"
        else:
            rows = [{"seq": 9, "app_name": "乙", "company_name": "C", "version": "1",
                     "store_source": "官网", "issue_types": ["x"]}]
        return {"rows": rows, "chain": "fake", "error": ""}

    monkeypatch.setattr(NL, "parse_attachment_ex", fake_parse_ex)
    monkeypatch.setattr(NL, "att_header_text",
                        lambda p, c=None: "序号 应用名称 应用开发者 复测应用版本 所涉问题"
                        if Path(p).name == "att_03.docx" else "序号 应用名称 应用开发者 应用版本 所涉问题")
    rows, src = NL.aggregate([str(tmp_path / n) for n in
                              ("att_01.docx", "att_02.docx", "att_03.docx")])
    names = [r["app_name"] for r in rows]
    assert names.count("甲1") == 1 and "甲11" not in names      # 名称集合重叠 90% → 留干净的一版
    assert len(rows) == 11 and src.count("+") == 1
    assert rows[-1]["list_kind"].startswith("复测名单")


def test_r30_audit_counts_tolerance():
    from universal_scraper import name_list as NL
    # 14 vs 15 = 6.7%、100 vs 95 = 5.3%（5% 边界外）都算超差；41 vs 41 不算
    assert NL.audit_counts([(1, 41, 41), (2, 14, 15), (3, 100, 95)]) == [(2, 14, 15), (3, 100, 95)]
    assert NL.audit_counts([(1, 41, None)]) == []


# ---------------- 审查轮（四路 agent）修复的回归 ----------------

def test_r30_stated_count_chinese_numerals():
    """中文数字必须正确进位（旧实现：一百零六→6、一百四十五→1045）。"""
    from universal_scraper import name_list as NL
    assert NL.stated_count("共发现一百零六款APP未完成整改。") == 106
    assert NL.stated_count("共发现一百四十五款APP未完成整改。") == 145
    assert NL.stated_count("尚有十二款APP未完成整改。") == 12
    assert NL.stated_count("尚有二十款APP未完成整改。") == 20


def test_r30_stated_count_sums_within_one_sentence():
    """同一句并列两个口径要都算；"共计"只在与"上述"同句时排除。"""
    from universal_scraper import name_list as NL
    assert NL.stated_count("尚有71款APP未完成整改，各通信管理局检查发现仍有74款APP未完成整改。") == 145
    assert NL.stated_count("本批共计84款APP未按要求完成整改（详见附件）。") == 84
    assert NL.stated_count("我部组织对上述共计106款APP进行下架处理。") is None


def test_r30_table_rows_recomputes_seq_ok_per_segment():
    """后段表头没有序号列时不得整段塌成一条（C1：6 行曾塌成 2 行）。"""
    from universal_scraper import name_list as NL
    rows = [
        ["序号", "应用名称", "应用开发者", "版本", "版本来源", "所涉问题"],
        ["1", "甲", "甲公司", "1.0", "官网", "违规收集个人信息"],
        ["", "", "", "", "", "过度索取权限"],
        ["应用名称", "应用开发者", "所涉问题"],                  # 第二段：无序号列
        ["乙", "乙公司", "欺骗误导强迫用户"],
        ["丙", "丙公司", "账号注销难"],
    ]
    stats = {}
    out = NL.table_rows(rows, stats)
    assert [r["app_name"] for r in out] == ["甲", "乙", "丙"], out
    assert out[0]["issue_types"] == ["违规收集个人信息", "过度索取权限"]
    assert stats["segments"] == 2


def test_r30_table_rows_fragment_unmapped_column_not_swallowed():
    """续行内容落在未映射列（备注）时必须留痕（M1：旧实现连痕迹都没有）。"""
    from universal_scraper import name_list as NL
    rows = [
        ["序号", "应用名称", "应用开发者", "版本", "版本来源", "所涉问题", "备注"],
        ["1", "甲", "甲公司", "1.0", "官网", "违规收集个人信息", ""],
        ["", "", "", "", "", "", "需要补充材料"],
    ]
    stats = {}
    out = NL.table_rows(rows, stats)
    assert len(out) == 1 and out[0]["issue_types"] == ["违规收集个人信息"]   # 不污染问题列
    drops = [d for d in stats["dropped"] if d[1] == "fragment_unmapped"]
    assert drops and "需要补充材料" in " ".join(drops[0][2])               # 但必须留痕


def test_r30_table_rows_skips_total_and_section_rows():
    """合计/分节行不得并进上一条（M2：'合计 41款' 曾进 issue_types）。"""
    from universal_scraper import name_list as NL
    rows = [
        ["序号", "应用名称", "应用开发者", "版本", "版本来源", "所涉问题"],
        ["1", "甲", "甲公司", "1.0", "官网", "违规收集个人信息"],
        ["合计", "41款", "", "", "", ""],
        ["（一）未整改名单", "", "", "", "", ""],
    ]
    stats = {}
    out = NL.table_rows(rows, stats)
    assert len(out) == 1 and out[0]["issue_types"] == ["违规收集个人信息"], out
    reasons = [d[1] for d in stats["dropped"]]
    assert "total_row" in reasons and "section" in reasons


def test_r30_ver_like_excludes_cjk():
    """版本形判定不得把中文问题项当版本号（M3：问题列曾被搬空）。"""
    from universal_scraper import name_list as NL
    assert NL.VER_LIKE.fullmatch("4.5.17.7")
    assert not NL.VER_LIKE.fullmatch("1.不给权限不让用")
    rows = [
        ["序号", "应用名称", "应用开发者", "所涉问题"],
        ["1", "甲", "甲公司", "1.不给权限不让用"],
    ]
    out = NL.table_rows(rows)
    assert not out[0].get("version") and out[0]["issue_types"] == ["1.不给权限不让用"]


def test_r30_parse_inline_html_rowspan_and_section():
    """内联表：rowspan 首列（应用类别）不得让整行左移；分节行不产生记录。"""
    from universal_scraper import name_list as NL
    html = ("<table>"
            "<tr><td>应用类别</td><td>序号</td><td>应用名称</td><td>应用开发者</td>"
            "<td>应用来源</td><td>应用版本</td><td>所涉问题</td></tr>"
            "<tr><td rowspan='2'>出行服务类</td><td>1</td><td>甲</td><td>甲公司</td>"
            "<td>应用宝</td><td>1.0</td><td>违规收集个人信息<br/>过度索取权限</td></tr>"
            "<tr><td>2</td><td>乙</td><td>乙公司</td><td>官网</td><td>2.0</td><td>账号注销难</td></tr>"
            "<tr><td colspan='7'>（一）未整改名单</td></tr>"
            "</table>")
    out = NL.parse_inline_html(html)
    assert [r["app_name"] for r in out] == ["甲", "乙"], out
    assert out[0]["company_name"] == "甲公司"
    assert out[0]["issue_types"] == ["违规收集个人信息", "过度索取权限"]
    assert out[1]["store_source"] == "官网"


def test_r30_assemble_doc_rows_textutil_fallback():
    """textutil 扁平流兜底：名称/企业/版本/来源/问题 逐字段归位。"""
    from universal_scraper import name_list as NL
    cells = ["1", "甲", "甲公司", "1.0", "官网", "违规收集个人信息",
             "2", "乙", "乙公司", "2.0", "应用宝", "账号注销难"]
    out = NL._assemble_doc_rows(cells)
    assert [r["app_name"] for r in out] == ["甲", "乙"]
    assert out[1]["store_source"] == "应用宝" and out[1]["version"] == "2.0"


def test_r30_row_from_cells_without_header_and_category_column():
    from universal_scraper import name_list as NL
    assert NL.table_rows([["1", "甲", "甲公司", "1.0", "官网", "违规收集个人信息"]])[0]["app_name"] == "甲"
    out = NL.table_rows([["出行服务类", "2", "乙", "乙公司", "2.0", "官网", "账号注销难"]])
    assert out and out[0]["app_name"] == "乙" and out[0]["seq"] == 2


def test_r30_lift_version_from_issue_column():
    from universal_scraper import name_list as NL
    rows = [
        ["序号", "应用名称", "应用开发者", "所涉问题"],
        ["1", "甲", "甲公司", "违规收集个人信息；4.5.17.7"],
    ]
    out = NL.table_rows(rows)
    assert out[0]["version"] == "4.5.17.7" and "4.5.17.7" not in out[0]["issue_types"]


def test_r30_is_header_row_tolerates_one_unknown_column():
    from universal_scraper import name_list as NL
    assert NL.is_header_row(["序号", "应用名称", "应用开发者", "应用版本", "所涉问题", "备注"])
    assert NL.header_map(["序号", "应用名称", "应用开发者", "应用版本", "所涉问题", "备注"])["app_name"] == 1


def test_r30_parse_attachment_ex_reports_errors(tmp_path):
    """不支持类型 / 损坏 docx / 大写扩展名 都必须给出可定位的 error。"""
    from universal_scraper import name_list as NL
    txt = tmp_path / "a.txt"
    txt.write_text("x")
    d1 = NL.parse_attachment_ex(txt)
    assert d1["chain"] == "none" and "不支持" in d1["error"]
    bad = tmp_path / "att_01.docx"
    bad.write_bytes(b"<!DOCTYPE html><html>x</html>")
    d2 = NL.parse_attachment_ex(bad)
    assert d2["rows"] == [] and d2["error"] and d2["chain"] == "docx"
    upper = tmp_path / "att_02.PNG"                     # 大写扩展名走同一条链
    upper.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    assert NL.parse_attachment_ex(upper)["chain"] == "img"


def test_r30_aggregate_ex_all_failed_has_diagnostics(tmp_path):
    """全失败时 src 也必须列出缺口（否则 wrapper 的 (rows, src) 无痕）。"""
    from universal_scraper import name_list as NL
    (tmp_path / "att_01.txt").write_text("x")
    d = NL.aggregate_ex([str(tmp_path / "att_01.txt")])
    assert d["rows"] == [] and "att_01.txt:0" in d["src"]
    assert d["files"][0]["chain"] == "none" and d["files"][0]["error"]


def test_r30_aggregate_ex_inline_fallback_and_doc_first_note(tmp_path, monkeypatch):
    from universal_scraper import name_list as NL
    (tmp_path / "att_01.doc").write_bytes(b"x")
    (tmp_path / "att_01.png").write_bytes(b"\x89PNG")

    def fake_empty(path, cache_dir=None, llm=None, ocr_cache=None, stats=None):
        return {"rows": [], "chain": "fake", "error": "空"}

    monkeypatch.setattr(NL, "parse_attachment_ex", fake_empty)
    html = ("<table><tr><td>序号</td><td>应用名称</td><td>应用开发者</td><td>应用版本</td></tr>"
            "<tr><td>1</td><td>甲</td><td>甲公司</td><td>1.0</td></tr></table>")
    d = NL.aggregate_ex([str(tmp_path / "att_01.doc"), str(tmp_path / "att_01.png")],
                        inline_html=html)
    assert len(d["rows"]) == 1 and d["src"] == "inline"

    def fake_ok(path, cache_dir=None, llm=None, ocr_cache=None, stats=None):
        return {"rows": [{"seq": 1, "app_name": "甲", "company_name": "甲公司", "version": "1.0",
                          "store_source": "官网", "issue_types": ["x"]}],
                "chain": "fake", "error": ""}

    monkeypatch.setattr(NL, "parse_attachment_ex", fake_ok)
    monkeypatch.setattr(NL, "att_header_text", lambda p, c=None: "序号 应用名称 应用开发者 应用版本")
    d2 = NL.aggregate_ex([str(tmp_path / "att_01.doc"), str(tmp_path / "att_01.png")])
    assert len(d2["rows"]) == 1 and any("文档优先" in n for n in d2["notes"])


def test_r30_audit_completeness_unreadable_and_empty():
    from universal_scraper import name_list as NL
    assert NL.declared_fields("") is None                      # 读不到 ≠ 没有列
    recs = [{"notice_id": "n1", "source_file": "a.docx", "seq": 1, "app_name": "A",
             "company_name": "C", "version": "1", "store_source": "", "issue_types": [],
             "batch_no_total": 1}]
    rep = NL.audit_completeness(recs, {("n1", "a.docx"): None})
    assert rep["checked"] == 5 and rep["missing"] == 2
    assert rep["sources_unreadable"] == [("n1", "a.docx")]
    empty = NL.audit_completeness([], {})
    assert empty["checked"] == 0 and empty["rate"] is None and empty["reason"] == "empty_records"


def test_r30_audit_counts_ex_separates_no_stated():
    from universal_scraper import name_list as NL
    r = NL.audit_counts_ex([(1, 41, 41), (2, 41, None), (3, 14, 15)])
    assert r["checked"] == 2 and r["bad"] == [(3, 14, 15)] and r["no_stated"] == [2]


def test_r30_ocr_cache_does_not_freeze_failures(tmp_path, monkeypatch):
    """空结果不得写缓存；命中空缓存视为未命中并重试（C3）。"""
    from universal_scraper import name_list as NL, local_ocr as L
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw
    img = Image.new("L", (120, 200), 255)
    dr = ImageDraw.Draw(img)
    for y in (10, 60, 110, 160):
        dr.line([(0, y), (119, y)], fill=0)
    p = tmp_path / "t.png"
    img.save(p)
    cache = tmp_path / "cache"
    monkeypatch.setattr(L, "row_bands", lambda path, **kw: [(11, 59), (61, 109), (111, 159)])
    monkeypatch.setattr(L, "available", lambda: True)
    monkeypatch.setattr(L, "ocr_image", lambda src: [])       # 交叉核验走 no_alt，不碰 Vision

    class FakeLLM:
        def __init__(self, seq):
            self.seq, self.calls = list(seq), 0

        def vision(self, prompt, url):
            self.calls += 1
            return self.seq.pop(0) if self.seq else ""

    bad = FakeLLM(["prose", "prose", "prose"])
    rows1 = NL.parse_images([p], llm=bad, ocr_cache=cache)
    assert rows1 == [] and (not cache.exists() or list(cache.glob("*.json")) == [])
    good = FakeLLM(['{"序号":"1","应用名称":"甲","应用开发者":"甲公司","应用来源":"官网",'
                    '"应用版本":"1.0","所涉问题":"违规收集个人信息"}'] * 4)
    rows2 = NL.parse_images([p], llm=good, ocr_cache=cache)
    assert len(rows2) == 3 and good.calls >= 3          # 未命中缓存 → 真的重试了


def test_r30_parse_images_marks_unavailable_not_differ(tmp_path, monkeypatch):
    """本地引擎不可用 → ocr_check="unavailable"，不得伪装成 differ（C2）。"""
    from universal_scraper import name_list as NL, local_ocr as L
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw
    img = Image.new("L", (120, 200), 255)
    dr = ImageDraw.Draw(img)
    for y in (10, 60, 110, 160):
        dr.line([(0, y), (119, y)], fill=0)
    p = tmp_path / "t.png"
    img.save(p)
    monkeypatch.setattr(L, "row_bands", lambda path, **kw: [(11, 59), (61, 109), (111, 159)])
    monkeypatch.setattr(L, "available", lambda: False)
    monkeypatch.setattr(L, "unavailable_reason", lambda: "测试：引擎不可用")

    class FakeLLM:
        def vision(self, prompt, url):
            return '{"序号":"1","应用名称":"甲","应用开发者":"甲公司","应用来源":"官网","应用版本":"1.0"}'

    stats = {}
    rows = NL.parse_images([p], llm=FakeLLM(), use_local_ocr=True, stats=stats)
    assert rows and all(r.get("ocr_check") == "unavailable" for r in rows)
    assert stats.get("ocr_unavailable") == "测试：引擎不可用"


def test_r30_cross_check_no_alt_for_missing_second_engine():
    from universal_scraper import local_ocr as L
    assert L.cross_check_verdict("北京米拓世纪科技有限公司", "") == "no_alt"
    assert L.cross_check_verdict("", "") == "no_alt"
    assert isinstance(L.unavailable_reason(), str)


def test_r30_download_targets_idempotent_after_sniff_rename(tmp_path):
    """嗅探改名后的文件第二轮必须跳过（M12：无扩展名 URL 曾每轮重下）。"""
    from universal_scraper import attachments as AT

    class _C:
        def __init__(self, mapping):
            self.mapping, self.hits = mapping, []

        def get(self, url, **kw):
            self.hits.append(url)
            body = self.mapping.get(url, b"")
            return {"body": body, "ok": bool(body), "status": 200 if body else 404}

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    url = "https://h/cms_files/part/9"
    c = _C({url: png})
    r1 = AT.download_targets(c, [url], tmp_path / "d", prefix="att")
    assert Path(r1["saved"][0]).name == "att_01.png"
    c.hits.clear()
    r2 = AT.download_targets(c, [url], tmp_path / "d", prefix="att")
    assert len(r2["skipped"]) == 1 and c.hits == []


def test_r30_download_targets_rejects_bom_html_and_cleans_fake(tmp_path):
    from universal_scraper import attachments as AT

    class _C:
        def __init__(self, mapping):
            self.mapping = mapping

        def get(self, url, **kw):
            body = self.mapping.get(url, b"")
            return {"body": body, "ok": bool(body), "status": 200 if body else 404}

    url = "https://h/cms_files/f/attach/2.pdf"
    c = _C({url: b"\xef\xbb\xbf<!DOCTYPE html><html>404</html>"})
    r = AT.download_targets(c, [url], tmp_path / "d", prefix="att")
    assert r["saved"] == [] and "HTML" in r["failed"][0]["error"]
    d = tmp_path / "d2"
    d.mkdir()
    (d / "att_01.pdf").write_bytes(b"\xef\xbb\xbf<!DOCTYPE html>")
    r2 = AT.download_targets(_C({}), [url], d, prefix="att")
    assert r2["failed"] and not (d / "att_01.pdf").exists()


def test_r30_sniff_prefers_worddocument_and_skips_static_assets():
    from universal_scraper import attachments as AT
    ole = b"\xd0\xcf\x11\xe0" + b"\x00" * 40 + b"WordDocument" + b"\x00" * 10 + b"Bookmark"
    assert AT.sniff_ext(ole) == ".doc"
    atts, _ = AT.extract_targets(
        '<a href="/static/js/files/loader.js?v=3">x</a>'
        '<a href="/cms_files/f/attach/a.docx">y</a>', "https://h")
    assert atts == ["https://h/cms_files/f/attach/a.docx"]


def test_r30_run_tool_strifies_before_whitelist():
    from universal_scraper import core

    class Evil(str):
        def __str__(self):
            return "/bin/echo"

    r = core.run_tool([Evil("soffice"), "x"])
    assert r["ok"] is False and "白名单" in r["err"]
    assert core.run_tool([["soffice"]])["ok"] is False          # 不可哈希参数不抛异常


def test_r30_doc_html_cache_key_includes_parent_dir(tmp_path, monkeypatch):
    """同名附件（att_01.doc）在不同批次目录下不得串用缓存（M4）。"""
    from universal_scraper import name_list as NL, core

    def fake_run_tool(args, timeout=180):
        outdir = Path(args[args.index("--outdir") + 1])
        src = Path(args[-1])
        (outdir / f"{src.stem}.html").write_text(
            f"<table><tr><td>{src.stem}-{src.parent.name}</td></tr></table>", encoding="utf-8")
        return {"ok": True, "out": "", "err": "", "code": 0}

    monkeypatch.setattr(core, "run_tool", fake_run_tool)
    d1, d2 = tmp_path / "batchA", tmp_path / "batchB"
    d1.mkdir()
    d2.mkdir()
    (d1 / "att_01.doc").write_bytes(b"x")
    (d2 / "att_01.doc").write_bytes(b"x")
    h1 = NL._doc_html(d1 / "att_01.doc", tmp_path / "cache")
    h2 = NL._doc_html(d2 / "att_01.doc", tmp_path / "cache")
    assert h1 != h2 and "batchA" in h1 and "batchB" in h2
    assert len(list((tmp_path / "cache").glob("*.html"))) == 2


def test_r30_write_jsonl_utf8_and_count(tmp_path):
    from universal_scraper import name_list as NL
    rows = [{"app_name": "甲", "issue_types": ["违规收集个人信息"]}]
    p = tmp_path / "sub" / "out.jsonl"
    n = NL.write_jsonl(rows, p)
    raw = p.read_text(encoding="utf-8")
    assert n == 1 and "甲" in raw and "\\u" not in raw


def test_r30_fetch_json_top_level_non_object_has_body_head():
    from universal_scraper import core

    class _R:
        def get(self, k, default=None):
            return {"body": b"[1, 2, 3]", "ok": True, "status": 200}.get(k, default)

    class _C:
        def get(self, url, **kw):
            return _R()

    with pytest.raises(RuntimeError) as e:
        core.fetch_json(_C(), "http://x/y")
    assert "[1, 2, 3]" in str(e.value)


def test_r30_xlsx_chain_via_pdf_table(tmp_path):
    """xlsx 名单走 pdf_table.parse_xlsx（行是"表头→值"字典，按列名映射）。"""
    from universal_scraper import name_list as NL
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["序号", "应用名称", "应用开发者", "应用来源", "应用版本", "所涉问题"])
    ws.append([1, "甲", "甲公司", "应用宝", "1.0", "违规收集个人信息"])
    p = tmp_path / "att_01.xlsx"
    wb.save(p)
    d = NL.parse_attachment_ex(p)
    assert d["chain"] == "xlsx" and len(d["rows"]) == 1
    assert d["rows"][0]["app_name"] == "甲" and d["rows"][0]["store_source"] == "应用宝"
