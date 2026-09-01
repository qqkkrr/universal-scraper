#!/usr/bin/env python3
"""Fetch and render the official MAG XML full text of Science Research Management 2026."""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import pathlib
import re
import sys
from urllib.parse import urlparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests
from lxml import etree

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
SITE = "https://www.kygl.net.cn"
CHROME_SHELL = "/Users/kairanqin/Library/Caches/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell"
OUT = pathlib.Path(os.environ.get("KYGL_OUT", "/Users/kairanqin/Desktop/Annualreport_tools-main/《科研管理》范文"))
CSV = OUT / "元数据" / "科研管理_论文清单.csv"
XML_ROOT = OUT / "元数据" / "官方XML"
RENDER_ROOT = OUT / "元数据" / "官方XML" / "rendered"
PDF_ROOT = OUT / "PDF"
MANIFEST = XML_ROOT / "manifest.json"

ARTICLE_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
@page { size: A4; margin: 20mm 18mm; }
body { font-family: "PingFang SC", "Songti SC", serif; font-size: 11pt; line-height: 1.7; color: #111; margin: 0; }
.article-head { border-bottom: 2px solid #333; padding-bottom: 10px; margin-bottom: 14px; }
.article-title { font-size: 17pt; line-height: 1.4; margin: 0 0 8px; }
.article-title-en { font-size: 10pt; color: #444; margin: 0 0 8px; }
.authors { font-size: 10.5pt; margin: 4px 0; }
.aff { font-size: 9.5pt; color: #333; margin: 2px 0; }
.journal-line { font-size: 9pt; color: #444; margin: 5px 0; }
.abstract { background: #f6f6f6; padding: 10px 12px; margin: 8px 0 12px; }
.abstract .body-p { text-indent: 1.5em; }
.sec-title, h2, h3, h4 { font-size: 13pt; font-weight: 700; margin: 14px 0 7px; }
.body-p { text-indent: 2em; margin: 0 0 .5em; text-align: justify; }
.kwd-group { margin: 4px 0; font-size: 10pt; }
.keyword { display: inline-block; margin-right: 6px; }
.funding { font-size: 9.5pt; color:#333; margin: 5px 0; }
.figure { margin: 14px 0; text-align: center; page-break-inside: avoid; }
.figure img { max-width: 100%; max-height: 65mm; border: 1px solid #ddd; }
.fig-label, .caption { font-size: 10pt; margin-top: 4px; color: #333; }
.data-table { border-collapse: collapse; width: 100%; font-size: 9pt; margin: 10px 0; }
.data-table td, .data-table th { border: 1px solid #999; padding: 3px; }
.formula { text-align: center; margin: 8px 0; }
.references { margin-top: 18px; font-size: 9pt; }
.ref-item { margin: 4px 0; line-height: 1.55; }
.note { font-size: 9pt; color: #333; }
a.ext-link { color: #0645ad; word-break: break-all; }
</style></head><body>
<div class="article-head">
<h1 class="article-title">@@TITLE@@</h1>
<div class="article-title-en">@@TITLE_EN@@</div>
<div class="authors">@@AUTHORS@@</div>
@@AFFS@@
<div class="journal-line">@@JOURNAL_LINE@@</div>
@@ABSTRACT@@
@@KEYWORDS@@
@@FUNDING@@
</div>
@@BODY@@
@@BACK@@
</body></html>"""


def local(e) -> str:
    return etree.QName(e).localname


def first(e, name: str):
    v = e.xpath("*[local-name()=$n]", n=name)
    return v[0] if v else None


def firsttext(e, name: str) -> str:
    v = first(e, name)
    return "".join(v.itertext()).strip() if v is not None else ""


def text(e) -> str:
    return "".join(e.itertext())


def esc(s) -> str:
    return html.escape(s or "", quote=False)


def child_html(e, sec_level: int = 0) -> str:
    out = esc(e.text or "")
    for c in e:
        out += element_html(c, sec_level)
        out += esc(c.tail or "")
    return out


def graphic_src(e) -> str:
    hrefs = e.xpath('@*[local-name()="href"]')
    if not hrefs:
        return ""
    return str(hrefs[0])


def element_html(e, sec_level: int = 0) -> str:
    name = local(e)
    if name in ("text", "title-group", "trans-article-title", "article-title", "source",
                "person-group", "name", "surname", "given-names", "string-name", "etal",
                "journal-id", "journal-title-group", "journal-title", "issn", "article-id",
                "article-categories", "article-categorie", "pub-date", "day", "month",
                "year", "history", "date", "received", "revised", "accepted", "contrib",
                "author-notes", "corresp", "email", "aff-alternatives", "aff", "institution-wrap",
                "institution", "addr-line", "country", "city", "postcode", "name-alternatives",
                "bio", "funding-group", "funding-source", "grant-number", "notes", "label"):
        return child_html(e, sec_level)
    if name in ("sec", "abstract", "trans-abstract", "body", "back"):
        parts = []
        for c in e:
            parts.append(element_html(c, sec_level + (1 if name == "sec" else 0)))
        return "".join(parts)
    if name == "title":
        level = min(3, max(1, sec_level))
        if sec_level == 0:
            return f'<h3 class="sec-title">{child_html(e, sec_level)}</h3>'
        return f'<h{min(5, level + 2)} class="sec-title">{child_html(e, sec_level)}</h{min(5, level + 2)}>'
    if name == "p":
        return f'<p class="body-p">{child_html(e, sec_level)}</p>'
    if name in ("disp-quote", "quote", "verse"):
        return f"<blockquote>{child_html(e, sec_level)}</blockquote>"
    if name == "fig":
        label = first(e, "label")
        caption = first(e, "caption")
        lab = f'<div class="fig-label">{child_html(label, sec_level)}</div>' if label is not None else ""
        cap = f'<figcaption class="caption">{child_html(caption, sec_level)}</figcaption>' if caption is not None else ""
        imgs = ""
        for g in e.xpath('.//*[local-name()="graphic"]'):
            src = graphic_src(g)
            if src:
                imgs += f'<img class="figure-img" src="assets/{pathlib.PurePosixPath(src).name}" loading="lazy">'
        return f'<figure class="figure">{lab}{imgs}{cap}</figure>'
    if name == "caption":
        return f'<div class="caption">{child_html(e, sec_level)}</div>'
    if name == "graphic":
        src = graphic_src(e)
        return f'<img class="figure-img" src="assets/{pathlib.PurePosixPath(src).name}" loading="lazy">' if src else ""
    if name == "table-wrap":
        tb = first(e, "table")
        return f'<div class="table-wrap">{child_html(tb, sec_level) if tb is not None else ""}</div>'
    if name == "table":
        return '<table class="data-table">' + child_html(e, sec_level) + "</table>"
    if name in ("thead", "tbody", "tfoot", "tr", "td", "th"):
        tag = "th" if name == "th" else name
        attrs = ""
        if name in ("td", "th"):
            spans = []
            for a in ("rowspan", "colspan"):
                if e.get(a):
                    spans.append(f'{a}="{esc(e.get(a))}"')
            if spans:
                attrs = " " + " ".join(spans)
        return f"<{tag}{attrs}>{child_html(e, sec_level)}</{tag}>"
    if name == "list":
        tag = "ol" if e.get("list-type") == "order" else "ul"
        return f'<{tag} class="article-list">{child_html(e, sec_level)}</{tag}>'
    if name == "list-item":
        return f"<li>{child_html(e, sec_level)}</li>"
    if name == "disp-formula":
        return f'<div class="formula">{child_html(e, sec_level)}</div>'
    if name == "inline-formula":
        return f'<span class="formula-inline">{child_html(e, sec_level)}</span>'
    if name == "math":
        try:
            return etree.tostring(e, encoding="unicode", with_tail=False)
        except Exception:
            return child_html(e, sec_level)
    if name in ("sup", "sub"):
        return f"<{name}>{child_html(e, sec_level)}</{name}>"
    if name in ("italic", "i", "em"):
        return f"<i>{child_html(e, sec_level)}</i>"
    if name in ("bold", "b", "strong"):
        return f"<b>{child_html(e, sec_level)}</b>"
    if name == "xref":
        return f'<sup class="xref">[{child_html(e, sec_level)}]</sup>'
    if name in ("fn", "fn-group", "note"):
        return f'<div class="note">{child_html(e, sec_level)}</div>'
    if name == "kwd-group":
        return f'<div class="kwd-group">{child_html(e, sec_level)}</div>'
    if name == "kwd":
        return f'<span class="keyword">{child_html(e, sec_level)}</span>'
    if name in ("uri", "url", "ext-link"):
        href = (e.get("href") or e.get("xlink:href") or "").strip()
        atxt = "".join(e.itertext()) or href
        return f'<a class="ext-link" href="{esc(href)}">{esc(atxt)}</a>'
    if name in ("hr", "break", "line-break"):
        return "<br>"
    if name == "ref-list":
        parts = ['<section class="references"><h2 class="sec-title">参考文献</h2>']
        for c in e:
            parts.append(element_html(c, sec_level))
        parts.append("</section>")
        return "".join(parts)
    if name == "ref":
        return f'<div class="ref-item">{child_html(e, sec_level)}</div>'
    if name == "mixed-citation":
        return child_html(e, sec_level)
    return child_html(e, sec_level)


def get_article_xml(rec: Dict[str, Any]) -> Dict[str, Any]:
    doi = rec["doi"].replace("https://doi.org/", "")
    url = f"{SITE}/CN/{doi}"
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        page = s.get(url, timeout=45)
        page.raise_for_status()
        m = re.search(r'id="article_nlmdtdXml"\s+value="([^"]+)"', page.text)
        if not m:
            return {"ok": False, "error": "no mag xml path", "url": url}
        rel = html.unescape(m.group(1))
        xml_url = f"{SITE}/{rel}"
        xml_resp = s.get(xml_url, timeout=60)
        xml_resp.raise_for_status()
        if not xml_resp.text.lstrip().startswith("<?xml"):
            return {"ok": False, "error": "xml response is not xml", "url": url}
        # Save immediately so partial runs can continue.
        issue = int(rec["issue"])
        num = int(doi.rsplit(".", 1)[-1])
        xml_dir = XML_ROOT / f"V47I{issue:02d}"
        xml_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", rec["title"])[:70] or str(num)
        xml_path = xml_dir / f"{num:03d}-{safe}.xml"
        xml_path.write_bytes(xml_resp.content)
        return {
            "ok": True, "url": url, "xml_url": xml_url, "rel": rel,
            "article_id": rec.get("article_id", ""), "doi": rec["doi"],
            "issue": issue, "num": num, "title": rec["title"],
            "xml_path": str(xml_path),
            "pdf_stem": f"2026-V47I{issue:02d}-{num:03d}",
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "url": url}


def fetch_all_xmls(workers: int = 4) -> Dict[str, Any]:
    XML_ROOT.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))
    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(get_article_xml, r): r for r in rows}
        for i, f in enumerate(as_completed(futs), 1):
            out = f.result()
            results.append(out)
            if i % 10 == 0:
                print(f"  fetched {i}/{len(rows)}", flush=True)
    ok = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    manifest = {"total": len(rows), "ok": len(ok), "fail": len(fail), "items": results}
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"XML fetch done: {len(ok)}/{len(rows)} ok, {len(fail)} failed")
    for r in fail[:20]:
        print("  FAIL", r.get("url"), r.get("error"))
    return manifest


def xml_doc_value(root, meta_selector, name):
    return None

def build_html(xml_path: pathlib.Path, html_path: pathlib.Path) -> Dict[str, Any]:
    html_path.parent.mkdir(parents=True, exist_ok=True)
    root = etree.parse(str(xml_path)).getroot()
    front = first(root, "front")
    ameta = first(front, "article-meta") if front is not None else None
    journal = first(front, "journal-meta") if front is not None else None
    title = firsttext(ameta, "title-group").strip() if ameta is not None else ""
    # re-parse specifically because title-group nested
    tg = first(ameta, "title-group") if ameta is not None else None
    if tg is not None:
        t = first(tg, "article-title")
        title = text(t).strip() if t is not None else title
    title_en = ""
    if tg is not None:
        t = first(tg, "trans-article-title")
        title_en = text(t).strip() if t is not None else ""
    authors = []
    if ameta is not None:
        cg = first(ameta, "contrib-group")
        if cg is not None:
            for c in cg.xpath('*[local-name()="contrib"]'):
                if c.get("contrib-type") != "author":
                    continue
                na = first(c, "name-alternatives")
                if na is None:
                    na = c
                sn = first(na, "string-name")
                if sn is not None:
                    authors.append(text(sn).strip())
        # affiliations
    affs = []
    if ameta is not None:
        cg = first(ameta, "contrib-group")
        if cg is not None:
            for aa in cg.xpath('*[local-name()="aff-alternatives"]'):
                aff = first(aa, "aff")
                if aff is not None:
                    txt = " ".join(text(aff).split())
                    if txt:
                        affs.append(txt)
    abstract_cn = ""
    if ameta is not None:
        a = first(ameta, "abstract")
        if a is not None:
            abstract_cn = " ".join(text(a).split())
    abstract_en = ""
    if ameta is not None:
        a = first(ameta, "trans-abstract")
        if a is not None:
            abstract_en = " ".join(text(a).split())
    kw = []
    kw_en = []
    if ameta is not None:
        groups = ameta.xpath('*[local-name()="kwd-group"]')
        for g in groups:
            kws = [text(x).strip() for x in g.xpath('*[local-name()="kwd"]')]
            if kws:
                (kw_en if g.get("xml:lang") == "en" else kw).extend(kws)
    funding = ""
    if ameta is not None:
        fg = first(ameta, "funding-group")
        if fg is not None:
            funding = " ".join(text(fg).split())
    journal_name = "科研管理"
    if journal is not None:
        jg = first(journal, "journal-title-group")
        jt = first(jg, "journal-title") if jg is not None else None
        if jt is not None:
            journal_name = text(jt).strip() or journal_name
    vol = firsttext(ameta, "volume") if ameta is not None else ""
    issue = firsttext(ameta, "issue") if ameta is not None else ""
    fpage = firsttext(ameta, "fpage") if ameta is not None else ""
    lpage = firsttext(ameta, "lpage") if ameta is not None else ""
    doi = ""
    if ameta is not None:
        for aid in ameta.xpath('*[local-name()="article-id"]'):
            if aid.get("pub-id-type") == "doi":
                doi = text(aid).strip()
    abstract_html = ""
    if abstract_cn:
        abstract_html = ('<section class="abstract"><h3 class="sec-title">摘要</h3>'
                         f'<p class="body-p">{esc(abstract_cn)}</p>'
                         f'<div class="article-title-en">{esc(abstract_en)}</div></section>')
    kw_html = ""
    if kw or kw_en:
        kw_html = '<div class="kwd-group"><b>关键词：</b>' + "；".join(esc(x) for x in kw) + (
            '<br><b>Key words：</b>' + "; ".join(esc(x) for x in kw_en) if kw_en else "") + "</div>"
    funding_html = f'<div class="funding"><b>基金项目：</b>{esc(funding)}</div>' if funding else ""
    body = first(root, "body")
    back = first(root, "back")
    body_html = element_html(body, 0) if body is not None else ""
    back_html = element_html(back, 0) if back is not None else ""
    journal_line = f"{esc(journal_name)} 2026, {esc(vol)}({esc(issue)}): {esc(fpage)}-{esc(lpage)}"
    if doi:
        journal_line += f" | DOI: {esc(doi)}"
    aff_html = "".join(f'<div class="aff">{esc(a)}</div>' for a in affs[:10])
    html_doc = ARTICLE_TEMPLATE.replace("@@TITLE@@", esc(title)).replace("@@TITLE_EN@@", esc(title_en)).replace(
        "@@AUTHORS@@", esc("，".join(authors))).replace("@@AFFS@@", aff_html).replace(
        "@@JOURNAL_LINE@@", journal_line).replace("@@ABSTRACT@@", abstract_html).replace(
        "@@KEYWORDS@@", kw_html).replace("@@FUNDING@@", funding_html).replace(
        "@@BODY@@", body_html).replace("@@BACK@@", back_html)
    html_path.write_text(html_doc, encoding="utf-8")
    return {"title": title, "abstract": abstract_cn, "pages": len(body.xpath(".//*[local-name()='p']")) if body is not None else 0}


def fetch_assets_for_xml(xml_path: pathlib.Path, asset_dir: pathlib.Path, article_id: str) -> List[str]:
    asset_dir.mkdir(parents=True, exist_ok=True)
    root = etree.parse(str(xml_path)).getroot()
    base = f"{SITE}/article/2026/1000-2995/{article_id}"
    added = []
    for href in root.xpath('//@*[local-name()="href"]'):
        s = str(href)
        if not s.endswith((".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".svg", ".pdf")):
            continue
        if s.startswith("http"):
            url = s
        elif s.startswith("/"):
            url = SITE + s
        elif s.startswith("article/"):
            url = SITE + "/" + s
        else:
            url = f"{base}/{s}"
        target = asset_dir / pathlib.PurePosixPath(urlparse(s).path).name
        if target.exists() and target.stat().st_size > 0:
            added.append(str(target))
            continue
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            if r.status_code == 200 and len(r.content) > 100:
                target.write_bytes(r.content)
                added.append(str(target))
            else:
                print("  asset fail", url, r.status_code)
        except Exception as e:
            print("  asset err", url, e)
    return added


def render_all(workers: int = 2) -> Dict[str, Any]:
    from playwright.sync_api import sync_playwright
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ok_items = [x for x in manifest["items"] if x.get("ok")]
    # Ensure original publisher PDFs are separated once.
    original_dir = PDF_ROOT / "原版PDF"
    original_dir.mkdir(parents=True, exist_ok=True)
    if not (PDF_ROOT / "_原版已归档").exists():
        moved = 0
        for p in list(PDF_ROOT.glob("*.pdf")) + list(PDF_ROOT.glob("_旧官网重复/*.pdf")):
            dest = original_dir / p.name
            if p.resolve() != dest.resolve():
                if not dest.exists():
                    p.replace(dest)
                    moved += 1
        (PDF_ROOT / "_原版已归档").write_text("原版 publisher PDFs archived under 原版PDF/\n", encoding="utf-8")
        print(f"archived {moved} original PDFs")
    fails = []
    done = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=CHROME_SHELL)
        for idx, item in enumerate(ok_items, 1):
            xml_path = pathlib.Path(item["xml_path"])
            rel = item.get("rel", "")
            m_id = re.search(r"/1000-2995/(\d+)/", rel)
            article_id = m_id.group(1) if m_id else item.get("article_id", "")
            pdf = PDF_ROOT / f"{item['pdf_stem']}-{re.sub(r'[\\/:*?\"<>|\\r\\n]+', '_', item['title'])[:70]}.pdf"
            if pdf.exists() and pdf.stat().st_size > 20 * 1024:
                done += 1
                continue
            out_html = RENDER_ROOT / f"{item['pdf_stem']}" / "article.html"
            asset_dir = out_html.parent / "assets"
            try:
                info = build_html(xml_path, out_html)
                fetch_assets_for_xml(xml_path, asset_dir, article_id)
                page = browser.new_page()
                page.goto(out_html.as_uri(), wait_until="load", timeout=60000)
                page.pdf(path=str(pdf), format="A4", print_background=True,
                         margin={"top": "20mm", "bottom": "20mm", "left": "18mm", "right": "18mm"})
                page.close()
                done += 1
                if done % 10 == 0:
                    print(f"  rendered {done}/{len(ok_items)}", flush=True)
            except Exception as e:
                fails.append({"pdf": str(pdf), "error": f"{type(e).__name__}: {e}"})
                print("  RENDER FAIL", item["title"][:40], e)
        browser.close()
    print(f"render done {done}/{len(ok_items)}, fail {len(fails)}")
    return {"done": done, "total": len(ok_items), "fails": fails}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--render-only", action="store_true")
    args = ap.parse_args()
    if not args.render_only:
        fetch_all_xmls()
    if not args.fetch_only:
        render_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
