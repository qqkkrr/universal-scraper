#!/usr/bin/env python3
"""📎 附件下载 + 表格型 PDF 结构化（batch1401/1600 战训：1403 的 187 份 PDF、
1514 等场景此前全靠 agent 手写 pypdf 启发式）。

两个能力：
1. download_attachments：批量下载（限速/重试/断点续传/%PDF 魔数校验/原子写入）
2. extract_tables：表格型 PDF → 行记录（pdfplumber 优先，缺失时降级 pypdf 文本启发式）

依赖可选：pip install pdfplumber（推荐，表格还原最好）或 pypdf。
用法:
  python3 -m universal_scraper.cli pdf --download urls.json --out attachments/
  python3 -m universal_scraper.cli pdf --tables attachments/xxx.pdf
urls.json 格式: [{"url": "...", "name": "可选文件名"}, ...] 或 ["url1", "url2"]
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def _guard_url(url: str) -> str:
    """安全边界：仅 http/https；host 解析到私网/环回/保留地址即拒绝。"""
    sp = urlsplit(url)
    if sp.scheme not in ("http", "https") or not sp.hostname:
        raise ValueError(f"仅允许 http/https 绝对地址: {url}")
    try:
        for info in socket.getaddrinfo(sp.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                raise ValueError(f"拒绝私有/保留地址: {sp.hostname} -> {ip}")
    except socket.gaierror as e:
        raise ValueError(f"域名解析失败: {e}")
    return url


def download_attachments(urls_file: str | Path, out_dir: str | Path,
                         interval: float = 1.0, retries: int = 3,
                         min_kb: int = 10, log=print) -> Dict[str, Any]:
    """批量下载附件。断点续传：同名且 >min_kb 的文件跳过；原子写入防半文件。"""
    from .core import make_http_client
    data = json.loads(Path(urls_file).expanduser().read_text(encoding="utf-8"))
    items = []
    for it in data:
        if isinstance(it, str):
            items.append({"url": it, "name": ""})
        elif isinstance(it, dict) and it.get("url"):
            items.append(it)
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    client = make_http_client({"min_interval": interval, "timeout": 60,
                               "http_backend": "auto", "max_retries": retries})
    ok, skip, fail = [], [], []
    for it in items:
        url, name = it["url"], (it.get("name") or "").strip()
        if not name:
            name = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_",
                          url.rsplit("/", 1)[-1].split("?")[0])[:80] or "attach.bin"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        dest = out / name
        if dest.exists() and dest.stat().st_size > min_kb * 1024:
            skip.append(name)
            continue
        last_err = ""
        for attempt in range(1, retries + 1):
            try:
                _guard_url(url)
                r = client.get(url)
                body = r.get("body") or b""
                if r.get("ok") and body[:4] == b"%PDF" and len(body) > min_kb * 1024:
                    tmp = dest.with_suffix(".pdf.tmp")
                    tmp.write_bytes(body)
                    tmp.replace(dest)
                    ok.append(name)
                    log(f"  ✓ {name} {len(body)//1024}KB")
                    break
                last_err = f"HTTP {r.get('status')} / 非PDF({body[:4]!r}) {len(body)}B"
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:60]}"
            time.sleep(interval * attempt)
        else:
            fail.append({"name": name, "error": last_err})
            log(f"  ✗ {name}: {last_err}")
        time.sleep(interval)
    return {"ok": ok, "skipped": skip, "failed": fail,
            "total": len(items), "dir": str(out)}


def extract_tables(pdf_path: str | Path, pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """表格型 PDF → 行记录（每页一个 {page, rows:[{列名:值}]}）。

    pdfplumber 优先（几何还原表格，列名取首行）；未安装降级 pypdf 文本启发式
    （按行拆 + 空白分列，列名为 col_1..col_n）。两者都没有时抛出可安装指引。
    """
    fp = Path(pdf_path).expanduser()
    if not fp.exists():
        raise FileNotFoundError(f"PDF 不存在: {fp}")
    if fp.read_bytes()[:4] != b"%PDF":
        raise ValueError(f"不是有效 PDF: {fp}")

    try:
        import pdfplumber  # type: ignore
        results = []
        with pdfplumber.open(str(fp)) as pdf:
            target = pages or range(1, len(pdf.pages) + 1)
            for pno in target:
                page = pdf.pages[pno - 1] if pno - 1 < len(pdf.pages) else None
                if page is None:
                    continue
                tables = page.extract_tables()
                for tb in tables or []:
                    if not tb or len(tb) < 2:
                        continue
                    header = [(c or "").strip() for c in tb[0]]
                    rows = []
                    for raw in tb[1:]:
                        row = {}
                        for i, cell in enumerate(raw):
                            key = header[i] if i < len(header) and header[i] else f"col_{i+1}"
                            row[key] = (cell or "").strip()
                        rows.append(row)
                    results.append({"page": pno, "rows": rows})
        return results
    except ImportError:
        pass

    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(str(fp))
        results = []
        target = pages or range(1, len(reader.pages) + 1)
        for pno in target:
            if pno - 1 >= len(reader.pages):
                continue
            text = reader.pages[pno - 1].extract_text() or ""
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if not lines:
                continue
            ncol = max(len(re.split(r"\s{2,}", l)) for l in lines)
            rows = []
            for l in lines:
                cells = re.split(r"\s{2,}", l)
                cells += [""] * (ncol - len(cells))
                rows.append({f"col_{i+1}": c for i, c in enumerate(cells)})
            results.append({"page": pno, "rows": rows, "_hint": "pypdf 文本启发式（建议装 pdfplumber 获得精确列名）"})
        return results
    except ImportError:
        raise ImportError("需要 pdfplumber（推荐）或 pypdf：pip install pdfplumber")
