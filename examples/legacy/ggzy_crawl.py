#!/usr/bin/env python3
"""全国公共资源交易平台（ggzy.gov.cn）公告抓取器。

用法:
  python3 ggzy_crawl.py \
    --keyword 数据中心 --begin 2025-01-01 --end 2025-01-31 \
    --stages 0001,0002 --out-dir outputs

依赖: 本目录 ggzy_bridge.cjs（Playwright 真实浏览器过 WAF）+ 框架 core/browser。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

# 让框架可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from universal_scraper.core import HttpClient, export_rows, log, die
from universal_scraper.browser import crawl_ggzy_list, CaptchaError, BrowserBridgeError

BASE = "https://www.ggzy.gov.cn"
BRIDGE = Path(__file__).resolve().parent / "ggzy_bridge.cjs"

# 详情页链接可能是相对路径，统一补全
def full_url(u: str) -> str:
    if not u:
        return ""
    if u.startswith("http"):
        return u
    return BASE + (u if u.startswith("/") else "/" + u)


def fetch_detail(http: HttpClient, url: str) -> Dict[str, str]:
    """抓取公告详情正文。

    列表里的 url 形如 .../deal/html/a/xxx.html，正文在 iframe 页:
    .../deal/html/b/xxx.html（可普通 HTTP 直连）。正文在 <div class="detail_content">。
    """
    b_url = url.replace("/html/a/", "/html/b/")
    res = http.get(b_url, allow_html_404=True)
    html = res.get("text", "")
    title_m = re.search(r"<title>(.*?)</title>", html, re.S)

    # 优先取 .detail_content 容器
    dc = re.search(r'<div[^>]*class="[^"]*detail_content[^"]*"[^>]*>(.*?)</div>\s*</div>', html, re.S)
    body_html = dc.group(1) if dc else html
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", body_html, flags=re.S)
    body = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", "", body)
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    body = re.sub(r"\n[ \t]*\n+", "\n", body).strip()
    orig = re.search(r'<a[^>]*href="([^"]+)"[^>]*>\s*原文链接', html) or \
          re.search(r'原文链接\s*<a[^>]*href="([^"]+)"', html)
    return {
        "detail_status": str(res.get("status")),
        "detail_title": title_m.group(1).strip() if title_m else "",
        "detail_url": b_url,
        "detail_original_link": (orig.group(1) if orig else ""),
        "detail_body": body[:30000],
    }


STAGE_LABELS = {"0001": "招标公告", "0002": "中标公告"}


def main() -> None:
    ap = argparse.ArgumentParser(description="ggzy 公告抓取")
    ap.add_argument("--keyword", default="数据中心")
    ap.add_argument("--begin", default="2025-01-01")
    ap.add_argument("--end", default="2025-01-31")
    ap.add_argument("--stages", default="0001,0002", help="0001=交易公告(招标), 0002=成交公示(中标)")
    ap.add_argument("--out-dir", default="outputs")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--settle-ms", type=int, default=1200)
    ap.add_argument("--with-detail", action="store_true", help="同时抓取详情页正文")
    ap.add_argument("--detail-interval", type=float, default=0.8)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    http = HttpClient(min_interval=args.detail_interval)
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]

    all_rows = []
    for stage in stages:
        label = STAGE_LABELS.get(stage, stage)
        log(f"===== 抓取 {label} (stage={stage}) =====")
        try:
            recs = crawl_ggzy_list(
                BRIDGE, args.keyword, args.begin, args.end, stage,
                max_pages=args.max_pages, settle=args.settle_ms,
            )
        except CaptchaError as e:
            log(f"{label}: 触发验证码，跳过该阶段: {e}", "ERROR")
            continue
        except BrowserBridgeError as e:
            log(f"{label}: 浏览器桥失败: {e}", "ERROR")
            continue

        # 用户要求"名称包含关键词"，这里按标题精确过滤
        kw = args.keyword
        before = len(recs)
        recs = [r for r in recs if kw in (r.get("title") or "")]
        log(f"  标题包含「{kw}」: {before} -> {len(recs)} 条")

        rows = []
        for i, r in enumerate(recs, 1):
            row = {
                "stage": stage,
                "stage_label": label,
                "title": r.get("title"),
                "publish_time": r.get("publishTime"),
                "information_type": r.get("informationType"),
                "information_type_text": r.get("informationTypeText"),
                "business_type_text": r.get("businessTypeText"),
                "province": r.get("provinceText"),
                "city": r.get("cityText"),
                "industry_type_text": r.get("industryTypeText"),
                "tender_project_code": r.get("tenderProjectCode"),
                "id": r.get("id"),
                "url": full_url(r.get("url")),
                "data_source": r.get("dataSources"),
                "source_platform": r.get("transactionSourcesPlatformText"),
                "deadline": r.get("deadline"),
            }
            # 补充原始字段（防遗漏）
            for k, v in r.items():
                if k not in row:
                    row[f"raw_{k}"] = v
            if args.with_detail and row["url"]:
                try:
                    det = fetch_detail(http, row["url"])
                    row.update(det)
                    log(f"  [{i}/{len(recs)}] 详情 OK: {row['title'][:30]}...")
                except Exception as e:
                    row["detail_status"] = f"ERR:{e}"
                    log(f"  [{i}/{len(recs)}] 详情失败: {e}", "WARN")
                time.sleep(args.detail_interval)
            rows.append(row)
            all_rows.append(row)

        # 分阶段导出
        paths = export_rows(rows, out_dir, f"ggzy_{args.keyword}_{args.begin}_{args.end}_{label}")
        log(f"{label} 导出完成: " + ", ".join(f"{k}={v}" for k, v in paths.items()))

    # 合并导出
    if all_rows:
        paths = export_rows(all_rows, out_dir, f"ggzy_{args.keyword}_{args.begin}_{args.end}_合并")
        log("合并导出: " + ", ".join(f"{k}={v}" for k, v in paths.items()))
        log(f"总计 {len(all_rows)} 条")
    else:
        log("没有抓到任何记录", "WARN")


if __name__ == "__main__":
    main()
