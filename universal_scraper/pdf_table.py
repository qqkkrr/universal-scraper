#!/usr/bin/env python3
"""📄 通用 PDF 解析模块（v1）：任何 PDF → 结构化表格行 / 纯文本。

设计目标（“万能”而非“只认识某站”）：
  1. 网格线表格 PDF（政策目录、报表、招标清单、统计年鉴等）
     → pdfplumber 抽表 + 自适应表头 → 直接输出结构化行（不烧 LLM）
  2. 纯文字 PDF（红头文件、通知全文、论文）
     → pdfminer 全文抽取 → 交 LLM 结构化（旧路径兜底）
  3. 自动处理：跨页多表、单元格换行、无表头表格、附件链接提取、相对路径
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# 常见表头关键词：命中 ≥2 个即认为是表头行
HEADER_KEYWORDS = (
    "序号", "编号", "名称", "标题", "单位", "部门", "机构", "类别", "类型",
    "日期", "时间", "金额", "价格", "数量", "状态", "备注", "说明", "内容",
    "地址", "电话", "联系人", "来源", "依据", "文件", "文号", "职业", "等级",
    "姓名", "性别", "年龄", "学历", "专业", "成绩", "得分", "排名", "附件",
)

# 列名 → 换行拼接策略：依据/法规用分号；部门/单位用顿号；其余用空格
_JOIN_BY_COL = {
    "；": ("依据", "法规", "条例", "规定", "办法", "通知", "文号"),
    "、": ("部门", "单位", "机构", "联系人", "地址"),
}


def is_pdf_url(url: str) -> bool:
    u = (url or "").strip().lower().split("?")[0]
    return u.endswith(".pdf")


def extract_pdf_links(html: str, base_url: Optional[str] = None) -> List[str]:
    """从 HTML 提取 PDF/Office 附件链接（去重、补全相对路径）。"""
    from urllib.parse import urljoin
    pats = [
        r'(?:href|src|fileurl|data-url)=["\']([^"\']*?\.(?:pdf|docx?|xlsx?|xls)(?:[?#][^"\']*)?)["\']',
        r'<a[^>]+href=["\']([^"\']+?\.(?:pdf|docx?|xlsx?|xls)(?:[?#][^"\']*)?)["\']',
    ]
    out: List[str] = []
    for pat in pats:
        for m in re.finditer(pat, html or "", re.I):
            u = m.group(1)
            if u.startswith(("data:", "javascript:", "#", "mailto:")):
                continue
            if base_url and not u.startswith(("http://", "https://")):
                try:
                    u = urljoin(base_url, u)
                except Exception:
                    continue
            out.append(u)
    # 去重保序
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def download_pdf(url: str, proxy: Optional[str] = None, timeout: int = 60,
                 headers: Optional[Dict[str, str]] = None) -> Path:
    """下载 PDF 到临时文件，返回路径；失败抛 RuntimeError。"""
    import requests
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15",
    }
    if headers:
        hdrs.update(headers)
    r = requests.get(url, timeout=timeout, verify=True, headers=hdrs,
                     proxies={"http": proxy, "https": proxy} if proxy else None)
    r.raise_for_status()
    if len(r.content) < 200 or not r.content.lstrip().startswith(b"%PDF"):
        raise RuntimeError(f"下载内容不是有效 PDF（{len(r.content)} 字节）: {url}")
    fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="us_pdf_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(r.content)
    except Exception:
        os.unlink(tmp)
        raise
    return Path(tmp)


def _clean_cell(cell: str, col_name: str = "", header: bool = False) -> str:
    """单元格清理：去空白、按列类型智能拼接换行。"""
    c = (cell or "").replace("\u3000", " ")
    if not c.strip():
        return ""
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in c.split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    if len(lines) == 1:
        return lines[0]
    if header:
        return "".join(lines)  # 表头单元格：直接拼（"实施部门\n（单位）"→"实施部门（单位）"）
    # 部门/单位类：顿号拼接（多单位并列）
    if any(k in (col_name or "") for k in _JOIN_BY_COL["、"]):
        return "、".join(lines)
    # 其余：下一行以《【（( 开头视为新条目加分号，否则是续行直接拼接
    out = lines[0]
    for ln in lines[1:]:
        out += ("；" if re.match(r"^[《【（(]", ln) else "") + ln
    return re.sub(r"[；]+", "；", out).strip("；")



def _merge_sparse_cols(table: List[List[Optional[str]]], drop: int,
                        hdr: Optional[List[str]] = None) -> List[List[Optional[str]]]:
    """合并 table 中多余的 drop 列到左邻列（处理 pdfplumber 拆列）。

    优先合并“相邻同名列”（如表头"职业资格名称"被拆成组名+子项两列）；
    否则合并空值最多的列。
    """
    if drop <= 0 or not table:
        return table
    ncols = max((len(r) for r in table if r), default=0)
    if ncols <= 1:
        return table
    empties = [0] * ncols
    for row in table:
        for ci in range(min(ncols, len(row))):
            if not (row[ci] or "").strip():
                empties[ci] += 1
    for _ in range(min(drop, ncols - 1)):
        # 优先：相邻同名列（hdr[ci]==hdr[ci-1]）
        ci = -1
        if hdr:
            for j in range(1, len(hdr)):
                if j < ncols and hdr[j] == hdr[j - 1]:
                    ci = j
                    break
        if ci < 0:
            ci = max(range(ncols), key=lambda i: empties[i])
            if ci == 0:
                ci = 1
        for row in table:
            if ci < len(row):
                left = row[ci - 1] or ""
                right = row[ci] or ""
                row[ci - 1] = (left + " " + right).strip() if (left and right) else (left or right)
                del row[ci]
        del empties[ci]
        ncols -= 1
    return table


STRONG_HEADER_KWS = ("序号", "编号", "名称", "标题", "日期", "时间", "金额", "单位", "部门", "类别", "备注", "文号")


def _looks_like_header(row: List[str]) -> bool:
    cells = [(c or "").strip() for c in row if (c or "").strip()]
    if len(cells) < 2:
        return False
    # 表头是短词：任何单元格都不该是长句/带书名号
    if any(len(c) > 12 or "《" in c or "。" in c for c in cells):
        return False
    strong = sum(1 for c in cells if any(k in c for k in STRONG_HEADER_KWS))
    weak = sum(1 for c in cells if any(k in c for k in HEADER_KEYWORDS))
    return strong >= 1 and weak >= 2


def extract_tables_from_pdf(path: str) -> List[Dict[str, Any]]:
    """pdfplumber 逐页抽表 → 自适应表头 → 结构化行。

    返回 [{列名: 值, "_page": n, "_table": m}, ...]；表头找不到时列名为 列1..列N。
    通用增强：
      - 表头单元格换行归一（"序\n号"→"序号"）
      - 跨页继承表头（后续页无表头且列数一致时复用上一张表表头）
      - "序号/编号"列空值继承上一行（合并单元格常见形态）
    """
    import pdfplumber
    out: List[Dict[str, Any]] = []
    last_header: List[str] = []
    last_inherit: List[int] = []  # 表头中“继承左邻”的空列位置（合并单元格）
    prev_seq = ""          # 跨页序号继承
    ctx: Dict[str, str] = {}  # 跨页上下文（类别/部门/备注）
    with pdfplumber.open(path) as pdf:
        for pi, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables()
            for ti, table in enumerate(tables, 1):
                if not table:
                    continue
                # 找表头行（前 4 行内），列名做空白归一
                hdr_idx, hdr = -1, []
                for i, row in enumerate(table[:4]):
                    cells = [_clean_cell(c, "", header=True) for c in row]
                    if _looks_like_header(cells):
                        hdr_idx = i
                        # 表头空单元格 = 合并单元格 → 继承左邻列名
                        hdr = []
                        inherit_pos: List[int] = []
                        for j, c in enumerate(cells):
                            name = re.sub(r"\s+", "", c or "").strip()
                            if not name and hdr:
                                name = hdr[-1]  # 合并单元格：继承左邻
                                inherit_pos.append(j)
                            elif not name:
                                name = f"列{j + 1}"
                            hdr.append(name)
                        last_header = hdr
                        last_inherit = inherit_pos
                        break
                if hdr_idx < 0:
                    ncols = max((len(r) for r in table if r), default=0)
                    if last_header and len(last_header) != ncols:
                        if len(last_header) > ncols:
                            # 尾部缺列/合并单元格列被省略 → 先删继承列，再截断
                            hdr = list(last_header)
                            for j in sorted(last_inherit, reverse=True):
                                if len(hdr) > ncols:
                                    del hdr[j]
                            if len(hdr) > ncols:
                                hdr = hdr[:ncols]
                        else:
                            # 列数不一致（如竖排文字被拆列）：合并“空值最多”的列到左邻，直到列数对齐
                            table = _merge_sparse_cols(table, ncols - len(last_header), hdr=last_header)
                            ncols = max((len(r) for r in table if r), default=0)
                            hdr = last_header if last_header and len(last_header) == ncols else None
                        if not hdr:
                            hdr = [f"列{i + 1}" for i in range(ncols)]
                    elif last_header and len(last_header) == ncols:
                        hdr = last_header  # 跨页继承表头
                    else:
                        hdr = [f"列{i + 1}" for i in range(ncols)]
                    hdr_idx = -1
                # 数据行（含序号列继承 + 跨页上下文继承）
                seq_cols = [ci for ci, c in enumerate(hdr) if any(k in c for k in ("序号", "编号"))]
                inherit_cols = [ci for ci, c in enumerate(hdr)
                                if any(k in c for k in ("类别", "部门", "单位", "备注", "依据"))]
                for row in table[hdr_idx + 1:]:
                    if not row or not any((c or "").strip() for c in row):
                        continue
                    rec: Dict[str, Any] = {"_page": pi, "_table": ti}
                    has_seq = False
                    for ci, cell in enumerate(row):
                        if ci >= len(hdr):
                            continue
                        col = hdr[ci] or f"列{ci + 1}"
                        val = _clean_cell(cell, col)
                        if ci in seq_cols:
                            if val:
                                prev_seq = val
                                has_seq = True
                            else:
                                val = prev_seq
                        # 子行（无序号）缺失类别/部门/备注 → 继承上一行（跨页）
                        if not has_seq and not val and ci in inherit_cols and ctx.get(col):
                            val = ctx[col]
                        if val:
                            if col in rec and rec[col]:
                                rec[col] = rec[col] + " " + val  # 合并单元格延续
                            else:
                                rec[col] = val
                    if has_seq:
                        # 主行：更新上下文（供后续子行继承）
                        for ci in inherit_cols:
                            if ci < len(hdr):
                                col = hdr[ci]
                                if rec.get(col):
                                    ctx[col] = rec[col]
                    if any(v for k, v in rec.items() if not k.startswith("_")):
                        out.append(rec)
    return out


def normalize_field_names(rows: List[Dict[str, Any]], fields: List[str]) -> List[Dict[str, Any]]:
    """把表头列名映射到任务字段：字段关键词匹配列名 → 重命名为任务字段名。

    例如 fields=["职业名称","资格类别"]，表头列"职业资格名称"命中"职业"→重命名。
    无法匹配的列保留原名（避免丢数据）。
    """
    if not fields or not rows:
        return rows
    mapping: Dict[str, str] = {}
    used: set = set()
    for f in fields:
        fk = re.sub(r"[（(].*?[)）]", "", f or "")
        best_col, best_score = "", 0
        for col in rows[0].keys():
            if col.startswith("_") or col in used:
                continue
            ck = re.sub(r"[（(].*?[)）]", "", col)
            score = 0
            for ch in fk:
                if ch and ch in ck:
                    score += 1
            # 全等优先
            if ck == fk:
                score = 100
            if score > best_score:
                best_col, best_score = col, score
        if best_col and best_score >= 1:
            mapping[best_col] = f
            used.add(best_col)
    if not mapping:
        return rows
    out = []
    for r in rows:
        nr = {k: v for k, v in r.items() if k.startswith("_")}
        for col, val in r.items():
            if col.startswith("_"):
                continue
            nr[mapping.get(col, col)] = val
        out.append(nr)
    return out


def parse_pdf_auto(path: str, fields: Optional[List[str]] = None,
                   min_table_rows: int = 2) -> Dict[str, Any]:
    """PDF 万能解析入口。

    返回:
      {"kind": "table", "rows": [...], "tables": n}   表格型，直接可用
      {"kind": "text",  "text": "...", "tables": 0}   文字型，需 LLM 结构化
      {"kind": "error", "error": "..."}               解析失败
    """
    try:
        rows = extract_tables_from_pdf(path)
        # 表格型判定：至少一个表有 ≥min_table_rows 行数据
        if rows:
            by_table: Dict[tuple, int] = {}
            for r in rows:
                k = (r.get("_page"), r.get("_table"))
                by_table[k] = by_table.get(k, 0) + 1
            if max(by_table.values(), default=0) >= min_table_rows:
                if fields:
                    rows = normalize_field_names(rows, fields)
                return {"kind": "table", "rows": rows, "tables": len(by_table)}
    except Exception:
        # pdfplumber 失败不致命，回退文本
        pass
    try:
        from pdfminer.high_level import extract_text
        txt = extract_text(path) or ""
        txt = re.sub(r"[ \t]+", " ", txt)
        txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
        if len(txt) < 30:
            return {"kind": "error", "error": "PDF 无有效文本（可能是扫描件，需 OCR）"}
        return {"kind": "text", "text": txt, "tables": 0}
    except Exception as e:
        return {"kind": "error", "error": f"PDF 解析失败: {type(e).__name__}: {e}"}


def is_attachment_url(url: str) -> bool:
    u = (url or "").strip().lower().split("?")[0]
    return u.endswith((".pdf", ".xlsx", ".xls", ".docx", ".doc"))


def parse_xlsx(path: str, fields: Optional[List[str]] = None) -> Dict[str, Any]:
    """Excel 附件解析：第一个工作表 → 表头行 → 行字典。"""
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header = None
    rows: List[Dict[str, Any]] = []
    for r in rows_iter:
        vals = ["" if v is None else str(v).strip() for v in r]
        if not any(vals):
            continue
        if header is None:
            # 第一行非空即表头（含关键词更好，但不强求）
            header = [v or f"列{i + 1}" for i, v in enumerate(vals)]
            continue
        rec: Dict[str, Any] = {}
        for i, v in enumerate(vals):
            if i < len(header) and v:
                rec[header[i]] = v
        if rec:
            rows.append(rec)
    wb.close()
    if not rows:
        return {"kind": "error", "error": "Excel 无数据行"}
    if fields:
        rows = normalize_field_names(rows, fields)
    return {"kind": "table", "rows": rows, "tables": 1}


def download_attachment(url: str, proxy: Optional[str] = None, timeout: int = 90) -> Path:
    """下载附件（PDF/Excel/Word）到临时文件，校验非空。"""
    import requests
    hdrs = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15"}
    r = requests.get(url, timeout=timeout, verify=True, headers=hdrs,
                     proxies={"http": proxy, "https": proxy} if proxy else None)
    r.raise_for_status()
    if len(r.content) < 50:
        raise RuntimeError(f"附件内容过小（{len(r.content)} 字节）: {url}")
    ext = os.path.splitext(url.split("?")[0])[1].lower() or ".bin"
    if not ext.startswith("."):
        ext = "." + ext
    fd, tmp = tempfile.mkstemp(suffix=ext, prefix="us_att_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(r.content)
    except Exception:
        os.unlink(tmp)
        raise
    return Path(tmp)


def attachment_to_rows(url: str, proxy: Optional[str] = None,
                       fields: Optional[List[str]] = None,
                       timeout: int = 90) -> Dict[str, Any]:
    """附件万能解析入口：按扩展名分派 PDF/Excel，返回 {kind, rows|text|error}。"""
    low = (url or "").split("?")[0].lower()
    try:
        fp = download_attachment(url, proxy=proxy, timeout=timeout)
    except Exception as e:
        return {"kind": "error", "error": f"附件下载失败: {e}"}
    try:
        if low.endswith((".xlsx", ".xls")):
            r = parse_xlsx(str(fp), fields=fields)
            r["_pdf"] = url
            return r
        if low.endswith((".docx", ".doc")):
            # Word 附件：zip 解包 document.xml 抽文本（不装额外依赖）
            try:
                import zipfile
                from xml.etree import ElementTree as ET
                txt_parts = []
                with zipfile.ZipFile(fp) as z:
                    if "word/document.xml" in z.namelist():
                        xml = z.read("word/document.xml")
                        root = ET.fromstring(xml)
                        for p in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                            t = "".join(n.text or "" for n in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
                            if t.strip():
                                txt_parts.append(t.strip())
                txt = "\n".join(txt_parts)
                if len(txt) >= 30:
                    return {"kind": "text", "text": txt, "tables": 0, "_pdf": url}
                return {"kind": "error", "error": "Word 附件无有效文本", "_pdf": url}
            except Exception as e:
                return {"kind": "error", "error": f"Word 解析失败: {type(e).__name__}: {e}", "_pdf": url}
        r = parse_pdf_auto(str(fp), fields=fields)
        r["_pdf"] = url
        return r
    finally:
        try:
            fp.unlink(missing_ok=True)
        except Exception:
            pass


def pdf_to_rows(url: str, proxy: Optional[str] = None, fields: Optional[List[str]] = None,
                timeout: int = 90) -> Dict[str, Any]:
    """下载 + 万能解析（战术层直接调用）。返回 parse_pdf_auto 结果 + _pdf 地址。"""
    try:
        fp = download_pdf(url, proxy=proxy, timeout=timeout)
    except Exception as e:
        return {"kind": "error", "error": f"PDF 下载失败: {e}"}
    try:
        r = parse_pdf_auto(str(fp), fields=fields)
        r["_pdf"] = url
        return r
    finally:
        try:
            fp.unlink(missing_ok=True)
        except Exception:
            pass
