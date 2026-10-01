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
    """安全边界：仅 http/https；host 解析到私网/环回/保留地址即拒绝。

    收官十二轮（审查 L）：与 core.assert_public_url / rangedl 同口径——支持
    US_ALLOW_PRIVATE=1 白名单（内网附件服务器/本地 mock），此前不生效。
    """
    import os as _os
    sp = urlsplit(url)
    if sp.scheme not in ("http", "https") or not sp.hostname:
        raise ValueError(f"仅允许 http/https 绝对地址: {url}")
    if _os.environ.get("US_ALLOW_PRIVATE") == "1":
        return url
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
    """批量下载附件。断点续传：同名且 >min_kb 的文件跳过；原子写入防半文件。
    审查修复：重试策略由外层循环独占（内层客户端 max_retries=1，避免 3×3=9 次放大）；
    _guard_url 的 ValueError（私网/坏协议）是确定性失败，直接记账不重试。"""
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
    retries = max(1, int(retries))
    # 审查十一轮（M）：interval 未钳制——负值 time.sleep(-1) 抛 ValueError 冲出
    # 函数，被 CLI 的 JSONDecodeError/ValueError 兜底误报"清单不是合法 JSON"且
    # 整批中止。mcp 有 _clamp_interval，此处对齐
    try:
        _iv = float(interval)
        interval = _iv if _iv == _iv and _iv not in (float("inf"), float("-inf")) else 1.0
        interval = max(0.1, interval)
    except (TypeError, ValueError):
        interval = 1.0
    client = make_http_client({"min_interval": interval, "timeout": 60,
                               "http_backend": "auto", "max_retries": 1})
    ok, skip, fail = [], [], []
    _name_owner: dict = {}   # name -> url（审查 P2，R15：同名不同 URL 的碰撞检测）
    for it in items:
        url, name = it["url"], (it.get("name") or "").strip()
        if name:
            # 审查修复（P2，R15）：显式 name 曾绕过白名单——路径穿越/NUL 字节
            # 可写穿 out_dir 或炸掉整个批次。统一走 allowlist
            name = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", name)[:80] or "attach.bin"
        else:
            name = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_",
                          url.rsplit("/", 1)[-1].split("?")[0])[:80] or "attach.bin"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        dest = out / name
        # OCR R131（H）：撞名检测曾被 exists() 短路——首个 URL 下载失败/尚未落盘
        # 时，第二个同 URL 撞名者直接静默覆盖。先查登记表再谈文件
        if _name_owner.get(name) not in (None, url):
            # 同名不同 URL：不是断点续跑，是两个不同附件撞名——如实记失败
            err = f"文件名碰撞：{url} 与已下载的 {_name_owner.get(name)} 同名"
            fail.append({"name": name, "error": err})
            log(f"  ✗ {name}: {err}")
            continue
        if dest.exists() and dest.stat().st_size > min_kb * 1024:
            if name not in _name_owner:
                _name_owner[name] = url
            skip.append(name)
            continue
        _name_owner[name] = url
        # 安全边界失败（私网/环回/坏协议）是确定性的：不进入重试循环
        try:
            _guard_url(url)
        except ValueError as e:
            fail.append({"name": name, "error": f"{type(e).__name__}: {e}"})
            log(f"  ✗ {name}: {e}")
            continue
        last_err = ""
        for attempt in range(1, retries + 1):
            try:
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
            if attempt < retries:
                time.sleep(interval * attempt)
        else:
            fail.append({"name": name, "error": last_err})
            log(f"  ✗ {name}: {last_err}")
        time.sleep(interval)
    return {"ok": ok, "skipped": skip, "failed": fail,
            "total": len(items), "dir": str(out)}


def table_quality_report(tables: List[Dict[str, Any]]) -> Dict[str, Any]:
    """表格提取质量门（招行年报摘要战训：无框线中文表格退化为 列1..N 垃圾列，
    却照样报"✅ 12 页表格/106 行"——完整率查不出语义垃圾）。

    检测两类退化：列名回退占比（列N/col_N）≥0.5、空单元格率 ≥0.6。
    退化时 degraded=True 并给出改道建议；调用方应把警告连同数据一起交付。"""
    import re as _re
    rows = [r for t in (tables or []) for r in (t.get("rows") or [])]
    n_rows = len(rows)
    fallback = 0
    cells = empty = 0
    for r in rows:
        keys = [k for k in r.keys() if not str(k).startswith("_")]
        if keys and all(_re.match(r"^(列|col_?)\d+$", str(k)) for k in keys):
            fallback += 1
        for k, v in r.items():
            if str(k).startswith("_"):
                continue
            cells += 1
            if not str(v or "").strip():
                empty += 1
    empty_rate = (empty / cells) if cells else 1.0
    fallback_rate = (fallback / n_rows) if n_rows else 0.0
    # 收官十二轮（审查 M，实测）：判据只有"全部键都是 列N"——首行是数据、被当
    # 表头的表（列名成了数据值）判不出，CLI 照报 ✅。补两条：表头键里数字占比
    # 过半（"甲/计算机/50/45/40"）、或键长超限（长句当列名）都算退化
    _datakey_rows = 0
    for r in rows:
        keys = [str(k) for k in r.keys() if not str(k).startswith("_")]
        if not keys:
            continue
        _numish = sum(1 for k in keys if _re.fullmatch(r"[-+]?\d[\d,]*\.?\d*%?", k.strip()))
        _long = sum(1 for k in keys if len(k.strip()) > 24)
        if _numish * 2 >= len(keys) or _long:
            _datakey_rows += 1
    _datakey_rate = (_datakey_rows / n_rows) if n_rows else 0.0
    # 审查修复 P2：0 表格同样是假成功（扫描件/图片型 PDF 常见）——归入退化
    degraded = (bool(rows) and (fallback_rate >= 0.5 or empty_rate >= 0.6
                                or _datakey_rate >= 0.5)) or n_rows == 0
    return {"rows": n_rows, "fallback_col_rows": fallback,
            "fallback_rate": round(fallback_rate, 2),
            "datakey_rate": round(_datakey_rate, 2),
            "empty_rate": round(empty_rate, 2), "degraded": degraded,
            "hint": ("表格疑似无框线/版式型或首行数据被当表头——"
                     "当前输出可能是垃圾列。建议改用文本版式解析（按词坐标聚类）或人工核对"
                     if degraded else "")}


