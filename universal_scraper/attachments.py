#!/usr/bin/env python3
"""📎 附件发现与落盘（列表/通报类任务的"下载契约"）。

三件事都是实战踩出来的，不是设计洁癖：
1. **扩展名三级定名**：URL 路径 → `?fileUrl=` 包壳参数 → 默认 `.bin`。
   政府 CMS 常把附件包成 `/api-gateway/...?fileUrl=/cms_files/.../x.docx`；
   白名单漏了图片类型时 `.png` 会落成 `.bin`（实测 30 个真实附件被误命名）。
2. **内容嗅探校正**：下载后按 magic bytes 定真身，OLE/ZIP 容器再按**流名子串**
   细分 `doc/docx`、`xls/xlsx`（ZIP 目录区可能在文件尾部，必须扫全量；OLE 先认
   `WordDocument` 再认 `Workbook`——`Book` 这类裸子串会让 Word 文档误判成 xls）。
3. **幂等落盘**：存在性按 `前缀_序号.*`（**任意已知扩展名**）判——因为落盘前会按
   内容改名（`.bin→.png`），只查 URL 推断名会让被改名的文件每轮"不存在"→ 重下。
   HTML/XML 外壳页（含 BOM 开头、`<head>/<meta>/<?xml/<!--` 开头）一律不写盘，
   已落盘的假附件在复检时删除重试；失败逐条记账（不静默丢件）。

**镜像并集**：CMS 镜像站的正页常被截断（实测正页 3 张名单图 vs 镜像 5 张 =
声明 38 条），附件/图片必须与全部镜像页取并集；同内容文件按 sha256 去重。

公开 API：
  ext_of_url(url) / sniff_ext(body) / extract_targets(html, base)
  mirror_union(pages) / download_targets(client, urls, dest_dir, ...) / dedupe_by_content(paths)
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

KNOWN_EXTS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".png", ".jpg", ".jpeg",
              ".gif", ".csv", ".txt", ".wps", ".et", ".ppt", ".pptx")
_ATT_PATH_HINT = re.compile(r"/(?:attach|oldfile|attachment|upload|files?)/", re.I)
_VIEWER = re.compile(r"viewer\.html", re.I)
_STORE_WORDS = re.compile(r"(官网|商店|市场|助手|Store|软件园|应用宝|豌豆荚|下载)", re.I)
_STATIC_EXT = re.compile(r"\.(?:js|css|json|ico|svg|woff2?|ttf|eot|map)$", re.I)
_HTML_HEAD = (b"<!doctype", b"<html", b"<head", b"<meta", b"<?xml", b"<!--")
_DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _looks_like_html(body: bytes) -> bool:
    """是否 HTML/XML 外壳页（含 BOM 与 <?xml 开头）。

    只看 `body[:15].lstrip().startswith(b"<!doctype html")` 会被两类绕过：
    UTF-8 BOM 开头的错误页、`<head>/<meta>/<?xml/<!--` 开头的网关页——它们会被当附件
    落盘，然后进入"0 行且无诊断"的静默链路。"""
    head = body[:256].lstrip()
    for bom in (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"):
        if head.startswith(bom):
            head = head[len(bom):].lstrip()
            break
    low = head.lower()
    return any(low.startswith(m) for m in _HTML_HEAD)


def ext_of_url(url: str, default: str = "bin") -> str:
    """扩展名三级定名：URL 路径 → `?fileUrl=` 包壳参数 → 默认。"""
    p = urllib.parse.urlparse(url or "")
    suf = Path(urllib.parse.unquote(p.path)).suffix.lower()
    if suf in KNOWN_EXTS:
        return suf
    for key in ("fileUrl", "fileurl", "file", "path", "url"):
        for v in urllib.parse.parse_qs(p.query).get(key, []):
            s2 = Path(urllib.parse.unquote(v)).suffix.lower()
            if s2 in KNOWN_EXTS:
                return s2
    return f".{default}"


def sniff_ext(body: bytes) -> str:
    """按内容定真身；认不出返回 ""（调用方保留 URL 推断值）。"""
    if not body:
        return ""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if body[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if body[:4] == b"%PDF":
        return ".pdf"
    if body[:4] == b"PK\x03\x04":
        # OOXML 家族：目录区可能不在头部，扫全量找流名
        if b"xl/workbook.xml" in body:
            return ".xlsx"
        if b"ppt/presentation.xml" in body:
            return ".pptx"
        return ".docx"
    if body[:4] == b"\xd0\xcf\x11\xe0":
        # OLE 容器：先认 Word 流（"Book" 是裸子串——"Bookmark" 会让 Word 文档误判成 .xls）
        if b"WordDocument" in body:
            return ".doc"
        if b"Workbook" in body:
            return ".xls"
        return ".doc"
    return ""


def _abs_url(u: str, base: str) -> str:
    u = urllib.parse.unquote(str(u or "").strip())
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http"):
        return u
    if u.startswith("/"):
        return base.rstrip("/") + u
    return base.rstrip("/") + "/" + u


def extract_targets(html: str, base: str = "") -> Tuple[List[str], List[str]]:
    """从页面 HTML 里抽 (附件, 图片)，按出现顺序去重。

    规则与教训：
      ① `viewer.html?file=<真附件>` —— pdfjs 外壳，取 file= 参数才是真附件
      ② href/src 直达已知扩展名（跳过 viewer.html 外壳本身）
      ③ `/attach/`、`/oldfile/` 等路径兜底（含无扩展名的 attach 链），同样跳过外壳
      ④ 正文图片（`/picture/...png`）——名单期常把表格做成图片
    """
    atts: List[str] = []
    imgs: List[str] = []

    def _push(lst: List[str], u: str) -> None:
        u = _abs_url(u, base)
        if u and u not in lst:
            lst.append(u)

    for m in re.finditer(r'viewer\.html\?file=([^"\'&]+)', html or "", re.I):
        _push(atts, m.group(1))
    ext_re = "|".join(e.lstrip(".") for e in KNOWN_EXTS if e not in (".png", ".jpg", ".jpeg", ".gif"))
    for m in re.finditer(r'(?:href|src)=["\']([^"\']+\.(?:' + ext_re + r'))["\']', html or "", re.I):
        if _VIEWER.search(m.group(1)):
            continue
        _push(atts, m.group(1))
    for m in re.finditer(r'["\']([^"\']*?' + _ATT_PATH_HINT.pattern + r'[^"\']+)["\']', html or "", re.I):
        u = m.group(1)
        if _VIEWER.search(u):
            continue
        if "?" in u and not _ATT_PATH_HINT.search(urllib.parse.urlparse(u).path):
            continue
        if _STATIC_EXT.search(urllib.parse.urlparse(u).path):
            continue                      # /files/ 下的 js/css 不是附件（会被下成 .bin 垃圾）
        _push(atts, u)
    for m in re.finditer(r'(?:src|href|data-src)=["\']([^"\']*?/(?:picture|image|img)/[^"\']+?\.(?:png|jpe?g|gif))["\']',
                         html or "", re.I):
        _push(imgs, m.group(1))
    return atts, imgs


def mirror_union(pages: Iterable[str], base: str = "") -> Tuple[List[str], List[str]]:
    """多页（正页 + 全部镜像）取并集：附件/图片各自按出现顺序去重。"""
    atts: List[str] = []
    imgs: List[str] = []
    for html in pages:
        a, i = extract_targets(html, base)
        for u in a:
            if u not in atts:
                atts.append(u)
        for u in i:
            if u not in imgs:
                imgs.append(u)
    return atts, imgs


def download_targets(client: Any, urls: List[str], dest_dir: Path, prefix: str = "att",
                     headers: Optional[Dict[str, str]] = None, start: int = 1,
                     referer: str = "") -> Dict[str, Any]:
    """幂等下载一组目标到 dest_dir，返回 {saved, skipped, failed, files}。

    - 文件名：`{prefix}_{序号:02d}{扩展名}`（扩展名先按 URL 推断，落盘前用内容嗅探校正）
    - 已存在且非空 → skipped（不重下，保住幂等）
    - 内容是 HTML（viewer 外壳/错误页）→ failed（不写盘，避免"假附件"）
    - 每个目标都过 `core.assert_public_url` 出站守卫（URL 来自页面，不可信）
    """
    from .core import assert_public_url

    dest_dir = Path(dest_dir)
    hdrs = {"User-Agent": _DEFAULT_UA}
    if referer:
        hdrs["Referer"] = referer
    if headers:
        hdrs.update(headers)
    saved: List[str] = []
    skipped: List[str] = []
    failed: List[Dict[str, str]] = []
    for j, u in enumerate(urls, start):
        try:
            assert_public_url(u, context="attachments.download_targets")
        except Exception as e:
            failed.append({"url": u, "error": f"出站守卫拒绝: {type(e).__name__}: {e}"})
            continue
        p = dest_dir / f"{prefix}_{j:02d}{ext_of_url(u)}"
        # 幂等键必须是"真扩展名"：落盘前会按内容嗅探改名（.bin→.png 等），
        # 若只查 URL 推断名，被改过名的文件每轮都"不存在"→ 重新下载（实测）。
        existing = [q for q in dest_dir.glob(f"{prefix}_{j:02d}.*")
                    if q.is_file() and q.stat().st_size > 0]
        if existing:
            head = existing[0].read_bytes()[:256]
            if _looks_like_html(head):
                failed.append({"url": u, "error": f"已落盘的是 HTML 外壳（{existing[0].name}），"
                                                  f"删除后重试"})
                existing[0].unlink(missing_ok=True)
                continue
            skipped.append(str(existing[0]))
            continue
        try:
            r = client.get(u, headers=hdrs)
        except Exception as e:
            failed.append({"url": u, "error": f"{type(e).__name__}: {e}"})
            continue
        body = r.get("body") or b""
        if not r.get("ok") or not body:
            failed.append({"url": u, "error": f"status={r.get('status')} 空体"})
            continue
        if _looks_like_html(body):
            failed.append({"url": u, "error": "内容是 HTML（viewer 外壳/错误页），非附件"})
            continue
        real = sniff_ext(body)
        if real and real != p.suffix:
            p = p.with_suffix(real)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body)
        except Exception as e:
            failed.append({"url": u, "error": f"写盘失败 {type(e).__name__}: {e}"})
            continue
        saved.append(str(p))
    files = skipped + saved
    return {"saved": saved, "skipped": skipped, "failed": failed, "files": files}


def dedupe_by_content(paths: List[str]) -> Tuple[List[str], List[str]]:
    """按 sha256 去重（同一附件被两条 URL 各下一遍：viewer 外壳链 + 直达链）。

    返回 (保留, 重复)。重复项由调用方决定是否删除（本函数只判定，不动文件）。
    """
    keep: List[str] = []
    dups: List[str] = []
    seen: Dict[str, str] = {}
    for p in paths:
        fp = Path(p)
        if not fp.exists() or not fp.is_file():
            keep.append(p)
            continue
        h = hashlib.sha256(fp.read_bytes()).hexdigest()
        if h in seen:
            dups.append(p)
        else:
            seen[h] = p
            keep.append(p)
    return keep, dups
