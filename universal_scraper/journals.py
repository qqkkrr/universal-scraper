#!/usr/bin/env python3
"""📚 期刊论文批量下载器（magtech 期刊系统通用，沈阳体育学院学报已精配）。

能力：
  1. 自动发现全部期次（可指定起始年份，如 2024 起）
  2. 每期文章列表：articleId / 标题 / 作者 / DOI / 卷期页码（期次页直接含，不用逐篇打开）
  3. 可选拉取文章页元数据：摘要 / 关键词 / 发表日期 / 英文标题（用于知识库索引）
  4. 调用 showArticleFile.do 换取 PDF 直链并下载（免费全文，无登录无验证码）
  5. 断点续传（已下载且 >10KB 的跳过）、并发下载、限速、重试、进度条
  6. 输出：PDF 全文目录 + 元数据 CSV/JSONL + Markdown 知识库索引

用法（也可通过万能工具 CLI）：
  python3 -m universal_scraper.journals --site sytyxb --since 2024 --out ~/Desktop/沈阳体育学院学报知识库
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import html
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _assert_http_url(url: str) -> str:
    """SSRF 边界：仅 http/https；host 解析到私网/环回/保留地址即拒绝。"""
    sp = urllib.parse.urlsplit(url)
    if sp.scheme not in ("http", "https"):
        raise ValueError(f"仅允许 http/https URL: {url}")
    for info in socket.getaddrinfo(sp.hostname, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
            raise ValueError(f"拒绝私有/保留地址: {sp.hostname} -> {ip}")
    return url

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.6 Safari/605.1.15")

# ---------------------------------------------------------------------------
# 期刊站点注册表（magtech 系统通用；新增期刊只需加一行）
# ---------------------------------------------------------------------------
KNOWN_JOURNALS: Dict[str, Dict[str, str]] = {
    "sytyxb": {
        "name": "沈阳体育学院学报",
        "base": "https://stxb.magtech.com.cn",
        "ctx": "/CN",
        "issn": "1004-0560",
        "note": "CSSCI/北大核心，2024 起免费全文",
    },
    "kygl": {
        "name": "科研管理",
        "base": "https://www.kygl.net.cn",
        "ctx": "/CN",
        "issn": "1000-2995",
        "note": "CSSCI/国家自然科学基金委管理科学部认定期刊，官方 magtech 站点；2026 年期次可访问，需验证免费全文 PDF",
    },
    "kygl_cast": {
        "name": "科研管理（中国科协期刊集群）",
        "base": "https://castjournals.cast.org.cn/joweb/kygl",
        "ctx": "/CN",
        "issn": "1000-2995",
        "mode": "cast",
        "journal_id": "1263530790265569318",
        "note": "CAST 集群官方镜像，文章 PDF 直链无需登录；当前已上线 2026 年 47 卷 3 期",
    },
    # 模板：同结构期刊照抄即可
    # "example": {"name": "某某学报", "base": "https://xxx.magtech.com.cn",
    #             "ctx": "/CN", "issn": "xxxx-xxxx", "note": ""},
}

DEFAULT_WORKERS = 6
MIN_INTERVAL = 0.35
MAX_RETRIES = 3


def _http_get(url: str, referer: str = "", timeout: int = 25) -> Tuple[int, bytes]:
    url = _assert_http_url(url)  # SSRF 边界：协议/私网校验后才发起请求
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh-Hans;q=0.9",
               "Accept-Encoding": "identity"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def _http_post(url: str, data: Dict[str, Any], referer: str = "", timeout: int = 25) -> Tuple[int, bytes]:
    url = _assert_http_url(url)  # SSRF 边界
    headers = {"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
               "Accept-Language": "zh-CN,zh-Hans;q=0.9"}
    if referer:
        headers["Referer"] = referer
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


# ---------------------------------------------------------------------------
# 1) 期次发现
# ---------------------------------------------------------------------------
def list_issues(site: Dict[str, str], since_year: int = 2024) -> List[Dict[str, str]]:
    """抓期刊"过刊列表"，返回 since_year 及以后的期次。"""
    for _k in ("base", "ctx"):
        if not site.get(_k):
            raise ValueError(f"站点配置缺少字段 '{_k}'（需要 base/ctx）: {site}")
    issues: List[Dict[str, str]] = []
    if site.get("mode") == "cast":
        status, raw = _http_get(f"{site['base']}{site['ctx']}/allvolumes")
        text = raw.decode("utf-8", "ignore")
        seen_cast = set()
        for m in re.finditer(
            r'href="https://castjournals\.cast\.org\.cn/joweb/kygl/CN/(\d{4})/(\d+)/(\d+)"',
            text,
        ):
            year, vol, issue = int(m.group(1)), m.group(2), m.group(3)
            if year < since_year:
                continue
            key = (year, vol, issue)
            if key in seen_cast:
                continue
            seen_cast.add(key)
            issues.append({
                "year": str(year), "vol": vol, "issue": issue,
                "url": f"{site['base']}{site['ctx']}/Y{year}/V{vol}/I{issue}",
                "label": f"{year}年 第{issue}期(卷{vol})",
            })
        issues.sort(key=lambda x: (int(x["year"]), int(x["vol"]), int(x["issue"])))
        return issues

    url = f"{site['base']}{site['ctx']}/article/showOldVolumnList.do"
    status, raw = _http_get(url)
    text = raw.decode("utf-8", "ignore")
    seen = set()
    for m in re.finditer(r'href="([^"]*?/Y(\d{4})/V(\d+)/I(\d+))[^"]*"[^>]*>(?:<[^>]+>)*\s*([^<]{0,40})?', text):
        link, year, vol, issue = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        if year < since_year:
            continue
        key = (year, vol, issue)
        if key in seen:
            continue
        seen.add(key)
        full = link if link.startswith("http") else site["base"] + link
        issues.append({"year": str(year), "vol": vol, "issue": issue, "url": full,
                       "label": f"{year}年 第{issue}期(卷{vol})"})
    # 按年份/期排序
    issues.sort(key=lambda x: (int(x["year"]), int(x["vol"]), int(x["issue"])))
    return issues


# ---------------------------------------------------------------------------
# 2) 单期文章列表解析（期次页直接含 articleId）
# ---------------------------------------------------------------------------
def parse_issue_html(text: str, issue: Dict[str, str],
                     site: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    arts: List[Dict[str, Any]] = []
    if site and site.get("mode") == "cast":
        cast_base = f"{site['base']}{site['ctx']}"
        cast_pat = (
            r'<div class="j-title-1">\s*<a href="[^"]*?/CN/(\d+)">(.*?)</a>'
            r'(.*?)(?=<div class="j-title-1">|<div class="articlesectionlisting">|\Z)'
        )
        for m in re.finditer(cast_pat, text, re.S):
            art_id = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if not title:
                continue
            block = m.group(3)
            author_m = re.search(r'class="j-author"[^>]*>(.*?)</div>', block, re.S)
            author = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", author_m.group(1))).strip() if author_m else ""
            doi_m = re.search(r'doi:\s*(10\.19571/j\.cnki\.1000-2995\.\d{4}\.\d{2}\.\d+)', block)
            vol_m = re.search(r'class="j-volumn"[^>]*>(.*?)</span>', block, re.S)
            vol_txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", vol_m.group(1))).strip() if vol_m else ""
            arts.append({
                "id": art_id, "title": title, "authors": author,
                "doi": doi_m.group(1) if doi_m else "",
                "url": f"{cast_base}/{art_id}", "vol_pages": vol_txt,
                "year": issue.get("year", ""), "vol": issue.get("vol", ""),
                "issue": issue.get("issue", ""), "issue_label": issue.get("label", ""),
            })
        return arts
    for m in re.finditer(r'<li\s+id="art(\d+)"[^>]*>(.*?)</li>', text, re.S):
        art_id = m.group(1)
        block = m.group(2)
        title_m = re.search(r'class="j-title-1[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not title_m:
            continue
        href = title_m.group(1)
        title = re.sub(r"<[^>]+>", "", title_m.group(2)).strip()
        if not title:
            continue
        author_m = re.search(r'class="j-author"[^>]*>(.*?)</div>', block, re.S)
        author = re.sub(r"<[^>]+>", "", author_m.group(1)).strip() if author_m else ""
        vol_m = re.search(r'class="j-volumn"[^>]*>(.*?)</div>', block, re.S)
        vol_txt = re.sub(r"<[^>]+>", " ", vol_m.group(1)).strip() if vol_m else ""
        vol_txt = re.sub(r"\s+", " ", vol_txt)
        doi_m = re.search(r'class="j-doi"[^>]*>(.*?)</a>', block, re.S)
        doi = re.sub(r"<[^>]+>", "", doi_m.group(1)).strip() if doi_m else ""
        art_url = href if href.startswith("http") else f"https://stxb.magtech.com.cn{href}"
        arts.append({
            "id": art_id,
            "title": title,
            "authors": author,
            "doi": doi,
            "url": art_url,
            "vol_pages": vol_txt,
            "year": issue.get("year", ""),
            "vol": issue.get("vol", ""),
            "issue": issue.get("issue", ""),
            "issue_label": issue.get("label", ""),
        })
    return arts


# ---------------------------------------------------------------------------
# 3) 文章页元数据（摘要/关键词/日期，做知识库索引）
# ---------------------------------------------------------------------------
def fetch_article_meta(art: Dict[str, Any], site: Dict[str, str]) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    try:
        status, raw = _http_get(art["url"], referer=site["base"] + site["ctx"] + "/")
        text = raw.decode("utf-8", "ignore")
        def mget(name: str) -> str:
            m = re.search(r'<meta\s+name="%s"\s+content="([^"]*)"' % re.escape(name), text, re.I)
            if not m:
                m = re.search(r'<meta\s+content="([^"]*)"\s+name="%s"' % re.escape(name), text, re.I)
            return html.unescape(m.group(1)).strip() if m else ""
        abstract = mget("citation_abstract") or mget("description")
        abstract = re.sub(r"<[^>]+>", "", abstract).strip()
        keywords = mget("keywords")
        pub_date = mget("citation_publication_date") or mget("citation_online_date")
        en_title = mget("citation_title_en") or mget("dc.title.alternative")
        meta = {"abstract": abstract[:3000], "keywords": keywords,
                "pub_date": pub_date, "en_title": en_title}
    except Exception as e:
        meta = {"abstract": "", "keywords": "", "pub_date": "", "en_title": "",
                "_meta_error": f"{type(e).__name__}: {e}"}
    return meta


# ---------------------------------------------------------------------------
# 4) 换 PDF 直链 + 下载
# ---------------------------------------------------------------------------
def get_pdf_url(site: Dict[str, str], art: Dict[str, Any]) -> Optional[str]:
    if site.get("mode") == "cast":
        return f"{site['base']}{site['ctx']}/PDF/{art['id']}"
    api = f"{site['base']}{site['ctx']}/article/showArticleFile.do?{int(time.time()*1000)}"
    status, raw = _http_post(api, {"attachType": "PDF", "id": art["id"], "json": "true"},
                             referer=art["url"])
    text = raw.decode("utf-8", "ignore")
    m = re.search(r'\[json\](.*)', text, re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(1))
    except Exception:
        return None
    if j.get("status") != 1:
        return None
    return j.get("pdfUrl") or j.get("pdfCnUrl") or None


def _pdf_name(art: Dict[str, Any]) -> str:
    safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", art.get("title", ""))[:80] or art.get("id", "0")
    return f"{art.get('year','')}-V{art.get('vol','')}I{art.get('issue','')}-{art.get('id','0')}-{safe}.pdf"


def download_pdf(site: Dict[str, str], art: Dict[str, Any], out_dir: Path,
                 min_interval: float = MIN_INTERVAL) -> Dict[str, Any]:
    """下载单篇 PDF，返回 {ok, file, size_kb, error}。已存在且 >10KB 视为完成（断点续传）。"""
    fname = _pdf_name(art)
    out = out_dir / fname
    if out.exists() and out.stat().st_size > 10 * 1024:
        return {"ok": True, "file": str(out), "size_kb": out.stat().st_size // 1024, "skipped": True}
    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            pdf_url = get_pdf_url(site, art)
            if not pdf_url:
                last_err = "无 PDF 直链(status!=1)"
                time.sleep(min_interval * 2)
                continue
            status, raw = _http_get(pdf_url, referer=art["url"], timeout=60)
            if status != 200 or not raw[:4].startswith(b"%PDF"):
                last_err = f"下载异常 HTTP {status}"
                time.sleep(min_interval * 2 * attempt)
                continue
            # 原子写：tmp + replace 防截断 PDF 被断点续传误判为已完成
            _tmp_out = out.with_suffix(".pdf.tmp")
            _tmp_out.write_bytes(raw)
            import os as _os
            _os.replace(_tmp_out, out)
            return {"ok": True, "file": str(out), "size_kb": len(raw) // 1024, "skipped": False}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(min_interval * 2 * attempt)
        finally:
            time.sleep(min_interval)
    return {"ok": False, "file": "", "size_kb": 0, "error": last_err}


# ---------------------------------------------------------------------------
def _resume_pdfs_batch(site: Dict[str, str], out_root: Path, workers: int,
                       cdp: str, log=print, batch_size: int = 150) -> Dict[str, Any]:
    """断点续传批抓 PDF（登录态 CDP 模式）：读论文清单 CSV，跳过已有，
    分批 fetch_pdfs_batch（每批后可换 IP），完成后统一重命名为标准文件名。"""
    import csv as _csv
    from .fetchers import BrowserFetcher
    meta_dir = out_root / "元数据"
    pdf_dir = out_root / "PDF"
    csv_path = meta_dir / f"{site['name']}_论文清单.csv"
    if not csv_path.exists():
        return {"error": f"清单不存在（先跑 --list-only 生成）: {csv_path}"}
    rows = list(_csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    log(f"📋 清单 {len(rows)} 篇")

    def _std_name(r):
        safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", r.get("title", ""))[:80] or r.get("article_id", "0")
        return f"{r.get('year','')}-V{r.get('vol','')}I{r.get('issue','')}-{r.get('article_id','0')}-{safe}.pdf"

    def _has_pdf(r):
        if (pdf_dir / _std_name(r)).exists() and (pdf_dir / _std_name(r)).stat().st_size > 10 * 1024:
            return True
        legacy = pdf_dir / f"pdf_{r.get('article_id','')}.pdf"
        return legacy.exists() and legacy.stat().st_size > 10 * 1024

    todo = [r for r in rows if not _has_pdf(r)]
    log(f"⬇️ 断点续传：已有 {len(rows) - len(todo)}，待抓 {len(todo)}")
    if not todo:
        # 统一重命名 legacy 命名
        for r in rows:
            legacy = pdf_dir / f"pdf_{r.get('article_id','')}.pdf"
            if legacy.exists():
                legacy.rename(pdf_dir / _std_name(r))
        return {"ok": True, "downloaded": 0, "total": len(rows)}

    bf = BrowserFetcher({"type": "browser", "url": site["base"],
                         "cdp": cdp or "http://127.0.0.1:9222", "headless": False},
                        {}, {}, out_root)
    total_ok = 0
    stopped = False
    for bi in range(0, len(todo), batch_size):
        batch = todo[bi:bi + batch_size]
        items = [{"id": r.get("article_id", ""), "article_url": r.get("url", "")} for r in batch if r.get("article_id")]
        if not items:
            continue
        r = bf.fetch_pdfs_batch(items, pdf_dir, base_url=site["base"],
                                wait_ms=500, block_limit=25)
        total_ok += len(r["ok"])
        log(f"  本批成功 {len(r['ok'])}/{len(items)}，累计 {total_ok}", flush=True)
        if not r.get("done", True):
            log("⚠️ 连续登录墙——配额/会话失效。请换 IP 或明日重跑本命令继续。")
            stopped = True
            break
        if bi + batch_size < len(todo):
            time.sleep(20)
    # 统一重命名 pdf_{id}.pdf → 标准名
    renamed = 0
    by_id = {r.get("article_id", ""): r for r in rows}
    for legacy in pdf_dir.glob("pdf_*.pdf"):
        aid = legacy.stem.replace("pdf_", "")
        r = by_id.get(aid)
        if r:
            legacy.rename(pdf_dir / _std_name(r))
            renamed += 1
    log(f"{'⚠️ 提前停止' if stopped else '✅ 完成'}：本轮新下 {total_ok} 篇，重命名 {renamed}，"
        f"总覆盖 {len(rows) - len([r for r in rows if not _has_pdf(r)])}/{len(rows)}")
    return {"ok": True, "downloaded": total_ok, "stopped": stopped, "total": len(rows)}


# ---------------------------------------------------------------------------
# 5) 主流程编排
# ---------------------------------------------------------------------------
def run(site_name: str = "sytyxb", since_year: int = 2024, out_dir: Optional[str] = None,
        workers: int = DEFAULT_WORKERS, with_meta: bool = True,
        min_interval: float = MIN_INTERVAL, log=print, list_only: bool = False,
        pdf_batch_resume: bool = False, cdp: str = "") -> Dict[str, Any]:
    if site_name not in KNOWN_JOURNALS:
        raise SystemExit(f"未知期刊站点 {site_name}，可用: {list(KNOWN_JOURNALS)}")
    site = KNOWN_JOURNALS[site_name]
    out_root = Path(out_dir or f"outputs/journal_{site_name}").expanduser()
    pdf_dir = out_root / "PDF"
    meta_dir = out_root / "元数据"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    if pdf_batch_resume:
        return _resume_pdfs_batch(site, out_root, workers, cdp=cdp, log=log)

    log(f"📚 {site['name']}（{site['base']}） 起始年份 {since_year}")

    # 1) 期次
    issues = list_issues(site, since_year=since_year)
    log(f"📅 发现 {len(issues)} 期: " + ", ".join(i["label"] for i in issues))
    if not issues:
        return {"error": "未发现期次", "issues": 0, "articles": 0}

    # 2) 文章列表（并发抓期次页）
    all_arts: List[Dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=min(workers, 8)) as ex:
        futs = {ex.submit(_fetch_issue, site, i): i for i in issues}
        for f in cf.as_completed(futs):
            i = futs[f]
            try:
                arts = f.result()
                all_arts.extend(arts)
                log(f"  ✅ {i['label']}: {len(arts)} 篇")
            except Exception as e:
                log(f"  ❌ {i['label']}: {type(e).__name__}: {e}")
            time.sleep(min_interval)

    # 2.5) 清单模式：只落论文清单（标题/作者/期次/DOI，从期次列表提取，无需登录）
    #      ——《科研管理》战例：官网 PDF 已登录墙化，元数据逐篇拉取慢且限流；
    #         全期次清单是核心交付物，等网站冷却后另跑 PDF/元数据。
    if list_only:
        list_records: List[Dict[str, Any]] = []
        for a in all_arts:
            list_records.append({
                "year": a.get("year", ""), "vol": a.get("vol", ""), "issue": a.get("issue", ""),
                "期次": a.get("issue_label", ""), "title": a.get("title", ""),
                "authors": a.get("authors", ""), "doi": a.get("doi", ""),
                "url": a.get("url", ""), "article_id": a.get("id", ""),
                "pages": a.get("vol_pages", ""),
            })
        list_records.sort(key=lambda x: (x["year"], x["vol"], x["issue"]))
        lcsv = meta_dir / f"{site['name']}_论文清单.csv"
        if list_records:
            with open(lcsv, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(list_records[0].keys()))
                w.writeheader()
                w.writerows(list_records)
        log(f"📋 清单模式完成：{len(list_records)} 篇 -> {lcsv}")
        return {"ok": True, "articles": len(list_records), "csv": str(lcsv)}

    # 3) 元数据（可选，增量缓存：中断后已拉取的不会重拉）
    meta_cache = meta_dir / ".meta_cache.jsonl"
    meta_cache_loaded: Dict[str, Dict[str, Any]] = {}
    if meta_cache.exists():
        for line in meta_cache.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                meta_cache_loaded[rec.get("id", "")] = rec
            except Exception:
                pass
    if with_meta:
        todo = [a for a in all_arts if a["id"] not in meta_cache_loaded]
        for a in all_arts:
            if a["id"] in meta_cache_loaded:
                a.update({k: v for k, v in meta_cache_loaded[a["id"]].items() if k != "id"})
        log(f"🔎 拉取 {len(todo)}/{len(all_arts)} 篇摘要/关键词（缓存 {len(all_arts)-len(todo)} 篇，并发 {workers}）...")
        _cache_buf: List[str] = []
        def _flush_cache():
            if not _cache_buf:
                return
            try:
                with open(meta_cache, "a", encoding="utf-8") as mc:
                    mc.write("".join(_cache_buf))
                _cache_buf.clear()
            except Exception:
                pass
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_article_meta, a, site): a for a in todo}
            done = 0
            for f in cf.as_completed(futs):
                a = futs[f]
                try:
                    meta = f.result()
                    a.update(meta)
                    _cache_buf.append(json.dumps({"id": a["id"], **meta}, ensure_ascii=False) + "\n")
                    if len(_cache_buf) >= 25:
                        _flush_cache()
                except Exception:
                    pass
                done += 1
                if done % 25 == 0:
                    log(f"  ... 元数据 {done}/{len(todo)}")
                time.sleep(min_interval / 2)
        _flush_cache()

    # 4) 下载 PDF（断点续传：已存在且 >10KB 的自动跳过）
    have_pdf = sum(1 for a in all_arts if (pdf_dir / _pdf_name(a)).exists() and (pdf_dir / _pdf_name(a)).stat().st_size > 10*1024)
    log(f"⬇️ 下载 PDF（并发 {workers}，断点续传，已有 {have_pdf}/{len(all_arts)} 篇）...")
    results: List[Dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(download_pdf, site, a, pdf_dir, min_interval): a for a in all_arts}
        done = 0
        for f in cf.as_completed(futs):
            a = futs[f]
            r = f.result()
            r["_art"] = a
            results.append(r)
            done += 1
            if done % 20 == 0 or done == len(all_arts):
                ok_n = sum(1 for x in results if x.get("ok"))
                log(f"  ... PDF {done}/{len(all_arts)}（成功 {ok_n}）")
            time.sleep(min_interval / 2)

    # 5) 落盘：元数据 + 索引
    ok = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    records: List[Dict[str, Any]] = []
    for r in results:
        a = r.get("_art", {})
        records.append({
            "year": a.get("year", ""), "vol": a.get("vol", ""), "issue": a.get("issue", ""),
            "title": a.get("title", ""), "authors": a.get("authors", ""),
            "doi": a.get("doi", ""), "url": a.get("url", ""),
            "article_id": a.get("id", ""), "pages": a.get("vol_pages", ""),
            "pub_date": a.get("pub_date", ""), "keywords": a.get("keywords", ""),
            "abstract": a.get("abstract", ""), "pdf_file": r.get("file", ""),
            "pdf_size_kb": r.get("size_kb", 0),
        })
    records.sort(key=lambda x: (x["year"], x["vol"], x["issue"]))

    csv_path = meta_dir / f"{site['name']}_论文清单.csv"
    jsonl_path = meta_dir / f"{site['name']}_论文清单.jsonl"
    idx_path = out_root / f"{site['name']}_知识库索引.md"

    if records:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(records[0].keys()))
            w.writeheader()
            w.writerows(records)
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # Markdown 索引（知识库入口：标题/作者/关键词/摘要/PDF 路径）
    lines = [f"# {site['name']} 知识库索引", "",
             f"- 覆盖范围：{since_year} 年至今（{len(issues)} 期 / {len(records)} 篇）",
             f"- PDF 目录：`{pdf_dir}`", f"- 清单：`{csv_path}`", "",
             "## 论文列表", ""]
    for rec in records:
        lines.append(f"### {rec['title']}")
        lines.append(f"- 作者：{rec['authors']} | 卷期：{rec['year']}年 {rec['vol']}({rec['issue']}) | 页码：{rec['pages']}")
        lines.append(f"- DOI：{rec['doi']} | 发表日期：{rec['pub_date']}")
        if rec.get("keywords"):
            lines.append(f"- 关键词：{rec['keywords']}")
        if rec.get("abstract"):
            lines.append(f"- 摘要：{rec['abstract'][:200]}{'…' if len(rec['abstract'])>200 else ''}")
        if rec.get("pdf_file"):
            lines.append(f"- PDF：`{rec['pdf_file']}`")
        lines.append("")
    idx_path.write_text("\n".join(lines), encoding="utf-8")

    total_mb = sum(r.get("size_kb", 0) for r in ok) / 1024
    summary = {
        "site": site_name, "journal": site["name"], "issues": len(issues),
        "articles_total": len(all_arts), "pdf_ok": len(ok), "pdf_fail": len(fail),
        "total_mb": round(total_mb, 1),
        "out_dir": str(out_root), "pdf_dir": str(pdf_dir),
        "csv": str(csv_path), "index": str(idx_path),
    }
    log("")
    log(f"🎉 完成：{len(ok)}/{len(all_arts)} 篇 PDF 下载成功（{round(total_mb,1)} MB）")
    log(f"   输出目录: {out_root}")
    log(f"   清单: {csv_path}")
    log(f"   知识库索引: {idx_path}")
    if fail:
        log(f"   ⚠️ 失败 {len(fail)} 篇：")
        for r in fail[:10]:
            a = r.get("_art", {})
            log(f"     - {a.get('title','')[:40]} | {r.get('error','')}")
    return summary


def _fetch_issue(site: Dict[str, str], issue: Dict[str, str]) -> List[Dict[str, Any]]:
    status, raw = _http_get(issue["url"], referer=site["base"] + site["ctx"] + "/")
    text = raw.decode("utf-8", "ignore")
    arts = parse_issue_html(text, issue, site=site)
    if not arts:
        raise RuntimeError(f"期次页无文章（HTTP {status}）")
    return arts


def main() -> int:
    ap = argparse.ArgumentParser(description="📚 期刊论文批量下载器（magtech 系统通用）")
    ap.add_argument("--site", default="sytyxb", help=f"期刊站点（可用: {list(KNOWN_JOURNALS)}）")
    ap.add_argument("--since", type=int, default=2024, help="起始年份（默认 2024）")
    ap.add_argument("--out", default=None, help="输出目录（默认 outputs/journal_<site>）")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并发数")
    ap.add_argument("--no-meta", action="store_true", help="跳过摘要/关键词拉取（更快）")
    ap.add_argument("--min-interval", type=float, default=MIN_INTERVAL, help="请求间隔秒")
    args = ap.parse_args()
    args.workers = max(1, min(args.workers, 32))  # 防 1000 线程爆炸
    summary = run(args.site, since_year=args.since, out_dir=args.out,
                  workers=args.workers, with_meta=not args.no_meta,
                  min_interval=args.min_interval)
    return 0 if summary.get("pdf_fail", 1) == 0 or summary.get("pdf_ok", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