def extract_tables(pdf_path: str | Path, pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """表格型 PDF → 行记录（每页一个 {page, rows:[{列名:值}]}）。

    pdfplumber 优先（几何还原表格，列名取首行）；未安装降级 pypdf 文本启发式。
    审查修复：ImportError 只允许发生在 import 本身——pdfplumber 装了但用起来崩
    （缺 pdfminer.six 等）必须大声抛出，绝不静默降级到列名更差的 pypdf。
    """
    fp = Path(pdf_path).expanduser()
    if not fp.exists():
        raise FileNotFoundError(f"PDF 不存在: {fp}")
    # 收官十二轮（审查 L）：整文件读盘 + 严格首 4 字节——BOM 前缀（规范允许）的
    # 合法 PDF 被拒（实测 pdfplumber 能正常打开）。只读头部 1KB 并在其中定位 %PDF
    with open(fp, "rb") as _f:
        _head = _f.read(1024)
    if b"%PDF" not in _head:
        raise ValueError(f"不是有效 PDF: {fp}")
    # 审查修复：0/负页码曾静默读成最后一页
    # OCR R131 终审（H）：调用方显式给了 pages 但过滤后为空（全是 0/负数）——
    # 曾静默提取全部页面（违背调用方意图）。区分 None（默认全页）与空列表
    if pages is not None:
        pages = [p for p in pages if p >= 1]
        if not pages:
            raise ValueError("pages 过滤后为空（原值全是 0/负页码？）——有效页码从 1 开始")

    try:
        import pdfplumber  # type: ignore
    except ImportError:
        pdfplumber = None  # 未安装 → 走 pypdf 降级（这是唯一允许的降级情形）

    if pdfplumber is not None:
        results = []
        # OCR R131（H）：pdfplumber 装了但依赖坏（缺 pdfminer.six）时，open/提取
        # 抛 ImportError——曾穿透 except 落到 pypdf 静默降级，违背 docstring 的
        # "必须大声抛出"。依赖性故障按声明上抛；捕获后 re-raise 带语境
        try:
            with pdfplumber.open(str(fp)) as pdf:
                for pno in pages or range(1, len(pdf.pages) + 1):
                    page = pdf.pages[pno - 1] if pno - 1 < len(pdf.pages) else None
                    if page is None:
                        continue
                    tables = page.extract_tables()
                    for tb in tables or []:
                        if not tb or len(tb) < 2:
                            continue
                        header = [(c or "").strip() for c in tb[0]]
                        # 审查八轮（MEDIUM）：表头重名（两列都叫「数量」）曾让 row[key]
                        # 互相覆盖——实测 header=["项目","数量","数量"], row=["甲","1","2"]
                        # 得 {'项目':'甲','数量':'2'}，第一列的值永久丢失。同名列加数字后缀。
                        # 审查十一轮（M）：后缀生成未检查与**既有**表头碰撞——
                        # header=["项目","数量","数量2","数量"] 时第三个"数量"后缀得
                        # "数量2"撞上已存在的列，值静默覆盖（第 4 列吃掉第 3 列，
                        # 且 quality_report 不报 degraded）。循环改名直到无碰撞
                        _seen_h: Dict[str, int] = {}
                        _hdr2 = []
                        _taken = set()
                        for _h in header:
                            _base = _h or ""
                            _cand = _base
                            while _cand in _taken:
                                _seen_h[_base] = _seen_h.get(_base, 1) + 1
                                _cand = f"{_base}{_seen_h[_base]}"
                            _taken.add(_cand)
                            _hdr2.append(_cand)
                        header = _hdr2
                        rows = []
                        for raw in tb[1:]:
                            row = {}
                            for i, cell in enumerate(raw):
                                key = header[i] if i < len(header) and header[i] else f"col_{i+1}"
                                row[key] = (cell or "").strip()
                            rows.append(row)
                        results.append({"page": pno, "rows": rows})
            return results
        except ImportError as e:
            raise ImportError(f"pdfplumber 已安装但依赖损坏（{e}）——按契约不静默降级，"
                              "请修复依赖：pip install --force-reinstall pdfplumber") from e

    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        raise ImportError("需要 pdfplumber（推荐）或 pypdf：pip install pdfplumber")
    reader = PdfReader(str(fp))
    results = []
    for pno in pages or range(1, len(reader.pages) + 1):
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
